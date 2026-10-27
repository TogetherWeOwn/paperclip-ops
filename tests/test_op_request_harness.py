"""Safety regressions for BOTH publication entrypoints (no live paths).

Run: python3 -B -m unittest discover -s tests -p test_op_request_harness.py -v
Breaks caught: continuing after mktemp/provenance/setup failure; zero/partial/
duplicate rewrites; late drift; falsely green failure cases with broken fakes.
"""

from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import op_request_harness as h
from op_request_publication import happy, post_publication_diagnostic, suite


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-check.", dir=h.scratch_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "op-request"
        self.baseline = h.FIXTURE.read_bytes()
        self.source.write_bytes(self.baseline)
        self.manifest = self.source.with_name("op-request.sha256")
        self.pin()
        self.origins = [h.Provenance.load(self.source, kind) for kind in ("fixed", "truthful")]

    def pin(self):
        self.manifest.write_text(h.digest(self.source.read_bytes()) + "  op-request\n")

    def refusal(self, action):
        with mock.patch.object(h, "run_process") as execute:
            with self.assertRaises((h.Refused, OSError)):
                action()
            execute.assert_not_called()

    def exercise(self, origin, fault=""):
        with h.publication_case(origin, fault) as case:
            case.run()

    def test_real_positive_control_reaches_private_publication(self):
        for origin in self.origins:
            with self.subTest(kind=origin.kind), h.publication_case(origin) as case:
                result = case.run()
                happy(case, result)
                self.assertEqual(result.returncode, 0)
                self.assertTrue(case.files()[0].is_relative_to(case.path))

    def test_mktemp_failure_or_invalid_output_cannot_reach_setup_or_execution(self):
        # /bin is only an inert string in a mocked RESULT; it is never an operand.
        replies = [(1, ""), (1, str(self.root) + "/opreq.abcdefgh\n"),
                   (0, ""), (0, "/bin\n"), (0, "/\n"), (0, "relative\n"),
                   (0, str(self.root) + "/../opreq.abcdefgh\n"),
                   (0, str(self.root) + "/opreq.abcdefgh\nextra\n")]
        for origin in self.origins:
            for code, output in replies:
                with self.subTest(kind=origin.kind, code=code, output=output), ExitStack() as stack:
                    stack.enter_context(mock.patch.object(h, "scratch_root", return_value=self.root))
                    stack.enter_context(mock.patch.object(h.subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")))
                    setup = stack.enter_context(mock.patch.object(h, "PublicationCase"))
                    mkdir = stack.enter_context(mock.patch.object(Path, "mkdir"))
                    write = stack.enter_context(mock.patch.object(h, "write_new"))
                    chmod = stack.enter_context(mock.patch.object(Path, "chmod"))
                    cleanup = stack.enter_context(mock.patch.object(h.shutil, "rmtree"))
                    self.refusal(lambda: self.exercise(origin))
                    for call in (setup, mkdir, write, chmod, cleanup):
                        call.assert_not_called()

    def test_mktemp_path_must_be_real_owned_private_empty_directory(self):
        candidate = self.root / "opreq.abcdefgh"
        target = self.root / "symlink-target"
        target.mkdir(mode=0o700)
        for flavor in ("missing", "file", "symlink", "writable", "nonempty", "foreign-owner"):
            with self.subTest(flavor=flavor), ExitStack() as stack:
                if flavor == "file":
                    candidate.write_text("do not delete")
                elif flavor == "symlink":
                    candidate.symlink_to(target, target_is_directory=True)
                elif flavor not in ("missing",):
                    candidate.mkdir(mode=0o700)
                    if flavor == "writable":
                        candidate.chmod(0o777)
                    if flavor == "nonempty":
                        (candidate / "existing").write_text("retain")
                    if flavor == "foreign-owner":
                        stack.enter_context(mock.patch.object(h.os, "geteuid", return_value=os.geteuid() + 1))
                stack.enter_context(mock.patch.object(h, "scratch_root", return_value=self.root))
                stack.enter_context(mock.patch.object(h.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, str(candidate) + "\n", "")))
                setup = stack.enter_context(mock.patch.object(h, "PublicationCase"))
                cleanup = stack.enter_context(mock.patch.object(h.shutil, "rmtree"))
                self.refusal(lambda: self.exercise(self.origins[0]))
                setup.assert_not_called()
                cleanup.assert_not_called()
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
            elif candidate.is_dir():
                for child in candidate.iterdir():
                    child.unlink()
                candidate.rmdir()

    def test_strict_single_entry_manifest_refuses_before_even_mktemp(self):
        checksum = h.digest(self.baseline).encode()
        valid = checksum + b"  op-request\n"
        bad = [None, b"", valid[:-1], valid + b"\n", valid * 2,
               checksum.upper() + b"  op-request\n", checksum + b" op-request\n",
               checksum + b" *op-request\n", checksum + b"  ../op-request\n",
               checksum + b"  op-request\r\n", b"x" * 64 + b"  op-request\n",
               b"0" * 64 + b"  op-request\n", b"# comment\n" + valid]
        for contents in bad:
            with self.subTest(contents=contents):
                if self.manifest.exists():
                    self.manifest.unlink()
                if contents is not None:
                    self.manifest.write_bytes(contents)
                with mock.patch.object(h, "sandbox") as temporary:
                    self.refusal(lambda: self.exercise(h.Provenance.load(self.source, "fixed")))
                    temporary.assert_not_called()

    def test_symlink_manifest_and_source_are_refused(self):
        alternate = self.root / "saved"
        self.manifest.rename(alternate)
        self.manifest.symlink_to(alternate)
        self.refusal(lambda: h.Provenance.load(self.source, "fixed"))
        self.manifest.unlink()
        self.pin()
        self.source.rename(self.root / "source-bytes")
        self.source.symlink_to(self.root / "source-bytes")
        for kind in ("fixed", "truthful"):
            self.refusal(lambda: h.Provenance.load(self.source, kind))

    def test_source_hash_drift_refuses_both_suites_without_execution(self):
        self.source.write_bytes(self.baseline + b"# drift\n")
        for origin in self.origins:
            self.refusal(lambda: h.Provenance.load(self.source, origin.kind))
            self.refusal(lambda: self.exercise(origin))
        self.pin()  # even a valid NEW manifest cannot retarget an in-flight suite
        self.refusal(lambda: self.exercise(self.origins[0]))

    def test_missing_or_duplicate_path_assignments_refuse_after_valid_hash(self):
        for name in (b"LOG", b"QUEUE"):
            original = next(line for line in self.baseline.splitlines(keepends=True) if line.startswith(name + b"="))
            for changed in (self.baseline.replace(original, b""), self.baseline + original):
                with self.subTest(name=name, changed=len(changed)):
                    self.source.write_bytes(changed)
                    self.pin()  # hash passes: the path gate, not provenance, must refuse
                    origin = h.Provenance.load(self.source, "fixed")
                    self.refusal(lambda: self.exercise(origin))

    def test_failed_empty_diff_partial_and_unrelated_rewrites_never_execute(self):
        render = h.rewrite
        def partial(data, replacements):
            return render(data, {"LOG": replacements["LOG"]})
        def unrelated(data, replacements):
            return render(data, replacements).replace(b"set -uo pipefail", b"set -u")
        for replacement in (lambda data, values: data, partial, unrelated):
            for origin in self.origins:
                with self.subTest(kind=origin.kind, replacement=replacement.__name__), mock.patch.object(h, "rewrite", side_effect=replacement):
                    self.refusal(lambda: self.exercise(origin))
        with mock.patch.object(h, "rewrite", side_effect=OSError("injected rewrite failure")):
            self.refusal(lambda: self.exercise(self.origins[0]))

    def test_silent_or_partial_runner_write_is_checked_again_before_execution(self):
        write = h.write_new
        for content in (None, b"#!/bin/bash\n", self.baseline):
            for origin in self.origins:
                for fault in ("", "log", "stdout"):
                    def broken_write(path, data):
                        if path.name != "run.sh":
                            return write(path, data)
                        if content is not None:
                            write(path, content)
                    with self.subTest(kind=origin.kind, fault=fault, content_length=len(content or b"")), mock.patch.object(h, "write_new", side_effect=broken_write):
                        self.refusal(lambda: self.exercise(origin, fault))

    def test_every_case_revalidates_runner_and_source_immediately_before_execution(self):
        for fault in ("", "cp", "chown", "chmod", "log", "stdout"):
            for kind in ("fixed", "truthful"):
                for drift in ("runner", "source", "manifest"):
                    if kind == "truthful" and drift == "manifest":
                        continue
                    with self.subTest(fault=fault, kind=kind, drift=drift):
                        self.source.write_bytes(self.baseline)
                        self.pin()
                        origin = h.Provenance.load(self.source, kind)
                        with h.publication_case(origin, fault) as case:
                            if drift == "runner":
                                case.runner.write_bytes(self.baseline)  # empty diff is NOT proof of sandboxing
                            elif drift == "source":
                                self.source.write_bytes(self.baseline + b"# drift\n")
                            else:
                                self.manifest.write_text("malformed\n")
                            self.refusal(case.run)
                            self.assertEqual(case.events(), [])
                            self.assertEqual(case.files(), [])

    def test_all_setup_errors_abort_not_just_increment_a_counter(self):
        for origin in self.origins:
            for fault in ("", "log", "stdout"):
                for operation in ("mkdir", "chmod", "write"):
                    with self.subTest(kind=origin.kind, fault=fault, operation=operation), ExitStack() as stack:
                        if operation == "write":
                            stack.enter_context(mock.patch.object(h, "write_new", side_effect=OSError("setup write failed")))
                        else:
                            stack.enter_context(mock.patch.object(Path, operation, side_effect=OSError("setup operation failed")))
                        self.refusal(lambda: self.exercise(origin, fault))
        with mock.patch.object(h, "regular_bytes", side_effect=OSError("read failed")):
            self.refusal(lambda: self.exercise(self.origins[0]))

    def test_failed_happy_control_cannot_yield_vacuous_failure_passes(self):
        refusal = subprocess.CompletedProcess([], 3, "", "not-operator-owned")
        output = io.StringIO()
        with mock.patch.object(h, "run_process", return_value=refusal) as execute, redirect_stdout(output):
            with self.assertRaisesRegex(h.Refused, "P1 control failed"):
                suite(self.origins[0])
            self.assertEqual(execute.call_count, 1)
        self.assertIn("FAIL - P1", output.getvalue())
        self.assertNotIn("ok   - F", output.getvalue())

    def test_diagnostic_checks_reject_absence_claims_and_missing_instructions(self):
        dest = self.root / "request-identity.json"
        for confirmed in (False, True):
            audit = "audit complete" if confirmed else "audit unconfirmed"
            message = f"publication complete dest={dest}; {audit}; do not retry; operator inspection required"
            post_publication_diagnostic(message, dest, confirmed)
            for fragment in ("publication complete", str(dest), audit, "do not retry", "operator inspection required"):
                with self.subTest(confirmed=confirmed, fragment=fragment), self.assertRaises(AssertionError):
                    post_publication_diagnostic(message.replace(fragment, "omitted"), dest, confirmed)
            with self.assertRaises(AssertionError):
                post_publication_diagnostic(f"publication incomplete {dest}; {audit}; do not retry; operator inspection required", dest, confirmed)


if __name__ == "__main__":
    unittest.main()
