"""Immutable, synthetic acceptance contract for --report-json.

Run: python3 -B -m unittest -v test_bridge_row_inventory test_bridge_inventory_report_acceptance

Wire contract chosen here where the specification leaves spelling open:
* rows: existing inventory rows, in existing bridge order.
* counts: {evaluated, present, zero}, or null when no analysis completed.
* diagnostics: ordered objects with code; zero_coverage also has model_id.
* codes: zero_coverage, fixture_read_error, fixture_decode_error,
  fixture_schema_error, usage_error. Read failures are retryable; other
  outcomes are not. next_action is a nonempty string, not parsed prose.
* Report-mode input/usage failures go to stdout as one object (stderr empty).
  --help remains human help, exit 0. Conflicting output flags perform no I/O.

These tests intentionally remain RED until the paired implementation. They
never inspect implementation diffs, real rosters, credentials, or accounts.
The Builder must not edit this acceptance file. Documentation remains a
Builder deliverable; tests do not grep human prose as a substitute for behavior.
"""
import builtins
import copy
import inspect
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

import bridge_row_inventory as inventory


MODELS = (
    ("muse-spark-1.3-contributor", "T3", "cliproxy-meta"),
    ("claude-sonnet-5-5", "T2", "cliproxy-claude"),
    ("gpt-6.1-sol", "T1", "cliproxy-codex"),
)
SENTINEL = "SYNTHETIC_PRIVATE_INPUT_DO_NOT_ECHO"
CLI = Path(__file__).with_name("bridge_row_inventory.py")


def fixture():
    return {
        "models": [{"id": mid, "enabled": True, "laneId": lane}
                   for mid, _, lane in MODELS],
        "lanes": [{"lane": lane} for _, _, lane in MODELS],
    }


def expected_rows(zero=(), lane_override=None):
    # Hand-derived contract, not assemble_rows used as its own oracle.
    return [{
        "model": mid, "tier": tier, "expectedLane": lane,
        "enabledLaneBoundCount": 0 if mid in zero else 1,
        "lanes": [] if mid in zero else [lane_override or lane],
        "status": "ZERO" if mid in zero else "present",
    } for mid, tier, lane in MODELS]


class ReportHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.json"

    def save(self, document):
        self.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return self.path

    def invoke(self, *args, deny_reads=False, read_error=None):
        """Run real main; only fixture reads and stdout/stderr are allowed.

        Trap external boundaries rather than replacing the analyzer. Keeping
        the real open permits decoding, schema checks, and filesystem failures.
        PermissionError injection is needed because chmod is ineffective as root.
        """
        real_open = builtins.open
        real_io_open = io.open
        before = {p.name: p.read_bytes() for p in Path(self.tmp.name).iterdir()
                  if p.is_file()}
        violations = []

        def forbidden(*args, **kwargs):
            violations.append("external operation")
            raise AssertionError("report attempted an external operation")

        def guard(opener):
            def opened(file, mode="r", *args, **kwargs):
                if (deny_reads or isinstance(file, int)
                        or os.path.abspath(os.fspath(file)) != str(self.path)
                        or any(c in mode for c in "wax+")):
                    violations.append("non-fixture read or write")
                    raise AssertionError("report may only read its fixture")
                if read_error is not None:
                    raise read_error
                return opener(file, mode, *args, **kwargs)
            return opened

        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("builtins.open", guard(real_open)))
            stack.enter_context(patch("io.open", guard(real_io_open)))
            for target in ("os.open", "os.system", "os.posix_spawn", "os.fork",
                           "os.remove", "os.unlink", "os.rename", "os.replace",
                           "os.mkdir", "os.rmdir", "subprocess.Popen",
                           "socket.socket", "socket.getaddrinfo"):
                stack.enter_context(patch(target, forbidden))
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    code = inventory.main(list(args))
                except SystemExit as exc:
                    code = exc.code
        self.assertEqual(violations, [], "forbidden I/O must not be swallowed")
        after = {p.name: p.read_bytes() for p in Path(self.tmp.name).iterdir()
                 if p.is_file()}
        self.assertEqual(after, before, "report execution changed its inputs")
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self, *, code, outcome, complete, rows, diagnostics, retryable=False,
               args=None, **invoke_options):
        result = self.invoke(*(args if args is not None else
                               ["--report-json", "--fixture", str(self.path)]),
                             **invoke_options)
        actual_code, stdout, stderr = result
        self.assertEqual(actual_code, code, stderr)
        self.assertEqual(stderr, "", "machine mode must not require prose parsing")
        try:
            def reject_constant(value):
                raise ValueError("nonfinite JSON: " + value)
            report = json.loads(stdout, parse_constant=reject_constant)
            # Also refuses finite-looking JSON numbers that overflow to infinity.
            json.dumps(report, allow_nan=False)
        except (ValueError, TypeError) as exc:
            self.fail("expected exactly one finite JSON object: %s; stdout=%r" % (exc, stdout))
        self.assertIsInstance(report, dict)
        self.assertEqual(report["schema_id"], "bridge-inventory-report.v1")
        self.assertEqual(report["outcome"], outcome)
        self.assertIs(report["check_complete"], complete)
        self.assertEqual(report["rows"], rows)
        counts = None if not complete else {
            "evaluated": 3,
            "present": sum(row["enabledLaneBoundCount"] > 0 for row in rows),
            "zero": sum(row["enabledLaneBoundCount"] == 0 for row in rows),
        }
        self.assertEqual(report["counts"], counts)
        self.assertIsInstance(report["diagnostics"], list)
        self.assertEqual([d["code"] for d in report["diagnostics"]],
                         [d["code"] for d in diagnostics])
        for actual, wanted in zip(report["diagnostics"], diagnostics):
            for key, value in wanted.items():
                self.assertEqual(actual.get(key), value)
        self.assertIs(report["retryable"], retryable)
        self.assertIsInstance(report["next_action"], str)
        self.assertTrue(report["next_action"].strip())
        return report, result

    def unmeasurable(self, diagnostic, **kwargs):
        return self.report(code=2, outcome="unmeasurable", complete=False,
                           rows=[], diagnostics=[{"code": diagnostic}], **kwargs)


class CoverageReportTest(ReportHarness):
    def test_all_present_is_complete_not_a_legacy_array(self):
        self.save(fixture())
        self.report(code=0, outcome="complete", complete=True,
                    rows=expected_rows(), diagnostics=[])

    def test_each_zero_cause_retains_healthy_siblings_and_stable_id(self):
        for cause in ("missing", "disabled", "unlaned", "unknown_lane"):
            with self.subTest(cause=cause):
                doc = fixture()
                if cause == "missing":
                    del doc["models"][1]
                elif cause == "disabled":
                    doc["models"][1]["enabled"] = False
                elif cause == "unlaned":
                    del doc["models"][1]["laneId"]
                else:
                    doc["models"][1]["laneId"] = "synthetic-unknown"
                self.save(doc)
                self.report(code=2, outcome="findings", complete=True,
                            rows=expected_rows(zero=(MODELS[1][0],)),
                            diagnostics=[{"code": "zero_coverage", "model_id": MODELS[1][0]}])

    def test_multiple_zero_models_all_survive_one_result(self):
        doc = fixture()
        del doc["models"][0]
        doc["models"][0]["enabled"] = False
        del doc["models"][1]["laneId"]
        self.save(doc)
        self.report(code=2, outcome="findings", complete=True,
                    rows=expected_rows(zero=tuple(m[0] for m in MODELS)),
                    diagnostics=[{"code": "zero_coverage", "model_id": mid}
                                 for mid, _, _ in MODELS])

    def test_empty_valid_evidence_is_findings_not_unmeasurable(self):
        self.save({"models": [], "lanes": []})
        self.report(code=2, outcome="findings", complete=True,
                    rows=expected_rows(zero=tuple(m[0] for m in MODELS)),
                    diagnostics=[{"code": "zero_coverage", "model_id": mid}
                                 for mid, _, _ in MODELS])

    def test_order_is_deterministic_even_when_input_arrays_are_reversed(self):
        doc = fixture()
        doc["models"][0]["enabled"] = False
        self.save(doc)
        options = dict(code=2, outcome="findings", complete=True,
                       rows=expected_rows(zero=(MODELS[0][0],)),
                       diagnostics=[{"code": "zero_coverage", "model_id": MODELS[0][0]}])
        _, first = self.report(**options)
        _, repeated = self.report(**options)
        doc["models"].reverse()
        doc["lanes"].reverse()
        self.save(doc)
        _, permuted = self.report(**options)
        self.assertEqual(first, repeated)
        self.assertEqual(first, permuted)

    def test_legitimate_quoted_unicode_identifiers_are_safely_encoded(self):
        doc = fixture()
        lane = 'synthetic-"lane\\\n雪'
        doc["lanes"] = [{"lane": lane}]
        for model in doc["models"]:
            model["laneId"] = lane
        doc["models"].append({"id": 'other-"model\\\n雪', "enabled": True, "laneId": lane})
        self.save(doc)
        self.report(code=0, outcome="complete", complete=True,
                    rows=expected_rows(lane_override=lane), diagnostics=[])

    def test_report_reuses_existing_analysis_and_zero_gate(self):
        doc = fixture()
        doc["models"][1]["enabled"] = False
        self.save(doc)
        with patch.object(inventory, "assemble_rows", wraps=inventory.assemble_rows) as assemble:
            with patch.object(inventory, "require_nonzero", wraps=inventory.require_nonzero) as gate:
                self.report(code=2, outcome="findings", complete=True,
                            rows=expected_rows(zero=(MODELS[1][0],)),
                            diagnostics=[{"code": "zero_coverage", "model_id": MODELS[1][0]}])
        self.assertEqual(assemble.call_count, 1)
        self.assertEqual(gate.call_count, 1)
        self.assertEqual(gate.call_args.args[0], expected_rows(zero=(MODELS[1][0],)))

    def test_zero_diagnostics_do_not_parse_exception_prose(self):
        doc = fixture()
        doc["models"][2]["enabled"] = False
        self.save(doc)
        with patch.object(inventory, "require_nonzero",
                          side_effect=inventory.InventoryError(SENTINEL)):
            _, (_, stdout, stderr) = self.report(
                code=2, outcome="findings", complete=True,
                rows=expected_rows(zero=(MODELS[2][0],)),
                diagnostics=[{"code": "zero_coverage", "model_id": MODELS[2][0]}])
        self.assertNotIn(SENTINEL, stdout + stderr)


class InvalidEvidenceReportTest(ReportHarness):
    def test_malformed_json_has_no_rows_counts_or_input_leak(self):
        self.path.write_text('{"' + SENTINEL + '": ', encoding="utf-8")
        _, (_, stdout, stderr) = self.unmeasurable("fixture_decode_error")
        self.assertNotIn(SENTINEL, stdout + stderr)

    def test_invalid_utf8_is_a_structured_decode_error(self):
        self.path.write_bytes(SENTINEL.encode() + b"\xff")
        _, (_, stdout, stderr) = self.unmeasurable("fixture_decode_error")
        self.assertNotIn(SENTINEL, stdout + stderr)

    def test_missing_fixture_is_retryable_without_fabricated_coverage(self):
        self.unmeasurable("fixture_read_error", retryable=True)

    def test_directory_fixture_is_a_structured_read_error(self):
        self.path.mkdir()
        self.unmeasurable("fixture_read_error", retryable=True)

    def test_permission_error_is_safe_even_if_raw_exception_contains_input(self):
        self.save(fixture())
        _, (_, stdout, stderr) = self.unmeasurable(
            "fixture_read_error", retryable=True,
            read_error=PermissionError(SENTINEL))
        self.assertNotIn(SENTINEL, stdout + stderr)

    def test_invalid_model_lane_shapes_and_duplicate_identities_fail_closed(self):
        valid = fixture()
        cases = [
            [], None, {}, {"models": [], "lanes": None},
            {"models": {}, "lanes": []}, {"models": [], "lanes": [None]},
            {"models": [None], "lanes": []},
            {"models": [{"id": []}], "lanes": []},
            {"models": [{"id": ""}], "lanes": []},
            {"models": [], "lanes": [{"lane": ""}]},
            {"models": [], "lanes": [{"lane": 7}]},
            dict(valid, models=valid["models"] + [valid["models"][0]]),
            dict(valid, lanes=valid["lanes"] + [valid["lanes"][0]]),
            {"models": [{"id": SENTINEL}, {"id": SENTINEL}], "lanes": []},
            {"models": [], "lanes": [{"lane": SENTINEL}, {"lane": SENTINEL}]},
        ]
        for bad_enabled in (1, "true", None, SENTINEL):
            doc = copy.deepcopy(valid)
            doc["models"][0]["enabled"] = bad_enabled
            cases.append(doc)
        for index, doc in enumerate(cases):
            with self.subTest(case=index):
                self.save(doc)
                _, (_, stdout, stderr) = self.unmeasurable("fixture_schema_error")
                self.assertNotIn(SENTINEL, stdout + stderr)
                self.assertNotIn("Traceback", stdout + stderr)


class UsageAndSafetyTest(ReportHarness):
    def test_help_explains_report_flag_and_exits_without_analysis(self):
        code, stdout, stderr = self.invoke("--help", deny_reads=True)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("--report-json", stdout)
        self.assertIn("--json", stdout)
        self.assertIn("read-only", stdout.lower())

    def test_conflicting_modes_refuse_without_opening_or_analyzing_fixture(self):
        with patch.object(inventory, "assemble_rows", side_effect=AssertionError("analysis before usage validation")):
            for args in (["--report-json", "--json"], ["--json", "--report-json"]):
                with self.subTest(args=args):
                    self.unmeasurable("usage_error", args=args, deny_reads=True)

    def test_report_usage_errors_are_structured_without_fixture_io(self):
        for args in (["--report-json", "--fixture"],
                     ["--report-json", "--not-a-real-option"]):
            with self.subTest(args=args):
                self.unmeasurable("usage_error", args=args, deny_reads=True)

    def test_real_process_reports_findings_with_the_same_exit_and_json_contract(self):
        doc = fixture()
        doc["models"][0]["enabled"] = False
        self.save(doc)
        proc = subprocess.run([sys.executable, "-B", str(CLI), "--report-json",
                               "--fixture", str(self.path)],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr, "")
        try:
            report = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail("CLI did not emit JSON: %s" % exc)
        self.assertEqual(report["rows"], expected_rows(zero=(MODELS[0][0],)))
        self.assertEqual(report["outcome"], "findings")
        self.assertIs(report["check_complete"], True)


class LegacyPositiveControls(ReportHarness):
    def test_legacy_json_bytes_and_array_shape_are_unchanged(self):
        self.save(fixture())
        code, stdout, stderr = self.invoke("--json", "--fixture", str(self.path))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, json.dumps(expected_rows(), indent=2, sort_keys=True) + "\n")

    def test_legacy_table_bytes_are_unchanged(self):
        self.save(fixture())
        code, stdout, stderr = self.invoke("--fixture", str(self.path))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout,
            "Bridge row inventory (3 models, all present)\n"
            "model                        tier lane             count status\n"
            "claude-sonnet-5-5            T2   cliproxy-claude      1 present\n"
            "gpt-6.1-sol                  T1   cliproxy-codex       1 present\n"
            "muse-spark-1.3-contributor   T3   cliproxy-meta        1 present\n")

    def test_legacy_zero_refusal_bytes_and_exit_are_unchanged(self):
        doc = fixture()
        doc["models"][1]["enabled"] = False
        self.save(doc)
        for flags in ([], ["--json"]):
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke(*flags, "--fixture", str(self.path)),
                    (2, "", "bridge_row_inventory: refused: zero enabled lane-bound rows "
                     "for bridge model(s): claude-sonnet-5-5\n"))

    def test_legacy_malformed_json_remains_prose_and_exit_two(self):
        self.path.write_text("{", encoding="utf-8")
        code, stdout, stderr = self.invoke("--json", "--fixture", str(self.path))
        self.assertEqual((code, stdout), (2, ""))
        self.assertTrue(stderr.startswith("bridge_row_inventory: cannot read fixture: "))

    def test_legacy_schema_refusal_bytes_are_unchanged(self):
        self.save({"models": []})
        self.assertEqual(self.invoke("--json", "--fixture", str(self.path)),
                         (2, "", "bridge_row_inventory: refused: fixture has no lanes array\n"))

    def test_existing_helper_signatures_and_input_purity_are_preserved(self):
        self.assertEqual(str(inspect.signature(inventory.assemble_rows)), "(doc)")
        self.assertEqual(str(inspect.signature(inventory.require_nonzero)), "(rows)")
        doc = fixture()
        before = copy.deepcopy(doc)
        rows = inventory.assemble_rows(doc)
        self.assertEqual(rows, expected_rows())
        self.assertIs(inventory.require_nonzero(rows), rows)
        inventory.format_table(rows)
        self.assertEqual(doc, before)
        self.assertEqual(rows, expected_rows())


if __name__ == "__main__":
    unittest.main()
