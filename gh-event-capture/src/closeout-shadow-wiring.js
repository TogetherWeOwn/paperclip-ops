// Runner wiring for the S3b shadow review (design §5.2, slice S3c).
//
// The first caller of closeout-sweep.js. Shadow only, config-gated, default
// off: with no `closeout.shadow` block the runner builds no hook and the sweep
// is byte-for-byte what it was. The hook has no board, no capture and no
// claim-save dependency and the transport is never handed a comment
// credential, so no call path here can claim, comment, wake or merge. Its only
// writes are the S3b debounce file (pending.json) inside createCloseoutReview.
//
// OBSERVATIONAL BY CONSTRUCTION. A failure for one repository is a counted
// error for that repository: it never fails the events stream, the sweep
// reconciliations or the pass `ok` flag. The one exception is a whole-pass
// stop (deadline, request budget, cancellation), which must still escape
// exactly as it does for every other sweep call.
//
// Policy source: nothing else supplies a closeout policy yet. Required checks
// are per repo and branch (design §3, §5.4), so they are read live from the
// branch rulesets, one call per enabled repository per review snapshot; the
// review check and the grace periods are the fixed values below. An unreadable
// or empty ruleset is a counted `policy-unavailable` error, never a guess.
//
// Mapping source: the runner supplies the warmed board-backed index to this
// hook only. The live events consumer still receives no index.
import { claimNamespace, loadClaimState } from './claim-store.js'
import { closeoutPolicy } from './closeout.js'
import { DEFAULT_DEBOUNCE_MS, pendingNamespace } from './closeout-pending.js'
import { createCloseoutReview, emptyCounters } from './closeout-sweep.js'
import { OPEN_ISSUE_LIST_MAX_REQUESTS } from './paperclip-adapter.js'
import { closeoutDisarmSettings } from './closeout-disarm-wiring.js'
import { closeoutRedmainSettings } from './closeout-redmain-wiring.js'
import { closeoutDeploySettings } from './closeout-deploy-wiring.js'
import { closeoutDeployFailSettings } from './closeout-deployfail-wiring.js'

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const DEBOUNCE_MIN_MS = 1000
const DEBOUNCE_MAX_MS = 3600000
const MAX_DECISION_LINES = 50

// The ruleset read is for this branch of each enabled repository.
export const SHADOW_BRANCH = 'main'
// Design §5.2/§5.4: ten minutes before a missing or stalled PR is an exception.
export const SHADOW_POLICY = Object.freeze({
  reviewCheck: 'Paperclip Review',
  holdLabels: Object.freeze(['hold']),
  missingGraceMs: 600000,
  stalledGraceMs: 600000,
})
const ERRORS = Object.freeze({ claims: 'claims-unreadable', policy: 'policy-unavailable', review: 'review-failed' })

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const plain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)

/**
 * Validate the optional `closeout` block of the consumer configuration.
 * Fail-closed: unknown keys, a half-configured block, repositories outside
 * `allowedRepositories` and out-of-range debounce all refuse. Absent, empty,
 * or an empty `enabledRepositories` list is off.
 *
 * @returns {null | { enabledRepositories: Set<string>, debounceMs: number, indexReadsPerPass: number }}
 */
export function closeoutShadowSettings(config) {
  const block = config?.closeout
  if (block === undefined) return null
  requireValue(plain(block) && Object.keys(block).every((key) => ['shadow', 'disarmShadow', 'redmainShadow', 'deployShadow', 'deployFailShadow'].includes(key)),
    'closeout configuration holds an unknown key')
  // Config loading and direct runner construction validate ALL children.
  closeoutDisarmSettings(config)
  closeoutRedmainSettings(config)
  closeoutDeploySettings(config)
  closeoutDeployFailSettings(config)
  if (block.shadow === undefined) return null
  const shadow = block.shadow
  requireValue(plain(shadow) && Object.keys(shadow).every((key) =>
    ['enabledRepositories', 'debounceMs', 'indexReadsPerPass'].includes(key)),
    'closeout shadow configuration holds an unknown key')
  const { enabledRepositories, debounceMs = DEFAULT_DEBOUNCE_MS, indexReadsPerPass = 60 } = shadow
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'closeout shadow enabledRepositories must be a list of unique owner/repo names')
  requireValue(Array.isArray(config.allowedRepositories) &&
    enabledRepositories.every((r) => config.allowedRepositories.includes(r)),
  'closeout shadow repositories must be allowed repositories')
  requireValue(Number.isInteger(debounceMs) && debounceMs >= DEBOUNCE_MIN_MS && debounceMs <= DEBOUNCE_MAX_MS,
    'closeout shadow debounce must be 1s to 1h in milliseconds')
  const maxRequests = config.limits?.maxRequests ?? 300
  const indexRequestBudget = Math.max(1, Math.floor((maxRequests - 1) / 2))
  const listRequests = OPEN_ISSUE_LIST_MAX_REQUESTS
  const fitsRequestBudget = enabledRepositories.length === 0 ||
    (indexReadsPerPass < maxRequests / 2 && indexRequestBudget >= listRequests + indexReadsPerPass)
  requireValue(Number.isInteger(indexReadsPerPass) && indexReadsPerPass >= 1 && indexReadsPerPass <= 150 &&
    fitsRequestBudget,
  'closeout shadow index reads and open-set pagination must fit below half the request budget')
  return enabledRepositories.length === 0 ? null
    : Object.freeze({ enabledRepositories: new Set(enabledRepositories), debounceMs, indexReadsPerPass })
}

function failure(stage) {
  const counters = emptyCounters()
  counters.errors = 1
  return { records: [], counters, pending: null, error: ERRORS[stage] }
}

/**
 * The `closeoutReview` hook for runSweepCycle.
 *
 * @param {object} deps settings (closeoutShadowSettings result), the receipt
 *   namespace digest, the state directory, the budget-wrapped GitHub adapter,
 *   `stopped` (true once the pass budget has stopped the pass), a `log` line
 *   sink, an injectable wall clock, and `index` (the warmed board-backed
 *   PR-task index supplied by the runner only to this shadow hook).
 * @returns {{ runReview }} `runReview` resolves `null` for a repository that
 *   is not enrolled (no reads at all), else the S3b shadow result, or a
 *   counted-error result when this repository's shadow failed.
 */
export function createShadowReviewHook({ settings, namespace, directory, github, index = null, stopped = () => false,
  log = () => {}, nowMs = () => Date.now() }) {
  requireValue(settings && settings.enabledRepositories instanceof Set, 'closeout shadow settings are invalid')
  requireValue(github && typeof github.getCloseoutSnapshot === 'function' &&
    typeof github.getRulesetRequiredChecks === 'function',
  'closeout shadow requires a snapshot reader and a ruleset reader')
  requireValue(typeof stopped === 'function' && typeof log === 'function' && typeof nowMs === 'function',
    'closeout shadow callbacks are invalid')
  let claims = null

  // Fixed words and counters only. Never a PR title, body, URL or error text:
  // those come from untrusted fields or child streams that can carry secrets.
  function emit(repository, result) {
    const line = (value) => {
      try { log(JSON.stringify(value)) } catch { /* A broken sink must not fail an observational pass. */ }
    }
    const decisions = result.records.filter((r) => r.decision !== null || r.error !== undefined)
    line({ event: 'closeout-shadow', repository, ...(result.error ? { error: result.error } : {}),
      counters: result.counters, pending: result.pending,
      ...(decisions.length > MAX_DECISION_LINES ? { omittedDecisions: decisions.length - MAX_DECISION_LINES } : {}) })
    for (const r of decisions.slice(0, MAX_DECISION_LINES)) {
      line({ event: 'closeout-shadow-decision', ref: `${r.repository}#${r.number}@${r.headSha ?? 'unknown'}`,
        decision: r.decision, claim: r.claim, reason: r.error ?? null })
    }
  }

  async function runReview({ repository, numbers }) {
    if (!settings.enabledRepositories.has(repository)) return null
    let stage = 'claims'
    let result
    try {
      // Read-only and memoized: one claims.json read per pass, none when no
      // review snapshot fires. This file is never written from here.
      claims ??= loadClaimState({ directory, namespace: claimNamespace(namespace) })
      const loaded = await claims
      stage = 'policy'
      const policy = closeoutPolicy({ ...SHADOW_POLICY,
        requiredChecks: await github.getRulesetRequiredChecks(repository, SHADOW_BRANCH) })
      stage = 'review'
      const review = createCloseoutReview({ directory, namespace: pendingNamespace(namespace), claims: loaded,
        github, index, policy, debounceMs: settings.debounceMs, nowMs })
      result = await review.runReview({ repository, numbers })
      // A stop during a snapshot read is swallowed by the per-PR catch inside
      // runCloseoutShadow and comes back as `snapshot-read-failed` errors.
      // Surface it here, before anything is logged, so a stopped pass never
      // reads as a shadow failure. The debounce clocks of the PRs it was
      // evaluating are already flushed (same as any unreadable snapshot): they
      // re-debounce next pass, a delay and nothing more.
      if (stopped()) throw new Error('sweep pass stopped')
    } catch {
      // A whole-pass stop is not a shadow failure: let it escape.
      if (stopped()) throw new Error('sweep pass stopped')
      result = failure(stage)
    }
    emit(repository, result)
    return result
  }

  return { runReview }
}
