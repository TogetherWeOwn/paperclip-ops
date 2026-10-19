import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, writeFile, readdir, rm, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { runProductPass } from '../src/consumer-runner.js'
import { loadConsumerConfig } from '../src/consumer-config.js'
import { receiptNamespace } from '../src/receipt-cycle.js'
import { createApp } from '../src/app.js'
import { createMemoryStore } from '../src/store-memory.js'
import { ingest } from './bridge-fixtures.mjs'
import { loadSweepState, saveSweepState } from '../src/sweep-store.js'
import { loadPassSchedule, savePassSchedule } from '../src/pass-schedule.js'
import { closeoutShadowSettings } from '../src/closeout-shadow-wiring.js'
import { closeoutDisarmSettings, createDisarmShadowHook } from '../src/closeout-disarm-wiring.js'

const A = 'TogetherWeOwn/paperclip-ops'
const B = 'TogetherWeOwn/kofra'
const companyId = '00000000-0000-4000-8000-000000000004'
const privateText = 'UNTRUSTED-PRIVATE-PAYLOAD'
const wall = Date.parse('2026-10-08T06:00:00Z')
const sha = (n) => String(n % 10).repeat(40)
const enrolled = (repos = [A]) => ({ disarmShadow: { enabledRepositories: repos } })
const envelope = (repo, fields) => ({ data: { repository: { nameWithOwner: repo, isPrivate: true, ...fields } } })
const node = (repo, number, over = {}) => ({ number, url: `https://github.com/${repo}/pull/${number}`,
  title: privateText, body: privateText, state: 'OPEN', isDraft: false, merged: false,
  headRefName: 'feature-unmapped', headRefOid: sha(number), baseRefName: 'main',
  reviewDecision: null, mergeStateStatus: 'DIRTY', autoMergeRequest: { enabledAt: 'unused' },
  author: { __typename: 'User', login: 'human' }, baseRepository: { nameWithOwner: repo },
  labels: { nodes: [], pageInfo: { hasNextPage: false } },
  commits: { nodes: [{ commit: { oid: sha(number), pushedDate: '2026-10-03T07:00:00Z',
    statusCheckRollup: { contexts: { nodes: [], pageInfo: { hasNextPage: false } } } } }] }, ...over })

async function fixture(t, { closeout, repos = [A], open = { [A]: [7] }, limits = {} } = {}) {
  t.mock.method(Date, 'now', () => wall)
  const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'disarm-wiring-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const config = { version: 1, mode: 'products-only-v1', captureOrigin: 'https://capture.test',
    boardOrigin: 'https://board.test', companyId, allowedRepositories: repos, stateDirectory: join(root, 'state'),
    captureTokenFile: join(root, 'capture.key'), boardTokenFile: join(root, 'board.key'), limits,
    ...(closeout === undefined ? {} : { closeout }) }
  const app = createApp({ store: createMemoryStore(), webhookSecret: 'fixture', queryToken: 'fixture', now: () => wall })
  const h = { config, root, open, snapshots: new Map(), primary: new Map(), calls: [], logs: [],
    snapshotHook: null, primaryHook: null, fetchHook: null, listHook: null, rulesRepositories: new Set() }
  h.ingest = (event, id, payload) => ingest(app, event, id, payload, 'fixture')
  h.fetch = async (url, init) => {
    h.calls.push(['http', init.method, url])
    if (h.fetchHook) { const response = await h.fetchHook(url, init); if (response) return response }
    if (url.startsWith(config.captureOrigin)) {
      assert.equal(init.method, 'GET', 'no capture claims or other writes')
      return app(new Request(url, init))
    }
    assert.equal(init.method, 'GET', 'no board writes, comments, mentions or wake path')
    assert.equal(new URL(url).pathname, `/api/companies/${companyId}/issues`, 'no extra board scan')
    return Response.json([])
  }
  h.run = async (file, args, options) => {
    assert.equal(file, 'fixture-gh')
    if (args[3] === '--method') {
      assert.deepEqual(args.slice(0, 5), ['api', '--hostname', 'github.com', '--method', 'GET'])
      const match = /^repos\/(.+\/[^/]+)\/rules\/branches\/main$/.exec(args[5])
      assert.ok(match && h.rulesRepositories.has(match[1]), 'only explicitly enabled read-only ruleset fixtures')
      h.calls.push(['rules', match[1]])
      return { stdout: JSON.stringify([{ type: 'required_status_checks',
        parameters: { required_status_checks: [{ context: 'ci-ok' }] } }]) }
    }
    assert.deepEqual(args.slice(0, 4), ['api', '--hostname', 'github.com', 'graphql'],
      'no arm, merge, disable-auto, REST mutation or raw command')
    assert.equal(options.shell, false)
    const query = args[5]
    const repository = `${args.find((v) => v.startsWith('owner=')).slice(6)}/${args.find((v) => v.startsWith('name=')).slice(5)}`
    if (query.includes('BridgeOpenPullRequests')) {
      h.calls.push(['list', repository])
      if (h.listHook) { const response = await h.listHook(repository, args); if (response) return response }
      const numbers = h.open[repository] ?? []
      return { stdout: JSON.stringify(envelope(repository, { pullRequests: { totalCount: numbers.length,
        nodes: numbers.map((number) => ({ number })), pageInfo: { hasNextPage: false, endCursor: null } } })) }
    }
    const number = Number(args.find((v) => v.startsWith('number=')).slice(7))
    const snapshot = query.includes('BridgeCloseoutSnapshot')
    h.calls.push([snapshot ? 'snapshot' : 'primary', repository, number])
    if (snapshot && h.snapshotHook) {
      const response = await h.snapshotHook({ repository, number }, options)
      if (response) return response
    }
    if (!snapshot && h.primaryHook) await h.primaryHook({ repository, number }, options)
    const over = (snapshot ? h.snapshots : h.primary).get(number) ?? {}
    return { stdout: JSON.stringify(envelope(repository, { pullRequest: node(repository, number, over) })) }
  }
  h.pass = (options = {}) => runProductPass({ config, captureToken: 'fixture', boardToken: 'fixture',
    githubExecutable: 'fixture-gh' }, { fetchImpl: h.fetch, run: h.run, log: (line) => h.logs.push(JSON.parse(line)), ...options })
  h.state = () => loadSweepState({ directory: config.stateDirectory, namespace: receiptNamespace(config) })
  h.rewindReview = async () => {
    const state = await h.state()
    for (const entry of Object.values(state.repos)) entry.review.lastCompleteMs = null
    await saveSweepState({ directory: config.stateDirectory, state })
  }
  h.summaryLogs = () => h.logs.filter((line) => line.event === 'closeout-disarm-shadow')
  h.kinds = (kind) => h.calls.filter(([k]) => k === kind)
  return h
}
const nonemptyReasons = (result) => Object.fromEntries(Object.entries(result.disarmShadow.byReason).filter(([, n]) => n))
async function stored(h) {
  const files = await readdir(h.config.stateDirectory)
  return Object.fromEntries(await Promise.all(files.sort().map(async (file) => [file,
    await readFile(join(h.config.stateDirectory, file), 'utf8')])))
}

function mappedPrimary(h, numbers) {
  const writes = []
  const issueId = '11111111-1111-4111-8111-111111111111'
  for (const number of numbers) h.primary.set(number, { headRefName: `TOG-${number}-mapped` })
  h.fetchHook = async (url, init) => {
    if (url.startsWith(h.config.captureOrigin)) return null
    const path = new URL(url).pathname
    if (/^\/api\/issues\/TOG-\d+$/.test(path)) return Response.json({ id: issueId, companyId,
      identifier: path.split('/').at(-1), status: 'in_progress' })
    if (path === `/api/issues/${issueId}/work-products` && init.method === 'GET') return Response.json([])
    assert.equal(path, `/api/issues/${issueId}/work-products`)
    assert.equal(init.method, 'POST', 'only normal primary product writes are permitted')
    const body = JSON.parse(init.body)
    writes.push(body)
    return Response.json({ id: '22222222-2222-4222-8222-222222222222', companyId, issueId, ...body })
  }
  return writes
}

test('absent and disabled disarm shadow preserve serialized results, namespace, persisted bytes and all I/O', async (t) => {
  const transcripts = []
  for (const closeout of [undefined, {}, enrolled([]), { shadow: { enabledRepositories: [] }, ...enrolled([]) }]) {
    const h = await fixture(t, { closeout })
    const namespace = receiptNamespace(h.config)
    assert.equal(namespace, receiptNamespace({ ...h.config, closeout: enrolled() }))
    const results = [await h.pass(), await h.pass()]
    assert.equal(results.every((r) => r.ok), true)
    assert.equal(results.every((r) => !Object.hasOwn(r, 'disarmShadow')), true, 'OFF adds no result key')
    assert.deepEqual(h.kinds('snapshot'), [], 'OFF adds zero snapshot requests')
    assert.deepEqual(h.logs, [], 'OFF adds zero logging')
    assert.equal(h.kinds('http').every(([, , url]) => url.startsWith(h.config.captureOrigin)), true)
    transcripts.push({ results: JSON.stringify(results), calls: h.calls, bytes: await stored(h), namespace })
  }
  for (const transcript of transcripts.slice(1)) assert.deepEqual(transcript, transcripts[0], 'OFF is byte-for-byte compatible')
})

test('backfill and review collect before mapping and observe enrolled drafts/holds after primary work', async (t) => {
  const h = await fixture(t, { closeout: enrolled(), repos: [A, B], open: { [A]: [7, 8, 9], [B]: [1] } })
  h.snapshots.set(7, { isDraft: true })
  h.snapshots.set(8, { labels: { nodes: [{ name: 'hold' }], pageInfo: { hasNextPage: false } } })
  h.snapshots.set(9, { isDraft: true, labels: { nodes: [{ name: 'hold' }], pageInfo: { hasNextPage: false } } })
  // The primary read is deliberately stale/unarmed: it cannot decide the shadow.
  h.primary.set(7, { autoMergeRequest: null })
  for (let pass = 0; pass < 2; pass++) {
    h.calls.length = 0
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(result.requests, h.calls.length, 'shadow snapshot uses the shared request counter')
    assert.deepEqual(h.kinds('snapshot'), [['snapshot', A, 7], ['snapshot', A, 8], ['snapshot', A, 9]])
    assert.ok(h.calls.findIndex((v) => v[0] === 'snapshot') > h.calls.findLastIndex((v) => v[0] !== 'snapshot'),
      'all primary I/O precedes optional snapshot reads')
    assert.equal(result.disarmShadow.counters.wouldDisarm, 3, 'empty LIVE enrollment produces only would-disarm')
    assert.equal(result.disarmShadow.counters.disarmed, 0)
    assert.equal(result.disarmShadow.counters.errors, 0, 'the refusing mutation dependency was never called')
    assert.deepEqual(nonemptyReasons(result), { 'armed-drafted': 1, 'armed-held': 1, 'armed-drafted-held': 1 })
    assert.equal(result.sweeps.processed, 4)
    assert.equal(h.summaryLogs().length, pass + 1, 'one fixed summary per pass')
  }
  h.calls.length = 0
  const quiet = await h.pass()
  assert.equal(quiet.disarmShadow.counters.total, 0, 'completed refs wait for the normal next review')
  assert.deepEqual(h.kinds('snapshot'), [])
})

test('failed primary still collects before mapping and observes the scheduled ref', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  h.snapshots.set(7, { isDraft: true })
  h.primaryHook = async () => { throw new Error(privateText) }
  const result = await h.pass()
  assert.equal(result.ok, false, 'primary failure still fails the pass')
  assert.equal(result.reason, 'sweeps-incomplete')
  assert.deepEqual(h.kinds('snapshot'), [['snapshot', A, 7]],
    'collect-before-mapping observes failed primary refs')
  assert.equal(result.disarmShadow.counters.wouldDisarm, 1)
  assert.deepEqual(nonemptyReasons(result), { 'armed-drafted': 1 })
  assert.deepEqual(result.sweeps.failures, [{ repository: A, number: 7, phase: 'backfill', attempt: 1 }])
  assert.equal(result.sweeps.processed, 0)
  assert.equal(result.sweeps.poisonedTotal, 0)
  assert.deepEqual(result.sweeps.poisonedNow, [])
  assert.equal(JSON.stringify(result.disarmShadow).includes(privateText), false)
  assert.equal(JSON.stringify(h.logs).includes(privateText), false)
})

const negative = [
  ['ready and not held', {}, 'clean', false],
  ['hold is case-sensitive', { labels: { nodes: [{ name: 'Hold' }], pageInfo: { hasNextPage: false } } }, 'clean', false],
  ['hold is exact', { labels: { nodes: [{ name: 'hold-extra' }], pageInfo: { hasNextPage: false } } }, 'clean', false],
  ['auto-merge absent', { isDraft: true, autoMergeRequest: null }, 'unarmed', false],
  ['closed', { isDraft: true, state: 'CLOSED' }, 'closed', false],
  ['merged', { isDraft: true, state: 'MERGED', merged: true }, 'closed', false],
  ['missing auto-merge', { isDraft: true, autoMergeRequest: undefined }, 'snapshot-read-failed', true],
  ['missing draft', { isDraft: undefined }, 'snapshot-read-failed', true],
  ['unknown state', { isDraft: true, state: 'UNKNOWN' }, 'snapshot-read-failed', true],
  ['missing snapshot', null, 'snapshot-read-failed', true],
  ['truncated labels', { isDraft: true, labels: { nodes: [], pageInfo: { hasNextPage: true } } }, 'snapshot-read-failed', true],
  ['invalid check rollup', { isDraft: true, commits: { nodes: [{ commit: { oid: sha(7), pushedDate: '2026-10-03T07:00:00Z',
    statusCheckRollup: { contexts: { nodes: [], pageInfo: { hasNextPage: true } } } } }] } }, 'snapshot-read-failed', true],
]
for (const [name, over, reason, error] of negative) {
  test(`${name} cannot produce a would-disarm or mutation`, async (t) => {
    const h = await fixture(t, { closeout: enrolled() })
    if (over === null) h.snapshotHook = async () => { throw new Error(privateText) }
    else h.snapshots.set(7, over)
    const result = await h.pass()
    assert.equal(result.ok, true, 'observational failure never fails primary work')
    assert.equal(result.disarmShadow.counters.wouldDisarm, 0)
    assert.equal(result.disarmShadow.counters.errors, Number(error))
    assert.deepEqual(nonemptyReasons(result), { [reason]: 1 })
    assert.equal(result.sweeps.processed, 1)
    assert.deepEqual(result.sweeps.failures, [])
    assert.equal(result.sweeps.poisonedTotal, 0)
    assert.equal(JSON.stringify(h.logs).includes(privateText), false, 'no arbitrary API fields or errors')
  })
}

test('permission-denied snapshot is an observation error, never acceptance or a credential retry', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  h.snapshotHook = async () => ({ stdout: JSON.stringify({ errors: [
    { type: 'FORBIDDEN', message: `Resource not accessible by integration ${privateText}` },
  ], data: { repository: { pullRequest: null } } }) })
  const result = await h.pass()
  assert.equal(result.ok, true, 'primary success does not imply observation acceptance')
  assert.equal(result.disarmShadow.counters.errors, 1)
  assert.equal(result.disarmShadow.counters.wouldDisarm, 0)
  assert.deepEqual(nonemptyReasons(result), { 'snapshot-read-failed': 1 })
  assert.deepEqual(h.kinds('snapshot'), [['snapshot', A, 7]], 'one read, no retry or alternate transport')
  assert.deepEqual(result.sweeps.failures, [])
  assert.equal(result.sweeps.poisonedTotal, 0)
  assert.deepEqual((await h.state()).repos[A].backfill.done, [7], 'primary success still checkpoints')
  assert.equal(JSON.stringify(h.logs).includes(privateText), false)
})

test('shadow does not allocate or change receipt, claim, pending or index state', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  h.snapshots.set(7, { isDraft: true })
  assert.equal((await h.pass()).ok, true)
  // Deliberately unreadable sentinels: this duty may not even read the stores.
  for (const name of ['claims.json', 'pending.json', 'pr-task-index.json']) {
    await writeFile(join(h.config.stateDirectory, name), privateText, { mode: 0o600 })
  }
  const protectedFiles = ['receipts.json', 'claims.json', 'pending.json', 'pr-task-index.json']
  const before = await Promise.all(protectedFiles.map(async (name) => ({ name,
    bytes: await readFile(join(h.config.stateDirectory, name), 'utf8'), info: await stat(join(h.config.stateDirectory, name)) })))
  assert.equal((await h.pass()).disarmShadow.counters.wouldDisarm, 1, 'repeated observations never consume a live duty')
  await h.rewindReview()
  assert.equal((await h.pass()).disarmShadow.counters.wouldDisarm, 1)
  for (const { name, bytes, info } of before) {
    const path = join(h.config.stateDirectory, name)
    assert.equal(await readFile(path, 'utf8'), bytes)
    const after = await stat(path)
    assert.equal(after.ino, info.ino, 'no atomic rewrite of live state')
    assert.equal(after.mtimeMs, info.mtimeMs)
  }
  assert.deepEqual((await readdir(h.config.stateDirectory)).sort(),
    ['claims.json', 'pending.json', 'pr-task-index.json', 'receipts.json', 'schedule.json', 'sweeps.json'])
  assert.equal(h.kinds('http').every(([, , url]) => url.startsWith(h.config.captureOrigin)), true, 'no board mapping/index reads')
})

test('resumed backfill and review drains observe only refs scheduled in this pass', async (t) => {
  const h = await fixture(t, { closeout: enrolled(), open: { [A]: [7, 8, 9] }, limits: { maxSweepItems: 1 } })
  for (const number of [7, 8, 9]) h.snapshots.set(number, { isDraft: true })
  for (const expected of [7, 8, 9, 7, 8, 9]) {
    h.calls.length = 0
    const result = await h.pass()
    assert.equal(result.sweeps.processed, 1)
    assert.deepEqual(h.kinds('snapshot'), [['snapshot', A, expected]], 'not a whole-list observation')
    assert.equal(result.disarmShadow.counters.wouldDisarm, 1)
  }
})

test('broken log sink is counted and cannot poison product checkpoint success', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  h.snapshots.set(7, { isDraft: true })
  const result = await h.pass({ log: () => { throw new Error(privateText) } })
  assert.equal(result.ok, true)
  assert.equal(result.disarmShadow.counters.wouldDisarm, 1)
  assert.equal(result.disarmShadow.counters.logErrors, 1)
  assert.deepEqual((await h.state()).repos[A].backfill.done, [7])
})

test('the optional read defers to four primary requests using the SAME remaining budget', async (t) => {
  const h = await fixture(t, { closeout: enrolled(), limits: { maxRequests: 7 } })
  h.snapshots.set(7, { isDraft: true })
  h.primary.set(7, { headRefName: 'T\x4fG-7-mapped' })
  const writes = []
  const issueId = '11111111-1111-4111-8111-111111111111'
  h.fetchHook = async (url, init) => {
    if (url.startsWith(h.config.captureOrigin)) return null
    const path = new URL(url).pathname
    if (path === '/api/issues/T\x4fG-7') return Response.json({ id: issueId, companyId, identifier: 'T\x4fG-7', status: 'in_progress' })
    if (path === `/api/issues/${issueId}/work-products` && init.method === 'GET') return Response.json([])
    if (path === `/api/issues/${issueId}/work-products` && init.method === 'POST') {
      const body = JSON.parse(init.body)
      writes.push(body)
      return Response.json({ id: '22222222-2222-4222-8222-222222222222', companyId, issueId, ...body })
    }
    assert.fail('unexpected board route')
  }
  const result = await h.pass()
  assert.equal(result.ok, true, 'optional shadow cannot starve mapped primary work')
  assert.equal(result.requests, 7)
  assert.deepEqual(h.kinds('snapshot'), [], 'deferred shadow launches no command')
  assert.equal(result.disarmShadow.counters.deferred, 1)
  assert.equal(result.disarmShadow.counters.errors, 0, 'deferral is not failed verification')
  assert.equal(result.disarmShadow.counters.wouldDisarm, 0)
  assert.equal(writes.length, 1, 'only the normal primary work-product write')
  assert.deepEqual(nonemptyReasons(result), { 'budget-deferred': 1 })
})

test('shadow ON preserves OFF primary completion across a tight whole-sweep request budget', async (t) => {
  const numbers = Array.from({ length: 20 }, (_, i) => i + 1)
  const results = []
  for (const closeout of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout, open: { [A]: numbers }, limits: { maxRequests: 30, maxSweepItems: 50 } })
    for (const number of numbers) h.snapshots.set(number, { isDraft: true })
    const result = await h.pass()
    assert.equal(result.ok, true, 'shadow ON cannot exhaust requests needed by later primary items')
    assert.equal(result.reason, null)
    assert.equal(result.requests, closeout ? 30 : 23)
    assert.equal(result.sweeps.processed, 20)
    if (closeout) {
      assert.equal(result.disarmShadow.counters.deferred, 13)
      assert.equal(result.disarmShadow.counters.wouldDisarm, 7, 'only actual post-primary surplus is observed')
      assert.equal(h.kinds('snapshot').length, 7)
      assert.ok(h.calls.findIndex((v) => v[0] === 'snapshot') > h.calls.findLastIndex((v) => v[0] !== 'snapshot'),
        'shadow does not spend requests until all primary work is done')
    }
    results.push({ events: result.events, sweeps: result.sweeps, bytes: await stored(h) })
  }
  assert.deepEqual(results[1], results[0], 'tight-budget ON preserves primary results and checkpoints')
})

test('surplus observation preserves all mapped primary slots, not only the next one', async (t) => {
  const numbers = Array.from({ length: 20 }, (_, i) => i + 1)
  const primary = []
  for (const closeout of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout, open: { [A]: numbers }, limits: { maxRequests: 84, maxSweepItems: 20 } })
    const writes = mappedPrimary(h, numbers)
    for (const number of numbers) h.snapshots.set(number, { isDraft: true })
    const result = await h.pass()
    assert.equal(result.ok, true, 'all mapped primary slots retain their requests')
    assert.equal(result.reason, null)
    assert.equal(writes.length, 20)
    assert.equal(result.requests, closeout ? 84 : 83)
    if (closeout) {
      assert.equal(result.disarmShadow.counters.wouldDisarm, 1, 'the one surplus request is observed, not always disabled')
      assert.equal(result.disarmShadow.counters.deferred, 19)
    }
    primary.push({ events: result.events, sweeps: result.sweeps, writes })
  }
  assert.deepEqual(primary[1], primary[0])
})

test('shadow reserves later repositories and paginated listings before spending surplus', async (t) => {
  const primary = []
  for (const closeout of [undefined, enrolled([A, B])]) {
    const h = await fixture(t, { closeout, repos: [A, B], open: { [B]: [7], [A]: [8, 9] },
      limits: { maxRequests: 19, maxSweepItems: 3, maxPages: 2 } })
    const writes = mappedPrimary(h, [7, 8, 9])
    h.listHook = async (repository, args) => {
      if (repository !== A) return null
      const second = args.includes('after=page-one')
      return { stdout: JSON.stringify(envelope(repository, { pullRequests: { totalCount: 2,
        nodes: [{ number: second ? 9 : 8 }], pageInfo: { hasNextPage: !second, endCursor: second ? null : 'page-one' } } })) }
    }
    const result = await h.pass()
    assert.equal(result.ok, true, 'observation cannot exhaust a later repository scan or primary item')
    assert.equal(result.reason, null)
    assert.equal(result.requests, 19)
    assert.equal(writes.length, 3)
    assert.deepEqual(h.kinds('snapshot'), [])
    if (closeout) assert.equal(result.disarmShadow.counters.deferred, 3)
    primary.push({ events: result.events, sweeps: result.sweeps, writes })
  }
  assert.deepEqual(primary[1], primary[0])
})

test('sweeps-first resumed drains defer shadow to the still-pending event stream', async (t) => {
  const primary = []
  for (const closeout of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout, open: { [A]: [] }, limits: { maxRequests: 6, maxSweepItems: 1 } })
    assert.equal((await h.pass()).ok, true)
    const state = await h.state()
    Object.assign(state.repos[A].review, { phase: 'draining', refs: [7], done: [], listedMs: wall, lastCompleteMs: null })
    await saveSweepState({ directory: h.config.stateDirectory, state })
    const writes = mappedPrimary(h, [7])
    h.calls.length = 0
    const result = await h.pass()
    assert.equal(result.ok, true, 'shadow cannot use requests needed by the pending event stream')
    assert.equal(result.requests, 6)
    assert.equal(writes.length, 1)
    assert.equal(h.calls[0][0], 'primary', 'sweeps-first schedule is not reordered for shadow')
    assert.deepEqual(h.kinds('snapshot'), [])
    if (closeout) assert.equal(result.disarmShadow.counters.deferred, 1)
    primary.push({ events: result.events, sweeps: result.sweeps, writes, bytes: await stored(h) })
  }
  assert.deepEqual(primary[1], primary[0])
})

test('sweeps-first check-suite fanout completes before any optional snapshot', async (t) => {
  const primary = []
  for (const closeout of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout, open: { [A]: [] },
      limits: { maxRequests: 19, maxSweepItems: 1, maxDeliveries: 1 } })
    assert.equal((await h.pass()).ok, true)
    const state = await h.state()
    Object.assign(state.repos[A].review, { phase: 'draining', refs: [7], done: [], listedMs: wall, lastCompleteMs: null })
    await saveSweepState({ directory: h.config.stateDirectory, state })
    const writes = mappedPrimary(h, [7, 8, 9, 10])
    await h.ingest('check_suite', 'fanout', { repository: { full_name: A }, action: 'completed',
      check_suite: { status: 'completed', head_sha: sha(8), pull_requests: [8, 9, 10].map((number) => ({ number })) } })
    h.calls.length = 0
    const result = await h.pass()
    assert.equal(result.ok, true, 'one event delivery can require all remaining primary requests')
    assert.equal(result.requests, 19)
    assert.equal(writes.length, 4, 'all three event PRs and the sweep PR reconcile')
    assert.deepEqual(h.kinds('primary'), [7, 8, 9, 10].map((number) => ['primary', A, number]))
    assert.deepEqual(h.kinds('snapshot'), [])
    if (closeout) assert.equal(result.disarmShadow.counters.deferred, 1)
    primary.push({ events: result.events, sweeps: result.sweeps, writes, bytes: await stored(h) })
  }
  assert.deepEqual(primary[1], primary[0], 'fanout outcomes and receipts remain identical')
})

test('exhausted request budget admits no shadow, no false checkpoint and no post-return I/O', async (t) => {
  const h = await fixture(t, { closeout: enrolled(), limits: { maxRequests: 3 } })
  const result = await h.pass()
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'request-budget')
  assert.equal(result.requests, 3)
  assert.deepEqual(h.kinds('snapshot'), [])
  assert.equal(result.disarmShadow.counters.wouldDisarm, 0)
  assert.deepEqual((await h.state()).repos[A].backfill.done, [])
  const count = h.calls.length
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(h.calls.length, count)
})

test('a stalled shadow read shares cancellation, drains and preserves completed primary checkpoints', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  const controller = new AbortController()
  let drained = false
  let active = 0
  const clock = { now: () => 0, setTimer: () => 1, clearTimer: () => {} }
  h.snapshotHook = async (_ref, options) => {
    assert.ok(options.signal instanceof AbortSignal, 'shadow read receives the pass AbortSignal')
    active++
    await new Promise((resolve) => {
      options.signal.addEventListener('abort', () => setImmediate(() => { active--; drained = true; resolve() }), { once: true })
      controller.abort()
    })
    throw new Error(privateText)
  }
  const result = await h.pass({ signal: controller.signal, clock })
  assert.equal(result.reason, 'cancelled', 'external cancellation still stops the whole pass')
  assert.equal(result.ok, false)
  assert.equal(drained, true, 'the pending command settles BEFORE pass return')
  assert.equal(active, 0)
  assert.equal(result.disarmShadow.counters.wouldDisarm, 0)
  assert.equal(result.disarmShadow.counters.total, 0, 'stopped read is not logged/countable as an observation')
  assert.deepEqual(h.kinds('primary'), [['primary', A, 7]], 'primary work completed before shadow started')
  assert.deepEqual(h.summaryLogs(), [], 'no decision log on a stopped pass')
  assert.deepEqual((await h.state()).repos[A].backfill.done, [7], 'a stopped observation cannot undo primary success')
  const count = h.calls.length
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(h.calls.length, count, 'no post-return work')
})

test('ten unmapped PRs fit a 15 s pass without the shadow (deadline probe baseline)', async (t) => {
  const numbers = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  const h = await fixture(t, { open: { [A]: numbers }, limits: { durationMs: 15000 } })
  let now = 0
  const run = async (...args) => { const result = await h.run(...args); now += 1000; return result }
  const result = await h.pass({ run, clock: { now: () => now, setTimer: () => 1, clearTimer: () => {} } })
  assert.equal(result.ok, true)
  assert.equal(result.reason, null)
  assert.deepEqual([...(await h.state()).repos[A].backfill.done].sort((a, b) => a - b), numbers)
})

test('a deadline tripped by post-primary shadow reads preserves the primary outcome', async (t) => {
  const numbers = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  const h = await fixture(t, { closeout: enrolled(), open: { [A]: numbers }, limits: { durationMs: 15000 } })
  for (const number of numbers) h.snapshots.set(number, { isDraft: true })
  let now = 0
  const run = async (...args) => { const result = await h.run(...args); now += 1000; return result }
  const result = await h.pass({ run, clock: { now: () => now, setTimer: () => 1, clearTimer: () => {} } })
  assert.equal(result.ok, true, 'a shadow-tripped deadline must not fail primary work')
  assert.equal(result.reason, null)
  assert.deepEqual([...(await h.state()).repos[A].backfill.done].sort((a, b) => a - b), numbers,
    'primary checkpoints already completed stay completed')
  assert.equal(result.disarmShadow.counters.total, numbers.length, 'every scheduled ref is accounted for')
  assert.ok(result.disarmShadow.counters.deferred > 0, 'refs past the shared deadline defer as budget-deferred')
  assert.equal(h.summaryLogs().length, 1, 'the shadow summary is still emitted for a contained drain stop')
})

test('an S3c index overrun that stops the pass before the shadow drain fails ON exactly as OFF', async (t) => {
  const outcomes = []
  for (const disarm of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout: { shadow: { enabledRepositories: [B], indexReadsPerPass: 1 }, ...disarm },
      repos: [A, B], open: { [A]: [7] }, limits: { durationMs: 600, maxPages: 1 } })
    h.rulesRepositories.add(B)
    h.snapshots.set(7, { isDraft: true })
    // Hold the board index read past the whole pass deadline. The lock-held
    // index drain has no budget check, so the stop is set before the shadow
    // drain starts; containing it would hide a real primary failure.
    const boardIssues = `${h.config.boardOrigin}/api/companies/${companyId}/issues`
    const fetch = h.fetch
    h.fetch = async (url, init) => {
      if (String(url).startsWith(boardIssues)) await new Promise((resolve) => setTimeout(resolve, 900))
      return fetch(url, init)
    }
    const result = await h.pass()
    assert.equal(result.ok, false, 'a pre-drain stop fails the pass with or without the shadow')
    assert.equal(result.reason, 'deadline')
    // Stream admission on a fresh pass is not pinned here: primary work may
    // or may not run before the held index read stops the pass. What must
    // hold under either admission is that no shadow drain read runs and any
    // checkpoint reflects only primary work that actually ran.
    assert.deepEqual(h.kinds('snapshot'), [], 'no shadow drain read runs after a pre-drain stop')
    assert.deepEqual((await h.state()).repos[A]?.backfill.done ?? [],
      h.kinds('primary').length ? [7] : [], 'checkpoint matches only the primary work that ran')
    if (disarm) {
      assert.equal(result.disarmShadow.counters.total, 0, 'no drain accounting for a stop set before the drain')
      assert.equal(result.disarmShadow.counters.deferred, 0, 'no drain read ran, so nothing is contained')
      assert.deepEqual(h.summaryLogs(), [], 'a stopped pass emits no shadow summary')
    }
    outcomes.push({ ok: result.ok, reason: result.reason })
  }
  assert.deepEqual(outcomes[1], outcomes[0], 'ON hides no pre-drain failure that OFF reports')
})

test('deadline exhausted before a scheduled ref starts produces no observation', async (t) => {
  const h = await fixture(t, { closeout: enrolled(), limits: { durationMs: 100 } })
  let now = 0
  const run = async (...args) => { const result = await h.run(...args); now = 100; return result }
  const result = await h.pass({ run, clock: { now: () => now, setTimer: () => 1, clearTimer: () => {} } })
  assert.equal(result.reason, 'deadline')
  assert.deepEqual(h.kinds('snapshot'), [])
  assert.equal(result.disarmShadow.counters.wouldDisarm, 0)
})

test('per-pass and per-repository scheduling fairness survives shadow and limited drains', async (t) => {
  const h = await fixture(t, { closeout: enrolled([A, B]), repos: [A, B], open: { [A]: [7, 8], [B]: [9, 1] },
    limits: { maxSweepItems: 1, maxRequests: 8 } })
  for (const number of [7, 8, 9, 1]) h.snapshots.set(number, { isDraft: true })
  const firstCalls = []
  const visited = []
  for (let pass = 0; pass < 4; pass++) {
    h.calls.length = 0
    const result = await h.pass()
    assert.ok(result.events, 'primary event stream is still admitted')
    assert.equal(result.requests, h.calls.length)
    firstCalls.push(h.calls[0][0])
    visited.push(...h.kinds('primary').map((v) => v.slice(1)))
  }
  assert.deepEqual(firstCalls, ['http', 'list', 'http', 'primary'], 'existing alternating stream admission')
  assert.deepEqual(visited, [[B, 9], [A, 7], [B, 1], [A, 8]], 'existing sorted repository cursor prevents starvation')
})

test('normal stream alternation cannot permanently starve an enrolled repository', async (t) => {
  const h = await fixture(t, { closeout: enrolled([A, B]), repos: [A, B], open: { [A]: [7], [B]: [8] },
    limits: { maxSweepItems: 1, maxPages: 1, maxRequests: 100 } })
  for (const number of [7, 8]) h.snapshots.set(number, { isDraft: true })
  for (let pass = 0; pass < 8; pass++) {
    t.mock.method(Date, 'now', () => wall + pass * 600001)
    const result = await h.pass()
    assert.equal(result.events.ok, true)
    assert.notEqual(result.reason, 'request-budget')
    assert.equal(result.sweeps.processed, 1)
  }
  for (const repository of [A, B]) {
    assert.equal(h.kinds('primary').filter(([, repo]) => repo === repository).length, 4)
    assert.equal(h.kinds('snapshot').filter(([, repo]) => repo === repository).length, 4,
      'every scheduled repository observes surplus under natural stream alternation')
  }
})

test('a warm index and later unenrolled repository cannot suppress disarm observations', async (t) => {
  const h = await fixture(t, { closeout: { ...enrolled([B]), shadow: { enabledRepositories: [B], indexReadsPerPass: 1 } },
    repos: [A, B], open: { [A]: [7], [B]: [8] }, limits: { maxPages: 1 } })
  h.rulesRepositories.add(B)
  h.snapshots.set(8, { isDraft: true })
  for (let pass = 0; pass < 5; pass++) {
    t.mock.method(Date, 'now', () => wall + pass * 600001)
    h.logs.length = 0
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(h.logs.find((line) => line.event === 'closeout-index').state, 'index-warm')
    assert.equal(result.disarmShadow.counters.wouldDisarm, 1,
      'warm index and an unrelated later repository do not block a scheduled observation')
  }
  assert.equal(h.kinds('rules').every(([, repository]) => repository === B), true)
})

test('later enrolled S3c whole-list fanout retains the shared budget before disarm', async (t) => {
  const results = []
  const later = Array.from({ length: 20 }, (_, i) => i + 10)
  for (const enabled of [false, true]) {
    const h = await fixture(t, { closeout: { shadow: { enabledRepositories: [B], indexReadsPerPass: 1, debounceMs: 1000 },
      ...(enabled ? enrolled() : {}) }, repos: [A, B], open: { [A]: [7], [B]: later }, limits: { maxPages: 1 } })
    h.rulesRepositories.add(B)
    assert.equal((await h.pass()).ok, true)
    assert.equal((await h.pass()).ok, true) // Normal review records the debounce candidates.
    t.mock.method(Date, 'now', () => wall + 1001)
    await h.rewindReview()
    // Force the disarm-enrolled A before B's due whole-list S3c review. The
    // natural setup cursor would run B first and hide the starvation regression.
    const storage = { directory: h.config.stateDirectory, namespace: receiptNamespace(h.config) }
    const schedule = await loadPassSchedule(storage)
    schedule.sweepCursor = B
    await savePassSchedule({ ...storage, state: schedule })
    h.config.limits.maxRequests = 30
    h.config.limits.maxSweepItems = 2
    h.calls.length = 0
    h.logs.length = 0
    const result = await h.pass()
    assert.equal(result.reason, 'sweeps-incomplete', 'bounded primary review stays partial, not request-budget failed')
    assert.equal(result.requests, 30)
    assert.equal(result.events.ok, true)
    assert.equal(result.sweeps.processed, 2)
    assert.equal(h.logs.find((line) => line.event === 'closeout-index').state, 'index-warm')
    assert.ok(h.calls.findIndex((v) => v[0] === 'primary' && v[1] === A) <
      h.calls.findIndex((v) => v[0] === 'snapshot' && v[1] === B), 'A reconciliation precedes the LATER S3c fanout')
    assert.deepEqual(h.kinds('snapshot'), later.map((number) => ['snapshot', B, number]), 'all due S3c refs run before disarm')
    assert.equal(result.sweeps.closeoutShadow[B].pending.due, 20, 'exercise actual unbounded later review fanout')
    if (enabled) assert.equal(result.disarmShadow.counters.deferred, 1)
    results.push({ events: result.events, sweeps: result.sweeps, bytes: await stored(h) })
  }
  assert.deepEqual(results[1], results[0], 'S6c does not change S3c or primary outcomes/checkpoints')
})

for (const indexState of ['warming', 'unavailable']) {
  test(`disarm is independent of the S3d index when ${indexState}`, async (t) => {
    const h = await fixture(t, { closeout: { ...enrolled(), shadow: { enabledRepositories: [A], indexReadsPerPass: 1 } } })
    h.snapshots.set(7, { isDraft: true })
    h.fetchHook = async (url) => {
      if (url.startsWith(h.config.captureOrigin)) return null
      if (indexState === 'unavailable') throw new Error(privateText)
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/work-products') || parsed.searchParams.has('afterId')) return Response.json([])
      return Response.json([{ id: '11111111-1111-4111-8111-111111111111', companyId, identifier: 'T\x4fG-7', status: 'in_progress' },
        { id: '22222222-2222-4222-8222-222222222222', companyId, identifier: 'T\x4fG-8', status: 'in_progress' }])
    }
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(h.logs.find((line) => line.event === 'closeout-index').state,
      indexState === 'warming' ? 'index-warming' : 'index-unavailable', 'prove the intended index state was reached')
    assert.equal(result.disarmShadow.counters.wouldDisarm, 1, 'no warmth/mapping/author/debounce prerequisite')
    assert.equal(h.summaryLogs().length, 1)
  })
}

test('configuration rejects keys, modes, wrong types and out-of-allowlist repositories at load AND run', async (t) => {
  const h = await fixture(t)
  for (const path of [h.config.captureTokenFile, h.config.boardTokenFile]) await writeFile(path, 'fixture', { mode: 0o600 })
  const configPath = join(h.root, 'consumer.json')
  const valid = { ...h.config, closeout: enrolled() }
  await writeFile(configPath, JSON.stringify(valid), { mode: 0o600 })
  assert.deepEqual((await loadConsumerConfig(configPath)).config.closeout, enrolled())
  assert.equal(closeoutDisarmSettings(h.config), null)
  assert.equal(closeoutDisarmSettings({ ...h.config, closeout: enrolled([]) }), null)
  const bad = [null, [], 'shadow', {}, { enabledRepositories: A }, { enabledRepositories: [A, A] },
    { enabledRepositories: [B] }, { enabledRepositories: ['../bad'] }, { enabledRepositories: ['X/..'] },
    ...['live', 'apply', 'enforce', 'shadow'].map((mode) => ({ enabledRepositories: [A], mode })),
    { enabledRepositories: [A], live: false }, { enabledRepositories: [A], enabled: false }]
  for (const disarmShadow of bad) {
    const config = { ...h.config, closeout: { disarmShadow } }
    assert.throws(() => closeoutShadowSettings(config), /closeout/)
    await assert.rejects(runProductPass({ config }, { fetchImpl: h.fetch, run: h.run }), /closeout/)
    await writeFile(configPath, JSON.stringify(config), { mode: 0o600 })
    await assert.rejects(loadConsumerConfig(configPath), /configuration/)
  }
  assert.deepEqual(h.calls, [], 'invalid configuration refuses before transport effects')
})

test('bounded summaries never contain helper records, arbitrary fields or transport error output', async (t) => {
  const numbers = Array.from({ length: 60 }, (_, i) => i + 1)
  const h = await fixture(t, { closeout: enrolled(), open: { [A]: numbers }, limits: { maxSweepItems: 60 } })
  for (const number of numbers) h.snapshots.set(number, { isDraft: true })
  const result = await h.pass()
  assert.equal(result.disarmShadow.counters.wouldDisarm, 60)
  assert.equal(h.summaryLogs().length, 1)
  assert.deepEqual(Object.keys(h.summaryLogs()[0]), ['event', 'counters', 'byReason'])
  assert.ok(JSON.stringify(h.summaryLogs()[0]).length < 1024, 'fixed summary size, not one record per PR')
  assert.equal(JSON.stringify(h.logs).includes(privateText), false)
  assert.equal(JSON.stringify(result.disarmShadow).includes(privateText), false)
})

test('a sweeps-first S3c index overrun after primary work still fails ON exactly as OFF', async (t) => {
  const outcomes = []
  for (const disarm of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout: { shadow: { enabledRepositories: [B], indexReadsPerPass: 1 }, ...disarm },
      repos: [A, B], open: { [A]: [] }, limits: { maxPages: 1, maxSweepItems: 1 } })
    h.rulesRepositories.add(B)
    h.snapshots.set(7, { isDraft: true })
    // Force the deterministic sweeps-first schedule the neighbors use: a
    // resumed review drain is admitted before the event stream, so primary
    // work for A/7 (which schedules the shadow ref) completes before the
    // held index read can stop the pass ahead of the shadow drain.
    assert.equal((await h.pass()).ok, true, 'setup pass succeeds')
    const state = await h.state()
    Object.assign(state.repos[A].review, { phase: 'draining', refs: [7], done: [], listedMs: wall, lastCompleteMs: null })
    await saveSweepState({ directory: h.config.stateDirectory, state })
    const boardIssues = `${h.config.boardOrigin}/api/companies/${companyId}/issues`
    const fetch = h.fetch
    // Wide real-time margins: instant fixture primary work must finish and
    // the held index read must start well inside the pass budget, while the
    // hold itself overruns it, even on a loaded runner.
    h.fetch = async (url, init) => {
      if (String(url).startsWith(boardIssues)) await new Promise((resolve) => setTimeout(resolve, 2000))
      return fetch(url, init)
    }
    h.config.limits.durationMs = 1200
    h.calls.length = 0
    h.logs.length = 0
    const result = await h.pass()
    assert.deepEqual(h.kinds('primary'), [['primary', A, 7]],
      'measured pass re-reviews A primary before the held index read')
    assert.ok(h.calls.findIndex((v) => v[0] === 'primary') <
      h.calls.findIndex((v) => v[0] === 'http' && String(v[2]).startsWith(boardIssues)),
      'primary work precedes the index overrun')
    assert.equal(result.ok, false, 'a pre-drain stop fails the pass with or without the shadow')
    assert.equal(result.reason, 'deadline')
    assert.deepEqual(h.kinds('snapshot'), [], 'no shadow drain read runs after a pre-drain stop')
    if (disarm) {
      assert.equal(result.disarmShadow.counters.total, 0, 'no drain accounting for a stop set before the drain')
      assert.equal(result.disarmShadow.counters.deferred, 0, 'no drain read ran, so nothing is contained')
      assert.equal(result.disarmShadow.counters.wouldDisarm, 0)
      assert.deepEqual(h.summaryLogs(), [], 'a stopped pass emits no shadow summary')
    }
    outcomes.push({ ok: result.ok, reason: result.reason, processed: result.sweeps.processed })
  }
  assert.deepEqual(outcomes[1], outcomes[0], 'ON hides no pre-drain failure that OFF reports')
})

test('an unexpected helper failure is observational and counted, not swallowed or product-poisoned', async () => {
  const hook = createDisarmShadowHook({ settings: { enabledRepositories: new Set([A]) },
    getCloseoutSnapshot: async () => null, check: () => {} })
  await hook.observe({ repository: A, number: 7 })
  assert.equal(hook.summary().counters.errors, 1)
  assert.equal(hook.summary().byReason['observation-failed'], 1)
})
