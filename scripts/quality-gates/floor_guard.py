#!/usr/bin/env python3
"""Floor guard for CONSTRAINTS.md (HARD).

Diff-scoped enforcement of the quality floor: fails when a change adds
suppressions, skips or deletes tests, strips assertions, or moves a
threshold the wrong way (rows marked must-not-grow fail on growth, all
others on decrease). Adapted from the constraint-driven-development skill's
references/floor-guard.md reference implementation; exit codes are unchanged,
with path-safe Git collection and patterns extended
for this repo's Python+bash stack. Git C-quoted patch paths are decoded once;
deletions use NUL framing and removed lines retain their old-side paths.
Renames remain delete+add, including moves between test filenames.

Scope: suppression/skip/stub patterns apply to code files only (.py, .sh,
.js/.mjs/.ts, workflow .yml/.yaml). Suppression names mentioned in markdown
prose (notably CONSTRAINTS.md documenting the floor itself) are exempt:
a suppression comment only functions in code, never in docs. Threshold and
exception checks apply to CONSTRAINTS.md; assertion/deletion checks apply
to test files (test_*.py/.sh, *_test.py, *.test./*.spec.js, test/tests/).

Contract: exit 0 clean, 1 at least one floor violation (block the change),
2 the guard could not run (no merge base, not a git repo). Never let a 2
read as a 0. Reports the rule and the location, never a secret value.

Usage:
    python3 scripts/quality-gates/floor_guard.py [--base REF]
        [--ignore-file PATH]
Stdlib only. Run: python3 -m unittest test_floor_guard
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BASE = "origin/main"
DEFAULT_IGNORE = REPO_ROOT / ".constraintsignore"

CODE_EXTENSIONS = frozenset({".py", ".sh", ".js", ".mjs", ".ts", ".tsx", ".yml", ".yaml"})

SUPPRESSIONS = re.compile(
    r"@ts-ignore|@ts-nocheck|eslint-disable|biome-ignore"
    r"|#\s*noqa|#\s*type:\s*ignore"
    r"|istanbul ignore|nosemgrep|gitleaks:allow|Stryker disable"
    r"|shellcheck disable|hadolint ignore|pylint:\s*disable"
    r"|zizmor:\s*ignore|ast-grep-ignore"
)
# Tracked audit-silencer exceptions (reviewed like code): exact
# (path, comment-line) pairs whose suppression is load-bearing and already
# scoped. The review-trigger workflow_run ignore is the only one: it is
# scoped to workflows:[CI] + types:[completed] with a conclusion==success
# job guard (reviewed exception, see REVIEWED_SUPPRESSION_LINES). Matching is equality on the stripped added line, so
# the same text appended to a trigger line -- or sitting in any other file --
# still fails gate 8. Every other zizmor/ast-grep silencer still fails.
REVIEWED_SUPPRESSION_LINES = frozenset({
    (".github/workflows/review-trigger.yml",
     "# zizmor: ignore[dangerous-triggers] -- workflow_run is load-bearing here"),
})
STUBS = re.compile(
    r"throw new [A-Za-z]*Error.*[Nn]ot implemented"
    r"|raise NotImplementedError"
    r"|catch\s*\(\w*\)\s*\{\s*\}|catch\s*\{\s*\}"
    r"|\bTODO\b"
    r"|pass\s*#\s*stub"
)
SKIPS = re.compile(
    r"\.(skip|todo)\b"
    r"|\bxit\(|\bxdescribe\("
    r"|@pytest\.mark\.skip|(?<!\w)pytest\.skip\(|unittest\.skip|t\.Skip\("
)
ASSERT = re.compile(r"\bassert\w*\s*\(|\bassert\b|\bexpect\s*\(")
NEW_EXCEPTION = re.compile(r"^\|\s*[WE]\d+\s*\|")


def count_assertions(text: str) -> int:
    """Return the number of assertion calls in one diff line."""
    return len(ASSERT.findall(text))


def is_code_file(path: str) -> bool:
    return Path(path).suffix in CODE_EXTENSIONS


def is_test_file(path: str) -> bool:
    parts = Path(path).parts
    if any(p in ("test", "tests") for p in parts):
        return True
    name = parts[-1] if parts else path
    return (
        name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    )


def is_constraints(path: str) -> bool:
    return Path(path).name == "CONSTRAINTS.md"


def classify_added(path: str, text: str) -> list:
    """Return floor rules violated by one added line (empty when clean)."""
    if is_constraints(path) and NEW_EXCEPTION.search(text):
        return ["new-exception"]
    if not is_code_file(path):
        return []
    rules = []
    if SUPPRESSIONS.search(text):
        if (path, text.strip()) not in REVIEWED_SUPPRESSION_LINES:
            rules.append("silenced-checker")
    if STUBS.search(text):
        rules.append("unfinished-work")
    if SKIPS.search(text):
        rules.append("test-made-easier")
    return rules


def is_assertion_removed(path: str, removed_text: str) -> bool:
    return is_test_file(path) and bool(ASSERT.search(removed_text))


def find_deleted_tests(name_status: str) -> list:
    """Return deleted tests from NUL-framed Git output (or legacy text callers)."""
    if "\0" in name_status:
        fields = name_status.split("\0")
        records = zip(fields[::2], fields[1::2])
    else:
        records = (
            (status, decode_git_path(path))
            for line in name_status.split("\n") if "\t" in line
            for status, path in [line.split("\t", 1)]
        )
    return [path for status, path in records
            if status.startswith("D") and is_test_file(path)]


def _numbers(text: str) -> list:
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]


def _row_key(text: str):
    stripped = text.strip()
    if stripped.startswith("|"):
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and cells[0]:
            return ("cell", cells[0])
    return ("skeleton", re.sub(r"\d+(?:\.\d+)?", "#", stripped))


GROWTH_CAPPED = re.compile(r"must not grow", re.IGNORECASE)


def compare_thresholds(removed: list, added: list) -> list:
    """Flag CONSTRAINTS.md lines whose numbers moved the wrong way.

    Takes (path, text) pairs; only tightening is silent. Rows marked
    "must not grow" (e.g. finding backlogs) tighten by shrinking, so only
    growth is flagged there; every other row keeps the default direction
    (only a decrease is flagged). Returns finding strings of the form
    `<old>  ->  <new>`.
    """
    findings = []
    added_constraints = [(p, t) for p, t in added if is_constraints(p)]
    for rpath, rtext in removed:
        if not is_constraints(rpath):
            continue
        key = _row_key(rtext)
        match = next(
            ((p, t) for p, t in added_constraints if _row_key(t) == key),
            None,
        )
        if match is None:
            continue
        old, new = _numbers(rtext), _numbers(match[1])
        capped = bool(
            GROWTH_CAPPED.search(rtext) or GROWTH_CAPPED.search(match[1])
        )
        if capped:
            weakened = any(n > o for n, o in zip(new, old))
        else:
            weakened = any(n < o for n, o in zip(new, old))
        if weakened:
            findings.append(f"{rtext.strip()}  ->  {match[1].strip()}")
    return findings


def is_ignored(path: str, patterns: list) -> bool:
    return any(fnmatch.fnmatchcase(path, pat) for pat in patterns)


def read_ignore_file(ignore_path: Path) -> list:
    if not ignore_path.exists():
        return []
    return [
        line.strip()
        for line in ignore_path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def decode_git_path(path: str) -> str:
    """Decode Git's C quoting once, preserving arbitrary filesystem bytes."""
    if not path.startswith('"'):
        return path
    if not path.endswith('"'):
        raise ValueError("unterminated Git path")
    escapes = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11,
               "f": 12, "r": 13, '"': 34, "\\": 92}
    raw = bytearray()
    pos = 1
    while pos < len(path) - 1:
        char = path[pos]
        pos += 1
        if char != "\\":
            raw.extend(os.fsencode(char))
            continue
        char = path[pos]
        if char in escapes:
            raw.append(escapes[char])
            pos += 1
        elif re.fullmatch(r"[0-3][0-7]{2}", path[pos:pos + 3]):
            raw.append(int(path[pos:pos + 3], 8))
            pos += 3
        else:
            raise ValueError("invalid Git path escape")
    return os.fsdecode(bytes(raw))


def parse_diff(diff_text: str) -> tuple:
    added, removed = [], []
    old_path = new_path = ""
    in_hunk = False
    # Only LF frames patch records; splitlines would consume CR in file content.
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            old_path = new_path = ""
            in_hunk = False
        elif not in_hunk and line.startswith(("--- ", "+++ ")):
            path = decode_git_path(line[4:].removesuffix("\t"))
            if path.startswith(("a/", "b/")):
                path = path[2:]
            if line.startswith("--- "):
                old_path = path
            else:
                new_path = path
        elif line.startswith("@@ "):
            in_hunk = True
        elif line.startswith("+"):
            added.append((new_path, line[1:]))
        elif line.startswith("-"):
            removed.append((old_path, line[1:]))
    return added, removed


def git(args: list, allowed_returncodes=(0,)) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false"] + args,
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in allowed_returncodes:
        return None
    # Avoid universal-newline conversion and retain non-UTF-8 filename bytes.
    return os.fsdecode(proc.stdout)


def collect(base: str) -> tuple | None:
    """Return (added, removed, name_status) vs base, or None when unknown."""
    merge_base = git(["merge-base", base, "HEAD"])
    if not merge_base or not merge_base.strip():
        return None
    merge_base = merge_base.strip()
    # Fail closed: a failed/timed-out collection (None) is not an empty
    # diff -- it is an unknown tree, so the guard must report rc 2 rather
    # than print `floor-guard: clean`.
    # --no-renames on both diffs: git detects renames by default, so
    # `git mv test_x.py checks.py` reports R100 and a D-only filter never
    # sees the deletion (same reason mutmut_changed.sh passes --no-renames).
    # With renames off the move shows as D + A, so the deleted test is
    # flagged and removed assertion lines stay attributed to the old path.
    tracked = git(["diff", "--no-renames", "--unified=0", merge_base, "--"])
    if tracked is None:
        return None
    try:
        added, removed = parse_diff(tracked)
    except ValueError:
        return None
    name_status = git(["diff", "--no-renames", "--name-status", "-z", "--diff-filter=D", merge_base, "--"])
    if name_status is None:
        return None
    untracked = git(["ls-files", "-z", "--others", "--exclude-standard"])
    if untracked is None:
        return None
    for path in filter(None, untracked.split("\0")):
        # --no-index exits 1 when differences exist: keep that stdout, or
        # every untracked file would silently skip the guard (fail open).
        single = git(
            ["diff", "--no-index", "--unified=0", "/dev/null", path],
            allowed_returncodes=(0, 1),
        )
        if single is None:
            return None
        if single:
            try:
                extra_added, _ = parse_diff(single)
            except ValueError:
                return None
            added.extend(extra_added)
    return added, removed, name_status


def check(added: list, removed: list, name_status: str, ignore: list,
          ignore_name: str = ".constraintsignore") -> list:
    findings = []
    for path, text in added:
        if Path(path).name == ignore_name:
            # The ignore file exempts its own checks: a broadened pattern
            # (glob) added in the same branch would silence the guard for
            # every matching file. Exact paths stay silent (reviewed like
            # code); comments are inert.
            stripped = text.strip()
            if stripped and not stripped.startswith("#") and any(
                c in stripped for c in "*?["
            ):
                findings.append(("ignore-broadened", path, stripped[:120]))
            continue
        if is_ignored(path, ignore):
            continue
        for rule in classify_added(path, text):
            findings.append((rule, path, text.strip()[:120]))
    # Assertion floor is a net decrease per test file: an edit that keeps
    # its assertions (e.g. rewording a message) must not fail, while
    # deleting a real self.assertX / assert / expect( must fail.
    removed_counts: dict = {}
    removed_sample: dict = {}
    for path, text in removed:
        if is_ignored(path, ignore):
            continue
        if not is_test_file(path):
            continue
        n = count_assertions(text)
        if n:
            removed_counts[path] = removed_counts.get(path, 0) + n
            removed_sample.setdefault(path, text)
    added_counts: dict = {}
    for path, text in added:
        if is_ignored(path, ignore):
            continue
        if not is_test_file(path):
            continue
        n = count_assertions(text)
        if n:
            added_counts[path] = added_counts.get(path, 0) + n
    for path, lost in removed_counts.items():
        kept = added_counts.get(path, 0)
        if lost > kept:
            sample = removed_sample.get(path, "")
            findings.append(("assertion-removed", path, sample.strip()[:120]))
    for path in find_deleted_tests(name_status):
        if not is_ignored(path, ignore):
            findings.append(("test-deleted", path, "tracked test file deleted"))
    for hit in compare_thresholds(removed, added):
        findings.append(("threshold-lowered", "CONSTRAINTS.md", hit[:120]))
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--ignore-file", default=str(DEFAULT_IGNORE))
    args = parser.parse_args(argv)

    collected = collect(args.base)
    if collected is None:
        print(f"floor-guard: no merge base against {args.base}", file=sys.stderr)
        return 2
    added, removed, name_status = collected
    findings = check(added, removed, name_status,
                     read_ignore_file(Path(args.ignore_file)),
                     Path(args.ignore_file).name)
    if not findings:
        print("floor-guard: clean")
        return 0
    print(f"floor-guard: {len(findings)} floor violation(s):", file=sys.stderr)
    for rule, path, text in findings:
        print(f"  [{rule}] {path}: {text}", file=sys.stderr)
    print(
        "\nEach is a move that lowers the bar. Fix the code, "
        "or route it through a tracked exception.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
