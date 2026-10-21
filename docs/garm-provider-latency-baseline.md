# GARM provider latency baseline

`garm_provider_latency_baseline.py` records GARM provider operation latency
(create/delete, milliseconds) into a baseline file and checks fresh samples
against it. Provider latency only: pool health, reaper TTL, queue wait and
runner lag are separate instruments. Local-only: two input files in, at most
the baseline file out. No network, credentials, host access or discovery.
Sample collection from live API metadata is an operator step, not a function.

## Commands

- `init --baseline B --source S` — write a provisional baseline at an absent
  path. Refuses when `B` exists; never overwrites evidence.
- `record --samples S --baseline B --source S [--recorded-by R] [--now T]` —
  append validated samples, recompute statistics, publish atomically. An
  absent `B` initializes first with seed thresholds (no recalibration).
- `check --samples S --baseline B` — verdict fresh samples vs thresholds.
  Read-only; never creates or changes `B`.

`--json` on any command prints exactly one finite JSON outcome object
(schema `garm-provider-latency-outcome.v1`, see below) instead of text.
`--dry-run` validates and computes the plan with zero writes. Unknown or
conflicting flags refuse without writes.

## Tri-state check

| Exit | Verdict | Meaning |
| --- | --- | --- |
| 0 | ok | Mature baseline, fresh samples within thresholds |
| 2 | breach | Mature baseline, an operation exceeds p50/p95 |
| 3 | unknown | Baseline absent/provisional/invalid, or an operation has no fresh samples |
| 1 | — | Usage or validation failure (bad shape, bad flags, invalid existing baseline on record/init, publication failure) |

Unknown is not permission to page and not permission to relax: the instrument
has no standing, so the decision must be made on something else and said so.

## Integrity rules

- Stored samples face the same domain as fresh ones: number, not bool,
  finite, >= 0, <= 6h (21600000 ms). Duplicates are data; windows hold at
  most 2000 samples per operation, oldest evicted first.
- Maturity is established from validated stored evidence: both operations
  need >= 10 samples (nearest-rank p50/p95, exact, no interpolation).
  Stored count/statistics/provisional/provenance metadata must reconcile
  exactly; a `provisional=false` flag over thin evidence is forged maturity
  and refuses.
- Absent vs invalid: an absent path initializes; an existing unreadable,
  malformed or inconsistent baseline is refused without replacing
  thresholds/history or truncating bytes. No silent reset, no auto-repair.
- Publication is atomic (temp file plus rename; the temp file inherits the
  existing baseline's permission bits, contents are fsynced before the
  rename and the directory after it). A failed rename reports exit 1 and
  the prior valid file survives.

## `--json` outcome contract (repo-owned)

One object, finite JSON, stable IDs:

- `schema`: `garm-provider-latency-outcome.v1`; `operation`: init/record/check.
- `baseline_schema` / `samples_schema`: the existing v1 input schema IDs.
- `source`: `{baseline: <input path>, samples: <input path or null>}`.
- `validity` / `maturity` / `coverage`: each `{ok|mature|adequate: bool,
  reason: <nonempty string>}`. On `record`, `coverage.adequate` is true
  only when stored evidence holds >= 10 samples per operation (the same
  threshold as maturity), in dry-run, published and publish-failed
  outcomes alike.
- `code`: stable string (`ok`, `breach`, `unknown_provisional`,
  `unknown_missing_baseline`, `unknown_missing_samples`,
  `invalid_baseline`, `unreadable_baseline`, `invalid_samples`,
  `invalid_arguments`, `baseline_exists`, `publish_failed`, `usage_error`);
  `retryable`: bool; `next_action`:
  nonempty remediation. `check` outcomes also carry `verdict`.
- Extra informational fields (e.g. `details`, `baseline`, `dry_run`) may be
  present; consumers must ignore unknown fields.

## Storage format

Baseline schema `garm-provider-latency-baseline.v1`: `provisional` bool,
per-operation `{count, min_ms, p50_ms, p95_ms, max_ms, samples}`,
`thresholds` seed alerts per operation, `provenance` with `total_samples`
equal to the stored sample count. Samples schema
`garm-provider-latency-samples.v1`: `{schema, samples:
[{operation, duration_ms}]}`.

Seed thresholds are round numbers above each operation's physical floor,
marked provisional until the first recalibration after 30 live samples per
operation. Recalibration and any deployed-baseline activation are separate,
explicitly authorized steps — never a side effect of this tool.
