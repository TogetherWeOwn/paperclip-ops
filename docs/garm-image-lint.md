# GARM image lint

`garm_image_lint.sh` (thin wrapper over `garm_image_lint.py`, standard library
only) structurally lints checked-in runner Dockerfiles against the isolated
image profile at `github-runner/garm/isolated-image-profile.json`. It is fully
offline: it never builds, pulls or runs an image, executes Dockerfile text,
invokes docker/podman/hadolint, touches the network or writes files. The
check is its own dry-run; no flag can reach production.

## Usage

```
garm_image_lint.sh [--json] [--] <Dockerfile> [...]
```

- `--json` (opt-in, anywhere before `--`, repeatable) prints exactly one
  `garm-image-lint.v1` object on stdout instead of text diagnostics; see the
  contract below. Without it, diagnostics keep their historic stderr form
  (`RULE <code> <file>:<line> <detail>`) and stdout stays empty.
- `--` ends flag parsing: every later token is a file operand, even one
  starting with `-`.
- `-h`/`--help` (before `--`) prints human help and exits 0, even combined
  with `--json`. Help names `--json` so the machine form is discoverable.
- Any other flag (`--json=true`, `--profile`, `--apply`, `--write`,
  `--execute`, `--production`, ...) is a parser error: exit 2, usage on
  stderr, empty stdout, and **no scan and no partial JSON** — a bad flag
  after a failing file never reports that file's findings.
- No operands is not an empty clean scan: exit 2 (text: usage on stderr;
  JSON: a `NO_INPUT` report in state `not_scanned`).

## Rules (unchanged policy)

Nine stable structural rules: `GARM-BASE-01` (single pinned profile base),
`GARM-USER-02` (final non-root USER), `GARM-PRIV-03` (no sudo / Docker surface),
`GARM-APT-04` (pinned installs, `--no-install-recommends`, same-layer cleanup,
no upgrades), `GARM-COPY-05` (no ADD), `GARM-EXEC-06` (JSON exec form),
`GARM-SECRET-07` (no credential-shaped ENV/ARG or credential flags),
`GARM-FETCH-08` (no piped remote installers) and `GARM-FORMAT-09` (supported
syntax only, no floating `latest`, no `MAINTAINER`). Operands of a finding are
never echoed, so a credential-shaped value cannot leak into either output.

## Exit codes

| Exit | Meaning |
| --- | --- |
| 0 | Every requested input was evaluated and no rule failed |
| 1 | At least one rule failed — even when other inputs were refused |
| 2 | Refusal or invalid invocation (no input, bad flag, unreadable/undecodable input, missing/invalid profile) with no rule failure |

The 1-over-2 precedence is deliberate: an observed rule violation is the
stronger signal, and both modes report the same exit for the same batch.

## `--json` contract: `garm-image-lint.v1`

Exactly one finite JSON object on stdout; stderr is empty for every handled
outcome. Fields (frozen meanings; consumers ignore unknown fields):

- `schema`: `"garm-image-lint.v1"`; `exit_code`: integer equal to the
  process exit status.
- `scan_complete`: `true` iff at least one input was evaluated and none was
  refused. `measurement_state`: `"complete"`, `"partial"` (some evaluated,
  some refused) or `"not_scanned"` (none evaluated). A complete scan may
  still contain rule violations — this is structural evidence only, not image
  execution, admission, deployment, a vulnerability audit or a build receipt.
  A partial or empty scan never claims complete/clean.
- `requested_files` / `evaluated_files` / `refused_files`: raw operand
  strings in argv order, duplicates included. Evaluated and refused
  partition the requested occurrences: a decoded file with a structural
  finding (including whole-file `GARM-FORMAT-09`) is *evaluated*; a file that
  cannot be read or decoded is *refused*. A global profile failure refuses
  every requested occurrence without scanning.
- `counts`: `{requested, evaluated, refused, findings}` — nonnegative
  integers mirroring the lists (`evaluated + refused = requested`).
- `findings`: `{code, file, line, message, retryable, next_action}` with an
  existing `GARM-*` rule ID, 1-based line (`0` for a whole-file parse
  refusal), `retryable: false`, `next_action: "fix_input"`. Order follows
  input occurrences, then `(line, code)` within each occurrence, independent
  of which output mode was selected.
- `errors`: same shape with `line: 0`, `retryable: false`, and a stable
  remediation per code: `INPUT_READ_ERROR` → `restore_input`,
  `INPUT_DECODE_ERROR` → `encode_utf8`, `PROFILE_READ_ERROR` →
  `restore_profile`, `PROFILE_INVALID` → `fix_profile`, `NO_INPUT` →
  `provide_input`. Per-input errors follow argv order. Profile errors name
  the isolated profile path; `NO_INPUT` is the only error with `file: null`.
  Read/decode refusals are errors, never structural `GARM-FORMAT-09`
  findings; in text mode a non-UTF-8 file reports `file is not valid UTF-8`
  under `GARM-USAGE` and exits 2 instead of a traceback.

Repeated runs of either entrypoint over the same inputs emit byte-identical
JSON, so agents can diff reports instead of parsing prose.
