#!/usr/bin/env python3
"""Immutable, offline acceptance contract for provider baseline integrity.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
    test_provider_baseline_integrity test_garm_provider_latency_baseline

These tests intentionally precede the implementation. They invoke the actual
entrypoint, never a copy of its validation/recomputation. Fixtures have literal,
hand-checked statistics. Only filesystem boundaries are fault-injected.

The new --json wire contract pinned here is deliberately small:
  schema = garm-provider-latency-outcome.v1; operation = init/record/check;
  baseline_schema and samples_schema name the existing v1 input schemas;
  source = {baseline: <input path>, samples: <input path or null>};
  validity/maturity/coverage each contain a nonempty reason string;
  code is a stable nonempty string, retryable is bool, next_action is nonempty;
  verdict retains ok/breach/unknown for checks (and their 0/2/3 exits).
Additional fields are allowed; the baseline storage format is not redesigned.

Breaks caught: forged maturity; accepting poisoned persisted numbers/metadata;
silent corruption reset; direct truncation/non-atomic publication; ignored
publication errors; altered percentile/window/threshold policy; JSON-only fake
success; and --dry-run/usage errors that write. No live collection is authorized.
"""
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "garm_provider_latency_baseline.py"
BASE_SCHEMA = "garm-provider-latency-baseline.v1"
SAMPLE_SCHEMA = "garm-provider-latency-samples.v1"
OUTCOME_SCHEMA = "garm-provider-latency-outcome.v1"
NOW = "2026-10-03T12:00:00Z"

# Run the real CLI in a fresh, credential-free interpreter. Audit hooks deny
# child commands/network and writes outside the fixture directory, including
# attempts via absolute paths. Unexpected operations are failures, not skips.
RUNNER = r'''
import json, os, pathlib, runpy, sys, sysconfig
script, root, baseline, fault, argv = sys.argv[1:]
root = pathlib.Path(root).resolve()
baseline = pathlib.Path(baseline).resolve()
repo = pathlib.Path(script).resolve().parent
stdlib = pathlib.Path(sysconfig.get_path("stdlib")).resolve()
argv = json.loads(argv)
sys.dont_write_bytecode = True
sys.path.insert(0, str(repo))

def inside(path, directory):
    return path == directory or directory in path.parents

def path_of(value):
    if isinstance(value, int):
        return None
    return pathlib.Path(os.fsdecode(value)).resolve()

def fence(message):
    os.write(2, ("OFFLINE_FENCE: " + message + "\n").encode())
    raise RuntimeError("OFFLINE_FENCE: " + message)

def audit(event, args):
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn",
                         "os.posix_spawn", "os.system", "ctypes.dlopen")):
        fence(event)
    if event == "open":
        path = path_of(args[0])
        if path is None:
            return
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+"))
        writing = writing or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT |
                                          os.O_TRUNC | os.O_APPEND))
        if writing and not inside(path, root):
            fence("write outside fixture")
        if not writing and not any(inside(path, p) for p in (root, repo, stdlib)):
            fence("read outside fixture/repository/stdlib")
        if path == baseline:
            if fault == "unreadable" and not writing:
                raise PermissionError("injected baseline read failure")
            if writing and fault in ("atomic", "publish_failure"):
                fence("direct baseline write instead of atomic publication")
    if event in ("os.rename", "os.remove", "os.rmdir", "os.mkdir",
                 "os.chmod", "os.link", "os.symlink", "os.truncate"):
        paths = args[:2] if event in ("os.rename", "os.link", "os.symlink") else args[:1]
        for value in paths:
            path = path_of(value)
            if path is not None and not inside(path, root):
                fence("filesystem mutation outside fixture")
        if (event in ("os.remove", "os.rmdir", "os.truncate")
                and path_of(args[0]) == baseline
                and fault in ("atomic", "publish_failure")):
            fence("baseline removed/truncated before atomic publication")
        if event == "os.rename" and path_of(args[1]) == baseline:
            if fault == "publish_failure":
                raise OSError("injected atomic publication failure")

sys.addaudithook(audit)
if fault == "close_failure":
    _real_close = os.close
    def _flaky_close(fd):
        raise OSError("injected post-rename directory close failure")
    os.close = _flaky_close
if fault == "dir_open_failure":
    _real_open = os.open
    def _flaky_open(path, *args, **kwargs):
        if path_of(path) == root:
            raise OSError("injected directory open failure")
        return _real_open(path, *args, **kwargs)
    os.open = _flaky_open
sys.argv = [script] + argv
runpy.run_path(script, run_name="__main__")
'''


def mature_baseline():
    """Literal valid v1 document, not generated by the code under test."""
    return {
        "schema": BASE_SCHEMA,
        "provisional": False,
        "operations": {
            "create": {"count": 10, "min_ms": 60000, "max_ms": 60000,
                       "p50_ms": 60000, "p95_ms": 60000,
                       "samples": [60000] * 10},
            "delete": {"count": 10, "min_ms": 10000, "max_ms": 10000,
                       "p50_ms": 10000, "p95_ms": 10000,
                       "samples": [10000] * 10},
        },
        "thresholds": {
            "create": {"p50_alert_ms": 600000, "p95_alert_ms": 1200000,
                       "rationale": "fixture create seed"},
            "delete": {"p50_alert_ms": 120000, "p95_alert_ms": 300000,
                       "rationale": "fixture delete seed"},
        },
        "provenance": {"recorded_at": NOW, "recorded_by": "fixture",
                       "source": "fixture seed", "total_samples": 20,
                       "refresh_cadence": "fixture cadence"},
    }


class ProviderBaselineIntegrityTest(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
            "PAPERCLIP_SCRATCH_DIR")
        self.temp = tempfile.TemporaryDirectory(prefix="provider-integrity-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = self.root / "baseline.json"
        self.samples = self.root / "samples.json"
        self.base = mature_baseline()
        self.write(self.baseline, self.base)
        self.write_samples()

    def write(self, path, doc):
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def write_samples(self, create=None, delete=None):
        create = [60000] * 10 if create is None else create
        delete = [10000] * 10 if delete is None else delete
        rows = [{"operation": op, "duration_ms": ms}
                for op, values in (("create", create), ("delete", delete))
                for ms in values]
        self.write(self.samples, {"schema": SAMPLE_SCHEMA, "samples": rows})

    def cli(self, operation, *extra, fault="", baseline=None):
        baseline = self.baseline if baseline is None else baseline
        argv = [operation, "--baseline", str(baseline)]
        if operation != "init":
            argv += ["--samples", str(self.samples)]
        if operation in ("init", "record"):
            argv += ["--source", "fixture update"]
        if operation == "record":
            argv += ["--recorded-by", "fixture", "--now", NOW]
        argv += list(extra)
        try:
            proc = subprocess.run(
                [sys.executable, "-B", "-c", RUNNER, str(SCRIPT),
                 str(self.root), str(baseline), fault, json.dumps(argv)],
                cwd=self.root, env={"PATH": str(self.root / "no-commands"),
                                    "HOME": str(self.root), "LC_ALL": "C",
                                    "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            self.fail("fixture CLI exceeded five-second offline bound")
        self.assertNotIn("OFFLINE_FENCE:", proc.stderr, proc.stderr)
        self.assertNotIn("Traceback (most recent call last)", proc.stderr, proc.stderr)
        return proc

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def outcome(self, proc, operation, baseline=None):
        def reject_constant(value):
            raise ValueError("non-finite JSON: " + value)
        try:
            doc = json.loads(proc.stdout, parse_constant=reject_constant)
        except (ValueError, TypeError) as exc:
            self.fail(f"--json must emit one finite JSON object: {exc}; "
                      f"exit={proc.returncode}, stdout={proc.stdout!r}, stderr={proc.stderr!r}")
        def assert_finite(value):
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), "non-finite number in JSON outcome")
            elif isinstance(value, dict):
                for item in value.values():
                    assert_finite(item)
            elif isinstance(value, list):
                for item in value:
                    assert_finite(item)
        assert_finite(doc)
        self.assertIsInstance(doc, dict)
        self.assertEqual(doc.get("schema"), OUTCOME_SCHEMA)
        self.assertEqual(doc.get("operation"), operation)
        self.assertEqual(doc.get("baseline_schema"), BASE_SCHEMA)
        self.assertEqual(doc.get("samples_schema"), SAMPLE_SCHEMA)
        source = doc.get("source")
        self.assertIsInstance(source, dict)
        self.assertEqual(source.get("baseline"), str(baseline or self.baseline))
        self.assertEqual(source.get("samples"), None if operation == "init" else str(self.samples))
        for key in ("validity", "maturity", "coverage"):
            self.assertIsInstance(doc.get(key), dict, key)
            self.assertIsInstance(doc[key].get("reason"), str, key)
            self.assertTrue(doc[key]["reason"].strip(), key)
        for key in ("code", "next_action"):
            self.assertIsInstance(doc.get(key), str, key)
            self.assertTrue(doc[key].strip(), key)
        self.assertIs(type(doc.get("retryable")), bool)
        return doc

    def assert_invalid_preserved(self, operation="record", fault=""):
        before = self.snapshot()
        proc = self.cli(operation, fault=fault)
        self.assertEqual(self.snapshot(), before, "existing evidence was changed or replaced")
        self.assertIn(proc.returncode, (1, 3), proc.stdout + proc.stderr)
        if operation == "check":
            # Corruption may be invalid (1) or unknown (3), never measured.
            self.assertNotIn('"verdict": "ok"', proc.stdout)
            self.assertNotIn('"verdict": "breach"', proc.stdout)
        return proc

    # Positive controls: preserving the existing behavior matters just as much
    # as refusing corruption. A blanket-refusal implementation fails these.
    def test_valid_mature_check_is_ok_and_read_only(self):
        before = self.snapshot()
        proc = self.cli("check")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "ok")
        self.assertEqual(self.snapshot(), before)

    def test_valid_tail_breach_retains_exit_two(self):
        self.write_samples(create=[60000] * 18 + [1500000] * 2)
        proc = self.cli("check")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "breach")

    def test_missing_fresh_operation_retains_unknown_exit_three(self):
        self.write_samples(delete=[])
        proc = self.cli("check")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "unknown")

    def test_absent_baseline_record_initializes_without_recalibration(self):
        self.baseline.unlink()
        proc = self.cli("record")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stored = json.loads(self.baseline.read_text())
        self.assertFalse(stored["provisional"])
        self.assertEqual(stored["operations"]["create"]["samples"], [60000] * 10)
        self.assertEqual(stored["thresholds"]["create"]["p50_alert_ms"], 600000)
        self.assertEqual(stored["thresholds"]["delete"]["p95_alert_ms"], 300000)

    def test_absent_baseline_check_is_unknown_and_does_not_create_it(self):
        self.baseline.unlink()
        before = self.snapshot()
        proc = self.cli("check")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_append_nearest_rank_and_thresholds_are_preserved(self):
        self.write_samples(create=[120000] * 10, delete=[20000] * 10)
        proc = self.cli("record")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stored = json.loads(self.baseline.read_text())
        self.assertEqual(stored["operations"]["create"], {
            "count": 20, "samples": [60000] * 10 + [120000] * 10,
            "min_ms": 60000, "max_ms": 120000, "p50_ms": 60000, "p95_ms": 120000})
        self.assertEqual(stored["thresholds"], self.base["thresholds"])
        self.assertEqual(stored["provenance"]["recorded_at"], NOW)
        self.assertEqual(stored["provenance"]["total_samples"], 40)

    def test_append_window_evicts_oldest_not_duplicates(self):
        self.write_samples(create=list(range(1, 2002)), delete=[])
        proc = self.cli("record")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        op = json.loads(self.baseline.read_text())["operations"]["create"]
        self.assertEqual(op["samples"], list(range(2, 2002)))
        self.assertEqual((op["count"], op["p50_ms"], op["p95_ms"]), (2000, 1001, 1901))

    def test_persisted_zero_and_six_hour_boundary_are_valid(self):
        self.base["operations"]["create"] = {
            "count": 10, "samples": [0] * 9 + [21600000],
            "min_ms": 0, "max_ms": 21600000, "p50_ms": 0, "p95_ms": 21600000}
        self.write(self.baseline, self.base)
        proc = self.cli("check")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_nine_per_operation_remains_unknown_even_with_false_flag(self):
        for op, value in (("create", 60000), ("delete", 10000)):
            self.base["operations"][op]["samples"] = [value] * 9
            self.base["operations"][op]["count"] = 9
        self.base["provenance"]["total_samples"] = 18
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved("check")

    def test_ten_create_but_empty_delete_cannot_be_measured(self):
        self.base["operations"]["delete"] = {
            "count": 0, "samples": [], "min_ms": None, "max_ms": None,
            "p50_ms": None, "p95_ms": None}
        self.base["provenance"]["total_samples"] = 10
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved("check")

    def test_empty_forged_nonprovisional_cannot_be_ok_or_breach(self):
        for op in ("create", "delete"):
            self.base["operations"][op] = {
                "count": 0, "samples": [], "min_ms": None, "max_ms": None,
                "p50_ms": None, "p95_ms": None}
        self.base["provenance"]["total_samples"] = 0
        self.write(self.baseline, self.base)
        for create in ([60000] * 10, [1500000] * 10):
            with self.subTest(fresh_create=create[0]):
                self.write_samples(create=create)
                self.assert_invalid_preserved("check")

    def test_valid_provisional_cannot_be_checked_as_measured(self):
        self.base["provisional"] = True
        self.write(self.baseline, self.base)
        proc = self.cli("check")
        self.assertIn(proc.returncode, (1, 3), proc.stdout + proc.stderr)

    def test_oversized_persisted_window_is_refused_not_silently_trimmed(self):
        self.base["operations"]["create"]["samples"] = [60000] * 2001
        self.base["operations"]["create"]["count"] = 2001
        self.base["provenance"]["total_samples"] = 2011
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved()

    def test_forged_maturity_record_is_not_silently_repaired(self):
        self.base["operations"]["delete"] = {
            "count": 0, "samples": [], "min_ms": None, "max_ms": None,
            "p50_ms": None, "p95_ms": None}
        self.base["provenance"]["total_samples"] = 10
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved()

    def test_inconsistent_provenance_total_is_refused_not_repaired(self):
        self.base["provenance"]["total_samples"] = 999
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved()

    def test_nonboolean_provisional_is_not_a_maturity_decision(self):
        self.base["provisional"] = 0
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved("check")

    def test_invalid_existing_dry_run_refuses_without_repair(self):
        self.baseline.write_bytes(b"{damaged evidence")
        before = self.snapshot()
        proc = self.cli("record", "--dry-run", "--json")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.outcome(proc, "record")

    def test_invalid_samples_dry_run_validates_without_creating_baseline(self):
        self.baseline.unlink()
        self.write_samples(create=[-1])
        before = self.snapshot()
        proc = self.cli("record", "--dry-run", "--json")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.outcome(proc, "record")

    def test_huge_integer_duration_is_structured_invalid_without_traceback(self):
        self.baseline.unlink()
        self.write_samples(create=[10**1000])
        before = self.snapshot()
        proc = self.cli("record", "--dry-run", "--json")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.outcome(proc, "record")

    def test_json_invalid_existing_never_echoes_nonfinite_values(self):
        self.base["operations"]["create"]["samples"] = [float("inf")] * 10
        self.write(self.baseline, self.base)
        before = self.snapshot()
        proc = self.cli("check", "--json")
        self.assertEqual(self.snapshot(), before)
        self.assertIn(proc.returncode, (1, 3), proc.stdout + proc.stderr)
        self.outcome(proc, "check")

    def test_huge_integer_threshold_is_finite_without_traceback(self):
        # Thresholds are finite-and-nonnegative with no upper bound, so a
        # huge int is valid input; before the isfinite guard it escaped as
        # an OverflowError traceback instead of a structured outcome.
        self.base["thresholds"]["create"]["p50_alert_ms"] = 10**1000
        self.write(self.baseline, self.base)
        before = self.snapshot()
        proc = self.cli("check", "--json")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.outcome(proc, "check")

    def test_json_forged_maturity_reports_unmeasured_with_reasons(self):
        self.base["operations"]["delete"] = {
            "count": 0, "samples": [], "min_ms": None, "max_ms": None,
            "p50_ms": None, "p95_ms": None}
        self.base["provenance"]["total_samples"] = 10
        self.write(self.baseline, self.base)
        before = self.snapshot()
        proc = self.cli("check", "--json")
        self.assertEqual(self.snapshot(), before)
        self.assertIn(proc.returncode, (1, 3), proc.stdout + proc.stderr)
        doc = self.outcome(proc, "check")
        self.assertEqual(doc.get("verdict"), "unknown")

    def test_existing_unreadable_record_is_not_reinitialized(self):
        self.assert_invalid_preserved(fault="unreadable")

    def test_existing_unreadable_check_is_not_measured(self):
        self.assert_invalid_preserved("check", fault="unreadable")

    def test_successful_update_publishes_atomically(self):
        self.write_samples(create=[120000] * 10)
        proc = self.cli("record", fault="atomic")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stored = json.loads(self.baseline.read_text())
        self.assertEqual(stored["operations"]["create"]["count"], 20)
        self.assertEqual(stored["thresholds"], self.base["thresholds"])

    def test_failed_publication_reports_failure_preserving_prior_valid_bytes(self):
        before = self.baseline.read_bytes()
        self.write_samples(create=[120000] * 10)
        proc = self.cli("record", fault="publish_failure")
        self.assertEqual(self.baseline.read_bytes(), before)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_json_init_record_check_share_versioned_contract(self):
        self.baseline.unlink()
        for operation in ("init", "record", "check"):
            with self.subTest(operation=operation):
                proc = self.cli(operation, "--json")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                doc = self.outcome(proc, operation)
                if operation == "check":
                    self.assertEqual(doc.get("verdict"), "ok")

    def test_json_breach_preserves_exit_and_verdict(self):
        self.write_samples(create=[1500000] * 10)
        proc = self.cli("check", "--json")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(self.outcome(proc, "check").get("verdict"), "breach")

    def test_json_missing_baseline_has_unknown_and_coded_remediation(self):
        self.baseline.unlink()
        proc = self.cli("check", "--json")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.outcome(proc, "check").get("verdict"), "unknown")
        self.assertFalse(self.baseline.exists())

    def test_json_invalid_samples_is_finite_coded_failure_without_writes(self):
        self.write_samples(create=[float("nan")])
        before = self.snapshot()
        proc = self.cli("record", "--json")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.outcome(proc, "record")

    def test_json_publication_failure_is_retryable_and_preserves_bytes(self):
        before = self.baseline.read_bytes()
        proc = self.cli("record", "--json", fault="publish_failure")
        self.assertEqual(self.baseline.read_bytes(), before)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        doc = self.outcome(proc, "record")
        self.assertTrue(doc["retryable"])

    def test_post_rename_close_failure_still_reports_success(self):
        self.write_samples(create=[120000] * 10)
        proc = self.cli("record", "--json", fault="close_failure")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        stored = json.loads(self.baseline.read_text())
        self.assertEqual(stored["operations"]["create"]["count"], 20)

    def test_directory_open_failure_fails_publication_without_writes(self):
        before = self.snapshot()
        proc = self.cli("record", "--json", fault="dir_open_failure")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        doc = self.outcome(proc, "record")
        self.assertEqual(doc["code"], "publish_failed")
        self.assertTrue(doc["retryable"])

    def test_cli_help_exposes_json_and_dry_run_without_writes(self):
        before = self.snapshot()
        for operation in ("init", "record", "check"):
            with self.subTest(operation=operation):
                proc = self.cli(operation, "--help")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("--json", proc.stdout)
                self.assertIn("--dry-run", proc.stdout)
                self.assertEqual(self.snapshot(), before)

    def test_unknown_flags_refuse_without_writes(self):
        for operation in ("init", "record", "check"):
            with self.subTest(operation=operation):
                before = self.snapshot()
                proc = self.cli(operation, "--not-a-provider-option")
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_conflicting_operation_flags_refuse_without_writes(self):
        before = self.snapshot()
        proc = self.cli("check", "--source", "must-not-be-used")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_json_unknown_flags_still_provide_coded_remediation(self):
        before = self.snapshot()
        proc = self.cli("record", "--json", "--not-a-provider-option")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.snapshot(), before)
        self.outcome(proc, "record")

    def test_provisional_record_reports_inadequate_coverage(self):
        self.baseline.unlink()
        self.write_samples(create=[60000] * 5, delete=[])
        for extra in (("--dry-run",), ()):
            with self.subTest(dry_run=bool(extra)):
                proc = self.cli("record", "--json", *extra)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                doc = self.outcome(proc, "record")
                self.assertFalse(doc["maturity"]["mature"])
                self.assertFalse(doc["coverage"]["adequate"])
                self.assertIn("10 per operation", doc["coverage"]["reason"])

    def test_mature_record_reports_adequate_coverage(self):
        proc = self.cli("record", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        doc = self.outcome(proc, "record")
        self.assertTrue(doc["maturity"]["mature"])
        self.assertTrue(doc["coverage"]["adequate"])

    def test_publish_failure_coverage_follows_threshold(self):
        proc = self.cli("record", "--json", fault="publish_failure")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        doc = self.outcome(proc, "record")
        self.assertEqual(doc["code"], "publish_failed")
        self.assertTrue(doc["coverage"]["adequate"])
        self.baseline.unlink()
        self.write_samples(create=[60000] * 5, delete=[])
        proc = self.cli("record", "--json", fault="publish_failure")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        doc = self.outcome(proc, "record")
        self.assertEqual(doc["code"], "publish_failed")
        self.assertFalse(doc["coverage"]["adequate"])

    def test_record_preserves_existing_baseline_mode(self):
        os.chmod(self.baseline, 0o640)
        proc = self.cli("record")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(os.stat(self.baseline).st_mode & 0o777, 0o640)

    def test_fresh_baseline_keeps_restrictive_default_mode(self):
        self.baseline.unlink()
        proc = self.cli("record")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(os.stat(self.baseline).st_mode & 0o777, 0o600)

    def test_explicit_argv_controls_json_usage_error(self):
        sys.path.insert(0, str(ROOT))
        try:
            from garm_provider_latency_baseline import main
        finally:
            sys.path.remove(str(ROOT))
        saved_argv, saved_stdout = sys.argv, sys.stdout
        try:
            sys.argv = ["garm_provider_latency_baseline.py"]
            capture = io.StringIO()
            sys.stdout = capture
            try:
                with self.assertRaises(SystemExit) as exited:
                    main(["record", "--json", "--baseline", str(self.baseline),
                          "--samples", str(self.samples), "--source", "x",
                          "--not-a-provider-option"])
            finally:
                sys.stdout = saved_stdout
            self.assertEqual(exited.exception.code, 1)
            doc = json.loads(capture.getvalue())
            self.assertEqual(doc["schema"], OUTCOME_SCHEMA)
            self.assertEqual(doc["code"], "usage_error")
        finally:
            sys.argv = saved_argv


# Each hostile persisted value must fail BOTH consumers independently. Testing
# only record would leave check's forged-clean path unprotected, and vice versa.
def poisoned_sample_case(value, operation):
    def test(self):
        # Keep counts/statistics coherent, so a metadata-only validator cannot
        # accidentally satisfy numeric-domain refusal.
        self.base["operations"]["create"] = {
            "count": 10, "samples": [value] * 10,
            "min_ms": value, "max_ms": value, "p50_ms": value, "p95_ms": value}
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved(operation)
    return test


for _name, _value in (("bool", True), ("nan", float("nan")),
                      ("positive_infinity", float("inf")),
                      ("negative_infinity", -float("inf")),
                      ("negative", -1), ("over_six_hours", 21600001)):
    for _operation in ("record", "check"):
        setattr(ProviderBaselineIntegrityTest,
                f"test_persisted_{_name}_refused_by_{_operation}",
                poisoned_sample_case(_value, _operation))


def inconsistent_metadata_case(field, value, operation):
    def test(self):
        self.base["operations"]["create"][field] = value
        self.write(self.baseline, self.base)
        self.assert_invalid_preserved(operation)
    return test


for _field, _value in (("count", 999), ("p50_ms", 1), ("p95_ms", 1),
                       ("min_ms", -1), ("max_ms", float("inf"))):
    for _operation in ("record", "check"):
        setattr(ProviderBaselineIntegrityTest,
                f"test_inconsistent_{_field}_refused_by_{_operation}",
                inconsistent_metadata_case(_field, _value, _operation))


def malformed_existing_case(payload, operation):
    def test(self):
        self.baseline.write_bytes(payload)
        self.assert_invalid_preserved(operation)
    return test


for _name, _payload in (("truncated", b'{"schema":'), ("empty", b""),
                        ("wrong_schema", b'{"schema":"unrelated.v1"}'),
                        ("non_utf8", b"\xff\xfe\x00damaged evidence")):
    for _operation in ("init", "record", "check"):
        setattr(ProviderBaselineIntegrityTest,
                f"test_existing_{_name}_preserved_by_{_operation}",
                malformed_existing_case(_payload, _operation))


def dry_run_case(operation, present):
    def test(self):
        if not present:
            self.baseline.unlink()
        before = self.snapshot()
        proc = self.cli(operation, "--dry-run", "--json")
        self.assertEqual(self.snapshot(), before)
        expected = 3 if operation == "check" and not present else 0
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        self.outcome(proc, operation)
    return test


for _operation in ("init", "record", "check"):
    # init of an existing baseline is deliberately not given overwrite semantics.
    for _present in ((False,) if _operation == "init" else (False, True)):
        setattr(ProviderBaselineIntegrityTest,
                f"test_dry_run_{_operation}_{'existing' if _present else 'absent'}_zero_writes",
                dry_run_case(_operation, _present))


if __name__ == "__main__":
    unittest.main()
