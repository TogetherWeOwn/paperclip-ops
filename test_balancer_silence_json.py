#!/usr/bin/env python3
# ===========================================================================
# test_balancer_silence_json.py — acceptance tests for machine-readable JSON
# verdicts from the balancer silence guard.
#
# Paired acceptance suite. The Builder rebases onto this card's handoff SHA
# and MUST NEVER EDIT THIS FILE. This branch never merges standalone; the
# implementation lands in the build PR where this suite must go GREEN.
#
# WHAT THIS PINS. `balancer_silence_guard.py --json` emits one versioned
# JSON document on stdout that agents can parse instead of scraping the
# FRESH/STALE/GAP/RETIRED/UNMEASURABLE prose. Text mode and exit codes stay
# byte-identical when --json is absent.
#
# THE ENVELOPE (exact contract; key sets are closed — no extras):
#
#   {
#     "schema_version": 1,                 # strict int, not bool
#     "tool": "balancer_silence_guard.py",
#     "verdict": "fresh"|"stale"|"retired"|"unmeasurable",
#     "producer": "fleet-quota-balancer",
#     "now": "<iso>"|null,                # effective now; null only for bad --now
#     "threshold_min": <number>,           # echo of --max-age-min (may be <=0 on bad input)
#     "window_h": <number>,                # echo of --window-h (may be <=0 on bad input)
#     "age_min": <number>|null,            # null when unmeasurable (no tail)
#     "last_row": null|{"at": "<iso>", "model": "<str>", "reason": "<str>"},
#     "state": {"summary": "<non-empty>"}, # equals read_state() text summary
#     "gaps": [                            # chronological, bounded to GAP_CAP
#       {"start": "<iso>", "end": "<iso>", "duration_min": <number>,
#        "left": {"model": "<str>", "reason": "<str>"},
#        "right": {"model": "<str>", "reason": "<str>"}}
#     ],
#     "gaps_truncated": <strict int >=0>,   # omitted due to cap
#     "retirement": {"consulted": <bool>, "accepted": <bool>,
#                    "detail": "<non-empty>"},
#     "error": null|{"code": "<stable>", "detail": "<non-empty>"},
#     "skipped_lines": <strict int >=0>
#   }
#
# VERDICT <-> EXIT: fresh->0, stale->1, retired->0, unmeasurable->2.
# `error` is null unless the verdict is "unmeasurable".
#
# STABLE ERROR CODES (exact strings):
#   log_missing, log_unreadable, log_no_parseable_row, log_no_row_before_now,
#   bad_now, bad_max_age_min, bad_window_h
#
# RETIREMENT. Consulted only on the stale path. Fresh and unmeasurable runs
# report consulted=false, accepted=false with a "not consulted" detail.
# Stale runs report consulted=true; accepted=true only for a valid marker
# (verdict retired), otherwise accepted=false with the same refusal reason
# text mode prints on stderr (parity). Refusal reasons are the existing
# read_retirement() notes: missing, unreadable, not-an-object,
# retired-is-not-true, field-missing, producer-mismatch,
# approved_at-unparseable, approved_at-in-future.
#
# GAPS. Consecutive-row gaps longer than the threshold, ending in-window,
# chronological. Bounded to GAP_CAP=10; gaps_truncated is total minus
# emitted (0 when under cap). Text GAP line count equals
# len(gaps)+gaps_truncated on every fixture.
#
# PARITY. Every JSON fixture also runs text mode: exit codes equal, verdict
# maps (FRESH->fresh, STALE->stale, RETIRED->retired, UNMEASURABLE->
# unmeasurable), last-row instant and age agree, gap counts agree.
#
# NO ECHO. JSON carries instants/models/reasons/counts only. Fixture
# sentinel payloads in extra log fields never appear in JSON bytes.
#
# Deterministic: temp-dir logs, injected --now, no network, no clock.
# ===========================================================================
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import balancer_silence_guard as guard

GAP_CAP = 10

TOP_KEYS = {"schema_version", "tool", "verdict", "producer", "now",
            "threshold_min", "window_h", "age_min", "last_row", "state",
            "gaps", "gaps_truncated", "retirement", "error", "skipped_lines"}
LAST_ROW_KEYS = {"at", "model", "reason"}
STATE_KEYS = {"summary"}
GAP_KEYS = {"start", "end", "duration_min", "left", "right"}
GAP_SIDE_KEYS = {"model", "reason"}
RETIREMENT_KEYS = {"consulted", "accepted", "detail"}
ERROR_KEYS = {"code", "detail"}

ERROR_CODES = {"log_missing", "log_unreadable", "log_no_parseable_row",
               "log_no_row_before_now", "bad_now", "bad_max_age_min",
               "bad_window_h"}

SENTINEL = "JSON-NOECHO-SENTINEL-8f3a9c1e7b2d"

BASE_NOW = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)


def synthetic_rows(end, count=30, step_min=1, model="model-a",
                   reason="reason-x", extra=""):
    lines = []
    for n in range(count, -1, -1):
        stamp = (end - timedelta(minutes=n * step_min)).isoformat()
        line = ('{"at": "%s", "model": "%s", "reason": "%s"%s}'
                % (stamp, model, reason, extra))
        lines.append(line)
    return "\n".join(lines) + "\n"


def gap_fixture_rows():
    """Four 1-min rows, a 193-min gap, then three 1-min rows (synthetic)."""
    base = datetime(2026, 10, 4, 14, 11, tzinfo=timezone.utc)
    stamps = [
        base,
        base + timedelta(minutes=1),
        base + timedelta(minutes=2),
        base + timedelta(minutes=3),
        base + timedelta(minutes=3 + 193),
        base + timedelta(minutes=3 + 193 + 1),
        base + timedelta(minutes=3 + 193 + 2),
    ]
    lines = []
    for i, stamp in enumerate(stamps):
        if i < 4:
            model, reason = "model-a", "reason-x"
        else:
            model, reason = "model-b", "reason-y"
        lines.append('{"at": "%s", "model": "%s", "reason": "%s"}'
                     % (stamp.isoformat(), model, reason))
    return "\n".join(lines) + "\n"


GAP_NOW = "2026-10-04T17:45:00+00:00"


class JsonCase(unittest.TestCase):
    maxDiff = 6000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write_log(self, content):
        path = os.path.join(self.tmp.name, "fleet-quota-balancer.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def write_state(self, content="{}"):
        path = os.path.join(self.tmp.name, "fleet-quota-balancer.state.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def write_marker(self, payload):
        path = os.path.join(self.tmp.name, "fleet-quota-balancer.retired.json")
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)
        return path

    def valid_marker(self, **overrides):
        marker = {
            "retired": True,
            "producer": "fleet-quota-balancer",
            "successor": "native-routing",
            "approved_by": "test-approver",
            "approved_at": "2026-10-04T17:40:00+00:00",
        }
        marker.update(overrides)
        return self.write_marker(marker)

    def run_guard(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = guard.main(list(argv))
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 2
        return rc, out.getvalue(), err.getvalue()

    def run_text(self, *argv):
        return self.run_guard(*argv)

    def run_json(self, *argv):
        rc, out, err = self.run_guard(*(list(argv) + ["--json"]))
        try:
            env = json.loads(out)
        except (ValueError, TypeError):
            env = None
        return rc, out, err, env

    def assertStrictInt(self, value, msg=""):
        self.assertIs(type(value), int,
                      "%s: %r is not a strict int" % (msg, value))

    def assertStrictBool(self, value, msg=""):
        self.assertIs(type(value), bool,
                      "%s: %r is not a strict bool" % (msg, value))

    def assertNumber(self, value, msg=""):
        self.assertTrue(isinstance(value, (int, float))
                        and not isinstance(value, bool),
                        "%s: %r is not a number" % (msg, value))

    def assertEnvelopeShape(self, env, threshold=None):
        self.assertIsInstance(env, dict, "JSON stdout is not an object")
        self.assertEqual(set(env.keys()), TOP_KEYS)
        self.assertStrictInt(env["schema_version"], "schema_version")
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["tool"], "balancer_silence_guard.py")
        self.assertIn(env["verdict"],
                      ("fresh", "stale", "retired", "unmeasurable"))
        self.assertEqual(env["producer"], "fleet-quota-balancer")
        if env["now"] is not None:
            self.assertIsInstance(env["now"], str)
            guard.parse_instant(env["now"])
        self.assertNumber(env["threshold_min"], "threshold_min")
        self.assertNumber(env["window_h"], "window_h")
        if env["verdict"] in ("fresh", "stale", "retired"):
            self.assertNumber(env["age_min"], "age_min")
            self.assertGreaterEqual(env["age_min"], 0)
            self.assertIsInstance(env["last_row"], dict)
            self.assertEqual(set(env["last_row"].keys()), LAST_ROW_KEYS)
            guard.parse_instant(env["last_row"]["at"])
            self.assertTrue(env["last_row"]["model"], "empty last_row.model")
            self.assertTrue(env["last_row"]["reason"], "empty last_row.reason")
            self.assertIsNone(env["error"])
        else:
            self.assertIsNone(env["age_min"])
            self.assertIsNone(env["last_row"])
            self.assertIsInstance(env["error"], dict)
            self.assertEqual(set(env["error"].keys()), ERROR_KEYS)
            self.assertIn(env["error"]["code"], ERROR_CODES)
            self.assertTrue(env["error"]["detail"].strip(), "empty error.detail")
        self.assertIsInstance(env["state"], dict)
        self.assertEqual(set(env["state"].keys()), STATE_KEYS)
        self.assertTrue(env["state"]["summary"].strip(), "empty state.summary")
        self.assertIsInstance(env["gaps"], list)
        self.assertLessEqual(len(env["gaps"]), GAP_CAP)
        self.assertStrictInt(env["gaps_truncated"], "gaps_truncated")
        self.assertGreaterEqual(env["gaps_truncated"], 0)
        if len(env["gaps"]) < GAP_CAP:
            # Under cap, nothing is omitted. At cap, truncated may be 0
            # (exactly cap gaps) or more; over-cap tests pin the count.
            pass
        prev_start = None
        for gap in env["gaps"]:
            self.assertEqual(set(gap.keys()), GAP_KEYS, gap)
            start = guard.parse_instant(gap["start"])
            end = guard.parse_instant(gap["end"])
            self.assertGreater(end, start)
            if prev_start is not None:
                self.assertGreaterEqual(start, prev_start,
                                        "gaps not chronological")
            prev_start = start
            self.assertNumber(gap["duration_min"], "duration_min")
            self.assertGreater(gap["duration_min"], 0)
            if threshold is not None:
                self.assertGreater(gap["duration_min"], threshold)
            for side in ("left", "right"):
                self.assertEqual(set(gap[side].keys()), GAP_SIDE_KEYS, gap)
                self.assertTrue(gap[side]["model"], "empty gap model")
                self.assertTrue(gap[side]["reason"], "empty gap reason")
        self.assertIsInstance(env["retirement"], dict)
        self.assertEqual(set(env["retirement"].keys()), RETIREMENT_KEYS)
        self.assertStrictBool(env["retirement"]["consulted"], "consulted")
        self.assertStrictBool(env["retirement"]["accepted"], "accepted")
        self.assertTrue(env["retirement"]["detail"].strip(),
                        "empty retirement.detail")
        if env["verdict"] in ("fresh", "unmeasurable"):
            self.assertFalse(env["retirement"]["consulted"],
                             "retirement consulted off the stale path")
            self.assertFalse(env["retirement"]["accepted"])
            self.assertIn("not consulted",
                          env["retirement"]["detail"].lower())
        else:
            self.assertTrue(env["retirement"]["consulted"])
            if env["verdict"] == "retired":
                self.assertTrue(env["retirement"]["accepted"])
            else:
                self.assertFalse(env["retirement"]["accepted"])
        self.assertStrictInt(env["skipped_lines"], "skipped_lines")
        self.assertGreaterEqual(env["skipped_lines"], 0)

    def assertSingleJsonStdout(self, out):
        self.assertTrue(out.strip(), "JSON stdout is empty")
        try:
            env = json.loads(out)
        except ValueError as exc:
            self.fail("stdout is not one JSON document: %s\nstdout=%r"
                      % (exc, out))
        for marker in ("FRESH:", "STALE:", "RETIRED:", "UNMEASURABLE:", "GAP "):
            self.assertNotIn(marker, out,
                             "prose marker %r leaked into JSON stdout" % marker)
        return env

    def text_verdict(self, out, err):
        if "FRESH:" in out:
            return "fresh"
        if "STALE:" in out:
            return "stale"
        if "RETIRED:" in out:
            return "retired"
        if "UNMEASURABLE:" in err:
            return "unmeasurable"
        self.fail("no text verdict in out=%r err=%r" % (out, err))

    def assertParity(self, text_rc, text_out, text_err, json_rc, env):
        self.assertEqual(text_rc, json_rc, "exit-code parity")
        expected = self.text_verdict(text_out, text_err)
        self.assertEqual(env["verdict"], expected, "verdict parity")
        if expected in ("fresh", "retired"):
            self.assertEqual(json_rc, 0)
        elif expected == "stale":
            self.assertEqual(json_rc, 1)
        else:
            self.assertEqual(json_rc, 2)
        text_gaps = [l for l in text_out.splitlines() if l.startswith("GAP ")]
        total = len(env["gaps"]) + env["gaps_truncated"]
        self.assertEqual(len(text_gaps), total, "gap-count parity")
        if expected in ("fresh", "stale", "retired"):
            m = re.search(r"last row (\S+) .* age=([0-9.]+) min", text_out)
            self.assertIsNotNone(m, "no last-row line in text: %r" % text_out)
            text_at = guard.parse_instant(m.group(1))
            json_at = guard.parse_instant(env["last_row"]["at"])
            self.assertEqual(text_at, json_at, "last-row instant parity")
            text_age = float(m.group(2))
            self.assertAlmostEqual(text_age, env["age_min"], delta=0.06,
                                   msg="age parity")


class JsonFreshStale(JsonCase):
    def test_fresh_log_json_fresh(self):
        log = self.write_log(synthetic_rows(BASE_NOW))
        state = self.write_state()
        now = BASE_NOW.isoformat()
        t_rc, t_out, t_err = self.run_text("--log", log, "--state", state,
                                           "--now", now)
        j_rc, j_out, j_err, env = self.run_json("--log", log, "--state", state,
                                               "--now", now)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "fresh")
        self.assertEqual(j_rc, 0)
        self.assertEqual(env["gaps"], [])
        self.assertEqual(env["gaps_truncated"], 0)
        self.assertAlmostEqual(env["age_min"], 0.0, delta=0.01)
        self.assertEqual(guard.parse_instant(env["last_row"]["at"]), BASE_NOW)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_stale_log_json_stale_with_gap(self):
        log = self.write_log(gap_fixture_rows())
        state = self.write_state()
        t_rc, t_out, t_err = self.run_text("--log", log, "--state", state,
                                           "--now", GAP_NOW)
        j_rc, j_out, j_err, env = self.run_json("--log", log, "--state", state,
                                               "--now", GAP_NOW)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "stale")
        self.assertEqual(j_rc, 1)
        self.assertEqual(len(env["gaps"]), 1)
        self.assertEqual(env["gaps_truncated"], 0)
        gap = env["gaps"][0]
        self.assertAlmostEqual(gap["duration_min"], 193.0, delta=0.2)
        self.assertEqual(gap["left"]["model"], "model-a")
        self.assertEqual(gap["right"]["model"], "model-b")
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_age_at_threshold_json_fresh(self):
        last = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log(synthetic_rows(last, count=2))
        state = self.write_state()
        now = (last + timedelta(minutes=10)).isoformat()
        t_rc, t_out, t_err = self.run_text("--log", log, "--state", state,
                                           "--now", now, "--max-age-min", "10")
        j_rc, j_out, j_err, env = self.run_json("--log", log, "--state", state,
                                               "--now", now, "--max-age-min",
                                               "10")
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "fresh")
        self.assertEqual(j_rc, 0)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_gap_at_threshold_json_no_gap(self):
        base = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        lines = [
            '{"at": "%s", "model": "m", "reason": "r"}' % base.isoformat(),
            '{"at": "%s", "model": "m", "reason": "r"}'
            % (base + timedelta(minutes=10)).isoformat(),
        ]
        log = self.write_log("\n".join(lines) + "\n")
        state = self.write_state()
        now = (base + timedelta(minutes=11)).isoformat()
        t_rc, t_out, t_err = self.run_text("--log", log, "--state", state,
                                           "--now", now, "--max-age-min", "10")
        j_rc, j_out, j_err, env = self.run_json("--log", log, "--state", state,
                                               "--now", now, "--max-age-min",
                                               "10")
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["gaps"], [])
        self.assertEqual(env["gaps_truncated"], 0)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)


class JsonRetirement(JsonCase):
    def test_stale_valid_marker_json_retired(self):
        log = self.write_log(gap_fixture_rows())
        state = self.write_state()
        marker = self.valid_marker()
        args = ["--log", log, "--state", state, "--now", GAP_NOW,
                "--retirement-file", marker]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "retired")
        self.assertEqual(j_rc, 0)
        self.assertTrue(env["retirement"]["consulted"])
        self.assertTrue(env["retirement"]["accepted"])
        self.assertEqual(len(env["gaps"]), 1)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def refusal_case(self, marker_path, expect_sub):
        log = self.write_log(gap_fixture_rows())
        state = self.write_state()
        args = ["--log", log, "--state", state, "--now", GAP_NOW,
                "--retirement-file", marker_path]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "stale")
        self.assertEqual(j_rc, 1)
        self.assertTrue(env["retirement"]["consulted"])
        self.assertFalse(env["retirement"]["accepted"])
        self.assertIn(expect_sub, env["retirement"]["detail"])
        self.assertIn(expect_sub, t_err)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_stale_missing_marker_file_json_stale(self):
        self.refusal_case(os.path.join(self.tmp.name, "absent.json"),
                          "retirement file missing")

    def test_stale_malformed_marker_json_stale(self):
        self.refusal_case(self.write_marker("not json{"), "unreadable")

    def test_stale_marker_array_json_stale(self):
        self.refusal_case(self.write_marker("[1, 2]"), "not an object")

    def test_stale_marker_retired_false_json_stale(self):
        self.refusal_case(self.valid_marker(retired=False),
                          "retired is not true")

    def test_stale_marker_non_boolean_retired_json_stale(self):
        for value in (1, "true"):
            with self.subTest(retired=value):
                log = self.write_log(gap_fixture_rows())
                state = self.write_state()
                marker = self.valid_marker(retired=value)
                args = ["--log", log, "--state", state, "--now", GAP_NOW,
                        "--retirement-file", marker]
                t_rc, t_out, t_err = self.run_text(*args)
                j_rc, j_out, j_err, env = self.run_json(*args)
                env = self.assertSingleJsonStdout(j_out)
                self.assertEnvelopeShape(env, threshold=10.0)
                self.assertEqual(env["verdict"], "stale")
                self.assertIn("retired is not true",
                              env["retirement"]["detail"])
                self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_stale_marker_required_field_matrix_json_stale(self):
        base = {
            "retired": True,
            "producer": "fleet-quota-balancer",
            "successor": "native-routing",
            "approved_by": "test-approver",
            "approved_at": "2026-10-04T17:40:00+00:00",
        }
        for field in ("producer", "successor", "approved_by", "approved_at"):
            for shape in ("missing", "blank", "non-string"):
                with self.subTest(field=field, shape=shape):
                    marker = dict(base)
                    if shape == "missing":
                        del marker[field]
                    elif shape == "blank":
                        marker[field] = "   "
                    else:
                        marker[field] = 12345
                    log = self.write_log(gap_fixture_rows())
                    state = self.write_state()
                    args = ["--log", log, "--state", state, "--now", GAP_NOW,
                            "--retirement-file", self.write_marker(marker)]
                    t_rc, t_out, t_err = self.run_text(*args)
                    j_rc, j_out, j_err, env = self.run_json(*args)
                    env = self.assertSingleJsonStdout(j_out)
                    self.assertEnvelopeShape(env, threshold=10.0)
                    self.assertEqual(env["verdict"], "stale")
                    self.assertIn(field, env["retirement"]["detail"])
                    self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_stale_marker_future_approval_json_stale(self):
        self.refusal_case(
            self.valid_marker(approved_at="2026-10-04T18:40:00+00:00"),
            "in the future")

    def test_stale_marker_other_producer_json_stale(self):
        self.refusal_case(self.valid_marker(producer="other-writer"),
                          "mismatch")

    def test_stale_marker_unparseable_approval_json_stale(self):
        self.refusal_case(self.valid_marker(approved_at="someday"),
                          "unparseable")

    def test_fresh_with_valid_marker_json_fresh_not_consulted(self):
        log = self.write_log(synthetic_rows(BASE_NOW))
        state = self.write_state()
        marker = self.valid_marker()
        args = ["--log", log, "--state", state, "--now", BASE_NOW.isoformat(),
                "--retirement-file", marker]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "fresh")
        self.assertFalse(env["retirement"]["consulted"])
        self.assertFalse(env["retirement"]["accepted"])
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_unmeasurable_with_valid_marker_json_not_consulted(self):
        state = self.write_state()
        marker = self.valid_marker()
        args = ["--log", os.path.join(self.tmp.name, "absent.log"),
                "--state", state,
                "--retirement-file", marker]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "unmeasurable")
        self.assertEqual(env["error"]["code"], "log_missing")
        self.assertFalse(env["retirement"]["consulted"])
        self.assertParity(t_rc, t_out, t_err, j_rc, env)


class JsonUnmeasurable(JsonCase):
    def test_missing_log_json_unmeasurable(self):
        state = self.write_state()
        args = ["--log", os.path.join(self.tmp.name, "absent.log"),
                "--state", state, "--now", BASE_NOW.isoformat()]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "unmeasurable")
        self.assertEqual(j_rc, 2)
        self.assertEqual(env["error"]["code"], "log_missing")
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_empty_log_json_unmeasurable(self):
        log = self.write_log("")
        state = self.write_state()
        args = ["--log", log, "--state", state, "--now", BASE_NOW.isoformat()]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "unmeasurable")
        self.assertEqual(env["error"]["code"], "log_no_parseable_row")
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_garbage_log_json_unmeasurable(self):
        log = self.write_log("not json\n{no at field}\n[1,2\n")
        state = self.write_state()
        args = ["--log", log, "--state", state, "--now", BASE_NOW.isoformat()]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "unmeasurable")
        self.assertEqual(env["error"]["code"], "log_no_parseable_row")
        self.assertEqual(env["skipped_lines"], 3)
        self.assertNotIn(SENTINEL, j_out)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_bad_now_json_unmeasurable(self):
        log = self.write_log(synthetic_rows(BASE_NOW))
        state = self.write_state()
        args = ["--log", log, "--state", state, "--now", "not-a-time"]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "unmeasurable")
        self.assertEqual(j_rc, 2)
        self.assertEqual(env["error"]["code"], "bad_now")
        self.assertIsNone(env["now"])
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_bad_max_age_min_json_unmeasurable(self):
        log = self.write_log(synthetic_rows(BASE_NOW))
        state = self.write_state()
        for bad in ("0", "-5"):
            with self.subTest(bad=bad):
                args = ["--log", log, "--state", state,
                        "--now", BASE_NOW.isoformat(),
                        "--max-age-min", bad]
                t_rc, t_out, t_err = self.run_text(*args)
                j_rc, j_out, j_err, env = self.run_json(*args)
                env = self.assertSingleJsonStdout(j_out)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "unmeasurable")
                self.assertEqual(env["error"]["code"], "bad_max_age_min")
                self.assertEqual(env["threshold_min"], float(bad))
                self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_bad_window_h_json_unmeasurable(self):
        log = self.write_log(synthetic_rows(BASE_NOW))
        state = self.write_state()
        for bad in ("0", "-5"):
            with self.subTest(bad=bad):
                args = ["--log", log, "--state", state,
                        "--now", BASE_NOW.isoformat(),
                        "--window-h", bad]
                t_rc, t_out, t_err = self.run_text(*args)
                j_rc, j_out, j_err, env = self.run_json(*args)
                env = self.assertSingleJsonStdout(j_out)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "unmeasurable")
                self.assertEqual(env["error"]["code"], "bad_window_h")
                self.assertEqual(env["window_h"], float(bad))
                self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_now_before_all_rows_json_unmeasurable(self):
        log = self.write_log(gap_fixture_rows())
        state = self.write_state()
        args = ["--log", log, "--state", state,
                "--now", "2026-10-04T14:00:00+00:00"]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "unmeasurable")
        self.assertEqual(env["error"]["code"], "log_no_row_before_now")
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_mid_gap_replay_json_excludes_future(self):
        log = self.write_log(gap_fixture_rows())
        state = self.write_state()
        replay_now = "2026-10-04T16:00:00+00:00"
        args = ["--log", log, "--state", state, "--now", replay_now]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "stale")
        self.assertNotIn("17:28", j_out)
        self.assertIn("2026-10-04T14:14", env["last_row"]["at"])
        self.assertEqual(env["gaps"], [])
        self.assertParity(t_rc, t_out, t_err, j_rc, env)


class JsonGapsBounded(JsonCase):
    def test_multi_gap_over_cap_json_bounded(self):
        base = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(16):
            stamp = (base + timedelta(minutes=i * 20)).isoformat()
            rows.append('{"at": "%s", "model": "model-%d", "reason": "reason-%d"}'
                        % (stamp, i, i))
        log = self.write_log("\n".join(rows) + "\n")
        state = self.write_state()
        last = base + timedelta(minutes=15 * 20)
        now = (last + timedelta(minutes=15)).isoformat()
        args = ["--log", log, "--state", state, "--now", now,
                "--max-age-min", "10", "--window-h", "24"]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(env["verdict"], "stale")
        self.assertEqual(len(env["gaps"]), GAP_CAP)
        self.assertEqual(env["gaps_truncated"], 15 - GAP_CAP)
        for gap in env["gaps"]:
            self.assertAlmostEqual(gap["duration_min"], 20.0, delta=0.01)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_window_excludes_old_gaps_json(self):
        # Gaps count when they END in-window (see GAPS in the header).
        # now=20:35 with window=2h gives window_start=18:35. The
        # 00:00->00:30 gap ends before 18:35, so it is excluded. The
        # bridge gap 00:30->20:00 starts before the window but ends at
        # 20:00, inside it, so it counts exactly as text mode reports it;
        # a start-in-window rule would wrongly drop it. The recent
        # 20:00->20:30 gap also counts: 2 gaps total, matching text parity.
        base = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        old_left = base.isoformat()
        old_right = (base + timedelta(minutes=30)).isoformat()
        recent_base = base + timedelta(hours=20)
        recent_left = recent_base.isoformat()
        recent_right = (recent_base + timedelta(minutes=30)).isoformat()
        rows = [
            '{"at": "%s", "model": "m", "reason": "r"}' % old_left,
            '{"at": "%s", "model": "m", "reason": "r"}' % old_right,
            '{"at": "%s", "model": "m", "reason": "r"}' % recent_left,
            '{"at": "%s", "model": "m", "reason": "r"}' % recent_right,
        ]
        log = self.write_log("\n".join(rows) + "\n")
        state = self.write_state()
        now = (recent_base + timedelta(minutes=35)).isoformat()
        args = ["--log", log, "--state", state, "--now", now,
                "--max-age-min", "10", "--window-h", "2"]
        t_rc, t_out, t_err = self.run_text(*args)
        j_rc, j_out, j_err, env = self.run_json(*args)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env, threshold=10.0)
        self.assertEqual(len(env["gaps"]), 2)
        self.assertEqual(env["gaps_truncated"], 0)
        bridge, recent = env["gaps"]
        self.assertEqual(guard.parse_instant(bridge["start"]),
                         guard.parse_instant(old_right))
        self.assertEqual(guard.parse_instant(bridge["end"]),
                         guard.parse_instant(recent_left))
        self.assertAlmostEqual(bridge["duration_min"], 1170.0, delta=0.01)
        self.assertEqual(guard.parse_instant(recent["start"]),
                         guard.parse_instant(recent_left))
        self.assertEqual(guard.parse_instant(recent["end"]),
                         guard.parse_instant(recent_right))
        self.assertAlmostEqual(recent["duration_min"], 30.0, delta=0.01)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)


class JsonNoEcho(JsonCase):
    def test_json_carries_no_log_body(self):
        extra = ', "payload": "%s", "changed": 0, "notes": "full log line"' % SENTINEL
        log = self.write_log(synthetic_rows(BASE_NOW, extra=extra))
        state = self.write_state()
        now = BASE_NOW.isoformat()
        t_rc, t_out, t_err = self.run_text("--log", log, "--state", state,
                                           "--now", now)
        j_rc, j_out, j_err, env = self.run_json("--log", log, "--state", state,
                                               "--now", now)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertNotIn(SENTINEL, j_out)
        self.assertNotIn("SENTINEL", j_out)
        self.assertNotIn("payload", j_out)
        self.assertNotIn("full log line", j_out)
        with open(log, "r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
        self.assertNotIn(first_line, j_out)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)

    def test_json_stdout_is_single_document_on_stale(self):
        log = self.write_log(gap_fixture_rows())
        state = self.write_state()
        t_rc, t_out, t_err = self.run_text("--log", log, "--state", state,
                                           "--now", GAP_NOW)
        j_rc, j_out, j_err, env = self.run_json("--log", log, "--state", state,
                                               "--now", GAP_NOW)
        env = self.assertSingleJsonStdout(j_out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "stale")
        self.assertNotIn(SENTINEL, j_out)
        self.assertParity(t_rc, t_out, t_err, j_rc, env)


class TextControls(JsonCase):
    """Positive controls: existing text behavior still works on the base.

    These run WITHOUT --json and MUST pass both before and after the build.
    They prove the new suite did not break the pinned text contract.
    """

    def test_text_fresh_control(self):
        log = self.write_log(synthetic_rows(BASE_NOW))
        rc, out, err = self.run_text("--log", log, "--state", self.write_state(),
                                     "--now", BASE_NOW.isoformat())
        self.assertEqual(rc, 0)
        self.assertIn("FRESH", out + err)

    def test_text_stale_with_gap_control(self):
        log = self.write_log(gap_fixture_rows())
        rc, out, err = self.run_text("--log", log, "--state", self.write_state(),
                                     "--now", GAP_NOW)
        self.assertEqual(rc, 1)
        self.assertIn("STALE", out + err)
        self.assertIn("GAP 193", out + err)

    def test_text_retired_control(self):
        log = self.write_log(gap_fixture_rows())
        rc, out, err = self.run_text("--log", log, "--state", self.write_state(),
                                     "--now", GAP_NOW,
                                     "--retirement-file", self.valid_marker())
        self.assertEqual(rc, 0)
        self.assertIn("RETIRED", out + err)

    def test_text_unmeasurable_missing_control(self):
        rc, out, err = self.run_text(
            "--log", os.path.join(self.tmp.name, "absent.log"),
            "--state", self.write_state())
        self.assertEqual(rc, 2)
        self.assertIn("UNMEASURABLE", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
