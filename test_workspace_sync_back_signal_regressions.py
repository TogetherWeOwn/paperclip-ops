"""Builder-owned offline regressions pinning two sync-back review mutants.

Discovery (run from the repository root):
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_workspace_sync_back_signal_regressions
Combined with the independent acceptance contract:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_workspace_sync_back_signal_acceptance test_workspace_sync_back_signal_regressions

Reuses the independent acceptance module's real entrypoints, sanitized
fixture, child confinement and sink stubs without modifying that module:
Paperclip Review required these pins, and the acceptance file is owned by
the Test Author. No production runtime, alert activation, transport or
rollout is qualified here.

Pins:
* overflowing float exponents (1e999/-1e999) in ignored fields — and nested
  in ignored containers — are unknown observations, never within_threshold
  recoveries, and the wrapper delivers them as success=false;
* the classifier's bounded stdin read returns unknown/input_too_large for
  over-cap input while the producer pipe is still open, instead of blocking
  for EOF.
"""

import json
import os
import subprocess
import unittest

import test_workspace_sync_back_signal_acceptance as acceptance


class WorkspaceSyncBackSignalRegressions(unittest.TestCase):
    setUp = acceptance.WorkspaceSyncBackAcceptance.setUp
    invoke = acceptance.WorkspaceSyncBackAcceptance.invoke
    report = acceptance.WorkspaceSyncBackAcceptance.report
    classification = acceptance.WorkspaceSyncBackAcceptance.classification
    unknown = acceptance.WorkspaceSyncBackAcceptance.unknown
    requests = acceptance.WorkspaceSyncBackAcceptance.requests
    delivery = acceptance.WorkspaceSyncBackAcceptance.delivery
    script = acceptance.WorkspaceSyncBackAcceptance.script

    def test_overflowing_exponent_in_ignored_field_is_unknown(self):
        # json.loads accepts 1e999 as inf; only the allow_nan=False
        # re-serialization disqualifies it. Reverting that line must fail here.
        base = acceptance.encoded()
        shapes = (b',"ignored":X}', b',"ignored":{"nested":X}}',
                  b',"ignored":[{"nested":X}]}')
        for exponent in (b"1e999", b"-1e999", b"1E999"):
            for shape in shapes:
                with self.subTest(exponent=exponent, shape=shape):
                    self.unknown(base[:-1] + shape.replace(b"X", exponent))

    def test_wrapper_overflow_exponent_alerts_instead_of_false_recovery(self):
        base = acceptance.encoded()
        for exponent in (b"1e999", b"-1e999"):
            with self.subTest(exponent=exponent):
                payload = base[:-1] + b',"ignored":' + exponent + b"}"
                result, _ = self.delivery(payload, "unknown", None)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bounded_stdin_read_returns_unknown_without_eof(self):
        # The producer holds over-cap bytes on an open pipe: the classifier
        # must exit 0 with unknown before EOF, not block awaiting it.
        # Reverting the bounded read to read() hangs here until the timeout.
        payload = acceptance.encoded(ignored="x" * 5000)
        self.assertGreater(len(payload), 4097)
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, payload)
            command = [str(self.bin / "python3"), str(acceptance.CLASSIFIER)]
            proc = subprocess.Popen(
                command, stdin=read_fd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.root, env=dict(self.env),
                preexec_fn=lambda: acceptance.confine_child(str(self.root), self.allowed),
            )
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("classifier blocked awaiting EOF on an open over-cap pipe")
            # The producer end is still open, so no EOF was (or could be) seen.
            os.fstat(write_fd)
            self.assertEqual(proc.returncode, 0, stderr.decode(errors="replace"))
            self.assertEqual(stderr, b"")
            report = json.loads(stdout)
            self.assertEqual(report["status"], "unknown")
            self.assertEqual(report["reason"], "input_too_large")
            self.assertIsNone(report["bytes"])
            self.assertIs(report["gatus"]["success"], False)
        finally:
            os.close(write_fd)
            os.close(read_fd)


if __name__ == "__main__":
    unittest.main()
