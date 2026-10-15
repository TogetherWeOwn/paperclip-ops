// Staging deploy-success → QA-smoke intent (design:
// docs/decisions/case_439e6875-pr-closeout-v2-design.md §7 row 2 + §9 slice S9,
// first S9 automation after S8 red-main).
//
// When a `deployment_status` event reports **success** in a **staging**
// environment for an allowlisted repo, the reconciler records one deduped
// QA-smoke intent per `(repo, sha)` — shadow-first: the decision and counters
// are logged, and this slice creates NO task and NO wake. Live task creation
// is a later slice after shadow validation, behind the same per-repo flag.
//
// Three pieces, mirroring the S8 split (closeout-redmain.js):
//
// 1. decideDeploySmoke: the pure verdict over one parsed `deployment_status`
//    delivery payload. Eligible or not, with a reason. Anything malformed
//    throws: a swallowed bad read would look exactly like "no deploy".
// 2. createDeployWatcher: the duty runner. Per repo it lists stored
//    `deployment_status` deliveries inside a bounded time window (default
//    the last 7 days; the capture `since` filter applies server-side, so a
//    long history can never blow the adapter page budget), decides each, and
//    either records the intent (flag on) or logs would-record (flag off).
//    One unreadable repo or one undecidable delivery is a counted error
//    record, never a thrown run.
// 3. Suppression input: the watcher takes an `isSuppressed(repository)`
//    predicate wired to the S8 red-main incident store. While a red-main
//    incident is open for the repo, eligible intents resolve to suppressed +
//    counted records and are never stored. Suppression covers intents ONLY:
//    this module takes no board, capture-claim or disarm dependency, so a
//    suppressed intent can never wake, claim — or disarm.
//
// Staging match rule (documented here and in
// gh-event-capture/docs/closeout-debounce-shadow.md): the deployment
// environment name, trimmed and lowercased, is staging iff it equals
// `staging`, starts with `staging-` / `staging_` / `staging/`, ends with
// `-staging` / `_staging` / `/staging`, or contains `-staging-` /
// `_staging_` / `/staging/`. So `staging`, `STAGING`, `staging-us`,
// `app-staging` and `app-staging-us` match; `production`, `preview`,
// `development` and `canary` do not. `stage` alone does NOT match: it is a
// distinct environment name, and guessing it as staging would smoke-test the
// wrong deploy.
//
// Allowlist: non-allowlisted repos are dropped before ANY store write (the
// same four-repo scope as S7/S8). The watcher enforces the configured
// `allowedRepositories` set per delivery even when the caller already scopes
// the scan, so a mis-scoped caller cannot write intents for a repo outside
// the relay scope.
//
// Dedupe key: `qa-smoke:{repo}:{sha}` — one intent per key. Repeats are
// logged + counted, never duplicated.
//
// Per-repo flag gate (§12, same pattern as S8/S6b): `enabledRepositories`,
// DEFAULT OFF (the empty set). Intent transitions persist only for listed
// repositories. Anywhere else the run logs `would-record` with counters and
// writes no state (shadow behavior). Rollback is the flag: remove the repo;
// recorded intents stay as audit history but nothing new is recorded.

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
// Only names that match this ever reach intent text; anything else is
// counted, never echoed (same floor as closeout.js SAFE_NAME).
const SAFE_NAME = /^[A-Za-z0-9 ._()/-]{1,64}$/
const QA_OWNER_ROLE = /^[a-z][a-z0-9-]{1,40}$/
const MAX_WATCH_REPOS = 100
const MAX_DELIVERIES_PER_REPO = 1000
const MS_LIMIT = 8.64e15
// Default scan window: D1 never prunes deliveries and the capture adapter's
// page budget throws past 10000 rows, so an unbounded listing goes blind
// exactly when history is longest. Staging smoke targets recent SHAs, so
// each run scans 7 days back unless the caller passes an explicit window.
// Explicit null means unbounded (backfill only): it can still throw
// capture-list-failed past the page budget, scoped to that repo.
export const DEFAULT_HISTORY_WINDOW_MS = 7 * 24 * 3600 * 1000

/**
 * The QA owner when no per-repo override names one. Shadow-only in this
 * slice (no task is created): the value is persisted on the intent so S10
 * promotion posts to the same owner without re-deciding routing.
 */
export const DEFAULT_QA_OWNER = 'qa'

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isMs = (value) => Number.isInteger(value) && value >= 0 && value <= MS_LIMIT

/**
 * Idempotent intent identity: one intent per (repo, sha). The environment
 * rides in the body, never in the key, so a re-deploy of the same SHA adds
 * nothing and a new SHA records fresh.
 */
export function qaSmokeKey(repository, sha) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'qa-smoke repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'qa-smoke sha is invalid')
  return `qa-smoke:${repository}:${sha}`
}

/**
 * Validate and freeze the deploy-smoke policy. Fails loudly. The enabled set
 * defaults to EMPTY (flag off everywhere): intent transitions must be opted
 * in per repo, never silently live.
 *
 * @param {{ enabledRepositories?: string[],
 *   qaOwners?: Record<string,string> }} policy
 */
export function deployPolicy(policy = {}) {
  requireValue(policy && typeof policy === 'object' && !Array.isArray(policy), 'qa-smoke policy is required')
  const { enabledRepositories = [], qaOwners = {} } = policy
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'enabledRepositories must be a list of unique owner/repo names')
  requireValue(qaOwners && typeof qaOwners === 'object' && !Array.isArray(qaOwners) &&
    Object.entries(qaOwners).every(([r, o]) => REPOSITORY.test(r) && typeof o === 'string' && QA_OWNER_ROLE.test(o)),
  'qaOwners must map owner/repo names to owner roles')
  return Object.freeze({
    enabledRepositories: Object.freeze([...enabledRepositories]),
    qaOwners: Object.freeze({ ...qaOwners }),
  })
}

/**
 * One constant assignee per repo: the per-repo override when present, else
 * DEFAULT_QA_OWNER. Never guesses from environment or payload text.
 */
export function resolveQaOwner(policy, repository) {
  requireValue(policy && typeof policy.qaOwners === 'object', 'qa-smoke policy is invalid')
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'qa-smoke repository is invalid')
  return policy.qaOwners[repository] ?? DEFAULT_QA_OWNER
}

/**
 * The documented staging match rule. Input must be a string; anything else
 * is not staging (the caller validates presence separately and throws on a
 * missing environment, so a missing env is loud, never a silent drop).
 */
export function isStagingEnvironment(value) {
  if (typeof value !== 'string') return false
  const name = value.trim().toLowerCase()
  if (name.length === 0 || name.length > 255) return false
  if (name === 'staging') return true
  for (const prefix of ['staging-', 'staging_', 'staging/']) {
    if (name.startsWith(prefix)) return true
  }
  for (const suffix of ['-staging', '_staging', '/staging']) {
    if (name.endsWith(suffix)) return true
  }
  for (const infix of ['-staging-', '_staging_', '/staging/']) {
    if (name.includes(infix)) return true
  }
  return false
}

function safeEnvironment(value) {
  return typeof value === 'string' && SAFE_NAME.test(value.trim()) ? value.trim() : null
}

/**
 * Pure verdict over one parsed `deployment_status` webhook payload.
 *
 * Reads the environment from `payload.deployment.environment`, falling back
 * to `payload.deployment_status.environment` and then `payload.environment:
 * GitHub documents the name on the deployment, but a tolerant read costs
 * nothing and a strict single-path read would drop a real deploy as
 * "malformed". The state is ALWAYS `payload.deployment_status.state`
 * (exact `success`); anything else is ineligible, never an intent. The SHA
 * is ALWAYS `payload.deployment.sha` (the commit the deploy points at).
 *
 * @param {any} payload parsed webhook body, `event === 'deployment_status'`
 * @returns {{ eligible: boolean, repository: string, sha: string|null,
 *   environment: string|null, key: string|null, reason: string }}
 */
export function decideDeploySmoke(payload) {
  requireValue(payload && typeof payload === 'object' && !Array.isArray(payload), 'deployment_status payload is required')
  const repository = payload.repository?.full_name
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'deployment_status repository is invalid')
  const status = payload.deployment_status
  requireValue(status && typeof status === 'object' && !Array.isArray(status),
    'deployment_status status is missing')
  requireValue(typeof status.state === 'string' && status.state.length > 0,
    'deployment_status state is missing')
  const deployment = payload.deployment
  requireValue(deployment && typeof deployment === 'object' && !Array.isArray(deployment),
    'deployment_status deployment is missing')
  requireValue(typeof deployment.sha === 'string' && SHA.test(deployment.sha),
    'deployment_status sha is invalid')
  const rawEnvironment = deployment.environment ?? status.environment ?? payload.environment
  requireValue(typeof rawEnvironment === 'string' && rawEnvironment.trim().length > 0,
    'deployment_status environment is missing')
  const sha = deployment.sha
  if (status.state !== 'success') {
    return { eligible: false, repository, sha, environment: rawEnvironment.trim(),
      key: null, reason: 'non-success-state' }
  }
  if (!isStagingEnvironment(rawEnvironment)) {
    return { eligible: false, repository, sha, environment: rawEnvironment.trim(),
      key: null, reason: 'non-staging-environment' }
  }
  return { eligible: true, repository, sha, environment: rawEnvironment.trim(),
    key: qaSmokeKey(repository, sha), reason: 'staging-success' }
}

/**
 * Fixed-words intent text: the key, the SHA, the safe environment name and
 * the routed owner. Titles, bodies and deploy prose are never accepted here
 * at all.
 */
export function renderQaSmokeIntent({ repository, sha, environment, qaOwner }) {
  const key = qaSmokeKey(repository, sha)
  requireValue(typeof qaOwner === 'string' && QA_OWNER_ROLE.test(qaOwner), 'qa-smoke intent owner is invalid')
  const env = safeEnvironment(environment)
  requireValue(env !== null, 'qa-smoke intent environment is unsafe')
  return `QA smoke ${key}: staging deploy of \`${sha}\` in environment \`${env}\` succeeded. Routed to ${qaOwner}.`
}

function emptyCounters() {
  return { total: 0, deliveries: 0, eligible: 0, recorded: 0, wouldRecord: 0,
    alreadyRecorded: 0, suppressed: 0, dropped: 0, nonSuccess: 0, nonStaging: 0,
    overBudget: 0, renderFailed: 0, errors: 0 }
}

function logLine(repository, sha, decision, reason) {
  return `closeout-deploy ${repository}@${sha ?? 'unknown'}: ${decision} (${reason})`
}

/**
 * Dependencies: capture.listDeliveries({ repository, event }) +
 * capture.getDelivery(id), and a QA-smoke intent store with
 * findIntent/has + recordIntent ONLY. There is deliberately no board,
 * claim or disarm dependency: this duty records intents and renders their
 * texts but posts nothing, so it cannot wake, claim, disarm or create a
 * task. Suppression arrives as a pure predicate, never as a store handle.
 *
 * Capture interface (async):
 *   listDeliveries({ repository, event: 'deployment_status' }) -> rows
 *   getDelivery(id) -> { delivery_id, repository, event, body, body_truncated }
 * Store interface (async):
 *   findIntent(repository, sha) -> null | { key, repository, sha, ... }
 *   recordIntent({ key, repository, sha, environment, firstSeenMs, qaOwner, body })
 */
export function createDeployWatcher({ capture, intents, policy, allowedRepositories,
  isSuppressed = null, nowMs = () => Date.now() }) {
  requireValue(capture && typeof capture.listDeliveries === 'function' &&
    typeof capture.getDelivery === 'function',
  'deploy watcher requires a capture reader with list/get deliveries, and nothing else')
  requireValue(intents && typeof intents.findIntent === 'function' &&
    typeof intents.recordIntent === 'function',
  'deploy watcher requires an intent store with find/record, and nothing else')
  const validated = deployPolicy(policy)
  requireValue(Array.isArray(allowedRepositories) && allowedRepositories.length > 0 &&
    allowedRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(allowedRepositories).size === allowedRepositories.length,
  'deploy watcher requires an explicit unique repository allowlist')
  requireValue(isSuppressed === null || typeof isSuppressed === 'function',
    'deploy suppression predicate is invalid')
  requireValue(typeof nowMs === 'function', 'deploy clock is invalid')
  const allowed = new Set(allowedRepositories)
  const enabled = new Set(validated.enabledRepositories)

  /**
   * Run the duty over explicit repositories. Non-allowlisted repos are
   * dropped before ANY read or write. One unreadable repo or one
   * undecidable delivery is a counted error record, never a thrown run.
   * Flag-off repos transition nothing (shadow behavior).
   *
   * Records carry { repository, sha, environment, key, decision, reason,
   * log, body? } with decision one of 'recorded' | 'would-record' |
   * 'already-recorded' | 'suppressed' | 'dropped' | 'ineligible' | 'none'.
   */
  async function runDeploy({ repositories, sinceMs = undefined, nowMs: nowOverride } = {}) {
    requireValue(Array.isArray(repositories) && repositories.length <= MAX_WATCH_REPOS &&
      repositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
      new Set(repositories).size === repositories.length,
    'deploy repositories are invalid')
    const now = nowOverride ?? nowMs()
    requireValue(isMs(now), 'deploy clock is invalid')
    // Omitted means the default recent window; explicit null means unbounded
    // (backfill only — past the adapter page budget it fails loud per repo).
    // Clamped at 0: a clock earlier than one window (tests) scans everything
    // rather than failing on its own default.
    const windowStart = sinceMs === undefined
      ? Math.max(0, now - DEFAULT_HISTORY_WINDOW_MS)
      : sinceMs
    requireValue(windowStart === null || (Number.isInteger(windowStart) && windowStart >= 0 && windowStart <= now),
      'deploy scan window is invalid')
    const records = []
    const counters = emptyCounters()
    for (const repository of repositories) {
      counters.total++
      // Dropped before any read or write: outside the relay scope entirely.
      if (!allowed.has(repository)) {
        counters.dropped++
        records.push({ repository, sha: null, environment: null, key: null,
          decision: 'dropped', reason: 'outside-allowlist',
          log: logLine(repository, null, 'dropped', 'outside-allowlist') })
        continue
      }
      let rows
      try {
        rows = await capture.listDeliveries({ repository, event: 'deployment_status',
          ...(windowStart === null ? {} : { sinceMs: windowStart }) })
      } catch {
        records.push({ repository, sha: null, environment: null, key: null,
          decision: 'none', reason: 'capture-list-failed',
          log: logLine(repository, null, 'none', 'capture-list-failed'),
          error: 'capture-list-failed' })
        counters.errors++
        continue
      }
      requireValue(Array.isArray(rows), 'deploy capture listing is invalid')
      // Bounded blast radius: D1 never prunes deliveries, so history grows
      // without bound. Process the newest-first window the adapter returns up
      // to the cap and count the rest as over-budget skips with a loud log,
      // instead of throwing the whole run for every repo once one repo
      // crosses the cap.
      let scoped = rows
      const overBudget = rows.length - MAX_DELIVERIES_PER_REPO
      if (overBudget > 0) {
        scoped = rows.slice(0, MAX_DELIVERIES_PER_REPO)
        counters.overBudget += overBudget
        records.push({ repository, sha: null, environment: null, key: null,
          decision: 'skipped', reason: `over-budget:${overBudget}-deliveries-unread`,
          log: logLine(repository, null, 'skipped', `over-budget:${overBudget}-deliveries-unread`) })
      }
      for (const row of scoped) {
        counters.deliveries++
        let delivery
        try {
          delivery = await capture.getDelivery(row.delivery_id)
        } catch {
          records.push({ repository, sha: null, environment: null, key: null,
            decision: 'none', reason: 'capture-read-failed',
            log: logLine(repository, null, 'none', 'capture-read-failed'),
            error: 'capture-read-failed' })
          counters.errors++
          continue
        }
        let payload
        try {
          requireValue(delivery && delivery.body_truncated === 0 && typeof delivery.body === 'string',
            'deploy delivery body is incomplete')
          payload = JSON.parse(delivery.body)
        } catch {
          records.push({ repository, sha: null, environment: null, key: null,
            decision: 'none', reason: 'undecidable-delivery',
            log: logLine(repository, null, 'none', 'undecidable-delivery'),
            error: 'undecidable-delivery' })
          counters.errors++
          continue
        }
        let verdict
        try {
          verdict = decideDeploySmoke(payload)
        } catch {
          records.push({ repository, sha: null, environment: null, key: null,
            decision: 'none', reason: 'undecidable-delivery',
            log: logLine(repository, null, 'none', 'undecidable-delivery'),
            error: 'undecidable-delivery' })
          counters.errors++
          continue
        }
        // The delivery names its own repo; a row scoped to one repo carrying
        // another repo's payload is dropped, never stored under the wrong key.
        if (verdict.repository !== repository) {
          counters.dropped++
          records.push({ repository, sha: verdict.sha, environment: verdict.environment,
            key: null, decision: 'dropped', reason: 'repository-mismatch',
            log: logLine(repository, verdict.sha, 'dropped', 'repository-mismatch') })
          continue
        }
        if (!verdict.eligible) {
          if (verdict.reason === 'non-success-state') counters.nonSuccess++
          else counters.nonStaging++
          records.push({ ...verdict, decision: 'ineligible',
            log: logLine(repository, verdict.sha, 'ineligible', verdict.reason) })
          continue
        }
        counters.eligible++
        let suppressed = false
        if (isSuppressed !== null) {
          try {
            suppressed = isSuppressed(repository)
          } catch {
            throw new Error('suppression predicate failed; refusing to guess')
          }
          requireValue(typeof suppressed === 'boolean', 'suppression predicate must return a boolean')
        }
        // S8 interaction: while a red-main incident is open for the repo,
        // eligible intents are logged + counted, never stored.
        if (suppressed) {
          counters.suppressed++
          records.push({ ...verdict, decision: 'suppressed', reason: 'red-main-open',
            log: logLine(repository, verdict.sha, 'suppressed', 'red-main-open') })
          continue
        }
        const on = enabled.has(repository)
        const qaOwner = resolveQaOwner(validated, repository)
        // An eligible environment outside SAFE_NAME (an en dash, over 64
        // characters) must be a counted per-delivery error, never a thrown
        // run: every run rescans history, so one such delivery would
        // otherwise fail every later run, in shadow mode too.
        let body
        try {
          body = renderQaSmokeIntent({ repository, sha: verdict.sha,
            environment: verdict.environment, qaOwner })
        } catch {
          counters.renderFailed++
          records.push({ ...verdict, body: null, decision: 'none', reason: 'intent-render-failed',
            log: logLine(repository, verdict.sha, 'none', 'intent-render-failed'),
            error: 'intent-render-failed' })
          counters.errors++
          continue
        }
        let existing
        try {
          existing = await intents.findIntent(repository, verdict.sha)
        } catch {
          records.push({ ...verdict, decision: 'none', reason: 'intent-store-unreadable',
            log: logLine(repository, verdict.sha, 'none', 'intent-store-unreadable'),
            error: 'intent-store-unreadable' })
          counters.errors++
          continue
        }
        requireValue(existing === null || (existing && existing.key === verdict.key),
        'qa-smoke intent store returned a foreign intent')
        // One intent per key: a repeat adds nothing — no new entry, no new
        // text, one counter. Checked BEFORE the flag so shadow runs prove
        // the dedupe without writing state.
        //
        // Rescan/prune contract: every run relists the full delivery
        // history, while the store keeps only the newest `maxEntries`
        // intents. An intent evicted by oldest-first pruning is therefore
        // re-recorded on the next run (counted `recorded` again, never
        // duplicated within the state). That is safe in this shadow slice
        // — no task and no wake ships — but the S10 live step MUST gate
        // creation on the same key (task index or verb idempotency), or a
        // pruned SHA would create a second QA task.
        if (existing !== null) {
          counters.alreadyRecorded++
          records.push({ ...verdict, body, decision: 'already-recorded', reason: 'intent-already-recorded',
            log: logLine(repository, verdict.sha, 'already-recorded', 'intent-already-recorded') })
          continue
        }
        if (!on) {
          counters.wouldRecord++
          records.push({ ...verdict, body, decision: 'would-record', reason: 'flag-off-shadow',
            log: logLine(repository, verdict.sha, 'would-record', 'flag-off-shadow') })
          continue
        }
        try {
          await intents.recordIntent({ key: verdict.key, repository,
            sha: verdict.sha, environment: verdict.environment,
            firstSeenMs: now, qaOwner, body })
        } catch {
          records.push({ ...verdict, body, decision: 'none', reason: 'intent-record-failed',
            log: logLine(repository, verdict.sha, 'none', 'intent-record-failed'),
            error: 'intent-record-failed' })
          counters.errors++
          continue
        }
        counters.recorded++
        records.push({ ...verdict, body, decision: 'recorded', reason: `staging-success:${verdict.sha}`,
          log: logLine(repository, verdict.sha, 'recorded', `staging-success:${verdict.sha}`) })
      }
    }
    return { records, counters, sinceMs: windowStart }
  }

  return { runDeploy }
}

// ---------------------------------------------------------------------------
// S9b: deploy-failure → deploy-fail intent (design:
// docs/decisions/case_439e6875-pr-closeout-v2-design.md §7 row 3 + §9 slice S9,
// second S9 automation after S9a staging deploy-success QA-smoke).
//
// When a `deployment_status` event reports **failure** or **error** for an
// allowlisted repo, the reconciler records one deduped deploy-failure intent
// per `(repo, sha)` — shadow-first: the decision and counters are logged, and
// this slice creates NO task and NO wake. Live task creation is a later slice
// after shadow validation, behind the same per-repo flag.
//
// Failure-state match rule (exact, case-sensitive): `state === 'failure'` or
// `state === 'error'` is eligible. `success` stays on the S9a path (it is
// acknowledged here as `success-on-smoke-path`, never stored). Every other
// state (`pending`, `in_progress`, `inactive`, `queued`, …) is acknowledged
// `non-failure-state` (2xx, counted, never stored).
//
// Environment does NOT filter failures: staging and production both count.
// The environment name is recorded on the intent (trimmed) but never decides
// eligibility — a failed production deploy needs its log pointer as much as
// a failed staging one.
//
// Log pointer chain (first SAFE pointer wins, else null):
// `deployment_status.log_url` → `deployment_status.target_url` →
// `deployment_status.logUrl` → `deployment.log_url` → `payload.log_url`.
// Each candidate is validated as it is read (https, no credentials, no
// whitespace, ≤2048 chars): unsafe candidates are skipped, so a safe fallback
// later in the chain still records, and a credentialed URL never reaches a
// record, a log line or the store. A delivery whose candidates are all unsafe
// records with `logUrl: null` plus a counted `unsafePointer` — never a thrown
// run, never a dropped intent, never a stored secret (watcher log lines carry
// repo@sha + decision + reason only, never the URL).
//
// Dedupe key: `deploy-fail:{repo}:{sha}` — one intent per key. Repeats are
// logged + counted, never duplicated. The environment rides in the body,
// never in the key, so a re-delivery for the same SHA adds nothing.
//
// Per-repo flag gate (§12, same pattern as S8/S9a): `enabledRepositories`,
// DEFAULT OFF (the empty set). Intent transitions persist only for listed
// repositories. Anywhere else the run logs `would-record` with counters and
// writes no state (shadow behavior). Rollback is the flag: remove the repo;
// recorded intents stay as audit history but nothing new is recorded.

const DEPLOY_OWNER_ROLE = /^[a-z][a-z0-9-]{1,40}$/
const MAX_LOG_URL = 2048
const FAILURE_STATES = new Set(['failure', 'error'])

/**
 * The deploy owner when no per-repo override names one. Shadow-only in this
 * slice (no task is created): the value is persisted on the intent so a later
 * promotion slice posts to the same owner without re-deciding routing.
 */
export const DEFAULT_DEPLOY_OWNER = 'deploy'

/**
 * Idempotent intent identity: one intent per (repo, sha). The environment and
 * the log pointer ride in the body, never in the key, so a re-delivery for
 * the same SHA adds nothing and a new SHA records fresh.
 */
export function deployFailKey(repository, sha) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'deploy-fail repository is invalid')
  requireValue(typeof sha === 'string' && SHA.test(sha), 'deploy-fail sha is invalid')
  return `deploy-fail:${repository}:${sha}`
}

/**
 * Validate and freeze the deploy-failure policy. Fails loudly. The enabled
 * set defaults to EMPTY (flag off everywhere): intent transitions must be
 * opted in per repo, never silently live.
 *
 * @param {{ enabledRepositories?: string[],
 *   deployOwners?: Record<string,string> }} policy
 */
export function deployFailPolicy(policy = {}) {
  requireValue(policy && typeof policy === 'object' && !Array.isArray(policy), 'deploy-fail policy is required')
  const { enabledRepositories = [], deployOwners = {} } = policy
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'enabledRepositories must be a list of unique owner/repo names')
  requireValue(deployOwners && typeof deployOwners === 'object' && !Array.isArray(deployOwners) &&
    Object.entries(deployOwners).every(([r, o]) => REPOSITORY.test(r) && typeof o === 'string' && DEPLOY_OWNER_ROLE.test(o)),
  'deployOwners must map owner/repo names to owner roles')
  return Object.freeze({
    enabledRepositories: Object.freeze([...enabledRepositories]),
    deployOwners: Object.freeze({ ...deployOwners }),
  })
}

/**
 * One constant assignee per repo: the per-repo override when present, else
 * DEFAULT_DEPLOY_OWNER. Never guesses from environment or payload text.
 */
export function resolveDeployOwner(policy, repository) {
  requireValue(policy && typeof policy.deployOwners === 'object', 'deploy-fail policy is invalid')
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'deploy-fail repository is invalid')
  return policy.deployOwners[repository] ?? DEFAULT_DEPLOY_OWNER
}

/**
 * The exact failure-state match rule. Only `failure` and `error`
 * (case-sensitive) are failures. `success` stays on the S9a path; every other
 * state is acknowledged ineligible. Non-strings are never failures.
 */
export function isFailureState(state) {
  return typeof state === 'string' && FAILURE_STATES.has(state)
}

function safeLogUrl(value) {
  if (value === null) return null
  if (typeof value !== 'string' || value.length === 0 || value.length > MAX_LOG_URL) return null
  if (/\s/.test(value)) return null
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    return null
  }
  if (parsed.protocol !== 'https:' || parsed.hostname.length === 0) return null
  if (parsed.username !== '' || parsed.password !== '') return null
  return value
}

function logPointerCandidates(payload) {
  const status = payload?.deployment_status
  const deployment = payload?.deployment
  return [
    status && typeof status === 'object' ? status.log_url : undefined,
    status && typeof status === 'object' ? status.target_url : undefined,
    status && typeof status === 'object' ? status.logUrl : undefined,
    deployment && typeof deployment === 'object' ? deployment.log_url : undefined,
    payload?.log_url,
  ]
}

/**
 * Tolerant log-pointer read across the documented chain. Returns the first
 * SAFE pointer (https, no credentials, no whitespace, ≤2048 chars), else
 * null. Each candidate is validated as it is read, so an unsafe first
 * candidate is skipped in favor of a safe fallback later in the chain, and a
 * credentialed URL can never reach a record, a log line or the store. Never
 * throws for a missing or all-unsafe pointer: absence is recorded as null
 * (with `logUrlUnsafe` on the verdict when candidates were dropped), never
 * as a malformed delivery.
 */
export function readDeployLogUrl(payload) {
  for (const candidate of logPointerCandidates(payload)) {
    if (typeof candidate === 'string' && candidate.trim().length > 0 &&
      safeLogUrl(candidate.trim()) !== null) return candidate.trim()
  }
  return null
}

/**
 * True when the delivery carries at least one non-empty pointer candidate,
 * safe or not. Compared against `readDeployLogUrl`: a present candidate with
 * a null read means every candidate was unsafe and the pointer was dropped.
 */
export function hasLogPointer(payload) {
  return logPointerCandidates(payload)
    .some((c) => typeof c === 'string' && c.trim().length > 0)
}

/**
 * Pure verdict over one parsed `deployment_status` webhook payload.
 *
 * Same tolerant environment read as S9a (`deployment.environment` →
 * `deployment_status.environment` → `payload.environment`), but the value is
 * RECORDED, never matched: staging and production both count. The state is
 * ALWAYS `payload.deployment_status.state` (exact `failure`/`error`);
 * `success` is acknowledged for the S9a path and anything else is
 * acknowledged non-failure — none of them is ever an intent. The SHA is
 * ALWAYS `payload.deployment.sha` (the commit the deploy points at).
 *
 * The log pointer is the first SAFE candidate (`readDeployLogUrl`); when
 * candidates were present but all unsafe, `logUrl` is null and `logUrlUnsafe`
 * is true so the watcher can count the drop. The pointer is optional: it never
 * affects eligibility and never drops the intent.
 *
 * @param {any} payload parsed webhook body, `event === 'deployment_status'`
 * @returns {{ eligible: boolean, repository: string, sha: string,
 *   environment: string, logUrl: string|null, logUrlUnsafe: boolean,
 *   state: string, key: string|null, reason: string }}
 */
export function decideDeployFailure(payload) {
  requireValue(payload && typeof payload === 'object' && !Array.isArray(payload), 'deployment_status payload is required')
  const repository = payload.repository?.full_name
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository),
    'deployment_status repository is invalid')
  const status = payload.deployment_status
  requireValue(status && typeof status === 'object' && !Array.isArray(status),
    'deployment_status status is missing')
  requireValue(typeof status.state === 'string' && status.state.length > 0,
    'deployment_status state is missing')
  const deployment = payload.deployment
  requireValue(deployment && typeof deployment === 'object' && !Array.isArray(deployment),
    'deployment_status deployment is missing')
  requireValue(typeof deployment.sha === 'string' && SHA.test(deployment.sha),
    'deployment_status sha is invalid')
  const rawEnvironment = deployment.environment ?? status.environment ?? payload.environment
  requireValue(typeof rawEnvironment === 'string' && rawEnvironment.trim().length > 0,
    'deployment_status environment is missing')
  const sha = deployment.sha
  const state = status.state
  const environment = rawEnvironment.trim()
  const logUrl = readDeployLogUrl(payload)
  const logUrlUnsafe = logUrl === null && hasLogPointer(payload)
  if (state === 'success') {
    return { eligible: false, repository, sha, environment, logUrl, logUrlUnsafe, state,
      key: null, reason: 'success-on-smoke-path' }
  }
  if (!isFailureState(state)) {
    return { eligible: false, repository, sha, environment, logUrl, logUrlUnsafe, state,
      key: null, reason: 'non-failure-state' }
  }
  return { eligible: true, repository, sha, environment, logUrl, logUrlUnsafe, state,
    key: deployFailKey(repository, sha), reason: 'deploy-failure' }
}

/**
 * Fixed-words intent text: the key, the SHA, the safe environment name, the
 * failure state, the log pointer (or its explicit absence) and the routed
 * owner. Titles, bodies and deploy prose are never accepted here at all.
 */
export function renderDeployFailIntent({ repository, sha, environment, logUrl = null, deployOwner, state }) {
  const key = deployFailKey(repository, sha)
  requireValue(typeof deployOwner === 'string' && DEPLOY_OWNER_ROLE.test(deployOwner), 'deploy-fail intent owner is invalid')
  requireValue(isFailureState(state), 'deploy-fail intent state is invalid')
  const env = safeEnvironment(environment)
  requireValue(env !== null, 'deploy-fail intent environment is unsafe')
  const url = safeLogUrl(logUrl)
  requireValue(logUrl === null || url !== null, 'deploy-fail intent log URL is unsafe')
  const pointer = url === null ? 'No log pointer.' : `Log: ${url}.`
  return `Deploy failure ${key}: deploy of \`${sha}\` in environment \`${env}\` reported \`${state}\`. ${pointer} Routed to ${deployOwner}.`
}

function emptyFailCounters() {
  return { total: 0, deliveries: 0, eligible: 0, recorded: 0, wouldRecord: 0,
    alreadyRecorded: 0, suppressed: 0, dropped: 0, nonFailure: 0,
    unsafePointer: 0, overBudget: 0, renderFailed: 0, errors: 0 }
}

/**
 * Dependencies: capture.listDeliveries({ repository, event }) +
 * capture.getDelivery(id), and a deploy-failure intent store with
 * findIntent/has + recordIntent ONLY. There is deliberately no board,
 * claim or disarm dependency: this duty records intents and renders their
 * texts but posts nothing, so it cannot wake, claim, disarm or create a
 * task. Suppression arrives as a pure predicate, never as a store handle.
 *
 * Capture interface (async):
 *   listDeliveries({ repository, event: 'deployment_status' }) -> rows
 *   getDelivery(id) -> { delivery_id, repository, event, body, body_truncated }
 * Store interface (async):
 *   findIntent(repository, sha) -> null | { key, repository, sha, ... }
 *   recordIntent({ key, repository, sha, environment, logUrl, firstSeenMs, deployOwner, body })
 */
export function createDeployFailWatcher({ capture, intents, policy, allowedRepositories,
  isSuppressed = null, nowMs = () => Date.now() }) {
  requireValue(capture && typeof capture.listDeliveries === 'function' &&
    typeof capture.getDelivery === 'function',
  'deploy-fail watcher requires a capture reader with list/get deliveries, and nothing else')
  requireValue(intents && typeof intents.findIntent === 'function' &&
    typeof intents.recordIntent === 'function',
  'deploy-fail watcher requires an intent store with find/record, and nothing else')
  const validated = deployFailPolicy(policy)
  requireValue(Array.isArray(allowedRepositories) && allowedRepositories.length > 0 &&
    allowedRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(allowedRepositories).size === allowedRepositories.length,
  'deploy-fail watcher requires an explicit unique repository allowlist')
  requireValue(isSuppressed === null || typeof isSuppressed === 'function',
    'deploy-fail suppression predicate is invalid')
  requireValue(typeof nowMs === 'function', 'deploy-fail clock is invalid')
  const allowed = new Set(allowedRepositories)
  const enabled = new Set(validated.enabledRepositories)

  /**
   * Run the duty over explicit repositories. Non-allowlisted repos are
   * dropped before ANY read or write. One unreadable repo or one
   * undecidable delivery is a counted error record, never a thrown run.
   * Flag-off repos transition nothing (shadow behavior).
   *
   * Records carry { repository, sha, environment, logUrl, logUrlUnsafe, state,
   * key, decision, reason, log, body? } with decision one of 'recorded' |
   * 'would-record' | 'already-recorded' | 'suppressed' | 'dropped' |
   * 'ineligible' | 'none'. `unsafePointer` counts eligible deliveries whose
   * log-pointer candidates were all unsafe (recorded with `logUrl: null`).
   */
  async function runDeployFail({ repositories, sinceMs = undefined, nowMs: nowOverride } = {}) {
    requireValue(Array.isArray(repositories) && repositories.length <= MAX_WATCH_REPOS &&
      repositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
      new Set(repositories).size === repositories.length,
    'deploy-fail repositories are invalid')
    const now = nowOverride ?? nowMs()
    requireValue(isMs(now), 'deploy-fail clock is invalid')
    // Omitted means the default recent window; explicit null means unbounded
    // (backfill only — past the adapter page budget it fails loud per repo).
    // Clamped at 0: a clock earlier than one window (tests) scans everything
    // rather than failing on its own default.
    const windowStart = sinceMs === undefined
      ? Math.max(0, now - DEFAULT_HISTORY_WINDOW_MS)
      : sinceMs
    requireValue(windowStart === null || (Number.isInteger(windowStart) && windowStart >= 0 && windowStart <= now),
      'deploy-fail scan window is invalid')
    const records = []
    const counters = emptyFailCounters()
    for (const repository of repositories) {
      counters.total++
      // Dropped before any read or write: outside the relay scope entirely.
      if (!allowed.has(repository)) {
        counters.dropped++
        records.push({ repository, sha: null, environment: null, logUrl: null, state: null, key: null,
          decision: 'dropped', reason: 'outside-allowlist',
          log: logLine(repository, null, 'dropped', 'outside-allowlist') })
        continue
      }
      let rows
      try {
        rows = await capture.listDeliveries({ repository, event: 'deployment_status',
          ...(windowStart === null ? {} : { sinceMs: windowStart }) })
      } catch {
        records.push({ repository, sha: null, environment: null, logUrl: null, state: null, key: null,
          decision: 'none', reason: 'capture-list-failed',
          log: logLine(repository, null, 'none', 'capture-list-failed'),
          error: 'capture-list-failed' })
        counters.errors++
        continue
      }
      requireValue(Array.isArray(rows), 'deploy-fail capture listing is invalid')
      // Bounded blast radius: D1 never prunes deliveries, so history grows
      // without bound. Process the newest-first window the adapter returns up
      // to the cap and count the rest as over-budget skips with a loud log,
      // instead of throwing the whole run for every repo once one repo
      // crosses the cap.
      let scoped = rows
      const overBudget = rows.length - MAX_DELIVERIES_PER_REPO
      if (overBudget > 0) {
        scoped = rows.slice(0, MAX_DELIVERIES_PER_REPO)
        counters.overBudget += overBudget
        records.push({ repository, sha: null, environment: null, logUrl: null, state: null, key: null,
          decision: 'skipped', reason: `over-budget:${overBudget}-deliveries-unread`,
          log: logLine(repository, null, 'skipped', `over-budget:${overBudget}-deliveries-unread`) })
      }
      for (const row of scoped) {
        counters.deliveries++
        let delivery
        try {
          delivery = await capture.getDelivery(row.delivery_id)
        } catch {
          records.push({ repository, sha: null, environment: null, logUrl: null, state: null, key: null,
            decision: 'none', reason: 'capture-read-failed',
            log: logLine(repository, null, 'none', 'capture-read-failed'),
            error: 'capture-read-failed' })
          counters.errors++
          continue
        }
        let payload
        try {
          requireValue(delivery && delivery.body_truncated === 0 && typeof delivery.body === 'string',
            'deploy delivery body is incomplete')
          payload = JSON.parse(delivery.body)
        } catch {
          records.push({ repository, sha: null, environment: null, logUrl: null, state: null, key: null,
            decision: 'none', reason: 'undecidable-delivery',
            log: logLine(repository, null, 'none', 'undecidable-delivery'),
            error: 'undecidable-delivery' })
          counters.errors++
          continue
        }
        let verdict
        try {
          verdict = decideDeployFailure(payload)
        } catch {
          records.push({ repository, sha: null, environment: null, logUrl: null, state: null, key: null,
            decision: 'none', reason: 'undecidable-delivery',
            log: logLine(repository, null, 'none', 'undecidable-delivery'),
            error: 'undecidable-delivery' })
          counters.errors++
          continue
        }
        // The delivery names its own repo; a row scoped to one repo carrying
        // another repo's payload is dropped, never stored under the wrong key.
        if (verdict.repository !== repository) {
          counters.dropped++
          records.push({ repository, sha: verdict.sha, environment: verdict.environment,
            logUrl: verdict.logUrl, state: verdict.state, key: null, decision: 'dropped', reason: 'repository-mismatch',
            log: logLine(repository, verdict.sha, 'dropped', 'repository-mismatch') })
          continue
        }
        if (!verdict.eligible) {
          counters.nonFailure++
          records.push({ ...verdict, decision: 'ineligible',
            log: logLine(repository, verdict.sha, 'ineligible', verdict.reason) })
          continue
        }
        counters.eligible++
        // The pointer is optional: an all-unsafe chain records with null plus
        // this counter, never a dropped intent and never a stored secret.
        if (verdict.logUrlUnsafe) counters.unsafePointer++
        let suppressed = false
        if (isSuppressed !== null) {
          try {
            suppressed = isSuppressed(repository)
          } catch {
            throw new Error('suppression predicate failed; refusing to guess')
          }
          requireValue(typeof suppressed === 'boolean', 'suppression predicate must return a boolean')
        }
        // S8 interaction: while a red-main incident is open for the repo,
        // eligible intents are logged + counted, never stored.
        if (suppressed) {
          counters.suppressed++
          records.push({ ...verdict, decision: 'suppressed', reason: 'red-main-open',
            log: logLine(repository, verdict.sha, 'suppressed', 'red-main-open') })
          continue
        }
        const on = enabled.has(repository)
        const deployOwner = resolveDeployOwner(validated, repository)
        // An eligible delivery with an unruly environment name (an en dash,
        // over 64 characters) must be a counted per-delivery error, never a
        // thrown run: every run rescans history, so one such delivery would
        // otherwise fail every later run, in shadow mode too. (Log pointers
        // are pre-validated at verdict time, so the render/store URL checks
        // below are defense in depth that cannot trip on verdict output.)
        let body
        try {
          body = renderDeployFailIntent({ repository, sha: verdict.sha,
            environment: verdict.environment, logUrl: verdict.logUrl,
            deployOwner, state: verdict.state })
        } catch {
          counters.renderFailed++
          records.push({ ...verdict, body: null, decision: 'none', reason: 'intent-render-failed',
            log: logLine(repository, verdict.sha, 'none', 'intent-render-failed'),
            error: 'intent-render-failed' })
          counters.errors++
          continue
        }
        let existing
        try {
          existing = await intents.findIntent(repository, verdict.sha)
        } catch {
          records.push({ ...verdict, body, decision: 'none', reason: 'intent-store-unreadable',
            log: logLine(repository, verdict.sha, 'none', 'intent-store-unreadable'),
            error: 'intent-store-unreadable' })
          counters.errors++
          continue
        }
        requireValue(existing === null || (existing && existing.key === verdict.key),
        'deploy-fail intent store returned a foreign intent')
        // One intent per key: a repeat adds nothing — no new entry, no new
        // text, one counter. Checked BEFORE the flag so shadow runs prove
        // the dedupe without writing state.
        //
        // Rescan/prune contract: every run relists the full delivery
        // history, while the store keeps only the newest `maxEntries`
        // intents. An intent evicted by oldest-first pruning is therefore
        // re-recorded on the next run (counted `recorded` again, never
        // duplicated within the state). That is safe in this shadow slice
        // — no task and no wake ships — but a later live step MUST gate
        // creation on the same key (task index or verb idempotency), or a
        // pruned SHA would create a second deploy task.
        if (existing !== null) {
          counters.alreadyRecorded++
          records.push({ ...verdict, body, decision: 'already-recorded', reason: 'intent-already-recorded',
            log: logLine(repository, verdict.sha, 'already-recorded', 'intent-already-recorded') })
          continue
        }
        if (!on) {
          counters.wouldRecord++
          records.push({ ...verdict, body, decision: 'would-record', reason: 'flag-off-shadow',
            log: logLine(repository, verdict.sha, 'would-record', 'flag-off-shadow') })
          continue
        }
        try {
          await intents.recordIntent({ key: verdict.key, repository,
            sha: verdict.sha, environment: verdict.environment, logUrl: verdict.logUrl,
            firstSeenMs: now, deployOwner, body })
        } catch {
          records.push({ ...verdict, body, decision: 'none', reason: 'intent-record-failed',
            log: logLine(repository, verdict.sha, 'none', 'intent-record-failed'),
            error: 'intent-record-failed' })
          counters.errors++
          continue
        }
        counters.recorded++
        records.push({ ...verdict, body, decision: 'recorded', reason: `deploy-failure:${verdict.state}:${verdict.sha}`,
          log: logLine(repository, verdict.sha, 'recorded', `deploy-failure:${verdict.state}:${verdict.sha}`) })
      }
    }
    return { records, counters, sinceMs: windowStart }
  }

  return { runDeployFail }
}
