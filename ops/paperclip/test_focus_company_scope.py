#!/usr/bin/env python3
# ===========================================================================
# test_focus_company_scope.py -- acceptance: focus gate excludes foreign-company
# runs before cancellation, with a versioned dry-run summary (public-safe).
#
# Contract under test (paired builder implements; this file is immutable):
#  1. Positive configured-company restriction before focus exclusion. The
#     cancel-candidate query selects only rows of the configured company and
#     then applies the existing within-company focus exclusion. Mixed-company
#     rows and issue-less foreign rows never enter cancel candidates. A
#     defensive company check immediately before any mutation refuses rows
#     whose company does not match. Existing within-company focus policy is
#     preserved; no new focus policy is created.
#  2. Versioned machine-readable dry-run/result summary. An explicit JSON mode
#     (`--format json` or `--json`) prints one JSON object to stdout with:
#       {version: 1, company_id, dry_run, counts: {candidates, refused,
#        confirmed}, runs: [{run_id, company_id, decision, scope_code,
#        error_code, next_action}], next_action}
#     decisions: candidate | refused | confirmed | unknown. Default text output
#     and existing callers (tick() 2-tuple) stay compatible. Refused or unknown
#     cancellations are never labelled completed/confirmed.
#  3. Default non-apply behavior. Default and explicit `--dry-run` perform no
#     cancel, wake, or enforcement-state mutation (no cancel API call, no wake
#     API call, no cancelled.tsv entry). Contradictory `--apply --dry-run`
#     fails closed (non-zero exit, nothing cancelled). Repeated fixture
#     previews are deterministic.
#  4. One-source help/docs. `--help` describes the single company contract,
#     shows one offline check, and states plainly that this does not authorize
#     activation of historical focus restrictions contrary to current owner
#     priorities. Example sentence the builder may copy verbatim:
#       "This does not authorize activation of historical focus restrictions
#        contrary to current owner priorities."
#     Offline check example the builder may copy verbatim:
#       "Offline check: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover
#        -s ops/paperclip -p 'test_focus*.py'"
#
# Red baseline (unmodified source): the SQL tests, adapter refusal tests,
# JSON-summary tests, flag-conflict test, determinism test, and help tests
# FAIL; the controls PASS. Expected red signatures on unmodified source:
#  - positive-predicate tests: assertion (SQL still negates company_id)
#  - 7-column adapter tests: ValueError (tick unpacks 6 columns) or wrong
#    cancel count if the shape is tolerated without a company check
#  - JSON tests: output is text/empty, not parseable JSON
#  - conflict test: exit 0 instead of non-zero
#  - help tests: empty output, contract phrases absent
# Green controls (pass before and after): legacy 6-column happy path, dry-run
# performs no API call, default text output, tick() 2-tuple, preserved
# within-company focus fragments in SQL.
#
# Offline: stdlib unittest with stubbed db()/api(); subprocess probes use a
# temp HOME with no ACTIVE file so no DB, network, host tool, or real run
# cancellation occurs. No skipped assertions. Synthetic fixture IDs only.
#
# Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/paperclip
#      -p 'test_focus*.py'
# ===========================================================================
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import focus_gate

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = Path(HERE, "focus_gate.py").read_text()

LOCAL_CID = focus_gate.CID
FOREIGN_CID = "11111111-1111-4111-8111-111111111111"
INCONSISTENT_CID = ""  # empty simulates a null/malformed company on the row

# 7-column synthetic rows: (run_id, agent_id, iid, ident, why, istatus, company)
EXCLUDED_LOCAL = ("run-local-excluded", "agent-local-1", "issue-local-excluded",
                  "case_local01", "wake", "todo", LOCAL_CID)
FOREIGN_WITH_ISSUE = ("run-foreign-issue", "agent-foreign-1", "issue-foreign-1",
                      "case_foreign01", "wake", "todo", FOREIGN_CID)
FOREIGN_NO_ISSUE = ("run-foreign-noissue", "agent-foreign-2", "-",
                    "case_foreign02", "wake", "-", FOREIGN_CID)
FOREIGN_CEO_NO_ISSUE = ("run-foreign-ceo", focus_gate.CEO, "-",
                        "case_foreign03", "wake", "-", FOREIGN_CID)
INCONSISTENT_ROW = ("run-inconsistent", "agent-local-2", "issue-inconsistent",
                    "case_incons01", "wake", "todo", INCONSISTENT_CID)
MIXED_FIXTURE = [EXCLUDED_LOCAL, FOREIGN_WITH_ISSUE, FOREIGN_NO_ISSUE,
                 INCONSISTENT_ROW]

# Legacy 6-column shape (existing callers bounds): no company column.
LEGACY_LOCAL = EXCLUDED_LOCAL[:6]

JSON_FLAGS = (["--format", "json"], ["--json"])
VOLATILE_KEYS = {"at", "timestamp", "time", "duration", "duration_ms",
                 "elapsed", "elapsed_ms"}


def strip_volatile(obj):
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items()
                if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = mock.patch.object(focus_gate, "STATE", self.tmp.name)
        self.state.start()
        self.addCleanup(self.state.stop)

    def gate_log(self):
        p = Path(self.tmp.name, "gate.jsonl")
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l]

    def cancelled(self):
        p = Path(self.tmp.name, "cancelled.tsv")
        return p.read_text() if p.exists() else ""

    def capture_sql(self):
        seen = {}

        def fake_db(sql):
            seen["sql"] = sql
            return []

        with mock.patch.object(focus_gate, "APPLY", False), \
                mock.patch.object(focus_gate, "db", side_effect=fake_db):
            focus_gate.tick()
        self.assertIn("sql", seen, "tick() must query via db()")
        return seen["sql"]

    def run_main_capture(self, argv, rows, apply=False, api_result="200"):
        """Run main() with mocked db/api; return captured stdout text."""
        Path(self.tmp.name, "ACTIVE").touch()
        calls = []

        def fake_api(method, path, body=None):
            calls.append((method, path))
            return api_result

        buf = io.StringIO()
        with mock.patch.object(focus_gate, "STATE", self.tmp.name), \
                mock.patch.object(focus_gate, "APPLY", apply), \
                mock.patch.object(focus_gate.sys, "argv", argv), \
                mock.patch.object(focus_gate, "db", return_value=[list(r) for r in rows]), \
                mock.patch.object(focus_gate, "api", side_effect=fake_api):
            with redirect_stdout(buf):
                focus_gate.main()
        return buf.getvalue(), calls

    def run_main_json(self, rows, apply=False, api_result="200"):
        """Return (summary, api_calls); fail unless a JSON mode works."""
        outputs = []
        for flags in JSON_FLAGS:
            out, calls = self.run_main_capture(
                ["focus_gate.py"] + flags, rows,
                apply=apply, api_result=api_result)
            outputs.append((flags, out))
            try:
                return json.loads(out), calls
            except (ValueError, json.JSONDecodeError):
                continue
        self.fail(
            "builder must implement --format json or --json printing one JSON "
            f"summary object; got {[o for _, o in outputs]!r}")


class CompanyScopeSql(Harness):
    def test_sql_has_positive_company_predicate_before_exclusion(self):
        sql = self.capture_sql()
        # Positive restriction: company match joined with AND NOT <focus
        # exclusion>, not buried inside a negation.
        self.assertRegex(
            sql,
            re.compile(r"q\.company_id\s*=\s*'" + re.escape(LOCAL_CID) +
                       r"'\s+and\s+not\b", re.IGNORECASE),
            "cancel-candidate query must positively restrict to the configured "
            f"company before focus exclusion; SQL was:\n{sql}")

    def test_sql_has_no_negated_company_predicate(self):
        sql = self.capture_sql().lower()
        self.assertNotIn(
            "not (q.company_id", sql,
            "negated company filter routes foreign-company runs into the "
            "cancellation loop; restrict positively instead")

    def test_sql_selects_company_id_for_row_validation(self):
        sql = self.capture_sql()
        # One occurrence in WHERE plus one in the final SELECT list so the
        # pre-mutation check can refuse inconsistent rows.
        self.assertGreaterEqual(
            sql.lower().count("q.company_id"), 2,
            "final SELECT must include q.company_id for the defensive "
            f"pre-mutation check; SQL was:\n{sql}")

    def test_sql_preserves_within_company_focus_policy(self):
        # Control: passes before and after; the builder keeps existing focus
        # logic instead of inventing a new policy.
        sql = self.capture_sql().lower()
        self.assertIn("q.iid in (select id::text from blk)", sql)
        self.assertIn("(q.iid is null and q.agent_id=", sql)


class CancelCandidateScoping(Harness):
    def test_only_excluded_local_cancelled_in_mixed_fixture(self):
        Path(self.tmp.name, "ACTIVE").touch()
        attempted = []

        def fake_api(method, path, body=None):
            attempted.append(path)
            return "200"

        with mock.patch.object(focus_gate, "APPLY", True), \
                mock.patch.object(focus_gate, "db",
                                  return_value=[list(r) for r in MIXED_FIXTURE]), \
                mock.patch.object(focus_gate, "api", side_effect=fake_api):
            try:
                focus_gate.tick()
            except ValueError:
                self.fail("tick() must accept company-scoped rows and refuse "
                          "foreign/inconsistent rows before mutation "
                          "(unpacked 6 columns; company column missing)")
        self.assertEqual(
            attempted, [f"/heartbeat-runs/{EXCLUDED_LOCAL[0]}/cancel"],
            "only the excluded-local run may reach the cancel route; "
            f"attempted: {attempted}")

    def test_foreign_with_issue_never_api_cancelled(self):
        attempted = []

        def fake_api(method, path, body=None):
            attempted.append(path)
            return "200"

        with mock.patch.object(focus_gate, "APPLY", True), \
                mock.patch.object(focus_gate, "db",
                                  return_value=[list(FOREIGN_WITH_ISSUE)]), \
                mock.patch.object(focus_gate, "api", side_effect=fake_api):
            try:
                focus_gate.tick()
            except ValueError:
                self.fail("tick() must tolerate company-scoped rows; "
                          "foreign-with-issue must be refused, not crash")
        self.assertEqual(attempted, [])
        self.assertEqual(self.cancelled(), "")

    def test_foreign_without_issue_never_api_cancelled(self):
        for row in (FOREIGN_NO_ISSUE, FOREIGN_CEO_NO_ISSUE):
            with self.subTest(run=row[0]):
                attempted = []

                def fake_api(method, path, body=None):
                    attempted.append(path)
                    return "200"

                with mock.patch.object(focus_gate, "APPLY", True), \
                        mock.patch.object(focus_gate, "db",
                                          return_value=[list(row)]), \
                        mock.patch.object(focus_gate, "api",
                                          side_effect=fake_api):
                    try:
                        focus_gate.tick()
                    except ValueError:
                        self.fail("tick() must tolerate company-scoped rows; "
                                  "issue-less foreign runs must be refused")
                self.assertEqual(attempted, [], row[0])
        self.assertEqual(self.cancelled(), "")

    def test_inconsistent_company_refused_before_mutation(self):
        attempted = []

        def fake_api(method, path, body=None):
            attempted.append(path)
            return "200"

        with mock.patch.object(focus_gate, "APPLY", True), \
                mock.patch.object(focus_gate, "db",
                                  return_value=[list(INCONSISTENT_ROW)]), \
                mock.patch.object(focus_gate, "api", side_effect=fake_api):
            try:
                focus_gate.tick()
            except ValueError:
                self.fail("tick() must tolerate company-scoped rows; "
                          "inconsistent rows must be refused before mutation")
        self.assertEqual(attempted, [])
        self.assertEqual(self.cancelled(), "")

    def test_excluded_local_still_cancelled_control(self):
        # Control: legacy 6-column shape keeps working (existing callers).
        attempted = []

        def fake_api(method, path, body=None):
            attempted.append(path)
            return "200"

        with mock.patch.object(focus_gate, "APPLY", True), \
                mock.patch.object(focus_gate, "db",
                                  return_value=[list(LEGACY_LOCAL)]), \
                mock.patch.object(focus_gate, "api", side_effect=fake_api):
            n, cancelled = focus_gate.tick()
        self.assertEqual((n, cancelled), (1, 1))
        self.assertEqual(attempted,
                         [f"/heartbeat-runs/{LEGACY_LOCAL[0]}/cancel"])


class DryRunResultSummary(Harness):
    def assert_summary_shape(self, summary):
        self.assertEqual(summary.get("version"), 1)
        self.assertEqual(summary.get("company_id"), LOCAL_CID)
        counts = summary.get("counts")
        self.assertIsInstance(counts, dict)
        for key in ("candidates", "refused", "confirmed"):
            self.assertIn(key, counts)
            self.assertIsInstance(counts[key], int)
        runs = summary.get("runs")
        self.assertIsInstance(runs, list)
        for run in runs:
            for key in ("run_id", "company_id", "decision", "scope_code",
                        "next_action"):
                self.assertIn(key, run, f"run entry missing {key}: {run!r}")
            self.assertTrue(run["run_id"])
            self.assertIn(run["decision"],
                          ("candidate", "refused", "confirmed", "unknown"))
            self.assertTrue(run["scope_code"])
            self.assertTrue(run["next_action"])
        self.assertTrue(summary.get("next_action"))

    def test_dry_run_summary_has_stable_ids_and_counts(self):
        summary, calls = self.run_main_json(MIXED_FIXTURE, apply=False)
        self.assert_summary_shape(summary)
        self.assertTrue(summary.get("dry_run"))
        self.assertEqual(summary["counts"],
                         {"candidates": 1, "refused": 3, "confirmed": 0})
        by_id = {r["run_id"]: r for r in summary["runs"]}
        self.assertEqual(set(by_id),
                         {r[0] for r in MIXED_FIXTURE})
        self.assertEqual(by_id[EXCLUDED_LOCAL[0]]["company_id"], LOCAL_CID)
        self.assertEqual(by_id[EXCLUDED_LOCAL[0]]["decision"], "candidate")
        for rid in (FOREIGN_WITH_ISSUE[0], FOREIGN_NO_ISSUE[0],
                    INCONSISTENT_ROW[0]):
            self.assertEqual(by_id[rid]["decision"], "refused", rid)
        self.assertEqual(calls, [], "dry run must call no cancel route")

    def test_scope_codes_discriminate_foreign_and_inconsistent(self):
        summary, _ = self.run_main_json(MIXED_FIXTURE, apply=False)
        by_id = {r["run_id"]: r for r in summary["runs"]}
        local = by_id[EXCLUDED_LOCAL[0]]["scope_code"]
        foreign = by_id[FOREIGN_WITH_ISSUE[0]]["scope_code"]
        inconsist = by_id[INCONSISTENT_ROW[0]]["scope_code"]
        self.assertNotEqual(local, foreign)
        self.assertNotEqual(local, inconsist)
        self.assertIn("foreign", foreign.lower())
        self.assertIn("inconsist", inconsist.lower())

    def test_refused_never_labelled_completed(self):
        summary, _ = self.run_main_json([FOREIGN_WITH_ISSUE], apply=False)
        self.assert_summary_shape(summary)
        run = summary["runs"][0]
        self.assertEqual(run["decision"], "refused")
        for key in ("decision", "status", "outcome"):
            if key in run and run[key] is not None:
                self.assertNotIn(str(run[key]).lower(),
                                 ("completed", "confirmed", "cancelled"))
        self.assertEqual(summary["counts"]["confirmed"], 0)

    def test_api_refusal_maps_to_refused_next_action(self):
        summary, calls = self.run_main_json([EXCLUDED_LOCAL], apply=True,
                                            api_result="403")
        self.assert_summary_shape(summary)
        run = summary["runs"][0]
        self.assertEqual(run["decision"], "refused")
        self.assertTrue(run["next_action"])
        self.assertNotIn(str(run["next_action"]).lower(), ("completed",))
        self.assertEqual(summary["counts"]["confirmed"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cancelled(), "",
                         "a refused cancel must record no enforcement state")

    def test_unknown_never_labelled_completed(self):
        for code in ("500", "502"):
            with self.subTest(code=code):
                summary, _ = self.run_main_json(
                    [EXCLUDED_LOCAL], apply=True, api_result=code)
                self.assert_summary_shape(summary)
                run = summary["runs"][0]
                self.assertEqual(run["decision"], "unknown")
                for key in ("decision", "status", "outcome"):
                    if key in run and run[key] is not None:
                        self.assertNotIn(str(run[key]).lower(),
                                         ("completed", "confirmed"))
                self.assertTrue(run.get("error_code"),
                                "unknown outcomes need a stable error code")
                self.assertTrue(run["next_action"])
                self.assertEqual(summary["counts"]["confirmed"], 0)
                self.assertEqual(self.cancelled(), "")

    def test_query_failure_reports_error_without_cancel(self):
        Path(self.tmp.name, "ACTIVE").touch()
        tried = []
        for flags in JSON_FLAGS:
            buf = io.StringIO()

            def boom(sql):
                raise subprocess.CalledProcessError(1, ["docker"],
                                                    stderr="connection refused")

            with mock.patch.object(focus_gate, "STATE", self.tmp.name), \
                    mock.patch.object(focus_gate, "APPLY", False), \
                    mock.patch.object(focus_gate.sys, "argv",
                                      ["focus_gate.py"] + flags), \
                    mock.patch.object(focus_gate, "db", side_effect=boom):
                with redirect_stdout(buf):
                    focus_gate.main()
            tried.append(buf.getvalue())
            try:
                summary = json.loads(buf.getvalue())
            except (ValueError, json.JSONDecodeError):
                continue
            self.assertEqual(summary.get("version"), 1)
            self.assertEqual(summary.get("counts", {}).get("confirmed"), 0)
            text = json.dumps(summary).lower()
            self.assertNotIn("completed", text)
            self.assertTrue(summary.get("next_action"))
            self.assertEqual(self.cancelled(), "")
            return
        self.fail("query failure in JSON mode must report a structured error "
                  f"summary, never a cancel; got {tried!r}")

    def test_tick_return_stays_compatible_control(self):
        # Control: existing callers keep the 2-tuple.
        with mock.patch.object(focus_gate, "APPLY", False), \
                mock.patch.object(focus_gate, "db",
                                  return_value=[list(LEGACY_LOCAL)]):
            result = focus_gate.tick()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertEqual(result, (1, 0))

    def test_default_text_output_stays_compatible_control(self):
        # Control: default human output is unchanged text.
        out, _ = self.run_main_capture(["focus_gate.py"], [LEGACY_LOCAL],
                                       apply=False)
        self.assertIn("non-focus queued: 1 (dry run)", out)


class DryRunSafety(Harness):
    def test_default_is_dry_run_with_no_api_or_state(self):
        # Control: default performs no cancel, wake, or enforcement mutation.
        out, calls = self.run_main_capture(["focus_gate.py"], [LEGACY_LOCAL],
                                           apply=False)
        self.assertEqual(calls, [])
        self.assertEqual(self.cancelled(), "")
        self.assertNotIn("/wakeup", json.dumps(calls))
        self.assertIn("dry run", out)

    def test_explicit_dry_run_performs_no_mutation(self):
        summary, calls = self.run_main_json(MIXED_FIXTURE, apply=False)
        self.assertTrue(summary.get("dry_run"))
        self.assertEqual(calls, [])
        self.assertEqual(self.cancelled(), "")
        self.assertEqual(summary["counts"]["confirmed"], 0)

    def test_apply_dry_run_conflict_fails_closed(self):
        home = tempfile.mkdtemp(prefix="fg-conflict-")
        self.addCleanup(__import__("shutil").rmtree, home, True)
        env = dict(os.environ, HOME=home)
        p = subprocess.run(
            [sys.executable, "focus_gate.py", "--apply", "--dry-run"],
            cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(
            p.returncode, 0,
            "contradictory --apply --dry-run must fail closed (non-zero exit)")
        state = Path(home, ".local", "state", "paperclip-focus", "cancelled.tsv")
        self.assertFalse(state.exists() and state.read_text(),
                         "conflicting flags must record no enforcement state")

    def test_no_wake_in_preview(self):
        _, calls = self.run_main_json(MIXED_FIXTURE, apply=False)
        self.assertEqual([c for c in calls if c[1].endswith("/wakeup")], [])

    def test_repeated_fixture_previews_are_deterministic(self):
        first, _ = self.run_main_json(MIXED_FIXTURE, apply=False)
        second, _ = self.run_main_json(MIXED_FIXTURE, apply=False)
        self.assertEqual(strip_volatile(first), strip_volatile(second))


class HelpDocsContract(unittest.TestCase):
    def run_help(self):
        home = tempfile.mkdtemp(prefix="fg-help-")
        self.addCleanup(__import__("shutil").rmtree, home, True)
        env = dict(os.environ, HOME=home)
        for flags in (["--help"], ["-h"]):
            p = subprocess.run(
                [sys.executable, "focus_gate.py"] + flags,
                cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout
        return ""

    def test_help_describes_one_company_contract(self):
        help_text = self.run_help().lower()
        self.assertIn("company", help_text,
                      "help must describe the single company contract")
        self.assertTrue(
            "configured" in help_text or "scope" in help_text
            or "company_id" in help_text,
            "help must name the configured-company scope")

    def test_help_includes_one_offline_check(self):
        help_text = self.run_help()
        self.assertIn("unittest", help_text,
                      "help must include one offline check command")
        self.assertIn("test_focus", help_text,
                      "offline check must name the focus acceptance suites")

    def test_help_states_no_activation_authorization(self):
        help_text = self.run_help().lower()
        for phrase in ("does not authorize", "activation", "historical",
                       "owner", "priorit"):
            self.assertIn(
                phrase, help_text,
                f"help must state no activation authorization ({phrase!r} "
                "missing); e.g. 'This does not authorize activation of "
                "historical focus restrictions contrary to current owner "
                "priorities.'")


if __name__ == "__main__":
    unittest.main()
