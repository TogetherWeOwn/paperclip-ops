#!/usr/bin/env python3
import contextlib
import datetime as dt
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stuck_run_reaper as r

NOW = dt.datetime(2026, 10, 7, 0, 0, tzinfo=dt.timezone.utc)


def iso(delta_min):
    return (NOW - dt.timedelta(minutes=delta_min)).isoformat()


class Classify(unittest.TestCase):
    def test_known_phrases(self):
        self.assertEqual(r.classify('adapter_failed', "ENOSPC: no space left on device, mkdir '/tmp/x'"), 'enospc')
        self.assertEqual(r.classify('adapter_failed', "hint: x\nerror: import.bundle did not send all necessary objects"),
                         'bundle_import')
        self.assertEqual(r.classify('x', 'Low-trust execution requires a sandbox environment driver.'),
                         'low_trust_no_sandbox')

    def test_normalizes_ids_and_tmp_paths(self):
        a = r.classify('setup_failed', "boom 00000000-0000-4000-8000-000000000060 at /tmp/abc-123 line 7")
        b = r.classify('setup_failed', "boom 00000000-0000-4000-8000-000000000061 at /tmp/zzz-999 line 8")
        self.assertEqual(a, b)

    def test_hint_lines_skipped(self):
        self.assertTrue(r.classify('e', 'hint: blah\nfatal: real cause').startswith('e:fatal'))


class Dead(unittest.TestCase):
    def run_row(self, minutes_idle, status='running'):
        t = iso(minutes_idle)
        return {'status': status, 'last_output_at': t, 'updated_at': t, 'process_started_at': t, 'created_at': t}

    def test_recent_activity_is_never_dead(self):
        self.assertFalse(r.is_dead(self.run_row(30), NOW, r.PID_ABSENT, False)[0])

    def test_silence_with_live_local_process_is_not_dead(self):
        self.assertFalse(r.is_dead(self.run_row(300), NOW, r.PID_ALIVE, False)[0])

    def test_silence_with_live_remote_process_is_not_dead(self):
        self.assertFalse(r.is_dead(self.run_row(300), NOW, r.PID_ABSENT, True)[0])

    def test_no_process_and_old_is_dead(self):
        self.assertTrue(r.is_dead(self.run_row(121), NOW, r.PID_ABSENT, False)[0])

    def test_unmeasured_probe_is_never_dead(self):
        dead, why = r.is_dead(self.run_row(500), NOW, r.PID_UNMEASURED, False)
        self.assertFalse(dead)
        self.assertEqual(why, 'local_pid_unmeasured')

    def test_terminal_status_ignored(self):
        self.assertFalse(r.is_dead(self.run_row(500, 'failed'), NOW, r.PID_ABSENT, False)[0])

    def test_newest_timestamp_wins(self):
        row = self.run_row(500)
        row['last_output_at'] = iso(10)
        self.assertFalse(r.is_dead(row, NOW, r.PID_ABSENT, False)[0])


def done(rc, out='', err=''):
    return subprocess.CompletedProcess(args=['docker', 'exec'], returncode=rc, stdout=out, stderr=err)


class Probe(unittest.TestCase):
    def probed(self, pid, run_result):
        with mock.patch.object(r.subprocess, 'run', run_result) as m:
            state, detail = r.probe_container_pid(pid)
        return m, state, detail

    def test_alive_control(self):
        m, state, detail = self.probed(123, mock.Mock(return_value=done(0, 'alive\n')))
        self.assertEqual((state, detail), (r.PID_ALIVE, ''))
        self.assertIn('/proc/123', ' '.join(m.call_args.args[0]))

    def test_absent_control(self):
        _, state, detail = self.probed('456', mock.Mock(return_value=done(0, 'absent\n')))
        self.assertEqual((state, detail), (r.PID_ABSENT, ''))

    def test_daemon_error_exit_1_is_unmeasured(self):
        _, state, detail = self.probed(
            123, mock.Mock(return_value=done(1, '', 'Cannot connect to the Docker daemon\n')))
        self.assertEqual(state, r.PID_UNMEASURED)
        self.assertIn('Docker daemon', detail)

    def test_exec_error_exit_1_is_unmeasured(self):
        _, state, detail = self.probed(
            123, mock.Mock(return_value=done(1, '', 'Error response from daemon: No such container\n')))
        self.assertEqual(state, r.PID_UNMEASURED)

    def test_bare_exit_1_is_unmeasured(self):
        _, state, detail = self.probed(123, mock.Mock(return_value=done(1)))
        self.assertEqual(state, r.PID_UNMEASURED)
        self.assertTrue(detail)

    def test_timeout_is_unmeasured(self):
        _, state, detail = self.probed(123, mock.Mock(side_effect=subprocess.TimeoutExpired('docker', 30)))
        self.assertEqual(state, r.PID_UNMEASURED)
        self.assertIn('timed out', detail)

    def test_malformed_output_is_unmeasured(self):
        for out in ('', 'yes\n', 'alive absent\n'):
            with self.subTest(out=out):
                _, state, detail = self.probed(123, mock.Mock(return_value=done(0, out)))
                self.assertEqual(state, r.PID_UNMEASURED)
                self.assertTrue(detail)

    def test_probe_is_bounded(self):
        m, _, _ = self.probed(123, mock.Mock(return_value=done(0, 'absent\n')))
        self.assertEqual(m.call_args.kwargs.get('timeout'), r.PROBE_TIMEOUT)

    def test_missing_pid_keeps_the_preexisting_absent_path(self):
        for pid in (None, '', '   '):
            with self.subTest(pid=pid):
                m, state, _ = self.probed(pid, mock.Mock(side_effect=AssertionError('must not probe')))
                self.assertEqual(state, r.PID_ABSENT)
                m.assert_not_called()

    def test_unparsable_pid_is_unmeasured_without_probing(self):
        m, state, detail = self.probed('not-a-pid', mock.Mock(side_effect=AssertionError('must not probe')))
        self.assertEqual(state, r.PID_UNMEASURED)
        self.assertIn('unparsable', detail)
        m.assert_not_called()


class UncertainRetention(unittest.TestCase):
    def old_run(self):
        t = iso(500)
        return {'id': 'run-9', 'status': 'running', 'process_pid': 123, 'process_started_at': t,
                'last_output_at': t, 'updated_at': t, 'created_at': t,
                'agent_id': 'other-agent', 'agent': 'fixture', 'issue_id': None}

    def find(self, probe_result):
        with mock.patch.object(r, 'psql_json', return_value=[self.old_run()]), \
                mock.patch.object(r, 'probe_container_pid', return_value=probe_result):
            return r.find_dead(NOW)

    def test_unmeasured_probe_retains_the_run(self):
        dead, unmeasured = self.find((r.PID_UNMEASURED, 'probe timed out after 30s'))
        self.assertEqual(dead, [])
        self.assertEqual(len(unmeasured), 1)
        self.assertIn('timed out', unmeasured[0]['diagnostic'])

    def test_confirmed_absence_with_sufficient_age_remains_actionable(self):
        dead, unmeasured = self.find((r.PID_ABSENT, ''))
        self.assertEqual(len(dead), 1)
        self.assertEqual(unmeasured, [])

    def test_dry_run_reports_unmeasured_and_cancels_nothing(self):
        entry = {'run': 'run-9', 'agent': 'fixture', 'issue': None,
                 'diagnostic': 'local PID probe unmeasured: docker exec exit=1'}
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / 'state.json').write_text('{}')
            with mock.patch.object(r, 'STATE_DIR', d), \
                    mock.patch.object(r, 'STATE', d / 'state.json'), \
                    mock.patch.object(r, 'PAUSE', d / 'PAUSE'), \
                    mock.patch.object(r, 'find_dead', return_value=([], [entry])), \
                    mock.patch.object(r, 'psql_json', return_value=[]), \
                    mock.patch.object(r, 'api', side_effect=AssertionError('must not call the API')), \
                    contextlib.redirect_stdout(io.StringIO()):
                report = r.run(dry=True)
        self.assertEqual(report['dead'], [])
        self.assertEqual(report['unmeasured'], [entry])


class Loop(unittest.TestCase):
    def fails(self, minutes, err='ENOSPC: no space', status='failed'):
        return [{'status': status, 'created_at': iso(m), 'error_code': 'adapter_failed', 'error': err} for m in minutes]

    def test_three_same_class_in_window(self):
        self.assertEqual(r.loop_verdict(self.fails([5, 30, 60]), NOW), (True, 'enospc'))

    def test_needs_three(self):
        self.assertFalse(r.loop_verdict(self.fails([5, 30]), NOW)[0])

    def test_newest_success_breaks_loop(self):
        runs = [{'status': 'succeeded', 'created_at': iso(1)}] + self.fails([5, 30, 60])
        self.assertFalse(r.loop_verdict(runs, NOW)[0])

    def test_outside_window(self):
        self.assertFalse(r.loop_verdict(self.fails([5, 30, 200]), NOW)[0])

    def test_mixed_classes(self):
        runs = self.fails([5, 30]) + self.fails([60], err='did not send all necessary objects')
        self.assertFalse(r.loop_verdict(runs, NOW)[0])


if __name__ == '__main__':
    unittest.main()
