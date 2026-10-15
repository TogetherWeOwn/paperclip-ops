#!/usr/bin/env python3
# ===========================================================================
# test_pacer_apply.py — hostile tests for the pacer applier-validator
# (case_755e7d5f slice 2)
#
# Written to FAIL a plausible-but-wrong implementation, not to confirm the
# one that exists:
#
# * §2 kills stale-plan and name-redirect mutants: moved live values,
#   missing/unsafe plan or live IDs, invalid plan-from/step targets, or a
#   same-name row with a different ID are held rather than patched.
# * §3 kills the merge-patch mutant: a body that carries only the one key
#   (stripping wakeOnDemand/modelProfiles/dailyCaps on the wholesale
#   REPLACE) or that smuggles any other key change. The diff proof pins
#   byte-identity everywhere else, including a pre-existing
#   wakeOnDemand:false the applier must preserve, never "fix".
# * §4 kills the brake-ignoring mutant (reconciler target applied above the
#   ID-keyed brake), name-only or conflicting aliases, and the clamp-silently
#   mutant (insane brake values refuse the whole run, exit 2, not clamp).
# * §5 kills the dead-kill-switch mutant: frozen lanes must hold in BOTH
#   modes, duplicate roster IDs refuse, and a frozen name absent from the
#   roster refuses (a typo must not freeze nothing). --frozen-lanes without
#   --roster refuses: no blind freezes.
# * §6 kills the quiet-directive mutant: dry-run output must carry no PATCH
#   bodies (effect none), and a shadow plan must be refused outright — there
#   is nothing to apply.
# * §7 kills the cancel mutant: no cancel/kill/terminate/disable key in the
#   output schema or the tool source.
# * §8 kills the rollback-blind mutant: restore intents must go through the
#   same drift/wholesale/brake path, and an agent already at baseline is
#   already-applied, not re-patched.
# * §9 kills the networked-applier mutant: the tool must not be able to open
#   a socket or shell out. The /api/agents/ path template is data (it names
#   the board route the OPERATOR calls), not a client.
# * §10 kills the silent-nothing mutant: a bundle with zero appliable moves
#   exits 5, not 0 — "nothing to do" is not "moves emitted".
#
# Deterministic: plans are generated from pinned fixtures at a pinned --now
# through the real reconciler (integration, not a checked-in plan blob), and
# live variants are built in-test by copy. No network, no clock read, no
# write outside temp dirs (and --out atomicity is tested there too).
# ===========================================================================
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "pacer_apply.py")
RECONCILER = os.path.join(HERE, "pacer_concurrency.py")
FIX = os.path.join(HERE, "tests", "fixtures", "case_5f1b2386")
NOW = "2026-10-04T12:00:00Z"

FORBIDDEN_OUTPUT_NEEDLES = ("cancel", "kill", "terminate", "disable")


def write_json(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)


def make_plan(tmp, mode="enforce", extra_plan_args=()):
    plan_path = os.path.join(tmp, "plan.json")
    args = [sys.executable, RECONCILER, "--now", NOW,
            "--lanes", os.path.join(FIX, "lanes.json"),
            "--agents", os.path.join(FIX, "agents.json"),
            "--caps", os.path.join(FIX, "caps.json"),
            "--state", os.path.join(FIX, "state.json"),
            "--mode", mode]
    if mode == "enforce":
        args.append("--yes")
    args.extend(("--out", plan_path))
    args.extend(extra_plan_args)
    cp = subprocess.run(args, capture_output=True, text=True)
    assert cp.returncode == 0, "reconciler exit %d: %s" % (cp.returncode,
                                                           cp.stderr)
    return plan_path


def run_apply(*args):
    cp = subprocess.run([sys.executable, TOOL, *args],
                        capture_output=True, text=True)
    return cp


class ApplyBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pacer-apply-")
        self.plan = make_plan(self.tmp)
        self.live_path = os.path.join(FIX, "live.json")
        with open(self.live_path, encoding="utf-8") as fh:
            self.live = json.load(fh)

    def live_variant(self, mutate):
        doc = copy.deepcopy(self.live)
        mutate(doc)
        path = os.path.join(self.tmp, "live-variant.json")
        write_json(path, doc)
        return path

    def base(self, *extra, **kw):
        args = ["--plan", self.plan, "--live", self.live_path, *extra]
        cp = run_apply(*args)
        assert cp.returncode == kw.get("want_rc", 0), \
            "exit %d (want %d): %s" % (cp.returncode, kw.get("want_rc", 0),
                                       cp.stderr)
        return json.loads(cp.stdout)

    def moves_by_name(self, bundle):
        return {m["name"]: m for m in bundle["moves"]}

    # -- §1  shape ---------------------------------------------------------
    def test_01_dry_run_carries_no_bodies_and_effect_none(self):
        bundle = self.base()
        self.assertEqual(bundle["mode"], "apply-dry-run")
        self.assertEqual(bundle["planMode"], "enforce")
        self.assertEqual(bundle["effect"], "none")
        self.assertEqual(bundle["patches"], [])
        self.assertEqual(bundle["summary"]["appliable"], 3)
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        expected_directives = sum(
            1 for rec in plan["agents"] if rec.get("effect") == "directive")
        self.assertEqual(bundle["summary"]["directives"], expected_directives)
        self.assertLess(expected_directives, len(plan["agents"]))
        self.assertEqual(len(bundle["moves"]), 3)
        blob = json.dumps(bundle)
        self.assertNotIn('"body"', blob)

    def test_01_yes_emits_three_patch_bodies(self):
        bundle = self.base("--yes")
        self.assertEqual(bundle["mode"], "apply")
        self.assertEqual(bundle["effect"], "directive")
        self.assertEqual(len(bundle["patches"]), 3)
        by_name = {p["name"]: p for p in bundle["patches"]}
        self.assertEqual(by_name["agent-ahead"]["target"], 2)
        self.assertEqual(by_name["agent-crit"]["target"], 1)
        self.assertEqual(by_name["agent-mixed"]["target"], 1)
        for entry in bundle["patches"]:
            patch = entry["patch"]
            self.assertEqual(patch["method"], "PATCH")
            self.assertEqual(patch["path"], "/api/agents/" + entry["agentId"])

    def test_01_critical_patch_is_last_even_when_plan_lists_it_first(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        critical = next(row for row in plan["agents"]
                        if row.get("critical") is True)
        plan["agents"].remove(critical)
        plan["agents"].insert(0, critical)
        write_json(self.plan, plan)

        bundle = self.base("--yes")
        self.assertEqual(bundle["patches"][-1]["agentId"],
                         critical["agentId"])
        self.assertIs(bundle["patches"][-1]["critical"], True)
        self.assertEqual(bundle["moves"][-1]["agentId"],
                         critical["agentId"])

    def test_01_duplicate_plan_agent_id_refuses_whole_bundle(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        duplicate = copy.deepcopy(next(
            row for row in plan["agents"]
            if row.get("name") == "agent-ahead"))
        duplicate["target"] = 1
        plan["agents"].append(duplicate)
        write_json(self.plan, plan)

        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(cp.stdout, "")
        self.assertIn("duplicate plan agent id", cp.stderr)

    # -- §2  drift holds ----------------------------------------------------
    def test_02_drifted_agent_held_rest_still_patch(self):
        def drift(doc):
            for row in doc["agents"]:
                if row["id"] == "a-ahead":
                    row["runtimeConfig"]["heartbeat"]["maxConcurrentRuns"] = 9
        bundle = self.base("--live", self.live_variant(drift), "--yes")
        by_name = {p["name"]: p for p in bundle["patches"]}
        self.assertNotIn("agent-ahead", by_name)
        self.assertIn("agent-crit", by_name)
        held = {h["name"]: h for h in bundle["held"]}
        self.assertIn("agent-ahead", held)
        self.assertIn("drift", held["agent-ahead"]["reason"])
        self.assertEqual(bundle["summary"]["held"], 1)

    def test_02_missing_live_record_held_not_crash(self):
        def drop(doc):
            doc["agents"] = [r for r in doc["agents"]
                             if r["id"] != "a-mixed"]
        bundle = self.base("--live", self.live_variant(drop), "--yes")
        by_name = {p["name"]: p for p in bundle["patches"]}
        self.assertNotIn("agent-mixed", by_name)
        self.assertIn("agent-ahead", by_name)
        held = {h["name"]: h for h in bundle["held"]}
        self.assertIn("no live runtimeConfig", held["agent-mixed"]["reason"])

    def test_02_name_fallback_holds_when_live_agent_id_differs(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        record = next(row for row in plan["agents"]
                      if row.get("name") == "agent-ahead")
        record["agentId"] = "retired-ahead-id"
        write_json(self.plan, plan)

        bundle = self.base("--yes")
        patches = {p["name"]: p for p in bundle["patches"]}
        held = {h["name"]: h for h in bundle["held"]}
        self.assertNotIn("agent-ahead", patches)
        self.assertIn("agent-ahead", held)
        self.assertEqual(held["agent-ahead"]["agentId"], "retired-ahead-id")
        self.assertIn("agent id mismatch", held["agent-ahead"]["reason"])

    def test_02_missing_or_unsafe_plan_id_is_held(self):
        with open(self.plan, encoding="utf-8") as fh:
            original = json.load(fh)
        bad_ids = (("absent", None), ("null", None), ("empty", ""),
                   ("whitespace", " "), ("padded", " a-ahead "),
                   ("query", "a-ahead?x=1"))
        for label, bad_id in bad_ids:
            plan = copy.deepcopy(original)
            record = next(row for row in plan["agents"]
                          if row.get("name") == "agent-ahead")
            if label == "absent":
                record.pop("agentId")
            else:
                record["agentId"] = bad_id
            write_json(self.plan, plan)
            with self.subTest(plan_id=label):
                bundle = self.base("--yes")
                patches = {p["name"]: p for p in bundle["patches"]}
                held = {h["name"]: h for h in bundle["held"]}
                self.assertNotIn("agent-ahead", patches)
                self.assertIn("agent-ahead", held)
                self.assertIn("usable agent id", held["agent-ahead"]["reason"])

    def test_02_live_id_must_be_a_safe_path_segment(self):
        for live_id in ("a-ahead?x=1", "a/ahead", ".."):
            def inject_id(doc):
                row = next(row for row in doc["agents"]
                           if row["name"] == "agent-ahead")
                row["id"] = live_id

            with self.subTest(live_id=live_id):
                bundle = self.base("--live", self.live_variant(inject_id),
                                   "--yes")
                patches = {p["name"]: p for p in bundle["patches"]}
                held = {h["name"]: h for h in bundle["held"]}
                self.assertNotIn("agent-ahead", patches)
                self.assertIn("agent-ahead", held)
                self.assertIn("no usable id", held["agent-ahead"]["reason"])

    def test_02_duplicate_live_ids_refuse_whole_bundle(self):
        live = copy.deepcopy(self.live)
        duplicate = copy.deepcopy(next(row for row in live["agents"]
                                      if row["id"] == "a-ahead"))
        duplicate["runtimeConfig"]["heartbeat"]["maxConcurrentRuns"] = 9
        live["agents"].append(duplicate)
        live_path = os.path.join(self.tmp, "live-duplicate-id.json")
        write_json(live_path, live)
        cp = run_apply("--plan", self.plan, "--live", live_path, "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("duplicate live agent id", cp.stderr)

    def test_02_name_fallback_holds_when_live_agent_id_is_missing(self):
        for variant, live_id in (("absent", None), ("null", None),
                                 ("whitespace", " "),
                                 ("padded", " a-ahead ")):
            def strip_id(doc):
                row = next(row for row in doc["agents"]
                           if row["name"] == "agent-ahead")
                if variant == "absent":
                    row.pop("id")
                else:
                    row["id"] = live_id

            with self.subTest(live_id=variant):
                bundle = self.base("--live", self.live_variant(strip_id),
                                   "--yes")
                patches = {p["name"]: p for p in bundle["patches"]}
                held = {h["name"]: h for h in bundle["held"]}
                self.assertNotIn("agent-ahead", patches)
                self.assertIn("agent-ahead", held)
                self.assertIn("no usable id", held["agent-ahead"]["reason"])

    def test_02_heartbeatless_live_record_held(self):
        def strip(doc):
            for row in doc["agents"]:
                if row["id"] == "a-crit":
                    del row["runtimeConfig"]["heartbeat"]
        bundle = self.base("--live", self.live_variant(strip), "--yes")
        by_name = {p["name"]: p for p in bundle["patches"]}
        self.assertNotIn("agent-crit", by_name)
        held = {h["name"]: h for h in bundle["held"]}
        self.assertIn("no heartbeat object", held["agent-crit"]["reason"])

    def test_02_missing_or_invalid_plan_from_is_held(self):
        with open(self.plan, encoding="utf-8") as fh:
            original = json.load(fh)
        bad_values = (("absent", None), ("null", None), ("boolean", True),
                      ("string", "3"), ("zero", 0), ("too-high", 33))
        for label, value in bad_values:
            plan = copy.deepcopy(original)
            record = next(row for row in plan["agents"]
                          if row.get("name") == "agent-ahead")
            if label == "absent":
                record.pop("from")
            else:
                record["from"] = value
            write_json(self.plan, plan)
            with self.subTest(plan_from=label):
                bundle = self.base("--yes")
                patches = {p["name"]: p for p in bundle["patches"]}
                held = {h["name"]: h for h in bundle["held"]}
                self.assertNotIn("agent-ahead", patches)
                self.assertIn("agent-ahead", held)
                self.assertIn("plan from", held["agent-ahead"]["reason"])

    def test_02_plan_direction_must_match_target(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        record = next(row for row in plan["agents"]
                      if row.get("name") == "agent-ahead")
        record["action"] = "up"
        write_json(self.plan, plan)

        bundle = self.base("--yes")
        patches = {p["name"]: p for p in bundle["patches"]}
        held = {h["name"]: h for h in bundle["held"]}
        self.assertNotIn("agent-ahead", patches)
        self.assertIn("plan action", held["agent-ahead"]["reason"])

    def test_02_up_plan_must_advance_one_step(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        record = next(row for row in plan["agents"]
                      if row.get("name") == "agent-ahead")
        record["action"] = "up"
        record["from"] = 1
        record["target"] = 32
        write_json(self.plan, plan)

        def set_live_target(doc):
            row = next(row for row in doc["agents"]
                       if row["id"] == "a-ahead")
            row["runtimeConfig"]["heartbeat"]["maxConcurrentRuns"] = 1

        live_path = self.live_variant(set_live_target)
        bundle = self.base("--live", live_path, "--yes")
        patches = {p["name"]: p for p in bundle["patches"]}
        held = {h["name"]: h for h in bundle["held"]}
        self.assertNotIn("agent-ahead", patches)
        self.assertIn("agent-ahead", held)
        self.assertIn("one-step", held["agent-ahead"]["reason"])

    def test_02_up_plan_advances_one_step_without_brake(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        record = next(row for row in plan["agents"]
                      if row.get("name") == "agent-ahead")
        record["action"] = "up"
        record["from"] = 1
        record["target"] = 2
        write_json(self.plan, plan)

        def set_live_target(doc):
            row = next(row for row in doc["agents"]
                       if row["id"] == "a-ahead")
            row["runtimeConfig"]["heartbeat"]["maxConcurrentRuns"] = 1

        live_path = self.live_variant(set_live_target)
        bundle = self.base("--live", live_path, "--yes")
        patches = {p["name"]: p for p in bundle["patches"]}
        self.assertIn("agent-ahead", patches)
        self.assertEqual(patches["agent-ahead"]["target"], 2)
        body = patches["agent-ahead"]["patch"]["body"]["runtimeConfig"]
        self.assertEqual(body["heartbeat"]["maxConcurrentRuns"], 2)

    def test_02_directive_action_must_match_plan_mode(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        record = next(row for row in plan["agents"]
                      if row.get("name") == "agent-ahead")
        record["action"] = "restore"
        write_json(self.plan, plan)

        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(cp.stdout, "")
        self.assertIn("does not match enforce plan", cp.stderr)

    # -- §3  wholesale write-back -------------------------------------------
    def test_03_body_preserves_every_other_key_byte_identical(self):
        bundle = self.base("--yes")
        live_by_id = {r["id"]: r for r in self.live["agents"]}
        for entry in bundle["patches"]:
            live = live_by_id[entry["agentId"]]["runtimeConfig"]
            body = entry["patch"]["body"]["runtimeConfig"]
            self.assertEqual(sorted(body.keys()), sorted(live.keys()))
            for key, value in live.items():
                if key == "heartbeat":
                    continue
                self.assertEqual(body[key], value,
                                 "non-heartbeat key %r changed" % key)
            for key, value in live["heartbeat"].items():
                if key == "maxConcurrentRuns":
                    continue
                self.assertEqual(body["heartbeat"][key], value,
                                 "heartbeat key %r changed" % key)
            self.assertEqual(body["heartbeat"]["maxConcurrentRuns"],
                             entry["target"])

    def test_03_preexisting_wakeondemand_false_preserved(self):
        # a-crit ships wakeOnDemand:false live. The applier must carry it
        # through byte-identical — preserving is correct read-modify-write;
        # "fixing" it would be an unasked reconfiguration, dropping it
        # would strip wakeability off a critical agent.
        bundle = self.base("--yes")
        by_name = {p["name"]: p for p in bundle["patches"]}
        body = by_name["agent-crit"]["patch"]["body"]["runtimeConfig"]
        self.assertIs(body["heartbeat"]["wakeOnDemand"], False)

    # -- §4  brake wins down -------------------------------------------------
    def test_04_brake_below_reconciler_caps_the_move(self):
        bundle = self.base("--brake", os.path.join(FIX, "brake.json"),
                           "--yes")
        by_name = {p["name"]: p for p in bundle["patches"]}
        # reconciler says 2, brake says 1: min wins.
        self.assertEqual(by_name["agent-ahead"]["target"], 1)
        self.assertTrue(by_name["agent-ahead"]["brakeCapped"])
        self.assertEqual(by_name["agent-ahead"]["from"], 3)
        body = by_name["agent-ahead"]["patch"]["body"]["runtimeConfig"]
        self.assertEqual(body["heartbeat"]["maxConcurrentRuns"], 1)
        # untouched by the brake: reconciler target stands.
        self.assertEqual(by_name["agent-crit"]["target"], 1)
        self.assertNotIn("brakeCapped", by_name["agent-crit"])
        self.assertEqual(bundle["summary"]["brakeCapped"], 1)

    def test_04_brake_reversal_reports_effective_action(self):
        with open(self.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        ahead = next(row for row in plan["agents"]
                     if row.get("name") == "agent-ahead")
        ahead["action"] = "up"
        ahead["target"] = 4
        write_json(self.plan, plan)

        brake_path = os.path.join(self.tmp, "brake-reversal.json")
        write_json(brake_path, {"targets": {"a-ahead": 2}})
        bundle = self.base("--brake", brake_path, "--yes")
        ahead = self.moves_by_name(bundle)["agent-ahead"]
        self.assertEqual(ahead["target"], 2)
        self.assertEqual(ahead["action"], "down")
        self.assertEqual(ahead["planAction"], "up")
        self.assertTrue(ahead["brakeCapped"])
        body = ahead["patch"]["body"]["runtimeConfig"]
        self.assertEqual(body["heartbeat"]["maxConcurrentRuns"], 2)

    def test_04_brake_above_reconciler_leaves_move_alone(self):
        brake_path = os.path.join(self.tmp, "brake-high.json")
        write_json(brake_path, {"targets": {"a-ahead": 32}})
        bundle = self.base("--brake", brake_path, "--yes")
        by_name = {p["name"]: p for p in bundle["patches"]}
        self.assertEqual(by_name["agent-ahead"]["target"], 2)
        self.assertNotIn("brakeCapped", by_name["agent-ahead"])

    def test_04_name_only_brake_target_refuses_whole_run(self):
        brake_path = os.path.join(self.tmp, "brake-name-only.json")
        write_json(brake_path, {"targets": {"agent-ahead": 1}})
        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--brake", brake_path, "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(cp.stdout, "")
        self.assertIn("name-keyed", cp.stderr)

    def test_04_live_name_only_brake_target_refuses_after_rename(self):
        def rename_live_agent(doc):
            row = next(row for row in doc["agents"]
                       if row["id"] == "a-ahead")
            row["name"] = "renamed-ahead"

        live_path = self.live_variant(rename_live_agent)
        brake_path = os.path.join(self.tmp, "brake-live-name-only.json")
        write_json(brake_path, {"targets": {"renamed-ahead": 1}})
        cp = run_apply("--plan", self.plan, "--live", live_path,
                       "--brake", brake_path, "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(cp.stdout, "")
        self.assertIn("name-keyed", cp.stderr)

    def test_04_conflicting_brake_aliases_refuse_whole_run(self):
        brake_path = os.path.join(self.tmp, "brake-conflicting-aliases.json")
        write_json(brake_path,
                   {"targets": {"a-ahead": 1, "agent-ahead": 2}})
        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--brake", brake_path, "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(cp.stdout, "")
        self.assertIn("conflicting brake target aliases", cp.stderr)

    def test_04_matching_brake_alias_uses_id_target(self):
        brake_path = os.path.join(self.tmp, "brake-matching-aliases.json")
        write_json(brake_path,
                   {"targets": {"a-ahead": 1, "agent-ahead": 1}})
        bundle = self.base("--brake", brake_path, "--yes")
        by_name = {p["name"]: p for p in bundle["patches"]}
        self.assertEqual(by_name["agent-ahead"]["target"], 1)

    def test_04_insane_brake_refuses_whole_run(self):
        for insane in (0, 33, "lots", True):
            brake_path = os.path.join(self.tmp, "brake-%r.json" % (insane,))
            write_json(brake_path, {"targets": {"a-ahead": insane}})
            cp = run_apply("--plan", self.plan, "--live", self.live_path,
                           "--brake", brake_path, "--yes")
            self.assertEqual(cp.returncode, 2, "brake %r" % (insane,))
            self.assertIn("refused", cp.stderr)

    # -- §5  frozen kill switch ----------------------------------------------
    def test_05_frozen_lane_holds_in_both_modes(self):
        roster = os.path.join(FIX, "agents.json")
        # agent-mixed serves claude-lane-1 + claude-lane-2 (per roster
        # fixture); freezing lane-1 holds it while lane-2-only agents move.
        for extra in ([], ["--yes"]):
            bundle = self.base("--roster", roster,
                               "--frozen-lanes", "claude-lane-1", *extra)
            names = [p["name"] for p in bundle["patches"]]
            self.assertNotIn("agent-mixed", names)
            ready = [m["name"] for m in bundle["moves"]]
            self.assertNotIn("agent-mixed", ready)
            held = {h["name"]: h for h in bundle["held"]}
            self.assertIn("agent-mixed", held)
            self.assertIn("frozen lane kill switch",
                          held["agent-mixed"]["reason"])
            # other lanes' agents still move.
            ready_all = [m["name"] for m in bundle["moves"]]
            self.assertIn("agent-ahead", ready_all)
            self.assertIn("agent-crit", ready_all)

    def test_05_frozen_holds_missing_or_malformed_roster_mapping(self):
        roster_path = os.path.join(FIX, "agents.json")
        with open(roster_path, encoding="utf-8") as fh:
            original = json.load(fh)

        missing = copy.deepcopy(original)
        missing["agents"] = [row for row in missing["agents"]
                             if row.get("name") != "agent-ahead"]
        malformed = copy.deepcopy(original)
        next(row for row in malformed["agents"]
             if row.get("name") == "agent-ahead")["lanes"] = "claude-lane-2"
        blank = copy.deepcopy(original)
        next(row for row in blank["agents"]
             if row.get("name") == "agent-ahead")["lanes"] = ["   "]
        collision = copy.deepcopy(original)
        collision_row = next(row for row in collision["agents"]
                             if row.get("name") == "agent-ahead")
        collision_row["id"] = "other-company-agent-id"
        collision_row["lanes"] = ["claude-lane-1"]

        id_name_collision = copy.deepcopy(original)
        id_name_collision["agents"] = [
            row for row in id_name_collision["agents"]
            if row.get("id") != "a-ahead"]
        id_name_collision["agents"].append({
            "id": "other-company-agent-id", "name": "a-ahead",
            "lanes": ["claude-lane-1"]})

        for label, roster in (("missing", missing), ("malformed", malformed),
                              ("blank", blank), ("id-collision", collision),
                              ("id-name-key-collision", id_name_collision)):
            with self.subTest(mapping=label):
                variant_path = os.path.join(self.tmp, "roster-%s.json" % label)
                write_json(variant_path, roster)
                bundle = self.base("--roster", variant_path,
                                   "--frozen-lanes", "claude-lane-2", "--yes",
                                   want_rc=5)
                patches = {p["name"]: p for p in bundle["patches"]}
                held = {h["name"]: h for h in bundle["held"]}
                self.assertNotIn("agent-ahead", patches)
                self.assertIn("agent-ahead", held)
                self.assertIn("no roster lane mapping",
                              held["agent-ahead"]["reason"])

    def test_05_duplicate_roster_id_refuses_frozen_mapping(self):
        roster = os.path.join(self.tmp, "roster-duplicate-id.json")
        with open(os.path.join(FIX, "agents.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["agents"].append({"id": "a-ahead", "name": "alias",
                              "lanes": ["claude-lane-1"]})
        write_json(roster, doc)
        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--roster", roster, "--frozen-lanes", "claude-lane-2",
                       "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(cp.stdout, "")
        self.assertIn("duplicate roster agent id", cp.stderr)

    def test_05_frozen_naming_nothing_refuses(self):
        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--roster", os.path.join(FIX, "agents.json"),
                       "--frozen-lanes", "no-such-lane", "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("names no lane", cp.stderr)

    def test_05_frozen_without_roster_refuses(self):
        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--frozen-lanes", "claude-lane-2", "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--roster", cp.stderr)

    # -- §6  directives are asked for -----------------------------------------
    def test_06_shadow_plan_refused_outright(self):
        shadow = make_plan(self.tmp, mode="shadow")
        cp = run_apply("--plan", shadow, "--live", self.live_path, "--yes")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no directives", cp.stderr)

    # -- §7  no cancel vocabulary ---------------------------------------------
    def test_07_output_has_no_cancel_keys(self):
        for extra in ([], ["--yes"]):
            bundle = self.base(*extra)
            blob = json.dumps(bundle).lower()
            for needle in FORBIDDEN_OUTPUT_NEEDLES:
                self.assertNotIn('"%s"' % needle, blob)

    def test_07_source_has_no_cancel_or_network_strings(self):
        # Precise, not substring: "kill switch" is the card's own term for
        # the frozen-lane control, and comments may say the tool opens no
        # socket. What must be absent is the cancel vocabulary as string
        # literals (a mutant action:"cancel" record) and any network/shell
        # client at import or call sites.
        with open(TOOL, encoding="utf-8") as fh:
            src = fh.read()
        for word in FORBIDDEN_OUTPUT_NEEDLES:
            self.assertNotIn('"%s"' % word, src.lower())
        for mod in ("urllib", "requests", "http.client", "socket",
                    "subprocess"):
            self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+%s" % mod,
                                "network/shell import: %s" % mod)
        for call in ("requests.", "urllib.", "socket.", "subprocess.",
                     "os.system", "os.popen", "http.client"):
            self.assertNotIn(call, src)

    # -- §8  rollback ----------------------------------------------------------
    def test_08_rollback_restores_through_same_path(self):
        plan_path = make_plan(self.tmp, mode="enforce",
                              extra_plan_args=("--rollback",))
        cp = run_apply("--plan", plan_path, "--live", self.live_path,
                       "--yes")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        bundle = json.loads(cp.stdout)
        self.assertEqual(bundle["mode"], "rollback")
        moves = {m["name"]: m for m in bundle["moves"]}
        # Only agent-behind is restorable on fixtures: it is the one
        # roster agent off its baseline (2 vs 3) with a live record.
        # agent-cool is also off-baseline but has no live record (held).
        self.assertEqual(set(moves), {"agent-behind"})
        self.assertEqual(moves["agent-behind"]["action"], "restore")
        self.assertEqual(moves["agent-behind"]["target"], 3)
        held = {h["name"]: h for h in bundle["held"]}
        self.assertIn("agent-cool", held)

    def test_08_rollback_needs_yes_like_enforce(self):
        plan_path = make_plan(self.tmp, mode="enforce",
                              extra_plan_args=("--rollback",))
        cp = run_apply("--plan", plan_path, "--live", self.live_path)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        bundle = json.loads(cp.stdout)
        self.assertEqual(bundle["effect"], "none")
        self.assertEqual(bundle["patches"], [])

    def test_08_brake_capped_restore_reports_effective_direction(self):
        plan_path = make_plan(self.tmp, mode="enforce",
                              extra_plan_args=("--rollback",))
        brake_path = os.path.join(self.tmp, "rollback-brake.json")
        write_json(brake_path, {"targets": {"a-behind": 1}})
        cp = run_apply("--plan", plan_path, "--live", self.live_path,
                       "--brake", brake_path, "--yes")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        bundle = json.loads(cp.stdout)
        behind = self.moves_by_name(bundle)["agent-behind"]
        self.assertEqual(behind["target"], 1)
        self.assertEqual(behind["action"], "down")
        self.assertEqual(behind["planAction"], "restore")

    def test_08_agent_already_at_baseline_is_already_applied(self):
        # agent-behind restores 2 -> 3. Live already AT 3 (plan applied
        # once already, or a hand edit to the same value) reports
        # already-applied — not a second patch, and not drift.
        def at_baseline(doc):
            for row in doc["agents"]:
                if row["id"] == "a-behind":
                    row["runtimeConfig"]["heartbeat"]["maxConcurrentRuns"] = 3
        plan_path = make_plan(self.tmp, mode="enforce",
                              extra_plan_args=("--rollback",))
        cp = run_apply("--plan", plan_path,
                       "--live", self.live_variant(at_baseline), "--yes")
        self.assertIn(cp.returncode, (0, 5), cp.stderr)
        bundle = json.loads(cp.stdout)
        already = {e["name"]: e for e in bundle["alreadyApplied"]}
        self.assertIn("agent-behind", already)
        self.assertEqual(bundle["patches"], [])

    # -- §9  offline -----------------------------------------------------------
    def test_09_plan_mode_garbage_refused(self):
        plan_path = os.path.join(self.tmp, "plan-bad.json")
        write_json(plan_path, {"mode": "surprise", "agents": []})
        cp = run_apply("--plan", plan_path, "--live", self.live_path,
                       "--yes")
        self.assertEqual(cp.returncode, 2)

    # -- §10  exit 5 on nothing appliable ---------------------------------------
    def test_10_all_held_exits_5_with_evidence(self):
        def drift_all(doc):
            for row in doc["agents"]:
                row["runtimeConfig"]["heartbeat"]["maxConcurrentRuns"] = 9
        cp = run_apply("--plan", self.plan,
                       "--live", self.live_variant(drift_all))
        self.assertEqual(cp.returncode, 5, cp.stderr)
        bundle = json.loads(cp.stdout)
        self.assertEqual(bundle["summary"]["appliable"], 0)
        self.assertEqual(len(bundle["held"]), 3)

    def test_10_out_writes_atomically(self):
        out_path = os.path.join(self.tmp, "bundle.json")
        cp = run_apply("--plan", self.plan, "--live", self.live_path,
                       "--out", out_path)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        with open(out_path, encoding="utf-8") as fh:
            bundle = json.load(fh)
        self.assertEqual(bundle["summary"]["appliable"], 3)
        leftovers = [f for f in os.listdir(self.tmp)
                     if f.startswith("bundle.json.tmp-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
