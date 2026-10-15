#!/usr/bin/env python3
"""Offline tests for watchdog/delivery.py (case_07aa689b).

No credentials, no network, no board writes. Detector rows come from the
real watchdog.detectors.detect(); writer rows come from the real
recovery_writer.decide() plus Decision.receipt(), mirroring dry-run
main() exactly. Timestamps are normalized to fixed values so ages and
latencies pin deterministically; --now is always explicit.
"""

import datetime
import json
import os
import re
import tempfile
import unittest

import recovery_writer as rw
from watchdog import delivery as dl
from watchdog.detectors import CEO_ROUTE, detect

T0 = datetime.datetime(2026, 10, 3, 14, 0, 0,
                       tzinfo=datetime.timezone.utc)
T0_ISO = "2026-10-03T14:00:00Z"
T0_PLUS_90 = "2026-10-03T14:01:30Z"


def snap(**overrides):
    base = {"now": T0_ISO, "agents": [], "hosts": [],
            "ciJobs": [], "queueAge": [], "issues": [], "runEvents": [],
            "parks": [], "redMains": [], "secretHits": []}
    base.update(overrides)
    return base


def assignment_evidence():
    return {
        "schema": "watchdog.assignment-evidence.v1",
        "complete": True,
        "sources": {
            "issues": {"complete": True, "ref": "test:issues"},
            "focus": {"complete": True, "ref": "test:focus"},
            "reviews": {"complete": True, "ref": "test:reviews"},
        },
    }


def assignment_snap(**overrides):
    return snap(assignmentEvidence=assignment_evidence(), **overrides)


def focused_issue(issue_id, identifier, assignee=None, backlog_min=90,
                   is_review=False, pr_author=None):
    review_source = "review-request" if is_review else "review-inventory"
    provenance = {
        "inFocus": {
            "source": "focus-membership", "ref": f"test:focus:{issue_id}"},
        "inBacklogMin": {
            "source": "status-history", "ref": f"test:history:{issue_id}"},
        "isReview": {
            "source": review_source, "ref": f"test:review:{issue_id}"},
    }
    row = {
        "id": issue_id,
        "identifier": identifier,
        "inFocus": True,
        "inBacklogMin": backlog_min,
        "assigneeAgentId": assignee,
        "isReview": is_review,
        "provenance": provenance,
    }
    if is_review:
        row["prAuthorAgentId"] = pr_author
        provenance["prAuthorAgentId"] = {
            "source": "review-request", "ref": f"test:pr:{identifier}"}
    return row


def live_unassigned(issue_id="iid-1", identifier="case_8c5fe4cc"):
    return {"id": issue_id, "identifier": identifier, "status": "backlog",
            "assigneeAgentId": None}


def dry_run_receipt(proposal, live):
    """Mirror recovery_writer.main() dry-run receipt logic exactly."""
    decision = rw.decide(proposal, live, rw.DEFAULT_CEO_AGENT_ID)
    apply_outcome = ("dry_run_held" if decision.outcome == "decided"
                     else decision.outcome)
    return decision.receipt("dry-run", apply_outcome)


def with_stamps(row, emitted=None, at=None):
    row = dict(row)
    if emitted is not None:
        row["emittedAt"] = emitted
    if at is not None:
        row["at"] = at
    return row


def proposals_of(records):
    return [r for r in records if r.get("type") == "proposal"]


def run_writer(proposals):
    """Drive recovery_writer.main() offline and return (exit, rows)."""
    import io
    from contextlib import redirect_stdout
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "proposals.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for row in proposals:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    out = io.StringIO()
    with redirect_stdout(out):
        code = rw.main(["--proposals", path])
    rows = [json.loads(line) for line in out.getvalue().splitlines()
            if line.strip()]
    return code, rows


def run_delivery(detector_rows, writer_rows, now_iso, stall_after_min=60):
    tmp = tempfile.mkdtemp()
    det_path = os.path.join(tmp, "detectors.jsonl")
    rec_path = os.path.join(tmp, "receipts.jsonl")
    with open(det_path, "w", encoding="utf-8") as handle:
        for row in detector_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    with open(rec_path, "w", encoding="utf-8") as handle:
        for row in writer_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        code = dl.main(["--detectors", det_path, "--receipts", rec_path,
                        "--now", now_iso,
                        "--stall-after-min", str(stall_after_min)])
    rows = [json.loads(line) for line in out.getvalue().splitlines()
            if line.strip()]
    return code, rows


def by_type(rows, kind):
    return [r for r in rows if r.get("type") == kind]


class FingerprintTest(unittest.TestCase):
    def test_every_phase1_proposal_matches_writer_fingerprint(self):
        records = detect(assignment_snap(
            issues=[focused_issue("iid-1", "case_8c5fe4cc", is_review=True,
                                  pr_author="agent-x")],
            runEvents=[{"issueId": "iid-1", "identifier": "case_8c5fe4cc",
                        "kind": "dispatch_stalled_issue",
                        "at": "2026-10-03T13:10:00Z"}] * 4,
            hosts=[{"host": "192.0.2.190", "diskPct": 10,
                    "load": 1.0, "vcpu": 4, "sustainedMin": 1}]))
        proposals = proposals_of(records)
        self.assertGreaterEqual(len(proposals), 2)
        for proposal in proposals:
            live = live_unassigned(proposal["issueId"],
                                   proposal.get("identifier") or "?")
            decision = rw.decide(proposal, live, rw.DEFAULT_CEO_AGENT_ID)
            self.assertEqual(dl.proposal_fingerprint(proposal),
                             decision.fingerprint)


class CleanCycleTest(unittest.TestCase):
    def test_all_delivered_reports_full_success_and_latency(self):
        records = detect(assignment_snap(
            issues=[focused_issue("iid-1", "case_8c5fe4cc")],
            hosts=[{"host": "192.0.2.190", "diskPct": 10,
                    "load": 1.0, "vcpu": 4, "sustainedMin": 1}]))
        proposals = [with_stamps(p, emitted=T0_ISO)
                     for p in proposals_of(records)]
        receipts = [with_stamps(
            dry_run_receipt(p, live_unassigned()), at=T0_PLUS_90)
            for p in proposals]
        code, rows = run_delivery(proposals, receipts, T0_PLUS_90)
        self.assertEqual(code, 0)
        metrics = by_type(rows, "delivery_metric")
        self.assertEqual(len(metrics), 1)
        metric = metrics[0]
        self.assertEqual(metric["detector"], "watchdog/assignment")
        self.assertEqual(metric["emitted"], metric["delivered"])
        self.assertEqual(metric["successRate"], 1.0)
        self.assertEqual(metric["missing"], 0)
        self.assertEqual(metric["failed"], 0)
        self.assertEqual(metric["latencySamples"], metric["emitted"])
        self.assertEqual(metric["medianLatencyMs"], 90000.0)
        self.assertEqual(metric["maxLatencyMs"], 90000.0)
        self.assertEqual(by_type(rows, "delivery_alert"), [])
        summary = by_type(rows, "delivery_summary")[0]
        self.assertEqual(summary["alertsHigh"], 0)
        self.assertEqual(summary["emitted"], summary["delivered"])

    def test_refused_is_counted_not_alerted(self):
        # Phase-1 mutation:"none" proposals never refuse, so exercise the
        # refused path with a real-mutation proposal against live state
        # that no longer needs the repair.
        proposal = {"type": "proposal", "detector": "watchdog/assignment",
                    "reason": "in-focus card needs an owner",
                    "mutation": "assign_blocker_owner",
                    "issueId": "iid-2", "identifier": "case_d02d0252",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        busy = live_unassigned("iid-2", "case_d02d0252")
        busy["assigneeAgentId"] = "agent-busy"
        receipt = with_stamps(dry_run_receipt(proposal, busy),
                              at=T0_PLUS_90)
        self.assertTrue(receipt["decision"].startswith("refused:"))
        code, rows = run_delivery([proposal], [receipt], T0_PLUS_90)
        self.assertEqual(code, 0)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["delivered"], metric["emitted"])
        self.assertEqual(metric["refused"], metric["emitted"])
        self.assertEqual(by_type(rows, "delivery_alert"), [])

    def test_rate_limited_receipt_joins_on_stamped_key(self):
        # The cap rewrites reason and mutation, so the receipt fingerprint
        # no longer equals the proposal. The writer stamps the pre-rewrite
        # value; a sibling proposal that shares detector, issue and
        # identifier but not reason must not absorb that receipt.
        proposal = {"type": "proposal", "detector": "watchdog/assignment",
                    "reason": "operator must decide",
                    "mutation": "operator_decision",
                    "issueId": "iid-9", "identifier": "case_f052904a",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        decision = rw.decide(proposal, live_unassigned("iid-9", "case_f052904a"),
                             rw.DEFAULT_CEO_AGENT_ID)
        self.assertEqual(decision.outcome, "decided")
        receipt = with_stamps(
            rw.rate_limited_receipt(decision, "apply", 1), at=T0_PLUS_90)
        self.assertEqual(receipt["decision"], "rate_limited")
        self.assertEqual(receipt["mutation"], "none")
        self.assertNotEqual(receipt["fingerprint"], decision.fingerprint)
        self.assertEqual(receipt["proposalFingerprint"],
                         decision.fingerprint)
        self.assertEqual(dl.proposal_fingerprint(proposal),
                         decision.fingerprint)
        code, rows = run_delivery([proposal], [receipt], T0_PLUS_90)
        self.assertEqual(code, 0)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["deferred"], 1)
        self.assertEqual(metric["missing"], 0)
        self.assertEqual(metric["failed"], 0)
        self.assertEqual(by_type(rows, "delivery_alert"), [])
        self.assertEqual(by_type(rows, "delivery_summary")[0]["orphanReceipts"],
                         0)
        sibling = dict(proposal, reason="a different reason on the same card")
        code, rows = run_delivery([sibling], [receipt], T0_PLUS_90)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["deferred"], 0)
        self.assertEqual(metric["missing"], 1)
        self.assertEqual(by_type(rows, "delivery_summary")[0]["orphanReceipts"],
                         1)


class MissingTest(unittest.TestCase):
    def _one_proposal_pair(self):
        records = detect(assignment_snap(
            issues=[focused_issue("iid-3", "case_a6dbab15")],
            hosts=[{"host": "192.0.2.190", "diskPct": 10,
                    "load": 1.0, "vcpu": 4, "sustainedMin": 1}]))
        proposals = [with_stamps(p, emitted=T0_ISO)
                     for p in proposals_of(records)]
        receipts = [with_stamps(
            dry_run_receipt(p, live_unassigned("iid-3", "case_a6dbab15")),
            at=T0_PLUS_90) for p in proposals]
        return proposals, receipts

    def test_missing_young_is_info_and_exit_zero(self):
        proposals, receipts = self._one_proposal_pair()
        code, rows = run_delivery(proposals, receipts[1:],
                                  "2026-10-03T14:05:00Z")
        self.assertEqual(code, 0)
        alerts = by_type(rows, "delivery_alert")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "info")
        self.assertEqual(alerts[0]["route"], CEO_ROUTE)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["missing"], 1)
        self.assertEqual(metric["successRate"], 0.0)
        self.assertEqual(metric["oldestMissingAgeMin"], 5.0)

    def test_missing_old_is_high_stall_and_exit_one(self):
        proposals, receipts = self._one_proposal_pair()
        code, rows = run_delivery(proposals, receipts[1:],
                                  "2026-10-03T16:00:00Z")
        self.assertEqual(code, 1)
        alerts = by_type(rows, "delivery_alert")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "high")
        self.assertIn("stall", alerts[0]["reason"])
        self.assertEqual(alerts[0]["route"], CEO_ROUTE)
        self.assertEqual(alerts[0]["owner"], "devops")
        summary = by_type(rows, "delivery_summary")[0]
        self.assertEqual(summary["alertsHigh"], 1)


class FailedDeliveryTest(unittest.TestCase):
    def test_failed_apply_alerts_devops_and_exit_one(self):
        proposal = {"type": "proposal", "detector": "watchdog/assignment",
                    "reason": "in-focus card needs an owner",
                    "mutation": "assign_blocker_owner",
                    "issueId": "iid-4", "identifier": "case_4ac9f045",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        receipt = {"type": "receipt", "at": T0_PLUS_90,
                   "writer": rw.WRITER_NAME, "mode": "apply",
                   "issueId": "iid-4", "identifier": "case_4ac9f045",
                   "detector": "watchdog/assignment",
                   "reason": "in-focus card needs an owner",
                   "mutation": "assign_blocker_owner",
                   "decision": "decided", "apply": "failed:HTTP 500"}
        receipt["fingerprint"] = dl.proposal_fingerprint(proposal)
        code, rows = run_delivery([proposal], [receipt], T0_PLUS_90)
        self.assertEqual(code, 1)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["failed"], 1)
        alerts = by_type(rows, "delivery_alert")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "high")
        self.assertEqual(alerts[0]["route"], "devops")

    def test_issueless_mutation_none_refusal_is_not_an_alert(self):
        # supply_famine proposes a page with no card. The writer refuses it
        # undecidable and exits 1. That refusal is the policy: counted, not
        # paged, because no operator action clears a sustained famine.
        records = detect(snap(supply={
            "readyNow": 1, "target": 12, "belowMin": 40.0,
            "idleAgents": 0, "censusAt": T0_ISO}))
        proposals = [with_stamps(p, emitted=T0_ISO)
                     for p in proposals_of(records)
                     if p.get("detector") == "watchdog/supply_famine"]
        self.assertEqual(len(proposals), 1)
        self.assertFalse(proposals[0].get("issueId"))
        writer_code, writer_rows = run_writer(proposals)
        self.assertEqual(writer_code, 1)
        receipts = by_type(writer_rows, "receipt")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["decision"], "refused:undecidable")
        self.assertNotIn("fingerprint", receipts[0])
        code, rows = run_delivery(proposals, writer_rows, T0_PLUS_90)
        self.assertEqual(code, 0)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["detector"], "watchdog/supply_famine")
        self.assertEqual(metric["refused"], 1)
        self.assertEqual(metric["failed"], 0)
        self.assertEqual(metric["missing"], 0)
        self.assertEqual(by_type(rows, "delivery_alert"), [])
        self.assertEqual(by_type(rows, "delivery_summary")[0]["alertsHigh"], 0)

    def test_mutating_undecidable_still_alerts(self):
        # A real mutation that names a card the writer will not accept is
        # still a failed delivery. reset_agent_error with an issue id is
        # refused before decide, with no fingerprint.
        proposal = {"type": "proposal", "detector": "watchdog/agent_error",
                    "reason": "benign exit-143",
                    "mutation": "reset_agent_error",
                    "issueId": "iid-5", "identifier": "case_0dd2d987",
                    "agentId": "00000000-0000-4000-8000-000000000020",
                    "runId": "00000000-0000-4000-8000-000000000087",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        writer_code, writer_rows = run_writer([proposal])
        self.assertEqual(writer_code, 1)
        receipts = by_type(writer_rows, "receipt")
        self.assertEqual(receipts[0]["decision"], "refused:undecidable")
        self.assertNotIn("fingerprint", receipts[0])
        code, rows = run_delivery([proposal], writer_rows, T0_PLUS_90)
        self.assertEqual(code, 1)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["failed"], 1)
        self.assertEqual(metric["missing"], 0)
        alerts = by_type(rows, "delivery_alert")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "high")
        self.assertEqual(alerts[0]["route"], "devops")

    def test_issueless_real_mutation_still_alerts(self):
        # The line between the famine page and a lost handoff is the
        # mutation conjunct. A real mutation (operator_decision) that names
        # no card is refused undecidable like a famine page, but it is a
        # mutation that never reached a card, so it must page DevOps.
        proposal = {"type": "proposal", "detector": "watchdog/org_standard",
                    "reason": "repo ruleset drift",
                    "mutation": "operator_decision",
                    "issueId": "", "identifier": "org-standard:repo",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        writer_code, writer_rows = run_writer([proposal])
        self.assertEqual(writer_code, 1)
        receipts = by_type(writer_rows, "receipt")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["decision"], "refused:undecidable")
        code, rows = run_delivery([proposal], writer_rows, T0_PLUS_90)
        self.assertEqual(code, 1)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["failed"], 1)
        self.assertEqual(metric["refused"], 0)
        self.assertEqual(metric["missing"], 0)
        alerts = by_type(rows, "delivery_alert")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "high")
        self.assertEqual(alerts[0]["route"], "devops")

    def test_agent_reset_receipt_joins_on_agent_and_run(self):
        proposal = {"type": "proposal", "detector": "watchdog/agent_error",
                    "reason": "benign exit-143",
                    "mutation": "reset_agent_error",
                    "identifier": "agent-reset",
                    "agentId": "00000000-0000-4000-8000-000000000020",
                    "runId": "00000000-0000-4000-8000-000000000087",
                    "finishedAt": "2026-10-03T13:00:00Z",
                    "result": {"subtype": "success",
                               "unmanagedBackgroundTask": {
                                   "terminalResultSeen": True}},
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        decision = rw.decide_agent_reset(proposal, now=T0)
        self.assertEqual(decision.outcome, "decided")
        self.assertEqual(decision.issue_id, "")
        self.assertEqual(dl.proposal_fingerprint(proposal),
                         decision.fingerprint)
        targetless = rw.Decision(
            "", proposal["identifier"], proposal["detector"],
            proposal["reason"], "reset_agent_error",
            None, None, None, {}, {}, "", "decided")
        self.assertNotEqual(decision.fingerprint, targetless.fingerprint)
        receipt = with_stamps(
            decision.receipt("dry-run", "dry_run_held"), at=T0_PLUS_90)
        code, rows = run_delivery([proposal], [receipt], T0_PLUS_90)
        self.assertEqual(code, 0)
        metric = by_type(rows, "delivery_metric")[0]
        self.assertEqual(metric["delivered"], 1)
        self.assertEqual(metric["clean"], 1)
        self.assertEqual(metric["missing"], 0)
        self.assertEqual(by_type(rows, "delivery_alert"), [])


class HygieneTest(unittest.TestCase):
    def test_orphan_receipts_noted_never_alerted(self):
        proposal = {"type": "proposal", "detector": "watchdog/assignment",
                    "reason": "in-focus card needs an owner",
                    "mutation": "none",
                    "issueId": "iid-6", "identifier": "case_15089482",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        good = {"type": "receipt", "at": T0_PLUS_90,
                "writer": rw.WRITER_NAME, "mode": "dry-run",
                "issueId": "iid-6", "identifier": "case_15089482",
                "detector": "watchdog/assignment",
                "reason": "in-focus card needs an owner",
                "mutation": "none",
                "decision": "no_mutation", "apply": "no_mutation"}
        good["fingerprint"] = dl.proposal_fingerprint(proposal)
        orphan = dict(good, fingerprint="0" * 24, identifier="TOG-X")
        code, rows = run_delivery([proposal], [good, orphan], T0_PLUS_90)
        self.assertEqual(code, 0)
        summary = by_type(rows, "delivery_summary")[0]
        self.assertEqual(summary["orphanReceipts"], 1)
        self.assertIn("note", summary)
        self.assertEqual(by_type(rows, "delivery_alert"), [])

    def test_alert_rows_carry_identifiers_only(self):
        marker = "MARKER-never-echo-7f3a9c"
        proposal = {"type": "proposal", "detector": "watchdog/churn",
                    "reason": "repeated stall re-wake", "mutation": "none",
                    "issueId": "iid-7", "identifier": "case_4ee01500",
                    "before": {"trap": marker}, "after": {"trap": marker},
                    "note": f"note carrying {marker}",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        code, rows = run_delivery([proposal], [],
                                  "2026-10-03T16:00:00Z")
        self.assertEqual(code, 1)
        blob = json.dumps(rows)
        self.assertNotIn(marker, blob)
        self.assertNotIn("ghp_", blob.replace('"patternName": "ghp_*"', ""))
        self.assertFalse(
            re.search(r"ghp_[A-Za-z0-9]{8,}", blob)
            or re.search(r"github_pat_[A-Za-z0-9_]{8,}", blob))

    def test_detector_summary_mismatch_is_surfaced(self):
        proposal = {"type": "proposal", "detector": "watchdog/assignment",
                    "reason": "r", "mutation": "none",
                    "issueId": "iid-8", "identifier": "case_1ef046c6",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only", "emittedAt": T0_ISO}
        summary_row = {"type": "summary", "detector": "watchdog/cycle",
                       "records": 9, "phase": "phase-1-read-only",
                       "perDetector": {"assignment": {"findings": 1,
                                                      "proposals": 5,
                                                      "latencyMs": 0.1}}}
        code, rows = run_delivery([proposal, summary_row], [], T0_ISO)
        summary = by_type(rows, "delivery_summary")[0]
        self.assertEqual(summary["detectorSummaryMismatch"],
                         {"summarySays": 5, "rowsSeen": 1})

    def test_quiet_cycle_is_summary_only_exit_zero(self):
        cycle = {"type": "cycle", "writer": rw.WRITER_NAME,
                 "mode": "dry-run", "proposals": 0}
        code, rows = run_delivery([], [cycle], T0_ISO)
        self.assertEqual(code, 0)
        summary = by_type(rows, "delivery_summary")[0]
        self.assertEqual(summary["emitted"], 0)
        self.assertEqual(by_type(rows, "delivery_metric"), [])
        self.assertEqual(by_type(rows, "delivery_alert"), [])


class EmissionSideTest(unittest.TestCase):
    def test_detector_records_carry_emission_timestamps(self):
        records = detect(assignment_snap(
            issues=[focused_issue("iid-1", "case_8c5fe4cc")],
            hosts=[{"host": "192.0.2.190",
                    "diskPct": 10, "load": 1.0,
                    "vcpu": 4, "sustainedMin": 1}]))
        self.assertTrue(records)
        for record in records:
            stamp = record.get("emittedAt")
            self.assertIsNotNone(stamp)
            self.assertIsNotNone(dl._parse_time(stamp))

    def test_detect_stats_cover_every_family(self):
        stats: dict = {}
        detect(snap(), stats)
        self.assertEqual(
            sorted(stats["perDetector"]),
            ["agent_error", "assignment", "autoscaler_slices", "churn",
             "ci_health", "host_health", "org_standard",
             "pending_interactions", "quota_expiry", "red_main",
             "secret_hit", "shadow_emit_gap", "stale_reviews",
             "stranded_429", "supply_famine"])
        for family, entry in stats["perDetector"].items():
            self.assertIn("findings", entry)
            self.assertIn("proposals", entry)
            self.assertGreaterEqual(entry["latencyMs"], 0)
        self.assertGreaterEqual(stats["elapsedMs"], 0)

    def test_emitted_at_is_the_snapshot_instant_and_deterministic(self):
        snapshot = snap(
            now="2026-10-03T14:00:00Z",
            issues=[{"id": "iid-1", "identifier": "case_8c5fe4cc", "inFocus": True,
                     "assigneeAgentId": None, "inBacklogMin": 90}])
        first = detect(snapshot)
        second = detect(snapshot)
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual({r["emittedAt"] for r in first},
                         {"2026-10-03T14:00:00Z"})

    def test_snapshot_without_now_is_stamped_at_the_cli_boundary(self):
        import contextlib
        import io
        from watchdog import detectors as det
        snapshot = assignment_snap(
            issues=[focused_issue("iid-1", "case_8c5fe4cc")])
        del snapshot["now"]
        self.assertTrue(all("emittedAt" not in r for r in detect(snapshot)))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(det.main(["--snapshot", path]), 0)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        records = [r for r in rows if r["type"] != "summary"]
        self.assertTrue(records)
        for record in records:
            self.assertIsNotNone(dl._parse_time(record["emittedAt"]))

    def test_cli_summary_metrics_are_opt_in(self):
        import contextlib
        import io
        from watchdog import detectors as det
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(snap(now="2026-10-03T14:00:00Z"), handle)

            def summary_row(*extra):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(
                        det.main([*extra, "--snapshot", path]), 0)
                return json.loads(out.getvalue().splitlines()[-1])

            plain = summary_row()
            self.assertEqual(sorted(plain),
                             ["detector", "nowSource", "phase", "records", "type"])
            metered = summary_row("--delivery-metrics")
            self.assertIn("elapsedMs", metered)
            self.assertEqual(
                sorted(metered["perDetector"]),
                ["agent_error", "assignment", "autoscaler_slices", "churn",
                 "ci_health", "host_health", "org_standard",
                 "pending_interactions", "quota_expiry", "red_main",
                 "secret_hit", "shadow_emit_gap", "stale_reviews",
                 "stranded_429", "supply_famine"])

    def test_writer_receipts_carry_emission_timestamps(self):
        proposal = {"type": "proposal", "detector": "watchdog/churn",
                    "reason": "repeated stall re-wake", "mutation": "none",
                    "issueId": "iid-9", "identifier": "case_f052904a",
                    "before": {}, "after": {}, "note": "n",
                    "phase": "phase-1-read-only"}
        receipt = dry_run_receipt(proposal, live_unassigned("iid-9",
                                                            "case_f052904a"))
        self.assertIsNotNone(dl._parse_time(receipt["at"]))


if __name__ == "__main__":
    unittest.main()
