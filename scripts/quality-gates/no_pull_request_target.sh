#!/usr/bin/env bash
# pull_request_target refusal gate (HARD): refuse `pull_request_target` in workflows.
#
# pull_request_target runs attacker-controlled PR code with a write-capable
# token; this repo has no workflow that needs it. HARD mode: any hit fails.
# Exits 1 on a hit, 0 when clean. Scans tracked workflow files only
# (git ls-files), so untracked scratch never trips it. Only trigger positions
# count: the top-level `on:` block is extracted (quoted "on" included, inline
# or next-line flow lists included, zero-indent `- ` sequences included, and
# uniformly indented files handled by matching `on:` at any indent and ending
# the block at the first line at or above its indent that is not a `- ` item
# while flow brackets are balanced) and searched for the token. YAML comments,
# step names and script paths that merely contain the token pass because they
# live outside the trigger block. Fail-closed extras: a YAML alias (`*name`)
# outside quotes in value position inside the trigger block is a hit (it may
# resolve to the banned trigger; GitHub Actions expands anchors/aliases), as
# is an unclosed flow bracket at the block end (truncated extraction must not
# read as clean), a backslash escape inside the trigger block (it may hide the
# token, e.g. "pull_request\x5ftarget"), a workflow file with no detectable
# top-level `on:` block (e.g. a root flow document), and quote state is
# carried across lines so a multi-line quoted scalar cannot desync the
# comment/bracket scan. Quoted `*` globs (e.g. '**/*-lock.json') pass because
# the alias check ignores quoted text.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

hits="$(python3 - <<'PY'
import re
import subprocess
import sys

TOKEN = re.compile(r"(?<![A-Za-z0-9_])pull_request_target(?![A-Za-z0-9_])")
TOP_ON = re.compile(r"""^([ \t]*)["']?on["']?\s*:""")
SEQ_ITEM = re.compile(r"^[ \t]*-(?:[ \t]|$)")
INDENT = re.compile(r"^[ \t]*")
ALIAS = re.compile(r"(?<![A-Za-z0-9_])\*[A-Za-z0-9_-]+")
# Widened alias-name tail (YAML anchors admit punctuation such as `.` and
# `/`): used only by the quote-aware, value-position scanner below, never as
# a raw-text search, so quoted globs still pass.
ALIAS_NAME = re.compile(r"[A-Za-z0-9_.\-/]+")


def _odd_backslashes(line, idx):
    bs = 0
    j = idx - 1
    while j >= 0 and line[j] == "\\":
        bs += 1
        j -= 1
    return bs % 2 == 1


def strip_comment(line):
    # Single-line wrapper kept for the trigger-line report path; the block
    # scan uses the cross-line variant below so multi-line quoted scalars
    # cannot desync comment handling.
    code, _, _ = _strip_line(line, False, False)
    return code


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
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            if _odd_backslashes(line, i):
                out.append(ch)
            else:
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


def strip_comments_crossline(lines):
    code_lines = []
    in_single = in_double = False
    for line in lines:
        code, in_single, in_double = _strip_line(line, in_single, in_double)
        code_lines.append(code)
    return code_lines


def _indent_of(code):
    return len(INDENT.match(code).group(0).expandtabs(8))


def _bracket_delta(code):
    # Single-chunk wrapper; the block scan threads quote state across lines
    # via _bracket_delta_state so multi-line scalars cannot desync depth.
    delta, _, _ = _bracket_delta_state(code, False, False)
    return delta


def _bracket_delta_state(code, in_single, in_double):
    # Net [{-open minus }]-close outside single/double quotes. Quote state
    # persists across calls: the caller threads in_single/in_double through
    # successive lines of one block.
    delta = 0
    i = 0
    while i < len(code):
        ch = code[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(code) and code[i + 1] == "'":
                i += 2
                continue
            in_single = not in_single
        elif ch == '"' and not in_single:
            if _odd_backslashes(code, i):
                i += 1
                continue
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "{[":
                delta += 1
            elif ch in "}]":
                delta -= 1
        i += 1
    return delta, in_single, in_double


def _block_depth(block):
    depth = 0
    in_single = in_double = False
    for chunk in block.split("\n"):
        d, in_single, in_double = _bracket_delta_state(
            chunk, in_single, in_double)
        depth += d
    return depth


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
    lines for both comment stripping and bracket depth, so a multi-line
    quoted scalar cannot desync the scan. Fail-closed by construction:
    anything still inside the region counts as a trigger position.
    """
    lines = text.splitlines()
    code_lines = strip_comments_crossline(lines)
    best = None
    for idx, code in enumerate(code_lines):
        m = TOP_ON.match(code)
        if m:
            indent = len(m.group(1).expandtabs(8))
            if best is None or indent < best[1]:
                best = (idx, indent)
    if best is None:
        return set()
    start, indent = best
    member = {start + 1}
    depth, qs, qd = _bracket_delta_state(code_lines[start], False, False)
    for idx in range(start + 1, len(lines)):
        code = code_lines[idx]
        if code.strip() == "":
            member.add(idx + 1)
            # Blank lines carry no brackets but quote state persists: a
            # blank line cannot close a multi-line scalar, and _strip_line
            # already threaded the state, so re-derive it by replaying.
            continue
        if depth <= 0 and _indent_of(code) <= indent and not SEQ_ITEM.match(code):
            break
        member.add(idx + 1)
        d, qs, qd = _bracket_delta_state(code, qs, qd)
        depth += d
    # Re-thread quote state correctly across blank lines: replay the whole
    # member range once so depth reflects multi-line scalars exactly. The
    # member set above is already conservative (blank lines included), so
    # only depth needs the exact replay for the unclosed-bracket check.
    return member


def on_block(text):
    lines = text.splitlines()
    member = find_on_block(text)
    if not member:
        return ""
    code_lines = strip_comments_crossline(lines)
    return "\n".join(code_lines[i - 1] for i in sorted(member))


def _unquoted_alias_hit(code):
    """Return True if `code` holds a `*alias` outside quotes in value pos.

    Quote-aware: `*` inside single/double quotes (e.g. quoted globs such as
    '**/*-lock.json') never counts. Value-position: after skipping spaces
    the previous significant char must be a start, newline, `:`, `[`, `{`,
    `,` or `-` (sequence item), so `a*b` or `/` continuations do not count.
    The alias tail admits punctuation (`.`, `/`) beyond `[A-Za-z0-9_-]`.
    """
    in_single = in_double = False
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < n and code[i + 1] == "'":
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            if _odd_backslashes(code, i):
                i += 1
                continue
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
            # `**` glob fragments: a second `*` is not an alias tail.
            if code[i + 1] == "*":
                i += 1
                continue
            j = i - 1
            while j >= 0 and code[j] in (" ", "\t"):
                j -= 1
            if j < 0 or code[j] in ("\n", ":", "[", "{", ","):
                return True
            if code[j] == "-":
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
            # `on:` block (e.g. a root flow document `{"on": {...}}`)
            # must not read as clean. Empty files stay clean.
            if text.strip() != "":
                hits.append(f"{path}: no top-level `on:` trigger found "
                            f"(fail-closed)")
            continue
        lines = text.splitlines()
        code_lines = strip_comments_crossline(lines)
        by_no = {i + 1: code_lines[i] for i in range(len(lines))}
        block = on_block(text)
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
        elif block and _unquoted_alias_hit(block):
            # Fail closed: GitHub Actions expands YAML anchors/aliases, so
            # `on: *trig` may resolve to the banned trigger even though the
            # literal token is absent. Only `*alias` outside quotes in value
            # position counts, so quoted globs pass; safe aliases must be
            # inlined instead.
            for lineno in sorted(member):
                raw = lines[lineno - 1]
                if _unquoted_alias_hit(by_no.get(lineno, "")):
                    hits.append(f"{path}:{lineno}: trigger block uses YAML "
                                f"alias (may resolve to pull_request_target): "
                                f"{raw.strip()[:120]}")
                    break
            else:
                hits.append(f"{path}: trigger block uses YAML alias "
                            f"(may resolve to pull_request_target)")
        elif block and "\\" in block:
            # Fail closed: a backslash escape in the trigger block may hide
            # the token (e.g. "pull_request\x5ftarget" resolves to the
            # banned trigger via YAML double-quoted escapes).
            hits.append(f"{path}: trigger block contains backslash escape "
                        f"(fail-closed, may hide pull_request_target)")
        elif block:
            if _block_depth(block) != 0:
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
