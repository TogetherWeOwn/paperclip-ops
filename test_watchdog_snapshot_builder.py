#!/usr/bin/env python3
"""Offline tests for watchdog/snapshot_builder.py (case_d42263d6).

The builder turns API exports into the snapshot watchdog/detectors.py reads.
These tests pin the one promise that matters: a section is built only from
evidence the exports carry, and a section that cannot be built stays ABSENT, so
the detector still says ``unknown`` instead of reading a clean bill. Every
section has a fixture built from the live API shape, plus hostile cases (a
missing field, a wrong type, a truncated export). Detector verdicts are checked
end to end through ``detect()``.

Pure fixtures: no credentials, no network, no board. CLI children get a minimal
environment and a temporary working directory. The mutation gate that proves
these tests fail when the builder breaks is
verification/watchdog-snapshot-builder-mutation-gate.py.
"""

import builtins
import copy
import datetime
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from watchdog import snapshot_builder as sb
from watchdog.detectors import detect, parse_time

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILDER = os.path.join(ROOT, "watchdog", "snapshot_builder.py")
DETECTORS = os.path.join(ROOT, "watchdog", "detectors.py")

NOW_TEXT = "2026-10-07T03:00:00Z"
NOW = parse_time(NOW_TEXT)

# A value no builder output or message may carry: it is planted in the field a
# hostile row gets wrong, and in a comment body past the 200 characters kept.
MARKER = "ZZ-ROW-VALUE-MARKER-5f3a91"


def at(minutes_ago):
    return (NOW - datetime.timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def issue(ident="case_8c5fe4cc", status="todo", priority="high", assignee="agent-a",
          updated=None, **extra):
    """A live-shaped issue row (trimmed to the fields the builder reads)."""
    row = {"id": f"id-{ident}", "identifier": ident, "status": status,
           "priority": priority, "assigneeAgentId": assignee,
           "updatedAt": updated or at(10), "title": "t", "labels": [],
           "monitorNextCheckAt": None, "activeRecoveryAction": None,
           "blockerAttention": {"state": "none", "unresolvedBlockerCount": 0},
           "activeRun": None}
    row.update(extra)
    return row


def agent(ident, status="idle", reports_to=None, url_key=None):
    return {"id": ident, "name": ident, "status": status,
            "reportsTo": reports_to, "urlKey": url_key or ident}


def run(rid, agent_id="agent-a", status="succeeded", created_ago=30,
        issue_id=None, wake=None, finished_ago=None, **extra):
    row = {"id": rid, "agentId": agent_id, "status": status,
           "createdAt": at(created_ago),
           "startedAt": at(created_ago),
           "finishedAt": None if finished_ago is None else at(finished_ago),
           "errorCode": None, "invocationSource": "automation",
           "contextSnapshot": {}}
    if issue_id:
        row["contextSnapshot"]["issueId"] = issue_id
    if wake:
        row["contextSnapshot"]["wakeReason"] = wake
    row.update(extra)
    return row


def sweep(rid, issue_id, created_ago, **extra):
    return run(rid, issue_id=issue_id, wake=sb.SWEEP_WAKE_REASON,
               created_ago=created_ago, **extra)


def build(**inputs):
    """build_snapshot with friendly keys; returns (snapshot, absent)."""
    names = {"recovery": "recoveryEvents", "rc": "redMainsRc"}
    return sb.build_snapshot({names.get(k, k): v for k, v in inputs.items()},
                             NOW)


def findings(snapshot, family):
    return [r for r in detect(snapshot)
            if r.get("detector") == f"watchdog/{family}"]


def unknowns(snapshot, family):
    return [r for r in findings(snapshot, family)
            if r.get("severity") == "unknown"]


class IssuesTest(unittest.TestCase):

    def test_focus_is_high_or_critical_in_todo_in_progress_or_in_review(self):
        for priority in ("critical", "high", "medium", "low"):
            for status in ("backlog", "todo", "in_progress", "in_review",
                           "blocked", "done", "cancelled"):
                with self.subTest(priority=priority, status=status):
                    snap, _ = build(issues=[issue(
                        status=status, priority=priority)])
                    want = (priority in ("critical", "high")
                            and status in ("todo", "in_progress",
                                           "in_review"))
                    self.assertIs(snap["issues"][0]["inFocus"], want)

    def test_backlog_minutes_count_only_for_backlog_cards(self):
        snap, _ = build(issues=[
            issue("case_8c5fe4cc", status="backlog", updated=at(90)),
            issue("case_d02d0252", status="todo", updated=at(60 * 24 * 5)),
            issue("case_a6dbab15", status="in_progress", updated=at(300))])
        by = {row["identifier"]: row for row in snap["issues"]}
        self.assertEqual(by["case_8c5fe4cc"]["inBacklogMin"], 90.0)
        self.assertNotIn("inBacklogMin", by["case_d02d0252"])
        self.assertNotIn("inBacklogMin", by["case_a6dbab15"])

    def test_backlog_minutes_never_go_negative(self):
        snap, _ = build(issues=[issue(status="backlog", updated=at(-45))])
        self.assertEqual(snap["issues"][0]["inBacklogMin"], 0.0)

    def test_assigned_todo_cards_do_not_claim_backlog_age(self):
        # 2026-10-07: a hand-built snapshot counted todo cards as backlog and
        # produced four false assignment findings.
        snap, _ = build(issues=[
            issue(f"TOG-{n}", status="todo", updated=at(60 * 24 * 3))
            for n in range(1, 5)])
        self.assertTrue(all(row["inFocus"] for row in snap["issues"]))
        self.assertTrue(all("inBacklogMin" not in row
                            for row in snap["issues"]))
        self.assertNotIn("assignmentEvidence", snap)

    def test_unassigned_focus_card_does_not_claim_assignment_evidence(self):
        snap, _ = build(issues=[issue("case_f052904a", assignee=None)])
        self.assertTrue(snap["issues"][0]["inFocus"])
        self.assertIsNone(snap.get("assignmentEvidence"))

    def test_row_fields_are_the_detector_contract_and_nothing_else(self):
        snap, absent = build(issues=[issue(
            "case_0dd2d987", status="in_review", priority="critical",
            assignee="agent-b")])
        self.assertEqual(absent.get("issues"), None)
        self.assertEqual(snap["issues"], [{
            "id": "id-case_0dd2d987", "identifier": "case_0dd2d987", "status": "in_review",
            "assigneeAgentId": "agent-b", "inFocus": True}])

    def test_review_authorship_is_never_invented(self):
        snap, _ = build(issues=[issue()])
        self.assertNotIn("isReview", snap["issues"][0])
        self.assertNotIn("prAuthorAgentId", snap["issues"][0])

    def test_hostile_rows_leave_the_section_absent(self):
        hostile = {
            "row not an object": ["oops"],
            "missing id": [{k: v for k, v in issue().items() if k != "id"}],
            "id wrong type": [issue(**{"id": 7})],
            "status wrong type": [issue(status=5)],
            "priority missing": [{k: v for k, v in issue().items()
                                  if k != "priority"}],
            "priority wrong type": [issue(priority=["high"])],
            "identifier wrong type": [issue(identifier=12)],
            "assignee wrong type": [issue(assignee=5)],
            "assignee missing": [{k: v for k, v in issue().items()
                                  if k != "assigneeAgentId"}],
            "status misspelled": [issue(status="in_progres")],
            "backlog without updatedAt": [issue(
                status="backlog", updated=None, updatedAt=None)],
            "backlog updatedAt wrong type": [issue(
                status="backlog", updatedAt=MARKER)],
            "export is an error object": {"error": MARKER},
            "export is a string": MARKER,
            "export is empty": [],
            "export is a full page": [issue(f"TOG-{n}") for n in
                                      range(sb.LIST_CAP)],
        }
        for label, export in hostile.items():
            with self.subTest(label):
                snap, absent = build(issues=export)
                self.assertNotIn("issues", snap)
                self.assertIn("issues", absent)
                self.assertNotIn(MARKER, absent["issues"])
                self.assertEqual(
                    [r["reason"] for r in unknowns(snap, "assignment")],
                    ["issues snapshot missing"])

    def test_one_bad_row_does_not_leave_a_partial_section(self):
        snap, absent = build(issues=[issue("case_8c5fe4cc"), "oops", issue("case_a6dbab15")])
        self.assertNotIn("issues", snap)
        self.assertIn("1 of 3 rows unreadable", absent["issues"])

    def test_no_export_means_no_section(self):
        snap, absent = build()
        self.assertEqual(set(snap), {"now"})
        self.assertEqual(absent["issues"], "no --issues supplied")


class AgentsTest(unittest.TestCase):

    def test_status_and_owning_lead_come_from_the_manager_url_key(self):
        snap, _ = build(agents=[
            agent("boss", url_key="cto"),
            agent("dev", status="running", reports_to="boss"),
            agent("solo")])
        self.assertEqual(snap["agents"], [
            {"agentId": "boss", "status": "idle"},
            {"agentId": "dev", "status": "running", "owningLead": "cto"},
            {"agentId": "solo", "status": "idle"}])

    def test_an_agent_in_error_is_unaged_so_the_detector_says_unknown(self):
        snap, _ = build(agents=[agent("boss", url_key="cto"),
                                agent("dev", status="error",
                                      reports_to="boss")])
        self.assertNotIn("errorSince", snap["agents"][1])
        [row] = unknowns(snap, "agent_error")
        self.assertEqual(row["reason"],
                         "agent in error with unreadable errorSince")
        self.assertEqual(row["owner"], "cto")

    def test_healthy_agents_are_a_verdict_not_an_unknown(self):
        snap, _ = build(agents=[agent("a"), agent("b", status="paused")])
        self.assertEqual(findings(snap, "agent_error"), [])

    def test_hostile_rows_leave_the_section_absent(self):
        hostile = {
            "row not an object": [None],
            "status wrong type": [agent("a", status=3)],
            "id missing": [{"status": "idle"}],
            "reportsTo wrong type": [agent("a", reports_to=9)],
            "urlKey wrong type": [agent("a", url_key=MARKER) | {"urlKey": 4}],
            "export is a dict": {"agents": []},
            "export is empty": [],
        }
        for label, export in hostile.items():
            with self.subTest(label):
                snap, absent = build(agents=export)
                self.assertNotIn("agents", snap)
                self.assertIn("agents", absent)
                self.assertEqual(
                    [r["reason"] for r in unknowns(snap, "agent_error")],
                    ["agents snapshot missing"])


class RunEventsTest(unittest.TestCase):

    def test_four_sweep_wakes_in_six_hours_fire_the_churn_detector(self):
        runs = [sweep(f"r{n}", "id-case_8c5fe4cc", 20 + 30 * n) for n in range(4)]
        snap, _ = build(runs=runs, issues=[issue("case_8c5fe4cc")])
        self.assertEqual(len(snap["runEvents"]), 4)
        self.assertEqual(snap["runEvents"][0], {
            "issueId": "id-case_8c5fe4cc", "kind": "dispatch_stalled_issue",
            "at": at(110), "identifier": "case_8c5fe4cc", "runId": "r3"})
        [hit] = [r for r in findings(snap, "churn")
                 if r["type"] == "finding"]
        self.assertEqual(hit["identifier"], "case_8c5fe4cc")
        self.assertEqual(hit["evidence"]["rewokes"], 4)

    def test_future_wakes_are_not_counted(self):
        snap, _ = build(runs=[sweep("future", "i1", -5),
                              sweep("in", "i1", 5)],
                        recovery=[{"issueId": "i1", "reason":
                                   "dispatch_stalled_issue", "at": at(-4)}])
        self.assertEqual([e["runId"] for e in snap["runEvents"]], ["in"])
        self.assertEqual(snap["parks"][0]["runCount6h"], 1)

    def test_duplicate_exports_count_each_run_id_once(self):
        row = sweep("r1", "i1", 5)
        snap, _ = build(runs=[row, copy.deepcopy(row)])
        self.assertEqual(len(snap["runEvents"]), 1)
        self.assertEqual(snap["parks"][0]["runCount6h"], 1)
        snap, absent = build(runs=[row, row | {"status": "failed"}])
        for key in ("runEvents", "parks", "agentRuns"):
            self.assertNotIn(key, snap)
            self.assertIn("conflicting duplicate", absent[key])

    def test_duplicate_recovery_events_count_once_when_run_id_is_known(self):
        row = {"issueId": "i1", "kind": "dispatch_stalled_issue",
               "at": at(5), "runId": "recovered"}
        snap, _ = build(runs=[run("r1")], recovery=[row, row.copy()])
        self.assertEqual(len(snap["runEvents"]), 1)

    def test_three_sweep_wakes_stay_quiet(self):
        snap, _ = build(runs=[sweep(f"r{n}", "i1", 20 + 30 * n)
                              for n in range(3)])
        self.assertEqual(len(snap["runEvents"]), 3)
        self.assertEqual(findings(snap, "churn"), [])

    def test_wakes_outside_the_six_hour_window_are_not_counted(self):
        snap, _ = build(runs=[sweep("old", "i1", 361),
                              sweep("in", "i1", 359)])
        self.assertEqual([e["runId"] for e in snap["runEvents"]], ["in"])

    def test_other_wake_reasons_and_runs_without_an_issue_are_ignored(self):
        snap, _ = build(runs=[
            run("a", issue_id="i1", wake="issue_commented"),
            run("b", issue_id="i1", wake="issue_assigned"),
            run("c", wake=sb.SWEEP_WAKE_REASON),
            run("d", issue_id="i1")])
        self.assertEqual([e["kind"] for e in snap["runEvents"]],
                         ["issue_commented"])
        self.assertNotIn("selfWake", snap["runEvents"][0])
        self.assertEqual(findings(snap, "churn"), [])

    def test_recovery_events_add_sweep_wakes_no_run_recorded(self):
        snap, _ = build(
            runs=[sweep("r1", "i1", 20)],
            recovery=[
                {"issueId": "i1", "reason": "dispatch_stalled_issue",
                 "createdAt": at(50), "identifier": "case_8c5fe4cc"},
                {"issueId": "i1", "kind": "dispatch_stalled_issue",
                 "at": at(80)},
                {"issueId": "i1", "reason": "issue_commented",
                 "createdAt": at(10)},
                {"issueId": "i1", "reason": "dispatch_stalled_issue",
                 "createdAt": at(400)}])
        self.assertEqual(len(snap["runEvents"]), 3)
        self.assertEqual({e.get("identifier") for e in snap["runEvents"]},
                         {"case_8c5fe4cc"})

    def test_a_recovery_event_for_a_counted_run_is_the_same_wake(self):
        snap, _ = build(
            runs=[sweep("r1", "i1", 20)],
            recovery=[{"issueId": "i1", "reason": "dispatch_stalled_issue",
                       "createdAt": at(20), "runId": "r1"},
                      {"issueId": "i1", "reason": "dispatch_stalled_issue",
                       "createdAt": at(21), "runId": "r-other"}])
        self.assertEqual([e["runId"] for e in snap["runEvents"]],
                         ["r-other", "r1"])
        self.assertEqual(len(snap["runEvents"]), 2)

    def test_identifier_comes_from_issues_even_when_that_section_is_absent(
            self):
        snap, absent = build(runs=[sweep("r1", "id-case_8c5fe4cc", 20)],
                             issues=[issue("case_8c5fe4cc"), "oops"])
        self.assertIn("issues", absent)
        self.assertEqual(snap["runEvents"][0]["identifier"], "case_8c5fe4cc")

    def test_output_is_ordered_oldest_first_regardless_of_export_order(self):
        runs = [sweep("r2", "i1", 10), sweep("r1", "i1", 90),
                sweep("r3", "i2", 50)]
        snap, _ = build(runs=runs)
        self.assertEqual([e["runId"] for e in snap["runEvents"]],
                         ["r1", "r3", "r2"])

    def test_a_full_page_that_does_not_reach_six_hours_back_is_absent(self):
        page = [run(f"r{n}", created_ago=n * 0.2) for n in range(sb.LIST_CAP)]
        snap, absent = build(runs=page)
        for section in ("runEvents", "parks"):
            self.assertNotIn(section, snap)
            self.assertIn("full page covering", absent[section])
        self.assertNotIn("agentRuns", snap)  # creation cannot bound completion
        self.assertIn("completion history", absent["agentRuns"])
        self.assertEqual(
            [r["reason"] for r in unknowns(snap, "churn")],
            ["churn snapshot missing"])

    def test_a_full_page_that_reaches_six_hours_back_is_built(self):
        page = [run(f"r{n}", created_ago=n * 0.4) for n in range(sb.LIST_CAP)]
        snap, absent = build(runs=page)
        self.assertEqual(snap["runEvents"], [])
        self.assertNotIn("runEvents", absent)

    def test_a_short_export_is_complete_whatever_its_age(self):
        snap, _ = build(runs=[run("r1", created_ago=5)])
        self.assertEqual(snap["runEvents"], [])

    def test_hostile_run_rows_leave_the_run_sections_absent(self):
        base = run("r1")
        hostile = {
            "row not an object": ["oops"],
            "id missing": [{k: v for k, v in base.items() if k != "id"}],
            "createdAt wrong type": [run("r", created_ago=1) | {
                "createdAt": 7}],
            "createdAt unparseable": [run("r") | {"createdAt": MARKER}],
            "finishedAt wrong type": [run("r") | {"finishedAt": 5}],
            "status wrong type": [run("r") | {"status": None}],
            "errorCode missing": [{k: v for k, v in base.items()
                                    if k != "errorCode"}],
            "errorCode wrong type": [run("r") | {"errorCode": 5}],
            "contextSnapshot a list": [run("r") | {"contextSnapshot": [1]}],
            "issueId wrong type": [run("r") | {
                "contextSnapshot": {"issueId": 4}}],
            "wakeReason wrong type": [run("r") | {
                "contextSnapshot": {"wakeReason": {"a": 1}}}],
            "export is an error object": {"error": MARKER},
            "export is empty": [],
        }
        for label, export in hostile.items():
            with self.subTest(label):
                snap, absent = build(runs=export)
                for section in ("runEvents", "parks", "agentRuns"):
                    self.assertNotIn(section, snap)
                    self.assertIn(section, absent)
                    self.assertNotIn(MARKER, absent[section])
                self.assertEqual(
                    [r["reason"] for r in unknowns(snap, "churn")],
                    ["churn snapshot missing"])

    def test_hostile_recovery_events_leave_only_runEvents_absent(self):
        good = {"issueId": "i1", "reason": "dispatch_stalled_issue",
                "createdAt": at(5)}
        hostile = {
            "row not an object": [good, "oops"],
            "issueId missing": [{k: v for k, v in good.items()
                                 if k != "issueId"}],
            "issueId wrong type": [good | {"issueId": 3}],
            "time unparseable": [good | {"createdAt": MARKER}],
            "time wrong type": [good | {"createdAt": 99}],
            "reason wrong type": [good | {"reason": ["x"]}],
            "runId wrong type": [good | {"runId": 1}],
            "export is a dict": {"events": []},
            "export is empty": [],
        }
        for label, events in hostile.items():
            with self.subTest(label):
                snap, absent = build(runs=[run("r1")], recovery=events)
                self.assertNotIn("runEvents", snap)
                self.assertIn("runEvents", absent)
                self.assertNotIn(MARKER, absent["runEvents"])
                self.assertIn("parks", snap)


class ParksTest(unittest.TestCase):

    def repair(self, rid, issue_id, ago):
        return run(rid, issue_id=issue_id, created_ago=ago,
                   wake=sb.DISPOSITION_WAKE_REASON)

    def test_run_comment_and_setup_failure_counts_are_per_issue(self):
        runs = [run("a", issue_id="i1", wake="issue_commented",
                    contextSnapshot={"issueId": "i1", "wakeReason":
                                     "issue_commented", "wakeCommentId": "c1"}),
                run("b", issue_id="i1", errorCode="setup_failed"),
                run("c", issue_id="i1", errorCode="setup_failed"),
                run("d", issue_id="i2", errorCode="setup_failed"),
                run("old", issue_id="i1", created_ago=361,
                    errorCode="setup_failed")]
        snap, _ = build(runs=runs, wakeComments={
            "c1": {"authorAgentId": "agent-a"}})
        [first, second] = snap["parks"]
        self.assertEqual(first, {"issueId": "i1", "runCount6h": 3,
                                 "issueCommentedCount": 1, "selfWakeCount": 1,
                                 "setupFailedCount": 2,
                                 "missingDispositionCount": 0})
        self.assertEqual(second["runCount6h"], 1)
        self.assertEqual(second["setupFailedCount"], 1)
        self.assertIs(snap["runEvents"][0]["selfWake"], True)
        self.assertEqual(findings(snap, "churn"), [])

    def test_other_agent_and_human_comments_are_not_self_wakes(self):
        for author in (None, "agent-b"):
            with self.subTest(author=author):
                snap, _ = build(runs=[run("r", contextSnapshot={
                    "issueId": "i1", "wakeReason": "issue_commented",
                    "commentId": "c1"})],
                    wakeComments={"c1": {"authorAgentId": author}})
                self.assertIs(snap["runEvents"][0]["selfWake"], False)
                self.assertEqual(snap["parks"][0]["selfWakeCount"], 0)

    def test_missing_comment_author_evidence_is_not_zero_self_wakes(self):
        snap, _ = build(runs=[run("r", issue_id="i1",
                                  wake="issue_commented")])
        self.assertEqual(snap["parks"][0]["issueCommentedCount"], 1)
        self.assertNotIn("selfWakeCount", snap["parks"][0])
        self.assertNotIn("selfWake", snap["runEvents"][0])

    def test_hostile_wake_comment_author_leaves_both_event_sections_absent(self):
        for value in ([], {"c1": {}}, {"c1": {"authorAgentId": 5}}):
            with self.subTest(value=value):
                snap, absent = build(runs=[run("r", contextSnapshot={
                    "issueId": "i1", "wakeReason": "issue_commented",
                    "commentId": "c1"})], wakeComments=value)
                for key in ("runEvents", "parks"):
                    self.assertNotIn(key, snap)
                    self.assertIn(key, absent)

    def test_three_repair_wakes_fire_the_missing_disposition_finding(self):
        snap, _ = build(
            runs=[self.repair(f"r{n}", "id-case_8c5fe4cc", 20 * n + 5)
                  for n in range(3)],
            issues=[issue("case_8c5fe4cc")])
        self.assertEqual(snap["parks"], [{
            "issueId": "id-case_8c5fe4cc", "missingDispositionCount": 3,
            "runCount6h": 3, "issueCommentedCount": 0,
            "setupFailedCount": 0, "selfWakeCount": 0,
            "identifier": "case_8c5fe4cc"}])
        [hit] = [r for r in findings(snap, "churn") if r["type"] == "finding"]
        self.assertEqual(hit["evidence"]["missingDispositionCount"], 3)

    def test_two_repair_wakes_stay_quiet(self):
        snap, _ = build(runs=[self.repair("a", "i1", 5),
                              self.repair("b", "i1", 9)])
        self.assertEqual(snap["parks"][0]["missingDispositionCount"], 2)
        self.assertEqual(findings(snap, "churn"), [])

    def test_counts_are_per_issue_inside_the_window_only(self):
        snap, _ = build(runs=[
            self.repair("a", "i1", 5), self.repair("b", "i1", 361),
            self.repair("c", "i2", 5), self.repair("d", "i2", 6),
            run("e", issue_id="i1", wake="issue_commented")])
        self.assertEqual([(r["issueId"], r["missingDispositionCount"])
                          for r in snap["parks"]], [("i1", 1), ("i2", 2)])
        self.assertEqual([r["runCount6h"] for r in snap["parks"]], [2, 2])

    def test_no_repair_wakes_is_a_zero_count_not_an_absent_section(self):
        snap, absent = build(runs=[run("a", issue_id="i1")])
        self.assertEqual(snap["parks"][0]["missingDispositionCount"], 0)
        self.assertNotIn("parks", absent)


class AssignedInProgressTest(unittest.TestCase):

    def card(self, ident="case_8c5fe4cc", **extra):
        return issue(ident, status="in_progress", **extra)

    def rejected(self, minutes_ago=45):
        return {"createdAt": at(minutes_ago),
                "body": "API Error: Request rejected (429) " + MARKER * 20}

    def snapshot(self, cards, comments, runs=None):
        return build(
            issues=cards, comments=comments,
            runs=runs if runs is not None else [run("seed", "agent-z")])

    def test_row_carries_the_continuation_fields_the_detector_reads(self):
        card = self.card(
            "case_8c5fe4cc", monitorNextCheckAt="2026-10-07T09:00:00.000Z",
            blockerAttention={"unresolvedBlockerCount": 2},
            activeRecoveryAction={"id": "x", "kind": "stall"},
            activeRun={"status": "queued"})
        runs = [run("l1", status="running", issue_id="id-case_8c5fe4cc"),
                run("l2", status="running", issue_id="id-case_8c5fe4cc"),
                run("q1", status="queued", issue_id="id-case_8c5fe4cc"),
                run("x", status="running", issue_id="id-OTHER"),
                run("s", status="succeeded", issue_id="id-case_8c5fe4cc")]
        snap, absent = self.snapshot([card], {"id-case_8c5fe4cc": None}, runs)
        self.assertNotIn("assignedInProgress", absent)
        self.assertEqual(snap["assignedInProgress"], [{
            "issueId": "id-case_8c5fe4cc", "identifier": "case_8c5fe4cc",
            "status": "in_progress", "assigneeAgentId": "agent-a",
            "liveRunCount": 2, "queuedRunCount": 1,
            "unresolvedBlockerCount": 2,
            "monitorNextCheckAt": "2026-10-07T09:00:00.000Z",
            "activeRecoveryAction": True}])

    def test_the_issues_active_run_floors_the_run_counts(self):
        snap, _ = self.snapshot(
            [self.card("case_8c5fe4cc", activeRun={"status": "running"}),
             self.card("case_d02d0252", activeRun={"status": "queued"})],
            {"id-case_8c5fe4cc": None, "id-case_d02d0252": None})
        counts = {r["identifier"]: (r["liveRunCount"], r["queuedRunCount"])
                  for r in snap["assignedInProgress"]}
        self.assertEqual(counts, {"case_8c5fe4cc": (1, 0), "case_d02d0252": (0, 1)})
        for row in snap["assignedInProgress"]:
            for key in ("liveRunCount", "queuedRunCount"):
                self.assertIs(type(row[key]), int)

    def test_only_assigned_in_progress_cards_are_rows(self):
        snap, _ = self.snapshot(
            [self.card("case_8c5fe4cc"), self.card("case_d02d0252", assignee=None),
             issue("case_a6dbab15", status="todo"), issue("case_4ac9f045", status="blocked")],
            {"id-case_8c5fe4cc": None})
        self.assertEqual([r["identifier"] for r in snap["assignedInProgress"]],
                         ["case_8c5fe4cc"])

    def test_newest_comment_keeps_the_first_200_characters_only(self):
        snap, _ = self.snapshot([self.card()],
                                {"id-case_8c5fe4cc": self.rejected()})
        body = snap["assignedInProgress"][0]["newestComment"]["body"]
        self.assertEqual(len(body), sb.COMMENT_KEEP_CHARS)
        self.assertTrue(body.startswith("API Error: Request rejected (429)"))

    def test_a_card_stranded_after_a_429_fires_through_the_detector(self):
        cards = [self.card("case_8c5fe4cc")]
        runs = [run("ok", "agent-a", "succeeded", created_ago=10,
                    finished_ago=5)]
        snap, absent = self.snapshot(cards, {"id-case_8c5fe4cc": self.rejected()},
                                     runs)
        self.assertEqual(absent, {k: v for k, v in absent.items()
                                  if k not in ("assignedInProgress",
                                               "agentRuns")})
        proposal = [r for r in findings(snap, "stranded_429")
                    if r["type"] == "proposal"]
        self.assertEqual([p["identifier"] for p in proposal], ["case_8c5fe4cc"])

    def test_a_live_run_keeps_the_stranded_detector_quiet(self):
        runs = [run("ok", "agent-a", "succeeded", created_ago=10,
                    finished_ago=5),
                run("live", "agent-a", "running", issue_id="id-case_8c5fe4cc")]
        snap, _ = self.snapshot([self.card("case_8c5fe4cc")],
                                {"id-case_8c5fe4cc": self.rejected()}, runs)
        self.assertEqual(findings(snap, "stranded_429"), [])

    def test_a_scheduled_retry_is_a_queued_continuation(self):
        runs = [run("ok", "agent-a", "succeeded", created_ago=10,
                    finished_ago=5),
                run("retry", "agent-a", "scheduled_retry",
                    issue_id="id-case_8c5fe4cc")]
        snap, _ = self.snapshot([self.card("case_8c5fe4cc")],
                                {"id-case_8c5fe4cc": self.rejected()}, runs)
        [row] = snap["assignedInProgress"]
        self.assertEqual(row["queuedRunCount"], 1)
        self.assertEqual(findings(snap, "stranded_429"), [])

    def test_active_scheduled_retry_also_floors_the_queued_count(self):
        card = self.card("case_8c5fe4cc", activeRun={"status": "scheduled_retry"})
        snap, _ = self.snapshot([card], {"id-case_8c5fe4cc": self.rejected()}, [
            run("ok", "agent-a", "succeeded", created_ago=10,
                finished_ago=5)])
        [row] = snap["assignedInProgress"]
        self.assertEqual(row["queuedRunCount"], 1)
        self.assertEqual(findings(snap, "stranded_429"), [])

    def test_hostile_comment_exports_leave_the_section_absent(self):
        hostile = {
            "card missing from the mapping": {},
            "mapping is a list": [self.rejected()],
            "comment is a string": {"id-case_8c5fe4cc": MARKER},
            "comment body wrong type": {"id-case_8c5fe4cc": {
                "createdAt": at(5), "body": 5}},
            "comment time unparseable": {"id-case_8c5fe4cc": {
                "createdAt": MARKER, "body": "x"}},
            "comment time missing": {"id-case_8c5fe4cc": {"body": "x"}},
        }
        for label, comments in hostile.items():
            with self.subTest(label):
                snap, absent = self.snapshot([self.card()], comments)
                self.assertNotIn("assignedInProgress", snap)
                self.assertIn("assignedInProgress", absent)
                self.assertNotIn(MARKER, absent["assignedInProgress"])
                self.assertEqual(
                    [r["reason"] for r in unknowns(snap, "stranded_429")],
                    ["assignedInProgress snapshot missing"])

    def test_no_comments_export_means_no_section(self):
        snap, absent = build(issues=[self.card()], runs=[run("r")])
        self.assertNotIn("assignedInProgress", snap)
        self.assertEqual(absent["assignedInProgress"],
                         "no --comments supplied")

    def test_hostile_card_fields_are_left_out_so_the_detector_says_unknown(
            self):
        hostile = {
            "blockers wrong type": {"blockerAttention": {
                "unresolvedBlockerCount": MARKER}},
            "blockerAttention a string": {"blockerAttention": MARKER},
            "blockers negative": {"blockerAttention": {
                "unresolvedBlockerCount": -1}},
            "blockers a bool": {"blockerAttention": {
                "unresolvedBlockerCount": True}},
            "monitor wrong type": {"monitorNextCheckAt": 5},
            "monitor unparseable": {"monitorNextCheckAt": MARKER},
            "recovery a string": {"activeRecoveryAction": MARKER},
            "recovery an empty object": {"activeRecoveryAction": {}},
        }
        omitted = {"blockers": "unresolvedBlockerCount",
                   "blockerAttention": "unresolvedBlockerCount",
                   "monitor": "monitorNextCheckAt",
                   "recovery": "activeRecoveryAction"}
        for label, patch in hostile.items():
            with self.subTest(label):
                snap, _ = self.snapshot(
                    [self.card(**patch)], {"id-case_8c5fe4cc": self.rejected()},
                    [run("ok", "agent-a", created_ago=10, finished_ago=5)])
                [row] = snap["assignedInProgress"]
                self.assertNotIn(omitted[label.split()[0]], row)
                [unknown] = unknowns(snap, "stranded_429")
                self.assertEqual(
                    unknown["reason"],
                    "429-ended card with unreadable continuation fields")
                self.assertNotIn(MARKER, json.dumps(unknown))

    def test_missing_continuation_keys_are_left_out_too(self):
        card = self.card()
        for key in ("monitorNextCheckAt", "activeRecoveryAction",
                    "blockerAttention"):
            del card[key]
        snap, _ = self.snapshot([card], {"id-case_8c5fe4cc": None})
        [row] = snap["assignedInProgress"]
        for key in ("monitorNextCheckAt", "activeRecoveryAction",
                    "unresolvedBlockerCount"):
            self.assertNotIn(key, row)

    def test_a_full_page_cannot_prove_live_run_counts(self):
        snap, absent = build(issues=[self.card()],
                             comments={"id-case_8c5fe4cc": self.rejected()},
                             runs=[run(f"r{n}", created_ago=n)
                                   for n in range(sb.LIST_CAP)])
        self.assertNotIn("assignedInProgress", snap)
        self.assertIn("cannot prove live/queued", absent["assignedInProgress"])

    def test_a_bad_issue_row_or_run_row_leaves_the_section_absent(self):
        snap, absent = build(issues=[self.card(), "oops"],
                             comments={"id-case_8c5fe4cc": None}, runs=[run("r")])
        self.assertIn("issues: 1 of 2", absent["assignedInProgress"])
        snap, absent = build(issues=[self.card()],
                             comments={"id-case_8c5fe4cc": None},
                             runs=[run("r") | {"createdAt": 3}])
        self.assertIn("runs: 1 of 1", absent["assignedInProgress"])


class AgentRunsTest(unittest.TestCase):

    def test_future_or_inverted_success_timestamps_do_not_prove_recovery(self):
        snap, _ = build(runs=[
            run("future", created_ago=5, finished_ago=-5),
            run("inverted", created_ago=5, finished_ago=10)])
        self.assertIsNone(snap["agentRuns"][0]["lastSucceededAt"])

    def test_last_succeeded_is_the_newest_finish_per_agent(self):
        snap, _ = build(runs=[
            run("a1", "agent-a", "succeeded", created_ago=100,
                finished_ago=90),
            run("a2", "agent-a", "succeeded", created_ago=50,
                finished_ago=40),
            run("a3", "agent-a", "failed", created_ago=20, finished_ago=10),
            run("a4", "agent-a", "running", created_ago=5),
            run("b1", "agent-b", "failed", created_ago=30, finished_ago=20)])
        self.assertEqual(snap["agentRuns"], [
            {"agentId": "agent-a", "lastSucceededAt": at(40)},
            {"agentId": "agent-b", "lastSucceededAt": None}])

    def test_an_assignee_with_no_runs_gets_a_row_with_no_success(self):
        snap, _ = build(
            runs=[run("r", "agent-a", created_ago=5, finished_ago=1)],
            issues=[issue("case_8c5fe4cc", status="in_progress", assignee="agent-q"),
                    issue("case_d02d0252", status="todo", assignee="agent-t")])
        self.assertEqual([r["agentId"] for r in snap["agentRuns"]],
                         ["agent-a", "agent-q"])
        self.assertIsNone(snap["agentRuns"][1]["lastSucceededAt"])

    def test_a_succeeded_run_without_a_finish_time_is_not_a_success(self):
        snap, _ = build(runs=[run("a", "agent-a", "succeeded",
                                  created_ago=20)])
        self.assertIsNone(snap["agentRuns"][0]["lastSucceededAt"])

    def test_a_failing_lane_stays_quiet_and_a_recovered_one_does_not(self):
        card = issue("case_8c5fe4cc", status="in_progress")
        stuck = {"id-case_8c5fe4cc": {"createdAt": at(45),
                              "body": "API Error: Request rejected (429)"}}
        failing = [run("f", "agent-a", "failed", created_ago=20,
                       finished_ago=10),
                   run("o", "agent-a", "succeeded", created_ago=120,
                       finished_ago=100)]
        snap, _ = build(issues=[card], comments=stuck, runs=failing)
        self.assertEqual(findings(snap, "stranded_429"), [])
        recovered = failing + [run("n", "agent-a", "succeeded",
                                   created_ago=9, finished_ago=3)]
        snap, _ = build(issues=[card], comments=stuck, runs=recovered)
        self.assertEqual(len(findings(snap, "stranded_429")), 2)

    def test_a_missing_agent_row_is_unknown_not_quiet(self):
        card = issue("case_8c5fe4cc", status="in_progress")
        stuck = {"id-case_8c5fe4cc": {"createdAt": at(45),
                              "body": "API Error: Request rejected (429)"}}
        snap, _ = build(issues=[card], comments=stuck, runs=[
            run("o", "agent-z", created_ago=9, finished_ago=3)])
        self.assertEqual(
            [r["agentId"] for r in snap["agentRuns"]], ["agent-a", "agent-z"])
        self.assertIsNone(snap["agentRuns"][0]["lastSucceededAt"])

    def test_hostile_finish_time_leaves_the_section_absent(self):
        snap, absent = build(runs=[run("a", finished_ago=3) | {
            "finishedAt": MARKER}])
        self.assertNotIn("agentRuns", snap)
        self.assertNotIn(MARKER, absent["agentRuns"])
        self.assertEqual(
            [r["reason"] for r in unknowns(snap, "stranded_429")],
            ["assignedInProgress snapshot missing",
             "agentRuns snapshot missing"])


class RedMainsTest(unittest.TestCase):

    RED = {"repo": "TogetherWeOwn/two-web-next", "signature": "ab12cd34",
           "dedupeTag": "red-main:v1:TogetherWeOwn/two-web-next:ab12cd34",
           "headSha": "deadbeef", "failingJobs": "build", "sigSource": "checks",
           "suggestedSeverity": "S1", "incidentExists": False}

    def test_a_red_main_is_passed_through_for_the_detector(self):
        snap, _ = build(redMains={"redMains": [self.RED]}, rc=1)
        self.assertEqual(snap["redMains"], [{
            "repo": "TogetherWeOwn/two-web-next", "signature": "ab12cd34",
            "incidentExists": False}])
        [hit] = findings(snap, "red_main")
        self.assertEqual(hit["severity"], "high")

    def test_a_tracked_red_main_is_info(self):
        snap, _ = build(redMains={"redMains": [
            self.RED | {"incidentExists": True}]}, rc=1)
        [hit] = findings(snap, "red_main")
        self.assertEqual(hit["severity"], "info")

    def test_a_clean_poll_is_an_empty_section(self):
        snap, absent = build(redMains={"redMains": []}, rc=0)
        self.assertEqual(snap["redMains"], [])
        self.assertNotIn("redMains", absent)
        self.assertEqual(findings(snap, "red_main"), [])

    def test_a_poll_that_did_not_see_every_repo_is_absent(self):
        for code in (3, 2, 4, -1, 127, None, True, False, "0"):
            with self.subTest(rc=code):
                snap, absent = build(redMains={"redMains": []}, rc=code)
                self.assertNotIn("redMains", snap)
                self.assertIn("redMains", absent)
                self.assertEqual(
                    [r["reason"] for r in unknowns(snap, "red_main")],
                    ["red-main snapshot missing"])

    def test_a_partial_snapshot_beside_exit_3_is_not_a_bill_of_health(self):
        snap, absent = build(redMains={"redMains": [self.RED]}, rc=3)
        self.assertNotIn("redMains", snap)
        self.assertIn("exited 3", absent["redMains"])

    def test_the_exit_code_and_the_entries_must_agree(self):
        for payload, code in (({"redMains": [self.RED]}, 0),
                              ({"redMains": []}, 1)):
            with self.subTest(code=code):
                snap, absent = build(redMains=payload, rc=code)
                self.assertNotIn("redMains", snap)
                self.assertIn("disagrees", absent["redMains"])

    def test_hostile_payloads_leave_the_section_absent(self):
        bad_row = {
            "row not an object": ["oops"],
            "repo missing": [{"signature": "s", "incidentExists": False}],
            "signature wrong type": [self.RED | {"signature": 5}],
            "incidentExists wrong type": [self.RED | {
                "incidentExists": MARKER}],
            "incidentExists missing": [{"repo": "o/r", "signature": "s"}],
        }
        for label, rows in bad_row.items():
            with self.subTest(label):
                snap, absent = build(redMains={"redMains": rows}, rc=1)
                self.assertNotIn("redMains", snap)
                self.assertNotIn(MARKER, absent["redMains"])
        for label, payload in {
                "payload is a list": [self.RED],
                "payload is a string": MARKER,
                "no redMains key": {"entries": []},
                "redMains a dict": {"redMains": {}}}.items():
            with self.subTest(label):
                snap, absent = build(redMains=payload, rc=1)
                self.assertNotIn("redMains", snap)
                self.assertNotIn(MARKER, absent["redMains"])


class WholeSnapshotTest(unittest.TestCase):

    def exports(self):
        stuck = {"createdAt": at(45),
                 "body": "API Error: Request rejected (429)"}
        return dict(
            issues=[issue("case_8c5fe4cc", status="in_progress"),
                    issue("case_d02d0252", status="todo", assignee=None, priority="low")],
            agents=[agent("agent-a")],
            runs=[sweep("r1", "id-case_8c5fe4cc", 20),
                  run("ok", "agent-a", created_ago=10, finished_ago=5)],
            comments={"id-case_8c5fe4cc": stuck},
            redMains={"redMains": []}, rc=0)

    def test_no_section_the_builder_builds_is_unknown(self):
        snap, absent = build(**self.exports())
        self.assertEqual(absent, {})
        # Raw issue exports do not prove the separate assignment evidence
        # sources required by the detector; that family intentionally stays
        # unknown until a provenance-backed handoff is supplied.
        built = ("agent_error", "churn", "stranded_429", "red_main")
        for family in built:
            with self.subTest(family):
                self.assertEqual(unknowns(snap, family), [])
        self.assertNotIn("assignmentEvidence", snap)

    def test_nothing_supplied_means_every_family_it_serves_says_unknown(self):
        snap, absent = build()
        self.assertEqual(snap, {"now": NOW_TEXT})
        self.assertEqual(set(absent), {name for name, _ in sb.SECTIONS})
        for family in ("agent_error", "assignment", "churn", "stranded_429",
                       "red_main"):
            with self.subTest(family):
                self.assertTrue(unknowns(snap, family))

    def test_sections_without_an_export_here_stay_absent(self):
        snap, _ = build(**self.exports())
        for key in ("hosts", "ciJobs", "queueAge", "secretHits", "reviews",
                    "quotaLanes", "supply", "pendingInteractions",
                    "autoscalerSlices", "orgStandard", "shadowEmit"):
            self.assertNotIn(key, snap)

    def test_building_changes_no_input_and_does_no_io(self):
        exports = self.exports()
        before = copy.deepcopy(exports)
        guard = AssertionError("builder touched the outside world")
        with mock.patch.object(builtins, "open", side_effect=guard), \
                mock.patch.object(socket, "socket", side_effect=guard), \
                mock.patch.object(subprocess, "Popen", side_effect=guard):
            build(**exports)
        self.assertEqual(exports, before)

    def test_the_snapshot_is_valid_strict_json(self):
        snap, _ = build(**self.exports())
        json.loads(json.dumps(snap, allow_nan=False))


class RunExportContractTest(unittest.TestCase):

    def test_complete_company_export_builds_run_sections(self):
        card = issue("case_8c5fe4cc", status="in_progress")
        sweep_run = sweep("wake", "id-case_8c5fe4cc", 20)
        success = run("success", "agent-a", "succeeded", created_ago=15,
                      issue_id="id-case_8c5fe4cc", finished_ago=5)
        retry = run("retry", "agent-a", "scheduled_retry",
                    issue_id="id-case_8c5fe4cc", created_ago=5)
        snap, absent = build(
            agents=[agent("lead"), agent("agent-a"), agent("agent-b")],
            issues=[card], runs=[sweep_run, success, retry],
            comments={"id-case_8c5fe4cc": None})
        for name in ("runEvents", "parks", "assignedInProgress", "agentRuns"):
            self.assertIn(name, snap)
            self.assertNotIn(name, absent)
        self.assertEqual(snap["runEvents"][0]["runId"], "wake")
        self.assertEqual(snap["parks"][0]["runCount6h"], 3)
        self.assertEqual(snap["assignedInProgress"][0]["queuedRunCount"], 1)
        self.assertEqual(snap["agentRuns"][0]["lastSucceededAt"], at(5))
        self.assertEqual(unknowns(snap, "churn"), [])
        self.assertEqual(unknowns(snap, "stranded_429"), [])

    def test_complete_per_agent_bundle_builds_run_sections(self):
        card = issue("case_8c5fe4cc", status="in_progress")
        bundle = {"byAgent": {
            "lead": [],
            "agent-a": [sweep("wake", "id-case_8c5fe4cc", 20),
                        run("success", "agent-a", "succeeded", 15,
                            issue_id="id-case_8c5fe4cc", finished_ago=5),
                        run("retry", "agent-a", "scheduled_retry",
                            issue_id="id-case_8c5fe4cc", created_ago=5)],
            "agent-b": []}}
        snap, absent = build(
            agents=[agent("lead"), agent("agent-a"), agent("agent-b")],
            issues=[card], runs=bundle, comments={"id-case_8c5fe4cc": None})
        for name in ("runEvents", "parks", "assignedInProgress", "agentRuns"):
            self.assertIn(name, snap)
            self.assertNotIn(name, absent)
        self.assertEqual(snap["runEvents"][0]["runId"], "wake")
        self.assertEqual(snap["parks"][0]["runCount6h"], 3)
        self.assertEqual(snap["assignedInProgress"][0]["queuedRunCount"], 1)
        self.assertEqual(snap["agentRuns"][0]["lastSucceededAt"], at(5))

    def test_combined_per_agent_rows_may_exceed_the_page_cap(self):
        bundle = {"byAgent": {
            "agent-a": [run(f"a-{n}", "agent-a", "failed", 7)
                        for n in range(600)],
            "agent-b": [run(f"b-{n}", "agent-b", "failed", 7)
                        for n in range(600)]}}
        snap, absent = build(agents=[agent("agent-a"), agent("agent-b")],
                             runs=bundle)
        for name in ("runEvents", "parks", "agentRuns"):
            self.assertIn(name, snap)
            self.assertNotIn(name, absent)
        self.assertEqual(len(snap["agentRuns"]), 2)

    def test_empty_per_agent_pages_are_measured(self):
        snap, absent = build(
            agents=[agent("agent-a"), agent("agent-b")],
            runs={"byAgent": {"agent-a": [], "agent-b": []}})
        for name in ("runEvents", "parks", "agentRuns"):
            self.assertEqual(snap[name], [])
            self.assertNotIn(name, absent)

    def test_full_per_agent_page_only_proves_covered_recent_sections(self):
        page = [run(str(n), "agent-a", "failed", 360)
                for n in range(sb.LIST_CAP)]
        bundle = {"byAgent": {"agent-a": page, "agent-b": []}}
        snap, absent = build(
            agents=[agent("agent-a"), agent("agent-b")],
            issues=[issue("case_8c5fe4cc", status="in_progress")], runs=bundle,
            comments={"id-case_8c5fe4cc": None})
        self.assertIn("runEvents", snap)
        self.assertIn("parks", snap)
        self.assertNotIn("agentRuns", snap)
        self.assertNotIn("assignedInProgress", snap)
        self.assertIn("full page", absent["agentRuns"])
        self.assertIn("full page", absent["assignedInProgress"])

    def test_uncapped_per_agent_page_proves_all_time_sections(self):
        page = [run(f"r-{n}", "agent-a", "failed", 360)
                for n in range(sb.LIST_CAP + 1)]
        bundle = {"byAgent": {"agent-a": page}}
        snap, absent = build(
            agents=[agent("agent-a")],
            issues=[issue("case_8c5fe4cc", status="in_progress")], runs=bundle,
            comments={"id-case_8c5fe4cc": None})
        for name in ("runEvents", "parks", "agentRuns", "assignedInProgress"):
            self.assertIn(name, snap)
            self.assertNotIn(name, absent)
        self.assertEqual(snap["agentRuns"], [
            {"agentId": "agent-a", "lastSucceededAt": None}])
        self.assertEqual(snap["assignedInProgress"][0]["liveRunCount"], 0)
        self.assertEqual(snap["assignedInProgress"][0]["queuedRunCount"], 0)
        self.assertEqual(unknowns(snap, "stranded_429"), [])

    def test_uncapped_pages_prove_recent_sections_without_six_hour_coverage(self):
        page = [run(f"r-{n}", "agent-a", "failed", 5)
                for n in range(sb.LIST_CAP + 1)]
        exports = (
            ("company", page),
            ("byAgent", {"byAgent": {"agent-a": page}}),
        )
        for label, runs in exports:
            with self.subTest(export=label):
                snap, absent = build(
                    agents=[agent("agent-a")],
                    issues=[issue("case_8c5fe4cc", status="in_progress")], runs=runs,
                    comments={"id-case_8c5fe4cc": None})
                for name in ("runEvents", "parks", "agentRuns",
                             "assignedInProgress"):
                    self.assertIn(name, snap)
                    self.assertNotIn(name, absent)
                self.assertEqual(snap["agentRuns"], [
                    {"agentId": "agent-a", "lastSucceededAt": None}])
                self.assertEqual(snap["assignedInProgress"][0]["liveRunCount"], 0)
                self.assertEqual(snap["assignedInProgress"][0]["queuedRunCount"], 0)
                self.assertEqual(unknowns(snap, "stranded_429"), [])

    def test_short_page_below_cap_proves_all_time_sections(self):
        page = [run(f"r-{n}", "agent-a", "failed", 5)
                for n in range(999)]
        exports = (page, {"byAgent": {"agent-a": page}})
        for runs in exports:
            with self.subTest(company_wide=isinstance(runs, list)):
                snap, absent = build(
                    agents=[agent("agent-a")],
                    issues=[issue("case_8c5fe4cc", status="in_progress")], runs=runs,
                    comments={"id-case_8c5fe4cc": None})
                for name in ("runEvents", "parks", "agentRuns",
                             "assignedInProgress"):
                    self.assertIn(name, snap)
                    self.assertNotIn(name, absent)

    def test_duplicate_rows_do_not_prove_uncapped_run_history(self):
        page = [run(f"r-{n}", "agent-a", "failed", 360)
                for n in range(sb.LIST_CAP)]
        page.append(copy.deepcopy(page[0]))
        exports = (page, {"byAgent": {"agent-a": page}})
        for runs in exports:
            with self.subTest(company_wide=isinstance(runs, list)):
                snap, absent = build(
                    agents=[agent("agent-a")],
                    issues=[issue("case_8c5fe4cc", status="in_progress")], runs=runs,
                    comments={"id-case_8c5fe4cc": None})
                self.assertIn("runEvents", snap)
                self.assertIn("parks", snap)
                for name in ("agentRuns", "assignedInProgress"):
                    self.assertNotIn(name, snap)
                    self.assertIn("full page", absent[name])

    def test_duplicate_rows_do_not_prove_recent_coverage(self):
        page = [run(f"r-{n}", "agent-a", "failed", 5)
                for n in range(sb.LIST_CAP)]
        page.append(copy.deepcopy(page[0]))
        exports = (page, {"byAgent": {"agent-a": page}})
        for runs in exports:
            with self.subTest(company_wide=isinstance(runs, list)):
                snap, absent = build(
                    agents=[agent("agent-a")], runs=runs)
                for name in ("runEvents", "parks"):
                    self.assertNotIn(name, snap)
                    self.assertIn("full page", absent[name])

    def test_a_full_per_agent_page_needs_six_hour_coverage(self):
        page = [run(str(n), "agent-a", "failed",
                    createdAt="2026-10-06T21:00:01Z")
                for n in range(sb.LIST_CAP)]
        snap, absent = build(
            agents=[agent("agent-a")], runs={"byAgent": {"agent-a": page}})
        for name in ("runEvents", "parks"):
            self.assertNotIn(name, snap)
            self.assertIn("full page", absent[name])

    def test_bad_per_agent_bundles_are_not_measured(self):
        roster = [agent("agent-a"), agent("agent-b")]
        base = {"agent-a": [], "agent-b": []}
        bundles = {
            "missing page": {"byAgent": {"agent-a": []}},
            "extra page": {"byAgent": base | {"unknown": []}},
            "wrong page type": {"byAgent": base | {"agent-a": None}},
            "issue-scoped pages unsupported": {"byIssue": {
                "id-case_8c5fe4cc": []}},
            "wrong row owner": {"byAgent": base | {
                "agent-a": [run("wrong", "agent-b")]}}}
        for label, bundle in bundles.items():
            with self.subTest(label):
                snap, absent = build(agents=roster, runs=bundle)
                for name in ("runEvents", "parks", "agentRuns"):
                    self.assertNotIn(name, snap)
                    self.assertIn(name, absent)

    def test_per_agent_bundle_needs_a_complete_agent_roster(self):
        snap, absent = build(runs={"byAgent": {"agent-a": []}})
        for name in ("runEvents", "parks", "agentRuns"):
            self.assertNotIn(name, snap)
            self.assertIn(name, absent)

    def test_live_or_queued_rows_need_issue_context(self):
        snap, absent = build(
            issues=[issue("case_8c5fe4cc", status="in_progress")],
            runs=[run("retry", status="scheduled_retry")],
            comments={"id-case_8c5fe4cc": None})
        self.assertNotIn("assignedInProgress", snap)
        self.assertIn("lacks contextSnapshot.issueId",
                      absent["assignedInProgress"])
        self.assertTrue(unknowns(snap, "stranded_429"))


class ReviewRegressionTest(unittest.TestCase):

    def stranded(self, runs, now=NOW, prefix=""):
        return sb.build_snapshot({
            "issues": [issue(status="in_progress")], "runs": runs,
            "comments": {"id-case_8c5fe4cc": {"createdAt": at(60), "body":
                         prefix + "API Error: Request rejected (429)"}}}, now)

    def test_unknown_agent_status_is_not_measured_healthy(self):
        snap, absent = build(agents=[agent("a", status="errror")])
        self.assertNotIn("agents", snap)
        self.assertIn("agents", absent)
        self.assertTrue(unknowns(snap, "agent_error"))

    def test_unknown_run_status_cannot_invent_zero_live_runs(self):
        for status in ("runnning", "new-status"):
            with self.subTest(status=status):
                snap, absent = self.stranded([
                    run("ok", created_ago=10, finished_ago=5),
                    run("live", status=status, issue_id="id-case_8c5fe4cc")])
                self.assertNotIn("assignedInProgress", snap)
                self.assertIn("assignedInProgress", absent)
                self.assertTrue(unknowns(snap, "stranded_429"))
                self.assertFalse(any(r["type"] == "proposal"
                                     for r in findings(snap, "stranded_429")))

    def test_unknown_active_run_status_cannot_invent_zero_live_runs(self):
        for active in ("running", {}, {"status": "runnning"}):
            with self.subTest(active=active):
                snap, absent = build(
                    issues=[issue(status="in_progress", activeRun=active)],
                    runs=[run("ok", created_ago=10, finished_ago=5)],
                    comments={"id-case_8c5fe4cc": {"createdAt": at(60), "body":
                              "API Error: Request rejected (429)"}})
                self.assertNotIn("assignedInProgress", snap)
                self.assertIn("assignedInProgress", absent)
                self.assertFalse(any(r["type"] == "proposal"
                                     for r in findings(snap, "stranded_429")))

    def test_all_export_statuses_are_recognized(self):
        agent_statuses = ("active", "paused", "idle", "running", "error",
                          "pending_approval", "terminated")
        run_statuses = ("queued", "scheduled_retry", "running", "succeeded",
                        "interrupted", "failed", "cancelled", "timed_out")
        snap, absent = build(
            agents=[agent(str(n), status) for n, status in
                    enumerate(agent_statuses)],
            runs=[run(str(n), status=status) for n, status in
                  enumerate(run_statuses)])
        self.assertNotIn("agents", absent)
        self.assertNotIn("parks", absent)
        self.assertEqual(len(snap["agents"]), len(agent_statuses))
        self.assertEqual(snap["parks"], [])

    def test_full_page_coverage_is_not_rounded_up_to_six_hours(self):
        edge = "2026-10-06T21:00:02Z"
        page = [run(str(n), createdAt=edge) for n in range(sb.LIST_CAP)]
        snap, absent = build(runs=page)
        for section in ("runEvents", "parks"):
            self.assertNotIn(section, snap)
            self.assertIn(section, absent)
        self.assertTrue(unknowns(snap, "churn"))
        exact = "2026-10-06T21:00:00Z"
        page[0] = page[0] | {"createdAt": exact}
        snap, _ = build(runs=page)
        self.assertIn("runEvents", snap)
        self.assertIn("parks", snap)

    def test_full_creation_page_cannot_prove_completion_history(self):
        page = [run(str(n), status="failed", created_ago=n / 50)
                for n in range(sb.LIST_CAP)]
        snap, absent = build(runs=page,
                             issues=[issue(status="in_progress")])
        self.assertNotIn("agentRuns", snap)
        self.assertIn("completion history", absent["agentRuns"])
        snap, _ = build(runs=[run("old", created_ago=60, finished_ago=5)])
        self.assertEqual(snap["agentRuns"][0]["lastSucceededAt"], at(5))

    def test_timestamp_precision_preserves_the_lane_threshold(self):
        now = parse_time("2026-10-07T03:00:00.900Z")
        for finish, fires in (("2026-10-07T02:45:00.800Z", False),
                              ("2026-10-07T02:45:00.900Z", True)):
            with self.subTest(finish=finish):
                snap, _ = self.stranded([
                    run("ok", created_ago=60, finishedAt=finish)], now)
                self.assertEqual(parse_time(snap["now"]), now)
                self.assertEqual(parse_time(snap["agentRuns"][0][
                    "lastSucceededAt"]), parse_time(finish))
                self.assertEqual(any(r["type"] == "proposal" for r in
                                     findings(snap, "stranded_429")), fires)

    def test_fractional_events_are_ordered_by_time_not_iso_text(self):
        snap, _ = build(runs=[
            sweep("later", "i1", 10,
                  createdAt="2026-10-07T02:50:00.100Z"),
            sweep("earlier", "i1", 10)])
        self.assertEqual([r["runId"] for r in snap["runEvents"]],
                         ["earlier", "later"])
        self.assertEqual([parse_time(r["at"]) for r in snap["runEvents"]],
                         [parse_time(at(10)),
                          parse_time("2026-10-07T02:50:00.100Z")])

    def test_comment_normalization_precedes_bounded_truncation(self):
        snap, _ = self.stranded([
            run("ok", created_ago=10, finished_ago=5)],
            prefix=" \t\n" * 200)
        body = snap["assignedInProgress"][0]["newestComment"]["body"]
        self.assertLessEqual(len(body), sb.COMMENT_KEEP_CHARS)
        self.assertTrue(body.startswith("API Error: Request rejected (429)"))
        self.assertTrue(any(r["type"] == "proposal"
                            for r in findings(snap, "stranded_429")))

    def test_conflicting_recovery_ids_refuse_in_either_order(self):
        first = {"issueId": "i1", "kind": "dispatch_stalled_issue",
                 "runId": "dup", "at": at(5)}
        for second in (first | {"issueId": "i2"}, first | {"at": at(6)}):
            for events in ([first, second], [second, first]):
                with self.subTest(events=events):
                    snap, absent = build(
                        runs=[sweep(str(n), "i1", 10 + n) for n in range(3)],
                        recovery=events)
                    self.assertNotIn("runEvents", snap)
                    self.assertIn("conflicting", absent["runEvents"])
                    self.assertFalse(any(r.get("severity") == "high"
                                         for r in findings(snap, "churn")))

    def test_recovery_identity_must_agree_with_a_known_run(self):
        event = {"issueId": "i1", "kind": "dispatch_stalled_issue",
                 "runId": "same", "at": at(5)}
        for changed in ({"issueId": "i2"}, {"at": at(6)}):
            with self.subTest(changed=changed):
                snap, absent = build(runs=[sweep("same", "i1", 5)],
                                     recovery=[event | changed])
                self.assertNotIn("runEvents", snap)
                self.assertIn("conflicting", absent["runEvents"])

    def test_unrepresentable_export_timestamps_stay_unknown(self):
        clock = "0001-01-01T00:00:00+01:00"
        snap, absent = build(runs=[run("old", createdAt=clock)])
        for section in ("runEvents", "parks", "agentRuns"):
            self.assertNotIn(section, snap)
            self.assertIn(section, absent)


class CliTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def export(self, name, value):
        with open(self.path(name), "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return self.path(name)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", BUILDER, *args], cwd=self.dir,
            env={"PATH": os.environ.get("PATH", "")}, capture_output=True,
            text=True, timeout=60)

    def good(self):
        return ["--issues", self.export("issues.json", [issue("case_8c5fe4cc")]),
                "--agents", self.export("agents.json", [agent("agent-a")]),
                "--runs", self.export("runs.json", [run("r1")]),
                "--now", NOW_TEXT]

    def test_writes_the_snapshot_and_reports_what_it_built(self):
        proc = self.cli("--out", self.path("snap.json"), *self.good(),
                        "--red-mains", self.export("red.json",
                                                   {"redMains": []}),
                        "--red-mains-rc", "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["built"], ["agentRuns", "agents", "issues",
                                           "parks", "redMains", "runEvents"])
        self.assertEqual(list(report["absent"]), ["assignedInProgress"])
        self.assertIn("absent assignedInProgress", proc.stderr)
        with open(self.path("snap.json"), encoding="utf-8") as handle:
            snap = json.load(handle)
        self.assertEqual(snap["now"], NOW_TEXT)
        self.assertEqual(snap["issues"][0]["identifier"], "case_8c5fe4cc")

    def test_fixture_exports_feed_both_clis_without_unknown_built_sections(self):
        fixtures = os.path.join(ROOT, "test", "fixtures",
                                "watchdog-snapshot-builder")
        args = ["--out", self.path("s.json"), "--now", NOW_TEXT,
                "--red-mains-rc", "1"]
        for flag in ("issues", "agents", "runs", "comments", "wake-comments",
                     "recovery-events", "red-mains"):
            args.extend(["--" + flag, os.path.join(fixtures, flag + ".json")])
        proc = self.cli(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["absent"], {})
        proc = subprocess.run(
            [sys.executable, "-B", DETECTORS, "--snapshot", self.path("s.json")],
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        # The issue export builds the raw issues section, not the separate
        # provenance-backed assignment evidence manifest.
        built = {"watchdog/" + name for name in
                 ("agent_error", "churn", "stranded_429", "red_main")}
        self.assertEqual([r for r in rows if r.get("detector") in built
                          and r.get("severity") == "unknown"], [])
        self.assertTrue(any(r.get("detector") == "watchdog/stranded_429"
                            and r.get("type") == "proposal" for r in rows))
        self.assertTrue(any(r.get("severity") == "unknown" for r in rows))

    def test_by_agent_fixture_builds_run_sections_through_both_clis(self):
        fixtures = os.path.join(ROOT, "test", "fixtures",
                                "watchdog-snapshot-builder")
        args = ["--out", self.path("by-agent.json"), "--now", NOW_TEXT]
        for flag, filename in (
                ("issues", "issues.json"), ("agents", "agents.json"),
                ("runs", "runs-by-agent.json"),
                ("comments", "comments.json"),
                ("red-mains", "red-mains.json")):
            args.extend(["--" + flag, os.path.join(fixtures, filename)])
        args.extend(["--red-mains-rc", "1"])
        proc = self.cli(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["absent"], {})
        self.assertTrue({"runEvents", "parks", "assignedInProgress", "agentRuns"}
                        <= set(report["built"]))
        proc = subprocess.run(
            [sys.executable, "-B", DETECTORS, "--snapshot",
             self.path("by-agent.json")], capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", "")}, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = [json.loads(line) for line in proc.stdout.splitlines()]
        unknown = {r["detector"] for r in records
                   if r.get("severity") == "unknown"}
        self.assertNotIn("watchdog/churn", unknown)
        self.assertNotIn("watchdog/stranded_429", unknown)

    def test_the_snapshot_feeds_the_detector_cli(self):
        self.assertEqual(self.cli("--out", self.path("s.json"),
                                  *self.good()).returncode, 0)
        proc = subprocess.run(
            [sys.executable, "-B", DETECTORS, "--snapshot",
             self.path("s.json")], capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", "")}, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = [json.loads(line) for line in proc.stdout.splitlines()]
        unknown = {r["detector"] for r in records
                   if r.get("severity") == "unknown"}
        self.assertNotIn("watchdog/churn", unknown)
        self.assertIn("watchdog/stranded_429", unknown)

    def test_an_unreadable_input_is_an_absent_section_not_a_crash(self):
        garbage = self.path("garbage.json")
        with open(garbage, "wb") as handle:
            handle.write(b"\xff\xfe{" + MARKER.encode())
        broken = self.path("broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{" + MARKER)
        for label, issues_path in (("missing file", self.path("nope.json")),
                                   ("not UTF-8", garbage),
                                   ("not JSON", broken)):
            with self.subTest(label):
                proc = self.cli("--out", self.path("s.json"), "--issues",
                                issues_path, "--now", NOW_TEXT)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("absent issues: cannot read --issues",
                              proc.stderr)
                self.assertNotIn(MARKER, proc.stderr + proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)
                with open(self.path("s.json"), encoding="utf-8") as handle:
                    self.assertNotIn("issues", json.load(handle))

    def test_red_mains_needs_the_pollers_exit_code(self):
        red = self.export("red.json", {"redMains": []})
        for args in (["--red-mains", red], ["--red-mains-rc", "0"]):
            with self.subTest(args=args):
                proc = self.cli("--out", self.path("s.json"), *args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("go together", proc.stderr)
                self.assertFalse(os.path.exists(self.path("s.json")))

    def test_usage_errors_exit_2_and_write_nothing(self):
        for label, args in (
                ("no --out", ["--issues", self.path("x.json")]),
                ("bad --now", ["--out", self.path("s.json"), "--now", "soon"]),
                ("unknown flag", ["--out", self.path("s.json"), "--net"]),
                ("rc not an int", ["--out", self.path("s.json"),
                                   "--red-mains", self.path("r.json"),
                                   "--red-mains-rc", "red"])):
            with self.subTest(label):
                proc = self.cli(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(os.path.exists(self.path("s.json")))

    def test_out_of_range_clocks_exit_2_without_a_traceback(self):
        for clock in ("0001-01-01T00:00:00+01:00",
                      "0001-01-01T05:59:59Z",
                      "9999-12-31T23:59:59-01:00"):
            with self.subTest(clock=clock):
                proc = self.cli("--out", self.path("s.json"), "--now", clock)
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(os.path.exists(self.path("s.json")))

    def test_an_unwritable_output_exits_2_and_leaves_no_temp_file(self):
        for label, out in (("directory", self.dir),
                           ("missing parent", self.path("no/such/s.json"))):
            with self.subTest(label):
                proc = self.cli("--out", out, *self.good())
                self.assertEqual(proc.returncode, 2)
                self.assertIn("cannot write snapshot", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(
                    [n for n in os.listdir(self.dir) if n.startswith(".snap")],
                    [])

    def test_the_output_replaces_an_older_snapshot_whole(self):
        with open(self.path("s.json"), "w", encoding="utf-8") as handle:
            handle.write(MARKER)
        self.assertEqual(
            self.cli("--out", self.path("s.json"), *self.good()).returncode, 0)
        with open(self.path("s.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["now"], NOW_TEXT)

    def test_two_runs_with_a_pinned_now_are_byte_identical(self):
        args = self.good()
        self.cli("--out", self.path("a.json"), *args)
        self.cli("--out", self.path("b.json"), *args)
        with open(self.path("a.json"), "rb") as a, \
                open(self.path("b.json"), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_the_clock_is_the_default_time_base(self):
        proc = self.cli("--out", self.path("s.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.path("s.json"), encoding="utf-8") as handle:
            self.assertIsNotNone(parse_time(json.load(handle)["now"]))


class RedMainHandoffTest(unittest.TestCase):
    """The host timer's envelope, read by the CEO routine through the CLI."""

    ROW = {"repo": "TogetherWeOwn/two-web-next", "signature": "ab12cd34",
           "dedupeTag": "red-main:v1:TogetherWeOwn/two-web-next:ab12cd34",
           "headSha": "deadbeef", "failingJobs": "build", "sigSource": "checks",
           "suggestedSeverity": "S1", "incidentExists": False}
    MEASURED = "2026-10-07T02:50:00Z"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def cli(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, "-B", BUILDER, *args], cwd=self.dir,
            env={"PATH": os.environ.get("PATH", "")}, capture_output=True,
            text=True, timeout=timeout)

    def envelope(self, **changes):
        doc = {"schema": "watchdog.red-main-handoff.v1",
               "measuredAt": self.MEASURED, "exitCode": 1,
               "redMains": [self.ROW]}
        doc.update(changes)
        return doc

    def handoff(self, doc=None, raw=None):
        with open(self.path("red-mains.json"), "wb") as handle:
            handle.write(raw if raw is not None
                         else json.dumps(doc).encode("utf-8"))
        return self.path("red-mains.json")

    def run_builder(self, *args, timeout=60):
        return self.cli("--out", self.path("snap.json"), "--now", NOW_TEXT,
                        "--red-mains-handoff", *args, timeout=timeout)

    def snap(self):
        with open(self.path("snap.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def red_main_records(self, snapshot):
        return [r for r in detect(snapshot)
                if r.get("detector") == "watchdog/red_main"]

    def test_a_fresh_red_handoff_is_read_with_its_source(self):
        path = self.handoff(self.envelope())
        proc = self.run_builder(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = self.snap()
        self.assertEqual(snap["redMains"], [{
            "repo": "TogetherWeOwn/two-web-next", "signature": "ab12cd34",
            "incidentExists": False}])
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(snap.get("redMainsSource"), {
            "source": "red-main-handoff", "measuredAt": self.MEASURED,
            "ref": f"sha256:{digest}"})
        [hit] = self.red_main_records(snap)
        self.assertEqual(hit["severity"], "high")
        self.assertEqual(hit["evidence"].get("provenance"),
                         snap.get("redMainsSource"))

    def test_a_fresh_green_handoff_is_a_measured_clean_section(self):
        proc = self.run_builder(self.handoff(
            self.envelope(exitCode=0, redMains=[])))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = self.snap()
        self.assertEqual(snap["redMains"], [])
        self.assertEqual(snap["redMainsSource"]["measuredAt"], self.MEASURED)
        self.assertEqual(self.red_main_records(snap), [])

    def test_the_thirty_minute_window_is_inclusive_on_both_sides(self):
        for at, fresh in (("2026-10-07T02:30:00Z", True),
                          ("2026-10-07T02:29:59Z", False),
                          ("2026-10-07T03:05:00Z", True),
                          ("2026-10-07T03:05:01Z", False)):
            with self.subTest(measuredAt=at):
                proc = self.run_builder(self.handoff(
                    self.envelope(measuredAt=at)))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                snap = self.snap()
                self.assertEqual("redMains" in snap, fresh)
                self.assertEqual("redMainsSource" in snap, fresh)

    def test_every_handoff_that_cannot_be_trusted_reads_as_unknown(self):
        for label, doc in (
                ("stale", self.envelope(measuredAt="2026-10-07T02:29:59Z")),
                ("dated after the firing",
                 self.envelope(measuredAt="2026-10-07T03:05:01Z")),
                ("unreadable time", self.envelope(measuredAt="soon")),
                ("no time", self.envelope(measuredAt=None)),
                ("another schema",
                 self.envelope(schema="watchdog.red-main-handoff.v0")),
                ("exit 3 beside entries", self.envelope(exitCode=3)),
                ("exit as text", self.envelope(exitCode="1")),
                ("exit as a boolean", self.envelope(exitCode=True)),
                ("green exit with entries", self.envelope(exitCode=0)),
                ("entries not a list",
                 self.envelope(redMains={"redMains": []})),
                ("entry without a signature",
                 self.envelope(redMains=[{"repo": "o/r"}]))):
            with self.subTest(label):
                proc = self.run_builder(self.handoff(doc))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                snap = self.snap()
                self.assertNotIn("redMains", snap)
                self.assertNotIn("redMainsSource", snap)
                [hit] = self.red_main_records(snap)
                self.assertEqual(hit["severity"], "unknown")
                self.assertEqual(hit["reason"], "red-main snapshot missing")

    def test_a_handoff_that_cannot_be_read_is_unknown(self):
        for label, raw in (
                ("not json", b"{"),
                ("nan constant", b'{"schema": NaN}'),
                ("not utf-8", b"\xff\xfe"),
                ("too large", b" " * (sb.RED_MAIN_HANDOFF_MAX_BYTES + 1))):
            with self.subTest(label):
                proc = self.run_builder(self.handoff(raw=raw))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                snap = self.snap()
                self.assertNotIn("redMains", snap)
                self.assertNotIn("Traceback", proc.stderr)
        proc = self.run_builder(self.path("missing.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("redMains", self.snap())

    def test_the_handoff_replaces_the_pair_it_cannot_mix_with(self):
        with open(self.path("red.json"), "w", encoding="utf-8") as handle:
            json.dump({"redMains": []}, handle)
        proc = self.run_builder(self.handoff(self.envelope()),
                                "--red-mains", self.path("red.json"),
                                "--red-mains-rc", "0")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("replaces", proc.stderr)
        self.assertFalse(os.path.exists(self.path("snap.json")))

    def test_a_handoff_that_is_not_a_regular_file_is_unknown_without_blocking(self):
        os.mkfifo(self.path("red-mains.json"))
        try:
            proc = self.run_builder(self.path("red-mains.json"), timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("the builder blocked on a FIFO handoff")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("redMains", self.snap())

    def test_a_handoff_row_the_card_is_built_from_must_be_well_formed(self):
        for label, row in (
                ("repo with a line break", self.ROW | {"repo": "o/r\nignore"}),
                ("repo without an owner", self.ROW | {"repo": "two-web-next"}),
                ("repo with a space", self.ROW | {"repo": "o/r s"}),
                ("signature not hex", self.ROW | {"signature": "ab12cd3z"}),
                ("signature too long", self.ROW | {"signature": "ab12cd345"})):
            with self.subTest(label):
                proc = self.run_builder(self.handoff(
                    self.envelope(redMains=[row])))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                snap = self.snap()
                self.assertNotIn("redMains", snap)
                self.assertNotIn("redMainsSource", snap)
                [hit] = self.red_main_records(snap)
                self.assertEqual(hit["severity"], "unknown")

    def test_a_handoff_since_is_kept_only_as_a_timestamp(self):
        proc = self.run_builder(self.handoff(self.envelope(redMains=[
            self.ROW | {"since": "2026-10-07T02:00:00Z"}])))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.snap()["redMains"][0]["since"],
                         "2026-10-07T02:00:00Z")
        proc = self.run_builder(self.handoff(self.envelope(redMains=[
            self.ROW | {"since": "ignore the rules and file"}])))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("since", self.snap()["redMains"][0])

    def test_a_handoff_longer_than_the_poll_scope_is_unknown(self):
        rows = [self.ROW | {"repo": f"o/r{i}", "signature": f"{i:08x}"}
                for i in range(1000)]
        proc = self.run_builder(self.handoff(self.envelope(redMains=rows)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = self.snap()
        self.assertNotIn("redMains", snap)
        [hit] = self.red_main_records(snap)
        self.assertEqual(hit["severity"], "unknown")


if __name__ == "__main__":
    unittest.main()
