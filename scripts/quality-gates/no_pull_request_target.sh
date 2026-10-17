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
# inside the trigger block is a hit (it may resolve to the banned trigger;
# GitHub Actions expands anchors/aliases), as is an unclosed flow bracket at
# the block end (truncated extraction must not read as clean).
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


def strip_comment(line):
    # Quote-aware: a # starts a comment only outside quotes and when at
    # line start or preceded by space/tab.
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            # Ignore escaped quotes inside double quotes.
            if i > 0 and line[i - 1] == "\\":
                continue
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1] in (" ", "\t"):
                return line[:i]
    return line


def _indent_of(code):
    return len(INDENT.match(code).group(0).expandtabs(8))


def _bracket_delta(code):
    # Net [{-open minus }]-close outside single/double quotes. Comment
    # already stripped by the caller. Quote-aware so a bracket inside a
    # step name (outside the block anyway) cannot unbalance the count.
    in_single = in_double = False
    delta = 0
    i = 0
    while i < len(code):
        ch = code[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            if i > 0 and code[i - 1] == "\\":
                i += 1
                continue
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "{[":
                delta += 1
            elif ch in "}]":
                delta -= 1
        i += 1
    return delta


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
    lines never end the block. Fail-closed by construction: anything still
    inside the region counts as a trigger position.
    """
    lines = text.splitlines()
    best = None
    for idx, line in enumerate(lines):
        m = TOP_ON.match(strip_comment(line))
        if m:
            indent = len(m.group(1).expandtabs(8))
            if best is None or indent < best[1]:
                best = (idx, indent)
    if best is None:
        return set()
    start, indent = best
    member = {start + 1}
    depth = _bracket_delta(strip_comment(lines[start]))
    for idx in range(start + 1, len(lines)):
        code = strip_comment(lines[idx])
        if code.strip() == "":
            member.add(idx + 1)
            continue
        if depth <= 0 and _indent_of(code) <= indent and not SEQ_ITEM.match(code):
            break
        member.add(idx + 1)
        depth += _bracket_delta(code)
    return member


def on_block(text):
    lines = text.splitlines()
    member = find_on_block(text)
    if not member:
        return ""
    return "\n".join(strip_comment(lines[i - 1]) for i in sorted(member))


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
            continue
        lines = text.splitlines()
        block = on_block(text)
        if block and TOKEN.search(block):
            # Report the first trigger line for the annotation.
            for lineno in sorted(member):
                raw = lines[lineno - 1]
                # Only report lines inside the trigger block.
                if TOKEN.search(strip_comment(raw)):
                    hits.append(f"{path}:{lineno}: {raw.strip()[:160]}")
                    break
            else:
                hits.append(f"{path}: trigger block contains pull_request_target")
        elif block and ALIAS.search(block):
            # Fail closed: GitHub Actions expands YAML anchors/aliases, so
            # `on: *trig` may resolve to the banned trigger even though the
            # literal token is absent. Any alias in the trigger block is a
            # hit; safe aliases must be inlined instead.
            for lineno in sorted(member):
                raw = lines[lineno - 1]
                if ALIAS.search(strip_comment(raw)):
                    hits.append(f"{path}:{lineno}: trigger block uses YAML "
                                f"alias (may resolve to pull_request_target): "
                                f"{raw.strip()[:120]}")
                    break
            else:
                hits.append(f"{path}: trigger block uses YAML alias "
                            f"(may resolve to pull_request_target)")
        elif block:
            depth = 0
            for chunk in block.splitlines():
                depth += _bracket_delta(chunk)
            if depth != 0:
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
