// Deterministic PR readiness: bot-maintained checks block + ready gate + auto-merge.
//
// Pure decision logic for case_2eafff8f. No I/O, no network, no credentials —
// the event handler (not shipped here) owns GitHub reads/writes through the
// operator App installation token. This module turns an authoritative snapshot
// into a deterministic PR-body section and three gate decisions, so everything
// worth testing runs under `node --test` with zero CI minutes.
//
// Principle (owner 2026-10-06): anything a machine can verify is set by code,
// never ticked by an agent. Only judgment items stay as author attestations,
// which pr-lint validates.
//
// SILENT SHAPES. A body without a readiness section parses to null rather
// than a guess. A snapshot this module cannot trust throws: a swallowed bad
// read would look exactly like "not ready" and a PR would wait forever.

export const READINESS_START = '<!-- readiness:start -->'
export const READINESS_END = '<!-- readiness:end -->'

// Check name produced by the review bot. Excluded from the ready gate (the
// gate fires BEFORE review runs) and included in the auto-merge gate.
export const REVIEW_CHECK = 'Paperclip Review'

// Heavy job whose completed success the auto-merge gate demands. ci-ok is an
// aggregator that passes when every needed job skipped, so a ci-ok success
// cannot prove heavy CI ran on a head; this names the job that can. Same
// hardcoding rationale as REVIEW_CHECK: the policy enables exactly one repo,
// so its job names are stable pins, not guesses.
export const HEAVY_CHECK = 'suites'

// GitHub treats skipped and neutral as satisfying a required check
// (same set as src/closeout.js PASSING).
const PASSING = new Set(['success', 'neutral', 'skipped'])

// Only names that match this ever reach a PR body; anything else throws.
// Same policy as closeout.js SAFE_NAME.
const SAFE_NAME = /^[A-Za-z0-9 ._()/-]{1,64}$/
const SHA = /^[a-f0-9]{40}$/
const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/(?!\.{1,2}$)[A-Za-z0-9_.-]+$/

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

function checkState(check) {
  // Accepts closeout-snapshot shape ({name, status, conclusion}) and the
  // simplified shape ({name, state}). Returns 'pass' | 'fail' | 'pending'.
  requireValue(check && typeof check === 'object' && !Array.isArray(check), 'check entry is required')
  requireValue(typeof check.name === 'string' && SAFE_NAME.test(check.name), 'check name is invalid')
  if (typeof check.state === 'string') {
    const s = check.state.toLowerCase()
    if (s === 'success' || s === 'pass' || s === 'passing') return 'pass'
    if (s === 'failure' || s === 'fail' || s === 'failing' || s === 'error') return 'fail'
    if (s === 'pending' || s === 'queued' || s === 'in_progress' || s === 'waiting') return 'pending'
    throw new Error(`check state is unknown: ${check.name}`)
  }
  requireValue(typeof check.status === 'string', `check status is missing: ${check.name}`)
  const status = check.status.toLowerCase()
  if (status === 'completed') {
    requireValue(typeof check.conclusion === 'string' && check.conclusion.length > 0, `completed check has no conclusion: ${check.name}`)
    const conclusion = check.conclusion.toLowerCase()
    if (PASSING.has(conclusion)) return 'pass'
    return 'fail'
  }
  if (status === 'queued' || status === 'in_progress' || status === 'pending' || status === 'waiting') return 'pending'
  throw new Error(`check status is unknown: ${check.name}`)
}

function emoji(state) {
  return state === 'pass' ? '✅' : state === 'fail' ? '❌' : '⏳'
}

/**
 * Build the bot-maintained readiness section. Deterministic for fixed inputs:
 * same arguments (including updatedMs) produce byte-identical output, so the
 * handler can skip the write when nothing changed.
 *
 * @param {object} input
 * @param {string} input.repository  "owner/repo"
 * @param {number} input.number  PR number
 * @param {string} input.headSha  40-char exact head the checks ran on
 * @param {Array} input.checks  required CI checks (excluding or including the
 *   review check; the review check is rendered on its own line when present)
 * @param {string | null} [input.reviewScore]  "5/5", "3/5", or null for pending
 * @param {string} [input.mergeable]  lowercase mergeable_state, e.g. "clean"
 * @param {number} [input.updatedMs]  Date.now(); injectable for tests
 * @returns {string}  full section including START/END markers
 */
export function buildReadinessBlock(input) {
  requireValue(input && typeof input === 'object', 'readiness input is required')
  const { repository, number, headSha, checks, reviewScore = null, mergeable = 'unknown', updatedMs = Date.now() } = input
  requireValue(typeof repository === 'string' && REPOSITORY.test(repository), 'repository is invalid')
  requireValue(Number.isSafeInteger(number) && number > 0 && number <= 2147483647, 'PR number is invalid')
  requireValue(typeof headSha === 'string' && SHA.test(headSha), 'head sha is invalid')
  requireValue(Array.isArray(checks), 'checks must be a list')
  requireValue(reviewScore === null || (typeof reviewScore === 'string' && reviewScore.length > 0 && reviewScore.length <= 32), 'review score is invalid')
  requireValue(typeof mergeable === 'string' && mergeable.length > 0 && mergeable.length <= 32, 'mergeable state is invalid')
  requireValue(Number.isInteger(updatedMs) && updatedMs >= 0, 'updated clock is invalid')

  const seen = new Set()
  const rows = []
  for (const check of checks) {
    const state = checkState(check)
    requireValue(!seen.has(check.name), `duplicate check name: ${check.name}`)
    seen.add(check.name)
    // The review check renders on its own scored line; a rollup row carrying
    // it would double-count the same signal in two places.
    if (check.name === REVIEW_CHECK) continue
    rows.push(`| ${check.name} | ${emoji(state)} |`)
  }
  rows.sort()

  const headUrl = `https://github.com/${repository}/pull/${number}/commits/${headSha}`
  const short = headSha.slice(0, 7)
  let reviewLine
  if (reviewScore === null) {
    reviewLine = `${REVIEW_CHECK}: pending ⏳`
  } else if (reviewScore.trim() === '5/5') {
    reviewLine = `${REVIEW_CHECK}: 5/5 ✅`
  } else {
    reviewLine = `${REVIEW_CHECK}: ${reviewScore.trim()} ❌`
  }
  const updated = new Date(updatedMs).toISOString()
  requireValue(updated !== 'Invalid Date' && !Number.isNaN(Date.parse(updated)), 'updated clock is invalid')

  const lines = [
    READINESS_START,
    '## Merge readiness (bot-maintained — do not edit)',
    '',
    `Head: [\`${short}\`](${headUrl}) (\`${headSha}\`) — checks below ran on this exact head.`,
    '',
    '| Check | State |',
    '|---|---|',
    ...rows,
    '',
    reviewLine,
    `Mergeable: ${mergeable}`,
    `Updated: ${updated}`,
    READINESS_END,
  ]
  return lines.join('\n')
}

/**
 * Extract the readiness section from a PR body. Returns null when the markers
 * are absent (a body that predates the handler, not evidence of tampering).
 *
 * @param {string} body
 * @returns {null | { block: string, inner: string, headSha: string | null }}
 */
export function parseReadinessBlock(body) {
  if (typeof body !== 'string') return null
  const start = body.indexOf(READINESS_START)
  if (start === -1) return null
  // Order-aware: the end marker must come AFTER the start marker. A stray
  // end marker before the block (or a missing end marker) is malformed, not
  // an empty section — callers fail closed on it.
  const end = body.indexOf(READINESS_END, start + READINESS_START.length)
  if (end === -1) return null
  const block = body.slice(start, end + READINESS_END.length)
  const inner = body.slice(start + READINESS_START.length, end)
  const m = /`([a-f0-9]{40})`/.exec(inner)
  return { block, inner, headSha: m ? m[1] : null }
}

/**
 * True when the body carries malformed markers: a start with no later end,
 * an end with no earlier start, or either marker more than once. The handler
 * must never write on top of these shapes — it would duplicate the block
 * (each write fires `edited`, looping the handler) or swallow author text.
 *
 * @param {string} body
 * @returns {boolean}
 */
export function hasMalformedReadinessMarkers(body) {
  if (typeof body !== 'string' || body.length === 0) return false
  const starts = body.split(READINESS_START).length - 1
  const ends = body.split(READINESS_END).length - 1
  if (starts === 0 && ends === 0) return false
  if (starts !== 1 || ends !== 1) return true
  return body.indexOf(READINESS_END) < body.indexOf(READINESS_START)
}

/**
 * Insert or replace the readiness section idempotently. Replacing the same
 * block is a no-op; a changed block replaces only the marked span and leaves
 * every author-written byte outside it untouched. Malformed markers throw
 * rather than append-or-replace: appending duplicates the block (each write
 * fires `edited`, looping the handler) and replacing across a stray marker
 * swallows author text. The handler logs and skips; pr-lint flags the shape.
 *
 * @param {string} body
 * @param {string} block  output of buildReadinessBlock
 * @returns {string}
 */
export function upsertReadinessBlock(body, block) {
  requireValue(typeof block === 'string' && block.includes(READINESS_START) && block.includes(READINESS_END), 'readiness block is invalid')
  const base = typeof body === 'string' ? body : ''
  if (hasMalformedReadinessMarkers(base)) throw new Error('readiness markers are malformed; refusing to write')
  const parsed = parseReadinessBlock(base)
  if (!parsed) {
    const sep = base.length > 0 && !base.endsWith('\n') ? '\n\n' : base.length > 0 ? '\n' : ''
    return `${base}${sep}${block}\n`
  }
  if (parsed.block === block) return base
  return base.slice(0, base.indexOf(READINESS_START)) + block + base.slice(base.indexOf(READINESS_END, base.indexOf(READINESS_START)) + READINESS_END.length)
}

/**
 * Validate the section shape for pr-lint: markers present and the five
 * machine lines intact. Returns an array of human-readable problems (empty
 * when valid). A missing section is a problem here; the caller decides the
 * level (warn during migration, error after the handler is live).
 *
 * @param {string} body
 * @returns {string[]}
 */
export function validateReadinessBlock(body) {
  const parsed = parseReadinessBlock(body)
  if (!parsed) return ['readiness block is missing']
  const problems = []
  if (!parsed.headSha) problems.push('readiness block head SHA is missing or hand-edited')
  if (!/\| Check \| State \|/.test(parsed.inner)) problems.push('readiness block check table is missing or hand-edited')
  if (!parsed.inner.includes(REVIEW_CHECK)) problems.push('readiness block review line is missing or hand-edited')
  if (!/Mergeable:/.test(parsed.inner)) problems.push('readiness block mergeable line is missing or hand-edited')
  if (!/Updated:/.test(parsed.inner)) problems.push('readiness block updated line is missing or hand-edited')
  return problems
}

/**
 * Ready gate: a draft PR whose required CI checks (excluding Paperclip
 * Review) are all green on the exact head is ready for review. Marking ready
 * fires `ready_for_review`, which is what starts Paperclip Review when
 * reviewDrafts=false.
 *
 * @param {object} snapshot  { draft, headSha, blockHeadSha, checks, requiredChecks }
 * @returns {boolean}
 */
export function shouldMarkReady(snapshot) {
  requireValue(snapshot && typeof snapshot === 'object', 'snapshot is required')
  const { draft, headSha, blockHeadSha = null, checks = [], requiredChecks = [] } = snapshot
  requireValue(typeof draft === 'boolean', 'draft flag is required')
  requireValue(typeof headSha === 'string' && SHA.test(headSha), 'head sha is invalid')
  if (!draft) return false
  if (blockHeadSha !== null && blockHeadSha !== headSha) return false
  requireValue(Array.isArray(requiredChecks) && requiredChecks.length > 0, 'required checks are required')
  const byName = new Map()
  for (const c of checks) {
    requireValue(c && typeof c.name === 'string', 'check entry is invalid')
    byName.set(c.name, c)
  }
  for (const name of requiredChecks) {
    if (name === REVIEW_CHECK) continue
    const found = byName.get(name)
    if (!found) return false
    if (checkState(found) !== 'pass') return false
  }
  return true
}

/**
 * Draft reset: a ready (non-draft) PR goes back to draft only on a new head
 * (the block still names the old one) or when a required check has
 * COMPLETED failing on this head. A check that is merely pending or missing
 * on the same head never flips it: the handler's own block write and the
 * `ready_for_review` transition re-queue checks such as pr-lint, and
 * flipping on pending would flap ready/draft forever, re-triggering
 * Paperclip Review every cycle.
 *
 * @param {object} snapshot  { draft, headSha, blockHeadSha, checks, requiredChecks }
 * @returns {boolean}
 */
export function shouldConvertToDraft(snapshot) {
  requireValue(snapshot && typeof snapshot === 'object', 'snapshot is required')
  const { draft, headSha, blockHeadSha = null, checks = [], requiredChecks = [] } = snapshot
  requireValue(typeof draft === 'boolean', 'draft flag is required')
  requireValue(typeof headSha === 'string' && SHA.test(headSha), 'head sha is invalid')
  if (draft) return false
  if (blockHeadSha !== null && blockHeadSha !== headSha) return true
  if (!Array.isArray(requiredChecks) || requiredChecks.length === 0) return false
  const byName = new Map()
  for (const c of checks) {
    if (c && typeof c.name === 'string') byName.set(c.name, c)
  }
  for (const name of requiredChecks) {
    if (name === REVIEW_CHECK) continue
    const found = byName.get(name)
    if (!found) continue
    let state
    try {
      state = checkState(found)
    } catch {
      return true
    }
    if (state === 'fail') return true
  }
  return false
}

// Heavy-CI evidence for the auto-merge gate. Only a COMPLETED SUCCESS
// counts: skipped (draft selection skips the suites job, and area gating
// skips it on ready PRs with no selected area) and missing both refuse,
// because the rollup carries no provenance to tell a draft skip from a
// path-gated one. Total function: malformed entries refuse, never throw.
function heavyCompletedSuccess(check) {
  if (!check || typeof check !== 'object' || Array.isArray(check)) return false
  if (typeof check.state === 'string') return check.state.toLowerCase() === 'success'
  if (typeof check.status !== 'string' || check.status.toLowerCase() !== 'completed') return false
  return typeof check.conclusion === 'string' && check.conclusion.toLowerCase() === 'success'
}

/**
 * Auto-merge gate: every required check passes on this head AND the
 * `Paperclip Review` check entry in the same rollup passes on this head AND
 * the heavy `suites` job completed with success on this head. The review
 * verdict is bound to the head through the rollup — never through a
 * separate score string, which could carry a 5/5 from an earlier head while
 * the review check on the current head is failing. The heavy bind exists
 * because ci-ok passes when every needed job skipped (draft selection sets
 * every area false), so a ci-ok success alone does not prove heavy CI ran
 * on this head — and a later review pass is not evidence that it did.
 * Enabling auto-merge (squash, --match-head-commit) does not merge by
 * itself; the branch rulesets remain the real gate.
 *
 * @param {object} snapshot  { draft, headSha, autoMergeEnabled, checks, requiredChecks }
 * @returns {boolean}
 */
export function shouldEnableAutoMerge(snapshot) {
  requireValue(snapshot && typeof snapshot === 'object', 'snapshot is required')
  const { draft, headSha, autoMergeEnabled, checks = [], requiredChecks = [] } = snapshot
  requireValue(typeof draft === 'boolean', 'draft flag is required')
  requireValue(typeof headSha === 'string' && SHA.test(headSha), 'head sha is invalid')
  requireValue(typeof autoMergeEnabled === 'boolean', 'auto-merge flag is required')
  if (draft || autoMergeEnabled) return false
  requireValue(Array.isArray(requiredChecks) && requiredChecks.length > 0, 'required checks are required')
  const byName = new Map()
  for (const c of checks) {
    requireValue(c && typeof c.name === 'string', 'check entry is invalid')
    byName.set(c.name, c)
  }
  // The review check binds the verdict to this head. Missing, pending, or
  // failing all refuse: only a pass on THIS head arms auto-merge.
  const review = byName.get(REVIEW_CHECK)
  if (!review || checkState(review) !== 'pass') return false
  // The heavy bind: ci-ok success does not prove heavy CI ran on this head
  // (draft selection passes ci-ok with every area skipped), so the arm
  // demands the suites job's own completed success. Skipped or missing
  // suites refuses — even with review passed — because a later review pass
  // is not evidence that heavy CI ran.
  if (!heavyCompletedSuccess(byName.get(HEAVY_CHECK))) return false
  for (const name of requiredChecks) {
    if (name === REVIEW_CHECK) continue
    const found = byName.get(name)
    if (!found) return false
    if (checkState(found) !== 'pass') return false
  }
  return true
}

// Events the handler reconciles. Everything else is stored by capture and
// ignored by this module: a webhook is a hint that a snapshot is due, never
// the snapshot itself.
const RELEVANT = new Set([
  'pull_request:opened', 'pull_request:synchronize', 'pull_request:reopened',
  'pull_request:edited', 'pull_request:ready_for_review', 'pull_request:converted_to_draft',
  'pull_request_review:submitted', 'check_run:completed', 'check_suite:completed',
  'status:success', 'status:failure', 'status:error', 'status:pending',
])

/**
 * @param {string} event  webhook event name
 * @param {string | null} action  webhook action or status state
 * @returns {boolean}
 */
export function isRelevantReadinessEvent(event, action) {
  if (typeof event !== 'string') return false
  if (event === 'status') return RELEVANT.has(`status:${String(action ?? '').toLowerCase()}`)
  if (event === 'check_run' || event === 'check_suite') return String(action ?? '').toLowerCase() === 'completed'
  if (event === 'pull_request' || event === 'pull_request_review') {
    return RELEVANT.has(`${event}:${String(action ?? '').toLowerCase()}`)
  }
  return false
}
