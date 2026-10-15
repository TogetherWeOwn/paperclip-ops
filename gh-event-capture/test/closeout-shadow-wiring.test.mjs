import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile, rm, stat, readdir } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { runProductPass } from '../src/consumer-runner.js'
import { loadConsumerConfig } from '../src/consumer-config.js'
import { main as cliMain } from '../src/consumer-cli.js'
import { receiptNamespace } from '../src/receipt-cycle.js'
import { claimNamespace, freshClaimState, addClaim, saveClaimState, CLAIM_FILE } from '../src/claim-store.js'
import { pendingNamespace, freshPendingState, recordObservation, savePendingState,
  loadPendingState } from '../src/closeout-pending.js'
import { buildPrTaskIndex } from '../src/pr-task-index.js'
import { closeoutShadowSettings, createShadowReviewHook, SHADOW_POLICY } from '../src/closeout-shadow-wiring.js'
import { createApp } from '../src/app.js'
import { createMemoryStore } from '../src/store-memory.js'

const REPO_A = 'TogetherWeOwn/paperclip-ops'
const REPO_B = 'TogetherWeOwn/kofra'
const companyId = '00000000-0000-4000-8000-000000000004'
const secret = 'PRIVATE-TEST-KEY'
const pushedAt = '2026-10-03T07:00:00.000Z'
const pushedMs = Date.parse(pushedAt)
const sha = (n) => String(n % 10).repeat(40)
const envelope = (repo, fields) => ({ data: { repository: { nameWithOwner: repo, isPrivate: true, ...fields } } })
const prNode = (repo, number, over = {}) => ({ number, url: `https://github.com/${repo}/pull/${number}`,
  title: 'Shadow PR', body: '', state: 'OPEN', isDraft: false, merged: false, headRefName: 'feature-x',
  headRefOid: sha(number), reviewDecision: null, mergeStateStatus: 'DIRTY', baseRefName: 'main',
  autoMergeRequest: null, author: { __typename: 'User', login: 'human' }, baseRepository: { nameWithOwner: repo },
  labels: { nodes: [], pageInfo: { hasNextPage: false } },
  commits: { nodes: [{ commit: { oid: sha(number), pushedDate: pushedAt,
    statusCheckRollup: { contexts: { nodes: [], pageInfo: { hasNextPage: false } } } } }] }, ...over })

async function harness(t, { repos = [REPO_A], closeout, open = { [REPO_A]: [7, 8] } } = {}) {
  const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'closeout-shadow-wiring-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const config = { version: 1, mode: 'products-only-v1', captureOrigin: 'https://capture.test',
    boardOrigin: 'https://board.test', companyId, allowedRepositories: repos, stateDirectory: join(root, 'state'),
    captureTokenFile: join(root, 'capture.key'), boardTokenFile: join(root, 'board.key'), limits: {},
    ...(closeout === undefined ? {} : { closeout }) }
  await writeFile(config.captureTokenFile, secret + '\n', { mode: 0o600 })
  await writeFile(config.boardTokenFile, secret, { mode: 0o600 })
  const app = createApp({ store: createMemoryStore(), webhookSecret: 'test', queryToken: secret, now: () => 1000 })
  const h = { root, config, open, calls: [], boardCalls: [], gh: [], logs: [], rules: ['ci-ok', 'pr-lint'],
    ruleFails: false, snapshotFails: new Set(), onRules: null, onSnapshot: null }
  h.fetch = async (url, init) => {
    // The index reads the open set; no other board route is reachable here.
    if (!url.startsWith(config.captureOrigin)) {
      h.boardCalls.push([init.method, url])
      assert.equal(init.method, 'GET')
      assert.equal(new URL(url).pathname, `/api/companies/${companyId}/issues`)
      return new Response('[]', { headers: { 'content-type': 'application/json' } })
    }
    h.calls.push([init.method, url])
    return app(new Request(url, init))
  }
  h.run = async (file, args) => {
    if (args.includes('--method')) {
      h.gh.push(['rules', args.at(-1)])
      if (h.onRules) await h.onRules()
      if (h.ruleFails) throw new Error(secret)
      return { stdout: JSON.stringify([{ type: 'required_status_checks',
        parameters: { strict_required_status_checks_policy: false, do_not_enforce_on_create: false,
          required_status_checks: h.rules.map((context) => ({ context, integration_id: 15368 })) } }]) }
    }
    const query = String(args[5])
    const repository = `${args.find((a) => a.startsWith('owner=')).slice(6)}/${args.find((a) => a.startsWith('name=')).slice(5)}`
    if (query.includes('BridgeOpenPullRequests')) {
      h.gh.push(['list', repository])
      const numbers = h.open[repository] ?? []
      return { stdout: JSON.stringify(envelope(repository, { pullRequests: { totalCount: numbers.length,
        nodes: numbers.map((number) => ({ number })), pageInfo: { hasNextPage: false, endCursor: null } } })) }
    }
    const number = Number(args.find((a) => a.startsWith('number=')).slice(7))
    if (query.includes('BridgeCloseoutSnapshot')) {
      h.gh.push(['snapshot', repository, number])
      if (h.onSnapshot) await h.onSnapshot(number)
      if (h.snapshotFails.has(number)) throw new Error(secret)
      return { stdout: JSON.stringify(envelope(repository, { pullRequest: prNode(repository, number) })) }
    }
    h.gh.push(['pr', repository, number])
    return { stdout: JSON.stringify(envelope(repository, { pullRequest: prNode(repository, number) })) }
  }
  h.indexLogs = []
  h.pass = (overrides = {}) => runProductPass({ config: h.config, captureToken: secret, boardToken: secret },
    { fetchImpl: h.fetch, run: h.run, log: (line) =>
      (JSON.parse(line).event === 'closeout-index' ? h.indexLogs : h.logs).push(line), ...overrides })
  // Debounce is a persisted first-seen clock: first seen at 0 is due now.
  h.seedDue = async (entries) => {
    const state = freshPendingState(pendingNamespace(receiptNamespace(h.config)))
    for (const [repository, number] of entries) recordObservation(state, { repository, number }, 0)
    await savePendingState({ directory: h.config.stateDirectory, state })
  }
  h.kinds = () => h.gh.map(([kind]) => kind)
  // Pass 1 completes the one-shot backfill; the review snapshot (and the hook)
  // fires from pass 2 on.
  h.reachReview = async (entries = Object.entries(h.open).flatMap(([r, ns]) => ns.map((n) => [r, n]))) => {
    const first = await h.pass()
    assert.equal(first.ok, true)
    await h.seedDue(entries)
    h.gh.length = 0
    h.logs.length = 0
    return first
  }
  return h
}

const counters = (over = {}) => ({ total: 0, mapped: 0, unmapped: 0, unmappedByReason: {}, noException: 0,
  exceptions: 0, byClass: {}, wouldClaim: 0, alreadyClaimed: 0, errors: 0, ...over })

test('no closeout block, an empty one and an empty repository list all run the old sweep path', async (t) => {
  const transcripts = []
  for (const closeout of [undefined, {}, { shadow: { enabledRepositories: [] } }]) {
    const h = await harness(t, { closeout })
    assert.equal((await h.pass()).ok, true)
    h.gh.length = 0
    const second = await h.pass()
    assert.equal(second.ok, true)
    assert.equal('closeoutShadow' in second.sweeps, false)
    assert.deepEqual(h.kinds().filter((k) => k !== 'list' && k !== 'pr'), [], 'no snapshot or ruleset read')
    assert.deepEqual(h.logs, [])
    assert.deepEqual((await readdir(h.config.stateDirectory)).filter((f) => /claims|pending/.test(f)), [])
    transcripts.push({ gh: h.gh, keys: Object.keys(second.sweeps).sort(), sweepsProcessed: second.sweeps.processed })
  }
  assert.deepEqual(transcripts[1], transcripts[0])
  assert.deepEqual(transcripts[2], transcripts[0])
})

test('an enrolled repository runs the shadow once per due PR through runProductPass', async (t) => {
  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  await h.reachReview()
  const second = await h.pass()
  assert.equal(second.ok, true)
  assert.deepEqual(h.gh.filter(([k]) => k === 'rules'), [['rules', `repos/${REPO_A}/rules/branches/main`]])
  assert.deepEqual(h.gh.filter(([k]) => k === 'snapshot').map(([, , n]) => n).sort(), [7, 8])
  // The live consumer passes no PR-task index, so nothing can map yet.
  assert.deepEqual(second.sweeps.closeoutShadow[REPO_A].counters,
    counters({ total: 2, unmapped: 2, unmappedByReason: { 'no-ref': 2 } }))
  assert.deepEqual(second.sweeps.closeoutShadow[REPO_A].pending,
    { observed: 2, observedNew: 0, due: 2, debounced: 0, flushed: 2, pruned: 0 })
  assert.deepEqual((await loadPendingState({ directory: h.config.stateDirectory,
    namespace: pendingNamespace(receiptNamespace(h.config)) })).pending, [])
  assert.equal(h.logs.length, 1)
  assert.deepEqual(JSON.parse(h.logs[0]), { event: 'closeout-shadow', repository: REPO_A,
    counters: counters({ total: 2, unmapped: 2, unmappedByReason: { 'no-ref': 2 } }),
    pending: { observed: 2, observedNew: 0, due: 2, debounced: 0, flushed: 2, pruned: 0 } })
  // The review interval has not elapsed: the next pass reads nothing new.
  h.gh.length = 0
  const third = await h.pass()
  assert.equal(third.ok, true)
  assert.deepEqual(h.kinds().filter((k) => k === 'snapshot' || k === 'rules'), [])
  assert.deepEqual(third.sweeps.closeoutShadow, {})
})

test('a repository that is not enrolled is untouched in the same pass', async (t) => {
  const h = await harness(t, { repos: [REPO_A, REPO_B], open: { [REPO_A]: [7], [REPO_B]: [9] },
    closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  await h.reachReview()
  const second = await h.pass()
  assert.equal(second.ok, true)
  assert.deepEqual(h.gh.filter(([k]) => k === 'snapshot'), [['snapshot', REPO_A, 7]])
  assert.deepEqual(h.gh.filter(([k]) => k === 'rules').map(([, p]) => p), [`repos/${REPO_A}/rules/branches/main`])
  assert.equal(second.sweeps.closeoutShadow[REPO_B], null)
  assert.equal(second.sweeps.closeoutShadow[REPO_A].counters.total, 1)
  const pending = await loadPendingState({ directory: h.config.stateDirectory,
    namespace: pendingNamespace(receiptNamespace(h.config)) })
  assert.deepEqual(pending.pending.map(([key]) => JSON.parse(key)[1]), [REPO_B], 'B keeps its own untouched debounce')
  assert.deepEqual(h.logs.map((l) => JSON.parse(l).repository), [REPO_A])
})

// The shadow is observational: whatever breaks for one repository is a counted
// error there and never moves the events stream, the reconciliations or `ok`.
const isolation = [
  ['an unreadable ruleset', 'policy-unavailable', (h) => { h.ruleFails = true }],
  ['an empty ruleset', 'policy-unavailable', (h) => { h.rules = [] }],
  ['a corrupt claims file', 'claims-unreadable',
    (h) => writeFile(join(h.config.stateDirectory, CLAIM_FILE), '{not json', { mode: 0o600 })],
  ['a corrupt pending file', 'review-failed',
    (h) => writeFile(join(h.config.stateDirectory, 'pending.json'), '{not json', { mode: 0o600 })],
]
for (const [name, label, break_] of isolation) {
  test(`${name} is a counted shadow error that never fails the pass`, async (t) => {
    const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
    await h.reachReview()
    await break_(h)
    const second = await h.pass()
    assert.equal(second.ok, true)
    assert.equal(second.events.ok, true)
    assert.equal(second.sweeps.ok, true)
    assert.equal(second.sweeps.processed, 2, 'the review reconciliations still ran')
    const shadow = second.sweeps.closeoutShadow[REPO_A]
    assert.equal(shadow.error, label)
    assert.deepEqual(shadow.counters, counters({ errors: 1 }))
    assert.deepEqual(shadow.records, [])
    assert.deepEqual(h.logs.map((l) => JSON.parse(l)).map(({ event, error }) => [event, error]),
      [['closeout-shadow', label]])
    assert.equal(h.logs.join('').includes(secret), false, 'no child stream or error text is logged')
  })
}

test('one unreadable snapshot is a per-PR error record, not a shadow failure', async (t) => {
  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  await h.reachReview()
  h.snapshotFails.add(7)
  const second = await h.pass()
  assert.equal(second.ok, true)
  assert.deepEqual(second.sweeps.closeoutShadow[REPO_A].counters,
    counters({ total: 2, unmapped: 1, unmappedByReason: { 'no-ref': 1 }, errors: 1 }))
  const lines = h.logs.map((l) => JSON.parse(l))
  assert.deepEqual(lines[1], { event: 'closeout-shadow-decision', ref: `${REPO_A}#7@unknown`,
    decision: null, claim: 'none', reason: 'snapshot-read-failed' })
  assert.equal(h.logs.join('').includes(secret), false)
})

test('a whole-pass stop escapes the shadow instead of becoming a counted error', async (t) => {
  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  await h.reachReview()
  const controller = new AbortController()
  h.onRules = async () => { controller.abort() }
  const stopped = await h.pass({ signal: controller.signal })
  assert.equal(stopped.ok, false)
  assert.equal(stopped.reason, 'cancelled')
  assert.equal(stopped.sweeps, null)
  assert.deepEqual(h.logs, [], 'a stop is not logged as a shadow failure')
})

test('a stop during a snapshot read escapes the shadow instead of logging per-PR errors', async (t) => {
  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  await h.reachReview()
  const controller = new AbortController()
  // The abort lands mid-review: PR 7 reads, then PR 8's read is refused by the
  // pass budget and the per-PR catch in runCloseoutShadow records it as an error.
  h.onSnapshot = async (number) => { if (number === 7) controller.abort() }
  const stopped = await h.pass({ signal: controller.signal })
  assert.equal(stopped.ok, false)
  assert.equal(stopped.reason, 'cancelled')
  assert.equal(stopped.sweeps, null)
  assert.deepEqual(h.logs, [], 'a stop during a snapshot read is not logged as a shadow result')
})

test('the shadow never writes a claim, comments or wakes; the index only lists the board', async (t) => {
  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  await h.reachReview()
  const second = await h.pass()
  assert.equal(second.ok, true)
  assert.equal(second.sweeps.closeoutShadow[REPO_A].counters.total, 2, 'the shadow really ran')
  // Absent claims stay absent: the runner loads read-only and never saves.
  assert.equal((await readdir(h.config.stateDirectory)).includes(CLAIM_FILE), false)
  assert.equal(h.boardCalls.length, 2, 'one open-set read per pass')
  assert.equal(h.boardCalls.every(([method, url]) => method === 'GET' &&
    new URL(url).pathname === `/api/companies/${companyId}/issues`), true)
  assert.equal(h.calls.every(([method]) => method === 'GET'), true, 'capture is read-only')
  assert.equal(h.logs.some((line) => /comment|wake|mention/i.test(line)), false)
})

test('an existing claim file keeps its bytes and its inode through a shadow pass', async (t) => {
  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  await h.reachReview()
  const claims = freshClaimState(claimNamespace(receiptNamespace(h.config)))
  addClaim(claims, { repository: REPO_A, number: 7, headSha: sha(7), class: 'conflict' }, 5000)
  await saveClaimState({ directory: h.config.stateDirectory, state: claims })
  const path = join(h.config.stateDirectory, CLAIM_FILE)
  const before = { bytes: await readFile(path, 'utf8'), info: await stat(path) }
  const second = await h.pass()
  assert.equal(second.ok, true)
  assert.equal(second.sweeps.closeoutShadow[REPO_A].counters.total, 2, 'the shadow really ran')
  const after = { bytes: await readFile(path, 'utf8'), info: await stat(path) }
  assert.equal(after.bytes, before.bytes)
  assert.equal(after.info.ino, before.info.ino, 'no atomic rewrite either')
  assert.equal(after.info.mtimeMs, before.info.mtimeMs)
  assert.equal(h.boardCalls.length, 2)
  assert.equal(h.boardCalls.every(([method]) => method === 'GET'), true)
})

test('toggling the closeout block never changes the receipt namespace', async (t) => {
  const base = await harness(t)
  const enabled = { ...base.config, closeout: { shadow: { enabledRepositories: [REPO_A], debounceMs: 5000 } } }
  assert.equal(receiptNamespace(enabled), receiptNamespace(base.config))
  assert.equal(receiptNamespace({ ...base.config, closeout: {} }), receiptNamespace(base.config))
  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  const namespace = receiptNamespace({ ...h.config, closeout: undefined })
  assert.equal((await h.pass()).ok, true)
  assert.equal(JSON.parse(await readFile(join(h.config.stateDirectory, 'receipts.json'), 'utf8')).namespace, namespace)
  // Removing the key later resumes the same receipts instead of refusing a namespace mismatch.
  delete h.config.closeout
  assert.equal((await h.pass()).ok, true)
  h.config.closeout = { shadow: { enabledRepositories: [REPO_A] } }
  assert.equal((await h.pass()).ok, true)
  assert.equal(JSON.parse(await readFile(join(h.config.stateDirectory, 'receipts.json'), 'utf8')).namespace, namespace)
})

test('the closeout block is validated fail-closed', () => {
  const base = { allowedRepositories: [REPO_A, REPO_B] }
  const settings = (closeout) => closeoutShadowSettings({ ...base, closeout })
  assert.equal(closeoutShadowSettings(base), null)
  assert.equal(settings({}), null)
  assert.equal(settings({ shadow: { enabledRepositories: [] } }), null)
  const on = settings({ shadow: { enabledRepositories: [REPO_A] } })
  assert.deepEqual([...on.enabledRepositories], [REPO_A])
  assert.equal(on.debounceMs, 90000)
  assert.equal(settings({ shadow: { enabledRepositories: [REPO_A, REPO_B], debounceMs: 1000 } }).debounceMs, 1000)
  assert.equal(settings({ shadow: { enabledRepositories: [REPO_A], debounceMs: 3600000 } }).debounceMs, 3600000)
  const refused = [
    { other: true },
    { shadow: { enabledRepositories: [REPO_A], extra: 1 } },
    { shadow: {} },
    { shadow: { debounceMs: 5000 } },
    { shadow: { enabledRepositories: REPO_A } },
    { shadow: { enabledRepositories: ['TogetherWeOwn/not-allowed'] } },
    { shadow: { enabledRepositories: ['not-a-repo'] } },
    { shadow: { enabledRepositories: ['../x/y'] } },
    { shadow: { enabledRepositories: [REPO_A, REPO_A] } },
    { shadow: { enabledRepositories: [REPO_A], debounceMs: 999 } },
    { shadow: { enabledRepositories: [REPO_A], debounceMs: 3600001 } },
    { shadow: { enabledRepositories: [REPO_A], debounceMs: 1500.5 } },
    { shadow: { enabledRepositories: [REPO_A], debounceMs: '5000' } },
    { shadow: null },
    [],
    null,
    'on',
  ]
  for (const closeout of refused) assert.throws(() => settings(closeout), /closeout/, JSON.stringify(closeout))
})

test('enabled shadow budgets cover capped open-set pagination and configured reads', async (t) => {
  const config = (maxRequests, indexReadsPerPass = 1, enabledRepositories = [REPO_A]) =>
    closeoutShadowSettings({ allowedRepositories: [REPO_A], limits: { maxRequests },
      closeout: { shadow: { enabledRepositories, indexReadsPerPass } } })
  assert.doesNotThrow(() => config(25), '11 list requests and one read fit in the nested budget')
  assert.throws(() => config(24), /request budget/)
  assert.equal(config(5, 1, []), null, 'a disabled shadow still makes no extra reads')

  const h = await harness(t, { closeout: { shadow: { enabledRepositories: [REPO_A], indexReadsPerPass: 1 } } })
  h.config.limits = { maxRequests: 5 }
  await assert.rejects(h.pass(), /request budget/)
  assert.deepEqual(h.boardCalls, [], 'an impossible budget refuses before listing issues')
  assert.deepEqual(h.calls, [], 'an impossible budget refuses before capture reads')
})

test('config load keeps a valid closeout block, still refuses unknown keys and bad blocks', async (t) => {
  const h = await harness(t)
  const write = async (extra) => {
    const path = join(h.root, `config-${Math.random().toString(16).slice(2)}.json`)
    await writeFile(path, JSON.stringify({ ...h.config, ...extra }), { mode: 0o600 })
    return path
  }
  const block = { shadow: { enabledRepositories: [REPO_A], debounceMs: 5000 } }
  const loaded = await loadConsumerConfig(await write({ closeout: block }))
  assert.deepEqual(loaded.config.closeout, block)
  assert.equal((await loadConsumerConfig(await write({}))).config.closeout, undefined)
  for (const extra of [{ closeoutt: block }, { closeout: { shadow: { enabledRepositories: [REPO_B] } } },
    { closeout: { shadow: { enabledRepositories: [REPO_A], debounceMs: 1 } } }, { closeout: { claims: true } }]) {
    await assert.rejects(loadConsumerConfig(await write(extra)), /consumer configuration or credential file is invalid/)
  }
  await assert.rejects(runProductPass({ config: { ...h.config, closeout: { shadow: { enabledRepositories: ['X/y'] } } },
    captureToken: secret, boardToken: secret }, { fetchImpl: h.fetch, run: h.run }), /closeout/)
  assert.deepEqual(h.calls, [], 'a bad block refuses before any effect')
})

// Hook-level tests with a fake adapter and an injected index: the runner
// passes no index yet, so this is how decisions are proven end to end.
async function hookFixture(t, { enabled = [REPO_A], numbers = [7, 8], indexNumbers = [7, 8], logs = [] } = {}) {
  const root = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'closeout-shadow-hook-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const config = { captureOrigin: 'https://capture.test', boardOrigin: 'https://board.test', companyId,
    allowedRepositories: [REPO_A, REPO_B], mode: 'products-only-v1' }
  const namespace = receiptNamespace(config)
  const directory = join(root, 'state')
  await mkdir(directory, { mode: 0o700 })
  const nowMs = pushedMs + 7200000
  const done = (name) => ({ name, status: 'completed', conclusion: 'success', completedMs: pushedMs + 1000 })
  const snapshots = new Map([
    [7, { headSha: sha(7), mergeableState: 'dirty', checks: [] }],
    [8, { headSha: sha(8), mergeableState: 'clean', reviewDecision: 'APPROVED',
      checks: [done('ci-ok'), done('pr-lint'), done(SHADOW_POLICY.reviewCheck)] }],
    [9, { headSha: sha(9), mergeableState: 'clean', checks: [] }],
  ])
  const reads = []
  const github = {
    async getRulesetRequiredChecks(repository, branch) { reads.push(['rules', repository, branch]); return ['ci-ok', 'pr-lint'] },
    async getCloseoutSnapshot(repository, number, options) {
      reads.push(['snapshot', repository, number])
      const s = snapshots.get(number) ?? { headSha: sha(number), mergeableState: 'dirty', checks: [] }
      return { repository, number, baseRef: 'main', state: 'open', merged: false, draft: false, autoMerge: false,
        labels: [], reviewDecision: null, headPushedMs: pushedMs, nowMs: options.nowMs, ...s }
    },
  }
  const index = buildPrTaskIndex(indexNumbers.map((n) => ({ issue: { id: `issue-${n}`, identifier: `TOG-${n}`,
    status: 'in_progress' }, products: [{ type: 'pull_request', provider: 'github', externalId: `${REPO_A}#${n}` }] })))
  const claims = freshClaimState(claimNamespace(namespace))
  addClaim(claims, { repository: REPO_A, number: 8, headSha: sha(8), class: 'stalled' }, 5000)
  await saveClaimState({ directory, state: claims })
  const seed = freshPendingState(pendingNamespace(namespace))
  for (const number of numbers) recordObservation(seed, { repository: REPO_A, number }, 0)
  await savePendingState({ directory, state: seed })
  const settings = closeoutShadowSettings({ allowedRepositories: config.allowedRepositories,
    closeout: { shadow: { enabledRepositories: enabled } } })
  const hook = createShadowReviewHook({ settings, namespace, directory, github, index,
    log: (line) => logs.push(line), nowMs: () => nowMs })
  return { hook, reads, logs, directory, namespace, github }
}

test('the hook reads the ruleset for the required checks and logs repo@sha decisions only', async (t) => {
  const f = await hookFixture(t)
  const claimsPath = join(f.directory, CLAIM_FILE)
  const before = await readFile(claimsPath, 'utf8')
  const result = await f.hook.runReview({ repository: REPO_A, numbers: [7, 8] })
  assert.deepEqual(f.reads.filter(([k]) => k === 'rules'), [['rules', REPO_A, 'main']])
  assert.deepEqual(result.counters, counters({ total: 2, mapped: 2, exceptions: 2,
    byClass: { conflict: 1, stalled: 1 }, wouldClaim: 1, alreadyClaimed: 1 }))
  const lines = f.logs.map((line) => JSON.parse(line))
  assert.deepEqual(lines, [
    { event: 'closeout-shadow', repository: REPO_A, counters: result.counters, pending: result.pending },
    { event: 'closeout-shadow-decision', ref: `${REPO_A}#7@${sha(7)}`, decision: 'conflict', claim: 'wouldClaim', reason: null },
    { event: 'closeout-shadow-decision', ref: `${REPO_A}#8@${sha(8)}`, decision: 'stalled', claim: 'alreadyClaimed', reason: null },
  ])
  assert.equal(await readFile(claimsPath, 'utf8'), before, 'claims are read, never written')
})

test('the hook skips a repository that is not enrolled without any read', async (t) => {
  const f = await hookFixture(t)
  assert.equal(await f.hook.runReview({ repository: REPO_B, numbers: [9] }), null)
  assert.deepEqual(f.reads, [])
  assert.deepEqual(f.logs, [])
})

test('decision lines are capped per repository and the omission is counted', async (t) => {
  const numbers = Array.from({ length: 60 }, (_, i) => 100 + i)
  const f = await hookFixture(t, { numbers, indexNumbers: numbers })
  const result = await f.hook.runReview({ repository: REPO_A, numbers })
  assert.equal(result.counters.total, 60)
  assert.equal(result.counters.exceptions, 60)
  const lines = f.logs.map((line) => JSON.parse(line))
  assert.equal(lines.filter((l) => l.event === 'closeout-shadow-decision').length, 50)
  assert.equal(lines[0].omittedDecisions, 10)
})

test('a throwing log sink never fails an observational review', async (t) => {
  const f = await hookFixture(t)
  const settings = closeoutShadowSettings({ allowedRepositories: [REPO_A],
    closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  const hook = createShadowReviewHook({ settings, namespace: f.namespace, directory: f.directory,
    github: f.github, index: null, log: () => { throw new Error(secret) }, nowMs: () => pushedMs + 7200000 })
  const result = await hook.runReview({ repository: REPO_A, numbers: [7] })
  assert.equal(result.counters.total, 1)
})

test('the hook refuses half-wired dependencies at construction', () => {
  const settings = closeoutShadowSettings({ allowedRepositories: [REPO_A],
    closeout: { shadow: { enabledRepositories: [REPO_A] } } })
  const base = { settings, namespace: 'a'.repeat(64), directory: '/x', github: { async getCloseoutSnapshot() {},
    async getRulesetRequiredChecks() {} } }
  assert.doesNotThrow(() => createShadowReviewHook(base))
  assert.throws(() => createShadowReviewHook({ ...base, settings: null }), /settings/)
  assert.throws(() => createShadowReviewHook({ ...base, github: { async getCloseoutSnapshot() {} } }), /ruleset reader/)
  assert.throws(() => createShadowReviewHook({ ...base, github: null }), /ruleset reader/)
  assert.throws(() => createShadowReviewHook({ ...base, log: 'x' }), /callbacks/)
})

test('the consumer CLI hands the pass its output as the log sink', async () => {
  const output = []
  const sink = (v) => output.push(v)
  let seen
  assert.equal(await cliMain(['events', '/private/config.json'], { load: async () => ({}),
    pass: async (settings, options) => { seen = options; return { ok: true } }, output: sink }), 0)
  assert.equal(seen.log, sink)
  seen.log('probe-line')
  assert.deepEqual(output, [JSON.stringify({ ok: true }), 'probe-line'])
})
