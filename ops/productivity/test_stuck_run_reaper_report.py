#!/usr/bin/env python3
"""Immutable acceptance: reaper ranked triage report (M12.5).

Builder contract (paired Builder implements; this file is never edited by
the Builder). Synthetic, offline, public-safe. No live DB/API/host access.

Required new behavior (currently RED):
- New read-only `report` subcommand:
    stuck_run_reaper.py report [--top N] [--min-idle-hours H] [--json] [--dry-run]
  Ranks dead-run suspects by idle age desc, retry-loop suspects by
  (runs desc, newest failure desc); ties deterministic (run/issue id asc).
  Default top 10; N validated 1-100. --min-idle-hours gates dead runs only
  and never relaxes below the 2h dead rule (additional filter, not a
  replacement). --dry-run is a no-op alias documenting read-onlyness.
- Full inventory, not action-capped: rows beyond MAX_ACTIONS appear with
  ordinary ranks; the report states the action cap separately.
- Same strict measurement as the actor: UNMEASURED PID probes list as
  `unmeasured` with diagnostics, never ranked dead; live-work cards list
  as `deferred_live_work`, never as parkable.
- --json emits a versioned envelope (schema version, generated_at, ranked
  rows with stable run/issue IDs + age/class/evidence, excluded counts
  with reasons, error code + retryable + next_action). Text keeps one row
  per line, greppable.
- Read-only: no cancels, no parks, no ledger writes, no state writes.
  Unknown flags refuse with usage + exit 2.

Builder interfaces accepted by these tests (backward compatible):
- Entry: `stuck_run_reaper.main()` with sys.argv patched to
  ['stuck_run_reaper.py', 'report', ...]. main() MUST exist and dispatch
  the report subcommand while preserving the existing run()/--recover-state
  behavior. It MAY return a dict report AND/OR print JSON/text to stdout;
  tests accept either path. Extra keys are allowed; volatile keys
  (generated_at/at/timestamp/duration) are ignored for stability compares.
- Fetch reuse: tests mock `find_dead`/`find_loops` for ranking, and
  `psql_json`/`probe_container_pid`/`operator_remote_alive` plus
  `live_run_guard.issue_has_live_work` for measurement strictness. A Builder
  that reuses find_dead/find_loops and the pure classifiers
  is_dead/loop_verdict/classify passes by design. A Builder that bypasses
  the shared live guard or the probe helpers will fail the behavioral tests.
  Time: report MUST call r.utc() for now (tests patch it for determinism).
- Envelope aliases accepted (top level):
    schema_version/version/schemaVersion (=1, int or "1"),
    generated_at/generatedAt/at/timestamp (ISO-8601),
    top/limit, min_idle_hours/minIdleHours/min-idle-hours,
    action_cap/actionCap/max_actions/MAX_ACTIONS (=MAX_ACTIONS),
    dead_runs/dead/deadRuns, retry_loops/loops/retryLoops,
    unmeasured/unmeasured_runs/unmeasuredRuns,
    deferred/deferred_live_work/deferredLiveWork,
    excluded/excluded_counts/counts/skipped,
    error/{code,retryable,next_action} OR top-level
      error_code/code + retryable + next_action/nextAction.
  Per-row aliases: rank/ranked (int 1-based); run_id/run/id;
    issue_id/iid/id; issue/ident/identifier; idle_hours/idle_h/age_hours;
    last_activity/lastActivity; error_class/class/errorClass;
    evidence/why/detail; runs/run_count/runCount; run_ids/runs_list;
    newest_failure/newestFailure; diagnostic/diagnostics.
  Excluded counts: dict {reason: count} OR list [{reason,count}]. Reasons
    use stable substrings: unmeasured, deferred_live_work/live_work,
    below_min_idle/min_idle. Counts must reconcile with list lengths.
  Error aliases: code/error_code/errorCode; retryable/retryableBool;
    next_action/nextAction. Success code is ok/success/0/"0".
- Text aliases: lines containing DEAD/dead + run id; LOOP/loop + issue id;
  UNMEASURED/unmeasured + diagnostic; DEFERRED/deferred + live_work/live.
  Action cap line contains cap + str(MAX_ACTIONS).
- Invalid flags: unknown flag, --top outside 1-100 or non-numeric, and
  --min-idle-hours negative or non-numeric MUST refuse with SystemExit
  non-zero (2 preferred), usage to stderr, zero api POST/PATCH/PUT/DELETE
  calls and zero ledger/state writes.

Run:
  (cd ops/productivity && python3 -m unittest test_stuck_run_reaper_report -v)
Regression controls (must stay green):
  (cd ops/productivity && python3 -m unittest test_stuck_run_reaper test_stuck_run_state)
"""
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stuck_run_reaper as r

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)

ISSUE_A = '00000000-0000-4000-8000-0000000000a1'
ISSUE_B = '00000000-0000-4000-8000-0000000000b2'
ISSUE_C = '00000000-0000-4000-8000-0000000000c3'
OTHER_AGENT = '00000000-0000-4000-8000-000000000099'


def iso(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()


def run_row(run_id, minutes_idle, agent='fixture', issue_id=ISSUE_A):
    t = iso(minutes_idle)
    return {'id': run_id, 'status': 'running', 'process_pid': 4242,
            'process_started_at': t, 'last_output_at': t, 'updated_at': t,
            'created_at': t, 'agent_id': OTHER_AGENT, 'agent': agent,
            'issue_id': issue_id}


def failed_run(run_id, minutes_ago, err='ENOSPC: no space'):
    return {'id': run_id, 'status': 'failed', 'created_at': iso(minutes_ago),
            'error_code': 'adapter_failed', 'error': err}


def loop_card(issue_id, ident, status='in_progress'):
    return {'id': issue_id, 'identifier': ident, 'status': status,
            'title': 'fixture %s' % ident, 'blockers': []}


def first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d[k]
    return default


def norm_code(v):
    return str(v).strip().lower() if v is not None else ''


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / 'state.json'
        self.ledger = self.dir / 'ledger.jsonl'
        self.actions = self.dir / 'actions.log'
        self.api = mock.Mock(return_value=('200', None))
        self.log = mock.Mock()
        for name, value in (('STATE', self.state), ('STATE_DIR', self.dir),
                            ('PAUSE', self.dir / 'PAUSE'),
                            ('LEDGER', self.ledger), ('ACTIONS_LOG', self.actions),
                            ('api', self.api), ('log', self.log)):
            p = mock.patch.object(r, name, value)
            p.start()
            self.addCleanup(p.stop)
        save = mock.Mock(side_effect=AssertionError('report must not save state'))
        p = mock.patch.object(r.atomic_state, 'save', save)
        p.start()
        self.addCleanup(p.stop)
        self.save = save

    def invoke(self, *args):
        """Invoke `report ...` via main(); returns (code, out, err, ret)."""
        self.assertTrue(hasattr(r, 'main'), 'RED: stuck_run_reaper.main() missing: '
                        'report subcommand not implemented')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['stuck_run_reaper.py', 'report', *args]), \
                mock.patch.object(r, 'utc', return_value=NOW), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ret = r.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 2
                return code, out.getvalue(), err.getvalue(), None
            except ValueError as e:
                # Builder may raise ValueError for invalid flags; treat as refusal
                # only when nothing was written.
                return 2, out.getvalue(), 'value error: %s' % e, None
        return 0, out.getvalue(), err.getvalue(), ret

    def json_report(self, *args, find_dead=None, find_loops=None, live=False):
        """Invoke with --json under stubbed fetch; return (envelope, raw stdout)."""
        if find_dead is None:
            find_dead = ([], [])
        if find_loops is None:
            find_loops = []
        if isinstance(live, bool):
            guard = mock.Mock(return_value=live)
        else:
            guard = mock.Mock(side_effect=lambda iid: live.get(iid, False))
        with mock.patch.object(r, 'find_dead', return_value=find_dead), \
                mock.patch.object(r, 'find_loops', return_value=find_loops), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', guard):
            code, out, err, ret = self.invoke(*args, '--json') if '--json' not in args \
                else self.invoke(*args)
        self.assertEqual(code, 0, 'report --json must exit 0, stderr=%r' % err[-500:])
        envelope = None
        if isinstance(ret, dict) and ret:
            envelope = ret
        if envelope is None:
            try:
                envelope = json.loads(out)
            except ValueError:
                self.fail('report --json must print one parseable object, got %r' % out[:500])
        self.assertIsInstance(envelope, dict)
        return envelope, out, guard

    def mutating_calls(self):
        return [c for c in self.api.call_args_list
                if c.args and str(c.args[0]).upper() in ('POST', 'PATCH', 'PUT', 'DELETE')]

    def assert_no_writes(self):
        self.assertEqual(self.mutating_calls(), [], 'report must not mutate via the API')
        self.log.assert_not_called()
        self.save.assert_not_called()
        self.assertFalse(self.ledger.exists(), 'report must not write the ledger')
        self.assertFalse(self.actions.exists(), 'report must not write the actions log')

    def dead_rows(self, envelope):
        rows = first(envelope, 'dead_runs', 'dead', 'deadRuns', default=[])
        self.assertIsInstance(rows, list)
        return rows

    def loop_rows(self, envelope):
        rows = first(envelope, 'retry_loops', 'loops', 'retryLoops', default=[])
        self.assertIsInstance(rows, list)
        return rows

    def row_run(self, row):
        return first(row, 'run_id', 'run', 'id')

    def row_issue_id(self, row):
        return first(row, 'issue_id', 'iid', 'id')

    def row_rank(self, row):
        return first(row, 'rank', 'ranked')


class CliContract(Base):
    def test_report_subcommand_exists(self):
        self.assertTrue(hasattr(r, 'main'), 'RED: main() missing')
        with mock.patch.object(r, 'find_dead', return_value=([], [])), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, _, err, _ = self.invoke()
        self.assertEqual(code, 0, 'bare report must exit 0, stderr=%r' % err[-300:])

    def test_unknown_flag_refuses_exit_2(self):
        with mock.patch.object(r, 'find_dead', return_value=([], [])), \
                mock.patch.object(r, 'find_loops', return_value=[]):
            code, _, err, _ = self.invoke('--bogus-flag')
        self.assertEqual(code, 2, 'unknown flags must refuse with exit 2')
        self.assertTrue(err.strip(), 'refusal must print usage to stderr')
        self.assert_no_writes()

    def test_top_bounds_refused(self):
        for bad in ('0', '101', '-1', 'abc', ''):
            with self.subTest(top=bad), \
                    mock.patch.object(r, 'find_dead', return_value=([], [])), \
                    mock.patch.object(r, 'find_loops', return_value=[]):
                code, _, err, _ = self.invoke('--top', bad)
                self.assertEqual(code, 2, '--top %r must refuse with exit 2' % bad)
                self.assertTrue(err.strip())
                self.assert_no_writes()

    def test_top_edges_accepted(self):
        dead = [({**run_row('run-%02d' % n, 300 + n), 'agent': 'fx'}, 'old') for n in range(3)]
        for top in ('1', '100'):
            with self.subTest(top=top), \
                    mock.patch.object(r, 'find_dead', return_value=(dead, [])), \
                    mock.patch.object(r, 'find_loops', return_value=[]), \
                    mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
                code, _, err, _ = self.invoke('--top', top)
                self.assertEqual(code, 0, '--top %s must exit 0: %s' % (top, err[-300:]))

    def test_min_idle_hours_invalid_refused(self):
        for bad in ('-1', 'abc', ''):
            with self.subTest(val=bad), \
                    mock.patch.object(r, 'find_dead', return_value=([], [])), \
                    mock.patch.object(r, 'find_loops', return_value=[]):
                code, _, err, _ = self.invoke('--min-idle-hours', bad)
                self.assertEqual(code, 2, '--min-idle-hours %r must refuse' % bad)
                self.assertTrue(err.strip())
                self.assert_no_writes()

    def test_dry_run_is_noop_alias(self):
        dead = [(run_row('run-dry-1', 400), 'old'), (run_row('run-dry-2', 500), 'old')]
        with mock.patch.object(r, 'find_dead', return_value=(dead, [])), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            c1, o1, e1, r1 = self.invoke('--json')
            c2, o2, e2, r2 = self.invoke('--json', '--dry-run')
        self.assertEqual((c1, c2), (0, 0), '%r %r' % (e1[-200:], e2[-200:]))
        j1, j2 = json.loads(o1) if not isinstance(r1, dict) else r1, \
            json.loads(o2) if not isinstance(r2, dict) else r2
        for vol in ('generated_at', 'generatedAt', 'at', 'timestamp'):
            j1.pop(vol, None)
            j2.pop(vol, None)
        self.assertEqual(j1, j2, '--dry-run must not change the read-only report')
        self.assert_no_writes()


class DeadRanking(Base):
    def test_idle_age_desc(self):
        dead = [(run_row('run-old', 600), 'old'), (run_row('run-mid', 300), 'old'),
                (run_row('run-new', 180), 'old')]
        env, _, _ = self.json_report(find_dead=(list(reversed(dead)), []))
        got = [self.row_run(x) for x in self.dead_rows(env)[:3]]
        self.assertEqual(got, ['run-old', 'run-mid', 'run-new'])

    def test_idle_tie_breaks_on_run_id_asc(self):
        dead = [(run_row('run-b', 400), 'old'), (run_row('run-a', 400), 'old'),
                (run_row('run-c', 400), 'old')]
        env, _, _ = self.json_report(find_dead=(dead, []))
        got = [self.row_run(x) for x in self.dead_rows(env)[:3]]
        self.assertEqual(got, ['run-a', 'run-b', 'run-c'])

    def test_ranks_are_ordinary_and_sequential(self):
        dead = [(run_row('run-%d' % n, 200 + n * 10), 'old') for n in range(4)]
        env, _, _ = self.json_report(find_dead=(dead, []))
        ranks = [self.row_rank(x) for x in self.dead_rows(env)[:4]]
        self.assertEqual(ranks, [1, 2, 3, 4])

    def test_min_idle_hours_gates_dead_only(self):
        dead = [(run_row('run-old', 600), 'old'), (run_row('run-mid', 300), 'old'),
                (run_row('run-young', 150), 'old')]
        loops = [(loop_card(ISSUE_A, 'case_gate_a'), 'enospc',
                  [failed_run('lr-1', 5), failed_run('lr-2', 30), failed_run('lr-3', 60)])]
        env, _, _ = self.json_report('--min-idle-hours', '6', find_dead=(dead, []),
                                     find_loops=loops, live=False)
        got = [self.row_run(x) for x in self.dead_rows(env)]
        self.assertIn('run-old', got)
        self.assertNotIn('run-mid', got, '300m idle must be gated by --min-idle-hours 6')
        self.assertNotIn('run-young', got)
        self.assertEqual(len(self.loop_rows(env)), 1, 'loops are not idle-gated')

    def test_min_idle_hours_below_dead_rule_changes_nothing(self):
        dead = [(run_row('run-old', 600), 'old')]
        env, _, _ = self.json_report('--min-idle-hours', '1', find_dead=(dead, []))
        self.assertEqual([self.row_run(x) for x in self.dead_rows(env)], ['run-old'])

    def test_default_top_is_10_per_section(self):
        dead = [(run_row('run-%02d' % n, 200 + n), 'old') for n in range(12)]
        loops = [(loop_card('00000000-0000-4000-8000-%012d' % n, 'case_top_%02d' % n), 'enospc',
                  [failed_run('t%d-1' % n, 5), failed_run('t%d-2' % n, 6),
                   failed_run('t%d-3' % n, 7)]) for n in range(12)]
        env, _, _ = self.json_report(find_dead=(dead, []), find_loops=loops, live=False)
        self.assertEqual(len(self.dead_rows(env)), 10, 'default top must cap dead at 10')
        self.assertEqual(len(self.loop_rows(env)), 10, 'default top must cap loops at 10')

    def test_top_caps_each_section(self):
        dead = [(run_row('run-%02d' % n, 200 + n), 'old') for n in range(5)]
        loops = [(loop_card('00000000-0000-4000-8000-%012d' % (100 + n), 'case_cap_%02d' % n),
                  'enospc', [failed_run('c%d-1' % n, 5), failed_run('c%d-2' % n, 6),
                              failed_run('c%d-3' % n, 7)]) for n in range(5)]
        env, _, _ = self.json_report('--top', '2', find_dead=(dead, []),
                                     find_loops=loops, live=False)
        self.assertEqual(len(self.dead_rows(env)), 2)
        self.assertEqual(len(self.loop_rows(env)), 2)


class LoopRanking(Base):
    def test_runs_desc_then_newest_failure_desc(self):
        newest = [(loop_card(ISSUE_A, 'case_loop_a'), 'enospc',
                   [failed_run('a-1', 50), failed_run('a-2', 60), failed_run('a-3', 70)]),
                  (loop_card(ISSUE_B, 'case_loop_b'), 'enospc',
                   [failed_run('b-1', 5), failed_run('b-2', 6), failed_run('b-3', 7),
                    failed_run('b-4', 8)]),
                  (loop_card(ISSUE_C, 'case_loop_c'), 'enospc',
                   [failed_run('c-1', 5), failed_run('c-2', 30), failed_run('c-3', 60)])]
        env, _, _ = self.json_report(find_loops=newest, live=False)
        got = [self.row_issue_id(x) for x in self.loop_rows(env)[:3]]
        self.assertEqual(got, [ISSUE_B, ISSUE_C, ISSUE_A],
                         'more runs first; same runs -> newest failure first')

    def test_loop_tie_breaks_on_issue_id_asc(self):
        mk = lambda iid, ident: (loop_card(iid, ident), 'enospc',
                                 [failed_run(ident + '-1', 10), failed_run(ident + '-2', 20),
                                  failed_run(ident + '-3', 30)])
        loops = [mk(ISSUE_C, 'case_tie_c'), mk(ISSUE_A, 'case_tie_a'), mk(ISSUE_B, 'case_tie_b')]
        env, _, _ = self.json_report(find_loops=loops, live=False)
        got = [self.row_issue_id(x) for x in self.loop_rows(env)[:3]]
        self.assertEqual(got, sorted([ISSUE_A, ISSUE_B, ISSUE_C]))

    def test_loop_ranks_are_ordinary(self):
        loops = [(loop_card('00000000-0000-4000-8000-%012d' % (200 + n), 'case_rank_%d' % n),
                  'enospc', [failed_run('r%d-1' % n, 5), failed_run('r%d-2' % n, 6),
                              failed_run('r%d-3' % n, 7)]) for n in range(3)]
        env, _, _ = self.json_report(find_loops=loops, live=False)
        self.assertEqual([self.row_rank(x) for x in self.loop_rows(env)[:3]], [1, 2, 3])

    def test_loop_rows_carry_class_and_evidence(self):
        loops = [(loop_card(ISSUE_A, 'case_evd_a'), 'enospc',
                  [failed_run('e-1', 5), failed_run('e-2', 30), failed_run('e-3', 60)])]
        env, _, _ = self.json_report(find_loops=loops, live=False)
        row = self.loop_rows(env)[0]
        cls = first(row, 'error_class', 'class', 'errorClass')
        self.assertEqual(cls, 'enospc')
        runs = first(row, 'runs', 'run_count', 'runCount', 'run_ids', 'runs_list')
        if isinstance(runs, int):
            self.assertEqual(runs, 3)
        else:
            self.assertEqual(len(runs), 3)


class FullInventory(Base):
    def test_rows_beyond_max_actions_keep_ordinary_ranks(self):
        dead = [(run_row('run-%02d' % n, 200 + n * 5), 'old') for n in range(7)]
        self.assertGreater(len(dead), r.MAX_ACTIONS)
        env, _, _ = self.json_report('--top', '10', find_dead=(dead, []))
        rows = self.dead_rows(env)
        self.assertEqual(len(rows), 7, 'full inventory must exceed MAX_ACTIONS')
        self.assertEqual([self.row_rank(x) for x in rows], [1, 2, 3, 4, 5, 6, 7])

    def test_action_cap_stated_separately_in_json(self):
        env, _, _ = self.json_report(find_dead=([(run_row('run-cap', 400), 'old')], []))
        cap = first(env, 'action_cap', 'actionCap', 'max_actions', 'MAX_ACTIONS')
        self.assertEqual(cap, r.MAX_ACTIONS)

    def test_action_cap_stated_in_text(self):
        dead = [(run_row('run-cap-t', 400), 'old')]
        with mock.patch.object(r, 'find_dead', return_value=(dead, [])), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, out, err, _ = self.invoke()
        self.assertEqual(code, 0, err[-300:])
        self.assertIn(str(r.MAX_ACTIONS), out)
        self.assertIn('cap', out.lower())


class Measurement(Base):
    def test_unmeasured_listed_never_ranked_dead(self):
        dead = [(run_row('run-dead', 500), 'old')]
        unmeasured = [{'run': 'run-probe-9', 'agent': 'fixture', 'issue': ISSUE_A,
                       'diagnostic': 'local PID probe unmeasured: probe timed out after 30s'}]
        env, _, _ = self.json_report(find_dead=(dead, unmeasured))
        self.assertEqual([self.row_run(x) for x in self.dead_rows(env)], ['run-dead'])
        un = first(env, 'unmeasured', 'unmeasured_runs', 'unmeasuredRuns', default=[])
        self.assertEqual(len(un), 1)
        diag = first(un[0], 'diagnostic', 'diagnostics', 'evidence', 'why', 'detail', default='')
        self.assertIn('timed out', str(diag))
        blob = json.dumps(env)
        self.assertNotIn('run-probe-9', json.dumps(self.dead_rows(env)), 'unmeasured must never rank dead')
        self.assertIn('run-probe-9', blob)

    def test_probe_unmeasured_end_to_end_via_stubs(self):
        t = iso(500)
        prow = {'id': 'run-e2e-9', 'status': 'running', 'process_pid': 123,
                'process_started_at': t, 'last_output_at': t, 'updated_at': t,
                'created_at': t, 'agent_id': OTHER_AGENT, 'agent': 'fixture',
                'issue_id': ISSUE_A}
        with mock.patch.object(r, 'psql_json', return_value=[prow]), \
                mock.patch.object(r, 'probe_container_pid',
                                   return_value=(r.PID_UNMEASURED, 'probe timed out after 30s')), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, out, err, ret = self.invoke('--json')
        self.assertEqual(code, 0, err[-300:])
        env = ret if isinstance(ret, dict) else json.loads(out)
        self.assertEqual(self.dead_rows(env), [])
        un = first(env, 'unmeasured', 'unmeasured_runs', 'unmeasuredRuns', default=[])
        self.assertEqual(len(un), 1)
        self.assertIn('run-e2e-9', json.dumps(un))

    def test_live_work_deferred_never_parkable(self):
        loops = [(loop_card(ISSUE_A, 'case_live_a'), 'enospc',
                  [failed_run('la-1', 5), failed_run('la-2', 6), failed_run('la-3', 7)]),
                 (loop_card(ISSUE_B, 'case_live_b'), 'enospc',
                  [failed_run('lb-1', 5), failed_run('lb-2', 6), failed_run('lb-3', 7)])]
        env, out, guard = self.json_report(find_loops=loops,
                                           live={ISSUE_A: True, ISSUE_B: False})
        parkable = [self.row_issue_id(x) for x in self.loop_rows(env)]
        self.assertEqual(parkable, [ISSUE_B])
        deferred = first(env, 'deferred', 'deferred_live_work', 'deferredLiveWork', default=[])
        self.assertEqual(len(deferred), 1)
        self.assertEqual(first(deferred[0], 'issue_id', 'iid', 'id'), ISSUE_A)
        reason = first(deferred[0], 'reason', 'skip_reason', 'error', 'code', 'status', default='')
        self.assertIn('live_work', str(reason).lower())
        called = [c.args[0] for c in guard.call_args_list if c.args]
        self.assertIn(ISSUE_A, called)
        self.assertIn(ISSUE_B, called)

    def test_live_probe_failure_counts_as_deferred(self):
        loops = [(loop_card(ISSUE_A, 'case_livefail_a'), 'enospc',
                  [failed_run('lf-1', 5), failed_run('lf-2', 6), failed_run('lf-3', 7)])]
        with mock.patch.object(r, 'find_dead', return_value=([], [])), \
                mock.patch.object(r, 'find_loops', return_value=loops), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work',
                                   side_effect=OSError('db down')):
            code, out, err, ret = self.invoke('--json')
        self.assertEqual(code, 0, err[-300:])
        env = ret if isinstance(ret, dict) else json.loads(out)
        self.assertEqual(self.loop_rows(env), [], 'guard failure must not read as parkable')
        deferred = first(env, 'deferred', 'deferred_live_work', 'deferredLiveWork', default=[])
        self.assertEqual(len(deferred), 1)

    def test_empty_board(self):
        env, _, _ = self.json_report(find_dead=([], []), find_loops=[], live=False)
        self.assertEqual(self.dead_rows(env), [])
        self.assertEqual(self.loop_rows(env), [])
        self.assertEqual(first(env, 'unmeasured', 'unmeasured_runs', 'unmeasuredRuns', default=[]), [])
        self.assertEqual(first(env, 'deferred', 'deferred_live_work', 'deferredLiveWork', default=[]), [])
        err = first(env, 'error', default=None)
        if isinstance(err, dict):
            self.assertIn(norm_code(first(err, 'code', 'error_code', 'errorCode')), ('ok', 'success', '0'))
        else:
            code = first(env, 'error_code', 'code', 'errorCode', default='ok')
            self.assertIn(norm_code(code), ('ok', 'success', '0'))


class JsonEnvelope(Base):
    def test_schema_version_and_generated_at(self):
        env, _, _ = self.json_report(find_dead=([(run_row('run-env', 400), 'old')], []))
        ver = first(env, 'schema_version', 'version', 'schemaVersion')
        self.assertIn(str(ver), ('1',), 'schema version must be 1')
        gen = first(env, 'generated_at', 'generatedAt', 'at', 'timestamp')
        self.assertTrue(gen, 'envelope must carry generated_at')
        parsed = r.parse_ts(str(gen)) if hasattr(r, 'parse_ts') else None
        if parsed is None:
            try:
                parsed = dt.datetime.fromisoformat(str(gen).replace('Z', '+00:00'))
            except ValueError:
                parsed = None
        self.assertIsNotNone(parsed, 'generated_at must be ISO-8601')
        self.assertLess(abs((parsed - NOW).total_seconds()), 3600)

    def test_dead_rows_carry_stable_ids_age_evidence(self):
        env, _, _ = self.json_report(find_dead=([(run_row('run-stable', 375), 'old')], []))
        row = self.dead_rows(env)[0]
        self.assertEqual(self.row_run(row), 'run-stable')
        self.assertEqual(first(row, 'issue_id', 'iid', 'issueId'), ISSUE_A)
        age = first(row, 'idle_hours', 'idle_h', 'age_hours', 'idleHours', 'ageHours')
        self.assertIsNotNone(age, 'dead rows must carry idle age')
        self.assertGreaterEqual(float(age), 6.0)
        self.assertLess(float(age), 6.5)
        ev = first(row, 'evidence', 'why', 'detail', 'reason', default='')
        self.assertTrue(str(ev).strip(), 'dead rows must carry evidence')

    def test_excluded_counts_with_reasons(self):
        dead = [(run_row('run-old', 600), 'old'), (run_row('run-mid', 300), 'old')]
        unmeasured = [{'run': 'run-u1', 'agent': 'fx', 'issue': ISSUE_A, 'diagnostic': 'timeout'}]
        loops = [(loop_card(ISSUE_A, 'case_ex_a'), 'enospc',
                  [failed_run('xa-1', 5), failed_run('xa-2', 6), failed_run('xa-3', 7)])]
        env, _, _ = self.json_report('--min-idle-hours', '6', find_dead=(dead, unmeasured),
                                     find_loops=loops, live={ISSUE_A: True})
        excl = first(env, 'excluded', 'excluded_counts', 'counts', 'skipped', default=None)
        self.assertIsNotNone(excl, 'envelope must carry excluded counts with reasons')
        if isinstance(excl, dict):
            blob = json.dumps(excl).lower()
            self.assertIn('unmeasured', blob)
            self.assertTrue('live_work' in blob or 'deferred' in blob)
            self.assertTrue('min_idle' in blob or 'below_min' in blob or 'min-idle' in blob)
            total = sum(v for v in excl.values() if isinstance(v, int))
            self.assertGreaterEqual(total, 3)
        else:
            self.assertIsInstance(excl, list)
            reasons = ' '.join(str(first(e, 'reason', 'code', 'error', default='')) for e in excl).lower()
            self.assertIn('unmeasured', reasons)
            self.assertTrue('live_work' in reasons or 'deferred' in reasons)

    def test_error_code_retryable_next_action(self):
        env, _, _ = self.json_report(find_dead=([(run_row('run-err', 400), 'old')], []))
        err = first(env, 'error', default=None)
        if isinstance(err, dict):
            code = first(err, 'code', 'error_code', 'errorCode')
            retryable = first(err, 'retryable', 'retryableBool')
            nxt = first(err, 'next_action', 'nextAction')
        else:
            code = first(env, 'error_code', 'code', 'errorCode')
            retryable = first(env, 'retryable', 'retryableBool')
            nxt = first(env, 'next_action', 'nextAction')
        self.assertIn(norm_code(code), ('ok', 'success', '0'))
        self.assertIsInstance(retryable, bool)
        self.assertTrue(str(nxt).strip(), 'envelope must carry next_action')

    def test_json_is_single_object_on_stdout(self):
        dead = [(run_row('run-one', 400), 'old')]
        with mock.patch.object(r, 'find_dead', return_value=(dead, [])), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, out, err, ret = self.invoke('--json')
        self.assertEqual(code, 0, err[-300:])
        if not isinstance(ret, dict):
            try:
                parsed = json.loads(out)
            except ValueError:
                self.fail('json mode must print one parseable object, got %r' % out[:500])
            self.assertIsInstance(parsed, dict)


class TextMode(Base):
    def test_one_row_per_line_greppable(self):
        dead = [(run_row('run-grep-1', 600), 'old'), (run_row('run-grep-2', 500), 'old')]
        loops = [(loop_card(ISSUE_A, 'case_grep_a'), 'enospc',
                  [failed_run('g-1', 5), failed_run('g-2', 6), failed_run('g-3', 7)])]
        with mock.patch.object(r, 'find_dead', return_value=(dead, [])), \
                mock.patch.object(r, 'find_loops', return_value=loops), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, out, err, _ = self.invoke()
        self.assertEqual(code, 0, err[-300:])
        lines = out.splitlines()
        for rid in ('run-grep-1', 'run-grep-2'):
            hits = [ln for ln in lines if rid in ln]
            self.assertEqual(len(hits), 1, '%s must appear on exactly one line' % rid)
            self.assertTrue('dead' in hits[0].lower() or 'rank' in hits[0].lower())
        hits = [ln for ln in lines if 'case_grep_a' in ln or ISSUE_A in ln]
        self.assertEqual(len(hits), 1, 'loop row must be one line')
        self.assertIn('enospc', hits[0])

    def test_text_lists_unmeasured_and_deferred(self):
        unmeasured = [{'run': 'run-text-u', 'agent': 'fx', 'issue': ISSUE_A,
                       'diagnostic': 'probe timed out after 30s'}]
        loops = [(loop_card(ISSUE_B, 'case_text_b'), 'enospc',
                  [failed_run('t-1', 5), failed_run('t-2', 6), failed_run('t-3', 7)])]
        with mock.patch.object(r, 'find_dead', return_value=([], unmeasured)), \
                mock.patch.object(r, 'find_loops', return_value=loops), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=True):
            code, out, err, _ = self.invoke()
        self.assertEqual(code, 0, err[-300:])
        low = out.lower()
        self.assertIn('run-text-u', out)
        self.assertIn('unmeasured', low)
        self.assertIn('timed out', out)
        self.assertTrue('case_text_b' in out or ISSUE_B in out)
        self.assertIn('deferred', low)

    def test_text_empty_board_still_greppable(self):
        with mock.patch.object(r, 'find_dead', return_value=([], [])), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, out, err, _ = self.invoke()
        self.assertEqual(code, 0, err[-300:])
        self.assertTrue(out.strip(), 'empty text report must still print a summary')
        self.assertIn(str(r.MAX_ACTIONS), out)


class ReadOnly(Base):
    def test_no_post_patch_writes(self):
        dead = [(run_row('run-ro-1', 400), 'old')]
        loops = [(loop_card(ISSUE_A, 'case_ro_a'), 'enospc',
                  [failed_run('ro-1', 5), failed_run('ro-2', 6), failed_run('ro-3', 7)])]
        with mock.patch.object(r, 'find_dead', return_value=(dead, [])), \
                mock.patch.object(r, 'find_loops', return_value=loops), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, _, err, _ = self.invoke('--json')
            self.assertEqual(code, 0, err[-300:])
            code, _, err, _ = self.invoke()
            self.assertEqual(code, 0, err[-300:])
        self.assert_no_writes()

    def test_state_file_untouched_or_absent(self):
        self.assertFalse(self.state.exists())
        with mock.patch.object(r, 'find_dead', return_value=([], [])), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, _, err, _ = self.invoke('--json')
        self.assertEqual(code, 0, err[-300:])
        self.assertFalse(self.state.exists(), 'report must not create state')
        self.save.assert_not_called()

    def test_corrupt_state_does_not_block_report(self):
        self.state.write_text('{"anchor_id":')
        with mock.patch.object(r, 'find_dead', return_value=([], [])), \
                mock.patch.object(r, 'find_loops', return_value=[]), \
                mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False):
            code, _, err, _ = self.invoke('--json')
        self.assertEqual(code, 0, 'read-only report must not fail on state: %s' % err[-300:])
        self.assertEqual(self.state.read_text(), '{"anchor_id":')

    def test_refusal_paths_write_nothing(self):
        self.state.write_text('{}')
        before = self.state.read_bytes()
        with mock.patch.object(r, 'find_dead', return_value=([], [])), \
                mock.patch.object(r, 'find_loops', return_value=[]):
            for args in (('--bogus',), ('--top', '0'), ('--min-idle-hours', '-2')):
                with self.subTest(args=args):
                    code, _, _, _ = self.invoke(*args)
                    self.assertEqual(code, 2)
        self.assertEqual(self.state.read_bytes(), before)
        self.assert_no_writes()


if __name__ == '__main__':
    unittest.main()
