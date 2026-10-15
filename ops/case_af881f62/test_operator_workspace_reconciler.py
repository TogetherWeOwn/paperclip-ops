#!/usr/bin/env python3
"""Hermetic tests for operator_workspace_reconciler.py (case_af881f62).

No network, no credentials, no board access: pure-function classification
plus a fake client for the apply/restore loops. Exit codes are pinned;
human-readable message text is not.
"""
import http.client
import json
import os
import tempfile
import unittest
from unittest import mock

import operator_workspace_reconciler as rec

OP = rec.OPERATOR_AGENT_ID
OTHER = "00000000-0000-4000-8000-000000000002"


def issue(identifier, status="blocked", pref="reuse_existing",
          ew="ew-1", pw="pw-1", settings=None, assignee=OP):
    row = {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "status": status,
        "title": f"Operator: {identifier}",
        "assigneeAgentId": assignee,
        "executionWorkspacePreference": pref,
        "executionWorkspaceId": ew,
        "projectWorkspaceId": pw,
        "executionWorkspaceSettings": settings,
    }
    return row


class ClassifyTest(unittest.TestCase):
    def test_blocked_with_workspace_is_candidate(self):
        verdict, _ = rec.classify(issue("case_8c5fe4cc"), OP)
        self.assertEqual(verdict, "candidate")

    def test_todo_and_backlog_are_candidates(self):
        for status in ("todo", "backlog"):
            verdict, _ = rec.classify(issue("case_8c5fe4cc", status=status), OP)
            self.assertEqual(verdict, "candidate")

    def test_null_preference_without_ids_still_needs_explicit_default(self):
        verdict, _ = rec.classify(
            issue("case_8c5fe4cc", pref=None, ew=None, pw=None), OP)
        self.assertEqual(verdict, "candidate")

    def test_already_agent_default_is_skipped(self):
        verdict, reason = rec.classify(
            issue("case_8c5fe4cc", pref="agent_default", ew=None, pw=None), OP)
        self.assertEqual((verdict, reason), ("skip", "already-agent-default"))

    def test_in_progress_is_never_a_candidate(self):
        verdict, reason = rec.classify(
            issue("case_8c5fe4cc", status="in_progress"), OP)
        self.assertEqual((verdict, reason), ("skip", "live-work-or-review-path"))

    def test_in_review_is_never_a_candidate(self):
        verdict, reason = rec.classify(
            issue("case_8c5fe4cc", status="in_review"), OP)
        self.assertEqual((verdict, reason), ("skip", "live-work-or-review-path"))

    def test_terminal_cards_are_skipped(self):
        for status in ("done", "cancelled"):
            verdict, reason = rec.classify(
                issue("case_8c5fe4cc", status=status), OP)
            self.assertEqual((verdict, reason), ("skip", "terminal"))

    def test_other_agents_cards_are_skipped(self):
        verdict, reason = rec.classify(issue("case_8c5fe4cc", assignee=OTHER), OP)
        self.assertEqual((verdict, reason), ("skip", "not-operator-assigned"))

    def test_settings_override_counts_as_workspace(self):
        verdict, _ = rec.classify(
            issue("case_8c5fe4cc", pref="agent_default", ew=None, pw=None,
                  settings={"mode": "isolated_workspace"}),
            OP,
        )
        self.assertEqual(verdict, "candidate")

    def test_garbage_row_is_skipped(self):
        self.assertEqual(rec.classify(None, OP)[0], "skip")


class RunLiveTest(unittest.TestCase):
    def test_running_without_finished_at_is_live(self):
        self.assertTrue(rec.run_is_live({"status": "running", "finishedAt": None}))

    def test_finished_row_is_not_live(self):
        self.assertFalse(rec.run_is_live(
            {"status": "running", "finishedAt": "2026-10-09T12:00:00Z"}))

    def test_terminal_status_without_finished_at_is_not_live(self):
        self.assertFalse(rec.run_is_live({"status": "failed", "finishedAt": None}))

    def test_garbage_row_is_not_live(self):
        self.assertFalse(rec.run_is_live(None))
        self.assertFalse(rec.run_is_live("running"))


class PatchShapeTest(unittest.TestCase):
    def test_flip_patch_detaches_all_workspace_refs(self):
        patch = rec.build_flip_patch()
        self.assertEqual(patch["executionWorkspacePreference"], "agent_default")
        self.assertIsNone(patch["executionWorkspaceId"])
        self.assertIsNone(patch["projectWorkspaceId"])
        self.assertIsNone(patch["executionWorkspaceSettings"])

    def test_readback_accepts_only_flipped_state(self):
        self.assertTrue(rec.readback_ok(
            issue("case_8c5fe4cc", pref="agent_default", ew=None, pw=None)))
        self.assertFalse(rec.readback_ok(issue("case_8c5fe4cc")))


class WakeDiffTest(unittest.TestCase):
    def test_no_new_wakes_is_empty(self):
        before = rec.wake_ids({"events": [{"id": "a"}]})
        after = rec.wake_ids({"events": [{"id": "a"}]})
        self.assertEqual(rec.detect_new_wakes(before, after), set())

    def test_new_wake_is_detected(self):
        before = rec.wake_ids({"events": [{"id": "a"}]})
        after = rec.wake_ids({"events": [{"id": "a"}, {"id": "b"}]})
        self.assertEqual(rec.detect_new_wakes(before, after), {"b"})

    def test_non_dict_diagnostics_is_empty(self):
        self.assertEqual(rec.wake_ids(None), set())
        self.assertEqual(rec.wake_ids({"events": None}), set())


class FakeClient:
    """Minimal stand-in: scripted issue rows, recorded PATCHes."""

    def __init__(self, rows, live=(), fail_patch=0, wakes_before=None,
                 wakes_after=None, minimal_echo=False, wake_on_patch=(),
                 fail_wake_calls=(), list_strip_fields=(), fail_ids=(),
                 fail_get_ids=(), raw_fail_get_ids=(),
                 commit_then_raise_ids=(), fail_get_calls=(),
                 reject_ids=(), delayed_commit_ids=()):
        self.rows = dict(rows)
        self.live = set(live)
        self.fail_patch = fail_patch
        self.fail_wake_calls = set(fail_wake_calls)
        # fail_get_calls fails the Nth GET call (1-based), e.g. {2} fails
        # the confirmatory re-read while the pre-PATCH read succeeds.
        self.fail_get_calls = set(fail_get_calls)
        # commit_then_raise_ids simulates a PATCH the server commits while
        # its response is lost: the row is updated, then the call raises.
        self.commit_then_raise_ids = set(commit_then_raise_ids)
        # reject_ids simulates a definite HTTP 4xx rejection: the server
        # refused, so nothing was committed and the re-read still shows
        # the old fields.
        self.reject_ids = set(reject_ids)
        # delayed_commit_ids simulates a 504/timeout that saves AFTER the
        # confirmatory re-read: the PATCH raises without updating, the
        # next GET still shows the old fields, then the server commits.
        self.delayed_commit_ids = set(delayed_commit_ids)
        self._delayed_pending = set()
        # raw_fail_get_ids simulates transport errors that escape the API
        # wrapper as raw exceptions (dropped connection, truncated body)
        # instead of ReconcileError.
        self.raw_fail_get_ids = set(raw_fail_get_ids)
        # list_strip_fields simulates the server nulling fields (notably
        # executionWorkspaceSettings) on the company issue-list endpoint
        # while the single-issue GET still returns the stored value.
        self.list_strip_fields = tuple(list_strip_fields)
        self.fail_ids = set(fail_ids)
        self.fail_get_ids = set(fail_get_ids)
        self.patches = []
        self.gets = 0
        self.minimal_echo = minimal_echo
        self.wake_on_patch = list(wake_on_patch)
        before = wakes_before if wakes_before is not None else {"events": []}
        self.wake_events = list(before.get("events", []))
        self.wakes_after = wakes_after
        self.wake_calls = 0

    def list_operator_issues(self, operator_agent_id, page_size=200,
                             max_pages=1000):
        listed = []
        for row in self.rows.values():
            copy = dict(row)
            for field in self.list_strip_fields:
                copy[field] = None
            listed.append(copy)
        return listed

    def get(self, path):
        self.gets += 1
        parts = path.split("/")
        if self.gets in self.fail_get_calls:
            raise rec.ReconcileError("HTTP 500: re-read boom")
        if parts[-1] in self.raw_fail_get_ids:
            raise ConnectionResetError("conn reset by peer")
        if parts[-1] in self.fail_get_ids:
            raise rec.ReconcileError("HTTP 500: get boom")
        row = dict(self.rows[parts[-1]])
        if parts[-1] in self._delayed_pending:
            # Re-read still shows the old fields; the server commits
            # right afterwards, so later reads see the flip.
            self._delayed_pending.discard(parts[-1])
            self.rows[parts[-1]].update(rec.build_flip_patch())
        return row

    def live_runs(self, issue_id):
        if issue_id == "boom":
            raise rec.ReconcileError("HTTP 500: boom")
        if issue_id in self.live:
            return [{"status": "running", "finishedAt": None}]
        return []

    def patch(self, path, body):
        if self.fail_patch > 0:
            self.fail_patch -= 1
            raise rec.ReconcileError("HTTP 500: boom")
        issue_id = path.split("/")[-1]
        if issue_id in self.reject_ids:
            raise rec.ReconcileError("HTTP 422: rejected")
        if issue_id in self.fail_ids:
            raise rec.ReconcileError("HTTP 500: boom")
        if issue_id in self.delayed_commit_ids:
            # 504/timeout: nothing saved yet; the server commits right
            # after the next (confirmatory) re-read.
            self._delayed_pending.add(issue_id)
            raise rec.ReconcileError("HTTP 504: gateway timeout")
        self.patches.append((path, body))
        self.rows[issue_id].update(body)
        for wake_id in self.wake_on_patch:
            self.wake_events.append({"id": wake_id})
        if issue_id in self.commit_then_raise_ids:
            raise rec.ReconcileError("HTTP 500: response lost after commit")
        if self.minimal_echo:
            return {"id": issue_id}
        return dict(self.rows[issue_id])

    def wake_diagnostics(self, issue_id):
        self.wake_calls += 1
        if self.wake_calls in self.fail_wake_calls:
            raise rec.ReconcileError("HTTP 500: wakes boom")
        if self.wakes_after is not None and self.wake_calls > 1:
            return self.wakes_after
        return {"events": list(self.wake_events)}


def backup_file(entries):
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({
        "version": rec.BACKUP_VERSION,
        "tool": "operator_workspace_reconciler",
        "createdAt": "2026-10-09T00:00:00Z",
        "operatorAgentId": OP,
        "flip": rec.build_flip_patch(),
        "entries": entries,
    }, handle, sort_keys=True)
    handle.close()
    return handle.name


class ApplyOneTest(unittest.TestCase):
    def test_happy_path_flips_and_records_patch(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row})
        entry = {"issueId": row["id"], "identifier": "case_8c5fe4cc",
                 "original": rec.original_fields(row)}
        self.assertEqual(
            rec.apply_one(client, entry, 0, OP), "flipped")
        self.assertEqual(client.patches[0][1], rec.build_flip_patch())

    def test_status_move_makes_entry_stale(self):
        row = issue("case_8c5fe4cc", status="in_progress")
        client = FakeClient({row["id"]: row})
        entry = {"issueId": row["id"], "identifier": "case_8c5fe4cc", "original": {}}
        outcome = rec.apply_one(client, entry, 0, OP)
        self.assertTrue(outcome.startswith("stale"))
        self.assertEqual(client.patches, [])

    def test_live_run_makes_entry_stale(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, live={row["id"]})
        entry = {"issueId": row["id"], "identifier": "case_8c5fe4cc", "original": {}}
        outcome = rec.apply_one(client, entry, 0, OP)
        self.assertTrue(outcome.startswith("stale"))
        self.assertEqual(client.patches, [])

    def test_patch_echo_serves_as_readback(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row})
        entry = {"issueId": row["id"], "identifier": "case_8c5fe4cc",
                 "original": rec.original_fields(row)}
        self.assertEqual(rec.apply_one(client, entry, 0, OP), "flipped")
        self.assertEqual(client.gets, 1)

    def test_minimal_echo_falls_back_to_reread(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, minimal_echo=True)
        entry = {"issueId": row["id"], "identifier": "case_8c5fe4cc",
                 "original": rec.original_fields(row)}
        self.assertEqual(rec.apply_one(client, entry, 0, OP), "flipped")
        self.assertEqual(client.gets, 2)


class DryRunTest(unittest.TestCase):
    def test_plan_classifies_and_gates_live_runs(self):
        cand = issue("case_8c5fe4cc")
        live = issue("case_d02d0252")
        clean = issue("case_a6dbab15", pref="agent_default", ew=None, pw=None)
        busy = issue("case_4ac9f045", status="in_progress")
        rows = {r["id"]: r for r in (cand, live, clean, busy)}
        client = FakeClient(rows, live={live["id"]})
        _, plan = rec.run_dry_run(client, OP, workers=4)
        by_id = {e["identifier"]: (e["verdict"], e["reason"]) for e in plan}
        self.assertEqual(by_id["case_8c5fe4cc"], ("candidate", "carries-workspace"))
        self.assertEqual(by_id["case_d02d0252"], ("skip", "live-run"))
        self.assertEqual(by_id["case_a6dbab15"], ("skip", "already-agent-default"))
        self.assertEqual(by_id["case_4ac9f045"], ("skip", "live-work-or-review-path"))

    def test_unreadable_run_list_is_an_error_not_a_skip(self):
        row = dict(issue("case_f052904a"), id="boom")
        client = FakeClient({"boom": row})
        _, plan = rec.run_dry_run(client, OP, workers=2)
        self.assertEqual(plan[0]["verdict"], "error")


class ApplyLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def args(self, backup):
        return rec.parse_args(
            ["--apply", "--backup", backup, "--rate-limit-seconds", "0"])

    def test_two_consecutive_failures_abort(self):
        rows = {f"id-TOG-{n}": issue(f"TOG-{n}") for n in (1, 2, 3)}
        client = FakeClient(rows, fail_patch=99)
        path = backup_file([
            {"issueId": rid, "identifier": rid, "original": {}} for rid in rows
        ])
        with self.assertRaises(rec.ReconcileError):
            rec.do_apply(client, self.args(path))
        self.assertEqual(len(client.patches), 0)

    def test_new_wake_aborts_apply(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, wake_on_patch=["wake-1"])
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc", "original": {}}])
        with self.assertRaises(rec.ReconcileError):
            rec.do_apply(client, self.args(path))

    def test_unreadable_probe_after_read_aborts_with_recorded_outcome(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, fail_wake_calls={2})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc", "original": {}}])
        with self.assertRaises(rec.ReconcileError) as ctx:
            rec.do_apply(client, self.args(path))
        self.assertIn("probe unreadable", str(ctx.exception))
        self.assertEqual(len(client.patches), 1)

    def test_backup_version_gate_refuses(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"version": 0, "entries": []}, handle)
        handle.close()
        with self.assertRaises(rec.ReconcileError):
            rec.load_backup_file(handle.name)


class ArgValidationTest(unittest.TestCase):
    def test_restore_and_apply_are_exclusive(self):
        self.assertEqual(rec.main(["--apply", "--backup", "b", "--restore", "r"]), 2)

    def test_apply_requires_backup(self):
        self.assertEqual(rec.main(["--apply"]), 2)

    def test_negative_rate_limit_is_usage_error(self):
        self.assertEqual(
            rec.main(["--rate-limit-seconds", "-1"]), 2)

    def test_zero_fetch_workers_is_usage_error(self):
        self.assertEqual(rec.main(["--fetch-workers", "0"]), 2)


class OperatorIdThreadingTest(unittest.TestCase):
    def test_apply_honours_backup_operator_id(self):
        row = issue("case_8c5fe4cc", assignee=OTHER)
        client = FakeClient({row["id"]: row})
        entry = {"issueId": row["id"], "identifier": "case_8c5fe4cc",
                 "original": rec.original_fields(row)}
        stale = rec.apply_one(client, entry, 0, OP)
        self.assertTrue(stale.startswith("stale"))
        self.assertEqual(client.patches, [])
        flipped = rec.apply_one(client, entry, 0, OTHER)
        self.assertEqual(flipped, "flipped")
        self.assertEqual(len(client.patches), 1)


class ProbeArmingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_stale_first_entry_does_not_disarm_probe(self):
        stale = issue("case_05330ec4", status="in_progress")
        real = issue("case_8c5fe4cc")
        rows = {r["id"]: r for r in (stale, real)}
        client = FakeClient(rows, wake_on_patch=["wake-1"])
        path = backup_file([
            {"issueId": stale["id"], "identifier": "case_05330ec4", "original": {}},
            {"issueId": real["id"], "identifier": "case_8c5fe4cc", "original": {}},
        ])
        args = rec.parse_args(
            ["--apply", "--backup", path, "--rate-limit-seconds", "0"])
        with self.assertRaises(rec.ReconcileError):
            rec.do_apply(client, args)
        patched = [p[0].split("/")[-1] for p in client.patches]
        self.assertEqual(patched, [real["id"]])

    def test_restore_file_covers_flipped_cards_only(self):
        stale = issue("case_05330ec4", status="in_progress")
        real = issue("case_8c5fe4cc")
        before = rec.original_fields(real)
        rows = {r["id"]: r for r in (stale, real)}
        client = FakeClient(rows)
        path = backup_file([
            {"issueId": stale["id"], "identifier": "case_05330ec4", "original": {}},
            {"issueId": real["id"], "identifier": "case_8c5fe4cc",
             "status": "blocked", "original": before},
        ])
        args = rec.parse_args(
            ["--apply", "--backup", path, "--rate-limit-seconds", "0"])
        self.assertEqual(rec.do_apply(client, args), 0)
        import glob
        restores = glob.glob("operator-workspace-restore-*.json")
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [real["id"]])
        rargs = rec.parse_args(
            ["--restore", restores[0], "--rate-limit-seconds", "0"])
        self.assertEqual(rec.do_restore(client, rargs), 0)
        self.assertEqual(
            rec.original_fields(client.rows[real["id"]]), before)
        self.assertEqual(client.rows[stale["id"]]["status"], "in_progress")

    def test_restore_file_is_loadable_and_reverts(self):
        row = issue("case_8c5fe4cc")
        before = rec.original_fields(row)
        client = FakeClient({row["id"]: row})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": before}])
        args = rec.parse_args(
            ["--apply", "--backup", path, "--rate-limit-seconds", "0"])
        self.assertEqual(rec.do_apply(client, args), 0)
        import glob
        restores = glob.glob("operator-workspace-restore-*.json")
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["entries"]), 1)
        rargs = rec.parse_args(
            ["--restore", restores[0], "--rate-limit-seconds", "0"])
        self.assertEqual(rec.do_restore(client, rargs), 0)
        self.assertEqual(rec.original_fields(client.rows[row["id"]]), before)


class FullReadTest(unittest.TestCase):
    """The list endpoint nulls executionWorkspaceSettings; the dry-run must
    classify and back up from the single-issue read."""

    def test_settings_only_card_is_a_candidate_with_real_original(self):
        settings = {"mode": "isolated_workspace"}
        row = issue("case_8c5fe4cc", pref="agent_default", ew=None, pw=None,
                    settings=settings)
        client = FakeClient(
            {row["id"]: row},
            list_strip_fields=("executionWorkspaceSettings",))
        _, plan = rec.run_dry_run(client, OP, workers=2)
        self.assertEqual(len(plan), 1)
        entry = plan[0]
        self.assertEqual(
            (entry["verdict"], entry["reason"]),
            ("candidate", "carries-workspace"))
        self.assertEqual(
            entry["original"]["executionWorkspaceSettings"], settings)

    def test_candidate_original_keeps_list_nulled_settings(self):
        settings = {"mode": "shared_workspace"}
        row = issue("case_8c5fe4cc", settings=settings)
        client = FakeClient(
            {row["id"]: row},
            list_strip_fields=("executionWorkspaceSettings",))
        _, plan = rec.run_dry_run(client, OP, workers=2)
        self.assertEqual(plan[0]["verdict"], "candidate")
        self.assertEqual(
            plan[0]["original"]["executionWorkspaceSettings"], settings)

    def test_unreadable_full_read_is_an_error(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, fail_get_ids={row["id"]})
        _, plan = rec.run_dry_run(client, OP, workers=2)
        self.assertEqual(plan[0]["verdict"], "error")


class AbortRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def args(self, backup):
        return rec.parse_args(
            ["--apply", "--backup", backup, "--rate-limit-seconds", "0"])

    def restore_paths(self):
        import glob
        return glob.glob("operator-workspace-restore-*.json")

    def test_budget_abort_still_writes_partial_restore(self):
        good = issue("case_8c5fe4cc")
        bad2 = issue("case_d02d0252")
        bad3 = issue("case_a6dbab15")
        rows = {r["id"]: r for r in (good, bad2, bad3)}
        # Definite 4xx rejections: refused server-side, so no restore
        # entry; the partial restore covers only the flipped card.
        client = FakeClient(rows, reject_ids={bad2["id"], bad3["id"]})
        path = backup_file([
            {"issueId": r["id"], "identifier": r["identifier"], "original": {}}
            for r in (good, bad2, bad3)
        ])
        with self.assertRaises(rec.ReconcileError):
            rec.do_apply(client, self.args(path))
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [good["id"]])

    def test_wake_abort_still_writes_restore(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, wake_on_patch=["wake-1"])
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc", "original": {}}])
        with self.assertRaises(rec.ReconcileError):
            rec.do_apply(client, self.args(path))
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [row["id"]])

    def test_probe_read_failure_abort_still_writes_restore(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, fail_wake_calls={2})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc", "original": {}}])
        with self.assertRaises(rec.ReconcileError):
            rec.do_apply(client, self.args(path))
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [row["id"]])

    def test_raw_transport_error_abort_still_writes_partial_restore(self):
        good = issue("case_8c5fe4cc")
        bad2 = issue("case_d02d0252")
        bad3 = issue("case_a6dbab15")
        rows = {r["id"]: r for r in (good, bad2, bad3)}
        client = FakeClient(
            rows, raw_fail_get_ids={bad2["id"], bad3["id"]})
        path = backup_file([
            {"issueId": r["id"], "identifier": r["identifier"], "original": {}}
            for r in (good, bad2, bad3)
        ])
        with self.assertRaises(rec.ReconcileError):
            rec.do_apply(client, self.args(path))
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [good["id"]])

    def test_patch_commit_then_response_lost_is_flip_uncertain_with_restore(self):
        row = issue("case_8c5fe4cc")
        before = rec.original_fields(dict(row))
        client = FakeClient(
            {row["id"]: row}, commit_then_raise_ids={row["id"]})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": before}])
        self.assertEqual(rec.do_apply(client, self.args(path)), 0)
        # The server committed the flip even though the call raised.
        self.assertTrue(rec.readback_ok(client.rows[row["id"]]))
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["outcomes"]), 1)
        self.assertTrue(
            loaded["outcomes"][0]["outcome"].startswith("flip-uncertain"))
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [row["id"]])
        self.assertEqual(loaded["entries"][0]["original"], before)

    def test_patch_uncertain_server_error_is_flip_uncertain_with_restore(self):
        # A 5xx PATCH failure cannot rule out a commit, so the card keeps
        # a restore entry even though the re-read shows the old fields.
        row = issue("case_8c5fe4cc")
        before = rec.original_fields(dict(row))
        client = FakeClient({row["id"]: row}, fail_ids={row["id"]})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": before}])
        self.assertEqual(rec.do_apply(client, self.args(path)), 0)
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["outcomes"]), 1)
        self.assertTrue(
            loaded["outcomes"][0]["outcome"].startswith("flip-uncertain"))
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [row["id"]])
        self.assertEqual(loaded["entries"][0]["original"], before)

    def test_patch_rejected_server_side_is_error_without_restore(self):
        # A definite 4xx rejection means the server refused, so a clean
        # re-read proves nothing changed: plain error, no restore entry.
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, reject_ids={row["id"]})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": {}}])
        self.assertEqual(rec.do_apply(client, self.args(path)), 0)
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["outcomes"]), 1)
        self.assertTrue(
            loaded["outcomes"][0]["outcome"].startswith("error:"))
        self.assertEqual(loaded["entries"], [])

    def test_patch_504_that_saves_after_reread_is_flip_uncertain(self):
        # The reviewer's repro: PATCH 504s without saving, the re-read
        # still shows the old fields, then the server commits. The card
        # must keep a restore entry anyway.
        row = issue("case_8c5fe4cc")
        before = rec.original_fields(dict(row))
        client = FakeClient(
            {row["id"]: row}, delayed_commit_ids={row["id"]})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": before}])
        self.assertEqual(rec.do_apply(client, self.args(path)), 0)
        # The flip landed on the server after the apply finished: the
        # next observer read sees it (the FakeClient commits the pending
        # write on the first GET after the failed PATCH).
        client.get(f"/api/issues/{row['id']}")
        self.assertTrue(rec.readback_ok(client.rows[row["id"]]))
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["outcomes"]), 1)
        self.assertTrue(
            loaded["outcomes"][0]["outcome"].startswith("flip-uncertain"))
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [row["id"]])
        self.assertEqual(loaded["entries"][0]["original"], before)

    def test_patch_4xx_raise_with_unreadable_reread_is_flip_uncertain(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient(
            {row["id"]: row}, reject_ids={row["id"]}, fail_get_calls={2})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": {}}])
        self.assertEqual(rec.do_apply(client, self.args(path)), 0)
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["outcomes"]), 1)
        self.assertTrue(
            loaded["outcomes"][0]["outcome"].startswith("flip-uncertain"))
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [row["id"]])

    def test_patch_raise_with_unreadable_reread_is_flip_uncertain(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient(
            {row["id"]: row}, fail_ids={row["id"]}, fail_get_calls={2})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": {}}])
        self.assertEqual(rec.do_apply(client, self.args(path)), 0)
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["outcomes"]), 1)
        self.assertTrue(
            loaded["outcomes"][0]["outcome"].startswith("flip-uncertain"))
        self.assertEqual(
            [e["issueId"] for e in loaded["entries"]], [row["id"]])

    def test_pre_patch_failure_stays_error_without_restore(self):
        row = issue("case_8c5fe4cc")
        client = FakeClient({row["id"]: row}, fail_get_ids={row["id"]})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": {}}])
        self.assertEqual(rec.do_apply(client, self.args(path)), 0)
        restores = self.restore_paths()
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(len(loaded["outcomes"]), 1)
        self.assertTrue(
            loaded["outcomes"][0]["outcome"].startswith("error:"))
        self.assertEqual(loaded["entries"], [])


class RequestTransportTest(unittest.TestCase):
    """The API wrapper must convert transport-layer failures to
    ReconcileError so the apply loop counts them (and writes the partial
    restore) instead of letting raw exceptions escape."""

    def client(self):
        return rec.PaperclipClient("https://paperclip.example", "k", "c")

    def test_dropped_connection_becomes_reconcile_error(self):
        client = self.client()
        with mock.patch.object(
                rec.urllib.request, "urlopen",
                side_effect=http.client.RemoteDisconnected("boom")):
            with self.assertRaises(rec.ReconcileError):
                client.get("/api/issues/x")

    def test_undecodable_body_becomes_reconcile_error(self):
        class BadBody:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"\xff\xfe not utf-8"

        client = self.client()
        with mock.patch.object(
                rec.urllib.request, "urlopen", return_value=BadBody()):
            with self.assertRaises(rec.ReconcileError):
                client.get("/api/issues/x")


class FreshOriginalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_restore_uses_pre_patch_values_not_dry_run_values(self):
        stale_original = {
            "executionWorkspacePreference": "reuse_existing",
            "executionWorkspaceId": "ew-old",
            "projectWorkspaceId": "pw-old",
            "executionWorkspaceSettings": None,
        }
        row = issue("case_8c5fe4cc", ew="ew-new", pw="pw-new",
                    settings={"mode": "shared_workspace"})
        fresh = rec.original_fields(row)
        self.assertNotEqual(
            {f: stale_original.get(f) for f in rec.WORKSPACE_FIELDS}, fresh)
        client = FakeClient({row["id"]: row})
        path = backup_file(
            [{"issueId": row["id"], "identifier": "case_8c5fe4cc",
              "status": "blocked", "original": stale_original}])
        args = rec.parse_args(
            ["--apply", "--backup", path, "--rate-limit-seconds", "0"])
        self.assertEqual(rec.do_apply(client, args), 0)
        import glob
        restores = glob.glob("operator-workspace-restore-*.json")
        self.assertEqual(len(restores), 1)
        loaded = rec.load_backup_file(restores[0])
        self.assertEqual(loaded["entries"][0]["original"], fresh)
        rargs = rec.parse_args(
            ["--restore", restores[0], "--rate-limit-seconds", "0"])
        self.assertEqual(rec.do_restore(client, rargs), 0)
        self.assertEqual(
            rec.original_fields(client.rows[row["id"]]), fresh)


class RestoreGuardTest(unittest.TestCase):
    def test_restore_skips_live_and_moved_cards(self):
        flipped = {"pref": "agent_default", "ew": None, "pw": None}
        carrying = rec.original_fields(issue("TOG-x"))
        ok = issue("case_8c5fe4cc", pref=flipped["pref"], ew=flipped["ew"],
                   pw=flipped["pw"])
        busy = issue("case_d02d0252", pref=flipped["pref"], ew=flipped["ew"],
                     pw=flipped["pw"], status="in_progress")
        live = issue("case_a6dbab15", pref=flipped["pref"], ew=flipped["ew"],
                     pw=flipped["pw"])
        moved = issue("case_4ac9f045", pref=flipped["pref"], ew=flipped["ew"],
                      pw=flipped["pw"], assignee=OTHER)
        gone = issue("case_0dd2d987", pref=flipped["pref"], ew=flipped["ew"],
                     pw=flipped["pw"], status="done")
        rows = {r["id"]: r for r in (ok, busy, live, moved, gone)}
        client = FakeClient(rows, live={live["id"]})
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({
            "version": rec.BACKUP_VERSION,
            "tool": "operator_workspace_reconciler",
            "createdAt": "2026-10-09T00:00:00Z",
            "operatorAgentId": OP,
            "entries": [
                {"issueId": r["id"], "identifier": r["identifier"],
                 "original": dict(carrying)}
                for r in (ok, busy, live, moved, gone)
            ],
        }, handle, sort_keys=True)
        handle.close()
        rargs = rec.parse_args(
            ["--restore", handle.name, "--rate-limit-seconds", "0"])
        self.assertEqual(rec.do_restore(client, rargs), 0)
        patched = [p[0].split("/")[-1] for p in client.patches]
        self.assertEqual(patched, [ok["id"]])
        self.assertEqual(
            rec.original_fields(client.rows[ok["id"]]), carrying)
        for row in (busy, live, moved, gone):
            self.assertEqual(
                rec.original_fields(client.rows[row["id"]]),
                rec.original_fields(row))


if __name__ == "__main__":
    unittest.main()
