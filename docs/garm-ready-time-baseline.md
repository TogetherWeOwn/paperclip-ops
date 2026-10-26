# GARM ready-time baseline

`garm_ready_time_baseline.py` records GARM VM create-to-ready time
(runner startup latency, derived from operator-supplied
`created_at`/`ready_at` UTC timestamp pairs) into a baseline file and
checks fresh samples against it. Startup only: provider API
create/delete latency, pool health/capacity, queue wait and job duration
are separate instruments. Local-only: two input files in, at most the
baseline file out. No network, credentials, host access or discovery.
Sample collection from live read metadata is an operator step, not a
function in the script.

## Commands

- `init --baseline B --source S` — write a provisional baseline at `B`
  (overwrites the given path; init semantics are unchanged).
- `record --samples S --baseline B --source S --recorded-by R [--now T]`
  — merge validated fresh samples into the stored baseline, recompute
  statistics, keep at most the newest 2000 samples.
- `check --samples S --baseline B` — verdict fresh samples vs stored
  thresholds. Read-only (its inherited dry-run); never writes `B`.

## Tri-state check

| Exit | Verdict | Meaning |
| --- | --- | --- |
| 0 | ok | Valid mature baseline, fresh samples within thresholds |
| 2 | breach | Valid mature baseline, fresh p50 or p95 exceeds its threshold |
| 3 | unknown | Baseline absent/unreadable, genuinely provisional, or stored evidence invalid |
| 1 | — | Usage or validation failure (bad fresh samples, invalid existing baseline on `record`, write failed) |

Unknown is not permission to page and not permission to relax: the
instrument has no standing, so the decision must be made on something
else and said so. `check` prints one JSON object with `verdict` plus
`detail` (or `reason` for an unreadable baseline file).

## Integrity rules

A stored baseline is evidence, not a claim: maturity is established
from its validated stored history, never from a caller-set flag. Before
`check` verdicts or `record` merges, the stored document (schema
`garm-ready-time-baseline.v1`) must reconcile exactly:

- `operations.startup` is an object; `samples` is a list of finite
  numbers (booleans and strings are not numbers), each from 0 through
  six hours (21600000 ms), at most 2000 samples. Duplicates are data.
- `count` is an integer equal to `len(samples)`;
  `min_ms`/`p50_ms`/`p95_ms`/`max_ms` equal the nearest-rank
  recomputation from the stored samples (rank =
  ceil(p/100 * n) on ascending sorted data), and are null exactly when
  the history is empty. Integer-spelled statistics equal to the
  recomputed value are valid.
- `provisional` is a boolean equal to `count < 10`: thin history
  claiming maturity and mature history claiming provisional standing
  are both inconsistent.
- `provenance` is an object whose `total_samples` is an integer equal
  to `count`.

Any mismatch — wrong types, stale or forged metadata, impossible
statistics — is refused, never silently repaired or reset:

- `check` answers `unknown` (exit 3) with the existing verdict/detail
  keys plus the additive machine field `detail.code` =
  `"invalid_baseline"` and a human `detail.reason` remediation string.
  No traceback; the baseline file stays byte-identical.
- Library `evaluate` mirrors that as
  `("unknown", {"code": "invalid_baseline", "reason": ...})`.
  Existing library signatures (`evaluate`, `record`, `init_baseline`,
  `parse_samples`, `percentile`, `summarize`) are unchanged.
- `record` applies the same validator before mutating: invalid stored
  evidence returns the existing validation failure (exit 1;
  `InvalidInput` in the library) and leaves the document/file bytes
  intact.
- Genuinely empty/provisional baselines initialized by the tool and
  valid mature documents it wrote keep their existing verdicts
  (unknown without the code; ok/breach) — fresh-sample validation and
  threshold policy are unchanged.

Validation boundary: the validator reconciles the stored document with
itself. It does not prove the samples were ever measured, contact any
host, or authorize any rollout; a valid mature baseline means only
that the stored evidence is internally consistent.

## Validation

```sh
python3 -B -m unittest -v test_garm_ready_time_baseline
python3 -B -m unittest -v test_garm_ready_time_stored_integrity
```
