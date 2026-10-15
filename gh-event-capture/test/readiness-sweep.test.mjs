// Readiness sweep runner offline suite. No network, no credentials: the
// GitHub surface is a double implementing the six duty methods plus
// listOpenPullRequests. Policy files live in a 0700 temp dir as 0600 files.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, chmod, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { access } from 'node:fs/promises'

import { parseSweepPolicy, loadSweepPolicy, runSweep, recordLine, removeIsolatedGhConfig, summaryLine, main } from '../scripts/readiness-sweep.mjs'
import { prepareIsolatedGhConfig } from '../src/gh-pinned.js'

const repo = 'TogetherWeOwn/paperclip-ops'
const other = 'TogetherWeOwn/kofra'
const head = (c) => c.repeat(40)
const HEAD = head('a')
const REQUIRED = ['ci-ok', 'gitleaks']
const NOW = 1728285600000
const GH = '/usr/bin/gh'
// Explicit empty env: the sweep refuses inherited token env, so every pass
// runs with a known-clean environment instead of the runner's ambient one.
const CLEAN_ENV = {}
const pol = (over = {}) => ({ version: 1, ghExecutable: GH, enabledRepositories: [], ...over })

function greenChecks() {
  return [
    { name: 'ci-ok', status: 'completed', conclusion: 'success', completedMs: 1 },
    { name: 'gitleaks', status: 'completed', conclusion: 'success', completedMs: 1 },
  ]
}

function snapshot(number, over = {}) {
  return { repository: repo, number, headSha: HEAD, baseRef: 'main',
    state: 'open', merged: false, draft: true, autoMerge: false,
    body: '## What Changed\n\n- a fix\n', labels: [],
    mergeableState: 'clean', reviewDecision: null,
    headPushedMs: 1, nowMs: NOW, checks: greenChecks(), ...over }
}

// Double with EXACTLY the seven sweep dependencies (six duty methods plus
// the open-PR list). Anything else (merge, close, comment) has nowhere to
// land, so an arm/merge mutant cannot hide.
function doubles(openPRs, snapshots, { failList = [], events = null } = {}) {
  const calls = []
  const github = {
    async listOpenPullRequests(repository) {
      calls.push(['list', repository])
      if (failList.includes(repository)) throw new Error('GitHub open PR scan is incomplete')
      return openPRs.get(repository) ?? []
    },
    async getCloseoutSnapshot(repository, number) {
      if (events) events.push(['snapshot', repository, number])
      const found = snapshots.get(`${repository}#${number}`)
      if (!found) throw new Error('GitHub query failed: transport, timeout, output limit or invalid JSON')
      return found
    },
    async getRulesetRequiredChecks() { return REQUIRED },
    async updatePullRequestBody(repository, number) { calls.push(['update-body', repository, number]) },
    async markReadyForReview(repository, number) { calls.push(['mark-ready', repository, number]) },
    async convertToDraft(repository, number) { calls.push(['convert-to-draft', repository, number]) },
    async enableAutoMerge(repository, number, headSha) { calls.push(['enable-automerge', repository, number, headSha]) },
  }
  return { github, calls }
}

async function policyFile(value) {
  const dir = await mkdtemp(join(tmpdir(), 'readiness-sweep-'))
  await chmod(dir, 0o700)
  const path = join(dir, 'policy.json')
  await writeFile(path, typeof value === 'string' ? value : JSON.stringify(value), { mode: 0o600 })
  return path
}

// --- policy shape ----------------------------------------------------------

test('minimal policy defaults scan to enabled and hold label to hold', () => {
  const parsed = parseSweepPolicy(pol({ enabledRepositories: [repo] }))
  assert.deepEqual(parsed.scanRepositories, [repo])
  assert.deepEqual(parsed.holdLabels, ['hold'])
  assert.equal(parsed.ghExecutable, GH)
})

test('unknown keys, wrong version and bad names are rejected', () => {
  assert.throws(() => parseSweepPolicy(pol({ extra: 1 })), /invalid/)
  assert.throws(() => parseSweepPolicy(pol({ version: 2 })), /invalid/)
  assert.throws(() => parseSweepPolicy(pol({ enabledRepositories: ['not a repo'] })), /invalid/)
  assert.throws(() => parseSweepPolicy(pol({ enabledRepositories: ['o/r', 'o/r'] })), /invalid/)
})

test('the gh pin is mandatory with no PATH fallback', () => {
  const { ghExecutable: _drop, ...noPin } = pol()
  assert.throws(() => parseSweepPolicy(noPin), /invalid/)
  assert.throws(() => parseSweepPolicy(pol({ ghExecutable: 'gh' })), /invalid/)
  assert.throws(() => parseSweepPolicy(pol({ ghExecutable: '/usr/bin/gh\0x' })), /invalid/)
})

test('garbage scan names are rejected at parse, not as run errors', () => {
  assert.throws(() => parseSweepPolicy(pol({ scanRepositories: ['not a repo'] })), /invalid/)
  assert.throws(() => parseSweepPolicy(pol({ scanRepositories: [repo, 'o/r', 'o/r'] })), /invalid/)
})

test('enabling a repository the sweep does not scan is rejected', () => {
  assert.throws(() => parseSweepPolicy(
    pol({ enabledRepositories: [repo], scanRepositories: [other] })), /does not scan/)
})

test('missing file, relative path and group-readable file are rejected opaquely', async () => {
  await assert.rejects(loadSweepPolicy('/nonexistent/readiness-policy.json'), /unreadable/)
  await assert.rejects(loadSweepPolicy('relative/policy.json'), /invalid/)
  await assert.rejects(loadSweepPolicy('/etc/hostname'), /unreadable|invalid/)
  const path = await policyFile(pol())
  await chmod(path, 0o640)
  await assert.rejects(loadSweepPolicy(path), /invalid/)
})

test('malformed JSON and wrong shape are rejected opaquely', async () => {
  await assert.rejects(loadSweepPolicy(await policyFile('{nope')), /unreadable/)
  await assert.rejects(loadSweepPolicy(await policyFile([1, 2])), /invalid/)
})

test('valid policy round-trips from a 0600 file', async () => {
  const path = await policyFile(pol({ enabledRepositories: [repo], scanRepositories: [repo, other] }))
  const parsed = await loadSweepPolicy(path)
  assert.deepEqual(parsed.enabledRepositories, [repo])
  assert.deepEqual(parsed.scanRepositories, [repo, other])
})

// --- sweep behavior --------------------------------------------------------

test('enabled green draft is acted on; scan-only repo would-acts and changes nothing', async () => {
  const path = await policyFile(pol({ enabledRepositories: [repo], scanRepositories: [repo, other] }))
  const openPRs = new Map([
    [repo, [{ repository: repo, number: 1 }, { repository: repo, number: 2 }]],
    [other, [{ repository: other, number: 7 }]],
  ])
  const snapshots = new Map([
    [`${repo}#1`, snapshot(1)],
    [`${repo}#2`, snapshot(2)],
    [`${other}#7`, snapshot(7, { repository: other })],
  ])
  const events = []
  const { github, calls } = doubles(openPRs, snapshots, { events })
  const streamed = []
  const { records, counters, listErrors } = await runSweep({ policyPath: path, github, nowMs: NOW,
    env: CLEAN_ENV, onRecord: (r) => {
      streamed.push(`${r.repository}#${r.number}`)
      events.push(['streamed', r.repository, r.number])
    } })
  assert.equal(listErrors, 0)
  assert.equal(counters.errors, 0)
  assert.equal(counters.acted, 2)
  assert.equal(counters.wouldAct, 1)
  const kinds = calls.map((call) => call[0])
  assert.ok(kinds.includes('update-body') && kinds.includes('mark-ready'))
  assert.ok(!kinds.includes('enable-automerge'))
  const writes = calls.filter((call) => call[0] !== 'list')
  assert.ok(writes.length > 0 && writes.every((call) => call[1] === repo),
    'scan-only repo takes no writes (list reads are not writes)')
  // Records stream per PR as the pass proceeds (SIGKILL observability):
  // the second PR in the same repo streams before the next repo starts.
  assert.deepEqual(streamed, [`${repo}#1`, `${repo}#2`, `${other}#7`])
  // Interleaving proof: PR 1's record streams before PR 2's snapshot is
  // even read. A batched implementation (all snapshots, then all records)
  // reads every snapshot first, so this fails against the old code.
  const at = (kind, repository, number) =>
    events.findIndex((e) => e[0] === kind && e[1] === repository && e[2] === number)
  assert.ok(at('snapshot', repo, 1) !== -1 && at('streamed', repo, 1) !== -1 && at('snapshot', repo, 2) !== -1)
  assert.ok(at('snapshot', repo, 1) < at('streamed', repo, 1) && at('streamed', repo, 1) < at('snapshot', repo, 2),
    'per-PR streaming: snapshot 1, stream 1, then snapshot 2')
  const acted = records.find((r) => r.number === 1)
  assert.deepEqual(acted.actions, ['update-body', 'mark-ready'])
  // Journal lines carry fixed words plus identity only — never PR text.
  const line = recordLine(acted)
  assert.ok(line.includes(`${repo}#1@${HEAD}`) && line.includes('acted'))
  assert.ok(!line.includes('a fix'))
})

test('one unreadable repo list is counted loudly without blinding the rest', async () => {
  const path = await policyFile(pol({ scanRepositories: [repo, other] }))
  const openPRs = new Map([[other, [{ repository: other, number: 7 }]]])
  const snapshots = new Map([[`${other}#7`, snapshot(7, { repository: other })]])
  const { github } = doubles(openPRs, snapshots, { failList: [repo] })
  const { counters, listErrors } = await runSweep({ policyPath: path, github, nowMs: NOW, env: CLEAN_ENV })
  assert.equal(listErrors, 1)
  assert.equal(counters.wouldAct, 1)
  assert.equal(counters.errors, 0)
})

test('flag-off example policy is an inert exit-0 with no injected github', async () => {
  // The shipped example shape: empty scan touches nothing — in particular it
  // never constructs the adapter (which requires a non-empty allowlist) and
  // never reaches the network, so this runs without credentials anywhere.
  const path = await policyFile(pol())
  const { records, counters, listErrors } = await runSweep({ policyPath: path, nowMs: NOW, env: CLEAN_ENV })
  assert.deepEqual(records, [])
  assert.equal(listErrors, 0)
  for (const key of ['total', 'evaluated', 'acted', 'wouldAct', 'noOp', 'errors',
    'updatedBody', 'markedReady', 'convertedToDraft', 'enabledAutomerge']) {
    assert.equal(counters[key], 0, `counter ${key} is not zero`)
  }
})

test('inherited token env refuses the pass before any policy read', async () => {
  const path = await policyFile(pol({ scanRepositories: [repo] }))
  const { github } = doubles(new Map(), new Map())
  await assert.rejects(runSweep({ policyPath: path, github, nowMs: NOW, env: { GH_TOKEN: 'x' } }), /refused/)
  await assert.rejects(runSweep({ policyPath: path, github, nowMs: NOW, env: { GITHUB_TOKEN: 'x' } }), /refused/)
})

test('per-pass config dir is prepared empty and removed afterwards', async () => {
  // The sweep removes its isolated GH_CONFIG_DIR in `finally` on every
  // settled path; this proves the primitive roundtrip leaves nothing behind.
  const dir = await prepareIsolatedGhConfig()
  await removeIsolatedGhConfig(dir)
  await assert.rejects(access(dir), { code: 'ENOENT' })
  // Removing twice is safe: a double-settled pass still cleans.
  await removeIsolatedGhConfig(dir)
})

test('a failing pass still runs config removal through the sweep (finally wiring)', async () => {
  // With `config` fakes injected, preparation runs even for doubles, so this
  // drives the real runSweep lifecycle without filesystem or network. The
  // list-error path throws past the try, and the `finally` must still remove
  // the prepared dir. Deleting the `finally` in runSweep fails this test —
  // the helper-only roundtrip above would not catch that.
  const path = await policyFile(pol({ scanRepositories: [repo] }))
  const { github } = doubles(new Map(), new Map(), { failList: [repo] })
  const prepared = []
  const removed = []
  const { listErrors } = await runSweep({ policyPath: path, github, nowMs: NOW,
    env: CLEAN_ENV,
    config: {
      prepare: async () => { prepared.push(`sentinel-${prepared.length}`); return prepared.at(-1) },
      remove: async (dir) => { removed.push(dir) },
    } })
  assert.equal(listErrors, 1)
  assert.deepEqual(prepared, ['sentinel-0'])
  assert.deepEqual(removed, ['sentinel-0'], 'finally must remove the prepared dir on error paths')
})

test('summary line carries every counter for journal grep', () => {
  const line = summaryLine({ total: 2, evaluated: 2, acted: 1, wouldAct: 1, noOp: 0,
    errors: 0, updatedBody: 1, markedReady: 1, convertedToDraft: 0, enabledAutomerge: 0 }, 0)
  for (const key of ['total=2', 'acted=1', 'wouldAct=1', 'errors=0', 'listErrors=0', 'markedReady=1']) {
    assert.ok(line.includes(key), `summary misses ${key}`)
  }
})

// --- main exit codes --------------------------------------------------------

test('main: usage failure exits 2, policy failure exits 2, errors exit 1, clean exits 0', async () => {
  const lines = []
  const errs = []
  const deps = { out: (l) => lines.push(l), err: (e) => errs.push(e) }
  assert.equal(await main([], deps), 2)
  assert.equal(await main(['/nonexistent/policy.json'], deps), 2)
  const okSweep = async () => ({ records: [], counters: { total: 0, evaluated: 0, acted: 0,
    wouldAct: 0, noOp: 0, errors: 0, updatedBody: 0, markedReady: 0, convertedToDraft: 0,
    enabledAutomerge: 0 }, listErrors: 0 })
  assert.equal(await main(['/p.json'], { ...deps, sweep: okSweep }), 0)
  const badSweep = async () => ({ records: [], counters: { total: 1, evaluated: 1, acted: 0,
    wouldAct: 0, noOp: 0, errors: 1, updatedBody: 0, markedReady: 0, convertedToDraft: 0,
    enabledAutomerge: 0 }, listErrors: 0 })
  assert.equal(await main(['/p.json'], { ...deps, sweep: badSweep }), 1)
  const throwing = async () => { throw new Error('boom') }
  assert.equal(await main(['/p.json'], { ...deps, sweep: throwing }), 1)
  const throwingPin = async () => {
    const error = new Error('pinned gh credential is missing or unusable')
    error.code = 'READINESS_GH_PIN'
    throw error
  }
  assert.equal(await main(['/p.json'], { ...deps, sweep: throwingPin }), 2)
})
