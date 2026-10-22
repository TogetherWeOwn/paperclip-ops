"""Behavioral acceptance for the timer-audit-integrity bundle (case_30b51a0f).

Pinned build contract (the Builder implements to this file; this file is
immutable once pushed -- a Builder that needs a different contract opens a
bounded follow-up instead of editing these tests):

  B1  I3 requires an effective FINITE POSITIVE runtime bound. The mere
      presence of a TimeoutStartSec key (or a timeout token in ExecStart)
      is not enough: TimeoutStartSec=0/infinity/empty/malformed and a
      `timeout 0` wrapper cannot satisfy I3. Explicit bounded-runtime
      waivers and legitimate finite systemd durations (bare seconds, s,
      ms, min, h, d, multi-component) still pass. Unsupported or
      ambiguous bounds are unknown/violations, never clean.
  B2  Scheduled-workflow inventory covers BOTH .yml and .yaml suffixes
      with the same schedule contract and deterministic ordering. An
      unregistered scheduled .yaml fails I5; a registered .yaml whose
      cron drifted fails I4; matching .yaml passes. Plugin-job,
      tracked-timer and declared-transition checks are retained; no
      blanket registration or exemptions.
  B3  Expected-live checks retain (scope, unit) identity end to end. A
      timer observed in user scope cannot satisfy an expected system
      timer of the same name, or vice versa. Measured absence (scope
      measured, timer missing) stays a violation (exit 1); an expected
      entry in an UNMEASURED scope is "could not measure" (exit 3),
      never a false absence claim. Vendor exemptions and the 0/1/2/3
      exit meanings are preserved.
  B4  --json emits versioned results: schemaVersion 1, stable
      invariant/entry/scope/unit IDs per finding, coverage and unknown
      counts, kebab-case error codes, per-finding retryable plus
      next_action, and a top-level next_action. Malformed registry or
      capture roots/rows produce controlled nonzero diagnostics (exit 2
      with malformed-registry/malformed-capture codes), never tracebacks
      or false clean. Default text output stays compatible.
  B5  --dry-run is an explicit offline read-only audit alias: identical
      exit/output to the default offline run, zero host commands, zero
      file writes. It is never host capture and never an apply mode.

RED-ON-CURRENT-SOURCE (the deliverable, not a blocker): every test whose
name says unbounded/yaml/scoped-absence/unmeasured/json/dry-run/help-docs
and the malformed-shape controls. GREEN CONTROLS (pass today, must stay
green): finite durations, timeout-wrapper, waivers, .yml behavior,
measured absence, vendor exemption, text compatibility, retained checks.

Offline rules: synthetic tmp trees and supplied live-capture JSON only.
Every checker invocation runs as a bounded fixture subprocess (timeout:
TIMEOUT_S, fail closed). A PATH shim intercepts systemctl/busctl/
journalctl, fails closed (exit 1) and records the attempt; tearDown fails
the test if any host command was attempted. No network, no systemd, no
state writes outside the run-owned tmp dir.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_timer_audit_integrity
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK = HERE / "ops" / "case_30b51a0f" / "host_timer_registry_check.py"
TIMEOUT_S = 60
HOST_COMMANDS = ("systemctl", "busctl", "journalctl")
CODE_RE = re.compile(r"^[a-z0-9-]+$")

TIMER_TEXT = "[Timer]\nOnCalendar=hourly\nUnit=example.service\n"


def service_text(timeout="TimeoutStartSec=30\n", extra=""):
    return f"[Service]\nType=oneshot\n{timeout}ExecStart=/bin/true\n{extra}"


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


def repo_timer_entry(eid="timer", scope="user",
                     timer="systemd/example.timer",
                     schedule=None, **over):
    return make_entry(eid, kind="systemd-timer", scope=scope, role="writer",
                      source={"type": "repo", "timer": timer},
                      schedule=schedule if schedule is not None else {"OnCalendar": "hourly"},
                      **over)


def live_entry(eid, scope, unit="foo.timer", **over):
    return make_entry(eid, kind="systemd-timer", scope=scope, role="writer",
                      source={"type": "host-only"}, bound="host", unit=unit, **over)


def workflow_entry(eid, rel, cron="5 4 * * *", **over):
    return make_entry(eid, kind="gh-actions-schedule", role="detector",
                      source={"type": "repo", "workflow": rel},
                      schedule={"cron": cron}, **over)


def plugin_entry(eid, manifest, jobs, **over):
    return make_entry(eid, kind="plugin-job", role="detector",
                      source={"type": "repo", "manifest": manifest},
                      schedule={"jobs": list(jobs)}, **over)


def live_timer(unit, fragment_dir="/home/synth/.config/systemd/user",
               active="active", file_state="enabled", result="success"):
    return {"unit": unit, "activeState": active, "unitFileState": file_state,
            "fragmentPath": f"{fragment_dir}/{unit}",
            "service": {"result": result}}


class AuditCase(unittest.TestCase):
    """Synthetic-tree fixture with host-command interception (fail closed)."""

    maxDiff = 4000

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timer-audit-integrity-"))
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

    def write_raw_registry(self, text):
        self.reg_path.write_text(text)
        return self.reg_path

    def write_timer(self, text=TIMER_TEXT, name="example.timer"):
        p = self.root / "systemd" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def write_service(self, text=None, name="example.service", **kwargs):
        p = self.root / "systemd" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(service_text(**kwargs) if text is None else text)
        return p

    def write_workflow(self, rel, cron="5 4 * * *"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('on:\n  schedule:\n    - cron: "%s"\n' % cron)
        return p

    def write_manifest(self, rel, schedules):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("jobs: [%s]\n" % ", ".join('{ schedule: "%s" }' % s for s in schedules))
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
        """Run with --json; fail unless stdout is a JSON envelope."""
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

    # -- contract assertions ------------------------------------------
    def assert_valid_envelope(self, doc):
        self.assertIsInstance(doc, dict)
        for key in ("schemaVersion", "violations", "unknown", "coverage",
                    "results", "next_action"):
            self.assertIn(key, doc, f"envelope lacks {key!r}: {sorted(doc)}")
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertIsInstance(doc["violations"], int)
        self.assertIsInstance(doc["unknown"], int)
        self.assertGreaterEqual(doc["violations"], 0)
        self.assertGreaterEqual(doc["unknown"], 0)
        self.assertIsInstance(doc["coverage"], dict)
        self.assertIn("entries", doc["coverage"])
        self.assertIn("liveScopesMeasured", doc["coverage"])
        self.assertIsInstance(doc["results"], list)
        for r in doc["results"]:
            self.assert_valid_result(r)
        self.assertIsInstance(doc["next_action"], str)
        self.assertTrue(doc["next_action"].strip(), "top-level next_action must be actionable")

    def assert_valid_result(self, r):
        self.assertIsInstance(r, dict)
        for key in ("invariant", "entry", "scope", "unit", "severity",
                    "code", "message", "retryable", "next_action"):
            self.assertIn(key, r, f"result lacks {key!r}: {r}")
        if r["code"] in ("malformed-registry", "malformed-capture"):
            self.assertIsNone(r["invariant"], r)
        else:
            self.assertIn(r["invariant"],
                          {"I1", "I2", "I3", "I4", "I5", "I6", "LIVE", "END", "SCHEMA"}, r)
        for key in ("entry", "scope", "unit"):
            self.assertTrue(r[key] is None or isinstance(r[key], str), r)
        self.assertIn(r["severity"], ("violation", "unknown"), r)
        self.assertIsInstance(r["code"], str)
        self.assertRegex(r["code"], CODE_RE, r)
        self.assertIsInstance(r["message"], str)
        self.assertTrue(r["message"].strip(), r)
        self.assertIn(r["retryable"], (True, False), r)
        self.assertIsInstance(r["next_action"], str)
        self.assertTrue(r["next_action"].strip(), r)

    def assert_counts_match(self, doc):
        kinds = [r["severity"] for r in doc["results"]]
        self.assertEqual(doc["violations"], kinds.count("violation"), doc)
        self.assertEqual(doc["unknown"], kinds.count("unknown"), doc)


class FiniteRuntimeBound(AuditCase):
    """B1: I3 needs an effective finite positive bound (RED cases + controls)."""

    def bound_case(self, **service_kwargs):
        self.write_timer()
        self.write_service(**service_kwargs)
        return self.check([repo_timer_entry()])

    def test_timeout_start_sec_zero_is_unbounded(self):
        code, out = self.bound_case(timeout="TimeoutStartSec=0\n")
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer", out)

    def test_timeout_start_sec_infinity_is_unbounded(self):
        code, out = self.bound_case(timeout="TimeoutStartSec=infinity\n")
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer", out)

    def test_timeout_start_sec_empty_is_unbounded(self):
        code, out = self.bound_case(timeout="TimeoutStartSec=\n")
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer", out)

    def test_timeout_start_sec_malformed_is_unbounded(self):
        code, out = self.bound_case(timeout="TimeoutStartSec=banana\n")
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer", out)

    def test_timeout_wrapper_zero_is_unbounded(self):
        svc = ("[Service]\nType=oneshot\n"
               "ExecStart=/usr/bin/timeout 0 /bin/work\n")
        self.write_timer()
        self.write_service(text=svc)
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer", out)

    def test_unknown_duration_suffix_is_never_clean(self):
        code, out = self.bound_case(timeout="TimeoutStartSec=10 lightyears\n")
        self.assertEqual(code, 1, out)
        self.assertIn("timer", out)

    def test_finite_durations_are_bounded(self):
        for value in ("30", "90s", "5min", "1h", "1min 30s", "500ms", "2d"):
            with self.subTest(value=value):
                self.write_timer()
                self.write_service(timeout=f"TimeoutStartSec={value}\n")
                code, out = self.check([repo_timer_entry()])
                self.assertEqual(code, 0, out)

    def test_positive_timeout_wrapper_is_bounded(self):
        svc = ("[Service]\nType=oneshot\n"
               "ExecStart=/usr/bin/timeout 60 /bin/work\n")
        self.write_timer()
        self.write_service(text=svc)
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)

    def test_explicit_waiver_still_exempts(self):
        self.write_timer()
        self.write_service(text="[Service]\nType=oneshot\nExecStart=/bin/true\n")
        entry = repo_timer_entry(waivers=[{"check": "bounded-runtime",
                                           "reason": "synthetic waiver control",
                                           "fix": "add TimeoutStartSec"}])
        code, out = self.check([entry])
        self.assertEqual(code, 0, out)

    def test_missing_bound_without_waiver_still_fails(self):
        self.write_timer()
        self.write_service(text="[Service]\nType=oneshot\nExecStart=/bin/true\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer", out)

    def test_restart_policy_still_rejected(self):
        code, out = self.bound_case(extra="Restart=on-failure\n")
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer", out)


class WorkflowInventory(AuditCase):
    """B2: .yml AND .yaml share one schedule contract (RED cases + controls)."""

    def test_unregistered_yaml_schedule_fails(self):
        self.write_workflow(".github/workflows/example.yml")
        self.write_workflow(".github/workflows/extra.yaml")
        code, out = self.check(
            [workflow_entry("workflow", ".github/workflows/example.yml")])
        self.assertEqual(code, 1, out)
        self.assertIn("I5 .github/workflows/extra.yaml", out)

    def test_registered_yaml_drift_fails(self):
        self.write_workflow(".github/workflows/extra.yaml", cron="6 4 * * *")
        code, out = self.check(
            [workflow_entry("workflow", ".github/workflows/extra.yaml",
                            cron="5 4 * * *")])
        self.assertEqual(code, 1, out)
        self.assertIn("I4 .github/workflows/extra.yaml", out)

    def test_mixed_suffix_inventory_is_deterministic(self):
        self.write_workflow(".github/workflows/z.yml")
        self.write_workflow(".github/workflows/a.yaml")
        self.write_workflow(".github/workflows/b.yaml")
        self.write_workflow(".github/workflows/c.yml")
        entries = [workflow_entry("workflow", ".github/workflows/z.yml")]
        reg = self.write_registry(entries)
        code, out = self.run_audit(registry=reg)
        self.assertEqual(code, 1, out)
        i5 = [l for l in out.splitlines() if l.startswith("I5 .github/workflows/")]
        for rel in ("a.yaml", "b.yaml", "c.yml"):
            self.assertIn(f"I5 .github/workflows/{rel}", i5)
        yaml_lines = [l for l in i5 if l.endswith(".yaml")]
        self.assertEqual(yaml_lines, sorted(yaml_lines))
        code2, out2 = self.run_audit(registry=reg)
        self.assertEqual((code2, out2), (code, out))

    def test_registered_yaml_matching_passes(self):
        self.write_workflow(".github/workflows/extra.yaml")
        code, out = self.check(
            [workflow_entry("workflow", ".github/workflows/extra.yaml")])
        self.assertEqual(code, 0, out)

    def test_yml_controls_unchanged(self):
        self.write_workflow(".github/workflows/example.yml")
        self.write_workflow(".github/workflows/extra.yml")
        code, out = self.check(
            [workflow_entry("workflow", ".github/workflows/example.yml")])
        self.assertEqual(code, 1, out)
        self.assertIn("I5 .github/workflows/extra.yml", out)
        code, out = self.check(
            [workflow_entry("w1", ".github/workflows/example.yml"),
             workflow_entry("w2", ".github/workflows/extra.yml")])
        self.assertEqual(code, 0, out)

    def test_unscheduled_yaml_is_ignored(self):
        other = self.root / ".github/workflows/notes.yaml"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text('on:\n  push:\n    branches: [main]\n')
        self.write_workflow(".github/workflows/example.yml")
        code, out = self.check(
            [workflow_entry("workflow", ".github/workflows/example.yml")])
        self.assertEqual(code, 0, out)


class ScopedLiveIdentity(AuditCase):
    """B3: expected-live keeps (scope, unit) identity (RED cases + controls)."""

    VENDOR_DIR = "/usr/lib/systemd/system"

    def test_user_timer_cannot_satisfy_expected_system_timer(self):
        entries = [live_entry("sys-foo", "system", expect_live=True),
                   live_entry("usr-foo", "user", expect_live=False)]
        cap = self.write_capture(user=[live_timer("foo.timer")], system=[])
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 1, out)
        self.assertIn("LIVE sys-foo", out)
        self.assertIn("expected live, absent", out)
        self.assertNotIn("not in the registry", out)

    def test_system_timer_cannot_satisfy_expected_user_timer(self):
        entries = [live_entry("usr-foo", "user", expect_live=True),
                   live_entry("sys-foo", "system", expect_live=False)]
        cap = self.write_capture(user=[],
                                 system=[live_timer("foo.timer",
                                                    fragment_dir="/etc/systemd/system")])
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 1, out)
        self.assertIn("LIVE usr-foo", out)
        self.assertIn("expected live, absent", out)
        self.assertNotIn("not in the registry", out)

    def test_vendor_timer_in_other_scope_satisfies_nothing(self):
        entries = [live_entry("sys-foo", "system", expect_live=True)]
        cap = self.write_capture(
            user=[live_timer("foo.timer", fragment_dir=self.VENDOR_DIR)],
            system=[])
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 1, out)
        self.assertIn("expected live, absent", out)
        self.assertNotIn("not in the registry", out)

    def test_unmeasured_scope_is_not_measured_absence(self):
        entries = [live_entry("sys-foo", "system", expect_live=True)]
        cap = self.write_capture(user=[], system=[], system_available=False)
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 3, out)
        self.assertIn("was not measured", out)
        self.assertNotIn("absent from the host capture", out)

    def test_same_name_in_both_scopes_live_is_clean(self):
        entries = [live_entry("sys-foo", "system", expect_live=True),
                   live_entry("usr-foo", "user", expect_live=True)]
        cap = self.write_capture(
            user=[live_timer("foo.timer")],
            system=[live_timer("foo.timer", fragment_dir="/etc/systemd/system")])
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 0, out)

    def test_measured_absence_still_fails(self):
        entries = [live_entry("sys-foo", "system", expect_live=True)]
        cap = self.write_capture(user=[], system=[])
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 1, out)
        self.assertIn("expected live, absent", out)

    def test_measured_absence_wins_over_unmeasured_other_scope(self):
        entries = [live_entry("usr-foo", "user", expect_live=True)]
        cap = self.write_capture(user=[], system=[], system_available=False)
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 1, out)
        self.assertIn("expected live, absent", out)
        self.assertIn("was not measured", out)

    def test_vendor_exemption_preserved(self):
        entries = [live_entry("usr-foo", "user", expect_live=False)]
        cap = self.write_capture(
            user=[live_timer("foo.timer")],
            system=[live_timer("apt-daily.timer", fragment_dir=self.VENDOR_DIR)])
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 0, out)

    def test_unmeasured_scope_without_expectations_is_exit_3(self):
        entries = [live_entry("usr-foo", "user", expect_live=False)]
        cap = self.write_capture(user=[live_timer("foo.timer")],
                                 system=[], system_available=False)
        code, out = self.check(entries, capture=cap)
        self.assertEqual(code, 3, out)


class JsonResults(AuditCase):
    """B4: versioned --json results (RED on current source: no --json flag)."""

    def clean_tree(self):
        self.write_timer()
        self.write_service()
        return self.write_registry([repo_timer_entry()])

    def test_json_envelope_shape_on_clean_tree(self):
        code, doc, _ = self.run_json(registry=self.clean_tree())
        self.assertEqual(code, 0, doc)
        self.assert_valid_envelope(doc)
        self.assertEqual(doc["violations"], 0)
        self.assertEqual(doc["unknown"], 0)
        self.assertEqual(doc["results"], [])
        self.assertEqual(doc["coverage"]["entries"], 1)
        self.assertEqual(doc["coverage"]["liveScopesMeasured"], [])
        self.assert_counts_match(doc)

    def test_json_violation_carries_stable_ids(self):
        self.write_timer()
        self.write_service(timeout="TimeoutStartSec=0\n")
        reg = self.write_registry([repo_timer_entry()])
        code, doc, _ = self.run_json(registry=reg)
        self.assertEqual(code, 1, doc)
        self.assert_valid_envelope(doc)
        self.assertEqual(doc["violations"], 1)
        (r,) = [x for x in doc["results"] if x["invariant"] == "I3"]
        self.assertEqual(r["entry"], "timer")
        self.assertEqual(r["unit"], "example.service")
        self.assertEqual(r["severity"], "violation")
        self.assert_counts_match(doc)

    def test_json_scope_unit_identity(self):
        entries = [live_entry("sys-foo", "system", expect_live=True),
                   live_entry("usr-foo", "user", expect_live=False)]
        cap = self.write_capture(user=[live_timer("foo.timer")], system=[])
        code, doc, _ = self.run_json(registry=self.write_registry(entries),
                                     capture=cap)
        self.assertEqual(code, 1, doc)
        self.assert_valid_envelope(doc)
        absents = [x for x in doc["results"]
                   if x["invariant"] == "LIVE" and "absent" in x["message"]]
        self.assertEqual(len(absents), 1, doc)
        self.assertEqual(absents[0]["entry"], "sys-foo")
        self.assertEqual(absents[0]["scope"], "system")
        self.assertEqual(absents[0]["unit"], "foo.timer")
        self.assertEqual(absents[0]["severity"], "violation")
        self.assertEqual(sorted(doc["coverage"]["liveScopesMeasured"]),
                         ["system", "user"])

    def test_json_unknown_suffix_is_never_clean(self):
        self.write_timer()
        self.write_service(timeout="TimeoutStartSec=10 lightyears\n")
        reg = self.write_registry([repo_timer_entry()])
        code, doc, _ = self.run_json(registry=reg)
        self.assertEqual(code, 1, doc)
        self.assert_valid_envelope(doc)
        self.assertGreaterEqual(doc["violations"] + doc["unknown"], 1, doc)
        self.assert_counts_match(doc)

    def test_json_replay_is_deterministic(self):
        self.write_timer()
        self.write_service(timeout="TimeoutStartSec=0\n")
        (self.root / "systemd/stray.timer").write_text(
            "[Timer]\nOnCalendar=daily\nUnit=stray.service\n")
        reg = self.write_registry([repo_timer_entry()])
        res1 = self.run_raw("--json", registry=reg)
        res2 = self.run_raw("--json", registry=reg)
        self.assertTrue(res1.stdout.strip(), res1.stderr[-1500:])
        json.loads(res1.stdout)  # must parse, or the replay proves nothing
        self.assertEqual((res2.returncode, res2.stdout),
                         (res1.returncode, res1.stdout))

    def test_json_counts_match_results(self):
        self.write_timer()
        self.write_service(timeout="TimeoutStartSec=infinity\n")
        (self.root / "systemd/stray.timer").write_text(
            "[Timer]\nOnCalendar=daily\nUnit=stray.service\n")
        reg = self.write_registry([repo_timer_entry()])
        code, doc, _ = self.run_json(registry=reg)
        self.assertEqual(code, 1, doc)
        self.assert_valid_envelope(doc)
        self.assertGreaterEqual(len(doc["results"]), 2, doc)
        self.assert_counts_match(doc)

    def test_json_malformed_registry_root(self):
        reg = self.write_raw_registry("{not json")
        code, doc, combined = self.run_json(registry=reg)
        self.assertEqual(code, 2, combined)
        self.assert_valid_envelope(doc)
        self.assertEqual(doc["results"][0]["code"], "malformed-registry")
        self.assertIs(doc["results"][0]["retryable"], False)
        self.assertNotIn("Traceback", combined)

    def test_json_malformed_registry_shape(self):
        reg = self.write_raw_registry("[]")
        code, doc, combined = self.run_json(registry=reg)
        self.assertEqual(code, 2, combined)
        self.assert_valid_envelope(doc)
        self.assertEqual(doc["results"][0]["code"], "malformed-registry")
        self.assertNotIn("Traceback", combined)

    def test_json_malformed_capture_root(self):
        cap = self.root / "capture.json"
        cap.write_text("{bad")
        code, doc, combined = self.run_json(
            registry=self.write_registry([live_entry("only", "user")]),
            capture=cap)
        self.assertEqual(code, 2, combined)
        self.assert_valid_envelope(doc)
        self.assertEqual(doc["results"][0]["code"], "malformed-capture")
        self.assertNotIn("Traceback", combined)

    def test_json_malformed_capture_row(self):
        cap = self.root / "capture.json"
        cap.write_text(json.dumps({"scopes": {
            "user": {"available": True, "timers": [{"activeState": "active"}]},
            "system": {"available": True, "timers": []}}}))
        code, doc, combined = self.run_json(
            registry=self.write_registry([live_entry("only", "user")]),
            capture=cap)
        self.assertEqual(code, 2, combined)
        self.assert_valid_envelope(doc)
        self.assertEqual(doc["results"][0]["code"], "malformed-capture")
        self.assertNotIn("Traceback", combined)

    def test_json_schema_row_violation(self):
        reg = self.write_registry([{"id": "x"}])
        code, doc, combined = self.run_json(registry=reg)
        self.assertEqual(code, 1, combined)
        self.assert_valid_envelope(doc)
        self.assertTrue(doc["results"], doc)
        self.assertEqual(doc["results"][0]["code"], "schema-violation")
        self.assertNotIn("Traceback", combined)

    def test_json_unmeasured_scope_is_exit_3(self):
        entries = [live_entry("usr-foo", "user", expect_live=False)]
        cap = self.write_capture(user=[live_timer("foo.timer")],
                                 system=[], system_available=False)
        code, doc, combined = self.run_json(
            registry=self.write_registry(entries), capture=cap)
        self.assertEqual(code, 3, combined)
        self.assert_valid_envelope(doc)
        self.assertEqual(doc["coverage"]["liveScopesMeasured"], ["user"])


class MalformedInputText(AuditCase):
    """B4 text mode: controlled nonzero diagnostics, never tracebacks."""

    def test_registry_root_list_is_controlled(self):
        code, out = self.run_audit(registry=self.write_raw_registry("[]"))
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("registry", out.lower())

    def test_registry_non_object_entry_is_controlled(self):
        code, out = self.run_audit(registry=self.write_raw_registry(
            json.dumps({"schemaVersion": 1, "entries": [42]})))
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("registry", out.lower())

    def test_capture_row_without_unit_is_controlled(self):
        cap = self.root / "capture.json"
        cap.write_text(json.dumps({"scopes": {
            "user": {"available": True, "timers": [{"activeState": "active"}]},
            "system": {"available": True, "timers": []}}}))
        code, out = self.run_audit(registry=self.write_registry([live_entry("only", "user")]),
                             capture=cap)
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("capture", out.lower())

    def test_capture_scopes_wrong_shape_is_controlled(self):
        cap = self.root / "capture.json"
        cap.write_text(json.dumps({"scopes": []}))
        code, out = self.run_audit(registry=self.write_registry([live_entry("only", "user")]),
                             capture=cap)
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)

    def test_unparseable_registry_is_usage_error(self):
        code, out = self.run_audit(registry=self.write_raw_registry("{not json"))
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)

    def test_unparseable_capture_is_usage_error(self):
        cap = self.root / "capture.json"
        cap.write_text("{bad")
        code, out = self.run_audit(registry=self.write_registry([live_entry("only", "user")]),
                             capture=cap)
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)

    def test_missing_fields_row_is_violation(self):
        code, out = self.check([{"id": "x"}])
        self.assertEqual(code, 1, out)
        self.assertIn("missing field", out)
        self.assertNotIn("Traceback", out)


class DryRun(AuditCase):
    """B5: --dry-run is an offline read-only alias (RED: no --dry-run flag)."""

    def dirty_tree(self):
        self.write_timer()
        self.write_service(timeout="TimeoutStartSec=0\n")
        return self.write_registry([repo_timer_entry()])

    def snapshot(self):
        files = sorted(p for p in self.root.rglob("*") if p.is_file())
        return {str(p.relative_to(self.root)): p.read_bytes() for p in files}

    def test_dry_run_matches_offline_audit(self):
        reg = self.dirty_tree()
        code, out = self.run_audit(registry=reg)
        self.assertEqual(code, 1, out)
        dry_code, dry_out = self.run_audit("--dry-run", registry=reg)
        self.assertEqual((dry_code, dry_out), (code, out))

    def test_dry_run_writes_nothing_and_touches_no_host(self):
        reg = self.dirty_tree()
        before = self.snapshot()
        dry_code, dry_out = self.run_audit("--dry-run", registry=reg)
        self.assertEqual(dry_code, 1, dry_out)
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_with_live_capture_is_still_offline(self):
        entries = [live_entry("usr-foo", "user", expect_live=True)]
        cap = self.write_capture(user=[live_timer("foo.timer")], system=[])
        reg = self.write_registry(entries)
        code, out = self.run_audit(registry=reg, capture=cap)
        dry_code, dry_out = self.run_audit("--dry-run", registry=reg, capture=cap)
        self.assertEqual((dry_code, dry_out), (code, out))


class TextCompatibility(AuditCase):
    """B4: default text output stays compatible (controls + help docs)."""

    def test_summary_line_format(self):
        self.write_timer()
        self.write_service()
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 0, out)
        self.assertIn("entries=1 unverified=0 violations=0", out)

    def test_violation_prefixes_stable(self):
        self.write_timer()
        self.write_service(text="[Service]\nType=oneshot\nExecStart=/bin/true\n")
        (self.root / "systemd").mkdir(exist_ok=True)
        (self.root / "systemd/stray.timer").write_text(
            "[Timer]\nOnCalendar=daily\nUnit=stray.service\n")
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I3 timer:", out)
        self.assertIn("I5 systemd/stray.timer", out)

    def test_waived_lines_preserved(self):
        self.write_timer()
        self.write_service(text="[Service]\nType=oneshot\nExecStart=/bin/true\n")
        entry = repo_timer_entry(waivers=[{"check": "bounded-runtime",
                                           "reason": "synthetic waiver control",
                                           "fix": "add TimeoutStartSec"}])
        code, out = self.check([entry])
        self.assertEqual(code, 0, out)
        self.assertIn("WAIVED timer [bounded-runtime]:", out)

    def test_help_exits_clean(self):
        code, out = self.run_audit("--help")
        self.assertEqual(code, 0, out)
        self.assertIn("usage", out.lower())

    def test_help_documents_json_and_dry_run(self):
        code, out = self.run_audit("--help")
        self.assertEqual(code, 0, out)
        self.assertIn("--json", out)
        self.assertIn("--dry-run", out)


class RetainedChecks(AuditCase):
    """B2/B3: plugin-job, tracked-timer and transition checks stay load-bearing."""

    def test_tracked_timer_still_must_be_registered(self):
        if shutil.which("git") is None:
            self.skipTest("git is required for the tracked-timer control")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True,
                       capture_output=True, env=self.env)
        stray = self.root / "systemd/stray.timer"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("[Timer]\nOnCalendar=daily\nUnit=stray.service\n")
        git_env = dict(self.env, GIT_CONFIG_NOSYSTEM="1", HOME=str(self.tmp),
                       GIT_AUTHOR_NAME="synth", GIT_AUTHOR_EMAIL="s@s",
                       GIT_COMMITTER_NAME="synth", GIT_COMMITTER_EMAIL="s@s")
        subprocess.run(["git", "-C", str(self.root), "add", "systemd/stray.timer"],
                       check=True, capture_output=True, env=git_env)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "stray"],
                       check=True, capture_output=True, env=git_env)
        stray.unlink()  # tracked but absent on disk: git inventory must still see it
        self.write_timer()
        self.write_service()
        code, out = self.check([repo_timer_entry()])
        self.assertEqual(code, 1, out)
        self.assertIn("I5 systemd/stray.timer is not in the registry", out)

    def test_plugin_manifest_drift_still_fails(self):
        self.write_manifest("plugins/example/src/manifest.ts", ["*/1 * * * *"])
        code, out = self.check(
            [plugin_entry("plugin", "plugins/example/src/manifest.ts",
                          ["*/5 * * * *"])])
        self.assertEqual(code, 1, out)
        self.assertIn("I4 plugins/example/src/manifest.ts", out)

    def test_dangling_transition_still_fails(self):
        entry = repo_timer_entry(supersededBy="absent",
                                 retireGate="synthetic gate")
        self.write_timer()
        self.write_service()
        code, out = self.check([entry])
        self.assertEqual(code, 1, out)
        self.assertIn("I6 timer: supersededBy 'absent' does not exist", out)

    def test_detector_write_still_refused(self):
        entry = make_entry("det", kind="plugin-job", role="detector",
                           writes=["fs:anything"],
                           source={"type": "repo",
                                   "manifest": "plugins/example/src/manifest.ts"},
                           schedule={"jobs": []})
        code, out = self.check([entry])
        self.assertEqual(code, 1, out)
        self.assertIn("a detector must not write", out)


if __name__ == "__main__":
    unittest.main()
