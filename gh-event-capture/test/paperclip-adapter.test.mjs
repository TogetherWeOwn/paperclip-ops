import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { createPaperclipAdapter } from '../src/paperclip-adapter.js'
import { createConsumer } from '../src/consumer.js'

const companyId = '00000000-0000-4000-8000-000000000004'
const issueId = '00000000-0000-4000-8000-000000000011'
const productId = '00000000-0000-4000-8000-000000000012'
const otherId = '00000000-0000-4000-8000-000000000003'
const runId = '00000000-0000-4000-8000-000000000018'
const repo = 'TogetherWeOwn/paperclip-ops'
const sha = 'a'.repeat(40)
const token = 'TEST-PRIVATE-BOARD-KEY'
const issue = { id: issueId, companyId, identifier: 'case_22b092f5', status: 'done' }
const body = { type: 'pull_request', provider: 'github', title: 'Bridge PR',
  url: `https://github.com/${repo}/pull/42`, externalId: `${repo}#42`, status: 'active', reviewState: 'none',
  metadata: { repo, number: 42, headSha: sha, headRef: 'case_22b092f5-bridge' } }
const product = { ...body, id: productId, companyId, issueId }
const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body),
  { status, headers: { 'content-type': 'application/json', ...headers } })
function harness(responses = [], options = {}) {
  const calls = []
  const adapter = createPaperclipAdapter({ baseUrl: 'https://board.test', token, companyId,
    allowedRepositories: [repo], runId, fetchImpl: async (url, init) => {
      calls.push({ url, init })
      assert.ok(responses.length, 'unexpected request or automatic retry')
      const value = responses.shift()
      if (value instanceof Error) throw value
      return typeof value === 'function' ? value(url, init) : value
    }, ...options })
  return { adapter, calls }
}

test('open issues list requests explicit identity pages, validates the whole set and registers product-read scope', async () => {
  const open = { ...issue, status: 'in_progress', description: token }
  const h = harness([json([open]), json([]), json([])])
  assert.deepEqual(await h.adapter.listOpenIssues(), [{ id: issueId, identifier: issue.identifier, status: 'in_progress' }])
  assert.equal(h.calls[0].url, `https://board.test/api/companies/${companyId}/issues?status=todo,in_progress,in_review,blocked&includePluginOperations=true&includeRoutineExecutions=true&sortField=id&sortDir=asc&limit=500&view=compact`)
  assert.deepEqual(await h.adapter.listWorkProducts(issueId), [])
  assert.equal(h.calls.every((c) => c.init.method === 'GET'), true)
})

test('open issues list refuses envelopes, saturation, duplicate or malformed rows without partial registration', async () => {
  const open = { ...issue, status: 'todo' }
  for (const rows of [{ items: [open], total: 1 }, [open, open], [open, { ...open, id: otherId }],
    [open, { ...open, id: otherId, identifier: 'case_1ef046c6', companyId: otherId }],
    [{ ...open, status: 'done' }], [{ ...open, status: 'unknown' }], [{ ...open, id: 'bad' }],
    [{ ...open, identifier: 'case_05330ec4' }], [{ ...open, companyId: undefined }], Array(5001).fill(open), null]) {
    const h = harness([json(rows)])
    await assert.rejects(h.adapter.listOpenIssues())
    await assert.rejects(h.adapter.listWorkProducts(issueId), /resolved/)
    assert.equal(h.calls.length, 1)
  }
  const h = harness([json([], 206)])
  await assert.rejects(h.adapter.listOpenIssues(), /invalid/)
})

test('open issue snapshot includes more than the default 500 rows without truncation', async () => {
  const rows = Array.from({ length: 501 }, (_, i) => ({ ...issue, status: 'blocked',
    id: `11111111-1111-4111-8111-${String(i).padStart(12, '0')}`, identifier: `TOG-${i + 1}` }))
  const h = harness([json(rows.slice(0, 500)), json(rows.slice(500)), json([])])
  assert.equal((await h.adapter.listOpenIssues()).length, 501)
  assert.equal(new URL(h.calls[1].url).searchParams.get('afterId'), rows[499].id)
})

test('short pages require an empty terminator and remain within the request cap', async () => {
  const rows = Array.from({ length: 12 }, (_, i) => ({ ...issue, status: 'todo',
    id: `11111111-1111-4111-8111-${String(i).padStart(12, '0')}`, identifier: `TOG-${i + 1}` }))
  const h = harness([json(rows.slice(0, 2)), json(rows.slice(2)), json([])])
  assert.equal((await h.adapter.listOpenIssues()).length, 12)
  assert.equal(h.calls.length, 3)
  const limited = harness(rows.map((row) => json([row])))
  await assert.rejects(limited.adapter.listOpenIssues(), /budget/)
  assert.equal(limited.calls.length, 11)
  await assert.rejects(limited.adapter.listWorkProducts(rows[0].id), /resolved/)
})

test('compact open pages exclude oversized execution metadata', async () => {
  const open = { ...issue, status: 'todo' }
  const h = harness([(url) => json([{ ...open, ...(new URL(url).searchParams.get('view') === 'compact'
    ? {} : { executionState: 'x'.repeat(4 * 1024 * 1024) }) }]), json([])])
  assert.equal((await h.adapter.listOpenIssues()).length, 1)
})

test('open issue pagination refuses cap overflow and nonadvancing cursors without registering a partial set', async () => {
  const rows = Array.from({ length: 5001 }, (_, i) => ({ ...issue, status: 'todo',
    id: `11111111-1111-4111-8111-${String(i).padStart(12, '0')}`, identifier: `TOG-${i + 1}` }))
  for (const pages of [Array.from({ length: 11 }, (_, i) => json(rows.slice(i * 500, (i + 1) * 500))),
    [json(rows.slice(0, 500)), json([rows[0]])]]) {
    const h = harness(pages)
    await assert.rejects(h.adapter.listOpenIssues())
    await assert.rejects(h.adapter.listWorkProducts(rows[0].id), /resolved/)
  }
})

test('exact product routes, fields and audit header; no bare comment capability', async () => {
  const h = harness([json(issue), json([]), json(product, 201), json({ ...product, status: 'merged' })])
  assert.deepEqual(Object.keys(h.adapter).sort(), ['commentIfEligible', 'createWorkProduct', 'getIssue', 'listInteractions', 'listOpenIssues', 'listWorkProducts', 'updateWorkProduct'])
  assert.deepEqual(await h.adapter.getIssue('case_22b092f5'), { id: issueId, identifier: issue.identifier, companyId })
  assert.deepEqual(await h.adapter.listWorkProducts(issueId), [])
  await h.adapter.createWorkProduct(issueId, { ...body, assigneeAgentId: otherId, reopen: true, createdByRunId: otherId })
  await h.adapter.updateWorkProduct(productId, { ...body, status: 'merged' })
  assert.deepEqual(h.calls.map((c) => [c.init.method, new URL(c.url).pathname]), [
    ['GET', '/api/issues/case_22b092f5'], ['GET', `/api/issues/${issueId}/work-products`],
    ['POST', `/api/issues/${issueId}/work-products`], ['PATCH', `/api/work-products/${productId}`],
  ])
  assert.deepEqual(JSON.parse(h.calls[2].init.body), body)
  for (const { url, init } of h.calls) {
    assert.equal(url.includes(token), false)
    assert.equal(init.headers.authorization, `Bearer ${token}`)
    assert.equal(init.redirect, 'manual')
    assert.equal(init.credentials, 'omit')
    assert.equal(init.cache, 'no-store')
    assert.equal(init.headers['X-Paperclip-Run-Id'], init.method === 'GET' ? undefined : runId)
  }
})

test('cross-company/mismatched/malformed issue responses are rejected before writes', async () => {
  for (const bad of [{ ...issue, companyId: otherId }, { ...issue, identifier: 'case_4ee01500' },
    { ...issue, id: 'not-uuid' }, { ...issue, identifier: null }, null]) {
    const h = harness([json(bad)])
    await assert.rejects(h.adapter.getIssue('case_22b092f5'), /identity/)
    await assert.rejects(h.adapter.createWorkProduct(issueId, body), /resolved/)
    assert.equal(h.calls.length, 1)
  }
  const h = harness([json({ ...issue, id: otherId })])
  await assert.rejects(h.adapter.getIssue(issueId), /identity/)
  await assert.rejects(h.adapter.getIssue('../comments'), /selector/)
  assert.equal(h.calls.length, 1)
})

test('complete lists reject envelopes, wrong company/issue, duplicates and missing fields', async () => {
  for (const rows of [{ products: [] }, [product, product], [{ ...product, companyId: otherId }],
    [{ ...product, issueId: otherId }], [{ ...product, reviewState: undefined }],
    [{ ...product, metadata: [] }], [{ ...product, externalId: undefined }]]) {
    const h = harness([json(issue), json(rows)])
    await h.adapter.getIssue(issueId)
    await assert.rejects(h.adapter.listWorkProducts(issueId))
    await assert.rejects(h.adapter.updateWorkProduct(productId, body), /listed/)
    assert.equal(h.calls.length, 2)
  }
})

test('complete lists have no implicit 100-row cap, but enforce explicit product budget', async () => {
  const rows = Array.from({ length: 101 }, (_, i) => ({ ...product,
    id: `11111111-1111-4111-8111-${String(i).padStart(12, '0')}` }))
  const h = harness([json(issue), json(rows)])
  await h.adapter.getIssue(issueId)
  assert.equal((await h.adapter.listWorkProducts(issueId)).length, 101)
  const small = harness([json(issue), json(rows)], { maxProducts: 100 })
  await small.adapter.getIssue(issueId)
  await assert.rejects(small.adapter.listWorkProducts(issueId), /budget/)
})

test('only previously listed github PRs can be updated; refresh removes stale bindings', async () => {
  const h = harness([json(issue), json([{ ...product, type: 'branch' }]), json([product]), json([])])
  await h.adapter.getIssue(issueId)
  await h.adapter.listWorkProducts(issueId)
  await assert.rejects(h.adapter.updateWorkProduct(productId, body), /listed/)
  await h.adapter.listWorkProducts(issueId)
  await h.adapter.listWorkProducts(issueId)
  await assert.rejects(h.adapter.updateWorkProduct(productId, body), /listed/)
  assert.equal(h.calls.length, 4)
})

test('write scope and canonical PR identities are checked before network I/O', async () => {
  const h = harness([json(issue)])
  await h.adapter.getIssue(issueId)
  for (const patch of [{ type: 'branch' }, { provider: 'other' }, { url: 'https://attacker.test' },
    { externalId: `${repo}#43` }, { metadata: { ...body.metadata, repo: 'Other/repo' } },
    { metadata: { ...body.metadata, headSha: 'bad' } }, { reviewState: 'draft' }, { status: 'open' }, { title: '' }]) {
    await assert.rejects(h.adapter.createWorkProduct(issueId, { ...body, ...patch }))
  }
  await assert.rejects(h.adapter.listWorkProducts(otherId), /resolved/)
  assert.equal(h.calls.length, 1)
})

test('create and patch require exact product/company/issue and confirmed field echoes', async () => {
  for (const patch of [{ issueId: otherId }, { companyId: otherId }, { status: 'closed' },
    { url: 'https://attacker.test' }, { reviewState: 'approved' }, { metadata: { ...body.metadata, headSha: 'b'.repeat(40) } }]) {
    const h = harness([json(issue), json({ ...product, ...patch }, 201)])
    await h.adapter.getIssue(issueId)
    await assert.rejects(h.adapter.createWorkProduct(issueId, body))
    assert.equal(h.calls.length, 2)
  }
  const h = harness([json(issue), json([product]), json({ ...product, id: otherId })])
  await h.adapter.getIssue(issueId)
  await h.adapter.listWorkProducts(issueId)
  await assert.rejects(h.adapter.updateWorkProduct(productId, body), /confirm/)
})

test('HTTP/parse/redirect failures and provider exceptions are redacted without retries', async () => {
  for (const response of [json({ error: token }, 403), json(issue, 206), json(issue, 201),
    new Response(token, { status: 302, headers: { location: 'https://other.test', 'content-type': 'application/json' } }),
    new Response(token, { headers: { 'content-type': 'text/html' } }),
    new Response(token, { headers: { 'content-type': 'application/json' } }), new Error(token)]) {
    const h = harness([response])
    await assert.rejects(h.adapter.getIssue(issueId), (e) => {
      assert.equal(e.message.includes(token), false)
      assert.equal(e.cause, undefined)
      assert.match(e.message, /Paperclip request/)
      return true
    })
    assert.equal(h.calls.length, 1)
  }
})

test('declared and streamed byte limits reject otherwise valid data, exact budget succeeds', async () => {
  const length = Buffer.byteLength(JSON.stringify(issue))
  const control = harness([json(issue)], { maxResponseBytes: length })
  assert.equal((await control.adapter.getIssue(issueId)).id, issueId)
  for (const [response, budget] of [[json(issue, 200, { 'content-length': String(length + 1) }), length],
    [json(issue), length - 1]]) {
    const h = harness([response], { maxResponseBytes: budget })
    await assert.rejects(h.adapter.getIssue(issueId), /request failed/)
  }
})

test('deadlines remain active during headers and response body reads', async () => {
  for (const phase of ['headers', 'body']) {
    const h = harness([], { timeoutMs: 10, fetchImpl: async (url, { signal }) => {
      if (phase === 'headers') return new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error(token)), { once: true })
      })
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('{'))
        signal.addEventListener('abort', () => controller.error(new Error(token)), { once: true })
      } }), { headers: { 'content-type': 'application/json' } })
    } })
    await assert.rejects(h.adapter.getIssue(issueId), /timed out/)
  }
})

function fakeBoard(options = {}) {
  const h = { products: [], calls: [], loseCreate: options.loseCreate ?? false }
  h.fetch = async (url, init) => {
    const path = new URL(url).pathname
    h.calls.push([init.method, path])
    if (init.method === 'GET' && ['/api/issues/case_22b092f5', `/api/issues/${issueId}`].includes(path)) return json(issue)
    if (init.method === 'GET' && path === `/api/issues/${issueId}/work-products`) return json(h.products)
    if (init.method === 'POST' && path === `/api/issues/${issueId}/work-products`) {
      const p = { ...JSON.parse(init.body), id: productId, companyId, issueId }
      h.products.push(p)
      if (h.loseCreate) { h.loseCreate = false; throw new Error('lost response '+token) }
      return json(p, 201)
    }
    if (init.method === 'PATCH' && path === `/api/work-products/${productId}`) {
      Object.assign(h.products[0], JSON.parse(init.body))
      return json(h.products[0])
    }
    assert.fail('unexpected route: '+path)
  }
  h.pr = { number: 42, html_url: body.url, user: { login: 'togetherweown[bot]' },
    head: { ref: 'case_22b092f5-bridge', sha }, base: { repo: { full_name: repo } },
    title: 'Bridge PR', state: 'open', draft: false, merged: false, reviewDecision: null }
  h.adapter = createPaperclipAdapter({ baseUrl: 'https://board.test', token, companyId,
    allowedRepositories: [repo], fetchImpl: h.fetch })
  h.consumer = createConsumer({ board: h.adapter, github: { async getPullRequest() { return h.pr } },
    allowedRepositories: [repo], mode: 'products-only-v1' })
  h.delivery = (action = 'opened') => ({ delivery_id: 'a', event: 'pull_request', body_truncated: 0,
    body: JSON.stringify({ action, repository: { full_name: repo }, pull_request: h.pr }) })
  return h
}

test('real core plus HTTP contract double recovers a lost create response into one product', async () => {
  const h = fakeBoard({ loseCreate: true })
  await assert.rejects(h.consumer.processDelivery(h.delivery()), /Paperclip request/)
  assert.equal(h.products.length, 1)
  assert.equal(h.calls.filter(([method]) => method === 'POST').length, 1)
  await h.consumer.processDelivery(h.delivery())
  assert.equal(h.products.length, 1)
  assert.equal(h.calls.filter(([method]) => method === 'POST').length, 1)
  h.products[0].metadata.operatorNote = 'preserve'
  h.pr.merged = true; h.pr.state = 'closed'; h.pr.reviewDecision = 'APPROVED'
  const result = await h.consumer.processDelivery(h.delivery('closed'))
  assert.deepEqual(result.wakes, ['disabled-by-policy'])
  assert.equal(h.products[0].status, 'merged')
  assert.equal(h.products[0].reviewState, 'approved')
  assert.equal(h.products[0].metadata.operatorNote, 'preserve')
  assert.equal(h.calls.some(([, path]) => /comments|wakeup|bridge\/claim/.test(path)), false)
})

test('real loopback socket handles product writes with non-waking routes only', async (t) => {
  const h = fakeBoard()
  const server = createServer(async (req, res) => {
    try {
      assert.equal(req.headers.authorization, `Bearer ${token}`)
      let body = ''
      for await (const chunk of req) body += chunk
      const reply = await h.fetch(`http://localhost${req.url}`, { method: req.method, body: body || undefined })
      res.writeHead(reply.status, { 'content-type': 'application/json' })
      res.end(await reply.text())
    } catch { res.writeHead(500); res.end() }
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve) }))
  const adapter = createPaperclipAdapter({ baseUrl: `http://127.0.0.1:${server.address().port}`, token,
    companyId, allowedRepositories: [repo] })
  await adapter.getIssue('case_22b092f5')
  await adapter.listWorkProducts(issueId)
  assert.equal((await adapter.createWorkProduct(issueId, body)).id, productId)
  assert.equal((await adapter.updateWorkProduct(productId, { ...body, reviewState: 'approved' })).reviewState, 'approved')
  assert.equal(h.products.length, 1)
})

test('configuration failures redact secrets and refuse unsafe origins', () => {
  for (const options of [{ baseUrl: 'http://remote.test' }, { baseUrl: `https://${token}@board.test` },
    { baseUrl: 'https://board.test/api' }, { baseUrl: 'https://board.test?token='+token },
    { token: '' }, { companyId: 'bad' }, { runId: 'bad' }, { allowedRepositories: [] }, { timeoutMs: 0 },
    { maxResponseBytes: 0 }, { maxProducts: 0 }, { commentToken: '' }, { commentToken: 'has space' }]) {
    assert.throws(() => harness([], options), (e) => !e.message.includes(token))
  }
})

const commentToken = 'TEST-AGENT-COMMENT-KEY'
const eligibleIssue = { id: issueId, companyId, identifier: 'case_22b092f5', status: 'in_progress',
  assigneeAgentId: 'engineer', assigneeUserId: null, blockedBy: [] }
const wakePr = { repository: repo, number: 42 }
const wakeBody = { body: `GitHub bridge: [PR #42](https://github.com/${repo}/pull/42) merged at head \`${sha}\`.` }
const mergeConfirmation = { kind: 'request_confirmation', status: 'pending',
  title: `Please confirm that ${repo}#42 is merged`, summary: `Confirm that ${repo}#42 is merged`,
  payload: { prompt: `Please confirm the PR https://github.com/${repo}/pull/42 is merged and green`,
    acceptLabel: 'Confirm merge' } }
function gatedHarness({ issueRow = eligibleIssue, interactions = [], comment = { id: 'comment-1' } } = {}) {
  // One response per read in call order: scope resolve, the gate's own issue
  // re-read, the gate's interactions read, then the comment POST.
  const h = harness([json(issueRow), json(issueRow), json(interactions),
    ...(comment instanceof Error ? [comment] : [json(comment, 201)])], { commentToken })
  return h
}
async function resolveScope(h) {
  await h.adapter.getIssue('case_22b092f5')
}

test('gated comment posts under agent-actor auth only when its own reads show eligible', async () => {
  const h = gatedHarness()
  await resolveScope(h)
  const result = await h.adapter.commentIfEligible(issueId, wakeBody, wakePr)
  assert.deepEqual(result, { sent: true, comment: { id: 'comment-1' } })
  const post = h.calls.at(-1)
  assert.equal(new URL(post.url).pathname, `/api/issues/${issueId}/comments`)
  assert.equal(post.init.headers.authorization, `Bearer ${commentToken}`)
  assert.deepEqual(JSON.parse(post.init.body), { body: wakeBody.body, reopen: false, resume: false })
})

test('interaction reads stay scoped and fail closed on envelopes', async () => {
  const h = gatedHarness({ interactions: [mergeConfirmation] })
  await resolveScope(h)
  await h.adapter.getIssue(issueId)
  assert.deepEqual(await h.adapter.listInteractions(issueId), [mergeConfirmation])
  const enveloped = harness([json(issue), json({ items: [] })])
  await enveloped.adapter.getIssue(issueId)
  await assert.rejects(enveloped.adapter.listInteractions(issueId), /incomplete/)
  await assert.rejects(harness().adapter.listInteractions(issueId), /resolved/)
})

test('a pending merge confirmation for this PR does not block the gated post', async () => {
  const h = gatedHarness({ interactions: [mergeConfirmation] })
  await resolveScope(h)
  assert.deepEqual(await h.adapter.commentIfEligible(issueId, wakeBody, wakePr),
    { sent: true, comment: { id: 'comment-1' } })
  assert.equal(h.calls.filter((c) => c.init.method === 'POST').length, 1)
})

test('parked, blocked, terminal and questioned tasks get no POST (no-reopen race)', async () => {
  for (const [issueRow, interactions] of [
    [{ ...eligibleIssue, status: 'blocked' }, []],
    [{ ...eligibleIssue, status: 'done' }, []],
    [{ ...eligibleIssue, status: 'cancelled' }, []],
    [{ ...eligibleIssue, assigneeAgentId: null }, []],
    [{ ...eligibleIssue, assigneeUserId: 'operator' }, []],
    [{ ...eligibleIssue, blockedBy: [{ status: 'in_progress' }] }, []],
    [eligibleIssue, [{ status: 'pending' }]],
    [eligibleIssue, [{ ...mergeConfirmation, title: `Please confirm that ${repo}#43 is merged`,
      summary: `Confirm that ${repo}#43 is merged`,
      payload: { prompt: `Please confirm the PR https://github.com/${repo}/pull/43 is merged and green` } }]],
  ]) {
    const h = gatedHarness({ issueRow, interactions })
    await resolveScope(h)
    assert.deepEqual(await h.adapter.commentIfEligible(issueId, wakeBody, wakePr), { sent: false })
    assert.equal(h.calls.some((c) => c.init.method === 'POST'), false, 'ineligible tasks must see no comment POST')
  }
})

test('incomplete eligibility reads fail closed before any POST', async () => {
  for (const [issueRow, interactions] of [
    [{ ...eligibleIssue, blockedBy: undefined }, []],
    [{ ...eligibleIssue, status: undefined }, []],
    [eligibleIssue, { items: [] }],
    [eligibleIssue, [{ status: 'unknown' }]],
  ]) {
    const h = gatedHarness({ issueRow, interactions })
    await resolveScope(h)
    await assert.rejects(h.adapter.commentIfEligible(issueId, wakeBody, wakePr), /incomplete/)
    assert.equal(h.calls.some((c) => c.init.method === 'POST'), false)
  }
})

test('gated comment refuses without agent-actor credential, scope, context or hygienic body', async () => {
  const h = harness()
  await assert.rejects(h.adapter.commentIfEligible(issueId, wakeBody, wakePr), /agent-actor credential/)
  const scoped = gatedHarness()
  await assert.rejects(scoped.adapter.commentIfEligible(issueId, wakeBody, wakePr), /resolved/)
  const ready = gatedHarness()
  await resolveScope(ready)
  for (const [input, pr, pattern] of [
    [{}, wakePr, /body is invalid/], [{ body: '' }, wakePr, /body is invalid/],
    [{ body: 'wake [@x](agent://y)' }, wakePr, /must not mention/],
    [wakeBody, null, /repository and PR number/], [wakeBody, { repository: 'bad', number: 42 }, /repository and PR number/],
  ]) {
    await assert.rejects(ready.adapter.commentIfEligible(issueId, input, pr), pattern)
  }
  assert.equal(ready.calls.length, 1, 'refused bodies and contexts must not reach the network')
})

test('an unconfirmed comment write is loud and never retried', async () => {
  const h = gatedHarness({ comment: new Error('lost response') })
  await resolveScope(h)
  await assert.rejects(h.adapter.commentIfEligible(issueId, wakeBody, wakePr), /Paperclip request/)
  assert.equal(h.calls.filter((c) => c.init.method === 'POST').length, 1)
})
