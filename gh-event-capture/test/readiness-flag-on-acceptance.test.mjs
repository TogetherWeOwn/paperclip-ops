// Independent acceptance for TOG-35419: readiness sweep flag-on for
// paperclip-ops (PR #64). Mirrors the readiness-flag-on scenarios from
// OUTSIDE the PR branch: no import from the PR's own test file, independent
// fixtures, PR numbers, check sets and helper shapes.
//
// Expected posture:
//   * Scope + disclosure pins are RED on main (flag-off: enabledRepositories
//     is empty, header still says UNPROVEN) and GREEN on the PR head.
//   * Arm / review-bind / convergence gates exercise the pre-existing pure
//     logic, so they are GREEN on main too; the mutant-kill proofs (M1/M2)
//     show they are not vacuous — removing the bind or widening the policy
//     fails loudly.
//   * No network, no credentials. The live ruleset read is a manual step in
//     the verdict, never part of this offline suite.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  REVIEW_CHECK,
  buildReadinessBlock,
  parseReadinessBlock,
  shouldMarkReady,
  shouldConvertToDraft,
  shouldEnableAutoMerge,
} from '../src/readiness.js'
import {
  decideReadinessActions,
  readinessPolicy,
  createReconciler,
} from '../src/readiness-reconcile.js'

const OPS = 'TogetherWeOwn/paperclip-ops'
const FOREIGN = 'TogetherWeOwn/two-web-next'
const HEAD_A = 'c'.repeat(40)
const HEAD_B = 'd'.repeat(40)
const FIXED_NOW = 1730000000000
// Live ruleset (2026-10-21 disclosure): Paperclip Review + ci-ok + pr-lint +
// public-safety. The sweep's requiredChecks carry the CI trio; the review
// verdict binds unconditionally on top, never through this list.
const REQUIRED_TRIO = ['ci-ok', 'pr-lint', 'public-safety']

const here = dirname(fileURLToPath(import.meta.url))
const shippedPolicyPath = join(here, '..', 'readiness-policy.json')
const examplePolicyPath = join(here, '..', 'readiness-policy.example.json')
const sweepWorkflowPath = join(here, '..', '..', '.github', 'workflows', 'readiness-sweep.yml')

function rollupEntry(name, status = 'completed', conclusion = 'success') {
  return conclusion === null
    ? { name, status }
    : { name, status, conclusion, completedMs: 7 }
}

function greenTrio() {
  return REQUIRED_TRIO.map((name) => rollupEntry(name))
}

function reviewPass() {
  return rollupEntry(REVIEW_CHECK, 'completed', 'success')
}

// Full reconcile snapshot with independent PR numbers (7xx series) and body.
function acceptanceSnapshot(prNumber, overrides = {}) {
  return {
    repository: OPS,
    number: prNumber,
    headSha: HEAD_A,
    baseRef: 'main',
    state: 'open',
    merged: false,
    draft: false,
    autoMerge: false,
    body: '## Summary\n\nAcceptance fixture body.\n',
    labels: [],
    mergeableState: 'clean',
    reviewDecision: null,
    headPushedMs: 3,
    nowMs: FIXED_NOW,
    checks: [...greenTrio(), reviewPass()],
    ...overrides,
  }
}

function decideFor(prNumber, overrides = {}, requiredChecks = REQUIRED_TRIO) {
  const snap = acceptanceSnapshot(prNumber, overrides)
  return decideReadinessActions(
    { snapshot: snap, body: snap.body, requiredChecks, nowMs: FIXED_NOW },
    readinessPolicy({ enabledRepositories: [OPS] }),
  )
}

// Minimal GitHub double with EXACTLY the six reconciler dependencies.
function acceptanceDoubles(snapshots, required = REQUIRED_TRIO) {
  const writes = []
  const github = {
    async getCloseoutSnapshot(repository, number) {
      const found = snapshots.get(`${repository}#${number}`)
      if (!found) throw new Error('GitHub query failed: transport, timeout, output limit or invalid JSON')
      return found
    },
    async getRulesetRequiredChecks() { return required },
    async updatePullRequestBody(repository, number) { writes.push(['update-body', repository, number]) },
    async markReadyForReview(repository, number) { writes.push(['mark-ready', repository, number]) },
    async convertToDraft(repository, number) { writes.push(['convert-to-draft', repository, number]) },
    async enableAutoMerge(repository, number, headSha) { writes.push(['enable-automerge', repository, number, headSha]) },
  }
  return { github, writes }
}

// --- S: scope pins --------------------------------------------------------
// S1 is the flag-on pin: RED on main (enabledRepositories []), GREEN on PR.

test('S1: the shipped policy enables exactly TogetherWeOwn/paperclip-ops', () => {
  const shipped = JSON.parse(readFileSync(shippedPolicyPath, 'utf8'))
  assert.deepEqual(
    [...shipped.enabledRepositories].sort(),
    [OPS],
    `shipped policy must enable exactly ${OPS}; got ${JSON.stringify(shipped.enabledRepositories)}`,
  )
})

test('S2: the example policy stays flag-off (no copy-paste enablement vector)', () => {
  const example = JSON.parse(readFileSync(examplePolicyPath, 'utf8'))
  assert.deepEqual(
    example.enabledRepositories,
    [],
    'example policy must keep enabledRepositories empty',
  )
})

test('S3: a widened-policy mutant fails the scope pin loudly', () => {
  // The pin under test is the S1 assertion shape: exact-set equality. A
  // second enabled repo must not slip through.
  const widened = [OPS, FOREIGN]
  assert.throws(
    () => assert.deepEqual([...widened].sort(), [OPS]),
    'widened policy must fail the exact-scope pin',
  )
  // And at the reconciler level the widened set would ACT on the foreign
  // repo, which is exactly what the pin exists to prevent.
  const foreignSnap = acceptanceSnapshot(701, { repository: FOREIGN, draft: true, checks: greenTrio() })
  const snaps = new Map([[`${FOREIGN}#701`, foreignSnap]])
  const { github } = acceptanceDoubles(snaps)
  const exactOps = createReconciler({ github, policy: { enabledRepositories: [OPS] }, nowMs: () => FIXED_NOW })
  const widenedPolicy = createReconciler({ github, policy: { enabledRepositories: [OPS, FOREIGN] }, nowMs: () => FIXED_NOW })
  return (async () => {
    const exact = await exactOps.runReconcile({ references: [{ repository: FOREIGN, number: 701 }], nowMs: FIXED_NOW })
    assert.equal(exact.records[0].decision, 'would-act', 'exact-ops policy never acts on a foreign repo')
    const wide = await widenedPolicy.runReconcile({ references: [{ repository: FOREIGN, number: 701 }], nowMs: FIXED_NOW })
    assert.equal(wide.records[0].decision, 'acted', 'widened mutant demonstrably acts on the foreign repo')
  })()
})

test('S4: with ops enabled, an ops PR acts while a foreign PR would-act only', async () => {
  const opsSnap = acceptanceSnapshot(702, { draft: true, checks: greenTrio() })
  const foreignSnap = acceptanceSnapshot(703, { repository: FOREIGN, draft: true, checks: greenTrio() })
  const snaps = new Map([[`${OPS}#702`, opsSnap], [`${FOREIGN}#703`, foreignSnap]])
  const { github } = acceptanceDoubles(snaps)
  const reconciler = createReconciler({ github, policy: { enabledRepositories: [OPS] }, nowMs: () => FIXED_NOW })
  const result = await reconciler.runReconcile({
    references: [
      { repository: OPS, number: 702 },
      { repository: FOREIGN, number: 703 },
    ],
    nowMs: FIXED_NOW,
  })
  const byKey = new Map(result.records.map((r) => [`${r.repository}#${r.number}`, r]))
  assert.equal(byKey.get(`${OPS}#702`).decision, 'acted')
  assert.ok(byKey.get(`${OPS}#702`).actions.includes('mark-ready'))
  assert.equal(byKey.get(`${FOREIGN}#703`).decision, 'would-act')
})

// --- A: arm requires exact-head verification --------------------------------

test('A1: the arm refuses when a required heavy check is missing from the rollup', () => {
  // ci-ok absent entirely: post-draft verification never ran on this head.
  const checks = [rollupEntry('pr-lint'), rollupEntry('public-safety'), reviewPass()]
  assert.equal(
    shouldEnableAutoMerge({ draft: false, headSha: HEAD_A, autoMergeEnabled: false, checks, requiredChecks: REQUIRED_TRIO }),
    false,
  )
  const decision = decideFor(711, { checks })
  assert.ok(!decision.actions.includes('enable-automerge'), `no arm on missing evidence, got ${decision.actions}`)
})

test('A2: the arm refuses while a required check is still pending', () => {
  const checks = [...greenTrio().slice(0, 2), rollupEntry('public-safety', 'in_progress', null), reviewPass()]
  assert.equal(
    shouldEnableAutoMerge({ draft: false, headSha: HEAD_A, autoMergeEnabled: false, checks, requiredChecks: REQUIRED_TRIO }),
    false,
  )
  const decision = decideFor(712, { checks })
  assert.ok(!decision.actions.includes('enable-automerge'))
})

test('A3 (control): exact-head verification with full evidence arms the gate', () => {
  const checks = [...greenTrio(), reviewPass()]
  assert.equal(
    shouldEnableAutoMerge({ draft: false, headSha: HEAD_A, autoMergeEnabled: false, checks, requiredChecks: REQUIRED_TRIO }),
    true,
    'the gate is live, not bricked: full exact-head evidence must arm',
  )
  const decision = decideFor(713, { checks })
  assert.ok(decision.actions.includes('enable-automerge'), `expected arm, got ${decision.actions} (${decision.reason})`)
})

test('A4: a sweep-readied PR with no post-draft verification sits stable (no arm, no flap, no churn)', () => {
  // Pass 1: green draft marks ready.
  const draftSnap = acceptanceSnapshot(714, { draft: true, checks: greenTrio() })
  const first = decideReadinessActions(
    { snapshot: draftSnap, body: draftSnap.body, requiredChecks: REQUIRED_TRIO, nowMs: FIXED_NOW },
    readinessPolicy({ enabledRepositories: [OPS] }),
  )
  assert.ok(first.actions.includes('mark-ready'), `pass 1 marks ready, got ${first.actions}`)

  // Pass 2: same head, now ready, but the ready transition re-queued checks
  // (pr-lint pending again). Fresh read, no carried state.
  const readyPending = acceptanceSnapshot(714, {
    draft: false,
    body: first.nextBody,
    checks: [rollupEntry('ci-ok'), rollupEntry('pr-lint', 'in_progress', null), rollupEntry('public-safety'), reviewPass()],
  })
  const second = decideReadinessActions(
    { snapshot: readyPending, body: readyPending.body, requiredChecks: REQUIRED_TRIO, nowMs: FIXED_NOW + 1000 },
    readinessPolicy({ enabledRepositories: [OPS] }),
  )
  assert.ok(!second.actions.includes('enable-automerge'), 'no arm without post-draft verification')
  assert.ok(!second.actions.includes('convert-to-draft'), 'pending evidence never flips back to draft (no flap)')
  assert.ok(!second.actions.includes('mark-ready'), 'already-ready PR takes no second transition (no churn)')
})

test('A5: skipped checks satisfy like the merge gate — safe only via the unconditional review bind', () => {
  const skippedTrio = [rollupEntry('ci-ok'), rollupEntry('pr-lint', 'completed', 'skipped'), rollupEntry('public-safety')]
  // With review evidence the skipped check satisfies and the gate arms.
  assert.equal(
    shouldEnableAutoMerge({
      draft: false, headSha: HEAD_A, autoMergeEnabled: false,
      checks: [...skippedTrio, reviewPass()], requiredChecks: REQUIRED_TRIO,
    }),
    true,
  )
  // Without review evidence the same skipped rollup must refuse: the skip is
  // safe ONLY because the review bind is unconditional.
  assert.equal(
    shouldEnableAutoMerge({
      draft: false, headSha: HEAD_A, autoMergeEnabled: false,
      checks: skippedTrio, requiredChecks: REQUIRED_TRIO,
    }),
    false,
    'skipped-required plus missing review must refuse',
  )
})

// --- B: review verdict binds unconditionally --------------------------------

test('B6: missing, pending, or failing review never arms', () => {
  const variants = {
    missing: greenTrio(),
    pending: [...greenTrio(), rollupEntry(REVIEW_CHECK, 'in_progress', null)],
    failing: [...greenTrio(), rollupEntry(REVIEW_CHECK, 'completed', 'failure')],
  }
  for (const [label, checks] of Object.entries(variants)) {
    assert.equal(
      shouldEnableAutoMerge({ draft: false, headSha: HEAD_A, autoMergeEnabled: false, checks, requiredChecks: REQUIRED_TRIO }),
      false,
      `${label} review must refuse the arm`,
    )
  }
  // Reconcile level: a fully-green CI rollup with no review entry takes no
  // arm action (block maintenance only).
  const decision = decideFor(721, { checks: greenTrio() })
  assert.ok(!decision.actions.includes('enable-automerge'))
})

test('B7: the review bind holds even when review is not a required check', () => {
  const ciOnly = ['ci-ok', 'pr-lint']
  const checks = [rollupEntry('ci-ok'), rollupEntry('pr-lint'), rollupEntry('public-safety')]
  // All CI green, review absent, review not required: still refuses.
  assert.equal(
    shouldEnableAutoMerge({ draft: false, headSha: HEAD_A, autoMergeEnabled: false, checks, requiredChecks: ciOnly }),
    false,
    'unconditional bind: missing review refuses even outside requiredChecks',
  )
  // Same rollup plus a passing review entry arms.
  assert.equal(
    shouldEnableAutoMerge({
      draft: false, headSha: HEAD_A, autoMergeEnabled: false,
      checks: [...checks, reviewPass()], requiredChecks: ciOnly,
    }),
    true,
  )
})

test('B8: a stale verdict converts to draft and never arms', () => {
  // Block names HEAD_A, snapshot is HEAD_B: a new head was pushed after the
  // review passed. Every check (including review) reads pass on the new
  // head's rollup, but the verdict is stale by construction.
  const staleBody = buildReadinessBlock({
    repository: OPS, number: 722, headSha: HEAD_A,
    checks: greenTrio(), reviewScore: '5/5', mergeable: 'clean', updatedMs: FIXED_NOW,
  })
  assert.equal(parseReadinessBlock(staleBody).headSha, HEAD_A)
  const snap = acceptanceSnapshot(722, {
    headSha: HEAD_B,
    body: `## Summary\n\nStale fixture.\n\n${staleBody}\n`,
    checks: [...greenTrio(), reviewPass()],
  })
  assert.equal(
    shouldConvertToDraft({
      draft: false, headSha: HEAD_B, blockHeadSha: HEAD_A,
      checks: snap.checks, requiredChecks: REQUIRED_TRIO,
    }),
    true,
    'new head after a passed review converts to draft',
  )
  const decision = decideReadinessActions(
    { snapshot: snap, body: snap.body, requiredChecks: REQUIRED_TRIO, nowMs: FIXED_NOW + 2000 },
    readinessPolicy({ enabledRepositories: [OPS] }),
  )
  assert.ok(decision.actions.includes('convert-to-draft'), `stale verdict converts, got ${decision.actions}`)
  assert.ok(!decision.actions.includes('enable-automerge'), 'stale verdict never arms')
})

// --- C: lazy convergence on fresh reads -------------------------------------

test('B9: sequential passes converge on fresh reads alone (a missing wake delays, never corrupts)', async () => {
  // Three passes, each a FRESH read: no state is carried except what GitHub
  // returns. A missing wake between pass 2 and pass 3 only delays the arm.
  const bodies = new Map()
  const reads = [
    acceptanceSnapshot(731, { draft: true, checks: greenTrio() }),
    acceptanceSnapshot(731, { draft: false, checks: [...greenTrio().slice(0, 2), rollupEntry('public-safety', 'in_progress', null)] }),
    acceptanceSnapshot(731, { draft: false, checks: [...greenTrio(), reviewPass()] }),
  ]
  let readIndex = 0
  const github = {
    async getCloseoutSnapshot() {
      const snap = reads[Math.min(readIndex, reads.length - 1)]
      const body = bodies.get(731) ?? snap.body
      return { ...snap, body }
    },
    async getRulesetRequiredChecks() { return REQUIRED_TRIO },
    async updatePullRequestBody(repository, number, body) { bodies.set(number, body) },
    async markReadyForReview() {},
    async convertToDraft() { throw new Error('must not flip to draft on this sequence') },
    async enableAutoMerge() {},
  }
  const reconciler = createReconciler({ github, policy: { enabledRepositories: [OPS] }, nowMs: () => FIXED_NOW })
  const ref = { repository: OPS, number: 731 }

  readIndex = 0
  const pass1 = await reconciler.runReconcile({ references: [ref], nowMs: FIXED_NOW })
  assert.ok(pass1.records[0].actions.includes('mark-ready'), `pass 1 marks ready, got ${pass1.records[0].actions}`)

  readIndex = 1 // fresh read: transition re-queued a check; review not yet run
  const pass2 = await reconciler.runReconcile({ references: [ref], nowMs: FIXED_NOW + 60_000 })
  assert.ok(!pass2.records[0].actions?.includes('enable-automerge'), 'pass 2 holds without evidence')
  assert.ok(!pass2.records[0].actions?.includes('convert-to-draft'), 'pass 2 never flaps on pending')

  // A missing wake here would simply skip pass 3 until a later event: the
  // delay is latency, not corruption. When the read finally happens:
  readIndex = 2
  const pass3 = await reconciler.runReconcile({ references: [ref], nowMs: FIXED_NOW + 120_000 })
  assert.ok(pass3.records[0].actions.includes('enable-automerge'), `pass 3 arms on fresh evidence, got ${pass3.records[0].actions}`)
})

test('B9b: re-reading an armed PR is a no-op (convergence terminates)', () => {
  const armed = acceptanceSnapshot(732, { autoMerge: true, checks: [...greenTrio(), reviewPass()] })
  // Seed the body with the current block so the pass has nothing to refresh:
  // build the exact block the duty would write, then re-decide on it.
  const first = decideReadinessActions(
    { snapshot: armed, body: armed.body, requiredChecks: REQUIRED_TRIO, nowMs: FIXED_NOW },
    readinessPolicy({ enabledRepositories: [OPS] }),
  )
  const settled = decideReadinessActions(
    { snapshot: { ...armed }, body: first.nextBody, requiredChecks: REQUIRED_TRIO, nowMs: FIXED_NOW + 5000 },
    readinessPolicy({ enabledRepositories: [OPS] }),
  )
  assert.deepEqual(settled.actions, [], `armed + fresh body converges to no-op, got ${settled.actions}`)
  assert.equal(
    shouldMarkReady({ draft: armed.draft, headSha: HEAD_A, blockHeadSha: HEAD_A, checks: armed.checks, requiredChecks: REQUIRED_TRIO }),
    false,
  )
})

// --- M: mutant kills (these prove the suite is not vacuous) ------------------

test('M1: a removed-review-bind mutant would arm where the real gate refuses', () => {
  // Naive mutant: arms on required-CI alone, ignoring the review entry.
  const naiveArmIgnoresReview = (checks, requiredChecks) => {
    const byName = new Map(checks.map((c) => [c.name, c]))
    return requiredChecks.every((name) => {
      const found = byName.get(name)
      return found?.status === 'completed' && ['success', 'neutral', 'skipped'].includes(found.conclusion)
    })
  }
  const noReview = greenTrio()
  assert.equal(naiveArmIgnoresReview(noReview, REQUIRED_TRIO), true, 'mutant arms without review (the bug)')
  assert.equal(
    shouldEnableAutoMerge({ draft: false, headSha: HEAD_A, autoMergeEnabled: false, checks: noReview, requiredChecks: REQUIRED_TRIO }),
    false,
    'real gate refuses without review: B6/B7/A5 kill the removed-bind mutant',
  )
})

test('M2: an unrecognized-review-pass mutant cannot arm through a score string', () => {
  // The gate binds through the review CHECK ENTRY, never a score string: a
  // 5/5 carried from an earlier head with a failing current-head review
  // entry must refuse.
  const checks = [...greenTrio(), rollupEntry(REVIEW_CHECK, 'completed', 'failure')]
  const snap = acceptanceSnapshot(741, { checks })
  // Display score would read 5/5 from a stale source; the gate must not care.
  assert.equal(
    shouldEnableAutoMerge({ draft: false, headSha: snap.headSha, autoMergeEnabled: false, checks, requiredChecks: REQUIRED_TRIO }),
    false,
    'failing current-head review entry refuses regardless of any score string',
  )
})

// --- D: disclosure pins ------------------------------------------------------
// D1–D3 assert the demonstrated flag-on posture: RED on main (UNPROVEN
// header), GREEN on the PR head.

test('D1: the sweep header states the demonstrated fail-closed posture (no UNPROVEN remains)', () => {
  const workflow = readFileSync(sweepWorkflowPath, 'utf8')
  assert.ok(!workflow.includes('UNPROVEN'), 'flag-on header must not carry UNPROVEN gaps')
  assert.ok(workflow.toLowerCase().includes('demonstrated'), 'header states the demonstrated posture')
  assert.ok(workflow.toLowerCase().includes('fail-closed') || workflow.toLowerCase().includes('fail closed'),
    'header states the fail-closed posture')
})

test('D2: the header discloses the minimal-expressible grant set', () => {
  // Header words it "the minimal EXPRESSIBLE grants" (PR body shortens to
  // minimal-expressible): pin the words case-insensitively, not the hyphen.
  const workflow = readFileSync(sweepWorkflowPath, 'utf8')
  const lower = workflow.toLowerCase()
  assert.ok(lower.includes('minimal') && lower.includes('expressible'),
    'header discloses minimal expressible grants')
  assert.ok(lower.includes('grant'), 'grant disclosure names grants')
})

test('D3: the header discloses lazy convergence', () => {
  // Header words it "converge lazily" (PR body shortens to lazy
  // convergence): "lazily" holds "lazi" but not "lazy" — pin the stem.
  const workflow = readFileSync(sweepWorkflowPath, 'utf8')
  const lower = workflow.toLowerCase()
  assert.ok(lower.includes('lazi'), 'header names lazy/lazily convergence')
  assert.ok(lower.includes('converg'), 'header discloses convergence behavior')
})
