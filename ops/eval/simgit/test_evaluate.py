import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
