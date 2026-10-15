#!/usr/bin/env python3
"""Hermetic tests for operator_workspace_sweep.py (case_9c941ce5).

Reuses the reconciler suite's fake client: no network, no credentials.
"""
import glob
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import operator_workspace_reconciler as rec
import operator_workspace_sweep as sweep_mod
from test_operator_workspace_reconciler import FakeClient, issue

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "case_629b4da2"),
)
import check_operator_copy_risk as gate  # noqa: E402

STAMP = "20261011T000000Z"


def rows_of(*rows):
    return {row["id"]: row for row in rows}


def clean(identifier, status="blocked"):
    return issue(identifier, status=status, pref="agent_default",
                 ew=None, pw=None)


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        self.state = os.path.join(self.tmp.name, "nested", "state")

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def sweep(self, client):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sweep_mod.sweep(client, self.state, 0, 2, STAMP)
        return code, out.getvalue()

    def files(self, pattern):
        return glob.glob(os.path.join(self.state, pattern))

    def test_clean_fleet_applies_nothing_and_writes_plan(self):
        row = clean("case_8c5fe4cc")
        client = FakeClient(rows_of(row))
        code, out = self.sweep(client)
        self.assertEqual(code, 0)
        self.assertEqual(client.patches, [])
        self.assertIn("no flip candidates", out)
        self.assertEqual(self.files("sweep-backup-*.json"), [])
        with open(os.path.join(self.state, sweep_mod.PLAN_NAME),
                  encoding="utf-8") as handle:
            plan = json.load(handle)
        self.assertEqual(plan["counts"]["candidates"], 0)
        self.assertIsNone(plan["backup"])
        self.assertEqual([e["identifier"] for e in plan["plan"]], ["case_8c5fe4cc"])

    def test_candidate_is_flipped_using_this_runs_backup(self):
        row = issue("case_d02d0252")
        client = FakeClient(rows_of(row))
        code, _ = self.sweep(client)
        self.assertEqual(code, 0)
        self.assertEqual(client.patches,
                         [(f"/api/issues/{row['id']}", rec.build_flip_patch())])
        self.assertTrue(rec.is_clean(rec.original_fields(client.rows[row["id"]])))
        backups = self.files("sweep-backup-*.json")
        self.assertEqual(len(backups), 1)
        with open(backups[0], encoding="utf-8") as handle:
            backup = json.load(handle)
        self.assertEqual([e["identifier"] for e in backup["entries"]], ["case_d02d0252"])
        restores = self.files("operator-workspace-restore-*.json")
        self.assertEqual(len(restores), 1)
        with open(restores[0], encoding="utf-8") as handle:
            restore = json.load(handle)
        self.assertEqual(restore["appliedFrom"], backups[0])
        plan_path = os.path.join(self.state, sweep_mod.PLAN_NAME)
        with open(plan_path, encoding="utf-8") as handle:
            final = json.load(handle)
        self.assertEqual(final["counts"]["candidates"], 0)
        self.assertEqual(final["backup"], backups[0])
        self.assertEqual([e["identifier"] for e in final["plan"]], ["case_d02d0252"])
        self.assertEqual(final["plan"][0]["reason"], "already-agent-default")
        gate_code, _ = gate.check(gate.load_plan(plan_path))
        self.assertEqual(gate_code, 0)

    def test_apply_leaves_cwd_unchanged(self):
        client = FakeClient(rows_of(issue("case_a6dbab15")))
        self.sweep(client)
        self.assertEqual(os.path.realpath(os.getcwd()),
                         os.path.realpath(self.cwd))

    def test_in_progress_and_in_review_cards_are_never_flipped(self):
        client = FakeClient(rows_of(
            issue("case_4ac9f045", status="in_progress"),
            issue("case_0dd2d987", status="in_review"),
        ))
        code, _ = self.sweep(client)
        self.assertEqual(code, 0)
        self.assertEqual(client.patches, [])
        self.assertEqual(self.files("sweep-backup-*.json"), [])

    def test_live_run_card_is_skipped_not_flipped(self):
        row = issue("case_15089482")
        client = FakeClient(rows_of(row), live=[row["id"]])
        code, _ = self.sweep(client)
        self.assertEqual(code, 0)
        self.assertEqual(client.patches, [])
        with open(os.path.join(self.state, sweep_mod.PLAN_NAME),
                  encoding="utf-8") as handle:
            plan = json.load(handle)
        self.assertEqual(plan["plan"][0]["reason"], "live-run")

    def test_rejected_patch_leaves_residue_and_fails_loudly(self):
        row = issue("case_4ee01500")
        client = FakeClient(rows_of(row), reject_ids=[row["id"]])
        with self.assertRaisesRegex(rec.ReconcileError, "case_4ee01500"):
            self.sweep(client)

    def test_unreadable_card_fails_loudly_without_candidates(self):
        row = clean("case_1ef046c6")
        client = FakeClient(rows_of(row), fail_get_ids=[row["id"]])
        with self.assertRaisesRegex(rec.ReconcileError, "case_1ef046c6"):
            self.sweep(client)
        self.assertEqual(client.patches, [])

    def test_budget_abort_propagates_and_keeps_partial_restore(self):
        client = FakeClient(rows_of(
            issue("case_f052904a"), issue("case_38d056f3"), issue("case_5ab07325")), fail_patch=2)
        with self.assertRaisesRegex(rec.ReconcileError, "consecutive failures"):
            self.sweep(client)
        restores = self.files("operator-workspace-restore-*.json")
        self.assertEqual(len(restores), 1)
        with open(restores[0], encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["entries"]), 2)
        self.assertFalse(os.path.exists(
            os.path.join(self.state, sweep_mod.PLAN_NAME)))
        self.assertEqual(os.path.realpath(os.getcwd()),
                         os.path.realpath(self.cwd))

    def test_empty_operator_list_refuses_to_report_clean(self):
        client = FakeClient({})
        with self.assertRaisesRegex(rec.ReconcileError, "empty"):
            self.sweep(client)

    def test_empty_list_after_apply_refuses_to_report_clean(self):
        class EmptyAfterApply(FakeClient):
            def list_operator_issues(self, operator_agent_id, page_size=200,
                                     max_pages=1000):
                if self.patches:
                    return []
                return super().list_operator_issues(
                    operator_agent_id, page_size, max_pages)

        client = EmptyAfterApply(rows_of(issue("case_de259302")))
        with self.assertRaisesRegex(rec.ReconcileError, "empty"):
            self.sweep(client)
        self.assertEqual(len(client.patches), 1)



class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_run_exits_zero_through_main(self):
        client = FakeClient(rows_of(clean("case_a1ff0549")))
        with mock.patch.object(rec, "PaperclipClient", return_value=client), \
                redirect_stdout(io.StringIO()):
            code = sweep_mod.main(["--state-dir", self.state])
        self.assertEqual(code, 0)

    def write_stale_plan(self):
        os.makedirs(self.state, exist_ok=True)
        plan_path = os.path.join(self.state, sweep_mod.PLAN_NAME)
        with open(plan_path, "w", encoding="utf-8") as handle:
            json.dump({"counts": {"candidates": 0}, "plan": [], "backup": None},
                      handle)
        return plan_path

    def test_failed_run_removes_stale_plan(self):
        plan_path = self.write_stale_plan()
        with mock.patch.object(rec, "PaperclipClient", return_value=FakeClient({})), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = sweep_mod.main(["--state-dir", self.state])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(plan_path))

    def test_refused_credentials_remove_stale_plan(self):
        plan_path = self.write_stale_plan()
        with mock.patch.dict(os.environ, {}, clear=True), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = sweep_mod.main(["--state-dir", self.state])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(plan_path))

    def test_missing_credentials_refuse_with_exit_one(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = sweep_mod.main(["--state-dir", self.state])
        self.assertEqual(code, 1)
        self.assertIn("refused:", err.getvalue())

    def test_negative_rate_limit_is_usage_error(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                sweep_mod.parse_args(
                    ["--state-dir", self.state, "--rate-limit-seconds", "-1"])
        self.assertEqual(caught.exception.code, 2)

    def test_zero_fetch_workers_is_usage_error(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                sweep_mod.parse_args(
                    ["--state-dir", self.state, "--fetch-workers", "0"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
