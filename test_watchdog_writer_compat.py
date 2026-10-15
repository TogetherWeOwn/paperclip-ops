#!/usr/bin/env python3
"""Offline contract for the private writer/detector compatibility gate."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
REQUIRED = {"create_pause_hold", "assign_blocker_owner", "restore_blocked", "operator_decision"}


class WriterCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate = (ROOT / "verification/platform-watchdog-gate.sh").read_text()
        block = gate.split("python3 - <<'WRITER_COMPAT'", 1)[1]
        cls.check = block.partition("\n")[2].split("\nWRITER_COMPAT", 1)[0]

    def run_check(self, allowed=None, diagnostics=None, compatible=None, stale=False):
        allowed = REQUIRED | {"reset_agent_error"} if allowed is None else allowed
        diagnostics = {"clear_pin"} if diagnostics is None else diagnostics
        compatible = REQUIRED | {"none", "clear_pin"} if compatible is None else compatible
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "watchdog").mkdir()
            (root / "recovery_writer.py").write_text(
                f"ALLOWED_MUTATIONS = {allowed!r}\nDIAGNOSTIC_MUTATIONS = {diagnostics!r}\n")
            (root / "watchdog/detectors.py").write_text(
                f"WRITER_COMPATIBLE_MUTATIONS = frozenset({compatible!r})\n")
            if stale:
                # A real stale origin/main must not hide a bad candidate.
                git_env = {"PATH": os.defpath, "HOME": directory,
                           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
                subprocess.run(["/usr/bin/git", "init", "-q", directory], env=git_env, check=True)
                subprocess.run(["/usr/bin/git", "add", "recovery_writer.py"], cwd=root,
                               env=git_env, check=True)
                subprocess.run(["/usr/bin/git", "-c", "user.name=Fixture", "-c",
                                "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                                "commit", "-qm", "fixture"], cwd=root, env=git_env, check=True)
                subprocess.run(["/usr/bin/git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                               cwd=root, env=git_env, check=True)
                (root / "recovery_writer.py").write_text(
                    f"ALLOWED_MUTATIONS = {REQUIRED - {'restore_blocked'}!r}\n"
                    f"DIAGNOSTIC_MUTATIONS = {diagnostics!r}\n")
            return subprocess.run([sys.executable, "-B", "-c", self.check], cwd=root,
                                  env={**os.environ, "GITHUB_ACTIONS": "true"},
                                  text=True, capture_output=True)

    def test_real_candidate_and_diagnostic_compatibility_pass(self):
        result = subprocess.run([sys.executable, "-B", "-c", self.check], cwd=ROOT,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_check().returncode, 0)

    def test_pin_clearing_authority_is_not_compatibility(self):
        result = self.run_check(allowed=REQUIRED | {"clear_pin"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("diagnostic-only", result.stderr)

    def test_missing_required_or_diagnostic_mutation_refuses(self):
        for kwargs in ({"allowed": REQUIRED - {"restore_blocked"}},
                       {"diagnostics": {"other"}},
                       {"compatible": REQUIRED | {"none"}}):
            with self.subTest(kwargs=kwargs):
                self.assertNotEqual(self.run_check(**kwargs).returncode, 0)

    def test_unknown_detector_mutation_refuses(self):
        result = self.run_check(compatible=REQUIRED | {"none", "clear_pin", "resume_issue"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported", result.stderr)

    def test_stale_origin_main_cannot_hide_candidate_drift(self):
        result = self.run_check(stale=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required mutation", result.stderr)


if __name__ == "__main__":
    unittest.main()
