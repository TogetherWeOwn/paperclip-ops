# Action pin check

`.github/scripts/action_pins.py` is the offline, read-only supply-chain
gate that every `uses:` is SHA-pinned: `owner/repo[/path]@<40-hex>` for
third-party actions and reusable workflows (for example,
`octo/repo/.github/workflows/reusable.yml@<40-hex>`), `./...` for local actions, or
`docker://...@sha256:<64-hex>` for containers. Tags, branches, short SHAs and
missing values are findings. It never uses the network, `gh`, or writes a
file; running it is already its dry-run. Pin detection is identical in both
output modes.

```sh
python3 .github/scripts/action_pins.py --json [PATH ...]
```

Omit the paths to scan the default discovery set
(`.github/workflows/*.yml|*.yaml` plus
`.github/actions/**/action.yml|action.yaml`); `--json` may appear before or
after the paths. Omit `--json` for the existing `::error` annotation/prose
output used by the `pr-lint` job. `--help` prints the same summary. Input
files are decoded as UTF-8, and text output is emitted as UTF-8, independent
of the host locale. Text annotation file properties escape `%`, CR, LF, comma
and colon once; message escaping is separate. JSON paths stay verbatim.

Extraction supports block and nested flow mappings, quoted scalar keys with
JSON-compatible Unicode escapes, and explicit scalar keys (`? uses` / `: value`).
It attributes findings to the key's source line, checks every declaration on a
flow line, and skips quoted values and literal/folded block-scalar bodies rather
than interpreting shell text as declarations. This is not a general YAML
validator: anchors, aliases, tags and complex keys are not resolved, and arbitrary
YAML support is not claimed. Pin policy is the same for all supported key forms.

## JSON contract: `action-pins.v1`

A `--json` check prints exactly one deterministic JSON object to stdout —
no annotations, prose, or tracebacks on stdout or stderr:

- `schema`: exactly `"action-pins.v1"`.
- `ok`: true only when the scan is complete with no findings and no errors.
- `scan_complete`: true only when discovery succeeded, at least one input was
  requested and every requested input was read and checked. A complete scan can
  still have findings (`ok` false, `scan_complete` true).
- `requested`, `checked`, `refused`: file-input counts with
  `requested == checked + refused`. Explicit mode counts unique path arguments;
  default mode counts uniquely discovered candidate paths. Duplicate paths are
  counted once. Each refused file input has one `errors` entry, but directory
  enumeration errors are additional: `len(errors)` is not the refused-file
  count. Unknown children of an unreadable directory are not counted. Missing
  evidence is never a clean result.
- `findings`: pin violations sorted by `(path, line, code, value)`, each with
  `code` (`"unpinned-action"` in v1), `path` (explicit paths verbatim),
  `line` (1-based), `value` (empty for a missing `uses:` value), `message`,
  `retryable` (false) and `next_action`
  (`"pin_to_full_sha_or_digest"`).
- `errors`: input/scan failures sorted by `(path or "", code)`, each with
  `code`, `path` (null only for the scan-level `no-inputs-discovered` error),
  `message`, `retryable` (false: every v1 failure is a deterministic local
  condition) and `next_action`:

  | Error code | Next action |
  | --- | --- |
  | `input-missing` | `fix_input_path` |
  | `input-unreadable` | `fix_input_permissions` |
  | `input-invalid-encoding` | `fix_input_encoding` |
  | `no-inputs-discovered` | `provide_input_files` |

  `no-inputs-discovered` means successfully empty discovery: all counts are 0
  and `scan_complete` is false. Missing optional default roots are normal.
  Actual enumeration failures (including nested action directories) produce
  `input-unreadable` with the directory path and diagnostic, force
  `scan_complete=false`, `ok=false` and exit 1, and preserve accessible findings
  and file errors. Failed enumeration with zero discovered files reports the
  enumeration error, not `no-inputs-discovered`. For example, denied workflows
  plus one readable pinned local action yields requested=1, checked=1, refused=0,
  an `input-unreadable` directory error and an incomplete/failed verdict.
- `retryable`: false in v1. `next_action`: `"none"` when `ok`, else the first
  error's, else the first finding's, `next_action`.

Repeated identical runs produce identical stdout bytes. JSON escaping
preserves paths and messages with spaces, Unicode and control characters.

## Exit and usage boundaries

- **0:** complete, clean check.
- **1:** findings, refused input, or no inputs discovered — in JSON mode,
  read `ok`/`scan_complete`; exit 1 alone does not prove a pin violation.
- **2:** usage error (for example an unknown option): plain-text usage on
  stderr, empty stdout, no JSON object.

`--help`/`-h` always print plain text and exit 0, even combined with
`--json`. Parse stdout as JSON only for an actual `--json` check, never for a
help or usage response.

## Offline verification

```sh
python3 -B -m unittest discover -s .github/scripts -p 'test_action_pins.py'
python3 -B -m unittest discover -s .github/scripts -p 'test_action_pins_json_acceptance.py'
```

These cover the pin rules, text/JSON verdict parity, explicit and
default-discovered input failures (including a directory, a no-writer FIFO,
invalid UTF-8 and actual non-root enumeration denial), ASCII-locale text output,
the bounded extraction forms and shell-body controls, both annotation sites,
deterministic ordering, unusual filenames, help/usage boundaries and
no-write behaviour. They run in the `pr-lint` CI job; the acceptance file is
maintained independently of the implementation. This source contract is not
an installation or deployment claim.
