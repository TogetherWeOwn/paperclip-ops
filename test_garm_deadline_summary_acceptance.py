"""Acceptance contract: completed-sample deadline exceedances.

Builder contract under test:

* Add an opt-in ``--deadline-summary`` flag to the existing ``check``
  subcommand only. The default ``check`` JSON output, its verdicts and
  its exit codes (0 ok / 2 breach / 3 unknown, 1 for usage/validation)
  are unchanged, and the ``evaluate``/``parse_samples``/``record`` /
  ``percentile``/``summarize`` helper contracts and signatures are
  unchanged. ``init`` and ``record`` behaviour is unchanged and neither
  of them (nor the top-level parser) accepts the new flag.
* With the flag, every JSON verdict emitted by ``check`` (ok, breach and
  unknown alike, including the unreadable-baseline unknown) gains
  exactly one extra top-level key, ``deadline_summary``, and no other
  key changes: removing ``deadline_summary`` from the opted-in object
  must leave an object equal to the default run on the same inputs,
  with the same exit code. Without the flag there is no
  ``deadline_summary`` key.
* The summary object has exactly these keys::

      {
        "schema": "garm-ready-deadline.v1",
        "evaluation_state": "evaluated" | "not_evaluated",
        "ready_timeout_ms": <finite number> | null,
        "evaluated_sample_count": <int>,
        "exceedance_count": <int> | null,
        "max_excess_ms": <finite number> | null
      }

  Counts are JSON integers (never booleans or floats); durations are
  finite JSON numbers (no NaN/Infinity tokens). The summary carries
  counts and durations only: no instance names, no input bodies, no
  provenance or other private metadata.
* ``evaluated`` applies exactly when ``check`` reaches a verdict on
  the fresh samples (verdict ok or breach: a mature, non-provisional
  baseline and at least one fresh sample). Then:
  ``ready_timeout_ms`` is the supplied baseline's
  ``thresholds.ready_timeout_ms`` (never a built-in constant and never
  a stored-history percentile), ``evaluated_sample_count`` is the
  number of fresh samples (duplicates are separate observations),
  ``exceedance_count`` counts fresh durations STRICTLY greater than
  ``ready_timeout_ms`` (a sample exactly at the deadline is not an
  exceedance), and ``max_excess_ms`` is the largest
  ``duration - ready_timeout_ms`` among exceeding samples, or 0 when
  none exceed. Only the fresh samples are measured; the baseline's
  stored history never contributes.
* ``not_evaluated`` applies to every unknown verdict (provisional
  baseline, missing baseline, unreadable baseline): check refused to
  verdict, so it must not imply measurements. Then
  ``evaluated_sample_count`` is 0 (nothing was evaluated, however many
  fresh rows were parsed), ``exceedance_count`` and ``max_excess_ms``
  are null (unavailable, never 0), and ``ready_timeout_ms`` is the
  supplied value when a readable, shape-valid baseline was loaded,
  else null.
* The summary is diagnostic only: it never changes the p50/p95 breach
  logic, threshold values, baseline maturity/validation, verdicts or
  exit codes, and it is not a gate. A percentile-ok report can
  legitimately contain deadline exceedances; a breach report can
  contain none against a raised supplied deadline. Completed-sample
  diagnostics neither detect unfinished instances nor prove live
  health nor authorize rollout: ``docs/garm-ready-time-baseline.md``
  and ``check --help`` must say so (the document names the flag, the
  ``garm-ready-deadline.v1`` schema, ``ready_timeout_ms``, the
  strictly-greater exceedance rule, ``not_evaluated``, and the
  completed-only limits using the words unfinished, live health and
  rollout).
* Malformed-input behaviour is preserved, not redefined: inputs the
  existing parser refuses before any JSON verdict exists (for example
  an empty fresh-sample list or more than 2000 fresh rows) keep
  exit 1 with the existing text diagnostic and no JSON verdict, with
  or without the flag. No new stored-baseline validator is part of
  this contract; fixtures here use only currently valid inputs.
* ``check`` stays source-only, read-only and idempotent: it reads
  only the two files it is given, writes nothing, contacts nothing,
  and repeated runs on unchanged inputs produce identical output.
  The implementation module must not gain network or subprocess
  imports.

Fixtures are synthetic and hermetic. Mature baselines are built
through the existing public init/record helpers so stored evidence is
internally consistent under the current contract; every behaviour
assertion is made through the real CLI in a subprocess. No network,
host, credential or live collection is involved.

Control classes (``ExistingBehaviorControls``) describe behaviour
that already holds before the feature exists and must still hold
after it: they pass on the unmodified source. The remaining classes
describe the new feature and are expected to fail on the unmodified
source because ``--deadline-summary`` does not exist yet.

IMMUTABLE: Builder starts from the pushed test branch/commit and
never edits this file. Corrections belong to the Test Author.

Run: python3 -B -m unittest -v test_garm_ready_time_baseline \
        test_garm_deadline_summary_acceptance
Offline, stdlib only, no network or credentials.
"""

import ast
import inspect
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from garm_ready_time_baseline import (  # noqa: E402
    evaluate,
    init_baseline,
    parse_samples,
    percentile,
    record,
    summarize,
)

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "garm_ready_time_baseline.py"
DOCS = ROOT / "docs" / "garm-ready-time-baseline.md"

SAMPLES_SCHEMA = "garm-ready-time-samples.v1"
SUMMARY_SCHEMA = "garm-ready-deadline.v1"
SUMMARY_KEYS = {
    "schema",
    "evaluation_state",
    "ready_timeout_ms",
    "evaluated_sample_count",
    "exceedance_count",
    "max_excess_ms",
}
DEFAULT_READY_TIMEOUT_MS = 600000  # seed per-VM deadline: 10 min
MAX_FRESH_SAMPLES = 2000           # existing fresh-sample limit
BASE = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone.utc)

INSTANCE_MARKER = "SYNTHETIC-INSTANCE-MARKER-91873"
SOURCE_MARKER = "SYNTHETIC-BASELINE-MARKER-64127"


def zulu(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_for(durations_s, prefix="synthetic-vm"):
    return [
        {
            "instance": "%s-%03d" % (prefix, i),
            "created_at": zulu(BASE),
            "ready_at": zulu(BASE + timedelta(seconds=seconds)),
        }
        for i, seconds in enumerate(durations_s)
    ]


def samples_doc(durations_s, prefix="synthetic-vm"):
    return {"schema": SAMPLES_SCHEMA,
            "samples": rows_for(durations_s, prefix)}


def mature_baseline(stored_s=None, ready_timeout_ms=None,
                    source="synthetic fixture"):
    """A currently valid, non-provisional baseline (>= 10 stored rows)."""
    if stored_s is None:
        stored_s = [120] * 12
    base = init_baseline(source)
    base = record(base, parse_samples(samples_doc(stored_s)),
                  source, "tester", "2026-10-03T00:00:00Z")
    if base["provisional"]:
        raise AssertionError("fixture baseline unexpectedly provisional")
    if ready_timeout_ms is not None:
        base["thresholds"]["ready_timeout_ms"] = ready_timeout_ms
    return base


def _reject_json_constant(value):
    raise ValueError("non-finite JSON constant: %s" % value)


class DeadlineSummaryBase(unittest.TestCase):
    """Shared hermetic fixture and strict subprocess helpers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            prefix="deadline-summary-acceptance-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.samples_path = self.root / "samples.json"
        self.baseline_path = self.root / "baseline.json"

    # -- fixture writers ----------------------------------------------
    def write_samples(self, durations_s, prefix="synthetic-vm",
                      path=None):
        path = path or self.samples_path
        path.write_text(json.dumps(samples_doc(durations_s, prefix)),
                        encoding="utf-8")
        return path

    def write_samples_doc(self, doc, path=None):
        path = path or self.samples_path
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def write_baseline(self, base, path=None):
        path = path or self.baseline_path
        path.write_text(json.dumps(base), encoding="utf-8")
        return path

    # -- subprocess ----------------------------------------------------
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT)] + list(argv),
            capture_output=True, text=True, timeout=30)

    def run_check(self, samples=None, baseline=None, optin=False):
        argv = ["check",
                "--samples", str(samples or self.samples_path),
                "--baseline", str(baseline or self.baseline_path)]
        if optin:
            argv.append("--deadline-summary")
        return self.run_cli(*argv)

    def stdout_json(self, proc, label):
        try:
            return json.loads(proc.stdout,
                              parse_constant=_reject_json_constant)
        except ValueError as exc:
            self.fail(
                "%s did not emit one strict finite JSON object: %s "
                "(exit=%s stdout=%r stderr=%r)"
                % (label, exc, proc.returncode, proc.stdout, proc.stderr))

    def run_both(self, samples=None, baseline=None):
        """Run default + opted-in; opting in may only add the summary."""
        default = self.run_check(samples, baseline, optin=False)
        optin = self.run_check(samples, baseline, optin=True)
        self.assertEqual(
            optin.returncode, default.returncode,
            "opted-in exit %s != default exit %s (stderr=%r)"
            % (optin.returncode, default.returncode, optin.stderr))
        default_json = self.stdout_json(default, "default check")
        optin_json = self.stdout_json(optin, "opted-in check")
        self.assertNotIn("deadline_summary", default_json)
        self.assertIn("deadline_summary", optin_json)
        stripped = {k: v for k, v in optin_json.items()
                    if k != "deadline_summary"}
        self.assertEqual(stripped, default_json,
                         "opting in changed verdict/detail output")
        return optin_json, optin

    # -- summary shape -------------------------------------------------
    def _finite_number(self, value, field):
        self.assertIn(type(value), (int, float),
                      "%s must be a JSON number, got %r" % (field, value))
        self.assertTrue(math.isfinite(value),
                        "%s must be finite, got %r" % (field, value))

    def assert_evaluated(self, summary, ready_timeout_ms,
                         evaluated_count, exceedance_count, max_excess_ms):
        self.assertIsInstance(summary, dict)
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertEqual(summary["schema"], SUMMARY_SCHEMA)
        self.assertEqual(summary["evaluation_state"], "evaluated")
        self._finite_number(summary["ready_timeout_ms"],
                            "ready_timeout_ms")
        self.assertEqual(summary["ready_timeout_ms"], ready_timeout_ms)
        self.assertIs(type(summary["evaluated_sample_count"]), int)
        self.assertEqual(summary["evaluated_sample_count"],
                         evaluated_count)
        self.assertIs(type(summary["exceedance_count"]), int)
        self.assertEqual(summary["exceedance_count"], exceedance_count)
        self._finite_number(summary["max_excess_ms"], "max_excess_ms")
        self.assertEqual(summary["max_excess_ms"], max_excess_ms)

    def assert_not_evaluated(self, summary, ready_timeout_ms):
        self.assertIsInstance(summary, dict)
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertEqual(summary["schema"], SUMMARY_SCHEMA)
        self.assertEqual(summary["evaluation_state"], "not_evaluated")
        if ready_timeout_ms is None:
            self.assertIsNone(summary["ready_timeout_ms"])
        else:
            self._finite_number(summary["ready_timeout_ms"],
                                "ready_timeout_ms")
            self.assertEqual(summary["ready_timeout_ms"],
                             ready_timeout_ms)
        # Nothing was evaluated; measurements are unavailable, never 0.
        self.assertIs(type(summary["evaluated_sample_count"]), int)
        self.assertEqual(summary["evaluated_sample_count"], 0)
        self.assertIsNone(summary["exceedance_count"])
        self.assertIsNone(summary["max_excess_ms"])

    def snapshot_inputs(self):
        return (
            sorted(p.name for p in self.root.iterdir()),
            self.samples_path.read_bytes()
            if self.samples_path.exists() else None,
            self.baseline_path.read_bytes()
            if self.baseline_path.exists() else None,
        )


class ExistingBehaviorControls(DeadlineSummaryBase):
    """Behaviour that holds before the feature and must survive it.

    Every test in this class passes on the unmodified source; a
    failure here after the change is a regression, not feature work.
    """

    def test_default_ok_exit_0_without_summary(self):
        self.write_baseline(mature_baseline())
        self.write_samples([120] * 12)
        proc = self.run_check()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = self.stdout_json(proc, "default check")
        self.assertEqual(body["verdict"], "ok")
        self.assertNotIn("deadline_summary", body)
        self.assertNotIn(SUMMARY_SCHEMA, proc.stdout)

    def test_default_breach_exit_2_without_summary(self):
        self.write_baseline(mature_baseline())
        self.write_samples([700] * 12)  # legacy p50 breach
        proc = self.run_check()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        body = self.stdout_json(proc, "default check")
        self.assertEqual(body["verdict"], "breach")
        self.assertNotIn("deadline_summary", body)

    def test_default_provisional_unknown_exit_3_without_summary(self):
        self.write_baseline(init_baseline("synthetic fixture"))
        self.write_samples([120] * 12)
        proc = self.run_check()
        self.assertEqual(proc.returncode, 3, proc.stderr)
        body = self.stdout_json(proc, "default check")
        self.assertEqual(body["verdict"], "unknown")
        self.assertNotIn("deadline_summary", body)

    def test_default_missing_baseline_unknown_exit_3_shape(self):
        self.write_samples([120] * 12)
        proc = self.run_check(baseline=self.root / "absent.json")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        body = self.stdout_json(proc, "default check")
        self.assertEqual(set(body), {"verdict", "reason"})
        self.assertEqual(body["verdict"], "unknown")

    def test_default_empty_samples_exit_1(self):
        self.write_baseline(mature_baseline())
        self.write_samples_doc({"schema": SAMPLES_SCHEMA, "samples": []})
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_evaluate_helper_detail_shape_unchanged(self):
        parsed = parse_samples(samples_doc([120] * 12))
        verdict, detail = evaluate(mature_baseline(), parsed)
        self.assertEqual(verdict, "ok")
        self.assertEqual(set(detail), {"count", "min_ms", "p50_ms",
                                       "p95_ms", "max_ms", "breaches"})
        self.assertNotIn("deadline_summary", json.dumps(detail))

        verdict, detail = evaluate(init_baseline("synthetic fixture"),
                                   parsed)
        self.assertEqual(verdict, "unknown")
        self.assertEqual(set(detail), {"reason"})

    def test_helper_signatures_unchanged(self):
        def params(func):
            return list(inspect.signature(func).parameters)

        self.assertEqual(params(evaluate), ["base", "parsed"])
        self.assertEqual(params(parse_samples), ["doc"])
        self.assertEqual(params(record),
                         ["base", "parsed", "source", "recorded_by", "now"])
        self.assertEqual(params(summarize), ["durations"])
        self.assertEqual(params(percentile), ["sorted_values", "pct"])

    def test_flag_rejected_outside_check(self):
        self.write_samples([120] * 12)
        self.write_baseline(init_baseline("synthetic fixture"))
        baseline_before = self.baseline_path.read_bytes()
        other = self.root / "other.json"
        cases = {
            "init": ["init", "--baseline", str(other),
                     "--source", "synthetic", "--deadline-summary"],
            "record": ["record", "--samples", str(self.samples_path),
                       "--baseline", str(self.baseline_path),
                       "--source", "synthetic", "--recorded-by", "tester",
                       "--deadline-summary"],
            "top-level": ["--deadline-summary", "check",
                          "--samples", str(self.samples_path),
                          "--baseline", str(self.baseline_path)],
        }
        for label, argv in cases.items():
            with self.subTest(placement=label):
                proc = self.run_cli(*argv)
                self.assertEqual(
                    proc.returncode, 2,
                    "%s: expected argparse refusal (2), got %s (%r)"
                    % (label, proc.returncode, proc.stdout))
        self.assertFalse(other.exists(),
                         "rejected init must not write a baseline")
        self.assertEqual(self.baseline_path.read_bytes(), baseline_before,
                         "rejected record must not touch the baseline")

    def test_source_has_no_network_or_subprocess_imports(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        banned = {"socket", "urllib", "http", "ftplib", "smtplib",
                  "telnetlib", "subprocess", "requests", "httpx",
                  "paramiko"}
        self.assertEqual(imported & banned, set(),
                         "check is source-only; banned imports present")

    def test_default_check_is_read_only_and_idempotent(self):
        self.write_baseline(mature_baseline())
        self.write_samples([120] * 12)
        before = self.snapshot_inputs()
        first = self.run_check()
        second = self.run_check()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(self.snapshot_inputs(), before)


class DeadlineSummaryEvaluatedTest(DeadlineSummaryBase):
    """New behaviour: evaluated summaries over the fresh samples."""

    def test_percentile_ok_with_single_exceedance(self):
        # 99 fast + 1 past the 600s deadline: nearest-rank p95 is the
        # 95th of 100 (100s), so the legacy verdict stays ok while the
        # completed-sample summary still exposes the one exceedance.
        self.write_baseline(mature_baseline())
        self.write_samples([100] * 99 + [700])
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(body["verdict"], "ok")
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 100, 1, 100000)

    def test_all_below_deadline_reports_zero_and_zero(self):
        self.write_baseline(mature_baseline())
        self.write_samples([120, 180, 240, 300] * 3)
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 12, 0, 0)

    def test_exactly_at_deadline_is_not_an_exceedance(self):
        self.write_baseline(mature_baseline())
        self.write_samples([100] * 9 + [600])  # one exactly at 600s
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 10, 0, 0)

    def test_one_second_above_deadline_counts(self):
        # Strictly-greater boundary, other side: 601s exceeds by 1000ms.
        self.write_baseline(mature_baseline())
        self.write_samples([100] * 9 + [601])
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 10, 1, 1000)

    def test_duplicate_exceedances_each_count(self):
        # Three identical exceeding durations are three observations;
        # a de-duplicating implementation reports 1 and fails here.
        self.write_baseline(mature_baseline())
        self.write_samples([100] * 7 + [700, 700, 700])
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 10, 3, 100000)

    def test_all_above_deadline_with_breach_verdict(self):
        # Breach verdicts carry the summary too, and the summary does
        # not change the legacy exit code.
        self.write_baseline(mature_baseline())
        self.write_samples([700] * 12)
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(body["verdict"], "breach")
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 12, 12, 100000)

    def test_max_excess_is_the_largest_not_first_or_last(self):
        self.write_baseline(mature_baseline())
        self.write_samples([650, 100, 800] + [100] * 7)
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 10, 2, 200000)

    def test_lower_supplied_deadline_is_used_not_a_constant(self):
        # Supplied deadline 150s: every fresh 200s sample exceeds it,
        # yet p50/p95 (200s) stay under their seed alerts, so the
        # verdict is ok: the deadline is diagnostic, not a gate, and
        # the number comes from the baseline, not a built-in seed.
        self.write_baseline(mature_baseline(ready_timeout_ms=150000))
        self.write_samples([200] * 10)
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(body["verdict"], "ok")
        self.assert_evaluated(body["deadline_summary"],
                              150000, 10, 10, 50000)

    def test_higher_supplied_deadline_is_used_not_a_constant(self):
        # Supplied deadline 900s: fresh 700s samples do not exceed it
        # (they would exceed the 600s seed), while the legacy p50
        # alert at 600s still breaches. Deadline and verdict are
        # independent axes.
        self.write_baseline(mature_baseline(ready_timeout_ms=900000))
        self.write_samples([700] * 10)
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(body["verdict"], "breach")
        self.assert_evaluated(body["deadline_summary"],
                              900000, 10, 0, 0)

    def test_supplied_float_deadline_ms_precision(self):
        # Sub-second supplied deadline: 599s is under, 600s exceeds by
        # exactly 499.5ms. Millisecond/float arithmetic is required;
        # truncating the threshold to int or whole seconds fails here.
        self.write_baseline(mature_baseline(ready_timeout_ms=599500.5))
        self.write_samples([100] * 8 + [599, 600])
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assert_evaluated(body["deadline_summary"],
                              599500.5, 10, 1, 499.5)

    def test_fresh_samples_measured_not_stored_history(self):
        # Stored history is entirely past the deadline; a fresh fast
        # export must report zero exceedances. Measuring the stored
        # history, its count or its percentiles fails here.
        self.write_baseline(mature_baseline(stored_s=[1800] * 12))
        self.write_samples([100] * 12)
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 0)
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 12, 0, 0)

    def test_summary_echoes_no_instance_names_or_input_bodies(self):
        self.write_baseline(
            mature_baseline(source=SOURCE_MARKER))
        self.write_samples([100] * 9 + [700], prefix=INSTANCE_MARKER)
        proc = self.run_check(optin=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(INSTANCE_MARKER, proc.stdout)
        self.assertNotIn(SOURCE_MARKER, proc.stdout)
        body = self.stdout_json(proc, "opted-in check")
        self.assert_evaluated(body["deadline_summary"],
                              DEFAULT_READY_TIMEOUT_MS, 10, 1, 100000)

    def test_opted_in_check_is_read_only_and_idempotent(self):
        self.write_baseline(mature_baseline())
        self.write_samples([100] * 9 + [700])
        before = self.snapshot_inputs()
        first = self.run_check(optin=True)
        second = self.run_check(optin=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(self.snapshot_inputs(), before)


class DeadlineSummaryNotEvaluatedTest(DeadlineSummaryBase):
    """New behaviour: unknown verdicts say not_evaluated, never zero."""

    def test_provisional_baseline_not_evaluated(self):
        # A fresh exceedance is deliberately present: a provisional
        # baseline must still refuse to measure instead of reporting
        # either the exceedance or a quiet zero.
        self.write_baseline(init_baseline("synthetic fixture"))
        self.write_samples([100] * 9 + [700])
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(body["verdict"], "unknown")
        self.assert_not_evaluated(body["deadline_summary"],
                                  DEFAULT_READY_TIMEOUT_MS)

    def test_missing_baseline_not_evaluated_timeout_unknown(self):
        self.write_samples([100] * 9 + [700])
        absent = self.root / "absent.json"
        body, proc = self.run_both(baseline=absent)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(body["verdict"], "unknown")
        self.assert_not_evaluated(body["deadline_summary"], None)

    def test_unreadable_baseline_not_evaluated_timeout_unknown(self):
        self.write_samples([120] * 12)
        self.baseline_path.write_text("not json {", encoding="utf-8")
        body, proc = self.run_both()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(body["verdict"], "unknown")
        self.assert_not_evaluated(body["deadline_summary"], None)


class DeadlineSummaryPreservedBoundariesTest(DeadlineSummaryBase):
    """Malformed-input and discoverability boundaries of the feature."""

    def test_opted_in_empty_samples_preserves_exit_1(self):
        # An empty fresh list is refused by the existing parser before
        # any JSON verdict exists; opting in must not turn it into an
        # evaluated zero, a not_evaluated JSON verdict or a new code.
        self.write_baseline(mature_baseline())
        self.write_samples_doc({"schema": SAMPLES_SCHEMA, "samples": []})
        proc = self.run_check(optin=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("invalid input", proc.stderr)

    def test_opted_in_over_limit_samples_preserves_exit_1(self):
        # The existing 2000-row fresh-sample limit is unchanged.
        self.write_baseline(mature_baseline())
        self.write_samples([60] * (MAX_FRESH_SAMPLES + 1))
        proc = self.run_check(optin=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_check_help_names_the_flag(self):
        proc = self.run_cli("check", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--deadline-summary", proc.stdout)

    def test_docs_describe_contract_and_its_limits(self):
        self.assertTrue(
            DOCS.is_file(),
            "docs/garm-ready-time-baseline.md must document the "
            "deadline-summary contract")
        text = DOCS.read_text(encoding="utf-8").lower()
        for token in ("--deadline-summary", SUMMARY_SCHEMA,
                      "ready_timeout_ms", "strictly greater",
                      "not_evaluated", "exceedance", "completed",
                      "unfinished", "live health", "rollout"):
            with self.subTest(token=token):
                self.assertIn(token.lower(), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
