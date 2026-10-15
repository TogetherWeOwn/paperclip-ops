#!/usr/bin/env python3
"""Offline acceptance for the quarantine-landing reds.

Reproduces, without network, database, or credentials, the four CI reds
seen on the quarantine head (secret scan, change detection, pr-lint and
the derivative aggregator), plus the fast-tier wiring the head carries:

* secret-scan red: ``public-owned-paths.txt`` drifted from its generator,
  so the freeze-list step exits 1.
* change-detection red: ``vendor/toolkit-files.txt`` lists case-gate rows
  for paths shipped by no release, so the restore step exits 1 with a pin
  drift message naming the recorded-command gate.
* pr-lint red: the lint job runs on the paid fallback pool while its first
  step refuses any runner that is not a private ephemeral runner.
* aggregator red: derivative of the above needed jobs ending failure.

Stdlib only, hermetic, under a second. Run with
``python3 -m unittest test_pr5_acceptance`` (also wired into
``scripts/verify-fast.sh``).
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The four ops-owned gates the restore step fails on: each manifest row
# points them at a release archive that ships none of them.
FIRST_PARTY_GATES = [
    "verification/case_9e76f76d-recorded-command.sh",
    "verification/case_9e76f76d-recorded-command.txt",
    "verification/case_46e26fa6-mutation-gate.sh",
    "verification/case_f909ca1f-mutation-gate.sh",
]

WORKFLOWS = [
    ".github/workflows/ci.yml",
    ".github/workflows/docs-fast-path.yml",
    ".github/workflows/pr-lint.yml",
]


def runs_on_values(text):
    """Return every ``runs-on`` value in a workflow file."""
    return re.findall(r"(?m)^\s*runs-on:\s*(.+?)\s*$", text)


def git_lines(*argv):
    completed = subprocess.run(
        ["git", *argv],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.splitlines()


class ManifestRows(unittest.TestCase):
    def test_no_release_rows_for_case_gates(self):
        """No vendored row may point a first-party gate at a release."""
        manifest = (ROOT / "vendor" / "toolkit-files.txt").read_text()
        bad = [
            line
            for line in manifest.splitlines()
            if line.split("|")[0] in FIRST_PARTY_GATES and "|v" in line
        ]
        self.assertEqual(
            bad,
            [],
            "unrestorable manifest rows (restore exits 1): %r" % (bad,),
        )

    def test_first_party_gates_tracked(self):
        """The four gates exist in the tree (sync skips tracked paths)."""
        missing = [p for p in FIRST_PARTY_GATES if not (ROOT / p).is_file()]
        self.assertEqual(missing, [], "first-party gates missing: %r" % (missing,))


class FreezeList(unittest.TestCase):
    def test_matches_generator(self):
        """The freeze list equals its generator output (secret-scan step)."""
        manifest = (ROOT / "vendor" / "toolkit-files.txt").read_text()
        dests = sorted(
            line.split("|")[0]
            for line in manifest.splitlines()
            if line.strip()
        )
        dests = [d for d in dests if not d.startswith("plugins/model-selection/")]
        base_tree = git_lines("ls-tree", "-r", "--name-only", "origin/main")
        if base_tree is None:
            base_tree = git_lines("ls-tree", "-r", "--name-only", "HEAD")
        self.assertIsNotNone(base_tree, "git ls-tree failed")
        work_tree = git_lines("ls-files")
        self.assertIsNotNone(work_tree, "git ls-files failed")
        survivors = set(base_tree) & set(work_tree)
        regen = sorted(d for d in dests if d not in survivors)
        committed_text = (ROOT / "public-owned-paths.txt").read_text()
        committed = sorted(
            line.strip()
            for line in committed_text.splitlines()
            if line.strip() and not line.startswith("#")
        )
        self.assertEqual(
            committed,
            regen,
            "freeze list drifted: committed %d paths, generator %d paths "
            "(re-run the command in the file header)" % (len(committed), len(regen)),
        )


class RunnerPools(unittest.TestCase):
    def test_no_paid_fallback(self):
        """No workflow may schedule on the paid fallback pool."""
        offenders = []
        for rel in WORKFLOWS:
            text = (ROOT / rel).read_text()
            for value in runs_on_values(text):
                if "ubuntu-latest" in value:
                    offenders.append("%s: %s" % (rel, value))
        self.assertEqual(
            offenders, [], "paid-fallback runs-on (lint red): %r" % (offenders,)
        )

    def test_pr_lint_routes_to_private_runner(self):
        """The lint runs-on value must satisfy its own runner proof."""
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        values = runs_on_values(text)
        self.assertTrue(values, "lint has no runs-on value")
        bad = [
            value
            for value in values
            if "garm" not in value
            and "self-hosted" not in value
            and "fromJSON" not in value
        ]
        self.assertEqual(
            bad,
            [],
            "lint runs-on refused by its runner proof step: %r" % (bad,),
        )

    def test_pr_lint_runner_guard_present(self):
        """The lint job still refuses non-private runners (not weakened)."""
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        self.assertIn("not a GARM runner", text)


class QuarantineWiring(unittest.TestCase):
    def test_fast_tier_wires_custody_suite(self):
        """The fast tier runs the quarantine custody suite."""
        text = (ROOT / "scripts" / "verify-fast.sh").read_text()
        self.assertIn("test_nongit_quarantine_custody", text)

    def test_custody_suite_exists(self):
        """The quarantine custody suite file exists in the tree."""
        self.assertTrue(
            (ROOT / "ops" / "productivity" / "test_nongit_quarantine_custody.py").is_file()
        )


class PublicText(unittest.TestCase):
    def test_no_tracker_ids_in_this_suite(self):
        """Public text carries no tracker ids (hygiene gate)."""
        text = Path(__file__).read_text()
        hits = re.findall(r"\b[A-Z]{2,}-\d+", text)
        self.assertEqual(hits, [], "tracker ids in public text: %r" % (hits,))


if __name__ == "__main__":
    unittest.main()
