#!/usr/bin/env python3
"""case_4a89a8a2: safety decisions for the connection-intent sweeper.

The sweeper may decline a pending Anthropic connection intent ONLY with
positive per-intent evidence: the row's error text starts with
`claude_auth_required` AND contains a known non-auth signature (stream
disconnect, 429/rate limit, overloaded). Everything else stays untouched.

Pending / old-enough / serviceSlug filtering lives in the SQL query, so this
suite pins those predicates in the issued SQL text as well as the Python-side
evidence gate. All rows are synthetic and every subprocess is stubbed: no DB,
no network, no credentials.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import connection_intent_sweeper as cis

ELIGIBLE_ERR = 'claude_auth_required stream disconnected before completion'

# Every signature in SIGNATURES paired with the required prefix must be
# eligible. The error column is lower()ed in SQL, so cases are lowercase.
ELIGIBLE_ERRORS = [
    'claude_auth_required stream disconnected before completion',
    'claude_auth_required stream error: upstream reset',
    'claude_auth_required stream closed unexpectedly',
    'claude_auth_required 429 too many requests',
    'claude_auth_required rate limit exceeded, retry later',
    'claude_auth_required rate_limit hit on claude endpoint',
    'claude_auth_required overloaded_error: over capacity',
    'claude_auth_required usage limit reached, try later',
]

# Genuine auth failures and unknown evidence: no signature, wrong prefix, or
# empty (the COALESCE default when no failed run is in the evidence window).
INELIGIBLE_ERRORS = [
    '',  # no failed run in the evidence window
    'claude_auth_required not logged in, please run claude login',
    'claude_auth_required 401 unauthorized: invalid credentials',
    'claude_auth_required invalid_grant: token revoked by owner',
    'claude_auth_required something unexpected with no known marker',
    'adapter_failed stream disconnected before completion',  # signature but wrong code
    'adapter_failed claude_auth_required stream disconnected',  # code mid-string, not prefix
    'adapter_failed ENOSPC: no space left on device',
    'model_busy overloaded worker pool',  # signature word but wrong prefix
    'stream disconnected before completion',  # signature but missing prefix
]


def row(iid='intent-1', ident='case_63ac0cf0', agent='agent-1', err=ELIGIBLE_ERR):
    return [iid, ident, agent, err]


def done_process(stdout=''):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr='')


class SqlContract(unittest.TestCase):
    """Pending / old-enough / Anthropic-only filtering lives in SQL."""

    def issued_sql(self):
        seen = []
        with mock.patch.object(cis, 'APPLY', False), \
                mock.patch.object(cis, 'psql', side_effect=lambda sql: seen.append(sql) or []), \
                mock.patch.object(cis.subprocess, 'run',
                                  side_effect=AssertionError('no subprocess in dry run')), \
                contextlib.redirect_stdout(io.StringIO()):
            cis.main()
        self.assertEqual(len(seen), 1)
        # Collapse newlines/runs of whitespace so multi-line predicates pin exactly.
        return ' '.join(seen[0].split())

    def test_query_selects_only_pending_connection_intents(self):
        sql = self.issued_sql()
        self.assertIn("t.kind='connection_intent'", sql)
        self.assertIn("t.status='pending'", sql)

    def test_query_selects_only_the_anthropic_service_slug(self):
        self.assertEqual(cis.SERVICES, ('anthropic',))
        sql = self.issued_sql()
        self.assertIn("'anthropic'", sql)
        self.assertIn('serviceSlug', sql)

    def test_query_enforces_min_age_and_evidence_window(self):
        sql = self.issued_sql()
        self.assertIn(f"t.created_at < now() - interval '{cis.MIN_AGE_MIN} minutes'", sql)
        self.assertIn(f"r.created_at between t.created_at - interval "
                      f"'{cis.EVIDENCE_WINDOW_MIN} minutes' and t.created_at", sql)

    def test_query_pulls_lowercased_code_plus_error(self):
        sql = self.issued_sql()
        self.assertIn('lower(', sql)
        self.assertIn('error_code', sql)

    def test_evidence_comes_from_this_cards_latest_failed_run(self):
        sql = self.issued_sql()
        self.assertIn("r.context_snapshot->>'issueId'=i.id::text", sql)
        self.assertIn("r.status='failed'", sql)
        self.assertIn(f"i.company_id='{cis.CID}'", sql)
        self.assertIn('order by r.created_at desc limit 1', sql)

    def test_prefix_code_mid_string_stays_untouched(self):
        helper = Eligibility('test_empty_evidence_stays_untouched')
        got = helper.run_main(
            [row(err='adapter_failed claude_auth_required stream disconnected')], apply=True)
        self.assertEqual(helper.curl_calls(got['calls']), [])
        self.assertIn('left intent-1', got['stdout'])


class Eligibility(unittest.TestCase):
    def run_main(self, rows, *, apply):
        calls = []
        out = io.StringIO()

        def fake_run(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            return done_process('200')

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'actions.log'
            with mock.patch.object(cis, 'APPLY', apply), \
                    mock.patch.object(cis, 'psql', return_value=[list(r) for r in rows]), \
                    mock.patch.object(cis, 'LOG', log), \
                    mock.patch.object(cis.subprocess, 'run', side_effect=fake_run), \
                    contextlib.redirect_stdout(out):
                cis.main()
            return {'calls': calls, 'stdout': out.getvalue(), 'log_path': log,
                    'log': log.read_text() if log.exists() else ''}

    def curl_calls(self, calls):
        return [(a, k) for a, k in calls if a and a[0] == 'curl']

    def test_each_known_signature_is_eligible(self):
        for err in ELIGIBLE_ERRORS:
            with self.subTest(err=err):
                got = self.run_main([row(err=err)], apply=True)
                self.assertEqual(len(self.curl_calls(got['calls'])), 1)
                self.assertIn('declined intent-1', got['stdout'])
                self.assertIn('case_63ac0cf0:200', got['log'])

    def test_apply_declines_only_the_eligible_row(self):
        rows = [row('eligible-1', 'case_a2c90701', 'agent-1', ELIGIBLE_ERRORS[0])]
        for n, err in enumerate(INELIGIBLE_ERRORS):
            rows.append(row(f'bad-{n}', f'case_120d2a14{n:02d}', 'agent-1', err))
        got = self.run_main(rows, apply=True)
        curls = self.curl_calls(got['calls'])
        self.assertEqual(len(curls), 1)
        argv = curls[0][0]
        # The one decline targets the eligible intent id in the URL.
        self.assertTrue(any('eligible-1' in a for a in argv if isinstance(a, str)))
        for n in range(len(INELIGIBLE_ERRORS)):
            self.assertNotIn(f'bad-{n}', ' '.join(a for a, _ in curls for a in a))
        self.assertIn('case_a2c90701:200', got['log'])
        for n in range(len(INELIGIBLE_ERRORS)):
            self.assertNotIn(f'case_120d2a14{n:02d}', got['log'])

    def test_ineligible_rows_are_left_with_a_reason_and_no_curl(self):
        for err in INELIGIBLE_ERRORS:
            with self.subTest(err=err[:40]):
                got = self.run_main([row(err=err)], apply=True)
                self.assertEqual(self.curl_calls(got['calls']), [])
                self.assertIn('left intent-1 on case_63ac0cf0', got['stdout'])
                self.assertEqual(got['log'], '')
                self.assertFalse(got['log_path'].exists())

    def test_real_auth_errors_stay_untouched(self):
        auth_errors = [e for e in INELIGIBLE_ERRORS if e.startswith('claude_auth_required')]
        self.assertGreaterEqual(len(auth_errors), 3)
        for err in auth_errors:
            with self.subTest(err=err[:40]):
                got = self.run_main([row(err=err)], apply=True)
                self.assertEqual(self.curl_calls(got['calls']), [])
                self.assertIn('no known misclassification evidence', got['stdout'])

    def test_signature_without_prefix_stays_untouched(self):
        got = self.run_main(
            [row(err='adapter_failed stream disconnected before completion')], apply=True)
        self.assertEqual(self.curl_calls(got['calls']), [])
        self.assertIn('left intent-1', got['stdout'])

    def test_empty_evidence_stays_untouched(self):
        got = self.run_main([row(err='')], apply=True)
        self.assertEqual(self.curl_calls(got['calls']), [])
        self.assertIn('left intent-1', got['stdout'])


class DryRunVsApply(unittest.TestCase):
    def test_default_is_dry_run_without_apply_flag(self):
        self.assertNotIn('--apply', sys.argv)
        self.assertFalse(cis.APPLY)

    def test_dry_run_makes_zero_decline_calls_and_writes_no_log(self):
        run = mock.Mock(side_effect=AssertionError('must not run subprocesses'))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'actions.log'
            with mock.patch.object(cis, 'APPLY', False), \
                    mock.patch.object(cis, 'psql', return_value=[row()]), \
                    mock.patch.object(cis, 'LOG', log), \
                    mock.patch.object(cis.subprocess, 'run', run), \
                    contextlib.redirect_stdout(out):
                cis.main()
            self.assertFalse(log.exists())
        run.assert_not_called()
        self.assertIn('would decline intent-1 on case_63ac0cf0', out.getvalue())
        self.assertNotIn('declined intent-1', out.getvalue().replace('would decline', ''))

    def test_dry_run_writes_no_log_even_with_no_eligible_rows(self):
        run = mock.Mock(side_effect=AssertionError('must not run subprocesses'))
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'actions.log'
            with mock.patch.object(cis, 'APPLY', False), \
                    mock.patch.object(cis, 'psql', return_value=[row(err='')]), \
                    mock.patch.object(cis, 'LOG', log), \
                    mock.patch.object(cis.subprocess, 'run', run), \
                    contextlib.redirect_stdout(io.StringIO()):
                cis.main()
            self.assertFalse(log.exists())
        run.assert_not_called()

    def test_apply_calls_only_the_supported_decline_endpoint(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            return done_process('200')

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'actions.log'
            with mock.patch.object(cis, 'APPLY', True), \
                    mock.patch.object(cis, 'psql', return_value=[row()]), \
                    mock.patch.object(cis, 'LOG', log), \
                    mock.patch.object(cis.subprocess, 'run', side_effect=fake_run), \
                    contextlib.redirect_stdout(io.StringIO()):
                cis.main()
        curls = [(a, k) for a, k in calls if a[0] == 'curl']
        self.assertEqual(len(curls), 1)
        self.assertEqual(len(calls), 1, 'apply must issue exactly one subprocess call per eligible row')
        argv, kwargs = curls[0]
        self.assertIn('-X', argv)
        self.assertEqual(argv[argv.index('-X') + 1], 'POST')
        url = [a for a in argv if isinstance(a, str) and a.startswith('http')][0]
        self.assertEqual(url, f'{cis.API}/connection-intents/intent-1/decline')
        self.assertIn('-sK', argv)
        self.assertIn('content-type: application/json', ' '.join(argv).lower())
        payload = argv[argv.index('-d') + 1]
        self.assertIn('CLIProxy', payload)
        self.assertEqual(kwargs.get('timeout'), cis.CURL_TIMEOUT)

    def test_apply_sends_no_other_endpoints(self):
        seen_urls = []

        def fake_run(args, **kwargs):
            seen_urls.extend(a for a in args if isinstance(a, str) and a.startswith('http'))
            return done_process('200')

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cis, 'APPLY', True), \
                    mock.patch.object(cis, 'psql', return_value=[row()]), \
                    mock.patch.object(cis, 'LOG', Path(tmp) / 'actions.log'), \
                    mock.patch.object(cis.subprocess, 'run', side_effect=fake_run), \
                    contextlib.redirect_stdout(io.StringIO()):
                cis.main()
        self.assertEqual(seen_urls, [f'{cis.API}/connection-intents/intent-1/decline'])


class TimeoutAndSecrets(unittest.TestCase):
    def test_timeouts_are_bounded_constants(self):
        self.assertIsInstance(cis.PSQL_TIMEOUT, int)
        self.assertIsInstance(cis.CURL_TIMEOUT, int)
        self.assertGreater(cis.PSQL_TIMEOUT, 0)
        self.assertGreater(cis.CURL_TIMEOUT, 0)

    def test_psql_call_carries_its_timeout(self):
        run = mock.Mock(return_value=done_process('a\tb\n'))
        with mock.patch.object(cis.subprocess, 'run', run):
            self.assertEqual(cis.psql('select 1'), [['a', 'b']])
        self.assertEqual(run.call_args.kwargs.get('timeout'), cis.PSQL_TIMEOUT)

    def test_db_timeout_is_visible_failure_with_no_decline_no_retry_no_secrets(self):
        psql = mock.Mock(side_effect=subprocess.TimeoutExpired('docker', cis.PSQL_TIMEOUT))
        curl = mock.Mock(side_effect=AssertionError('must not decline after a db timeout'))
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'actions.log'
            with mock.patch.object(cis, 'APPLY', True), \
                    mock.patch.object(cis, 'psql', psql), \
                    mock.patch.object(cis, 'LOG', log), \
                    mock.patch.object(cis.subprocess, 'run', curl), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    cis.main()
            self.assertFalse(log.exists())
        self.assertNotEqual(cm.exception.code, 0)
        combined = out.getvalue() + err.getvalue()
        self.assertIn('FAILED', combined)
        self.assertIn('timed out', combined)
        self.assertEqual(psql.call_count, 1, 'db query must not be retried')
        curl.assert_not_called()
        for secret_word in ('token', 'bearer', 'secret', 'password', 'api_key', 'apikey'):
            self.assertNotIn(secret_word, combined.lower())

    def test_decline_timeout_is_visible_failure_with_no_log_write_no_retry_no_secrets(self):
        out = io.StringIO()
        curl = mock.Mock(side_effect=subprocess.TimeoutExpired('curl', cis.CURL_TIMEOUT))
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'actions.log'
            with mock.patch.object(cis, 'APPLY', True), \
                    mock.patch.object(cis, 'psql', return_value=[row()]), \
                    mock.patch.object(cis, 'LOG', log), \
                    mock.patch.object(cis.subprocess, 'run', curl), \
                    contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    cis.main()
            self.assertFalse(log.exists(), 'timed-out declines must not be logged as declined')
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('FAILED decline intent-1', out.getvalue())
        self.assertNotIn('declined intent-1 on', out.getvalue().replace('FAILED decline', ''))
        self.assertEqual(curl.call_count, 1, 'declines must not be retried')
        self.assertNotIn('token', out.getvalue().lower())
        self.assertNotIn('bearer', out.getvalue().lower())

    def test_nonzero_transport_with_stdout_200_never_confirms_a_decline(self):
        for code in (7, 28):
            with self.subTest(returncode=code), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / 'actions.log'
                out, err = io.StringIO(), io.StringIO()
                result = subprocess.CompletedProcess([], code, stdout='200', stderr='raw-stderr-canary')
                with mock.patch.object(cis, 'APPLY', True), \
                        mock.patch.object(sys, 'argv', ['sweeper', '--apply', '--json-summary']), \
                        mock.patch.object(cis, 'psql', return_value=[row()]), \
                        mock.patch.object(cis, 'LOG', log), \
                        mock.patch.object(cis.subprocess, 'run', return_value=result) as run, \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        cis.main()
                self.assertNotEqual(cm.exception.code, 0)
                self.assertEqual(run.call_count, 1)
                self.assertFalse(log.exists())
                self.assertNotIn('declined intent-1 on', out.getvalue())
                self.assertIn('FAILED decline intent-1', out.getvalue())
                self.assertNotIn('raw-stderr-canary', out.getvalue() + err.getvalue())
                summary = json.loads(out.getvalue().splitlines()[-1])
                self.assertEqual(summary['counts']['attempted'], 1)
                self.assertEqual(summary['counts']['confirmed'], 0)
                self.assertIn(summary['actions'][0]['outcome'], {'definite_failure', 'unknown'})

    def test_decline_argv_carries_no_credential_material(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            return done_process('200')

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cis, 'APPLY', True), \
                    mock.patch.object(cis, 'psql', return_value=[row()]), \
                    mock.patch.object(cis, 'LOG', Path(tmp) / 'actions.log'), \
                    mock.patch.object(cis.subprocess, 'run', side_effect=fake_run), \
                    contextlib.redirect_stdout(io.StringIO()):
                cis.main()
        argv = calls[0][0]
        blob = ' '.join(argv).lower()
        self.assertIn('-sk', blob, 'auth must travel via the curl -K file, never inline')
        for secret_word in ('token', 'bearer', 'authorization', 'password', 'secret'):
            self.assertNotIn(secret_word, blob)


if __name__ == '__main__':
    unittest.main()
