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
- Tests never touch the board, the journal, or real log files: every
  subprocess output, clock read, alert call, and state path is a fixture.
  All classes except the Main* threshold/stale-history/torn-state classes
  also never call alert() or main() (the alert/main guard stays in place
  for every other class). The Main* classes drive the real main() with
  mocked sh/time/alert and a tmp STATE to pin the production alert
  thresholds that a test-local replica cannot prove.

Pre-fix expectation (installed 113-line watchdog): the GateHoldsLiveness
tests and the malformed-admits test fail; TrueSilence, WindowBounds, and the
service-failure counts pass. Post-fix: the full file passes.
Post e8cb63c9 review fixes (9247ca2): the Main* threshold, stale-history,
carry-auth, and torn-state cases fail on e8cb63c9 behavior where applicable
and pass at head; the 25 original tests stay green.
"""

import calendar
import importlib.util
import json
import os
import tempfile
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


def capacity_for_log(log_text):
    """capacity_collapse() result for fixture log text at the fixed clock."""
    alert_guard, main_guard = _guarded()
    with mock.patch.object(psw, "sh", return_value=log_text), \
            mock.patch("time.time", return_value=FIXED_NOW), \
            alert_guard, main_guard:
        return psw.capacity_collapse()


def capacity_line(ts_str, value_text):
    return f"{ts_str} model-capacity-caps: C*={value_text} pool=main"


def carry_auth_for_log(log_text):
    """carry_auth_errors() count for fixture log text at the fixed clock."""
    alert_guard, main_guard = _guarded()
    with mock.patch.object(psw, "sh", return_value=log_text), \
            mock.patch("time.time", return_value=FIXED_NOW), \
            alert_guard, main_guard:
        return psw.carry_auth_errors()


def carry_line(ts_str, suffix="for account 7"):
    return f"{ts_str} review-carry: credential response invalid {suffix}"


def run_main(tail4000="", tail6000="", journals=None, initial_state=None):
    """Drive the real psw.main() with mocked sh/time/alert and a tmp STATE.

    tail4000: fixture for `tail -n 4000` (gate ticks, carry-auth, history).
    tail6000: fixture for `tail -n 6000` (capacity lines; "" keeps it quiet).
    journals: dict unit -> journalctl output (missing units read clean).
    initial_state: None (missing file), bytes, str, or dict to pre-write.

    Returns {"keys", "calls", "state", "sh_log"}: alert keys in call order,
    raw alert mock calls, the rewritten STATE parsed as JSON (or the raised
    exception when the rewrite is missing/invalid), and every mocked sh call.
    """
    if journals is None:
        journals = {}
    sh_log = []
    with tempfile.TemporaryDirectory() as tmpd:
        state_path = Path(tmpd) / "pipeline-stall-watch.state.json"
        if initial_state is not None:
            if isinstance(initial_state, bytes):
                state_path.write_bytes(initial_state)
            elif isinstance(initial_state, dict):
                state_path.write_text(json.dumps(initial_state))
            else:
                state_path.write_text(initial_state)

        def fake_sh(*args):
            sh_log.append(tuple(args))
            if not args:
                raise AssertionError("sh called with no args")
            if args[0] == "tail":
                if len(args) >= 3 and args[2] == "4000":
                    return tail4000
                if len(args) >= 3 and args[2] == "6000":
                    return tail6000
                raise AssertionError(f"unexpected tail args {args}")
            if args[0] == "journalctl":
                unit = args[args.index("-u") + 1] if "-u" in args else ""
                return journals.get(unit, "")
            raise AssertionError(f"unexpected sh call {args}")

        with mock.patch.object(psw, "sh", side_effect=fake_sh), \
                mock.patch("time.time", return_value=FIXED_NOW), \
                mock.patch.object(psw, "alert") as alert_mock, \
                mock.patch.object(psw, "STATE", state_path):
            psw.main()
            calls = list(alert_mock.call_args_list)
            keys = [c.args[0] if c.args else c.kwargs.get("key") for c in calls]
            try:
                state_data = json.loads(state_path.read_text())
            except Exception as exc:
                state_data = exc
    return {"keys": keys, "calls": calls, "state": state_data, "sh_log": list(sh_log)}


FAIL_LINE = "review-gate.service: Failed with result 'exit-code'."

RECENT_TICK = None  # built per-test via gate_line(ts_before(minutes=5), ...) to keep the clock fixture local.


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


class CapacityCollapseRobustness(unittest.TestCase):
    """Malformed C* values are skipped, never crash the scan (review finding)."""

    def test_malformed_capacity_value_is_skipped(self):
        log = "\n".join([
            capacity_line(ts_before(minutes=90), "8.0"),
            # Matches C*=([0-9.]+) but float() rejects it: must not raise.
            capacity_line(ts_before(minutes=60), "1.2.3"),
            capacity_line(ts_before(minutes=30), "4.0"),
        ])
        self.assertEqual(capacity_for_log(log), (4.0, 8.0))

    def test_all_malformed_capacity_values_yield_none(self):
        log = "\n".join([
            capacity_line(ts_before(minutes=90), "1.2.3"),
            capacity_line(ts_before(minutes=30), "9.9.9"),
        ])
        self.assertIsNone(capacity_for_log(log))

    def test_valid_capacity_values_unchanged(self):
        log = "\n".join([
            capacity_line(ts_before(minutes=90), "8.0"),
            capacity_line(ts_before(minutes=30), "4.0"),
        ])
        self.assertEqual(capacity_for_log(log), (4.0, 8.0))


class UtcTimestampParsing(unittest.TestCase):
    """`Z` ticks parse as UTC even on DST-observing hosts (review finding)."""

    def test_recent_tick_is_recent_under_dst_timezone(self):
        if not hasattr(time, "tzset"):
            self.skipTest("tzset unavailable on this platform")
        if not os.path.exists("/usr/share/zoneinfo/America/New_York"):
            self.skipTest("America/New_York tzdata missing")
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            if time.localtime(FIXED_NOW).tm_isdst <= 0:
                self.skipTest("fixed clock is outside DST; the mktime bug is not exercisable")
            log = gate_line(ts_before(minutes=5), "gate admits 1 head")
            age = age_for_log(log)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 5.0, places=6)
        self.assertFalse(is_silent(age), f"DST-zone tick must read recent, got age {age}")


class MainGateSilenceThresholds(unittest.TestCase):
    """Production gate-silent threshold pinned through the real main().

    Mutant check: `age > 2000` keeps the just-over case quiet (fail); the
    exact/under cases pin strict greater-than against `age >= 20`.
    """

    def test_just_over_20m_alerts_via_main(self):
        tail = gate_line(ts_before(minutes=20, seconds=1), "gate admits 1 head")
        result = run_main(tail4000=tail)
        self.assertEqual(result["keys"], ["gate-silent"],
                         f"20m+1s must alert gate-silent via main(), got {result['keys']}")

    def test_exactly_20m_quiet_via_main(self):
        tail = gate_line(ts_before(minutes=20), "gate admits 1 head")
        result = run_main(tail4000=tail)
        self.assertNotIn("gate-silent", result["keys"],
                         "exactly 20m must stay quiet via main() (strict >)")
        self.assertEqual(result["keys"], [])

    def test_just_under_20m_quiet_via_main(self):
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FIXED_NOW - 20 * 60 + 1))
        tail = gate_line(recent, "gate admits 1 head")
        result = run_main(tail4000=tail)
        self.assertNotIn("gate-silent", result["keys"])
        self.assertEqual(result["keys"], [])


class MainServiceFailureAlerts(unittest.TestCase):
    """Production service-failure alert line pinned through main(), per unit.

    Mutant check: `n >= 99` keeps the 3-failure case quiet (fail); a summed
    cross-unit count would alert the 2+2 case (fail).
    """

    def _recent_tick(self):
        return gate_line(ts_before(minutes=5), "gate admits 1 head")

    def test_two_failures_quiet_via_main(self):
        journals = {"review-gate.service": f"{FAIL_LINE}\nok\n{FAIL_LINE}\n"}
        result = run_main(tail4000=self._recent_tick(), journals=journals)
        fail_keys = [k for k in result["keys"] if k.startswith("fail:")]
        self.assertEqual(fail_keys, [], f"2 failures must stay quiet, got {result['keys']}")

    def test_three_failures_alert_via_main(self):
        journals = {"review-gate.service": f"{FAIL_LINE}\n{FAIL_LINE}\n{FAIL_LINE}\n"}
        result = run_main(tail4000=self._recent_tick(), journals=journals)
        self.assertIn("fail:review-gate.service", result["keys"],
                      f"3 failures must alert, got {result['keys']}")

    def test_failure_counts_are_per_unit_not_summed(self):
        journals = {
            "review-gate.service": f"{FAIL_LINE}\n{FAIL_LINE}\n",
            "review-carry.service": f"{FAIL_LINE}\n{FAIL_LINE}\n",
        }
        result = run_main(tail4000=self._recent_tick(), journals=journals)
        fail_keys = [k for k in result["keys"] if k.startswith("fail:")]
        self.assertEqual(fail_keys, [],
                         f"2+2 across units must not sum to an alert, got {result['keys']}")

    def test_three_failures_alert_only_failing_unit(self):
        fail_carry = "review-carry.service: Failed with result 'exit-code'."
        journals = {"review-carry.service": f"{fail_carry}\n" * 3}
        result = run_main(tail4000=self._recent_tick(), journals=journals)
        self.assertIn("fail:review-carry.service", result["keys"])
        self.assertNotIn("fail:review-gate.service", result["keys"])
        self.assertNotIn("fail:pr-automerge.service", result["keys"])


class MainCapacityBounds(unittest.TestCase):
    """Production capacity alert pinned through main(): latest < 1 AND prior > 10.

    Both edges are strict; mutants `<= 1` / `>= 10` alert the edge-quiet
    cases (fail), and widened bounds alert the low-prior quiet case (fail).
    """

    def _recent_tick(self):
        return gate_line(ts_before(minutes=5), "gate admits 1 head")

    def _cap_tail(self, prior, latest):
        return "\n".join([
            capacity_line(ts_before(minutes=90), str(prior)),
            capacity_line(ts_before(minutes=5), str(latest)),
        ])

    def test_collapse_inside_bounds_alerts_via_main(self):
        result = run_main(tail4000=self._recent_tick(),
                          tail6000=self._cap_tail(12.0, 0.5))
        self.assertIn("capacity-collapse", result["keys"])

    def test_collapse_just_inside_both_edges_alerts(self):
        result = run_main(tail4000=self._recent_tick(),
                          tail6000=self._cap_tail(10.01, 0.99))
        self.assertIn("capacity-collapse", result["keys"],
                      f"0.99/10.01 must alert, got {result['keys']}")

    def test_latest_at_one_quiet_via_main(self):
        result = run_main(tail4000=self._recent_tick(),
                          tail6000=self._cap_tail(12.0, 1.0))
        self.assertNotIn("capacity-collapse", result["keys"],
                         "latest == 1.0 must stay quiet (strict < 1)")

    def test_prior_at_ten_quiet_via_main(self):
        result = run_main(tail4000=self._recent_tick(),
                          tail6000=self._cap_tail(10.0, 0.5))
        self.assertNotIn("capacity-collapse", result["keys"],
                         "prior == 10.0 must stay quiet (strict > 10)")

    def test_both_edges_quiet_via_main(self):
        result = run_main(tail4000=self._recent_tick(),
                          tail6000=self._cap_tail(10.0, 1.0))
        self.assertNotIn("capacity-collapse", result["keys"])

    def test_low_prior_quiet_despite_collapsed_latest(self):
        result = run_main(tail4000=self._recent_tick(),
                          tail6000=self._cap_tail(9.9, 0.2))
        self.assertNotIn("capacity-collapse", result["keys"],
                         "collapsed latest with low prior must stay quiet")


class MainStaleHistory(unittest.TestCase):
    """No-tick tail branch pinned through main(): old history alerts, fresh/empty/garbage stays quiet.

    Fails on e8cb63c9: the alert case stays quiet there (no stale-history
    branch). The quiet cases pin no false positives on both revisions.
    """

    def test_no_tick_with_old_history_alerts_via_main(self):
        tail = "\n".join([
            f"{ts_before(minutes=30)} review-carry: carried 2 reviews",
            f"{ts_before(minutes=25)} pr-automerge: merged 1 head",
        ])
        result = run_main(tail4000=tail)
        self.assertIn("gate-silent", result["keys"],
                      f"no tick + >20m history must alert, got {result['keys']}")

    def test_no_tick_with_empty_tail_quiet_via_main(self):
        result = run_main(tail4000="")
        self.assertNotIn("gate-silent", result["keys"])
        self.assertEqual(result["keys"], [])

    def test_no_tick_with_recent_only_history_quiet_via_main(self):
        tail = "\n".join([
            f"{ts_before(minutes=10)} review-carry: carried 1 review",
            f"{ts_before(minutes=5)} pr-automerge: merged 1 head",
        ])
        result = run_main(tail4000=tail)
        self.assertNotIn("gate-silent", result["keys"],
                         "no tick but all history <20m must stay quiet (fresh install)")
        self.assertEqual(result["keys"], [])

    def test_no_tick_with_all_garbage_tail_quiet_via_main(self):
        tail = "\n".join([
            "not a log line at all",
            "short",
            "BAD-TIMESTAMP review-carry: garbage timestamp, no gate keywords",
        ])
        result = run_main(tail4000=tail)
        self.assertNotIn("gate-silent", result["keys"],
                         "no tick + unparseable tail must stay quiet")
        self.assertEqual(result["keys"], [])


class CarryAuthWindow(unittest.TestCase):
    """carry_auth_errors() counts only in-window well-formed lines.

    Fails on e8cb63c9: malformed-timestamp, undated, and short phrase lines
    count there via lexicographic prefix compare (probed 2/2 undated). The
    1800 s window mutant (1800->0) drops the in-window count to 0 (fail).
    """

    def test_in_window_well_formed_lines_count(self):
        log = "\n".join([
            carry_line(ts_before(minutes=10)),
            carry_line(ts_before(minutes=5)),
        ])
        self.assertEqual(carry_auth_for_log(log), 2)

    def test_stale_lines_do_not_count(self):
        log = "\n".join([
            carry_line(ts_before(minutes=35)),
            carry_line(ts_before(minutes=40)),
        ])
        self.assertEqual(carry_auth_for_log(log), 0,
                         "lines older than 30m must not count")

    def test_malformed_timestamp_lines_do_not_count(self):
        log = "\n".join([
            carry_line(ts_before(minutes=5)),
            "BAD-TIMESTAMP review-carry: credential response invalid for account 7",
        ])
        self.assertEqual(carry_auth_for_log(log), 1,
                         "malformed timestamp must be skipped, valid line kept")

    def test_undated_lines_do_not_count(self):
        log = "\n".join([
            carry_line(ts_before(minutes=5)),
            "review-carry: credential response invalid for account 7",
        ])
        self.assertEqual(carry_auth_for_log(log), 1,
                         "undated phrase line must not count as in-window")

    def test_short_lines_do_not_count(self):
        log = "\n".join([
            "short",
            "x credential response invalid",
            carry_line(ts_before(minutes=5)),
        ])
        self.assertEqual(carry_auth_for_log(log), 1,
                         "short/undated phrase lines must not count")


class TornStateResilience(unittest.TestCase):
    """Corrupt STATE never kills the watchdog: all checks run, valid state is rewritten.

    Fails on e8cb63c9: main() raises JSONDecodeError/UnicodeDecodeError at
    load before any check, so no alert fires and no valid state is written.
    """

    def _all_firing_fixtures(self):
        tail4000 = "\n".join([
            gate_line(ts_before(minutes=25), "gate admits 1 head"),
            carry_line(ts_before(minutes=5)),
        ])
        tail6000 = "\n".join([
            capacity_line(ts_before(minutes=90), "12.0"),
            capacity_line(ts_before(minutes=5), "0.5"),
        ])
        journals = {"review-gate.service": f"{FAIL_LINE}\n" * 3}
        return tail4000, tail6000, journals

    def _assert_all_checks_ran(self, sh_log):
        units_seen = set()
        saw_tail4000 = saw_tail6000 = False
        for args in sh_log:
            if not args:
                continue
            if args[0] == "journalctl" and "-u" in args:
                units_seen.add(args[args.index("-u") + 1])
            if args[0] == "tail" and len(args) >= 3:
                if args[2] == "4000":
                    saw_tail4000 = True
                if args[2] == "6000":
                    saw_tail6000 = True
        for unit in ("review-gate.service", "review-carry.service", "pr-automerge.service"):
            self.assertIn(unit, units_seen, f"main() must check {unit} despite torn state")
        self.assertTrue(saw_tail4000, "main() must read the 4000-line tail despite torn state")
        self.assertTrue(saw_tail6000, "main() must read the 6000-line tail despite torn state")

    def test_corrupt_state_bytes_runs_all_checks_and_rewrites_valid(self):
        tail4000, tail6000, journals = self._all_firing_fixtures()
        result = run_main(tail4000=tail4000, tail6000=tail6000, journals=journals,
                          initial_state=b"\xff\xfe\x00\x01 torn {{{ not json")
        for key in ("fail:review-gate.service", "gate-silent", "carry-auth", "capacity-collapse"):
            self.assertIn(key, result["keys"],
                          f"{key} must still fire with corrupt STATE, got {result['keys']}")
        self._assert_all_checks_ran(result["sh_log"])
        self.assertIsInstance(result["state"], dict,
                              f"torn STATE must be rewritten as valid JSON, got {result['state']!r}")

    def test_truncated_json_state_treated_as_empty(self):
        tail4000, _, _ = self._all_firing_fixtures()
        result = run_main(tail4000=tail4000, initial_state='{"gate-silent": {"at": 123')
        self.assertIn("gate-silent", result["keys"])
        self.assertIsInstance(result["state"], dict)


if __name__ == "__main__":
    unittest.main()
