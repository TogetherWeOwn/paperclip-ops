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
value is an exact `test` or `job`. An exact test match takes precedence over a
job match. For a job query, a summary row named `job <job>` takes precedence;
otherwise the query must identify exactly one row. Duplicate or ambiguous rows
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
