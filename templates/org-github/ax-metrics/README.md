# AX metric contracts

The existing standalone reporter emits report schema **3**. Every metric has
its own flat `contract` object with `id`, integer `version`, `unit`, `row_grain`,
`time_semantics`, `zero_semantics`, and `null_semantics`. Version 1 identifies
these definitions, independently of the report schema and package versions.
A change to a metric's meaning, units, grain, or null rules requires a new
metric contract version. Adding an unrelated metric does not version the others.
The definitions live in `METRIC_CONTRACTS` in `ax_metrics.py`, not in environment
configuration. Returned contracts are independent copies.

`SourceWindow`, `GitHubRow`, `MetricObservation`, `MetricResult`, and `Report`
are the Python typed boundary. They describe the producer's output and internal
transform inputs; `TypedDict` is not a runtime validator for arbitrary fixtures.
`build_report` performs no network calls, environment reads, clock reads, or
writes. Collection and optional credential configuration remain in the existing
CLI adapter. No new service, schedule, host script, or deployment is required.

## Definitions and limits

| Metric ID | Unit | Row grain | Zero / null |
| --- | --- | --- | --- |
| `first_push_green_rate` | `fraction` (0–1, **not percent**) | repository + pull request; conclusive sampled first-commit state | Zero: sampled conclusive states have no success. Null: no conclusive states. |
| `tokens_per_merged_pr` | `tokens_per_pull_request` | company token total divided by merged PRs from configured repositories | Zero: verified zero-token total with positive denominator. Null: absent/invalid evidence, unverified window, or zero PR denominator. |
| `escalations_per_100_cards` | `blocked_cards_per_100_touched_cards` (0–100) | company issue in the touched population | Zero: complete, nonempty touched population has no blocked cards. Null: absent/incomplete scan or empty population. |

These are **proxies**, not direct causal measurements:

- The first metric observes the first commit's checks **at collection time**;
  it cannot reconstruct whether the original first push passed. GitHub search
  uses `merged:>=` with the report start's calendar date and no upper bound.
  Up to 30 PRs per repository are sampled. Pending or absent states do not enter
  the numeric denominator. `error` is a conclusive failure.
- Token cost evidence matches report start/end **calendar dates**, not exact
  instants. The company-wide numerator is not attributed to individual PRs or
  necessarily to the configured repository set. GitHub's denominator uses the
  same calendar-date lower bound above. No exact-window or per-PR attribution
  claim is made.
- The issue proxy uses each issue's **current** blocked state, filtering
  `updatedAt` strings at or above the report start, without an upper bound.
  It is not a history of escalation events. Capped/failed scans withhold it.

The report's `source_window` is requested window provenance, not proof that the
underlying sources use identical exact-instant windows. Contract metadata makes
these existing collection limits explicit; this change does not redesign them.

## Schema 2 → 3 migration

Keep consuming existing `value`, `definition`, `provenance`, and validity fields,
but do not interpret a numeric `value` alone as complete-population evidence.
Check `contract.id` + `contract.version`, then `validity` and `completeness`.

The first-commit metric is `valid` / `complete` only when every merged PR has a
conclusive state and no source row reports truncation. Otherwise a nonempty
conclusive sample still yields its fraction, with `validity=partial`,
`completeness=partial`, `reason_code=sample_partial`, and an actionable
`reason` / `next_action`. `counts` reports `merged_prs`, `sampled_prs`, and
`conclusive_prs`. For example, 15 successes among 30 conclusive sampled PRs
out of 80 merged PRs yields **0.5 partial**, not a 50% full-population result.

When merged PRs exist but no states are conclusive, the value is null,
`validity=withheld`, `reason_code=no_evidence`, and `completeness=unknown`.
An empty merged population remains null / `no_prs` with complete coverage.
The token and issue calculations and their existing reason codes are unchanged.

## Offline verification

```sh
python3 -B -m unittest discover -s templates/org-github/tests -p 'test_ax_*py'
python3 -B templates/org-github/ax-metrics/ax_metrics.py --help
python3 -B templates/org-github/ax-metrics/ax_metrics.py --from-file fixture.json --days 7
```

Identical fixtures produce identical JSON, independent of credentials. The
bundled workflow summary shows the green rate's validity, completeness and
reason code, plus conclusive-versus-merged and sampled PR counts when available.
Thus the same numeric fraction with partial coverage is visibly different from
a complete result; withheld/no-evidence results retain their unknown status.
Cost and card values remain redacted as `see log (internal)`. The template is
not activated by this contract.
Full reports can contain private costs/card data: retain them in private
artifacts, never public PRs. Revert the source change to roll back schema 3;
no stored state is migrated. This sanitized repository is not a live-host
installation bundle.
