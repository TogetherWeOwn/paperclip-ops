// Host-side GitHub reads. Use the installed gh credential path, never shell
// interpolation or tokens in arguments. No polling or retries in this adapter.
// PR bodies travel via a 0600 temp file (--input), never argv: /proc is
// world-readable and this host is shared, so internal TOG references in a body
// must not appear in a process command line.
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { GH_PIN_CODE, childFailureFacts, graphqlErrorType } from './gh-pinned.js'

const execute = promisify(execFile)
const PR_QUERY = `query BridgePullRequest($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequest(number: $number) {
      number url title body state isDraft merged headRefName headRefOid reviewDecision
      author { __typename login }
      baseRepository { nameWithOwner }
    }
  }
}`
const OPEN_QUERY = `query BridgeOpenPullRequests($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(first: 100, after: $after, states: [OPEN], orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      nodes { number }
      pageInfo { hasNextPage endCursor }
    }
  }
}`
const VERDICTS = new Set([null, 'REVIEW_REQUIRED', 'APPROVED', 'CHANGES_REQUESTED'])
const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/
// One read for the closeout snapshot (design §5.1: the decision is computed
// from a fresh read, never from delivery payloads). Lifecycle, head, labels,
// merge state, auto-merge flag and the head commit's check rollup come from a
// single GraphQL instant, so checks can never be attributed to a different head
// than the one returned. Commits are matched by oid, not position: last:10 is
// only a window, and a head outside it fails loudly instead of reading a
// sibling commit's checks.
const SNAPSHOT_QUERY = `query BridgeCloseoutSnapshot($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner isPrivate
    pullRequest(number: $number) {
      number url title body state isDraft merged headRefName headRefOid reviewDecision
      mergeStateStatus baseRefName
      autoMergeRequest { enabledAt }
      author { __typename login }
      baseRepository { nameWithOwner }
      labels(first: 100) { nodes { name } pageInfo { hasNextPage } }
      commits(last: 10) {
        nodes { commit {
          oid pushedDate
          statusCheckRollup { contexts(first: 100) {
            nodes { __typename
              ... on CheckRun { name status conclusion completedAt }
              ... on StatusContext { context state createdAt } }
            pageInfo { hasNextPage } } } }
        }
      }
    }
  }
}`
// One read for the red-main verdict (design §5.6, slice S8): the default
// branch's head commit and its check rollup in a single GraphQL instant, so
// conclusions can never be attributed to a different SHA than the one
// returned. Chosen over stored `workflow_run` / `check_suite` deliveries per
// §5.1: deliveries are hints, and the incident decision is computed from a
// fresh read. A null rollup means no checks have ever reported, not a failed
// read: it becomes an empty set, which the red-main verdict reads as pending
// (unobservable CI is not green), never as green.
const MAIN_HEAD_QUERY = `query BridgeMainHead($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    defaultBranchRef {
      name
      target { __typename
        ... on Commit {
          oid pushedDate
          statusCheckRollup { contexts(first: 100) {
            nodes { __typename
              ... on CheckRun { name status conclusion completedAt }
              ... on StatusContext { context state createdAt } }
            pageInfo { hasNextPage } } } }
      }
    }
  }
}`
// GraphQL mergeStateStatus values; lowercased they are exactly the REST
// mergeable_state set closeout.js validates, so the mapping is case-folding.
const GRAPHQL_MERGE_STATE = new Set(['BEHIND', 'BLOCKED', 'CLEAN', 'DIRTY', 'DRAFT', 'HAS_HOOKS', 'UNSTABLE', 'UNKNOWN'])
// CheckRun.status values that mean "not finished". Anything else unmapped
// throws: an unknown terminal state must never read as pending forever.
const RUN_QUEUED = new Set(['QUEUED', 'PENDING', 'REQUESTED', 'WAITING'])
// StatusContext has no run to watch: EXPECTED is queued, PENDING is running,
// and ERROR/FAILURE both mean the status did not pass, so both read as failure.
const MS_LIMIT = 8.64e15

class SnapshotReadError extends Error {
  constructor(message, code) {
    super(message)
    this.code = code
  }
}

// Only adapter-created errors carry a diagnostic; never echo child streams or
// arbitrary exception messages into a public Actions log.
export function snapshotFailureCode(error) {
  return error instanceof SnapshotReadError ? error.code : 'snapshot-invalid'
}

class WriteFailureError extends Error {
  constructor(message, code) {
    super(message)
    this.code = code
  }
}

// Closed diagnostic set for write failures, so the sweep log can name the
// failure class — a nonexistent endpoint, a credential/scope refusal and a
// transport failure are three different fixes. Child text itself is never
// echoed: it can carry tokens or private bodies.
export function writeFailureCode(error) {
  return error instanceof WriteFailureError ? error.code : 'unknown'
}

// Map a failed child to its write class. The pinned runner (production)
// already reduced child output to fixed-shape facts; a raw execFile error
// (default runner) is reduced the same way here. Every other byte of child
// output stays out of logs and errors.
function writeStatusFromChild(error) {
  const facts = error?.code === GH_PIN_CODE ? error : childFailureFacts(error)
  if (typeof facts?.httpStatus === 'string' && /^[1-5][0-9]{2}$/.test(facts.httpStatus)) return `http-${facts.httpStatus}`
  return graphqlClass(facts?.graphqlErrorType) ?? 'unknown'
}

function graphqlClass(type) {
  return typeof type === 'string' && /^[A-Z][A-Z_]{0,39}$/.test(type)
    ? `graphql-${type.toLowerCase().replaceAll('_', '-')}` : null
}

// A runner that exits 0 with a GraphQL errors[] body still names the class
// instead of collapsing into `unconfirmed`.
function refuseGraphqlErrors(result, message) {
  const type = graphqlClass(graphqlErrorType(result))
  if (type) throw new WriteFailureError(message, type)
}

function requireValue(condition, message, code = null) {
  if (!condition) throw code === null ? new Error(message) : new SnapshotReadError(message, code)
}

function prNumber(number) {
  // GraphQL Int is signed 32-bit, not the whole JavaScript safe-integer range.
  return Number.isInteger(number) && number > 0 && number <= 2147483647
}

function normalize(pr, repository, number) {
  requireValue(pr && pr.number === number && pr.baseRepository?.nameWithOwner === repository &&
    pr.url === `https://github.com/${repository}/pull/${number}`, 'GitHub PR identity is incomplete or mismatched')
  requireValue(typeof pr.headRefOid === 'string' && /^[a-f0-9]{40}$/.test(pr.headRefOid) &&
    typeof pr.headRefName === 'string' && pr.headRefName.length > 0, 'GitHub PR head is incomplete')
  requireValue(typeof pr.title === 'string' && pr.title.length > 0 && typeof pr.body === 'string',
    'GitHub PR text is incomplete')
  requireValue(['OPEN', 'CLOSED', 'MERGED'].includes(pr.state) && typeof pr.isDraft === 'boolean' &&
    typeof pr.merged === 'boolean' && pr.merged === (pr.state === 'MERGED'), 'GitHub PR lifecycle is incomplete or inconsistent')
  requireValue(Object.hasOwn(pr, 'reviewDecision') && VERDICTS.has(pr.reviewDecision),
    'GitHub reviewDecision is missing or unknown')
  // A deleted author is an explicit null, not a missing API field. Only Bot
  // actors receive the REST [bot] suffix; a User can never be promoted to Bot.
  let user = null
  if (pr.author !== null) {
    requireValue(pr.author && ['Bot', 'User'].includes(pr.author.__typename) &&
      typeof pr.author.login === 'string' && /^[A-Za-z0-9-]+(?:\[bot\])?$/.test(pr.author.login),
    'GitHub PR author is incomplete')
    const bot = pr.author.__typename === 'Bot'
    requireValue(bot || !pr.author.login.endsWith('[bot]'), 'GitHub PR author type is inconsistent')
    user = { type: bot ? 'Bot' : 'User', login: bot && !pr.author.login.endsWith('[bot]')
      ? `${pr.author.login}[bot]` : pr.author.login }
  }
  return {
    number, html_url: pr.url, title: pr.title, body: pr.body, user,
    state: pr.state === 'OPEN' ? 'open' : 'closed', draft: pr.isDraft, merged: pr.merged,
    head: { ref: pr.headRefName, sha: pr.headRefOid }, base: { repo: { full_name: repository } },
    reviewDecision: pr.reviewDecision,
  }
}

function parseTime(value, message) {
  requireValue(typeof value === 'string', message)
  const ms = Date.parse(value)
  requireValue(Number.isInteger(ms) && ms >= 0 && ms <= MS_LIMIT, message)
  return ms
}

function normalizeCheck(node) {
  requireValue(node && typeof node === 'object', 'GitHub check entry is malformed')
  if (node.__typename === 'CheckRun') {
    requireValue(typeof node.name === 'string' && node.name.length > 0, 'GitHub check run has no name')
    if (node.status === 'COMPLETED') {
      requireValue(typeof node.conclusion === 'string' && node.conclusion.length > 0, 'completed check run has no conclusion')
      // Forward-compat: conclusions pass through lowercased (TIMED_OUT ->
      // timed_out). Unknown ones are closeout's call, which reads them as
      // pending rather than guessing.
      return { name: node.name, status: 'completed', conclusion: node.conclusion.toLowerCase(), completedMs: parseTime(node.completedAt, 'completed check run has no completion time') }
    }
    requireValue(node.status === 'IN_PROGRESS' || RUN_QUEUED.has(node.status), 'GitHub check run status is unknown')
    return { name: node.name, status: node.status === 'IN_PROGRESS' ? 'in_progress' : 'queued' }
  }
  if (node.__typename === 'StatusContext') {
    requireValue(typeof node.context === 'string' && node.context.length > 0, 'GitHub status has no context name')
    if (node.state === 'SUCCESS') return { name: node.context, status: 'completed', conclusion: 'success', completedMs: parseTime(node.createdAt, 'completed status has no creation time') }
    if (node.state === 'ERROR' || node.state === 'FAILURE') return { name: node.context, status: 'completed', conclusion: 'failure', completedMs: parseTime(node.createdAt, 'completed status has no creation time') }
    requireValue(node.state === 'EXPECTED' || node.state === 'PENDING', 'GitHub status state is unknown')
    return { name: node.context, status: node.state === 'PENDING' ? 'in_progress' : 'queued' }
  }
  throw new Error('GitHub check entry type is unknown')
}

// Readiness has no push-age decision. Repeated names are legitimate across
// workflows/status sources, but none may hide a failure or an unfinished run.
function readinessChecks(checks) {
  const rank = (check) => check.status !== 'completed' ? 1
    : ['success', 'neutral', 'skipped'].includes(check.conclusion) ? 0 : 2
  const byName = new Map()
  for (const check of checks) {
    const prior = byName.get(check.name)
    if (!prior || rank(check) > rank(prior) ||
      (rank(check) === rank(prior) && JSON.stringify(check) < JSON.stringify(prior))) {
      byName.set(check.name, check)
    }
  }
  return [...byName.values()].sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
}

function normalizeSnapshot(pr, repository, number, nowMs, readiness = false) {
  requireValue(pr && pr.number === number && pr.baseRepository?.nameWithOwner === repository &&
    pr.url === `https://github.com/${repository}/pull/${number}`, 'GitHub PR identity is incomplete or mismatched')
  requireValue(typeof pr.headRefOid === 'string' && /^[a-f0-9]{40}$/.test(pr.headRefOid) &&
    typeof pr.headRefName === 'string' && pr.headRefName.length > 0, 'GitHub PR head is incomplete')
  requireValue(['OPEN', 'CLOSED', 'MERGED'].includes(pr.state) && typeof pr.isDraft === 'boolean' &&
    typeof pr.merged === 'boolean' && pr.merged === (pr.state === 'MERGED'), 'GitHub PR lifecycle is incomplete or inconsistent')
  requireValue(Object.hasOwn(pr, 'reviewDecision') && VERDICTS.has(pr.reviewDecision),
    'GitHub reviewDecision is missing or unknown')
  // An explicit null is "no auto-merge"; a missing field is a dropped read.
  requireValue(Object.hasOwn(pr, 'autoMergeRequest') &&
    (pr.autoMergeRequest === null || (typeof pr.autoMergeRequest === 'object' && pr.autoMergeRequest !== null)),
  'GitHub auto-merge flag is missing')
  requireValue(typeof pr.baseRefName === 'string' && pr.baseRefName.length > 0, 'GitHub PR base is incomplete')
  requireValue(typeof pr.mergeStateStatus === 'string' && GRAPHQL_MERGE_STATE.has(pr.mergeStateStatus),
    'GitHub merge state is missing or unknown')
  requireValue(pr.labels && Array.isArray(pr.labels.nodes) &&
    pr.labels.nodes.every((l) => typeof l?.name === 'string' && l.name.length > 0) &&
    pr.labels.pageInfo?.hasNextPage === false, 'GitHub PR labels are incomplete or truncated')
  requireValue(pr.commits && Array.isArray(pr.commits.nodes) && pr.commits.nodes.length > 0,
    'GitHub PR commits are missing')
  const head = pr.commits.nodes.find((n) => n?.commit?.oid === pr.headRefOid)
  requireValue(head, 'GitHub head commit is outside the snapshot window; the PR moved during the read', 'head-outside-window')
  const headPushedMs = readiness && head.commit.pushedDate === null ? null
    : parseTime(head.commit.pushedDate, 'GitHub head push time is missing or invalid')
  // A null rollup means no checks have ever reported, not a failed read: it
  // becomes an empty set, which closeout reports as missing after its grace
  // period. Inventing entries here would be the silent direction.
  const rollup = head.commit.statusCheckRollup ?? null
  requireValue(rollup === null || (typeof rollup === 'object' &&
    (rollup.contexts === null || rollup.contexts === undefined ||
    (Array.isArray(rollup.contexts?.nodes) && rollup.contexts.pageInfo?.hasNextPage === false))),
  'GitHub check rollup is incomplete or truncated', 'check-rollup-incomplete')
  const checks = []
  const counts = new Map()
  for (const node of rollup?.contexts?.nodes ?? []) {
    let check
    try {
      check = normalizeCheck(node)
    } catch (error) {
      if (!readiness) throw error
      throw new SnapshotReadError('GitHub check entry is malformed', 'check-malformed')
    }
    // Closeout is age-sensitive and keeps its original ambiguity guard.
    requireValue(readiness || !counts.has(check.name), 'GitHub check rollup has duplicate check names')
    counts.set(check.name, (counts.get(check.name) ?? 0) + 1)
    checks.push(check)
  }
  requireValue(Number.isInteger(nowMs) && nowMs >= 0 && nowMs <= MS_LIMIT, 'snapshot clock is invalid')
  requireValue(headPushedMs === null || nowMs >= headPushedMs, 'snapshot clock precedes the head push')
  // The body is advisory readiness payload, not closeout evidence: fixtures
  // that predate it (and a PR with no description, which GitHub returns as
  // null) read as null, and the readiness duty treats null as undecidable
  // rather than "no block". Never defaulted to '' here: an empty string would
  // read as a body without a block and earn a write every pass.
  const body = typeof pr.body === 'string' ? pr.body : null
  return {
    repository, number, headSha: pr.headRefOid, baseRef: pr.baseRefName,
    state: pr.state === 'OPEN' ? 'open' : 'closed', merged: pr.merged, draft: pr.isDraft,
    body,
    autoMerge: pr.autoMergeRequest !== null,
    labels: pr.labels.nodes.map((l) => l.name),
    mergeableState: pr.mergeStateStatus.toLowerCase(),
    reviewDecision: pr.reviewDecision, headPushedMs, nowMs,
    checks: readiness ? readinessChecks(checks) : checks,
    ...(readiness ? { snapshotFacts: {
      headPushTime: headPushedMs === null ? 'null' : 'present',
      repeatedNames: [...counts.values()].filter((count) => count > 1).length,
      repeatedEntries: checks.length - counts.size,
    } } : {}),
  }
}

// Git ref hygiene for a branch interpolated into a REST path: rejects control
// characters, ref-metacharacters git itself forbids, and `..`, so `.`/`..`
// segments can never walk the path. encodeURIComponent applies on top.
function branchName(branch) {
  return typeof branch === 'string' && branch.length > 0 && branch.length <= 255 &&
    !/[\0-\x1f\x7f ~^:?*\[\\]/.test(branch) && !/(^|\/)\.\.(\/|$)/.test(branch) &&
    !/^[/.]/.test(branch) && !/[/.]$/.test(branch)
}

export function createGithubAdapter({ allowedRepositories, run = execute, maxPages = 100 }) {
  requireValue(Array.isArray(allowedRepositories) && allowedRepositories.length > 0 &&
    allowedRepositories.every((repo) => typeof repo === 'string' && REPOSITORY.test(repo)),
  'an explicit valid GitHub repository allowlist is required')
  requireValue(Number.isInteger(maxPages) && maxPages > 0 && maxPages <= 1000, 'GitHub page budget is invalid')
  const repositories = new Set(allowedRepositories)

  async function query(repository, document, variables = []) {
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    const [owner, name] = repository.split('/')
    let result
    try {
      const { stdout } = await run('gh', ['api', '--hostname', 'github.com', 'graphql',
        '-f', `query=${document}`, '-f', `owner=${owner}`, '-f', `name=${name}`, ...variables], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 4 * 1024 * 1024,
      })
      result = JSON.parse(stdout)
    } catch {
      // Child errors include stdout/stderr (potentially credentials or private
      // response bodies). Do not retain a cause or expose either stream.
      throw new SnapshotReadError('GitHub query failed: transport, timeout, output limit or invalid JSON', 'query-transport')
    }
    requireValue(result && typeof result === 'object' && !Array.isArray(result) &&
      (!Object.hasOwn(result, 'errors') || (Array.isArray(result.errors) && result.errors.length === 0)),
    'GitHub query returned errors or an incomplete response', 'query-response')
    const repo = result.data?.repository
    requireValue(repo && repo.nameWithOwner === repository, 'GitHub repository response is incomplete or mismatched')
    return repo
  }

  async function getPullRequest(repository, number) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    // Lifecycle, head, issue-binding text, author and aggregate review decision
    // come from one PR query, never independently timed REST + review reads.
    const repo = await query(repository, PR_QUERY, ['-F', `number=${number}`])
    return normalize(repo.pullRequest, repository, number)
  }

  async function getCloseoutSnapshot(repository, number, options = {}) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    // One authoritative read per flush (design §5.1). No polling, no retries:
    // a read that cannot complete throws and the caller leaves its cursor
    // unadvanced. nowMs is injectable only so tests can pin the clock.
    const { nowMs = Date.now() } = options ?? {}
    const repo = await query(repository, SNAPSHOT_QUERY, ['-F', `number=${number}`])
    requireValue(typeof repo.isPrivate === 'boolean', 'GitHub repository visibility is missing')
    return { ...normalizeSnapshot(repo.pullRequest, repository, number, nowMs), isPrivate: repo.isPrivate }
  }

  async function getReadinessSnapshot(repository, number, options = {}) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    const { nowMs = Date.now() } = options ?? {}
    const repo = await query(repository, SNAPSHOT_QUERY, ['-F', `number=${number}`])
    requireValue(typeof repo.isPrivate === 'boolean', 'GitHub repository visibility is missing')
    return { ...normalizeSnapshot(repo.pullRequest, repository, number, nowMs, true), isPrivate: repo.isPrivate }
  }

  async function disableAutoMerge(repository, number) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    // S6b disarm-only duty (design §2 + §12): this duty's ONLY merge
    // mutation. It never arms (`--auto`) or merges immediately. The module's
    // only other `gh pr merge` path is the reconcile duty's `enableAutoMerge`
    // arm below, which can merge immediately when the PR is already eligible.
    // Scoped to
    // the allowlist like every other call; a failure throws and the caller
    // leaves its cursor unadvanced. Disabling twice is safe (idempotent), so
    // the repair sweep may repeat it.
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    try {
      await run('gh', ['pr', 'merge', String(number), '--repo', repository, '--disable-auto'], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024,
      })
    } catch {
      // Child streams may carry credentials or private bodies.
      throw new Error('GitHub disarm failed: transport, timeout or output limit')
    }
  }

  function normalizeMainHead(repo, repository, nowMs) {
    requireValue(repo && repo.nameWithOwner === repository, 'GitHub repository response is incomplete or mismatched')
    const ref = repo.defaultBranchRef
    // A repo with no default branch (brand-new and empty) has no main head to
    // judge: fail loudly rather than read "no branches" as green.
    requireValue(ref && typeof ref.name === 'string' && ref.name.length > 0 && ref.name.length <= 255,
      'GitHub default branch is missing')
    const target = ref.target
    requireValue(target && target.__typename === 'Commit' &&
      typeof target.oid === 'string' && /^[a-f0-9]{40}$/.test(target.oid),
    'GitHub default-branch head is missing or not a commit')
    const headPushedMs = parseTime(target.pushedDate, 'GitHub head push time is missing or invalid')
    const rollup = target.statusCheckRollup ?? null
    requireValue(rollup === null || (typeof rollup === 'object' &&
      (rollup.contexts === null || rollup.contexts === undefined ||
      (Array.isArray(rollup.contexts?.nodes) && rollup.contexts.pageInfo?.hasNextPage === false))),
    'GitHub check rollup is incomplete or truncated')
    const checks = []
    const seen = new Set()
    for (const node of rollup?.contexts?.nodes ?? []) {
      const check = normalizeCheck(node)
      requireValue(!seen.has(check.name), 'GitHub check rollup has duplicate check names')
      seen.add(check.name)
      checks.push(check)
    }
    requireValue(Number.isInteger(nowMs) && nowMs >= 0 && nowMs <= MS_LIMIT, 'snapshot clock is invalid')
    requireValue(nowMs >= headPushedMs, 'snapshot clock precedes the head push')
    return { repository, branch: ref.name, mainSha: target.oid, headPushedMs, checks }
  }

  async function getMainHeadChecks(repository, options = {}) {
    // One authoritative main-head read per sweep (design §5.1). No polling,
    // no retries: a read that cannot complete throws and the caller records a
    // counted error. nowMs is injectable only so tests can pin the clock.
    const { nowMs = Date.now() } = options ?? {}
    const repo = await query(repository, MAIN_HEAD_QUERY)
    const read = normalizeMainHead(repo, repository, nowMs)
    // The required set is per repo+branch from branch protection: a check
    // that is not required here must never block this repo's green. A
    // ruleset-only repo 404s here, which throws loudly rather than reading
    // as "no required checks".
    const requiredChecks = await getRequiredChecks(repository, read.branch)
    return { ...read, requiredChecks }
  }

  async function rest(repository, path) {
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    const [owner, name] = repository.split('/')
    let result
    try {
      const { stdout } = await run('gh', ['api', '--hostname', 'github.com', `repos/${owner}/${name}/${path}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 4 * 1024 * 1024,
      })
      result = JSON.parse(stdout)
    } catch {
      // As in query(): child streams may carry credentials or private bodies.
      throw new Error('GitHub request failed: transport, timeout, output limit or invalid JSON')
    }
    requireValue(result && typeof result === 'object' && !Array.isArray(result), 'GitHub response is incomplete')
    return result
  }

  async function getRequiredChecks(repository, branch) {
    requireValue(branchName(branch), 'GitHub branch is invalid')
    // Legacy branch-protection read, one call, no retries. A branch with no
    // protection 404s, which surfaces as a failure here: a ruleset-only repo
    // must never silently read as "no required checks". required_status_checks
    // itself null means protection without named checks, which is truthfully [].
    const protection = await rest(repository, `branches/${encodeURIComponent(branch)}/protection`)
    requireValue(Object.hasOwn(protection, 'required_status_checks'), 'GitHub branch protection is incomplete')
    const required = protection.required_status_checks
    if (required === null) return []
    requireValue(typeof required === 'object' && !Array.isArray(required) &&
      Array.isArray(required.contexts) && Array.isArray(required.checks), 'GitHub required checks are incomplete')
    const names = new Set()
    for (const context of required.contexts) {
      requireValue(typeof context === 'string' && context.length > 0, 'GitHub required check name is invalid')
      names.add(context)
    }
    for (const check of required.checks) {
      requireValue(check && typeof check.context === 'string' && check.context.length > 0, 'GitHub required check name is invalid')
      names.add(check.context)
    }
    // Never filter here: an oddly named required check must reach the policy,
    // which rejects unsafe names loudly, rather than being silently dropped.
    return [...names].sort()
  }

  async function listOpenPullRequests(repository) {
    const references = []
    const seenNumbers = new Set()
    const seenCursors = new Set()
    let after = null
    let total = null
    for (let page = 0; page < maxPages; page++) {
      const repo = await query(repository, OPEN_QUERY, after === null ? [] : ['-f', `after=${after}`])
      const connection = repo.pullRequests
      requireValue(connection && Array.isArray(connection.nodes) && connection.nodes.length <= 100 &&
        Number.isSafeInteger(connection.totalCount) && connection.totalCount >= 0 &&
        typeof connection.pageInfo?.hasNextPage === 'boolean' &&
        (connection.pageInfo.endCursor === null || typeof connection.pageInfo.endCursor === 'string'),
      'GitHub open PR page is incomplete')
      if (total === null) total = connection.totalCount
      requireValue(total === connection.totalCount, 'GitHub open PR set changed during pagination; retry the scan')
      for (const node of connection.nodes) {
        requireValue(prNumber(node?.number) && !seenNumbers.has(node.number), 'GitHub open PR page contains invalid or duplicate identities')
        seenNumbers.add(node.number)
        references.push({ repository, number: node.number })
      }
      requireValue(references.length <= total, 'GitHub open PR count is inconsistent')
      if (!connection.pageInfo.hasNextPage) {
        requireValue(references.length === total, 'GitHub open PR scan is incomplete')
        return references
      }
      const cursor = connection.pageInfo.endCursor
      requireValue(connection.nodes.length > 0 && typeof cursor === 'string' && cursor.length > 0 &&
        !seenCursors.has(cursor), 'GitHub open PR cursor did not advance')
      seenCursors.add(cursor)
      after = cursor
    }
    // Never return a truncated backfill as a completed scan.
    throw new Error('GitHub open PR page budget exceeded; scan is incomplete')
  }

  // Ruleset required checks for one base branch. The readiness gate
  // reads these — the branch RULESETS, never the webhook payload and never
  // legacy branch protection (which 404s on ruleset-only repos; getRequiredChecks
  // above stays the closeout path). One GET against the branch-scoped rules
  // endpoint, which returns only the rules already applying to that branch —
  // no ref-pattern matching and no ruleset-detail fetch on our side. (The
  // org-wide /rulesets list cannot be used: it answers list summaries with no
  // `rules`, and gh upgrades the call to POST once `-f` params are added.)
  async function getRulesetRequiredChecks(repository, branch) {
    requireValue(branchName(branch), 'GitHub branch is invalid')
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    const [owner, name] = repository.split('/')
    let rules
    try {
      const { stdout } = await run('gh', ['api', '--hostname', 'github.com', '--method', 'GET',
        `repos/${owner}/${name}/rules/branches/${encodeURIComponent(branch)}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 4 * 1024 * 1024,
      })
      rules = JSON.parse(stdout)
    } catch {
      throw new Error('GitHub ruleset list failed: transport, timeout, output limit or invalid JSON')
    }
    requireValue(Array.isArray(rules), 'GitHub ruleset list is incomplete')
    const names = new Set()
    for (const rule of rules) {
      requireValue(rule && typeof rule === 'object', 'GitHub ruleset entry is malformed')
      if (rule?.type !== 'required_status_checks') continue
      // The REST payload for rules/branches/{branch} names the list
      // `required_status_checks`, one rule object per source ruleset.
      const required = rule.parameters?.required_status_checks
      requireValue(Array.isArray(required), 'GitHub ruleset required checks are incomplete')
      for (const check of required) {
        requireValue(check && typeof check.context === 'string' && check.context.length > 0,
          'GitHub ruleset required check name is invalid')
        names.add(check.context)
      }
    }
    // Never filter here: an oddly named required check must reach the policy,
    // which rejects unsafe names loudly, rather than being silently dropped.
    return [...names].sort()
  }

  async function updatePullRequestBody(repository, number, body) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    requireValue(typeof body === 'string' && body.length > 0 && body.length <= 65536,
      'GitHub PR body is invalid')
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    const [owner, name] = repository.split('/')
    // The body travels in a 0600 file, never argv: /proc is world-readable and
    // this host is shared, so internal references in the text must not appear
    // in a command line. The directory is 0700 for the same reason.
    const dir = await mkdtemp(join(tmpdir(), 'gh-pr-body-'))
    const file = join(dir, 'body.json')
    try {
      await writeFile(file, JSON.stringify({ body }), { mode: 0o600 })
      await run('gh', ['api', '--hostname', 'github.com', '--method', 'PATCH',
        '--input', file, `repos/${owner}/${name}/pulls/${number}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024,
      })
    } catch (error) {
      throw new WriteFailureError('GitHub body update failed: transport, timeout or output limit', writeStatusFromChild(error))
    } finally {
      await rm(dir, { recursive: true, force: true })
    }
  }

  // GraphQL only: GitHub ships no REST pulls/{n}/ready_for_review endpoint
  // (the REST OpenAPI has no such path), so the earlier REST writer 404'd on
  // every call while the identical-shape GraphQL convert-to-draft below
  // worked. Mark-ready resolves the node id the same way and confirms the
  // mutation left the PR non-draft, so a silent no-op fails closed.
  async function markReadyForReview(repository, number) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    const [owner, name] = repository.split('/')
    const idQuery = 'query ReadinessNodeId($owner: String!, $name: String!, $number: Int!) {' +
      ' repository(owner: $owner, name: $name) { nameWithOwner pullRequest(number: $number) { id } } }'
    const mutation = 'mutation ReadinessToReady($pullRequestId: ID!) {' +
      ' markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) { pullRequest { isDraft } } }'
    try {
      const idOut = await run('gh', ['api', '--hostname', 'github.com', 'graphql',
        '-f', `query=${idQuery}`, '-f', `owner=${owner}`, '-f', `name=${name}`,
        '-F', `number=${number}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024,
      })
      const idResult = JSON.parse(idOut.stdout)
      const nodeId = idResult?.data?.repository?.pullRequest?.id
      if (!(typeof nodeId === 'string' && /^[A-Za-z0-9+/=_-]+$/.test(nodeId) && nodeId.length <= 256)) {
        throw new WriteFailureError('GitHub PR node id is incomplete', 'node-id-incomplete')
      }
      const mutateOut = await run('gh', ['api', '--hostname', 'github.com', 'graphql',
        '-f', `query=${mutation}`, '-f', `pullRequestId=${nodeId}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024,
      })
      const mutateResult = JSON.parse(mutateOut.stdout)
      refuseGraphqlErrors(mutateResult, 'GitHub mark-ready was refused')
      if (mutateResult?.data?.markPullRequestReadyForReview?.pullRequest?.isDraft !== false) {
        throw new WriteFailureError('GitHub mark-ready did not confirm a non-draft', 'unconfirmed')
      }
    } catch (error) {
      if (error instanceof WriteFailureError) throw error
      throw new WriteFailureError('GitHub mark-ready failed: transport, timeout, output limit or invalid JSON', writeStatusFromChild(error))
    }
  }

  async function convertToDraft(repository, number) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    const [owner, name] = repository.split('/')
    const idQuery = 'query ReadinessNodeId($owner: String!, $name: String!, $number: Int!) {' +
      ' repository(owner: $owner, name: $name) { nameWithOwner pullRequest(number: $number) { id } } }'
    const mutation = 'mutation ReadinessToDraft($pullRequestId: ID!) {' +
      ' convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) { pullRequest { isDraft } } }'
    try {
      const idOut = await run('gh', ['api', '--hostname', 'github.com', 'graphql',
        '-f', `query=${idQuery}`, '-f', `owner=${owner}`, '-f', `name=${name}`,
        '-F', `number=${number}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024,
      })
      const idResult = JSON.parse(idOut.stdout)
      const nodeId = idResult?.data?.repository?.pullRequest?.id
      if (!(typeof nodeId === 'string' && /^[A-Za-z0-9+/=_-]+$/.test(nodeId) && nodeId.length <= 256)) {
        throw new WriteFailureError('GitHub PR node id is incomplete', 'node-id-incomplete')
      }
      const mutateOut = await run('gh', ['api', '--hostname', 'github.com', 'graphql',
        '-f', `query=${mutation}`, '-f', `pullRequestId=${nodeId}`], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024,
      })
      const mutateResult = JSON.parse(mutateOut.stdout)
      refuseGraphqlErrors(mutateResult, 'GitHub convert-to-draft was refused')
      if (mutateResult?.data?.convertPullRequestToDraft?.pullRequest?.isDraft !== true) {
        throw new WriteFailureError('GitHub convert-to-draft did not confirm a draft', 'unconfirmed')
      }
    } catch (error) {
      if (error instanceof WriteFailureError) throw error
      throw new WriteFailureError('GitHub convert-to-draft failed: transport, timeout, output limit or invalid JSON', writeStatusFromChild(error))
    }
  }

  async function enableAutoMerge(repository, number, headSha) {
    requireValue(prNumber(number), 'GitHub PR number is invalid')
    // The reconcile duty's ONLY arm path (design: readiness-handler.md). It
    // can merge immediately when the PR is already eligible. The only other
    // `gh pr merge` path is the S6b `disableAutoMerge` disarm above, which
    // only disables auto-merge. Scoped to the
    // allowlist like every other call; a failure throws and the caller records
    // it. Arming twice is safe (idempotent), so the sweep may repeat it. The
    // head SHA binds the arm to the exact head the gate evaluated
    // (shouldEnableAutoMerge): without it gh exits 1 during flag parsing and
    // the arm would silently never bind to any head.
    requireValue(typeof headSha === 'string' && /^[a-f0-9]{40}$/.test(headSha),
      'GitHub auto-merge head sha is invalid')
    requireValue(repositories.has(repository), 'GitHub repository is outside the configured scope')
    try {
      await run('gh', ['pr', 'merge', String(number), '--repo', repository,
        '--auto', '--squash', '--match-head-commit', headSha], {
        encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024,
      })
    } catch (error) {
      throw new WriteFailureError('GitHub auto-merge arm failed: transport, timeout or output limit', writeStatusFromChild(error))
    }
  }

  return { getPullRequest, listOpenPullRequests, getCloseoutSnapshot, getReadinessSnapshot, getRequiredChecks,
    getRulesetRequiredChecks, updatePullRequestBody, markReadyForReview, convertToDraft,
    enableAutoMerge, disableAutoMerge, getMainHeadChecks }
}
