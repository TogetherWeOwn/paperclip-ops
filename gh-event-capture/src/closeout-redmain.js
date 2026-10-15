// Red-main incident path (design: docs/decisions/case_439e6875-pr-closeout-v2-design.md
// §5.6 + §9 slice S8, §12 flag gate). When the sweep sees the default branch
// red, the reconciler records exactly one fix incident per (repo, first red
// main SHA) and suppresses per-PR exception wakes for that repo while it is
// open. Main red outranks every other work in that repo, so the suppression is
// deliberate silence, not a missed wake: each suppressed decision is logged
// with a counter per (repo, PR, head, class).
//
// Detection source: the default-branch head commit's check rollup, read fresh
// from GitHub at sweep time (`github.getMainHeadChecks`, a GraphQL
// `defaultBranchRef.target.statusCheckRollup` instant, same shape as the S3a
// snapshot). NOT stored `workflow_run` / `check_suite` deliveries: per §5.1
// deliveries are hints and the decision is computed from a fresh read, so a
// lost, duplicated or reordered delivery costs latency, never a wrong
// incident. `Red` means a required check completed with a failing conclusion
// at the main head. Pending or absent checks are `pending`, never green:
// unobservable CI is not green, and a still-running main must neither open an
// incident nor recover one.
//
// Incident shape: the incident is a reconciler-side record with the idempotent
// key `main-red:{repo}` and the first red main SHA in the body, routed to the
// repo's CI owner. Board task creation and commenting ship with the
// issue-creation verb (S10 promotion): this slice persists the incident plus
// the exact fixed-words incident/recovery texts that promotion will post, the
// same split as S1 (which renders wake text) versus S5a (which posts it).
//
// Three pieces:
//
// 1. decideMainRed: the pure verdict over one main-head read. Red, green or
//    pending, with the failing/pending check names. Anything malformed throws:
//    a swallowed bad read would look exactly like "main is green".
// 2. createRedmainWatcher: the duty runner. Per repo it reads one main head,
//    decides, and either transitions the incident store (flag on) or records
//    would-open/would-recover (flag off). One unreadable repo is a counted
//    error record, never a thrown run.
// 3. applySuppression: the pure wake-suppression filter. Exception decisions
//    for a repo with an open incident resolve to suppressed + counted records.
//    It takes no board, capture or claim dependency, so it cannot post, claim
//    or disarm: suppression covers wakes ONLY, never disarm (S6b). A reviewer
//    can verify the exclusion by observing this module imports no disarm path
//    and the disarm duty never consults it.
//
// Per-repo flag gate (§12, same pattern as S6b): `enabledRepositories`,
// DEFAULT OFF (the empty set). Incident transitions persist only for listed
// repositories. Anywhere else the run logs `would-open` / `would-suppress`
// with counters and writes no state (shadow behavior). Rollback is the flag:
// remove the repo; an open incident stays recorded but stops suppressing once
// its repo leaves the enabled set only after it recovers (suppression consults
// the same flag, so removal unsuppresses immediately).
//
// Routing: `resolveCiOwner` keeps one constant assignee per repo. There is no
// existing single repo→CI-owner mapping to read (the required-checks ownership
// matrix names an owner per (repo, check), not per repo), so the default is
// named here: DEFAULT_CI_OWNER (`devops`), overridable per repo via
// `ciOwners`. The design names DevOps as the repo CI owner (§5.6).

import { CLOSEOUT_CLASSES } from './closeout.js'

// `.` and `..` are excluded so the rebuilt key can never contain a dot segment.
const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
// Only names that match this ever reach incident text; anything else is
// counted, never echoed (same floor as closeout.js SAFE_NAME).
const SAFE_NAME = /^[A-Za-z0-9 ._()/-]{1,64}$/
const OWNER_ROLE = /^[a-z][a-z0-9-]{1,40}$/
// GitHub treats skipped and neutral as satisfying a required check; the
// failing set matches closeout.js exactly.
const PASSING = new Set(['success', 'neutral', 'skipped'])
const FAILING = new Set(['failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure', 'stale'])
const MAX_WATCH_REPOS = 100
const MAX_SUPPRESS_DECISIONS = 10000
const MS_LIMIT = 8.64e15

/**
 * The repo's CI owner when no per-repo override names one. There is no
 * single repo→CI-owner mapping to read (the ownership matrix is per check),
 * so the design's standing owner (§5.6: the repo's CI owner, DevOps) is the
 * default, stated once here rather than scattered at call sites.
 */
export const DEFAULT_CI_OWNER = 'devops'

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isMs = (value) => Number.isInteger(value) && value >= 0 && value <= MS_LIMIT

/**
 * Idempotent incident identity: one open incident per repo. The first red main
 * SHA rides in the body, never in the key, so a second red SHA while one is
 * open adds nothing and a re-red after recovery opens fresh.
 */
export function mainRedIncidentKey(repository) {
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'red-main repository is invalid')
  return `main-red:${repository}`
}

/**
 * Validate and freeze the red-main policy. Fails loudly. The enabled set
 * defaults to EMPTY (flag off everywhere): incident transitions must be opted
 * in per repo, never silently live. The required-check set is NOT policy: it
 * rides on each head read (resolved per repo+branch from branch protection),
 * so a check that is not required on one repo can never block that repo's
 * green. A single global list would let a repo open red yet never recover.
 *
 * @param {{ enabledRepositories?: string[],
 *   ciOwners?: Record<string,string> }} policy
 */
export function redmainPolicy(policy = {}) {
  requireValue(policy && typeof policy === 'object' && !Array.isArray(policy), 'red-main policy is required')
  const { enabledRepositories = [], ciOwners = {} } = policy
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'enabledRepositories must be a list of unique owner/repo names')
  requireValue(ciOwners && typeof ciOwners === 'object' && !Array.isArray(ciOwners) &&
    Object.entries(ciOwners).every(([r, o]) => REPOSITORY.test(r) && typeof o === 'string' && OWNER_ROLE.test(o)),
  'ciOwners must map owner/repo names to owner roles')
  return Object.freeze({
    enabledRepositories: Object.freeze([...enabledRepositories]),
    ciOwners: Object.freeze({ ...ciOwners }),
  })
}

function validateRequiredChecks(names) {
  requireValue(Array.isArray(names) && names.length > 0 &&
    names.every((n) => typeof n === 'string' && SAFE_NAME.test(n)) &&
    new Set(names).size === names.length,
  'requiredChecks must be a non-empty list of unique, safe check names')
  return [...names]
}

/**
 * One constant assignee per repo: the per-repo override when present, else
 * DEFAULT_CI_OWNER. Never guesses from check names or PR text.
 */
export function resolveCiOwner(policy, repository) {
  requireValue(policy && typeof policy.ciOwners === 'object', 'red-main policy is invalid')
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'red-main repository is invalid')
  return policy.ciOwners[repository] ?? DEFAULT_CI_OWNER
}

function validateMainHead(head) {
  requireValue(head && typeof head === 'object' && !Array.isArray(head), 'red-main head read is required')
  requireValue(typeof head.repository === 'string' && REPOSITORY.test(head.repository),
    'red-main head repository is invalid')
  requireValue(typeof head.branch === 'string' && head.branch.length > 0 && head.branch.length <= 255,
    'red-main head branch is invalid')
  requireValue(typeof head.mainSha === 'string' && SHA.test(head.mainSha), 'red-main head sha is invalid')
  // The required set is per repo+branch (branch protection), validated like a
  // policy: a typo that silently required nothing would turn red main green.
  validateRequiredChecks(head.requiredChecks)
  requireValue(Array.isArray(head.checks), 'red-main head checks are missing')
  const names = new Set()
  for (const c of head.checks) {
    requireValue(c && typeof c.name === 'string' && c.name.length > 0 &&
      (c.status === 'queued' || c.status === 'in_progress' || c.status === 'completed'),
    'red-main head check is malformed')
    requireValue(!names.has(c.name), 'red-main head has duplicate check names')
    names.add(c.name)
    if (c.status === 'completed') {
      requireValue(typeof c.conclusion === 'string' && c.conclusion.length > 0,
        'completed red-main check has no conclusion')
    }
  }
}

/**
 * @param {object} head one authoritative main-head read (see validateMainHead)
 * @param {ReturnType<typeof redmainPolicy>} policy
 * @returns {{ state: 'red'|'green'|'pending', repository, branch, mainSha,
 *   failedChecks: string[], pendingChecks: string[] }}
 */
export function decideMainRed(head, policy) {
  validateMainHead(head)
  redmainPolicy(policy)
  const byName = new Map(head.checks.map((c) => [c.name, c]))
  const failed = []
  const pending = []
  for (const name of head.requiredChecks) {
    const check = byName.get(name)
    // Absent is pending, not green: a required check that never reported on
    // main is unobservable CI, and unobservable CI must neither open an
    // incident nor recover one.
    if (!check) { pending.push(name); continue }
    if (check.status !== 'completed') { pending.push(name); continue }
    if (FAILING.has(check.conclusion)) { failed.push(name); continue }
    // An unrecognised conclusion is neither a pass nor a known failure. Treat
    // it as pending rather than guess; the next sweep reads again.
    if (PASSING.has(check.conclusion)) continue
    pending.push(name)
  }
  if (failed.length > 0) {
    return { state: 'red', repository: head.repository, branch: head.branch,
      mainSha: head.mainSha, failedChecks: [...failed].sort(), pendingChecks: [] }
  }
  if (pending.length > 0) {
    return { state: 'pending', repository: head.repository, branch: head.branch,
      mainSha: head.mainSha, failedChecks: [], pendingChecks: [...pending].sort() }
  }
  return { state: 'green', repository: head.repository, branch: head.branch,
    mainSha: head.mainSha, failedChecks: [], pendingChecks: [] }
}

function safeNames(names) {
  return names.filter((n) => typeof n === 'string' && SAFE_NAME.test(n))
}

/**
 * Fixed-words incident text: the key, the first red SHA, the failing checks
 * that passed SAFE_NAME, and the routed owner. Titles, bodies and review prose
 * are never accepted here at all.
 */
export function renderRedmainIncident({ repository, mainSha, failedChecks = [], ciOwner }) {
  const key = mainRedIncidentKey(repository)
  requireValue(typeof mainSha === 'string' && SHA.test(mainSha), 'red-main incident sha is invalid')
  requireValue(typeof ciOwner === 'string' && OWNER_ROLE.test(ciOwner), 'red-main incident owner is invalid')
  const names = safeNames(failedChecks)
  const checks = names.length > 0 ? ` Failing required checks: ${names.map((n) => `\`${n}\``).join(', ')}.` : ''
  return `Red main incident ${key}: default branch is red at \`${mainSha}\`.${checks} Routed to ${ciOwner}.`
}

/**
 * Fixed-words recovery text: the key, the recovered green SHA and the SHA the
 * incident opened on. One comment on green, then the incident is eligible for
 * close; a later red SHA opens a fresh incident under the same key.
 */
export function renderRedmainRecovery({ repository, firstRedSha, recoveredSha, ciOwner }) {
  const key = mainRedIncidentKey(repository)
  requireValue(typeof firstRedSha === 'string' && SHA.test(firstRedSha), 'red-main recovery first-red sha is invalid')
  requireValue(typeof recoveredSha === 'string' && SHA.test(recoveredSha), 'red-main recovery sha is invalid')
  requireValue(typeof ciOwner === 'string' && OWNER_ROLE.test(ciOwner), 'red-main recovery owner is invalid')
  return `Red main incident ${key} recovered: default branch is green at \`${recoveredSha}\` (incident opened on \`${firstRedSha}\`). Routed to ${ciOwner} for closeout.`
}

function emptyCounters() {
  return { total: 0, evaluated: 0, red: 0, green: 0, pending: 0, opened: 0,
    wouldOpen: 0, alreadyOpen: 0, recovered: 0, wouldRecover: 0, noOp: 0, errors: 0 }
}

function logLine(repository, mainSha, decision, reason) {
  return `closeout-redmain ${repository}@${mainSha}: ${decision} (${reason})`
}

/**
 * Dependencies: github.getMainHeadChecks(repo) and a red-main incident store
 * with findOpen/openIncident/closeIncident ONLY. There is deliberately no
 * board, capture or claim dependency: this duty records incidents and renders
 * their texts but posts nothing, so it cannot wake, claim or double-post.
 *
 * Store interface (async):
 *   findOpen(repository) -> null | { key, repository, firstRedSha, openedMs, ciOwner, status }
 *   openIncident({ key, repository, firstRedSha, openedMs, ciOwner, body })
 *   closeIncident(key, { recoveredSha, recoveredMs, recoveryBody })
 */
export function createRedmainWatcher({ github, incidents, policy, nowMs = () => Date.now() }) {
  requireValue(github && typeof github.getMainHeadChecks === 'function',
    'red-main watcher requires a main-head reader')
  requireValue(incidents && typeof incidents.findOpen === 'function' &&
    typeof incidents.openIncident === 'function' && typeof incidents.closeIncident === 'function',
  'red-main watcher requires an incident store with find/open/close, and nothing else')
  const validated = redmainPolicy(policy)
  const enabled = new Set(validated.enabledRepositories)
  requireValue(typeof nowMs === 'function', 'red-main clock is invalid')

  /**
   * Run the duty over explicit repositories. One unreadable or undecidable
   * repo is a counted error record, never a thrown run: one bad repo must not
   * blind the rest. Flag-off repos transition nothing (shadow behavior).
   *
   * Records carry { repository, branch, mainSha, key, decision, reason, log,
   * body? } with decision one of 'opened' | 'would-open' | 'already-open' |
   * 'recovered' | 'would-recover' | 'none'.
   */
  async function runRedmain({ repositories, nowMs: nowOverride } = {}) {
    requireValue(Array.isArray(repositories) && repositories.length <= MAX_WATCH_REPOS &&
      repositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
      new Set(repositories).size === repositories.length,
    'red-main repositories are invalid')
    const now = nowOverride ?? nowMs()
    requireValue(isMs(now), 'red-main clock is invalid')
    const records = []
    const counters = emptyCounters()
    for (const repository of repositories) {
      counters.total++
      const key = mainRedIncidentKey(repository)
      let head
      try {
        head = await github.getMainHeadChecks(repository)
      } catch {
        // No transport detail is stored: child streams may carry credentials
        // or private bodies, and the adapter already reports a fixed string.
        records.push({ repository, branch: null, mainSha: null, key, decision: 'none',
          reason: 'main-head-read-failed', log: logLine(repository, 'unknown', 'none', 'main-head-read-failed'),
          error: 'main-head-read-failed' })
        counters.errors++
        continue
      }
      let verdict
      try {
        verdict = decideMainRed(head, validated)
      } catch {
        records.push({ repository, branch: head.branch ?? null, mainSha: head.mainSha ?? null, key,
          decision: 'none', reason: 'undecidable-head',
          log: logLine(repository, head.mainSha ?? 'unknown', 'none', 'undecidable-head'),
          error: 'undecidable-head' })
        counters.errors++
        continue
      }
      counters.evaluated++
      const on = enabled.has(repository)
      if (verdict.state === 'pending') {
        counters.pending++
        records.push({ ...verdict, key, decision: 'none', reason: 'main-pending',
          log: logLine(repository, verdict.mainSha, 'none', 'main-pending') })
        continue
      }
      let open
      try {
        open = await incidents.findOpen(repository)
      } catch {
        records.push({ ...verdict, key, decision: 'none', reason: 'incident-store-unreadable',
          log: logLine(repository, verdict.mainSha, 'none', 'incident-store-unreadable'),
          error: 'incident-store-unreadable' })
        counters.errors++
        continue
      }
      requireValue(open === null || (open && open.key === key && open.status === 'open' &&
        typeof open.firstRedSha === 'string' && SHA.test(open.firstRedSha)),
      'red-main incident store returned a foreign or malformed open incident')
      if (verdict.state === 'red') {
        counters.red++
        // Exactly one open incident per repo: a second red SHA while one is
        // open adds nothing — no new incident, no new text, one counter.
        if (open !== null) {
          counters.alreadyOpen++
          records.push({ ...verdict, key, decision: 'already-open', reason: 'incident-already-open',
            log: logLine(repository, verdict.mainSha, 'already-open', `open-since:${open.firstRedSha}`) })
          continue
        }
        const ciOwner = resolveCiOwner(validated, repository)
        const body = renderRedmainIncident({ repository, mainSha: verdict.mainSha,
          failedChecks: verdict.failedChecks, ciOwner })
        if (!on) {
          counters.wouldOpen++
          records.push({ ...verdict, key, decision: 'would-open', reason: 'flag-off-shadow', body,
            log: logLine(repository, verdict.mainSha, 'would-open', 'flag-off-shadow') })
          continue
        }
        try {
          await incidents.openIncident({ key, repository, firstRedSha: verdict.mainSha,
            openedMs: now, ciOwner, body })
        } catch {
          records.push({ ...verdict, key, decision: 'none', reason: 'incident-open-failed',
            log: logLine(repository, verdict.mainSha, 'none', 'incident-open-failed'),
            error: 'incident-open-failed' })
          counters.errors++
          continue
        }
        counters.opened++
        records.push({ ...verdict, key, decision: 'opened', reason: `first-red:${verdict.mainSha}`, body,
          log: logLine(repository, verdict.mainSha, 'opened', `first-red:${verdict.mainSha}`) })
        continue
      }
      // Green.
      counters.green++
      if (open === null) {
        counters.noOp++
        records.push({ ...verdict, key, decision: 'none', reason: 'main-green-no-incident',
          log: logLine(repository, verdict.mainSha, 'none', 'main-green-no-incident') })
        continue
      }
      const recoveryBody = renderRedmainRecovery({ repository, firstRedSha: open.firstRedSha,
        recoveredSha: verdict.mainSha, ciOwner: open.ciOwner })
      if (!on) {
        counters.wouldRecover++
        records.push({ ...verdict, key, decision: 'would-recover', reason: 'flag-off-shadow',
          body: recoveryBody,
          log: logLine(repository, verdict.mainSha, 'would-recover', 'flag-off-shadow') })
        continue
      }
      try {
        await incidents.closeIncident(key, { recoveredSha: verdict.mainSha, recoveredMs: now,
          recoveryBody })
      } catch {
        records.push({ ...verdict, key, decision: 'none', reason: 'incident-recover-failed',
          log: logLine(repository, verdict.mainSha, 'none', 'incident-recover-failed'),
          error: 'incident-recover-failed' })
        counters.errors++
        continue
      }
      counters.recovered++
      records.push({ ...verdict, key, decision: 'recovered', reason: `recovered-at:${verdict.mainSha}`,
        body: recoveryBody,
        log: logLine(repository, verdict.mainSha, 'recovered', `recovered-at:${verdict.mainSha}`) })
    }
    return { records, counters }
  }

  return { runRedmain }
}

function validateExceptionDecision(d) {
  requireValue(d && typeof d === 'object' && !Array.isArray(d), 'suppression decision is required')
  requireValue(typeof d.repository === 'string' && REPOSITORY.test(d.repository),
    'suppression decision repository is invalid')
  requireValue(Number.isSafeInteger(d.number) && d.number > 0 && d.number <= 2147483647,
    'suppression decision PR number is invalid')
  requireValue(typeof d.headSha === 'string' && SHA.test(d.headSha), 'suppression decision head sha is invalid')
  requireValue(CLOSEOUT_CLASSES.includes(d.class), 'suppression decision class is unknown')
}

/**
 * Suppress per-PR exception wakes while the repo's red-main incident is open.
 * Pure: decisions in, records + counters out. `merged` and `closed_unmerged`
 * fall under the same suppression (the daily digest still lists
 * merged-but-task-open, §5.4). Takes no board/capture/claim/disarm dependency,
 * so a suppressed decision can never wake, claim — or disarm. Disarm (§2/S6b)
 * still runs: never consult this filter on the disarm path.
 *
 * @param {Array<{repository, number, headSha, class}>} decisions exception
 *   decisions the reconciler would otherwise wake on.
 * @param {(repository: string) => boolean} isSuppressed true when the repo's
 *   `main-red:{repo}` incident is open AND the repo flag is on.
 * @returns {{ records, counters }} records carry { repository, number,
 *   headSha, class, suppressed, log }; counters { total, suppressed, passed,
 *   byClass }.
 */
export function applySuppression(decisions, isSuppressed) {
  requireValue(Array.isArray(decisions) && decisions.length <= MAX_SUPPRESS_DECISIONS,
    'suppression decisions are invalid')
  requireValue(typeof isSuppressed === 'function', 'suppression predicate is required')
  const seen = new Set()
  const records = []
  const counters = { total: 0, suppressed: 0, passed: 0, byClass: {} }
  for (const d of decisions) {
    validateExceptionDecision(d)
    const key = JSON.stringify(['closeout', d.repository, d.number, d.headSha, d.class])
    requireValue(!seen.has(key), 'suppression decisions hold a duplicate (repo, PR, head, class)')
    seen.add(key)
    counters.total++
    let suppressed
    try {
      suppressed = isSuppressed(d.repository)
    } catch {
      throw new Error('suppression predicate failed; refusing to guess')
    }
    requireValue(typeof suppressed === 'boolean', 'suppression predicate must return a boolean')
    if (suppressed) {
      counters.suppressed++
      counters.byClass[d.class] = (counters.byClass[d.class] ?? 0) + 1
      records.push({ ...d, suppressed: true,
        log: `closeout-redmain ${d.repository}#${d.number}@${d.headSha}: suppressed (${d.class})` })
    } else {
      counters.passed++
      records.push({ ...d, suppressed: false,
        log: `closeout-redmain ${d.repository}#${d.number}@${d.headSha}: pass (${d.class})` })
    }
  }
  return { records, counters }
}
