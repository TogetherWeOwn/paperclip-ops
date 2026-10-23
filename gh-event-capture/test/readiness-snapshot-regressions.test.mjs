// Acceptance regressions for the readiness sweep's snapshot read.
//
// Live symptom on main: every open PR read as `snapshot-read-failed`
// (total=13 evaluated=0 errors=13). Two real GitHub shapes trip the strict
// closeout normalizer, and neither is a read failure for a readiness decision:
//   1. the head commit's `pushedDate` is an explicit null;
//   2. the head commit's check rollup repeats check names (a rerun, a
//      status context beside a check run, several workflow runs).
//
// Contract pinned here (independent of any internal structure):
//   - the readiness reconciler reads through a DEDICATED adapter method,
//     getReadinessSnapshot(repository, number, { nowMs }); readiness decisions
//     never use the push time, so an explicit null is accepted there, and it
//     is never replaced by an invented time;
//   - getCloseoutSnapshot stays strict (a missing push age and repeated check
//     names remain errors for the age-sensitive closeout consumers);
//   - repeated names collapse conservatively: a name passes ONLY if every
//     entry with that name passes, regardless of input order;
//   - every other guard (exact head match, identity/lifecycle/clock,
//     truncation, malformed checks, transport) still fails as a counted error
//     with no mutation and no retry;
//   - the sweep exposes safe, fixed-vocabulary facts about the null push time,
//     the repeated-name counts and the failure class, never raw GitHub text.
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
const OTHER_HEAD = 'd'.repeat(40)
const PUSHED_AT = '2026-10-03T07:00:00.000Z'
const PUSHED_MS = Date.parse(PUSHED_AT)
const NOW = PUSHED_MS + 60_000
const REVIEW = 'Paperclip Review'
const REQUIRED = ['ci-ok', 'secret-scan', REVIEW]
const RULES = [{ type: 'required_status_checks',
  parameters: { required_status_checks: REQUIRED.map((context) => ({ context })) } }]
// Text that must never surface in anything the sweep emits. Deliberately not
// token-shaped so no secret scanner flags the fixture.
const SENTINEL = 'leaktest-sentinel-xyz'

// --- GraphQL fixtures -------------------------------------------------------

const checkRun = (name, status, conclusion = null, completedAt = null) =>
  ({ __typename: 'CheckRun', name, status, conclusion, completedAt })
const statusCtx = (context, state, createdAt = PUSHED_AT) =>
  ({ __typename: 'StatusContext', context, state, createdAt })
const ok = (name) => checkRun(name, 'COMPLETED', 'SUCCESS', PUSHED_AT)
const skipped = (name) => checkRun(name, 'COMPLETED', 'SKIPPED', PUSHED_AT)
const bad = (name) => checkRun(name, 'COMPLETED', 'FAILURE', PUSHED_AT)
const running = (name) => checkRun(name, 'IN_PROGRESS')
const queued = (name) => checkRun(name, 'QUEUED')
const ctxOk = (name) => statusCtx(name, 'SUCCESS')
const ctxBad = (name) => statusCtx(name, 'FAILURE')
const ctxPending = (name) => statusCtx(name, 'PENDING')
const ctxExpected = (name) => statusCtx(name, 'EXPECTED')

const baseChecks = () => REQUIRED.map(ok)

function pullRequest({ draft = true, nodes = baseChecks(), pushedDate = null, oid = HEAD,
  commits = null, rollup = undefined, overrides = {} } = {}) {
  return {
    number: 7, url: `https://github.com/${repository}/pull/7`,
    title: 'Readiness fixture', body: '## What Changed\n\n- a fix\n',
    state: 'OPEN', isDraft: draft, merged: false,
    headRefName: 'fixture-branch', headRefOid: HEAD, reviewDecision: null,
    mergeStateStatus: 'CLEAN', baseRefName: 'main', autoMergeRequest: null,
    author: { __typename: 'User', login: 'fixture-author' },
    baseRepository: { nameWithOwner: repository },
    labels: { nodes: [], pageInfo: { hasNextPage: false } },
    commits: commits ?? { nodes: [{ commit: { oid, pushedDate,
      statusCheckRollup: rollup === undefined
        ? { contexts: { nodes, pageInfo: { hasNextPage: false } } } : rollup } }] },
    ...overrides,
  }
}
const snapshotEnvelope = (pr) => ({ data: { repository: { nameWithOwner: repository, isPrivate: false, pullRequest: pr } } })
const listEnvelope = (numbers) => ({ data: { repository: { nameWithOwner: repository,
  pullRequests: { nodes: numbers.map((number) => ({ number })), totalCount: numbers.length,
    pageInfo: { hasNextPage: false, endCursor: null } } } } })

// One GitHub double behind both the direct adapter (`run('gh', args)`) and the
// production runner (`exec(executable, args, options)`): only `args` matter.
// Writes are classified and recorded so "zero mutation" is observable.
function githubDouble({ pr = pullRequest(), snapshot = undefined, rules = RULES, snapshotError = null } = {}) {
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
        if (snapshotError) throw snapshotError
        return JSON.stringify(snapshot ?? snapshotEnvelope(pr))
      }
      throw new Error('unexpected GraphQL document')
    }
    if (args.some((arg) => typeof arg === 'string' && arg.includes('/rules/branches/'))) return JSON.stringify(rules)
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

// The real adapter plus recording writers: the reconciler under test sees the
// production snapshot reader and decision code, and writes land nowhere.
function reconcilerOver(double, { enabled = true, closeoutCalls = [] } = {}) {
  const adapter = adapterOver(double)
  const bodies = []
  const transitions = []
  const github = {
    ...adapter,
    // The strict closeout reader must NOT be the readiness path.
    getCloseoutSnapshot: async (...args) => { closeoutCalls.push(args); throw new Error('closeout reader is not the readiness path') },
    updatePullRequestBody: async (_repo, _number, body) => { bodies.push(body) },
    markReadyForReview: async () => { transitions.push('mark-ready') },
    convertToDraft: async () => { transitions.push('convert-to-draft') },
    enableAutoMerge: async () => { transitions.push('enable-automerge') },
  }
  const reconciler = createReconciler({ github,
    policy: { enabledRepositories: enabled ? [repository] : [], holdLabels: ['hold'] }, nowMs: () => NOW })
  return { reconciler, bodies, transitions }
}

async function scratch(t) {
  const dir = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'readiness-snapshot-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  await chmod(dir, 0o700)
  return dir
}

async function policyFile(t, { enabled = [] } = {}) {
  const path = join(await scratch(t), 'policy.json')
  await writeFile(path, JSON.stringify({ version: 1, ghExecutable: GH,
    enabledRepositories: enabled, scanRepositories: [repository], holdLabels: ['hold'] }), { mode: 0o600 })
  return path
}

const JOB_ENV = { READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN }

async function sweep(t, double, { enabled = [], env = JOB_ENV } = {}) {
  const policyPath = await policyFile(t, { enabled })
  return runSweep({ policyPath, nowMs: NOW, env, exec: double.exec })
}

// Everything a job can observe from a pass: the records, the counters and the
// exact lines main() would print.
function observable({ records, counters, listErrors }) {
  return [JSON.stringify(records), JSON.stringify(counters), String(listErrors),
    ...records.map(recordLine), summaryLine(counters, listErrors)].join('\n')
}

function permutations(items) {
  if (items.length <= 1) return [items]
  return items.flatMap((item, index) =>
    permutations([...items.slice(0, index), ...items.slice(index + 1)]).map((rest) => [item, ...rest]))
}

// Every ordering of the repeated entries, placed at the front, at the back and
// split around the other checks, so neither array order nor adjacency of the
// repeats can matter.
function arrangements(name, entries) {
  const others = baseChecks().filter((node) => (node.name ?? node.context) !== name)
  // The arm requires the heavy job's own completed success on the head
  // (flag-on proof A): carry passing suites evidence in every arrangement so
  // these cases pin repeated-name strictness, not the heavy-evidence gate.
  const heavy = [ok('suites')]
  return permutations(entries).flatMap((p) => [
    [...p, ...others, ...heavy], [...others, ...p, ...heavy], [p[0], ...others, ...p.slice(1), ...heavy],
  ])
}

async function outcomeAcrossOrders({ name, entries, draft }) {
  const outcomes = []
  for (const nodes of arrangements(name, entries)) {
    const double = githubDouble({ pr: pullRequest({ draft, nodes }) })
    const { reconciler, bodies } = reconcilerOver(double)
    const { records, counters } = await reconciler.runReconcile({ references: [{ repository, number: 7 }] })
    assert.equal(counters.errors, 0, `repeats must not error: ${JSON.stringify(records[0])}`)
    assert.equal(counters.evaluated, 1)
    assert.equal(records.length, 1)
    outcomes.push({ actions: [...(records[0].actions ?? [])], body: bodies.at(-1) ?? null })
  }
  const [first, ...rest] = outcomes
  for (const other of rest) {
    assert.deepEqual(other.actions, first.actions, 'input order changed the readiness verdict')
    assert.equal(other.body, first.body, 'input order changed the readiness block')
  }
  return first
}

const rowsFor = (body, name) => (body ?? '').split('\n').filter((line) => line.startsWith(`| ${name} |`))

// --- 1. dedicated readiness reader: explicit null push time ------------------

test('getReadinessSnapshot accepts an explicit null push time and never invents one', async () => {
  const double = githubDouble({ pr: pullRequest({ pushedDate: null }) })
  const adapter = adapterOver(double)
  assert.equal(typeof adapter.getReadinessSnapshot, 'function', 'a dedicated readiness reader is required')
  const snapshot = await adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW })
  assert.equal(snapshot.repository, repository)
  assert.equal(snapshot.number, 7)
  assert.equal(snapshot.headSha, HEAD)
  assert.equal(snapshot.baseRef, 'main')
  assert.equal(snapshot.state, 'open')
  assert.equal(snapshot.draft, true)
  assert.equal(snapshot.merged, false)
  assert.equal(snapshot.autoMerge, false)
  assert.equal(snapshot.mergeableState, 'clean')
  assert.equal(snapshot.reviewDecision, null)
  assert.equal(snapshot.nowMs, NOW)
  assert.equal(snapshot.body, '## What Changed\n\n- a fix\n')
  assert.deepEqual(snapshot.labels, [])
  assert.deepEqual([...snapshot.checks.map((c) => c.name)].sort(), [...REQUIRED].sort())
  // A push time is either absent or an explicit null; never a stand-in.
  assert.ok(snapshot.headPushedMs === null || snapshot.headPushedMs === undefined,
    `push time must not be fabricated, got ${String(snapshot.headPushedMs)}`)
  assert.equal(double.snapshots.length, 1, 'one authoritative read, no retry')
})

test('a real push time passes through the readiness reader without error', async () => {
  const adapter = adapterOver(githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT }) }))
  const snapshot = await adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW })
  assert.equal(snapshot.headSha, HEAD)
  assert.equal(snapshot.checks.length, REQUIRED.length)
})

test('readiness reader keeps a null rollup as "no checks yet", not a failed read', async () => {
  const adapter = adapterOver(githubDouble({ pr: pullRequest({ rollup: null }) }))
  assert.deepEqual((await adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW })).checks, [])
})

test('readiness reader does not invent a push time from other timestamps', async () => {
  // The only timestamps in the payload are check completion times; the head
  // push time must not be borrowed from them, from the clock, or from 0.
  const double = githubDouble({ pr: pullRequest({ pushedDate: null }) })
  const snapshot = await adapterOver(double).getReadinessSnapshot(repository, 7, { nowMs: NOW })
  for (const forbidden of [PUSHED_MS, NOW, 0]) assert.notEqual(snapshot.headPushedMs, forbidden)
})

// --- 2. closeout stays strict ------------------------------------------------

test('getCloseoutSnapshot still rejects a missing push time (null and absent)', async () => {
  const absent = pullRequest()
  delete absent.commits.nodes[0].commit.pushedDate
  for (const pr of [pullRequest({ pushedDate: null }), absent, pullRequest({ pushedDate: 'not-a-date' })]) {
    const double = githubDouble({ pr })
    await assert.rejects(adapterOver(double).getCloseoutSnapshot(repository, 7, { nowMs: NOW }))
    assert.equal(double.snapshots.length, 1)
  }
})

test('getCloseoutSnapshot still rejects repeated check names, whatever their state', async () => {
  for (const nodes of [
    [ok('ci-ok'), ok('ci-ok')], [ok('ci-ok'), bad('ci-ok')], [ok('ci-ok'), running('ci-ok')],
    [ok('ci-ok'), ctxOk('ci-ok')], [ctxOk('ci-ok'), ctxBad('ci-ok')],
  ]) {
    const double = githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT, nodes }) })
    await assert.rejects(adapterOver(double).getCloseoutSnapshot(repository, 7, { nowMs: NOW }))
  }
})

test('each trigger is independently sufficient to fail closeout (fixtures exercise both causes)', async () => {
  // null push time with unique names
  await assert.rejects(adapterOver(githubDouble({ pr: pullRequest({ pushedDate: null }) }))
    .getCloseoutSnapshot(repository, 7, { nowMs: NOW }))
  // valid push time with repeated names
  await assert.rejects(adapterOver(githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT,
    nodes: [...baseChecks(), ok('ci-ok')] }) })).getCloseoutSnapshot(repository, 7, { nowMs: NOW }))
  // control: valid push time, unique names
  const control = await adapterOver(githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT }) }))
    .getCloseoutSnapshot(repository, 7, { nowMs: NOW })
  assert.equal(control.headPushedMs, PUSHED_MS)
})

test('closeout still rejects a clock before the head push (age-sensitive guard kept)', async () => {
  await assert.rejects(adapterOver(githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT }) }))
    .getCloseoutSnapshot(repository, 7, { nowMs: PUSHED_MS - 1 }))
})

// --- 3. readiness reader keeps every other guard -----------------------------

const guardCases = () => {
  const missing = (key) => { const pr = pullRequest(); delete pr[key]; return pr }
  return [
    ['PR number mismatch', pullRequest({ overrides: { number: 8 } })],
    ['PR url mismatch', pullRequest({ overrides: { url: 'https://example.com/7' } })],
    ['base repository mismatch', pullRequest({ overrides: { baseRepository: { nameWithOwner: 'Other/repo' } } })],
    ['head sha malformed', pullRequest({ overrides: { headRefOid: 'abc' } })],
    ['head branch missing', pullRequest({ overrides: { headRefName: null } })],
    ['lifecycle inconsistent (merged flag)', pullRequest({ overrides: { merged: true } })],
    ['lifecycle inconsistent (state)', pullRequest({ overrides: { state: 'MERGED' } })],
    ['lifecycle unknown', pullRequest({ overrides: { state: 'UNKNOWN' } })],
    ['draft flag not boolean', pullRequest({ overrides: { isDraft: null } })],
    ['review decision unknown', pullRequest({ overrides: { reviewDecision: 'unreviewed' } })],
    ['review decision dropped', missing('reviewDecision')],
    ['auto-merge flag dropped', missing('autoMergeRequest')],
    ['base ref missing', pullRequest({ overrides: { baseRefName: '' } })],
    ['merge state unknown', pullRequest({ overrides: { mergeStateStatus: 'BOGUS' } })],
    ['merge state dropped', missing('mergeStateStatus')],
    ['labels truncated', pullRequest({ overrides: { labels: { nodes: [], pageInfo: { hasNextPage: true } } } })],
    ['label entry malformed', pullRequest({ overrides: { labels: { nodes: [{ name: '' }], pageInfo: { hasNextPage: false } } } })],
    ['commits missing', pullRequest({ commits: { nodes: [] } })],
    ['head outside the snapshot window', pullRequest({ oid: OTHER_HEAD })],
    ['rollup truncated', pullRequest({ rollup: { contexts: { nodes: baseChecks(), pageInfo: { hasNextPage: true } } } })],
    ['rollup nodes not a list', pullRequest({ rollup: { contexts: { nodes: null, pageInfo: { hasNextPage: false } } } })],
    ['completed run without conclusion', pullRequest({ nodes: [checkRun('ci-ok', 'COMPLETED', null, PUSHED_AT)] })],
    ['completed run without completion time', pullRequest({ nodes: [checkRun('ci-ok', 'COMPLETED', 'SUCCESS', null)] })],
    ['run status unknown', pullRequest({ nodes: [checkRun('ci-ok', 'BOGUS')] })],
    ['run without a name', pullRequest({ nodes: [checkRun('', 'QUEUED')] })],
    ['status state unknown', pullRequest({ nodes: [statusCtx('ci-ok', 'BOGUS')] })],
    ['check entry of unknown type', pullRequest({ nodes: [{ __typename: 'Deployment', name: 'deploy' }] })],
    ['check entry not an object', pullRequest({ nodes: [null] })],
  ]
}

test('readiness reader still rejects every malformed, truncated or mismatched snapshot', async () => {
  for (const [label, pr] of guardCases()) {
    const double = githubDouble({ pr })
    const adapter = adapterOver(double)
    assert.equal(typeof adapter.getReadinessSnapshot, 'function', 'a dedicated readiness reader is required')
    await assert.rejects(adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW }), undefined, label)
    assert.equal(double.snapshots.length, 1, `${label}: one read, no retry`)
  }
})

test('readiness reader keeps the null-date case strict about everything else at the same time', async () => {
  // A null push time must not unlock a second malformation.
  for (const [label, pr] of guardCases()) {
    const nullDated = JSON.parse(JSON.stringify(pr))
    if (nullDated.commits?.nodes?.[0]?.commit) nullDated.commits.nodes[0].commit.pushedDate = null
    const adapter = adapterOver(githubDouble({ pr: nullDated }))
    assert.equal(typeof adapter.getReadinessSnapshot, 'function', 'a dedicated readiness reader is required')
    await assert.rejects(adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW }), undefined, `${label} (null push time)`)
  }
})

test('readiness reader rejects an invalid clock and transport or query errors', async () => {
  const adapter = adapterOver(githubDouble({ pr: pullRequest() }))
  assert.equal(typeof adapter.getReadinessSnapshot, 'function', 'a dedicated readiness reader is required')
  for (const nowMs of [Number.NaN, -1, 8.64e15 + 1, '5', 1.5, null]) {
    await assert.rejects(adapter.getReadinessSnapshot(repository, 7, { nowMs }), undefined, `clock ${String(nowMs)}`)
  }
  for (const number of [null, '7', 0, -1, 1.5, 2147483648]) {
    await assert.rejects(adapter.getReadinessSnapshot(repository, number, { nowMs: NOW }))
  }
  await assert.rejects(adapter.getReadinessSnapshot('Other/repo', 7, { nowMs: NOW }))
  const transport = adapterOver(githubDouble({ snapshotError: Object.assign(new Error(SENTINEL), { stderr: SENTINEL }) }))
  await assert.rejects(transport.getReadinessSnapshot(repository, 7, { nowMs: NOW }), (error) => {
    assert.equal(String(error?.message ?? error).includes(SENTINEL), false)
    return true
  })
  for (const response of [{ errors: [{ message: SENTINEL }], ...snapshotEnvelope(pullRequest()) },
    { data: { repository: null } }, {}]) {
    const failing = adapterOver(githubDouble({ snapshot: response }))
    await assert.rejects(failing.getReadinessSnapshot(repository, 7, { nowMs: NOW }), (error) => {
      assert.equal(String(error?.message ?? error).includes(SENTINEL), false)
      return true
    })
  }
})

test('readiness reader errors never echo untrusted names, titles, bodies or labels', async () => {
  const hostile = pullRequest({ nodes: [checkRun(`bad-${SENTINEL}`, 'COMPLETED', null, PUSHED_AT)],
    overrides: { title: SENTINEL, body: SENTINEL, headRefName: SENTINEL,
      labels: { nodes: [{ name: SENTINEL }], pageInfo: { hasNextPage: false } } } })
  const adapter = adapterOver(githubDouble({ pr: hostile }))
  assert.equal(typeof adapter.getReadinessSnapshot, 'function', 'a dedicated readiness reader is required')
  await assert.rejects(adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW }), (error) => {
    assert.equal(String(error?.message ?? error).includes(SENTINEL), false)
    return true
  })
})

test('every GraphQL document the readiness reader sends is brace- and paren-balanced', async () => {
  const double = githubDouble({ pr: pullRequest() })
  const adapter = adapterOver(double)
  assert.equal(typeof adapter.getReadinessSnapshot, 'function', 'a dedicated readiness reader is required')
  await adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW })
  assert.equal(double.snapshots.length, 1)
  const document = double.snapshots[0].find((arg) => arg.startsWith('query=')).slice('query='.length)
  for (const [open, close, label] of [['{', '}', 'brace'], ['(', ')', 'paren']]) {
    let depth = 0
    for (const char of document) {
      if (char === open) depth++
      if (char === close) { assert.ok(depth > 0, `closes a ${label} that was never opened`); depth-- }
    }
    assert.equal(depth, 0, `${depth} ${label}(s) left unclosed`)
  }
  // The reader must still ask for exactly what the guards need.
  for (const field of ['headRefOid', 'commits', 'statusCheckRollup', 'mergeStateStatus', 'autoMergeRequest',
    'baseRefName', 'labels', 'reviewDecision', 'isDraft', 'hasNextPage']) {
    assert.ok(document.includes(field), `readiness query lost ${field}`)
  }
})

// --- 4. the reconciler is wired to the readiness reader ----------------------

test('the reconciler reads through getReadinessSnapshot with the pass clock, never the closeout reader', async () => {
  const double = githubDouble({ pr: pullRequest({ pushedDate: null }) })
  const closeoutCalls = []
  const { reconciler } = reconcilerOver(double, { closeoutCalls })
  const { records, counters } = await reconciler.runReconcile({ references: [{ repository, number: 7 }] })
  assert.deepEqual(closeoutCalls, [], 'readiness must not call the strict closeout reader')
  assert.equal(counters.errors, 0, JSON.stringify(records))
  assert.equal(counters.evaluated, 1)
  assert.equal(counters.acted, 1)
  assert.deepEqual(records[0].actions, ['update-body', 'mark-ready'])
  assert.equal(double.snapshots.length, 1)
})

test('the reconciler builds without a closeout reader (it depends on the readiness reader only)', () => {
  const unused = async () => { throw new Error('unused') }
  assert.doesNotThrow(() => createReconciler({
    github: { getReadinessSnapshot: unused, getRulesetRequiredChecks: unused, updatePullRequestBody: unused,
      markReadyForReview: unused, convertToDraft: unused, enableAutoMerge: unused },
    policy: { enabledRepositories: [], holdLabels: ['hold'] } }))
  assert.throws(() => createReconciler({
    github: { getRulesetRequiredChecks: unused, updatePullRequestBody: unused,
      markReadyForReview: unused, convertToDraft: unused, enableAutoMerge: unused },
    policy: { enabledRepositories: [], holdLabels: ['hold'] } }))
})

// --- 5. repeated names collapse conservatively, independent of order ---------

const DRAFT_CASES = [
  // [label, name, entries, mayMarkReady]
  ['check runs, all passing', 'ci-ok', [ok('ci-ok'), ok('ci-ok')], true],
  ['three passing check runs', 'ci-ok', [ok('ci-ok'), ok('ci-ok'), ok('ci-ok')], true],
  ['check run and status context both passing', 'ci-ok', [ok('ci-ok'), ctxOk('ci-ok')], true],
  ['status contexts all passing', 'secret-scan', [ctxOk('secret-scan'), ctxOk('secret-scan')], true],
  ['passing with a skipped duplicate', 'ci-ok', [ok('ci-ok'), skipped('ci-ok')], true],
  ['failure hidden behind a passing duplicate', 'ci-ok', [ok('ci-ok'), bad('ci-ok')], false],
  ['failure among three', 'ci-ok', [ok('ci-ok'), ok('ci-ok'), bad('ci-ok')], false],
  ['pending hidden behind a passing duplicate', 'ci-ok', [ok('ci-ok'), running('ci-ok')], false],
  ['queued hidden behind a passing duplicate', 'secret-scan', [ok('secret-scan'), queued('secret-scan')], false],
  ['failing status context beside a passing run', 'ci-ok', [ok('ci-ok'), ctxBad('ci-ok')], false],
  ['pending status context beside a passing run', 'ci-ok', [ok('ci-ok'), ctxPending('ci-ok')], false],
  ['expected status context beside a passing status', 'ci-ok', [ctxOk('ci-ok'), ctxExpected('ci-ok')], false],
  ['failure beside pending', 'ci-ok', [bad('ci-ok'), running('ci-ok')], false],
  ['failing then passing status contexts', 'secret-scan', [ctxBad('secret-scan'), ctxOk('secret-scan')], false],
]

for (const [label, name, entries, mayMarkReady] of DRAFT_CASES) {
  test(`draft PR, repeated required check (${label}): ready gate follows the strictest entry in every order`, async () => {
    const outcome = await outcomeAcrossOrders({ name, entries, draft: true })
    assert.equal(outcome.actions.includes('mark-ready'), mayMarkReady, JSON.stringify(outcome.actions))
    assert.ok(outcome.actions.includes('update-body'), 'the block is still maintained')
    assert.ok(!outcome.actions.includes('enable-automerge'))
    assert.ok(!outcome.actions.includes('convert-to-draft'))
    // The block lists the repeated name once, at its strictest state.
    const rows = rowsFor(outcome.body, name)
    assert.equal(rows.length, 1, `one row for ${name}, got ${JSON.stringify(rows)}`)
    const passing = entries.every((e) => ['SUCCESS', 'SKIPPED'].includes(e.conclusion ?? e.state))
    assert.equal(rows[0].includes('✅'), passing, rows[0])
    if (entries.some((e) => e.conclusion === 'FAILURE' || e.state === 'FAILURE')) {
      assert.ok(rows[0].includes('❌'), `failure must stay visible: ${rows[0]}`)
    }
  })
}

test('draft PR: a repeated name that is not required cannot block the ready gate', async () => {
  const outcome = await (async () => {
    const results = []
    for (const nodes of permutations([ok('lint'), bad('lint')]).map((p) => [...baseChecks(), ...p])) {
      const { reconciler } = reconcilerOver(githubDouble({ pr: pullRequest({ nodes }) }))
      const { records, counters } = await reconciler.runReconcile({ references: [{ repository, number: 7 }] })
      assert.equal(counters.errors, 0, JSON.stringify(records))
      results.push(records[0].actions)
    }
    return results
  })()
  for (const actions of outcome) assert.ok(actions.includes('mark-ready'), JSON.stringify(actions))
})

const READY_CASES = [
  ['required check: all entries pass', 'ci-ok', [ok('ci-ok'), ok('ci-ok')], { automerge: true, draft: false }],
  ['required check: run and status pass', 'ci-ok', [ok('ci-ok'), ctxOk('ci-ok')], { automerge: true, draft: false }],
  ['required check: completed failure behind a pass', 'ci-ok', [ok('ci-ok'), bad('ci-ok')], { automerge: false, draft: true }],
  ['required check: failing status behind a pass', 'ci-ok', [ok('ci-ok'), ctxBad('ci-ok')], { automerge: false, draft: true }],
  ['required check: failing status context pair', 'secret-scan', [ctxOk('secret-scan'), ctxBad('secret-scan')], { automerge: false, draft: true }],
  ['required check: pending behind a pass is not a failure', 'ci-ok', [ok('ci-ok'), running('ci-ok')], { automerge: false, draft: false }],
  ['required check: queued behind a pass is not a failure', 'ci-ok', [ok('ci-ok'), queued('ci-ok')], { automerge: false, draft: false }],
  ['review check: all entries pass', REVIEW, [ok(REVIEW), ok(REVIEW)], { automerge: true, draft: false }],
  ['review check: run and status pass', REVIEW, [ok(REVIEW), ctxOk(REVIEW)], { automerge: true, draft: false }],
  ['review check: failure behind a pass', REVIEW, [ok(REVIEW), bad(REVIEW)], { automerge: false }],
  ['review check: failing status behind a pass', REVIEW, [ctxOk(REVIEW), ctxBad(REVIEW)], { automerge: false }],
  ['review check: pending behind a pass', REVIEW, [ok(REVIEW), running(REVIEW)], { automerge: false, draft: false }],
  ['review check: pending status behind a pass', REVIEW, [ok(REVIEW), ctxPending(REVIEW)], { automerge: false, draft: false }],
]

for (const [label, name, entries, expected] of READY_CASES) {
  test(`ready PR, repeated ${label}: arm and draft-reset follow the strictest entry in every order`, async () => {
    const outcome = await outcomeAcrossOrders({ name, entries, draft: false })
    assert.equal(outcome.actions.includes('enable-automerge'), expected.automerge, JSON.stringify(outcome.actions))
    if (expected.draft !== undefined) {
      assert.equal(outcome.actions.includes('convert-to-draft'), expected.draft, JSON.stringify(outcome.actions))
    }
    assert.ok(!outcome.actions.includes('mark-ready'))
    if (name === REVIEW) {
      // The score line is display only but must never claim 5/5 over a
      // failing or pending duplicate of the review check.
      const passing = entries.every((e) => ['SUCCESS', 'SKIPPED'].includes(e.conclusion ?? e.state))
      assert.equal(outcome.body.includes(`${REVIEW}: 5/5 ✅`), passing, outcome.body)
    } else {
      assert.equal(rowsFor(outcome.body, name).length, 1)
    }
  })
}

test('a failure cannot be hidden by a passing duplicate in either position (explicit pair, both orders)', async () => {
  for (const nodes of [[bad('ci-ok'), ok('ci-ok')], [ok('ci-ok'), bad('ci-ok')]]) {
    const others = baseChecks().filter((n) => n.name !== 'ci-ok')
    const { reconciler, transitions } = reconcilerOver(githubDouble({
      pr: pullRequest({ draft: false, nodes: [...nodes, ...others] }) }))
    const { records } = await reconciler.runReconcile({ references: [{ repository, number: 7 }] })
    assert.ok(records[0].actions.includes('convert-to-draft'), JSON.stringify(records[0].actions))
    assert.deepEqual(transitions, ['convert-to-draft'])
  }
})

// --- 6. production sweep wiring: flag-off, zero writes -----------------------

test('runSweep: null push time plus repeated passing names evaluates flag-off with zero writes', async (t) => {
  const nodes = [...baseChecks(), ok('ci-ok'), ctxOk('secret-scan'), ok('ci-ok'), ok(REVIEW)]
  const double = githubDouble({ pr: pullRequest({ pushedDate: null, nodes }) })
  const result = await sweep(t, double)
  assert.equal(result.listErrors, 0)
  assert.equal(result.counters.total, 1)
  assert.equal(result.counters.evaluated, 1)
  assert.equal(result.counters.errors, 0, JSON.stringify(result.records))
  assert.equal(result.counters.wouldAct, 1)
  assert.equal(result.counters.acted, 0)
  for (const key of ['updatedBody', 'markedReady', 'convertedToDraft', 'enabledAutomerge']) {
    assert.equal(result.counters[key], 0, key)
  }
  assert.equal(result.records[0].decision, 'would-act')
  assert.deepEqual(result.records[0].actions, ['update-body', 'mark-ready'])
  assert.equal(result.records[0].headSha, HEAD)
  assert.deepEqual(double.writes, [], `flag-off pass wrote: ${JSON.stringify(double.writes)}`)
  assert.equal(double.snapshots.length, 1)
})

test('runSweep: null push time plus repeated passing names under a read-only pass on an enabled repo writes nothing', async (t) => {
  const nodes = [...baseChecks(), ok('ci-ok'), ctxOk('secret-scan')]
  const double = githubDouble({ pr: pullRequest({ pushedDate: null, nodes }) })
  const result = await sweep(t, double, { enabled: [repository], env: { ...JOB_ENV, READINESS_READ_ONLY: '1' } })
  assert.equal(result.counters.errors, 0, JSON.stringify(result.records))
  assert.equal(result.counters.wouldAct, 1)
  assert.equal(result.counters.acted, 0)
  assert.deepEqual(double.writes, [])
})

test('runSweep: repeated names with a hidden failure evaluate (not error) and still never pass the gate', async (t) => {
  const nodes = [...baseChecks(), bad('ci-ok')]
  const double = githubDouble({ pr: pullRequest({ pushedDate: null, nodes }) })
  const result = await sweep(t, double)
  assert.equal(result.counters.errors, 0, JSON.stringify(result.records))
  assert.equal(result.counters.evaluated, 1)
  assert.ok(!(result.records[0].actions ?? []).includes('mark-ready'))
  assert.deepEqual(double.writes, [])
})

// --- 7. failures stay counted errors: no mutation, no retry ------------------

const FAILURES = [
  ['transport failure', { snapshotError: Object.assign(new Error(SENTINEL), { stdout: SENTINEL, stderr: SENTINEL, code: 1 }) }],
  ['permission failure (GraphQL errors)', { snapshot: { errors: [{ type: 'FORBIDDEN', message: SENTINEL }], ...snapshotEnvelope(pullRequest()) } }],
  ['head outside the snapshot window', { pr: pullRequest({ oid: OTHER_HEAD }) }],
  ['truncated rollup', { pr: pullRequest({ rollup: { contexts: { nodes: baseChecks(), pageInfo: { hasNextPage: true } } } }) }],
  ['malformed check', { pr: pullRequest({ nodes: [checkRun(`bad-${SENTINEL}`, 'COMPLETED', null, PUSHED_AT)] }) }],
  ['identity mismatch', { pr: pullRequest({ overrides: { baseRepository: { nameWithOwner: 'Other/repo' } } }) }],
  ['lifecycle inconsistency', { pr: pullRequest({ overrides: { merged: true } }) }],
  ['truncated labels', { pr: pullRequest({ overrides: { labels: { nodes: [], pageInfo: { hasNextPage: true } } } }) }],
]

for (const [label, options] of FAILURES) {
  for (const enabled of [[], [repository]]) {
    test(`runSweep: ${label} with a null push time is a counted error with no mutation and no retry (${enabled.length ? 'flag-on' : 'flag-off'})`, async (t) => {
      const double = githubDouble(options)
      // Fixture detail: the failing snapshot also carries a null push time.
      const result = await sweep(t, double, { enabled })
      assert.equal(result.counters.errors, 1, JSON.stringify(result.records))
      assert.equal(result.counters.evaluated, 0)
      assert.equal(result.counters.acted + result.counters.wouldAct, 0)
      assert.deepEqual(double.writes, [])
      assert.equal(double.snapshots.length, 1, 'no retry of the snapshot read')
      assert.ok(!double.calls.some((args) => args.some((a) => typeof a === 'string' && a.includes('/rules/branches/'))),
        'no ruleset read after a failed snapshot')
      assert.equal(result.records.length, 1)
      assert.ok(result.records[0].error, 'the record carries a fixed error code')
      assert.ok(!observable(result).includes(SENTINEL), 'untrusted text must not surface')
    })
  }
}

// --- 8. job-observable facts: null push time, repeated names, failure class --

async function observe(t, pr) {
  const double = githubDouble({ pr })
  const result = await sweep(t, double)
  assert.equal(result.counters.errors, 0, JSON.stringify(result.records))
  assert.equal(result.counters.evaluated, 1)
  assert.deepEqual(double.writes, [])
  return observable(result)
}

test('the sweep exposes distinct facts for a null push time, for repeated names and for their counts', async (t) => {
  const dupOne = [...baseChecks(), ok('ci-ok')]
  const dupTwo = [...baseChecks(), ok('ci-ok'), ok('secret-scan'), ok('secret-scan')]
  const seen = {
    control: await observe(t, pullRequest({ pushedDate: PUSHED_AT })),
    nullDate: await observe(t, pullRequest({ pushedDate: null })),
    repeatedOne: await observe(t, pullRequest({ pushedDate: PUSHED_AT, nodes: dupOne })),
    repeatedTwo: await observe(t, pullRequest({ pushedDate: PUSHED_AT, nodes: dupTwo })),
    both: await observe(t, pullRequest({ pushedDate: null, nodes: dupOne })),
  }
  const labels = Object.keys(seen)
  for (let i = 0; i < labels.length; i++) {
    for (let j = i + 1; j < labels.length; j++) {
      assert.notEqual(seen[labels[i]], seen[labels[j]],
        `a pass over "${labels[i]}" is indistinguishable from "${labels[j]}" in job output`)
    }
  }
})

test('job-output facts do not depend on check input order', async (t) => {
  const a = await observe(t, pullRequest({ pushedDate: null, nodes: [...baseChecks(), ok('ci-ok')] }))
  const b = await observe(t, pullRequest({ pushedDate: null, nodes: [ok('ci-ok'), ...baseChecks()] }))
  assert.equal(a, b, 'facts must not depend on input order')
})

test('failure classes are distinguishable in job output without exposing raw text', async (t) => {
  const classes = {
    transport: { snapshotError: Object.assign(new Error(SENTINEL), { stderr: SENTINEL }) },
    headOutsideWindow: { pr: pullRequest({ oid: OTHER_HEAD }) },
    truncatedRollup: { pr: pullRequest({ rollup: { contexts: { nodes: baseChecks(), pageInfo: { hasNextPage: true } } } }) },
    malformedCheck: { pr: pullRequest({ nodes: [checkRun(`bad-${SENTINEL}`, 'COMPLETED', null, PUSHED_AT)] }) },
  }
  const seen = {}
  for (const [label, options] of Object.entries(classes)) {
    const double = githubDouble(options)
    const result = await sweep(t, double)
    assert.equal(result.counters.errors, 1, label)
    assert.deepEqual(double.writes, [], label)
    seen[label] = observable(result)
    assert.ok(!seen[label].includes(SENTINEL), `${label} leaked untrusted text`)
  }
  const labels = Object.keys(seen)
  for (let i = 0; i < labels.length; i++) {
    for (let j = i + 1; j < labels.length; j++) {
      assert.notEqual(seen[labels[i]], seen[labels[j]],
        `failure "${labels[i]}" is indistinguishable from "${labels[j]}" in job output`)
    }
  }
})

test('nothing the sweep emits carries untrusted GitHub text (records, lines, counters, console)', async (t) => {
  const captured = []
  for (const method of ['log', 'error', 'warn', 'info', 'debug']) {
    t.mock.method(console, method, (...args) => { captured.push(args.map(String).join(' ')) })
  }
  const hostile = pullRequest({ pushedDate: null,
    nodes: [ok('ci-ok'), ok('ci-ok'), ok('secret-scan'), ok(REVIEW), ok(`Dup_${SENTINEL}`), bad(`Dup_${SENTINEL}`)],
    overrides: { title: `title ${SENTINEL}`, body: `body ${SENTINEL}`, headRefName: `branch-${SENTINEL}`,
      author: { __typename: 'User', login: `author-${SENTINEL}` },
      labels: { nodes: [{ name: `label-${SENTINEL}` }], pageInfo: { hasNextPage: false } } } })
  const double = githubDouble({ pr: hostile })
  const result = await sweep(t, double)
  assert.equal(result.counters.errors, 0, JSON.stringify(result.records))
  assert.ok(!observable(result).includes(SENTINEL), 'untrusted text surfaced in sweep output')
  assert.ok(!captured.join('\n').includes(SENTINEL), 'untrusted text surfaced on the console')
  assert.ok(!captured.join('\n').includes(TOKEN), 'the job token surfaced on the console')
})
