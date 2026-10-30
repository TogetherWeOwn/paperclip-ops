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


def _dispatch_code_lines(lines):
    """Mask block and multiline quoted scalar content, retaining line numbers.

    This is deliberately not a YAML parser. Only scalar headers in mapping
    values and sequence items are recognized; unsupported event shapes are
    reported separately rather than inferred from scalar text.
    """
    result = []
    block_indent = None
    quote = None
    for line in lines:
        indent = _indent_of(line)
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                result.append("")
                continue
            block_indent = None
        if quote is not None:
            result.append("")
            # YAML double quotes use backslash escapes; single quotes double.
            pos = 0
            while pos < len(line):
                if quote == '"' and line[pos] == "\\":
                    pos += 2
                elif line[pos:pos + 2] == "''" and quote == "'":
                    pos += 2
                elif line[pos] == quote:
                    quote = None
                    break
                else:
                    pos += 1
            continue
        code = _strip_comment(line).rstrip()
        result.append(code)
        value = re.match(r"^\s*(?:[^:#]+:\s*|-\s+)(.*)$", code)
        if not value:
            continue
        scalar = value.group(1).strip()
        if re.fullmatch(r"[|>](?:[+-]?[1-9]?|[1-9][+-]?)", scalar):
            block_indent = indent
        elif scalar.startswith(("'", '"')):
            q = scalar[0]
            pos = 1
            while pos < len(scalar):
                if q == '"' and scalar[pos] == "\\":
                    pos += 2
                elif q == "'" and scalar[pos:pos + 2] == "''":
                    pos += 2
                elif scalar[pos] == q:
                    break
                else:
                    pos += 1
            else:
                quote = q
    return result


def _dispatch_children(code_lines, parent, end):
    """Return immediate child line indexes and the parent's block end."""
    indent = _indent_of(code_lines[parent])
    stop = parent + 1
    while stop < end and (not code_lines[stop].strip()
                          or _indent_of(code_lines[stop]) > indent):
        stop += 1
    indexes = [i for i in range(parent + 1, stop) if code_lines[i].strip()]
    if not indexes:
        return [], stop
    child_indent = min(_indent_of(code_lines[i]) for i in indexes)
    return [i for i in indexes if _indent_of(code_lines[i]) == child_indent], stop


def _dispatch_key(code, key):
    return re.match(r"^\s*(['\"]?)" + re.escape(key) + r"\1\s*:\s*(.*)$", code)


def _dispatch_ambiguous_key(code):
    # An escaped quoted key might decode to a significant key. Merge keys
    # can import one indirectly. Neither is interpreted by this scanner.
    return bool(re.match(r'^\s*"[^"]*\\', code)
                or re.match(r"^\s*'[^']*''", code)
                or re.match(r"^\s*<<\s*:", code))


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
    code = _dispatch_code_lines(lines)
    total = len(code)

    def uncertain(index, reason):
        unknown.append(_coverage(relpath, index + 1, "unknown", reason))

    def ambiguous(index):
        if _dispatch_ambiguous_key(code[index]):
            uncertain(index, "escaped or merged mapping key is unsupported")
            return True
        return False

    # Only a root-level `on` mapping is an event declaration. In particular,
    # `on` under env, and workflow-looking text in run scalars, are data.
    for on in range(total):
        if not code[on].strip() or _indent_of(code[on]) != 0:
            continue
        if ambiguous(on):
            continue
        match = _dispatch_key(code[on], "on")
        if not match:
            continue
        value = match.group(2).strip()
        if value:
            if (value.startswith(("*", "&", "!", "|", ">"))
                    or (value.startswith(("{", "[")) and
                        ("\\" in value or re.search(r"(?:^|[\s\[,{:])[*&]", value)))):
                uncertain(on, "unsupported `on:` value; events not parsed")
            elif "workflow_dispatch" in value:
                skipped.append(_coverage(
                    relpath, on + 1, "skipped",
                    "inline `on:` form names workflow_dispatch; inputs not parsed"))
            continue
        events, on_end = _dispatch_children(code, on, total)
        for event in events:
            if ambiguous(event):
                continue
            match = _dispatch_key(code[event], "workflow_dispatch")
            if not match:
                if code[event].lstrip().startswith(("-", "*", "&")) and (
                        "workflow_dispatch" in code[event] or "*" in code[event]
                        or "&" in code[event]):
                    uncertain(event, "unsupported event sequence or alias")
                continue
            value = match.group(2).strip()
            if value:
                if value.startswith(("*", "&", "!", "|", ">")):
                    uncertain(event, "unsupported workflow_dispatch value")
                else:
                    skipped.append(_coverage(
                        relpath, event + 1, "skipped",
                        "inline workflow_dispatch form; inputs not parsed"))
                continue
            dispatch_children, dispatch_end = _dispatch_children(code, event, on_end)
            for inputs in dispatch_children:
                if ambiguous(inputs):
                    continue
                match = _dispatch_key(code[inputs], "inputs")
                if not match:
                    continue
                value = match.group(2).strip()
                if value:
                    if value in ("{}", "[]"):
                        skipped.append(_coverage(
                            relpath, inputs + 1, "skipped",
                            "workflow_dispatch inputs use an inline empty "
                            "form; nothing to type-check"))
                    else:
                        uncertain(inputs, "workflow_dispatch inputs use an unsupported "
                                  "shape (anchors, aliases, or flow mappings); "
                                  "inputs not type-checked")
                    continue
                entries, inputs_end = _dispatch_children(code, inputs, dispatch_end)
                for entry in entries:
                    name_match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*:\s*$", code[entry])
                    if not name_match:
                        uncertain(entry, "workflow_dispatch input entry uses an unsupported "
                                  "shape (quoted name or flow style); entry not type-checked")
                        continue
                    name = name_match.group(1)
                    properties, _ = _dispatch_children(code, entry, inputs_end)
                    type_value = None
                    options = False
                    unsupported = False
                    for prop in properties:
                        if ambiguous(prop):
                            unsupported = True
                            continue
                        type_match = _dispatch_key(code[prop], "type")
                        if type_match:
                            type_value = type_match.group(2).strip()
                            if (not type_value or type_value.startswith(("|", ">", "*", "&", "!", "{", "["))
                                    or "\\" in type_value or "''" in type_value
                                    or (type_value.startswith(("'", '"')) and
                                        (len(type_value) < 2 or type_value[-1] != type_value[0]))):
                                uncertain(prop, "unsupported workflow_dispatch input type value")
                                unsupported = True
                        if _dispatch_key(code[prop], "options"):
                            options = True
                    if unsupported:
                        continue
                    evidence = lines[entry].strip()
                    if type_value is None:
                        findings.append(_finding(
                            "dispatch-inputs", relpath,
                            f"workflow_dispatch input '{name}' has no type",
                            line=entry + 1, rule_id="dispatch-inputs/untyped",
                            evidence=evidence,
                            remediation=f"Declare `type:` (string, choice, boolean, "
                                        f"number, environment) for workflow_dispatch input '{name}'."))
                    elif type_value.strip("'\"") == "choice" and not options:
                        findings.append(_finding(
                            "dispatch-inputs", relpath,
                            f"workflow_dispatch choice input '{name}' lists no options",
                            line=entry + 1, rule_id="dispatch-inputs/choice-without-options",
                            evidence=evidence,
                            remediation=f"Add an `options:` list to workflow_dispatch choice input '{name}'."))
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
