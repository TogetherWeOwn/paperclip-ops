#!/usr/bin/env python3
"""Offline suite for pr_closeout_snapshot_repair.py (case_f03f7f10).

Pure synthetic fixtures: no API, database, credential or timer. Green here
means the REPAIR works -- it is never independent GitHub verification, which
this sandbox cannot reach.

WHAT THIS SUITE IS BUILT TO CATCH (mirrors the CTO 19:17Z delta findings):

  * THE UNNAMED CHECK. Snapshot rows with `name: null` joined by nothing.
    §1 pins that the null name is filled from the ledger dict key via check
    id (plus sha/completedAt agreement), and that required names join again.
  * THE UNJOINABLE FILL. A null row whose id is absent from the ledger, or
    whose sha/completedAt disagree, filled with an invented name. §2 pins it
    stays null so verification refuses loudly.
  * THE PRESENT-NAME OVERWRITE. A row that already carries a name replaced by
    the ledger key. §3 pins present names are never touched.
  * THE BORROWED CLOCK. A record claiming core-check success while its clock
    matches an unrelated check's completion. §4 pins the mislabeled bot-114
    shape (gitleaks clock, failed core check) unsets to UNKNOWN with evidence
    naming the actual events -- and keeps the CTO's note that this does not
    imply every security-check completion is meaningless.
  * THE ORPHAN CLOCK. A core-success claim whose clock matches no retained
    completion at all (the ops-490 shape). §5 pins unset with the retained
    core identity named.
  * THE RESTAMP. A record whose clock is stale but a later same-head core
    success exists. §6 pins the clock moves to that event, not to now.
  * THE HEALTHY CLOCK. A record whose clock already joins to a qualifying
    core success. §7 pins it is preserved byte-for-byte.
  * THE NON-CORE RECORD. Merge receipts and non-core evidence left alone. §8
    pins non-claiming records and empty/missing inputs refuse or pass clean
    instead of inventing clocks.
  * THE REWORDED CLAIM. The 19:48Z checkpoint said "bound-head" instead of
    "exact-head" and repeated the borrowed gitleaks clock on bot #71 (core
    `check` pending) and #114 (no core row at all). §9 pins every wording is
    a claim and both shapes unset with the real core state named.
  * THE CHARACTER-SPLIT REQUIRED LIST. Web #307 at 19:48Z carried
    list("UNKNOWN") as its required checks. §10 pins the rejoin, that a real
    list (even of one short name) is untouched, and that verification names
    the shape.
  * THE UNJOINABLE REQUIRED NAME AND THE STALE ZIP. §11 pins verification
    refuses a required name with no same-head row, and that the re-emitted
    ZIP carries the repaired snapshot while every other member stays
    byte-identical.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import pr_closeout_snapshot_repair as repair

HEAD = "a" * 40
CORE_EVIDENCE = ("Successful exact-head core-check completion timestamp, "
                 "not polling/updatedAt progress")


def check_row(name, conclusion, completed, check_id, sha=HEAD):
    return {"name": name, "sha": sha, "conclusion": conclusion,
            "status": "completed", "id": check_id,
            "completedAt": completed}


def ledger_dict(*rows):
    """Canonical-ledger shape for merged rows: dict keyed by check name."""
    out = {}
    for name, conclusion, completed, check_id in rows:
        out[name] = {"id": check_id, "headSha": HEAD,
                     "status": "completed", "conclusion": conclusion,
                     "completedAt": completed}
    return out


def ledger_list(*rows):
    """Canonical-ledger shape for live rows: list rows already named."""
    return [{"id": check_id, "name": name, "headSha": HEAD,
             "status": "completed", "conclusion": conclusion,
             "completedAt": completed}
            for name, conclusion, completed, check_id in rows]


def snapshot_doc(check_rows):
    return {"generatedAt": "2026-10-02T19:01:28Z",
            "snapshots": {"TogetherWeOwn/two-web-next#291": {
                "headSha": HEAD, "checks": check_rows,
                "requiredChecks": ["check", "gitleaks", "pr-lint"]}}}


def ledger_doc(entries, evidence="GitHub merge receipt",
               clock="2026-10-02T18:41:47Z"):
    # A non-core default: name-only tests must not trip the clock verifier
    # with an unrelated core-success claim.
    return {"generatedAt": "2026-10-02T19:01:28Z",
            "records": {"two-web-next#291": {
                "repo": "TogetherWeOwn/two-web-next", "pr": 291,
                "headSha": HEAD, "latestExactHeadChecks": entries,
                "lastProgressAt": clock,
                "lastProgressEvidence": evidence}}}


def ledger_doc_with_clock(entries, evidence=CORE_EVIDENCE,
                           clock="2026-10-02T18:41:47Z"):
    return ledger_doc(entries, evidence=evidence, clock=clock)


class NullNames(unittest.TestCase):
    def test_null_name_fills_from_ledger_dict_key(self):
        # The verified #291 shape: the null row keeps id/sha/conclusion/
        # completedAt; only the name is dropped in conversion.
        snap = snapshot_doc([check_row(None, "skipped",
                                        "2026-10-02T11:56:22Z", 110827224458)])
        led = ledger_doc(ledger_dict(
            ("[code]smith", "skipped", "2026-10-02T11:56:22Z", 110827224458)))
        filled, _, unfilled = repair.repair_check_names(snap, led)
        self.assertEqual(filled, 1)
        self.assertEqual(unfilled, [])
        rows = snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["checks"]
        self.assertEqual(rows[0]["name"], "[code]smith")
        self.assertEqual(rows[0]["id"], 110827224458)
        # Single-row fixture: joinability is proven for the filled name.
        snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["requiredChecks"] = [
            "[code]smith"]
        self.assertEqual(repair.verify(snap, led), [])

    def test_required_names_join_after_fill(self):
        snap = snapshot_doc([
            check_row(None, "success", "2026-10-02T12:03:07Z", 1),
            check_row(None, "success", "2026-10-02T12:01:00Z", 2),
            check_row(None, "success", "2026-10-02T12:02:00Z", 3)])
        led = ledger_doc(ledger_dict(
            ("check", "success", "2026-10-02T12:03:07Z", 1),
            ("gitleaks", "success", "2026-10-02T12:01:00Z", 2),
            ("pr-lint", "success", "2026-10-02T12:02:00Z", 3)))
        filled, _, _ = repair.repair_check_names(snap, led)
        self.assertEqual(filled, 3)
        self.assertEqual(repair.verify(snap, led), [])


class UnjoinableRows(unittest.TestCase):
    def test_unknown_id_stays_null_for_verification(self):
        snap = snapshot_doc([check_row(None, "skipped",
                                        "2026-10-02T11:56:22Z", 999)])
        led = ledger_doc(ledger_dict(
            ("[code]smith", "skipped", "2026-10-02T11:56:22Z", 110827224458)))
        filled, _, unfilled = repair.repair_check_names(snap, led)
        self.assertEqual(filled, 0)
        self.assertEqual(len(unfilled), 1)
        self.assertIsNone(
            snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["checks"][0]["name"])
        # No required names: the null row alone must fail verification.
        snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["requiredChecks"] = []
        self.assertEqual(
            repair.verify(snap, led),
            ["TogetherWeOwn/two-web-next#291: 1 check rows with null names"])

    def test_sha_mismatch_stays_null(self):
        # Same id, different sha: a different attempt, not this row's name.
        snap = snapshot_doc([check_row(None, "success",
                                        "2026-10-02T12:03:07Z", 1, sha=HEAD)])
        led = ledger_doc({"check": {"id": 1, "headSha": "b" * 40,
                                    "conclusion": "success",
                                    "completedAt": "2026-10-02T12:03:07Z"}})
        filled, _, unfilled = repair.repair_check_names(snap, led)
        self.assertEqual(filled, 0)
        self.assertEqual(len(unfilled), 1)

    def test_completed_at_mismatch_stays_null(self):
        snap = snapshot_doc([check_row(None, "success",
                                        "2026-10-02T12:03:07Z", 1)])
        led = ledger_doc({"check": {"id": 1, "headSha": HEAD,
                                    "conclusion": "success",
                                    "completedAt": "2026-10-02T12:04:00Z"}})
        filled, _, unfilled = repair.repair_check_names(snap, led)
        self.assertEqual(filled, 0)
        self.assertEqual(len(unfilled), 1)

    def test_present_name_is_never_overwritten(self):
        snap = snapshot_doc([check_row("gitleaks", "success",
                                        "2026-10-02T12:01:00Z", 2)])
        led = ledger_doc({"check": {"id": 2, "headSha": HEAD,
                                    "conclusion": "success",
                                    "completedAt": "2026-10-02T12:01:00Z"}})
        filled, _, unfilled = repair.repair_check_names(snap, led)
        self.assertEqual(filled, 0)
        self.assertEqual(unfilled, [])
        self.assertEqual(
            snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["checks"][0]["name"],
            "gitleaks")


class BorrowedClocks(unittest.TestCase):
    def ledger_and_snapshot(self, clock, checks):
        led = ledger_doc_with_clock(ledger_list(*checks), clock=clock)
        snap_rows = [check_row(name, conclusion, completed, check_id)
                     for name, conclusion, completed, check_id in checks]
        snap = {"generatedAt": "2026-10-02T19:01:28Z",
                "snapshots": {"TogetherWeOwn/two-web-next#291": {
                    "headSha": HEAD, "checks": snap_rows,
                    "requiredChecks": []}}}
        return led, snap

    def test_gitleaks_clock_with_failed_core_unsets(self):
        # The verified bot-114 shape: the clock matches gitleaks/success
        # while the retained core check FAILED at a later time.
        led, snap = self.ledger_and_snapshot(
            "2026-10-02T18:41:47Z",
            [("gitleaks", "success", "2026-10-02T18:41:47Z", 10),
             ("check", "failure", "2026-10-02T18:45:58Z", 11)])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 1))
        record = led["records"]["two-web-next#291"]
        self.assertIsNone(record["lastProgressAt"])
        evidence = record["lastProgressEvidence"]
        self.assertIn("check/failure", evidence)
        self.assertIn("2026-10-02T18:45:58Z", evidence)
        self.assertIn("gitleaks/success", evidence)
        self.assertIn("not meaningless", evidence)
        self.assertFalse(repair.CORE_SUCCESS_CLAIM_RE.search(evidence))

    def test_orphan_clock_with_no_retained_completion_unsets(self):
        # The verified ops-490 shape: the claimed clock matches nothing.
        led, snap = self.ledger_and_snapshot(
            "2026-10-02T18:32:07Z",
            [("pr-lint", "success", "2026-10-02T18:31:31Z", 20)])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 1))
        record = led["records"]["two-web-next#291"]
        self.assertIsNone(record["lastProgressAt"])
        self.assertIn("no retained core", record["lastProgressEvidence"].lower())

    def test_stale_clock_restamps_to_latest_core_success(self):
        led, snap = self.ledger_and_snapshot(
            "2026-10-02T12:00:00Z",
            [("check", "success", "2026-10-02T12:00:00Z", 30),
             ("check", "success", "2026-10-02T12:30:00Z", 31)])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (1, 0))
        self.assertEqual(led["records"]["two-web-next#291"]["lastProgressAt"],
                         "2026-10-02T12:30:00Z")

    def test_joining_clock_is_preserved(self):
        led, snap = self.ledger_and_snapshot(
            "2026-10-02T17:27:52Z",
            [("check", "success", "2026-10-02T17:27:52Z", 40),
             ("gitleaks", "success", "2026-10-02T17:17:26Z", 41)])
        before = dict(led["records"]["two-web-next#291"])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 0))
        self.assertEqual(led["records"]["two-web-next#291"], before)

    def test_off_head_core_success_does_not_count(self):
        led, snap = self.ledger_and_snapshot(
            "2026-10-02T18:41:47Z",
            [("gitleaks", "success", "2026-10-02T18:41:47Z", 50)])
        snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["checks"].append(
            check_row("check", "success", "2026-10-02T18:40:00Z", 51,
                      sha="b" * 40))
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 1))
        self.assertIsNone(led["records"]["two-web-next#291"]["lastProgressAt"])


class NonCoreRecords(unittest.TestCase):
    def test_merge_receipt_clock_is_untouched(self):
        led = ledger_doc(ledger_dict(
            ("check", "success", "2026-10-02T12:03:07Z", 1)),
            evidence="GitHub merge receipt",
            clock="2026-10-02T13:40:35Z")
        snap = snapshot_doc([check_row("check", "success",
                                        "2026-10-02T12:03:07Z", 1)])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 0))
        self.assertEqual(led["records"]["two-web-next#291"]["lastProgressAt"],
                         "2026-10-02T13:40:35Z")

    def test_missing_evidence_is_untouched(self):
        led = ledger_doc(ledger_dict(
            ("check", "success", "2026-10-02T12:03:07Z", 1)),
            evidence=None, clock="2026-10-02T13:40:35Z")
        snap = snapshot_doc([check_row("check", "success",
                                        "2026-10-02T12:03:07Z", 1)])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 0))

    def test_empty_inputs_refuse_clean(self):
        # Each side is checked alone so one guard cannot mask the other.
        led = ledger_doc([])
        snap = snapshot_doc([])
        for call in (lambda: repair.repair_check_names({"snapshots": {}}, led),
                     lambda: repair.repair_check_names(snap, {"records": {}}),
                     lambda: repair.repair_progress_clocks(led, {"snapshots": {}}),
                     lambda: repair.repair_progress_clocks({"records": {}}, snap),
                     lambda: repair.repair_required_checks({"snapshots": {}})):
            with self.assertRaises(repair.RepairError):
                call()

    def test_check_only_detects_both_defects(self):
        led = ledger_doc_with_clock(ledger_dict(
            ("check", "success", "2026-10-02T12:03:07Z", 1)))
        snap = snapshot_doc([check_row(None, "success",
                                        "2026-10-02T12:03:07Z", 1)])
        proc = run_tool(led, snap, "--check-only")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("null names", proc.stdout)
        self.assertIn("claims core-check success", proc.stdout)


TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "pr_closeout_snapshot_repair.py")


def run_tool(led, snap, *extra, work=None):
    """Run the CLI against JSON fixtures in a private directory."""
    own = work is None
    work = work or tempfile.mkdtemp()
    try:
        ledger_path = os.path.join(work, "ledger.json")
        snapshot_path = os.path.join(work, "snapshot.json")
        with open(ledger_path, "w", encoding="utf-8") as out:
            json.dump(led, out)
        with open(snapshot_path, "w", encoding="utf-8") as out:
            json.dump(snap, out)
        return subprocess.run(
            [sys.executable, TOOL, "--ledger", ledger_path,
             "--snapshot", snapshot_path,
             "--ledger-out", os.path.join(work, "fixed-ledger.json"),
             "--report-out", os.path.join(work, "report.json"), *extra],
            capture_output=True, text=True, timeout=60)
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


def single_target(head_checks, required=None):
    return {"generatedAt": "2026-10-02T19:48:00Z",
            "snapshots": {"TogetherWeOwn/two-web-next#291": {
                "headSha": HEAD, "checks": head_checks,
                "requiredChecks": required if required is not None else []}}}


class RewordedClaims(unittest.TestCase):
    BOUND = ("Successful bound-head core-check completion, not read-time or "
             "GitHub updatedAt")

    def test_every_recorded_wording_is_a_core_claim(self):
        for evidence in (CORE_EVIDENCE, self.BOUND,
                         "Verified successful exact-head core-check completion "
                         "timestamp; not inferred from PR updatedAt"):
            self.assertTrue(repair.claims_core_success(evidence), evidence)
        self.assertFalse(repair.claims_core_success("GitHub merge receipt"))

    def test_bound_head_gitleaks_clock_with_pending_core_unsets(self):
        # The 19:48Z bot-71 shape: the clock is gitleaks/success while the
        # core check is still in flight (no conclusion, no completedAt).
        led = ledger_doc_with_clock(
            [], evidence=self.BOUND, clock="2026-10-02T19:35:05Z")
        snap = single_target([
            check_row("gitleaks", "success", "2026-10-02T19:35:05Z", 60),
            check_row("check", None, None, 61)])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 1))
        record = led["records"]["two-web-next#291"]
        self.assertIsNone(record["lastProgressAt"])
        self.assertIn("check/pending", record["lastProgressEvidence"])
        self.assertIn("gitleaks/success completedAt 2026-10-02T19:35:05Z",
                      record["lastProgressEvidence"])

    def test_bound_head_clock_with_no_core_row_unsets(self):
        # The 19:48Z bot-114 shape: no core `check` row at the head at all.
        led = ledger_doc_with_clock(
            [], evidence=self.BOUND, clock="2026-10-02T19:36:34Z")
        snap = single_target([
            check_row("gitleaks", "success", "2026-10-02T19:36:34Z", 70),
            check_row("pr-lint", "success", "2026-10-02T19:36:23Z", 71)])
        restamped, unset, _ = repair.repair_progress_clocks(led, snap)
        self.assertEqual((restamped, unset), (0, 1))
        self.assertIn("no retained core `check` attempt",
                      led["records"]["two-web-next#291"]["lastProgressEvidence"])
        self.assertEqual(repair.verify(snap, led), [])


class CharacterSplitRequired(unittest.TestCase):
    def test_character_split_required_rejoins(self):
        snap = single_target([], required=list("UNKNOWN"))
        repairs = repair.repair_required_checks(snap)
        self.assertEqual(repairs, [{"target": "TogetherWeOwn/two-web-next#291",
                                    "requiredChecks": "UNKNOWN"}])
        self.assertEqual(
            snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["requiredChecks"],
            "UNKNOWN")

    def test_real_required_list_is_untouched(self):
        for required in (["check", "gitleaks", "pr-lint"], ["c"], []):
            snap = single_target([], required=list(required))
            self.assertEqual(repair.repair_required_checks(snap), [])
            self.assertEqual(
                snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["requiredChecks"],
                required)

    def test_verify_names_character_split_shape(self):
        snap = single_target([], required=list("UNKNOWN"))
        errors = repair.verify(snap, ledger_doc([]))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("character-split string 'UNKNOWN'", errors[0])


class JoinabilityAndZip(unittest.TestCase):
    def test_unjoinable_required_name_is_flagged(self):
        snap = single_target(
            [check_row("check", "success", "2026-10-02T12:03:07Z", 1),
             check_row("gitleaks", "success", "2026-10-02T12:01:00Z", 2),
             check_row("pr-lint", "success", "2026-10-02T12:02:00Z", 3,
                       sha="b" * 40)],
            required=["check", "gitleaks", "pr-lint"])
        errors = repair.verify(snap, ledger_doc([]))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("required check pr-lint not joinable", errors[0])

    def test_core_claim_without_snapshot_target_is_flagged(self):
        led = ledger_doc_with_clock([])
        led["records"]["two-bot-next#114"] = dict(
            led["records"]["two-web-next#291"])
        snap = single_target(
            [check_row("gitleaks", "success", "2026-10-02T18:41:47Z", 1)])
        repair.repair_progress_clocks(led, snap)
        errors = repair.verify(snap, led)
        self.assertEqual(
            errors, ["two-bot-next#114: core-success claim with no snapshot target"])

    def test_zip_reemits_repaired_snapshot_and_keeps_other_members(self):
        led = ledger_doc(ledger_dict(
            ("check", "success", "2026-10-02T12:03:07Z", 1)))
        snap = snapshot_doc([check_row(None, "success",
                                        "2026-10-02T12:03:07Z", 1)])
        snap["snapshots"]["TogetherWeOwn/two-web-next#291"]["requiredChecks"] = [
            "check"]
        handoff = b"# runtime handoff\n"
        work = tempfile.mkdtemp()
        try:
            zip_in = os.path.join(work, "in.zip")
            zip_out = os.path.join(work, "out.zip")
            with zipfile.ZipFile(zip_in, "w") as archive:
                archive.writestr("registry.json", b"{}")
                archive.writestr("snapshot.json", json.dumps(snap))
                archive.writestr("RUNTIME-HANDOFF.md", handoff)
            proc = run_tool(led, {"unused": True}, "--zip-in", zip_in,
                            "--zip-out", zip_out, work=work)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            with zipfile.ZipFile(zip_out) as archive:
                self.assertEqual(
                    [info.filename for info in archive.infolist()],
                    ["registry.json", "snapshot.json", "RUNTIME-HANDOFF.md"])
                fixed = json.loads(archive.read("snapshot.json"))
                self.assertEqual(archive.read("RUNTIME-HANDOFF.md"), handoff)
            rows = fixed["snapshots"]["TogetherWeOwn/two-web-next#291"]["checks"]
            self.assertEqual(rows[0]["name"], "check")
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
