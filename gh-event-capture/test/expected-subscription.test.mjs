// Default subscription fixture for check-subscription.sh.
// The drift script defaults EXPECTED_SUBSCRIPTION to
// gh-event-capture/expected-subscription.json; without this file the default
// path exits 3 ("cannot read"). This pins the file's shape (event set +
// hook_url key) and its scrub (no tracker ids, no secrets, no private names).

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const raw = readFileSync(join(root, 'expected-subscription.json'), 'utf8')
const doc = JSON.parse(raw)

test('event set matches the six scoped events plus installation_repositories', () => {
  assert.deepEqual(
    [...doc.events].sort(),
    ['installation', 'installation_repositories', 'member', 'membership', 'organization', 'push', 'repository'].sort(),
  )
})

test('hook_url key is present (empty until the webhook is registered)', () => {
  assert.ok('hook_url' in doc, 'hook_url key must exist')
  assert.ok(doc.hook_url === '' || String(doc.hook_url).startsWith('https://'))
})

test('fixture carries no tracker ids, secrets, or private names', () => {
  for (const pat of [/TOG-\d+/, /ghp_[A-Za-z0-9]+/, /BEGIN [A-Z ]*PRIVATE KEY/, /paperclip-ops-tooling/, /135\.148\.\d+\.\d+/]) {
    assert.ok(!pat.test(raw), `scrub leak: ${pat}`)
  }
})
