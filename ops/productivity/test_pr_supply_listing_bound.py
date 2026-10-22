"""Immutable acceptance contract for pr-supply listing-bound refusal.

Paired acceptance: the implementation builds on this test branch and
NEVER edits this file.

Listing-bound scheme: single-call
limit+1 probe. `list_prs` calls `gh pr list --limit 101` (BOUND+1) once per
repo. BOUND = 100.

Locked contract:
- New constant `pr_supply.PR_SUPPLY_LIST_BOUND == 100`. Probe argv is
  `--limit 101` (str(BOUND + 1)). Exactly one `gh pr list` call per repo
  per tick (no second page, no pagination loop).
- Exactly-at-bound (100 rows for the probe): complete. `list_prs` returns
  all 100, no incomplete signal, tick reconciles normally, any stale
  `:list-incomplete` marker is cleared, HELD file rewrites on a healthy
  tick.
- Over-bound (101 rows for the probe, i.e. repo has 101+ open PRs):
  `list_prs` returns the first 100 (probe row dropped, never reconciled)
  AND emits the explicit incomplete signal:
  - log line (stdout, greppable): exactly
    `pr-supply: list incomplete for <repo-full>: bound 100 observed 101+`
  - state marker, visible to the next tick:
    key `<repo-full>:list-incomplete` ->
    `{'bound': 100, 'observed': 101, 'at': '<ISO8601Z>'}`.
  The bounded 100 are still reconciled normally (nudge/create within
  MAX_ACTIONS / MAX_NEW_CARDS). Coverage claims never include unseen PRs:
  the tick summary counts bounded PRs only, and an incomplete tick leaves
  merge-ready-held.txt UNCHANGED (same as a failed listing: no rewrite on
  incomplete coverage).
- Existing refusals preserved (per-repo, zero writes for that repo, other
  repos still processed): timeout, non-zero exit, empty output, invalid
  JSON (unparseable or non-list), empty token (refuses before `gh` is
  called). Log stays `pr-supply: list failed for <repo>: <reason>`.
  A failed listing writes no marker and no receipts for that repo.
- Caps hold on an incomplete tick: MAX_ACTIONS bounds nudges, MAX_NEW_CARDS
  bounds creates; the incomplete signal + marker are still emitted.

Harness: hermetic `gh` stub at argv level (no network, no token, no control
plane). The stub is probe-aware: it returns min(--limit, repo_size) rows,
so the current `--limit 100` code path sees a silent 100-row truncation
for a 101-PR repo (RED: no signal), while the `--limit 101` probe reveals
the 101st row (GREEN after Builder).

Regression control (Builder runs both):
  (cd ops/productivity && python3 -m unittest -v test_pr_supply test_pr_supply_listing_bound)
Builder also wires this file into the ci.yml ops/productivity unittest list.
"""
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pr_supply as ps
import review_error_rerequest as rr
from test_review_error_rerequest import host_ok_stub

REPO = 'TogetherWeOwn/two-web-next'
REPO_A = 'TogetherWeOwn/repo-a'
BOUND = 100
PROBE = 101
CARD = '00000000-0000-4000-8000-000000000058'
TOKEN_ENV = {'GH_TOKEN': 'test-token', 'PATH': '/usr/bin:/bin:/usr/local/bin'}
INCOMPLETE_LINE = f'pr-supply: list incomplete for {REPO}: bound {BOUND} observed {PROBE}+'
MARKER_SUFFIX = ':list-incomplete'


def make_pr(n):
    return {'number': n, 'title': f'Test PR {n}',
            'url': f'https://github.com/{REPO}/pull/{n}', 'isDraft': False,
            'mergeStateStatus': 'CLEAN', 'headRefOid': f'{n:040x}',
            'createdAt': '2026-10-01T00:00:00Z',
            'statusCheckRollup': [{'name': 'Paperclip Review', 'conclusion': 'SUCCESS'}]}


def marker_key(full):
    return f'{full}{MARKER_SUFFIX}'


def probe_aware_gh(repo_sizes, record=None):
    """Hermetic gh stub: returns min(--limit, repo_size) rows per repo.

    repo_sizes maps repo-full -> open-PR count. record (optional list)
    collects every gh argv for argv-level assertions.
    """
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ['gh', 'pr', 'list']:
            if record is not None:
                record.append(list(cmd))
            full = cmd[cmd.index('-R') + 1]
            limit = int(cmd[cmd.index('--limit') + 1])
            size = repo_sizes.get(full, 0)
            prs = [make_pr(n + 1) for n in range(min(limit, size))]
            return types.SimpleNamespace(stdout=json.dumps(prs), returncode=0, stderr='')
        raise AssertionError(f'unexpected subprocess call in hermetic stub: {cmd[:3]}')
    return fake_run


def _owned_rows():
    return [[CARD, 'case_63ac0cf0', 'in_progress']]


class HarnessMixin:
    def run_tick(self, *, repos, repo_sizes=None, gh_side_effect=None,
                 gh_env_val=None, apply=True, owned=True, held_text=None,
                 initial_state=None):
        comments, api_calls, gh_argvs = [], [], []
        out, err, error = io.StringIO(), io.StringIO(), None
        new_ids = [0]

        if gh_side_effect is None:
            gh_side_effect = probe_aware_gh(repo_sizes or {}, record=gh_argvs)
        else:
            inner = gh_side_effect

            def gh_side_effect(cmd, **kwargs):  # noqa: F811 (wrap to record argv)
                if cmd[:3] == ['gh', 'pr', 'list']:
                    gh_argvs.append(list(cmd))
                return inner(cmd, **kwargs)

        def fake_api(method, path, body):
            api_calls.append((path, body))
            if path.endswith('/issues'):
                new_ids[0] += 1
                return {'id': f'new-{new_ids[0]}', 'identifier': f'case_698e8eb7-{new_ids[0]}'}
            return {'id': f'wp-{len(api_calls)}'}

        def fake_psql(sql):
            if 'issue_work_products' not in sql:
                return []
            return _owned_rows() if owned else []

        def fake_run(cmd, **kwargs):
            return gh_side_effect(cmd, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {'host_ok': host_ok_stub()}), \
                mock.patch.object(ps, 'APPLY', apply), \
                mock.patch.object(ps, 'STATE', Path(tmp, 'state.json')), \
                mock.patch.object(ps, 'HELD', Path(tmp, 'held.txt')), \
                mock.patch.object(ps, 'PAUSE', Path(tmp, 'PAUSE')), \
                mock.patch.object(ps, 'LOG', Path(tmp, 'operator.log')), \
                mock.patch.object(ps, 'REPOS', repos), \
                mock.patch.object(rr, 'gh_env', return_value=dict(gh_env_val or TOKEN_ENV)), \
                mock.patch.object(rr, 'psql', side_effect=fake_psql), \
                mock.patch.object(ps, 'api', side_effect=fake_api), \
                mock.patch.object(rr, 'card_hold', return_value=None), \
                mock.patch.object(rr.live_run_guard, 'issue_has_live_work', return_value=False), \
                mock.patch.object(rr, 'comment',
                                   side_effect=lambda iid, body: comments.append((iid, body)) or '200'), \
                mock.patch.object(ps.subprocess, 'run', side_effect=fake_run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            held_file = Path(tmp, 'held.txt')
            if held_text is not None:
                held_file.write_text(held_text)
            if initial_state is not None:
                Path(tmp, 'state.json').write_text(json.dumps(initial_state))
            try:
                ps.main()
            except SystemExit as e:
                error = e
            except Exception as e:  # noqa: BLE001 (harness surfaces crashes to assertions)
                error = e
            state_file = Path(tmp, 'state.json')
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
            held = held_file.read_text() if held_file.exists() else None
        return {'comments': comments, 'api_calls': api_calls, 'gh_argvs': gh_argvs,
                'stdout': out.getvalue(), 'stderr': err.getvalue(), 'state': state,
                'error': error, 'held': held}

    def gh_limits(self, result):
        return [a[a.index('--limit') + 1] for a in result['gh_argvs']]

    def creates(self, result):
        return [p for p, _ in result['api_calls'] if p.endswith('/issues')]


class ProbeScheme(unittest.TestCase):
    def call_list_prs(self, repo_sizes, full=REPO):
        record, out = [], io.StringIO()
        with mock.patch.object(ps.subprocess, 'run',
                               side_effect=probe_aware_gh(repo_sizes, record=record)), \
                contextlib.redirect_stdout(out):
            prs = ps.list_prs(full, dict(TOKEN_ENV))
        return prs, record, out.getvalue()

    def test_bound_constant_is_100(self):
        self.assertEqual(getattr(ps, 'PR_SUPPLY_LIST_BOUND', None), BOUND)

    def test_gh_argv_uses_limit_probe_bound_plus_one(self):
        _, record, _ = self.call_list_prs({REPO: 1})
        self.assertEqual(len(record), 1)
        argv = record[0]
        self.assertEqual(argv[:3], ['gh', 'pr', 'list'])
        self.assertEqual(argv[argv.index('--limit') + 1], str(PROBE))

    def test_exactly_one_gh_call_per_repo(self):
        _, record, _ = self.call_list_prs({REPO: BOUND})
        self.assertEqual(len(record), 1)

    def test_exactly_at_bound_returns_complete_with_no_incomplete_signal(self):
        prs, record, out = self.call_list_prs({REPO: BOUND})
        self.assertEqual(len(prs), BOUND)
        self.assertNotIn('list incomplete', out)
        self.assertNotIn('list failed', out)

    def test_empty_listing_is_complete_with_no_signal(self):
        prs, _, out = self.call_list_prs({REPO: 0})
        self.assertEqual(prs, [])
        self.assertNotIn('list incomplete', out)
        self.assertNotIn('list failed', out)

    def test_over_bound_returns_bounded_set_with_incomplete_signal(self):
        prs, record, out = self.call_list_prs({REPO: PROBE})
        self.assertEqual(len(prs), BOUND)
        self.assertEqual([p['number'] for p in prs], list(range(1, BOUND + 1)))
        self.assertIn(INCOMPLETE_LINE, out)

    def test_probe_row_is_never_reconciled(self):
        prs, _, _ = self.call_list_prs({REPO: PROBE})
        self.assertNotIn(PROBE, [p['number'] for p in prs])


class IncompleteTick(HarnessMixin, unittest.TestCase):
    REPOS = {'two-web-next': ('Web Engineer', 'TWO Web Next', 0)}

    def test_over_bound_tick_reconciles_bounded_set_and_emits_incomplete_signal(self):
        held = 'OLD held line\n'
        result = self.run_tick(repos=self.REPOS, repo_sizes={REPO: PROBE},
                               held_text=held)
        self.assertIsNone(result['error'])
        # Bounded set still reconciled (nudge path, capped by MAX_ACTIONS).
        self.assertEqual(len(result['comments']), ps.MAX_ACTIONS)
        receipts = [k for k in result['state'] if '@' in k]
        self.assertEqual(len(receipts), ps.MAX_ACTIONS)
        # Incomplete signal: log line + state marker.
        self.assertIn(INCOMPLETE_LINE, result['stdout'])
        marker = result['state'].get(marker_key(REPO))
        self.assertIsNotNone(marker)
        self.assertEqual(marker['bound'], BOUND)
        self.assertEqual(marker['observed'], PROBE)
        # Coverage claims never include unseen PRs: bounded count only,
        # held file left unchanged on incomplete coverage.
        self.assertIn(f'{BOUND} actionable open PRs', result['stdout'])
        self.assertNotIn(f'{PROBE} actionable open PRs', result['stdout'])
        self.assertEqual(result['held'], held)
        # Single probe call per repo.
        self.assertEqual(len(result['gh_argvs']), 1)
        self.assertEqual(self.gh_limits(result), [str(PROBE)])

    def test_probe_row_gets_no_receipt_no_comment_no_card(self):
        result = self.run_tick(repos=self.REPOS, repo_sizes={REPO: PROBE})
        probe_head = f'{PROBE:040x}'
        self.assertFalse([k for k in result['state'] if probe_head in k])
        self.assertFalse([b for _, b in result['comments'] if f'#{PROBE}' in b or probe_head in b])
        self.assertFalse([b for _, b in result['api_calls'] if f'#{PROBE}' in json.dumps(b)])

    def test_complete_tick_clears_stale_incomplete_marker(self):
        stale = {marker_key(REPO): {'bound': BOUND, 'observed': PROBE,
                                    'at': '2026-10-01T00:00:00Z'}}
        result = self.run_tick(repos=self.REPOS, repo_sizes={REPO: BOUND},
                               initial_state=stale, held_text='OLD held\n')
        self.assertIsNone(result['error'])
        self.assertNotIn(marker_key(REPO), result['state'])
        self.assertNotIn('list incomplete', result['stdout'])
        self.assertIn('merge-ready PRs held by blocked cards', result['held'])

    def test_incomplete_marker_shape(self):
        result = self.run_tick(repos=self.REPOS, repo_sizes={REPO: PROBE})
        marker = result['state'].get(marker_key(REPO))
        self.assertIsNotNone(marker)
        self.assertEqual(marker['bound'], BOUND)
        self.assertEqual(marker['observed'], PROBE)
        self.assertRegex(marker['at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


class RefusalPins(HarnessMixin, unittest.TestCase):
    REPOS = {'repo-a': ('Web Engineer', 'TWO Web Next', 0),
             'two-web-next': ('Web Engineer', 'TWO Web Next', 0)}
    HEALTHY = [make_pr(7)]

    def failing_tick(self, failing_side_effect):
        def fake_run(cmd, **kwargs):
            if cmd[:3] == ['gh', 'pr', 'list'] and cmd[cmd.index('-R') + 1] == REPO_A:
                return failing_side_effect(cmd, **kwargs)
            if cmd[:3] == ['gh', 'pr', 'list']:
                return types.SimpleNamespace(stdout=json.dumps(self.HEALTHY),
                                             returncode=0, stderr='')
            raise AssertionError(f'unexpected call: {cmd[:3]}')
        return self.run_tick(repos=self.REPOS, gh_side_effect=fake_run)

    def assert_per_repo_refusal(self, result, reason):
        self.assertIsNone(result['error'])
        self.assertIn(f'pr-supply: list failed for {REPO_A}: {reason}', result['stdout'])
        self.assertFalse([k for k in result['state'] if 'repo-a' in k])
        self.assertFalse([p for p, _ in result['api_calls'] if 'repo-a' in json.dumps(p)])
        # Healthy repo still processed.
        self.assertEqual(len(result['comments']), 1)

    def test_timeout_refuses_per_repo_with_zero_writes(self):
        def fail(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd[0], 60)
        self.assert_per_repo_refusal(self.failing_tick(fail), 'timeout after')

    def test_nonzero_exit_refuses_per_repo_with_zero_writes(self):
        def fail(cmd, **kwargs):
            return types.SimpleNamespace(stdout='', returncode=1, stderr='boom')
        self.assert_per_repo_refusal(self.failing_tick(fail), 'gh exit 1')

    def test_empty_output_refuses_per_repo_with_zero_writes(self):
        def fail(cmd, **kwargs):
            return types.SimpleNamespace(stdout=' \n', returncode=0, stderr='')
        self.assert_per_repo_refusal(self.failing_tick(fail), 'empty output')

    def test_invalid_json_refuses_per_repo_with_zero_writes(self):
        for stdout in ('{not json', 'null', '{}', '"str"', '123', '[1,2'):
            with self.subTest(stdout=stdout):
                def fail(cmd, _s=stdout, **kwargs):
                    return types.SimpleNamespace(stdout=_s, returncode=0, stderr='')
                result = self.failing_tick(fail)
                self.assertIsNone(result['error'])
                self.assertIn(f'pr-supply: list failed for {REPO_A}: invalid JSON',
                              result['stdout'])
                self.assertFalse([k for k in result['state'] if 'repo-a' in k])
                self.assertEqual(len(result['comments']), 1)

    def test_empty_token_makes_no_listing_call_with_zero_writes(self):
        repos = {'two-web-next': ('Web Engineer', 'TWO Web Next', 0)}
        result = self.run_tick(repos=repos, repo_sizes={REPO: 1},
                               gh_env_val={'GH_TOKEN': '', 'PATH': '/usr/bin:/bin'})
        self.assertEqual(result['gh_argvs'], [])
        self.assertIn(f'pr-supply: list failed for {REPO}: empty token', result['stdout'])
        self.assertEqual(result['state'], {})
        self.assertEqual(result['comments'], [])
        self.assertIsInstance(result['error'], SystemExit)
        self.assertNotEqual(result['error'].code, 0)

    def test_failed_listing_leaves_held_file_unchanged(self):
        held = 'OLD held line\n'
        repos = {'two-web-next': ('Web Engineer', 'TWO Web Next', 0)}

        def fail(cmd, **kwargs):
            return types.SimpleNamespace(stdout='', returncode=1, stderr='boom')
        result = self.run_tick(repos=repos, gh_side_effect=fail, held_text=held)
        self.assertEqual(result['held'], held)

    def test_failed_listing_writes_no_incomplete_marker(self):
        repos = {'two-web-next': ('Web Engineer', 'TWO Web Next', 0)}

        def fail(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd[0], 60)
        result = self.run_tick(repos=repos, gh_side_effect=fail)
        self.assertNotIn(marker_key(REPO), result['state'])


class CapInteraction(HarnessMixin, unittest.TestCase):
    REPOS = {'two-web-next': ('Web Engineer', 'TWO Web Next', 0)}

    def test_max_actions_bounds_nudges_on_incomplete_tick(self):
        result = self.run_tick(repos=self.REPOS, repo_sizes={REPO: PROBE})
        self.assertEqual(len(result['comments']), ps.MAX_ACTIONS)
        receipts = [k for k in result['state'] if '@' in k]
        self.assertEqual(len(receipts), ps.MAX_ACTIONS)
        self.assertIn(INCOMPLETE_LINE, result['stdout'])
        self.assertIn(marker_key(REPO), result['state'])

    def test_max_new_cards_bounds_creates_on_incomplete_tick(self):
        result = self.run_tick(repos=self.REPOS, repo_sizes={REPO: PROBE}, owned=False)
        self.assertEqual(len(self.creates(result)), ps.MAX_NEW_CARDS)
        markers = [k for k in result['state'] if k.endswith(':create-timeout')]
        self.assertEqual(markers, [])
        self.assertIn(INCOMPLETE_LINE, result['stdout'])
        self.assertIn(marker_key(REPO), result['state'])
        pending = [k for k in result['state'] if k.endswith(':pending-card')]
        self.assertEqual(pending, [])


if __name__ == '__main__':
    unittest.main()
