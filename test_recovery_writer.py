#!/usr/bin/env python3
# ===========================================================================
# test_recovery_writer.py — offline hostile suite for recovery_writer.py.
#
# Written to FAIL a plausible-but-wrong implementation, not to confirm the one
# that exists. Every section below is either a drift the old timers actually
# exhibited, or the specific shortcut a reimplementation would take.
#
# Deterministic: no network, no credentials, no clock reads. Fixture rows are
# inline so the suite survives the live board changing underneath it.
#
#   python3 -m unittest -v test_recovery_writer.py
# ===========================================================================

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import mock_open, patch

import recovery_writer as rw


def setUpModule():
    # Every client in this suite uses a test-only base (board.invalid or
    # loopback); the hatch lets the allowlist layer through without a host
    # file so the policy assertions below see the writer, not the gate.
    os.environ["PAPERCLIP_API_ALLOWLIST_TEST"] = "1"


def tearDownModule():
    os.environ.pop("PAPERCLIP_API_ALLOWLIST_TEST", None)


CEO = "00000000-0000-4000-8000-000000000031"
OWNER_USER = "u-owner-0000-1111-2222-333333333333"
OWNER_AGENT = "a-owner-0000-1111-2222-333333333333"
OTHER_USER = "u-stranger-0000-1111-2222-444444444444"
IMPL = "00000000-0000-4000-8000-000000000003"


def live(identifier: str = "case_8c5fe4cc", **changes):
    base = {
        "id": "00000000-0000-4000-8000-000000000019",
        "identifier": identifier,
        "projectId": "00000000-0000-4000-8000-000000000118",
        "companyId": "00000000-0000-4000-8000-000000000181",
        "status": "todo",
        "assigneeAgentId": None,
        "assigneeUserId": None,
        "unblockDescriptor": None,
        "treeControlState": {},
    }
    base.update(changes)
    return base


def proposal(mutation: str, **changes):
    base = {
        "issueId": "00000000-0000-4000-8000-000000000019",
        "identifier": "case_8c5fe4cc",
        "detector": "test-detector",
        "reason": "test_reason",
        "mutation": mutation,
        "before": {"status": "todo"},
        "after": {"status": "todo"},
        "note": "test proposal",
    }
    base.update(changes)
    return base


def decide(mutation: str, live_row=None, ceo=CEO, **kw):
    prop = proposal(mutation, **kw.pop("proposal", {}))
    prop.update({k: v for k, v in kw.items() if k in prop})
    extra = {k: v for k, v in kw.items() if k not in prop}
    prop.update(extra)
    return rw.decide(prop, live_row or live(), ceo,
                     kw.get("owner_user", OWNER_USER), kw.get("owner_agent", OWNER_AGENT))


def json_response(*_args, **_kwargs):
    # Fresh valid responses make a missing guard fail assertions, not mock I/O.
    return io.BytesIO(b'{"ok": true}')


class HostAdmission(unittest.TestCase):
    MEM = "MemTotal: 33554432 kB\nMemFree: 1 kB\nMemAvailable: 8388608 kB\n"
    HEALTHY = {
        "cpuSomeAvg60": 5.0,
        "ioSomeAvg60": 4.0,
        "memoryFullAvg60": 0.5,
        "load1": 49.1,
        "load5": 49.1,
        "load15": 49.1,
    }

    def sample(self, **changes):
        result = dict(self.HEALTHY)
        result.update(changes)
        return result

    def test_healthy_psi_admits_when_load1_and_load15_exceed_96(self):
        data = {
            "/proc/pressure/cpu": "some avg10=5.00 avg60=5.00 avg300=4.00 total=10\n",
            "/proc/pressure/io": "some avg10=4.00 avg60=4.00 avg300=3.00 total=8\n",
            "/proc/pressure/memory": (
                "some avg10=1.00 avg60=1.00 avg300=1.00 total=2\n"
                "full avg10=0.50 avg60=0.50 avg300=0.40 total=1\n"
            ),
            "/proc/loadavg": "120.1 96.0 144.1 32/100 123\n",
        }
        pressure = rw.host_pressure.read_pressure(data.__getitem__)
        sample = rw.host_admission(pressure, self.MEM)
        self.assertTrue(sample["allowed"])
        self.assertEqual(sample["load1"], 120.1)
        self.assertEqual(sample["load5"], 96.0)
        self.assertEqual(sample["load15"], 144.1)
        self.assertEqual(sample["memAvailableBytes"], 8 * 1024**3)

    def test_psi_thresholds_are_strict_and_pressure_vetoes_independently(self):
        for field, value, reason in (
            ("cpuSomeAvg60", 30.1, "cpu"),
            ("ioSomeAvg60", 30.1, "io"),
            ("memoryFullAvg60", 5.1, "memory_pressure"),
        ):
            with self.subTest(field=field):
                sample = rw.host_admission(self.sample(**{field: value}), self.MEM)
                self.assertFalse(sample["allowed"])
                self.assertEqual(sample["reason"], reason)
        boundary = rw.host_admission(
            self.sample(cpuSomeAvg60=30, ioSomeAvg60=30, memoryFullAvg60=5, load5=96),
            self.MEM,
        )
        self.assertTrue(boundary["allowed"])

    def test_load5_hard_ceiling_refuses_even_with_healthy_psi(self):
        sample = rw.host_admission(self.sample(load5=96.1), self.MEM)
        self.assertFalse(sample["allowed"])
        self.assertEqual(sample["reason"], "load5")

    def test_memavailable_not_free_or_total_controls_admission(self):
        sample = rw.host_admission(
            self.HEALTHY, self.MEM.replace("8388608", "8388607")
        )
        self.assertFalse(sample["allowed"])
        self.assertEqual(sample["reason"], "memory")

    def test_invalid_pressure_never_becomes_headroom(self):
        for pressure in (
            None,
            {},
            self.sample(cpuSomeAvg60=float("nan")),
            self.sample(ioSomeAvg60=float("inf")),
            self.sample(memoryFullAvg60=-1),
            self.sample(load5=float("nan")),
            self.sample(load15=True),
            self.sample(cpuSomeAvg60=101),
        ):
            with self.subTest(pressure=pressure), self.assertRaises(rw.WriterError):
                rw.host_admission(pressure, self.MEM)

    def test_missing_or_malformed_psi_and_load_never_become_zero(self):
        base = {
            "/proc/pressure/cpu": "some avg10=1 avg60=1 avg300=1 total=1\n",
            "/proc/pressure/io": "some avg10=1 avg60=1 avg300=1 total=1\n",
            "/proc/pressure/memory": "full avg10=1 avg60=1 avg300=1 total=1\n",
            "/proc/loadavg": "1 1 1 1/1 1\n",
        }
        invalid = (
            ("/proc/pressure/cpu", ""),
            ("/proc/pressure/io", "some avg10=1 avg300=1 total=1\n"),
            ("/proc/pressure/memory", "full avg10=1 avg60=nan avg300=1 total=1\n"),
            ("/proc/loadavg", "1 invalid 1 1/1 1\n"),
            ("/proc/loadavg", "1 1\n"),
        )
        for path, text in invalid:
            data = dict(base)
            data[path] = text
            with self.subTest(path=path, text=text), self.assertRaises(rw.host_pressure.PressureError):
                rw.host_pressure.read_pressure(data.__getitem__)

    def test_oserror_reading_pressure_files_becomes_pressure_error(self):
        def unreadable(_path):
            raise OSError("synthetic PSI read failure")

        with self.assertRaises(rw.host_pressure.PressureError):
            rw.host_pressure.read_pressure(unreadable)

    def test_missing_duplicate_or_malformed_memavailable_refuses(self):
        for meminfo in (
            "MemFree: 33554432 kB\n",
            self.MEM + self.MEM,
            "MemAvailable: NaN kB\n",
            "MemAvailable: -1 kB\n",
            "MemAvailable: 8388608 MB\n",
            "MemAvailable: 8388608\n",
        ):
            with self.subTest(meminfo=meminfo), self.assertRaises(rw.WriterError):
                rw.host_admission(self.HEALTHY, meminfo)

    def test_unreadable_pressure_or_memory_refuses_before_any_request(self):
        client = rw.PaperclipClient("https://board.invalid", "synthetic-key")
        for pressure_error, memory_error in (
            (rw.host_pressure.PressureError("pressure unavailable"), None),
            (None, OSError("memory unavailable")),
            (None, UnicodeError("bad telemetry")),
        ):
            with self.subTest(pressure_error=pressure_error, memory_error=memory_error):
                with patch.object(rw.host_pressure, "read_pressure",
                                  side_effect=pressure_error, return_value=self.HEALTHY), \
                        patch("builtins.open", mock_open(read_data=self.MEM)) as opened, \
                        patch.object(rw.urllib.request, "urlopen", side_effect=json_response) as send:
                    if memory_error:
                        opened.side_effect = memory_error
                    with self.assertRaises(rw.WriterError):
                        client.patch("/api/issues/x", {"assigneeAgentId": IMPL})
                    send.assert_not_called()

    def test_every_write_rechecks_pressure_and_stops_the_next_write(self):
        client = rw.PaperclipClient("https://board.invalid", "synthetic-key")
        with patch.object(rw.host_pressure, "read_pressure",
                          side_effect=[self.HEALTHY, self.sample(load5=96.1)]), \
                patch("builtins.open", mock_open(read_data=self.MEM)) as opened, \
                patch.object(rw.urllib.request, "urlopen",
                             side_effect=json_response) as send:
            client.patch("/api/issues/x", {"assigneeAgentId": IMPL})
            with self.assertRaises(rw.WriterError):
                client.post("/api/issues/x/comments", {"body": "synthetic"})
            self.assertEqual(send.call_count, 1)
            self.assertEqual(opened.call_count, 2)
            opened.assert_called_with("/proc/meminfo", encoding="ascii")

    def test_get_only_preflight_needs_no_host_admission(self):
        client = rw.PaperclipClient("https://board.invalid", "synthetic-key")
        with patch.object(rw, "read_host_admission") as sample, \
                patch.object(rw.urllib.request, "urlopen",
                             side_effect=json_response) as send:
            client.get("/api/issues/read-only-canary")
            sample.assert_not_called()
            send.assert_called_once()

    def test_apply_cycle_refusal_is_nonzero_with_zero_writes_and_receipt(self):
        rows = [proposal("assign_blocker_owner", targetAgentId=IMPL)]
        pressured = self.sample(load5=96.1)
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump({"proposals": rows}, handle)
            handle.flush()
            with patch.dict(os.environ, {"PAPERCLIP_API_URL": "https://board.invalid",
                                         "PAPERCLIP_API_KEY": "synthetic-key"}), \
                    patch.object(rw.PaperclipClient, "read_live", return_value=live()), \
                    patch.object(rw, "read_host_admission",
                                 return_value=rw.host_admission(pressured, self.MEM)), \
                    patch.object(rw.urllib.request, "urlopen", side_effect=json_response) as send:
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = rw.main(["--proposals", handle.name, "--apply"])
                self.assertEqual(rc, 1)
                send.assert_not_called()
                records = [json.loads(line) for line in out.getvalue().splitlines()]
                receipt = next(row for row in records if row["type"] == "receipt")
                self.assertIn("refused:host_admission", receipt["apply"])
                self.assertEqual(records[-1]["applied"], 0)
                self.assertEqual(records[-1]["failed"], 1)

    def test_dry_run_and_cap_zero_remain_keyless_or_observe_only(self):
        rows = [dict(proposal("assign_blocker_owner", targetAgentId=IMPL), live=live())]
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump({"proposals": rows}, handle)
            handle.flush()
            for args in ([], ["--apply", "--max-repairs", "0"]):
                with self.subTest(args=args), \
                        patch.dict(os.environ, {"PAPERCLIP_API_URL": "https://board.invalid",
                                                 "PAPERCLIP_API_KEY": "synthetic-key"}), \
                        patch.object(rw.PaperclipClient, "read_live", return_value=live()), \
                        patch.object(rw, "read_host_admission") as sample, \
                        patch.object(rw.urllib.request, "urlopen") as send:
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(rw.main(["--proposals", handle.name, *args]), 0)
                    sample.assert_not_called()
                    send.assert_not_called()


class ProtectedPins(unittest.TestCase):
    def test_matching_auto_model_and_env_pin_are_diagnostic_only(self):
        overrides = {"adapterConfig": {"model": "pinned-model", "env": {"PIN": "fixture"}}}
        row = live(assigneeAdapterOverrides=overrides)
        before = copy.deepcopy(row)
        decision = decide("clear_pin", row, provenance="auto", pinModel="pinned-model")
        self.assertEqual(decision.outcome, "refused:protected_pin")
        self.assertNotIn("clear_pin", rw.ALLOWED_MUTATIONS)
        self.assertEqual(row, before)
        forged = replace(decision, outcome="decided")
        client = rw.PaperclipClient("https://board.invalid", "synthetic-key")
        with patch.object(client, "patch", return_value={"ok": True}) as send:
            self.assertEqual(rw.apply_decision(client, forged)["wrote"], False)
            send.assert_not_called()

    def test_transport_refuses_pin_changes_even_without_decision_policy(self):
        client = rw.PaperclipClient("https://board.invalid", "synthetic-key")
        for field in ("assigneeAdapterOverrides", "adapterConfig", "runtimeConfig"):
            for method in (client.patch, client.post):
                with self.subTest(field=field, method=method.__name__), \
                        patch.object(rw, "read_host_admission", return_value={"allowed": True}) as sample, \
                        patch.object(rw.urllib.request, "urlopen", side_effect=json_response) as send:
                    with self.assertRaisesRegex(rw.WriterError, "refused:protected_pin"):
                        method("/api/issues/x", {field: None})
                    sample.assert_not_called()
                    send.assert_not_called()


class ArgvOnlyApply(unittest.TestCase):
    def test_decoy_env_does_not_enable_apply(self):
        # The drift: mode read from the environment, so an EnvironmentFile
        # meant for credentials flipped a detector into a writer. Here apply
        # is argv-only; the decoy that would have armed the old shape is
        # inert.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"proposals": [proposal("none")]}, handle)
            path = handle.name
        try:
            with patch.dict(os.environ, {"RECOVERY_WRITER_APPLY": "1", "APPLY": "1",
                                          "RECOVERY_WRITER_MODE": "apply"}, clear=False):
                args = rw.parse_args(["--proposals", path])
            self.assertFalse(args.apply)
        finally:
            os.unlink(path)

    def test_no_environment_variable_is_read_for_apply(self):
        # Behavioural, not textual: with every plausible arming variable set,
        # a cycle without --apply still performs no mutation.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"proposals": [proposal("none")]}, handle)
            path = handle.name
        try:
            env = {"RECOVERY_WRITER_APPLY": "1", "APPLY": "true",
                   "RECOVERY_WRITER_MODE": "apply", "DRY_RUN": "0"}
            with patch.dict(os.environ, env, clear=False):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = rw.main(["--proposals", path])
            self.assertEqual(rc, 0)
            summary = [json.loads(line) for line in out.getvalue().splitlines()
                       if '"type": "summary"' in line][0]
            self.assertEqual(summary["mode"], "dry-run")
            self.assertEqual(summary["applied"], 0)
        finally:
            os.unlink(path)

    def test_source_never_consults_env_for_mode(self):
        # The sibling guard: if someone later adds an env arming path, this
        # names the exact lines. os.environ reads are allowed for credentials
        # and identity plumbing, never for the write bit.
        import pathlib
        source = pathlib.Path(rw.__file__).read_text()
        for env_name in ("RECOVERY_WRITER_APPLY", "WRITER_APPLY", "APPLY_MODE",
                         "RECOVERY_MODE", "DRY_RUN", "DRY-RUN"):
            self.assertNotIn(f'"{env_name}"', source)
            self.assertNotIn(f"'{env_name}'", source)


class OwnerRedirect(unittest.TestCase):
    def test_owner_user_target_is_redirected_to_chief_executive(self):
        prop = proposal("assign_blocker_owner", targetUserId=OWNER_USER,
                        after={"assigneeAgentId": CEO},
                        ownerEvidence="unblockDescriptor.owner.agentId")
        row = live(status="blocked",
                   unblockDescriptor={"owner": {"agentId": CEO}, "action": "Take it."})
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "decided")
        self.assertEqual(decision.target_agent_id, CEO)
        self.assertEqual(decision.redirected, "user-target->chief-executive")

    def test_owner_agent_target_is_redirected_to_chief_executive(self):
        prop = proposal("assign_blocker_owner", targetAgentId=OWNER_AGENT,
                        after={"assigneeAgentId": CEO},
                        ownerEvidence="unblockDescriptor.owner.agentId")
        row = live(status="blocked",
                   unblockDescriptor={"owner": {"agentId": CEO}, "action": "Take it."})
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "decided")
        self.assertEqual(decision.target_agent_id, CEO)
        self.assertEqual(decision.redirected, "owner-agent->chief-executive")

    def test_owner_target_with_no_executive_refuses_never_delivers(self):
        prop = proposal("assign_blocker_owner", targetUserId=OWNER_USER,
                        after={"assigneeUserId": OWNER_USER},
                        ownerEvidence="unblockDescriptor.owner.agentId")
        row = live(status="blocked",
                   unblockDescriptor={"owner": {"agentId": IMPL}, "action": "Take it."})
        decision = rw.decide(prop, row, None, OWNER_USER, OWNER_AGENT)
        self.assertTrue(decision.outcome.startswith("refused:"))
        self.assertIsNone(decision.target_agent_id)

    def test_unknown_user_target_refuses_even_with_executive(self):
        # The plausible hole: "any user target redirects to the chief
        # executive" would let a detector assign arbitrary humans by way of
        # the redirect. Only the configured owner identity redirects.
        prop = proposal("assign_blocker_owner", targetUserId=OTHER_USER,
                        after={"assigneeUserId": OTHER_USER})
        decision = rw.decide(prop, live(status="blocked"), CEO, OWNER_USER, OWNER_AGENT)
        self.assertTrue(decision.outcome.startswith("refused:"))
        self.assertIsNone(decision.target_agent_id)

    def test_user_target_with_unconfigured_owner_identity_refuses(self):
        # Fail closed: without --owner-user-id the writer cannot verify a
        # user target IS the owner, so every user target refuses.
        prop = proposal("assign_blocker_owner", targetUserId=OWNER_USER,
                        after={"assigneeUserId": OWNER_USER})
        decision = rw.decide(prop, live(status="blocked"), CEO, None, None)
        self.assertTrue(decision.outcome.startswith("refused:"))

    def test_patch_route_refuses_user_assignment_without_network(self):
        # Structural: the guard fires before any byte leaves the process, so
        # this test proves it with a client that could not reach anything.
        client = rw.PaperclipClient("http://127.0.0.1:9", "dead-key", None)
        with self.assertRaises(rw.WriterError) as caught:
            client.patch("/api/issues/x", {"assigneeUserId": OWNER_USER})
        self.assertIn("agents only", str(caught.exception))


class SinglePolicy(unittest.TestCase):
    def test_unknown_mutations_are_diagnostic_never_applied(self):
        # Resumes, review handbacks, releases and resets belong to the native
        # paths. A detector proposing them gets a diagnostic, not a write.
        for mutation in ("resume", "wake_same_agent", "release",
                         "reset", "unblock", "handback", "assign_reviewer"):
            decision = decide(mutation)
            self.assertEqual(decision.mutation, mutation)
            self.assertEqual(decision.outcome, "refused:unknown_mutation")

    def test_terminal_card_never_reopens(self):
        prop = proposal("assign_blocker_owner", targetAgentId=IMPL)
        decision = rw.decide(prop, live(status="done"), CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:terminal")
        prop = proposal("restore_blocked")
        decision = rw.decide(prop, live(status="cancelled",
                                        unblockDescriptor={"action": "x"}),
                             CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:terminal")

    def test_hold_barrier_refuses_everything_but_maintenance(self):
        held = live(status="blocked", treeControlState={
            "activePauseHold": {"id": "pause", "mode": "pause", "status": "active"}})
        prop = proposal("assign_blocker_owner", targetAgentId=IMPL,
                        ownerEvidence="unblockDescriptor.owner.agentId")
        row = copy.deepcopy(held)
        row["unblockDescriptor"] = {"owner": {"agentId": IMPL}, "action": "Take it."}
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:hold_barrier")

        prop = proposal("create_pause_hold", holdEvidence="owner comment 1: hold")
        decision = rw.decide(prop, held, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:already_held")

        prop = proposal("restore_blocked")
        row = copy.deepcopy(held)
        row.update(status="todo", unblockDescriptor={"owner": {"agentId": IMPL},
                                                     "action": "hold: do not proceed"})
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "decided")

    def test_body_prose_alone_neither_holds_nor_contains(self):
        # Unauthenticated title/body prose may not steer the writer: it
        # refuses nothing here and initiates nothing.
        row = live(status="blocked",
                   title="HOLD do not proceed",
                   description="owner hold on everything",
                   unblockDescriptor={"owner": {"agentId": IMPL}, "action": "Take it."})
        prop = proposal("assign_blocker_owner", targetAgentId=IMPL,
                        ownerEvidence="unblockDescriptor.owner.agentId")
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "decided")

    def test_stale_owner_refuses(self):
        row = live(status="todo",
                   unblockDescriptor={"owner": {"agentId": "00000000-0000-4000-8000-000000000182"},
                                      "action": "Take it."})
        prop = proposal("assign_blocker_owner", targetAgentId=IMPL,
                        ownerEvidence="unblockDescriptor.owner.agentId")
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:owner_changed")

    def test_manual_pin_never_clears(self):
        row = live(status="blocked",
                   assigneeAdapterOverrides={"adapterConfig": {"model": "muse"}})
        prop = proposal("clear_pin", provenance="manual", pinModel="muse")
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:manual_pin")

    def test_missing_provenance_never_clears(self):
        # The quiet default matters: a detector that forgot to classify must
        # not clear by omission.
        row = live(status="blocked",
                   assigneeAdapterOverrides={"adapterConfig": {"model": "muse"}})
        prop = proposal("clear_pin", pinModel="muse")
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:manual_pin")

    def test_changed_pin_refuses_stale_clear(self):
        row = live(status="blocked",
                   assigneeAdapterOverrides={"adapterConfig": {"model": "opus"}})
        prop = proposal("clear_pin", provenance="auto", pinModel="muse")
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:pin_changed")

    def test_gone_pin_refuses(self):
        prop = proposal("clear_pin", provenance="auto", pinModel="muse")
        decision = rw.decide(prop, live(status="blocked"), CEO, OWNER_USER, OWNER_AGENT)
        self.assertEqual(decision.outcome, "refused:pin_gone")

    def test_receipt_carries_single_schema(self):
        prop = proposal("assign_blocker_owner", targetAgentId=IMPL,
                        ownerEvidence="unblockDescriptor.owner.agentId")
        row = live(status="blocked",
                   unblockDescriptor={"owner": {"agentId": IMPL}, "action": "Take it."})
        decision = rw.decide(prop, row, CEO, OWNER_USER, OWNER_AGENT)
        receipt = decision.receipt("dry-run", "dry_run_held")
        for key in ("writer", "policyVersion", "mode", "issueId", "identifier",
                    "detector", "reason", "mutation", "targetAgentId", "sourceId",
                    "redirected", "fingerprint", "before", "after", "note",
                    "decision", "apply"):
            self.assertIn(key, receipt, f"receipt missing {key}")
        self.assertEqual(receipt["writer"], rw.WRITER_NAME)
        self.assertEqual(receipt["policyVersion"], rw.POLICY_VERSION)
        self.assertRegex(receipt["fingerprint"], r"^[0-9a-f]{24}$")


class InputAndBounds(unittest.TestCase):
    def test_zero_proposals_is_an_error_not_a_clean_cycle(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"proposals": []}, handle)
            path = handle.name
        try:
            with self.assertRaises(rw.WriterError):
                rw.load_proposals(path)
        finally:
            os.unlink(path)

    def test_malformed_proposals_fail_loudly(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"proposals": [oops')
            path = handle.name
        try:
            with self.assertRaises(rw.WriterError):
                rw.load_proposals(path)
        finally:
            os.unlink(path)

    def test_max_repairs_zero_is_accepted_as_observe_only(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"proposals": [proposal("none")]}, handle)
            path = handle.name
        try:
            args = rw.parse_args(["--proposals", path, "--max-repairs", "0"])
            self.assertEqual(args.max_repairs, 0)
        finally:
            os.unlink(path)

    def test_negative_max_repairs_is_a_usage_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"proposals": [proposal("none")]}, handle)
            path = handle.name
        try:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit) as caught:
                    rw.parse_args(["--proposals", path, "--max-repairs", "-1"])
            self.assertEqual(caught.exception.code, 2)
        finally:
            os.unlink(path)

    def test_jsonl_detector_output_feeds_unmodified(self):
        lines = [
            json.dumps({"type": "cycle", "mode": "dry-run"}),
            json.dumps({"type": "proposal", **proposal("none")}),
            json.dumps({"type": "summary", "mode": "dry-run"}),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write("\n".join(lines) + "\n")
            path = handle.name
        try:
            rows = rw.load_proposals(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["mutation"], "none")
        finally:
            os.unlink(path)

    def test_refusals_do_not_fail_a_cycle(self):
        # A refused proposal is the policy working. The suite's own sibling
        # (test_liveness_reconciler.py) counts failures the old way; this
        # writer must stay green on a diagnostic-only cycle.
        rows = [
            proposal("resume"),
            proposal("assign_blocker_owner", targetAgentId=IMPL),
        ]
        rows[1]["live"] = live(status="done")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"proposals": rows}, handle)
            path = handle.name
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = rw.main(["--proposals", path])
            self.assertEqual(rc, 0, out.getvalue())
            summary = [json.loads(line) for line in out.getvalue().splitlines()
                       if '"type": "summary"' in line][0]
            self.assertEqual(summary["failed"], 0)
        finally:
            os.unlink(path)

    def test_decided_dry_run_records_without_applying(self):
        row = live(status="blocked",
                   unblockDescriptor={"owner": {"agentId": IMPL}, "action": "Take it."})
        rows = [dict(proposal("assign_blocker_owner", targetAgentId=IMPL,
                              ownerEvidence="unblockDescriptor.owner.agentId"),
                     live=row)]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"proposals": rows}, handle)
            path = handle.name
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = rw.main(["--proposals", path])
            self.assertEqual(rc, 0, out.getvalue())
            receipts = [json.loads(line) for line in out.getvalue().splitlines()
                        if '"type": "receipt"' in line]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["apply"], "dry_run_held")
        finally:
            os.unlink(path)


class BenignAgentReset(unittest.TestCase):
    # First and only auto-action: reset_agent_error for the known-benign
    # exit-143 false-failure (subtype=success plus
    # unmanagedBackgroundTask.terminalResultSeen=true), past 10m. Every
    # case runs offline against fixtures: decide() with an injectable clock,
    # apply_decision() against a stub client. No network, no credentials.
    AGENT = "00000000-0000-4000-8000-000000000020"
    RUN = "00000000-0000-4000-8000-000000000087"
    NOW = rw.datetime.datetime(2026, 10, 3, 20, 0, 0,
                               tzinfo=rw.datetime.timezone.utc)

    def benign_result(self):
        return {
            "subtype": "success",
            "unmanagedBackgroundTask": {"terminalResultSeen": True},
        }

    def reset_proposal(self, **overrides):
        base = proposal("reset_agent_error",
                        agentId=self.AGENT, runId=self.RUN,
                        finishedAt="2026-10-03T19:30:00Z",
                        result=self.benign_result())
        # Agent-targeted resets name no board card: the shared helper's
        # issueId must go, or the writer (correctly) refuses the proposal.
        base.pop("issueId", None)
        base.update(overrides)
        return base

    def benign_decision(self, **overrides):
        return rw.decide_agent_reset(self.reset_proposal(**overrides),
                                     now=self.NOW)

    def test_benign_past_10m_is_decided(self):
        decision = self.benign_decision()
        self.assertEqual(decision.outcome, "decided")
        self.assertEqual(decision.mutation, "reset_agent_error")
        self.assertEqual(decision.target_agent_id, self.AGENT)
        self.assertEqual(decision.source_id, self.RUN)

    def test_evidence_lives_in_before_after(self):
        # A receipt reader must see what justified the reset without
        # chasing proposal plumbing: the pinned evidence is in before/after.
        decision = self.benign_decision()
        self.assertEqual(decision.before["subtype"], "success")
        self.assertTrue(decision.before["terminalResultSeen"])
        self.assertEqual(decision.after, {"agentId": self.AGENT,
                                          "status": "idle"})

    def test_wrong_subtype_refuses(self):
        decision = self.benign_decision(
            result={"subtype": "crash",
                    "unmanagedBackgroundTask": {"terminalResultSeen": True}})
        self.assertEqual(decision.outcome, "refused:non_benign")
        self.assertNotEqual(decision.after.get("status"), "idle")

    def test_seen_false_refuses(self):
        decision = self.benign_decision(
            result={"subtype": "success",
                    "unmanagedBackgroundTask": {"terminalResultSeen": False}})
        self.assertEqual(decision.outcome, "refused:non_benign")

    def test_missing_result_refuses_never_resets_by_omission(self):
        # The quiet default matters: a detector that forgot to classify must
        # not reset by omission. Absence refuses exactly like a wrong value.
        bare = proposal("reset_agent_error", agentId=self.AGENT,
                        runId=self.RUN,
                        finishedAt="2026-10-03T19:30:00Z")
        decision = rw.decide_agent_reset(bare, now=self.NOW)
        self.assertEqual(decision.outcome, "refused:non_benign")

    def test_missing_key_refuses(self):
        # Tricky shape: a result dict with neither key. Not benign.
        decision = self.benign_decision(result={"other": 1})
        self.assertEqual(decision.outcome, "refused:non_benign")

    def test_truthy_string_is_not_true(self):
        # Strict identity: "true"/1 must not pass a `is True` gate.
        for seen in ("true", 1, "True"):
            decision = self.benign_decision(
                result={"subtype": "success", "unmanagedBackgroundTask":
                        {"terminalResultSeen": seen}})
            self.assertEqual(decision.outcome, "refused:non_benign",
                             f"seen={seen!r} passed the gate")

    def test_fresh_run_refuses(self):
        decision = self.benign_decision(
            finishedAt="2026-10-03T19:55:00Z")
        self.assertEqual(decision.outcome, "refused:too_fresh")

    def test_boundary_is_not_past(self):
        # Exactly 10m is not PAST 10m: the threshold is strict.
        decision = self.benign_decision(
            finishedAt="2026-10-03T19:50:00Z")
        self.assertEqual(decision.outcome, "refused:too_fresh")

    def test_missing_run_refuses(self):
        bare = self.reset_proposal()
        del bare["runId"]
        decision = rw.decide_agent_reset(bare, now=self.NOW)
        self.assertEqual(decision.outcome, "refused:no_run")

    def test_missing_agent_refuses(self):
        bare = self.reset_proposal()
        del bare["agentId"]
        decision = rw.decide_agent_reset(bare, now=self.NOW)
        self.assertEqual(decision.outcome, "refused:no_agent")

    def test_unparseable_finished_refuses(self):
        decision = self.benign_decision(finishedAt="not-a-time")
        self.assertEqual(decision.outcome, "refused:unaged")

    def test_decide_routes_reset_through_the_reset_gate(self):
        decision = decide("reset_agent_error",
                          proposal={"agentId": self.AGENT, "runId": self.RUN,
                                    "finishedAt": "2020-01-01T00:00:00Z",
                                    "result": self.benign_result()})
        self.assertEqual(decision.mutation, "reset_agent_error")
        self.assertEqual(decision.outcome, "decided")

    def test_reset_with_card_id_is_undecidable(self):
        # Agent-targeted resets name no board card: an issueId here is a
        # detector bug, and the cycle fails loudly rather than resetting
        # the wrong scope.
        rows = [self.reset_proposal(issueId="00000000-0000-4000-8000-000000000019")]
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as handle:
            json.dump({"proposals": rows}, handle)
            path = handle.name
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = rw.main(["--proposals", path])
            self.assertEqual(rc, 1, out.getvalue())
            receipts = [json.loads(line) for line in out.getvalue().splitlines()
                        if '"type": "receipt"' in line]
            self.assertEqual(receipts[0]["decision"], "refused:undecidable")
        finally:
            os.unlink(path)

    def test_decided_reset_dry_run_held(self):
        rows = [self.reset_proposal()]
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as handle:
            json.dump({"proposals": rows}, handle)
            path = handle.name
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = rw.main(["--proposals", path])
            self.assertEqual(rc, 0, out.getvalue())
            receipts = [json.loads(line) for line in out.getvalue().splitlines()
                        if '"type": "receipt"' in line]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["decision"], "decided")
            self.assertEqual(receipts[0]["apply"], "dry_run_held")
        finally:
            os.unlink(path)


class BenignAgentResetApply(unittest.TestCase):
    AGENT = BenignAgentReset.AGENT
    RUN = BenignAgentReset.RUN

    def benign(self):
        return BenignAgentReset().benign_decision()

    class StubClient(rw.PaperclipClient):
        def __init__(self, agent_status="error", run=None):
            self.agent_status = agent_status
            self.run = run
            self.posts = []

        def get(self, path):
            if path == f"/api/agents/{BenignAgentReset.AGENT}":
                return {"id": BenignAgentReset.AGENT,
                        "status": self.agent_status}
            if path == f"/api/heartbeat-runs/{BenignAgentReset.RUN}":
                return self.run
            raise AssertionError(f"unexpected GET {path}")

        def post(self, path, body):
            self.posts.append(path)
            if path == f"/api/agents/{BenignAgentReset.AGENT}/clear-error":
                self.agent_status = "idle"
                return {"status": "idle"}
            raise AssertionError(f"unexpected POST {path}")

    def live_run(self, **overrides):
        base = {
            "id": self.RUN, "agentId": self.AGENT, "status": "failed",
            "finishedAt": "2026-10-03T19:30:00Z",
            "resultJson": BenignAgentReset().benign_result(),
        }
        base.update(overrides)
        return base

    def test_apply_resets_and_verifies_idle(self):
        client = self.StubClient(run=self.live_run())
        result = rw.apply_decision(client, self.benign())
        self.assertEqual(result, {"outcome": "applied", "wrote": True})
        self.assertEqual(
            client.posts,
            [f"/api/agents/{self.AGENT}/clear-error"])

    def test_apply_refuses_live_agent_not_error(self):
        client = self.StubClient(agent_status="idle", run=self.live_run())
        with self.assertRaises(rw.WriterError):
            rw.apply_decision(client, self.benign())
        self.assertEqual(client.posts, [])

    def test_apply_refuses_live_run_not_failed(self):
        client = self.StubClient(run=self.live_run(status="succeeded"))
        with self.assertRaises(rw.WriterError):
            rw.apply_decision(client, self.benign())
        self.assertEqual(client.posts, [])

    def test_apply_refuses_wrong_agent_run(self):
        client = self.StubClient(
            run=self.live_run(agentId="00000000-0000-4000-8000-000000000182"))
        with self.assertRaises(rw.WriterError):
            rw.apply_decision(client, self.benign())
        self.assertEqual(client.posts, [])

    def test_apply_refuses_live_non_benign(self):
        client = self.StubClient(
            run=self.live_run(resultJson={"subtype": "crash"}))
        with self.assertRaises(rw.WriterError):
            rw.apply_decision(client, self.benign())
        self.assertEqual(client.posts, [])

    def test_apply_refuses_fresh_live_run(self):
        # Offline evidence said past-10m; the live run finished a minute
        # ago. The live read wins: no write.
        fresh = self.live_run(
            finishedAt=rw.datetime.datetime.now(
                rw.datetime.timezone.utc).isoformat())
        client = self.StubClient(run=fresh)
        with self.assertRaises(rw.WriterError):
            rw.apply_decision(client, self.benign())
        self.assertEqual(client.posts, [])

    def test_apply_refuses_when_idle_never_confirms(self):
        class StuckClient(self.StubClient):
            def post(self, path, body):
                self.posts.append(path)
                return {"status": "error"}
        client = StuckClient(run=self.live_run())
        with self.assertRaises(rw.WriterError):
            rw.apply_decision(client, self.benign())
        self.assertEqual(
            client.posts,
            [f"/api/agents/{self.AGENT}/clear-error"])

    def test_non_decided_never_writes(self):
        refused = rw.decide_agent_reset(
            proposal("reset_agent_error"),
            now=BenignAgentReset.NOW)
        self.assertTrue(refused.outcome.startswith("refused:"))
        client = self.StubClient(run=self.live_run())
        result = rw.apply_decision(client, refused)
        self.assertEqual(result["wrote"], False)
        self.assertEqual(client.posts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=[__file__])