# Reviewer-model bake-off, round 2: high-effort second pass (case_96f9122f)

Run 2026-10-04. Protocol `docs/review-bot/bakeoff-protocol.md` §2: second pass
varies effort for the top two arms only. Top two by round-1
severity-weighted recall: opus 25/35 = 0.714, sonnet 22/35 = 0.629
(third: astra 20/35 = 0.571). Same 19 cases, same harness, `high` effort,
rep 1. Raw results: `results/round2-high/` (38 files, secret-scanned).
Two diagnostic retries of the failed cases in `results/round2-high-retry/`.
Scorer: `score.py` (failed runs count zero-found per protocol §5).

## Table (authoritative `score.py` output, full denominators)

| Model (served id) | Effort | Runs/failed | Recall found/defects | 95% CI | Diff-only | Needs-context | Prec-lb | Sev exact | False-pass 5/5 | Critical missed | Lat p50 / p95 (s) | Tokens/review |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Opus 5.5 (`claude-opus-5-5`) | high | 19/1 | 14/24 = 0.58 | 0.39–0.76 | 11/17 | 3/7 | 18/45 = 0.40 | 0.39 | 0/18 | 0/2 | 35.4 / 79.6 | ~47k |
| Sonnet 5.5 (`claude-sonnet-5-5`) | high | 19/1 | 14/24 = 0.58 | 0.39–0.76 | 11/17 | 3/7 | 18/31 = 0.58 | 0.56 | 1/18 | 0/2 | 23.3 / 50.5 | ~46k |

## Medium → high (same method, full denominators)

| Arm | Recall | Weighted recall | Real-only recall | False-pass | Works (valid/19) |
| --- | --- | --- | --- | --- | --- |
| opus medium | 15/24 = 0.62 | 25/35 = 0.714 | 7/16 = 0.44 | 0 | 19/19 = 1.000 |
| opus high | 14/24 = 0.58 | 24/35 = 0.686 | 6/16 = 0.38 | 0 | 18/19 = 0.947 |
| sonnet medium | 13/24 = 0.54 | 22/35 = 0.629 | 6/16 = 0.38 | 0 | 19/19 = 1.000 |
| sonnet high | 14/24 = 0.58 | 24/35 = 0.686 | 7/16 = 0.44 | 1 (planted minor `p-0183` scored 5/5) | 18/19 = 0.947 |

## Verdicts

1. **High effort does not beat medium for either arm.** Recall deltas
   (−0.04 opus, +0.04 sonnet) and real-only deltas are within noise;
   weighted recall is flat-to-down for opus, up for sonnet but tied with
   opus-high at 0.686 — still below opus-medium's 0.714.
2. **Both high arms trip the protocol §7.1 Works gate** (needs ≥95% valid;
   both are 18/19 = 94.7%). Failure mode is identical and systematic:
   `codex exec` exits 0, burns tokens (17–103 s, 2–10k output tokens),
   writes no final message (`parse_error: no final message`). The two
   diagnostic retries reproduced it exactly on fresh runs
   (`results/round2-high-retry/`), so it is not teardown flake. Failed
   cases: `r-0036`/sonnet (real, 2 minor defects), `r-0227`/opus (real,
   1 minor defect).
3. **High effort made sonnet less safe**: its one false-pass is a planted
   minor the medium arm caught. No planted critical missed anywhere.
4. **Cost**: high costs ~same tokens as medium (~46–47k) but higher latency
   (opus p50 35.4 s vs 27.0 s; p95 79.6 s vs 45.7 s). Keeper delta on
   claude-lane-2 over the batch: 0.00 pp at 2-decimal resolution
   (0.13 → 0.13 of 7d) — below Keeper resolution per protocol §6, so
   tokens remain the comparable measure. Lane healthy throughout.

## Recommendation

Do NOT move the reviewer to high effort: no recall gain, a Works-gate
trip on both arms, one new false-pass, higher latency. Opus-medium
remains the best-recall arm (0.62 / weighted 0.714, zero false-pass);
the round-1 pilot-pin guidance (keep `gpt-6.1-sol` or switch to
`gpt-6-astra` on value) is unchanged — no arm is promoted to required
on this evidence. Advisory-pilot data plus the Greptile $0 head-to-head
remain the promotion inputs.

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
