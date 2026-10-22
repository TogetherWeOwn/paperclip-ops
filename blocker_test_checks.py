#!/usr/bin/env python3
"""Deterministic test-missing/wrong checks (case_6f2e8a41).

Covers blocker-ledger class 'test missing/wrong' (ledger subtype
`test_gap`, 24% of Claude blockers): a changed-src-without-changed-tests
gate, a changed-line coverage gate, and test-guard (vacuity) rules.

Rule: deterministic first; AI proposes, code disposes. Staging-first:
callers land in warn (advisory) mode; enforce only after the FP rate is
measured. No network, no clock, stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path, PurePosixPath

TEST_EXEMPT_LABEL = "tests-exempt"

SRC_SUFFIXES = (
    ".py", ".sh", ".js", ".ts", ".mjs", ".cjs", ".mts", ".cts",
    ".tsx", ".jsx", ".svelte", ".rs", ".php", ".go",
)
TEST_DIR_NAMES = ("test", "tests", "__tests__")


def is_test_path(path: str) -> bool:
    """Basename `test_*` / `*_test.*`, colocated `*.test.*` / `*.spec.*`
    (plus bare `*.test` / `*.spec`), or under a test(s)/__tests__ dir."""
    posix = PurePosixPath(path.replace(os.sep, "/"))
    name = posix.name
    lower = name.lower()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if name.startswith("test_") or stem.endswith("_test"):
        return True
    if ".test." in lower or ".spec." in lower:
        return True
    if lower.endswith(".test") or lower.endswith(".spec"):
        return True
    return any(part in TEST_DIR_NAMES for part in posix.parts[:-1])


def is_src_path(path: str) -> bool:
    """Code suffix that is not a test path."""
    if is_test_path(path):
        return False
    lower = path.lower()
    return lower.endswith(SRC_SUFFIXES)


def check_src_without_tests(changed_files, labels=(), deleted_files=()) -> list:
    """One finding per changed src file when no changed test file exists.

    Waived by the exempt label. Docs-only (or tests-only) changes pass.
    Files in ``deleted_files`` count as neither changed src nor changed
    tests: deleting dead code needs no new tests, and a deleted test does
    not satisfy a src change. Callers that list changes with
    ``--diff-filter=ACMRTUXB`` already exclude deletions; this parameter
    covers callers with a plain name list plus a deletion list.
    """
    if TEST_EXEMPT_LABEL in (labels or ()):
        return []
    deleted = set(deleted_files or ())
    files = [f for f in (changed_files or ()) if f not in deleted]
    src = [f for f in files if is_src_path(f)]
    if not src:
        return []
    if any(is_test_path(f) for f in files):
        return []
    return [f"src change without tests: {f}" for f in src]


def check_changed_line_coverage(changed_lines, covered_lines) -> list:
    """One finding per changed-but-uncovered line, naming `file:line`."""
    findings = []
    for path in sorted((changed_lines or {}).keys()):
        changed = set((changed_lines or {}).get(path) or set())
        covered = set((covered_lines or {}).get(path) or set())
        for line in sorted(changed - covered):
            findings.append(f"{path}:{line}: changed line not covered by tests")
    return findings


_ASSERT_CALL_PREFIXES = ("assert", "expect", "should", "verify", "check")
_ASSERT_CALL_NAMES = ("raises", "warns", "fail")
_SKIP_BASENAMES = ("conftest.py", "__init__.py")


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id.lower()
    if isinstance(func, ast.Attribute):
        return func.attr.lower()
    return ""


_NESTED_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _scoped_nodes(root: ast.AST):
    """Root plus descendants, excluding nested def/class bodies.

    Assertions (or helper calls) inside an uncalled nested function or
    class body never execute when the outer test runs, so they must not
    satisfy the vacuity guard. The root itself is always yielded; only
    scopes *below* it are pruned.
    """
    yield root
    for child in ast.iter_child_nodes(root):
        if isinstance(child, _NESTED_SCOPE_TYPES):
            continue
        yield from _scoped_nodes(child)


def _has_assertion(tree: ast.AST) -> bool:
    for node in _scoped_nodes(tree):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call) and _call_name(
                    item.context_expr
                ) in _ASSERT_CALL_NAMES:
                    return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _ASSERT_CALL_NAMES or name.startswith(_ASSERT_CALL_PREFIXES):
                return True
    return False


def _asserting_helper_names(tree: ast.AST, test_ids: set) -> set:
    """Names of non-test functions whose body performs an assertion.

    A `test_*` function that only delegates to such a helper (the idiomatic
    unittest style, e.g. `self.lint(GOOD)` where `lint()` does the
    `assertEqual`) still verifies behaviour, so it must not be reported as
    vacuous. Only same-module helpers are visible here; helpers imported
    from another module stay out of scope and still count as vacuous.
    Transitive delegation (helper calling an asserting helper) resolves
    to a fixpoint.
    """
    helpers: set = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if id(node) in test_ids:
                continue
            name = node.name.lower()
            if name in helpers:
                continue
            if _has_assertion(node) or any(
                isinstance(child, ast.Call) and _call_name(child) in helpers
                for child in _scoped_nodes(node)
            ):
                helpers.add(name)
                changed = True
    return helpers


def _calls_asserting_helper(fn: ast.AST, helpers: set) -> bool:
    """Whether a test function calls a same-module asserting helper."""
    return any(
        isinstance(node, ast.Call) and _call_name(node) in helpers
        for node in _scoped_nodes(fn)
    )


def _test_functions(tree: ast.AST) -> list:
    """Test functions/methods: defs the collectors would pick up.

    unittest's loader and pytest both collect names with the `test`
    prefix (``test_*`` and camelCase ``testSomething`` alike)."""
    found = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Only test-prefixed defs are test functions: helpers such as
                # setUp/tearDown in a Test* class are not, and nested
                # functions are part of their enclosing test, not
                # separate candidates.
                if child.name.startswith("test"):
                    found.append(child)
            else:
                visit(child)

    visit(tree)
    return found


def check_test_guards(test_contents) -> list:
    """One finding per changed Python test file that is empty, unparseable,
    or contains a test function with no assertions (vacuous-test guard).

    The vacuity parse is Python-only (`ast`): non-Python tests (shell,
    JS/TS) are skipped, never flagged unparseable. A test that delegates
    to a same-module helper whose body asserts (e.g. `self.lint(GOOD)`)
    counts as asserting; only a test with neither an inline assertion nor
    a call to an asserting helper is vacuous."""
    findings = []
    for path in sorted((test_contents or {}).keys()):
        basename = PurePosixPath(path.replace(os.sep, "/")).name
        if basename in _SKIP_BASENAMES:
            continue
        content = (test_contents or {}).get(path) or ""
        if not path.lower().endswith(".py"):
            continue
        if not content.strip():
            # Only test modules are flagged empty; an empty helper under
            # tests/ (e.g. __init__.py, handled above) is legitimate.
            if basename.startswith("test_") or basename.rsplit(".", 1)[0].endswith("_test"):
                findings.append(f"{path}: empty test file (no test content)")
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            findings.append(f"{path}: unparseable test file ({exc.msg})")
            continue
        functions = _test_functions(tree)
        if not functions:
            # Helper/fixture module under a test dir, not a test module.
            continue
        test_ids = {id(fn) for fn in functions}
        helpers = _asserting_helper_names(tree, test_ids)
        if any(
            not _has_assertion(fn) and not _calls_asserting_helper(fn, helpers)
            for fn in functions
        ):
            findings.append(f"{path}: no assertions (vacuous test)")
    return findings


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Deterministic test-missing/wrong gate (warn by default)."
    )
    parser.add_argument("--changed-files", default="",
                        help="Newline- or comma-separated changed paths.")
    parser.add_argument("--changed-files-file", default="",
                        help="File with one changed path per line.")
    parser.add_argument("--deleted-files", default="",
                        help="Newline- or comma-separated deleted paths.")
    parser.add_argument("--deleted-files-file", default="",
                        help="File with one deleted path per line.")
    parser.add_argument("--labels", default="",
                        help="Comma-separated PR labels.")
    parser.add_argument("--mode", default="warn",
                        choices=("warn", "enforce", "enforcing"),
                        help="warn prints findings and exits 0; "
                             "enforce (or its enforcing alias) exits 1 "
                             "on findings.")
    parser.add_argument("--coverage-json", default="",
                        help="JSON file {\"changed\": {path: [lines]}, "
                             "\"covered\": {path: [lines]}}; when given, "
                             "changed-line coverage findings are included. "
                             "Without it the coverage check has no data "
                             "and reports nothing.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    files: list = []
    if args.changed_files_file:
        try:
            with open(args.changed_files_file, encoding="utf-8") as handle:
                files.extend(line.strip() for line in handle if line.strip())
        except OSError as exc:
            print(f"test-missing gate: cannot read files list: {exc}",
                  file=sys.stderr)
            return 2
    if args.changed_files:
        raw = args.changed_files.replace(",", "\n")
        files.extend(line.strip() for line in raw.splitlines() if line.strip())
    deleted: list = []
    if args.deleted_files_file:
        try:
            with open(args.deleted_files_file, encoding="utf-8") as handle:
                deleted.extend(line.strip() for line in handle if line.strip())
        except OSError as exc:
            print(f"test-missing gate: cannot read deleted-files list: {exc}",
                  file=sys.stderr)
            return 2
    if args.deleted_files:
        raw_deleted = args.deleted_files.replace(",", "\n")
        deleted.extend(
            line.strip() for line in raw_deleted.splitlines() if line.strip()
        )
    labels = [part.strip() for part in (args.labels or "").replace(",", "\n").splitlines()
              if part.strip()]
    findings = check_src_without_tests(files, labels, deleted_files=deleted)
    # Vacuity guards run on every changed test file readable from the
    # checkout. Deleted files are absent, not empty, so they are skipped;
    # a present-but-unreadable test file is a finding, never a silent
    # pass (an unreadable test must not read as covered). Deletion is
    # caller-declared only: never infer it from filesystem existence (a
    # wrong working directory would then fail the gate open).
    deleted_set = set(deleted)
    test_contents = {}
    for path in files:
        if path in deleted_set:
            continue
        if not is_test_path(path):
            continue
        try:
            test_contents[path] = Path(path).read_text(
                encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            continue
        except OSError:
            findings.append(f"{path}: unreadable test file")
    findings += check_test_guards(test_contents)
    # Changed-line coverage runs when the caller supplies coverage data.
    if args.coverage_json:
        try:
            with open(args.coverage_json, encoding="utf-8") as handle:
                coverage = json.load(handle)
            changed_lines = {
                p: set(lines or ())
                for p, lines in (coverage.get("changed") or {}).items()
            }
            covered_lines = {
                p: set(lines or ())
                for p, lines in (coverage.get("covered") or {}).items()
            }
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            print(f"test-missing gate: cannot read coverage data: {exc}",
                  file=sys.stderr)
            return 2
        findings += check_changed_line_coverage(changed_lines, covered_lines)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if findings:
        print("test-missing gate findings:")
        for finding in findings:
            print(f"- {finding}")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as handle:
                    handle.write("## test-missing gate\n")
                    for finding in findings:
                        handle.write(f"- {finding}\n")
            except OSError:
                pass
        return 1 if args.mode in ("enforce", "enforcing") else 0
    print("test-missing gate: no findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
