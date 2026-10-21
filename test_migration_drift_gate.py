#!/usr/bin/env python3
"""Offline suite for migration_drift_gate.py (shared migration gates).

Fully offline: fixture digests/ledgers/evidence in a throwaway temp dir, no
database, no network, no credentials. Uses the module's main() in-process so
exit codes are asserted, not just eyeballed.
"""
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

import migration_drift_gate as gate


def sha(b):
    return hashlib.sha256(b).hexdigest()


A = sha(b"-- phase: expand\nselect 1;\n")
B = sha(b"-- phase: expand\nselect 2;\n")
C = sha(b"-- phase: contract\nselect 3;\n")


class DriftGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="drift-gate-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, text):
        p = self.root / name
        p.write_text(text)
        return str(p)

    def hashes(self, pairs):
        return self.write("candidate.txt",
                          "".join(f"{h}  {t}.sql\n" for t, h in pairs))

    def ledger(self, hashes):
        return self.write("ledger.txt", "".join(h + "\n" for h in hashes))

    def evidence(self, status="passed", age_hours=1, migrations=(("0042_a", A),)):
        finished = (datetime.now(timezone.utc) - timedelta(hours=age_hours))
        doc = {"version": 1, "provider": "planetscale", "branch": "rehearsal/x",
               "apply_target": "rehearsal/x", "base": "prod",
               "migrations": [{"tag": t, "sha256": h, "result": "applied"}
                               for t, h in migrations],
               "status": status,
               "started_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "cleanup": "deleted"}
        return self.write("evidence.json", json.dumps(doc))

    def run_gate(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = gate.main(list(args))
        return rc, buf.getvalue()

    def test_green_candidate_matching_ledger(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                           "--ledger", self.ledger([A, B]))
        self.assertEqual(rc, 0, out)
        self.assertIn("ok: all 2 candidate migrations", out)
        self.assertIn("ok: no out-of-band", out)

    def test_pending_candidate_fails_without_rehearsal(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                           "--ledger", self.ledger([A]))
        self.assertEqual(rc, 1, out)
        self.assertIn("0043_b", out)

    def test_pending_subset_passes_with_bound_rehearsal(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                           "--ledger", self.ledger([A]),
                           "--evidence", self.evidence(migrations=(("0043_b", B),)))
        self.assertEqual(rc, 0, out)
        self.assertIn("pending migration(s) require bound rehearsal", out)
        self.assertIn("bound to the candidate chain (1 migration(s))", out)

    def test_failed_cleanup_evidence_fails(self):
        path = self.evidence()
        doc = json.loads(Path(path).read_text())
        doc["cleanup"] = "failed"
        Path(path).write_text(json.dumps(doc))
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A]), "--evidence", path)
        self.assertEqual(rc, 1, out)
        self.assertIn("cleanup is not confirmed deleted", out)

    def test_unknown_ledger_row_fails(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A, C]))
        self.assertEqual(rc, 1, out)
        self.assertIn("out-of-band", out)

    def test_fresh_evidence_passes(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A]),
                           "--evidence", self.evidence(age_hours=1))
        self.assertEqual(rc, 0, out)
        self.assertIn("ok: rehearsal evidence fresh", out)
        self.assertIn("bound to the candidate chain", out)

    def test_stale_evidence_fails(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A]),
                           "--evidence", self.evidence(age_hours=30))
        self.assertEqual(rc, 1, out)
        self.assertIn("older than 24h", out)

    def test_failed_evidence_fails(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A]),
                           "--evidence", self.evidence(status="failed"))
        self.assertEqual(rc, 1, out)
        self.assertIn("'failed'", out)

    def test_unbound_evidence_fails(self):
        # Evidence for an unrelated chain cannot pass, even when fresh.
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A]),
                           "--evidence", self.evidence(migrations=(("0099_other", B),)))
        self.assertEqual(rc, 1, out)
        self.assertIn("not bound", out)

    def test_digest_mismatch_evidence_fails(self):
        # Same tags, different digests: the chain moved since rehearsal.
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A]),
                           "--evidence", self.evidence(migrations=(("0042_a", B),)))
        self.assertEqual(rc, 1, out)
        self.assertIn("not bound", out)

    def test_evidence_in_wrong_migration_order_fails(self):
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
                           "--ledger", self.ledger([A, B]),
                           "--evidence", self.evidence(migrations=(("0043_b", B), ("0042_a", A))))
        self.assertEqual(rc, 1, out)
        self.assertIn("migration order does not match filename order", out)

    def test_future_evidence_fails(self):
        # A finished_at in the future (forged freshness) fails instead of
        # printing a negative age as ok.
        rc, out = self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                           "--ledger", self.ledger([A]),
                           "--evidence", self.evidence(age_hours=-2))
        self.assertEqual(rc, 1, out)
        self.assertIn("in the future", out)

    def test_empty_ledger_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_gate("--sql-hashes", self.hashes([("0042_a", A)]),
                     "--ledger", self.ledger([]))
        self.assertEqual(cm.exception.code, 2)

    def test_malformed_hashes_are_refused(self):
        bad = self.write("bad.txt", "not-a-hash  0042_a.sql\n")
        with self.assertRaises(SystemExit) as cm:
            self.run_gate("--sql-hashes", bad, "--ledger", self.ledger([A]))
        self.assertEqual(cm.exception.code, 2)


class ReviewRoundTest(unittest.TestCase):
    """Round-1 review pins (Paperclip Review 4/5 on 21bb8bab).

    The acceptance suite (test_migration_evidence_ax.py) is Test-Author
    owned and immutable, so the review's new fixtures live here: byte-level
    input fixtures in both modes, text-refusal stdout preservation,
    refused-first next_action precedence, and one emitter per verdict
    reason the acceptance suite only allowlists.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="drift-gate-review-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_bytes(self, name, data):
        p = self.root / name
        p.write_bytes(data)
        return str(p)

    def hashes(self, pairs):
        p = self.root / "candidate.txt"
        p.write_text("".join(f"{h}  {t}.sql\n" for t, h in pairs))
        return str(p)

    def ledger(self, hashes):
        p = self.root / "ledger.txt"
        p.write_text("".join(h + "\n" for h in hashes))
        return str(p)

    def evidence_doc(self, **overrides):
        finished = (datetime.now(timezone.utc) - timedelta(hours=1))
        ts = finished.strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = {"version": 1, "provider": "planetscale", "branch": "rehearsal/x",
               "apply_target": "rehearsal/x", "base": "prod",
               "migrations": [{"tag": "0042_a", "sha256": A, "result": "applied"}],
               "status": "passed", "started_at": ts, "finished_at": ts,
               "cleanup": "deleted"}
        doc.update(overrides)
        return doc

    def run_full(self, *args):
        """(rc, stdout, stderr). Refusals surface as rc 2; anything else
        that raises (a gate crash) propagates and fails the test."""
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            try:
                rc = gate.main(list(args))
            except SystemExit as e:
                rc = e.code
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def run_json(self, *args):
        rc, out, err = self.run_full(*args, "--json")
        self.assertNotIn("Traceback", out + err, f"no traceback in JSON mode: rc={rc!r}")
        try:
            obj = json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"JSON stdout must parse: rc={rc!r} out={out[:400]!r} err={err[:400]!r}")
        return rc, obj, out, err

    def assert_refused_json(self, obj, rc, expected_reason, context):
        self.assertEqual(rc, 2, f"{context}: must fail closed exit 2")
        self.assertEqual(obj["verdict"], "refused", f"{context}: verdict must be refused")
        reasons = [c["reason"] for c in obj["checks"]]
        self.assertIn(expected_reason, reasons,
                      f"{context}: expected reason {expected_reason!r} in {reasons}")
        self.assertTrue(obj["next_action"].strip(),
                        f"{context}: next_action must be actionable on refusal")

    # Finding 1: non-UTF-8 inputs refuse with a verdict instead of crashing.
    def test_non_utf8_candidate_refused_both_modes(self):
        bad = self.write_bytes("candidate.bin", b"\xff\xfe\x00bad\n")
        ok_ledger = self.ledger([A])
        rc, out, err = self.run_full("--sql-hashes", bad, "--ledger", ok_ledger)
        self.assertEqual(rc, 2, f"text must refuse, got rc={rc!r} out={out[:200]!r}")
        self.assertIn("REFUSED", err)
        self.assertNotIn("Traceback", out + err)
        rc, obj, _, _ = self.run_json("--sql-hashes", bad, "--ledger", ok_ledger)
        self.assert_refused_json(obj, rc, "input_malformed_hashes", "non-utf8-candidate")

    def test_non_utf8_ledger_refused_both_modes(self):
        ok_hashes = self.hashes([("0042_a", A)])
        bad = self.write_bytes("ledger.bin", b"\xff\xfe\x00bad\n")
        rc, out, err = self.run_full("--sql-hashes", ok_hashes, "--ledger", bad)
        self.assertEqual(rc, 2, f"text must refuse, got rc={rc!r} out={out[:200]!r}")
        self.assertIn("REFUSED", err)
        self.assertNotIn("Traceback", out + err)
        rc, obj, _, _ = self.run_json("--sql-hashes", ok_hashes, "--ledger", bad)
        self.assert_refused_json(obj, rc, "input_malformed_ledger", "non-utf8-ledger")

    def test_non_utf8_evidence_refused_both_modes(self):
        ok_hashes = self.hashes([("0042_a", A)])
        ok_ledger = self.ledger([A])
        bad = self.write_bytes("evidence.bin", b"\xff\xfe\x00bad")
        rc, out, err = self.run_full("--sql-hashes", ok_hashes, "--ledger", ok_ledger,
                                     "--evidence", bad)
        self.assertEqual(rc, 2, f"text must refuse, got rc={rc!r} out={out[:200]!r}")
        self.assertIn("REFUSED", err)
        self.assertNotIn("Traceback", out + err)
        rc, obj, _, _ = self.run_json("--sql-hashes", ok_hashes, "--ledger", ok_ledger,
                                      "--evidence", bad)
        self.assert_refused_json(obj, rc, "evidence_invalid_json", "non-utf8-evidence")

    # Finding 2: text mode keeps the lines evaluated before an evidence refusal.
    def test_text_refusal_prints_earlier_lines(self):
        ev = self.root / "evidence.json"
        ev.write_text("[1, 2]")
        rc, out, err = self.run_full(
            "--sql-hashes", self.hashes([("0042_a", A), ("0043_b", B)]),
            "--ledger", self.ledger([A, C]),
            "--evidence", str(ev))
        self.assertEqual(rc, 2, f"text must fail closed exit 2, got rc={rc!r}")
        self.assertIn("REFUSED", err)
        self.assertIn("pending migration(s) require bound rehearsal evidence", out,
                      "text refusal must keep the candidate line evaluated before it")
        self.assertIn("out-of-band", out,
                      "text refusal must keep the ledger diagnostic evaluated before it")
        self.assertNotIn("Traceback", out + err)

    # Finding 5: refused-first next_action precedence is pinned by a combined
    # fixture (an earlier ledger FAIL plus a later evidence refusal).
    def test_refusal_action_precedes_failed_ledger(self):
        ev = self.root / "evidence.json"
        ev.write_text("[1, 2]")
        rc, obj, _, _ = self.run_json(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A, C]),
            "--evidence", str(ev))
        self.assertEqual(rc, 2)
        self.assertEqual(obj["verdict"], "refused")
        self.assertEqual(obj["next_action"],
                         "Fix the evidence shape: top-level JSON must be an object.")

    # Finding 6: every NEXT_ACTIONS reason the gate can emit is exercised.
    def test_unparseable_time_emits_stable_reason(self):
        ev = self.root / "evidence.json"
        ev.write_text(json.dumps(self.evidence_doc(finished_at="not-a-time")))
        rc, obj, _, _ = self.run_json(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]),
            "--evidence", str(ev))
        self.assertEqual(rc, 1, "unparseable finished_at is a gate failure, not a refusal")
        self.assertEqual(obj["verdict"], "fail")
        reasons = [c["reason"] for c in obj["checks"]]
        self.assertIn("evidence_unparseable_time", reasons)
        self.assertTrue(obj["next_action"].strip())

    def test_empty_candidate_emits_stable_reason(self):
        empty = self.write_bytes("empty.txt", b"")
        rc, obj, _, _ = self.run_json("--sql-hashes", empty, "--ledger", self.ledger([A]))
        self.assert_refused_json(obj, rc, "input_empty_candidate", "empty-candidate")

    def test_hashless_ledger_row_emits_stable_reason(self):
        bad = self.root / "ledger.txt"
        bad.write_text("no hash on this row\n")
        rc, obj, _, _ = self.run_json(
            "--sql-hashes", self.hashes([("0042_a", A)]), "--ledger", str(bad))
        self.assert_refused_json(obj, rc, "input_malformed_ledger", "hashless-ledger-row")

    # Finding 7 (AX2): a top-level field refusal must not point only at rows.
    def test_top_level_field_action_names_top_level_fields(self):
        ev = self.root / "evidence.json"
        ev.write_text(json.dumps(self.evidence_doc(finished_at=123)))
        rc, obj, _, _ = self.run_json(
            "--sql-hashes", self.hashes([("0042_a", A)]),
            "--ledger", self.ledger([A]),
            "--evidence", str(ev))
        self.assertEqual(rc, 2)
        self.assertEqual(obj["verdict"], "refused")
        reasons = [c["reason"] for c in obj["checks"]]
        self.assertIn("evidence_invalid_field", reasons)
        self.assertIn("top-level", obj["next_action"],
                      f"action must name the level that failed: {obj['next_action']!r}")


if __name__ == "__main__":
    unittest.main()
