#!/usr/bin/env python3
"""Tests for balancer_silence_guard.py (case_7edd6ab6).

Every contract carries a positive control: a world in which the guard MUST
alarm (exit 1) or refuse (exit 2). A gate that has gone vacuous fails here
instead of passing quietly. Offline: the log is a temp file, --now is
injected, no host, no credentials.

The GAP_ROWS fixture is cut verbatim from the live
fleet-quota-balancer.log around the 2026-10-04 14:14:31Z -> 17:28:14Z
silence (193 min): four 1-minute rows, the gap, then three 1-minute rows.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import balancer_silence_guard as guard

# Verbatim rows from /paperclip/shared/host-bridge/fleet-quota-balancer.log.
GAP_ROWS = """\
{"at": "2026-10-04T14:11:11.431481+00:00", "model": "gpt-6.1-sol", "reason": "muse_collective_95", "changed": 0}
{"at": "2026-10-04T14:12:20.831632+00:00", "model": "gpt-6.1-sol", "reason": "muse_collective_95", "changed": 0}
{"at": "2026-10-04T14:13:30.860643+00:00", "model": "gpt-6.1-sol", "reason": "muse_collective_95", "changed": 0}
{"at": "2026-10-04T14:14:31.672897+00:00", "model": "gpt-6.1-sol", "reason": "muse_collective_95", "changed": 0}
{"at": "2026-10-04T17:28:14.425776+00:00", "model": "muse-spark-1.3-contributor(xhigh)", "reason": "meta_weekly_below_98", "meta_combined": 0.955, "claude_5h_combined": 53.5, "agents_changed": 26, "cards_repinned": 20}
{"at": "2026-10-04T17:29:20.794090+00:00", "model": "muse-spark-1.3-contributor(xhigh)", "reason": "meta_weekly_below_98", "meta_combined": 0.955, "claude_5h_combined": 53.5, "agents_changed": 0, "cards_repinned": 0}
{"at": "2026-10-04T17:30:21.028250+00:00", "model": "muse-spark-1.3-contributor(xhigh)", "reason": "meta_weekly_below_98", "meta_combined": 0.955, "claude_5h_combined": 53.5, "agents_changed": 0, "cards_repinned": 19}
"""

AFTER_GAP_NOW = "2026-10-04T17:45:00+00:00"  # ~15 min after the last row


def healthy_rows(end, count=30, step_min=1):
    lines = []
    for n in range(count, 0, -1):
        stamp = (end - timedelta(minutes=n)).isoformat()
        lines.append(
            '{"at": "%s", "model": "muse-spark-1.3-contributor(xhigh)", '
            '"reason": "meta_weekly_below_98", "changed": 0}' % stamp)
    lines.append(
        '{"at": "%s", "model": "muse-spark-1.3-contributor(xhigh)", '
        '"reason": "meta_weekly_below_98", "changed": 0}' % end.isoformat())
    return "\n".join(lines) + "\n"


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = io.StringIO()
        self.err = io.StringIO()

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

    def run_guard(self, *argv):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return guard.main(list(argv))

    def all_output(self):
        return self.out.getvalue() + self.err.getvalue()


class Silence(GuardCase):
    def test_replay_of_the_193_min_gap_alarms(self):
        """Positive control: the real silence must exit 1 and name the gap."""
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW)
        self.assertEqual(rc, 1)
        output = self.all_output()
        self.assertIn("GAP 193", output)
        self.assertIn("2026-10-04T14:14:31", output)
        self.assertIn("2026-10-04T17:28:14", output)
        self.assertIn("gpt-6.1-sol", output)
        self.assertIn("muse_collective_95", output)
        self.assertIn("muse-spark-1.3-contributor(xhigh)", output)
        self.assertIn("meta_weekly_below_98", output)
        self.assertIn("STALE", output)

    def test_mid_gap_replay_alarms_on_the_stale_tail(self):
        """--now inside the silence: rows stamped after it do not exist yet."""
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", "2026-10-04T16:00:00+00:00")
        self.assertEqual(rc, 1)
        output = self.all_output()
        self.assertIn("STALE", output)
        self.assertIn("2026-10-04T14:14:31", output)
        self.assertNotIn("17:28:14", output)  # the future row is excluded

    def test_healthy_minute_cadence_is_fresh(self):
        end = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log(healthy_rows(end))
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", end.isoformat())
        self.assertEqual(rc, 0)
        self.assertIn("FRESH", self.all_output())
        self.assertNotIn("GAP", self.all_output())

    def test_age_exactly_at_threshold_is_fresh(self):
        """Strict `>`: age == threshold is not stale. Kills the `>=` mutant."""
        last = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log(healthy_rows(last, count=2))
        now = (last + timedelta(minutes=10)).isoformat()
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", now, "--max-age-min", "10")
        self.assertEqual(rc, 0)
        self.assertIn("FRESH", self.all_output())

    def test_gap_exactly_at_threshold_is_not_reported(self):
        """Strict `>`: a gap == threshold is not a gap. Kills the `>=` mutant."""
        base = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        lines = [
            '{"at": "%s", "model": "m", "reason": "r"}' % base.isoformat(),
            '{"at": "%s", "model": "m", "reason": "r"}'
            % (base + timedelta(minutes=10)).isoformat(),
        ]
        log = self.write_log("\n".join(lines) + "\n")
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", (base + timedelta(minutes=11)).isoformat(),
                            "--max-age-min", "10")
        self.assertEqual(rc, 0)
        self.assertNotIn("GAP", self.all_output())

    def test_custom_threshold_respected(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW, "--max-age-min", "200")
        self.assertEqual(rc, 0)
        output = self.all_output()
        self.assertNotIn("GAP", output)  # 193 min < 200 min threshold


class Unmeasurable(GuardCase):
    def test_missing_log_is_unmeasurable(self):
        rc = self.run_guard("--log", os.path.join(self.tmp.name, "absent.log"),
                            "--state", self.write_state())
        self.assertEqual(rc, 2)
        self.assertIn("UNMEASURABLE", self.err.getvalue())

    def test_empty_log_is_unmeasurable(self):
        rc = self.run_guard("--log", self.write_log(""),
                            "--state", self.write_state())
        self.assertEqual(rc, 2)
        self.assertIn("UNMEASURABLE", self.err.getvalue())

    def test_garbage_log_is_unmeasurable(self):
        rc = self.run_guard(
            "--log", self.write_log("not json\n{no at field}\n[1,2\n"),
            "--state", self.write_state())
        self.assertEqual(rc, 2)
        self.assertIn("UNMEASURABLE", self.err.getvalue())

    def test_blank_lines_are_ignored_not_fatal(self):
        end = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log("\n" + healthy_rows(end) + "\n\n")
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", end.isoformat())
        self.assertEqual(rc, 0)

    def test_garbage_lines_among_good_rows_warn_but_measure(self):
        """Positive control: stray garbage must not silence a live log."""
        end = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log("GARBAGE\n" + healthy_rows(end))
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", end.isoformat())
        self.assertEqual(rc, 0)
        self.assertIn("skipped 1", self.err.getvalue())

    def test_bad_now_is_unmeasurable(self):
        end = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log(healthy_rows(end))
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", "not-a-time")
        self.assertEqual(rc, 2)

    def test_bad_threshold_is_unmeasurable(self):
        end = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log(healthy_rows(end))
        for bad in ("0", "-5"):
            with self.subTest(bad=bad):
                self.out.truncate(0)
                self.err.truncate(0)
                rc = self.run_guard("--log", log,
                                    "--state", self.write_state(),
                                    "--now", end.isoformat(),
                                    "--max-age-min", bad)
                self.assertEqual(rc, 2)

    def test_now_before_all_rows_is_unmeasurable(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", "2026-10-04T14:00:00+00:00")
        self.assertEqual(rc, 2)

    def test_missing_state_file_does_not_change_the_verdict(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log,
                            "--state", os.path.join(self.tmp.name, "absent.json"),
                            "--now", AFTER_GAP_NOW)
        self.assertEqual(rc, 1)


class Retirement(GuardCase):
    """Explicit retirement excuses quantified silence, never blindness.

    Every rejection below is a positive control for fail-closed: a bad
    file must leave the stale verdict standing, not mask it and not
    crash the guard.
    """

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
            "evidence": "synthetic-fixture",
        }
        marker.update(overrides)
        return self.write_marker(marker)

    def test_stale_log_with_valid_marker_is_retired(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file", self.valid_marker())
        self.assertEqual(rc, 0)
        output = self.all_output()
        self.assertIn("RETIRED", output)
        self.assertIn("explicitly retired", output)
        self.assertIn("GAP 193", output)  # the audit trail is preserved
        self.assertIn("retirement: producer=fleet-quota-balancer", output)
        self.assertNotIn("STALE", output)

    def test_stale_log_with_missing_marker_file_still_alarms(self):
        """The flag names a file that is absent: no evidence, still stale."""
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file",
                            os.path.join(self.tmp.name, "absent.json"))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("retirement file missing", self.err.getvalue())
        self.assertIn("retirement ignored", self.err.getvalue())

    def test_marker_with_retired_false_is_ignored(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file",
                            self.valid_marker(retired=False))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("retired is not true", self.err.getvalue())

    def test_malformed_marker_is_ignored(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file", self.write_marker("not json{"))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("unreadable", self.err.getvalue())

    def test_marker_array_is_ignored(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file", self.write_marker("[1, 2]"))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("not an object", self.err.getvalue())

    def test_marker_without_optional_evidence_is_retired(self):
        """`evidence` is recommended but not required: absence still retires."""
        log = self.write_log(GAP_ROWS)
        marker = {
            "retired": True,
            "producer": "fleet-quota-balancer",
            "successor": "native-routing",
            "approved_by": "test-approver",
            "approved_at": "2026-10-04T17:40:00+00:00",
        }
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file", self.write_marker(marker))
        self.assertEqual(rc, 0)
        self.assertIn("RETIRED", self.all_output())

    def test_marker_missing_required_field_is_ignored(self):
        log = self.write_log(GAP_ROWS)
        marker = {
            "retired": True,
            "producer": "fleet-quota-balancer",
            "successor": "native-routing",
            "approved_at": "2026-10-04T17:40:00+00:00",
        }  # approved_by deliberately absent
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file", self.write_marker(marker))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("approved_by", self.err.getvalue())

    def test_marker_with_future_approval_is_ignored(self):
        """An approval stamped after --now did not exist at replay time."""
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file", self.valid_marker(
                                approved_at="2026-10-04T18:40:00+00:00"))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("in the future", self.err.getvalue())

    def test_marker_for_other_producer_is_ignored(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file",
                            self.valid_marker(producer="other-writer"))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("mismatch", self.err.getvalue())

    def test_marker_with_unparseable_approval_is_ignored(self):
        log = self.write_log(GAP_ROWS)
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", AFTER_GAP_NOW,
                            "--retirement-file",
                            self.valid_marker(approved_at="someday"))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", self.all_output())
        self.assertIn("unparseable", self.err.getvalue())

    def test_live_log_with_valid_marker_stays_fresh(self):
        """The marker excuses silence; a live feed needs no excuse."""
        end = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
        log = self.write_log(healthy_rows(end))
        rc = self.run_guard("--log", log, "--state", self.write_state(),
                            "--now", end.isoformat(),
                            "--retirement-file", self.valid_marker())
        self.assertEqual(rc, 0)
        output = self.all_output()
        self.assertIn("FRESH", output)
        self.assertNotIn("RETIRED", output)

    def test_missing_log_with_valid_marker_stays_unmeasurable(self):
        """Blindness is never retired: the marker needs a quantified tail."""
        rc = self.run_guard("--log", os.path.join(self.tmp.name, "absent.log"),
                            "--state", self.write_state(),
                            "--retirement-file", self.valid_marker())
        self.assertEqual(rc, 2)
        self.assertIn("UNMEASURABLE", self.err.getvalue())
        self.assertNotIn("RETIRED", self.all_output())


class NoProducerWrite(unittest.TestCase):
    """Static pin: the guard cannot re-enable, rewrite or reroute anything.

    Retirement evidence flows one way (file in, verdict out). A guard that
    could restart the producer, write a model default or mutate a roster
    could turn a retirement marker into a resurrection; this test fails if
    any control, write-mode or network surface ever lands in the source.
    """

    FORBIDDEN_TOKENS = (
        "systemctl", "subprocess", "os.system", "os.exec", "os.remove",
        "os.unlink", "os.rename", "os.replace", "shutil", "socket", "urllib",
        "http.client", "requests", "pathlib",
    )

    def test_guard_source_has_no_write_or_control_surface(self):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "balancer_silence_guard.py")
        with open(src, "r", encoding="utf-8") as handle:
            text = handle.read()
        for token in self.FORBIDDEN_TOKENS:
            self.assertNotIn(token, text,
                             f"guard source must not contain {token!r}")
        for match in re.finditer(r"open\s*\([^)]*\)", text):
            call = match.group(0)
            self.assertNotRegex(
                call, r"""['"][^'"]*[wax+][^'"]*['"]""",
                f"guard must only open files read-only: {call}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
