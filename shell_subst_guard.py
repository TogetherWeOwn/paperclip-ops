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
"""

from __future__ import annotations

import argparse
import sys

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_FINDING = 3

FINDING_NAME = "unbounded-yes-substitution"
EXCERPT_LEN = 160

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
# never commands.
_RESET_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "while", "until", "do", "done",
     "select", "esac"}
)

_OPERATOR_RESETS = frozenset({";", "&", "&&", "|", "||", "(", "{", "!", "\n"})


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


def _strip_comments(text: str) -> str:
    """Blank comment spans outside single/double quotes, keeping newlines so
    line-based command positions survive."""
    out: list[str] = []
    single = double = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if single:
            out.append(ch)
            if ch == "'":
                single = False
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
    on this line, in order. `quoted` is True for `<<'D'` / `<<"D"`."""
    found: list[tuple[str, bool]] = []
    single = double = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if single:
            if ch == "'":
                single = False
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
            i += 1
            continue
        if ch == '"':
            double = True
            i += 1
            continue
        if ch == "<" and i + 1 < n and line[i + 1] == "<":
            j = i + 2
            if j < n and line[j] == "-":
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            if j < n and line[j] in "'\"":
                quote = line[j]
                j += 1
                start = j
                while j < n and line[j] != quote:
                    j += 1
                if j < n:
                    found.append((line[start:j], True))
                    i = j + 1
                    continue
                i = n
                continue
            start = j
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            if j > start:
                found.append((line[start:j], False))
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
    substitution text inside unquoted heredocs is still caught.
    """
    lines = text.split("\n")
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in lines:
        if pending and line.strip() == pending[0][0]:
            out.append(line)  # the delimiter line itself
            pending.pop(0)
            continue
        if pending:
            _delim, quoted = pending[0]
            out.append("" if (not quoted_only or quoted) else line)
            continue
        out.append(line)
        pending.extend(_heredoc_delims(line))
    return "\n".join(out)


def _skip_balanced(text: str, i: int, opener: str, closer: str) -> int:
    """Skip from an opening bracket at/before i past its match, quote-aware."""
    single = double = False
    depth = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if single:
            if ch == "'":
                single = False
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


def extract_substitutions(command: str) -> list[str]:
    """Return the body of every `$(...)` and backtick span in the command.

    Quote-, escape- and comment-aware. `$((...))` arithmetic and `${...}`
    parameter expansion are not command substitutions and are skipped, but
    their interiors are still scanned for nested `$(...)`/backtick spans
    (e.g. `${A:-$(yes)}`, `$(( $(yes) + 1 ))`). `case` pattern `)` never
    closes a span. Unclosed spans run to end of input (fail-closed: the
    lexer cannot prove the shell would reject them). Content lines of
    quoted-delimiter heredocs are dropped first: the shell never expands
    them, so `$(yes)` written there is literal stdin, not a substitution.
    """
    command = _strip_heredoc_bodies(command, quoted_only=True)
    spans: list[str] = []
    # Open `$(` spans: (body_start, paren_depth).
    stack: list[tuple[int, int]] = []
    # Saved (single, double) quote contexts, one per open `$(`.
    quote_stack: list[tuple[bool, bool]] = []
    # Case-pattern tracking so a pattern `)` never closes a span.
    case_stack: list[str] = []  # "subject" | "pattern" | "body"
    single = double = False
    in_backtick = False
    backtick_start = 0
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if single:
            if ch == "'":
                single = False
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and not double:
            # Inside double quotes a single quote is a literal character.
            single = True
            i += 1
            continue
        if ch == '"':
            double = not double
            i += 1
            continue
        if ch == "#" and not double and not in_backtick \
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
                spans.append(command[backtick_start:i])
            i += 1
            continue
        # Case keyword tracking (outside quotes/backticks). Tracked at every
        # level so `case` at top level and nested `case` inside a substitution
        # both nest correctly; only a `)` while in "pattern" is special.
        if not double and not in_backtick:
            if _is_keyword_at(command, i, "case"):
                case_stack.append("subject")
                i += 4
                continue
            if case_stack and case_stack[-1] == "subject" \
                    and _is_keyword_at(command, i, "in"):
                case_stack[-1] = "pattern"
                i += 2
                continue
            if _is_keyword_at(command, i, "esac"):
                if case_stack:
                    case_stack.pop()
                i += 4
                continue
            if case_stack and case_stack[-1] == "body":
                if command.startswith(";;&", i):
                    case_stack[-1] = "pattern"
                    i += 3
                    continue
                if command.startswith(";;", i) \
                        or command.startswith(";&", i):
                    case_stack[-1] = "pattern"
                    i += 2
                    continue
        nxt = command[i + 1] if i + 1 < n else ""
        if ch == "$" and nxt == "(":
            if i + 2 < n and command[i + 2] == "(":
                end = _skip_balanced(command, i + 2, "(", ")")
                interior = command[i + 3:end - 1] if end - 1 > i + 3 else ""
                if interior:
                    spans.extend(extract_substitutions(interior))
                i = end
                continue
            quote_stack.append((single, double))
            single = False
            double = False
            stack.append((i + 2, 1))
            i += 2
            continue
        if ch == "$" and nxt == "{":
            end = _skip_balanced(command, i + 1, "{", "}")
            interior = command[i + 2:end - 1] if end - 1 > i + 2 else ""
            if interior:
                spans.extend(extract_substitutions(interior))
            i = end
            continue
        if ch == "(" and stack:
            if case_stack and case_stack[-1] == "pattern":
                i += 1  # leading `(` of a case pattern is not grouping
                continue
            start, depth = stack[-1]
            stack[-1] = (start, depth + 1)
            i += 1
            continue
        if ch == ")" and stack:
            if case_stack and case_stack[-1] == "pattern":
                case_stack[-1] = "body"
                i += 1
                continue
            start, depth = stack.pop()
            if depth == 1:
                spans.append(command[start:i])
                if quote_stack:
                    single, double = quote_stack.pop()
            else:
                stack.append((start, depth - 1))
            i += 1
            continue
        i += 1
    for start, _depth in stack:
        spans.append(command[start:])
    if in_backtick:
        spans.append(command[backtick_start:])
    return spans


# Word characters stop a bare word. `#` is intentionally NOT a delimiter:
# comments are pre-stripped, so a surviving `#` is an in-word literal.
# `<`/`>` ARE delimiters: they separate redirects (`2>/dev/null` is `2`,
# `>`, `/dev/null`, never one word).
_WORD_STOPS = " \t\r\n;|&(){}!<>`$'\""


def _consume_quoted(body: str, i: int) -> tuple[str, int]:
    """Consume a quoted section starting at a quote char; return (text, end).

    Handles `$'..'` / `$".."` by including a directly preceding `$`.
    """
    start = i
    if i > 0 and body[i - 1] == "$":
        start = i - 1
    quote = body[i]
    i += 1
    n = len(body)
    while i < n:
        if body[i] == "\\" and quote == '"':
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
            quoted, i = _consume_quoted(body, i + 1)
            parts.append(quoted)
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
    own spans by the extractor.
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
            if i + 2 < n and body[i + 2] == "(":
                i = _skip_balanced(body, i + 2, "(", ")")
            else:
                i = _skip_balanced(body, i + 1, "(", ")")
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
            i = _skip_balanced(body, i, "(", ")")
            continue
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
        if ch in ";|&(){}!":
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
                        if esc in "01234567":
                            k = j + 1
                            digits = ""
                            while k < n and len(digits) < 3 \
                                    and word[k] in "01234567":
                                digits += word[k]
                                k += 1
                            try:
                                buf.append(chr(int(digits, 8)))
                                j = k
                                continue
                            except ValueError:
                                buf.append(esc)
                                j += 2
                                continue
                        buf.append(esc)
                        j += 2
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
    case_stack: list[str] = []  # "subject" | "pattern" | "body"
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
                # are patterns, never commands. A closing `)` enters the
                # case body (`esac` arrives as a word token; see below).
                if val == ")":
                    case_stack[-1] = "body"
                    cmd_pos = True
                elif val == "(":
                    pass
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
                    # A newline inside a `for v in ...` list (before `do`)
                    # does not end the list; its words are still not commands.
                    i += 1
                    continue
                cmd_pos = True
                skip_flags = False
                skip_one = False
                skip_until_do = False
                for_list = False
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
                cmd_pos = True
            i += 1
            continue
        if skip_until_do:
            if val == "do":
                skip_until_do = False
                cmd_pos = True
            i += 1
            continue
        if val == "case":
            case_stack.append("subject")
            cmd_pos = False
            i += 1
            continue
        if val in _RESET_KEYWORDS:
            # `do` after a `for ((...))` prelude is handled above; a plain
            # `do` resets like the other keywords.
            cmd_pos = True
            skip_flags = False
            skip_one = False
            for_list = False
            i += 1
            continue
        if val in ("for", "select"):
            skip_one = True  # the loop variable is not a command
            for_list = True
            cmd_pos = False
            i += 1
            continue
        if val == "in" and for_list:
            # `for v in words...`: list items are not commands until `do`.
            cmd_pos = False
            i += 1
            continue
        if val == "function":
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
    """
    parser = argparse.ArgumentParser(
        prog="shell_subst_guard.py",
        description="Refuse an unbounded `yes` in shell command substitution.")
    parser.add_argument("check", nargs="?", help="must be the word `check`")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--command", help="the shell command string to scan")
    group.add_argument("--file", help="file holding the shell command to scan")
    group.add_argument("--stdin", action="store_true",
                       help="read the shell command from stdin")
    args = parser.parse_args(argv)
    if args.check != "check":
        parser.print_usage(sys.stderr)
        return EXIT_REFUSED
    if args.command is not None:
        command = args.command
    elif args.file is not None:
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                command = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print("shell_subst_guard: not readable: %s (%s)"
                  % (args.file, exc), file=sys.stderr)
            return EXIT_REFUSED
    else:
        try:
            command = sys.stdin.read()
        except (OSError, UnicodeDecodeError) as exc:
            print("shell_subst_guard: not readable: stdin (%s)"
                  % (exc,), file=sys.stderr)
            return EXIT_REFUSED
    findings = check_command(command)
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
