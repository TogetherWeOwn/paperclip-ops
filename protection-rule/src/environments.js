// Protected environments served by this App.
//
// One entry per (repository, environment) pair GitHub may ask about. The pair
// selects the decision mode AND the Paperclip card carrying the CEO GO:
//
//   mode 'plan' — migration apply (slice B contract): the apply run claims a
//     plan (hash, run) binding, the plan artifact chain is verified, and the
//     CEO GO names the exact plan hash on the card. Only staging-migrate-apply
//     serves this mode; no production workflow carries the slice-B marker.
//   mode 'sha' — production deploys: there is no plan artifact, so the CEO GO
//     names the exact head SHA of the run under review on the card. Approve
//     iff the fresh-read head SHA carries a GO on THIS environment's card.
//
// Plan mode also carries `sourceEvidence`: which required CI checks and which
// independent review check must be green, and the GitHub App id each is pinned
// to, on every commit whose code or data the apply trusts (migration source,
// producing plan head, apply workflow head). The values are the live main
// ruleset of that repository (read-only `GET /repos/TogetherWeOwn/two-bot-next/
// rules/branches/main`, 2026-10-07): required contexts `ci-ok`, `gitleaks` and
// `pr-lint` pinned to GitHub Actions (15368), and `Paperclip Review` pinned to
// the review App `togetherweown-reviewer` (5191093). A check NAME is claimable
// by any App or commit status with write access; the App id is what
// authenticates who produced it. Weakening the set is a code change on purpose.
//
// The GO card is part of the approval key: a GO posted anywhere else approves
// nothing. Adding an environment is a code change on purpose — scope that
// moves without review is how an approver starts approving the wrong thing.
// `validatePolicies` runs at startup; an invalid table refuses to start.

const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/
const CHECK_NAME = /^[A-Za-z0-9 ._()/-]{1,64}$/
const SCORE = /^[0-9]{1,2}\/[0-9]{1,2}$/

export const MODES = ['plan', 'sha']

export const PROTECTED_ENVIRONMENTS = [
  {
    repository: 'TogetherWeOwn/two-bot-next',
    environment: 'staging-migrate-apply',
    mode: 'plan',
    // Parent card: CEO GOs bind plan hashes for the staging migration gate.
    goIssueId: '00000000-0000-4000-8000-000000000133',
    sourceEvidence: {
      ci: { appId: 15368, checks: ['ci-ok', 'gitleaks', 'pr-lint'] },
      review: { appId: 5191093, check: 'Paperclip Review', score: '5/5' },
    },
  },
  {
    repository: 'TogetherWeOwn/kagi-shinobi',
    environment: 'production',
    mode: 'sha',
    // First-deploy launch card: CEO GOs bind exact deploy SHAs.
    goIssueId: '00000000-0000-4000-8000-000000000134',
  },
  {
    repository: 'TogetherWeOwn/two-web-next',
    environment: 'production',
    mode: 'sha',
    // Production cutover card: CEO GOs bind exact deploy SHAs.
    goIssueId: '00000000-0000-4000-8000-000000000135',
  },
  {
    repository: 'TogetherWeOwn/two-bot-next',
    environment: 'production',
    mode: 'sha',
    // Standing TWO Next production directive card (re-pinnable to the
    // per-launch card when one exists): CEO GOs bind exact deploy SHAs.
    goIssueId: '00000000-0000-4000-8000-000000000136',
  },
]

function invalid(message) {
  throw new Error(message)
}

/**
 * Check one policy entry. Throws on the first defect — the table is code, so
 * a defect is a programming error, and starting with it would serve a scope
 * nobody reviewed.
 *
 * @param {any} entry
 */
export function validatePolicy(entry) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) invalid('protected environment entry is invalid')
  if (typeof entry.repository !== 'string' || !REPOSITORY.test(entry.repository)) invalid('protected environment repository is invalid')
  if (typeof entry.environment !== 'string' || entry.environment.length === 0 || entry.environment.length > 128) {
    invalid('protected environment name is invalid')
  }
  if (!MODES.includes(entry.mode)) invalid('protected environment mode is invalid')
  if (typeof entry.goIssueId !== 'string' || !UUID.test(entry.goIssueId)) invalid('protected environment GO card is invalid')
  validateSourceEvidence(entry)
}

function isAppId(value) {
  return Number.isSafeInteger(value) && value > 0
}

/**
 * Check one `sourceEvidence` block. Plan mode must carry a complete one: an
 * entry without it would approve a migration on ancestry and a CEO GO alone,
 * which is exactly the gap this block closes. Sha mode must not carry one:
 * production deploys are governed by the exact-SHA GO and nothing here reads it.
 *
 * @param {any} entry
 */
function validateSourceEvidence(entry) {
  const block = entry.sourceEvidence
  if (entry.mode !== 'plan') {
    if (block !== undefined) invalid('protected environment source evidence is only valid in plan mode')
    return
  }
  if (!block || typeof block !== 'object' || Array.isArray(block)) invalid('protected environment source evidence is missing')
  const { ci, review } = block
  if (!ci || typeof ci !== 'object' || !isAppId(ci.appId) || !Array.isArray(ci.checks) || ci.checks.length === 0 ||
      ci.checks.length > 16 || !ci.checks.every((name) => typeof name === 'string' && CHECK_NAME.test(name)) ||
      new Set(ci.checks).size !== ci.checks.length) {
    invalid('protected environment CI evidence is invalid')
  }
  if (!review || typeof review !== 'object' || !isAppId(review.appId) || typeof review.check !== 'string' ||
      !CHECK_NAME.test(review.check) || typeof review.score !== 'string' || !SCORE.test(review.score)) {
    invalid('protected environment review evidence is invalid')
  }
  if (ci.checks.includes(review.check)) invalid('protected environment review check is also a CI check')
}

/**
 * Check a whole table: every entry valid, no duplicate (repository,
 * environment) pair. Returns the list unchanged for inline use.
 *
 * @param {any[]} list
 * @returns {any[]}
 */
export function validatePolicies(list) {
  if (!Array.isArray(list) || list.length === 0) invalid('protected environment table is empty')
  const seen = new Set()
  for (const entry of list) {
    validatePolicy(entry)
    const key = `${entry.repository}\0${entry.environment}`
    if (seen.has(key)) invalid('protected environment table holds a duplicate')
    seen.add(key)
  }
  return list
}

/**
 * Resolve the policy for one delivery. Exact match on both halves; anything
 * else is unserved and the caller rejects it. Never fuzzy: a prefix match
 * here would serve an environment nobody listed.
 *
 * @param {object} args
 * @param {string | null} args.repository
 * @param {string | null} args.environment
 * @param {Array} [args.policies]
 * @returns {{ repository: string, environment: string, mode: string, goIssueId: string } | null}
 */
export function findPolicy({ repository, environment, policies = PROTECTED_ENVIRONMENTS }) {
  if (typeof repository !== 'string' || typeof environment !== 'string') return null
  return policies.find((p) => p.repository === repository && p.environment === environment) ?? null
}

/**
 * Repositories holding at least one protected environment. The receiver uses
 * this to tell "known repo, unknown environment" (answerable rejection) from
 * "foreign repo" (no verdict posted outside our scope).
 *
 * @param {Array} [policies]
 * @returns {Set<string>}
 */
export function scopedRepositories(policies = PROTECTED_ENVIRONMENTS) {
  return new Set(policies.map((p) => p.repository))
}
