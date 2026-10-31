"""Additional regression controls for the immutable permissions acceptance suite."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_workflow_permissions_json_acceptance as acceptance
from test_workflow_permissions_json_acceptance import CLIFixture, GOOD, MISSING


class PermissionsCompanion(CLIFixture):
    def test_help_named_files_after_separator_are_checked_in_both_formats(self):
        for name in ("--help", "-h", "--json"):
            for contents, exit_code, verdict in ((GOOD, 0, "pass"), (MISSING, 1, "fail")):
                with self.subTest(name=name, verdict=verdict):
                    self.fixture(contents, name)
                    text = self.invoke("--", name)
                    self.assert_exit(text, exit_code)
                    self.assertNotIn("usage:", text.stdout.lower())
                    result = self.invoke("--json", "--", name)
                    self.assert_exit(result, exit_code)
                    report = self.report(result, checked=1, unmeasurable=0, verdict=verdict)
                    self.assertEqual([f["file"] for f in report["findings"]],
                                     [name] if exit_code else [])
                    self.assertEqual(report["errors"], [])

    def test_error_codes_have_their_exact_retryable_boolean(self):
        good = self.fixture(GOOD)
        invalid = self.fixture("jobs: [broken\n", "invalid.yml")
        cases = (
            ((invalid,), False, "invalid_workflow", False, 1),
            ((self.cwd / "absent.yml",), False, "unreadable_workflow", True, 1),
            ((good,), True, "yaml_parser_unavailable", True, 1),
            ((), False, "no_workflow_inputs", False, 0),
            (("--unknown",), False, "invalid_arguments", False, 0),
        )
        for args, deny_yaml, code, retryable, unmeasurable in cases:
            with self.subTest(code=code):
                result = self.invoke("--json", *args, deny_yaml=deny_yaml)
                self.assert_exit(result, 2)
                report = self.report(result, checked=0, unmeasurable=unmeasurable, verdict="inconclusive")
                self.assertEqual(len(report["errors"]), 1)
                self.assertEqual(report["errors"][0]["code"], code)
                self.assertIs(report["errors"][0]["retryable"], retryable)

    def test_missing_parser_counts_both_selected_inputs(self):
        first = self.fixture(GOOD, "first.yml")
        second = self.fixture(MISSING, "second.yaml")
        for args in ((first, second), ("--json", first, second)):
            with self.subTest(args=args):
                result = self.invoke(*args, deny_yaml=True)
                self.assert_exit(result, 2)
                if args[0] == "--json":
                    report = self.report(result, checked=0, unmeasurable=2,
                                         verdict="inconclusive")
                    self.assertEqual(report["findings"], [])
                    self.assertEqual(len(report["errors"]), 1)
                    self.assertEqual(report["errors"][0]["code"], "yaml_parser_unavailable")
                    self.assertIsNone(report["errors"][0]["file"])
                    self.assertIs(report["errors"][0]["retryable"], True)

    def test_each_process_audit_event_is_denied(self):
        # Each probe must hit its own event; none can be masked by Popen.
        probes = (
            ("os.exec", "os.execv('/bin/true', ['true'])"),
            ("os.system", "os.system('true')"),
            ("os.posix_spawn", "os.posix_spawn('/bin/true', ['true'], {})"),
            ("os.fork", "os.fork()"),
            ("os.forkpty", "os.forkpty()"),
        )
        for event, operation in probes:
            with self.subTest(event=event):
                probe = ("import os\ntry:\n " + operation + "\n"
                         "except PermissionError:\n pass\n"
                         "else:\n raise AssertionError('process escaped')\n")
                result = self.invoke(probe, mode="probe", expected_denials=(event,))
                self.assert_exit(result, 0)


class CheckoutStateControls(unittest.TestCase):
    def setUp(self):
        self.git = shutil.which("git")
        self.assertIsNotNone(self.git, "git on the parent PATH is required")
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        self.temp = tempfile.TemporaryDirectory(prefix="checkout-observer-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.repo = self.cwd / "repo"
        self.repo.mkdir()
        self.log = self.cwd / "helper.log"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(HOME=str(self.cwd), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.run_git("init", "--quiet", "--template=", ".")
        self.path = self.repo / "tracked.txt"
        self.path.write_text("before\n")
        self.run_git("add", "tracked.txt")
        self.run_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--no-gpg-sign",
                     "-m", "fixture", "-m", "Co-Authored-By: Paperclip <noreply@paperclip.ing>")
        self.path.write_text("after\n")
        self.helper = self.cwd / "git-helper"
        self.helper.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"log = Path({str(self.log)!r})\n"
            "with log.open('a') as stream:\n stream.write('invoked\\n')\n"
            "print('helper-' + str(len(log.read_text().splitlines())))\n"
        )
        self.helper.chmod(0o755)

    def run_git(self, *args):
        return subprocess.run([self.git, *args], cwd=self.repo, env=self.env,
                              text=True, capture_output=True, timeout=15, check=True)

    def snapshot(self, **env):
        with patch.object(acceptance, "ROOT", self.repo), \
                patch.object(acceptance, "CHECKER", self.repo / "check_workflow_job_permissions.py"), \
                patch.dict(os.environ, dict(self.env, **env), clear=True):
            return acceptance.checkout_state()

    def assert_helper_is_suppressed(self):
        self.assertIn("invoked", self.log.read_text(), "positive control must execute the helper")
        self.log.unlink()
        before = self.snapshot()
        self.assertEqual(before, self.snapshot(), "unchanged checkout must have a stable fingerprint")
        self.assertFalse(self.log.exists(), "observer executed a Git helper")
        self.path.write_text("another real change\n")
        self.assertNotEqual(before, self.snapshot(), "built-in diff must still detect real changes")

    def test_checkout_fingerprint_disables_external_diff(self):
        self.env["GIT_EXTERNAL_DIFF"] = str(self.helper)
        self.run_git("diff", "--binary", "HEAD")
        self.assert_helper_is_suppressed()

    def test_checkout_fingerprint_disables_text_conversion(self):
        attributes = self.repo / ".git/info/attributes"
        attributes.parent.mkdir(exist_ok=True)
        attributes.write_text("*.txt diff=fixture\n")
        self.run_git("config", "diff.fixture.textconv", str(self.helper))
        self.run_git("diff", "--binary", "HEAD")
        self.assert_helper_is_suppressed()

    def test_checkout_fingerprint_disables_fsmonitor(self):
        self.run_git("config", "core.fsmonitor", str(self.helper))
        self.run_git("status", "--porcelain=v1")
        self.assert_helper_is_suppressed()

    def test_checkout_fingerprint_uses_git_from_parent_path(self):
        bin_dir = self.cwd / "parent-bin"
        bin_dir.mkdir()
        git = bin_dir / "git"
        git.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"with open({str(self.log)!r}, 'a') as stream:\n stream.write('parent-git\\n')\n"
            f"os.environ['PATH'] = {self.env['PATH']!r}\n"
            f"os.execv({self.git!r}, [{self.git!r}, *sys.argv[1:]])\n"
        )
        git.chmod(0o755)
        before = self.snapshot(PATH=str(bin_dir))
        self.assertEqual(before, self.snapshot(PATH=str(bin_dir)))
        self.assertTrue(self.log.exists(), "observer ignored Git on the supported parent PATH")
        self.assertIn("parent-git", self.log.read_text())

    def test_checkout_fingerprint_reports_missing_parent_git(self):
        bin_dir = self.cwd / "empty-bin"
        bin_dir.mkdir()
        with self.assertRaisesRegex(RuntimeError, "git.*required"):
            self.snapshot(PATH=str(bin_dir))
