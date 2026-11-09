import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("simgit_evaluation", Path(__file__).with_name("evaluate.py"))
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


class ProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.root / "tree"
        self.record = {"path": str(self.path), "worktree": str(self.path),
                       "cleanup_token": str(self.path), "mode": "overlay", "branch": "test",
                       "base": "a" * 40, "ephemeral": True}
        self.doctor = {"identity": "simgit", "version": "0.5.0", "repository": str(self.source),
                       "repository_details": {"common_git_owner": str(self.source),
                                              "common_git_dir": str(self.source / ".git")},
                       "git_worktree_supported": True, "populate_mode": "overlay"}

    def validate(self, record):
        return evaluation.validate_allocation(record, self.source, self.path, "test", "a" * 40)

    def test_accepts_only_expected_allocation(self):
        self.assertEqual(self.validate(self.record), str(self.path))

    def test_rejects_relative_inside_mismatched_and_noncanonical_paths(self):
        for raw in ("tree", str(self.source), str(self.source / "tree"),
                    str(self.root / "other"), str(self.root / "x" / ".." / "tree"), None):
            for field in ("path", "worktree", "cleanup_token"):
                with self.subTest(raw=raw, field=field), self.assertRaises(ValueError):
                    self.validate({**self.record, field: raw})

    def test_rejects_symlink_into_source(self):
        link = self.root / "link"
        link.symlink_to(self.source, target_is_directory=True)
        raw = str(link / "tree")
        record = {**self.record, **dict.fromkeys(("path", "worktree", "cleanup_token"), raw)}
        with self.assertRaises(ValueError):
            self.validate(record)

    def test_rejects_wrong_mode_branch_base_or_persistence(self):
        for field, value in (("mode", "git-checkout"), ("mode", "cow-clone"),
                             ("branch", None), ("branch", "other"), ("base", "b" * 40),
                             ("ephemeral", False), ("ephemeral", 1)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate({**self.record, field: value})

    def test_rejects_malformed_shapes(self):
        for value in (None, [], "{}", {}, {"path": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(value)

    def test_identity_is_not_inferred_from_executable_name(self):
        evaluation.validate_doctor(self.doctor, self.source)
        for field, value in (("identity", "sg"), ("identity", None), ("version", "0.4.0"),
                             ("populate_mode", "git-checkout"), ("repository", None),
                             ("git_worktree_supported", None)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                evaluation.validate_doctor({**self.doctor, field: value}, self.source)
        wrong = copy.deepcopy(self.doctor)
        wrong["repository_details"]["common_git_owner"] = str(self.root / "other")
        with self.assertRaises(ValueError):
            evaluation.validate_doctor(wrong, self.source)


class AllocationTests(unittest.TestCase):
    def test_deduplicates_hardlinked_inodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            original.write_bytes(b"x" * 8192)
            os.link(original, root / "alias")
            expected = (root.stat().st_blocks + original.stat().st_blocks) * 512
            self.assertEqual(evaluation.allocated(root)["allocated_bytes"], expected)
            self.assertEqual(evaluation.allocated(root)["unique_inodes"], 2)

    def test_excludes_mounted_view_and_separates_generated_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = root / "view"
            view.mkdir()
            (view / "double-counted").write_bytes(b"x" * 8192)
            generated = root / "generated"
            generated.mkdir()
            (generated / "output").write_bytes(b"x" * 8192)
            result = evaluation.allocated(root, [{"path": str(view)}])
            expected = sum(p.stat().st_blocks * 512 for p in (root, generated, generated / "output"))
            self.assertEqual(result["allocated_bytes"], expected)
            self.assertEqual(result["categories"]["generated_output"],
                             (generated.stat().st_blocks + (generated / "output").stat().st_blocks) * 512)


class CorrectionTests(unittest.TestCase):
    def test_counts_directory_symlink_without_following_target(self):
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory)
            target = outer / ("long-directory-" + "x" * 150)
            target.mkdir()
            (target / "not-in-boundary").write_bytes(b"x" * 8192)
            root = outer / "measured"
            root.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            result = evaluation.allocated(root)
            self.assertEqual(result["unique_inodes"], 2)
            self.assertEqual(result["allocated_bytes"], (root.stat().st_blocks + link.lstat().st_blocks) * 512)

    def test_crash_or_unrelated_error_is_not_a_native_refusal(self):
        reason = "worktree has uncommitted changes"
        for code, stdout, stderr in ((-11, "", reason), (1, "", "unrelated error"),
                                     (0, "", reason), (1, "{}", reason)):
            self.assertFalse(evaluation.expected_refusal(subprocess.CompletedProcess([], code, stdout, stderr), reason))
        self.assertTrue(evaluation.expected_refusal(subprocess.CompletedProcess([], 1, "", reason), reason))

    def test_timeout_keeps_partial_command_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = evaluation.Evaluation(root / "never-executed", root / "data", root)
            with mock.patch.object(evaluation.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                    ["never-executed"], 30, output=b"partial", stderr=b"diagnostic")):
                with self.assertRaises(subprocess.TimeoutExpired):
                    instance.command(root, "never-executed")
            entry = instance.result["commands"][-1]
            self.assertEqual(entry["argv"], ["never-executed"])
            self.assertEqual(entry["outcome"], "TIMEOUT")
            self.assertEqual(entry["stdout"], "partial")
            self.assertEqual(entry["stderr"], "diagnostic")
            self.assertIsNone(entry["exit"])

    def test_group_cleanup_is_attempted_even_after_launcher_exit(self):
        with mock.patch.object(evaluation.os, "killpg") as kill:
            evaluation.stop_process_group(12345)
            kill.assert_called_once_with(12345, evaluation.signal.SIGKILL)
        with mock.patch.object(evaluation.os, "killpg", side_effect=ProcessLookupError):
            evaluation.stop_process_group(12345)

    def test_soft_timeout_raises_a_catchable_interruption(self):
        with self.assertRaises(InterruptedError):
            evaluation.termination_requested(evaluation.signal.SIGTERM, None)

    def test_scenario_failure_enters_native_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = evaluation.Evaluation(root / "never-executed", root / "data", root)
            source = root / "source"
            with mock.patch.object(instance, "source", return_value=(source, "a" * 40, {})), \
                 mock.patch.object(instance, "snapshot", return_value={}), \
                 mock.patch.object(instance, "create", side_effect=InterruptedError("synthetic failure")), \
                 mock.patch.object(instance, "cleanup") as cleanup:
                with self.assertRaises(InterruptedError):
                    instance.scenario("git", "cold", 1)
                cleanup.assert_called_once_with("git", source, root / "data" / "git-cold-n1")

    def test_workflow_lifetime_budget_and_no_reruns(self):
        workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "simgit-evaluation.yml"
        text = workflow.read_text()
        self.assertIn("github.run_attempt == 1 && github.run_number <= 2", text)
        self.assertIn("ci-policy: non-required --", text)


if __name__ == "__main__":
    unittest.main()
