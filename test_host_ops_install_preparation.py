#!/usr/bin/env python3
"""Real temporary-filesystem journal promotion; all service calls are mocked."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from test_host_ops_install_transaction import (ACTIVE, INACTIVE, REF, ROOT,
                                              build_fixture, state, txn)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-ops-prepare-")
        self.addCleanup(self.tmp.cleanup)
        fx = build_fixture(self.tmp.name)
        self.root, self.sources = fx["root"], fx["sources"]
        self.owner, self.gid = fx["owner"], fx["gid"]
        self.container = fx["directory"].parent
        # build_fixture makes this empty placeholder. Production must create its
        # own journals; no pre-existing install-* is exempted from the scanner.
        fx["directory"].rmdir()
        self.container.chmod(0o700)
        self.source_dir = self.root / "source"
        for name in ("install_state.py", "install_transaction.py",
                     "spool_provisioning.py"):
            shutil.copyfile(ROOT / "host-ops" / name, self.source_dir / name)
        self.probe = state.InstallState(self.container, self.root, self.owner)
        self.before = {path: self.probe.target(path).read_bytes() for path in state.FILES.values()}
        observer = patch.object(state, "service_state", return_value=dict(ACTIVE))
        controller = patch.object(state, "systemctl", return_value=(0, ""))
        self.service, self.control = observer.start(), controller.start()
        self.addCleanup(observer.stop)
        self.addCleanup(controller.stop)

    def prepare(self):
        return txn.prepare_journal(self.container, self.source_dir, self.sources, REF,
                                   self.owner, self.gid, self.gid, self.root, self.owner)

    def untouched(self):
        self.control.assert_not_called()
        for absolute, data in self.before.items():
            self.assertEqual(self.probe.target(absolute).read_bytes(), data)

    def scan(self):
        txn.require_no_unfinished_install(self.container, self.root, self.owner)

    def test_positive_complete_journal_is_visible_before_any_mutation(self):
        journal = self.prepare()
        self.assertRegex(journal.directory.name, r"^install-[0-9a-f]{24}$")
        self.assertEqual(journal.load()["phase"], "prepared")
        journal.verify_files(journal.load())
        self.assertEqual({p.name for p in journal.directory.iterdir()},
                         {"state.json", "install_state.py", "install_transaction.py",
                          "spool_provisioning.py"})
        for name in ("install_state.py", "install_transaction.py",
                     "spool_provisioning.py"):
            path = journal.directory / name
            self.assertEqual(path.read_bytes(), (self.source_dir / name).read_bytes())
            self.assertEqual((path.stat().st_uid, stat.S_IMODE(path.stat().st_mode)), (self.owner, 0o600))
        # The staged helpers must satisfy the transaction's own top-level
        # imports: the rollback entrypoint re-executes install_transaction.py
        # from this journal directory, so a staged set missing a sibling
        # import would fail with ModuleNotFoundError after live files moved.
        helpers = {name: (journal.directory / name) for name in
                   ("install_state.py", "spool_provisioning.py")}
        source = (journal.directory / "install_transaction.py").read_text()
        for helper, path in helpers.items():
            self.assertIn(f"import {helper[:-3]}", source)
            self.assertTrue(path.is_file() and not path.is_symlink())
        self.assertFalse(list(self.container.glob("prepare-*")))
        self.untouched()
        with self.assertRaisesRegex(state.Refusal, "unfinished installation"):
            self.scan()

    def test_prepare_then_apply_uses_the_published_snapshot(self):
        journal = self.prepare()
        prepared = journal.load()
        self.service.side_effect = lambda require_stable=True: dict(ACTIVE if require_stable else INACTIVE)
        with patch.object(state.InstallState, "snapshot", side_effect=AssertionError("must not recapture")):
            txn.apply_prepared(journal, self.sources, REF, self.owner, self.gid, self.gid)
        record = journal.load()
        self.assertEqual(record["phase"], "installed")
        self.assertEqual(record["files"], prepared["files"])
        self.assertEqual(record["keyMetadata"], prepared["keyMetadata"])
        self.control.assert_any_call("stop", state.UNIT)
        self.scan()

    def test_snapshot_failure_leaves_only_non_mutating_preparation_and_retry_works(self):
        with patch.object(state.InstallState, "snapshot", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.prepare()
        orphan, = self.container.glob("prepare-*")
        self.assertFalse(list(self.container.glob("install-*")))
        self.assertFalse((orphan / "state.json").exists())
        self.scan()
        self.untouched()
        journal = self.prepare()
        self.assertTrue(orphan.is_dir())  # No automatic cleanup or evidence loss.
        self.assertEqual(journal.load()["phase"], "prepared")

    def test_each_setup_fsync_failure_stays_pre_mutation(self):
        # Discover the positive-control sync count, then fail each boundary in a
        # fresh fixture. Includes directory creation, state save, and post-rename.
        real_sync = state.InstallState.sync
        seen = []
        with patch.object(state.InstallState, "sync", side_effect=lambda p: (seen.append(p), real_sync(p))[-1]):
            journal = self.prepare()
        preparation = self.container / journal.directory.name.replace("install-", "prepare-", 1)
        self.assertEqual(seen, [self.container, preparation, preparation, self.container])
        # This positive journal is retained; independent fixture roots below.
        for boundary in range(len(seen)):
            with self.subTest(boundary=boundary):
                case = PreparationTests(methodName="runTest")
                case.setUp()
                try:
                    count = 0
                    def fail_sync(path):
                        nonlocal count
                        index = count
                        count += 1
                        if index == boundary:
                            raise OSError("synthetic fsync failure")
                        return real_sync(path)
                    with patch.object(state.InstallState, "sync", side_effect=fail_sync):
                        with self.assertRaises(OSError):
                            case.prepare()
                    case.untouched()
                    published = list(case.container.glob("install-*"))
                    if published:
                        self.assertEqual(len(published), 1)
                        self.assertEqual(state.InstallState(published[0], case.root, case.owner).load()["phase"], "prepared")
                        with self.assertRaises(state.Refusal):
                            case.scan()
                    else:
                        case.scan()
                finally:
                    case.doCleanups()

    def test_helper_partial_write_retained_without_install_visibility(self):
        real_fsync = os.fsync
        calls = 0
        def fail_second_file(fd):
            nonlocal calls
            if stat.S_ISREG(os.fstat(fd).st_mode):
                calls += 1
                if calls == 2:
                    raise OSError("synthetic helper flush failure")
            return real_fsync(fd)
        with patch.object(os, "fsync", side_effect=fail_second_file):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(calls, 2)
        self.assertFalse(list(self.container.glob("install-*")))
        self.assertEqual(len(list(self.container.glob("prepare-*"))), 1)
        self.scan()
        self.untouched()

    def test_helper_corruption_before_promotion_refuses(self):
        real_snapshot = state.InstallState.snapshot
        def corrupt(journal, *args):
            record = real_snapshot(journal, *args)
            (journal.directory / "install_state.py").write_bytes(b"not reviewed code")
            return record
        with patch.object(state.InstallState, "snapshot", corrupt):
            with self.assertRaisesRegex(state.Refusal, "helper differs"):
                self.prepare()
        self.assertFalse(list(self.container.glob("install-*")))
        self.untouched()

    def test_missing_or_symlink_helper_refuses_before_journal_creation(self):
        path = self.source_dir / "install_transaction.py"
        path.unlink()
        with self.assertRaises(OSError):
            self.prepare()
        path.symlink_to(self.source_dir / "install_state.py")
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(list(self.container.iterdir()), [])
        self.untouched()

    def test_missing_provisioning_helper_refuses_before_journal_creation(self):
        # The rollback entrypoint re-executes the transaction module from the
        # journal; without its staged provisioning import the journal would
        # publish a rollback that fails with ModuleNotFoundError. Refuse
        # before any journal exists, like the other helpers.
        path = self.source_dir / "spool_provisioning.py"
        path.unlink()
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(list(self.container.iterdir()), [])
        self.untouched()

    def test_no_replace_preserves_empty_and_nonempty_destination(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                src = self.root / ("prepare-test" + str(populated))
                dst = self.root / ("install-test" + str(populated))
                src.mkdir(); dst.mkdir()
                (src / "source").write_bytes(b"source")
                if populated:
                    (dst / "sentinel").write_bytes(b"independent destination")
                inode = dst.stat().st_ino
                with self.assertRaises(state.Refusal):
                    txn.promote_journal(src, dst)
                self.assertEqual(dst.stat().st_ino, inode)
                self.assertEqual((src / "source").read_bytes(), b"source")
                self.assertEqual({p.name for p in dst.iterdir()}, {"sentinel"} if populated else set())

    def test_missing_renameat2_never_falls_back(self):
        with patch.object(txn.ctypes, "CDLL", return_value=object()):
            with self.assertRaisesRegex(state.Refusal, "no-replace"):
                self.prepare()
        self.assertFalse(list(self.container.glob("install-*")))
        self.scan()
        self.untouched()

    def test_kernel_rename_unavailable_retains_preparation(self):
        with patch.object(txn.ctypes, "CDLL") as library:
            library.return_value.renameat2.return_value = -1
            with patch.object(txn.ctypes, "get_errno", return_value=errno.ENOSYS):
                with self.assertRaisesRegex(state.Refusal, "Function not implemented"):
                    self.prepare()
        self.assertFalse(list(self.container.glob("install-*")))
        self.assertEqual(len(list(self.container.glob("prepare-*"))), 1)
        self.scan()
        self.untouched()

    def test_file_destination_and_missing_source_refuse_without_changes(self):
        source, destination = self.root / "prepare-test", self.root / "install-test"
        source.mkdir()
        destination.write_bytes(b"independent destination")
        with self.assertRaises(state.Refusal):
            txn.promote_journal(source, destination)
        self.assertEqual(destination.read_bytes(), b"independent destination")
        self.assertTrue(source.is_dir())
        with self.assertRaises(state.Refusal):
            txn.promote_journal(self.root / "missing", self.root / "absent")
        self.assertFalse((self.root / "absent").exists())
        self.untouched()

    def test_legacy_incomplete_install_is_not_ignored(self):
        legacy = self.container / "install-legacy"
        legacy.mkdir(mode=0o700)
        with self.assertRaisesRegex(state.Refusal, "legacy journal"):
            self.prepare()
        self.assertEqual(list(self.container.iterdir()), [legacy])
        self.untouched()

    def test_untrusted_install_path_and_corrupt_record_refuse(self):
        prior = self.container / "install-prior"
        prior.symlink_to(self.root / "source")
        with self.assertRaises(state.Refusal):
            self.scan()
        prior.unlink()
        prior.mkdir(mode=0o700)
        (prior / "state.json").write_text("{}")
        (prior / "state.json").chmod(0o600)
        with self.assertRaises(state.Refusal):
            self.scan()
        self.untouched()

    def test_prepare_namespace_is_not_mutation_capable(self):
        with patch.object(txn, "promote_journal", side_effect=state.Refusal("stop before promote")):
            with self.assertRaises(state.Refusal):
                self.prepare()
        orphan, = self.container.glob("prepare-*")
        journal = state.InstallState(orphan, self.root, self.owner)
        self.assertEqual(journal.load()["phase"], "prepared")
        with self.assertRaisesRegex(state.Refusal, "published install"):
            txn.apply_prepared(journal, self.sources, REF, self.owner, self.gid, self.gid)
        self.untouched()
        self.scan()

    def test_apply_refuses_source_preimage_key_and_service_drift_before_stop(self):
        journal = self.prepare()
        target = journal.target(state.FILES["broker.py"])
        original = target.read_bytes()
        target.write_bytes(b"independent edit")
        with self.assertRaises(state.Refusal):
            txn.apply_prepared(journal, self.sources, REF, self.owner, self.gid, self.gid)
        self.control.assert_not_called()
        target.write_bytes(original)
        os.utime(target, ns=(journal.load()["files"][state.FILES["broker.py"]]["before"]["atimeNs"],
                             journal.load()["files"][state.FILES["broker.py"]]["before"]["mtimeNs"]))
        source = self.sources["broker.py"]
        source_bytes = source.read_bytes()
        source.write_bytes(b"changed source")
        with self.assertRaisesRegex(state.Refusal, "source changed"):
            txn.apply_prepared(journal, self.sources, REF, self.owner, self.gid, self.gid)
        source.write_bytes(source_bytes)
        key = journal.target(state.KEYS[0])
        key.chmod(0o640)
        with self.assertRaises(state.Refusal):
            txn.apply_prepared(journal, self.sources, REF, self.owner, self.gid, self.gid)
        key.chmod(0o600)
        self.service.return_value = dict(INACTIVE)
        with self.assertRaisesRegex(state.Refusal, "service state changed"):
            txn.apply_prepared(journal, self.sources, REF, self.owner, self.gid, self.gid)
        self.control.assert_not_called()

    def test_crash_after_promotion_has_a_usable_rollback_and_blocks_retry(self):
        journal = self.prepare()
        with self.assertRaises(state.Refusal):
            self.prepare()
        # No application has happened. Rollback is still a valid, locked operator
        # recovery route after a process dies between promotion and apply.
        self.service.side_effect = lambda require_stable=True: dict(ACTIVE if require_stable else INACTIVE)
        state.rollback(journal)
        self.assertEqual(journal.load()["phase"], "rolled_back")
        journal.verify_files(journal.load())
        self.scan()

    def test_cli_refuses_rollback_of_preparation_directory(self):
        @contextmanager
        def lock():
            yield self.container
        with patch.object(os, "geteuid", return_value=0), patch.object(os, "getgid", return_value=0), \
             patch.object(txn, "install_lock", lock), patch.object(state, "rollback") as rollback:
            self.assertEqual(txn.main(["rollback", "--state", str(self.container / "prepare-test")]), 2)
            rollback.assert_not_called()
        self.untouched()


if __name__ == "__main__":
    unittest.main()
