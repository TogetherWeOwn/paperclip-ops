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


# `usage:` certifies only as the advertised form: the first thing on the
# line after an optional comment marker, quote, or echo/print command.
# The command may use the call form (`print("usage: ...")`), not just a
# space after the verb: `print "..."` is a syntax error in Python 3.
USAGE_RE = re.compile(
    r"^\s*(?:#+|//|;)?\s*"
    r"(?:(?:echo|printf|print|puts)\s*\(?\s*(?:-\w+\s+)*)?"
    r"[\"'(]*\s*usage:", re.IGNORECASE)


def _string_content_mask(code):
    """Per-char flag: True where *code* sits inside a string literal.

    Delimiters themselves are False, so a match that starts on an opening
    quote is outside any enclosing literal. Handles ``\\`` escapes and
    triple quotes; an unbalanced quote errs toward "inside a literal",
    which warns rather than certifies.
    """
    mask = [False] * len(code)
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch not in ("'", '"'):
            i += 1
            continue
        delim = code[i:i + 3] if code[i:i + 3] in ("'''", '"""') else ch
        i += len(delim)
        while i < n and not code.startswith(delim, i):
            if code[i] == "\\" and i + 1 < n:
                mask[i] = mask[i + 1] = True
                i += 2
                continue
            mask[i] = True
            i += 1
        i += len(delim)
    return mask


def _code_match(pattern, code, in_literal):
    """True if *pattern* matches at a position outside string literals."""
    return any(not in_literal[m.start()] for m in re.finditer(pattern, code))


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
    skip = _heredoc_body_lines(lines)
    for idx, line in enumerate(lines):
        if idx not in skip and USAGE_RE.match(line):
            for flag in MACHINE_FLAGS:
                if re.search(_flag_literal(flag), line):
                    certified.add(flag)
    if len(certified) == len(MACHINE_FLAGS):
        return certified
    code_lines = [_strip_comment(line) for line in lines]
    code = "\n".join("" if idx in skip else line
                     for idx, line in enumerate(code_lines))
    in_literal = _string_content_mask(code)
    for flag in MACHINE_FLAGS:
        if flag in certified:
            continue
        if _code_match(
                r"add_argument\s*\(\s*(?:(['\"])-[^'\"\s]*\1\s*,\s*)*"
                r"(['\"])%s\2" % re.escape(flag), code, in_literal):
            certified.add(flag)
        elif _code_match(r"['\"]%s['\"]\s+in\s+[^\n]*\bargv\b"
                         % re.escape(flag), code, in_literal):
            certified.add(flag)
        elif _shell_flag_comparison(code_lines, flag, skip):
            certified.add(flag)
        elif _shell_case_branch(code_lines, flag, skip):
            certified.add(flag)
    return certified


_POS_PARAM_RE = r"(?:\$1\b|\$\{1(?::[^}]*)?\}|\$[@*#]|\$\{[@*#]\})"


def _blank_quoted(line):
    """Copy of *line* with quoted spans replaced by spaces.

    Length-preserving, so match offsets still line up with the original.
    Quote tracking mirrors :func:`_strip_comment`.
    """
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'" and i + 1 < len(line):
            # Escaped char: never opens/closes a quote. Blank both inside
            # a quoted span; keep them outside (offsets stay aligned).
            out.append("  " if quote else line[i:i + 2])
            i += 2
            continue
        if quote is None and ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == quote:
            quote = None
            out.append(ch)
        elif quote is None:
            out.append(ch)
        else:
            out.append(" ")
        i += 1
    return "".join(out)


_HEREDOC_OPEN_RE = re.compile(r"<<(-?)\s*\\?['\"]?([A-Za-z0-9_]+)")


def _heredoc_body_lines(code_lines):
    """Indexes of lines inside `<<word ... word` heredoc bodies.

    Heredoc bodies are generated text, not executed code: none of the
    certification channels may use their contents as evidence for the
    generator script. The opener is matched on the raw line (a quoted
    delimiter such as `<<'EOF'` is blanked away) but accepted only when
    its `<<` survives quote-blanking, so `echo "<<"` never opens a span.
    `<<-` allows tab-indented terminators, otherwise the terminator must
    match exactly. An unterminated opener skips to end of file (bash
    rejects it anyway).
    """
    skip = set()
    term, tabs = None, False
    for idx, line in enumerate(code_lines):
        if term is not None:
            skip.add(idx)
            body = line.lstrip("\t") if tabs else line
            if body == term:
                term = None
            continue
        line = _strip_comment(line)
        bare = _blank_quoted(line)
        for found in _HEREDOC_OPEN_RE.finditer(line):
            if bare[found.start():found.start() + 2] == "<<":
                tabs = found.group(1) == "-"
                term = found.group(2)
                break
    return skip


def _shell_flag_comparison(code_lines, flag, skip):
    """A positional-parameter equality test against the flag certifies it.

    Matches ``[[ "$1" == "--json" ]]``, ``[ "$1" = --dry-run ]`` and the
    reversed operand order, including trailing compound conditions
    (``[[ "$1" == "--json" && ... ]]``). The test opener must sit outside
    quotes, so incidental strings such as ``echo '"$1" == "--json"'``
    or ``echo '[ $1 = --json ]'`` never certify. Deliberately excludes
    ``!=`` (a negative test advertises nothing), non-positional
    variables (``$FLAGS``), and single-quoted parameters (``'$1'``
    compares a literal, not the argument).
    """
    var = r"\"?%s\"?" % _POS_PARAM_RE
    lit = _flag_literal(flag)
    operands = r"(?:%s\s*(?:==|=)\s*%s|%s\s*(?:==|=)\s*%s)" % (
        var, lit, lit, var)
    opener = re.compile(r"\[\[\s*|\[\s+")
    test = re.compile(operands + r"(?=[^\n\]]*(?:\]\]|\]))")
    for idx, line in enumerate(code_lines):
        if idx in skip:
            continue
        bare = _blank_quoted(line)
        for match in opener.finditer(bare):
            if test.match(line, match.end()):
                return True
    return False


def _shell_case_branch(code_lines, flag, skip):
    """A ``case`` branch on $1/$@/$* matching the flag certifies it.

    Only ``case`` statements switching on a positional parameter count:
    ``case "$FORMAT" in --json)`` matches a *value*, not a CLI flag.
    """
    depth = 0
    expect_label = False
    for idx, line in enumerate(code_lines):
        if idx in skip:
            continue
        opens = re.match(r"^\s*case\s+(.*?)\s+in\s*$", line)
        if depth == 0:
            if opens and re.search(_POS_PARAM_RE, opens.group(1)):
                depth, expect_label = 1, True
            continue
        if opens:
            depth += 1
            continue
        if re.match(r"^\s*esac\b", line):
            depth -= 1
            continue
        if re.match(r"^\s*case\b", line):
            bare_open = _blank_quoted(line)
            if re.match(r"^\s*case\b[^)]*\bin\b", bare_open):
                if re.search(r"\besac\b", bare_open):
                    # One-line nested case: open and close cancel out,
                    # so skip the line before its own `;;` can re-arm
                    # label parsing.
                    continue
                # Nested opener with trailing content: still a nested
                # case, tracked so its labels and `;;` stay fenced.
                depth += 1
                continue
        if depth > 1:
            continue
        # Only a line where a label is expected can be a branch label;
        # body lines (e.g. a child command's `--json)` continuation) can't.
        # Terminators are read off the quote-blanked line so a quoted
        # `;;` inside a branch body can't re-arm label parsing.
        bare = _blank_quoted(line)
        if expect_label and ")" in line:
            head, rest = line.split(")", 1)
            for alt in head.strip().lstrip("(").split("|"):
                if alt.strip().strip("\"'") == flag:
                    return True
            bare_rest = _blank_quoted(rest)
            expect_label = ";;" in bare_rest or ";&" in bare_rest
        elif ";;" in bare or ";&" in bare:
            expect_label = True
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
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'" and i + 1 < len(line):
            out.append(line[i:i + 2])
            i += 2
            continue
        if quote is None and ch in ("'", '"'):
            quote = ch
        elif ch == quote:
            quote = None
        elif ch == "#" and quote is None:
            break
        out.append(ch)
        i += 1
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
        while i < total and (not lines[i].strip()
                             or lines[i].lstrip().startswith("#")
                             or _indent_of(lines[i]) > base_indent):
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
                         if ln.strip() and not ln.lstrip().startswith("#")
                         and _indent_of(ln) > inputs_indent]
        if not child_indents:
            continue
        entry_indent = min(child_indents)
        entries = []
        current = None
        for lineno_offset, ln in enumerate(rest):
            if not ln.strip() or ln.lstrip().startswith("#"):
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
                # Its child lines must not be absorbed by the prior entry.
                current = None
            elif _indent_of(ln) == entry_indent:
                # Quoted names (`"second":`) and any other shape
                # neither pattern recognizes: unknown, never silently
                # dropped; reset so their children can't be absorbed
                # into the previous entry.
                unknown.append(_coverage(
                    relpath,
                    block_start + inputs_at[0] + 1 + lineno_offset + 1,
                    "unknown",
                    "workflow_dispatch input entry uses an unrecognized "
                    "shape (quoted name or flow style); entry not "
                    "type-checked"))
                current = None
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
