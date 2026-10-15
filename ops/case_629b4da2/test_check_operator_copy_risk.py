#!/usr/bin/env python3
"""Hermetic tests for check_operator_copy_risk.py (case_629b4da2).

No network, no credentials, no board access: pure-function checks over
in-memory reconciler plan rows. Exit codes are pinned; human-readable
message text is not.
"""
import os
import sys
import unittest

import check_operator_copy_risk as gate

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "case_af881f62"),
)
import operator_workspace_reconciler as rec  # noqa: E402


def entry(identifier, status="blocked", pref="reuse_existing",
          ew="ew-1", pw="pw-1", settings=None,
          verdict="candidate", reason="carries-workspace"):
    return {
        "issueId": "id-{}".format(identifier),
        "identifier": identifier,
        "status": status,
        "title": "Operator: {}".format(identifier),
        "verdict": verdict,
        "reason": reason,
        "original": {
            "executionWorkspacePreference": pref,
            "executionWorkspaceId": ew,
            "projectWorkspaceId": pw,
            "executionWorkspaceSettings": settings,
        },
    }


def clean_entry(identifier, status="blocked"):
    return entry(identifier, status=status, pref="agent_default",
                 ew=None, pw=None, settings=None,
                 verdict="skip", reason="already-agent-default")


class IsCleanTest(unittest.TestCase):
    def test_agent_default_with_nulls_is_clean(self):
        self.assertTrue(gate.is_clean({
            "executionWorkspacePreference": "agent_default",
            "executionWorkspaceId": None,
            "projectWorkspaceId": None,
            "executionWorkspaceSettings": None,
        }))

    def test_workspace_ids_are_not_clean(self):
        self.assertFalse(gate.is_clean({
            "executionWorkspacePreference": "agent_default",
            "executionWorkspaceId": "ew-1",
            "projectWorkspaceId": None,
            "executionWorkspaceSettings": None,
        }))

    def test_settings_override_is_not_clean(self):
        self.assertFalse(gate.is_clean({
            "executionWorkspacePreference": "agent_default",
            "executionWorkspaceId": None,
            "projectWorkspaceId": None,
            "executionWorkspaceSettings": {"mode": "isolated_workspace"},
        }))

    def test_null_preference_is_not_clean(self):
        self.assertFalse(gate.is_clean({
            "executionWorkspacePreference": None,
            "executionWorkspaceId": None,
            "projectWorkspaceId": None,
            "executionWorkspaceSettings": None,
        }))

    def test_non_dict_is_not_clean(self):
        self.assertFalse(gate.is_clean(None))


class CheckTest(unittest.TestCase):
    def test_empty_plan_is_clean(self):
        code, _ = gate.check([])
        self.assertEqual(code, 0)

    def test_all_clean_plan_is_clean(self):
        code, _ = gate.check([clean_entry("case_8c5fe4cc"), clean_entry("case_d02d0252")])
        self.assertEqual(code, 0)

    def test_blocked_carrier_fails(self):
        code, lines = gate.check([clean_entry("case_8c5fe4cc"), entry("case_d02d0252")])
        self.assertEqual(code, 1)
        self.assertTrue(any("case_d02d0252" in line for line in lines))

    def test_todo_backlog_and_live_work_carriers_fail(self):
        plan = [
            entry("case_8c5fe4cc", status="todo"),
            entry("case_d02d0252", status="backlog"),
            entry("case_a6dbab15", status="in_progress",
                  verdict="skip", reason="live-work-or-review-path"),
            entry("case_4ac9f045", status="in_review",
                  verdict="skip", reason="live-work-or-review-path"),
        ]
        code, _ = gate.check(plan)
        self.assertEqual(code, 1)
        self.assertEqual(len(gate.at_risk_entries(plan)), 4)

    def test_terminal_carriers_are_not_risk(self):
        plan = [
            entry("case_8c5fe4cc", status="done",
                  verdict="skip", reason="terminal"),
            entry("case_d02d0252", status="cancelled",
                  verdict="skip", reason="terminal"),
        ]
        code, _ = gate.check(plan)
        self.assertEqual(code, 0)

    def test_live_run_skip_with_workspace_is_risk(self):
        plan = [entry("case_8c5fe4cc", verdict="skip", reason="live-run")]
        code, _ = gate.check(plan)
        self.assertEqual(code, 1)

    def test_report_names_every_risky_card(self):
        plan = [entry("case_8c5fe4cc"), entry("case_d02d0252"), clean_entry("case_a6dbab15")]
        _, lines = gate.check(plan)
        body = "\n".join(lines)
        self.assertIn("case_8c5fe4cc", body)
        self.assertIn("case_d02d0252", body)
        self.assertNotIn("case_a6dbab15", body)

    def test_unreadable_entry_with_clean_fields_is_still_risk(self):
        # Fail closed: the list row nulls executionWorkspaceSettings, so a
        # settings-only carrier looks clean exactly when its true state is
        # unknown (verdict error = full-read-unreadable / run-list-unreadable).
        bad = clean_entry("case_f052904a")
        bad["verdict"] = "error"
        bad["reason"] = "full-read-unreadable: timeout"
        code, lines = gate.check([bad])
        self.assertEqual(code, 1)
        self.assertTrue(any("case_f052904a" in line for line in lines))

    def test_error_on_terminal_card_is_not_risk(self):
        bad = clean_entry("case_f052904a", status="done")
        bad["verdict"] = "error"
        bad["reason"] = "full-read-unreadable: timeout"
        code, _ = gate.check([bad])
        self.assertEqual(code, 0)


class ParityTest(unittest.TestCase):
    def test_workspace_fields_match_reconciler(self):
        self.assertEqual(
            tuple(gate.WORKSPACE_FIELDS), tuple(rec.WORKSPACE_FIELDS))
        self.assertEqual(gate.FLIP_PREFERENCE, rec.FLIP_PREFERENCE)

    def test_is_clean_agrees_with_reconciler(self):
        clean = {
            "executionWorkspacePreference": "agent_default",
            "executionWorkspaceId": None,
            "projectWorkspaceId": None,
            "executionWorkspaceSettings": None,
        }
        cases = [
            dict(clean),
            dict(clean, executionWorkspaceId="ew-1"),
            dict(clean, projectWorkspaceId="pw-1"),
            dict(clean, executionWorkspaceSettings={"mode": "x"}),
            dict(clean, executionWorkspacePreference=None),
            dict(clean, executionWorkspacePreference="reuse_existing",
                 executionWorkspaceId="ew-1", projectWorkspaceId="pw-1"),
            {},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                self.assertEqual(gate.is_clean(fields), rec.is_clean(fields))


class LoadPlanTest(unittest.TestCase):
    def test_missing_plan_list_refused(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False) as handle:
            handle.write('{"counts": {}}')
            path = handle.name
        try:
            with self.assertRaises(ValueError):
                gate.load_plan(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
