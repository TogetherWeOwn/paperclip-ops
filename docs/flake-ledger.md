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

## Ranked triage report

Start triage from the N flakiest jobs/tests instead of querying one name:

```sh
python3 ops/productivity/flake_ledger.py report <repo> [--top N] [--min-observations M] [--json] [--dry-run]
```

Rows matching `<repo>` exactly rank by flip rate (`flips / observations`),
highest first. Ties break deterministically: observations descending, then
job name ascending, then test name ascending. `--top N` defaults to 10 and
accepts integers 1-100 only. `--min-observations M` defaults to 5 and
accepts positive integers only; rows below the gate list as `insufficient`
with counts and never appear in the ranking, regardless of `--top`.

The report applies the same strict validity as `is_flaky`: every ledger row
must be well-formed (including other repos' rows), and duplicate (job, test)
pairs or test-vs-job collisions across different rows make the whole report
unavailable (non-zero exit + explicit reason), never silently combined. A
row whose own test equals its own job is one row, not a collision. Unlike
`is_flaky`, job summary rows (`job <job>`) carry no precedence: every row
ranks as a row.

Text output prints one greppable row per line (ranked rows, then
insufficient rows), byte-deterministic across runs. `--json` prints a
pure-JSON envelope on stdout: `schema_version` 1, `repo`, `generated_at`
(ISO-8601), `top`, `min_observations`, ranked `rows` (stable `id` plus
job/test/flips/observations/rate), `insufficient` rows with counts,
`excluded` counts with reasons, and `error` (null on success, or
`{code, retryable, next_action}` on failure).

Stable row IDs contain three slash-separated components: repository, job, and
test. Within each component, literal `%` becomes `%25` first, then `/` becomes
`%2F`; all other characters stay unchanged. This preserves tuple boundaries
and keeps literal percent sequences distinct from escaped slashes. For example,
`(r, a/b, c)` has ID `r/a%2Fb/c`, while `(r, a%2Fb, c)` has ID
`r/a%252Fb/c`. IDs do not depend on ranking or the observation gate.

The report is read-only: it never writes the ledger, reruns tests, or files
cards. `--dry-run` is a documented no-op alias producing byte-identical text
output. Unknown flags, a missing or blank repo, and extra positionals refuse
with usage + exit 2; unavailable evidence exits non-zero with an explicit
reason. `report --help` prints the full usage.

## Snapshot comparison

Compare two saved ledger snapshots to see which supplied evidence changed,
without rerunning anything:

```sh
python3 ops/productivity/flake_ledger.py compare <repo> --before FILE --after FILE [--limit N] [--dry-run]
```

`--before` and `--after` are both mandatory explicit local files (the same
file may be supplied twice). Compare never falls back to `HOME`,
`FLAKE_LEDGER_PATH`, live collection, or the ledger writer, and it never
writes its inputs or any sidecar. `--dry-run` is a no-op alias with
byte-identical output. `--limit` defaults to 100 and accepts integers
1-1000 only.

The output is exactly one `flake-snapshot-comparison.v1` JSON object:
`schema`, `status`, `code`, `retryable` (always false), `next_action`,
`repo`, `limit`, `window_verified`, `continuity_verified`, `sources`,
`total`, `returned`, `truncated`, `category_counts`, and `rows`.
`sources` carries the SHA-256 of the exact supplied `--before`/`--after`
bytes and no file paths. Rows are the union of the exact repo's
`(job, test)` tuples from both snapshots, sorted by job then test; `--limit`
truncates that sorted list, while `total`, `category_counts`, and status are
computed over the full set before truncation. Each row carries only `id`
(the same `%`/`/` escaping as the report), `job`, `test`, `category`, the
`before`/`after` cumulative `flips`/`observations`/`rate` (rate is null for
zero observations; an absent side is null), and a descriptive `change`.

Each tuple falls in exactly one category:

- `compared`: present in both snapshots and neither counter decreased;
  `change` holds the non-negative counter differences, plus the rate
  difference when both cumulative rates exist.
- `reset_suspected`: present in both, but flips or observations decreased.
  The tuple is incomparable: every `change` value is null, never a negative
  count or an improvement claim.
- `added_evidence` / `missing_evidence`: present only in the after / before
  snapshot. These never mean a test was newly introduced or fixed.

`window_verified` and `continuity_verified` are always false: even monotone
counters do not prove the same cohort or window, an interval rate,
causation, or a live trend. Read the comparison as descriptive triage only.

Exit codes separate the states. A fully described comparison exits 0
(`complete`/`compared`). Any `reset_suspected` row makes the result
`partial`/`partial_reset_suspected` with exit 1. Usage errors (missing or
duplicate flags, a blank repo or file value, an invalid `--limit`, or any
other flag) refuse with exit 2 and usage on stderr only. Unusable evidence
(a missing, unreadable, empty, malformed, duplicate, or ambiguous snapshot,
or no rows for the repo in either snapshot) also refuses with exit 2, as one
`unavailable` JSON object with zero counts and no `sources`; a repo absent
from a snapshot is unavailable evidence, never a verified zero. Errors never
echo raw rows, parser exception bodies, or absolute paths. Both snapshots
must pass the same strict row validation as the report, including other
repos' rows, and ambiguous rows are never combined. `compare --help` prints
the full usage with an example.

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
