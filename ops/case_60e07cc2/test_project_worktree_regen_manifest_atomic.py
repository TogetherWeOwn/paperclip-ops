#!/usr/bin/env python3
# ===========================================================================
# test_project_worktree_regen_manifest_atomic.py — offline tests for the
# atomic manifest write (case_a0e409f4).
#
# The manifest is the evidence of what the pruner removed; under ENOSPC a
# truncation reads as data. These tests pin that write_manifest goes through
# ops/lib/atomic_state (tmp + fsync + os.replace) and that a failing write
# fails closed: previous manifest bytes identical, non-zero exit via
# PruneError, and the in-memory summary still printed so the pass leaves
# evidence.
#
# Deterministic: no network, no credentials, throwaway dirs only.
#
#   python3 ops/case_60e07cc2/test_project_worktree_regen_manifest_atomic.py
# ===========================================================================
import contextlib
import inspect
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import project_worktree_regen_prune as tool  # noqa: E402
from project_worktree_regen_prune import (  # noqa: E402
    PruneError,
    parse_argv,
    run_tool,
    write_manifest,
)


class AtomicWriteTests(unittest.TestCase):
    # Kills: a plain open(path, "w") that truncates the manifest before the
    # bytes exist (under ENOSPC the truncation reads as data).

    def test_write_goes_through_atomic_state(self):
        source = inspect.getsource(write_manifest)
        self.assertIn("write_json_atomic", source)
        self.assertNotIn('open(path', source)

    def test_manifest_is_parseable(self):
        with tempfile.TemporaryDirectory() as out:
            payload = {"stamp": "s", "summary": {"n": 1}, "results": []}
            path = write_manifest(out, payload, "20261010T000000Z")
            with open(path) as handle:
                self.assertEqual(json.load(handle)["summary"], {"n": 1})

    def test_replace_failure_preserves_previous_bytes(self):
        # Kills: truncating the old manifest before the new bytes land.
        with tempfile.TemporaryDirectory() as out:
            stamp = "20261010T000000Z"
            path = os.path.join(out, "project-regen-%s.json" % stamp)
            payload = {"stamp": stamp, "summary": {"n": 2}, "results": []}
            self.assertEqual(write_manifest(out, payload, stamp), path)
            previous = b"previous manifest bytes\n"
            with open(path, "wb") as handle:
                handle.write(previous)
            with mock.patch.object(tool.os, "replace",
                                   side_effect=OSError(28, "No space")):
                with self.assertRaises(OSError):
                    write_manifest(out, payload, stamp)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), previous)
            self.assertEqual(os.listdir(out), [os.path.basename(path)])

    def test_read_only_manifest_dir_fails_loudly(self):
        # A failing write must raise, never silently skip the manifest.
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permission bits")
        with tempfile.TemporaryDirectory() as root:
            out = os.path.join(root, "manifests")
            os.makedirs(out)
            os.chmod(out, stat.S_IRUSR | stat.S_IXUSR)
            try:
                with self.assertRaises(OSError):
                    write_manifest(out, {"a": 1}, "20261010T000000Z")
            finally:
                os.chmod(out, 0o700)

    def test_run_tool_manifest_failure_prints_summary_and_raises(self):
        # Fails closed: summary evidence on stdout, PruneError (exit 2 in
        # main) carrying the manifest path and errno.
        with tempfile.TemporaryDirectory() as root:
            out = os.path.join(root, "manifests")
            opts = parse_argv(["--projects-root", root,
                               "--manifest-dir", out])
            stdout = io.StringIO()
            with mock.patch.object(tool, "build_context", return_value={}), \
                    mock.patch.object(tool, "enumerate_candidates",
                                      return_value=([], [])), \
                    mock.patch.object(tool, "write_manifest",
                                      side_effect=OSError(28, "No space")), \
                    contextlib.redirect_stdout(stdout):
                with self.assertRaises(PruneError) as raised:
                    run_tool(opts, None, {}, clock=lambda: 1000)
            summary = json.loads(stdout.getvalue())
            self.assertIn("took_s", summary)
            self.assertIn(out, str(raised.exception))
            self.assertIn("errno=28", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
