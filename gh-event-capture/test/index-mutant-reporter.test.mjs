import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { assertionKilled } from './index-mutant-reporter.mjs'

const failure = { type: 'test:fail', name: 'selected assertion', number: 1,
  failureType: 'testCodeFailure', errorName: 'AssertionError', code: 'ERR_ASSERTION' }
const result = (overrides = {}, report = failure) => ({ status: 1, signal: null,
  stdout: JSON.stringify(report) + '\n' + JSON.stringify({ type: 'report:complete' }) + '\n', ...overrides })

test('mutant classifier uses the selected structured assertion code', () => {
  assert.equal(assertionKilled(result(), 'selected assertion'), true)
  assert.equal(assertionKilled(result({}, { ...failure, code: 'ERR_TEST_FAILURE',
    diagnostic: 'AssertionError [ERR_ASSERTION]' }), 'selected assertion'), false)
  assert.equal(assertionKilled(result({}, { ...failure, code: null }), 'selected assertion'), false)
  assert.equal(assertionKilled(result({}, { ...failure, code: 'ERR_TEST_FAILURE',
    errorName: 'TypeError', diagnostic: 'AssertionError [ERR_ASSERTION]' }), 'selected assertion'), false)
  assert.equal(assertionKilled(result({ stdout: 'not ok 1 - selected assertion\nERR_ASSERTION' }), 'selected assertion'), false)
  assert.equal(assertionKilled(result({ stdout: '' }), 'selected assertion'), false)
  assert.equal(assertionKilled(result(), 'another selected test'), false)
  assert.equal(assertionKilled(result({}, { ...failure, number: 2 }), 'selected assertion'), false)
})

test('mutant classifier refuses signals, spawn failures and extra failures', () => {
  for (const overrides of [{ status: 0 }, { status: null }, { status: 13 }, { signal: 'SIGKILL' },
    { error: new Error('synthetic spawn failure') }, { stdout: JSON.stringify(failure) },
    { stdout: JSON.stringify(failure) + '\n' + result().stdout }]) {
    assert.equal(assertionKilled(result(overrides), 'selected assertion'), false)
  }
})

for (const kind of ['assertion', 'misleading runtime']) {
  test(`reporter preserves actual ${kind} failure identity`, () => {
    const name = `selected ${kind}`
    const env = { ...process.env }
    delete env.NODE_TEST_CONTEXT // This is an independent CLI, not the parent test worker.
    const child = spawnSync(process.execPath, ['--test',
      `--test-reporter=${fileURLToPath(new URL('./index-mutant-reporter.mjs', import.meta.url))}`,
      `--test-name-pattern=^${name}$`,
      fileURLToPath(new URL('./fixtures/index-mutant-classification-probe.mjs', import.meta.url))],
    { encoding: 'utf8', timeout: 10000, env })
    assert.equal(child.status, 1, child.stderr)
    assert.equal(child.signal, null)
    assert.equal(child.error, undefined)
    const reports = child.stdout.trim().split('\n').map((line) => JSON.parse(line))
    assert.equal(reports.length, 2)
    assert.equal(reports[1].type, 'report:complete')
    const report = reports[0]
    assert.equal(report.name, name)
    assert.equal(report.errorName, kind === 'assertion' ? 'AssertionError' : 'TypeError')
    assert.equal(report.code, kind === 'assertion' ? 'ERR_ASSERTION' : null)
    assert.equal(assertionKilled(child, name), kind === 'assertion')
  })
}
