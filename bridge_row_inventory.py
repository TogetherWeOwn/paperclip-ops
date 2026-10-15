#!/usr/bin/env python3
# ===========================================================================
# bridge_row_inventory.py — Router roster: enabled lane-bound row inventory
# per bridge model (case_611525e4, parent case_cc33e13f; mirror of the case_862c218c Muse,
# case_586352f5 Claude and case_59a1ee73 Codex countdown readouts, for row PRESENCE,
# not burn).
#
# SCOPE: read-only. From the committed assembled roster config only, report
# per bridge model (muse-spark contributor, claude-sonnet-5-5, gpt-6.1-sol)
# how many ENABLED lane-bound rows exist, and fail visibly on zero. No live
# account reads, no pacing change, no operator wait — importing or running
# this module changes no pacing behaviour.
#
# LANE MAP (pinned, not inferred at runtime — provenance):
#   muse-spark-1.3-contributor -> cliproxy-meta   (assemble-additive-config.mjs
#     laneForNewModel `/^muse-/`, pinned by case_ce7af737-meta-lane-rows.spec.ts)
#   claude-sonnet-5-5           -> cliproxy-claude (`/^claude-/`, the reviewed
#     roster's Claude route for the fleet-quota-balancer PRIMARY choice)
#   gpt-6.1-sol                 -> cliproxy-codex   (`/^(?:gpt-|codex-)/`, the
#     reviewed roster's Codex route for the bridge FALLBACK choice)
# A runtime prefix-guess would re-derive this map on every run and could
# drift away from the assembly it claims to inventory (the map would then be
# a second definition of id -> lane). The map lives here as data with its
# provenance above, and §2 of the suite pins each binding against the
# committed reviewed-roster.json ground truth, so drift is a red build.
#
# WHAT COUNTS (both conjuncts, never one — §4 kills the `or` mutant):
#   enabled is True (boolean; truthy 1/"true" is refused, never counted) AND
#   laneId names a lane present in the fixture's lanes array. An enabled row
#   bound to an unknown lane is unlaned: it counts 0 here, exactly as the
#   assembly's enabled-outside-lanes guard refuses it there.
#
# WHAT THIS REFUSES, AND WHY (same contract family as the countdowns):
#   * A bridge model with zero enabled lane-bound rows is an InventoryError
#     naming the model — never a quiet "0" row. Zero coverage is the finding
#     this readout exists to surface (the `[[ "" -eq 0 ]]` bash trap's
#     roster-row costume: an absent row scored as present).
#   * Duplicate rows for one model id are refused: two rows for one id would
#     let a stale row shadow the live one in the count.
#   * Non-bridge rows are ignored, never merged: a total across models would
#     hide a zero model behind its siblings (the averaging mutant's costume).
#   * A missing bridge id is zero, not absent-from-output: every bridge model
#     always emits exactly one row, so a dropped id fails instead of
#     vanishing.
#
# EXIT CODES (cli)
#   0  inventory printed, every bridge model has >= 1 enabled lane-bound row
#   2  usage / validation: bad fixture, a refused row, or any bridge model
#      at zero enabled lane-bound rows (the visible failure this card wants)
#
# Read-only. Opens one JSON fixture, prints to stdout. No network, no
# credential, no roster write, no file written.
# ===========================================================================
from __future__ import annotations

import argparse
import json
import os
import sys

# (model id, tier, expected lane) in display order: bridge MUSE, PRIMARY,
# FALLBACK per the case_cc33e13f bridge ranking.
BRIDGE_MODELS = (
    ("muse-spark-1.3-contributor", "T3", "cliproxy-meta"),
    ("claude-sonnet-5-5", "T2", "cliproxy-claude"),
    ("gpt-6.1-sol", "T1", "cliproxy-codex"),
)


class InventoryError(ValueError):
    """A fixture (or roster state) that cannot be inventoried honestly."""


def _lane_set(doc):
    raw_lanes = doc.get("lanes")
    if not isinstance(raw_lanes, list):
        raise InventoryError("fixture has no lanes array")
    seen = set()
    for entry in raw_lanes:
        lane = entry.get("lane") if isinstance(entry, dict) else None
        if not isinstance(lane, str) or not lane:
            raise InventoryError("fixture has a lane row with no lane name")
        if lane in seen:
            raise InventoryError("lane %s: duplicate lane row" % lane)
        seen.add(lane)
    return seen


def assemble_rows(doc):
    """Pure: one inventory row per bridge model. Input order of the models
    array is irrelevant; output order follows BRIDGE_MODELS. Raises
    InventoryError on a dishonest row, never on a zero count (zero is the
    finding — see require_nonzero)."""
    if not isinstance(doc, dict):
        raise InventoryError("fixture is not an object")
    known_lanes = _lane_set(doc)
    raw_models = doc.get("models")
    if not isinstance(raw_models, list):
        raise InventoryError("fixture has no models array")
    seen_ids = set()
    by_id = {}
    for entry in raw_models:
        if not isinstance(entry, dict):
            raise InventoryError("model ?: row is not an object")
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise InventoryError("model ?: missing or empty id")
        if model_id in seen_ids:
            raise InventoryError("model %s: duplicate model row" % model_id)
        seen_ids.add(model_id)
        by_id[model_id] = entry
    rows = []
    for model_id, tier, expected_lane in BRIDGE_MODELS:
        entry = by_id.get(model_id)
        lanes: list = []
        if entry is not None:
            enabled = entry.get("enabled")
            if type(enabled) is not bool:
                raise InventoryError(
                    "model %s: enabled %r is not a boolean" % (model_id, enabled))
            lane_id = entry.get("laneId")
            # Both conjuncts: enabled AND bound to a known lane. `or` here
            # would count a disabled-but-laned row as coverage (§4).
            if enabled is True and isinstance(lane_id, str) \
                    and lane_id in known_lanes:
                lanes.append(lane_id)
        rows.append({
            "model": model_id,
            "tier": tier,
            "expectedLane": expected_lane,
            "enabledLaneBoundCount": len(lanes),
            "lanes": lanes,
            "status": "present" if lanes else "ZERO",
        })
    return rows


def require_nonzero(rows):
    """Fail visibly on zero: every bridge model must have >= 1 enabled
    lane-bound row. Raises InventoryError naming each zero model."""
    zero = [r["model"] for r in rows if r["enabledLaneBoundCount"] < 1]
    if zero:
        raise InventoryError(
            "zero enabled lane-bound rows for bridge model(s): %s"
            % ", ".join(zero))
    return rows


def format_table(rows):
    """Pure display: fixed-width table, ZERO rows first (the failing model
    is the headline). Sorts a copy; the input list is untouched."""
    ordered = sorted(rows, key=lambda r: (r["enabledLaneBoundCount"], r["model"]))
    lines = ["%-28s %-4s %-16s %5s %s" % (
        "model", "tier", "lane", "count", "status")]
    for row in ordered:
        lane = row["lanes"][0] if row["lanes"] else row["expectedLane"] + " (unbound)"
        lines.append("%-28s %-4s %-16s %5d %s" % (
            row["model"], row["tier"], lane,
            row["enabledLaneBoundCount"], row["status"]))
    return "\n".join(lines) + "\n"


def load_fixture(path):
    """Read the fixture file. The only I/O in this module."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise InventoryError("fixture %s is not an object" % path)
    return doc


def main(argv=None):
    default_fixture = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "tests", "fixtures", "case_381fc549", "bridge-assembled-rows.json")
    p = argparse.ArgumentParser(
        description="Bridge-model enabled lane-bound row inventory "
                    "(read-only).")
    p.add_argument("--fixture", default=default_fixture)
    p.add_argument("--json", action="store_true",
                   help="emit the inventory rows as JSON instead of the "
                        "human table")
    a = p.parse_args(argv)

    try:
        doc = load_fixture(a.fixture)
        rows = require_nonzero(assemble_rows(doc))
    except (OSError, json.JSONDecodeError) as exc:
        print("bridge_row_inventory: cannot read fixture: %s" % exc,
              file=sys.stderr)
        return 2
    except InventoryError as exc:
        print("bridge_row_inventory: refused: %s" % exc, file=sys.stderr)
        return 2

    if a.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print("Bridge row inventory (%d models, all present)" % len(rows))
        print(format_table(rows), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
