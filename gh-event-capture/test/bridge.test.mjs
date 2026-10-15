// bridge.js offline suite (case_22b092f5). Pure decision logic, no network, no D1,
// no Paperclip credentials — see `src/bridge.js`'s header.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { extractIssueRef, classifyPullRequestEvent, classifyCheckSuiteEvent, claimKey } from '../src/bridge.js'

function pr(overrides = {}) {
  return {
    number: 42,
    html_url: 'https://github.com/TogetherWeOwn/kofra/pull/42',
    title: 'case_22b092f5: bridge worker events onto the board',
    head: { ref: 'case_22b092f5-bridge', sha: 'abc123' },
    user: { login: 'togetherweown[bot]' },
    draft: false,
    merged: false,
    ...overrides,
  }
}

// --- extractIssueRef --------------------------------------------------------

test('extractIssueRef finds a TOG-n reference, case-insensitively', () => {
  // Branch refs in this repo are lower-case by convention (`case_22b092f5-bridge`);
  // the match must be case-insensitive or check_suite's wake path never fires.
  assert.equal(extractIssueRef('case_22b092f5-bridge'), 'case_22b092f5')
  assert.equal(extractIssueRef('case_22b092f5-bridge'), 'case_22b092f5')
  assert.equal(extractIssueRef('Fixes case_f052904a and mentions case_22b092f5 later'), 'case_f052904a') // first match wins
  assert.equal(extractIssueRef('no reference here'), null)
  assert.equal(extractIssueRef(null), null)
  assert.equal(extractIssueRef(undefined), null)
})

// --- classifyPullRequestEvent -----------------------------------------------

test('opened by the agent with an issue ref in the branch creates a work product, no wake', () => {
  const d = classifyPullRequestEvent({ action: 'opened', pull_request: pr() })
  assert.deepEqual(d, {
    issueRef: 'case_22b092f5',
    action: 'opened',
    prNumber: 42,
    url: 'https://github.com/TogetherWeOwn/kofra/pull/42',
    title: 'case_22b092f5: bridge worker events onto the board',
    headSha: 'abc123',
    kind: 'work_product_create',
    status: 'active',
    wake: false,
  })
})

test('opened PR with no TOG- ref anywhere is not classified', () => {
  const d = classifyPullRequestEvent({
    action: 'opened',
    pull_request: pr({ head: { ref: 'chore-cleanup', sha: 'x' }, title: 'cleanup', body: null }),
  })
  assert.equal(d, null)
})

test('opened by a human (not the agent login) is not classified', () => {
  const d = classifyPullRequestEvent({ action: 'opened', pull_request: pr({ user: { login: 'Rick7C2' } }) })
  assert.equal(d, null)
})

test('reopened is treated the same as opened', () => {
  const d = classifyPullRequestEvent({ action: 'reopened', pull_request: pr() })
  assert.equal(d.kind, 'work_product_create')
  assert.equal(d.wake, false)
})

test('draft PR reports status draft without asserting a review verdict', () => {
  const d = classifyPullRequestEvent({ action: 'opened', pull_request: pr({ draft: true }) })
  assert.equal(d.status, 'draft')
  assert.equal(Object.hasOwn(d, 'reviewState'), false)
})

test('synchronize patches status, no wake', () => {
  const d = classifyPullRequestEvent({ action: 'synchronize', pull_request: pr({ head: { ref: 'case_22b092f5-bridge', sha: 'def456' } }) })
  assert.deepEqual(d, {
    issueRef: 'case_22b092f5',
    action: 'synchronize',
    prNumber: 42,
    url: 'https://github.com/TogetherWeOwn/kofra/pull/42',
    title: 'case_22b092f5: bridge worker events onto the board',
    headSha: 'def456',
    kind: 'work_product_update',
    status: 'active',
    wake: false,
  })
})

test('closed and merged wakes; closed and not merged does not', () => {
  const merged = classifyPullRequestEvent({ action: 'closed', pull_request: pr({ merged: true }) })
  assert.equal(merged.status, 'merged')
  assert.equal(merged.wake, true)

  const closed = classifyPullRequestEvent({ action: 'closed', pull_request: pr({ merged: false }) })
  assert.equal(closed.status, 'closed')
  assert.equal(closed.wake, false)
})

test('an action outside the scoped set is not classified', () => {
  assert.equal(classifyPullRequestEvent({ action: 'labeled', pull_request: pr() }), null)
  assert.equal(classifyPullRequestEvent({ action: 'assigned', pull_request: pr() }), null)
})

test('missing pull_request object is not classified', () => {
  assert.equal(classifyPullRequestEvent({ action: 'opened' }), null)
  assert.equal(classifyPullRequestEvent(null), null)
})

// --- classifyCheckSuiteEvent -------------------------------------------------

function checkSuite(overrides = {}) {
  return {
    status: 'completed',
    conclusion: 'success',
    head_sha: 'abc123',
    pull_requests: [{ number: 42, head: { ref: 'case_22b092f5-bridge' } }],
    ...overrides,
  }
}

test('completed check_suite against a TOG- branch yields one wake candidate', () => {
  const out = classifyCheckSuiteEvent({ repository: { full_name: 'TogetherWeOwn/kofra' }, check_suite: checkSuite() },
    [pr({ base: { repo: { full_name: 'TogetherWeOwn/kofra' } } })])
  assert.deepEqual(out, [
    { issueRef: 'case_22b092f5', prNumber: 42, headSha: 'abc123', conclusion: 'success', kind: 'check_suite_completed', wake: true },
  ])
})

test('non-completed check_suite yields nothing', () => {
  assert.deepEqual(classifyCheckSuiteEvent({ check_suite: checkSuite({ status: 'in_progress' }) }), [])
  assert.deepEqual(classifyCheckSuiteEvent({ check_suite: checkSuite({ status: 'queued' }) }), [])
})

test('completed check_suite with no associated PRs yields nothing', () => {
  assert.deepEqual(classifyCheckSuiteEvent({ check_suite: checkSuite({ pull_requests: [] }) }), [])
})

test('a PR whose branch carries no TOG- ref is skipped, others still yield', () => {
  const resolved = [
    pr({ number: 1, title: '', head: { ref: 'chore-cleanup', sha: 'abc123' }, base: { repo: { full_name: 'TogetherWeOwn/kofra' } } }),
    pr({ number: 2, head: { ref: 'case_f052904a-fix', sha: 'abc123' }, base: { repo: { full_name: 'TogetherWeOwn/kofra' } } }),
  ]
  const out = classifyCheckSuiteEvent({
    repository: { full_name: 'TogetherWeOwn/kofra' },
    check_suite: checkSuite({ pull_requests: resolved.map(({ number, head }) => ({ number, head })) }),
  }, resolved)
  assert.deepEqual(out, [
    { issueRef: 'case_f052904a', prNumber: 2, headSha: 'abc123', conclusion: 'success', kind: 'check_suite_completed', wake: true },
  ])
})

test('missing check_suite object yields nothing', () => {
  assert.deepEqual(classifyCheckSuiteEvent({}), [])
  assert.deepEqual(classifyCheckSuiteEvent(null), [])
})

// --- claimKey ----------------------------------------------------------------

test('claimKey is stable and distinguishes kind and head sha', () => {
  const a = claimKey({ issueRef: 'case_22b092f5', headSha: 'abc123', kind: 'check_suite_completed' })
  const b = claimKey({ issueRef: 'case_22b092f5', headSha: 'abc123', kind: 'check_suite_completed' })
  assert.equal(a, b)

  const differentSha = claimKey({ issueRef: 'case_22b092f5', headSha: 'def456', kind: 'check_suite_completed' })
  assert.notEqual(a, differentSha)

  const differentKind = claimKey({ issueRef: 'case_22b092f5', headSha: 'abc123', kind: 'pull_request_merged' })
  assert.notEqual(a, differentKind)
})

test('only work-product claims tolerate a null head sha', () => {
  const c = { issueRef: 'case_22b092f5', headSha: null, kind: 'work_product_create',
    url: pr().html_url, action: 'opened', deliveryId: 'd-1' }
  assert.equal(claimKey(c), claimKey({ ...c, headSha: undefined }))
  assert.throws(() => claimKey({ ...c, kind: 'check_suite_completed' }), /head_sha/)
})
