"""Exercise the live workflow's actual shell without credentials or network."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/disclosure-audit.yml"
COUNTS = "== 3 declared/clean, 2 post-hoc adjudicated, 7 undeclared publication(s), 0 unresolved"


def live_script():
    lines = WORKFLOW.read_text().splitlines()
    step = lines.index("      - name: Live disclosure audit")
    start = lines.index("        run: |", step) + 1
    body = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        body.append(line[10:])
    return "\n".join(body) + "\n"


class DisclosureAuditWorkflowTests(unittest.TestCase):
    def run_live(self, audit_rc=0, probe="example/private-probe", tee_rc=0, counts=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "verification").mkdir()
            (root / "bin").mkdir()
            audit = root / "verification/case_092cd51d-public-fork-disclosure-audit.sh"
            audit.write_text(
                '#!/usr/bin/env bash\n'
                'touch "$AUDIT_MARKER"\n'
                'if [ "$STUB_COUNTS" = 1 ]; then printf \'%s\\n\' "$STUB_COUNTS_LINE"; fi\n'
                'exit "$STUB_AUDIT_RC"\n'
            )
            audit.chmod(0o755)
            if tee_rc:
                tee = root / "bin/tee"
                tee.write_text("#!/usr/bin/env bash\ncat > /dev/null\nexit \"$STUB_TEE_RC\"\n")
                tee.chmod(0o755)
            summary = root / "summary"
            marker = root / "audit-ran"
            env = {
                "PATH": str(root / "bin") + os.pathsep + os.defpath,
                "HOME": str(root),
                "RUNNER_TEMP": str(root),
                "GITHUB_STEP_SUMMARY": str(summary),
                "DISCLOSURE_AUDIT_PRIVATE_PROBE": probe,
                "STUB_AUDIT_RC": str(audit_rc),
                "STUB_TEE_RC": str(tee_rc),
                "STUB_COUNTS": "1" if counts else "0",
                "STUB_COUNTS_LINE": COUNTS,
                "AUDIT_MARKER": str(marker),
            }
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", live_script()],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result, summary.read_text() if summary.exists() else "", marker.exists()

    def test_empty_probe_fails_before_audit(self):
        result, _, ran = self.run_live(probe="")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(ran, "must not fall back to the public probe")
        self.assertIn("cannot evaluate", result.stdout + result.stderr)

    def test_success_records_counts(self):
        result, summary, ran = self.run_live()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(ran)
        self.assertIn("exit 0", summary)
        self.assertIn(COUNTS, summary)

    def test_undeclared_failure_still_records_counts(self):
        result, summary, ran = self.run_live(audit_rc=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(ran)
        self.assertIn("exit 1", summary)
        self.assertIn(COUNTS, summary)

    def test_evaluation_failure_still_records_summary(self):
        result, summary, ran = self.run_live(audit_rc=2, counts=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(ran)
        self.assertIn("exit 2", summary)
        self.assertIn("no counts line", summary)

    def test_tee_failure_is_not_success(self):
        result, summary, _ = self.run_live(tee_rc=9)
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertIn("Disclosure audit", summary)

    def test_audit_failure_takes_precedence_over_tee_failure(self):
        result, summary, _ = self.run_live(audit_rc=2, tee_rc=9, counts=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("exit 2", summary)

    def step_lines(self, prefix):
        lines = WORKFLOW.read_text().splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith(prefix))
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("      - ")), len(lines))
        return lines[start:end]

    def test_checkout_uses_reviewed_default_branch(self):
        lines = self.step_lines("      - uses: actions/checkout@")
        self.assertIn("          ref: ${{ github.event.repository.default_branch }}", lines)

    def test_checkout_does_not_persist_credentials(self):
        lines = self.step_lines("      - uses: actions/checkout@")
        self.assertIn("          persist-credentials: false", lines)

    def test_live_step_requires_explicit_opt_in(self):
        lines = self.step_lines("      - name: Live disclosure audit")
        self.assertIn("        if: vars.DISCLOSURE_AUDIT_LIVE == '1'", lines)

    def test_job_permissions_are_explicit_and_read_only(self):
        self.assertIn("    permissions:\n      contents: read\n", WORKFLOW.read_text())


if __name__ == "__main__":
    unittest.main()
