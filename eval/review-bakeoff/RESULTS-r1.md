# Reviewer-model bake-off, round 1: quality vs cost (case_96f9122f)

Run 2026-10-04. Dataset: 19 cases (11 real merged-then-fixed PRs from
`two-web-next` + 8 planted bugs: 2 critical, 3 major, 3 minor). Every arm ran
`codex exec` at medium reasoning effort through CLIProxy — the same transport
the provisioned reviewer (`codex_local`) uses. Lane health from Keeper
2026-10-04T04:50Z. 133/133 runs, 0 failed.

Raw results: `results/<model>-medium-r1/` (19 files each, secret-scanned).
Scorer: `score.py` (location match ±15 lines; unmatched findings are
"unjudged", so precision is a lower bound).

## Hygiene qualification (2026-10-25)

**Descriptive observations only; model differences and winner claims are withheld.**
Every arm requested medium effort, but each cell has only one repetition.
Run-to-run variance is **unmeasured**, not zero. Wilson intervals below do not
estimate that variance. Raw inputs/results are absent from this public snapshot,
so neither spread nor effective provider effort can be reconstructed here.
The historical pin recommendations below are superseded by this qualification;
no new model evaluations were run. See [HYGIENE.md](HYGIENE.md).

## Table

| Model (served id) | Effort (requested) | Recall repeat spread | Lane | Recall found/defects | 95% CI | Real-only found/16 | Planted found/8 | False-pass 5/5 | Critical missed | Sev exact | Lat p50 / p95 (s) | Tokens/review | Lane util after run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Opus 5.5 (`claude-opus-5-5`) | medium | unmeasured (1 rep) | claude-lane-2 (7d) | 15/24 = 0.62 | 0.43–0.79 | 7/16 | 8/8 | 0/19 | 0/2 | 0.55 | 27.0 / 45.7 | ~46k | 13% of 7d |
| Sonnet 5.5 (`claude-sonnet-5-5`) | medium | unmeasured (1 rep) | claude-lane-2 (7d) | 13/24 = 0.54 | 0.35–0.72 | 6/16 | 7/8 | 0/19 | 0/2 | 0.38 | 10.7 / 22.0 | ~46k | 13% of 7d |
| Astra (`gpt-6-astra`) | medium | unmeasured (1 rep) | codex-lane-1/2 (wk) | 12/24 = 0.50 | 0.31–0.69 | 4/16 | 8/8 | 2/19 | 0/2 | 0.62 | 16.5 / 29.3 | ~31k | 31–33% of wk |
| Sol (`gpt-6.1-sol`) | medium | unmeasured (1 rep) | codex-lane-1/2 (wk) | 11/24 = 0.46 | 0.28–0.65 | 4/16 | 7/8 | 3/19 | 0/2 | 0.67 | 16.7 / 31.7 | ~31k | 31–33% of wk |
| Muse 1.3 (`muse-spark-1.3`) | medium | unmeasured (1 rep) | meta (no Keeper) | 11/24 = 0.46 | 0.28–0.65 | 4/16 | 7/8 | 6/19 | 0/2 | 0.69 | 29.6 / 56.0 | ~34k | n/a (tokens only) |
| GLM-5.3 (`glm-5.3`) | medium | unmeasured (1 rep) | zai-lane-1 (wk) | 11/24 = 0.46 | 0.28–0.65 | 4/16 | 7/8 | 0/19 | 0/2 | 0.46 | 108.8 / 275.3 | ~35k | 43% of wk |
| Codex auto-review (`codex-auto-review`) | medium | unmeasured (1 rep) | codex-lane-1/2 (wk) | 9/24 = 0.38 | 0.21–0.57 | 2/16 | 7/8 | 3/19 | 0/2 | 0.56 | 23.2 / 53.9 | ~29k | 31–33% of wk |

Notes:
- These are single-run observations, not a model ranking. All pairwise quality,
  safety and cost-effectiveness differences are withheld until same-effort,
  same-input repetition spread is available.
- Planted cases cannot establish real-world recall. Needs-context defects are
  confounded by inline changed-file-only context; no arm could inspect other files.
- No blind TP/FP judging was performed, so precision is only a lower bound.

## Recommendation

Do not change model pins or promote an arm to a required gate based on this
singleton table. Earlier winner/switch recommendations are withdrawn as
unsupported by measured repeat variance. Round 2 changes effort and is not an
independent repeat of round 1. This hygiene update authorizes no additional
model calls or spend.

## Deviations from protocol

All 7 arms ran medium effort, same 19 cases, same harness, same day, one
repetition each. The full list, with reasons, is under "Deviations" in
`docs/review-bot/bakeoff-protocol.md`. The ones that change how to read this
table:

- The reviewer had no repo access, only inline context (protocol section 4), so
  needs-context results are confounded.
- No blind TP/FP judging: precision is a lower bound, not a measured rate.
- Quota per review is post-run lane utilisation only, no per-batch Keeper delta.
- One repetition per cell: no run-to-run spread.

## Next

The second pass ran (`RESULTS-r2.md`). The Greptile comparison did not use the
19-case subset: it ran as the separate Corpus B in case_a84d4bc9 (see
`RECOMMENDATION.md`). What remains is the advisory pilot on `two-web-next`
with formal approvals off, which waits on the gates listed in
`RECOMMENDATION.md`.
