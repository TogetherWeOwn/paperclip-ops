#!/usr/bin/env python3
"""shell_subst_guard.py -- refuse an unbounded `yes` inside shell command substitution.

WHY THIS EXISTS. A single Bash tool command of the observed shape

    echo "RUNSET=$([ -n "$PAPERCLIP_RUN_ID" ] && yes || no)"

collected the infinite stdout of `yes` into a command substitution until the
whole application memory cgroup was exhausted (about 30 GiB of anonymous
RSS, one bash process, memcg OOM). The host itself was healthy (CPU/IO/memory
pressure all low, tens of GiB free), so no host-level pressure response --
concurrency cuts, restarts, model changes -- addresses it. Shell command
substitution (`$(...)` and backticks) collects stdout until EOF, and `yes`
never sends EOF, so the collection is unbounded by construction. This gate
is the deterministic pre-execution refusal for exactly that mechanism.

WHAT IT CHECKS. `check --command <string> | --file <path> | --stdin` scans
one shell command WITHOUT executing it and refuses (exit 3) when any
`$(...)` or backtick span contains `yes` in executable command position:

    RUNSET=$([ -n "$PAPERCLIP_RUN_ID" ] && yes || no)   refused (observed shape)
    x=$(yes) ; x=`yes` ; x=$(command yes)               refused
    x=$(FOO=1 yes) ; x=$(/usr/bin/yes) ; x=$("yes")     refused
    x=$(yes | head -n 1)                                refused (fail-closed pin,
                                                        see below)
    x=${A:-$(yes)} ; x=$(( $(yes) + 1 ))                refused (nested inside
                                                        expansion/arithmetic)
    x=$(case $A in *) yes;; esac)                      refused (case body)
    x=$(2>/dev/null yes) ; x=$(</dev/null yes)         refused (leading
    x=$(FOO=1 2>/dev/null yes)                           redirect does not clear
                                                        command position)
    echo "a #b $(yes)" ; x=$(echo ${#A}; yes)          refused (# inside double
                                                        quotes / ${#..} is not
                                                        a comment)
    x=$(ye"s") ; x=$(y'es') ; x=$($'yes')              refused (glued quoting)
    x=$(exec yes) ; x=$(eval yes) ; x=$(nice yes)       refused (direct prefixes)
    x=$(echo "a)b"; yes)                               refused (a paren inside
                                                        double quotes never
                                                        closes the span)
    x=$(echo case; yes)                                refused (case as an
                                                        argument hides nothing)
    x=$(ye + line-continuation + s)                   refused (line
    x=$(dollar + line-continuation + (yes))          continuations are
                                                        deleted first)
    a=$'\''; x=$(yes)                                  refused (dollar-single
                                                        escapes hold)
    x=$({fd}>/dev/null yes)                            refused (var-fd redirect
                                                        keeps command position)
    x=$(case $A in @(a|b)) p;; *) yes;; esac)          refused (extglob groups
                                                        and quoted parens in
                                                        patterns)
    x=$(backslash-escaped yes in $'...')               refused (hex x79, octal
                                                        171, unicode u0079
                                                        and U00000079 all
                                                        decode to yes)
    cat <<EOF / don't $(yes) / EOF                     refused (quotes and #
                                                        mean nothing in an
                                                        unquoted heredoc body)
    x=$($'\\571es')                                    refused (octal escapes
                                                        above 255 wrap
                                                        mod 256 to one byte)
    x=$(n=$((1+1)); yes)                               refused (arithmetic never
                                                        truncates the span)
    x=$(for i in a b /newline/ do yes; done)           refused (do ends a
                                                        newline-separated
                                                        for list)
    x=$(((1)); yes) ; x=`((echo hi); yes)`             refused (`((` without
                                                        a matching arithmetic
                                                        `))` is subshells)

The following stay allowed, and the suite pins each one:

    RUNSET=$([ -n "$PAPERCLIP_RUN_ID" ] && printf 'yes' || printf 'no')
    echo $(echo yes) ; echo 'yes' ; echo "yes"          (yes as literal/argument)
    # $(yes)                                           (comments are not code)
    x=$((1 + 2)) ; x=${FLAG:-yes}                      (arithmetic/parameter
                                                        expansion is not a
                                                        substitution)
    x=$( [[ $A == yes ]] && printf ok)                 (test operands)
    x=$(case $A in yes) printf ok;; *) printf no;; esac) (case patterns)
    type yes                                            (descriptive-only builtin:
                                                        bounded existence probe)
    cat <<'EOF'                                        (quoted heredoc bodies
    $(yes)                                               are literal stdin)
    EOF
    yes | head -n 1                                    (no substitution: nothing
                                                        is collected, so the
                                                        memcg mechanism here
                                                        does not apply)
    x=$(echo "("); yes | head -n 1                     (quoted paren, and the
                                                        bare yes is bounded)
    case $x in a) v=$(date);; esac; yes | head -1      (top-level case never
                                                        swallows following
                                                        commands)

INTENTIONAL FAIL-CLOSED OVER-APPROXIMATIONS (pinned by tests, not silent):

    * `$(yes | head -n 1)` is refused even though head bounds it. The gate
      cannot verify the bound (compare `head -n 999999999`), so it refuses
      and directs the author to `printf`. Never silently weakened.
    * `$(yes --help)` / `$(yes --version)` are refused although bounded.
    * `$(command -v yes)` is refused although `-v` only describes. `command`
      can also execute its operand and the gate does not model per-flag
      semantics; use the descriptive-only `type yes`, which stays allowed.
    * An unclosed `$(` is refused (the shell would reject it too).

REMAINING GAPS (honest, out of scope for this proven failure):

    * Other unbounded producers (`cat /dev/zero`, `tail -f`, `find /`,
      `xargs yes`, ...) are not flagged. This gate scopes to the proven
      `yes`-in-substitution failure, not general command censorship.
    * A substitution fed through process substitution (`$(cat <(yes))`)
      collects unboundedly but is not flagged.
    * Commands assembled at runtime (`eval "$DYN"`, variable-held scripts)
      are invisible to any lexical gate. A wording-only instruction is not
      a substitute for this check at the admission boundary.
    * Flag-argument wrappers the gate does not model (`sudo -u <user> yes`,
      `env -u <name> yes`, `nice -n <n> yes`, a function literally named
      `yes`) are missed. Direct `exec`/`eval`/`nice` prefixes with no
      flag-argument (e.g. `$(exec yes)`) ARE refused.
    * Matching is exact ASCII `yes` (quote/escape/path normalized, including
      glued `ye"s"`/`$'yes'` forms). Unicode homoglyph spellings are not
      modeled.

INTEGRATION BOUNDARY. No Bash-tool admission hook exists in this public
tree: the fleet-settings hook surface covered by hook_spawn_guard.sh handles
prompt-submit hooks only, and the runtime that executes agent Bash tool calls
(and owns the supported PreToolUse matcher settings surface) lives in the
managed platform repository, not here. Do NOT copy private tooling into this
repo to work around that. Installation is owned by the Host Operator through
the managed adapter hook path after review: register a PreToolUse entry whose
matcher covers the Bash tool, invoke this gate with the exact tool command
string, and block execution on exit 3. Rollback is removing that one matcher
entry; no data migration, no host-wide ulimit/BASH_ENV policy (explicitly
out of scope -- an unreviewed global shell policy is not this change).

Offline: reads one string or file. No network, no credential, no execution of
the scanned command. Exit 0 clean - 2 usage error - 3 finding refused.

MACHINE MODE. `check --json` with one of `--command`/`--file`/`--stdin`
emits exactly one deterministic JSON object on available stdout with empty
stderr, on every exit (0/2/3). With stdout closed, only the exit status is
available; closed stdin is an `input-unreadable` error. Schema
`shell-substitution.v1`:
`{schema, outcome: clean|findings|input_error, input_kind:
command|file|stdin|null, finding_count, findings: [{code}], error}`.
Findings carry only the stable rule code (ordered, deduplicated like the
text mode); no excerpts, offsets, command text, argv or file bytes appear
in either stream. Errors are generic `{code, message, retryable: false,
next_action}` with `invalid-arguments`/`input-unreadable`/`invalid-utf8`.
`--json --help` adds `{usage, examples}` to a clean envelope. Argparse's
unambiguous option abbreviations retain the same output mode, including on
errors and help. Legacy text keeps its diagnostic excerpts by design; machine
mode is private checker output, not a general redactor or credential boundary.
"""

from __future__ import annotations

import argparse
import json
import sys

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_FINDING = 3

FINDING_NAME = "unbounded-yes-substitution"
EXCERPT_LEN = 160
SCHEMA_ID = "shell-substitution.v1"

_JSON_USAGE = ("shell_subst_guard.py check [--json] "
               "(--command COMMAND | --file FILE | --stdin)")
_JSON_EXAMPLES = (
    "shell_subst_guard.py check --command 'x=$(yes)'",
    "shell_subst_guard.py check --file ./script.sh",
    "printf 'x=$(yes)' | shell_subst_guard.py check --stdin",
    "shell_subst_guard.py check --json --command 'x=$(yes)'",
)
_MSG_INVALID_ARGS = ("Invalid arguments. Provide check with exactly one "
                     "of --command, --file, or --stdin.")
_MSG_UNREADABLE = ("Input could not be read. Provide a readable file "
                   "or input stream.")
_MSG_BAD_UTF8 = "Input is not valid UTF-8. Provide UTF-8 encoded input."


def _json_error(code: str, message: str, next_action: str) -> dict:
    """One generic machine-mode error: a stable code, a message that never
    quotes supplied input, and the corrective action. Never retryable: the
    same input would fail the same way."""
    return {"code": code, "message": message, "retryable": False,
            "next_action": next_action}


def _json_envelope(input_kind: str | None, findings: list[dict],
                   error: dict | None) -> dict:
    """The single `shell-substitution.v1` verdict object: outcome is
    `input_error` when `error` is set, else `findings` or `clean` by count.
    Findings carry rule codes only — never excerpts, paths or offsets."""
    count = len(findings)
    return {"schema": SCHEMA_ID,
            "outcome": "input_error" if error
            else ("findings" if count else "clean"),
            "input_kind": input_kind, "finding_count": count,
            "findings": findings, "error": error}


def _emit_json(obj: dict) -> None:
    """Write the envelope as one deterministic compact line on stdout.
    An unavailable stdout (closed before startup) emits nothing instead of
    raising, so the caller can still return the verdict exit code."""
    if sys.stdout is not None:
        sys.stdout.write(json.dumps(obj, sort_keys=True,
                                    separators=(",", ":")) + "\n")


def _json_input_error(input_kind: str | None, code: str, message: str,
                      next_action: str) -> dict:
    """An `input_error` envelope with no findings for one input failure."""
    return _json_envelope(input_kind, [],
                          _json_error(code, message, next_action))


class _GuardParser(argparse.ArgumentParser):
    """ArgumentParser whose help and error exits stay in machine mode when
    JSON was requested, so argparse never prints usage prose echoing argv.
    Text mode keeps the stock argparse behaviour byte-for-byte."""

    def __init__(self, *args, json_mode: bool = False, **kwargs) -> None:
        """Record whether this parse must answer with JSON envelopes."""
        super().__init__(*args, **kwargs)
        self._json_mode = json_mode

    def print_help(self, file=None) -> None:
        """JSON mode: a clean envelope carrying usage and examples.
        Text mode: the standard argparse help text."""
        if self._json_mode:
            obj = _json_envelope(None, [], None)
            obj["help"] = {"usage": _JSON_USAGE,
                           "examples": list(_JSON_EXAMPLES)}
            _emit_json(obj)
            return
        super().print_help(file)

    def error(self, message: str) -> None:
        """JSON mode: emit a generic `invalid-arguments` envelope (argparse's
        message quotes the offending argv, so it is discarded) and exit 2.
        Text mode: the standard argparse error and usage exit."""
        if self._json_mode:
            _emit_json(_json_input_error(None, "invalid-arguments",
                                         _MSG_INVALID_ARGS,
                                         "correct_arguments"))
            raise SystemExit(EXIT_REFUSED)
        super().error(message)


def _read_stdin_text() -> str:
    """Read stdin once and decode it as strict UTF-8. An unavailable stream
    raises OSError and undecodable bytes raise UnicodeDecodeError, so the
    caller can tell unreadable input from invalid UTF-8."""
    if sys.stdin is None:
        raise OSError("Input stream is unavailable")
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        data = buf.read()
        if isinstance(data, str):
            return data
        return data.decode("utf-8")
    return sys.stdin.read()

# Words that, in command position, pass command position through to the next
# word (with `-flag` words skipped). Flag-argument forms (`sudo -u <user> yes`,
# `env -u <name> yes`, `nice -n <n> yes`) are a documented gap, not a silent
# allow. `exec`/`eval`/`nice` take their command directly like
# `command`/`builtin`, so their direct forms are modelled here; a bare
# redirect-only `exec 2>/dev/null` with no command word stays allowed because
# no `yes` word follows it.
_TRANSPARENT_PREFIXES = frozenset(
    {"command", "builtin", "env", "time", "sudo", "exec", "eval", "nice"}
)

# Keywords that reset the parser to command position. `in` is deliberately
# absent: after `case X in` / `for v in` the words are patterns/list items,
# never commands. `for`/`select` are absent too: they open a word list
# (loop variable plus `in` items) in their own arm below, so a loop
# variable named `yes` is never read as a command.
_RESET_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "while", "until", "do", "done",
     "esac"}
)

_OPERATOR_RESETS = frozenset(
    {";", "&", "&&", "|", "||", "(", ")", "{", "!", "\n"}
)


def _is_comment_start(text: str, i: int) -> bool:
    """A `#` opens a comment at string start or after whitespace or one of
    `; & | ( { !` or a newline -- never inside a word (`a#b`, `$#`)."""
    if text[i] != "#":
        return False
    if i == 0:
        return True
    # `${#A}` / `${#}` length expansion is not a comment: the `#`
    # directly follows `${`.
    if text[i - 1] == "{" and i >= 2 and text[i - 2] == "$":
        return False
    return text[i - 1] in " \t;|&({\n!"


def _is_keyword_at(text: str, i: int, kw: str) -> bool:
    """True when `kw` starts at `i` on word boundaries (not part of a longer
    word such as `cases` for `case`)."""
    if not text.startswith(kw, i):
        return False
    if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        return False
    j = i + len(kw)
    if j < len(text) and (text[j].isalnum() or text[j] == "_"):
        return False
    return True


def _is_dollar_quote(text: str, i: int) -> bool:
    """True when the `'` at `i` opens `$'...'` ANSI-C quoting: directly
    preceded by an unescaped `$` (an even run of preceding backslashes, so
    `\\$'` is a literal `$` plus plain quotes). Inside `$'...'` a backslash
    escapes the next character, so `\\'` never closes the span. `$$'...'`
    parity is deliberately not modelled: reading it as `$'...'` only
    over-approximates (fail-closed)."""
    if i <= 0 or text[i] != "'" or text[i - 1] != "$":
        return False
    j = i - 2
    while j >= 0 and text[j] == "\\":
        j -= 1
    return (i - 2 - j) % 2 == 0


# Keywords directly followed by a command, so `case`/`esac` after one of
# them is a case boundary (`if case ...`, `then case ...`, `time case ...`
# are all valid Bash). `for`/`select`/`function`/`in` are excluded (followed
# by names or list items), as are `esac`/`fi`/`done` (verified against Bash
# 5.2: a separator is required after them).
_CASE_CMD_WORDS = frozenset(
    {"if", "then", "elif", "else", "while", "until", "do", "time"}
)


def _case_at_cmd_pos(text: str, i: int) -> bool:
    """True when a `case`/`esac` keyword at `i` sits in command position.

    Command position holds at the start of input, after a command separator
    (`;`, `&`, `|`, `(`, `{`, newline, `!`), after a `)` (a pattern close or
    subshell leaves command position), after a command-establishing keyword
    (`if`, `then`, ..., `time`), or after an assignment word (`FOO=1 case`
    is invalid Bash, but tracking it as a case boundary keeps the gate
    fail-closed there; resolved by recursing before the assignment so
    `echo a=b case` still reads as an argument). Anything else is an
    argument or pattern word, never a case boundary.
    """
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0:
        return True
    if text[j] in ";|&({\n!)":
        return True
    if text[j] == ")":
        return True
    if text[j].isalnum() or text[j] == "_":
        k = j
        while k >= 0 and (text[k].isalnum() or text[k] == "_"):
            k -= 1
        if text[k + 1:j + 1] in _CASE_CMD_WORDS:
            return True
        # The word just scanned may be the right-hand side of an assignment
        # (`FOO=1`, `A+=x`): look for the `NAME=` prefix and recurse before
        # it, so only an assignment in command position qualifies.
        if k >= 0 and text[k] == "=":
            m = k - 1
            if m >= 0 and text[m] == "+":
                m -= 1
            e = m
            while e >= 0 and (text[e].isalnum() or text[e] == "_"):
                e -= 1
            if text[e + 1:m + 1].isidentifier():
                return _case_at_cmd_pos(text, e + 1)
    return False


def _strip_comments(text: str) -> str:
    """Blank comment spans outside single/double quotes, keeping newlines so
    line-based command positions survive."""
    out: list[str] = []
    single = double = False
    single_dollar = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if single:
            if single_dollar and ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            out.append(ch)
            if ch == "'":
                single = False
                single_dollar = False
            i += 1
            continue
        if double:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                double = False
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == "'":
            single = True
            single_dollar = _is_dollar_quote(text, i)
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            double = True
            out.append(ch)
            i += 1
            continue
        if ch == "#" and _is_comment_start(text, i):
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _heredoc_delims(line: str) -> list[tuple[str, bool]]:
    """All `(delim, quoted)` pairs for `<<[-]DELIM` operators outside quotes
    and comments on this line, in order. The delimiter parses as a shell
    word: any quoted or backslash-escaped segment marks it quoted and
    contributes its dequoted text (`<<'D'`, `<<"D"`, `<<E'O'F`, `<<\\D`).
    A `<<` mentioned in comment text opens no heredoc. An unresolvable
    delimiter (unterminated quote, trailing line-continuation backslash)
    registers nothing: kept lines are then scanned, which is fail-closed.
    """
    found: list[tuple[str, bool]] = []
    single = double = False
    single_dollar = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if single:
            if single_dollar and ch == "\\":
                i += 2
                continue
            if ch == "'":
                single = False
                single_dollar = False
            i += 1
            continue
        if double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                double = False
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            single = True
            single_dollar = _is_dollar_quote(line, i)
            i += 1
            continue
        if ch == '"':
            double = True
            i += 1
            continue
        if ch == "#" and _is_comment_start(line, i):
            break  # comment text mentions no operator
        if ch == "<" and i + 1 < n and line[i + 1] == "<":
            j = i + 2
            if j < n and line[j] == "-":
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            buf: list[str] = []
            quoted = False
            bad = False
            while j < n and line[j] not in " \t\n|&;()<>":
                c = line[j]
                if c in "'\"":
                    quoted = True
                    quote = c
                    j += 1
                    while j < n and line[j] != quote:
                        buf.append(line[j])
                        j += 1
                    if j >= n:
                        bad = True  # runs past end of line: unresolvable
                        break
                    j += 1
                elif c == "\\" and j + 1 < n:
                    quoted = True
                    buf.append(line[j + 1])
                    j += 2
                elif c == "\\":
                    bad = True  # continuation: delimiter spans lines
                    break
                elif c == "#" and _is_comment_start(line, j):
                    break
                else:
                    buf.append(c)
                    j += 1
            if buf and not bad:
                found.append(("".join(buf), quoted))
                i = j
                continue
            i += 2
            continue
        i += 1
    return found


def _strip_heredoc_bodies(text: str, quoted_only: bool = False) -> str:
    """Drop heredoc CONTENT lines (literal stdin, never commands) while keeping
    the operator lines. With `quoted_only=False` (word analysis) every heredoc
    body is dropped; with `quoted_only=True` (span extraction) only quoted-
    delimiter bodies -- which the shell never expands -- are dropped, so
    substitution text inside unquoted heredocs is still caught. Kept unquoted
    bodies have quote characters and `#` blanked outside substitution spans:
    the shell runs $ and backtick expansions there but gives quotes and `#`
    no semantics, so an apostrophe in prose (`don't`) must not open quote
    state and a `#` must not open a comment that would hide a later
    substitution -- while quotes inside a span keep shell semantics
    (`$(y'es')` still glues, a quoted paren still does not close).
    """
    lines = text.split("\n")
    out: list[str] = []
    kept: list[bool] = []  # kept unquoted-body line: blank later, block-wise
    pending: list[tuple[str, bool]] = []
    for line in lines:
        if pending and line.strip() == pending[0][0]:
            out.append(line)  # the delimiter line itself
            kept.append(False)
            pending.pop(0)
            continue
        if pending:
            _delim, quoted = pending[0]
            if not quoted_only or quoted:
                out.append("")
                kept.append(False)
            else:
                out.append(line)
                kept.append(True)
            continue
        out.append(line)
        kept.append(False)
        pending.extend(_heredoc_delims(line))
    # Blank each contiguous kept-body run as one block so multi-line spans
    # stay intact (a delimiter or operator line always separates bodies).
    # Blanking is length-preserving, so line positions never shift.
    i = 0
    total = len(out)
    while i < total:
        if not kept[i]:
            i += 1
            continue
        j = i
        while j < total and kept[j]:
            j += 1
        out[i:j] = _blank_body_block("\n".join(out[i:j])).split("\n")
        i = j
    return "\n".join(out)


def _skip_balanced(text: str, i: int, opener: str, closer: str) -> int:
    """Skip from an opening bracket at/before i past its match, quote-aware."""
    single = double = False
    single_dollar = False
    depth = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if single:
            if single_dollar and ch == "\\":
                i += 2
                continue
            if ch == "'":
                single = False
                single_dollar = False
            i += 1
            continue
        if double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                double = False
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            single = True
            single_dollar = _is_dollar_quote(text, i)
        elif ch == '"':
            double = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _arithmetic_end(text: str, i: int) -> int | None:
    """Return the end of `((...))` at `i`, or None for subshell syntax.

    Bash tries arithmetic first, but reparses as subshells if the expression
    has no matching `))`. Balance from the SECOND opener: its matching `)`
    must be immediately followed by another `)`. Merely balancing both
    openers and checking the last two bytes is insufficient: `((echo);(yes))`
    ends in `))` but the first expression closes at `);`, so it is subshells.
    """
    end = _skip_balanced(text, i + 1, "(", ")")
    if end < len(text) and text[end] == ")":
        return end + 1
    return None


def _scan_substitution_ranges(
        command: str, prose_outside: bool = False,
) -> list[tuple[int, int, int, int]]:
    """Return `(outer_start, outer_end, body_start, body_end)` for every
    `$(...)` and backtick span in the command. The single span scanner:
    `extract_substitutions` slices bodies from these ranges, and heredoc
    blanking uses them to tell span text from prose, so the two can never
    disagree on a boundary.

    Quote-, escape- and comment-aware. Only a matching arithmetic `))` makes
    `$((` arithmetic; otherwise Bash reparses it as `$(` plus a subshell.
    `$((...))` arithmetic and `${...}` parameter expansion are skipped, but
    their interiors are still scanned for nested `$(...)`/backtick spans
    (e.g. `${A:-$(yes)}`, `$(( $(yes) + 1 ))`). `case` pattern `)` never
    closes a span: `case`/`esac` are tracked only in command position and
    only at their own substitution depth, and extglob groups (`@(...)` and
    siblings) in pattern position are counted so only the `)` back at depth
    zero terminates the pattern. A backslash-newline is deleted before
    scanning (a line continuation), so it splits neither a `$`+`(` opener
    nor, in the word lexer, a command word. `$'...'` honors backslash
    escapes, so `$'\\''` never desyncs the scan. Parens inside double quotes
    never count. Unclosed spans run to end of input (fail-closed: the lexer
    cannot prove the shell would reject them).

    With `prose_outside=True` the text is an unquoted-heredoc body block:
    quotes, `#` and case keywords outside spans are literal prose (the
    shell gives them no semantics there), while `$(`, backtick, `$((` and
    `${` still open spans whose interiors are shell code with full
    semantics -- exactly where Bash parses code versus prose.
    """
    spans: list[tuple[int, int, int, int]] = []
    # Open `$(` spans: (body_start, paren_depth).
    stack: list[tuple[int, int]] = []
    # Saved (single, double, single_dollar) quote contexts, one per `$(`.
    quote_stack: list[tuple[bool, bool, bool]] = []
    # Case-pattern tracking so a pattern `)` never closes a span:
    # (state, substitution depth, extglob depth).
    case_stack: list[tuple[str, int, int]] = []
    single = double = False
    single_dollar = False
    in_backtick = False
    backtick_start = 0
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if single:
            if single_dollar and ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                single = False
                single_dollar = False
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        # In prose (unquoted-heredoc body) quotes and `#` outside spans are
        # literal characters; inside spans they keep shell semantics.
        in_code = bool(stack) or in_backtick or not prose_outside
        if ch == "'" and not double and in_code:
            # Inside double quotes a single quote is a literal character.
            single = True
            single_dollar = _is_dollar_quote(command, i)
            i += 1
            continue
        if ch == '"' and in_code:
            double = not double
            i += 1
            continue
        if ch == "#" and not double and not in_backtick and in_code \
                and _is_comment_start(command, i):
            while i < n and command[i] != "\n":
                i += 1
            continue
        if ch == "`":
            if not in_backtick:
                in_backtick = True
                backtick_start = i + 1
            else:
                in_backtick = False
                spans.append((backtick_start - 1, i + 1,
                              backtick_start, i))
            i += 1
            continue
        # Case keyword tracking (outside quotes/backticks). `case`/`esac`
        # count only in command position (`echo case` is an argument) and
        # only at their own substitution depth, so a nested `$(esac)` never
        # pops the outer case. In prose, `case` words outside spans are
        # literal text, never case boundaries.
        if not double and not in_backtick and in_code:
            if _is_keyword_at(command, i, "case"):
                if _case_at_cmd_pos(command, i):
                    case_stack.append(("subject", len(stack), 0))
                i += 4
                continue
            if _is_keyword_at(command, i, "in"):
                if case_stack and case_stack[-1][0] == "subject" \
                        and len(stack) == case_stack[-1][1] \
                        and (i == 0 or command[i - 1] != "$"):
                    _st, _d, _e = case_stack[-1]
                    case_stack[-1] = ("pattern", _d, _e)
                i += 2
                continue
            if _is_keyword_at(command, i, "esac"):
                if _case_at_cmd_pos(command, i) and case_stack \
                        and len(stack) == case_stack[-1][1]:
                    case_stack.pop()
                i += 4
                continue
            if case_stack and case_stack[-1][0] == "body" \
                    and len(stack) == case_stack[-1][1]:
                if command.startswith(";;&", i):
                    _st, _d, _e = case_stack[-1]
                    case_stack[-1] = ("pattern", _d, _e)
                    i += 3
                    continue
                if command.startswith(";;", i) \
                        or command.startswith(";&", i):
                    _st, _d, _e = case_stack[-1]
                    case_stack[-1] = ("pattern", _d, _e)
                    i += 2
                    continue
        if ch == "$":
            # A line continuation between `$` and `(`/`{` is deleted by the
            # shell before parsing, so look past any backslash-newlines.
            j = i + 1
            while j + 1 < n and command[j] == "\\" \
                    and command[j + 1] == "\n":
                j += 2
            if j < n and command[j] == "(":
                end = _arithmetic_end(command, j) \
                    if command.startswith("((", j) else None
                if end is not None:
                    interior = command[j + 2:end - 2]
                    if interior:
                        # The interior is shell code even in prose mode
                        # (arithmetic containing a substitution), so it
                        # scans with prose_outside=False; ranges shift by
                        # the interior offset. Re-stripping heredocs here
                        # would be a no-op: the caller already stripped.
                        base = j + 2
                        for found in _scan_substitution_ranges(interior):
                            o_s, o_e, b_s, b_e = found
                            spans.append((o_s + base, o_e + base,
                                          b_s + base, b_e + base))
                    i = end
                    continue
                # No arithmetic `))`: Bash reparses `$((` as `$(` whose
                # body starts with a subshell `(`. Let ordinary span depth
                # count that extra opener, preserving any commands inside.
                quote_stack.append((single, double, single_dollar))
                single = False
                double = False
                single_dollar = False
                stack.append((j + 1, 1))
                i = j + 1
                continue
            if j < n and command[j] == "{":
                end = _skip_balanced(command, j, "{", "}")
                interior = command[j + 1:end - 1] \
                    if end - 1 > j + 1 else ""
                if interior:
                    base = j + 1
                    for found in _scan_substitution_ranges(interior):
                        o_s, o_e, b_s, b_e = found
                        spans.append((o_s + base, o_e + base,
                                      b_s + base, b_e + base))
                i = end
                continue
        # An extglob mark directly before `(` in pattern position opens a
        # group whose `)` closes the group, not the pattern.
        if ch in "@?*+!" and not double and not in_backtick and case_stack \
                and case_stack[-1][0] == "pattern" \
                and len(stack) == case_stack[-1][1] \
                and i + 1 < n and command[i + 1] == "(":
            _st, _d, _e = case_stack[-1]
            case_stack[-1] = (_st, _d, _e + 1)
            i += 2
            continue
        if ch == "(" and not double:
            if case_stack and case_stack[-1][0] == "pattern" \
                    and len(stack) == case_stack[-1][1]:
                i += 1  # leading `(` of a case pattern is not grouping
                continue
            if stack:
                start, depth = stack[-1]
                stack[-1] = (start, depth + 1)
            i += 1
            continue
        if ch == ")" and not double:
            if case_stack and case_stack[-1][0] == "pattern" \
                    and len(stack) == case_stack[-1][1]:
                _st, _d, _e = case_stack[-1]
                if _e > 0:
                    case_stack[-1] = (_st, _d, _e - 1)
                else:
                    case_stack[-1] = ("body", _d, _e)
                i += 1
                continue
            if stack:
                start, depth = stack.pop()
                if depth == 1:
                    # start is just past `$(`, so start-2 is the `$`.
                    spans.append((start - 2, i + 1, start, i))
                    if quote_stack:
                        single, double, single_dollar = quote_stack.pop()
                    while case_stack \
                            and case_stack[-1][1] > len(stack):
                        case_stack.pop()
                else:
                    stack.append((start, depth - 1))
            i += 1
            continue
        i += 1
    for start, _depth in stack:
        spans.append((start - 2, n, start, n))
    if in_backtick:
        spans.append((backtick_start - 1, n, backtick_start, n))
    return spans


def extract_substitutions(command: str) -> list[str]:
    """Return the body of every `$(...)` and backtick span in the command.

    Bodies are sliced from `_scan_substitution_ranges`, so extraction and
    heredoc blanking share one span scanner and cannot disagree on a
    boundary. Content lines of quoted-delimiter heredocs are dropped first:
    the shell never expands them, so `$(yes)` written there is literal
    stdin, not a substitution. Kept unquoted-heredoc bodies have quotes
    and `#` blanked outside spans first (only $ and backtick expansions
    are live there), so prose punctuation hides no span while span
    interiors keep shell quoting semantics.
    """
    command = _strip_heredoc_bodies(command, quoted_only=True)
    return [command[b_s:b_e]
            for _o_s, _o_e, b_s, b_e
            in _scan_substitution_ranges(command)]


def _blank_body_block(block: str) -> str:
    """Blank `'`, `"` and `#` outside spans in one kept unquoted-heredoc
    body block, preserving span interiors verbatim.

    The shell runs $ and backtick expansions in the body but gives quotes
    and `#` no semantics there -- except inside a substitution span, whose
    content parses as shell code (glued quoting forms one word, a quoted
    paren does not close the span). Scanning the whole block (not one
    line) keeps multi-line spans intact. Length-preserving: callers split
    the result back into the same number of lines.
    """
    inside = [(o_s, o_e)
              for o_s, o_e, _b_s, _b_e
              in _scan_substitution_ranges(block, prose_outside=True)]
    return "".join(
        " " if c in "'\"#" and not any(o_s <= k < o_e for o_s, o_e in inside)
        else c
        for k, c in enumerate(block))


# Word characters stop a bare word. `#` is intentionally NOT a delimiter:
# comments are pre-stripped, so a surviving `#` is an in-word literal.
# `<`/`>` ARE delimiters: they separate redirects (`2>/dev/null` is `2`,
# `>`, `/dev/null`, never one word).
_WORD_STOPS = " \t\r\n;|&(){}!<>`$'\""


def _consume_quoted(body: str, i: int, dollar: bool = False) -> tuple[str, int]:
    """Consume a quoted section starting at a quote char; return (text, end).

    With `dollar=True` the section is `$'..'` / `$".."` (the `$` is included
    in the returned text). A backslash escapes the next character inside
    `"..."` and inside `$'...'`, so `\\'` never closes a dollar-single span;
    inside plain `'...'` a backslash is literal.
    """
    start = i - 1 if dollar else i
    quote = body[i]
    i += 1
    n = len(body)
    while i < n:
        if body[i] == "\\" and (quote == '"' or dollar):
            i += 2
            continue
        if body[i] == quote:
            return body[start:i + 1], i + 1
        i += 1
    return body[start:], n  # unclosed quote runs out (fail-closed)


def _consume_glued_word(body: str, i: int) -> tuple[str, int]:
    """Consume one full shell word of glued bare/quoted/escaped segments.

    Adjacent segments with no delimiter between them form ONE shell word:
    `ye"s"`, `y'es'`, `"y"es`, `ye''s`, `$'yes'` and `\\y\\e\\s` are each a
    single word that executes as `yes`. Returns (verbatim_text, end).
    """
    parts: list[str] = []
    n = len(body)
    while i < n:
        ch = body[i]
        if ch in "'\"":
            quoted, i = _consume_quoted(body, i)
            parts.append(quoted)
            continue
        if ch == "$" and i + 1 < n and body[i + 1] in "'\"":
            quoted, i = _consume_quoted(body, i + 1, dollar=True)
            parts.append(quoted)
            continue
        if ch == "\\" and i + 1 < n and body[i + 1] == "\n":
            i += 2  # line continuation is deleted; the word continues
            continue
        if ch == "\\" and i + 1 < n and body[i + 1] != "\n":
            parts.append(body[i:i + 2])
            i += 2
            continue
        if ch in " \t\r\n;|&(){}!<>`$":
            break
        parts.append(ch)
        i += 1
    return "".join(parts), i


def _tokenize(body: str) -> list[tuple[str, str]]:
    """Split a substitution body into ("word"|"op", text) tokens.

    Glued bare/quoted/escaped segments form one word token (`ye"s"` is one
    word) for normalization. A bare digits word directly adjacent to a
    redirect (`2>/dev/null`) is an IO-number redirect prefix, not a command,
    and is skipped. Nested `$(...)`, backticks, `$((...))`, `${...}` and
    `<(...)`/`>(...)` spans are skipped whole -- they are analysed as their
    own spans by the extractor. A bare `((...))` likewise skips whole but
    emits a `((` marker word so the body parser can tell a `for ((...))`
    prelude from a missing loop variable.
    """
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(body)
    multi = ("&&", "||", ";;&", ";;", ";&", "<<<", "<<", ">>", "<(", ">(", "<&",
             ">&", "<>", ">|")
    while i < n:
        ch = body[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "\n":
            tokens.append(("op", "\n"))
            i += 1
            continue
        if ch == "\\" and i + 1 < n and body[i + 1] == "\n":
            i += 2  # line continuation is whitespace
            continue
        if ch == "`":
            j = i + 1
            while j < n:
                if body[j] == "\\":
                    j += 2
                    continue
                if body[j] == "`":
                    break
                j += 1
            i = j + 1 if j < n else n
            continue
        if ch == "$" and i + 1 < n and body[i + 1] == "(":
            end = _arithmetic_end(body, i + 1) \
                if body.startswith("((", i + 1) else None
            # Real arithmetic consumes both closing parens. Otherwise this
            # is an ordinary substitution (possibly starting a subshell);
            # its commands are analysed separately by the span extractor.
            i = end if end is not None else _skip_balanced(body, i + 1, "(", ")")
            continue
        if ch == "$" and i + 1 < n and body[i + 1] == "{":
            i = _skip_balanced(body, i + 1, "{", "}")
            continue
        if ch == "$" and i + 1 < n and body[i + 1] in "'\"":
            word, i = _consume_glued_word(body, i)
            tokens.append(("word", word))
            continue
        if ch in "'\"":
            word, i = _consume_glued_word(body, i)
            tokens.append(("word", word))
            continue
        if ch == "(" and i + 1 < n and body[i + 1] == "(":
            # A `for ((...))` arithmetic prelude (or a bare `((...))`
            # compound command): skip it whole, but emit a marker word so
            # `for`'s skip_one consumes the prelude -- setting
            # skip_until_do -- instead of eating a following `do` as the
            # loop variable (`for ((i=0;i<1;i++)) do yes`). Anywhere else
            # the marker is an inert non-`yes` word.
            end = _arithmetic_end(body, i)
            if end is not None:
                i = end
                tokens.append(("word", "(("))
                continue
            # Not arithmetic: fall through to ordinary `(` operators, so
            # `((echo hi); yes)` exposes its command words.
        matched = False
        for op in multi:
            if body.startswith(op, i):
                if op in ("<(", ">("):
                    i = _skip_balanced(body, i + 1, "(", ")")
                else:
                    tokens.append(("op", op))
                    i += len(op)
                matched = True
                break
        if matched:
            continue
        if ch == "{":
            # A `{name}`/`{digits}` group directly adjacent to a redirect
            # (`{fd}>/dev/null`) is a var-fd redirect prefix, not a command:
            # skip it like an IO number. Anything else (`{ echo; }`,
            # `{a,b}`, `{fd} >file` with a space) keeps the old tokens.
            j = i + 1
            while j < n and (body[j].isalnum() or body[j] == "_"):
                j += 1
            name = body[i + 1:j]
            if name and (name.isidentifier() or name.isdigit()) \
                    and j < n and body[j] == "}" \
                    and j + 1 < n and body[j + 1] in "<>":
                i = j + 1
                continue
            tokens.append(("op", ch))
            i += 1
            continue
        if ch in ";|&()}!":
            tokens.append(("op", ch))
            i += 1
            continue
        if ch in "<>":
            tokens.append(("op", ch))
            i += 1
            continue
        if ch == "$":
            # `$VAR` / `$1` / `$@` style expansion: one word, never `yes`.
            j = i + 1
            if j < n and body[j] in "@*#?$!-0123456789":
                j += 1
            elif j < n and (body[j].isalpha() or body[j] == "_"):
                j += 1
                while j < n and (body[j].isalnum() or body[j] == "_"):
                    j += 1
            else:
                tokens.append(("word", "$"))
                i += 1
                continue
            tokens.append(("word", body[i:j]))
            i = j
            continue
        # Bare / glued word (may carry escapes and quoted segments).
        word, ni = _consume_glued_word(body, i)
        if not word:
            # Unconsumable char (should not happen): advance to avoid a loop.
            tokens.append(("word", ch))
            i += 1
            continue
        # A bare digits word directly adjacent to a redirect is an IO number
        # (`2>/dev/null`, `2>&1`): part of the redirect, not a command.
        if word.isdigit() and ni < n and body[ni] in "<>":
            i = ni
            continue
        tokens.append(("word", word))
        i = ni
    return tokens


def _normalize_word(word: str) -> str:
    """Reduce a glued command word to its bare command name for comparison.

    Dequotes segment-wise using shell quoting rules: `'...'` literal,
    `"..."` with backslash unescaping, `$'...'` ANSI-C escapes, `$"..."`
    locale contents, and `\\x` -> `x` outside quotes. A path basename is
    taken last (`/usr/bin/yes` -> `yes`).
    """
    res: list[str] = []
    i = 0
    n = len(word)
    while i < n:
        ch = word[i]
        if ch == "\\" and i + 1 < n:
            res.append(word[i + 1])
            i += 2
            continue
        if ch == "'":
            j = word.find("'", i + 1)
            if j == -1:
                res.append(word[i + 1:])
                break
            res.append(word[i + 1:j])
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                if word[j] == "\\" and j + 1 < n:
                    if word[j + 1] == "\n":
                        j += 2  # line continuation is deleted in quotes
                        continue
                    buf.append(word[j + 1])
                    j += 2
                    continue
                if word[j] == '"':
                    break
                buf.append(word[j])
                j += 1
            res.append("".join(buf))
            i = j + 1 if j < n else n
            continue
        if ch == "$" and i + 1 < n and word[i + 1] in "'\"":
            quote = word[i + 1]
            j = i + 2
            buf = []
            while j < n:
                if word[j] == "\\" and j + 1 < n:
                    esc = word[j + 1]
                    if quote == "'":
                        if esc == "n":
                            buf.append("\n")
                            j += 2
                            continue
                        if esc == "t":
                            buf.append("\t")
                            j += 2
                            continue
                        if esc == "x":
                            hexpart = word[j + 2:j + 4]
                            try:
                                buf.append(chr(int(hexpart, 16)))
                                j += 4
                                continue
                            except ValueError:
                                buf.append(esc)
                                j += 2
                                continue
                        if esc == "u":
                            hexpart = word[j + 2:j + 6]
                            if len(hexpart) == 4:
                                try:
                                    buf.append(chr(int(hexpart, 16)))
                                    j += 6
                                    continue
                                except ValueError:
                                    pass
                            buf.append(esc)
                            j += 2
                            continue
                        if esc == "U":
                            hexpart = word[j + 2:j + 10]
                            if len(hexpart) == 8:
                                try:
                                    buf.append(chr(int(hexpart, 16)))
                                    j += 10
                                    continue
                                except ValueError:
                                    pass
                            buf.append(esc)
                            j += 2
                            continue
                        if esc in "01234567":
                            k = j + 1
                            digits = ""
                            while k < n and len(digits) < 3 \
                                    and word[k] in "01234567":
                                digits += word[k]
                                k += 1
                            try:
                                # Bash stores one byte: values above 255 wrap
                                # (`\571` is 377, byte 121, the letter `y`).
                                buf.append(chr(int(digits, 8) % 256))
                                j = k
                                continue
                            except ValueError:
                                buf.append(esc)
                                j += 2
                                continue
                        buf.append(esc)
                        j += 2
                        continue
                    if word[j + 1] == "\n":
                        j += 2  # line continuation is deleted in quotes
                        continue
                    buf.append(word[j + 1])
                    j += 2
                    continue
                if word[j] == quote:
                    break
                buf.append(word[j])
                j += 1
            res.append("".join(buf))
            i = j + 1 if j < n else n
            continue
        if ch == "$":
            res.append(ch)
            i += 1
            continue
        res.append(ch)
        i += 1
    w = "".join(res)
    if "/" in w:
        w = w.rsplit("/", 1)[-1]
    return w


def _looks_like_assignment(word: str) -> bool:
    """True when the word is a `NAME=value` (or `NAME+=value`) assignment."""
    if "=" not in word:
        return False
    name = word.split("=", 1)[0]
    if name.endswith("+"):
        name = name[:-1]
    return name.isidentifier()


def body_has_unbounded_yes(body: str) -> bool:
    """True when the substitution body would execute `yes` unboundedly."""
    text = _strip_heredoc_bodies(_strip_comments(body))
    tokens = _tokenize(text)
    # Parser state.
    cmd_pos = True       # next word is in command position
    skip_flags = False   # after a transparent prefix, skipping -flags
    skip_one = False     # redirect target / function name / for-var
    for_list = False     # inside a `for v [in words]` word list
    for_in = False       # ... and its `in` word has been seen
    after_sep = False    # previous token was a separator (`;`, newline...)
    case_stack: list[str] = []  # "subject" | "pattern" | "body"
    case_ext = 0         # extglob group depth while in "pattern"
    skip_until_do = False  # `for ((...))` arithmetic prelude
    skip_test = 0        # inside `[[ ... ]]`
    i = 0
    n = len(tokens)
    while i < n:
        kind, val = tokens[i]
        if kind == "op":
            if val == "[[":
                skip_test += 1
                i += 1
                continue
            if skip_test:
                if val == "]]":
                    skip_test -= 1
                i += 1
                continue
            if case_stack and case_stack[-1] == "pattern":
                # Pattern position: words, `|` and an optional leading `(`
                # are patterns, never commands. An extglob mark (`@?*+!`)
                # directly before `(` opens a group whose `)` closes the
                # group; only the `)` back at depth zero enters the case
                # body (`esac` arrives as a word token; see below).
                if val == ")":
                    if case_ext:
                        case_ext -= 1
                    else:
                        case_stack[-1] = "body"
                        cmd_pos = True
                elif val == "(" and i > 0:
                    pk, pv = tokens[i - 1]
                    if (pk == "word" and pv in ("@", "?", "*", "+")) \
                            or (pk == "op" and pv == "!"):
                        case_ext += 1
                i += 1
                continue
            if val in (";;", ";&", ";;&"):
                if case_stack and case_stack[-1] == "body":
                    case_stack[-1] = "pattern"
                cmd_pos = False
                i += 1
                continue
            if val in _OPERATOR_RESETS:
                if val == "\n" and for_list:
                    # A newline inside a `for v in ...` list ends the list:
                    # the words before it are still not commands, but the
                    # next `do` is the loop keyword (Bash 5.2: anything
                    # else there is a syntax error, so nothing executes).
                    after_sep = True
                    i += 1
                    continue
                cmd_pos = True
                skip_flags = False
                skip_one = False
                skip_until_do = False
                for_list = False
                for_in = False
                after_sep = True
                i += 1
                continue
            if val in (">", "<", ">>", "<<", ">&", "<&", "<>", ">|", "<<<"):
                # A redirect (including a leading IO number, already skipped
                # by the tokenizer) names only its target: it never clears
                # command position, so `2>/dev/null yes` still checks `yes`.
                skip_one = True  # redirect target is a filename, not a command
                i += 1
                continue
            # Any other operator (`[[` handled above, `]]`, `(`/`)` balanced
            # leftovers, `=`...): it separates words, position unchanged.
            i += 1
            continue
        # kind == "word"
        prev_was_sep = after_sep
        after_sep = False
        if skip_test:
            i += 1
            continue
        if case_stack and case_stack[-1] == "subject":
            if val == "in":
                case_stack[-1] = "pattern"
            i += 1
            continue
        if case_stack and case_stack[-1] == "pattern":
            # Bare pattern words (with or without a trailing `)`; a glued
            # `)` arrives as its own op token and is handled above).
            # `esac` is not a pattern: it closes the case.
            if val == "esac":
                case_stack.pop()
                case_ext = 0
                cmd_pos = True
            i += 1
            continue
        if skip_until_do:
            if val == "do":
                skip_until_do = False
                cmd_pos = True
            i += 1
            continue
        # Keywords count only in command position and never as a redirect
        # target or other skipped word: `echo case` / `echo esac yes` are
        # plain arguments, so a bare `case` word swallows nothing.
        if val == "case" and cmd_pos and not skip_one:
            case_stack.append("subject")
            case_ext = 0
            cmd_pos = False
            i += 1
            continue
        # `do` terminates a `for`/`select` list only after a separator
        # (newline ends the list like `;`) or with no `in` at all (`for i
        # do`): a `do` inside the list (`for i in do yes`) is an ordinary
        # list word -- Bash 5.2 iterates it, never executes it. A `do`
        # after a `for ((...))` prelude is consumed by skip_until_do above,
        # never by `for`'s skip_one.
        if val in _RESET_KEYWORDS and not skip_one \
                and (cmd_pos or (val == "do" and for_list
                                 and (prev_was_sep or not for_in))):
            # `do` after a `for ((...))` prelude is handled above; a plain
            # `do` resets like the other keywords.
            cmd_pos = True
            skip_flags = False
            skip_one = False
            for_list = False
            i += 1
            continue
        if val in ("for", "select") and cmd_pos and not skip_one:
            skip_one = True  # the loop variable is not a command
            for_list = True
            cmd_pos = False
            i += 1
            continue
        if val == "in" and for_list:
            # `for v in words...`: list items are not commands until a
            # separator-`do` ends the list.
            for_in = True
            cmd_pos = False
            i += 1
            continue
        if val == "function" and cmd_pos and not skip_one:
            skip_one = True  # the function name is not a command
            cmd_pos = False
            i += 1
            continue
        if skip_one:
            skip_one = False
            if val == "((":
                # `for ((...))` prelude: skip words until `do`.
                skip_until_do = True
            i += 1
            continue
        if not cmd_pos:
            i += 1
            continue
        if _looks_like_assignment(val):
            i += 1  # `FOO=bar yes`: still command position after the assignment
            continue
        if _normalize_word(val) in _TRANSPARENT_PREFIXES:
            skip_flags = True
            i += 1
            continue
        if skip_flags:
            if val.startswith("-") and val != "-":
                i += 1
                continue
            skip_flags = False
            # fall through: this word is the prefixed command
        if _normalize_word(val) == "yes":
            return True
        cmd_pos = False
        i += 1
    return False


def check_command(command: str) -> list[str]:
    """Return one FINDING line per offending substitution body (deduplicated)."""
    findings: list[str] = []
    for body in extract_substitutions(command):
        if body_has_unbounded_yes(body):
            excerpt = " ".join(body.split())
            if len(excerpt) > EXCERPT_LEN:
                excerpt = excerpt[:EXCERPT_LEN] + "..."
            line = "%s %s" % (FINDING_NAME, excerpt)
            if line not in findings:
                findings.append(line)
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: scan one command string, file or stdin without running it.

    Returns 0 when clean, 2 on usage errors (including unreadable or
    undecodable input), and 3 when a finding refuses the command.
    With --json, available stdout holds exactly one deterministic JSON object
    and stderr stays empty on every exit; errors use generic codes without
    echoing supplied text, argv or paths. Closed stdout preserves the exit code.
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    # Match argparse's unambiguous --json prefixes, including invalid explicit
    # values, so parse failures cannot fall back to excerpt-bearing text.
    json_mode = any(arg.partition("=")[0] in ("--j", "--js", "--jso", "--json")
                    for arg in raw)
    parser = _GuardParser(
        prog="shell_subst_guard.py",
        description="Refuse an unbounded `yes` in shell command substitution.",
        json_mode=json_mode)
    parser.add_argument("check", nargs="?", help="must be the word `check`")
    parser.add_argument("--json", action="store_true",
                        help="emit one private JSON verdict object "
                        "(schema shell-substitution.v1) with empty stderr")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--command", help="the shell command string to scan")
    group.add_argument("--file", help="file holding the shell command to scan")
    group.add_argument("--stdin", action="store_true",
                       help="read the shell command from stdin")
    args = parser.parse_args(raw)
    if args.check != "check":
        if json_mode:
            _emit_json(_json_input_error(None, "invalid-arguments",
                                         _MSG_INVALID_ARGS,
                                         "correct_arguments"))
            return EXIT_REFUSED
        parser.print_usage(sys.stderr)
        return EXIT_REFUSED
    if args.command is not None:
        input_kind = "command"
        command = args.command
    elif args.file is not None:
        input_kind = "file"
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                command = fh.read()
        except UnicodeDecodeError:
            if json_mode:
                _emit_json(_json_input_error(
                    input_kind, "invalid-utf8", _MSG_BAD_UTF8,
                    "provide_utf8_input"))
                return EXIT_REFUSED
            print("shell_subst_guard: not readable: %s (invalid UTF-8)"
                  % (args.file,), file=sys.stderr)
            return EXIT_REFUSED
        except OSError as exc:
            if json_mode:
                _emit_json(_json_input_error(
                    input_kind, "input-unreadable", _MSG_UNREADABLE,
                    "provide_readable_input"))
                return EXIT_REFUSED
            print("shell_subst_guard: not readable: %s (%s)"
                  % (args.file, exc), file=sys.stderr)
            return EXIT_REFUSED
    else:
        input_kind = "stdin"
        try:
            command = _read_stdin_text()
        except UnicodeDecodeError:
            if json_mode:
                _emit_json(_json_input_error(
                    input_kind, "invalid-utf8", _MSG_BAD_UTF8,
                    "provide_utf8_input"))
                return EXIT_REFUSED
            print("shell_subst_guard: not readable: stdin (invalid UTF-8)",
                  file=sys.stderr)
            return EXIT_REFUSED
        except OSError as exc:
            if json_mode:
                _emit_json(_json_input_error(
                    input_kind, "input-unreadable", _MSG_UNREADABLE,
                    "provide_readable_input"))
                return EXIT_REFUSED
            print("shell_subst_guard: not readable: stdin (%s)"
                  % (exc,), file=sys.stderr)
            return EXIT_REFUSED
    findings = check_command(command)
    if json_mode:
        coded = [{"code": FINDING_NAME} for _ in findings]
        _emit_json(_json_envelope(input_kind, coded, None))
        return EXIT_FINDING if coded else EXIT_OK
    if findings:
        for line in findings:
            print("FINDING %s" % line)
        print("shell_subst_guard: %d finding(s)" % len(findings),
              file=sys.stderr)
        return EXIT_FINDING
    print("shell_subst_guard: clean")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
