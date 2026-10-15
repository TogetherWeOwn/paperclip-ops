#!/usr/bin/env python3
"""case_b83af0e8: subprocess bounds for the board jobs (replay reconcile, unblock, hide-noop).

Every subprocess.run in reconcile_replay_blocked, board_unblock and
hide_noop_comments must carry an explicit timeout= (board_unblock psql keeps
its 120 s bound); a timeout surfaces as failed/unmeasured, never as
healthy-empty output. Offline: stdlib unittest with a fake subprocess.run.
"""
import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import board_unblock as bu
import hide_noop_comments as hn
import reconcile_replay_blocked as rr


def untimed_runs(source):
    """Line numbers of subprocess.run calls without a timeout= keyword."""
    return [n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == 'run'
            and isinstance(n.func.value, ast.Name) and n.func.value.id == 'subprocess'
            and not any(k.arg == 'timeout' for k in n.keywords)]


def done_process(stdout=''):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr='')


def quiet_stdout():
    return contextlib.redirect_stdout(io.StringIO())


def timed_out(*args, **kwargs):
    raise subprocess.TimeoutExpired(args[0] if args else kwargs.get('args'), kwargs.get('timeout'))


class BoundsPresent(unittest.TestCase):
    def test_every_subprocess_run_is_bounded(self):
        self.assertGreaterEqual(Path(rr.__file__).read_text().count('subprocess.run('), 2)
        self.assertGreaterEqual(Path(bu.__file__).read_text().count('subprocess.run('), 2)
        self.assertGreaterEqual(Path(hn.__file__).read_text().count('subprocess.run('), 1)
        for mod in (rr, bu, hn):
            with self.subTest(mod=mod.__name__):
                self.assertEqual(untimed_runs(Path(mod.__file__).read_text()), [])

    def test_the_bound_check_can_go_red(self):
        self.assertEqual(untimed_runs('import subprocess\nsubprocess.run(["x"], timeout=5)\n'
                                     'subprocess.run(["x"])\n'), [3])


class ReconcileBounds(unittest.TestCase):
    def test_psql_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('a\tb\n'))
        with mock.patch.object(rr.subprocess, 'run', run):
            self.assertEqual(rr.psql('select 1'), 'a\tb\n')
        self.assertEqual(run.call_args.kwargs.get('timeout'), rr.PSQL_TIMEOUT)

    def test_wake_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('{"id":"run-1","status":"queued"}'))
        with mock.patch.object(rr.subprocess, 'run', run), quiet_stdout():
            self.assertEqual(rr.wake('aid', 'iid', 'case_8c5fe4cc'), 'run-1')
        argv = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs.get('timeout'), rr.CURL_TIMEOUT)
        self.assertEqual(argv[argv.index('--max-time') + 1], '30')

    def test_wake_returns_a_run_id_only_when_a_run_started(self):
        for stdout in ('{"status":"skipped","reason":"execution_reconciliation_required"}',
                       '{"status":"skipped"}', '{"id":"run-1","status":"skipped"}',
                       '{"status":"queued"}', 'not json', ''):
            with self.subTest(stdout=stdout):
                run = mock.Mock(return_value=done_process(stdout))
                out = io.StringIO()
                with mock.patch.object(rr.subprocess, 'run', run), contextlib.redirect_stdout(out):
                    self.assertIsNone(rr.wake('aid', 'iid', 'case_8c5fe4cc'))
                self.assertIn('FAILED', out.getvalue())

    def test_wake_timeout_is_a_failure_never_a_wake(self):
        out = io.StringIO()
        with mock.patch.object(rr.subprocess, 'run', side_effect=timed_out), \
                contextlib.redirect_stdout(out):
            self.assertIsNone(rr.wake('aid', 'iid', 'case_8c5fe4cc'))
        self.assertIn('case_8c5fe4cc', out.getvalue())
        self.assertIn('FAILED', out.getvalue())

    def test_curl_max_time_expiry_is_a_failure_never_a_wake(self):
        # curl's own --max-time fires first: exit 28, empty stdout, while
        # subprocess.run returns normally. That must read as a failure.
        out = io.StringIO()
        run = mock.Mock(return_value=subprocess.CompletedProcess(args=[], returncode=28, stdout='', stderr=''))
        with mock.patch.object(rr.subprocess, 'run', run), contextlib.redirect_stdout(out):
            self.assertIsNone(rr.wake('aid', 'iid', 'case_8c5fe4cc'))
        self.assertIn('case_8c5fe4cc', out.getvalue())
        self.assertIn('FAILED', out.getvalue())

    def test_dry_run_wakes_nothing(self):
        run = mock.Mock(return_value=done_process('ra1\tiid1\tTOG-1\ttodo\taid1\n'))
        out = io.StringIO()
        with mock.patch.object(rr.subprocess, 'run', run), contextlib.redirect_stdout(out):
            self.assertEqual(rr.main([]), 0)
        self.assertEqual(run.call_count, 1)
        self.assertNotEqual(run.call_args.args[0][0], 'curl')
        self.assertIn('TOG-' '1', out.getvalue())

    def test_psql_timeout_aborts_before_anything_is_applied(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(rr, 'ROOT', tmp), \
                    mock.patch.object(rr.subprocess, 'run', side_effect=timed_out), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(subprocess.TimeoutExpired):
                    rr.main(['--apply'])
            self.assertEqual(list(Path(tmp).glob('ops/productivity/replay-blocked-backup-*.json')), [])

    def test_wake_timeout_marks_failed_and_wakes_nothing(self):
        import tempfile

        def run(cmd, **kwargs):
            if cmd[0] == 'curl':
                timed_out(cmd, **kwargs)
            sql = kwargs.get('input', '')
            if 'json_agg' in sql:
                return done_process('[]')
            if 'update issue_recovery_actions' in sql:
                return done_process('')
            return done_process('ra1\tiid1\tTOG-1\ttodo\taid1\n')

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(f'{tmp}/ops/productivity')
            os.makedirs(f'{tmp}/ops/worker-vps')
            out = io.StringIO()
            with mock.patch.object(rr, 'ROOT', tmp), \
                    mock.patch.object(rr.subprocess, 'run', side_effect=run), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(rr.main(['--apply']), 0)
            self.assertIn('FAILED', out.getvalue())
            self.assertEqual(len(list(Path(tmp).glob('ops/productivity/replay-blocked-backup-*.json'))), 1)
            self.assertIn('woke 0', Path(tmp, 'ops/worker-vps/operator-actions.log').read_text())


class ReconcileDeadline(unittest.TestCase):
    def test_run_deadline_sits_below_parent_step_bound(self):
        import flow_guard as fg
        self.assertLess(rr.RUN_DEADLINE_SECONDS, fg.STEP_TIMEOUT_SECONDS)

    def test_module_constants_use_environment_overrides(self):
        import importlib.util
        env = {'RECONCILE_REPLAY_PSQL_TIMEOUT_SECONDS': '11',
               'RECONCILE_REPLAY_CURL_TIMEOUT_SECONDS': '12',
               'RECONCILE_REPLAY_RUN_DEADLINE_SECONDS': '13'}
        spec = importlib.util.spec_from_file_location('reconcile_config_fixture', rr.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, env):
            spec.loader.exec_module(module)
        self.assertEqual((module.PSQL_TIMEOUT, module.CURL_TIMEOUT, module.RUN_DEADLINE_SECONDS), (11, 12, 13))

    def test_invalid_timeouts_fail_before_any_subprocess(self):
        import importlib.util
        for value in ('0', '-1', 'nan', 'inf', 'not-a-number'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'RECONCILE_REPLAY_RUN_DEADLINE_SECONDS': value}):
                spec = importlib.util.spec_from_file_location('reconcile_invalid_fixture', rr.__file__)
                with mock.patch.object(rr.subprocess, 'run') as run, self.assertRaises(ValueError):
                    spec.loader.exec_module(importlib.util.module_from_spec(spec))
                run.assert_not_called()

    def _apply_with_rows(self, rows, monotonic, wake=None):
        import tempfile
        sqls = []

        def run(cmd, **kwargs):
            sql = kwargs.get('input', '')
            if cmd[0] == 'docker':
                sqls.append(sql)
                if 'json_agg' in sql:
                    return done_process('[]')
                return done_process('')
            if cmd[0] == 'curl':
                if wake is not None:
                    return wake(cmd, kwargs)
                return done_process('{"id":"run-x","status":"queued"}')
            self.fail(f'unexpected subprocess: {cmd[0]}')

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(f'{tmp}/ops/productivity')
            os.makedirs(f'{tmp}/ops/worker-vps')
            select = ''.join(f'ra{i}\tiid{i}\tTOG-{i}\ttodo\taid{i}\n' for i in range(rows))

            def fake_psql(sql):
                sqls.append(sql)
                if 'join issues i on i.id=ra.source_issue_id' in sql:
                    return select
                if 'json_agg' in sql:
                    return '[]'
                return ''
            with mock.patch.object(rr, 'ROOT', tmp), \
                    mock.patch.object(rr.time, 'monotonic', side_effect=monotonic), \
                    mock.patch.object(rr.subprocess, 'run', side_effect=run), \
                    mock.patch.object(rr, 'psql', side_effect=fake_psql):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(rr.main(['--apply', '--wake', '5']), 0)
            oplog = Path(tmp, 'ops/worker-vps/operator-actions.log').read_text()
            return sqls, out.getvalue(), oplog

    def test_deadline_defers_remaining_wakes_and_leaves_them_blocked(self):
        sqls, out, oplog = self._apply_with_rows(
            3, [0.0, 0.0, rr.RUN_DEADLINE_SECONDS + 1.0, rr.RUN_DEADLINE_SECONDS + 1.0])
        # First wake attempted and marked; the rest deferred past the deadline.
        self.assertIn('TOG-' '0 woke run', out)
        self.assertNotIn('TOG-' '1 woke run', out)
        self.assertNotIn('TOG-' '2 woke run', out)
        self.assertIn('run deadline', out)
        # Only the woken row is released; deferred rows are never touched and stay 'blocked'.
        self.assertTrue(any('operator_reconciled' in s and 'ra0' in s for s in sqls),
                        'woken row must be marked reconciled')
        self.assertFalse(any('operator_reconciled' in s and ('ra1' in s or 'ra2' in s) for s in sqls),
                         'deferred rows must never be marked reconciled')
        self.assertFalse(any('\'"blocked"\'' in s for s in sqls),
                         'a successful wake must not re-hold its row')
        self.assertIn('attempting', oplog)
        self.assertIn('deferred', oplog)

    def test_step_budget_defers_a_row_whose_wake_could_outlast_the_parent_bound(self):
        # A row starting this late cannot fit a full CURL_TIMEOUT wake inside
        # the parent step bound, so it is deferred untouched instead of being
        # marked and then killed mid-wake with no retry.
        late = rr.PARENT_STEP_BOUND_SECONDS - rr.CURL_TIMEOUT + 1.0
        self.assertLess(late, rr.RUN_DEADLINE_SECONDS,
                        'test setup: the budget gate must bind before the run deadline')
        sqls, out, oplog = self._apply_with_rows(3, [0.0, 0.0, 0.0, late])
        self.assertIn('TOG-' '0 woke run', out)
        self.assertIn('TOG-' '1 woke run', out)
        self.assertNotIn('TOG-' '2 woke run', out)
        self.assertIn('step budget', out)
        self.assertFalse(any('operator_reconciled' in s and 'ra2' in s for s in sqls),
                         'budget-deferred rows must never be marked reconciled')
        self.assertIn('deferred', oplog)

    def test_deadline_at_or_above_step_bound_fails_fast(self):
        import importlib.util
        for deadline, bound in (('120', '120'), ('300', '120'), ('100', '90')):
            with self.subTest(deadline=deadline, bound=bound):
                env = {'RECONCILE_REPLAY_RUN_DEADLINE_SECONDS': deadline,
                       'FLOW_GUARD_STEP_TIMEOUT_SECONDS': bound}
                spec = importlib.util.spec_from_file_location('reconcile_deadline_bound_fixture', rr.__file__)
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(rr.subprocess, 'run') as run, \
                        self.assertRaises(ValueError):
                    spec.loader.exec_module(importlib.util.module_from_spec(spec))
                run.assert_not_called()


class ReplayHoldFake:
    """Subprocess fake for reconcile --apply. As on the server, a wake is skipped while its row's hold is 'blocked'.

    Records mark, wake and restore events in order, each with whether that card's
    `attempting` line was already in the operator log at that moment."""

    def __init__(self, tmp, rows, timeout=(), refuse=()):
        self.tmp = tmp
        # Rows are (rid, iid, ident) for todo-with-assignee, or full
        # (rid, iid, ident, status, assignee) tuples for mixed-status coverage.
        self.rows = [tuple(r) + ('todo', f'aid-{r[2]}') if len(r) == 3 else tuple(r) for r in rows]
        self.hold = {r[0]: 'blocked' for r in self.rows}
        self.by_rid = {r[0]: r[2] for r in self.rows}
        self.by_issue = {r[1]: (r[0], r[2]) for r in self.rows}
        self.timeout, self.refuse = set(timeout), set(refuse)
        self.events = []
        self.started = []

    def _logged(self, ident):
        log = Path(self.tmp, 'ops/worker-vps/operator-actions.log')
        return log.exists() and f'attempting {ident} ' in log.read_text()

    def __call__(self, cmd, **kwargs):
        if cmd[0] == 'curl':
            return self.wake(cmd)
        sql = kwargs.get('input', '')
        if 'join issues i on i.id=ra.source_issue_id' in sql:
            return done_process(''.join(f'{rid}\t{iid}\t{ident}\t{st}\t{aid}\n'
                                        for rid, iid, ident, st, aid in self.rows))
        if 'json_agg' in sql:
            return done_process('[]')
        for rid in re.findall(r"'(ra\d+)'", sql):
            ident = self.by_rid[rid]
            if '"operator_reconciled"' in sql:
                self.hold[rid] = 'operator_reconciled'
                # The up-front idle release is one bulk UPDATE (where id in (...));
                # per-row releases just before a wake are single-row UPDATEs.
                kind = 'idle-release' if 'where id in (' in sql else 'mark'
                self.events.append((kind, ident, self._logged(ident)))
            elif '"blocked"' in sql:
                self.hold[rid] = 'blocked'
                self.events.append(('restore', ident, self._logged(ident)))
        return done_process('')

    def wake(self, cmd):
        payload = json.loads(cmd[cmd.index('--data-binary') + 1])
        rid, ident = self.by_issue[payload['payload']['issueId']]
        self.events.append(('wake', ident, self._logged(ident)))
        if ident in self.timeout:
            raise subprocess.TimeoutExpired(cmd, rr.CURL_TIMEOUT)
        if ident in self.refuse or self.hold[rid] == 'blocked':
            return done_process('{"status":"skipped","reason":"execution_reconciliation_required"}')
        self.started.append(ident)
        return done_process(json.dumps({'id': f'run-{ident}', 'status': 'queued'}))


class ReplayHoldRelease(unittest.TestCase):
    ROWS = [('ra0', 'iid0', 'case_05330ec4'), ('ra1', 'iid1', 'case_8c5fe4cc'), ('ra2', 'iid2', 'case_d02d0252'), ('ra3', 'iid3', 'case_a6dbab15')]
    # Eligible rows interleaved with idle ones (wrong status, or no assignee)
    # plus more eligible rows than a small --wake cap, so every branch binds.
    MIXED_ROWS = [('ra0', 'iid0', 'case_05330ec4', 'todo', 'aid-0'),
                  ('ra1', 'iid1', 'case_8c5fe4cc', 'todo', 'aid-1'),
                  ('ra2', 'iid2', 'case_d02d0252', 'in_review', 'aid-2'),
                  ('ra3', 'iid3', 'case_a6dbab15', 'blocked', 'aid-3'),
                  ('ra4', 'iid4', 'case_4ac9f045', 'todo', ''),
                  ('ra5', 'iid5', 'case_0dd2d987', 'todo', 'aid-5'),
                  ('ra6', 'iid6', 'case_15089482', 'in_progress', 'aid-6')]
    IDLE_IDENTS = ('case_d02d0252', 'case_a6dbab15', 'case_4ac9f045')

    def _apply(self, **fake_options):
        return self._apply_rows(self.ROWS, 4, **fake_options)

    def _apply_rows(self, rows, wake_n, **fake_options):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(f'{tmp}/ops/productivity')
        os.makedirs(f'{tmp}/ops/worker-vps')
        host = ReplayHoldFake(tmp, rows, **fake_options)
        out = io.StringIO()
        with mock.patch.object(rr, 'ROOT', tmp), \
                mock.patch.object(rr.subprocess, 'run', side_effect=host), \
                contextlib.redirect_stdout(out):
            self.assertEqual(rr.main(['--apply', '--wake', str(wake_n)]), 0)
        oplog = Path(tmp, 'ops/worker-vps/operator-actions.log').read_text()
        return host, out.getvalue(), oplog

    def test_every_released_card_starts_a_run(self):
        host, _, oplog = self._apply()
        self.assertEqual(host.started, ['case_05330ec4', 'case_8c5fe4cc', 'case_d02d0252', 'case_a6dbab15'])
        self.assertTrue(all(state == 'operator_reconciled' for state in host.hold.values()), host.hold)
        self.assertIn('woke 4', oplog)

    def test_timed_out_and_refused_wakes_re_hold_only_their_row(self):
        host, out, oplog = self._apply(timeout={'case_8c5fe4cc'}, refuse={'case_d02d0252'})
        self.assertEqual(host.started, ['case_05330ec4', 'case_a6dbab15'])
        self.assertEqual(host.hold, {'ra0': 'operator_reconciled', 'ra1': 'blocked',
                                     'ra2': 'blocked', 'ra3': 'operator_reconciled'})
        self.assertIn('case_8c5fe4cc wake FAILED', out)
        self.assertIn('case_d02d0252 wake FAILED', out)
        self.assertIn('woke 2', oplog)
        self.assertIn('wake FAILED for 2 card(s) (case_8c5fe4cc, case_d02d0252)', oplog)

    def test_each_card_is_logged_before_its_mark_and_its_wake(self):
        host, _, _ = self._apply(refuse={'case_d02d0252'})
        self.assertTrue(host.events)
        self.assertTrue(all(logged for _, _, logged in host.events), host.events)
        self.assertEqual([kind for kind, ident, _ in host.events if ident == 'case_05330ec4'], ['mark', 'wake'])
        self.assertEqual([kind for kind, ident, _ in host.events if ident == 'case_d02d0252'], ['mark', 'wake', 'restore'])

    def test_restore_strips_exactly_the_note_the_mark_appended(self):
        sqls = []
        with mock.patch.object(rr, 'psql', side_effect=sqls.append):
            rr.mark_reconciled('ra0', 'T')
            rr.restore_blocked('ra0', 'T')
        mark, restore = sqls
        note = rr.reconcile_note('T')
        self.assertIn(f"'{note}'", mark)
        self.assertIn(f"replace(coalesce(resolution_note,''),'{note}','')", restore)
        self.assertIn("'\"blocked\"'", restore)

    def test_idle_rows_are_released_up_front_without_a_wake(self):
        host, _, oplog = self._apply_rows(self.MIXED_ROWS, 10)
        for ident in self.IDLE_IDENTS:
            with self.subTest(ident=ident):
                kinds = [kind for kind, i, _ in host.events if i == ident]
                self.assertEqual(kinds, ['idle-release'],
                                 f'{ident} must be released by the bulk UPDATE, never marked for a wake')
        self.assertEqual(host.started, ['case_05330ec4', 'case_8c5fe4cc', 'case_0dd2d987', 'case_15089482'])
        self.assertTrue(all(state == 'operator_reconciled' for state in host.hold.values()), host.hold)
        self.assertIn('reconciled 7 rows', oplog)
        self.assertIn('woke 4', oplog)
        self.assertNotIn('deferred', oplog)

    def test_wake_cap_defers_surplus_rows_and_reports_counts(self):
        host, _, oplog = self._apply_rows(self.MIXED_ROWS, 2)
        self.assertEqual(host.started, ['case_05330ec4', 'case_8c5fe4cc'])
        # Surplus eligible rows are never touched, so they stay 'blocked'.
        self.assertEqual(host.hold['ra5'], 'blocked')
        self.assertEqual(host.hold['ra6'], 'blocked')
        self.assertEqual([i for _, i, _ in host.events if i in ('case_0dd2d987', 'case_15089482')], [])
        # 3 idle releases + 2 wakes reconciled; the surplus pair deferred.
        self.assertIn('reconciled 5 rows', oplog)
        self.assertIn('woke 2', oplog)
        self.assertIn('deferred 2 card(s) (case_0dd2d987, case_15089482)', oplog)


class BoardUnblockBounds(unittest.TestCase):
    def test_psql_bound_is_preserved_at_120(self):
        run = mock.Mock(return_value=done_process('a\tb\n'))
        with mock.patch.object(bu.subprocess, 'run', run):
            bu.psql('select 1')
        self.assertEqual(run.call_args.kwargs.get('timeout'), 120)

    def test_api_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('200'))
        with mock.patch.object(bu.subprocess, 'run', run), \
                mock.patch('builtins.open', side_effect=FileNotFoundError), \
                quiet_stdout():
            code, resp = bu.api('POST', 'agents/aid/wakeup', {'source': 'on_demand'})
        argv = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs.get('timeout'), bu.API_TIMEOUT)
        self.assertEqual(argv[argv.index('-m') + 1], '90')
        self.assertEqual(code, '200')
        self.assertEqual(resp, {})

    def test_api_timeout_is_never_healthy(self):
        with mock.patch.object(bu.subprocess, 'run', side_effect=timed_out):
            code, resp = bu.api('POST', 'agents/aid/wakeup', {'source': 'on_demand'})
            self.assertEqual((code, resp), ('timeout', {}))
            self.assertNotEqual(code, '200')
            self.assertNotIn(resp.get('status'), ('queued', 'started', 'scheduled'))
            code, resp = bu.api('PATCH', 'issues/cid', {'status': 'todo'})
            self.assertNotEqual(resp.get('id'), 'cid')

    def test_psql_timeout_aborts_with_no_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            report = str(Path(tmp, 'board-unblock.md'))
            with mock.patch.object(bu, 'psql', side_effect=timed_out), \
                    mock.patch.object(bu, 'REPORT', report), \
                    quiet_stdout():
                with self.assertRaises(subprocess.TimeoutExpired):
                    bu.main()
            self.assertFalse(Path(report).exists())

    def test_api_curl_timeout_ignores_stale_response_file(self):
        import json as _json
        # A previous successful action left a 'queued' body on disk; curl's own
        # -m timeout fires (rc 28, code '000') without touching the file.
        Path(bu.RESP_FILE).write_text(_json.dumps({'status': 'queued', 'id': 'cid'}))
        try:
            run = mock.Mock(return_value=subprocess.CompletedProcess(
                args=[], returncode=28, stdout='000', stderr=''))
            with mock.patch.object(bu.subprocess, 'run', run):
                code, resp = bu.api('POST', 'agents/aid/wakeup', {'source': 'on_demand'})
            self.assertNotEqual(code, '200')
            self.assertEqual(resp, {})
            self.assertNotIn(resp.get('status'), ('queued', 'started', 'scheduled'))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(bu.RESP_FILE)

    def test_mid_wave_psql_timeout_still_persists_picked(self):
        import json as _json
        import tempfile
        proj = bu.FOCUS_PROJECTS[0]
        cards = [
            ['cid1', 'case_8c5fe4cc', 't1', 'in_review', proj, '', '', 'false', 'false',
             'upd', 'null', '', '', ''],
            ['cid2', 'case_d02d0252', 't2', 'in_review', proj, '', '', 'false', 'false',
             'upd', 'null', '', '', ''],
        ]
        cap_calls = {'n': 0}

        def fake_psql(sql):
            if "heartbeat_runs where agent_id" in sql:
                cap_calls['n'] += 1
                if cap_calls['n'] >= 2:
                    raise subprocess.TimeoutExpired(['psql'], 120)
                return [['0']]
            if sql == "select count(*) from heartbeat_runs where status='running'":
                return [['0']]
            if sql.startswith('with recursive roots'):
                return [['cid1'], ['cid2']]
            if 'from agents where company' in sql:
                return [[bu.REVIEWER, 'Reviewer', 'idle']]
            if 'unblock_descriptor' in sql:
                return cards
            return []

        real_open = open

        def fake_open(path, *args, **kwargs):
            if path == '/proc/loadavg':
                return io.StringIO('0.50 0.40 0.30 1/1 1')
            if path == '/proc/meminfo':
                return io.StringIO('MemAvailable: 33554432 kB')
            return real_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            report = str(Path(tmp, 'board-unblock.md'))
            oplog = str(Path(tmp, 'operator-actions.log'))
            out = io.StringIO()
            with mock.patch.dict(os.environ, {'HOME': tmp}), \
                    mock.patch.object(bu, 'psql', side_effect=fake_psql), \
                    mock.patch.object(bu, 'api', return_value=('200', {'status': 'queued'})), \
                    mock.patch.object(bu, 'APPLY', True), \
                    mock.patch.object(bu, 'WAVE_N', 10), \
                    mock.patch.object(bu, 'REPORT', report), \
                    mock.patch.object(bu, 'OPLOG', oplog), \
                    mock.patch('builtins.open', fake_open), \
                    mock.patch('time.sleep'), \
                    contextlib.redirect_stdout(out):
                bu.main()
            acted = _json.load(real_open(os.path.join(tmp, '.local/state/board-unblock/acted.json')))
            self.assertIn('case_8c5fe4cc', acted)
            self.assertNotIn('case_d02d0252', acted)
            self.assertIn('case_8c5fe4cc', Path(oplog).read_text())
            self.assertIn('## Wave', Path(report).read_text())
            self.assertIn('wave done: 1 actions, 1 ok', out.getvalue())
            self.assertIn('stopped early', out.getvalue())


class HideNoopBounds(unittest.TestCase):
    def test_psql_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('x\n'))
        with mock.patch.object(hn.subprocess, 'run', run):
            self.assertEqual(hn.psql('select 1'), 'x\n')
        self.assertEqual(run.call_args.kwargs.get('timeout'), hn.PSQL_TIMEOUT)

    def test_psql_timeout_fails_closed(self):
        with mock.patch.object(hn.subprocess, 'run', side_effect=timed_out):
            with self.assertRaises(SystemExit) as cm:
                hn.psql('select 1')
        self.assertIsInstance(cm.exception.code, str)
        self.assertIn('timed out', cm.exception.code)
        self.assertIn('nothing hidden', cm.exception.code)

    def test_main_timeout_hides_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            backup = str(Path(tmp, 'hidden-noop-comments.jsonl'))
            state = str(Path(tmp, 'flow-guard'))
            os.makedirs(state)
            with mock.patch.object(hn, 'BACKUP', backup), \
                    mock.patch.object(hn, 'STATE_DIR', state), \
                    mock.patch.object(hn.subprocess, 'run', side_effect=timed_out), \
                    quiet_stdout():
                with self.assertRaises(SystemExit) as cm:
                    hn.main([])
            self.assertNotEqual(cm.exception.code, 0)
            self.assertFalse(Path(backup).exists())

    def test_pause_stops_the_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'PAUSE').write_text('')
            run = mock.Mock()
            with mock.patch.object(hn, 'STATE_DIR', tmp), \
                    mock.patch.object(hn.subprocess, 'run', run), \
                    quiet_stdout():
                self.assertEqual(hn.main([]), 0)
            run.assert_not_called()

    def test_dry_run_with_no_rows_hides_nothing(self):
        out = io.StringIO()
        with mock.patch.object(hn, 'psql', return_value=''), contextlib.redirect_stdout(out):
            self.assertEqual(hn.main([]), 0)
        self.assertIn('0 Paperclip-made no-op comment(s)', out.getvalue())


if __name__ == '__main__':
    unittest.main()
