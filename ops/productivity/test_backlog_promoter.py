#!/usr/bin/env python3
"""Offline: a fake subprocess.run stands in for psql, gh_token.sh, gh and curl. No host, network or credential."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import backlog_promoter as bp
from test_review_error_rerequest import host_ok_stub, timed_out, untimed_runs

PR_CARD = ('a1', 'case_698e8eb7', 'Land PR #12 lint fix', 'high', 'Founding Engineer', 'idle')
PLAIN_CARD = ('a2', 'case_a08f133a', 'Add retry to the deploy step', 'medium', 'Founding Engineer', 'idle')


class Run(unittest.TestCase):
    def run_promoter(self, *, rows=(PR_CARD, PLAIN_CARD), runnable='0', pr_state='OPEN', slow=(),
                     patch_stdout='200', patch_returncode=0, log_path=None, patch_script=None):
        """slow: which calls time out, from {'runnable', 'rows', 'token', 'pr', 'patch'}.
        patch_script: optional per-PATCH outcomes, each 'timeout', an Exception
        to raise, or a (stdout, returncode) tuple."""
        patches, calls = [], []
        script = list(patch_script or [])

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            self.assertIn('timeout', kwargs)
            if cmd[0] == 'docker':
                sql = cmd[-1]
                if 'count(*)' in sql:
                    kind, out = 'runnable', runnable
                elif 'from agents' in sql:
                    kind, out = 'agents', 'Founding Engineer\tag-1\tidle\nWeb Engineer\tag-2\tidle'
                else:
                    kind = 'rows'
                    out = '\n'.join('\t'.join(r) for r in rows) if "p.name='TWO Bot Next'" in sql else ''
            elif cmd[0].endswith('gh_token.sh'):
                kind, out = 'token', 'tok'
            elif cmd[0] == 'gh':
                kind, out = 'pr', pr_state
            else:
                kind, out = 'patch', patch_stdout
                patches.append(cmd[cmd.index('-X') + 2].rsplit('/', 1)[1])
            if kind in slow:
                timed_out(cmd, **kwargs)
            if kind == 'patch' and script:
                item = script.pop(0)
                if item == 'timeout':
                    timed_out(cmd, **kwargs)
                if isinstance(item, Exception):
                    raise item
                out, returncode = item
            else:
                returncode = patch_returncode
            return subprocess.CompletedProcess(cmd, returncode if kind == 'patch' else 0,
                                               stdout=out + '\n', stderr='')

        stdout, error, exit_code = io.StringIO(), None, None
        with tempfile.TemporaryDirectory() as tmp:
            log = log_path or Path(tmp, 'operator.log')
            if not log.exists():
                log.write_text('')
            with mock.patch.dict(sys.modules, {'host_ok': host_ok_stub()}), \
                    mock.patch.dict(bp._pr_cache, clear=True), \
                    mock.patch.object(bp, 'APPLY', True), \
                    mock.patch.object(bp, 'LOG', log), \
                    mock.patch.object(bp, 'PAUSE', Path(tmp, 'PAUSE')), \
                    mock.patch.object(bp.subprocess, 'run', side_effect=run), \
                    mock.patch.object(sys, 'argv', ['promoter', '--apply', '--json-summary']), \
                    contextlib.redirect_stdout(stdout):
                try:
                    exit_code = bp.main()
                    if exit_code is None:  # Match sys.exit(main()) for imported legacy success.
                        exit_code = 0
                except Exception as e:
                    error = e
            return {'patches': patches, 'calls': calls, 'log': log.read_text(), 'stdout': stdout.getvalue(),
                    'error': error, 'exit': exit_code}

    def test_open_pr_card_and_plain_card_are_promoted(self):
        out = self.run_promoter()
        self.assertIsNone(out['error'])
        self.assertEqual(out['patches'], ['a1', 'a2'])
        self.assertIn('case_698e8eb7 backlog->todo', out['log'])

    def test_closed_pr_card_stays_in_backlog(self):
        out = self.run_promoter(pr_state='MERGED')
        self.assertEqual(out['patches'], ['a2'])

    def test_timed_out_pr_state_leaves_the_card_in_backlog(self):
        out = self.run_promoter(slow={'pr'})
        self.assertIsNone(out['error'])
        self.assertEqual(out['patches'], ['a2'])
        self.assertNotIn('case_698e8eb7', out['log'])
        self.assertIn('gh pr timed out', out['stdout'])

    def test_timed_out_token_fetch_leaves_the_card_in_backlog(self):
        out = self.run_promoter(slow={'token'})
        self.assertEqual(out['patches'], ['a2'])
        self.assertFalse([c for c, _ in out['calls'] if c[0] == 'gh'], 'gh ran without a token')

    def test_timed_out_runnable_count_promotes_nothing(self):
        out = self.run_promoter(slow={'runnable'})
        self.assertIsInstance(out['error'], subprocess.TimeoutExpired)
        self.assertEqual(out['patches'], [])
        self.assertEqual(out['log'], '')

    def test_timed_out_backlog_read_promotes_nothing(self):
        out = self.run_promoter(slow={'rows'})
        self.assertIsInstance(out['error'], subprocess.TimeoutExpired)
        self.assertEqual(out['patches'], [])

    def test_timed_out_patches_are_unknown_and_do_not_suppress_a_fresh_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            first = self.run_promoter(slow={'patch'}, log_path=log)
            self.assertIsNone(first['error'])
            self.assertEqual(first['patches'], ['a1', 'a2'])
            self.assertNotEqual(first['exit'], 0)
            self.assertEqual(first['log'], '', 'timeouts must not create completed-promotion records')
            summary = json.loads(first['stdout'].splitlines()[-1])
            self.assertEqual(summary['counts'], {'attempted': 2, 'confirmed': 0,
                                                 'definite_failure': 0, 'unknown': 2})
            for action, card in zip(summary['actions'], (PR_CARD, PLAIN_CARD)):
                self.assertIn(card[0], action['target'])
            self.assertEqual([a['outcome'] for a in summary['actions']], ['unknown', 'unknown'])
            for card in (PR_CARD, PLAIN_CARD):
                self.assertTrue(any(card[1] in line and 'unknown' in line
                                    for line in first['stdout'].splitlines() if not line.startswith('{')))
            second = self.run_promoter(log_path=log)
            self.assertIsNone(second['error'])
            self.assertEqual(second['patches'], ['a1', 'a2'], 'unknown is not 24h bounce suppression')
            self.assertEqual(second['exit'], 0)
            for card in (PR_CARD, PLAIN_CARD):
                self.assertIn(f'{card[1]} backlog->todo', second['log'])

    def test_unknown_duplicate_candidate_is_attempted_once_per_tick(self):
        out = self.run_promoter(rows=(PR_CARD, PR_CARD, PLAIN_CARD), slow={'patch'})
        self.assertIsNone(out['error'])
        self.assertEqual(out['patches'], ['a1', 'a2'])
        self.assertEqual(out['log'], '')
        summary = json.loads(out['stdout'].splitlines()[-1])
        self.assertEqual(summary['counts'], {'attempted': 2, 'confirmed': 0,
                                             'definite_failure': 0, 'unknown': 2})

    def test_nonzero_transport_with_stdout_200_never_confirms(self):
        for code in (7, 28, 56):
            with self.subTest(returncode=code), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp, 'operator.log')
                first = self.run_promoter(patch_returncode=code, log_path=log)
                self.assertIsNone(first['error'])
                self.assertEqual(first['patches'], ['a1', 'a2'])
                self.assertNotEqual(first['exit'], 0)
                self.assertEqual(first['log'], '')
                summary = json.loads(first['stdout'].splitlines()[-1])
                self.assertEqual(summary['counts']['attempted'], 2)
                self.assertEqual(summary['counts']['confirmed'], 0)
                self.assertTrue(all(a['outcome'] in {'definite_failure', 'unknown'}
                                    for a in summary['actions']))
                if code == 56:
                    # A non-special-cased curl exit pins the generic branch.
                    self.assertEqual([a['outcome'] for a in summary['actions']],
                                     ['unknown', 'unknown'])
                    self.assertEqual([a['code'] for a in summary['actions']],
                                     ['curl_transport_error'] * 2)
                second = self.run_promoter(log_path=log)
                self.assertIsNone(second['error'])
                self.assertEqual(second['patches'], ['a1', 'a2'])
                self.assertEqual(second['exit'], 0)

    def test_definite_failures_do_not_remove_the_attempt_cap(self):
        rows = tuple((f'card-{n}', f'case_fixture{n}', 'Tighten deploy guard',
                      'medium', 'Founding Engineer', 'idle') for n in range(5))
        with mock.patch.object(bp, 'MAX_PER_RUN', 2):
            out = self.run_promoter(rows=rows, patch_stdout='403')
        self.assertIsNone(out['error'])
        self.assertEqual(out['patches'], ['card-0', 'card-1'])
        self.assertNotEqual(out['exit'], 0)
        self.assertEqual(out['log'], '')
        summary = json.loads(out['stdout'].splitlines()[-1])
        self.assertEqual(summary['counts'], {'attempted': 2, 'confirmed': 0,
                                             'definite_failure': 2, 'unknown': 0})

    def test_seeded_bounce_log_pins_marker_and_age_bounds(self):
        def line(ts, marked):
            stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts))
            suffix = ('HTTP 200 outcome=confirmed code=http_200' if marked
                      else 'HTTP timeout')
            return (f'{stamp} backlog-promoter: {PLAIN_CARD[1]} backlog->todo '
                    f'[TWO Bot Next/medium] assignee Founding Engineer {suffix}')

        now = time.time()
        cases = [
            ('recent legacy line without a marker never suppresses',
             line(now - 60, False), ['a2']),
            ('a confirmed promotion older than 24h never suppresses',
             line(now - 25 * 3600, True), ['a2']),
            ('a confirmed promotion timestamped in the future never suppresses',
             line(now + 3600, True), ['a2']),
            ('a confirmed promotion an hour old suppresses the bounce',
             line(now - 3600, True), []),
        ]
        for name, seed, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp, 'operator.log')
                log.write_text(seed + '\n')
                out = self.run_promoter(rows=(PLAIN_CARD,), log_path=log)
                self.assertIsNone(out['error'])
                self.assertEqual(out['patches'], expected, name)

    def test_unknown_first_attempt_reserves_the_last_runnable_slot(self):
        # Mirror of the definite-failure capacity test: with need=1 the timed-out
        # first card (unknown, may have landed) reserves the slot, so the
        # confirmable second card must not be attempted this tick; otherwise
        # uncertain writes overshoot TARGET_RUNNABLE.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            out = self.run_promoter(rows=(PR_CARD, PLAIN_CARD),
                                    runnable=str(bp.TARGET_RUNNABLE - 1),
                                    patch_script=['timeout', ('200', 0)],
                                    log_path=log)
        self.assertIsNone(out['error'])
        self.assertEqual(out['patches'], ['a1'],
                         'an unknown first attempt reserves the last slot for this tick')
        self.assertNotEqual(out['exit'], 0)
        self.assertEqual(out['log'], '', 'no completed-promotion record for an unknown write')
        summary = json.loads(out['stdout'].splitlines()[-1])
        self.assertEqual(summary['counts'], {'attempted': 1, 'confirmed': 0,
                                             'definite_failure': 0, 'unknown': 1})

    def test_missing_curl_binary_is_a_definite_failure_and_the_batch_continues(self):
        boom = FileNotFoundError(2, 'No such file or directory', 'curl')
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            out = self.run_promoter(rows=(PR_CARD, PLAIN_CARD),
                                    patch_script=[boom, ('200', 0)],
                                    log_path=log)
        self.assertIsNone(out['error'])
        self.assertEqual(out['patches'], ['a1', 'a2'],
                         'a missing curl binary must not abort the rest of the batch')
        self.assertNotEqual(out['exit'], 0)
        self.assertNotIn(f'{PR_CARD[1]} backlog->todo', out['log'])
        self.assertIn(f'{PLAIN_CARD[1]} backlog->todo', out['log'])
        summary = json.loads(out['stdout'].splitlines()[-1])
        self.assertEqual(summary['counts'], {'attempted': 2, 'confirmed': 1,
                                             'definite_failure': 1, 'unknown': 0})
        self.assertEqual(summary['actions'][0]['outcome'], 'definite_failure')
        self.assertEqual(summary['actions'][0]['code'], 'curl_unavailable')


class Timeouts(unittest.TestCase):
    def test_every_subprocess_run_is_bounded(self):
        source = Path(bp.__file__).read_text()
        self.assertGreaterEqual(source.count('subprocess.run('), 3)
        self.assertEqual(untimed_runs(source), [])

    def test_timeouts_are_the_named_constants(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout='OPEN', stderr=''))
        with mock.patch.object(bp.subprocess, 'run', run), mock.patch.dict(bp._pr_cache, clear=True):
            bp.psql('select 1')
            bp.pr_open('two-bot-next', '12')
            bp.api('PATCH', 'issues/a1', {'status': 'todo'})
        self.assertEqual([c.kwargs['timeout'] for c in run.call_args_list],
                         [bp.PSQL_TIMEOUT_S, bp.GH_TIMEOUT_S, bp.GH_TIMEOUT_S, bp.CURL_TIMEOUT_S + 10])
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--max-time') + 1], f'{bp.CURL_TIMEOUT_S:g}')

    def test_env_overrides_a_default_and_bad_values_fail_loudly(self):
        with mock.patch.dict(os.environ, {'BACKLOG_PROMOTER_PSQL_TIMEOUT_S': '12'}):
            self.assertEqual(bp._seconds('BACKLOG_PROMOTER_PSQL_TIMEOUT_S', 60), 12)
        for bad in ('0', '-1', 'nan', 'inf', '3601'):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {'BACKLOG_PROMOTER_PSQL_TIMEOUT_S': bad}):
                with self.assertRaises(SystemExit):
                    bp._seconds('BACKLOG_PROMOTER_PSQL_TIMEOUT_S', 60)


if __name__ == '__main__':
    unittest.main()
