#!/usr/bin/env python3
# ===========================================================================
# test_garm_ready_time_stored_integrity.py — acceptance tests: a stored
# ready-time baseline that is internally inconsistent must be REFUSED,
# never silently trusted, repaired or reset.
#
# Test-contract note (the builder implements against this; the companion
# suite test_garm_ready_time_baseline.py keeps pinning fresh-sample
# behaviour and must stay green unchanged):
#
# Stored baseline shape (schema "garm-ready-time-baseline.v1"):
#   {schema, provisional: bool, thresholds: {...},
#    operations: {startup: {count, min_ms, p50_ms, p95_ms, max_ms,
#                           samples: [ms, ...]}},
#    provenance: {source, refresh_cadence, recorded_at, recorded_by,
#                 total_samples}}
#
# Validity contract for the stored document:
#   - top level is an object with the documented baseline schema id;
#     operations and operations.startup are objects; samples is a list.
#   - every stored sample is a finite number (bool and string are NOT
#     numbers), 0 <= sample <= MAX_DURATION_MS (six hours); at most
#     MAX_SAMPLES (2,000) stored samples; duplicates are valid data.
#   - count is an int (not bool/float/string) equal to len(samples);
#     min_ms/p50_ms/p95_ms/max_ms equal the values recomputed from
#     samples with the existing nearest-rank algorithm (null exactly
#     when samples is empty); integer-spelled stats equal to the
#     recomputed float are valid (numeric equality).
#   - provisional is a bool equal to (count < MIN_SAMPLES): thin
#     history claiming maturity AND mature history claiming
#     provisional standing are both inconsistent.
#   - provenance is an object whose total_samples is an int (not
#     bool/float/string) equal to count.
#   - thresholds, fresh-sample validation and fresh-sample threshold
#     policy are out of scope: unchanged.
#
# Refusal contract:
#   - CLI `check` on an invalid baseline exits 3, prints one JSON
#     object with verdict "unknown", retains the existing verdict/key
#     shape (a "detail" or "reason" key), carries the additive machine
#     code "invalid_baseline" as a value somewhere in that object plus
#     a non-empty human reason/remediation string, prints no
#     traceback, and leaves the baseline file byte-identical.
#   - Genuinely empty/provisional VALID baselines still answer
#     unknown/exit 3 WITHOUT the invalid_baseline code; valid mature
#     baselines still answer ok/0 and breach/2 on fresh samples.
#   - Library `evaluate(invalid, fresh)` returns ("unknown", detail)
#     with the "invalid_baseline" code present in detail (mirroring
#     the existing provisional-unknown library shape). Existing
#     library signatures (evaluate, record, init_baseline,
#     parse_samples, percentile, summarize) are preserved.
#   - `record` applies the same validator BEFORE mutating: library
#     record(invalid, ...) raises the existing InvalidInput and leaves
#     the passed document unmutated; CLI record on an invalid baseline
#     file exits 1 (existing validation failure), prints no traceback
#     and leaves the file bytes intact. `check` never writes.
#   - No silent repair/reset: nothing above rewrites an invalid
#     baseline into a valid one.
#
# Validation commands:
#   python3 -B -m unittest -v test_garm_ready_time_stored_integrity
#   python3 -B -m unittest -v test_garm_ready_time_baseline
#
# Hermetic: subprocesses run with Git locator variables and BASH_ENV
# scrubbed, cwd inside a fresh temp dir, synthetic inputs only; no
# network, no real baseline, no writes outside temp dirs.
# ===========================================================================
import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from garm_ready_time_baseline import (
    BASELINE_SCHEMA,
    InvalidInput,
    MAX_DURATION_MS,
    MAX_SAMPLES,
    MIN_SAMPLES,
    evaluate,
    init_baseline,
    parse_samples,
    record,
    summarize,
)

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "garm_ready_time_baseline.py")
SAMPLES_SCHEMA = "garm-ready-time-samples.v1"
BASE = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone.utc)
CODE = "invalid_baseline"

SCRUB_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
             "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "GIT_COMMON_DIR", "GIT_NAMESPACE", "BASH_ENV", "ENV")


def zulu(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def pair_row(name, created_dt, duration_s):
    return {"instance": name,
            "created_at": zulu(created_dt),
            "ready_at": zulu(created_dt + timedelta(seconds=duration_s))}


def samples_doc(rows):
    return {"schema": SAMPLES_SCHEMA, "samples": rows}


def twelve_good_rows():
    return [pair_row("vm-%02d" % i, BASE + timedelta(minutes=i),
                     120 + i * 10)
            for i in range(12)]


def fresh_good():
    return parse_samples(samples_doc(twelve_good_rows()))


def stored_from_samples(samples):
    """A stored document the tool itself could have written."""
    base = init_baseline("test seed")
    startup = {"count": len(samples),
               "min_ms": None, "p50_ms": None, "p95_ms": None,
               "max_ms": None, "samples": list(samples)}
    if samples:
        startup.update(summarize(list(samples)))
        startup["samples"] = list(samples)
    base["operations"]["startup"] = startup
    base["provisional"] = len(samples) < MIN_SAMPLES
    base["provenance"].update(
        {"source": "test", "recorded_at": "2026-10-03T00:00:00Z",
         "recorded_by": "tester", "total_samples": len(samples)})
    return base


def mature_doc():
    return record(init_baseline("test seed"), fresh_good(),
                  "test", "tester", "2026-10-03T00:00:00Z")


def scrubbed_env():
    env = dict(os.environ)
    for key in SCRUB_ENV:
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_cli(cwd, *argv):
    return subprocess.run(
        [sys.executable, "-B", SCRIPT] + list(argv),
        capture_output=True, text=True, timeout=30,
        cwd=cwd, env=scrubbed_env())


def values_deep(obj):
    if isinstance(obj, dict):
        for key, val in obj.items():
            yield key
            yield from values_deep(val)
    elif isinstance(obj, list):
        for val in obj:
            yield from values_deep(val)
    else:
        yield obj


def has_code(obj):
    return any(val == CODE for val in values_deep(obj))


class StoredIntegrityCase(unittest.TestCase):
    """Shared fixture: fresh samples file + helpers in a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.samples_path = os.path.join(self.tmp, "samples.json")
        with open(self.samples_path, "w", encoding="utf-8") as handle:
            json.dump(samples_doc(twelve_good_rows()), handle)
        self.baseline_path = os.path.join(self.tmp, "baseline.json")

    def write_baseline(self, doc):
        with open(self.baseline_path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        with open(self.baseline_path, "rb") as handle:
            return handle.read()

    def check_cli(self, doc):
        before = self.write_baseline(doc)
        proc = run_cli(self.tmp, "check", "--samples", self.samples_path,
                       "--baseline", self.baseline_path)
        with open(self.baseline_path, "rb") as handle:
            self.assertEqual(handle.read(), before,
                             "check mutated the baseline file")
        return proc

    def assert_invalid_check(self, doc):
        proc = self.check_cli(doc)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verdict"], "unknown")
        self.assertTrue("detail" in payload or "reason" in payload,
                        "existing unknown key shape must be retained")
        self.assertTrue(has_code(payload),
                        "missing additive code %r in %r" % (CODE, payload))
        strings = [v for v in values_deep(payload)
                   if isinstance(v, str) and v not in (CODE, "unknown")]
        self.assertTrue(any(s.strip() for s in strings),
                        "expected a human reason/remediation string")
        return payload

    def assert_invalid_evaluate(self, doc):
        verdict, detail = evaluate(doc, fresh_good())
        self.assertEqual(verdict, "unknown")
        self.assertTrue(has_code(detail),
                        "missing code %r in detail %r" % (CODE, detail))

    def mutated(self, mutator, doc=None):
        doc = copy.deepcopy(mature_doc() if doc is None else doc)
        mutator(doc)
        return doc


class StoredShapeRefusalTest(StoredIntegrityCase):
    """Startup/operations/top-level objects of the wrong shape."""

    def test_startup_null(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["operations"].__setitem__("startup", None)))

    def test_startup_list(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["operations"].__setitem__("startup", [])))

    def test_startup_string(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["operations"].__setitem__("startup", "startup")))

    def test_startup_missing(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["operations"].pop("startup")))

    def test_operations_null(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.__setitem__("operations", None)))

    def test_operations_missing(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.pop("operations")))

    def test_top_level_list(self):
        self.assert_invalid_check([1, 2, 3])

    def test_top_level_null(self):
        self.assert_invalid_check(None)

    def test_wrong_baseline_schema(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.__setitem__("schema", SAMPLES_SCHEMA)))

    def test_schema_missing(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.pop("schema")))

    def test_samples_not_a_list(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["operations"]["startup"].__setitem__(
                "samples", "120000")))

    def test_samples_missing(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["operations"]["startup"].pop("samples")))

    def test_library_startup_null(self):
        self.assert_invalid_evaluate(self.mutated(
            lambda d: d["operations"].__setitem__("startup", None)))

    def test_library_startup_list(self):
        self.assert_invalid_evaluate(self.mutated(
            lambda d: d["operations"].__setitem__("startup", [])))


class StoredSampleRefusalTest(StoredIntegrityCase):
    """Stored history values: finite numbers inside the 6h bound."""

    def doc_with(self, samples):
        # Stats are whatever summarize would say only when the samples
        # are summable; hostile values keep the mature doc's stats, so
        # refusal must come from validating the samples themselves.
        return self.mutated(
            lambda d: d["operations"]["startup"].__setitem__(
                "samples", samples))

    def test_bool_sample(self):
        samples = [120000.0] * 11 + [True]
        self.assert_invalid_check(self.doc_with(samples))

    def test_string_sample(self):
        samples = [120000.0] * 11 + ["120000"]
        self.assert_invalid_check(self.doc_with(samples))

    def test_null_sample(self):
        samples = [120000.0] * 11 + [None]
        self.assert_invalid_check(self.doc_with(samples))

    def test_negative_sample(self):
        self.assert_invalid_check(stored_from_samples(
            [-1.0] + [120000.0] * 11))

    def test_over_six_hour_sample(self):
        self.assert_invalid_check(stored_from_samples(
            [float(MAX_DURATION_MS) + 1000.0] + [120000.0] * 11))

    def test_nan_sample(self):
        samples = [120000.0] * 11 + [math.nan]
        self.assert_invalid_check(self.doc_with(samples))

    def test_infinite_sample(self):
        samples = [120000.0] * 11 + [math.inf]
        self.assert_invalid_check(self.doc_with(samples))

    def test_oversized_history(self):
        self.assert_invalid_check(stored_from_samples(
            [120000.0] * (MAX_SAMPLES + 1)))

    def test_library_bool_sample(self):
        self.assert_invalid_evaluate(self.doc_with(
            [120000.0] * 11 + [False]))

    def test_library_negative_sample(self):
        self.assert_invalid_evaluate(stored_from_samples(
            [-5.0] + [120000.0] * 11))


class StoredStatsReconciliationTest(StoredIntegrityCase):
    """Serialized statistics must equal recomputation from history."""

    def startup(self, doc):
        return doc["operations"]["startup"]

    def test_empty_history_forged_count(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("samples", [])))

    def test_count_too_high(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("count", 13)))

    def test_count_too_low(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("count", 11)))

    def test_count_bool(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("count", True)))

    def test_count_string(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("count", "12")))

    def test_count_float(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("count", 12.0)))

    def test_tampered_min(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("min_ms", 1.0)))

    def test_tampered_p50(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("p50_ms", 999999.0)))

    def test_tampered_p95(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("p95_ms", 1.0)))

    def test_tampered_max(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("max_ms", 999999.0)))

    def test_stats_null_with_samples(self):
        def wipe(doc):
            for key in ("min_ms", "p50_ms", "p95_ms", "max_ms"):
                self.startup(doc)[key] = None
        self.assert_invalid_check(self.mutated(wipe))

    def test_stats_present_with_empty_history(self):
        def forge(doc):
            startup = self.startup(doc)
            startup["samples"] = []
            startup["count"] = 0
            doc["provisional"] = True
            doc["provenance"]["total_samples"] = 0
        self.assert_invalid_check(self.mutated(forge))

    def test_stat_bool(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("p50_ms", True)))

    def test_stat_string(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("p95_ms", "230000")))

    def test_stat_nonfinite(self):
        self.assert_invalid_check(self.mutated(
            lambda d: self.startup(d).__setitem__("max_ms", math.inf)))

    def test_provenance_total_mismatch(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["provenance"].__setitem__("total_samples", 999)))

    def test_provenance_total_bool(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["provenance"].__setitem__("total_samples", True)))

    def test_provenance_total_string(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["provenance"].__setitem__("total_samples", "12")))

    def test_provenance_total_missing(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d["provenance"].pop("total_samples")))

    def test_provenance_null(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.__setitem__("provenance", None)))

    def test_provenance_list(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.__setitem__("provenance", [])))

    def test_library_tampered_p95(self):
        self.assert_invalid_evaluate(self.mutated(
            lambda d: self.startup(d).__setitem__("p95_ms", 1.0)))

    def test_library_forged_total(self):
        self.assert_invalid_evaluate(self.mutated(
            lambda d: d["provenance"].__setitem__("total_samples", 0)))


class ProvisionalReconciliationTest(StoredIntegrityCase):
    """The provisional flag must agree with the stored depth."""

    def test_provisional_string(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.__setitem__("provisional", "false")))

    def test_provisional_int(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.__setitem__("provisional", 0)))

    def test_provisional_missing(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.pop("provisional")))

    def test_thin_history_claiming_maturity(self):
        doc = stored_from_samples([120000.0] * (MIN_SAMPLES - 1))
        doc["provisional"] = False
        self.assert_invalid_check(doc)

    def test_empty_history_claiming_maturity(self):
        doc = stored_from_samples([])
        doc["provisional"] = False
        self.assert_invalid_check(doc)

    def test_mature_history_claiming_provisional(self):
        self.assert_invalid_check(self.mutated(
            lambda d: d.__setitem__("provisional", True)))

    def test_library_thin_claiming_maturity(self):
        doc = stored_from_samples([120000.0] * 3)
        doc["provisional"] = False
        self.assert_invalid_evaluate(doc)


class ValidControlsTest(StoredIntegrityCase):
    """Genuine tool-produced documents keep their current verdicts."""

    def assert_unknown_without_code(self, doc):
        proc = self.check_cli(doc)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verdict"], "unknown")
        self.assertFalse(has_code(payload),
                         "valid provisional doc flagged: %r" % payload)

    def assert_ok_check(self, doc):
        proc = self.check_cli(doc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verdict"], "ok")
        self.assertFalse(has_code(payload))

    def test_empty_initialized_baseline(self):
        self.assert_unknown_without_code(init_baseline("test seed"))

    def test_thin_provisional_baseline(self):
        self.assert_unknown_without_code(
            stored_from_samples([120000.0] * (MIN_SAMPLES - 1)))

    def test_valid_mature_baseline(self):
        self.assert_ok_check(mature_doc())

    def test_exactly_min_samples_is_mature(self):
        self.assert_ok_check(stored_from_samples(
            [120000.0 + i * 1000.0 for i in range(MIN_SAMPLES)]))

    def test_duplicate_samples_are_valid(self):
        self.assert_ok_check(stored_from_samples([150000.0] * 12))

    def test_zero_duration_samples_are_valid(self):
        self.assert_ok_check(stored_from_samples([0.0] * 12))

    def test_exactly_six_hour_samples_are_valid(self):
        self.assert_ok_check(stored_from_samples(
            [float(MAX_DURATION_MS)] * 12))

    def test_unsorted_history_with_consistent_stats(self):
        samples = [230000.0, 120000.0, 170000.0, 200000.0, 130000.0,
                   220000.0, 140000.0, 210000.0, 150000.0, 160000.0,
                   180000.0, 190000.0]
        self.assert_ok_check(stored_from_samples(samples))

    def test_integer_spelled_stats_are_valid(self):
        doc = stored_from_samples([120000.0] * 12)
        startup = doc["operations"]["startup"]
        for key in ("min_ms", "p50_ms", "p95_ms", "max_ms"):
            startup[key] = int(startup[key])
        startup["samples"] = [120000] * 12
        self.assert_ok_check(doc)

    def test_exactly_max_samples_is_valid(self):
        self.assert_ok_check(stored_from_samples(
            [120000.0] * MAX_SAMPLES))

    def test_library_valid_mature_evaluates_ok(self):
        verdict, detail = evaluate(mature_doc(), fresh_good())
        self.assertEqual(verdict, "ok")
        self.assertEqual(detail["breaches"], [])

    def test_library_empty_evaluates_unknown_without_code(self):
        verdict, detail = evaluate(init_baseline("test seed"),
                                   fresh_good())
        self.assertEqual(verdict, "unknown")
        self.assertFalse(has_code(detail))


class FreshThresholdControlsTest(StoredIntegrityCase):
    """Fresh-sample threshold policy is unchanged by stored checks."""

    def write_samples(self, rows, name):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(samples_doc(rows), handle)
        return path

    def test_breach_still_exit_2_on_valid_mature(self):
        self.write_baseline(mature_doc())
        slow = self.write_samples(
            [pair_row("vm-%02d" % i, BASE, 700) for i in range(12)],
            "slow.json")
        proc = run_cli(self.tmp, "check", "--samples", slow,
                       "--baseline", self.baseline_path)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "breach")

    def test_bad_fresh_samples_still_exit_1(self):
        self.write_baseline(mature_doc())
        empty = self.write_samples([], "empty.json")
        proc = run_cli(self.tmp, "check", "--samples", empty,
                       "--baseline", self.baseline_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class RecordRefusalTest(StoredIntegrityCase):
    """record validates stored evidence before any mutation."""

    RECORD_ARGS = ("--source", "test", "--recorded-by", "tester",
                   "--now", "2026-10-03T00:00:00Z")

    def assert_record_refused_cli(self, doc):
        before = self.write_baseline(doc)
        proc = run_cli(self.tmp, "record", "--samples",
                       self.samples_path, "--baseline",
                       self.baseline_path, *self.RECORD_ARGS)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        with open(self.baseline_path, "rb") as handle:
            self.assertEqual(handle.read(), before,
                             "refused record changed baseline bytes")

    def test_record_refuses_forged_count_file(self):
        self.assert_record_refused_cli(self.mutated(
            lambda d: d["operations"]["startup"].__setitem__(
                "samples", [])))

    def test_record_refuses_startup_null_file(self):
        self.assert_record_refused_cli(self.mutated(
            lambda d: d["operations"].__setitem__("startup", None)))

    def test_record_refuses_tampered_stats_file(self):
        self.assert_record_refused_cli(self.mutated(
            lambda d: d["operations"]["startup"].__setitem__(
                "p50_ms", 1.0)))

    def test_record_refuses_bool_sample_file(self):
        self.assert_record_refused_cli(self.mutated(
            lambda d: d["operations"]["startup"].__setitem__(
                "samples", [120000.0] * 11 + [True])))

    def test_library_record_raises_and_does_not_mutate(self):
        doc = self.mutated(
            lambda d: d["operations"]["startup"].__setitem__(
                "p95_ms", 1.0))
        before = copy.deepcopy(doc)
        with self.assertRaises(InvalidInput):
            record(doc, fresh_good(), "test", "tester",
                   "2026-10-03T00:00:00Z")
        self.assertEqual(doc, before)

    def test_library_record_raises_on_startup_list(self):
        doc = self.mutated(
            lambda d: d["operations"].__setitem__("startup", []))
        before = copy.deepcopy(doc)
        with self.assertRaises(InvalidInput):
            record(doc, fresh_good(), "test", "tester",
                   "2026-10-03T00:00:00Z")
        self.assertEqual(doc, before)

    def test_record_still_works_on_valid_baseline(self):
        before = self.write_baseline(mature_doc())
        proc = run_cli(self.tmp, "record", "--samples",
                       self.samples_path, "--baseline",
                       self.baseline_path, *self.RECORD_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(self.baseline_path, "rb") as handle:
            self.assertNotEqual(handle.read(), before)
        with open(self.baseline_path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["operations"]["startup"]["count"], 24)
        self.assertEqual(stored["provenance"]["total_samples"], 24)

    def test_record_still_works_on_empty_initialized(self):
        self.write_baseline(init_baseline("test seed"))
        proc = run_cli(self.tmp, "record", "--samples",
                       self.samples_path, "--baseline",
                       self.baseline_path, *self.RECORD_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class SchemaConstantsTest(unittest.TestCase):
    """Documented schema ids and bounds reused, not reinvented."""

    def test_baseline_schema_id(self):
        self.assertEqual(BASELINE_SCHEMA, "garm-ready-time-baseline.v1")
        self.assertEqual(init_baseline("s")["schema"], BASELINE_SCHEMA)

    def test_bounds(self):
        self.assertEqual(MAX_DURATION_MS, 6 * 60 * 60 * 1000)
        self.assertEqual(MAX_SAMPLES, 2000)
        self.assertEqual(MIN_SAMPLES, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
