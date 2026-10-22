"""Frozen regressions: suppress shared-base/empty-list false CI author-action wakes.

TOG-36383, Test-Author owned. Parent TOG-36381 implements the guard module plus
the wiring patch to green; this file is owned here and must not be edited by
the implementation.

Defect: the host PR pump tells PR authors to fix/push for CI failures a branch
cannot repair (shared-base ``readiness-sweep`` red while required gates are
green), sometimes with an EMPTY failing-check list. The repair is SOURCE-ONLY
notification work: verdicts, eligibility, checks and gates are UNCHANGED - only
the author-action wake decision is guarded.

Excerpt finding (TOG-36382, host ``pr_pump.py`` @ ``e04ae3e2...``, 6842 bytes):
the pump renders through installed ``P.wake_text(full, pr, rec['state'],
{'red': [], 'url': None})`` with an explicitly EMPTY red list (lines 96-100),
with no fresh GitHub checks query. The composition tests below cover that exact
shape: a suppressed decision never reaches any render (initial, stale-reminder,
or pump form). The stored controller state carries no red list, so the excerpt
proves no additional known-base member: ``KNOWN_BASE_EXECUTED`` stays
``('readiness-sweep',)`` until a follow-up excerpt refines it.

Guard contract (parent implements exactly this;
``ops/productivity/pr_controller_wake_guard.py``, pure stdlib, no host imports,
deterministic, no I/O):

- ``actionable_red(red, required, known_base_executed=())`` -> ordered
  ``[(name, url)]`` author-actionable subset of the ``classify()`` red list.
- ``author_wake(state, detail, required, known_base_executed=())`` ->
  ``('wake'|'suppress', reason)``. ``suppress`` ONLY when ``state == 'ci_red'``
  and the actionable list is empty. Falsy ``required`` (rules fetch failed)
  returns ``wake`` (fail-closed: today's behavior).
- ``KNOWN_BASE_EXECUTED == ('readiness-sweep',)`` named constant.
- ``patches/pr-controller-notify-guard.patch`` wires the guard at the
  wake-emission site(s) with ``--fuzz=0``; no ``classify``/``wake_text``
  verdict or text change.

Reconciliations frozen here (the issue scenarios leave these edges implicit):

- R1. An EMPTY red list suppresses under ANY required value, including falsy
  ``set()``/``None``. An empty list names no check to fix, so no failure can
  be hidden and fail-closed has nothing to protect; this kills the exact
  observed degenerate text. Scenario 4's "any red list" therefore means any
  NON-EMPTY red list.
- R2. Required membership wins over known-base-executed: a failing required
  check is always actionable (it merge-blocks the PR; hiding it would stall
  the author silently).
- R3. Unknown checks (in neither set) are actionable (fail-closed).
- R4. The ``reason`` string is NOT pinned, only that it is a non-empty ``str``;
  the verdict (``'wake'``|``'suppress'``) is contractual.
- R5. Off the ``ci_red`` path the guard always returns ``wake``, even for
  states the controller never wakes on (``green``, ``ci_running``,
  ``reviewing``, ``passed``, ``review_error``, ``draft``): "wake" here means
  "not suppressed by THIS guard"; the controller's ``WAKE_STATES`` gate still
  owns which states wake. The guard must not second-guess it.
- R6. ``actionable_red`` with falsy ``required`` returns the full red list
  unchanged (fail-closed at both layers, so no future caller can mistake an
  unknown policy for an empty one).

Conventions (mirror ``test_pr_controller_backstop.py``):

- ``PR_CONTROLLER_SOURCE`` env override selects a full staged controller
  without importing host clients.
- ``patched_fixture_source()`` applies the patch with
  ``patch --batch --fuzz=0 -p1``.
- AST-load only ``latest_check_contexts``/``classify``/``wake_text``; the
  guard module loads by plain import; mock time (``NOW``); ``python3 -B``
  safe; no network, no host imports.
- Behavior pins load from the unpatched fixture because the patch is
  verdict-neutral by contract; a dedicated fidelity test pins patched ==>
  fixture for the three functions once the patch lands.

Red-before (expected now): ``GuardContractTest``, ``GuardedCompositionTest``
and ``NotifyGuardPatchTest`` fail - guard module + patch file absent.
``NotifyFixtureSeamTest`` passes - it pins pre-fix verdicts and reproduces the
degenerate pump body the guard must suppress. Green-after: the full file
passes unmodified against the parent's implementation.

Run::

  (cd ops/productivity && python3 -B -m unittest -v test_pr_controller_notify_guard)

Staged controller (no host imports)::

  PR_CONTROLLER_SOURCE=/path/to/staged/pr_controller.py \\
    python3 -B ops/productivity/test_pr_controller_notify_guard.py -v
"""
import ast
import calendar
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # plain sibling imports whatever the cwd; -B safe
PATCH = HERE / 'patches' / 'pr-controller-notify-guard.patch'
FIXTURE = HERE / 'fixtures' / 'pr_controller_notify_before.py'
NOW = 1800000000
FULL = 'TogetherWeOwn/paperclip-ops'
NUM = 74
HEAD = 'cf880813deadbeef'
RUN_URL = 'https://github.com/TogetherWeOwn/paperclip-ops/actions/runs/424242'

# Frozen required set for these tests. The live value comes from the branch
# ruleset (required_checks() minus Paperclip Review); only membership against
# the red list matters here, not the live ruleset contents.
REQUIRED = {'ci-ok', 'pr-lint', 'public-safety'}

# Installed constants, byte-verified from the TOG-36112 snapshot of the live
# controller (SHA256 0fab0c18f7f7a18cc0540566889957d0306cc40e0d5ac4bfac3ea3a31d8d0bee):
# DRAFT_IDLE_H at installed line 52, RED at installed line 79.
DRAFT_IDLE_H = 6
RED = {'FAILURE', 'TIMED_OUT', 'STARTUP_FAILURE', 'ERROR'}

# Pump composition literals, byte-verified from host pr_pump.py @ e04ae3e2...
# lines 96-100 via the TOG-36382 evidence document. Quoted literals only; the
# host pump file itself is never committed to any repo.
PUMP_PREFIX = '**Host PR pump: this PR is waiting on you and you have a free slot. Work it now.** '
PUMP_SUFFIX = ("\n\nCheck the PR's current checks/review on GitHub first "
               "(`gh pr view` / `gh pr checks`), fix, push once, "
               "then set this card to in_review. If something outside this PR truly blocks you, "
               "add it as a first-class blocker instead of parking the card.")
PUMP_EMPTY_DETAIL = {'red': [], 'url': None}

ACTIONABLE_MARKER = '**CI is red**'
PUSH_ONCE = 'push once'
NON_CI_RED_STATES = ('green', 'ci_running', 'reviewing', 'passed', 'findings',
                     'conflict', 'draft', 'draft_idle', 'queue_ejected', 'review_error')

try:
    import pr_controller_wake_guard as guard
    _GUARD_IMPORT_ERROR = None
except ImportError as exc:  # red-before: the parent has not landed the module yet
    guard = None
    _GUARD_IMPORT_ERROR = exc


def require_guard(test):
    """Fail (not skip) when the guard module is absent: that red is the receipt."""
    if guard is None:
        test.fail(f'RED-BEFORE: pr_controller_wake_guard absent: {_GUARD_IMPORT_ERROR}')
    return guard


def stamp(value):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(value))


def parse(value):
    return calendar.timegm(time.strptime(value, '%Y-%m-%dT%H:%M:%SZ'))


def controller_source():
    if os.environ.get('PR_CONTROLLER_SOURCE'):
        return Path(os.environ['PR_CONTROLLER_SOURCE']).read_text()
    return FIXTURE.read_text()


def patched_fixture_source():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'ops/productivity/pr_controller.py'
        target.parent.mkdir(parents=True)
        target.write_text(FIXTURE.read_text())
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                       cwd=directory, check=True, capture_output=True, text=True)
        return target.read_text()


def load_controller(source):
    """AST-load only the three seam functions; never import host clients."""
    tree = ast.parse(source)
    wanted = {n.name: n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name in ('latest_check_contexts', 'classify', 'wake_text')}
    missing = {'latest_check_contexts', 'classify', 'wake_text'} - set(wanted)
    assert not missing, f'controller source lacks {sorted(missing)}'
    env = {'re': re, 'NOW': NOW, 'parse': parse,
           'DRAFT_IDLE_H': DRAFT_IDLE_H, 'RED': set(RED)}
    for name, node in wanted.items():
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<controller>', 'exec'), env)
    assert callable(env['classify']) and callable(env['wake_text'])
    return env


def check_run(name, conclusion='SUCCESS', url=RUN_URL, status='COMPLETED'):
    """Check-run style context. No checkSuite key, so latest_check_contexts
    passes it through untouched (pinned by test_latest_check_contexts_...)."""
    return {'name': name, 'status': status, 'conclusion': conclusion, 'detailsUrl': url}


def make_pr(contexts, *, draft=False, committed=None,
            mergeable='MERGEABLE', merge_state='CLEAN'):
    pr = {'url': f'https://github.com/{FULL}/pull/{NUM}', 'number': NUM,
          'headRefOid': HEAD, 'isDraft': draft, 'mergeable': mergeable,
          'mergeStateStatus': merge_state, '_contexts': list(contexts)}
    if committed is not None:
        pr['_committed'] = committed
    return pr


def pump_pr():
    """PR object as the pump synthesizes it from stored state (pr_pump.py:93-95)."""
    return {'url': f'https://github.com/{FULL}/pull/{NUM}', 'number': NUM, 'headRefOid': HEAD}


def reviews_entry(state='completed', score='4', url='https://example.com/review/1'):
    return {(FULL.lower(), NUM, HEAD): (state, score, url)}


def all_required_success():
    return [check_run(name) for name in sorted(REQUIRED)]


def stale_wrap(cur, since_h, body):
    """Stale-reminder composition mirrors installed rewake_stale (lines 497-499)."""
    return (f"**Reminder: still {cur.replace('_', ' ')} after {since_h}h on the same head.** "
            + body + "\n\nFix and push once now; don't merge main unless conflict/main-red.")


def guarded_emit(g, env, full, pr, state, detail, required, known, renderer):
    """The guarded path: decide first, render only on 'wake'. Suppression must
    never invoke the renderer (covers initial, stale-reminder and pump forms,
    which all render through wake_text)."""
    verdict, reason = g.author_wake(state, detail, required, known_base_executed=known)
    if verdict != 'wake':
        return verdict, reason, None
    return verdict, reason, renderer(full, pr, state, detail)


class NotifyFixtureSeamTest(unittest.TestCase):
    """Pre-fix verdict pins + degenerate-body reproduction. Green now and after:
    the patch is verdict-neutral by contract, so these pins must never move."""

    @classmethod
    def setUpClass(cls):
        # NOTE: keep the loaded functions inside cls.env (a dict). Storing a
        # bare function as a class attribute would bind it as a method and
        # prepend self to every call.
        cls.env = load_controller(controller_source())

    def test_latest_check_contexts_passes_plain_runs_through(self):
        contexts = [check_run('ci-ok'), check_run('readiness-sweep', 'FAILURE')]
        self.assertEqual(contexts, self.env['latest_check_contexts'](contexts))

    def test_shared_base_only_still_classifies_ci_red(self):
        contexts = all_required_success() + [check_run('readiness-sweep', 'FAILURE')]
        state, detail = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('ci_red', state)
        self.assertEqual([('readiness-sweep', RUN_URL)], detail['red'])
        self.assertFalse(detail['pending'])

    def test_genuine_required_failure_classifies_ci_red(self):
        contexts = [check_run('ci-ok', 'FAILURE'), check_run('pr-lint'), check_run('public-safety')]
        state, detail = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('ci_red', state)
        self.assertEqual([('ci-ok', RUN_URL)], detail['red'])

    def test_mixed_failure_lists_every_red_check_in_order(self):
        contexts = [check_run('ci-ok', 'FAILURE'), check_run('pr-lint'),
                    check_run('public-safety'), check_run('readiness-sweep', 'FAILURE')]
        state, detail = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('ci_red', state)
        self.assertEqual([('ci-ok', RUN_URL), ('readiness-sweep', RUN_URL)], detail['red'])

    def test_all_required_green_without_review_is_green(self):
        state, detail = self.env['classify'](make_pr(all_required_success()), set(REQUIRED), {}, FULL)
        self.assertEqual(('green', {}), (state, detail))

    def test_pending_required_check_is_ci_running(self):
        contexts = [check_run('ci-ok'), check_run('pr-lint', '', status='IN_PROGRESS'),
                    check_run('public-safety')]
        state, _ = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('ci_running', state)

    def test_missing_required_check_is_ci_running(self):
        state, _ = self.env['classify'](make_pr([check_run('ci-ok')]), set(REQUIRED), {}, FULL)
        self.assertEqual('ci_running', state)

    def test_non_required_pending_does_not_hold_review(self):
        contexts = all_required_success() + [check_run('nightly', '', status='IN_PROGRESS')]
        state, _ = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('green', state)

    def test_paperclip_review_failure_is_ignored(self):
        contexts = all_required_success() + [check_run('Paperclip Review', 'FAILURE')]
        state, _ = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('green', state)

    def test_cancelled_and_stale_runs_are_ignored_but_leave_required_unmet(self):
        contexts = all_required_success() + [check_run('nightly', 'CANCELLED')]
        state, _ = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('green', state)
        contexts = [check_run('ci-ok'), check_run('pr-lint', 'CANCELLED'), check_run('public-safety')]
        state, _ = self.env['classify'](make_pr(contexts), set(REQUIRED), {}, FULL)
        self.assertEqual('ci_running', state)

    def test_review_state_matrix(self):
        cases = [('queued', '', 'reviewing'), ('running', '', 'reviewing'),
                 ('received', '', 'reviewing'), ('completed', '5', 'passed'),
                 ('completed', '4', 'findings'), ('failed', '', 'review_error'),
                 ('superseded', '', 'green'), ('cancelled', '', 'green')]
        for review_state, score, expected in cases:
            with self.subTest(review_state=review_state, score=score):
                reviews = reviews_entry(state=review_state, score=score)
                state, detail = self.env['classify'](make_pr(all_required_success()),
                                              set(REQUIRED), reviews, FULL)
                self.assertEqual(expected, state)
                if expected == 'findings':
                    self.assertEqual(score, detail['score'])
                if expected == 'review_error':
                    self.assertEqual(review_state, detail['review_state'])

    def test_conflict_short_circuits_before_checks(self):
        contexts = [check_run('ci-ok', 'FAILURE')]
        pr = make_pr(contexts, mergeable='CONFLICTING', merge_state='DIRTY')
        state, _ = self.env['classify'](pr, set(REQUIRED), {}, FULL)
        self.assertEqual('conflict', state)

    def test_draft_and_draft_idle_boundaries(self):
        pr = make_pr([], draft=True, committed=stamp(NOW - 3600))
        self.assertEqual('draft', self.env['classify'](pr, set(REQUIRED), {}, FULL)[0])
        pr = make_pr([], draft=True, committed=stamp(NOW - 7 * 3600))
        self.assertEqual('draft_idle', self.env['classify'](pr, set(REQUIRED), {}, FULL)[0])

    def test_empty_red_pump_body_reproduced_byte_exact(self):
        """Anchor the observed degenerate artifact (TOG-36112/TOG-35516): the
        pump's ``{'red': [], 'url': None}`` render. The guard must ensure this
        actionable text is never emitted for a suppressed decision."""
        text = self.env['wake_text'](FULL, pump_pr(), 'ci_red', dict(PUMP_EMPTY_DETAIL))
        self.assertIn(ACTIONABLE_MARKER, text)
        self.assertIn('Failing checks:\n\n\nOpen the logs, fix the cause on this PR '
                      'branch and push once.', text)
        pump_full = PUMP_PREFIX + text + PUMP_SUFFIX
        self.assertIn('**Host PR pump:', pump_full)
        self.assertIn(PUSH_ONCE, pump_full)

    def test_existing_wake_key_paths_still_owned_by_controller(self):
        import pr_controller_wakes as wakes
        self.assertTrue(callable(wakes.post_wake))
        self.assertTrue(callable(wakes.reconcile_wake))
        self.assertTrue(hasattr(wakes, 'WAKE_MAX_RETRIES'))


class GuardContractTest(unittest.TestCase):
    """Pure guard-module contract. RED-BEFORE: fails until the parent lands
    ops/productivity/pr_controller_wake_guard.py."""

    def setUp(self):
        self.g = require_guard(self)
        self.known = self.g.KNOWN_BASE_EXECUTED

    def test_known_base_executed_constant_frozen(self):
        self.assertIsInstance(self.known, tuple)
        self.assertEqual(('readiness-sweep',), self.known)

    def test_actionable_red_filters_only_known_base_preserving_order(self):
        red = [('ci-ok', 'u1'), ('readiness-sweep', 'u2'), ('pr-lint', 'u3')]
        result = self.g.actionable_red(red, set(REQUIRED), known_base_executed=self.known)
        self.assertIsInstance(result, list)
        self.assertEqual([('ci-ok', 'u1'), ('pr-lint', 'u3')], result)

    def test_actionable_red_empty_in_empty_out(self):
        result = self.g.actionable_red([], set(REQUIRED), known_base_executed=self.known)
        self.assertEqual([], result)

    def test_actionable_red_required_wins_over_known_base(self):
        red = [('ci-ok', 'u1')]
        result = self.g.actionable_red(red, {'ci-ok'}, known_base_executed=('ci-ok',))
        self.assertEqual(red, result)

    def test_actionable_red_unknown_is_actionable(self):
        red = [('nightly-perf', 'u9')]
        result = self.g.actionable_red(red, set(REQUIRED), known_base_executed=self.known)
        self.assertEqual(red, result)

    def test_actionable_red_falsy_required_returns_full_list(self):
        red = [('readiness-sweep', 'u2'), ('ci-ok', 'u1')]
        for required in (set(), None):
            with self.subTest(required=required):
                self.assertEqual(list(red), self.g.actionable_red(
                    list(red), required, known_base_executed=self.known))

    def test_author_wake_suppresses_only_ci_red(self):
        for state in NON_CI_RED_STATES:
            with self.subTest(state=state):
                verdict, reason = self.g.author_wake(
                    state, {'red': []}, set(REQUIRED), known_base_executed=self.known)
                self.assertEqual('wake', verdict)
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)

    def test_author_wake_ci_red_empty_red_suppresses_under_any_required(self):
        for required in (set(REQUIRED), {'other-check'}, set(), None):
            with self.subTest(required=required):
                verdict, reason = self.g.author_wake(
                    'ci_red', {'red': []}, required, known_base_executed=self.known)
                self.assertEqual('suppress', verdict)
                self.assertTrue(reason)
        verdict, _ = self.g.author_wake('ci_red', dict(PUMP_EMPTY_DETAIL),
                                        set(REQUIRED), known_base_executed=self.known)
        self.assertEqual('suppress', verdict)
        for detail in ({}, {'red': None}):
            with self.subTest(detail=detail):
                verdict, _ = self.g.author_wake(
                    'ci_red', dict(detail), set(REQUIRED), known_base_executed=self.known)
                self.assertEqual('suppress', verdict)

    def test_author_wake_ci_red_known_base_only_suppresses(self):
        verdict, _ = self.g.author_wake(
            'ci_red', {'red': [('readiness-sweep', RUN_URL)]},
            set(REQUIRED), known_base_executed=self.known)
        self.assertEqual('suppress', verdict)

    def test_author_wake_ci_red_required_failure_wakes(self):
        verdict, _ = self.g.author_wake(
            'ci_red', {'red': [('ci-ok', RUN_URL)]},
            set(REQUIRED), known_base_executed=self.known)
        self.assertEqual('wake', verdict)

    def test_author_wake_ci_red_mixed_wakes(self):
        red = [('readiness-sweep', RUN_URL), ('ci-ok', RUN_URL)]
        verdict, _ = self.g.author_wake(
            'ci_red', {'red': red}, set(REQUIRED), known_base_executed=self.known)
        self.assertEqual('wake', verdict)

    def test_author_wake_ci_red_unknown_wakes(self):
        verdict, _ = self.g.author_wake(
            'ci_red', {'red': [('nightly-perf', RUN_URL)]},
            set(REQUIRED), known_base_executed=self.known)
        self.assertEqual('wake', verdict)

    def test_author_wake_falsy_required_wakes_on_any_nonempty_red(self):
        reds = ([('ci-ok', RUN_URL)], [('readiness-sweep', RUN_URL)],
                [('nightly-perf', RUN_URL)],
                [('readiness-sweep', RUN_URL), ('ci-ok', RUN_URL)])
        for required in (set(), None):
            for red in reds:
                with self.subTest(required=required, red=red):
                    verdict, _ = self.g.author_wake(
                        'ci_red', {'red': list(red)}, required,
                        known_base_executed=self.known)
                    self.assertEqual('wake', verdict)

    def test_author_wake_reason_is_nonempty_str(self):
        samples = [('ci_red', {'red': []}, set(REQUIRED)),
                   ('ci_red', {'red': [('ci-ok', RUN_URL)]}, set(REQUIRED)),
                   ('ci_red', {'red': [('ci-ok', RUN_URL)]}, set()),
                   ('green', {}, set(REQUIRED))]
        for state, detail, required in samples:
            with self.subTest(state=state, detail=detail, required=required):
                verdict, reason = self.g.author_wake(
                    state, dict(detail), required, known_base_executed=self.known)
                self.assertIn(verdict, ('wake', 'suppress'))
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)

    def test_positional_known_base_argument_order(self):
        verdict, _ = self.g.author_wake('ci_red', {'red': []}, set(REQUIRED), self.known)
        self.assertEqual('suppress', verdict)
        red = [('ci-ok', 'u1')]
        self.assertEqual(red, self.g.actionable_red(red, set(REQUIRED), self.known))

    def test_deterministic_across_repeated_calls(self):
        red = [('readiness-sweep', RUN_URL), ('ci-ok', RUN_URL)]
        first = self.g.actionable_red(red, set(REQUIRED), known_base_executed=self.known)
        first_wake = self.g.author_wake('ci_red', {'red': red}, set(REQUIRED),
                                        known_base_executed=self.known)
        for _ in range(5):
            self.assertEqual(first, self.g.actionable_red(
                list(red), set(REQUIRED), known_base_executed=self.known))
            self.assertEqual(first_wake, self.g.author_wake(
                'ci_red', {'red': list(red)}, set(REQUIRED), known_base_executed=self.known))

    def test_inputs_are_not_mutated_and_results_are_fresh(self):
        red = [('readiness-sweep', RUN_URL), ('ci-ok', RUN_URL)]
        required = set(REQUIRED)
        detail = {'red': list(red), 'pending': False}
        first = self.g.actionable_red(red, required, known_base_executed=self.known)
        self.assertEqual([('ci-ok', RUN_URL)], first)
        first.append(('mutated', 'x'))
        second = self.g.actionable_red(red, required, known_base_executed=self.known)
        self.assertEqual([('ci-ok', RUN_URL)], second)
        self.assertEqual([('readiness-sweep', RUN_URL), ('ci-ok', RUN_URL)], red)
        self.assertEqual({'red': list(red), 'pending': False}, detail)
        self.assertEqual(set(REQUIRED), required)
        self.g.author_wake('ci_red', detail, required, known_base_executed=self.known)
        self.assertEqual({'red': list(red), 'pending': False}, detail)
        self.assertEqual(set(REQUIRED), required)
        self.assertEqual(('readiness-sweep',), self.g.KNOWN_BASE_EXECUTED)

    def test_imports_stdlib_only_and_no_io(self):
        source = Path(self.g.__file__).read_text()
        self.assertTrue(Path(self.g.__file__).resolve().is_relative_to(HERE),
                        'guard module must be the tracked sibling, not a host import')
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.update(node.module.split('.')[0:1])
        stdlib = set(sys.stdlib_module_names)
        self.assertTrue(imported <= stdlib, f'non-stdlib imports: {sorted(imported - stdlib)}')
        io_modules = {'os', 'subprocess', 'socket', 'urllib', 'http', 'ssl', 'ftplib',
                      'smtplib', 'imaplib', 'poplib', 'pathlib', 'shutil', 'tempfile', 'io',
                      'sqlite3', 'requests', 'httpx', 'psycopg', 'psycopg2', 'ctypes', 'pty'}
        self.assertTrue(imported.isdisjoint(io_modules),
                        f'I/O imports forbidden in the guard: {sorted(imported & io_modules)}')
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertTrue(calls.isdisjoint({'open', '__import__', 'exec', 'eval'}),
                        f'I/O/dynamic calls forbidden in the guard: {sorted(calls)}')

    def test_public_surface_is_contracted_only(self):
        public = {n for n in dir(self.g) if not n.startswith('_')}
        self.assertTrue(public <= {'actionable_red', 'author_wake', 'KNOWN_BASE_EXECUTED'},
                        f'uncontracted public names: {sorted(public)}')
        self.assertTrue(callable(self.g.actionable_red))
        self.assertTrue(callable(self.g.author_wake))
        joined = ' '.join(public).lower()
        for banned in ('wake_key', 'wkey', 'backoff', 'retry', 'reconcile',
                       'cooldown', 'dedup', 'post_wake'):
            self.assertNotIn(banned, joined)

class GuardedCompositionTest(unittest.TestCase):
    """classify -> guard -> render/no-render through every wake form. RED-BEFORE:
    fails until the parent lands the guard module."""

    @classmethod
    def setUpClass(cls):
        cls.env = load_controller(controller_source())

    def setUp(self):
        self.g = require_guard(self)
        self.known = self.g.KNOWN_BASE_EXECUTED
        self.renderer = Mock(wraps=self.env['wake_text'])

    def emit(self, pr, state, detail, required):
        return guarded_emit(self.g, self.env, FULL, pr, state, detail,
                            required, self.known, self.renderer)

    def test_shared_base_suppresses_initial_and_never_renders(self):
        contexts = all_required_success() + [check_run('readiness-sweep', 'FAILURE')]
        pr = make_pr(contexts)
        state, detail = self.env['classify'](pr, set(REQUIRED), {}, FULL)
        self.assertEqual('ci_red', state)
        verdict, _, text = self.emit(pr, state, detail, set(REQUIRED))
        self.assertEqual('suppress', verdict)
        self.assertIsNone(text)
        self.renderer.assert_not_called()
        # Contrast: unguarded, this exact input emits the actionable instruction.
        self.assertIn(ACTIONABLE_MARKER, self.env['wake_text'](FULL, pr, state, detail))
        self.assertIn(PUSH_ONCE, self.env['wake_text'](FULL, pr, state, detail))

    def test_shared_base_suppresses_stale_reminder_form(self):
        contexts = all_required_success() + [check_run('readiness-sweep', 'FAILURE')]
        pr = make_pr(contexts)
        state, detail = self.env['classify'](pr, set(REQUIRED), {}, FULL)
        verdict, _, text = self.emit(pr, state, detail, set(REQUIRED))
        self.assertEqual('suppress', verdict)
        self.assertIsNone(text)
        self.renderer.assert_not_called()
        # Contrast: the stale wrapper around the unguarded body is actionable.
        stale = stale_wrap(state, 3.0, self.env['wake_text'](FULL, pr, state, detail))
        self.assertIn(ACTIONABLE_MARKER, stale)
        self.assertIn('Fix and push once now', stale)

    def test_pump_empty_detail_suppresses(self):
        pr = pump_pr()
        verdict, _, text = self.emit(pr, 'ci_red', dict(PUMP_EMPTY_DETAIL), set(REQUIRED))
        self.assertEqual('suppress', verdict)
        self.assertIsNone(text)
        self.renderer.assert_not_called()
        # Contrast: the observed pump artifact this suppresses.
        pump_full = (PUMP_PREFIX
                     + self.env['wake_text'](FULL, pr, 'ci_red', dict(PUMP_EMPTY_DETAIL))
                     + PUMP_SUFFIX)
        self.assertIn('Failing checks:\n\n\nOpen the logs', pump_full)

    def test_empty_actionable_never_invokes_render(self):
        for detail in ({'red': []}, {'red': [('readiness-sweep', RUN_URL)]}):
            for required in (set(REQUIRED), {'other-check'}, set(), None):
                renderer = Mock(wraps=self.env['wake_text'])
                verdict, _, text = guarded_emit(
                    self.g, self.env, FULL, pump_pr(), 'ci_red', dict(detail),
                    required, self.known, renderer)
                with self.subTest(detail=detail, required=required):
                    if not detail['red']:
                        self.assertEqual('suppress', verdict)
                    else:
                        self.assertEqual('suppress' if required else 'wake', verdict)
                    if verdict == 'suppress':
                        self.assertIsNone(text)
                        renderer.assert_not_called()

    def test_genuine_failure_renders_byte_identical(self):
        contexts = [check_run('ci-ok', 'FAILURE'), check_run('pr-lint'), check_run('public-safety')]
        pr = make_pr(contexts)
        state, detail = self.env['classify'](pr, set(REQUIRED), {}, FULL)
        verdict, _, text = self.emit(pr, state, detail, set(REQUIRED))
        self.assertEqual('wake', verdict)
        self.assertEqual(self.env['wake_text'](FULL, pr, state, detail), text)
        self.assertIn(ACTIONABLE_MARKER, text)
        self.assertIn('- `ci-ok`:', text)
        # Mixed red: the guard wakes AND the render keeps the full list - the
        # guard filters the decision, never the rendered failing checks.
        contexts = [check_run('ci-ok', 'FAILURE'), check_run('pr-lint'),
                    check_run('public-safety'), check_run('readiness-sweep', 'FAILURE')]
        pr = make_pr(contexts)
        state, detail = self.env['classify'](pr, set(REQUIRED), {}, FULL)
        verdict, _, text = self.emit(pr, state, detail, set(REQUIRED))
        self.assertEqual('wake', verdict)
        self.assertEqual(self.env['wake_text'](FULL, pr, state, detail), text)
        self.assertIn('- `ci-ok`:', text)
        self.assertIn('- `readiness-sweep`:', text)

    def test_fail_closed_renders_byte_identical(self):
        reds = ([('ci-ok', RUN_URL)], [('readiness-sweep', RUN_URL)],
                [('nightly-perf', RUN_URL)])
        for required in (set(), None):
            for red in reds:
                with self.subTest(required=required, red=red):
                    renderer = Mock(wraps=self.env['wake_text'])
                    detail = {'red': list(red)}
                    verdict, _, text = guarded_emit(
                        self.g, self.env, FULL, pump_pr(), 'ci_red', detail,
                        required, self.known, renderer)
                    self.assertEqual('wake', verdict)
                    self.assertEqual(self.env['wake_text'](FULL, pump_pr(), 'ci_red', detail), text)
        detail = {'red': [('nightly-perf', RUN_URL)]}
        verdict, _, text = self.emit(pump_pr(), 'ci_red', detail, set(REQUIRED))
        self.assertEqual('wake', verdict)
        self.assertEqual(self.env['wake_text'](FULL, pump_pr(), 'ci_red', detail), text)

    def test_other_states_pass_through_byte_identical(self):
        matrix = [
            ('green', make_pr(all_required_success()), set(REQUIRED), {}),
            ('ci_running', make_pr([check_run('ci-ok'),
                                    check_run('pr-lint', '', status='IN_PROGRESS'),
                                    check_run('public-safety')]), set(REQUIRED), {}),
            ('reviewing', make_pr(all_required_success()), set(REQUIRED),
             reviews_entry(state='running', score='')),
            ('passed', make_pr(all_required_success()), set(REQUIRED),
             reviews_entry(state='completed', score='5')),
            ('findings', make_pr(all_required_success()), set(REQUIRED),
             reviews_entry(state='completed', score='4')),
            ('conflict', make_pr(all_required_success(), mergeable='CONFLICTING',
                                 merge_state='DIRTY'), set(REQUIRED), {}),
            ('draft', make_pr([], draft=True, committed=stamp(NOW - 3600)), set(REQUIRED), {}),
            ('draft_idle', make_pr([], draft=True, committed=stamp(NOW - 7 * 3600)),
             set(REQUIRED), {}),
            ('review_error', make_pr(all_required_success()), set(REQUIRED),
             reviews_entry(state='failed', score='')),
        ]
        for expected, pr, required, reviews in matrix:
            with self.subTest(state=expected):
                state, detail = self.env['classify'](pr, required, reviews, FULL)
                self.assertEqual(expected, state)
                renderer = Mock(wraps=self.env['wake_text'])
                verdict, _, text = guarded_emit(
                    self.g, self.env, FULL, pr, state, detail,
                    required, self.known, renderer)
                self.assertEqual('wake', verdict)
                self.assertEqual(self.env['wake_text'](FULL, pr, state, detail), text)
                renderer.assert_called_once()
        # queue_ejected has no classify() verdict (it is detected outside
        # classify); the guard still passes it through untouched.
        renderer = Mock(wraps=self.env['wake_text'])
        verdict, _, text = guarded_emit(self.g, self.env, FULL, pump_pr(),
                                        'queue_ejected', {}, set(REQUIRED),
                                        self.known, renderer)
        self.assertEqual('wake', verdict)
        self.assertEqual(self.env['wake_text'](FULL, pump_pr(), 'queue_ejected', {}), text)
        self.assertIn('merge queue ejected head', text)


class NotifyGuardPatchTest(unittest.TestCase):
    """Patch presence, --fuzz=0 application at both offsets, and verdict
    neutrality. RED-BEFORE: fails until the parent lands
    patches/pr-controller-notify-guard.patch."""

    def require_patch(self):
        self.assertTrue(PATCH.exists(),
                        'RED-BEFORE: patches/pr-controller-notify-guard.patch absent')

    def test_notify_guard_patch_applies_without_fuzz(self):
        self.require_patch()
        source = patched_fixture_source()
        self.assertIn('def wake_text', source)
        self.assertIn('def classify', source)

    def test_notify_guard_patch_applies_at_live_offset_without_fuzz(self):
        # Installed wake_text starts at line 299, so the live controller
        # carries 298 lines above this seam. GNU patch with --fuzz=0 floats
        # UP (backward search) but never down, so every hunk must anchor at
        # or after its context position in EVERY target: anchor each hunk at
        # its 298-prefix offset-test position (fixture_line + 298). The BOF
        # round-trip then floats up, the offset file applies exact, and the
        # live install floats up to its true (lower) line. A hunk anchored
        # at line 1 applies at BOF but FAILS here by design.
        self.require_patch()
        prefix = ''.join(f'# live controller prefix line {i}\n' for i in range(1, 299))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'ops/productivity/pr_controller.py'
            target.parent.mkdir(parents=True)
            target.write_text(prefix + FIXTURE.read_text())
            result = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            offset_patched = target.read_text()
        self.assertEqual(patched_fixture_source(), offset_patched[len(prefix):])

    def test_offset_check_is_independent_of_staged_source(self):
        self.require_patch()
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / 'staged_controller.py'
            staged.write_text('# full staged controller\n' + patched_fixture_source())
            with patch.dict(os.environ, {'PR_CONTROLLER_SOURCE': str(staged)}):
                self.test_notify_guard_patch_applies_at_live_offset_without_fuzz()

    def test_patched_functions_are_verdict_neutral(self):
        self.require_patch()
        before = ast.parse(FIXTURE.read_text())
        after = ast.parse(patched_fixture_source())
        for name in ('latest_check_contexts', 'classify', 'wake_text'):
            with self.subTest(function=name):
                old = next(n for n in before.body
                           if isinstance(n, ast.FunctionDef) and n.name == name)
                new = next(n for n in after.body
                           if isinstance(n, ast.FunctionDef) and n.name == name)
                self.assertEqual(ast.dump(old), ast.dump(new))

    def test_patch_references_guard_decision(self):
        self.require_patch()
        text = PATCH.read_text()
        self.assertIn('pr_controller_wake_guard', text)
        self.assertIn('author_wake', text)


if __name__ == '__main__':
    unittest.main()
