# Reviewer-model bake-off, round 1: quality vs cost (case_96f9122f)

Run 2026-10-04. Dataset: 19 cases (11 real merged-then-fixed PRs from
`two-web-next` + 8 planted bugs: 2 critical, 3 major, 3 minor). Every arm ran
`codex exec` at medium reasoning effort through CLIProxy — the same transport
the provisioned reviewer (`codex_local`) uses. Lane health from Keeper
2026-10-04T04:50Z. 133/133 runs, 0 failed.

Raw results: `results/<model>-medium-r1/` (19 files each, secret-scanned).
Scorer: `score.py` (location match ±15 lines; unmatched findings are
"unjudged", so precision is a lower bound).

## Table

| Model (served id) | Lane | Recall found/defects | 95% CI | Real-only found/16 | Planted found/8 | False-pass 5/5 | Critical missed | Sev exact | Lat p50 / p95 (s) | Tokens/review | Lane util after run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Opus 5.5 (`claude-opus-5-5`) | claude-lane-2 (7d) | 15/24 = 0.62 | 0.43–0.79 | 7/16 | 8/8 | 0/19 | 0/2 | 0.55 | 27.0 / 45.7 | ~46k | 13% of 7d |
| Sonnet 5.5 (`claude-sonnet-5-5`) | claude-lane-2 (7d) | 13/24 = 0.54 | 0.35–0.72 | 6/16 | 7/8 | 0/19 | 0/2 | 0.38 | 10.7 / 22.0 | ~46k | 13% of 7d |
| Astra (`gpt-6-astra`) | codex-lane-1/2 (wk) | 12/24 = 0.50 | 0.31–0.69 | 4/16 | 8/8 | 2/19 | 0/2 | 0.62 | 16.5 / 29.3 | ~31k | 31–33% of wk |
| Sol (`gpt-6.1-sol`) | codex-lane-1/2 (wk) | 11/24 = 0.46 | 0.28–0.65 | 4/16 | 7/8 | 3/19 | 0/2 | 0.67 | 16.7 / 31.7 | ~31k | 31–33% of wk |
| Muse 1.3 (`muse-spark-1.3`) | meta (no Keeper) | 11/24 = 0.46 | 0.28–0.65 | 4/16 | 7/8 | 6/19 | 0/2 | 0.69 | 29.6 / 56.0 | ~34k | n/a (tokens only) |
| GLM-5.3 (`glm-5.3`) | zai-lane-1 (wk) | 11/24 = 0.46 | 0.28–0.65 | 4/16 | 7/8 | 0/19 | 0/2 | 0.46 | 108.8 / 275.3 | ~35k | 43% of wk |
| Codex auto-review (`codex-auto-review`) | codex-lane-1/2 (wk) | 9/24 = 0.38 | 0.21–0.57 | 2/16 | 7/8 | 3/19 | 0/2 | 0.56 | 23.2 / 53.9 | ~29k | 31–33% of wk |

Notes:
- No arm missed a planted critical (0/2 missed everywhere). The safety
  separation is in false-passes: in this round the Claude arms and GLM never
  passed a buggy case 5/5 (round 2: Sonnet at high effort passed 1 of 18);
  Muse passed 6/19 buggy cases clean — disqualifying for a gate.
- **Real-only recall is the number to quote.** The 24 defects are 16 real
  and 8 planted. Every arm finds 7-8 of the 8 planted bugs, which lifts the
  headline recall. On the 16 real defects the arms find: Opus 7 (0.44,
  95% CI 0.23-0.67), Sonnet 6 (0.38), Astra 4, Sol 4, Muse 4, GLM 4 (each
  0.25, CI 0.10-0.49), Codex 2 (0.12). Only Opus reaches the protocol's
  0.40 bar (section 7.5) on real cases, and it misses 9 of 16. The pinned
  Sol misses 12 of 16. Planted cases cannot support real-world recall
  claims (protocol 3.2).
- Confidence intervals overlap, so recall ordering beyond opus-first is
  suggestive, not proven. Needs-context defects (7/24) score lowest for
  every arm (best: opus 3/7), but this is **confounded**: the harness gave no
  arm access to files outside the PR's changed files, so no arm could have
  read the context those defects need (protocol section 4, Deviations).
- Reviewer agent is currently pinned to `gpt-6.1-sol` per spec. Sol is
  mid-pack on recall with 3 false-passes.
- Roster caveat (pre-registered): `claude-sonnet-5` is T2 disabled ("never
  on Claude while sol/luna available") — eval traffic only, not a routing
  proposal. Muse rows are `-free` Zen rows, disabled; quota unmeasurable.

## Recommendation

For the advisory pilot, keep the reviewer's `gpt-6.1-sol` pin OR switch to
`gpt-6-astra`: astra matches sol's cost/lane (~31k tokens, codex lanes at
~1/3 weekly) with slightly better headline recall (0.50 vs 0.46) and fewer
false-passes (2 vs 3). On real defects only the two are tied at 4/16; Astra's
extra find is one planted case. The gap is within noise — either is defensible;
do not re-pin on cost alone.

Do NOT promote any arm to a required check on this evidence: best overall
recall is 0.62 with overlapping CIs, and on real defects only (the figure that
matters) the best arm finds 7 of 16 and misses 9. Promotion needs the
advisory-pilot data (two weeks per plan). The second pass at higher effort for
the top two arms has since run (`RESULTS-r2.md`): it did not help.

Muse 1.3 is excluded from reviewer duty: 6/19 false-passes fail the gate's
safety bar regardless of recall. Codex auto-review trails the field.
GLM-5.3 is a cost-effective zero-false-pass option only if p95 ~275 s
latency is acceptable for advisory reviews.

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
