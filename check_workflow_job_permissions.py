#!/usr/bin/env python3
"""Require an explicit job-level `permissions:` block on every workflow job.

A job-level block COMPLETELY REPLACES the top-level block for that job; it
does not merge. A job without its own block silently inherits whatever scopes
the workflow floor carries, so a floor change can widen a job nobody reviewed
and least-privilege review cannot see what each job claims. This check
requires every job in `.github/workflows/` to declare its own block: `{}`
where the job uses no token scopes at all (no checkout, no API, no artifact
actions), otherwise exactly the scopes it needs.

Presence only: this check does not judge whether a declared set is minimal.
It fails closed (exit 2) on anything it cannot parse or decide: unreadable
files, malformed YAML, multiple documents, duplicate keys on the checked
paths, non-mapping job bodies, or a missing YAML parser. A present but
non-mapping `permissions:` value (null, list, scalar) is a missing
declaration (exit 1), not a parse error.

Usage: check_workflow_job_permissions.py [workflow-file ...]
  (default: .github/workflows/*.yml and *.yaml, direct children only)

Exit codes: 0 every job has a block; 1 a job is missing one (names the job);
  2 fail-closed (unreadable file, invalid workflow, or no parser).
  Tests pin exit codes, never message text.
"""
import glob
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # Fail closed without a traceback; see main().
    yaml = None


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


def check_file(path):
    """(findings, error): findings are (lineno, job) missing a block."""
    if yaml is None:
        return [], f"{path}: YAML parser unavailable (PyYAML not installed)"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [], f"{path}: unreadable ({exc})"
    except OSError as exc:
        return [], f"{path}: unreadable ({exc.strerror or exc})"
    try:
        # get_single_node fails closed on multiple documents; empty and
        # comment-only streams compose to None.
        document = yaml.compose(text)
    except yaml.YAMLError as exc:
        return [], f"{path}: invalid YAML ({exc})"
    except Exception as exc:  # Fail closed, never traceback on input.
        return [], f"{path}: invalid YAML ({exc})"
    if document is None:
        return [], f"{path}: workflow is empty"
    try:
        return _job_findings(document, path)
    except Exception as exc:  # Fail closed, never traceback on input.
        return [], f"{path}: invalid workflow ({exc})"


def default_files():
    """Fresh cwd-based discovery on every call; never cached."""
    directory = os.path.join(".github", "workflows")
    return sorted(glob.glob(os.path.join(directory, "*.yml"))
                  + glob.glob(os.path.join(directory, "*.yaml")))


def main(argv=None):
    args = list(argv[1:] if argv is not None else sys.argv[1:])
    files = args or default_files()
    if not files:
        print("workflow job permissions: no workflow files found")
        return 2
    total, bad, broken = 0, 0, False
    for path in files:
        findings, error = check_file(path)
        if error:
            print(f"{path}: error: {error}")
            broken = True
            continue
        total += 1
        for lineno, name in findings:
            print(f"{path}:{lineno}: job `{name}` has no job-level "
                  f"`permissions:` block (declare `{{}}` where the job "
                  f"needs no token scopes, else exactly the scopes it needs)")
            bad += 1
    if broken:
        print("workflow job permissions: INCONCLUSIVE (a file could not be read)")
        return 2
    if bad:
        print(f"workflow job permissions: FAIL ({bad} job(s) without a block)")
        return 1
    print(f"workflow job permissions: OK ({total} file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
