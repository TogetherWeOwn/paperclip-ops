# GARM ready-time baseline

`garm_ready_time_baseline.py` records GARM VM create-to-ready time into a
baseline file and checks fresh samples against it. It baselines runner
startup latency only: provider API latency, pool health, reaper TTL, queue
wait and job duration are separate instruments. A breach here means VMs are
slow to become ready, never that the pool is empty.

Local-only and source-only: the tool reads exactly the samples and baseline
files it is given, and writes only the baseline path on `init`/`record`.
No network, credentials, host access or discovery. Sample collection from
live read metadata is an operator step, not a function in the tool.

Samples are timestamp pairs, not bare durations: each row names its
instance and both clocks (`created_at`, `ready_at`, strict UTC Zulu
seconds), so every stored duration is re-derivable, ready-before-create
refuses instead of filing a negative, and durations above six hours refuse
as clock/join errors. Duplicate durations are data, not shape violations.
p50/p95 use the nearest-rank method on ascending sorted data.

## Commands

- `init --baseline B --source S` — write a provisional baseline with the
  seed thresholds.
- `record --samples S --baseline B --source S --recorded-by R [--now T]` —
  merge validated fresh samples into the baseline (bounded window of the
  newest 2000 samples) and recompute the stored statistics. A baseline
  holding fewer than 10 stored samples stays provisional.
- `check --samples S --baseline B [--deadline-summary]` — evaluate the
  fresh samples against the baseline thresholds and print one JSON
  verdict. Read-only, idempotent, and inherently a dry run: it writes
  nothing and repeated runs on unchanged inputs produce identical output.

## Tri-state check

| Exit | Verdict | Meaning |
| --- | --- | --- |
| 0 | ok | Mature baseline, fresh p50/p95 within thresholds |
| 2 | breach | Mature baseline, fresh p50 or p95 exceeds its alert |
| 3 | unknown | Baseline missing/unreadable/provisional, or too few stored samples |
| 1 | — | Usage or validation failure (bad shape or bad value in a supplied file) |

Breach is evaluated on the fresh samples only, never on the baseline's
stored history. Unknown is not permission to page and not permission to
relax: the instrument has no standing, so the decision must be made on
something else and said so.

Default `check` output is `{"verdict": ..., "detail": {...}}` (an
unreadable baseline reports `{"verdict": "unknown", "reason": ...}`).
Malformed inputs the existing parser refuses before any verdict exists —
for example an empty fresh-sample list or more than 2000 fresh rows —
keep exit 1 with the text diagnostic and no JSON verdict, with or without
`--deadline-summary`.

## `--deadline-summary` (completed-sample deadline exceedances)

Percentiles intentionally do not describe individual samples: a report
can be percentile-ok while one completed startup took far longer than the
per-VM deadline. The opt-in `--deadline-summary` flag on `check` (only
`check`; `init`, `record` and the top-level parser reject it) adds exactly
one extra top-level key, `deadline_summary`, to every JSON verdict `check`
emits. Removing that key leaves the default output unchanged, and the
verdict and exit code are identical with and without the flag.

The summary object has exactly these keys:

```json
{
  "schema": "garm-ready-deadline.v1",
  "evaluation_state": "evaluated",
  "ready_timeout_ms": 600000,
  "evaluated_sample_count": 100,
  "exceedance_count": 1,
  "max_excess_ms": 100000
}
```

- `schema` is always `garm-ready-deadline.v1`.
- `ready_timeout_ms` is the supplied baseline's
  `thresholds.ready_timeout_ms` — never a built-in constant and never a
  stored-history percentile.
- `evaluation_state` is `evaluated` exactly when `check` reaches an ok or
  breach verdict on the fresh samples (a mature, non-provisional baseline
  and at least one fresh sample). Then `evaluated_sample_count` is the
  number of fresh samples (duplicates are separate observations),
  `exceedance_count` counts fresh durations strictly greater than
  `ready_timeout_ms` (a completed sample exactly at the deadline is not
  an exceedance), and `max_excess_ms` is the largest
  `duration - ready_timeout_ms` among exceeding samples, or `0` when none
  exceed. Only the fresh samples are measured; the stored history never
  contributes.
- Every unknown verdict (provisional, missing or unreadable baseline)
  reports `evaluation_state: "not_evaluated"`: `evaluated_sample_count`
  is `0`, `exceedance_count` and `max_excess_ms` are `null` (unavailable,
  never a quiet zero), and `ready_timeout_ms` is the supplied value when
  a readable, shape-valid baseline was loaded, else `null`.

Counts are JSON integers and durations finite JSON numbers. The summary
carries counts and durations only: no instance names, no input bodies,
no provenance or other private metadata.

### What the summary is not

The summary is diagnostic only. It never changes the p50/p95 breach
logic, threshold values, baseline maturity or validation, verdicts or
exit codes, and it is not a gate: a percentile-ok report can legitimately
contain deadline exceedances, and a breach report can contain none
against a raised supplied deadline. Because it measures completed
samples only, it cannot detect unfinished instances — a VM still starting
has no `ready_at` and never enters the sample set — and it neither proves
live health nor authorizes rollout. Timeout enforcement and reaper
policy stay separate, explicitly authorized decisions.

## Storage format

Baseline schema `garm-ready-time-baseline.v1`: `provisional` flag,
`thresholds` (`p50_alert_ms`, `p95_alert_ms`, `ready_timeout_ms`, each a
finite number greater than zero, plus the seed rationale), one `startup`
operation holding `{count, min_ms, p50_ms, p95_ms, max_ms, samples}`, and
`provenance`. Samples schema `garm-ready-time-samples.v1`:
`{schema, samples: [{instance, created_at, ready_at}]}`.

Seed thresholds sit deliberately above the physical floor of the startup
path (LXD create, image boot, cloud-init, runner registration) and are
marked provisional until the first recalibration after 30 live startup
samples. The seed `ready_timeout_ms` is the proposed per-VM
create-to-ready deadline; the outer reaper bound is unchanged by this
tool.
