// S5a wake eligibility (design §5.4): the merge-confirmation-tolerant predicate
// shared by the consumer pre-claim gates and the adapter's own pre-post gate.
//
// Mirror note: isMergeConfirmationText / mergeConfirmationReferences mirror the
// installed Paperclip server's getMergeConfirmationPullRequestReferences
// (services/issue-thread-interactions.js) plus extractGitHubPullRequestReferences
// (services/github-pull-request-merge.js): same kind/payload/target guards, same
// intent pattern, same merge-only vocabulary, same URL/shorthand extraction with
// case-insensitive owner/repo matching. S5b owns the opener side (the pending
// request_confirmation the originating agent opens at PR open). Opener
// constraint, verified against the pattern: the intent test anchors at the
// joined text's start and `.` never crosses a newline, so the title's FIRST
// line must carry the merge intent itself (e.g. "Please confirm that
// owner/repo#N is merged") — a bare "Please confirm merge" title does NOT
// match. If the server vocabulary changes, this mirror must change with it;
// the unit tests pin the shared cases so drift fails loudly here instead of
// silently waking wrong.
//
// No I/O. Pure predicate: an exception wake is delivered to a task that holds a
// pending merge confirmation for that PR, and is refused when the task holds any
// other pending interaction.

const WAKE_STATUSES = new Set(['todo', 'in_progress', 'in_review'])
const INTERACTION_STATUSES = new Set(['pending', 'accepted', 'rejected', 'answered', 'cancelled', 'expired', 'failed'])
// A confirmation that already delivered the built-in wake, or is still pending
// it, owns the merged wake. Any other state never delivered one.
const CONFIRMATION_LIVE_STATES = new Set(['pending', 'accepted', 'answered'])

// Mirrors GITHUB_PULL_REQUEST_URL_PATTERN / GITHUB_PULL_REQUEST_SHORTHAND_PATTERN.
const PR_URL_PATTERN = /https:\/\/(?:www\.)?github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)/gi
const PR_SHORTHAND_PATTERN = /(^|[^A-Za-z0-9_.-])([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)#([1-9][0-9]*)\b/g
// Mirrors MERGE_CONFIRMATION_INTENT_PATTERN.
const MERGE_INTENT_PATTERN = /^(?:please\s+)?(?:confirm(?:\s+that)?\s+.{0,80}\s+)?(?:merge|merged)\b|\bready\s+to\s+merge\b/i
// Mirrors MERGE_CONFIRMATION_ALLOWED_WORDS.
const MERGE_ONLY_WORDS = new Set([
  'all', 'and', 'approved', 'are', 'both', 'checks', 'ci', 'confirm', 'github',
  'green', 'is', 'it', 'link', 'linked', 'links', 'merge', 'merged', 'merging',
  'passed', 'passing', 'please', 'pr', 'primary', 'prs', 'pull', 'ready',
  'reference', 'references', 'request', 'requests', 'review', 'secondary',
  'tests', 'that', 'the', 'these', 'this', 'to', 'url',
])

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)

// After replacing recognised PR references, every trusted field must contain
// merge-only vocabulary. An otherwise valid merge prompt that smuggles an
// additional action ("also deploy to prod") is NOT a merge confirmation.
function isMergeOnlyText(value) {
  PR_URL_PATTERN.lastIndex = 0
  const withoutUrls = value.replace(PR_URL_PATTERN, ' pr_reference ')
  PR_SHORTHAND_PATTERN.lastIndex = 0
  const withoutReferences = withoutUrls.replace(PR_SHORTHAND_PATTERN, (_match, prefix) => `${prefix} pr_reference `)
  const normalized = withoutReferences
    .replace(/\b(?:github-)?pr-[1-9][0-9]*\b/gi, ' pr_reference ')
    .replace(/[`*_\[\]{}()<>:;,.!?"'=+&|\\/-]+/g, ' ')
    .trim()
    .toLowerCase()
  if (!normalized) return true
  return normalized.split(/\s+/).every((word) => word === 'pr_reference' || MERGE_ONLY_WORDS.has(word))
}

function addPrReference(references, owner, repo, rawNumber) {
  const number = Number(rawNumber)
  if (!Number.isSafeInteger(number) || number <= 0) return
  const key = `${owner.toLowerCase()}/${repo.toLowerCase()}#${number}`
  if (!references.has(key)) references.set(key, { host: 'github.com', owner, repo, number })
}

function extractPrReferences(values) {
  const references = new Map()
  for (const value of values) {
    if (typeof value !== 'string' || value.length === 0) continue
    PR_URL_PATTERN.lastIndex = 0
    for (const match of value.matchAll(PR_URL_PATTERN)) addPrReference(references, match[1], match[2], match[3])
    PR_SHORTHAND_PATTERN.lastIndex = 0
    for (const match of value.matchAll(PR_SHORTHAND_PATTERN)) addPrReference(references, match[2], match[3], match[4])
  }
  return [...references.values()]
}

/**
 * The PR references the built-in sweep would accept this interaction as a
 * merge confirmation for. Empty means "not a merge confirmation, for any PR".
 * Status-agnostic like the server function; callers filter by status.
 */
export function mergeConfirmationReferences(interaction) {
  if (!object(interaction) || interaction.kind !== 'request_confirmation') return []
  const payload = object(interaction.payload) ? interaction.payload : null
  if (!payload || payload.toolAction !== undefined || payload.secretProposal !== undefined) return []
  const target = object(payload.target) ? payload.target : null
  // Plan/document confirmations must never inherit merge-confirmation authority
  // merely because their prose links to a PR.
  if (target?.type === 'issue_document') return []
  const intentValues = [interaction.title, interaction.summary, payload.prompt, payload.acceptLabel]
  const intentText = intentValues.filter((value) => typeof value === 'string').join('\n')
  if (!MERGE_INTENT_PATTERN.test(intentText)) return []
  const trustedTextValues = [...intentValues, payload.detailsMarkdown, target?.key, target?.label, target?.href]
  if (!trustedTextValues.every((value) => typeof value !== 'string' || isMergeOnlyText(value))) return []
  return extractPrReferences(trustedTextValues)
}

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/

function prContext(pr) {
  requireValue(object(pr) && typeof pr.repository === 'string' && REPOSITORY.test(pr.repository) &&
    Number.isSafeInteger(pr.number) && pr.number > 0, 'wake eligibility needs a repository and PR number')
  return { repository: pr.repository, number: pr.number }
}

/**
 * True when the interaction is a PENDING merge confirmation for this PR: the
 * one pending interaction the eligibility predicate tolerates. Any other
 * pending interaction (an unrelated open question, possibly waiting on a
 * human) still suppresses the wake.
 */
export function isMergeConfirmationFor(interaction, pr) {
  const { repository, number } = prContext(pr)
  if (!object(interaction) || interaction.status !== 'pending') return false
  return mergeConfirmationReferences(interaction).some((ref) =>
    ref.owner.toLowerCase() === repository.split('/')[0].toLowerCase() &&
    ref.repo.toLowerCase() === repository.split('/').slice(1).join('/').toLowerCase() &&
    ref.number === number)
}

/**
 * Merge-confirmation-tolerant eligibility. Identical fail-closed shape to the
 * previous any-pending predicate, except a pending merge confirmation for this
 * PR does not suppress the wake. Without the tolerance every exception wake
 * would return ineligible from PR open to merge (the confirmation is pending
 * that whole time), claim nothing, and retry forever without waking.
 */
export function wakeEligible(issue, interactions, pr) {
  const context = prContext(pr)
  requireValue(Array.isArray(interactions) && interactions.every((i) => INTERACTION_STATUSES.has(i?.status)),
    'Paperclip interactions response is incomplete')
  requireValue(Array.isArray(issue.blockedBy), 'Paperclip blocker response is incomplete')
  if (interactions.some((i) => i.status === 'pending' && !isMergeConfirmationFor(i, context))) return false
  if (issue.blockedBy.some((b) => b.status !== 'done')) return false
  return WAKE_STATUSES.has(issue.status) && typeof issue.assigneeAgentId === 'string' &&
    issue.assigneeAgentId.length > 0 && issue.assigneeUserId === null
}

/**
 * True when the built-in path owns (or already delivered) the merged wake for
 * this PR: a live (pending) or delivered (accepted/answered) merge
 * confirmation exists. Expired, cancelled or rejected confirmations never
 * delivered a wake, so they do not suppress the reconciler fallback.
 *
 * Exactly-one-merged-wake rule: the reconciler posts its own merged wake only
 * when this returns false, and never alongside the built-in wake. The fallback
 * itself still passes through the gated transport, so an ineligible task is
 * never woken by it either.
 */
export function hasMergeConfirmation(interactions, pr) {
  const context = prContext(pr)
  requireValue(Array.isArray(interactions), 'Paperclip interactions response is incomplete')
  return interactions.some((i) => object(i) && CONFIRMATION_LIVE_STATES.has(i.status) &&
    mergeConfirmationReferences(i).some((ref) =>
      ref.owner.toLowerCase() === context.repository.split('/')[0].toLowerCase() &&
      ref.repo.toLowerCase() === context.repository.split('/').slice(1).join('/').toLowerCase() &&
      ref.number === context.number))
}

/**
 * Built-in path evaluation (S5a §3): the platform's merge confirmation covers
 * ONLY the merged class — the sweep accepts a pending request_confirmation in
 * merge-only vocabulary naming the PR, and accepts it when the PR merges. A
 * confirmation per exception class (conflict, changes_requested,
 * required_check_failed/missing, stalled, closed_unmerged) is not natural: the
 * vocabulary test above rejects non-merge actions, and one pending card per
 * class per PR would each suppress wakes until answered. So the atomic
 * transport serves the exception classes, while merged goes through the
 * built-in path first and the reconciler posts only this fallback.
 *
 * Whether the reconciler may post the fallback merged wake for this PR.
 * Fail-closed on unreadable input like every other predicate here.
 */
export function shouldPostMergedFallback(interactions, pr) {
  return !hasMergeConfirmation(interactions, pr)
}
