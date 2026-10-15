import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile, rm, stat, symlink, readdir } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { runProductPass } from '../src/consumer-runner.js'
import { receiptNamespace } from '../src/receipt-cycle.js'
import { loadConsumerConfig } from '../src/consumer-config.js'
import { INDEX_FILE, loadPrTaskIndex, refreshPrTaskIndex } from '../src/pr-task-index-source.js'
import { pendingNamespace, freshPendingState, recordObservation, savePendingState } from '../src/closeout-pending.js'
import { freshSweep, loadSweepState, saveSweepState } from '../src/sweep-store.js'
import { closeoutShadowSettings } from '../src/closeout-shadow-wiring.js'
import { createApp } from '../src/app.js'
import { createMemoryStore } from '../src/store-memory.js'

const REPO = 'TogetherWeOwn/two-web-next'
const OTHER = 'Other/not-allowed'
const COMPANY = '00000000-0000-4000-8000-000000000004'
const SECRET = 'TEST-PRIVATE-KEY'
const id = (n) => `11111111-1111-4111-8111-${String(n).padStart(12, '0')}`
const issue = (n) => ({ id: id(n), identifier: `TOG-${n}`, status: 'in_progress', companyId: COMPANY })
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const row = (n, externalId, url) => ({ id: id(10000 + n), issueId: id(n), companyId: COMPANY,
  provider: 'github', type: 'pull_request', title: SECRET, externalId, url,
  status: 'active', reviewState: 'none', metadata: { untrusted: SECRET } })

async function harness(t, { count = 3, reads = 2, enabled = true, numbers = [7, 8, 9, 10] } = {}) {
  const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'pr-index-source-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const config = { version: 1, mode: 'products-only-v1', captureOrigin: 'https://capture.test',
    boardOrigin: 'https://board.test', companyId: COMPANY, allowedRepositories: [REPO], stateDirectory: join(root, 'state'),
    captureTokenFile: join(root, 'capture.key'), boardTokenFile: join(root, 'board.key'), limits: {},
    ...(enabled ? { closeout: { shadow: { enabledRepositories: [REPO], indexReadsPerPass: reads } } } : {}) }
  const app = createApp({ store: createMemoryStore(), webhookSecret: 'test', queryToken: SECRET, now: () => 1000 })
  const h = { config, root, issues: Array.from({ length: count }, (_, n) => issue(n + 1)), products: new Map(),
    calls: [], gh: [], logs: [], failList: false, failReads: new Set() }
  h.products.set(id(1), [row(1, `${REPO}#7`, `https://github.com/${REPO}/pull/7`)])
  h.products.set(id(2), [row(2, null, `https://github.com/${REPO}/pull/8`)])
  h.products.set(id(3), [row(3, `${REPO}#9`, `https://github.com/${REPO}/pull/90`)])
  h.fetch = async (url, init) => {
    h.calls.push([init.method, url])
    assert.equal(init.method, 'GET', 'index/shadow must never issue any write or comment')
    assert.equal(init.body, undefined)
    assert.equal(init.signal.aborted, false)
    if (url.startsWith(config.captureOrigin)) return app(new Request(url, init))
    const parsed = new URL(url)
    if (parsed.pathname === `/api/companies/${COMPANY}/issues`) {
      assert.equal(parsed.searchParams.get('status'), 'todo,in_progress,in_review,blocked')
      assert.equal(parsed.searchParams.get('limit'), '500', 'never assume a larger limit is honored')
      assert.equal(parsed.searchParams.get('includePluginOperations'), 'true')
      assert.equal(parsed.searchParams.get('includeRoutineExecutions'), 'true')
      assert.equal(parsed.searchParams.get('sortField'), 'id')
      assert.equal(parsed.searchParams.get('sortDir'), 'asc')
      assert.equal(parsed.searchParams.get('view'), 'compact')
      const afterId = parsed.searchParams.get('afterId')
      return h.failList ? json({ secret: SECRET }, 500)
        : json(h.issues.filter((i) => afterId === null || i.id > afterId).sort((a, b) => a.id.localeCompare(b.id)).slice(0, 500))
    }
    const match = /^\/api\/issues\/([a-f0-9-]+)\/work-products$/.exec(parsed.pathname)
    assert.ok(match, 'only list and work-product reads may reach the board')
    if (h.failReads.has(match[1])) throw new Error(SECRET)
    return json(h.products.get(match[1]) ?? [])
  }
  h.run = async (_file, args) => {
    assert.equal(args[0], 'api', 'no GitHub mutation')
    assert.equal(args.includes('POST') || args.includes('PATCH'), false)
    if (args.includes('--method')) {
      h.gh.push('rules')
      return { stdout: JSON.stringify([{ type: 'required_status_checks', parameters: {
        required_status_checks: [{ context: 'ci-ok' }, { context: 'pr-lint' }] } }]) }
    }
    const query = args[5]
    const repository = { nameWithOwner: REPO, isPrivate: false }
    if (query.includes('BridgeOpenPullRequests')) {
      h.gh.push('list')
      repository.pullRequests = { totalCount: numbers.length, nodes: numbers.map((number) => ({ number })),
        pageInfo: { hasNextPage: false, endCursor: null } }
    } else {
      const number = Number(args.find((a) => a.startsWith('number=')).slice(7))
      h.gh.push(query.includes('BridgeCloseoutSnapshot') ? 'snapshot' : 'pr')
      repository.pullRequest = { number, url: `https://github.com/${REPO}/pull/${number}`, title: SECRET, body: SECRET,
        state: 'OPEN', isDraft: false, merged: false, headRefName: 'feature/no-internal-ref', headRefOid: 'a'.repeat(40),
        reviewDecision: null, mergeStateStatus: 'DIRTY', baseRefName: 'main', autoMergeRequest: null,
        author: { __typename: 'User', login: 'human' }, baseRepository: { nameWithOwner: REPO },
        labels: { nodes: [], pageInfo: { hasNextPage: false } }, commits: { nodes: [{ commit: {
          oid: 'a'.repeat(40), pushedDate: '2026-10-03T07:00:00.000Z',
          statusCheckRollup: { contexts: { nodes: [], pageInfo: { hasNextPage: false } } } } }] } }
    }
    return { stdout: JSON.stringify({ data: { repository } }) }
  }
  h.pass = (options = {}) => runProductPass({ config, captureToken: SECRET, boardToken: SECRET },
    { fetchImpl: h.fetch, run: h.run, log: (line) => h.logs.push(JSON.parse(line)), ...options })
  h.cache = () => readFile(join(config.stateDirectory, INDEX_FILE), 'utf8').then(JSON.parse)
  h.indexLine = () => h.logs.findLast((line) => line.event === 'closeout-index')
  h.seedDue = async () => {
    await mkdir(config.stateDirectory, { recursive: true, mode: 0o700 })
    const state = freshPendingState(pendingNamespace(receiptNamespace(config)))
    for (const number of numbers) recordObservation(state, { repository: REPO, number }, 0)
    await savePendingState({ directory: config.stateDirectory, state })
    // Rearm the review listing too; repeated passes otherwise wait ten minutes.
    const storage = { directory: config.stateDirectory, namespace: receiptNamespace(config) }
    const sweeps = await loadSweepState(storage)
    if (sweeps.repos[REPO]) {
      sweeps.repos[REPO].review = freshSweep()
      await saveSweepState({ ...storage, state: sweeps })
    }
  }
  h.reads = () => h.calls.filter(([, url]) => url.endsWith('/work-products')).map(([, url]) => url.split('/').at(-2))
  return h
}

test('disabled index adds no board reads, result keys or namespace changes', async (t) => {
  const h = await harness(t, { enabled: false })
  const namespace = receiptNamespace(h.config)
  const first = await h.pass()
  assert.equal(first.ok, true)
  assert.equal(h.calls.some(([, url]) => url.startsWith(h.config.boardOrigin)), false)
  assert.deepEqual(Object.keys(first).sort(), ['events', 'mode', 'ok', 'reason', 'requests', 'sweeps'])
  assert.equal('closeoutShadow' in first.sweeps, false)
  assert.deepEqual(h.logs, [])
  const baseline = { keys: Object.keys(first).sort(), sweepKeys: Object.keys(first.sweeps).sort() }
  h.config.closeout = { shadow: { enabledRepositories: [], indexReadsPerPass: 1 } }
  const off = await h.pass()
  assert.equal(h.calls.some(([, url]) => url.startsWith(h.config.boardOrigin)), false)
  assert.deepEqual({ keys: Object.keys(off).sort(), sweepKeys: Object.keys(off.sweeps).sort() }, baseline)
  assert.equal(receiptNamespace(h.config), namespace)
  h.config.closeout.shadow.enabledRepositories = [REPO]
  assert.equal(receiptNamespace(h.config), namespace)
})

test('warm-up gate skips partial index; read cap and shared budget are enforced', async (t) => {
  const h = await harness(t)
  const first = await h.pass()
  assert.equal(first.ok, true)
  assert.equal(h.calls.filter(([, url]) => url.includes('/companies/')).length, 2)
  assert.deepEqual(h.reads(), [id(1), id(2)])
  assert.equal(h.indexLine().state, 'index-warming')
  assert.equal(h.indexLine().readCount, 2)
  assert.equal(h.indexLine().openCount, 3)
  assert.equal('closeoutShadow' in first.sweeps, false)
  assert.equal(h.gh.includes('snapshot') || h.gh.includes('rules'), false)
  assert.equal(first.requests, h.calls.length + h.gh.length, 'every index read counts in the pass budget')
  assert.equal((await stat(join(h.config.stateDirectory, INDEX_FILE))).mode & 0o777, 0o600)
  assert.equal((await readdir(h.config.stateDirectory)).some((p) => p.endsWith('.tmp')), false)
})

test('warm index maps externalId and URL-only rows; conflicts and public keyless PRs stay unmapped', async (t) => {
  const h = await harness(t)
  await h.pass()
  await h.seedDue()
  const second = await h.pass()
  assert.equal(second.ok, true)
  assert.deepEqual(h.reads().slice(2), [id(3), id(1)], 'unread precedes the oldest read entry')
  assert.equal(h.indexLine().state, 'index-warm')
  assert.equal(h.indexLine().conflict, 1)
  assert.equal(h.indexLine().keyed, 2)
  const shadow = second.sweeps.closeoutShadow[REPO]
  assert.deepEqual(shadow.records.map((r) => r.mapping), [
    { issueRef: 'T\x4fG-1', source: 'work-product' }, { issueRef: 'T\x4fG-2', source: 'work-product' },
    { unmapped: 'no-work-product' }, { unmapped: 'no-work-product' },
  ])
  assert.equal(shadow.counters.wouldClaim, 2)
  assert.equal(h.gh.filter((s) => s === 'snapshot').length, 4)
  assert.equal(h.logs.some((line) => JSON.stringify(line).includes(SECRET)), false)
  assert.equal((await readFile(join(h.config.stateDirectory, INDEX_FILE), 'utf8')).includes(SECRET), false)
  assert.equal((await readdir(h.config.stateDirectory)).includes('claims.json'), false)
})

test('short identity pages cannot hide a conflicting claimant', async (t) => {
  const h = await harness(t, { count: 3, reads: 3, numbers: [7] })
  h.products.set(id(3), [row(3, `${REPO}#7`, null)])
  const fetch = h.fetch
  h.fetch = async (url, init) => {
    const response = await fetch(url, init)
    return url.includes('/companies/') ? json((await response.json()).slice(0, 2)) : response
  }
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.indexLine().openCount, 3)
  await h.seedDue()
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(h.indexLine().openCount, 3)
  assert.equal(h.indexLine().readCount, 3)
  assert.equal(h.indexLine().ambiguous, 1)
  assert.deepEqual(result.sweeps.closeoutShadow[REPO].records[0].mapping, { unmapped: 'ambiguous' })
})

test('URL derivation refuses non-allowed and noncanonical repositories without guessing metadata', async (t) => {
  const h = await harness(t, { count: 1, reads: 1 })
  h.products.set(id(1), [
    row(1, null, `https://github.com/${OTHER}/pull/7`),
    { ...row(1, null, `https://github.com/${REPO}/pull/8?query=1`), id: id(20001) },
    { ...row(1, null, null), id: id(20002), metadata: { repo: REPO, number: 9, title: `${REPO}#9` } },
  ])
  assert.equal((await h.pass()).ok, true)
  assert.deepEqual((await h.cache()).entries[0].keys, [])
  assert.equal(h.indexLine().noKey, 3)
  assert.equal(h.indexLine().viaUrl, 0)
})

test('an issue leaving the open set is dropped; newly-open issues are read before stale entries', async (t) => {
  const h = await harness(t, { count: 2, reads: 2 })
  await h.pass()
  h.config.closeout.shadow.indexReadsPerPass = 1
  h.issues = [issue(2), issue(4)]
  h.products.set(id(4), [row(4, `${REPO}#10`, null)])
  await h.seedDue()
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.deepEqual(h.reads().slice(2), [id(4)])
  assert.deepEqual((await h.cache()).entries.map((e) => e.issueId), [id(2), id(4)])
  assert.equal(result.sweeps.closeoutShadow[REPO].records[0].mapping.unmapped, 'no-work-product')
  assert.equal(h.indexLine().readCount, 2)
})

test('list failure serves the last good warm cache; no cache skips as unavailable, never fails ok', async (t) => {
  const h = await harness(t, { count: 2, reads: 2 })
  await h.pass()
  await h.seedDue()
  const before = await readFile(join(h.config.stateDirectory, INDEX_FILE), 'utf8')
  h.failList = true
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.sweeps.closeoutShadow[REPO].counters.mapped, 2)
  assert.equal(h.indexLine().indexRefreshErrors, 1)
  assert.equal(h.indexLine().refreshReads, 0)
  assert.equal(await readFile(join(h.config.stateDirectory, INDEX_FILE), 'utf8'), before)
  const cold = await harness(t)
  cold.failList = true
  const unavailable = await cold.pass()
  assert.equal(unavailable.ok, true)
  assert.equal(cold.indexLine().state, 'index-unavailable')
  assert.equal('closeoutShadow' in unavailable.sweeps, false)
  assert.deepEqual(cold.reads(), [])
})

test('a failed work-product read retains its last good keys and time; unread failures stay warming', async (t) => {
  t.mock.method(Date, 'now', () => Date.parse('2026-10-07T00:00:00.000Z'))
  const h = await harness(t, { count: 2, reads: 2 })
  await h.pass()
  const before = await h.cache()
  h.products.set(id(1), [])
  h.failReads.add(id(1))
  await h.seedDue()
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.sweeps.closeoutShadow[REPO].counters.mapped, 2)
  assert.deepEqual((await h.cache()).entries[0], before.entries[0])
  assert.equal(h.indexLine().indexRefreshErrors, 1)
  const cold = await harness(t, { count: 1, reads: 1 })
  cold.failReads.add(id(1))
  assert.equal((await cold.pass()).ok, true)
  assert.equal(cold.indexLine().state, 'index-warming')
  assert.equal(cold.indexLine().readCount, 0)
  const reads = cold.reads().length
  assert.equal((await cold.pass()).ok, true)
  assert.equal(cold.indexLine().refreshReads, 0, 'an unexpired retry is not attempted even without alternatives')
  assert.equal(cold.reads().length, reads)
})

test('failed read cooldown starts at failure time and remains unexpired on the next pass', async (t) => {
  let now = Date.parse('2026-10-07T00:00:00.000Z')
  t.mock.method(Date, 'now', () => now)
  const h = await harness(t, { count: 1, reads: 1, numbers: [] })
  h.config.limits.durationMs = 300000
  const fetch = h.fetch
  h.fetch = async (url, init) => {
    if (url.endsWith('/work-products')) now += 70000
    return fetch(url, init)
  }
  h.failReads.add(id(1))

  assert.equal((await h.pass()).ok, true)
  const cache = await h.cache()
  assert.equal(cache.builtAt, now)
  assert.equal(cache.retryAfter[id(1)], now + 60000)
  const reads = h.reads().length
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.indexLine().refreshReads, 0)
  assert.equal(h.reads().length, reads)

  now += 60000
  h.failReads.delete(id(1))
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.reads().length, reads + 1)
  assert.deepEqual((await h.cache()).retryAfter, {})
})

test('failed read cooldown survives rollback before save and across passes', async (t) => {
  const startedAt = Date.parse('2026-10-07T00:00:00.000Z')
  let now = startedAt
  let inFailedRead = false
  let lateSamples = 0
  t.mock.method(Date, 'now', () => {
    if (!inFailedRead) return now
    return lateSamples++ === 0 ? startedAt + 70000 : now + 30000
  })
  const h = await harness(t, { count: 1, reads: 1, numbers: [] })
  const fetch = h.fetch
  h.fetch = async (url, init) => {
    if (url.endsWith('/work-products')) inFailedRead = true
    return fetch(url, init)
  }
  h.failReads.add(id(1))

  assert.equal((await h.pass()).ok, true)
  assert.equal(h.indexLine().indexRefreshErrors, 1, 'the retry cooldown cache must persist')
  const cache = await h.cache()
  assert.equal(cache.builtAt, startedAt + 70000)
  assert.equal(cache.retryAfter[id(1)], startedAt + 130000)
  assert.ok(await loadPrTaskIndex({ directory: h.config.stateDirectory, namespace: receiptNamespace(h.config) }))

  const reads = h.reads().length
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.indexLine().refreshReads, 0, 'the validated cache high-water must survive the rollback')
  assert.equal(h.reads().length, reads)
  now += 100000
  h.failReads.delete(id(1))
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.reads().length, reads + 1, 'retry becomes due after the cooldown')
  assert.deepEqual((await h.cache()).retryAfter, {})
})

test('far-future valid cache does not pin retry eligibility', async (t) => {
  let now = Date.parse('2026-10-07T00:00:00.000Z')
  t.mock.method(Date, 'now', () => now)
  const h = await harness(t, { count: 1, reads: 1, numbers: [] })
  h.failReads.add(id(1))
  assert.equal((await h.pass()).ok, true)
  const cache = await h.cache()
  cache.builtAt += 24 * 60 * 60 * 1000
  cache.retryAfter[id(1)] = cache.builtAt + 60000
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), JSON.stringify(cache), { mode: 0o600 })
  assert.ok(await loadPrTaskIndex({ directory: h.config.stateDirectory, namespace: receiptNamespace(h.config) }))

  h.failReads.delete(id(1))
  const reads = h.reads().length
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.reads().length, reads + 1, 'an unreasonable future cache cannot defer the retry')
  assert.equal(h.indexLine().warm, true)
})

test('corrupt cache re-warms without throwing or serving a valid-looking partial entry', async (t) => {
  const h = await harness(t, { count: 2, reads: 2 })
  await h.pass()
  const cache = await h.cache()
  cache.entries[1].extra = 'unrecognized'
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), JSON.stringify(cache), { mode: 0o600 })
  h.config.closeout.shadow.indexReadsPerPass = 1
  await h.seedDue()
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(h.indexLine().state, 'index-warming')
  assert.equal(h.indexLine().readCount, 1)
  assert.equal('closeoutShadow' in result.sweeps, false)
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), '{invalid json', { mode: 0o600 })
  h.failList = true
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.indexLine().state, 'index-unavailable')
})

test('cache reads allocate only file size plus a growth sentinel', async (t) => {
  const h = await harness(t, { count: 1, reads: 1 })
  await h.pass()
  const size = (await stat(join(h.config.stateDirectory, INDEX_FILE))).size
  const allocate = Buffer.alloc
  const sizes = []
  t.mock.method(Buffer, 'alloc', (bytes, ...args) => { sizes.push(bytes); return allocate(bytes, ...args) })
  assert.ok(await loadPrTaskIndex({ directory: h.config.stateDirectory, namespace: receiptNamespace(h.config) }))
  assert.deepEqual(sizes, [size + 1])
})

test('key capacity is enforced before serialization and index construction', async (t) => {
  const h = await harness(t, { count: 60, reads: 60, numbers: [] })
  await mkdir(h.config.stateDirectory, { mode: 0o700 })
  const encode = JSON.stringify
  let largestEncoding = 0
  t.mock.method(JSON, 'stringify', (...args) => {
    const text = encode(...args)
    largestEncoding = Math.max(largestEncoding, Buffer.byteLength(text))
    return text
  })
  const repository = `${REPO}-${'x'.repeat(350)}`
  const board = { listOpenIssues: async () => h.issues,
    listWorkProducts: async (issueId) => Array.from({ length: 1000 }, (_, i) => ({
      type: 'pull_request', provider: 'github', url: null,
      externalId: `${repository}#${Number(issueId.slice(-12)) * 1000 + i + 1}`,
    })) }
  const args = { board, directory: h.config.stateDirectory, namespace: receiptNamespace(h.config),
    allowedRepositories: [repository], nowMs: 0 }
  const result = await refreshPrTaskIndex(args)
  assert.equal(result.index === null, true, 'an over-cap issue must not make the index warm')
  assert.ok(result.summary.readCount > 0 && result.summary.readCount < 60)
  assert.ok(result.summary.indexRefreshErrors > 0)
  assert.ok((await stat(join(h.config.stateDirectory, INDEX_FILE))).size <= 16 * 1024 * 1024)
  const cache = await h.cache()
  assert.ok(cache.entries.filter((entry) => entry.readAt === null).every((entry) => entry.keys.length === 0))
  // A now-warm cache near capacity must not hide newly listed unread issues
  // when adding their metadata would exceed the same conservative budget.
  cache.entries = cache.entries.filter((entry) => entry.readAt !== null)
  cache.retryAfter = {}
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), JSON.stringify(cache))
  assert.ok(await loadPrTaskIndex(args))
  h.issues = Array.from({ length: 5000 }, (_, i) => issue(i + 1))
  const expanded = await refreshPrTaskIndex(args)
  assert.equal(expanded.index === null, true, 'known unread membership cannot fall back to a warm subset')
  assert.equal(expanded.summary.refreshReads, 0)
  assert.equal(expanded.summary.indexRefreshErrors, 1)
  assert.ok(largestEncoding <= 16 * 1024 * 1024, 'never serialize an over-cap state')
})

test('strict cache loads reject namespace mismatch, bad keys, unsafe files and symlinks', async (t) => {
  const h = await harness(t, { count: 1, reads: 1 })
  await h.pass()
  const path = join(h.config.stateDirectory, INDEX_FILE)
  const args = { directory: h.config.stateDirectory, namespace: receiptNamespace(h.config) }
  const original = await h.cache()
  for (const mutate of [
    (s) => { s.namespace = 'b'.repeat(64) }, (s) => { s.extra = true },
    (s) => { s.entries.push(s.entries[0]) }, (s) => { s.entries[0].status = 'done' },
    (s) => { s.entries[0].readAt = s.builtAt + 1 }, (s) => { s.entries[0].keys = ['not-a-key'] },
    (s) => { s.entries[0].readAt = null },
    (s) => { s.entries[0].issueId = [s.entries[0].issueId] },
    (s) => { s.entries[0].identifier = [s.entries[0].identifier] },
    (s) => { s.retryAfter = [] },
    (s) => { s.retryAfter[id(99)] = s.builtAt },
    (s) => { s.retryAfter[id(1)] = s.builtAt + 60001 },
    (s) => { s.retryAfter[id(1)] = 'invalid' },
  ]) {
    const bad = structuredClone(original); mutate(bad)
    await writeFile(path, JSON.stringify(bad), { mode: 0o600 })
    assert.equal(await loadPrTaskIndex(args), null)
  }
  await rm(path)
  const target = join(h.root, 'target')
  await writeFile(target, JSON.stringify(original), { mode: 0o600 })
  await symlink(target, path)
  assert.equal(await loadPrTaskIndex(args), null)
  const before = await readFile(target, 'utf8')
  assert.equal((await h.pass()).ok, true)
  assert.equal(await readFile(target, 'utf8'), before, 'atomic rename never follows the rejected symlink')
})

test('identity pagination includes the 1001st claimant and refuses false unique mapping', async (t) => {
  const h = await harness(t, { count: 1001, reads: 150, numbers: [7] })
  h.config.limits.maxRequests = 500
  h.products.set(id(1001), [row(1001, null, `https://github.com/${REPO}/pull/7`)])
  for (let n = 0; n < 7; n++) assert.equal((await h.pass()).ok, true)
  assert.equal(h.indexLine().readCount, 1001)
  assert.equal(h.indexLine().ambiguous, 1)
  await h.seedDue()
  const result = await h.pass()
  assert.equal(result.sweeps.closeoutShadow[REPO].records[0].mapping.unmapped, 'ambiguous')
  assert.equal(h.calls.filter(([, url]) => url.includes('/companies/')).length, 32)
})

test('slow index refresh reserves time for both production streams on every pass', async (t) => {
  const h = await harness(t, { count: 60, reads: 60, numbers: [] })
  const fetch = h.fetch
  let now = 0
  h.fetch = async (url, init) => {
    const result = await fetch(url, init)
    if (url.endsWith('/work-products')) now += 1000
    return result
  }
  const clock = { now: () => now, setTimer: () => ({}), clearTimer: () => {} }
  for (let n = 0; n < 6; n++) {
    now = 0
    const before = h.calls.length
    const beforeGh = h.gh.length
    const result = await h.pass({ clock })
    assert.equal(result.ok, true)
    assert.notEqual(result.events, null)
    assert.notEqual(result.sweeps, null)
    assert.ok(h.calls.slice(before).some(([, url]) => url.startsWith(h.config.captureOrigin)))
    assert.ok(h.indexLine().refreshReads <= 12)
    assert.equal(result.requests, h.calls.slice(before).length + h.gh.length - beforeGh)
  }
  assert.equal(h.indexLine().warm, true)
})

test('in-flight index deadline aborts only its own fetch and leaves both production streams running', async (t) => {
  const h = await harness(t, { count: 2, reads: 2, numbers: [] })
  const fetch = h.fetch
  const timers = []
  const clock = { now: () => 0, setTimer: (fire, ms) => { const timer = { fire, ms }; timers.push(timer); return timer },
    clearTimer: () => {} }
  h.fetch = async (url, init) => {
    const result = await fetch(url, init)
    if (!url.endsWith('/work-products')) return result
    return new Promise((resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new Error(SECRET)), { once: true })
      timers.find((timer) => timer.ms === 11250).fire()
      h.indexAborted = init.signal.aborted
      if (!h.indexAborted) resolve(result)
    })
  }
  const result = await h.pass({ clock })
  assert.equal(h.indexAborted, true, 'the index timer must interrupt its in-flight fetch')
  assert.equal(result.ok, true)
  assert.notEqual(result.events, null)
  assert.notEqual(result.sweeps, null)
  assert.equal(h.indexLine().refreshReads, 1)
  assert.equal(h.indexLine().indexRefreshErrors, 1)
  assert.equal(h.indexLine().readCount, 0)
  assert.equal(h.indexLine().warm, false)
  assert.equal(result.requests, h.calls.length + h.gh.length)
  assert.equal(h.logs.some((line) => JSON.stringify(line).includes(SECRET)), false)
})

test('failed oldest entry cools down without changing its good read time or starving another entry', async (t) => {
  const h = await harness(t, { count: 2, reads: 2 })
  await h.pass()
  const cache = await h.cache()
  cache.entries[0].readAt -= 1000
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), JSON.stringify(cache))
  h.config.closeout.shadow.indexReadsPerPass = 1
  h.failReads.add(id(1))
  h.products.set(id(2), [])
  await h.pass()
  await h.seedDue()
  const result = await h.pass()
  assert.deepEqual(h.reads().slice(2), [id(1), id(2)])
  assert.deepEqual((await h.cache()).entries[0], cache.entries[0])
  assert.equal(h.indexLine().warm, true)
  assert.equal(result.sweeps.closeoutShadow[REPO].records[1].mapping.unmapped, 'no-work-product')
  const expired = await h.cache()
  expired.retryAfter[id(1)] = 0
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), JSON.stringify(expired))
  h.failReads.delete(id(1))
  h.products.set(id(1), [])
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.reads().at(-1), id(1), 'an expired cooldown retries the oldest entry')
  assert.deepEqual((await h.cache()).entries[0].keys, [])
  assert.deepEqual((await h.cache()).retryAfter, {})
})

test('failed oldest cannot starve another entry across passes sixty-one seconds apart', async (t) => {
  let now = Date.parse('2026-10-07T00:00:00.000Z')
  t.mock.method(Date, 'now', () => now)
  const h = await harness(t, { count: 2, reads: 2, numbers: [7, 8] })
  assert.equal((await h.pass()).ok, true)
  const cache = await h.cache()
  cache.entries[0].readAt = 100
  cache.entries[1].readAt = 200
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), JSON.stringify(cache))
  h.config.closeout.shadow.indexReadsPerPass = 1
  h.failReads.add(id(1))
  h.products.set(id(2), [])
  for (let pass = 0; pass < 4; pass++) {
    now += 61000
    assert.equal((await h.pass()).ok, true)
    assert.equal(h.indexLine().warm, true)
  }
  assert.deepEqual(h.reads().slice(2), [id(1), id(2), id(1), id(2)],
    'attempt ordering must survive a fully expired cooldown between passes')
  const final = await h.cache()
  assert.deepEqual(final.entries[0], cache.entries[0], 'failed reads never change good keys/time')
  assert.equal(final.entries[1].readAt, now)
  assert.deepEqual(final.entries[1].keys, [])
  await h.seedDue()
  const result = await h.pass()
  assert.equal(result.sweeps.closeoutShadow[REPO].records[1].mapping.unmapped, 'no-work-product')
})

test('recognizable external URL conflicts cannot be bypassed; unrecognized legacy IDs still allow URL fallback', async (t) => {
  const h = await harness(t, { count: 1, reads: 1, numbers: [7, 8] })
  h.products.set(id(1), [row(1, `https://github.com/${REPO}/pull/8`, `https://github.com/${REPO}/pull/7`),
    { ...row(1, 'legacy-opaque-id', `https://github.com/${REPO}/pull/8`), id: id(20001) }])
  assert.equal((await h.pass()).ok, true)
  await h.seedDue()
  const result = await h.pass()
  assert.deepEqual((await h.cache()).entries[0].keys, [`${REPO}#8`])
  assert.equal(h.indexLine().conflict, 1)
  assert.equal(h.indexLine().viaUrl, 1)
  assert.equal(result.sweeps.closeoutShadow[REPO].records[0].mapping.unmapped, 'no-work-product')
})

test('warm cache logs maximum age and entries older than six hours even when refresh fails', async (t) => {
  const h = await harness(t, { count: 2, reads: 2 })
  await h.pass()
  const cache = await h.cache()
  cache.builtAt = Date.now() - 7 * 60 * 60 * 1000
  cache.entries.forEach((e) => { e.readAt = cache.builtAt })
  await writeFile(join(h.config.stateDirectory, INDEX_FILE), JSON.stringify(cache), { mode: 0o600 })
  h.failList = true
  assert.equal((await h.pass()).ok, true)
  assert.equal(h.indexLine().warm, true)
  assert.equal(h.indexLine().olderThan6h, 2)
  assert.ok(h.indexLine().indexMaxAgeMs >= 7 * 60 * 60 * 1000)
  const throwing = await h.pass({ log: () => { throw new Error(SECRET) } })
  assert.equal(throwing.ok, true)
})

test('ambiguous claimants are counted and never resolved to a task', async (t) => {
  const h = await harness(t, { count: 2, reads: 2 })
  h.products.set(id(2), [row(2, null, `https://github.com/${REPO}/pull/7`)])
  await h.pass()
  assert.equal(h.indexLine().ambiguous, 1)
  assert.equal(h.indexLine().viaUrl, 1)
  await h.seedDue()
  const result = await h.pass()
  assert.deepEqual(result.sweeps.closeoutShadow[REPO].records[0].mapping, { unmapped: 'ambiguous' })
})

test('indexReadsPerPass is strict at load and pass start, below half and within pagination budget', async (t) => {
  const h = await harness(t)
  const settings = (reads, maxRequests = 300) => closeoutShadowSettings({ ...h.config, limits: { maxRequests },
    closeout: { shadow: { enabledRepositories: [REPO], indexReadsPerPass: reads } } })
  assert.equal(settings(1).indexReadsPerPass, 1)
  assert.equal(settings(149, 321).indexReadsPerPass, 149)
  assert.equal(settings(150, 323).indexReadsPerPass, 150)
  for (const reads of [0, -1, 151, 1.5, '60', null]) assert.throws(() => settings(reads), /closeout/)
  assert.throws(() => settings(150), /half/)
  assert.throws(() => settings(60, 120), /half/)
  await writeFile(h.config.captureTokenFile, SECRET, { mode: 0o600 })
  await writeFile(h.config.boardTokenFile, SECRET, { mode: 0o600 })
  const path = join(h.root, 'config.json')
  await writeFile(path, JSON.stringify(h.config), { mode: 0o600 })
  assert.equal((await loadConsumerConfig(path)).config.closeout.shadow.indexReadsPerPass, 2)
  h.config.closeout.shadow.indexReadsPerPass = 151
  await writeFile(path, JSON.stringify(h.config), { mode: 0o600 })
  await assert.rejects(loadConsumerConfig(path), /configuration/)
  await assert.rejects(h.pass(), /closeout/)
  assert.deepEqual(h.calls, [])
})

test('local measured 613-issue warm-up takes eleven passes at the default sixty reads', async (t) => {
  const h = await harness(t, { count: 613, reads: 60, numbers: [] })
  delete h.config.closeout.shadow.indexReadsPerPass
  const counts = []
  for (let pass = 0; pass < 11; pass++) {
    assert.equal((await h.pass()).ok, true)
    counts.push(h.indexLine().readCount)
    assert.equal(h.indexLine().refreshReads, 60)
    assert.equal(h.indexLine().warm, pass === 10)
  }
  assert.deepEqual(counts, [60, 120, 180, 240, 300, 360, 420, 480, 540, 600, 613])
  assert.equal(h.reads().length, 660)
})
