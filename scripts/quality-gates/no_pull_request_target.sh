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
# punctuation, verbatim `!<...>` included), so neither a
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


def _at_scalar_start(line, i):
    # A quote opens a YAML scalar only at a value start: line start (after
    # indent), after `[`, `{` or `,`, after `:` (with or without the space:
    # JSON-style `"k":"v"` is a value start too), after `-` or `?` followed
    # by a space, or after a YAML anchor (`&name`) or tag (`!name`/`!!name`
    # /`!<verbatim>`): `&d "..."`, `&a! "..."`, `&"a" "..."` and `!!str "..."`
    # open a quoted scalar, otherwise a `}` or `#` inside that string
    # desyncs the comment/bracket scan. The anchor/tag test is a whole-token
    # test (any anchor-name charset, including a trailing `&`/`!` or an
    # embedded quote), never a walk back to the nearest `&`/`!`. A bare word
    # character is NOT a value start, so
    # an apostrophe inside a plain scalar (e.g. `description: it's` or
    # `name: Don't deploy`) is literal text and must not open quote mode.
    # Closing an already-open scalar is always allowed.
    j = i - 1
    while j >= 0 and line[j] in (" ", "\t"):
        j -= 1
    if j < 0:
        return True
    if line[j] in "[{,":
        return True
    if line[j] == ":":
        return True
    if line[j] in "-?" and j < i - 1:
        return True
    # Verbatim tag `!<...>` directly before the quote: the tag URI may hold
    # `,` (e.g. `!<tag:yaml.org,2002:str>`), which would split a generic
    # token scan, so handle it here. A `>` that is not a verbatim tag falls
    # through to the anchor/tag token scan below (e.g. `&<> "..."` is an
    # anchor named `<>`, not a verbatim tag).
    if line[j] == ">":
        k = j - 1
        while k >= 0 and line[k] not in (" ", "\t", '"', "'"):
            if line[k] == "<":
                if k - 1 >= 0 and line[k - 1] == "!":
                    return True
                break
            k -= 1
    # Anchor/tag name directly before the quote: take the whitespace- and
    # flow-delimited token ending at j (an anchor/tag name admits any other
    # character, including `&`, `!`, quotes, `~:+@%` and `<>`, e.g. `&a!`,
    # `&a&`, `&!`, `&"a"`, `&<>`) and accept it when it starts with `&` or
    # `!`. The walk never stops at `&`, `!` or quotes, so a name ending in
    # `&`/`!` or holding a quote still resolves. Fail closed on an empty
    # token (a quote right after a flow bracket in an odd position).
    k = j
    while k >= 0 and line[k] not in " \t,[]{}":
        k -= 1
    token = line[k + 1:j + 1]
    if token == "":
        return True
    return token[0] in "&!"


def _strip_line(line, in_single, in_double):
    # Strip a YAML `#` comment, quote-aware. in_single/in_double persist
    # across lines: a double-quoted (or single-quoted) scalar opened on a
    # previous line stays open, so `#` on the continuation line is literal
    # text, not a comment. `''` inside single quotes is an escaped quote;
    # `\"` (odd backslashes) inside double quotes is escaped.
    out = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(line) and line[i + 1] == "'":
                out.append(ch)
                out.append(line[i + 1])
                i += 2
                continue
            if in_single or _at_scalar_start(line, i):
                in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            if _odd_backslashes(line, i):
                out.append(ch)
            else:
                if in_double or _at_scalar_start(line, i):
                    in_double = not in_double
                out.append(ch)
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1] in (" ", "\t"):
                break
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out), in_single, in_double


def strip_comments_crossline(lines, literal):
    # Comment-strip every line, threading quote state across lines. Lines
    # inside a `|`/`>` literal block are opaque text: they are passed through
    # unchanged and cannot open or close an outer quoted scalar.
    code_lines = []
    in_single = in_double = False
    for idx, line in enumerate(lines):
        if literal[idx]:
            code_lines.append(line)
            continue
        code, in_single, in_double = _strip_line(line, in_single, in_double)
        code_lines.append(code)
    return code_lines


def _indent_of(code):
    return len(INDENT.match(code).group(0).expandtabs(8))


def _bracket_delta_state(code, in_single, in_double):
    # Net [{-open minus }]-close outside single/double quotes. Quote state
    # persists across calls: the caller threads in_single/in_double through
    # successive lines of one block. Quotes only open at a scalar start (see
    # _at_scalar_start) so a plain-scalar apostrophe cannot desync depth.
    delta = 0
    i = 0
    while i < len(code):
        ch = code[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(code) and code[i + 1] == "'":
                i += 2
                continue
            if in_single or _at_scalar_start(code, i):
                in_single = not in_single
        elif ch == '"' and not in_single:
            if _odd_backslashes(code, i):
                i += 1
                continue
            if in_double or _at_scalar_start(code, i):
                in_double = not in_double
        elif not in_single and not in_double:
            if ch in "{[":
                delta += 1
            elif ch in "}]":
                delta -= 1
        i += 1
    return delta, in_single, in_double


def _block_depth(pairs):
    # Net flow-bracket depth over (code, is_literal) block lines. Literal
    # content is opaque text: its brackets are not structure and its quotes
    # never touch the threaded quote state.
    depth = 0
    in_single = in_double = False
    for code, is_lit in pairs:
        if is_lit:
            continue
        d, in_single, in_double = _bracket_delta_state(
            code, in_single, in_double)
        depth += d
    return depth


def _mask_quotes(line):
    # Replace characters inside single/double quotes with spaces (fresh quote
    # state per line). Used only to spot block-scalar headers: missing a
    # header merely treats those lines as normal text (today's behaviour),
    # never as clean.
    out = list(line)
    in_single = in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(line) and line[i + 1] == "'":
                out[i] = out[i + 1] = " "
                i += 2
                continue
            in_single = not in_single
            out[i] = " "
        elif ch == '"' and not in_single:
            if not _odd_backslashes(line, i):
                in_double = not in_double
            out[i] = " "
        elif in_single or in_double:
            out[i] = " "
        i += 1
    return "".join(out)


def _is_block_scalar_header(line):
    # `key: |` / `key: >-` / `key: |2` (trailing comment allowed): the `|`
    # or `>` must sit in value position outside quotes.
    masked = _mask_quotes(line)
    for k, ch in enumerate(masked):
        if ch == "#" and (k == 0 or masked[k - 1] in (" ", "\t")):
            masked = masked[:k]
            break
    return bool(BLOCK_SCALAR_HEADER.search(masked))


def _literal_flags(lines):
    # True for lines inside a `|`/`>` literal/folded block scalar. Their
    # content is opaque text, so `on:`-looking lines there are not trigger
    # candidates and quotes/brackets/aliases inside are not structure.
    # Missing a header (or over-matching one) can only move a hit between
    # fail-closed messages, never to clean: skipped `on:` lines shrink the
    # candidate set toward the fail-closed empty set.
    flags = [False] * len(lines)
    header_indent = None
    for idx, line in enumerate(lines):
        if header_indent is not None:
            if line.strip() == "" or _indent_of(line) > header_indent:
                flags[idx] = True
                continue
            header_indent = None
        if _is_block_scalar_header(line):
            header_indent = _indent_of(line)
    return flags


def find_on_candidates(text):
    """Split text into lines/code/literal flags plus TOP_ON candidates.

    Returns (lines, code_lines, literal, cands) where cands holds
    (line index, indent) for every non-literal `on:`-looking line.
    """
    lines = text.splitlines()
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
        stripped = code.strip()
        if stripped == "":
            continue
        if stripped[0] in ("{", "["):
            # A root flow document (`{"on": {...}}`, possibly indented) has
            # no `on:` line at a trustworthy position: any `on:`-looking
            # line is a nested decoy while the real trigger sits mid-line
            # where the extractor never looks, so yield no block (the
            # caller fails this closed). An indented root has no column-0
            # content, which is exactly what let the decoy win.
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
    depth, qs, qd = _bracket_delta_state(code_lines[start], False, False)
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
        d, qs, qd = _bracket_delta_state(code, qs, qd)
        depth += d
    return member


def _quote_states(code):
    # Per-character quote map for one line (fresh state): True where the
    # character sits inside single/double quotes. Quote opens use the same
    # scalar-start rule as the block scan.
    inside = [False] * len(code)
    in_single = in_double = False
    i = 0
    while i < len(code):
        ch = code[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(code) and code[i + 1] == "'":
                inside[i] = inside[i + 1] = True
                i += 2
                continue
            if in_single or _at_scalar_start(code, i):
                in_single = not in_single
        elif ch == '"' and not in_single:
            if _odd_backslashes(code, i):
                inside[i] = in_double
            elif in_double or _at_scalar_start(code, i):
                in_double = not in_double
        else:
            inside[i] = in_single or in_double
        i += 1
    return inside


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
    for code, is_lit in zip(code_lines, literal):
        if is_lit or code.strip() == "":
            continue
        if _indent_of(code) != min_indent:
            continue
        if code.lstrip(" \t").startswith("?"):
            return code.strip()[:120]
        quoted = _quote_states(code)
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
    for code, is_lit in pairs:
        if is_lit:
            continue
        i = 0
        n = len(code)
        while i < n:
            ch = code[i]
            if ch == "'" and not in_double:
                if in_single and i + 1 < n and code[i + 1] == "'":
                    i += 2
                    continue
                if in_single or _at_scalar_start(code, i):
                    in_single = not in_single
                i += 1
                continue
            if ch == '"' and not in_single:
                if _odd_backslashes(code, i):
                    i += 1
                    continue
                if in_double or _at_scalar_start(code, i):
                    in_double = not in_double
                i += 1
                continue
            if ch == "*" and not in_single and not in_double:
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
            with open(path, encoding="utf-8", errors="replace") as fh:
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
