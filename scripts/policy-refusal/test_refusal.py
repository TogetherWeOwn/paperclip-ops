#!/usr/bin/env python3
"""Arguments and runner markers cannot turn the refusal into evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT = Path(__file__).with_name("refusal.py")
EXPECTED = {
    "qualification": "POLICY_ONLY",
    "stock_reference": "UNESTABLISHED",
    "upstream_defect_eligible": False,
    "scenario_observation": "NOT_RUN",
    "verdict": "ERROR",
}


class RefusalTest(unittest.TestCase):
    def test_forged_inputs_do_not_change_result(self):
        env = dict(os.environ, GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted")
        for args in ([], ["REPRODUCED"], ["NOT_REPRODUCED"],
                     ["--complete=true"], ["invalid", "REPRODUCED"]):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(SCRIPT), *args],
                    capture_output=True, text=True, check=False, env=env, timeout=5,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout), EXPECTED)
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
