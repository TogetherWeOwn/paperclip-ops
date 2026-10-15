# Measuring the pre-review gate (reference only — never ships to the org repo)

This file cites internal evidence, so it stays in the private staging repo.
Only `pre-review.sh`, `RUBRIC.md` and `AGENT-RULE.md` are copied outward.

## Metric

**First-pass 5/5 rate** = merged PRs whose first review verdict on a ready
head scored 5/5, divided by merged PRs that received a review. Companion
metrics: reviews per merged PR, reviewer cost per merged PR.

## Baseline (before)

- First-pass review 49.5%, 3.3 reviews/PR ([case_b8d9a708](/TOG/issues/case_b8d9a708),
  from `ops/analysis/BOTTLENECKS-20261007.md`).
- 529 reviewer findings on 183 PRs across 6 repos (433 warnings, 95 errors;
  88% in code, 12% in docs) — the pool the 10 buckets were drawn from.
- Cheap-model checks scored at chance and do not ship; deterministic rules
  beat them ([case_8c53addb](/TOG/issues/case_8c53addb) verdict, 2026-10-09).

Target (parent epic): first-pass >= 80%.

## Recompute method (the "after" readout)

1. List merged PRs in the window per repo (REST pulls, `state=closed`,
   `merged_at` in window).
2. For each PR, take the FIRST review check-run on the first ready head and
   read its verdict from the check-run output (REST check-runs + job-log
   tail, not the newest-run default listing).
3. Rate = first-verdict-5/5 count / reviewed count. Same query before/after.

The scheduled adoption readout owns the "after" number; this file only pins
the definition so the two readouts are comparable.
