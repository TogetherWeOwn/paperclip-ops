#!/usr/bin/env python3
"""Installer journal tests use private temp roots and fake systemd only."""

import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("install_state", ROOT / "host-ops/install_state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
ACTIVE = {"active": "active", "enabled": "enabled"}
INACTIVE = {"active": "inactive", "enabled": "disabled"}
ABSENT = {"active": "inactive", "enabled": "absent"}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-ops-install-state-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "var/lib/paperclip-host-ops-installer/install-test"
        self.directory.mkdir(parents=True, mode=0o700)
        self.journal = state.InstallState(self.directory, self.root, os.getuid())
        self.sources = {}
        source_dir = self.root / "source"
        source_dir.mkdir()
        for i, (name, absolute) in enumerate(state.FILES.items()):
            source = source_dir / name
            source.write_bytes(("new-" + name).encode())
            self.sources[name] = source
            target = self.journal.target(absolute)
            target.parent.mkdir(parents=True, exist_ok=True)
            if i % 2 == 0:
                target.write_bytes(("old-" + name).encode())
                target.chmod(0o640)
                os.utime(target, ns=(10074797050384100, 10074797050384100))
        for i, absolute in enumerate(state.KEYS):
            path = self.journal.target(absolute)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PRIVATE-SYNTHETIC-CANARY" if i == 0 else b"public-fixture-key")
            path.chmod(0o600 if i == 0 else 0o644)
        self.key_contents = {key: self.journal.target(key).read_bytes() for key in state.KEYS}

    def snapshot(self, service=ACTIVE):
        return self.journal.snapshot(self.sources, "a" * 40, copy.deepcopy(service))

    def apply(self, count=None):
        for i, (name, absolute) in enumerate(state.FILES.items()):
            if count is not None and i >= count:
                break
            target = self.journal.target(absolute)
            target.write_bytes(self.sources[name].read_bytes())
            target.chmod(0o644)

    def assert_restored(self, record):
        self.journal.verify_files(record)
        for key, content in self.key_contents.items():
            self.assertEqual(self.journal.target(key).read_bytes(), content)

    def test_snapshot_is_durable_private_and_has_no_private_key_bytes(self):
        record = self.snapshot()
        path = self.directory / "state.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.journal.load(), record)
        data = path.read_bytes()
        self.assertNotIn(b"PRIVATE-SYNTHETIC-CANARY", data)
        self.assertNotIn(base64.b64encode(b"PRIVATE-SYNTHETIC-CANARY"), data)
        self.assertEqual(set(record["files"]), set(state.FILES.values()))
        self.assertEqual(set(record["keyMetadata"]), set(state.KEYS))
        with self.assertRaises(state.Refusal):
            self.snapshot()

    def test_restore_all_preimages_bytes_modes_times_and_absence(self):
        record = self.snapshot()
        self.apply()
        self.journal.target(state.KEYS[0]).chmod(0o640)
        self.journal.restore_files(record)
        self.assert_restored(record)
        self.journal.restore_files(record)  # Idempotent after a partial rollback.
        self.assert_restored(record)

    def test_partial_installs_restore_at_every_completed_file_boundary(self):
        record = self.snapshot()
        for count in range(len(state.FILES) + 1):
            with self.subTest(completed=count):
                self.apply(count)
                self.journal.restore_files(record)
                self.assert_restored(record)

    def test_xattrs_are_preserved_not_silently_dropped(self):
        path = self.journal.target(state.FILES["broker.py"])
        os.setxattr(path, "user.host-ops-fixture", b"prior-xattr")
        record = self.snapshot()
        self.apply()
        os.setxattr(path, "user.host-ops-fixture", b"new-xattr")
        self.journal.restore_files(record)
        self.assertEqual(os.getxattr(path, "user.host-ops-fixture"), b"prior-xattr")
        self.assert_restored(record)

    def test_independent_change_refuses_before_any_restore(self):
        record = self.snapshot()
        self.apply()
        first = self.journal.target(state.FILES["broker.py"])
        last = self.journal.target(state.PUBLIC_OUT)
        last.write_bytes(b"independent-host-edit")
        with self.assertRaises(state.Refusal):
            self.journal.restore_files(record)
        self.assertEqual(first.read_bytes(), self.sources["broker.py"].read_bytes())
        self.assertEqual(last.read_bytes(), b"independent-host-edit")

    def test_unknown_partial_file_is_not_misreported_as_restored(self):
        record = self.snapshot()
        target = self.journal.target(state.FILES["broker.py"])
        target.write_bytes(b"truncated-unrecognised-copy")
        with self.assertRaises(state.Refusal):
            self.journal.restore_files(record)
        self.assertEqual(self.journal.load()["phase"], "prepared")

    def test_replaced_signing_key_refuses_without_touching_files(self):
        record = self.snapshot()
        self.apply()
        key = self.journal.target(state.KEYS[0])
        replacement = key.with_name("replacement")
        replacement.write_bytes(b"different synthetic key")
        os.replace(replacement, key)
        with self.assertRaises(state.Refusal):
            self.journal.restore_files(record)
        self.assertEqual(self.journal.target(state.FILES["broker.py"]).read_bytes(), self.sources["broker.py"].read_bytes())
        self.assertEqual(key.read_bytes(), b"different synthetic key")

    def test_symlink_hardlink_and_writable_parent_refuse(self):
        record = self.snapshot()
        first = self.journal.target(state.FILES["broker.py"])
        old = first.read_bytes()
        external = self.root / "external"
        external.write_bytes(old)
        first.unlink()
        first.symlink_to(external)
        with self.assertRaises((state.Refusal, OSError)):
            self.journal.restore_files(record)
        first.unlink()
        os.link(external, first)
        with self.assertRaises(state.Refusal):
            self.journal.restore_files(record)
        first.unlink()
        first.write_bytes(old)
        first.parent.chmod(0o777)
        try:
            with self.assertRaises(state.Refusal):
                self.journal.restore_files(record)
        finally:
            first.parent.chmod(0o755)
        self.assertEqual(external.read_bytes(), old)

    def test_snapshot_refuses_symlink_source_and_no_journal_is_published(self):
        source = self.sources["broker.py"]
        source.unlink()
        source.symlink_to(self.sources["config"])
        with self.assertRaises((state.Refusal, OSError)):
            self.snapshot()
        self.assertFalse((self.directory / "state.json").exists())

    def test_corrupt_manifest_cannot_retarget_or_restore_modified_backup_bytes(self):
        record = self.snapshot()
        variations = []
        changed = copy.deepcopy(record)
        changed["files"]["/etc/other-file"] = changed["files"].pop(state.PUBLIC_OUT)
        variations.append(changed)
        changed = copy.deepcopy(record)
        changed["files"][state.FILES["broker.py"]]["before"]["content"] = base64.b64encode(b"tampered backup").decode()
        variations.append(changed)
        changed = copy.deepcopy(record)
        changed["keyMetadata"][state.KEYS[0]]["content"] = "forbidden"
        variations.append(changed)
        changed = copy.deepcopy(record)
        changed["service"] = {"active": "active", "enabled": "absent"}
        variations.append(changed)
        for item in variations:
            with self.subTest(item=item), self.assertRaises(state.Refusal):
                self.journal.restore_files(item)
        self.assert_restored(record)

    def test_unsafe_journal_modes_refuse(self):
        self.snapshot()
        path = self.directory / "state.json"
        path.chmod(0o644)
        with self.assertRaises(state.Refusal):
            self.journal.load()
        path.chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaises(state.Refusal):
            self.journal.load()

    def test_restore_write_failure_keeps_journal_retriable(self):
        record = self.snapshot()
        self.apply()
        real_replace = state.os.replace
        count = 0
        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic disk failure")
            return real_replace(source, destination)
        with patch.object(state.os, "replace", side_effect=fail_second), self.assertRaises(OSError):
            self.journal.restore_files(record)
        self.assertEqual(self.journal.load()["phase"], "prepared")
        self.journal.restore_files(record)
        self.assert_restored(record)
        self.assertFalse(list(self.root.rglob(".host-ops-restore-*")))

    def test_noop_restore_cannot_pass_postcondition(self):
        record = self.snapshot()
        self.apply()
        with patch.object(state.os, "replace", return_value=None), self.assertRaises(state.Refusal):
            self.journal.restore_files(record)

    def test_service_restore_active_and_enabled(self):
        record = self.snapshot(ACTIVE)
        self.apply()
        with patch.object(state, "service_state", side_effect=[INACTIVE, INACTIVE, ACTIVE]), patch.object(state, "systemctl", return_value=(0, "")) as ctl:
            state.rollback(self.journal)
        self.assertEqual([call.args for call in ctl.call_args_list], [
            ("stop", state.UNIT), ("daemon-reload",), ("enable", state.UNIT), ("start", state.UNIT),
        ])
        self.assertEqual(self.journal.load()["phase"], "rolled_back")
        self.assert_restored(record)
        with patch.object(state, "service_state", return_value=ACTIVE), patch.object(state, "systemctl", side_effect=AssertionError("must not repeat service mutations")):
            state.rollback(self.journal)
            self.journal.target(state.FILES["broker.py"]).write_bytes(b"later independent edit")
            with self.assertRaises(state.Refusal):
                state.rollback(self.journal)

    def test_service_restore_absent_disables_before_removing_unit(self):
        for absolute in state.FILES.values():
            self.journal.target(absolute).unlink(missing_ok=True)
        record = self.snapshot(ABSENT)
        self.apply()
        with patch.object(state, "service_state", side_effect=[ACTIVE, INACTIVE, ABSENT]), patch.object(state, "systemctl", return_value=(0, "")) as ctl:
            state.rollback(self.journal)
        self.assertEqual([call.args for call in ctl.call_args_list], [
            ("stop", state.UNIT), ("disable", state.UNIT), ("daemon-reload",),
        ])
        self.assert_restored(record)

    def test_service_stop_failure_never_starts_file_restore(self):
        self.snapshot()
        self.apply()
        with patch.object(state, "service_state", return_value=ACTIVE), patch.object(state, "systemctl", side_effect=state.Refusal("service failed")):
            with self.assertRaises(state.Refusal):
                state.rollback(self.journal)
        self.assertEqual(self.journal.target(state.FILES["broker.py"]).read_bytes(), self.sources["broker.py"].read_bytes())
        self.assertEqual(self.journal.load()["phase"], "prepared")

    def test_bad_service_postcondition_never_marks_rolled_back(self):
        record = self.snapshot()
        self.apply()
        with patch.object(state, "service_state", side_effect=[INACTIVE, INACTIVE, INACTIVE]), patch.object(state, "systemctl", return_value=(0, "")):
            with self.assertRaises(state.Refusal):
                state.rollback(self.journal)
        self.assert_restored(record)
        self.assertEqual(self.journal.load()["phase"], "prepared")

    def test_stable_snapshot_rejects_failed_but_rollback_can_stop_failed(self):
        observation = "LoadState=loaded\nActiveState=failed\nUnitFileState=enabled\n"
        with patch.object(state, "systemctl", return_value=(0, observation)):
            with self.assertRaises(state.Refusal):
                state.service_state()
            self.assertEqual(state.service_state(require_stable=False), {"active": "failed", "enabled": "enabled"})

    def test_installed_marker_requires_every_postimage_and_active_service(self):
        self.snapshot()
        with patch.object(state, "service_state", return_value=ACTIVE):
            with self.assertRaises((state.Refusal, OSError)):
                state.mark_installed(self.journal)
            self.apply()
            state.mark_installed(self.journal)
        self.assertEqual(self.journal.load()["phase"], "installed")
        with patch.object(state, "service_state", return_value=ACTIVE), self.assertRaises(state.Refusal):
            state.mark_installed(self.journal)

    def test_installed_marker_refuses_inactive_service(self):
        self.snapshot()
        self.apply()
        with patch.object(state, "service_state", return_value=INACTIVE), self.assertRaises(state.Refusal):
            state.mark_installed(self.journal)
        self.assertEqual(self.journal.load()["phase"], "prepared")

    def test_failed_start_can_roll_back_to_disabled_inactive(self):
        record = self.snapshot(INACTIVE)
        self.apply()
        failed = {"active": "failed", "enabled": "enabled"}
        with patch.object(state, "service_state", side_effect=[failed, INACTIVE, INACTIVE]) as observe, patch.object(state, "systemctl", return_value=(0, "")) as ctl:
            state.rollback(self.journal)
        self.assertEqual(observe.call_args_list[0].kwargs, {"require_stable": False})
        self.assertEqual([call.args for call in ctl.call_args_list], [
            ("stop", state.UNIT), ("disable", state.UNIT), ("daemon-reload",),
        ])
        self.assert_restored(record)
        self.assertEqual(self.journal.load()["phase"], "rolled_back")

    def test_snapshot_rejects_short_ref_or_incomplete_inventory(self):
        for sources, ref in ((self.sources, "a" * 7), ({}, "a" * 40)):
            with self.assertRaises(state.Refusal):
                self.journal.snapshot(sources, ref, ACTIVE)
        self.assertFalse((self.directory / "state.json").exists())

    def test_fifo_source_refuses_without_blocking(self):
        source = self.sources["broker.py"]
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(state.Refusal):
            self.snapshot()

    def test_audit_spool_and_replay_contents_are_untouched(self):
        sentinels = {}
        for relative in ("var/log/paperclip-host-ops/audit.jsonl", "var/spool/paperclip-host-ops/requests/request.json", "var/lib/paperclip-host-ops/replay-state.json"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("sentinel-" + relative).encode())
            sentinels[path] = (path.read_bytes(), path.stat().st_ino)
        record = self.snapshot()
        self.apply()
        self.journal.restore_files(record)
        for path, (content, inode) in sentinels.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_ino, inode)

    def test_cli_root_and_fixed_journal_path_are_required(self):
        with patch.object(state.os, "geteuid", return_value=1000), patch.object(state, "InstallState") as constructor, patch.object(state.sys, "stderr"):
            self.assertEqual(state.main(["rollback", "--state", str(state.JOURNAL_ROOT / "install-test")]), 2)
            constructor.assert_not_called()
        with patch.object(state.os, "geteuid", return_value=0), patch.object(state, "InstallState") as constructor, patch.object(state.sys, "stderr"):
            for path in ("/tmp/install-test", str(state.JOURNAL_ROOT / ".."), str(state.JOURNAL_ROOT / "install-test/nested")):
                self.assertEqual(state.main(["rollback", "--state", path]), 2)
            constructor.assert_not_called()

    def test_cli_refuses_malformed_journal_with_nonzero_exit(self):
        self.snapshot()
        (self.directory / "state.json").write_text(json.dumps({"version": 1}))
        with patch.object(state.os, "geteuid", return_value=0), patch.object(state, "InstallState", return_value=self.journal), patch.object(state.sys, "stderr"), patch.object(state, "systemctl", side_effect=AssertionError("invalid journal must not reach systemd")):
            self.assertEqual(state.main(["rollback", "--state", str(state.JOURNAL_ROOT / "install-test")]), 2)

    def test_absent_service_keeps_empty_property_with_all(self):
        def show(*args):
            self.assertEqual(args, ("show", state.UNIT, "--all", "--no-pager", "--property=LoadState,ActiveState,UnitFileState"))
            return 0, "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n"
        with patch.object(state, "systemctl", side_effect=show):
            self.assertEqual(state.service_state(), ABSENT)
        with patch.object(state, "systemctl", return_value=(0, "LoadState=not-found\nActiveState=inactive\n")):
            with self.assertRaises(state.Refusal):
                state.service_state()

    def test_key_inplace_write_refuses_before_file_restore(self):
        record = self.snapshot()
        self.apply()
        key = self.journal.target(state.KEYS[0])
        key.write_bytes(b"X" * key.stat().st_size)
        saved = record["keyMetadata"][state.KEYS[0]]
        os.utime(key, ns=(saved["atimeNs"], saved["mtimeNs"] + 1000000000))
        self.assertEqual(key.stat().st_ino, saved["inode"])
        with self.assertRaises(state.Refusal):
            self.journal.restore_files(record)
        self.assertEqual(self.journal.target(state.FILES["broker.py"]).read_bytes(), self.sources["broker.py"].read_bytes())

    def test_unsafe_key_preimage_refuses_snapshot(self):
        for absolute, mode in ((state.KEYS[0], 0o644), (state.KEYS[1], 0o666)):
            key = self.journal.target(absolute)
            old_mode = stat.S_IMODE(key.stat().st_mode)
            key.chmod(mode)
            try:
                with self.assertRaises(state.Refusal):
                    self.snapshot()
            finally:
                key.chmod(old_mode)
        self.assertFalse((self.directory / "state.json").exists())

    def test_service_must_be_inactive_before_file_restore(self):
        self.snapshot()
        self.apply()
        with patch.object(state, "service_state", return_value=ACTIVE), patch.object(state, "systemctl", return_value=(0, "")):
            with self.assertRaises(state.Refusal):
                state.rollback(self.journal)
        self.assertEqual(self.journal.target(state.FILES["broker.py"]).read_bytes(), self.sources["broker.py"].read_bytes())
        self.assertEqual(self.journal.load()["phase"], "prepared")

    def test_restart_changes_cannot_be_recorded_as_successful_restore(self):
        record = self.snapshot()
        self.apply()
        def service_mutation(*args):
            if args == ("start", state.UNIT):
                self.journal.target(state.FILES["broker.py"]).write_bytes(b"changed during restart")
            return 0, ""
        with patch.object(state, "service_state", side_effect=[INACTIVE, INACTIVE, ACTIVE]), patch.object(state, "systemctl", side_effect=service_mutation):
            with self.assertRaises(state.Refusal):
                state.rollback(self.journal)
        self.assertEqual(self.journal.load()["phase"], "prepared")
        self.assertNotEqual(self.journal.target(state.FILES["broker.py"]).read_bytes(), base64.b64decode(record["files"][state.FILES["broker.py"]]["before"]["content"]))

    def test_new_file_parent_is_revalidated_before_unlink(self):
        record = self.snapshot()
        self.apply()
        first = self.journal.target(state.FILES["broker.py"])
        new_file = self.journal.target(state.FILES["image_binding.py"])
        real_replace = state.os.replace
        def change_parent(source, destination):
            real_replace(source, destination)
            if destination == first:
                first.parent.chmod(0o777)
        try:
            with patch.object(state.os, "replace", side_effect=change_parent), self.assertRaises(state.Refusal):
                self.journal.restore_files(record)
            self.assertTrue(new_file.exists())
        finally:
            first.parent.chmod(0o755)

    def test_key_parent_is_revalidated_before_metadata_open(self):
        record = self.snapshot()
        self.apply()
        last = self.journal.target(state.PUBLIC_OUT)
        key = self.journal.target(state.KEYS[0])
        real_replace = state.os.replace
        def change_parent(source, destination):
            real_replace(source, destination)
            if destination == last:
                key.parent.chmod(0o777)
        try:
            with patch.object(state.os, "replace", side_effect=change_parent), patch.object(state.os, "open", wraps=state.os.open) as opened:
                with self.assertRaises(state.Refusal):
                    self.journal.restore_files(record)
                self.assertFalse(any(call.args[0] == key for call in opened.call_args_list))
        finally:
            key.parent.chmod(0o755)

    def test_systemctl_errors_are_bounded_and_sanitized(self):
        with patch.object(state.subprocess, "run", side_effect=subprocess.TimeoutExpired("sensitive-fixture", 45)):
            with self.assertRaises(state.Refusal) as caught:
                state.systemctl("stop", state.UNIT)
        self.assertNotIn("sensitive-fixture", str(caught.exception))
        with patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")) as run:
            state.systemctl("daemon-reload")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/systemctl", "daemon-reload"])
        self.assertEqual(run.call_args.kwargs["timeout"], 45)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
