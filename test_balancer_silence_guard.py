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
import os
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
