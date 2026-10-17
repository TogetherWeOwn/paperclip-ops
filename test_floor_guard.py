"""Unit tests for the floor guard (fast, hermetic).

Covers scripts/quality-gates/floor_guard.py pure logic: suppression, skip,
stub, assertion-removed, deleted-test, threshold-lowered and new-exception
detection, plus the markdown exemption for documented suppression names.
No git writes, no network. Run: python3 -m unittest test_floor_guard
"""
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
GUARD_PATH = REPO_ROOT / "scripts" / "quality-gates" / "floor_guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location(
        "floor_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_guard()


class TestAddedSuppressions(unittest.TestCase):
    def test_noqa_in_py_is_flagged(self):
        self.assertIn("silenced-checker",
                      guard.classify_added("ops/a.py", "x = 1  # noqa"))

    def test_shellcheck_disable_in_sh_is_flagged(self):
        self.assertIn("silenced-checker",
                      guard.classify_added("ops/a.sh", "# shellcheck disable=SC2086"))

    def test_shellcheck_disable_in_workflow_is_flagged(self):
        self.assertIn("silenced-checker",
                      guard.classify_added(".github/workflows/q.yml",
                                           "# shellcheck disable=SC2086"))

    def test_nosemgrep_in_py_is_flagged(self):
        self.assertIn("silenced-checker",
                      guard.classify_added("ops/a.py", "x = 1  # nosemgrep"))

    def test_suppression_names_in_markdown_are_exempt(self):
        # CONSTRAINTS.md documents the floor in prose; naming a
        # suppression there must not trip the guard (suppressions only
        # function in code, never in markdown).
        self.assertEqual(
            guard.classify_added("CONSTRAINTS.md", "- No `# noqa` comments"), [])
        self.assertEqual(
            guard.classify_added("docs/notes.md", "never use eslint-disable"), [])

    def test_plain_code_is_clean(self):
        self.assertEqual(guard.classify_added("ops/a.py", "x = timeout(5)"), [])

    def test_zizmor_ignore_in_workflow_is_flagged(self):
        # Audit silencers let a banned trigger past the HARD gates, so a
        # new one must fail gate 8 (reviewer bypass probe).
        self.assertIn("silenced-checker",
                      guard.classify_added(".github/workflows/q.yml",
                                           "run: x # zizmor: ignore[dangerous-triggers]"))

    def test_ast_grep_ignore_is_flagged(self):
        self.assertIn("silenced-checker",
                      guard.classify_added("ops/a.py",
                                           "x = 1  # ast-grep-ignore: rule"))

    def test_reviewed_workflow_run_exception_is_clean(self):
        # Tracked exception: the reviewed review-trigger
        # workflow_run ignore stays green; a new bare ignore still fails.
        self.assertEqual(
            guard.classify_added(
                ".github/workflows/review-trigger.yml",
                "  # zizmor: ignore[dangerous-triggers] -- workflow_run is load-bearing here"),
            [])
        self.assertIn("silenced-checker",
                      guard.classify_added(".github/workflows/q.yml",
                                           "  # zizmor: ignore[dangerous-triggers]"))

    def test_reviewed_text_on_trigger_line_still_fails(self):
        # The tracked exception is the exact (path, comment-line)
        # pair, not a substring. The reviewed text trailing a trigger line
        # in another workflow must still fail gate 8.
        self.assertIn(
            "silenced-checker",
            guard.classify_added(
                ".github/workflows/other.yml",
                "- pull_request_target  # zizmor: ignore[dangerous-triggers]"
                " -- workflow_run is load-bearing here"))

    def test_reviewed_text_in_other_file_still_fails(self):
        # The same comment-only line outside review-trigger.yml is not the
        # reviewed exception.
        self.assertIn(
            "silenced-checker",
            guard.classify_added(
                ".github/workflows/other.yml",
                "  # zizmor: ignore[dangerous-triggers]"
                " -- workflow_run is load-bearing here"))

    def test_reviewed_text_as_code_in_same_file_still_fails(self):
        # Even in review-trigger.yml, only the comment-only line is exempt;
        # the text on a code line still fails.
        self.assertIn(
            "silenced-checker",
            guard.classify_added(
                ".github/workflows/review-trigger.yml",
                "run: echo x  # zizmor: ignore[dangerous-triggers]"
                " -- workflow_run is load-bearing here"))


class TestAddedSkips(unittest.TestCase):
    def test_it_skip_is_flagged(self):
        self.assertIn("test-made-easier",
                      guard.classify_added("ops/a.test.js", "it.skip('x', f)"))

    def test_pytest_mark_skip_is_flagged(self):
        self.assertIn("test-made-easier",
                      guard.classify_added("test_a.py", "@pytest.mark.skip(reason='x')"))

    def test_unittest_skip_is_flagged(self):
        self.assertIn("test-made-easier",
                      guard.classify_added("test_a.py", "@unittest.skip('x')"))

    def test_skipping_echo_is_not_flagged(self):
        self.assertEqual(
            guard.classify_added("scripts/quality-gates/mutmut_changed.sh",
                                 'echo "mutation gate: nothing to diff -- skipping"'), [])

    def test_test_name_containing_skips_is_not_flagged(self):
        self.assertEqual(
            guard.classify_added("test_quality_gates.py",
                                 "def test_parses_rows_skips_comments(self):"), [])


class TestAddedStubs(unittest.TestCase):
    def test_not_implemented_is_flagged(self):
        self.assertIn("unfinished-work",
                      guard.classify_added("ops/a.py", "raise NotImplementedError"))

    def test_todo_in_code_is_flagged(self):
        self.assertIn("unfinished-work",
                      guard.classify_added("ops/a.py", "# TODO: implement this"))

    def test_todo_in_markdown_is_exempt(self):
        self.assertEqual(
            guard.classify_added("docs/plan.md", "- TODO: measure coverage"), [])


class TestAssertionRemoved(unittest.TestCase):
    def test_removed_assert_in_test_is_flagged(self):
        self.assertTrue(guard.is_assertion_removed("test_a.py", "    assert x == 1"))

    def test_removed_expect_in_test_is_flagged(self):
        self.assertTrue(guard.is_assertion_removed("ops/a.test.js", "  expect(x).toBe(1)"))

    def test_removed_self_assert_is_flagged(self):
        # The repo has 5,579 self.assertX calls vs 16 bare asserts: deleting
        # a real unittest assertion must fail the HARD gate.
        self.assertTrue(guard.is_assertion_removed(
            "test_a.py", "    self.assertEqual(result.returncode, 0)"))
        self.assertTrue(guard.is_assertion_removed(
            "test_a.py", '    self.assertIn("ok", describe())'))

    def test_should_word_alone_is_clean(self):
        # Changing "should" to "must" inside an assertion message keeps the
        # assertion: the word alone must not trip the gate.
        self.assertFalse(guard.is_assertion_removed(
            "test_a.py", "  # should handle empty input"))

    def test_removed_plain_line_is_clean(self):
        self.assertFalse(guard.is_assertion_removed("test_a.py", "    x = compute()"))

    def test_assert_in_non_test_is_clean(self):
        self.assertFalse(guard.is_assertion_removed("ops/a.py", "    assert x == 1"))


class TestAssertionNetDecrease(unittest.TestCase):
    def test_message_only_edit_is_clean(self):
        # An edit that keeps its assertion (message wording only) is not a
        # stripped assertion, even though one removed line held an assert.
        removed = [("test_a.py", '    self.assertIn(value, "should FAIL")')]
        added = [("test_a.py", '    self.assertIn(value, "must FAIL")')]
        findings = guard.check(added, removed, "", [])
        self.assertEqual(
            [f for f in findings if f[0] == "assertion-removed"], [])

    def test_deleted_assertion_is_flagged(self):
        removed = [("test_a.py", "    self.assertEqual(compute(), 42)")]
        findings = guard.check([], removed, "", [])
        self.assertTrue(
            any(f[0] == "assertion-removed" for f in findings))


class TestDeletedTests(unittest.TestCase):
    def test_deleted_test_file_is_flagged(self):
        self.assertEqual(guard.find_deleted_tests("D\ttest_a.py"), ["test_a.py"])

    def test_deleted_bash_test_is_flagged(self):
        self.assertEqual(guard.find_deleted_tests("D\ttest_sweep.sh"), ["test_sweep.sh"])

    def test_deleted_non_test_is_clean(self):
        self.assertEqual(guard.find_deleted_tests("D\tops/a.py"), [])

    def test_modified_test_is_clean(self):
        self.assertEqual(guard.find_deleted_tests("M\ttest_a.py"), [])


class TestThresholdsAndExceptions(unittest.TestCase):
    def test_lowered_number_is_flagged(self):
        removed = [("CONSTRAINTS.md", "| Coverage | Changed lines >= 80% |")]
        added = [("CONSTRAINTS.md", "| Coverage | Changed lines >= 70% |")]
        self.assertEqual(len(guard.compare_thresholds(removed, added)), 1)

    def test_tightened_number_is_silent(self):
        removed = [("CONSTRAINTS.md", "| Coverage | Changed lines >= 70% |")]
        added = [("CONSTRAINTS.md", "| Coverage | Changed lines >= 80% |")]
        self.assertEqual(guard.compare_thresholds(removed, added), [])

    def test_growth_capped_row_shrink_is_silent(self):
        # Backlog rows marked "must not grow" tighten by shrinking: a
        # sanctioned fix sweep must not trip the guard.
        removed = [("CONSTRAINTS.md", "| Subprocess-timeout backlog | 64 rows | must not grow; shrinks only via reviewed sweep |")]
        added = [("CONSTRAINTS.md", "| Subprocess-timeout backlog | 30 rows | must not grow; shrinks only via reviewed sweep |")]
        self.assertEqual(guard.compare_thresholds(removed, added), [])

    def test_growth_capped_row_growth_is_flagged(self):
        removed = [("CONSTRAINTS.md", "| Subprocess-timeout backlog | 30 rows | must not grow; shrinks only via reviewed sweep |")]
        added = [("CONSTRAINTS.md", "| Subprocess-timeout backlog | 31 rows | must not grow; shrinks only via reviewed sweep |")]
        self.assertEqual(len(guard.compare_thresholds(removed, added)), 1)

    def test_new_exception_row_is_flagged(self):
        self.assertIn("new-exception",
                      guard.classify_added("CONSTRAINTS.md", "| W1 | lint | src/legacy | tracked | me | 2027-01-01 |"))

    def test_constraints_prose_edit_is_clean(self):
        self.assertEqual(
            guard.classify_added("CONSTRAINTS.md", "| Coverage | Changed lines >= 80% | `pytest` | CI |"), [])


class TestConstraintsIgnore(unittest.TestCase):
    def test_ignore_file_exempts_path(self):
        self.assertTrue(guard.is_ignored("ops/legacy/a.py", ["ops/legacy/*"]))
        self.assertFalse(guard.is_ignored("ops/a.py", ["ops/legacy/*"]))


class TestSpacedAndNonAsciiPaths(unittest.TestCase):
    # Reviewer probe: git writes `+++ b/sp ace.py<TAB>` for spaced paths
    # and octal-quotes non-ASCII ones (raw with core.quotePath=false).
    # parse_diff must not mangle either, or the HARD guard skips the file.
    def test_parse_diff_strips_trailing_tab_from_spaced_path(self):
        diff = ("diff --git a/sp ace.py b/sp ace.py\n"
                "+++ b/sp ace.py\t\n"
                "+x = 1  # noqa\n")
        added, _ = guard.parse_diff(diff)
        self.assertEqual(added, [("sp ace.py", "x = 1  # noqa")])

    def test_spaced_path_suppression_is_flagged(self):
        findings = guard.check([("sp ace.py", "x = 1  # noqa")], [], "", [])
        self.assertTrue(any(f[0] == "silenced-checker" and f[1] == "sp ace.py"
                            for f in findings))

    def test_non_ascii_path_suppression_is_flagged(self):
        findings = guard.check([("é.py", "x = 1  # noqa")], [], "", [])
        self.assertTrue(any(f[0] == "silenced-checker" and f[1] == "é.py"
                            for f in findings))

    def test_deleted_spaced_test_is_flagged(self):
        self.assertEqual(guard.find_deleted_tests("D\tsp ace_test.py"),
                         ["sp ace_test.py"])

    def test_deleted_spaced_non_test_is_clean(self):
        self.assertEqual(guard.find_deleted_tests("D\tsp ace.py"), [])


class TestCollectFailClosed(unittest.TestCase):
    # A failed/timed-out git collection (None) is an unknown tree, never
    # an empty diff: collect must return None (rc 2), not clean data.
    def _git_failing_at(self, failing_cmd):
        def fake(args, allowed_returncodes=(0,)):
            if args and args[0] == "merge-base":
                return "abc123\n"
            if args and args[0] == failing_cmd:
                return None
            return ""
        return fake

    def test_tracked_diff_failure_returns_none(self):
        orig, guard.git = guard.git, self._git_failing_at("diff")
        try:
            self.assertIsNone(guard.collect("origin/main"))
        finally:
            guard.git = orig

    def test_untracked_listing_failure_returns_none(self):
        orig, guard.git = guard.git, self._git_failing_at("ls-files")
        try:
            self.assertIsNone(guard.collect("origin/main"))
        finally:
            guard.git = orig


class TestCollectNoRenames(unittest.TestCase):
    # Paperclip Review round 1: git detects renames by default, so
    # `git mv test_x.py checks.py` reports R100 and a D-only filter misses
    # the deletion. Both diffs must pass --no-renames so the move shows as
    # D + A (mutmut_changed.sh already does this).
    def test_diffs_disable_rename_detection(self):
        seen = []

        def fake(args, allowed_returncodes=(0,)):
            seen.append(args)
            if args and args[0] == "merge-base":
                return "abc123\n"
            if args and args[0] == "ls-files":
                return ""
            return ""

        orig, guard.git = guard.git, fake
        try:
            guard.collect("origin/main")
        finally:
            guard.git = orig
        diff_calls = [a for a in seen if a and a[0] == "diff"]
        self.assertEqual(len(diff_calls), 2)
        for call in diff_calls:
            self.assertIn("--no-renames", call)

    def test_rename_to_non_test_name_flags_deleted_test(self):
        # With --no-renames the rename surfaces as a D row for the old
        # test path: that must produce test-deleted (plus assertion
        # handling stays on the old path via parse_diff).
        findings = guard.check([], [], "D\ttest_widget.py", [])
        self.assertTrue(any(f[0] == "test-deleted" and f[1] == "test_widget.py"
                            for f in findings))


class TestIgnoreBroadening(unittest.TestCase):
    # A glob added to the ignore file in the same branch would exempt its
    # own checks; exact paths stay silent (reviewed like code).
    def test_added_glob_pattern_is_flagged(self):
        findings = guard.check([(".constraintsignore", "*.py")], [], "", [])
        self.assertTrue(any(f[0] == "ignore-broadened" for f in findings))

    def test_added_exact_path_is_silent(self):
        findings = guard.check(
            [(".constraintsignore", "scripts/quality-gates/floor_guard.py")],
            [], "", [])
        self.assertEqual(
            [f for f in findings if f[0] == "ignore-broadened"], [])

    def test_added_comment_is_silent(self):
        findings = guard.check(
            [(".constraintsignore", "# why this exemption exists")], [], "", [])
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
