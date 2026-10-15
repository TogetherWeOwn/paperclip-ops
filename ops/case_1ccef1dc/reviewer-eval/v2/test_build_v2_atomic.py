"""case_a896464a: build_v2.py writes every JSON/diff/report output atomically.

Offline, hermetic: temp dirs, AST reads and unittest.mock only. No network,
no credential, no model call, no real git repo.
"""
import ast
import errno
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "..", "lib")))
sys.path.insert(0, HERE)

import atomic_state as a
import build_v2 as b


def _open_write_calls(tree):
    """open() calls that truncate: a 'w' (or 'w+') in the mode position."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "open"):
            continue
        modes = [a2 for a2 in node.args[1:2]]
        modes += [kw.value for kw in node.keywords if kw.arg == "mode"]
        for m in modes:
            if isinstance(m, ast.Constant) and isinstance(m.value, str) and "w" in m.value:
                hits.append(node.lineno)
    return hits


class NoPlainOpenWriters(unittest.TestCase):
    def test_no_truncating_open_remains(self):
        src = Path(HERE, "build_v2.py").read_text()
        tree = ast.parse(src)
        self.assertEqual(_open_write_calls(tree), [])

    def test_json_dump_never_takes_an_open_handle(self):
        src = Path(HERE, "build_v2.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "dump" and isinstance(f.value, ast.Name) and f.value.id == "json":
                for arg in node.args[1:]:
                    self.assertFalse(
                        isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "open",
                        f"json.dump to open() at line {node.lineno}")

    def test_every_write_goes_through_the_atomic_helper(self):
        src = Path(HERE, "build_v2.py").read_text()
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        names = [n.func.id for n in calls]
        # apply_replace + vdir pr.diff + view pr.diff
        self.assertGreaterEqual(names.count("write_text_atomic"), 3)
        # vdir meta + vdir reviewer_view + view reviewer_view + corpus-report
        self.assertGreaterEqual(names.count("write_json_atomic"), 4)


class WriteTextAtomic(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "pr.diff"

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_round_trip_keeps_mode_and_leaves_no_temp(self):
        self.path.write_text("old\n")
        os.chmod(self.path, 0o640)
        a.write_text_atomic(self.path, "new\n")
        self.assertEqual(self.path.read_text(), "new\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(self.names(), ["pr.diff"])

    def test_read_only_dir_raises_and_preserves_previous_bytes(self):
        self.path.write_bytes(b"previous-bytes\n")
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(a.tempfile, "mkstemp", side_effect=denied):
            with self.assertRaises(PermissionError):
                a.write_text_atomic(self.path, "new\n")
        self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")
        self.assertEqual(self.names(), ["pr.diff"])

    def test_enospc_at_fsync_or_replace_preserves_previous_bytes(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                self.path.write_bytes(b"previous-bytes\n")
                failure = OSError(errno.ENOSPC, "No space left on device")
                with mock.patch.object(a.os, operation, side_effect=failure):
                    with self.assertRaises(OSError):
                        a.write_text_atomic(self.path, "new\n")
                self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")
                self.assertEqual(self.names(), ["pr.diff"])


class BuilderWiring(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_apply_replace_routes_through_the_helper(self):
        target = self.dir / "x.ts"
        target.write_text("a > 0 && a <= 9\n")
        with mock.patch.object(b, "write_text_atomic", wraps=b.write_text_atomic) as w:
            line = b.apply_replace(str(self.dir), "x.ts", "a > 0 && a <= 9", "a >= 0 && a <= 9", "S01")
        self.assertEqual(line, 1)
        self.assertEqual(w.call_count, 1)
        self.assertEqual(target.read_text(), "a >= 0 && a <= 9\n")

    def test_failing_apply_replace_leaves_previous_bytes(self):
        target = self.dir / "x.ts"
        target.write_bytes(b"previous-bytes: a > 0\n")
        failure = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(b, "write_text_atomic", side_effect=failure):
            with self.assertRaises(OSError):
                b.apply_replace(str(self.dir), "x.ts", "a > 0", "a >= 0", "S01")
        self.assertEqual(target.read_bytes(), b"previous-bytes: a > 0\n")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["x.ts"])


if __name__ == "__main__":
    unittest.main()
