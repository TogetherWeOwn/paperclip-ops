"""case_02f68c9d: subprocess bounds for the connection, cleanup and idle-waiter sweeps.

Every subprocess.run in connection_intent_sweeper, cleanup_blocked and
idle_waiters must carry an explicit timeout=, a timeout must surface as
failed/unmeasured (never healthy-empty), and dry runs must issue no writes.
"""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cleanup_blocked as cb
import connection_intent_sweeper as cis
import idle_waiters as w

EVIDENCE = 'claude_auth_required stream disconnected before completion'
ROW = ['i1', 'case_8c5fe4cc', 'agent-1', EVIDENCE]


def done_process(stdout=''):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr='')


def quiet_stdout():
    return contextlib.redirect_stdout(io.StringIO())


class SweeperBounds(unittest.TestCase):
    def test_psql_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('a\tb\n'))
        with mock.patch.object(cis.subprocess, 'run', run):
            self.assertEqual(cis.psql('select 1'), [['a', 'b']])
        self.assertEqual(run.call_args.kwargs.get('timeout'), cis.PSQL_TIMEOUT)

    def test_decline_call_is_bounded(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return done_process('200')

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cis, 'APPLY', True), \
                mock.patch.object(cis, 'psql', return_value=[list(ROW)]), \
                mock.patch.object(cis, 'LOG', Path(tmp) / 'actions.log'), \
                mock.patch.object(cis.subprocess, 'run', side_effect=fake_run), \
                quiet_stdout():
            cis.main()
        curl = [kw for args, kw in calls if args[0] == 'curl']
        self.assertEqual(len(curl), 1)
        self.assertEqual(curl[0].get('timeout'), cis.CURL_TIMEOUT)

    def test_psql_timeout_fails_loud_with_no_decline(self):
        run = mock.Mock()
        err = io.StringIO()
        with mock.patch.object(cis, 'APPLY', True), \
                mock.patch.object(cis, 'psql', side_effect=subprocess.TimeoutExpired('docker', 60)), \
                mock.patch.object(cis.subprocess, 'run', run), \
                quiet_stdout(), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cis.main()
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('FAILED', err.getvalue())
        run.assert_not_called()

    def test_decline_timeout_records_failed_not_declined(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cis, 'APPLY', True), \
                mock.patch.object(cis, 'psql', return_value=[list(ROW)]), \
                mock.patch.object(cis, 'LOG', Path(tmp) / 'actions.log'), \
                mock.patch.object(cis.subprocess, 'run',
                                  side_effect=subprocess.TimeoutExpired('curl', 60)), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                cis.main()
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('FAILED decline', out.getvalue())
        self.assertNotIn('declined i1', out.getvalue())
        self.assertFalse((Path(tmp) / 'actions.log').exists())

    def test_dry_run_issues_no_writes(self):
        run = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(cis, 'APPLY', False), \
                mock.patch.object(cis, 'psql', return_value=[list(ROW)]), \
                mock.patch.object(cis.subprocess, 'run', run), \
                contextlib.redirect_stdout(out):
            cis.main()
        run.assert_not_called()
        self.assertIn('would decline', out.getvalue())


class CleanupBounds(unittest.TestCase):
    def test_psql_call_is_bounded(self):
        run = mock.Mock(return_value=done_process(''))
        with mock.patch.object(cb.subprocess, 'run', run):
            cb.psql('select 1')
        self.assertEqual(run.call_args.kwargs.get('timeout'), cb.PSQL_TIMEOUT)

    def test_patch_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('{"status":"backlog"}'))
        with mock.patch.object(cb.subprocess, 'run', run):
            self.assertEqual(cb.patch('iid', {'status': 'backlog'}), {'status': 'backlog'})
        self.assertEqual(run.call_args.kwargs.get('timeout'), cb.CURL_TIMEOUT)

    def test_patch_timeout_returns_failure_never_healthy(self):
        with mock.patch.object(cb.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('curl', 60)):
            r = cb.patch('iid', {'status': 'backlog'})
        self.assertNotIn('status', r)
        self.assertNotIn('id', r)
        self.assertIn('_timeout', r)

    def test_nowake_dry_run_issues_no_writes(self):
        run = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(cb, 'rows', return_value=[]), \
                mock.patch.object(cb.subprocess, 'run', run), \
                mock.patch.object(cb, 'open', side_effect=AssertionError('must not open files')), \
                contextlib.redirect_stdout(out):
            cb.main(['--phase', 'nowake'])
        run.assert_not_called()
        self.assertIn('(dry run)', out.getvalue())

    def test_nowake_apply_patch_timeout_records_failed(self):
        writes = []

        class FakeLog:
            def write(self, line):
                writes.append(line)

            def flush(self):
                pass

            def close(self):
                pass

        out = io.StringIO()
        with mock.patch.object(cb, 'rows', side_effect=[[[('x' * 36), 'case_f052904a']], [], []]), \
                mock.patch.object(cb, 'psql', return_value=''), \
                mock.patch.object(cb.subprocess, 'run',
                                  side_effect=subprocess.TimeoutExpired('curl', 60)), \
                mock.patch.object(cb, 'open', return_value=FakeLog()), \
                contextlib.redirect_stdout(out):
            cb.main(['--phase', 'nowake', '--apply'])
        self.assertIn('FAILED backlog', out.getvalue())
        self.assertTrue(any('FAILED backlog' in line for line in writes))

    def test_psql_timeout_aborts_loud_with_no_success_records(self):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(cb, 'rows', side_effect=subprocess.TimeoutExpired('docker', 60)), \
                mock.patch.object(cb, 'open', side_effect=AssertionError('must not open files')), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cb.main(['--phase', 'nowake'])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('FAILED', err.getvalue())
        self.assertNotIn('legacy-bare -> backlog', out.getvalue())

    def test_nowake_apply_midphase_timeout_reports_partially_applied_with_tsv(self):
        writes = []

        class FakeLog:
            def write(self, line):
                writes.append(line)

            def flush(self):
                pass

            def close(self):
                pass

        out = io.StringIO()
        err = io.StringIO()
        cards = [[('x' * 36), 'case_f052904a'], [('y' * 36), 'case_38d056f3']]
        with mock.patch.object(cb, 'rows', side_effect=[cards, subprocess.TimeoutExpired('docker', 60)]), \
                mock.patch.object(cb, 'patch', return_value={'status': 'backlog'}), \
                mock.patch.object(cb, 'open', return_value=FakeLog()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cb.main(['--phase', 'nowake', '--apply'])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('partially applied, see TSV', err.getvalue())
        self.assertNotIn('nothing applied', err.getvalue())
        self.assertIn('TSV:', out.getvalue())
        self.assertTrue(any('legacy-bare -> backlog' in line for line in writes))

    def test_nowake_apply_update_timeout_reports_partially_applied(self):
        class FakeLog:
            def write(self, line):
                pass

            def flush(self):
                pass

            def close(self):
                pass

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(cb, 'rows', return_value=[]), \
                mock.patch.object(cb, 'psql', side_effect=subprocess.TimeoutExpired('docker', 60)), \
                mock.patch.object(cb, 'open', return_value=FakeLog()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cb.main(['--phase', 'nowake', '--apply'])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('partially applied, see TSV', err.getvalue())
        self.assertIn('TSV:', out.getvalue())


class IdleWaiterBounds(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / 'idle-waiters.json'
        self.state.write_text(json.dumps({'touched': {}, 'flagged': {}}))

    def patch_state(self):
        for name, value in (('STATE', str(self.state)), ('STATE_DIR', str(self.dir))):
            p = mock.patch.object(w, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_psql_call_is_bounded(self):
        run = mock.Mock(return_value=done_process(''))
        with mock.patch.object(w.subprocess, 'run', run):
            w.psql('select 1')
        self.assertEqual(run.call_args.kwargs.get('timeout'), w.PSQL_TIMEOUT)

    def test_api_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('{}'))
        with mock.patch.object(w.subprocess, 'run', run):
            self.assertEqual(w.api('GET', 'x', {}), {})
        self.assertEqual(run.call_args.kwargs.get('timeout'), w.API_TIMEOUT)

    def test_api_timeout_returns_failure_never_healthy(self):
        with mock.patch.object(w.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('curl', 90)):
            r = w.api('PATCH', 'issues/x', {'status': 'blocked'})
        self.assertNotIn('status', r)
        self.assertIn('_timeout', r)

    def test_waiters_query_timeout_fails_loud_not_healthy_empty(self):
        self.patch_state()
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(w, 'psql', side_effect=subprocess.TimeoutExpired('docker', 60)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                w.run(apply=False)
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('FAILED', err.getvalue())
        self.assertNotIn('waiters:', out.getvalue())

    def test_park_patch_timeout_logs_failed_not_parked(self):
        self.patch_state()
        waiter = ['w' + '0' * 35, 'case_ccba9d4c', 'in_progress', 'stuck thing',
                  '', 'still waiting on TOG-' '999999301 to land', '']
        doer = ['d' + '0' * 35, 'todo', 'agent-9', 'real work', '0']

        def fake_psql(sql):
            if 'with n as' in sql:
                return [waiter]
            if 'select o.identifier' in sql:
                return []
            return [doer]

        log = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(w, 'psql', side_effect=fake_psql), \
                mock.patch.object(w.live_run_guard, 'issue_has_live_work', return_value=False), \
                mock.patch.object(w, 'log', log), \
                mock.patch.object(w.subprocess, 'run',
                                  side_effect=subprocess.TimeoutExpired('curl', 90)), \
                contextlib.redirect_stdout(out):
            w.run(apply=True)
        self.assertTrue(any('ok=False' in str(call) for call in log.call_args_list))
        self.assertIn('parked 0', out.getvalue())

    def test_dry_run_issues_no_writes(self):
        self.patch_state()
        waiter = ['w' + '0' * 35, 'case_ccba9d4c', 'in_progress', 'stuck thing',
                  '', 'still waiting on TOG-' '999999301 to land', '']
        doer = ['d' + '0' * 35, 'todo', 'agent-9', 'real work', '0']

        def fake_psql(sql):
            if 'with n as' in sql:
                return [waiter]
            if 'select o.identifier' in sql:
                return []
            return [doer]

        api = mock.Mock()
        run = mock.Mock()
        with mock.patch.object(w, 'psql', side_effect=fake_psql), \
                mock.patch.object(w, 'api', api), \
                mock.patch.object(w.subprocess, 'run', run), \
                quiet_stdout():
            w.run(apply=False)
        api.assert_not_called()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
