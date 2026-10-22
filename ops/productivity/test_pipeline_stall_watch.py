#!/usr/bin/env python3
"""Pipeline-silence acceptance: successful gate-holds ticks count as liveness.

The watchdog reports review-gate silence when no successful tick appears in
20 minutes. A guarded tick that intentionally holds work for reviewer
capacity/backlog ("gate holds") is a successful tick: it proves the gate ran.
Only "gate admits" / "requested review" were recognized, so a recent holds
tick with an old admission read as silent. These tests pin the corrected
predicate.

Run: (cd ops/productivity && python3 -B -m unittest test_pipeline_stall_watch)

Contract for the module under test (ops/productivity/pipeline_stall_watch.py):
- Minimal fix: recognize "gate holds" alongside "gate admits" and
  "requested review" when scanning " review-gate: " lines, keep the 20-minute
  threshold with strict greater-than, and skip lines with malformed
  timestamps instead of raising.
- Optional pure helpers, used when present, never required:
  is_gate_tick(line), last_gate_tick_age_min_from_lines(lines, now),
  is_gate_silent(age_min), GATE_SILENCE_MIN == 20.
  Without them the tests drive the installed impure functions with mocked
  log/journal output and a fixed clock, so the same file fails pre-fix and
  passes post-fix with no API change required.
- Tests never call alert() or main() and never touch the board, the journal,
  or real log files: every subprocess output and clock read is a fixture.

Pre-fix expectation (installed 113-line watchdog): the GateHoldsLiveness
tests and the malformed-admits test fail; TrueSilence, WindowBounds, and the
service-failure counts pass. Post-fix: the full file passes.
"""

import calendar
import importlib.util
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import pipeline_stall_watch as psw
except ImportError:  # `python3 test_pipeline_stall_watch.py` without the dir on sys.path.
    _CANDIDATE = Path(__file__).with_name("pipeline_stall_watch.py")
    if not _CANDIDATE.exists():
        raise ImportError(
            "pipeline_stall_watch.py is not on sys.path nor beside this test; "
            "the fix lands it at ops/productivity/pipeline_stall_watch.py on this branch."
        )
    _spec = importlib.util.spec_from_file_location("pipeline_stall_watch", _CANDIDATE)
    psw = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(psw)


FIXED_NOW_STR = "2026-10-22T05:10:00Z"
FIXED_NOW = calendar.timegm(time.strptime(FIXED_NOW_STR, "%Y-%m-%dT%H:%M:%SZ"))
SILENCE_MINUTES = 20
THRESHOLD_NAMES = (
    "GATE_SILENCE_MIN",
    "GATE_SILENCE_MINUTES",
    "GATE_SILENCE_THRESHOLD_MIN",
    "SILENCE_MIN",
)


def ts_before(minutes=0, seconds=0):
    """UTC timestamp string `minutes`+`seconds` before the fixed clock."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FIXED_NOW - minutes * 60 - seconds))


def gate_line(ts_str, message):
    return f"{ts_str} review-gate: {message}"


def _guarded():
    """Context that fails the test if the alert/main path is reached."""
    return (
        mock.patch.object(psw, "alert", side_effect=AssertionError("tests must not call alert")),
        mock.patch.object(psw, "main", side_effect=AssertionError("tests must not call main")),
    )


def age_for_log(log_text):
    """Gate-tick age in minutes for fixture log text at the fixed clock.

    Uses the pure helper when the module exposes one, else drives the
    installed impure reader with a mocked tail output and clock. Returns None
    when no recognized tick exists.
    """
    lines = log_text.splitlines()
    alert_guard, main_guard = _guarded()
    if hasattr(psw, "last_gate_tick_age_min_from_lines"):
        with alert_guard, main_guard:
            return psw.last_gate_tick_age_min_from_lines(lines, FIXED_NOW)
    if hasattr(psw, "gate_tick_age_min"):
        with alert_guard, main_guard:
            return psw.gate_tick_age_min(lines, FIXED_NOW)
    with mock.patch.object(psw, "sh", return_value=log_text), \
            mock.patch("time.time", return_value=FIXED_NOW), \
            alert_guard, main_guard:
        return psw.last_gate_tick_age_min()


def is_silent(age_min):
    """Silence predicate: unknown (None) is not silent; else strictly over 20m."""
    if hasattr(psw, "is_gate_silent"):
        alert_guard, main_guard = _guarded()
        with alert_guard, main_guard:
            return psw.is_gate_silent(age_min)
    threshold = SILENCE_MINUTES
    for name in THRESHOLD_NAMES:
        if hasattr(psw, name):
            threshold = getattr(psw, name)
            break
    return age_min is not None and age_min > threshold


def failures_for_journal(journal_text, unit="review-gate.service"):
    """Service-failure count for fixture journal output."""
    alert_guard, main_guard = _guarded()
    if hasattr(psw, "count_failures"):
        with alert_guard, main_guard:
            return psw.count_failures(journal_text)
    with mock.patch.object(psw, "sh", return_value=journal_text), alert_guard, main_guard:
        return psw.failures(unit)


class GateHoldsLiveness(unittest.TestCase):
    """Successful backpressure ticks are liveness. All fail pre-fix."""

    def test_old_admits_plus_recent_holds_is_not_silent(self):
        log = "\n".join([
            gate_line(ts_before(minutes=25), "gate admits 3 heads; requested review"),
            gate_line(ts_before(minutes=5), "gate holds; reviewer capacity exhausted, backlog 15"),
        ])
        age = age_for_log(log)
        self.assertIsNotNone(age, "recent holds tick must be recognized")
        self.assertLess(age, 10, f"age follows the holds tick, got {age}")
        self.assertFalse(is_silent(age), f"recent holds must not read silent (age {age})")

    def test_recent_holds_alone_counts_as_liveness(self):
        log = gate_line(ts_before(minutes=4), "gate holds; backlog 15, waiting on reviewers")
        age = age_for_log(log)
        self.assertIsNotNone(age, "holds alone must produce a tick age, not unknown")
        self.assertLess(age, 10, f"holds-only age must be recent, got {age}")
        self.assertFalse(is_silent(age))

    def test_holds_with_backlog_mention_is_liveness_not_throughput(self):
        # Liveness only: the test asserts the gate ran recently. It deliberately
        # does NOT assert the backlog drained, merged, or shrank.
        log = gate_line(ts_before(minutes=6), "gate holds (backlog 15, reviewer capacity 0)")
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertLess(age, 10)
        self.assertFalse(is_silent(age))

    def test_stale_holds_does_not_mask_true_silence(self):
        log = "\n".join([
            gate_line(ts_before(minutes=35), "gate holds; backlog 9"),
            gate_line(ts_before(minutes=25), "gate admits 1 head"),
        ])
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertGreater(age, SILENCE_MINUTES)
        self.assertTrue(is_silent(age), "holds older than 20m must still read silent")


class TrueSilence(unittest.TestCase):
    """Genuinely stale logs still alert. Pass pre-fix and post-fix."""

    def test_stale_admits_with_no_holds_is_silent(self):
        log = gate_line(ts_before(minutes=25), "gate admits 2 heads; requested review")
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertGreater(age, SILENCE_MINUTES)
        self.assertTrue(is_silent(age))

    def test_stale_requested_review_with_no_holds_is_silent(self):
        log = gate_line(ts_before(minutes=30), "requested review for 4 heads")
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertTrue(is_silent(age))

    def test_empty_log_returns_unknown_not_silent(self):
        age = age_for_log("")
        self.assertIsNone(age, "no history must stay unknown, never a fabricated age")
        self.assertFalse(is_silent(age), "unknown state must not raise the silence alert")

    def test_no_gate_lines_returns_unknown(self):
        log = "\n".join([
            f"{ts_before(minutes=2)} review-carry: carried 3 reviews",
            f"{ts_before(minutes=1)} pr-automerge: merged 1 head",
        ])
        age = age_for_log(log)
        self.assertIsNone(age)
        self.assertFalse(is_silent(age))


class WindowBounds(unittest.TestCase):
    """The 20-minute threshold is strict greater-than and stays 20."""

    def test_exactly_20m_is_not_silent(self):
        log = gate_line(ts_before(minutes=20), "gate admits 1 head")
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 20.0, places=6)
        self.assertFalse(is_silent(age), "exactly 20m must not alert (strict >)")

    def test_just_over_20m_is_silent(self):
        log = gate_line(ts_before(minutes=20, seconds=1), "gate admits 1 head")
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertGreater(age, SILENCE_MINUTES)
        self.assertTrue(is_silent(age))

    def test_just_under_20m_is_not_silent(self):
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FIXED_NOW - 20 * 60 + 1))
        log = gate_line(recent, "gate admits 1 head")
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertLess(age, SILENCE_MINUTES)
        self.assertFalse(is_silent(age))

    def test_threshold_constant_is_20_if_present(self):
        found = [(name, getattr(psw, name)) for name in THRESHOLD_NAMES if hasattr(psw, name)]
        if not found:
            self.skipTest("no named threshold constant; WindowBounds pins 20m behaviorally")
        for name, value in found:
            self.assertEqual(value, SILENCE_MINUTES, f"{name} must stay 20 minutes")


class Robustness(unittest.TestCase):
    """Malformed input never crashes and never swallows a real alert."""

    def test_garbage_lines_do_not_mask_true_silence(self):
        log = "\n".join([
            "not a log line at all",
            "short",
            f"{ts_before(minutes=25)} review-gate: gate admits 1 head",
            "",
            "2026-13-99T99:99:99Z review-carry: garbage timestamp, no gate keywords",
            f"{ts_before(minutes=3)} review-carry: carried 1 review",
        ])
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertGreater(age, SILENCE_MINUTES)
        self.assertTrue(is_silent(age))

    def test_malformed_timestamp_on_admits_line_is_skipped(self):
        log = "\n".join([
            gate_line(ts_before(minutes=25), "gate admits 1 head"),
            "BAD-TIMESTAMP review-gate: gate admits 9 heads",
        ])
        age = age_for_log(log)
        self.assertIsNotNone(age, "malformed tick must be skipped, valid history kept")
        self.assertGreater(age, SILENCE_MINUTES)
        self.assertTrue(is_silent(age))

    def test_malformed_timestamp_on_holds_line_is_skipped(self):
        log = "\n".join([
            gate_line(ts_before(minutes=25), "gate admits 1 head"),
            "BAD-TIMESTAMP review-gate: gate holds; backlog 4",
        ])
        age = age_for_log(log)
        self.assertIsNotNone(age)
        self.assertGreater(age, SILENCE_MINUTES)
        self.assertTrue(is_silent(age))

    def test_repeated_service_failures_still_count(self):
        fail = "review-gate.service: Failed with result 'exit-code'."
        cases = [
            ("no failures", "started cleanly\nready\n", 0),
            ("two failures stay below the alert line", f"{fail}\nok\n{fail}\n", 2),
            ("three failures trip the alert line", f"{fail}\n{fail}\n{fail}\n", 3),
            ("five failures count fully", f"{fail}\n" * 5, 5),
        ]
        for name, journal, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(failures_for_journal(journal), expected)

    def test_service_failures_independent_of_gate_liveness(self):
        fail = "review-gate.service: Failed with result 'exit-code'."
        log = "\n".join([
            gate_line(ts_before(minutes=25), "gate admits 1 head"),
            gate_line(ts_before(minutes=5), "gate holds; backlog 6"),
        ])
        age = age_for_log(log)
        self.assertFalse(is_silent(age), "recent holds must keep the gate healthy")
        self.assertEqual(failures_for_journal(f"{fail}\n{fail}\n{fail}\n"), 3)


class NoBoardMutation(unittest.TestCase):
    """Helpers prove the alert/main path is unreachable from these tests."""

    def test_age_computation_never_reaches_alert_or_main(self):
        fixtures = [
            "",
            gate_line(ts_before(minutes=25), "gate admits 1 head"),
            gate_line(ts_before(minutes=5), "gate holds; backlog 2"),
        ]
        for body in fixtures:
            with self.subTest(body=body[:40]):
                age_for_log(body)  # raises if alert/main is called.

    def test_failure_count_never_reaches_alert_or_main(self):
        failures_for_journal("Failed with result 'exit-code'.\n")  # raises if alert/main is called.


class OptionalPureHelpers(unittest.TestCase):
    """Exercised only when the fix exposes pure helpers; skipped otherwise."""

    def test_is_gate_tick_recognizes_holds_when_present(self):
        if not hasattr(psw, "is_gate_tick"):
            self.skipTest("is_gate_tick not exposed; age tests cover recognition")
        self.assertTrue(psw.is_gate_tick(gate_line(ts_before(minutes=1), "gate holds; backlog 1")))
        self.assertTrue(psw.is_gate_tick(gate_line(ts_before(minutes=1), "gate admits 1 head")))
        self.assertTrue(psw.is_gate_tick(gate_line(ts_before(minutes=1), "requested review")))
        self.assertFalse(psw.is_gate_tick(f"{ts_before(minutes=1)} review-carry: carried 1"))
        self.assertFalse(psw.is_gate_tick("garbage line"))

    def test_pure_age_helper_matches_impure_when_present(self):
        pure = None
        for name in ("last_gate_tick_age_min_from_lines", "gate_tick_age_min"):
            if hasattr(psw, name):
                pure = getattr(psw, name)
                break
        if pure is None:
            self.skipTest("no pure age helper exposed")
        log = "\n".join([
            gate_line(ts_before(minutes=25), "gate admits 1 head"),
            gate_line(ts_before(minutes=5), "gate holds; backlog 1"),
        ])
        alert_guard, main_guard = _guarded()
        with alert_guard, main_guard:
            pure_age = pure(log.splitlines(), FIXED_NOW)
        self.assertIsNotNone(pure_age)
        self.assertLess(pure_age, 10)


if __name__ == "__main__":
    unittest.main()
