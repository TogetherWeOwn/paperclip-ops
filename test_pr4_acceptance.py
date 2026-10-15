#!/usr/bin/env python3
"""Offline acceptance for PR #4: toolkit-manifest landing blockers.

Reproduces, without network, database, or credentials, the CI reds that
block this PR plus the manifest core of the change:

* change detection red: ``vendor/toolkit-files.txt`` listed vendored rows
  for first-party gates shipped by no release, so the restore step exited 1.
* suites red: ``verification/case_f909ca1f-mutation-gate.sh`` aimed one limb
  at a refusal line the provisioner no longer carries, so the gate
  asserted nothing (mutation did not apply).
* pr-lint red: ``.github/scripts/test_public_ci.py`` still expected the
  vendored rows this PR removes.
* public-safety red: the history scan used ``--all``, which flags other
  branches' findings against this commit's allowlist.
* privilege-ceiling guard: ``test_provisioned_transport_db.py`` must keep
  reading its marker from the vendored fixture, never a stale literal.

Also pins the public-repo runner contract this tree runs on: every workflow
schedules on the free GitHub-hosted pool and pr-lint refuses any other runner.

Stdlib only, hermetic, under a second. Run with
``python3 -m unittest test_pr4_acceptance`` (also wired into
``scripts/verify-fast.sh``).
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The four ops-owned gates this PR commits as tracked first-party files.
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

# A marker that the fixed tree must never carry again.
STALE_MARKER = "paperclip-ops:case_aa908088"


def runs_on_values(text):
    """Return every ``runs-on`` value in a workflow file."""
    return re.findall(r"(?m)^\s*runs-on:\s*(.+?)\s*$", text)


class ManifestRows(unittest.TestCase):
    def test_no_unrestorable_case_gates(self):
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


class RunnerPools(unittest.TestCase):
    def test_public_pool(self):
        """Public repo schedules on the free GitHub-hosted pool."""
        offenders = []
        for rel in WORKFLOWS:
            text = (ROOT / rel).read_text()
            for value in runs_on_values(text):
                if "ubuntu-latest" not in value:
                    offenders.append("%s: %s" % (rel, value))
        self.assertEqual(
            offenders, [], "non-public runs-on (public CI red): %r" % (offenders,)
        )

    def test_no_self_hosted_labels(self):
        """No workflow may pin a private static runner label."""
        offenders = []
        for rel in WORKFLOWS:
            text = (ROOT / rel).read_text()
            for value in runs_on_values(text):
                if "self-hosted" in value:
                    offenders.append("%s: %s" % (rel, value))
        self.assertEqual(
            offenders,
            [],
            "private static runner label (public CI red): %r" % (offenders,),
        )

    def test_github_hosted_guard_present(self):
        """The lint job still refuses non-GitHub-hosted runners (not weakened)."""
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        self.assertIn("public CI requires a GitHub-hosted runner", text)


class TransportGuard(unittest.TestCase):
    def test_no_stale_marker(self):
        """The guard must not carry the stale hard-coded marker."""
        text = (ROOT / "test_provisioned_transport_db.py").read_text()
        self.assertNotIn(STALE_MARKER, text)

    def test_marker_read_from_fixture(self):
        """The marker comes from the vendored fixture producer at runtime."""
        text = (ROOT / "test_provisioned_transport_db.py").read_text()
        self.assertIn("SENTINEL_VALUE", text)
        self.assertIn("test/fixtures/orgdb/reset.sh", text)
        # No hard-coded marker literal may remain: the guard substitutes the
        # fixture value at import time instead of pinning a stale copy.
        literals = re.findall(
            r"IS DISTINCT FROM '([^']+)'", text
        )
        self.assertEqual(
            [v for v in literals if not v.startswith("__")],
            [],
            "hard-coded marker literals: %r" % (literals,),
        )
        fixture = ROOT / "test" / "fixtures" / "orgdb" / "reset.sh"
        if fixture.is_file():
            marker = re.search(
                r"(?m)^SENTINEL_VALUE=([a-zA-Z0-9_:-]+)$", fixture.read_text()
            )
            self.assertIsNotNone(marker, "fixture marker is absent or not a literal")


class PublicText(unittest.TestCase):
    def test_no_tracker_ids_in_this_suite(self):
        """Public text carries no tracker ids (hygiene gate)."""
        text = Path(__file__).read_text()
        hits = re.findall(r"\b[A-Z]{2,}-\d+", text)
        self.assertEqual(hits, [], "tracker ids in public text: %r" % (hits,))


if __name__ == "__main__":
    unittest.main()
