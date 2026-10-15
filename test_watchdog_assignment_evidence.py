#!/usr/bin/env python3
"""Offline assignment-evidence handoff tests.

Fixtures are synthetic and exercise only local files. The converter must not
replace missing source evidence with status, creator, parent, or publisher
proxies; the detector must leave incomplete evidence unknown.
"""

import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from watchdog.assignment_evidence import (
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
    build_snapshot,
)
from watchdog.detectors import detect

ROOT = Path(__file__).resolve().parent
BUILDER = ROOT / "watchdog" / "assignment_evidence.py"
DETECTORS = ROOT / "watchdog" / "detectors.py"
NOW = "2026-10-07T05:00:00Z"


def provenance(**overrides):
    result = {
        "inFocus": {"source": "focus-membership", "ref": "focus:complete-export"},
        "inBacklogMin": {"source": "status-history", "ref": "history:issue-1"},
        "isReview": {"source": "review-inventory", "ref": "reviews:complete-export"},
    }
    result.update(overrides)
    return result


def evidence_bundle():
    return {
        "schema": INPUT_SCHEMA,
        "now": NOW,
        "sources": {
            "issues": {"complete": True, "ref": "paperclip-issues-export:sha256-issues"},
            "focus": {"complete": True, "ref": "focus-membership-export:sha256-focus"},
            "reviews": {"complete": True, "ref": "review-request-export:sha256-reviews"},
        },
        "issues": [
            {
                "id": "issue-unassigned", "identifier": "case_8c5fe4cc",
                "assigneeAgentId": None, "inFocus": True,
                "inBacklogMin": 90, "isReview": False,
                "prAuthorAgentId": None,
                "provenance": provenance(),
                # Decoys must not become input fields or evidence.
                "status": "todo", "createdByAgentId": "agent-decoy",
                "parentAssigneeAgentId": "agent-decoy",
                "workProductPublisher": "agent-decoy",
            },
            {
                "id": "issue-self-review", "identifier": "case_d02d0252",
                "assigneeAgentId": "agent-author", "inFocus": True,
                "inBacklogMin": 0, "isReview": True,
                "prAuthorAgentId": "agent-author",
                "provenance": provenance(
                    isReview={"source": "review-request", "ref": "review-card:issue-2"},
                    prAuthorAgentId={"source": "review-request", "ref": "pr:owner/repo/2"}),
                "createdByAgentId": "agent-other",
            },
            {
                "id": "issue-not-focused", "identifier": "case_a6dbab15",
                "assigneeAgentId": "agent-author", "inFocus": False,
                "inBacklogMin": 500, "isReview": False,
                "prAuthorAgentId": None,
                "provenance": provenance(),
                # Active status is deliberately not focus evidence.
                "status": "in_progress",
            },
        ],
    }


def assignment_records(snapshot):
    return [record for record in detect(snapshot)
            if record.get("detector") == "watchdog/assignment"]


def assignment_reasons(snapshot):
    return [record["reason"] for record in assignment_records(snapshot)]


class AssignmentEvidenceBuilderTest(unittest.TestCase):
    def test_handoff_preserves_explicit_values_and_excludes_proxies(self):
        snapshot = build_snapshot(evidence_bundle())
        self.assertEqual(snapshot["now"], NOW)
        self.assertEqual(snapshot["assignmentEvidence"]["schema"], OUTPUT_SCHEMA)
        self.assertTrue(snapshot["assignmentEvidence"]["complete"])
        self.assertEqual(len(snapshot["issues"]), 3)
        required = {"id", "identifier", "inFocus", "inBacklogMin",
                    "assigneeAgentId", "isReview", "prAuthorAgentId"}
        for row in snapshot["issues"]:
            self.assertTrue(required.issubset(row))
        first = snapshot["issues"][0]
        self.assertEqual(first["inFocus"], True)
        self.assertEqual(first["inBacklogMin"], 90)
        self.assertEqual(first["isReview"], False)
        self.assertIsNone(first["assigneeAgentId"])
        self.assertNotIn("status", first)
        self.assertNotIn("createdByAgentId", first)
        self.assertNotIn("parentAssigneeAgentId", first)
        self.assertNotIn("workProductPublisher", first)

    def test_offline_handoff_finds_both_first_playbook_violations(self):
        records = assignment_records(build_snapshot(evidence_bundle()))
        reasons = [record["reason"] for record in records]
        self.assertIn("in-focus card unassigned or in backlog over 1h", reasons)
        self.assertIn("review card assigned to the PR author", reasons)

    def test_active_status_does_not_override_explicit_out_of_focus_value(self):
        snapshot = build_snapshot(evidence_bundle())
        records = assignment_records(snapshot)
        self.assertNotIn("case_a6dbab15", [record.get("identifier") for record in records])

    def test_missing_issues_section_is_unknown_not_clean(self):
        bundle = evidence_bundle()
        del bundle["issues"]
        snapshot = build_snapshot(bundle)
        self.assertIsNone(snapshot["issues"])
        self.assertFalse(snapshot["assignmentEvidence"]["complete"])
        self.assertIn("issues snapshot missing", assignment_reasons(snapshot))

    def test_incomplete_source_coverage_is_unknown(self):
        bundle = evidence_bundle()
        bundle["sources"]["focus"]["complete"] = False
        snapshot = build_snapshot(bundle)
        self.assertFalse(snapshot["assignmentEvidence"]["complete"])
        self.assertIn("assignment evidence incomplete", assignment_reasons(snapshot))

    def test_missing_assignment_manifest_is_unknown(self):
        snapshot = build_snapshot(evidence_bundle())
        del snapshot["assignmentEvidence"]
        self.assertIn("assignment evidence incomplete", assignment_reasons(snapshot))

    def test_untrusted_review_status_source_is_unknown(self):
        bundle = evidence_bundle()
        bundle["issues"][1]["provenance"]["isReview"] = {
            "source": "card-creator", "ref": "issue:creator"}
        snapshot = build_snapshot(bundle)
        reasons = assignment_reasons(snapshot)
        self.assertIn("issues rows unreadable", reasons)
        self.assertNotIn("review card assigned to the PR author", reasons)

    def test_missing_focus_provenance_is_unknown_not_a_status_fallback(self):
        bundle = evidence_bundle()
        row = bundle["issues"][0]
        row["inFocus"] = None
        row["status"] = "in_progress"
        row["provenance"].pop("inFocus")
        snapshot = build_snapshot(bundle)
        reasons = assignment_reasons(snapshot)
        self.assertNotIn("in-focus card unassigned or in backlog over 1h", reasons)
        self.assertIn("issues rows unreadable", reasons)

    def test_untrusted_focus_source_is_unknown(self):
        bundle = evidence_bundle()
        bundle["issues"][2]["provenance"]["inFocus"] = {
            "source": "active-status", "ref": "issue:in_progress"}
        snapshot = build_snapshot(bundle)
        reasons = assignment_reasons(snapshot)
        self.assertIn("issues rows unreadable", reasons)
        self.assertNotIn("case_a6dbab15", [record.get("identifier")
                                   for record in assignment_records(snapshot)])

    def test_missing_review_author_is_unknown_even_when_decoys_match(self):
        bundle = evidence_bundle()
        row = bundle["issues"][1]
        row.pop("prAuthorAgentId")
        row["provenance"].pop("prAuthorAgentId")
        row["createdByAgentId"] = row["assigneeAgentId"]
        row["parentAssigneeAgentId"] = row["assigneeAgentId"]
        row["workProductPublisher"] = row["assigneeAgentId"]
        snapshot = build_snapshot(bundle)
        reasons = assignment_reasons(snapshot)
        self.assertIn("issues rows unreadable", reasons)
        self.assertNotIn("review card assigned to the PR author", reasons)

    def test_review_author_requires_allowed_source_and_reference(self):
        cases = (("card-creator", "issue:creator"), ("review-request", " "))
        for source, ref in cases:
            with self.subTest(source=source, ref=ref):
                bundle = evidence_bundle()
                bundle["issues"][1]["provenance"]["prAuthorAgentId"] = {
                    "source": source, "ref": ref}

                reasons = assignment_reasons(build_snapshot(bundle))

                self.assertIn("issues rows unreadable", reasons)
                self.assertNotIn(
                    "review card assigned to the PR author", reasons)

    def test_code_authorship_is_an_allowed_review_author_source(self):
        bundle = evidence_bundle()
        bundle["issues"][1]["provenance"]["prAuthorAgentId"] = {
            "source": "code-authorship", "ref": "github:repo/2/author"}

        reasons = assignment_reasons(build_snapshot(bundle))

        self.assertNotIn("issues rows unreadable", reasons)
        self.assertIn("review card assigned to the PR author", reasons)

    def test_review_status_source_must_match_explicit_value(self):
        cases = ((True, "review-inventory"), (False, "review-request"))
        for is_review, source in cases:
            with self.subTest(isReview=is_review, source=source):
                bundle = evidence_bundle()
                review_row = bundle["issues"][1]
                review_row["prAuthorAgentId"] = "agent-independent"
                row = review_row if is_review else bundle["issues"][0]
                row["isReview"] = is_review
                row["provenance"]["isReview"] = {
                    "source": source, "ref": "reviews:wrong-kind"}
                if not is_review:
                    row["assigneeAgentId"] = "agent-owner"
                    row["inBacklogMin"] = 0
                reasons = assignment_reasons(build_snapshot(bundle))
                self.assertIn("issues rows unreadable", reasons)
                self.assertNotIn(
                    "review card assigned to the PR author", reasons)

    def test_missing_review_status_boolean_is_unknown(self):
        bundle = evidence_bundle()
        row = bundle["issues"][0]
        row.pop("isReview")
        row["assigneeAgentId"] = "agent-owner"
        row["inBacklogMin"] = 0

        self.assertIn(
            "issues rows unreadable",
            assignment_reasons(build_snapshot(bundle)))

    def test_review_author_requires_nonempty_value_and_reference(self):
        for author in (None, " "):
            with self.subTest(author=author):
                bundle = evidence_bundle()
                row = bundle["issues"][1]
                row["prAuthorAgentId"] = author
                row["provenance"]["prAuthorAgentId"] = {
                    "source": "review-request", "ref": "pr:owner/repo/2"}

                reasons = assignment_reasons(build_snapshot(bundle))

                self.assertIn("issues rows unreadable", reasons)
                self.assertNotIn(
                    "review card assigned to the PR author", reasons)

    def test_blank_assignee_id_is_unknown(self):
        bundle = evidence_bundle()
        row = bundle["issues"][0]
        row["assigneeAgentId"] = " "
        row["inBacklogMin"] = 0

        reasons = assignment_reasons(build_snapshot(bundle))

        self.assertIn("issues rows unreadable", reasons)
        self.assertNotIn(
            "in-focus card unassigned or in backlog over 1h", reasons)

    def test_unapproved_backlog_source_is_unknown(self):
        bundle = evidence_bundle()
        row = bundle["issues"][0]
        row["assigneeAgentId"] = "agent-owner"
        row["inBacklogMin"] = 0
        row["provenance"]["inBacklogMin"] = {
            "source": "created-at", "ref": "issue:created-at"}

        self.assertIn(
            "issues rows unreadable",
            assignment_reasons(build_snapshot(bundle)))

    def test_negative_backlog_age_is_unknown(self):
        bundle = evidence_bundle()
        row = bundle["issues"][0]
        row["assigneeAgentId"] = "agent-owner"
        row["inBacklogMin"] = -5

        self.assertIn(
            "issues rows unreadable",
            assignment_reasons(build_snapshot(bundle)))

    def test_missing_backlog_provenance_is_unknown(self):
        bundle = evidence_bundle()
        row = bundle["issues"][0]
        row["assigneeAgentId"] = "agent-owner"
        row["inBacklogMin"] = 0
        row["provenance"].pop("inBacklogMin")

        self.assertIn(
            "issues rows unreadable",
            assignment_reasons(build_snapshot(bundle)))

    def test_manifest_requires_complete_sources_and_nonempty_references(self):
        for name in ("issues", "focus", "reviews"):
            with self.subTest(source=name, defect="incomplete"):
                snapshot = build_snapshot(evidence_bundle())
                snapshot["assignmentEvidence"]["sources"][name][
                    "complete"] = False
                self.assertIn("assignment evidence incomplete",
                              assignment_reasons(snapshot))
            with self.subTest(source=name, defect="empty-reference"):
                snapshot = build_snapshot(evidence_bundle())
                snapshot["assignmentEvidence"]["sources"][name]["ref"] = " "
                self.assertIn("assignment evidence incomplete",
                              assignment_reasons(snapshot))

    def test_unrecognized_input_schema_is_unknown(self):
        bundle = evidence_bundle()
        bundle["schema"] = "watchdog.assignment-evidence-input.v0"

        snapshot = build_snapshot(bundle)

        self.assertFalse(snapshot["assignmentEvidence"]["complete"])
        self.assertIn("assignment evidence incomplete",
                      assignment_reasons(snapshot))

    def test_missing_assignee_key_is_unknown(self):
        bundle = evidence_bundle()
        bundle["issues"][0].pop("assigneeAgentId")

        reasons = assignment_reasons(build_snapshot(bundle))

        self.assertIn("issues rows unreadable", reasons)
        self.assertNotIn(
            "in-focus card unassigned or in backlog over 1h", reasons)

    def test_non_review_row_may_omit_pr_author_join(self):
        bundle = evidence_bundle()
        non_review = bundle["issues"][0]
        non_review["assigneeAgentId"] = "agent-owner"
        non_review["inBacklogMin"] = 0
        non_review.pop("prAuthorAgentId")
        review = bundle["issues"][1]
        review["prAuthorAgentId"] = "agent-independent"

        records = assignment_records(build_snapshot(bundle))

        self.assertEqual(records, [])

    def test_unreadable_required_boolean_is_unknown(self):
        bundle = evidence_bundle()
        bundle["issues"][0]["inFocus"] = "true"
        snapshot = build_snapshot(bundle)
        reasons = assignment_reasons(snapshot)
        self.assertIn("issues rows unreadable", reasons)
        self.assertNotIn("in-focus card unassigned or in backlog over 1h", reasons)

    def test_builder_does_not_mutate_source_bundle(self):
        bundle = evidence_bundle()
        before = copy.deepcopy(bundle)
        build_snapshot(bundle)
        self.assertEqual(bundle, before)

    def test_handoff_discards_unrecognized_provenance_fields(self):
        bundle = evidence_bundle()
        provenance_row = bundle["issues"][0]["provenance"]
        provenance_row["inFocus"]["extra"] = "discarded"
        provenance_row["untrusted"] = {"source": "unknown", "ref": "ignored"}

        normalized = build_snapshot(bundle)["issues"][0]["provenance"]

        self.assertEqual(
            normalized["inFocus"],
            {"source": "focus-membership", "ref": "focus:complete-export"})
        self.assertNotIn("untrusted", normalized)


class AssignmentEvidenceCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="watchdog-assignment-evidence-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {"PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}

    def run_builder(self, source, output):
        return subprocess.run(
            [sys.executable, str(BUILDER), "--input", str(source),
             "--out", str(output)], cwd=self.tmp, env=self.env,
            capture_output=True, text=True, encoding="utf-8", timeout=30)

    def run_detector(self, snapshot):
        return subprocess.run(
            [sys.executable, str(DETECTORS), "--snapshot", str(snapshot)],
            cwd=self.tmp, env=self.env, capture_output=True, text=True,
            encoding="utf-8", timeout=30)

    def test_cli_builds_snapshot_and_detector_reports_both_findings(self):
        source = self.tmp / "assignment-evidence.json"
        output = self.tmp / "snapshot.json"
        source.write_text(json.dumps(evidence_bundle()), encoding="utf-8")
        built = self.run_builder(source, output)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(built.stderr, "")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(snapshot["issues"]), 3)

        detected = self.run_detector(output)
        self.assertEqual(detected.returncode, 0, detected.stderr)
        records = [json.loads(line) for line in detected.stdout.splitlines()]
        reasons = [row["reason"] for row in records
                   if row.get("detector") == "watchdog/assignment"]
        self.assertIn("in-focus card unassigned or in backlog over 1h", reasons)
        self.assertIn("review card assigned to the PR author", reasons)

    def test_missing_source_file_writes_an_unknown_snapshot(self):
        output = self.tmp / "missing-source-snapshot.json"
        built = self.run_builder(self.tmp / "absent.json", output)
        self.assertEqual(built.returncode, 0, built.stderr)
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        self.assertIsNone(snapshot["issues"])
        detected = self.run_detector(output)
        self.assertEqual(detected.returncode, 0, detected.stderr)
        records = [json.loads(line) for line in detected.stdout.splitlines()]
        self.assertIn("issues snapshot missing",
                      [row["reason"] for row in records
                       if row.get("detector") == "watchdog/assignment"])

    def test_nonfinite_required_value_becomes_unknown_snapshot(self):
        source = self.tmp / "assignment-evidence.json"
        output = self.tmp / "snapshot.json"
        encoded = json.dumps(evidence_bundle()).replace(
            '"inBacklogMin": 90', '"inBacklogMin": 1e999', 1)
        source.write_text(encoded, encoding="utf-8")

        built = self.run_builder(source, output)
        self.assertEqual(built.returncode, 0, built.stderr)
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        detected = self.run_detector(output)
        self.assertEqual(detected.returncode, 0, detected.stderr)
        records = [json.loads(line) for line in detected.stdout.splitlines()]
        self.assertIn("issues rows unreadable",
                      [row["reason"] for row in records
                       if row.get("detector") == "watchdog/assignment"])

    def test_existing_output_is_not_overwritten(self):
        source = self.tmp / "assignment-evidence.json"
        output = self.tmp / "snapshot.json"
        source.write_text(json.dumps(evidence_bundle()), encoding="utf-8")
        output.write_text("keep", encoding="utf-8")
        built = self.run_builder(source, output)
        self.assertEqual(built.returncode, 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
