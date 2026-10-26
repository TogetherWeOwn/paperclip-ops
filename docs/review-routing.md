# Public review-routing contract

The authoritative decision implementation is `.github/scripts/review_trigger.py`
(authoritative: review_trigger.py). This document describes source behavior, not
private deployment state or permission to invoke a production reviewer.

A head needs a green `ci-ok` before a review request. Risky paths, security labels,
or a diff exceeding `LARGE_DIFF_LINES = 600` select the risk path. Release automation
also has an explicit request path after CI is green.

The audit sample is deterministic: `sha256(head)` modulo 10 equals zero. Re-running
the decision for the same head does not change its sample membership.

Standard heads with a completed checklist also request review once CI is green;
they are not exempt merely because the standalone cost-routing helper labels them
standard. `review_route.sh` and `review_routing_weekly.py` provide routing analysis
and reporting, not a replacement for the authoritative gate or repository rules.

Public CI tests these decisions offline. Reviewer invocation must use the
configured review-gate integration; a source-level routing decision never grants
merge authority or bypasses a required review check.

## Weekly report input integrity

`review_routing_weekly.py` is an advisory analysis report, not an
authoritative gate. It validates supplied ledger evidence before any
denominator, join or promotion arithmetic: unknown routes, unqualified
identities and mistyped fields are excluded with reason counts (exit
nonzero, explicitly partial), never silently cheap. Promotion uses both
cutoff and `--now` bounds; future findings never promote. Undated
findings keep the documented conservative fallback. `--json` emits
versioned schema 1 with provenance and recovery fields; `--dry-run` is
an offline read-only replay alias. Offline check:
`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_review_report_input_integrity`.

## Repository breakdown (opt-in)

`--by-repo` is valid only with `--json` (any other combination is a
controlled nonzero usage error with a `next_action`); without it the
default text and JSON outputs are unchanged. Example:
`review_routing_weekly.py --ledger LEDGER.jsonl --findings FINDINGS.jsonl --json --by-repo`.

The opt-in adds one versioned section, `repository_breakdown`
(`schema_version: 1`), projected from the same validated records — no
second parser or evidence source. Its `repositories` rows are the sorted
union of repositories from validated decisions and validated findings, so a
findings-only repository appears with zero routed PRs and null shares
instead of a fabricated decision. Each row carries `counts`
(`routed_prs`, `expensive_reviews`, `matched_findings`,
`unmatched_findings`), `metrics` (`expensive_review_share` and
`findings_per_expensive_review` recomputed from that repository's own
denominators, null when a denominator is zero, plus
`audit_sample_defects`), and a sorted bucket histogram. Semantics are the
existing global ones per repository: decisions deduplicate by `(repo, pr)`
with any expensive decision winning, findings are not deduplicated, a
finding matches only an expensive-routed PR in the same repository, and a
missing bucket means `other`. Additive row counts, audit catches and bucket
histograms therefore reconcile exactly with the global totals; shares are
never summed or averaged.

Invalid or excluded rows are never attributed to a repository, even when a
malformed row carries a plausible `repo` field: they appear only in the
section's global `unattributed_counts`. The section inherits the global
state — `ok`/`measured` for a clean cohort, `partial`/`partial` when any
row was excluded (no slice claims completeness), and `not_evaluated` with
no repository rows for the empty-ledger early return, missing inputs and
usage errors, preserving the existing global counts and exits. The section
contains only validated repository identities, bucket IDs and aggregates:
never raw findings, notes, row bodies or rejected identities. It changes
nothing about routing, promotion candidates, promotion windows or review
gates. Offline check:
`python3 -B -m unittest -v test_review_report_input_integrity test_review_report_empty_ledger test_review_repository_breakdown_acceptance`.
