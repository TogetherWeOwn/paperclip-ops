// Readiness offline suite (T\x4fG-990009). Pure decision logic, no network,
// no credentials — see `src/readiness.js`'s header.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  READINESS_START,
  READINESS_END,
  hasMalformedReadinessMarkers,
  REVIEW_CHECK,
  buildReadinessBlock,
  parseReadinessBlock,
  upsertReadinessBlock,
  validateReadinessBlock,
  shouldMarkReady,
  shouldConvertToDraft,
  shouldEnableAutoMerge,
  isRelevantReadinessEvent,
} from '../src/readiness.js'

const REPO = 'TogetherWeOwn/paperclip-ops'
const HEAD = 'a'.repeat(40)
const HEAD2 = 'b'.repeat(40)

function snapshotChecks() {
  return [
    { name: 'ci-ok', status: 'completed', conclusion: 'success', completedMs: 1 },
    { name: 'gitleaks', status: 'completed', conclusion: 'success', completedMs: 1 },
    { name: 'pr-lint', status: 'completed', conclusion: 'skipped', completedMs: 1 },
  ]
}

function block(overrides = {}) {
  return buildReadinessBlock({
    repository: REPO,
    number: 42,
    headSha: HEAD,
    checks: snapshotChecks(),
    reviewScore: null,
    mergeable: 'clean',
    updatedMs: 1728285600000,
    ...overrides,
  })
}

// --- build ---------------------------------------------------------------

test('build renders head link, check emojis, review pending, mergeable and updated', () => {
  const b = block()
  assert.ok(b.startsWith(READINESS_START))
  assert.ok(b.endsWith(READINESS_END))
  assert.ok(b.includes(`\`${HEAD}\``), 'full head sha present')
  assert.ok(b.includes(HEAD.slice(0, 7)), 'short sha present')
  assert.ok(b.includes(`https://github.com/${REPO}/pull/42/commits/${HEAD}`))
  assert.ok(b.includes('| ci-ok | ✅ |'))
  assert.ok(b.includes('| gitleaks | ✅ |'))
  // skipped counts as passing (same rule as closeout.js)
  assert.ok(b.includes('| pr-lint | ✅ |'))
  assert.ok(b.includes(`${REVIEW_CHECK}: pending ⏳`))
  assert.ok(b.includes('Mergeable: clean'))
  assert.ok(b.includes('Updated: '))
  assert.ok(b.includes('do not edit'))
})

test('build is deterministic for fixed inputs', () => {
  assert.equal(block(), block())
})

test('failing and pending checks render ❌ and ⏳', () => {
  const b = block({
    checks: [
      { name: 'ci-ok', status: 'completed', conclusion: 'failure', completedMs: 1 },
      { name: 'gitleaks', status: 'in_progress' },
    ],
  })
  assert.ok(b.includes('| ci-ok | ❌ |'))
  assert.ok(b.includes('| gitleaks | ⏳ |'))
})

test('review 5/5 renders ✅, any other score renders ❌', () => {
  assert.ok(block({ reviewScore: '5/5' }).includes(`${REVIEW_CHECK}: 5/5 ✅`))
  assert.ok(block({ reviewScore: '3/5' }).includes(`${REVIEW_CHECK}: 3/5 ❌`))
})

test('review check in the rollup does not double-render as a table row', () => {
  const b = block({ checks: [...snapshotChecks(), { name: REVIEW_CHECK, status: 'completed', conclusion: 'success', completedMs: 1 }] })
  assert.equal((b.match(/Paperclip Review/g) ?? []).length, 1)
})

test('build rejects bad repository, sha, names and clocks', () => {
  assert.throws(() => block({ repository: '../evil' }))
  assert.throws(() => block({ headSha: 'short' }))
  assert.throws(() => block({ checks: [{ name: 'x'.repeat(65), status: 'completed', conclusion: 'success', completedMs: 1 }] }))
  assert.throws(() => block({ checks: [{ name: 'ci-ok', status: 'weird' }] }))
  assert.throws(() => block({ checks: 'not-a-list' }))
  assert.throws(() => block({ updatedMs: -1 }))
})

// --- parse / upsert ------------------------------------------------------

test('parse returns null when markers are absent', () => {
  assert.equal(parseReadinessBlock('no markers here'), null)
  assert.equal(parseReadinessBlock(null), null)
})

test('parse extracts the exact head sha', () => {
  const parsed = parseReadinessBlock(`intro\n\n${block()}\n`)
  assert.equal(parsed.headSha, HEAD)
})

test('upsert appends when missing and replaces only the marked span', () => {
  const body = '## What Changed\n\n- a fix\n'
  const added = upsertReadinessBlock(body, block())
  assert.ok(added.startsWith(body))
  assert.ok(added.includes(READINESS_START))
  // Author text outside the span survives a refresh byte-for-byte.
  const refreshed = upsertReadinessBlock(added, block({ headSha: HEAD2 }))
  assert.ok(refreshed.startsWith(body))
  assert.ok(refreshed.includes(HEAD2))
  assert.ok(!refreshed.includes(HEAD))
})

test('upsert with an identical block is a no-op (handler can skip the write)', () => {
  const once = upsertReadinessBlock('intro', block())
  assert.equal(upsertReadinessBlock(once, block()), once)
})

test('validate accepts a fresh block and flags a missing one', () => {
  assert.deepEqual(validateReadinessBlock(block()), [])
  assert.deepEqual(validateReadinessBlock('no block'), ['readiness block is missing'])
})

test('validate flags a hand-edited block', () => {
  const broken = block().replace(/`[a-f0-9]{40}`/, '`hand-edited`')
  assert.ok(validateReadinessBlock(broken).some((p) => p.includes('head SHA')))
  const noTable = block().replace('| Check | State |', '| Checks |')
  assert.ok(validateReadinessBlock(noTable).some((p) => p.includes('check table')))
})

test('malformed markers fail closed: parse misses, upsert throws, never duplicates or deletes', () => {
  const good = `intro\n\n${block()}\n`
  // Stray end marker before the block: the old code appended a second block
  // on every event (each write fires edited, looping the handler).
  const strayEnd = `intro\n${READINESS_END}\n\n${block()}\n`
  assert.equal(parseReadinessBlock(strayEnd)?.headSha, HEAD, 'end marker binds after start')
  assert.equal(hasMalformedReadinessMarkers(strayEnd), true)
  assert.throws(() => upsertReadinessBlock(strayEnd, block()), /malformed/)
  // Missing end marker: the old code replaced across the stray span on the
  // next pass, swallowing author text. Now it throws instead.
  const missingEnd = good.replace(READINESS_END, '')
  assert.equal(parseReadinessBlock(missingEnd), null)
  assert.equal(hasMalformedReadinessMarkers(missingEnd), true)
  assert.throws(() => upsertReadinessBlock(missingEnd, block()), /malformed/)
  // Duplicated block: also malformed, never silently merged.
  const doubled = `${good}\n${block()}\n`
  assert.equal(hasMalformedReadinessMarkers(doubled), true)
  assert.throws(() => upsertReadinessBlock(doubled, block()), /malformed/)
  // Clean shapes stay quiet.
  assert.equal(hasMalformedReadinessMarkers(good), false)
  assert.equal(hasMalformedReadinessMarkers('no markers'), false)
  assert.equal(hasMalformedReadinessMarkers(''), false)
})

// --- gates ---------------------------------------------------------------

test('ready gate fires only for drafts with every required check green on the exact head', () => {
  const base = { draft: true, headSha: HEAD, blockHeadSha: HEAD, checks: snapshotChecks(), requiredChecks: ['ci-ok', 'gitleaks', 'pr-lint'] }
  assert.equal(shouldMarkReady(base), true)
  // Already ready: no second transition.
  assert.equal(shouldMarkReady({ ...base, draft: false }), false)
  // Stale block (new push not yet reconciled): do not mark ready.
  assert.equal(shouldMarkReady({ ...base, blockHeadSha: HEAD2 }), false)
  // One check failing or missing: stay draft.
  const failing = snapshotChecks().map((c) => (c.name === 'ci-ok' ? { ...c, conclusion: 'failure' } : c))
  assert.equal(shouldMarkReady({ ...base, checks: failing }), false)
  assert.equal(shouldMarkReady({ ...base, checks: snapshotChecks().slice(0, 2) }), false)
  // The review check is excluded: pending review never blocks the ready transition.
  assert.equal(shouldMarkReady({ ...base, requiredChecks: ['ci-ok', 'gitleaks', 'pr-lint', REVIEW_CHECK] }), true)
})

test('draft reset fires on a new head or a completed failure — never on pending', () => {
  const green = { draft: false, headSha: HEAD2, blockHeadSha: HEAD, checks: snapshotChecks(), requiredChecks: ['ci-ok'] }
  assert.equal(shouldConvertToDraft(green), true, 'new head under a ready PR goes back to draft')
  const red = { draft: false, headSha: HEAD, blockHeadSha: HEAD, checks: [{ name: 'ci-ok', status: 'completed', conclusion: 'failure', completedMs: 1 }], requiredChecks: ['ci-ok'] }
  assert.equal(shouldConvertToDraft(red), true, 'completed failure on the same head goes back to draft')
  const steady = { draft: false, headSha: HEAD, blockHeadSha: HEAD, checks: snapshotChecks(), requiredChecks: ['ci-ok', 'gitleaks'] }
  assert.equal(shouldConvertToDraft(steady), false, 'green ready PR stays ready')
  assert.equal(shouldConvertToDraft({ ...steady, draft: true }), false, 'already draft: no transition')
  // The handler's own block write and the ready_for_review transition re-queue
  // checks (pr-lint runs on edited): pending on the same head must wait, not
  // flap back to draft and re-trigger review every cycle.
  const pending = { draft: false, headSha: HEAD, blockHeadSha: HEAD, checks: [{ name: 'ci-ok', status: 'in_progress' }], requiredChecks: ['ci-ok'] }
  assert.equal(shouldConvertToDraft(pending), false, 'pending check on the same head stays ready')
  const missing = { draft: false, headSha: HEAD, blockHeadSha: HEAD, checks: [], requiredChecks: ['ci-ok'] }
  assert.equal(shouldConvertToDraft(missing), false, 'missing check on the same head stays ready')
})

function reviewPass() {
  return { name: REVIEW_CHECK, status: 'completed', conclusion: 'success', completedMs: 1 }
}

test('auto-merge arms only when CI and the review check pass on THIS head', () => {
  const base = { draft: false, headSha: HEAD, autoMergeEnabled: false, checks: [...snapshotChecks(), reviewPass()], requiredChecks: ['ci-ok', 'gitleaks', 'pr-lint'] }
  assert.equal(shouldEnableAutoMerge(base), true)
  assert.equal(shouldEnableAutoMerge({ ...base, draft: true }), false, 'never arm a draft')
  assert.equal(shouldEnableAutoMerge({ ...base, autoMergeEnabled: true }), false, 'already armed: no-op')
  // The verdict is bound through the rollup, never a score string: a missing,
  // pending or failing review check on this head refuses, even with green CI.
  assert.equal(shouldEnableAutoMerge({ ...base, checks: snapshotChecks() }), false, 'missing review check never arms')
  const pendingReview = [...snapshotChecks(), { name: REVIEW_CHECK, status: 'in_progress' }]
  assert.equal(shouldEnableAutoMerge({ ...base, checks: pendingReview }), false, 'pending review never arms')
  const failingReview = [...snapshotChecks(), { name: REVIEW_CHECK, status: 'completed', conclusion: 'failure', completedMs: 1 }]
  assert.equal(shouldEnableAutoMerge({ ...base, checks: failingReview }), false, 'failing review never arms')
  const failingCi = [...snapshotChecks().map((c) => (c.name === 'ci-ok' ? { ...c, conclusion: 'failure' } : c)), reviewPass()]
  assert.equal(shouldEnableAutoMerge({ ...base, checks: failingCi }), false, 'red CI never arms')
})

// --- event filter ---------------------------------------------------------

test('only readiness-relevant events reconcile', () => {
  assert.equal(isRelevantReadinessEvent('pull_request', 'opened'), true)
  assert.equal(isRelevantReadinessEvent('pull_request', 'synchronize'), true)
  assert.equal(isRelevantReadinessEvent('pull_request', 'labeled'), false)
  assert.equal(isRelevantReadinessEvent('check_run', 'completed'), true)
  assert.equal(isRelevantReadinessEvent('check_run', 'created'), false)
  assert.equal(isRelevantReadinessEvent('check_suite', 'completed'), true)
  assert.equal(isRelevantReadinessEvent('pull_request_review', 'submitted'), true)
  assert.equal(isRelevantReadinessEvent('status', 'success'), true)
  assert.equal(isRelevantReadinessEvent('push', null), false)
  assert.equal(isRelevantReadinessEvent(null, null), false)
})
