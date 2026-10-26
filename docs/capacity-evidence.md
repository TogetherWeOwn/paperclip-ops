# Offline capacity evidence contract (version 1)

`./coolify_capacity_headroom.sh` replays supplied job evidence, not live capacity.
It never collects jobs, reads credentials, writes files, runs host commands or
changes pools. `--dry-run` is an alias for exactly the same read-only replay.
A GO is conditional on the caller's coverage assertions, **not authorization to
remove a host, increase caps or deploy**. Slots are logical shared slots, not cores.

## CLI and compatibility

```sh
./coolify_capacity_headroom.sh --help
./coolify_capacity_headroom.sh --jobs sample.json
./coolify_capacity_headroom.sh --jobs sample.json --json --dry-run
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_capacity_evidence_contract
bash test_coolify_capacity_headroom.sh
```

Requires Bash and Python 3 (standard library only). Keep the `.sh` entrypoint and
its sibling `.py` together. No `jq`, date subprocesses or network adapter is needed.
Supported value flags remain `--jobs`, `--node7b1ff338-max` (default 2),
`--node575e13db-max` (default 4), `--min-hours` (default 24), and
`--min-attempts` (default 10). Numeric values are nonnegative whole numbers;
leading zeros are decimal. These are existing report parameters, not new policy.
Every value flag requires a following value; a following `--flag` is not a value.
Empty numeric values, negative/fractional/non-finite values, repeated value
options and unknown flags refuse. Help does not suppress invalid options;
`--help` remains human-readable even when `--json` is present. No flag activates
live work. A missing Python interpreter or sibling module refuses with exit 2
and `UNREADABLE_INPUT` (also a JSON error when requested).

Exit **0** means a readable report, including measured **NO-GO** and readable
**INCONCLUSIVE**. Exit **2** means invalid/unreadable input or no usable compute.
Text retains `runner_minutes_total`, class minutes (`coolify`, `node7b1ff338`,
`garm`, `other`), exclusions, `span_h`, `matched`, peaks, `garm_slots`, `verdict`
and `reason`; new counts and per-arm fields are additive. Bare arrays and
`{jobs: [...]}` produce identical text, but no longer claim measured GO from
nominal slots alone. Thin-arm counts are never borrowed from another runner class.

## Evidence and counting

Accept a bare array or `{ "jobs": [...] }`. Strict UTF-8 JSON only: duplicate
object keys and non-JSON numeric constants refuse. Inputs must be regular files;
FIFOs/devices refuse without blocking. All-non-object arrays refuse; mixed
non-object rows are counted as malformed and invalidate completeness.

Each job has `runner_name`, `started_at`, `completed_at`. Optional `id` must be a
positive JSON integer (not boolean, float or numeric string). Names, status,
conclusion and cohort IDs, when non-null and supplied, must be strings; labels
must be an array of strings. Missing/null/empty runner names are **not compute**,
regardless of queued/cancelled clock spans. Assigned rows need real calendar UTC
clocks in `YYYY-MM-DDTHH:MM:SSZ` form and end strictly after start. A supplied
status must be `completed`; a supplied conclusion must be nonempty. Otherwise
exclude the attempt and mark evidence incomplete, never zero-fill its duration.
Legacy rows may omit status/conclusion when completed clocks are supplied.

Runner prefixes classify candidate `coolify-vps-*`, incumbent `garm-*`, known
static `ci-node7b1ff338-*`, and unclassified `other`. Static/other minutes remain
visible; only candidate + incumbent contribute to the shared-slot replay.
Unclassified assigned rows invalidate completeness rather than assuming they
consume no shared slots. Static rows do not manufacture either compared arm.

Repeated job IDs or runner/start/end tuples are excluded after the first valid
occurrence, including mixed ID-bearing/ID-less projections. Duplicates cannot increase
attempt adequacy and invalidate completeness (including conflicting repeats).
`job_ids` contains the sorted unique IDs of accepted compute rows. Queue-only
rows do not invalidate execution coverage; malformed/excluded compute and unknown
runner classes do. Counts remain separate: unknown rows are accepted compute,
whereas duplicates, bad clocks, malformed and no-runner rows are excluded.

## Measured residual capacity

A measured verdict additionally requires an explicit envelope:

```json
{
  "jobs": [],
  "evidence": {
    "cohort_id": "synthetic-trial-v1",
    "complete": true,
    "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T02:00:00Z"},
    "arms": ["coolify", "garm"]
  }
}
```

This **shape example is empty and refuses NO_COMPUTE**. For runnable positive,
saturated, non-overlapping and tie examples, the Test Author's immutable
`test_capacity_evidence_contract.py` owns the synthetic `sample()` and
`shared_load()` fixture constructors; the offline check above generates them in
run-owned temporary directories. No live Actions capture or pool query is needed.

The complete flag is a literal boolean. Both named arms must appear exactly once.
The cohort ID is a nonblank, unpadded string and every accepted compute row must
carry that same ID. The declared window must contain all accepted intervals.
The envelope is a **caller assertion**, never independent proof of inventory.

Each arm needs its own configured attempt count and observed min-start/max-end
span. Their observed windows must also overlap for at least `--min-hours`, with
positive overlap even if that threshold is zero. This avoids comparing disjoint
periods or borrowing the enclosing window as measured coverage. Coverage assumes
that absent executions inside the complete asserted window mean zero occupancy;
continuous execution is not required. Thresholds apply before rounding display
hours. Both arms must be present even with zero configured minimum attempts.

Replay uses `[start,end)` intervals: end/start ties are not overlap. Compute the
candidate peak, incumbent peak, and **time-aligned combined peak**, not the sum of
independent maxima. Under complete, adequate, aligned coverage:

- combined peak <= nominal slots: `GO / MEASURED_FIT`;
- combined peak > nominal slots: `NO-GO / CAPACITY_EXCEEDED`.

Otherwise report `INCONCLUSIVE / INSUFFICIENT_EVIDENCE`,
`residual_headroom_known=false`, and `combined_peak=null`. Missing provenance,
thin/missing arms, disjoint windows, contradictory window/cohort assertions or
excluded compute never create a free-slot estimate. A saturated incumbent and an
overlapping candidate cannot produce an unqualified fit. Replaying incomplete
evidence does not fix it: remediation requests new/corrected offline evidence,
not an automatic retry or a host action.

## JSON wire shape

`--json` emits one deterministic object on stdout (except human-readable
`--help`). `NO_COMPUTE` errors also retain the evaluated exclusion counts.
Reports and errors contain:

- `schema_version: 1`, `verdict`, stable `code`, `reason`;
- `retryable: false` (identical offline replay cannot repair input/coverage);
- nonempty `next_action`.

Reports additionally contain `counts` (`input_rows`, `accepted_rows`,
`excluded_no_runner`, `excluded_clock`, `malformed_rows`, `duplicate_rows`,
`unknown_rows`), `cohort_id` (null when unspecified/invalid), `job_ids`,
`arms.coolify` and `arms.garm` (`matched_attempts`, `observed_hours`, `adequate`,
`complete`, `window`), enclosing `window`, `evidence_complete`,
`aligned_observed_hours`, configured `thresholds`, `assumptions`, `runner_minutes`,
`span_h`, `peak_all`, and `capacity` (`nominal_slots`, `candidate_peak`,
`incumbent_peak`, `combined_peak`, `residual_headroom_known`). Completeness and
adequacy are separate: a complete but thin sample still has unknown residual
headroom. Report numbers describe only supplied observations, never current hosts.

Input refusal codes: `MISSING_OPTION_VALUE`, `INVALID_OPTION_VALUE`,
`UNSUPPORTED_OPTION`, `INVALID_JOBS`, `UNREADABLE_INPUT`, `NO_COMPUTE`.
Errors never echo raw job rows/file contents or tracebacks. Additional fields may
be added within version 1; consumers should branch on verdict/code, not prose.

## Offline verification boundary

The immutable acceptance suite executes the real entrypoint with an empty
credential environment, Linux Landlock read-only access and a seccomp network
fence. It denies private-canary reads, persisted writes and unapproved executable
invocations; positive controls prove those fences. Missing-value subprocesses
have deadlines. Unsupported Linux/ABI environments fail closed, not unsandboxed.
The parent harness alone writes synthetic fixtures/output captures. Supply
`PAPERCLIP_RUN_SCRATCH_DIR` or `PAPERCLIP_SCRATCH_DIR` to choose their location.
The legacy shell suite preserves class/minute/exit compatibility using synthetic
fixtures; neither suite calls a live service.
