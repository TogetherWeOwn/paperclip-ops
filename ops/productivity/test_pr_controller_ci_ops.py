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
        gh, api = FakeGh(), FakeApi(fail=True)
        outcome = tick(make_pr(), [make_check(workflow='deploy')], {}, gh, api)
        self.assertEqual('suppressed', outcome['action'])
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
