"""Builder-owned pins: rollup-comment deferral and strict CLI modes.

Covers the review findings outside the immutable acceptance file
(test_idle_waiter_live_path.py, never edited here):

- RollupGuard: a CEO rollup comment must not supersede live or unmeasured
  work on the rollup card. When the shared guard reports anything but
  literal False for the rollup id, flagging is deferred to the next tick:
  zero board writes, flagged receipts unchanged, and each waiter skipped
  with next_action retry_later.
- CliModes: --json emits one parseable object; --apply/--dry-run and
  --json/--recover-state conflicts are refused before any effect;
  explicit --dry-run writes nothing; paused --json still envelopes.

Synthetic, offline, public-safe. No live DB/API/queue/host access.
Doer refs use split concatenation so the source never contains a literal
internal task identifier pattern.
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

import idle_waiters as w  # noqa: E402
import live_run_guard  # noqa: E402

WAITER_IID = '00000000-0000-4000-8000-000000000071'
WAITER_IDENT = 'case_cli_71'
DOER_IID = '00000000-0000-4000-8000-000000000072'
DOER_AGENT = '00000000-0000-4000-8000-000000000073'
DOER_REF = 'TOG-' '999999501'
ROLLUP_IID = '00000000-0000-4000-8000-000000000074'


def waiter(iid, ident, title, notes):
    return [iid, ident, 'in_progress', title, '', notes, '']


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

    def patch_guard(self, mapping=None, default=False):
        mapping = mapping or {}

        def fn(iid):
            if iid in mapping:
                value = mapping[iid]
                if isinstance(value, Exception):
                    raise value
                return value
            return default

        patch = mock.patch.object(live_run_guard, 'issue_has_live_work', side_effect=fn)
        guard = patch.start()
        self.addCleanup(patch.stop)
        return guard

    def run_apply(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return w.run(apply=True)

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['idle_waiters.py', *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ret = w.main()
            except SystemExit as e:
                return e, out.getvalue(), err.getvalue()
        return ret, out.getvalue(), err.getvalue()

    def nodoer_board(self, **kwargs):
        self.board = Board(
            waiters=[waiter(WAITER_IID, WAITER_IDENT, 'Card cli seventy-one', 'no-op: nothing new')],
            **kwargs)

    def doer_board(self):
        self.board = Board(
            waiters=[waiter(WAITER_IID, WAITER_IDENT, 'Card cli seventy-one',
                            'no-op: nothing new; no-op: waiting on ' + DOER_REF)],
            cards={DOER_REF: [DOER_IID, 'in_progress', DOER_AGENT, 'Build parser', '0']})


class RollupGuard(Base):
    def test_rollup_live_work_defers_flagging(self):
        prior = {'case_prior_cli': time.time()}
        self.write_json({'touched': {}, 'flagged': dict(prior)})
        self.nodoer_board()
        guard = self.patch_guard({ROLLUP_IID: True}, default=False)
        report = self.run_apply()
        self.assertEqual(self.api.call_count, 0, 'deferred flagging posts nothing')
        self.assertEqual(self.disk()['flagged'], prior, 'flagged receipts unchanged')
        self.assertEqual(report['flagged'], [])
        called = [c.args[0] for c in guard.call_args_list if c.args]
        self.assertIn(ROLLUP_IID, called, 'rollup id must be guard-checked')
        skips = [e for e in report['skipped']
                 if e.get('issue') == WAITER_IDENT and e.get('issue_id') == WAITER_IID]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]['reason'], 'live_or_unmeasured')
        self.assertEqual(skips[0]['error_code'], 'live_or_unmeasured')
        self.assertEqual(skips[0]['next_action'], 'retry_later')

    def test_rollup_guard_lookup_failure_defers_flagging(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.nodoer_board()
        self.patch_guard({ROLLUP_IID: OSError('db down')}, default=False)
        report = self.run_apply()
        self.assertEqual(self.api.call_count, 0, 'unmeasured rollup posts nothing')
        self.assertEqual(self.disk()['flagged'], {})
        skips = [e for e in report['skipped'] if e.get('issue') == WAITER_IDENT]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]['reason'], 'guard_lookup_failed')
        self.assertEqual(skips[0]['next_action'], 'retry_later')

    def test_rollup_no_live_work_posts_once(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.nodoer_board()
        self.patch_guard(default=False)
        report = self.run_apply()
        self.assertEqual(self.api.call_count, 1)
        method, path, body = self.api.call_args.args
        self.assertEqual((method, path), ('POST', f'issues/{ROLLUP_IID}/comments'))
        self.assertIn(WAITER_IDENT, body['body'])
        self.assertIn(WAITER_IDENT, self.disk()['flagged'])
        self.assertEqual([(e['issue'], e['issue_id']) for e in report['flagged']],
                         [(WAITER_IDENT, WAITER_IID)])

    def test_missing_rollup_card_creates_issue_without_guard_block(self):
        # Creating a fresh rollup issue cannot supersede a retry: there is no
        # existing card or run to cancel, so no guard lookup gates creation.
        self.write_json({'touched': {}, 'flagged': {}})
        self.nodoer_board(rollup=None)
        self.patch_guard(default=True)
        self.run_apply()
        self.assertEqual(self.api.call_count, 1)
        method, path, body = self.api.call_args.args
        self.assertEqual((method, path), ('POST', f'companies/{w.CID}/issues'))
        self.assertEqual(body['title'], w.ROLLUP_TITLE)
        self.assertIn(WAITER_IDENT, self.disk()['flagged'])


class CliModes(Base):
    def test_json_emits_single_parseable_object(self):
        self.write_json({'touched': {}, 'flagged': {}})
        raw = self.state.read_bytes()
        self.doer_board()
        self.patch_guard(default=False)
        ret, out, _ = self.run_main('--json')
        report = json.loads(out)
        self.assertEqual(report['version'], 1)
        self.assertTrue(report['dry_run'])
        self.assertEqual(ret, report)
        self.assertEqual(self.api.call_count, 0, 'json dry-run posts nothing')
        self.assertEqual(self.state.read_bytes(), raw, 'json dry-run writes no state')

    def test_explicit_dry_run_makes_no_writes(self):
        self.write_json({'touched': {}, 'flagged': {}})
        raw = self.state.read_bytes()
        self.doer_board()
        self.patch_guard(default=False)
        ret, out, _ = self.run_main('--dry-run')
        self.assertIsInstance(ret, dict)
        self.assertTrue(ret['dry_run'])
        self.assertIn('waiters:', out)
        self.assertEqual(self.api.call_count, 0)
        self.assertEqual(self.state.read_bytes(), raw)

    def test_apply_and_dry_run_conflict_refused(self):
        self.write_json({'touched': {}, 'flagged': {}})
        raw = self.state.read_bytes()
        self.doer_board()
        self.patch_guard(default=False)
        outcome, _, _ = self.run_main('--apply', '--dry-run')
        self.assertIsInstance(outcome, SystemExit)
        self.assertEqual(outcome.code, 2)
        self.assertEqual(self.api.call_count, 0, 'conflicting modes park nothing')
        self.assertEqual(self.state.read_bytes(), raw)

    def test_json_and_recover_state_conflict_refused(self):
        self.write_json({'touched': {}, 'flagged': {}})
        raw = self.state.read_bytes()
        doc = self.dir / 'recovery.json'
        doc.write_text(json.dumps({'provenance': {'source': 'x', 'retrieved_at': 'y', 'complete': True},
                                   'touched': {}, 'flagged': {}}))
        self.patch_guard(default=False)
        outcome, _, _ = self.run_main('--json', '--recover-state', str(doc))
        self.assertIsInstance(outcome, SystemExit)
        self.assertEqual(outcome.code, 2)
        self.assertEqual(self.api.call_count, 0)
        self.assertEqual(self.state.read_bytes(), raw)

    def test_paused_json_reports_paused(self):
        self.write_json({'touched': {}, 'flagged': {}})
        (self.dir / 'PAUSE').write_text('')
        self.doer_board()
        self.patch_guard(default=False)
        ret, out, _ = self.run_main('--json')
        report = json.loads(out)
        self.assertEqual(report['reason'], 'paused')
        self.assertEqual(report['next_action'], 'wait')
        self.assertEqual((report['parked'], report['skipped']), ([], []))
        self.assertEqual(ret, report)
        self.assertEqual(self.api.call_count, 0)
        self.psql.assert_not_called()

    def test_json_apply_reports_parked(self):
        self.write_json({'touched': {}, 'flagged': {}})
        self.doer_board()
        self.patch_guard(default=False)
        ret, out, _ = self.run_main('--json', '--apply')
        report = json.loads(out)
        self.assertFalse(report['dry_run'])
        self.assertEqual([(e['issue'], e['issue_id']) for e in report['parked']],
                         [(WAITER_IDENT, WAITER_IID)])
        self.assertEqual(ret, report)
        patches = [c for c in self.api.call_args_list if c.args and c.args[0] == 'PATCH']
        self.assertEqual(len(patches), 1)


if __name__ == '__main__':
    unittest.main()
