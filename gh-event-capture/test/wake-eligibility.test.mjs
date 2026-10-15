import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isMergeConfirmationFor, wakeEligible, hasMergeConfirmation,
  shouldPostMergedFallback, mergeConfirmationReferences } from '../src/wake-eligibility.js'

const repo = 'TogetherWeOwn/paperclip-ops'
const pr = { repository: repo, number: 42 }
// Every word below is inside the server's merge-only vocabulary (or a PR
// reference): S5b's opener must stay inside it too, or the tolerance stops
// recognising the confirmation and every exception wake goes ineligible.
const confirmation = (overrides = {}) => ({
  kind: 'request_confirmation',
  status: 'pending',
  title: `Please confirm that TogetherWeOwn/paperclip-ops#42 is merged`,
  summary: `Confirm that ${repo}#42 is merged`,
  payload: {
    prompt: `Please confirm the PR https://github.com/${repo}/pull/42 is merged and green`,
    acceptLabel: 'Confirm merge',
    continuationPolicy: 'wake_assignee_on_accept',
  },
  ...overrides,
})
const issue = (overrides = {}) => ({
  id: 'issue-1', identifier: 'case_8c5fe4cc', status: 'in_progress',
  assigneeAgentId: 'engineer', assigneeUserId: null, blockedBy: [], ...overrides,
})

test('a pending merge confirmation names this PR and nothing else', () => {
  const refs = mergeConfirmationReferences(confirmation())
  assert.deepEqual(refs, [{ host: 'github.com', owner: 'TogetherWeOwn', repo: 'paperclip-ops', number: 42 }])
  assert.equal(isMergeConfirmationFor(confirmation(), pr), true)
})

test('references match case-insensitively and across URL/shorthand forms', () => {
  const upper = confirmation({ summary: `Confirm that TOGETHERWEOWN/PAPERCLIP-OPS-TOOLING#42 is merged` })
  assert.equal(isMergeConfirmationFor(upper, pr), true)
  const otherPr = confirmation({ title: `Please confirm that ${repo}#43 is merged`,
    summary: `Confirm that ${repo}#43 is merged`,
    payload: { prompt: `Please confirm the PR https://github.com/${repo}/pull/43 is merged and green` } })
  assert.equal(isMergeConfirmationFor(otherPr, pr), false)
  assert.equal(isMergeConfirmationFor(confirmation(), { repository: repo, number: 43 }), false)
})

test('non-merge vocabulary, tool actions and document targets are never confirmations', () => {
  // A per-exception-class confirmation is not natural: extra actions fail the
  // merge-only test, so the atomic transport (not the built-in path) serves
  // the exception classes.
  const extraAction = confirmation({ payload: { prompt: `Please confirm the PR https://github.com/${repo}/pull/42 is merged and deploy to prod` } })
  assert.deepEqual(mergeConfirmationReferences(extraAction), [])
  const toolAction = confirmation({ payload: { prompt: 'Please confirm merge', toolAction: {} } })
  assert.deepEqual(mergeConfirmationReferences(toolAction), [])
  const secretProposal = confirmation({ payload: { prompt: 'Please confirm merge', secretProposal: {} } })
  assert.deepEqual(mergeConfirmationReferences(secretProposal), [])
  const documentTarget = confirmation({ payload: { prompt: 'Please confirm merge', target: { type: 'issue_document', key: 'plan' } } })
  assert.deepEqual(mergeConfirmationReferences(documentTarget), [])
  const question = { kind: 'ask_user_questions', status: 'pending', title: 'Please confirm merge' }
  assert.deepEqual(mergeConfirmationReferences(question), [])
  for (const bad of [extraAction, toolAction, documentTarget, question]) {
    assert.equal(isMergeConfirmationFor(bad, pr), false)
    assert.equal(wakeEligible(issue(), [bad], pr), false)
  }
})

test('only pending confirmations are tolerated; resolved ones do not suppress', () => {
  assert.equal(wakeEligible(issue(), [confirmation()], pr), true)
  for (const status of ['accepted', 'rejected', 'answered', 'cancelled', 'expired', 'failed']) {
    assert.equal(wakeEligible(issue(), [confirmation({ status })], pr), true)
  }
})

test('an unrelated pending question still suppresses the wake', () => {
  const pending = { kind: 'request_confirmation', status: 'pending', title: 'Please confirm the release notes' }
  assert.deepEqual(mergeConfirmationReferences(pending), [])
  assert.equal(wakeEligible(issue(), [confirmation(), pending], pr), false)
})

test('parked, blocked, terminal and unassigned tasks stay ineligible', () => {
  const eligible = [issue(), [confirmation()], pr]
  assert.equal(wakeEligible(...eligible), true)
  for (const change of [
    { status: 'blocked' }, { status: 'done' }, { status: 'cancelled' }, { status: 'backlog' },
    { assigneeAgentId: null }, { assigneeUserId: 'operator' },
    { blockedBy: [{ status: 'in_progress' }] },
  ]) {
    assert.equal(wakeEligible(issue(change), [confirmation()], pr), false)
  }
  assert.equal(wakeEligible(issue({ blockedBy: [{ status: 'done' }] }), [confirmation()], pr), true)
})

test('incomplete reads fail closed instead of waking', () => {
  for (const interactions of [[{}], [{ status: 'unknown' }], [null], { items: [] }]) {
    assert.throws(() => wakeEligible(issue(), interactions, pr), /incomplete/)
  }
  assert.throws(() => wakeEligible({}, [], pr), /blocker/)
  assert.throws(() => wakeEligible(issue(), [], null), /repository and PR number/)
  assert.throws(() => wakeEligible(issue(), [], { repository: 'bad', number: 42 }), /repository and PR number/)
  assert.throws(() => wakeEligible(issue(), [], { repository: repo, number: -1 }), /repository and PR number/)
})

test('exactly-one-merged-wake: fallback posts only with no live or delivered confirmation', () => {
  assert.equal(shouldPostMergedFallback([], pr), true)
  assert.equal(shouldPostMergedFallback([{ kind: 'ask_user_questions', status: 'pending' }], pr), true)
  // Expired/cancelled/rejected confirmations never delivered a wake.
  for (const status of ['expired', 'cancelled', 'rejected', 'failed']) {
    assert.equal(hasMergeConfirmation([confirmation({ status })], pr), false)
    assert.equal(shouldPostMergedFallback([confirmation({ status })], pr), true)
  }
  // Pending (the built-in sweep will fire) and accepted/answered (it fired).
  for (const status of ['pending', 'accepted', 'answered']) {
    assert.equal(hasMergeConfirmation([confirmation({ status })], pr), true)
    assert.equal(shouldPostMergedFallback([confirmation({ status })], pr), false)
  }
  assert.equal(hasMergeConfirmation([confirmation()], { repository: repo, number: 43 }), false)
})
