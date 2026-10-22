"""Regression proofs for the PR #90 review findings (case_30b51a0f).

Builder-owned companion to the immutable `test_timer_audit_integrity.py`
contract: each finding from the CodeRabbit and Paperclip Review rounds on
`fix(timer-audit): reject unbounded services, yaml gaps, scope-blind absence`
gets a red-before/green-after case here. The immutable acceptance file is
untouched; these tests pin only the review-driven behavior deltas:

  timeout-arg-position .... `timeout` counts as a bound only as the executable
      itself (first token, modulo systemd -/@/+ prefixes) or as the command run
      by a leading flock/env/nice wrapper; anywhere else it is argv, not a bound
  live-scope-prefix-exit . hard findings are selected by result severity, so an
      expected-live-absent entry whose id starts with "scope" still exits 1
  entry-scope-never-measured  systemd-timer scopes outside {user, system} are
      schema violations, never silently clean
  duplicate-execstart-bound  every ExecStart/ExecStartPre/ExecStartPost line
      runs, so more than one start command needs a finite TimeoutStartSec
  capture-available-type  a non-boolean capture `available` is malformed
      (exit 2), never a quietly measured scope
  malformed-row-traceback  malformed registry rows yield controlled nonzero
      diagnostics with a valid JSON envelope, never a traceback
  duration-overflow-finite  durations that overflow to infinity are not finite
  acceptance-suite-in-ci  the immutable suite is executed by the CI host-timer
      step, not just runnable by hand
  undecodable-repo-bytes  unit/workflow/manifest files with invalid UTF-8
      (or unparseable unit ini) yield controlled I4 violations with a
      valid JSON envelope, never a traceback

Offline rules: synthetic tmp trees and supplied live-capture JSON only.
Every checker invocation runs as a bounded fixture subprocess (timeout:
TIMEOUT_S, fail closed). A PATH shim intercepts systemctl/busctl/
journalctl, fails closed (exit 1) and records the attempt; tearDown fails
the test if any host command was attempted. No network, no systemd, no
state writes outside the run-owned tmp dir.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_timer_audit_review_proofs
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK = HERE / "ops" / "case_30b51a0f" / "host_timer_registry_check.py"
CI_WORKFLOW = HERE / ".github" / "workflows" / "ci.yml"
TIMEOUT_S = 60
HOST_COMMANDS = ("systemctl", "busctl", "journalctl")

TIMER_TEXT = "[Timer]\nOnCalendar=hourly\nUnit=example.service\n"


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "host_timer_registry_check_under_test", str(CHECK))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_entry(eid, kind="systemd-timer", scope="user", role="writer",
               source=None, schedule=None, purpose=None, writes=None,
               state="active", expect_live="unknown", verified=True,
               bound="TimeoutStartSec", **over):
    e = {"id": eid, "kind": kind, "scope": scope,
         "source": source if source is not None else {"type": "host-only"},
         "purpose": purpose if purpose is not None else f"{eid}-purpose",
         "role": role,
         "writes": writes if writes is not None else ([f"fs:{eid}"] if role == "writer" else []),
         "reports": [], "schedule": schedule if schedule is not None else {},
         "bound": bound, "receipt": "journal", "state": state,
         "expectLive": expect_live, "verified": verified, "owner": "synthetic"}
    e.update(over)
    return e


def repo_timer_entry(eid="timer", scope="user", timer="systemd/example.timer",
                     schedule=None, **over):
    return make_entry(eid, kind="systemd-timer", scope=scope, role="writer",
                      source={"type": "repo", "timer": timer},
                      schedule=schedule if schedule is not None else {"OnCalendar": "hourly"},
                      **over)


def live_entry(eid, scope, unit="foo.timer", **over):
    return make_entry(eid, kind="systemd-timer", scope=scope, role="writer",
                      source={"type": "host-only"}, bound="host", unit=unit, **over)


def live_timer(unit, fragment_dir="/home/synth/.config/systemd/user"):
    return {"unit": unit, "activeState": "active", "unitFileState": "enabled",
            "fragmentPath": f"{fragment_dir}/{unit}", "service": {"result": "success"}}


class ProofCase(unittest.TestCase):
    """Synthetic-tree fixture with host-command interception (fail closed)."""

    maxDiff = 4000

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timer-audit-proofs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.marker = self.tmp / "host-commands.log"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for cmd in HOST_COMMANDS:
            shim = bindir / cmd
            shim.write_text('#!/bin/sh\necho "HOST-COMMAND %s $@" >> "$HOST_COMMAND_MARKER"\n'
                            "exit 1\n" % cmd)
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                        HOST_COMMAND_MARKER=str(self.marker))
        self.reg_path = self.root / "registry.json"

    def tearDown(self):
        if self.marker.is_file() and self.marker.read_text().strip():
            self.fail("host command attempted (fail closed):\n"
                      + self.marker.read_text())

    # -- writers ------------------------------------------------------
    def write_registry(self, entries):
        self.reg_path.write_text(json.dumps({"schemaVersion": 1, "entries": entries}))
        return self.reg_path

    def write_timer(self, text=TIMER_TEXT, name="example.timer"):
        p = self.root / "systemd" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def write_service(self, text, name="example.service"):
        p = self.root / "systemd" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def write_bytes_file(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def write_capture(self, user=(), system=(), user_available=True,
                      system_available=True):
        def scope(timers, available):
            return {"available": available,
                    "reason": "" if available else "synthetic bus down",
                    "timers": list(timers)}
        path = self.root / "capture.json"
        path.write_text(json.dumps({"scopes": {"user": scope(user, user_available),
                                              "system": scope(system, system_available)}}))
        return path

    def write_raw_capture(self, text):
        path = self.root / "capture.json"
        path.write_text(text)
        return path

    # -- runner (bounded fixture subprocess, fail closed) -------------
    def run_raw(self, *extra, registry=None, capture=None):
        cmd = [sys.executable, str(CHECK), "--root", str(self.root)]
        if registry is not None:
            cmd += ["--registry", str(registry)]
        if capture is not None:
            cmd += ["--live", str(capture)]
        try:
            return subprocess.run(cmd + list(extra), capture_output=True, text=True,
                                  env=self.env, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            self.fail(f"checker exceeded the {TIMEOUT_S}s bound (fail closed): {exc}")
            raise AssertionError("unreachable")

    def run_audit(self, *extra, registry=None, capture=None):
        res = self.run_raw(*extra, registry=registry, capture=capture)
        return res.returncode, res.stdout + res.stderr

    def check(self, entries, *extra, capture=None):
        return self.run_audit(*extra, registry=self.write_registry(entries),
                              capture=capture)

    def run_json(self, *extra, registry=None, capture=None):
        res = self.run_raw(*extra, "--json", registry=registry, capture=capture)
        combined = res.stdout + res.stderr
        self.assertTrue(res.stdout.strip(),
                        f"--json produced no stdout (exit {res.returncode}): {combined[-1500:]}")
        try:
            doc = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"--json stdout is not JSON (exit {res.returncode}): {exc}\n"
                      f"stdout tail: {res.stdout[-1500:]}")
            raise AssertionError("unreachable")
        return res.returncode, doc, combined

    def assert_valid_envelope(self, doc):
        self.assertIsInstance(doc, dict)
        for key in ("schemaVersion", "violations", "unknown", "coverage",
                    "results", "next_action"):
            self.assertIn(key, doc, f"envelope lacks {key!r}: {sorted(doc)}")
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertIsInstance(doc["results"], list)


class TimeoutArgPosition(ProofCase):
    """timeout-arg-position: only a commanding `timeout` bounds I3."""

    def test_worker_argument_timeout_is_unbounded(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=/usr/bin/worker timeout 60\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer: example.service", out)

    def test_flock_wrapped_timeout_stays_bounded(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=/usr/bin/flock -n /tmp/guard.lock "
                           "/usr/bin/timeout 45s /bin/work\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)

    def test_env_wrapped_timeout_stays_bounded(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=/usr/bin/env FOO=bar /usr/bin/timeout 60 /bin/work\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)

    def test_prefixed_timeout_executable_stays_bounded(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=-/usr/bin/timeout 60 /bin/work\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)


class ScopePrefixExit(ProofCase):
    """live-scope-prefix-exit: severity, not message prefix, picks hard findings."""

    def test_scope_prefixed_id_absence_exits_1(self):
        entries = [live_entry("scope-foo", "system", expect_live=True)]
        cap = self.write_capture()
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 1, out)
        self.assertIn("LIVE scope-foo: expected live, absent", out)

    def test_scope_prefixed_id_absence_exits_1_json(self):
        entries = [live_entry("scope-foo", "system", expect_live=True)]
        cap = self.write_capture()
        code, doc, combined = self.run_json(registry=self.write_registry(entries),
                                            capture=cap)
        self.assert_valid_envelope(doc)
        self.assertEqual(code, 1, combined)
        self.assertEqual(doc["violations"], 1, doc)


class ForeignEntryScope(ProofCase):
    """entry-scope-never-measured: foreign systemd-timer scopes cannot be clean."""

    def test_capitalized_scope_is_rejected(self):
        entries = [live_entry("sys-foo", "System", expect_live=True)]
        cap = self.write_capture()
        code, out = self.check(entries, capture=cap)
        self.assertNotEqual(code, 0, out)
        self.assertIn("must be 'user' or 'system'", out)

    def test_foreign_scope_is_rejected_json(self):
        entries = [live_entry("host-foo", "host", expect_live=True)]
        cap = self.write_capture()
        code, doc, combined = self.run_json(registry=self.write_registry(entries),
                                            capture=cap)
        self.assert_valid_envelope(doc)
        self.assertNotEqual(code, 0, combined)
        self.assertGreaterEqual(doc["violations"], 1, doc)


class DuplicateExecStartBound(ProofCase):
    """duplicate-execstart-bound: every start command needs coverage."""

    def test_second_execstart_without_bound_fails(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=/bin/longwork\n"
                           "ExecStart=/usr/bin/timeout 60 /bin/a\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer: example.service runs 2 start commands", out)

    def test_unbounded_execstartpre_fails(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStartPre=/bin/longsetup\n"
                           "ExecStart=/usr/bin/timeout 60 /bin/a\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer: example.service runs 2 start commands", out)

    def test_multi_command_with_finite_timeout_stays_bounded(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\nTimeoutStartSec=5min\n"
                           "ExecStartPre=/bin/setup\n"
                           "ExecStart=/bin/longwork\n"
                           "ExecStart=/usr/bin/timeout 60 /bin/a\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)

    def test_execstart_reset_forgets_earlier_lines(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=/bin/longwork\n"
                           "ExecStart=\n"
                           "ExecStart=/usr/bin/timeout 60 /bin/a\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)

    def test_semicolon_separated_second_command_without_bound_fails(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=/usr/bin/timeout 60 /bin/a ; /bin/longwork\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer: example.service runs 2 start commands", out)

    def test_escaped_semicolon_stays_a_literal_argument(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           "ExecStart=/usr/bin/timeout 60 /bin/echo \\;\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)

    def test_quoted_semicolon_stays_a_literal_argument(self):
        self.write_timer()
        self.write_service('[Service]\nType=oneshot\n'
                           'ExecStart=/usr/bin/timeout 60 /bin/echo ";"\n')
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)


class CaptureAvailableType(ProofCase):
    """capture-available-type: `available` must be a real boolean."""

    def test_numeric_available_with_bad_row_is_malformed(self):
        cap = self.write_raw_capture(json.dumps(
            {"scopes": {"user": {"available": 1, "timers": [{"activeState": "active"}]},
                        "system": {"available": True, "timers": []}}}))
        code, out = self.check([live_entry("usr-foo", "user")], capture=cap)
        self.assertEqual(code, 2, out)
        self.assertIn("scopes.user.available must be a boolean", out)

    def test_string_available_with_bad_row_is_malformed(self):
        cap = self.write_raw_capture(json.dumps(
            {"scopes": {"user": {"available": "yes",
                                "timers": [{"activeState": "active"}]},
                        "system": {"available": True, "timers": []}}}))
        code, out = self.check([live_entry("usr-foo", "user")], capture=cap)
        self.assertEqual(code, 2, out)

    def test_numeric_available_is_malformed_json(self):
        cap = self.write_raw_capture(json.dumps(
            {"scopes": {"user": {"available": 1, "timers": [{"activeState": "active"}]},
                        "system": {"available": True, "timers": []}}}))
        code, doc, combined = self.run_json(
            registry=self.write_registry([live_entry("usr-foo", "user")]), capture=cap)
        self.assert_valid_envelope(doc)
        self.assertEqual(code, 2, combined)
        self.assertTrue(any(r["code"] == "malformed-capture" for r in doc["results"]), doc)

    def test_missing_available_is_malformed(self):
        cap = self.write_raw_capture(json.dumps(
            {"scopes": {"user": {"timers": []},
                        "system": {"available": True, "timers": []}}}))
        code, out = self.check([live_entry("usr-foo", "user")], capture=cap)
        self.assertEqual(code, 2, out)
        self.assertIn("scopes.user.available must be a boolean", out)
        code, doc, combined = self.run_json(
            registry=self.write_registry([live_entry("usr-foo", "user")]), capture=cap)
        self.assert_valid_envelope(doc)
        self.assertEqual(code, 2, combined)
        self.assertTrue(any(r["code"] == "malformed-capture" for r in doc["results"]), doc)


class MalformedRowNoTraceback(ProofCase):
    """malformed-row-traceback: bad rows diagnose, never crash."""

    SINKS = {
        "kind": {"kind": []},
        "role": {"role": {}},
        "state": {"state": []},
        "purpose": {"purpose": []},
        "writes": {"writes": [42]},
        "supersededBy": {"supersededBy": ["x"]},
        "supersededBy_empty": {"supersededBy": []},
        "retiredWith": {"retiredWith": {"x": 1}},
        "source.timer": {"source": {"type": "repo", "timer": []}},
        "schedule.jobs": {"kind": "plugin-job", "role": "detector", "writes": [],
                          "source": {"type": "repo",
                                     "manifest": "plugins/x/src/manifest.ts"},
                          "schedule": {"jobs": 42}},
    }

    def test_each_sink_is_controlled_text(self):
        for name, over in self.SINKS.items():
            with self.subTest(sink=name):
                code, out = self.check([make_entry("row", **over)])
                self.assertNotIn("Traceback", out)
                self.assertNotEqual(code, 0, out)

    def test_each_sink_is_controlled_json(self):
        for name, over in self.SINKS.items():
            with self.subTest(sink=name):
                code, doc, combined = self.run_json(
                    registry=self.write_registry([make_entry("row", **over)]))
                self.assert_valid_envelope(doc)
                self.assertNotIn("Traceback", combined)
                self.assertNotEqual(code, 0, combined)

    def test_non_object_waiver_item_with_end_state_is_controlled(self):
        entry = make_entry("row", waivers=[42])
        code, out = self.check([entry], "--end-state")
        self.assertNotIn("Traceback", out)
        self.assertNotEqual(code, 0, out)
        code, doc, combined = self.run_json(
            "--end-state", registry=self.write_registry([entry]))
        self.assert_valid_envelope(doc)
        self.assertNotIn("Traceback", combined)
        self.assertNotEqual(code, 0, combined)


class DurationOverflowFinite(ProofCase):
    """duration-overflow-finite: infinite totals are not finite bounds."""

    HUGE_YEARS = "9" * 400 + "years"

    def test_huge_duration_service_is_unbounded(self):
        self.write_timer()
        self.write_service("[Service]\nType=oneshot\n"
                           f"TimeoutStartSec={self.HUGE_YEARS}\n"
                           "ExecStart=/bin/work\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer: example.service", out)

    def test_huge_duration_parses_not_finite(self):
        checker = load_checker()
        finite, reason = checker.parse_finite_duration(self.HUGE_YEARS)
        self.assertFalse(finite, reason)


class AcceptanceSuiteInCI(ProofCase):
    """acceptance-suite-in-ci: the suites gating the checker actually execute."""

    def test_immutable_suite_runs_in_host_timer_step(self):
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("test_timer_audit_integrity", text)

    def test_review_proofs_run_in_host_timer_step(self):
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(Path(__file__).stem, text)


class UndecodableRepoBytes(ProofCase):
    """undecodable-repo-bytes: bad file bytes diagnose, never crash."""

    BAD = b"[Service]\nExecStart=/bin/\xff\xfe\n"
    BOUNDED_SERVICE = ("[Service]\nType=oneshot\nTimeoutStartSec=5min\n"
                       "ExecStart=/bin/work\n")

    def plant(self, sink):
        """Clean repo-timer baseline plus one undecodable file; returns marker."""
        self.write_timer()
        self.write_service(self.BOUNDED_SERVICE)
        if sink == "service":
            self.write_bytes_file("systemd/example.service", self.BAD)
            return "cannot read service file example.service"
        if sink == "timer":
            self.write_bytes_file("systemd/example.timer", self.BAD)
            return "cannot read timer file systemd/example.timer"
        if sink == "workflow":
            self.write_bytes_file(".github/workflows/nightly.yml", self.BAD)
            return "cannot read workflow file"
        if sink == "manifest":
            self.write_bytes_file("plugins/x/src/manifest.ts", self.BAD)
            return "cannot read manifest file"
        raise AssertionError(f"unknown sink {sink}")

    SINKS = ("service", "timer", "workflow", "manifest")

    def test_service_start_commands_returns_empty_on_bad_bytes(self):
        checker = load_checker()
        path = self.write_bytes_file("systemd/example.service", self.BAD)
        self.assertEqual(checker.service_start_commands(path), [])

    GARBAGE = "not a unit file \x00 no sections\n"

    def test_garbage_unit_content_is_controlled(self):
        for sink, marker in (("systemd/example.service",
                              "cannot read service file"),
                             ("systemd/example.timer",
                              "cannot read timer file")):
            with self.subTest(sink=sink):
                self.write_timer()
                self.write_service(self.BOUNDED_SERVICE)
                (self.root / sink).write_text(self.GARBAGE)
                code, out = self.check([repo_timer_entry()])
                self.assertNotIn("Traceback", out)
                self.assertEqual(code, 1, out)
                self.assertIn(marker, out)
                code, doc, combined = self.run_json(
                    registry=self.write_registry([repo_timer_entry()]))
                self.assert_valid_envelope(doc)
                self.assertNotIn("Traceback", combined)
                self.assertEqual(code, 1, combined)
                codes = [r.get("code") for r in doc["results"]]
                self.assertIn("unreadable-source", codes)

    def test_each_sink_is_controlled_text(self):
        for sink in self.SINKS:
            with self.subTest(sink=sink):
                marker = self.plant(sink)
                code, out = self.check([repo_timer_entry()])
                self.assertNotIn("Traceback", out)
                self.assertEqual(code, 1, out)
                self.assertIn(marker, out)

    def test_each_sink_is_controlled_json(self):
        for sink in self.SINKS:
            with self.subTest(sink=sink):
                marker = self.plant(sink)
                code, doc, combined = self.run_json(
                    registry=self.write_registry([repo_timer_entry()]))
                self.assert_valid_envelope(doc)
                self.assertNotIn("Traceback", combined)
                self.assertEqual(code, 1, combined)
                self.assertIn(marker, combined)
                codes = [r.get("code") for r in doc["results"]]
                self.assertIn("unreadable-source", codes)


if __name__ == "__main__":
    unittest.main()
