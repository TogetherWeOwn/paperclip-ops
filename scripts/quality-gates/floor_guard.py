#!/usr/bin/env python3
"""Floor guard for CONSTRAINTS.md (HARD).

Diff-scoped enforcement of the quality floor: fails when a change adds
suppressions, skips or deletes tests, strips assertions, or moves a
threshold the wrong way (rows marked must-not-grow fail on growth, all
others on decrease). Adapted from the constraint-driven-development skill's
references/floor-guard.md reference implementation; the diff plumbing and
exit codes are identical, only the patterns are extended for this repo's
Python+bash stack.

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
    """Return deleted test-file paths from `git diff --name-status` output."""
    deleted = []
    for line in name_status.splitlines():
        # name-status separates status and path with TAB (never spaces):
        # splitting on whitespace would mangle paths containing spaces.
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("D") and is_test_file(parts[1]):
            deleted.append(parts[1])
    return deleted


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


def parse_diff(diff_text: str) -> tuple:
    added, removed = [], []
    current = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            # Git appends a TAB after paths containing spaces
            # (e.g. `+++ b/sp ace.py<TAB>`); non-ASCII paths arrive raw
            # because every git() call sets core.quotePath=false. Strip
            # the TAB so spaced paths still classify as code/test files.
            current = line[4:].rstrip("\t")
            if current.startswith("b/"):
                current = current[2:]
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((current, line[1:]))
        elif line.startswith("-") and not line.startswith("---"):
            removed.append((current, line[1:]))
    return added, removed


def git(args: list, allowed_returncodes=(0,)) -> str | None:
    try:
        proc = subprocess.run(
            # core.quotePath=false keeps non-ASCII paths raw (no
            # octal-quoted `"b/\303\251.py"` headers for parse_diff to
            # misclassify); ls-files -z output is NUL-delimited already.
            ["git", "-c", "core.quotePath=false"] + args,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in allowed_returncodes:
        return None
    return proc.stdout


def collect(base: str) -> tuple | None:
    """Return (added, removed, name_status) vs base, or None when unknown."""
    merge_base = git(["merge-base", base, "HEAD"])
    if not merge_base or not merge_base.strip():
        return None
    merge_base = merge_base.strip()
    # Fail closed: a failed/timed-out collection (None) is not an empty
    # diff -- it is an unknown tree, so the guard must report rc 2 rather
    # than print `floor-guard: clean`.
    tracked = git(["diff", "--unified=0", merge_base, "--"])
    if tracked is None:
        return None
    added, removed = parse_diff(tracked)
    name_status = git(["diff", "--name-status", "--diff-filter=D", merge_base, "--"])
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
            extra_added, _ = parse_diff(single)
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
