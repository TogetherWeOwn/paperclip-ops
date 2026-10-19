# Review trigger: Paperclip Review only on ready heads

The reviewer is the FINAL review and runs far less often. PRs exceed 5/5
**before** review is requested. Deterministic checks first; the design must
not depend on any single model provider.

## Decision

Request Paperclip Review exactly once per ready head, from a workflow, not
from every push:

- **Ready** = `ci-ok` green on the exact head + non-draft + template
  checklist complete + no success/failure Paperclip Review verdict on that head.
- **Never**: drafts, every-push triggers, re-reviews of an already-reviewed
  head (a new review needs a new push).
- **Auto-invoked without the checklist**: release-please PRs and risky paths.
- **Risky paths**: security boundary
  (auth/permissions/secrets/credentials/tokens), migrations, infra and
  workflow files, host/runner/provisioning/broker/deploy code, PRs carrying
  risk/security/tier labels, diffs over 600 lines, plus a deterministic 10%
  audit sample (`sha256(head) mod 10 == 0`, stable under re-evaluation).

## How it works (this repo)

| Piece | Role |
|---|---|
| `.github/scripts/review_trigger.py` | Pure stdlib decision (`decide()`); no network, no model. |
| `.github/scripts/review_trigger_step.py` | GitHub reads + idempotent trigger post. |
| `ops/productivity/review_dedup.py` | Shared same-head idempotency guard, also used by `review_error_rerequest.py`. |
| `test_review_trigger.py`, `test_review_trigger_step.py` | Offline unit tests, run in `pr-lint`. |

Precedence: draft → already-reviewed head → ci-ok red → release-please →
risky → checklist → audit sample → ready head. Every skip exits 0 with a
notice; anything that prevents reaching a decision exits 1 (fail closed).
Check-run and comment reads follow `page=N` to the last page, and exit 1 past
3000 items rather than decide on a partial view. The file list is capped at
one page; a full page routes the PR to review.

Trigger record per head: the `review-ready` label (machine signal) first,
then one `@togetherweown-reviewer review <sha>` comment (audit trail, checked
before posting). Label-first avoids an orphan comment when the label write
fails; the reviewer invocation evaluates the label at review time and
tolerates either order, but this repo always writes label-first.
The verdict check-run test is the one-review-per-head enforcer:
a success/failure verdict is never re-requested on the same head, but
incomplete runs (action_required, cancelled, timed_out, stale) never block a
re-request on the next CI completion. Same-head idempotency: just
before posting, the step re-reads the head's check-runs and suppresses the
post when a live Paperclip Review run already covers the head (manual
mention, gate listing, or an overlapping delivery started it without leaving
the sha marker the comment check looks for). Error outcomes stay retryable;
the host reconciler owns the wake path for those heads (one wake per head via its state key; repeated errors still wake).

## Platform half (Operator)

The step posts the request; the reviewer invocation must honor it and stop
firing on every push:

1. Disable auto-review on `synchronize`/every-push for all repos.
2. Invoke the reviewer when the `review-ready` label lands from this
   step (actor `github-actions[bot]`, comment marker
   `@togetherweown-reviewer review <sha>`), reviewing that exact `head_sha`.
3. Keep drafts excluded and keep the existing risky-path always-review list
   aligned with `RISKY_PATH_PATTERNS` in `review_trigger.py` (one list owns
   both).
4. Reviewer model: one pinned model only. No alternate-model path may gate a review.

Rollback of this repo half: remove the trigger step call; reviews return to
manual `@togetherweown-reviewer review` mentions, nothing else changes.

## Rollout and measure

1. Land here, watch 5+ PRs: one trigger per ready head,
   zero drafts, release-please auto-invoked.
2. Adopting repos copy the step plus `ops/productivity/review_dedup.py`,
   which the step imports relative to the repo root. Repos with a
   sparse checkout must include `ops/productivity`, or the step fails
   closed instead of posting.
3. Measure weekly: first-pass 5/5 rate per repo (target ≥80%), reviews per
   merged PR, reviewer cost. Tune `LARGE_DIFF_LINES` from data, not by feel.
