#!/usr/bin/env python3
"""Exercise filing-strip source contracts without publishing private reports."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from public_snapshot_test_support import private_artifact_absent

NAMES = (
    "agent-run-credential-isolation", "hindsight-company-config-bootstrap",
    "interaction-comment-supersession-default", "manifest-refresh-escalation",
    "omniroute-false-cliproxy-terminal-state", "omniroute-truncated-reasoning-false-502",
    "plugin-auth-surface", "discord-plugin-proactive-job-scope",
)
GATE = "verification/upstream-bundle-filing-strip-gate.sh"


def stage_synthetic(destination):
    """Create inert text fixtures only, never a historical disclosure packet."""
    destination = Path(destination)
    docs = destination / "docs/upstream"
    docs.mkdir(parents=True, exist_ok=True)
    report = (
        "# DRAFT synthetic source fixture — not filed\n"
        "Held with the others pending the operator's decision.\n\n"
        "---\n# Synthetic parser example\n"
        "Not a real report, disclosure, finding, or historical receipt.\n\n"
        "## Summary\nExample text only.\n\n"
        "## Verification\nOffline parser assertions only.\n"
    )
    for name in NAMES:
        (docs / (name + ".md")).write_text(report)
    readme = (
        "# Synthetic filing-strip fixture\nNot a live filing checklist or authorization.\n\n"
        "## How to strip a report before filing\n"
        "The rule is structural, not lexical: remove line 1 through the text preceding the first structural boundary.\n"
        "A single-# title is never the boundary. Preserve the Hard precondition carve-out.\n\n"
        "| File | Boundary | Strip |\n| --- | --- | --- |\n"
    )
    readme += "".join(f"| `{name}.md` | 4 | delete lines 1-3 |\n" for name in NAMES)
    readme += "\n## The reports\n\n| File | Source contract |\n| --- | --- |\n"
    for name in NAMES:
        category = "authorization" if name == "plugin-auth-surface" else "scope"
        readme += f"| `{name}.md` | {category} synthetic fixture only |\n"
    (docs / "README.md").write_text(readme)


class FilingStrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-filing-strip-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "verification").mkdir()
        for name in (GATE, "verification/upstream-bundle-filing-gate.sh"):
            shutil.copyfile(ROOT / name, self.root / name)
        stage_synthetic(self.root)
        baseline = self.run_gate()
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)

    def run_gate(self, root=None, **environment):
        root = root or self.root
        return subprocess.run(["bash", str(root / GATE)], cwd=root,
                              env={"PATH": os.environ["PATH"], **environment},
                              capture_output=True, text=True, timeout=20)

    def test_synthetic_baseline(self):
        self.assertIn("PASS  8/8", self.run_gate().stdout)

    def test_missing_report_refuses(self):
        (self.root / "docs/upstream" / (NAMES[0] + ".md")).unlink()
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("unreadable or empty", result.stdout)

    def test_missing_readme_refuses(self):
        (self.root / "docs/upstream/README.md").unlink()
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("README.md unreadable or empty", result.stdout)

    def test_disclosure_below_boundary_refuses(self):
        path = self.root / "docs/upstream" / (NAMES[0] + ".md")
        path.write_text(path.read_text() + "Held with the others pending the operator.\n")
        self.assertEqual(self.run_gate().returncode, 1)

    def test_superseded_instruction_refuses(self):
        path = self.root / "docs/upstream/README.md"
        path.write_text(path.read_text() + "The banner should be edited only by whoever actually files it.\n")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("still teaches the line-1-only edit", result.stdout)

    def test_unknown_explicit_ref_is_not_skipped(self):
        result = self.run_gate(GATE_REF="missing-synthetic-ref")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("SKIP", result.stdout)

    def test_private_report_integration(self):
        overrides = {"GATE_REF": os.environ["GATE_REF"]} if "GATE_REF" in os.environ else {}
        if not overrides and private_artifact_absent(ROOT, "docs/upstream"):
            self.skipTest("private disclosure report bundle is excluded from the fresh public snapshot; synthetic source-contract and mutation controls remain enabled")
        result = self.run_gate(ROOT, **overrides)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--stage":
        stage_synthetic(sys.argv[2])
    else:
        unittest.main(verbosity=2)
