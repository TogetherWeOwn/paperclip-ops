import ast
import copy
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch

from garm.desired_target import RESOURCES, propose_target

AT = "2026-10-04T10:30:00Z"


def fixture():
    return {
        "snapshot": {
            "evidence_class": "synthetic",
            "pool": {
                "id": "fixture-pool", "resource_domain": "fixture-domain",
                "generation": 7, "current_target": 4, "role": "isolated-private",
                "selector": "fixture-isolated-only", "image_fingerprint": "a" * 64,
                "profile": {"requested_cpu": 2, "requested_ram_mib": 4096,
                            "requested_disk_gib": 20},
            },
            "inventory": {
                "busy": 1, "booting": 0, "idle": 2,
                "reservations": ["fixture-unstarted-1"], "complete": True,
                "observed_at": AT,
            },
            "queue": {
                "class": "private-garm", "visibility": "private", "role": "isolated-private",
                "selector": "fixture-isolated-only", "image_fingerprint": "a" * 64,
                "count": 10, "complete": True, "observed_at": AT,
            },
            "readback": {"generation": 7, "target": 4, "observed_at": AT},
            "warmup_complete": True, "idle_only_downscale_verified": True,
        },
        "configured_bounds": {"pool_id": "fixture-pool", "min_target": 0, "max_target": 8},
        "validated_bounds": {
            "pool_id": "fixture-pool", "resource_domain": "fixture-domain", "generation": 7,
            "max_target": 6, "observed_at": AT, "valid_until": "2026-10-04T10:45:00Z",
            "consistent": True, "all_reservations_accounted": True, "pressure_clear": True,
            # Occupied inventory this headroom was measured against: busy 1 +
            # booting 0 + 1 reservation + idle 2.
            "accounted_occupied": 4,
            "available_cpu": 4, "available_ram_mib": 8192, "available_disk_gib": 40,
        },
        "requested_target": 6, "expected_generation": 7, "at": AT,
    }


def rollback_fixture():
    case = fixture()
    case["requested_target"] = 0
    case["snapshot"]["inventory"].update(booting=1, idle=1)
    case["snapshot"]["warmup_complete"] = False
    return case


def change(case, path, value):
    parts = path.split(".")
    target = case
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


class DesiredTargetTest(unittest.TestCase):
    def assert_hold(self, path, value, reason, case=None):
        case = fixture() if case is None else case
        change(case, path, value)
        result = propose_target(**case)
        self.assertEqual(result["action"], "hold", path)
        self.assertEqual(result["reason"], reason, path)
        self.assertTrue(result["desired_target"] is None or result["desired_target"] <= 4, path)
        self.assertIs(result["actuation_authorized"], False)
        return result

    def test_positive_expansion_is_desired_only(self):
        result = propose_target(**fixture())
        self.assertEqual(result, {
            "mode": "desired-only", "actuation_authorized": False,
            "action": "propose", "desired_target": 6, "reason": "safe_expansion",
            "protected_floor": 2, "retained_reservations": ["fixture-unstarted-1"],
        })

    def test_each_resource_exact_boundary_and_one_unit_short(self):
        for resource in RESOURCES:
            with self.subTest(resource=resource):
                case = fixture()
                case["configured_bounds"]["max_target"] = 20
                case["validated_bounds"]["max_target"] = 20
                for other in RESOURCES:
                    case["validated_bounds"]["available_" + other] = 100000
                cost = case["snapshot"]["pool"]["profile"]["requested_" + resource]
                case["validated_bounds"]["available_" + resource] = 2 * cost
                self.assertEqual(propose_target(**case)["desired_target"], 6)
                self.assertEqual(propose_target(**case)["action"], "propose")
                # The requested resources now exceed measured headroom by ONE unit.
                case["validated_bounds"]["available_" + resource] -= 1
                result = propose_target(**case)
                self.assertEqual((result["action"], result["desired_target"]), ("clamp", 5))
                self.assertEqual(result["reason"], "headroom_" + resource)

    def test_each_resource_zero_headroom_holds_instead_of_downscaling(self):
        for resource in RESOURCES:
            self.assert_hold("validated_bounds.available_" + resource, 0, "headroom_" + resource)

    def test_configured_and_validated_max_are_independent(self):
        for bound, reason in (("configured_bounds", "configured_max"),
                              ("validated_bounds", "validated_max")):
            with self.subTest(bound=bound):
                case = fixture()
                case[bound]["max_target"] = 5
                result = propose_target(**case)
                self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                                 ("clamp", 5, reason))
        case = fixture()
        case["configured_bounds"]["max_target"] = 1000
        self.assertEqual(propose_target(**case)["desired_target"], 6)
        case["requested_target"] = 1000
        self.assertEqual(propose_target(**case)["desired_target"], 6)

    def test_unmet_eligible_demand_can_only_tighten_the_bounds(self):
        # Fixture idle=2 serves a single queued job: floor 2 + max(2, 1) = 4
        # stays within the current total, so demand only holds.
        case = fixture()
        case["snapshot"]["queue"]["count"] = 1
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "eligible_demand"))
        self.assert_hold("snapshot.queue.count", 0, "no_eligible_demand")

    def test_idle_runners_serve_queued_demand_before_expansion(self):
        # Exact boundary: 2 idle runners cover 2 queued jobs, no new slot.
        case = fixture()
        case["snapshot"]["queue"]["count"] = 2
        case["requested_target"] = 6
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "eligible_demand"))
        # One unit over: 3 queued jobs need one slot beyond idle, 2 + 3 = 5.
        case["snapshot"]["queue"]["count"] = 3
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("clamp", 5, "eligible_demand"))

    def test_expired_validated_max_does_not_deepen_downscale(self):
        # Fresh validated max still caps a downscale inside its envelope.
        case = fixture()
        case["validated_bounds"]["max_target"] = 2
        case["requested_target"] = 3
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("clamp", 2, "pool_bound"))
        # The same budget expired two hours earlier cannot force the extra
        # drain: the configured maximum applies instead.
        case["validated_bounds"].update(observed_at="2026-10-04T08:15:00Z",
                                        valid_until="2026-10-04T08:30:00Z")
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("propose", 3, "safe_downscale"))
        # Unparseable budget timestamps are treated as not fresh, same result.
        case["validated_bounds"].update(observed_at="2026-10-04T08:15:00Z",
                                        valid_until=None)
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("propose", 3, "safe_downscale"))

    def test_public_standard_hosted_demand_never_expands(self):
        self.assert_hold("snapshot.queue.class", "public-standard-hosted", "ineligible_queue_class")
        self.assert_hold("snapshot.queue.visibility", "public", "ineligible_queue_class")
        self.assert_hold("snapshot.queue.class", "unknown", "ineligible_queue_class")

    def test_private_label_and_image_mismatch_are_not_capacity_shortage(self):
        self.assert_hold("snapshot.queue.selector", "fixture-build-only", "private_label_mismatch")
        self.assert_hold("snapshot.queue.role", "privileged-private", "private_label_mismatch")
        self.assert_hold("snapshot.queue.image_fingerprint", "b" * 64, "private_image_mismatch")

    def test_queue_quality_holds_expansion(self):
        self.assert_hold("snapshot.queue.complete", False, "queue_incomplete")
        for timestamp in ("2026-10-04T10:14:59Z", "2026-10-04T10:30:01Z"):
            self.assert_hold("snapshot.queue.observed_at", timestamp,
                             "queue.observed_at: stale_or_future")

    def test_unknown_and_contradictory_budgets_hold(self):
        self.assert_hold("validated_bounds", None, "validated_bounds: object required")
        self.assert_hold("validated_bounds.max_target", None, "validated_bounds.max_target")
        self.assert_hold("validated_bounds.consistent", False, "contradictory_budget")
        self.assert_hold("validated_bounds.pressure_clear", False, "resource_pressure")
        self.assert_hold("validated_bounds.all_reservations_accounted", False,
                         "reservations_unaccounted")
        self.assert_hold("configured_bounds.min_target", 9, "configured_bounds_conflict")
        self.assert_hold("configured_bounds.min_target", 7, "minimum_exceeds_validated_bound")

    def test_budget_age_and_expiry_have_exact_inclusive_boundaries(self):
        case = fixture()
        case["validated_bounds"]["observed_at"] = "2026-10-04T10:15:00Z"
        case["validated_bounds"]["valid_until"] = AT
        self.assertEqual(propose_target(**case)["action"], "propose")
        for timestamp in ("2026-10-04T10:14:59Z", "2026-10-04T10:30:01Z"):
            self.assert_hold("validated_bounds.observed_at", timestamp,
                             "validated_bounds.observed_at: stale_or_future")
        self.assert_hold("validated_bounds.valid_until", "2026-10-04T10:29:59Z",
                         "validated_budget_expired")

    def test_pool_and_domain_bounds_cannot_be_transferred(self):
        for field in ("configured_bounds.pool_id", "validated_bounds.pool_id",
                      "validated_bounds.resource_domain"):
            reason = {"configured_bounds.pool_id": "configured_pool_mismatch",
                      "validated_bounds.pool_id": "validated_pool_mismatch",
                      "validated_bounds.resource_domain": "resource_domain_mismatch"}[field]
            self.assert_hold(field, "fixture-other", reason)

    def test_generation_and_readback_mismatch_hold(self):
        for path in ("expected_generation", "snapshot.pool.generation", "validated_bounds.generation"):
            self.assert_hold(path, 8, "generation_mismatch")
        self.assert_hold("snapshot.readback.generation", 8, "readback_mismatch")
        self.assert_hold("snapshot.readback.target", 3, "readback_mismatch")
        self.assert_hold("snapshot.readback.observed_at", "2026-10-04T10:14:59Z",
                         "readback.observed_at: stale_or_future")
        self.assert_hold("snapshot.readback.target", 3, "readback_mismatch", rollback_fixture())

    def test_warmup_or_booting_inventory_holds_expansion(self):
        self.assert_hold("snapshot.warmup_complete", False, "warmup_pending")
        case = fixture()
        case["snapshot"]["inventory"].update(booting=1, idle=1)
        # A contradicted warmup flag cannot hide actually booting work.
        self.assert_hold("snapshot.warmup_complete", True, "warmup_pending", case)

    def test_unfilled_current_target_still_consumes_future_headroom(self):
        case = fixture()
        case["snapshot"]["inventory"]["idle"] = 0
        # Occupied drops 4 -> 2; the budget must be measured against the same
        # live inventory before it can fence expansion.
        case["validated_bounds"]["accounted_occupied"] = 2
        # Occupied=2, current desired=4: adding a fifth needs THREE missing slots,
        # not just requested-current=1. No resources were freed by the old cap.
        case["requested_target"] = 5
        case["validated_bounds"]["available_cpu"] = 2
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "headroom_cpu"))

    def test_changed_occupancy_with_stale_accounting_holds_expansion(self):
        # P1: the budget was measured against occupied=4 at 10:20. Two prior
        # unfilled desired slots have since become busy (occupied=6... capped
        # here by current=6 after raising it), with no new headroom observed.
        # Reusing the still-age-valid budget must not propose a sixth slot.
        case = fixture()
        case["snapshot"]["pool"]["current_target"] = 6
        case["snapshot"]["readback"]["target"] = 6
        case["snapshot"]["inventory"].update(busy=3, idle=2)
        case["configured_bounds"]["max_target"] = 20
        case["validated_bounds"]["max_target"] = 20
        case["requested_target"] = 8
        # Occupied is now 3 + 0 + 1 + 2 = 6; the budget still accounts 4.
        result = propose_target(**case)
        self.assertEqual((result["action"], result["reason"]),
                         ("hold", "accounting_observation_mismatch"))
        self.assertEqual(result["desired_target"], 6)
        # Re-baselining the budget against the new live inventory restores
        # the expansion decision; nothing else changed.
        case["validated_bounds"]["accounted_occupied"] = 6
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"],
                          result["reason"]), ("propose", 8, "safe_expansion"))

    def test_unfilled_desired_capacity_absorbs_demand_before_increase(self):
        # P2: occupied=2 with current=4 leaves two unfilled desired slots.
        # One unmet job fits inside the already-desired footprint; it must
        # not justify raising the target to 5.
        case = fixture()
        case["snapshot"]["inventory"]["idle"] = 0
        case["validated_bounds"]["accounted_occupied"] = 2
        case["snapshot"]["queue"]["count"] = 1
        case["requested_target"] = 5
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "eligible_demand"))
        # Two unmet jobs need one slot beyond the live footprint: 2 + 2 = 4,
        # still within the current total, so still a hold.
        case["snapshot"]["queue"]["count"] = 2
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "eligible_demand"))

    def test_rollback_preserves_busy_booting_and_outstanding_reservations(self):
        case = rollback_fixture()
        before = copy.deepcopy(case)
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["protected_floor"]),
                         ("clamp", 3, 3))
        self.assertEqual(result["reason"], "active_work_floor")
        self.assertEqual(result["retained_reservations"], ["fixture-unstarted-1"])
        self.assertEqual(case, before)
        result["retained_reservations"].clear()
        self.assertEqual(case, before)

    def test_rollback_floor_can_exceed_new_caps_without_cancelling_work(self):
        case = rollback_fixture()
        case["configured_bounds"]["max_target"] = 1
        case["validated_bounds"]["max_target"] = 1
        result = propose_target(**case)
        self.assertEqual(result["desired_target"], 3)
        self.assertEqual(result["reason"], "active_work_floor")
        self.assertIs(result["actuation_authorized"], False)

    def test_each_additional_reservation_is_protected(self):
        case = rollback_fixture()
        case["snapshot"]["pool"]["current_target"] = 5
        case["snapshot"]["readback"]["target"] = 5
        case["snapshot"]["inventory"]["reservations"].append("fixture-unstarted-2")
        result = propose_target(**case)
        self.assertEqual(result["desired_target"], 4)
        self.assertEqual(result["retained_reservations"], ["fixture-unstarted-1", "fixture-unstarted-2"])

    def test_rollback_does_not_need_a_positive_expansion_signal(self):
        case = rollback_fixture()
        case["snapshot"]["queue"] = None
        case["validated_bounds"].update(observed_at="2026-10-04T09:00:00Z", pressure_clear=False,
                                        available_cpu=None, available_ram_mib=None, available_disk_gib=None)
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"]), ("clamp", 3))
        self.assertEqual(result["retained_reservations"], ["fixture-unstarted-1"])

    def test_downscale_requires_verified_idle_only_semantics(self):
        self.assert_hold("snapshot.idle_only_downscale_verified", False,
                         "idle_only_downscale_unverified", rollback_fixture())

    def test_safe_idle_downscale_and_unchanged_target(self):
        case = fixture()
        case["requested_target"] = 3
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("propose", 3, "safe_downscale"))
        case["requested_target"] = 4
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "unchanged"))

    def test_configured_minimum_is_not_an_expansion_loophole(self):
        case = fixture()
        case["configured_bounds"]["min_target"] = 3
        case["requested_target"] = 0
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("clamp", 3, "configured_minimum"))
        case["configured_bounds"]["min_target"] = 5
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "downscale_would_expand"))

    def test_expansion_cannot_land_below_configured_minimum(self):
        case = fixture()
        case["configured_bounds"]["min_target"] = 6
        case["requested_target"] = 5
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "requested_below_configured_minimum"))
        case["requested_target"] = 6
        case["validated_bounds"]["available_cpu"] = 2
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"], result["reason"]),
                         ("hold", 4, "no_feasible_configured_minimum"))

    def test_unknown_or_contradictory_inventory_is_not_zero(self):
        for path, value, reason in (
            ("snapshot.inventory.complete", False, "inventory_incomplete"),
            ("snapshot.inventory.observed_at", "2026-10-04T10:14:59Z",
             "inventory.observed_at: stale_or_future"),
            ("snapshot.inventory.busy", None, "inventory.busy"),
            ("snapshot.inventory.reservations", ["fixture-unstarted-1", "fixture-unstarted-1"],
             "duplicate_reservation"),
        ):
            result = self.assert_hold(path, value, reason)
            self.assertIsNone(result["desired_target"])
        result = self.assert_hold("snapshot.inventory.busy", 5, "inventory_target_conflict")
        self.assertIsNone(result["desired_target"])
        self.assertEqual(result["protected_floor"], 6)
        self.assertIsNone(result["retained_reservations"])

    def test_idle_driven_inventory_conflict_yields_no_validated_target(self):
        # P2: busy=1 plus idle=3 plus one reservation gives occupied=5 above
        # current=4 while the floor stays 2. Rejected inventory must not be
        # returned as a validated numeric hold with retained IDs.
        case = fixture()
        case["snapshot"]["inventory"]["idle"] = 3
        result = propose_target(**case)
        self.assertEqual((result["action"], result["reason"]),
                         ("hold", "inventory_target_conflict"))
        self.assertIsNone(result["desired_target"])
        self.assertEqual(result["protected_floor"], 2)
        self.assertIsNone(result["retained_reservations"])

    def test_unknown_headroom_or_malformed_profile_never_expands(self):
        for resource in RESOURCES:
            for value in (None, True, -1, 1.5, float("nan"), float("inf"), "100"):
                self.assert_hold("validated_bounds.available_" + resource, value,
                                 "validated_bounds.available_" + resource)
            self.assert_hold("snapshot.pool.profile.requested_" + resource, 0,
                             "profile.requested_" + resource)

    def test_missing_extra_and_live_evidence_fields_fail_closed(self):
        case = fixture()
        del case["validated_bounds"]["available_cpu"]
        result = propose_target(**case)
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["reason"], "validated_bounds: missing or unexpected fields")
        case = fixture()
        case["snapshot"]["actuator"] = "fixture-not-a-client"
        self.assertEqual(propose_target(**case)["action"], "hold")
        self.assert_hold("snapshot.evidence_class", "live", "synthetic_only")
        self.assert_hold("requested_target", True, "requested_target")
        self.assert_hold("expected_generation", True, "expected_generation")
        self.assert_hold("at", "unknown", "evaluation_time")

    def test_zero_inventory_is_not_implicitly_expansion_demand(self):
        case = fixture()
        case["snapshot"]["pool"]["current_target"] = 0
        case["snapshot"]["readback"]["target"] = 0
        case["snapshot"]["inventory"].update(busy=0, booting=0, idle=0, reservations=[])
        case["validated_bounds"]["accounted_occupied"] = 0
        case["requested_target"] = 0
        self.assertEqual(propose_target(**case)["desired_target"], 0)
        case["requested_target"] = 1
        case["snapshot"]["queue"]["count"] = 0
        result = propose_target(**case)
        self.assertEqual((result["action"], result["desired_target"]), ("hold", 0))
        case["snapshot"]["queue"]["count"] = 1
        self.assertEqual(propose_target(**case)["desired_target"], 1)

    def test_all_actions_are_deterministic_pure_and_make_zero_actuator_calls(self):
        cases = [fixture(), rollback_fixture(), fixture()]
        cases[2]["snapshot"]["queue"]["class"] = "public-standard-hosted"
        before = copy.deepcopy(cases)
        seams = ("builtins.open", "socket.socket", "urllib.request.urlopen", "subprocess.Popen",
                 "os.system", "pathlib.Path.write_text", "pathlib.Path.write_bytes")
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch(seam, side_effect=AssertionError("actuator/I/O called")))
                     for seam in seams]
            results = [propose_target(**case) for case in cases]
            self.assertEqual({r["action"] for r in results}, {"propose", "clamp", "hold"})
            for case, first in zip(cases, results):
                self.assertEqual(propose_target(**case), first)
                self.assertIs(first["actuation_authorized"], False)
            for mock in mocks:
                mock.assert_not_called()
        self.assertEqual(cases, before)

    def test_module_has_no_client_or_actuator_imports(self):
        source = Path(__file__).with_name("garm") / "desired_target.py"
        tree = ast.parse(source.read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertTrue(all(isinstance(node, ast.ImportFrom) for node in imports))
        self.assertEqual({node.module for node in imports}, {"datetime", "validate_receipt"})


if __name__ == "__main__":
    unittest.main()
