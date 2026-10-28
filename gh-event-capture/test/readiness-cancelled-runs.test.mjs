// Retained cancellations wait; only a later same-producer pass supersedes one.
// Exercise the real snapshot reader and decision function, with offline GitHub I/O.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createGithubAdapter } from '../src/github-adapter.js'
import { decideReadinessActions, readinessPolicy } from '../src/readiness-reconcile.js'
import { shouldMarkReady, shouldConvertToDraft, shouldEnableAutoMerge } from '../src/readiness.js'

const repository = 'TogetherWeOwn/paperclip-ops'
const HEAD = 'c'.repeat(40)
const NOW = Date.parse('2026-10-03T08:00:00.000Z')
const REQUIRED = ['ci-ok', 'secret-scan', 'Paperclip Review']
const policy = readinessPolicy({ enabledRepositories: [repository] })
const at = (seconds) => new Date(NOW - 600_000 + seconds * 1000).toISOString()
const run = (name, conclusion, start = 10, end = 20, app = 'github-actions') => ({
  __typename: 'CheckRun', name, status: 'COMPLETED', conclusion,
  startedAt: at(start), completedAt: at(end), checkSuite: { app: { slug: app } },
})
const cancelled = (name = 'ci-ok') => run(name, 'CANCELLED')

async function snapshotFor(entries, name = 'ci-ok') {
  const nodes = [...['ci-ok', 'secret-scan', 'suites', 'Paperclip Review']
    .filter((other) => other !== name).map((other) => run(other, 'SUCCESS')), ...entries]
  const pr = {
    number: 7, url: `https://github.com/${repository}/pull/7`,
    title: 'Cancelled readiness fixture', body: '## What Changed\n\n- a fix\n',
    state: 'OPEN', isDraft: false, merged: false,
    headRefName: 'fixture-branch', headRefOid: HEAD, reviewDecision: null,
    mergeStateStatus: 'CLEAN', baseRefName: 'main', autoMergeRequest: null,
    author: { __typename: 'User', login: 'fixture-author' },
    baseRepository: { nameWithOwner: repository },
    labels: { nodes: [], pageInfo: { hasNextPage: false } },
    commits: { nodes: [{ commit: { oid: HEAD, pushedDate: at(0),
      statusCheckRollup: { contexts: { nodes, pageInfo: { hasNextPage: false } } } } }] },
  }
  const adapter = createGithubAdapter({ allowedRepositories: [repository],
    run: async () => ({ stdout: JSON.stringify({ data: { repository: {
      nameWithOwner: repository, isPrivate: false, pullRequest: pr,
    } } }), stderr: '' }),
  })
  return adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW })
}

function gates(snapshot) {
  const base = { headSha: HEAD, blockHeadSha: HEAD, checks: snapshot.checks,
    requiredChecks: REQUIRED }
  return {
    ready: shouldMarkReady({ ...base, draft: true }),
    draft: shouldConvertToDraft({ ...base, draft: false }),
    autoMerge: shouldEnableAutoMerge({ ...base, draft: false, autoMergeEnabled: false }),
  }
}

function decisionFor(snapshot) {
  return decideReadinessActions({ snapshot, body: snapshot.body,
    requiredChecks: REQUIRED, nowMs: NOW }, policy)
}

const noApp = (entry) => ({ ...entry, checkSuite: null })
const noStart = (entry) => ({ ...entry, startedAt: null })
const retainedCases = [
  ['cancelled only', [cancelled()]],
  ['older pass, even when it completes later', [cancelled(), run('ci-ok', 'SUCCESS', 5, 40)]],
  ['equal attempt start times', [cancelled(), run('ci-ok', 'SUCCESS', 10, 40)]],
  ['later pass from another producer', [cancelled(), run('ci-ok', 'SUCCESS', 30, 40, 'another-app')]],
  ['later status-context pass', [cancelled(), { __typename: 'StatusContext',
    context: 'ci-ok', state: 'SUCCESS', createdAt: at(40) }]],
  ['later pass for a different check name', [cancelled(), run('other-check', 'SUCCESS', 30, 40)]],
  ['unknown cancellation producer', [noApp(cancelled()), run('ci-ok', 'SUCCESS', 30, 40)]],
  ['unknown passing producer', [cancelled(), noApp(run('ci-ok', 'SUCCESS', 30, 40))]],
  ['both producers unknown', [noApp(cancelled()), noApp(run('ci-ok', 'SUCCESS', 30, 40))]],
  ['unknown cancellation start', [noStart(cancelled()), run('ci-ok', 'SUCCESS', 30, 40)]],
  ['unknown passing start', [cancelled(), noStart(run('ci-ok', 'SUCCESS', 30, 40))]],
]

for (const [label, entries] of retainedCases) {
  test(`${label}: cancellation remains pending and never passes either gate in either order`, async () => {
    for (const ordered of [entries, [...entries].reverse()]) {
      const snapshot = await snapshotFor(ordered)
      assert.equal(snapshot.checks.find((c) => c.name === 'ci-ok').conclusion, 'cancelled')
      assert.deepEqual(gates(snapshot), { ready: false, draft: false, autoMerge: false })
      const decision = decisionFor(snapshot)
      assert.deepEqual(decision.actions, ['update-body'])
      assert.ok(decision.block.includes('| ci-ok | ⏳ |'), decision.block)
    }
  })
}

for (const conclusion of ['SUCCESS', 'NEUTRAL', 'SKIPPED']) {
  test(`later same-producer ${conclusion} supersedes cancellation in either rollup order`, async () => {
    const entries = [cancelled(), run('ci-ok', conclusion, 30, 40)]
    for (const ordered of [entries, [...entries].reverse()]) {
      const snapshot = await snapshotFor(ordered)
      assert.equal(snapshot.checks.find((c) => c.name === 'ci-ok').conclusion, conclusion.toLowerCase())
      assert.deepEqual(gates(snapshot), { ready: true, draft: false, autoMerge: true })
    }
  })
}

for (const conclusion of ['FAILURE', 'TIMED_OUT', 'STALE', 'ACTION_REQUIRED']) {
  test(`${conclusion} cannot be hidden by cancellation or a later pass`, async () => {
    const entries = [cancelled(), run('ci-ok', conclusion, 5, 15)]
    for (const withPass of [entries, [...entries, run('ci-ok', 'SUCCESS', 30, 40)]]) {
      for (const ordered of [withPass, [...withPass].reverse()]) {
        const snapshot = await snapshotFor(ordered)
        assert.equal(snapshot.checks.find((c) => c.name === 'ci-ok').conclusion, conclusion.toLowerCase())
        assert.deepEqual(gates(snapshot), { ready: false, draft: true, autoMerge: false })
        assert.ok(decisionFor(snapshot).block.includes('| ci-ok | ❌ |'))
      }
    }
  })
}

for (const status of ['IN_PROGRESS', 'QUEUED']) {
  test(`${status} beside cancellation waits without draft flapping or arming auto-merge`, async () => {
    const entries = [cancelled(), { ...run('ci-ok', null, 30), status, completedAt: null }]
    for (const ordered of [entries, [...entries].reverse()]) {
      const snapshot = await snapshotFor(ordered)
      assert.deepEqual(gates(snapshot), { ready: false, draft: false, autoMerge: false })
      assert.deepEqual(decisionFor(snapshot).actions, ['update-body'])
      assert.ok(decisionFor(snapshot).block.includes('| ci-ok | ⏳ |'))
    }
  })
}

test('a new head still resets a ready PR to draft even with a cancelled required check', async () => {
  const snapshot = await snapshotFor([cancelled()])
  assert.equal(shouldConvertToDraft({ draft: false, headSha: HEAD, blockHeadSha: 'd'.repeat(40),
    checks: snapshot.checks, requiredChecks: REQUIRED }), true)
})

for (const [conclusion, line] of [
  ['CANCELLED', 'Paperclip Review: pending ⏳'],
  ['FAILURE', 'Paperclip Review: see check ❌'],
  ['TIMED_OUT', 'Paperclip Review: see check ❌'],
  ['STALE', 'Paperclip Review: see check ❌'],
  ['UNKNOWN_FUTURE_CONCLUSION', 'Paperclip Review: see check ❌'],
  ['SUCCESS', 'Paperclip Review: 5/5 ✅'],
]) {
  test(`real decision renders completed ${conclusion} review as ${line}`, async () => {
    const snapshot = await snapshotFor([run('Paperclip Review', conclusion)], 'Paperclip Review')
    const decision = decisionFor(snapshot)
    assert.equal(decision.block.split('\n').find((row) => row.startsWith('Paperclip Review:')), line)
    assert.equal(gates(snapshot).autoMerge, conclusion === 'SUCCESS')
  })
}

for (const conclusion of ['FAILURE', 'TIMED_OUT']) {
  test(`cancelled review cannot mask a genuine ${conclusion} review in either order`, async () => {
    const entries = [cancelled('Paperclip Review'), run('Paperclip Review', conclusion, 5, 15)]
    for (const ordered of [entries, [...entries].reverse()]) {
      const snapshot = await snapshotFor(ordered, 'Paperclip Review')
      const decision = decisionFor(snapshot)
      assert.equal(decision.block.split('\n').find((row) => row.startsWith('Paperclip Review:')),
        'Paperclip Review: see check ❌')
      assert.equal(gates(snapshot).autoMerge, false)
    }
  })
}
