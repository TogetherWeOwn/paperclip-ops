"""Offline atomic-write suite for survival_all.write_report — case_0d0fab73.

WHAT THIS PROTECTS. survival_all.py persisted its eval report with
``json.dump(out, open(path, "w"))``: an ENOSPC mid-write leaves a truncated
survival-report.json that reads as data. The writer now goes through
``ops/lib/atomic_state.write_json_atomic`` (same-dir temp, fsync, atomic
rename) and a failing write names the path on stderr and exits non-zero.

THE TRAP IN TESTING. A suite that only checks the happy path stays green
when the atomic call is reverted to a plain open, so every assertion here
is paired with a failure-injection case: ENOSPC at fsync and at replace
must raise SystemExit(2) with the previous bytes intact and no temp file
left behind. A plain ``open(path, "w")`` never reaches os.replace, so the
reverted line fails the raises assertion (and would already have truncated
the file). The happy path proves the report still lands as valid JSON.

Offline and credential-free by construction: tempfile dirs, fabricated
report rows, ENOSPC injected with mocks. No vitest, no DB, no network.
"""

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "lib"))

import atomic_state  # noqa: E402

import survival_all  # noqa: E402

ROWS = [{"variant": "114-S15", "seed": "s1", "file": "a.ts",
         "exit": 0, "ci_silent": True, "tail": "ok"}]


class WriteReport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "survival-report.json"

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_happy_path_writes_valid_json_and_leaves_no_temp(self):
        self.path.write_bytes(b'{"old": true}\n')
        survival_all.write_report(str(self.path), ROWS)
        self.assertEqual(json.loads(self.path.read_text()), ROWS)
        self.assertEqual(self.names(), ["survival-report.json"])

    def test_enospc_at_fsync_or_replace_exits_nonzero_and_preserves_bytes(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                self.path.write_bytes(b'previous-bytes\n')
                failure = OSError(errno.ENOSPC, "No space left on device")
                err = io.StringIO()
                with mock.patch.object(atomic_state.os, operation,
                                        side_effect=failure):
                    with contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as cm:
                            survival_all.write_report(str(self.path), ROWS)
                self.assertEqual(cm.exception.code, 2)
                self.assertEqual(self.path.read_bytes(), b"previous-bytes\n")
                self.assertEqual(self.names(), ["survival-report.json"])
                self.assertIn("survival-report.json", err.getvalue())

    def test_failing_write_names_path_with_errno(self):
        self.path.write_bytes(b"previous-bytes\n")
        failure = OSError(errno.ENOSPC, "No space left on device")
        err = io.StringIO()
        with mock.patch.object(atomic_state.os, "replace",
                                side_effect=failure):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    survival_all.write_report(str(self.path), ROWS)
        self.assertIn(str(self.path), err.getvalue())
        self.assertIn(str(errno.ENOSPC), err.getvalue())

    def make_corpus(self, parent):
        """Fabricated single-variant corpus dir driving main()'s call site."""
        corpus = parent / "corpus"
        corpus.mkdir()
        (corpus / "corpus-report.json").write_text(
            json.dumps([{"variant": "V1", "seed": "s1"}]))
        vdir = corpus / "V1"
        (vdir / "tree").mkdir(parents=True)
        (vdir / "meta.json").write_text(
            json.dumps({"truth": {"file": "a.ts"}, "pr": 1}))
        return corpus

    def test_main_happy_path_writes_valid_json_and_leaves_no_temp(self):
        corpus = self.make_corpus(self.dir)
        out_path = corpus / "survival-report.json"
        out_path.write_bytes(b'{"old": true}\n')
        with mock.patch.object(survival_all, "vitest_related",
                               return_value=(0, "ok")):
            with mock.patch.object(sys, "argv",
                                   ["survival_all.py", "--dir", str(corpus)]):
                survival_all.main()
        rows = json.loads(out_path.read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["variant"], "V1")
        self.assertTrue(rows[0]["ci_silent"])
        self.assertEqual(sorted(p.name for p in corpus.iterdir()),
                         ["V1", "corpus-report.json", "survival-report.json"])

    def test_main_write_failure_preserves_bytes_and_exits_nonzero(self):
        # Covers main()'s call site (survival_all.py:109): a revert to plain
        # open() bypasses os.replace, so the mocked failure never fires,
        # main() exits 0, and this test goes red.
        corpus = self.make_corpus(self.dir)
        out_path = corpus / "survival-report.json"
        out_path.write_bytes(b"previous-bytes\n")
        failure = OSError(errno.ENOSPC, "No space left on device")
        err = io.StringIO()
        with mock.patch.object(survival_all, "vitest_related",
                               return_value=(0, "ok")):
            with mock.patch.object(atomic_state.os, "replace",
                                   side_effect=failure):
                with contextlib.redirect_stderr(err):
                    with mock.patch.object(sys, "argv",
                                           ["survival_all.py", "--dir",
                                            str(corpus)]):
                        with self.assertRaises(SystemExit) as cm:
                            survival_all.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(out_path.read_bytes(), b"previous-bytes\n")
        self.assertIn("survival-report.json", err.getvalue())


if __name__ == "__main__":
    unittest.main()
