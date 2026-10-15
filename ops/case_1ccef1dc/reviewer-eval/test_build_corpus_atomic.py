"""Offline guard for case_6c091838: v1 corpus outputs are written atomically.

A truncated corpus JSON, diff or report reads as data, so every write in
build_corpus.py must go through ops/lib/atomic_state.py and a failing write
must leave the previous bytes identical while raising (non-zero exit).

Stdlib only, hermetic: ENOSPC is injected with mocks so the suite holds for
any uid, including root (a chmod-based read-only dir would not stop root).
"""
import errno
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_corpus

HERE = Path(__file__).resolve().parent


class NoPlainOpenWriters(unittest.TestCase):
    def test_no_write_mode_open_remains(self):
        source = (HERE / 'build_corpus.py').read_text()
        writers = re.findall(r"open\(.*[\"']w[\"']", source)
        self.assertEqual(writers, [])

    def test_module_writes_through_the_atomic_helper(self):
        import atomic_state
        self.assertIs(build_corpus.write_json_atomic, atomic_state.write_json_atomic)
        self.assertIs(build_corpus.write_text_atomic, atomic_state.write_text_atomic)


class FailingWritesKeepPreviousBytes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_apply_seed_replaces_exactly_once(self):
        target = self.dir / 'seeded.py'
        target.write_text('a = 1\nb = 2\n')
        seed = {'id': 's1', 'file': 'seeded.py', 'old': 'a = 1', 'new': 'a = 2'}
        line = build_corpus.apply_seed(str(self.dir), seed)
        self.assertEqual(line, 1)
        self.assertEqual(target.read_text(), 'a = 2\nb = 2\n')
        self.assertEqual(self.names(), ['seeded.py'])

    def test_failing_seed_write_preserves_bytes_and_raises(self):
        target = self.dir / 'seeded.py'
        target.write_bytes(b'a = 1\n')
        seed = {'id': 's1', 'file': 'seeded.py', 'old': 'a = 1', 'new': 'a = 2'}
        failure = OSError(errno.ENOSPC, 'No space left on device')
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation), \
                    mock.patch.object(os, operation, side_effect=failure):
                with self.assertRaises(OSError):
                    build_corpus.apply_seed(str(self.dir), seed)
            self.assertEqual(target.read_bytes(), b'a = 1\n')
            self.assertEqual(self.names(), ['seeded.py'])

    def test_failing_report_write_preserves_bytes_and_raises(self):
        report = self.dir / 'corpus-report.json'
        report.write_bytes(b'[{"variant": "1-clean"}]\n')
        failure = OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(os, 'replace', side_effect=failure):
            with self.assertRaises(OSError):
                build_corpus.write_json_atomic(str(report), [{'variant': '1-clean', 'seed': 's1'}])
        self.assertEqual(report.read_bytes(), b'[{"variant": "1-clean"}]\n')
        self.assertEqual(self.names(), ['corpus-report.json'])


if __name__ == '__main__':
    unittest.main()
