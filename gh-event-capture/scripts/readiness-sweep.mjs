#!/usr/bin/env node
// Readiness sweep: one pass of the PR readiness reconcile duty.
//
// The duty lives in ../src/readiness-reconcile.js (pure decisions +
// reconciler) and the GitHub surface in ../src/github-adapter.js; this
// script is the only production caller that constructs them. It performs ONE
// pass and exits: no repeating scheduler lives here — the Actions workflow
// (.github/workflows/readiness-sweep.yml, event-driven) is the only driver
// (an unconditional loop is forbidden: it would act without a supervisor
// deadline and hide stalls).
//
// Fail-closed: a bad policy, a refused credential, an unreadable PR list, or
// any per-PR error is counted and loud (exit 1 on run errors; exit 2 on
// usage, policy or credential failure). Records stream one log line each
// as their repo finishes: fixed words plus validated repo/number/head only —
// bodies, titles and label names are never echoed (fork-controlled text).
//
// Policy file (0600, owner-uid, absolute path as argv[2]):
//   {"version":1,"ghExecutable":"/usr/bin/gh","enabledRepositories":[],
//    "scanRepositories":[],"holdLabels":["hold"]}
// The default is flag-off everywhere: an empty scan list exits 0 having
// touched nothing, and repos in scanRepositories but absent from
// enabledRepositories log would-act and change nothing. Rollback is the
// flag: remove the repo. The repo ships readiness-policy.example.json (shape
// reference, always flag-off) plus the live readiness-policy.json (flag-on
// for paperclip-ops only since the reviewed flag-on change; rollback is the
// revert); the workflow installs the live file 0600 into job-private temp
// before invoking this script.
//
// Credential (review R1): GitHub is reached ONLY through the absolute,
// policy-pinned `ghExecutable` — never a PATH-resolved `gh` — with a
// per-pass isolated empty GH_CONFIG_DIR (no stored personal login is
// reachable) and a startup `auth status` preflight that exits 2 when no
// credential is present. The ONLY admitted credential is the workflow's
// ephemeral per-job token: READINESS_JOB_TOKEN=1 plus exactly GH_TOKEN (see
// resolveSweepToken — an ambient token without the flag, or two token
// sources at once, fails closed). The child inherits no HOME, so the pinned
// executable must be HOME-independent. No PEM, no mint, no key custody:
// the host-PEM wrapper path was retired and must not be reintroduced.
//
// Fork-context guard: the workflow sets READINESS_READ_ONLY=1 when the
// triggering event is a fork pull_request (whose job token GitHub already
// limits to read-only). A read-only pass keeps its scan but reconciles with
// an empty enabled set, so every decision lands as would-act instead of
// erroring per PR.

import { constants } from 'node:fs'
import { open, rm } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { createGithubAdapter } from '../src/github-adapter.js'
import { createReconciler, emptyCounters, readinessPolicy } from '../src/readiness-reconcile.js'
import { GH_PIN_CODE, createPinnedRunner, isReadOnlyPass, preflightGh, prepareIsolatedGhConfig, resolveSweepToken } from '../src/gh-pinned.js'

const POLICY_LIMIT = 8 * 1024
const POLICY_CODE = 'READINESS_SWEEP_POLICY'

function requireValue(condition, message) {
  if (!condition) {
    const error = new Error(message)
    error.code = POLICY_CODE
    throw error
  }
}

function fail(message) {
  const error = new Error(message)
  error.code = POLICY_CODE
  throw error
}

// Private file read mirroring consumer-config.js: absolute path, no
// symlinks, owner-uid, no group/other permission bits, size cap. JSON,
// open and decoder errors can contain private contents, so every failure
// maps to one opaque message.
async function readPrivateJson(path) {
  let file
  try {
    requireValue(typeof path === 'string' && path.startsWith('/') && !path.includes('\0'),
      'readiness sweep policy path is invalid')
    file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const info = await file.stat()
    requireValue(info.isFile() && info.uid === process.getuid() &&
      (info.mode & 0o077) === 0 && info.size <= POLICY_LIMIT,
    'readiness sweep policy file is invalid')
    const buffer = Buffer.alloc(POLICY_LIMIT + 1)
    let length = 0
    while (length < buffer.length) {
      const { bytesRead } = await file.read(buffer, length, buffer.length - length, null)
      if (!bytesRead) break
      length += bytesRead
    }
    requireValue(length <= POLICY_LIMIT, 'readiness sweep policy file is invalid')
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, length)))
    requireValue(value && typeof value === 'object' && !Array.isArray(value),
      'readiness sweep policy is invalid')
    return value
  } catch (error) {
    if (error?.code === POLICY_CODE) throw error
    fail('readiness sweep policy is unreadable')
  } finally {
    if (file) await file.close()
  }
}

// Strict policy shape. Unknown keys are rejected: a misspelled
// enabledRepositories that silently parsed would leave the gate flag-off
// while the Operator believes it is live (or vice versa).
export function parseSweepPolicy(value) {
  requireValue(value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).every((key) => ['version', 'ghExecutable', 'enabledRepositories', 'scanRepositories', 'holdLabels'].includes(key)) &&
    value.version === 1, 'readiness sweep policy is invalid')
  // The gh pin is mandatory with no PATH fallback: an omitted executable
  // must fail closed at parse, not resolve ambiently at runtime.
  const ghExecutable = value.ghExecutable ?? fail('readiness sweep policy is invalid')
  requireValue(typeof ghExecutable === 'string' && ghExecutable.startsWith('/') &&
    !ghExecutable.includes('\0'), 'readiness sweep policy is invalid')
  const enabledRepositories = value.enabledRepositories ?? fail('readiness sweep policy is invalid')
  const scanRepositories = value.scanRepositories ?? [...enabledRepositories]
  const holdLabels = value.holdLabels ?? ['hold']
  // Repository-name validation lives in readinessPolicy: one validator, no
  // drift. Its message is remapped to the opaque policy error: the raw text
  // can echo the offending value. The scan list is validated by the same
  // rule through a validation-only call, so a garbage scan name fails at
  // parse (exit 2) instead of surfacing as a run error (exit 1).
  const validated = validatePolicyList(enabledRepositories, holdLabels)
  validatePolicyList(scanRepositories, holdLabels)
  const scan = new Set(scanRepositories)
  requireValue(validated.enabledRepositories.every((r) => scan.has(r)),
    'readiness sweep policy enables a repository it does not scan')
  return {
    version: 1,
    ghExecutable,
    enabledRepositories: [...validated.enabledRepositories],
    scanRepositories: [...scan],
    holdLabels: [...validated.holdLabels],
  }
}

function validatePolicyList(repositories, holdLabels) {
  try {
    return readinessPolicy({ enabledRepositories: repositories, holdLabels })
  } catch {
    fail('readiness sweep policy is invalid')
  }
}

export async function loadSweepPolicy(path) {
  return parseSweepPolicy(await readPrivateJson(path))
}

export function recordLine(record) {
  const detail = record.decision === 'acted' || record.decision === 'error'
    ? (record.actions ?? []).join('+')
    : (record.reason ?? 'n/a')
  return `readiness ${record.repository}#${record.number}@${record.headSha}: ${record.decision} (${detail})`
}

// One supervised pass. `github` is injectable only so tests can run without
// credentials or network; production always builds the real adapter over the
// pinned runner, whose allowlist is exactly the scanned set. `env` is
// injectable so tests can drive the token posture and the read-only guard
// without mutating process state. `exec` is injectable only so tests can prove
// the production token wiring without network: with a recording `exec` double
// and no `github` double, the production createPinnedRunner line below
// executes and the test asserts the admitted token reached the child —
// dropping the jobToken argument fails that test (the github-double tests
// never execute this line). `onRecord` streams each record the moment
// its PR is reconciled, so a pass the supervisor SIGKILLs at the deadline
// loses at most the in-flight PR (writes are idempotent; only unflushed
// lines would be lost). `config` is injectable so tests can prove the
// prepare/remove lifecycle through a real pass without touching the
// filesystem: with fakes injected, preparation runs even for doubles, and
// removal in `finally` is what the lifecycle test asserts — deleting the
// `finally` fails that test. Production always uses the defaults (real
// per-pass directory, real removal, real exec).
export async function runSweep({ policyPath, github = null, nowMs = Date.now(), env = process.env, onRecord = null, config = null, exec = undefined } = {}) {
  const prepare = config?.prepare ?? prepareIsolatedGhConfig
  const remove = config?.remove ?? removeIsolatedGhConfig
  const jobToken = resolveSweepToken(env)
  const policy = await loadSweepPolicy(policyPath)
  // Flag-off is a clean no-op: an empty scan exits 0 having touched nothing
  // — in particular it never constructs the adapter (which requires a
  // non-empty allowlist) and never executes the preflight.
  if (policy.scanRepositories.length === 0) {
    return { records: [], counters: emptyCounters(), listErrors: 0 }
  }
  // One isolated, empty GH_CONFIG_DIR per pass: the child can never resolve a
  // stored personal `gh` login. Prepared only for real passes by default —
  // injected doubles (tests) never touch the filesystem or the network unless
  // they inject `config` fakes. Removed in `finally` on every settled path
  // (success or error) so host tmp does not accumulate ~288 empty dirs/day;
  // only a supervisor SIGKILL at the deadline can strand one, and host tmp
  // reaping owns those.
  const configDir = github && !config ? null : await prepare()
  try {
    const runner = github ? null : createPinnedRunner({ executable: policy.ghExecutable, configDir, jobToken, exec })
    const adapter = github ?? createGithubAdapter({ allowedRepositories: policy.scanRepositories, run: runner })
    if (!github) await preflightGh(runner)
    // Fork-context guard: a read-only pass keeps its scan (reads still prove
    // the wiring) but reconciles with an empty enabled set, so every decision
    // lands as would-act instead of erroring per PR against a read-only
    // token. The flag is set only by the sweep workflow (see header).
    const readOnly = isReadOnlyPass(env)
    const reconciler = createReconciler({
      github: adapter,
      policy: { enabledRepositories: readOnly ? [] : policy.enabledRepositories, holdLabels: policy.holdLabels },
      nowMs: () => nowMs,
    })
    const records = []
    const counters = emptyCounters()
    let listErrors = 0
    for (const repository of policy.scanRepositories) {
      let references
      try {
        references = await adapter.listOpenPullRequests(repository)
      } catch {
        // One unreadable repo must not blind the rest of the scan: count it
        // loudly and continue. The exit code carries it.
        listErrors++
        continue
      }
      // Per-PR passes: runReconcile holds no cross-PR state, so reconciling
      // one reference at a time changes nothing except observability — each
      // record streams before the next PR is touched.
      for (const ref of references) {
        const result = await reconciler.runReconcile({ references: [ref], nowMs })
        for (const key of Object.keys(counters)) counters[key] += result.counters[key]
        for (const record of result.records) {
          records.push(record)
          if (onRecord) onRecord(record)
        }
      }
    }
    return { records, counters, listErrors }
  } finally {
    if (configDir) await remove(configDir)
  }
}

// Removal primitive for the per-pass config dir, exported so tests can prove
// the prepare/remove roundtrip leaves nothing behind. Recursive + force: the
// dir is verified empty at creation, so anything inside it afterwards is the
// child's own doing and still must not outlive the pass.
export async function removeIsolatedGhConfig(dir) {
  await rm(dir, { recursive: true, force: true })
}

export function summaryLine(counters, listErrors) {
  return 'readiness sweep done: ' +
    `total=${counters.total} evaluated=${counters.evaluated} acted=${counters.acted} ` +
    `wouldAct=${counters.wouldAct} noOp=${counters.noOp} errors=${counters.errors} ` +
    `listErrors=${listErrors} updatedBody=${counters.updatedBody} ` +
    `markedReady=${counters.markedReady} convertedToDraft=${counters.convertedToDraft} ` +
    `enabledAutomerge=${counters.enabledAutomerge}`
}

// Returns the process exit code instead of exiting, so tests can drive every
// path without a subprocess.
export async function main(argv = process.argv.slice(2), deps = {}) {
  const { sweep = runSweep, out = console.log, err = console.error } = deps
  if (argv.length !== 1 || typeof argv[0] !== 'string') {
    err('usage: readiness-sweep.mjs <absolute-policy-path>')
    return 2
  }
  try {
    const { records, counters, listErrors } = await sweep({
      policyPath: argv[0], onRecord: (record) => out(recordLine(record)),
    })
    void records
    out(summaryLine(counters, listErrors))
    return counters.errors + listErrors > 0 ? 1 : 0
  } catch (error) {
    err(`readiness sweep failed: ${error?.message ?? error}`)
    return error?.code === POLICY_CODE || error?.code === GH_PIN_CODE ? 2 : 1
  }
}

const isMain = process.argv[1] === fileURLToPath(import.meta.url)
if (isMain) {
  main().then(
    (code) => process.exit(code),
    () => process.exit(1),
  )
}
