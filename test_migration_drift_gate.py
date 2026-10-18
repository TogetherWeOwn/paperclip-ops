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
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
