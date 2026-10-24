#!/usr/bin/env python3
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import resolve_outdated_threads as r

HEAD = 'a' * 40
OTHER = 'b' * 40
TRUSTED = r.TRUSTED_REVIEW_APP_ID
FOREIGN = 1234567


def thread(resolved=False, outdated=True, login='togetherweown-reviewer', cid=1):
    return {'id': 'T', 'isResolved': resolved, 'isOutdated': outdated,
            'comments': {'nodes': [{'databaseId': cid, 'author': {'login': login}}]}}


class Candidates(unittest.TestCase):
    def test_outdated_reviewer_thread(self):
        self.assertEqual(len(r.candidates([thread()])), 1)

    def test_not_outdated_never(self):
        self.assertEqual(r.candidates([thread(outdated=False)]), [])

    def test_resolved_skipped(self):
        self.assertEqual(r.candidates([thread(resolved=True)]), [])

    def test_other_author_skipped(self):
        self.assertEqual(r.candidates([thread(login='greptile-apps')]), [])


_MISSING = object()


def cr(started, conclusion, title=_MISSING, name='Paperclip Review', app_id=TRUSTED,
       head=HEAD, status='completed', slug='togetherweown-reviewer', summary=None):
    output = {}
    if title is not _MISSING:
        output['title'] = title
    if summary is not None:
        output['summary'] = summary
    run = {'name': name, 'started_at': started, 'conclusion': conclusion,
           'output': output, 'status': status, 'head_sha': head}
    if app_id is not None:
        run['app'] = {'id': app_id, 'slug': slug}
    return run


class Evidence(unittest.TestCase):
    def test_trusted_positive(self):
        ok, _ = r.review_evidence([cr('2026-10-06T11:00Z', 'success', '5/5')], HEAD)
        self.assertTrue(ok)
        self.assertTrue(r.head_is_5of5([cr('2026-10-06T11:00Z', 'success', '5/5')], HEAD))

    def test_summary_fallback_for_missing_or_empty_title(self):
        for title in (_MISSING, ''):
            with self.subTest(title=title):
                run = cr('2026-10-06T11:00Z', 'success', title,
                         summary='5/5\nAdditional details')
                self.assertTrue(r.head_is_5of5([run], HEAD))

    def test_summary_fallback_tolerates_crlf(self):
        run = cr('2026-10-06T11:00Z', 'success', '',
                 summary='5/5\r\nAdditional details')
        self.assertTrue(r.head_is_5of5([run], HEAD))

    def test_nonempty_title_precedes_summary(self):
        ok, reason = r.review_evidence(
            [cr('2026-10-06T11:00Z', 'success', 'reviewed',
                summary='5/5\nAdditional details')],
            HEAD,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'incomplete_score')

    def test_latest_trusted_wins(self):
        runs = [cr('2026-10-06T10:00Z', 'failure', '3/5'),
                cr('2026-10-06T11:00Z', 'success', '5/5')]
        self.assertTrue(r.head_is_5of5(runs, HEAD))
        stale = [cr('2026-10-06T12:00Z', 'success', '4/5'),
                 cr('2026-10-06T11:00Z', 'success', '5/5')]
        self.assertFalse(r.head_is_5of5(stale, HEAD))

    def test_wrong_app_newer_success_ignored(self):
        runs = [cr('2026-10-06T11:00Z', 'failure', '3/5'),
                cr('2026-10-06T12:00Z', 'success', '5/5', app_id=FOREIGN)]
        ok, _ = r.review_evidence(runs, HEAD)
        self.assertFalse(ok)

    def test_wrong_app_only_refuses(self):
        ok, reason = r.review_evidence([cr('2026-10-06T12:00Z', 'success', '5/5', app_id=FOREIGN)], HEAD)
        self.assertFalse(ok)
        self.assertEqual(reason, 'no_trusted_evidence')

    def test_missing_identity(self):
        ok, reason = r.review_evidence([cr('2026-10-06T12:00Z', 'success', '5/5', app_id=None)], HEAD)
        self.assertFalse(ok)
        self.assertEqual(reason, 'missing_identity')

    def test_missing_expected_head(self):
        ok, reason = r.review_evidence([cr('2026-10-06T12:00Z', 'success', '5/5')], '')
        self.assertFalse(ok)
        self.assertEqual(reason, 'missing_head')

    def test_missing_run_head(self):
        run = cr('2026-10-06T12:00Z', 'success', '5/5')
        del run['head_sha']
        ok, reason = r.review_evidence([run], HEAD)
        self.assertFalse(ok)
        self.assertEqual(reason, 'missing_head')

    def test_wrong_head(self):
        ok, reason = r.review_evidence([cr('2026-10-06T12:00Z', 'success', '5/5', head=OTHER)], HEAD)
        self.assertFalse(ok)
        self.assertEqual(reason, 'wrong_head')

    def test_incomplete_status(self):
        ok, reason = r.review_evidence(
            [cr('2026-10-06T12:00Z', 'success', '5/5', status='in_progress')], HEAD)
        self.assertFalse(ok)
        self.assertEqual(reason, 'incomplete_status')

    def test_incomplete_conclusion(self):
        ok, reason = r.review_evidence(
            [cr('2026-10-06T12:00Z', 'action_required', 'Authorized manual review required')], HEAD)
        self.assertFalse(ok)
        self.assertEqual(reason, 'incomplete_conclusion')

    def test_incomplete_score(self):
        ok, reason = r.review_evidence([cr('2026-10-06T12:00Z', 'success', '4/5')], HEAD)
        self.assertFalse(ok)
        self.assertEqual(reason, 'incomplete_score')

    def test_no_review_check(self):
        ok, _ = r.review_evidence(
            [cr('2026-10-06T12:00Z', 'success', '5/5', name='ci-ok', app_id=15368)], HEAD)
        self.assertFalse(ok)

    def test_pending(self):
        ok, _ = r.review_evidence([cr('2026-10-06T12:00Z', None, 'Waiting', status='queued')], HEAD)
        self.assertFalse(ok)

    def test_app_id_authoritative_over_slug(self):
        run = cr('2026-10-06T12:00Z', 'success', '5/5', slug='renamed-app')
        self.assertTrue(r.head_is_5of5([run], HEAD))


def drive(check_runs, head=HEAD, dry=False):
    """Run one tick against canned PR/check data with all mutations recorded. No network, no threads resolved."""
    repo, num = 'TogetherWeOwn/paperclip-ops', 1
    pr_payload = json.dumps({'data': {'repository': {'pullRequest': {
        'headRefOid': head,
        'reviewThreads': {'nodes': [thread()]}}}}})
    checks_payload = json.dumps({'check_runs': check_runs})
    mutations = []

    def fake_gh(e, *args, input_=None):
        if any('resolveReviewThread' in a for a in args):
            mutations.append('resolve')
            return '{}'
        if any('/replies' in a for a in args):
            mutations.append('reply')
            return '{}'
        if any('check-runs' in a for a in args):
            return checks_payload
        if args[:2] == ('api', 'graphql'):
            return pr_payload
        raise AssertionError(f'unexpected gh call: {args[:4]}')

    tmp = tempfile.TemporaryDirectory()
    patches = [mock.patch.object(r, 'env', lambda: {}),
               mock.patch.object(r, 'open_prs', lambda e: [(repo, num)]),
               mock.patch.object(r, 'gh', fake_gh),
               mock.patch.object(r, 'log'),
               mock.patch.object(r, 'STATE_DIR', Path(tmp.name) / 'state'),
               mock.patch.object(r, 'PAUSE', Path(tmp.name) / 'state' / 'PAUSE')]
    for p in patches:
        p.start()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            report = r.run(dry=dry)
    finally:
        for p in reversed(patches):
            p.stop()
        tmp.cleanup()
    return report, mutations


class RunRefusals(unittest.TestCase):
    def assert_refuses_without_mutations(self, check_runs, head=HEAD):
        report, mutations = drive(check_runs, head=head, dry=False)
        self.assertEqual(mutations, [])
        self.assertTrue(any('skipped' in item for item in report))
        return report

    def test_refuses_wrong_app_newer_success(self):
        self.assert_refuses_without_mutations(
            [cr('2026-10-06T11:00Z', 'failure', '3/5'),
             cr('2026-10-06T12:00Z', 'success', '5/5', app_id=FOREIGN)])

    def test_refuses_missing_identity(self):
        self.assert_refuses_without_mutations([cr('2026-10-06T12:00Z', 'success', '5/5', app_id=None)])

    def test_refuses_missing_head(self):
        self.assert_refuses_without_mutations(
            [cr('2026-10-06T12:00Z', 'success', '5/5')], head='')

    def test_refuses_wrong_head(self):
        self.assert_refuses_without_mutations(
            [cr('2026-10-06T12:00Z', 'success', '5/5', head=OTHER)])

    def test_refuses_incomplete(self):
        self.assert_refuses_without_mutations(
            [cr('2026-10-06T12:00Z', 'action_required', 'Authorized manual review required')])

    def test_trusted_evidence_passes_gate_without_live_mutations(self):
        report, mutations = drive([cr('2026-10-06T11:00Z', 'success', '5/5')], dry=True)
        self.assertEqual(mutations, [])
        self.assertTrue(any(item.get('resolved') == 1 for item in report))


def run_with_helper(behavior, dry=False):
    """Run one tick with the token helper stubbed and gh recorded. No network.

    behavior: 'timeout' | ('exit', N) | 'missing' | 'empty' | ('ok', token).
    Returns (exit_code, stdout_text, gh_calls, mutations, helper_calls).
    """
    repo, num = 'TogetherWeOwn/paperclip-ops', 1
    pr_payload = json.dumps({'data': {'repository': {'pullRequest': {
        'headRefOid': HEAD,
        'reviewThreads': {'nodes': [thread()]}}}}})
    checks_payload = json.dumps({'check_runs': [cr('2026-10-06T11:00Z', 'success', '5/5')]})
    gh_calls, mutations, helper_calls = [], [], []

    def fake_run(cmd, **kwargs):
        helper_calls.append({'cmd': list(cmd), 'timeout': kwargs.get('timeout')})
        if behavior == 'timeout':
            raise subprocess.TimeoutExpired(cmd, kwargs.get('timeout') or 0)
        if behavior == 'missing':
            raise FileNotFoundError('gh_token.sh')
        if isinstance(behavior, tuple) and behavior[0] == 'exit':
            return subprocess.CompletedProcess(cmd, behavior[1], stdout='', stderr='boom')
        if behavior == 'empty':
            return subprocess.CompletedProcess(cmd, 0, stdout='  \n', stderr='')
        if isinstance(behavior, tuple) and behavior[0] == 'ok':
            return subprocess.CompletedProcess(cmd, 0, stdout=behavior[1] + '\n', stderr='')
        raise AssertionError(f'unknown helper behavior: {behavior!r}')

    def fake_gh(e, *args, input_=None):
        gh_calls.append({'args': list(args), 'env': dict(e)})
        if any('resolveReviewThread' in a for a in args):
            mutations.append('resolve')
            return '{}'
        if any('/replies' in a for a in args):
            mutations.append('reply')
            return '{}'
        if any('check-runs' in a for a in args):
            return checks_payload
        if args[:2] == ('api', 'graphql'):
            return pr_payload
        raise AssertionError(f'unexpected gh call: {args[:4]}')

    tmp = tempfile.TemporaryDirectory()
    patches = [mock.patch.object(r.subprocess, 'run', fake_run),
               mock.patch.object(r, 'gh', fake_gh),
               mock.patch.object(r, 'open_prs', lambda e: [(repo, num)]),
               mock.patch.object(r, 'log'),
               mock.patch.object(r, 'STATE_DIR', Path(tmp.name) / 'state'),
               mock.patch.object(r, 'PAUSE', Path(tmp.name) / 'state' / 'PAUSE'),
               mock.patch.dict(os.environ)]
    for p in patches:
        p.start()
    os.environ.pop('GH_TOKEN', None)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            try:
                r.run(dry=dry)
                code = 0
            except SystemExit as s:
                code = s.code
    finally:
        for p in reversed(patches):
            p.stop()
        tmp.cleanup()
    return code, out.getvalue(), gh_calls, mutations, helper_calls


class TokenHelperRefusal(unittest.TestCase):
    """case_8567b436: token-helper failure refuses with zero gh calls, non-zero exit."""

    def test_helper_timeout_refuses_without_gh(self):
        code, out, gh_calls, mutations, _ = run_with_helper('timeout')
        self.assertNotEqual(code, 0)
        self.assertEqual(gh_calls, [])
        self.assertEqual(mutations, [])
        self.assertIn('token helper: timeout', out)

    def test_helper_exit_refuses_without_gh(self):
        code, out, gh_calls, mutations, _ = run_with_helper(('exit', 1))
        self.assertNotEqual(code, 0)
        self.assertEqual(gh_calls, [])
        self.assertEqual(mutations, [])
        self.assertIn('token helper: exit 1', out)

    def test_helper_empty_refuses_without_gh(self):
        code, out, gh_calls, mutations, _ = run_with_helper('empty')
        self.assertNotEqual(code, 0)
        self.assertEqual(gh_calls, [])
        self.assertEqual(mutations, [])
        self.assertIn('token helper: empty', out)

    def test_helper_missing_refuses_without_gh(self):
        code, out, gh_calls, mutations, _ = run_with_helper('missing')
        self.assertNotEqual(code, 0)
        self.assertEqual(gh_calls, [])
        self.assertEqual(mutations, [])
        self.assertIn('token helper: exit 127', out)

    def test_helper_call_is_bounded(self):
        _, _, _, _, helper_calls = run_with_helper(('ok', 'SECRET-TOKEN'))
        self.assertTrue(helper_calls)
        for h in helper_calls:
            self.assertEqual(h['timeout'], r.TOKEN_TIMEOUT)

    def test_success_token_only_in_child_env(self):
        token = 'SECRET-TOKEN-XYZ'
        code, out, gh_calls, mutations, _ = run_with_helper(('ok', token))
        self.assertEqual(code, 0)
        self.assertTrue(gh_calls)
        for c in gh_calls:
            self.assertEqual(c['env'].get('GH_TOKEN'), token)
            self.assertNotIn(token, ' '.join(c['args']))
        self.assertNotIn(token, out)
        self.assertNotEqual(os.environ.get('GH_TOKEN'), token)

    def test_success_still_resolves(self):
        _, _, _, mutations, _ = run_with_helper(('ok', 'SECRET-TOKEN'))
        self.assertEqual(mutations, ['reply', 'resolve'])


if __name__ == '__main__':
    unittest.main()
