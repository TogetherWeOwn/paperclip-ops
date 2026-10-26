#!/usr/bin/env python3
"""Immutable, test-first acceptance contract for the three countdown CLIs.

Run: python3 -B -m unittest -v test_countdown_report_acceptance
This suite is intentionally RED until --report-json is implemented. Do not
skip it, change its expectations, or change the legacy tests to make it green.
Fixtures below are synthetic, never account data. Only test code lives here.

Canonical countdown-report.v1 contract (same for claude, codex, and muse):
  --report-json replaces the table/legacy --json array with ONE finite JSON
  object on stdout, no stderr, exit 0 iff complete; otherwise exit 2.
  provider is lowercase; readout is <provider>_countdown. as_of is the actual
  evaluation UTC instant, not fixture observedAt; null only for invalid clocks
  or usage that cannot establish a clock. An explicit valid --now is retained.
  counts = {input, evaluated, invalid}: source records, records checked, and
  distinct invalid records (NOT number of diagnostics). All are null when
  parsing/usage prevents evaluation. Known empty input has counts 0/0/0.
  observations retain the five legacy row fields and input order. They contain
  only unambiguous valid rows. observations_partial is true iff incomplete.
  complete = nonempty, every lane valid, check_complete true, no diagnostics.
  incomplete = nonempty parsed lanes with any invalid record, check_complete
  false, even when there are no valid observations. Empty/unreadable/malformed
  input and usage failures are unmeasurable, never successful capacity.
  diagnostics have code, lane (string or null), source_index (zero-based or
  null for input/usage errors), retryable (boolean), and next_action (stable
  machine-readable action). A message is optional, must not echo raw values.
  Every independently invalid source record is identified. Duplicate names
  invalidate ALL members, regardless of validity/order; each member gets a
  duplicate_lane diagnostic. Extra field diagnostics are allowed, so invalid
  counts must not be computed by counting diagnostics. Missing/invalid identity
  is lane=null with the real source_index, never an invented lane name.
  Codes/actions/retryability are pinned in DIAGNOSTICS below. They describe
  repairing an offline fixture, never probing an account or requesting resets.
  disclaimer explicitly says this is NOT admission or readiness authorization.
  No aggregate headroom, inferred reset, substituted utilization or clock.

Complete example (whitespace/key order not significant, repeated bytes are):
  {"schema_id":"countdown-report.v1","provider":"claude",
   "readout":"claude_countdown","as_of":"2040-01-02T03:00:00Z",
   "outcome":"complete","check_complete":true,
   "counts":{"input":1,"evaluated":1,"invalid":0},
   "observations_partial":false,
   "observations":[{"lane":"sample","weeklyUtilization":0.5,
     "headroomPct":50.0,"hoursToReset":24.0,
     "resetsAt":"2040-01-03T03:00:00Z"}],"diagnostics":[],
   "disclaimer":"Diagnostic observations only; not admission or readiness authorization."}
Partial example: valid sample plus a null-utilization sibling -> incomplete,
  check_complete=false, counts=2/2/1, observations_partial=true, sample alone,
  diagnostic={code:invalid_utilization,lane:<sibling>,source_index:1,
              retryable:false,next_action:provide_finite_utilization}.
Unmeasurable example: invalid UTF-8 -> counts=null/null/null, no observations,
  observations_partial=false, fixture_encoding diagnostic with null lane/index.

Breaks caught: fail-fast reporting, first/last-wins duplicates, averaging,
shared-reset snapping, integer truncation, false completion, fabricated clocks
or capacity, swallowed input errors, raw-value disclosure, schema/code drift,
legacy partial output, and accidental network/process/write side effects.
The audit guard protects real CLI execution; it does not replace the readout.
"""

import copy
import datetime
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PROVIDERS = ("claude", "codex", "muse")
NOW = "2040-01-02T03:00:00Z"
UTC = datetime.timezone.utc
NOW_DT = datetime.datetime(2040, 1, 2, 3, tzinfo=UTC)
RESET_A = "2040-01-02T09:30:00.125Z"
RESET_B = "2040-01-05T03:00:00Z"
RESET_C = "2040-01-03T03:00:00Z"
CANARY = "RAW_INPUT_CANARY_DO_NOT_ECHO"
REPORT_KEYS = {
    "schema_id", "provider", "readout", "as_of", "outcome", "check_complete",
    "counts", "observations_partial", "observations", "diagnostics", "disclaimer",
}
ROW_KEYS = {"lane", "weeklyUtilization", "headroomPct", "hoursToReset", "resetsAt"}
DIAGNOSTIC_KEYS = {"code", "lane", "source_index", "retryable", "next_action"}
DIAGNOSTICS = {
    "invalid_lane_record": (False, "provide_lane_object"),
    "invalid_lane_identity": (False, "provide_lane_identity"),
    "duplicate_lane": (False, "disambiguate_lane_identity"),
    "invalid_utilization": (False, "provide_finite_utilization"),
    "invalid_reset": (False, "provide_valid_reset"),
    "expired_reset": (True, "provide_current_window"),
    "empty_input": (False, "provide_lane_records"),
    "invalid_now": (False, "provide_valid_utc_now"),
    "usage_error": (False, "correct_arguments"),
    "fixture_unreadable": (True, "provide_readable_fixture"),
    "fixture_encoding": (False, "provide_utf8_fixture"),
    "fixture_json": (False, "provide_valid_json"),
    "fixture_shape": (False, "provide_lane_list"),
}
_MISSING = object()

# Install BEFORE importing/running a CLI. Deny operations before they happen;
# pass only a synthetic environment, never inherited credentials. Read roots
# allow the checkout, this test's scratch, and the Python standard library.
_GUARD = r'''
import os, runpy, sys
roots = [os.path.realpath(p) for p in (sys.argv[1], sys.argv[2], sys.base_prefix)]
def allowed(path):
    if isinstance(path, int):
        return True
    path = os.path.realpath(os.fsdecode(path))
    return path == os.devnull or any(path == p or path.startswith(p + os.sep) for p in roots)
def offline_guard(event, args):
    denied = event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
    denied = denied or event in {
        "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "pty.spawn",
        "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.link",
        "os.symlink", "os.chmod", "os.chown", "os.truncate", "os.utime",
    }
    if event == "open":
        flags = args[2] or 0
        denied = denied or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        denied = denied or not allowed(args[0])
    if event in {"os.listdir", "os.scandir"}:
        denied = denied or not allowed(args[0] if args[0] is not None else os.curdir)
    if denied:
        raise RuntimeError("offline acceptance guard: " + event)
sys.addaudithook(offline_guard)
'''
_RUN_CLI = _GUARD + r'''
script = sys.argv[3]
sys.path.insert(0, sys.argv[1])
sys.argv = [script] + sys.argv[4:]
runpy.run_path(script, run_name="__main__")
'''


def lane(name="sample", utilization=0.125, reset=RESET_A):
    return {"lane": name, "weekly_utilization": utilization,
            "weekly_reset_utc": reset}


def valid_lanes(provider):
    return [lane(provider + "-a", 0.0, RESET_A),
            lane(provider + "-b", 0.125, RESET_B),
            lane(provider + "-c", 1.0, RESET_C)]


def reject_nonfinite(value):
    raise ValueError("non-finite JSON token: " + value)


def unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key: " + key)
        obj[key] = value
    return obj


class CountdownCLI(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="countdown-report-test-")
        self.addCleanup(self.scratch.cleanup)
        self.tmp = Path(self.scratch.name)
        self.env = {"PATH": os.defpath, "HOME": str(self.tmp / "empty-home"),
                    "TMPDIR": str(self.tmp), "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def fixture(self, doc):
        path = self.tmp / "fixture.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def run_cli(self, provider, path, *flags, now=NOW):
        args = [sys.executable, "-B", "-c", _RUN_CLI, str(HERE), str(self.tmp),
                str(HERE / (provider + "_countdown.py")), "--fixture", str(path)]
        if now is not None:
            args.extend(["--now", now])
        args.extend(flags)
        proc = subprocess.run(args, capture_output=True, text=True,
                              cwd=self.tmp, env=self.env, timeout=20)
        self.assertNotIn("offline acceptance guard:", proc.stderr,
                         "the real CLI attempted a forbidden side effect")
        return proc

    def report(self, provider, doc, *, now=NOW):
        return self.parse_report(
            provider, self.run_cli(provider, self.fixture(doc), "--report-json", now=now))

    def parse_report(self, provider, proc):
        self.assertTrue(proc.stdout.strip(),
                        "--report-json must emit one report, not argparse refusal: " + proc.stderr)
        try:
            report = json.loads(proc.stdout, parse_constant=reject_nonfinite,
                                object_pairs_hook=unique_keys)
            # Also catches legal exponent tokens such as 1e999 that overflow
            # the decoder's float, including inside optional diagnostic fields.
            json.dumps(report, allow_nan=False)
        except ValueError as exc:
            self.fail("stdout is not one finite, unambiguous JSON document: %s" % exc)
        self.assertIsInstance(report, dict, "report mode is not a legacy rows array")
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["schema_id"], "countdown-report.v1")
        self.assertEqual(report["provider"], provider)
        self.assertEqual(report["readout"], provider + "_countdown")
        self.assertIn(report["outcome"], ("complete", "incomplete", "unmeasurable"))
        self.assertIs(report["check_complete"], report["outcome"] == "complete")
        self.assertIs(report["observations_partial"], report["outcome"] == "incomplete")
        self.assertEqual(proc.returncode, 0 if report["check_complete"] else 2)
        self.assertEqual(proc.stderr, "", "report errors belong in safe JSON, not stderr")
        self.assertEqual(set(report["counts"]), {"input", "evaluated", "invalid"})
        for count in report["counts"].values():
            self.assertTrue(count is None or (type(count) is int and count >= 0))
        self.assertIsInstance(report["observations"], list)
        for row in report["observations"]:
            self.assertEqual(set(row), ROW_KEYS)
            self.assertIsInstance(row["lane"], str)
            self.assertTrue(row["lane"])
            for key in ("weeklyUtilization", "headroomPct", "hoursToReset"):
                self.assertIn(type(row[key]), (int, float))
                self.assertTrue(math.isfinite(row[key]))
            self.assertGreater(row["hoursToReset"], 0)
        self.assertIsInstance(report["diagnostics"], list)
        for diag in report["diagnostics"]:
            self.assertTrue(DIAGNOSTIC_KEYS <= set(diag))
            self.assertIn(diag["code"], DIAGNOSTICS)
            retryable, action = DIAGNOSTICS[diag["code"]]
            self.assertIs(diag["retryable"], retryable)
            self.assertEqual(diag["next_action"], action)
            self.assertTrue(diag["lane"] is None or isinstance(diag["lane"], str))
            index = diag["source_index"]
            self.assertTrue(index is None or (type(index) is int and index >= 0))
            self.assertNotIn(CANARY, json.dumps({k: v for k, v in diag.items()
                                               if k != "lane"}))
        self.assertIsInstance(report["disclaimer"], str)
        for word in ("not", "admission", "readiness"):
            self.assertIn(word, report["disclaimer"].lower())
        return report

    def assert_counts(self, report, input_count, evaluated, invalid):
        self.assertEqual(report["counts"],
                         {"input": input_count, "evaluated": evaluated, "invalid": invalid})

    def assert_diag(self, report, code, index=None, name=None):
        matches = [d for d in report["diagnostics"] if d["code"] == code
                   and d["source_index"] == index and d["lane"] == name]
        self.assertTrue(matches, "missing %s diagnostic for source %r, lane %r: %r"
                        % (code, index, name, report["diagnostics"]))

    def assert_unmeasurable(self, report, code, *, as_of=NOW):
        self.assertEqual(report["as_of"], as_of)
        self.assertEqual(report["outcome"], "unmeasurable")
        self.assert_counts(report, None, None, None)
        self.assertEqual(report["observations"], [])
        self.assert_diag(report, code)


class LegacyCompatibilityTests(CountdownCLI):
    """Positive controls must already pass on the unmodified baseline."""

    def test_json_arrays_keep_independent_hand_calculated_rows(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                rows = valid_lanes(provider)
                proc = self.run_cli(provider, self.fixture({"lanes": rows}), "--json")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                got = json.loads(proc.stdout)
                self.assertIsInstance(got, list)
                self.assertEqual([r["lane"] for r in got], [r["lane"] for r in rows])
                for row, used, headroom, hours, reset in zip(
                        got, (0.0, 0.125, 1.0), (100.0, 87.5, 0.0),
                        (6.500034722222222, 72.0, 24.0), (RESET_A, RESET_B, RESET_C)):
                    self.assertEqual(set(row), ROW_KEYS)
                    self.assertEqual(row["weeklyUtilization"], used)
                    self.assertEqual(row["headroomPct"], headroom)
                    self.assertAlmostEqual(row["hoursToReset"], hours)
                    self.assertEqual(row["resetsAt"], reset)

    def test_default_tables_keep_header_and_thinnest_first(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                proc = self.run_cli(provider, self.fixture({"lanes": valid_lanes(provider)}))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                lines = proc.stdout.splitlines()
                self.assertIn(provider.capitalize() + " weekly countdown", lines[0])
                self.assertIn("now " + NOW, lines[0])
                self.assertEqual(len(lines), 5)
                self.assertIn("headroom%", lines[1])
                self.assertEqual([s.split()[0] for s in lines[2:]],
                                 [provider + "-c", provider + "-b", provider + "-a"])
                self.assertEqual(proc.stderr, "")

    def test_cli_legacy_refusal_never_leaks_valid_siblings(self):
        for provider in PROVIDERS:
            for bad in (lane("bad", None), lane("bad", reset=NOW),
                        lane("good")):
                for flags in ((), ("--json",)):
                    with self.subTest(provider=provider, bad=bad, flags=flags):
                        proc = self.run_cli(provider, self.fixture(
                            {"lanes": [lane("good"), bad]}), *flags)
                        self.assertEqual(proc.returncode, 2)
                        self.assertEqual(proc.stdout, "")
                        self.assertTrue(proc.stderr)

    def test_assemble_rows_is_strict_and_does_not_mutate_inputs(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                module = importlib.import_module(provider + "_countdown")
                rows = valid_lanes(provider)
                before = copy.deepcopy(rows)
                got = module.assemble_rows(rows, NOW_DT)
                self.assertEqual(rows, before)
                self.assertEqual(len(got), 3)
                self.assertEqual([r["headroomPct"] for r in got], [100.0, 87.5, 0.0])
                for bad in (lane("bad", None), lane("bad", reset=NOW),
                            lane(provider + "-a")):
                    with self.assertRaises(module.CountdownError):
                        module.assemble_rows(rows + [bad], NOW_DT)
                self.assertEqual(module.assemble_rows([], NOW_DT), [])

    def test_legacy_bad_clock_and_missing_fixture_keep_exit_two_empty_stdout(self):
        for provider in PROVIDERS:
            for flags in ((), ("--json",)):
                with self.subTest(provider=provider, flags=flags):
                    path = self.fixture({"lanes": [lane()]})
                    for proc in (self.run_cli(provider, path, *flags, now="tomorrow"),
                                 self.run_cli(provider, self.tmp / "absent", *flags)):
                        self.assertEqual(proc.returncode, 2)
                        self.assertEqual(proc.stdout, "")
                        self.assertTrue(proc.stderr)


class ReportAcceptanceTests(CountdownCLI):
    def test_complete_report_keeps_real_math_and_per_lane_resets(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                rows = valid_lanes(provider)
                report = self.report(provider, {"observedAt": "2000-01-01T00:00:00Z",
                                                "lanes": rows})
                self.assertEqual(report["as_of"], NOW)
                self.assertEqual(report["outcome"], "complete")
                self.assert_counts(report, 3, 3, 0)
                self.assertEqual(report["diagnostics"], [])
                got = report["observations"]
                self.assertEqual([r["lane"] for r in got], [r["lane"] for r in rows])
                self.assertEqual([r["headroomPct"] for r in got], [100.0, 87.5, 0.0])
                self.assertEqual([r["weeklyUtilization"] for r in got], [0.0, 0.125, 1.0])
                self.assertEqual([r["resetsAt"] for r in got], [RESET_A, RESET_B, RESET_C])
                for row, hours in zip(got, (6.500034722222222, 72.0, 24.0)):
                    self.assertAlmostEqual(row["hoursToReset"], hours)

    def test_same_reset_does_not_aggregate_or_require_historical_cohort_size(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [lane("one", 0.1, RESET_C),
                                                         lane("two", 0.9, RESET_C)]})
                self.assertEqual(report["outcome"], "complete")
                self.assert_counts(report, 2, 2, 0)
                self.assertEqual([r["lane"] for r in report["observations"]], ["one", "two"])
                for row, headroom in zip(report["observations"], (90.0, 10.0)):
                    self.assertAlmostEqual(row["headroomPct"], headroom)
                    self.assertEqual(row["hoursToReset"], 24.0)
                    self.assertEqual(row["resetsAt"], RESET_C)

    def test_all_independently_bad_lanes_are_reported_with_valid_partial_sibling(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [lane("bad-util", None),
                    lane("good", 0.125, RESET_B), lane("bad-reset", reset=NOW)]})
                self.assertEqual(report["outcome"], "incomplete")
                self.assertEqual(report["as_of"], NOW)
                self.assert_counts(report, 3, 3, 2)
                self.assertEqual(report["observations"], [{"lane": "good",
                    "weeklyUtilization": 0.125, "headroomPct": 87.5,
                    "hoursToReset": 72.0, "resetsAt": RESET_B}])
                self.assert_diag(report, "invalid_utilization", 0, "bad-util")
                self.assert_diag(report, "expired_reset", 2, "bad-reset")

    def test_diagnostic_messages_do_not_echo_identity_outside_the_identity_field(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [lane(CANARY, None), lane("good")]})
                self.assert_diag(report, "invalid_utilization", 0, CANARY)
                safe_fields = copy.deepcopy(report)
                for diag in safe_fields["diagnostics"]:
                    diag.pop("lane")
                self.assertNotIn(CANARY, json.dumps(safe_fields))

    def test_every_bad_record_is_checked_even_when_none_are_valid(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [lane("one", False),
                                                         lane("two", reset="yesterday")]})
                self.assertEqual(report["outcome"], "incomplete")
                self.assert_counts(report, 2, 2, 2)
                self.assertEqual(report["observations"], [])
                self.assert_diag(report, "invalid_utilization", 0, "one")
                self.assert_diag(report, "invalid_reset", 1, "two")

    def test_duplicate_identity_invalidates_all_members_in_both_orders(self):
        for provider in PROVIDERS:
            for duplicate in (lane("duplicate", 0.75), lane("duplicate", None),
                              lane("duplicate", reset=NOW)):
                for reverse in (False, True):
                    with self.subTest(provider=provider, duplicate=duplicate, reverse=reverse):
                        pair = [lane("duplicate", 0.25), duplicate]
                        if reverse:
                            pair.reverse()
                        report = self.report(provider, {"lanes": pair + [lane("unique")]})
                        self.assertEqual(report["outcome"], "incomplete")
                        self.assert_counts(report, 3, 3, 2)
                        self.assertEqual([r["lane"] for r in report["observations"]], ["unique"])
                        self.assert_diag(report, "duplicate_lane", 0, "duplicate")
                        self.assert_diag(report, "duplicate_lane", 1, "duplicate")

    def test_missing_or_invalid_identity_uses_source_index_not_invented_name(self):
        for provider in PROVIDERS:
            for identity in (_MISSING, None, "", False, 7, [], {}):
                with self.subTest(provider=provider, identity=repr(identity)):
                    bad = lane()
                    bad.pop("lane")
                    if identity is not _MISSING:
                        bad["lane"] = identity
                    report = self.report(provider, {"lanes": [lane("good"), bad]})
                    self.assertEqual(report["outcome"], "incomplete")
                    self.assert_counts(report, 2, 2, 1)
                    self.assertEqual([r["lane"] for r in report["observations"]], ["good"])
                    self.assert_diag(report, "invalid_lane_identity", 1)

    def test_non_object_lane_records_are_independently_diagnosed(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [None, True, 3, [], CANARY, lane("good")]})
                self.assertEqual(report["outcome"], "incomplete")
                self.assert_counts(report, 6, 6, 5)
                self.assertEqual([r["lane"] for r in report["observations"]], ["good"])
                for index in range(5):
                    self.assert_diag(report, "invalid_lane_record", index)

    def test_missing_null_bool_nonfinite_out_of_range_utilization_never_becomes_capacity(self):
        bad_values = (_MISSING, None, True, False, "0.5", CANARY,
                      float("nan"), float("inf"), float("-inf"), -0.01, 1.01, 10**400)
        for provider in PROVIDERS:
            for value in bad_values:
                with self.subTest(provider=provider, value=repr(value)):
                    bad = lane("bad", value)
                    if value is _MISSING:
                        bad.pop("weekly_utilization")
                    report = self.report(provider, {"lanes": [bad, lane("good")]})
                    self.assertEqual(report["outcome"], "incomplete")
                    self.assert_counts(report, 2, 2, 1)
                    self.assertEqual([r["lane"] for r in report["observations"]], ["good"])
                    self.assert_diag(report, "invalid_utilization", 0, "bad")

    def test_missing_or_malformed_reset_is_not_replaced_with_a_shared_default(self):
        for provider in PROVIDERS:
            for value in (_MISSING, None, True, "", CANARY, "2040-01-03",
                          "2040-01-03T03:00:00", "2040-01-03T03:00:00+00:00"):
                with self.subTest(provider=provider, reset=repr(value)):
                    bad = lane("bad", reset=value)
                    if value is _MISSING:
                        bad.pop("weekly_reset_utc")
                    report = self.report(provider, {"lanes": [lane("good"), bad]})
                    self.assertEqual(report["outcome"], "incomplete")
                    self.assert_counts(report, 2, 2, 1)
                    self.assertEqual([r["lane"] for r in report["observations"]], ["good"])
                    self.assert_diag(report, "invalid_reset", 1, "bad")

    def test_past_and_exactly_now_resets_refuse_but_one_second_future_is_live(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [
                    lane("past", reset="2040-01-02T02:59:59Z"),
                    lane("exact", reset=NOW),
                    lane("live", reset="2040-01-02T03:00:01Z")]})
                self.assertEqual(report["outcome"], "incomplete")
                self.assert_counts(report, 3, 3, 2)
                self.assert_diag(report, "expired_reset", 0, "past")
                self.assert_diag(report, "expired_reset", 1, "exact")
                self.assertEqual([r["lane"] for r in report["observations"]], ["live"])
                self.assertAlmostEqual(report["observations"][0]["hoursToReset"],
                                       0.0002777777777777778)

    def test_empty_fixture_is_unmeasurable_with_known_zero_counts(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": []})
                self.assertEqual(report["outcome"], "unmeasurable")
                self.assertEqual(report["as_of"], NOW)
                self.assert_counts(report, 0, 0, 0)
                self.assertEqual(report["observations"], [])
                self.assert_diag(report, "empty_input")

    def test_invalid_top_level_shapes_do_not_invent_aggregate_counts(self):
        for provider in PROVIDERS:
            for doc in (None, True, [], 3, CANARY, {}, {"lanes": None},
                        {"lanes": {}}, {"lanes": True}, {"lanes": CANARY}):
                with self.subTest(provider=provider, doc=doc):
                    self.assert_unmeasurable(self.report(provider, doc), "fixture_shape")

    def test_bad_json_and_encoding_produce_safe_reports_with_unknown_counts(self):
        for provider in PROVIDERS:
            for payload, code in ((b'{"lanes": [', "fixture_json"),
                                  (b'{"' + CANARY.encode() + b'": }', "fixture_json"),
                                  (b'{"lanes": ["\xff"]}', "fixture_encoding"),
                                  (b"", "fixture_json")):
                with self.subTest(provider=provider, code=code, payload=payload):
                    path = self.tmp / "invalid-input.json"
                    path.write_bytes(payload)
                    proc = self.run_cli(provider, path, "--report-json")
                    self.assertNotIn(CANARY, proc.stdout + proc.stderr)
                    self.assert_unmeasurable(self.parse_report(provider, proc), code)

    def test_unreadable_fixture_errors_do_not_disclose_the_path(self):
        for provider in PROVIDERS:
            for path in (self.tmp / CANARY, self.tmp):
                with self.subTest(provider=provider, path=path.name):
                    proc = self.run_cli(provider, path, "--report-json")
                    self.assertNotIn(str(path), proc.stdout + proc.stderr)
                    self.assertNotIn(CANARY, proc.stdout + proc.stderr)
                    self.assert_unmeasurable(self.parse_report(provider, proc), "fixture_unreadable")

    def test_bad_now_has_no_fabricated_as_of_or_observations(self):
        for provider in PROVIDERS:
            for now in (CANARY, "", "2040-01-02", "2040-01-02T03:00:00",
                        "2040-01-02T03:00:00+00:00", "2040-02-30T03:00:00Z"):
                with self.subTest(provider=provider, now=now):
                    proc = self.run_cli(provider, self.fixture({"lanes": [lane()]}),
                                        "--report-json", now=now)
                    self.assertNotIn(CANARY, proc.stdout + proc.stderr)
                    self.assert_unmeasurable(self.parse_report(provider, proc),
                                             "invalid_now", as_of=None)

    def test_report_flag_conflicts_and_usage_boundaries_are_safe_json(self):
        combinations = (("--json", "--report-json"), ("--report-json", "--json"),
                        ("--report-json", "--fixture"), ("--report-json", "--now"),
                        ("--report-json", "--" + CANARY),
                        ("--report-json", CANARY), ("--report-json=" + CANARY,))
        for provider in PROVIDERS:
            for flags in combinations:
                with self.subTest(provider=provider, flags=flags):
                    proc = self.run_cli(provider, self.fixture({"lanes": [lane()]}), *flags)
                    self.assertNotIn(CANARY, proc.stdout + proc.stderr)
                    self.assert_unmeasurable(self.parse_report(provider, proc), "usage_error")

    def test_fractional_now_is_used_without_truncating_the_reset_delta(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [lane(reset=RESET_A)]},
                                     now="2040-01-02T03:00:00.125Z")
                self.assertEqual(report["outcome"], "complete")
                as_of = datetime.datetime.fromisoformat(report["as_of"].replace("Z", "+00:00"))
                self.assertEqual(as_of, NOW_DT.replace(microsecond=125000))
                self.assertEqual(as_of.utcoffset(), datetime.timedelta(0))
                self.assertEqual(report["observations"][0]["hoursToReset"], 6.5)

    def test_default_clock_is_real_utc_not_fixture_observed_at(self):
        # The only live-clock test: empty input needs no live quota/reset data.
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                before = datetime.datetime.now(UTC).replace(microsecond=0)
                report = self.report(provider, {"observedAt": "2000-01-01T00:00:00Z",
                                                "lanes": []}, now=None)
                after = datetime.datetime.now(UTC)
                self.assertEqual(report["outcome"], "unmeasurable")
                self.assertIsInstance(report["as_of"], str)
                self.assertTrue(report["as_of"].endswith("Z"))
                as_of = datetime.datetime.fromisoformat(report["as_of"].replace("Z", "+00:00"))
                self.assertLessEqual(before, as_of)
                self.assertLessEqual(as_of, after)

    def test_repeated_reports_are_byte_deterministic_and_leave_fixture_unchanged(self):
        for provider in PROVIDERS:
            for rows in (valid_lanes(provider), [lane("bad", None), lane("good")]):
                with self.subTest(provider=provider, rows=rows):
                    path = self.fixture({"lanes": rows})
                    before = path.read_bytes()
                    first = self.run_cli(provider, path, "--report-json")
                    second = self.run_cli(provider, path, "--report-json")
                    self.parse_report(provider, first)
                    self.parse_report(provider, second)
                    self.assertEqual(first.stdout, second.stdout)
                    self.assertEqual(first.returncode, second.returncode)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [path.name])

    def test_providers_share_schema_and_error_code_correspondence(self):
        signatures = []
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                report = self.report(provider, {"lanes": [lane("valid"),
                    lane("bad-util", None), lane("bad-reset", reset=NOW), {}, None]})
                self.assert_counts(report, 5, 5, 4)
                for code, index, name in (("invalid_utilization", 1, "bad-util"),
                                          ("expired_reset", 2, "bad-reset"),
                                          ("invalid_lane_identity", 3, None),
                                          ("invalid_lane_record", 4, None)):
                    self.assert_diag(report, code, index, name)
                signatures.append((set(report), set(report["counts"]),
                    [set(r) for r in report["observations"]],
                    sorted((d["code"], d["source_index"], d["retryable"], d["next_action"])
                           for d in report["diagnostics"]),
                    [set(d) for d in report["diagnostics"]]))
        self.assertEqual(len(signatures), 3)
        self.assertEqual(signatures[0], signatures[1])
        self.assertEqual(signatures[1], signatures[2])


class OfflineGuardControls(CountdownCLI):
    def test_guard_rejects_write_network_and_child_process_before_side_effects(self):
        for operation in ("open('forbidden-write', 'w')",
                          "__import__('socket').socket()",
                          "__import__('subprocess').run(['unavailable-command'])"):
            with self.subTest(operation=operation):
                proc = subprocess.run(
                    [sys.executable, "-B", "-c", _GUARD + "\n" + operation,
                     str(HERE), str(self.tmp)], capture_output=True, text=True,
                    cwd=self.tmp, env=self.env, timeout=20)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("offline acceptance guard:", proc.stderr)
                self.assertFalse((self.tmp / "forbidden-write").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
