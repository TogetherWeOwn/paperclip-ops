#!/usr/bin/env python3
# ===========================================================================
# pacer_apply.py — offline applier-validator for pacer enforce/rollback plans
# (case_755e7d5f slice 2, parent case_58fddbd4; design from case_24119081 ADR "focused
# Capacity Orchestrator")
#
# SCOPE: turns an enforce plan from pacer_concurrency.py (--mode enforce
# --yes) plus operator-fetched live runtimeConfigs into exact board PATCH
# bodies. The fast half (per-run admit/hold) is still shadow: this tool moves
# only maxConcurrentRuns, which queues work instead of discarding it.
#
# WHAT THIS DOES NOT DO (deliberately):
#   * No network, no credential, no board write, and no live sender. It prints
#     whole-runtimeConfig PATCH bodies without a version precondition; a
#     concurrent config edit after the read can be overwritten. Do not queue
#     or apply these bundles unattended. This repo does not establish a safe
#     server-side compare-and-set contract for live use.
#   * No model choice, no pins, no weights, no wakeOnDemand, no daily caps.
#     The write-back body is the live runtimeConfig with exactly one key
#     changed; anything else differing is a refused drift, never a merge.
#   * No cancel/kill/terminate/disable. The output vocabulary is patch/hold/
#     already-applied/skip, pinned by test.
#
# CONTRACT RULES (mirroring quota_brake.sh rule 1, enforced per agent):
#   1. READ-MODIFY-WRITE WHOLESALE. PATCH /api/agents/{id} REPLACES
#      runtimeConfig; it does not merge. The body is deepcopy(live) with
#      only heartbeat.maxConcurrentRuns changed. Verified by diffing the
#      outgoing body against what was read: every other key byte-identical.
#   2. END-STATE FIRST, DRIFT HOLDS. Live already AT want means the move is
#      done, however the world got there (re-running an applied plan reports
#      already-applied, not a second patch). Only a live value that is
#      NEITHER the plan's "from" NOR want is drift, and that agent is held —
#      the applier never rebases a stale plan onto a moved world.
#   3. BRAKE WINS DOWN. With --brake targets, want is min(reconciler, brake).
#      Targets are keyed by agent id; plan/live-name aliases are checked,
#      name-only targets and conflicting aliases refuse the whole run.
#      Insane values also refuse.
#   4. FROZEN HOLDS. With --roster plus --frozen-lanes, a moved agent serving
#      a frozen lane is held in both dry-run and apply. A mapping must be keyed
#      by the selected live id; duplicate IDs, absent or malformed mappings
#      also hold/refuse. A frozen name absent from the roster refuses the
#      whole run: a typo must not freeze nothing.
#   5. AGENT IDENTITY. A directive must carry a usable, path-safe plan id.
#      The selected live row must carry the same id; a same-name fallback never
#      redirects a write to another id.
#   6. DIRECTIVES ARE ASKED FOR. Without --yes the tool validates and reports
#      appliable moves but emits no PATCH bodies (effect:"none"). With --yes
#      it emits bodies (effect:"directive"). A shadow plan (mode shadow) is
#      refused outright: there is nothing to apply.
#   7. PER-AGENT HOLD, NEVER SILENT. Unresolvable agents (no live config, no
#      heartbeat object, drift, frozen, already-applied) are listed with
#      reasons. Held agents are re-planned by the next reconciler window;
#      the bundle still covers every resolvable agent.
#
# EXIT CODES
#   0  bundle emitted with >= 1 appliable move (dry-run validation counts:
#      the moves were proven appliable, bodies withheld by mode, not by data)
#   2  usage / validation (shadow plan, insane want/brake, frozen naming
#      nothing, unreadable input, --frozen-lanes without --roster)
#   5  nothing appliable: every moved agent held, already-applied, or skipped.
#      Zero PATCHes out of a live plan is "nothing to do", not "all clear".
#
# Offline: JSON files in, one JSON bundle out. No network, no credential, no
# clock read. --out writes atomically (temp + os.replace).
# ===========================================================================
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys

MAX_SANE_TARGET = 32
MIN_TARGET = 1

APPLIABLE_ACTIONS = ("up", "down", "restore")


class ApplyError(ValueError):
    """Refused input or an unappliable world. Never emits a partial bundle."""


def _positive_int(raw, what):
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ApplyError("%s %r is not an int" % (what, raw))
    if raw < MIN_TARGET or raw > MAX_SANE_TARGET:
        raise ApplyError("%s %r outside [%d, %d]: refusing, not clamping"
                         % (what, raw, MIN_TARGET, MAX_SANE_TARGET))
    return raw


def _usable_agent_id(value):
    """Return whether value is safe as one unescaped API path segment."""
    return (isinstance(value, str) and value == value.strip()
            and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None)


def _diff_paths(old, new, path=""):
    """Yield dotted paths where two JSON structures differ."""
    if type(old) is not type(new):
        yield path or "<root>"
        return
    if isinstance(old, dict):
        for key in old:
            sub = "%s.%s" % (path, key) if path else str(key)
            if key not in new:
                yield sub
            else:
                for hit in _diff_paths(old[key], new[key], sub):
                    yield hit
        for key in new:
            if key not in old:
                sub = "%s.%s" % (path, key) if path else str(key)
                yield sub
    elif isinstance(old, list):
        if len(old) != len(new):
            yield path or "<root>"
        else:
            for i, (a, b) in enumerate(zip(old, new)):
                for hit in _diff_paths(a, b, "%s[%d]" % (path, i)):
                    yield hit
    else:
        if old != new:
            yield path or "<root>"


def apply_agent(rec, live_by_id, live_by_name, brake, lanes_by_agent, frozen,
                emit_bodies):
    """One plan record -> (entry, ready).

    entry always lands in moves (or skipped for non-directives); ready is
    True when the move is appliable. The PATCH body rides on the entry only
    when emit_bodies — dry-run proves appliability, it does not hand out
    write payloads.
    """
    raw_plan_id = rec.get("agentId")
    raw_name = rec.get("name")
    aid = (raw_plan_id if isinstance(raw_plan_id, str) and raw_plan_id
           else raw_name if isinstance(raw_name, str) and raw_name else "?")
    name = raw_name if isinstance(raw_name, str) and raw_name else aid
    action = rec.get("action")
    if action not in APPLIABLE_ACTIONS or rec.get("effect") != "directive":
        return ({"agentId": aid, "name": name, "action": "skip",
                 "reason": "plan carries no directive for this agent"}, False)
    plan_id = raw_plan_id
    if not _usable_agent_id(plan_id):
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "plan record has no usable agent id"}, False)
    aid = plan_id
    if name == "?":
        name = aid
    live = live_by_id.get(plan_id)
    if live is None:
        live = live_by_name.get(name)
    if live is None:
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "no live runtimeConfig for id %r or name %r"
                           % (aid, name)}, False)
    live_id = live.get("id")
    if not _usable_agent_id(live_id):
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "live agent row has no usable id"}, False)
    if plan_id != live_id:
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "agent id mismatch: plan %r != live %r"
                           % (plan_id, live_id)}, False)
    # Frozen kill switch first: cheapest refusal, checked before drift so a
    # frozen agent never even gets its drift evaluated as appliable.
    if frozen and lanes_by_agent is not None:
        served = lanes_by_agent.get(live_id)
        if not isinstance(served, list) or not served:
            return ({"agentId": aid, "name": name, "action": "held",
                     "reason": "no roster lane mapping under frozen lane kill switch"},
                    False)
        hit = [lane for lane in served if lane in frozen]
        if hit:
            return ({"agentId": aid, "name": name, "action": "held",
                     "reason": "frozen lane kill switch: %s" % ",".join(hit)},
                    False)
    cfg = live.get("runtimeConfig")
    if not isinstance(cfg, dict):
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "live runtimeConfig is not an object"}, False)
    beat = cfg.get("heartbeat")
    if not isinstance(beat, dict):
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "live runtimeConfig has no heartbeat object: "
                           "refusing to write blind"}, False)
    want = _positive_int(rec.get("target"),
                         "plan target for %s" % name)
    plan_from = rec.get("from")
    if isinstance(plan_from, bool) or not isinstance(plan_from, int) \
            or not MIN_TARGET <= plan_from <= MAX_SANE_TARGET:
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "plan from %r is not a sane positive int"
                           % (plan_from,)}, False)
    if (action == "up" and want <= plan_from) \
            or (action == "down" and want >= plan_from):
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "plan action %s does not match target %d from %d"
                           % (action, want, plan_from)}, False)
    if action == "up" and want > plan_from + 1:
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "up plan exceeds one-step target: %d from %d"
                           % (want, plan_from)}, False)
    live_cur = beat.get("maxConcurrentRuns")
    if not isinstance(live_cur, int) or isinstance(live_cur, bool):
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "live maxConcurrentRuns %r is not an int"
                           % (live_cur,)}, False)
    brake_capped = False
    brake_name_aliases = []
    for alias in (name, live.get("name")):
        if isinstance(alias, str) and alias and alias != live_id \
                and alias not in brake_name_aliases:
            brake_name_aliases.append(alias)
    present_aliases = [alias for alias in brake_name_aliases if alias in brake]
    has_id_brake = live_id in brake
    if present_aliases and not has_id_brake:
        raise ApplyError("brake target for %s is name-keyed; require agent id %s"
                         % (present_aliases[0], live_id))
    if has_id_brake:
        capped = _positive_int(brake[live_id],
                               "brake target for %s" % live_id)
        for alias in present_aliases:
            alias_capped = _positive_int(
                brake[alias], "brake name alias for %s" % alias)
            if alias_capped != capped:
                raise ApplyError(
                    "conflicting brake target aliases for %s: id %s=%d, "
                    "name %s=%d" % (name, live_id, capped, alias,
                                     alias_capped))
        if capped < want:
            want = capped
            brake_capped = True
    # End-state first: live already AT want means the move is done, however
    # the world got there (applied plan re-run, hand edit to the same
    # value). Only a live value that is NEITHER from NOR want is drift.
    if want == live_cur:
        return ({"agentId": aid, "name": name, "action": "already-applied",
                 "reason": "live already at target %d" % want,
                 "target": want}, False)
    if live_cur != plan_from:
        return ({"agentId": aid, "name": name, "action": "held",
                 "reason": "drift: live maxConcurrentRuns %r != plan-from %r"
                           % (live_cur, plan_from),
                 "live": live_cur, "planFrom": plan_from}, False)
    body = copy.deepcopy(cfg)
    body["heartbeat"]["maxConcurrentRuns"] = want
    # Wholesale-write proof: the ONLY allowed diff is the one key we moved.
    diffs = [d for d in _diff_paths(cfg, body)
             if d != "heartbeat.maxConcurrentRuns"]
    if diffs:
        raise ApplyError("write-back for %s touches %r: refusing"
                         % (name, diffs))
    effective_action = action
    if action in ("up", "down") or brake_capped:
        effective_action = "up" if want > live_cur else "down"
    entry = {"agentId": aid, "name": name, "action": effective_action,
             "from": live_cur, "target": want}
    if rec.get("critical") is True:
        entry["critical"] = True
    if effective_action != action:
        entry["planAction"] = action
    if brake_capped:
        entry["brakeCapped"] = True
        entry["reason"] = "min(reconciler, brake): brake caps target"
        if effective_action != action:
            entry["reason"] += "; effective action changed from %s to %s" \
                                % (action, effective_action)
    else:
        entry["reason"] = "plan %s %r -> %r" % (effective_action, live_cur,
                                                  want)
    if emit_bodies:
        entry["patch"] = {"method": "PATCH",
                          "path": "/api/agents/%s" % live_id,
                          "body": {"runtimeConfig": body}}
    return (entry, True)


def build_bundle(plan_doc, live_doc, brake, lanes_by_agent, frozen,
                 emit_bodies):
    if not isinstance(plan_doc, dict):
        raise ApplyError("plan doc is not an object")
    mode = plan_doc.get("mode")
    if mode not in ("enforce", "rollback"):
        raise ApplyError("plan mode %r carries no directives: refusing to "
                         "apply a %s plan" % (mode, mode))
    recs = plan_doc.get("agents")
    if not isinstance(recs, list):
        raise ApplyError("plan doc has no agents array")
    expected_actions = ("restore",) if mode == "rollback" else ("up", "down")
    seen_plan_ids = set()
    for rec in recs:
        if not isinstance(rec, dict):
            raise ApplyError("plan record is not an object: %r" % (rec,))
        plan_id = rec.get("agentId")
        if _usable_agent_id(plan_id):
            if plan_id in seen_plan_ids:
                raise ApplyError("duplicate plan agent id %r: refusing bundle"
                                 % plan_id)
            seen_plan_ids.add(plan_id)
        if rec.get("effect") == "directive" \
                and rec.get("action") not in expected_actions:
            raise ApplyError("directive action %r does not match %s plan"
                             % (rec.get("action"), mode))
    if not isinstance(live_doc, dict) or not isinstance(live_doc.get("agents"),
                                                         list):
        raise ApplyError("live doc has no agents array")
    live_by_id = {}
    live_by_name = {}
    for row in live_doc["agents"]:
        if not isinstance(row, dict):
            raise ApplyError("live row is not an object: %r" % (row,))
        row_id = row.get("id")
        if _usable_agent_id(row_id):
            if row_id in live_by_id:
                raise ApplyError("duplicate live agent id %r: refusing apply"
                                 % row_id)
            live_by_id[row_id] = row
        row_name = row.get("name")
        if isinstance(row_name, str) and row_name:
            live_by_name[row_name] = row
    if frozen and lanes_by_agent is None:
        raise ApplyError("--frozen-lanes needs --roster: refusing to freeze "
                         "blind")
    if lanes_by_agent is not None:
        known = set()
        for lanes in lanes_by_agent.values():
            known.update(lanes)
        for lane in frozen:
            if lane not in known:
                raise ApplyError("frozen lane %r names no lane in the roster"
                                 % lane)
    moves, held, already, skipped, patches = [], [], [], [], []
    for rec in recs:
        if not isinstance(rec, dict):
            raise ApplyError("plan record is not an object: %r" % (rec,))
        entry, ready = apply_agent(rec, live_by_id, live_by_name, brake,
                                   lanes_by_agent, frozen, emit_bodies)
        if ready:
            moves.append(entry)
            if emit_bodies:
                patches.append(entry)
            # Without --yes the entry stays in moves bodyless: dry-run proves
            # appliability, it does not hand out write payloads.
        elif entry["action"] == "held":
            held.append(entry)
        elif entry["action"] == "already-applied":
            already.append(entry)
        else:
            skipped.append(entry)
    # Preserve the plan's relative order while placing every critical move last.
    moves.sort(key=lambda entry: entry.get("critical") is True)
    patches.sort(key=lambda entry: entry.get("critical") is True)
    directive_count = sum(1 for rec in recs
                          if rec.get("effect") == "directive")
    kind = "rollback" if mode == "rollback" else "apply"
    return {"mode": kind if emit_bodies else "%s-dry-run" % kind,
            "planMode": mode,
            "effect": "directive" if emit_bodies else "none",
            "moves": moves,
            "patches": patches,
            "held": held, "alreadyApplied": already, "skipped": skipped,
            "summary": {"directives": directive_count,
                        "appliable": len(moves),
                        "patches": len(patches),
                        "held": len(held),
                        "alreadyApplied": len(already),
                        "brakeCapped": len([m for m in moves
                                            if m.get("brakeCapped")])}}


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Offline applier-validator for pacer enforce/rollback "
                    "plans (case_755e7d5f). Emits board PATCH bodies, never "
                    "writes.")
    p.add_argument("--plan", required=True,
                   help="enforce/rollback plan JSON from pacer_concurrency.py")
    p.add_argument("--live", required=True,
                   help="live runtimeConfigs JSON (operator-fetched)")
    p.add_argument("--brake", default=None,
                   help="brake targets JSON {\"targets\": {agent_id: N}}")
    p.add_argument("--roster", default=None,
                   help="agent roster JSON with lane mapping (for --frozen-lanes)")
    p.add_argument("--frozen-lanes", default="",
                   help="comma-separated per-lane kill switch")
    p.add_argument("--yes", action="store_true",
                   help="required to emit PATCH bodies")
    p.add_argument("--out", default=None, help="write bundle JSON here")
    a = p.parse_args(argv)

    frozen = [s for s in (x.strip() for x in a.frozen_lanes.split(",")) if s]

    try:
        plan_doc = load_json(a.plan)
        live_doc = load_json(a.live)
        brake = {}
        if a.brake:
            brake_doc = load_json(a.brake)
            if not isinstance(brake_doc, dict) \
                    or not isinstance(brake_doc.get("targets"), dict):
                raise ApplyError("brake doc has no targets object")
            brake = brake_doc["targets"]
        lanes_by_agent = None
        if a.roster:
            roster_doc = load_json(a.roster)
            if not isinstance(roster_doc, dict) \
                    or not isinstance(roster_doc.get("agents"), list):
                raise ApplyError("roster doc has no agents array")
            lanes_by_agent = {}
            seen_roster_ids = set()
            for row in roster_doc["agents"]:
                if not isinstance(row, dict):
                    raise ApplyError("roster row is not an object")
                row_id = row.get("id")
                if _usable_agent_id(row_id):
                    if row_id in seen_roster_ids:
                        raise ApplyError(
                            "duplicate roster agent id %r: refusing lane map"
                            % row_id)
                    seen_roster_ids.add(row_id)
                lanes = row.get("lanes")
                if _usable_agent_id(row_id) and isinstance(lanes, list) \
                        and all(isinstance(lane, str) and lane
                                and lane.strip() == lane
                                for lane in lanes):
                    lanes_by_agent[row_id] = lanes
        bundle = build_bundle(plan_doc, live_doc, brake, lanes_by_agent,
                              frozen, a.yes)
        text = json.dumps(bundle, indent=2, sort_keys=True)
    except ApplyError as exc:
        print("pacer_apply: refused: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print("pacer_apply: cannot read input: %s" % exc, file=sys.stderr)
        return 2

    # Exit 5 when nothing was appliable, so a scheduler can tell "nothing
    # to do" apart from "moves emitted". The bundle still prints: the held
    # and already-applied lists are the evidence.
    rc = 5 if bundle["summary"]["appliable"] == 0 else 0

    if a.out:
        try:
            tmp = "%s.tmp-%d" % (a.out, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            os.replace(tmp, a.out)
        except OSError as exc:
            print("pacer_apply: cannot write --out: %s" % exc, file=sys.stderr)
            return 2
    else:
        print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
