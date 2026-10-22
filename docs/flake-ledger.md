# Red-build triage and flake ledger

Use the uploaded nextest aggregate ledger as supporting evidence before
considering a rerun. The query is read-only and never reruns tests:

```sh
python3 ops/productivity/flake_ledger.py is_flaky <repo> <job-or-test>
```

The command reads `~/.local/share/flake-ledger.json` by default. Set
`FLAKE_LEDGER_PATH` when the uploaded ledger is stored elsewhere. It accepts
JSONL records or the JSON ledger envelope (`{"tests": [...]}`). Each evidence
record must contain these fields:

```json
{"repo":"two-bot-next","job":"rust tests","test":"sample_test","flips":2,"observations":5}
```

`observations` is the number of observed runs and `flips` is the number of
outcome changes in that evidence. All records must be valid, including records
that do not match the query. Rows match when `repo` is exact and the requested
value is an exact `test` or `job`. When the value matches different rows as a
test name and as a job name, the query is ambiguous and unavailable. Among job
matches, a summary row named `job <job>` takes precedence; otherwise the query
must identify exactly one row. Duplicate or ambiguous rows
are unavailable rather than combined, because separate test rows may describe
the same CI runs.

Interpret the result as follows:

1. `yes` means the matching evidence covers at least five runs and its flip rate
   (`flips / observations`) is strictly greater than 0.2. Record the evidence
   and consider the one permitted rerun under the applicable CI policy.
2. `no` means sufficient, readable evidence shows a flip rate of 0.2 or less.
   Treat the failure as real and fix it normally.
3. `unavailable: ...` with a non-zero exit means evidence is missing, unreadable,
   corrupt, malformed, unmatched, or covers fewer than five runs. Record
   **“ledger unavailable, treated as real”** and fix the failure normally,
   unless the Residual `unavailable` fallback below grants a one-time CEO
   exception.

Never treat unavailable evidence as `no`, and never rerun on your own
authority because the ledger could not be checked; the only exception is a
CEO grant under the Residual `unavailable` fallback below. The command prints
only the literal `yes` or `no` on a
successful decision; unavailable details are explicit and non-zero so callers
cannot mistake an evidence failure for a negative classification.

## Residual `unavailable` fallback

`unavailable` never authorizes a rerun by itself. The literal-`yes` rule above
still governs every self-authorized rerun: only a `yes` verdict permits the
one agent-initiated rerun. This section names the single exception — a
CEO-granted, Operator-executed rerun — and any proposal to widen it needs
security review first.

Eligibility mirrors the host-rerun exclusions below: security, supply-chain,
policy, deploy, migrate, drill and smoke jobs are never eligible, nor are
paid-hosted runs on private repos. Runs already at attempt 2+ are ineligible:
when the host controller has already consumed the ladder's one rerun, no
exception remains to grant. The fallback therefore serves only eligible
test-only checks still at attempt 1 because the controller did not rerun them
(non-PR builds, the `PR_CONTROLLER_CI_DISABLE` kill switch, or a controller
error) — the granted exception is that one rerun, never a second same-head
rerun.

When the ledger stays `unavailable` and no code fix exists for the red check,
escalate instead of rerunning:

1. **Who decides.** The agent holding the red build cannot self-authorize a
   rerun. Open a decision card to the CEO with the evidence below; the CEO
   grants or denies a one-time exception. Rerun authorization never comes from
   the red build's owner.
2. **What evidence.** The exception request carries: the exact `is_flaky`
   command plus verbatim `unavailable` output; proof the run is still attempt 1
   on the same head; the failing step and why no file in the diff can influence
   it; multi-branch coincidence (same step, same window, run ids) or a
   same-branch flip when available; a local reproduction of the failure
   signature when feasible; and recovery evidence for any external dependency
   involved.
3. **Attempt bound.** At most one same-head failed-jobs rerun per granted
   exception, executed by the Operator after re-verifying head and attempt 1.
   A repeat same-step failure is real: open a fix card, no further exception.
   Gates are unchanged: required checks green plus review on that exact head
   before merge.
4. **Ledger repair.** Every granted exception files a follow-up to restore the
   ledger evidence, matched to the `unavailable` cause in the verbatim output:
   insufficient runs → backfill the matching row until it has at least five
   valid, non-ambiguous observations; ambiguous rows (duplicates, or a test/job
   name collision) → remove or merge rows until exactly one row matches, since
   `eligibility()` raises ambiguous before it checks the five-run minimum
   (`ops/productivity/flake_ledger.py:90-119`); corrupt or invalid records in
   any row → repair or remove them first, since `read_ledger` validates every
   record and `record_flake` refuses corrupt or ambiguous ledgers
   (`flake_ledger.py:32-78,151-183`); missing or empty ledger file → recreate
   it, then backfill. Record future fail-then-pass pairs with `record_flake`.
   Close the follow-up only after rerunning the exact `is_flaky` query and
   observing a literal `yes` or `no`; never claim recovery from the observation
   count alone.

Record the decision (real / escalated with decision link) on the red-build
card, so every red build still has a recorded decision.

## Automatic host rerun of PR checks

The host PR controller consumes the ladder's one permitted rerun itself on
eligible PR heads. Before waking the author for a red required check, it
requests `gh run rerun <id> --failed` once per workflow run and holds the
wake until the retry concludes. A retry that passes on the same head is
recorded as one flake in this ledger (`record_flake`: two observations, one
flip); a retry that fails wakes the author as before and counts one CI
round. After 3 red rounds on different heads the controller posts one
re-scope comment instead of further wakes, until a head goes all green.
Three flakes for one job/test in a rolling 7 days open one `[flake]` card.

What this changes for agents: nothing about the verdict ladder above. The
literal-`yes` rule still governs every agent-initiated rerun, `unavailable`
still means "treated as real" except under the Residual fallback's CEO
exception, and nothing is ever dismissed without a ledger entry or an
escalated decision link. When the controller already reran a run (visible as
attempt 2
on the check), do not request a second rerun; fix the failure normally.
Only test-only workflows (check/ci/e2e/pr-gates basenames) are eligible:
security, supply-chain, policy, deploy, migrate, drill and smoke jobs are
never auto-rerun, nor are paid-hosted runs on private repos or runs already
at attempt 2+. A kill switch (`PR_CONTROLLER_CI_DISABLE`) restores
wake-on-first-red without a code change.
