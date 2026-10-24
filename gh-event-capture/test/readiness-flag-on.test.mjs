// Flag-on binding proofs (ops-only enablement). No network, no credentials:
// the GitHub surface is a double implementing exactly the six methods the
// duty may call.
//
// What this pins — the two pre-enablement gaps from the sweep workflow's
// EVENT CONTRACT, demonstrated fail-closed here before any repo is enabled
// (the workflow header names this file as its proof):
//   A. Heavy-CI handoff: a sweep-made ready transition does not start heavy
//      CI selection (token writes re-fire nothing), and draft selection
//      passes ci-ok with every area skipped — so the arm requires exact-head
//      verification (every required check plus the review check passing on
//      THIS head) PLUS the suites job's own completed success on THIS head,
//      and refuses on missing, pending, or skipped-heavy evidence. A later
//      review pass is not evidence that heavy CI ran.
//   B. Arm wake: no dedicated wake exists for external check completion, so
//      the arm binds the review verdict to THIS head unconditionally (even
//      when review is not required) and stale verdicts convert to draft
//      instead of arming; passes converge on fresh reads alone, so a missing
//      wake delays but never corrupts.
//   C. Scope: the shipped policy enables at most paperclip-ops; the example
//      stays flag-off.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { decideReadinessActions, readinessPolicy, createReconciler } from '../src/readiness-reconcile.js'

const repo = 'TogetherWeOwn/paperclip-ops'
const head = (c) => c.repeat(40)
const HEAD = head('a')
const HEAD2 = head('b')
const NOW = 1728285600000
// Live ruleset shape on main (verified 2026-10-21): three suite checks plus
// the review check. `suites` is the live heavy job name (ci.yml): tests that
// arm add its completed success explicitly — it is evidence, not required.
const REQUIRED = ['ci-ok', 'pr-lint', 'public-safety', 'Paperclip Review']
const SUITES = ['ci-ok', 'pr-lint', 'public-safety']

const pass = (name) => ({ name, status: 'completed', conclusion: 'success', completedMs: 1 })
const pending = (name) => ({ name, status: 'in_progress' })
const REVIEW_PASS = () => pass('Paperclip Review')

function snapshot(number, over = {}) {
  return { repository: repo, number, headSha: HEAD, baseRef: 'main',
    state: 'open', merged: false, draft: false, autoMerge: false,
    body: '## What Changed\n\n- a fix\n', labels: [],
    mergeableState: 'clean', reviewDecision: null,
    headPushedMs: 1, nowMs: NOW,
    checks: SUITES.map(pass), ...over }
}

function input(number, over = {}, requiredChecks = REQUIRED) {
  const snap = snapshot(number, over)
  return { snapshot: snap, body: snap.body, requiredChecks, nowMs: NOW }
}

const on = readinessPolicy({ enabledRepositories: [repo] })

// Injected GitHub double: EXACTLY the six duty dependencies. The snapshot
// map stays live across passes so sequential-pass tests re-read changed
// state, exactly as production re-reads GitHub on every pass.
function doubles(snapshots, { rulesets = null } = {}) {
  const calls = []
  const github = {
    async getReadinessSnapshot(repository, number) {
      calls.push(['read-snapshot', repository, number])
      const found = snapshots.get(`${repository}#${number}`)
      if (!found) throw new Error('GitHub query failed: transport, timeout, output limit or invalid JSON')
      return found
    },
    async getRulesetRequiredChecks(repository, branch) {
      calls.push(['read-rulesets', repository, branch])
      if (rulesets === null) return REQUIRED
      const found = rulesets.get(repository)
      if (!found) throw new Error('GitHub ruleset list failed: transport, timeout, output limit or invalid JSON')
      return found
    },
    async updatePullRequestBody(repository, number, body) {
      calls.push(['update-body', repository, number, body])
    },
    async markReadyForReview(repository, number) {
      calls.push(['mark-ready', repository, number])
    },
    async convertToDraft(repository, number) {
      calls.push(['convert-to-draft', repository, number])
    },
    async enableAutoMerge(repository, number, headSha) {
      calls.push(['enable-automerge', repository, number, headSha])
    },
  }
  return { github, calls }
}

const ops = (call) => call[0]
const writes = (calls) => calls.map(ops).filter((op) => op !== 'read-snapshot' && op !== 'read-rulesets')

// --- A. heavy-CI handoff: no verification, no arm ----------------------------

test('A1: the arm refuses a ready PR with no heavy evidence (suites missing from the rollup)', () => {
  // The live post-sweep-ready shape with the review variable isolated OUT:
  // review passed on this head, every required check green, but the suites
  // job never reported on this head (heavy selection never ran). Missing
  // evidence is neither failure (no draft flap) nor license (no arm).
  const decision = decideReadinessActions(
    input(1, { checks: [...SUITES.map(pass), REVIEW_PASS()] }), on)
  assert.deepEqual(decision.actions, ['update-body'])
})

test('A2: the arm refuses while a required check is pending', () => {
  // Heavy success is present, so this pins the pending-check refusal rather
  // than passing through the heavy bind (cf. A1, which isolates the reverse).
  const decision = decideReadinessActions(
    input(2, { checks: [pass('ci-ok'), pass('pr-lint'), pending('public-safety'), pass('suites'), REVIEW_PASS()] }), on)
  assert.deepEqual(decision.actions, ['update-body'])
})

test('A3: control — exact-head verification plus heavy success arms (the gate is live, not bricked)', () => {
  const decision = decideReadinessActions(
    input(3, { checks: [...SUITES.map(pass), pass('suites'), REVIEW_PASS()] }), on)
  assert.ok(decision.actions.includes('enable-automerge'))
})

test('A4: post-draft sequence — a sweep-readied PR with no post-draft verification sits stable: no arm, no flap, no churn', async () => {
  // Pass 1: green draft. The ready gate excludes the review check, so the
  // fast verdict (all suites green, review not yet run) marks ready.
  const snapshots = new Map([[`${repo}#4`, snapshot(4, { draft: true })]])
  const { github, calls } = doubles(snapshots)
  const reconciler = createReconciler({ github, policy: on })
  const first = await reconciler.runReconcile({ references: [{ repository: repo, number: 4 }], nowMs: NOW })
  assert.equal(first.records[0].decision, 'acted')
  assert.deepEqual(first.records[0].actions, ['update-body', 'mark-ready'])
  // Pass 2 (an hour later, whatever woke it): the same head is now ready,
  // but the sweep-made transition fired no CI and no review — fresh reads
  // see exactly the standing fast verdict. Nothing to do: no arm, no flap,
  // and not even a block rewrite (the timestamp-blind compare holds).
  const written = calls.find((call) => call[0] === 'update-body')[3]
  snapshots.set(`${repo}#4`, snapshot(4, { draft: false, body: written }))
  const second = await reconciler.runReconcile({ references: [{ repository: repo, number: 4 }], nowMs: NOW + 3600000 })
  assert.equal(second.records[0].decision, 'none')
  assert.equal(second.records[0].reason, 'up-to-date')
  assert.ok(!('actions' in second.records[0]), 'a stable pass records no actions at all')
  assert.ok(writes(calls).every((op) => op !== 'enable-automerge'), 'no arm without post-draft verification')
  assert.ok(writes(calls).every((op) => op !== 'convert-to-draft'), 'no flap on unchanged evidence')
})

test('A5: skipped heavy evidence refuses the arm even with review passed — a skip is not a success', () => {
  const skipped = { name: 'suites', status: 'completed', conclusion: 'skipped', completedMs: 1 }
  // Review passed on this head, but the rollup carries no provenance to tell
  // a draft skip from a path-gated one — and a comment-triggered review can
  // pass without any human-driven full CI running. Skipped suites therefore
  // refuses the arm; only a completed success licenses it. (Required checks
  // themselves still satisfy on skipped, exactly like the merge gate.)
  const draftSkipped = decideReadinessActions(
    input(5, { checks: [...SUITES.map(pass), skipped, REVIEW_PASS()] }), on)
  assert.ok(!draftSkipped.actions.includes('enable-automerge'))
  // The post-sweep-ready shape: heavies skipped by draft selection, review
  // never ran. Both binds refuse the arm.
  const stuck = decideReadinessActions(input(5, { checks: [...SUITES.map(pass), skipped] }), on)
  assert.ok(!stuck.actions.includes('enable-automerge'))
})

// --- B. arm wake: the review verdict binds THIS head -------------------------

test('B6: missing, pending, or failing review never arms', () => {
  // Every variant carries heavy success, so these pin the review bind rather
  // than passing through the heavy bind.
  const variants = {
    missing: [...SUITES.map(pass), pass('suites')],
    pending: [...SUITES.map(pass), pass('suites'), pending('Paperclip Review')],
    failing: [...SUITES.map(pass), pass('suites'), { name: 'Paperclip Review', status: 'completed', conclusion: 'failure', completedMs: 1 }],
  }
  for (const [shape, checks] of Object.entries(variants)) {
    const decision = decideReadinessActions(input(6, { checks }), on)
    assert.ok(!decision.actions.includes('enable-automerge'), `${shape} review must refuse the arm`)
    assert.ok(!decision.actions.includes('convert-to-draft'), `${shape} review must not flap to draft`)
  }
})

test('B7: the review bind is unconditional — the arm refuses even when review is not required', () => {
  const withoutReview = [...SUITES]
  // Rulesets without the review check, everything green plus heavy success,
  // review never ran: still no arm. A ruleset edit can never silently
  // disarm this bind.
  const refused = decideReadinessActions(input(7, { checks: [...SUITES.map(pass), pass('suites')] }, withoutReview), on)
  assert.ok(!refused.actions.includes('enable-automerge'))
  // Control: the same shape with review passed and heavy success on this
  // head arms — the bind refuses, it does not brick.
  const armed = decideReadinessActions(
    input(7, { checks: [...SUITES.map(pass), pass('suites'), REVIEW_PASS()] }, withoutReview), on)
  assert.ok(armed.actions.includes('enable-automerge'))
})

test('B8: a stale verdict never arms — review passed on the old head, new head pushed', () => {
  // Plant the block for HEAD, then move the head: even a rollup that still
  // shows review-passed (a stale entry) cannot arm — the head mismatch
  // converts to draft first, and the else-if chain admits exactly one
  // transition per pass.
  const planted = decideReadinessActions(input(8), on)
  const stale = snapshot(8, { headSha: HEAD2,
    body: `intro\n\n${planted.block}\n`,
    checks: [...SUITES.map(pass), REVIEW_PASS()] })
  const decision = decideReadinessActions(
    { snapshot: stale, body: stale.body, requiredChecks: REQUIRED, nowMs: NOW }, on)
  assert.ok(decision.actions.includes('convert-to-draft'), 'new head converts to draft')
  assert.ok(!decision.actions.includes('enable-automerge'), 'stale verdict must not arm')
})

test('B9: passes converge on fresh reads alone — a missing wake delays the arm, never corrupts it', async () => {
  // Pass 1: ready, suites green, review not yet run. No arm — and the
  // decision takes no wake payload, only the snapshot, so there is nothing
  // for a missing wake to corrupt.
  const snapshots = new Map([[`${repo}#9`, snapshot(9)]])
  const { github, calls } = doubles(snapshots)
  const reconciler = createReconciler({ github, policy: on })
  await reconciler.runReconcile({ references: [{ repository: repo, number: 9 }], nowMs: NOW })
  assert.ok(writes(calls).every((op) => op !== 'enable-automerge'), 'pass 1 must not arm')
  // Pass 2: heavy suites and review have both completed on the SAME head
  // (a full CI run plus review on the ready head). Any later pass —
  // whatever woke it — re-reads and arms on the fresh evidence.
  const written = calls.find((call) => call[0] === 'update-body')[3]
  snapshots.set(`${repo}#9`, snapshot(9, { body: written,
    checks: [...SUITES.map(pass), pass('suites'), REVIEW_PASS()] }))
  await reconciler.runReconcile({ references: [{ repository: repo, number: 9 }], nowMs: NOW + 3600000 })
  const arm = calls.find((call) => call[0] === 'enable-automerge')
  assert.ok(arm, 'pass 2 arms on fresh exact-head evidence')
  assert.equal(arm[3], HEAD, 'the arm binds the evaluated head')
})

// --- C. scope: ops only -------------------------------------------------------

test('C10: the shipped policy enables at most paperclip-ops (scope expansion fails here)', () => {
  // The flag-on binding is ops-only: listing any other repo must fail loudly
  // in CI, not ship silently. The pin is a subset (not an equality), so a
  // flag-off rollback — the documented revocation — stays green.
  const dir = dirname(fileURLToPath(import.meta.url))
  const shipped = JSON.parse(readFileSync(join(dir, '..', 'readiness-policy.json'), 'utf8'))
  const validated = readinessPolicy(shipped)
  for (const entry of validated.enabledRepositories) {
    assert.equal(entry, 'TogetherWeOwn/paperclip-ops', `scope-broadening entry: ${entry}`)
  }
})

test('C11: the example policy stays flag-off (a copy-paste enablement vector otherwise)', () => {
  const dir = dirname(fileURLToPath(import.meta.url))
  const example = JSON.parse(readFileSync(join(dir, '..', 'readiness-policy.example.json'), 'utf8'))
  assert.deepEqual(readinessPolicy(example).enabledRepositories, [])
})
