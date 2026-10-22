"""Builder-owned regression pin for the empty-ledger findings fix.

The immutable acceptance suite (test_review_report_input_integrity.py) does
not cover an empty decision ledger paired with content-invalid findings, so
the fix that validates findings before the empty-ledger early return in
review_routing_weekly.py needs its own pin: reverting that fix must turn
this suite red. Each test runs the REAL public CLI on synthetic JSONL in a
run-owned temp dir with a five-second deadline and no inherited credentials.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_review_report_empty_ledger
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "review_routing_weekly.py"
NOW = "2026-10-09T00:00:00Z"


class ReviewReportEmptyLedger(unittest.TestCase):
    maxDiff = 5000

    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
            "PAPERCLIP_SCRATCH_DIR") or os.environ.get("TMPDIR")
        self.tmp = tempfile.TemporaryDirectory(prefix="review-report-empty-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.ledger = self.work / "decisions.jsonl"
        self.findings = self.work / "findings.jsonl"
        self.no_commands = self.work / "no-commands"
        self.no_commands.mkdir()
        self.env = {
            "PATH": str(self.no_commands), "HOME": str(self.work),
            "TMPDIR": str(self.work), "LC_ALL": "C", "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def invoke(self, ledger_rows, finding_rows, *, machine=False):
        self.ledger.write_text("".join(json.dumps(r, allow_nan=False) + "\n"
                                       for r in ledger_rows))
        self.findings.write_text("".join(json.dumps(r, allow_nan=False) + "\n"
                                         for r in finding_rows))
        args = ["--ledger", str(self.ledger), "--findings", str(self.findings),
                "--now", NOW]
        if machine:
            args.append("--json")
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(ENTRYPOINT), *args],
                cwd=self.work, env=self.env, text=True, capture_output=True,
                timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("report exceeded the bounded five-second offline deadline")
        self.assertNotIn("Traceback (most recent call last)", result.stderr,
                         "bad evidence must be a controlled result, not a traceback")
        return result

    def test_empty_ledger_plus_invalid_finding_json_reports_excluded(self):
        # bool pr is content-invalid (invalid_pr), never silently skipped.
        finding = {"repo": "a/x", "pr": True, "bucket": "tier0-lint",
                   "audit_only": False, "ts": NOW}
        result = self.invoke([], [finding], machine=True)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail("--json must return one JSON object: "
                      + repr((result.returncode, result.stdout, result.stderr)))
        self.assertEqual(data["status"], "unknown")
        self.assertEqual(data["counts_state"], "unknown")
        self.assertEqual(data["counts"]["invalid_findings"], 1)
        self.assertEqual(data["counts"]["excluded_rows"], 1)
        self.assertEqual(data["reason_counts"], {"invalid_pr": 1})
        self.assertIn("warn:", result.stderr)
        self.assertIn("invalid_pr", result.stderr)

    def test_empty_ledger_plus_invalid_finding_text_reports_excluded(self):
        finding = {"repo": "a/x", "pr": True, "bucket": "tier0-lint",
                   "audit_only": False, "ts": NOW}
        result = self.invoke([], [finding])
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn("status: unknown", result.stdout)
        self.assertIn("excluded_rows: 1", result.stdout)
        self.assertIn("invalid_pr=1", result.stdout)
        self.assertIn("warn:", result.stderr)
        self.assertIn("invalid_pr", result.stderr)


if __name__ == "__main__":
    unittest.main()
