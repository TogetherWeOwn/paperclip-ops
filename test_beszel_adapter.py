#!/usr/bin/env python3
"""Hermetic suite for ops/case_f2010cb7/beszel_adapter.py.

Pins, each with a positive control in the healthy fixture run:
  1. MAPPING — native cpu/mp/dp pass through; load15_per_cpu = la[2]/threads;
     failed_units = info.sv[1]; disk bytes = GiB * 2**30.
  2. NO INVENTED FIELDS — inode_used_percent and container_restarts_24h
     never appear (Beszel records neither).
  3. DOWN HOST FAILS CLOSED — status != "up" emits {"name"} only, even
     with fresh-looking stats and sv present.
  4. STALE STATS FAIL CLOSED — newest stats older than --max-age emit
     {"name"} only (control: same fixture, --now within the bound maps).
  5. MISSING sv OMITS failed_units (unmeasured != 0) but keeps the rest.
  6. HISTORY — >= 2 points map du to used_bytes sorted by time; a single
     point omits disk_history entirely.
  7. REFUSALS — empty systems, non-JSON systems, missing stats-latest
     each exit 2 with empty stdout.
  8. CORES FALLBACK — info.t absent divides by info.c.

Variants are built by mutating fixture copies in a temp dir; the shipped
fixtures are never modified. No network, no credentials.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADAPTER = ROOT / "ops/case_f2010cb7/beszel_adapter.py"
NATIVE = ROOT / "ops/case_f2010cb7/fixtures/native"
NOW = "1792238400"  # 2026-10-17 12:00:00Z; fixtures' latest stats are 11:59
GIB = 2**30


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="beszel_adapter_test."))
        for name in ("systems.json", "stats-latest.json", "stats-history.json"):
            shutil.copy(NATIVE / name, self.dir / name)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_adapter(self, *extra, now=NOW):
        proc = subprocess.run(
            [sys.executable, str(ADAPTER), "--now", now, *extra, str(self.dir)],
            capture_output=True, text=True, timeout=30)
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc, out

    def mutate(self, filename, fn):
        path = self.dir / filename
        data = json.loads(path.read_text())
        fn(data)
        path.write_text(json.dumps(data))

    def host(self, out, name):
        return next(h for h in out if h["name"] == name)


class TestMapping(AdapterCase):
    def test_healthy_mapping_and_no_invented_fields(self):
        proc, out = self.run_adapter()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        h = self.host(out, "node7b1ff338")
        self.assertEqual(h["cpu_used_percent"], 25.0)
        self.assertEqual(h["mem_used_percent"], 60.0)
        self.assertEqual(h["disk_used_percent"], 42.5)
        self.assertAlmostEqual(h["load15_per_cpu"], 3.2 / 4)
        self.assertEqual(h["failed_units"], 0)
        self.assertEqual(h["disk_total_bytes"], 100 * GIB)
        self.assertNotIn("inode_used_percent", h)
        self.assertNotIn("container_restarts_24h", h)

    def test_history_points(self):
        _proc, out = self.run_adapter()
        hist = self.host(out, "node7b1ff338")["disk_history"]
        self.assertEqual(len(hist), 3)
        self.assertEqual([p["used_bytes"] for p in hist],
                         [round(42.3 * GIB), round(42.4 * GIB), round(42.5 * GIB)])
        self.assertEqual([p["t"] for p in hist], sorted(p["t"] for p in hist))

    def test_single_history_point_omits_history(self):
        self.mutate("stats-history.json", lambda d: d.__setitem__("items", d["items"][:1]))
        _proc, out = self.run_adapter()
        self.assertNotIn("disk_history", self.host(out, "node7b1ff338"))

    def test_cores_fallback_when_threads_absent(self):
        def drop_threads(d):
            del d["items"][0]["info"]["t"]
        self.mutate("systems.json", drop_threads)
        _proc, out = self.run_adapter()
        self.assertAlmostEqual(self.host(out, "node7b1ff338")["load15_per_cpu"], 3.2 / 2)

    def test_missing_sv_omits_failed_units_only(self):
        def drop_sv(d):
            del d["items"][0]["info"]["sv"]
        self.mutate("systems.json", drop_sv)
        _proc, out = self.run_adapter()
        h = self.host(out, "node7b1ff338")
        self.assertNotIn("failed_units", h)
        self.assertEqual(h["cpu_used_percent"], 25.0)


class TestFailClosed(AdapterCase):
    def test_down_host_emits_name_only(self):
        _proc, out = self.run_adapter()
        self.assertEqual(self.host(out, "ovh-1"), {"name": "ovh-1"})

    def test_stale_stats_emit_name_only(self):
        later = str(int(NOW) + 3600)  # stats now 61 min old > 600s bound
        proc, out = self.run_adapter(now=later)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.host(out, "node7b1ff338"), {"name": "node7b1ff338"})

    def test_missing_stats_record_emits_name_only(self):
        self.mutate("stats-latest.json", lambda d: d.__setitem__("items", []))
        _proc, out = self.run_adapter()
        self.assertEqual(self.host(out, "node7b1ff338"), {"name": "node7b1ff338"})

    def test_non_numeric_stat_emits_name_only(self):
        def poison(d):
            d["items"][0]["stats"]["cpu"] = "high"
        self.mutate("stats-latest.json", poison)
        _proc, out = self.run_adapter()
        self.assertEqual(self.host(out, "node7b1ff338"), {"name": "node7b1ff338"})


class TestRefusals(AdapterCase):
    def assert_refusal(self, proc):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_empty_systems_refuses(self):
        self.mutate("systems.json", lambda d: d.__setitem__("items", []))
        proc, _ = self.run_adapter()
        self.assert_refusal(proc)

    def test_non_json_systems_refuses(self):
        (self.dir / "systems.json").write_text("not json")
        proc, _ = self.run_adapter()
        self.assert_refusal(proc)

    def test_missing_stats_latest_refuses(self):
        (self.dir / "stats-latest.json").unlink()
        proc, _ = self.run_adapter()
        self.assert_refusal(proc)

    def test_missing_history_is_tolerated(self):
        (self.dir / "stats-history.json").unlink()
        proc, out = self.run_adapter()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("disk_history", self.host(out, "node7b1ff338"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
