"""Additional regression controls for the immutable permissions acceptance suite."""
import sys

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
