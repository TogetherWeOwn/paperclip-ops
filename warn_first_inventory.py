#!/usr/bin/env python3
"""Read-only inventory of PR #881 WARN-first hits per rule (case_b871a1bb).

Mirrors the 16 vendored ast-grep rules plus the 5 semgrep rules with
line-oriented stdlib checks, so the inventory runs hermetically with no
tool installs. It sizes the M3.4 hard ratchet: per-rule hit counts with
exact file:line locations, and the zero-hit rules that can flip to HARD
with no backlog.

Scope mirrors Gate 4c (`ast-grep scan`, quality-gates.yml on the
case_db71b0ff branch): hidden directories — notably `.github/` — are skipped
by default, because ast-grep skips hidden paths unless passed
`--no-ignore hidden`. Pass `--include-hidden` to scan them anyway; the
two `gha-*` rules only fire there, so they are marked out-of-gate-scope
and never recommended, since the gate cannot evaluate them. Their
`--include-hidden` counts are advisory mirrors, not gate-exact output.

The five semgrep rows encode the rules' *intended* semantics (a direct
top-level `timeout=` keyword; any `subprocess.*(..., shell=True, ...)`),
not the byte output of the semgrep engine: Gate 6's
`python-subprocess-no-timeout` carries a top-level `pattern-not-regex`
that semgrep 1.180.0 ignores, so the engine flags even calls that pass
`timeout=` (flagged to the case_db71b0ff author; exact gate verdicts still
come from semgrep in CI). Each semgrep row is labelled with its origin
in the JSON report so intended semantics are never mistaken for engine
output.

`total` counts per-rule hits — one call site can fire both
subprocess-timeout rules — while `unique_locations` counts distinct
file:line sites, so the backlog is not double-counted.

Read-only contract: this script never writes to the tree. In a git
checkout it reads tracked files (`git ls-files`); elsewhere it walks the
tree, skipping `.git` and hidden dirs. Unreadable paths are listed as
skipped and suppress all flip recommendations; the CLI exits nonzero for
an incomplete scan.

Encoding / incomplete-scan contract: Python, shell and workflow source
is decoded as strict UTF-8 only. Encoding cookies do not opt into legacy
or unknown codecs, and there is no guessing, replacement or ignore
fallback; valid Unicode, including a literal U+FFFD, is ordinary source.
A binary, unreadable or undecodable in-scope file is skipped whole (it
contributes only its sorted skipped path, never hits), so counts from
decoded siblings are partial evidence: flip recommendations are empty
and the CLI exits 2 in both text and JSON modes. Skipped diagnostics
carry fixed text and paths only — never rejected bytes, source snippets
or arbitrary exception text. The agent-runnable check is
`python3 -B -m unittest -v test_warn_first_inventory
test_warn_inventory_decoding_acceptance`.

Test exclusion matches the gates: basenames starting with `test_` or
ending with `_test.py`, or any path part named `test`/`tests`.

Usage:
    python3 warn_first_inventory.py [--root PATH] [--format text|json]
        [--include-hidden]
Stdlib only.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

PER_PAGE_100 = "per_page=" + "100"
RELEASE_PLEASE = "release-please" + "--"

RULE_ORDER = [
    "py-subprocess-timeout",
    "semgrep-python-subprocess-no-timeout",
    "python-subprocess-shell-true",
    "python-os-system",
    "python-dangerous-eval-exec",
    "py-oswalk-onerror",
    "py-utc-mktime",
    "py-release-branch-exemption",
    "py-github-first-page-api",
    "py-redos-assert-token",
    "py-redos-yaml-indent",
    "py-default-redirect-opener",
    "bash-gh-first-page-only",
    "bash-json-stderr-merge",
    "bash-curl-pipe-shell",
    "bash-unquoted-url-ampersand",
    "bash-unquoted-db-url-ampersand",
    "bash-source-env-export",
    "bash-pusher-missing-env-load",
    "gha-artifact-always-unscrubbed",
    "gha-branch-name-trust",
]

# Rules whose `files:` globs live under `.github/`: the gate never scans
# them (hidden-dir skip), so they are out of gate scope and never
# recommended, even when `--include-hidden` surfaces hits.
HIDDEN_ONLY_RULES = frozenset(
    {
        "gha-artifact-always-unscrubbed",
        "gha-branch-name-trust",
    }
)

# Rows that encode semgrep-rule intended semantics (see header) rather
# than vendored ast-grep rules.
SEMGREP_RULES = frozenset(
    {
        "semgrep-python-subprocess-no-timeout",
        "python-subprocess-shell-true",
        "python-dangerous-eval-exec",
        "python-os-system",
        "bash-curl-pipe-shell",
    }
)

SUBPROCESS_FUNCS = re.compile(
    r"subprocess\.(run|call|check_call|check_output)\s*\("
)
# Any subprocess factory (Popen, etc.): the semgrep shell-true rule uses
# `subprocess.$FUNC(..., shell=True, ...)`, so the mirror must too.
SUBPROCESS_ANY = re.compile(r"subprocess\.(\w+)\s*\(")
SHELL_TRUE = re.compile(r"shell\s*=\s*True\b")
OS_SYSTEM = re.compile(r"\bos\.system\s*\(")
EVAL_EXEC = re.compile(r"\b(eval|exec)\s*\(")
OS_WALK = re.compile(r"\bos\.walk\s*\(")
ONERROR_KW = re.compile(r"onerror\s*=")
MKTIME_TRAILING = re.compile(r"time\.mktime\s*\(.*-\s*time\.timezone")
FIRST_PAGE_API = re.compile(r"\bapi\s*\(")
# Literal text of the vendored invalid fixture's regex source
# (`re.compile(r"\b\w*assert\w*!\s*\(")`): match the source text, not
# runtime strings such as "assertion_failed!".
ASSERT_TOKEN = re.compile(r"\\b\\w\*assert\\w\*!")
# Literal text of the vendored invalid fixture's regex source
# (`(?:^[ \t]+.*\n)*?`): the contiguous literal, not a loose
# allow-anything-between pattern.
YAML_INDENT = re.compile(re.escape("[ \\t]+.*\\n"))
URLOPEN_CALL = re.compile(r"\burlopen\s*\(")
GH_API = re.compile(r"\bgh\s+api\b")
STDERR_MERGE = re.compile(r"2\s*>\s*&\s*1")
FETCH_CMD = re.compile(r"\b(?:curl|wget)\b")
PIPE_SHELL = re.compile(r"\|\s*(?:bash|sh)\b")
UNQUOTED_URL = re.compile(
    r"^[A-Z][A-Z0-9_]*=https?://[^\s\"']+&"
)
UNQUOTED_DB_URL = re.compile(
    r"^[A-Z][A-Z0-9_]*=postgres(?:ql)?://[^\s\"']+&"
)
SOURCE_ENV = re.compile(r"^(?:source|\.)\s+\S*\.env")
SET_A = re.compile(r"^\s*set\s+-a\b")
PUSHER_CALL = re.compile(
    r"(push-db-backup-freshness|push-fleet-run-stall"
    r"|push-queue-forecast|push-operator-runs-disk)\.sh"
)
ENV_LOAD = re.compile(r"^(?:\.|source)\s+.*\.env")
UPLOAD_ARTIFACT = re.compile(r"uses:\s*actions/upload-artifact@")
ALWAYS_CALL = re.compile(r"always\s*\(\s*\)")
SWEPT_MARK = re.compile(r"swept")
HEAD_REF = re.compile(r"github\.head_ref")
BRANCH_TRUST = re.compile(r"release-please|lockfile|refresh")
ACTOR_GUARD = re.compile(r"github\.actor|head\.repo")


def is_test_path(path):
    parts = Path(path).parts
    if any(p in ("test", "tests") for p in parts):
        return True
    name = parts[-1] if parts else path
    return name.startswith("test_") or name.endswith("_test.py")


def is_hidden(rel):
    """True when any path part is dot-prefixed (gate 4c skips these)."""
    return any(part.startswith(".") for part in Path(rel).parts)


def is_python_file(rel):
    return rel.endswith(".py")


def is_shell_file(rel):
    return rel.endswith(".sh")


def is_workflow_file(rel):
    return rel.startswith(".github/workflows/") and (
        rel.endswith(".yml") or rel.endswith(".yaml")
    )


def is_scripts_tree(rel):
    return rel == "scripts" or rel.startswith("scripts/")


class InventoryCounts(dict):
    def __init__(self):
        super().__init__((rule, []) for rule in RULE_ORDER)
        self.skipped = []


def _ceiling_dirs():
    """Realpaths from GIT_CEILING_DIRECTORIES git will not ascend above."""
    ceilings = set()
    raw = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if entry:
            ceilings.add(os.path.realpath(entry))
    return ceilings


def _has_git_marker(root):
    """True when root or an ancestor below the ceiling holds `.git`.

    Hand-rolled fallback for when `git rev-parse` refuses (dubious
    ownership, dangling worktree pointer, corrupt `.git`, localized
    message): any refusal with a `.git` marker still means a checkout, so
    the caller fails closed via `ls-files` instead of walking untracked
    files. Stops at GIT_CEILING_DIRECTORIES so temp fixtures stay
    hermetic when TMPDIR sits inside a checkout.
    """
    ceilings = _ceiling_dirs()
    try:
        current = Path(os.path.realpath(str(root)))
    except OSError:
        return False
    chain = (current, *current.parents)
    for directory in chain:
        if directory != current and str(directory) in ceilings:
            break
        if (directory / ".git").exists():
            return True
    return False


def _is_inside_work_tree(root):
    """True when root is inside a git work tree; None when git is missing.

    Uses `git rev-parse --is-inside-work-tree` so GIT_CEILING_DIRECTORIES
    is honoured (a hand-rolled ancestor `.git` walk is not hermetic when
    TMPDIR sits inside a checkout). Any refusal with a `.git` marker at
    or below the ceiling (dubious ownership, dangling pointer, corrupt
    `.git`, localized message) still returns True to fail closed; only a
    refusal with no marker returns False.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and proc.stdout.strip() == "true":
        return True
    return _has_git_marker(root)


def _has_filesystem_files(root):
    """True when root holds any file outside `.git`.

    Existence probe for the untracked/ignored-dir case: `git ls-files`
    exits 0 with empty output there, which must not report a false clean.
    Permission errors assume files exist (fail closed).
    """
    found = False

    def _onerror(_exc):
        nonlocal found
        found = True

    try:
        for dirpath, dirnames, filenames in os.walk(
            root, onerror=_onerror
        ):
            if ".git" in dirnames:
                dirnames.remove(".git")
            if filenames:
                return True
            if found:
                return True
    except OSError:
        return True
    return found


def list_files(root, include_hidden=False, skipped=None):
    root = Path(root)
    # A --root below the checkout top is still inside the checkout: ask
    # git (respects GIT_CEILING_DIRECTORIES) so subdir scans stay
    # tracked-only and fail closed when git errors (e.g. dubious
    # ownership, rc 128). When git itself is missing, fall back to a
    # `.git` ancestor walk so a checkout still fails closed.
    inside = _is_inside_work_tree(root)
    if inside is None:
        in_git_checkout = _has_git_marker(root)
    else:
        in_git_checkout = inside
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
        # Fixed text, never str(exc): skipped diagnostics must not carry
        # arbitrary exception text into the report.
        git_error = "git unavailable"
    else:
        git_error = f"git ls-files exited {proc.returncode}"

    if in_git_checkout:
        if proc is None or proc.returncode != 0:
            if skipped is not None:
                skipped.append(f".git ({git_error})")
            return []
        # -z emits NUL-separated paths so quoted non-ASCII names (e.g.
        # "caf\303\251.py" under core.quotepath) survive intact.
        files = sorted(f for f in (proc.stdout or "").split("\x00") if f)
        if not files:
            # Untracked or ignored dir inside a checkout: ls-files exits 0
            # empty, which must not report a false clean with flips.
            if _has_filesystem_files(root):
                if skipped is not None:
                    skipped.append(
                        f"{root} (no tracked files: untracked or "
                        "ignored dir inside a checkout)"
                    )
                return []
            return []
        if not include_hidden:
            files = [f for f in files if not is_hidden(f)]
        return files

    found = []

    def record_walk_error(error):
        if skipped is not None:
            path = getattr(error, "filename", None) or str(root)
            skipped.append(f"{path} (walk failed)")

    for dirpath, dirnames, filenames in os.walk(
        root, onerror=record_walk_error
    ):
        dirnames[:] = [
            d
            for d in dirnames
            if d != ".git" and (include_hidden or not d.startswith("."))
        ]
        for name in filenames:
            if not include_hidden and name.startswith("."):
                continue
            full = Path(dirpath, name)
            found.append(str(full.relative_to(root)))
    return sorted(found)


def read_text(root, rel):
    """Return `rel` decoded as strict UTF-8, or None when it cannot be.

    None means the caller records `rel` as skipped (incomplete evidence):
    binary (NUL-probed) and undecodable source take that path, so no
    lossy decoding ever fabricates or hides rule tokens. `OSError` from
    the read propagates for the caller's same skipped handling. The
    decode error is deliberately discarded: its text can echo rejected
    bytes, and skipped entries carry paths only.
    """
    raw = (Path(root) / rel).read_bytes()
    if b"\x00" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def balanced_call(text, start):
    """Return the call text starting at the opening paren index.

    Balances parentheses up to a cap; falls back to the rest of the
    line when unbalanced (single-line calls are the common case).
    """
    depth = 0
    pos = start
    limit = min(len(text), start + 2000)
    while pos < limit:
        char = text[pos]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:pos + 1]
        pos += 1
    end = text.find("\n", start)
    if end == -1:
        end = len(text)
    return text[start:end]


def line_of(text, index):
    return text.count("\n", 0, index) + 1


def _split_top_level_args(inner):
    """Split call args on commas at nesting depth 0 (code is blanked)."""
    parts = []
    current = []
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in inner:
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
        elif char == "," and not stack:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _timeout_ok(call):
    """True when the call passes a real top-level `timeout=` keyword.

    Mirrors the vendored rule's `^timeout\\s*=` (a direct keyword
    argument): a nested `env=dict(timeout=30)` does not count, a
    suffixed `read_timeout=` does not match (the segment must start with
    `timeout`), and `timeout=None` is still a violation.
    """
    open_paren = call.find("(")
    if open_paren == -1:
        return False
    inner = call[open_paren + 1 :]
    if inner.endswith(")"):
        inner = inner[:-1]
    for part in _split_top_level_args(inner):
        match = re.match(r"\s*timeout\s*=(.*)$", part, re.DOTALL)
        if match:
            return not re.match(r"\s*None\b", match.group(1))
    return False


def scan_python_calls(text):
    """Yield (kind, line_no, call_text) for subprocess/os.walk calls."""
    for kind, pattern in (
        ("subprocess", SUBPROCESS_FUNCS),
        ("oswalk", OS_WALK),
    ):
        for match in pattern.finditer(text):
            open_paren = text.find("(", match.start())
            call = balanced_call(text, open_paren)
            yield kind, line_of(text, match.start()), call


def blanked_code_and_strings(text):
    """Split Python source into code-only lines plus raw string tokens.

    Returns (code_text, [(line_no, raw_token)]) where code_text has
    every string literal and comment replaced by spaces (length and
    line numbers preserved), and raw_token is the undecoded token
    text for `kind: string` rules. On Python 3.12+ f-strings tokenize
    as FSTRING_START/MIDDLE/END (not STRING), so literal spans are
    blanked via FSTRING_MIDDLE while `{...}` expressions stay code.
    Returns (None, None) when the source does not tokenize, so callers
    fall back to raw text.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:
        return None, None
    rows = [list(line) for line in text.splitlines()]

    def blank(sr, sc, er, ec):
        for row in range(sr - 1, er):
            if row < 0 or row >= len(rows):
                continue
            start = sc if row == sr - 1 else 0
            stop = ec if row == er - 1 else len(rows[row])
            for i in range(start, min(stop, len(rows[row]))):
                rows[row][i] = " "

    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    strings = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            (row, col) = tok.start
            blank(row, col, row, len(rows[row - 1]) if row - 1 < len(rows) else col)
        elif tok.type == tokenize.STRING:
            (sr, sc), (er, ec) = tok.start, tok.end
            strings.append((sr, tok.string))
            blank(sr, sc, er, ec)
        elif (
            fstring_middle is not None
            and tok.type == fstring_middle
        ):
            (sr, sc), (er, ec) = tok.start, tok.end
            strings.append((sr, tok.string))
            blank(sr, sc, er, ec)
        elif fstring_start is not None and tok.type in (
            fstring_start,
            fstring_end,
        ):
            continue
    return "\n".join("".join(row) for row in rows), strings


def strip_bash_comment(line):
    """Cut a `#` comment that starts the line or follows whitespace."""
    match = re.search(r"(^|\s)#", line)
    if match and match.group(1):
        return line[: match.start()] + " "
    if line.startswith("#"):
        return ""
    return line


_HEREDOC_DELIM = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def _heredoc_at(line, pos):
    """Parse `<<[-]delim` at `pos`; None when herestring/invalid.

    Returns (delim, strip_tabs, endpos). Only static word delimiters
    qualify, so a dynamic `<<$var` never swallows the rest of the file.
    """
    n = len(line)
    j = pos + 2
    if j < n and line[j] == "<":
        return None  # herestring `<<<`, not a heredoc
    strip_tabs = False
    if j < n and line[j] == "-":
        strip_tabs = True
        j += 1
    while j < n and line[j] in " \t":
        j += 1
    if j >= n:
        return None
    if (
        line[j] == "\\"
        and j + 1 < n
        and line[j + 1] in "\"'"
    ):
        # Backslash-quoted delimiter (`<<\'PY\'`): same heredoc.
        quote = line[j + 1]
        end = line.find(quote, j + 2)
        if end == -1:
            return None
        delim = line[j + 2 : end]
        endpos = end + 1
    elif line[j] in "\"'":
        quote = line[j]
        end = line.find(quote, j + 1)
        if end == -1:
            return None
        delim = line[j + 1 : end]
        endpos = end + 1
    elif line[j] == "\\":
        end = j + 1
        while end < n and (line[end].isalnum() or line[end] in "_-"):
            end += 1
        delim = line[j + 1 : end]
        endpos = end
    else:
        end = j
        while end < n and line[end] not in " \t;|&()<>#\"'\\":
            end += 1
        delim = line[j:end]
        endpos = end
    if not delim or not _HEREDOC_DELIM.fullmatch(delim):
        return None
    return delim, strip_tabs, endpos


def substitution_depths(lines):
    """Per-line (inside_before, inside, open_start) for `$()` scanning.

    Tracks command-substitution nesting across lines with a frame stack:
    each `$(` or backtick pushes a frame with a fresh quote context (as
    the shell does — a `'` inside `"$(...)"` opens single quotes, and a
    backtick inside `'...'` is literal), so one stuck opener cannot
    poison the rest of the file. `inside` is a list of (start, end)
    offsets into the comment-stripped line where the gate's
    `command_substitution` node holds, so a `2>&1` is a hit only when
    the redirect itself sits inside a substitution — `a=$(x) 2>&1` and
    `cmd 2>&1 $(y)` are not. A hit on a continuation line is attributed
    via open_start, the 1-based line where the innermost open
    substitution began. `$((...))` arithmetic is not a substitution and
    is skipped; a `)` closes a substitution only at the paren/brace
    levels where it opened, so `${...:-...$(...)...}` and subshells
    balance while function-body braces are ignored. `#` comments are
    cut only outside quotes (a `#` inside `"..."`, as in the Postgres
    `#>>` operator, is literal). `<<[-]delim` heredoc bodies are
    skipped through the closing delimiter, so embedded quotes never
    desync the tracker.
    """
    stack = [{"kind": "base", "line": None, "quote": None}]
    parens = 0
    braces = 0
    heredocs = []
    result = []
    for lineno, raw in enumerate(lines, start=1):
        line = raw
        inside_before = len(stack) > 1
        open_start = stack[-1]["line"] if len(stack) > 1 else None
        if heredocs:
            delim, strip_tabs = heredocs[0]
            text = line.lstrip("\t") if strip_tabs else line
            if text == delim:
                heredocs.pop(0)
            # Body text belongs to the open node's span: it is checked
            # when inside a substitution, but changes no state.
            result.append(
                (
                    inside_before,
                    [(0, len(line))] if inside_before else [],
                    open_start,
                )
            )
            continue
        inside = []
        range_start = None
        pos = 0
        while pos < len(line):
            if len(stack) > 1:
                if range_start is None:
                    range_start = pos
            elif range_start is not None:
                inside.append((range_start, pos))
                range_start = None
            char = line[pos]
            top = stack[-1]
            quote = top["quote"]
            if quote == "'":
                # Backslash is literal inside single quotes.
                if char == "'":
                    top["quote"] = None
            elif char == "\\":
                pos += 2
                continue
            elif quote == '"':
                if char == '"':
                    top["quote"] = None
                elif char == "`":
                    if top["kind"] == "backtick":
                        stack.pop()
                    else:
                        stack.append(
                            {
                                "kind": "backtick",
                                "line": lineno,
                                "quote": None,
                                "parens": parens,
                                "braces": braces,
                            }
                        )
                elif (
                    char == "$"
                    and pos + 1 < len(line)
                    and line[pos + 1] == "("
                ):
                    if pos + 2 < len(line) and line[pos + 2] == "(":
                        pos += 3
                        continue
                    stack.append(
                        {
                            "kind": "subst",
                            "line": lineno,
                            "quote": None,
                            "parens": parens,
                            "braces": braces,
                        }
                    )
                    pos += 1
                elif (
                    char == "$"
                    and pos + 1 < len(line)
                    and line[pos + 1] == "{"
                ):
                    braces += 1
                    pos += 1
                elif char == "}" and braces > 0:
                    braces -= 1
            else:
                if char == "#" and (
                    pos == 0 or line[pos - 1] in " \t;&|()"
                ):
                    # Unquoted `#` at a word boundary starts a comment:
                    # no further state changes on this line. The tail
                    # stays inside the open node's text span, so ranges
                    # extend to EOL (a `#` inside `"..."`, as in the
                    # Postgres `#>>` operator, is literal and never
                    # reaches this branch).
                    break
                elif char == "'":
                    top["quote"] = "'"
                elif char == '"':
                    top["quote"] = '"'
                elif (
                    char == "<"
                    and pos + 1 < len(line)
                    and line[pos + 1] == "<"
                ):
                    # A lone `<` (redirect, `<(` process substitution)
                    # is literal; only `<<` can open a heredoc.
                    parsed = _heredoc_at(line, pos)
                    if parsed is None:
                        pass
                    else:
                        heredocs.append([parsed[0], parsed[1]])
                        pos = parsed[2]
                        continue
                elif char == "`":
                    if top["kind"] == "backtick":
                        stack.pop()
                    else:
                        stack.append(
                            {
                                "kind": "backtick",
                                "line": lineno,
                                "quote": None,
                                "parens": parens,
                                "braces": braces,
                            }
                        )
                elif (
                    char == "$"
                    and pos + 1 < len(line)
                    and line[pos + 1] == "("
                ):
                    if pos + 2 < len(line) and line[pos + 2] == "(":
                        pos += 3
                        continue
                    stack.append(
                        {
                            "kind": "subst",
                            "line": lineno,
                            "quote": None,
                            "parens": parens,
                            "braces": braces,
                        }
                    )
                    pos += 1
                elif char == "(":
                    parens += 1
                elif char == ")":
                    top_frame = stack[-1]
                    if (
                        len(stack) > 1
                        and top_frame["kind"] == "subst"
                        and parens == top_frame["parens"]
                        and braces == top_frame["braces"]
                    ):
                        stack.pop()
                    elif parens > 0:
                        parens -= 1
                elif (
                    char == "$"
                    and pos + 1 < len(line)
                    and line[pos + 1] == "{"
                ):
                    braces += 1
                    pos += 1
                elif char == "}" and braces > 0:
                    braces -= 1
            pos += 1
        if range_start is not None:
            inside.append((range_start, len(line)))
        result.append((inside_before, inside, open_start))
    return result


def workflow_steps(lines):
    """Yield each YAML sequence-item mapping with its starting line."""
    start = None
    step_indent = None
    mapping = []
    for line_no, line in enumerate(lines, start=1):
        match = re.match(r"^(\s*)-\s+", line)
        if match:
            indent = len(match.group(1))
            if start is not None and indent == step_indent:
                yield start, mapping
                start, mapping = line_no, [line]
                continue
            if start is not None and indent < step_indent:
                yield start, mapping
                start = None
                mapping = []
        if start is None and match:
            start = line_no
            step_indent = len(match.group(1))
            mapping = [line]
        elif start is not None:
            indent = len(line) - len(line.lstrip())
            if line.strip() and indent <= step_indent:
                yield start, mapping
                start = None
                mapping = []
            else:
                mapping.append(line)
    if start is not None:
        yield start, mapping


def scan_file(rel, text):
    """Return [(rule_id, line_no, excerpt)] for one file."""
    hits = []
    lines = text.splitlines()

    def add(rule, line_no):
        excerpt = lines[line_no - 1].strip()[:160] if 0 < line_no <= len(lines) else ""
        hits.append((rule, line_no, excerpt))

    if is_python_file(rel):
        code_text, strings = blanked_code_and_strings(text)
        call_text = code_text if code_text is not None else text
        for kind, line_no, call in scan_python_calls(call_text):
            if kind == "subprocess":
                if not _timeout_ok(call):
                    add("py-subprocess-timeout", line_no)
                    add("semgrep-python-subprocess-no-timeout", line_no)
                if SHELL_TRUE.search(call):
                    add("python-subprocess-shell-true", line_no)
            elif not ONERROR_KW.search(call):
                add("py-oswalk-onerror", line_no)
        for match in SUBPROCESS_ANY.finditer(call_text):
            if match.group(1) in ("run", "call", "check_call", "check_output"):
                continue  # covered by the loop above
            open_paren = call_text.find("(", match.start())
            call = balanced_call(call_text, open_paren)
            if SHELL_TRUE.search(call):
                add("python-subprocess-shell-true", line_of(call_text, match.start()))
        code_lines = call_text.splitlines()
        for num, (line, raw_line) in enumerate(zip(code_lines, lines), start=1):
            if OS_SYSTEM.search(line):
                add("python-os-system", num)
            if EVAL_EXEC.search(line):
                add("python-dangerous-eval-exec", num)
            if MKTIME_TRAILING.search(line):
                add("py-utc-mktime", num)
            if "startswith(" in line and RELEASE_PLEASE in raw_line:
                add("py-release-branch-exemption", num)
            if FIRST_PAGE_API.search(line) and PER_PAGE_100 in raw_line:
                add("py-github-first-page-api", num)
            if is_scripts_tree(rel) and URLOPEN_CALL.search(line):
                add("py-default-redirect-opener", num)
        if strings is None:
            string_spans = []
        else:
            string_spans = strings
        for line_no, raw_token in string_spans:
            if ASSERT_TOKEN.search(raw_token):
                add("py-redos-assert-token", line_no)
            if YAML_INDENT.search(raw_token):
                add("py-redos-yaml-indent", line_no)

    if is_shell_file(rel):
        code_lines = [strip_bash_comment(line) for line in lines]
        has_set_a = any(SET_A.search(line) for line in code_lines)
        has_env_load = any(ENV_LOAD.search(line) for line in code_lines)
        depths = substitution_depths(lines)
        for num, (line, raw_sh, (inside_before, inside, open_start)) in enumerate(
            zip(code_lines, lines, depths), start=1
        ):
            if GH_API.search(line) and PER_PAGE_100 in line and "--paginate" not in line:
                add("bash-gh-first-page-only", num)
            # Match on the raw line: `inside` offsets are raw-based, and
            # the in-scanner comment cut keeps `#` inside quotes literal.
            stderr_at = STDERR_MERGE.search(raw_sh)
            if stderr_at and any(
                start <= stderr_at.start() < end for start, end in inside
            ):
                # Attribute to the substitution's opening line (where the
                # gate reports it), not the continuation line holding 2>&1.
                add(
                    "bash-json-stderr-merge",
                    (open_start or num) if inside_before else num,
                )
            if FETCH_CMD.search(line) and PIPE_SHELL.search(line):
                add("bash-curl-pipe-shell", num)
            if UNQUOTED_URL.search(line):
                add("bash-unquoted-url-ampersand", num)
            if UNQUOTED_DB_URL.search(line):
                add("bash-unquoted-db-url-ampersand", num)
            if SOURCE_ENV.search(line) and not has_set_a:
                add("bash-source-env-export", num)
            if PUSHER_CALL.search(line) and not has_env_load:
                add("bash-pusher-missing-env-load", num)

    if is_workflow_file(rel):
        for step_line, step in workflow_steps(lines):
            step_text = "\n".join(step)
            if (
                UPLOAD_ARTIFACT.search(step_text)
                and ALWAYS_CALL.search(step_text)
                and not SWEPT_MARK.search(step_text)
            ):
                upload_line = next(
                    index
                    for index, line in enumerate(step, start=step_line)
                    if UPLOAD_ARTIFACT.search(line)
                )
                add("gha-artifact-always-unscrubbed", upload_line)
        for num, line in enumerate(lines, start=1):
            if (
                HEAD_REF.search(line)
                and BRANCH_TRUST.search(line)
                and not ACTOR_GUARD.search(line)
            ):
                add("gha-branch-name-trust", num)

    return hits


def scan_tree(root, include_hidden=False):
    root = Path(root)
    counts = InventoryCounts()
    files = list_files(
        root, include_hidden=include_hidden, skipped=counts.skipped
    )
    for rel in files:
        if is_test_path(rel):
            continue
        if not (
            is_python_file(rel) or is_shell_file(rel) or is_workflow_file(rel)
        ):
            continue
        try:
            text = read_text(root, rel)
        except (OSError, UnicodeDecodeError):
            text = None
        if text is None:
            counts.skipped.append(rel)
            continue
        for rule, line_no, excerpt in scan_file(rel, text):
            if rule in counts:
                counts[rule].append((rel, line_no, excerpt))
    for rule in counts:
        counts[rule].sort()
    counts.skipped.sort()
    return counts


def recommend(counts, include_hidden=False):
    """In-scope zero-hit rules, sorted: they flip to HARD with no backlog.

    Incomplete scans and rules outside gate scope cannot support a flip.
    """
    if getattr(counts, "skipped", []):
        return []
    return sorted(
        rule
        for rule in RULE_ORDER
        if not counts.get(rule) and rule not in HIDDEN_ONLY_RULES
    )


def build_report(root, counts, include_hidden=False):
    by_rule = {}
    for rule in RULE_ORDER:
        hits = counts.get(rule, [])
        entry = {
            "count": len(hits),
            "origin": (
                "semgrep (intended semantics, see header)"
                if rule in SEMGREP_RULES
                else (
                    "ast-grep (advisory hidden-scope mirror)"
                    if rule in HIDDEN_ONLY_RULES
                    else "ast-grep"
                )
            ),
            "hits": [
                {"file": rel, "line": num, "excerpt": excerpt}
                for rel, num, excerpt in hits
            ],
        }
        by_rule[rule] = entry
    total = sum(len(hits) for hits in counts.values())
    unique = len(
        {(rel, num) for hits in counts.values() for rel, num, _ in hits}
    )
    return {
        "root": str(root),
        "by_rule": by_rule,
        "total": total,
        "unique_locations": unique,
        "skipped": list(getattr(counts, "skipped", [])),
        "out_of_gate_scope": sorted(HIDDEN_ONLY_RULES),
        "recommend": recommend(counts, include_hidden=include_hidden),
    }


def format_text(report):
    out = [f"WARN-first inventory (root): {report['root']}"]
    for rule in RULE_ORDER:
        entry = report["by_rule"][rule]
        out.append(f"{rule}: {entry['count']} hit(s)")
        for hit in entry["hits"]:
            out.append(f"  {hit['file']}:{hit['line']}: {hit['excerpt']}")
    out.append(
        f"total: {report['total']} rule-hit(s) across "
        f"{report['unique_locations']} unique location(s)"
    )
    if report.get("out_of_gate_scope"):
        out.append(
            "out of gate scope (never flip candidates): "
            + ", ".join(report["out_of_gate_scope"])
        )
    skipped = report.get("skipped", [])
    if skipped:
        out.append(f"incomplete scan: {len(skipped)} path(s) skipped")
        out.extend(f"  skipped: {path}" for path in skipped)
    else:
        flips = report["recommend"]
        out.append(
            "flips next (zero hits): " + (", ".join(flips) if flips else "none")
        )
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only WARN-first hit inventory per rule."
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="scan dot-directories (e.g. .github/); the gate skips them",
    )
    args = parser.parse_args(argv)
    counts = scan_tree(Path(args.root), include_hidden=args.include_hidden)
    report = build_report(
        Path(args.root), counts, include_hidden=args.include_hidden
    )
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_text(report), end="")
    return 2 if report["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
