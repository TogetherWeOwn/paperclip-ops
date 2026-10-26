// Latest-completed aggregation for repeated readiness check names.
//
// A head can carry several same-name runs (duplicate triggers, re-runs, and
// routine concurrency cancellations). The readiness snapshot aggregates them
// to one entry per name; the temporal last state must win, so a later
// successful completion clears an earlier cancellation instead of the
// cancellation poisoning the head forever. Equal completion times keep the
// conservative worst-wins tie-break, and an unfinished duplicate stays
// visible next to a passing completion.
//
// Offline: every GitHub call goes through an injected `run` double.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createGithubAdapter } from '../src/github-adapter.js'
import { shouldMarkReady, shouldConvertToDraft } from '../src/readiness.js'

const repository = 'TogetherWeOwn/paperclip-ops'
const HEAD = 'e'.repeat(40)
const PUSHED_AT = '2026-10-03T07:00:00.000Z'
const PUSHED_MS = Date.parse(PUSHED_AT)
const EARLY = '2026-10-03T07:01:00.000Z'
const LATER = '2026-10-03T07:02:00.000Z'
const NOW = Date.parse(LATER) + 60_000

const checkRun = (name, status, conclusion = null, completedAt = null) =>
  ({ __typename: 'CheckRun', name, status, conclusion, completedAt })
const statusCtx = (context, state, createdAt) =>
  ({ __typename: 'StatusContext', context, state, createdAt })
const completed = (name, conclusion, at) => checkRun(name, 'COMPLETED', conclusion, at)

function pullRequest(nodes) {
  return {
    number: 7, url: `https://github.com/${repository}/pull/7`,
    title: 'Latest-completed fixture', body: '## What Changed\n\n- a fix\n',
    state: 'OPEN', isDraft: true, merged: false,
    headRefName: 'fixture-branch', headRefOid: HEAD, reviewDecision: null,
    mergeStateStatus: 'CLEAN', baseRefName: 'main', autoMergeRequest: null,
    author: { __typename: 'User', login: 'fixture-author' },
    baseRepository: { nameWithOwner: repository },
    labels: { nodes: [], pageInfo: { hasNextPage: false } },
    commits: { nodes: [{ commit: { oid: HEAD, pushedDate: PUSHED_AT,
      statusCheckRollup: { contexts: { nodes, pageInfo: { hasNextPage: false } } } } }] },
  }
}

async function aggregated(entries) {
  const results = []
  for (const nodes of [entries, [...entries].reverse()]) {
    const envelope = { data: { repository: { nameWithOwner: repository, isPrivate: false,
      pullRequest: pullRequest(nodes) } } }
    const adapter = createGithubAdapter({ allowedRepositories: [repository],
      run: async () => ({ stdout: JSON.stringify(envelope), stderr: '' }) })
    const snapshot = await adapter.getReadinessSnapshot(repository, 7, { nowMs: NOW })
    assert.equal(snapshot.checks.filter((c) => c.name === 'pr-lint').length, 1,
      'one aggregated entry per name')
    results.push(snapshot)
  }
  assert.deepEqual(results[0].checks, results[1].checks,
    'aggregation must not depend on input order')
  return results[0]
}

const gate = (snapshot, draft) => ({
  markReady: shouldMarkReady({ draft, headSha: HEAD, checks: snapshot.checks,
    requiredChecks: ['pr-lint'] }),
  convertToDraft: shouldConvertToDraft({ draft, headSha: HEAD, checks: snapshot.checks,
    requiredChecks: ['pr-lint'] }),
})

test('cancelled earlier then success later aggregates to success and may mark ready', async () => {
  const snapshot = await aggregated([
    completed('pr-lint', 'CANCELLED', EARLY),
    completed('pr-lint', 'SUCCESS', LATER),
  ])
  assert.deepEqual(snapshot.checks.find((c) => c.name === 'pr-lint'),
    { name: 'pr-lint', status: 'completed', conclusion: 'success', completedMs: Date.parse(LATER) })
  assert.deepEqual(gate(snapshot, true), { markReady: true, convertToDraft: false })
  assert.deepEqual(gate(snapshot, false), { markReady: false, convertToDraft: false })
})

test('success earlier then failure later aggregates to failure and blocks', async () => {
  const snapshot = await aggregated([
    completed('pr-lint', 'SUCCESS', EARLY),
    completed('pr-lint', 'FAILURE', LATER),
  ])
  assert.deepEqual(snapshot.checks.find((c) => c.name === 'pr-lint'),
    { name: 'pr-lint', status: 'completed', conclusion: 'failure', completedMs: Date.parse(LATER) })
  assert.deepEqual(gate(snapshot, true), { markReady: false, convertToDraft: false })
  assert.deepEqual(gate(snapshot, false), { markReady: false, convertToDraft: true })
})

test('cancelled as the last completed state still blocks', async () => {
  const only = await aggregated([completed('pr-lint', 'CANCELLED', EARLY)])
  assert.equal(only.checks.find((c) => c.name === 'pr-lint').conclusion, 'cancelled')
  assert.equal(gate(only, true).markReady, false)
  assert.equal(gate(only, false).convertToDraft, true)

  const afterSuccess = await aggregated([
    completed('pr-lint', 'SUCCESS', EARLY),
    completed('pr-lint', 'CANCELLED', LATER),
  ])
  assert.equal(afterSuccess.checks.find((c) => c.name === 'pr-lint').conclusion, 'cancelled')
  assert.equal(gate(afterSuccess, true).markReady, false)
  assert.equal(gate(afterSuccess, false).convertToDraft, true)
})

test('a later success supersedes an earlier failure from a duplicate run', async () => {
  const snapshot = await aggregated([
    completed('pr-lint', 'FAILURE', EARLY),
    completed('pr-lint', 'SUCCESS', LATER),
  ])
  assert.equal(snapshot.checks.find((c) => c.name === 'pr-lint').conclusion, 'success')
  assert.deepEqual(gate(snapshot, true), { markReady: true, convertToDraft: false })
})

test('equal completion times keep the conservative worst-wins tie-break', async () => {
  const snapshot = await aggregated([
    completed('pr-lint', 'SUCCESS', EARLY),
    completed('pr-lint', 'FAILURE', EARLY),
    completed('pr-lint', 'CANCELLED', EARLY),
  ])
  const merged = snapshot.checks.find((c) => c.name === 'pr-lint')
  assert.equal(merged.status, 'completed')
  assert.notEqual(merged.conclusion, 'success')
  assert.equal(gate(snapshot, true).markReady, false)
  assert.equal(gate(snapshot, false).convertToDraft, true)
})

test('an unfinished duplicate stays visible next to a passing completion', async () => {
  const snapshot = await aggregated([
    completed('pr-lint', 'CANCELLED', EARLY),
    completed('pr-lint', 'SUCCESS', LATER),
    checkRun('pr-lint', 'IN_PROGRESS'),
  ])
  assert.deepEqual(snapshot.checks.find((c) => c.name === 'pr-lint'),
    { name: 'pr-lint', status: 'in_progress' })
  assert.deepEqual(gate(snapshot, true), { markReady: false, convertToDraft: false })
  assert.deepEqual(gate(snapshot, false), { markReady: false, convertToDraft: false })
})

test('duplicate names across check sources aggregate by completion time', async () => {
  // The rollup can carry the same name from a CheckRun and from a legacy
  // StatusContext. Required checks are matched by name, so the name is the
  // identity the gate sees; the later completion carries the last state.
  const snapshot = await aggregated([
    completed('pr-lint', 'CANCELLED', EARLY),
    statusCtx('pr-lint', 'SUCCESS', LATER),
  ])
  assert.deepEqual(snapshot.checks.find((c) => c.name === 'pr-lint'),
    { name: 'pr-lint', status: 'completed', conclusion: 'success', completedMs: Date.parse(LATER) })
  assert.equal(gate(snapshot, true).markReady, true)

  const failing = await aggregated([
    statusCtx('pr-lint', 'SUCCESS', EARLY),
    statusCtx('pr-lint', 'FAILURE', LATER),
  ])
  assert.equal(failing.checks.find((c) => c.name === 'pr-lint').conclusion, 'failure')
  assert.equal(gate(failing, true).markReady, false)
})
