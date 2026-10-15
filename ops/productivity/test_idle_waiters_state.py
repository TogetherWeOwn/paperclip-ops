import contextlib
import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

import atomic_state as a  # noqa: E402
import idle_waiters as w  # noqa: E402

DOER = '00000000-0000-4000-8000-000000000055'
AGENT = '00000000-0000-4000-8000-000000000056'
ROLLUP = '00000000-0000-4000-8000-000000000057'
SIX = {f'TOG-{n}': 1790000000.0 + n for n in range(101, 107)}
REAL_FDOPEN = os.fdopen
REAL_FSYNC = os.fsync


def waiter(iid, ident, title, notes):
    return [iid, ident, 'in_progress', title, '', notes, '']


class Board:
    def __init__(self, waiters=(), cards=None, blockers=None, rollup=ROLLUP):
        self.waiters = [list(r) for r in waiters]
        self.cards = cards or {}
        self.blockers = blockers or {}
        self.rollup = rollup

    def psql(self, sql):
        if 'with n as' in sql:
            return self.waiters
        if 'select o.identifier' in sql:
            iid = sql.split("related_issue_id='")[1].split("'")[0]
            return [[b] for b in self.blockers.get(iid, [])]
        if 'select id from issues where company_id' in sql:
            return [[self.rollup]] if self.rollup else []
        ident = sql.split("i.identifier='")[1].split("'")[0]
        return [self.cards[ident]] if ident in self.cards else []


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / 'idle-waiters.json'
        self.board = Board()
        self.psql = mock.Mock(side_effect=lambda sql: self.board.psql(sql))
        self.api = mock.Mock(return_value={'status': 'blocked'})
        self.log = mock.Mock()
        for name, value in (('STATE', str(self.state)), ('STATE_DIR', str(self.dir)), ('psql', self.psql),
                            ('api', self.api), ('log', self.log)):
            patch = mock.patch.object(w, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def write_json(self, obj):
        self.state.write_text(json.dumps(obj))

    def disk(self):
        return json.loads(self.state.read_bytes())

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def run_apply(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return w.run(apply=True)

    def run_dry(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return w.run(apply=False)


class FailClosed(Base):
    def assert_refused_untouched(self, raw=None):
        self.psql.reset_mock()
        self.api.reset_mock()
        self.log.reset_mock()
        with self.assertRaises(a.StateError):
            self.run_apply()
        self.psql.assert_not_called()
        self.api.assert_not_called()
        self.log.assert_not_called()
        if raw is not None:
            self.assertEqual(self.state.read_bytes(), raw)

    def test_missing_state_is_refused_not_read_as_empty(self):
        self.assert_refused_untouched()
        self.assertEqual(self.names(), [])

    def test_empty_state_is_refused(self):
        self.state.write_bytes(b'')
        self.assert_refused_untouched(b'')

    def test_truncated_state_is_refused(self):
        raw = b'{"touched": {}, "flagged": {"case_ccba9d4c": 1790000101.0'
        self.state.write_bytes(raw)
        self.assert_refused_untouched(raw)

    def test_malformed_or_invalid_state_is_refused(self):
        cases = [
            b'[]', b'null', b'{"touched": {}}', b'{"touched": {}, "flagged": {}, "cap": 3}',
            b'{"touched": [], "flagged": {}}', b'{"touched": {}, "flagged": {"case_ccba9d4c": "x"}}',
            b'{"touched": {}, "flagged": {"case_ccba9d4c": true}}', b'{"touched": {}, "flagged": {"case_ccba9d4c": NaN}}',
            b'{"touched": {}, "flagged": {"case_ccba9d4c": 1e400}}', b'{"touched": {}, "flagged": ["case_ccba9d4c"]}',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.state.write_bytes(raw)
                self.assert_refused_untouched(raw)

    def test_paused_stops_before_reading_state(self):
        self.state.write_bytes(b'')
        (self.dir / 'PAUSE').write_text('')
        self.assertIsNone(self.run_apply())
        self.psql.assert_not_called()
        self.api.assert_not_called()
        self.assertEqual(self.state.read_bytes(), b'')

    def home_state(self):
        return self.dir / 'home' / '.local' / 'state' / 'flow-guard' / 'idle-waiters.json'

    def cli(self, *args):
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir(exist_ok=True)
        marks = {}
        for tool in ('docker', 'curl'):
            marks[tool] = self.dir / f'{tool}.called'
            (bin_dir / tool).write_text(f'#!/bin/sh\n: > "{marks[tool]}"\nexit 1\n')
            os.chmod(bin_dir / tool, 0o755)
        env = {'PATH': str(bin_dir), 'HOME': str(self.dir / 'home')}
        result = subprocess.run([sys.executable, w.__file__, *args], env=env, capture_output=True, text=True)
        return result, marks

    def test_cli_refuses_missing_state_without_calling_docker_or_curl(self):
        result, marks = self.cli('--apply')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(any(mark.exists() for mark in marks.values()))

    def test_cli_database_failure_stops_before_any_api_call(self):
        self.home_state().parent.mkdir(parents=True)
        self.home_state().write_text(json.dumps({'touched': {}, 'flagged': SIX}))
        result, marks = self.cli('--apply')
        self.assertEqual(result.returncode, 1)
        self.assertTrue(marks['docker'].exists())
        self.assertFalse(marks['curl'].exists())

    def test_cli_recovery_refusals_exit_1_and_write_nothing(self):
        self.home_state().parent.mkdir(parents=True)
        self.home_state().write_text(json.dumps({'touched': {}, 'flagged': SIX}))
        raw = self.home_state().read_bytes()
        list_doc = self.dir / 'list.json'
        list_doc.write_text('[1, 2]')
        incomplete = self.dir / 'incomplete.json'
        incomplete.write_text(json.dumps({'provenance': {'source': 'x', 'retrieved_at': 'y', 'complete': False},
                                          'touched': {}, 'flagged': {}}))
        complete = self.dir / 'complete.json'
        complete.write_text(json.dumps({'provenance': {'source': 'x', 'retrieved_at': 'y', 'complete': True},
                                        'touched': {}, 'flagged': {}}))
        for args in (['--recover-state'], ['--recover-state', str(list_doc)], ['--recover-state', str(incomplete)],
                     ['--recover-state', str(complete)], ['--recover-state', str(self.dir / 'absent.json')]):
            with self.subTest(args=args):
                result, marks = self.cli(*args)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(self.home_state().read_bytes(), raw)
                self.assertFalse(any(mark.exists() for mark in marks.values()))

    def test_cli_recovery_of_missing_state_writes_the_recovered_state(self):
        doc = self.dir / 'recovery.json'
        doc.write_text(json.dumps({'provenance': {'source': 'x', 'retrieved_at': 'y', 'complete': True},
                                   'touched': {}, 'flagged': SIX}))
        result, _ = self.cli('--recover-state', str(doc))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.home_state().read_text()), {'touched': {}, 'flagged': SIX})


class PsqlSafety(unittest.TestCase):
    def test_psql_pins_on_error_stop_and_check(self):
        with mock.patch.object(w.subprocess, 'run') as run_mock:
            run_mock.return_value = mock.Mock(stdout='a\x1fb\n')
            self.assertEqual(w.psql('select 1'), [['a', 'b']])
        argv = run_mock.call_args.args[0]
        self.assertIn('-v', argv)
        self.assertIn('ON_ERROR_STOP=1', argv)
        self.assertTrue(run_mock.call_args.kwargs.get('check'))

    def test_cli_passes_on_error_stop_to_docker(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        home = d / 'home'
        (home / '.local' / 'state' / 'flow-guard').mkdir(parents=True)
        (home / '.local' / 'state' / 'flow-guard' / 'idle-waiters.json').write_text(
            json.dumps({'touched': {}, 'flagged': {}}))
        bindir = d / 'bin'
        bindir.mkdir()
        (bindir / 'docker').write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{d / "docker.argv"}"\n')
        os.chmod(bindir / 'docker', 0o755)
        curl_mark = d / 'curl.called'
        (bindir / 'curl').write_text(f'#!/bin/sh\n: > "{curl_mark}"\nexit 1\n')
        os.chmod(bindir / 'curl', 0o755)
        env = {'PATH': str(bindir), 'HOME': str(home)}
        result = subprocess.run([sys.executable, w.__file__, '--apply'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        argv = (d / 'docker.argv').read_text().splitlines()
        self.assertIn('-v', argv)
        self.assertIn('ON_ERROR_STOP=1', argv)
        self.assertFalse(curl_mark.exists())


class WriteFailures(Base):
    def setUp(self):
        super().setUp()
        self.write_json({'touched': {}, 'flagged': SIX})
        self.before = self.state.read_bytes()
        self.board = Board(waiters=[waiter('uuid-207', 'case_adae7506', 'Card seven', 'no-op: nothing new')])

    def assert_failed_before_post(self, error):
        with self.assertRaises(error):
            self.run_apply()
        self.api.assert_not_called()
        self.assertEqual(self.state.read_bytes(), self.before)
        self.assertEqual(self.names(), ['idle-waiters.json'])
        self.assertEqual(set(self.disk()['flagged']), set(SIX))

    def test_enospc_on_fsync_posts_nothing_and_keeps_prior_state(self):
        full = OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(a.os, 'fsync', side_effect=full):
            self.assert_failed_before_post(a.StateError)

    def test_truncated_write_keeps_prior_state(self):
        class Torn:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()
                return False

            def write(self, payload):
                self.f.write(payload[:len(payload) // 2])
                self.f.flush()
                raise OSError(errno.ENOSPC, 'No space left on device')

            def flush(self):
                self.f.flush()

            def fileno(self):
                return self.f.fileno()

        with mock.patch.object(a.os, 'fdopen', side_effect=lambda fd, mode: Torn(REAL_FDOPEN(fd, mode))):
            self.assert_failed_before_post(a.StateError)

    def test_interrupted_replace_keeps_prior_state(self):
        with mock.patch.object(a.os, 'replace', side_effect=KeyboardInterrupt):
            self.assert_failed_before_post(KeyboardInterrupt)

    def test_flag_is_durable_before_the_notice_is_posted(self):
        seen = {}

        def record_state(*_args):
            seen['flagged'] = set(self.disk()['flagged'])
            return {}

        self.api.side_effect = record_state
        self.run_apply()
        self.assertIn('case_adae7506', seen['flagged'])


class ParkWrites(Base):
    def setUp(self):
        super().setUp()
        self.write_json({'touched': {}, 'flagged': SIX})
        self.before = self.state.read_bytes()
        self.board = Board(waiters=[waiter('uuid-300', 'case_e224f2ef', 'Card three hundred', 'no-op: waiting on case_3d790dcb')],
                           cards={'case_3d790dcb': [DOER, 'in_progress', AGENT, 'Build parser', '0']})

    def test_failed_save_before_a_park_parks_nothing_and_the_retry_parks_once(self):
        with mock.patch.object(a.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            with self.assertRaises(a.StateError):
                self.run_apply()
        self.api.assert_not_called()
        self.assertEqual(self.state.read_bytes(), self.before)
        self.run_apply()
        self.api.assert_called_once()
        self.assertEqual(self.api.call_args.args[:2], ('PATCH', 'issues/uuid-300'))

    def test_park_is_recorded_before_the_patch(self):
        seen = {}

        def record_state(*_args):
            seen['touched'] = set(self.disk()['touched'])
            return {'status': 'blocked'}

        self.api.side_effect = record_state
        self.run_apply()
        self.assertEqual(seen['touched'], {'case_e224f2ef'})

    def test_failed_final_save_after_a_park_keeps_the_park_record(self):
        real = w.persist
        saves = []

        def fail_second_save(state):
            saves.append(state)
            if len(saves) == 2:
                raise a.StateError('no space left on device')
            real(state)

        with mock.patch.object(w, 'persist', side_effect=fail_second_save):
            with self.assertRaises(a.StateError):
                self.run_apply()
        self.api.assert_called_once()
        self.assertEqual(set(self.disk()['touched']), {'case_e224f2ef'})
        self.api.reset_mock()
        self.run_apply()
        self.api.assert_not_called()

    def test_a_parked_waiter_is_not_parked_again_while_its_record_lasts(self):
        self.run_apply()
        self.api.reset_mock()
        self.run_apply()
        self.api.assert_not_called()

    def test_directory_fsync_failure_after_the_park_record_sends_no_patch(self):
        def fail_directory_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, 'Input/output error')
            return REAL_FSYNC(fd)

        with mock.patch.object(a.os, 'fsync', side_effect=fail_directory_fsync):
            with self.assertRaises(a.StateError):
                self.run_apply()
        self.api.assert_not_called()
        self.assertIn('case_e224f2ef', self.disk()['touched'])
        self.run_apply()
        self.api.assert_not_called()


class Recover(Base):
    def recovery_doc(self, flagged, complete=True):
        return {'provenance': {'source': 'operator-actions.log idle-waiters lines', 'retrieved_at': '2026-10-09T12:00:00Z',
                               'complete': complete},
                'touched': {}, 'flagged': flagged}

    def write_doc(self, doc):
        path = self.dir / 'recovery.json'
        path.write_text(json.dumps(doc))
        return str(path)

    def test_recovered_six_suppression_entries_are_not_replayed(self):
        self.state.write_bytes(b'')
        w.recover_state(self.write_doc(self.recovery_doc(SIX)))
        self.assertEqual(self.disk(), {'touched': {}, 'flagged': SIX})
        six = [waiter(f'uuid-{n}', ident, f'Card {n}', 'no-op: nothing new') for n, ident in enumerate(SIX, 1)]
        self.board = Board(waiters=six + [waiter('uuid-207', 'case_adae7506', 'Card seven', 'no-op: nothing new')])
        self.run_apply()
        self.assertEqual(self.api.call_count, 1)
        method, path, body = self.api.call_args.args
        self.assertEqual((method, path), ('POST', f'issues/{ROLLUP}/comments'))
        self.assertIn('case_adae7506', body['body'])
        for ident in SIX:
            self.assertNotIn(ident, body['body'])
        self.assertEqual(set(self.disk()['flagged']), set(SIX) | {'case_adae7506'})

    def test_recovery_keeps_replaced_bytes_and_writes_provenance(self):
        self.state.write_bytes(b'')
        w.recover_state(self.write_doc(self.recovery_doc(SIX)))
        backups = [p for p in self.dir.iterdir() if '.replaced-' in p.name]
        self.assertEqual([p.read_bytes() for p in backups], [b''])
        lines = (self.dir / 'state-recovery.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['state'], {'touched': {}, 'flagged': SIX})

    def test_recovery_refuses_valid_state(self):
        raw = json.dumps({'touched': {}, 'flagged': SIX}).encode()
        self.state.write_bytes(raw)
        doc = self.write_doc(self.recovery_doc({'case_c89b5362': 1.0}))
        with self.assertRaises(a.StateError):
            w.recover_state(doc)
        self.assertEqual(self.state.read_bytes(), raw)
        self.assertEqual(self.names(), ['idle-waiters.json', 'recovery.json'])

    def test_recovery_needs_complete_provenance_and_valid_entries(self):
        self.state.write_bytes(b'')
        bad_docs = [
            self.recovery_doc(SIX, complete=False),
            {'provenance': self.recovery_doc({})['provenance'], 'touched': {}, 'flagged': ['case_ccba9d4c']},
            [1, 2],
            {'provenance': 'complete', 'touched': {}, 'flagged': {}},
        ]
        for doc in bad_docs:
            with self.subTest(doc=doc):
                with self.assertRaises(a.StateError):
                    w.recover_state(self.write_doc(doc))
                self.assertEqual(self.state.read_bytes(), b'')
                self.assertFalse((self.dir / 'state-recovery.jsonl').exists())


class Normal(Base):
    def test_dry_run_reads_but_never_writes_or_calls_the_api(self):
        raw = json.dumps({'touched': {}, 'flagged': {}}).encode()
        self.state.write_bytes(raw)
        self.board = Board(waiters=[waiter('uuid-207', 'case_adae7506', 'Card seven', 'no-op: nothing new')])
        self.run_dry()
        self.api.assert_not_called()
        self.assertEqual(self.state.read_bytes(), raw)

    def test_park_patches_the_doer_blocker_and_records_touched(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.board = Board(waiters=[waiter('uuid-300', 'case_e224f2ef', 'Card three hundred', 'no-op: waiting on case_3d790dcb')],
                           cards={'case_3d790dcb': [DOER, 'in_progress', AGENT, 'Build parser', '0']})
        self.run_apply()
        self.api.assert_called_once()
        method, path, body = self.api.call_args.args
        self.assertEqual((method, path), ('PATCH', 'issues/uuid-300'))
        self.assertEqual(body['status'], 'blocked')
        self.assertEqual(body['blockedByIssueIds'], [DOER])
        self.assertIn('case_e224f2ef', self.disk()['touched'])
        self.assertEqual(self.disk()['flagged'], {})

    def test_nodoer_waiter_is_flagged_once_across_runs(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.board = Board(waiters=[waiter('uuid-207', 'case_adae7506', 'Card seven', 'no-op: nothing new')])
        self.run_apply()
        self.assertEqual(self.api.call_count, 1)
        self.assertIn('case_adae7506', self.disk()['flagged'])
        self.api.reset_mock()
        self.run_apply()
        self.api.assert_not_called()

    def test_flag_without_a_rollup_card_creates_one(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.board = Board(waiters=[waiter('uuid-207', 'case_adae7506', 'Card seven', 'no-op: nothing new')], rollup=None)
        self.run_apply()
        method, path, body = self.api.call_args.args
        self.assertEqual((method, path), ('POST', f'companies/{w.CID}/issues'))
        self.assertEqual((body['title'], body['assigneeAgentId']), (w.ROLLUP_TITLE, w.CEO))

    def test_touched_within_a_day_is_skipped_and_older_entries_expire(self):
        now = time.time()
        self.write_json({'touched': {'TOG-A': now - 3600, 'TOG-B': now - 90000}, 'flagged': {}})
        self.board = Board(waiters=[waiter('uuid-a', 'TOG-A', 'Card A', 'no-op: nothing new'),
                                    waiter('uuid-b', 'TOG-B', 'Card B', 'no-op: nothing new')])
        self.run_apply()
        method, path, body = self.api.call_args.args
        self.assertIn('TOG-B', body['body'])
        self.assertNotIn('TOG-A', body['body'])
        self.assertEqual(self.disk()['touched'], {'TOG-A': now - 3600})

    def test_operator_and_pr_waiters_are_left_alone(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.board = Board(waiters=[
            waiter('uuid-op', 'case_77f64939', 'Operator: rotate host cert', 'no-op: nothing new'),
            waiter('uuid-pr', 'case_ce558910', 'Watch the CI', 'no-op: waiting on pull/812 CI checks'),
        ])
        self.run_apply()
        self.api.assert_not_called()
        self.assertEqual(self.disk()['flagged'], {})

    def test_apply_with_nothing_to_do_keeps_the_same_state(self):
        state = {'touched': {'TOG-A': time.time() - 3600}, 'flagged': dict(SIX)}
        self.write_json(state)
        self.run_apply()
        self.api.assert_not_called()
        self.assertEqual(self.disk(), state)
        self.assertEqual(self.names(), ['idle-waiters.json'])


if __name__ == '__main__':
    unittest.main()
