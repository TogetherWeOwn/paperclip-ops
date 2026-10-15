// Disarm-only reconciler duty (design: docs/decisions/case_439e6875-pr-closeout-v2-design.md
// §2 + §4 + §12, slice S6b). When a read sees a drafted or `hold`-labeled PR
// with auto-merge armed, run `gh pr merge --disable-auto`. The reconciler
// never merges and never arms, in every phase: this module takes only a
// snapshot reader and a disarm writer, so there is no arm or merge call path
// to mistake. A reviewer can verify this by observing the injected
// dependencies: `getCloseoutSnapshot` and `disableAutoMerge` only.
//
// Two pieces:
//
// 1. decideDisarm: the pure decision. Inputs are the S3a snapshot fields
//    (draft boolean, labels, auto-merge armed flag). Armed + (draft OR hold)
//    => disarm; anything else => null (no-op).
// 2. createDisarmer: the duty runner. Per open PR it reads one authoritative
//    snapshot, decides, and either disarms (flag on) or records would-disarm
//    (flag off). It returns per-PR records plus aggregate counters; the caller
//    logs them. Either way one decision + counter per (repo, PR, head).
//
// Per-repo flag gate (§12): `enabledRepositories`, DEFAULT OFF (the empty
// set). Disarm executes only for listed repositories. When a repo is not
// listed, the run logs `would-disarm` with a counter and makes no gh call
// (shadow behavior). Rollback is the flag: remove the repo, then
// `gh pr merge --disable-auto` across its armed PRs.
//
// Disarm bypasses the S3b debounce BY CONSTRUCTION: this module owns no
// pending store and consults none. It acts on the first read that sees the
// hold, and the repair sweep repeats it by re-running the duty (disabling
// twice is safe, so the repeat is idempotent).
//
// THE RACE, STATED HONESTLY (§2): a `hold` added after the final required
// check goes green loses to GitHub's merge. GitHub merges in seconds and the
// reconciler's next read cannot win that race, so disarm here is best-effort.
// A `hold` must be set BEFORE the final check passes. When the hold has to be
// certain, convert the PR to a DRAFT instead: GitHub itself disarms auto-merge
// on a draft, with no reconciler in the path. Do not claim otherwise.

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
const MAX_DISARM_REFS = 10000

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isRef = (value) => value && typeof value === 'object' && !Array.isArray(value) &&
  typeof value.repository === 'string' && REPOSITORY.test(value.repository) &&
  Number.isSafeInteger(value.number) && value.number > 0 && value.number <= 2147483647

/**
 * Validate and freeze the disarm policy. Fails loudly. The enabled set
 * defaults to EMPTY (flag off everywhere): disarm must be opted in per repo,
 * never silently live.
 *
 * @param {{ enabledRepositories?: string[], holdLabels?: string[] }} policy
 */
export function disarmPolicy(policy = {}) {
  requireValue(policy && typeof policy === 'object' && !Array.isArray(policy), 'disarm policy is required')
  const { enabledRepositories = [], holdLabels = ['hold'] } = policy
  requireValue(Array.isArray(enabledRepositories) &&
    enabledRepositories.every((r) => typeof r === 'string' && REPOSITORY.test(r)) &&
    new Set(enabledRepositories).size === enabledRepositories.length,
  'enabledRepositories must be a list of unique owner/repo names')
  requireValue(Array.isArray(holdLabels) && holdLabels.every((l) => typeof l === 'string' && l.length > 0),
    'holdLabels must be a list of label names')
  return Object.freeze({
    enabledRepositories: Object.freeze([...enabledRepositories]),
    holdLabels: Object.freeze([...holdLabels]),
  })
}

function validateDisarmSnapshot(s) {
  requireValue(s && typeof s === 'object' && !Array.isArray(s), 'disarm snapshot is required')
  requireValue(typeof s.repository === 'string' && REPOSITORY.test(s.repository), 'disarm snapshot repository is invalid')
  requireValue(Number.isSafeInteger(s.number) && s.number > 0, 'disarm snapshot PR number is invalid')
  requireValue(typeof s.headSha === 'string' && SHA.test(s.headSha), 'disarm snapshot head sha is invalid')
  requireValue(s.state === 'open' || s.state === 'closed', 'disarm snapshot state is unknown')
  // An explicit boolean is "known"; a missing field is a dropped read, which
  // must throw rather than read as unarmed (silent) or armed (spurious call).
  requireValue(typeof s.draft === 'boolean' && typeof s.autoMerge === 'boolean',
    'disarm snapshot lifecycle is incomplete')
  requireValue(Array.isArray(s.labels) && s.labels.every((l) => typeof l === 'string'),
    'disarm snapshot labels are invalid')
}

/**
 * @param {object} snapshot one authoritative S3a closeout snapshot
 * @param {ReturnType<typeof disarmPolicy>} policy
 * @returns {null | { repository, number, headSha, drafted, held }}
 *   a disarm verdict, or null when there is nothing to disarm.
 */
export function decideDisarm(snapshot, policy) {
  validateDisarmSnapshot(snapshot)
  requireValue(policy && Array.isArray(policy.holdLabels), 'disarm policy is invalid')
  if (snapshot.state !== 'open') return null
  if (snapshot.autoMerge !== true) return null
  const held = snapshot.labels.some((l) => policy.holdLabels.includes(l))
  if (snapshot.draft !== true && !held) return null
  return {
    repository: snapshot.repository,
    number: snapshot.number,
    headSha: snapshot.headSha,
    drafted: snapshot.draft === true,
    held,
  }
}

function emptyCounters() {
  return { total: 0, evaluated: 0, needsDisarm: 0, disarmed: 0, wouldDisarm: 0, noOp: 0, errors: 0 }
}

// Fixed words plus validated repo/number/head only. Label names, titles and
// bodies are never echoed: they are collaborator- or fork-controlled text.
function logLine(repository, number, headSha, decision, reason) {
  return `closeout-disarm ${repository}#${number}@${headSha}: ${decision} (${reason})`
}

/**
 * Dependencies: github.getCloseoutSnapshot(repo, number, { nowMs }) and
 * github.disableAutoMerge(repo, number) ONLY. There is deliberately no arm
 * and no merge dependency: this duty cannot perform either operation even
 * if asked to.
 */
export function createDisarmer({ github, policy, nowMs = () => Date.now() }) {
  requireValue(github && typeof github.getCloseoutSnapshot === 'function' &&
    typeof github.disableAutoMerge === 'function',
  'disarm duty requires a snapshot reader and a disarm writer, and nothing else')
  const validated = disarmPolicy(policy)
  const enabled = new Set(validated.enabledRepositories)
  requireValue(typeof nowMs === 'function', 'disarm clock is invalid')

  /**
   * Run the duty over explicit open-PR references. No debounce: the first
   * read that sees an armed held/drafted PR disarms it. Re-running repeats
   * the disarm idempotently. One unreadable or undecidable PR is a counted
   * error record, never a thrown run: one bad PR must not blind the rest.
   *
   * @returns {{ records, counters }} records carry { repository, number,
   *   headSha, decision: 'disarmed' | 'would-disarm' | 'none', reason, log }.
   */
  async function runDisarm({ references, nowMs: nowOverride } = {}) {
    requireValue(Array.isArray(references) && references.length <= MAX_DISARM_REFS,
      'disarm references are invalid')
    const now = nowOverride ?? nowMs()
    requireValue(Number.isInteger(now) && now >= 0, 'disarm clock is invalid')
    const seen = new Set()
    const records = []
    const counters = emptyCounters()
    for (const ref of references) {
      requireValue(isRef(ref), 'disarm reference is invalid')
      const key = `${ref.repository}#${ref.number}`
      requireValue(!seen.has(key), 'disarm references hold a duplicate PR')
      seen.add(key)
      counters.total++
      let snapshot
      try {
        snapshot = await github.getCloseoutSnapshot(ref.repository, ref.number, { nowMs: now })
      } catch {
        // No transport detail is stored: child streams may carry credentials
        // or private bodies, and the adapter already reports a fixed string.
        records.push({ ...ref, headSha: null, decision: 'none', reason: 'snapshot-read-failed',
          log: logLine(ref.repository, ref.number, 'unknown', 'none', 'snapshot-read-failed'),
          error: 'snapshot-read-failed' })
        counters.errors++
        continue
      }
      let verdict
      try {
        verdict = decideDisarm(snapshot, validated)
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha ?? null, decision: 'none',
          reason: 'undecidable-snapshot',
          log: logLine(ref.repository, ref.number, snapshot.headSha ?? 'unknown', 'none', 'undecidable-snapshot'),
          error: 'undecidable-snapshot' })
        counters.errors++
        continue
      }
      counters.evaluated++
      if (verdict === null) {
        const reason = snapshot.state !== 'open' ? 'closed'
          : snapshot.autoMerge !== true ? 'unarmed' : 'clean'
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'none', reason,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'none', reason) })
        counters.noOp++
        continue
      }
      counters.needsDisarm++
      const reason = verdict.drafted && verdict.held ? 'armed-drafted-held'
        : verdict.drafted ? 'armed-drafted' : 'armed-held'
      if (!enabled.has(ref.repository)) {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'would-disarm', reason,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'would-disarm', reason) })
        counters.wouldDisarm++
        continue
      }
      try {
        await github.disableAutoMerge(ref.repository, ref.number)
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'none', reason: 'disarm-failed',
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'none', 'disarm-failed'),
          error: 'disarm-failed' })
        counters.errors++
        continue
      }
      records.push({ ...ref, headSha: snapshot.headSha, decision: 'disarmed', reason,
        log: logLine(ref.repository, ref.number, snapshot.headSha, 'disarmed', reason) })
      counters.disarmed++
    }
    return { records, counters }
  }

  return { runDisarm }
}
