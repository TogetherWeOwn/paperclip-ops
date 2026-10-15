// poll-prs offline suite. Injected fakes only: no network, no board
// credentials. One integration test drives the real decision core.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { decideUpstreamWatch } from '../src/decision-core/upstream-watcher.js'
import {
  POLICY, RETRY, scopeTargets, etagHeaders, classifyHttpStatus,
  backoffWithJitter, shouldRunTick, workerPolicy, runPollTick,
} from '../src/worker/poll-prs.js'

const T0 = 1_758_000_000_000
const HOUR = 3_600_000
const DAY = 24 * HOUR
const HEAD_A = 'a'.repeat(40)
const HEAD_B = 'b'.repeat(40)

test('POLICY pins the compliance spec: 4h response, 4h red-CI, 7d silence, 12h re-wake, 24h red', () => {
  assert.equal(POLICY.responseSlaMs, 4 * HOUR)
  assert.equal(POLICY.redCiSlaMs, 4 * HOUR)
  assert.equal(POLICY.silencePingMs, 7 * DAY)
  assert.equal(POLICY.rewakeMs, 12 * HOUR)
  assert.equal(POLICY.gatusRedMs, 24 * HOUR)
  assert.equal(POLICY.pollInterval, '*/2 * * * *')
})

test('workerPolicy overrides the core 24h defaults with 4h/4h/7d', () => {
  assert.deepEqual(workerPolicy(), {
    responseSlaMs: 4 * HOUR,
    redCiSlaMs: 4 * HOUR,
    silencePingMs: 7 * DAY,
  })
})

test('scopeTargets dedups and prefers org kind on overlap', () => {
  assert.deepEqual(scopeTargets({
    upstreamRepos: ['b/repo', 'a/repo', 'a/repo', 'shared/repo'],
    orgRepos: ['shared/repo', 'org/only'],
  }), {
    upstream: ['a/repo', 'b/repo'],
    org: ['org/only', 'shared/repo'],
  })
})

test('etagHeaders sends If-None-Match only when an ETag is stored', () => {
  assert.deepEqual(etagHeaders({}), {})
  assert.deepEqual(etagHeaders({ etag: '"abc"' }), { 'If-None-Match': '"abc"' })
})

test('classifyHttpStatus: 401/403 stops the source, 429/5xx retries, other unknowns stay unknown', () => {
  assert.equal(classifyHttpStatus(200), 'fresh')
  assert.equal(classifyHttpStatus(304), 'not-modified')
  assert.equal(classifyHttpStatus(401), 'stop-source')
  assert.equal(classifyHttpStatus(403), 'stop-source')
  assert.equal(classifyHttpStatus(429), 'retry')
  assert.equal(classifyHttpStatus(503), 'retry')
  assert.equal(classifyHttpStatus(418), 'unknown')
})

test('backoffWithJitter stays bounded and grows with attempt', () => {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const wait = backoffWithJitter(attempt, () => 0.5)
    assert.ok(wait > 0 && wait <= RETRY.maxMs, `attempt ${attempt}: ${wait}`)
  }
  assert.ok(backoffWithJitter(0, () => 0) < backoffWithJitter(4, () => 0))
})

test('disabled tick performs zero reads and zero wakes (orphaned-job trap)', async () => {
  let collected = 0
  const out = await runPollTick({
    pluginEnabled: false,
    scope: scopeTargets({}),
    collect: async () => { collected += 1; return [] },
  })
  assert.equal(out.outcome, 'stopped-disabled')
  assert.equal(collected, 0)
  assert.equal(out.wakes, 0)
})

test('304 reads resolve silent without touching the decision core', async () => {
  let decided = 0
  const out = await runPollTick({
    pluginEnabled: true,
    scope: scopeTargets({ orgRepos: ['org/only'] }),
    collect: async () => [{ key: 'org/only#7', status: 304, etag: '"v1"' }],
    decide: (...args) => { decided += 1; return decideUpstreamWatch(...args) },
  })
  assert.equal(decided, 0)
  assert.deepEqual(out.results, [{ key: 'org/only#7', outcome: 'silent', via: 'etag-304' }])
  assert.equal(out.wakes, 0)
})

test('non-fresh, non-304 reads resolve unknown and never silent', async () => {
  for (const status of [401, 429, 503, 418]) {
    const out = await runPollTick({
      pluginEnabled: true,
      scope: scopeTargets({}),
      collect: async () => [{ key: `k#${status}`, status }],
    })
    assert.equal(out.results[0].outcome, 'unknown', `status ${status}`)
    assert.equal(out.wakes, 0)
  }
})

test('digest delivers once; failed delivery withholds the ack advance', async () => {
  const fakeDigest = { action: 'digest', dedupeKey: 'fp-1', nextAck: { sig: 'n1' } }
  const input = { prev: null, next: {}, tracking: {}, nowMs: T0 }
  const delivered = await runPollTick({
    pluginEnabled: true,
    scope: scopeTargets({}),
    collect: async () => [{ key: 'k#1', status: 200, input }],
    decide: () => fakeDigest,
    deliver: async () => ({ delivered: true }),
  })
  assert.equal(delivered.wakes, 1)
  assert.deepEqual(delivered.results[0], { key: 'k#1', outcome: 'digest', delivered: true, ack: { sig: 'n1' } })

  const withheld = await runPollTick({
    pluginEnabled: true,
    scope: scopeTargets({}),
    collect: async () => [{ key: 'k#1', status: 200, input }],
    decide: () => fakeDigest,
    deliver: async () => ({ delivered: false }),
  })
  assert.equal(withheld.wakes, 0)
  assert.deepEqual(withheld.results[0], { key: 'k#1', outcome: 'digest', delivered: false })
})

test('integration: real core baselines first sight, digests a head push, stays silent after', async () => {
  const tracking = {
    repository: 'org/only', number: 7, issueId: 'issue-1',
    identifier: 'DEMO-1', cardOpen: true,
  }
  const snapshot = (overrides = {}) => ({
    repository: 'org/only', number: 7, state: 'open', merged: false,
    headSha: HEAD_A, ci: { rollup: 'green', sinceMs: null },
    mergeable: 'mergeable', reviewDecision: 'review_required',
    threads: [], comments: [],
    lastOurResponseAtMs: null, lastMaintainerAtMs: T0 - HOUR,
    fetchedAtMs: T0, readOk: true, ...overrides,
  })
  const acks = {}
  const deliveries = []
  const collectFor = (snap) => async () => [{
    key: 'org/only#7', status: 200,
    input: { prev: acks['org/only#7'] ?? null, next: snap, tracking, nowMs: T0 },
  }]

  const first = await runPollTick({
    pluginEnabled: true, scope: scopeTargets({ orgRepos: ['org/only'] }),
    acks, collect: collectFor(snapshot()),
    deliver: async (d) => { deliveries.push(d); return { delivered: true } },
  })
  assert.equal(first.results[0].outcome, 'baseline')
  assert.equal(first.wakes, 0)
  acks['org/only#7'] = first.results[0].ack

  const push = await runPollTick({
    pluginEnabled: true, scope: scopeTargets({ orgRepos: ['org/only'] }),
    acks, collect: collectFor(snapshot({ headSha: HEAD_B, fetchedAtMs: T0 + 1 })),
    deliver: async (d) => { deliveries.push(d); return { delivered: true } },
  })
  assert.equal(push.results[0].outcome, 'digest')
  assert.equal(push.wakes, 1)
  assert.equal(deliveries.length, 1)
})
