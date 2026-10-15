"""Offline atomic-write suite for merge_survival.write_verdicts — case_f8ea5fd3.

WHAT THIS PROTECTS. merge_survival.py folded survival rows into each seeded
``<variant>/meta.json`` and ``survival-summary.json`` with
``json.dump(data, open(path, "w"))``: a read-modify-write (the file is read
first), so an ENOSPC mid-write destroys the verdicts it just read and leaves
a truncated file that reads as data. Both writes now go through
``ops/lib/atomic_state.write_json_atomic`` (same-dir temp, fsync, atomic
rename) and a failing write names the path and errno on stderr and exits
non-zero (SystemExit 2).

THE TRAP IN TESTING. A suite that only checks the happy path stays green
when the atomic call is reverted to a plain open, so every assertion here
is paired with a failure-injection case: ENOSPC at fsync and at replace
must raise SystemExit(2) with the previous bytes intact and no temp file
left behind. A plain ``open(path, "w")`` never reaches os.replace, so the
reverted line fails the raises assertion (and would already have truncated
the file). The happy path proves the verdicts still land as valid JSON,
and the through-main() cases prove both write sites route through the
helper. An AST check pins the file shut: no truncating open() and no
json.dump-to-open-handle may return.

Offline and credential-free by construction: tempfile dirs, fabricated
corpus fixtures, ENOSPC injected with mocks. No vitest, no DB, no network.
"""
import ast
import contextlib
import errno
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "..", "lib")))
sys.path.insert(0, HERE)

import atomic_state  # noqa: E402

import merge_survival as m  # noqa: E402


def make_corpus(root):
    """Minimal corpus: corpus-report with one seeded + one clean variant."""
    corpus = os.path.join(root, "corpus")
    os.makedirs(os.path.join(corpus, "7-S01"))
    os.makedirs(os.path.join(corpus, "7-clean"))
    with open(os.path.join(corpus, "corpus-report.json"), "w") as fh:
        json.dump([{"variant": "7-S01", "seed": "S01"},
                   {"variant": "7-clean"}], fh)
    with open(os.path.join(corpus, "7-S01", "meta.json"), "w") as fh:
        json.dump({"variant": "7-S01", "seeded": True}, fh)
    with open(os.path.join(corpus, "7-clean", "meta.json"), "w") as fh:
        json.dump({"variant": "7-clean", "seeded": False}, fh)
    nodb = os.path.join(root, "nodb.json")
    with open(nodb, "w") as fh:
        json.dump([{"variant": "7-S01", "seed": "S01", "exit": 0,
                    "ci_silent": True}], fh)
    return corpus, nodb


def run_main(corpus, nodb, db=None):
    argv = ["merge_survival.py", "--corpus", corpus, "--nodb", nodb]
    if db:
        argv += ["--db", db]
    with mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
        rc = m.main()
    return rc, stdout.getvalue().strip()


def _open_write_calls(tree):
    """open() calls that truncate: a 'w' (or 'w+') in the mode position."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "open"):
            continue
        modes = list(node.args[1:2])
        modes += [kw.value for kw in node.keywords if kw.arg == "mode"]
        for mode in modes:
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
                    and "w" in mode.value:
                hits.append(node.lineno)
    return hits


class NoPlainOpenWriters(unittest.TestCase):
    def test_no_truncating_open_remains(self):
        src = Path(HERE, "merge_survival.py").read_text()
        self.assertEqual(_open_write_calls(ast.parse(src)), [])

    def test_json_dump_never_takes_an_open_handle(self):
        tree = ast.parse(Path(HERE, "merge_survival.py").read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "dump" \
                    and isinstance(func.value, ast.Name) \
                    and func.value.id == "json":
                for arg in node.args[1:]:
                    self.assertFalse(
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Name)
                        and arg.func.id == "open",
                        f"json.dump to open() at line {node.lineno}")

    def test_both_write_sites_route_through_the_helper(self):
        tree = ast.parse(Path(HERE, "merge_survival.py").read_text())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        names = [n.func.id for n in calls]
        # per-variant meta.json + survival-summary.json
        self.assertGreaterEqual(names.count("write_verdicts"), 2)


class WriteVerdicts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "meta.json"

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_happy_path_writes_valid_json_and_leaves_no_temp(self):
        self.path.write_bytes(b'{"old": true}\n')
        m.write_verdicts(str(self.path), {"ci_silent": True})
        self.assertEqual(json.loads(self.path.read_text()),
                         {"ci_silent": True})
        self.assertEqual(self.names(), ["meta.json"])

    def test_enospc_at_fsync_or_replace_exits_nonzero_and_preserves_bytes(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                self.path.write_bytes(b"previous-bytes\n")
                failure = OSError(errno.ENOSPC, "No space left on device")
                err = io.StringIO()
                with mock.patch.object(atomic_state.os, operation,
                                       side_effect=failure):
                    with contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as cm:
                            m.write_verdicts(str(self.path), {"ci_silent": True})
                self.assertEqual(cm.exception.code, 2)
                self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")
                self.assertEqual(self.names(), ["meta.json"])
                self.assertIn("meta.json", err.getvalue())

    def test_failing_write_names_path_with_errno(self):
        self.path.write_bytes(b"previous-bytes\n")
        failure = OSError(errno.ENOSPC, "No space left on device")
        err = io.StringIO()
        with mock.patch.object(atomic_state.os, "replace",
                               side_effect=failure):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    m.write_verdicts(str(self.path), {"ci_silent": True})
        self.assertIn(str(self.path), err.getvalue())
        self.assertIn(str(errno.ENOSPC), err.getvalue())


class ThroughMain(unittest.TestCase):
    def test_happy_path_folds_verdicts_and_summary(self):
        with tempfile.TemporaryDirectory() as root:
            corpus, nodb = make_corpus(root)
            rc, printed = run_main(corpus, nodb)
            self.assertEqual(rc, 0)
            self.assertEqual(printed, "SILENT 1/1")
            meta = json.loads(Path(corpus, "7-S01", "meta.json").read_text())
            self.assertTrue(meta["ci_silent"])
            self.assertTrue(meta["survival"]["no_database"]["silent"])
            summary = json.loads(Path(corpus, "survival-summary.json").read_text())
            self.assertEqual(len(summary), 1)
            self.assertTrue(summary[0]["ci_silent"])

    def test_enospc_at_replace_exits_nonzero_and_preserves_meta_bytes(self):
        # Mutant-killer: with a plain open(..., "w") writer restored, main
        # never calls os.replace, so it succeeds and assertRaises goes red —
        # and the plain open would already have truncated the verdicts.
        with tempfile.TemporaryDirectory() as root:
            corpus, nodb = make_corpus(root)
            target = os.path.join(corpus, "7-S01", "meta.json")
            previous = Path(target).read_bytes()
            failure = OSError(errno.ENOSPC, "No space left on device")
            with mock.patch.object(atomic_state.os, "replace",
                                   side_effect=failure):
                with self.assertRaises(SystemExit) as cm:
                    run_main(corpus, nodb)
            self.assertEqual(cm.exception.code, 2)
            self.assertEqual(Path(target).read_bytes(), previous)

    def test_read_only_variant_dir_exits_nonzero_and_preserves_bytes(self):
        # The per-variant temp file lives beside meta.json (same-dir temp),
        # so the read-only dir that matters is the variant dir, not the
        # corpus root: the first verdict write must fail before any byte
        # is replaced, and no summary may appear either.
        import subprocess
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads every directory; chmod cannot block it")
        with tempfile.TemporaryDirectory() as root:
            corpus, nodb = make_corpus(root)
            variant_dir = os.path.join(corpus, "7-S01")
            target = os.path.join(variant_dir, "meta.json")
            with open(target, "rb") as fh:
                previous = fh.read()
            os.chmod(variant_dir, 0o555)
            try:
                proc = subprocess.run(
                    [sys.executable, m.__file__, "--corpus", corpus,
                     "--nodb", nodb],
                    capture_output=True, text=True, timeout=120)
            finally:
                os.chmod(variant_dir, 0o755)
            self.assertNotEqual(proc.returncode, 0)
            with open(target, "rb") as fh:
                self.assertEqual(fh.read(), previous)
            self.assertFalse(
                os.path.exists(os.path.join(corpus, "survival-summary.json")))


if __name__ == "__main__":
    unittest.main()
