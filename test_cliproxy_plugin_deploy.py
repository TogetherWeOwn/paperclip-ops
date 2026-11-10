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

    def test_watchdog_propagates_rollback_failure_and_rechecks_repeat(self):
        self.deploy.state("loading")
        with patch.object(self.deploy, "restore_config", side_effect=OSError("copy failed")):
            self.assertEqual(self.deploy.watchdog(), 1)
        self.assertEqual(self.marker(), "rollback-FAILED")
        self.assertEqual(self.deploy.watchdog(), 1)
        self.assertEqual(self.deploy.rollback(), 0)
        self.assertEqual(self.deploy.watchdog(), 0)
        self.config.write_bytes(b"later config")
        self.assertEqual(self.deploy.watchdog(), 1)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
