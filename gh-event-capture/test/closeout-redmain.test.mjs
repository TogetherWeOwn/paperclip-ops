import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createGithubAdapter } from '../src/github-adapter.js'
import { decideDisarm, disarmPolicy } from '../src/closeout-disarm.js'
import {
  DEFAULT_CI_OWNER, mainRedIncidentKey, redmainPolicy, resolveCiOwner,
  decideMainRed, renderRedmainIncident, renderRedmainRecovery,
  createRedmainWatcher, applySuppression,
} from '../src/closeout-redmain.js'
import {
  freshRedmainState, checkRedmainState, redmainNamespace, redmainKey,
  findOpenIncident, applyOpen, applyRecovery, createMemoryRedmainStore,
} from '../src/redmain-store.js'
import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  loadRedmainState, saveRedmainState, createFileRedmainStore,
} from '../src/redmain-store.js'

const repo = 'TogetherWeOwn/kofra'
const other = 'TogetherWeOwn/routeware'
const sha = (c) => c.repeat(40)
const REQUIRED = ['check', 'gitleaks', 'pr-lint']
const policy = redmainPolicy()
const on = redmainPolicy({ enabledRepositories: [repo] })
const onBoth = redmainPolicy({ enabledRepositories: [repo, other] })

function head(over = {}) {
  return { repository: repo, branch: 'main', mainSha: sha('a'), requiredChecks: [...REQUIRED], checks: [], ...over }
}
const pass = (name) => ({ name, status: 'completed', conclusion: 'success', completedMs: 100 })
const fail = (name, conclusion = 'failure') => ({ name, status: 'completed', conclusion, completedMs: 100 })
const running = (name) => ({ name, status: 'in_progress' })
const greenHead = (over = {}) => head({ checks: REQUIRED.map(pass), ...over })
const redHead = (over = {}) =>
  head({ checks: [fail('check'), pass('gitleaks'), pass('pr-lint')], ...over })

// Injected GitHub double: implements EXACTLY the one red-main read. There is
// no board, capture or claim method to call, so the watcher cannot post.
function doubles(heads, { failReads = [] } = {}) {
  const calls = []
  const github = {
    async getMainHeadChecks(repository) {
      calls.push(['read-main-head', repository])
      if (failReads.includes(repository)) throw new Error('GitHub query failed: transport, timeout, output limit or invalid JSON')
      const found = heads.get(repository)
      if (!found) throw new Error('GitHub query failed: transport, timeout, output limit or invalid JSON')
      return found
    },
  }
  return { github, calls }
}

test('red main with none open opens exactly one incident with the red SHA', async () => {
  const { github, calls } = doubles(new Map([[repo, redHead()]]))
  const incidents = createMemoryRedmainStore()
  const { records, counters } = await createRedmainWatcher({ github, incidents, policy: on })
    .runRedmain({ repositories: [repo], nowMs: 1000 })
  assert.deepEqual(counters, { total: 1, evaluated: 1, red: 1, green: 0, pending: 0,
    opened: 1, wouldOpen: 0, alreadyOpen: 0, recovered: 0, wouldRecover: 0, noOp: 0, errors: 0 })
  assert.equal(records[0].decision, 'opened')
  assert.equal(records[0].key, mainRedIncidentKey(repo))
  assert.equal(records[0].key, 'main-red:TogetherWeOwn/kofra')
  assert.match(records[0].log, new RegExp(`^closeout-redmain ${repo}@${sha('a')}: opened \\(first-red:${sha('a')}\\)$`))
  assert.match(records[0].body, new RegExp(`Red main incident main-red:${repo}.*${sha('a')}.*devops`))
  const open = await incidents.findOpen(repo)
  assert.equal(open.firstRedSha, sha('a'))
  assert.equal(open.ciOwner, 'devops')
  assert.equal(open.status, 'open')
  assert.deepEqual(calls, [['read-main-head', repo]])
})

// M1 duplicate-incident: a second red SHA while one is open must add nothing.
test('second red SHA while open adds no incident and keeps the first SHA', async () => {
  const { github } = doubles(new Map([[repo, redHead()]]))
  const incidents = createMemoryRedmainStore()
  const watcher = createRedmainWatcher({ github, incidents, policy: on })
  await watcher.runRedmain({ repositories: [repo], nowMs: 1000 })
  const { github: github2 } = doubles(new Map([[repo, redHead({ mainSha: sha('b') })]]))
  const watcher2 = createRedmainWatcher({ github: github2, incidents, policy: on })
  const { records, counters } = await watcher2.runRedmain({ repositories: [repo], nowMs: 2000 })
  assert.equal(records[0].decision, 'already-open')
  assert.match(records[0].log, new RegExp(`already-open \\(open-since:${sha('a')}\\)`))
  assert.equal(counters.alreadyOpen, 1)
  assert.equal(counters.opened, 0)
  assert.equal(incidents.state.incidents.length, 1)
  assert.equal((await incidents.findOpen(repo)).firstRedSha, sha('a'))
})

// M2 no-recovery: green with an open incident must recover exactly once.
test('green with an open incident posts one recovery and closes it', async () => {
  const { github } = doubles(new Map([[repo, redHead()]]))
  const incidents = createMemoryRedmainStore()
  await createRedmainWatcher({ github, incidents, policy: on }).runRedmain({ repositories: [repo], nowMs: 1000 })
  const { github: github2 } = doubles(new Map([[repo, greenHead({ mainSha: sha('c') })]]))
  const { records, counters } = await createRedmainWatcher({ github: github2, incidents, policy: on })
    .runRedmain({ repositories: [repo], nowMs: 2000 })
  assert.equal(records[0].decision, 'recovered')
  assert.match(records[0].body, new RegExp(`recovered.*${sha('c')}.*${sha('a')}`))
  assert.equal(counters.recovered, 1)
  assert.equal(await incidents.findOpen(repo), null)
  // Re-red after recovery opens a FRESH incident with the new SHA.
  const { github: github3 } = doubles(new Map([[repo, redHead({ mainSha: sha('d') })]]))
  const again = await createRedmainWatcher({ github: github3, incidents, policy: on })
    .runRedmain({ repositories: [repo], nowMs: 3000 })
  assert.equal(again.records[0].decision, 'opened')
  assert.equal((await incidents.findOpen(repo)).firstRedSha, sha('d'))
  assert.equal(incidents.state.incidents.length, 2)
})

test('green with none open and pending heads are silent no-ops', async () => {
  const pendingRunning = head({ repository: other, branch: 'main', mainSha: sha('9'),
    checks: [running('check'), pass('gitleaks'), pass('pr-lint')] })
  const pendingMissing = head({ checks: [pass('check'), pass('gitleaks')] })
  const { github } = doubles(new Map([
    [repo, greenHead()],
    [other, pendingRunning],
  ]))
  const incidents = createMemoryRedmainStore()
  const watcher = createRedmainWatcher({ github, incidents,
    policy: onBoth })
  const { records, counters } = await watcher.runRedmain({ repositories: [repo, other], nowMs: 1000 })
  assert.deepEqual(records.map((r) => [r.repository, r.decision, r.reason]), [
    [repo, 'none', 'main-green-no-incident'],
    [other, 'none', 'main-pending'],
  ])
  assert.deepEqual([counters.green, counters.pending, counters.noOp, counters.opened], [1, 1, 1, 0])
  assert.equal(incidents.state.incidents.length, 0)
  // Pending never recovers an open incident either.
  const { github: red } = doubles(new Map([[repo, redHead()]]))
  await createRedmainWatcher({ github: red, incidents,
    policy: on })
    .runRedmain({ repositories: [repo], nowMs: 2000 })
  const { github: still } = doubles(new Map([[repo, pendingMissing]]))
  const { records: rec2 } = await createRedmainWatcher({ github: still, incidents,
    policy: on })
    .runRedmain({ repositories: [repo], nowMs: 3000 })
  assert.equal(rec2[0].decision, 'none')
  assert.equal(rec2[0].reason, 'main-pending')
  assert.notEqual(await incidents.findOpen(repo), null)
})

// M4 flag-off-writes: flag off must persist nothing and touch no writer.
test('flag off logs would-open/would-recover and writes no state', async () => {
  const { github } = doubles(new Map([[repo, redHead()]]))
  const incidents = createMemoryRedmainStore()
  const watcher = createRedmainWatcher({ github, incidents, policy })
  const { records, counters } = await watcher.runRedmain({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'would-open')
  assert.equal(records[0].reason, 'flag-off-shadow')
  assert.match(records[0].log, /would-open \(flag-off-shadow\)/)
  assert.equal(counters.wouldOpen, 1)
  assert.equal(counters.opened, 0)
  assert.equal(incidents.state.incidents.length, 0)
  assert.equal(await incidents.findOpen(repo), null)
  // A would-be recovery is the same: logged, counted, unwritten.
  await incidents.openIncident({ key: mainRedIncidentKey(repo), repository: repo,
    firstRedSha: sha('a'), openedMs: 500, ciOwner: 'devops', body: 'seeded' })
  const { github: github2 } = doubles(new Map([[repo, greenHead({ mainSha: sha('c') })]]))
  const { records: rec2, counters: cnt2 } = await createRedmainWatcher({ github: github2, incidents, policy })
    .runRedmain({ repositories: [repo], nowMs: 2000 })
  assert.equal(rec2[0].decision, 'would-recover')
  assert.equal(cnt2.wouldRecover, 1)
  assert.notEqual(await incidents.findOpen(repo), null)
})

test('exception decisions for a suppressed repo are suppressed and counted, never woken', async () => {
  const { github } = doubles(new Map([[repo, redHead()]]))
  const incidents = createMemoryRedmainStore()
  await createRedmainWatcher({ github, incidents, policy: on }).runRedmain({ repositories: [repo], nowMs: 1000 })
  const isSuppressed = (repository) =>
    repository === repo && enabledFor(repository)
  const enabledSet = new Set(on.enabledRepositories)
  function enabledFor(repository) { return enabledSet.has(repository) }
  const decisions = [
    { repository: repo, number: 7, headSha: sha('e'), class: 'required_check_failed' },
    { repository: repo, number: 8, headSha: sha('f'), class: 'merged' },
    { repository: repo, number: 9, headSha: sha('1'), class: 'closed_unmerged' },
    { repository: other, number: 1, headSha: sha('2'), class: 'conflict' },
  ]
  const openOf = async (r) => (await incidents.findOpen(r)) !== null
  const syncSuppressed = (r) => r === repo
  assert.equal(await openOf(repo), true)
  assert.equal(await openOf(other), false)
  const { records, counters } = applySuppression(decisions, (r) => syncSuppressed(r) && enabledFor(r))
  assert.deepEqual(counters, { total: 4, suppressed: 3, passed: 1,
    byClass: { required_check_failed: 1, merged: 1, closed_unmerged: 1 } })
  assert.deepEqual(records.map((r) => r.suppressed), [true, true, true, false])
  assert.match(records[0].log, new RegExp(`suppressed \\(required_check_failed\\)$`))
  assert.ok(isSuppressed(repo))
  // applySuppression takes no board/capture/claim dependency: suppression is a
  // record, never a wake. There is no wake call to assert on because there is
  // no wake parameter to pass.
})

test('suppression consults the flag: removing the repo unsuppresses immediately', () => {
  const decisions = [{ repository: repo, number: 7, headSha: sha('e'), class: 'stalled' }]
  const { counters: off } = applySuppression(decisions, () => false)
  assert.deepEqual([off.suppressed, off.passed], [0, 1])
  const { counters: flaggedOff } = applySuppression(decisions, (r) => r === other)
  assert.deepEqual([flaggedOff.suppressed, flaggedOff.passed], [0, 1])
})

// M3 suppression-blocks-disarm: the disarm duty never consults suppression.
test('disarm still fires for an armed held PR while the repo incident is open', async () => {
  const { github } = doubles(new Map([[repo, redHead()]]))
  const incidents = createMemoryRedmainStore()
  await createRedmainWatcher({ github, incidents, policy: on }).runRedmain({ repositories: [repo], nowMs: 1000 })
  assert.notEqual(await incidents.findOpen(repo), null)
  const verdict = decideDisarm({ repository: repo, number: 7, headSha: sha('e'),
    state: 'open', draft: false, autoMerge: true, labels: ['hold'] }, disarmPolicy())
  assert.notEqual(verdict, null)
  assert.equal(verdict.held, true)
})

test('routing defaults to devops and stays constant per repo', () => {
  assert.equal(DEFAULT_CI_OWNER, 'devops')
  assert.equal(resolveCiOwner(policy, repo), 'devops')
  assert.equal(resolveCiOwner(policy, other), 'devops')
  const custom = redmainPolicy({ ciOwners: { [repo]: 'founding-engineer' } })
  assert.equal(resolveCiOwner(custom, repo), 'founding-engineer')
  assert.equal(resolveCiOwner(custom, other), 'devops')
})

test('decideMainRed reads only required checks: red, timeout and cancelled count', () => {
  for (const conclusion of ['failure', 'timed_out', 'cancelled']) {
    const verdict = decideMainRed(head({ checks: [fail('check', conclusion), pass('gitleaks'), pass('pr-lint')] }), policy)
    assert.equal(verdict.state, 'red')
    assert.deepEqual(verdict.failedChecks, ['check'])
  }
  // Extra failing non-required checks do not redden main.
  const extra = decideMainRed(head({ checks: [...REQUIRED.map(pass), fail('flaky-optional')] }), policy)
  assert.equal(extra.state, 'green')
  // Unknown conclusions are pending, never green and never red.
  const weird = decideMainRed(head({ checks: [pass('check'), pass('gitleaks'),
    { name: 'pr-lint', status: 'completed', conclusion: 'mystery', completedMs: 100 }] }), policy)
  assert.equal(weird.state, 'pending')
  assert.throws(() => decideMainRed(head({ requiredChecks: ['bad name!'], checks: [pass('check')] }), policy))
  assert.throws(() => decideMainRed(head({ requiredChecks: [], checks: [pass('check')] }), policy))
})

test('rendered texts carry only fixed words, the key, SHAs and safe names', () => {
  const body = renderRedmainIncident({ repository: repo, mainSha: sha('a'),
    failedChecks: ['check', 'evil`; rm -rf'], ciOwner: 'devops' })
  assert.ok(!body.includes('evil'))
  assert.match(body, new RegExp(`main-red:${repo}.*${sha('a')}.*\`check\`.*devops`))
  const recovery = renderRedmainRecovery({ repository: repo, firstRedSha: sha('a'),
    recoveredSha: sha('c'), ciOwner: 'devops' })
  assert.match(recovery, new RegExp(`main-red:${repo}.*${sha('c')}.*${sha('a')}`))
})

test('one unreadable repo is a counted error that never blinds the rest', async () => {
  const { github } = doubles(new Map([[other, greenHead({ repository: other, branch: 'main' })]]),
    { failReads: [repo] })
  const incidents = createMemoryRedmainStore()
  const watcher = createRedmainWatcher({ github, incidents,
    policy: onBoth })
  const { records, counters } = await watcher.runRedmain({ repositories: [repo, other], nowMs: 1000 })
  assert.deepEqual(records.map((r) => [r.repository, r.decision, r.reason]), [
    [repo, 'none', 'main-head-read-failed'],
    [other, 'none', 'main-green-no-incident'],
  ])
  assert.equal(counters.errors, 1)
})

test('store: double open throws, recovery needs an open incident, repos are independent', async () => {
  const incidents = createMemoryRedmainStore()
  assert.equal(await incidents.findOpen(repo), null)
  await incidents.openIncident({ key: mainRedIncidentKey(repo), repository: repo,
    firstRedSha: sha('a'), openedMs: 1000, ciOwner: 'devops', body: 'first' })
  await assert.rejects(incidents.openIncident({ key: mainRedIncidentKey(repo), repository: repo,
    firstRedSha: sha('b'), openedMs: 2000, ciOwner: 'devops', body: 'dup' }), /already open/)
  await assert.rejects(incidents.closeIncident(mainRedIncidentKey(other),
    { recoveredSha: sha('c'), recoveredMs: 3000, recoveryBody: 'x' }), /not open/)
  await incidents.openIncident({ key: mainRedIncidentKey(other), repository: other,
    firstRedSha: sha('b'), openedMs: 1500, ciOwner: 'devops', body: 'other' })
  await incidents.closeIncident(mainRedIncidentKey(repo),
    { recoveredSha: sha('c'), recoveredMs: 3000, recoveryBody: 'recovered' })
  assert.equal(await incidents.findOpen(repo), null)
  assert.notEqual(await incidents.findOpen(other), null)
  const state = incidents.state
  checkRedmainState(state)
  const digest = createHash('sha256').update('x'.repeat(1)).digest('hex')
  assert.equal(redmainNamespace(digest).length, 64)
  assert.equal(redmainKey(repo), `main-red:${repo}`)
  assert.throws(() => applyOpen(freshRedmainState(redmainNamespace(digest)),
    { repository: 'bad repo!', firstRedSha: sha('a'), openedMs: 1, ciOwner: 'devops', body: 'x' }))
})

test('store: recovered history survives but never suppresses; open entries are never evicted', () => {
  const digest = createHash('sha256').update('y').digest('hex')
  const ns = redmainNamespace(digest)
  const state = freshRedmainState(ns)
  applyOpen(state, { repository: repo, firstRedSha: sha('a'), openedMs: 1, ciOwner: 'devops', body: 'one' }, 2)
  applyRecovery(state, repo, { recoveredSha: sha('c'), recoveredMs: 2, recoveryBody: 'back' })
  applyOpen(state, { repository: other, firstRedSha: sha('b'), openedMs: 3, ciOwner: 'devops', body: 'two' }, 2)
  assert.equal(findOpenIncident(state, repo), null)
  assert.equal(state.incidents.length, 2)
  // Budget of 1 with two opens must refuse rather than evict an open incident.
  const tight = freshRedmainState(ns)
  applyOpen(tight, { repository: repo, firstRedSha: sha('a'), openedMs: 1, ciOwner: 'devops', body: 'one' }, 1)
  assert.throws(() => applyOpen(tight, { repository: other, firstRedSha: sha('b'),
    openedMs: 2, ciOwner: 'devops', body: 'two' }, 1), /open incidents/)
})

test('store honors a configured budget above the default on every call', async () => {
  const digest = createHash('sha256').update('big').digest('hex')
  const ns = redmainNamespace(digest)
  const state = freshRedmainState(ns)
  for (let i = 0; i < 1001; i++) {
    applyOpen(state, { repository: `TogetherWeOwn/repo${i}`, firstRedSha: sha('a'),
      openedMs: i + 1, ciOwner: 'devops', body: `history ${i}` }, 2000)
    applyRecovery(state, `TogetherWeOwn/repo${i}`, { recoveredSha: sha('c'),
      recoveredMs: i + 2, recoveryBody: 'back' }, 2000)
  }
  // The default limit of 1000 would throw on this legitimate history.
  assert.throws(() => findOpenIncident(state, repo))
  assert.equal(findOpenIncident(state, repo, 2000), null)
  const incidents = createMemoryRedmainStore(state, 2000)
  await incidents.openIncident({ key: mainRedIncidentKey(repo), repository: repo,
    firstRedSha: sha('b'), openedMs: 5000, ciOwner: 'devops', body: 'late' })
  await incidents.closeIncident(mainRedIncidentKey(repo),
    { recoveredSha: sha('d'), recoveredMs: 6000, recoveryBody: 'closed late' })
  assert.equal(await incidents.findOpen(repo), null)
})

test('file store round-trips one open incident across restarts', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'redmain-test-'))
  try {
    const digest = createHash('sha256').update('file').digest('hex')
    const namespace = redmainNamespace(digest)
    const store = createFileRedmainStore({ directory: dir, namespace })
    assert.equal(await store.findOpen(repo), null)
    await store.openIncident({ key: mainRedIncidentKey(repo), repository: repo,
      firstRedSha: sha('a'), openedMs: 1000, ciOwner: 'devops', body: 'first' })
    // A fresh handle over the same directory sees the same open incident.
    const reopened = createFileRedmainStore({ directory: dir, namespace })
    assert.equal((await reopened.findOpen(repo)).firstRedSha, sha('a'))
    await reopened.closeIncident(mainRedIncidentKey(repo),
      { recoveredSha: sha('c'), recoveredMs: 2000, recoveryBody: 'back' })
    assert.equal(await createFileRedmainStore({ directory: dir, namespace }).findOpen(repo), null)
    const loaded = await loadRedmainState({ directory: dir, namespace })
    assert.equal(loaded.incidents.length, 1)
    assert.equal(loaded.incidents[0].status, 'recovered')
    await saveRedmainState({ directory: dir, state: loaded })
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('adapter reads the default-branch head rollup in one instant', async () => {
  const repository = repo
  const pushedMs = 1000000
  const rollup = (nodes) => ({
    data: { repository: { nameWithOwner: repository, defaultBranchRef: { name: 'main',
      target: { __typename: 'Commit', oid: sha('a'), pushedDate: new Date(pushedMs).toISOString(),
        statusCheckRollup: { contexts: { nodes, pageInfo: { hasNextPage: false } } } } } } },
  })
  const runRun = { name: 'check', __typename: 'CheckRun', status: 'COMPLETED', conclusion: 'FAILURE', completedAt: new Date(pushedMs).toISOString() }
  const runPending = { name: 'gitleaks', __typename: 'CheckRun', status: 'IN_PROGRESS', conclusion: null, completedAt: null }
  const ctxPass = { __typename: 'StatusContext', context: 'pr-lint', state: 'SUCCESS', createdAt: new Date(pushedMs).toISOString() }
  // The required set is per repo+branch: protection requires check+gitleaks
  // but NOT pr-lint, so this repo judges only its own two.
  const protection = { required_status_checks: { contexts: ['check'], checks: [{ context: 'gitleaks' }] } }
  const stub = (headDoc) => async (file, args) => {
    assert.equal(file, 'gh')
    if (args.includes('graphql')) return { stdout: JSON.stringify(headDoc) }
    assert.ok(args.some((a) => typeof a === 'string' && a.includes('/protection')))
    return { stdout: JSON.stringify(protection) }
  }
  const adapter = createGithubAdapter({ allowedRepositories: [repository],
    run: stub(rollup([runRun, runPending, ctxPass])) })
  const read = await adapter.getMainHeadChecks(repository, { nowMs: pushedMs + 1 })
  assert.equal(read.branch, 'main')
  assert.equal(read.mainSha, sha('a'))
  assert.deepEqual(read.requiredChecks, ['check', 'gitleaks'])
  assert.deepEqual(read.checks.map((c) => [c.name, c.status, c.conclusion ?? null]), [
    ['check', 'completed', 'failure'],
    ['gitleaks', 'in_progress', null],
    ['pr-lint', 'completed', 'success'],
  ])
  const verdict = decideMainRed({ repository, branch: read.branch, mainSha: read.mainSha,
    requiredChecks: read.requiredChecks, checks: read.checks }, policy)
  assert.equal(verdict.state, 'red')
  // A repo that does not require pr-lint goes green without it: the global
  // list can no longer hold a repo red forever.
  const calm = decideMainRed({ repository, branch: 'main', mainSha: sha('a'),
    requiredChecks: ['check', 'gitleaks'],
    checks: [pass('check'), pass('gitleaks')] }, policy)
  assert.equal(calm.state, 'green')
  // Null rollup reads as pending (unobservable), never green.
  const empty = createGithubAdapter({ allowedRepositories: [repository],
    run: stub({ data: { repository: { nameWithOwner: repository,
      defaultBranchRef: { name: 'main', target: { __typename: 'Commit', oid: sha('a'),
        pushedDate: new Date(pushedMs).toISOString(), statusCheckRollup: null } } } } }) })
  const unread = await empty.getMainHeadChecks(repository, { nowMs: pushedMs + 1 })
  assert.deepEqual(unread.checks, [])
  assert.equal(decideMainRed({ repository, branch: 'main', mainSha: sha('a'),
    requiredChecks: unread.requiredChecks, checks: [] }, policy).state, 'pending')
})
