// Flag-on binding proofs (ops-only enablement). No network, no credentials:
// the GitHub surface is a double implementing exactly the six methods the
// duty may call.
//
// What this pins — the pre-enablement gaps from the sweep workflow's EVENT
// CONTRACT, demonstrated fail-closed before enabling a repository:
//   A. Heavy-CI handoff: draft ci-ok can succeed with heavy jobs skipped, and
//      an independent comment review does not prove non-draft CI ran. Only
//      exact-head ready-ci completed/success plus required checks and review
//      can arm. Token-ready PRs wait without flapping for a non-draft CI run.
//   B. Arm wake: no dedicated wake exists for external check completion, so
//      the arm binds the review verdict to THIS head unconditionally (even
//      when review is not required) and stale verdicts convert to draft
//      instead of arming; passes converge on fresh reads alone, so a missing
//      wake delays but never corrupts.
//   C. Scope: the shipped policy enables at most paperclip-ops; the example
//      stays flag-off.
//   D. The real CI workflow, not a fictional required context, produces the
//      ready-ci marker after non-draft ci-ok success without a dependency cycle.

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
// Live required set (verified 2026-10-21). ready-ci is deliberately NOT a
// required context: the arm must enforce its evidence gate unconditionally.
const REQUIRED = ['ci-ok', 'pr-lint', 'public-safety', 'Paperclip Review']
const SUITES = ['ci-ok', 'pr-lint', 'public-safety']

const pass = (name) => ({ name, status: 'completed', conclusion: 'success', completedMs: 1 })
const pending = (name) => ({ name, status: 'in_progress' })
const REVIEW_PASS = () => pass('Paperclip Review')
const READY_CI_PASS = () => pass('ready-ci')
const READY_CI_SKIPPED = () => ({ name: 'ready-ci', status: 'completed', conclusion: 'skipped', completedMs: 1 })

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
    async getCloseoutSnapshot(repository, number) {
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

// --- A. heavy-CI handoff: explicit non-draft evidence, no ruleset change ------

test('A1: all four required contexts and independent review pass, but missing ready-ci refuses the arm', () => {
  const decision = decideReadinessActions(
    input(1, { checks: [...SUITES.map(pass), REVIEW_PASS()] }), on)
  assert.deepEqual(decision.actions, ['update-body'], 'no arm or draft flap without non-draft CI evidence')
})

test('A2: ready-ci success cannot bypass a pending required check', () => {
  const decision = decideReadinessActions(
    input(2, { checks: [pass('ci-ok'), pass('pr-lint'), pending('public-safety'), REVIEW_PASS(), READY_CI_PASS()] }), on)
  assert.deepEqual(decision.actions, ['update-body'])
})

test('A3: control — all four required contexts plus exact-head ready-ci success arm', () => {
  const decision = decideReadinessActions(
    input(3, { checks: [...SUITES.map(pass), REVIEW_PASS(), READY_CI_PASS()] }), on)
  assert.ok(decision.actions.includes('enable-automerge'))
})

test('A4: sweep-ready plus independent same-head review never arms on skipped ready-ci; later non-draft success arms', async () => {
  // Green draft: ci-ok succeeded after draft selection skipped heavy areas.
  // The existing ready gate is unchanged: skipped ready-ci must not block it.
  const draftChecks = [...SUITES.map(pass), READY_CI_SKIPPED()]
  const snapshots = new Map([[`${repo}#4`, snapshot(4, { draft: true, checks: draftChecks })]])
  const { github, calls } = doubles(snapshots)
  const reconciler = createReconciler({ github, policy: on })
  const run = (nowMs) => reconciler.runReconcile({ references: [{ repository: repo, number: 4 }], nowMs })
  const first = await run(NOW)
  assert.equal(first.records[0].decision, 'acted')
  assert.deepEqual(first.records[0].actions, ['update-body', 'mark-ready'])
  const written = calls.find((call) => call[0] === 'update-body')[3]
  snapshots.set(`${repo}#4`, snapshot(4, { body: written, checks: draftChecks }))
  const second = await run(NOW + 3600000)
  assert.equal(second.records[0].decision, 'none')
  assert.equal(second.records[0].reason, 'up-to-date')
  assert.ok(!('actions' in second.records[0]), 'unchanged post-ready evidence is a no-op')

  // A comment-triggered review now passes independently on the SAME head.
  // It does not start non-draft CI: ci-ok is still the draft-run success,
  // ready-ci is still skipped. Repeated fresh reads must never arm or flap.
  const reviewedChecks = [...draftChecks, REVIEW_PASS()]
  snapshots.set(`${repo}#4`, snapshot(4, { body: written, checks: reviewedChecks }))
  const reviewed = await run(NOW + 7200000)
  assert.deepEqual(reviewed.records[0].actions, ['update-body'], 'review alone is not non-draft CI evidence')
  const reviewedBody = calls.filter((call) => call[0] === 'update-body').at(-1)[3]
  snapshots.set(`${repo}#4`, snapshot(4, { body: reviewedBody, checks: reviewedChecks }))
  const stable = await run(NOW + 10800000)
  assert.equal(stable.records[0].decision, 'none')
  assert.equal(stable.records[0].reason, 'up-to-date')
  assert.ok(!('actions' in stable.records[0]), 'no body churn on unchanged draft-only CI evidence')
  assert.deepEqual(writes(calls), ['update-body', 'mark-ready', 'update-body'], 'no arm, flap, or repeated ready transition')

  // A non-draft CI event/rerun finally produces the real success marker.
  // No head change, ruleset change, or second ready transition is necessary.
  snapshots.set(`${repo}#4`, snapshot(4, { body: reviewedBody,
    checks: [...SUITES.map(pass), REVIEW_PASS(), READY_CI_PASS()] }))
  const verified = await run(NOW + 14400000)
  assert.deepEqual(verified.records[0].actions, ['update-body', 'enable-automerge'])
  const arms = calls.filter((call) => call[0] === 'enable-automerge')
  assert.deepEqual(arms, [['enable-automerge', repo, 4, HEAD]], 'one arm bound to the evaluated head')
  assert.ok(!writes(calls).includes('convert-to-draft'))
})

test('A5: path-gated required checks may skip, but only ready-ci success proves non-draft CI', () => {
  // public-safety is a real required context. Its ordinary skip semantics
  // stay unchanged; ready-ci must NOT inherit those permissive semantics.
  const checks = [pass('ci-ok'), pass('pr-lint'),
    { name: 'public-safety', status: 'completed', conclusion: 'skipped', completedMs: 1 }, REVIEW_PASS()]
  const legitimate = decideReadinessActions(input(5, { checks: [...checks, READY_CI_PASS()] }), on)
  assert.ok(legitimate.actions.includes('enable-automerge'))
  const draftOnly = decideReadinessActions(input(5, { checks: [...checks, READY_CI_SKIPPED()] }), on)
  assert.deepEqual(draftOnly.actions, ['update-body'], 'skipped ready-ci never authorizes an arm or a draft flap')
})

for (const [shape, marker] of [
  ['neutral', { name: 'ready-ci', status: 'completed', conclusion: 'neutral', completedMs: 1 }],
  ['pending', pending('ready-ci')],
  ['failed', { name: 'ready-ci', status: 'completed', conclusion: 'failure', completedMs: 1 }],
]) {
  test(`A6: ${shape} optional ready-ci neither arms nor flaps a ready PR`, () => {
    const decision = decideReadinessActions(input(6, {
      checks: [...SUITES.map(pass), REVIEW_PASS(), marker],
    }), on)
    assert.deepEqual(decision.actions, ['update-body'])
  })
}

// --- B. arm wake: the review verdict binds THIS head -------------------------

test('B6: missing, pending, or failing review never arms even with ready-ci success', () => {
  const variants = {
    missing: [...SUITES.map(pass), READY_CI_PASS()],
    pending: [...SUITES.map(pass), READY_CI_PASS(), pending('Paperclip Review')],
    failing: [...SUITES.map(pass), READY_CI_PASS(), { name: 'Paperclip Review', status: 'completed', conclusion: 'failure', completedMs: 1 }],
  }
  for (const [shape, checks] of Object.entries(variants)) {
    const decision = decideReadinessActions(input(6, { checks }), on)
    assert.ok(!decision.actions.includes('enable-automerge'), `${shape} review must refuse the arm`)
    assert.ok(!decision.actions.includes('convert-to-draft'), `${shape} review must not flap to draft`)
  }
})

test('B7: the review bind is unconditional — the arm refuses even when review is not required', () => {
  const withoutReview = [...SUITES]
  const refused = decideReadinessActions(
    input(7, { checks: [...SUITES.map(pass), READY_CI_PASS()] }, withoutReview), on)
  assert.ok(!refused.actions.includes('enable-automerge'))
  const armed = decideReadinessActions(
    input(7, { checks: [...SUITES.map(pass), REVIEW_PASS(), READY_CI_PASS()] }, withoutReview), on)
  assert.ok(armed.actions.includes('enable-automerge'))
})

test('B8: a stale verdict never arms — even with ready-ci and review passed in the rollup', () => {
  const planted = decideReadinessActions(input(8), on)
  const stale = snapshot(8, { headSha: HEAD2,
    body: `intro\n\n${planted.block}\n`,
    checks: [...SUITES.map(pass), REVIEW_PASS(), READY_CI_PASS()] })
  const decision = decideReadinessActions(
    { snapshot: stale, body: stale.body, requiredChecks: REQUIRED, nowMs: NOW }, on)
  assert.ok(decision.actions.includes('convert-to-draft'), 'new head converts to draft')
  assert.ok(!decision.actions.includes('enable-automerge'), 'stale verdict must not arm')
})

test('B9: passes converge on fresh reads alone — a missing wake delays the arm, never corrupts it', async () => {
  const snapshots = new Map([[`${repo}#9`, snapshot(9, { checks: [...SUITES.map(pass), READY_CI_PASS()] })]])
  const { github, calls } = doubles(snapshots)
  const reconciler = createReconciler({ github, policy: on })
  await reconciler.runReconcile({ references: [{ repository: repo, number: 9 }], nowMs: NOW })
  assert.ok(writes(calls).every((op) => op !== 'enable-automerge'), 'pass 1 must not arm without review')
  const written = calls.find((call) => call[0] === 'update-body')[3]
  snapshots.set(`${repo}#9`, snapshot(9, { body: written,
    checks: [...SUITES.map(pass), REVIEW_PASS(), READY_CI_PASS()] }))
  await reconciler.runReconcile({ references: [{ repository: repo, number: 9 }], nowMs: NOW + 3600000 })
  const arm = calls.find((call) => call[0] === 'enable-automerge')
  assert.ok(arm, 'pass 2 arms on fresh exact-head evidence')
  assert.equal(arm[3], HEAD, 'the arm binds the evaluated head')
})

test('B10: hold still blocks every transition with ready-ci and review success', () => {
  const decision = decideReadinessActions(input(10, { labels: ['hold'],
    checks: [...SUITES.map(pass), REVIEW_PASS(), READY_CI_PASS()] }), on)
  assert.deepEqual(decision.actions, ['update-body'])
  assert.equal(decision.reason, 'held-block-only')
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

// --- D. real CI producer contract --------------------------------------------
// Read job blocks from the actual workflow, excluding comment-only lines.
// Like the security selectors in readiness-job-token.test.mjs, pins are exact
// and intentionally fail on an unrecognized YAML layout rather than silently
// passing a substring that could belong to a different job or a comment.
const ciLines = readFileSync(new URL('../../.github/workflows/ci.yml', import.meta.url), 'utf8')
  .split('\n').filter((line) => !/^\s*#/.test(line))

function ciJob(id) {
  const start = ciLines.indexOf(`  ${id}:`)
  assert.ok(start > ciLines.indexOf('jobs:'), `CI defines the real ${id} job`)
  const end = ciLines.findIndex((line, index) => index > start && /^  [\w-]+:/.test(line))
  return ciLines.slice(start + 1, end === -1 ? undefined : end)
}

function jobField(lines, key) {
  const prefix = `    ${key}:`
  const matches = lines.filter((line) => line.startsWith(prefix))
  assert.equal(matches.length, 1, `exactly one job field ${key}`)
  return matches[0].slice(prefix.length).trim()
}

test('D12: real ready-ci producer depends on ci-ok success and excludes draft runs', () => {
  const job = ciJob('ready-ci')
  // Without an explicit name GitHub uses the id. A renamed display context
  // would strand the consumer, so an override must still be exactly ready-ci.
  if (job.some((line) => line.startsWith('    name:'))) assert.equal(jobField(job, 'name'), 'ready-ci')
  assert.equal(jobField(job, 'needs'), '[ci-ok]')
  assert.equal(jobField(job, 'if'), "${{ !cancelled() && needs.ci-ok.result == 'success' && github.event.pull_request.draft != true }}")
  const aggregate = ciJob('ci-ok')
  const needsAt = aggregate.findIndex((line) => line.startsWith('    needs:'))
  assert.ok(needsAt !== -1, 'ci-ok retains its aggregate dependencies')
  const dependencies = [aggregate[needsAt]]
  for (const line of aggregate.slice(needsAt + 1)) {
    if (/^    \S/.test(line)) break
    dependencies.push(line)
  }
  assert.ok(!dependencies.join('\n').includes('ready-ci'), 'ci-ok must not depend on ready-ci (no cycle)')
})

test('D13: ready-ci is bounded and hosted, with one fixed success step and no checkout or action', () => {
  const job = ciJob('ready-ci')
  assert.equal(jobField(job, 'runs-on'), 'ubuntu-latest')
  const timeout = Number(jobField(job, 'timeout-minutes'))
  assert.ok(Number.isInteger(timeout) && timeout > 0, 'fixed marker job has an explicit positive deadline')
  assert.ok(!job.some((line) => /\buses:/.test(line)), 'marker executes no action or checkout')
  const stepsAt = job.indexOf('    steps:')
  assert.ok(stepsAt !== -1, 'marker has steps')
  const stepLines = job.slice(stepsAt + 1).filter((line) => line.trim())
  assert.equal(stepLines.filter((line) => /^      - /.test(line)).length, 1, 'one fixed marker step')
  const runAt = stepLines.findIndex((line) => /^      (?:- |  )run:/.test(line))
  assert.ok(runAt !== -1, 'marker step runs a fixed success command')
  assert.equal(stepLines.filter((line) => /^      (?:- |  )run:/.test(line)).length, 1)
  const inline = stepLines[runAt].replace(/^      (?:- |  )run:\s*/, '')
  const command = inline === '|' ? stepLines.slice(runAt + 1).map((line) => line.trim()).join('\n') : inline
  // The success marker needs no PR data, external process or expansion.
  // A no-op or fixed literal echo succeeds without running repository code.
  assert.match(command, /^(?:true|echo (?:"[\w .:-]+"|'[\w .:-]+'))$/, 'fixed success command only')
})
