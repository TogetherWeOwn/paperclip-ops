#!/usr/bin/env python3
"""Synthetic filesystem faults only; no operational adapter or network access."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("plugin_deploy", SOURCE / "cliproxy_plugin_deploy.py")
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        # Do not launch detached timers from the fixture suite.
        watchdog = patch.object(subject.Deployment, "start_watchdog")
        watchdog.start()
        self.addCleanup(watchdog.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="plugin-transaction-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "service"
        self.state = self.base / "state"
        self.root.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        (self.root / "auths").mkdir(mode=0o700)
        (self.root / "plugins").mkdir(mode=0o700)
        self.config = self.root / "config.yaml"
        self.config.write_text("baseline: true\n")
        self.config.chmod(0o600)
        self.original = self.config.read_bytes()
        self.auth = self.root / "auths/dummy.json"
        self.artifact = self.root / "plugins/dummy.so"
        self.source = self.base / "dummy.so"
        self.source.write_bytes(b"not a shared library\n")
        self.adapter = self.base / "adapter"
        self.adapter.write_text('''#!/bin/sh
set -eu
case "$1" in
  render) printf 'baseline: true\nplugin: true\n' > "$2/config.yaml.new"; chmod 600 "$2/config.yaml.new" ;;
  auth) printf '{"dummy": "inert"}\n' ;;
  preflight|models-baseline|route|verify|restart) exit 0 ;;
  *) exit 2 ;;
esac
''')
        self.adapter.chmod(0o700)
        self.env = {"S": str(self.root), "STATE_DIR": str(self.state),
                    "AUTH_REL": "auths/dummy.json", "ARTIFACT_REL": "plugins/dummy.so",
                    "ARTIFACT_SRC": str(self.source), "SO_SHA": subject.digest(self.source),
                    "DEPLOY_ADAPTER": str(self.adapter), "WD_SECS": "0"}
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.deploy = subject.Deployment(self.state)
        self.assertEqual(self.deploy.run(), 0)
        self.rendered = self.config.read_bytes()
        self.assertEqual(self.marker(), "verified")

    def marker(self):
        return (self.state / "state").read_text().strip()

    def assert_restored(self):
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertFalse(os.path.lexists(self.auth))
        self.assertFalse(os.path.lexists(self.artifact))
        self.assertEqual(self.marker(), "rolledback")

    def test_success_and_repeated_rollback(self):
        self.assertEqual(self.deploy.rollback(), 0)
        self.assert_restored()
        self.assertEqual(self.deploy.rollback(), 0)
        self.assert_restored()

    def test_failed_mutations_cannot_be_hidden_by_healthy_routes(self):
        real = subject.command
        for fault in ("copy", "auth-remove", "artifact-remove", "chmod"):
            with self.subTest(fault=fault):
                def injected(*args):
                    fail = ((fault == "copy" and args[0] == "cp") or
                            (fault == "chmod" and args[0] == "chmod") or
                            (fault == "auth-remove" and args[-1] == str(self.auth)) or
                            (fault == "artifact-remove" and args[-1] == str(self.artifact)))
                    if fail:
                        raise subprocess.CalledProcessError(1, args[0])
                    return real(*args)
                with patch.object(subject, "command", side_effect=injected):
                    self.assertEqual(self.deploy.rollback(), 1)
                self.assertEqual(self.marker(), "rollback-FAILED")
                self.assertEqual(self.deploy.rollback(), 0)
                self.assert_restored()

    def test_successful_noop_copy_and_chmod_are_detected(self):
        real = subject.command
        for fault in ("cp", "chmod"):
            with self.subTest(fault=fault):
                def injected(*args):
                    if args[0] == fault:
                        if fault == "chmod":
                            Path(args[-1]).chmod(0o644)
                        return
                    return real(*args)
                with patch.object(subject, "command", side_effect=injected):
                    self.assertEqual(self.deploy.rollback(), 1)
                self.assertEqual(self.marker(), "rollback-FAILED")

    def test_successful_noop_removals_are_detected(self):
        real = subject.command
        for target in (self.auth, self.artifact):
            with self.subTest(target=target.name):
                target.write_bytes(b'{"dummy": "inert"}\n' if target == self.auth else self.source.read_bytes())
                def injected(*args):
                    if args[0] == "rm" and args[-1] == str(target):
                        return
                    return real(*args)
                with patch.object(subject, "command", side_effect=injected):
                    self.assertEqual(self.deploy.rollback(), 1)
                self.assertEqual(self.marker(), "rollback-FAILED")

    def test_state_write_failure_never_returns_success(self):
        real = subject.atomic
        for failed_state in (b"rolling-back\n", b"rolledback\n", b"rollback-FAILED\n"):
            with self.subTest(failed_state=failed_state):
                def injected(path, data, *args):
                    if path.name == "state" and (data == failed_state or failed_state == b"rollback-FAILED\n"):
                        raise OSError("injected state persistence failure")
                    return real(path, data, *args)
                with patch.object(subject, "atomic", side_effect=injected):
                    self.assertEqual(self.deploy.rollback(), 1)
                self.assertNotEqual(self.marker(), "rolledback")

    def test_state_replace_failure_is_atomic_and_nonzero(self):
        real = subject.os.replace
        def injected(src, dst):
            if Path(dst).name == "state":
                raise OSError("injected rename failure")
            return real(src, dst)
        with patch.object(subject.os, "replace", side_effect=injected):
            self.assertEqual(self.deploy.rollback(), 1)
        self.assertEqual(self.marker(), "verified")
        self.assertFalse(list(self.state.glob(".transaction-*")))

    def test_contender_is_busy_not_completed_then_can_retry(self):
        with (self.root / ".cliproxy-plugin.lock").open("rb") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(["bash", str(SOURCE / "cliproxy_plugin_deploy.sh"), "rollback"],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 75)
            self.assertEqual(self.marker(), "verified")
            self.assertEqual(self.config.read_bytes(), self.rendered)
        self.assertEqual(self.deploy.rollback(), 0)
        self.assert_restored()

    def test_repeat_rechecks_disk_not_just_marker(self):
        self.assertEqual(self.deploy.rollback(), 0)
        self.config.chmod(0o644)
        self.assertEqual(self.deploy.rollback(), 0)
        self.assert_restored()
        self.config.write_text("later unrelated config\n")
        with self.assertRaises(subject.Refusal):
            self.deploy.ownership()
        self.assertEqual(self.deploy.rollback(), 1)
        self.assertEqual(self.config.read_text(), "later unrelated config\n")

    def test_unrelated_identity_is_never_overwritten_or_deleted(self):
        for target in (self.config, self.auth, self.artifact):
            with self.subTest(target=target.name):
                saved = target.read_bytes()
                target.write_bytes(b"unrelated owner\n")
                self.assertEqual(self.deploy.rollback(), 1)
                self.assertEqual(target.read_bytes(), b"unrelated owner\n")
                target.write_bytes(saved)

    def test_replaced_owner_or_backup_refuses_before_mutation(self):
        for target in (self.root / ".cliproxy-plugin-owner", self.state / "config.yaml.orig",
                       self.state / "deployment.json"):
            with self.subTest(target=target.name):
                saved = target.read_bytes()
                target.write_bytes(b"wrong identity")
                self.assertNotEqual(subject.main(["rollback"]), 0)
                self.assertEqual(self.config.read_bytes(), self.rendered)
                self.assertTrue(self.auth.exists())
                target.write_bytes(saved)

    def test_symlink_target_refused_without_touching_referent(self):
        self.auth.unlink()
        victim = self.base / "unrelated"
        victim.write_bytes(b"preserve me")
        self.auth.symlink_to(victim)
        self.assertEqual(subject.main(["rollback"]), 1)
        self.assertEqual(victim.read_bytes(), b"preserve me")

    def test_health_failure_and_restart_failure_are_not_success(self):
        with patch.object(self.deploy, "healthy", return_value=False):
            self.assertEqual(self.deploy.rollback(), 1)
        self.assertEqual(self.marker(), "rollback-FAILED")
        with patch.object(self.deploy, "healthy", side_effect=[False, True]):
            self.assertEqual(self.deploy.rollback(), 0)
        self.assert_restored()

    def test_watchdog_does_not_reopen_completed_rollback(self):
        self.assertEqual(self.deploy.rollback(), 0)
        old_state = (self.state / "state").read_bytes()
        self.config.write_bytes(b"newer deployment config\n")
        (self.root / ".cliproxy-plugin-owner").write_text("newer deployment\n")
        with patch.object(self.deploy, "rollback_locked", wraps=self.deploy.rollback_locked) as rollback, \
                patch.object(self.deploy, "helper") as helper:
            self.assertEqual(self.deploy.watchdog(), 0)
        rollback.assert_not_called()
        helper.assert_not_called()
        self.assertEqual((self.state / "state").read_bytes(), old_state)
        self.assertEqual(self.config.read_bytes(), b"newer deployment config\n")

    def test_watchdog_propagates_rollback_failure_and_rechecks_repeat(self):
        self.deploy.state("loading")
        with patch.object(self.deploy, "restore_config", side_effect=OSError("copy failed")):
            self.assertEqual(self.deploy.watchdog(), 1)
        self.assertEqual(self.marker(), "rollback-FAILED")
        self.assertEqual(self.deploy.watchdog(), 1)
        self.assertEqual(self.deploy.rollback(), 0)
        self.assertEqual(self.deploy.watchdog(), 0)
        self.config.write_bytes(b"later config")
        self.assertEqual(self.deploy.watchdog(), 0)  # historical completion, not fresh qualification
        self.assertEqual(self.marker(), "rolledback")

    def test_forward_copy_failure_does_not_publish_partial_material(self):
        self.assertEqual(self.deploy.rollback(), 0)
        real = subject.os.fsync
        for target in (self.artifact, self.auth):
            with self.subTest(target=target.name):
                fresh = self.base / (target.name + "-write-fault")
                fresh.mkdir(mode=0o700)
                fired = False
                def injected(fd):
                    nonlocal fired
                    path = Path(f"/proc/self/fd/{fd}").resolve()
                    if not fired and path.parent == target.parent and stat.S_ISREG(os.fstat(fd).st_mode):
                        fired = True
                        raise OSError("injected forward copy persistence failure")
                    return real(fd)
                second = subject.Deployment(fresh)
                with patch.object(subject.os, "fsync", side_effect=injected):
                    self.assertEqual(second.run(), 1)
                self.assertTrue(fired)
                self.assertEqual(second.read_state(), "rolledback")
                self.assertFalse(self.auth.exists() or self.artifact.exists())
                self.assertFalse(list(target.parent.glob(".transaction-*")))
                self.assertEqual(self.config.read_bytes(), self.original)

    def test_partial_forward_write_never_reaches_destination(self):
        self.assertEqual(self.deploy.rollback(), 0)
        real = subject.os.fdopen
        for target in (self.artifact, self.auth):
            with self.subTest(target=target.name):
                fresh = self.base / (target.name + "-partial-write")
                fresh.mkdir(mode=0o700)
                def injected(fd, *args, **kwargs):
                    path = Path(f"/proc/self/fd/{fd}").resolve()
                    stream = real(fd, *args, **kwargs)
                    if path.parent == target.parent:
                        write = stream.write
                        def partial(data):
                            write(data[:1])
                            raise OSError("injected partial forward write")
                        stream.write = partial
                    return stream
                second = subject.Deployment(fresh)
                with patch.object(subject.os, "fdopen", side_effect=injected):
                    self.assertEqual(second.run(), 1)
                self.assertEqual(second.read_state(), "rolledback")
                self.assertFalse(self.auth.exists() or self.artifact.exists())
                self.assertFalse(list(target.parent.glob(".transaction-*")))
                self.assertEqual(self.config.read_bytes(), self.original)

    def test_incompatible_baseline_metadata_refuses_before_service_writes(self):
        self.assertEqual(self.deploy.rollback(), 0)
        owner_marker = (self.root / ".cliproxy-plugin-owner").read_bytes()
        real_stat = Path.stat
        for fault in ("mode", "owner", "group"):
            with self.subTest(fault=fault):
                fresh = self.base / ("metadata-" + fault)
                fresh.mkdir(mode=0o700)
                self.config.write_bytes(self.original)
                self.auth.unlink(missing_ok=True)
                self.artifact.unlink(missing_ok=True)
                (self.root / ".cliproxy-plugin-owner").write_bytes(owner_marker)
                self.config.chmod(0o644 if fault == "mode" else 0o600)
                def injected(path, *args, **kwargs):
                    info = real_stat(path, *args, **kwargs)
                    if path == self.config and fault in ("owner", "group"):
                        fields = list(info)
                        fields[4 if fault == "owner" else 5] += 1
                        return os.stat_result(fields)
                    return info
                with patch.object(Path, "stat", injected):
                    self.assertEqual(subject.Deployment(fresh).run(), 3)
                self.assertEqual((fresh / "state").read_text().strip(), "aborted-unchanged")
                self.assertFalse((fresh / "deployment.json").exists())
                self.assertEqual((self.root / ".cliproxy-plugin-owner").read_bytes(), owner_marker)
                self.assertEqual(self.config.read_bytes(), self.original)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode),
                                 0o644 if fault == "mode" else 0o600)
                self.assertFalse(self.auth.exists() or self.artifact.exists())

    def test_release_source_replacement_uses_only_validated_bytes(self):
        self.assertEqual(self.deploy.rollback(), 0)
        fresh = self.base / "release-replacement"
        fresh.mkdir(mode=0o700)
        second = subject.Deployment(fresh)
        validated = self.source.read_bytes()
        real = second.helper
        def injected(action):
            if action == "preflight":
                replacement = self.base / "updated-release.so"
                replacement.write_bytes(b"different release bytes\n")
                os.replace(replacement, self.source)
            return real(action)
        with patch.object(second, "helper", side_effect=injected):
            self.assertEqual(second.run(), 0)
        self.assertEqual(self.artifact.read_bytes(), validated)
        self.assertEqual(subject.digest(self.artifact), self.env["SO_SHA"])
        self.assertNotEqual(self.artifact.read_bytes(), self.source.read_bytes())
        self.assertEqual(second.rollback(), 0)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.auth.exists() or self.artifact.exists())

    def test_release_snapshot_mismatch_refuses_before_service_publication(self):
        self.assertEqual(self.deploy.rollback(), 0)
        fresh = self.base / "release-read-drift"
        fresh.mkdir(mode=0o700)
        real_read = Path.read_bytes
        def injected(path):
            if path == self.source:
                return b"unexpected bytes returned by release read\n"
            return real_read(path)
        marker = (self.root / ".cliproxy-plugin-owner").read_bytes()
        with patch.object(Path, "read_bytes", injected):
            self.assertEqual(subject.Deployment(fresh).run(), 3)
        self.assertFalse((fresh / "deployment.json").exists())
        self.assertEqual((self.root / ".cliproxy-plugin-owner").read_bytes(), marker)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.auth.exists() or self.artifact.exists())

    def test_recovery_group_context_drift_refuses_before_mutation(self):
        baseline_gid = self.config.stat().st_gid
        other_gid = baseline_gid + 1
        real_stat = Path.stat
        for context in ("effective-group", "setgid-directory"):
            with self.subTest(context=context):
                def injected(path, *args, **kwargs):
                    info = real_stat(path, *args, **kwargs)
                    fields = list(info)
                    if path == self.root:
                        fields[0] = ((fields[0] | stat.S_ISGID) if context == "setgid-directory"
                                     else fields[0] & ~stat.S_ISGID)
                        fields[5] = other_gid
                    elif path == self.config or path.name.startswith(".restore-"):
                        # Model files created in recovery group B without changing host identity.
                        fields[5] = other_gid
                    return os.stat_result(fields)
                with patch.object(subject.os, "getegid", return_value=other_gid), \
                        patch.object(Path, "stat", injected), \
                        patch.object(subject, "command", wraps=subject.command) as mutation:
                    self.assertEqual(subject.Deployment(self.state).rollback(), 1)
                mutation.assert_not_called()
                self.assertEqual(self.config.read_bytes(), self.rendered)
                self.assertTrue(self.auth.exists() and self.artifact.exists())
                self.assertEqual(self.marker(), "rollback-FAILED")
        self.assertEqual(subject.Deployment(self.state).rollback(), 0)
        self.assertEqual(self.config.stat().st_gid, baseline_gid)
        self.assert_restored()

    def test_manifest_binds_baseline_metadata_and_legacy_schema_refuses(self):
        manifest_path = self.state / "deployment.json"
        manifest = json.loads(manifest_path.read_text())
        info = self.config.stat()
        self.assertEqual(manifest["schema"], 2)
        self.assertEqual(manifest["config_metadata"],
                         {"mode": 0o600, "uid": info.st_uid, "gid": info.st_gid})
        manifest["schema"] = 1
        del manifest["config_metadata"]
        manifest_path.write_text(json.dumps(manifest))
        marker = manifest["id"] + " " + subject.digest(manifest_path) + "\n"
        (self.root / ".cliproxy-plugin-owner").write_text(marker)
        with patch.object(subject, "command", wraps=subject.command) as mutation:
            self.assertEqual(subject.main(["rollback"]), 1)
        mutation.assert_not_called()
        self.assertEqual(self.marker(), "verified")
        self.assertEqual(self.config.read_bytes(), self.rendered)
        self.assertTrue(self.auth.exists() and self.artifact.exists())

    def test_restore_temporary_group_drift_is_not_published(self):
        real_stat = Path.stat
        def injected(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path.name.startswith(".restore-"):
                fields = list(info)
                fields[5] += 1
                return os.stat_result(fields)
            return info
        with patch.object(Path, "stat", injected):
            self.assertEqual(self.deploy.rollback(), 1)
        self.assertEqual(self.config.read_bytes(), self.rendered)
        self.assertEqual(self.marker(), "rollback-FAILED")
        self.assertTrue(self.artifact.exists())

    def test_preflight_config_drift_refuses_before_service_writes(self):
        self.assertEqual(self.deploy.rollback(), 0)
        fresh = self.base / "preflight-drift"
        fresh.mkdir(mode=0o700)
        second = subject.Deployment(fresh)
        real = second.helper
        def injected(action):
            if action == "preflight":
                self.config.write_bytes(b"external config edit\n")
            return real(action)
        with patch.object(second, "helper", side_effect=injected):
            self.assertEqual(second.run(), 3)
        self.assertEqual(second.read_state(), "aborted-unchanged")
        self.assertFalse((fresh / "deployment.json").exists())
        self.assertFalse(self.auth.exists() or self.artifact.exists())
        self.assertEqual(self.config.read_bytes(), b"external config edit\n")

    def test_preapply_identity_drift_refuses_before_exclusive_copies(self):
        self.assertEqual(self.deploy.rollback(), 0)
        fresh = self.base / "preapply-drift"
        fresh.mkdir(mode=0o700)
        second = subject.Deployment(fresh)
        def injected():
            self.config.write_bytes(b"external config edit\n")
        with patch.object(second, "start_watchdog", side_effect=injected):
            self.assertEqual(second.run(), 1)
        self.assertEqual(second.read_state(), "rollback-FAILED")
        self.assertFalse(self.auth.exists() or self.artifact.exists())
        self.assertEqual(self.config.read_bytes(), b"external config edit\n")

    def test_forward_failure_returns_nonzero_even_after_successful_rollback(self):
        # Use a fresh state and restored service; the old state remains immutable.
        self.assertEqual(self.deploy.rollback(), 0)
        new_state = self.base / "second-state"
        new_state.mkdir(mode=0o700)
        second = subject.Deployment(new_state)
        real = second.helper
        def injected(action):
            if action == "verify":
                raise subprocess.CalledProcessError(1, "fixture verification")
            return real(action)
        with patch.object(second, "helper", side_effect=injected):
            self.assertEqual(second.run(), 1)
        self.assertEqual(second.read_state(), "rolledback")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.deploy.rollback(), 1)  # superseded deployment identity

    def test_run_cannot_reuse_state_or_change_saved_backup(self):
        backup = (self.state / "config.yaml.orig").read_bytes()
        self.assertEqual(subject.main(["run"]), 1)
        self.assertEqual(self.marker(), "verified")
        self.assertEqual((self.state / "config.yaml.orig").read_bytes(), backup)

    def test_shared_state_is_locked_across_service_roots(self):
        self.assertEqual(self.deploy.rollback(), 0)
        shared = self.base / "shared-state"
        shared.mkdir(mode=0o700)
        other = self.base / "other-service"
        other.mkdir(mode=0o700)
        for name in ("auths", "plugins"):
            (other / name).mkdir(mode=0o700)
        (other / "config.yaml").write_bytes(self.original)
        (other / "config.yaml").chmod(0o600)
        holder = subject.Deployment(shared)
        real = holder.helper
        def interleaved(action):
            if action == "preflight":
                before = (shared / "state").read_bytes()
                with patch.dict(os.environ, {"S": str(other), "STATE_DIR": str(shared)}):
                    self.assertEqual(subject.main(["run"]), 75)
                self.assertEqual((shared / "state").read_bytes(), before)
                self.assertEqual((other / "config.yaml").read_bytes(), self.original)
                self.assertFalse((other / ".cliproxy-plugin-owner").exists())
            return real(action)
        with patch.object(holder, "helper", side_effect=interleaved):
            self.assertEqual(holder.run(), 0)
        self.assertEqual(json.loads((shared / "deployment.json").read_text())["root"], str(self.root))

    def test_state_lock_also_guards_recovery_reads(self):
        with (self.state / ".cliproxy-plugin.lock").open("a+b") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for action in ("rollback", "watchdog"):
                with self.subTest(action=action):
                    self.assertEqual(subject.main([action]), 75)
                    self.assertEqual(self.marker(), "verified")
                    self.assertEqual(self.config.read_bytes(), self.rendered)

    def test_sticky_service_and_target_directories_are_refused(self):
        for directory in (self.root, self.auth.parent, self.artifact.parent):
            with self.subTest(directory=directory.name):
                directory.chmod(0o1777)
                try:
                    self.assertEqual(subject.main(["rollback"]), 1)
                    self.assertEqual(self.config.read_bytes(), self.rendered)
                    self.assertTrue(self.auth.exists() and self.artifact.exists())
                finally:
                    directory.chmod(0o700)

    def test_insecure_or_linked_lock_inode_is_refused(self):
        for directory in (self.root, self.state):
            lock = directory / ".cliproxy-plugin.lock"
            lock.touch(mode=0o600)
            with self.subTest(directory=directory.name, fault="permissions"):
                lock.chmod(0o666)
                try:
                    self.assertEqual(subject.main(["rollback"]), 1)
                finally:
                    lock.chmod(0o600)
            with self.subTest(directory=directory.name, fault="hardlink"):
                alias = self.base / "lock-alias"
                os.link(lock, alias)
                try:
                    self.assertEqual(subject.main(["rollback"]), 1)
                finally:
                    alias.unlink()
            self.assertEqual(self.marker(), "verified")

    def test_removal_parents_are_synced_before_success(self):
        events = []
        real_sync, real_state = subject.sync_dir, self.deploy.state
        def synced(path):
            events.append(("sync", path))
            return real_sync(path)
        def stored(value):
            events.append(("state", value))
            return real_state(value)
        with patch.object(subject, "sync_dir", side_effect=synced), \
                patch.object(self.deploy, "state", side_effect=stored):
            self.assertEqual(self.deploy.rollback(), 0)
        for parent in (self.auth.parent, self.artifact.parent):
            self.assertIn(("sync", parent), events)
            self.assertLess(events.index(("sync", parent)), events.index(("state", "rolledback")))
        self.assert_restored()

    def test_removal_directory_sync_failure_is_not_success(self):
        real = subject.sync_dir
        for parent in (self.auth.parent, self.artifact.parent):
            with self.subTest(parent=parent.name):
                def injected(path):
                    if path == parent:
                        raise OSError("injected deletion persistence failure")
                    return real(path)
                with patch.object(subject, "sync_dir", side_effect=injected):
                    self.assertEqual(self.deploy.rollback(), 1)
                self.assertEqual(self.marker(), "rollback-FAILED")

    def test_rendered_backup_fsync_failure_refuses_preparation(self):
        self.assertEqual(self.deploy.rollback(), 0)
        fresh = self.base / "fresh-state"
        fresh.mkdir(mode=0o700)
        real = subject.os.fsync
        def injected(fd):
            if Path(f"/proc/self/fd/{fd}").resolve() == fresh / "config.yaml.new":
                raise OSError("injected rendered data persistence failure")
            return real(fd)
        with patch.object(subject.os, "fsync", side_effect=injected):
            self.assertEqual(subject.Deployment(fresh).run(), 3)
        self.assertFalse((fresh / "deployment.json").exists())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.auth.exists() or self.artifact.exists())

    def test_internal_metadata_destinations_refused_before_preparation(self):
        self.assertEqual(self.deploy.rollback(), 0)
        (self.root / ".cliproxy-plugin-owner").unlink()
        for key in ("AUTH_REL", "ARTIFACT_REL"):
            for name in (".cliproxy-plugin-owner", ".cliproxy-plugin.lock"):
                with self.subTest(key=key, name=name):
                    fresh = self.base / f"{key}-{name}-state"
                    fresh.mkdir(mode=0o700)
                    with patch.dict(os.environ, {key: name}):
                        self.assertEqual(subject.Deployment(fresh).run(), 3)
                    self.assertFalse((fresh / "deployment.json").exists())
                    self.assertFalse((self.root / ".cliproxy-plugin-owner").exists())
                    self.assertFalse(self.auth.exists() or self.artifact.exists())
                    self.assertEqual(self.config.read_bytes(), self.original)

    def test_unloadable_manifest_refused_before_preparation(self):
        self.assertEqual(self.deploy.rollback(), 0)
        fresh = self.base / "long-path-state"
        fresh.mkdir(mode=0o700)
        relatives = {}
        for key, prefix in (("AUTH_REL", "auth-long"), ("ARTIFACT_REL", "artifact-long")):
            parent = self.root / prefix
            for _ in range(16):
                parent /= "é" * 90
            parent.mkdir(mode=0o700, parents=True)
            relatives[key] = str((parent / "dummy").relative_to(self.root))
        with patch.dict(os.environ, relatives):
            self.assertEqual(subject.Deployment(fresh).run(), 3)
        self.assertFalse((fresh / "deployment.json").exists())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_nonzero_restart_action_is_not_success(self):
        with patch.object(self.deploy, "healthy", return_value=False), \
                patch.object(self.deploy, "helper", side_effect=subprocess.CalledProcessError(1, "restart")):
            self.assertEqual(self.deploy.rollback(), 1)
        self.assertEqual(self.marker(), "rollback-FAILED")

    def test_cli_watchdog_exit_and_usage(self):
        self.deploy.state("rollback-FAILED")
        for args, expected in ((["watchdog"], 1), (["invalid"], 2), ([], 2)):
            result = subprocess.run(["bash", str(SOURCE / "cliproxy_plugin_deploy.sh"), *args],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, expected)

    def test_reconstructed_unsafe_trace_fools_the_same_postcondition_oracle(self):
        # Deliberately reconstructed unsafe behavior, NOT an original executable.
        def unsafe_rollback():
            # Model a failed saved-config copy and failed auth/library removals,
            # followed by healthy baseline routes and an unconditional success.
            if self.deploy.healthy():
                self.deploy.state("rolledback")
                return 0
            return 1
        self.assertEqual(unsafe_rollback(), 0)
        with self.assertRaises(subject.Refusal):
            self.deploy.postconditions()
        self.assertNotEqual(self.config.read_bytes(), self.original)
        self.assertTrue(self.auth.exists() and self.artifact.exists())


class PrerequisiteTests(unittest.TestCase):
    def test_rollback_plan_requires_python311(self):
        inventory = json.loads((SOURCE / "scripts/verify-fast-inventory.json").read_text())
        suite = next(s for s in inventory["suites"] if s["id"] == "cliproxy-plugin-rollback")
        self.assertEqual(suite["prerequisites"], ["bash", "python3>=3.11"])
        spec = importlib.util.spec_from_file_location("verify_plan", SOURCE / "scripts/verify-fast-plan.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        real_run = subprocess.run
        for version, ready in (((3, 10), False), ((3, 11), True)):
            with self.subTest(version=version):
                def injected(argv, **kwargs):
                    self.assertEqual(argv[:3], ["python3", "-I", "-c"])
                    code = f"import sys; sys.version_info = {version!r}; " + argv[3]
                    return real_run([*argv[:3], code], **kwargs)
                with patch.object(planner.subprocess, "run", side_effect=injected):
                    plan, _ = planner.build_plan({**inventory, "suites": [suite]}, SOURCE)
                prereqs = {p["name"]: p["ready"] for p in plan["suites"][0]["prerequisites"]}
                self.assertTrue(prereqs["bash"])
                self.assertEqual(prereqs["python3>=3.11"], ready)


if __name__ == "__main__":
    unittest.main(verbosity=2)
