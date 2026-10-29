# Action pin check

**Consumer:** local automation editing workflow/action files, and the existing
`pr-lint` job. `.github/scripts/action_pins.py` checks invocation references:
`owner/repo[/path]@<40-hex>` for third-party actions/reusable workflows (for example,
`octo/repo/.github/workflows/reusable.yml@<40-hex>`), `./...` for local references,
or `docker://...@sha256:<64-hex>` for containers. Tags, branches, short SHAs and
missing values are findings. Text and JSON use the same findings and pin policy.
An actual scan is offline, read-only and non-preparing: no network, `gh`, package
installation or file writes. Running it is its dry-run.

## Prepare once; scan offline

Parsing requires **PyYAML 6.0.2**, declared in
[the dedicated requirement](../.github/scripts/action-pins-requirements.txt).
Use a dedicated isolated environment outside the checkout, not global/user-site
installation or the unrelated code-navigation environment. Preparation may fetch
the pinned package from the existing package source; scans never do.

```sh
AP_ENV="$(mktemp -d /tmp/action-pins-python.XXXXXX)"
python3 -m venv --without-pip "$AP_ENV/venv"
python3 -m pip --python "$AP_ENV/venv/bin/python3" install \
  --disable-pip-version-check --no-deps -r .github/scripts/action-pins-requirements.txt
AP_PY="$AP_ENV/venv/bin/python3"
"$AP_PY" -B .github/scripts/action_pins.py --json [PATH ...]
```

This uses an existing pip launcher to prepare the venv even on systems without
`ensurepip`; it does not install pip globally. Keep the prepared interpreter
usable by the user running the check. Permission-denial tests retain their real
unprivileged execution boundary; do not grant access to unrelated directories or
weaken those fixtures. The CI preparation step creates the same isolated parser
environment in runner temporary storage and uses its interpreter for all lint
tests and the real pin gate, even when subprocesses scrub `PYTHONPATH`.

Missing/wrong-version parser dependencies produce `input-parser-unavailable`,
never a traceback, automatic installation or legacy-parser fallback. Stdlib help,
discovery and file prechecks still work without the parser: missing paths,
invalid UTF-8, special files and empty discovery retain their existing outcomes.

Omit paths to discover `.github/workflows/*.yml|*.yaml` plus
`.github/actions/**/action.yml|action.yaml`. Omit `--json` for the existing Actions
annotation/prose output. `--json` may appear before or after paths; `--help`
describes the interface. Identical path spellings count once, not once per argv
occurrence; distinct spellings are not filesystem-identity deduplication.

## Safe parsing and invocation positions

The checker parses every YAML document using explicit `SafeLoader` event parsing
and representation-node composition. It never constructs Python/application
objects, evaluates expressions or resolves network references. Mapping pairs are
not collapsed into a dictionary, so duplicate applicable occurrences survive.
Findings use the invocation key's 1-based source line.

Recognized positions are:

- Workflow `jobs.<job>.uses` and mappings in `jobs.<job>.steps[*]`.
- Action mappings in `runs.steps[*]`, including extraction fixtures without
  `runs.using` (not certification that an action is runnable).
- Root fragments: direct `uses`, root `steps` list or direct step mapping, and a
  bare root step list. A root `branches` field does not hide a direct `uses`.

When workflow `jobs` or action `runs` structure is present, only those positions
apply, rather than falling back to arbitrary fragment recursion. `env`, `with`,
metadata and scalar bodies are data, not invocation subtrees. Multiline strings,
embedded `#` in plain scalars, shorthand/colonless flow entries, quoted/explicit
keys, BOM/document markers and directly anchored literal nodes are parsed as YAML,
not searched as raw lines. Empty/missing `uses` remains an unpinned finding.

Aliases, merge keys, nonstandard tags and complex mapping keys refuse the whole
file. Collection/non-string invocation values and unsupported invocation-bearing
container shapes likewise refuse rather than silently skipping declarations.
Standard scalar tags in ordinary data are allowed without constructing objects.
This is not general GitHub workflow/schema validation.

Per-file inclusive limits are **4,194,304 source bytes**, **128 simultaneous YAML
collections** and **100,000 parser events**. Reads are bounded even for a growing
file; event limits and alias rejection precede composition. No recursion-limit
increase is used. Directory discovery is iterative, reports enumeration errors
and avoids ancestor symlink cycles. OS termination or an unusable output stream
cannot be promised a recoverable report.

## JSON contract: `action-pins.v1`

A `--json` check prints exactly one deterministic UTF-8 JSON object to stdout,
with empty stderr for controlled check outcomes:

- `schema`: exactly `"action-pins.v1"`.
- `ok`: true only when complete with no findings/errors.
- `scan_complete`: discovery succeeded, at least one file was requested, and
  every requested file was fully read, parsed and inspected. Complete scans can
  still have pin findings (`ok=false`). No refused file counts as checked.
- `requested`, `checked`, `refused`: unique explicit/discovered candidate path
  counts, with `requested == checked + refused`. Directory enumeration failures
  are additional errors, not phantom refused files or counts of hidden children;
  `len(errors)` is not the refused-file count.
- `findings`: sorted by `(path, line, code, value)`, each with `code`
  (`"unpinned-action"`), `path`, 1-based `line`, exact parsed string `value` (empty
  for missing value), `message`, `retryable=false`, and
  `next_action="pin_to_full_sha_or_digest"`.
- `errors`: sorted by `(path or "", code)`, each with `code`, `path` (null only
  for `no-inputs-discovered`), `message`, `retryable=false` and `next_action`:

  | Error code | Next action |
  | --- | --- |
  | `input-missing` | `fix_input_path` |
  | `input-unreadable` | `fix_input_permissions` |
  | `input-invalid-encoding` | `fix_input_encoding` |
  | `no-inputs-discovered` | `provide_input_files` |
  | `input-parser-unavailable` | `prepare_parser_dependency` |
  | `input-invalid-yaml` | `fix_input_yaml` |
  | `input-unsupported-yaml` | `simplify_input_yaml` |
  | `input-resource-limit` | `reduce_input_complexity` |

  Missing optional roots are normal. Successfully empty discovery produces
  `no-inputs-discovered` and zero counts. Failed enumeration with zero files
  reports its actual directory error, not successful-empty discovery. Any error
  forces incomplete/failed/exit 1; findings from other fully checked files
  survive. A refused file does not contribute partially extracted findings.
- Top-level `retryable=false`; `next_action` is `none` when clean, otherwise the
  first sorted error's action, otherwise the first sorted finding's action.

For example, unreadable workflows plus one readable pinned local action yields
requested=1, checked=1, refused=0, a directory `input-unreadable` error and an
incomplete/failed verdict. Parser absence with one readable valid input instead
yields requested=1, checked=0, refused=1 and `input-parser-unavailable`.

Both normal output modes emit UTF-8 even under inherited
`PYTHONIOENCODING=utf-16`. Valid UTF-8 filename bytes retain their intended Unicode
identity even with an ASCII filesystem locale. Genuinely invalid UTF-8 filename
bytes remain reversible surrogate escapes in JSON and backslash surrogate
escapes in text, never replacement names or filesystem renaming. Text annotation
file properties escape `%`, CR, LF, comma and colon once, separately from message
escaping (`%`, CR, LF). Explicit path identity is otherwise preserved; do not
normalize paths in the consumer to hide producer errors.

## Exit and usage boundaries

- **0:** complete, clean check.
- **1:** pin findings, refused input/parser conditions or failed/empty discovery.
  Read `ok`/`scan_complete`: exit 1 alone does not prove a pin violation.
- **2:** usage error, plain-text usage on stderr, empty stdout and no JSON object.

`--help`/`-h` always print plain text and exit 0, even with `--json` and without
PyYAML. Parse stdout as JSON only for actual JSON check runs, never help/usage.

## Offline verification

After the explicit preparation above:

```sh
"$AP_PY" -B -m unittest discover -s .github/scripts -p 'test_action_pins.py'
"$AP_PY" -B -m unittest discover -s .github/scripts -p 'test_action_pins_json_acceptance.py'
"$AP_PY" -B -m unittest discover -s .github/scripts -p 'test_action_pins_node_acceptance.py'
```

These real CLI tests cover pin/context rules, partial findings and refusals,
parser absence/version, resource boundaries, traversal denials/deep directories,
no-writer FIFOs, multiline/flow scalar controls, duplicate/multidocument inputs,
ASCII filename identity, UTF-8 transport, both annotation sites, help/usage,
determinism and no-write behavior. Acceptance modules are independently
maintained. All three suites run explicitly in `pr-lint`; this document does not
claim `verify-fast` runs them. Fixture verification is not staging, deployment,
installation or downstream adoption evidence. Undo source changes with a
reviewed revert, never weakened guards.
