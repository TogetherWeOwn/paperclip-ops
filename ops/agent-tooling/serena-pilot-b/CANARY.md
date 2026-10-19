# Canary plan: one builder, 3 days (starts after Operator reports healthy)

## Canary subject

Default: Web Engineer (`69c12a25-80df-4c80-8026-f12651e653eb`), unless the CEO
redirects. One builder only; the other three keep the binding off until the
verdict (binding exists but unassigned, or assigned-but-paused — Operator
records which).

## Metrics (DevOps owns measurement)

- Input tokens per merged card: canary 3 days vs the same builder's previous
  2 weeks (source: run token accounting per merged card).
- Big-file re-reads: count of whole-file reads of files >1 MiB (or the
  `src/handlers.rs` / `src/lib.rs` class) in canary window vs baseline.
- Quality guard: first-review 5/5 rate must not drop vs baseline.

## Verdict

- Success: >=20% fewer input tokens per merged card AND no drop in
  first-review 5/5. Then bind the remaining three builders.
- Failure/rollback: Operator removes the binding per OPERATOR_SPEC UNDO;
  canary builder returns to whole-file reads. Record the numbers either way.

## Open gaps

- G1: Stage-0 `RESULTS.md` config snippet + 7-day `REPORT.md` baselines not
  found in paperclip-ops at this head. Baselines must be pulled from run
  accounting before the canary starts; reconcile `builder-readonly.yml`
  with the snippet when located.
- G2: Company `mcp_local_stdio` registration mechanism IDs (connection row,
  binding rows) to be reported back by the Operator on the child card.
