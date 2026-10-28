"""Author-owned path-collection acceptance tests; stdlib + local Git only.

Run: python3 -m unittest -v test_floor_guard_paths_acceptance
These tests exercise collect/check/main against disposable repositories, not
handwritten patch headers. They intentionally fail before the collector fix.
The Builder must consume this file unchanged; no policy changes are required.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


GUARD_PATH = Path(__file__).resolve().parent / "scripts/quality-gates/floor_guard.py"
# Literal backslash escape sequences must not be decoded twice. Git permits
# non-UTF-8 filenames on POSIX too; fsdecode/fsencode preserves their bytes.
PATHS = (
    "normal.py",
    "sp ace-é.py",
    "tab\tname.py",
    "line\nname.py",
    'double"name.py',
    "back\\slash.py",
    "carriage\rname.py",
    "literal\\t\\n\\042.py",
    os.fsdecode(b"raw-\xff.py"),
)
SUPPRESSION = "value = 1  # noqa"
TEST_BODY = "def test_value():\n    assert 1 == 1\n"


class GitFixture:
    def __init__(self, root):
        self.root = root
        self.git("init", "--quiet", "--template=")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True,
            text=True, check=True, timeout=15,
        )
        return result.stdout.strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "-m",
                 "fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        return self.git("rev-parse", "HEAD")


class TestFloorGuardPathsAcceptance(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory())
        # Drop inherited Git locations, global config, hooks, signing settings,
        # credentials and shell startup scripts. Every Git write stays local.
        self.enterContext(mock.patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": home,
            "XDG_CONFIG_HOME": home,
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }, clear=True))
        spec = importlib.util.spec_from_file_location("floor_guard", GUARD_PATH)
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)

    @contextlib.contextmanager
    def fixture(self, initial=None):
        with tempfile.TemporaryDirectory() as directory:
            repo = GitFixture(Path(directory))
            for path, content in (initial or {}).items():
                repo.write(path, content)
            repo.base = repo.commit()
            with mock.patch.object(self.guard, "REPO_ROOT", repo.root):
                yield repo

    def collected(self, repo):
        try:
            result = self.guard.collect(repo.base)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.fail(f"valid Git filename must be collected losslessly: {error!r}")
        self.assertIsNotNone(result, "measurable local diff must not become unknown")
        return result

    def findings(self, repo, ignore=()):
        result = self.guard.check(*self.collected(repo), ignore=list(ignore))
        return sorted((rule, path) for rule, path, _ in result)

    def main_result(self, repo, base=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = self.guard.main([
                    "--base", base if base is not None else repo.base,
                    "--ignore-file", str(repo.root / ".constraintsignore"),
                ])
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.fail(f"guard must return its 0/1/2 contract, not raise: {error!r}")
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_unknown(self, repo, base=None):
        code, stdout, stderr = self.main_result(repo, base)
        self.assertEqual(code, 2)
        self.assertNotIn("floor-guard: clean", stdout + stderr)
        self.assertNotIn("floor violation(s)", stdout + stderr)
        self.assertTrue(stderr.strip(), "unknown collection needs an explicit diagnostic")

    def test_added_code_is_not_silenced_by_path_framing(self):
        for path in PATHS:
            for state in ("committed", "staged", "unstaged", "untracked"):
                with self.subTest(path=path, state=state):
                    initial = {path: "value = 0\n"} if state == "unstaged" else {}
                    with self.fixture(initial) as repo:
                        repo.write(path, SUPPRESSION + "\n")
                        if state == "committed":
                            repo.commit()
                        elif state == "staged":
                            repo.git("add", "--all")
                        collected = self.collected(repo)
                        self.assertIn((path, SUPPRESSION), collected[0])
                        self.assertEqual(self.findings(repo), [("silenced-checker", path)])

    def test_clean_code_under_unusual_paths_is_clean(self):
        for path in PATHS:
            for tracked in (False, True):
                with self.subTest(path=path, tracked=tracked), self.fixture() as repo:
                    repo.write(path, "value = 1\n")
                    if tracked:
                        repo.commit()
                    self.assertIn((path, "value = 1"), self.collected(repo)[0])
                    self.assertEqual(self.findings(repo), [])
                    self.assertEqual(self.main_result(repo), (0, "floor-guard: clean\n", ""))

    def test_cli_uses_finding_exit_not_clean_or_unknown(self):
        for path in PATHS:
            with self.subTest(path=path), self.fixture() as repo:
                repo.write(path, SUPPRESSION + "\n")
                code, stdout, stderr = self.main_result(repo)
                self.assertEqual(code, 1)
                self.assertIn("[silenced-checker]", stderr)
                self.assertNotIn("floor-guard: clean", stdout + stderr)

    def test_removed_assertion_stays_on_old_test_path(self):
        for leaf in PATHS:
            path = "test_" + leaf
            with self.subTest(path=path), self.fixture({path: TEST_BODY}) as repo:
                repo.write(path, "def test_value():\n    pass\n")
                self.assertIn((path, "    assert 1 == 1"), self.collected(repo)[1])
                self.assertEqual(self.findings(repo), [("assertion-removed", path)])

    def test_deleted_test_keeps_both_existing_floor_rules_and_old_path(self):
        for leaf in PATHS:
            path = "test_" + leaf
            with self.subTest(path=path), self.fixture({path: TEST_BODY}) as repo:
                (repo.root / path).unlink()
                self.assertIn((path, "    assert 1 == 1"), self.collected(repo)[1])
                self.assertEqual(self.findings(repo), [
                    ("assertion-removed", path), ("test-deleted", path),
                ])

    def test_assertionless_test_deletion_still_reports_deleted_test(self):
        for leaf in PATHS:
            path = "test_" + leaf
            with self.subTest(path=path), self.fixture({path: "def test_value():\n    pass\n"}) as repo:
                (repo.root / path).unlink()
                repo.git("add", "--all")
                self.assertEqual(self.findings(repo), [("test-deleted", path)])

    def test_rename_keeps_old_attribution_even_when_destination_is_a_test(self):
        for leaf in PATHS:
            old = "test_" + leaf
            for new in ("checks.py", "test_relocated.py"):
                with self.subTest(old=old, new=new), self.fixture({old: TEST_BODY}) as repo:
                    (repo.root / old).rename(repo.root / new)
                    repo.git("add", "--all")
                    self.assertEqual(self.findings(repo), [
                        ("assertion-removed", old), ("test-deleted", old),
                    ])

    def test_test_directory_recognition_survives_quoted_parent_path(self):
        for leaf in PATHS:
            path = "tests/" + leaf.removesuffix(".py") + "/widget.py"
            with self.subTest(path=path), self.fixture({path: TEST_BODY}) as repo:
                repo.write(path, "def test_value():\n    pass\n")
                self.assertEqual(self.findings(repo), [("assertion-removed", path)])

    def test_same_count_assertion_edit_remains_clean(self):
        for leaf in PATHS:
            path = "test_" + leaf
            with self.subTest(path=path), self.fixture({path: TEST_BODY}) as repo:
                repo.write(path, 'def test_value():\n    assert 1 == 1, "message changed"\n')
                self.assertEqual(self.findings(repo), [])

    def test_deleted_non_test_does_not_acquire_test_rules(self):
        for path in PATHS:
            with self.subTest(path=path), self.fixture({path: "assert 1 == 1\n"}) as repo:
                (repo.root / path).unlink()
                self.assertEqual(self.findings(repo), [])

    def test_documented_suppression_names_remain_exempt(self):
        for leaf in PATHS:
            path = "docs/" + leaf.removesuffix(".py") + ".md"
            with self.subTest(path=path), self.fixture() as repo:
                repo.write(path, "Do not add `# noqa` comments to code.\n")
                self.assertEqual(self.findings(repo), [])

    def test_existing_ignore_patterns_apply_to_real_unquoted_path(self):
        for path in PATHS:
            with self.subTest(path=path), self.fixture() as repo:
                repo.write(path, SUPPRESSION + "\n")
                self.assertEqual(self.findings(repo, ignore=[path]), [])

    def test_adjacent_records_cannot_absorb_each_other(self):
        # One patch contains all names plus a clean neighbour: headers and
        # name-status records must not bleed into the next file's attribution.
        initial = {"test_" + leaf: TEST_BODY for leaf in PATHS}
        with self.fixture(initial) as repo:
            for path in initial:
                (repo.root / path).unlink()
            for leaf in PATHS:
                repo.write("added/" + leaf, SUPPRESSION + "\n")
            repo.write("clean.py", "value = 1\n")
            repo.git("add", "--all")
            expected = [("silenced-checker", "added/" + leaf) for leaf in PATHS]
            expected += [(rule, path) for path in initial
                         for rule in ("assertion-removed", "test-deleted")]
            self.assertEqual(self.findings(repo), sorted(expected))

    def test_existing_threshold_direction_is_unchanged_under_quoted_directory(self):
        for prefix in ("", 'tab\tline\nquote"back\\/'):
            path = prefix + "CONSTRAINTS.md"
            for new, expected in ((70, [("threshold-lowered", "CONSTRAINTS.md")]), (90, [])):
                with self.subTest(path=path, new=new), self.fixture({
                    path: "| Coverage | Changed lines >= 80% |\n",
                }) as repo:
                    repo.write(path, f"| Coverage | Changed lines >= {new}% |\n")
                    self.assertEqual(self.findings(repo), expected)

    def test_invalid_base_is_unknown_not_clean(self):
        with self.fixture() as repo:
            self.assertIsNone(self.guard.collect("refs/heads/nonexistent-fixture-base"))
            self.assert_unknown(repo, "refs/heads/nonexistent-fixture-base")

    def test_real_diff_command_failure_is_unknown_not_clean(self):
        with self.fixture({"normal.py": "value = 0\n"}) as repo:
            repo.write("normal.py", SUPPRESSION + "\n")
            repo.git("config", "diff.external", str(repo.root / "nonexistent-diff-command"))
            self.assertIsNone(self.guard.collect(repo.base))
            self.assert_unknown(repo)

    def test_each_collection_command_failure_preserves_exit_two(self):
        # Inject only the OS/subprocess failure boundary. All other Git
        # commands run normally; assertions concern collect/main outcomes.
        for stage in ("patch", "deletions", "untracked-list", "untracked-patch"):
            for failure in ("exit", "timeout"):
                with self.subTest(stage=stage, failure=failure), self.fixture({
                    "normal.py": "value = 0\n",
                }) as repo:
                    repo.write("normal.py", SUPPRESSION + "\n")
                    repo.write("untracked.py", SUPPRESSION + "\n")
                    real_run = subprocess.run

                    def failing_run(command, *args, **kwargs):
                        if command[0] == "git":
                            is_diff = "diff" in command
                            is_no_index = "--no-index" in command
                            is_names = "--name-status" in command
                            selected = {
                                "patch": is_diff and not is_no_index and not is_names,
                                "deletions": is_diff and is_names,
                                "untracked-list": "ls-files" in command,
                                "untracked-patch": is_diff and is_no_index,
                            }[stage]
                            if selected:
                                if failure == "timeout":
                                    raise subprocess.TimeoutExpired(command, 120)
                                return subprocess.CompletedProcess(command, 128, "", "fixture command failure")
                        return real_run(command, *args, **kwargs)

                    with mock.patch.object(self.guard.subprocess, "run", side_effect=failing_run):
                        self.assertIsNone(self.guard.collect(repo.base))
                        self.assert_unknown(repo)


if __name__ == "__main__":
    unittest.main()
