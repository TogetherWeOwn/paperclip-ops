// Paperclip transport: product writes for products-only-v1, plus the S5a gated
// wake transport. The wake path (listInteractions + commentIfEligible) is the
// ONLY comment operation: a bare comment POST is never exposed, because a
// board-actor comment implicitly reopens blocked/terminal assigned tasks and no
// flag disables that move. commentIfEligible therefore gates on its own reads
// and posts under agent-actor auth, where comments stay communicative.
import { wakeEligible } from './wake-eligibility.js'
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/
const REF = /^TOG-[1-9]\d*$/
const REPO = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/
const TYPES = new Set(['preview_url', 'runtime_service', 'pull_request', 'branch', 'commit', 'artifact', 'document'])
const STATUSES = new Set(['active', 'ready_for_review', 'approved', 'changes_requested', 'merged', 'closed', 'failed', 'archived', 'draft'])
const REVIEWS = new Set(['none', 'needs_board_review', 'approved', 'changes_requested'])
function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const uuid = (v) => typeof v === 'string' && UUID.test(v)

const COMMENT_MAX_BYTES = 4096
export const OPEN_ISSUE_LIST_PAGE_SIZE = 500
export const OPEN_ISSUE_LIST_MAX_ROWS = 5000
export const OPEN_ISSUE_LIST_MAX_REQUESTS = Math.floor(OPEN_ISSUE_LIST_MAX_ROWS / OPEN_ISSUE_LIST_PAGE_SIZE) + 1

export function createPaperclipAdapter({ baseUrl, token, companyId, allowedRepositories,
  runId = null, commentToken = null, fetchImpl = globalThis.fetch, timeoutMs = 30000,
  maxResponseBytes = 4 * 1024 * 1024, maxProducts = 10000 }) {
  let origin
  try {
    const url = new URL(baseUrl)
    requireValue((url.protocol === 'https:' || (url.protocol === 'http:' &&
      ['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname))) && !url.username && !url.password &&
      url.pathname === '/' && !url.search && !url.hash, 'invalid origin')
    origin = url.origin
  } catch { throw new Error('Paperclip base URL must be HTTPS or loopback HTTP, with no credentials, path or query') }
  requireValue(typeof token === 'string' && token.length > 0 && !/\s/.test(token), 'Paperclip credential is missing or invalid')
  requireValue(uuid(companyId) && (runId === null || uuid(runId)), 'Paperclip company/run identity is invalid')
  // The gated comment posts under agent-actor auth: only agent-actor comments
  // are exempt from the implicit reopen of blocked/terminal assigned tasks.
  // Absent, commentIfEligible refuses and the core never claims a wake.
  requireValue(commentToken === null || (typeof commentToken === 'string' && commentToken.length > 0 &&
    !/\s/.test(commentToken)), 'Paperclip comment credential is invalid')
  requireValue(Array.isArray(allowedRepositories) && allowedRepositories.length > 0 &&
    allowedRepositories.every((repo) => typeof repo === 'string' && REPO.test(repo)), 'Paperclip repository scope is required')
  requireValue(Number.isInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 120000 &&
    Number.isInteger(maxResponseBytes) && maxResponseBytes > 0 && maxResponseBytes <= 16 * 1024 * 1024 &&
    Number.isInteger(maxProducts) && maxProducts > 0 && maxProducts <= 100000, 'Paperclip transport budget is invalid')
  const repositories = new Set(allowedRepositories)
  const issues = new Set()
  const productIssues = new Map()

  async function request(method, path, body, authToken = token) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    let response
    let reader
    try {
      const encoded = body === undefined ? undefined : JSON.stringify(body)
      requireValue(encoded === undefined || Buffer.byteLength(encoded) <= maxResponseBytes, 'request too large')
      response = await fetchImpl(origin + '/api' + path, {
        method, redirect: 'manual', credentials: 'omit', cache: 'no-store', signal: controller.signal,
        headers: { authorization: `Bearer ${authToken}`, accept: 'application/json',
          ...(encoded === undefined ? {} : { 'content-type': 'application/json' }),
          ...(method !== 'GET' && runId !== null ? { 'X-Paperclip-Run-Id': runId } : {}) },
        ...(encoded === undefined ? {} : { body: encoded }),
      })
      requireValue((response.status === 200 || (method === 'POST' && response.status === 201)) &&
        !response.redirected && /^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') ?? ''),
      'invalid response')
      const length = response.headers.get('content-length')
      requireValue(length === null || (/^\d+$/.test(length) && Number(length) <= maxResponseBytes), 'response too large')
      reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8', { fatal: true })
      let bytes = 0
      let text = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        bytes += value.byteLength
        requireValue(bytes <= maxResponseBytes, 'response too large')
        text += decoder.decode(value, { stream: true })
      }
      text += decoder.decode()
      return JSON.parse(text)
    } catch {
      // Do not retain provider error messages, response bodies or nested causes.
      // In particular, a failed write is never automatically retried here.
      throw new Error(controller.signal.aborted ? 'Paperclip request timed out' : 'Paperclip request failed or response was invalid')
    } finally {
      clearTimeout(timer)
      if (reader) { void reader.cancel().catch(() => {}); reader.releaseLock() }
      else if (response?.body) void response.body.cancel().catch(() => {})
    }
  }
  async function getIssue(refOrId) {
    requireValue(typeof refOrId === 'string' && (REF.test(refOrId) || uuid(refOrId)), 'Paperclip issue selector is invalid')
    const issue = await request('GET', `/issues/${encodeURIComponent(refOrId)}`)
    requireValue(object(issue) && uuid(issue.id) && issue.companyId === companyId && REF.test(issue.identifier) &&
      (uuid(refOrId) ? issue.id === refOrId : issue.identifier === refOrId), 'Paperclip issue identity or company does not match')
    issues.add(issue.id)
    // Product mode needs identity only. Do not expose partial eligibility data
    // that a full-mode caller could mistake for an atomic eligibility contract.
    return { id: issue.id, identifier: issue.identifier, companyId }
  }
  async function listOpenIssues() {
    // The API clamps large limits and omits plugin/routine origins by default.
    // Traverse bounded compact identity pages; only an empty page is EOF.
    const rows = []
    const ids = new Set()
    const refs = new Set()
    let afterId = null
    for (let pageNumber = 0; ; pageNumber++) {
      requireValue(pageNumber < OPEN_ISSUE_LIST_MAX_REQUESTS, 'Paperclip open issue pagination exceeded its budget')
      const page = await request('GET', `/companies/${companyId}/issues?status=todo,in_progress,in_review,blocked` +
        `&includePluginOperations=true&includeRoutineExecutions=true&sortField=id&sortDir=asc&limit=${OPEN_ISSUE_LIST_PAGE_SIZE}&view=compact` +
        (afterId === null ? '' : `&afterId=${afterId}`))
      requireValue(Array.isArray(page) && page.length <= OPEN_ISSUE_LIST_PAGE_SIZE &&
        rows.length + page.length <= OPEN_ISSUE_LIST_MAX_ROWS,
      'Paperclip open issue list is incomplete or exceeds its budget')
      for (const row of page) {
        requireValue(object(row) && uuid(row.id) && row.companyId === companyId && typeof row.identifier === 'string' &&
          REF.test(row.identifier) && ['todo', 'in_progress', 'in_review', 'blocked'].includes(row.status) &&
          !ids.has(row.id) && !refs.has(row.identifier) && (afterId === null || row.id > afterId),
        'Paperclip open issue identity or shape does not match')
        ids.add(row.id); refs.add(row.identifier); rows.push(row); afterId = row.id
      }
      if (page.length === 0) break
    }
    // Register only after the entire response validates, never a partial list.
    for (const id of ids) issues.add(id)
    return rows.map(({ id, identifier, status }) => ({ id, identifier, status }))
  }
  function issueScope(id) {
    requireValue(uuid(id) && issues.has(id), 'Paperclip issue must first be resolved in this company')
  }
  function productShape(p, issueId) {
    requireValue(object(p) && uuid(p.id) && p.companyId === companyId && p.issueId === issueId &&
      TYPES.has(p.type) && typeof p.provider === 'string' && p.provider.length > 0 &&
      typeof p.title === 'string' && p.title.length > 0 && STATUSES.has(p.status) && REVIEWS.has(p.reviewState) &&
      (p.url === null || typeof p.url === 'string') && (p.externalId === null || typeof p.externalId === 'string') &&
      (p.metadata === null || object(p.metadata)), 'Paperclip product identity or shape does not match')
  }
  async function listWorkProducts(issueId) {
    issueScope(issueId)
    // Installed API returns the complete array, with no pagination or limit.
    // Refuse a future envelope/cap rather than treating it as an empty result.
    const rows = await request('GET', `/issues/${issueId}/work-products`)
    requireValue(Array.isArray(rows) && rows.length <= maxProducts, 'Paperclip product list is incomplete or exceeds its budget')
    const ids = new Set()
    for (const p of rows) {
      productShape(p, issueId)
      requireValue(!ids.has(p.id), 'Paperclip product list contains duplicate identities')
      ids.add(p.id)
    }
    for (const [id, bound] of productIssues) if (bound === issueId) productIssues.delete(id)
    for (const p of rows) if (p.type === 'pull_request' && p.provider === 'github') productIssues.set(p.id, issueId)
    return rows
  }
  function writeBody(body) {
    requireValue(object(body) && body.type === 'pull_request' && body.provider === 'github' &&
      typeof body.title === 'string' && body.title.length > 0 && STATUSES.has(body.status) && REVIEWS.has(body.reviewState) &&
      object(body.metadata), 'Paperclip PR write is incomplete')
    const { repo, number, headSha } = body.metadata
    requireValue(repositories.has(repo) && Number.isSafeInteger(number) && number > 0 &&
      typeof headSha === 'string' && /^[a-f0-9]{40}$/.test(headSha) &&
      body.url === `https://github.com/${repo}/pull/${number}` && body.externalId === `${repo}#${number}`,
    'Paperclip PR write is outside repository scope or has conflicting identity')
    // Whitelist fields. Callers cannot smuggle assignees, status transitions of
    // issues, run attribution or other issue fields through a product request.
    return { type: 'pull_request', provider: 'github', title: body.title, url: body.url,
      externalId: body.externalId, status: body.status, reviewState: body.reviewState, metadata: body.metadata }
  }
  async function write(method, path, issueId, body, expectedId) {
    issueScope(issueId)
    const payload = writeBody(body)
    const result = await request(method, path, payload)
    productShape(result, issueId)
    requireValue((!expectedId || result.id === expectedId) &&
      ['type', 'provider', 'title', 'url', 'externalId', 'status', 'reviewState'].every((key) => result[key] === payload[key]) &&
      result.metadata?.repo === payload.metadata.repo && result.metadata?.number === payload.metadata.number &&
      result.metadata?.headSha === payload.metadata.headSha, 'Paperclip did not confirm the PR write')
    productIssues.set(result.id, issueId)
    return result
  }
  async function createWorkProduct(issueId, body) {
    issueScope(issueId)
    return write('POST', `/issues/${issueId}/work-products`, issueId, body)
  }
  async function updateWorkProduct(id, body) {
    requireValue(uuid(id) && productIssues.has(id), 'Paperclip product must first be listed in this company')
    return write('PATCH', `/work-products/${id}`, productIssues.get(id), body, id)
  }
  async function listInteractions(issueId) {
    issueScope(issueId)
    const rows = await request('GET', `/issues/${issueId}/interactions`)
    requireValue(Array.isArray(rows), 'Paperclip interactions response is incomplete')
    return rows
  }
  // S5a wake transport: the gate and the post are one operation. Callers never
  // check-then-post themselves, so a stale eligibility read cannot be laundered
  // into a post. Returns { sent: false } when complete reads show ineligibility
  // (no POST is issued), and throws — failing closed, never waking — on
  // incomplete reads, missing PR context, or an unhygienic body.
  //
  // No-reopen proof. Two facts bound the race between this gate and the POST:
  // (1) the POST carries agent-actor auth plus explicit reopen:false /
  // resume:false, and agent-actor comments never implicitly move lifecycle, so
  // even a task that turned blocked/terminal after the gate keeps its status;
  // (2) the body carries no '@', so a stray post can wake nobody new — at worst
  // a communicative comment lands, never a lifecycle transition. A board-auth
  // POST could not offer this, which is why this method refuses without an
  // agent-actor commentToken.
  async function commentIfEligible(issueId, input, pr) {
    requireValue(typeof commentToken === 'string',
      'comment transport requires an agent-actor credential; refusing before any write')
    issueScope(issueId)
    requireValue(object(input) && typeof input.body === 'string' && input.body.length > 0 &&
      Buffer.byteLength(input.body) <= COMMENT_MAX_BYTES, 'wake comment body is invalid')
    // Wake text is fixed words plus a validated PR URL and head SHA. PR
    // titles/bodies can carry mentions, so any mention marker fails the body
    // before any read: this transport can wake nobody it was not given.
    requireValue(!input.body.includes('@'), 'wake comment must not mention')
    requireValue(object(pr) && REPO.test(pr.repository) &&
      Number.isSafeInteger(pr.number) && pr.number > 0, 'wake comment needs a repository and PR number')
    const prContext = { repository: pr.repository, number: pr.number }
    const issue = await request('GET', `/issues/${issueId}`)
    requireValue(object(issue) && issue.id === issueId && issue.companyId === companyId &&
      typeof issue.identifier === 'string' && typeof issue.status === 'string' &&
      Array.isArray(issue.blockedBy) &&
      (issue.assigneeAgentId === null || typeof issue.assigneeAgentId === 'string') &&
      (issue.assigneeUserId === null || typeof issue.assigneeUserId === 'string'),
    'Paperclip issue eligibility read is incomplete')
    const interactions = await listInteractions(issueId)
    if (!wakeEligible(issue, interactions, prContext)) return { sent: false }
    const posted = await request('POST', `/issues/${issueId}/comments`,
      { body: input.body, reopen: false, resume: false }, commentToken)
    requireValue(object(posted) && typeof posted.id === 'string' && posted.id.length > 0,
      'Paperclip did not confirm the wake comment')
    return { sent: true, comment: posted }
  }
  return { getIssue, listOpenIssues, listWorkProducts, createWorkProduct, updateWorkProduct, listInteractions, commentIfEligible }
}
