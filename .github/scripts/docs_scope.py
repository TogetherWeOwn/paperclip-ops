#!/usr/bin/env python3
"""Docs-only change-scope detector for the docs fast-path (case_085c458c).

Prints `docs_only=true` only when every changed path is prose that nothing
executes or reads; anything else, or any doubt, prints `docs_only=false` and
the full suite runs. Fail-closed: an empty list, an unreadable list, or a
rename that touches code all read as not-docs-only.

Runs inside the non-required `docs-fast-path` workflow, so adopting it adds no
new required check and needs no ruleset change. Stdlib only. Pattern follows
two-web-next `ci/change-scope.sh` (case_d9023491): no `paths:` filter on the
workflow trigger (that would leave required checks pending forever), a scope
job that classifies, heavy jobs that skip on `docs_only`, and required checks
that always report under their own names.

Usage:
  git diff --name-status origin/main...HEAD | python3 docs_scope.py
  python3 docs_scope.py --files <(git diff --name-only --no-renames origin/main...HEAD)

Always pass `--no-renames` with `--name-only`: with rename detection on, a
moved file lists only its destination, so `git mv src/x.py docs/x.md` would
hide the deleted code path. `--name-status` needs no flag; it prints both
sides of a rename.

Exit status is always 0; the verdict is on stdout for $GITHUB_OUTPUT.
"""
import re
import sys

# Prose extensions that are never executed. Case-insensitive.
PROSE_SUFFIXES = (".md", ".rst", ".txt")

# Prose directories. A bare `docs/foo` without suffix still counts.
PROSE_DIRS = ("docs/",)

# Never docs-only: workflow, gate, and policy inputs. A PR touching these must
# run the full suite even when every other file is prose.
CODE_ANCHORS = (".github/", "test/fixtures/", "policy/")


def is_prose_path(path: str) -> bool:
    """True when a single path is prose that nothing executes or reads."""
    if not path or path.strip() != path:
        return False
    lowered = path.lower()
    for anchor in CODE_ANCHORS:
        if lowered.startswith(anchor) or ("/" + anchor) in lowered:
            return False
    if lowered.startswith(PROSE_DIRS):
        return True
    for suffix in PROSE_SUFFIXES:
        if lowered.endswith(suffix):
            # Markdown elsewhere (src/, test fixtures, .github/) may be
            # shipped or read by a gate, so only top-level or docs/ prose
            # counts; anything nested outside docs/ fails closed.
            if "/" not in path or lowered.startswith(PROSE_DIRS):
                return True
            return False
    # Exact bare names only: a prefix match would let root code such as
    # `licensed-code.py` or `license-check.sh` read as prose (case_e5882bc5).
    if lowered in ("license", "licence", "license-mit", "license-apache"):
        return "/" not in path
    return False


def classify(paths: list[str]) -> bool:
    """True only when the list is non-empty and every path is prose."""
    if not paths:
        return False
    return all(is_prose_path(p) for p in paths)


def parse_name_status(lines: list[str]) -> list[str]:
    """Expand `git diff --name-status` lines to the paths to classify.

    Renames (`R100 old new`) contribute BOTH names: moving code into docs/
    is not a docs change. Plain `--name-only` output passes through.
    """
    paths: list[str] = []
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = re.split(r"\s+", line.strip(), maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"[ACDMRTUXB][0-9]*", parts[0]):
            rest = parts[1]
            # Rename with tab separator carries both sides.
            if "\t" in rest:
                paths.extend(p for p in rest.split("\t") if p)
            # Similarity renames may render as {old => new}; keep both halves.
            elif "{ " in rest or "=>" in rest:
                paths.append(rest)
            else:
                paths.append(rest)
        else:
            paths.append(line.strip())
    return paths


def main(argv: list[str]) -> int:
    if "--files" in argv:
        idx = argv.index("--files")
        try:
            with open(argv[idx + 1], encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (IndexError, OSError) as exc:
            print(f"change-scope: cannot read file list ({exc})", file=sys.stderr)
            print("docs_only=false")
            return 0
    else:
        lines = sys.stdin.read().splitlines()
    paths = parse_name_status(lines)
    print(f"docs_only={'true' if classify(paths) else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
