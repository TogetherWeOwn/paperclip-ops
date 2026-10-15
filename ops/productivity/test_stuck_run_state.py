import contextlib
import datetime as dt
import errno
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

import atomic_state as a  # noqa: E402
import live_run_guard as g  # noqa: E402
import stuck_run_reaper as r  # noqa: E402

A = '00000000-0000-4000-8000-000000000062'
B = '00000000-0000-4000-8000-000000000063'


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / 'state.json'
        self.sql = mock.Mock(return_value=[])
        self.api = mock.Mock(side_effect=AssertionError('unexpected API call'))
        for name, value in (('STATE', self.state), ('STATE_DIR', self.dir), ('PAUSE', self.dir / 'PAUSE'),
                            ('psql_json', self.sql), ('api', self.api)):
            patch = mock.patch.object(r, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def disk(self):
        return json.loads(self.state.read_text())


class FailClosed(Base):
    def assert_refused_without_side_effects(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(a.StateError):
            r.run()
        self.sql.assert_not_called()
        self.api.assert_not_called()

    def test_empty_state_stops_before_any_db_or_api_call(self):
        self.state.write_bytes(b'')
        self.assert_refused_without_side_effects()

    def test_missing_state_stops_before_any_db_or_api_call(self):
        self.assert_refused_without_side_effects()

    def test_malformed_or_invalid_state_stops_the_run(self):
        for raw in (b'{"anchor_id":', b'{"anchor_id": "%s", "cap": 3}' % A.encode(), b'{"anchor_id": "not-a-uuid"}',
                    b'{"anchor_pending": true, "anchor_id": "%s"}' % A.encode()):
            with self.subTest(raw=raw):
                self.state.write_bytes(raw)
                self.assert_refused_without_side_effects()


class Anchor(Base):
    def test_live_anchor_is_preserved_without_writes(self):
        self.state.write_text(json.dumps({'anchor_id': A}))
        before = self.state.read_bytes()
        self.sql.side_effect = lambda query: [{'status': 'backlog'}]
        self.assertEqual(r.anchor_id({'anchor_id': A}, False), A)
        self.api.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def test_missing_anchor_is_pending_before_the_post_and_recorded_after(self):
        self.state.write_text('{}')
        seen_during_post = []

        def post(*_args, **_kwargs):
            seen_during_post.append(self.disk())
            return '201', {'id': B}

        self.api.side_effect = post
        self.assertEqual(r.anchor_id({}, False), B)
        self.assertEqual(seen_during_post, [{'anchor_pending': True}])
        self.assertEqual(self.disk(), {'anchor_id': B})

    def test_failed_pending_write_never_posts(self):
        self.state.write_text('{}')
        full = OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(r.atomic_state, 'save', side_effect=full), self.assertRaises(OSError):
            r.anchor_id({}, False)
        self.api.assert_not_called()
        self.assertEqual(self.disk(), {})

    def test_created_anchor_that_cannot_be_recorded_blocks_the_next_run(self):
        self.state.write_text('{}')
        self.api.side_effect = lambda *_args, **_kwargs: ('201', {'id': B})
        real_save = a.save
        writes = []

        def second_write_fails(path, data, validate):
            writes.append(dict(data))
            if len(writes) == 2:
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real_save(path, data, validate)

        with mock.patch.object(r.atomic_state, 'save', side_effect=second_write_fails), \
                self.assertRaises(a.StateError):
            r.anchor_id({}, False)
        self.assertEqual(self.disk(), {'anchor_pending': True})
        self.api.reset_mock()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(a.StateError):
            r.run()
        self.api.assert_not_called()

    def test_unknown_outcome_keeps_pending(self):
        self.state.write_text('{}')
        self.api.side_effect = lambda *_args, **_kwargs: ('502', None)
        with self.assertRaises(a.StateError):
            r.anchor_id({}, False)
        self.assertEqual(self.disk(), {'anchor_pending': True})

    def test_created_without_a_readable_body_keeps_pending(self):
        self.state.write_text('{}')
        self.api.side_effect = lambda *_args, **_kwargs: ('201', None)
        with self.assertRaises(a.StateError):
            r.anchor_id({}, False)
        self.assertEqual(self.disk(), {'anchor_pending': True})

    def test_rejected_post_clears_pending(self):
        self.state.write_text('{}')
        self.api.side_effect = lambda *_args, **_kwargs: ('400', None)
        with self.assertRaises(RuntimeError):
            r.anchor_id({}, False)
        self.assertEqual(self.disk(), {})

    def test_closed_anchor_is_replaced_once_and_recorded(self):
        self.state.write_text(json.dumps({'anchor_id': A}))
        self.sql.side_effect = lambda query: [{'status': 'done'}]
        self.api.side_effect = lambda *_args, **_kwargs: ('201', {'id': B})
        self.assertEqual(r.anchor_id({'anchor_id': A}, False), B)
        self.assertEqual(self.disk(), {'anchor_id': B})
        self.assertEqual(self.api.call_count, 1)

    def test_unresolved_pending_state_refuses_without_a_post(self):
        self.state.write_text(json.dumps({'anchor_pending': True}))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(a.StateError):
                r.run()
        self.api.assert_not_called()

    def test_unresolved_pending_state_still_cancels_dead_runs_then_refuses_parking(self):
        self.state.write_text(json.dumps({'anchor_pending': True}))
        self.api.side_effect = lambda *_args, **_kwargs: ('200', None)
        dead = ({'id': 'run-1', 'agent': 'fixture'}, 'no process')
        with mock.patch.object(r, 'find_dead', return_value=([dead], [])), \
                mock.patch.object(r, 'ACTIONS_LOG', self.dir / 'actions.log'), \
                mock.patch.object(r, 'LEDGER', self.dir / 'ledger.jsonl'), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(a.StateError):
            r.run()
        self.api.assert_called_once_with('POST', '/heartbeat-runs/run-1/cancel', {})


class Run(Base):
    def test_valid_state_with_nothing_to_do_leaves_the_file_untouched(self):
        self.state.write_text(json.dumps({'anchor_id': A}))
        before = self.state.read_bytes()
        self.sql.side_effect = lambda query: [{'status': 'backlog'}] if 'select status from issues' in query else []
        with contextlib.redirect_stdout(io.StringIO()):
            r.run()
        self.assertEqual(self.state.read_bytes(), before)
        self.api.assert_not_called()


class Api(unittest.TestCase):
    def test_body_and_status_come_from_stdout(self):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"id": "%s"}\n201' % B, stderr='')
        with mock.patch.object(r.subprocess, 'run', return_value=done):
            self.assertEqual(r.api('POST', '/x', {}), ('201', {'id': B}))

    def test_failed_body_write_yields_no_body(self):
        done = subprocess.CompletedProcess(args=[], returncode=23, stdout='\n201', stderr='write error')
        with mock.patch.object(r.subprocess, 'run', return_value=done):
            self.assertEqual(r.api('POST', '/x', {}), ('201', None))


class Recovery(Base):
    def doc(self, matches, **provenance):
        prov = {'source': 'board title search', 'retrieved_at': '2026-10-09T03:30:00Z', 'complete': True,
                'title_search': r.ANCHOR_TITLE}
        prov.update(provenance)
        return {'provenance': prov, 'title_search_matches': matches}

    def write_doc(self, doc):
        path = self.dir / 'recovery.json'
        path.write_text(json.dumps(doc))
        return path

    @staticmethod
    def hit(anchor, status='backlog'):
        return {'id': anchor, 'status': status}

    def test_refuses_to_replace_a_valid_anchor(self):
        self.state.write_text(json.dumps({'anchor_id': A}))
        with self.assertRaises(a.StateError):
            r.recover_state(self.write_doc(self.doc([self.hit(B)])))
        self.assertEqual(self.disk(), {'anchor_id': A})

    def test_empty_state_recovers_to_the_single_title_match(self):
        self.state.write_bytes(b'')
        r.recover_state(self.write_doc(self.doc([self.hit(A)])))
        self.assertEqual(self.disk(), {'anchor_id': A})
        self.api.assert_not_called()

    def test_pending_state_resolves_with_title_evidence(self):
        self.state.write_text(json.dumps({'anchor_pending': True}))
        r.recover_state(self.write_doc(self.doc([self.hit(B)])))
        self.assertEqual(self.disk(), {'anchor_id': B})

    def test_zero_title_matches_records_no_anchor(self):
        self.state.write_bytes(b'')
        r.recover_state(self.write_doc(self.doc([])))
        self.assertEqual(self.disk(), {})

    def test_closed_match_does_not_block_recovery_of_the_open_anchor(self):
        self.state.write_bytes(b'')
        r.recover_state(self.write_doc(self.doc([self.hit(A, 'done'), self.hit(B, 'in_progress')])))
        self.assertEqual(self.disk(), {'anchor_id': B})
        self.api.assert_not_called()

    def test_only_closed_matches_record_no_anchor(self):
        self.state.write_bytes(b'')
        r.recover_state(self.write_doc(self.doc([self.hit(A, 'cancelled')])))
        self.assertEqual(self.disk(), {})

    def test_two_open_matches_or_incomplete_search_are_refused(self):
        self.state.write_bytes(b'')
        for doc in (self.doc([self.hit(A), self.hit(B)]), self.doc([self.hit(A)], complete=False),
                    self.doc([self.hit(A)], title_search='other'), self.doc([A]), self.doc([{'id': A}])):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                r.recover_state(self.write_doc(doc))
        self.assertEqual(self.state.read_bytes(), b'')


class LiveWork(Base):
    def setUp(self):
        super().setUp()
        for name, value in (('ACTIONS_LOG', self.dir / 'actions.log'), ('LEDGER', self.dir / 'ledger.jsonl')):
            patch = mock.patch.object(r, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.state.write_text(json.dumps({'anchor_id': A}))
        self.sql.side_effect = self.fake_sql

    @staticmethod
    def failed_runs():
        created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
        return [{'id': f'run-{n}', 'status': 'failed', 'created_at': created, 'error_code': 'adapter_failed',
                 'error': 'Adapter exited 1'} for n in (1, 2, 3)]

    def fake_sql(self, query):
        if 'select status from issues' in query:
            return [{'status': 'backlog'}]
        if "select distinct r.context_snapshot->>'issueId'" in query:
            return [{'issue_id': B}]
        if 'select id, status, created_at, error_code' in query:
            return self.failed_runs()
        if 'select i.id, i.identifier' in query:
            return [{'id': B, 'identifier': 'case_8c5fe4cc', 'status': 'in_progress', 'title': 'fixture', 'blockers': []}]
        if 'select coalesce(description' in query:
            return [{'d': 'card body'}]
        return []

    def test_live_work_on_the_card_skips_the_park(self):
        with mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=True) as live, \
                contextlib.redirect_stdout(io.StringIO()):
            report = r.run()
        live.assert_called_once_with(B)
        self.assertEqual([(item['issue'], item['skipped']) for item in report['loops']], [('case_8c5fe4cc', 'live_work')])
        self.api.assert_not_called()

    def test_no_live_work_parks_the_card_behind_the_anchor(self):
        self.api.side_effect = lambda *_args, **_kwargs: ('200', None)
        with mock.patch.object(r.live_run_guard, 'issue_has_live_work', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            report = r.run()
        self.assertEqual([item.get('skipped') for item in report['loops']], [None])
        method, path, body = self.api.call_args.args
        self.assertEqual((method, path), ('PATCH', f'/issues/{B}'))
        self.assertEqual((body['status'], body['blockedByIssueIds']), ('blocked', [A]))


class GuardContract(unittest.TestCase):
    def test_the_reaper_uses_the_repository_guard_not_a_stub(self):
        self.assertEqual(Path(r.live_run_guard.__file__).resolve(),
                         (Path(__file__).resolve().parents[1] / 'lib' / 'live_run_guard.py').resolve())

    @staticmethod
    def answer(issue_id=B, **behaviour):
        with mock.patch.object(g.subprocess, 'run', **behaviour) as run:
            return g.issue_has_live_work(issue_id), run

    def test_psql_answers_decide_the_guard(self):
        for stdout, live in (('f\n', False), ('t\n', True)):
            with self.subTest(stdout=stdout):
                done = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr='')
                self.assertIs(self.answer(return_value=done)[0], live)

    def test_any_failure_to_answer_counts_as_live_work(self):
        failures = (
            {'return_value': subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='refused')},
            {'side_effect': FileNotFoundError('docker')},
            {'side_effect': subprocess.TimeoutExpired(cmd='docker', timeout=30)},
        )
        for behaviour in failures:
            with self.subTest(behaviour=behaviour):
                self.assertTrue(self.answer(**behaviour)[0])

    def test_malformed_issue_id_is_live_without_a_query(self):
        for bad in ("x'; drop table issues; --", f'{B}\n', 'a' * 36):
            with self.subTest(bad=bad):
                answer, run = self.answer(issue_id=bad)
                self.assertTrue(answer)
                run.assert_not_called()

    def test_queued_running_and_scheduled_retry_runs_count_whatever_the_clock(self):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout='f\n', stderr='')
        _, run = self.answer(return_value=done)
        query = run.call_args.args[0][-1]
        for status in ('running', 'queued', 'scheduled_retry'):
            with self.subTest(status=status):
                self.assertIn(f"'{status}'", query)
        self.assertNotIn('now()', query)

    def test_the_query_matches_the_card_by_issue_id_or_task_id_and_a_live_status(self):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout='f\n', stderr='')
        _, run = self.answer(return_value=done)
        query = run.call_args.args[0][-1]
        self.assertIn(f"where coalesce(h.context_snapshot->>'issueId', h.context_snapshot->>'taskId')='{B}' "
                      "and h.status in ('queued','running','scheduled_retry'))", query)


if __name__ == '__main__':
    unittest.main()
