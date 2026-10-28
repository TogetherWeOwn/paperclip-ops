"""Unit tests for the quality-gate helpers (fast, hermetic).

Covers scripts/quality-gates/subprocess_timeout_gate.py pure logic:
backlog parsing, test-path exclusion, and backlog comparison. No ast-grep
binary, no network, no git writes: scan() is mocked at the subprocess
boundary. Run: python3 -m unittest test_quality_gates
"""
import importlib.util
import json
import re
import shlex
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
        # Both halves of the wiring are pinned: the value must reach the
        # job environment (not GITHUB_OUTPUT or nowhere), and the Record
        # step must run before the gates that consume it. Either half
        # dropped silently falls back to the payload base the wiring
        # exists to avoid, mis-attributing main-side changes to the PR.
        self.assertIn(
            'echo "QUALITY_GATE_BASE_SHA=$QUALITY_GATE_BASE_SHA" >> "$GITHUB_ENV"',
            workflow,
        )
        record_at = workflow.index("Record checked-out PR base for diff gates")
        self.assertLess(record_at, workflow.index("Gate 7 (WARN)"))
        self.assertLess(record_at, workflow.index("Gate 8 (HARD)"))
        self.assertEqual(
            workflow.count("BASE_SHA: ${{ env.QUALITY_GATE_BASE_SHA || github.event.pull_request.base.sha }}"),
            2,
        )

    def test_gate_tools_install_is_hash_locked(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "quality-gates.yml").read_text()
        # The supply-chain fix is pinned, not just present: the tools must
        # install from the hash-locked file with --require-hashes, never as
        # bare version-pinned arguments (whose transitive trees pip would
        # resolve fresh from PyPI on every run). Reverting the install step
        # to floating direct installs must turn this suite red.
        # The flag must appear on code lines, not just in prose comments:
        # deleting both pip flags while leaving the comments must fail.
        code_lines = [
            line
            for line in workflow.splitlines()
            if not line.strip().startswith("#")
        ]
        self.assertEqual(sum("--require-hashes" in line for line in code_lines), 2)
        self.assertIn("-r scripts/quality-gates-requirements.txt", workflow)
        for bare in ("ruff==", "semgrep==", "mutmut[patch]==", "zizmor=="):
            self.assertNotIn(bare, workflow)
        # mutmut and glob2 ship sdist-only: their builds must run with
        # build isolation disabled against the pre-installed pinned
        # backend, never fetching unpinned setuptools into an isolated
        # build env. The backend install must come first.
        self.assertIn("--no-build-isolation", workflow)
        self.assertIn("-r scripts/quality-gates-build-requirements.txt", workflow)
        self.assertLess(
            workflow.index("-r scripts/quality-gates-build-requirements.txt"),
            workflow.index("-r scripts/quality-gates-requirements.txt"),
        )

class TestWorkflowBoundedDownloads(unittest.TestCase):
    """Every tool download in the quality-gates workflow is bounded.

    A bare `curl -sSL` exits 0 on an HTTP error page and can hang a runner
    indefinitely on a stalled connection. Each download must fail on HTTP
    errors (-f) and carry explicit connect/total time bounds plus a bounded
    retry budget. Reverting any single download to `curl -sSL` must turn
    this suite red.
    """

    EXPECTED_DOWNLOADS = {
        "https://bootstrap.pypa.io/get-pip.py": "$RUNNER_TEMP/get-pip.py",
        "shellcheck/releases/download/": "shellcheck.tar.xz",
        "mvdan/sh/releases/download/": "shfmt",
        "rhysd/actionlint/releases/download/": "actionlint.tar.gz",
        "ast-grep/ast-grep/releases/download/": "ast-grep.zip",
    }
    BOUNDED_OPTIONS = (
        "--connect-timeout", "--max-time", "--retry", "--retry-delay", "--retry-max-time",
    )

    @staticmethod
    def curl_invocations():
        workflow = (REPO_ROOT / ".github" / "workflows" / "quality-gates.yml").read_text()
        invocations = []
        for line in workflow.splitlines():
            stripped = line.strip()
            # Only shell lines invoking curl are tokenized; other YAML
            # lines (prose, expressions) need not be valid shell.
            if stripped.startswith("#") or not re.search(r"(^|[\s;&|(])curl\s", stripped):
                continue
            tokens = shlex.split(stripped)
            invocations.append(tokens[tokens.index("curl") + 1:])
        return invocations

    @staticmethod
    def option_value(args, option):
        for index, arg in enumerate(args):
            if arg == option and index + 1 < len(args):
                return args[index + 1]
            if arg.startswith(option + "="):
                return arg.split("=", 1)[1]
        return None

    def test_all_five_downloads_are_present(self):
        invocations = self.curl_invocations()
        self.assertEqual(len(invocations), len(self.EXPECTED_DOWNLOADS))
        for url_part, output in self.EXPECTED_DOWNLOADS.items():
            matches = [args for args in invocations if any(url_part in arg for arg in args)]
            self.assertEqual(len(matches), 1, url_part)
            self.assertEqual(self.option_value(matches[0], "-o"), output, url_part)

    def test_every_download_fails_on_http_errors(self):
        for args in self.curl_invocations():
            short_flags = "".join(
                arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--")
                and arg != "-o"
            )
            self.assertTrue("f" in short_flags or "--fail" in args, f"missing --fail: {args}")
            # -sS keeps errors visible; -L follows release redirects.
            for flag in "sSL":
                self.assertIn(flag, short_flags, f"missing -{flag}: {args}")

    def test_every_download_has_time_and_retry_bounds(self):
        for args in self.curl_invocations():
            values = {}
            for option in self.BOUNDED_OPTIONS:
                value = self.option_value(args, option)
                self.assertIsNotNone(value, f"missing {option}: {args}")
                self.assertTrue(value.isdigit() and int(value) > 0, f"{option} {value!r}: {args}")
                values[option] = int(value)
            # Each attempt is capped by --max-time, so the worst case per
            # download is (retries + 1) attempts; keep it well under the
            # job budget so a stalled mirror fails the step instead of
            # hanging the runner.
            self.assertLess(values["--connect-timeout"], values["--max-time"], args)
            self.assertLessEqual(values["--max-time"] * (values["--retry"] + 1), 600, args)


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
