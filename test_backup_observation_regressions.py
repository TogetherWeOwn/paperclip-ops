"""Supplemental immutable offline regressions for the backup observer.

Discovery (run from the repository root):
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_backup_observation_regressions
Combined contract:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_backup_observation_contract test_backup_observation_regressions

Uses the original contract's real entrypoint, sanitized environment, write/socket
fences, snapshots and bounded subprocess runner. Every invocation is --no-health.
Only syscall/command boundaries are faulted: date advances after startup; find
can emit a real fixture path followed by failure. Neither seam supplies verdicts.
The audit file is the only writable state and proves invalid numeric input is
refused before any observation. No workflow, source, pins or live paths change.
"""

import json
import os
import unittest

import test_backup_observation_contract as contract


MODES = ((), ("--staleness-only",))
UNKNOWN = "paperclip-unknown.sql.gz"
NUMERIC_OPTIONS = ("--window-bytes", "--orphan-min-age-minutes",
                   "--max-backup-age-hours", "--min-backup-bytes")

# Extend the original boundary adapters, not the detector. Counting clock reads
# in the already-permitted audit file avoids another writable state exception.
ADAPTER = contract.ADAPTER.replace(
    'if name == "date" and args == ["-u", "+%s"]:\n'
    '    print(1800000000)\n'
    '    sys.exit(0)\n',
    '''audit("invoked")
if name == "date" and args == ["-u", "+%s"]:
    audit("clock")
    reads = sum(json.loads(line)["kind"] == "clock"
                for line in pathlib.Path("audit").read_text().splitlines())
    print(1800000000 if reads == 1 else 1800000000 + cfg.get("clock_advance", 0))
    sys.exit(0)
if name == "find" and cfg.get("partial_entry"):
    if "./archives" not in args or "-print0" not in args:
        audit("blocked")
        sys.exit(97)
    audit("injected")
    sys.stdout.buffer.write(os.fsencode(cfg["partial_entry"]) + b"\\0")
    sys.stdout.buffer.flush()
    sys.stderr.write("find: synthetic partial enumeration failure\\n")
    sys.exit(1)
''')


class BackupObservationRegressions(unittest.TestCase):
    # Reuse helpers without inheriting/discovering the original tests twice.
    cleanup = contract.BackupObservationContract.cleanup
    write = contract.BackupObservationContract.write
    archive = contract.BackupObservationContract.archive
    snapshot = contract.BackupObservationContract.snapshot
    verdict = contract.BackupObservationContract.verdict
    machine = contract.BackupObservationContract.machine

    def setUp(self):
        contract.BackupObservationContract.setUp(self)
        self.write("adapter.py", ADAPTER).chmod(0o755)

    def raw(self, *args, **kwargs):
        return contract.BackupObservationContract.invoke(
            self, *args, json_output=False, **kwargs)

    def invoke(self, *args, **kwargs):
        rc, text = self.raw(*args, "--json", **kwargs)
        try:
            data = json.loads(text)  # strict control-byte and single-object check
        except ValueError:
            self.fail(f"Expected strict observer JSON: exit={rc}, output={text!r}")
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("exitCode"), rc, data)
        self.assertEqual(data.get("healthStatus"), "not_checked", data)
        return rc, data

    def events(self):
        return [json.loads(line) for line in (self.root / "audit").read_text().splitlines()]

    def cwd_archive(self):
        return self.write(contract.FRESH, "x" * 4096)

    def usage_error(self, option, value, *mode):
        rc, text = self.raw(*mode, option, value, "--json")
        self.assertEqual(rc, 2, text)
        self.assertEqual(self.events(), [],
                         f"Invalid {option}={value} reached observation: {text}")
        self.assertTrue(text.strip(), "Usage errors need an explanation")
        self.assertNotIn('"verdict"', text, "Usage failure is not an observation")

    def test_empty_directory_never_substitutes_cwd_with_fresh_archive(self):
        # Catches normalization of an empty argument into ./.
        self.cwd_archive()
        for mode in MODES:
            with self.subTest(mode=mode):
                data = self.verdict(self.invoke(*mode, "--backup-dir", ""), 2, "inconclusive")
                self.machine(data, "unknown", "directory_unmeasured")
                self.assertEqual(data["newestUsableBackup"], "none")
                self.assertFalse(any(e["command"] == "find" for e in self.events()))

    def test_empty_directory_does_not_fabricate_measured_empty_stall(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                data = self.verdict(self.invoke(*mode, "--backup-dir", ""), 2, "inconclusive")
                self.machine(data, "unknown", "directory_unmeasured")
                self.assertFalse(any(e["command"] == "find" for e in self.events()))

    def test_empty_directory_preserves_independent_measured_suppression(self):
        self.cwd_archive()
        self.write("server.log", contract.COMPLETE + contract.SKIP)
        data = self.verdict(self.invoke("--backup-dir", ""), 1, "stall")
        signals = self.machine(data, "unknown", "directory_unmeasured")
        self.assertEqual(signals["suppression"]["state"], "measured")
        self.assertEqual(data["newestUsableBackup"], "none")
        self.assertTrue(any("SUPPRESSION" in r for r in data["reasons"]), data)
        self.assertFalse(any(e["command"] == "find" for e in self.events()))

    def test_explicit_dot_remains_a_measured_fresh_directory(self):
        self.cwd_archive()
        for mode in MODES:
            with self.subTest(mode=mode):
                data = self.verdict(self.invoke(*mode, "--backup-dir", "."), 0, "ok")
                self.machine(data)
                self.assertEqual(data["newestUsableBackup"], contract.FRESH)
                self.assertEqual(data["newestUsableBackupAgeMinutes"], 0)

    def test_explicit_dot_remains_a_measured_empty_directory(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                data = self.verdict(self.invoke(*mode, "--backup-dir", "."), 1, "stall")
                self.machine(data)
                self.assertTrue(any("NO USABLE BACKUP" in r for r in data["reasons"]), data)

    def reject_long_numeric_option(self, option):
        # Includes 19 digits that fit int64: this is a decimal-width guard too.
        self.archive(size=20)
        for value in ("1000000000000000000", "9999999999999999999",
                      "18446744073709551616", "9" * 80):
            for mode in MODES:
                with self.subTest(option=option, value=value, mode=mode):
                    self.usage_error(option, value, *mode)

    def test_window_rejects_19_plus_digits_before_observation(self):
        self.reject_long_numeric_option("--window-bytes")

    def test_orphan_minutes_reject_19_plus_digits_before_observation(self):
        self.reject_long_numeric_option("--orphan-min-age-minutes")

    def test_backup_hours_reject_19_plus_digits_before_observation(self):
        self.reject_long_numeric_option("--max-backup-age-hours")

    def test_minimum_bytes_reject_19_plus_digits_before_observation(self):
        self.reject_long_numeric_option("--min-backup-bytes")

    def test_minute_conversion_rejects_int64_overflow_within_18_digits(self):
        self.archive()
        for value in ("153722867280912931", "999999999999999999"):
            for mode in MODES:
                with self.subTest(value=value, mode=mode):
                    self.usage_error("--orphan-min-age-minutes", value, *mode)

    def test_hour_conversion_rejects_int64_overflow_within_18_digits(self):
        self.archive()
        for value in ("2562047788015216", "99999999999999999", "999999999999999999"):
            for mode in MODES:
                with self.subTest(value=value, mode=mode):
                    self.usage_error("--max-backup-age-hours", value, *mode)

    def test_safe_numeric_upper_bounds_are_still_observations(self):
        self.archive()
        cases = (("--window-bytes", "999999999999999999", 0, "ok"),
                 ("--min-backup-bytes", "999999999999999999", 1, "stall"),
                 ("--orphan-min-age-minutes", "153722867280912930", 0, "ok"),
                 ("--max-backup-age-hours", "2562047788015215", 0, "ok"))
        for option, value, rc, verdict in cases:
            for mode in MODES:
                with self.subTest(option=option, value=value, mode=mode):
                    data = self.verdict(self.invoke(*mode, option, value), rc, verdict)
                    self.machine(data)

    def test_small_positive_numeric_controls_are_not_usage_errors(self):
        self.archive()
        for option in NUMERIC_OPTIONS:
            with self.subTest(option=option):
                rc, verdict = (2, "inconclusive") if option == "--window-bytes" else (0, "ok")
                self.verdict(self.invoke(option, "1"), rc, verdict)

    def test_default_thresholds_still_reject_tiny_and_stale_archives(self):
        self.archive(size=20)
        data = self.verdict(self.invoke(), 1, "stall")
        self.assertEqual(data["minBackupBytes"], 1024)
        self.assertEqual(data["maxBackupAgeHours"], 2)
        self.archive(size=1024, age=120)
        self.verdict(self.invoke(), 0, "ok")
        self.archive(size=1024, age=121)
        self.verdict(self.invoke(), 1, "stall")

    def test_archive_written_between_clocks_uses_post_stat_age(self):
        # Startup T; archive T+60; post-stat T+3660 => age exactly 60 minutes.
        self.config["clock_advance"] = 3660
        self.archive(age=-1)
        for mode in MODES:
            with self.subTest(mode=mode):
                data = self.verdict(self.invoke(*mode), 0, "ok")
                self.machine(data)
                self.assertEqual(data["newestUsableBackup"], contract.FRESH)
                self.assertEqual(data["newestUsableBackupAgeMinutes"], 60)
                self.assertNotIn("mtime_future", data["codes"])

    def test_log_written_between_clocks_is_not_future_unknown(self):
        self.config["clock_advance"] = 3660
        self.archive()
        log = self.write("server.log", contract.COMPLETE)
        os.utime(log, (contract.NOW + 60, contract.NOW + 60))
        data = self.verdict(self.invoke(), 0, "ok")
        self.machine(data, "measured", signal_id="suppression")
        self.assertTrue(data["windowAnchored"])
        self.assertFalse(data["logStale"])
        self.assertNotIn("mtime_future", data["codes"])

    def test_orphan_crosses_age_cutoff_at_post_stat_clock(self):
        # 60 minutes at startup; 91 after stat, crossing the default 90 cutoff.
        self.config["clock_advance"] = 1860
        self.archive()
        self.archive(contract.ORPHAN, age=60, size=2048)
        data = self.verdict(self.invoke(), 1, "stall")
        self.assertEqual(data["orphanCount"], 1)
        self.machine(data, "measured", signal_id="orphan")
        self.assertTrue(any(f"{contract.ORPHAN} is 91 minutes old with no .sql.gz sibling (2048 bytes)" in r
                            for r in data["reasons"]), data)
        data = self.verdict(self.invoke("--orphan-min-age-minutes", "92"), 0, "ok")
        self.assertEqual(data["orphanCount"], 0)

    def test_stale_archive_beside_unknown_candidate_cannot_prove_global_staleness(self):
        # Catches ignoring selected_unknown just because a stale usable file exists.
        self.archive(age=180)
        self.archive(UNKNOWN, age=5)
        for mode in MODES:
            with self.subTest(mode=mode):
                data = self.verdict(self.invoke(*mode, fault="size_failed", target=UNKNOWN),
                                    2, "inconclusive")
                self.machine(data, "unknown", "stat_failed")
                self.assertEqual(data["newestUsableBackup"], contract.FRESH)
                self.assertEqual(data["newestUsableBackupAgeMinutes"], 180)
                self.assertNotIn("archive_stale", data["signals"]["freshness"]["codes"])

    def test_fresh_independent_archive_survives_unknown_selected_candidate(self):
        self.archive()
        self.archive(UNKNOWN, age=5)
        for mode in MODES:
            with self.subTest(mode=mode):
                data = self.verdict(self.invoke(*mode, fault="size_failed", target=UNKNOWN), 0, "ok")
                self.machine(data, "measured", "stat_failed")
                self.assertEqual(data["newestUsableBackup"], contract.FRESH)
                self.assertEqual(data["newestUsableBackupAgeMinutes"], 30)

    def test_positive_orphan_survives_partial_failed_enumeration(self):
        # Complete-scan-only orphan detection would drop this independent evidence.
        self.archive(contract.ORPHAN, age=180, size=2048)
        self.config["partial_entry"] = "./archives/" + contract.ORPHAN
        data = self.verdict(self.invoke(), 1, "stall")
        self.assertEqual(data["orphanCount"], 1)
        signals = self.machine(data, "unknown", "directory_unmeasured")
        # The scan remains incomplete even though this one positive finding is usable.
        self.assertIn("directory_unmeasured", signals["orphan"]["codes"])
        self.assertIn("orphan_dump", signals["orphan"]["codes"])
        self.assertTrue(any(f"{contract.ORPHAN} is 180 minutes old with no .sql.gz sibling (2048 bytes)" in r
                            for r in data["reasons"]), data)
        self.assertTrue(any(e["command"] == "find" and e["kind"] == "injected"
                            for e in self.events()), "Partial enumeration seam was not exercised")

    def test_pipe_and_control_byte_names_round_trip_with_ages_and_sizes(self):
        # Catches both pipe-delimited orphan records and tab-only JSON escaping.
        fresh = "paperclip-fresh|control\x01.sql.gz"
        orphan = "paperclip-orphan|control\x01.sql"
        second = "paperclip-second|control\x01.sql"
        self.archive(fresh, age=30, size=4096)
        self.archive(orphan, age=181, size=2001)
        self.archive(orphan + ".gz", age=181, size=7)
        self.archive(second, age=182, size=2002)
        self.archive(second + ".gz", age=182, size=11)
        data = self.verdict(self.invoke(), 1, "stall")
        self.assertEqual(data["newestUsableBackup"], fresh)
        self.assertEqual(data["newestUsableBackupAgeMinutes"], 30)
        self.assertEqual(data["orphanCount"], 2)
        for name, age, size, sibling_size in ((orphan, 181, 2001, 7), (second, 182, 2002, 11)):
            with self.subTest(name=name):
                matching = [r for r in data["reasons"]
                            if f"{name} is {age} minutes old ({size} bytes)" in r]
                self.assertEqual(len(matching), 1, data)
                self.assertIn(f"sibling is only {sibling_size} bytes", matching[0])
        data = self.verdict(self.invoke("--staleness-only"), 0, "ok")
        self.assertEqual(data["newestUsableBackup"], fresh)
        self.assertEqual(data["newestUsableBackupAgeMinutes"], 30)


if __name__ == "__main__":
    unittest.main()
