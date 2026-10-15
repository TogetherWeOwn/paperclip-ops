#!/usr/bin/env python3
"""Public CI regression checks; no network, host inventory or credentials."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicCI(unittest.TestCase):
    def test_all_runner_routes_are_standard_hosted(self):
        routes = []
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            for line in workflow.read_text().splitlines():
                if re.match(r"\s*runs-on(?:-json)?:", line):
                    routes.append(line)
                    self.assertIn("ubuntu-latest", line, str(workflow))
                    self.assertNotIn("self-hosted", line, str(workflow))
        self.assertTrue(routes)

    def test_required_producers_handle_merge_groups(self):
        for name in ("ci.yml", "pr-lint.yml", "public-safety.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertRegex(text, r"(?m)^  merge_group:", name)

    def test_lint_attests_hosted_environment_not_runner_name(self):
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        self.assertIn('"${RUNNER_ENVIRONMENT:-}" == github-hosted', text)
        self.assertNotIn('case "${RUNNER_NAME:-}"', text)

    def test_renamed_archive_sources_use_public_paths(self):
        manifest = (ROOT / "vendor/toolkit-files.txt").read_text().splitlines()
        expected = {
            "verification/case_9e76f76d-recorded-command.sh": "verification/recorded-monitor-command-gate.sh",
            "verification/case_9e76f76d-recorded-command.txt": "verification/recorded-monitor-command.txt",
            "verification/case_46e26fa6-mutation-gate.sh": "verification/plugin-deploy-drift-detector-mutation-gate.sh",
            "verification/case_f909ca1f-mutation-gate.sh": "verification/grant-attribution-resolver-mutation-gate.sh",
        }
        for dest, source in expected.items():
            self.assertIn(f"{dest}|{source}", manifest)


if __name__ == "__main__":
    unittest.main()
