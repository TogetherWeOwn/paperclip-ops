// Acceptance tests for superseded cancelled checks in the readiness read.
//
// Incident shape: one head, four CheckRuns with one required name, the same
// check-suite provenance — a CANCELLED run, then SUCCESS runs that started
// after the cancelled run completed. The previous collapse kept the cancelled
// entry forever (worst rank wins, chronology and provenance discarded), so a
// healthy non-draft PR was converted back to draft on every pass.
//
// Contract pinned here, at the adapter-snapshot and reconciler level only
// (never at an internal helper's shape):
//   - a completed `cancelled` CheckRun is dropped from the readiness collapse
//     ONLY when a completed `success` CheckRun with the same name AND the same
//     provenance started strictly after the cancelled run completed.
//     Provenance is checkSuite.app.slug, plus — when checkSuite.workflowRun is
//     an object — its event and workflow.databaseId. An explicitly null
//     workflowRun (a non-Actions app) proves app-only provenance;
//   - everything else fails closed: equal/overlapping/missing chronology,
//     missing, malformed or different provenance, SKIPPED/NEUTRAL successors,
//     and non-cancelled failures are never superseded; the conservative
//     worst-rank collapse governs whatever remains;
//   - missing optional provenance/startedAt fields never error the snapshot by
//     themselves, while completedAt stays required for completed runs;
//   - the strict closeout and main-head readers keep rejecting duplicate
//     names, including this exact incident shape;
//   - the production snapshot query actually requests the new fields, emitted
//     check objects keep their public shape, and provenance text never reaches
//     PR bodies or sweep output.
//
// Offline: every GitHub call goes through an injected `run`/`exec` double.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, chmod, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { createGithubAdapter } from '../src/github-adapter.js'
import { createReconciler } from '../src/readiness-reconcile.js'
import { runSweep, recordLine, summaryLine } from '../scripts/readiness-sweep.mjs'

const repository = 'TogetherWeOwn/paperclip-ops'
const GH = '/usr/bin/gh'
const TOKEN = 'test-only-not-a-credential'
const HEAD = 'c'.repeat(40)
const PUSHED_AT = '2026-10-26T03:00:00.000Z'
const PUSHED_MS = Date.parse(PUSHED_AT)
const NOW = Date.parse('2026-10-26T11:00:00.000Z')
const REVIEW = 'Paperclip Review'
const NAME = 'pr-lint'
const REQUIRED = ['ci-ok', NAME, 'secret-scan', REVIEW]
const RULES = [{ type: 'required_status_checks',
  parameters: { required_status_checks: REQUIRED.map((context) => ({ context })) } }]
// Text that must never surface in a snapshot, a PR body or sweep output.
// Deliberately not token-shaped so no secret scanner flags the fixture.
const SENTINEL = 'leaktest-sentinel-xyz'
const WORKFLOW_ID = 987654

// Incident chronology (all on the head above).
const T = {
  cancelStart: '2026-10-26T03:48:02.000Z',
  cancelDone: '2026-10-26T03:48:18.000Z',
  successStart: '2026-10-26T03:48:20.000Z',
  successDone: '2026-10-26T03:49:00.000Z',
  success2Start: '2026-10-26T03:49:54.000Z',
  success2Done: '2026-10-26T03:50:30.000Z',
  success3Start: '2026-10-26T10:01:42.000Z',
  success3Done: '2026-10-26T10:02:10.000Z',
}

// --- GraphQL fixtures -------------------------------------------------------

const actionsProvenance = ({ slug = 'github-actions', event = 'pull_request', workflowId = WORKFLOW_ID } = {}) =>
  ({ app: { slug }, workflowRun: { event, workflow: { databaseId: workflowId } } })
const appOnlyProvenance = (slug = 'github-actions') => ({ app: { slug }, workflowRun: null })

function completedRun({ name = NAME, conclusion, startedAt, completedAt, checkSuite = actionsProvenance() }) {
  return { __typename: 'CheckRun', name, status: 'COMPLETED', conclusion, startedAt, completedAt, checkSuite }
}
const cancelledRun = (over = {}) =>
  completedRun({ conclusion: 'CANCELLED', startedAt: T.cancelStart, completedAt: T.cancelDone, ...over })
const successRun = (over = {}) =>
  completedRun({ conclusion: 'SUCCESS', startedAt: T.successStart, completedAt: T.successDone, ...over })
const failingRun = (conclusion, over = {}) =>
  completedRun({ conclusion, startedAt: T.cancelStart, completedAt: T.cancelDone, ...over })
const pendingRun = (status) => ({ __typename: 'CheckRun', name: NAME, status, conclusion: null,
  startedAt: status === 'IN_PROGRESS' ? '2026-10-26T03:50:00.000Z' : null, completedAt: null,
  checkSuite: actionsProvenance() })
const statusCtx = (context, state, createdAt = T.successDone) =>
  ({ __typename: 'StatusContext', context, state, createdAt })

// Pre-existing fixture shape: no startedAt, no checkSuite. Every older suite
// feeds checks like these, so they must keep reading without error.
const bareRun = (name, conclusion = 'SUCCESS', completedAt = PUSHED_AT) =>
  ({ __typename: 'CheckRun', name, status: 'COMPLETED', conclusion, completedAt })
const baseChecks = () => REQUIRED.map((name) => bareRun(name))
const heavy = () => [bareRun('suites')]

const publicCheck = (conclusion, completedAt) =>
  ({ name: NAME, status: 'completed', conclusion, completedMs: Date.parse(completedAt) })
const PUBLIC_CANCELLED = publicCheck('cancelled', T.cancelDone)
const PUBLIC_SUCCESS = publicCheck('success', T.successDone)

function pullRequest({ draft = true, nodes = baseChecks(), pushedDate = PUSHED_AT } = {}) {
  return {
    number: 7, url: `https://github.com/${repository}/pull/7`,
    title: 'Supersession fixture', body: '## What Changed\n\n- a fix\n',
    state: 'OPEN', isDraft: draft, merged: false,
    headRefName: 'fixture-branch', headRefOid: HEAD, reviewDecision: null,
    mergeStateStatus: 'CLEAN', baseRefName: 'main', autoMergeRequest: null,
    author: { __typename: 'User', login: 'fixture-author' },
    baseRepository: { nameWithOwner: repository },
    labels: { nodes: [], pageInfo: { hasNextPage: false } },
    commits: { nodes: [{ commit: { oid: HEAD, pushedDate,
      statusCheckRollup: { contexts: { nodes, pageInfo: { hasNextPage: false } } } } }] },
  }
}
const snapshotEnvelope = (pr) => ({ data: { repository: { nameWithOwner: repository, isPrivate: false, pullRequest: pr } } })
const listEnvelope = (numbers) => ({ data: { repository: { nameWithOwner: repository,
  pullRequests: { nodes: numbers.map((number) => ({ number })), totalCount: numbers.length,
    pageInfo: { hasNextPage: false, endCursor: null } } } } })
const mainEnvelope = (nodes) => ({ data: { repository: { nameWithOwner: repository,
  defaultBranchRef: { name: 'main', target: { __typename: 'Commit', oid: HEAD, pushedDate: PUSHED_AT,
    statusCheckRollup: { contexts: { nodes, pageInfo: { hasNextPage: false } } } } } } } })

// One GitHub double behind the direct adapter (`run('gh', args)`) and the
// production runner (`exec(executable, args, options)`). Writes are recorded
// so "zero mutation" stays observable.
function githubDouble({ pr = pullRequest(), rules = RULES, mainNodes = [] } = {}) {
  const calls = []
  const writes = []
  const snapshots = []
  function respond(args) {
    calls.push([...args])
    if (args[0] === 'auth') return ''
    const query = args.find((arg) => typeof arg === 'string' && arg.startsWith('query='))
    if (args.includes('--method') && args[args.indexOf('--method') + 1] !== 'GET') {
      writes.push([...args])
      return '{}'
    }
    if (args[0] === 'pr' && args[1] === 'merge') { writes.push([...args]); return '' }
    if (query !== undefined) {
      if (query.includes('mutation')) { writes.push([...args]); return '{}' }
      if (query.includes('pullRequests(')) return JSON.stringify(listEnvelope([7]))
      if (query.includes('pullRequest(number')) {
        snapshots.push([...args])
        return JSON.stringify(snapshotEnvelope(pr))
      }
      if (query.includes('defaultBranchRef')) return JSON.stringify(mainEnvelope(mainNodes))
      throw new Error('unexpected GraphQL document')
    }
    if (args.some((arg) => typeof arg === 'string' && arg.includes('/rules/branches/'))) return JSON.stringify(rules)
    if (args.some((arg) => typeof arg === 'string' && arg.includes('/branches/main/protection'))) {
      return JSON.stringify({ required_status_checks: { contexts: REQUIRED, checks: [] } })
    }
    throw new Error('unexpected GitHub request')
  }
  return {
    calls, writes, snapshots,
    run: async (_file, args) => ({ stdout: respond(args), stderr: '' }),
    exec: async (_file, args) => ({ stdout: respond(args), stderr: '' }),
  }
}

function adapterOver(double) {
  return createGithubAdapter({ allowedRepositories: [repository], run: double.run })
}

function reconcilerOver(double) {
  const adapter = adapterOver(double)
  const bodies = []
  const transitions = []
  const github = {
    ...adapter,
    getCloseoutSnapshot: async () => { throw new Error('closeout reader is not the readiness path') },
    updatePullRequestBody: async (_repo, _number, body) => { bodies.push(body) },
    markReadyForReview: async () => { transitions.push('mark-ready') },
    convertToDraft: async () => { transitions.push('convert-to-draft') },
    enableAutoMerge: async () => { transitions.push('enable-automerge') },
  }
  const reconciler = createReconciler({ github,
    policy: { enabledRepositories: [repository], holdLabels: ['hold'] }, nowMs: () => NOW })
  return { reconciler, bodies, transitions }
}

const nodeName = (node) => node.__typename === 'StatusContext' ? node.context : node.name

function permutations(items) {
  if (items.length <= 1) return [items]
  return items.flatMap((item, index) =>
    permutations([...items.slice(0, index), ...items.slice(index + 1)]).map((rest) => [item, ...rest]))
}

// Every ordering of the repeated entries, at the front, at the back and split
// around the other required checks, so neither order nor adjacency can matter.
function arrangements(entries) {
  const others = baseChecks().filter((node) => nodeName(node) !== NAME)
  return permutations(entries).flatMap((p) => [
    [...p, ...others, ...heavy()], [...others, ...p, ...heavy()], [p[0], ...others, ...p.slice(1), ...heavy()],
  ])
}

// Adapter level: the collapse for NAME (and the whole emitted check list) is
// identical in every input order.
async function collapsedAcrossOrders(entries) {
  let firstChecks = null
  let firstMerged = null
  for (const nodes of arrangements(entries)) {
    const snapshot = await adapterOver(githubDouble({ pr: pullRequest({ nodes }) }))
      .getReadinessSnapshot(repository, 7, { nowMs: NOW })
    const matches = snapshot.checks.filter((c) => c.name === NAME)
    assert.equal(matches.length, 1, `exactly one collapsed entry for ${NAME}`)
    if (firstChecks === null) { firstChecks = snapshot.checks; firstMerged = matches[0] } else {
      assert.deepEqual(snapshot.checks, firstChecks, 'input order changed the emitted checks')
    }
  }
  return firstMerged
}

// Reconciler level: actions, transitions and the maintained body are identical
// in every input order, and nothing errors.
async function gateAcrossOrders(entries, draft) {
  const outcomes = []
  for (const nodes of arrangements(entries)) {
    const { reconciler, bodies, transitions } = reconcilerOver(githubDouble({ pr: pullRequest({ draft, nodes }) }))
    const { records, counters } = await reconciler.runReconcile({ references: [{ repository, number: 7 }] })
    assert.equal(counters.errors, 0, `must evaluate, not error: ${JSON.stringify(records[0])}`)
    assert.equal(counters.evaluated, 1)
    assert.equal(records.length, 1)
    outcomes.push({ actions: [...(records[0].actions ?? [])], body: bodies.at(-1) ?? null, transitions: [...transitions] })
  }
  const [first, ...rest] = outcomes
  for (const other of rest) {
    assert.deepEqual(other, first, 'input order changed the reconciler outcome')
  }
  return first
}

const rowsFor = (body) => (body ?? '').split('\n').filter((line) => line.startsWith(`| ${NAME} |`))

// Positive gate expectations shared by every supersession case: a draft may
// mark ready, a ready PR arms auto-merge (the arrangements carry the passing
// review entry and the heavy `suites` success the arm demands).
async function assertSupersededGates(entries, label) {
  const draft = await gateAcrossOrders(entries, true)
  assert.deepEqual(draft.actions, ['update-body', 'mark-ready'], `${label}: draft actions`)
  assert.deepEqual(draft.transitions, ['mark-ready'], `${label}: draft transitions`)
  const ready = await gateAcrossOrders(entries, false)
  assert.deepEqual(ready.actions, ['update-body', 'enable-automerge'], `${label}: ready actions`)
  assert.deepEqual(ready.transitions, ['enable-automerge'], `${label}: ready transitions`)
  return { draft, ready }
}

// Fail-closed gate expectations: the block is maintained, a draft never marks
// ready, and a ready PR with a completed failing required check goes back.
async function assertFailsClosed(label, entries, expectedMerged = PUBLIC_CANCELLED) {
  const merged = await collapsedAcrossOrders(entries)
  if (expectedMerged !== null) assert.deepEqual(merged, expectedMerged, `${label}: collapsed entry`)
  const draft = await gateAcrossOrders(entries, true)
  assert.deepEqual(draft.actions, ['update-body'], `${label}: draft must not mark ready`)
  assert.deepEqual(draft.transitions, [], `${label}: draft transitions`)
  const ready = await gateAcrossOrders(entries, false)
  assert.deepEqual(ready.actions, ['update-body', 'convert-to-draft'], `${label}: ready actions`)
  assert.deepEqual(ready.transitions, ['convert-to-draft'], `${label}: ready transitions`)
  const rows = rowsFor(ready.body)
  assert.equal(rows.length, 1, `${label}: one body row for ${NAME}`)
  assert.ok(rows[0].includes('❌'), `${label}: failing row stays visible: ${rows[0]}`)
  return merged
}

// --- 1. positives: a qualifying later success supersedes the cancelled run --

test('single cancelled plus single qualifying later success, both orders: collapse is the success and both gates pass', async () => {
  const entries = [cancelledRun(), successRun()]
  assert.deepEqual(await collapsedAcrossOrders(entries), PUBLIC_SUCCESS)
  const { ready } = await assertSupersededGates(entries, 'pair')
  const rows = rowsFor(ready.body)
  assert.equal(rows.length, 1, 'one body row for the repeated name')
  assert.ok(rows[0].includes('✅'), rows[0])
  for (const leaked of ['github-actions', 'pull_request', String(WORKFLOW_ID), 'checkSuite', 'startedAt']) {
    assert.ok(!ready.body.includes(leaked), `provenance reached the PR body: ${leaked}`)
  }
})

test('a success starting 1ms after the cancelled run completed supersedes it (strictly-after boundary)', async () => {
  const entries = [cancelledRun(), successRun({ startedAt: '2026-10-26T03:48:18.001Z' })]
  assert.deepEqual(await collapsedAcrossOrders(entries), PUBLIC_SUCCESS)
  const draft = await gateAcrossOrders(entries, true)
  assert.deepEqual(draft.actions, ['update-body', 'mark-ready'])
})

test('incident shape (cancelled, then three later successes, any order): collapses to a success, draft marks ready, ready arms instead of converting', async () => {
  const entries = [
    cancelledRun(),
    successRun(),
    successRun({ startedAt: T.success2Start, completedAt: T.success2Done }),
    successRun({ startedAt: T.success3Start, completedAt: T.success3Done }),
  ]
  const merged = await collapsedAcrossOrders(entries)
  assert.equal(merged.status, 'completed')
  assert.equal(merged.conclusion, 'success')
  const { ready } = await assertSupersededGates(entries, 'incident')
  const rows = rowsFor(ready.body)
  assert.equal(rows.length, 1, 'the repeated name renders once')
  assert.ok(rows[0].includes('✅'), rows[0])
  assert.ok(ready.body.includes(`${REVIEW}: 5/5 ✅`), ready.body)
})

test('cancelled-success-cancelled-success chain where each success starts after the preceding cancelled completed', async () => {
  const entries = [
    cancelledRun({ startedAt: '2026-10-26T03:10:00.000Z', completedAt: '2026-10-26T03:10:10.000Z' }),
    successRun({ startedAt: '2026-10-26T03:10:11.000Z', completedAt: '2026-10-26T03:10:20.000Z' }),
    cancelledRun({ startedAt: '2026-10-26T03:10:21.000Z', completedAt: '2026-10-26T03:10:30.000Z' }),
    successRun({ startedAt: '2026-10-26T03:10:31.000Z', completedAt: '2026-10-26T03:10:40.000Z' }),
  ]
  const merged = await collapsedAcrossOrders(entries)
  assert.equal(merged.status, 'completed')
  assert.equal(merged.conclusion, 'success')
  await assertSupersededGates(entries, 'chain')
})

test('explicitly null workflowRun on both entries proves app-only provenance and supersedes', async () => {
  const entries = [
    cancelledRun({ checkSuite: appOnlyProvenance('some-ci-app') }),
    successRun({ checkSuite: appOnlyProvenance('some-ci-app') }),
  ]
  assert.deepEqual(await collapsedAcrossOrders(entries), PUBLIC_SUCCESS)
  await assertSupersededGates(entries, 'app-only')
})

for (const status of ['IN_PROGRESS', 'QUEUED']) {
  test(`a ${status.toLowerCase().replace('_', '-')} entry beside a superseded cancelled run keeps the collapse pending (no mark-ready, no arm, no convert)`, async () => {
    const entries = [cancelledRun(), successRun(), pendingRun(status)]
    const merged = await collapsedAcrossOrders(entries)
    assert.deepEqual(merged, { name: NAME, status: status === 'IN_PROGRESS' ? 'in_progress' : 'queued' })
    for (const draft of [true, false]) {
      const outcome = await gateAcrossOrders(entries, draft)
      assert.deepEqual(outcome.actions, ['update-body'])
      assert.deepEqual(outcome.transitions, [])
      const rows = rowsFor(outcome.body)
      assert.equal(rows.length, 1)
      assert.ok(rows[0].includes('⏳'), rows[0])
    }
  })
}

// --- 2. chronology controls: no supersession without strictly-after ----------

const pairWith = (mutate) => () => {
  const cancelled = cancelledRun()
  const success = successRun()
  mutate(cancelled, success)
  return [cancelled, success]
}

const CHRONOLOGY_CASES = [
  ['successor started exactly when the cancelled run completed', pairWith((_c, s) => { s.startedAt = T.cancelDone })],
  ['successor started 1ms before the cancelled run completed', pairWith((_c, s) => { s.startedAt = '2026-10-26T03:48:17.999Z' })],
  ['successor started before the cancelled run even started', pairWith((_c, s) => { s.startedAt = '2026-10-26T03:48:01.000Z' })],
  ['successor startedAt field absent', pairWith((_c, s) => { delete s.startedAt })],
  ['successor startedAt null', pairWith((_c, s) => { s.startedAt = null })],
  ['successor startedAt unparsable', pairWith((_c, s) => { s.startedAt = 'not-a-date' })],
  ['successor startedAt not a string', pairWith((_c, s) => { s.startedAt = Date.parse(T.successStart) })],
]

for (const [label, makeEntries] of CHRONOLOGY_CASES) {
  test(`no supersession: ${label}`, async () => {
    await assertFailsClosed(label, makeEntries())
  })
}

// --- 3. missing or malformed provenance on either entry ----------------------

const PROVENANCE_CASES = [
  ['successor checkSuite absent', pairWith((_c, s) => { delete s.checkSuite })],
  ['cancelled checkSuite absent', pairWith((c) => { delete c.checkSuite })],
  ['successor checkSuite null', pairWith((_c, s) => { s.checkSuite = null })],
  ['cancelled checkSuite null', pairWith((c) => { c.checkSuite = null })],
  ['successor app absent', pairWith((_c, s) => { delete s.checkSuite.app })],
  ['successor app null', pairWith((_c, s) => { s.checkSuite.app = null })],
  ['cancelled app absent', pairWith((c) => { delete c.checkSuite.app })],
  ['successor slug absent', pairWith((_c, s) => { delete s.checkSuite.app.slug })],
  ['successor slug empty', pairWith((_c, s) => { s.checkSuite.app.slug = '' })],
  ['successor slug null', pairWith((_c, s) => { s.checkSuite.app.slug = null })],
  ['successor slug not a string', pairWith((_c, s) => { s.checkSuite.app.slug = 42 })],
  ['cancelled slug empty', pairWith((c) => { c.checkSuite.app.slug = '' })],
  ['successor workflowRun field absent', pairWith((_c, s) => { delete s.checkSuite.workflowRun })],
  ['cancelled workflowRun field absent', pairWith((c) => { delete c.checkSuite.workflowRun })],
  ['successor workflowRun empty object', pairWith((_c, s) => { s.checkSuite.workflowRun = {} })],
  ['successor event absent', pairWith((_c, s) => { delete s.checkSuite.workflowRun.event })],
  ['successor event empty', pairWith((_c, s) => { s.checkSuite.workflowRun.event = '' })],
  ['successor event null', pairWith((_c, s) => { s.checkSuite.workflowRun.event = null })],
  ['successor event not a string', pairWith((_c, s) => { s.checkSuite.workflowRun.event = 42 })],
  ['cancelled event absent', pairWith((c) => { delete c.checkSuite.workflowRun.event })],
  ['successor workflow absent', pairWith((_c, s) => { delete s.checkSuite.workflowRun.workflow })],
  ['successor workflow null', pairWith((_c, s) => { s.checkSuite.workflowRun.workflow = null })],
  ['successor workflow empty object (databaseId absent)', pairWith((_c, s) => { s.checkSuite.workflowRun.workflow = {} })],
  ['successor databaseId null', pairWith((_c, s) => { s.checkSuite.workflowRun.workflow.databaseId = null })],
  ['successor databaseId zero', pairWith((_c, s) => { s.checkSuite.workflowRun.workflow.databaseId = 0 })],
  ['successor databaseId negative', pairWith((_c, s) => { s.checkSuite.workflowRun.workflow.databaseId = -1 })],
  ['successor databaseId fractional', pairWith((_c, s) => { s.checkSuite.workflowRun.workflow.databaseId = 1.5 })],
  ['successor databaseId a string', pairWith((_c, s) => { s.checkSuite.workflowRun.workflow.databaseId = String(WORKFLOW_ID) })],
  ['cancelled databaseId zero', pairWith((c) => { c.checkSuite.workflowRun.workflow.databaseId = 0 })],
]

for (const [label, makeEntries] of PROVENANCE_CASES) {
  test(`no supersession and no snapshot error: ${label}`, async () => {
    await assertFailsClosed(label, makeEntries())
  })
}

// --- 4. different provenance never supersedes ---------------------------------

const DIFFERENT_PROVENANCE_CASES = [
  ['different app slug', () => [cancelledRun(), successRun({ checkSuite: actionsProvenance({ slug: 'other-app' }) })]],
  ['different workflow databaseId', () => [cancelledRun(), successRun({ checkSuite: actionsProvenance({ workflowId: WORKFLOW_ID + 1 }) })]],
  ['different workflowRun event', () => [cancelledRun(), successRun({ checkSuite: actionsProvenance({ event: 'push' }) })]],
  ['app-only successor beside an Actions cancelled run (same slug)', () => [cancelledRun(), successRun({ checkSuite: appOnlyProvenance() })]],
  ['Actions successor beside an app-only cancelled run (same slug)', () => [cancelledRun({ checkSuite: appOnlyProvenance() }), successRun()]],
  ['app-only entries with different slugs', () => [
    cancelledRun({ checkSuite: appOnlyProvenance('some-ci-app') }),
    successRun({ checkSuite: appOnlyProvenance('other-ci-app') }),
  ]],
  ['a same-name StatusContext SUCCESS created after the cancelled CheckRun completed', () => [
    cancelledRun(), statusCtx(NAME, 'SUCCESS'),
  ]],
]

for (const [label, makeEntries] of DIFFERENT_PROVENANCE_CASES) {
  test(`no supersession: ${label}`, async () => {
    await assertFailsClosed(label, makeEntries())
  })
}

// --- 5. only a cancelled run can be dropped, and only by SUCCESS --------------

for (const conclusion of ['SKIPPED', 'NEUTRAL']) {
  test(`a later same-provenance ${conclusion} does not supersede a cancelled run`, async () => {
    await assertFailsClosed(conclusion, [
      cancelledRun(),
      completedRun({ conclusion, startedAt: T.successStart, completedAt: T.successDone }),
    ])
  })
}

for (const conclusion of ['FAILURE', 'TIMED_OUT']) {
  test(`a later same-provenance SUCCESS never supersedes a ${conclusion} run in this slice`, async () => {
    await assertFailsClosed(conclusion, [failingRun(conclusion), successRun()],
      publicCheck(conclusion.toLowerCase(), T.cancelDone))
  })
}

// --- 6. whatever remains after a drop still governs ---------------------------

test('a later cancelled run with no qualifying success after it still dominates', async () => {
  const entries = [
    cancelledRun({ startedAt: '2026-10-26T03:10:00.000Z', completedAt: '2026-10-26T03:10:10.000Z' }),
    successRun({ startedAt: '2026-10-26T03:10:11.000Z', completedAt: '2026-10-26T03:10:20.000Z' }),
    cancelledRun({ startedAt: '2026-10-26T03:10:21.000Z', completedAt: '2026-10-26T03:10:30.000Z' }),
  ]
  const merged = await collapsedAcrossOrders(entries)
  assert.equal(merged.status, 'completed')
  assert.equal(merged.conclusion, 'cancelled')
  const draft = await gateAcrossOrders(entries, true)
  assert.deepEqual(draft.actions, ['update-body'])
  const ready = await gateAcrossOrders(entries, false)
  assert.deepEqual(ready.actions, ['update-body', 'convert-to-draft'])
})

test('a different-provenance failure for the same name still dominates beside a qualifying pair', async () => {
  const entries = [
    cancelledRun(),
    successRun(),
    failingRun('FAILURE', { startedAt: '2026-10-26T03:47:00.000Z', completedAt: '2026-10-26T03:48:00.000Z',
      checkSuite: actionsProvenance({ slug: 'other-app' }) }),
  ]
  const merged = await collapsedAcrossOrders(entries)
  assert.equal(merged.status, 'completed')
  assert.ok(['cancelled', 'failure'].includes(merged.conclusion),
    `a failing entry must remain dominant, got ${merged.conclusion}`)
  const draft = await gateAcrossOrders(entries, true)
  assert.deepEqual(draft.actions, ['update-body'])
  const ready = await gateAcrossOrders(entries, false)
  assert.deepEqual(ready.actions, ['update-body', 'convert-to-draft'])
  const rows = rowsFor(ready.body)
  assert.equal(rows.length, 1)
  assert.ok(rows[0].includes('❌'), rows[0])
})

test('checks with no startedAt or checkSuite anywhere still read (optional fields alone never error) and never supersede', async () => {
  const entries = [bareRun(NAME, 'CANCELLED', T.cancelDone), bareRun(NAME, 'SUCCESS', T.successDone)]
  assert.deepEqual(await collapsedAcrossOrders(entries), PUBLIC_CANCELLED)
  const draft = await gateAcrossOrders(entries, true)
  assert.deepEqual(draft.actions, ['update-body'])
})

test('completedAt stays required for a completed successor, and the error never echoes provenance text', async () => {
  const hostileProvenance = actionsProvenance({ slug: SENTINEL, event: SENTINEL })
  const nodes = [
    cancelledRun({ checkSuite: hostileProvenance }),
    completedRun({ conclusion: 'SUCCESS', startedAt: T.successStart, completedAt: null, checkSuite: hostileProvenance }),
    ...baseChecks().filter((n) => n.name !== NAME),
  ]
  const adapter = adapterOver(githubDouble({ pr: pullRequest({ nodes }) }))
  await assert.rejects(adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW }), (error) => {
    assert.equal(String(error?.message ?? error).includes(SENTINEL), false)
    return true
  })
})

// --- 7. strict readers unchanged ------------------------------------------------

test('getCloseoutSnapshot still rejects the incident shape and a qualifying pair as duplicate names', async () => {
  const others = baseChecks().filter((n) => n.name !== NAME)
  for (const entries of [
    [cancelledRun(), successRun(),
      successRun({ startedAt: T.success2Start, completedAt: T.success2Done }),
      successRun({ startedAt: T.success3Start, completedAt: T.success3Done })],
    [cancelledRun(), successRun()],
  ]) {
    const double = githubDouble({ pr: pullRequest({ nodes: [...entries, ...others, ...heavy()] }) })
    await assert.rejects(adapterOver(double).getCloseoutSnapshot(repository, 7, { nowMs: NOW }))
  }
  // Control: unique names still read, with today's public check shape.
  const control = await adapterOver(githubDouble({ pr: pullRequest({ nodes: [...baseChecks(), ...heavy()] }) }))
    .getCloseoutSnapshot(repository, 7, { nowMs: NOW })
  assert.deepEqual(control.checks.find((c) => c.name === NAME),
    { name: NAME, status: 'completed', conclusion: 'success', completedMs: PUSHED_MS })
})

test('getMainHeadChecks still rejects the incident shape as duplicate names', async () => {
  const incident = [cancelledRun(), successRun(),
    successRun({ startedAt: T.success2Start, completedAt: T.success2Done }),
    successRun({ startedAt: T.success3Start, completedAt: T.success3Done })]
  await assert.rejects(adapterOver(githubDouble({ mainNodes: incident }))
    .getMainHeadChecks(repository, { nowMs: NOW }))
  const control = await adapterOver(githubDouble({ mainNodes: baseChecks() }))
    .getMainHeadChecks(repository, { nowMs: NOW })
  assert.equal(control.checks.length, REQUIRED.length)
  assert.deepEqual(control.requiredChecks, [...REQUIRED].sort())
})

// --- 8. production query, emitted shape, no untrusted text ----------------------

test('the readiness snapshot query requests startedAt and checkSuite provenance and stays balanced', async () => {
  const double = githubDouble({ pr: pullRequest() })
  await adapterOver(double).getReadinessSnapshot(repository, 7, { nowMs: NOW })
  assert.equal(double.snapshots.length, 1)
  const document = double.snapshots[0].find((arg) => arg.startsWith('query=')).slice('query='.length)
  for (const field of ['startedAt', 'checkSuite', 'app', 'slug', 'workflowRun', 'event', 'workflow', 'databaseId']) {
    assert.ok(new RegExp(`\\b${field}\\b`).test(document), `readiness query does not request ${field}`)
  }
  for (const [open, close, label] of [['{', '}', 'brace'], ['(', ')', 'paren']]) {
    let depth = 0
    for (const char of document) {
      if (char === open) depth++
      if (char === close) { assert.ok(depth > 0, `closes a ${label} that was never opened`); depth-- }
    }
    assert.equal(depth, 0, `${depth} ${label}(s) left unclosed`)
  }
})

test('emitted snapshot checks keep the public shape and carry no provenance fields', async () => {
  const nodes = [cancelledRun(), successRun(), pendingRun('IN_PROGRESS'),
    ...baseChecks().filter((n) => n.name !== NAME), ...heavy()]
  const snapshot = await adapterOver(githubDouble({ pr: pullRequest({ nodes }) }))
    .getReadinessSnapshot(repository, 7, { nowMs: NOW })
  for (const check of snapshot.checks) {
    assert.deepEqual(Object.keys(check).sort(), check.status === 'completed'
      ? ['completedMs', 'conclusion', 'name', 'status'] : ['name', 'status'], JSON.stringify(check))
  }
  const serialized = JSON.stringify(snapshot.checks)
  for (const leaked of ['github-actions', 'pull_request', String(WORKFLOW_ID), 'checkSuite', 'startedAt', 'workflowRun']) {
    assert.ok(!serialized.includes(leaked), `provenance reached emitted checks: ${leaked}`)
  }
})

async function scratch(t) {
  const dir = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'readiness-supersession-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  await chmod(dir, 0o700)
  return dir
}

test('provenance text from GitHub never surfaces in the snapshot or in sweep records and lines', async (t) => {
  const hostileProvenance = actionsProvenance({ slug: SENTINEL, event: SENTINEL })
  const nodes = [
    cancelledRun({ checkSuite: hostileProvenance }),
    successRun({ checkSuite: hostileProvenance }),
    ...baseChecks().filter((n) => n.name !== NAME),
    ...heavy(),
  ]
  const pr = pullRequest({ nodes })
  const snapshot = await adapterOver(githubDouble({ pr })).getReadinessSnapshot(repository, 7, { nowMs: NOW })
  assert.ok(!JSON.stringify(snapshot).includes(SENTINEL), 'provenance surfaced in the snapshot')

  const policyPath = join(await scratch(t), 'policy.json')
  await writeFile(policyPath, JSON.stringify({ version: 1, ghExecutable: GH,
    enabledRepositories: [], scanRepositories: [repository], holdLabels: ['hold'] }), { mode: 0o600 })
  const double = githubDouble({ pr })
  const result = await runSweep({ policyPath, nowMs: NOW,
    env: { READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN }, exec: double.exec })
  assert.equal(result.counters.errors, 0, JSON.stringify(result.records))
  assert.equal(result.counters.evaluated, 1)
  assert.deepEqual(double.writes, [])
  const observable = [JSON.stringify(result.records), JSON.stringify(result.counters),
    ...result.records.map(recordLine), summaryLine(result.counters, result.listErrors)].join('\n')
  assert.ok(!observable.includes(SENTINEL), 'provenance surfaced in sweep output')
  assert.ok(!observable.includes(TOKEN), 'the job token surfaced in sweep output')
})
