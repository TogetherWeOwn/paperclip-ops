#!/usr/bin/env python3
"""Orphan-suite ratchet: fail when a test suite is wired nowhere.

Every tracked ``test_*`` suite (``.py``/``.sh``/``.mjs``/``.cjs``/``.js``/
``.ts``/``.mts``/``.cts``) and every tracked JS/TS spec file
(``*.test.*`` / ``*.spec.*`` over the same JS/TS extensions), excluding
``vendor/``, ``node_modules/`` and toolkit-restored paths, must be either
wired into CI or named in
``verification/orphan_suites_allowlist.json`` with a one-line reason. The
check fails when a suite is neither, and also when an exemption goes stale
(names a file that is gone, was never an unwired suite, or is now wired).

A suite counts as wired when any of these holds:

* a ``run:`` step in ``.github/workflows/*.yml`` references it by filename
  or path;
* a script those steps call references it. The corpus starts with the
  parse tier of ``scripts/verify-fast.sh`` (the text above its
  ``--parse-only`` exit, which is all CI executes) and then follows every
  by-name reference to a tracked script (``.sh``/``.py``/``.mjs``/``.cjs``/
  ``.js``) to a fixpoint, so suites executed by helper gates (mutation
  gates, installer harnesses, ``unittest discover`` wrappers) count as
  wired. Hops through a variable prefix (``bash "$ROOT/x.sh"``,
  ``"${ROOT}/x.sh"``) are followed. A missing parse-exit marker fails
  the check rather than silently widening the corpus to the full tier;
* a ``python3 -m unittest <module>`` reference names its module stem
  (e.g. ``test_pr_supply`` for ``ops/productivity/test_pr_supply.py``)
  when that stem is unique among tracked suites, or a
  ``unittest discover -s <dir> -p '<glob>'`` pattern covers it.
  ``discover`` pairs are collected per command segment through the
  same command-position check (flags in any order, either quote
  style), so a pattern quoted in a message or assignment wires
  nothing. A missing ``-s`` means the current directory: it resolves
  against a preceding ``cd`` or the step's ``working-directory``.
  ``discover`` collects ``.py`` modules the way unittest does: a root
  (or unscoped) pattern covers root-level suites plus nested suites
  under tracked packages (every intervening directory holds a
  tracked ``__init__.py``); anything deeper is still an orphan.
* a ``node --test <targets...>`` reference names its file: every argument
  past the flag runs, not just the first one, so a second spec on the
  same line wires too (option values such as a ``--test-name-pattern``
  regex excepted). A target glob (``test/*.test.mjs``) expands
  against tracked suites, and a bare directory target (or a bare
  ``node --test`` with no targets) covers ``*.test.{mjs,cjs,js}``
  suites beneath it, matching the node test runner's default file
  matching. ``*.spec.*`` files are outside those defaults, so they
  need an explicit file mention, a covering glob, ``vitest run``
  coverage or an exemption;
* an ``npm test`` / ``npm run <script>`` step resolves the named script
  from the step package's ``package.json`` (via its
  ``working-directory:`` or a preceding ``cd``) and wires what that
  script executes: ``node --test`` targets as above, a direct
  ``node <suite file>``, or ``vitest run`` coverage. A ``vitest run``
  with no file filters wires every tracked ``*.test.*`` / ``*.spec.*``
  suite under the package directory; with filters, only suites matching
  a filter by exact path or glob. Only the resolved script's executed
  targets count.

Deliberately NOT wiring:

* references below the ``--parse-only`` exit of ``scripts/verify-fast.sh``:
  the full local tier never runs in CI;
* a bare filename or module-stem mention when another tracked suite shares
  that basename or stem: a path-qualified reference or a ``discover``
  pattern scoped to the suite's directory is required instead;
* full-line comments (``#`` or ``//``) and trailing ``#`` comments
  outside quotes are stripped from every corpus file;
* mentions outside command position never wire. A match counts only
  when its token is a bare suite name/path (never a word inside a
  quoted sentence) sitting where a command segment (split on
  ``&&``/``||``/``|``/``;``/backtick/``$(`` outside quotes) executes
  it: as the command itself, as an interpreter's script or
  ``-m unittest``/``-m pytest`` argument (any other ``-m`` module
  inspects its arguments), as a bare ``pytest``/``coverage run``/
  ``source``/``$VAR``-command argument, in a ``for VAR in ...`` list
  whose body (from ``do`` to its matching ``done``, even on later
  lines) executes the loop variable, as the bare-path value of a
  scalar assignment whose variable later runs in command position
  (``SUITE="$ROOT/test_x.sh"`` then ``bash "$SUITE"``), as an
  argument of a shell function that forwards ``$1``/``$@`` to a
  runner, or behind a known wrapper (``check``/``run-suite``/``info``)
  whose words name what runs. Launchers are walked through
  (``env``/``sudo``/``nice``/``uv run`` ...) with their flags and
  option values skipped. The default is closed: any other command --
  inspectors, stagers, linters, checksum/hash tools, unlisted tools --
  never wires, so no denylist has to stay ahead of the next tool that
  names a suite. Interpreter short-flag clusters count
  (``bash -lc`` is ``-c``), and options that take an argument
  (``-o``/``-W``/``-X``) are skipped when finding the script word.
  Parse checks (``ast.parse``, ``py_compile``, ``bash -n``,
  ``node --check``), array manifests (``files=( ... )``) and message
  text never wire, and a helper script named only outside command
  position (including a string literal in a ``.py`` data tuple) is
  not followed into the corpus either. A suite chained after a check
  on the same line still wires. In a ``.py`` segment only a real
  runner call (``subprocess``/``os.system``/``runpy``) wires: a
  string-form call only when the judged string runs the suite, and a
  ``subprocess``-style argv list only when its first element is an
  interpreter (or ``sys.executable``) that would run the matched word,
  so ``print``/logging/``raise``/argparse/help text never does;
* Python docstrings never wire: triple-quoted blocks in ``.py`` helpers
  are usage text, so a suite named only there is still an orphan.
  Bracket continuations are joined first, so a multi-line ``print(``
  is still one message line;
* shell ``echo``/``printf``-only lines never wire: a reminder that names
  a suite does not execute it. A separator inside quotes is message text,
  not chaining, so it does not keep the line; a pipe into a real runner
  does (``echo payload | bash ./test_x.sh`` wires);
* shell heredoc bodies never wire: ``cat <<EOF`` usage text is not a run.
  Bodies fed to an interpreter's stdin (``bash -s <<'EOS'``) are run
  input, so a suite listed there wires;
* shell function bodies judge line by line: only array manifests and
  open quotes join continuation lines, so a ``case`` arm or an
  inspector command inside ``f() { ... }`` never wires, while a run
  on its own body line still does;
* classifier tables never wire: ``.github/scripts/ci_changes.py`` names
  suites so changed paths classify to an area. It is not followed as a
  helper script, so listing a suite there does not count as running it;
* package ``check``/``build``/``typecheck`` scripts never wire: only the
  resolved ``npm test`` / ``npm run <script>`` script's executed
  targets count, so ``node --check`` / ``tsc`` / build mentions wire
  nothing even when CI runs that script too. An ``npm test`` with no
  resolvable ``package.json`` (or no ``test`` script in it) wires
  nothing: an unresolvable package fails closed;
* a suite never wires itself: matches inside the suite's own file are
  ignored, so usage text that names its own file does not count;
* shell ``case`` arms (``test_foo.sh)`` with nothing but whitespace, ``(``
  or ``|`` before the name on the line) do not count. An allowlist-style
  pattern mention pins text; it does not execute the suite.

Usage: ``python3 verification/orphan_suites.py [--root DIR]
[--allowlist PATH]``. Exits 0 when every suite is wired or exempt, 1 naming
each offending file otherwise.
"""
import fnmatch
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = ROOT / "verification/orphan_suites_allowlist.json"
DEFAULT_WORKFLOWS = ROOT / ".github/workflows"
VERIFY_FAST = Path("scripts/verify-fast.sh")

SCRIPT_EXTS = (".sh", ".py", ".mjs", ".cjs", ".js")
TEST_PREFIX_EXTS = (".py", ".sh", ".mjs", ".cjs", ".js", ".ts", ".mts",
                    ".cts")
SPEC_SUFFIXES = (".test.mjs", ".test.cjs", ".test.js", ".test.ts",
                 ".test.mts", ".test.cts",
                 ".spec.mjs", ".spec.cjs", ".spec.js", ".spec.ts",
                 ".spec.mts", ".spec.cts")

USAGE = ("usage: orphan_suites.py [--root DIR] [--allowlist PATH]")


def tracked_files(root):
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    return [entry for entry in proc.stdout.split("\0") if entry]


def restored_paths(root):
    """Toolkit-restored destinations (manifest minus tracked allowlist)."""
    try:
        manifest = (root / "vendor/toolkit-files.txt").read_text()
        allowlist = (root / "vendor/toolkit-tracked-allowlist.txt"
                     ).read_text().splitlines()
    except OSError:
        return set()
    tracked = {line for line in allowlist if line and not line.startswith("#")}
    destinations = {line.split("|", 1)[0] for line in manifest.splitlines()
                    if line}
    return destinations - tracked


def is_excluded(path, restored):
    parts = Path(path).parts
    return (path in restored or "vendor" in parts or "node_modules" in parts)


def is_suite_name(name):
    """Whether a filename is a test suite by repo convention.

    ``test_*`` scripts (Python, shell and JS/TS) plus JS/TS spec files
    (``*.test.*`` / ``*.spec.*``): the patterns this repo runs suites
    under, via ``unittest``/direct execution and ``node --test`` /
    ``vitest run`` respectively.
    """
    if name.startswith("test_") and name.endswith(TEST_PREFIX_EXTS):
        return True
    return name.endswith(SPEC_SUFFIXES)


def tracked_suites(root):
    restored = restored_paths(root)
    suites = [entry for entry in tracked_files(root)
              if is_suite_name(Path(entry).name)
              and not is_excluded(entry, restored)]
    return sorted(suites)


DOCSTRING_RE = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
ECHO_ONLY_RE = re.compile(r"^[ \t]*(?:echo|printf)(?![\w\-])")
PRINT_ONLY_RE = re.compile(r"^[ \t]*print\s*\(")
CMD_SEP_RE = re.compile(r"&&|\|\||\||;|`|\$\(")
# Message-only commands kept for the generic-command guard: a bare
# suite path after ``die``/``fail``-style verbs is message text.
# ``log``/``info``/``usage`` etc. are excluded there because repos
# also use them as run wrappers (``info bash ./test_x.sh``).
DIE_MSG_RE = re.compile(
    r"^(?:die|fail|fatal|warn|warning|err|error|log|info|debug|usage|abort"
    r"|panic)\b[\s(:]")
LEADING_KEYWORD_RE = re.compile(
    r"(?:then|do|else|elif|fi|if|while|until|for|in|case|esac|select|done"
    r"|time|coproc|function)\b")
CASE_PATTERN_RE = re.compile(r"^\(?[\w*?|\[\]\-/.\"'$]+\)")
# ``.py`` calls that really execute: in a ``.py`` segment only these
# wire, so help/argparse/log/raise text and data tuples never do.
PY_RUNNER_RE = re.compile(
    r"\b(?:subprocess\.(?:run|call|check_call|check_output|Popen)"
    r"|os\.system|os\.spawn\w*|os\.exec\w*|check_call"
    r"|runpy\.(?:run_path|run_module)|loadTestsFrom\w*)\s*\(")
# A heredoc opener feeding an interpreter's stdin (``bash -s <<'EOS'``,
# ``python3 - <<'PY'``): the body is run input, not usage text.
INTERPRETER_STDIN_RE = re.compile(
    r"\b(?:bash|sh|dash|python3?|node)\b[^\n]*?"
    r"(?:(?:^|\s)-(?:\s|$)|\s-s\b|/dev/stdin\b)")


def _paren_delta(line):
    """Net open-bracket depth of a line, ignoring quoted spans."""
    span = _strip_quoted_spans(line)
    return (span.count("(") + span.count("[") + span.count("{")
            - span.count(")") - span.count("]") - span.count("}"))


def _join_open_parens(lines):
    """Join ``.py`` continuation lines inside unclosed brackets.

    A suite named on the continuation of a multi-line ``print(`` belongs
    to that call (still a message), while one inside a multi-line
    ``subprocess.run([`` still belongs to a run.
    """
    out = []
    buf = ""
    depth = 0
    for line in lines:
        buf = line if not buf else buf + " " + line
        depth += _paren_delta(line)
        if depth <= 0:
            out.append(buf)
            buf = ""
            depth = 0
    if buf:
        out.append(buf)
    return out


def _open_quote(text):
    """Quote char still open at end of text (backslash-aware), or None."""
    quote = None
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
        i += 1
    return quote


def _join_shell_continuations(lines):
    """Join shell lines inside array manifests or open quotes.

    A suite named on the continuation of a multi-line ``echo "usage:
    ... "`` belongs to that message, and one inside a ``files=( ... )``
    manifest belongs to that assignment: both must judge as one logical
    line, or the bare filename on its own line reads as a command.
    Anything else -- brace-delimited function bodies, brace groups,
    subshells, ``[[ ... ]]`` tests -- is judged line by line: joining a
    whole function body into one segment puts the function name ahead of
    every command in it, so ``case`` arms, inspector commands and array
    manifests inside the body stop being recognised.
    """
    out = []
    buf = ""
    depth = 0
    for line in lines:
        buf = line if not buf else buf + " " + line
        span = _strip_quoted_spans(line)
        # Only array manifests (``name=(`` ... ``)``) span lines as data;
        # function bodies and brace groups are separate commands.
        if depth > 0 or re.search(r"[A-Za-z_]\w*\+?=\(", span):
            depth += span.count("(") - span.count(")")
        if depth <= 0 and _open_quote(buf) is None:
            out.append(buf)
            buf = ""
            depth = 0
        elif depth < 0:
            depth = 0
    if buf:
        out.append(buf)
    return out


def _strip_quoted_spans(line):
    """Line with single/double-quoted spans removed (backslash-aware).

    Used only to judge whether a message line chains a real command:
    separators inside quotes are message text, not chaining.
    """
    out = []
    i, n = 0, len(line)
    quote = None
    while i < n:
        char = line[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _has_command_sep(line):
    """Whether the line chains another command outside quotes."""
    return CMD_SEP_RE.search(_strip_quoted_spans(line)) is not None


def _strip_inline_comment(line):
    """Cut a trailing ``#`` comment outside quotes (shell and python).

    ``#`` starts a comment only unquoted and at line start or after
    whitespace or ``;``/``&``/``|``/``(``/``)``, so ``$#``, ``#``-fragments
    in URLs and ``#`` inside words survive. ``//`` is never stripped
    inline (URLs); full-line ``//`` is still dropped by ``code_text``.
    """
    quote = None
    i, n = 0, len(line)
    while i < n:
        char = line[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        if char == "#" and (i == 0 or line[i - 1] in " \t;&|()"):
            return line[:i]
        i += 1
    return line


def _heredoc_delim(line):
    """Heredoc delimiter if the line opens ``<<[ -]DELIM`` outside quotes."""
    quote = None
    i, n = 0, len(line)
    while i < n:
        char = line[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        if line.startswith("<<", i):
            match = re.match(r"<<-?\s*([\"']?)(\w+)\1", line[i:])
            if match:
                return match.group(2)
            i += 2
            continue
        i += 1
    return None


def _strip_heredocs(lines):
    """Drop shell heredoc bodies: ``cat <<EOF`` usage text never runs.

    Bodies fed to an interpreter's stdin (``bash -s <<'EOS'``) are run
    input, so they are kept and judged like any other command text.
    """
    out = []
    term = None
    keep = False
    for line in lines:
        if term is not None:
            if keep:
                out.append(line)
            if line.strip() == term:
                term = None
                keep = False
            continue
        delim = _heredoc_delim(line)
        if delim is not None:
            term = delim
            keep = INTERPRETER_STDIN_RE.search(line) is not None
        out.append(line)
    return out


def code_text(text, python=False):
    """Comment/docstring/message-stripped text.

    Full-line comments are always dropped, as are trailing ``#`` comments
    outside quotes. For ``.py`` segments (``python=True``) triple-quoted
    docstring blocks are usage text, not runs, so they go too; bracket
    continuations are joined first so a multi-line ``print(`` is still
    one message line. Lines whose only command is ``print(...)`` go too.
    For shell segments a line whose only command is ``echo``/``printf``
    is a reminder, not a run (a separator inside quotes does not count
    as chaining), and heredoc bodies are usage text unless fed to an
    interpreter's stdin. Lines chaining another command are kept.
    """
    if python:
        text = DOCSTRING_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("#", "//")):
            continue
        line = _strip_inline_comment(line)
        if not line.strip():
            continue
        lines.append(line)
    if python:
        lines = _join_open_parens(lines)
    else:
        lines = _join_shell_continuations(_strip_heredocs(lines))
    kept = []
    for line in lines:
        if not _has_command_sep(line):
            if line.rstrip().endswith("\\"):
                # Backslash-continued reminder: the next line belongs to
                # this echo/print (logical_text joins them). Stripping
                # now would orphan the continuation as a bare command.
                kept.append(line)
                continue
            if not python and ECHO_ONLY_RE.match(line):
                continue
            if python and PRINT_ONLY_RE.match(line):
                continue
        kept.append(line)
    return "\n".join(kept)


PARSE_TIER_EXIT = re.compile(
    r"^[ \t]*if \[\[ \"\$MODE\" == \"parse\" \]\]; then[ \t]*$",
    re.MULTILINE)


def parse_tier_text(text, source):
    """Text of verify-fast.sh that CI executes (up to the parse-only exit).

    References below the exit never run in CI, so they must not count as
    wiring. A missing marker fails closed: silently widening the corpus
    would re-bless full-tier-only suites.
    """
    match = PARSE_TIER_EXIT.search(text)
    if match is None:
        raise RuntimeError(f"parse-tier marker not found in {source}")
    return text[:match.start()]


def _step_working_dir(lines, i, run_indent, forward_from=None):
    """``working-directory:`` of the step owning the ``run:`` at ``i``.

    YAML key order in a step is free, so the key is searched both before
    the ``run:`` line and after the run body (``forward_from``: first line
    past a block body, else the line after ``run:``), stopping at the next
    step entry with the same indent rules either way.
    """
    j = i - 1
    while j >= 0 and i - j <= 25:
        text = lines[j]
        stripped = text.lstrip()
        if not stripped or stripped.startswith("#"):
            j -= 1
            continue
        indent = len(text) - len(stripped)
        if indent < run_indent:
            break
        if stripped.startswith("- ") and indent <= run_indent + 2:
            break
        if indent in (run_indent, run_indent + 2):
            wd = re.match(r"working-directory\s*:\s*(.+?)\s*$", stripped)
            if wd is not None:
                value = wd.group(1).strip("\"'")
                if not value or value.startswith("$"):
                    return None
                return value
        j -= 1
    k = forward_from if forward_from is not None else i + 1
    while k < len(lines) and k - i <= 25:
        text = lines[k]
        stripped = text.lstrip()
        if not stripped or stripped.startswith("#"):
            k += 1
            continue
        indent = len(text) - len(stripped)
        if indent < run_indent:
            break
        if stripped.startswith("- ") and indent <= run_indent + 2:
            break
        if indent in (run_indent, run_indent + 2):
            wd = re.match(r"working-directory\s*:\s*(.+?)\s*$", stripped)
            if wd is not None:
                value = wd.group(1).strip("\"'")
                if not value or value.startswith("$"):
                    return None
                return value
        k += 1
    return None


def _has_job_defaults_wd(text):
    """Whether the workflow sets ``defaults.run.working-directory``.

    Either a job-level or a workflow-level ``defaults:`` block with a
    ``run:`` child naming ``working-directory:`` shifts every step's cwd
    that lacks its own key. The detector does not resolve that cwd, so
    callers fail closed for affected steps.
    """
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"defaults\s*:\s*(?:#.*)?$", stripped) is None:
            continue
        def_indent = len(line) - len(stripped)
        j = idx + 1
        while j < len(lines):
            cand = lines[j]
            cand_stripped = cand.lstrip()
            if not cand_stripped or cand_stripped.startswith("#"):
                j += 1
                continue
            cand_indent = len(cand) - len(cand_stripped)
            if cand_indent <= def_indent:
                break
            if re.match(r"run\s*:\s*(?:#.*)?$",
                        cand_stripped) is not None:
                run_indent = cand_indent
                k = j + 1
                while k < len(lines):
                    sub = lines[k]
                    sub_stripped = sub.lstrip()
                    if not sub_stripped or sub_stripped.startswith("#"):
                        k += 1
                        continue
                    sub_indent = len(sub) - len(sub_stripped)
                    if sub_indent <= run_indent:
                        break
                    if re.match(r"working-directory\s*:",
                                sub_stripped) is not None:
                        return True
                    k += 1
            j += 1
    return False


def run_bodies(text):
    """Collect ``run:`` bodies without assuming any job layout.

    Captures inline bodies (``run: ./foo.sh``) and indented block bodies
    (``run: |`` followed by more-indented lines). Comment lines can never
    open a block. A step's ``working-directory:`` (before or after
    ``run:``) is prepended as a ``cd`` so relative ``discover -s``
    directories resolve against it; unresolvable (variable) directories
    are left alone. When the workflow sets a job-level
    ``defaults.run.working-directory`` and the step names none of its
    own, the step's cwd is unresolved, so its body is dropped (fail
    closed: it wires nothing) rather than resolved against the repo root.
    """
    bodies = []
    lines = text.splitlines()
    job_wd = _has_job_defaults_wd(text)
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            match = re.match(
                r"^(\s*)(?:-\s+)?run:\s*(\|[-+]?|>[-+]?)?\s*(.*)$", line)
            if match and (match.group(2) or match.group(3)
                          or line.lstrip().startswith("run:")
                          or "- run:" in line):
                indent = len(match.group(1))
                inline = match.group(3)
                if match.group(2):
                    block = []
                    j = i + 1
                    while j < len(lines) and (
                            not lines[j].strip()
                            or len(lines[j]) - len(lines[j].lstrip()) > indent):
                        block.append(lines[j])
                        j += 1
                    body = textwrap.dedent("\n".join(block))
                    wd = _step_working_dir(lines, i, indent,
                                           forward_from=j)
                    if wd is not None:
                        body = "cd \"%s\"\n" % wd + body
                        bodies.append(body)
                    elif not job_wd:
                        bodies.append(body)
                    i = j - 1
                elif inline:
                    wd = _step_working_dir(lines, i, indent,
                                           forward_from=i + 1)
                    if wd is not None:
                        inline = "cd \"%s\" && %s" % (wd, inline)
                        bodies.append(inline)
                    elif not job_wd:
                        bodies.append(inline)
        i += 1
    return bodies


def is_case_arm(line, start, end):
    """True for shell ``case`` arms: ``test_foo.sh)`` with only whitespace,
    ``(`` or ``|`` before the name on the line, or an alternation arm such
    as ``test_foo.sh|test_bar.sh)``. A pattern mention pins text; it does
    not execute the suite. Parenthesised invocations such as
    ``(cd dir && python3 -m unittest test_foo)`` still count, because command
    text precedes the name on the line."""
    if line[end:end + 1] != ")":
        return False
    before = line[:start]
    if re.fullmatch(r"[\s(|]*", before) is not None:
        return True
    return re.fullmatch(r"\s*\(?[\w.\-$/{}*\"' ]*\|", before) is not None


TABLE_ONLY_SCRIPTS = frozenset({".github/scripts/ci_changes.py"})


def split_segments(line):
    """Split a logical line into ``(segment, offset)`` command segments.

    Splits on ``&&``/``||``/``|``/``;``/backtick/``$(`` outside quotes, so a
    suite chained after a check (``cp a b && bash ./test_x.sh``) is judged
    by its own command, not by the check. Separators inside quotes are
    message text.
    """
    segments = []
    start = 0
    i, n = 0, len(line or "")
    quote = None
    while i < n:
        char = line[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        if char == "`" or line.startswith("$(", i):
            segments.append((line[start:i], start))
            i += 1 if char == "`" else 2
            start = i
            continue
        if line.startswith("&&", i) or line.startswith("||", i):
            segments.append((line[start:i], start))
            i += 2
            start = i
            continue
        if char in ("|", ";"):
            segments.append((line[start:i], start))
            i += 1
            start = i
            continue
        i += 1
    segments.append((line[start:], start))
    return segments


def _shell_tokens(segment):
    """Tokenise a segment into ``(text, start, end)`` shell words.

    Quotes are removed from token text but quoted whitespace stays
    inside the token, so a sentence (``"run ./test_x.sh by hand"``) is
    one token and never equals a bare suite path. Newlines separate
    words too (multi-line loop/function bodies judge as text).
    Offsets span the original (quoted) token.
    """
    tokens = []
    i, n = 0, len(segment or "")
    while i < n:
        if segment[i] in " \t\n":
            i += 1
            continue
        start = i
        chars = []
        quote = None
        while i < n:
            char = segment[i]
            if segment.startswith("$(", i):
                # Command substitution stays inside the token even when
                # it contains spaces/quotes: ``$(dirname "$0")/x.py``
                # is one path word.
                depth = 0
                while i < n:
                    chars.append(segment[i])
                    if segment[i] == "(":
                        depth += 1
                    elif segment[i] == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
                continue
            if quote is not None:
                if char == "\\" and i + 1 < n:
                    chars.append(segment[i + 1])
                    i += 2
                    continue
                if char == quote:
                    quote = None
                    i += 1
                    continue
                chars.append(char)
                i += 1
                continue
            if char in ("'", '"'):
                quote = char
                i += 1
                continue
            if char == "\\" and i + 1 < n:
                chars.append(segment[i + 1])
                i += 2
                continue
            if char in " \t\n":
                break
            chars.append(char)
            i += 1
        if i == start and not chars:
            # Defensive: the skip and break sets must agree, or an
            # unknown separator stalls here forever emitting empty
            # tokens. Skip the character instead of hanging.
            i += 1
            continue
        tokens.append(("".join(chars), start, i))
    return tokens


_PATH_PREFIX_RE = re.compile(r"^(?:\./|/|\$\{?\w+\}?/)")


def _bare_value(text):
    """Token text reduced to a bare path/name, or the text unchanged.

    Strips wrapping punctuation, a ``VAR=``/``--flag=`` prefix and a
    ``./``/``/``/``$VAR/`` path prefix. A token with interior
    whitespace (a quoted sentence) can never equal a suite name after
    this, because suite names have no whitespace.
    """
    value = text.strip().lstrip("([{\"'").rstrip(")]}.,;\"'")
    if value.startswith("$("):
        depth = 0
        for pos, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    if value[pos + 1:pos + 2] == "/":
                        value = value[pos + 2:]
                    break
    if "=" in value:
        head, _, tail = value.partition("=")
        if re.fullmatch(r"[A-Za-z_][\w-]*|--?[\w-]+", head):
            value = tail.lstrip("([{\"'").rstrip(")]}.,;\"'")
    value = _PATH_PREFIX_RE.sub("", value)
    return value


def _is_bare_token(token_text, matched):
    """Whether a token is exactly the matched suite name/path.

    Accepts path prefixes (``./``, ``$ROOT/``), a ``VAR=`` value and
    dotted-module forms (``ops.test_x``, ``test_x.TestCase`` for a
    ``test_x`` stem match). Anything longer -- a sentence, a longer
    filename, another path -- is not bare.
    """
    value = _bare_value(token_text)
    name = _bare_value(matched)
    if not value or not name:
        return False
    if "(" in value:
        # Python call argument glued to its callee in one shell token
        # (``loadTestsFromName("test_x")``).
        value = value.rsplit("(", 1)[-1].rstrip(")]}.,;\"'")
    if any(char in value for char in " \t"):
        return False
    if value == name or value.endswith("." + name) \
            or value.startswith(name + "."):
        return True
    # Basename/stem match (only consulted when that name is unique
    # among tracked suites): a directory-prefixed token ending in the
    # name is the same suite, e.g. a step with a different
    # ``working-directory:`` running ``./test/test_x.sh``.
    return "/" not in name and value.endswith("/" + name)


# Commands whose bare-path argument is inspected/staged/described,
# never executed. A suite named as their argument does not wire.
# The default is closed: a command not listed here (and not an
# interpreter, a wrapper or the suite itself) does not wire either --
# inspectors must never be enumerated to stay ahead of the next lint,
# checksum or copy tool that names a suite file.
NON_RUNNER_COMMANDS = frozenset({
    "echo", "printf", "test", "[", "[[", "read", "cat", "tac", "head",
    "tail", "less", "more", "cp", "mv", "rm", "mkdir", "rmdir",
    "chmod", "chown", "touch", "ln", "install", "grep", "egrep",
    "fgrep", "rg", "ag", "ls", "dir", "shellcheck", "shfmt", "ruff",
    "flake8", "pylint", "mypy", "git", "tar", "zip", "unzip", "gzip",
    "sha256sum", "md5sum", "sum", "wc", "stat", "file", "du", "df",
    "sed", "awk", "cut", "tr", "sort", "uniq", "diff", "cmp", "comm",
    "jq", "yq", "curl", "wget", "true", "false", ":", "cd", "pushd",
    "popd", "pwd", "which", "type", "hash", "basename", "dirname",
    "realpath", "readlink", "date", "sleep", "kill", "wait", "unset",
    "set", "shift", "die", "fail", "fatal", "abort", "panic",
    "command", "builtin",
})
# Launchers: the real command follows them (plus their flags).
LAUNCHER_COMMANDS = frozenset({
    "env", "exec", "time", "sudo", "timeout", "nice", "nohup",
    "stdbuf", "xargs", "uv",
})
# Launcher flags that take a separate value, per launcher: ``env -u
# NAME``, ``nice -n N``, ``sudo -u USER``, ``stdbuf -o FILE``. The value
# is an option argument, never the command. Flags that take none
# (``env -i``, ``sudo -n``) are absent, so the word after them stays
# the command. Attached forms (``-uNAME``) need no entry: they are
# skipped as one flag word.
LAUNCHER_OPTARG = {
    "env": frozenset({"-u"}),
    "nice": frozenset({"-n"}),
    "sudo": frozenset({"-u", "-g", "--user", "--group"}),
    "stdbuf": frozenset({"-o", "-e", "-i"}),
    "xargs": frozenset({"-n", "-I", "-P", "-s", "-a"}),
}
# ``uv run`` flags that take a separate value (``--python 3.12``).
UV_RUN_OPTARG = frozenset({
    "--python", "-p", "--with", "--group", "--project",
})
# Known runner wrappers: the words after them name what runs, so a bare
# suite token behind one wires (``run-suite test ./test_x.sh``). Any
# other unknown command does not wire.
WRAPPER_COMMANDS = frozenset({
    "check", "run-suite", "info",
})
# ``python3 -m`` modules that execute their file/stem arguments.
# Any other module (``mypy``, ``pyflakes``, ``black`` is not ``-m``,
# ``semgrep`` ...) inspects or stages its arguments, never runs them.
RUNNER_MODULES = frozenset({"unittest", "pytest"})
# Declaration builtins: assigning the loop variable (or anything else)
# through them does not execute it.
DECLARATION_COMMANDS = frozenset({
    "export", "local", "declare", "readonly", "typeset",
})
PYTHON_COMMANDS = frozenset({"python", "python3"})
SHELL_COMMANDS = frozenset({"bash", "sh", "dash", "zsh", "ash"})
ASSIGN_TOKEN_RE = re.compile(r"^[A-Za-z_]\w*\+?=")
REDIRECT_TOKEN_RE = re.compile(r"^\d*(?:>>?|<<<|<<|<&|>&|&>)(.*)$")
NOISE_TOKENS = frozenset({
    "then", "do", "else", "elif", "fi", "if", "while", "until",
    "done", "esac", "{", "}", "(", ")", "!", "coproc",
})


def _command_name(token_text):
    """Command word of a token: last path component, wrapper-free."""
    name = token_text.lstrip("({").rstrip(")")
    name = _PATH_PREFIX_RE.sub("", name)
    return name.rsplit("/", 1)[-1]


# Interpreter flags that take a separate argument (``bash -o pipefail``,
# ``python3 -W error``): the argument is an option value, never the
# script. Flags that take no argument (``-O``, ``-p``) are not listed.
FLAG_WITH_ARG = frozenset({"-o", "-W", "-X", "-m", "-c"})
# ``node --test`` flags that take a separate value
# (``--test-name-pattern '<re>'``): the value is a test-name regex or a
# reporter setting, never a test target. Attached ``--flag=value`` forms
# need no entry: they are skipped as one flag word.
NODE_TEST_OPTARG = frozenset({
    "--test-reporter", "--test-name-pattern", "--test-skip-pattern",
    "--test-concurrency", "--test-shard", "--test-reporter-destination",
    "--test-coverage-lines", "--test-coverage-functions",
    "--test-coverage-branches", "--test-coverage-statements",
})
# ``vitest run`` flags that take a separate value (``--dir tests``): the
# value scopes the run, it is not a file filter.
VITEST_OPTARG = frozenset({
    "--dir", "--root", "--project", "--config", "-c", "-r",
    "--reporter", "--testNamePattern", "-t", "--coverage.reportsDirectory",
})
# ``node --test`` default file matching for bare directory targets (and
# a bare ``node --test`` with no targets): ``*.spec.*`` files are
# outside the runner's defaults, so they need an explicit file mention,
# a covering glob, ``vitest run`` coverage or an exemption.
NODE_TEST_DEFAULT_EXTS = (".test.mjs", ".test.cjs", ".test.js")


def _first_nonflag(tokens, index):
    """Index of the first token at/after ``index`` that is not a flag."""
    while index < len(tokens):
        text = tokens[index][0]
        if text == "--":
            index += 1
            return index if index < len(tokens) else None
        if text in FLAG_WITH_ARG:
            index += 2
            continue
        if text.startswith("-") and text != "-":
            index += 1
            continue
        return index
    return None


def _is_c_cluster(text):
    """Whether a token is a bundled short-flag cluster containing ``c``."""
    return (re.fullmatch(r"-[A-Za-z]{2,}", text or "") is not None
            and "c" in text)


def _shell_c_index(texts):
    """Index of the ``-c`` (or ``-<flags>c``) token, or None.

    Only flags and their arguments may precede it: a script word first
    means ``-c`` belongs to the script's own argv, not the interpreter.
    """
    i = 0
    while i < len(texts):
        text = texts[i]
        if text == "-c" or _is_c_cluster(text):
            return i
        if text == "--":
            return None
        if text == "-o" or re.fullmatch(r"-o.+", text or ""):
            i += 2 if text == "-o" else 1
            continue
        if text.startswith("-") and text != "-":
            i += 1
            continue
        return None
    return None


def _argv_word(text):
    """One argv word with quote/comma/paren wrapping removed."""
    word = (text or "").strip().rstrip(",;)]")
    if len(word) >= 2 and word[0] == word[-1] \
            and word[0] in ("'", '"'):
        word = word[1:-1]
    return word.strip()


def _interpreter_wires(command, tokens, cmd_index, match_index, matched=""):
    """Whether an interpreter invocation runs the matched token.

    ``python3 -m unittest <stems>`` wires the stems; ``-m py_compile``,
    ``-c`` inline code, ``bash -n`` and ``node --check`` never run the
    file. Otherwise only the first non-flag argument (the script) is
    executed; later arguments are its argv. ``bash -c '<code>'`` is
    judged by recursing into the code string.
    """
    args = tokens[cmd_index + 1:]
    texts = [text for text, _, _ in args]
    if command in PYTHON_COMMANDS or command.startswith("python3."):
        if "-c" in texts:
            return False
        if "-m" in texts:
            module_index = cmd_index + 1 + texts.index("-m") + 1
            if module_index >= len(tokens):
                return False
            if _bare_value(tokens[module_index][0]) not in RUNNER_MODULES:
                # ``-m mypy``/``-m pyflakes``/``-m py_compile`` inspect
                # their arguments; only a test runner executes them.
                return False
            return match_index > module_index
        script = _first_nonflag(tokens, cmd_index + 1)
        return script is not None and match_index == script
    if command in SHELL_COMMANDS:
        if "-n" in texts:
            return False
        c_at = _shell_c_index(texts)
        if c_at is not None:
            code_index = cmd_index + 1 + c_at + 1
            if code_index >= len(tokens) or match_index != code_index:
                return False
            code = tokens[code_index][0]
            for text, start, _end in _shell_tokens(code):
                if _is_bare_token(text, matched):
                    at = code.find(matched, start)
                    if at == -1:
                        at = start
                    return match_wires(
                        code, at, at + len(matched), python=False)
            return False
        if "-s" in texts:
            return False
        script = _first_nonflag(tokens, cmd_index + 1)
        return script is not None and match_index == script
    if command == "node":
        if "--check" in texts or "-e" in texts or "--eval" in texts:
            return False
        flag_zone = []
        for text in texts:
            # ``--test`` counts only in flag position (before any script
            # word): ``node script.js --test`` passes ``--test`` to the
            # script, it does not run the test runner.
            if text == "--" or (text and not text.startswith("-")):
                break
            flag_zone.append(text)
        if "--test" in flag_zone:
            # ``node --test`` runs every target past the flag, not just
            # the first: each non-flag word after it is a test target
            # (a file, a glob or a directory), except option values
            # (``--test-name-pattern '<re>'`` takes its regex, which is
            # a test-name filter, not a file). Globs and directories
            # are expanded by the js-coverage pass; here only an exact
            # file mention wires.
            test_at = texts.index("--test")
            rel = match_index - (cmd_index + 1)
            if rel <= test_at or texts[rel].startswith("-"):
                return False
            i = test_at + 1
            while i < len(texts):
                if texts[i] in NODE_TEST_OPTARG:
                    if rel == i + 1:
                        return False
                    i += 2
                    continue
                i += 1
            return True
        script = _first_nonflag(tokens, cmd_index + 1)
        return script is not None and match_index == script
    return False


def _walk_command(tokens, match_index):
    """Walk a segment's prelude to its command word.

    Returns ``(kind, index)``: ``"assign"`` for a pure assignment
    statement, ``"for"`` for a ``for VAR in ...`` header, ``"command"``
    for the command-word index, or ``(None, None)`` when no command
    word applies (array manifests, missing words).
    """
    index = 0
    # Strip leading noise: keywords, case patterns with a following
    # command, redirections and env assignments.
    while index < len(tokens):
        text = tokens[index][0]
        if text in NOISE_TOKENS or LEADING_KEYWORD_RE.fullmatch(text):
            index += 1
            continue
        if (CASE_PATTERN_RE.match(text) and text.endswith(")")
                and index < match_index):
            index += 1
            continue
        redirect = REDIRECT_TOKEN_RE.match(text)
        if redirect:
            index += 1 if redirect.group(1) else 2
            continue
        if ASSIGN_TOKEN_RE.match(text) and index < match_index:
            if "=(" in text:
                # Array manifest (``files=( ... )``): its elements are
                # data for a later command, never executions.
                return None, None
            index += 1
            continue
        break
    if index >= len(tokens):
        return None, None
    first = tokens[index][0]
    if ASSIGN_TOKEN_RE.match(first):
        # Pure assignment statement: the caller judges the value.
        if "=(" in first or "+=(" in first:
            return None, None
        return "assign", index
    if first == "for":
        if any(text == "in" for text, _, _ in tokens[index:match_index]):
            return "for", index
        return None, None
    # Walk launchers (env/timeout/sudo/xargs/time/uv ...) to the command.
    while index < len(tokens):
        command = _command_name(tokens[index][0])
        if command not in LAUNCHER_COMMANDS:
            break
        launcher = command
        index += 1
        while index < len(tokens) and (
                tokens[index][0].startswith("-")
                or ASSIGN_TOKEN_RE.match(tokens[index][0])
                or (launcher == "timeout"
                    and re.fullmatch(r"[\d.]+[smhd]?", tokens[index][0]))):
            if tokens[index][0] in LAUNCHER_OPTARG.get(launcher, ()) \
                    and index + 1 < len(tokens):
                # ``env -u NAME``/``nice -n N``/``sudo -u USER``: the
                # next word is the option's value, not the command.
                index += 2
                continue
            index += 1
        if launcher == "uv" and index < len(tokens) \
                and tokens[index][0] == "run":
            # ``uv run [flags] <program>``: skip the subcommand and
            # its flags to the program (``uv run python ...``).
            index += 1
            while index < len(tokens) and (
                    tokens[index][0].startswith("-")
                    or ASSIGN_TOKEN_RE.match(tokens[index][0])):
                if tokens[index][0] in UV_RUN_OPTARG \
                        and index + 1 < len(tokens):
                    index += 2
                    continue
                index += 1
    if index >= len(tokens) or index > match_index:
        return None, None
    return "command", index


def _in_code(text, pos):
    """Whether ``pos`` sits in code (not a quote span or ``#`` comment)."""
    quote = None
    i = 0
    while i < pos:
        char = text[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        i += 1
    if quote is not None:
        return False
    line_start = text.rfind("\n", 0, pos) + 1
    cut = _strip_inline_comment(text[line_start:pos + 1])
    return len(cut) > pos - line_start


def _loop_body(after):
    """Body of the ``for``/``while`` loop opened in ``after``.

    From the first ``do`` to its matching ``done`` (nesting-aware), so a
    multi-line loop body judges with its header. Keyword matches inside
    quotes/comments or glued to ``)`` (case patterns) never count.
    """
    start = None
    for cand in re.finditer(r"(?<![\w-])do(?![\w-])", after or ""):
        if _in_code(after, cand.start()):
            start = cand.end()
            break
    if start is None:
        return ""
    depth = 1
    for kw in re.finditer(
            r"(?<![\w-])(for|while|until|select|done)(?![\w-])",
            after[start:]):
        if after[start + kw.end():start + kw.end() + 1] == ")":
            continue
        if not _in_code(after, start + kw.start()):
            continue
        if kw.group(1) == "done":
            depth -= 1
            if depth == 0:
                return after[start:start + kw.start()]
        else:
            depth += 1
    return after[start:]


def _func_body_runs_arg(body):
    """Whether a shell function body executes its ``$1``/``$@`` arguments."""
    for segment, _offset in split_segments(body or ""):
        tokens = _shell_tokens(segment)
        for pos, (text, _start, _end) in enumerate(tokens):
            if text not in ("$1", "${1}", "$@", "${@}", "$*", "${*}"):
                continue
            kind, index = _walk_command(tokens, pos)
            if kind != "command":
                continue
            if pos == index:
                return True
            if _command_runs_arg(
                    _command_name(tokens[index][0]), tokens, index, pos):
                return True
    return False


def _function_bodies(text):
    """Shell function definitions in text: ``{name: body}``.

    Both ``name() { ... }`` and ``function name { ... }`` forms, one-line
    or multi-line (brace matching is quote-aware). A call wires a suite
    argument only when the body executes its arguments (see
    ``_func_body_runs_arg``); anything else never wires.
    """
    bodies = {}
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        match = re.match(
            r"\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)\s*(\{)?(.*)$",
            lines[i])
        func = re.match(
            r"\s*function\s+([A-Za-z_]\w*)\s*(\{)?(.*)$", lines[i])
        name, brace, rest = None, None, ""
        if match is not None:
            name, brace, rest = match.group(1), match.group(2), match.group(3)
        elif func is not None:
            name, brace, rest = func.group(1), func.group(2), func.group(3)
        if name is None or not brace:
            i += 1
            continue
        chunk = rest + "\n" + "\n".join(lines[i + 1:])
        depth = 1
        quote = None
        p = 0
        stop = None
        while p < len(chunk):
            char = chunk[p]
            if quote is not None:
                if char == "\\":
                    p += 2
                    continue
                if char == quote:
                    quote = None
                p += 1
                continue
            if char in ("'", '"'):
                quote = char
                p += 1
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    stop = p
                    break
            p += 1
        if stop is None:
            bodies[name] = chunk
            i = len(lines)
        else:
            bodies[name] = chunk[:stop]
            i += 1 + chunk[:stop].count("\n")
    return bodies


def _command_runs_arg(command, tokens, index, pos, matched="", funcs=None):
    """Whether ``command`` executes the suite token at ``pos``.

    Closed default shared by direct mentions and loop/function variable
    uses: interpreters run their script word, ``pytest``/``coverage run``/
    ``source`` run their file arguments, a ``$VAR`` command word is an
    indirection that may run anything, a known wrapper names what runs,
    and a shell function that forwards ``$1``/``$@`` runs its suite
    arguments. Anything else -- inspectors, stagers, linters, unknown
    tools -- never wires.
    """
    if (command in PYTHON_COMMANDS or command.startswith("python3.")
            or command in SHELL_COMMANDS or command == "node"):
        return _interpreter_wires(command, tokens, index, pos, matched)
    if command in NON_RUNNER_COMMANDS or command in DECLARATION_COMMANDS:
        return False
    if DIE_MSG_RE.match(tokens[index][0]) and command not in (
            "log", "info", "debug", "usage", "warn", "warning",
            "err", "error"):
        return False
    if command in WRAPPER_COMMANDS:
        return True
    if command == "pytest":
        # ``pytest -q path`` executes its file arguments.
        return True
    if command in ("source", "."):
        # Sourcing executes the file in the current shell.
        return True
    if command == "coverage":
        # Only ``coverage run`` executes; ``report``/``xml`` inspect.
        nxt = _first_nonflag(tokens, index + 1)
        return (nxt is not None
                and _command_name(tokens[nxt][0]) == "run")
    if tokens[index][0].strip("'\"").startswith("$"):
        # A variable command word (``"$PY"``, ``$SHELL``) is an
        # indirection that may run anything.
        return True
    if funcs and command in funcs \
            and _func_body_runs_arg(funcs[command]):
        return True
    return False


def _judge_var_use(tokens, pos, names, funcs=None):
    """Whether the ``$var`` token at ``pos`` is executed in its segment."""
    if tokens[pos][0] not in names:
        return False
    kind, index = _walk_command(tokens, pos)
    if kind != "command":
        return False
    if pos == index:
        return True
    return _command_runs_arg(
        _command_name(tokens[index][0]), tokens, index, pos,
        funcs=funcs)


def _loop_var_runs(after, var, funcs=None):
    """Whether ``$var`` is executed (not merely inspected) in the loop body.

    A ``for VAR in <suites>`` header wires its suites only when the loop
    body runs the variable: ``bash "$f"`` executes it,
    ``shellcheck "$f"`` merely inspects it. The body runs from ``do`` to
    its matching ``done``, so multi-line loops judge with their header.
    """
    if not var:
        return False
    names = {"$" + var, "${" + var + "}"}
    body = _loop_body(after)
    if not body:
        return False
    for segment, _offset in split_segments(body):
        tokens = _shell_tokens(segment)
        for pos in range(len(tokens)):
            if _judge_var_use(tokens, pos, names, funcs=funcs):
                return True
    return False


def _var_use_runs(after, var, funcs=None):
    """Whether ``$var`` is executed after its scalar assignment.

    A bare suite-path assignment (``SUITE="$ROOT/test_x.sh"``) wires
    only when a later reference to the variable runs in command
    position: ``bash "$SUITE"`` executes it, ``shellcheck "$SUITE"``
    merely inspects it, and an unused assignment wires nothing. A
    reassignment first cuts the flow: only uses before it count.
    """
    if not var:
        return False
    names = {"$" + var, "${" + var + "}"}
    reassign = re.search(
        r"(?<![\w])" + re.escape(var) + r"\s*\+?=", after or "")
    bound = reassign.start() if reassign is not None else None
    for segment, offset in split_segments(after or ""):
        if bound is not None and offset >= bound:
            break
        tokens = _shell_tokens(segment)
        for pos in range(len(tokens)):
            if _judge_var_use(tokens, pos, names, funcs=funcs):
                return True
    return False


def _for_header_wires(tokens, after, funcs=None):
    """Whether a ``for VAR in ...`` header segment executes its suites."""
    if len(tokens) < 2:
        return False
    return _loop_var_runs(after, tokens[1][0], funcs=funcs)


def _substitution_inner(token_text, rel_pos):
    """Inner text of the ``$(...)``/backtick span containing ``rel_pos``.

    Command substitution always executes when its line runs
    (``out="$(./test_x.sh)"`` runs the suite), so a suite named
    inside one is judged by recursing into the inner text. Returns
    None when the position sits outside any substitution span (for
    example after it, as in ``"$(dirname "$0")/test_x.py"``).
    """
    for match in re.finditer(r"`([^`]*)`", token_text or ""):
        if match.start() < rel_pos < match.end():
            return match.group(1)
    opens = [match.start() for match in re.finditer(r"\$\(",
                                                    token_text or "")
             if match.start() < rel_pos]
    for open_at in reversed(opens):
        depth = 1
        for pos in range(open_at + 2, len(token_text)):
            if token_text[pos] == "(":
                depth += 1
            elif token_text[pos] == ")":
                depth -= 1
                if depth == 0:
                    if open_at + 2 <= rel_pos < pos:
                        return token_text[open_at + 2:pos]
                    break
    return None


def _call_span(segment, open_index):
    """Index just past the call closing ``segment[open_index] == '('``.

    Quote-aware (backslash-aware); None when the call never closes.
    """
    quote = None
    depth = 0
    i = open_index
    n = len(segment)
    while i < n:
        char = segment[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _string_literal(segment, quote_index):
    """``(content, end)`` of the string literal opening at ``quote_index``.

    Handles triple-quoted literals; ``(None, None)`` when unterminated.
    """
    quote = segment[quote_index]
    triple = segment[quote_index:quote_index + 3] == quote * 3
    opener = 3 if triple else 1
    i = quote_index + opener
    n = len(segment)
    while i < n:
        char = segment[i]
        if char == "\\":
            i += 2
            continue
        if triple and segment[i:i + 3] == quote * 3:
            return segment[quote_index + opener:i], i + 3
        if not triple and char == quote:
            return segment[quote_index + opener:i], i + 1
        i += 1
    return None, None


def _argv_list_wires(inner, base, rel_start, matched):
    """Whether the match inside a ``[...]`` argv list is executed.

    The list wires only when its first element is an interpreter (or
    ``sys.executable``) and that interpreter would run the matched word
    with its own flag rules: ``["bash", "-n", suite]`` syntax-checks
    and ``["python3", "-m", "mypy", ...]`` type-checks their arguments,
    neither runs them.
    """
    words = []
    pos = 0
    for part in inner.split(","):
        word = _argv_word(part)
        if word:
            words.append((word, pos, pos + len(part)))
        pos += len(part) + 1
    if not words:
        return False
    first = _bare_value(words[0][0])
    if first == "sys.executable":
        first = "python3"
    if not (first in PYTHON_COMMANDS or first.startswith("python3.")
            or first in SHELL_COMMANDS or first == "node"):
        return first == _bare_value(matched)
    at = rel_start - base
    mpos = None
    for k, (_word, start, end) in enumerate(words):
        if start <= at < end:
            mpos = k
            break
    if mpos is None:
        return False
    pseudo = [(word, start, end) for word, start, end in words]
    return _interpreter_wires(first, pseudo, 0, mpos, matched)


def _py_runner_wires(segment, tokens, match_index, matched,
                     rel_start, rel_end):
    """Whether the match in a ``.py`` segment is really executed.

    Only a runner call wires, and then only the argument it executes.
    ``os.system("...")`` and string-form ``subprocess`` calls execute
    their string, so a match inside one is judged with the shell
    grammar (``os.system("shellcheck ./test_x.sh")`` inspects, it does
    not run). A ``subprocess``-style argv list wires only when its
    first element is an interpreter (or ``sys.executable``) that would
    run the matched word. ``runpy``/``loadTestsFrom*`` execute their
    argument by definition; any other mention (help strings, argparse
    text, ``sys.exit`` messages, data tuples, a method call on a
    result) never wires.
    """
    for call in PY_RUNNER_RE.finditer(segment):
        open_at = segment.find("(", call.start())
        if open_at == -1:
            continue
        close_at = _call_span(segment, open_at)
        if close_at is None:
            continue
        if not (call.start() <= rel_start < close_at):
            continue
        name = re.match(r"[\w.]+", segment[call.start():])
        short = name.group(0) if name else ""
        if "runpy" in short or "loadTestsFrom" in short:
            return True
        arg_at = open_at + 1
        while arg_at < len(segment) and segment[arg_at] in " \t":
            arg_at += 1
        if arg_at < len(segment) and segment[arg_at] in ("'", '"'):
            # String form (``os.system("...")``,
            # ``subprocess.run("...", shell=True)``): judge the string
            # itself with the shell grammar.
            literal, lend = _string_literal(segment, arg_at)
            if literal is None or not (arg_at < rel_start < lend):
                return False
            quote = segment[arg_at]
            opener = (3 if segment[arg_at:arg_at + 3] == quote * 3 else 1)
            at = rel_start - (arg_at + opener)
            return match_wires(literal, at, at + len(matched),
                               python=False)
        if arg_at < len(segment) and segment[arg_at] == "[":
            # List form: judged by the argv-list rule below.
            break
        return False
    if not _is_bare_token(tokens[match_index][0], matched):
        return False
    for match in re.finditer(r"\[([^\[\]]*)\]", segment):
        if match.start() <= rel_start < match.end():
            return _argv_list_wires(match.group(1), match.start() + 1,
                                    rel_start, matched)
    return False


def _segment_wires_match(segment, rel_start, rel_end, python=False,
                         after="", funcs=None):
    """Whether the match inside one command segment is executed.

    Command position only: the matched token must be a bare suite
    name/path (never a word inside a sentence) and must sit where a
    command runs it -- as the command itself, as an interpreter's
    script or ``-m unittest``/``-m pytest`` argument, as a bare
    ``pytest``/``coverage run``/``source``/``$VAR``-command argument, in
    a ``for VAR in ...`` list whose body (through its matching ``done``,
    even on later lines) executes the loop variable, as the bare-path
    value of a scalar assignment whose variable later runs in command
    position (``SUITE="$ROOT/test_x.sh"`` then ``bash "$SUITE"``), as an
    argument of a shell function that forwards ``$1``/``$@`` to a
    runner, or behind a known wrapper (``check``/``run-suite``/``info``)
    whose words name what runs. Any other command -- inspectors,
    stagers, linters, unknown tools -- never wires: the default is
    closed. Array manifests (``files=( ... )``) and parse checks
    (``ast.parse``/``py_compile``) never wire. In a ``.py`` segment only
    a real runner call (``subprocess``/``os.system``/``runpy``) wires: a
    string-form call only when the judged string runs the suite, and a
    ``subprocess``-style argv list only when its first element is an
    interpreter (or ``sys.executable``) that would run the matched word,
    so help strings, argparse text, ``sys.exit`` messages and data
    tuples never do.
    """
    matched = segment[rel_start:rel_end]
    if python:
        if not PY_RUNNER_RE.search(segment):
            return False
    elif "ast.parse" in segment or "py_compile" in segment:
        return False
    tokens = _shell_tokens(segment)
    match_index = None
    for pos, (_, start, end) in enumerate(tokens):
        if start <= rel_start < end:
            match_index = pos
            break
    if match_index is None:
        return False
    if not python and match_index >= 2 \
            and (tokens[match_index - 1][0] == "-c"
                 or _is_c_cluster(tokens[match_index - 1][0])) \
            and _command_name(tokens[match_index - 2][0]) in SHELL_COMMANDS:
        # ``bash -c '<code>'`` (or ``-lc``/``-ec`` clusters) anywhere
        # in the segment (e.g. under a ``check NAME ...`` wrapper):
        # judge the code string itself.
        code = tokens[match_index][0]
        at = code.find(matched)
        if at == -1:
            return False
        return match_wires(code, at, at + len(matched), python=False)
    if not python:
        # ``$(...)``/backticks anywhere in the segment (e.g. an
        # ``out="$(...)"`` capture): substitution bodies execute when
        # the line runs, so judge the inner text itself.
        token_text, token_start, _token_end = tokens[match_index]
        inner = _substitution_inner(token_text, rel_start - token_start)
        if inner is not None:
            at = inner.find(matched)
            if at == -1:
                return False
            return match_wires(inner, at, at + len(matched),
                               python=False)
    if python:
        # ``.py`` matches usually sit glued inside a larger token (a
        # string literal or argv list), so bare-token judging lives in
        # ``_py_runner_wires``, not here.
        return _py_runner_wires(segment, tokens, match_index, matched,
                                rel_start, rel_end)
    if not _is_bare_token(tokens[match_index][0], matched):
        return False
    if tokens and tokens[0][0].lstrip("(") == "for":
        # ``for`` is a shell keyword the prelude walk skips, so a
        # loop header is judged here: only a body that executes the
        # loop variable wires the listed suites.
        return _for_header_wires(tokens, after, funcs=funcs)
    kind, index = _walk_command(tokens, match_index)
    if kind is None:
        return False
    if kind == "assign":
        # Pure assignment statement: a scalar bare-path value wires only
        # when the variable later runs in command position (the
        # ``SUITE="$ROOT/test_x.sh"`` idiom); array manifests and
        # sentence values never do (bare-token check above already
        # rejected sentences).
        if not any(ASSIGN_TOKEN_RE.match(text)
                   for text, _, _ in tokens[index:match_index + 1]):
            return False
        var = tokens[index][0].split("=", 1)[0].rstrip("+")
        return _var_use_runs(after, var, funcs=funcs)
    if kind == "for":
        return _for_header_wires(tokens, after, funcs=funcs)
    if match_index == index:
        return True
    return _command_runs_arg(
        _command_name(tokens[index][0]), tokens, index, match_index,
        matched, funcs=funcs)


def match_wires(line, start, end, python=False, tail="", funcs=None):
    """Whether a suite/token match at ``(start, end)`` counts as wiring.

    Case-arm patterns never count; otherwise the match wires exactly
    when its own command segment executes it in command position
    (see ``_segment_wires_match``). ``tail`` carries the text after the
    line within the same corpus block, so multi-line loop bodies and
    later variable uses judge with the match; ``funcs`` maps shell
    function names to bodies for call-site judging.
    """
    if is_case_arm(line, start, end):
        return False
    for segment, offset in split_segments(line):
        if offset <= start < offset + len(segment):
            return _segment_wires_match(
                segment, start - offset, end - offset, python=python,
                after=line[offset + len(segment):] + (tail or ""),
                funcs=funcs)
    return False


def script_index(tracked):
    index = {}
    for entry in tracked:
        if entry.endswith(SCRIPT_EXTS):
            index.setdefault(Path(entry).name, []).append(entry)
    return index


def find_script_refs(text, index, python=False):
    """Tracked scripts referenced by filename/path token.

    Case arms never count, and neither do non-executing mentions: a
    script named only outside command position (``ast.parse``/
    ``py_compile``/``bash -n``/``chmod``/``cp``/message text, or a
    string literal in a ``.py`` data tuple) is staged, inspected or
    described, not executed, so following it would mine usage text as
    wiring. A script chained after the check on the same line is
    followed. Hops through a variable prefix (``bash "$ROOT/x.sh"``,
    ``"${ROOT}/x.sh"``) are followed: the ``$VAR/`` prefix is not part
    of the tracked path, so it is accepted and stripped before matching.
    """
    refs = set()
    funcs = None if python else _function_bodies(text)
    for match in re.finditer(
            r"(?<![\w\-])(?:\$\{?\w+\}?/|\./|/)?"
            r"([\w.\-/]+\.(?:sh|py|mjs|cjs|js))"
            r"(?![\w.\-])", text):
        token = match.group(1)
        base = token.rsplit("/", 1)[-1]
        for candidate in index.get(base, []):
            if candidate.endswith(token) or candidate == token.lstrip("./"):
                line_start = text.rfind("\n", 0, match.start(1)) + 1
                line_end = text.find("\n", match.end(1))
                line = text[line_start:line_end if line_end != -1 else None]
                start = match.start(1) - line_start
                tail = text[line_end + 1:] if line_end != -1 else ""
                if match_wires(line, start, start + len(token),
                               python=python, tail=tail, funcs=funcs):
                    refs.add(candidate)
    return refs


def logical_text(text, python=False):
    """Comment-stripped text with shell continuations joined.

    A suite named on the continuation of a multi-line command belongs to
    that command's logical line: ``ast.parse ... \\`` followed by bare
    filenames must see the ``ast.parse`` marker, or the filenames read as
    execution references. Comments are stripped first so a trailing
    backslash on a comment cannot glue it to the next line.
    """
    return code_text(text, python=python).replace("\\\n", " ")


def build_corpus(root):
    """Return [(source, logical_text)] reachable from CI run steps.

    One entry per run body (not one per workflow): a ``cd`` or loop
    body in one step must not leak into the next step's lines when
    later lines are judged with a match.
    """
    segments = []
    for workflow in sorted((root / ".github/workflows").glob("*.yml")):
        bodies = run_bodies(workflow.read_text())
        for body in bodies:
            segments.append((str(workflow.relative_to(root)),
                             logical_text(body)))
    verify = root / VERIFY_FAST
    segments.append((str(VERIFY_FAST), logical_text(
        parse_tier_text(verify.read_text(), str(VERIFY_FAST)))))
    tracked = tracked_files(root)
    restored = restored_paths(root)
    index = script_index(tracked)
    # The parse-tier seed above is the whole of verify-fast.sh CI runs;
    # pin it so a workflow reference to the script never re-adds the file.
    included = {str(VERIFY_FAST)}
    while True:
        refs = set()
        for source, text in segments:
            refs.update(find_script_refs(
                text, index, python=source.endswith(".py")))
        refs = {ref for ref in refs
                if ref not in included and ref not in TABLE_ONLY_SCRIPTS
                and not is_excluded(ref, restored)}
        if not refs:
            break
        for ref in sorted(refs):
            included.add(ref)
            segments.append((ref, logical_text(
                (root / ref).read_text(), python=ref.endswith(".py"))))
    return segments


def _parse_discover_argv(words):
    """``(directory, pattern)`` from argv words after ``discover``."""
    directory = None
    pattern = None
    positional = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in ("-s", "--start-directory") and i + 1 < len(words):
            directory = words[i + 1]
            i += 2
        elif word in ("-p", "--pattern") and i + 1 < len(words):
            pattern = words[i + 1]
            i += 2
        elif word in ("-t", "--top-level-directory") and i + 1 < len(words):
            i += 2
        elif word.startswith("--start-directory="):
            directory = word.split("=", 1)[1]
            i += 1
        elif word.startswith("--pattern="):
            pattern = word.split("=", 1)[1]
            i += 1
        elif word.startswith("--top-level-directory="):
            i += 1
        elif re.fullmatch(r"-[a-zA-Z]{2,}", word or ""):
            attached = re.match(r"^-(s|p)(.+)$", word)
            if attached is not None:
                if attached.group(1) == "s":
                    directory = attached.group(2)
                else:
                    pattern = attached.group(2)
            i += 1
        elif word == "--":
            positional.extend(words[i + 1:])
            break
        elif word.startswith("-"):
            # ``-v``/``-q``/``-f``/``--verbose`` take no argument.
            i += 1
        else:
            positional.append(word)
            i += 1
    if directory is None and positional:
        directory = positional.pop(0)
    if pattern is None and positional:
        pattern = positional.pop(0)
    return directory, pattern


def _normalize_discover_dir(directory, cd_prefix=None):
    """Suite-relative start directory, ``""`` for the repo root."""
    text = (directory or "").strip()
    if text.startswith("$"):
        # Unresolvable variable: fail closed, record nothing.
        return None
    if cd_prefix and not text.startswith("/"):
        # ``(cd other && ... discover ...)`` (or a ``working-directory:``
        # step) runs under ``other/``: resolve the start directory
        # against it. A missing ``-s`` means ``.``, i.e. the current
        # directory, not the repo root.
        text = cd_prefix if not text else cd_prefix.rstrip("/") + "/" + text
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    if text in ("", "."):
        return ""
    return text


def _list_argv_after(segment, tokens, pos):
    """Argv words after a token inside its ``[...]`` list, else None."""
    start = tokens[pos][1]
    for match in re.finditer(r"\[([^\[\]]*)\]", segment):
        if match.start() <= start < match.end():
            words = [_argv_word(part)
                     for part in match.group(1).split(",")]
            words = [word for word in words if word]
            if not words:
                return None
            first = _bare_value(words[0])
            if first in PYTHON_COMMANDS or first.startswith("python3.") \
                    or first in SHELL_COMMANDS or first == "node":
                at = next((i for i, word in enumerate(words)
                           if word == "discover"), None)
                if at is None:
                    return None
                return words[at + 1:]
            return None
    return None


def _discover_call_argv(tokens, pos, python, segment):
    """Argv words after a ``discover`` token, or None when not a run.

    Shell: the segment command must be python with ``-m unittest``
    before ``discover``. ``.py``: ``discover`` must sit in an argv
    list whose first element is an interpreter.
    """
    if python:
        if PY_RUNNER_RE.search(segment) is None:
            return None
        return _list_argv_after(segment, tokens, pos)
    kind, index = _walk_command(tokens, pos)
    if kind != "command":
        return None
    command = _command_name(tokens[index][0])
    if command not in PYTHON_COMMANDS \
            and not command.startswith("python3."):
        return None
    seen_module = False
    for j in range(index + 1, pos):
        if tokens[j][0] == "-m" and j + 1 < len(tokens):
            if _bare_value(tokens[j + 1][0]) != "unittest":
                return None
            seen_module = True
    if not seen_module:
        return None
    return [_argv_word(text) for text, _, _ in tokens[pos + 1:]]


def _block_discovers(text, python):
    """``(directory, pattern)`` pairs one corpus block really discovers.

    ``cd`` persists across the block's lines (each run body is its own
    block, so it never leaks into the next step); a ``discover`` token
    counts only through the same command-position check as every other
    mention.
    """
    pairs = []
    cwd = None
    for line in text.splitlines():
        for segment, _offset in split_segments(line):
            tokens = _shell_tokens(segment)
            if not tokens:
                continue
            if not python and _command_name(tokens[0][0]) == "cd" \
                    and len(tokens) == 2:
                cwd = _argv_word(tokens[1][0])
                if cwd.startswith("$") or cwd == "-":
                    cwd = None
            for pos, (text, _start, _end) in enumerate(tokens):
                if _argv_word(text) != "discover":
                    continue
                argv = _discover_call_argv(tokens, pos, python, segment)
                if argv is None:
                    continue
                directory, pattern = _parse_discover_argv(argv)
                directory = _normalize_discover_dir(directory, cwd)
                if directory is None:
                    continue
                pairs.append((directory, pattern or "test*.py"))
    return pairs


def discover_patterns(segments):
    """``(directory, pattern)`` pairs the corpus really discovers.

    Collected per command segment through the same command-position
    check as every other mention: a ``discover`` pattern quoted in a
    ``grep`` line, a ``die`` message, an ``assert`` message or an
    assignment wires nothing. Flags parse in any order and either
    quote style, ``-s .`` is the repo root, and a missing ``-s``
    resolves against a preceding ``cd`` or the step's
    ``working-directory``.
    """
    pairs = []
    for source, text in segments:
        python = source.endswith(".py")
        pairs.extend(_block_discovers(text, python))
    return pairs


_JS_RUNNERS = frozenset({"node", "npm", "vitest", "npx"})
_JS_ANCHORS = frozenset({"--test"}) | _JS_RUNNERS


def _runner_targets(words, optargs):
    """Non-flag words, skipping flags and their option values."""
    targets = []
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--":
            targets.extend(w for w in words[i + 1:] if w)
            break
        if word.startswith("-") and word != "-":
            i += 2 if word in optargs and i + 1 < len(words) else 1
            continue
        if word and word != "-":
            targets.append(word)
        i += 1
    return targets


def _parse_js_argv(words):
    """``(kind, payload)`` from argv words, else None.

    ``node --test ...`` -> ``("node-test", targets)``; plain
    ``node <file>`` -> ``("node-file", [script])``; ``vitest run ...``
    -> ``("vitest", filters)``; ``npm test`` -> ``("npm", "test")``;
    ``npm run <name>`` -> ``("npm", name)``. ``node --check`` / ``-e``,
    non-run vitest subcommands and workspace-scoped npm (``--workspace``
    moves the package elsewhere, which this pass does not resolve)
    never parse.
    """
    if not words:
        return None
    head = _bare_value(words[0])
    if head == "npx" and len(words) > 1 \
            and _bare_value(words[1]) == "vitest":
        words = words[1:]
        head = "vitest"
    if head == "node":
        rest = words[1:]
        if "--check" in rest or "-e" in rest or "--eval" in rest:
            return None
        flag_zone = []
        for word in rest:
            if word == "--" or (word and not word.startswith("-")):
                break
            flag_zone.append(word)
        if "--test" in flag_zone:
            after = rest[rest.index("--test") + 1:]
            return ("node-test",
                    _runner_targets(after, NODE_TEST_OPTARG))
        return ("node-file", _runner_targets(rest, set())[:1])
    if head == "vitest":
        rest = words[1:]
        if "run" in rest:
            rest = rest[rest.index("run") + 1:]
        elif "--run" in rest:
            at = rest.index("--run")
            rest = rest[:at] + rest[at + 1:]
        else:
            return None
        return ("vitest", _runner_targets(rest, VITEST_OPTARG))
    if head == "npm":
        if "--workspace" in words or "--workspaces" in words \
                or "-w" in words:
            return None
        rest = [word for word in words[1:]
                if word and not word.startswith("-")]
        if not rest:
            return None
        if rest[0] in ("test", "t"):
            return ("npm", "test")
        if rest[0] in ("run", "run-script") and len(rest) > 1:
            return ("npm", rest[1])
        return None
    return None


def _js_list_argv(segment, tokens, pos):
    """Argv words of the ``[...]`` list containing ``pos``, else None."""
    start = tokens[pos][1]
    for match in re.finditer(r"\[([^\[\]]*)\]", segment):
        if match.start() <= start < match.end():
            words = [_argv_word(part)
                     for part in match.group(1).split(",")]
            return [word for word in words if word]
    return None


def _js_runner_argv(tokens, pos, python, segment):
    """``(kind, payload)`` for a JS-runner anchor at ``pos``, else None.

    Shell segments verify command position through the same prelude
    walk as every other mention (so ``echo npm test`` and
    ``grep -r vitest`` parse nothing); ``.py`` segments read the argv
    list the anchor sits in, and only inside a real runner call.
    """
    if python:
        if PY_RUNNER_RE.search(segment) is None:
            return None
        words = _js_list_argv(segment, tokens, pos)
        if not words or _bare_value(words[0]) not in _JS_RUNNERS:
            return None
        return _parse_js_argv(words)
    kind, index = _walk_command(tokens, pos)
    if kind != "command":
        return None
    if _command_name(tokens[index][0]) not in _JS_RUNNERS:
        return None
    words = [_argv_word(text) for text, _, _ in tokens[index:]]
    return _parse_js_argv(words)


def _block_js_runs(text, python):
    """``(cwd, kind, payload)`` JS test runs in one corpus block.

    ``cd`` persists across the block's lines (each run body is its own
    block, so it never leaks into the next step); a runner counts only
    through the same command-position check as every other mention.
    """
    runs = []
    cwd = None
    for line in text.splitlines():
        for segment, _offset in split_segments(line):
            tokens = _shell_tokens(segment)
            if not tokens:
                continue
            if not python and _command_name(tokens[0][0]) == "cd" \
                    and len(tokens) == 2:
                target = _argv_word(tokens[1][0])
                cwd = None if target.startswith("$") or target == "-" \
                    else target
            for pos, (word, _start, _end) in enumerate(tokens):
                bare = _argv_word(word)
                if bare not in _JS_ANCHORS \
                        and _command_name(bare) not in _JS_RUNNERS:
                    continue
                parsed = _js_runner_argv(tokens, pos, python, segment)
                if parsed is None:
                    continue
                # One run per segment: without this the same invocation
                # would report once per anchor (``node`` and ``--test``).
                runs.append((cwd, parsed[0], parsed[1]))
                break
    return runs


def _join_target(directory, target):
    """Root-relative path for a runner target, else None.

    Leading ``./`` is stripped; absolute paths, variable paths and
    anything escaping the repo root fail closed.
    """
    text = (target or "").strip().strip("'\"")
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or text.startswith("$"):
        return None
    joined = (directory + "/" + text).strip("/") if directory else text
    parts = []
    for part in joined.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _glob_match(path, pattern):
    """Whether a path matches a glob with shell directory rules.

    ``*``/``?``/``[...]`` never cross ``/`` (plain ``fnmatch`` would
    let ``test/*.test.mjs`` wire nested suites the shell never
    expands); only an explicit ``**`` segment crosses directories.
    """
    pparts = path.split("/")
    sparts = pattern.split("/")

    def rec(pi, si):
        if si == len(sparts):
            return pi == len(pparts)
        if sparts[si] == "**":
            return any(rec(k, si + 1)
                       for k in range(pi, len(pparts) + 1))
        if pi == len(pparts):
            return False
        if not fnmatch.fnmatchcase(pparts[pi], sparts[si]):
            return False
        return rec(pi + 1, si + 1)

    return rec(0, 0)


def _target_suites(joined, suites, tracked):
    """Suites covered by one joined ``node --test`` target.

    An exact file wires itself; a glob expands against tracked suites
    with shell directory rules; a bare directory covers
    ``*.test.{mjs,cjs,js}`` suites beneath it (the runner's default
    file matching, not specs). Anything else wires nothing.
    """
    if any(char in joined for char in "*?["):
        return {s for s in suites if _glob_match(s, joined)}
    if joined in suites:
        return {joined}
    if any(t.startswith(joined + "/") for t in tracked):
        prefix = joined + "/"
        return {s for s in suites
                if s.startswith(prefix)
                and s.endswith(NODE_TEST_DEFAULT_EXTS)}
    return set()


def _vitest_suites(directory, filters, suites):
    """Suites a ``vitest run`` covers under ``directory``.

    With no file filters every tracked ``*.test.*`` / ``*.spec.*``
    suite under the package wires; with filters, only suites matching
    a filter by exact path, glob or (for a bare word) basename.
    """
    if directory:
        prefix = directory.rstrip("/") + "/"
        owned = {s for s in suites if s.startswith(prefix)}
    else:
        owned = set(suites)
    owned = {s for s in owned
             if Path(s).name.endswith(SPEC_SUFFIXES)}
    if not filters:
        return owned
    wired = set()
    for fil in filters:
        joined = _join_target(directory, fil)
        if joined is None:
            continue
        if any(char in joined for char in "*?["):
            wired.update(s for s in owned if _glob_match(s, joined))
        elif joined in owned:
            wired.add(joined)
        elif "/" not in fil:
            wired.update(s for s in owned if Path(s).name == fil)
    return wired


def _resolve_package_script(root, directory, name):
    """Text of ``scripts[name]`` in the package.json under ``directory``.

    None when the file is missing, unparseable, or names no such
    script: an unresolvable package fails closed and wires nothing.
    """
    pkg = root / directory / "package.json" if directory \
        else root / "package.json"
    try:
        data = json.loads(pkg.read_text())
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return None
    script = scripts.get(name)
    return script if isinstance(script, str) else None


def _valid_dir(cwd):
    """Root-relative directory for a run cwd, else None (fail closed)."""
    directory = _normalize_discover_dir(cwd or "", None)
    if directory is None or directory.startswith("/") \
            or ".." in Path(directory).parts:
        return None
    return directory


def js_wired_suites(root, segments, suites):
    """Tracked JS/TS suites run via globs, dirs, npm scripts or vitest.

    Exact file mentions wire through the token machinery in
    ``is_wired``; this pass covers what text matching cannot: ``node
    --test`` globs and bare directories, ``package.json`` script
    resolution behind ``npm test`` / ``npm run <script>`` (nested
    ``npm run`` inside a script resolves too, cycle-guarded), and
    ``vitest run`` coverage. Unresolvable packages, variable
    directories and unknown runners wire nothing (fail closed).
    """
    root = Path(root)
    suite_set = set(suites)
    tracked = set(tracked_files(root))
    wired = set()
    queue = []
    for source, text in segments:
        queue.extend((cwd, kind, payload) for cwd, kind, payload
                     in _block_js_runs(text, source.endswith(".py")))
    seen_npm = set()
    while queue:
        cwd, kind, payload = queue.pop(0)
        directory = _valid_dir(cwd)
        if directory is None:
            continue
        if kind == "node-test":
            if not payload:
                # Bare ``node --test`` runs the current directory
                # (recursively, under default file matching).
                if directory:
                    wired.update(_target_suites(
                        directory, suite_set, tracked))
                else:
                    wired.update(
                        s for s in suite_set
                        if s.endswith(NODE_TEST_DEFAULT_EXTS))
                continue
            for target in payload:
                joined = _join_target(directory, target)
                if joined is not None:
                    wired.update(
                        _target_suites(joined, suite_set, tracked))
        elif kind == "vitest":
            wired.update(_vitest_suites(directory, payload, suite_set))
        elif kind == "node-file":
            for target in payload:
                joined = _join_target(directory, target)
                if joined is not None and joined in suite_set:
                    wired.add(joined)
        elif kind == "npm":
            key = (directory, payload)
            if key in seen_npm:
                continue
            seen_npm.add(key)
            script = _resolve_package_script(root, directory, payload)
            if script is None:
                continue
            cleaned = "\n".join(_strip_inline_comment(line)
                                for line in script.splitlines())
            for sub_cwd, sub_kind, sub_payload in _block_js_runs(
                    cleaned, python=False):
                if sub_cwd is None:
                    queue.append((directory or None, sub_kind,
                                  sub_payload))
                else:
                    joined_dir = _join_target(directory, sub_cwd)
                    if joined_dir is not None:
                        queue.append((joined_dir, sub_kind,
                                      sub_payload))
    return wired & suite_set


def is_wired(suite, segments, discovers, counts, tracked=frozenset()):
    """Whether any corpus segment executes the suite.

    ``counts`` is a (basename_counts, stem_counts) pair over tracked
    suites. A bare basename or stem mention wires the suite only when no
    other tracked suite shares that name; otherwise a path-qualified
    reference or a directory-scoped discover pattern is required.
    ``tracked`` is the full tracked-file set: ``unittest discover``
    only collects ``.py`` modules and recurses only into tracked
    packages (directories holding ``__init__.py``), so a discover
    pattern wires a nested suite only under a package chain, and a
    root (or unscoped) pattern wires root-level suites only.
    """
    basename_counts, stem_counts = counts
    basename = Path(suite).name
    stem = Path(suite).stem
    unique_basename = basename_counts.get(basename, 0) == 1
    unique_stem = stem_counts.get(stem, 0) == 1
    tracked_set = set(tracked)
    for source, text in segments:
        if source == suite:
            continue
        python = source.endswith(".py")
        funcs = None if python else _function_bodies(text)
        if "/" in suite:
            # Complete-path match only: a longer path ending in the
            # suite path (``mcp/test/test_x.py`` for ``test/test_x.py``)
            # or a longer filename must not wire this suite. Only a
            # ``./`` or ``$VAR/`` hop prefix may precede it.
            for match in re.finditer(
                    r"(?<![\w.\-/])(?:\$\{?\w+\}?/|\./)?("
                    + re.escape(suite) + r")(?![\w.\-])", text):
                line_start = text.rfind("\n", 0, match.start(1)) + 1
                line_end = text.find("\n", match.end(1))
                line = text[line_start:line_end if line_end != -1 else None]
                start = match.start(1) - line_start
                tail = text[line_end + 1:] if line_end != -1 else ""
                if match_wires(line, start, start + len(suite),
                               python=python, tail=tail, funcs=funcs):
                    return True
        if not unique_basename and not unique_stem:
            continue
        if unique_basename:
            for match in re.finditer(
                    r"(?<![\w\-])(?:\./)?" + re.escape(basename)
                    + r"(?![\w.\-])", text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start:line_end if line_end != -1 else None]
                start = match.start() - line_start
                tail = text[line_end + 1:] if line_end != -1 else ""
                if match_wires(line, start, start + len(match.group(0)),
                               python=python, tail=tail, funcs=funcs):
                    return True
        if not unique_stem:
            continue
        for match in re.finditer(
                r"(?<![\w\-])" + re.escape(stem) + r"(?![\w])", text):
            end = match.end()
            # A stem inside a filename token (test_alpha in test_alpha.sh)
            # is judged at the end of the whole token, so a case arm on the
            # filename also suppresses its stem.
            suffix = re.match(r"\.(?:sh|py|mjs|cjs|js|ts|mts|cts)",
                                text[end:])
            if suffix:
                end += len(suffix.group(0))
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", end)
            line = text[line_start:line_end if line_end != -1 else None]
            start = match.start() - line_start
            tail = text[line_end + 1:] if line_end != -1 else ""
            if match_wires(line, start, start + (end - match.start()),
                           python=python, tail=tail, funcs=funcs):
                return True
    for directory, pattern in discovers:
        if not suite.endswith(".py"):
            # ``unittest discover`` only collects ``.py`` modules.
            continue
        if directory:
            if not (suite == directory
                    or suite.startswith(directory + "/")):
                continue
            try:
                rel_parent = Path(suite).parent.relative_to(directory)
            except ValueError:
                continue
            base = Path(directory)
        else:
            rel_parent = Path(suite).parent
            base = Path()
        if any(str(base.joinpath(*rel_parent.parts[:i + 1],
                                 "__init__.py")) not in tracked_set
               for i in range(len(rel_parent.parts))):
            # Not under a tracked package chain: ``unittest discover``
            # never imports it.
            continue
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


def load_allowlist(path):
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise RuntimeError(f"allowlist missing: {path}")
    except (OSError, ValueError) as error:
        raise RuntimeError(f"allowlist unreadable: {error}")
    if not isinstance(raw, dict):
        raise RuntimeError("allowlist must be a JSON object of path -> reason")
    return raw


def check(root, allowlist):
    """Return a list of error strings; empty means the ratchet holds."""
    root = Path(root)
    errors = []
    for path, reason in sorted(allowlist.items()):
        if (not isinstance(path, str) or not isinstance(reason, str)
                or not reason.strip() or "\n" in reason.strip()):
            errors.append(f"allowlist entry needs a one-line reason: {path}")
    segments = build_corpus(root)
    discovers = discover_patterns(segments)
    suites = set(tracked_suites(root))
    tracked = set(tracked_files(root))
    basename_counts: dict = {}
    stem_counts: dict = {}
    for suite in suites:
        basename_counts[Path(suite).name] = (
            basename_counts.get(Path(suite).name, 0) + 1)
        stem_counts[Path(suite).stem] = (
            stem_counts.get(Path(suite).stem, 0) + 1)
    counts = (basename_counts, stem_counts)
    wired = {suite for suite in suites
             if is_wired(suite, segments, discovers, counts, tracked)}
    wired |= js_wired_suites(root, segments, suites)
    for suite in sorted(suites - wired):
        if suite not in allowlist:
            errors.append(
                f"orphan suite is neither wired nor exempt: {suite} "
                f"(wire it into a run: step in .github/workflows/*.yml, or "
                f"exempt it in verification/orphan_suites_allowlist.json)")
    for path in sorted(allowlist):
        if path not in suites:
            errors.append(
                f"stale exemption (not an unwired suite): {path}")
        elif path in wired:
            errors.append(
                f"stale exemption (now wired, drop it): {path}")
    return errors


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    root = ROOT
    # None until ``--allowlist`` overrides it: the default allowlist
    # lives under the selected root (``DEFAULT_ALLOWLIST`` is an
    # absolute path into this checkout, so joining it to ``--root``
    # would silently check another tree against our exemptions).
    allowlist_path = None
    i = 0
    while i < len(args):
        if args[i] == "--root" and i + 1 < len(args):
            i += 1
            root = Path(args[i])
        elif args[i] == "--allowlist" and i + 1 < len(args):
            i += 1
            allowlist_path = Path(args[i])
        elif args[i] in ("-h", "--help"):
            print(USAGE)
            return 0
        else:
            print(f"FAIL: unknown option: {args[i]}\n{USAGE}")
            return 2
        i += 1
    try:
        if allowlist_path is None:
            allowlist_path = Path(
                "verification/orphan_suites_allowlist.json")
        allowlist = load_allowlist(root / allowlist_path
                                   if not Path(allowlist_path).is_absolute()
                                   else allowlist_path)
        errors = check(root, allowlist)
    except RuntimeError as error:
        print(f"FAIL: {error}")
        return 1
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    print(f"PASS: orphan-suite ratchet holds "
          f"({len(tracked_suites(root))} suites, "
          f"{len(allowlist)} exemptions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
