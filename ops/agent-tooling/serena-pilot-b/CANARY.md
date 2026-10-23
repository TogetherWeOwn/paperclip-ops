# Canary plan: one builder, 3 days (starts after Operator reports healthy)

## Canary subject

Default: Web Engineer, unless the CEO redirects. One builder only. Enabling
is a docs step, not a binding: the host adds BUILDER_TOOLS_SNIPPET.md to the
canary builder's TOOLS.md first; the other builders get the snippet only on a
success verdict. No gateway state exists to toggle.

## Metrics (DevOps owns measurement)

- Input tokens per merged card: canary 3 days vs the same builder's previous
  2 weeks (source: run token accounting per merged card).
- Big-file re-reads: count of whole-file reads of files >1 MiB (or the
  `src/handlers.rs` / `src/lib.rs` class) in canary window vs baseline.
- Quality guard: first-review 5/5 rate must not drop vs baseline.

## Verdict

- Success: >=20% fewer input tokens per merged card AND no drop in
  first-review 5/5. Then document the CLI in the remaining builders'
  TOOLS.md (same snippet).
- Failure/rollback: Operator removes the install per OPERATOR_SPEC UNDO;
  canary builder returns to whole-file reads. Record the numbers either way.

## Open gaps

- G1: Stage-0 config snippet + 7-day baselines not found in this repo at the
  spec head. Baselines must be pulled from run accounting before the canary
  starts; reconcile `contexts/builder-readonly.yml` with the snippet when
  located.
- G2 (retired): was the gateway connection/binding IDs. There is no
  connection in the CLI shape; the Operator reports install paths and primed
  languages instead (OPERATOR_SPEC step 6).
- G3: language-server downloads live under `/tmp/serena-home` and are
  re-downloaded on first use after a /tmp prune (one slow call, then fast).
  If prunes prove frequent, promote the primed dirs to persistent storage.
