"""Offline tests for arm_import.py (case_38a65c30).

All arm JSON outputs must go through ops/lib atomic_state.write_json_atomic,
so a failing write leaves the previous output byte-identical and the command
exits non-zero instead of leaving a truncated result that reads as data.
Hermetic: temp dirs only, no network, credential or model call.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arm_import as ai


def fixtures(root, raw_text='{"verdict": "CHANGES", "findings": []}'):
    mapping = {"r01": "7-T1", "r02": "7-clean"}
    map_path = os.path.join(root, "amap.json")
    with open(map_path, "w") as handle:
        handle.write(json.dumps(mapping))
    raw = os.path.join(root, "raw")
    os.makedirs(raw)
    with open(os.path.join(raw, "r01.json"), "w") as handle:
        handle.write("prose before " + raw_text)
    with open(os.path.join(raw, "r02.json"), "w") as handle:
        handle.write("not json at all")
    usage = {"r01": {"input_tokens": 10, "output_tokens": 5, "seconds": 1}}
    usage_path = os.path.join(root, "usage.json")
    with open(usage_path, "w") as handle:
        handle.write(json.dumps(usage))
    return map_path, raw, usage_path


def run_main(map_path, raw, out, arm="a", usage=None):
    argv = ["arm_import.py", "--map", map_path, "--raw", raw,
            "--arm", arm, "--out", out]
    if usage:
        argv += ["--usage", usage]
    with mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
        rc = ai.main()
    return rc, stdout.getvalue().strip()


class ImportSemantics(unittest.TestCase):
    def test_verdicts_usage_join_and_invalid_count_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            map_path, raw, usage_path = fixtures(root)
            out = os.path.join(root, "imp")
            rc, printed = run_main(map_path, raw, out, usage=usage_path)
            self.assertEqual(rc, 0)
            self.assertEqual(printed, "1")
            changed = json.load(open(os.path.join(out, "a", "7-T1.json")))
            self.assertEqual(changed["verdict"], "CHANGES")
            self.assertEqual(len(changed["findings"]), 0)
            self.assertEqual(changed["usage"], {"input_tokens": 10, "output_tokens": 5, "seconds": 1})
            invalid = json.load(open(os.path.join(out, "a", "7-clean.json")))
            self.assertTrue(invalid["invalid"])
            self.assertEqual(invalid["verdict"], "APPROVE")
            self.assertEqual(invalid["usage"], {})


class FailClosed(unittest.TestCase):
    def test_enospc_on_replace_preserves_previous_bytes(self):
        # Mutant-killer: with a plain open(..., "w") writer restored, main
        # never calls os.replace, so it succeeds and assertRaises goes red.
        with tempfile.TemporaryDirectory() as root:
            map_path, raw, _ = fixtures(root)
            out = os.path.join(root, "imp")
            dest = os.path.join(out, "a")
            os.makedirs(dest)
            previous = b'{"old": true}'
            target = os.path.join(dest, "7-T1.json")
            with open(target, "wb") as handle:
                handle.write(previous)
            before = sorted(os.listdir(dest))
            with mock.patch.object(ai.os, "replace",
                                   side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    run_main(map_path, raw, out)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), previous)
            self.assertEqual(sorted(os.listdir(dest)), before)

    def test_readonly_dest_dir_exits_nonzero_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            map_path, raw, _ = fixtures(root)
            out = os.path.join(root, "imp")
            dest = os.path.join(out, "a")
            os.makedirs(dest)
            previous = b'{"old": true}'
            target = os.path.join(dest, "7-T1.json")
            with open(target, "wb") as handle:
                handle.write(previous)
            os.chmod(dest, 0o555)
            try:
                proc = subprocess.run(
                    [sys.executable, ai.__file__, "--map", map_path,
                     "--raw", raw, "--arm", "a", "--out", out],
                    capture_output=True, text=True, timeout=120)
            finally:
                os.chmod(dest, 0o755)
            self.assertNotEqual(proc.returncode, 0)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), previous)
            self.assertEqual(sorted(os.listdir(dest)), ["7-T1.json"])


if __name__ == "__main__":
    unittest.main()
