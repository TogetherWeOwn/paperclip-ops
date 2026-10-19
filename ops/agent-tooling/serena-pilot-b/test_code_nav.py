#!/usr/bin/env python3
"""Fast unit tests for code_nav.py (Pilot B). Hermetic: temp dirs only, no
network, no Serena server. Wired into scripts/verify-fast.sh."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import code_nav


class RootRelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "src"))
        with open(os.path.join(self.root, "src", "a.ts"), "w") as f:
            f.write("x")
        self._cwd = os.getcwd()
        os.chdir(os.path.join(self.root, "src"))

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def test_cwd_relative(self):
        self.assertEqual(code_nav._to_root_rel(self.root, "a.ts"), "src/a.ts")

    def test_repo_relative(self):
        self.assertEqual(code_nav._to_root_rel(self.root, "src/a.ts"), "src/a.ts")

    def test_cwd_wins_over_root(self):
        with open(os.path.join(self.root, "a.ts"), "w") as f:
            f.write("root")
        self.assertEqual(code_nav._to_root_rel(self.root, "a.ts"), "src/a.ts")

    def test_absolute(self):
        self.assertEqual(
            code_nav._to_root_rel(self.root, os.path.join(self.root, "src", "a.ts")),
            "src/a.ts",
        )

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(SystemExit) as c:
            code_nav._to_root_rel(self.root, "../../etc/hostname")
        self.assertEqual(c.exception.code, 2)

    def test_absolute_escape_rejected(self):
        with self.assertRaises(SystemExit) as c:
            code_nav._to_root_rel(self.root, "/etc/hostname")
        self.assertEqual(c.exception.code, 2)

    def test_missing_rejected(self):
        with self.assertRaises(SystemExit) as c:
            code_nav._to_root_rel(self.root, "nope.ts")
        self.assertEqual(c.exception.code, 2)


class ServerArgvTest(unittest.TestCase):
    """The spec-critical flags must not silently drift: without
    --agent-interface tools the 3-tool allowlist does not apply (2.0.0
    defaults to REPL), and without --project-from-cwd the server cannot see
    the caller's worktree."""

    def test_required_flags_present(self):
        tail = code_nav.SERENA_ARGV_TAIL
        for flag in ("--project-from-cwd", "--agent-interface", "--mode",
                     "--enable-web-dashboard", "--open-web-dashboard",
                     "--context", "--transport"):
            self.assertIn(flag, tail)

    def test_flag_values(self):
        tail = code_nav.SERENA_ARGV_TAIL
        self.assertEqual(tail[tail.index("--agent-interface") + 1], "tools")
        self.assertEqual(tail[tail.index("--mode") + 1], "no-memories")
        self.assertEqual(tail[tail.index("--enable-web-dashboard") + 1], "false")
        self.assertEqual(tail[tail.index("--open-web-dashboard") + 1], "false")
        self.assertEqual(tail[tail.index("--transport") + 1], "stdio")


class ConfigTemplateTest(unittest.TestCase):
    """The seeded global config must keep the two load-bearing lines: an
    empty `projects` key (Serena fatals without it) and the data-folder
    redirect (without it the first call dirties the repo with .serena/)."""

    def test_template_pins_load_bearing_keys(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "serena-config.pilot.yml")) as f:
            text = f.read()
        self.assertIn("projects: []", text)
        self.assertIn(
            'project_serena_folder_location: "/tmp/serena-home/projects/$projectFolderName"',
            text,
        )


class ChildEnvTest(unittest.TestCase):
    def test_telemetry_opt_out_is_lowercase_false(self):
        # Serena's agent.py compares against exactly "false".
        with mock.patch.dict(os.environ, {"HOME": "/root"}, clear=True):
            env = code_nav._child_env("/tmp/serena-home")
        self.assertEqual(env["SERENA_USAGE_REPORTING"], "false")

    def test_home_redirected_from_world_writable_paperclip(self):
        with mock.patch.dict(os.environ, {"HOME": "/paperclip"}, clear=True):
            env = code_nav._child_env("/tmp/serena-home")
        self.assertEqual(env["HOME"], "/tmp/serena-home")

    def test_home_preserved_elsewhere(self):
        with mock.patch.dict(os.environ, {"HOME": "/root"}, clear=True):
            env = code_nav._child_env("/tmp/serena-home")
        self.assertEqual(env["HOME"], "/root")


if __name__ == "__main__":
    unittest.main()
