// bridge.js offline suite (T\x4fG-990017). Pure decision logic, no network, no D1,
// no Paperclip credentials — see `src/bridge.js`'s header.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { extractIssueRef, classifyPullRequestEvent, classifyCheckSuiteEvent, claimKey } from '../src/bridge.js'

function pr(overrides = {}) {
  return {
    number: 42,
    html_url: 'https://github.com/TogetherWeOwn/kofra/pull/42',
    title: 'T\x4fG-990017: bridge worker events onto the board',
    head: { ref: 't\x6fg-990017-bridge', sha: 'abc123' },
    user: { login: 'togetherweown[bot]' },
    draft: false,
    merged: false,
    ...overrides,
  }
}

// --- extractIssueRef --------------------------------------------------------

test('extractIssueRef finds a TOG-n reference, case-insensitively', () => {
  // Branch refs in this repo are lower-case by convention (`t\x6fg-990017-bridge`);
  // the match must be case-insensitive or check_suite's wake path never fires.
  assert.equal(extractIssueRef('t\x6fg-990017-bridge'), 'T\x4fG-990017')
  assert.equal(extractIssueRef('T\x4fG-990017-bridge'), 'T\x4fG-990017')
  assert.equal(extractIssueRef('Fixes T\x4fG-9 and mentions T\x4fG-990017 later'), 'T\x4fG-9') // first match wins
  assert.equal(extractIssueRef('no reference here'), null)
  assert.equal(extractIssueRef(null), null)
  assert.equal(extractIssueRef(undefined), null)
})

// --- classifyPullRequestEvent -----------------------------------------------

test('opened by the agent with an issue ref in the branch creates a work product, no wake', () => {
  const d = classifyPullRequestEvent({ action: 'opened', pull_request: pr() })
  assert.deepEqual(d, {
    issueRef: 'T\x4fG-990017',
    action: 'opened',
    prNumber: 42,
    url: 'https://github.com/TogetherWeOwn/kofra/pull/42',
    title: 'T\x4fG-990017: bridge worker events onto the board',
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
  const d = classifyPullRequestEvent({ action: 'synchronize', pull_request: pr({ head: { ref: 't\x6fg-990017-bridge', sha: 'def456' } }) })
  assert.deepEqual(d, {
    issueRef: 'T\x4fG-990017',
    action: 'synchronize',
    prNumber: 42,
    url: 'https://github.com/TogetherWeOwn/kofra/pull/42',
    title: 'T\x4fG-990017: bridge worker events onto the board',
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
    pull_requests: [{ number: 42, head: { ref: 't\x6fg-990017-bridge' } }],
    ...overrides,
  }
}

test('completed check_suite against a TOG- branch yields one wake candidate', () => {
  const out = classifyCheckSuiteEvent({ repository: { full_name: 'TogetherWeOwn/kofra' }, check_suite: checkSuite() },
    [pr({ base: { repo: { full_name: 'TogetherWeOwn/kofra' } } })])
  assert.deepEqual(out, [
    { issueRef: 'T\x4fG-990017', prNumber: 42, headSha: 'abc123', conclusion: 'success', kind: 'check_suite_completed', wake: true },
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
    pr({ number: 2, head: { ref: 't\x6fg-9-fix', sha: 'abc123' }, base: { repo: { full_name: 'TogetherWeOwn/kofra' } } }),
  ]
  const out = classifyCheckSuiteEvent({
    repository: { full_name: 'TogetherWeOwn/kofra' },
    check_suite: checkSuite({ pull_requests: resolved.map(({ number, head }) => ({ number, head })) }),
  }, resolved)
  assert.deepEqual(out, [
    { issueRef: 'T\x4fG-9', prNumber: 2, headSha: 'abc123', conclusion: 'success', kind: 'check_suite_completed', wake: true },
  ])
})

test('missing check_suite object yields nothing', () => {
  assert.deepEqual(classifyCheckSuiteEvent({}), [])
  assert.deepEqual(classifyCheckSuiteEvent(null), [])
})

// --- claimKey ----------------------------------------------------------------

test('claimKey is stable and distinguishes kind and head sha', () => {
  const a = claimKey({ issueRef: 'T\x4fG-990017', headSha: 'abc123', kind: 'check_suite_completed' })
  const b = claimKey({ issueRef: 'T\x4fG-990017', headSha: 'abc123', kind: 'check_suite_completed' })
  assert.equal(a, b)

  const differentSha = claimKey({ issueRef: 'T\x4fG-990017', headSha: 'def456', kind: 'check_suite_completed' })
  assert.notEqual(a, differentSha)

  const differentKind = claimKey({ issueRef: 'T\x4fG-990017', headSha: 'abc123', kind: 'pull_request_merged' })
  assert.notEqual(a, differentKind)
})

test('only work-product claims tolerate a null head sha', () => {
  const c = { issueRef: 'T\x4fG-990017', headSha: null, kind: 'work_product_create',
    url: pr().html_url, action: 'opened', deliveryId: 'd-1' }
  assert.equal(claimKey(c), claimKey({ ...c, headSha: undefined }))
  assert.throws(() => claimKey({ ...c, kind: 'check_suite_completed' }), /head_sha/)
})
