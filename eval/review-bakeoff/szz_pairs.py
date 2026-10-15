#!/usr/bin/env python3
"""Mine "merged PR that later needed a fix" pairs from a squash-merge history.

This is the ground-truth source for the review-bot bake-off (docs/review-bot/
bakeoff-protocol.md, section 3). It is a deliberately plain SZZ pass so the
dataset is reproducible and not hand-picked:

  1. A FIX commit is a first-parent, non-merge commit on the base branch whose
     subject is a Conventional Commit of type `fix` or `revert` and ends in
     `(#N)` (GitHub squash-merge).
  2. For every line the fix deleted or rewrote, `git blame` the pre-fix parent
     to find the commit that last wrote that line.
  3. A blamed commit that is itself a squash-merge `(#M)` older than the fix is
     the INTRODUCING PR. Lines are counted per (introducing PR, fix PR) and
     split into source vs test lines.

It does NOT decide that the introducing PR was *wrong* in a way a reviewer could
see -- that needs a human reading the fix PR. The output is a ranked candidate
list; `verified` stays false until someone adds a defect description.

Pure stdlib. Read-only against the repo (git log / diff-tree / blame only).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict

PR_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")
FIX_SUBJECT = re.compile(r"^(fix|revert)(\([^)]*\))?!?:", re.IGNORECASE)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) \d+ \d+")


def git(repo: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def is_test_path(path: str) -> bool:
    p = path.lower()
    return (
        p.startswith(("test/", "tests/", "e2e/"))
        or "/test/" in p
        or "/tests/" in p
        or ".test." in p
        or ".spec." in p
    )


def is_noise_path(path: str) -> bool:
    p = path.lower()
    return p.endswith((".md", ".lock", ".svg", ".png", ".json")) or "package-lock" in p


def list_commits(repo: str, ref: str) -> list[dict]:
    out = git(repo, "log", "--first-parent", "--no-merges", "--format=%H%x09%ct%x09%s", ref)
    commits = []
    for line in out.splitlines():
        sha, ct, subject = line.split("\t", 2)
        m = PR_SUFFIX.search(subject)
        commits.append(
            {"sha": sha, "ct": int(ct), "subject": subject, "pr": int(m.group(1)) if m else None}
        )
    return commits


def deleted_ranges(repo: str, sha: str) -> dict[str, list[tuple[int, int]]]:
    """Old-side (path -> [(start, count)]) line ranges the commit removed/rewrote."""
    diff = git(repo, "diff-tree", "-p", "-U0", "--no-renames", "--no-commit-id", "-r", sha)
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    path = None
    in_header = False
    for line in diff.splitlines():
        # A deleted SQL/Lua comment ("-- x") renders as "--- x"; only trust
        # ---/+++ lines between a `diff --git` line and the first hunk.
        if line.startswith("diff --git "):
            in_header, path = True, None
        elif in_header and line.startswith("--- "):
            path = None if line == "--- /dev/null" else line[6:]
        elif in_header and line.startswith("+++ "):
            continue
        elif line.startswith("@@"):
            in_header = False
            m = HUNK.match(line)
            if m and path:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                if count > 0:
                    ranges[path].append((start, count))
    return ranges


def blame_lines(repo: str, parent: str, path: str, start: int, count: int) -> list[tuple[str, int, str]]:
    """(commit, line number in THAT commit's version of the file, path at that commit)."""
    out = git(
        repo,
        "blame",
        "--porcelain",
        "-L",
        f"{start},{start + count - 1}",
        parent,
        "--",
        path,
        check=False,
    )
    names: dict[str, str] = {}
    current: tuple[str, int] | None = None
    result = []
    for line in out.splitlines():
        m = BLAME_HEADER.match(line)
        if m:
            current = (m.group(1), int(line.split()[1]))
        elif line.startswith("filename ") and current:
            names[current[0]] = line[len("filename "):]
        elif line.startswith("\t") and current:
            result.append((current[0], current[1], names.get(current[0], path)))
            current = None
    return result


def added_lines(repo: str, sha: str) -> int:
    total = 0
    for line in git(repo, "show", "--numstat", "--format=", sha).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            total += int(parts[0])
    return total


def mine(repo: str, ref: str, max_lag_days: float, max_intro_added: int) -> list[dict]:
    commits = list_commits(repo, ref)
    by_sha = {c["sha"]: c for c in commits}
    pairs: dict[tuple[int, int], dict] = {}
    for fix in commits:
        if fix["pr"] is None or not FIX_SUBJECT.match(fix["subject"]):
            continue
        parent = git(repo, "rev-parse", f"{fix['sha']}^", check=False).strip()
        if not parent.startswith(tuple("0123456789abcdef")) or len(parent) != 40:
            continue  # root commit: nothing before it to blame
        for path, spans in deleted_ranges(repo, fix["sha"]).items():
            if is_noise_path(path):
                continue
            kind = "test" if is_test_path(path) else "src"
            for start, count in spans:
                for blamed, orig_line, intro_path in blame_lines(repo, parent, path, start, count):
                    intro = by_sha.get(blamed)
                    if not intro or intro["pr"] is None or intro["pr"] == fix["pr"]:
                        continue
                    lag_days = (fix["ct"] - intro["ct"]) / 86400.0
                    if lag_days < 0 or lag_days > max_lag_days:
                        continue
                    rec = pairs.setdefault(
                        (intro["pr"], fix["pr"]),
                        {
                            "introducing_pr": intro["pr"],
                            "introducing_sha": intro["sha"],
                            "introducing_subject": intro["subject"],
                            "fix_pr": fix["pr"],
                            "fix_sha": fix["sha"],
                            "fix_subject": fix["subject"],
                            "lag_days": round(lag_days, 2),
                            "src_lines": 0,
                            "test_lines": 0,
                            "files": set(),
                            "locations": set(),
                        },
                    )
                    rec["src_lines" if kind == "src" else "test_lines"] += 1
                    rec["files"].add(path)
                    # Where the defective line sat in the introducing PR's own tree,
                    # which is what a reviewer of that PR can point at.
                    rec["locations"].add((intro_path, orig_line, kind))
    result = []
    for rec in pairs.values():
        rec["introducing_added_lines"] = added_lines(repo, rec["introducing_sha"])
        if rec["introducing_added_lines"] > max_intro_added:
            continue
        rec["files"] = sorted(rec["files"])
        rec["locations"] = [
            {"file": f, "line": n, "kind": k} for f, n, k in sorted(rec["locations"])
        ][:400]
        rec["category"] = "functional" if rec["src_lines"] else "ci-break"
        rec["verified"] = False
        result.append(rec)
    result.sort(key=lambda r: (-r["src_lines"], -r["test_lines"], r["introducing_pr"]))
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="path to a full (non-shallow) clone")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--max-lag-days", type=float, default=14.0)
    ap.add_argument("--max-intro-added", type=int, default=800,
                    help="skip introducing PRs adding more lines than this (prompt-size cap)")
    ap.add_argument("--json", dest="json_out", help="write the candidate list here")
    args = ap.parse_args(argv)
    if git(args.repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        print("refusing a shallow clone: blame would attribute every line to the boundary commit",
              file=sys.stderr)
        return 2
    pairs = mine(args.repo, args.ref, args.max_lag_days, args.max_intro_added)
    text = json.dumps(pairs, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(f"{len(pairs)} candidate pairs "
          f"({sum(1 for p in pairs if p['category'] == 'functional')} functional, "
          f"{sum(1 for p in pairs if p['category'] == 'ci-break')} ci-break)", file=sys.stderr)
    for p in pairs[:25]:
        print(f"#{p['introducing_pr']:>4} -> fix #{p['fix_pr']:>4}  src={p['src_lines']:>3} "
              f"test={p['test_lines']:>3} lag={p['lag_days']:>5}d add={p['introducing_added_lines']:>4}  "
              f"{p['introducing_subject'][:60]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
