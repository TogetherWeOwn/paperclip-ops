#!/usr/bin/env python3
"""Require an explicit permission mapping on every GitHub Actions job.

Offline; requires PyYAML (also used by the runner-label guard). Parse YAML
nodes, not indentation, so quoted keys, aliases and flow mappings work and
step/run-string keys cannot stand in for a job declaration. BaseLoader keeps
YAML 1.1 boolean-looking job names such as `on` as strings. No objects are
constructed or aliases expanded. Relevant duplicate/merge keys fail closed.

A job-level declaration replaces, rather than augments, workflow permissions.
`{}` grants nothing; null, absent and scalar declarations are not mappings.
This gate checks explicit declarations, not whether scopes are minimal.

Usage: check_workflow_job_permissions.py [workflow-file ...]
Default: every .github/workflows/*.yml and *.yaml relative to the working dir.
Exit 0: compliant; 1: missing/invalid declaration; 2: cannot validate input.
"""
from pathlib import Path
import sys

try:
    import yaml
except ImportError:
    yaml = None


def mapping(node, context):
    """Return key -> (key node, value node), refusing ambiguous mappings."""
    if not isinstance(node, yaml.MappingNode):
        raise ValueError(f"{context} must be a mapping")
    result = {}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            raise ValueError(f"{context} has a non-scalar key")
        if key.value == "<<" or key.value in result:
            raise ValueError(f"{context} has a duplicate or merge key")
        result[key.value] = (key, value)
    return result


def check_file(path):
    """Return ((line, job) findings, error); never print workflow content."""
    if yaml is None:
        return [], "PyYAML is required; install the pinned workflow parser"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [], f"cannot read workflow ({type(exc).__name__})"
    try:
        root = mapping(yaml.compose(text, Loader=yaml.BaseLoader), "workflow")
        jobs = mapping(root.get("jobs", (None, None))[1], "jobs")
        if not jobs:
            raise ValueError("jobs mapping is empty")
        findings = []
        for name, (key, config) in jobs.items():
            job = mapping(config, f"job {name!r}")
            permissions = job.get("permissions", (None, None))[1]
            if not isinstance(permissions, yaml.MappingNode):
                findings.append((key.start_mark.line + 1, name))
                continue
            scopes = mapping(permissions, f"job {name!r} permissions")
            if any(not isinstance(value, yaml.ScalarNode)
                   or value.value not in ("read", "write", "none")
                   for _, value in scopes.values()):
                findings.append((key.start_mark.line + 1, name))
        return findings, ""
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        # Parser errors include source snippets; only emit their type/position.
        if isinstance(exc, yaml.YAMLError):
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}" if mark else ""
            return [], f"invalid YAML{where} ({type(exc).__name__})"
        return [], str(exc) if isinstance(exc, ValueError) else "YAML nesting too deep"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--help"]:
        print(__doc__)
        return 0
    if any(arg.startswith("-") for arg in args):
        print("usage: check_workflow_job_permissions.py [workflow-file ...]", file=sys.stderr)
        return 2
    try:
        files = [Path(arg) for arg in args] if args else sorted(
            path for path in Path(".github/workflows").iterdir()
            if path.suffix in (".yml", ".yaml")
        )
    except OSError:
        files = []
    if not files:
        print("workflow job permissions: no workflow files found", file=sys.stderr)
        return 2
    bad, broken = 0, False
    for path in files:
        findings, error = check_file(path)
        if error:
            print(f"{path}: cannot validate: {error}", file=sys.stderr)
            broken = True
        for line, name in findings:
            print(f"{path}:{line}: job {name!r} needs an explicit job-level "
                  "permissions mapping ({} grants nothing; null does not)", file=sys.stderr)
            bad += 1
    if broken:
        return 2
    if bad:
        print(f"workflow job permissions: FAIL ({bad} job(s))", file=sys.stderr)
        return 1
    print(f"workflow job permissions: OK ({len(files)} workflows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
