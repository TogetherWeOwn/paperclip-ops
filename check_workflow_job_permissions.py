#!/usr/bin/env python3
"""Require an explicit job-level `permissions:` block on every workflow job.

A job-level block COMPLETELY REPLACES the top-level block for that job; it
does not merge. A job without its own block silently inherits whatever scopes
the workflow floor carries, so a floor change can widen a job nobody reviewed
and per-job declaration review cannot see what each job claims. This check
requires every job in `.github/workflows/` to declare its own block: `{}`
where the job uses no token scopes at all (no checkout, no API, no artifact
actions), otherwise exactly the scopes it needs.

Presence only: this check does not judge whether a declared set is minimal.
It fails closed (exit 2) on anything it cannot parse or decide: unreadable
files, malformed YAML, multiple documents, duplicate keys on the checked
paths, non-mapping job bodies, or a missing YAML parser. A present but
non-mapping `permissions:` value (null, list, scalar) is a missing
declaration (exit 1), not a parse error.

Usage: check_workflow_job_permissions.py [--json] [workflow-file ...]
  (default: .github/workflows/*.yml and *.yaml, direct children only)

Exit codes: 0 every job has a block; 1 a job is missing one (names the job);
  2 fail-closed (unreadable file, invalid workflow, or no parser).
  Tests pin exit codes, never message text.

`--json` prints one versioned object on stdout instead of prose; see the
"Structured output" section of `docs/workflow-permissions.md` for the v1
contract (rule id, error codes, deterministic ordering).
"""
import glob
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # Fail closed without a traceback; see main().
    yaml = None

RULE_ID = "workflow.job_permissions.required"
MISSING_CODE = "missing_job_permissions"
SCHEMA_VERSION = 1

USAGE = """\
usage: check_workflow_job_permissions.py [--json] [workflow-file ...]

Require an explicit job-level `permissions:` presence declaration on every
workflow job (declare `{}` where a job needs no token scopes). Presence
only: a pass does not say the declared scopes are minimal. The check is
read-only (a dry run): it reads local files and never writes, fetches, or
runs other commands.

options:
  --json     print one versioned JSON verdict object instead of prose
  -h, --help show this help and exit

With no files, .github/workflows/*.yml and *.yaml are checked.
Exit codes: 0 every job declares a block; 1 a job is missing one;
2 a file or the parser could not be read (inconclusive).
"""

# Machine outcomes for the structured error envelope: code, whether retrying
# the same command could succeed, and the corrective action. Display text
# from the parser never decides the code; check_file classifies by cause.
ERROR_SPECS = {
    "invalid_workflow": (
        False,
        "Fix the workflow YAML so it parses as one mapping with a jobs block, then rerun the check.",
    ),
    "unreadable_workflow": (
        True,
        "Restore read access to the workflow file or check the path, then rerun the check.",
    ),
    "yaml_parser_unavailable": (
        True,
        "Install the pinned PyYAML requirements so the YAML parser is available, then rerun the check.",
    ),
    "no_workflow_inputs": (
        False,
        "Provide workflow file paths or run where .github/workflows holds .yml/.yaml files, then rerun the check.",
    ),
    "invalid_arguments": (
        False,
        "Run with --help to check the supported flags; remove unsupported flags and rerun the check.",
    ),
}

MISSING_NEXT_ACTION = (
    "Add a job-level `permissions:` mapping to the job (declare `{}` where "
    "it needs no token scopes, else exactly the permissions scopes it needs)."
)


def _scalar_key(node):
    """Decoded key text for scalar mapping keys, else None."""
    if yaml is not None and isinstance(node, yaml.ScalarNode):
        return node.value
    return None


def _duplicate_key(pairs):
    """First repeated scalar key in mapping pairs, else None."""
    seen = set()
    for key, _ in pairs:
        name = _scalar_key(key)
        if name is None:
            continue
        if name in seen:
            return name
        seen.add(name)
    return None


def _job_findings(document, path):
    """(findings, error) for a composed workflow document.

    findings are (1-based job-key lineno, job name) for jobs whose own
    `permissions:` declaration is absent or not a mapping. error is "" on
    success; any invalid shape returns ([], message).
    """
    if not isinstance(document, yaml.MappingNode):
        return [], f"{path}: workflow root is not a mapping"
    dup = _duplicate_key(document.value)
    if dup is not None:
        return [], f"{path}: duplicate top-level key {dup!r}"
    jobs_node = None
    for key, value in document.value:
        if _scalar_key(key) == "jobs":
            jobs_node = value
            break
    if jobs_node is None:
        return [], f"{path}: no `jobs:` block found"
    if not isinstance(jobs_node, yaml.MappingNode) or not jobs_node.value:
        return [], f"{path}: `jobs:` block holds no jobs"
    dup = _duplicate_key(jobs_node.value)
    if dup is not None:
        return [], f"{path}: duplicate job {dup!r}"
    findings = []
    for key, body in jobs_node.value:
        name = _scalar_key(key)
        lineno = key.start_mark.line + 1
        if name is None:
            return [], f"{path}:{lineno}: job key is not a scalar"
        if not isinstance(body, yaml.MappingNode):
            return [], f"{path}:{lineno}: job `{name}` body is not a mapping"
        declarations = [(k, v) for k, v in body.value
                        if _scalar_key(k) == "permissions"]
        if len(declarations) > 1:
            return [], (f"{path}:{lineno}: job `{name}` declares "
                        f"`permissions:` more than once")
        if not declarations:
            findings.append((lineno, name))
            continue
        _, value = declarations[0]
        # The composer resolves `*alias` to its anchored node, so an alias
        # to a mapping is already a MappingNode here; an undefined alias
        # raised before we got this far. Anything else (null, sequence,
        # scalar) is a missing declaration, not a parse error.
        if not isinstance(value, yaml.MappingNode):
            findings.append((lineno, name))
            continue
        dup = _duplicate_key(value.value)
        if dup is not None:
            return [], (f"{path}:{lineno}: job `{name}` repeats "
                        f"permission scope {dup!r}")
    if not findings and not jobs_node.value:
        return [], f"{path}: `jobs:` block holds no jobs"
    return findings, ""


def _check_file_detailed(path):
    """(findings, error, code): code classifies the error by cause.

    findings are (lineno, job) for jobs missing a block; error is the prose
    message ("" on success) and code is the stable machine code ("" when
    there is no error). The structured envelope never parses message text.
    """
    if yaml is None:
        return [], (f"{path}: YAML parser unavailable (PyYAML not installed)"), "yaml_parser_unavailable"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [], f"{path}: unreadable ({exc})", "unreadable_workflow"
    except OSError as exc:
        return [], f"{path}: unreadable ({exc.strerror or exc})", "unreadable_workflow"
    try:
        # get_single_node fails closed on multiple documents; empty and
        # comment-only streams compose to None.
        document = yaml.compose(text)
    except yaml.YAMLError as exc:
        return [], f"{path}: invalid YAML ({exc})", "invalid_workflow"
    except Exception as exc:  # Fail closed, never traceback on input.
        return [], f"{path}: invalid YAML ({exc})", "invalid_workflow"
    if document is None:
        return [], f"{path}: workflow is empty", "invalid_workflow"
    try:
        findings, error = _job_findings(document, path)
    except Exception as exc:  # Fail closed, never traceback on input.
        return [], f"{path}: invalid workflow ({exc})", "invalid_workflow"
    return findings, error, "invalid_workflow" if error else ""


def check_file(path):
    """(findings, error): findings are (lineno, job) missing a block."""
    findings, error, _code = _check_file_detailed(path)
    return findings, error


def default_files():
    """Fresh cwd-based discovery on every call; never cached."""
    directory = os.path.join(".github", "workflows")
    return sorted(glob.glob(os.path.join(directory, "*.yml"))
                  + glob.glob(os.path.join(directory, "*.yaml")))


def _structured_error(code, file):
    retryable, next_action = ERROR_SPECS[code]
    return {"code": code, "file": file, "retryable": retryable,
            "next_action": next_action}


def _report(verdict, checked, unmeasurable, findings, errors):
    if verdict == "inconclusive":
        exit_code, measurement = 2, "inconclusive"
    elif verdict == "fail":
        exit_code, measurement = 1, "complete"
    else:
        exit_code, measurement = 0, "complete"
    return exit_code, {
        "schema_version": SCHEMA_VERSION,
        "checked_files": checked,
        "unmeasurable_files": unmeasurable,
        "measurement": measurement,
        "verdict": verdict,
        "findings": sorted(findings,
                           key=lambda f: (f["file"], f["line"], f["job"], f["rule_id"])),
        "errors": sorted(errors, key=lambda e: (e["file"] or "", e["code"])),
    }


def _parse_args(args):
    """(paths, json_mode, unsupported, help_requested): split flags from paths.

    `--json` may appear anywhere; any other `-`-prefixed argument is a
    rejected flag, never a path. `--` ends flag parsing, so a dash-named
    file after it (including one named `--help`) is a path.
    """
    paths, json_mode, unsupported, help_requested = [], False, None, False
    flags_open = True
    for arg in args:
        text = os.fspath(arg)
        if flags_open and text == "--":
            flags_open = False
        elif flags_open and text in ("-h", "--help"):
            help_requested = True
        elif flags_open and text == "--json":
            json_mode = True
        elif flags_open and text.startswith("-") and text != "-":
            unsupported = unsupported or text
        else:
            paths.append(text)
    return paths, json_mode, unsupported, help_requested


def main(argv=None):
    args = list(argv[1:] if argv is not None else sys.argv[1:])
    paths, json_mode, unsupported, help_requested = _parse_args(args)
    if help_requested:
        print(USAGE, end="")
        return 0

    def emit(exit_code, report):
        if json_mode:
            print(json.dumps(report, sort_keys=True))
            return exit_code
        for line in prose_lines:
            print(line)
        return exit_code

    prose_lines = []
    if unsupported is not None:
        prose_lines = [
            f"workflow job permissions: unsupported option {unsupported} (see --help)",
        ]
        return emit(*_report("inconclusive", 0, 0, [],
                             [_structured_error("invalid_arguments", None)]))
    files = paths or default_files()
    if not files:
        prose_lines = ["workflow job permissions: no workflow files found"]
        return emit(*_report("inconclusive", 0, 0, [],
                             [_structured_error("no_workflow_inputs", None)]))
    if yaml is None:
        prose_lines = [
            f"{path}: error: {path}: YAML parser unavailable (PyYAML not installed)"
            for path in files
        ] + ["workflow job permissions: INCONCLUSIVE (a file could not be read)"]
        return emit(*_report("inconclusive", 0, len(files), [],
                             [_structured_error("yaml_parser_unavailable", None)]))
    findings, errors = [], []
    total, bad, broken = 0, 0, False
    for path in files:
        found, error, code = _check_file_detailed(path)
        if error:
            prose_lines.append(f"{path}: error: {error}")
            errors.append(_structured_error(code, path))
            broken = True
            continue
        total += 1
        for lineno, name in found:
            prose_lines.append(f"{path}:{lineno}: job `{name}` has no job-level "
                               f"`permissions:` block (declare `{{}}` where the job "
                               f"needs no token scopes, else exactly the scopes it needs)")
            findings.append({"rule_id": RULE_ID, "code": MISSING_CODE,
                             "file": path, "job": name, "line": lineno,
                             "retryable": False,
                             "next_action": MISSING_NEXT_ACTION})
            bad += 1
    if broken:
        prose_lines.append("workflow job permissions: INCONCLUSIVE (a file could not be read)")
        return emit(*_report("inconclusive", total, len(errors), findings, errors))
    if bad:
        prose_lines.append(f"workflow job permissions: FAIL ({bad} job(s) without a block)")
        return emit(*_report("fail", total, 0, findings, errors))
    prose_lines.append(f"workflow job permissions: OK ({total} file(s))")
    return emit(*_report("pass", total, 0, findings, errors))


if __name__ == "__main__":
    sys.exit(main())
