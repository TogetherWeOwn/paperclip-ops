# Trace correlation: offline card/run/PR/head/deploy join

`ops/paperclip/trace_correlator.py` joins caller-assembled evidence into
queryable per-run trace records plus one aggregate lead-time SLO verdict.
It is read-only and offline: no network, no credentials, no board reads or
writes, no timer, no host install. It does not deploy, merge, approve or
gate anything. **Never put it in a transport/execution gate or use its exit
status to admit or refuse work.** It reports traces, not rollout approval.

## Documentation accompaniment

In `.docs-drift.json`, `trace-correlation-contract` maps the exact source path
`ops/paperclip/trace_correlator.py` to `docs/trace-correlation.md`. Once the mapping
is in the supplied target commit, a source change needs an added or content-modified
regular file at this doc path in the same patch; unrelated, deleted or chmod-only
docs do not count.

```sh
python3 scripts/quality-gates/docs_drift.py --base <full-target-sha> --head <full-pr-head-sha>
```

See [the gate contract](docs-drift.md) for base-owned policy and JSON/exit details.
Accompaniment is not semantic correctness, capacity admission, deployment
authorization or evidence freshness. This development-quality check does not
change `trace-correlation.v1`, its input/error/exit behavior or its read-only
operation. The correlator must never become an execution/transport gate; neither
its report nor its exit status authorizes or refuses work.

## Evidence contract

Feed one JSON document on stdin (strict UTF-8, at most 1048576 bytes,
duplicate keys and nonfinite numbers rejected):

```json
{
  "schema_version": 1,
  "runs": [
    {"runId": "run-1", "contextIssueId": "DEMO-1",
     "createdAt": "2026-10-20T10:00:00Z",
     "startedAt": "2026-10-20T10:02:00Z",
     "finishedAt": "2026-10-20T10:07:00Z"}
  ],
  "workProducts": [
    {"issueId": "DEMO-1", "createdByRunId": "run-1", "type": "pull_request",
     "repo": "demo-org/demo-repo", "pr": 7, "headSha": "aaaa... (40 hex)",
     "mergeSha": "bbbb... (40 hex, merged PRs only)"}
  ],
  "deployments": [
    {"repo": "demo-org/demo-repo", "sha": "aaaa... (40 hex)",
     "deployId": "2026.10.20.1+aaaaaaa",
     "mergedAt": "2026-10-20T10:07:00Z",
     "deployedAt": "2026-10-20T10:27:00Z"}
  ]
}
```

Run rows accept both native shapes: REST (`runId`/`contextIssueId`) and
SDK-summary (`id`/`issueId`), normalized explicitly. A work-product row is
a PR link only when it carries explicit `repo` (`owner/name`) and `pr`
(positive integer); `type`, `url` and titles are never linkage. `headSha`
is the PR head; merged PRs add the exact `mergeSha` (merge/squash commit),
because production deploys reference the merged commit, not the head. A
deploy row joins only on exact `repo` plus full 40-hex `sha` equality with
either sha; a merge-sha match wins over a head-sha match and the winner is
recorded as `shaMatch`. Timestamps are ISO strings; unreadable ones are
missing evidence, never an error. The producer includes production
deployment rows only: staging or synthetic rows would mislabel the
merge-to-prod span and must be excluded upstream.

The producer assembles this document from explicit native reads (issue
runs, work products, PR head SHAs, deployment rows) and defines the
cohort and window. The correlator cannot prove a producer's cohort claim.
It must not receive secrets, credentials, note bodies or private paths:
IDs, counts, reasons and timestamps only.

## Join contract

```sh
python3 ops/paperclip/trace_correlator.py < evidence.json
```

Output schema is `trace-correlation.v1`: `traces`, one `slo` row and one
`delivery` row. One trace per (run, PR-link) pair, sorted by
(card, run, repo, pr); runs without a link still emit a partial trace.

- Stages are seconds, rounded to milliseconds: `admissionToStartS`
  (created to started), `executionS` (started to finished),
  `mergeToProdS` (merged to deployed). Missing spans stay null, never
  zero, and every gap is named in `missing` (`run.finishedAt`,
  `pr_link`, `pr.headSha`, `deploy`, `deploy.mergedAt`, ...).
- Negative spans (clock skew) stay null with a `*_negative` marker.
- Mutable head refs (branch names, `latest`) are rejected
  (`link_head_not_exact_sha`), never resolved. A deploy matches on repo
  and full SHA against a link's `mergeSha` or `headSha`; a merge-SHA
  match wins. For the chosen repo and SHA, the earliest `deployedAt`
  wins; ties keep file order. A deploy that no trace selects is an
  orphan, including unselected duplicates: counted, never joined.
- Duplicate run IDs after the first are invalid (`run_duplicate_id`).
- Invalid rows are counted in `delivery.invalidRows` with their reasons
  in `delivery.unknowns`; they never abort the report.

## SLO contract

The `slo` row evaluates G4 merge-to-prod lead time (goal
`c753f77b-76ea-419c-867b-1578bdc9491a`): p50 under 3600 s, p90 under
10800 s. Percentiles use linear interpolation over eligible samples
only (traces with a measured `mergeToProdS`). Zero eligible samples is
verdict `unknown` with reason `zero_eligible_samples`, never met.
Thresholds are aggregate-only: per-trace rows carry no verdict, and a
single slow trace is not a breach.

## Delivery self-test

The `delivery` row measures whether the evidence pipeline delivers
joinable IDs: per-handoff coverage (`coverageRunCard` = valid run rows
carrying a (run, card) pair over rows seen; `coveragePrJoin` = explicit
PR links joined to a matching run and card; `coverageDeployJoin` =
deploys selected by a trace through `mergeSha` or `headSha`), orphan
counts (work products with no matching run/card and deployments no trace
selects, reported never guessed) and invalid counts. Coverage is receipt
coverage, not a success rate: a joined row with failed effects still
counts as joined. A silent producer shows up as missing evidence and an
`unknown` verdict, not as a clean board.

Exit 0 means the verdict is met; 1 means missed or unknown (read the
report: a breach or an evidence gap needs a human); 2 means unreadable
or invalid input with no report on stdout. The exit status is not the
verdict; consumers must read `slo.verdict`.

Until a steady evidence producer and its window are verified, every live
reading must state its cohort, its eligible-sample count and its
unknowns alongside the verdict.
