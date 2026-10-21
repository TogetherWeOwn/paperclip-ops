#!/usr/bin/env python3
"""Subprocess-timeout gate (WARN): subprocess calls without timeout= outside tests.

Runs `ast-grep scan` with the vendored shared rule
sg-rules/python/py-subprocess-timeout.yml (see sg-rules/VENDORED.md), then
diffs the per-file finding counts against the committed backlog
(docs/quality-gates/subprocess-timeout-backlog.txt, one `path: count` row
per file). New files with findings, or files whose count grew past the
backlog, are emitted as GitHub `::warning::` annotations. Exit is ALWAYS 0
in warn mode: this gate warns, never fails (WARN mode per the card).
--write-backlog exits nonzero when the scan fails, so a failed scan never
wipes the backlog to a header-only file.

Requires sgconfig.yml, sg-rules/python/py-subprocess-timeout.yml and the
backlog to be present; until they land the gate reports scan-unavailable
(rc 0, no findings) and --write-backlog refuses.

Backlog regeneration (after a deliberate fix sweep, reviewed like code):
    python3 scripts/quality-gates/subprocess_timeout_gate.py --write-backlog

Exclusion set (mirrors sgconfig.yml): basenames starting with `test_` or
ending with `_test.py`, or any path part named `test`/`tests`. Everything
else scans, including eval/ and verification/ harnesses: they run in CI and
can hang it too.

Usage:
    python3 scripts/quality-gates/subprocess_timeout_gate.py [--ast-grep BIN]
        [--backlog PATH] [--write-backlog]
Stdlib only.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BACKLOG = (
    REPO_ROOT / "docs" / "quality-gates" / "subprocess-timeout-backlog.txt"
)
RULE_ID = "py-subprocess-timeout"


def is_test_path(path: str) -> bool:
    parts = Path(path).parts
    if any(p in ("test", "tests") for p in parts):
        return True
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def read_backlog(backlog: Path) -> dict:
    counts = {}
    if not backlog.exists():
        return counts
    for line in backlog.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, count = line.rpartition(":")
        counts[path.strip()] = int(count.strip())
    return counts


def scan(ast_grep: str) -> dict | None:
    # Returns None when the scan could not run (binary missing, timeout,
    # non-0/1 exit, unparseable output): callers must not treat that as a
    # clean empty result. A failed scan wiping the backlog (or reading as
    # clean) would hide every finding on the next run.
    # ast-grep skips hidden dirs by default: --no-ignore hidden keeps the
    # gha-* rules over .github/workflows and .github/scripts running.
    try:
        proc = subprocess.run(
            [ast_grep, "scan", "--config", "sgconfig.yml",
             "--no-ignore", "hidden", "--json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"::warning::ast-grep scan failed ({type(exc).__name__}): "
              f"{str(exc)[:500]}")
        return None
    if proc.returncode not in (0, 1):
        print(f"::warning::ast-grep scan failed (exit {proc.returncode}): "
              f"{proc.stderr.strip()[:500]}")
        return None
    try:
        findings = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(f"::warning::ast-grep output unparseable: {exc}")
        return None
    counts = {}
    for finding in findings:
        if finding.get("ruleId") != RULE_ID:
            continue
        found = Path(finding["file"])
        if not found.is_absolute():
            found = REPO_ROOT / found
        rel = str(found.relative_to(REPO_ROOT))
        if is_test_path(rel):
            continue
        counts[rel] = counts.get(rel, 0) + 1
    return counts


def compare(current: dict, backlog: dict) -> list:
    """Return warning lines for new files and grown counts."""
    warnings = []
    for path in sorted(current):
        old = backlog.get(path, 0)
        new = current[path]
        if old == 0:
            warnings.append(
                f"{path}: {new} subprocess call(s) without timeout= "
                f"(new file, not in backlog)"
            )
        elif new > old:
            warnings.append(
                f"{path}: subprocess-without-timeout grew "
                f"{old} -> {new}"
            )
    return warnings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ast-grep", default="ast-grep")
    parser.add_argument("--backlog", default=str(DEFAULT_BACKLOG))
    parser.add_argument("--write-backlog", action="store_true")
    args = parser.parse_args(argv)

    current = scan(args.ast_grep)
    if args.write_backlog:
        if current is None:
            print("::error::subprocess-timeout gate: scan failed, "
                  "refusing to rewrite backlog",
                  file=sys.stderr)
            return 2
        backlog_path = Path(args.backlog)
        backlog_path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# subprocess-timeout gate backlog: tracked non-test files with",
                 "# subprocess calls lacking timeout=, as `path: count`.",
                 "# Regenerate only in a reviewed PR that shrinks it.",
                 ""]
        lines += [f"{p}: {current[p]}" for p in sorted(current)]
        backlog_path.write_text("\n".join(lines) + "\n")
        print(f"wrote {len(current)} backlog rows to {backlog_path}")
        return 0

    if current is None:
        # WARN mode stays rc 0, but never present a failed scan as clean:
        # skip the backlog comparison entirely (scan() already warned).
        print("subprocess-timeout gate: scan unavailable, "
              "skipping backlog comparison")
        return 0
    backlog = read_backlog(Path(args.backlog))
    warnings = compare(current, backlog)
    print(f"subprocess-timeout gate: {len(current)} files with findings, "
          f"{len(warnings)} new/grown vs backlog")
    for warning in warnings:
        print(f"::warning::{warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
