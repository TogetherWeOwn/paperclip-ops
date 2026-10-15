import test from 'node:test'
import assert from 'node:assert/strict'

test('selected assertion', () => { assert.fail('synthetic assertion') })
test('selected misleading runtime', () => { throw new TypeError('AssertionError [ERR_ASSERTION] is diagnostic text only') })
