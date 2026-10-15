#!/usr/bin/env python3
"""Offline custody tests for the non-git quarantine guard.

No Docker, no purge/move, no real restore, no agent wake, no provider
mutation, no production access: every DB, lock and filesystem read is a
stub. The suite proves the guard fails closed and coordinates with the
SSH restore writer lock.
"""
import datetime as dt
import hashlib
import unittest

import nongit_repo_quarantine as q

NOW = dt.datetime(2026, 10, 15, 4, 0, tzinfo=dt.timezone.utc)
NOW_EPOCH = NOW.timestamp()

WS_PARENT = "/paperclip/instances/default/worktrees/example-parent-card"
WS_CHILD_SHARED = WS_PARENT  # sibling cards share one execution workspace
NESTED = WS_PARENT + "/.paperclip-repositories/example-repo-aaaabbbbcccc"


def hold_doc(expiry):
    return {"owner": "example-operator", "expiry": expiry}


class HoldGate(unittest.TestCase):
    def test_missing_hold_is_inactive(self):
        active, _ = q.hold_status(None, NOW_EPOCH)
        self.assertFalse(active)

    def test_unexpired_hold_is_active(self):
        active, why = q.hold_status(
            hold_doc("2026-10-15T05:24:40Z"), NOW_EPOCH)
        self.assertTrue(active)
        self.assertIn("hold", why)

    def test_expired_hold_is_inactive(self):
        active, _ = q.hold_status(
            hold_doc("2026-10-15T03:00:00Z"), NOW_EPOCH)
        self.assertFalse(active)

    def test_malformed_hold_fails_closed_active(self):
        for bad in ({"owner": "x"}, {"expiry": "not-a-time"},
                    {"expiry": ""}, {"expiry": None}, ["list"],
                    "string", 42):
            with self.subTest(bad=bad):
                active, _ = q.hold_status(bad, NOW_EPOCH)
                self.assertTrue(active)

    def test_hold_expiry_keys_are_equivalent(self):
        for doc in ({"owner": "x", "expiry": "2026-10-15T05:24:40Z"},
                    {"owner": "x", "expiresAt": "2026-10-15T05:24:40Z"},
                    {"owner": "x", "expires_at": "2026-10-15T05:24:40Z"}):
            with self.subTest(doc=doc):
                active, _ = q.hold_status(doc, NOW_EPOCH)
                self.assertTrue(active)


class WorkspaceIdentity(unittest.TestCase):
    def test_nested_dir_resolves_to_worktree_root(self):
        self.assertEqual(q.workspace_root_for_nested(NESTED), WS_PARENT)

    def test_project_worktree_nested_resolves(self):
        nested = ("/paperclip/instances/default/projects/example-co/example-proj"
                  "/.paperclip/worktrees/example-wt/.paperclip-repositories/example-repo-dddd")
        root = q.workspace_root_for_nested(nested)
        self.assertTrue(root.endswith("/example-wt"))
        self.assertNotIn(".paperclip-repositories", root)

    def test_path_without_marker_is_ambiguous(self):
        self.assertIsNone(q.workspace_root_for_nested("/tmp/example-no-marker"))

    def test_lock_path_uses_sha256_of_canonical_root(self):
        lock, digest = q.lock_path_for_workspace(WS_PARENT)
        expected = hashlib.sha256(WS_PARENT.encode()).hexdigest()
        self.assertEqual(digest, expected)
        self.assertTrue(lock.endswith("/" + expected + ".lock"))
        self.assertIn("directory-merge", lock)

    def test_lock_path_differs_per_workspace(self):
        a, _ = q.lock_path_for_workspace(WS_PARENT)
        b, _ = q.lock_path_for_workspace(WS_PARENT + "-sibling")
        self.assertNotEqual(a, b)


class CustodySql(unittest.TestCase):
    def test_live_sql_covers_issue_task_workspace_and_retry(self):
        sql = q.live_runs_sql(["ws-1"], ["issue-1"])
        for token in ("context_snapshot->>'issueId'",
                      "context_snapshot->>'taskId'",
                      "context_snapshot->>'executionWorkspaceId'",
                      "native_issue_id",
                      "'queued'", "'running'", "'scheduled_retry'"):
            self.assertIn(token, sql)

    def test_workspace_lookup_sql_escapes_quotes(self):
        sql = q.workspace_lookup_sql("/w/o'clock")
        self.assertIn("/w/o''clock", sql)
        self.assertNotIn("/w/o'clock", sql.replace("''", ""))

    def test_empty_identity_fails_closed(self):
        self.assertTrue(q.identity_is_ambiguous([]))
        self.assertTrue(q.identity_is_ambiguous(None))


class SiblingRace(unittest.TestCase):
    def test_parent_idle_child_running_blocks_move(self):
        # Same workspace, parent has no live run, sibling child does.
        decision = q.decide(
            live_before_lock=True,  # child running found via workspace join
            lock_acquired=True,
            live_under_lock=True,
        )
        self.assertFalse(decision.proceed)
        self.assertIn("live", decision.reason)

    def test_shared_sibling_workspace_is_checked_not_parent_name(self):
        # The old parent-name-only check would miss this; the custody
        # decision must block on workspace-linked liveness.
        linked = {"parent-issue": False, "sibling-issue": True}
        self.assertTrue(q.any_linked_live(linked))

    def test_native_and_task_keys_count_as_live(self):
        for row in ({"issueId": "x"}, {"taskId": "x"},
                    {"nativeIssueId": "x"}, {"native_issue_id": "x"}):
            with self.subTest(row=row):
                self.assertTrue(q.row_matches_linked(row, {"x"}))

    def test_queued_and_retry_count_as_live(self):
        for status in ("queued", "running", "scheduled_retry"):
            with self.subTest(status=status):
                self.assertTrue(q.run_status_is_live(status, None))
        # A finished row with a future retry is still live.
        future = "2099-01-01T00:00:00Z"
        self.assertTrue(q.run_status_is_live("succeeded", future))
        self.assertFalse(q.run_status_is_live("succeeded", None))

    def test_db_failure_blocks_move(self):
        decision = q.decide(
            live_before_lock=True,  # fail-closed maps DB error to live
            lock_acquired=False,
            live_under_lock=True,
        )
        self.assertFalse(decision.proceed)


class WriterLock(unittest.TestCase):
    def test_lock_contention_blocks_move(self):
        decision = q.decide(
            live_before_lock=False,
            lock_acquired=False,
            live_under_lock=False,
        )
        self.assertFalse(decision.proceed)
        self.assertIn("lock", decision.reason)

    def test_live_under_lock_blocks_despite_clean_precheck(self):
        # Wider pre-move SELECT alone does not close the race.
        decision = q.decide(
            live_before_lock=False,
            lock_acquired=True,
            live_under_lock=True,
        )
        self.assertFalse(decision.proceed)

    def test_clean_precheck_plus_lock_plus_clean_recheck_proceeds(self):
        decision = q.decide(
            live_before_lock=False,
            lock_acquired=True,
            live_under_lock=False,
        )
        self.assertTrue(decision.proceed)


if __name__ == "__main__":
    unittest.main()
