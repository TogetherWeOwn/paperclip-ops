// Job-token credential route offline suite. No network, no credentials: the
// child process is an injected `exec` double that captures its environment,
// and the sweep runs against GitHub doubles. Policy files live in a 0700
// temp dir as 0600 files.
//
// What this pins:
//   1. resolveSweepToken admits EXACTLY the workflow combination
//      (READINESS_JOB_TOKEN=1 plus one GH_TOKEN) and refuses everything else.
//   2. The token reaches the child ONLY through the explicit `jobToken`
//      runner parameter, never through the base environment.
//   3. READINESS_READ_ONLY=1 reconciles as would-act with zero writes.
//   4. .github/workflows/readiness-sweep.yml carries exactly the four
//      reviewed grants, names every PR-check workflow in its workflow_run
//      wake list, and pins the fork/base queue split plus the read-only
//      selector as exact lines — widening the workflow fails here and
//      needs a fresh security review.
//   5. runSweep passes its admitted token into the production runner
//      (a recording-exec test with no github double: the github-double
//      tests never execute that line).

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, chmod, rm } from 'node:fs/promises'
import { readdirSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { GH_PIN_CODE, createPinnedRunner, isReadOnlyPass, prepareIsolatedGhConfig,
  resolveSweepToken } from '../src/gh-pinned.js'
import { runSweep } from '../scripts/readiness-sweep.mjs'

const repo = 'TogetherWeOwn/paperclip-ops'
const HEAD = 'a'.repeat(40)
const REQUIRED = ['ci-ok', 'secret-scan']
const NOW = 1728285600000
const GH = '/usr/bin/gh'
// Deliberately NOT token-shaped: a low-entropy phrase no secret scanner
// flags. The tests pin admission and passthrough, never a token format.
const TOKEN = 'test-only-not-a-credential'

// Pin the error CODE (stable contract), never the message text (drifts).
const pinRejects = (action) => assert.rejects(action, (error) => error?.code === GH_PIN_CODE)
const pinThrows = (action) => assert.throws(action, (error) => error?.code === GH_PIN_CODE)

async function scratch(t, prefix) {
  const dir = await mkdtemp(join(process.env.PAPERCLIP_RUN_SCRATCH_DIR || tmpdir(), prefix))
  t.after(() => rm(dir, { recursive: true, force: true }))
  await chmod(dir, 0o700)
  return dir
}

async function policyFile(t, value) {
  const dir = await scratch(t, 'readiness-job-token-')
  const path = join(dir, 'policy.json')
  await writeFile(path, JSON.stringify(value), { mode: 0o600 })
  return path
}

function greenDraft(number, over = {}) {
  return { repository: repo, number, headSha: HEAD, baseRef: 'main',
    state: 'open', merged: false, draft: true, autoMerge: false,
    body: '## What Changed\n\n- a fix\n', labels: [],
    mergeableState: 'clean', reviewDecision: null,
    headPushedMs: 1, nowMs: NOW,
    checks: REQUIRED.map((name) => ({ name, status: 'completed', conclusion: 'success', completedMs: 1 })),
    ...over }
}

// Double with EXACTLY the seven sweep dependencies (six duty methods plus
// the open-PR list). Writers record invocations so read-only can prove zero
// writes; anything else (merge, close, comment) has nowhere to land.
function doubles(snapshots) {
  const calls = []
  const github = {
    async listOpenPullRequests(repository) {
      calls.push(['list', repository])
      return [...snapshots.values()]
        .filter((snap) => snap.repository === repository)
        .map((snap) => ({ repository: snap.repository, number: snap.number }))
    },
    async getCloseoutSnapshot(repository, number) {
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

// --- token posture --------------------------------------------------------

test('without the job flag, a clean env admits no credential', () => {
  assert.equal(resolveSweepToken({}), undefined)
})

test('without the job flag, any ambient token fails closed (retired host posture cannot authenticate)', () => {
  pinThrows(() => resolveSweepToken({ GH_TOKEN: TOKEN }))
  pinThrows(() => resolveSweepToken({ GITHUB_TOKEN: TOKEN }))
  pinThrows(() => resolveSweepToken({ GH_TOKEN: TOKEN, GITHUB_TOKEN: TOKEN }))
})

test('a mistyped job flag leaves the route off, so the ambient token still fails closed', () => {
  for (const flag of ['', 'true', 'yes', '0', '2']) {
    pinThrows(() => resolveSweepToken({ READINESS_JOB_TOKEN: flag, GH_TOKEN: TOKEN }),
      `flag ${JSON.stringify(flag)} must not enable the route`)
  }
})

test('job mode admits exactly one GH_TOKEN and returns it verbatim', () => {
  assert.equal(resolveSweepToken({ READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN }), TOKEN)
})

test('job mode without a usable GH_TOKEN fails closed', () => {
  pinThrows(() => resolveSweepToken({ READINESS_JOB_TOKEN: '1' }))
  pinThrows(() => resolveSweepToken({ READINESS_JOB_TOKEN: '1', GH_TOKEN: '' }))
  pinThrows(() => resolveSweepToken({ READINESS_JOB_TOKEN: '1', GH_TOKEN: 'a\0b' }))
  pinThrows(() => resolveSweepToken({ READINESS_JOB_TOKEN: '1', GH_TOKEN: 42 }))
})

test('job mode with two token sources fails closed as ambiguous (no silent identity choice)', () => {
  pinThrows(() => resolveSweepToken({ READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN, GITHUB_TOKEN: TOKEN }))
  // An empty second source is absent, not ambiguous — matching the truthiness
  // rule refuseInheritedTokens has always used (agent shells carry empty
  // GH_TOKEN/GITHUB_TOKEN exports; emptiness must not brick the route).
  assert.equal(resolveSweepToken({ READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN, GITHUB_TOKEN: '' }), TOKEN)
})

test('read-only flag is an exact match', () => {
  assert.equal(isReadOnlyPass({ READINESS_READ_ONLY: '1' }), true)
  assert.equal(isReadOnlyPass({}), false)
  assert.equal(isReadOnlyPass({ READINESS_READ_ONLY: 'true' }), false)
  assert.equal(isReadOnlyPass({ READINESS_READ_ONLY: '' }), false)
})

// --- runner token channel --------------------------------------------------

async function isolatedDir(t) {
  const dir = await prepareIsolatedGhConfig()
  t.after(() => rm(dir, { recursive: true, force: true }))
  return dir
}

test('the job token reaches the child env as GH_TOKEN and nothing else token-shaped does', async (t) => {
  let captured = null
  const runner = createPinnedRunner({ executable: GH, configDir: await isolatedDir(t), jobToken: TOKEN,
    exec: async (_file, _args, options) => { captured = options.env; return { stdout: '', stderr: '' } } })
  await runner('gh', ['auth', 'status'])
  assert.equal(captured.GH_TOKEN, TOKEN)
  assert.ok(!('GITHUB_TOKEN' in captured), 'no second token source reaches the child')
  assert.ok(!('HOME' in captured), 'no HOME reaches the child')
  assert.ok(typeof captured.GH_CONFIG_DIR === 'string' && captured.GH_CONFIG_DIR.length > 0)
})

test('a token in the base env is still refused even when the explicit job token is valid', async (t) => {
  const dir = await isolatedDir(t)
  pinThrows(() => createPinnedRunner({ executable: GH, configDir: dir,
    jobToken: TOKEN, env: { PATH: '/usr/bin', GH_TOKEN: 'ambient-must-not-arrive' } }))
})

test('an empty or malformed job token fails closed at construction', async (t) => {
  const dir = await isolatedDir(t)
  pinThrows(() => createPinnedRunner({ executable: GH, configDir: dir, jobToken: '' }))
  pinThrows(() => createPinnedRunner({ executable: GH, configDir: dir, jobToken: 'a\0b' }))
  pinThrows(() => createPinnedRunner({ executable: GH, configDir: dir, jobToken: 42 }))
})

test('without a job token the child env carries no token keys (preflight then fails closed)', async (t) => {
  let captured = null
  const runner = createPinnedRunner({ executable: GH, configDir: await isolatedDir(t),
    exec: async (_file, _args, options) => { captured = options.env; return { stdout: '', stderr: '' } } })
  await runner('gh', ['auth', 'status'])
  assert.ok(!('GH_TOKEN' in captured) && !('GITHUB_TOKEN' in captured))
})

// --- read-only pass ----------------------------------------------------------

test('a read-only pass on an enabled repo would-acts with zero writes', async (t) => {
  const path = await policyFile(t, { version: 1, ghExecutable: GH,
    enabledRepositories: [repo], scanRepositories: [repo], holdLabels: ['hold'] })
  const snapshots = new Map([[`${repo}#1`, greenDraft(1)]])
  const { github, calls } = doubles(snapshots)
  const { records, counters, listErrors } = await runSweep({ policyPath: path, github, nowMs: NOW,
    env: { READINESS_READ_ONLY: '1' } })
  assert.equal(listErrors, 0)
  assert.equal(counters.errors, 0)
  assert.equal(counters.acted, 0)
  assert.equal(counters.wouldAct, 1)
  assert.deepEqual(records.map((r) => r.decision), ['would-act'])
  assert.deepEqual(records[0].actions, ['update-body', 'mark-ready'])
  assert.ok(calls.every((call) => call[0] === 'list'), `zero writes, got ${JSON.stringify(calls)}`)
})

test('control: the same enabled repo without the flag acts (the flag is what holds the writes)', async (t) => {
  const path = await policyFile(t, { version: 1, ghExecutable: GH,
    enabledRepositories: [repo], scanRepositories: [repo], holdLabels: ['hold'] })
  const snapshots = new Map([[`${repo}#1`, greenDraft(1)]])
  const { github, calls } = doubles(snapshots)
  const { counters } = await runSweep({ policyPath: path, github, nowMs: NOW, env: {} })
  assert.equal(counters.acted, 1)
  assert.equal(counters.wouldAct, 0)
  const kinds = calls.map((call) => call[0])
  assert.ok(kinds.includes('update-body') && kinds.includes('mark-ready'))
})

test('a job-token env runs the full pass posture without touching the network (doubles)', async (t) => {
  const path = await policyFile(t, { version: 1, ghExecutable: GH,
    enabledRepositories: [], scanRepositories: [repo], holdLabels: ['hold'] })
  const snapshots = new Map([[`${repo}#1`, greenDraft(1)]])
  const { github, calls } = doubles(snapshots)
  const { counters } = await runSweep({ policyPath: path, github, nowMs: NOW,
    env: { READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN } })
  assert.equal(counters.errors, 0)
  assert.equal(counters.wouldAct, 1)
  assert.ok(calls.every((call) => call[0] === 'list'))
})

test('production path: the admitted job token reaches the child GH_TOKEN with no github double', async (t) => {
  // The doubles above prove posture; this proves wiring. Every other test
  // injects a github double, which skips the production createPinnedRunner
  // line entirely — dropping the jobToken argument keeps them all green
  // while every real pass then fails preflight. This test injects only a
  // recording exec double, so the production runner line executes against a
  // canned empty PR list: the one-line mutant (createPinnedRunner without
  // jobToken) fails here because the child env carries no GH_TOKEN.
  const path = await policyFile(t, { version: 1, ghExecutable: GH,
    enabledRepositories: [], scanRepositories: [repo], holdLabels: ['hold'] })
  const seen = []
  const exec = async (_file, args, options) => {
    seen.push({ args: [...args], env: { ...options.env } })
    if (args[0] === 'auth') return { stdout: '', stderr: '' }
    return { stdout: JSON.stringify({ data: { repository: { nameWithOwner: repo,
      pullRequests: { nodes: [], totalCount: 0, pageInfo: { hasNextPage: false, endCursor: null } } } } }),
    stderr: '' }
  }
  const { records, counters, listErrors } = await runSweep({ policyPath: path, nowMs: NOW,
    env: { READINESS_JOB_TOKEN: '1', GH_TOKEN: TOKEN }, exec })
  assert.equal(listErrors, 0)
  assert.deepEqual(records, [])
  assert.equal(counters.total, 0)
  assert.ok(seen.length >= 2, `production path executed preflight plus list, got ${seen.length} child calls`)
  for (const call of seen) {
    assert.equal(call.env.GH_TOKEN, TOKEN, 'every child call carries the admitted job token')
    assert.ok(!('GITHUB_TOKEN' in call.env), 'no second token source reaches the child')
    assert.ok(!('HOME' in call.env), 'no HOME reaches the child')
  }
})

test('an ambient token without the flag fails the pass closed before any read', async (t) => {
  const path = await policyFile(t, { version: 1, ghExecutable: GH,
    enabledRepositories: [], scanRepositories: [repo], holdLabels: ['hold'] })
  const snapshots = new Map([[`${repo}#1`, greenDraft(1)]])
  const { github, calls } = doubles(snapshots)
  await pinRejects(runSweep({ policyPath: path, github, nowMs: NOW, env: { GH_TOKEN: TOKEN } }))
  assert.deepEqual(calls, [], 'refused posture reads nothing')
})

// --- workflow contract -------------------------------------------------------
// The workflow file is the credential binding: its permissions block is the
// grant scope a security review covers. These assertions pin the exact
// reviewed shape — editing the workflow's triggers, grants, or execution
// edits this contract deliberately, in the same diff.

const workflowRoot = dirname(dirname(fileURLToPath(import.meta.url)))
const workflowPath = join(workflowRoot, '..', '.github', 'workflows', 'readiness-sweep.yml')
const workflow = readFileSync(workflowPath, 'utf8')
const workflowLines = workflow.split('\n')
// Contract assertions read code lines only: the header comments explain the
// binding in prose (and name the things that must be absent), so matching
// against comments would prove nothing.
const codeLines = workflowLines.filter((line) => !/^\s*#/.test(line))
const code = codeLines.join('\n')

// Exact-line pin: the caller asserts full equality against the returned line.
// Substring hunts stay green when the guarded line is inverted while the same
// accessor survives elsewhere in the file (the fork accessor also appears in
// the concurrency group), so security selectors are pinned line-exact here.
function exactCodeLine(prefix) {
  const matches = codeLines.filter((line) => line.trimStart().startsWith(prefix))
  assert.equal(matches.length, 1, `exactly one ${JSON.stringify(prefix)} code line`)
  return matches[0].trim()
}

// Trigger keys of one workflow file's `on:` block (bare block style only: a
// flow-style `on:` fails loudly so the new shape gets a deliberate decision
// instead of silently missing the wake list).
function onBlockKeys(text, file) {
  const lines = text.split('\n')
  const start = lines.findIndex((line) => /^on:\s*(#.*)?$/.test(line))
  assert.ok(start !== -1, `${file} carries a bare block-style on: key`)
  const keys = []
  for (const line of lines.slice(start + 1)) {
    if (/^[^ #\s]/.test(line)) break
    const match = /^  ([a-z_]+):/.exec(line)
    if (match) keys.push(match[1])
  }
  return keys
}

function jobPermissionsBlock() {
  const start = workflowLines.findIndex((line) => /^    permissions:\s*$/.test(line))
  assert.ok(start !== -1, 'job-level permissions block exists')
  const pairs = []
  for (const line of workflowLines.slice(start + 1)) {
    if (!/^      /.test(line)) break
    const match = /^      ([a-z-]+): ([\w-]+)\s*$/.exec(line)
    assert.ok(match, `permissions block holds only scope lines, got ${JSON.stringify(line)}`)
    pairs.push([match[1], match[2]])
  }
  return pairs.sort((a, b) => (a[0] < b[0] ? -1 : 1))
}

test('the sweep job carries exactly the four reviewed grants, nothing more', () => {
  assert.deepEqual(jobPermissionsBlock(), [
    ['checks', 'read'],
    ['contents', 'write'],
    ['pull-requests', 'write'],
    ['statuses', 'read'],
  ])
})

test('top-level permissions deny by default (the job block is the only grant)', () => {
  assert.ok(workflowLines.some((line) => /^permissions: \{\}\s*$/.test(line)))
})

test('no permission grant outside the reviewed four exists anywhere in the file', () => {
  // Every `scope: read|write|none` code line is a grant; the set must be
  // exactly the four. (`metadata` is implicit, not a job permission key —
  // actionlint rejects it, so it must never appear here.)
  const grants = []
  for (const line of codeLines) {
    const match = /^ {2,}([a-z-]+): (read|write|none)\s*$/.exec(line)
    if (match) grants.push(match[1])
  }
  assert.deepEqual([...new Set(grants)].sort(),
    ['checks', 'contents', 'pull-requests', 'statuses'])
})

test('the workflow is event-driven with no schedule and no dead check_suite (completion wakes arrive as workflow_run)', () => {
  assert.ok(!code.includes('schedule:'), 'no schedule trigger')
  assert.ok(!code.includes('cron:'), 'no cron expression')
  // check_suite never fires here: GitHub suppresses it when the suite was
  // created by Actions or the head SHA is associated with Actions, which
  // covers every PR head in this repo. A present-but-dead trigger would read
  // as coverage for exactly the state change it cannot deliver.
  assert.ok(!code.includes('check_suite'), 'no dead check_suite trigger')
  assert.ok(code.includes('workflow_run:'), 'CI completion wakes arrive as workflow_run')
})

test('triggers cover PR changes, gating-workflow completion, and manual dispatch', () => {
  for (const trigger of ['ready_for_review', 'converted_to_draft', 'labeled', 'synchronize', 'edited',
    'workflow_run:', 'workflow_dispatch:']) {
    assert.ok(code.includes(trigger), `trigger present: ${trigger}`)
  }
  assert.ok(!code.includes('pull_request_target'), 'never pull_request_target')
})

test('workflow_run names every PR-check workflow, so no gating completion can pass without a wake', () => {
  // The gate waits on ruleset-required checks; a completion the sweep never
  // hears about stalls its PR until some later event. workflow_run has no
  // wildcard, so the workflows list must name every workflow that posts PR
  // checks. This test DERIVES that set by scanning the sibling workflow
  // files for pull_request triggers: adding a PR workflow without naming it
  // here fails loudly instead of stalling PRs silently.
  const workflowsDir = join(workflowRoot, '..', '.github', 'workflows')
  const knownTriggers = new Set(['pull_request', 'push', 'merge_group', 'workflow_call',
    'workflow_dispatch', 'workflow_run', 'schedule', 'check_run', 'check_suite'])
  const excluded = new Map([
    ['Readiness Sweep', 'naming itself would self-trigger on every pass'],
    ['review-rules (shadow)', 'shadow checks never gate readiness, so their completion wakes nothing'],
  ])
  const expected = []
  for (const file of readdirSync(workflowsDir).filter((name) => name.endsWith('.yml')).sort()) {
    const text = readFileSync(join(workflowsDir, file), 'utf8')
    const nameLine = text.split('\n').find((line) => /^name:/.test(line)) ?? ''
    const nameMatch = /^name:\s*(.+?)\s*$/.exec(nameLine)
    assert.ok(nameMatch, `${file} carries an explicit name (workflow_run matches by name, never by file)`)
    const keys = onBlockKeys(text, file)
    assert.ok(keys.length > 0, `${file} declares at least one recognized trigger`)
    for (const key of keys) {
      assert.ok(knownTriggers.has(key), `${file} trigger ${JSON.stringify(key)} is a deliberate decision, not a silent miss`)
    }
    if (!keys.includes('pull_request')) continue
    if (excluded.has(nameMatch[1])) continue
    expected.push(nameMatch[1])
  }
  assert.ok(expected.length >= 2, `sibling scan found PR-check workflows, got ${JSON.stringify(expected)}`)
  const namedMatch = /^workflows:\s*\[(.*)\]\s*$/.exec(exactCodeLine('workflows:'))
  assert.ok(namedMatch, 'workflow_run carries a flow-style workflows list')
  const named = namedMatch[1].split(',').map((entry) => entry.trim()).filter(Boolean).sort()
  assert.ok(!named.includes('Readiness Sweep'), 'the sweep never names itself (no self-trigger loop)')
  assert.deepEqual(named, [...expected].sort(),
    'every PR-check workflow is named in the wake list, nothing more')
})

test('workflow_run wakes on completion (success or failure both re-evaluate the gate)', () => {
  const start = workflowLines.findIndex((line) => /^  workflow_run:\s*(#.*)?$/.test(line))
  assert.ok(start !== -1, 'workflow_run trigger block exists')
  const block = []
  for (const line of workflowLines.slice(start + 1)) {
    if (/^  [a-z_]+:/.test(line) || /^[^ #\s]/.test(line)) break
    block.push(line)
  }
  assert.ok(block.some((line) => /types:\s*\[completed\]/.test(line)), 'workflow_run wakes on completed')
})

test('one base queue for every write-capable pass plus a separate fork queue (never keyed on event name)', () => {
  const groupLine = exactCodeLine('group: readiness-sweep-')
  // The group keys ONLY on fork/read-only separation: write-capable passes
  // from pull_request, workflow_run and dispatch MUST serialize — body/ready
  // writes carry no head precondition, so overlapping passes let a stale
  // snapshot overwrite newer state or mark an unevaluated head ready. An
  // event-name segment would split the base queue per trigger and re-open
  // the overlap (github.event_name appears below only inside the fork
  // predicate, never as a key segment); the fork queue stays separate so a
  // flood of fork-triggered passes cannot head-of-line-block pending
  // write-capable passes.
  assert.ok(!groupLine.includes('${{ github.event_name }}'), 'no event-name segment in the group key')
  assert.equal(groupLine, 'group: readiness-sweep-${{ ((github.event_name == \'pull_request\' && github.event.pull_request.head.repo.fork) || (github.event_name == \'workflow_run\' && github.event.workflow_run.head_repository.fork)) && \'fork\' || \'base\' }}')
  assert.ok(code.includes('cancel-in-progress: false'), 'queued passes wait, none are cancelled')
})

test('one pass at a time with a bounded deadline on a GitHub-hosted runner', () => {
  assert.ok(code.includes('runs-on: ubuntu-latest'))
  assert.ok(!code.includes('self-hosted'), 'no self-hosted runner anywhere')
  assert.ok(code.includes('timeout-minutes: 5'))
})

test('checkout is credentialless and both actions are full-SHA pinned', () => {
  assert.ok(code.includes('persist-credentials: false'))
  const uses = codeLines.filter((line) => /^\s+- uses:/.test(line))
  assert.ok(uses.length > 0)
  for (const line of uses) {
    assert.match(line, /@[0-9a-f]{40}( |$)/, `full-SHA pin: ${line.trim()}`)
  }
})

test('checkout pins the reviewed base tree, never PR-head code (pwn-request guard)', () => {
  // Without an explicit ref, pull_request jobs check out the PR merge ref and
  // would execute fork-controlled code beside the job token. The pin below is
  // the whole mitigation: base SHA for PR events (correct even when the PR
  // targets a non-default branch), default branch otherwise. `github.sha` is
  // event-dependent and must never select the tree.
  assert.ok(code.includes('ref: ${{'), 'checkout carries an explicit ref pin')
  assert.ok(code.includes('github.event.pull_request.base.sha'), 'PR events run the base tree')
  assert.ok(code.includes('github.event.repository.default_branch'), 'other events run the default branch')
  assert.ok(!code.includes('github.sha'), 'no event-dependent ref anywhere')
  // The workflow_run untrusted-head threat (the finding behind the file's
  // zizmor dangerous-triggers suppression): the triggering run's head must
  // never select code. These accessors have no legitimate use in this file —
  // fork origin comes from head.repo.fork / head_repository.fork, which
  // contain neither substring.
  assert.ok(!code.includes('head_sha'), 'the triggering head SHA is never referenced')
  assert.ok(!code.includes('head_branch'), 'the triggering head branch is never referenced')
})

test('the job token enters only as GH_TOKEN with the exact mode flags', () => {
  assert.ok(code.includes('GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}'))
  assert.ok(code.includes("READINESS_JOB_TOKEN: '1'"))
  assert.ok(!code.includes('GITHUB_TOKEN: ${{'), 'no second token source is wired')
})

test('fork context — PR or workflow_run — selects read-only via the exact env line', () => {
  // The flag MUST be an exact-line pin, not a substring hunt: the fork
  // accessor also appears in the concurrency group, so asserting "fork
  // appears somewhere" stays green when this line is inverted (!fork) — which
  // would read-only every base pass (nothing ever acted on) while fork passes
  // error per PR. workflow_run jobs run base code with base permissions even
  // for fork-head completions, so fork origin is re-derived from
  // head_repository.fork: a fork-timed wake must never write.
  assert.equal(exactCodeLine('READINESS_READ_ONLY:'),
    'READINESS_READ_ONLY: ${{ ((github.event_name == \'pull_request\' && github.event.pull_request.head.repo.fork) || (github.event_name == \'workflow_run\' && github.event.workflow_run.head_repository.fork)) && \'1\' || \'\' }}')
})

test('the pass skips loud when the base tree predates the policy (bootstrap, never expected-red)', () => {
  // pull_request runs check out the reviewed base tree, so on the change that
  // introduces this route the base tree has no policy file yet. The step must
  // skip green with a loud line — not fail — or the introduction change
  // carries an expected-red check.
  assert.ok(code.includes('if [ ! -f gh-event-capture/readiness-policy.json ]; then'))
  assert.ok(code.includes('exit 0'))
})

test('no key material, mint, or foreign secret reference exists in the workflow', () => {
  for (const fragment of ['PRIVATE KEY', 'gh-app.pem', 'GH_APP', 'BEGIN ', 'mint', 'secrets.GH_',
    'secrets.PAPERCLIP', 'secrets.OPERATOR']) {
    assert.ok(!code.includes(fragment), `forbidden fragment absent: ${fragment}`)
  }
})
