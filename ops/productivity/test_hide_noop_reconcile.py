#!/usr/bin/env python3
"""case_6a189da9: offline safety tests for noop-comment hiding and replay-blocked reconciliation.

hide_noop_comments.py tombstones board comments and reconcile_replay_blocked.py
releases frozen cards; both mutate the board, so this suite pins their safety
gates with stubbed psql/API: no DB, no network, no credentials.

Hide side: ONLY Paperclip-made no-op run notes qualify (server-created from a
run's final message, no-event wake, NOOP body, >=2min old). User-authored and
agent-written substantive content is never touched; the filter lives in SQL,
so the suite pins each predicate in the issued SQL text plus the WAKES/NOOP
constants, and proves dry-run issues zero writes and timeouts fail loudly.

Reconcile side: ONLY genuinely frozen rows (evidence.automaticRecovery.replay
='blocked') on eligible cards are released; healthy/moving cards (a running or
queued run exists) never appear, parked projects and held cards are excluded,
and wakes go only to todo/in_progress cards with an assignee. A timed-out wake
is recorded as a failure and the row is left 'blocked' for retry, never
counted as woken.
"""
import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hide_noop_comments as hn
import reconcile_replay_blocked as rrb


def done_process(stdout=''):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr='')


# Paperclip-made no-op run notes: must match the ~* NOOP filter.
NOOP_BODIES = [
    'no-op: nothing new since last check',
    'Nothing new on this card',
    'no new activity observed',
    'duplicate wake, handled quietly',
    'no action taken this run',
    'no changes made',
    'no change needed',
    'no comment posted; nothing actionable',
    'Nothing actionable at this time',
]

# Human/agent substantive content: must NEVER match, and would never satisfy
# the server-created predicates either.
HUMAN_BODIES = [
    'Fixed the bug, see PR #123 for details',
    'Please review the deploy plan before Friday',
    'Deployed to staging, smoke tests pass',
    'I disagree with the approach; see my comment above',
    'noop',  # bare word without a nothing-happened claim is not in the pattern
    '',
]


def hide_row(cid='00000000-0000-4000-8000-000000000054', ident='case_63ac0cf0'):
    return json.dumps({'id': cid, 'issue': ident, 'body': NOOP_BODIES[0],
                       'presentation': None, 'metadata': None,
                       'run': 'r1', 'wake': 'issue_monitor_due', 'at': '2026-10-12T00:00:00Z'})


def replay_row(rid='ra-1', iid='i' + '0' * 35, ident='case_63ac0cf0',
               status='todo', aid='agent-1'):
    return '\t'.join([rid, iid, ident, status, aid])


class HideNoopSqlContract(unittest.TestCase):
    """Every safety predicate lives in the SELECT; pin each one."""

    def issued_sql(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(hn, 'STATE_DIR', tmp), \
                    mock.patch.object(hn, 'psql', side_effect=lambda sql: seen.append(sql) or ''), \
                    mock.patch.object(hn.subprocess, 'run',
                                      side_effect=AssertionError('no subprocess in dry run')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hn.main([]), 0)
        self.assertEqual(len(seen), 1)
        return ' '.join(seen[0].split())

    def test_user_authored_comments_excluded_require_agent_author(self):
        sql = self.issued_sql()
        self.assertIn('and c.author_agent_id is not null', sql)

    def test_only_server_created_run_notes_require_presentation_decision(self):
        sql = self.issued_sql()
        self.assertIn("and r.result_json->'presentationDecision'->>'commentId'=c.id::text", sql)
        self.assertIn("and r.result_json->'presentationDecision'->>'commentAction'='create'", sql)

    def test_only_no_event_wakes_require_wake_reason(self):
        sql = self.issued_sql()
        wakes = ','.join("'" + w + "'" for w in hn.WAKES)
        self.assertIn(f"and r.context_snapshot->>'wakeReason' in ({wakes})", sql)
        for wake in hn.WAKES:
            self.assertIn(wake, sql)

    def test_only_noop_bodies_require_noop_regex(self):
        sql = self.issued_sql()
        self.assertIn('and c.body ~*', sql)
        for token in ('no-op', 'nothing new', 'duplicate wake', 'no action taken'):
            self.assertIn(token, sql)

    def test_fresh_and_deleted_comments_excluded(self):
        sql = self.issued_sql()
        self.assertIn('and c.deleted_at is null', sql)
        self.assertIn("c.created_at < now()-interval '2 minutes'", sql)
        self.assertIn("c.created_at > now()-interval", sql)

    def test_company_scoped(self):
        self.assertIn(f"c.company_id='{hn.CID}'", self.issued_sql())

    def test_where_clause_is_pure_conjunction(self):
        where = self.issued_sql().split(' where ', 1)[1]
        self.assertNotRegex(where, r'\bor\b')

    def test_wake_allowlist_is_no_event_only(self):
        for wake in hn.WAKES:
            self.assertIn(wake, ("issue_continuation_needed", "issue_monitor_due",
                                 "dispatch_stalled_issue", "issue_assignment_recovery",
                                 "issue_graph_liveness_backstop", "replay_block_reconciled"))

    def test_noop_regex_matches_run_notes_not_human_content(self):
        for body in NOOP_BODIES:
            with self.subTest(body=body[:40]):
                self.assertIsNotNone(re.search(hn.NOOP, body, re.IGNORECASE), body)
        for body in HUMAN_BODIES:
            with self.subTest(body=body[:40]):
                self.assertIsNone(re.search(hn.NOOP, body, re.IGNORECASE), body)


class HideNoopSafety(unittest.TestCase):
    def test_pause_file_returns_zero_with_no_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'PAUSE').write_text('paused')
            run = mock.Mock(side_effect=AssertionError('must not touch the db while paused'))
            with mock.patch.object(hn, 'STATE_DIR', tmp), \
                    mock.patch.object(hn, 'psql', run), \
                    mock.patch.object(hn.subprocess, 'run', run), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hn.main([]), 0)
        run.assert_not_called()

    def test_dry_run_is_default_and_issues_zero_writes(self):
        self.assertNotIn('--apply', __import__('sys').argv)
        run = mock.Mock(side_effect=AssertionError('dry run must not run subprocesses'))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            backup = str(Path(tmp) / 'hidden.jsonl')
            with mock.patch.object(hn, 'STATE_DIR', tmp), \
                    mock.patch.object(hn, 'BACKUP', backup), \
                    mock.patch.object(hn, 'psql', return_value=hide_row() + '\n'), \
                    mock.patch.object(hn.subprocess, 'run', run), \
                    mock.patch('os.open', side_effect=AssertionError('must not open backup')), \
                    mock.patch('builtins.open', side_effect=AssertionError('must not open log')), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(hn.main([]), 0)
            self.assertFalse(Path(backup).exists())
        run.assert_not_called()
        self.assertIn('1 Paperclip-made no-op comment(s)', out.getvalue())
        self.assertNotIn('hid 1', out.getvalue())

    def test_psql_timeout_fails_loud_with_nothing_hidden(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(hn, 'STATE_DIR', tmp), \
                    mock.patch.object(hn.subprocess, 'run',
                                      side_effect=subprocess.TimeoutExpired('docker', hn.PSQL_TIMEOUT)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    hn.main([])
        self.assertNotEqual(cm.exception.code, 0)
        combined = out.getvalue() + str(cm.exception)
        self.assertIn('timed out', combined)
        self.assertNotIn('hid ', out.getvalue())

    def test_psql_call_is_bounded(self):
        run = mock.Mock(return_value=done_process(''))
        with mock.patch.object(hn.subprocess, 'run', run):
            hn.psql('select 1')
        self.assertEqual(run.call_args.kwargs.get('timeout'), hn.PSQL_TIMEOUT)

    def test_apply_tombstones_only_returned_ids_with_backup(self):
        good = '00000000-0000-4000-8000-000000000054'
        row = hide_row(cid=good)
        updates = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ops' / 'worker-vps').mkdir(parents=True)
            backup = str(root / 'hidden.jsonl')

            def fake_psql(sql):
                if 'select json_build_object' in sql:
                    return row + '\n'
                if 'responsible_user_id' in sql:
                    return 'owner-1\n'
                updates.append(sql)
                return ''

            out = io.StringIO()
            with mock.patch.object(hn, 'STATE_DIR', tmp), \
                    mock.patch.object(hn, 'BACKUP', backup), \
                    mock.patch.object(hn, 'ROOT', str(root)), \
                    mock.patch.object(hn, 'psql', side_effect=fake_psql), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(hn.main(['--apply']), 0)
            backed = [json.loads(line) for line in Path(backup).read_text().splitlines()]
            self.assertEqual([r['id'] for r in backed], [good])
            log = (root / 'ops' / 'worker-vps' / 'operator-actions.log').read_text()
            self.assertIn('tombstoned 1', log)
        self.assertEqual(len(updates), 1)
        self.assertIn(good, updates[0])
        self.assertIn('deleted_at=now()', updates[0].replace(' ', ''))
        self.assertIn('and deleted_at is null', updates[0])
        self.assertIn('hid 1', out.getvalue())


class ReconcileSqlContract(unittest.TestCase):
    """Only genuinely frozen rows on eligible cards may be selected."""

    def issued_sql(self):
        seen = []
        with mock.patch.object(rrb, 'psql', side_effect=lambda sql: seen.append(sql) or ''):
            rrb.main([])
        self.assertEqual(len(seen), 1)
        return ' '.join(seen[0].split())

    def test_only_replay_blocked_rows_selected(self):
        sql = self.issued_sql()
        self.assertIn("and ra.evidence->'automaticRecovery'->>'replay'='blocked'", sql)

    def test_only_unparked_eligible_statuses_selected(self):
        sql = self.issued_sql()
        self.assertIn("i.status in ('todo','in_progress','in_review','blocked')", sql)

    def test_parked_projects_and_held_cards_excluded(self):
        sql = self.issued_sql()
        parked = ','.join("'" + p.replace("'", "''") + "'" for p in rrb.PARKED)
        held = ','.join(f"'{h}'" for h in rrb.HELD)
        self.assertIn(f"coalesce(p.name,'') not in ({parked})", sql)
        self.assertIn(f"i.identifier not in ({held})", sql)

    def test_parked_project_and_held_card_exclusions_are_pinned(self):
        self.assertEqual(set(rrb.PARKED), {
            'TWO Community & Growth', 'Fleet Intelligence & Evaluation', 'Here.Now Clone',
            'Host AI Bridge (MCP)', 'Kofra', 'NNTune', 'Onboarding', 'Prism Gateway',
            'Routeware Shadow API', 'TWO Web', 'Wayselect', 'cpa-plugin-zai-coding-plan',
        })
        self.assertEqual(set(rrb.HELD), {'case_6ceef68e', 'case_fa4fcb64', 'case_b8566208'})

    def test_healthy_and_moving_cards_excluded(self):
        sql = self.issued_sql()
        self.assertIn("and not exists (select 1 from heartbeat_runs r where "
                      "r.context_snapshot->>'issueId'=i.id::text "
                      "and r.status in ('running','queued'))", sql)

    def test_company_scoped(self):
        self.assertIn(f"where i.company_id='{rrb.CID}'", self.issued_sql())

    def test_where_clause_is_pure_conjunction(self):
        where = self.issued_sql().split(' where ', 1)[1]
        self.assertNotRegex(where, r'\bor\b')


class ReconcileSafety(unittest.TestCase):
    def run_apply(self, rows, wake_results=None, wake_n=10):
        """Run --apply with stubbed psql/wake; returns SQL log, wake calls, tmp root."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'ops' / 'productivity').mkdir(parents=True)
        (root / 'ops' / 'worker-vps').mkdir(parents=True)
        sqls = []
        tsv = '\n'.join(rows)

        def fake_psql(sql):
            sqls.append(sql)
            if 'select ra.id' in sql:
                return tsv
            if 'json_agg' in sql:
                return '[]'
            return ''

        wakes = []

        def fake_wake(aid, iid, ident):
            wakes.append((aid, iid, ident))
            if wake_results is None:
                return '{}'
            return wake_results.pop(0)

        out = io.StringIO()
        with mock.patch.object(rrb, 'ROOT', str(root)), \
                mock.patch.object(rrb, 'psql', side_effect=fake_psql), \
                mock.patch.object(rrb, 'wake', side_effect=fake_wake), \
                contextlib.redirect_stdout(out):
            self.assertEqual(rrb.main(['--apply', '--wake', str(wake_n)]), 0)
        return {'sqls': sqls, 'wakes': wakes, 'root': root, 'stdout': out.getvalue()}

    def test_dry_run_issues_zero_writes(self):
        run = mock.Mock(side_effect=AssertionError('dry run must not run subprocesses'))
        out = io.StringIO()
        with mock.patch.object(rrb, 'psql', return_value=replay_row() + '\n'), \
                mock.patch.object(rrb.subprocess, 'run', run), \
                mock.patch('builtins.open', side_effect=AssertionError('must not open files')), \
                contextlib.redirect_stdout(out):
            self.assertEqual(rrb.main([]), 0)
        run.assert_not_called()
        self.assertIn('case_63ac0cf0 todo', out.getvalue())

    def test_wakes_only_todo_and_in_progress_with_assignee(self):
        rows = [replay_row('ra-1', 'i' + '1' * 35, 'case_8c5fe4cc', 'todo', 'agent-1'),
                replay_row('ra-2', 'i' + '2' * 35, 'case_d02d0252', 'in_progress', 'agent-2'),
                replay_row('ra-3', 'i' + '3' * 35, 'case_a6dbab15', 'in_review', 'agent-3'),
                replay_row('ra-4', 'i' + '4' * 35, 'case_4ac9f045', 'blocked', 'agent-4'),
                replay_row('ra-5', 'i' + '5' * 35, 'case_0dd2d987', 'todo', '')]
        got = self.run_apply(rows)
        woken = [ident for _, _, ident in got['wakes']]
        self.assertEqual(woken, ['case_8c5fe4cc', 'case_d02d0252'])
        reconcile = [s for s in got['sqls'] if 'operator_reconciled' in s]
        # Idle rows go out in one bulk release; each woken row is marked
        # just before its wake, so a kill strands at most one row.
        self.assertEqual(len(reconcile), 3)
        for rid in ('ra-1', 'ra-2', 'ra-3', 'ra-4', 'ra-5'):
            self.assertTrue(any(rid in s for s in reconcile), rid)
        self.assertNotIn('left blocked', ' '.join(got['sqls']))

    def test_wake_timeout_leaves_row_blocked_for_retry_never_counted(self):
        rows = [replay_row('ra-1', 'i' + '1' * 35, 'case_8c5fe4cc', 'todo', 'agent-1')]
        got = self.run_apply(rows, wake_results=[None])
        self.assertEqual(len(got['wakes']), 1)
        revert = [s for s in got['sqls'] if '"blocked"' in s and 'ra-1' in s]
        self.assertEqual(len(revert), 1)
        self.assertNotIn('operator_reconciled', revert[0])
        self.assertNotIn('case_8c5fe4cc {}', got['stdout'])
        self.assertNotIn('woke run', got['stdout'])
        log = (got['root'] / 'ops' / 'worker-vps' / 'operator-actions.log').read_text()
        self.assertIn('wake FAILED for 1 card(s) (case_8c5fe4cc)', log)

    def test_db_timeout_aborts_before_any_write(self):
        backups_before = 0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(rrb, 'ROOT', str(root)), \
                    mock.patch.object(rrb, 'psql',
                                      side_effect=subprocess.TimeoutExpired('docker', rrb.PSQL_TIMEOUT)), \
                    mock.patch.object(rrb, 'wake',
                                      side_effect=AssertionError('must not wake after a db timeout')):
                with self.assertRaises(subprocess.TimeoutExpired):
                    rrb.main(['--apply'])
            backups_before = len(list(root.rglob('replay-blocked-backup-*.json')))
        self.assertEqual(backups_before, 0)

    def test_timeouts_are_bounded(self):
        self.assertIsInstance(rrb.PSQL_TIMEOUT, int)
        self.assertIsInstance(rrb.CURL_TIMEOUT, int)
        self.assertGreater(rrb.PSQL_TIMEOUT, 0)
        self.assertGreater(rrb.CURL_TIMEOUT, 0)
        run = mock.Mock(return_value=done_process(''))
        with mock.patch.object(rrb.subprocess, 'run', run):
            rrb.psql('select 1')
        self.assertEqual(run.call_args.kwargs.get('timeout'), rrb.PSQL_TIMEOUT)
        run = mock.Mock(return_value=done_process('{}'))
        with mock.patch.object(rrb.subprocess, 'run', run):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(rrb, 'ROOT', tmp):
                    with contextlib.redirect_stdout(io.StringIO()):
                        # '{}' starts no run, so it reads as a failure, never a wake.
                        self.assertIsNone(rrb.wake('agent-1', 'i1', 'case_8c5fe4cc'))
        run = mock.Mock(return_value=done_process('{"id": "run-1", "status": "queued"}'))
        with mock.patch.object(rrb.subprocess, 'run', run):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(rrb, 'ROOT', tmp):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(rrb.wake('agent-1', 'i1', 'case_8c5fe4cc'), 'run-1')
        run = mock.Mock(return_value=done_process('{"status": "skipped"}'))
        with mock.patch.object(rrb.subprocess, 'run', run):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(rrb, 'ROOT', tmp):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertIsNone(rrb.wake('agent-1', 'i1', 'case_8c5fe4cc'))
        args, kwargs = run.call_args
        self.assertIn('--max-time', args[0])
        self.assertEqual(args[0][args[0].index('--max-time') + 1], '30')
        self.assertEqual(kwargs.get('timeout'), rrb.CURL_TIMEOUT)

    def test_wake_argv_carries_no_credential_material(self):
        run = mock.Mock(return_value=done_process('{}'))
        with mock.patch.object(rrb.subprocess, 'run', run):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(rrb, 'ROOT', tmp):
                    rrb.wake('agent-1', 'i1', 'case_8c5fe4cc')
        argv = run.call_args.args[0]
        blob = ' '.join(argv).lower()
        self.assertIn('-sk', blob)
        for secret_word in ('token', 'bearer', 'authorization', 'password', 'secret'):
            self.assertNotIn(secret_word, blob)


if __name__ == '__main__':
    unittest.main()
