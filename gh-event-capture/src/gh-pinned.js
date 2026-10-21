// Pinned gh execution for the readiness sweep (review R1; the credential
// route runs as an event-driven Actions job — see the sweep workflow).
//
// A bare `execFile('gh', ...)` resolves through PATH: whoever controls PATH
// controls the binary that performs merge mutations (mark-ready, draft,
// body, auto-merge). This module is the only approved way for the readiness
// sweep to reach GitHub: an absolute, pinned executable, no shell, an
// explicit minimal child environment (nothing inherited, so no stored
// interactive login can steer it), and a startup `auth status` preflight
// that fails closed when no credential is present.
//
// Credential route: the sweep runs as an event-driven Actions job whose ONLY
// credential is the ephemeral per-job GITHUB_TOKEN. The workflow sets the
// job-token flag and GH_TOKEN; resolveSweepToken admits exactly that
// combination and refuses everything else — in particular an ambient token
// without the flag (the old host posture) still fails closed, and two token
// sources at once fail closed as ambiguous. The token reaches the child ONLY
// through the explicit `jobToken` runner parameter, never through the base
// environment. The retired host-PEM wrapper path must not be reintroduced:
// no PEM, no mint, no key custody anywhere in this route.

import { execFile } from 'node:child_process'
import { mkdtempSync, readdirSync, statSync } from 'node:fs'
import { mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { promisify } from 'node:util'

export const GH_PIN_CODE = 'READINESS_GH_PIN'

export function pinError(message) {
  const error = new Error(message)
  error.code = GH_PIN_CODE
  return error
}

export function requirePinnedPath(path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.includes('\0')) {
    throw pinError('pinned gh executable is invalid')
  }
}

// The child environment is explicit and minimal: nothing is inherited. In
// particular HOME, XDG_*, GH_* and GITHUB_* never reach the child, so the
// pinned binary can never pick up whatever interactive login happens to be
// stored on the machine. Authentication flows exclusively through the
// explicit `jobToken` runner parameter (the workflow's ephemeral job token,
// admitted by resolveSweepToken below) — never through the base environment,
// which still refuses every token key. Without a job token the preflight
// below fails closed. `env` is injectable so tests can pin the exact child
// environment.
export const PINNED_CHILD_ENV = { PATH: '/usr/local/bin:/usr/bin:/bin', LANG: 'C.UTF-8' }

// Keys that must never appear in the child environment, even by explicit
// injection: HOME/XDG_CONFIG_HOME/GH_CONFIG_DIR would let the child resolve a
// stored personal `gh` login, and token keys would steer auth. GH_CONFIG_DIR
// is set by createPinnedRunner itself from the isolated directory below.
const REFUSED_CHILD_KEYS = ['HOME', 'XDG_CONFIG_HOME', 'GH_CONFIG_DIR', 'GH_TOKEN', 'GITHUB_TOKEN']

export function scrubbedEnv(env = PINNED_CHILD_ENV) {
  if (!env || typeof env !== 'object' || Array.isArray(env) ||
    Object.values(env).some((v) => typeof v !== 'string' || v.includes('\0'))) {
    throw pinError('pinned child environment is invalid')
  }
  if (REFUSED_CHILD_KEYS.some((key) => key in env)) {
    throw pinError('pinned child environment is invalid')
  }
  return { ...env }
}

// One empty config directory per pass, so the child can never read a stored
// personal `gh` login (default ~/.config/gh, keyring-adjacent hosts.yml).
// Auth arrives only as the GH_TOKEN child-env value below, so no persisted
// auth is ever needed inside the directory. mkdtemp creates it 0700; the
// emptiness check fails closed on a reused or pre-seeded path.
export function assertIsolatedConfigDir(dir) {
  if (typeof dir !== 'string' || !dir.startsWith('/') || dir.includes('\0')) {
    throw pinError('pinned gh config directory is invalid')
  }
  let stat
  try {
    stat = statSync(dir)
  } catch {
    throw pinError('pinned gh config directory is invalid')
  }
  if (!stat.isDirectory()) {
    throw pinError('pinned gh config directory is invalid')
  }
  let entries
  try {
    entries = readdirSync(dir)
  } catch {
    throw pinError('pinned gh config directory is invalid')
  }
  if (entries.length > 0) {
    throw pinError('pinned gh config directory is not empty')
  }
}

export async function prepareIsolatedGhConfig() {
  const dir = await mkdtemp(join(tmpdir(), 'readiness-gh-'))
  assertIsolatedConfigDir(dir)
  return dir
}

export function prepareIsolatedGhConfigSync() {
  const dir = mkdtempSync(join(tmpdir(), 'readiness-gh-'))
  assertIsolatedConfigDir(dir)
  return dir
}

// `run` with the adapter's (file, args, options) shape: the file argument is
// accepted and ignored — the pinned path is always used, so a caller can
// never redirect execution back to PATH. The shell is forced off even if a
// caller passes shell:true. `configDir` is mandatory with no default: without
// an isolated GH_CONFIG_DIR the child could resolve a stored personal login,
// so construction fails closed instead of running exposed. `jobToken` is the
// ONLY channel by which a token enters the child environment: it is added
// after the base env is scrubbed (which still refuses every token key), so a
// token can never arrive ambiently — only through this explicit parameter,
// fed by resolveSweepToken.
export function createPinnedRunner({ executable, exec = promisify(execFile), env = PINNED_CHILD_ENV, configDir, jobToken } = {}) {
  requirePinnedPath(executable)
  assertIsolatedConfigDir(configDir)
  if (jobToken !== undefined && (typeof jobToken !== 'string' || jobToken.length === 0 || jobToken.includes('\0'))) {
    throw pinError('pinned gh job token is invalid')
  }
  const childEnv = { ...scrubbedEnv(env), GH_CONFIG_DIR: configDir,
    ...(jobToken === undefined ? {} : { GH_TOKEN: jobToken }) }
  return async function run(_file, args, options = {}) {
    if (!Array.isArray(args) || args.some((a) => typeof a !== 'string' || a.includes('\0'))) {
      throw pinError('pinned gh arguments are invalid')
    }
    const { encoding = 'utf8', timeout = 30000, maxBuffer = 4 * 1024 * 1024 } = options ?? {}
    try {
      return await exec(executable, args, { encoding, shell: false, timeout, maxBuffer, env: { ...childEnv } })
    } catch (error) {
      // Child output can carry token material: only the short code travels.
      throw pinError(`pinned gh invocation failed: ${String(error?.code ?? 'unknown')}`)
    }
  }
}

// Startup credential preflight: proves the pinned gh can authenticate before
// any pass reads or writes. `auth status` is read-only and side-effect free;
// it never prints a token.
export async function preflightGh(run) {
  try {
    await run('gh', ['auth', 'status'], { timeout: 30000, maxBuffer: 1024 * 1024 })
  } catch {
    throw pinError('pinned gh credential is missing or unusable')
  }
}

// Inherited token env must never steer a pass that did not explicitly opt
// into the job-token route: without the workflow's flag, an ambient token is
// a foreign identity and the pass fails closed. Env is a parameter so tests
// can drive both paths without mutating process state.
export function refuseInheritedTokens(env = process.env) {
  if (env?.GH_TOKEN || env?.GITHUB_TOKEN) {
    throw pinError('inherited token environment is refused')
  }
}

// Resolve the sweep's credential posture from the process environment.
// Returns the job token for the runner, or undefined when no credential was
// admitted (the `auth status` preflight then fails closed as before).
//
// The workflow opts in with the exact flag READINESS_JOB_TOKEN=1 (any other
// value, including a typo, leaves the route off) and supplies exactly one
// token source, GH_TOKEN. Two sources at once fail closed as ambiguous: a
// GITHUB_TOKEN beside the job GH_TOKEN would silently choose an identity.
// Without the flag, both token keys are refused exactly as before, so the
// retired host posture (ambient login, no flag) can never authenticate.
export function resolveSweepToken(env = process.env) {
  if (env?.READINESS_JOB_TOKEN !== '1') {
    refuseInheritedTokens(env)
    return undefined
  }
  const token = env?.GH_TOKEN
  if (typeof token !== 'string' || token.length === 0 || token.includes('\0')) {
    throw pinError('job token environment is missing or invalid')
  }
  if (env?.GITHUB_TOKEN) {
    throw pinError('job token environment is ambiguous')
  }
  return token
}

// Fork-context guard, set ONLY by the sweep workflow when the triggering
// event is a fork pull_request (whose job token GitHub already limits to
// read-only): the pass degrades to would-act everywhere instead of erroring
// per PR. Exact '1' match, like the job-token flag above.
export function isReadOnlyPass(env = process.env) {
  return env?.READINESS_READ_ONLY === '1'
}
