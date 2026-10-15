// Source evidence for a staging migration apply: green required CI and an
// independent Paperclip Review, authenticated per exact commit.
//
// Ancestry and a CEO GO are not review. `evidence.js` proves the claim, the
// producing plan and main-history reachability; none of that says anyone read
// or tested the commit. This module reads GitHub check runs and pull-request
// association, never a delivery payload, and answers only with a result that
// `decide` can check: `{ ok: true, shas }` or `{ ok: false, reason }`.
//
// What counts, per commit SHA (the migration source, the producing plan head
// and the apply workflow head are each checked, deduplicated):
//
//   CI      every required check named in the policy has at least one run on
//           THAT commit, produced by the pinned CI App, and every such run is
//           completed/success. A run from another App, a commit status, a
//           skipped or neutral run, a pending run, or a run on another commit
//           is not CI.
//   Review  the commit is the `merge_commit_sha` of exactly one pull request
//           merged into `main` of this repository (a squash merge makes a NEW
//           commit; the review ran on the pull request head, and GitHub's own
//           `merge_commit_sha` is the only link back). On that pull request's
//           head there is exactly one latest check run with the review check's
//           name from the pinned review App, completed/success, titled with the
//           required score, recorded against that pull request number and head,
//           finished no later than the merge. A review that postdates the merge
//           reviewed nothing that was gated.
//
// "Independent" means produced by the pinned review App: GitHub cannot say
// which agent authored a pull request (every agent pushes as one App), so
// author-versus-reviewer cannot be proven here and is not claimed.
//
// Anything absent, stale, mismatched or ambiguous fails closed, and the two
// failure classes keep distinct stable reasons so an operator can tell a red
// pipeline from a missing review. Reads only: no write, no retry, no polling.

import { REASON } from './decide.js'

const SHA40 = /^[0-9a-f]{40}$/
const MAX_COMMITS = 8

function requireValue(condition, message) {
  if (!condition) throw new Error(message)
}

function isoMs(value) {
  const ms = typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value) ? Date.parse(value) : NaN
  requireValue(Number.isFinite(ms), 'GitHub timestamp is invalid')
  return ms
}

async function requireGreenChecks({ github, repository, sha, ci }) {
  const runs = await github.listCheckRuns(repository, sha)
  for (const name of ci.checks) {
    const found = runs.filter((run) => run.name === name && run.appId === ci.appId)
    requireValue(found.length >= 1, `required check is absent on ${sha}`)
    requireValue(
      found.every((run) => run.headSha === sha && run.status === 'completed' && run.conclusion === 'success'),
      `required check is not green on ${sha}`,
    )
  }
}

async function requireIndependentReview({ github, repository, sha, review }) {
  const pulls = await github.listPullsForCommit(repository, sha)
  const merged = pulls.filter((pull) => pull.mergeCommitSha === sha)
  requireValue(merged.length === 1, `${sha} is not the merge commit of exactly one pull request`)
  const pull = merged[0]
  requireValue(
    pull.state === 'closed' && pull.baseRef === 'main' && pull.baseRepository === repository && pull.headSha !== null,
    `pull request for ${sha} is not a closed merge into main`,
  )
  const mergedAt = isoMs(pull.mergedAt)
  const runs = await github.listCheckRuns(repository, pull.headSha)
  const found = runs.filter((run) => run.name === review.check && run.appId === review.appId)
  requireValue(found.length === 1, `review is absent or ambiguous on the head of the pull request for ${sha}`)
  const run = found[0]
  requireValue(run.headSha === pull.headSha, 'review is bound to another head')
  // The review App records `<id>:<pull request number>:<head sha>` on its run
  // (observed on live runs; its format is not documented elsewhere, so a format
  // change fails closed here). It keeps a review recorded for another pull
  // request from vouching for this head.
  requireValue(typeof run.externalId === 'string' && run.externalId.endsWith(`:${pull.number}:${pull.headSha}`), 'review is recorded against another pull request')
  requireValue(run.status === 'completed' && run.conclusion === 'success', 'review is not successful')
  requireValue(run.title === review.score, 'review score is below the required score')
  requireValue(isoMs(run.completedAt) <= mergedAt, 'review finished after the merge')
}

/**
 * @param {object} args
 * @param {object} args.github   reader from `createGithubReader`
 * @param {string} args.repository
 * @param {string[]} args.shas   every commit whose code or data the apply trusts
 * @param {{ ci: { appId: number, checks: string[] }, review: { appId: number, check: string, score: string } }} args.evidence
 *   the policy's `sourceEvidence` block (already validated at startup)
 * @returns {Promise<{ ok: true, shas: string[] } | { ok: false, reason: string }>}
 */
export async function verifySourceEvidence({ github, repository, shas, evidence }) {
  requireValue(evidence && evidence.ci && evidence.review, 'source evidence policy is missing')
  requireValue(
    Array.isArray(shas) && shas.length > 0 && shas.length <= MAX_COMMITS &&
      shas.every((sha) => typeof sha === 'string' && SHA40.test(sha)),
    'source evidence request is invalid',
  )
  const distinct = [...new Set(shas)]
  try {
    for (const sha of distinct) await requireGreenChecks({ github, repository, sha, ci: evidence.ci })
  } catch {
    return { ok: false, reason: REASON.SOURCE_CI_UNVERIFIED }
  }
  try {
    for (const sha of distinct) await requireIndependentReview({ github, repository, sha, review: evidence.review })
  } catch {
    return { ok: false, reason: REASON.SOURCE_REVIEW_UNVERIFIED }
  }
  return { ok: true, shas: distinct }
}
