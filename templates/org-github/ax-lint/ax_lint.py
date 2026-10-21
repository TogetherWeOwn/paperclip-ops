#!/usr/bin/env python3
"""ax-lint: advisory Agent Experience lint for repositories.

Warns (never fails in ``warn`` mode) about three cheap, deterministic
AX gaps:

* ``agents-map`` (AX6): a missing ``AGENTS.md`` entry map, or one over
  the line budget.
* ``script-surface`` (AX1/AX4): a flag-taking script with no
  *advertised top-level* ``--json``/``--dry-run`` support. Only a
  ``usage:`` line, an argparse declaration, an ``argv`` test, a
  ``$1`` comparison, or a ``case`` branch on a positional parameter
  certifies support; TODO/prose comments, child-command invocations,
  eval-built commands and incidental strings warn instead of silently
  passing.
* ``dispatch-inputs`` (AX2): a ``workflow_dispatch`` input with no
  ``type``, or a ``choice`` input with no ``options``.

Every finding carries a stable ``rule_id``, the source ``path``,
``line``/``evidence`` where available, and a ``remediation`` next
action. ``--json`` additionally reports a ``coverage`` object naming
the skipped/unsupported shapes the scan examined but did not clear.

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
    # The reusable workflow checks the lint source out at .ax-source/
    # inside the caller's tree; its scripts are ours, not the caller's.
    ".ax-source",
})

SCRIPT_EXTENSIONS = (".sh", ".bash", ".py")

FLAG_RE = re.compile(r"--[a-zA-Z][a-zA-Z0-9-]*")
SHEBANG_RE = re.compile(r"^#!")

KEY_RE_TEMPLATE = r"^\s*(['\"]?)%s\1\s*:"

MACHINE_FLAGS = ("--json", "--dry-run")


def _finding(check, path, message, line=None, rule_id=None, evidence=None,
             remediation=None):
    return {"check": check, "rule_id": rule_id or check, "path": path,
            "line": line, "message": message, "evidence": evidence,
            "remediation": remediation}


def _coverage(path, line, category, reason):
    return {"path": path, "line": line, "category": category,
            "reason": reason}


def check_agents_map(root):
    """AGENTS.md exists at the repo root and fits the map budget (AX6)."""
    path = os.path.join(root, "AGENTS.md")
    if not os.path.isfile(path):
        return [_finding(
            "agents-map", "AGENTS.md",
            "no AGENTS.md entry map at the repo root",
            rule_id="agents-map/missing",
            remediation="Add an AGENTS.md entry map at the repo root "
                        "(under 120 lines).")]
    with open(path, encoding="utf-8", errors="replace") as fh:
        count = 0
        over_line = None
        for lineno, file_line in enumerate(fh, start=1):
            count = lineno
            if lineno == AGENTS_BUDGET_LINES + 1:
                over_line = file_line.strip()
    if count > AGENTS_BUDGET_LINES:
        return [_finding(
            "agents-map", "AGENTS.md",
            f"AGENTS.md is {count} lines, over the {AGENTS_BUDGET_LINES}-line "
            "map budget; move procedures into docs/skills",
            line=AGENTS_BUDGET_LINES + 1,
            rule_id="agents-map/oversize",
            evidence=over_line,
            remediation="Move procedures out of AGENTS.md into docs/skills "
                        f"to fit the {AGENTS_BUDGET_LINES}-line map budget.")]
    return []


def _is_script_candidate(root, relpath, fullname):
    # Any exact `test`/`tests` directory segment marks a harness (root or
    # nested); lookalikes such as `mytests/` or `contest.sh` still scan.
    parent_dirs = relpath.split(os.sep)[:-1]
    if "test" in parent_dirs or "tests" in parent_dirs:
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


def _flag_literal(flag):
    """Exact flag token: `--json` never matches `--json-file`."""
    return r"(?<![\w-])[\"']?%s[\"']?(?![\w-])" % re.escape(flag)


def _certified_surface_flags(text):
    """Machine flags with advertised top-level support.

    Conservative on purpose: only a ``usage:`` line, an argparse-style
    ``add_argument('--flag')`` declaration, an ``argv`` membership test,
    a ``$1``/``$@``/``$*``/``$#`` equality test, or a ``case`` branch on
    a positional parameter certifies support. Comment-only mentions
    (TODO/prose), child-command invocations, eval-built commands and
    unrelated strings never certify; unrecognized shapes warn as
    uncertified rather than silently passing.
    """
    certified = set()
    lines = text.splitlines()
    for line in lines:
        if "usage:" in line.lower():
            for flag in MACHINE_FLAGS:
                if re.search(_flag_literal(flag), line):
                    certified.add(flag)
    if len(certified) == len(MACHINE_FLAGS):
        return certified
    code_lines = [_strip_comment(line) for line in lines]
    code = "\n".join(code_lines)
    for flag in MACHINE_FLAGS:
        if flag in certified:
            continue
        if re.search(r"add_argument\s*\([^)]*(['\"])%s\1"
                     % re.escape(flag), code):
            certified.add(flag)
        elif re.search(r"['\"]%s['\"]\s+in\s+[^\n]*\bargv\b"
                       % re.escape(flag), code):
            certified.add(flag)
        elif _shell_flag_comparison(code_lines, flag):
            certified.add(flag)
        elif _shell_case_branch(code_lines, flag):
            certified.add(flag)
    return certified


_POS_PARAM_RE = r"(?:\$1\b|\$\{1(?::[^}]*)?\}|\$[@*#]|\$\{[@*#]\})"


def _shell_flag_comparison(code_lines, flag):
    """A positional-parameter equality test against the flag certifies it.

    Matches ``[[ "$1" == "--json" ]]``, ``[ "$1" = --dry-run ]`` and the
    reversed operand order. Deliberately excludes ``!=`` (a negative test
    advertises nothing), non-positional variables (``$FLAGS``), and
    single-quoted parameters (``'$1'`` compares a literal, not the argument).
    """
    var = r"\"?%s\"?" % _POS_PARAM_RE
    lit = _flag_literal(flag)
    compare = re.compile(
        r"%s\s*(?:==|=)\s*%s|%s\s*(?:==|=)\s*%s" % (var, lit, lit, var))
    return any(compare.search(line) for line in code_lines)


def _shell_case_branch(code_lines, flag):
    """A ``case`` branch on $1/$@/$* matching the flag certifies it.

    Only ``case`` statements switching on a positional parameter count:
    ``case "$FORMAT" in --json)`` matches a *value*, not a CLI flag.
    """
    in_case = False
    for line in code_lines:
        if not in_case:
            match = re.match(r"^\s*case\s+(.*?)\s+in\s*$", line)
            if match and re.search(_POS_PARAM_RE, match.group(1)):
                in_case = True
            continue
        if re.match(r"^\s*esac\b", line):
            in_case = False
            continue
        segments = line.split(")", 1)
        if len(segments) < 2:
            continue
        for alt in segments[0].split("|"):
            if alt.strip().strip("\"'") == flag:
                return True
    return False


def _first_flag_evidence(text):
    """First ``--flag`` mention as a (1-based line, stripped text) pair."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FLAG_RE.search(line):
            return lineno, line.strip()
    return None, None


def check_script_surface(root):
    """Flag-taking scripts offer --json or --dry-run (AX1/AX4).

    A script "takes flags" when its text mentions any ``--long-flag``.
    Scripts with no long flags are not CLIs and are skipped. Support is
    certified only by advertised top-level evidence (see
    :func:`_certified_surface_flags`); incidental mentions warn instead
    of silently passing. Test scripts are skipped: they are harnesses,
    not agent surfaces.
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
            if _certified_surface_flags(text):
                continue
            lineno, evidence = _first_flag_evidence(text)
            findings.append(_finding(
                "script-surface", relpath,
                "takes flags but offers neither --json nor --dry-run",
                line=lineno,
                rule_id="script-surface/no-json-or-dry-run",
                evidence=evidence,
                remediation="Add top-level --json output or --dry-run "
                            "preview to the CLI and advertise it in a "
                            "`# usage:` line (e.g. "
                            "`# usage: tool --target X --json`)."))
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
    not fail what it cannot parse. Skipped shapes are reported via
    :func:`coverage` (and the ``--json`` coverage object), never
    silently cleared.
    """
    findings, _, _ = _dispatch_scan_all(root)
    return findings


def coverage(root):
    """Skipped/unsupported shapes the scan examined but did not clear.

    Returns ``{"skipped": [...], "unknown": [...]}``; each entry names
    the ``path``, ``line`` and ``reason``. ``skipped`` holds recognized
    shapes with nothing to check (inline ``workflow_dispatch``/``on:``
    forms); ``unknown`` holds shapes the indentation scan cannot parse
    (anchors, aliases, flow mappings). Always present in ``--json``,
    empty when the tree has no such shapes.
    """
    _, skipped, unknown = _dispatch_scan_all(root)
    return {"skipped": skipped, "unknown": unknown}


def _dispatch_scan_all(root):
    findings = []
    skipped = []
    unknown = []
    workflows = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(workflows):
        return findings, skipped, unknown
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
        found, skip, unk = _scan_dispatch(relpath, lines)
        findings.extend(found)
        skipped.extend(skip)
        unknown.extend(unk)
    return findings, skipped, unknown


def _scan_dispatch(relpath, lines):
    findings = []
    skipped = []
    unknown = []
    i, total = 0, len(lines)
    while i < total:
        line = lines[i]
        code = _strip_comment(line).rstrip()
        on_match = re.match(r"^\s*on\s*:\s*(.*)$", code)
        if on_match and on_match.group(1).strip() \
                and "workflow_dispatch" in code:
            skipped.append(_coverage(
                relpath, i + 1, "skipped",
                "inline `on:` form names workflow_dispatch; "
                "inputs not parsed"))
            i += 1
            continue
        match = re.match(r"^(\s*)workflow_dispatch\s*:\s*(.*)$", code)
        if not match:
            i += 1
            continue
        if match.group(2).strip():
            skipped.append(_coverage(
                relpath, i + 1, "skipped",
                "inline workflow_dispatch form; inputs not parsed"))
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
            for k, ln in enumerate(block):
                junk = re.match(r"^\s*inputs\s*:\s*(.*)$",
                                _strip_comment(ln).rstrip())
                if not junk:
                    continue
                rest = junk.group(1).strip()
                if rest in ("{}", "[]"):
                    skipped.append(_coverage(
                        relpath, block_start + k + 1, "skipped",
                        "workflow_dispatch inputs use an inline empty "
                        "form; nothing to type-check"))
                else:
                    unknown.append(_coverage(
                        relpath, block_start + k + 1, "unknown",
                        "workflow_dispatch inputs use an unsupported "
                        "shape (anchors, aliases, or flow mappings); "
                        "inputs not type-checked"))
                break
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
                entry_lineno = (block_start + inputs_at[0] + 1
                                + lineno_offset + 1)
                current = (name_match.group(1), entry_lineno, [ln])
                entries.append(current)
            elif _indent_of(ln) == entry_indent and re.match(
                    r"^\s*[A-Za-z0-9_.-]+\s*:", _strip_comment(ln)):
                unknown.append(_coverage(
                    relpath,
                    block_start + inputs_at[0] + 1 + lineno_offset + 1,
                    "unknown",
                    "workflow_dispatch input entry uses an unsupported "
                    "flow style; entry not type-checked"))
            elif current is not None and _indent_of(ln) > entry_indent:
                current[2].append(ln)
        for entry_name, entry_lineno, entry_lines in entries:
            evidence = lines[entry_lineno - 1].strip()
            if not _key_present(entry_lines, "type"):
                findings.append(_finding(
                    "dispatch-inputs", relpath,
                    f"workflow_dispatch input '{entry_name}' has no type",
                    line=entry_lineno,
                    rule_id="dispatch-inputs/untyped",
                    evidence=evidence,
                    remediation=f"Declare `type:` (string, choice, "
                                f"boolean, number, environment) for "
                                f"workflow_dispatch input '{entry_name}'."))
            elif _key_scalar(entry_lines, "type") == "choice" \
                    and not _key_present(entry_lines, "options"):
                findings.append(_finding(
                    "dispatch-inputs", relpath,
                    f"workflow_dispatch choice input '{entry_name}' "
                    "lists no options",
                    line=entry_lineno,
                    rule_id="dispatch-inputs/choice-without-options",
                    evidence=evidence,
                    remediation=f"Add an `options:` list to "
                                f"workflow_dispatch choice input "
                                f"'{entry_name}'."))
    return findings, skipped, unknown


def lint(root):
    findings = []
    findings.extend(check_agents_map(root))
    findings.extend(check_script_surface(root))
    findings.extend(check_dispatch_inputs(root))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ax_lint.py",
        description="Advisory Agent Experience lint (read-only; warn mode "
                    "always exits 0). Rules: agents-map, script-surface, "
                    "dispatch-inputs.",
        epilog="rules:\n"
               "  agents-map: AGENTS.md entry map exists, <= 120 lines\n"
               "    example: keep the root map short; move detail to docs/\n"
               "  script-surface: flag-taking scripts advertise top-level\n"
               "    --json or --dry-run (usage: line, argparse, argv test,\n"
               "    $1 comparison, or case branch on $1/$@/$*)\n"
               "    example: # usage: deploy --target X --json\n"
               "  dispatch-inputs: workflow_dispatch inputs declare type:;\n"
               "    choice inputs list options:\n"
               "    example: inputs:\n      target:\n        type: string\n"
               "  --json adds a coverage object naming skipped/unsupported\n"
               "  workflow shapes the scan did not clear.\n"
               "examples:\n"
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
                          "coverage": coverage(root),
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
