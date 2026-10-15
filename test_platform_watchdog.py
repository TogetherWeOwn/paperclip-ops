#!/usr/bin/env python3
"""Offline tests for watchdog/detectors.py (case_311c4d5b, phase 1).

No credentials, no network, no board writes. Every case runs on fixtures.
"""

import contextlib
import copy
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from watchdog import detectors
from watchdog.assignment_contract import OUTPUT_SCHEMA
from watchdog.detectors import (
    ASSIGNMENT_VIOLATION_REVIEW_AUTHOR,
    ASSIGNMENT_VIOLATION_UNASSIGNED,
    CHURN_VIOLATION_MISSING_DISPOSITION,
    CHURN_VIOLATION_REWOKE,
    FUTURE_SKEW_MIN,
    WRITER_COMPATIBLE_MUTATIONS,
    assignment_dedupe_key,
    churn_dedupe_key,
    detect,
    main,
    now_source,
)

NOW = datetime.datetime(2026, 10, 3, 14, 0, 0,
                        tzinfo=datetime.timezone.utc)


def snap(**overrides):
    base = {"now": "2026-10-03T14:00:00Z", "agents": [], "hosts": [],
            "ciJobs": [], "queueAge": [], "issues": [], "runEvents": [],
            "assignmentEvidence": {
                "schema": OUTPUT_SCHEMA, "complete": True,
                "sources": {
                    "issues": {"complete": True, "ref": "fixture:issues"},
                    "focus": {"complete": True, "ref": "fixture:focus"},
                    "reviews": {"complete": True, "ref": "fixture:reviews"}}},
            "parks": [], "redMains": [], "secretHits": [],
            "quotaLanes": [],
            "pendingInteractions": [],
            "reviews": [], "openProposalSignatures": [],
            "assignedInProgress": [], "agentRuns": [],
            "supply": {"readyNow": 12, "target": 12, "belowMin": 0.0,
                       "idleAgents": 2,
                       "censusAt": "2026-10-03T14:00:00Z"}}
    base.update(overrides)
    if "issues" in overrides and "assignmentEvidence" not in overrides:
        rows = []
        for source in base["issues"]:
            if not isinstance(source, dict):
                rows.append(source)
                continue
            row = copy.deepcopy(source)
            row.setdefault("isReview", False)
            row.setdefault("prAuthorAgentId", None)
            provenance = row.setdefault("provenance", {})
            if isinstance(provenance, dict):
                if "inFocus" in row:
                    provenance.setdefault("inFocus", {
                        "source": "focus-membership", "ref": "fixture:focus"})
                if row.get("inBacklogMin") is not None:
                    provenance.setdefault("inBacklogMin", {
                        "source": "status-history", "ref": "fixture:history"})
                provenance.setdefault("isReview", {
                    "source": "review-request" if row["isReview"]
                    else "review-inventory", "ref": "fixture:reviews"})
                if row["isReview"] is True and row.get("prAuthorAgentId"):
                    provenance.setdefault("prAuthorAgentId", {
                        "source": "review-request", "ref": "fixture:pr"})
            rows.append(row)
        base["issues"] = rows
    return base


def reasons(records, detector):
    return [r["reason"] for r in records
            if r.get("detector") == f"watchdog/{detector}"]


class AgentErrorTest(unittest.TestCase):
    def test_benign_signature_is_info_not_reset(self):
        records = detect(snap(agents=[{
            "agentId": "a1", "status": "error",
            "errorSince": "2026-10-03T13:30:00Z",
            "subtype": "success", "terminalResultSeen": True,
            "owningLead": "cto"}]))
        hits = [r for r in records
                if r.get("detector") == "watchdog/agent_error"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "info")
        self.assertIn("benign", hits[0]["reason"].lower())

    def test_non_benign_routes_to_lead(self):
        records = detect(snap(agents=[{
            "agentId": "a2", "status": "error",
            "errorSince": "2026-10-03T13:00:00Z",
            "subtype": "crash", "owningLead": "cto"}]))
        hits = [r for r in records
                if r.get("detector") == "watchdog/agent_error"]
        self.assertEqual(hits[0]["severity"], "high")
        self.assertEqual(hits[0]["owner"], "cto")

    def test_under_threshold_is_silent(self):
        records = detect(snap(agents=[{
            "agentId": "a3", "status": "error",
            "errorSince": "2026-10-03T13:55:00Z",
            "subtype": "crash"}]))
        self.assertEqual(reasons(records, "agent_error"), [])

    def test_missing_section_is_unknown(self):
        records = detect(snap())
        del records  # replaced below
        s = snap()
        del s["agents"]
        records = detect(s)
        self.assertIn("agents snapshot missing",
                      reasons(records, "agent_error"))


class HostHealthTest(unittest.TestCase):
    def test_disk_threshold_fires(self):
        records = detect(snap(hosts=[
            {"host": "192.0.2.190", "diskPct": 85, "load": 1.0,
             "vcpu": 4, "sustainedMin": 10}]))
        self.assertIn("disk at or above 85%",
                      reasons(records, "host_health"))

    def test_disk_just_below_is_silent(self):
        records = detect(snap(hosts=[
            {"host": "192.0.2.190", "diskPct": 84.9, "load": 1.0,
             "vcpu": 4, "sustainedMin": 10}]))
        self.assertNotIn("disk at or above 85%",
                         reasons(records, "host_health"))

    def test_missing_coolify_is_coverage_gap(self):
        records = detect(snap(hosts=[
            {"host": "garm-1", "diskPct": 10, "load": 1.0,
             "vcpu": 4, "sustainedMin": 10}]))
        self.assertIn("required host missing from snapshot",
                      reasons(records, "host_health"))

    def test_load_threshold(self):
        records = detect(snap(hosts=[
            {"host": "192.0.2.190", "diskPct": 10, "load": 6.1,
             "vcpu": 4, "sustainedMin": 5}]))
        self.assertIn("load above 1.5x vCPU sustained",
                      reasons(records, "host_health"))


class CiHealthTest(unittest.TestCase):
    def test_runtime_above_2x_fires(self):
        records = detect(snap(ciJobs=[{
            "repo": "r", "job": "j", "runtimeMin": 41, "baselineMin": 20}]))
        self.assertIn("CI job runtime above 2x baseline",
                      reasons(records, "ci_health"))

    def test_runtime_at_2x_is_silent(self):
        records = detect(snap(ciJobs=[{
            "repo": "r", "job": "j", "runtimeMin": 40, "baselineMin": 20}]))
        self.assertNotIn("CI job runtime above 2x baseline",
                         reasons(records, "ci_health"))

    def test_zero_baseline_is_unknown(self):
        records = detect(snap(ciJobs=[{
            "repo": "r", "job": "j", "runtimeMin": 5, "baselineMin": 0}]))
        self.assertIn("CI job with non-positive baseline",
                      reasons(records, "ci_health"))

    def test_queue_high(self):
        records = detect(snap(queueAge=[{"label": "self-hosted",
                                         "ageMin": 160}]))
        hits = [r for r in records
                if r.get("reason") == "runner queue age elevated"]
        self.assertEqual(hits[0]["severity"], "high")


class AssignmentTest(unittest.TestCase):
    def test_unassigned_in_focus_fires_with_proposal(self):
        records = detect(snap(issues=[{
            "id": "iid-1", "identifier": "case_8c5fe4cc", "status": "backlog",
            "assigneeAgentId": None, "inFocus": True, "inBacklogMin": 90,
            "isReview": False, "prAuthorAgentId": None}]))
        self.assertIn("in-focus card unassigned or in backlog over 1h",
                      reasons(records, "assignment"))
        proposals = [r for r in records if r.get("type") == "proposal"]
        self.assertTrue(all(p["mutation"] == "none" for p in proposals))

    def test_review_by_author_fires(self):
        records = detect(snap(issues=[{
            "id": "iid-2", "identifier": "case_d02d0252", "status": "in_review",
            "assigneeAgentId": "agent-x", "inFocus": True, "inBacklogMin": 0,
            "isReview": True, "prAuthorAgentId": "agent-x"}]))
        self.assertIn("review card assigned to the PR author",
                      reasons(records, "assignment"))

    def test_out_of_focus_is_silent(self):
        records = detect(snap(issues=[{
            "id": "iid-3", "identifier": "case_a6dbab15", "status": "backlog",
            "assigneeAgentId": None, "inFocus": False, "inBacklogMin": 999,
            "isReview": False, "prAuthorAgentId": None}]))
        self.assertEqual(reasons(records, "assignment"), [])

    def test_assigned_in_time_is_silent_green(self):
        # Positive green control: correctly assigned, fresh backlog,
        # independent reviewer stays silent.
        records = detect(snap(issues=[{
            "id": "iid-4", "identifier": "case_4ac9f045", "status": "todo",
            "assigneeAgentId": "agent-a", "inFocus": True,
            "inBacklogMin": 10, "isReview": True,
            "prAuthorAgentId": "agent-b"}]))
        self.assertEqual(reasons(records, "assignment"), [])

    def test_review_by_independent_reviewer_is_silent(self):
        records = detect(snap(issues=[{
            "id": "iid-5", "identifier": "case_0dd2d987", "status": "in_review",
            "assigneeAgentId": "agent-a", "inFocus": True, "inBacklogMin": 0,
            "isReview": True, "prAuthorAgentId": "agent-b"}]))
        self.assertEqual(reasons(records, "assignment"), [])

    def test_backlog_at_threshold_is_silent_over_fires(self):
        at = snap(issues=[{
            "id": "iid-6", "identifier": "case_15089482", "status": "todo",
            "assigneeAgentId": "agent-a", "inFocus": True,
            "inBacklogMin": 60, "isReview": False,
            "prAuthorAgentId": None}])
        self.assertEqual(reasons(detect(at), "assignment"), [])
        over = snap(issues=[{
            "id": "iid-6", "identifier": "case_15089482", "status": "backlog",
            "assigneeAgentId": "agent-a", "inFocus": True,
            "inBacklogMin": 61, "isReview": False,
            "prAuthorAgentId": None}])
        self.assertIn("in-focus card unassigned or in backlog over 1h",
                      reasons(detect(over), "assignment"))

    def test_dedupe_keys_stable_per_card_violation(self):
        # Positive control: repeat polls emit the same key (no timestamps
        # in the key), finding and proposal share it, and the two
        # violations use distinct keys so one card can hold both without
        # duplicate incident cards.
        first = detect(snap(issues=[{
            "id": "iid-7", "identifier": "case_4ee01500", "status": "backlog",
            "assigneeAgentId": "agent-x", "inFocus": True,
            "inBacklogMin": 999, "isReview": True,
            "prAuthorAgentId": "agent-x"}]))
        second = detect(snap(issues=[{
            "id": "iid-7", "identifier": "case_4ee01500", "status": "backlog",
            "assigneeAgentId": "agent-x", "inFocus": True,
            "inBacklogMin": 1000, "isReview": True,
            "prAuthorAgentId": "agent-x"}]))
        keys_first = sorted(
            r["dedupeKey"] for r in first
            if r.get("detector") == "watchdog/assignment"
            and "dedupeKey" in r)
        keys_second = sorted(
            r["dedupeKey"] for r in second
            if r.get("detector") == "watchdog/assignment"
            and "dedupeKey" in r)
        self.assertEqual(keys_first, keys_second)
        self.assertEqual(
            keys_first,
            [f"watchdog/assignment:{ASSIGNMENT_VIOLATION_REVIEW_AUTHOR}:iid-7"] * 2
            + [f"watchdog/assignment:{ASSIGNMENT_VIOLATION_UNASSIGNED}:iid-7"] * 2)
        findings = [r for r in first
                    if r.get("detector") == "watchdog/assignment"
                    and r.get("type") == "finding"]
        proposals = [r for r in first
                     if r.get("detector") == "watchdog/assignment"
                     and r.get("type") == "proposal"]
        self.assertEqual(
            sorted(r["dedupeKey"] for r in findings),
            sorted(r["dedupeKey"] for r in proposals))

    def test_dedupe_helper_format(self):
        self.assertEqual(
            assignment_dedupe_key(ASSIGNMENT_VIOLATION_UNASSIGNED, "iid-9"),
            "watchdog/assignment:unassigned-backlog:iid-9")


class ChurnTest(unittest.TestCase):
    def test_rewoke_burst_fires(self):
        events = [{"issueId": "iid-9", "identifier": "case_f052904a",
                   "kind": "dispatch_stalled_issue",
                   "at": f"2026-10-03T13:0{i}:00Z"} for i in range(4)]
        records = detect(snap(runEvents=events))
        self.assertIn("card re-woken more than 3 times in 6h",
                      reasons(records, "churn"))

    def test_three_rewokes_silent(self):
        events = [{"issueId": "iid-9", "identifier": "case_f052904a",
                   "kind": "dispatch_stalled_issue",
                   "at": f"2026-10-03T13:0{i}:00Z"} for i in range(3)]
        records = detect(snap(runEvents=events))
        self.assertNotIn("card re-woken more than 3 times in 6h",
                         reasons(records, "churn"))

    def test_park_count_fires(self):
        records = detect(snap(parks=[{
            "issueId": "iid-8", "identifier": "case_1ef046c6",
            "missingDispositionCount": 3}]))
        self.assertIn("card parked for missing disposition more than twice",
                      reasons(records, "churn"))

    def test_two_misses_stay_quiet(self):
        # Quiet control per case_ad1c18ec: at (not above) each threshold the
        # detector emits nothing.
        events = [{"issueId": "iid-9", "identifier": "case_f052904a",
                   "kind": "dispatch_stalled_issue",
                   "at": f"2026-10-03T13:0{i}:00Z"} for i in range(3)]
        records = detect(snap(
            runEvents=events,
            parks=[{"issueId": "iid-8", "identifier": "case_1ef046c6",
                    "missingDispositionCount": 2}]))
        self.assertEqual(reasons(records, "churn"), [])

    def test_rewoke_and_park_routes_carry_dedupe_keys(self):
        events = [{"issueId": "iid-9", "identifier": "case_f052904a",
                   "kind": "dispatch_stalled_issue",
                   "at": f"2026-10-03T13:0{i}:00Z"} for i in range(4)]
        records = detect(snap(
            runEvents=events,
            parks=[{"issueId": "iid-8", "identifier": "case_1ef046c6",
                    "missingDispositionCount": 3}]))
        churn_rows = [r for r in records
                      if r.get("detector") == "watchdog/churn"]
        self.assertEqual(len(churn_rows), 4)  # 2 findings + 2 proposals
        for row in churn_rows:
            if row.get("type") == "finding":
                self.assertEqual(row["owner"], "ceo")
                self.assertEqual(row["route"], "ceo-hourly-routine")
        findings = [r for r in churn_rows if r.get("type") == "finding"]
        proposals = [r for r in churn_rows if r.get("type") == "proposal"]
        self.assertEqual(
            sorted(r["dedupeKey"] for r in findings),
            sorted(r["dedupeKey"] for r in proposals))

    def test_dedupe_keys_stable_per_card_signature(self):
        # Positive control: repeat polls emit the same key (no timestamps
        # or counts in the key), and the two violations use distinct keys
        # so one card can hold both without duplicate incident cards.
        def fixture(rewoke_n, park_n):
            events = [{"issueId": "iid-9", "identifier": "case_f052904a",
                       "kind": "dispatch_stalled_issue",
                       "at": f"2026-10-03T13:{i:02d}:00Z"}
                      for i in range(rewoke_n)]
            parks = [{"issueId": "iid-9", "identifier": "case_f052904a",
                      "missingDispositionCount": park_n}]
            return snap(runEvents=events, parks=parks)
        first = detect(fixture(4, 3))
        second = detect(fixture(9, 10))
        keys_first = sorted(
            r["dedupeKey"] for r in first
            if r.get("detector") == "watchdog/churn"
            and "dedupeKey" in r)
        keys_second = sorted(
            r["dedupeKey"] for r in second
            if r.get("detector") == "watchdog/churn"
            and "dedupeKey" in r)
        self.assertEqual(keys_first, keys_second)
        self.assertEqual(
            keys_first,
            [f"watchdog/churn:{CHURN_VIOLATION_MISSING_DISPOSITION}:iid-9"] * 2
            + [f"watchdog/churn:{CHURN_VIOLATION_REWOKE}:iid-9"] * 2)

    def test_dedupe_helper_format(self):
        self.assertEqual(
            churn_dedupe_key(CHURN_VIOLATION_REWOKE, "iid-9"),
            "watchdog/churn:rewoke-burst:iid-9")
        self.assertEqual(
            churn_dedupe_key(CHURN_VIOLATION_MISSING_DISPOSITION, "iid-9"),
            "watchdog/churn:missing-disposition:iid-9")


class RedMainTest(unittest.TestCase):
    def test_new_red_fires_high(self):
        records = detect(snap(redMains=[{
            "repo": "paperclip-ops", "signature": "s1",
            "incidentExists": False}]))
        hits = [r for r in records
                if r.get("detector") == "watchdog/red_main"]
        self.assertEqual(hits[0]["severity"], "high")

    def test_existing_incident_is_info_repeat(self):
        records = detect(snap(redMains=[{
            "repo": "paperclip-ops", "signature": "s1",
            "incidentExists": True}]))
        hits = [r for r in records
                if r.get("detector") == "watchdog/red_main"]
        self.assertEqual(hits[0]["severity"], "info")
        self.assertEqual(
            len([r for r in records if r.get("type") == "proposal"]), 0)


class SecretHitTest(unittest.TestCase):
    REFUSAL = "finding evidence carries a possible secret value"

    def metadata_snapshot(self, field, value):
        snapshot = snap(
            secretHits=[{"source": "fixture:secret-hit", "patternName": "ghp_*",
                         "valueLen": 40, "at": "2026-10-03T13:00:00Z"}],
            issues=[{"id": "fixture-assignment", "identifier": "fixture-card",
                     "inFocus": True, "assigneeAgentId": None,
                     "inBacklogMin": 90}])
        if field == "assignment.identifier":
            snapshot["issues"][0]["identifier"] = value
        else:
            snapshot["secretHits"][0][field] = value
        return snapshot

    def metadata_cases(self, lengths):
        # Build unmistakably fake values at runtime, never token-shaped literals.
        prefixes = (("classic", "ghp_", "FAKE000"),
                    ("fine-grained", "github_pat_", "FAKE000"),
                    ("fine-grained-underscore", "github_pat_", "FAKE_00"))
        for name, prefix, stem in prefixes:
            for length in lengths:
                value = prefix + stem + "0" * (length - len(stem))
                for field in ("source", "patternName", "assignment.identifier"):
                    yield name, length, field, value

    def assert_ciso_and_none_proposal(self, records):
        hits = [r for r in records
                if r.get("detector") == "watchdog/secret_hit"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")
        self.assertEqual(hits[0]["owner"], "ciso")
        self.assertEqual(hits[0]["route"], "ciso")
        proposals = [r for r in records if r.get("type") == "proposal"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["mutation"], "none")

    def assert_metadata_preserved(self, records, field, value):
        self.assert_ciso_and_none_proposal(records)
        detector = ("watchdog/assignment" if field == "assignment.identifier"
                    else "watchdog/secret_hit")
        key = "identifier" if field == "assignment.identifier" else field
        hits = [r for r in records if r.get("type") == "finding"
                and r.get("detector") == detector]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["evidence"][key], value)

    def run_cli(self, snapshot):
        scratch = (os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
                   or os.environ.get("PAPERCLIP_SCRATCH_DIR"))
        with tempfile.TemporaryDirectory(prefix="watchdog-secret-fixture-",
                                         dir=scratch) as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            return subprocess.run(
                [sys.executable,
                 str(Path(__file__).resolve().parent / "watchdog/detectors.py"),
                 "--snapshot", str(path)],
                capture_output=True, text=True, timeout=10,
                # No inherited credentials or API configuration in the child.
                env={"PYTHONIOENCODING": "utf-8",
                     "PYTHONDONTWRITEBYTECODE": "1"})

    def cli_records(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(records[-1]["type"], "summary")
        self.assertEqual(records[-1]["records"], len(records) - 1)
        return records[:-1]

    def test_hit_is_critical_and_value_free(self):
        for pattern in ("ghp_*", "github_pat_*"):
            with self.subTest(pattern=pattern):
                records = detect(self.metadata_snapshot("patternName", pattern))
                self.assert_metadata_preserved(records, "patternName", pattern)
                hits = [r for r in records
                        if r.get("detector") == "watchdog/secret_hit"]
                blob = json.dumps(hits[0])
                without_pattern = blob.replace(
                    f'"patternName": "{pattern}"', "")
                self.assertNotIn("ghp_", without_pattern)
                self.assertNotIn("github_pat_", without_pattern)

    def test_value_shaped_metadata_is_refused_without_echo(self):
        for prefix, length, field, value in self.metadata_cases((8, 9)):
            with self.subTest(prefix=prefix, length=length, field=field):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    with self.assertRaises(ValueError) as caught:
                        detect(self.metadata_snapshot(field, value))
                self.assertEqual(str(caught.exception), self.REFUSAL)
                self.assertNotIn(value, str(caught.exception))
                self.assertNotIn(value, stdout.getvalue())
                self.assertNotIn(value, stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_value_shaped_metadata_cli_is_refused_without_echo(self):
        for prefix, length, field, value in self.metadata_cases((8, 9)):
            with self.subTest(prefix=prefix, length=length, field=field):
                result = self.run_cli(self.metadata_snapshot(field, value))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.endswith(
                    f"ValueError: {self.REFUSAL}\n"))
                self.assertNotIn(value, result.stdout)
                self.assertNotIn(value, result.stderr)

    def test_below_length_boundary_metadata_is_preserved(self):
        for prefix, length, field, value in self.metadata_cases((7,)):
            with self.subTest(prefix=prefix, length=length, field=field):
                records = detect(self.metadata_snapshot(field, value))
                self.assert_metadata_preserved(records, field, value)

    def test_below_length_boundary_metadata_cli_is_preserved(self):
        for prefix, length, field, value in self.metadata_cases((7,)):
            with self.subTest(prefix=prefix, length=length, field=field):
                result = self.run_cli(self.metadata_snapshot(field, value))
                records = self.cli_records(result)
                self.assert_metadata_preserved(records, field, value)

    def test_wildcard_patterns_cli_keep_ciso_route_and_none_proposals(self):
        for pattern in ("ghp_*", "github_pat_*"):
            with self.subTest(pattern=pattern):
                result = self.run_cli(
                    self.metadata_snapshot("patternName", pattern))
                records = self.cli_records(result)
                self.assert_metadata_preserved(records, "patternName", pattern)


class StaleReviewTest(unittest.TestCase):
    def review(self, **overrides):
        card = {"issueId": "iid-r", "identifier": "TOG-R",
                "status": "in_review", "isReview": True,
                "assigneeAgentId": None, "assigneeUserId": None,
                "prAuthorAgentId": "agent-a",
                "prAuthorProvenance": {"source": "review-request",
                                       "ref": "issue-comment:author-declaration"},
                "prUrl": "https://github.com/o/r/pull/9",
                "assignedSince": "2026-10-03T12:00:00Z",
                "updatedAt": "2026-10-03T12:00:00Z"}
        card.update(overrides)
        return card

    def stale_reviews(self, records):
        return [r for r in records
                if r.get("detector") == "watchdog/stale_reviews"]

    def test_unassigned_stale_fires_with_director_proposal(self):
        records = detect(snap(reviews=[self.review()]))
        hits = [r for r in self.stale_reviews(records)
                if r.get("type") == "finding"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["reason"], "stale review with no owner")
        self.assertEqual(hits[0]["severity"], "high")
        self.assertEqual(hits[0]["owner"], "director")
        self.assertEqual(hits[0]["route"], "director")
        proposals = [r for r in self.stale_reviews(records)
                     if r.get("type") == "proposal"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["mutation"], "none")

    def test_author_owned_stale_fires(self):
        records = detect(snap(reviews=[
            self.review(assigneeAgentId="agent-a")]))
        self.assertIn("stale review owned by the PR author",
                      reasons(records, "stale_reviews"))

    def test_user_held_stale_fires(self):
        records = detect(snap(reviews=[
            self.review(assigneeAgentId="agent-x",
                        assigneeUserId="user-1")]))
        hits = [r for r in self.stale_reviews(records)
                if r.get("type") == "finding"]
        self.assertEqual(hits[0]["reason"],
                         "stale review held by a user identity")
        self.assertEqual(hits[0]["owner"], "director")

    def test_fresh_assignment_is_silent(self):
        records = detect(snap(reviews=[
            self.review(assignedSince="2026-10-03T13:30:00Z",
                        updatedAt="2026-10-03T13:30:00Z")]))
        self.assertEqual(reasons(records, "stale_reviews"), [])

    def test_assigned_non_author_is_silent(self):
        records = detect(snap(reviews=[
            self.review(assigneeAgentId="agent-x")]))
        self.assertEqual(reasons(records, "stale_reviews"), [])

    def test_missing_author_is_unknown_not_independent(self):
        for missing in ("absent", None, "", "   ", 7):
            with self.subTest(author=missing):
                card = self.review(assigneeAgentId="agent-r")
                if missing == "absent":
                    del card["prAuthorAgentId"]
                else:
                    card["prAuthorAgentId"] = missing
                records = self.stale_reviews(detect(snap(reviews=[card])))
                hits = [r for r in records if r["type"] == "finding"]
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["reason"],
                                 "stale review author provenance unknown")
                self.assertEqual(hits[0]["severity"], "unknown")
                self.assertEqual(hits[0]["route"], "director")
                self.assertIsNone(hits[0]["evidence"]["prAuthorAgentId"])
                self.assertEqual(hits[0]["evidence"]["authorStatus"], "unknown")
                proposals = [r for r in records if r["type"] == "proposal"]
                self.assertEqual(len(proposals), 1)
                self.assertEqual(proposals[0]["mutation"], "none")
                self.assertNotIn("assign_blocker_owner", proposals[0]["note"])

    def test_provenance_is_required_even_for_matching_author_id(self):
        for provenance in (None, {}, "review-request", {"source": []},
                           {"source": "review-request"},
                           {"source": "code-authorship", "ref": " "}):
            with self.subTest(provenance=provenance):
                card = self.review(assigneeAgentId="agent-a",
                                   prAuthorProvenance=provenance)
                records = self.stale_reviews(detect(snap(reviews=[card])))
                hit = next(r for r in records if r["type"] == "finding")
                self.assertEqual(hit["severity"], "unknown")
                self.assertNotIn("stale review owned by the PR author",
                                 reasons(records, "stale_reviews"))

    def test_proxy_identities_never_prove_code_authorship(self):
        for source in ("github-bot-login", "review-card-creator",
                       "parent-owner", "work-product-publisher"):
            with self.subTest(source=source):
                card = self.review(assigneeAgentId="agent-r",
                                   prAuthorAgentId="agent-r",
                                   prAuthorProvenance={"source": source,
                                                       "ref": "publisher-run:r"},
                                   createdByAgentId="agent-r",
                                   parentAssigneeAgentId="agent-r",
                                   publisherAgentId="agent-r",
                                   githubLogin="shared-bot")
                records = self.stale_reviews(detect(snap(reviews=[card])))
                hit = next(r for r in records if r["type"] == "finding")
                self.assertEqual(hit["severity"], "unknown")
                self.assertIsNone(hit["evidence"]["prAuthorAgentId"])

    def test_source_declared_independent_author_ignores_publisher_match(self):
        for source in ("review-request", "code-authorship"):
            with self.subTest(source=source):
                card = self.review(assigneeAgentId="agent-r",
                                   prAuthorProvenance={"source": source,
                                                       "ref": "source:author-a"},
                                   createdByAgentId="agent-r",
                                   parentAssigneeAgentId="agent-r",
                                   publisherAgentId="agent-r")
                self.assertEqual(self.stale_reviews(detect(snap(reviews=[card]))), [])

    def test_unknown_author_repeat_does_not_duplicate_confirmation(self):
        card = self.review(assigneeAgentId="agent-r", prAuthorAgentId=None)
        records = self.stale_reviews(detect(snap(
            reviews=[card], openProposalSignatures=["TOG-R:author-unknown"])))
        self.assertEqual([r for r in records if r["type"] == "proposal"], [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reason"], "stale review already proposed")
        self.assertEqual(records[0]["evidence"]["authorStatus"], "unknown")

    def test_unknown_author_fresh_or_non_review_is_silent(self):
        for overrides in ({"assignedSince": "2026-10-03T13:30:00Z"},
                          {"isReview": False}):
            with self.subTest(overrides=overrides):
                card = self.review(assigneeAgentId="agent-r",
                                   prAuthorAgentId=None, **overrides)
                self.assertEqual(self.stale_reviews(detect(snap(reviews=[card]))), [])

    def test_unknown_author_unreadable_clock_is_not_fresh(self):
        card = self.review(assigneeAgentId="agent-r", prAuthorAgentId=None,
                           assignedSince=None, updatedAt=None)
        records = self.stale_reviews(detect(snap(reviews=[card])))
        self.assertEqual(records[0]["reason"],
                         "stale-review candidate with unreadable ownership clock")
        self.assertEqual(records[0]["severity"], "unknown")

    def test_unassigned_unknown_author_has_one_ownership_proposal(self):
        records = self.stale_reviews(detect(snap(
            reviews=[self.review(prAuthorAgentId=None)])))
        self.assertEqual(len([r for r in records if r["type"] == "proposal"]), 1)
        hit = next(r for r in records if r["type"] == "finding")
        self.assertEqual(hit["reason"], "stale review with no owner")
        self.assertEqual(hit["evidence"]["authorStatus"], "unknown")

    def test_non_review_card_is_silent(self):
        card = self.review(isReview=False)
        records = detect(snap(reviews=[card]))
        self.assertEqual(reasons(records, "stale_reviews"), [])

    def test_repeat_poll_does_not_duplicate(self):
        card = self.review()
        first = detect(snap(reviews=[card]))
        self.assertEqual(len([r for r in self.stale_reviews(first)
                              if r.get("type") == "proposal"]), 1)
        second = detect(snap(
            reviews=[card],
            openProposalSignatures=["TOG-R:unassigned"]))
        proposals = [r for r in self.stale_reviews(second)
                     if r.get("type") == "proposal"]
        self.assertEqual(proposals, [])
        repeats = [r for r in self.stale_reviews(second)
                   if r.get("reason") == "stale review already proposed"]
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["severity"], "info")

    def test_changed_kind_is_a_new_signature(self):
        card = self.review(assigneeAgentId="agent-a")
        records = detect(snap(
            reviews=[card],
            openProposalSignatures=["TOG-R:unassigned"]))
        proposals = [r for r in self.stale_reviews(records)
                     if r.get("type") == "proposal"]
        self.assertEqual(len(proposals), 1)
        self.assertIn("author-owned",
                      proposals[0]["reason"])

    def test_missing_clock_is_unknown(self):
        card = self.review(assignedSince=None, updatedAt=None)
        records = detect(snap(reviews=[card]))
        self.assertIn("stale-review candidate with unreadable ownership clock",
                      reasons(records, "stale_reviews"))

    def test_missing_section_is_unknown(self):
        s = snap()
        del s["reviews"]
        records = detect(s)
        self.assertIn("reviews snapshot missing",
                      reasons(records, "stale_reviews"))


class QuotaExpiryTest(unittest.TestCase):
    def test_both_thresholds_fire_high_with_none_proposal(self):
        records = detect(snap(quotaLanes=[
            {"lane": "Muse-weekly-1", "hoursToReset": 24,
             "headroomPct": 10}]))
        hits = [r for r in records
                if r.get("detector") == "watchdog/quota_expiry"]
        self.assertEqual(len(hits), 2)
        finding = [r for r in hits if r.get("type") == "finding"][0]
        proposal = [r for r in hits if r.get("type") == "proposal"][0]
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["owner"], "cto")
        self.assertEqual(finding["route"], "cto")
        self.assertEqual(proposal["mutation"], "none")

    def test_hours_at_threshold_is_silent(self):
        records = detect(snap(quotaLanes=[
            {"lane": "Muse-weekly-1", "hoursToReset": 48,
             "headroomPct": 10}]))
        self.assertEqual(reasons(records, "quota_expiry"), [])

    def test_headroom_at_threshold_is_silent(self):
        records = detect(snap(quotaLanes=[
            {"lane": "Muse-weekly-1", "hoursToReset": 24,
             "headroomPct": 25}]))
        self.assertEqual(reasons(records, "quota_expiry"), [])

    def test_either_threshold_met_is_silent(self):
        records = detect(snap(quotaLanes=[
            {"lane": "Muse-weekly-1", "hoursToReset": 72,
             "headroomPct": 10}]))
        self.assertEqual(reasons(records, "quota_expiry"), [])

    def test_non_claude_lane_is_silent(self):
        records = detect(snap(quotaLanes=[
            {"lane": "codex-weekly-1", "hoursToReset": 1,
             "headroomPct": 1}]))
        self.assertEqual(reasons(records, "quota_expiry"), [])

    def test_missing_section_is_unknown(self):
        s = snap()
        del s["quotaLanes"]
        records = detect(s)
        hits = [r for r in records
                if r.get("detector") == "watchdog/quota_expiry"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "unknown")

    def test_empty_lanes_are_silent(self):
        self.assertEqual(reasons(detect(snap()), "quota_expiry"), [])

    def test_unreadable_numbers_are_unknown(self):
        records = detect(snap(quotaLanes=[
            {"lane": "Muse-weekly-1", "hoursToReset": "soon",
             "headroomPct": 10}]))
        hits = [r for r in records
                if r.get("detector") == "watchdog/quota_expiry"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "unknown")

class PendingInteractionsTest(unittest.TestCase):
    def stale(self, **overrides):
        item = {"issueId": "iid-7", "identifier": "case_4ee01500",
                "interactionId": "int-1", "kind": "request_confirmation",
                "createdAt": "2026-10-03T13:00:00Z"}
        item.update(overrides)
        return item

    def test_stale_with_resolver_wakes_resolver(self):
        records = detect(snap(pendingInteractions=[
            self.stale(resolverAgentId="agent-r")]))
        hits = [r for r in records
                if r.get("reason") == "pending interaction older than "
                "30m with addressed resolver"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "info")
        self.assertEqual(hits[0]["owner"], "agent-r")
        self.assertEqual(hits[0]["route"], "agent-r")

    def test_stale_without_resolver_routes_to_ceo_desk(self):
        records = detect(snap(pendingInteractions=[self.stale()]))
        hits = [r for r in records
                if r.get("reason") == "pending interaction older than "
                "30m with no clear resolver"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["owner"], "ceo")
        self.assertEqual(hits[0]["route"], "ceo-desk")
        self.assertIn("case_d36e8c4a", hits[0]["suggestedAction"])

    def test_under_threshold_fires_metric_only(self):
        records = detect(snap(pendingInteractions=[
            self.stale(createdAt="2026-10-03T13:45:00Z",
                       resolverAgentId="agent-r")]))
        self.assertEqual(reasons(records, "pending_interactions"),
                         ["pending-interaction backlog metric"])
        per_item = [r for r in records
                    if r.get("reason", "").startswith("pending interaction")]
        self.assertEqual(per_item, [])

    def test_over_2h_is_high_and_metric_alerts(self):
        records = detect(snap(pendingInteractions=[
            self.stale(createdAt="2026-10-03T11:00:00Z",
                       resolverAgentId="agent-r"),
            self.stale(interactionId="int-2",
                       createdAt="2026-10-03T13:00:00Z")]))
        per_item = [r for r in records
                    if r.get("type") == "finding"
                    and r.get("reason", "").startswith("pending interaction")]
        by_int = {r["evidence"]["interactionId"]: r for r in per_item}
        self.assertEqual(by_int["int-1"]["severity"], "high")
        self.assertEqual(by_int["int-2"]["severity"], "info")
        metric = [r for r in records
                  if r.get("reason") == "pending-interaction backlog metric"]
        self.assertEqual(len(metric), 1)
        self.assertEqual(metric[0]["severity"], "high")
        self.assertEqual(metric[0]["evidence"]["count"], 2)
        self.assertEqual(metric[0]["evidence"]["over2h"], 1)

    def test_metric_carries_count_and_median(self):
        records = detect(snap(pendingInteractions=[
            self.stale(createdAt="2026-10-03T13:00:00Z",
                       resolverAgentId="agent-r"),
            self.stale(interactionId="int-2",
                       createdAt="2026-10-03T13:30:00Z",
                       resolverAgentId="agent-r")]))
        metric = [r for r in records
                  if r.get("reason") == "pending-interaction backlog metric"]
        self.assertEqual(len(metric), 1)
        self.assertEqual(metric[0]["severity"], "info")
        self.assertEqual(metric[0]["evidence"]["count"], 2)
        self.assertEqual(metric[0]["evidence"]["medianAgeMin"], 45.0)
        self.assertEqual(metric[0]["evidence"]["over2h"], 0)

    def test_stale_emits_none_mutation_proposal(self):
        records = detect(snap(pendingInteractions=[
            self.stale(resolverAgentId="agent-r")]))
        proposals = [r for r in records
                     if r.get("detector") == "watchdog/pending_interactions"
                     and r.get("type") == "proposal"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["mutation"], "none")

    def test_missing_section_is_unknown(self):
        s = snap()
        del s["pendingInteractions"]
        records = detect(s)
        self.assertIn("pending-interactions snapshot missing",
                      reasons(records, "pending_interactions"))


class SupplyFamineTest(unittest.TestCase):
    def famine(self, **overrides):
        supply = {"readyNow": 0, "target": 12, "belowMin": 35.0,
                  "idleAgents": 2, "censusAt": "2026-10-03T14:00:00Z"}
        supply.update(overrides)
        return supply

    def test_below_target_sustained_fires_high_with_proposal(self):
        records = detect(snap(supply=self.famine()))
        hits = [r for r in records
                if r.get("detector") == "watchdog/supply_famine"
                and r.get("type") == "finding"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "high")
        self.assertEqual(hits[0]["owner"], "ceo")
        self.assertEqual(hits[0]["route"], "ceo-hourly-routine")
        self.assertIn("case_d36e8c4a", hits[0]["suggestedAction"])
        proposals = [r for r in records
                     if r.get("detector") == "watchdog/supply_famine"
                     and r.get("type") == "proposal"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["mutation"], "none")

    def test_below_target_under_30m_is_info_to_coo(self):
        records = detect(snap(supply=self.famine(belowMin=10.0)))
        hits = [r for r in records
                if r.get("detector") == "watchdog/supply_famine"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "info")
        self.assertEqual(hits[0]["owner"], "coo")

    def test_at_target_is_silent(self):
        records = detect(snap(supply=self.famine(readyNow=12)))
        self.assertEqual(reasons(records, "supply_famine"), [])

    def test_missing_section_is_unknown(self):
        s = snap()
        del s["supply"]
        records = detect(s)
        self.assertIn("supply snapshot missing",
                      reasons(records, "supply_famine"))


class AutoscalerSlicesTest(unittest.TestCase):
    def idle_slice(self, **overrides):
        item = {"sliceId": "slice-1", "identifier": "case_9ae341a0",
                "status": "backlog", "assigneeAgentId": None,
                "unassignedSince": "2026-10-03T11:00:00Z"}
        item.update(overrides)
        return item

    def test_unassigned_over_2h_fires_high_with_proposal(self):
        records = detect(snap(autoscalerSlices=[self.idle_slice()]))
        hits = [r for r in records
                if r.get("detector") == "watchdog/autoscaler_slices"
                and r.get("type") == "finding"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "high")
        self.assertEqual(hits[0]["owner"], "coo")
        self.assertEqual(hits[0]["route"], "ceo-hourly-routine")
        self.assertIn("over 2h", hits[0]["reason"])
        self.assertEqual(hits[0]["evidence"]["ageMin"], 180.0)
        proposals = [r for r in records
                     if r.get("detector") == "watchdog/autoscaler_slices"
                     and r.get("type") == "proposal"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["mutation"], "none")

    def test_assigned_slice_is_silent(self):
        records = detect(snap(autoscalerSlices=[
            self.idle_slice(assigneeAgentId="agent-a")]))
        self.assertEqual(reasons(records, "autoscaler_slices"), [])

    def test_fresh_slice_is_silent(self):
        records = detect(snap(autoscalerSlices=[
            self.idle_slice(unassignedSince="2026-10-03T13:30:00Z")]))
        self.assertEqual(reasons(records, "autoscaler_slices"), [])

    def test_at_threshold_is_silent(self):
        records = detect(snap(autoscalerSlices=[
            self.idle_slice(unassignedSince="2026-10-03T12:00:00Z")]))
        self.assertEqual(reasons(records, "autoscaler_slices"), [])

    def test_missing_section_is_unknown(self):
        s = snap()
        self.assertNotIn("autoscalerSlices", s)
        self.assertIn("autoscaler-slices snapshot missing",
                      reasons(detect(s), "autoscaler_slices"))

    def test_unreadable_since_is_unknown(self):
        records = detect(snap(autoscalerSlices=[
            self.idle_slice(unassignedSince="not-a-time")]))
        self.assertIn("autoscaler slice with unreadable unassignedSince",
                      reasons(records, "autoscaler_slices"))


class ClockTest(unittest.TestCase):
    """case_988ba6e5: `now` is the only clock a recorded snapshot may be aged
    against, and a row dated after it is unknown, never a negative age."""

    STALE = {
        "agents": [{"agentId": "a1", "status": "error",
                    "errorSince": "2026-10-03T13:00:00Z",
                    "subtype": "crash", "owningLead": "cto"}],
        "pendingInteractions": [{
            "issueId": "iid-7", "identifier": "case_4ee01500",
            "interactionId": "int-1", "kind": "request_confirmation",
            "createdAt": "2026-10-03T11:30:00Z",
            "resolverAgentId": "agent-r"}],
        "autoscalerSlices": [{
            "sliceId": "slice-1", "identifier": "case_9ae341a0",
            "status": "backlog", "assigneeAgentId": None,
            "unassignedSince": "2026-10-03T11:00:00Z"}],
        "runEvents": [{"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                       "kind": "dispatch_stalled_issue",
                       "at": f"2026-10-03T13:{minute}:00Z"}
                      for minute in (10, 20, 30, 40)],
        "shadowEmit": {"lastDecisionAt": "2026-10-03T12:00:00Z"},
        "reviews": [{
            "issueId": "iid-r", "identifier": "TOG-R", "status": "in_review",
            "isReview": True, "assigneeAgentId": None,
            "assigneeUserId": None, "prAuthorAgentId": "agent-a",
            "prUrl": "https://github.com/o/r/pull/9",
            "assignedSince": "2026-10-03T12:00:00Z",
            "updatedAt": "2026-10-03T12:00:00Z"}],
        "assignedInProgress": [{
            "issueId": "iid-4", "identifier": "case_4ac9f045",
            "status": "in_progress", "assigneeAgentId": "agent-s",
            "newestComment": {
                "body": "API Error: Request rejected (429) rate limit",
                "createdAt": "2026-10-03T13:00:00Z"},
            "liveRunCount": 0, "queuedRunCount": 0,
            "unresolvedBlockerCount": 0, "activeRecoveryAction": None,
            "monitorNextCheckAt": None}],
        "agentRuns": [{"agentId": "agent-s",
                       "lastSucceededAt": "2026-10-03T13:55:00Z"}],
    }
    AGE_DETECTORS = ("agent_error", "pending_interactions",
                     "autoscaler_slices", "churn", "shadow_emit_gap",
                     "stale_reviews", "stranded_429")

    @staticmethod
    def clean(**overrides):
        base = {"hosts": [{"host": "192.0.2.190", "diskPct": 10,
                           "load": 1.0, "vcpu": 4, "sustainedMin": 1}],
                "autoscalerSlices": [],
                # Main's org-standard detector is silent on this fixture so
                # the clock assertions below isolate clock behavior.
                "orgStandard": org_snap()["orgStandard"]}
        base.update(overrides)
        return snap(**base)

    @staticmethod
    @contextlib.contextmanager
    def wall_clock(moment):
        class Frozen(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(
                    tzinfo=None)

        fake = types.SimpleNamespace(datetime=Frozen,
                                     timezone=datetime.timezone,
                                     timedelta=datetime.timedelta)
        with mock.patch.object(detectors, "datetime", fake):
            yield

    @staticmethod
    def run_main(snapshot):
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "snapshot.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--snapshot", path])
        assert code == 0
        return out.getvalue()

    def unknowns(self, records, detector):
        return [r for r in records
                if r.get("detector") == f"watchdog/{detector}"
                and r.get("severity") == "unknown"]

    def test_stale_rows_fire_every_age_detector_at_snapshot_now(self):
        # Positive control: the rows below are what the garbage-now tests
        # must NOT age against the wall clock.
        records = detect(self.clean(**self.STALE))
        for family in self.AGE_DETECTORS:
            self.assertTrue(
                [r for r in records
                 if r.get("detector") == f"watchdog/{family}"
                 and r.get("severity") == "high"], family)

    def test_present_but_unreadable_now_is_one_unknown_never_wallclock(self):
        for bad in ("not-a-time", None, "", 1759500000, ["x"]):
            with self.subTest(now=bad):
                snapshot = self.clean(**self.STALE)
                snapshot["now"] = bad
                # The wall clock sits where the rows WOULD age into alerts.
                with self.wall_clock(NOW):
                    records = detect(snapshot)
                self.assertEqual(
                    [(r["detector"], r["reason"], r.get("severity"))
                     for r in records],
                    [("watchdog/snapshot_clock", "snapshot now unreadable",
                      "unknown")])
                self.assertEqual(records[0]["evidence"]["field"], "now")
                self.assertEqual(now_source(snapshot), "unreadable")

    def test_unreadable_now_does_not_echo_the_value(self):
        snapshot = self.clean(now="ghp_" + "A1b2C3d4E5f6G7h8I9j0")
        records = detect(snapshot)
        self.assertEqual(records[0]["evidence"],
                         {"field": "now", "valueType": "str"})

    def test_unreadable_now_keeps_independent_unknowns_and_parks(self):
        snapshot = self.clean(
            parks=[{"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                    "missingDispositionCount": 4}])
        snapshot["now"] = "not-a-time"
        del snapshot["agents"]
        records = detect(snapshot)
        self.assertEqual(
            sorted(r["reason"] for r in records if r["type"] == "finding"),
            ["agents snapshot missing",
             "card parked for missing disposition more than twice",
             "snapshot now unreadable"])

    def test_unreadable_now_keeps_churn_section_unknowns(self):
        # No clock means no event windowing, not no section verdict: churn
        # still reports its own unreadable runEvents section and rows.
        for events, reason in (
                ({"a": 1}, "runEvents snapshot unreadable"),
                ([1, 2], "runEvents rows unreadable")):
            with self.subTest(reason=reason):
                snapshot = self.clean(runEvents=events)
                snapshot["now"] = "not-a-time"
                self.assertIn(reason, reasons(detect(snapshot), "churn"))
                # Same snapshot under a readable clock: identical churn verdict.
                snapshot["now"] = "2026-10-03T14:00:00Z"
                self.assertIn(reason, reasons(detect(snapshot), "churn"))

    def test_unreadable_now_never_windows_valid_churn_events(self):
        snapshot = self.clean(runEvents=self.STALE["runEvents"])
        snapshot["now"] = None
        self.assertEqual(reasons(detect(snapshot), "churn"), [])

    def test_row_with_unreadable_stamp_stays_unknown_without_a_clock(self):
        snapshot = self.clean(autoscalerSlices=[{
            "sliceId": "s", "identifier": "case_8c5fe4cc", "assigneeAgentId": None,
            "unassignedSince": "not-a-time"}])
        snapshot["now"] = None
        self.assertIn("autoscaler slice with unreadable unassignedSince",
                      reasons(detect(snapshot), "autoscaler_slices"))

    def test_absent_now_keeps_wallclock_and_summary_says_which_clock(self):
        snapshot = self.clean(**self.STALE)
        del snapshot["now"]
        with self.wall_clock(NOW):
            records = detect(snapshot)
            summary = json.loads(self.run_main(snapshot).splitlines()[-1])
        for family in self.AGE_DETECTORS:
            self.assertTrue(
                [r for r in records
                 if r.get("detector") == f"watchdog/{family}"
                 and r.get("severity") == "high"], family)
        self.assertEqual(summary["nowSource"], "wallclock")
        for now, source in (("2026-10-03T14:00:00Z", "snapshot"),
                            ("not-a-time", "unreadable"), (None, "unreadable")):
            snapshot["now"] = now
            summary = json.loads(self.run_main(snapshot).splitlines()[-1])
            self.assertEqual(summary["nowSource"], source)
            self.assertEqual(summary["type"], "summary")
            self.assertEqual(summary["phase"], detectors.PHASE)

    def test_same_snapshot_is_byte_identical_under_two_wall_clocks(self):
        garbage = self.clean(**self.STALE)
        garbage["now"] = "not-a-time"
        snapshots = {"snapshot-now": self.clean(**self.STALE),
                     "unreadable-now": garbage}
        for label, snapshot in snapshots.items():
            with self.subTest(label):
                with self.wall_clock(NOW):
                    first = self.run_main(snapshot)
                with self.wall_clock(NOW + datetime.timedelta(days=200)):
                    second = self.run_main(snapshot)
                self.assertEqual(first, second)
        # The patch is live: without `now` the two clocks DO disagree.
        undated = self.clean(**self.STALE)
        del undated["now"]
        with self.wall_clock(NOW):
            first = self.run_main(undated)
        with self.wall_clock(NOW + datetime.timedelta(days=200)):
            second = self.run_main(undated)
        self.assertNotEqual(first, second)

    def future_cases(self):
        """detector -> builder(ts) of a single row dated `ts`."""
        return {
            "agent_error": lambda ts: {"agents": [{
                "agentId": "a1", "status": "error", "errorSince": ts,
                "subtype": "crash", "owningLead": "cto"}]},
            "pending_interactions": lambda ts: {"pendingInteractions": [{
                "issueId": "iid-7", "identifier": "case_4ee01500",
                "interactionId": "int-1", "kind": "request_confirmation",
                "createdAt": ts, "resolverAgentId": "agent-r"}]},
            "autoscaler_slices": lambda ts: {"autoscalerSlices": [{
                "sliceId": "slice-1", "identifier": "case_9ae341a0",
                "status": "backlog", "assigneeAgentId": None,
                "unassignedSince": ts}]},
            "churn": lambda ts: {"runEvents": [{
                "issueId": "iid-1", "identifier": "case_8c5fe4cc",
                "kind": "dispatch_stalled_issue", "at": ts}]},
            "shadow_emit_gap": lambda ts: {
                "shadowEmit": {"lastDecisionAt": ts}},
            "stale_reviews": lambda ts: {"reviews": [dict(
                self.STALE["reviews"][0], assignedSince=ts, updatedAt=ts)]},
            "stranded_429": lambda ts: {"assignedInProgress": [dict(
                self.STALE["assignedInProgress"][0], newestComment={
                    "body": "API Error: Request rejected (429) rate limit",
                    "createdAt": ts})]},
        }

    def test_future_dated_boundary_is_plus_five_minutes_exactly(self):
        self.assertEqual(FUTURE_SKEW_MIN, 5)
        at_edge = NOW + datetime.timedelta(minutes=FUTURE_SKEW_MIN)
        past_edge = at_edge + datetime.timedelta(seconds=1)
        stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        for family, build in self.future_cases().items():
            with self.subTest(family, edge="+5m"):
                records = detect(self.clean(**build(stamp(at_edge))))
                self.assertEqual(self.unknowns(records, family), [])
            with self.subTest(family, edge="+5m1s"):
                records = detect(self.clean(**build(stamp(past_edge))))
                found = self.unknowns(records, family)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["evidence"]["count"], 1)
                self.assertIn("future-dated", found[0]["reason"])
                self.assertEqual(found[0]["route"], "ceo-hourly-routine")

    def test_future_dated_rows_never_read_healthy_or_negative(self):
        for family, build in self.future_cases().items():
            for ts in ("2026-10-04T14:00:00Z", "2026-10-05T14:00:00Z"):
                with self.subTest(family, ts=ts):
                    records = detect(self.clean(**build(ts)))
                    self.assertEqual(len(self.unknowns(records, family)), 1)
                    for record in records:
                        age = (record.get("evidence") or {}).get("ageMin")
                        self.assertTrue(age is None or age >= 0, record)

    def test_future_dated_rows_collapse_to_one_finding_with_the_count(self):
        build = self.future_cases()["agent_error"]
        rows = [build("2026-10-04T14:00:00Z")["agents"][0]
                for _ in range(3)]
        found = self.unknowns(detect(self.clean(agents=rows)), "agent_error")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["evidence"]["count"], 3)
        self.assertEqual(found[0]["evidence"]["field"], "errorSince")

    def test_future_dated_pending_row_is_out_of_the_median(self):
        rows = [
            {"issueId": "iid-7", "identifier": "case_4ee01500",
             "interactionId": f"int-{n}", "kind": "request_confirmation",
             "createdAt": created, "resolverAgentId": "agent-r"}
            for n, created in enumerate(("2026-10-03T13:00:00Z",
                                         "2026-10-05T14:00:00Z"))]
        records = detect(self.clean(pendingInteractions=rows))
        metric = [r for r in records
                  if r["reason"] == "pending-interaction backlog metric"]
        self.assertEqual(len(metric), 1)
        self.assertEqual(metric[0]["evidence"]["count"], 1)
        self.assertEqual(metric[0]["evidence"]["medianAgeMin"], 60.0)
        self.assertEqual(len(self.unknowns(records, "pending_interactions")),
                         1)

    def test_skew_inside_tolerance_reads_as_age_zero(self):
        records = detect(self.clean(pendingInteractions=[{
            "issueId": "iid-7", "identifier": "case_4ee01500",
            "interactionId": "int-1", "kind": "request_confirmation",
            "createdAt": "2026-10-03T14:03:00Z",
            "resolverAgentId": "agent-r"}]))
        metric = [r for r in records
                  if r["reason"] == "pending-interaction backlog metric"]
        self.assertEqual(metric[0]["evidence"]["medianAgeMin"], 0.0)

    def test_future_dated_stalls_do_not_count_toward_the_rewoke_burst(self):
        events = [{"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                   "kind": "dispatch_stalled_issue",
                   "at": "2026-10-04T13:00:00Z"}] * 4
        records = detect(self.clean(runEvents=events))
        self.assertEqual(reasons(records, "churn"),
                         ["4 row(s) with future-dated runEvents.at"])

    def test_unreadable_now_is_one_unknown_for_each_late_added_detector(self):
        # Each of these once did `now - moment` on the None clock and raised
        # TypeError, which stops every detector family for the cycle.
        for family in ("shadow_emit_gap", "stale_reviews", "stranded_429"):
            build = {
                "shadow_emit_gap": {"shadowEmit": self.STALE["shadowEmit"]},
                "stale_reviews": {"reviews": self.STALE["reviews"]},
                "stranded_429": {
                    "assignedInProgress": self.STALE["assignedInProgress"],
                    "agentRuns": self.STALE["agentRuns"]},
            }[family]
            for bad in ("not-a-time", None):
                with self.subTest(family, now=bad):
                    snapshot = self.clean(**build)
                    positive = detect(snapshot)
                    self.assertTrue(
                        [r for r in positive
                         if r.get("detector") == f"watchdog/{family}"
                         and r.get("severity") == "high"], family)
                    snapshot["now"] = bad
                    records = detect(snapshot)
                    self.assertEqual(
                        [(r["detector"], r["reason"]) for r in records],
                        [("watchdog/snapshot_clock",
                          "snapshot now unreadable")])
        text = detect(self.clean(now="not-a-time"))[0]["suggestedAction"]
        for family in self.AGE_DETECTORS:
            self.assertIn(family, text)

    def test_future_dated_lane_success_is_unknown_not_a_silent_skip(self):
        base = self.STALE
        stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        edge = NOW + datetime.timedelta(minutes=FUTURE_SKEW_MIN)
        for label, moment, expect_unknown in (
                ("+5m", edge, False),
                ("+5m1s", edge + datetime.timedelta(seconds=1), True)):
            with self.subTest(label):
                records = detect(self.clean(
                    assignedInProgress=base["assignedInProgress"],
                    agentRuns=[{"agentId": "agent-s",
                                "lastSucceededAt": stamp(moment)}]))
                found = self.unknowns(records, "stranded_429")
                self.assertEqual(len(found), 1 if expect_unknown else 0)
                if expect_unknown:
                    self.assertEqual(found[0]["evidence"]["field"],
                                     "lastSucceededAt")
                    self.assertEqual(found[0]["evidence"]["count"], 1)
                    self.assertEqual(
                        [r for r in records if r.get("severity") == "high"],
                        [])
                else:
                    self.assertEqual(
                        len([r for r in records if r.get("severity") == "high"
                             and r["detector"] == "watchdog/stranded_429"]),
                        1)

    def test_future_dated_shadow_and_review_rows_never_read_healthy(self):
        # A negative gap/age used to compare <= the threshold and read quiet.
        shadow = detect(self.clean(
            shadowEmit={"lastDecisionAt": "2026-10-05T14:00:00Z"}))
        found = self.unknowns(shadow, "shadow_emit_gap")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["owner"], "cto")
        self.assertEqual(found[0]["evidence"]["field"], "lastDecisionAt")
        reviews = detect(self.clean(reviews=[
            dict(self.STALE["reviews"][0], assignedSince=None,
                 updatedAt="2026-10-05T14:00:00Z")]))
        found = self.unknowns(reviews, "stale_reviews")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["owner"], "director")
        self.assertEqual(found[0]["evidence"]["count"], 1)

    def test_future_dated_review_cards_collapse_to_one_finding(self):
        cards = [dict(self.STALE["reviews"][0], identifier=f"TOG-{n}",
                      issueId=f"iid-{n}", assignedSince="2026-10-05T14:00:00Z")
                 for n in range(3)]
        found = self.unknowns(detect(self.clean(reviews=cards)),
                              "stale_reviews")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["evidence"]["count"], 3)


class ShadowEmitGapTest(unittest.TestCase):
    def test_gap_past_30m_fires_high_to_cto(self):
        records = detect(snap(shadowEmit={
            "lastDecisionAt": "2026-10-03T13:00:00Z"}))
        hits = [r for r in records
                if r.get("detector") == "watchdog/shadow_emit_gap"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "high")
        self.assertEqual(hits[0]["owner"], "cto")
        self.assertEqual(hits[0]["route"], "cto")
        self.assertEqual(
            len([r for r in records if r.get("type") == "proposal"
                 and r.get("detector") == "watchdog/shadow_emit_gap"]), 0)

    def test_gap_at_threshold_is_silent(self):
        records = detect(snap(shadowEmit={
            "lastDecisionAt": "2026-10-03T13:30:00Z"}))
        self.assertEqual(reasons(records, "shadow_emit_gap"), [])

    def test_fresh_emit_is_silent(self):
        records = detect(snap(shadowEmit={
            "lastDecisionAt": "2026-10-03T13:55:00Z"}))
        self.assertEqual(reasons(records, "shadow_emit_gap"), [])

    def test_missing_section_is_silent(self):
        self.assertEqual(reasons(detect(snap()), "shadow_emit_gap"), [])

    def test_unreadable_timestamp_is_unknown(self):
        records = detect(snap(shadowEmit={"lastDecisionAt": "not-a-time"}))
        hits = [r for r in records
                if r.get("detector") == "watchdog/shadow_emit_gap"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "unknown")


def org_snap(**overrides):
    section = {"canonical": {"templateSha": "t", "agentsSha": "a",
                             "contributingSha": "c"},
               "repos": [{"repo": "two-bot-next",
                           "templateSha": "t", "agentsSha": "a",
                           "contributingSha": "c",
                           "inheritsOrgDefault": False,
                           "requiresPrLint": True, "squashOnly": True}]}
    section.update(overrides)
    return snap(orgStandard=section)


class OrgStandardDriftTest(unittest.TestCase):
    def test_template_drift_fires_high_with_proposal(self):
        s = org_snap(repos=[{
            "repo": "two-web-next",
            "templateSha": "stale", "agentsSha": "a",
            "contributingSha": "c",
            "inheritsOrgDefault": False, "requiresPrLint": True, "squashOnly": True}])
        records = detect(s)
        hits = [r for r in records
                if r.get("type") == "finding"
                and r.get("detector") == "watchdog/org_standard"
                and r.get("reason") == "repo template bundle drifted from canonical"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "high")
        self.assertEqual(hits[0]["owner"], "director")
        self.assertEqual(hits[0]["evidence"]["driftedFiles"],
                         ["templateSha"])
        proposals = [r for r in records if r.get("type") == "proposal"
                     and r.get("detector") == "watchdog/org_standard"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["mutation"], "none")

    def test_clean_repo_is_silent(self):
        self.assertEqual(reasons(detect(org_snap()), "org_standard"), [])

    def test_missing_section_is_unknown(self):
        s = snap()
        self.assertNotIn("orgStandard", s)
        self.assertIn("org-standard snapshot missing",
                      reasons(detect(s), "org_standard"))

    def test_inherited_org_default_covers_absence(self):
        s = org_snap(repos=[{
            "repo": "two-web-next",
            "templateSha": None, "agentsSha": None,
            "contributingSha": None,
            "inheritsOrgDefault": True, "requiresPrLint": True, "squashOnly": True}])
        self.assertEqual(reasons(detect(s), "org_standard"), [])

    def test_absent_file_without_inheritance_fires(self):
        s = org_snap(repos=[{
            "repo": "two-web-next",
            "templateSha": None, "agentsSha": "a",
            "contributingSha": "c",
            "inheritsOrgDefault": False, "requiresPrLint": True, "squashOnly": True}])
        records = detect(s)
        hits = [r for r in records
                if r.get("reason") == "repo template bundle drifted from canonical"]
        self.assertEqual(hits[0]["evidence"]["driftedFiles"],
                         ["templateSha"])

    def test_pr_lint_not_required_fires(self):
        s = org_snap(repos=[{
            "repo": "two-web-next",
            "templateSha": "t", "agentsSha": "a",
            "contributingSha": "c",
            "inheritsOrgDefault": False,
            "requiresPrLint": False, "squashOnly": True}])
        records = detect(s)
        self.assertIn("repo does not require the conventional-title check",
                      reasons(records, "org_standard"))
        self.assertNotIn("repo allows non-squash merges",
                         reasons(records, "org_standard"))

    def test_non_squash_merge_fires(self):
        s = org_snap(repos=[{
            "repo": "two-web-next",
            "templateSha": "t", "agentsSha": "a",
            "contributingSha": "c",
            "inheritsOrgDefault": False,
            "requiresPrLint": True, "squashOnly": False}])
        records = detect(s)
        self.assertIn("repo allows non-squash merges",
                      reasons(records, "org_standard"))

    def test_no_org_ruleset_is_not_a_finding(self):
        # case_8843860f: the org ruleset was withdrawn; absence is not drift.
        entry = org_snap()["orgStandard"]["repos"][0]
        self.assertNotIn("rulesetAttached", entry)
        self.assertEqual(reasons(detect(org_snap()), "org_standard"), [])
STRANDED_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "test", "fixtures",
    "case_1017ef76-stranded-429.json")
ACTION = "one resume comment from any agent"


class Stranded429Test(unittest.TestCase):
    """case_1017ef76: assigned in-progress cards whose run ended on a 429.

    The fixture is the board as measured at 2026-10-04T20:50Z, 30 cards deep;
    these are the seven it names. Every negative case flips ONE field of a
    real row and must fall silent, while the unmodified row fires, so a
    detector that never fires cannot pass.
    """

    REAL = ("case_0e12073a", "case_a99daa6c", "case_0bf02bac", "case_1d10b79d",
            "case_89c0bb6e", "case_5cc8f0a3", "case_df697f05")
    AGE_MIN = {"case_0e12073a": 278.3, "case_a99daa6c": 212.5, "case_0bf02bac": 271.9,
               "case_1d10b79d": 279.2, "case_89c0bb6e": 227.4, "case_5cc8f0a3": 272.7,
               "case_df697f05": 279.1}
    # Assignees of the real cards, from agentRuns in the fixture.
    SLOW_LANE = "00000000-0000-4000-8000-000000000153"  # 13.7m since success

    def setUp(self):
        with open(STRANDED_FIXTURE, encoding="utf-8") as handle:
            self.fixture = json.load(handle)

    def snapshot(self, **overrides):
        base = {key: copy.deepcopy(self.fixture[key])
                for key in ("now", "assignedInProgress", "agentRuns")}
        base.update(overrides)
        return base

    def row(self, snapshot, identifier):
        return next(r for r in snapshot["assignedInProgress"]
                    if r["identifier"] == identifier)

    @staticmethod
    def of(records, kind):
        return [r for r in records
                if r.get("detector") == "watchdog/stranded_429"
                and r.get("type") == kind]

    def flagged(self, records):
        return {r["identifier"] for r in self.of(records, "finding")
                if r["severity"] == "high"}

    def unknown(self, records):
        return [r for r in self.of(records, "finding")
                if r["severity"] == "unknown"]

    def only_row_changed(self, identifier, **patch):
        """Flag set after patching one real row; the others still fire."""
        snapshot = self.snapshot()
        self.row(snapshot, identifier).update(patch)
        flagged = self.flagged(detect(snapshot))
        self.assertEqual(flagged | {identifier}, set(self.REAL))
        return identifier in flagged

    def test_fixture_is_the_seven_named_cards(self):
        self.assertEqual(
            [r["identifier"] for r in self.fixture["assignedInProgress"]],
            list(self.REAL))
        for row in self.fixture["assignedInProgress"]:
            self.assertTrue(
                row["newestComment"]["body"].startswith(
                    "API Error: Request rejected (429)"))

    def test_real_cards_each_get_one_finding_and_one_proposal(self):
        records = detect(self.snapshot())
        self.assertEqual(self.flagged(records), set(self.REAL))
        self.assertEqual(len(self.of(records, "finding")), 7)
        proposals = self.of(records, "proposal")
        self.assertEqual(len(proposals), 7)
        by_id = {r["identifier"]: r
                 for r in self.fixture["assignedInProgress"]}
        for proposal in proposals:
            row = by_id[proposal["identifier"]]
            self.assertEqual(proposal["issueId"], row["issueId"])
            self.assertEqual(proposal["mutation"], "none")
            self.assertEqual(proposal["before"], {
                "status": "in_progress",
                "assigneeAgentId": row["assigneeAgentId"],
                "ageMin": self.AGE_MIN[proposal["identifier"]]})
            self.assertEqual(proposal["after"], {"suggestedAction": ACTION})
            self.assertIn(ACTION, proposal["note"])
        for finding in self.of(records, "finding"):
            self.assertEqual(finding["severity"], "high")
            self.assertEqual(finding["owner"], "director")
            self.assertEqual(finding["route"], "ceo-hourly-routine")
            self.assertIn(ACTION, finding["suggestedAction"])
            self.assertEqual(finding["evidence"]["ageMin"],
                             self.AGE_MIN[finding["identifier"]])

    def test_output_never_echoes_the_comment_body(self):
        text = json.dumps(self.of(detect(self.snapshot()), "finding")
                          + self.of(detect(self.snapshot()), "proposal"))
        self.assertNotIn("API Error", text)
        self.assertNotIn("claude-sonnet", text)

    def test_dedupe_key_is_stable_per_episode_and_new_per_comment(self):
        first = detect(self.snapshot())
        again = detect(self.snapshot())
        self.assertEqual(first, again)
        for finding, proposal in zip(self.of(first, "finding"),
                                     self.of(first, "proposal")):
            self.assertEqual(finding["dedupeKey"], proposal["dedupeKey"])
        key = {r["identifier"]: r["dedupeKey"]
               for r in self.of(first, "finding")}
        self.assertEqual(
            key["case_0e12073a"],
            "watchdog/stranded-429:00000000-0000-4000-8000-000000000144:"
            "2026-10-04T16:11:43Z")
        self.assertEqual(len(set(key.values())), 7)
        # Stranded again after a later 429: a new episode, a new key.
        later = self.snapshot()
        self.row(later, "case_0e12073a")["newestComment"]["createdAt"] = \
            "2026-10-04T18:00:00Z"
        relabelled = {r["identifier"]: r["dedupeKey"]
                      for r in self.of(detect(later), "finding")}
        self.assertNotEqual(relabelled["case_0e12073a"], key["case_0e12073a"])
        self.assertEqual(relabelled["case_a99daa6c"], key["case_a99daa6c"])

    def test_future_monitor_is_silent_but_triggered_null_monitor_fires(self):
        # case_0e12073a really carried a triggered monitor with nextCheckAt null
        # at 20:50Z; that is not a future monitor. Its real rescheduled
        # value (21:22Z) is.
        self.assertTrue(self.only_row_changed(
            "case_0e12073a", monitorNextCheckAt=None))
        self.assertFalse(self.only_row_changed(
            "case_0e12073a", monitorNextCheckAt="2026-10-04T21:22:00.000Z"))
        # An overdue monitor will not fire by itself: still stranded.
        self.assertTrue(self.only_row_changed(
            "case_0e12073a", monitorNextCheckAt="2026-10-04T20:00:00Z"))

    def test_open_blocker_edge_is_silent(self):
        self.assertFalse(self.only_row_changed(
            "case_0bf02bac", unresolvedBlockerCount=1))

    def test_newest_comment_that_is_not_a_429_is_silent(self):
        # case_1d10b79d's real comment before the 429 (PR #561 head push).
        calm = {"id": "x", "createdAt": "2026-10-04T15:28:40Z",
                "body": "Head `5a2aa6b4` pushed on PR #561 (same branch)."}
        self.assertFalse(self.only_row_changed(
            "case_1d10b79d", newestComment=calm))
        # Only a body that STARTS with the rejection counts.
        mid = dict(calm, body="Earlier: API Error: Request rejected (429) x")
        self.assertFalse(self.only_row_changed(
            "case_1d10b79d", newestComment=mid))
        other = dict(calm, body="API Error: Request rejected (500) boom")
        self.assertFalse(self.only_row_changed(
            "case_1d10b79d", newestComment=other))
        # Leading whitespace is tolerated.
        padded = dict(self.row(self.snapshot(), "case_1d10b79d")[
            "newestComment"])
        padded["body"] = "\n  " + padded["body"]
        self.assertTrue(self.only_row_changed(
            "case_1d10b79d", newestComment=padded))
        self.assertFalse(self.only_row_changed("case_1d10b79d",
                                               newestComment=None))

    def test_assignee_without_a_recent_success_is_silent(self):
        for stamp in (None, "2026-10-04T20:34:59Z", "2026-10-04T16:00:00Z"):
            with self.subTest(lastSucceededAt=stamp):
                snapshot = self.snapshot()
                for run in snapshot["agentRuns"]:
                    if run["agentId"] == self.SLOW_LANE:
                        run["lastSucceededAt"] = stamp
                records = detect(snapshot)
                self.assertEqual(
                    self.flagged(records),
                    set(self.REAL) - {"case_1d10b79d", "case_5cc8f0a3"})
                self.assertEqual(self.unknown(records), [])

    def test_each_continuation_path_alone_silences_the_card(self):
        for patch in ({"liveRunCount": 1}, {"queuedRunCount": 2},
                      {"unresolvedBlockerCount": 1},
                      {"activeRecoveryAction": {"id": "ra-1"}},
                      {"monitorNextCheckAt": "2026-10-04T23:00:00Z"}):
            with self.subTest(patch):
                self.assertFalse(self.only_row_changed("case_a99daa6c", **patch))

    def test_only_assigned_in_progress_cards_count(self):
        for patch in ({"status": "blocked"}, {"status": "todo"},
                      {"status": "in_review"}, {"assigneeAgentId": None},
                      {"assigneeAgentId": ""}):
            with self.subTest(patch):
                self.assertFalse(self.only_row_changed("case_a99daa6c", **patch))
                snapshot = self.snapshot()
                self.row(snapshot, "case_a99daa6c").update(patch)
                # Out of scope is silent, not an unknown-lane finding.
                self.assertEqual(self.unknown(detect(snapshot)), [])

    def test_comment_age_boundary_is_strictly_older_than_30_minutes(self):
        for stamp, fires in (("2026-10-04T20:20:00Z", False),
                             ("2026-10-04T20:19:59Z", True),
                             ("2026-10-04T20:30:00Z", False)):
            with self.subTest(createdAt=stamp):
                snapshot = self.snapshot()
                self.row(snapshot, "case_0e12073a")["newestComment"][
                    "createdAt"] = stamp
                self.assertEqual("case_0e12073a" in self.flagged(detect(snapshot)),
                                 fires)

    def test_lane_boundary_is_a_success_within_15_minutes(self):
        for stamp, fires in (("2026-10-04T20:35:00Z", True),
                             ("2026-10-04T20:34:59Z", False)):
            with self.subTest(lastSucceededAt=stamp):
                snapshot = self.snapshot()
                for run in snapshot["agentRuns"]:
                    if run["agentId"] == self.SLOW_LANE:
                        run["lastSucceededAt"] = stamp
                self.assertEqual("case_1d10b79d" in self.flagged(detect(snapshot)),
                                 fires)

    def test_newest_succeeded_row_wins_for_a_duplicated_agent(self):
        snapshot = self.snapshot()
        snapshot["agentRuns"].insert(
            0, {"agentId": self.SLOW_LANE, "lastSucceededAt": None})
        snapshot["agentRuns"].append(
            {"agentId": self.SLOW_LANE,
             "lastSucceededAt": "2026-10-04T16:00:00Z"})
        records = detect(snapshot)
        self.assertEqual(self.flagged(records) & {"case_1d10b79d", "case_5cc8f0a3"},
                         {"case_1d10b79d", "case_5cc8f0a3"})

    def test_missing_sections_are_unknown_never_a_pass(self):
        for section in ("assignedInProgress", "agentRuns"):
            with self.subTest(section):
                snapshot = self.snapshot()
                del snapshot[section]
                found = self.unknown(detect(snapshot))
                self.assertEqual([r["reason"] for r in found],
                                 [f"{section} snapshot missing"])
                self.assertEqual(self.flagged(detect(snapshot)), set())

    def test_unreadable_fields_are_unknown_not_flagged(self):
        cases = (("liveRunCount", "x"), ("queuedRunCount", None),
                 ("unresolvedBlockerCount", -1), ("liveRunCount", True),
                 ("monitorNextCheckAt", "not-a-time"))
        for field, value in cases:
            with self.subTest(field, value=value):
                snapshot = self.snapshot()
                self.row(snapshot, "case_a99daa6c")[field] = value
                records = detect(snapshot)
                found = self.unknown(records)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["identifier"], "case_a99daa6c")
                self.assertEqual(found[0]["evidence"]["fields"], [field])
                self.assertNotIn("case_a99daa6c", self.flagged(records))
        for field in ("activeRecoveryAction", "monitorNextCheckAt"):
            with self.subTest(field, value="absent"):
                snapshot = self.snapshot()
                del self.row(snapshot, "case_a99daa6c")[field]
                found = self.unknown(detect(snapshot))
                self.assertEqual(found[0]["evidence"]["fields"], [field])

    def test_a_definite_continuation_beats_an_unreadable_field(self):
        snapshot = self.snapshot()
        row = self.row(snapshot, "case_a99daa6c")
        row["liveRunCount"] = 1
        row["queuedRunCount"] = "x"
        self.assertEqual(self.unknown(detect(snapshot)), [])
        self.assertNotIn("case_a99daa6c", self.flagged(detect(snapshot)))

    def test_unknown_lane_evidence_is_unknown_not_silent(self):
        snapshot = self.snapshot()
        snapshot["agentRuns"] = [r for r in snapshot["agentRuns"]
                                 if r["agentId"] != self.SLOW_LANE]
        found = self.unknown(detect(snapshot))
        self.assertEqual({r["identifier"] for r in found},
                         {"case_1d10b79d", "case_5cc8f0a3"})
        self.assertIn("no agentRuns row", found[0]["reason"])
        snapshot = self.snapshot()
        for run in snapshot["agentRuns"]:
            if run["agentId"] == self.SLOW_LANE:
                run["lastSucceededAt"] = "not-a-time"
        found = self.unknown(detect(snapshot))
        self.assertEqual({r["identifier"] for r in found},
                         {"case_1d10b79d", "case_5cc8f0a3"})
        self.assertIn("unreadable lastSucceededAt", found[0]["reason"])

    def test_unreadable_comment_time_or_missing_issue_id_is_unknown(self):
        for field, value in (("createdAt", "not-a-time"), ("createdAt", None)):
            with self.subTest(field, value=value):
                snapshot = self.snapshot()
                self.row(snapshot, "case_a99daa6c")["newestComment"][field] = value
                found = self.unknown(detect(snapshot))
                self.assertEqual([r["identifier"] for r in found],
                                 ["case_a99daa6c"])
        snapshot = self.snapshot()
        self.row(snapshot, "case_a99daa6c")["issueId"] = None
        self.assertEqual([r["identifier"] for r in
                          self.unknown(detect(snapshot))], ["case_a99daa6c"])

    def test_non_object_rows_are_ignored_without_a_crash(self):
        snapshot = self.snapshot()
        snapshot["assignedInProgress"].insert(0, "garbage")
        snapshot["agentRuns"].insert(0, 42)
        self.assertEqual(self.flagged(detect(snapshot)), set(self.REAL))

    def test_unhashable_agent_ids_are_unknown_never_a_crash(self):
        # Paperclip Review on PR #718: a list or object id reached a dict
        # lookup and raised TypeError, stopping all 14 families for the
        # cycle. Both fields now accept non-empty strings only.
        for junk in ([1], {"a": 1}):
            with self.subTest(junk=junk):
                snapshot = self.snapshot()
                self.row(snapshot, "case_a99daa6c")["assigneeAgentId"] = junk
                records = detect(snapshot)
                self.assertNotIn("case_a99daa6c", self.flagged(records))
                found = [r for r in self.unknown(records)
                         if r["identifier"] == "case_a99daa6c"]
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["evidence"]["fields"],
                                 ["assigneeAgentId"])
                snapshot = self.snapshot()
                snapshot["agentRuns"].append(
                    {"agentId": junk,
                     "lastSucceededAt": "2026-10-04T20:40:00Z"})
                records = detect(snapshot)
                self.assertEqual(self.flagged(records), set(self.REAL))
                self.assertIn("agentRuns rows unreadable",
                              [r["reason"] for r in self.unknown(records)])

    def test_cli_prints_the_seven_proposals_as_jsonl(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--snapshot", STRANDED_FIXTURE])
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        proposals = [r for r in rows
                     if r.get("detector") == "watchdog/stranded_429"
                     and r["type"] == "proposal"]
        self.assertEqual(sorted(r["identifier"] for r in proposals),
                         sorted(self.REAL))


class WriterCompatTest(unittest.TestCase):
    def test_phase1_proposals_are_none_mutation(self):
        s = snap(
            issues=[{"id": "i1", "identifier": "case_8c5fe4cc", "inFocus": True,
                     "assigneeAgentId": None, "inBacklogMin": 90}],
            runEvents=[{"issueId": "i1", "identifier": "case_8c5fe4cc",
                        "kind": "dispatch_stalled_issue",
                        "at": "2026-10-03T13:00:00Z"}] * 4,
            hosts=[{"host": "192.0.2.190", "diskPct": 10,
                    "load": 1.0, "vcpu": 4, "sustainedMin": 1}])
        for record in detect(s):
            if record.get("type") == "proposal":
                self.assertEqual(record["mutation"], "none")
                self.assertIn(record["mutation"],
                              WRITER_COMPATIBLE_MUTATIONS)

    def test_compat_set_covers_writer_allowlist(self):
        self.assertEqual(WRITER_COMPATIBLE_MUTATIONS,
                         {"none", "create_pause_hold", "assign_blocker_owner",
                          "restore_blocked", "operator_decision",
                          "clear_pin"})


if __name__ == "__main__":
    unittest.main()
