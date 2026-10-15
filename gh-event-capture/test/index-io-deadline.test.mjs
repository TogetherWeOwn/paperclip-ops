import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

for (const phase of ['save', 'load']) {
  test(`cache ${phase} deadline yields production and drains before unlock`, () => {
    const result = spawnSync(process.execPath, ['--experimental-vm-modules',
      fileURLToPath(new URL('./fixtures/index-io-probe.mjs', import.meta.url)), phase],
    { encoding: 'utf8', timeout: 10000 })
    if (result.status !== 0) {
      // A parser/runtime error, signal or timeout is not an assertion-verified
      // mutant kill. Preserve its failure without converting it to ERR_ASSERTION.
      if (result.signal !== null || !result.stderr.includes('AssertionError [ERR_ASSERTION]') ||
          /SyntaxError|ReferenceError|TypeError|ERR_MODULE|ENOENT/.test(result.stderr)) {
        throw new Error(`filesystem probe failed (${result.status ?? result.signal}): ${result.stderr}`)
      }
      assert.fail(result.stderr)
    }
    assert.match(result.stdout, /production ran before drainage; whole-pass stop preserved; lock released after settlement/)
  })
}
