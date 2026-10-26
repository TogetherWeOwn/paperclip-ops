# Workflow job permissions check

Consumer: **agent or operator running the local gate**. Canonical home for
the `check_workflow_job_permissions.py` presence contract; the script
docstring states the policy rationale.

Every job in `.github/workflows/` must declare its own job-level
`permissions:` mapping (`{}` where the job needs no token scopes). The check
is presence-only: a pass means each job made an explicit declaration; it
does not say the declared scopes are minimal. It fails closed (exit 2) on
anything it cannot parse or decide and is read-only: it reads local files
only, never writes, downloads, or runs other commands.

## Usage

```sh
python3 check_workflow_job_permissions.py [workflow-file ...]   # prose
python3 check_workflow_job_permissions.py --json [workflow-file ...]
python3 check_workflow_job_permissions.py --help
```

With no files, `.github/workflows/*.yml` and `*.yaml` (direct children) are
checked. Exit codes: 0 every job declares a block; 1 a job is missing one;
2 a file or the parser could not be read (inconclusive, never success).

## Structured output

`--json` prints exactly one versioned object on stdout (nothing on stderr):

```json
{"checked_files": 12, "errors": [], "findings": [], "measurement": "complete", "schema_version": 1, "unmeasurable_files": 0, "verdict": "pass"}
```

A missing declaration:

```json
{"checked_files": 1, "errors": [], "findings": [{"code": "missing_job_permissions", "file": "ci.yml", "job": "build", "line": 2, "next_action": "Add a job-level `permissions:` mapping to the job (declare `{}` where it needs no token scopes, else exactly the permissions scopes it needs).", "retryable": false, "rule_id": "workflow.job_permissions.required"}], "measurement": "complete", "schema_version": 1, "unmeasurable_files": 0, "verdict": "fail"}
```

Mixed readable and unreadable input stays inconclusive (exit 2) while
positively observed findings are retained:

```json
{"checked_files": 1, "errors": [{"code": "unreadable_workflow", "file": "gone.yml", "next_action": "Restore read access to the workflow file or check the path, then rerun the check.", "retryable": true}], "findings": [{"code": "missing_job_permissions", "file": "ci.yml", "job": "build", "line": 2, "next_action": "Add a job-level `permissions:` mapping to the job (declare `{}` where it needs no token scopes, else exactly the permissions scopes it needs).", "retryable": false, "rule_id": "workflow.job_permissions.required"}], "measurement": "inconclusive", "schema_version": 1, "unmeasurable_files": 1, "verdict": "inconclusive"}
```

Contract fields:

- `schema_version`: integer `1`.
- `checked_files` / `unmeasurable_files`: files fully checked vs. files
  that could not be measured; `measurement` is `complete` only when nothing
  is unmeasurable.
- `verdict`: `pass` (exit 0), `fail` (exit 1), or `inconclusive` (exit 2).
  Exit 2 always outranks exit 1.
- `findings`: `{rule_id, code, file, job, line, retryable, next_action}`.
  The only rule is `workflow.job_permissions.required` with code
  `missing_job_permissions`; `line` is the 1-based job-key line. Findings
  sort by `(file, line, job, rule_id)`.
- `errors`: `{code, file, retryable, next_action}`, sorted by
  `(file or "", code)`. Codes: `invalid_workflow`, `unreadable_workflow`,
  `yaml_parser_unavailable`, `no_workflow_inputs`, `invalid_arguments`.
  Global errors carry `file: null`.

Paths are reported exactly as passed (spaces and Unicode survive); codes
and ordering never depend on parser display text.

## Recovery

After fixing the named file/job or the reported error, rerun the exact
command that failed, for example:

```sh
python3 check_workflow_job_permissions.py --json
```

A `yaml_parser_unavailable` result means the pinned PyYAML requirement is
not installed in that environment; install the repository's pinned
requirements with the normal verification setup, then rerun the same
command. Never treat exit 2 as pass, and never edit workflows with this
tool: it only reports.
