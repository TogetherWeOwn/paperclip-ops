// QA-owned review-gap pins for the readiness snapshot reader.
//
// Paperclip Review 4/5 on PR #66 (head 8c6aa23) found production correct but
// three test-gate gaps: the neutral rank branch, the readiness absent-date
// and clock guards, and the exact diagnostic values. This file pins those
// branches. It leaves the independent Test Author suite
// (test/readiness-snapshot-regressions.test.mjs) byte-identical: no existing
// acceptance assertion is edited here.
//
// Offline: every GitHub call goes through an injected `run`/`exec` double.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, chmod, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { createGithubAdapter } from '../src/github-adapter.js'
import { createReconciler } from '../src/readiness-reconcile.js'
import { runSweep, recordLine } from '../scripts/readiness-sweep.mjs'

const repository = 'TogetherWeOwn/paperclip-ops'
const GH = '/usr/bin/gh'
const TOKEN = 'test-only-not-a-credential'
const HEAD = 'c'.repeat(40)
const OTHER_HEAD = 'd'.repeat(40)
const PUSHED_AT = '2026-10-03T07:00:00.000Z'
const PUSHED_MS = Date.parse(PUSHED_AT)
const NOW = PUSHED_MS + 60_000
const REQUIRED = ['ci-ok', 'secret-scan', 'Paperclip Review']
const RULES = [{ type: 'required_status_checks',
  parameters: { required_status_checks: REQUIRED.map((context) => ({ context })) } }]

const checkRun = (name, status, conclusion = null, completedAt = null) =>
  ({ __typename: 'CheckRun', name, status, conclusion, completedAt })
const ok = (name) => checkRun(name, 'COMPLETED', 'SUCCESS', PUSHED_AT)
const baseChecks = () => REQUIRED.map(ok)

function pullRequest({ draft = true, nodes = baseChecks(), pushedDate = null, oid = HEAD,
  rollup = undefined, overrides = {} } = {}) {
  return {
    number: 7, url: `https://github.com/${repository}/pull/7`,
    title: 'Review-gap fixture', body: '## What Changed\n\n- a fix\n',
    state: 'OPEN', isDraft: draft, merged: false,
    headRefName: 'fixture-branch', headRefOid: HEAD, reviewDecision: null,
    mergeStateStatus: 'CLEAN', baseRefName: 'main', autoMergeRequest: null,
    author: { __typename: 'User', login: 'fixture-author' },
    baseRepository: { nameWithOwner: repository },
    labels: { nodes: [], pageInfo: { hasNextPage: false } },
    commits: { nodes: [{ commit: { oid, pushedDate,
      statusCheckRollup: rollup === undefined
        ? { contexts: { nodes, pageInfo: { hasNextPage: false } } } : rollup } }] },
    ...overrides,
  }
}
const snapshotEnvelope = (pr) => ({ data: { repository: { nameWithOwner: repository, isPrivate: false, pullRequest: pr } } })
const listEnvelope = (numbers) => ({ data: { repository: { nameWithOwner: repository,
  pullRequests: { nodes: numbers.map((number) => ({ number })), totalCount: numbers.length,
    pageInfo: { hasNextPage: false, endCursor: null } } } } })

function githubDouble({ pr = pullRequest(), snapshot = undefined, rules = RULES, snapshotError = null } = {}) {
  const calls = []
  const writes = []
  const snapshots = []
  function respond(args) {
    calls.push([...args])
    if (args[0] === 'auth') return ''
    const query = args.find((arg) => typeof arg === 'string' && arg.startsWith('query='))
    if (args.includes('--method') && args[args.indexOf('--method') + 1] !== 'GET') {
      writes.push([...args])
      return '{}'
    }
    if (args[0] === 'pr' && args[1] === 'merge') { writes.push([...args]); return '' }
    if (query !== undefined) {
      if (query.includes('mutation')) { writes.push([...args]); return '{}' }
      if (query.includes('pullRequests(')) return JSON.stringify(listEnvelope([7]))
      if (query.includes('pullRequest(number')) {
        snapshots.push([...args])
        if (snapshotError) throw snapshotError
        return JSON.stringify(snapshot ?? snapshotEnvelope(pr))
      }
      throw new Error('unexpected GraphQL document')
    }
    if (args.some((arg) => typeof arg === 'string' && arg.includes('/rules/branches/'))) return JSON.stringify(rules)
    throw new Error('unexpected GitHub request')
  }
  return {
    calls, writes, snapshots,
    run: async (_file, args) => ({ stdout: respond(args), stderr: '' }),
    exec: async (_file, args) => ({ stdout: respond(args), stderr: '' }),
  }
}

function adapterOver(double) {
  return createGithubAdapter({ allowedRepositories: [repository], run: double.run })
}

function reconcilerOver(double, { enabled = true } = {}) {
  const adapter = adapterOver(double)
  const github = {
    ...adapter,
    getCloseoutSnapshot: async () => { throw new Error('closeout reader is not the readiness path') },
    updatePullRequestBody: async () => {},
    markReadyForReview: async () => {},
    convertToDraft: async () => {},
    enableAutoMerge: async () => {},
  }
  const reconciler = createReconciler({ github,
    policy: { enabledRepositories: enabled ? [repository] : [], holdLabels: ['hold'] }, nowMs: () => NOW })
  return { reconciler }
}

async function scratch(t) {
  const dir = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), 'readiness-review-gaps-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  await chmod(dir, 0o700)
  return dir
}

async function policyFile(t, { enabled = [] } = {}) {
  const path = join(await scratch(t), 'policy.json')
  await writeFile(path, JSON.stringify({ version: 1, ghExecutable: GH,
    enabledRepositories: enabled, scanRepositories: [repository], holdLabels: ['hold'] }), { mode: 0o600 })
  return path
}

const JOB_ENV = { READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN }

async function sweep(t, double, { enabled = [], env = JOB_ENV } = {}) {
  const policyPath = await policyFile(t, { enabled })
  return runSweep({ policyPath, nowMs: NOW, env, exec: double.exec })
}

// --- Finding 1: neutral rank branch and non-FAILURE failing conclusions ------

async function aggregatedConclusion(entries) {
  const others = baseChecks().filter((node) => node.name !== 'ci-ok')
  const conclusions = []
  for (const nodes of [[...entries, ...others], [...entries].reverse().concat(others)]) {
    const snapshot = await adapterOver(githubDouble({
      pr: pullRequest({ pushedDate: PUSHED_AT, nodes }) })).getReadinessSnapshot(repository, 7, { nowMs: NOW })
    const merged = snapshot.checks.find((c) => c.name === 'ci-ok')
    assert.equal(snapshot.checks.filter((c) => c.name === 'ci-ok').length, 1, 'one aggregated entry per name')
    conclusions.push(merged)
  }
  assert.deepEqual(conclusions[0], conclusions[1], 'aggregation must not depend on input order')
  return conclusions[0]
}

async function gateActions(entries, draft) {
  const others = baseChecks().filter((node) => node.name !== 'ci-ok')
  const seen = []
  for (const nodes of [[...entries, ...others], [...entries].reverse().concat(others)]) {
    const { reconciler } = reconcilerOver(githubDouble({ pr: pullRequest({ draft, pushedDate: PUSHED_AT, nodes }) }))
    const { records, counters } = await reconciler.runReconcile({ references: [{ repository, number: 7 }] })
    assert.equal(counters.errors, 0, JSON.stringify(records))
    assert.equal(counters.evaluated, 1)
    seen.push(records[0].actions ?? [])
  }
  assert.deepEqual(seen[0], seen[1], 'gate must not depend on input order')
  return seen[0]
}

test('NEUTRAL with TIMED_OUT aggregates to timed_out in both orders and never passes the gate', async () => {
  const entries = [checkRun('ci-ok', 'COMPLETED', 'NEUTRAL', PUSHED_AT), checkRun('ci-ok', 'COMPLETED', 'TIMED_OUT', PUSHED_AT)]
  const merged = await aggregatedConclusion(entries)
  assert.equal(merged.status, 'completed')
  assert.equal(merged.conclusion, 'timed_out')
  assert.deepEqual(await gateActions(entries, true), ['update-body'])
  assert.deepEqual(await gateActions(entries, false), ['update-body', 'convert-to-draft'])
})

test('SKIPPED with unattributed CANCELLED retains cancellation and waits in both orders', async () => {
  const entries = [checkRun('ci-ok', 'COMPLETED', 'SKIPPED', PUSHED_AT), checkRun('ci-ok', 'COMPLETED', 'CANCELLED', PUSHED_AT)]
  const merged = await aggregatedConclusion(entries)
  assert.equal(merged.status, 'completed')
  assert.equal(merged.conclusion, 'cancelled')
  assert.deepEqual(await gateActions(entries, true), ['update-body'])
  assert.deepEqual(await gateActions(entries, false), ['update-body'])
})

// --- Finding 2: readiness absent-date and clock guards -----------------------

test('readiness reader rejects an absent pushedDate field (only explicit null is accepted)', async () => {
  const pr = pullRequest({ pushedDate: PUSHED_AT })
  delete pr.commits.nodes[0].commit.pushedDate
  const double = githubDouble({ pr })
  await assert.rejects(adapterOver(double).getReadinessSnapshot(repository, 7, { nowMs: NOW }))
  assert.equal(double.snapshots.length, 1, 'one read, no retry')
})

test('readiness reader rejects a clock before a real head push', async () => {
  const double = githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT }) })
  await assert.rejects(adapterOver(double).getReadinessSnapshot(repository, 7, { nowMs: PUSHED_MS - 1 }))
  assert.equal(double.snapshots.length, 1, 'one read, no retry')
})

test('readiness reader accepts the boundary clock exactly at the head push', async () => {
  const snapshot = await adapterOver(githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT }) }))
    .getReadinessSnapshot(repository, 7, { nowMs: PUSHED_MS })
  assert.equal(snapshot.headPushedMs, PUSHED_MS)
})

// --- Finding 3: exact diagnostic values --------------------------------------

test('snapshotFacts and record lines are exact for control, null-date and repeated fixtures', async (t) => {
  const dupOne = [...baseChecks(), ok('ci-ok')]
  const dupTwo = [...baseChecks(), ok('ci-ok'), ok('secret-scan'), ok('secret-scan')]
  const cases = [
    ['control', pullRequest({ pushedDate: PUSHED_AT }),
      { headPushTime: 'present', repeatedNames: 0, repeatedEntries: 0 }],
    ['nullDate', pullRequest({ pushedDate: null }),
      { headPushTime: 'null', repeatedNames: 0, repeatedEntries: 0 }],
    ['dupOne', pullRequest({ pushedDate: PUSHED_AT, nodes: dupOne }),
      { headPushTime: 'present', repeatedNames: 1, repeatedEntries: 1 }],
    ['dupTwo', pullRequest({ pushedDate: PUSHED_AT, nodes: dupTwo }),
      { headPushTime: 'present', repeatedNames: 2, repeatedEntries: 3 }],
  ]
  for (const [label, pr, facts] of cases) {
    const double = githubDouble({ pr })
    const result = await sweep(t, double)
    assert.equal(result.counters.errors, 0, `${label}: ${JSON.stringify(result.records)}`)
    assert.equal(result.counters.evaluated, 1, label)
    assert.deepEqual(double.writes, [], label)
    assert.deepEqual(result.records[0].snapshotFacts, facts, label)
    assert.equal(recordLine(result.records[0]),
      `readiness ${repository}#7@${HEAD}: would-act (block-missing+mark-ready) ` +
      `headPushTime=${facts.headPushTime} repeatedNames=${facts.repeatedNames} repeatedEntries=${facts.repeatedEntries}`,
      label)
  }
})

test('sweep log surfaces the sanitized writeFailure class of a failed mark-ready', async () => {
  const record = {
    repository, number: 7, headSha: HEAD, decision: 'error',
    actions: ['mark-ready'], reason: 'mark-ready',
    steps: [{ action: 'mark-ready', result: 'failed', error: 'mark-ready-failed', failure: 'http-404' }],
  }
  assert.equal(recordLine(record),
    `readiness ${repository}#7@${HEAD}: error (mark-ready) writeFailure=mark-ready:http-404`)
})

// End to end through runSweep and the production pinned runner (PR #169
// review): a GraphQL permission refusal on mark-ready (HTTP 200, errors[].type
// FORBIDDEN, gh exit 1, no HTTP code) and an HTTP 401 must each reach the log
// line as their class — before the runner kept these facts, both said unknown.
test('sweep log names the real write failure class through the pinned runner', async (t) => {
  const refusal = JSON.stringify({ data: { markPullRequestReadyForReview: null },
    errors: [{ type: 'FORBIDDEN', path: ['markPullRequestReadyForReview'], extensions: { saml_failure: false },
      locations: [{ line: 1, column: 58 }], message: 'Resource not accessible by integration' }] })
  for (const [stdout, stderr, expected] of [
    [refusal, 'gh: Resource not accessible by integration', 'graphql-forbidden'],
    ['', 'gh: Bad credentials (HTTP 401)', 'http-401'],
  ]) {
    const double = githubDouble({ pr: pullRequest({ pushedDate: PUSHED_AT }) })
    const exec = async (file, args) => {
      const query = args.find((arg) => typeof arg === 'string' && arg.startsWith('query=')) ?? ''
      if (query.includes('ReadinessNodeId')) {
        return { stdout: JSON.stringify({ data: { repository: { nameWithOwner: repository, pullRequest: { id: 'UFJfNw==' } } } }), stderr: '' }
      }
      if (query.includes('markPullRequestReadyForReview')) {
        throw Object.assign(new Error(`Command failed: gh ${TOKEN}`), { code: 1, stdout, stderr: `${stderr} ${TOKEN}` })
      }
      return double.exec(file, args)
    }
    const policyPath = await policyFile(t, { enabled: [repository] })
    const result = await runSweep({ policyPath, nowMs: NOW, env: JOB_ENV, exec })
    const record = result.records[0]
    assert.equal(record.decision, 'error', JSON.stringify(record))
    const line = recordLine(record)
    assert.ok(line.endsWith(` writeFailure=mark-ready:${expected}`), line)
    assert.equal(JSON.stringify(record).includes(TOKEN), false)
    assert.equal(line.includes(TOKEN), false)
  }
})

test('snapshotFailure is exact for every failure class', async (t) => {
  const classes = {
    'query-transport': { snapshotError: new Error('boom') },
    'query-response': { snapshot: { errors: [{ message: 'boom' }], ...snapshotEnvelope(pullRequest()) } },
    'head-outside-window': { pr: pullRequest({ oid: OTHER_HEAD }) },
    'check-rollup-incomplete': { pr: pullRequest({ rollup: { contexts: { nodes: baseChecks(), pageInfo: { hasNextPage: true } } } }) },
    'check-malformed': { pr: pullRequest({ nodes: [checkRun('ci-ok', 'COMPLETED', null, PUSHED_AT)] }) },
    'snapshot-invalid': { pr: pullRequest({ overrides: { baseRepository: { nameWithOwner: 'Other/repo' } } }) },
  }
  for (const [code, options] of Object.entries(classes)) {
    const double = githubDouble(options)
    const result = await sweep(t, double)
    assert.equal(result.counters.errors, 1, code)
    assert.equal(result.counters.evaluated, 0, code)
    assert.deepEqual(double.writes, [], code)
    assert.equal(result.records[0].snapshotFailure, code, code)
    assert.equal(recordLine(result.records[0]),
      `readiness ${repository}#7@null: none (snapshot-read-failed) snapshotFailure=${code}`,
      code)
  }
})


test('readiness rejects malformed check start times before any downstream write', async (t) => {
  // Date.parse accepts the number 0 through coercion, so this also pins the
  // explicit string-type guard rather than merely testing an invalid date.
  for (const startedAt of ['not-a-date', '', 0, false, { value: 1 }]) {
    const nodes = baseChecks()
    nodes[0] = { ...nodes[0], startedAt, checkSuite: { app: { slug: 'github-actions' } } }
    const pr = pullRequest({ pushedDate: PUSHED_AT, nodes })
    const direct = githubDouble({ pr })
    await assert.rejects(adapterOver(direct).getReadinessSnapshot(repository, 7, { nowMs: NOW }),
      (error) => error.code === 'check-malformed')
    assert.deepEqual(direct.writes, [])
    const double = githubDouble({ pr })
    const result = await sweep(t, double, { enabled: [repository] })
    assert.equal(result.counters.errors, 1, JSON.stringify(startedAt))
    assert.equal(result.counters.evaluated, 0, JSON.stringify(startedAt))
    assert.equal(result.records[0].snapshotFailure, 'check-malformed', JSON.stringify(startedAt))
    assert.deepEqual(double.writes, [], JSON.stringify(startedAt))
  }
})
