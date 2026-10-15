#!/usr/bin/env python3
"""Regression tests for the case_0ce22efa sweep-auto node7b1ff338 pressure-gate candidate.

What this proves, offline: the gate code carried by
sweep_auto_node7b1ff338_pressure.patch (this directory) implements the node7b1ff338
controller policy from the issue -- load1 > 12 with low PSI and load5 <= 96
is not a veto; each real CPU/IO/memory/load5/MemAvailable breach holds;
missing or malformed telemetry fails closed.

How: added (`+`) lines are extracted from the patch file itself, exec'd as a
module, and driven with synthetic /proc fixtures. No live host, provider, or
model calls. The patch's removed lines are allowlisted so the candidate
cannot silently touch the legacy pin-clear / owner-reassignment rules.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATCH_PATH = os.path.join(HERE, "sweep_auto_node7b1ff338_pressure.patch")

sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import host_pressure  # noqa: E402  (maintained counterpart; pure stdlib module)

RECOVERY_WRITER_PATH = os.path.join(HERE, "..", "..", "recovery_writer.py")

# Patch prose/call-site lines excluded from the helper module.
# CallSiteTest separately executes every added line in the final hunk.
SKIP_PREFIXES = (
    "Aborts (log only):",
    "MemAvailable <",
    "failure,",
    "veto = ",
    "if veto",
    "raise Abort",
)


def load_patch_lines() -> list[str]:
    with open(PATCH_PATH, encoding="ascii") as handle:
        return handle.read().splitlines()


def split_patch(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (added, removed, context) lines; diff headers excluded."""
    added = [l[1:] for l in lines if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in lines if l.startswith("-") and not l.startswith("---")]
    context = [l[1:] for l in lines if l.startswith(" ") or l == ""]
    return added, removed, context


def exec_gate_module(added: list[str]) -> dict:
    skipped = [l for l in added if l.lstrip().startswith(SKIP_PREFIXES)]
    assert len(skipped) == 6, f"expected 3 doc + 3 call-site skips, got {skipped!r}"
    src_lines = [l for l in added if not l.lstrip().startswith(SKIP_PREFIXES)]
    namespace: dict = {}
    ast.parse("\n".join(src_lines))
    exec(compile("\n".join(src_lines), "node7b1ff338_pressure_gate(patch)", "exec"), namespace)
    for name in ("node7b1ff338_pressure_veto", "_psi_avg60", "_loadavg_triplet",
                 "_mem_available_bytes", "PressureError"):
        assert name in namespace, f"patched gate missing {name}"
    return namespace


def fixtures(cpu_some=3.4, io_some=0.32, mem_full=0.0,
              load1=12.3, load5=14.08, load15=10.0,
              mem_available_kb=125 * 1024 * 1024):
    files = {
        "/proc/pressure/cpu":
            f"some avg10={cpu_some:.2f} avg60={cpu_some:.2f} avg300={cpu_some:.2f} total=0\n",
        "/proc/pressure/io":
            f"some avg10={io_some:.2f} avg60={io_some:.2f} avg300={io_some:.2f} total=0\n",
        "/proc/pressure/memory":
            f"some avg10=1.00 avg60=1.00 avg300=1.00 total=0\n"
            f"full avg10={mem_full:.2f} avg60={mem_full:.2f} avg300={mem_full:.2f} total=0\n",
        "/proc/loadavg": f"{load1} {load5} {load15} 4/1500 12345\n",
        "/proc/meminfo":
            "MemTotal:       131072000 kB\n"
            f"MemAvailable:   {mem_available_kb} kB\n",
    }
    return lambda path: files[path]


class PatchStructureTest(unittest.TestCase):
    def test_four_hunks_against_host_path(self):
        lines = load_patch_lines()
        self.assertTrue(lines[0].startswith("#"))
        self.assertIn("bf32bd3277724b39aae03b6bc17714fa3c874099eeebf1024afbede2b589826c",
                      "\n".join(lines[:6]))
        self.assertIn("--- a/ops/paperclip/sweep-auto/sweep_auto.py", lines)
        self.assertIn("+++ b/ops/paperclip/sweep-auto/sweep_auto.py", lines)
        hunks = [l for l in lines if l.startswith("@@")]
        self.assertEqual(len(hunks), 4)
        for hunk in hunks:
            self.assertRegex(hunk, r"^@@ -\d+,\d+ \+\d+,\d+ @@$")

    def test_removals_are_exactly_the_retired_brake(self):
        _, removed, _ = split_patch(load_patch_lines())
        self.assertEqual(removed, [
            "Aborts (log only): 1-min load > 12, /api/health slower than 10 s or not ok, DB helper failure, board key 401.",
            "LOAD_ABORT = 12.0",
            "        if s.load > LOAD_ABORT:",
            '            raise Abort(f"load1 {s.load:.1f} > {LOAD_ABORT}")',
        ])

    def test_added_code_touches_no_legacy_mutation_rule(self):
        added, _, _ = split_patch(load_patch_lines())
        suspects = [l for l in added
                    if re.search(r"(?i)pin.clear|clear.pin|releas|reset|assign|credential|secret|password|token", l)]
        self.assertEqual(suspects, [], f"patch must not touch legacy rules: {suspects!r}")

    def test_retired_symbol_fully_gone_from_added_code(self):
        added, _, _ = split_patch(load_patch_lines())
        self.assertFalse(any("LOAD_ABORT" in l for l in added))
        self.assertTrue(any("veto = node7b1ff338_pressure_veto()" in l for l in added))


class GateMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        added, _, _ = split_patch(load_patch_lines())
        cls.ns = exec_gate_module(added)
        # staticmethod: a plain function held as a class attribute would
        # otherwise bind self as its first argument on instance access.
        cls.veto = staticmethod(cls.ns["node7b1ff338_pressure_veto"])

    def test_issue_repro_load1_above_12_with_low_psi_is_headroom(self):
        # 13:03Z abort (load1 12.3) and 13:07Z host_pressure=false readings.
        for load1 in (12.1, 12.3, 15.9):
            with self.subTest(load1=load1):
                self.assertIsNone(self.veto(fixtures(load1=load1)))

    def test_load1_alone_never_vetoes(self):
        self.assertIsNone(self.veto(fixtures(load1=500.0)))

    def test_each_real_breach_holds(self):
        cases = [
            (dict(cpu_some=30.1), "cpu"),
            (dict(io_some=30.1), "io"),
            (dict(mem_full=5.1), "memory full"),
            (dict(load5=96.1), "load5"),
            (dict(mem_available_kb=7 * 1024 * 1024), "MemAvailable"),
        ]
        for kwargs, needle in cases:
            with self.subTest(kwargs=kwargs):
                reason = self.veto(fixtures(**kwargs))
                self.assertIsNotNone(reason)
                self.assertIn(needle, reason)

    def test_boundaries_hold_headroom(self):
        self.assertIsNone(self.veto(fixtures(
            cpu_some=30.0, io_some=30.0, mem_full=5.0, load5=96.0,
            mem_available_kb=8 * 1024 * 1024)))

    def test_missing_or_malformed_telemetry_fails_closed(self):
        def missing(path):
            raise OSError("synthetic /proc read failure")

        bad_readers = {"oserror": missing}

        def empty_cpu(path):
            files = fixtures()(path)
            return "" if path == "/proc/pressure/cpu" else files

        def nan_cpu(path):
            files = fixtures()(path)
            if path == "/proc/pressure/cpu":
                return "some avg10=nan avg60=nan avg300=nan total=0\n"
            return files

        bad_readers["empty_cpu"] = empty_cpu
        bad_readers["nan_cpu"] = nan_cpu

        for name, reader in bad_readers.items():
            with self.subTest(name=name):
                reason = self.veto(reader)
                self.assertIsNotNone(reason)
                self.assertIn("fail closed", reason)

        malformed_files = {
            "missing_avg60": {"/proc/pressure/cpu": "some avg10=1 avg300=1 total=1\n"},
            "psi_over_100": {"/proc/pressure/cpu": "some avg10=1 avg60=101 avg300=1 total=1\n"},
            "duplicated_row": {"/proc/pressure/cpu": "some avg10=1 avg60=1 avg300=1 total=1\nsome avg10=2 avg60=2 avg300=2 total=2\n"},
            "short_loadavg": {"/proc/loadavg": "12.3 14.08\n"},
            "negative_load": {"/proc/loadavg": "12.3 -1 10.0 4/1500 1\n"},
            "nan_load": {"/proc/loadavg": "nan 14.08 10.0 4/1500 1\n"},
            "inf_load": {"/proc/loadavg": "inf 14.08 10.0 4/1500 1\n"},
            "inf_psi": {"/proc/pressure/cpu": "some avg10=1 avg60=inf avg300=1 total=1\n"},
            "meminfo_mb_unit": {"/proc/meminfo": "MemAvailable:   8388608 MB\n"},
            "meminfo_no_unit": {"/proc/meminfo": "MemAvailable:   8388608\n"},
            "meminfo_negative": {"/proc/meminfo": "MemAvailable:   -1 kB\n"},
            "meminfo_nan": {"/proc/meminfo": "MemAvailable:   NaN kB\n"},
            "meminfo_missing": {"/proc/meminfo": "MemTotal: 131072000 kB\n"},
        }
        for name, overlay in malformed_files.items():
            with self.subTest(name=name):
                base = fixtures()

                def reader(path, base=base, overlay=overlay):
                    return overlay.get(path, base(path))

                reason = self.veto(reader)
                self.assertIsNotNone(reason)
                self.assertIn("fail closed", reason)

    def test_veto_never_raises(self):
        try:
            self.veto(lambda path: "")
            self.veto(None if False else (lambda path: (_ for _ in ()).throw(OSError("x"))))
        except Exception as error:  # noqa: BLE001
            self.fail(f"veto must return a string, not raise: {error!r}")

    def test_thresholds_match_maintained_counterpart(self):
        self.assertEqual(self.ns["node7525e5a1_CPU_SOME_MAX"], host_pressure.CPU_SOME_MAX)
        self.assertEqual(self.ns["node7525e5a1_IO_SOME_MAX"], host_pressure.IO_SOME_MAX)
        self.assertEqual(self.ns["node7525e5a1_MEM_FULL_MAX"], host_pressure.MEM_FULL_MAX)
        self.assertEqual(self.ns["node7525e5a1_LOAD5_HARD_MAX"], host_pressure.LOAD5_HARD_MAX)
        with open(RECOVERY_WRITER_PATH, encoding="ascii") as handle:
            writer_src = handle.read()
        self.assertIn("MIN_AVAILABLE_BYTES = 8 * 1024**3", writer_src)
        self.assertIn("node7525e5a1_MIN_AVAILABLE_BYTES = 8 * 1024**3",
                      "\n".join(load_patch_lines()))


class CallSiteTest(unittest.TestCase):
    class Abort(Exception):
        pass

    @classmethod
    def setUpClass(cls):
        lines = load_patch_lines()
        added, _, _ = split_patch(lines)
        cls.gate = staticmethod(exec_gate_module(added)["node7b1ff338_pressure_veto"])
        # Take the final hunk's entire added block, not a prefix-filtered copy
        # or a hand-written equivalent. Only /proc reads and Abort are stubbed;
        # no surrounding host code, health requests, or board writes execute.
        start = max(i for i, line in enumerate(lines) if line.startswith("@@"))
        call_site, _, _ = split_patch(lines[start + 1:])
        cls.namespace = {"Abort": cls.Abort}
        body = textwrap.indent(textwrap.dedent("\n".join(call_site)), "    ")
        exec(compile("def check_firing(s):\n" + body,
                     "node7b1ff338_pressure_call_site(patch)", "exec"), cls.namespace)
        cls.check_firing = staticmethod(cls.namespace["check_firing"])

    def test_headroom_continues_at_the_real_call_site(self):
        cases = [dict(load1=load1) for load1 in (12.1, 12.3, 15.9, 500.0)]
        cases.append(dict(cpu_some=30.0, io_some=30.0, mem_full=5.0,
                          load5=96.0, mem_available_kb=8 * 1024 * 1024))
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                reader = fixtures(**kwargs)
                self.namespace["node7b1ff338_pressure_veto"] = lambda: self.gate(reader)
                self.check_firing(SimpleNamespace(load=kwargs.get("load1", 12.3)))

    def test_pressure_and_unreadable_telemetry_abort_at_the_real_call_site(self):
        def missing(path):
            raise OSError("synthetic /proc read failure")

        readers = [fixtures(**kwargs) for kwargs in (
            dict(cpu_some=30.1), dict(io_some=30.1), dict(mem_full=5.1),
            dict(load5=96.1), dict(mem_available_kb=7 * 1024 * 1024),
        )]
        readers.extend((missing, lambda path: ""))
        for i, reader in enumerate(readers):
            with self.subTest(case=i):
                self.namespace["node7b1ff338_pressure_veto"] = lambda: self.gate(reader)
                with self.assertRaises(self.Abort):
                    self.check_firing(SimpleNamespace(load=12.3))

    def test_empty_veto_still_aborts_at_the_real_call_site(self):
        # The helper contract is None vs a string, not truthiness.
        self.namespace["node7b1ff338_pressure_veto"] = lambda: ""
        with self.assertRaises(self.Abort):
            self.check_firing(SimpleNamespace(load=12.3))


class PatchMechanicsTest(unittest.TestCase):
    def test_patch_applies_to_its_own_preimage(self):
        if shutil.which("patch") is None:
            self.skipTest("patch(1) unavailable")
        lines = load_patch_lines()
        pre, post = [], []
        in_hunk = False
        for line in lines:
            if line.startswith("@@"):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if line.startswith("-"):
                pre.append(line[1:])
            elif line.startswith("+"):
                post.append(line[1:])
            elif line.startswith(" ") or line == "":
                pre.append(line[1:])
                post.append(line[1:])
            else:
                self.fail(f"bare line breaks unified diff: {line!r}")
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = os.path.join(tmp, "ops", "paperclip", "sweep-auto")
            os.makedirs(target_dir)
            target = os.path.join(target_dir, "sweep_auto.py")
            with open(target, "w", encoding="ascii") as handle:
                handle.write("\n".join(pre) + "\n")
            for args in (["-p1", "--dry-run"], ["-p1", "--silent"]):
                proc = subprocess.run(
                    [shutil.which("patch"), *args,
                     "-i", PATCH_PATH],
                    cwd=tmp, capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            with open(target, encoding="ascii") as handle:
                self.assertEqual(handle.read(), "\n".join(post) + "\n")


if __name__ == "__main__":
    unittest.main()
