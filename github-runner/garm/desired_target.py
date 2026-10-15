"""Pure synthetic GARM target boundary; proposals never authorize actuation."""

from datetime import timedelta

from .validate_receipt import (
    InvalidReceipt, ROLES, digest, instant, integer, obj, require, text,
)

RESOURCES = ("cpu", "ram_mib", "disk_gib")
MAX_AGE = timedelta(minutes=15)  # Offline fixture horizon, not a deployed policy.


def propose_target(snapshot, configured_bounds, validated_bounds,
                   requested_target, expected_generation, at):
    """Return desired-only hold/clamp/propose without I/O, clocks or mutation.

    Headroom is net of ALL live inventory, outstanding reservations (including
    other host workloads), and safety margins. Each reservation ID represents
    one additional unstarted slot, NOT a slot already in busy/booting/idle.
    Queue count is unmet eligible demand, excluding those reserved slots.
    Validated headroom carries the occupied total it was measured against;
    expansion requires that binding to match live inventory. Unknown inventory
    yields no numeric target; it is never read as empty.
    """
    current = floor = reservations = None
    inventory_ok = False

    def result(action, target, reason):
        retained = (list(reservations)
                    if (inventory_ok and reservations is not None) else None)
        return {
            "mode": "desired-only", "actuation_authorized": False,
            "action": action, "desired_target": target, "reason": reason,
            "protected_floor": floor,
            "retained_reservations": retained,
        }

    def hold(reason):
        # A numeric hold at the current total is validated evidence only when
        # the inventory consistency check passed. Rejected inventory yields no
        # numeric target; retained IDs are cleared above for the same reason.
        target = (current if (inventory_ok and floor is not None
                              and current >= floor) else None)
        return result("hold", target, reason)

    def fresh(value, field):
        observed = instant(value, field)
        require(timedelta(0) <= now - observed <= MAX_AGE, field + ": stale_or_future")
        return observed

    try:
        now = instant(at, "evaluation_time")
        integer(requested_target, 0, "requested_target")
        integer(expected_generation, 0, "expected_generation")
        s = obj(snapshot, {"evidence_class", "pool", "inventory", "queue", "readback",
                           "warmup_complete", "idle_only_downscale_verified"}, "snapshot")
        require(s["evidence_class"] == "synthetic", "synthetic_only")
        pool = obj(s["pool"], {"id", "resource_domain", "generation", "current_target",
                               "role", "selector", "image_fingerprint", "profile"}, "pool")
        for key in ("id", "resource_domain", "selector"):
            text(pool[key], "pool." + key)
        integer(pool["generation"], 0, "pool.generation")
        require(type(pool["role"]) is str and pool["role"] in ROLES, "pool.role")
        digest(pool["image_fingerprint"], 64, "pool.image_fingerprint")
        current = integer(pool["current_target"], 0, "pool.current_target")
        profile = obj(pool["profile"], {"requested_" + r for r in RESOURCES}, "profile")
        for resource in RESOURCES:
            integer(profile["requested_" + resource], 1, "profile.requested_" + resource)

        inventory = obj(s["inventory"], {"busy", "booting", "idle", "reservations",
                                         "complete", "observed_at"}, "inventory")
        require(inventory["complete"] is True, "inventory_incomplete")
        fresh(inventory["observed_at"], "inventory.observed_at")
        for key in ("busy", "booting", "idle"):
            integer(inventory[key], 0, "inventory." + key)
        ids = inventory["reservations"]
        require(type(ids) is list, "inventory.reservations")
        for reservation in ids:
            text(reservation, "inventory.reservation_id")
        require(len(set(ids)) == len(ids), "duplicate_reservation")
        reservations = list(ids)
        floor = inventory["busy"] + inventory["booting"] + len(reservations)
        occupied = floor + inventory["idle"]
        require(occupied <= current, "inventory_target_conflict")
        inventory_ok = True

        configured = obj(configured_bounds, {"pool_id", "min_target", "max_target"},
                         "configured_bounds")
        require(configured["pool_id"] == pool["id"], "configured_pool_mismatch")
        minimum = integer(configured["min_target"], 0, "configured_bounds.min_target")
        configured_max = integer(configured["max_target"], 0, "configured_bounds.max_target")
        require(minimum <= configured_max, "configured_bounds_conflict")
        validated = obj(validated_bounds, {"pool_id", "resource_domain", "generation",
                                           "max_target", "observed_at", "valid_until",
                                           "consistent", "all_reservations_accounted",
                                           "pressure_clear", "accounted_occupied"}
                        | {"available_" + r for r in RESOURCES}, "validated_bounds")
        require(validated["pool_id"] == pool["id"], "validated_pool_mismatch")
        require(validated["resource_domain"] == pool["resource_domain"],
                "resource_domain_mismatch")
        integer(validated["generation"], 0, "validated_bounds.generation")
        validated_max = integer(validated["max_target"], 0, "validated_bounds.max_target")
        require(validated["consistent"] is True, "contradictory_budget")
        require(minimum <= validated_max, "minimum_exceeds_validated_bound")
        cap = min(configured_max, validated_max)

        readback = obj(s["readback"], {"generation", "target", "observed_at"}, "readback")
        integer(readback["generation"], 0, "readback.generation")
        integer(readback["target"], 0, "readback.target")
        fresh(readback["observed_at"], "readback.observed_at")
        require(pool["generation"] == expected_generation == validated["generation"],
                "generation_mismatch")
        require(readback["generation"] == expected_generation
                and readback["target"] == current, "readback_mismatch")
        for key in ("warmup_complete", "idle_only_downscale_verified"):
            require(type(s[key]) is bool, "snapshot." + key)

        if requested_target == current:
            return hold("unchanged")
        if requested_target < current:
            require(s["idle_only_downscale_verified"], "idle_only_downscale_unverified")
            lower = max(minimum, floor)
            # An expired validated budget must not force a deeper drain than
            # requested: expansion rejects the same evidence, so the
            # validated max caps a downscale only while its budget is fresh.
            # Unparseable timestamps are treated as not fresh.
            bound = cap
            try:
                seen = instant(validated["observed_at"], "validated_bounds.observed_at")
                until = instant(validated["valid_until"], "validated_bounds.valid_until")
                if not seen <= now <= until:
                    bound = configured_max
            except InvalidReceipt:
                bound = configured_max
            target = max(lower, min(requested_target, bound))
            require(target <= current, "downscale_would_expand")
            reason = ("active_work_floor" if target == floor and target > requested_target
                      else "configured_minimum" if target > requested_target
                      else "pool_bound" if target < requested_target else "safe_downscale")
            action = "hold" if target == current else "clamp" if target != requested_target else "propose"
            return result(action, target, reason)

        observed = fresh(validated["observed_at"], "validated_bounds.observed_at")
        expires = instant(validated["valid_until"], "validated_bounds.valid_until")
        require(observed <= now <= expires, "validated_budget_expired")
        require(validated["all_reservations_accounted"] is True, "reservations_unaccounted")
        require(validated["pressure_clear"] is True, "resource_pressure")
        require(s["warmup_complete"] and inventory["booting"] == 0, "warmup_pending")
        # The headroom below was measured net of a specific occupancy. A newer
        # inventory with different occupancy is not fenced by this budget's
        # age or predicates; expansion needs the same accounting observation.
        accounted = integer(validated["accounted_occupied"], 0,
                            "validated_bounds.accounted_occupied")
        require(accounted == occupied, "accounting_observation_mismatch")

        queue = obj(s["queue"], {"class", "visibility", "role", "selector",
                                 "image_fingerprint", "count", "complete", "observed_at"}, "queue")
        require(queue["class"] == "private-garm" and queue["visibility"] == "private",
                "ineligible_queue_class")
        require(queue["role"] == pool["role"] and queue["selector"] == pool["selector"],
                "private_label_mismatch")
        digest(queue["image_fingerprint"], 64, "queue.image_fingerprint")
        require(queue["image_fingerprint"] == pool["image_fingerprint"], "private_image_mismatch")
        require(queue["complete"] is True, "queue_incomplete")
        fresh(queue["observed_at"], "queue.observed_at")
        count = integer(queue["count"], 0, "queue.count")
        require(count > 0, "no_eligible_demand")

        # Idle runners serve queued demand before any increase, and unfilled
        # prior desired capacity absorbs demand: occupied <= current passed
        # above, so current - occupied slots were desired but never filled
        # and are not reservations. The retained footprint is the floor
        # (busy + booting + reservations); the demand total is that floor
        # plus whichever is larger of idle capacity and unmet demand, not
        # current plus demand.
        demand_total = floor + max(inventory["idle"], count)
        ceilings = [(configured_max, "configured_max"), (validated_max, "validated_max"),
                    (demand_total, "eligible_demand")]
        for resource in RESOURCES:
            available = integer(validated["available_" + resource], 0,
                                "validated_bounds.available_" + resource)
            # A prior desired cap is not a reservation. Charge every missing slot,
            # including prior unfilled capacity; never reclaim inventory on paper.
            ceilings.append((occupied + available // profile["requested_" + resource],
                             "headroom_" + resource))
        ceiling, reason = min(ceilings, key=lambda item: item[0])
        if ceiling <= current:
            return hold(reason)
        require(requested_target >= minimum, "requested_below_configured_minimum")
        require(ceiling >= minimum, "no_feasible_configured_minimum")
        target = min(requested_target, ceiling)
        return result("clamp" if target != requested_target else "propose", target,
                      reason if target != requested_target else "safe_expansion")
    except InvalidReceipt as error:
        return hold(str(error))
