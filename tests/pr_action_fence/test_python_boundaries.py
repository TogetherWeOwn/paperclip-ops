"""Real public Python senders, with all external transports replaced below them.

Removing any named final guard must make its denial case fail with a nonzero
mutation count. Existing functions are exercised even before context is added.
"""
import builtins
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
import types
import unittest
from unittest import mock

from support import (ApiSpy, COMMENT, DENY, HEAD, ISSUE, NEW_HEAD, NOW, REPOSITORY,
                     ROOT, captured, contained, context, import_productivity, pr,
                     scratch, with_context)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contained())

    def module(self, name):
        return import_productivity(name)

    def assert_denial(self, value, text=''):
        self.assertIn(DENY, json.dumps(value, default=str) + text,
                      'A refused boundary must produce a review-only decision/HOLD receipt')


class ControllerWakeBoundary(OfflineCase):
    def drive(self, repository=REPOSITORY, number=1, *, head=HEAD, rec=None, ctx=None):
        wakes = self.module('pr_controller_wakes')
        state = copy.deepcopy(rec or {})
        state['pr_context'] = ctx or context(repository, number, head)
        post = ApiSpy()
        result, output = captured(lambda: with_context(
            wakes.post_wake, state, 'draft_idle@' + head, ISSUE,
            'Offline lifecycle action; message content is not authorization.', post,
            pr_context=state['pr_context']))
        return post.calls, state, result, output

    def test_initial_draft_idle_boundary_denies_before_comment(self):
        calls, state, result, output = self.drive()
        self.assertEqual([], calls, 'missing final owner_wake guard: initial comment dispatched')
        self.assertNotIn('pending_wake', state)
        self.assert_denial([state, result], output)

    def test_six_hour_stale_reminder_rechecks_guard(self):
        record = {'woke': 'draft_idle@' + HEAD,
                  'rewake': {'draft_idle@' + HEAD: '2026-10-22T00:00:00Z'}}
        calls, state, result, output = self.drive(rec=record)
        self.assertEqual([], calls, 'missing final owner_wake guard: repeated comment dispatched')
        self.assert_denial([state, result], output)

    def test_delayed_work_created_before_policy_is_not_permission(self):
        record = {'pending_wake': {'key': 'draft_idle@' + HEAD,
                                  'issue_id': ISSUE, 'comment_id': COMMENT},
                  'policy_digest': '0' * 64}
        calls, _, _, _ = self.drive(rec=record)
        self.assertEqual([], calls, 'legacy queued work bypassed the current boundary')

    def test_future_review_and_new_head_never_release_subject(self):
        for head in (HEAD, NEW_HEAD):
            with self.subTest(head=head):
                calls, _, _, _ = self.drive(head=head, ctx=context(
                    head=head, review_score=5, ci_green=True, draft=False))
                self.assertEqual([], calls, 'subject restriction incorrectly expired with head/review')

    def test_mixed_case_and_whitespace_stay_restricted(self):
        calls, _, _, _ = self.drive(repository='  TOGETHERWEOWN/CLIProxyAPI  ')
        self.assertEqual([], calls, 'noncanonical identity bypassed final owner_wake guard')

    def test_invalid_or_missing_context_denies_instead_of_treating_as_other(self):
        for ctx in ({}, context(repository='https://github.com/TogetherWeOwn/CLIProxyAPI'),
                    context(pr_number=True), context(expected_head=None)):
            with self.subTest(context=ctx):
                wakes = self.module('pr_controller_wakes')
                post = ApiSpy()
                with_context(wakes.post_wake, {}, 'draft_idle@' + HEAD, ISSUE, 'fixture', post,
                             pr_context=ctx)
                self.assertEqual([], post.calls, 'invalid/missing trusted context must fail closed')

    def test_unrelated_controls_reach_original_transport(self):
        for repository, number in [('example/other', 1), (REPOSITORY, 2),
                                   (REPOSITORY, 10), (REPOSITORY, 11),
                                   ('TogetherWeOwn/CLIProxyAPI-extra', 1)]:
            with self.subTest(repository=repository, number=number):
                calls, state, _, _ = self.drive(repository, number)
                self.assertEqual(1, len(calls), 'noninterference control must really send')
                self.assertEqual(COMMENT, state['pending_wake']['comment_id'])


class CiAlternateBoundary(OfflineCase):
    def drive(self, repository=REPOSITORY, number=1, attempt=2):
        ci = self.module('pr_controller_ci')
        api = ApiSpy()
        processes, cards = [], []

        def gh(argv):
            processes.append(list(argv))
            return 0, 'offline rerun'

        ledger = types.SimpleNamespace(verdict=lambda *_: 'no', record_flake=lambda _: None)
        check = {'workflow': 'ci.yml', 'job': 'unit-tests', 'test': None,
                 'run_id': 101, 'attempt': attempt, 'conclusion': 'failure',
                 'private_repo': False, 'paid_runner': False}
        with mock.patch.object(ci, 'kill_switch_active', return_value=False):
            result, output = captured(lambda: ci.handle_ci_tick(
                pr=pr(repository, number), checks=[check], state={}, gh=gh, api=api,
                ledger=ledger, quarantine_card=cards.append, wake_text='Offline CI action', now=NOW))
        return api.calls, processes, cards, result, output

    def test_alternate_post_is_guarded_even_without_ordinary_post_wake(self):
        calls, processes, cards, result, output = self.drive(attempt=2)
        self.assertEqual([], calls, 'alternate CI _post dispatched a protected action comment')
        self.assertEqual([], processes)
        self.assertEqual([], cards)
        self.assert_denial(result, output)

    def test_rerun_dispatcher_does_not_trigger_another_review_or_ci_run(self):
        calls, processes, cards, _, _ = self.drive(attempt=1)
        self.assertEqual([], processes, 'protected CI _rerun reached fake gh')
        self.assertEqual([], calls)
        self.assertEqual([], cards)

    def test_direct_alternate_post_has_its_own_guard(self):
        ci = self.module('pr_controller_ci')
        api = ApiSpy()
        with_context(ci._post, api, ISSUE, 'Offline direct CI action', pr_context=context())
        self.assertEqual([], api.calls, 'dispatcher-only guard left direct _post callable unfenced')

    def test_direct_rerun_has_its_own_guard(self):
        ci = self.module('pr_controller_ci')
        calls = []
        with_context(ci._rerun, lambda argv: calls.append(argv) or (0, ''), 101,
                     pr_context=context())
        self.assertEqual([], calls, 'dispatcher-only guard left direct _rerun callable unfenced')

    def test_unrelated_ci_keeps_original_attempt_gates(self):
        calls, processes, _, _, _ = self.drive('example/other', attempt=2)
        self.assertEqual(1, len(calls))
        self.assertEqual([], processes)
        calls, processes, _, _, _ = self.drive('example/other', attempt=1)
        self.assertEqual([], calls)
        self.assertEqual([['gh', 'run', 'rerun', '101', '--failed']], processes)


class SupplyAndReviewBoundary(OfflineCase):
    def supply(self, repository=REPOSITORY, number=1, *, owned=True, repeat=False,
               pending=False, hold=None):
        ps, rr = self.module('pr_supply'), self.module('review_error_rerequest')
        writes = []
        host = types.ModuleType('host_ok')
        host.host_ok = lambda: (True, 'offline fixture')
        fixture = pr(repository, number)
        state = {}
        if repeat:
            state[f'{repository}#{number}@{HEAD}:draft'] = 'fixture 2026-10-21T00:00:00Z'
        if pending:
            state[ps.pending_key(repository, number)] = {
                'card_id': ISSUE, 'identifier': 'fixture', 'pr_url': fixture['url'],
                'first_seen': '2026-10-21T00:00:00Z', 'attempts': 1}

        def api(method, path, body):
            writes.append((method, path, copy.deepcopy(body)))
            return {'id': ISSUE, 'identifier': 'fixture'}

        def runner(argv, **kwargs):
            if argv[0] != 'curl':
                raise AssertionError('unexpected process: supply fake handles curl only')
            writes.append(('curl', list(argv)))
            return types.SimpleNamespace(stdout='200', returncode=0, stderr='')

        with scratch() as tmp, contextlib.ExitStack() as stack:
            for name, value in [('APPLY', True), ('REPOS', {repository.split('/')[1]: ('fixture', 'fixture', 0)}),
                                ('STATE', Path(tmp, 'state.json')), ('HELD', Path(tmp, 'held.txt')),
                                ('PAUSE', Path(tmp, 'PAUSE')), ('LOG', Path(tmp, 'log'))]:
                stack.enter_context(mock.patch.object(ps, name, value))
            ps.STATE.write_text(json.dumps(state))
            stack.enter_context(mock.patch.dict(sys.modules, {'host_ok': host}))
            stack.enter_context(mock.patch.object(ps.time, 'time', return_value=NOW))
            stack.enter_context(mock.patch.object(ps, 'list_prs', return_value=[fixture]))
            stack.enter_context(mock.patch.object(ps, 'owning_card', return_value=[ISSUE, 'fixture', 'in_progress'] if owned else None))
            stack.enter_context(mock.patch.object(ps, 'card_has_pr', return_value=False))
            stack.enter_context(mock.patch.object(ps, 'api', side_effect=api))
            stack.enter_context(mock.patch.object(rr, 'gh_env', return_value={}))
            stack.enter_context(mock.patch.object(rr, 'psql', return_value=[]))
            stack.enter_context(mock.patch.object(rr, 'card_hold', return_value=hold))
            stack.enter_context(mock.patch.object(rr.live_run_guard, 'issue_has_live_work', return_value=False))
            stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=runner))
            result, output = captured(ps.main)
            return writes, result, output

    def test_direct_supply_action_card_writer_cannot_bypass_generation(self):
        ps = self.module('pr_supply')
        writes = []
        with mock.patch.object(subprocess, 'run', side_effect=lambda argv, **_: (
                writes.append(argv) or types.SimpleNamespace(stdout=json.dumps({'id': ISSUE}), returncode=0, stderr=''))):
            with_context(ps.api, 'POST', 'companies/fixture/issues',
                         {'title': 'Offline action', 'status': 'todo', 'assigneeAgentId': 'fixture'},
                         pr_context=context())
        self.assertEqual([], writes, 'supply generator filtering left assigned-card final writer bypassable')

    def test_direct_supply_attachment_writer_cannot_bypass_generation(self):
        ps = self.module('pr_supply')
        writes = []
        with mock.patch.object(subprocess, 'run', side_effect=lambda argv, **_: (
                writes.append(argv) or types.SimpleNamespace(stdout=json.dumps({'id': ISSUE}), returncode=0, stderr=''))):
            with_context(ps.attach_work_product, ISSUE, pr()['url'], REPOSITORY, 1, pr_context=context())
        self.assertEqual([], writes, 'legacy action-card attachment must recheck the current fence')

    def test_supply_initial_owner_comment_is_guarded(self):
        writes, _, _ = self.supply()
        self.assertEqual([], writes, 'supply existing-owner comment reached curl')

    def test_supply_six_hour_same_head_renudge_is_guarded(self):
        writes, _, _ = self.supply(repeat=True)
        self.assertEqual([], writes, 'same-head supply renudge bypassed fence')

    def test_supply_assigned_action_card_and_attachment_are_guarded(self):
        writes, _, _ = self.supply(owned=False)
        self.assertEqual([], writes, 'supply created an assigned action card/work product')

    def test_supply_pending_attachment_from_before_fence_is_guarded(self):
        writes, _, _ = self.supply(owned=False, pending=True)
        self.assertEqual([], writes, 'legacy pending attachment bypassed final fence')

    def test_supply_unrelated_control_sends_but_existing_hold_still_wins(self):
        writes, _, _ = self.supply('TogetherWeOwn/example-control')
        self.assertEqual(1, len(writes))
        writes, _, _ = self.supply('TogetherWeOwn/example-control', hold='blocked')
        self.assertEqual([], writes)

    def test_review_company_wide_error_intake_is_guarded_outside_curated_inventory(self):
        rr = self.module('review_error_rerequest')
        writes = []
        host = types.ModuleType('host_ok')
        host.host_ok = lambda: (True, 'offline fixture')
        with scratch() as tmp, contextlib.ExitStack() as stack:
            for name, value in [('APPLY', True), ('REPOS', ()), ('STATE', Path(tmp, 'state.json')),
                                ('PARKED', Path(tmp, 'parked.txt')), ('LOG', Path(tmp, 'log'))]:
                stack.enter_context(mock.patch.object(rr, name, value))
            stack.enter_context(mock.patch.dict(sys.modules, {'host_ok': host}))
            stack.enter_context(mock.patch.object(rr, 'psql', return_value=[[REPOSITORY, '1', HEAD, 'error']]))
            stack.enter_context(mock.patch.object(rr, 'gh_env', return_value={}))
            stack.enter_context(mock.patch.object(rr, 'pr_head', return_value=('OPEN', HEAD)))
            stack.enter_context(mock.patch.object(rr, 'owning_card', return_value=[ISSUE, 'fixture']))
            stack.enter_context(mock.patch.object(rr, 'unreviewed', return_value=[]))
            stack.enter_context(mock.patch.object(rr, 'merge_ready', return_value=[]))
            stack.enter_context(mock.patch.object(rr, 'card_hold', return_value=None))
            stack.enter_context(mock.patch.object(rr.live_run_guard, 'issue_has_live_work', return_value=False))
            stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=lambda argv, **_: (
                writes.append(argv) or types.SimpleNamespace(stdout='200', returncode=0, stderr=''))))
            captured(rr.main)
        self.assertEqual([], writes, 'company-wide intake delivered a protected review-rerequest comment')

    def test_direct_review_sender_cannot_be_bypassed(self):
        rr = self.module('review_error_rerequest')
        writes = []
        with mock.patch.object(subprocess, 'run', side_effect=lambda argv, **_: (
                writes.append(argv) or types.SimpleNamespace(stdout='200', returncode=0, stderr=''))):
            with_context(rr.comment, ISSUE, 'Offline action', pr_context=context())
        self.assertEqual([], writes, 'raw review action sender lacks its own final guard')


class OrphanBoundary(OfflineCase):
    def test_direct_orphan_action_card_writer_cannot_bypass_discovery(self):
        orphan = self.module('orphan_prs')
        writes = []
        with mock.patch.object(subprocess, 'run', side_effect=lambda argv, **_: (
                writes.append(argv) or types.SimpleNamespace(stdout=json.dumps({'id': ISSUE}), returncode=0, stderr=''))):
            with_context(orphan.api, 'POST', 'companies/fixture/issues',
                         {'title': 'Offline shepherd action', 'status': 'todo', 'assigneeAgentId': 'fixture'},
                         pr_context=context())
        self.assertEqual([], writes, 'orphan inventory omission cannot protect the final card writer')

    def test_orphan_assigned_action_card_cannot_be_created_for_injected_ready_mirror(self):
        self.assertEqual([], self.drive(REPOSITORY), 'orphan final intake created a shepherd-to-merge card')

    def test_unrelated_orphan_positive_control_reaches_card_and_attachment(self):
        self.assertEqual(2, len(self.drive('example/other')))

    def drive(self, repository):
        orphan = self.module('orphan_prs')
        writes = []
        with scratch() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            (root / 'ops/worker-vps').mkdir(parents=True)
            for name, value in [('APPLY', True), ('ROOT', str(root)), ('STATE_DIR', str(root)),
                                ('REPOS', {repository: ('fixture', 'fixture')})]:
                stack.enter_context(mock.patch.object(orphan, name, value))
            stack.enter_context(mock.patch.object(orphan.time, 'time', return_value=NOW))
            stack.enter_context(mock.patch.object(orphan, 'psql', return_value=[]))
            stack.enter_context(mock.patch.object(orphan, 'referenced', return_value=False))
            stack.enter_context(mock.patch.object(orphan, 'api', side_effect=lambda m, p, b: (
                writes.append((m, p, b)) or {'id': ISSUE, 'identifier': 'fixture'})))

            def runner(argv, **kwargs):
                if argv[0].endswith('gh_token.sh'):
                    return types.SimpleNamespace(stdout='', returncode=0, stderr='')
                if argv[:3] != ['gh', 'pr', 'list']:
                    raise AssertionError('unexpected process in orphan intake')
                return types.SimpleNamespace(stdout=json.dumps([pr(repository, isDraft=False)]),
                                             returncode=0, stderr='')

            stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=runner))
            captured(orphan.main)
        return writes


class HygieneBoundary(OfflineCase):
    def drive(self, repository, *, direct=False):
        """Execute the real script; its import-time token command is an inert fake.

        The owner-reference discovery regex is not under test. Substitute a
        synthetic reference so public fixtures contain no internal identifiers.
        Actual choose_owner and actual final PATCH loop remain real.
        """
        writes = []
        number = 1
        detail = {'number': number, 'title': 'Offline fixture', 'draft': False,
                  'mergeable_state': 'clean', 'body': 'Fixture owner reference',
                  'head': {'sha': HEAD, 'ref': 'test/fixture', 'repo': {'full_name': repository}}}
        old_open, old_findall = builtins.open, re.findall

        def fake_open(path, mode='r', *args, **kwargs):
            if Path(path).name == 'gh-inst-token':
                return io.StringIO('')
            return old_open(path, mode, *args, **kwargs)

        def response(request, **kwargs):
            url = request.full_url
            if '/orgs/' in url:
                data = [{'name': repository.split('/')[1]}] if url.endswith('&page=1') and not direct else []
            elif 'pulls?state=' in url:
                data = [detail]
            elif '/pulls/1' in url:
                data = detail
            else:
                raise AssertionError('unexpected hygiene network request')
            return io.BytesIO(json.dumps(data).encode())

        def runner(argv, **kwargs):
            if argv[0] == 'bash':
                return types.SimpleNamespace(stdout='', returncode=0)
            if argv[0] == 'docker':
                return types.SimpleNamespace(stdout=f'{ISSUE}|fixture|blocked|fixture|f', returncode=0)
            if argv[0] != 'curl':
                raise AssertionError('unexpected hygiene process')
            if '-X' in argv:
                writes.append(list(argv))
                payload = {'id': ISSUE}
            else:
                url = next(x for x in argv if x.startswith('http'))
                if url.endswith('/work-products'):
                    payload = [{'type': 'pull_request', 'provider': 'github',
                                'url': f'https://github.com/{repository}/pull/1'}]
                else:
                    payload = {'id': ISSUE}
            return types.SimpleNamespace(stdout=json.dumps(payload), returncode=0)

        # Only file-local state/log output is redirected, never a policy file.
        with scratch() as tmp, contextlib.ExitStack() as stack:
            source = ROOT / 'ops/pr-hygiene/sweep.py'
            stack.enter_context(mock.patch.object(builtins, 'open', side_effect=fake_open))
            stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=runner))
            stack.enter_context(mock.patch('urllib.request.urlopen', side_effect=response))
            stack.enter_context(mock.patch.object(re, 'findall', side_effect=lambda pattern, text, *a, **k:
                ['fixture'] if pattern.endswith(r'-\d+') else old_findall(pattern, text, *a, **k)))
            guard = self.module('live_run_guard')
            stack.enter_context(mock.patch.object(guard, 'issue_has_live_work', return_value=False))
            # Run using the original filename/import resolution, but redirect
            # script-owned files via the file open seam, not a rewritten source.
            old_exists = __import__('os').path.exists
            stack.enter_context(mock.patch('os.path.exists', side_effect=lambda path:
                False if Path(path).name in ('state.json', 'PAUSE') else old_exists(path)))
            original = fake_open

            def routed_open(path, mode='r', *a, **k):
                if Path(path).parent == source.parent and Path(path).name in ('state.json', 'sweep.log'):
                    path = Path(tmp) / Path(path).name
                return original(path, mode, *a, **k)

            stack.enter_context(mock.patch.object(builtins, 'open', side_effect=routed_open))
            namespace, _ = captured(lambda: runpy.run_path(str(source), run_name='offline_hygiene_acceptance'))
            if direct:
                with_context(namespace['api'], 'PATCH', f'issues/{ISSUE}',
                             {'comment': 'Offline lifecycle action', 'status': 'todo'},
                             pr_context=context(repository=repository))
        return writes

    def test_hygiene_direct_final_patch_cannot_bypass_generator(self):
        self.assertEqual([], self.drive(REPOSITORY, direct=True),
                         'generator filtering left the actual hygiene PATCH independently callable')

    def test_hygiene_final_patch_denies_comment_and_status_resume(self):
        self.assertEqual([], self.drive(REPOSITORY), 'hygiene PATCH issued protected comment/status resume')

    def test_unrelated_hygiene_control_really_resumes_and_comments(self):
        writes = self.drive('TogetherWeOwn/example-control')
        self.assertEqual(1, len(writes))
        payload = json.loads(writes[0][writes[0].index('-d') + 1])
        self.assertEqual('todo', payload['status'])
        self.assertIn('comment', payload)


if __name__ == '__main__':
    unittest.main()
