"""Fast harness checks with stub suites; no real mutation matrix runs here."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


GATE = Path(__file__).resolve().parent / "verification/private-mutation-gates.sh"


class PrivateMutationHarnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = GATE.read_text()
        names = (
            "mutation_run_is_unattributable",
            "write_mutation_suite_wrapper",
            "notify_suite",
        )
        cls.functions = "\n".join(
            re.search(rf"^{name}\(\) \{{\n.*?^\}}$", text, re.M | re.S).group(0)
            for name in names
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}

    def run_suite(self, body="exit 0"):
        suite = self.root / "stub-suite.sh"
        suite.write_text("#!/usr/bin/env bash\n" + body + "\n")
        suite.chmod(0o755)
        script = self.functions + """
write_mutation_suite_wrapper "$1" stub-suite.sh || exit 99
cd "$1" || exit 99
notify_suite "$1" "$1/output"
rc=$?
if mutation_run_is_unattributable "$rc"; then
  printf 'unattributable'
else
  printf 'killed'
fi
exit "$rc"
"""
        return subprocess.run(
            ["bash", "-c", script, "fixture", str(self.root)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def capture_timeout(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "timeout"
        stub.write_text(
            '#!/usr/bin/env bash\n'
            'printf "%s\\n" "$NOTIFY_SH" "$@" > "$CAPTURE"\n'
        )
        stub.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        self.env["CAPTURE"] = str(self.root / "args")
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.root / "args").read_text().splitlines()

    def test_notification_default_budget_and_staged_paths(self):
        self.assertEqual(
            self.capture_timeout(),
            [str(self.root / "notify_paperclip_issue.sh"), "180",
             str(self.root / ".mutation-suite-wrapper.sh")],
        )

    def test_notification_budget_override(self):
        self.env["NOTIFY_MUTATION_TIMEOUT"] = "0.05"
        self.assertEqual(self.capture_timeout()[1], "0.05")

    def test_only_suite_assertion_failure_counts_as_kill(self):
        for suite_rc in (0, 1, 42, 43, 124, 125, 126, 127):
            with self.subTest(suite_rc=suite_rc):
                result = self.run_suite(f"exit {suite_rc}")
                expected_rc = {1: 42, 42: 43}.get(suite_rc, suite_rc)
                self.assertEqual(result.returncode, expected_rc, result.stderr)
                self.assertEqual(
                    result.stdout, "killed" if suite_rc == 1 else "unattributable"
                )

    def argv_mutant(self):
        invocation = re.search(
            r'^notify_mutation "notify-credential-on-argv" \\\n.*?(?=^notify_mutation )',
            GATE.read_text(), re.M | re.S,
        ).group(0)
        captured = subprocess.run(
            ["bash", "-c", 'notify_mutation() { shift; printf "%s\\0" "$@"; };\n'
             + invocation], env=self.env, capture_output=True, check=True,
        )
        expressions = captured.stdout.decode().rstrip("\0").split("\0")
        notifier = self.root / "notify_paperclip_issue.sh"
        notifier.write_text((GATE.parent.parent / notifier.name).read_text())
        for expression in expressions:
            subprocess.run(["sed", "-i", expression, str(notifier)], check=True)
        subprocess.run(["bash", "-n", str(notifier)], check=True)
        notifier.chmod(0o755)
        return notifier

    def test_argv_mutant_reaches_curl_without_stray_redirection(self):
        notifier = self.argv_mutant()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(
            '#!/usr/bin/python3\n'
            'import json, os, sys\n'
            'from pathlib import Path\n'
            'args = sys.argv[1:]\n'
            'Path(os.environ["CAPTURE"]).write_text(json.dumps(args))\n'
            'Path(os.environ["CONFIG_CAPTURE"]).write_text(\n'
            '    Path(args[args.index("--config") + 1]).read_text())\n'
            'if os.environ["CURL_FAIL"] == "1":\n'
            '    print("stub transport failure", file=sys.stderr)\n'
            '    sys.exit(7)\n'
            'print("200", end="")\n'
        )
        curl.chmod(0o755)
        self.env.update({
            "PATH": str(bin_dir) + os.pathsep + self.env["PATH"],
            "CAPTURE": str(self.root / "curl-args.json"),
            "CONFIG_CAPTURE": str(self.root / "curl-config"),
            "TMPDIR": str(self.root),
            "PAPERCLIP_API_URL": "http://fixture.invalid",
            "PAPERCLIP_API_KEY": "fixture-key-not-a-credential",
        })
        payload = json.dumps({
            "requestId": "REQ-fixture", "decision": "approved",
            "recipientRole": "fixture", "notifyIssue": "fixture-issue",
            "body": "literal $b payload",
        })
        for curl_fail, expected_rc in (("0", 0), ("1", 70)):
            with self.subTest(curl_fail=curl_fail):
                self.env["CURL_FAIL"] = curl_fail
                result = subprocess.run(
                    ["bash", str(notifier)], input=payload, cwd=self.root,
                    env=self.env, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, expected_rc, result.stderr)
                args = json.loads((self.root / "curl-args.json").read_text())
                self.assertIn("Authorization: Bearer fixture-key-not-a-credential", args)
                config = (self.root / "curl-config").read_text()
                data = next(line.removeprefix("data-binary = ")
                            for line in config.splitlines() if line.startswith("data-binary = "))
                self.assertIn("literal $b payload", json.loads(json.loads(data))["body"])
                self.assertIn('url = "http://fixture.invalid/api/issues/fixture-issue/comments"',
                              config)
                self.assertIn('header = "Authorization: Bearer fixture-key-not-a-credential"',
                              config)
                self.assertFalse((self.root / "1").exists())
                if curl_fail == "1":
                    self.assertIn("stub transport failure", result.stderr)

    def test_argv_mutant_kill_requires_argv_detector(self):
        notifier = self.argv_mutant()
        source = GATE.parent.parent / "test_notify_transport.sh"
        section = source.read_text().split('hdr "2. A requester cannot steer', 1)[0]
        self.assertIn('hdr "1. The credential never reaches argv"', section)
        section += '\nexit "$(( FAIL > 0 ))"\n'
        detector = 'if grep -q \'CANARY-KEY-DO-NOT-LEAK\' "$ARGV"; then'
        self.assertEqual(section.count(detector), 1)
        suite = self.root / "credential-suite.sh"
        self.env["TMPDIR"] = str(self.root)
        baseline = GATE.parent.parent / "notify_paperclip_issue.sh"
        for detector_enabled in (True, False):
            suite.write_text(section if detector_enabled else section.replace(detector, "if false; then"))
            for subject in (baseline, notifier):
                with self.subTest(detector_enabled=detector_enabled, subject=subject):
                    env = {**self.env, "NOTIFY_SH": str(subject)}
                    result = subprocess.run(
                        ["bash", str(suite)], cwd=self.root, env=env,
                        capture_output=True, text=True, timeout=5,
                    )
                    expected = int(detector_enabled and subject == notifier)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertFalse((self.root / "1").exists())

    def test_actual_timeout_is_not_a_kill(self):
        self.env["NOTIFY_MUTATION_TIMEOUT"] = "0.05"
        result = self.run_suite("sleep 10")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual(result.stdout, "unattributable")


if __name__ == "__main__":
    unittest.main()
