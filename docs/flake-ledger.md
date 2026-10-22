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
   **“ledger unavailable, treated as real”** and fix the failure normally.

Never treat unavailable evidence as `no`, and never rerun because the ledger
could not be checked. The command prints only the literal `yes` or `no` on a
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

The report is read-only: it never writes the ledger, reruns tests, or files
cards. `--dry-run` is a documented no-op alias producing byte-identical text
output. Unknown flags, a missing or blank repo, and extra positionals refuse
with usage + exit 2; unavailable evidence exits non-zero with an explicit
reason. `report --help` prints the full usage.

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
still means "treated as real", and nothing is ever dismissed without a
ledger entry. When the controller already reran a run (visible as attempt 2
on the check), do not request a second rerun; fix the failure normally.
Only test-only workflows (check/ci/e2e/pr-gates basenames) are eligible:
security, supply-chain, policy, deploy, migrate, drill and smoke jobs are
never auto-rerun, nor are paid-hosted runs on private repos or runs already
at attempt 2+. A kill switch (`PR_CONTROLLER_CI_DISABLE`) restores
wake-on-first-red without a code change.
