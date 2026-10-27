"""Execute the bounded runbook recipe ONLY on dummy unprivileged files.

CI command (Builder wires it): python3 -B tests/test_op_request_swap.py
Draft verification: add --runbook /run-scratch/candidate.md. Extracts the single
op-request-swap block; no fallback copy can hide a missing/stale product block.
Only ACTION/SOURCE/TARGET/BACKUP/OLD/NEW/PATH and EUID identity are rewritten.
All failure injection stays at command boundaries, never in the recipe logic.
"""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unittest

import op_request_harness as h


RUNBOOK = h.SOURCE.with_name("README.md")
OLD_BYTES = b"#!/bin/bash\nprintf 'baseline dummy\\n'\n"
NEW_BYTES = b"#!/bin/bash\nprintf 'candidate dummy\\n'\n"
START = b"<!-- op-request-swap:start -->"
END = b"<!-- op-request-swap:end -->"


def extract_recipe(path):
    content = h.regular_bytes(path)
    if content.count(START) != 1 or content.count(END) != 1:
        raise h.Refused("runbook needs exactly one marked op-request-swap block")
    between = content.split(START, 1)[1].split(END, 1)[0]
    match = re.fullmatch(rb"\s*```bash\n(.*?)\n```\s*", between, re.S)
    if not match:
        raise h.Refused("marked block must contain exactly one Bash fence")
    return match[1] + b"\n"


def snapshot(path):
    value = path.stat()
    return (path.read_bytes(), value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode), value.st_ino)


class AtomicSwapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recipe = extract_recipe(RUNBOOK)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(h.sandbox())
        h.install_fakes(self.root, ("id", "stat", "cp", "cat", "mktemp", "mv", "ln", "rm"))
        (self.root / "target-dir").mkdir(mode=0o700)
        (self.root / "backup-dir").mkdir(mode=0o700)
        self.target = self.root / "target-dir/op-request"
        self.backup = self.root / "backup-dir/baseline"
        self.source = self.root / "candidate"
        self.source.write_bytes(NEW_BYTES)
        self.target.write_bytes(OLD_BYTES)
        self.target.chmod(0o751)
        self.sequence = 0

    def seed_backup(self, content=OLD_BYTES):
        self.backup.write_bytes(content)
        self.backup.chmod(0o751)

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_recipe(self, action="install", fault=""):
        # EUID cannot be assigned in Bash; replace its ONE identity observation,
        # not the root predicate. The test uid/gid remain real and unprivileged.
        self.assertNotEqual(os.geteuid(), 0)
        if self.recipe.count(b"$EUID") != 1:
            raise h.Refused("expected exactly one root-identity observation")
        controlled = self.recipe.replace(b"$EUID", b"$(id -u)")
        replacements = {
            "ACTION": h.shell_literal(action), "SOURCE": h.shell_literal(self.source),
            "TARGET": h.shell_literal(self.target), "BACKUP": h.shell_literal(self.backup),
            "OLD": h.digest(OLD_BYTES), "NEW": h.digest(NEW_BYTES),
            "export PATH": h.shell_literal(f"{self.root}/bin:/usr/bin:/bin"),
        }
        runner = h.rewrite(controlled, replacements)
        h.verify_rewrite(controlled, runner, replacements)
        self.sequence += 1
        path = self.root / f"recipe-{self.sequence}.sh"
        h.write_new(path, runner)
        h.verify_rewrite(controlled, h.regular_bytes(path), replacements)
        return subprocess.run(["/bin/bash", str(path)],
                              env=h.clean_env(self.root, OPREQ_MODE="swap", OPREQ_FAULT=fault),
                              capture_output=True, text=True, timeout=20, check=False)

    def assert_retained(self, before_target, before_backup):
        self.assertEqual(snapshot(self.target), before_target, "target bytes/uid/gid/mode/inode changed before a successful rename")
        if before_backup is None:
            self.assertFalse(self.backup.exists(), "failed backup staging published a backup")
        else:
            self.assertEqual(snapshot(self.backup), before_backup, "baseline backup was changed/replaced")

    def assert_refused(self, result):
        self.assertNotEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertNotIn("Verified ", result.stdout)

    def assert_no_staging(self):
        for directory in (self.target.parent, self.backup.parent):
            self.assertFalse(any(p.name.startswith(".op-request.") for p in directory.iterdir()), "private staging was not cleaned up")

    def test_install_and_inverse_preserve_bytes_uid_gid_mode_and_baseline_backup(self):
        before = snapshot(self.target)
        result = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = snapshot(self.target)
        self.assertEqual(installed[:4], (NEW_BYTES, *before[1:4]))
        self.assertNotEqual(installed[4], before[4], "install did not use inode replacement")
        saved = snapshot(self.backup)
        self.assertEqual(saved[:4], before[:4])
        self.source.unlink()  # inverse uses baseline backup, not the reviewed clone
        result = self.run_recipe("rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.target)[:4], before[:4])
        self.assertNotEqual(snapshot(self.target)[4], installed[4])
        self.assertEqual(snapshot(self.backup), saved)
        self.assertEqual(sum(event[0] == "mv" for event in self.events()), 2)
        self.assert_no_staging()

    def test_repeat_install_refuses_and_cannot_replace_baseline_backup(self):
        result = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stderr)
        before_target, before_backup = snapshot(self.target), snapshot(self.backup)
        result = self.run_recipe()
        self.assert_refused(result)
        self.assert_retained(before_target, before_backup)
        self.assertEqual(sum(event[0] == "mv" for event in self.events()), 1)

    def test_current_or_source_hash_mismatch_refuses_before_replacement(self):
        for mismatch in ("current", "source"):
            with self.subTest(mismatch=mismatch):
                self.target.write_bytes(OLD_BYTES if mismatch == "source" else b"unexpected current bytes")
                self.source.write_bytes(NEW_BYTES if mismatch == "current" else b"wrong candidate")
                self.seed_backup()
                before_target, before_backup = snapshot(self.target), snapshot(self.backup)
                result = self.run_recipe()
                self.assert_refused(result)
                self.assert_retained(before_target, before_backup)
        self.assertFalse(any(event[0] == "mv" for event in self.events()))

    def test_missing_or_corrupt_rollback_backup_refuses_without_replacement(self):
        self.target.write_bytes(NEW_BYTES)
        before_target = snapshot(self.target)
        result = self.run_recipe("rollback")
        self.assert_refused(result)
        self.assert_retained(before_target, None)
        self.seed_backup(b"corrupted baseline")
        before_backup = snapshot(self.backup)
        result = self.run_recipe("rollback")
        self.assert_refused(result)
        self.assert_retained(before_target, before_backup)
        self.assertFalse(any(event[0] == "mv" for event in self.events()))

    def test_corrupt_writable_or_symlink_install_backup_is_not_overwritten(self):
        before_target = snapshot(self.target)
        for flavor in ("corrupt", "writable", "symlink"):
            with self.subTest(flavor=flavor):
                self.seed_backup(b"corrupted" if flavor == "corrupt" else OLD_BYTES)
                if flavor == "writable":
                    self.backup.chmod(0o777)
                if flavor == "symlink":
                    other = self.root / "saved-baseline"
                    self.backup.rename(other)
                    self.backup.symlink_to(other)
                before_backup = snapshot(self.backup)
                result = self.run_recipe()
                self.assert_refused(result)
                self.assert_retained(before_target, before_backup)
                if flavor == "symlink":
                    self.assertTrue(self.backup.is_symlink())
                self.backup.unlink()
        self.assertFalse(any(event[0] == "mv" for event in self.events()))

    def test_failed_partial_and_corrupt_swap_staging_leave_target_and_backup_unchanged(self):
        self.seed_backup()
        before_target, before_backup = snapshot(self.target), snapshot(self.backup)
        faults = ("cp-swap", "cp-partial-swap", "cp-corrupt-swap", "cat-partial", "cat-corrupt",
                  "mktemp-swap", "mktemp-empty-swap", "mv")
        for fault in faults:
            with self.subTest(fault=fault):
                result = self.run_recipe(fault=fault)
                self.assert_refused(result)
                self.assert_retained(before_target, before_backup)
                self.assert_no_staging()

    def test_rollback_staging_and_rename_failures_retain_fixed_target_and_baseline(self):
        self.target.write_bytes(NEW_BYTES)
        self.seed_backup()
        before_target, before_backup = snapshot(self.target), snapshot(self.backup)
        for fault in ("cp-swap", "cp-partial-swap", "cat-partial", "cat-corrupt", "mktemp-swap", "mv"):
            with self.subTest(fault=fault):
                self.assert_refused(self.run_recipe("rollback", fault))
                self.assert_retained(before_target, before_backup)
                self.assert_no_staging()

    def test_failed_backup_staging_never_changes_target_or_publishes_partial_backup(self):
        before = snapshot(self.target)
        for fault in ("cp-backup", "cp-partial-backup", "cp-corrupt-backup", "mktemp-backup", "mktemp-empty-backup", "ln"):
            with self.subTest(fault=fault):
                self.assert_refused(self.run_recipe(fault=fault))
                self.assert_retained(before, None)
                self.assert_no_staging()
        # Controls: the same no-backup initial state can create a good backup.
        result = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.backup)[:4], before[:4])

    def test_failure_after_first_backup_publication_retains_that_baseline(self):
        before = snapshot(self.target)
        self.assert_refused(self.run_recipe(fault="cat-partial"))
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(snapshot(self.backup)[:4], before[:4])
        self.assert_no_staging()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runbook", type=Path, default=RUNBOOK)
    args = parser.parse_args()
    RUNBOOK = args.runbook
    unittest.main(argv=[sys.argv[0], "-v"])
