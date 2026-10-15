#!/usr/bin/env python3
"""Offline custody tests for the non-git quarantine guard.

No Docker, no purge/move, no real restore, no agent wake, no provider
mutation, no production access: DB reads are stubs keyed on the generated
SQL, and the lock holder script runs against a temp directory with the
docker prefix stripped. The suite drives workspace_live, the lock
acquire/release, quarantine_one ordering and main()'s hold gate, so
removing the lock, the recheck-under-lock or a custody query fails here.
"""
import datetime as dt
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

import nongit_repo_quarantine as q

NOW = dt.datetime(2026, 10, 15, 4, 0, tzinfo=dt.timezone.utc)
NOW_EPOCH = NOW.timestamp()

WS_PARENT = "/paperclip/instances/default/worktrees/example-parent-card"
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
                      "context_snapshot->>'nativeIssueId'",
                      "native_issue_id",
                      "'queued'", "'running'", "'scheduled_retry'",
                      "scheduled_retry_at > now()"):
            self.assertIn(token, sql)

    def test_empty_issue_list_drops_issue_predicates(self):
        # Regression: a sentinel in ('') matched every live run whose
        # coalesced key was empty, over-blocking orphaned workspaces.
        sql = q.live_runs_sql(["ws-1"], [])
        self.assertNotIn("in ('')", sql)
        self.assertNotIn("context_snapshot->>'issueId'", sql)
        self.assertNotIn("context_snapshot->>'taskId'", sql)
        self.assertNotIn("native_issue_id", sql.split("heartbeat_runs")[1])
        self.assertIn("context_snapshot->>'executionWorkspaceId'", sql)
        self.assertIn("'ws-1'", sql)

    def test_no_ids_at_all_yields_false_identity(self):
        sql = q.live_runs_sql([], [])
        self.assertIn("and (false)", sql)
        self.assertNotIn("in ('')", sql)

    def test_lease_sql_empty_workspace_is_false(self):
        self.assertIn("where false", q.lease_check_sql([]))

    def test_workspace_lookup_sql_escapes_quotes(self):
        sql = q.workspace_lookup_sql("/w/o'clock")
        self.assertIn("/w/o''clock", sql)
        self.assertNotIn("/w/o'clock", sql.replace("''", ""))

    def test_empty_identity_fails_closed(self):
        self.assertTrue(q.identity_is_ambiguous([]))
        self.assertTrue(q.identity_is_ambiguous(None))


class StubPsql:
    """psql_atc stub: answers by SQL substring, records every query."""

    def __init__(self, ws_rows="ws-1|/root||issue-sib|open\n",
                 issue_rows="issue-sib\n", run_count="0\n",
                 lease_count="0\n", fail_on=None):
        self.ws_rows = ws_rows
        self.issue_rows = issue_rows
        self.run_count = run_count
        self.lease_count = lease_count
        self.fail_on = fail_on
        self.queries = []

    def __call__(self, sql):
        self.queries.append(sql)
        if self.fail_on and self.fail_on in sql:
            return (False, "")
        if "from execution_workspaces" in sql:
            return (True, self.ws_rows)
        if "from issues" in sql:
            return (True, self.issue_rows)
        if "from heartbeat_runs" in sql:
            return (True, self.run_count)
        if "execution_workspace_runtime_leases" in sql:
            return (True, self.lease_count)
        return (False, "")


class WorkspaceLive(unittest.TestCase):
    def run_live(self, stub):
        original = q.psql_atc
        q.psql_atc = stub
        try:
            return q.workspace_live(WS_PARENT, None)
        finally:
            q.psql_atc = original

    def test_sibling_run_on_shared_workspace_is_live(self):
        # Parent idle, sibling running: the workspace join finds count 1.
        self.assertTrue(self.run_live(StubPsql(run_count="1\n")))

    def test_no_live_runs_and_no_leases_is_clear(self):
        self.assertFalse(self.run_live(StubPsql()))

    def test_db_failure_fails_closed(self):
        self.assertTrue(self.run_live(
            StubPsql(fail_on="from execution_workspaces")))
        self.assertTrue(self.run_live(
            StubPsql(fail_on="from heartbeat_runs")))

    def test_missing_workspace_rows_fail_closed(self):
        self.assertTrue(self.run_live(StubPsql(ws_rows="")))

    def test_unexpired_lease_is_live(self):
        self.assertTrue(self.run_live(StubPsql(lease_count="2\n")))

    def test_unrelated_run_does_not_block_orphaned_workspace(self):
        # No linked issues: the generated run query must carry no issue
        # predicates, so a live run for another issue cannot match.
        stub = StubPsql(ws_rows="ws-9|/root|| |open\n", issue_rows="")
        self.assertFalse(self.run_live(stub))
        run_sql = [s for s in stub.queries if "heartbeat_runs" in s][0]
        self.assertNotIn("in ('')", run_sql)
        self.assertNotIn("context_snapshot->>'issueId'", run_sql)
        self.assertIn("'ws-9'", run_sql)


def _local(cmd):
    """Strip the docker-exec prefix so a command runs against temp paths."""
    return cmd[cmd.index("sh"):]


def writer_sees_stale(owner_path):
    """The restore writer's staleness rule: dead pid means stale."""
    with open(owner_path) as handle:
        owner = json.load(handle)
    try:
        os.kill(int(owner["pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return False


class WriterLockScript(unittest.TestCase):
    def test_holder_pid_stays_alive_so_lock_is_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "deadbeef.lock")
            proc = subprocess.Popen(
                ["sh", "-c", q.build_lock_holder_script(), "_", lock, "tok-1"],
                stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(),
                                 q.LOCK_READY_MARKER)
                self.assertFalse(writer_sees_stale(lock + "/owner.json"))
                with open(lock + "/" + q.LOCK_TOKEN_FILENAME) as handle:
                    self.assertEqual(handle.read(), "tok-1")
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                proc.stdout.close()
            self.assertTrue(writer_sees_stale(lock + "/owner.json"))

    def test_holder_script_refuses_when_lock_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "held.lock")
            os.makedirs(lock)
            out = subprocess.run(
                ["sh", "-c", q.build_lock_holder_script(), "_", lock, "tok-2"],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(out.returncode, 11)


class WriterLockLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._instance_root = q.INSTANCE_ROOT
        q.INSTANCE_ROOT = self.tmp.name
        self.addCleanup(setattr, q, "INSTANCE_ROOT", self._instance_root)

    @staticmethod
    def local_popen(cmd, **kw):
        kw.pop("stderr", None)
        return subprocess.Popen(_local(cmd), **kw)

    @staticmethod
    def local_runner(cmd, **kw):
        return subprocess.run(_local(cmd), capture_output=True, text=True)

    def test_acquire_then_release_removes_own_lock(self):
        handle = q.acquire_restore_lock(WS_PARENT, popen=self.local_popen)
        self.assertIsNotNone(handle)
        self.assertTrue(os.path.isdir(handle.lock_path))
        self.assertFalse(writer_sees_stale(handle.lock_path + "/owner.json"))
        # A second acquire on the same workspace is refused.
        self.assertIsNone(
            q.acquire_restore_lock(WS_PARENT, popen=self.local_popen))
        q.release_restore_lock(handle, runner=self.local_runner)
        self.assertFalse(os.path.exists(handle.lock_path))
        self.assertIsNotNone(handle.process.poll())

    def test_release_never_removes_a_reclaimed_lock(self):
        handle = q.acquire_restore_lock(WS_PARENT, popen=self.local_popen)
        self.assertIsNotNone(handle)
        # Simulate the writer reclaiming: its lock has no token of ours.
        with open(handle.lock_path + "/" + q.LOCK_TOKEN_FILENAME, "w") as fh:
            fh.write("someone-else")
        q.release_restore_lock(handle, runner=self.local_runner)
        self.assertTrue(os.path.isdir(handle.lock_path))
        self.assertIsNotNone(handle.process.poll())


class RealpathResolution(unittest.TestCase):
    class _Out:
        def __init__(self, rc, stdout):
            self.returncode = rc
            self.stdout = stdout

    def test_resolved_path_is_returned(self):
        out = q.resolve_workspace_realpath(
            "/link/root",
            runner=lambda cmd, **kw: self._Out(0, "/real/root\n"))
        self.assertEqual(out, "/real/root")

    def test_failure_blocks_with_none(self):
        self.assertIsNone(q.resolve_workspace_realpath(
            "/link/root", runner=lambda cmd, **kw: self._Out(1, "")))
        self.assertIsNone(q.resolve_workspace_realpath(
            "/link/root", runner=lambda cmd, **kw: self._Out(0, "relative\n")))


class QuarantineOneOrder(unittest.TestCase):
    def make_deps(self, live_seq=(False, False), canonical="/real/root",
                  handle=object(), still=True, move=(True, "")):
        calls = []
        state = {"live_i": 0}

        def live_check(root, fallback):
            calls.append("live")
            value = live_seq[min(state["live_i"], len(live_seq) - 1)]
            state["live_i"] += 1
            if isinstance(value, Exception):
                raise value
            return value

        def resolve(root):
            calls.append("resolve")
            return canonical

        def acquire(root):
            calls.append("acquire:" + root)
            return handle

        def release(h):
            calls.append("release")
            self.assertIs(h, handle)

        def still_fn(d):
            calls.append("still")
            return still

        def move_fn(d, dest):
            calls.append("move")
            return move

        deps = dict(live_check=live_check, resolve=resolve, acquire=acquire,
                    release=release, still=still_fn, move=move_fn)
        return calls, deps

    def run_one(self, deps, apply_move=True):
        return q.quarantine_one(NESTED, WS_PARENT, "CARD-1",
                                "/quarantine/dest", apply_move, **deps)

    def test_clean_path_order_is_acquire_recheck_move_release(self):
        calls, deps = self.make_deps()
        outcome, _ = self.run_one(deps)
        self.assertEqual(outcome, "moved")
        self.assertEqual(
            calls,
            ["live", "resolve", "acquire:/real/root", "live", "still",
             "move", "release"])

    def test_live_before_lock_skips_everything(self):
        calls, deps = self.make_deps(live_seq=(True,))
        outcome, _ = self.run_one(deps)
        self.assertEqual(outcome, "skip")
        self.assertEqual(calls, ["live"])

    def test_custody_exception_fails_closed(self):
        calls, deps = self.make_deps(live_seq=(RuntimeError("db down"),))
        outcome, detail = self.run_one(deps)
        self.assertEqual(outcome, "skip")
        self.assertIn("fail closed", detail)
        self.assertEqual(calls, ["live"])

    def test_dry_run_never_touches_lock_or_move(self):
        calls, deps = self.make_deps()
        outcome, _ = self.run_one(deps, apply_move=False)
        self.assertEqual(outcome, "would-move")
        self.assertEqual(calls, ["live"])

    def test_unresolved_canonical_path_blocks_before_lock(self):
        calls, deps = self.make_deps(canonical=None)
        outcome, detail = self.run_one(deps)
        self.assertEqual(outcome, "skip")
        self.assertIn("unresolved", detail)
        self.assertEqual(calls, ["live", "resolve"])

    def test_lock_contention_blocks_without_recheck(self):
        calls, deps = self.make_deps(handle=None)
        outcome, detail = self.run_one(deps)
        self.assertEqual(outcome, "skip")
        self.assertIn("lock held", detail)
        self.assertEqual(calls, ["live", "resolve", "acquire:/real/root"])

    def test_live_under_lock_blocks_move_but_releases(self):
        calls, deps = self.make_deps(live_seq=(False, True))
        outcome, detail = self.run_one(deps)
        self.assertEqual(outcome, "skip")
        self.assertIn("under-lock", detail)
        self.assertNotIn("move", calls)
        self.assertEqual(calls[-1], "release")

    def test_path_changed_under_lock_blocks_move(self):
        calls, deps = self.make_deps(still=False)
        outcome, _ = self.run_one(deps)
        self.assertEqual(outcome, "skip")
        self.assertNotIn("move", calls)
        self.assertEqual(calls[-1], "release")


class MainHoldGate(unittest.TestCase):
    def test_active_hold_returns_before_any_container_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            hold = os.path.join(tmp, q.HOLD_FILENAME)
            with open(hold, "w") as fh:
                json.dump({"owner": "x", "expiry": "2099-01-01T00:00:00Z"}, fh)
            calls = []
            original_path, original_sh = q.HOLD_PATH, q.sh
            q.HOLD_PATH = hold
            q.sh = lambda *a, **k: calls.append(a)
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    q.main()
            finally:
                q.HOLD_PATH, q.sh = original_path, original_sh
            self.assertEqual(calls, [])
            self.assertIn("hold active", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
