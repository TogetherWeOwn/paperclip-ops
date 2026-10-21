import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, writeFile, readdir, rm, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'
import { runProductPass } from '../src/consumer-runner.js'
import { loadConsumerConfig } from '../src/consumer-config.js'
import { receiptNamespace } from '../src/receipt-cycle.js'
import { loadSweepState } from '../src/sweep-store.js'
import { createApp } from '../src/app.js'
import { createMemoryStore } from '../src/store-memory.js'
import { closeoutShadowSettings } from '../src/closeout-shadow-wiring.js'

// Protected acceptance contract. Tests own this file and its package enumeration;
// implementation fixes belong in source, not in these tests. No live services.
// Proposed sibling interface: closeoutDeploySettings(config), DeployShadowDeferred,
// createDeployShadowHook({ settings, capture, check, stopReason, log, nowMs })
// -> { observe(repository), summary(), emit({ ignoreStop }?) }.
// Summary: existing deploy watcher counters + deferred/logErrors, and fixed
// byReason counts only. Runner result field deployShadow, log closeout-deploy-shadow.
const A = 'ExampleOrg/widget'
const B = 'ExampleOrg/gadget'
const foreign = 'OtherOrg/widget'
const wall = Date.parse('2026-10-20T12:00:00Z')
const weekAgo = Date.parse('2026-10-13T12:00:00Z')
const privateText = 'UNTRUSTED-DEPLOY-PROSE'
const sha = 'a'.repeat(40)
const enrolled = (repos = [A]) => ({ deployShadow: { enabledRepositories: repos } })
const moduleUrl = new URL('../src/closeout-deploy-wiring.js', import.meta.url)
const payload = (over = {}) => ({ repository: { full_name: A }, deployment_status: { state: 'success',
  description: privateText, log_url: `https://logs.example.test/${privateText}` },
  deployment: { sha, environment: 'staging', description: privateText }, ...over })
const delivery = (id = 'deploy-1', body = payload(), over = {}) => ({ delivery_id: id,
  repository: A, event: 'deployment_status', received_ms: wall - 1000, body_truncated: 0,
  body: JSON.stringify(body), ...over })

let wiring
try { wiring = await import(moduleUrl) }
catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('closeout-deploy-wiring.js')) throw error }
function api() {
  assert.ok(wiring, 'missing deployment-success shadow wiring module')
  for (const name of ['closeoutDeploySettings', 'createDeployShadowHook', 'DeployShadowDeferred']) {
    assert.equal(typeof wiring[name], 'function', `missing ${name} export`)
  }
  return wiring
}
function hookFixture({ rows = [delivery()], createHook, ...options } = {}) {
  const reads = []
  const logs = []
  let current = rows
  const hook = (createHook ?? api().createDeployShadowHook)({ settings: { enabledRepositories: new Set([A, B]) },
    capture: {
      listDeliveries: async (filters) => { reads.push(['list', filters]); return current },
      getDelivery: async (id) => { reads.push(['get', id]); return current.find((r) => r.delivery_id === id) },
    }, check: () => {}, nowMs: () => wall, log: (line) => logs.push(JSON.parse(line)), ...options })
  return { hook, reads, logs, setRows: (value) => { current = value } }
}
function safeSummary(summary) {
  assert.deepEqual(Object.keys(summary).sort(), ['byReason', 'counters'])
  assert.ok(Object.keys(summary.byReason).length <= 24, 'reason cardinality is fixed, not one key per delivery')
  for (const [key, n] of Object.entries({ ...summary.counters, ...summary.byReason })) {
    assert.match(key, /^[A-Za-z][A-Za-z-]*$/)
    assert.ok(Number.isSafeInteger(n) && n >= 0, `${key} must be a measured count`)
  }
  assert.equal(summary.counters.recorded, 0, 'shadow enrollment is not live enrollment')
  assert.equal(summary.counters.suppressed, 0, 'red-main suppression is not connected')
  const text = JSON.stringify(summary)
  for (const value of [privateText, A, B, sha, 'qa-smoke:', 'https://']) assert.equal(text.includes(value), false)
  assert.ok(text.length < 2048)
}

async function fixture(t, { closeout, repos = [A], open = { [A]: [7] }, limits = {}, rows = [delivery()] } = {}) {
  t.mock.method(Date, 'now', () => wall)
  const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'deploy-wiring-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const config = { version: 1, mode: 'products-only-v1', captureOrigin: 'https://capture.example.test',
    boardOrigin: 'https://board.example.test', companyId: '00000000-0000-4000-8000-000000000009',
    allowedRepositories: repos, stateDirectory: join(root, 'state'), captureTokenFile: join(root, 'capture.key'),
    boardTokenFile: join(root, 'board.key'), limits, ...(closeout === undefined ? {} : { closeout }) }
  const store = createMemoryStore()
  for (const row of rows) await store.append(row)
  const app = createApp({ store, webhookSecret: 'fixture', queryToken: 'fixture', now: () => wall })
  const h = { root, config, calls: [], logs: [], fetchHook: null, primaryHook: null }
  h.fetch = async (url, init) => {
    h.calls.push(['http', init.method, url])
    assert.equal(init.method, 'GET', 'observer must never claim, create tasks or wake')
    assert.equal(new URL(url).origin, config.captureOrigin, 'no board or other service reads with deploy alone')
    assert.equal(init.signal.aborted, false)
    if (h.fetchHook) { const result = await h.fetchHook(new URL(url), init); if (result) return result }
    return app(new Request(url, init))
  }
  const envelope = (repository, fields) => ({ data: { repository: { nameWithOwner: repository, ...fields } } })
  h.run = async (file, args, options) => {
    assert.equal(file, 'fixture-gh')
    assert.equal(options.shell, false)
    assert.deepEqual(args.slice(0, 3), ['api', '--hostname', 'github.com'])
    if (args[3] !== 'graphql') {
      assert.match(args[3], /^repos\/ExampleOrg\/(widget|gadget)\/branches\/main\/protection$/)
      h.calls.push(['protection'])
      return { stdout: JSON.stringify({ required_status_checks: { contexts: ['ci-ok'], checks: [] } }) }
    }
    const query = args[5]
    const repository = `${args.find((v) => v.startsWith('owner=')).slice(6)}/${args.find((v) => v.startsWith('name=')).slice(5)}`
    if (query.includes('BridgeOpenPullRequests')) {
      h.calls.push(['list', repository])
      const numbers = open[repository] ?? []
      return { stdout: JSON.stringify(envelope(repository, { pullRequests: { totalCount: numbers.length,
        nodes: numbers.map((number) => ({ number })), pageInfo: { hasNextPage: false, endCursor: null } } })) }
    }
    if (query.includes('BridgeMainHead')) {
      h.calls.push(['main', repository])
      return { stdout: JSON.stringify(envelope(repository, { defaultBranchRef: { name: 'main', target: {
        __typename: 'Commit', oid: sha, pushedDate: '2026-10-20T09:00:00Z',
        statusCheckRollup: { contexts: { nodes: [{ __typename: 'CheckRun',
          name: 'ci-ok', status: 'COMPLETED', conclusion: 'FAILURE', completedAt: '2026-10-20T09:01:00Z' }], pageInfo: { hasNextPage: false } } } } } })) }
    }
    const snapshot = query.includes('BridgeCloseoutSnapshot')
    assert.ok(snapshot || query.includes('BridgePullRequest'), 'no writes or other GitHub operations')
    h.calls.push([snapshot ? 'snapshot' : 'primary', repository])
    if (!snapshot && h.primaryHook) await h.primaryHook(options)
    return { stdout: JSON.stringify(envelope(repository, { isPrivate: false, pullRequest: {
      number: 7, url: `https://github.com/${repository}/pull/7`, title: privateText, body: privateText,
      state: 'OPEN', isDraft: false, merged: false, headRefName: 'feature-unmapped', headRefOid: sha,
      baseRefName: 'main', reviewDecision: null, mergeStateStatus: 'DIRTY', autoMergeRequest: { enabledAt: 'unused' },
      author: { __typename: 'User', login: 'human' }, baseRepository: { nameWithOwner: repository },
      labels: { nodes: [], pageInfo: { hasNextPage: false } }, commits: { nodes: [{ commit: { oid: sha,
        pushedDate: '2026-10-20T09:00:00Z', statusCheckRollup: { contexts: { nodes: [],
          pageInfo: { hasNextPage: false } } } } }] },
    } })) }
  }
  h.pass = (options = {}) => runProductPass({ config, captureToken: 'fixture', boardToken: 'fixture',
    githubExecutable: 'fixture-gh' }, { fetchImpl: h.fetch, run: h.run,
    log: (line) => h.logs.push(JSON.parse(line)), ...options })
  h.state = () => loadSweepState({ directory: config.stateDirectory, namespace: receiptNamespace(config) })
  h.deployCalls = () => h.calls.filter(([kind, , url]) => kind === 'http' &&
    (new URL(url).searchParams.get('event') === 'deployment_status' || new URL(url).pathname.startsWith('/events/')))
  h.summaryLogs = () => h.logs.filter((line) => line.event === 'closeout-deploy-shadow')
  return h
}
async function stored(h) {
  const files = await readdir(h.config.stateDirectory)
  return Object.fromEntries(await Promise.all(files.sort().map(async (file) =>
    [file, await readFile(join(h.config.stateDirectory, file), 'utf8')])) )
}
const fakeClock = (now = () => 0) => ({ now, setTimer: () => 1, clearTimer: () => {} })

test('fixture control: existing primary pass never treats deployments as receipt events', async (t) => {
  const h = await fixture(t)
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.requests, 4)
  assert.deepEqual(h.deployCalls(), [])
  assert.deepEqual(h.logs, [])
  assert.deepEqual((await h.state()).repos[A].backfill.done, [7])
})

test('fixture control: sibling disarm and red-main see valid snapshots without deployment enrollment', async (t) => {
  const h = await fixture(t, { closeout: { disarmShadow: { enabledRepositories: [A] },
    redmainShadow: { enabledRepositories: [A] } } })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.disarmShadow.counters.evaluated, 1)
  assert.equal(result.redmainShadow.counters.wouldOpen, 1)
  assert.deepEqual(h.deployCalls(), [])
})

// Catches an off-mode hook adding reads, logs, fields, cursor changes or files.
test('absent/empty deploy settings preserve serialized output, I/O and persisted bytes across passes', async (t) => {
  const transcripts = []
  for (const closeout of [undefined, {}, enrolled([]), { shadow: { enabledRepositories: [] },
    disarmShadow: { enabledRepositories: [] }, redmainShadow: { enabledRepositories: [] }, ...enrolled([]) }]) {
    const h = await fixture(t, { closeout })
    const results = [await h.pass(), await h.pass()]
    assert.equal(results.every((r) => r.ok && !Object.hasOwn(r, 'deployShadow')), true)
    assert.deepEqual(h.deployCalls(), [])
    assert.deepEqual(h.logs, [])
    transcripts.push({ results: JSON.stringify(results), calls: h.calls, bytes: await stored(h),
      namespace: receiptNamespace(h.config) })
  }
  for (const value of transcripts.slice(1)) assert.deepEqual(value, transcripts[0])
})

test('settings accept equality/subsets and reject invalid children at load/direct-run before effects', async (t) => {
  const { closeoutDeploySettings } = api()
  const h = await fixture(t)
  for (const file of [h.config.captureTokenFile, h.config.boardTokenFile]) await writeFile(file, 'fixture', { mode: 0o600 })
  const file = join(h.root, 'consumer.json')
  assert.equal(closeoutDeploySettings(h.config), null)
  assert.equal(closeoutDeploySettings({ ...h.config, closeout: enrolled([]) }), null)
  for (const repos of [[A], [B], [A, B]]) {
    const config = { ...h.config, allowedRepositories: [A, B], closeout: enrolled(repos) }
    assert.deepEqual([...closeoutDeploySettings(config).enabledRepositories], repos)
    assert.equal(closeoutShadowSettings(config), null, 'child registered in the shared validator')
    await writeFile(file, JSON.stringify(config), { mode: 0o600 })
    assert.deepEqual((await loadConsumerConfig(file)).config.closeout, enrolled(repos))
    assert.equal(receiptNamespace(config), receiptNamespace({ ...config, closeout: undefined }), 'shadow is not part of receipt identity')
  }
  for (const deployShadow of [null, [], true, 'on', {}, { enabledRepositories: A },
    { enabledRepositories: [A, A] }, { enabledRepositories: [foreign] },
    ...['../bad', 'X/..', 'X/.', '/widget', 'ExampleOrg/widget/', ' ExampleOrg/widget', 'X/a b', null, 1]
      .map((r) => ({ enabledRepositories: [r] })),
    ...['mode', 'live', 'apply', 'enforce', 'sinceMs', 'enabled', 'qaOwners']
      .map((key) => ({ enabledRepositories: [A], [key]: true }))]) {
    const config = { ...h.config, closeout: { shadow: { enabledRepositories: [] },
      disarmShadow: { enabledRepositories: [] }, redmainShadow: { enabledRepositories: [] }, deployShadow } }
    assert.throws(() => closeoutDeploySettings(config), /closeout/)
    assert.throws(() => closeoutShadowSettings(config), /closeout/)
    await assert.rejects(runProductPass({ config }, { fetchImpl: h.fetch, run: h.run }), /closeout/)
    await writeFile(file, JSON.stringify(config), { mode: 0o600 })
    await assert.rejects(loadConsumerConfig(file), /configuration/)
  }
  assert.deepEqual(h.calls, [])
  await assert.rejects(stat(h.config.stateDirectory), { code: 'ENOENT' })
})

// Import-only instrumentation observes the real watcher's dependency boundary.
// No writable-store injection seam or replacement classifier is added to source.
async function instrumentedHook() {
  api()
  const target = new URL('../src/closeout-deploy.js', import.meta.url).href
  const wrapper = `import { createDeployWatcher as real } from ${JSON.stringify(target)};
    export * from ${JSON.stringify(target)};
    export let received;
    export let writes = 0;
    export function createDeployWatcher(deps) {
      received = deps;
      return real({ ...deps, intents: { ...deps.intents,
        recordIntent: async (...args) => { writes++; return deps.intents.recordIntent(...args) } } });
    }`
  const wrapperUrl = `data:text/javascript;base64,${Buffer.from(wrapper).toString('base64')}`
  const source = (await readFile(moduleUrl, 'utf8')).replace(/(from\s*|import\s*)(['"])(\.\.?\/[^'"]+)\2/g,
    (_match, prefix, _quote, specifier) => `${prefix}${JSON.stringify(specifier === './closeout-deploy.js'
      ? wrapperUrl : new URL(specifier, moduleUrl).href)}`)
  const hookModule = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)
  return { createHook: hookModule.createDeployShadowHook, watcherModule: await import(wrapperUrl) }
}

test('live enrollment stays EMPTY; private writer throws and is never called for a positive shadow decision', async () => {
  const { createHook, watcherModule } = await instrumentedHook()
  const h = hookFixture({ createHook })
  await h.hook.observe(A)
  const deps = watcherModule.received
  assert.ok(deps, 'hook must call the existing createDeployWatcher, not reimplement classification')
  assert.equal(Object.keys(deps).every((key) =>
    ['capture', 'intents', 'policy', 'allowedRepositories', 'isSuppressed', 'nowMs'].includes(key)), true,
  'no board, GitHub writer, persistent store or wake capability reaches the watcher')
  assert.deepEqual([...deps.policy.enabledRepositories], [], 'regression: enabling live repos is forbidden even with a refusing writer')
  assert.deepEqual(Object.keys(deps.capture).sort(), ['getDelivery', 'listDeliveries'])
  assert.deepEqual(Object.keys(deps.intents).sort(), ['findIntent', 'recordIntent'])
  assert.equal(deps.isSuppressed == null || deps.isSuppressed(A) === false, true)
  const summary = h.hook.summary()
  assert.equal(summary.counters.wouldRecord, 1)
  assert.equal(summary.counters.errors, 0, 'a caught writer refusal must not masquerade as observation')
  assert.equal(summary.counters.recorded, 0)
  h.setRows([delivery('deploy-2', payload({ deployment: { sha: 'b'.repeat(40), environment: 'staging' } }))])
  await h.hook.observe(A)
  assert.equal(watcherModule.writes, 0)
  await assert.rejects(deps.intents.recordIntent({ key: `qa-smoke:${A}:${sha}`, repository: A, sha }))
  safeSummary(h.hook.summary())
})

test('staging lowercase success is observed; repeated passes do not claim durable dedupe', async () => {
  for (const environment of ['staging', ' STAGING ', 'svc-staging-us']) {
    const h = hookFixture({ rows: [delivery('one', payload({ deployment: { sha, environment } }))] })
    await h.hook.observe(foreign)
    assert.deepEqual(h.reads, [])
    await h.hook.observe(A)
    assert.equal(h.hook.summary().counters.wouldRecord, 1)
    assert.equal(h.reads.length, 2)
    assert.equal(h.reads[0][0], 'list')
    const { repository, event, sinceMs } = h.reads[0][1]
    assert.deepEqual({ repository, event, sinceMs }, { repository: A, event: 'deployment_status', sinceMs: weekAgo })
    assert.deepEqual(h.reads[1], ['get', 'one'])
    const copy = h.hook.summary()
    copy.counters.wouldRecord = 999
    copy.byReason['flag-off-shadow'] = 999
    assert.equal(h.hook.summary().counters.wouldRecord, 1)
    h.hook.emit()
    assert.deepEqual(h.logs, [{ event: 'closeout-deploy-shadow', ...h.hook.summary() }])
    safeSummary(h.hook.summary())
  }
  const h = hookFixture({ rows: [delivery('one'), delivery('two')] })
  await h.hook.observe(A)
  const c = h.hook.summary().counters
  // Both honest strategies are allowed: count each would-record, or pass-local
  // key tracking. A fresh hook MUST forget either strategy's observation state.
  assert.equal(c.wouldRecord + c.alreadyRecorded, 2)
  assert.ok(c.wouldRecord >= 1)
  const fresh = hookFixture()
  await fresh.hook.observe(A)
  assert.equal(fresh.hook.summary().counters.wouldRecord, 1)
})

for (const [name, row, counter] of [
  ['production', delivery('one', payload({ deployment: { sha, environment: 'production' } })), 'nonStaging'],
  ['stage is not staging', delivery('one', payload({ deployment: { sha, environment: 'stage' } })), 'nonStaging'],
  ...['failure', 'error', 'pending', 'SUCCESS'].map((state) => [state,
    delivery('one', payload({ deployment_status: { state } })), 'nonSuccess']),
  ['foreign payload', delivery('one', payload({ repository: { full_name: foreign } })), 'dropped'],
  ['bad sha', delivery('one', payload({ deployment: { sha: privateText, environment: 'staging' } })), 'errors'],
  ['truncated', delivery('one', payload(), { body_truncated: 1 }), 'errors'],
  ['invalid json', delivery('one', payload(), { body: privateText }), 'errors'],
  ['missing status', delivery('one', payload({ deployment_status: null })), 'errors'],
  ['missing environment', delivery('one', payload({ deployment: { sha } })), 'errors'],
  ['unsafe render', delivery('one', payload({ deployment: { sha, environment: 'staging-!' + privateText } })), 'errors'],
]) {
  test(`${name} cannot become a success intent or leak payload text`, async () => {
    const h = hookFixture({ rows: [row] })
    await h.hook.observe(A)
    assert.equal(h.hook.summary().counters.wouldRecord, 0)
    assert.equal(h.hook.summary().counters[counter], 1)
    h.hook.emit()
    safeSummary(h.hook.summary())
    assert.equal(JSON.stringify(h.logs).includes(privateText), false)
  })
}

test('bounded history/cap never reads the 1001st body; reasons and logs stay constant-sized', async () => {
  const rows = Array.from({ length: 1001 }, (_, n) => delivery(`deploy-${n}`, payload({
    deployment: { sha: n.toString(16).padStart(40, '0'), environment: 'staging' } })))
  const h = hookFixture({ rows })
  await h.hook.observe(A)
  assert.equal(h.reads.filter(([kind]) => kind === 'get').length, 1000)
  assert.equal(h.reads.some(([kind, id]) => kind === 'get' && id === 'deploy-1000'), false)
  assert.equal(h.hook.summary().counters.overBudget, 1)
  assert.equal(h.hook.summary().counters.wouldRecord, 1000)
  h.hook.emit()
  assert.equal(h.logs.length, 1)
  safeSummary(h.hook.summary())
})

for (const phase of ['list', 'get']) {
  test(`${phase} sentinel swallowed by watcher remains a deferral, not capture error or empty success`, async () => {
    const { DeployShadowDeferred } = api()
    const h = hookFixture({ capture: {
      listDeliveries: async () => { if (phase === 'list') throw new DeployShadowDeferred(); return [delivery()] },
      getDelivery: async () => { throw new DeployShadowDeferred() },
    } })
    await h.hook.observe(A)
    assert.equal(h.hook.summary().counters.deferred, 1)
    assert.equal(h.hook.summary().counters.errors, 0)
    assert.equal(h.hook.summary().counters.wouldRecord, 0)
    assert.equal(h.hook.summary().byReason['budget-deferred'], 1)
  })
  test(`${phase} provider errors stay errors and later repository observations continue`, async () => {
    const h = hookFixture({ capture: {
      listDeliveries: async ({ repository }) => { if (repository === A && phase === 'list') throw new Error(privateText)
        return [delivery(repository === A ? 'bad' : 'good', payload({ repository: { full_name: repository } }))] },
      getDelivery: async (id) => { if (id === 'bad') throw new Error(privateText)
        return delivery('good', payload({ repository: { full_name: B } }), { repository: B }) },
    } })
    await h.hook.observe(A)
    await h.hook.observe(B)
    assert.equal(h.hook.summary().counters.errors, 1)
    assert.equal(h.hook.summary().counters.wouldRecord, 1)
    assert.equal(h.hook.summary().counters.deferred, 0)
    h.hook.emit()
    safeSummary(h.hook.summary())
  })
}

for (const reason of ['deadline', 'request-budget', 'cancelled', null]) {
  test(`hook contains only deadline, not ${String(reason)} stops after an awaited read`, async () => {
    let stopped = false
    const h = hookFixture({ check: () => { if (stopped) throw new Error('stopped') }, stopReason: () => reason,
      capture: { listDeliveries: async () => { stopped = true; throw new Error(privateText) },
        getDelivery: async () => { assert.fail('no body read after stop') } } })
    if (reason === 'deadline') {
      await h.hook.observe(A)
      assert.equal(h.hook.summary().counters.deferred, 1)
      assert.equal(h.hook.summary().counters.errors, 0)
      assert.throws(() => h.hook.emit())
      h.hook.emit({ ignoreStop: true })
      assert.equal(h.logs.length, 1)
    } else {
      await assert.rejects(h.hook.observe(A), /consumer pass stopped/)
      assert.equal(h.hook.summary().counters.total, 0)
      assert.deepEqual(h.logs, [])
    }
  })
}

test('bad clock and broken log sink are measured without forwarding exception messages', async () => {
  const h = hookFixture({ nowMs: () => -1, log: () => { throw new Error(privateText) } })
  await h.hook.observe(A)
  assert.equal(h.hook.summary().counters.errors, 1)
  h.hook.emit()
  assert.equal(h.hook.summary().counters.logErrors, 1)
  safeSummary(h.hook.summary())
})

// Catches early/concurrent drains and a deployment accidentally entering receipts.
test('runner observes under lock AFTER both primary streams, sequentially, with no new durable state', async (t) => {
  const transcripts = []
  for (const closeout of [undefined, enrolled()]) {
    const h = await fixture(t, { closeout })
    h.fetchHook = async (url) => {
      if (url.searchParams.get('event') !== 'deployment_status' && !url.pathname.startsWith('/events/')) return
      assert.equal((await stat(join(h.config.stateDirectory, 'lock'))).isDirectory(), true)
      assert.deepEqual((await h.state()).repos[A].backfill.done, [7])
      assert.equal(h.calls.filter(([kind]) => kind === 'primary').length, 1)
      assert.equal(h.calls.filter(([kind, , value]) => kind === 'http' &&
        ['pull_request', 'check_suite'].includes(new URL(value).searchParams.get('event'))).length, 2)
      await new Promise((resolve) => setImmediate(resolve))
      assert.equal(h.calls.at(-1)[2], url.href, 'no concurrent or fire-and-forget work while a read is unsettled')
    }
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(result.requests, h.calls.length)
    assert.deepEqual((await readdir(h.config.stateDirectory)).sort(), ['receipts.json', 'schedule.json', 'sweeps.json'])
    if (closeout) {
      assert.equal(h.deployCalls().length, 2)
      assert.equal(result.deployShadow.counters.wouldRecord, 1)
      safeSummary(result.deployShadow)
      assert.equal(h.summaryLogs().length, 1)
    }
    transcripts.push({ events: result.events, sweeps: result.sweeps, bytes: await stored(h) })
    await assert.rejects(stat(join(h.config.stateDirectory, 'lock')), { code: 'ENOENT' })
    const count = h.calls.length
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(h.calls.length, count)
  }
  assert.deepEqual(transcripts[1], transcripts[0])
})

test('empty PR set still observes only enrolled repos and fresh passes repeat the shadow observation', async (t) => {
  const h = await fixture(t, { closeout: enrolled([B]), repos: [A, B], open: {},
    rows: [delivery('other', payload({ repository: { full_name: B } }), { repository: B })] })
  for (let n = 0; n < 2; n++) {
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(result.deployShadow.counters.wouldRecord, 1)
    assert.equal(h.summaryLogs().length, n + 1)
  }
  const lists = h.deployCalls().filter(([, , url]) => new URL(url).pathname === '/events')
  assert.equal(lists.length, 2)
  assert.equal(lists.every(([, , url]) => new URL(url).searchParams.get('repository') === B), true)
  assert.equal(lists.every(([, , url]) => new URL(url).searchParams.get('since') === '2026-10-13T12:00:00.000Z'), true)
})

for (const [maxRequests, reads, wouldRecord, deferred] of [[4, 0, 0, 1], [5, 1, 0, 1], [6, 2, 1, 0]]) {
  test(`shared surplus ${maxRequests} admits each list/body request before reserve, without whole-pass stop`, async (t) => {
    const h = await fixture(t, { closeout: enrolled(), limits: { maxRequests } })
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(result.reason, null)
    assert.equal(result.requests, maxRequests)
    assert.equal(h.deployCalls().length, reads)
    assert.equal(result.deployShadow.counters.wouldRecord, wouldRecord)
    assert.equal(result.deployShadow.counters.deferred, deferred)
    assert.equal(result.deployShadow.counters.errors, 0)
    assert.equal(h.summaryLogs().length, 1)
  })
}

for (const maxRequests of [5, 6, 7, 8, 9]) {
  test(`pagination and each body share guarded surplus at ${maxRequests} requests`, async (t) => {
    const h = await fixture(t, { closeout: enrolled(), limits: { maxRequests, pageSize: 1 },
      rows: [delivery('newer'), delivery('older', payload(), { received_ms: wall - 2000 })] })
    const result = await h.pass()
    assert.equal(result.ok, true)
    assert.equal(result.reason, null)
    assert.equal(result.requests, maxRequests)
    assert.equal(result.deployShadow.counters.deferred, Number(maxRequests < 9))
    assert.equal(result.deployShadow.counters.errors, 0)
    if (maxRequests < 8) assert.equal(result.deployShadow.counters.wouldRecord, 0)
    if (maxRequests === 9) assert.ok(result.deployShadow.counters.wouldRecord >= 1)
    const reads = h.deployCalls().map(([, , url]) => new URL(url))
    assert.equal(reads.length, maxRequests - 4)
    assert.equal(reads.filter((u) => u.pathname.startsWith('/events/')).length, Math.max(0, maxRequests - 7))
  })
}

test('page-cap and malformed capture failures remain loud; old history is never fetched', async (t) => {
  const h = await fixture(t, { closeout: enrolled(), limits: { pageSize: 1, maxPages: 1 }, rows: [delivery()] })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.deployShadow.counters.errors, 1, 'incomplete listing is not a measured empty history')
  assert.equal(result.deployShadow.counters.wouldRecord, 0)
  assert.equal(h.deployCalls().length, 1)
  const old = await fixture(t, { closeout: enrolled(), rows: [delivery('old', payload(), { received_ms: weekAgo - 1 })] })
  const aged = await old.pass()
  assert.equal(aged.deployShadow.counters.deliveries, 0)
  assert.equal(old.deployCalls().length, 1)
})

for (const phase of ['list', 'get', 'list-body', 'get-body']) {
  test(`deadline during ${phase} is contained only in drain and prevents further reads`, async (t) => {
    const h = await fixture(t, { closeout: enrolled(), limits: { durationMs: 100 } })
    let now = 0
    let chunks = 0
    h.fetchHook = async (url) => {
      const selected = phase.startsWith('list') ? url.searchParams.get('event') === 'deployment_status'
        : url.pathname.startsWith('/events/')
      if (!selected) return
      if (!phase.endsWith('-body')) { now = 100; return }
      const row = delivery()
      const text = JSON.stringify(phase.startsWith('list') ? { events: [row], count: 1, next_cursor: null } : row)
      return { status: 200, redirected: false, headers: new Headers({ 'content-type': 'application/json' }),
        body: { getReader: () => ({ read: async () => {
          chunks++
          assert.equal(chunks, 1, 'deadline must be checked before another body read')
          now = 100
          return { done: false, value: new TextEncoder().encode(text) }
        }, cancel: async () => {}, releaseLock: () => {} }) } }
    }
    const result = await h.pass({ clock: fakeClock(() => now) })
    assert.equal(result.ok, true)
    assert.equal(result.reason, null)
    assert.equal(result.deployShadow.counters.deferred, 1)
    assert.equal(result.deployShadow.counters.wouldRecord, 0)
    assert.equal(result.deployShadow.counters.errors, 0)
    assert.equal(h.deployCalls().length, phase.startsWith('list') ? 1 : 2)
    assert.equal(h.summaryLogs().length, 1)
    assert.deepEqual((await h.state()).repos[A].backfill.done, [7])
  })
}

for (const reason of ['deadline', 'request-budget', 'cancelled']) {
  test(`pre-drain ${reason} preserves off-mode failure and emits no successful observation`, async (t) => {
    const outcomes = []
    for (const closeout of [undefined, enrolled()]) {
      const h = await fixture(t, { closeout, limits: reason === 'request-budget' ? { maxRequests: 3 } : { durationMs: 100 } })
      let now = 0
      const controller = new AbortController()
      h.primaryHook = async () => { if (reason === 'deadline') now = 100; if (reason === 'cancelled') controller.abort() }
      const result = await h.pass({ signal: controller.signal, clock: fakeClock(() => now) })
      assert.equal(result.ok, false)
      assert.equal(result.reason, reason)
      assert.deepEqual(h.deployCalls(), [])
      assert.deepEqual(h.summaryLogs(), [])
      if (closeout) assert.equal(result.deployShadow.counters.deferred, 0)
      outcomes.push({ ok: result.ok, reason: result.reason, events: result.events, sweeps: result.sweeps, bytes: await stored(h) })
    }
    assert.deepEqual(outcomes[1], outcomes[0])
  })
}

for (const phase of ['list', 'get', 'body']) {
  test(`cancellation during ${phase} settles under lock and fails without logs or post-return work`, async (t) => {
    const h = await fixture(t, { closeout: enrolled() })
    const controller = new AbortController()
    let settled = false
    h.fetchHook = async (url, init) => {
      if (phase === 'list' ? url.searchParams.get('event') !== 'deployment_status' : !url.pathname.startsWith('/events/')) return
      const settle = async () => {
        controller.abort()
        assert.equal(init.signal.aborted, true)
        await new Promise((resolve) => setImmediate(resolve))
        assert.equal((await stat(join(h.config.stateDirectory, 'lock'))).isDirectory(), true)
        settled = true
        throw new Error(privateText)
      }
      if (phase !== 'body') return settle()
      return { status: 200, redirected: false, headers: new Headers({ 'content-type': 'application/json' }),
        body: { getReader: () => ({ read: settle, cancel: async () => {}, releaseLock: () => {} }) } }
    }
    const result = await h.pass({ signal: controller.signal, clock: fakeClock() })
    assert.equal(result.ok, false)
    assert.equal(result.reason, 'cancelled')
    assert.equal(settled, true)
    assert.equal(result.deployShadow.counters.wouldRecord, 0)
    assert.deepEqual(h.summaryLogs(), [])
    await assert.rejects(stat(join(h.config.stateDirectory, 'lock')), { code: 'ENOENT' })
    const count = h.calls.length
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(h.calls.length, count)
  })
}

for (const failure of ['transport', 'malformed', 'truncated', 'wrong-event']) {
  test(`${failure} runner observation errors are sanitized and preserve primary persisted bytes`, async (t) => {
    const values = []
    for (const closeout of [undefined, enrolled()]) {
      const h = await fixture(t, { closeout })
      h.fetchHook = async (url) => {
        if (!url.pathname.startsWith('/events/')) return
        if (failure === 'transport') throw new Error(privateText)
        const row = delivery()
        if (failure === 'malformed') row.body = privateText
        if (failure === 'truncated') row.body_truncated = 1
        if (failure === 'wrong-event') row.event = 'pull_request'
        return Response.json(row)
      }
      const result = await h.pass()
      assert.equal(result.ok, true)
      if (closeout) {
        assert.equal(result.deployShadow.counters.errors, 1)
        assert.equal(result.deployShadow.counters.wouldRecord, 0)
        safeSummary(result.deployShadow)
      }
      assert.equal(JSON.stringify(h.logs).includes(privateText), false)
      values.push({ events: result.events, sweeps: result.sweeps, bytes: await stored(h) })
    }
    assert.deepEqual(values[1], values[0])
  })
}

test('primary errors stay failed even if a deployment shadow observation succeeds', async (t) => {
  const h = await fixture(t, { closeout: enrolled() })
  h.primaryHook = async () => { throw new Error(privateText) }
  const result = await h.pass()
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'sweeps-incomplete')
  assert.equal(result.deployShadow.counters.wouldRecord, 1)
  assert.equal(h.summaryLogs().length, 1)
  assert.deepEqual((await h.state()).repos[A].backfill.done, [])
})

test('disarm and red-main coexist without suppression, writes or unbounded logs', async (t) => {
  const h = await fixture(t, { closeout: { ...enrolled(), disarmShadow: { enabledRepositories: [A] },
    redmainShadow: { enabledRepositories: [A] } } })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.disarmShadow.counters.evaluated, 1)
  assert.equal(result.redmainShadow.counters.wouldOpen, 1)
  assert.equal(result.deployShadow.counters.wouldRecord, 1, 'red-main counters cannot suppress deploy shadow')
  safeSummary(result.deployShadow)
  assert.equal(h.logs.filter((line) => line.event === 'closeout-disarm-shadow').length, 1)
  assert.equal(h.logs.filter((line) => line.event === 'closeout-redmain-shadow').length, 1)
  assert.equal(h.summaryLogs().length, 1)
  assert.deepEqual(Object.keys(h.summaryLogs()[0]).sort(), ['byReason', 'counters', 'event'])
  assert.equal(JSON.stringify(h.logs).includes(privateText), false)
})

test('coexisting observers share a fully spent surplus without whole-pass request-budget failure', async (t) => {
  const h = await fixture(t, { closeout: { ...enrolled(), disarmShadow: { enabledRepositories: [A] },
    redmainShadow: { enabledRepositories: [A] } }, limits: { maxRequests: 4 } })
  const result = await h.pass()
  assert.equal(result.ok, true)
  assert.equal(result.reason, null)
  assert.equal(result.requests, 4)
  for (const key of ['disarmShadow', 'redmainShadow', 'deployShadow']) {
    assert.equal(result[key].counters.deferred, 1)
    assert.equal(result[key].counters.errors, 0)
  }
  assert.deepEqual(h.deployCalls(), [])
  assert.equal(h.logs.length, 3)
})

test('a deadline in the earlier disarm drain also defers deploy without hiding primary failure state', async (t) => {
  const h = await fixture(t, { closeout: { ...enrolled(), disarmShadow: { enabledRepositories: [A] } },
    limits: { durationMs: 100 } })
  let now = 0
  const run = h.run
  h.run = async (...args) => {
    const value = await run(...args)
    if (args[1][5].includes('BridgeCloseoutSnapshot')) now = 100
    return value
  }
  const result = await h.pass({ clock: fakeClock(() => now) })
  assert.equal(result.ok, true)
  assert.equal(result.reason, null)
  assert.equal(result.disarmShadow.counters.deferred, 1)
  assert.equal(result.deployShadow.counters.deferred, 1)
  assert.equal(result.deployShadow.counters.errors, 0)
  assert.deepEqual(h.deployCalls(), [])
  assert.equal(h.summaryLogs().length, 1)
})

// SourceTextModule isolates the runner's existing index dependency without a
// production injection seam. A controlled index promise proves it is awaited
// before observation, including when the index has already spent the deadline.
test('index settlement precedes deployment drain; pre-drain index overrun cannot be contained', () => {
  const script = `
    import assert from 'node:assert/strict';
    import { readFile, mkdtemp, rm, stat } from 'node:fs/promises';
    import { SourceTextModule, SyntheticModule } from 'node:vm';
    import { join } from 'node:path';
    import { tmpdir } from 'node:os';
    const base = ${JSON.stringify(new URL('../src/', import.meta.url).href)};
    for (const enableDeploy of [false, true]) for (const overrun of [false, true]) {
      let settle, settled = false, now = 0, deployReads = 0;
      const timers = [];
      const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'deploy-index-'));
      const cache = new Map();
      function load(url) {
        if (cache.has(url)) return cache.get(url);
        const task = (async () => {
        let mod;
        if (url.endsWith('/pr-task-index-source.js')) {
          mod = new SyntheticModule(['refreshPrTaskIndex'], function () {
            this.setExport('refreshPrTaskIndex', async () => {
              await new Promise(resolve => { settle = resolve; setImmediate(() => timers[1]()) });
              assert.equal((await stat(join(root, 'lock'))).isDirectory(), true);
              settled = true;
              if (overrun) now = 100;
              return null;
            });
          });
        } else if (url.endsWith('/closeout-deploy-wiring.js')) {
          const native = await import(url);
          mod = new SyntheticModule(Object.keys(native), function () {
            for (const key of Object.keys(native)) this.setExport(key, native[key]);
            this.setExport('createDeployShadowHook', (deps) => {
              assert.equal(Object.keys(deps).every(key =>
                ['settings', 'capture', 'check', 'stopReason', 'log', 'nowMs'].includes(key)), true);
              assert.deepEqual(Object.keys(deps.capture).sort(), ['getDelivery', 'listDeliveries']);
              return native.createDeployShadowHook(deps);
            });
          });
        } else if (url.endsWith('/sweep-cycle.js')) {
          const native = await import(url);
          mod = new SyntheticModule(Object.keys(native), function () {
            for (const key of Object.keys(native)) this.setExport(key, native[key]);
            this.setExport('runSweepCycle', async (...args) => {
              const result = await native.runSweepCycle(...args);
              const resolve = settle; settle = null; setImmediate(resolve);
              return result;
            });
          });
        } else if (url.startsWith('node:')) {
          const native = await import(url);
          mod = new SyntheticModule(Object.keys(native), function () {
            for (const key of Object.keys(native)) this.setExport(key, native[key]);
          });
        } else mod = new SourceTextModule(await readFile(new URL(url), 'utf8'), { identifier: url });
        return mod;
        })();
        cache.set(url, task);
        return task;
      }
      try {
        const mod = await load(new URL('consumer-runner.js', base).href);
        await mod.link((specifier, parent) => load(specifier.startsWith('node:') ? specifier : new URL(specifier, parent.identifier).href));
        await mod.evaluate();
        const config = { version: 1, mode: 'products-only-v1', captureOrigin: 'https://capture.example.test',
          boardOrigin: 'https://board.example.test', companyId: '00000000-0000-4000-8000-000000000009',
          allowedRepositories: ['ExampleOrg/widget'], stateDirectory: root, limits: { durationMs: 100 },
          closeout: { shadow: { enabledRepositories: ['ExampleOrg/widget'], indexReadsPerPass: 1 },
            ...(enableDeploy ? { deployShadow: { enabledRepositories: ['ExampleOrg/widget'] } } : {}) } };
        const result = await mod.namespace.runProductPass({ config, captureToken: 'fixture', boardToken: 'fixture' }, {
          clock: { now: () => now, setTimer: (fn) => { timers.push(fn); return timers.length }, clearTimer: () => {} },
          fetchImpl: async (url, init) => {
            assert.equal(init.method, 'GET');
            if (new URL(url).searchParams.get('event') === 'deployment_status') {
              assert.equal(settled, true); deployReads++;
            }
            return Response.json({ events: [], count: 0, next_cursor: null });
          },
          run: async () => ({ stdout: JSON.stringify({ data: { repository: { nameWithOwner: 'ExampleOrg/widget',
            pullRequests: { totalCount: 0, nodes: [], pageInfo: { hasNextPage: false, endCursor: null } } } } }) }),
        });
        assert.equal(settled, true);
        assert.equal(result.ok, !overrun);
        assert.equal(result.reason, overrun ? 'deadline' : null);
        assert.equal(deployReads, !enableDeploy || overrun ? 0 : 1);
        if (enableDeploy) assert.equal(result.deployShadow.counters.deferred, 0);
        else console.log('off-mode index control passed: overrun=' + overrun);
        await assert.rejects(stat(join(root, 'lock')), { code: 'ENOENT' });
      } finally { await rm(root, { recursive: true, force: true }) }
    }
    console.log('index settled under lock; late pre-drain deadline preserved');
  `
  const result = spawnSync(process.execPath, ['--experimental-vm-modules', '--input-type=module', '-e', script],
    { encoding: 'utf8', timeout: 10000 })
  assert.equal(result.status, 0, result.stdout + result.stderr)
  assert.match(result.stdout, /index settled under lock; late pre-drain deadline preserved/)
})
