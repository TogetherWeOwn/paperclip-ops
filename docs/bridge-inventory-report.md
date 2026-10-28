# Bridge inventory report

`bridge_row_inventory.py --report-json` inventories caller-supplied assembled
source evidence. It is read-only, idempotent and inherently its own dry-run:
only the selected UTF-8 JSON fixture is opened; no live reads, network,
subprocess, writes, quota/reset requests, installation or policy actions occur.
Coverage is **not** live serving readiness, credential health or quota admission.
The existing model IDs, lane policy and helper signatures are unchanged.

## Synthetic invocation and focused check

From the repository root, without setup or live configuration:

```sh
python3 -B bridge_row_inventory.py --report-json \
  --fixture tests/fixtures/case_381fc549/bridge-assembled-rows.json
python3 -B -m unittest -v test_bridge_row_inventory
```

The check includes the immutable `test_bridge_inventory_report_acceptance` suite
through the existing unittest loader. Running that module separately alongside
the legacy module repeats its acceptance cases. One existing private-roster
comparison skips in this public snapshot; synthetic cases remain enabled.

## Wire schema: `bridge-inventory-report.v1`

Report mode writes exactly one finite JSON object and a newline to stdout, with
empty stderr, on completed analysis and handled input/usage errors. Object keys
are sorted; rows and diagnostics follow the existing `BRIDGE_MODELS` order, not
the fixture's array order. There are no timestamps or raw exception messages.

| Field | Meaning |
| --- | --- |
| `schema_id` | Always `bridge-inventory-report.v1`. |
| `outcome` | `complete`: every model has coverage; `findings`: completed analysis with one or more zeros; `unmeasurable`: no usable analysis. |
| `check_complete` | True for both `complete` and `findings`; false for `unmeasurable`. Never asserts live readiness. |
| `rows` | Every evaluated model, including missing, disabled and unlaned models. Each retains `model`, `tier`, `expectedLane`, `enabledLaneBoundCount`, `lanes`, `status` (`present` or `ZERO`). Empty array if unmeasurable. |
| `counts` | `{evaluated, present, zero}` model counts after completed analysis; `null` if unmeasurable, never fabricated zeros. |
| `diagnostics` | Ordered array of code objects; each `zero_coverage` also includes the existing stable `model_id`. Empty for complete coverage. |
| `retryable` | True only for `fixture_read_error`; a hint to retry after restoring access, not an automatic retry. |
| `next_action` | Nonempty human guidance. Branch on codes and fields, never parse this prose. No action is executed. |

For example, empty but valid evidence (`{"models":[],"lanes":[]}`) produces
`outcome: findings`, `check_complete: true`, three rows, counts
`{evaluated: 3, present: 0, zero: 3}` and three `zero_coverage` diagnostics.
Malformed input instead produces `outcome: unmeasurable`,
`check_complete: false`, `rows: []` and `counts: null`.

## Stable diagnostics and exits

| Code | Meaning | Exit | Retryable |
| --- | --- | --- | --- |
| *(none)* | Nonzero coverage for every bridge model. | 0 | false |
| `zero_coverage` | One evaluated model has no enabled lane-bound row; includes `model_id`. Every zero is reported, with healthy siblings retained. | 2 | false |
| `fixture_read_error` | Fixture missing, unreadable or not a regular file (including a FIFO or device). Non-regular files are refused before opening. | 2 | true |
| `fixture_decode_error` | Invalid UTF-8/JSON or a JSON decoder limit. | 2 | false |
| `fixture_schema_error` | Duplicate JSON object key, invalid model/lane shape, duplicate identity or invalid bridge enabled boolean. Report decoding rejects duplicate keys at every nesting level instead of accepting the last value; the existing analyzer handles row validation. | 2 | false |
| `usage_error` | Invalid CLI arguments or conflicting output modes. No fixture read or analysis. | 2 | false |

Machine errors never echo the fixture path, contents or raw exception text.
Legitimate identifiers in evaluated rows are JSON-encoded, including quotes,
backslashes, newlines and Unicode. Non-bridge rows remain ignored by coverage;
this is not a general-purpose roster schema validator.

`--help` remains human-readable help, exit 0, without reading evidence.
`--json` and `--report-json` are mutually exclusive in either order. Unknown
options and missing option values in report mode emit structured `usage_error`.
Existing argparse unique long-option abbreviations remain supported.

Without `--report-json`, the default human table and legacy `--json` array keep
their existing success bytes and refusal semantics: any zero exits 2 with prose
on stderr and no rows on stdout. The additive report does not replace those
interfaces or change the detector fixtures or CI/review gates.

## Observation and rollback

Within seven days of merge, replay the synthetic zero/input classes using the
focused check and sample up to five actual report uses if available. Record
synthetic coverage separately from real use; zero real uses is **unmeasured**,
not a successful adoption claim. Targets: every zero model recoverable from one
JSON result, no failed-input-as-clean outcomes and no exception-prose parsing.
This document creates no monitor, installation or host automation.

Undo the source/docs change with a normal revert, or stop opting into the new
flag. There is no deployed service, persisted state or policy to roll back.
