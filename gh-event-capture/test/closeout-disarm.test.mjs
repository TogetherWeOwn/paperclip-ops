import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createGithubAdapter } from '../src/github-adapter.js'
import { decideDisarm, disarmPolicy, createDisarmer } from '../src/closeout-disarm.js'

const repo = 'TogetherWeOwn/kofra'
const other = 'TogetherWeOwn/routeware'
const head = (c) => c.repeat(40)
const off = disarmPolicy()
const on = disarmPolicy({ enabledRepositories: [repo] })

function snapshot(number, over = {}) {
  return { repository: repo, number, headSha: head('a'), state: 'open',
    draft: false, autoMerge: false, labels: [], ...over }
}

// Injected GitHub double. It implements EXACTLY the two disarm dependencies:
// a snapshot reader and a disarm writer. Any arm or merge call path would
// have nowhere to land: there is no such method to call.
function doubles(snapshots, { failDisarm = [] } = {}) {
  const calls = []
  const github = {
    async getCloseoutSnapshot(repository, number, { nowMs } = {}) {
      calls.push(['read-snapshot', repository, number, nowMs])
      const found = snapshots.get(`${repository}#${number}`)
      if (!found) throw new Error('GitHub query failed: transport, timeout, output limit or invalid JSON')
      return found
    },
    async disableAutoMerge(repository, number) {
      calls.push(['disable-auto', repository, number])
      if (failDisarm.includes(number)) throw new Error('GitHub disarm failed: transport, timeout or output limit')
    },
  }
  return { github, calls }
}

test('armed+held with the flag on disarms exactly once', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1, { headSha: head('b'), autoMerge: true, labels: ['hold'] })]])
  const { github, calls } = doubles(snapshots)
  const { records, counters } = await createDisarmer({ github, policy: on })
    .runDisarm({ references: [{ repository: repo, number: 1 }], nowMs: 1000 })
  assert.deepEqual(counters, { total: 1, evaluated: 1, needsDisarm: 1, disarmed: 1, wouldDisarm: 0, noOp: 0, errors: 0 })
  assert.equal(records[0].decision, 'disarmed')
  assert.equal(records[0].reason, 'armed-held')
  assert.equal(records[0].headSha, head('b'))
  assert.match(records[0].log, new RegExp(`^closeout-disarm ${repo}#1@${head('b')}: disarmed \\(armed-held\\)$`))
  assert.deepEqual(calls.filter(([op]) => op === 'disable-auto'), [['disable-auto', repo, 1]])
})

test('armed+held with the flag off logs would-disarm and makes no gh call', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1, { autoMerge: true, labels: ['hold'] })]])
  const { github, calls } = doubles(snapshots)
  const { records, counters } = await createDisarmer({ github, policy: off })
    .runDisarm({ references: [{ repository: repo, number: 1 }], nowMs: 1000 })
  assert.deepEqual([counters.needsDisarm, counters.wouldDisarm, counters.disarmed, counters.errors], [1, 1, 0, 0])
  assert.equal(records[0].decision, 'would-disarm')
  assert.match(records[0].log, /would-disarm \(armed-held\)/)
  assert.deepEqual(calls.filter(([op]) => op === 'disable-auto'), [])
})

test('armed+drafted disarms the same way, flag on and off', async () => {
  for (const [policy, decision, disarms] of [[on, 'disarmed', 1], [off, 'would-disarm', 0]]) {
    const snapshots = new Map([[`${repo}#2`, snapshot(2, { autoMerge: true, draft: true })]])
    const { github, calls } = doubles(snapshots)
    const { records } = await createDisarmer({ github, policy })
      .runDisarm({ references: [{ repository: repo, number: 2 }], nowMs: 1000 })
    assert.equal(records[0].decision, decision)
    assert.equal(records[0].reason, 'armed-drafted')
    assert.equal(calls.filter(([op]) => op === 'disable-auto').length, disarms)
  }
})

test('unarmed+held and unarmed clean PRs are no-ops with no gh call', async () => {
  const snapshots = new Map([
    [`${repo}#3`, snapshot(3, { labels: ['hold'] })],
    [`${repo}#4`, snapshot(4, {})],
    [`${repo}#5`, snapshot(5, { state: 'closed' })],
  ])
  const { github, calls } = doubles(snapshots)
  const { records, counters } = await createDisarmer({ github, policy: on })
    .runDisarm({ references: [3, 4, 5].map((number) => ({ repository: repo, number })), nowMs: 1000 })
  assert.deepEqual(counters.noOp, 3)
  assert.deepEqual([counters.disarmed, counters.wouldDisarm, counters.errors], [0, 0, 0])
  assert.deepEqual(records.map((r) => [r.number, r.decision, r.reason]),
    [[3, 'none', 'unarmed'], [4, 'none', 'unarmed'], [5, 'none', 'closed']])
  assert.deepEqual(calls.filter(([op]) => op === 'disable-auto'), [])
})

test('the duty never arms and never merges', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1, { autoMerge: true, labels: ['hold'] })]])
  const { github, calls } = doubles(snapshots)
  await createDisarmer({ github, policy: on })
    .runDisarm({ references: [{ repository: repo, number: 1 }], nowMs: 1000 })
  // Every outbound call is either a snapshot read or a disarm. There is no
  // `--auto`, no merge PUT, no third operation: the double exposes no such
  // method, so an arm/merge mutant has nowhere to hide.
  for (const [op] of calls) assert.ok(['read-snapshot', 'disable-auto'].includes(op), `unexpected call ${op}`)
  assert.equal(calls.filter(([op]) => op === 'disable-auto').length, 1)
})

test('disarm bypasses the debounce: the first read acts, repeats repeat it', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1, { autoMerge: true, labels: ['hold'] })]])
  const { github, calls } = doubles(snapshots)
  const disarmer = createDisarmer({ github, policy: on })
  const refs = [{ repository: repo, number: 1 }]
  // Same millisecond twice: a debounced duty would stay silent on the first
  // sighting and evaluate nothing. Disarm acts immediately, both times, and
  // disabling twice is safe (idempotent).
  await disarmer.runDisarm({ references: refs, nowMs: 0 })
  const second = await disarmer.runDisarm({ references: refs, nowMs: 0 })
  assert.equal(second.records[0].decision, 'disarmed')
  assert.equal(calls.filter(([op]) => op === 'disable-auto').length, 2)
})

test('the flag is per repo: an enabled repo disarms while another only logs', async () => {
  const snapshots = new Map([
    [`${repo}#1`, snapshot(1, { autoMerge: true, labels: ['hold'] })],
    [`${other}#7`, { ...snapshot(7, { autoMerge: true, draft: true }), repository: other }],
  ])
  const { github } = doubles(snapshots)
  const { records, counters } = await createDisarmer({ github, policy: on })
    .runDisarm({ references: [{ repository: repo, number: 1 }, { repository: other, number: 7 }], nowMs: 1000 })
  assert.deepEqual(records.map((r) => r.decision), ['disarmed', 'would-disarm'])
  assert.deepEqual([counters.disarmed, counters.wouldDisarm], [1, 1])
})

test('unreadable, undecidable and failed-disarm PRs are counted errors, not a crash', async () => {
  const snapshots = new Map([
    [`${repo}#1`, snapshot(1, { autoMerge: true, labels: ['hold'] })],
    [`${repo}#6`, snapshot(6, { autoMerge: true, draft: true })],
    [`${repo}#8`, { repository: repo, number: 8, headSha: head('c'), state: 'open', draft: 'yes', autoMerge: true, labels: [] }],
  ])
  const { github } = doubles(snapshots, { failDisarm: [6] })
  const { records, counters } = await createDisarmer({ github, policy: on })
    .runDisarm({ references: [1, 6, 8, 9].map((number) => ({ repository: repo, number })), nowMs: 1000 })
  assert.equal(counters.disarmed, 1)
  assert.equal(counters.errors, 3)
  const byNumber = new Map(records.map((r) => [r.number, r]))
  assert.equal(byNumber.get(6).error, 'disarm-failed')
  assert.equal(byNumber.get(8).error, 'undecidable-snapshot')
  assert.equal(byNumber.get(9).error, 'snapshot-read-failed')
  for (const r of records) assert.match(r.log, /^closeout-disarm \S+: \S+ \(\S+\)$/)
})

test('duty rejects invalid wiring instead of running half-configured', async () => {
  const snapshots = new Map([[`${repo}#1`, snapshot(1, { autoMerge: true, labels: ['hold'] })]])
  const { github } = doubles(snapshots)
  const refs = [{ repository: repo, number: 1 }]
  await assert.rejects(createDisarmer({ github, policy: on }).runDisarm({ references: [refs[0], refs[0]] }), /duplicate/)
  await assert.rejects(createDisarmer({ github, policy: on }).runDisarm({ references: [{ repository: repo, number: 0 }] }), /reference is invalid/)
  assert.throws(() => createDisarmer({ github: {}, policy: on }), /nothing else/)
  assert.throws(() => disarmPolicy({ enabledRepositories: ['not-a-repo!!'] }), /enabledRepositories/)
  assert.throws(() => decideDisarm({ ...snapshot(1), autoMerge: null }, on), /lifecycle/)
  assert.equal(decideDisarm(snapshot(1, { autoMerge: true, labels: ['hold', 'extra'] }), on).held, true)
  // A custom hold-label set is honoured: 'hold' alone no longer holds.
  const custom = disarmPolicy({ enabledRepositories: [repo], holdLabels: ['frozen'] })
  assert.equal(decideDisarm(snapshot(1, { autoMerge: true, labels: ['hold'] }), custom), null)
})

test('adapter disarm calls exactly gh pr merge --disable-auto, scoped to the allowlist', async () => {
  const argv = []
  const run = async (...args) => { argv.push(args); return { stdout: '', stderr: '' } }
  const adapter = createGithubAdapter({ allowedRepositories: [repo], run })
  await adapter.disableAutoMerge(repo, 42)
  assert.deepEqual(argv, [[
    'gh', ['pr', 'merge', '42', '--repo', repo, '--disable-auto'],
    { encoding: 'utf8', shell: false, timeout: 30000, maxBuffer: 1024 * 1024 },
  ]])
  // The argv carries --disable-auto and never --auto: an arm-instead-of-disarm
  // mutant changes this exact array and dies here.
  const flags = argv[0][1]
  assert.ok(flags.includes('--disable-auto'))
  assert.ok(!flags.includes('--auto'))
  await assert.rejects(adapter.disableAutoMerge('EvilOrg/other', 42), /outside the configured scope/)
  await assert.rejects(adapter.disableAutoMerge(repo, 0), /PR number is invalid/)
  const failing = createGithubAdapter({ allowedRepositories: [repo], run: async () => { throw new Error('boom') } })
  await assert.rejects(failing.disableAutoMerge(repo, 1), /disarm failed/)
})
