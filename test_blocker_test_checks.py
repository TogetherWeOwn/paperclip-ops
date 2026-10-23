#!/usr/bin/env python3
"""Acceptance tests for the deterministic test-missing/wrong checks (case_6f2e8a41).

Covers blocker-ledger class 'test missing/wrong' (ledger subtype `test_gap`,
24% of Claude blockers): a changed-src-without-changed-tests gate, a
changed-line coverage gate, and test-guard (vacuity) rules.

Contract under test (to be implemented by the Builder in
`blocker_test_checks.py`, repo root):

- `TEST_EXEMPT_LABEL == "tests-exempt"`: label that waives the
  src-without-tests gate.
- `is_test_path(path) -> bool`: basename `test_*` / `*_test.*`, colocated
  `*.test.*` / `*.spec.*` (plus bare `*.test` / `*.spec`), or under a
  `test(s)/` / `__tests__/` directory.
- `is_src_path(path) -> bool`: code suffix (`.py`, `.sh`, `.js`, `.ts`,
  `.mjs`, `.cjs`, `.mts`, `.cts`, `.tsx`, `.jsx`, `.svelte`, `.rs`,
  `.php`, `.go`) that is not a test path.
- `check_src_without_tests(changed_files, labels=(), deleted_files=()) ->
  list[str]`: one finding per changed src file when no changed test file
  is present, unless the exempt label is present. Docs-only (or
  tests-only) changes pass. Deleted files count as neither src nor tests.
- `check_changed_line_coverage(changed_lines, covered_lines) -> list[str]`:
  one finding per changed-but-uncovered line, naming `file:line`.
- `check_test_guards(test_contents) -> list[str]`: one finding per changed
  Python test file that is empty, unparseable, or holds a test function
  with no assertions and no call to a same-module asserting helper
  (vacuous-test guard). `conftest.py` / `__init__.py` and helper-only
  modules are skipped, never flagged.

Style: stdlib unittest, in-memory fixtures, one behavior per test. No
network, no clock, no filesystem writes. The module under test is loaded
via importlib so a missing module fails every test for the right reason.
"""

import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "blocker_test_checks.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "blocker_test_checks", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load_module()


class ExemptLabelTest(unittest.TestCase):
    def test_exempt_label_name_is_pinned(self):
        self.assertEqual(CHECK.TEST_EXEMPT_LABEL, "tests-exempt")


class SrcWithoutTestsTest(unittest.TestCase):
    def test_src_change_without_test_change_fails(self):
        findings = CHECK.check_src_without_tests(["warn_first_inventory.py"])
        self.assertTrue(findings, "src-only change must produce a finding")
        self.assertIn("warn_first_inventory.py", findings[0])

    def test_src_change_with_changed_test_passes(self):
        findings = CHECK.check_src_without_tests(
            ["warn_first_inventory.py", "test_warn_first_inventory.py"]
        )
        self.assertEqual(findings, [])

    def test_exempt_label_waives_src_without_tests(self):
        findings = CHECK.check_src_without_tests(
            ["warn_first_inventory.py"], labels=["tests-exempt"]
        )
        self.assertEqual(findings, [])

    def test_docs_only_change_passes_without_tests(self):
        findings = CHECK.check_src_without_tests(["README.md", "docs/runbook.md"])
        self.assertEqual(findings, [])


class ChangedLineCoverageTest(unittest.TestCase):
    def test_uncovered_changed_lines_fail(self):
        findings = CHECK.check_changed_line_coverage(
            {"warn_first_inventory.py": {10, 11}},
            {"warn_first_inventory.py": {10}},
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("warn_first_inventory.py:11", findings[0])

    def test_fully_covered_changed_lines_pass(self):
        findings = CHECK.check_changed_line_coverage(
            {"warn_first_inventory.py": {10, 11}},
            {"warn_first_inventory.py": {10, 11, 12}},
        )
        self.assertEqual(findings, [])


class TestGuardTest(unittest.TestCase):
    def test_new_test_without_assertions_fails(self):
        findings = CHECK.check_test_guards(
            {"test_new_thing.py": "def test_it():\n    x = 1\n"}
        )
        self.assertTrue(findings, "assertion-free test must produce a finding")
        self.assertIn("test_new_thing.py", findings[0])

    def test_new_test_with_assertions_passes(self):
        findings = CHECK.check_test_guards(
            {"test_new_thing.py": "def test_it():\n    assert 1 + 1 == 2\n"}
        )
        self.assertEqual(findings, [])

    def test_test_calling_asserting_helper_passes(self):
        findings = CHECK.check_test_guards(
            {
                "tests/test_lint.py": (
                    "import unittest\n"
                    "class LintTests(unittest.TestCase):\n"
                    "    def lint(self, text):\n"
                    "        self.assertEqual(text, 'ok')\n"
                    "    def test_good(self):\n"
                    "        self.lint('ok')\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_mixed_asserting_and_vacuous_file_still_fails(self):
        findings = CHECK.check_test_guards(
            {
                "tests/test_mixed.py": (
                    "def test_ok():\n    assert True\n"
                    "def test_bad():\n    x = 1\n"
                )
            }
        )
        self.assertTrue(findings, "one vacuous test must fail the file")
        self.assertIn("test_mixed.py", findings[0])

    def test_unparseable_test_file_fails(self):
        findings = CHECK.check_test_guards(
            {"test_broken.py": "def test_it(:\n    assert False\n"}
        )
        self.assertTrue(findings, "unparseable test must produce a finding")
        self.assertIn("test_broken.py", findings[0])

    def test_shell_test_is_not_parsed_as_python(self):
        findings = CHECK.check_test_guards(
            {"test_runner.sh": "#!/bin/sh\nrun_suite\n"}
        )
        self.assertEqual(findings, [])

    def test_camelcase_test_is_collected_and_setup_is_not(self):
        vacuous = CHECK.check_test_guards(
            {
                "tests/test_camel.py": (
                    "class T:\n"
                    "    def setUp(self):\n"
                    "        self.x = 1\n"
                    "    def testSomething(self):\n"
                    "        x = 1\n"
                )
            }
        )
        self.assertTrue(vacuous, "vacuous camelCase test must be flagged")
        passing = CHECK.check_test_guards(
            {
                "tests/test_camel_ok.py": (
                    "class T:\n"
                    "    def setUp(self):\n"
                    "        self.x = 1\n"
                    "    def testSomething(self):\n"
                    "        assert self.x == 1\n"
                )
            }
        )
        self.assertEqual(passing, [])

    def test_empty_test_module_fails(self):
        findings = CHECK.check_test_guards({"tests/test_empty.py": ""})
        self.assertTrue(findings, "empty test module must produce a finding")
        self.assertIn("test_empty.py", findings[0])


class TestLayoutTest(unittest.TestCase):
    def test___tests___dir_counts_as_test_dir(self):
        self.assertTrue(CHECK.is_test_path("__tests__/app.js"))
        self.assertFalse(CHECK.is_src_path("__tests__/app.js"))

    def test_mjs_src_is_gated_and_satisfied_by___tests___pair(self):
        self.assertTrue(CHECK.is_src_path("src/app.mjs"))
        self.assertEqual(
            CHECK.check_src_without_tests(["src/app.mjs", "__tests__/app.js"]),
            [],
        )
        self.assertTrue(CHECK.check_src_without_tests(["src/app.mjs"]))

    def test_shell_and_go_src_are_gated(self):
        self.assertTrue(CHECK.is_src_path("run.sh"))
        self.assertTrue(CHECK.is_src_path("main.go"))
        self.assertTrue(CHECK.check_src_without_tests(["run.sh"]))
        self.assertTrue(CHECK.check_src_without_tests(["main.go"]))


if __name__ == "__main__":
    unittest.main()
