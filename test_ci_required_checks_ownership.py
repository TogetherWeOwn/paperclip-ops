#!/usr/bin/env python3
"""Offline tests for ci_required_checks_ownership.py (case_bab7b60c).

No network or real `gh`: recorded rules and controlled API responses exercise
ownership, per-producer freshness, pagination, and unmeasured read failures.
"""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.parse
from unittest import mock

import ci_required_checks_ownership as own
from ci_required_checks_ownership import audit_repo

OWNERS = {
    "automation-engineer": {"area": "ci-gates", "active": True, "note": ""},
    "devops": {"area": "infra", "active": True, "note": ""},
    "ghost-role": {"area": "gone", "active": False, "note": "departed"},
}

MATRIX_REPO = {
    "repo": "Example/repo",
    "branch": "main",
    "checks": [
        {"context": "check", "owner_role": "automation-engineer",
         "producer_workflow": ".github/workflows/ci.yml", "producer_job": "check",
         "area": "ci", "note": ""},
        {"context": "gitleaks", "owner_role": "ghost-role",
         "producer_workflow": ".github/workflows/ci.yml", "producer_job": "gitleaks",
         "area": "ci", "note": ""},
    ],
}


def codes(findings, severity):
    return {f["code"] for f in findings if f["severity"] == severity}


class OwnershipTest(unittest.TestCase):
    def test_owned_check_is_not_a_fail(self):
        f = audit_repo(OWNERS, MATRIX_REPO, [{"context": "check", "integration_id": 15368}],
                       False, "gh", 14)
        self.assertNotIn("NO_OWNER", codes(f, "FAIL"))
        self.assertNotIn("DEPARTED_OWNER", codes(f, "FAIL"))

    def test_missing_matrix_entry_is_fail_no_owner(self):
        f = audit_repo(OWNERS, MATRIX_REPO, [{"context": "brand-new-gate", "integration_id": 15368}],
                       False, "gh", 14)
        hit = [x for x in f if x["severity"] == "FAIL" and x["code"] == "NO_OWNER"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["context"], "brand-new-gate")

    def test_empty_owner_role_is_fail_no_owner(self):
        repo = {"repo": "Example/repo", "branch": "main", "checks": [
            {"context": "check", "owner_role": "  ", "producer_workflow": "",
             "producer_job": "", "area": "", "note": ""}]}
        f = audit_repo(OWNERS, repo, [{"context": "check", "integration_id": 15368}],
                       False, "gh", 14)
        self.assertIn("NO_OWNER", codes(f, "FAIL"))

    def test_inactive_roster_owner_is_fail_departed(self):
        f = audit_repo(OWNERS, MATRIX_REPO, [{"context": "gitleaks", "integration_id": 15368}],
                       False, "gh", 14)
        hit = [x for x in f if x["severity"] == "FAIL" and x["code"] == "DEPARTED_OWNER"]
        self.assertEqual(len(hit), 1)
        self.assertIn("ghost-role", hit[0]["detail"])

    def test_unknown_role_is_fail_departed(self):
        repo = {"repo": "Example/repo", "branch": "main", "checks": [
            {"context": "check", "owner_role": "nobody-here",
             "producer_workflow": ".github/workflows/ci.yml", "producer_job": "check",
             "area": "", "note": ""}]}
        f = audit_repo(OWNERS, repo, [{"context": "check", "integration_id": 15368}],
                       False, "gh", 14)
        self.assertIn("DEPARTED_OWNER", codes(f, "FAIL"))

    def test_offline_mode_reports_unknown_freshness_not_stale(self):
        f = audit_repo(OWNERS, MATRIX_REPO, [{"context": "check", "integration_id": 15368}],
                       False, "gh", 14)
        self.assertIn("UNKNOWN_FRESHNESS", codes(f, "WARN"))
        self.assertNotIn("STALE_NO_PASS", codes(f, "WARN"))

    def test_live_fresh_success_is_info_ok(self):
        req = [{"context": "check", "integration_id": 15368}]
        with mock.patch.object(own, "latest_success_age_days",
                               return_value=("ok", 0.1, "last success fresh")):
            f = audit_repo(OWNERS, MATRIX_REPO, req, True, "gh", 14)
        ok = [x for x in f if x["code"] == "OK"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(codes(f, "FAIL"), set())

    def test_live_no_success_is_warn_stale_not_fail(self):
        req = [{"context": "check", "integration_id": 15368}]
        with mock.patch.object(own, "latest_success_age_days",
                               return_value=("stale", None, "3 recent run(s), none successful")):
            f = audit_repo(OWNERS, MATRIX_REPO, req, True, "gh", 14)
        self.assertIn("STALE_NO_PASS", codes(f, "WARN"))
        self.assertEqual(codes(f, "FAIL"), set())

    def test_live_old_success_is_warn_stale(self):
        req = [{"context": "check", "integration_id": 15368}]
        with mock.patch.object(own, "latest_success_age_days",
                               return_value=("ok", 30.0, "last success long ago")):
            f = audit_repo(OWNERS, MATRIX_REPO, req, True, "gh", 14)
        self.assertIn("STALE_NO_PASS", codes(f, "WARN"))

    def test_live_unreadable_runs_is_unknown_never_stale(self):
        # A 403 on workflow-runs (this broker has no actions:read) must not
        # read as "the producer never passes".
        req = [{"context": "check", "integration_id": 15368}]
        with mock.patch.object(own, "latest_success_age_days",
                               return_value=("unknown", None, "workflow-runs unreadable: 403")):
            f = audit_repo(OWNERS, MATRIX_REPO, req, True, "gh", 14)
        self.assertIn("UNKNOWN_FRESHNESS", codes(f, "WARN"))
        self.assertNotIn("STALE_NO_PASS", codes(f, "WARN"))

    def test_matrix_extra_is_info_not_fail(self):
        f = audit_repo(OWNERS, MATRIX_REPO, [], False, "gh", 14)
        self.assertEqual(codes(f, "FAIL"), set())
        extra = [x for x in f if x["code"] == "NOT_REQUIRED"]
        self.assertEqual(len(extra), 2)


NOW = dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc)
RUNS = "repos/Example/repo/actions/workflows/ci.yml/runs"


def stamp(days=0):
    return (NOW - dt.timedelta(days=days)).isoformat()


def run_row(run_id=1, **extra):
    return {"id": run_id, "conclusion": "success", "updated_at": stamp(), **extra}


def job_row(job_id=1, name="check", conclusion="success", **extra):
    return {"id": job_id, "name": name, "conclusion": conclusion,
            "completed_at": stamp(1), **extra}


def page(key, rows, total=None):
    return {key: rows, "total_count": len(rows) if total is None else total}


class ProducerHistoryTest(unittest.TestCase):
    def setUp(self):
        self.responses = {(RUNS, 1): page("workflow_runs", [run_row()])}
        self.calls = []

    def jobs(self, rows, run_id=1, page_no=1, total=None):
        self.responses[(f"repos/Example/repo/actions/runs/{run_id}/jobs", page_no)] = page("jobs", rows, total)

    def api(self, gh_bin, verb, endpoint, **kw):
        self.assertEqual((gh_bin, verb), ("gh", "api"))
        parsed = urllib.parse.urlsplit(endpoint)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertNotIn("created", query)  # old-created reruns must remain visible
        self.assertNotIn("status", query)  # unrelated failures must not hide a pass
        self.assertNotIn("branch", query)  # PR-only producers remain visible
        if parsed.path.endswith("/jobs"):
            self.assertEqual(query["filter"], ["all"])
        key = (parsed.path, int(query["page"][0]))
        self.calls.append(key)
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return value

    def measure(self, check="check", days=14, history=None, needed=(), budget=None):
        with mock.patch.object(own, "_gh", side_effect=self.api):
            return own.latest_success_age_days(
                "Example/repo", ".github/workflows/ci.yml", check, "gh", days,
                now=NOW, history=history, needed=needed, budget=budget,
            )

    def test_successful_workflow_skipped_producer_is_stale(self):
        self.jobs([job_row(conclusion="skipped", completed_at=None)])
        self.assertEqual(self.measure()[0], "stale")
        self.assertTrue(any(path.endswith("/jobs") for path, _ in self.calls))

    def test_successful_workflow_missing_producer_is_stale(self):
        self.jobs([job_row(name="unrelated")])
        self.assertEqual(self.measure()[0], "stale")

    def test_failed_workflow_passing_producer_is_fresh(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [run_row(conclusion="failure")])
        self.jobs([job_row(), job_row(2, "unrelated", "failure")])
        state, age, _ = self.measure()
        self.assertEqual((state, age), ("ok", 1))

    def test_reusable_contexts_do_not_share_caller_success(self):
        self.jobs([job_row(name="ci / verify"), job_row(2, "ci / e2e", "skipped")])
        history, wanted = {}, ("ci / verify", "ci / e2e")
        self.assertEqual(self.measure("ci / verify", history=history, needed=wanted)[0], "ok")
        self.assertEqual(self.measure("ci / e2e", history=history, needed=wanted)[0], "stale")
        self.assertEqual(len(self.calls), 2)  # one history read shared by both checks

    def test_job_id_is_not_used_in_place_of_emitted_check_name(self):
        self.jobs([job_row(name="Offline suites")])
        self.assertEqual(self.measure("Offline suites")[0], "ok")
        self.assertEqual(self.measure("offline-suites")[0], "stale")

    def test_newer_cancelled_runs_do_not_hide_page_two_pass(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [
            run_row(i, conclusion="cancelled") for i in range(1, 101)
        ], 101)
        for i in range(1, 101):
            self.jobs([], run_id=i)
        self.responses[(RUNS, 2)] = page("workflow_runs", [run_row(101)], 101)
        self.jobs([job_row()], run_id=101)
        self.assertEqual(self.measure()[:2], ("ok", 1))
        self.assertIn((RUNS, 2), self.calls)

    def test_old_created_rerun_is_read_and_stops_at_first_fresh_pass(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [
            run_row(1, updated_at=stamp(15)),
            run_row(2, created_at=stamp(90)),
            run_row(3, created_at=stamp(120)),
        ])
        self.jobs([job_row(completed_at=stamp(5))], run_id=2)
        self.jobs([job_row(completed_at=stamp(0.25))], run_id=3)
        self.assertEqual(self.measure()[:2], ("ok", 5))
        jobs_read = [path for path, _ in self.calls if path.endswith("/jobs")]
        self.assertEqual(jobs_read, ["repos/Example/repo/actions/runs/2/jobs"])  # run 1 is out of window; run 3 never read

    def test_every_needed_producer_is_read_for_before_stopping(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [run_row(1), run_row(2), run_row(3)])
        self.jobs([job_row(name="a")], run_id=1)
        self.jobs([job_row(name="a", completed_at=stamp(0.5))], run_id=2)
        self.jobs([job_row(name="b", completed_at=stamp(3))], run_id=3)
        state, age, _ = self.measure("b", needed=("a", "b"))
        self.assertEqual((state, age), ("ok", 3))
        self.assertEqual(sum(path.endswith("/jobs") for path, _ in self.calls), 3)

    def test_passing_job_older_than_window_is_not_workflow_update_success(self):
        self.jobs([job_row(completed_at=stamp(15))])
        self.assertEqual(self.measure()[0], "stale")

    def test_configured_window_includes_exact_boundary(self):
        self.jobs([job_row(completed_at=stamp(2))])
        self.assertEqual(self.measure(days=2)[:2], ("ok", 2))
        self.assertEqual(self.measure(days=1)[0], "stale")

    def test_job_pages_and_old_attempts_are_searched(self):
        self.jobs([job_row(i, "unrelated") for i in range(1, 101)], total=101)
        self.jobs([job_row(101)], page_no=2, total=101)
        self.assertEqual(self.measure()[:2], ("ok", 1))

    def test_newest_completion_in_all_attempts_is_selected(self):
        self.jobs([job_row(completed_at=stamp(5)), job_row(2, completed_at=stamp(0.1))])
        self.assertAlmostEqual(self.measure()[1], 0.1)

    def test_truncated_history_without_pass_is_unknown(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [run_row()], 2)
        self.jobs([job_row(conclusion="skipped")])
        with mock.patch.object(own, "MAX_HISTORY_PAGES", 1):
            self.assertEqual(self.measure()[0], "unknown")

    def test_truncated_history_with_measured_pass_is_fresh_not_negative_proof(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [run_row()], 2)
        self.jobs([job_row()])
        with mock.patch.object(own, "MAX_HISTORY_PAGES", 1):
            state, age, detail = self.measure()
            self.assertEqual((state, age), ("ok", 1))
            self.assertNotIn("incomplete", detail)  # stopped early on a measured pass
            # A second wanted producer is still unmeasured, so the unread tail is reported.
            state, age, detail = self.measure(needed=("check", "other"), history={})
        self.assertEqual((state, age), ("ok", 1))
        self.assertIn("incomplete", detail)

    def test_cached_read_that_never_looked_for_a_producer_is_read_again(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [run_row(1), run_row(2)])
        self.jobs([job_row(name="a")], run_id=1)
        self.jobs([job_row(2, "b")], run_id=2)
        history = {}
        self.assertEqual(self.measure("a", history=history, needed=("a",))[0], "ok")
        self.assertEqual(len(self.calls), 2)  # stopped after run 1
        self.assertEqual(self.measure("b", history=history, needed=("b",))[0], "ok")
        self.assertEqual(len(self.calls), 5)  # re-read, not trusted as a negative for "b"

    def test_incomplete_job_pages_without_pass_are_unknown(self):
        self.jobs([job_row(name="unrelated")], total=2)
        with mock.patch.object(own, "MAX_HISTORY_PAGES", 1):
            self.assertEqual(self.measure()[0], "unknown")

    def test_denied_runs_and_jobs_are_unknown(self):
        for key in ((RUNS, 1), ("repos/Example/repo/actions/runs/1/jobs", 1)):
            with self.subTest(key=key):
                self.responses[(RUNS, 1)] = page("workflow_runs", [run_row()])
                self.jobs([])
                self.responses[key] = own.InputError("HTTP 403")
                self.assertEqual(self.measure()[0], "unknown")

    def test_malformed_timestamps_and_responses_are_unknown(self):
        for value in (None, "bad", "2026-10-04T10:00:00", stamp(-1)):
            with self.subTest(value=value):
                self.jobs([job_row(completed_at=value)])
                self.assertEqual(self.measure()[0], "unknown")
        self.responses[(RUNS, 1)] = {"workflow_runs": []}
        self.assertEqual(self.measure()[0], "unknown")

    def test_empty_page_before_reported_total_is_unknown(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [], 2)
        self.assertEqual(self.measure()[0], "unknown")

    def test_empty_complete_history_is_stale(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [])
        self.assertEqual(self.measure()[0], "stale")

    def test_duplicate_pages_do_not_certify_complete_negative_history(self):
        self.responses[(RUNS, 1)] = page("workflow_runs", [run_row()], 2)
        self.responses[(RUNS, 2)] = page("workflow_runs", [run_row()], 2)
        self.jobs([])
        with mock.patch.object(own, "MAX_HISTORY_PAGES", 2):
            self.assertEqual(self.measure()[0], "unknown")

    def test_audit_matches_named_producers_and_reuses_workflow_history(self):
        repo = {**MATRIX_REPO, "checks": [
            {**MATRIX_REPO["checks"][0], "context": "ci / verify"},
            {**MATRIX_REPO["checks"][0], "context": "ci / e2e"},
        ]}
        self.jobs([job_row(name="ci / verify"), job_row(2, "ci / e2e", "skipped")])
        required = [{"context": row["context"]} for row in repo["checks"]]
        with mock.patch.object(own.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = NOW
            with mock.patch.object(own, "_gh", side_effect=self.api):
                findings = own.audit_repo(OWNERS, repo, required, True, "gh", 14)
        by_context = {f["context"]: f for f in findings}
        self.assertEqual(by_context["ci / verify"]["code"], "OK")
        self.assertEqual(by_context["ci / e2e"]["code"], "STALE_NO_PASS")
        self.assertEqual(len(self.calls), 2)


class BoundedReadTest(unittest.TestCase):
    """Busy repos: stop early on a fresh pass, and never read without bound."""

    def setUp(self):
        self.calls = []
        self.responses = {}
        self.now = [0.0]

    def api(self, gh_bin, verb, endpoint, **kw):
        parsed = urllib.parse.urlsplit(endpoint)
        query = urllib.parse.parse_qs(parsed.query)
        self.calls.append((parsed.path, int(query["page"][0]), kw.get("timeout")))
        if parsed.path == RUNS:
            first = (int(query["page"][0]) - 1) * 100
            rows = [run_row(first + i + 1) for i in range(100)]
            return page("workflow_runs", rows, 5000)  # thousands of runs: never complete
        return page("jobs", self.responses.get(parsed.path, [job_row(name="unrelated")]))

    def measure(self, budget, needed=("check",)):
        with mock.patch.object(own, "_gh", side_effect=self.api):
            return own.latest_success_age_days(
                "Example/repo", ".github/workflows/ci.yml", "check", "gh", 14,
                now=NOW, history={}, needed=needed, budget=budget,
            )

    def budget(self, calls, seconds=60.0):
        return own.ReadBudget(calls, seconds, clock=lambda: self.now[0])

    def test_busy_repo_stops_after_first_run_with_a_fresh_pass(self):
        self.responses["repos/Example/repo/actions/runs/1/jobs"] = [job_row()]
        state, age, _ = self.measure(self.budget(60))
        self.assertEqual((state, age), ("ok", 1))
        self.assertEqual([c[0] for c in self.calls],
                         [RUNS, "repos/Example/repo/actions/runs/1/jobs"])  # 2 calls out of 5000 runs

    def test_call_budget_ends_in_unknown_and_is_never_exceeded(self):
        budget = self.budget(7)
        state, age, detail = self.measure(budget)
        self.assertEqual((state, age), ("unknown", None))
        self.assertIn("budget", detail)
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(budget.calls, 7)

    def test_call_budget_is_shared_across_workflows_of_one_repo(self):
        budget = self.budget(4)
        self.measure(budget)
        before = len(self.calls)
        with mock.patch.object(own, "_gh", side_effect=self.api):
            state, _, detail = own.latest_success_age_days(
                "Example/repo", ".github/workflows/other.yml", "check", "gh", 14, now=NOW, history={}, budget=budget)
        self.assertEqual(state, "unknown")
        self.assertEqual(len(self.calls), before)  # nothing left to spend

    def test_wall_clock_budget_ends_in_unknown(self):
        budget = self.budget(1000, seconds=10.0)
        real = self.api
        def slow(gh_bin, verb, endpoint, **kw):
            self.now[0] += 4.0
            return real(gh_bin, verb, endpoint, **kw)
        with mock.patch.object(own, "_gh", side_effect=slow):
            state, _, detail = own.latest_success_age_days(
                "Example/repo", ".github/workflows/ci.yml", "check", "gh", 14, now=NOW, history={}, budget=budget)
        self.assertEqual(state, "unknown")
        self.assertIn("--max-seconds", detail)
        self.assertEqual(len(self.calls), 3)  # 4s, 8s, 12s: the 4th call is refused

    def test_each_call_timeout_fits_the_remaining_budget(self):
        self.now[0] = 0.0
        budget = self.budget(10, seconds=100.0)
        self.assertEqual(budget.begin_call(), own.GH_TIMEOUT)
        self.now[0] = 95.0
        self.assertEqual(budget.begin_call(), 5.0)

    def test_measured_pass_before_budget_runs_out_is_still_fresh(self):
        self.responses["repos/Example/repo/actions/runs/1/jobs"] = [job_row()]
        state, age, _ = self.measure(self.budget(2), needed=("check", "other"))
        self.assertEqual((state, age), ("ok", 1))

    def test_audit_repo_reads_each_workflow_once_within_budget(self):
        self.responses["repos/Example/repo/actions/runs/1/jobs"] = [job_row()]
        self.responses["repos/Example/repo/actions/runs/2/jobs"] = [job_row(2, "gitleaks")]
        required = [{"context": "check"}, {"context": "gitleaks"}]
        with mock.patch.object(own.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = NOW
            with mock.patch.object(own, "_gh", side_effect=self.api):
                findings = own.audit_repo(OWNERS, {**MATRIX_REPO, "checks": [
                    MATRIX_REPO["checks"][0], {**MATRIX_REPO["checks"][1], "owner_role": "devops"}]},
                    required, True, "gh", 14, max_calls=5, max_seconds=30)
        self.assertEqual({f["context"]: f["code"] for f in findings}, {"check": "OK", "gitleaks": "OK"})
        self.assertEqual(len(self.calls), 3)  # one runs page + both runs' jobs, read once for both producers

    def test_audit_repo_budget_turns_a_busy_repo_into_unknown_freshness(self):
        required = [{"context": "check"}]
        with mock.patch.object(own.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = NOW
            with mock.patch.object(own, "_gh", side_effect=self.api):
                findings = own.audit_repo(OWNERS, MATRIX_REPO, required, True, "gh", 14,
                                          max_calls=6, max_seconds=30)
        warned = [f for f in findings if f["severity"] != "INFO"]
        self.assertEqual([(f["severity"], f["code"]) for f in warned], [("WARN", "UNKNOWN_FRESHNESS")])
        self.assertIn("budget", warned[0]["detail"])
        self.assertEqual(len(self.calls), 6)


class UnreadableProcessTest(unittest.TestCase):
    def errors(self):
        return [subprocess.TimeoutExpired(["gh", "api"], 60), FileNotFoundError("gh"),
                PermissionError("gh")]

    def test_launch_and_timeout_failures_become_input_error(self):
        for error in self.errors():
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(own.subprocess, "run", side_effect=error):
                    with self.assertRaises(own.InputError):
                        own._gh("gh", "api", "repos/Example/repo")

    def test_unavailable_freshness_is_unknown_not_ownership_fail(self):
        for error in self.errors():
            with mock.patch.object(own.subprocess, "run", side_effect=error):
                f = audit_repo(OWNERS, MATRIX_REPO, [{"context": "check"}], True, "gh", 14)
            self.assertIn("UNKNOWN_FRESHNESS", codes(f, "WARN"))
            self.assertEqual(codes(f, "FAIL"), set())

    def test_unavailable_rules_returns_exit_two_without_traceback(self):
        matrix = str(Path(__file__).with_name("required_checks_ownership.json"))
        for error in self.errors():
            with mock.patch.object(own.subprocess, "run", side_effect=error), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(own.main(["--matrix", matrix, "--all-repos"]), own.EXIT_UNMEASURED)

    def test_unavailable_freshness_keeps_cli_exit_zero_or_strict_one(self):
        matrix = str(Path(__file__).with_name("required_checks_ownership.json"))
        def process(argv, **kw):
            if "/rules/branches/" in argv[-1]:
                rules = [{"type": "required_status_checks", "parameters": {
                    "required_status_checks": [{"context": "Offline suites"}]}}]
                return subprocess.CompletedProcess(argv, 0, json.dumps(rules), "")
            raise FileNotFoundError("gh")
        args = ["--matrix", matrix, "--repo", "TogetherWeOwn/paperclip-ops", "--json"]
        for strict, expected in (([], 0), (["--strict"], 1)):
            output = io.StringIO()
            with mock.patch.object(own.subprocess, "run", side_effect=process), contextlib.redirect_stdout(output):
                self.assertEqual(own.main(args + strict), expected)
            findings = json.loads(output.getvalue())["repos"][0]["findings"]
            self.assertIn("UNKNOWN_FRESHNESS", codes(findings, "WARN"))
            self.assertEqual(codes(findings, "FAIL"), set())


class InputErrorExitTest(unittest.TestCase):
    """Every bad input exits 2 with one stderr line, never a traceback."""

    MATRIX = str(Path(__file__).with_name("required_checks_ownership.json"))
    REPO = "TogetherWeOwn/paperclip-ops"

    def run_main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = own.main(["--matrix", self.MATRIX, *argv])
        return code, err.getvalue()

    def scratch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def rules_file(self, text):
        path = self.scratch() / "rules.json"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_missing_rules_file_is_exit_two(self):
        code, err = self.run_main("--repo", self.REPO, "--rules-json", "/nonexistent/rules.json")
        self.assertEqual(code, own.EXIT_UNMEASURED)
        self.assertIn("cannot read rules JSON", err)
        self.assertNotIn("Traceback", err)

    def test_invalid_json_rules_file_is_exit_two(self):
        code, err = self.run_main("--repo", self.REPO, "--rules-json", self.rules_file("{not json"))
        self.assertEqual(code, own.EXIT_UNMEASURED)
        self.assertIn("cannot read rules JSON", err)

    def test_non_utf8_rules_file_is_exit_two(self):
        path = self.scratch() / "rules.json"
        path.write_bytes(b"\xff\xfe\x00")
        code, err = self.run_main("--repo", self.REPO, "--rules-json", str(path))
        self.assertEqual(code, own.EXIT_UNMEASURED)
        self.assertNotIn("Traceback", err)

    def test_malformed_rule_rows_are_exit_two(self):
        bad = [
            '["not-an-object"]',
            '[{"type": "required_status_checks", "parameters": "x"}]',
            '[{"type": "required_status_checks", "parameters": {"required_status_checks": {"context": "ci-ok"}}}]',
            '[{"type": "required_status_checks", "parameters": {"required_status_checks": ["ci-ok"]}}]',
            '[{"type": "required_status_checks", "parameters": {"required_status_checks": [{"integration_id": 1}]}}]',
        ]
        for text in bad:
            with self.subTest(text=text):
                code, err = self.run_main("--repo", self.REPO, "--rules-json", self.rules_file(text))
                self.assertEqual(code, own.EXIT_UNMEASURED)
                self.assertIn("rules JSON", err)

    def test_rules_json_with_all_repos_is_rejected_not_ignored(self):
        code, err = self.run_main("--all-repos", "--rules-json", self.rules_file("[]"))
        self.assertEqual(code, own.EXIT_UNMEASURED)
        self.assertIn("cannot be combined", err)

    def test_rules_json_without_repo_is_rejected(self):
        code, err = self.run_main("--rules-json", self.rules_file("[]"))
        self.assertEqual(code, own.EXIT_UNMEASURED)
        self.assertIn("--repo", err)

    def test_valid_recorded_rules_still_measure(self):
        rules = json.dumps([{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "ci-ok"}, {"context": "pr-lint"}]}}])
        code, _ = self.run_main("--repo", self.REPO, "--rules-json", self.rules_file(rules))
        self.assertEqual(code, own.EXIT_OK)

    def test_non_object_matrix_and_bad_repo_rows_are_exit_two(self):
        for text in ("[]", '{"owners": {}, "repos": ["x"]}', '{"owners": {}, "repos": [{"branch": "main"}]}'):
            with self.subTest(text=text):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = own.main(["--matrix", self.rules_file(text), "--all-repos"])
                self.assertEqual(code, own.EXIT_UNMEASURED)
                self.assertNotIn("Traceback", err.getvalue())

    def test_non_integer_stale_days_and_bad_budgets_are_exit_two(self):
        doc = json.loads(Path(self.MATRIX).read_text(encoding="utf-8"))
        doc["stale_days"] = "soon"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(own.main(["--matrix", self.rules_file(json.dumps(doc)), "--repo", self.REPO,
                                       "--rules-json", self.rules_file("[]")]), own.EXIT_UNMEASURED)
        self.assertIn("stale_days", err.getvalue())
        for flag, value in (("--max-calls", "0"), ("--max-seconds", "0")):
            with self.subTest(flag=flag):
                code, err_text = self.run_main("--repo", self.REPO, flag, value)
                self.assertEqual(code, own.EXIT_UNMEASURED)
                self.assertIn("positive", err_text)

    def test_live_default_branch_response_that_is_not_an_object_is_exit_two(self):
        def process(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        err = io.StringIO()
        with mock.patch.object(own.subprocess, "run", side_effect=process), contextlib.redirect_stderr(err):
            code = own.main(["--matrix", self.MATRIX, "--repo", self.REPO, "--branch", ""])
        self.assertEqual(code, own.EXIT_UNMEASURED)
        self.assertNotIn("Traceback", err.getvalue())


# Required contexts as read from the live branch rules on 2026-10-04, after the
# ops-tooling ruleset flipped to the ci-ok aggregator (#654).
LIVE_REQUIRED_2026_10_04 = {
    "TogetherWeOwn/paperclip-ops": ["ci-ok", "pr-lint"],
    "TogetherWeOwn/two-bot-next": ["ci-ok", "pr-lint", "gitleaks"],
    "TogetherWeOwn/two-web-next": ["check", "pr-lint", "gitleaks"],
    "TogetherWeOwn/paperclip": ["ci / verify", "ci / e2e"],
}


class ShippedMatrixTest(unittest.TestCase):
    def test_shipped_matrix_owns_every_context_required_on_2026_10_04(self):
        doc = own.load_matrix(str(Path(__file__).with_name("required_checks_ownership.json")))
        owners = doc["owners"]
        by_repo = {r["repo"]: r for r in doc["repos"]}
        self.assertEqual(set(by_repo), set(LIVE_REQUIRED_2026_10_04))
        for repo, contexts in LIVE_REQUIRED_2026_10_04.items():
            with self.subTest(repo=repo):
                findings = audit_repo(owners, by_repo[repo], [{"context": c} for c in contexts], False, "gh", 14)
                self.assertEqual(codes(findings, "FAIL"), set())


if __name__ == "__main__":
    unittest.main()
