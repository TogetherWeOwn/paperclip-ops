#!/usr/bin/env python3
"""Regression coverage added during the diagnostic-delta integration rebuild."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_diagnostic_delta import RepositoryFixture, SCRIPT, PRODUCER, BAD_SCRIPT

ROOT = Path(__file__).resolve().parent


def load_gate():
    spec = importlib.util.spec_from_file_location("delta_hardening", SCRIPT)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


class OutputLimits(unittest.TestCase):
    def test_producer_is_stopped_before_excess_stdout_or_stderr_is_collected(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "finished"
            producer = root / "producer.py"
            for channel in ("stdout", "stderr"):
                with self.subTest(channel=channel):
                    producer.write_text(
                        "import sys\nfrom pathlib import Path\n"
                        f"for _ in range(128):\n    sys.{channel}.buffer.write(b'x' * 65536)\n"
                        f"Path({str(marker)!r}).touch()\n")
                    with patch.object(gate, "PRODUCER", producer):
                        with self.assertRaises(gate.Unmeasurable) as error:
                            gate.scan(root, ["**"], {})
                    self.assertEqual(error.exception.code, "producer_output_limit")
                    self.assertFalse(marker.exists(), "Producer must be killed before draining its full output")

    def test_exact_report_and_stderr_byte_limits_are_accepted(self):
        gate = load_gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            producer = root / "producer.py"
            report = {"tool": "ax-lint", "mode": "warn", "root": "", "findings": [],
                      "summary": {"findings": 0, "mode": "warn"},
                      "coverage": {"skipped": [], "unknown": []}}
            report["root"] = "x" * (1024 * 1024 - len(json.dumps(report).encode()))
            raw = json.dumps(report)
            self.assertEqual(len(raw.encode()), 1024 * 1024)
            producer.write_text("import sys\n" + f"sys.stdout.write({raw!r})\n"
                                "sys.stderr.buffer.write(b'x' * 65536)\n")
            with patch.object(gate, "PRODUCER", producer):
                self.assertEqual(gate.scan(root, ["**"], {}), (set(), {"skipped": [], "unknown": []}))


class CollectorTimeouts(unittest.TestCase):
    def test_stalled_producer_is_reaped_with_open_or_closed_pipes(self):
        path = SCRIPT.with_name("capture_ax_report.py")
        spec = importlib.util.spec_from_file_location("ax_collector", path)
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "pid"
            for close_pipes in (False, True):
                with self.subTest(close_pipes=close_pipes):
                    script = ("import os, time\nfrom pathlib import Path\n"
                              f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                              + ("os.close(1); os.close(2)\n" if close_pipes else "")
                              + "time.sleep(10)\n")
                    start = time.monotonic()
                    with patch.object(collector, "TIMEOUT", 0.5):
                        self.assertEqual(collector.capture([sys.executable, "-I", "-c", script]), 124)
                    self.assertLess(time.monotonic() - start, 5)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), 0)


class SourceLimits(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def entry(size, index=1):
        return (b"100644", b"blob", f"{index:040x}".encode(), size)

    def test_single_file_at_limit_is_read_but_one_extra_byte_is_refused(self):
        gate = self.gate
        size = 1024 * 1024
        entry = self.entry(size)
        data = b"x" * size
        raw = entry[2] + b" blob " + str(size).encode() + b"\n" + data + b"\n"
        with patch.object(gate, "git", return_value=raw):
            self.assertEqual(gate.blobs(self.root, {"x": entry}), {"x": data})
        with patch.object(gate, "git") as git:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.blobs(self.root, {"x": self.entry(size + 1)})
            self.assertEqual(error.exception.code, "snapshot_limit")
            git.assert_not_called()

    def test_aggregate_bytes_at_limit_are_accepted_and_next_byte_is_refused(self):
        gate = self.gate
        tree = {f"scripts/{i}.sh": self.entry(1024 * 1024, i + 1) for i in range(32)}
        with patch.object(gate, "blobs", return_value={}) as blobs:
            self.assertEqual(gate.snapshot(self.root, tree, ["scripts/**"], self.root), {})
            blobs.assert_called_once()
        tree["scripts/extra.sh"] = self.entry(1, 33)
        with patch.object(gate, "blobs") as blobs:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.snapshot(self.root, tree, ["scripts/**"], self.root)
            self.assertEqual(error.exception.code, "snapshot_limit")
            blobs.assert_not_called()
        # Independently exercise the blob-allocation guard, not only metadata selection.
        tree.pop("scripts/extra.sh")
        raw = b"".join(e[2] + b" blob 1048576\n" + b"x" * 1048576 + b"\n" for e in tree.values())
        with patch.object(gate, "git", return_value=raw):
            self.assertEqual(sum(map(len, gate.blobs(self.root, tree).values())), 32 * 1024 * 1024)
        tree["scripts/extra.sh"] = self.entry(1, 33)
        with patch.object(gate, "git") as git:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.blobs(self.root, tree)
            self.assertEqual(error.exception.code, "snapshot_limit")
            git.assert_not_called()

    def test_selected_path_count_at_limit_and_one_over(self):
        gate = self.gate
        tree = {f"scripts/{i}.sh": self.entry(0) for i in range(10000)}
        with patch.object(gate, "blobs", return_value={}) as blobs:
            self.assertEqual(gate.snapshot(self.root, tree, ["scripts/**"], self.root), {})
            self.assertEqual(len(blobs.call_args.args[1]), 10000)
        tree["scripts/extra.sh"] = self.entry(0)
        with patch.object(gate, "blobs") as blobs:
            with self.assertRaises(gate.Unmeasurable) as error:
                gate.snapshot(self.root, tree, ["scripts/**"], self.root)
            self.assertEqual(error.exception.code, "snapshot_limit")
            blobs.assert_not_called()


class EnvironmentAndEvidence(RepositoryFixture):
    def test_hostile_git_environment_cannot_redirect_the_real_entry_point(self):
        self.put("scripts/new.sh", BAD_SCRIPT)
        head = self.commit()
        self.env.update(GIT_DIR=str(self.root / "missing.git"), GIT_WORK_TREE=str(self.root / "elsewhere"),
                        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.repositoryformatversion",
                        GIT_CONFIG_VALUE_0="999", GIT_OBJECT_DIRECTORY=str(self.root / "missing-objects"))
        report = self.check(1, head=head)
        self.assert_counts(report, 0, 1, 1, 0)

    def test_exact_executing_file_digests_on_clean_and_unmeasurable_results(self):
        for code, base in ((0, self.base), (2, "f" * 40)):
            with self.subTest(code=code):
                report = self.check(code, base=base)
                self.assertEqual(report["checker_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
                self.assertEqual(report["producer_sha256"], hashlib.sha256(PRODUCER.read_bytes()).hexdigest())
                collector = SCRIPT.with_name("capture_ax_report.py")
                self.assertEqual(report["collector_sha256"], hashlib.sha256(collector.read_bytes()).hexdigest())
                self.assertNotEqual(report["checker_sha256"], report["producer_sha256"])

    def test_unreadable_executing_file_digest_is_null_not_an_invented_hash(self):
        gate = load_gate()
        output = io.StringIO()
        missing = self.root / "missing-producer.py"
        with patch.object(gate, "PRODUCER", missing), redirect_stdout(output):
            self.assertEqual(gate.main(self.argv(self.base, self.base)), 2)
        report = json.loads(output.getvalue())
        self.assertIsNone(report["producer_sha256"])
        self.assertEqual(report["checker_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        with patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")):
            self.assertIsNone(gate.fingerprint(SCRIPT))


class IntegrationContract(unittest.TestCase):
    def test_ci_keeps_all_suites_and_uses_prepared_python_and_yaml(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        step, = [s for job in workflow["jobs"].values() for s in job.get("steps", [])
                 if s.get("name") == "quality-gate helper suites"]
        for suite in ("test_quality_gates", "test_floor_guard", "test_no_pull_request_target",
                      "test_docs_drift", "test_docs_explain_acceptance", "test_diagnostic_delta",
                      "test_diagnostic_delta_workflow", "test_diagnostic_delta_hardening"):
            self.assertIn(suite, step["run"].split())
        self.assertIn('"$STATIC_LABEL_PYTHON"', step["run"])
        self.assertIn("static-label-python.outputs.python-path", step["env"]["STATIC_LABEL_PYTHON"])
        self.assertEqual(step["env"]["PYTHONPATH"], "${{ runner.temp }}/static-label-guard-deps")

    def test_new_workflow_has_a_matching_hung_job_threshold(self):
        thresholds = json.loads((ROOT / "ops/case_e1e23b50/hung_job_thresholds.json").read_text())
        self.assertEqual(thresholds["jobs"].get("diagnostic-delta"), 5)


if __name__ == "__main__":
    unittest.main()
