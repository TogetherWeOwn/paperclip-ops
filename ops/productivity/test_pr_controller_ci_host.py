"""Builder-owned tests: host glue for the ci_red rerun policy.

Pins checks_from_detail shapes, cost annotation, shadow null-sinks, the
operator-log UNDO lines, and the fail-safe 'proceed' direction.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pr_controller_ci_host as host

T0 = 1790000000
OWNER = '00000000-0000-4000-8000-000000000001'
WAKE_TEXT = '**CI red** on this head (CI-RED-WAKE-STUB).'
HEAD_A = 'a' * 40


class FakeGh:
    """Routes gh api lookups; records rerun requests."""

    def __init__(self, private=False, self_hosted=False, fail_api=False):
        self.private = private
        self.self_hosted = self_hosted
        self.fail_api = fail_api
        self.reruns = []
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ['gh', 'run', 'rerun']:
            self.reruns.append(list(argv))
            return (0, 'rerun requested')
        if self.fail_api:
            return (1, 'boom')
        if argv[1:2] == ['api'] and argv[2].endswith('/jobs'):
            labels = ['self-hosted'] if self.self_hosted else ['ubuntu-latest']
            return (0, json.dumps([labels]))
        if argv[1:2] == ['api']:
            return (0, json.dumps(self.private))
        raise AssertionError(f'unexpected gh call: {argv}')


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, dict(payload)))
        return ('201', json.dumps({'id': 'comment-1'}))


class FakeLedger:
    def __init__(self):
        self.recorded = []

    def verdict(self, repo, key):
        return 'no'

    def record_flake(self, entry):
        self.recorded.append(dict(entry))


class Harness:
    def __init__(self, gh=None):
        self.gh = gh or FakeGh()
        self.api = FakeApi()
        self.ledger = FakeLedger()
        self.cards = []
        self.logs = []
        self.actions = []

    def run(self, detail, rec=None, shadow=False, head=HEAD_A,
              shared=None):
        return host.handle_ci_red(
            full='TogetherWeOwn/two-bot-next',
            pr={'number': 7, 'headRefOid': head},
            detail=detail, rec={} if rec is None else rec,
            owner_card_id=OWNER, wake_text=WAKE_TEXT,
            api=self.api, gh=self.gh, ledger=self.ledger,
            quarantine_card=self.cards.append,
            log=self.logs.append, log_action=self.actions.append,
            shadow=shadow, now=T0, shared=shared)

    def reset(self, detail, rec=None, shadow=False, head=HEAD_A,
              shared=None):
        return host.handle_ci_reset(
            full='TogetherWeOwn/two-bot-next',
            pr={'number': 7, 'headRefOid': head},
            detail=detail, rec={} if rec is None else rec,
            ledger=self.ledger, quarantine_card=self.cards.append,
            log=self.logs.append, shadow=shadow, now=T0, shared=shared)


def detail_row(**overrides):
    row = {'workflow': 'check.yml', 'job': 'rust tests',
           'run_id': 101, 'attempt': 1, 'conclusion': 'failure'}
    row.update(overrides)
    return row


class DetailShapeTest(unittest.TestCase):
    def test_checks_and_failing_aliases_parse(self):
        for key in ('checks', 'failing_checks', 'failing'):
            checks, reason = host.checks_from_detail(
                {key: [detail_row()]})
            self.assertEqual('ok', reason)
            self.assertEqual(1, len(checks))
            self.assertEqual(101, checks[0]['run_id'])

    def test_alternate_key_names_parse(self):
        checks, reason = host.checks_from_detail({'checks': [{
            'workflow_file': '.github/workflows/ci.yml', 'name': 'tests',
            'id': '4242', 'run_attempt': '1', 'state': 'FAILED'}]})
        self.assertEqual('ok', reason)
        self.assertEqual('.github/workflows/ci.yml', checks[0]['workflow'])
        self.assertEqual('tests', checks[0]['job'])
        self.assertEqual(4242, checks[0]['run_id'])
        self.assertEqual(1, checks[0]['attempt'])
        self.assertEqual('failure', checks[0]['conclusion'])

    def test_unconcluded_and_unusable_rows_are_dropped(self):
        checks, reason = host.checks_from_detail({'checks': [
            detail_row(conclusion='in_progress'),
            detail_row(conclusion='cancelled'),
            {'workflow': 'check.yml', 'job': 'x'},
            'not-a-row',
        ]})
        self.assertEqual([], checks)
        self.assertIn('no concluded', reason)

    def test_missing_or_misshapen_detail_explains(self):
        for detail, fragment in ((None, 'not an object'),
                                 ({}, 'no checks'),
                                 ({'checks': {}}, 'not a list')):
            _, reason = host.checks_from_detail(detail)
            self.assertIn(fragment, reason)


class CostAnnotationTest(unittest.TestCase):
    def test_public_repo_is_never_paid(self):
        checks = host.annotate_cost(
            [host._normalize_check(detail_row())], gh=FakeGh(private=False),
            full='o/r', log=lambda line: None)
        self.assertEqual((False, False),
                         (checks[0]['private_repo'], checks[0]['paid_runner']))

    def test_private_self_hosted_is_not_paid(self):
        checks = host.annotate_cost(
            [host._normalize_check(detail_row())],
            gh=FakeGh(private=True, self_hosted=True),
            full='o/r', log=lambda line: None)
        self.assertEqual((True, False),
                         (checks[0]['private_repo'], checks[0]['paid_runner']))

    def test_private_hosted_is_paid(self):
        checks = host.annotate_cost(
            [host._normalize_check(detail_row())],
            gh=FakeGh(private=True, self_hosted=False),
            full='o/r', log=lambda line: None)
        self.assertEqual((True, True),
                         (checks[0]['private_repo'], checks[0]['paid_runner']))

    def test_failed_lookup_degrades_to_paid(self):
        logs = []
        checks = host.annotate_cost(
            [host._normalize_check(detail_row())],
            gh=FakeGh(fail_api=True), full='o/r', log=logs.append)
        self.assertEqual((True, True),
                         (checks[0]['private_repo'], checks[0]['paid_runner']))


class LivePathTest(unittest.TestCase):
    def test_first_red_reruns_and_skips_wake_with_undo_log(self):
        env = Harness()
        rec = {}
        verdict = env.run({'checks': [detail_row()]}, rec=rec)
        self.assertEqual('handled', verdict)
        self.assertEqual([['gh', 'run', 'rerun', '101', '--failed']],
                         env.gh.reruns)
        self.assertEqual([], env.api.calls)
        self.assertEqual(1, len(env.actions))
        self.assertIn('ci-policy reran', env.actions[0])
        self.assertIn('UNDO', env.actions[0])
        self.assertIn('ci_policy', rec)

    def test_unknown_detail_shape_wakes_as_today(self):
        env = Harness()
        verdict = env.run({'pending': True})
        self.assertEqual('proceed', verdict)
        self.assertEqual([], env.gh.reruns)
        self.assertTrue(any('no check rows' in line for line in env.logs))

    def test_glue_never_raises(self):
        env = Harness()
        verdict = env.run(None, rec=None)
        self.assertEqual('proceed', verdict)

    def test_failed_post_proceeds_to_the_normal_wake_path(self):
        env = Harness()

        def failing_api(method, path, payload):
            raise OSError('api is down')

        env.api = failing_api
        rec = {}
        verdict = env.run({'checks': [detail_row(workflow='deploy')]},
                          rec=rec)
        self.assertEqual('proceed', verdict)
        self.assertIsNone(rec['ci_policy']['tick_comment_id'])
        self.assertTrue(any('post failed' in line for line in env.logs))

    def test_tick_comment_id_is_fresh_every_tick(self):
        env = Harness()
        rec = {}
        env.run({'checks': [detail_row(run_id=101)]}, rec=rec)
        self.assertIsNone(rec['ci_policy']['tick_comment_id'])
        verdict = env.run(
            {'checks': [detail_row(run_id=101, attempt=2)]}, rec=rec)
        self.assertEqual('handled', verdict)
        self.assertEqual('comment-1',
                         rec['ci_policy']['tick_comment_id'])
        # A later quiet tick clears the id; the patch never sees a stale one.
        verdict = env.run(
            {'checks': [detail_row(run_id=101, attempt=2)]}, rec=rec)
        self.assertEqual('handled', verdict)
        self.assertIsNone(rec['ci_policy']['tick_comment_id'])

    def test_shared_store_carries_flakes_across_prs(self):
        env = Harness()
        shared = {}
        for head, run_id in ((HEAD_A, 101), ('b' * 40, 102),
                             ('c' * 40, 103)):
            rec = {}
            env.run({'checks': [detail_row(run_id=run_id)]}, rec=rec,
                    head=head, shared=shared)
            verdict = env.run(
                {'checks': [detail_row(run_id=run_id, attempt=2,
                                       conclusion='success')]},
                rec=rec, head=head, shared=shared)
            self.assertEqual('handled', verdict)
            self.assertNotIn('flakes', rec['ci_policy'])
        self.assertEqual(3, len(shared['flakes']))
        self.assertEqual(1, len(env.cards))
        self.assertIn('[flake]', env.cards[0]['title'])


class ShadowPathTest(unittest.TestCase):
    def test_shadow_logs_would_reran_and_always_proceeds(self):
        env = Harness()
        rec = {}
        verdict = env.run({'checks': [detail_row()]}, rec=rec, shadow=True)
        self.assertEqual('proceed', verdict)
        self.assertEqual([], env.gh.reruns)
        self.assertEqual([], env.api.calls)
        self.assertEqual([], env.cards)
        self.assertEqual([], env.ledger.recorded)
        self.assertEqual(1, len(env.actions))
        self.assertIn('would-reran', env.actions[0])
        self.assertIn('runs=101', env.actions[0])
        self.assertIn('ci_policy_shadow', rec)
        self.assertNotIn('ci_policy', rec)

    def test_shadow_repeat_tick_logs_would_suppressed(self):
        env = Harness()
        rec = {}
        env.run({'checks': [detail_row()]}, rec=rec, shadow=True)
        verdict = env.run({'checks': [detail_row()]}, rec=rec, shadow=True)
        self.assertEqual('proceed', verdict)
        self.assertEqual(2, len(env.actions))
        self.assertIn('would-suppressed', env.actions[1])

    def test_shadow_without_rows_logs_would_wake(self):
        env = Harness()
        verdict = env.run({'pending': True}, shadow=True)
        self.assertEqual('proceed', verdict)
        self.assertEqual(1, len(env.actions))
        self.assertIn('would-wake', env.actions[0])

    def test_shadow_flakes_stay_namespaced_off_the_live_window(self):
        env = Harness()
        shared = {'flakes': [{'repo': 'r', 'workflow': 'w', 'job': 'j',
                              'test': 't', 'ts': T0, 'run_id': 1}]}
        rec = {}
        env.run({'checks': [detail_row(run_id=101)]}, rec=rec, shadow=True,
                shared=shared)
        env.run({'checks': [detail_row(run_id=101, attempt=2,
                                       conclusion='success')]},
                rec=rec, shadow=True, shared=shared)
        self.assertEqual(1, len(shared['flakes']))
        self.assertEqual(1, len(shared['shadow']['flakes']))


class ResetPassTest(unittest.TestCase):
    def test_green_reset_clears_rounds_and_records_pass_flake(self):
        env = Harness()
        rec = {}
        verdict = env.run({'checks': [detail_row(run_id=101)]}, rec=rec)
        self.assertEqual('handled', verdict)
        self.assertEqual(1, len(env.gh.reruns))
        verdict = env.reset(
            {'checks': [detail_row(run_id=101, attempt=2,
                                   conclusion='success')]},
            rec=rec)
        self.assertEqual('proceed', verdict)
        # The headline case: the retry passed, so one flake is recorded
        # and the host wake still proceeds (the reset never suppresses).
        self.assertEqual(1, len(env.ledger.recorded))
        self.assertEqual('rust tests', env.ledger.recorded[0]['job'])
        self.assertEqual(0, rec['ci_policy']['round_count'])
        self.assertEqual([], rec['ci_policy']['rounds'])
        self.assertEqual({}, rec['ci_policy']['flights'])
        self.assertEqual(1, len(env.gh.reruns))
        self.assertEqual([], env.api.calls)
        self.assertEqual([], env.cards)
        self.assertTrue(any('reset rounds/cap' in line
                            for line in env.logs))

    def test_capped_record_resets_then_reruns_again(self):
        env = Harness()
        rec = {}
        for head, run_id in ((HEAD_A, 101), ('b' * 40, 102),
                             ('c' * 40, 103)):
            env.run({'checks': [detail_row(run_id=run_id)]}, rec=rec,
                    head=head)
            env.run({'checks': [detail_row(run_id=run_id, attempt=2)]},
                    rec=rec, head=head)
        self.assertEqual(3, rec['ci_policy']['round_count'])
        self.assertEqual('c' * 40, rec['ci_policy']['capped_head'])
        verdict = env.reset(
            {'checks': [detail_row(run_id=104, attempt=1,
                                   conclusion='success')]},
            rec=rec, head='d' * 40)
        self.assertEqual('proceed', verdict)
        self.assertEqual(0, rec['ci_policy']['round_count'])
        self.assertNotIn('capped_head', rec['ci_policy'])
        # The next red head reruns fresh instead of staying capped.
        verdict = env.run({'checks': [detail_row(run_id=105)]}, rec=rec,
                           head='e' * 40)
        self.assertEqual('handled', verdict)
        self.assertEqual(4, len(env.gh.reruns))

    def test_reset_with_red_rows_leaves_state(self):
        env = Harness()
        rec = {}
        env.run({'checks': [detail_row(run_id=101)]}, rec=rec)
        before = json.loads(json.dumps(rec['ci_policy']))
        verdict = env.reset(
            {'checks': [detail_row(run_id=101, attempt=2)]}, rec=rec)
        self.assertEqual('proceed', verdict)
        self.assertEqual(before, rec['ci_policy'])
        self.assertFalse(any('reset rounds/cap' in line
                             for line in env.logs))

    def test_reset_with_no_rows_proceeds(self):
        env = Harness()
        verdict = env.reset({'pending': True}, rec={})
        self.assertEqual('proceed', verdict)

    def test_reset_never_raises(self):
        env = Harness()
        verdict = env.reset(None, rec=None)
        self.assertEqual('proceed', verdict)

    def test_reset_under_kill_switch_skips(self):
        env = Harness()
        rec = {'ci_policy': {'round_count': 3, 'rounds': [{}, {}, {}],
                             'capped_head': HEAD_A, 'flights': {}}}
        with mock.patch.dict(os.environ, {'PR_CONTROLLER_CI_DISABLE': '1'},
                             clear=False):
            verdict = env.reset(
                {'checks': [detail_row(run_id=104, attempt=1,
                                       conclusion='success')]},
                rec=rec)
        self.assertEqual('proceed', verdict)
        # The switch freezes policy state; ignoring (not clearing) the cap
        # is what restores wake-on-first-red while it is set.
        self.assertEqual(3, rec['ci_policy']['round_count'])
        self.assertEqual(HEAD_A, rec['ci_policy']['capped_head'])
        self.assertTrue(any('kill switch holds' in line
                            for line in env.logs))

    def test_reset_shadow_resets_shadow_key_only(self):
        env = Harness()
        rec = {}
        env.run({'checks': [detail_row(run_id=101)]}, rec=rec, shadow=True)
        env.run({'checks': [detail_row(run_id=101, attempt=2)]}, rec=rec,
                shadow=True)
        self.assertEqual(1, rec['ci_policy_shadow']['round_count'])
        verdict = env.reset(
            {'checks': [detail_row(run_id=101, attempt=2,
                                   conclusion='success')]},
            rec=rec, shadow=True)
        self.assertEqual('proceed', verdict)
        self.assertEqual(0, rec['ci_policy_shadow']['round_count'])
        self.assertNotIn('ci_policy', rec)
        self.assertEqual([], env.ledger.recorded)
        self.assertTrue(any('shadow reset pass' in line
                            for line in env.logs))

    def test_shadow_active_flag(self):
        with mock.patch.dict(os.environ, {'CI_POLICY_SHADOW': '1'},
                             clear=False):
            self.assertTrue(host.shadow_active())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(host.shadow_active())


class DefaultWiringTest(unittest.TestCase):
    def test_default_gh_refuses_non_gh(self):
        with self.assertRaises(ValueError):
            host.default_gh(['rm', '-rf', '/'])

    def test_default_gh_runs_and_reports_exit(self):
        with mock.patch('subprocess.run') as run:
            run.return_value.returncode = 3
            run.return_value.stdout = 'out'
            run.return_value.stderr = 'err'
            code, text = host.default_gh(['gh', 'api', 'x'])
        self.assertEqual(3, code)
        self.assertIn('out', text)
        self.assertIn('err', text)

    def test_log_action_appends_timestamped_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, 'operator-actions.log')
            with mock.patch.dict(os.environ, {'OPERATOR_ACTIONS_LOG': str(target)},
                                 clear=False):
                host.default_log_action('ci-policy reran o/r#7 run 1')
            body = target.read_text(encoding='utf-8')
        self.assertIn('ci-policy reran o/r#7 run 1', body)
        self.assertRegex(body, r'^\d{4}-\d{2}-\d{2}T')

    def test_log_action_never_raises(self):
        with mock.patch.dict(os.environ, {'OPERATOR_ACTIONS_LOG': '/proc/nope/x.log'},
                             clear=False):
            host.default_log_action('unwritable but harmless')

    def test_find_card_matches_exact_flake_title(self):
        seen = []

        def search(method, path, payload):
            seen.append((method, path))
            return ('200', json.dumps({'issues': [
                {'id': 'card-9', 'title': '[flake] x'},
                {'id': 'card-1', 'title': 'other'}]}))

        find = host.default_find_card(search, 'company-1')
        self.assertEqual('card-9', find({'title': '[flake] x'}))
        self.assertIsNone(find({'title': '[flake] missing'}))
        self.assertTrue(all(method == 'GET' and
                            path.startswith('companies/company-1/issues')
                            for method, path in seen))

    def test_find_card_failure_fails_open_to_none(self):
        def boom(method, path, payload):
            raise OSError('down')

        find = host.default_find_card(boom, 'company-1')
        self.assertIsNone(find({'title': '[flake] x'}))
        find = host.default_find_card(FakeApi(), '')
        self.assertIsNone(find({'title': '[flake] x'}))

    def test_default_deps_reads_shadow_env(self):
        with mock.patch.dict(os.environ, {'CI_POLICY_SHADOW': '1',
                                          'PAPERCLIP_COMPANY_ID': 'c1'},
                             clear=False):
            deps = host.default_deps(api=FakeApi(), log=lambda line: None)
        self.assertTrue(deps['shadow'])
        self.assertTrue(callable(deps['quarantine_card']))
        self.assertTrue(callable(deps['gh']))
        with mock.patch.dict(os.environ, {}, clear=True):
            deps = host.default_deps(api=FakeApi(), log=lambda line: None)
        self.assertFalse(deps['shadow'])


class OutcomeStateTest(unittest.TestCase):
    def test_cap_records_capped_head_and_reports_comment_id(self):
        import pr_controller_ci as ci
        gh, api = FakeGh(), FakeApi()
        ledger = FakeLedger()
        state = {}
        for head, run_id in (('a' * 40, 101), ('b' * 40, 102),
                             ('c' * 40, 103)):
            pr = {'repo': 'two-bot-next', 'number': 7, 'head_sha': head,
                  'owner_card_id': OWNER}
            red = {'workflow': 'check.yml', 'job': 't', 'test': None,
                   'run_id': run_id, 'attempt': 1, 'conclusion': 'failure',
                   'private_repo': False, 'paid_runner': False}
            ci.handle_ci_tick(pr=pr, checks=[red], state=state, gh=gh,
                              api=api, ledger=ledger,
                              quarantine_card=lambda payload: 'k',
                              wake_text=WAKE_TEXT, now=T0)
            red2 = dict(red, attempt=2)
            outcome = ci.handle_ci_tick(
                pr=pr, checks=[red2], state=state, gh=gh, api=api,
                ledger=ledger, quarantine_card=lambda payload: 'k',
                wake_text=WAKE_TEXT, now=T0 + 600)
        self.assertEqual('capped', outcome['action'])
        self.assertEqual('comment-1', outcome['comment_id'])
        self.assertEqual('c' * 40, state['capped_head'])
        # The helper reports the id on the outcome; the host glue owns the
        # per-tick stash (tick_comment_id), so no persistent copy lingers.
        self.assertNotIn('last_outcome', state)
        self.assertNotIn('tick_comment_id', state)


if __name__ == '__main__':
    unittest.main()
