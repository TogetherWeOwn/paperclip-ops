"""Builder-owned tests: kill switch, shadow dry-run, and eligibility edges.

The acceptance contract lives in test_pr_controller_ci.py (Test Author owned,
never edited here). This file pins the operations surface the host install
relies on: the kill switch, the log-only shadow path, and the never-rerun
fail-safes for malformed check rows.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pr_controller_ci as ci


T0 = 1790000000
OWNER = '00000000-0000-4000-8000-000000000001'
WAKE_TEXT = '**CI red** on this head (CI-RED-WAKE-STUB).'


def make_pr(head='a' * 40):
    return {'repo': 'two-bot-next', 'number': 7, 'head_sha': head,
            'owner_card_id': OWNER}


def make_check(workflow='check.yml', job='rust tests', run_id=101,
               attempt=1, conclusion='failure', test=None,
               private=False, paid=False):
    return {'workflow': workflow, 'job': job, 'test': test, 'run_id': run_id,
            'attempt': attempt, 'conclusion': conclusion,
            'private_repo': private, 'paid_runner': paid}


class FakeGh:
    def __init__(self, fail=False):
        self.argv_calls = []
        self.fail = fail

    def __call__(self, argv):
        self.argv_calls.append(list(argv))
        if self.fail:
            raise OSError('gh is down')
        return (0, 'rerun requested')


class FakeApi:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, method, path, payload):
        self.calls.append((method, path, dict(payload)))
        if self.fail:
            raise OSError('api is down')
        return ('201', json.dumps({'id': 'comment-1'}))


class FakeLedger:
    def __init__(self):
        self.recorded = []

    def verdict(self, repo, key):
        return 'no'

    def record_flake(self, entry):
        self.recorded.append(dict(entry))


def tick(pr, checks, state, gh, api, now=T0, **kwargs):
    return ci.handle_ci_tick(
        pr=pr, checks=checks, state=state, gh=gh, api=api,
        ledger=FakeLedger(), quarantine_card=lambda payload: 'card-1',
        wake_text=WAKE_TEXT, now=now, **kwargs)


class KillSwitchTest(unittest.TestCase):
    def test_env_kill_switch_wakes_on_first_red_without_rerun(self):
        with mock.patch.dict(os.environ, {'PR_CONTROLLER_CI_DISABLE': '1'},
                             clear=False):
            gh, api = FakeGh(), FakeApi()
            outcome = tick(make_pr(), [make_check()], {}, gh, api)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual([], gh.argv_calls)
        self.assertEqual(1, len(api.calls))
        self.assertEqual(WAKE_TEXT, api.calls[0][2]['body'])

    def test_flag_file_kill_switch_wakes_on_first_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp, 'disable')
            flag.write_text('stop\n', encoding='utf-8')
            with mock.patch.dict(
                    os.environ,
                    {'PR_CONTROLLER_CI_DISABLE_FILE': str(flag)},
                    clear=False):
                gh, api = FakeGh(), FakeApi()
                outcome = tick(make_pr(), [make_check()], {}, gh, api)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual([], gh.argv_calls)
        self.assertEqual(1, len(api.calls))

    def test_kill_switch_still_reports_green(self):
        with mock.patch.dict(os.environ, {'PR_CONTROLLER_CI_DISABLE': 'true'},
                             clear=False):
            gh, api = FakeGh(), FakeApi()
            outcome = tick(make_pr(),
                           [make_check(conclusion='success')], {}, gh, api)
        self.assertEqual('green', outcome['action'])
        self.assertEqual([], gh.argv_calls)
        self.assertEqual([], api.calls)


class DryRunTest(unittest.TestCase):
    def test_dry_run_decides_without_side_effects_or_state(self):
        gh, api = FakeGh(), FakeApi()
        state = {}
        outcome = tick(make_pr(), [make_check()], state, gh, api, dry_run=True)
        self.assertEqual('reran', outcome['action'])
        self.assertTrue(outcome['dry_run'])
        self.assertEqual([], gh.argv_calls)
        self.assertEqual([], api.calls)
        self.assertEqual({}, state)

    def test_dry_run_would_wake_ineligible_without_posting(self):
        gh, api = FakeGh(), FakeApi()
        state = {}
        outcome = tick(make_pr(), [make_check(workflow='deploy')],
                       state, gh, api, dry_run=True)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual([], api.calls)
        self.assertEqual({}, state)

    def test_dry_run_leaves_nonempty_nested_state_untouched(self):
        gh, api = FakeGh(), FakeApi()
        live = {}
        tick(make_pr(), [make_check(run_id=101)], live, gh, api)
        self.assertIn('101', live['flights'])
        before = json.loads(json.dumps(live))
        # A concluded retry in dry-run must not pop the live flight.
        outcome = tick(make_pr(), [make_check(run_id=101, attempt=2)],
                       live, gh, api, dry_run=True)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual(before, live)
        self.assertIn('101', live['flights'])
        self.assertEqual([], api.calls)


class SharedFlakeStoreTest(unittest.TestCase):
    def flake_tick(self, pr, run_id, state, shared, now, ledger, cards):
        red = make_check(run_id=run_id)
        ci.handle_ci_tick(pr=pr, checks=[red], state=state, gh=FakeGh(),
                          api=FakeApi(), ledger=ledger,
                          quarantine_card=cards.append, wake_text=WAKE_TEXT,
                          now=now, shared=shared)
        green = make_check(run_id=run_id, attempt=2, conclusion='success')
        return ci.handle_ci_tick(
            pr=pr, checks=[green], state=state, gh=FakeGh(), api=FakeApi(),
            ledger=ledger, quarantine_card=cards.append, wake_text=WAKE_TEXT,
            now=now + 600, shared=shared)

    def test_flakes_aggregate_across_prs_into_one_card(self):
        ledger, cards = FakeLedger(), []
        shared = {}
        state_a, state_b = {}, {}
        outcome = self.flake_tick(make_pr('a' * 40), 101, state_a, shared,
                                  T0, ledger, cards)
        self.assertEqual('green', outcome['action'])
        self.assertEqual([], cards)
        self.flake_tick(make_pr('b' * 40), 102, state_b, shared,
                        T0 + 600, ledger, cards)
        self.assertEqual([], cards)
        # Third flake, back on PR A: the shared window reaches 3.
        shared = json.loads(json.dumps(shared))  # restart-proof
        self.flake_tick(make_pr('c' * 40), 103, state_a, shared,
                        T0 + 1200, ledger, cards)
        self.assertEqual(1, len(cards))
        self.assertIn('[flake]', cards[0]['title'])
        self.assertEqual(['101', '102', '103'], cards[0]['run_ids'])
        # The window lives in the shared store, not in either PR record.
        self.assertEqual(3, len(shared['flakes']))
        self.assertNotIn('flakes', state_a)
        self.assertNotIn('flakes', state_b)

    def test_rounds_and_flights_stay_per_pr(self):
        ledger, cards = FakeLedger(), []
        shared = {}
        state_a, state_b = {}, {}
        gh, api = FakeGh(), FakeApi()
        tick(make_pr('a' * 40), [make_check(run_id=101)], state_a, gh, api,
             shared=shared)
        tick(make_pr('a' * 40), [make_check(run_id=101, attempt=2)],
             state_a, gh, api, shared=shared)
        self.assertEqual(1, state_a['round_count'])
        self.assertNotIn('round_count', state_b)
        self.assertNotIn('round_count', shared)
        self.assertNotIn('flights', shared)
        self.assertNotIn('handled', shared)

    def test_pending_flakes_flush_from_shared_on_another_pr_tick(self):
        ledger, cards = FakeLedger(), []

        class OutageLedger(FakeLedger):
            def record_flake(self, entry):
                raise OSError('ledger down')

        failing = OutageLedger()
        shared = {}
        state_a, state_b = {}, {}
        self.flake_tick(make_pr('a' * 40), 101, state_a, shared,
                        T0, failing, cards)
        self.assertEqual(1, len(shared['pending_flakes']))
        self.assertNotIn('pending_flakes', state_a)
        outcome = self.flake_tick(make_pr('b' * 40), 102, state_b, shared,
                                  T0 + 600, ledger, cards)
        self.assertEqual('green', outcome['action'])
        self.assertEqual(2, len(ledger.recorded))
        self.assertEqual([], shared['pending_flakes'])


class EligibilityEdgeTest(unittest.TestCase):
    def test_malformed_rows_wake_without_rerun(self):
        for check in (
                make_check(workflow=None),
                make_check(workflow='check.yml', job=None),
                make_check(run_id=None),
                make_check(attempt=0),
                make_check(attempt='1'),
                make_check(attempt=None),
        ):
            with self.subTest(check=check):
                self.assertFalse(ci.is_eligible(check))
                gh, api = FakeGh(), FakeApi()
                outcome = tick(make_pr(), [check], {}, gh, api)
                self.assertEqual('woke', outcome['action'])
                self.assertEqual([], gh.argv_calls)

    def test_case_insensitive_deny_catches_codeql_and_deploy(self):
        for workflow, job in (('check.yml', 'CodeQL'),
                              ('Deploy to production', 'tests')):
            with self.subTest(workflow=workflow, job=job):
                self.assertFalse(
                    ci.is_eligible(make_check(workflow, job)))

    def test_workflow_path_basename_is_accepted(self):
        check = make_check('.github/workflows/check.yml')
        self.assertTrue(ci.is_eligible(check))

    def test_broken_gh_degrades_to_wake_not_silence(self):
        gh, api = FakeGh(fail=True), FakeApi()
        outcome = tick(make_pr(), [make_check()], {}, gh, api)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual(1, len(api.calls))

    def test_failed_post_stays_retryable(self):
        # A failed post is NOT suppressed: the host must fall through to its
        # normal wake path (which owns the retry) instead of dropping the wake.
        gh, api = FakeGh(), FakeApi(fail=True)
        state = {}
        outcome = tick(make_pr(), [make_check(workflow='deploy')], state,
                       gh, api)
        self.assertEqual('post_failed', outcome['action'])
        self.assertEqual([], gh.argv_calls)
        self.assertNotIn('comment_id', outcome)
        self.assertEqual([], state.get('handled', []))

    def test_failed_wake_post_after_rerun_is_post_failed(self):
        gh, api = FakeGh(), FakeApi()
        state = {}
        tick(make_pr(), [make_check(run_id=101)], state, gh, api)
        api.fail = True
        outcome = tick(make_pr(),
                       [make_check(run_id=101, attempt=2)], state, gh, api)
        self.assertEqual('post_failed', outcome['action'])
        # The retry re-decides instead of staying silent: the flight is
        # gone, so the same red wakes through the ineligible path.
        api.fail = False
        outcome = tick(make_pr(),
                       [make_check(run_id=101, attempt=2)], state, gh, api)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual(2, len(api.calls))  # one failed, one delivered

    def test_failed_cap_post_redecides_next_pass(self):
        gh, api = FakeGh(), FakeApi()
        state = {}
        for head, run_id in (('a' * 40, 101), ('b' * 40, 102)):
            pr = make_pr(head)
            tick(pr, [make_check(run_id=run_id)], state, gh, api)
            tick(pr, [make_check(run_id=run_id, attempt=2)], state, gh, api)
        pr = make_pr('c' * 40)
        tick(pr, [make_check(run_id=103)], state, gh, api)
        api.fail = True
        outcome = tick(pr, [make_check(run_id=103, attempt=2)], state,
                       gh, api)
        self.assertEqual('post_failed', outcome['action'])
        self.assertEqual(2, state['round_count'])
        self.assertNotIn('capped_head', state)
        # The retry fails safe to a normal wake (the flight is gone, so the
        # same red wakes through the ineligible path instead of capping).
        api.fail = False
        outcome = tick(pr, [make_check(run_id=103, attempt=2)], state,
                       gh, api)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual(2, state['round_count'])
        # ... and the next red head still caps (without the decrement it
        # would conclude as round 4 and the cap comment would never post).
        pr = make_pr('d' * 40)
        tick(pr, [make_check(run_id=104)], state, gh, api)
        outcome = tick(pr, [make_check(run_id=104, attempt=2)], state,
                       gh, api)
        self.assertEqual('capped', outcome['action'])
        self.assertEqual('d' * 40, state['capped_head'])

    def test_failed_kill_switch_post_is_post_failed(self):
        with mock.patch.dict(os.environ, {'PR_CONTROLLER_CI_DISABLE': '1'},
                             clear=False):
            gh, api = FakeGh(), FakeApi(fail=True)
            outcome = tick(make_pr(), [make_check()], {}, gh, api)
        self.assertEqual('post_failed', outcome['action'])
        self.assertEqual([], gh.argv_calls)

    def test_quarantine_card_failure_does_not_crash_tick(self):
        gh, api = FakeGh(), FakeApi()
        ledger = FakeLedger()
        state = {}

        def boom(payload):
            raise OSError('cards are down')

        for head, run_id in (('a' * 40, 101), ('b' * 40, 102),
                             ('c' * 40, 103)):
            pr = make_pr(head)
            red = make_check(run_id=run_id)
            ci.handle_ci_tick(pr=pr, checks=[red], state=state, gh=gh,
                              api=api, ledger=ledger,
                              quarantine_card=boom, wake_text=WAKE_TEXT,
                              now=T0)
            green = make_check(run_id=run_id, conclusion='success', attempt=2)
            outcome = ci.handle_ci_tick(
                pr=pr, checks=[green], state=state, gh=gh, api=api,
                ledger=ledger, quarantine_card=boom, wake_text=WAKE_TEXT,
                now=T0 + 600)
            self.assertEqual('green', outcome['action'])
        self.assertEqual(3, len(ledger.recorded))


if __name__ == '__main__':
    unittest.main()
