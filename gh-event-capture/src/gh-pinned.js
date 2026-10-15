// Pinned gh execution for unattended host duties (case_4b213922, review R1).
//
// A bare `execFile('gh', ...)` resolves through PATH: whoever controls PATH
// controls the binary that performs merge mutations (mark-ready, draft,
// body, auto-merge). This module is the only approved way for the readiness
// sweep to reach GitHub: an absolute, operator-pinned executable, no shell,
// an explicit minimal child environment (nothing inherited, so no stored
// interactive login can steer it), inherited token env refused, and a
// startup `auth status` preflight that fails closed when no credential is
// present. The readiness-scoped App-token
// wrapper (write grants) is a follow-up card; until it lands, the pin plus
// preflight keep the duty on the Operator-provisioned gh and nothing else.

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
// stored on the machine. Authentication flows exclusively through the pinned
// executable itself — the Operator-installed wrapper, which mints
// per-invocation tokens from its own private files. Until that wrapper is
// installed, the preflight below fails closed. `env` is injectable so tests
// can pin the exact child environment.
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
// personal `gh` login (default ~/.config/gh, keyring-adjacent hosts.yml). The
// wrapper mints per-invocation tokens from its own private files, so no
// persisted auth is ever needed inside the directory. mkdtemp creates it
// 0700; the emptiness check fails closed on a reused or pre-seeded path.
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
// so construction fails closed instead of running exposed.
export function createPinnedRunner({ executable, exec = promisify(execFile), env = PINNED_CHILD_ENV, configDir } = {}) {
  requirePinnedPath(executable)
  assertIsolatedConfigDir(configDir)
  const childEnv = { ...scrubbedEnv(env), GH_CONFIG_DIR: configDir }
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

// Inherited token env must never steer an unattended pass: the unit strips
// GH_TOKEN/GITHUB_TOKEN, and the sweep refuses them as a second lock. Env is
// a parameter so tests can drive both paths without mutating process state.
export function refuseInheritedTokens(env = process.env) {
  if (env?.GH_TOKEN || env?.GITHUB_TOKEN) {
    throw pinError('inherited token environment is refused')
  }
}
