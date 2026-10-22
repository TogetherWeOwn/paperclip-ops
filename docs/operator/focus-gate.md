# Historical focus gate: single-company contract

`ops/paperclip/focus_gate.py` retains a historical within-company focus policy.
This does not authorize activation of historical focus restrictions contrary to
current owner priorities. Source completion is not a deployed runtime, an
installation, or a grant of host, DB, credential or cross-company authority.
Do not create `ACTIVE` or install this gate as part of verifying this repair.

## Scope and compatibility

The configured company is the source's `CID` constant, not a CLI-supplied target.
The candidate SQL positively restricts `q.company_id = CID` **before** negating
the existing focus predicate. Foreign runs, including those without an issue,
are not selected. The focus roots, closure and issue-less CEO exception are
unchanged; this repair defines no new admission or product-priority policy.

The query projects the run's company as its seventh column. Before any cancel
call, the gate refuses foreign, empty-company or malformed rows. A legacy
six-column adapter remains compatible under its existing already-scoped-company
contract; it cannot represent a foreign company. New adapters must return the
seventh company column, including an empty field for NULL, rather than dropping
it. `tick()` still returns `(candidate_count, confirmed_count)` or `(None, 0)`
when a DB timeout prevents measurement. Direct query failures still raise.
The historical rewake lookup also restricts issues to `CID`.

## CLI safety

- Default mode and explicit `--dry-run` preview only: no cancel, wake, or
  `cancelled.tsv` write. A preview does not create the state directory or alter
  `ACTIVE`, `allow.txt` or `supply_tier.json`. Diagnostic `gate.jsonl` entries may
  be appended if the state directory already exists.
- `--apply --dry-run` is rejected before any effects. Unknown flags and
  abbreviations are rejected, even alongside `--apply`.
- `--format text` is the compatible default. `--format json` emits one version 1
  object per tick, including an inactive result when no `ACTIVE` marker exists.
- `--loop SECONDS` retains loop operation; zero means one shot, negative values
  fail. `--rewake` previews by default and only wakes with `--apply`. JSON mode
  with `--rewake` fails closed: cancellation and wake results are not conflated.
- DB and API subprocess bounds remain in force. Never retry a refusal with a
  different credential. A transport failure or unknown response requires state
  verification before any retry, not an assumption that nothing happened.

`--help` is safe without a state directory. A normal candidate preview still
reads the DB if `ACTIVE` exists; it is **not** an offline check.

## JSON v1

Top-level fields are `version`, `company_id`, `dry_run`, `measurement`,
`error_code`, `counts`, `runs` and `next_action`. `measurement` is `inactive`,
`measured` or `unmeasured`; zero counts in an unmeasured result are not an
all-clear. Counts are `candidates` (locally scoped rows eligible for a cancel),
`refused` (scope/shape or API refusals) and `confirmed` (HTTP 200 only).
Candidate and refused counts can overlap when the API refuses a local candidate.
Each run carries stable `run_id` and `company_id`, plus `decision`, `scope_code`,
`error_code` (null when absent) and `next_action`. No timestamps enter the JSON
summary, so repeated synthetic previews are deterministic.

| Scope code | Meaning |
| --- | --- |
| `configured_company_excluded` | Local row excluded by the unchanged focus predicate |
| `foreign_company` | Adapter supplied a different company; refused before API call |
| `inconsistent_company` | Empty company or malformed row; refused before API call |

| Error code | Meaning / next action |
| --- | --- |
| `company_mismatch`, `inconsistent_row` | Inspect the candidate adapter; do not mutate this row |
| `query_timeout`, `query_failed` | Query unmeasured; inspect query failure |
| `query_or_processing_failed` | Processing unmeasured; inspect failure before retry |
| `cancel_refused` | HTTP 4xx; inspect refusal without credential retry |
| `cancel_transport_unknown`, `cancel_response_unknown` | Unknown effect; verify run state before retry |

Run decisions are `candidate`, `refused`, `confirmed` or `unknown`. Preview rows
remain candidates. Only HTTP 200 confirms a cancellation and appends the ledger;
4xx refuses it, while all other responses or transport failures are unknown.
A nonzero curl exit is unknown even if its captured stdout looks like `200`.
Neither refused nor unknown outcomes are labelled completed or recorded in the
cancellation ledger. One-shot query/processing errors, refusals and unknown
results exit nonzero; inactive and successful previews exit zero. Loop mode
continues with diagnostics, as before.

## Offline verification

From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/paperclip -p 'test_focus*.py'
```

The immutable scope acceptance and existing timeout suites use synthetic rows,
injected adapters and temporary state. They do not access a live DB, network,
host tools, or cancel real runs. Query-text assertions prove the positive company
predicate independently of adapter stubs. No deployed credential's cross-company
capability is tested or claimed.
