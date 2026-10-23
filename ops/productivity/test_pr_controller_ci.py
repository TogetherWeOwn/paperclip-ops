"""Acceptance tests: rerun one red CI job, record flakes, cap CI-red rounds at 3.

Paired test file for the build task that adds ``ops/productivity/pr_controller_ci.py``.
RED until that helper lands (``import pr_controller_ci`` fails); GREEN after.
The paired build task must NOT edit this file to make it pass -- implement the
helper so the behaviour below holds.

Contract under test
-------------------
``pr_controller_ci.handle_ci_tick`` is called once per controller pass for one PR::

    outcome = handle_ci_tick(pr=..., checks=..., state=...,
                             gh=..., api=..., ledger=...,
                             quarantine_card=..., wake_text=..., now=...)

* ``pr``: ``{'repo', 'number', 'head_sha', 'owner_card_id'}``.
* ``checks``: required checks on this head, each
  ``{'workflow', 'job', 'test' (or None), 'run_id', 'attempt', 'conclusion',
  'private_repo', 'paid_runner'}`` with ``conclusion`` one of
  ``'success'`` / ``'failure'`` (pending checks never reach this helper).
* ``state``: an opaque, JSON-serializable dict the helper owns. The controller
  persists it between passes; every tick below round-trips it through JSON to
  prove a restart loses nothing.
* ``gh``: callable taking an argv list, ``gh(['gh', 'run', 'rerun', ...])``.
  Reruns must request exactly ``gh run rerun <id> --failed``.
* ``api``: callable ``api(method, path, payload)`` following the
  ``pr_controller_wakes.post_wake`` convention. Wakes and cap comments are
  ``POST issues/<owner-card-id>/comments`` with ``{'body': text}``.
* ``ledger``: ``verdict(repo, job_or_test)`` returning ``'yes'`` / ``'no'`` /
  ``'unavailable'``, and ``record_flake(entry)``. Either may fail when the
  ledger is down; the helper must never let that escape the tick.
* ``quarantine_card``: callable taking a payload dict for ``[flake]`` cards.
* ``wake_text``: the caller-rendered ``ci_red`` body ("as today"). The helper
  must post it verbatim and must never reword it.
* ``now``: epoch seconds, used for the 7-day flake window.
* ``outcome``: a dict with at least ``'action'``, one of ``'reran'``,
  ``'woke'``, ``'capped'``, ``'suppressed'`` (this tick requested no rerun
  and posted nothing: round 4+, a rerun already in flight, or a repeat of an
  already-handled tick) or ``'green'`` (all required checks green; rounds
  reset).

Behaviour pinned below (numbers: rerun once, 3 rounds, 3 flakes, 7 days):

* First red on an eligible test-only workflow at attempt 1 reruns once and
  posts no wake. Eligibility comes from the ``CI_WORKFLOWS`` allow-list, read
  from ``flake_ledger`` when it defines one, else from ``pr_controller_ci``.
* A rerun that passes on the same head records one flake entry with exactly
  the keys ``repo``, ``workflow``, ``job``, ``test``, ``run_id``, ``attempt``
  and ``head_sha``. Extra keys are allowed; missing keys fail.
* A rerun that fails wakes exactly once with the verbatim ``wake_text`` and
  counts one CI round. The same run is never rerun again.
* Security / supply-chain / policy jobs, deploy / migrate / drill / smoke
  workflows, paid-hosted runs on private repos, and attempts >= 2 wake as
  today without any rerun. Deny matching is case-insensitive: a security
  exclusion that misses ``CodeQL`` because of case would be a safety hole.
* The 3rd red round (3rd distinct head, each red after its rerun) posts no
  author wake. Instead exactly one owner-card comment asks the author to
  split the PR, or to fix or quarantine the failing test, and lists the 3
  heads, the failing checks and the ledger verdicts. Rounds 4+ post nothing
  (reruns continue: the cap silences posts, not the per-run rerun rule).
  Any all-green required-check result resets the counter.
* 3+ recorded flakes for one (repo, workflow, job/test) within a rolling 7
  days call ``quarantine_card`` exactly once (the card title contains
  ``[flake]``); older flakes do not count.
* Repeating any tick after a JSON restart changes nothing: no second rerun,
  wake, cap comment or flake entry.

Run with ``python3 -m pytest ops/productivity/test_pr_controller_ci.py`` where
pytest is available (pure unittest style, no pytest import), or
``(cd ops/productivity && python3 -m unittest -v test_pr_controller_ci)``.
No network, no tokens: ``gh`` and the Paperclip API are fakes.
"""

import json
import unittest

import pr_controller_ci as ci

try:
    from flake_ledger import CI_WORKFLOWS
except ImportError:
    try:
        CI_WORKFLOWS = ci.CI_WORKFLOWS
    except AttributeError:
        raise ImportError(
            'neither flake_ledger nor pr_controller_ci defines CI_WORKFLOWS; '
            'the build task must add the test-only workflow allow-list'
        )

T0 = 1790000000
DAY = 24 * 3600

OWNER = '00000000-0000-4000-8000-000000000001'
COMMENT = '00000000-0000-4000-8000-000000000002'
WAKE_TEXT = '**CI red** on this head (CI-RED-WAKE-STUB): one required check failed.'

HEAD_A = 'a' * 40
HEAD_B = 'b' * 40
HEAD_C = 'c' * 40
HEAD_D = 'd' * 40
HEAD_E = 'e' * 40
HEAD_F = 'f' * 40


def eligible_workflow():
    """One workflow the implementation itself declares test-only and rerunnable."""
    names = sorted(CI_WORKFLOWS)
    if not names:
        raise AssertionError(
            'CI_WORKFLOWS allow-list is empty; the helper must declare at '
            'least one test-only workflow'
        )
    return names[0]


def make_pr(head, repo='two-bot-next', number=7, owner=OWNER):
    return {'repo': repo, 'number': number, 'head_sha': head, 'owner_card_id': owner}


def make_check(workflow, job, run_id, attempt=1, conclusion='failure',
               test=None, private=False, paid=False):
    return {'workflow': workflow, 'job': job, 'test': test, 'run_id': run_id,
            'attempt': attempt, 'conclusion': conclusion,
            'private_repo': private, 'paid_runner': paid}


class FakeGh:
    """Captures argv; reruns must spell exactly ``gh run rerun <id> --failed``."""

    def __init__(self):
        self.argv_calls = []

    def __call__(self, argv):
        self.argv_calls.append(list(argv))
        return (0, 'rerun requested')


class FakeApi:
    """Paperclip API fake using the post_wake (method, path, payload) convention."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, dict(payload)))
        return ('201', json.dumps({'id': COMMENT}))

    def comments_to(self, card_id):
        return [payload for method, path, payload in self.calls
                if method == 'POST' and path == f'issues/{card_id}/comments']

    def wakes(self):
        return [payload for payload in self.comments_to(OWNER)
                if payload.get('body') == WAKE_TEXT]


class SimulatedLedgerOutage(Exception):
    """The test ledger is down; the helper must absorb this, never crash."""


class FakeLedger:
    def __init__(self):
        self.verdicts = {}
        self.available = True
        self.recorded = []
        self.verdict_calls = []

    def verdict(self, repo, key):
        self.verdict_calls.append((repo, key))
        if not self.available:
            return 'unavailable'
        return self.verdicts.get((repo, key), 'no')

    def record_flake(self, entry):
        if not self.available:
            raise SimulatedLedgerOutage('ledger unavailable')
        self.recorded.append(dict(entry))


class RaisingLedger(FakeLedger):
    def verdict(self, repo, key):
        raise SimulatedLedgerOutage('ledger unavailable')

    def record_flake(self, entry):
        raise SimulatedLedgerOutage('ledger unavailable')


class FakeCards:
    """quarantine_card fake with a stable id, as the real dedup returns."""

    def __init__(self):
        self.calls = []

    def __call__(self, payload):
        self.calls.append(dict(payload))
        return 'flake-card-1'


class CITickCase(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGh()
        self.api = FakeApi()
        self.ledger = FakeLedger()
        self.cards = FakeCards()

    def fresh(self):
        self.setUp()

    def drive(self, *, pr, checks, state, now):
        """One controller pass, with a JSON restart before it and after it."""
        state = json.loads(json.dumps(state))
        outcome = ci.handle_ci_tick(
            pr=pr, checks=checks, state=state, gh=self.gh, api=self.api,
            ledger=self.ledger, quarantine_card=self.cards,
            wake_text=WAKE_TEXT, now=now)
        self.assertIsInstance(outcome, dict)
        self.assertIn('action', outcome)
        json.dumps(state)  # state must stay persistable after every tick
        return outcome, state


class FirstRedTest(CITickCase):
    def test_first_red_reruns_once_and_posts_no_wake(self):
        wf = eligible_workflow()
        pr = make_pr(HEAD_A)
        red = make_check(wf, 'rust tests', 101, attempt=1)
        outcome, _ = self.drive(pr=pr, checks=[red], state={}, now=T0)
        self.assertEqual('reran', outcome['action'])
        self.assertEqual([['gh', 'run', 'rerun', '101', '--failed']],
                         self.gh.argv_calls)
        self.assertEqual([], self.api.calls)
        self.assertEqual([], self.cards.calls)
        self.assertEqual([], self.ledger.recorded)


class RerunOutcomeTest(CITickCase):
    def test_rerun_pass_records_one_flake_and_stays_quiet(self):
        wf = eligible_workflow()
        pr = make_pr(HEAD_A)
        red = make_check(wf, 'rust tests', 101, attempt=1, test='sample_test')
        outcome, state = self.drive(pr=pr, checks=[red], state={}, now=T0)
        self.assertEqual('reran', outcome['action'])
        green = make_check(wf, 'rust tests', 101, attempt=2,
                           conclusion='success', test='sample_test')
        outcome, state = self.drive(pr=pr, checks=[green], state=state,
                                    now=T0 + 600)
        self.assertEqual('green', outcome['action'])
        self.assertEqual([], self.api.calls)
        self.assertEqual(1, len(self.gh.argv_calls))  # never rerun again
        self.assertEqual(1, len(self.ledger.recorded))
        entry = self.ledger.recorded[0]
        self.assertEqual('two-bot-next', entry['repo'])
        self.assertEqual(wf, entry['workflow'])
        self.assertEqual('rust tests', entry['job'])
        self.assertEqual('sample_test', entry['test'])
        self.assertEqual(101, entry['run_id'])
        self.assertEqual(2, entry['attempt'])
        self.assertEqual(HEAD_A, entry['head_sha'])
        # A later pass on the same green head records nothing more.
        outcome, _ = self.drive(pr=pr, checks=[green], state=state,
                                now=T0 + 1200)
        self.assertEqual('green', outcome['action'])
        self.assertEqual(1, len(self.ledger.recorded))
        self.assertEqual([], self.api.calls)
        self.assertEqual(1, len(self.gh.argv_calls))

    def test_rerun_fail_wakes_once_verbatim_and_never_reruns_again(self):
        wf = eligible_workflow()
        pr = make_pr(HEAD_A)
        red1 = make_check(wf, 'rust tests', 101, attempt=1)
        outcome, state = self.drive(pr=pr, checks=[red1], state={}, now=T0)
        self.assertEqual('reran', outcome['action'])
        red2 = make_check(wf, 'rust tests', 101, attempt=2)
        outcome, _ = self.drive(pr=pr, checks=[red2], state=state,
                                now=T0 + 600)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual(1, len(self.gh.argv_calls))
        wakes = self.api.wakes()
        self.assertEqual(1, len(wakes))
        self.assertEqual(WAKE_TEXT, wakes[0]['body'])
        self.assertEqual(1, len(self.api.calls))


class NeverRerunTest(CITickCase):
    def assert_wakes_without_rerun(self, check):
        pr = make_pr(HEAD_A)
        outcome, _ = self.drive(pr=pr, checks=[check], state={}, now=T0)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual([], self.gh.argv_calls)
        self.assertEqual(1, len(self.api.calls))
        self.assertEqual(WAKE_TEXT, self.api.wakes()[0]['body'])

    def test_security_and_policy_jobs_wake_without_rerun(self):
        jobs = ['gitleaks', 'Gitleaks secret scan', 'dependency-audit',
                'npm-audit', 'zizmor', 'CodeQL', 'supply-chain']
        for job in jobs:
            with self.subTest(job=job):
                self.fresh()
                self.assert_wakes_without_rerun(
                    make_check(eligible_workflow(), job, 101, attempt=1))

    def test_deploy_like_workflows_wake_without_rerun(self):
        workflows = ['deploy', 'Deploy to production', 'migrate', 'drill', 'smoke']
        for workflow in workflows:
            with self.subTest(workflow=workflow):
                self.fresh()
                self.assert_wakes_without_rerun(
                    make_check(workflow, 'rust tests', 101, attempt=1))

    def test_private_paid_runs_wake_but_other_combos_rerun(self):
        wf = eligible_workflow()
        # Private repo + paid GitHub-hosted runner: wake, never rerun.
        self.assert_wakes_without_rerun(
            make_check(wf, 'rust tests', 101, attempt=1, private=True, paid=True))
        # Either condition alone stays eligible.
        for private, paid in ((True, False), (False, True)):
            with self.subTest(private=private, paid=paid):
                self.fresh()
                pr = make_pr(HEAD_A)
                check = make_check(wf, 'rust tests', 101, attempt=1,
                                   private=private, paid=paid)
                outcome, _ = self.drive(pr=pr, checks=[check], state={},
                                        now=T0)
                self.assertEqual('reran', outcome['action'])
                self.assertEqual(1, len(self.gh.argv_calls))
                self.assertEqual([], self.api.calls)

    def test_attempt_two_or_more_wakes_without_rerun(self):
        wf = eligible_workflow()
        for attempt in (2, 3):
            with self.subTest(attempt=attempt):
                self.fresh()
                self.assert_wakes_without_rerun(
                    make_check(wf, 'rust tests', 101, attempt=attempt))


class RoundCapTest(CITickCase):
    ROUNDS = ((HEAD_A, 'tests-a', 101), (HEAD_B, 'tests-b', 102),
              (HEAD_C, 'tests-c', 103))

    def full_red_round(self, head, job, run_id, state, now, verdicts=None):
        """Attempt-1 red (rerun) then attempt-2 red; return (outcome, state)."""
        if verdicts is not None:
            self.ledger.verdicts.update(verdicts)
        wf = eligible_workflow()
        pr = make_pr(head)
        red1 = make_check(wf, job, run_id, attempt=1)
        outcome, state = self.drive(pr=pr, checks=[red1], state=state, now=now)
        self.assertEqual('reran', outcome['action'])
        red2 = make_check(wf, job, run_id, attempt=2)
        outcome, state = self.drive(pr=pr, checks=[red2], state=state,
                                    now=now + 600)
        return outcome, state

    def test_third_red_round_posts_owner_comment_instead_of_wake(self):
        state = {}
        verdicts = {('two-bot-next', 'tests-a'): 'yes',
                    ('two-bot-next', 'tests-b'): 'no',
                    ('two-bot-next', 'tests-c'): 'unavailable'}
        self.ledger.verdicts.update(verdicts)
        for head, job, run_id in self.ROUNDS[:2]:
            outcome, state = self.full_red_round(head, job, run_id, state, T0)
            self.assertEqual('woke', outcome['action'])
        self.assertEqual(2, len(self.api.wakes()))
        head, job, run_id = self.ROUNDS[2]
        outcome, _ = self.full_red_round(head, job, run_id, state, T0)
        self.assertEqual('capped', outcome['action'])
        comments = self.api.comments_to(OWNER)
        self.assertEqual(3, len(comments))
        cap = comments[2]['body']
        self.assertNotEqual(WAKE_TEXT, cap)
        lowered = cap.lower()
        for token in ('split', 'fix', 'quarantine'):
            self.assertIn(token, lowered)
        for pinned_head in (HEAD_A, HEAD_B, HEAD_C):
            self.assertIn(pinned_head[:8], cap)
        for pinned_job in ('tests-a', 'tests-b', 'tests-c'):
            self.assertIn(pinned_job, cap)
        for verdict in ('yes', 'no', 'unavailable'):
            self.assertRegex(cap, r'(?i)\b%s\b' % verdict)
        # The author wake count is unchanged: rounds 1-2 only.
        self.assertEqual(2, len(self.api.wakes()))

    def test_rounds_four_and_up_post_nothing_but_keep_rerunning(self):
        state = {}
        for head, job, run_id in self.ROUNDS:
            outcome, state = self.full_red_round(head, job, run_id, state, T0)
        self.assertEqual('capped', outcome['action'])
        self.assertEqual(3, len(self.api.calls))
        reruns = len(self.gh.argv_calls)
        # Round 4 still reruns (the cap silences posts, not reruns), then hushes.
        wf = eligible_workflow()
        pr = make_pr(HEAD_D)
        red1 = make_check(wf, 'tests-d', 104, attempt=1)
        outcome, state = self.drive(pr=pr, checks=[red1], state=state, now=T0)
        self.assertEqual('reran', outcome['action'])
        self.assertEqual(reruns + 1, len(self.gh.argv_calls))
        self.assertEqual(3, len(self.api.calls))
        red2 = make_check(wf, 'tests-d', 104, attempt=2)
        outcome, state = self.drive(pr=pr, checks=[red2], state=state,
                                    now=T0 + 600)
        self.assertEqual('suppressed', outcome['action'])
        self.assertEqual(3, len(self.api.calls))
        self.assertEqual(reruns + 1, len(self.gh.argv_calls))
        # A later head that flakes still records its flake, quietly.
        pr = make_pr(HEAD_E)
        red = make_check(wf, 'tests-e', 105, attempt=1)
        outcome, state = self.drive(pr=pr, checks=[red], state=state, now=T0)
        self.assertEqual('reran', outcome['action'])
        green = make_check(wf, 'tests-e', 105, attempt=2,
                           conclusion='success')
        outcome, _ = self.drive(pr=pr, checks=[green], state=state,
                                now=T0 + 600)
        self.assertEqual('green', outcome['action'])
        self.assertEqual(1, len(self.ledger.recorded))
        self.assertEqual(3, len(self.api.calls))
        self.assertEqual(2, len(self.api.wakes()))

    def test_all_green_resets_the_round_counter(self):
        state = {}
        for head, job, run_id in self.ROUNDS[:2]:
            outcome, state = self.full_red_round(head, job, run_id, state, T0)
            self.assertEqual('woke', outcome['action'])
        wf = eligible_workflow()
        pr = make_pr(HEAD_C)
        green = make_check(wf, 'tests-c', 103, attempt=1,
                           conclusion='success')
        outcome, state = self.drive(pr=pr, checks=[green], state=state, now=T0)
        self.assertEqual('green', outcome['action'])
        # Without the reset these would be rounds 3 (cap), 4 and 5 (silence).
        for head, job, run_id in ((HEAD_D, 'tests-d', 104),
                                 (HEAD_E, 'tests-e', 105)):
            outcome, state = self.full_red_round(head, job, run_id, state, T0)
            self.assertEqual('woke', outcome['action'])
        outcome, _ = self.full_red_round(HEAD_F, 'tests-f', 106, state, T0)
        self.assertEqual('capped', outcome['action'])
        self.assertEqual(4, len(self.api.wakes()))
        comments = self.api.comments_to(OWNER)
        self.assertEqual(5, len(comments))
        cap = comments[4]['body']
        for pinned_head in (HEAD_D, HEAD_E, HEAD_F):
            self.assertIn(pinned_head[:8], cap)
        # Pre-reset rounds are gone from the evidence, not just the count.
        self.assertNotIn(HEAD_A[:8], cap)
        self.assertNotIn(HEAD_B[:8], cap)


class FlakeCardTest(CITickCase):
    def flake_cycle(self, head, run_id, state, now, job='rust tests'):
        """One red-then-green cycle on its own head; a flake is recorded."""
        wf = eligible_workflow()
        pr = make_pr(head)
        red = make_check(wf, job, run_id, attempt=1)
        outcome, state = self.drive(pr=pr, checks=[red], state=state, now=now)
        self.assertEqual('reran', outcome['action'])
        green = make_check(wf, job, run_id, attempt=2,
                           conclusion='success')
        outcome, state = self.drive(pr=pr, checks=[green], state=state,
                                    now=now + 600)
        self.assertEqual('green', outcome['action'])
        return pr, green, state

    def test_three_flakes_in_seven_days_creates_exactly_one_flake_card(self):
        wf = eligible_workflow()
        state = {}
        heads = ((HEAD_A, 101, T0), (HEAD_B, 102, T0 + DAY),
                 (HEAD_C, 103, T0 + 2 * DAY))
        for head, run_id, now in heads[:2]:
            _, _, state = self.flake_cycle(head, run_id, state, now)
            self.assertEqual([], self.cards.calls)
        _, green, state = self.flake_cycle(*heads[2][:2], state, heads[2][2])
        self.assertEqual(3, len(self.ledger.recorded))
        self.assertEqual(1, len(self.cards.calls))
        payload = self.cards.calls[0]
        self.assertIn('[flake]', payload['title'])
        blob = json.dumps(payload)
        for token in ('two-bot-next', wf, 'rust tests'):
            self.assertIn(token, blob)
        # Repeating the green pass does not card twice.
        pr = make_pr(HEAD_C)
        outcome, _ = self.drive(pr=pr, checks=[green], state=state,
                                now=T0 + 3 * DAY)
        self.assertEqual('green', outcome['action'])
        self.assertEqual(1, len(self.cards.calls))
        self.assertEqual(3, len(self.ledger.recorded))

    def test_flakes_outside_seven_days_do_not_card(self):
        state = {}
        for head, run_id, now in ((HEAD_A, 101, T0), (HEAD_B, 102, T0 + DAY),
                                 (HEAD_C, 103, T0 + 9 * DAY)):
            _, _, state = self.flake_cycle(head, run_id, state, now)
        self.assertEqual(3, len(self.ledger.recorded))
        self.assertEqual([], self.cards.calls)


class IdempotencyTest(CITickCase):
    def test_repeated_red_tick_reruns_only_once(self):
        wf = eligible_workflow()
        pr = make_pr(HEAD_A)
        red = make_check(wf, 'rust tests', 101, attempt=1)
        state = {}
        actions = []
        for _ in range(3):
            outcome, state = self.drive(pr=pr, checks=[red], state=state,
                                        now=T0)
            actions.append(outcome['action'])
        self.assertEqual(['reran', 'suppressed', 'suppressed'], actions)
        self.assertEqual(1, len(self.gh.argv_calls))
        self.assertEqual([], self.api.calls)

    def test_repeated_wake_tick_posts_only_once(self):
        wf = eligible_workflow()
        pr = make_pr(HEAD_A)
        red1 = make_check(wf, 'rust tests', 101, attempt=1)
        outcome, state = self.drive(pr=pr, checks=[red1], state={}, now=T0)
        self.assertEqual('reran', outcome['action'])
        red2 = make_check(wf, 'rust tests', 101, attempt=2)
        actions = []
        for _ in range(3):
            outcome, state = self.drive(pr=pr, checks=[red2], state=state,
                                        now=T0)
            actions.append(outcome['action'])
        self.assertEqual(['woke', 'suppressed', 'suppressed'], actions)
        self.assertEqual(1, len(self.gh.argv_calls))
        self.assertEqual(1, len(self.api.wakes()))

    def test_repeated_cap_tick_comments_only_once(self):
        helper = RoundCapTest()
        helper.setUp()
        helper.gh, helper.api = self.gh, self.api
        helper.ledger, helper.cards = self.ledger, self.cards
        state = {}
        for head, job, run_id in RoundCapTest.ROUNDS:
            outcome, state = helper.full_red_round(head, job, run_id, state,
                                                  T0)
        self.assertEqual('capped', outcome['action'])
        self.assertEqual(3, len(self.api.calls))
        wf = eligible_workflow()
        pr = make_pr(HEAD_C)
        red2 = make_check(wf, 'tests-c', 103, attempt=2)
        for _ in range(2):
            outcome, state = self.drive(pr=pr, checks=[red2], state=state,
                                        now=T0)
            self.assertEqual('suppressed', outcome['action'])
        self.assertEqual(3, len(self.api.calls))
        self.assertEqual(2, len(self.api.wakes()))


class LedgerUnavailableTest(CITickCase):
    def test_unavailable_verdict_still_reruns_then_wakes(self):
        self.ledger.available = False
        wf = eligible_workflow()
        pr = make_pr(HEAD_A)
        red1 = make_check(wf, 'rust tests', 101, attempt=1)
        outcome, state = self.drive(pr=pr, checks=[red1], state={}, now=T0)
        self.assertEqual('reran', outcome['action'])
        red2 = make_check(wf, 'rust tests', 101, attempt=2)
        outcome, _ = self.drive(pr=pr, checks=[red2], state=state,
                                now=T0 + 600)
        self.assertEqual('woke', outcome['action'])
        self.assertEqual(1, len(self.gh.argv_calls))
        self.assertEqual(1, len(self.api.wakes()))

    def test_unavailable_record_queues_until_ledger_returns(self):
        self.ledger.available = False
        wf = eligible_workflow()
        pr = make_pr(HEAD_A)
        red = make_check(wf, 'rust tests', 101, attempt=1, test='sample_test')
        outcome, state = self.drive(pr=pr, checks=[red], state={}, now=T0)
        self.assertEqual('reran', outcome['action'])
        green = make_check(wf, 'rust tests', 101, attempt=2,
                           conclusion='success', test='sample_test')
        outcome, state = self.drive(pr=pr, checks=[green], state=state,
                                    now=T0 + 600)
        self.assertEqual('green', outcome['action'])
        self.assertEqual([], self.ledger.recorded)
        self.assertEqual([], self.api.calls)
        # The ledger is back: the next pass records the queued result.
        self.ledger.available = True
        outcome, state = self.drive(pr=pr, checks=[green], state=state,
                                    now=T0 + 1200)
        self.assertEqual(1, len(self.ledger.recorded))
        entry = self.ledger.recorded[0]
        self.assertEqual('two-bot-next', entry['repo'])
        self.assertEqual(wf, entry['workflow'])
        self.assertEqual('rust tests', entry['job'])
        self.assertEqual('sample_test', entry['test'])
        self.assertEqual(101, entry['run_id'])
        self.assertEqual(2, entry['attempt'])
        self.assertEqual(HEAD_A, entry['head_sha'])
        self.assertEqual([], self.api.calls)
        # And only once.
        outcome, _ = self.drive(pr=pr, checks=[green], state=state,
                                now=T0 + 1800)
        self.assertEqual(1, len(self.ledger.recorded))

    def test_raising_ledger_never_crashes_and_cap_says_unavailable(self):
        self.ledger = RaisingLedger()
        helper = RoundCapTest()
        helper.setUp()
        helper.gh, helper.api = self.gh, self.api
        helper.ledger, helper.cards = self.ledger, self.cards
        state = {}
        actions = []
        for head, job, run_id in RoundCapTest.ROUNDS:
            outcome, state = helper.full_red_round(head, job, run_id, state,
                                                  T0)
            actions.append(outcome['action'])
        self.assertEqual(['woke', 'woke', 'capped'], actions)
        comments = self.api.comments_to(OWNER)
        self.assertEqual(3, len(comments))
        self.assertIn('unavailable', comments[2]['body'].lower())


class ContractTest(unittest.TestCase):
    def test_allow_list_is_test_only_and_nonempty(self):
        self.assertTrue(CI_WORKFLOWS)
        for name in CI_WORKFLOWS:
            self.assertIsInstance(name, str)
            self.assertTrue(name.strip())
        lowered = [name.lower() for name in CI_WORKFLOWS]
        for banned in ('deploy', 'migrate', 'drill', 'smoke'):
            for name in lowered:
                self.assertNotIn(banned, name)


if __name__ == '__main__':
    unittest.main()
