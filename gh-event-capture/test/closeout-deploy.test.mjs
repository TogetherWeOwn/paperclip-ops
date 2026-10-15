import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_QA_OWNER, qaSmokeKey, deployPolicy, resolveQaOwner,
  isStagingEnvironment, decideDeploySmoke, renderQaSmokeIntent,
  createDeployWatcher,
} from '../src/closeout-deploy.js'
import {
  freshQasmokeState, checkQasmokeState, qasmokeNamespace, qasmokeKey,
  findIntent, applyRecord, createMemoryQasmokeStore,
} from '../src/qasmoke-store.js'
import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  loadQasmokeState, saveQasmokeState, createFileQasmokeStore,
} from '../src/qasmoke-store.js'

const repo = 'TogetherWeOwn/kofra'
const other = 'TogetherWeOwn/routeware'
const outsider = 'EvilOrg/payload'
const sha = (c) => c.repeat(40)
const policy = deployPolicy()
const on = deployPolicy({ enabledRepositories: [repo] })
const ALLOWED = [repo, other]

function payload(over = {}) {
  return {
    repository: { full_name: repo },
    deployment_status: { state: 'success' },
    deployment: { sha: sha('a'), environment: 'staging' },
    ...over,
  }
}

// Capture double: implements EXACTLY the two deploy reads. There is no
// board, claim or disarm method to call, so the watcher cannot post.
function doubles(rowsByRepo, { failLists = [], failReads = [] } = {}) {
  const calls = []
  const bodies = new Map()
  for (const [r, rows] of rowsByRepo) {
    for (const row of rows) bodies.set(row.delivery_id, row)
  }
  const capture = {
    async listDeliveries({ repository, event }) {
      calls.push(['list', repository, event])
      if (failLists.includes(repository)) throw new Error('capture list failed')
      if (event !== 'deployment_status') throw new Error('capture event is outside bridge scope')
      if (![repo, other, outsider].includes(repository) && ![...rowsByRepo.keys()].includes(repository)) {
        throw new Error('capture repository is outside the configured scope')
      }
      return (rowsByRepo.get(repository) ?? []).map((r) => ({ ...r }))
    },
    async getDelivery(id) {
      calls.push(['get', id])
      if (failReads.includes(id)) throw new Error('capture request failed or response was invalid')
      const found = bodies.get(id)
      if (!found) throw new Error('capture request failed or response was invalid')
      return { ...found }
    },
  }
  return { capture, calls }
}

function row(repoName, id, body, extra = {}) {
  return { delivery_id: id, repository: repoName, event: 'deployment_status',
    received_ms: 1000, body: JSON.stringify(body), body_truncated: 0, ...extra }
}

test('qa-smoke key is repo plus sha', () => {
  assert.equal(qaSmokeKey(repo, sha('a')), `qa-smoke:${repo}:${sha('a')}`)
  assert.throws(() => qaSmokeKey('bad', sha('a')), /repository is invalid/)
  assert.throws(() => qaSmokeKey(repo, 'short'), /sha is invalid/)
})

test('policy defaults off and freezes', () => {
  assert.deepEqual([...policy.enabledRepositories], [])
  assert.equal(resolveQaOwner(policy, repo), DEFAULT_QA_OWNER)
  assert.equal(DEFAULT_QA_OWNER, 'qa')
  const custom = deployPolicy({ enabledRepositories: [repo], qaOwners: { [repo]: 'qa-team' } })
  assert.equal(resolveQaOwner(custom, repo), 'qa-team')
  assert.equal(resolveQaOwner(custom, other), 'qa')
  assert.throws(() => deployPolicy({ enabledRepositories: [repo, repo] }), /unique/)
})

test('staging match rule: staging variants match, production does not', () => {
  for (const name of ['staging', 'STAGING', ' Staging ', 'staging-us', 'staging_eu',
    'staging/us', 'app-staging', 'app_staging', 'app/staging',
    'app-staging-us', 'svc-staging-blue']) {
    assert.equal(isStagingEnvironment(name), true, name)
  }
  for (const name of ['production', 'preview', 'development', 'canary',
    'stage', 'stag', 'stagingroom', 'my-stagingroom', '', '   ', null, undefined, 42]) {
    assert.equal(isStagingEnvironment(name), false, String(name))
  }
})

test('success plus staging is eligible with the dedupe key', () => {
  const verdict = decideDeploySmoke(payload())
  assert.equal(verdict.eligible, true)
  assert.equal(verdict.key, `qa-smoke:${repo}:${sha('a')}`)
  assert.equal(verdict.reason, 'staging-success')
  // Environment falls back across the three documented paths.
  assert.equal(decideDeploySmoke(payload({ deployment: { sha: sha('a') },
    deployment_status: { state: 'success', environment: 'staging-us' } })).eligible, true)
  assert.equal(decideDeploySmoke({ repository: { full_name: repo },
    deployment_status: { state: 'success' }, deployment: { sha: sha('a') },
    environment: 'app-staging' }).eligible, true)
})

test('non-success state is ineligible, never an intent', () => {
  for (const state of ['pending', 'in_progress', 'failure', 'error', 'inactive']) {
    const verdict = decideDeploySmoke(payload({ deployment_status: { state } }))
    assert.equal(verdict.eligible, false)
    assert.equal(verdict.reason, 'non-success-state')
    assert.equal(verdict.key, null)
  }
})

test('non-staging environment is acknowledged ineligible, never stored', () => {
  const verdict = decideDeploySmoke(payload({ deployment: { sha: sha('a'), environment: 'production' } }))
  assert.equal(verdict.eligible, false)
  assert.equal(verdict.reason, 'non-staging-environment')
  assert.equal(verdict.key, null)
})

test('malformed payload throws instead of reading as no-deploy', () => {
  assert.throws(() => decideDeploySmoke(null), /payload is required/)
  assert.throws(() => decideDeploySmoke({}), /repository is invalid/)
  assert.throws(() => decideDeploySmoke(payload({ deployment_status: null })), /status is missing/)
  assert.throws(() => decideDeploySmoke(payload({ deployment_status: {} })), /state is missing/)
  assert.throws(() => decideDeploySmoke(payload({ deployment: null })), /deployment is missing/)
  assert.throws(() => decideDeploySmoke(payload({ deployment: { sha: 'short', environment: 'staging' } })),
    /sha is invalid/)
  assert.throws(() => decideDeploySmoke(payload({ deployment: { sha: sha('a') } })),
    /environment is missing/)
})

test('intent text is fixed words with safe fields only', () => {
  const body = renderQaSmokeIntent({ repository: repo, sha: sha('a'), environment: 'staging', qaOwner: 'qa' })
  assert.match(body, new RegExp(`^QA smoke qa-smoke:${repo}:${sha('a')}:`))
  assert.match(body, /staging.*qa/)
  assert.throws(() => renderQaSmokeIntent({ repository: repo, sha: sha('a'),
    environment: 'staging; rm -rf', qaOwner: 'qa' }), /environment is unsafe/)
})

test('eligible delivery with flag on records exactly one intent', async () => {
  const { capture, calls } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryQasmokeStore()
  const { records, counters } = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'recorded')
  assert.equal(records[0].key, `qa-smoke:${repo}:${sha('a')}`)
  assert.match(records[0].log, new RegExp(`^closeout-deploy ${repo}@${sha('a')}: recorded`))
  assert.equal(counters.recorded, 1)
  assert.equal(counters.eligible, 1)
  const found = await intents.findIntent(repo, sha('a'))
  assert.equal(found.environment, 'staging')
  assert.equal(found.qaOwner, 'qa')
  assert.deepEqual(calls[0], ['list', repo, 'deployment_status'])
})

// M1 duplicate-intent: a repeat deploy of the same SHA must add nothing.
test('repeat deploy of the same SHA adds no intent and keeps the first', async () => {
  const first = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryQasmokeStore()
  const watcher = createDeployWatcher({ capture: first.capture, intents, policy: on,
    allowedRepositories: ALLOWED })
  await watcher.runDeploy({ repositories: [repo], nowMs: 1000 })
  const second = doubles(new Map([[repo, [row(repo, 'd2', payload({ deployment: { sha: sha('a'), environment: 'staging-us' } }))]]]))
  const watcher2 = createDeployWatcher({ capture: second.capture, intents, policy: on,
    allowedRepositories: ALLOWED })
  const { records, counters } = await watcher2.runDeploy({ repositories: [repo], nowMs: 2000 })
  assert.equal(records[0].decision, 'already-recorded')
  assert.equal(counters.alreadyRecorded, 1)
  assert.equal(counters.recorded, 0)
  assert.equal(intents.state.intents.length, 1)
  assert.equal((await intents.findIntent(repo, sha('a'))).environment, 'staging')
})

// M2 flag-off shadow: eligible delivery logs would-record and writes nothing.
test('flag-off repo logs would-record and writes no state', async () => {
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryQasmokeStore()
  const { records, counters } = await createDeployWatcher({ capture, intents, policy,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'would-record')
  assert.equal(counters.wouldRecord, 1)
  assert.equal(counters.recorded, 0)
  assert.equal(await intents.findIntent(repo, sha('a')), null)
})

// M3 staging filter: production success is counted ineligible, never stored.
test('production success is ineligible and never stored even with flag on', async () => {
  const { capture } = doubles(new Map([[repo,
    [row(repo, 'd1', payload({ deployment: { sha: sha('b'), environment: 'production' } }))]]]))
  const intents = createMemoryQasmokeStore()
  const { records, counters } = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'ineligible')
  assert.equal(records[0].reason, 'non-staging-environment')
  assert.equal(counters.nonStaging, 1)
  assert.equal(await intents.findIntent(repo, sha('b')), null)
})

test('non-success state is counted and never stored', async () => {
  const { capture } = doubles(new Map([[repo,
    [row(repo, 'd1', payload({ deployment_status: { state: 'failure' } }))]]]))
  const intents = createMemoryQasmokeStore()
  const { records, counters } = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'ineligible')
  assert.equal(counters.nonSuccess, 1)
  assert.equal(await intents.findIntent(repo, sha('a')), null)
})

test('non-allowlisted repo is dropped before any capture read or store write', async () => {
  const { capture, calls } = doubles(new Map())
  const intents = createMemoryQasmokeStore()
  const dropped = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [outsider], nowMs: 1000 })
  assert.equal(dropped.records[0].decision, 'dropped')
  assert.equal(dropped.records[0].reason, 'outside-allowlist')
  assert.equal(dropped.counters.dropped, 1)
  assert.deepEqual(calls, [], 'no capture read for a dropped repo')
  assert.equal(await intents.findIntent(outsider, sha('a')), null)
  const { capture: c2, calls: calls2 } = doubles(new Map([[other, [row(other, 'd1',
    { repository: { full_name: other }, deployment_status: { state: 'success' },
      deployment: { sha: sha('c'), environment: 'staging' } })]]]))
  const intents2 = createMemoryQasmokeStore()
  const narrow = createDeployWatcher({ capture: c2, intents: intents2, policy: deployPolicy({ enabledRepositories: [other] }),
    allowedRepositories: [repo] })
  const out = await narrow.runDeploy({ repositories: [other], nowMs: 1000 })
  assert.equal(out.records[0].decision, 'dropped')
  assert.equal(out.records[0].reason, 'outside-allowlist')
  assert.equal(out.counters.dropped, 1)
  assert.deepEqual(calls2, [], 'no capture read for a dropped repo')
  assert.equal(await intents2.findIntent(other, sha('c')), null)
})

test('red-main suppression logs suppressed and stores nothing', async () => {
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryQasmokeStore()
  const { records, counters } = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED, isSuppressed: () => true })
    .runDeploy({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'suppressed')
  assert.equal(records[0].reason, 'red-main-open')
  assert.equal(counters.suppressed, 1)
  assert.equal(await intents.findIntent(repo, sha('a')), null)
})

test('suppression predicate failure refuses to guess', async () => {
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryQasmokeStore()
  await assert.rejects(() => createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED, isSuppressed: () => { throw new Error('store down') } })
    .runDeploy({ repositories: [repo], nowMs: 1000 }), /refusing to guess/)
})

test('unreadable repo is a counted error, never a thrown run', async () => {
  const { capture } = doubles(new Map(), { failLists: [repo] })
  const intents = createMemoryQasmokeStore()
  const { records, counters } = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'none')
  assert.equal(counters.errors, 1)
})

// R1 unruly environment: an eligible staging name outside SAFE_NAME is a
// counted per-delivery error, never a thrown run — and the next delivery
// still processes.
test('unrenderable staging environment is a counted error, later deliveries still record', async () => {
  const weird = payload({ deployment: { sha: sha('b'), environment: 'Preview – app-staging' } })
  const { capture } = doubles(new Map([[repo,
    [row(repo, 'd1', weird), row(repo, 'd2', payload())]]]))
  const intents = createMemoryQasmokeStore()
  const { records, counters } = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'none')
  assert.equal(records[0].reason, 'intent-render-failed')
  assert.equal(counters.renderFailed, 1)
  assert.equal(counters.errors, 1)
  assert.equal(records[1].decision, 'recorded')
  assert.equal(counters.recorded, 1)
  assert.equal(await intents.findIntent(repo, sha('b')), null)
  assert.notEqual(await intents.findIntent(repo, sha('a')), null)
})

// R2 over-budget history: rows past the per-repo cap are loud skips, and the
// run stays green for this repo and the rest.
test('deliveries past the per-repo cap are skipped loudly, the run stays green', async () => {
  const rows = []
  for (let i = 0; i < 1002; i++) {
    const hex = i.toString(16).padStart(40, '0')
    rows.push(row(repo, `d${i}`, payload({ deployment: { sha: hex, environment: 'staging' } })))
  }
  const { capture } = doubles(new Map([[repo, rows], [other,
    [row(other, 'e1', { repository: { full_name: other }, deployment_status: { state: 'success' },
      deployment: { sha: sha('c'), environment: 'staging' } })]]]))
  const intents = createMemoryQasmokeStore()
  const watching = createDeployWatcher({ capture, intents,
    policy: deployPolicy({ enabledRepositories: [repo, other] }),
    allowedRepositories: ALLOWED })
  const { records, counters } = await watching.runDeploy({ repositories: [repo, other], nowMs: 1000 })
  assert.equal(counters.overBudget, 2)
  assert.ok(records.some((r) => r.decision === 'skipped' && r.reason === 'over-budget:2-deliveries-unread'))
  assert.equal(counters.errors, 0)
  const otherRecord = records.find((r) => r.repository === other)
  assert.equal(otherRecord.decision, 'recorded')
})

// R4 scan window: an explicit sinceMs is forwarded to capture (which
// filters server-side), so a long history can never blow the page budget.
test('explicit sinceMs is forwarded and old rows are never listed', async () => {
  const seen = []
  const rows = [
    row(repo, 'd-old', payload({ deployment: { sha: sha('a'), environment: 'staging' } }), { received_ms: 1000 }),
    row(repo, 'd-new', payload({ deployment: { sha: sha('b'), environment: 'staging' } }), { received_ms: 9000 }),
  ]
  const bodies = new Map(rows.map((r) => [r.delivery_id, r]))
  const capture = {
    async listDeliveries(options) {
      seen.push(options)
      assert.equal(options.event, 'deployment_status')
      // Same contract as the real adapter: the since filter applies.
      return rows.filter((r) => options.sinceMs == null || r.received_ms >= options.sinceMs)
    },
    async getDelivery(id) {
      const found = bodies.get(id)
      if (!found) throw new Error('capture request failed or response was invalid')
      return { ...found }
    },
  }
  const intents = createMemoryQasmokeStore()
  const { records, counters, sinceMs: applied } = await createDeployWatcher({ capture, intents,
    policy: on, allowedRepositories: ALLOWED })
    .runDeploy({ repositories: [repo], sinceMs: 5000, nowMs: 9000 })
  assert.equal(applied, 5000)
  assert.deepEqual(seen, [{ repository: repo, event: 'deployment_status', sinceMs: 5000 }])
  assert.equal(counters.deliveries, 1)
  assert.equal(records[0].decision, 'recorded')
  assert.equal((await intents.findIntent(repo, sha('b'))).sha, sha('b'))
  assert.equal(await intents.findIntent(repo, sha('a')), null)
})

// R5 default window: omitting sinceMs scans now minus 7 days.
test('omitted sinceMs defaults to the 7-day window', async () => {
  const seen = []
  const capture = {
    async listDeliveries(options) { seen.push(options); return [] },
    async getDelivery() { throw new Error('must not be called with no rows') },
  }
  const intents = createMemoryQasmokeStore()
  const nowMs = 7 * 24 * 3600 * 1000 + 60000
  const { sinceMs: applied } = await createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeploy({ repositories: [repo], nowMs })
  assert.equal(applied, 60000)
  assert.deepEqual(seen, [{ repository: repo, event: 'deployment_status', sinceMs: 60000 }])
})

// R6 window validation: future or negative sinceMs fails loudly.
test('invalid scan window fails loudly', async () => {
  const capture = {
    async listDeliveries() { throw new Error('must not be called') },
    async getDelivery() { throw new Error('must not be called') },
  }
  const intents = createMemoryQasmokeStore()
  const watcher = createDeployWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED })
  await assert.rejects(() => watcher.runDeploy({ repositories: [repo], sinceMs: 9001, nowMs: 9000 }),
    /scan window is invalid/)
  await assert.rejects(() => watcher.runDeploy({ repositories: [repo], sinceMs: -1, nowMs: 9000 }),
    /scan window is invalid/)
})

// R3 eviction rescan: a pruned SHA is re-recorded on the next run — counted,
// never thrown, never duplicated in state.
test('evicted intent re-records on rescan without duplication or throw', async () => {
  const intents = createMemoryQasmokeStore(null, 2)
  const watching = (rows) => createDeployWatcher({
    capture: doubles(new Map([[repo, rows]])).capture, intents, policy: on,
    allowedRepositories: ALLOWED,
  })
  const mkrow = (id, c) => row(repo, id,
    payload({ deployment: { sha: sha(c), environment: 'staging' } }))
  await watching([mkrow('d1', 'a')]).runDeploy({ repositories: [repo], nowMs: 1000 })
  await watching([mkrow('d2', 'b')]).runDeploy({ repositories: [repo], nowMs: 2000 })
  assert.equal(intents.state.intents.length, 2)
  await watching([mkrow('d3', 'c')]).runDeploy({ repositories: [repo], nowMs: 3000 })
  assert.equal(intents.state.intents.length, 2)
  assert.equal(await intents.findIntent(repo, sha('a')), null, 'oldest pruned first')
  const { records, counters } = await watching([mkrow('d1', 'a')])
    .runDeploy({ repositories: [repo], nowMs: 4000 })
  assert.equal(records[0].decision, 'recorded')
  assert.equal(counters.recorded, 1)
  assert.equal(counters.errors, 0)
  const keys = intents.state.intents.map((e) => e.key)
  assert.equal(new Set(keys).size, keys.length, 'no duplicate keys in state')
})

test('store rejects a duplicate record and a foreign key', async () => {
  const intents = createMemoryQasmokeStore()
  await intents.recordIntent({ key: qasmokeKey(repo, sha('a')), repository: repo,
    sha: sha('a'), environment: 'staging', firstSeenMs: 1000, qaOwner: 'qa', body: 'x' })
  await assert.rejects(() => intents.recordIntent({ key: qasmokeKey(repo, sha('a')),
    repository: repo, sha: sha('a'), environment: 'staging', firstSeenMs: 2000,
    qaOwner: 'qa', body: 'y' }), /already recorded/)
  const state = freshQasmokeState(createHash('sha256').update('qasmoke-test-seed').digest('hex'))
  checkQasmokeState(state)
  assert.equal(findIntent(state, repo, sha('a')), null)
  applyRecord(state, { repository: repo, sha: sha('a'), environment: 'staging',
    firstSeenMs: 1000, qaOwner: 'qa', body: 'x' })
  assert.throws(() => applyRecord(state, { repository: repo, sha: sha('a'),
    environment: 'staging', firstSeenMs: 2000, qaOwner: 'qa', body: 'y' }), /already recorded/)
})

test('store namespace derives distinctly and mismatches refuse', async () => {
  const receipt = createHash('sha256').update('receipt-scope').digest('hex')
  const ns = qasmokeNamespace(receipt)
  assert.match(ns, /^[a-f0-9]{64}$/)
  assert.notEqual(ns, receipt)
  const dir = await mkdtemp(join(tmpdir(), 'qasmoke-'))
  try {
    const live = freshQasmokeState(ns)
    applyRecord(live, { repository: repo, sha: sha('a'), environment: 'staging',
      firstSeenMs: 1000, qaOwner: 'qa', body: 'x' })
    await saveQasmokeState({ directory: dir, state: live })
    const same = await loadQasmokeState({ directory: dir, namespace: ns })
    assert.equal(same.intents.length, 1)
    const otherNs = createHash('sha256').update('other-scope').digest('hex')
    await assert.rejects(() => loadQasmokeState({ directory: dir, namespace: otherNs }),
      /namespace does not match/)
    const files = createFileQasmokeStore({ directory: dir, namespace: ns })
    assert.equal((await files.findIntent(repo, sha('a'))).sha, sha('a'))
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('file store round-trips one intent', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'qasmoke-file-'))
  try {
    const ns = createHash('sha256').update('file-scope').digest('hex')
    const files = createFileQasmokeStore({ directory: dir, namespace: ns })
    assert.equal(await files.findIntent(repo, sha('a')), null)
    await files.recordIntent({ key: qasmokeKey(repo, sha('a')), repository: repo,
      sha: sha('a'), environment: 'staging', firstSeenMs: 1000, qaOwner: 'qa', body: 'x' })
    assert.equal((await files.findIntent(repo, sha('a'))).environment, 'staging')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})
