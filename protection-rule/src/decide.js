// Pure verdict for the protection-rule App.
//
// Two modes, selected per environment by the policy table (`environments.js`):
//   'plan' — APPROVE only when ALL hold for the pending deployment:
//     (1) an authenticated pre-gate claim matches the prior plan's Rust
//         projection and the separately reviewed source SHA,
//     (2) green required CI and an independent Paperclip Review are
//         authenticated on EVERY commit the apply trusts (the migration
//         source, the producing plan head and the apply workflow head), and
//     (3) the CEO posted GO for that exact hash on the environment's card.
//   'sha' — APPROVE only when the CEO posted GO for the exact head SHA of the
//     run under review on the environment's card. Production deploys carry no
//     plan artifact, so the GO binds the commit itself.
// REJECT everything else. There is no third outcome and no "approve on doubt"
// branch; every error shape below resolves to `rejected`.
//
// This module takes already-verified evidence and returns a verdict. It does
// no I/O, reads no secret, and approves nothing by itself — the caller posts
// the verdict to GitHub's callback URL. Keep it pure so the whole decision
// table is testable with no network, no credential and no clock.

export const EVENT = 'deployment_protection_rule'

// Verdict reasons. Stable identifiers, not prose: operators match on them and
// the GitHub callback comment carries them, so rewording one is a breaking
// change. Assert on these, never on a message.
export const REASON = {
  APPROVED: 'plan_and_go_verified',
  WRONG_EVENT: 'not_protection_rule_event',
  WRONG_ENVIRONMENT: 'wrong_environment',
  UNKNOWN_MODE: 'unknown_mode',
  CLAIM_MISSING: 'claim_unestablished',
  PLAN_NOT_FOUND: 'plan_not_found',
  SHA_MISMATCH: 'plan_commit_mismatch',
  HASH_MISMATCH: 'plan_hash_mismatch',
  SOURCE_EVIDENCE_MISSING: 'source_evidence_missing',
  SOURCE_CI_UNVERIFIED: 'source_ci_unverified',
  SOURCE_REVIEW_UNVERIFIED: 'source_review_unverified',
  GO_MISSING: 'ceo_go_missing',
  GO_SHA_MISSING: 'ceo_go_sha_missing',
}

import { isRunId } from './document.js'

const SHA256 = /^[0-9a-f]{64}$/
const SHA40 = /^[0-9a-f]{40}$/
const MODES = ['plan', 'sha']
// The only two failures `verifySourceEvidence` may report. Anything else is a
// malformed result and is treated as no evidence at all.
const SOURCE_FAILURES = [REASON.SOURCE_CI_UNVERIFIED, REASON.SOURCE_REVIEW_UNVERIFIED]

function isClaim(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof value.planManifestSha256 === 'string' &&
    SHA256.test(value.planManifestSha256) &&
    isRunId(value.planRunId) &&
    typeof value.sourceSha === 'string' && SHA40.test(value.sourceSha) &&
    typeof value.workflowHeadSha === 'string' && SHA40.test(value.workflowHeadSha)
  )
}

function isPlan(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    isRunId(value.runId) &&
    typeof value.sourceSha === 'string' && SHA40.test(value.sourceSha) &&
    typeof value.workflowHeadSha === 'string' && SHA40.test(value.workflowHeadSha) &&
    typeof value.headSha === 'string' &&
    SHA40.test(value.headSha) &&
    typeof value.manifestSha256 === 'string' &&
    SHA256.test(value.manifestSha256)
  )
}

function isSourceEvidence(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  if (value.ok === true) {
    return Array.isArray(value.shas) && value.shas.every((entry) => typeof entry === 'string' && SHA40.test(entry))
  }
  return value.ok === false && SOURCE_FAILURES.includes(value.reason)
}

function isPolicy(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof value.environment === 'string' &&
    value.environment.length > 0 &&
    MODES.includes(value.mode)
  )
}

function isShaList(value) {
  return (
    Array.isArray(value) &&
    value.every((entry) => typeof entry === 'string' && SHA40.test(entry))
  )
}

/**
 * Decide the verdict for one pending deployment.
 *
 * @param {object} args
 * @param {string} args.event            GitHub event name from the webhook
 * @param {string} args.environment      environment named by the webhook
 * @param {object} args.policy           resolved policy for (repository, environment)
 * @param {string} args.headSha          run head SHA from a fresh API read
 * @param {object | null} [args.claim]   the apply run's claimed plan, or null (plan mode)
 * @param {object | null} [args.plan]    verified plan evidence, or null (plan mode)
 * @param {{ ok: boolean, shas?: string[], reason?: string } | null} [args.source]
 *   authenticated CI and review evidence per commit (plan mode); see `verifySourceEvidence`
 * @param {string[]} [args.goHashes]     CEO-GO'd plan hashes from the board scan (plan mode)
 * @param {string[]} [args.goShas]       CEO-GO'd head SHAs from the board scan (sha mode)
 * @returns {{ state: 'approved' | 'rejected', reason: string }}
 */
export function decide({ event, environment, policy, headSha, claim = null, plan = null, source = null, goHashes = [], goShas = [] }) {
  if (event !== EVENT) return { state: 'rejected', reason: REASON.WRONG_EVENT }
  if (!isPolicy(policy) || environment !== policy.environment) {
    return { state: 'rejected', reason: REASON.WRONG_ENVIRONMENT }
  }
  if (typeof headSha !== 'string' || !SHA40.test(headSha)) {
    return { state: 'rejected', reason: REASON.SHA_MISMATCH }
  }
  if (policy.mode === 'sha') {
    // No plan artifact on production deploys: the GO binds the commit itself.
    // A malformed GO set is a rejection, never a pass — without a readable
    // set there is nothing to bind the deployment to.
    if (!isShaList(goShas)) {
      return { state: 'rejected', reason: REASON.GO_MISSING }
    }
    if (!goShas.includes(headSha)) {
      return { state: 'rejected', reason: REASON.GO_SHA_MISSING }
    }
    return { state: 'approved', reason: REASON.APPROVED }
  }
  if (policy.mode !== 'plan') return { state: 'rejected', reason: REASON.UNKNOWN_MODE }
  // A malformed or absent claim is a rejection, never a pass: without a well-
  // formed (hash, run) pair there is nothing to bind the deployment to.
  if (!isClaim(claim)) return { state: 'rejected', reason: REASON.CLAIM_MISSING }
  if (!isPlan(plan)) return { state: 'rejected', reason: REASON.PLAN_NOT_FOUND }
  if (plan.runId !== claim.planRunId) return { state: 'rejected', reason: REASON.PLAN_NOT_FOUND }
  if (claim.workflowHeadSha !== headSha || plan.workflowHeadSha !== headSha || plan.sourceSha !== claim.sourceSha) {
    return { state: 'rejected', reason: REASON.SHA_MISMATCH }
  }
  if (plan.manifestSha256 !== claim.planManifestSha256) {
    return { state: 'rejected', reason: REASON.HASH_MISMATCH }
  }
  // Ancestry and a CEO GO are not review. Every commit whose code or data the
  // apply trusts must carry authenticated green CI and an independent review:
  // the migration source, the producing plan's workflow head and the apply
  // workflow head. A result that is absent, malformed, or does not cover all
  // three is no evidence, never a partial pass.
  if (!isSourceEvidence(source)) return { state: 'rejected', reason: REASON.SOURCE_EVIDENCE_MISSING }
  if (source.ok === false) return { state: 'rejected', reason: source.reason }
  if (![claim.sourceSha, claim.workflowHeadSha, plan.headSha].every((entry) => source.shas.includes(entry))) {
    return { state: 'rejected', reason: REASON.SOURCE_EVIDENCE_MISSING }
  }
  if (!Array.isArray(goHashes) || !goHashes.includes(claim.planManifestSha256)) {
    return { state: 'rejected', reason: REASON.GO_MISSING }
  }
  return { state: 'approved', reason: REASON.APPROVED }
}
