import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildMergeConfirmation, openMergeConfirmation } from '../src/merge-confirmation.js'
// S5b imports the S5a predicate; it never copies the vocabulary. If the
// server vocabulary drifts, these assertions fail here instead of silently
// opening confirmations the sweep would refuse.
import { isMergeConfirmationFor, mergeConfirmationReferences, wakeEligible } from '../src/wake-eligibility.js'

const repo = 'TogetherWeOwn/paperclip-ops'
const pr = { repository: repo, number: 42 }
const issue = (overrides = {}) => ({
  id: 'issue-1', identifier: 'case_8c5fe4cc', status: 'in_progress',
  assigneeAgentId: 'engineer', assigneeUserId: null, blockedBy: [], ...overrides,
})
// A created interaction carries no status until the server stores it pending.
const asPending = (envelope) => ({ ...envelope, status: 'pending' })

test('the generated confirmation passes the S5a merge-confirmation test for its PR', () => {
  const envelope = buildMergeConfirmation({ repository: repo, number: 42 })
  const refs = mergeConfirmationReferences(asPending(envelope))
  assert.deepEqual(refs, [{ host: 'github.com', owner: 'TogetherWeOwn', repo: 'paperclip-ops', number: 42 }])
  assert.equal(isMergeConfirmationFor(asPending(envelope), pr), true)
})

test('the generated confirmation keeps the exception-wake path eligible', () => {
  assert.equal(wakeEligible(issue(), [asPending(buildMergeConfirmation({ repository: repo, number: 42 }))], pr), true)
})

test('the envelope carries the S5b contract fields and nothing the predicate forbids', () => {
  const envelope = buildMergeConfirmation({ repository: repo, number: 42 })
  assert.equal(envelope.kind, 'request_confirmation')
  assert.equal(envelope.continuationPolicy, 'wake_assignee_on_accept')
  assert.equal(envelope.payload.version, 1)
  assert.equal(envelope.payload.supersedeOnUserComment, false)
  assert.ok(!('toolAction' in envelope.payload))
  assert.ok(!('secretProposal' in envelope.payload))
  assert.ok(!('target' in envelope.payload))
  // Pinned to the envelope keys the platform accepts
  // (interaction_envelope_lint.sh): anything else would be stripped silent.
  const accepted = new Set(['addresseeAgentId', 'continuationPolicy', 'idempotencyKey', 'kind',
    'payload', 'resolverPolicy', 'sourceCommentId', 'sourceRunId', 'summary', 'title'])
  for (const key of Object.keys(envelope)) assert.ok(accepted.has(key), `unexpected envelope key: ${key}`)
})

test('the title carries merge intent with the PR ref on its first line', () => {
  const envelope = buildMergeConfirmation({ repository: repo, number: 42 })
  const [first] = envelope.title.split('\n')
  assert.match(first, /is merged/)
  assert.match(first, /TogetherWeOwn\/paperclip-ops#42/)
  assert.equal(envelope.detailsMarkdown, undefined)
  assert.match(envelope.payload.detailsMarkdown, /https:\/\/github\.com\/TogetherWeOwn\/paperclip-ops\/pull\/42/)
})

test('mutant M1 (dropped PR reference) is killed: bare titles are not confirmations', () => {
  const bare = {
    ...buildMergeConfirmation({ repository: repo, number: 42 }),
    status: 'pending',
    title: 'Please confirm merge',
    summary: 'Confirm merge',
    payload: { version: 1, prompt: 'Please confirm merge', acceptLabel: 'Confirm merge' },
  }
  assert.deepEqual(mergeConfirmationReferences(bare), [])
  assert.equal(isMergeConfirmationFor(bare, pr), false)
})

test('mutant M2 (smuggled extra action) is killed: deploy prose is not merge-only', () => {
  const base = buildMergeConfirmation({ repository: repo, number: 42 })
  const smuggled = {
    ...base,
    status: 'pending',
    payload: { ...base.payload, prompt: `${base.payload.prompt} and deploy to prod` },
  }
  assert.deepEqual(mergeConfirmationReferences(smuggled), [])
  assert.equal(isMergeConfirmationFor(smuggled, pr), false)
})

test('mutant M3 (non-merge vocabulary) is killed: launch prose is not merge-only', () => {
  const base = buildMergeConfirmation({ repository: repo, number: 42 })
  const relaunched = {
    ...base,
    status: 'pending',
    payload: { ...base.payload, acceptLabel: 'Approve launch' },
  }
  assert.deepEqual(mergeConfirmationReferences(relaunched), [])
  assert.equal(isMergeConfirmationFor(relaunched, pr), false)
})

test('mutant M4 (wrong-PR reference) is killed: it names another PR', () => {
  const other = asPending(buildMergeConfirmation({ repository: repo, number: 43 }))
  assert.equal(mergeConfirmationReferences(other).length, 1)
  assert.equal(isMergeConfirmationFor(other, pr), false)
})

test('mutant M5 (intent off the first line) is killed: the anchor never crosses a newline', () => {
  const base = buildMergeConfirmation({ repository: repo, number: 42 })
  const buried = {
    ...base,
    status: 'pending',
    title: `Status update\nPlease confirm that ${repo}#42 is merged`,
  }
  assert.deepEqual(mergeConfirmationReferences(buried), [])
  assert.equal(isMergeConfirmationFor(buried, pr), false)
})

test('the builder refuses invalid PR context without emitting text', () => {
  for (const input of [
    null, {}, { repository: repo }, { number: 42 },
    { repository: '', number: 42 }, { repository: 'bad', number: 42 },
    { repository: 'a/b/c', number: 42 }, { repository: repo, number: 0 },
    { repository: repo, number: -1 }, { repository: repo, number: 1.5 },
    { repository: repo, number: '42' },
  ]) {
    assert.throws(() => buildMergeConfirmation(input), /merge confirmation needs/)
  }
})

test('the idempotency key is deterministic per PR', () => {
  const first = buildMergeConfirmation({ repository: repo, number: 42 })
  const retry = buildMergeConfirmation({ repository: repo, number: 42 })
  assert.equal(first.idempotencyKey, retry.idempotencyKey)
  assert.notEqual(first.idempotencyKey, buildMergeConfirmation({ repository: repo, number: 43 }).idempotencyKey)
})

test('open posts the built envelope to the issue interactions API', async () => {
  const calls = []
  const fetchImpl = async (url, options) => {
    calls.push({ url, options })
    return {
      status: 201, redirected: false,
      headers: { get: () => 'application/json' },
      json: async () => ({ id: '00000000-0000-4000-8000-000000000016', kind: 'request_confirmation', status: 'pending' }),
    }
  }
  const row = await openMergeConfirmation({
    issueId: '00000000-0000-4000-8000-000000000017',
    repository: repo, number: 42,
    baseUrl: 'https://paperclip.example.com/api', token: 'token-for-test',
    fetchImpl, timeoutMs: 1000,
  })
  assert.equal(row.kind, 'request_confirmation')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://paperclip.example.com/api/issues/00000000-0000-4000-8000-000000000017/interactions')
  assert.equal(calls[0].options.method, 'POST')
  assert.match(calls[0].options.headers.authorization, /^Bearer /)
  assert.deepEqual(JSON.parse(calls[0].options.body), buildMergeConfirmation({ repository: repo, number: 42 }))
})

test('open fails closed before any write on invalid input', async () => {
  let calls = 0
  const fetchImpl = async () => { calls += 1; throw new Error('must not be called') }
  for (const input of [
    { issueId: 'not-a-uuid', repository: repo, number: 42 },
    { issueId: '00000000-0000-4000-8000-000000000017', repository: 'bad', number: 42 },
    { issueId: '00000000-0000-4000-8000-000000000017', repository: repo, number: -1 },
  ]) {
    await assert.rejects(() => openMergeConfirmation({
      ...input, baseUrl: 'https://paperclip.example.com', token: 'token-for-test', fetchImpl,
    }), /merge confirmation needs|issue UUID/)
  }
  assert.equal(calls, 0)
})

test('open fails closed on transport and confirmation errors', async () => {
  const issueId = '00000000-0000-4000-8000-000000000017'
  const base = { issueId, repository: repo, number: 42, baseUrl: 'https://paperclip.example.com', token: 'token-for-test' }
  await assert.rejects(() => openMergeConfirmation({
    ...base, fetchImpl: async () => { throw new Error('down') },
  }), /merge confirmation post failed/)
  await assert.rejects(() => openMergeConfirmation({
    ...base, fetchImpl: async () => ({ status: 400, redirected: false, headers: { get: () => 'application/json' } }),
  }), /did not confirm/)
  await assert.rejects(() => openMergeConfirmation({
    ...base,
    fetchImpl: async () => ({
      status: 201, redirected: false,
      headers: { get: () => 'application/json' },
      json: async () => ({ id: 'x', kind: 'ask_user_questions' }),
    }),
  }), /did not confirm/)
})
