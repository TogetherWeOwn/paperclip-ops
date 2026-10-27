# CI policy verdict records

`.github/scripts/ci_policy.py` lints PR-triggered workflows for change gating and
`ci-ok` completeness. By default it prints annotations, `note:` lines and a
closing `CI policy OK` line, with exit 0 or 1. The opt-in `--json` flag prints one
`ci-policy-verdicts.v1` object instead, so an agent can act on a stable `code`
rather than parse prose. Detection, markers, modes and thresholds are unchanged,
and the default output is byte-identical to before.

## Run

```sh
# Offline, read-only: reads the named workflow files, writes nothing, no network.
CI_POLICY_MODE=error python3 .github/scripts/ci_policy.py --json
python3 .github/scripts/ci_policy.py --json path/to/workflow.yml
python3 .github/scripts/ci_policy.py --help

# Acceptance suite (frozen contract) and the existing unit tests:
python3 -B -m unittest discover -s .github/scripts -p 'test_ci_policy_json_acceptance.py'
python3 -m unittest discover -s .github/scripts -p 'test_ci_policy.py'
```

`--json` may appear before or after the paths. `CI_POLICY_MODE` (`warn` or
`error`, default `warn`) and `HEAVY_TIMEOUT_MINUTES` work as before; there is no
`--mode` flag. With no paths the lint discovers `.github/workflows/*.yml`.
`--help` prints plain text and exits 0. An unknown option is a usage error: exit
2, empty stdout, usage on stderr.

## Contract, version 1

Stdout is exactly one JSON object. Paths are deduplicated and sorted, so the
report is identical across repeated runs and argument orders. Additive top-level
fields may appear in a future minor revision; the fields below are frozen.

| Field | Meaning |
|---|---|
| `schema` | `"ci-policy-verdicts.v1"` |
| `mode` | effective mode, `"warn"` or `"error"` |
| `outcome` | `unmeasurable` when any requested input could not be measured or none was requested; else `findings` when any exists; else `clean` |
| `totals` | `requested == linted + skipped + unmeasurable`; `findings == len(findings)` |
| `findings` | one record per violation, sorted by `(path, job, rule, code)` |
| `unmeasurable` | inputs that could not be measured, sorted by `(path, code)` |
| `coverage.limits` | `{code, description, paths}` entries, disclosed rather than fixed |

`skipped` counts out-of-scope workflows: no `pull_request` or
`pull_request_target` trigger, or a `ci-policy: non-required` marker (the trigger
check wins over the marker).

### Finding record

Exactly `rule`, `path`, `job`, `line`, `code`, `retryable` (always `false`) and
`next_action`. `path` is the argument as given; `job` is the job name (null for
the workflow-level finding); `line` is the 1-based job header line (null for the
workflow-level finding). No message, job body, step text, `env`/secret expression
or marker reason is ever emitted.

| `rule` | `code` | `next_action` |
|---|---|---|
| `change-gating` | `heavy-job-no-timeout` | `gate_job_on_change_detection` |
| `change-gating` | `heavy-job-ungated` | `gate_job_on_change_detection` |
| `workflow-paths` | `workflow-level-paths` | `move_paths_filter_to_job_level` |
| `ci-ok-coverage` | `ci-ok-needs-missing` | `add_job_to_ci_ok_needs` |
| `ci-ok-coverage` | `ci-ok-exempt-no-reason` | `add_exempt_reason` |

### Unmeasurable record

Exactly `path`, `code`, `retryable` (always `false`) and `next_action`. A partial
failure keeps the findings of the inputs that were measured, and an unmeasurable
input is never reported as clean.

| `code` | Cause | `next_action` |
|---|---|---|
| `input-missing` | the file cannot be read | `fix_input_path` |
| `input-invalid-encoding` | the file is not valid UTF-8 | `fix_input_encoding` |
| `workflow-unparseable` | no top-level `on:` key, so the scope cannot be decided | `fix_workflow_syntax` |
| `no-inputs-discovered` | default discovery found no workflow (path null, not counted in `totals.unmeasurable`) | `provide_workflow_files` |

### Coverage limits

`ci-ok-always-structural` is always present with empty `paths`: a job listed
directly in `ci-ok` `needs:` that runs under `always()` counts as covered even
though its failure would not propagate. The detection is not changed; the gap is
disclosed. `skipped-no-pull-request-trigger`, `skipped-non-required-marker` and
`ci-ok-rule-skipped-no-ci-ok-job` appear only when at least one path falls in that
class, listing exactly those paths.

## Exit codes with `--json`

- `2`: outcome `unmeasurable`, in both modes.
- `1`: at least one finding in `error` mode.
- `0`: otherwise (including findings in `warn` mode).

Without `--json` the exit codes are unchanged (0, or 1 for a finding in error
mode). A missing or non-UTF-8 input still fails the prose run with a traceback, as
before. Neither mode changes which check is required or the configured mode.
