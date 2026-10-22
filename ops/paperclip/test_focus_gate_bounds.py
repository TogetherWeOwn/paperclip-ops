#!/usr/bin/env python3
# ===========================================================================
# test_focus_gate_bounds.py -- timeout bounds for the owner focus gate
# (ops/paperclip/focus_gate.py, case_e20c0209; roadmap v34 item M13.1).
#
# db() shelled `docker exec ... psql` with no timeout and api() wrapped curl
# in an unbounded subprocess.run, so in --loop mode one hung daemon call
# stalled the whole gate and enforcement silently stopped (fail-open).
# Both calls now carry explicit env-overridable timeouts; curl keeps its own
# -m 60 with the process backstop above it. A timeout logs event="error"
# and cancels nothing: a db timeout ends the tick unmeasured, an api timeout
# skips that row and the loop keeps going. Dry-run default is preserved.
#
# Offline: stdlib unittest with a stubbed subprocess.run. No network, no
# database, no credential, no host.
# ===========================================================================
import ast
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import focus_gate

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = Path(HERE, "focus_gate.py").read_text()


def untimed_runs(source):
    """Line numbers of subprocess.run calls without a timeout= keyword."""
    return [n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == 'run'
            and isinstance(n.func.value, ast.Name) and n.func.value.id == 'subprocess'
            and not any(k.arg == 'timeout' for k in n.keywords)]


def done_process(stdout=''):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr='')


def timed_out(*args, **kwargs):
    raise subprocess.TimeoutExpired(args[0] if args else kwargs.get('args'), kwargs.get('timeout'))


def db_rows(*rows):
    """CompletedProcess whose stdout parses to db() row lists."""
    return done_process(''.join('\t'.join(r) + '\n' for r in rows))


ROW = ('run-1', 'agent-1', 'issue-1', 'case_8c5fe4cc', 'wake', 'todo')


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = mock.patch.object(focus_gate, 'STATE', self.tmp.name)
        self.state.start()
        self.addCleanup(self.state.stop)
        self.apply = mock.patch.object(focus_gate, 'APPLY', True)
        self.apply.start()
        self.addCleanup(self.apply.stop)

    def gate_log(self):
        p = Path(self.tmp.name, 'gate.jsonl')
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l]

    def cancelled(self):
        p = Path(self.tmp.name, 'cancelled.tsv')
        return p.read_text() if p.exists() else ''


class BoundsPresent(Harness):
    def test_every_subprocess_run_is_bounded(self):
        self.assertGreaterEqual(SOURCE.count('subprocess.run('), 2)
        self.assertEqual(untimed_runs(SOURCE), [])

    def test_the_bound_check_can_go_red(self):
        self.assertEqual(untimed_runs('import subprocess\nsubprocess.run(["x"], timeout=5)\n'
                                     'subprocess.run(["x"])\n'), [3])

    def test_import_has_no_side_effects(self):
        # The module used to run its loop at import time; tests must be able
        # to import it without touching STATE or spawning children.
        home = tempfile.mkdtemp(prefix='fg-import-')
        self.addCleanup(__import__('shutil').rmtree, home, True)
        env = dict(os.environ, HOME=home)
        p = subprocess.run([sys.executable, '-c', 'import focus_gate'],
                           cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(os.listdir(home), [], 'import must not create STATE or anything else')


class SecondsHelper(Harness):
    def test_env_override_is_a_float(self):
        with mock.patch.dict(os.environ, {'FOCUS_GATE_DB_TIMEOUT_S': '7.5'}):
            importlib.reload(focus_gate)
        try:
            self.assertEqual(focus_gate.DB_TIMEOUT_S, 7.5)
        finally:
            importlib.reload(focus_gate)
        self.assertEqual(focus_gate.DB_TIMEOUT_S, 60)

    def test_bad_values_refuse(self):
        for bad in ('0', '-5', '3601', 'soon'):
            with self.subTest(bad=bad), \
                    mock.patch.dict(os.environ, {'FOCUS_GATE_DB_TIMEOUT_S': bad}):
                with self.assertRaises(SystemExit):
                    importlib.reload(focus_gate)
        importlib.reload(focus_gate)

    def test_api_backstop_must_exceed_curl_max_time(self):
        for bad in ('0.5', '60'):
            with self.subTest(bad=bad), \
                    mock.patch.dict(os.environ, {'FOCUS_GATE_API_TIMEOUT_S': bad}):
                with self.assertRaises(SystemExit):
                    importlib.reload(focus_gate)
        importlib.reload(focus_gate)


class DbBounds(Harness):
    def test_db_call_is_bounded(self):
        run = mock.Mock(return_value=db_rows(('a', 'b')))
        with mock.patch.object(focus_gate.subprocess, 'run', run):
            self.assertEqual(focus_gate.db('select 1'), [['a', 'b']])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ['docker', 'exec', '-i', 'paperclip-db'])
        self.assertEqual(run.call_args.kwargs.get('timeout'), focus_gate.DB_TIMEOUT_S)
        self.assertEqual(run.call_args.kwargs.get('input'), 'select 1')

    def test_hung_docker_logs_error_with_zero_cancels(self):
        run = mock.Mock(side_effect=timed_out)
        with mock.patch.object(focus_gate.subprocess, 'run', run):
            self.assertEqual(focus_gate.tick(), (None, 0))
        events = self.gate_log()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'error')
        self.assertEqual(self.cancelled(), '')

    def test_hung_docker_in_one_shot_prints_unmeasured_not_zero(self):
        # A db timeout is unmeasured, never a measured all-clear: one-shot
        # main() must not print "non-focus queued: 0".
        Path(self.tmp.name, 'ACTIVE').touch()
        with mock.patch.object(focus_gate, 'STATE', self.tmp.name), \
                mock.patch.object(focus_gate, 'APPLY', False), \
                mock.patch.object(focus_gate.sys, 'argv', ['focus_gate.py']), \
                mock.patch.object(focus_gate, 'tick', return_value=(None, 0)), \
                mock.patch('builtins.print') as output:
            focus_gate.main()
        output.assert_called_once()
        printed = output.call_args.args[0]
        self.assertIn('unmeasured', printed)
        self.assertNotIn('non-focus queued: 0', printed)

    def test_hung_docker_in_loop_stays_quiet(self):
        Path(self.tmp.name, 'ACTIVE').touch()
        with mock.patch.object(focus_gate, 'STATE', self.tmp.name), \
                mock.patch.object(focus_gate, 'APPLY', False), \
                mock.patch.object(focus_gate.sys, 'argv', ['focus_gate.py', '--loop', '9999']), \
                mock.patch.object(focus_gate, 'tick', return_value=(None, 0)), \
                mock.patch.object(focus_gate.time, 'sleep', side_effect=KeyboardInterrupt), \
                mock.patch('builtins.print') as output:
            with self.assertRaises(KeyboardInterrupt):
                focus_gate.main()
        output.assert_not_called()

    def test_refused_psql_fails_closed(self):
        # A refused daemon exits non-zero, which check=True turns into
        # CalledProcessError (raised here by the stub, as the real call
        # would): nothing measured, nothing cancelled.
        def refused(*args, **kwargs):
            raise subprocess.CalledProcessError(1, args[0], stderr='connection refused')
        with mock.patch.object(focus_gate.subprocess, 'run', side_effect=refused):
            with self.assertRaises(subprocess.CalledProcessError):
                focus_gate.tick()
        self.assertEqual(self.cancelled(), '')


class ApiBounds(Harness):
    def test_api_call_is_bounded(self):
        run = mock.Mock(return_value=done_process('200'))
        with mock.patch.object(focus_gate.subprocess, 'run', run):
            self.assertEqual(focus_gate.api('POST', '/x'), '200')
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], 'curl')
        self.assertEqual(argv[argv.index('-m') + 1], '60')
        self.assertEqual(run.call_args.kwargs.get('timeout'), focus_gate.API_TIMEOUT_S)

    def test_api_backstop_sits_above_curl_max_time(self):
        run = mock.Mock(return_value=done_process('200'))
        with mock.patch.object(focus_gate.subprocess, 'run', run):
            focus_gate.api('POST', '/x', {'a': 1})
        argv = run.call_args.args[0]
        curl_max = float(argv[argv.index('-m') + 1])
        self.assertGreater(focus_gate.API_TIMEOUT_S, curl_max)
        self.assertIn('--data-binary', argv)

    def test_hung_api_logs_error_skips_row_and_keeps_loop(self):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd[0])
            if cmd[0] == 'docker':
                return db_rows(ROW, ('run-2', 'agent-2', 'issue-2', 'case_d02d0252', 'wake', 'todo'))
            timed_out(cmd, **kwargs)

        with mock.patch.object(focus_gate.subprocess, 'run', side_effect=fake):
            self.assertEqual(focus_gate.tick(), (2, 0))
        # Both rows attempted (loop stayed alive), both logged error, none recorded.
        self.assertEqual(calls, ['docker', 'curl', 'curl'])
        events = self.gate_log()
        self.assertEqual([e['event'] for e in events], ['error', 'error'])
        self.assertEqual(self.cancelled(), '')

    def test_non_200_cancel_fails_closed(self):
        def fake(cmd, **kwargs):
            if cmd[0] == 'docker':
                return db_rows(ROW)
            return done_process('500')

        with mock.patch.object(focus_gate.subprocess, 'run', side_effect=fake):
            self.assertEqual(focus_gate.tick(), (1, 0))
        events = self.gate_log()
        self.assertEqual([(e['event'], e['code']) for e in events], [('cancel', '500')])
        self.assertEqual(self.cancelled(), '')


class TickPolicy(Harness):
    def test_dry_run_calls_no_api_and_records_nothing(self):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd[0])
            return db_rows(ROW)

        with mock.patch.object(focus_gate, 'APPLY', False), \
                mock.patch.object(focus_gate.subprocess, 'run', side_effect=fake):
            self.assertEqual(focus_gate.tick(), (1, 0))
        self.assertEqual(calls, ['docker'])
        events = self.gate_log()
        self.assertEqual([(e['event'], e['code']) for e in events], [('cancel', 'dry')])
        self.assertEqual(self.cancelled(), '')

    def test_apply_happy_path_records_only_on_200(self):
        def fake(cmd, **kwargs):
            if cmd[0] == 'docker':
                return db_rows(ROW)
            return done_process('200')

        with mock.patch.object(focus_gate.subprocess, 'run', side_effect=fake):
            self.assertEqual(focus_gate.tick(), (1, 1))
        events = self.gate_log()
        self.assertEqual([(e['event'], e['code']) for e in events], [('cancel', '200')])
        body = self.cancelled()
        self.assertIn('run-1', body)
        self.assertIn('case_8c5fe4cc', body)


class RewakeBounds(Harness):
    def write_cancelled(self):
        Path(self.tmp.name, 'cancelled.tsv').write_text(
            '2026-10-12T00:00:00Z\trun-1\tagent-1\tissue-1\tTOG-1\twake\n')

    def test_hung_db_during_rewake_logs_error_and_skips_issue(self):
        self.write_cancelled()
        with mock.patch.object(focus_gate, 'db', side_effect=subprocess.TimeoutExpired('docker', 60)), \
                mock.patch.object(focus_gate, 'api') as api:
            focus_gate.rewake()
        self.assertEqual([e['event'] for e in self.gate_log()], ['error'])
        api.assert_not_called()

    def test_hung_api_during_rewake_logs_error(self):
        self.write_cancelled()
        with mock.patch.object(focus_gate, 'db', return_value=[['todo', 'agent-1']]), \
                mock.patch.object(focus_gate, 'api', side_effect=subprocess.TimeoutExpired('curl', 70)):
            focus_gate.rewake()
        self.assertEqual([e['event'] for e in self.gate_log()], ['error'])

    def test_apply_output_reports_confirmed_cancels(self):
        Path(self.tmp.name, 'ACTIVE').touch()
        with mock.patch.object(focus_gate, 'STATE', self.tmp.name), \
                mock.patch.object(focus_gate, 'APPLY', True), \
                mock.patch.object(focus_gate.sys, 'argv', ['focus_gate.py']), \
                mock.patch.object(focus_gate, 'tick', return_value=(2, 0)), \
                mock.patch('builtins.print') as output:
            focus_gate.main()
        output.assert_called_once()
        self.assertIn('non-focus queued: 2 (cancelled: 0)', output.call_args.args[0])


class QueryFailedCodePreserved(Harness):
    # main()'s handler must not overwrite the specific error_code tick() set:
    # docs/operator/focus-gate.md lists query_failed as a distinct code, so the
    # CLI must emit it (not the generic query_or_processing_failed) when the
    # tick query fails. The generic code remains the fallback for errors that
    # set no specific code.
    def test_json_carries_query_failed_not_generic(self):
        Path(self.tmp.name, 'ACTIVE').touch()

        def boom(sql):
            raise subprocess.CalledProcessError(1, ['docker'], stderr='connection refused')

        buf = io.StringIO()
        with mock.patch.object(focus_gate, 'APPLY', False), \
                mock.patch.object(focus_gate.sys, 'argv', ['focus_gate.py', '--format', 'json']), \
                mock.patch.object(focus_gate, 'db', side_effect=boom):
            with redirect_stdout(buf):
                rc = focus_gate.main()
        summary = json.loads(buf.getvalue())
        self.assertEqual(summary['error_code'], 'query_failed')
        self.assertEqual(summary['measurement'], 'unmeasured')
        self.assertEqual(summary['counts']['confirmed'], 0)
        self.assertEqual(self.cancelled(), '')
        self.assertNotEqual(rc, 0)

    def test_unclassified_processing_error_keeps_generic_code(self):
        Path(self.tmp.name, 'ACTIVE').touch()
        row = ('run-9', 'agent-9', 'issue-9', 'case_99999999', 'wake', 'todo', focus_gate.CID)
        buf = io.StringIO()
        with mock.patch.object(focus_gate, 'APPLY', True), \
                mock.patch.object(focus_gate.sys, 'argv', ['focus_gate.py', '--format', 'json']), \
                mock.patch.object(focus_gate, 'db', return_value=[list(row)]), \
                mock.patch.object(focus_gate, 'api', side_effect=RuntimeError('boom')):
            with redirect_stdout(buf):
                rc = focus_gate.main()
        summary = json.loads(buf.getvalue())
        self.assertEqual(summary['error_code'], 'query_or_processing_failed')
        self.assertNotEqual(rc, 0)


class RewakeCompanyScope(Harness):
    # The rewake lookup must stay scoped to the configured company: a
    # cancelled.tsv row pointing at a foreign open issue must never wake a
    # foreign-company agent, even if recorded by the pre-fix gate or tampered
    # state. The SQL-honoring stub below exposes the foreign row only when the
    # lookup omits the company_id predicate, so the refusal test goes red if
    # the predicate is ever dropped.
    def write_cancelled(self, agent='agent-f', iid='issue-f', ident='TOG-F'):
        Path(self.tmp.name, 'cancelled.tsv').write_text(
            f'2026-10-12T00:00:00Z\trun-f\t{agent}\t{iid}\t{ident}\twake\n')

    def test_rewake_lookup_scopes_company(self):
        self.write_cancelled()
        seen = {}

        def fake_db(sql):
            seen['sql'] = sql
            return []

        with mock.patch.object(focus_gate, 'db', side_effect=fake_db):
            focus_gate.rewake()
        self.assertIn('sql', seen, 'rewake() must look up via db()')
        self.assertIn(f"company_id='{focus_gate.CID}'", seen['sql'])

    def test_rewake_ignores_foreign_open_issue(self):
        self.write_cancelled()
        foreign_open = [['in_progress', 'agent-f']]  # status, assignee match the wake criteria

        def fake_db(sql):
            if f"company_id='{focus_gate.CID}'" in sql:
                return []  # scoped lookup finds no local row
            return [list(r) for r in foreign_open]

        with mock.patch.object(focus_gate, 'db', side_effect=fake_db), \
                mock.patch.object(focus_gate, 'api') as api:
            focus_gate.rewake()
        api.assert_not_called()
        self.assertEqual([e['event'] for e in self.gate_log()], ['rewake_skip'])


class PreviewCreatesNoState(unittest.TestCase):
    # A default preview must not create the state directory (docs contract):
    # run the real CLI in a subprocess with a fresh HOME and no ACTIVE file.
    def test_default_preview_creates_no_state_dir(self):
        home = tempfile.mkdtemp(prefix='fg-preview-')
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ, HOME=home)
        p = subprocess.run([sys.executable, 'focus_gate.py'], cwd=HERE,
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            home, '.local', 'state', 'paperclip-focus')),
            'a preview must not create the state directory')


class MainExitCodes(Harness):
    # Refusals, unknown results and query failures exit nonzero; inactive and
    # clean previews exit zero (docs/operator/focus-gate.md). main()'s return
    # value is the pin: reverting it to a constant keeps every output-only
    # test green.
    def run_main_rc(self, argv, rows=(), apply=False, api_result='200',
                    active=True, db_side_effect=None):
        if active:
            Path(self.tmp.name, 'ACTIVE').touch()
        db_patch = (mock.patch.object(focus_gate, 'db', side_effect=db_side_effect)
                    if db_side_effect is not None
                    else mock.patch.object(focus_gate, 'db',
                                           return_value=[list(r) for r in rows]))
        buf = io.StringIO()
        with mock.patch.object(focus_gate, 'APPLY', apply), \
                mock.patch.object(focus_gate.sys, 'argv', argv), \
                db_patch, \
                mock.patch.object(focus_gate, 'api', return_value=api_result):
            with redirect_stdout(buf):
                return focus_gate.main()

    def test_refused_preview_exits_nonzero(self):
        foreign = ('run-f', 'agent-f', 'issue-f', 'case_ffffffff', 'wake', 'todo',
                   '00000000-0000-4000-8000-00000000ffff')
        self.assertNotEqual(
            self.run_main_rc(['focus_gate.py', '--format', 'json'], rows=[foreign]), 0)

    def test_unknown_preview_exits_nonzero(self):
        local = ('run-l', 'agent-l', 'issue-l', 'case_11111111', 'wake', 'todo', focus_gate.CID)
        self.assertNotEqual(
            self.run_main_rc(['focus_gate.py'], rows=[local], apply=True, api_result='500'), 0)

    def test_query_failure_exits_nonzero(self):
        def boom(sql):
            raise subprocess.CalledProcessError(1, ['docker'], stderr='refused')

        self.assertNotEqual(self.run_main_rc(['focus_gate.py'], db_side_effect=boom), 0)

    def test_inactive_exits_zero(self):
        self.assertEqual(self.run_main_rc(['focus_gate.py'], active=False), 0)

    def test_clean_preview_exits_zero(self):
        self.assertEqual(self.run_main_rc(['focus_gate.py'], rows=[]), 0)


if __name__ == '__main__':
    unittest.main()
