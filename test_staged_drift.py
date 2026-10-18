#!/usr/bin/env python3
"""Offline staged-port controls; never import private source history."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

from public_snapshot_test_support import private_artifact_absent

ROOT = Path(__file__).resolve().parent
SCRIPT = Path("templates/org-github/check-staged-drift.sh")
STAGE = Path("templates/org-github/tier-0")


class SyntheticDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="public-staged-drift-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "--quiet")
        self.git("config", "user.name", "Synthetic Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.workflow = "name: Synthetic\non: push\njobs: {}\n"
        self.put(".github/workflows/tier-0-quality-gate.yml", self.workflow)
        self.put("semgrep-rules/example.yml", "rules: []\n")
        self.git("add", ".github", "semgrep-rules")
        self.git("commit", "--quiet", "-m", "test: synthetic source fixture\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>")
        self.pin = self.git("rev-parse", "HEAD").strip()
        self.put(SCRIPT, (ROOT / SCRIPT).read_text())
        self.put(STAGE / "SOURCE", "SOURCE_SHA=" + self.pin + "\n")
        self.put(STAGE / "tier-0-quality-gate.yml", "# paperclip-ops@" + self.pin + "\n" + self.workflow)
        self.put(STAGE / "semgrep-rules/example.yml", "rules: []\n")
        baseline = self.check()
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, text=True)

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def check(self, *args):
        return subprocess.run(["bash", str(self.root / SCRIPT), *args], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=10)

    def expect(self, code, text, *args):
        result = self.check(*args)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn(text, result.stdout + result.stderr)

    def test_clean_port(self):
        self.expect(0, "staged matches source")

    def test_changed_rule(self):
        self.put(STAGE / "semgrep-rules/example.yml", "rules: [changed]\n")
        self.expect(1, "differs from source")

    def test_added_rule(self):
        self.put(STAGE / "semgrep-rules/extra.yml", "rules: []\n")
        self.expect(1, "staged but missing in source")

    def test_removed_rule(self):
        (self.root / STAGE / "semgrep-rules/example.yml").unlink()
        self.expect(1, "missing staged, present in source")

    def test_changed_workflow_body(self):
        self.put(STAGE / "tier-0-quality-gate.yml", "# paperclip-ops@" + self.pin + "\nname: Changed\n")
        self.expect(1, "body differs from source")

    def test_mismatched_header_pin(self):
        self.put(STAGE / "tier-0-quality-gate.yml", "# paperclip-ops@" + "0" * 40 + "\n" + self.workflow)
        self.expect(1, "header names")

    def test_executable_header_is_not_ignored(self):
        self.put(STAGE / "tier-0-quality-gate.yml", "# paperclip-ops@" + self.pin + "\nenv: {}\n" + self.workflow)
        self.expect(2, "non-comment line above")

    def test_missing_pin_refuses(self):
        (self.root / STAGE / "SOURCE").unlink()
        self.expect(2, "pin record missing")

    def test_malformed_pin_refuses(self):
        self.put(STAGE / "SOURCE", "SOURCE_SHA=not-a-commit\n")
        self.expect(2, "no valid SOURCE_SHA")

    def add_port_addition(self, name="added.yml"):
        # Machine list shape parsed by check-staged-drift.sh: a bullet
        # `- \x60semgrep-rules/<file>\x60` in tier-0/PORT-ADDITIONS.md.
        self.put(STAGE / "PORT-ADDITIONS.md", "- `semgrep-rules/" + name + "`\n")
        return name

    def test_listed_addition_staged_passes(self):
        name = self.add_port_addition()
        self.put(STAGE / "semgrep-rules" / name, "rules: []\n")
        self.expect(0, "staged matches source")

    def test_listed_addition_deleted_fails_missing_staged(self):
        self.add_port_addition()
        self.expect(1, "listed in PORT-ADDITIONS.md but missing staged")

    def test_listed_addition_now_in_source_fails_drop_exemption(self):
        name = self.add_port_addition()
        self.put(STAGE / "semgrep-rules" / name, "rules: []\n")
        self.put("semgrep-rules/" + name, "rules: []\n")
        self.git("add", "semgrep-rules")
        self.git("commit", "--quiet", "-m", "test: addition merged upstream\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>")
        self.expect(1, "drop the exemption", "HEAD")

    def test_unlisted_extra_staged_fails_with_manifest_present(self):
        name = self.add_port_addition()
        self.put(STAGE / "semgrep-rules" / name, "rules: []\n")
        self.put(STAGE / "semgrep-rules/unlisted.yml", "rules: []\n")
        self.expect(1, "staged but missing in source")

    def test_absent_history_refuses_even_with_public_markers(self):
        self.put("README.md", "## Fresh-history publication candidate\n")
        self.put(".github/scripts/public_safety.py", "# fixture marker\n")
        self.expect(2, "not a local git object", "0" * 40)


class HistoricalSourcePort(unittest.TestCase):
    def test_retained_private_source_port(self):
        source = (ROOT / STAGE / "SOURCE").read_text()
        pins = re.findall(r"(?m)^SOURCE_SHA=([0-9a-f]{40})$", source)
        self.assertEqual(len(pins), 1, "source pin must remain well-formed")
        exists = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", pins[0] + "^{commit}"],
                                capture_output=True).returncode == 0
        if (not exists and private_artifact_absent(ROOT, ".github/workflows/tier-0-quality-gate.yml")
                and private_artifact_absent(ROOT, "semgrep-rules")):
            self.skipTest("private Tier-0 source workflow/rules and historical Git object are excluded from the fresh public snapshot; all synthetic drift/refusal controls run")
        result = subprocess.run(["bash", str(ROOT / SCRIPT)], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
