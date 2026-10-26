#!/usr/bin/env python3
# ===========================================================================
# test_bridge_row_inventory.py — hostile tests for the bridge-model enabled
# lane-bound row inventory (case_611525e4, parent case_cc33e13f; mirror of the
# case_862c218c/14609/14457 countdown suites, for row PRESENCE, not burn).
#
# Written to FAIL a plausible-but-wrong implementation, not to confirm the
# one that exists:
#
# * §2 kills roster-drift silence: the fixture re-expresses the committed
#   reviewed-roster.json bridge rows, so every row is pinned against that
#   file (id present, enabled true, tier exact). A roster edit that disables
#   a bridge row turns this suite red instead of letting the fixture lie.
# * §3 kills the quiet-zero mutant: a disabled, missing, or unlaned bridge
#   row must count 0 AND fail visibly (require_nonzero names the model).
#   Zero coverage is the finding, never a clean "0" line.
# * §4 kills the `or`-instead-of-`and` mutant (disabled-but-laned counted as
#   coverage), the truthy-enabled mutant (1/"true" counted), the duplicate
#   mutant (a stale second row inflating the count), and the averaging
#   mutant (a healthy sibling hiding a zero model — per-model rows stay
#   separate, exactly as countdown lanes are never averaged).
# * §5 kills the silent-CLI mutant: the CLI exits 2 with the zero model
#   named on stderr, and prints the table only when all three are present.
#
# Deterministic: no clock, no network, no writes outside a temp dir, no live
# lane reads. The only repo read outside the fixture is the committed
# reviewed-roster.json in §2.
# ===========================================================================
import json
import os
import subprocess
import sys
import tempfile
import unittest
from public_snapshot_test_support import private_artifact_absent

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge_row_inventory import (  # noqa: E402
    BRIDGE_MODELS,
    InventoryError,
    assemble_rows,
    format_table,
    require_nonzero,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "fixtures", "case_381fc549",
                        "bridge-assembled-rows.json")
READOUT = os.path.join(HERE, "bridge_row_inventory.py")
REVIEWED_ROSTER = os.path.join(
    HERE, "plugins", "model-selection", "config", "reviewed-roster.json")

EXPECTED = {
    "muse-spark-1.3-contributor": ("T3", "cliproxy-meta"),
    "claude-sonnet-5-5": ("T2", "cliproxy-claude"),
    "gpt-6.1-sol": ("T1", "cliproxy-codex"),
}


def doc(models, lanes=("cliproxy-meta", "cliproxy-claude", "cliproxy-codex")):
    return {"lanes": [{"lane": lane} for lane in lanes],
            "models": models}


def row(model_id, enabled=True, lane_id=None, tier=None):
    entry = {"id": model_id, "enabled": enabled,
             "laneId": lane_id if lane_id is not None
             else EXPECTED[model_id][1]}
    if tier is not None:
        entry["tier"] = tier
    return entry


def full_doc():
    return doc([row(mid) for mid in EXPECTED])


def by_model(rows):
    return {r["model"]: r for r in rows}


class FixtureTest(unittest.TestCase):
    """§1: the committed assembled fixture carries exactly the 3 bridge
    rows, each enabled and bound to its assembly lane."""

    def test_fixture_is_present_with_three_bridge_rows(self):
        self.assertTrue(os.path.exists(FIXTURE),
                        "fixture %s is missing -- §1 is the headline case "
                        "and must never silently skip" % FIXTURE)
        with open(FIXTURE, encoding="utf-8") as fh:
            fixture = json.load(fh)
        self.assertEqual(
            sorted(m["id"] for m in fixture["models"]),
            sorted(EXPECTED))
        for entry in fixture["models"]:
            tier, lane = EXPECTED[entry["id"]]
            self.assertEqual(entry["tier"], tier)
            self.assertIs(entry["enabled"], True)
            self.assertEqual(entry["laneId"], lane)
        self.assertEqual(
            sorted(e["lane"] for e in fixture["lanes"]),
            ["cliproxy-claude", "cliproxy-codex", "cliproxy-meta"])

    def test_bridge_map_covers_exactly_the_three_bridge_models(self):
        self.assertEqual(sorted(m for m, _, _ in BRIDGE_MODELS),
                         sorted(EXPECTED))
        for model_id, tier, lane in BRIDGE_MODELS:
            self.assertEqual((tier, lane), EXPECTED[model_id])

    def test_happy_path_counts_one_enabled_lane_bound_row_each(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            rows = require_nonzero(assemble_rows(json.load(fh)))
        self.assertEqual(
            [(r["model"], r["enabledLaneBoundCount"], r["status"])
             for r in rows],
            [(m, 1, "present") for m, _, _ in BRIDGE_MODELS])
        heads = by_model(rows)
        for model_id, (_, lane) in EXPECTED.items():
            self.assertEqual(heads[model_id]["lanes"], [lane])


class RosterGroundTruthTest(unittest.TestCase):
    """§2: the fixture matches the committed reviewed-roster.json — id
    present, enabled true, tier exact. Roster drift is a red build."""

    def test_fixture_matches_reviewed_roster_ground_truth(self):
        if private_artifact_absent(HERE, "plugins/model-selection/config/reviewed-roster.json"):
            self.skipTest("private deployment roster is excluded from the fresh public snapshot; synthetic bridge inventory and refusal tests remain enabled")
        self.assertTrue(os.path.exists(REVIEWED_ROSTER),
                        "reviewed roster %s is missing" % REVIEWED_ROSTER)
        with open(REVIEWED_ROSTER, encoding="utf-8") as fh:
            roster = json.load(fh)
        with open(FIXTURE, encoding="utf-8") as fh:
            fixture = json.load(fh)
        roster_by_id = {m["id"]: m for m in roster["models"]}
        for entry in fixture["models"]:
            live = roster_by_id.get(entry["id"])
            self.assertIsNotNone(
                live, "bridge row %s left the reviewed roster" % entry["id"])
            self.assertIs(live["enabled"], True,
                          "bridge row %s is no longer enabled in the "
                          "reviewed roster" % entry["id"])
            self.assertEqual(live["tier"], entry["tier"],
                             "bridge row %s changed tier" % entry["id"])


class ZeroFailsVisiblyTest(unittest.TestCase):
    """§3: zero enabled lane-bound rows is refused naming the model."""

    def test_disabled_row_counts_zero_and_fails_naming_model(self):
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("claude-sonnet-5-5", enabled=False),
            row("gpt-6.1-sol")]))
        self.assertEqual(by_model(rows)["claude-sonnet-5-5"][
            "enabledLaneBoundCount"], 0)
        self.assertEqual(by_model(rows)["claude-sonnet-5-5"]["status"], "ZERO")
        with self.assertRaises(InventoryError) as ctx:
            require_nonzero(rows)
        self.assertIn("claude-sonnet-5-5", str(ctx.exception))

    def test_missing_row_counts_zero_not_absent(self):
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("gpt-6.1-sol")]))
        self.assertEqual(len(rows), 3)
        self.assertEqual(by_model(rows)["claude-sonnet-5-5"][
            "enabledLaneBoundCount"], 0)
        with self.assertRaises(InventoryError) as ctx:
            require_nonzero(rows)
        self.assertIn("claude-sonnet-5-5", str(ctx.exception))

    def test_enabled_but_unlaned_row_counts_zero(self):
        entry = row("gpt-6.1-sol")
        del entry["laneId"]
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("claude-sonnet-5-5"), entry]))
        self.assertEqual(by_model(rows)["gpt-6.1-sol"][
            "enabledLaneBoundCount"], 0)
        with self.assertRaises(InventoryError) as ctx:
            require_nonzero(rows)
        self.assertIn("gpt-6.1-sol", str(ctx.exception))

    def test_enabled_but_unknown_lane_counts_zero(self):
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("claude-sonnet-5-5"),
            row("gpt-6.1-sol", lane_id="cliproxy-typo")]))
        self.assertEqual(by_model(rows)["gpt-6.1-sol"][
            "enabledLaneBoundCount"], 0)
        with self.assertRaises(InventoryError) as ctx:
            require_nonzero(rows)
        self.assertIn("gpt-6.1-sol", str(ctx.exception))

    def test_all_zero_names_every_zero_model(self):
        rows = assemble_rows(doc([], lanes=[]))
        with self.assertRaises(InventoryError) as ctx:
            require_nonzero(rows)
        for model_id in EXPECTED:
            self.assertIn(model_id, str(ctx.exception))

    def test_refused_zero_leaves_no_partial_claim(self):
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("claude-sonnet-5-5", enabled=False),
            row("gpt-6.1-sol")]))
        with self.assertRaises(InventoryError):
            require_nonzero(rows)
        # The healthy siblings still counted — the failure names only the
        # zero model, it does not poison the other rows.
        self.assertEqual(by_model(rows)["muse-spark-1.3-contributor"][
            "enabledLaneBoundCount"], 1)


class MutantKillTest(unittest.TestCase):
    """§4: the `or`, truthy-enabled, duplicate, and averaging mutants die."""

    def test_disabled_but_laned_row_is_not_coverage(self):
        # Kills `enabled or laned`: either conjunct alone must not count.
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor", enabled=False),
            row("claude-sonnet-5-5"),
            row("gpt-6.1-sol")]))
        self.assertEqual(by_model(rows)["muse-spark-1.3-contributor"][
            "enabledLaneBoundCount"], 0)
        with self.assertRaises(InventoryError) as ctx:
            require_nonzero(rows)
        self.assertIn("muse-spark-1.3-contributor", str(ctx.exception))

    def test_truthy_non_boolean_enabled_is_refused_never_counted(self):
        for bad in (1, "true", None):
            with self.assertRaises(InventoryError) as ctx:
                assemble_rows(doc([
                    row("muse-spark-1.3-contributor"),
                    row("claude-sonnet-5-5"),
                    dict(row("gpt-6.1-sol"), enabled=bad)]))
            self.assertIn("gpt-6.1-sol", str(ctx.exception))

    def test_duplicate_model_row_is_refused(self):
        with self.assertRaises(InventoryError) as ctx:
            assemble_rows(doc([
                row("muse-spark-1.3-contributor"),
                row("muse-spark-1.3-contributor"),
                row("claude-sonnet-5-5"),
                row("gpt-6.1-sol")]))
        self.assertIn("muse-spark-1.3-contributor", str(ctx.exception))

    def test_healthy_sibling_never_hides_a_zero_model(self):
        # Kills the averaging mutant: two healthy models do not excuse the
        # third. The inventory is per-model, never a total.
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("claude-sonnet-5-5", enabled=False),
            row("gpt-6.1-sol")]))
        counts = [r["enabledLaneBoundCount"] for r in rows]
        self.assertEqual(sorted(counts), [0, 1, 1])
        with self.assertRaises(InventoryError) as ctx:
            require_nonzero(rows)
        self.assertIn("claude-sonnet-5-5", str(ctx.exception))
        self.assertNotIn("muse-spark-1.3-contributor", str(ctx.exception))

    def test_non_bridge_rows_are_ignored_never_merged(self):
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("claude-sonnet-5-5"),
            row("gpt-6.1-sol"),
            {"id": "gpt-6-astra", "enabled": True,
             "laneId": "cliproxy-codex"}]))
        self.assertEqual(len(rows), 3)
        require_nonzero(rows)  # must not raise

    def test_missing_id_and_non_object_rows_are_refused(self):
        with self.assertRaises(InventoryError):
            assemble_rows(doc([{"enabled": True, "laneId": "cliproxy-meta"},
                               row("claude-sonnet-5-5"),
                               row("gpt-6.1-sol")]))
        with self.assertRaises(InventoryError):
            assemble_rows({"lanes": [{"lane": "cliproxy-meta"}],
                           "models": ["muse-spark-1.3-contributor"]})

    def test_duplicate_lane_and_missing_lanes_array_are_refused(self):
        with self.assertRaises(InventoryError) as ctx:
            assemble_rows(doc([row("muse-spark-1.3-contributor")],
                              lanes=("cliproxy-meta", "cliproxy-meta")))
        self.assertIn("cliproxy-meta", str(ctx.exception))
        with self.assertRaises(InventoryError):
            assemble_rows({"models": []})


class DisplayTest(unittest.TestCase):
    """Display: ZERO rows headline first without mutating the input."""

    def test_zero_rows_sort_first_and_input_untouched(self):
        rows = assemble_rows(doc([
            row("muse-spark-1.3-contributor"),
            row("claude-sonnet-5-5", enabled=False),
            row("gpt-6.1-sol")]))
        before = [r["model"] for r in rows]
        table = format_table(rows)
        body = table.splitlines()[1:]
        self.assertTrue(body[0].startswith("claude-sonnet-5-5"))
        self.assertIn("ZERO", body[0])
        self.assertIn("(unbound)", body[0])
        self.assertEqual([r["model"] for r in rows], before)

    def test_present_rows_show_lane_count_and_status(self):
        table = format_table(require_nonzero(assemble_rows(full_doc())))
        for model_id, (_, lane) in EXPECTED.items():
            line = next(ln for ln in table.splitlines()
                        if ln.startswith(model_id))
            self.assertIn(lane, line)
            self.assertIn("1", line.split())
            self.assertIn("present", line)


class CliTest(unittest.TestCase):
    """§5: the CLI prints the table on green, exits 2 naming the model."""

    def test_cli_prints_table_on_fixture(self):
        proc = subprocess.run(
            [sys.executable, READOUT], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Bridge row inventory (3 models, all present)",
                      proc.stdout)
        for model_id, (_, lane) in EXPECTED.items():
            self.assertIn(model_id, proc.stdout)
            self.assertIn(lane, proc.stdout)

    def test_cli_json_mode_emits_rows(self):
        proc = subprocess.run(
            [sys.executable, READOUT, "--json"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)
        self.assertEqual(
            [(r["model"], r["enabledLaneBoundCount"]) for r in rows],
            [(m, 1) for m, _, _ in BRIDGE_MODELS])

    def test_cli_fails_visibly_on_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            zero_fixture = os.path.join(tmp, "zero-rows.json")
            with open(zero_fixture, "w", encoding="utf-8") as fh:
                json.dump(doc([
                    row("muse-spark-1.3-contributor"),
                    row("claude-sonnet-5-5", enabled=False),
                    row("gpt-6.1-sol")]), fh)
            proc = subprocess.run(
                [sys.executable, READOUT, "--fixture", zero_fixture],
                capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("claude-sonnet-5-5", proc.stderr)
        self.assertNotIn("claude-sonnet-5-5\n", proc.stdout)

    def test_cli_rejects_unreadable_fixture(self):
        proc = subprocess.run(
            [sys.executable, READOUT, "--fixture",
             os.path.join(HERE, "does-not-exist.json")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


def load_tests(loader, standard_tests, pattern):
    """Keep report acceptance reachable through the existing CI entry point."""
    standard_tests.addTests(loader.loadTestsFromName(
        "test_bridge_inventory_report_acceptance"))
    return standard_tests


if __name__ == "__main__":
    unittest.main()
