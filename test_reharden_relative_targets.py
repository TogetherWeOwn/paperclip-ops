#!/usr/bin/env python3
"""Offline unit regressions for container-relative reharden symlink targets.

Only the docker write-denial probes are stubbed. The script walks real fixture
symlinks and modes; no host, daemon, credentials or container namespace is used.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REHARDEN = Path(__file__).resolve().parent / "paperclip-upgrade/post-restart-reharden.sh"


class RelativeTargetTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("TMPDIR")
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        for name in (".local/bin", ".local/share", "plugin-packages-root", "sub/deep"):
            path = self.data / name
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o755)
        for name in (".gitconfig", ".local/share/tool", "evil"):
            path = self.data / name
            path.write_text("fixture\n")
            path.chmod(0o644)
        self.link = self.data / ".local/bin/tool"
        self.docker = self.root / "docker"
        self.docker.write_text('#!/bin/sh\n[ "$1" = exec ] || exit 99\nprintf "DENIED\\n"\n')
        self.docker.chmod(0o700)
        self.env = {
            **os.environ,
            "PAPERCLIP_UPGRADE_EXPECT_OWNER": f"{os.getuid()}:{os.getgid()}",
            "PAPERCLIP_UPGRADE_REQUIRE_CARGO_POOL": "0",
            "DOCKER": str(self.docker),
        }
        self.env.pop("BASH_ENV", None)

    def run_gate(self, data=None):
        return subprocess.run(
            ["bash", str(REHARDEN)],
            cwd=self.root,
            env={**self.env, "PAPERCLIP_DATA": str(self.data) if data is None else data},
            text=True, capture_output=True, timeout=10,
        )

    def assert_refused(self, needle, data=None):
        result = self.run_gate(data)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(needle, result.stdout)

    def test_plain_relative_target_stays_accepted(self):
        self.link.symlink_to("../share/tool")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_relative_escape_does_not_judge_safe_host_twin(self):
        # Host: data/.gitconfig (safe). Container: /data/.gitconfig (outside mount).
        self.link.symlink_to("../../../data/.gitconfig")
        self.assert_refused("escapes the container mount")

    def test_second_relative_hop_cannot_escape_mount(self):
        (self.data / "mid").symlink_to("../data/.gitconfig")
        self.link.symlink_to("/paperclip/mid")
        self.assert_refused("escapes the container mount")

    def test_relative_hop_at_mount_root_stays_accepted(self):
        (self.data / "mid").symlink_to(".gitconfig")
        self.link.symlink_to("/paperclip/mid")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_relative_dotdot_after_symlink_fails_closed(self):
        (self.data / "alias").symlink_to("sub/deep")
        target = self.data / "sub/evil"
        target.write_text("writable\n")
        target.chmod(0o666)
        self.link.symlink_to("../../alias/../evil")
        self.assert_refused("after a non-directory")

    def test_relative_directory_component_hop_is_remapped(self):
        (self.data / "mid").symlink_to(".local/share")
        self.link.symlink_to("/paperclip/mid/tool")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_data_dir_spellings_cannot_bypass_relative_gate(self):
        (self.root / "parent-alias").symlink_to(self.root, target_is_directory=True)
        self.link.symlink_to("../../../data/.gitconfig")
        for data in ("data", "./data", str(self.data) + "/", str(self.root / "parent-alias/data")):
            with self.subTest(data=data):
                self.assert_refused("escapes the container mount", data)

    def test_data_dir_spellings_keep_valid_targets_accepted(self):
        (self.root / "parent-alias").symlink_to(self.root, target_is_directory=True)
        self.link.symlink_to("../share/tool")
        for data in ("data", "./data", str(self.data) + "/", str(self.root / "parent-alias/data")):
            with self.subTest(data=data):
                result = self.run_gate(data)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
