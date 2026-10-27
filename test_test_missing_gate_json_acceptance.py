"""Acceptance contract for the test-missing gate's ``--json`` mode.

These tests freeze the machine-readable contract of
``blocker_test_checks.py --json`` (and its verbatim staged twin under
``templates/org-github/test-missing/``) before it is implemented. They
exercise only the external CLI against hermetic temporary trees, so the
implementation may be structured any way that satisfies them. The
deterministic unit contract stays pinned by
``test_blocker_test_checks.py`` and ``test_test_missing_org_template.py``.

Frozen contract
===============

``--json`` is opt-in. With it, stdout is exactly one JSON object with
exactly these keys:

- ``schema``: the string ``"test-missing-verdicts.v1"``
- ``outcome``: ``"clean"``, ``"findings"`` or ``"unmeasurable"``
- ``mode``: the requested mode echoed back (``"warn"`` by default,
  else the ``--mode`` value as passed, including ``"enforcing"``)
- ``findings``: a list of objects with exactly the keys ``rule``,
  ``path``, ``line``, ``code``, ``next_action``:

  - ``rule``: one of ``src-without-tests``, ``vacuity``,
    ``changed-line-coverage``, ``missing-test-file``,
    ``unreadable-test-file``
  - ``path``: the changed path as supplied (repo-relative string)
  - ``line``: an integer >= 1 for ``changed-line-coverage``, else null
  - ``code``: a stable machine token (lower-case kebab/dot token).
    The same finding scenario always yields the same code, across
    paths, runs and both staged copies; different rules never share
    a code.
  - ``next_action``: non-empty, single-line, at most 200 characters.

- ``totals``: exactly ``{"findings": <count>}`` when the run could be
  measured (equal to ``len(findings)``), and exactly
  ``{"findings": None}`` when unmeasurable (an unknown count is never
  published as an invented zero). ``findings`` is empty when clean
  and when unmeasurable.

Exit codes are identical with and without ``--json``: 0 when clean or
in warn mode, 1 on findings in enforce mode, 2 on unreadable inputs
in every mode. Unreadable input lists or coverage data (missing or
malformed ``--changed-files-file``, ``--deleted-files-file`` or
``--coverage-json``) are ``unmeasurable`` -- never rendered clean --
and keep the existing ``cannot read`` diagnostic on stderr. A changed
test file that is missing or unreadable is a *finding* (rules above),
unless its deletion was declared. The ``tests-exempt`` label waives
only ``src-without-tests``; coverage and vacuity findings still fire.

Default prose output and the ``GITHUB_STEP_SUMMARY`` append are
unchanged without ``--json``, and the summary appended in ``--json``
mode is byte-identical to the one prose mode appends for the same
inputs. No file contents appear in any new field: paths, rule names,
codes and line anchors only. Output is deterministic: the same tree
yields byte-identical stdout, and both staged copies yield identical
verdicts for identical inputs.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_SCRIPT = HERE / "blocker_test_checks.py"
TWIN_SCRIPT = (HERE / "templates" / "org-github" / "test-missing"
               / "blocker_test_checks.py")
SCRIPTS = (ROOT_SCRIPT, TWIN_SCRIPT)

SCHEMA = "test-missing-verdicts.v1"
TOP_KEYS = {"schema", "outcome", "mode", "findings", "totals"}
ENTRY_KEYS = {"rule", "path", "line", "code", "next_action"}
RULES = {"src-without-tests", "vacuity", "changed-line-coverage",
         "missing-test-file", "unreadable-test-file"}
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MARKER = "CONTENT_ECHO_MARKER_7F3A"
TIMEOUT = 30

ASSERTING_TEST = "def test_it():\n    assert True\n"
VACUOUS_TEST = "def test_it():\n    x = 1\n"


def coverage(changed, covered):
    return json.dumps({"changed": changed, "covered": covered})


def run_gate(script, args, files=None, dirs=(), unreadable=(),
             summary_pre=None):
    """Run one staged copy in a hermetic tree.

    Returns (exit, stdout, stderr, summary) where summary is the final
    GITHUB_STEP_SUMMARY file content, or None if the file was never
    created. ``summary_pre`` seeds the file, proving append behavior.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, content in (files or {}).items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        for rel in dirs:
            (root / rel).mkdir(parents=True, exist_ok=True)
        for rel in unreadable:
            (root / rel).chmod(0)
        env = dict(os.environ)
        summary_path = root / "summary.md"
        env["GITHUB_STEP_SUMMARY"] = str(summary_path)
        if summary_pre is not None:
            summary_path.write_text(summary_pre)
        proc = subprocess.run(
            [sys.executable, "-B", str(script), *args],
            capture_output=True, text=True, cwd=str(root), env=env,
            timeout=TIMEOUT)
        summary = (summary_path.read_text()
                   if summary_path.exists() else None)
        return proc.returncode, proc.stdout, proc.stderr, summary


class JsonContractCase(unittest.TestCase):
    def parse_stdout(self, code, stdout, stderr):
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            self.fail(
                "stdout is not a single JSON document "
                f"(exit {code}): {stdout[:200]!r} {stderr[:200]!r}")

    def parse_json(self, script, args, **kwargs):
        code, stdout, stderr, summary = run_gate(
            script, [*args, "--json"], **kwargs)
        obj = self.parse_stdout(code, stdout, stderr)
        return code, obj, stdout, stderr, summary

    def assert_entry_shape(self, entry):
        self.assertEqual(set(entry), ENTRY_KEYS)
        self.assertIn(entry["rule"], RULES)
        self.assertIsInstance(entry["path"], str)
        self.assertTrue(entry["path"].strip(), "path must be non-empty")
        if entry["rule"] == "changed-line-coverage":
            self.assertEqual(type(entry["line"]), int)
            self.assertGreaterEqual(entry["line"], 1)
        else:
            self.assertIsNone(entry["line"], entry)
        self.assertIsInstance(entry["code"], str)
        self.assertRegex(entry["code"], CODE_RE)
        self.assertIsInstance(entry["next_action"], str)
        self.assertTrue(entry["next_action"].strip(),
                        "next_action must be non-empty")
        self.assertNotIn("\n", entry["next_action"])
        self.assertLessEqual(len(entry["next_action"]), 200)
        self.assertNotIn(MARKER, json.dumps(entry))

    def assert_verdict(self, obj, outcome, mode, expected):
        """``expected`` is a list of (rule, path, line) triples."""
        self.assertEqual(set(obj), TOP_KEYS)
        self.assertEqual(obj["schema"], SCHEMA)
        self.assertEqual(obj["outcome"], outcome)
        self.assertEqual(obj["mode"], mode)
        self.assertEqual(obj["totals"], {"findings": len(expected)})
        got = sorted(
            (f["rule"], f["path"], f["line"]) for f in obj["findings"])
        self.assertEqual(got, sorted(expected))
        for entry in obj["findings"]:
            self.assert_entry_shape(entry)

    def assert_json_run(self, args, expected_exit, outcome, mode,
                        expected, script=ROOT_SCRIPT, **kwargs):
        code, obj, stdout, stderr, summary = self.parse_json(
            script, args, **kwargs)
        self.assertEqual(code, expected_exit, stdout + stderr)
        self.assertEqual(stderr, "",
                         "measured --json runs keep stderr clean")
        self.assertNotIn("test-missing gate findings", stdout)
        self.assertNotIn(MARKER, stdout)
        self.assert_verdict(obj, outcome, mode, expected)
        return obj, stdout, summary

    def assert_unmeasurable(self, args, mode, script=ROOT_SCRIPT,
                            **kwargs):
        code, obj, stdout, stderr, _summary = self.parse_json(
            script, args, **kwargs)
        self.assertEqual(code, 2, stdout + stderr)
        self.assertEqual(set(obj), TOP_KEYS)
        self.assertEqual(obj["schema"], SCHEMA)
        self.assertEqual(obj["outcome"], "unmeasurable")
        self.assertNotEqual(obj["outcome"], "clean")
        self.assertEqual(obj["mode"], mode)
        self.assertEqual(obj["findings"], [])
        self.assertEqual(obj["totals"], {"findings": None})
        self.assertIn("cannot read", stderr)


class CleanJsonTest(JsonContractCase):
    def test_docs_only_change_is_clean(self):
        self.assert_json_run(
            ["--changed-files", "README.md,docs/runbook.md",
             "--mode", "enforce"], 0, "clean", "enforce", [])

    def test_src_with_asserting_test_is_clean(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_foo.py",
             "--mode", "enforce"], 0, "clean", "enforce", [],
            files={"src/foo.py": "x = 1\n",
                   "tests/test_foo.py": ASSERTING_TEST})

    def test_no_changed_files_is_clean(self):
        self.assert_json_run(["--mode", "enforce"], 0, "clean",
                             "enforce", [])

    def test_changed_files_file_input_is_clean(self):
        self.assert_json_run(
            ["--changed-files-file", "changed.txt",
             "--mode", "enforce"], 0, "clean", "enforce", [],
            files={"changed.txt": "README.md\n"})


class TypedFindingsTest(JsonContractCase):
    def test_src_without_tests(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py", "--mode", "enforce"],
            1, "findings", "enforce",
            [("src-without-tests", "src/foo.py", None)])

    def test_src_without_tests_one_entry_per_src_file(self):
        obj, _out, _s = self.assert_json_run(
            ["--changed-files", "src/b.py,src/a.py",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("src-without-tests", "src/a.py", None),
             ("src-without-tests", "src/b.py", None)])
        codes = {f["code"] for f in obj["findings"]}
        self.assertEqual(len(codes), 1,
                         "same scenario must share one stable code")

    def test_vacuous_test(self):
        self.assert_json_run(
            ["--changed-files", "tests/test_v.py", "--mode", "enforce"],
            1, "findings", "enforce",
            [("vacuity", "tests/test_v.py", None)],
            files={"tests/test_v.py": VACUOUS_TEST})

    def test_empty_test_module_is_vacuity(self):
        self.assert_json_run(
            ["--changed-files", "tests/test_empty.py",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("vacuity", "tests/test_empty.py", None)],
            files={"tests/test_empty.py": ""})

    def test_unparseable_test_is_vacuity(self):
        self.assert_json_run(
            ["--changed-files", "tests/test_bad.py",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("vacuity", "tests/test_bad.py", None)],
            files={"tests/test_bad.py": "def test_it(:\n"
                                         "    assert False\n"})

    def test_changed_line_coverage_names_each_uncovered_line(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_foo.py",
             "--mode", "enforce", "--coverage-json", "cov.json"],
            1, "findings", "enforce",
            [("changed-line-coverage", "src/foo.py", 2),
             ("changed-line-coverage", "src/foo.py", 9)],
            files={"src/foo.py": "x = 1\n",
                   "tests/test_foo.py": ASSERTING_TEST,
                   "cov.json": coverage({"src/foo.py": [9, 2, 5]},
                                        {"src/foo.py": [5]})})

    def test_fully_covered_changed_lines_are_clean(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_foo.py",
             "--mode", "enforce", "--coverage-json", "cov.json"],
            0, "clean", "enforce", [],
            files={"src/foo.py": "x = 1\n",
                   "tests/test_foo.py": ASSERTING_TEST,
                   "cov.json": coverage({"src/foo.py": [1]},
                                        {"src/foo.py": [1, 2]})})

    def test_missing_test_file(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_absent.py",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("missing-test-file", "tests/test_absent.py", None)],
            files={"src/foo.py": "x = 1\n"})

    def test_unreadable_test_file_directory(self):
        # A directory at the test path is unreadable as a file even
        # when permission bits are ignored (e.g. running as root).
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_dir.py",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("unreadable-test-file", "tests/test_dir.py", None)],
            files={"src/foo.py": "x = 1\n"},
            dirs=("tests/test_dir.py",))

    def test_unreadable_test_file_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            probe.write_text("x = 1\n")
            probe.chmod(0)
            try:
                readable = probe.read_text()
            except OSError:
                readable = None
            if readable is not None:
                self.skipTest("mode 000 still readable (running as root?)")
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_perm.py",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("unreadable-test-file", "tests/test_perm.py", None)],
            files={"src/foo.py": "x = 1\n",
                   "tests/test_perm.py": VACUOUS_TEST},
            unreadable=("tests/test_perm.py",))

    def test_codes_are_distinct_per_rule(self):
        scenarios = [
            (["--changed-files", "src/foo.py", "--mode", "enforce"],
             {}),
            (["--changed-files", "tests/test_v.py",
              "--mode", "enforce"],
             {"files": {"tests/test_v.py": VACUOUS_TEST}}),
            (["--changed-files", "src/foo.py,tests/test_foo.py",
              "--mode", "enforce", "--coverage-json", "cov.json"],
             {"files": {"src/foo.py": "x = 1\n",
                        "tests/test_foo.py": ASSERTING_TEST,
                        "cov.json": coverage({"src/foo.py": [3]},
                                             {"src/foo.py": []})}}),
            (["--changed-files", "tests/test_absent.py",
              "--mode", "enforce"], {}),
            (["--changed-files", "tests/test_dir.py",
              "--mode", "enforce"],
             {"dirs": ("tests/test_dir.py",)}),
        ]
        codes_by_rule = {}
        for args, kwargs in scenarios:
            _code, obj, _out, _err, _s = self.parse_json(
                ROOT_SCRIPT, args, **kwargs)
            self.assertEqual(len(obj["findings"]), 1, obj)
            entry = obj["findings"][0]
            codes_by_rule[entry["rule"]] = entry["code"]
        self.assertEqual(set(codes_by_rule), RULES)
        self.assertEqual(len(set(codes_by_rule.values())), len(RULES),
                         "different rules must never share a code")


class LabelDeletedCoverageInterplayTest(JsonContractCase):
    def test_tests_exempt_label_waives_src_rule(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py", "--labels", "tests-exempt",
             "--mode", "enforce"], 0, "clean", "enforce", [])

    def test_other_labels_do_not_waive_src_rule(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py",
             "--labels", "bug,enhancement", "--mode", "enforce"],
            1, "findings", "enforce",
            [("src-without-tests", "src/foo.py", None)])

    def test_tests_exempt_label_does_not_waive_coverage(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py", "--labels", "tests-exempt",
             "--mode", "enforce", "--coverage-json", "cov.json"],
            1, "findings", "enforce",
            [("changed-line-coverage", "src/foo.py", 1)],
            files={"cov.json": coverage({"src/foo.py": [1]},
                                        {"src/foo.py": []})})

    def test_tests_exempt_label_does_not_waive_vacuity(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_v.py",
             "--labels", "tests-exempt", "--mode", "enforce"],
            1, "findings", "enforce",
            [("vacuity", "tests/test_v.py", None)],
            files={"tests/test_v.py": VACUOUS_TEST})

    def test_declared_deleted_test_is_not_missing(self):
        self.assert_json_run(
            ["--changed-files", "tests/test_gone.py",
             "--deleted-files", "tests/test_gone.py",
             "--mode", "enforce"], 0, "clean", "enforce", [])

    def test_declared_deleted_src_is_clean(self):
        self.assert_json_run(
            ["--changed-files", "src/old.py",
             "--deleted-files", "src/old.py", "--mode", "enforce"],
            0, "clean", "enforce", [])

    def test_deleted_test_does_not_satisfy_src_gate(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_gone.py",
             "--deleted-files", "tests/test_gone.py",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("src-without-tests", "src/foo.py", None)],
            files={"src/foo.py": "x = 1\n"})

    def test_deleted_files_file_declares_deletion(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py,tests/test_gone.py",
             "--deleted-files-file", "deleted.txt",
             "--mode", "enforce"], 1, "findings", "enforce",
            [("src-without-tests", "src/foo.py", None)],
            files={"src/foo.py": "x = 1\n",
                   "deleted.txt": "tests/test_gone.py\n"})

    def test_src_and_coverage_findings_coexist(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py", "--mode", "enforce",
             "--coverage-json", "cov.json"], 1, "findings", "enforce",
            [("src-without-tests", "src/foo.py", None),
             ("changed-line-coverage", "src/foo.py", 4)],
            files={"cov.json": coverage({"src/foo.py": [4]},
                                        {"src/foo.py": []})})


class ExitParityTest(JsonContractCase):
    """Prose vs JSON exit codes, in warn AND enforce modes."""

    SCENARIOS = [
        ("clean",
         ["--changed-files", "README.md"], {}, 0, 0),
        ("src-without-tests",
         ["--changed-files", "src/foo.py"], {}, 0, 1),
        ("vacuity",
         ["--changed-files", "tests/test_v.py"],
         {"files": {"tests/test_v.py": VACUOUS_TEST}}, 0, 1),
        ("missing-test-file",
         ["--changed-files", "tests/test_absent.py"], {}, 0, 1),
    ]

    def test_exit_parity_warn_and_enforce(self):
        for name, base_args, kwargs, warn_exit, enforce_exit in \
                self.SCENARIOS:
            for mode, expected in (("warn", warn_exit),
                                   ("enforce", enforce_exit)):
                with self.subTest(scenario=name, mode=mode):
                    args = [*base_args, "--mode", mode]
                    prose_code, _o, _e, _s = run_gate(
                        ROOT_SCRIPT, args, **kwargs)
                    self.assertEqual(prose_code, expected)
                    json_code, obj, _o, _e, _s = self.parse_json(
                        ROOT_SCRIPT, args, **kwargs)
                    self.assertEqual(json_code, prose_code)
                    self.assertEqual(obj["mode"], mode)
                    expected_outcome = ("clean" if not obj["findings"]
                                        else "findings")
                    self.assertEqual(obj["outcome"], expected_outcome)

    def test_warn_mode_reports_findings_with_exit_zero(self):
        obj, _out, _s = self.assert_json_run(
            ["--changed-files", "src/foo.py", "--mode", "warn"],
            0, "findings", "warn",
            [("src-without-tests", "src/foo.py", None)])
        self.assertEqual(obj["totals"], {"findings": 1})

    def test_default_mode_is_warn(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py"], 0, "findings", "warn",
            [("src-without-tests", "src/foo.py", None)])

    def test_enforcing_alias_echoes_mode_and_enforces(self):
        self.assert_json_run(
            ["--changed-files", "src/foo.py", "--mode", "enforcing"],
            1, "findings", "enforcing",
            [("src-without-tests", "src/foo.py", None)])


class UnmeasurableTest(JsonContractCase):
    def test_unreadable_changed_files_file(self):
        for mode in ("warn", "enforce"):
            with self.subTest(mode=mode):
                self.assert_unmeasurable(
                    ["--changed-files-file", "nope.txt",
                     "--mode", mode], mode)

    def test_unreadable_deleted_files_file(self):
        self.assert_unmeasurable(
            ["--changed-files", "src/foo.py",
             "--deleted-files-file", "nope.txt", "--mode", "enforce"],
            "enforce")

    def test_missing_coverage_json(self):
        self.assert_unmeasurable(
            ["--changed-files", "src/foo.py", "--mode", "enforce",
             "--coverage-json", "nope.json"], "enforce")

    def test_malformed_coverage_json(self):
        self.assert_unmeasurable(
            ["--changed-files", "src/foo.py", "--mode", "warn",
             "--coverage-json", "cov.json"], "warn",
            files={"cov.json": "{ not json\n"})

    def test_unmeasurable_exit_parity_with_prose(self):
        for mode in ("warn", "enforce"):
            with self.subTest(mode=mode):
                args = ["--changed-files-file", "nope.txt",
                        "--mode", mode]
                prose_code, _o, prose_err, _s = run_gate(
                    ROOT_SCRIPT, args)
                self.assertEqual(prose_code, 2)
                self.assertIn("cannot read", prose_err)


class ParityMatrixTest(JsonContractCase):
    """Twin parity, prose finding-count parity, SUMMARY parity and
    determinism across the scenario matrix."""

    MATRIX = [
        ("clean-docs", ["--changed-files", "README.md"], {}),
        ("src-without-tests", ["--changed-files", "src/foo.py"], {}),
        ("vacuity", ["--changed-files", "tests/test_v.py"],
         {"files": {"tests/test_v.py": VACUOUS_TEST}}),
        ("missing", ["--changed-files", "tests/test_absent.py"], {}),
        ("unreadable-dir", ["--changed-files", "tests/test_dir.py"],
         {"dirs": ("tests/test_dir.py",)}),
        ("coverage",
         ["--changed-files", "src/foo.py,tests/test_foo.py",
          "--coverage-json", "cov.json"],
         {"files": {"src/foo.py": "x = 1\n",
                    "tests/test_foo.py": ASSERTING_TEST,
                    "cov.json": coverage({"src/foo.py": [7]},
                                         {"src/foo.py": []})}}),
        ("exempt", ["--changed-files", "src/foo.py",
                    "--labels", "tests-exempt"], {}),
        ("unmeasurable", ["--changed-files-file", "nope.txt"], {}),
    ]

    def test_twin_parity_across_matrix(self):
        for name, base_args, kwargs in self.MATRIX:
            for mode in ("warn", "enforce"):
                with self.subTest(scenario=name, mode=mode):
                    args = [*base_args, "--mode", mode, "--json"]
                    root_run = run_gate(ROOT_SCRIPT, args, **kwargs)
                    twin_run = run_gate(TWIN_SCRIPT, args, **kwargs)
                    self.assertEqual(root_run[0], twin_run[0],
                                     "twin exit codes must match")
                    self.assertEqual(
                        self.parse_stdout(root_run[0], root_run[1],
                                          root_run[2]),
                        self.parse_stdout(twin_run[0], twin_run[1],
                                          twin_run[2]),
                        "both staged copies must emit identical verdicts")
                    self.assertEqual(
                        root_run[1], twin_run[1],
                        "both staged copies must emit identical bytes")

    def test_prose_finding_count_parity_across_matrix(self):
        for name, base_args, kwargs in self.MATRIX:
            with self.subTest(scenario=name):
                args = [*base_args, "--mode", "enforce"]
                prose_code, prose_out, _e, _s = run_gate(
                    ROOT_SCRIPT, args, **kwargs)
                json_code, obj, _o, _e, _s = self.parse_json(
                    ROOT_SCRIPT, args, **kwargs)
                self.assertEqual(json_code, prose_code)
                if obj["outcome"] == "unmeasurable":
                    self.assertEqual(prose_code, 2)
                    continue
                prose_findings = prose_out.count("\n- ") + (
                    1 if prose_out.startswith("- ") else 0)
                if "no findings" in prose_out:
                    prose_findings = 0
                self.assertEqual(len(obj["findings"]), prose_findings)
                for entry in obj["findings"]:
                    self.assertIn(entry["path"], prose_out)

    def test_summary_parity_across_matrix(self):
        for name, base_args, kwargs in self.MATRIX:
            for mode in ("warn", "enforce"):
                with self.subTest(scenario=name, mode=mode):
                    args = [*base_args, "--mode", mode]
                    _c, _o, _e, prose_summary = run_gate(
                        ROOT_SCRIPT, args, summary_pre="PRE-EXISTING\n",
                        **kwargs)
                    _c, json_out, json_err, json_summary = run_gate(
                        ROOT_SCRIPT, [*args, "--json"],
                        summary_pre="PRE-EXISTING\n", **kwargs)
                    self.parse_stdout(_c, json_out, json_err)
                    self.assertEqual(
                        json_summary, prose_summary,
                        "--json must append the identical summary")
                    if prose_summary is not None:
                        self.assertTrue(
                            prose_summary.startswith("PRE-EXISTING\n"))

    def test_json_output_byte_identical_across_runs(self):
        args = ["--changed-files", "src/foo.py", "--mode", "enforce",
                "--coverage-json", "cov.json", "--json"]
        kwargs = {"files": {"cov.json": coverage({"src/foo.py": [4]},
                                                 {"src/foo.py": []})}}
        first = run_gate(ROOT_SCRIPT, args, **kwargs)
        second = run_gate(ROOT_SCRIPT, args, **kwargs)
        self.parse_stdout(first[0], first[1], first[2])
        self.assertEqual(first, second)


class NoContentEchoTest(JsonContractCase):
    def test_file_contents_never_echoed(self):
        cases = [
            ("vacuity",
             ["--changed-files", "src/foo.py,tests/test_foo.py",
              "--mode", "enforce"],
             {"files": {
                 "src/foo.py": f"# {MARKER}\nx = 1\n",
                 "tests/test_foo.py": f"# {MARKER}\n" + VACUOUS_TEST}}),
            ("coverage",
             ["--changed-files", "src/foo.py,tests/test_foo.py",
              "--mode", "enforce", "--coverage-json", "cov.json"],
             {"files": {
                 "src/foo.py": f"# {MARKER}\nx = 1\n",
                 "tests/test_foo.py": f"# {MARKER}\n" + ASSERTING_TEST,
                 "cov.json": coverage({"src/foo.py": [1]},
                                      {"src/foo.py": []})}}),
        ]
        for name, args, kwargs in cases:
            with self.subTest(scenario=name):
                code, obj, stdout, stderr, summary = self.parse_json(
                    ROOT_SCRIPT, args, **kwargs)
                self.assertEqual(code, 1)
                self.assertTrue(obj["findings"])
                self.assertNotIn(MARKER, stdout)
                self.assertNotIn(MARKER, stderr)
                self.assertNotIn(MARKER, summary or "")


class UsageAndProseControlTest(unittest.TestCase):
    """--help documents the flag; default prose behavior (already
    true today) must not regress."""

    def test_help_documents_json_flag(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                code, stdout, _e, _s = run_gate(script, ["--help"])
                self.assertEqual(code, 0)
                self.assertIn("--json", stdout)

    def test_prose_src_without_tests_unchanged(self):
        code, stdout, _e, _s = run_gate(
            ROOT_SCRIPT, ["--changed-files", "src/foo.py",
                          "--mode", "enforce"])
        self.assertEqual(code, 1)
        self.assertIn("src change without tests: src/foo.py", stdout)

    def test_prose_clean_unchanged(self):
        code, stdout, _e, _s = run_gate(
            ROOT_SCRIPT, ["--changed-files", "README.md",
                          "--mode", "enforce"])
        self.assertEqual(code, 0)
        self.assertIn("no findings", stdout)

    def test_prose_warn_unchanged(self):
        code, stdout, _e, _s = run_gate(
            ROOT_SCRIPT, ["--changed-files", "src/foo.py",
                          "--mode", "warn"])
        self.assertEqual(code, 0)
        self.assertIn("src change without tests", stdout)


if __name__ == "__main__":
    unittest.main()
