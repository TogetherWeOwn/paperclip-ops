// Offline suite for the protection-rule verdict.
//
// No credentials, no network, no clock. `decide` is pure: every test pins a
// (state, reason) pair for one row of the decision table. Assertions pin the
// stable reason identifiers, never message text, which drifts.
//
// The mutations this suite must catch: any branch that turns a rejection into
// an approval, in either mode. Each rejection row below exists so that
// weakening it goes red.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { decide, EVENT, REASON } from '../src/decide.js'

const HEAD = 'a'.repeat(40)
const OTHER_HEAD = 'd'.repeat(40)
const HASH_A = 'b'.repeat(64)
const HASH_B = 'c'.repeat(64)

const PLAN_POLICY = { repository: 'TogetherWeOwn/two-bot-next', environment: 'staging-migrate-apply', mode: 'plan' }
const SHA_POLICY = { repository: 'TogetherWeOwn/kagi-shinobi', environment: 'production', mode: 'sha' }

function claim(overrides = {}) {
  return { planManifestSha256: HASH_A, planRunId: '37110947190', sourceSha: 'e'.repeat(40), workflowHeadSha: HEAD, ...overrides }
}

function plan(overrides = {}) {
  return { runId: '37110947190', headSha: OTHER_HEAD, sourceSha: 'e'.repeat(40), workflowHeadSha: HEAD, manifestSha256: HASH_A, ...overrides }
}

const SOURCE = 'e'.repeat(40)

// Verified evidence covering every commit the apply trusts: the migration
// source, the apply workflow head and the producing plan head.
function sourceEvidence(overrides = {}) {
  return { ok: true, shas: [SOURCE, HEAD, OTHER_HEAD], ...overrides }
}

function planArgs(overrides = {}) {
  return {
    event: EVENT,
    environment: PLAN_POLICY.environment,
    policy: PLAN_POLICY,
    headSha: HEAD,
    claim: claim(),
    plan: plan(),
    source: sourceEvidence(),
    goHashes: [HASH_A],
    ...overrides,
  }
}

function shaArgs(overrides = {}) {
  return {
    event: EVENT,
    environment: SHA_POLICY.environment,
    policy: SHA_POLICY,
    headSha: HEAD,
    goShas: [HEAD],
    ...overrides,
  }
}

// The two rows that approve. Everything below rejects.
test('plan mode: matching plan and CEO GO approves', () => {
  assert.deepEqual(decide(planArgs()), { state: 'approved', reason: REASON.APPROVED })
})

test('sha mode: CEO GO for the exact head SHA approves', () => {
  assert.deepEqual(decide(shaArgs()), { state: 'approved', reason: REASON.APPROVED })
})

test('wrong event rejects in both modes', () => {
  assert.deepEqual(decide(planArgs({ event: 'push' })).reason, REASON.WRONG_EVENT)
  assert.deepEqual(decide(shaArgs({ event: 'push' })).reason, REASON.WRONG_EVENT)
})

test('wrong environment rejects (policy does not match the delivery)', () => {
  assert.deepEqual(decide(planArgs({ environment: 'staging-migrate-plan' })).reason, REASON.WRONG_ENVIRONMENT)
  assert.deepEqual(decide(shaArgs({ environment: 'staging' })).reason, REASON.WRONG_ENVIRONMENT)
})

test('missing policy rejects', () => {
  assert.deepEqual(decide(planArgs({ policy: null })).reason, REASON.WRONG_ENVIRONMENT)
})

test('policy naming another environment rejects cross-environment approval', () => {
  // A plan-mode policy for staging must never approve a production delivery,
  // and a sha-mode policy must never approve by plan hash.
  assert.deepEqual(decide(planArgs({ environment: SHA_POLICY.environment, policy: SHA_POLICY })).reason, REASON.GO_SHA_MISSING)
  assert.deepEqual(decide(shaArgs({ environment: PLAN_POLICY.environment, policy: PLAN_POLICY })).reason, REASON.CLAIM_MISSING)
})

test('missing claim rejects in plan mode', () => {
  assert.equal(decide(planArgs({ claim: null })).reason, REASON.CLAIM_MISSING)
})

test('malformed claim rejects in plan mode', () => {
  assert.equal(decide(planArgs({ claim: { planManifestSha256: 'short', planRunId: 101 } })).reason, REASON.CLAIM_MISSING)
  assert.equal(decide(planArgs({ claim: { planManifestSha256: HASH_A } })).reason, REASON.CLAIM_MISSING)
  assert.equal(decide(planArgs({ claim: { planManifestSha256: HASH_A.toUpperCase(), planRunId: 101 } })).reason, REASON.CLAIM_MISSING)
})

test('missing plan rejects in plan mode', () => {
  assert.equal(decide(planArgs({ plan: null })).reason, REASON.PLAN_NOT_FOUND)
})

test('plan run id mismatch rejects', () => {
  assert.equal(decide(planArgs({ plan: plan({ runId: 102 }) })).reason, REASON.PLAN_NOT_FOUND)
})

test('plan on a different reviewed source rejects', () => {
  assert.equal(decide(planArgs({ plan: plan({ sourceSha: OTHER_HEAD }) })).reason, REASON.SHA_MISMATCH)
})

test('claim or plan authenticating another apply workflow head rejects', () => {
  assert.equal(decide(planArgs({ claim: claim({ workflowHeadSha: OTHER_HEAD }) })).reason, REASON.SHA_MISMATCH)
  assert.equal(decide(planArgs({ plan: plan({ workflowHeadSha: OTHER_HEAD }) })).reason, REASON.SHA_MISMATCH)
})

test('invalid run head rejects in both modes', () => {
  assert.equal(decide(planArgs({ headSha: 'not-a-sha' })).reason, REASON.SHA_MISMATCH)
  assert.equal(decide(shaArgs({ headSha: 'not-a-sha' })).reason, REASON.SHA_MISMATCH)
})

test('hash mismatch rejects in plan mode', () => {
  assert.equal(decide(planArgs({ plan: plan({ manifestSha256: HASH_B }) })).reason, REASON.HASH_MISMATCH)
})

test('absent or malformed source evidence rejects as missing, never a pass', () => {
  for (const source of [null, undefined, [], 'ok', 7, {}, { ok: 'yes', shas: [SOURCE, HEAD, OTHER_HEAD] },
    { ok: true }, { ok: true, shas: 'x' }, { ok: true, shas: [SOURCE, HEAD, 'not-a-sha'] },
    { ok: false }, { ok: false, reason: 'plan_and_go_verified' }, { ok: false, reason: 'anything_else' }]) {
    assert.deepEqual(decide(planArgs({ source })), { state: 'rejected', reason: REASON.SOURCE_EVIDENCE_MISSING }, JSON.stringify(source))
  }
})

test('a failed source verification rejects with its own stable reason', () => {
  for (const reason of [REASON.SOURCE_CI_UNVERIFIED, REASON.SOURCE_REVIEW_UNVERIFIED]) {
    assert.deepEqual(decide(planArgs({ source: { ok: false, reason } })), { state: 'rejected', reason })
  }
})

test('source evidence must cover the migration source, the workflow head and the plan head', () => {
  for (const missing of [SOURCE, HEAD, OTHER_HEAD]) {
    const shas = [SOURCE, HEAD, OTHER_HEAD].filter((sha) => sha !== missing)
    assert.equal(decide(planArgs({ source: sourceEvidence({ shas }) })).reason, REASON.SOURCE_EVIDENCE_MISSING, missing)
  }
  assert.equal(decide(planArgs({ source: sourceEvidence({ shas: [] }) })).reason, REASON.SOURCE_EVIDENCE_MISSING)
  // Extra verified commits are harmless; the three required ones are not optional.
  assert.equal(decide(planArgs({ source: sourceEvidence({ shas: [SOURCE, HEAD, OTHER_HEAD, 'f'.repeat(40)] }) })).state, 'approved')
})

test('a CEO GO and a verified plan do not approve without source evidence', () => {
  assert.deepEqual(decide(planArgs({ source: null, goHashes: [HASH_A] })), { state: 'rejected', reason: REASON.SOURCE_EVIDENCE_MISSING })
})

test('source evidence is checked before the CEO GO, after the plan binding', () => {
  assert.equal(decide(planArgs({ source: null, goHashes: [] })).reason, REASON.SOURCE_EVIDENCE_MISSING)
  assert.equal(decide(planArgs({ source: null, plan: plan({ manifestSha256: HASH_B }) })).reason, REASON.HASH_MISMATCH)
  assert.equal(decide(planArgs({ source: null, plan: null })).reason, REASON.PLAN_NOT_FOUND)
})

test('sha mode never reads source evidence', () => {
  assert.equal(decide(shaArgs({ source: null })).state, 'approved')
  assert.equal(decide(shaArgs({ source: { ok: false, reason: REASON.SOURCE_CI_UNVERIFIED } })).state, 'approved')
})

test('missing CEO GO rejects in plan mode', () => {
  assert.equal(decide(planArgs({ goHashes: [] })).reason, REASON.GO_MISSING)
  assert.equal(decide(planArgs({ goHashes: [HASH_B] })).reason, REASON.GO_MISSING)
  assert.equal(decide(planArgs({ goHashes: null })).reason, REASON.GO_MISSING)
})

test('GO for the exact hash approves among others', () => {
  assert.equal(decide(planArgs({ goHashes: [HASH_B, HASH_A] })).state, 'approved')
})

test('sha mode: GO for another SHA rejects', () => {
  assert.deepEqual(decide(shaArgs({ goShas: [OTHER_HEAD] })), { state: 'rejected', reason: REASON.GO_SHA_MISSING })
  assert.deepEqual(decide(shaArgs({ goShas: [] })), { state: 'rejected', reason: REASON.GO_SHA_MISSING })
})

test('sha mode: malformed GO set rejects (never a pass)', () => {
  assert.equal(decide(shaArgs({ goShas: null })).reason, REASON.GO_MISSING)
  assert.equal(decide(shaArgs({ goShas: ['not-a-sha'] })).reason, REASON.GO_MISSING)
})

test('sha mode: GO for the exact SHA approves among others', () => {
  assert.equal(decide(shaArgs({ goShas: [OTHER_HEAD, HEAD] })).state, 'approved')
})

test('sha mode: a plan-hash GO does not approve', () => {
  // Plan hashes (64hex) are not SHAs (40hex): they cannot appear in goShas,
  // and a GO set carrying one is malformed, which rejects.
  assert.equal(decide(shaArgs({ goShas: [HASH_A] })).reason, REASON.GO_MISSING)
})
