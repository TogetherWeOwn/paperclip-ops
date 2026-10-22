// Readiness reconcile duty offline suite (T\x4fG-990010). No network, no
// credentials: the GitHub surface is a double implementing exactly the six
// methods the duty may call. Anything else (merge, close, comment) has nowhere
// to land, so an arm/merge mutant cannot hide.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { decideReadinessActions, readinessPolicy, createReconciler } from '../src/readiness-reconcile.js'

const repo = 'TogetherWeOwn/paperclip-ops'
const other = 'TogetherWeOwn/kofra'
const head = (c) => c.repeat(40)
const HEAD = head('a')
const HEAD2 = head('b')
const REQUIRED = ['ci-ok', 'gitleaks']
const NOW = 1728285600000

const off = readinessPolicy()
const on = readinessPolicy({ enabledRepositories: [repo] })

function greenChecks() {
  return [
    { name: 'ci-ok', status: 'completed', conclusion: 'success', completedMs: 1 },
    { name: 'gitleaks', status: 'completed', conclusion: 'success', completedMs: 1 },
  ]
}

function snapshot(number, over = {}) {
  return { repository: repo, number, headSha: HEAD, baseRef: 'main',
    state: 'open', merged: false, draft: true, autoMerge: false,
    body: '## What Changed\n\n- a fix\n', labels: [],
    mergeableState: 'clean', reviewDecision: null,
    headPushedMs: 1, nowMs: NOW, checks: greenChecks(), ...over }
}

function input(number, over = {}) {
  const snap = snapshot(number, over)
  return { snapshot: snap, body: snap.body, requiredChecks: REQUIRED, nowMs: NOW }
}

// Injected GitHub double: EXACTLY the six duty dependencies.
function doubles(snapshots, { rulesets = null, fail = [] } = {}) {
  const calls = []
  const github = {
    async getCloseoutSnapshot(repository, number, { nowMs } = {}) {
      calls.push(['read-snapshot', repository, number, nowMs])
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
      if (fail.includes('update-body')) throw new Error('GitHub body update failed: transport, timeout or output limit')
    },
    async markReadyForReview(repository, number) {
      calls.push(['mark-ready', repository, number])
      if (fail.includes('mark-ready')) throw new Error('GitHub mark-ready failed: transport, timeout, output limit or invalid JSON')
    },
    async convertToDraft(repository, number) {
      calls.push(['convert-to-draft', repository, number])
      if (fail.includes('convert-to-draft')) throw new Error('GitHub convert-to-draft failed: transport, timeout, output limit or invalid JSON')
    },
    async enableAutoMerge(repository, number, headSha) {
      calls.push(['enable-automerge', repository, number, headSha])
      if (fail.includes('enable-automerge')) throw new Error('GitHub auto-merge arm failed: transport, timeout or output limit')
    },
  }
  return { github, calls }
}

const ops = (call) => call[0]

// --- pure decide -----------------------------------------------------------

test('green draft with no block appends the block and marks ready', () => {
  const decision = decideReadinessActions(input(1), on)
  assert.deepEqual(decision.actions, ['update-body', 'mark-ready'])
  assert.ok(decision.nextBody.includes('<!-- readiness:start -->'))
  assert.ok(decision.nextBody.startsWith('## What Changed'))
  assert.equal(decision.blockHeadSha, null)
  assert.equal(decision.reason, 'block-missing+mark-ready')
})

test('byte-identical body at a fixed clock skips the write but keeps the transition', () => {
  const first = decideReadinessActions(input(1), on)
  assert.deepEqual(first.actions, ['update-body', 'mark-ready'])
  // Reconciled body, same clock: the block is byte-identical, so no
  // update-body — but the draft is still unmarked, so mark-ready remains.
  const reconciled = snapshot(1, { draft: true, body: first.nextBody })
  const second = decideReadinessActions({ snapshot: reconciled, body: first.nextBody,
    requiredChecks: REQUIRED, nowMs: NOW }, on)
  assert.deepEqual(second.actions, ['mark-ready'])
  assert.equal(second.reason, 'mark-ready')
  // And a ready, green PR reconciled at a fixed clock is a true no-op.
  const firstReady = decideReadinessActions(input(2, { draft: false }), on)
  assert.deepEqual(firstReady.actions, ['update-body'])
  const ready = snapshot(2, { draft: false, body: firstReady.nextBody })
  const settled = decideReadinessActions({ snapshot: ready, body: firstReady.nextBody,
    requiredChecks: REQUIRED, nowMs: NOW }, on)
  assert.deepEqual(settled.actions, [])
  assert.equal(settled.reason, 'up-to-date')
})

test('a later clock alone never rewrites the block: the Updated line refreshes only on real change', () => {
  const LATER = NOW + 60000
  const first = decideReadinessActions(input(1), on)
  assert.deepEqual(first.actions, ['update-body', 'mark-ready'])
  // Same checks, one minute later: the block content is identical apart from
  // `Updated:`, so no update-body — but the draft still needs marking ready.
  const reconciled = snapshot(1, { draft: true, body: first.nextBody })
  const second = decideReadinessActions({ snapshot: reconciled, body: first.nextBody,
    requiredChecks: REQUIRED, nowMs: LATER }, on)
  assert.deepEqual(second.actions, ['mark-ready'])
  // And a settled ready PR stays a true no-op across clocks.
  const reviewPass = { name: 'Paperclip Review', status: 'completed', conclusion: 'success', completedMs: 1 }
  const settledAt = (clock) => {
    const snap = snapshot(2, { draft: false, autoMerge: true,
      checks: [...greenChecks(), reviewPass], body: first.nextBody })
    // Reconcile once to plant the block for head 2, then re-decide on it.
    const planted = decideReadinessActions(
      { snapshot: snap, body: snap.body, requiredChecks: REQUIRED, nowMs: clock }, on)
    const replant = snapshot(2, { draft: false, autoMerge: true,
      checks: [...greenChecks(), reviewPass], body: planted.nextBody })
    return decideReadinessActions(
      { snapshot: replant, body: planted.nextBody, requiredChecks: REQUIRED, nowMs: clock }, on)
  }
  assert.deepEqual(settledAt(NOW).actions, [])
  assert.deepEqual(settledAt(LATER).actions, [])
})

test('new head under a ready PR converts back to draft', () => {
  const stale = snapshot(3, { draft: false, headSha: HEAD2,
    body: `intro\n\n${decideReadinessActions(input(3), on).block}\n` })
  const decision = decideReadinessActions({ snapshot: stale, body: stale.body,
    requiredChecks: REQUIRED, nowMs: NOW }, on)
  assert.ok(decision.actions.includes('convert-to-draft'))
  assert.ok(!decision.actions.includes('mark-ready'))
  assert.ok(!decision.actions.includes('enable-automerge'))
})

test('completed failure on the same head converts back to draft', () => {
  const red = greenChecks().map((c) => (c.name === 'ci-ok'
    ? { ...c, conclusion: 'failure' } : c))
  const decision = decideReadinessActions(input(4, { draft: false, checks: red }), on)
  assert.ok(decision.actions.includes('convert-to-draft'))
})

test('pending checks never flap a ready PR back to draft', () => {
  const pending = [{ name: 'ci-ok', status: 'in_progress' },
    { name: 'gitleaks', status: 'completed', conclusion: 'success', completedMs: 1 }]
  const decision = decideReadinessActions(input(5, { draft: false, checks: pending }), on)
  assert.ok(!decision.actions.includes('convert-to-draft'))
})

test('auto-merge arms only on a ready PR with CI and the review check green on this head', () => {
  const reviewPass = { name: 'Paperclip Review', status: 'completed', conclusion: 'success', completedMs: 1 }
  const armed = decideReadinessActions(
    input(6, { draft: false, autoMerge: false, checks: [...greenChecks(), reviewPass] }), on)
  assert.ok(armed.actions.includes('enable-automerge'))
  assert.ok(!armed.actions.includes('mark-ready'))
  // Missing review check on this head: no arm, even with green CI.
  const missing = decideReadinessActions(input(6, { draft: false }), on)
  assert.ok(!missing.actions.includes('enable-automerge'))
  // Already armed: no second arm call.
  const already = decideReadinessActions(
    input(6, { draft: false, autoMerge: true, checks: [...greenChecks(), reviewPass] }), on)
  assert.ok(!already.actions.includes('enable-automerge'))
  // Drafts never arm.
  const draft = decideReadinessActions(
    input(6, { draft: true, checks: [...greenChecks(), reviewPass] }), on)
  assert.ok(!draft.actions.includes('enable-automerge'))
})

test('malformed markers fail closed: no action, named reason', () => {
  const good = decideReadinessActions(input(7), on)
  const doubled = `intro\n\n${good.block}\n${good.block}\n`
  const decision = decideReadinessActions(
    { snapshot: snapshot(7, { body: doubled }), body: doubled, requiredChecks: REQUIRED, nowMs: NOW }, on)
  assert.deepEqual(decision.actions, [])
  assert.equal(decision.reason, 'malformed-markers')
  assert.equal(decision.malformed, true)
})

test('held PRs keep the block but take no transition', () => {
  const decision = decideReadinessActions(input(8, { labels: ['hold'] }), on)
  assert.deepEqual(decision.actions, ['update-body'])
  assert.equal(decision.reason, 'held-block-only')
  assert.equal(decision.held, true)
})

test('closed and merged PRs are no-ops', () => {
  for (const over of [[{ state: 'closed', merged: false }], [{ state: 'closed', merged: true }]]) {
    const decision = decideReadinessActions(input(9, ...over), on)
    assert.deepEqual(decision.actions, [])
    assert.equal(decision.closed, true)
  }
})

test('empty required checks and missing bodies throw instead of guessing', () => {
  assert.throws(() => decideReadinessActions({ ...input(10), requiredChecks: [] }, on))
  assert.throws(() => decideReadinessActions(
    { ...input(10), snapshot: snapshot(10, { body: null }), body: null }, on))
})

// --- runner ----------------------------------------------------------------

test('green draft reconciles end to end: body write then mark-ready', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1)]])
  const { github, calls } = doubles(snapshots)
  const { records, counters } = await createReconciler({ github, policy: on })
    .runReconcile({ references: [{ repository: repo, number: 1 }], nowMs: NOW })
  assert.equal(records[0].decision, 'acted')
  assert.deepEqual(records[0].actions, ['update-body', 'mark-ready'])
  assert.deepEqual(records[0].steps.map((s) => [s.action, s.result]),
    [['update-body', 'done'], ['mark-ready', 'done']])
  assert.deepEqual([counters.acted, counters.updatedBody, counters.markedReady, counters.errors], [1, 1, 1, 0])
  assert.match(records[0].log, new RegExp(`^readiness ${repo}#1@${HEAD}: acted \\(block-missing\\+mark-ready\\)$`))
  // Only the six duty operations ever appear.
  for (const [op] of calls) {
    assert.ok(['read-snapshot', 'read-rulesets', 'update-body', 'mark-ready',
      'convert-to-draft', 'enable-automerge'].includes(op), `unexpected call ${op}`)
  }
  // The body write carries the full reconciled body, not just the block.
  const written = calls.find(([op]) => op === 'update-body')[3]
  assert.ok(written.startsWith('## What Changed'))
  assert.ok(written.includes('<!-- readiness:start -->'))
})

test('flag off: green drafts log would-act and make no gh write', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1)]])
  const { github, calls } = doubles(snapshots)
  const { records, counters } = await createReconciler({ github, policy: off })
    .runReconcile({ references: [{ repository: repo, number: 1 }], nowMs: NOW })
  assert.equal(records[0].decision, 'would-act')
  assert.deepEqual(records[0].actions, ['update-body', 'mark-ready'])
  assert.deepEqual(calls.map(ops), ['read-snapshot', 'read-rulesets'])
  assert.equal(counters.wouldAct, 1)
})

test('flag is per repo: enabled reconciles while another only logs', async () => {
  const snapshots = new Map([
    [`${repo}#1`, snapshot(1)],
    [`${other}#7`, { ...snapshot(7), repository: other }],
  ])
  const { github, calls } = doubles(snapshots)
  const { records } = await createReconciler({ github, policy: on })
    .runReconcile({ references: [{ repository: repo, number: 1 }, { repository: other, number: 7 }], nowMs: NOW })
  assert.deepEqual(records.map((r) => [r.number, r.decision]), [[1, 'acted'], [7, 'would-act']])
  assert.deepEqual(calls.filter(([op]) => op === 'update-body').map((c) => c[1]), [repo])
})

test('unreadable snapshot, missing body and failed ruleset read are counted errors', async () => {
  const snapshots = new Map([
    [`${repo}#2`, snapshot(2, { body: null })],
    [`${repo}#3`, snapshot(3)],
  ])
  const { github } = doubles(snapshots, { rulesets: new Map() })
  const { records, counters } = await createReconciler({ github, policy: on })
    .runReconcile({ references: [
      { repository: repo, number: 1 }, { repository: repo, number: 2 }, { repository: repo, number: 3 },
    ], nowMs: NOW })
  assert.deepEqual(records.map((r) => [r.number, r.decision, r.reason]),
    [[1, 'none', 'snapshot-read-failed'], [2, 'none', 'body-unavailable'], [3, 'none', 'ruleset-read-failed']])
  assert.equal(counters.errors, 3)
})

test('a failed write records the step and counts the error without blinding the rest', async () => {
  const snapshots = new Map([
    [`${repo}#1`, snapshot(1)],
    [`${repo}#2`, snapshot(2)],
  ])
  const { github } = doubles(snapshots, { fail: ['mark-ready'] })
  const { records, counters } = await createReconciler({ github, policy: on })
    .runReconcile({ references: [{ repository: repo, number: 1 }, { repository: repo, number: 2 }], nowMs: NOW })
  assert.equal(records[0].decision, 'error')
  assert.deepEqual(records[0].steps.map((s) => [s.action, s.result]),
    [['update-body', 'done'], ['mark-ready', 'failed']])
  assert.equal(records[1].decision, 'error')
  assert.equal(counters.errors, 2)
  assert.equal(counters.updatedBody, 2)
  assert.equal(counters.markedReady, 0)
})

test('the duty never merges, never closes and never comments', async () => {
  const reviewPass = { name: 'Paperclip Review', status: 'completed', conclusion: 'success', completedMs: 1 }
  const snapshots = new Map([
    [`${repo}#6`, snapshot(6, { draft: false, checks: [...greenChecks(), reviewPass] })],
  ])
  const { github, calls } = doubles(snapshots)
  await createReconciler({ github, policy: on })
    .runReconcile({ references: [{ repository: repo, number: 6 }], nowMs: NOW })
  for (const [op] of calls) {
    assert.ok(['read-snapshot', 'read-rulesets', 'update-body', 'mark-ready',
      'convert-to-draft', 'enable-automerge'].includes(op), `unexpected call ${op}`)
  }
  assert.ok(calls.some(([op]) => op === 'enable-automerge'))
  // The arm binds the evaluated head, never an unbound call.
  const arm = calls.find(([op]) => op === 'enable-automerge')
  assert.equal(arm[3], HEAD)
})

test('duplicate references and bad clocks throw before any write', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1)]])
  const { github, calls } = doubles(snapshots)
  const reconciler = createReconciler({ github, policy: on })
  await assert.rejects(reconciler.runReconcile({
    references: [{ repository: repo, number: 1 }, { repository: repo, number: 1 }], nowMs: NOW,
  }))
  await assert.rejects(reconciler.runReconcile({ references: [{ repository: repo, number: 1 }], nowMs: -1 }))
  assert.deepEqual(calls.map(ops), [])
})

test('policy defaults to flag-off everywhere and rejects bad repos', () => {
  assert.deepEqual(readinessPolicy().enabledRepositories, [])
  assert.throws(() => readinessPolicy({ enabledRepositories: ['not a repo'] }))
  assert.throws(() => readinessPolicy({ enabledRepositories: [repo, repo] }))
  assert.throws(() => createReconciler({ github: {}, policy: on }))
})

test('readiness-only reader reconciles a snapshot without push age', async () => {
  // Readiness snapshots carry no headPushedMs (GitHub may return a null
  // pushedDate). With only getReadinessSnapshot present, the reconciler
  // must use it and reconcile instead of recording snapshot-read-failed.
  const snap = snapshot(1)
  delete snap.headPushedMs
  const snapshots = new Map([[`${repo}#1`, snap]])
  const { github, calls } = doubles(snapshots)
  github.getReadinessSnapshot = github.getCloseoutSnapshot
  delete github.getCloseoutSnapshot
  const { records, counters } = await createReconciler({ github, policy: on })
    .runReconcile({ references: [{ repository: repo, number: 1 }], nowMs: NOW })
  assert.equal(records[0].decision, 'acted')
  assert.deepEqual(records[0].actions, ['update-body', 'mark-ready'])
  assert.equal(counters.errors, 0)
  assert.ok(calls.some(([op]) => op === 'read-snapshot'))
})
