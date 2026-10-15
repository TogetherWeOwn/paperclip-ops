// Offline suite for the protected-environment policy table.
//
// No credentials, no network, no clock. The table is code, so its shape is a
// contract: exact-match resolution, disjoint mode markers, and a startup
// validator that refuses a defective table. Assertions pin resolution
// behaviour and rejection — never message text.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { PROTECTED_ENVIRONMENTS, findPolicy, scopedRepositories, validatePolicies } from '../src/environments.js'

test('the shipped table validates', () => {
  assert.deepEqual(validatePolicies(PROTECTED_ENVIRONMENTS), PROTECTED_ENVIRONMENTS)
})

test('the shipped table serves staging-migrate-apply in plan mode', () => {
  const policy = findPolicy({ repository: 'TogetherWeOwn/two-bot-next', environment: 'staging-migrate-apply' })
  assert.equal(policy?.mode, 'plan')
  assert.equal(policy?.repository, 'TogetherWeOwn/two-bot-next')
})

test('the shipped table serves all three production environments in sha mode', () => {
  for (const [repository, goIssueId] of [
    ['TogetherWeOwn/kagi-shinobi', '00000000-0000-4000-8000-000000000134'],
    ['TogetherWeOwn/two-web-next', '00000000-0000-4000-8000-000000000135'],
    ['TogetherWeOwn/two-bot-next', '00000000-0000-4000-8000-000000000136'],
  ]) {
    const policy = findPolicy({ repository, environment: 'production' })
    assert.equal(policy?.mode, 'sha', repository)
    assert.equal(policy?.goIssueId, goIssueId, repository)
  }
})

test('exact match only: near-misses resolve to null', () => {
  assert.equal(findPolicy({ repository: 'TogetherWeOwn/two-bot-next', environment: 'staging-migrate-apply ' }), null)
  assert.equal(findPolicy({ repository: 'TogetherWeOwn/two-bot-next', environment: 'Production' }), null)
  assert.equal(findPolicy({ repository: 'togetherweown/two-bot-next', environment: 'production' }), null)
  assert.equal(findPolicy({ repository: null, environment: 'production' }), null)
  assert.equal(findPolicy({ repository: 'TogetherWeOwn/two-bot-next', environment: null }), null)
})

test('unserved environment on a known repo resolves to null', () => {
  assert.equal(findPolicy({ repository: 'TogetherWeOwn/two-bot-next', environment: 'staging' }), null)
})

test('scoped repositories cover all four table rows', () => {
  assert.deepEqual(
    [...scopedRepositories()].sort(),
    ['TogetherWeOwn/kagi-shinobi', 'TogetherWeOwn/two-bot-next', 'TogetherWeOwn/two-web-next'],
  )
})

test('validator refuses an empty table', () => {
  assert.throws(() => validatePolicies([]))
})

test('validator refuses a duplicate pair', () => {
  assert.throws(() => validatePolicies([
    { repository: 'A/b', environment: 'production', mode: 'sha', goIssueId: '00000000-0000-4000-8000-000000000136' },
    { repository: 'A/b', environment: 'production', mode: 'sha', goIssueId: '00000000-0000-4000-8000-000000000136' },
  ]))
})

test('validator refuses a bad mode, repo, or GO card', () => {
  const good = { repository: 'A/b', environment: 'production', mode: 'sha', goIssueId: '00000000-0000-4000-8000-000000000136' }
  assert.throws(() => validatePolicies([{ ...good, mode: 'plan-plus' }]))
  assert.throws(() => validatePolicies([{ ...good, repository: 'not a repo' }]))
  assert.throws(() => validatePolicies([{ ...good, goIssueId: 'not-a-uuid' }]))
  assert.throws(() => validatePolicies([{ ...good, environment: '' }]))
})

// Source evidence: plan mode must name the required CI and review checks and
// the App each is pinned to; sha mode must carry none.
const PLAN_ENTRY = {
  repository: 'A/b', environment: 'staging-migrate-apply', mode: 'plan', goIssueId: '00000000-0000-4000-8000-000000000133',
  sourceEvidence: {
    ci: { appId: 15368, checks: ['ci-ok', 'gitleaks', 'pr-lint'] },
    review: { appId: 5191093, check: 'Paperclip Review', score: '5/5' },
  },
}

test('the shipped plan entry pins CI and review to their Apps (live Bot main ruleset)', () => {
  const policy = findPolicy({ repository: 'TogetherWeOwn/two-bot-next', environment: 'staging-migrate-apply' })
  assert.deepEqual(policy.sourceEvidence, PLAN_ENTRY.sourceEvidence)
  for (const entry of PROTECTED_ENVIRONMENTS.filter((p) => p.mode === 'sha')) assert.equal(entry.sourceEvidence, undefined)
})

test('validator accepts a complete plan entry', () => {
  assert.deepEqual(validatePolicies([PLAN_ENTRY]), [PLAN_ENTRY])
})

test('validator refuses a plan entry without source evidence, or with any defect in it', () => {
  const withEvidence = (patch) => ({ ...PLAN_ENTRY, sourceEvidence: { ...PLAN_ENTRY.sourceEvidence, ...patch } })
  const { sourceEvidence: _omitted, ...bare } = PLAN_ENTRY
  assert.throws(() => validatePolicies([bare]))
  assert.throws(() => validatePolicies([{ ...PLAN_ENTRY, sourceEvidence: null }]))
  assert.throws(() => validatePolicies([{ ...PLAN_ENTRY, sourceEvidence: [] }]))
  assert.throws(() => validatePolicies([withEvidence({ ci: undefined })]))
  assert.throws(() => validatePolicies([withEvidence({ review: undefined })]))
  for (const appId of [0, -1, 1.5, '15368', null, 2 ** 60]) {
    assert.throws(() => validatePolicies([withEvidence({ ci: { appId, checks: ['ci-ok'] } })]), String(appId))
    assert.throws(() => validatePolicies([withEvidence({ review: { ...PLAN_ENTRY.sourceEvidence.review, appId } })]), String(appId))
  }
  for (const checks of [[], 'ci-ok', ['ci-ok', 'ci-ok'], ['ci ok\n'], [''], [7], Array.from({ length: 17 }, (_, i) => `c${i}`)]) {
    assert.throws(() => validatePolicies([withEvidence({ ci: { appId: 15368, checks } })]), JSON.stringify(checks))
  }
  for (const review of [{ appId: 5191093, check: '', score: '5/5' }, { appId: 5191093, check: 'Paperclip Review', score: 'five' },
    { appId: 5191093, check: 'Paperclip Review', score: '5/5/5' }, { appId: 5191093, check: 'Paperclip Review' }, { appId: 5191093, score: '5/5' }]) {
    assert.throws(() => validatePolicies([withEvidence({ review })]), JSON.stringify(review))
  }
  // The review check cannot double as a CI check: it would then be satisfied by the CI App.
  assert.throws(() => validatePolicies([withEvidence({ ci: { appId: 15368, checks: ['ci-ok', 'Paperclip Review'] } })]))
})

test('validator refuses source evidence on a sha-mode entry', () => {
  const sha = { repository: 'A/b', environment: 'production', mode: 'sha', goIssueId: '00000000-0000-4000-8000-000000000136' }
  assert.deepEqual(validatePolicies([sha]), [sha])
  assert.throws(() => validatePolicies([{ ...sha, sourceEvidence: PLAN_ENTRY.sourceEvidence }]))
})
