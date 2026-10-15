#!/usr/bin/env python3
"""Offline: a fake subprocess.run stands in for psql, gh_token.sh, gh and curl. No host, network or credential."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import backlog_promoter as bp
from test_review_error_rerequest import host_ok_stub, timed_out, untimed_runs

PR_CARD = ('a1', 'case_698e8eb7', 'Land PR #12 lint fix', 'high', 'Founding Engineer', 'idle')
PLAIN_CARD = ('a2', 'case_a08f133a', 'Add retry to the deploy step', 'medium', 'Founding Engineer', 'idle')


class Run(unittest.TestCase):
    def run_promoter(self, *, rows=(PR_CARD, PLAIN_CARD), runnable='0', pr_state='OPEN', slow=()):
        """slow: which reads time out, from {'runnable', 'rows', 'token', 'pr', 'patch'}."""
        patches, calls = [], []

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
                kind, out = 'patch', '200'
                patches.append(cmd[cmd.index('-X') + 2].rsplit('/', 1)[1])
            if kind in slow:
                if kind == 'patch':
                    patches.pop()
                timed_out(cmd, **kwargs)
            return types.SimpleNamespace(stdout=out + '\n')

        stdout, error = io.StringIO(), None
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, 'operator.log')
            log.write_text('')
            with mock.patch.dict(sys.modules, {'host_ok': host_ok_stub()}), \
                    mock.patch.dict(bp._pr_cache, clear=True), \
                    mock.patch.object(bp, 'APPLY', True), \
                    mock.patch.object(bp, 'LOG', log), \
                    mock.patch.object(bp, 'PAUSE', Path(tmp, 'PAUSE')), \
                    mock.patch.object(bp.subprocess, 'run', side_effect=run), \
                    contextlib.redirect_stdout(stdout):
                try:
                    bp.main()
                except Exception as e:
                    error = e
            return {'patches': patches, 'calls': calls, 'log': log.read_text(), 'stdout': stdout.getvalue(),
                    'error': error}

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

    def test_timed_out_patch_is_logged_and_the_run_goes_on(self):
        out = self.run_promoter(slow={'patch'})
        self.assertIsNone(out['error'])
        self.assertIn('case_698e8eb7 backlog->todo [TWO Bot Next/high] assignee Founding Engineer HTTP timeout', out['log'])
        self.assertIn('case_a08f133a backlog->todo', out['log'])


class Timeouts(unittest.TestCase):
    def test_every_subprocess_run_is_bounded(self):
        source = Path(bp.__file__).read_text()
        self.assertGreaterEqual(source.count('subprocess.run('), 3)
        self.assertEqual(untimed_runs(source), [])

    def test_timeouts_are_the_named_constants(self):
        run = mock.Mock(return_value=types.SimpleNamespace(stdout='OPEN'))
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
