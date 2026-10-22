// Host-side readiness reconcile duty (case_4b213922, under case_2eafff8f).
//
// Per open PR it reads one authoritative snapshot (getReadinessSnapshot: lifecycle,
// head SHA, reviewDecision, mergeStateStatus, autoMergeRequest, head
// statusCheckRollup), resolves required checks from the base-branch RULESETS
// (never the payload, never legacy branch protection which 404s on
// ruleset-only repos), and applies the pure decisions in src/readiness.js:
// maintain the bot-maintained block, mark-ready on green, draft-reset on a new
// head or a completed failure, and arm auto-merge only when CI and the
// Paperclip Review check entry pass on THIS head.
//
// HOST-SIDE BY CONSTRUCTION, not in the Worker. The Worker (app.js) is a
// capture-only store behind an unauthenticated webhook route: giving it an
// installation token that can mark ready, convert to draft and arm auto-merge
// would put merge mutations one forged-delivery bug away from anyone holding
// the webhook secret. This duty runs on the host beside the consumer, through
// the approved gh credential path, over explicit open-PR references — the same
// shape as closeout-disarm.js. Lost, duplicated or reordered deliveries cost
// latency, never correctness: every pass re-reads before it writes.
//
// FAIL CLOSED. A malformed readiness block (start with no later end, end with
// no earlier start, either marker twice) is logged and skipped: writing on top
// would duplicate the block (each write fires `edited`, looping the next pass)
// or swallow author text. A snapshot without a body string is undecidable, not
// "no block". A held PR keeps its block maintained but takes no transition:
// arming auto-merge under a hold would fight the disarm duty, which would
// immediately disarm it again.

import { snapshotFailureCode } from './github-adapter.js'
import {
  REVIEW_CHECK,
  buildReadinessBlock,
  parseReadinessBlock,
  upsertReadinessBlock,
  hasMalformedReadinessMarkers,
  shouldMarkReady,
  shouldConvertToDraft,
  shouldEnableAutoMerge,
} from './readiness.js'

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/
const SHA = /^[a-f0-9]{40}$/
const MAX_RECONCILE_REFS = 10000
const MS_LIMIT = 8.64e15

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const isRef = (value) => value && typeof value === 'object' && !Array.isArray(value) &&
  typeof value.repository === 'string' && REPOSITORY.test(value.repository) &&
  Number.isSafeInteger(value.number) && value.number > 0 && value.number <= 2147483647

/**
 * Validate and freeze the readiness policy. Fails loudly. The enabled set
 * defaults to EMPTY (flag off everywhere): the gate must be opted in per repo,
 * never silently live — same rule as the disarm duty. Rollback is the flag:
 * remove the repo; already-armed auto-merges stay armed until the disarm duty
 * or a human stands them down, which the records name.
 *
 * @param {{ enabledRepositories?: string[], holdLabels?: string[] }} policy
 */
export function readinessPolicy(policy = {}) {
  requireValue(policy && typeof policy === 'object' && !Array.isArray(policy), 'readiness policy is required')
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

// The review score line is display only: the auto-merge gate binds the verdict
// through the Paperclip Review CHECK ENTRY on this head (shouldEnableAutoMerge),
// never through this string, which could otherwise carry a 5/5 from an earlier
// head while the check on the current head is failing.
function reviewScoreFor(snapshot, checks) {
  const review = checks.find((c) => c && c.name === REVIEW_CHECK)
  if (!review) return null
  const status = String(review.status ?? '').toLowerCase()
  if (status !== 'completed') return null
  const conclusion = String(review.conclusion ?? '').toLowerCase()
  if (conclusion === 'success' || conclusion === 'neutral' || conclusion === 'skipped') return '5/5'
  if (typeof review.conclusion === 'string' && review.conclusion.length > 0) return 'see check'
  return null
}

function validateDecideInput(input) {
  requireValue(input && typeof input === 'object' && !Array.isArray(input), 'readiness input is required')
  const { snapshot, body, requiredChecks, nowMs } = input
  requireValue(snapshot && typeof snapshot === 'object', 'readiness snapshot is required')
  requireValue(typeof snapshot.repository === 'string' && REPOSITORY.test(snapshot.repository),
    'readiness snapshot repository is invalid')
  requireValue(Number.isSafeInteger(snapshot.number) && snapshot.number > 0, 'readiness snapshot PR number is invalid')
  requireValue(typeof snapshot.headSha === 'string' && SHA.test(snapshot.headSha), 'readiness snapshot head sha is invalid')
  requireValue(snapshot.state === 'open' || snapshot.state === 'closed', 'readiness snapshot state is unknown')
  requireValue(typeof snapshot.draft === 'boolean' && typeof snapshot.autoMerge === 'boolean',
    'readiness snapshot lifecycle is incomplete')
  requireValue(typeof snapshot.mergeableState === 'string' && snapshot.mergeableState.length > 0,
    'readiness snapshot mergeable state is missing')
  requireValue(Array.isArray(snapshot.checks), 'readiness snapshot checks are missing')
  requireValue(typeof body === 'string', 'readiness PR body is unavailable')
  requireValue(Array.isArray(requiredChecks) && requiredChecks.length > 0, 'required checks are required')
  requireValue(Number.isInteger(nowMs) && nowMs >= 0 && nowMs <= MS_LIMIT, 'readiness clock is invalid')
}

/**
 * Pure decision: what the duty should do to one PR. No I/O.
 *
 * @returns {{ repository, number, headSha, block, nextBody, blockHeadSha, malformed,
 *   held, closed, actions: Array<'update-body' | 'mark-ready' | 'convert-to-draft' | 'enable-automerge'>,
 *   reason }}
 */
export function decideReadinessActions(input, policy) {
  validateDecideInput(input)
  requireValue(policy && Array.isArray(policy.holdLabels), 'readiness policy is invalid')
  const { snapshot, body, requiredChecks, nowMs } = input
  const { repository, number, headSha } = snapshot
  const base = { repository, number, headSha, block: null, nextBody: null, blockHeadSha: null,
    malformed: false, held: false, closed: snapshot.state !== 'open', actions: [] }

  if (snapshot.state !== 'open') return { ...base, reason: snapshot.merged ? 'merged' : 'closed' }
  if (hasMalformedReadinessMarkers(body)) {
    // Fail closed: pr-lint flags the shape for a human; we touch nothing.
    return { ...base, malformed: true, reason: 'malformed-markers' }
  }

  const parsed = parseReadinessBlock(body)
  const checks = snapshot.checks.filter((c) => c && typeof c.name === 'string')
  const block = buildReadinessBlock({
    repository, number, headSha,
    checks: checks.map((c) => ({ name: c.name, status: c.status, conclusion: c.conclusion })),
    reviewScore: reviewScoreFor(snapshot, checks),
    mergeable: snapshot.mergeableState,
    updatedMs: nowMs,
  })
  // The upsert is authoritative for "did anything change": it replaces only the
  // marked span and leaves every author-written byte outside it untouched.
  // Compared timestamp-blind: the block always carries a fresh `Updated:` line,
  // so a naive byte comparison differs on every pass and would rewrite every
  // open PR each sweep — each write firing `edited`, re-queueing pr-lint, and
  // starving the auto-merge gate. The timestamp refreshes only when something
  // else changed. (Malformed markers already returned above; upsert cannot throw.)
  const withoutUpdated = (section) => section.replace(/^Updated: .*$/m, 'Updated:')
  const nextBody = (() => {
    if (!parsed) return upsertReadinessBlock(body, block)
    if (withoutUpdated(parsed.block) === withoutUpdated(block)) return body
    return upsertReadinessBlock(body, block)
  })()
  const blockHeadSha = parsed ? parsed.headSha : null
  const held = Array.isArray(snapshot.labels) &&
    snapshot.labels.some((l) => policy.holdLabels.includes(l))

  const gateSnapshot = {
    draft: snapshot.draft, headSha, blockHeadSha,
    checks: checks.map((c) => ({ name: c.name, status: c.status, conclusion: c.conclusion })),
    requiredChecks,
    autoMergeEnabled: snapshot.autoMerge,
  }

  const actions = []
  let reason = 'up-to-date'
  const bodyReason = nextBody !== body ? (parsed ? 'block-stale' : 'block-missing') : null
  if (bodyReason !== null) {
    actions.push('update-body')
    reason = bodyReason
  }

  if (!held) {
    // shouldMarkReady (draft) and shouldConvertToDraft (non-draft) are
    // mutually exclusive by construction: exactly one can fire per pass.
    const join = (transition) => (bodyReason !== null ? `${bodyReason}+${transition}` : transition)
    if (shouldMarkReady({ ...gateSnapshot })) {
      actions.push('mark-ready')
      reason = join('mark-ready')
    } else if (shouldConvertToDraft({ ...gateSnapshot })) {
      actions.push('convert-to-draft')
      reason = join('convert-to-draft')
    } else if (shouldEnableAutoMerge({ ...gateSnapshot })) {
      actions.push('enable-automerge')
      reason = join('enable-automerge')
    }
  } else if (actions.length > 0) {
    reason = 'held-block-only'
  } else {
    reason = 'held'
  }

  return { ...base, block, nextBody, blockHeadSha, held, actions, reason }
}

// Fixed words plus validated repo/number/head only. Bodies, titles and label
// names are never echoed: they are collaborator- or fork-controlled text.
function logLine(repository, number, headSha, decision, reason) {
  return `readiness ${repository}#${number}@${headSha}: ${decision} (${reason})`
}

export function emptyCounters() {
  return { total: 0, evaluated: 0, acted: 0, wouldAct: 0, noOp: 0, errors: 0,
    updatedBody: 0, markedReady: 0, convertedToDraft: 0, enabledAutomerge: 0 }
}

/**
 * Dependencies (and deliberately NOTHING else):
 * github.getReadinessSnapshot, github.getRulesetRequiredChecks,
 * github.updatePullRequestBody, github.markReadyForReview,
 * github.convertToDraft, github.enableAutoMerge. There is no merge, no close
 * and no comment call path in this module to mistake for one of these.
 */
export function createReconciler({ github, policy, nowMs = () => Date.now() }) {
  requireValue(github && typeof github.getReadinessSnapshot === 'function' &&
    typeof github.getRulesetRequiredChecks === 'function' &&
    typeof github.updatePullRequestBody === 'function' &&
    typeof github.markReadyForReview === 'function' &&
    typeof github.convertToDraft === 'function' &&
    typeof github.enableAutoMerge === 'function',
  'readiness duty requires a snapshot reader, a ruleset reader and four writers, and nothing else')
  const validated = readinessPolicy(policy)
  const enabled = new Set(validated.enabledRepositories)
  requireValue(typeof nowMs === 'function', 'readiness clock is invalid')

  const writers = {
    'update-body': (ref, decision) => github.updatePullRequestBody(ref.repository, ref.number, decision.nextBody),
    'mark-ready': (ref) => github.markReadyForReview(ref.repository, ref.number),
    'convert-to-draft': (ref) => github.convertToDraft(ref.repository, ref.number),
    // Bound to the head the pure gate evaluated: the arm refuses any other head.
    'enable-automerge': (ref, decision) => github.enableAutoMerge(ref.repository, ref.number, decision.headSha),
  }
  const counterFor = {
    'update-body': 'updatedBody', 'mark-ready': 'markedReady',
    'convert-to-draft': 'convertedToDraft', 'enable-automerge': 'enabledAutomerge',
  }

  /**
   * Run the duty over explicit open-PR references. No debounce: the first
   * read that sees a green draft marks it ready. Re-running repeats each step
   * idempotently (byte-identical bodies skip, ready/draft/automerge transitions
   * are no-ops when already in state). One unreadable or undecidable PR is a
   * counted error record, never a thrown run.
   *
   * @returns {{ records, counters }}
   */
  async function runReconcile({ references, nowMs: nowOverride } = {}) {
    requireValue(Array.isArray(references) && references.length <= MAX_RECONCILE_REFS,
      'readiness references are invalid')
    const now = nowOverride ?? nowMs()
    requireValue(Number.isInteger(now) && now >= 0, 'readiness clock is invalid')
    // Validate every reference BEFORE acting: a duplicate or malformed entry
    // is a caller bug, and throwing for it mid-run would leave earlier PRs
    // acted on and later PRs untouched. Read/decide/write failures stay
    // per-PR records below — those are GitHub state, not caller bugs.
    const seen = new Set()
    for (const ref of references) {
      requireValue(isRef(ref), 'readiness reference is invalid')
      const key = `${ref.repository}#${ref.number}`
      requireValue(!seen.has(key), 'readiness references hold a duplicate PR')
      seen.add(key)
    }
    const records = []
    const facts = new Map()
    const counters = emptyCounters()
    for (const ref of references) {
      counters.total++

      let snapshot
      try {
        snapshot = await github.getReadinessSnapshot(ref.repository, ref.number, { nowMs: now })
        if (snapshot.snapshotFacts) facts.set(`${ref.repository}#${ref.number}`, snapshot.snapshotFacts)
      } catch (error) {
        records.push({ ...ref, headSha: null, decision: 'none', reason: 'snapshot-read-failed',
          snapshotFailure: snapshotFailureCode(error),
          log: logLine(ref.repository, ref.number, 'unknown', 'none', 'snapshot-read-failed'),
          error: 'snapshot-read-failed' })
        counters.errors++
        continue
      }
      if (typeof snapshot.body !== 'string') {
        records.push({ ...ref, headSha: snapshot.headSha ?? null, decision: 'none',
          reason: 'body-unavailable',
          log: logLine(ref.repository, ref.number, snapshot.headSha ?? 'unknown', 'none', 'body-unavailable'),
          error: 'body-unavailable' })
        counters.errors++
        continue
      }
      let requiredChecks
      try {
        requiredChecks = await github.getRulesetRequiredChecks(ref.repository, snapshot.baseRef)
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'none', reason: 'ruleset-read-failed',
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'none', 'ruleset-read-failed'),
          error: 'ruleset-read-failed' })
        counters.errors++
        continue
      }
      let decision
      try {
        decision = decideReadinessActions({ snapshot, body: snapshot.body, requiredChecks, nowMs: now }, validated)
      } catch {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'none',
          reason: 'undecidable-snapshot',
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'none', 'undecidable-snapshot'),
          error: 'undecidable-snapshot' })
        counters.errors++
        continue
      }
      counters.evaluated++
      if (decision.actions.length === 0) {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'none', reason: decision.reason,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'none', decision.reason) })
        counters.noOp++
        continue
      }
      if (!enabled.has(ref.repository)) {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'would-act',
          actions: [...decision.actions], reason: decision.reason,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'would-act', decision.reason) })
        counters.wouldAct++
        continue
      }
      const steps = []
      let failed = false
      for (const action of decision.actions) {
        try {
          await writers[action](ref, decision)
          steps.push({ action, result: 'done' })
          counters[counterFor[action]]++
        } catch {
          steps.push({ action, result: 'failed', error: `${action}-failed` })
          failed = true
        }
      }
      if (failed) {
        records.push({ ...ref, headSha: snapshot.headSha, decision: 'error', actions: [...decision.actions],
          steps, reason: decision.reason,
          log: logLine(ref.repository, ref.number, snapshot.headSha, 'error', decision.reason),
          error: 'write-failed' })
        counters.errors++
        continue
      }
      records.push({ ...ref, headSha: snapshot.headSha, decision: 'acted', actions: [...decision.actions],
        steps, reason: decision.reason,
        log: logLine(ref.repository, ref.number, snapshot.headSha, 'acted', decision.reason) })
      counters.acted++
    }
    for (const record of records) {
      const snapshotFacts = facts.get(`${record.repository}#${record.number}`)
      if (snapshotFacts) record.snapshotFacts = snapshotFacts
    }
    return { records, counters }
  }

  return { runReconcile }
}
