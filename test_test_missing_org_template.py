#!/usr/bin/env python3
"""Regression tests for the staged test-missing gate drop (case_6f2e8a41).

Ported subset of the org-template acceptance suite: the staged logic copy,
the CLI wiring, and the gate-logic delta (wider source suffixes, deleted
files, stronger vacuity rules). The deterministic unit contract itself is
pinned by `test_blocker_test_checks.py`.

Scope note: the reusable-workflow, wrapper, ruleset and adoption-guide
classes are NOT ported here. Those staged files belong to the base mirror
and arrive with the pending toolkit-consolidation PR, whose newer
gate-execution matrix supersedes the embedded-script tests; porting them
now would fork the base. This file pins only the delta plus the
staged-copy and caller-deferral rules that hold with or without the base.

Style: stdlib unittest, hermetic (no network, no clock). Every test
fails while the staged files are missing — the correct RED reason.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGE = HERE / "templates" / "org-github" / "test-missing"
CALLER = HERE / ".github" / "workflows" / "test-missing.yml"


class CallerTest(unittest.TestCase):
    def test_repo_caller_is_not_enabled_before_org_gate_is_installed(self):
        self.assertFalse(
            CALLER.exists(),
            "do not call the org workflow until its pinned ref contains the gate",
        )


class StagedLogicCopyTest(unittest.TestCase):
    def test_staged_logic_copy_exists(self):
        staged = STAGE / "blocker_test_checks.py"
        self.assertTrue(staged.is_file(), f"missing staged copy: {staged}")

    def test_staged_copy_matches_root_source(self):
        root = HERE / "blocker_test_checks.py"
        staged = STAGE / "blocker_test_checks.py"
        self.assertTrue(root.is_file(), "root source missing (Builder implements it)")
        self.assertTrue(staged.is_file(), f"missing staged copy: {staged}")
        self.assertEqual(
            staged.read_bytes(),
            root.read_bytes(),
            "staged copy must be verbatim (fix upstream, then re-copy)",
        )


class CliWiringRegressionTest(unittest.TestCase):
    def test_cli_invokes_coverage_and_test_guard_checks(self):
        source = (HERE / "blocker_test_checks.py").read_text()
        main = re.search(r"(?s)^def main\(.*?(?=^if __name__|\\Z)", source, re.M)
        self.assertIsNotNone(main, "CLI entry point is required")
        self.assertIn("check_changed_line_coverage(", main.group())
        self.assertIn("check_test_guards(", main.group())

    def test_acceptance_suites_are_wired_into_fast_verification(self):
        verify_fast = (HERE / "scripts" / "verify-fast.sh").read_text()
        self.assertIn("test_blocker_test_checks", verify_fast)
        self.assertIn("test_test_missing_org_template", verify_fast)


def _load_root_module():
    spec = importlib.util.spec_from_file_location(
        "blocker_test_checks", HERE / "blocker_test_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GateLogicRegressionTest(unittest.TestCase):
    def test_colocated_test_and_spec_files_count_as_tests(self):
        mod = _load_root_module()
        for path in ("src/foo.test.ts", "src/foo.spec.ts", "src/foo.test.tsx"):
            self.assertTrue(mod.is_test_path(path), path)
            self.assertFalse(mod.is_src_path(path), path)
        self.assertEqual(
            mod.check_src_without_tests(["src/foo.ts", "src/foo.test.ts"]), []
        )

    def test_bare_test_and_spec_suffixes_count_as_tests(self):
        # Merge pin: the base mirror counts bare `*.test` / `*.spec`
        # (release.test, app.spec, any case) as tests; the ported delta
        # keeps that behavior, so the consolidation re-sync must not
        # regress it and this port must not drop it.
        mod = _load_root_module()
        for path in ("release.test", "app.spec", "src/release.test",
                     "src/app.spec", "RELEASE.TEST", "APP.SPEC",
                     "src/FOO.TEST.TS", "src/FOO.SPEC.JS"):
            self.assertTrue(mod.is_test_path(path), path)
            self.assertFalse(mod.is_src_path(path), path)
        self.assertEqual(
            mod.check_src_without_tests(["src/a.py", "release.test"]), []
        )
        self.assertEqual(
            mod.check_src_without_tests(["src/a.py", "app.spec"]), []
        )

    def test_extended_src_suffixes_are_gated(self):
        mod = _load_root_module()
        for path in ("src/App.tsx", "src/a.jsx", "src/a.svelte",
                     "src/lib.rs", "app/Foo.php", "main.go"):
            self.assertTrue(mod.is_src_path(path), path)
            self.assertTrue(mod.check_src_without_tests([path]), path)
        for path in ("src/a.cjs", "src/a.mts", "src/a.cts"):
            self.assertTrue(mod.is_src_path(path), path)
            self.assertTrue(mod.check_src_without_tests([path]), path)

    def test_deleted_files_count_as_neither_src_nor_tests(self):
        mod = _load_root_module()
        self.assertEqual(
            mod.check_src_without_tests(["src/old.py"], deleted_files=["src/old.py"]),
            [],
        )
        findings = mod.check_src_without_tests(
            ["src/foo.py", "tests/test_gone.py"],
            deleted_files=["tests/test_gone.py"],
        )
        self.assertTrue(findings, "deleted test must not satisfy a src change")

    def test_vacuity_skips_helpers_and_accepts_raises(self):
        mod = _load_root_module()
        self.assertEqual(
            mod.check_test_guards({"tests/conftest.py": "import pytest\n"}), []
        )
        self.assertEqual(mod.check_test_guards({"tests/__init__.py": ""}), [])
        self.assertEqual(
            mod.check_test_guards(
                {"tests/test_raises.py":
                 "import pytest\ndef test_it():\n    with pytest.raises(ValueError):\n        raise ValueError()\n"}
            ),
            [],
        )
        self.assertTrue(
            mod.check_test_guards(
                {"tests/test_vacuous.py": "def test_it():\n    x = 1\n"}
            )
        )

    def test_helper_delegated_assertions_pass(self):
        mod = _load_root_module()
        self.assertEqual(
            mod.check_test_guards(
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
            ),
            [],
        )

    def test_mixed_asserting_and_vacuous_file_fails(self):
        mod = _load_root_module()
        findings = mod.check_test_guards(
            {
                "tests/test_mixed.py": (
                    "def test_ok():\n    assert True\n"
                    "def test_bad():\n    x = 1\n"
                )
            }
        )
        self.assertTrue(findings, "one vacuous test must fail the file")


class CliExecutionTest(unittest.TestCase):
    """Run the staged CLI as a subprocess: exit codes,
    --changed-files-file, label parsing and --coverage-json are behaviour,
    not regex-matchable text."""

    def run_cli(self, args, files=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, content in (files or {}).items():
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
            env = dict(os.environ)
            env.pop("GITHUB_STEP_SUMMARY", None)
            proc = subprocess.run(
                [sys.executable, str(HERE / "blocker_test_checks.py"), *args],
                capture_output=True, text=True, cwd=str(root), env=env,
            )
            return proc.returncode, proc.stdout + proc.stderr

    def test_cli_enforce_returns_1_on_src_without_tests(self):
        rc, out = self.run_cli(
            ["--changed-files", "src/foo.py", "--mode", "enforce"])
        self.assertEqual(rc, 1, out)

    def test_cli_warn_returns_0_with_findings(self):
        rc, out = self.run_cli(
            ["--changed-files", "src/foo.py", "--mode", "warn"])
        self.assertEqual(rc, 0, out)
        self.assertIn("src change without tests", out)

    def test_cli_changed_files_file(self):
        rc, out = self.run_cli(
            ["--changed-files-file", "changed.txt", "--mode", "enforce"],
            files={"changed.txt": "src/foo.py\n"},
        )
        self.assertEqual(rc, 1, out)

    def test_cli_labels_waive_src_rule(self):
        rc, out = self.run_cli(
            ["--changed-files", "src/foo.py", "--labels", "tests-exempt",
             "--mode", "enforce"])
        self.assertEqual(rc, 0, out)

    def test_cli_coverage_json_adds_findings(self):
        cov = json.dumps(
            {"changed": {"src/foo.py": [1]}, "covered": {"src/foo.py": []}})
        rc, out = self.run_cli(
            ["--changed-files", "src/foo.py", "--mode", "enforce",
             "--coverage-json", "cov.json"],
            files={"cov.json": cov},
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("changed line not covered", out)


if __name__ == "__main__":
    unittest.main()
