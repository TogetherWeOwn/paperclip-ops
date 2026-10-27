# Required-check audit outcomes

`ci_required_checks_audit.py` is a read-only checker: does every required
status check have a workflow job that can produce it on a pull request, and
can that job actually fail? It reads a pinned manifest (`--manifest`), a
recorded branch-rules document (`--rules-json`), or a live repository
(`--repo`, read-only `gh api`). It never writes to GitHub. The offline
`--manifest`/`--rules-json` paths never invoke `gh`, the network, or writes.

```sh
python3 ci_required_checks_audit.py --manifest required_checks.json --workflows .github/workflows --json
python3 ci_required_checks_audit.py --rules-json rules.json --workflows .github/workflows --branch main --json
```

Omit `--json` for the text report. Helper signatures (`load_manifest`,
`required_from_rules`, `load_workflows`, `audit`) are unchanged.

## Measured JSON (unchanged)

With `--json`, a measured audit prints exactly one object with the existing
top-level fields: `branch`, `required` (each `context`/`integration_id`),
`counts` (`FAIL`/`WARN`/`INFO`) and `findings` (each `severity`, `code`,
`context`, `detail`). An unparseable workflow — including an entry that cannot
be opened or decoded — is still a measured finding (`UNPARSEABLE`), not an
input failure. Text output and finding semantics are unchanged.

## Unmeasurable JSON: schema version 1

With `--json`, a caught offline input failure prints exactly one JSON object
to stdout, nothing to stderr, and exits 2:

- `schema_version`: `1` (integer).
- `outcome`: `"unmeasurable"`; `measurement_state`: `"unmeasured"`.
- `error_code`: one stable code from the table below.
- `retryable`: `false` — repair the local input; do not silently retry.
- `next_action`: an actionable repair instruction naming what to fix.
- `branch`: the known branch, or `null` when unknown (see below).
- `source`: `{"kind": "manifest" | "rules-json", "path": <supplied path>}`.
- `counts`: `null` and `findings`: `null` — an unmeasured run is never a clean
  audit and carries no fabricated counts or finding evidence.

| `error_code` | Cause |
| --- | --- |
| `MANIFEST_UNREADABLE` | manifest path missing, a directory, or not readable |
| `MANIFEST_INVALID` | manifest is malformed/non-UTF-8 JSON, not an object, `required` is missing/not a list of `{context, integration_id}` with string contexts and integer/null/absent `integration_id`, or `branch` is not a string |
| `RULES_UNREADABLE` | rules path missing, a directory, or not readable |
| `RULES_INVALID` | rules document is malformed/non-UTF-8 JSON, not a list of branch rules, or a nested rule (missing/non-string `type`, null/non-object `parameters`, missing/null/non-list checks) or check entry (non-integer non-null `integration_id`) is the wrong shape |
| `WORKFLOWS_REQUIRED` | `--workflows` omitted with `--manifest`/`--rules-json` |
| `NO_REQUIRED_CHECKS` | the input read cleanly but names no required status checks |
| `NO_WORKFLOWS` | the workflow directory cannot be read, or contains no workflow files and no parse errors |

`next_action` values name the repair, for example "Provide a readable
manifest file at the supplied manifest path and rerun the audit",
"Pass --workflows with a directory of workflow files and rerun the audit",
or "Provide at least one required status check in the input and rerun the
audit". Repeating the same invocation returns the identical object.

`branch` is what the run actually knows: an explicit `--branch` always wins.
A manifest's own `branch` survives later input failures (including a manifest
with a valid `branch` but an invalid `required` list). A manifest that cannot
be read as JSON contributes no branch, so `branch` is `null` unless `--branch`
was supplied — unreadable metadata is not evidence of a branch. Rules carry
no branch, so rules input failures report `null` before the source loads;
once rules load, `NO_REQUIRED_CHECKS`/`NO_WORKFLOWS` report the existing
default `"main"` when no `--branch` was given.

Text mode is unchanged on every path above: stdout stays empty, the
`ci_required_checks_audit: …` diagnostic stays on stderr, and the exit is 2.
`--strict` does not turn an unmeasured result into exit 1.

## Exit and usage boundaries

- **0:** measured, no FAIL (and no WARN under `--strict`).
- **1:** measured findings — at least one FAIL, or a WARN under `--strict`.
- **2:** nothing could be measured (bad input), or an argument syntax error.

Argument syntax errors deliberately remain outside the JSON envelope:
unknown flags, a missing option value, a missing source, conflicting
sources, and a bare invocation (or only `--repo`/`--json`) print argparse
usage to stderr with empty stdout and exit 2 — even with `--json`. Parse
stdout as JSON only for an actual `--json` audit, not a help or usage
response. Live `--repo` failures keep their existing prose diagnostic and
exit 2; there is no live-network error envelope in this contract.

## Offline verification

```sh
python3 -B -m unittest -v test_ci_required_checks_audit test_required_checks_unmeasurable_json_acceptance
```

The first suite pins the measured audit, finding semantics, text behaviour
and exits. The second pins this unmeasurable envelope — single-object
parsing, stable codes, branch handling, no fabricated counts, argparse
boundary, and zero `gh`/network/write calls behind a poisoned `PATH` and an
audit-hook guard. Both run in the local fast tier and in CI. This document
describes source behaviour, not an installation or deployment claim.
