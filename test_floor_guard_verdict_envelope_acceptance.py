"""Independent acceptance contract for floor-guard JSON verdicts.

Run: python3 -B -m unittest -v test_floor_guard_verdict_envelope_acceptance

Only the external CLI is exercised, in disposable offline Git repositories.
The Builder must preserve this file and implement the contract it freezes:

* Exactly one stdout object: schema, outcome, totals, findings, diagnostic.
  Schema is floor-guard-verdicts.v1; outcomes clean/findings/unmeasurable
  retain the existing prose exit codes 0/1/2, respectively.
* totals is {"findings": N} after collection, {"findings": null} when
  collection cannot run. Unknown is not an invented clean count.
* Each finding has exactly rule, path, line, code, retryable, next_action.
  Codes reuse the stable rule identifiers. Paths are repository-relative.
  Added-line anchors use the new file's line number; aggregated removal
  and threshold findings may use null when no single anchor is known.
* diagnostic is null for completed checks, otherwise {code, retryable,
  next_action}; a missing merge base uses merge-base-unavailable and true.
* next_action is nonblank, single-line and at most 240 characters. JSON
  records never contain matched content. Fixed action wording is not frozen.

These tests intentionally fail before --json exists. No expectedFailure,
feature-detection skip, mock verdict, or implementation changes hide RED.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
GUARD_REL = Path("scripts/quality-gates/floor_guard.py")
TOP_KEYS = {"schema", "outcome", "totals", "findings", "diagnostic"}
FINDING_KEYS = {"rule", "path", "line", "code", "retryable", "next_action"}
DIAGNOSTIC_KEYS = {"code", "retryable", "next_action"}


class FloorGuardVerdictEnvelopeAcceptance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="floor-verdict-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        self.root.mkdir()
        home = Path(temporary.name) / "home"
        home.mkdir()
        # No inherited credentials, Git worktree/index overrides, or user hooks.
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.git("init", "--quiet")
        self.git("config", "user.name", "Acceptance Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", os.devnull)
        self.write(".constraintsignore", "# Fixture has no exemptions.\n")
        script = self.root / GUARD_REL
        script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / GUARD_REL, script)
        self.write("src/widget.py", "def value():\n    return 1\n")
        self.write("tests/test_widget.py",
                   "import unittest\n\nclass WidgetTest(unittest.TestCase):\n"
                   "    def test_value(self):\n"
                   "        self.assertEqual(1, 1)\n")
        self.write("CONSTRAINTS.md",
                   "# Quality floor\n\n| Coverage | Changed lines >= 80% |\n")
        self.write("docs/notes.md", "# Fixture notes\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture baseline")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, env=self.env, check=True,
            capture_output=True, text=True, timeout=30)

    def run_guard(self, json_mode=False, base=None, ignore=None):
        args = [sys.executable, "-B", str(self.root / GUARD_REL),
                "--base", self.base if base is None else base]
        if ignore is not None:
            args += ["--ignore-file", str(self.root / ignore)]
        if json_mode:
            args.append("--json")
        return subprocess.run(args, cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def assert_action(self, record):
        action = record["next_action"]
        self.assertIsInstance(action, str)
        self.assertTrue(action.strip(), "a verdict must tell the agent what to do")
        self.assertLessEqual(len(action), 240)
        self.assertEqual(action, action.strip())
        self.assertEqual(len(action.splitlines()), 1)

    def assert_verdict(self, outcome, count, *, base=None, ignore=None):
        """Reject wrong exits, missing JSON, extra documents and prose on stdout."""
        expected_exit = {"clean": 0, "findings": 1, "unmeasurable": 2}[outcome]
        prose = self.run_guard(base=base, ignore=ignore)
        self.assertEqual(prose.returncode, expected_exit, prose.stderr)
        result = self.run_guard(True, base=base, ignore=ignore)
        self.assertEqual(result.returncode, expected_exit,
                         f"--json must preserve prose exit {expected_exit}: "
                         f"{result.stderr}")
        try:
            obj = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail("--json must emit exactly one JSON object; "
                      f"exit={result.returncode}, stdout={result.stdout!r}, "
                      f"stderr={result.stderr!r}")
        self.assertIsInstance(obj, dict)
        self.assertEqual(set(obj), TOP_KEYS)
        self.assertEqual(obj["schema"], "floor-guard-verdicts.v1")
        self.assertEqual(obj["outcome"], outcome)
        self.assertEqual(obj["totals"], {"findings": count})
        if count is not None:
            self.assertIs(type(obj["totals"]["findings"]), int)
        self.assertIsInstance(obj["findings"], list)
        self.assertEqual(len(obj["findings"]), 0 if count is None else count)
        if outcome == "unmeasurable":
            diagnostic = obj["diagnostic"]
            self.assertIsInstance(diagnostic, dict)
            self.assertEqual(set(diagnostic), DIAGNOSTIC_KEYS)
            self.assertEqual(diagnostic["code"], "merge-base-unavailable")
            self.assertIs(diagnostic["retryable"], True)
            self.assert_action(diagnostic)
        else:
            self.assertIsNone(obj["diagnostic"])
        for finding in obj["findings"]:
            self.assertEqual(set(finding), FINDING_KEYS)
            self.assertIsInstance(finding["path"], str)
            path = PurePosixPath(finding["path"])
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertEqual(path.as_posix(), finding["path"])
            self.assertIs(finding["retryable"], False)
            self.assert_action(finding)
            if finding["line"] is not None:
                self.assertIs(type(finding["line"]), int)
                self.assertGreater(finding["line"], 0)
        return obj, result, prose

    def assert_finding(self, finding, rule, path, line=None):
        self.assertEqual(finding["rule"], rule)
        self.assertEqual(finding["code"], rule)
        self.assertEqual(finding["path"], path)
        if line is not None:
            self.assertEqual(finding["line"], line)

    def add_suppression(self, path="src/widget.py"):
        self.write(path, "def value():\n    return 1\n"
                   "# " + "noqa" + " -- fixture-suppression-canary\n")

    def test_clean_diff_has_zero_findings_and_exit_parity(self):
        obj, _, prose = self.assert_verdict("clean", 0)
        self.assertEqual(obj["findings"], [])
        self.assertEqual(prose.stdout, "floor-guard: clean\n")
        self.assertEqual(prose.stderr, "")

    def test_added_suppression_is_one_located_nonretryable_finding(self):
        self.add_suppression()
        obj, _, prose = self.assert_verdict("findings", 1)
        self.assert_finding(obj["findings"][0], "silenced-checker",
                            "src/widget.py", 3)
        self.assertIn("[silenced-checker] src/widget.py:", prose.stderr)

    def test_deleted_assertion_is_a_finding_not_a_clean_edit(self):
        self.write("tests/test_widget.py",
                   "import unittest\n\nclass WidgetTest(unittest.TestCase):\n"
                   "    def test_value(self):\n        value = 1\n")
        obj, _, _ = self.assert_verdict("findings", 1)
        self.assert_finding(obj["findings"][0], "assertion-removed",
                            "tests/test_widget.py")
        self.assertIn(obj["findings"][0]["line"], (None, 5))

    def test_constraints_threshold_decrease_is_a_finding(self):
        self.write("CONSTRAINTS.md",
                   "# Quality floor\n\n| Coverage | Changed lines >= 70% |\n")
        obj, _, _ = self.assert_verdict("findings", 1)
        self.assert_finding(obj["findings"][0], "threshold-lowered",
                            "CONSTRAINTS.md")
        self.assertIn(obj["findings"][0]["line"], (None, 3))

    def test_markdown_suppression_prose_stays_exempt(self):
        self.write("docs/notes.md", "# Fixture notes\nNever use "
                   "`# " + "noqa" + "` or `eslint-" + "disable` in code.\n")
        self.assert_verdict("clean", 0)

    def test_missing_merge_base_is_retryable_unmeasurable_not_findings(self):
        # A real unknown tree, even with a violation waiting in the worktree.
        self.add_suppression()
        obj, _, prose = self.assert_verdict(
            "unmeasurable", None, base="refs/heads/fixture-missing-base")
        self.assertEqual(obj["findings"], [])
        self.assertNotIn("floor-guard: clean", prose.stdout + prose.stderr)
        self.assertIn("no merge base", prose.stderr)

    def test_json_is_deterministic_across_repeated_runs_for_each_outcome(self):
        for outcome, count in (("clean", 0), ("findings", 1),
                               ("unmeasurable", None)):
            with self.subTest(outcome=outcome):
                if outcome == "findings":
                    self.add_suppression()
                base = ("refs/heads/fixture-missing-base"
                        if outcome == "unmeasurable" else None)
                first, result, _ = self.assert_verdict(outcome, count, base=base)
                second, repeated, _ = self.assert_verdict(outcome, count, base=base)
                self.assertEqual(first, second)
                self.assertEqual(result.stdout, repeated.stdout)

    def test_multiple_findings_keep_distinct_rules_paths_and_totals(self):
        self.add_suppression()
        self.write("tests/test_widget.py",
                   "import unittest\n\nclass WidgetTest(unittest.TestCase):\n"
                   "    def test_value(self):\n        value = 1\n")
        self.write("CONSTRAINTS.md",
                   "# Quality floor\n\n| Coverage | Changed lines >= 70% |\n")
        obj, _, _ = self.assert_verdict("findings", 3)
        self.assertEqual({(f["rule"], f["code"], f["path"])
                          for f in obj["findings"]}, {
            ("silenced-checker", "silenced-checker", "src/widget.py"),
            ("assertion-removed", "assertion-removed", "tests/test_widget.py"),
            ("threshold-lowered", "threshold-lowered", "CONSTRAINTS.md"),
        })

    def test_ignore_file_selection_does_not_change_between_modes(self):
        self.add_suppression()
        self.assert_verdict("findings", 1)
        self.write("fixture.ignore", "src/widget.py\n")
        self.assert_verdict("clean", 0, ignore="fixture.ignore")
        # An explicit alternative must not replace the default permanently.
        self.assert_verdict("findings", 1)

    def test_default_ignore_file_still_exempts_an_exact_path(self):
        self.add_suppression()
        self.write(".constraintsignore", "src/widget.py\n")
        self.assert_verdict("clean", 0)

    def test_explicit_base_is_honored_not_replaced_by_default(self):
        self.add_suppression()
        self.git("add", "src/widget.py")
        self.git("commit", "--quiet", "-m", "fixture suppression")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assert_verdict("clean", 0, base=head)
        self.assert_verdict("findings", 1, base=self.base)

    def test_untracked_code_and_spaced_paths_have_repo_relative_anchors(self):
        self.add_suppression("src/sp ace.py")
        obj, _, _ = self.assert_verdict("findings", 1)
        self.assert_finding(obj["findings"][0], "silenced-checker",
                            "src/sp ace.py", 3)

    def test_json_does_not_echo_matched_text_or_secret_candidate(self):
        canary = "fixture-private-value-not-a-real-credential-739182"
        self.write("src/widget.py", "def value():\n    return 1\n"
                   "credential = '" + canary + "'  # " + "noqa" + "\n")
        obj, result, _ = self.assert_verdict("findings", 1)
        self.assert_finding(obj["findings"][0], "silenced-checker",
                            "src/widget.py", 3)
        for forbidden in (canary, "credential =", "noqa", str(self.root)):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, result.stdout)
        # The failed-collection diagnostic must not reflect a caller's base.
        _, failed, _ = self.assert_verdict("unmeasurable", None, base=canary)
        self.assertNotIn(canary, failed.stdout)

    def test_threshold_finding_does_not_echo_constraint_row_contents(self):
        canary = "fixture-private-threshold-label-287416"
        self.write("CONSTRAINTS.md", "| " + canary + " | Coverage >= 80% |\n")
        self.git("add", "CONSTRAINTS.md")
        self.git("commit", "--quiet", "-m", "fixture threshold baseline")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("CONSTRAINTS.md", "| " + canary + " | Coverage >= 70% |\n")
        obj, result, _ = self.assert_verdict("findings", 1)
        self.assert_finding(obj["findings"][0], "threshold-lowered",
                            "CONSTRAINTS.md")
        self.assertNotIn(canary, result.stdout)
        self.assertNotIn("Coverage >=", result.stdout)

    def test_removed_assertion_finding_does_not_echo_old_file_contents(self):
        canary = "fixture-private-assertion-value-163852"
        self.write("tests/test_widget.py", "def test_value():\n"
                   "    assert '" + canary + "' != ''\n")
        self.git("add", "tests/test_widget.py")
        self.git("commit", "--quiet", "-m", "fixture assertion baseline")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("tests/test_widget.py", "def test_value():\n    value = 1\n")
        obj, result, _ = self.assert_verdict("findings", 1)
        self.assert_finding(obj["findings"][0], "assertion-removed",
                            "tests/test_widget.py")
        self.assertNotIn(canary, result.stdout)
        self.assertNotIn("assert '", result.stdout)

    def test_json_is_read_only_for_the_fixture_worktree(self):
        self.add_suppression()
        before = {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(self.root).parts}
        status = self.git("status", "--porcelain=v1", "--untracked-files=all").stdout
        self.assert_verdict("findings", 1)
        after = {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(self.root).parts}
        self.assertEqual(before, after)
        self.assertEqual(status, self.git("status", "--porcelain=v1",
                                          "--untracked-files=all").stdout)

    def test_help_discovers_json_without_running_a_check(self):
        result = subprocess.run(
            [sys.executable, "-B", str(self.root / GUARD_REL), "--help"],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--json", result.stdout)


if __name__ == "__main__":
    unittest.main()
