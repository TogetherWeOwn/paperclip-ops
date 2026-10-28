# Reviewer-model bake-off, round 2: high-effort second pass (case_96f9122f)

Run 2026-10-04. Protocol `docs/review-bot/bakeoff-protocol.md` §2: second pass
varies effort for the top two arms only. Top two by round-1
severity-weighted recall: opus 25/35 = 0.714, sonnet 22/35 = 0.629
(third: astra 20/35 = 0.571). Same 19 cases, same harness, `high` effort,
rep 1. Raw results: `results/round2-high/` (38 files, secret-scanned).
Two diagnostic retries of the failed cases in `results/round2-high-retry/`.
Scorer: `score.py` (failed runs count zero-found per protocol §5).

## Hygiene qualification (2026-10-25)

**All comparative differences are withheld.** Only one repetition per arm was
run; recall repeat spread is unmeasured, not zero. Medium versus high changes
effort, so it is rejected as a controlled model comparison. Diagnostic retries
of selected failures are not complete independent repetitions. Wilson intervals
are not run-to-run variance. Raw inputs/results are absent from this public
snapshot; no new model calls were made. See [HYGIENE.md](HYGIENE.md).

## Table (historical observations, full denominators)

| Model (served id) | Effort (requested) | Recall repeat spread | Runs/failed | Recall found/defects | 95% CI | Diff-only | Needs-context | Prec-lb | Sev exact | False-pass 5/5 | Critical missed | Lat p50 / p95 (s) | Tokens/review |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Opus 5.5 (`claude-opus-5-5`) | high | unmeasured (1 rep) | 19/1 | 14/24 = 0.58 | 0.39–0.76 | 11/17 | 3/7 | 18/45 = 0.40 | 0.39 | 0/18 | 0/2 | 35.4 / 79.6 | ~47k |
| Sonnet 5.5 (`claude-sonnet-5-5`) | high | unmeasured (1 rep) | 19/1 | 14/24 = 0.58 | 0.39–0.76 | 11/17 | 3/7 | 18/31 = 0.58 | 0.56 | 1/18 | 0/2 | 23.3 / 50.5 | ~46k |

## Historical medium/high observations (not a valid comparison)

| Arm | Effort (requested) | Recall repeat spread | Recall | Weighted recall | Real-only recall | False-pass | Works (valid/19) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| opus | medium | unmeasured (1 rep) | 15/24 = 0.62 | 25/35 = 0.714 | 7/16 = 0.44 | 0 | 19/19 = 1.000 |
| opus | high | unmeasured (1 rep) | 14/24 = 0.58 | 24/35 = 0.686 | 6/16 = 0.38 | 0 | 18/19 = 0.947 |
| sonnet | medium | unmeasured (1 rep) | 13/24 = 0.54 | 22/35 = 0.629 | 6/16 = 0.38 | 0 | 19/19 = 1.000 |
| sonnet | high | unmeasured (1 rep) | 14/24 = 0.58 | 24/35 = 0.686 | 7/16 = 0.44 | 1 (planted minor `p-0183` scored 5/5) | 18/19 = 0.947 |

## Qualified observations

Both high-effort arms produced 18 valid reviews out of 19, below the protocol's
95% Works bar. The two selected diagnostic retries again returned no final
message. These are recorded failures, not evidence of a model difference.
One high-effort review falsely passed a planted minor. Token/latency values in
the tables are descriptive observations only, not measured comparative gains.

## Recommendation

No winner, effort improvement, safety regression or model-pin change can be
inferred from these singleton cells. Earlier comparative verdicts are withdrawn
pending balanced same-effort repeats. No arm is promoted to required status on
this evidence, and this update authorizes no extra model calls or spend.

## Deviations from protocol

- Protocol §9 anticipated three repetitions for the top two arms to
  estimate run-to-run spread; only one high-effort rep was run (lane
  budget + the Works-gate trip made further high reps low-value).
  Spread estimation is open.
- 28 result files from the interrupted first launch referenced a
  teardown-cleaned scratch clone (`latency 0.0`, harness error) and were
  deleted, not scored; the relaunched batch re-ran those cases fresh.
  Genuine outcomes (including the 2 nulls) were kept.
- Keeper idle-interval noise floor (§6) not measured for round 2;
  reported delta is raw.
