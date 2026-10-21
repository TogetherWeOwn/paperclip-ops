"""Unit tests for the quality-gate helpers (fast, hermetic).

Covers scripts/quality-gates/subprocess_timeout_gate.py pure logic:
backlog parsing, test-path exclusion, and backlog comparison. No ast-grep
binary, no network, no git writes: scan() is mocked at the subprocess
boundary. Run: python3 -m unittest test_quality_gates
"""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent
GATE_PATH = REPO_ROOT / "scripts" / "quality-gates" / "subprocess_timeout_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "subprocess_timeout_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_gate()


class TestIsTestPath(unittest.TestCase):
    def test_plain_source_is_not_test(self):
        self.assertFalse(gate.is_test_path("ops/productivity/sweep.py"))

    def test_test_prefix_is_test(self):
        self.assertTrue(gate.is_test_path("test_sweep.py"))

    def test_test_suffix_is_test(self):
        self.assertTrue(gate.is_test_path("ops/sweep_test.py"))

    def test_tests_dir_is_test(self):
        self.assertTrue(gate.is_test_path("ops/tests/sweep.py"))

    def test_test_dir_is_test(self):
        self.assertTrue(gate.is_test_path("test/sweep.py"))

    def test_substring_is_not_enough(self):
        # A naive `in` check would exclude these real files.
        self.assertFalse(gate.is_test_path("latest.py"))
        self.assertFalse(gate.is_test_path("ops/contest.py"))
        self.assertFalse(gate.is_test_path("verification/case_214e61d3-mutation-gate.py"))


class TestReadBacklog(unittest.TestCase):
    def test_parses_rows_skips_comments(self):
        text = ("# comment\n"
                "\n"
                "a.py: 3\n"
                "ops/b.py: 12\n")
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", return_value=text):
            self.assertEqual(gate.read_backlog(Path("x")),
                             {"a.py": 3, "ops/b.py": 12})

    def test_missing_backlog_is_empty(self):
        self.assertEqual(gate.read_backlog(Path("/nonexistent/backlog.txt")), {})


class TestCompare(unittest.TestCase):
    def test_new_file_warns(self):
        warnings = gate.compare({"new.py": 2}, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("new.py", warnings[0])

    def test_grown_count_warns(self):
        warnings = gate.compare({"a.py": 5}, {"a.py": 3})
        self.assertEqual(len(warnings), 1)
        self.assertIn("3 -> 5", warnings[0])

    def test_shrunk_count_is_silent(self):
        self.assertEqual(gate.compare({"a.py": 1}, {"a.py": 3}), [])

    def test_fixed_file_is_silent(self):
        self.assertEqual(gate.compare({}, {"a.py": 3}), [])

    def test_unchanged_is_silent(self):
        self.assertEqual(gate.compare({"a.py": 3}, {"a.py": 3}), [])


class TestWorkflowComparisonBase(unittest.TestCase):
    def test_pr_diff_gates_use_checked_out_merge_base(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "quality-gates.yml").read_text()
        self.assertIn('QUALITY_GATE_BASE_SHA="$(git rev-parse HEAD^1)"', workflow)
        self.assertEqual(
            workflow.count("BASE_SHA: ${{ env.QUALITY_GATE_BASE_SHA || github.event.pull_request.base.sha }}"),
            2,
        )

class TestScan(unittest.TestCase):
    def _finding(self, file, rule="py-subprocess-timeout"):
        return {"ruleId": rule, "file": file,
                "range": {"start": {"line": 1, "column": 1},
                          "end": {"line": 1, "column": 10}}}

    def _run_scan(self, findings):
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = json.dumps(findings)
        proc.stderr = ""
        with mock.patch.object(gate.subprocess, "run", return_value=proc):
            return gate.scan("ast-grep")

    def test_relative_paths_resolve_against_repo(self):
        counts = self._run_scan([self._finding("ops/a.py"),
                                 self._finding("ops/a.py")])
        self.assertEqual(counts, {"ops/a.py": 2})

    def test_test_files_are_excluded(self):
        counts = self._run_scan([self._finding("test_a.py"),
                                 self._finding("ops/tests/b.py")])
        self.assertEqual(counts, {})

    def test_other_rules_are_ignored(self):
        counts = self._run_scan([self._finding("ops/a.py", rule="other-rule")])
        self.assertEqual(counts, {})

    def test_vendored_rule_id_is_counted(self):
        # Vendored shared rule (sg-rules/python/py-subprocess-timeout.yml).
        counts = self._run_scan([self._finding("ops/a.py",
                                               rule="py-subprocess-timeout")])
        self.assertEqual(counts, {"ops/a.py": 1})

    def test_retired_handwritten_rule_id_is_ignored(self):
        # The old hand-written sg-rules/subprocess-no-timeout.yml is gone;
        # findings under its id must not count.
        counts = self._run_scan([self._finding("ops/a.py",
                                               rule="subprocess-no-timeout")])
        self.assertEqual(counts, {})

    def test_failed_scan_warns_and_returns_none(self):
        # Paperclip Review round 1: a failed scan must not read as clean
        # ({} would let --write-backlog wipe the backlog to a header).
        proc = mock.Mock()
        proc.returncode = 2
        proc.stderr = "boom"
        with mock.patch.object(gate.subprocess, "run", return_value=proc):
            self.assertIsNone(gate.scan("ast-grep"))

    def test_missing_binary_returns_none(self):
        with mock.patch.object(gate.subprocess, "run",
                               side_effect=FileNotFoundError("no ast-grep")):
            self.assertIsNone(gate.scan("ast-grep"))

    def test_timeout_returns_none(self):
        import subprocess as sp
        with mock.patch.object(gate.subprocess, "run",
                               side_effect=sp.TimeoutExpired("ast-grep", 300)):
            self.assertIsNone(gate.scan("ast-grep"))

    def test_unparseable_output_returns_none(self):
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = "not json"
        proc.stderr = ""
        with mock.patch.object(gate.subprocess, "run", return_value=proc):
            self.assertIsNone(gate.scan("ast-grep"))

    def test_scan_includes_hidden_dirs(self):
        # ast-grep skips .github/ by default, so the gha-* rules never run
        # without --no-ignore hidden (gate 4b misses .github/scripts).
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = "[]"
        proc.stderr = ""
        with mock.patch.object(gate.subprocess, "run", return_value=proc) as run:
            gate.scan("ast-grep")
        argv = run.call_args[0][0]
        self.assertIn("--no-ignore", argv)
        self.assertIn("hidden", argv)


class TestMainFailClosed(unittest.TestCase):
    # Paperclip Review round 1: --write-backlog must refuse (nonzero)
    # when the scan fails, never truncate the backlog to a header; warn
    # mode must stay rc 0 without presenting the failure as clean.
    def test_write_backlog_refuses_on_failed_scan(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            backlog = Path(tmp) / "backlog.txt"
            backlog.write_text("# header\na.py: 2\n")
            with mock.patch.object(gate, "scan", return_value=None):
                rc = gate.main(["--backlog", str(backlog),
                                "--write-backlog"])
            self.assertNotEqual(rc, 0)
            self.assertEqual(backlog.read_text(), "# header\na.py: 2\n")

    def test_warn_mode_stays_zero_on_failed_scan(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            backlog = Path(tmp) / "backlog.txt"
            backlog.write_text("a.py: 3\n")
            with mock.patch.object(gate, "scan", return_value=None):
                rc = gate.main(["--backlog", str(backlog)])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
