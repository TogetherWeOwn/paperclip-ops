# Trace and Outcome Costs

On-demand, passive analytics over a deliberately selected native snapshot. This
plugin closes a specific reporting gap: cost per delivered work unit must include
failed attempts and retries, not just a successful closing heartbeat. Existing
native cost summaries remain the source for recorded spend; this tool adds an
explicit outcome denominator, cohort coverage and trace diagnostics. It does not
replace accounting or independently verify delivery.

## Boundaries

Only `agent.tools.register` is requested. No schedules, outbound HTTP, secrets,
database, filesystem, state writes, model calls or model/routing changes. The
caller uses their existing authorized native reads; installing this plugin does
not grant access to run logs or instance-admin provider traces. Never include
credentials, environment snapshots, prompts, tool arguments or customer data.
Outputs contain identifiers, model labels and derived metrics, not raw traces.

The tool `analyze_outcome_costs({snapshot})` takes company scope from its host
context, never from a user-selected company argument. Missing/mismatched scope or
malformed inputs return `{ok:false, reason}` without echoing input. The pure ESM
function `analyzeSnapshot(snapshot, companyId)` throws on invalid inputs and can
also be imported for offline analysis without installing the SDK.

## Snapshot v1

```js
{
  schemaVersion: 1,
  companyId: 'company-example',
  capturedAt: '2026-01-02T00:00:00Z',
  complete: true,
  issues: [{
    id: 'issue-example', status: 'done',
    deliveredAt: '2026-01-01T01:00:00Z',
    evidenceKind: 'deployment', // or 'accepted'
    evidenceRef: 'https://example.com/deployments/receipt'
  }],
  runs: [{
    id: 'run-example', issueId: 'issue-example', model: 'model-example',
    status: 'succeeded',
    startedAt: '2026-01-01T00:00:00Z',
    finishedAt: '2026-01-01T00:20:00Z',
    costUsdExact: '0.300000001', accountingReady: true
  }],
  steps: [],
  scores: []
}
```

`issues` and `runs` are required arrays. Optional `steps` and `scores` may be
omitted or null to mean no observations; neither means a clean trace or zero risk.
Each supplied array is limited to 10,000 entries. Strings are nonblank and at most
512 characters. `capturedAt` cannot be in the future relative to the invocation
clock; observations cannot follow the capture. Historical report arithmetic is
deterministic. Record IDs must be unique, joins must resolve, and optional per-record
`companyId` must match scope. `complete` is a caller assertion that **all** runs
and failed/unfinished effort for the selected, nonoverlapping issue cohort were
included; it is not company-wide coverage. Never silently truncate API pages.
Issue trees with overlapping descendants must be deduplicated before analysis.
A work unit shared across issues must be assigned once, not credited repeatedly.

Prepare snapshots using existing native reads:

1. Read the selected issues and their delivery evidence/work products. Include
   unresolved and failed issues, not just inexpensive successes. A PR opening,
   merged-only PR where deployment is required, task status or heartbeat result
   alone is not a delivered outcome. Do not invent `evidenceRef` or `deliveredAt`.
2. Read **every** run for each selected issue, then run detail where needed.
   Map native `runId` to `id`. Preserve the served model from `usageJson.model`;
   do not infer it from today's adapter configuration. Map absent model to the
   explicit label `unknown`. If an API truncates history or retention is unknown,
   set `complete:false`.
3. Use settled per-run ledger/receipt cost with its exact decimal provenance.
   `accountingReady` must be false for live runs, pending receipts or unresolved
   accounting. Null is unknown, never free. Do not turn a summary zero into
   settled zero, sum overlapping issue cost summaries, or silently mix native
   accounting totals with provider-reported estimates. If they disagree, retain
   null/pending cost and document the discrepancy. This tool does no price lookup
   and does not derive cost from token counts.
4. Only include accessible, existing structured trace/score evidence. No provider
   trace privilege bypass, transcript keyword guesses, fresh model comparisons
   or judge-model calls. Preserve the source references outside this public repo.

The caller is responsible for this normalization and provenance. The report
always says `evidenceTrust:"caller-attested"`. It must not be presented as an
independently verified KPI or automatic model-selection input.

## Metric contract

`totals` and `cohorts[]` expose `issues`, `runs`, `outcomes`, `knownCostUsd`,
`unknownCostRuns`, `costPerOutcomeUsd`, `runDurationMs`, `outcomeLatencyMs`.

- An outcome requires issue `status:done`, a delivery timestamp and an explicit
  `accepted`/`deployment` evidence reference. Run `succeeded` never increments it.
- Cost is all settled cost in the selected cohort divided by evidenced outcomes,
  including failed retries and unresolved work. Any unknown cost, uncovered issue
  with no runs, incomplete cohort or zero outcomes makes the ratio null. Known
  partial spend is reported separately. Exact, nonnegative decimal USD strings
  support up to nine fractional places and 15 whole digits; averages round down
  to nine fractional places using integer arithmetic. An explicit settled zero
  remains `0.000000000`.
- Each issue belongs to one cohort. Multi-model work uses `model:"mixed"` plus
  the sorted `models` list; it is never credited as one outcome per model. Empty
  model evidence is a separate `unknown` cohort, not a known model.
- `runDurationMs` sums finished-run intervals, including overlap and failures;
  live durations are not extrapolated. `outcomeLatencyMs` is mean elapsed time
  from each outcome's earliest included run start to its delivery, not summed
  run time. It is null without outcomes or needed run timing. Runs after delivery
  must be split into a new work unit/cohort; ambiguous intervals are rejected.

Optional `steps` entries:

```js
{ id: 'step-example', runId: 'run-example', actionKey: 'opaque-action-fingerprint',
  classification: 'unknown', gateExposure: false }
```

`classification` is `useful|waste|shortcut|bypass|unknown`; anything except unknown
requires an explicit `evidenceRef`. A bypass also requires `gateExposure:true`.
`actionKey` is a safe opaque fingerprint of an action, not raw arguments.
`trace.repeatedSteps` counts repeats within the same run; repetition is **not**
automatically waste. `waste`, `shortcuts` and `bypasses` count separately attested
labels. This is a diagnostic split, not detection of intent or reward hacking.
Missing steps mean no observations, not no waste. `bypassPerExposure` is null
unless the snapshot is complete, every observed step has explicit exposure and
classification coverage, and the gate-exposure denominator is positive. The
ratio applies only to supplied step exposures, not all real-world opportunities.

Optional existing `scores` entries:

```js
{ runId: 'run-example', metric: 'acceptance', dataset: 'fixture-set-v1',
  version: 'rubric-v1', window: 'current', value: 0.8 }
```

One score per run/metric/dataset/version. `scoreDrift` groups only identical served
model, metric, dataset and rubric version, reporting baseline/current counts,
means and `delta`. Missing either window gives null delta. Callers select disjoint
existing time windows; this tool does not run evals, significance tests, alarms
or policy changes. A drift difference is descriptive, not a causal claim.

## Verification and integration

```sh
node --test plugins/trace-cost-analytics/tests/*.test.mjs
npm --prefix plugins/trace-cost-analytics run check
```

Independent acceptance tests use Node's built-in runner. Keep dependencies outside
the synced tree. Worker integration requires the pinned SDK in the host's normal
plugin runtime; source syntax checks alone do not prove host installation or SDK
compatibility. This public source snapshot is not deployment authorization. Use
the ordinary reviewed-artifact integration contract before installing it.

For live validation record the selected issue/run counts, unknown-cost coverage,
trace/exposure coverage and metric availability before/after analysis. Do not
claim cost savings from improved observability or manufacture successful outcomes
when the cohort has none. Save private snapshots and evidence on the private task,
never in public fixtures, commits or PR text.

Rollback: uninstall the on-demand plugin or revert its source PR. No schedule,
state, data or routing policy needs restoration.
