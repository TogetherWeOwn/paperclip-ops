"""Immutable acceptance: idle-waiter live path preserves scheduled retries.

Builder contract (paired builder implements; this file is never edited by the
builder). Synthetic, offline, public-safe. No live DB/API/queue/host access.

Required new behavior (currently RED):
- Use the existing shared fail-closed live-run guard immediately before every
  parking PATCH and before touched-state persistence. Running, queued or
  scheduled_retry work keyed by issueId or taskId is protected; a run
  appearing after initial selection also prevents parking.
- Align candidate filtering with the shared statuses/context keys without
  duplicating a divergent definition. Guard lookup failure is
  unmeasured/skip, never permission to park. Preserve waiter/doer classifier
  and actual blocker relationships.
- Provide versioned JSON summary output with stable issue IDs, skip
  reason/error codes and next_action for live or unmeasured cases, retaining
  text compatibility. Dry-run is idempotent and makes no board writes or
  touched/flagged-state changes; invalid flags cannot apply parking.
- Bundle filter alignment, last-moment guard and diagnostics/docs in one PR.
  Keep atomic state validation intact; document guard semantics beside the
  canonical entrypoint with one offline check, not a second policy source.

Builder interfaces accepted by these tests (backward compatible):
- w.run(apply=...) keeps its signature and text prints ('waiters:' line plus
  PARK/NODOER lines). It MUST also return a versioned dict report OR print a
  JSON object containing that report to stdout. Either path satisfies the
  structured-output tests. Extra keys are allowed; volatile keys (at,
  timestamp, duration) are ignored for stability comparisons.
- Report shape (keys may use these aliases):
    {"version": 1, "dry_run": bool,
     "parked": [{"issue": ident, "issue_id": iid, ...}],
     "skipped": [{"issue": ident, "issue_id": iid,
                  "reason"|"skip_reason"|"error": "...",
                  "next_action": "..."}]}
  Top-level "skips"/"skip" alias accepted for "skipped". Per-entry aliases
  accepted: issue/ident/identifier, issue_id/iid/id. Reason aliases:
  reason/skip_reason/error/code. Version accepts 1 or "1".
- Live skip: reason mentions live/scheduled_retry/running/queued.
  Unmeasured skip (guard lookup failure/exception): reason mentions
  unmeasured/error/fail/unknown/guard. Both require non-empty next_action in
  {skip, wait, retry, retry_later, manual_review, unknown, refused, none}
  (case-insensitive substring match).
- Invalid flags: w.main() with an unknown flag plus --apply MUST NOT park
  (zero api calls, zero touched writes). Acceptable refusals: SystemExit
  non-zero, StateError/ValueError, or a returned/printed report whose skip
  reason mentions invalid/refused/unknown with next_action refused/unknown
  and zero api calls.
- Guard delegation: idle_waiters MUST import live_run_guard and call
  issue_has_live_work(iid) immediately before each PATCH and before touched
  persistence. Tests mock live_run_guard.issue_has_live_work (and
  w.issue_has_live_work / w.live_run_guard when present). A builder that
  duplicates the query instead of calling the shared guard will fail the
  behavioral tests by design.
- A run arriving during durable touched-receipt persistence must be caught by
  the second guard; remove only that provisional receipt, preserve unrelated
  touched/flagged entries, and report the live skip without a board write.
- Only literal False permits parking at either guard; unexpected values are
  unmeasured skips. A post-persist refusal removes the provisional receipt.
- Candidate SQL: the selection query ('with n as') MUST contain
  'scheduled_retry' and 'taskId' (coalesce issueId/taskId) while retaining
  'running' and 'queued'.
- Docs: idle_waiters.py source MUST contain 'live_run_guard',
  'issue_has_live_work', 'scheduled_retry', and a fail-closed mention
  ('fail-closed' or 'fail closed' or 'fail_closed') beside the canonical
  entrypoint. One offline static check enforces a single policy source:
  no 'def issue_has_live_work' redefinition in idle_waiters.py.

Positive/negative controls that already PASS on the red baseline:
- No-live-work parks once; waiter/doer classifier preserved; atomic state
  validation intact; dry-run makes no api calls; text compatibility retained.

Run:
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/productivity -p 'test_idle_wait*.py'
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/lib -p 'test_live_run_guard.py'
"""
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

import atomic_state as a  # noqa: E402
import idle_waiters as w  # noqa: E402
import live_run_guard  # noqa: E402

# Synthetic, public-safe identifiers. Doer refs use split concatenation so the
# source never contains a literal internal task identifier pattern.
WAITER_IID = '00000000-0000-4000-8000-000000000061'
WAITER_IDENT = 'case_live_path_01'
DOER_IID = '00000000-0000-4000-8000-000000000062'
DOER_AGENT = '00000000-0000-4000-8000-000000000063'
DOER_REF = 'TOG-' '999999401'
DOER_REF_B = 'TOG-' '999999402'
ROLLUP_IID = '00000000-0000-4000-8000-000000000064'
NEXT_ACTIONS = {'skip', 'wait', 'retry', 'retry_later', 'manual_review', 'unknown', 'refused', 'none'}


def waiter(iid, ident, title, notes):
    return [iid, ident, 'in_progress', title, '', notes, '']


def eligible_doer_card(title='Build parser'):
    return [DOER_IID, 'in_progress', DOER_AGENT, title, '0']


class Board:
    def __init__(self, waiters=(), cards=None, blockers=None, rollup=ROLLUP_IID):
        self.waiters = [list(r) for r in waiters]
        self.cards = cards or {}
        self.blockers = blockers or {}
        self.rollup = rollup

    def psql(self, sql):
        if 'with n as' in sql:
            return self.waiters
        if 'select o.identifier' in sql:
            iid = sql.split("related_issue_id='")[1].split("'")[0]
            return [[b] for b in self.blockers.get(iid, [])]
        if 'select id from issues where company_id' in sql:
            return [[self.rollup]] if self.rollup else []
        ident = sql.split("i.identifier='")[1].split("'")[0]
        return [self.cards[ident]] if ident in self.cards else []


def extract_report(ret, stdout):
    """Return the versioned report dict from return value or stdout JSON."""
    if isinstance(ret, dict) and 'version' in ret:
        return ret
    text = (stdout or '').strip()
    if not text:
        return None
    # Fast path: whole stdout is JSON.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and 'version' in obj:
            return obj
    except ValueError:
        pass
    # Slow path: last JSON object in stdout (text lines plus JSON report).
    start = text.rfind('{')
    end = text.rfind('}')
    while start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict) and 'version' in obj:
                return obj
        except ValueError:
            pass
        start = text.rfind('{', 0, start)
    return None


def report_lists(report):
    parked = report.get('parked', []) if isinstance(report, dict) else []
    skipped = []
    if isinstance(report, dict):
        for key in ('skipped', 'skips', 'skip'):
            if isinstance(report.get(key), list):
                skipped = report.get(key)
                break
    return parked or [], skipped or []


def entry_issue(entry):
    for key in ('issue', 'ident', 'identifier'):
        if entry.get(key):
            return entry.get(key)
    return None


def entry_iid(entry):
    for key in ('issue_id', 'iid', 'id'):
        if entry.get(key):
            return entry.get(key)
    return None


def entry_reason(entry):
    for key in ('reason', 'skip_reason', 'error', 'code'):
        if entry.get(key):
            return str(entry.get(key))
    return ''


def entry_next(entry):
    for key in ('next_action', 'nextAction', 'action'):
        if entry.get(key):
            return str(entry.get(key))
    return ''


def stable_subset(report):
    parked, skipped = report_lists(report)

    def park_key(e):
        return (entry_issue(e), entry_iid(e))

    def skip_key(e):
        return (entry_issue(e), entry_iid(e), entry_reason(e).lower(), entry_next(e).lower())

    return {
        'version': str(report.get('version')),
        'parked': sorted([park_key(e) for e in parked if isinstance(e, dict)]),
        'skipped': sorted([skip_key(e) for e in skipped if isinstance(e, dict)]),
    }


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / 'idle-waiters.json'
        self.board = Board()
        self.psql = mock.Mock(side_effect=lambda sql: self.board.psql(sql))
        self.api = mock.Mock(return_value={'status': 'blocked'})
        self.log = mock.Mock()
        for name, value in (('STATE', str(self.state)), ('STATE_DIR', str(self.dir)), ('psql', self.psql),
                            ('api', self.api), ('log', self.log)):
            patch = mock.patch.object(w, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def write_json(self, obj):
        self.state.write_text(json.dumps(obj))

    def disk(self):
        return json.loads(self.state.read_bytes())

    def run_apply_capture(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ret = w.run(apply=True)
        return ret, buf.getvalue()

    def run_dry_capture(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ret = w.run(apply=False)
        return ret, buf.getvalue()

    def patch_guard(self, return_value=None, side_effect=None, exception=None):
        """Mock the shared guard at every import location the builder may use."""
        if exception is not None:
            p = mock.patch.object(live_run_guard, 'issue_has_live_work', side_effect=exception)
        elif side_effect is not None:
            p = mock.patch.object(live_run_guard, 'issue_has_live_work', side_effect=side_effect)
        else:
            p = mock.patch.object(live_run_guard, 'issue_has_live_work', return_value=return_value)
        primary = p.start()
        self.addCleanup(p.stop)
        if hasattr(w, 'issue_has_live_work'):
            if exception is not None:
                p2 = mock.patch.object(w, 'issue_has_live_work', side_effect=exception)
            elif side_effect is not None:
                p2 = mock.patch.object(w, 'issue_has_live_work', side_effect=side_effect)
            else:
                p2 = mock.patch.object(w, 'issue_has_live_work', return_value=return_value)
            p2.start()
            self.addCleanup(p2.stop)
        if hasattr(w, 'live_run_guard') and w.live_run_guard is not live_run_guard:
            try:
                if exception is not None:
                    p3 = mock.patch.object(w.live_run_guard, 'issue_has_live_work', side_effect=exception)
                elif side_effect is not None:
                    p3 = mock.patch.object(w.live_run_guard, 'issue_has_live_work', side_effect=side_effect)
                else:
                    p3 = mock.patch.object(w.live_run_guard, 'issue_has_live_work', return_value=return_value)
                p3.start()
                self.addCleanup(p3.stop)
            except (AttributeError, TypeError):
                pass
        return primary

    def waiter_with_doer(self, notes=None):
        ref = DOER_REF
        self.write_json({'touched': {}, 'flagged': {}})
        self.board = Board(
            waiters=[waiter(WAITER_IID, WAITER_IDENT, 'Card live path',
                            notes or ('no-op: nothing new; no-op: waiting on ' + ref))],
            cards={ref: eligible_doer_card()})

    def patch_calls(self):
        return [c for c in self.api.call_args_list if c.args and c.args[0] == 'PATCH']

    def selection_sql(self):
        for c in self.psql.call_args_list:
            sql = c.args[0] if c.args else ''
            if 'with n as' in sql:
                return sql
        return ''


class LiveGuardBeforePark(Base):
    def test_scheduled_retry_is_protected_no_patch_no_touched(self):
        self.waiter_with_doer()
        guard = self.patch_guard(return_value=True)
        _, _ = self.run_apply_capture()
        self.assertEqual(self.patch_calls(), [], 'scheduled_retry work must prevent parking')
        self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))
        guard.assert_called()
        called_ids = [str(c.args[0]) for c in guard.call_args_list if c.args]
        self.assertIn(WAITER_IID, called_ids)

    def test_task_id_keyed_running_is_protected(self):
        self.waiter_with_doer()
        guard = self.patch_guard(return_value=True)
        _, _ = self.run_apply_capture()
        self.assertEqual(self.patch_calls(), [], 'taskId-keyed running work must prevent parking')
        self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))
        guard.assert_called_with(WAITER_IID)

    def test_task_id_keyed_queued_is_protected(self):
        self.waiter_with_doer(notes='no-op: no change needed; no-op: pending ' + DOER_REF)
        guard = self.patch_guard(return_value=True)
        _, _ = self.run_apply_capture()
        self.assertEqual(self.patch_calls(), [], 'taskId-keyed queued work must prevent parking')
        self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))
        self.assertTrue(guard.called)

    def test_newly_arrived_run_at_final_guard_prevents_parking(self):
        # Selection already returned the waiter; the final guard sees live work.
        self.waiter_with_doer()
        order = []
        orig_psql = self.psql.side_effect

        def track_psql(sql):
            order.append('psql')
            return orig_psql(sql)

        self.psql.side_effect = track_psql
        guard = self.patch_guard(return_value=True)
        orig_guard = guard.side_effect

        def track_guard(iid):
            order.append('guard')
            return True if orig_guard is None else orig_guard(iid)

        guard.side_effect = track_guard
        _, _ = self.run_apply_capture()
        self.assertIn('psql', order, 'selection query must run before the final guard')
        self.assertIn('guard', order, 'final guard must run after selection, before any PATCH')
        self.assertLess(order.index('psql'), order.index('guard'))
        self.assertEqual(self.patch_calls(), [])
        self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))

    def test_guard_lookup_failure_is_unmeasured_skip_never_park(self):
        self.waiter_with_doer()
        with mock.patch.object(live_run_guard.subprocess, 'run', side_effect=OSError('no docker')):
            _, _ = self.run_apply_capture()
        self.assertEqual(self.patch_calls(), [], 'lookup failure is unmeasured/skip, never park')
        self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))

    def test_guard_exception_is_unmeasured_skip_not_crash(self):
        self.waiter_with_doer()
        self.patch_guard(exception=OSError('db down'))
        try:
            _, _ = self.run_apply_capture()
        except OSError:
            self.fail('guard exception must be treated as unmeasured/skip, not crash')
        self.assertEqual(self.patch_calls(), [])
        self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))

    def test_run_arriving_during_receipt_persistence_removes_provisional_receipt(self):
        # Catches a missing post-persist guard or missing receipt rollback.
        self.waiter_with_doer()
        prior = {'touched': {'case_prior_waiter': time.time()},
                 'flagged': {'case_prior_flag': time.time()}}
        self.write_json(prior)
        live = False
        observed_receipts = []
        real_persist = w.persist

        def persist_then_arrive(state):
            nonlocal live
            real_persist(state)
            if WAITER_IDENT in self.disk()['touched']:
                live = True

        def guard_after_arrival(iid):
            self.assertEqual(iid, WAITER_IID)
            observed_receipts.append(WAITER_IDENT in self.disk()['touched'])
            return live

        self.patch_guard(side_effect=guard_after_arrival)
        with mock.patch.object(w, 'persist', side_effect=persist_then_arrive):
            ret, out = self.run_apply_capture()
        self.assertTrue(live, 'fixture must inject arrival after a real durable receipt')
        self.assertGreaterEqual(len(observed_receipts), 2)
        self.assertFalse(observed_receipts[0], 'first guard must precede the receipt')
        self.assertIn(True, observed_receipts[1:], 'a second guard must see the provisional receipt')
        self.api.assert_not_called()
        self.assertEqual(self.disk(), prior, 'rollback must preserve unrelated touched/flagged entries')
        report = extract_report(ret, out)
        self.assertIsNotNone(report)
        parked, skipped = report_lists(report)
        self.assertEqual(parked, [])
        match = [e for e in skipped if entry_iid(e) == WAITER_IID]
        self.assertTrue(match)
        self.assertIn('live', entry_reason(match[0]).lower())
        self.assertTrue(any(k in entry_next(match[0]).lower() for k in NEXT_ACTIONS))

    def test_only_literal_false_permits_parking_at_either_guard(self):
        # Catches truthiness/equality checks accepting 0, empty or malformed results.
        for stage in ('before_receipt', 'after_receipt'):
            for value in (None, 0, '', [], {}, 'false', 1, object()):
                with self.subTest(stage=stage, value=value):
                    self.waiter_with_doer()
                    prior = {'touched': {'case_prior_waiter': time.time()},
                             'flagged': {'case_prior_flag': time.time()}}
                    self.write_json(prior)
                    self.api.reset_mock()
                    receipts = []

                    def unexpected_guard(iid):
                        self.assertEqual(iid, WAITER_IID)
                        has_receipt = WAITER_IDENT in self.disk()['touched']
                        receipts.append(has_receipt)
                        if stage == 'after_receipt' and not has_receipt:
                            return False
                        return value

                    self.patch_guard(side_effect=unexpected_guard)
                    ret, out = self.run_apply_capture()
                    self.assertTrue(receipts, 'parking must consult the shared guard')
                    self.assertFalse(receipts[0])
                    if stage == 'after_receipt':
                        self.assertIn(True, receipts, 'exercise the post-persist guard')
                    else:
                        self.assertNotIn(True, receipts, 'unknown first result must not permit a receipt')
                    self.api.assert_not_called()
                    self.assertEqual(self.disk(), prior)
                    report = extract_report(ret, out)
                    self.assertIsNotNone(report)
                    parked, skipped = report_lists(report)
                    self.assertEqual(parked, [])
                    match = [e for e in skipped if entry_iid(e) == WAITER_IID]
                    self.assertTrue(match, 'unexpected guard result requires a diagnostic')
                    self.assertTrue(any(k in entry_reason(match[0]).lower()
                                        for k in ('unmeasured', 'error', 'fail', 'unknown', 'guard')))
                    self.assertTrue(any(k in entry_next(match[0]).lower() for k in NEXT_ACTIONS))

    def test_positive_control_no_live_work_parks_once(self):
        self.waiter_with_doer()
        self.patch_guard(return_value=False)
        _, _ = self.run_apply_capture()
        patches = self.patch_calls()
        self.assertEqual(len(patches), 1)
        method, path, body = patches[0].args
        self.assertEqual((method, path), ('PATCH', f'issues/{WAITER_IID}'))
        self.assertEqual(body['status'], 'blocked')
        self.assertEqual(body['blockedByIssueIds'], [DOER_IID])
        self.assertIn(WAITER_IDENT, self.disk()['touched'])


class CandidateFilterAlignment(Base):
    def test_candidate_sql_includes_scheduled_retry_and_task_keys(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.board = Board(waiters=[waiter(WAITER_IID, WAITER_IDENT, 'Card live path', 'no-op: nothing new')])
        self.patch_guard(return_value=False)
        self.run_dry_capture()
        sql = self.selection_sql()
        self.assertIn('with n as', sql)
        self.assertIn('scheduled_retry', sql)
        self.assertIn('taskId', sql)
        self.assertIn('running', sql)
        self.assertIn('queued', sql)

    def test_uses_shared_guard_not_divergent_definition(self):
        source = Path(w.__file__).read_text()
        self.assertIn('live_run_guard', source)
        self.assertIn('issue_has_live_work', source)
        self.assertNotIn('def issue_has_live_work', source)
        self.assertIn('scheduled_retry', source)

    def test_preserves_waiter_doer_classifier(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.patch_guard(return_value=False)
        self.board = Board(waiters=[
            waiter('uuid-op-61', 'case_live_op_61', 'Operator: rotate host cert', 'no-op: nothing new'),
            waiter('uuid-pr-61', 'case_live_pr_61', 'Watch the CI',
                   'no-op: waiting on pull/812 CI checks; no-op: nothing new'),
        ])
        _, _ = self.run_apply_capture()
        self.assertEqual(self.patch_calls(), [])
        self.assertEqual(self.api.call_count, 0)
        # No-doer waiter is flagged once (existing behavior preserved).
        self.write_json({'touched': {}, 'flagged': {}})
        self.api.reset_mock()
        self.board = Board(waiters=[waiter('uuid-nd-61', 'case_live_nd_61', 'Card no doer', 'no-op: nothing new')])
        _, _ = self.run_apply_capture()
        self.assertEqual(self.patch_calls(), [])
        self.assertEqual(self.api.call_count, 1)
        method, path, _ = self.api.call_args.args
        self.assertEqual(method, 'POST')

    def test_preserves_actual_blocker_relationships(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.patch_guard(return_value=False)
        # Eligible doer via existing open blocker (no text ref needed).
        self.board = Board(
            waiters=[waiter(WAITER_IID, WAITER_IDENT, 'Card live path', 'no-op: nothing new; no-op: no change')],
            cards={DOER_REF: eligible_doer_card()},
            blockers={WAITER_IID: [DOER_REF]})
        _, _ = self.run_apply_capture()
        patches = self.patch_calls()
        self.assertEqual(len(patches), 1)
        _, _, body = patches[0].args
        self.assertEqual(body['blockedByIssueIds'], [DOER_IID])
        # Ineligible doers (closed, unassigned, passive, blocked) never park.
        for title, status, agent, blocked in [('Done work', 'done', DOER_AGENT, '0'),
                                              ('No owner', 'in_progress', '', '0'),
                                              ('Weekly monitor rollup', 'in_progress', DOER_AGENT, '0'),
                                              ('Blocked work', 'in_progress', DOER_AGENT, '2')]:
            with self.subTest(title=title):
                self.write_json({'touched': {}, 'flagged': {}})
                self.api.reset_mock()
                self.board = Board(
                    waiters=[waiter(WAITER_IID, WAITER_IDENT, 'Card live path',
                                    'no-op: nothing new; no-op: waiting on ' + DOER_REF_B)],
                    cards={DOER_REF_B: [DOER_IID, status, agent, title, blocked]})
                _, _ = self.run_apply_capture()
                self.assertEqual(self.patch_calls(), [])


class StructuredOutputs(Base):
    def test_versioned_json_summary_with_stable_ids(self):
        self.waiter_with_doer()
        self.patch_guard(return_value=False)
        ret, out = self.run_apply_capture()
        report = extract_report(ret, out)
        self.assertIsNotNone(report, 'run must return or print a versioned JSON report')
        self.assertIn(str(report.get('version')), ('1',))
        parked, _ = report_lists(report)
        self.assertTrue(parked, 'parked entries must list stable issue IDs')
        found = [(entry_issue(e), entry_iid(e)) for e in parked if isinstance(e, dict)]
        self.assertIn((WAITER_IDENT, WAITER_IID), found)

    def test_live_skip_has_reason_and_next_action(self):
        self.waiter_with_doer()
        self.patch_guard(return_value=True)
        ret, out = self.run_apply_capture()
        self.assertEqual(self.patch_calls(), [])
        report = extract_report(ret, out)
        self.assertIsNotNone(report)
        _, skipped = report_lists(report)
        match = [e for e in skipped if isinstance(e, dict)
                 and entry_issue(e) == WAITER_IDENT and entry_iid(e) == WAITER_IID]
        self.assertTrue(match, 'live skip must carry stable issue IDs')
        reason = entry_reason(match[0]).lower()
        self.assertTrue(any(k in reason for k in ('live', 'scheduled_retry', 'running', 'queued')), reason)
        nxt = entry_next(match[0]).lower()
        self.assertTrue(nxt, 'live skip requires next_action')
        self.assertTrue(any(k in nxt for k in NEXT_ACTIONS), nxt)

    def test_unmeasured_skip_has_error_and_next_action(self):
        self.waiter_with_doer()
        self.patch_guard(exception=OSError('db down'))
        try:
            ret, out = self.run_apply_capture()
        except OSError:
            self.fail('unmeasured guard failure must not crash')
        self.assertEqual(self.patch_calls(), [])
        report = extract_report(ret, out)
        self.assertIsNotNone(report)
        _, skipped = report_lists(report)
        match = [e for e in skipped if isinstance(e, dict)
                 and entry_issue(e) == WAITER_IDENT and entry_iid(e) == WAITER_IID]
        self.assertTrue(match, 'unmeasured skip must carry stable issue IDs')
        reason = entry_reason(match[0]).lower()
        self.assertTrue(any(k in reason for k in ('unmeasured', 'error', 'fail', 'unknown', 'guard')), reason)
        nxt = entry_next(match[0]).lower()
        self.assertTrue(nxt, 'unmeasured skip requires next_action')
        self.assertTrue(any(k in nxt for k in NEXT_ACTIONS), nxt)

    def test_dry_run_idempotent_no_writes_stable_json(self):
        self.waiter_with_doer()
        self.patch_guard(return_value=False)
        before = self.state.read_bytes()
        ret1, out1 = self.run_dry_capture()
        mid = self.state.read_bytes()
        ret2, out2 = self.run_dry_capture()
        after = self.state.read_bytes()
        self.assertEqual(self.api.call_count, 0, 'dry-run makes no board writes')
        self.assertEqual(before, mid)
        self.assertEqual(mid, after)
        r1 = extract_report(ret1, out1)
        r2 = extract_report(ret2, out2)
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(stable_subset(r1), stable_subset(r2))

    def test_invalid_flags_cannot_apply_parking(self):
        self.waiter_with_doer()
        self.patch_guard(return_value=False)
        with mock.patch.object(sys, 'argv', ['idle_waiters.py', '--apply', '--bogus-flag-xyz-61']):
            buf = io.StringIO()
            outcome = None
            try:
                with contextlib.redirect_stdout(buf):
                    outcome = w.main()
            except SystemExit as e:
                outcome = e
            except (ValueError, a.StateError) as e:
                outcome = e
        self.assertEqual(self.api.call_count, 0, 'invalid flags must never apply parking')
        self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))
        if isinstance(outcome, SystemExit):
            self.assertNotEqual(outcome.code, 0)
        elif isinstance(outcome, dict) and 'version' in outcome:
            _, skipped = report_lists(outcome)
            reasons = ' '.join(entry_reason(e).lower() for e in skipped if isinstance(e, dict))
            nexts = ' '.join(entry_next(e).lower() for e in skipped if isinstance(e, dict))
            self.assertTrue(any(k in reasons for k in ('invalid', 'refused', 'unknown', 'error')), reasons)
            self.assertTrue(any(k in nexts for k in ('refused', 'unknown')), nexts)
        else:
            report = extract_report(outcome if isinstance(outcome, dict) else None, buf.getvalue())
            if report is not None:
                _, skipped = report_lists(report)
                reasons = ' '.join(entry_reason(e).lower() for e in skipped if isinstance(e, dict))
                self.assertTrue(any(k in reasons for k in ('invalid', 'refused', 'unknown', 'error')), reasons)

    def test_text_compatibility_retained(self):
        self.waiter_with_doer()
        self.patch_guard(return_value=False)
        _, dry_out = self.run_dry_capture()
        self.assertIn('waiters:', dry_out)
        _, apply_out = self.run_apply_capture()
        self.assertIn('waiters:', apply_out)
        self.assertIn('PARK', apply_out)

    def test_zero_live_effect_negatives_for_protected_cases(self):
        for name, setup_guard in [('live', lambda: self.patch_guard(return_value=True)),
                                  ('unmeasured', lambda: self.patch_guard(exception=OSError('down')))]:
            with self.subTest(case=name):
                self.waiter_with_doer()
                self.api.reset_mock()
                setup_guard()
                flagged_before = dict(self.disk().get('flagged', {}))
                try:
                    _, _ = self.run_apply_capture()
                except OSError:
                    self.fail(f'{name} must not crash')
                self.assertEqual(self.patch_calls(), [], f'{name}: zero parking PATCHes')
                self.assertEqual(self.api.call_count, 0, f'{name}: zero board writes')
                self.assertNotIn(WAITER_IDENT, self.disk().get('touched', {}))
                self.assertEqual(self.disk().get('flagged', {}), flagged_before)


class BundleDocsAndSafety(Base):
    def test_atomic_state_validation_intact(self):
        self.patch_guard(return_value=False)
        self.board = Board(waiters=[waiter(WAITER_IID, WAITER_IDENT, 'Card live path', 'no-op: nothing new')])
        for raw in (None, b'', b'[]', b'{"touched": {}}',
                    b'{"touched": {}, "flagged": {}, "cap": 3}'):
            with self.subTest(raw=raw):
                if raw is None:
                    if self.state.exists():
                        self.state.unlink()
                else:
                    self.state.write_bytes(raw)
                self.psql.reset_mock()
                self.api.reset_mock()
                with self.assertRaises(a.StateError):
                    self.run_apply_capture()
                self.psql.assert_not_called()
                self.assertEqual(self.api.call_count, 0)

    def test_guard_semantics_documented_beside_entrypoint(self):
        source = Path(w.__file__).read_text()
        lowered = source.lower()
        self.assertIn('live_run_guard', source)
        self.assertIn('issue_has_live_work', source)
        self.assertIn('scheduled_retry', source)
        self.assertTrue('fail-closed' in lowered or 'fail closed' in lowered or 'fail_closed' in lowered)

    def test_single_policy_source_offline_check(self):
        source = Path(w.__file__).read_text()
        self.assertIn('live_run_guard', source)
        self.assertNotIn('def issue_has_live_work', source)
        self.assertNotIn('LIVE_STATUSES', source)
        self.assertIn('taskId', source)

    def test_source_only_no_live_subprocess_in_dry_run(self):
        self.waiter_with_doer()
        self.patch_guard(return_value=False)
        with mock.patch('subprocess.run', side_effect=AssertionError('live subprocess')) as run_mock:
            _, _ = self.run_dry_capture()
        run_mock.assert_not_called()
        self.assertEqual(self.api.call_count, 0)


if __name__ == '__main__':
    unittest.main()
