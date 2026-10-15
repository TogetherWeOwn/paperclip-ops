"""Tests for the host timer registry gate and the read-only capture (case_30b51a0f).

No network, no credentials, no systemd: the capture is driven through a fake
`systemctl` on PATH. Every invariant has a positive control: a mutation of the real
tree or registry that must turn the gate red, so a gate that has gone vacuous fails
here instead of passing quietly.
"""

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
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from public_snapshot_test_support import private_artifact_absent

PRIVATE_REGISTRY_ABSENT = private_artifact_absent(
    REPO, "ops/case_30b51a0f/host-timer-registry.json")
PRIVATE_REGISTRY_REASON = (
    "private installed host-timer inventory is excluded from the fresh public "
    "snapshot; synthetic invariant, capture and refusal controls remain enabled")
CHECK = HERE / "host_timer_registry_check.py"
CAPTURE = HERE / "host_timer_capture.py"
SECRET = "SYNTHETIC_DO_NOT_PRINT_7f3a"

sys.path.insert(0, str(HERE))
import host_timer_capture as cap  # noqa: E402

COPY_PATHS = ["systemd", "ops/case_4ea7d92e/systemd", "ops/case_e77a1e99/systemd", "ops/case_0ddff3ec/systemd",
              "gh-event-capture/systemd",
              "plugins/model-selection/src/manifest.ts", "plugins/cliproxy-insight/src/manifest.ts",
              "plugins/dispatch/dist/manifest.js", ".github/workflows/disclosure-audit.yml",
              ".github/workflows/ci.yml", "ops/case_30b51a0f/host-timer-registry.json",
              "ops/case_34c2c99f/operator-prune-regen-dirs.timer", "ops/case_34c2c99f/operator-prune-regen-dirs.service"]


def run_check(root, *extra, registry=None):
    cmd = [sys.executable, str(CHECK), "--root", str(root)]
    if registry:
        cmd += ["--registry", str(registry)]
    res = subprocess.run(cmd + list(extra), capture_output=True, text=True)
    return res.returncode, res.stdout + res.stderr


class TreeCase(unittest.TestCase):
    def setUp(self):
        if PRIVATE_REGISTRY_ABSENT:
            self.skipTest(PRIVATE_REGISTRY_REASON)
        self.tmp = Path(tempfile.mkdtemp(prefix="case_b56e4585-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "repo"
        for rel in COPY_PATHS:
            src, dst = REPO / rel, self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy(src, dst)
        self.reg_path = self.root / "ops/case_30b51a0f/host-timer-registry.json"

    def registry(self):
        return json.loads(self.reg_path.read_text())

    def write_registry(self, doc):
        self.reg_path.write_text(json.dumps(doc))

    def entry(self, doc, eid):
        return next(e for e in doc["entries"] if e["id"] == eid)

    def red(self, needle, *extra):
        code, out = run_check(self.root, *extra)
        self.assertEqual(code, 1, out)
        self.assertIn(needle, out)


class RealTree(unittest.TestCase):
    def setUp(self):
        if PRIVATE_REGISTRY_ABSENT:
            self.skipTest(PRIVATE_REGISTRY_REASON)

    def test_red_main_poll_is_registered_but_not_installed(self):
        doc = json.loads((REPO / "ops/case_30b51a0f/host-timer-registry.json").read_text())
        e = next(x for x in doc["entries"] if x["id"] == "case_1a1b67a4-red-main-poll")
        self.assertEqual(e["source"], {
            "type": "repo",
            "timer": "ops/case_e77a1e99/systemd/paperclip-case_1a1b67a4-red-main-poll.timer",
        })
        self.assertEqual(e["schedule"], {"OnCalendar": "*:0,15,30,45"})
        self.assertEqual(e["role"], "detector")
        self.assertEqual(e["writes"], [])
        # Read-only timer (live 2026-10-06: the deployed run gate refuses
        # runless agent comment writes): proposals go to host files/journal,
        # never the board. Board delivery is the runful routine's.
        self.assertEqual(e["reports"], ["host:proposal-files", "host:journal"])
        self.assertEqual(e["state"], "planned")
        self.assertFalse(e["expectLive"])
        self.assertTrue(e["verified"])

    def test_removing_red_main_registration_retrips_incident(self):
        doc = json.loads((REPO / "ops/case_30b51a0f/host-timer-registry.json").read_text())
        doc["entries"] = [e for e in doc["entries"] if e["id"] != "case_1a1b67a4-red-main-poll"]
        with tempfile.TemporaryDirectory(prefix="case_1a1b67a4-registry-") as tmp:
            registry = Path(tmp) / "registry.json"
            registry.write_text(json.dumps(doc))
            code, out = run_check(REPO, registry=registry)
        self.assertEqual(code, 1, out)
        self.assertIn("I5 ops/case_e77a1e99/systemd/paperclip-case_1a1b67a4-red-main-poll.timer "
                      "is not in the registry", out)

    def test_real_registry_is_clean_against_the_real_repo(self):
        code, out = run_check(REPO)
        self.assertEqual(code, 0, out)

    def test_real_registry_is_not_at_end_state(self):
        # Transitions are declared on purpose; the end-state gate must see them.
        code, out = run_check(REPO, "--end-state")
        self.assertEqual(code, 1, out)
        self.assertIn("END recovery-writer", out)

    def test_recovery_writer_is_present_but_not_installed(self):
        # case_406818da: systemd/paperclip-recovery-writer.timer shipped (#520)
        # while the registry still called the writer planned/card-only, which
        # failed every Offline suite with I5. The entry must cite the repo
        # timer file, match its schedule, and read as approved (planned) but
        # NOT installed (expectLive false: the operator installs the reviewed
        # bundle, never this checkout).
        doc = json.loads((REPO / "ops/case_30b51a0f/host-timer-registry.json").read_text())
        e = next(x for x in doc["entries"] if x["id"] == "recovery-writer")
        self.assertEqual(e["source"],
                         {"type": "repo", "timer": "systemd/paperclip-recovery-writer.timer"})
        self.assertTrue((REPO / "systemd/paperclip-recovery-writer.timer").is_file())
        self.assertEqual(e["schedule"], {"OnBootSec": "7m", "OnUnitActiveSec": "10m"})
        self.assertEqual(e["state"], "planned")
        self.assertFalse(e["expectLive"])
        self.assertTrue(e["verified"])


class Invariants(TreeCase):
    def test_copy_is_clean(self):
        self.assertEqual(run_check(self.root)[0], 0)

    def test_unregistered_timer_file_fails(self):
        (self.root / "systemd/paperclip-new-sweep.timer").write_text(
            "[Timer]\nOnCalendar=hourly\nUnit=paperclip-new-sweep.service\n")
        self.red("I5 systemd/paperclip-new-sweep.timer is not in the registry")

    def test_recovery_writer_citation_removed_retrips_incident(self):
        # Positive control for case_406818da: reverting the entry to planned/card
        # while the timer file ships must fail with the incident signature.
        doc = self.registry()
        self.entry(doc, "recovery-writer")["source"] = {"type": "planned", "card": "case_db409e65"}
        self.write_registry(doc)
        self.red("I5 systemd/paperclip-recovery-writer.timer is not in the registry")

    def test_ci_schedule_entry_removed_fails(self):
        doc = self.registry()
        doc["entries"] = [e for e in doc["entries"] if e["id"] != "gha-ci-full-suite"]
        self.write_registry(doc)
        self.red("I5 .github/workflows/ci.yml has a cron schedule")

    def test_ci_schedule_drift_fails(self):
        wf = self.root / ".github/workflows/ci.yml"
        text = wf.read_text()
        self.assertIn("41 2 * * *", text)
        wf.write_text(text.replace("41 2 * * *", "43 2 * * *"))
        self.red("I4 .github/workflows/ci.yml")

    def test_second_primary_for_a_purpose_fails(self):
        doc = self.registry()
        del self.entry(doc, "host-db-backup")["redundancyOf"]
        self.write_registry(doc)
        self.red("I1 purpose 'db-backup'")

    def test_two_primary_writers_of_one_target_fail(self):
        doc = self.registry()
        self.entry(doc, "recovery-writer")["writes"].append("board:tree-hold")
        self.write_registry(doc)
        self.red("I2 target 'board:tree-hold'")

    def test_native_writers_do_not_trip_the_writer_rule(self):
        doc = self.registry()
        self.entry(doc, "native-stranded-recovery")["writes"].append("board:tree-hold")
        self.write_registry(doc)
        self.assertEqual(run_check(self.root)[0], 0)

    def test_unbounded_service_fails(self):
        svc = self.root / "ops/case_4ea7d92e/systemd/paperclip-case_d376bf4e-sweeper-watchdog.service"
        svc.write_text("\n".join(l for l in svc.read_text().splitlines()
                                 if not l.startswith("TimeoutStartSec")) + "\n")
        self.red("I3 case_d376bf4e-sweeper-watchdog")

    def test_waiver_is_what_lets_the_quota_controller_pass(self):
        doc = self.registry()
        del self.entry(doc, "cliproxy-quota-controller")["waivers"]
        self.write_registry(doc)
        self.red("I3 cliproxy-quota-controller")

    def test_self_retrying_oneshot_fails(self):
        svc = self.root / "systemd/paperclip-liveness-reconciler.service"
        svc.write_text(svc.read_text().replace("[Service]\n", "[Service]\nRestart=on-failure\n", 1))
        self.red("I3 liveness-reconciler")

    def test_schedule_drift_fails(self):
        t = self.root / "systemd/paperclip-host-db-backup.timer"
        t.write_text(t.read_text().replace("*:27:00", "*:07:00"))
        self.red("I4 host-db-backup: schedule")

    def test_new_plugin_job_must_be_registered(self):
        m = self.root / "plugins/cliproxy-insight/src/manifest.ts"
        m.write_text(m.read_text() + '\n  schedule: "*/1 * * * *",\n')
        self.red("I4 plugins/cliproxy-insight/src/manifest.ts")

    def test_unregistered_plugin_manifest_fails(self):
        extra = self.root / "plugins/new-plugin/src/manifest.ts"
        extra.parent.mkdir(parents=True)
        extra.write_text('jobs: [{ schedule: "*/5 * * * *" }]\n')
        self.red("I5 plugins/new-plugin/src/manifest.ts")

    def test_workflow_cron_drift_and_new_cron_fail(self):
        wf = self.root / ".github/workflows/disclosure-audit.yml"
        wf.write_text(wf.read_text().replace('"17 3 * * *"', '"*/5 * * * *"'))
        self.red("I4 .github/workflows/disclosure-audit.yml")
        wf.write_text(wf.read_text())
        other = self.root / ".github/workflows/nightly.yml"
        other.write_text('on:\n  schedule:\n    - cron: "5 4 * * *"\n')
        code, out = run_check(self.root)
        self.assertIn("I5 .github/workflows/nightly.yml", out)

    def test_dangling_and_cross_purpose_supersession_fail(self):
        doc = self.registry()
        self.entry(doc, "sweep-auto")["supersededBy"] = "no-such-entry"
        self.entry(doc, "agent-error-sweep")["supersededBy"] = "cliproxy-quota-controller"
        self.write_registry(doc)
        code, out = run_check(self.root)
        self.assertEqual(code, 1, out)
        self.assertIn("I6 sweep-auto: supersededBy 'no-such-entry' does not exist", out)
        self.assertIn("I6 agent-error-sweep: supersededBy 'cliproxy-quota-controller' serves purpose", out)

    def test_supersession_needs_a_retire_gate_and_redundancy_needs_a_reason(self):
        doc = self.registry()
        del self.entry(doc, "board-guard")["retireGate"]
        del self.entry(doc, "host-db-backup")["redundancyReason"]
        self.write_registry(doc)
        code, out = run_check(self.root)
        self.assertEqual(code, 1, out)
        self.assertIn("I6 board-guard: supersededBy needs a retireGate", out)
        self.assertIn("I6 host-db-backup: redundancyOf needs a redundancyReason", out)

    def test_detector_may_not_write(self):
        doc = self.registry()
        self.entry(doc, "case_d376bf4e-git-remote-sweep")["writes"] = ["fs:anything"]
        self.write_registry(doc)
        self.red("a detector must not write")

    def test_waiver_without_a_fix_is_refused(self):
        doc = self.registry()
        del self.entry(doc, "cliproxy-quota-controller")["waivers"][0]["fix"]
        self.write_registry(doc)
        self.red("a waiver needs check, reason and fix")

    def test_unreadable_registry_is_usage_error_not_clean(self):
        self.reg_path.write_text("{not json")
        self.assertEqual(run_check(self.root)[0], 2)


class SyntheticMixin:
    def synthetic(self, **over):
        e = {"id": "only", "kind": "systemd-timer", "scope": "user", "source": {"type": "host-only"},
             "purpose": "p", "role": "writer", "writes": ["t"], "reports": [], "schedule": {},
             "bound": "x", "receipt": "x", "state": "active", "expectLive": "unknown",
             "verified": True, "owner": "x", "unit": "only.timer"}
        e.update(over)
        return {"schemaVersion": 1, "entries": [e]}

    def run_synth(self, doc, *extra):
        tmp = Path(tempfile.mkdtemp(prefix="case_b56e4585-end-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        reg = tmp / "r.json"
        reg.write_text(json.dumps(doc))
        (tmp / ".github/workflows").mkdir(parents=True)
        return run_check(tmp, "--end-state", *extra, registry=reg)


class SyntheticInvariants(SyntheticMixin, unittest.TestCase):
    """Exercise all registry invariants without any installed-host inventory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="synthetic-registry-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = self.tmp / "registry.json"
        self.timer = self.tmp / "systemd/example.timer"
        self.service = self.tmp / "systemd/example.service"
        self.workflow = self.tmp / ".github/workflows/example.yml"
        self.plugin = self.tmp / "plugins/example/src/manifest.ts"
        for path in (self.timer, self.workflow, self.plugin):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.timer.write_text("[Timer]\nOnCalendar=hourly\nUnit=example.service\n")
        self.service.write_text("[Service]\nType=oneshot\nTimeoutStartSec=30\nExecStart=/bin/true\n")
        self.workflow.write_text('on:\n  schedule:\n    - cron: "5 4 * * *"\n')
        self.plugin.write_text('jobs: [{ schedule: "*/5 * * * *" }]\n')
        self.doc = {"schemaVersion": 1, "entries": [
            self.synthetic(id="timer", source={"type": "repo", "timer": "systemd/example.timer"},
                           schedule={"OnCalendar": "hourly"})["entries"][0],
            self.synthetic(id="plugin", kind="plugin-job", purpose="plugin-observation",
                           role="detector", writes=[], source={"type": "repo", "manifest": "plugins/example/src/manifest.ts"},
                           schedule={"jobs": ["*/5 * * * *"]})["entries"][0],
            self.synthetic(id="workflow", kind="gh-actions-schedule", purpose="workflow-observation",
                           role="detector", writes=[], source={"type": "repo", "workflow": ".github/workflows/example.yml"},
                           schedule={"cron": "5 4 * * *"})["entries"][0],
        ]}
        code, out = self.check()
        self.assertEqual(code, 0, "synthetic baseline must pass before any mutation: " + out)

    def check(self):
        self.reg.write_text(json.dumps(self.doc))
        return run_check(self.tmp, registry=self.reg)

    def red(self, needle):
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(needle, out)

    def test_duplicate_primary_purpose_is_rejected(self):
        self.doc["entries"].append(self.synthetic(id="second", writes=["other"])["entries"][0])
        self.red("I1 purpose 'p'")

    def test_duplicate_writer_is_rejected_but_native_writer_is_exempt(self):
        second = self.synthetic(id="second", purpose="other")["entries"][0]
        self.doc["entries"].append(second)
        self.red("I2 target 't'")
        second["kind"] = "native"
        self.assertEqual(self.check()[0], 0)

    def test_runtime_bound_and_waiver_are_load_bearing(self):
        self.service.write_text("[Service]\nType=oneshot\nExecStart=/bin/true\n")
        self.red("I3 timer")
        self.doc["entries"][0]["waivers"] = [{
            "check": "bounded-runtime", "reason": "synthetic waiver control", "fix": "add timeout"}]
        self.assertEqual(self.check()[0], 0)
        del self.doc["entries"][0]["waivers"][0]["fix"]
        self.red("a waiver needs check, reason and fix")

    def test_self_retrying_oneshot_is_rejected(self):
        self.service.write_text(self.service.read_text() + "Restart=on-failure\n")
        self.red("I3 timer")

    def test_all_schedule_sources_are_checked(self):
        for path, before, after, needle in (
            (self.timer, "hourly", "daily", "I4 timer"),
            (self.plugin, "*/5", "*/1", "I4 plugins/example/src/manifest.ts"),
            (self.workflow, "5 4", "6 4", "I4 .github/workflows/example.yml"),
        ):
            with self.subTest(path=path.name):
                original = path.read_text()
                path.write_text(original.replace(before, after))
                self.red(needle)
                path.write_text(original)

    def test_unregistered_sources_are_rejected(self):
        for entry in list(self.doc["entries"]):
            with self.subTest(kind=entry["kind"]):
                self.doc["entries"].remove(entry)
                self.red("I5 ")
                self.doc["entries"].append(entry)

    def test_invalid_transitions_are_rejected(self):
        entry = self.doc["entries"][0]
        entry["supersededBy"] = "absent"
        self.red("does not exist")
        entry["supersededBy"] = "plugin"
        self.red("serves purpose")
        self.red("supersededBy needs a retireGate")
        del entry["supersededBy"]
        entry["redundancyOf"] = "plugin"
        self.red("redundancyOf needs a redundancyReason")

    def test_detector_cannot_write(self):
        self.doc["entries"][1]["writes"] = ["fs:synthetic"]
        self.red("a detector must not write")

    def test_unreadable_registry_never_passes(self):
        self.reg.write_text("{not json")
        self.assertEqual(run_check(self.tmp, registry=self.reg)[0], 2)


class EndState(SyntheticMixin, unittest.TestCase):
    def test_finished_consolidation_passes(self):
        self.assertEqual(self.run_synth(self.synthetic())[0], 0)

    def test_unverified_entry_blocks_the_end_state(self):
        code, out = self.run_synth(self.synthetic(verified=False))
        self.assertEqual(code, 1, out)
        self.assertIn("END only: not verified", out)

    def test_planned_entry_blocks_the_end_state(self):
        self.assertEqual(self.run_synth(self.synthetic(state="planned"))[0], 1)


def capture_fixture(**over):
    timer = {"unit": "only.timer", "activeState": "active", "unitFileState": "enabled",
             "fragmentPath": "/home/u/.config/systemd/user/only.timer",
             "service": {"result": "success"}}
    timer.update(over)
    return {"scopes": {"user": {"available": True, "timers": [timer]},
                       "system": {"available": True, "timers": []}}}


class Live(SyntheticMixin, unittest.TestCase):
    def run_live(self, capture, doc=None):
        tmp = Path(tempfile.mkdtemp(prefix="case_b56e4585-live-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        reg, cap_path = tmp / "r.json", tmp / "c.json"
        reg.write_text(json.dumps(doc or self.synthetic()))
        cap_path.write_text(json.dumps(capture))
        (tmp / ".github/workflows").mkdir(parents=True)
        return run_check(tmp, "--live", str(cap_path), registry=reg)

    def test_matching_host_is_clean(self):
        self.assertEqual(self.run_live(capture_fixture())[0], 0)

    def test_unregistered_host_timer_fails(self):
        cap = capture_fixture()
        cap["scopes"]["user"]["timers"].append(
            {"unit": "mystery.timer", "activeState": "active", "unitFileState": "enabled",
             "fragmentPath": "/home/u/.config/systemd/user/mystery.timer", "service": {}})
        code, out = self.run_live(cap)
        self.assertEqual(code, 1, out)
        self.assertIn("user/mystery.timer: on the host, not in the registry", out)

    def test_vendor_timer_is_not_ours_to_classify(self):
        cap = capture_fixture()
        cap["scopes"]["system"]["timers"].append(
            {"unit": "apt-daily.timer", "activeState": "active", "unitFileState": "enabled",
             "fragmentPath": "/usr/lib/systemd/system/apt-daily.timer", "service": {}})
        self.assertEqual(self.run_live(cap)[0], 0)

    def test_retired_timer_still_present_fails(self):
        code, out = self.run_live(capture_fixture(), self.synthetic(state="retired"))
        self.assertEqual(code, 1, out)
        self.assertIn("registry says retired, host still has it", out)

    def test_failed_last_run_fails(self):
        code, out = self.run_live(capture_fixture(service={"result": "exit-code"}))
        self.assertEqual(code, 1, out)
        self.assertIn("last run result 'exit-code'", out)

    def test_expected_live_but_absent_fails(self):
        cap = capture_fixture()
        cap["scopes"]["user"]["timers"] = []
        code, out = self.run_live(cap, self.synthetic(expectLive=True))
        self.assertEqual(code, 1, out)
        self.assertIn("expected live, absent from the host capture", out)

    def test_unmeasured_scope_is_not_a_clean_result(self):
        cap = capture_fixture()
        cap["scopes"]["system"] = {"available": False, "reason": "no bus", "timers": []}
        code, out = self.run_live(cap)
        self.assertEqual(code, 3, out)

    def test_unmeasured_scope_does_not_mask_a_real_violation(self):
        cap = capture_fixture(service={"result": "exit-code"})
        cap["scopes"]["system"] = {"available": False, "reason": "no bus", "timers": []}
        self.assertEqual(self.run_live(cap)[0], 1)


FAKE_SYSTEMCTL = r"""#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$*" in
  *"--user"*) scope=user ;;
  *) scope=system ;;
esac
[ "$FAKE_FAIL_USER" = 1 ] && [ "$scope" = user ] && { echo "Failed to connect to bus" >&2; exit 1; }
case "$*" in
  *list-units*) [ "$scope" = user ] && echo "wd.timer loaded active waiting Watchdog"; exit 0 ;;
  *list-unit-files*) [ "$scope" = user ] && echo "wd.timer enabled enabled"; exit 0 ;;
  *" show wd.timer"*)
    echo "Id=wd.timer"; echo "ActiveState=active"; echo "UnitFileState=enabled"; echo "Unit=wd.service"
    echo "FragmentPath=$FAKE_UNIT_DIR/wd.timer"; echo "TimersCalendar={ OnCalendar=*-*-* 0/2:17:00 ; next_elapse=x }"
    echo "Environment=TOKEN=$FAKE_SECRET"; exit 0 ;;
  *" show wd.service"*)
    echo "Id=wd.service"; echo "Type=oneshot"; echo "Result=success"; echo "ExecMainStatus=0"
    echo "TimeoutStartUSec=5min"; echo "Restart=no"
    echo "EnvironmentFiles=$FAKE_UNIT_DIR/wd.env (ignore_errors=no)"
    echo "ExecStart={ path=/usr/bin/check ; argv[]=/usr/bin/check --token=$FAKE_SECRET ; ignore_errors=no }"
    echo "FragmentPath=$FAKE_UNIT_DIR/wd.service"; exit 0 ;;
esac
exit 0
"""


class Capture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="case_b56e4585-cap-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "systemctl"
        fake.write_text(FAKE_SYSTEMCTL)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls.log"
        (self.tmp / "wd.timer").write_text("[Timer]\n")
        (self.tmp / "wd.service").write_text("[Service]\n")
        (self.tmp / "wd.env").write_text(f"PAPERCLIP_AGENT_API_KEY={SECRET}\n")
        (self.tmp / "etc/cron.d").mkdir(parents=True)
        (self.tmp / "etc/cron.d/backup").write_text("")

    def capture(self, **env):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_LOG": str(self.log), "FAKE_SECRET": SECRET,
             "FAKE_UNIT_DIR": str(self.tmp), "HOME": str(self.tmp)}
        e.update(env)
        res = subprocess.run([sys.executable, str(CAPTURE), "--etc-root", str(self.tmp / "etc")],
                             capture_output=True, text=True, env=e)
        return res, (json.loads(res.stdout) if res.returncode == 0 else None)

    def test_shape_and_values(self):
        res, doc = self.capture()
        self.assertEqual(res.returncode, 0, res.stderr)
        rec = doc["scopes"]["user"]["timers"][0]
        self.assertEqual(rec["unit"], "wd.timer")
        self.assertEqual(rec["service"]["execPath"], "/usr/bin/check")
        self.assertEqual(rec["service"]["result"], "success")
        self.assertEqual(rec["service"]["environmentFiles"], [str(self.tmp / "wd.env")])
        self.assertTrue(rec["fragmentSha256"])
        self.assertEqual(doc["cron"]["etc"]["cron.d"], ["backup"])

    def test_no_secret_value_reaches_the_output(self):
        res, _ = self.capture()
        self.assertNotIn(SECRET, res.stdout + res.stderr)

    def test_environment_property_is_never_requested(self):
        self.capture()
        calls = self.log.read_text().splitlines()
        shows = [c for c in calls if " show " in f" {c} "]
        self.assertTrue(shows)
        for c in shows:
            props = c.split("-p ")[-1].split(",")
            self.assertNotIn("Environment", props, c)

    def test_only_read_verbs_are_invoked(self):
        self.capture()
        for call in self.log.read_text().splitlines():
            verb = [a for a in call.split() if not a.startswith("-")][0]
            self.assertIn(verb, cap.ALLOWED_VERBS, call)

    def test_allow_list_refuses_a_mutating_verb(self):
        for verb in ("start", "stop", "restart", "enable", "disable", "daemon-reload", "mask"):
            with self.assertRaises(ValueError):
                cap.sysctl("user", verb, "x.timer")

    def test_unreadable_scope_is_recorded_not_hidden(self):
        res, doc = self.capture(FAKE_FAIL_USER="1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(doc["scopes"]["user"]["available"])
        self.assertIn("bus", doc["scopes"]["user"]["reason"])
        self.assertTrue(doc["scopes"]["system"]["available"])

    def test_synthetic_capture_feeds_the_checker_end_to_end(self):
        _, doc = self.capture(FAKE_FAIL_USER="1")
        root = self.tmp / "synthetic-repo"
        (root / ".github/workflows").mkdir(parents=True)
        registry, capture = root / "registry.json", root / "capture.json"
        registry.write_text(json.dumps(SyntheticMixin().synthetic()))
        capture.write_text(json.dumps(doc))
        code, out = run_check(root, "--live", str(capture), registry=registry)
        self.assertEqual(code, 3, out)

    def test_capture_feeds_the_checker_end_to_end(self):
        if PRIVATE_REGISTRY_ABSENT:
            self.skipTest(PRIVATE_REGISTRY_REASON)
        _, doc = self.capture(FAKE_FAIL_USER="1")
        path = self.tmp / "cap.json"
        path.write_text(json.dumps(doc))
        code, out = run_check(REPO, "--live", str(path))
        self.assertEqual(code, 3, out)


if __name__ == "__main__":
    unittest.main()
