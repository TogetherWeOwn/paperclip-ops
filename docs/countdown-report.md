# Countdown diagnostic report contract (`countdown-report.v1`)

`claude_countdown.py`, `codex_countdown.py` and `muse_countdown.py` are
read-only, offline readouts over a supplied JSON fixture. Their default
mode is strict and unchanged: one dishonest lane refuses the whole readout
(exit 2, message on stderr, nothing on stdout).

`--report-json` is an opt-in diagnostic mode for debugging a fixture in one
run instead of one refusal at a time. It never probes a live account, never
requests or consumes a reset, never writes a file, and says so in every
report: the `disclaimer` field states the report is diagnostic observations
only — **not admission or readiness authorization**.

## Usage

```sh
python3 claude_countdown.py --fixture lanes.json --now 2040-01-02T03:00:00Z --report-json
python3 codex_countdown.py --fixture lanes.json --now 2040-01-02T03:00:00Z --report-json
python3 muse_countdown.py --fixture lanes.json --now 2040-01-02T03:00:00Z --report-json
```

`--report-json` is mutually exclusive with `--json`. In report mode there
is exactly one JSON object on stdout and nothing on stderr — including for
bad clocks, unreadable fixtures and usage errors, which become diagnostics
instead of argparse refusals. Exit is `0` only when `check_complete` is
true, otherwise `2`. Output is byte-deterministic for the same fixture and
clock (`sort_keys`, finite JSON only).

## Report shape

| Field | Meaning |
| --- | --- |
| `schema_id` | Always `countdown-report.v1`. |
| `provider` | `claude`, `codex` or `muse`; `readout` is `<provider>_countdown`. |
| `as_of` | The actual evaluation instant (explicit valid `--now`, kept verbatim including fractional seconds, or the real UTC clock). `null` only when no clock could be established (invalid `--now`, or a usage failure before a clock). Never the fixture's `observedAt`. |
| `outcome` | `complete`, `incomplete` or `unmeasurable` (below). |
| `check_complete` | True iff `complete`. |
| `counts.input` / `evaluated` / `invalid` | Source records, records checked, and **distinct invalid records** (not diagnostic count — one record can carry a field diagnostic plus `duplicate_lane`). All three are `null` when parsing or usage prevents evaluation; a known-empty fixture is `0/0/0`. |
| `observations_partial` | True iff `incomplete`: observations are a partial subset. |
| `observations` | Unambiguous valid rows only, in input order, with the five legacy row fields (`lane`, `weeklyUtilization`, `headroomPct`, `hoursToReset`, `resetsAt`) computed with the existing validators and arithmetic. No averaging, no shared-reset snapping, no invented capacity. |
| `diagnostics` | One entry per defect (below). |
| `disclaimer` | The not-admission/readiness disclaimer. |

Outcomes:

- `complete` — non-empty input, every record valid, no diagnostics.
- `incomplete` — non-empty parsed input with at least one invalid record.
  Valid siblings appear as explicitly partial observations.
- `unmeasurable` — empty input, or input/usage that cannot be evaluated.
  Never successful capacity.

## Diagnostics

Each diagnostic is `{code, lane, source_index, retryable, next_action}`.
`lane` is the record's own identity or `null` (missing/invalid identity is
reported by zero-based `source_index`, never by an invented name);
`source_index` is `null` for fixture-level and usage errors. No raw input
value, path or message is echoed — actions describe repairing an offline
fixture, never probing an account.

| `code` | `retryable` | `next_action` |
| --- | --- | --- |
| `invalid_lane_record` | no | `provide_lane_object` |
| `invalid_lane_identity` | no | `provide_lane_identity` |
| `duplicate_lane` | no | `disambiguate_lane_identity` |
| `invalid_utilization` | no | `provide_finite_utilization` |
| `invalid_reset` | no | `provide_valid_reset` |
| `expired_reset` | yes | `provide_current_window` |
| `empty_input` | no | `provide_lane_records` |
| `invalid_now` | no | `provide_valid_utc_now` |
| `usage_error` | no | `correct_arguments` |
| `fixture_unreadable` | yes | `provide_readable_fixture` |
| `fixture_encoding` | no | `provide_utf8_fixture` |
| `fixture_json` | no | `provide_valid_json` |
| `fixture_shape` | no | `provide_lane_list` |

Rules worth calling out:

- **Every** independently invalid record is diagnosed in one run; report
  mode never stops at the first bad lane. `counts.invalid` counts distinct
  records, so it is not `len(diagnostics)`.
- Duplicate lane identities are ambiguous: **all** members are invalid
  (regardless of their other fields or order), none is picked, averaged or
  kept as an observation, and each member gets a `duplicate_lane`
  diagnostic — plus its own field diagnostic if it also has one.
- Utilization and reset validation, provider reset semantics and row math
  are the strict readouts' own (`parse_ts`, `[0, 1]` finite utilization,
  reset strictly after `as_of`); report mode adds no second capacity model.
- Duplicate JSON object keys refuse as `fixture_json` in report mode
  (ambiguous input); legacy mode keeps its historical parser.

## Examples

Complete:

```json
{"schema_id": "countdown-report.v1", "provider": "claude",
 "readout": "claude_countdown", "as_of": "2040-01-02T03:00:00Z",
 "outcome": "complete", "check_complete": true,
 "counts": {"input": 1, "evaluated": 1, "invalid": 0},
 "observations_partial": false,
 "observations": [{"lane": "sample", "weeklyUtilization": 0.5,
   "headroomPct": 50.0, "hoursToReset": 24.0,
   "resetsAt": "2040-01-03T03:00:00Z"}], "diagnostics": [],
 "disclaimer": "Diagnostic observations only; not admission or readiness authorization."}
```

Partial (valid `sample` plus a null-utilization sibling): `incomplete`,
`check_complete: false`, `counts` 2/2/1, `observations_partial: true`,
`sample` alone in `observations`, and
`{code: invalid_utilization, lane: <sibling>, source_index: 1, retryable: false, next_action: provide_finite_utilization}`.

Unmeasurable (invalid UTF-8): counts `null/null/null`, no observations,
`observations_partial: false`, one `fixture_encoding` diagnostic with null
lane and source index.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  test_claude_countdown test_codex_countdown test_muse_countdown \
  test_countdown_report_acceptance
```

The acceptance suite pins this contract mechanically across all three
providers (schema/key/code correspondence, deterministic bytes, legacy
all-or-nothing controls, and an audit-hook guard proving report mode has
no network, subprocess or write side effects). Legacy default tables,
`--json` arrays, exits and strict refusals are unchanged.
