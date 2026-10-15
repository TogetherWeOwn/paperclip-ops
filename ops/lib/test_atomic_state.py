import errno
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atomic_state as a

PROV = {'source': 'fixture', 'retrieved_at': '2026-10-09T00:00:00Z', 'complete': True}


def shape(d):
    if not isinstance(d, dict) or set(d) != {'n'} or not isinstance(d['n'], int):
        raise ValueError('expected {"n": int}')


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / 'state.json'

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())


class Load(Base):
    def test_missing_file_fails_closed(self):
        with self.assertRaises(a.StateError):
            a.load(self.path, shape)

    def test_empty_file_fails_closed(self):
        self.path.write_bytes(b'')
        with self.assertRaises(a.StateError):
            a.load(self.path, shape)

    def test_truncated_json_fails_closed(self):
        self.path.write_text('{"n": 1')
        with self.assertRaises(a.StateError):
            a.load(self.path, shape)

    def test_wrong_shape_fails_closed(self):
        self.path.write_text('{"n": "one"}')
        with self.assertRaises(a.StateError):
            a.load(self.path, shape)


class Save(Base):
    def test_round_trip_keeps_mode_and_leaves_no_temp(self):
        self.path.write_text('{"n": 1}')
        os.chmod(self.path, 0o640)
        a.save(self.path, {'n': 2}, shape)
        self.assertEqual(a.load(self.path, shape), {'n': 2})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(self.names(), ['state.json'])

    def test_enospc_during_fsync_keeps_previous_state(self):
        self.path.write_text('{"n": 1}')
        full = OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(a.os, 'fsync', side_effect=full):
            with self.assertRaises(OSError):
                a.save(self.path, {'n': 2}, shape)
        self.assertEqual(a.load(self.path, shape), {'n': 1})
        self.assertEqual(self.names(), ['state.json'])

    def test_interrupted_replace_keeps_previous_state(self):
        self.path.write_text('{"n": 1}')
        with mock.patch.object(a.os, 'replace', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                a.save(self.path, {'n': 2}, shape)
        self.assertEqual(a.load(self.path, shape), {'n': 1})
        self.assertEqual(self.names(), ['state.json'])

    def test_invalid_data_is_never_written(self):
        self.path.write_text('{"n": 1}')
        with self.assertRaises(ValueError):
            a.save(self.path, {'n': 'two'}, shape)
        self.assertEqual(a.load(self.path, shape), {'n': 1})
        self.assertEqual(self.names(), ['state.json'])


class WriteJsonAtomic(Base):
    def test_round_trip_is_parseable_and_atomic(self):
        self.path.write_bytes(b'{"old": true}')
        a.write_json_atomic(self.path, {'n': 2})
        self.assertEqual(json.loads(self.path.read_text()), {'n': 2})
        self.assertEqual(self.names(), ['state.json'])

    def test_enospc_during_write_preserves_previous_bytes(self):
        self.path.write_bytes(b'previous-bytes\n')
        real_fdopen = a.os.fdopen

        class FailingWriter:
            def __init__(self, fd):
                self.handle = real_fdopen(fd, 'wb')

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, _payload):
                raise OSError(errno.ENOSPC, 'No space left on device')

            def flush(self):
                self.handle.flush()

            def fileno(self):
                return self.handle.fileno()

        with mock.patch.object(a.os, 'fdopen', side_effect=lambda fd, _mode: FailingWriter(fd)):
            with self.assertRaises(OSError):
                a.write_json_atomic(self.path, {'n': 2})
        self.assertEqual(self.path.read_bytes(), b'previous-bytes\n')
        self.assertEqual(self.names(), ['state.json'])

    def test_enospc_at_fsync_or_replace_preserves_previous_bytes(self):
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation):
                self.path.write_bytes(b'previous-bytes\n')
                failure = OSError(errno.ENOSPC, 'No space left on device')
                with mock.patch.object(a.os, operation, side_effect=failure):
                    with self.assertRaises(OSError):
                        a.write_json_atomic(self.path, {'n': 2})
                self.assertEqual(self.path.read_bytes(), b'previous-bytes\n')
                self.assertEqual(self.names(), ['state.json'])


class WriteTextAtomic(Base):
    def test_round_trip_keeps_exact_bytes_mode_and_leaves_no_temp(self):
        self.path.write_bytes(b'old\n')
        os.chmod(self.path, 0o640)
        a.write_text_atomic(self.path, 'diff --git a/f b/f\n')
        self.assertEqual(self.path.read_bytes(), b'diff --git a/f b/f\n')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(self.names(), ['state.json'])

    def test_enospc_during_write_preserves_previous_bytes(self):
        self.path.write_bytes(b'previous-bytes\n')
        real_fdopen = a.os.fdopen

        class FailingWriter:
            def __init__(self, fd):
                self.handle = real_fdopen(fd, 'wb')

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, _payload):
                raise OSError(errno.ENOSPC, 'No space left on device')

            def flush(self):
                self.handle.flush()

            def fileno(self):
                return self.handle.fileno()

        with mock.patch.object(a.os, 'fdopen', side_effect=lambda fd, _mode: FailingWriter(fd)):
            with self.assertRaises(OSError):
                a.write_text_atomic(self.path, 'new-bytes\n')
        self.assertEqual(self.path.read_bytes(), b'previous-bytes\n')
        self.assertEqual(self.names(), ['state.json'])

    def test_enospc_at_fsync_or_replace_preserves_previous_bytes(self):
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation):
                self.path.write_bytes(b'previous-bytes\n')
                failure = OSError(errno.ENOSPC, 'No space left on device')
                with mock.patch.object(a.os, operation, side_effect=failure):
                    with self.assertRaises(OSError):
                        a.write_text_atomic(self.path, 'new-bytes\n')
                self.assertEqual(self.path.read_bytes(), b'previous-bytes\n')
                self.assertEqual(self.names(), ['state.json'])


class Recover(Base):
    def test_replaces_empty_file_keeps_bytes_and_provenance(self):
        self.path.write_bytes(b'')
        a.recover(self.path, {'n': 3}, shape, PROV)
        self.assertEqual(a.load(self.path, shape), {'n': 3})
        backups = [p for p in self.dir.iterdir() if '.replaced-' in p.name]
        self.assertEqual([p.read_bytes() for p in backups], [b''])
        lines = (self.dir / 'state-recovery.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('"source": "fixture"', lines[0])

    def test_refuses_valid_state_by_default(self):
        self.path.write_text('{"n": 1}')
        with self.assertRaises(a.StateError):
            a.recover(self.path, {'n': 2}, shape, PROV)
        self.assertEqual(a.load(self.path, shape), {'n': 1})
        self.assertFalse((self.dir / 'state-recovery.jsonl').exists())

    def test_unreadable_state_is_never_replaced(self):
        self.path.write_text('{"n": 1}')
        io_error = OSError(errno.EIO, 'Input/output error')
        with mock.patch.object(a.Path, 'read_bytes', side_effect=io_error), self.assertRaises(OSError):
            a.recover(self.path, {'n': 2}, shape, PROV, may_replace=lambda old: True)
        self.assertEqual(a.load(self.path, shape), {'n': 1})
        self.assertFalse((self.dir / 'state-recovery.jsonl').exists())

    def test_may_replace_allows_only_the_named_valid_state(self):
        self.path.write_text('{"n": 1}')
        a.recover(self.path, {'n': 2}, shape, PROV, may_replace=lambda old: old['n'] == 1)
        self.assertEqual(a.load(self.path, shape), {'n': 2})

    def test_refuses_invalid_recovery_data_before_writing(self):
        self.path.write_bytes(b'')
        with self.assertRaises(ValueError):
            a.recover(self.path, {'n': 'x'}, shape, PROV)
        self.assertEqual(self.path.read_bytes(), b'')
        self.assertEqual(self.names(), ['state.json'])


if __name__ == '__main__':
    unittest.main()
