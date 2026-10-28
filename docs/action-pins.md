# Action pin check

`.github/scripts/action_pins.py` is the offline, read-only supply-chain
gate that every `uses:` is SHA-pinned: `owner/repo@<40-hex>` for third-party
actions and reusable workflows, `./...` for local actions, or
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
of the host locale.

## JSON contract: `action-pins.v1`

A `--json` check prints exactly one deterministic JSON object to stdout —
no annotations, prose, or tracebacks on stdout or stderr:

- `schema`: exactly `"action-pins.v1"`.
- `ok`: true only when the scan is complete with no findings and no errors.
- `scan_complete`: true only when at least one input was requested and every
  requested input was read and checked. A complete scan can still have
  findings (`ok` false, `scan_complete` true).
- `requested`, `checked`, `refused`: input counts with
  `requested == checked + refused`. Explicit mode counts the path arguments;
  default mode counts discovered files. Each refused input has one `errors`
  entry. Missing evidence is never a clean result.
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

  `no-inputs-discovered` is scan-level: all counts are 0 and
  `scan_complete` is false. Findings from checked inputs survive a partial
  input failure.
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
default-discovered input failures (including a directory, a non-regular file
such as a FIFO, or invalid UTF-8),
deterministic ordering, unusual filenames, help/usage boundaries and
no-write behaviour. They run in the `pr-lint` CI job; the acceptance file is
maintained independently of the implementation. This source contract is not
an installation or deployment claim.
