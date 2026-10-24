"""Supplemental acceptance for bounded listing health and serialized apply ticks.

Test-only handoff: keep the two original supply suites unchanged. Run with:
  python3 -m unittest -v test_pr_supply_tick_health
from ops/productivity. External calls are synthetic; filesystem and flock are real.
The intentionally RED baseline needs floor metadata, explicit ListingResult,
terminal degraded status, entrypoint propagation, and whole-tick serialization.
"""
import contextlib
import fcntl
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pr_supply as ps
import review_error_rerequest as rr
from test_pr_supply_listing_bound import (
    BOUND, CARD, INCOMPLETE_LINE, PROBE, REPO, REPO_A, TOKEN_ENV,
    make_pr, marker_key, probe_aware_gh,
)
from test_review_error_rerequest import host_ok_stub

REPOS = {'two-web-next': ('Fixture Role', 'Fixture Project', 0)}
MIXED_REPOS = {'repo-a': ('Fixture Role', 'Fixture Project', 0), **REPOS}
STALE_MARKER = {'bound': 100, 'observed': 101, 'observed_min': 101,
                'at': '2026-10-01T00:00:00Z'}
OLD_HELD = b'previous coverage\r\nretained byte for byte\n'


class TickHarness:
    """Patch only external boundaries; keep main, state, receipts and lock real."""
    def __init__(self, *, sizes=None, repos=None, apply=True, owned=True,
                 initial_state=None, held=OLD_HELD):
        self.sizes = {REPO: 500} if sizes is None else dict(sizes)
        self.repos = dict(REPOS if repos is None else repos)
        self.apply, self.owned = apply, owned
        self.initial_state, self.initial_held = initial_state, held
        self.comments, self.api_calls, self.gh_argvs, self.sql = [], [], [], []
        self.on_comment = None
        self.out, self.err = io.StringIO(), io.StringIO()

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.stack.__enter__()
        root = self.stack.enter_context(tempfile.TemporaryDirectory(
            dir=os.environ.get('PAPERCLIP_RUN_SCRATCH_DIR') or
            os.environ.get('PAPERCLIP_SCRATCH_DIR')))
        self.root = Path(root)
        self.state, self.held = self.root / 'state.json', self.root / 'held.txt'
        self.pause, self.log = self.root / 'PAUSE', self.root / 'operator.log'
        if self.initial_state is not None:
            self.state.write_text(json.dumps(self.initial_state))
        if self.initial_held is not None:
            self.held.write_bytes(self.initial_held)
        self.stack.enter_context(mock.patch.dict(sys.modules, {'host_ok': host_ok_stub()}))
        for name, value in {'APPLY': self.apply, 'STATE': self.state,
                            'HELD': self.held, 'PAUSE': self.pause, 'LOG': self.log,
                            'REPOS': self.repos, 'MAX_ACTIONS': 2,
                            'MAX_NEW_CARDS': 1}.items():
            self.stack.enter_context(mock.patch.object(ps, name, value))
        self.stack.enter_context(mock.patch.object(rr, 'gh_env', return_value=dict(TOKEN_ENV)))
        self.stack.enter_context(mock.patch.object(rr, 'psql', side_effect=self.psql))
        self.stack.enter_context(mock.patch.object(rr, 'card_hold', return_value=None))
        self.stack.enter_context(mock.patch.object(
            rr.live_run_guard, 'issue_has_live_work', return_value=False))
        self.stack.enter_context(mock.patch.object(rr, 'comment', side_effect=self.comment))
        self.stack.enter_context(mock.patch.object(ps, 'api', side_effect=self.api))
        self.stack.enter_context(mock.patch.object(ps.subprocess, 'run', side_effect=self.gh))
        self.stack.enter_context(contextlib.redirect_stdout(self.out))
        self.stack.enter_context(contextlib.redirect_stderr(self.err))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def gh(self, cmd, **kwargs):
        if cmd[:3] != ['gh', 'pr', 'list']:
            raise AssertionError(f'unexpected subprocess: {cmd[:3]}')
        full = cmd[cmd.index('-R') + 1]
        if self.sizes.get(full) == 'failed':
            self.gh_argvs.append(list(cmd))
            return types.SimpleNamespace(stdout='', stderr='synthetic refusal', returncode=1)
        return probe_aware_gh(self.sizes, record=self.gh_argvs)(cmd, **kwargs)

    def psql(self, sql):
        self.sql.append(sql)
        if self.owned and 'issue_work_products' in sql:
            return [[CARD, 'case_fixture', 'in_progress']]
        return []

    def comment(self, iid, body):
        self.comments.append((iid, body))
        if self.on_comment is not None:
            self.on_comment()
        return '200'

    def api(self, method, path, body):
        self.api_calls.append((method, path, body))
        if path.endswith('/issues'):
            return {'id': CARD, 'identifier': 'case_fixture'}
        return {'id': 'synthetic-work-product'}

    def tick(self):
        try:
            return ps.main(), None
        except (Exception, SystemExit) as error:
            return None, error

    def read_state(self):
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def receipts(self):
        return {k: v for k, v in self.read_state().items() if '@' in k}

    def files(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}

    def lock_files(self):
        known = {self.state, self.held, self.pause, self.log}
        return [p for p in self.root.iterdir() if p.is_file() and p not in known]


class FloorAndTerminalHealth(unittest.TestCase):
    def assert_degraded(self, harness, result):
        value, error = result
        self.assertIsNone(error, f'main must return degraded status, not raise: {error!r}')
        self.assertIs(type(value), int, 'incomplete coverage must return an integer status')
        self.assertEqual(value, 2)
        self.assertEqual(harness.held.read_bytes(), OLD_HELD)

    def test_500_open_prs_report_a_floor_not_an_exact_total(self):
        with TickHarness() as h:
            h.tick()
            marker = h.read_state()[marker_key(REPO)]
            self.assertIs(type(marker['observed']), int)
            self.assertEqual(marker['observed'], 101)
            self.assertEqual(marker.get('observed_min'), 101,
                             'observed=101 is a lower bound, not an exact repo count')
            self.assertIs(type(marker['observed_min']), int)
            self.assertEqual(marker['bound'], 100)
            self.assertRegex(marker['at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
            self.assertNotIn(500, marker.values(), 'the probe cannot know the total')
            self.assertIn(INCOMPLETE_LINE, h.out.getvalue())
            self.assertIn('100 actionable open PRs', h.out.getvalue())
            self.assertNotIn('500 actionable open PRs', h.out.getvalue())

    def test_degraded_return_occurs_after_capped_nudges_and_receipts(self):
        with TickHarness() as h:
            result = h.tick()
            self.assertEqual(len(h.comments), 2)
            self.assertEqual(len(h.receipts()), 2)
            self.assertEqual(len(h.gh_argvs), 1)
            self.assertNotIn(f'{101:040x}', json.dumps(h.read_state()))
            self.assert_degraded(h, result)

    def test_degraded_return_occurs_after_capped_card_creation(self):
        with TickHarness(owned=False) as h:
            result = h.tick()
            creates = [p for _, p, _ in h.api_calls if p.endswith('/issues')]
            attaches = [p for _, p, _ in h.api_calls if p.endswith('/work-products')]
            self.assertEqual(len(creates), 1)
            self.assertEqual(len(attaches), 1)
            self.assertEqual(len(h.receipts()), 1)
            self.assert_degraded(h, result)

    def test_complete_and_incomplete_mix_is_degraded_in_either_order(self):
        for repos in (MIXED_REPOS, dict(reversed(list(MIXED_REPOS.items())))):
            with self.subTest(order=list(repos)), TickHarness(
                    repos=repos, sizes={REPO_A: 1, REPO: 500}) as h:
                result = h.tick()
                self.assertEqual(len(h.gh_argvs), 2)
                self.assertTrue(h.receipts())
                self.assertIn(marker_key(REPO), h.read_state())
                self.assertNotIn(marker_key(REPO_A), h.read_state())
                self.assert_degraded(h, result)

    def test_failed_and_incomplete_mix_still_finishes_bounded_work_then_returns_2(self):
        for repos in (MIXED_REPOS, dict(reversed(list(MIXED_REPOS.items())))):
            with self.subTest(order=list(repos)), TickHarness(
                    repos=repos, sizes={REPO_A: 'failed', REPO: 500}) as h:
                result = h.tick()
                self.assertEqual(len(h.comments), 2)
                self.assertEqual(len(h.receipts()), 2)
                self.assertFalse(any(REPO_A in k for k in h.read_state()))
                self.assertIn(f'pr-supply: list failed for {REPO_A}', h.out.getvalue())
                self.assert_degraded(h, result)

    def test_complete_tick_clears_floor_and_preserves_prior_receipts(self):
        with TickHarness() as h:
            h.tick()
            receipts = h.receipts()
            self.assertEqual(len(receipts), 2)
            h.sizes[REPO] = 100
            value, error = h.tick()
            self.assertIsNone(error)
            self.assertIn(value, (None, 0))
            self.assertNotIn(marker_key(REPO), h.read_state())
            self.assertTrue(receipts.items() <= h.read_state().items())
            self.assertIn(b'merge-ready PRs held by blocked cards', h.held.read_bytes())

    def test_all_failed_retains_nonzero_refusal_and_zero_mutations(self):
        with TickHarness(sizes={REPO: 'failed'}) as h:
            before = h.files()
            value, error = h.tick()
            if isinstance(error, SystemExit):
                self.assertNotEqual(error.code, 0)
            else:
                self.assertIsNone(error)
                self.assertIs(type(value), int)
                self.assertNotEqual(value, 0)
            self.assertEqual(h.read_state(), {})
            self.assertEqual(h.comments, [])
            self.assertEqual(h.api_calls, [])
            self.assertEqual(h.held.read_bytes(), before['held.txt'])

    def test_dry_run_is_degraded_but_creates_no_files_or_external_mutations(self):
        with TickHarness(apply=False, held=None) as h:
            result = h.tick()
            self.assertEqual(h.files(), {}, 'dry-run must not create even a lock or log')
            self.assertEqual(h.comments, [])
            self.assertEqual(h.api_calls, [])
            self.assertIn(INCOMPLETE_LINE, h.out.getvalue())
            value, error = result
            self.assertIsNone(error)
            self.assertIs(type(value), int)
            self.assertEqual(value, 2)

    def test_dry_run_preserves_existing_marker_receipts_and_held_bytes(self):
        prior = {marker_key(REPO): STALE_MARKER, 'fixture#9@abc:merge_ready': 'sent'}
        with TickHarness(apply=False, initial_state=prior) as h:
            before = h.files()
            h.tick()
            self.assertEqual(h.files(), before)
            self.assertEqual(h.comments, [])
            self.assertEqual(h.api_calls, [])

    def test_complete_dry_run_preserves_stale_marker_and_all_file_bytes(self):
        prior = {marker_key(REPO): STALE_MARKER, 'fixture#9@abc:merge_ready': 'sent'}
        with TickHarness(apply=False, sizes={REPO: 0}, initial_state=prior) as h:
            before = h.files()
            value, error = h.tick()
            self.assertIsNone(error)
            self.assertIn(value, (None, 0))
            self.assertEqual(len(h.gh_argvs), 1)
            self.assertEqual(h.gh_argvs[0][h.gh_argvs[0].index('--limit') + 1], '101')
            self.assertEqual(h.files(), before, 'complete dry-run must preserve every file byte')
            self.assertEqual(h.read_state(), prior)
            self.assertEqual(h.comments, [])
            self.assertEqual(h.api_calls, [])

    def test_incomplete_tick_persists_marker_without_any_action_receipts(self):
        with TickHarness(sizes={REPO: 101}, owned=False) as h:
            gh = h.gh

            def pending_reviews(cmd, **kwargs):
                response = gh(cmd, **kwargs)
                rows = json.loads(response.stdout)
                for row in rows:
                    row['statusCheckRollup'] = [
                        {'name': 'Paperclip Review', 'status': 'IN_PROGRESS', 'conclusion': None}]
                return types.SimpleNamespace(stdout=json.dumps(rows), returncode=0, stderr='')

            held_before = h.held.read_bytes()
            self.assertEqual(h.receipts(), {})
            with mock.patch.object(ps.subprocess, 'run', side_effect=pending_reviews):
                value, error = h.tick()
            self.assertIsNone(error)
            self.assertIs(type(value), int)
            self.assertEqual(value, 2)
            self.assertEqual(len(h.gh_argvs), 1)
            self.assertEqual(h.gh_argvs[0][h.gh_argvs[0].index('--limit') + 1], '101')
            self.assertEqual(h.comments, [])
            self.assertEqual(h.api_calls, [])
            self.assertEqual(h.receipts(), {})
            self.assertEqual(h.held.read_bytes(), held_before)
            self.assertIn(INCOMPLETE_LINE, h.out.getvalue())
            state = h.read_state()
            self.assertIn(marker_key(REPO), state,
                          'incomplete marker must persist on disk even without action receipts')
            marker = state[marker_key(REPO)]
            self.assertEqual(marker['bound'], 100)
            self.assertEqual(marker['observed'], 101)
            self.assertEqual(marker['observed_min'], 101)

    def test_pause_bypasses_listing_and_all_writes(self):
        with TickHarness(initial_state={marker_key(REPO): STALE_MARKER}) as h:
            h.pause.write_text('paused\n')
            before = h.files()
            _, error = h.tick()
            self.assertIsNone(error)
            self.assertEqual(h.files(), before)
            self.assertEqual(h.gh_argvs, [])
            self.assertEqual(h.sql, [])
            self.assertEqual(h.comments, [])
            self.assertEqual(h.api_calls, [])


class ExplicitListingResult(unittest.TestCase):
    def probe(self):
        probe = getattr(ps, 'probe_prs', None)
        self.assertTrue(callable(probe), 'probe_prs must expose the explicit listing verdict')
        return probe

    def result(self, rows, incomplete):
        factory = getattr(ps, 'ListingResult', None)
        self.assertTrue(callable(factory), 'ListingResult(rows, incomplete) is required')
        return factory(rows, incomplete)

    def test_probe_carries_rows_and_verdict_with_one_limit_101_call(self):
        probe = self.probe()
        for size, count, incomplete in ((0, 0, False), (100, 100, False), (500, 100, True)):
            with self.subTest(size=size), TickHarness(sizes={REPO: size}) as h:
                result = probe(REPO, dict(TOKEN_ENV))
                self.assertIsInstance(result, ps.ListingResult)
                self.assertNotIsInstance(result, list)
                self.assertEqual([row['number'] for row in result.rows], list(range(1, count + 1)))
                self.assertIs(result.incomplete, incomplete)
                self.assertEqual(len(h.gh_argvs), 1)
                self.assertEqual(h.gh_argvs[0][h.gh_argvs[0].index('--limit') + 1], '101')
                self.assertEqual(h.files(), {'held.txt': OLD_HELD})

    def test_copying_and_mutating_rows_cannot_change_the_result_verdict(self):
        probe = self.probe()
        with TickHarness() as h:
            result = probe(REPO, dict(TOKEN_ENV))
            copied = list(result.rows)
            copied.clear()
            self.assertEqual(len(result.rows), 100)
            self.assertIs(result.incomplete, True)
            self.assertNotIsInstance(result, list)
            self.assertEqual(h.files(), {'held.txt': OLD_HELD})

    def test_compatibility_adapter_returns_an_ordinary_list_without_metadata(self):
        with TickHarness() as h:
            rows = ps.list_prs(REPO, dict(TOKEN_ENV))
            self.assertIs(type(rows), list, 'legacy adapter must not be a metadata-bearing list subclass')
            self.assertEqual([row['number'] for row in rows], list(range(1, 101)))
            self.assertFalse(hasattr(rows, 'incomplete'))
            self.assertEqual(len(h.gh_argvs), 1)
            self.assertEqual(h.files(), {'held.txt': OLD_HELD})

    def test_probe_preserves_listing_refusals_without_returning_false_completeness(self):
        probe = self.probe()
        cases = ('timeout', 'exit', 'empty', 'json', 'non-list', 'token')
        for reason in cases:
            with self.subTest(reason=reason), TickHarness() as h:
                def refuse(cmd, **kwargs):
                    h.gh_argvs.append(list(cmd))
                    if reason == 'timeout':
                        raise subprocess.TimeoutExpired(cmd, 60)
                    stdout = {'empty': ' \n', 'json': '{bad', 'non-list': '{}'}
                    return types.SimpleNamespace(stdout=stdout.get(reason, ''),
                                                 stderr='synthetic failure',
                                                 returncode=1 if reason == 'exit' else 0)
                env = dict(TOKEN_ENV, GH_TOKEN='') if reason == 'token' else dict(TOKEN_ENV)
                with mock.patch.object(ps.subprocess, 'run', side_effect=refuse):
                    self.assertIsNone(probe(REPO, env))
                self.assertEqual(len(h.gh_argvs), 0 if reason == 'token' else 1)
                self.assertIn(f'pr-supply: list failed for {REPO}:', h.out.getvalue())
                self.assertEqual(h.files(), {'held.txt': OLD_HELD})

    def test_main_consumes_explicit_result_not_the_legacy_adapter(self):
        result = self.result([make_pr(7)], True)
        with TickHarness(sizes={REPO: 0}) as h, \
                mock.patch.object(ps, 'probe_prs', return_value=result, create=True), \
                mock.patch.object(ps, 'list_prs', side_effect=AssertionError('legacy adapter used')):
            value, error = h.tick()
            self.assertIsNone(error)
            self.assertEqual(value, 2)
            self.assertEqual(len(h.comments), 1)
            self.assertEqual(len(h.receipts()), 1)
            self.assertEqual(h.read_state()[marker_key(REPO)]['observed_min'], 101)
            self.assertEqual(h.held.read_bytes(), OLD_HELD)
            self.assertEqual(h.gh_argvs, [])

    def test_explicit_complete_result_can_clear_a_previous_marker(self):
        result = self.result([], False)
        with TickHarness(initial_state={marker_key(REPO): STALE_MARKER}) as h, \
                mock.patch.object(ps, 'probe_prs', return_value=result, create=True), \
                mock.patch.object(ps, 'list_prs', side_effect=AssertionError('legacy adapter used')):
            value, error = h.tick()
            self.assertIsNone(error)
            self.assertIn(value, (None, 0))
            self.assertNotIn(marker_key(REPO), h.read_state())
            self.assertIn(b'merge-ready PRs held by blocked cards', h.held.read_bytes())

    def test_lost_or_malformed_result_cannot_clear_prior_incomplete_coverage(self):
        malformed = (None, [], types.SimpleNamespace(rows=[]),
                     types.SimpleNamespace(rows=[], incomplete='false'))
        for result in malformed:
            with self.subTest(result=result), TickHarness(
                    sizes={REPO: 0}, initial_state={marker_key(REPO): STALE_MARKER}) as h, \
                    mock.patch.object(ps, 'probe_prs', return_value=result, create=True):
                value, error = h.tick()
                self.assertEqual(h.read_state().get(marker_key(REPO)), STALE_MARKER,
                                 'a lost verdict must not masquerade as complete coverage')
                self.assertEqual(h.held.read_bytes(), OLD_HELD)
                self.assertEqual(h.comments, [])
                self.assertEqual(h.api_calls, [])
                if isinstance(error, SystemExit):
                    self.assertNotEqual(error.code, 0)
                else:
                    self.assertIsNone(error, 'malformed result must be a controlled refusal')
                    self.assertIs(type(value), int)
                    self.assertNotEqual(value, 0)


class SerializedApplyTicks(unittest.TestCase):
    def overlap(self, h):
        observations = []

        def nested_tick():
            if observations:
                return
            observation = {}
            observations.append(observation)
            before_files = h.files()
            before_calls = (len(h.gh_argvs), len(h.sql), len(h.comments), len(h.api_calls))
            state_accesses = []
            read_text, write_text = Path.read_text, Path.write_text

            def read(path, *args, **kwargs):
                if path == h.state:
                    state_accesses.append('read')
                return read_text(path, *args, **kwargs)

            def write(path, *args, **kwargs):
                if path == h.state:
                    state_accesses.append('write')
                return write_text(path, *args, **kwargs)

            h.sizes[REPO] = 0
            with mock.patch.object(Path, 'read_text', read), \
                    mock.patch.object(Path, 'write_text', write):
                observation['result'] = h.tick()
            h.sizes[REPO] = 500
            observation['state_accesses'] = state_accesses
            observation['files_unchanged'] = h.files() == before_files
            observation['calls_unchanged'] = before_calls == (
                len(h.gh_argvs), len(h.sql), len(h.comments), len(h.api_calls))
        h.on_comment = nested_tick
        outer = h.tick()
        return outer, observations

    def test_contender_has_no_listing_api_receipt_held_or_state_side_effects(self):
        with TickHarness() as h:
            _, observations = self.overlap(h)
            self.assertEqual(len(observations), 1, 'fixture must reach a real reconcile action')
            observation = observations[0]
            self.assertEqual(observation['state_accesses'], [], 'lock must precede state reads')
            self.assertTrue(observation['calls_unchanged'], 'contender must not call external boundaries')
            self.assertTrue(observation['files_unchanged'], 'contender must not mutate even HELD or logs')
            self.assertEqual(h.held.read_bytes(), OLD_HELD)
            self.assertEqual(len(h.gh_argvs), 1)
            self.assertEqual(len(h.receipts()), 2)

    def test_complete_tick_after_release_clears_marker_without_losing_receipts(self):
        with TickHarness() as h:
            self.overlap(h)
            prior = h.receipts()
            self.assertEqual(len(prior), 2)
            self.assertIn(marker_key(REPO), h.read_state())
            h.on_comment = None
            h.sizes[REPO] = 0
            before = len(h.gh_argvs)
            value, error = h.tick()
            self.assertIsNone(error)
            self.assertIn(value, (None, 0))
            self.assertEqual(len(h.gh_argvs), before + 1, 'released lock must admit the next tick')
            self.assertNotIn(marker_key(REPO), h.read_state())
            self.assertTrue(prior.items() <= h.read_state().items())
            self.assertIn(b'merge-ready PRs held by blocked cards', h.held.read_bytes())

    def test_real_flock_is_on_a_stable_sibling_and_held_during_actions(self):
        with TickHarness() as h:
            held_locks = []

            def inspect_lock():
                for path in h.lock_files():
                    with path.open('a') as stream:
                        try:
                            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            held_locks.append((path, path.stat().st_ino))
                        else:
                            fcntl.flock(stream, fcntl.LOCK_UN)
            h.on_comment = inspect_lock
            h.tick()
            self.assertTrue(held_locks, 'another descriptor must contend on a real sibling flock')
            path, inode = held_locks[0]
            self.assertTrue(path.exists(), 'do not unlink the stable lock inode on release')
            self.assertEqual(path.stat().st_ino, inode)
            with path.open('a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = h.files()
                calls = (len(h.gh_argvs), len(h.sql), len(h.comments), len(h.api_calls))
                h.tick()
                self.assertEqual(h.files(), before)
                self.assertEqual(calls, (len(h.gh_argvs), len(h.sql), len(h.comments), len(h.api_calls)))
                fcntl.flock(stream, fcntl.LOCK_UN)

    def test_lock_covers_initial_state_read_and_marker_persistence(self):
        with TickHarness(initial_state={marker_key(REPO): STALE_MARKER}) as h:
            observations = {'read': [], 'marker-write': []}
            read_text, write_text = Path.read_text, Path.write_text

            def another_descriptor_contends():
                for path in h.lock_files():
                    with path.open('a') as stream:
                        try:
                            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return True
                        else:
                            fcntl.flock(stream, fcntl.LOCK_UN)
                return False

            def read(path, *args, **kwargs):
                if path == h.state:
                    observations['read'].append(another_descriptor_contends())
                return read_text(path, *args, **kwargs)

            def write(path, data, *args, **kwargs):
                if path.parent == h.root:
                    try:
                        parsed = json.loads(data)
                    except (TypeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict) and marker_key(REPO) in parsed:
                        observations['marker-write'].append(another_descriptor_contends())
                return write_text(path, data, *args, **kwargs)

            with mock.patch.object(Path, 'read_text', read), \
                    mock.patch.object(Path, 'write_text', write):
                h.tick()
            for phase, held in observations.items():
                self.assertTrue(held, f'fixture must exercise {phase}')
                self.assertTrue(all(held), f'sibling flock must cover {phase}')
            self.assertEqual(len(h.receipts()), 2)
            self.assertIn(marker_key(REPO), h.read_state())

    def test_exception_releases_lock_and_next_tick_reconciles(self):
        with TickHarness() as h:
            with mock.patch.object(rr, 'psql', side_effect=RuntimeError('synthetic lookup failure')):
                _, error = h.tick()
            self.assertIsInstance(error, RuntimeError)
            self.assertEqual(h.comments, [])
            h.sizes[REPO] = 1
            value, error = h.tick()
            self.assertIsNone(error)
            self.assertIn(value, (None, 0))
            self.assertEqual(len(h.comments), 1)
            self.assertEqual(len(h.receipts()), 1)
            self.assertNotIn(marker_key(REPO), h.read_state())
            for path in h.lock_files():
                with path.open('a') as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream, fcntl.LOCK_UN)


class EntrypointStatus(unittest.TestCase):
    def run_entrypoint(self, size):
        """Execute real __main__; only dependency imports and configuration are synthetic."""
        with TickHarness(sizes={REPO: size}) as h:
            synthetic = types.ModuleType('review_error_rerequest')
            synthetic.ROOT, synthetic.CID = h.root, '00000000-0000-4000-8000-000000000001'
            synthetic.API = 'https://control.invalid/api'
            synthetic.gh_env = lambda: dict(TOKEN_ENV)
            synthetic.psql = h.psql
            synthetic.card_hold = lambda *args, **kwargs: None
            synthetic.live_run_guard = types.SimpleNamespace(issue_has_live_work=lambda iid: False)
            synthetic.comment = h.comment
            synthetic.merge_notice = lambda repo, number, sha: f'conditional notice {repo}#{number}@{sha}'
            synthetic.pr_url_patterns = lambda repo, number: ('%synthetic-url%', '%synthetic-short%')
            source = str(Path(ps.__file__).resolve())
            config = {'APPLY': True, 'STATE': h.state, 'HELD': h.held,
                      'PAUSE': h.pause, 'LOG': h.log, 'REPOS': dict(REPOS),
                      'MAX_ACTIONS': 2, 'MAX_NEW_CARDS': 1}
            main_returns = []

            def configure(frame, event, arg):
                if frame.f_code.co_filename == source and frame.f_code.co_name == 'main':
                    if event == 'call':
                        frame.f_globals.update(config)
                    if event == 'return':
                        main_returns.append(arg)
                    return configure
                return None

            exit_code, error = 0, None
            previous_trace = sys.gettrace()
            with mock.patch.dict(sys.modules, {'review_error_rerequest': synthetic,
                                               'host_ok': host_ok_stub()}), \
                    mock.patch.dict(os.environ, {'HOME': str(h.root), 'PATH': '/nonexistent'}, clear=True), \
                    mock.patch.object(sys, 'argv', [source, '--apply']):
                try:
                    sys.settrace(configure)
                    runpy.run_path(source, run_name='__main__')
                except SystemExit as outcome:
                    exit_code = outcome.code
                except Exception as outcome:
                    error = outcome
                finally:
                    sys.settrace(previous_trace)
            return {'exit': exit_code, 'error': error, 'returns': main_returns,
                    'comments': h.comments, 'state': h.read_state(),
                    'argvs': h.gh_argvs, 'held': h.held.read_bytes()}

    def test_cli_propagates_degraded_main_result_after_bounded_reconciliation(self):
        result = self.run_entrypoint(500)
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['argvs']), 1, 'entrypoint must execute the real tick')
        self.assertEqual(len(result['comments']), 2)
        self.assertIn(marker_key(REPO), result['state'])
        self.assertEqual(result['held'], OLD_HELD)
        self.assertEqual(result['exit'], 2, '__main__ must propagate main() via SystemExit')
        self.assertEqual(result['returns'], [2])

    def test_cli_healthy_tick_exits_successfully(self):
        result = self.run_entrypoint(0)
        self.assertIsNone(result['error'])
        self.assertIn(result['exit'], (None, 0))
        self.assertEqual(len(result['argvs']), 1)
        self.assertNotIn(marker_key(REPO), result['state'])
        self.assertIn(b'merge-ready PRs held by blocked cards', result['held'])


if __name__ == '__main__':
    unittest.main()
