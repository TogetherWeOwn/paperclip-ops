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

## Hygiene qualification (2026-10-25)

**Comparative conclusions below are withdrawn, not validated by the historical
tables.** Corpus A has one repetition per arm; its repeat spread is unmeasured.
Corpus B's effort, repetitions and source bytes are unverified here. Neither
corpus can support a reported model difference, an effort benefit or a winner
without matched effort and measured same-input repeat spread. No extra model
calls were made to fill these gaps. See [HYGIENE.md](HYGIENE.md).

## Quality vs cost (descriptive historical observations only)

Cost is tokens per review plus the lane window it spends. No arm cost money; every
lane is a flat subscription. Keeper's per-account delta was below its resolution
for single batches, so tokens are the comparable measure (protocol section 6).

| Model | A: effort (requested) | A: recall repeat spread | A: recall | A: real-only recall | A: false-pass 5/5 | A: tokens | A: p50 latency | B: effort | B: recall repeat spread | B: recall of Greptile | B: precision | Lane |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Opus 5.5 | medium | unmeasured (1 rep) | 0.62 | 7/16 = 0.44 | 0/19 | ~46k | 27 s | unrecorded | unmeasured | 53% | 56% | claude-lane-2 (7d) |
| Sonnet 5.5 | medium | unmeasured (1 rep) | 0.54 | 6/16 = 0.38 | 0/19 | ~46k | 11 s | unrecorded | unmeasured | 51% | 57% | claude-lane-2 (7d) |
| Astra | medium | unmeasured (1 rep) | 0.50 | 4/16 = 0.25 | 2/19 | ~31k | 17 s | unrecorded | unmeasured | 31% | 89% | codex lanes (wk) |
| Sol (reviewer's pin) | medium | unmeasured (1 rep) | 0.46 | 4/16 = 0.25 | 3/19 | ~31k | 17 s | unrecorded | unmeasured | 27% | 85% | codex lanes (wk) |
| GLM-5.3 | medium | unmeasured (1 rep) | 0.46 | 4/16 = 0.25 | 0/19 | ~35k | 109 s | unrecorded | unmeasured | 42% | 49% | zai-lane-1 (wk) |
| Muse 1.3 | medium | unmeasured (1 rep) | 0.46 | 4/16 = 0.25 | 6/19 | ~34k | 30 s | unrecorded | unmeasured | 38% | 51% | meta (no Keeper) |
| Codex auto-review | medium | unmeasured (1 rep) | 0.38 | 2/16 = 0.12 | 3/19 | ~29k | 23 s | unrecorded | unmeasured | not run | not run | codex lanes (wk) |

Greptile on corpus B: 55 of its 56 findings valid (98%), 2.8 findings per PR
(effort unrecorded, repeat spread unmeasured; unverified descriptive observation).

Read the A recall columns with care. Corpus A has 24 labelled defects: 16 real and 8 planted.
Every arm finds 7-8 of the 8 planted bugs, so the headline recall is lifted by cases that
nearly every arm catches (protocol 3.2: planted cases cannot support real-world recall
claims). The real-only column is the figure to quote. It also inherits the harness limit
that no arm could read files outside the PR's changed files (protocol Deviations 1).

Two-stage, corpus B, same blinding (operator; effort unrecorded and repeat
spread unmeasured for every configuration): Opus finder then strict Sol
verifier gives 31% recall at 92% precision; Opus and Sonnet, deduplicated, then a
lenient Sol verifier gives 56% at 58%. Bar was at least 50% recall at 80% or
higher precision. **No configuration met it.**

## Comparative conclusions

Withheld. The historical model-family, model-ordering, safety-difference and
medium-versus-high claims were not supported by same-effort repeat variance.
The values above remain a record of observations, not comparative findings.
Corpus B is also unverified from this snapshot. Selected diagnostic retries do
not establish repeat spread for a complete arm.

## Recommendation

Do not select or re-pin a model, introduce a two-stage flow, or promote a required
gate from these tables. Earlier winner-based recommendations are withdrawn.
Existing production decisions and separate security/pilot gates are not changed
by this qualification. No new evaluation, routing change or spend is authorized.
Any future comparison needs an owner request, upfront cost estimate, recorded
matched effort, isolated execution and balanced same-input repeat evidence.

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
