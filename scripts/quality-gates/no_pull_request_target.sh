#!/usr/bin/env bash
# pull_request_target refusal gate (HARD): refuse `pull_request_target` in workflows.
#
# pull_request_target runs attacker-controlled PR code with a write-capable
# token; this repo has no workflow that needs it. HARD mode: any hit fails.
# Exits 1 on a hit, 0 when clean. Scans tracked workflow files only
# (git ls-files), so untracked scratch never trips it. Only trigger positions
# count: the top-level `on:` block is extracted (quoted "on" included,
# leading `&anchor`/`!tag` properties skipped, inline or next-line flow
# lists included, zero-indent `- ` sequences included, and uniformly
# indented files handled by matching `on:` at any indent and ending
# the block at the first line at or above its indent that is not a `- ` item
# while flow brackets are balanced) and searched for the token. YAML comments,
# step names and script paths that merely contain the token pass because they
# live outside the trigger block. Fail-closed extras: a YAML alias (`*name`,
# any anchor-name charset, including a `*`-prefixed name such as `**t`)
# outside quotes in value position inside the trigger block is a hit (it may
# resolve to the banned trigger; GitHub Actions expands anchors/aliases),
# as is an unclosed flow bracket at the block end (truncated extraction must
# not read as clean), a backslash escape inside the trigger block (it may
# hide the token, e.g. "pull_request\x5ftarget"), a workflow file with no
# detectable top-level `on:` block (e.g. a root flow document, indented or
# not: the first content character decides, so a nested decoy `on:` can
# never win for one),
# duplicate `on:` lines at the winning indent, a winning `on:` below column 0
# while column-0 content exists (the real top-level key is disguised and a
# decoy line won), or a key position at the minimum indent holding `\`, an
# unquoted `&`/`!`, a value-position `*alias` or an explicit `?`
# (escaped/aliased/anchored `on:`, including in a uniformly indented file
# where the decoy sits below the hidden key; nested globs and continuations
# are ordinary content). Quote state is carried
# across lines but a quote only opens at a scalar start (anchors/tags admit
# punctuation, verbatim `!<...>` included even when glued to a flow bracket
# (its opening is the nearest `!<` at a token boundary, so an inner `!<`
# inside the URI never wins),
# but only when whitespace-separated from the token and at a node start: a
# quote glued to an anchor/tag name is part of the name, and a property
# glued to, or spaced after, a plain-scalar word (`foo &b`, `foo!<x>`) is
# scalar text, not a property; `-`/`?` count only as standalone indicators
# (an indicator after a word or mid-scalar property is scalar text too);
# `:` is a value start only after whitespace or closing a real quoted key,
# so a colon inside a name, or after a lone plain-scalar apostrophe, is not
# either). A line ending mid plain scalar inside flow pends as a
# continuation for any non-delimiter ending (letters and trailing
# punctuation alike), unless it ends with a complete anchor/tag/alias at
# value position (`key: &a`, any name charset, anchor+tag chains included
# even glued to a pending quoted-key colon, holds its node on the next
# line, whose quotes stay fresh); blank and
# comment-only lines inside flow carry that pending
# state forward; and a `-`/`?`-led chain starting a continuation line is
# plain text, never a fresh node),
# and lines split on "\n" only (U+2028/U+0085 are scalar content, not
# line breaks), so neither a
# multi-line quoted scalar nor an apostrophe in a plain scalar (e.g.
# `description: it's`) can desync the comment/bracket scan. Literal `|`/`>`
# block-scalar content is opaque text: `on:`-looking lines there never win the
# trigger block and quotes/brackets/aliases inside are not structure (the
# token itself is still scanned). Quoted `*` globs (e.g. '**/*-lock.json')
# pass because the alias check ignores quoted text.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

hits="$(python3 - <<'PY'
import re
import subprocess
import sys

TOKEN = re.compile(r"(?<![A-Za-z0-9_])pull_request_target(?![A-Za-z0-9_])")
TOP_ON = re.compile(r"""^([ \t]*)(?:[&!][^\s]*[ \t]+)*["']?on["']?\s*:""")
SEQ_ITEM = re.compile(r"^[ \t]*-(?:[ \t]|$)")
INDENT = re.compile(r"^[ \t]*")
# An alias name runs to whitespace or `,[]{}`: a YAML anchor may hold any
# other character (e.g. `&~t`, `&+t`, `&:t`), so the tail admits punctuation.
# Used only by the quote-aware, value-position scanner below, never as a
# raw-text search, so quoted globs still pass.
ALIAS_NAME = re.compile(r"[^\s,\[\]{}]+")
BLOCK_SCALAR_HEADER = re.compile(r":\s*[|>](?:[1-9])?[+-]?\s*$")


def _odd_backslashes(line, idx):
    bs = 0
    j = idx - 1
    while j >= 0 and line[j] == "\\":
        bs += 1
        j -= 1
    return bs % 2 == 1


def _verbatim_opens(s, end):
    # Candidate `!<` openings for the `>` at index `end`, nearest first. A
    # candidate must sit at a token boundary (line start or after whitespace
    # / `,[]{}` / `:`) and its `!<...>` span must hold no whitespace
    # (verbatim URIs cannot). Nearest-first (not rfind-once, not
    # earliest-first): a second `!<` inside the URI (`!<a!<b,c>`) fails the
    # boundary test so the outer opening wins, while an earlier unrelated
    # `!<` (`!<a>,!<b>`) loses to the nearer real opening instead of
    # spanning the comma. Callers validate the node's own position.
    idx = s.rfind("!<", 0, end)
    while idx != -1:
        if (idx == 0 or s[idx - 1] in " \t,[]{}:") and not any(
                c in " \t" for c in s[idx:end + 1]):
            yield idx
        if idx == 0:
            break
        idx = s.rfind("!<", 0, idx)


def _before_is_node_start(line, s, closed_quote, pending_key=False,
                           in_plain_cur=False):
    # True when index s (the start of an `&anchor` / `!tag` / `!<...>`
    # token) sits at a YAML node start: line start, after a flow indicator
    # (`[`, `{`, `,`), after a mapping colon (`key: &a`, `key:&a`,
    # `"k":&a`), after a standalone `-`/`?` indicator, or after another
    # property (`&a &b "v"`). A token glued to, or separated only by spaces
    # from, a plain-scalar word (`foo &b`, `foo!<x>`) is mid-scalar text,
    # not a property, so a quote after it must not open. When the previous
    # line ended inside a flow plain scalar (`in_plain_cur`), a token at the
    # start of this line is a continuation of that scalar, not a fresh node.
    if in_plain_cur:
        k2 = s - 1
        while k2 >= 0 and line[k2] in (" ", "\t"):
            k2 -= 1
        if k2 < 0:
            return False
    if s <= 0:
        return True
    if line[s - 1] in "[{,":
        return True
    k = s - 1
    while k >= 0 and line[k] in (" ", "\t"):
        k -= 1
    if k < 0:
        return True
    m = k
    prev = None
    if line[k] == ">":
        # A trailing verbatim tag (`!<a,b>`) is one token even though its
        # URI may hold commas: consume the `!<...>` span whole (opening at
        # a token boundary, nearest first) instead of splitting it at its
        # comma. A `>` that is not a verbatim tag (e.g. `&<>`, `a>b`)
        # yields no candidate and falls through to the token walk below.
        for idx in _verbatim_opens(line, k):
            prev = line[idx:k + 1]
            m = idx - 1
            break
    if prev is None:
        while m >= 0 and line[m] not in " \t,[]{}":
            if line[m] == ":" and _quoted_key_colon(line, m, closed_quote,
                                                    pending_key):
                # Stop at the real key colon, not at a quote in a property
                # name.
                break
            m -= 1
        prev = line[m + 1:k + 1]
    if prev == "":
        return True
    if prev[0] in "&!":
        return _before_is_node_start(line, m + 1, closed_quote, pending_key,
                                     in_plain_cur)
    if prev in ("-", "?"):
        if in_plain_cur and (m < 0 or line[:m].strip() == ""):
            # The `-`/`?` leads a token chain at the start of a
            # continuation line (`- &a "p`, `? !!str "p`): YAML reads the
            # whole line as plain text, not a fresh node.
            return False
        # The indicator itself must sit at a node start (recurse, as the
        # `&`/`!` branch does): after a plain-scalar word (`foo - &a "p`)
        # or a mid-scalar property (`foo &a ? !t "p`) it is scalar text,
        # not an indicator, so a quote after the chain must not open.
        return _before_is_node_start(line, m + 1, closed_quote, pending_key,
                                     in_plain_cur)
    if prev.endswith(":"):
        return True
    return False


def _quoted_key_colon(line, colon, closed_quote, pending_key=False):
    # The scan records actual closing quotes, including on a continuation
    # line. Never replay a prefix with fresh state or search for an opener.
    # YAML permits spaces/tabs before a JSON-style key's adjacent value.
    # A colon that starts the line (only indent before it) closes a quoted
    # key from the previous line (`"k"` newline `:"v"` is a valid flow key
    # plus adjacent value): recognise it via the cross-line pending flag.
    k = colon - 1
    while k >= 0 and line[k] in " \t":
        k -= 1
    if k >= 0 and k == closed_quote:
        return True
    if pending_key and k < 0:
        return True
    return False


def _at_scalar_start(line, i, closed_quote, pending_key=False,
                     in_plain_cur=False):
    # A quote opens a YAML scalar only at a value start: line start (after
    # indent), after `[`, `{` or `,`, after a mapping `:` (whitespace before
    # the quote, or JSON-style `"k" :"v"` where the colon follows a real
    # closed quoted key with optional spaces/tabs: a colon inside an anchor/tag
    # name or plain scalar such as `&a:"` or `!:'` is part of the name, not
    # a value start, and neither is a colon after a lone plain-scalar
    # apostrophe such as `foo':"`),
    # after a standalone `-` or `?` indicator (a `-`/`?` glued to, or
    # trailing, a plain-scalar word such as `foo-` is scalar text, not an
    # indicator), or after a YAML anchor (`&name`) or tag
    # (`!name`/`!!name` /`!<verbatim>`) at a node start: `&d "..."`, `&a!
    # "..."`, `&"a" "..."` and `!!str "..."` open a quoted scalar, but a
    # mid-scalar `&b`/`!t`/`!<x>` (as in `foo &b "p`) does not -- otherwise
    # a `}` or `#` inside that string desyncs the comment/bracket scan. The
    # anchor/tag test is a whole-token test (any anchor-name charset,
    # including a trailing `&`/`!` or an embedded quote), never a walk back
    # to the nearest `&`/`!`. A bare word character is NOT a value start, so
    # an apostrophe inside a plain scalar (e.g. `description: it's` or
    # `name: Don't deploy`) is literal text and must not open quote mode.
    # A line that continues a flow plain scalar from the previous line
    # (`in_plain_cur`: `description: foo bar` newline `"p ...`) never opens
    # at its leading token: YAML reads it as plain text (`foo bar "p`).
    # Closing an already-open scalar is always allowed.
    j = i - 1
    while j >= 0 and line[j] in (" ", "\t"):
        j -= 1
    if j < 0:
        if in_plain_cur:
            return False
        return True
    if line[j] in "[{,":
        return True
    if line[j] == ":" and j < i - 1:
        return True
    if line[j] == ":" and _quoted_key_colon(line, j, closed_quote,
                                            pending_key):
        return True
    if (line[j] in "-?" and j < i - 1
            and _before_is_node_start(line, j, closed_quote, pending_key,
                                      in_plain_cur)):
        # The indicator itself must sit at a node start: a
        # space-separated `-`/`?` inside a plain scalar (`foo - "p`) is
        # scalar text, not an indicator.
        return True
    # Verbatim tag `!<...>` directly before the quote: the tag URI may hold
    # `,`, `<`, a second `!<` or quotes (e.g. `!<tag:yaml.org,2002:str>`,
    # `!<(<,>`, `!<?',>`, `!<a,b'c>`, `!<x,y!<z>`), and the tag itself may
    # sit glued to a flow indicator (`[`, `{` or `,` with no space, e.g.
    # `[!<a,b>`, `[z,!<a,b>`, `{!<a,b>`), so take the opening `!<` from the
    # nearest-first boundary candidates (verbatim tags cannot hold
    # whitespace) instead of taking the token from the last whitespace,
    # stopping at the first `<` or quote, or trusting one rfind hit (which
    # may be an inner `!<` inside the URI). The tag must sit at a node
    # start: a `!<x>` glued to, or spaced after, a plain-scalar word (as in
    # `foo!<x> "p` or `foo !<x> "p`) is scalar text, not a tag. A `>` that
    # is not a verbatim tag falls through to the anchor/tag token scan
    # below (e.g. `&<> "..."` is an anchor named `<>`, not a verbatim tag).
    if line[j] == ">":
        for idx in _verbatim_opens(line, j):
            if _before_is_node_start(line, idx, closed_quote,
                                      pending_key, in_plain_cur):
                return True
    # Anchor/tag name directly before the quote: take the whitespace- and
    # flow-delimited token ending at j (an anchor/tag name admits any other
    # character, including `&`, `!`, quotes, `~:+@%` and `<>`, e.g. `&a!`,
    # `&a&`, `&!`, `&"a"`, `&<>`) and accept it when it starts with `&` or
    # `!`. The walk never stops at `&`, `!` or quotes, so a name ending in
    # `&`/`!` or holding a quote still resolves. A quote glued to the token
    # (`&"`, `&'a"`) is part of the anchor/tag name, not a scalar start:
    # only a quote separated from the token by whitespace opens, so `&" "a`
    # keeps the first quote inside the name and opens at the real scalar.
    # Fail closed on an empty token (a quote right after a flow bracket in
    # an odd position).
    if line[i - 1] not in (" ", "\t"):
        return False
    k = j
    while k >= 0 and line[k] not in " \t,[]{}":
        if line[k] == ":" and _quoted_key_colon(line, k, closed_quote,
                                                pending_key):
            # Preserve the same key boundary through chains of properties.
            break
        k -= 1
    token = line[k + 1:j + 1]
    if token == "":
        return True
    if token[0] in "&!":
        return _before_is_node_start(line, k + 1, closed_quote, pending_key,
                                     in_plain_cur)
    return False


def _pending_next_for(code, closed_quote, in_single_end, in_double_end):
    # True when this (comment-stripped) line ends right after a real closing
    # quote: the next line may start with that key's colon (`"k"` newline
    # `:"v"` is a valid flow key plus adjacent value). Only spaces/tabs may
    # sit between the quote and the line end; an open string never pends.
    if in_single_end or in_double_end:
        return False
    stripped = code.rstrip(" \t")
    if not stripped or stripped[-1] not in "\"'":
        return False
    return closed_quote == len(stripped) - 1


def _plain_next_for(code, in_single_end, in_double_end, depth_end,
                    closed_quote=None, pending_key=False):
    # True when this line ends inside a flow plain scalar that may continue
    # on the next line (`description: foo bar%` newline `"p ...` reads as
    # `foo bar% "p` in YAML: a plain scalar may end in almost any
    # non-indicator character). Only tracked inside flow collections;
    # block lines never pend (a dedented `*alias` is a fresh key, not a
    # continuation). Fresh (False): a line ending with a structural
    # delimiter or key colon, with a real closing quote (pending_key
    # territory), with a standalone `-`/`?` indicator, or with a complete
    # anchor/tag/alias at value position (`key: &a` EOL holds its node on
    # the next line, whose quotes must open). Any other scalar ending
    # pends, including bare trailing `&`/`!`/`-`/`?` after scalar text.
    if in_single_end or in_double_end:
        return False
    if depth_end <= 0:
        return False
    stripped = code.rstrip(" \t")
    if not stripped:
        return False
    c = stripped[-1]
    if c in ",:{}[]":
        return False
    if c in "\"'" and closed_quote == len(stripped) - 1:
        # A real closing quote ends the value (pending_key territory).
        # Anything else quote-final (a stray quote char, or a quote glued
        # inside an anchor/tag name such as `&a"`) falls through to the
        # token test below.
        return False
    m = re.search(r"!<[^ \t]*>$|[&*!][^\s,\[\]{}]+$", stripped)
    if m:
        # A complete anchor/tag/alias at value position (`key: &a`, `[!t`,
        # `- *t`) is a complete node or a property awaiting its node: the
        # next line is fresh, so its quotes must open. The property name
        # may end in any character (quote, `&`, `!`, `-`, `?`: `&a!` is an
        # anchor named `a!`), so this test runs before the last-character
        # branches below. After scalar text (`foo &a`) the line may still
        # continue as plain text. A value may also carry an anchor and a
        # tag as a pair (`&a !!str`): walk back over the whole trailing
        # property chain before applying the value-position test. A token
        # starting with `&`/`!` is a property regardless of colons inside
        # it (anchor names may hold `:`, e.g. `&a:b`); only a token that
        # is not a property can be a key boundary. A verbatim `!<...>`
        # span is consumed to its closing `>` before tokenizing, never
        # split on a comma inside its URI; the opening comes from the
        # nearest-first boundary candidates, so an inner `!<` inside the
        # URI cannot win over the real opening. The first property may be
        # glued to its quoted key's colon (`"k":&a !!str`, or a pending
        # quoted key whose colon starts this line): a real quoted-key
        # colon inside the fused token still marks the value boundary.
        j = m.start() - 1
        while j >= 0 and stripped[j] in " \t":
            j -= 1
        while j >= 0:
            if stripped[j] == ">":
                idx = next(_verbatim_opens(stripped, j), None)
                if idx is not None:
                    j = idx - 1
                    while j >= 0 and stripped[j] in " \t":
                        j -= 1
                    continue
            k = j
            while k >= 0 and stripped[k] not in " \t,[]{}":
                k -= 1
            tok = stripped[k + 1:j + 1]
            if not tok or tok[0] not in "&!":
                for c_idx in range(k + 1, j + 1):
                    if stripped[c_idx] != ":":
                        continue
                    if c_idx + 1 > j or stripped[c_idx + 1] not in "&!":
                        continue
                    if _quoted_key_colon(stripped, c_idx, closed_quote,
                                            pending_key):
                        return False
                break
            j = k
            while j >= 0 and stripped[j] in " \t":
                j -= 1
        if j < 0 or stripped[j] in ",{[:?-":
            return False
        return True
    if c in "\"'":
        # A stray quote char is scalar text (e.g. a trailing apostrophe in
        # `foo'`); a real closing quote returned above.
        return True
    if c in "-?":
        # Standalone at value position (whole line, or after an opening
        # delimiter, key colon or explicit key) is fresh; after scalar
        # text (`foo -`) it is plain text and continues.
        j = len(stripped) - 2
        while j >= 0 and stripped[j] in " \t":
            j -= 1
        if j < 0 or stripped[j] in ",{[:?":
            return False
        return True
    if c in "&!":
        # Bare (naming nothing): awaiting its name at value position
        # (`key: &` EOL) is fresh; after scalar text (`foo &`) it is
        # plain text and continues.
        j = len(stripped) - 2
        while j >= 0 and stripped[j] in " \t":
            j -= 1
        if j < 0 or stripped[j] in ",{[:?-":
            return False
        return True
    return True


def _scan_line(line, in_single=False, in_double=False, pending_key=False,
               in_plain=False, depth=0):
    # One quote-state machine for comments, flow depth, aliases and literal
    # headers. mask preserves positions outside quoted scalars; quoted bytes
    # are spaces. closed_quote is an actual transition in this scan, never a
    # recursive replay with a different initial state. Quote state is carried
    # across lines; closed_quote resets per line while `pending_key` (the
    # previous line ended after a quoted key) and `in_plain` (the previous
    # line ended in a flow plain scalar) cross the boundary. `depth` is the
    # flow depth before this line; the returned depth includes this line.
    # Returns (code, mask, in_single, in_double, pending_next, in_plain_next,
    # depth_end).
    mask = list(line)
    closed_quote = None
    in_plain_cur = bool(in_plain and depth > 0)
    i = 0
    while i < len(line):
        ch = line[i]
        was_quoted = in_single or in_double
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(line) and line[i + 1] == "'":
                mask[i] = mask[i + 1] = " "
                i += 2
                continue
            if in_single:
                in_single = False
                closed_quote = i
            elif _at_scalar_start(line, i, closed_quote, pending_key,
                                  in_plain_cur):
                in_single = True
        elif ch == '"' and not in_single:
            if not _odd_backslashes(line, i):
                if in_double:
                    in_double = False
                    closed_quote = i
                elif _at_scalar_start(line, i, closed_quote, pending_key,
                                      in_plain_cur):
                    in_double = True
        elif ch == "#" and not was_quoted:
            if i == 0 or line[i - 1] in (" ", "\t"):
                code = line[:i]
                m = "".join(mask[:i])
                delta = m.count("{") + m.count("[") - m.count("}") - m.count("]")
                depth_end = depth + delta
                if code.strip() == "" and depth > 0:
                    # Blank or comment-only line inside flow: separation
                    # space, not a boundary; carry the incoming
                    # cross-line state forward unchanged.
                    return (code, m, in_single, in_double, pending_key,
                            in_plain, depth_end)
                pending_next = _pending_next_for(code, closed_quote,
                                                 in_single, in_double)
                in_plain_next = _plain_next_for(code, in_single, in_double,
                                                depth_end, closed_quote,
                                                pending_key)
                return (code, m, in_single, in_double, pending_next,
                        in_plain_next, depth_end)
        elif ch in ",:{}[]" and not was_quoted and not in_single \
                and not in_double:
            in_plain_cur = False
        if was_quoted or in_single or in_double:
            mask[i] = " "
        i += 1
    m = "".join(mask)
    delta = m.count("{") + m.count("[") - m.count("}") - m.count("]")
    depth_end = depth + delta
    if line.strip() == "" and depth > 0:
        # Blank line inside flow: separation space, not a boundary; carry
        # the incoming cross-line state forward unchanged.
        pending_next, in_plain_next = pending_key, in_plain
    else:
        pending_next = _pending_next_for(line, closed_quote, in_single,
                                         in_double)
        in_plain_next = _plain_next_for(line, in_single, in_double,
                                        depth_end, closed_quote,
                                        pending_key)
    return line, m, in_single, in_double, pending_next, in_plain_next, depth_end


def _strip_line(line, in_single, in_double, pending_key, in_plain, depth):
    code, _, in_single, in_double, pending_key, in_plain, depth = _scan_line(
        line, in_single, in_double, pending_key, in_plain, depth)
    return code, in_single, in_double, pending_key, in_plain, depth


def strip_comments_crossline(lines, literal):
    # Comment-strip every line, threading quote state across lines. Lines
    # inside a `|`/`>` literal block are opaque text: they are passed through
    # unchanged and cannot open or close an outer quoted scalar.
    code_lines = []
    in_single = in_double = False
    pending_key = in_plain = False
    depth = 0
    for idx, line in enumerate(lines):
        if literal[idx]:
            code_lines.append(line)
            continue
        code, in_single, in_double, pending_key, in_plain, depth = _strip_line(
            line, in_single, in_double, pending_key, in_plain, depth)
        code_lines.append(code)
    return code_lines


def _indent_of(code):
    return len(INDENT.match(code).group(0).expandtabs(8))


def _bracket_delta_state(code, in_single, in_double, pending_key, in_plain,
                         depth):
    _, mask, in_single, in_double, pending_key, in_plain, depth_end = \
        _scan_line(code, in_single, in_double, pending_key, in_plain, depth)
    delta = mask.count("{") + mask.count("[") - mask.count("}") - mask.count("]")
    return delta, in_single, in_double, pending_key, in_plain, depth_end


def _block_depth(pairs):
    # Net flow-bracket depth over (code, is_literal) block lines. Literal
    # content is opaque text: its brackets are not structure and its quotes
    # never touch the threaded quote state.
    depth = 0
    in_single = in_double = False
    pending_key = in_plain = False
    for code, is_lit in pairs:
        if is_lit:
            continue
        d, in_single, in_double, pending_key, in_plain, depth_end = \
            _bracket_delta_state(code, in_single, in_double, pending_key,
                                 in_plain, depth)
        depth += d
    return depth


def _literal_flags(lines):
    # Only a header outside quoted scalars AND flow collections can make
    # subsequent indented lines opaque. Use the same cross-line scan as
    # comment stripping; a plain-scalar apostrophe cannot forge a header,
    # and a continuation's real closing quote must always be consumed.
    flags = [False] * len(lines)
    header_indent = None
    in_single = in_double = False
    pending_key = in_plain = False
    depth = 0
    for idx, line in enumerate(lines):
        if header_indent is not None:
            if line.strip() == "" or _indent_of(line) > header_indent:
                flags[idx] = True
                continue
            header_indent = None
        _, mask, in_single, in_double, pending_key, in_plain, depth_end = \
            _scan_line(line, in_single, in_double, pending_key, in_plain,
                       depth)
        depth = depth_end
        if (depth == 0 and not in_single and not in_double
                and BLOCK_SCALAR_HEADER.search(mask)):
            header_indent = _indent_of(line)
    return flags


def _strip_leading_props(s):
    # Consume leading YAML node properties (`&anchor`, `!tag`, `!!tag`,
    # `!h!suffix`, `!<verbatim>`) plus whitespace, returning the remainder.
    # Verbatim URIs hold no whitespace, so a whitespace split is exact.
    rest = s
    while True:
        rest = rest.lstrip(" \t")
        if rest.startswith("!<"):
            end = rest.find(">")
            if end == -1:
                break
            rest = rest[end + 1:]
            continue
        m = re.match(r"[&!]\S*", rest)
        if m:
            rest = rest[m.end():]
            continue
        break
    return rest.lstrip(" \t")


def find_on_candidates(text):
    """Split text into lines/code/literal flags plus TOP_ON candidates.

    Returns (lines, code_lines, literal, cands) where cands holds
    (line index, indent) for every non-literal `on:`-looking line.
    """
    # A leading UTF-8 BOM is not content: strip it so `on:`, `---`, `%YAML`
    # and root flow documents resolve (str.strip() keeps U+FEFF).
    # Split on "\n" only: str.splitlines() also splits on U+2028/U+2029/U+0085
    # (and \x0b/\x0c/\x1c-\x1e), which YAML 1.2 treats as ordinary scalar
    # content, so a separator inside a quoted scalar would forge a phantom
    # column-0 line that ends the trigger block early. The file is read in
    # text mode, so universal newlines already translated \r\n and \r.
    text = text.lstrip("﻿")
    lines = text.split("\n")
    literal = _literal_flags(lines)
    code_lines = strip_comments_crossline(lines, literal)
    cands = []
    for idx, code in enumerate(code_lines):
        if literal[idx] or code.strip() == "":
            continue
        m = TOP_ON.match(code)
        if m:
            cands.append((idx, len(m.group(1).expandtabs(8))))
    return lines, code_lines, literal, cands


def find_on_block(text):
    """Return 1-based line numbers inside the top-level `on:` trigger block.

    The `on:` line with the smallest indent wins, so a uniformly indented
    file still resolves its one top-level mapping. Collection ends at the
    first later line at or above that indent that is not a `- ` sequence
    item and arrives with flow brackets balanced: in YAML a block sequence
    may sit at the same indent as its key, so `- push` at column 0
    continues the block while `jobs:` ends it, and a top-level flow mapping
    (`on: {push: {},` + `pull_request_target: {}}` with the continuation at
    column 0) stays inside until its brackets close. Blank and comment-only
    lines never end the block. Quote state (single/double) is carried across
    lines for both comment stripping and bracket depth, but quotes only open
    at a scalar start, so a multi-line quoted scalar or a plain-scalar
    apostrophe cannot desync the scan. Fail-closed by construction:
    anything still inside the region counts as a trigger position, and a
    winning `on:` below column 0 while column-0 content exists (a disguised
    top-level key plus a decoy line) yields no block at all.
    """
    lines, code_lines, literal, cands = find_on_candidates(text)
    for code in code_lines:
        stripped = code.strip().lstrip("﻿")
        if stripped == "":
            continue
        if stripped.startswith("%"):
            # A `%YAML`/`%TAG` directive precedes the document: not content.
            continue
        if (stripped == "---" or stripped.startswith("--- ")
                or stripped.startswith("---\t") or stripped == "..."):
            # A document marker before any content is not content either.
            # Content after `---` on the same line decides: `--- {` is a
            # root flow document. Node properties on the marker line
            # (`--- &r`, `--- !!map`) are not content either: skip them and
            # decide on what follows, continuing when only properties sit
            # on the marker line.
            rest = stripped[3:].strip()
            if rest == "":
                continue
            rest = _strip_leading_props(rest)
            if rest == "":
                continue
            if rest[0] in ("{", "["):
                return set()
            break
        if stripped[0] in ("&", "!"):
            # A node property on its own line (`&r`, `!!map`, `&r !!map`)
            # before a root flow document is not content: skip it and decide
            # on the next content line. Properties glued to content
            # (`&t on:`, `&r {`) fall through to the remainder test below.
            rest = _strip_leading_props(stripped)
            if rest == "":
                continue
            if rest[0] in ("{", "["):
                return set()
            break
        if stripped[0] in ("{", "["):
            # A root flow document (`{"on": {...}}`, possibly indented, and
            # possibly behind a directive/`---`/property preamble skipped
            # above) has no `on:` line at a trustworthy position: any
            # `on:`-looking line is a nested decoy while the real trigger
            # sits mid-line where the extractor never looks, so yield no
            # block (the caller fails this closed). An indented root has no
            # column-0 content, which is exactly what let the decoy win.
            return set()
        break
    if not cands:
        return set()
    best = min(indent for _, indent in cands)
    if best > 0 and any(code.strip() != "" and _indent_of(code) == 0
                        for code in code_lines):
        # The document has top-level content but its `on:` key was not
        # recognized (escaped, aliased or explicit `?` key) while a decoy
        # `on:`-looking line matched below column 0: the real trigger would
        # go unscanned, so yield no block (the caller fails this closed).
        return set()
    start = next(idx for idx, indent in cands if indent == best)
    indent = best
    member = {start + 1}
    depth, qs, qd, qp, qpl, depth_end = _bracket_delta_state(
        code_lines[start], False, False, False, False, 0)
    depth = depth_end
    for idx in range(start + 1, len(lines)):
        code = code_lines[idx]
        if code.strip() == "":
            member.add(idx + 1)
            continue
        if depth <= 0 and _indent_of(code) <= indent and not SEQ_ITEM.match(code):
            break
        member.add(idx + 1)
        if literal[idx]:
            continue
        d, qs, qd, qp, qpl, depth_end = _bracket_delta_state(
            code, qs, qd, qp, qpl, depth)
        depth += d
    return member


def _suspicious_top_key(code_lines, literal):
    """Return a report line if any key hides the trigger, else None.

    A top-level `on:` written with a YAML escape (`"\\x6fn"`), through an
    alias (`*k : ...`), with a node property (`&t on:`, `!!str on:`) or as
    an explicit `?` key never matches TOP_ON without its property prefix.
    If any other `on:`-looking line exists it would win as a clean decoy,
    so fail closed when a backslash, an unquoted `&`/`!`, a value-position
    `*alias` or an explicit `?` key appears in key position (text before
    the first unquoted `:`) at the minimum indent -- the document's
    top-level indent, so a uniformly indented file keeps its coverage while
    nested globs (`- docs/**`), nested continuations (`ls *.txt`) and
    multi-line flow fragments are ordinary content. Ordinary quoted keys
    (`"on":`) hold none of these and pass.
    """
    nonblank = [c for c, lit in zip(code_lines, literal)
                if not lit and c.strip() != ""]
    if not nonblank:
        return None
    min_indent = min(_indent_of(c) for c in nonblank)
    in_single = in_double = False
    pending_key = in_plain = False
    depth = 0
    for code, is_lit in zip(code_lines, literal):
        if is_lit:
            continue
        _, mask, in_single, in_double, pending_key, in_plain, depth = \
            _scan_line(code, in_single, in_double, pending_key, in_plain,
                       depth)
        if code.strip() == "" or _indent_of(code) != min_indent:
            continue
        if mask.lstrip(" \t").startswith("?"):
            return code.strip()[:120]
        quoted = [a != b for a, b in zip(code, mask)]
        end = len(code)
        for k, ch in enumerate(code):
            if ch == ":" and not quoted[k]:
                end = k
                break
        if end == len(code):
            # No key colon on a top-level line: a colon-less sequence item
            # or continuation cannot hide `on:` (`?` handled above).
            continue
        key, key_quoted = code[:end], quoted[:end]
        if "\\" in key:
            return code.strip()[:120]
        for k, ch in enumerate(key):
            if ch in "&!" and not key_quoted[k]:
                return code.strip()[:120]
        for k, ch in enumerate(key):
            if ch == "*" and not key_quoted[k]:
                if k > 0 and re.match(r"[A-Za-z0-9_]", key[k - 1]):
                    continue
                m = ALIAS_NAME.match(key, k + 1)
                if not m:
                    continue
                # No `**` glob skip: a value-position `*` always starts an
                # alias (an anchor name may itself start with `*`, e.g.
                # `&*t` / `**t`), while mid-scalar globs (`docs/**`) are
                # already excluded by the value-position test below and an
                # unquoted scalar cannot start with `*` at all.
                j = k - 1
                while j >= 0 and key[j] in (" ", "\t"):
                    j -= 1
                if j < 0 or key[j] in (":", "[", "{", ","):
                    return code.strip()[:120]
                if key[j] in ("-", "?"):
                    return code.strip()[:120]
    return None


def _unquoted_alias_hit(pairs):
    """Return True if the block holds a `*alias` outside quotes in value pos.

    Quote-aware: `*` inside single/double quotes (e.g. quoted globs such as
    '**/*-lock.json') never counts, and quotes only open at a scalar start
    so a plain-scalar apostrophe cannot blind the scan. Value-position: the
    previous significant char on the line must be a line start, `:`, `[`,
    `{`, `,`, `-` (sequence item) or `?` (explicit key), so `a*b` does not
    count. The alias tail runs to whitespace or `,[]{}` because anchor names
    admit punctuation. Literal block-scalar lines are opaque text and never
    count (an alias cannot live inside a literal string).
    """
    in_single = in_double = False
    pending_key = in_plain = False
    depth = 0
    for code, is_lit in pairs:
        if is_lit:
            continue
        _, mask, in_single, in_double, pending_key, in_plain, depth = \
            _scan_line(code, in_single, in_double, pending_key, in_plain,
                       depth)
        i = 0
        n = len(mask)
        while i < n:
            ch = mask[i]
            if ch == "*":
                if i > 0 and re.match(r"[A-Za-z0-9_]", code[i - 1]):
                    i += 1
                    continue
                m = ALIAS_NAME.match(code, i + 1)
                if not m:
                    i += 1
                    continue
                # No `**` glob skip: a value-position `*` always starts an
                # alias (an anchor name may itself start with `*`, e.g.
                # `&*t` / `**t`), while mid-scalar globs (`docs/**`) are
                # already excluded by the value-position test below and an
                # unquoted scalar cannot start with `*` at all.
                j = i - 1
                while j >= 0 and code[j] in (" ", "\t"):
                    j -= 1
                if j < 0 or code[j] in (":", "[", "{", ","):
                    return True
                if code[j] in ("-", "?"):
                    return True
                i += 1
                continue
            i += 1
    return False


def main():
    proc = subprocess.run(
        ["git", "ls-files", "-z",
         ".github/workflows/*.yml", ".github/workflows/*.yaml"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if proc.returncode != 0:
        # Fail closed: a failed file lookup must never read as clean.
        sys.stderr.write(proc.stderr)
        sys.exit(2)
    files = [f for f in proc.stdout.split("\0") if f]
    hits = []
    for path in files:
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        member = find_on_block(text)
        if not member:
            # Fail closed: a workflow file with no detectable top-level
            # `on:` block (e.g. a root flow document `{"on": {...}}`, or a
            # disguised top-level key masked by a decoy line) must not read
            # as clean. Empty files stay clean.
            if text.strip() != "":
                hits.append(f"{path}: no top-level `on:` trigger found "
                            f"(fail-closed)")
            continue
        lines, code_lines, literal, cands = find_on_candidates(text)
        best = min(indent for _, indent in cands)
        if sum(1 for _, indent in cands if indent == best) > 1:
            # Fail closed: two `on:` lines at the winning indent is either
            # a duplicate top-level key or a decoy next to the real one.
            hits.append(f"{path}: duplicate `on:` trigger lines "
                        f"(fail-closed)")
            continue
        by_no = {i + 1: code_lines[i] for i in range(len(lines))}
        pairs = [(code_lines[i - 1], literal[i - 1]) for i in sorted(member)]
        block = "\n".join(code for code, _ in pairs)
        if block and TOKEN.search(block):
            # Report the first trigger line for the annotation.
            for lineno in sorted(member):
                raw = lines[lineno - 1]
                # Only report lines inside the trigger block.
                if TOKEN.search(by_no.get(lineno, "")):
                    hits.append(f"{path}:{lineno}: {raw.strip()[:160]}")
                    break
            else:
                hits.append(f"{path}: trigger block contains pull_request_target")
        elif block and _unquoted_alias_hit(pairs):
            # Fail closed: GitHub Actions expands YAML anchors/aliases, so
            # `on: *trig` may resolve to the banned trigger even though the
            # literal token is absent. Only `*alias` outside quotes in value
            # position counts, so quoted globs pass; safe aliases must be
            # inlined instead.
            for lineno in sorted(member):
                if literal[lineno - 1]:
                    continue
                raw = lines[lineno - 1]
                if _unquoted_alias_hit([(by_no.get(lineno, ""), False)]):
                    hits.append(f"{path}:{lineno}: trigger block uses YAML "
                                f"alias (may resolve to pull_request_target): "
                                f"{raw.strip()[:120]}")
                    break
            else:
                hits.append(f"{path}: trigger block uses YAML alias "
                            f"(may resolve to pull_request_target)")
        elif block and _suspicious_top_key(code_lines, literal):
            # Fail closed: a column-0 key position holding a backslash, an
            # unquoted `*` or an explicit `?` is an escaped/aliased top-level
            # `on:` that the block extractor cannot see.
            hits.append(f"{path}: top-level key hides trigger via "
                        f"escape/alias/explicit key (fail-closed): "
                        f"{_suspicious_top_key(code_lines, literal)}")
        elif block and "\\" in block:
            # Fail closed: a backslash escape in the trigger block may hide
            # the token (e.g. "pull_request\x5ftarget" resolves to the
            # banned trigger via YAML double-quoted escapes).
            hits.append(f"{path}: trigger block contains backslash escape "
                        f"(fail-closed, may hide pull_request_target)")
        elif block:
            if _block_depth(pairs) != 0:
                # Fail closed: truncated flow extraction must not read as
                # clean (e.g. an unclosed `{`/`[` at the block end).
                hits.append(f"{path}: trigger block has unclosed flow "
                            f"bracket (fail-closed)")
    for hit in hits:
        print(hit)


main()
PY
)"

if [[ -n "$hits" ]]; then
  echo "::error::pull_request_target is banned (refusal gate, HARD):"
  printf '%s\n' "$hits"
  exit 1
fi
echo "no-pull_request_target gate: clean"
