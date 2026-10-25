#!/usr/bin/env python3
"""Offline controls for the portable docs accompaniment gate."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts/quality-gates/docs_drift.py"
SPEC = importlib.util.spec_from_file_location("docs_drift", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class DocsDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docs-drift-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.policy = {"version": 1, "rules": [{"id": "api", "sources": ["src/*"], "docs": ["docs/*"]}]}
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.put("src/api.py", "before\n")
        self.put("docs/api.md", "before\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, text=True).strip()

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "test: fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        return self.git("rev-parse", "HEAD")

    def check(self, expected, *, base=None, head=None):
        result = subprocess.run(["python3", str(SCRIPT), "--repo", str(self.root),
                                 "--base=" + (base or self.base), "--head=" + (head or self.commit())],
                                capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def change_source(self):
        self.put("src/api.py", "after\n")

    def test_empty_diff_and_unrelated_change(self):
        self.assertEqual(self.check(0, head=self.base)["changed_paths"], 0)
        self.put("unrelated.txt", "change\n")
        self.assertEqual(self.check(0)["checked_rules"], 0)

    def test_source_without_docs_fails(self):
        self.change_source()
        report = self.check(1)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["findings"][0]["paths"], ["src/api.py"])
        self.assertEqual(report["findings"][0]["code"], "docs_required")

    def test_modified_or_added_docs_pass(self):
        self.change_source()
        self.put("docs/api.md", "after\n")
        self.assertEqual(self.check(0)["checked_rules"], 1)
        self.git("reset", "--hard", self.base)
        self.change_source()
        self.put("docs/new.md", "new docs\n")
        self.check(0)

    def test_deleted_docs_do_not_satisfy(self):
        self.change_source()
        (self.root / "docs/api.md").unlink()
        self.check(1)

    def test_symlink_and_mode_only_docs_do_not_satisfy(self):
        self.change_source()
        os.symlink("api.md", self.root / "docs/link.md")
        self.check(1)
        self.git("reset", "--hard", self.base)
        self.change_source()
        os.chmod(self.root / "docs/api.md", 0o755)
        self.check(1)

    def test_source_deletion_requires_docs(self):
        (self.root / "src/api.py").unlink()
        self.check(1)

    def test_source_rename_retains_old_path(self):
        self.git("mv", "src/api.py", "outside.py")
        self.assertEqual(self.check(1)["findings"][0]["paths"], ["src/api.py"])

    def test_docs_rename_outside_mapping_does_not_satisfy(self):
        self.change_source()
        self.git("mv", "docs/api.md", "outside.md")
        self.check(1)

    def test_spaces_newlines_unicode_and_option_like_paths(self):
        paths = ["src/a b.py", "src/a\nb.py", "src/☃.py", "src/--flag.py"]
        for name in paths:
            self.put(name, "new\n")
        self.assertEqual(self.check(1)["findings"][0]["paths"], sorted(paths))
        self.put("docs/a\nb.md", "new docs\n")
        self.check(0)

    def test_head_policy_deletion_or_weakening_cannot_bypass(self):
        self.change_source()
        (self.root / ".docs-drift.json").unlink()
        self.check(1)
        self.put(".docs-drift.json", json.dumps({"version": 1, "rules": []}))
        self.check(1)

    def test_new_policy_only_activates_after_merge(self):
        self.git("rm", ".docs-drift.json")
        unconfigured = self.commit()
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.change_source()
        report = self.check(0, base=unconfigured)
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual(report["checked_rules"], 0)

    def test_malformed_base_policy_is_unmeasurable(self):
        self.put(".docs-drift.json", '{"version":1,"rules":[]}' )
        self.base = self.commit()
        self.assertEqual(self.check(2)["code"], "invalid_policy")

    def test_invalid_base_shape(self):
        for value in [[], {"version": True, "rules": self.policy["rules"]},
                      {"version": 1, "rules": [{"id": "x", "sources": ["../*"], "docs": ["docs/*"]}]}]:
            with self.subTest(value=value):
                self.put(".docs-drift.json", json.dumps(value))
                self.base = self.commit()
                self.check(2)

    def test_bad_sha_missing_object_and_disjoint_history(self):
        self.assertEqual(self.check(2, head="--help")["code"], "invalid_sha")
        self.assertEqual(self.check(2, head="0" * 40)["code"], "git_failed")
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        self.put("new", "other history\n")
        self.assertEqual(self.check(2)["code"], "git_failed")

    def test_target_policy_uses_supplied_base_not_merge_base(self):
        self.change_source()
        head = self.commit()
        self.git("checkout", "--detach", self.base)
        self.policy["rules"][0]["docs"] = ["manual/*"]
        self.put(".docs-drift.json", json.dumps(self.policy))
        target = self.commit()
        report = self.check(1, base=target, head=head)
        self.assertEqual(report["policy_sha"], target)
        self.assertEqual(report["merge_base_sha"], self.base)
        self.assertEqual(report["findings"][0]["docs"], ["manual/*"])

    def test_one_rule_cannot_be_satisfied_by_another_rules_docs(self):
        self.policy["rules"].append({"id": "other", "sources": ["lib/*"], "docs": ["manual/*"]})
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.base = self.commit()
        self.change_source()
        self.put("lib/other.py", "change\n")
        self.put("docs/api.md", "after\n")
        self.assertEqual([f["rule"] for f in self.check(1)["findings"]], ["other"])

    def test_overlapping_source_and_docs_cannot_satisfy_itself(self):
        self.policy["rules"][0]["docs"] = ["src/*"]
        self.put(".docs-drift.json", json.dumps(self.policy))
        self.base = self.commit()
        self.change_source()
        self.check(1)

    def test_git_timeout_is_unmeasurable(self):
        with patch.object(GATE.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)):
            with self.assertRaises(GATE.Unmeasurable) as result:
                GATE.check(self.root, self.base, self.base)
        self.assertEqual(result.exception.code, "git_timeout")
        self.assertTrue(result.exception.retryable)

    def test_git_environment_cannot_redirect_repository_or_inject_config(self):
        self.change_source()
        head = self.commit()
        with patch.dict(os.environ, {"GIT_DIR": "/missing/repo.git", "GIT_WORK_TREE": "/missing",
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare",
                                     "GIT_CONFIG_VALUE_0": "true"}):
            report = GATE.check(self.root, self.base, head)
        self.assertEqual(report["status"], "finding")
        self.assertEqual(report["checked_rules"], 1)


if __name__ == "__main__":
    unittest.main()
