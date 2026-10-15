#!/usr/bin/env python3
"""Offline tests for watchdog/sections.py and its call sites (case_a8404a2d).

The contract under test: a snapshot whose section has the wrong type, whose
row is not an object, or whose number cannot be read yields an ``unknown``
finding. It never passes silently and never raises. No credentials, no
network, no board writes; every case runs on fixtures.

The mutation control (MutationControlTest) swaps the helper for the legacy
behaviour one section at a time and requires the same probes to fail, so a
test that cannot see a reverted helper cannot pass here.
"""

import contextlib
import copy
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import watchdog.detectors as detectors
from watchdog.detectors import detect
from watchdog.sections import SectionRead, read_float, read_int

ROOT = os.path.dirname(os.path.abspath(__file__))
COOLIFY = "192.0.2.190"
NAN = float("nan")
INF = float("inf")

# The detector families this file owns. Records from detectors added later
# (other PRs add sections to the snapshot) are ignored, so a new section that
# reads as missing in this file's base snapshot cannot fail these tests.
FAMILIES = ("agent_error", "host_health", "ci_health", "assignment", "churn",
            "pending_interactions", "autoscaler_slices", "red_main",
            "secret_hit", "supply_famine")

COOLIFY_ROW = {"host": COOLIFY, "diskPct": 10, "load": 1.0, "vcpu": 4,
               "sustainedMin": 1}

# section -> (family, rows that each fire one ordinary finding, reason).
FIRING = {
    "agents": ("agent_error", [{
        "agentId": "a1", "status": "error",
        "errorSince": "2026-10-03T13:00:00Z", "subtype": "crash",
        "owningLead": "cto"}], "non-benign agent error past 10m"),
    "hosts": ("host_health", [dict(COOLIFY_ROW, diskPct=99)],
              "disk at or above 85%"),
    "ciJobs": ("ci_health", [{"repo": "r", "job": "j", "runtimeMin": 45,
                              "baselineMin": 20}],
               "CI job runtime above 2x baseline"),
    "queueAge": ("ci_health", [{"label": "self-hosted", "ageMin": 160}],
                 "runner queue age elevated"),
    "issues": ("assignment", [{
        "id": "iid-1", "identifier": "case_8c5fe4cc", "inFocus": True,
        "assigneeAgentId": None, "inBacklogMin": 90,
        "isReview": False, "prAuthorAgentId": None,
        "provenance": {
            "inFocus": {"source": "focus-membership", "ref": "fixture:focus"},
            "inBacklogMin": {"source": "status-history", "ref": "fixture:history"},
            "isReview": {"source": "review-inventory", "ref": "fixture:reviews"}}}],
               "in-focus card unassigned or in backlog over 1h"),
    "runEvents": ("churn", [{
        "issueId": "iid-1", "identifier": "case_8c5fe4cc",
        "kind": "dispatch_stalled_issue",
        "at": f"2026-10-03T13:0{i}:00Z"} for i in range(4)],
                  "card re-woken more than 3 times in 6h"),
    "parks": ("churn", [{"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                         "missingDispositionCount": 3}],
              "card parked for missing disposition more than twice"),
    "pendingInteractions": ("pending_interactions", [{
        "issueId": "iid-7", "identifier": "case_4ee01500", "interactionId": "int-1",
        "kind": "request_confirmation",
        "createdAt": "2026-10-03T13:00:00Z", "resolverAgentId": "agent-r"}],
                            "pending interaction older than 30m with "
                            "addressed resolver"),
    "autoscalerSlices": ("autoscaler_slices", [{
        "sliceId": "slice-1", "identifier": "case_9ae341a0", "status": "backlog",
        "assigneeAgentId": None,
        "unassignedSince": "2026-10-03T11:00:00Z"}],
                         "autoscaler slice unassigned for over 2h"),
    "redMains": ("red_main", [{"repo": "r", "signature": "s1",
                               "incidentExists": False}],
                 "red main with no incident"),
    "secretHits": ("secret_hit", [{
        "source": "run-log:x", "patternName": "ghp_*", "valueLen": 40,
        "at": "2026-10-03T13:00:00Z"}],
                   "secret pattern hit in run log or transcript"),
}
SECTIONS = list(FIRING)

# Values a collector can put in a section slot by mistake. Includes the ones
# the old `rows or []` shortcut read as an empty list (0, False, "", {}).
JUNK_SECTIONS = [5, 0, 2.5, True, False, "oops-sentinel", "", {},
                 {"x": 1}]
# Rows that are not objects.
JUNK_ROWS = ["row-sentinel", 5, None, [], True, [{"a": 1}]]
# Values for any single field of a row.
JUNK_FIELDS = [None, True, False, 0, -1, 1.5, NAN, INF, "", "x", [], {},
               [1], {"a": 1}]


def base(**overrides):
    """A well-formed snapshot that is silent for every family above."""
    snapshot = {"now": "2026-10-03T14:00:00Z", "agents": [],
                "hosts": [dict(COOLIFY_ROW)], "ciJobs": [], "queueAge": [],
                "issues": [],
                "assignmentEvidence": {
                    "schema": "watchdog.assignment-evidence.v1",
                    "complete": True,
                    "sources": {
                        "issues": {"complete": True, "ref": "fixture:issues"},
                        "focus": {"complete": True, "ref": "fixture:focus"},
                        "reviews": {"complete": True, "ref": "fixture:reviews"}}},
                "runEvents": [], "parks": [], "redMains": [],
                "secretHits": [], "pendingInteractions": [],
                "autoscalerSlices": [],
                "supply": {"readyNow": 12, "target": 12, "belowMin": 0.0,
                           "idleAgents": 2,
                           "censusAt": "2026-10-03T14:00:00Z"}}
    snapshot.update(overrides)
    return snapshot


def mine(records):
    return [r for r in records
            if r.get("detector") in {f"watchdog/{f}" for f in FAMILIES}]


def unknown_for(records, section, kind):
    """Records of this section's family reporting ``kind`` for ``section``."""
    family = FIRING[section][0]
    reason = f"{section} {kind}"
    return [r for r in records
            if r.get("detector") == f"watchdog/{family}"
            and r.get("severity") == "unknown"
            and r.get("reason") == reason
            and r["evidence"].get("section") == section]


def section_flagged(section, junk):
    records = detect(base(**{section: junk}))
    return len(unknown_for(records, section, "snapshot unreadable")) == 1


def rows_flagged(section, rows, expected):
    records = detect(base(**{section: rows}))
    found = unknown_for(records, section, "rows unreadable")
    return (len(found) == 1
            and found[0]["evidence"]["unreadableRows"] == expected)


class ReadNumberTest(unittest.TestCase):
    def test_read_float_accepts_what_float_always_accepted(self):
        for value, want in [(1, 1.0), (0, 0.0), (-3, -3.0), (2.5, 2.5),
                            ("85", 85.0), (" 2.5 ", 2.5)]:
            with self.subTest(value=value):
                self.assertEqual(read_float(value), want)

    def test_read_float_rejects_what_passes_every_threshold_silently(self):
        for value in [None, True, False, "lots", "", "nan", "inf", [], {},
                      NAN, INF, float("-inf"), 10 ** 400]:
            with self.subTest(value=repr(value)[:20]):
                self.assertIsNone(read_float(value))

    def test_read_int_keeps_the_legacy_coercion(self):
        for value, want in [(3, 3), (0, 0), ("3", 3), (3.9, 3), (-2, -2)]:
            with self.subTest(value=value):
                self.assertEqual(read_int(value), want)

    def test_read_int_rejects_what_used_to_raise_or_lie(self):
        # int(float("inf")) raised an uncaught OverflowError; True read as 1.
        for value in [None, True, False, "many", "", "3.5", [], {}, NAN,
                      INF]:
            with self.subTest(value=repr(value)):
                self.assertIsNone(read_int(value))


class SectionReadTest(unittest.TestCase):
    def finding(self, detector, reason, severity, owner, route, evidence,
                suggested):
        return {"detector": detector, "reason": reason,
                "severity": severity, "owner": owner, "route": route,
                "evidence": evidence, "suggested": suggested}

    def unknown(self, read):
        return read.unknown(self.finding, "agent_error", "cto", "route")

    def test_absent_section_is_the_callers_call_not_a_complaint(self):
        read = SectionRead("agents", None)
        self.assertEqual(list(read), [])
        self.assertEqual(self.unknown(read), [])

    def test_clean_list_reports_nothing(self):
        read = SectionRead("agents", [{"a": 1}, {"b": 2}])
        self.assertEqual(list(read), [{"a": 1}, {"b": 2}])
        self.assertEqual(self.unknown(read), [])

    def test_mistyped_section_is_one_finding_naming_only_the_type(self):
        for value in JUNK_SECTIONS:
            with self.subTest(value=repr(value)):
                read = SectionRead("agents", value)
                self.assertEqual(list(read), [])
                (found,) = self.unknown(read)
                self.assertEqual(found["severity"], "unknown")
                self.assertEqual(found["reason"], "agents snapshot unreadable")
                self.assertEqual(found["evidence"],
                                 {"section": "agents",
                                  "found": type(value).__name__})
                self.assertNotIn("sentinel", json.dumps(found))

    def test_non_dict_rows_are_counted_once_and_never_echoed(self):
        rows = [{"ok": 1}, "row-sentinel", 7, None]
        read = SectionRead("agents", rows)
        self.assertEqual(list(read), [{"ok": 1}])
        (found,) = self.unknown(read)
        self.assertEqual(found["reason"], "agents rows unreadable")
        self.assertEqual(found["evidence"],
                         {"section": "agents", "rows": 4,
                          "unreadableRows": 3})
        self.assertNotIn("sentinel", json.dumps(found))

    def test_flag_counts_a_row_once_however_many_fields_are_bad(self):
        read = SectionRead("hosts", [{"a": 1}, {"b": 2}])
        for index, _row in enumerate(read):
            if index == 0:
                read.flag()
                read.flag()
        self.assertEqual(read.unreadable, 1)
        (found,) = read.unknown(self.finding, "host_health", "o", "r")
        self.assertEqual(found["evidence"]["unreadableRows"], 1)

    def test_equal_rows_are_counted_separately(self):
        row = {"a": 1}
        read = SectionRead("hosts", [row, row, row])
        for _row in read:
            read.flag()
        self.assertEqual(read.unreadable, 3)

    def test_flag_outside_iteration_is_a_programming_error(self):
        with self.assertRaises(RuntimeError):
            SectionRead("hosts", [{"a": 1}]).flag()

    def test_flagged_and_non_dict_rows_share_one_finding(self):
        read = SectionRead("hosts", [{"a": 1}, "x"])
        for _row in read:
            read.flag()
        (found,) = read.unknown(self.finding, "host_health", "o", "r")
        self.assertEqual(found["evidence"],
                         {"section": "hosts", "rows": 2, "unreadableRows": 2})


class SectionSlotTest(unittest.TestCase):
    """A section of the wrong type: one unknown, no raise, no value echoed."""

    def test_well_formed_base_is_silent(self):
        self.assertEqual(mine(detect(base())), [])

    def test_every_slot_flags_every_junk_value(self):
        for section in SECTIONS:
            for junk in JUNK_SECTIONS:
                with self.subTest(section=section, junk=repr(junk)):
                    records = detect(base(**{section: junk}))
                    self.assertEqual(
                        len(unknown_for(records, section,
                                        "snapshot unreadable")), 1)
                    self.assertNotIn("sentinel", json.dumps(records))

    def test_the_card_probe_values(self):
        # The exact probes from the card's gap table.
        probes = [("agents", "oops"), ("issues", {"x": 1}),
                  ("redMains", {}), ("pendingInteractions", {}),
                  ("autoscalerSlices", "x"), ("agents", 5), ("hosts", 5),
                  ("secretHits", 5)]
        for section, junk in probes:
            with self.subTest(section=section, junk=junk):
                self.assertTrue(section_flagged(section, junk))

    def test_integer_section_never_raises(self):
        for section in SECTIONS:
            with self.subTest(section=section):
                detect(base(**{section: 5}))

    def test_a_mistyped_ci_section_does_not_hide_its_sibling(self):
        records = detect(base(ciJobs="oops-sentinel",
                              queueAge=FIRING["queueAge"][1]))
        self.assertEqual(
            len(unknown_for(records, "ciJobs", "snapshot unreadable")), 1)
        self.assertEqual(
            unknown_for(records, "queueAge", "snapshot unreadable"), [])
        self.assertIn("runner queue age elevated",
                      [r["reason"] for r in mine(records)])

    def test_a_mistyped_churn_section_does_not_hide_its_sibling(self):
        records = detect(base(runEvents={"x": 1},
                              parks=FIRING["parks"][1]))
        self.assertEqual(
            len(unknown_for(records, "runEvents", "snapshot unreadable")), 1)
        self.assertIn(FIRING["parks"][2],
                      [r["reason"] for r in mine(records)])

    def test_supply_section_still_reports_unreadable(self):
        for junk in (5, "x", [], True):
            with self.subTest(junk=junk):
                records = detect(base(supply=junk))
                self.assertIn("supply snapshot unreadable",
                              [r["reason"] for r in mine(records)])


class RowTest(unittest.TestCase):
    """A row that is not an object: one aggregate unknown with a count."""

    def test_non_dict_rows_aggregate_per_section(self):
        for section in SECTIONS:
            for bad in JUNK_ROWS:
                with self.subTest(section=section, bad=repr(bad)):
                    records = detect(base(**{section: [bad, bad, bad]}))
                    found = unknown_for(records, section, "rows unreadable")
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0]["evidence"],
                                     {"section": section, "rows": 3,
                                      "unreadableRows": 3})
                    self.assertNotIn("sentinel", json.dumps(records))

    def test_the_card_probe_row(self):
        self.assertTrue(rows_flagged("agents", ["error"], 1))

    def test_a_bad_row_never_suppresses_a_good_row(self):
        for section in SECTIONS:
            family, rows, reason = FIRING[section]
            with self.subTest(section=section):
                records = detect(base(**{section: rows + ["row-sentinel"]}))
                self.assertIn(reason, [r["reason"] for r in mine(records)])
                found = unknown_for(records, section, "rows unreadable")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["evidence"]["unreadableRows"], 1)

    def test_good_rows_alone_report_no_aggregate(self):
        for section in SECTIONS:
            family, rows, reason = FIRING[section]
            with self.subTest(section=section):
                records = detect(base(**{section: rows}))
                self.assertEqual(
                    unknown_for(records, section, "rows unreadable"), [])
                self.assertIn(reason, [r["reason"] for r in mine(records)])

    def test_every_firing_row_together_is_free_of_harness_unknowns(self):
        merged = {section: copy.deepcopy(FIRING[section][1])
                  for section in SECTIONS}
        records = mine(detect(base(**merged)))
        harness = [r for r in records
                   if r.get("severity") == "unknown"
                   and r["reason"].endswith("unreadable")]
        self.assertEqual(harness, [])


class HostFieldTest(unittest.TestCase):
    OTHER = {"host": "garm-1", "diskPct": 10, "load": 1.0, "vcpu": 4,
             "sustainedMin": 1}
    BAD = {"diskPct": ["lots-sentinel", None, NAN, INF, True, []],
           "load": ["lots-sentinel", None, NAN, INF, True, {}],
           "vcpu": ["lots-sentinel", None, NAN, INF, True, 0, -2],
           "sustainedMin": ["lots-sentinel", None, NAN, INF, True, []]}

    def test_unreadable_field_on_any_host_is_one_aggregate(self):
        for field, values in self.BAD.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    other = dict(self.OTHER, **{field: value})
                    records = detect(base(
                        hosts=[dict(COOLIFY_ROW), other, dict(other)]))
                    found = unknown_for(records, "hosts", "rows unreadable")
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0]["evidence"]["unreadableRows"], 2)
                    self.assertNotIn("sentinel", json.dumps(records))
                    reasons = [r["reason"] for r in mine(records)]
                    self.assertNotIn(
                        "required host with unreadable health fields",
                        reasons)

    def test_unreadable_field_on_the_required_host_is_unknown(self):
        for field, values in self.BAD.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    row = dict(COOLIFY_ROW, **{field: value})
                    records = detect(base(hosts=[row]))
                    found = [r for r in mine(records)
                             if r["reason"] ==
                             "required host with unreadable health fields"]
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0]["severity"], "unknown")
                    self.assertEqual(found[0]["evidence"],
                                     {"host": COOLIFY,
                                      "unreadableFields": [field]})
                    self.assertNotIn("sentinel", json.dumps(records))

    def test_the_card_probe_coolify_disk(self):
        records = detect(base(hosts=[dict(COOLIFY_ROW, diskPct="lots")]))
        self.assertIn("required host with unreadable health fields",
                      [r["reason"] for r in mine(records)])

    def test_every_unreadable_field_is_named_once_sorted(self):
        row = dict(COOLIFY_ROW, diskPct="x", load="y", vcpu=0)
        found = [r for r in mine(detect(base(hosts=[row])))
                 if r["reason"] == "required host with unreadable health "
                 "fields"]
        self.assertEqual(found[0]["evidence"]["unreadableFields"],
                         ["diskPct", "load", "vcpu"])

    def test_absent_sustained_minutes_still_means_zero(self):
        row = {k: v for k, v in COOLIFY_ROW.items() if k != "sustainedMin"}
        self.assertEqual(mine(detect(base(hosts=[row]))), [])

    def test_a_readable_row_still_fires_beside_an_unreadable_one(self):
        records = detect(base(hosts=[
            dict(COOLIFY_ROW, diskPct=99), dict(self.OTHER, load="x")]))
        reasons = [r["reason"] for r in mine(records)]
        self.assertIn("disk at or above 85%", reasons)
        self.assertIn("hosts rows unreadable", reasons)

    def test_unreadable_disk_does_not_hide_a_load_breach(self):
        row = dict(COOLIFY_ROW, diskPct="x", load=9.0, sustainedMin=10)
        reasons = [r["reason"] for r in mine(detect(base(hosts=[row])))]
        self.assertIn("load above 1.5x vCPU sustained", reasons)


class QueueFieldTest(unittest.TestCase):
    def test_unreadable_age_is_one_aggregate_not_a_skip(self):
        for value in ["long-sentinel", None, NAN, INF, True, [], {}]:
            with self.subTest(value=repr(value)):
                records = detect(base(queueAge=[
                    {"label": "self-hosted", "ageMin": value}] * 3))
                found = unknown_for(records, "queueAge", "rows unreadable")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["evidence"]["unreadableRows"], 3)
                self.assertNotIn("sentinel", json.dumps(records))

    def test_the_card_probe(self):
        self.assertTrue(rows_flagged("queueAge", [{"ageMin": "long"}], 1))

    def test_readable_rows_keep_their_verdicts(self):
        records = detect(base(queueAge=[
            {"label": "a", "ageMin": 5}, {"label": "b", "ageMin": 160},
            {"label": "c", "ageMin": "long"}]))
        self.assertEqual(
            [r["severity"] for r in mine(records)
             if r["reason"] == "runner queue age elevated"], ["high"])
        self.assertEqual(
            len(unknown_for(records, "queueAge", "rows unreadable")), 1)


class CiJobFieldTest(unittest.TestCase):
    def test_non_finite_numbers_are_unreadable_not_a_pass(self):
        # NaN and Infinity made every comparison False: a silent pass. They
        # now take the existing per-job unknown path.
        for field in ("runtimeMin", "baselineMin"):
            for value in (NAN, INF, True):
                with self.subTest(field=field, value=repr(value)):
                    job = {"repo": "r", "job": "j", "runtimeMin": 45,
                           "baselineMin": 20}
                    job[field] = value
                    reasons = [r["reason"]
                               for r in mine(detect(base(ciJobs=[job])))]
                    self.assertIn(
                        "CI job with unreadable runtime or baseline",
                        reasons)

    def test_existing_unknown_for_text_is_unchanged(self):
        job = {"repo": "r", "job": "j", "runtimeMin": "slow",
               "baselineMin": 20}
        reasons = [r["reason"] for r in mine(detect(base(ciJobs=[job])))]
        self.assertEqual(reasons,
                         ["CI job with unreadable runtime or baseline"])


class ChurnFieldTest(unittest.TestCase):
    def stalled(self, **overrides):
        event = {"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                 "kind": "dispatch_stalled_issue",
                 "at": "2026-10-03T13:00:00Z"}
        event.update(overrides)
        return event

    def test_unreadable_park_count_is_one_aggregate(self):
        for value in ["many-sentinel", None, INF, NAN, True, []]:
            with self.subTest(value=repr(value)):
                row = {"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                       "missingDispositionCount": value}
                records = detect(base(parks=[row, row]))
                found = unknown_for(records, "parks", "rows unreadable")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["evidence"]["unreadableRows"], 2)
                self.assertNotIn("sentinel", json.dumps(records))

    def test_the_card_probe_park(self):
        self.assertTrue(rows_flagged(
            "parks", [{"missingDispositionCount": "many"}], 1))

    def test_park_over_threshold_without_an_issue_is_unreadable(self):
        for missing in ({}, {"issueId": None}, {"issueId": ""}):
            with self.subTest(missing=missing):
                row = dict(missing, identifier="case_8c5fe4cc",
                           missingDispositionCount=3)
                records = detect(base(parks=[row]))
                self.assertEqual(
                    len(unknown_for(records, "parks", "rows unreadable")), 1)
                self.assertNotIn(FIRING["parks"][2],
                                 [r["reason"] for r in mine(records)])

    def test_park_under_threshold_or_absent_count_is_silent(self):
        for row in ({"issueId": "i", "missingDispositionCount": 2},
                    {"identifier": "case_8c5fe4cc", "missingDispositionCount": 0},
                    {"issueId": "i"}):
            with self.subTest(row=row):
                self.assertEqual(mine(detect(base(parks=[row]))), [])

    def test_stalled_event_without_a_usable_issue_is_one_aggregate(self):
        for overrides in ({"issueId": None}, {"issueId": ""},
                          {"issueId": 5}, {"issueId": []},
                          {"identifier": ["case_8c5fe4cc"]}, {"identifier": 7}):
            with self.subTest(overrides=overrides):
                event = self.stalled(**overrides)
                records = detect(base(runEvents=[event, event]))
                found = unknown_for(records, "runEvents", "rows unreadable")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["evidence"]["unreadableRows"], 2)

    def test_stalled_event_with_no_issue_key_at_all(self):
        event = self.stalled()
        del event["issueId"]
        self.assertTrue(rows_flagged("runEvents", [event], 1))

    def test_events_that_are_legitimately_ignored_stay_silent(self):
        ignored = [
            {"kind": "something_else", "at": "2026-10-03T13:00:00Z"},
            {"kind": "dispatch_stalled_issue",
             "at": "2026-10-02T01:00:00Z"},  # outside the 6h window
        ]
        self.assertEqual(mine(detect(base(runEvents=ignored))), [])

    def test_good_events_still_fire_beside_a_bad_one(self):
        events = FIRING["runEvents"][1] + [self.stalled(issueId=None)]
        records = detect(base(runEvents=events))
        self.assertIn(FIRING["runEvents"][2],
                      [r["reason"] for r in mine(records)])


class AssignmentFieldTest(unittest.TestCase):
    def issue(self, **overrides):
        row = {"id": "iid-1", "identifier": "case_8c5fe4cc", "inFocus": True,
               "assigneeAgentId": "agent-a", "inBacklogMin": 10,
               "isReview": False, "prAuthorAgentId": None,
               "provenance": {
                   "inFocus": {"source": "focus-membership",
                               "ref": "fixture:focus"},
                   "inBacklogMin": {"source": "status-history",
                                     "ref": "fixture:history"},
                   "isReview": {"source": "review-inventory",
                                "ref": "fixture:reviews"}}}
        row.update(overrides)
        return row

    def test_unreadable_backlog_minutes_is_one_aggregate(self):
        for value in ["a-while-sentinel", NAN, INF, True, [], {}]:
            with self.subTest(value=repr(value)):
                row = self.issue(inBacklogMin=value)
                records = detect(base(issues=[row, dict(row, id="iid-2")]))
                found = unknown_for(records, "issues", "rows unreadable")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["evidence"]["unreadableRows"], 2)
                self.assertNotIn("sentinel", json.dumps(records))

    def test_unreadable_backlog_still_fires_the_unassigned_finding(self):
        row = self.issue(inBacklogMin="x", assigneeAgentId=None)
        reasons = [r["reason"] for r in mine(detect(base(issues=[row])))]
        self.assertIn(FIRING["issues"][2], reasons)
        self.assertIn("issues rows unreadable", reasons)

    def test_absent_or_null_backlog_minutes_is_unknown(self):
        for row in (self.issue(inBacklogMin=None),
                    {k: v for k, v in self.issue().items()
                     if k != "inBacklogMin"}):
            with self.subTest(row=row):
                reasons = [r["reason"]
                           for r in mine(detect(base(issues=[row])))]
                self.assertIn("issues rows unreadable", reasons)
                self.assertNotIn(FIRING["issues"][2], reasons)

    def test_in_focus_row_without_an_id_is_unreadable(self):
        for row in (self.issue(id=None), self.issue(id="")):
            with self.subTest(row=row):
                records = detect(base(issues=[row]))
                self.assertEqual(
                    len(unknown_for(records, "issues", "rows unreadable")), 1)

    def test_out_of_focus_rows_are_not_judged_so_not_flagged(self):
        row = self.issue(inFocus=False, id=None, inBacklogMin="x")
        self.assertEqual(mine(detect(base(issues=[row]))), [])


class SupplyFieldTest(unittest.TestCase):
    def test_non_finite_and_bool_readings_are_unreadable(self):
        for field in ("readyNow", "target", "belowMin"):
            for value in (INF, NAN, True, "x", None):
                with self.subTest(field=field, value=repr(value)):
                    supply = {"readyNow": 0, "target": 12, "belowMin": 35.0}
                    supply[field] = value
                    reasons = [r["reason"]
                               for r in mine(detect(base(supply=supply)))]
                    self.assertEqual(reasons, ["supply snapshot unreadable"])

    def test_unreadable_idle_count_stays_evidence_only(self):
        for value in (INF, "x", True, None):
            with self.subTest(value=repr(value)):
                supply = {"readyNow": 0, "target": 12, "belowMin": 35.0,
                          "idleAgents": value}
                records = [r for r in mine(detect(base(supply=supply)))
                           if r["type"] == "finding"]
                self.assertEqual(records[0]["evidence"]["idleAgents"], 0)


class NeverRaiseTest(unittest.TestCase):
    """Property check: no field of any row, set to anything, raises."""

    def test_every_field_of_every_firing_row_survives_junk(self):
        calls = 0
        for section in SECTIONS:
            rows = FIRING[section][1]
            for field in rows[0]:
                for junk in JUNK_FIELDS:
                    mutated = copy.deepcopy(rows)
                    mutated[0][field] = junk
                    with self.subTest(section=section, field=field,
                                      junk=repr(junk)):
                        records = detect(base(**{section: mutated}))
                        json.dumps(records)
                    calls += 1
        self.assertGreater(calls, 500)

    def test_every_supply_field_survives_junk(self):
        for field in ("readyNow", "target", "belowMin", "idleAgents",
                      "censusAt"):
            for junk in JUNK_FIELDS:
                supply = {"readyNow": 0, "target": 12, "belowMin": 35.0,
                          "idleAgents": 2, "censusAt": "2026-10-03T14:00:00Z"}
                supply[field] = junk
                with self.subTest(field=field, junk=repr(junk)):
                    json.dumps(detect(base(supply=supply)))

    def test_whole_section_junk_never_raises_in_any_combination(self):
        for section in SECTIONS + ["supply"]:
            for junk in JUNK_SECTIONS + [[], [[]], [None], [[], 5, "x"]]:
                with self.subTest(section=section, junk=repr(junk)):
                    json.dumps(detect(base(**{section: junk})))


class Legacy:
    """What every detector did before the helper: dict rows, silently.

    ``value or []`` read 0, False, "" and {} as no rows; an integer slot
    raised from the ``for`` loop; unreadable rows were skipped in silence.
    """

    def __init__(self, section, value):
        self.rows = [row for row in iter(value or []) if isinstance(row, dict)]

    def __iter__(self):
        return iter(self.rows)

    def flag(self):
        pass

    def unknown(self, finding, detector, owner, route):
        return []


@contextlib.contextmanager
def reverted(section):
    """Revert the helper in the one detector that reads ``section``."""
    real = detectors.SectionRead

    def factory(name, value):
        return Legacy(name, value) if name == section else real(name, value)

    with mock.patch.object(detectors, "SectionRead", factory):
        yield


def mutated_detectors(old, new):
    """Load a copy of watchdog/detectors.py with one source line mutated."""
    with open(detectors.__file__, encoding="utf-8") as handle:
        source = handle.read()
    if source.count(old) != 1:
        raise AssertionError(f"mutation anchor not unique in source: {old!r}")
    module = types.ModuleType("watchdog_detectors_mutant")
    module.__file__ = detectors.__file__
    exec(compile(source.replace(old, new), detectors.__file__, "exec"),
         module.__dict__)
    return module


def survives(probe, *args):
    try:
        return probe(*args)
    except Exception:
        return False


# One representative unreadable row per section; each is flagged today.
ROW_PROBES = {
    "agents": ["error"],
    "hosts": [dict(COOLIFY_ROW), {"host": "garm-1", "diskPct": "lots"}],
    "ciJobs": ["row"],
    "queueAge": [{"label": "q", "ageMin": "long"}],
    "issues": [{"id": "i", "inFocus": True, "assigneeAgentId": "a",
                "inBacklogMin": "x"}],
    "runEvents": [{"kind": "dispatch_stalled_issue",
                   "at": "2026-10-03T13:00:00Z"}],
    "parks": [{"issueId": "i", "missingDispositionCount": "many"}],
    "pendingInteractions": ["row"],
    "autoscalerSlices": ["row"],
    "redMains": ["row"],
    "secretHits": ["row"],
}


class MutationControlTest(unittest.TestCase):
    """Reverting the helper in one detector must fail its probes."""

    def test_probes_pass_on_the_real_helper(self):
        for section in SECTIONS:
            with self.subTest(section=section):
                self.assertTrue(section_flagged(section, "oops-sentinel"))
                self.assertTrue(section_flagged(section, 5))
                self.assertTrue(rows_flagged(section, ROW_PROBES[section], 1))

    def test_reverting_the_helper_fails_the_section_probe(self):
        for section in SECTIONS:
            for junk in JUNK_SECTIONS:
                with self.subTest(section=section, junk=repr(junk)):
                    with reverted(section):
                        self.assertFalse(
                            survives(section_flagged, section, junk))

    def test_reverting_the_helper_fails_the_row_probe(self):
        for section in SECTIONS:
            with self.subTest(section=section):
                with reverted(section):
                    self.assertFalse(survives(
                        rows_flagged, section, ROW_PROBES[section], 1))

    def test_a_revert_is_confined_to_its_own_detector(self):
        with reverted("agents"):
            self.assertFalse(survives(section_flagged, "agents", 5))
            self.assertTrue(survives(section_flagged, "hosts", 5))
            self.assertTrue(survives(section_flagged, "secretHits", 5))

    def test_dropping_the_required_host_branch_is_caught(self):
        # The required-host verdict is its own branch, not part of the
        # helper, so it gets its own source-level mutant.
        row = dict(COOLIFY_ROW, diskPct="lots")

        def required_host_unknown(module):
            return "required host with unreadable health fields" in [
                r["reason"] for r in module.detect(base(hosts=[row]))]

        self.assertTrue(required_host_unknown(detectors))
        mutant = mutated_detectors("elif required in unreadable:",
                                   "elif False:")
        self.assertFalse(required_host_unknown(mutant))

    def test_dropping_the_aggregate_call_is_caught(self):
        mutant = mutated_detectors(
            'out.extend(queue_rows.unknown(_finding, "ci_health", "devops", '
            'CEO_ROUTE))', "pass")

        def queue_flagged(module):
            records = module.detect(base(queueAge=[{"ageMin": "long"}]))
            return any(r.get("reason") == "queueAge rows unreadable"
                       for r in records)

        self.assertTrue(queue_flagged(detectors))
        self.assertFalse(queue_flagged(mutant))


class CliTest(unittest.TestCase):
    """The CLI never prints a traceback for a mistyped snapshot."""

    SNAPSHOT = base(agents=5, hosts=5, secretHits=5, issues="oops-sentinel",
                    queueAge=[{"ageMin": "long"}])

    def run_cli(self, argv, cwd):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snapshot.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.SNAPSHOT, handle)
            return subprocess.run(
                [sys.executable, *argv, "--snapshot", path], cwd=cwd,
                capture_output=True, text=True, timeout=60)

    def check(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(lines[-1]["type"], "summary")
        reasons = {line.get("reason") for line in lines}
        for section in ("agents", "hosts", "secretHits", "issues"):
            self.assertIn(f"{section} snapshot unreadable", reasons)
        self.assertIn("queueAge rows unreadable", reasons)
        self.assertNotIn("sentinel", result.stdout)
        return result.stdout

    def test_script_mode_from_any_directory(self):
        # `python3 watchdog/detectors.py` puts watchdog/ on sys.path, not the
        # repo root, so the sibling import must work without the package.
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.run_cli(
                [os.path.join(ROOT, "watchdog", "detectors.py")], elsewhere)
        self.check(result)

    def test_module_mode_matches_script_mode(self):
        script = self.check(self.run_cli(
            [os.path.join(ROOT, "watchdog", "detectors.py")], ROOT))
        module = self.check(self.run_cli(["-m", "watchdog.detectors"], ROOT))
        self.assertEqual(script, module)


if __name__ == "__main__":
    unittest.main()
