# Review bot: quality-vs-cost table and recommendation (case_96f9122f)

Closes the evaluation slice of case_96f9122f. Date 2026-10-04. Two independent
datasets, both at $0 and with no production traffic:

- **Corpus A (this directory):** 19 `two-web-next` cases, 11 real merged-then-fixed
  PRs plus 8 planted bugs. 7 models, `codex exec` through CLIProxy (the
  provisioned reviewer's transport), 171 runs. See `RESULTS-r1.md`, `RESULTS-r2.md`.
- **Corpus B (case_a84d4bc9, operator run):** 20 public upstream `paperclipai` PRs
  that Greptile had already reviewed; Greptile's 56 first-review inline findings
  are the answer key. 7 models, chat-only, blinded, Opus-judged. Numbers below are
  the operator's, as recorded on case_a84d4bc9. This seat could not read
  `ops/greptile-eval/REPORT-20261004.md` (not on `origin/main`), so they are
  **unverified here**.

## Quality vs cost

Cost is tokens per review plus the lane window it spends. No arm cost money; every
lane is a flat subscription. Keeper's per-account delta was below its resolution
for single batches, so tokens are the comparable measure (protocol section 6).

| Model | A: recall | A: real-only recall | A: false-pass 5/5 | A: tokens | A: p50 latency | B: recall of Greptile | B: precision | Lane |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Opus 5.5 | 0.62 | 7/16 = 0.44 | 0/19 | ~46k | 27 s | 53% | 56% | claude-lane-2 (7d) |
| Sonnet 5.5 | 0.54 | 6/16 = 0.38 | 0/19 | ~46k | 11 s | 51% | 57% | claude-lane-2 (7d) |
| Astra | 0.50 | 4/16 = 0.25 | 2/19 | ~31k | 17 s | 31% | 89% | codex lanes (wk) |
| Sol (reviewer's pin) | 0.46 | 4/16 = 0.25 | 3/19 | ~31k | 17 s | 27% | 85% | codex lanes (wk) |
| GLM-5.3 | 0.46 | 4/16 = 0.25 | 0/19 | ~35k | 109 s | 42% | 49% | zai-lane-1 (wk) |
| Muse 1.3 | 0.46 | 4/16 = 0.25 | 6/19 | ~34k | 30 s | 38% | 51% | meta (no Keeper) |
| Codex auto-review | 0.38 | 2/16 = 0.12 | 3/19 | ~29k | 23 s | not run | not run | codex lanes (wk) |

Greptile on corpus B: 55 of its 56 findings valid (98%), 2.8 findings per PR.

Read the A recall columns with care. Corpus A has 24 labelled defects: 16 real and 8 planted.
Every arm finds 7-8 of the 8 planted bugs, so the headline recall is lifted by cases that
nearly every arm catches (protocol 3.2: planted cases cannot support real-world recall
claims). The real-only column is the figure to quote. It also inherits the harness limit
that no arm could read files outside the PR's changed files (protocol Deviations 1).

Two-stage, corpus B, same blinding (operator): Opus finder then strict Sol
verifier gives 31% recall at 92% precision; Opus and Sonnet, deduplicated, then a
lenient Sol verifier gives 56% at 58%. Bar was at least 50% recall at 80% or
higher precision. **No configuration met it.**

## What the two corpora agree on

1. **Precision and recall trade off by family.** Claude arms find more and are
   noisy (B precision 56-57%) and in round 1 never passed a buggy case 5/5 on A (Sonnet at
   high effort passed 1 of 18 in round 2). Codex arms
   are quiet (B precision 85-89%) and find fewer; on A they pass 2-3 buggy cases
   clean.
2. **Astra is at least as good as Sol on both corpora** (A: 0.50 vs 0.46 recall,
   2 vs 3 false-passes; B: 31%/89% vs 27%/85%) at the same cost and lane. On A's real
   defects alone they are tied at 4/16; Astra's extra find is a planted case. Every
   gap is inside the confidence interval, so this is a direction, not a result.
3. **Muse 1.3 is out** for gate duty: 6 of 19 buggy cases passed clean. Its quota
   is also unmeasurable (no Keeper record).
4. **High effort buys nothing** (round 2): recall flat, one new false-pass,
   latency up, and both arms tripped the 95% Works gate on a systematic
   `codex exec` no-final-message failure.
5. **No arm clears the promotion bar.** Best recall is 0.62 on A overall but 0.44 on
   its real defects (Opus, 7 of 16); on B it is 53%. The best arm misses 9 of the 16 real
   defects, and the pinned Sol misses 12 of 16 (finds 4). Planted bugs are the only
   cases every arm gets: 7-8 of 8.

## Recommendation

1. **Do not replace Greptile with the review bot.** Use the bot as a
   high-precision complement. The operator's complement design (Opus finder,
   strict Sol verifier) is a two-stage flow the native bot cannot run: it drives
   one reviewer agent per task. Adopting it is a CEO/Director product call and
   needs its own build; this seat filed no engineering work for it.
2. **Advisory pilot model: keep the reviewer on `gpt-6.1-sol`.** It is already
   provisioned, sits on the healthy codex lanes (about one third of the weekly
   window), costs no Claude quota, and keeps the pilot a single-variable change.
   Revisit Astra at the two-week checkpoint: if the Astra-over-Sol gap holds on
   pilot data, re-pin with one `Operator:` card. Do not re-pin on this evidence
   alone.
3. **Keep Claude arms off the pilot reviewer.** Opus has the best recall, but
   `claude-lane-2` was at 86% of its window earlier in the run and the roster
   holds Sonnet 5.5 disabled for routing. Opus is the right *finder* if the
   two-stage flow is ever built.
4. **Promotion to required is a separate decision** after at least two weeks of
   advisory data on `two-web-next`, with formal APPROVE and REQUEST_CHANGES off.
   Proposed bar from this evidence: the pilot's measured recall on defects that
   reviewers later confirmed is at least 0.50, false-pass is at most 5% of
   confirmed-buggy PRs, and zero planted or confirmed critical is missed. For scale, the
   best real-only recall measured here is 0.44 and the pinned Sol's is 0.25, so expect the
   pilot to start below this bar.

## What is not proven, and who acts

| Gap | Status | Next actor |
| --- | --- | --- |
| Configured-reviewer native run | Probe passed, standing use not yet allowed. Reviewer `00000000-0000-4000-8000-000000000008` (`codex_local`) was created with wake-on-demand off; the CEO turned it on (read back 2026-10-04) and run `ab199a7e` (`gpt-6.1-sol`, `-s read-only`) succeeded on the test task with a 2/5 review. It has no tools, so its only output is the final comment. Attribution: `/costs/by-agent` shows the tokens under the agent, but the runs are classed `metered_api` / unpriced (as for every CLIProxy-routed agent), so lane utilisation from CLIProxy is the quota reading. No real PR goes to it until the review-card convention and the CISO check on case_b9cd6a4b close. | CISO, then CTO |
| Review-bot GitHub App | Not confirmed registered or installed (Gate 2, case_edc76c48, in progress): org-owned App, installed on the private pilot mirror only. Handoff: `docs/review-bot/owner-handoff-github-app.md`. | Operator via CEO |
| Public check-run link leak | Ruled by the CTO (case_9da4adbc): run the pilot on a private mirror now, and request an upstream setting to omit task and run links in parallel (draft: case_e89a4fde). No run on the public `two-web-next` until then. | Operator (mirror); CEO publishes the upstream request |
| Corpus B report bytes | `ops/greptile-eval/REPORT-20261004.md` and raw data are not reachable from this seat. | Operator, when it lands |
| Run-to-run spread | One repetition per arm; the protocol asked for three on the top two. | Pilot data |

Rollback for everything above: set `wakeOnDemand:false` on the reviewer (stops all
dispatch), disable the repository in Paperclip, suspend the App installation,
`org_provisioner.sh deactivate` the reviewer. No spend and no
ruleset change is made by any step here.
