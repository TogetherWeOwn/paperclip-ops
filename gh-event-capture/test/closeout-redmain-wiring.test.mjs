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
import { loadSweepState } from '../src/sweep-store.js'
import { closeoutShadowSettings } from '../src/closeout-shadow-wiring.js'
import { closeoutRedmainSettings, createRedmainShadowHook, RedmainShadowDeferred } from '../src/closeout-redmain-wiring.js'

const A = 'TogetherWeOwn/paperclip-ops'
const B = 'TogetherWeOwn/kofra'
const companyId = '00000000-0000-4000-8000-000000000004'
const privateText = 'UNTRUSTED-PRIVATE-PAYLOAD'
const wall = Date.parse('2026-10-08T06:00:00Z')
const sha = (n) => String(n % 10).repeat(40)
const enrolled = (repos = [A]) => ({ redmainShadow: { enabledRepositories: repos } })
const envelope = (repository, fields) => ({ data: { repository: { nameWithOwner: repository, ...fields } } })
const check = (conclusion = 'failure') => ({ name: 'ci-ok', status: 'completed', conclusion })
const head = (conclusion = 'failure', repository = A) => ({ repository, branch: 'main', mainSha: sha(1),
  requiredChecks: ['ci-ok'], checks: [check(conclusion)] })
const headNode = (conclusion = 'FAILURE') => ({ name: 'main', target: { __typename: 'Commit', oid: sha(1),
  pushedDate: '2026-10-03T07:00:00Z', statusCheckRollup: { contexts: { nodes: [
    { __typename: 'CheckRun', name: 'ci-ok', status: 'COMPLETED', conclusion, completedAt: '2026-10-03T07:01:00Z' },
  ], pageInfo: { hasNextPage: false } } } } })
const prNode = (repository, number) => ({ number, url: `https://github.com/${repository}/pull/${number}`,
  title: privateText, body: privateText, state: 'OPEN', isDraft: false, merged: false,
  headRefName: 'feature-unmapped', headRefOid: sha(number), baseRefName: 'main',
  reviewDecision: null, mergeStateStatus: 'DIRTY', autoMergeRequest: { enabledAt: 'unused' },
  author: { __typename: 'User', login: 'human' }, baseRepository: { nameWithOwner: repository },
  labels: { nodes: [], pageInfo: { hasNextPage: false } },
  commits: { nodes: [{ commit: { oid: sha(number), pushedDate: '2026-10-03T07:00:00Z',
    statusCheckRollup: { contexts: { nodes: [], pageInfo: { hasNextPage: false } } } } }] } })

async function fixture(t, { closeout, repos = [A], open = { [A]: [7] }, limits = {} } = {}) {
  t.mock.method(Date, 'now', () => wall)
  const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'redmain-wiring-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const config = { version: 1, mode: 'products-only-v1', captureOrigin: 'https://capture.test',
    boardOrigin: 'https://board.test', companyId, allowedRepositories: repos, stateDirectory: join(root, 'state'),
    captureTokenFile: join(root, 'capture.key'), boardTokenFile: join(root, 'board.key'), limits,
    ...(closeout === undefined ? {} : { closeout }) }
  const app = createApp({ store: createMemoryStore(), webhookSecret: 'fixture', queryToken: 'fixture', now: () => wall })
  const h = { config, root, open, calls: [], logs: [], heads: new Map(), mainHook: null,
    protectionHook: null, snapshotHook: null, primaryHook: null, fetchHook: null }
  h.fetch = async (url, init) => {
    h.calls.push(['http', init.method, url])
    if (h.fetchHook) { const response = await h.fetchHook(url, init); if (response) return response }
    assert.equal(init.method, 'GET', 'no claims, board writes, comments or wake path')
    assert.ok(url.startsWith(config.captureOrigin), 'no board scans or index prerequisite')
    return app(new Request(url, init))
  }
  h.run = async (file, args, options) => {
    assert.equal(file, 'fixture-gh')
    assert.equal(options.shell, false)
    assert.deepEqual(args.slice(0, 3), ['api', '--hostname', 'github.com'])
    if (args[3] !== 'graphql') {
      const match = /^repos\/(.+\/[^/]+)\/branches\/main\/protection$/.exec(args[3])
      assert.ok(match, 'only read-only branch protection; no mutation commands')
      h.calls.push(['protection', match[1]])
      if (h.protectionHook) { const response = await h.protectionHook(match[1], options); if (response) return response }
      return { stdout: JSON.stringify({ required_status_checks: { contexts: ['ci-ok'], checks: [] } }) }
    }
    const query = args[5]
    const repository = `${args.find((v) => v.startsWith('owner=')).slice(6)}/${args.find((v) => v.startsWith('name=')).slice(5)}`
    if (query.includes('BridgeOpenPullRequests')) {
      h.calls.push(['list', repository])
      const numbers = h.open[repository] ?? []
      return { stdout: JSON.stringify(envelope(repository, { pullRequests: { totalCount: numbers.length,
        nodes: numbers.map((number) => ({ number })), pageInfo: { hasNextPage: false, endCursor: null } } })) }
    }
    if (query.includes('BridgeMainHead')) {
      h.calls.push(['main', repository])
      if (h.mainHook) { const response = await h.mainHook(repository, options); if (response) return response }
      return { stdout: JSON.stringify(envelope(repository, { defaultBranchRef: h.heads.get(repository) ?? headNode() })) }
    }
    const number = Number(args.find((v) => v.startsWith('number=')).slice(7))
    const snapshot = query.includes('BridgeCloseoutSnapshot')
    assert.ok(snapshot || query.includes('BridgePullRequest'), 'no other GraphQL operation')
    h.calls.push([snapshot ? 'snapshot' : 'primary', repository, number])
    if (snapshot && h.snapshotHook) await h.snapshotHook(repository, options)
    if (!snapshot && h.primaryHook) await h.primaryHook(repository, options)
    return { stdout: JSON.stringify(envelope(repository, { isPrivate: true, pullRequest: prNode(repository, number) })) }
  }
  h.pass = (options = {}) => runProductPass({ config, captureToken: 'fixture', boardToken: 'fixture',
    githubExecutable: 'fixture-gh' }, { fetchImpl: h.fetch, run: h.run, log: (line) => h.logs.push(JSON.parse(line)), ...options })
  h.state = () => loadSweepState({ directory: config.stateDirectory, namespace: receiptNamespace(config) })
  h.summaryLogs = () => h.logs.filter((line) => line.event === 'closeout-redmain-shadow')
  h.kinds = (kind) => h.calls.filter(([k]) => k === kind)
  return h
}
async function stored(h) {
  const files = await readdir(h.config.stateDirectory)
  return Object.fromEntries(await Promise.all(files.sort().map(async (file) => [file,
    await readFile(join(h.config.stateDirectory, file), 'utf8')])))
}
const nonemptyReasons = (summary) => Object.fromEntries(Object.entries(summary.byReason).filter(([, n]) => n))
function hookFixture(options = {}) {
  let current = head()
  const reads = []
  const logs = []
  const hook = createRedmainShadowHook({ settings: { enabledRepositories: new Set([A, B]) },
    getMainHeadChecks: async (repository) => { reads.push(repository); return current },
    check: () => {}, log: (line) => logs.push(JSON.parse(line)), nowMs: () => wall, ...options })
  return { hook, reads, logs, setHead: (value) => { current = value } }
}

test('absent and empty settings preserve serialized results, namespace, persisted bytes and I/O', async (t) => {
  const transcripts = []
  for (const closeout of [undefined, {}, enrolled([]), { shadow: { enabledRepositories: [] }, ...enrolled([]) }]) {
    const h = await fixture(t, { closeout })
    const results = [await h.pass(), await h.pass()]
    assert.equal(results.every((r) => r.ok && !Object.hasOwn(r, 'redmainShadow')), true)
    assert.deepEqual(h.kinds('main'), [])
    assert.deepEqual(h.kinds('protection'), [])
    assert.deepEqual(h.logs, [])
    const namespace = receiptNamespace(h.config)
    assert.equal(namespace, receiptNamespace({ ...h.config, closeout: enrolled() }))
    transcripts.push({ results: JSON.stringify(results), calls: h.calls, bytes: await stored(h), namespace })
  }
  for (const transcript of transcripts.slice(1)) assert.deepEqual(transcript, transcripts[0])
})

test('settings validate at load and direct run before any effect, including alongside other children', async (t) => {
  const h = await fixture(t)
  for (const path of [h.config.captureTokenFile, h.config.boardTokenFile]) await writeFile(path, 'fixture', { mode: 0o600 })
  const path = join(h.root, 'consumer.json')
  await writeFile(path, JSON.stringify({ ...h.config, closeout: enrolled() }), { mode: 0o600 })
  assert.deepEqual((await loadConsumerConfig(path)).config.closeout, enrolled())
  assert.equal(closeoutRedmainSettings(h.config), null)
  assert.equal(closeoutRedmainSettings({ ...h.config, closeout: enrolled([]) }), null)
  assert.deepEqual([...closeoutRedmainSettings({ ...h.config, closeout: enrolled() }).enabledRepositories], [A])
  for (const redmainShadow of [null, [], 'shadow', {}, { enabledRepositories: A }, { enabledRepositories: [A, A] },
    { enabledRepositories: [B] }, { enabledRepositories: ['../bad'] }, { enabledRepositories: ['X/..'] },
    { enabledRepositories: [A], enabled: true }, { enabledRepositories: [A], live: false },
    ...['live', 'shadow', 'apply', 'enforce'].map((mode) => ({ enabledRepositories: [A], mode }))]) {
    const config = { ...h.config, closeout: { shadow: { enabledRepositories: [] },
      disarmShadow: { enabledRepositories: [] }, redmainShadow } }
    assert.throws(() => closeoutRedmainSettings(config), /closeout/)
    assert.throws(() => closeoutShadowSettings(config), /closeout/)
    await assert.rejects(runProductPass({ config }, { fetchImpl: h.fetch, run: h.run }), /closeout/)
    await writeFile(path, JSON.stringify(config), { mode: 0o600 })
    await assert.rejects(loadConsumerConfig(path), /configuration/)
  }
  assert.deepEqual(h.calls, [])
})

test('red/open, second red/already-open, pending, recovery, green/none and re-red use memory only', async () => {
  const h = hookFixture()
  for (const conclusion of ['failure', 'failure', 'unknown', 'success', 'success', 'failure']) {
    h.setHead({ ...head(conclusion), mainSha: sha(h.reads.length + 1) })
    await h.hook.observe(A)
  }
  await h.hook.observe('Other/repo')
  assert.equal(h.reads.length, 6, 'one main-head read per enrolled observation only')
  const { counters, byReason } = h.hook.summary()
  assert.deepEqual(counters, { total: 6, evaluated: 6, red: 3, green: 2, pending: 1,
    opened: 0, wouldOpen: 2, alreadyOpen: 1, recovered: 0, wouldRecover: 1, noOp: 1, errors: 0,
    deferred: 0, logErrors: 0 })
  assert.equal(byReason['flag-off-shadow'], 3)
  assert.equal(byReason['incident-already-open'], 1)
  assert.equal(byReason['main-pending'], 1)
  assert.equal(byReason['main-green-no-incident'], 1)
  h.hook.emit()
  assert.deepEqual(h.logs, [{ event: 'closeout-redmain-shadow', ...h.hook.summary() }])
  // Returning a summary must not grant access to mutable internal state.
  const copy = h.hook.summary()
  copy.counters.wouldOpen = 999
  copy.byReason['flag-off-shadow'] = 999
  assert.equal(h.hook.summary().counters.wouldOpen, 2)
  assert.equal(h.hook.summary().byReason['flag-off-shadow'], 3)
})

test('refusing incident-store write spies stay uncalled across red and recovery paths', async () => {
  // Instrument the PRIVATE refusing methods, without adding a writable-store
  // injection seam to production or exporting the hook-local incident map.
  const moduleUrl = new URL('../src/closeout-redmain-wiring.js', import.meta.url)
  let source = await readFile(moduleUrl, 'utf8')
  for (const method of ['openIncident', 'closeIncident']) {
    assert.equal(source.split(`${method}: async () => {`).length, 2, 'writer spy must instrument exactly one method')
  }
  source = source.replace("'./closeout-redmain.js'", JSON.stringify(new URL('../src/closeout-redmain.js', import.meta.url).href))
    .replace('openIncident: async () => {', 'openIncident: async () => { writes.openIncident++;')
    .replace('closeIncident: async () => {', 'closeIncident: async () => { writes.closeIncident++;')
  const { createRedmainShadowHook: createHook, writes } = await import(`data:text/javascript;base64,${Buffer.from(
    source + '\nexport const writes = { openIncident: 0, closeIncident: 0 }\n').toString('base64')}`)
  let read = head()
  const hook = createHook({ settings: { enabledRepositories: new Set([A]) }, getMainHeadChecks: async () => read,
    check: () => {}, nowMs: () => wall })
  await hook.observe(A)
  await hook.observe(A)
  read = head('success')
  await hook.observe(A)
  assert.deepEqual(writes, { openIncident: 0, closeIncident: 0 })
  assert.equal(hook.summary().counters.errors, 0)
  assert.equal(hook.summary().counters.wouldOpen, 1)
  assert.equal(hook.summary().counters.wouldRecover, 1)
})

test('unreadable, null, malformed and foreign heads count errors without poisoning later reads or logs', async () => {
  let value
  const h = hookFixture({ getMainHeadChecks: async () => { if (value instanceof Error) throw value; return value } })
  for (value of [new Error(privateText), null, { ...head(), mainSha: privateText }, head('failure', B)]) {
    await h.hook.observe(A)
  }
  value = head()
  await h.hook.observe(A)
  h.hook.emit()
  assert.equal(h.hook.summary().counters.errors, 4)
  assert.equal(h.hook.summary().counters.wouldOpen, 1)
  assert.deepEqual(nonemptyReasons(h.hook.summary()), {
    'flag-off-shadow': 1, 'main-head-read-failed': 3, 'undecidable-head': 1,
  })
  assert.equal(JSON.stringify(h.logs).includes(privateText), false)
  assert.deepEqual(Object.keys(h.logs[0]), ['event', 'counters', 'byReason'])
})

test('a bad hook clock counts unexpected observation failures and continues', async () => {
  const h = hookFixture({ nowMs: () => -1 })
  await h.hook.observe(A)
  assert.equal(h.hook.summary().counters.errors, 1)
  assert.equal(h.hook.summary().byReason['observation-failed'], 1)
})

test('hook admission defers locally and broken log sinks cannot poison observations', async () => {
  const h = hookFixture({ getMainHeadChecks: async () => { throw new RedmainShadowDeferred() },
    log: () => { throw new Error(privateText) } })
  await h.hook.observe(A)
  h.hook.emit()
  assert.equal(h.hook.summary().counters.deferred, 1)
  assert.equal(h.hook.summary().counters.errors, 0)
  assert.equal(h.hook.summary().counters.logErrors, 1)
})

for (const reason of ['deadline', 'request-budget', 'cancelled', null]) {
  test(`hook contains only deadline, not ${String(reason)} stops`, async () => {
    let stopped = false
    const h = hookFixture({ check: () => { if (stopped) throw new Error('stopped') }, stopReason: () => reason,
      getMainHeadChecks: async () => { stopped = true; throw new Error(privateText) } })
    if (reason === 'deadline') {
      await h.hook.observe(A)
      assert.equal(h.hook.summary().counters.deferred, 1)
      assert.throws(() => h.hook.emit(), /stopped/)
      h.hook.emit({ ignoreStop: true })
      assert.equal(h.logs.length, 1)
    } else {
      await assert.rejects(h.hook.observe(A), /consumer pass stopped/)
      assert.equal(h.hook.summary().counters.total, 0)
      assert.deepEqual(h.logs, [])
    }
  })
}

test('runner reads one fresh main head per enrolled repo AFTER primary work even without PRs', async (t) => {
  const h = await fixture(t, { closeout: enrolled([B]), repos: [A, B], open: { [A]: [7, 8] } })
  for (let pass = 0; pass < 2; pass++) {
    h.calls.length = 0
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(result.requests, h.calls.length)
    assert.deepEqual(h.kinds('main'), [['main', B]])
    assert.deepEqual(h.kinds('protection'), [['protection', B]])
    assert.deepEqual(h.calls.slice(-2), [['main', B], ['protection', B]])
    assert.equal(result.redmainShadow.counters.wouldOpen, 1, 'incident memory never survives a pass')
    assert.equal(result.redmainShadow.counters.opened, 0)
    assert.equal(result.redmainShadow.counters.errors, 0)
    assert.equal(h.summaryLogs().length, pass + 1)
  }
})

test('main read holds the receipt lock; no incident, claim, pending or index state is persisted', async (t) => {
  const results = []
  for (const closeout of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout })
    h.mainHook = async () => {
      assert.equal((await stat(join(h.config.stateDirectory, 'lock'))).isDirectory(), true)
      assert.deepEqual((await h.state()).repos[A].backfill.done, [7], 'primary checkpoint precedes observation')
    }
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.deepEqual((await readdir(h.config.stateDirectory)).sort(), ['receipts.json', 'schedule.json', 'sweeps.json'])
    results.push({ events: result.events, sweeps: result.sweeps, bytes: await stored(h) })
  }
  assert.deepEqual(results[1], results[0])
})

for (const maxRequests of [4, 5, 6]) {
  test(`surplus admission at ${maxRequests} requests guards BOTH transport reads without a request-budget stop`, async (t) => {
    const h = await fixture(t, { closeout: enrolled(), limits: { maxRequests } })
    const result = await h.pass()
    assert.equal(result.ok, true, 'four primary calls retain their budget')
    assert.equal(result.reason, null)
    assert.equal(result.requests, maxRequests)
    assert.equal(result.redmainShadow.counters.deferred, Number(maxRequests < 6))
    assert.equal(result.redmainShadow.counters.errors, 0)
    assert.equal(result.redmainShadow.counters.wouldOpen, Number(maxRequests === 6))
    assert.equal(h.kinds('main').length, Number(maxRequests > 4))
    assert.equal(h.kinds('protection').length, Number(maxRequests === 6))
    assert.equal(h.summaryLogs().length, 1)
  })
}

test('surplus deferral continues across repositories, never launches post-return I/O', async (t) => {
  const h = await fixture(t, { closeout: enrolled([A, B]), repos: [A, B], limits: { maxRequests: 10 } })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.requests, 10)
  assert.equal(result.redmainShadow.counters.total, 2)
  assert.equal(result.redmainShadow.counters.wouldOpen, 1)
  assert.equal(result.redmainShadow.counters.deferred, 1)
  assert.equal(result.redmainShadow.counters.errors, 0)
  assert.deepEqual(h.kinds('main'), [['main', A]])
  const count = h.calls.length
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(h.calls.length, count)
})

test('a request-budget stop before the drain fails exactly as shadows OFF', async (t) => {
  const outcomes = []
  for (const closeout of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout, limits: { maxRequests: 3 } })
    const result = await h.pass()
    assert.equal(result.ok, false)
    assert.equal(result.reason, 'request-budget')
    assert.deepEqual(h.kinds('main'), [])
    assert.deepEqual(h.summaryLogs(), [])
    if (closeout) assert.equal(result.redmainShadow.counters.total, 0)
    outcomes.push({ ok: result.ok, reason: result.reason, events: result.events, sweeps: result.sweeps, bytes: await stored(h) })
  }
  assert.deepEqual(outcomes[1], outcomes[0])
})

for (const phase of ['main', 'protection']) {
  test(`deadline during ${phase} drain read defers and emits summary without failing primary`, async (t) => {
    const h = await fixture(t, { closeout: enrolled([A, B]), repos: [A, B], limits: { durationMs: 100 } })
    let now = 0
    h[`${phase}Hook`] = async () => { now = 100 }
    const result = await h.pass({ clock: { now: () => now, setTimer: () => 1, clearTimer: () => {} } })
    assert.equal(result.ok, true)
    assert.equal(result.reason, null)
    assert.equal(result.redmainShadow.counters.deferred, 2)
    assert.equal(result.redmainShadow.counters.errors, 0)
    assert.deepEqual(h.kinds('main'), [['main', A]], 'no more reads after the drain deadline')
    assert.equal(h.kinds('protection').length, Number(phase === 'protection'))
    assert.equal(h.summaryLogs().length, 1, 'ignoreStop still emits a contained summary')
    assert.deepEqual((await h.state()).repos[A].backfill.done, [7])
  })
}

test('deadline before drain entry is never contained', async (t) => {
  const h = await fixture(t, { closeout: enrolled(), limits: { durationMs: 100 } })
  let now = 0
  h.primaryHook = async () => { now = 100 }
  const result = await h.pass({ clock: { now: () => now, setTimer: () => 1, clearTimer: () => {} } })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'deadline')
  assert.equal(result.redmainShadow.counters.deferred, 0)
  assert.deepEqual(h.kinds('main'), [])
  assert.deepEqual(h.summaryLogs(), [])
})

test('cancelled main read drains under lock and fails the pass, without a summary or post-return work', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  const controller = new AbortController()
  let drained = false
  h.mainHook = async (_repository, options) => {
    await new Promise((resolve) => {
      options.signal.addEventListener('abort', () => setImmediate(async () => {
        assert.equal((await stat(join(h.config.stateDirectory, 'lock'))).isDirectory(), true)
        drained = true
        resolve()
      }), { once: true })
      controller.abort()
    })
    throw new Error(privateText)
  }
  const result = await h.pass({ signal: controller.signal,
    clock: { now: () => 0, setTimer: () => 1, clearTimer: () => {} } })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'cancelled')
  assert.equal(drained, true)
  assert.equal(result.redmainShadow.counters.total, 0)
  assert.deepEqual(h.summaryLogs(), [])
  assert.deepEqual(h.kinds('protection'), [])
  assert.deepEqual((await h.state()).repos[A].backfill.done, [7])
  await assert.rejects(stat(join(h.config.stateDirectory, 'lock')), { code: 'ENOENT' })
  const count = h.calls.length
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(h.calls.length, count)
})

for (const failing of ['transport', 'malformed', 'protection']) {
  test(`${failing} observation errors are counted with no credential retry, and later repositories continue`, async (t) => {
    const h = await fixture(t, { closeout: enrolled([A, B]), repos: [A, B] })
    h.mainHook = async (repository) => {
      if (repository !== A) return
      if (failing === 'transport') throw new Error(privateText)
      if (failing === 'malformed') return { stdout: JSON.stringify(envelope(A, { defaultBranchRef: privateText })) }
    }
    h.protectionHook = async (repository) => { if (repository === A && failing === 'protection') throw new Error(privateText) }
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(result.redmainShadow.counters.errors, 1)
    assert.equal(result.redmainShadow.counters.wouldOpen, 1)
    assert.deepEqual(h.kinds('main'), [['main', A], ['main', B]])
    assert.deepEqual(nonemptyReasons(result.redmainShadow), { 'flag-off-shadow': 1, 'main-head-read-failed': 1 })
    assert.equal(JSON.stringify(h.logs).includes(privateText), false)
  })
}

test('primary failure still observes and emits; early cycle failure emits its empty summary', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  h.primaryHook = async () => { throw new Error(privateText) }
  const result = await h.pass()
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'sweeps-incomplete')
  assert.equal(result.redmainShadow.counters.wouldOpen, 1)
  assert.equal(h.summaryLogs().length, 1)
  h.logs.length = 0
  await writeFile(join(h.config.stateDirectory, 'schedule.json'), privateText, { mode: 0o600 })
  const broken = await h.pass()
  assert.equal(broken.reason, 'cycle-failed')
  assert.equal(broken.redmainShadow.counters.total, 0)
  assert.equal(h.summaryLogs().length, 1, 'catch path emits once when no whole-pass stop exists')
})

for (const phase of ['snapshot', 'main']) {
  test(`both drains preserve the primary outcome when ${phase} exhausts the deadline`, async (t) => {
    const h = await fixture(t, { closeout: { ...enrolled(), disarmShadow: { enabledRepositories: [A] } },
      limits: { durationMs: 100 } })
    let now = 0
    h[`${phase}Hook`] = async () => { now = 100 }
    const result = await h.pass({ clock: { now: () => now, setTimer: () => 1, clearTimer: () => {} } })
    assert.equal(result.ok, true)
    assert.equal(result.reason, null)
    assert.equal(result.redmainShadow.counters.deferred, 1)
    assert.equal(result.disarmShadow.counters.deferred, Number(phase === 'snapshot'))
    assert.equal(h.summaryLogs().length, 1)
    assert.equal(h.logs.filter((line) => line.event === 'closeout-disarm-shadow').length, 1)
    assert.equal(h.kinds('main').length, Number(phase === 'main'))
  })
}

test('disarm remains independent of red-main observations and neither shadow writes', async (t) => {
  const h = await fixture(t, { closeout: { ...enrolled(), disarmShadow: { enabledRepositories: [A] } } })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.disarmShadow.counters.evaluated, 1)
  assert.equal(result.redmainShadow.counters.wouldOpen, 1)
  assert.equal(result.redmainShadow.counters.opened, 0)
  assert.deepEqual(h.calls.slice(-3), [['snapshot', A, 7], ['main', A], ['protection', A]])
  const log = h.summaryLogs()[0]
  assert.deepEqual(Object.keys(log), ['event', 'counters', 'byReason'])
  assert.ok(JSON.stringify(log).length < 1024)
  assert.equal(JSON.stringify(h.logs).includes(privateText), false)
})
