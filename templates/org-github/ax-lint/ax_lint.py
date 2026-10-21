#!/usr/bin/env python3
"""ax-lint: advisory Agent Experience lint for repositories.

Warns (never fails in ``warn`` mode) about three cheap, deterministic
AX gaps:

* ``agents-map`` (AX6): a missing ``AGENTS.md`` entry map, or one over
  the line budget.
* ``script-surface`` (AX1/AX4): a flag-taking script with neither
  ``--json`` nor ``--dry-run``.
* ``dispatch-inputs`` (AX2): a ``workflow_dispatch`` input with no
  ``type``, or a ``choice`` input with no ``options``.

Read-only: it never modifies the tree, so ``--dry-run`` is N/A.
Machine-readable output is ``--json`` (AX1). Hermetic stdlib only:
no network, no credentials, no third-party imports.
"""

import argparse
import json
import os
import re
import sys

AGENTS_BUDGET_LINES = 120

EXCLUDE_DIRS = frozenset({
    ".git", "node_modules", "vendor", ".venv", "venv", "__pycache__",
    ".tox", "target", "dist", "build", ".mypy_cache", ".pytest_cache",
})

SCRIPT_EXTENSIONS = (".sh", ".bash", ".py")

FLAG_RE = re.compile(r"--[a-zA-Z][a-zA-Z0-9-]*")
SHEBANG_RE = re.compile(r"^#!")

KEY_RE_TEMPLATE = r"^\s*(['\"]?)%s\1\s*:"


def _finding(check, path, message, line=None):
    return {"check": check, "path": path, "line": line, "message": message}


def check_agents_map(root):
    """AGENTS.md exists at the repo root and fits the map budget (AX6)."""
    path = os.path.join(root, "AGENTS.md")
    if not os.path.isfile(path):
        return [_finding("agents-map", "AGENTS.md",
                          "no AGENTS.md entry map at the repo root")]
    with open(path, encoding="utf-8", errors="replace") as fh:
        count = sum(1 for _ in fh)
    if count > AGENTS_BUDGET_LINES:
        return [_finding(
            "agents-map", "AGENTS.md",
            f"AGENTS.md is {count} lines, over the {AGENTS_BUDGET_LINES}-line "
            "map budget; move procedures into docs/skills",
            line=AGENTS_BUDGET_LINES + 1)]
    return []


def _is_script_candidate(root, relpath, fullname):
    if relpath.startswith("tests/") or relpath.startswith("test/"):
        return False
    name = os.path.basename(relpath)
    if name.startswith("test_") or name.startswith("test-"):
        return False
    if not (os.access(fullname, os.X_OK) or relpath.endswith(SCRIPT_EXTENSIONS)):
        return False
    try:
        with open(fullname, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return False
    return bool(SHEBANG_RE.match(first))


def check_script_surface(root):
    """Flag-taking scripts offer --json or --dry-run (AX1/AX4).

    A script "takes flags" when its text mentions any ``--long-flag``.
    Scripts with no long flags are not CLIs and are skipped. Test
    scripts are skipped: they are harnesses, not agent surfaces.
    """
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            fullname = os.path.join(dirpath, name)
            if not os.path.isfile(fullname) or os.path.islink(fullname):
                continue
            relpath = os.path.relpath(fullname, root)
            if not _is_script_candidate(root, relpath, fullname):
                continue
            try:
                with open(fullname, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            flags = set(FLAG_RE.findall(text))
            if not flags:
                continue
            if "--json" not in flags and "--dry-run" not in flags:
                findings.append(_finding(
                    "script-surface", relpath,
                    "takes flags but offers neither --json nor --dry-run"))
    return findings


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _strip_comment(line):
    # Conservative: cut at the first '#' outside single/double quotes.
    out = []
    quote = None
    for ch in line:
        if quote is None and ch in ("'", '"'):
            quote = ch
        elif ch == quote:
            quote = None
        elif ch == "#" and quote is None:
            break
        out.append(ch)
    return "".join(out)


def _key_present(block_lines, key):
    return any(re.match(KEY_RE_TEMPLATE % re.escape(key), _strip_comment(ln))
               for ln in block_lines)


def _key_scalar(block_lines, key):
    for ln in block_lines:
        code = _strip_comment(ln)
        match = re.match(KEY_RE_TEMPLATE % re.escape(key) + r"\s*(.*)$", code)
        if match:
            return match.group(2).strip().strip("'\"")
    return ""


def check_dispatch_inputs(root):
    """workflow_dispatch inputs are typed; choices list options (AX2).

    Indentation scan of the standard mapping shape only. Workflows that
    put ``workflow_dispatch`` inline (``{}``/``[]``), use anchors, or
    otherwise deviate are skipped, not flagged: an advisory lint must
    not fail what it cannot parse.
    """
    findings = []
    workflows = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(workflows):
        return findings
    for name in sorted(os.listdir(workflows)):
        if not name.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(workflows, name)
        relpath = os.path.relpath(path, root)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        findings.extend(_dispatch_findings(relpath, lines))
    return findings


def _dispatch_findings(relpath, lines):
    findings = []
    i, total = 0, len(lines)
    while i < total:
        line = lines[i]
        code = _strip_comment(line).rstrip()
        match = re.match(r"^(\s*)workflow_dispatch\s*:\s*(.*)$", code)
        if not match or match.group(2).strip():
            i += 1
            continue
        base_indent = _indent_of(line)
        i += 1
        # Collect the workflow_dispatch block, then find `inputs:` in it.
        block_start = i
        while i < total and (not lines[i].strip() or _indent_of(lines[i]) > base_indent):
            i += 1
        block = lines[block_start:i]
        inputs_at = [k for k, ln in enumerate(block)
                     if re.match(r"^\s*inputs\s*:\s*(#.*)?$", _strip_comment(ln).rstrip())]
        if not inputs_at:
            continue
        inputs_indent = _indent_of(block[inputs_at[0]])
        rest = block[inputs_at[0] + 1:]
        # Input entries are the shallowest indented lines under `inputs:`.
        child_indents = [_indent_of(ln) for ln in rest
                         if ln.strip() and _indent_of(ln) > inputs_indent]
        if not child_indents:
            continue
        entry_indent = min(child_indents)
        entries = []
        current = None
        for lineno_offset, ln in enumerate(rest):
            if not ln.strip():
                if current is not None:
                    current[2].append(ln)
                continue
            if _indent_of(ln) <= inputs_indent:
                break
            name_match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*:\s*(#.*)?$", _strip_comment(ln).rstrip())
            if _indent_of(ln) == entry_indent and name_match:
                current = (name_match.group(1), inputs_at[0] + 1 + lineno_offset, [ln])
                entries.append(current)
            elif current is not None and _indent_of(ln) > entry_indent:
                current[2].append(ln)
        for entry_name, _, entry_lines in entries:
            if not _key_present(entry_lines, "type"):
                findings.append(_finding(
                    "dispatch-inputs", relpath,
                    f"workflow_dispatch input '{entry_name}' has no type"))
            elif _key_scalar(entry_lines, "type") == "choice" \
                    and not _key_present(entry_lines, "options"):
                findings.append(_finding(
                    "dispatch-inputs", relpath,
                    f"workflow_dispatch choice input '{entry_name}' "
                    "lists no options"))
    return findings


def lint(root):
    findings = []
    findings.extend(check_agents_map(root))
    findings.extend(check_script_surface(root))
    findings.extend(check_dispatch_inputs(root))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ax_lint.py",
        description="Advisory Agent Experience lint: AGENTS.md map size, "
                    "script --json/--dry-run surface, workflow_dispatch "
                    "input types. Read-only; warn mode always exits 0.",
        epilog="examples:\n"
               "  python3 ax_lint.py --root .\n"
               "  python3 ax_lint.py --root . --mode error\n"
               "  python3 ax_lint.py --root . --json | jq .summary\n")
    parser.add_argument("--root", default=".",
                        help="repository root to lint (default: .)")
    parser.add_argument("--mode", choices=("warn", "error"), default="warn",
                        help="warn prints findings and exits 0 "
                             "(advisory); error exits 1 when any finding "
                             "exists (default: warn)")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    findings = lint(root)
    summary = {"findings": len(findings), "mode": args.mode}
    if args.json:
        print(json.dumps({"tool": "ax-lint", "mode": args.mode,
                          "root": root, "findings": findings,
                          "summary": summary}, indent=2))
    else:
        for item in findings:
            where = item["path"]
            if item["line"] is not None:
                where += f":{item['line']}"
            print(f"{where}: [{item['check']}] {item['message']}")
        print(f"ax-lint: {len(findings)} finding(s) "
              f"(mode={args.mode}, advisory)")
    if args.mode == "error" and findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
