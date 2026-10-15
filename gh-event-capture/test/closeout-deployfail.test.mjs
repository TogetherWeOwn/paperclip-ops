import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_DEPLOY_OWNER, deployFailKey, deployFailPolicy, resolveDeployOwner,
  isFailureState, readDeployLogUrl, hasLogPointer, decideDeployFailure, renderDeployFailIntent,
  createDeployFailWatcher,
} from '../src/closeout-deploy.js'
import {
  freshDeployfailState, checkDeployfailState, deployfailNamespace, deployFailKey as storeKey,
  findIntent, applyRecord, createMemoryDeployfailStore,
} from '../src/deployfail-store.js'
import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  loadDeployfailState, saveDeployfailState, createFileDeployfailStore,
} from '../src/deployfail-store.js'

const repo = 'TogetherWeOwn/kofra'
const other = 'TogetherWeOwn/routeware'
const outsider = 'EvilOrg/payload'
const sha = (c) => c.repeat(40)
const LOG = 'https://github.com/TogetherWeOwn/kofra/actions/runs/123'
const policy = deployFailPolicy()
const on = deployFailPolicy({ enabledRepositories: [repo] })
const ALLOWED = [repo, other]

function payload(over = {}) {
  return {
    repository: { full_name: repo },
    deployment_status: { state: 'failure', log_url: LOG },
    deployment: { sha: sha('a'), environment: 'production' },
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

test('deploy-fail key is repo plus sha', () => {
  assert.equal(deployFailKey(repo, sha('a')), `deploy-fail:${repo}:${sha('a')}`)
  assert.equal(storeKey(repo, sha('a')), `deploy-fail:${repo}:${sha('a')}`)
  assert.throws(() => deployFailKey('bad', sha('a')), /repository is invalid/)
  assert.throws(() => deployFailKey(repo, 'short'), /sha is invalid/)
})

test('policy defaults off and freezes', () => {
  assert.deepEqual([...policy.enabledRepositories], [])
  assert.equal(resolveDeployOwner(policy, repo), DEFAULT_DEPLOY_OWNER)
  assert.equal(DEFAULT_DEPLOY_OWNER, 'deploy')
  const custom = deployFailPolicy({ enabledRepositories: [repo], deployOwners: { [repo]: 'deploy-team' } })
  assert.equal(resolveDeployOwner(custom, repo), 'deploy-team')
  assert.equal(resolveDeployOwner(custom, other), 'deploy')
  assert.throws(() => deployFailPolicy({ enabledRepositories: [repo, repo] }), /unique/)
})

test('failure-state match rule is exact: failure/error only', () => {
  assert.equal(isFailureState('failure'), true)
  assert.equal(isFailureState('error'), true)
  for (const s of ['success', 'pending', 'in_progress', 'inactive', 'queued',
    'Failure', 'ERROR', 'FAILED', '', 'failure ', ' failure', null, undefined, 42]) {
    assert.equal(isFailureState(s), false, String(s))
  }
})

test('failure plus any environment is eligible with the dedupe key', () => {
  const verdict = decideDeployFailure(payload())
  assert.equal(verdict.eligible, true)
  assert.equal(verdict.key, `deploy-fail:${repo}:${sha('a')}`)
  assert.equal(verdict.reason, 'deploy-failure')
  assert.equal(verdict.state, 'failure')
  assert.equal(verdict.logUrl, LOG)
  const err = decideDeployFailure(payload({ deployment_status: { state: 'error', log_url: LOG } }))
  assert.equal(err.eligible, true)
  assert.equal(err.state, 'error')
  // Environment falls back across the three documented paths.
  assert.equal(decideDeployFailure(payload({ deployment: { sha: sha('a') },
    deployment_status: { state: 'failure', environment: 'staging' } })).eligible, true)
  assert.equal(decideDeployFailure({ repository: { full_name: repo },
    deployment_status: { state: 'error' }, deployment: { sha: sha('a') },
    environment: 'staging' }).eligible, true)
})

test('success stays on the S9a path: acknowledged, never a deploy-fail intent', () => {
  const verdict = decideDeployFailure(payload({ deployment_status: { state: 'success', log_url: LOG } }))
  assert.equal(verdict.eligible, false)
  assert.equal(verdict.reason, 'success-on-smoke-path')
  assert.equal(verdict.key, null)
})

test('non-failure states are ineligible, never an intent', () => {
  for (const state of ['pending', 'in_progress', 'inactive', 'queued']) {
    const verdict = decideDeployFailure(payload({ deployment_status: { state } }))
    assert.equal(verdict.eligible, false)
    assert.equal(verdict.reason, 'non-failure-state')
    assert.equal(verdict.key, null)
  }
})

test('environment never filters failures: staging and production both count', () => {
  for (const environment of ['staging', 'staging-us', 'app-staging', 'production', 'prod-eu']) {
    const verdict = decideDeployFailure(
      payload({ deployment: { sha: sha('a'), environment } }))
    assert.equal(verdict.eligible, true, environment)
    assert.equal(verdict.environment, environment)
  }
})

test('log pointer follows the documented chain, null when absent', () => {
  assert.equal(readDeployLogUrl(payload()), LOG)
  assert.equal(hasLogPointer(payload()), true)
  assert.equal(readDeployLogUrl(payload({
    deployment_status: { state: 'failure', target_url: 'https://example.com/a' },
  })), 'https://example.com/a')
  assert.equal(readDeployLogUrl(payload({
    deployment_status: { state: 'failure' },
    deployment: { sha: sha('a'), environment: 'production', log_url: 'https://example.com/b' },
  })), 'https://example.com/b')
  assert.equal(readDeployLogUrl(payload({
    deployment_status: { state: 'failure' },
    deployment: { sha: sha('a'), environment: 'production' },
  })), null)
  assert.equal(hasLogPointer(payload({
    deployment_status: { state: 'failure' },
    deployment: { sha: sha('a'), environment: 'production' },
  })), false)
  // First present pointer wins.
  assert.equal(readDeployLogUrl(payload({
    deployment_status: { state: 'failure', log_url: 'https://example.com/first', target_url: 'https://example.com/second' },
  })), 'https://example.com/first')
})

test('unsafe pointer candidates are skipped in favor of a safe fallback', () => {
  // Credentialed first candidate is skipped; the safe fallback still records.
  const p = payload({
    deployment_status: { state: 'failure', log_url: 'https://user:tok@example.com/x', target_url: 'https://example.com/safe' },
  })
  assert.equal(readDeployLogUrl(p), 'https://example.com/safe')
  assert.equal(hasLogPointer(p), true)
  const verdict = decideDeployFailure(p)
  assert.equal(verdict.eligible, true)
  assert.equal(verdict.logUrl, 'https://example.com/safe')
  assert.equal(verdict.logUrlUnsafe, false)
  // http-only and javascript: pointers are not safe reads either.
  for (const bad of ['http://example.com/x', 'javascript:alert(1)', 'https://user:pass@example.com/x']) {
    assert.equal(readDeployLogUrl(payload({ deployment_status: { state: 'error', log_url: bad } })), null, bad)
  }
})

test('all-unsafe pointer chain reads null and flags the drop, never the secret', () => {
  const secret = 'https://user:tok@example.com/secret-log'
  const verdict = decideDeployFailure(payload({ deployment_status: { state: 'failure', log_url: secret } }))
  assert.equal(verdict.eligible, true)
  assert.equal(verdict.logUrl, null)
  assert.equal(verdict.logUrlUnsafe, true)
  // The verdict carries no trace of the credentialed URL.
  assert.ok(!JSON.stringify(verdict).includes('tok'))
})

test('malformed payload throws instead of reading as no-deploy', () => {
  assert.throws(() => decideDeployFailure(null), /payload is required/)
  assert.throws(() => decideDeployFailure({}), /repository is invalid/)
  assert.throws(() => decideDeployFailure(payload({ deployment_status: null })), /status is missing/)
  assert.throws(() => decideDeployFailure(payload({ deployment_status: {} })), /state is missing/)
  assert.throws(() => decideDeployFailure(payload({ deployment: null })), /deployment is missing/)
  assert.throws(() => decideDeployFailure(payload({ deployment: { sha: 'short', environment: 'production' } })),
    /sha is invalid/)
  assert.throws(() => decideDeployFailure(payload({ deployment: { sha: sha('a') } })),
    /environment is missing/)
})

test('intent text is fixed words with safe fields only', () => {
  const body = renderDeployFailIntent({ repository: repo, sha: sha('a'),
    environment: 'production', logUrl: LOG, deployOwner: 'deploy', state: 'failure' })
  assert.match(body, new RegExp(`^Deploy failure deploy-fail:${repo}:${sha('a')}:`))
  assert.match(body, /production.*failure/)
  assert.match(body, new RegExp(LOG.replace(/[./]/g, (c) => `\\${c}`)))
  const noPointer = renderDeployFailIntent({ repository: repo, sha: sha('a'),
    environment: 'staging', logUrl: null, deployOwner: 'deploy', state: 'error' })
  assert.match(noPointer, /No log pointer/)
  assert.throws(() => renderDeployFailIntent({ repository: repo, sha: sha('a'),
    environment: 'staging; rm -rf', logUrl: null, deployOwner: 'deploy', state: 'failure' }), /environment is unsafe/)
  assert.throws(() => renderDeployFailIntent({ repository: repo, sha: sha('a'),
    environment: 'production', logUrl: 'javascript:alert(1)', deployOwner: 'deploy', state: 'failure' }),
  /log URL is unsafe/)
  assert.throws(() => renderDeployFailIntent({ repository: repo, sha: sha('a'),
    environment: 'production', logUrl: 'https://user:pass@example.com/x', deployOwner: 'deploy', state: 'failure' }),
  /log URL is unsafe/)
  assert.throws(() => renderDeployFailIntent({ repository: repo, sha: sha('a'),
    environment: 'production', logUrl: null, deployOwner: 'deploy', state: 'success' }), /state is invalid/)
})

test('eligible failure with flag on records exactly one intent', async () => {
  const { capture, calls } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'recorded')
  assert.equal(records[0].key, `deploy-fail:${repo}:${sha('a')}`)
  assert.match(records[0].log, new RegExp(`^closeout-deploy ${repo}@${sha('a')}: recorded`))
  assert.equal(counters.recorded, 1)
  assert.equal(counters.eligible, 1)
  const found = await intents.findIntent(repo, sha('a'))
  assert.equal(found.environment, 'production')
  assert.equal(found.logUrl, LOG)
  assert.equal(found.deployOwner, 'deploy')
  assert.deepEqual(calls[0], ['list', repo, 'deployment_status'])
})

// M1 duplicate-intent: a repeat delivery for the same SHA must add nothing.
test('repeat delivery for the same SHA adds no intent and keeps the first', async () => {
  const first = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryDeployfailStore()
  const watcher = createDeployFailWatcher({ capture: first.capture, intents, policy: on,
    allowedRepositories: ALLOWED })
  await watcher.runDeployFail({ repositories: [repo], nowMs: 1000 })
  const second = doubles(new Map([[repo, [row(repo, 'd2',
    payload({ deployment: { sha: sha('a'), environment: 'staging' } }))]]]))
  const watcher2 = createDeployFailWatcher({ capture: second.capture, intents, policy: on,
    allowedRepositories: ALLOWED })
  const { records, counters } = await watcher2.runDeployFail({ repositories: [repo], nowMs: 2000 })
  assert.equal(records[0].decision, 'already-recorded')
  assert.equal(counters.alreadyRecorded, 1)
  assert.equal(counters.recorded, 0)
  assert.equal(intents.state.intents.length, 1)
  assert.equal((await intents.findIntent(repo, sha('a'))).environment, 'production')
})

// M2 flag-off shadow: eligible delivery logs would-record and writes nothing.
test('flag-off repo logs would-record and writes no state', async () => {
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'would-record')
  assert.equal(counters.wouldRecord, 1)
  assert.equal(counters.recorded, 0)
  assert.equal(await intents.findIntent(repo, sha('a')), null)
})

// M3 failure-state filter: success is counted non-failure, never stored.
test('success delivery is ineligible and never stored even with flag on', async () => {
  const { capture } = doubles(new Map([[repo,
    [row(repo, 'd1', payload({ deployment_status: { state: 'success', log_url: LOG } }))]]]))
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'ineligible')
  assert.equal(records[0].reason, 'success-on-smoke-path')
  assert.equal(counters.nonFailure, 1)
  assert.equal(await intents.findIntent(repo, sha('a')), null)
})

test('non-allowlisted repo is dropped before any capture read or store write', async () => {
  const { capture, calls } = doubles(new Map())
  const intents = createMemoryDeployfailStore()
  const dropped = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [outsider], nowMs: 1000 })
  assert.equal(dropped.records[0].decision, 'dropped')
  assert.equal(dropped.records[0].reason, 'outside-allowlist')
  assert.equal(dropped.counters.dropped, 1)
  assert.deepEqual(calls, [], 'no capture read for a dropped repo')
  assert.equal(await intents.findIntent(outsider, sha('a')), null)
  const { capture: c2, calls: calls2 } = doubles(new Map([[other, [row(other, 'd1',
    { repository: { full_name: other }, deployment_status: { state: 'failure' },
      deployment: { sha: sha('c'), environment: 'production' } })]]]))
  const intents2 = createMemoryDeployfailStore()
  const narrow = createDeployFailWatcher({ capture: c2, intents: intents2, policy: deployFailPolicy({ enabledRepositories: [other] }),
    allowedRepositories: [repo] })
  const out = await narrow.runDeployFail({ repositories: [other], nowMs: 1000 })
  assert.equal(out.records[0].decision, 'dropped')
  assert.equal(out.records[0].reason, 'outside-allowlist')
  assert.equal(out.counters.dropped, 1)
  assert.deepEqual(calls2, [], 'no capture read for a dropped repo')
  assert.equal(await intents2.findIntent(other, sha('c')), null)
})

test('red-main suppression logs suppressed and stores nothing', async () => {
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED, isSuppressed: () => true })
    .runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'suppressed')
  assert.equal(records[0].reason, 'red-main-open')
  assert.equal(counters.suppressed, 1)
  assert.equal(await intents.findIntent(repo, sha('a')), null)
})

test('suppression predicate failure refuses to guess', async () => {
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', payload())]]]))
  const intents = createMemoryDeployfailStore()
  await assert.rejects(() => createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED, isSuppressed: () => { throw new Error('store down') } })
    .runDeployFail({ repositories: [repo], nowMs: 1000 }), /refusing to guess/)
})

test('unreadable repo is a counted error, never a thrown run', async () => {
  const { capture } = doubles(new Map(), { failLists: [repo] })
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'none')
  assert.equal(counters.errors, 1)
})

// R1 unruly fields: an eligible delivery with an unsafe environment or log
// pointer is a counted per-delivery error, never a thrown run — and the next
// delivery still processes.
test('unrenderable delivery is a counted error, later deliveries still record', async () => {
  const weird = payload({ deployment: { sha: sha('b'), environment: 'Preview – failed' } })
  const { capture } = doubles(new Map([[repo,
    [row(repo, 'd1', weird), row(repo, 'd2', payload())]]]))
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'none')
  assert.equal(records[0].reason, 'intent-render-failed')
  assert.equal(counters.renderFailed, 1)
  assert.equal(counters.errors, 1)
  assert.equal(records[1].decision, 'recorded')
  assert.equal(counters.recorded, 1)
  assert.equal(await intents.findIntent(repo, sha('b')), null)
  assert.notEqual(await intents.findIntent(repo, sha('a')), null)
})

test('all-unsafe log pointer records with null plus a counter, never a dropped intent', async () => {
  const bad = payload({ deployment_status: { state: 'error', log_url: 'javascript:alert(1)' } })
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', bad)]]]))
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'recorded')
  assert.equal(records[0].logUrl, null)
  assert.equal(counters.unsafePointer, 1)
  assert.equal(counters.renderFailed, 0)
  assert.equal(counters.errors, 0)
  const found = await intents.findIntent(repo, sha('a'))
  assert.equal(found.logUrl, null)
  assert.match(found.body, /No log pointer/)
})

test('unsafe first candidate with a safe fallback records the fallback, no counter', async () => {
  const mixed = payload({
    deployment_status: { state: 'failure', log_url: 'http://example.com/plain', target_url: LOG },
  })
  const { capture } = doubles(new Map([[repo, [row(repo, 'd1', mixed)]]]))
  const intents = createMemoryDeployfailStore()
  const { records, counters } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs: 1000 })
  assert.equal(records[0].decision, 'recorded')
  assert.equal(records[0].logUrl, LOG)
  assert.equal(counters.unsafePointer, 0)
  assert.equal((await intents.findIntent(repo, sha('a'))).logUrl, LOG)
})

// R2 over-budget history: rows past the per-repo cap are loud skips, and the
// run stays green for this repo and the rest.
test('deliveries past the per-repo cap are skipped loudly, the run stays green', async () => {
  const rows = []
  for (let i = 0; i < 1002; i++) {
    const hex = i.toString(16).padStart(40, '0')
    rows.push(row(repo, `d${i}`, payload({ deployment: { sha: hex, environment: 'production' } })))
  }
  const { capture } = doubles(new Map([[repo, rows], [other,
    [row(other, 'e1', { repository: { full_name: other }, deployment_status: { state: 'error' },
      deployment: { sha: sha('c'), environment: 'staging' } })]]]))
  const intents = createMemoryDeployfailStore()
  const watching = createDeployFailWatcher({ capture, intents,
    policy: deployFailPolicy({ enabledRepositories: [repo, other] }),
    allowedRepositories: ALLOWED })
  const { records, counters } = await watching.runDeployFail({ repositories: [repo, other], nowMs: 1000 })
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
    row(repo, 'd-old', payload({ deployment: { sha: sha('a'), environment: 'production' } }), { received_ms: 1000 }),
    row(repo, 'd-new', payload({ deployment: { sha: sha('b'), environment: 'production' } }), { received_ms: 9000 }),
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
  const intents = createMemoryDeployfailStore()
  const { records, counters, sinceMs: applied } = await createDeployFailWatcher({ capture, intents,
    policy: on, allowedRepositories: ALLOWED })
    .runDeployFail({ repositories: [repo], sinceMs: 5000, nowMs: 9000 })
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
  const intents = createMemoryDeployfailStore()
  const nowMs = 7 * 24 * 3600 * 1000 + 60000
  const { sinceMs: applied } = await createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED }).runDeployFail({ repositories: [repo], nowMs })
  assert.equal(applied, 60000)
  assert.deepEqual(seen, [{ repository: repo, event: 'deployment_status', sinceMs: 60000 }])
})

// R6 window validation: future or negative sinceMs fails loudly.
test('invalid scan window fails loudly', async () => {
  const capture = {
    async listDeliveries() { throw new Error('must not be called') },
    async getDelivery() { throw new Error('must not be called') },
  }
  const intents = createMemoryDeployfailStore()
  const watcher = createDeployFailWatcher({ capture, intents, policy: on,
    allowedRepositories: ALLOWED })
  await assert.rejects(() => watcher.runDeployFail({ repositories: [repo], sinceMs: 9001, nowMs: 9000 }),
    /scan window is invalid/)
  await assert.rejects(() => watcher.runDeployFail({ repositories: [repo], sinceMs: -1, nowMs: 9000 }),
    /scan window is invalid/)
})

// R3 eviction rescan: a pruned SHA is re-recorded on the next run — counted,
// never thrown, never duplicated in state.
test('evicted intent re-records on rescan without duplication or throw', async () => {
  const intents = createMemoryDeployfailStore(null, 2)
  const watching = (rows) => createDeployFailWatcher({
    capture: doubles(new Map([[repo, rows]])).capture, intents, policy: on,
    allowedRepositories: ALLOWED,
  })
  const mkrow = (id, c) => row(repo, id,
    payload({ deployment: { sha: sha(c), environment: 'production' } }))
  await watching([mkrow('d1', 'a')]).runDeployFail({ repositories: [repo], nowMs: 1000 })
  await watching([mkrow('d2', 'b')]).runDeployFail({ repositories: [repo], nowMs: 2000 })
  assert.equal(intents.state.intents.length, 2)
  await watching([mkrow('d3', 'c')]).runDeployFail({ repositories: [repo], nowMs: 3000 })
  assert.equal(intents.state.intents.length, 2)
  assert.equal(await intents.findIntent(repo, sha('a')), null, 'oldest pruned first')
  const { records, counters } = await watching([mkrow('d1', 'a')])
    .runDeployFail({ repositories: [repo], nowMs: 4000 })
  assert.equal(records[0].decision, 'recorded')
  assert.equal(counters.recorded, 1)
  assert.equal(counters.errors, 0)
  const keys = intents.state.intents.map((e) => e.key)
  assert.equal(new Set(keys).size, keys.length, 'no duplicate keys in state')
})

test('store rejects a duplicate record and a foreign key', async () => {
  const intents = createMemoryDeployfailStore()
  await intents.recordIntent({ key: storeKey(repo, sha('a')), repository: repo,
    sha: sha('a'), environment: 'production', logUrl: LOG, firstSeenMs: 1000, deployOwner: 'deploy', body: 'x' })
  await assert.rejects(() => intents.recordIntent({ key: storeKey(repo, sha('a')),
    repository: repo, sha: sha('a'), environment: 'production', logUrl: LOG, firstSeenMs: 2000,
    deployOwner: 'deploy', body: 'y' }), /already recorded/)
  const state = freshDeployfailState(createHash('sha256').update('deployfail-test-seed').digest('hex'))
  checkDeployfailState(state)
  assert.equal(findIntent(state, repo, sha('a')), null)
  applyRecord(state, { repository: repo, sha: sha('a'), environment: 'production',
    logUrl: LOG, firstSeenMs: 1000, deployOwner: 'deploy', body: 'x' })
  assert.throws(() => applyRecord(state, { repository: repo, sha: sha('a'),
    environment: 'production', logUrl: LOG, firstSeenMs: 2000, deployOwner: 'deploy', body: 'y' }),
  /already recorded/)
  assert.throws(() => applyRecord(state, { repository: repo, sha: sha('b'),
    environment: 'production', logUrl: 'javascript:alert(1)', firstSeenMs: 2000,
    deployOwner: 'deploy', body: 'y' }), /log URL is invalid/)
})

test('store namespace derives distinctly and mismatches refuse', async () => {
  const receipt = createHash('sha256').update('receipt-scope').digest('hex')
  const ns = deployfailNamespace(receipt)
  assert.match(ns, /^[a-f0-9]{64}$/)
  assert.notEqual(ns, receipt)
  const dir = await mkdtemp(join(tmpdir(), 'deployfail-'))
  try {
    const live = freshDeployfailState(ns)
    applyRecord(live, { repository: repo, sha: sha('a'), environment: 'production',
      logUrl: LOG, firstSeenMs: 1000, deployOwner: 'deploy', body: 'x' })
    await saveDeployfailState({ directory: dir, state: live })
    const same = await loadDeployfailState({ directory: dir, namespace: ns })
    assert.equal(same.intents.length, 1)
    const otherNs = createHash('sha256').update('other-scope').digest('hex')
    await assert.rejects(() => loadDeployfailState({ directory: dir, namespace: otherNs }),
      /namespace does not match/)
    const files = createFileDeployfailStore({ directory: dir, namespace: ns })
    assert.equal((await files.findIntent(repo, sha('a'))).sha, sha('a'))
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('file store round-trips one intent', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'deployfail-file-'))
  try {
    const ns = createHash('sha256').update('file-scope').digest('hex')
    const files = createFileDeployfailStore({ directory: dir, namespace: ns })
    assert.equal(await files.findIntent(repo, sha('a')), null)
    await files.recordIntent({ key: storeKey(repo, sha('a')), repository: repo,
      sha: sha('a'), environment: 'staging', logUrl: null, firstSeenMs: 1000, deployOwner: 'deploy', body: 'x' })
    assert.equal((await files.findIntent(repo, sha('a'))).environment, 'staging')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})
