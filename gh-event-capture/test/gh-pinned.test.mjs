// Pinned gh runner offline suite (review R1). No network, no credentials:
// the child process is a stub recording its executable and arguments.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { GH_PIN_CODE, PINNED_CHILD_ENV, assertIsolatedConfigDir, createPinnedRunner, preflightGh, prepareIsolatedGhConfig, prepareIsolatedGhConfigSync, refuseInheritedTokens, scrubbedEnv } from '../src/gh-pinned.js'

const PIN = '/usr/bin/gh'

// One isolated config dir per test, removed afterwards. Production prepares
// one per pass via prepareIsolatedGhConfig; tests use the sync variant.
function isolatedDir(t) {
  const dir = prepareIsolatedGhConfigSync()
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

function stubExec(calls, { fail = null } = {}) {
  return async (executable, args, options) => {
    calls.push({ executable, args, options })
    if (fail) {
      const error = new Error('child failed')
      error.code = fail
      throw error
    }
    return { stdout: 'ok' }
  }
}

test('relative, empty and NUL paths are refused at construction', () => {
  for (const bad of ['gh', '', '/usr/bin/gh\0x', null, undefined]) {
    assert.throws(() => createPinnedRunner({ executable: bad }), /pinned gh executable is invalid/)
  }
})

test('the file argument never redirects execution back to PATH', async (t) => {
  const calls = []
  const run = createPinnedRunner({ executable: PIN, exec: stubExec(calls), configDir: isolatedDir(t) })
  await run('evil-from-path', ['api', 'user'])
  assert.equal(calls.length, 1)
  assert.equal(calls[0].executable, PIN)
  assert.deepEqual(calls[0].args, ['api', 'user'])
})

test('the shell stays off and non-string args are refused', async (t) => {
  const calls = []
  const run = createPinnedRunner({ executable: PIN, exec: stubExec(calls), configDir: isolatedDir(t) })
  await run('gh', ['auth', 'status'], { shell: true })
  assert.equal(calls[0].options.shell, false)
  await assert.rejects(run('gh', ['ok', 42]), /arguments are invalid/)
  await assert.rejects(run('gh', 'not-an-array'), /arguments are invalid/)
})

test('child failures map to the opaque pin code, never child output', async (t) => {
  const run = createPinnedRunner({ executable: PIN, exec: stubExec([], { fail: 1 }), configDir: isolatedDir(t) })
  try {
    await run('gh', ['auth', 'status'])
    assert.fail('must throw')
  } catch (error) {
    assert.equal(error.code, GH_PIN_CODE)
    assert.match(error.message, /pinned gh invocation failed: 1/)
  }
})

test('preflight passes a healthy pin and fails closed otherwise', async (t) => {
  const calls = []
  await preflightGh(createPinnedRunner({ executable: PIN, exec: stubExec(calls), configDir: isolatedDir(t) }))
  assert.deepEqual(calls[0].args, ['auth', 'status'])
  const failing = createPinnedRunner({ executable: PIN, exec: stubExec([], { fail: 'ENOENT' }), configDir: isolatedDir(t) })
  await assert.rejects(preflightGh(failing), /credential is missing or unusable/)
})

test('the child inherits nothing: exact minimal env, no HOME or tokens', async (t) => {
  const calls = []
  const dir = isolatedDir(t)
  const run = createPinnedRunner({ executable: PIN, exec: stubExec(calls), configDir: dir })
  await run('gh', ['auth', 'status'])
  assert.deepEqual(calls[0].options.env, { ...PINNED_CHILD_ENV, GH_CONFIG_DIR: dir })
  assert.ok(!('HOME' in calls[0].options.env), 'stored logins are unreachable')
  assert.ok(!('GH_TOKEN' in calls[0].options.env || 'GITHUB_TOKEN' in calls[0].options.env))
  assert.ok(!('XDG_CONFIG_HOME' in calls[0].options.env), 'no alternate config root')
  assert.ok(Object.values(calls[0].options.env).every((v) => !v.includes(' ')), 'no smuggled assignments')
})

test('explicit env override is copied, garbage env is refused', async (t) => {
  const calls = []
  const dir = isolatedDir(t)
  const run = createPinnedRunner({ executable: PIN, exec: stubExec(calls), env: { PATH: '/x', LANG: 'C' }, configDir: dir })
  await run('gh', ['auth', 'status'])
  assert.deepEqual(calls[0].options.env, { PATH: '/x', LANG: 'C', GH_CONFIG_DIR: dir })
  for (const bad of [null, [], { PATH: 42 }, { PATH: '/x\0y' }]) {
    assert.throws(() => createPinnedRunner({ executable: PIN, env: bad, configDir: dir }), /child environment is invalid/)
  }
  // Login-steering keys are refused even by explicit injection: the isolated
  // GH_CONFIG_DIR is the only config root the child ever sees.
  for (const key of ['HOME', 'XDG_CONFIG_HOME', 'GH_CONFIG_DIR', 'GH_TOKEN', 'GITHUB_TOKEN']) {
    assert.throws(() => createPinnedRunner({ executable: PIN, env: { PATH: '/x', [key]: '/y' }, configDir: dir }),
      /child environment is invalid/, `${key} must not reach the child`)
  }
  assert.deepEqual(scrubbedEnv(), PINNED_CHILD_ENV)
})

test('a missing, relative, non-empty or non-directory config is refused', async (t) => {
  const dir = isolatedDir(t)
  for (const bad of [undefined, null, '', 'relative/dir', '/usr/bin/gh\0x', '/nonexistent/readiness-gh-dir']) {
    assert.throws(() => createPinnedRunner({ executable: PIN, configDir: bad }), /config directory is invalid/)
  }
  assert.throws(() => createPinnedRunner({ executable: PIN, configDir: '/usr/bin/gh' }), /config directory is invalid/)
  writeFileSync(join(dir, 'hosts.yml'), 'oauth_token: personal-login\n')
  assert.throws(() => createPinnedRunner({ executable: PIN, configDir: dir }), /not empty/)
})

test('prepared config dirs exist, are empty, and differ per pass', async (t) => {
  const first = await prepareIsolatedGhConfig()
  t.after(() => rmSync(first, { recursive: true, force: true }))
  assertIsolatedConfigDir(first)
  const second = await prepareIsolatedGhConfig()
  t.after(() => rmSync(second, { recursive: true, force: true }))
  assert.ok(first !== second, 'each pass gets its own directory')
  assert.throws(() => assertIsolatedConfigDir('/nonexistent/readiness-gh-dir'), /invalid/)
})

test('inherited token env is refused; clean env passes', () => {
  assert.throws(() => refuseInheritedTokens({ GH_TOKEN: 'x' }), /refused/)
  assert.throws(() => refuseInheritedTokens({ GITHUB_TOKEN: 'x' }), /refused/)
  refuseInheritedTokens({})
  refuseInheritedTokens({ GH_TOKEN: '', GITHUB_TOKEN: '' })
})

// Writer diagnostics (PR #169 review): the pinned runner used to drop every
// byte of child output, so the adapter's write classifier only ever saw the
// opaque pin message and every production write failure logged
// writeFailure=...:unknown. The runner now keeps two fixed-shape facts — the
// `(HTTP NNN)` status gh prints, and the GraphQL errors[].type GitHub returns
// with HTTP 200 for permission refusals — and still nothing else.
const GRAPHQL_FORBIDDEN = JSON.stringify({ data: { markPullRequestReadyForReview: null },
  errors: [{ type: 'FORBIDDEN', path: ['markPullRequestReadyForReview'], extensions: { saml_failure: false },
    locations: [{ line: 1, column: 58 }], message: 'Resource not accessible by integration' }] })

// A real executable driven through the default execFile path: the exact
// production runner, only the binary is a stub.
function fakeGh(t, script) {
  const dir = mkdtempSync(join(tmpdir(), 'gh-pinned-fake-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const path = join(dir, 'gh')
  writeFileSync(path, `#!/bin/sh\n${script}\n`)
  chmodSync(path, 0o700)
  return path
}

test('real child failures keep only the sanitized HTTP status and GraphQL type', async (t) => {
  const cases = [
    ['echo "gh: Not Found (HTTP 404) SECRET-ghs_abc" >&2; exit 1', { httpStatus: '404', graphqlErrorType: undefined }],
    [`printf '%s' '${GRAPHQL_FORBIDDEN}'; echo "gh: Resource not accessible by integration SECRET-ghs_abc" >&2; exit 1`,
      { httpStatus: undefined, graphqlErrorType: 'FORBIDDEN' }],
    ['echo "gh: Resource not accessible by integration SECRET-ghs_abc" >&2; exit 1',
      { httpStatus: undefined, graphqlErrorType: 'FORBIDDEN' }],
    ['echo "SECRET-ghs_abc transport" >&2; exit 1', { httpStatus: undefined, graphqlErrorType: undefined }],
  ]
  for (const [script, expected] of cases) {
    const run = createPinnedRunner({ executable: fakeGh(t, script), configDir: isolatedDir(t) })
    await assert.rejects(run('gh', ['api', 'graphql']), (error) => {
      assert.equal(error.code, GH_PIN_CODE)
      assert.match(error.message, /^pinned gh invocation failed: 1$/)
      assert.equal(error.httpStatus, expected.httpStatus, script)
      assert.equal(error.graphqlErrorType, expected.graphqlErrorType, script)
      assert.equal(Object.hasOwn(error, 'stdout'), false)
      assert.equal(Object.hasOwn(error, 'stderr'), false)
      assert.equal(JSON.stringify({ ...error, message: error.message }).includes('SECRET'), false)
      assert.equal(String(error.stack).includes('SECRET'), false)
      return true
    })
  }
})

test('child facts outside the fixed shapes are dropped', async (t) => {
  for (const [stdout, stderr] of [
    [JSON.stringify({ errors: [{ type: 'forbidden; rm -rf', message: 'x' }] }), ''],
    [JSON.stringify({ errors: [{ type: 'A'.repeat(64), message: 'x' }] }), ''],
    ['not json', 'gh: (HTTP 999)'],
  ]) {
    const exec = async () => { throw Object.assign(new Error('child failed'), { code: 1, stdout, stderr }) }
    const run = createPinnedRunner({ executable: PIN, exec, configDir: isolatedDir(t) })
    await assert.rejects(run('gh', ['api', 'graphql']), (error) => {
      assert.equal(error.httpStatus, undefined)
      assert.equal(error.graphqlErrorType, undefined)
      return true
    })
  }
})
