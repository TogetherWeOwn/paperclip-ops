#!/usr/bin/env python3
"""case_59a265cf: does losing a lane's pace verdict ERASE an exclusion it had already earned?

Why this script replaces the `--lane`/quality split in `lane_exclusion_audit.py`
-------------------------------------------------------------------------------
The earlier audit split the decision ring by `laneSnapshot.quality`. That field
is an AGGREGATE: `shadow-emit.ts:285` computes it as `allFreshAndClean` over
EVERY lane in the ledger, so it goes `"cached"` when *any* lane is stale or
errored. A codex admission in the `cached` arm therefore does not show that
codex's OWN verdict was missing -- some other lane being stale produces the
same label. That audit cannot support the conclusion it was used for.

The per-lane discriminator this script uses instead
---------------------------------------------------
`shadow-emit.ts:131` collapses two distinct ledger states into one label:

    laneStateLabel(verdict):
      if (!verdict) return "unavailable";              # A: no verdict at all
      ...
      if (verdict.serviceable === false) return "unavailable";   # B: observed unserviceable

so `state == "unavailable"` alone is ambiguous between A and B. The candidate
list disambiguates it exactly, because `hardStopExcluded` is a total function of
that same verdict:

    hardStopExcluded(ledger, model):
      if (!verdict) return false;                      # A -> model SURVIVES into candidates
      if (verdict.serviceable === false) return true;  # B -> model REMOVED from candidates

Therefore, for a lane reading `"unavailable"`:

    lane admitted to `candidates`  <=>  state A (verdict null)   -- fail-open
    lane absent from `candidates`  <=>  state B (observed false) -- correct exclusion

The two cases are mutually exclusive and jointly exhaustive, so the pair
(state label, admitted?) recovers the ledger state the label threw away. No
selector instrumentation is needed. `accounts == []` is carried as a redundant
cross-check: `accountSnapshots(verdict)` is empty exactly when the verdict is
null, and it must agree with the admission-based classification.

What counts as the defect
-------------------------
A lane that was NEVER observed is legitimately fail-neutral -- "an unobserved
lane is not evidence of exhaustion" is the documented intent and this script
does not count it. The defect is strictly the REGRESSION: the ring is walked in
timestamp order, and a lane is only counted as fail-open once it has ALREADY
been seen in state B (correctly excluded on an observed-unserviceable verdict)
earlier in the same ring. That is evidence being lost, not evidence never held.

Exit codes
----------
    0  no regression found for any audited lane
    1  at least one fail-open regression (the defect is present)
    2  not measurable -- no lane was ever observed unserviceable in this ring,
       so the regression is untestable here. An empty arm is not a green.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys


def load(path: str) -> list[dict]:
    records = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    records.sort(key=lambda r: r.get("ts") or "")
    return records


def audit(records: list[dict], lane: str) -> dict:
    counts = collections.Counter()
    observed_unserviceable_at = None
    first_failopen = None
    failopen_examples = []
    disagreements = 0

    for rec in records:
        snap = (rec.get("laneSnapshot") or {}).get("lanes") or {}
        entry = snap.get(lane)
        if entry is None:
            counts["lane-absent-from-snapshot"] += 1
            continue

        state = entry.get("state")
        admitted = any(c.get("lane") == lane for c in (rec.get("candidates") or []))
        accounts_empty = not (entry.get("accounts") or [])
        ts = rec.get("ts")

        if state != "unavailable":
            counts[f"state:{state}" + (" admitted" if admitted else " excluded")] += 1
            if state == "exhausted":
                # `exhausted` with serviceable not false would be its own bug;
                # record it separately rather than folding it into this one.
                if admitted:
                    counts["exhausted-but-admitted"] += 1
            continue

        # state == "unavailable": disambiguate A vs B by admission.
        if admitted:
            # State A: verdict was null. Cross-check against accounts.
            if not accounts_empty:
                disagreements += 1
            if observed_unserviceable_at is not None:
                counts["failopen-regression"] += 1
                if first_failopen is None:
                    first_failopen = ts
                if len(failopen_examples) < 5:
                    failopen_examples.append(
                        {
                            "ts": ts,
                            "issue": rec.get("issueIdentifier"),
                            "tier": rec.get("tier"),
                            "picked": rec.get("pickedModel"),
                            "quality": (rec.get("laneSnapshot") or {}).get("quality"),
                            "laneFetchErrors": (rec.get("laneSnapshot") or {}).get("laneFetchErrors"),
                        }
                    )
            else:
                counts["failopen-never-observed"] += 1
        else:
            # State B: verdict.serviceable === false -- correctly excluded.
            counts["correctly-excluded"] += 1
            if accounts_empty:
                # verdict present but no accounts is possible; not a disagreement.
                pass
            if observed_unserviceable_at is None:
                observed_unserviceable_at = ts

    return {
        "lane": lane,
        "counts": dict(counts),
        "observed_unserviceable_at": observed_unserviceable_at,
        "first_failopen": first_failopen,
        "failopen_examples": failopen_examples,
        "accounts_disagreements": disagreements,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ring",
        default="/paperclip/instances/default/companies/00000000-0000-4000-8000-000000000004"
        "/plugin-local/model-selection/shadow-decisions/decisions.jsonl",
    )
    ap.add_argument("--lane", action="append", default=None, help="repeatable; default = every lane in the ring")
    args = ap.parse_args()

    records = load(args.ring)
    if not records:
        print("not measurable: ring is empty")
        return 2

    lanes = args.lane
    if not lanes:
        seen = set()
        for rec in records:
            seen.update(((rec.get("laneSnapshot") or {}).get("lanes") or {}).keys())
        lanes = sorted(seen)

    print(f"ring: {len(records)} records  {records[0].get('ts')} .. {records[-1].get('ts')}")
    print()

    any_measurable = False
    any_failopen = False
    for lane in lanes:
        result = audit(records, lane)
        measurable = result["observed_unserviceable_at"] is not None
        any_measurable = any_measurable or measurable
        n_fail = result["counts"].get("failopen-regression", 0)
        any_failopen = any_failopen or n_fail > 0

        verdict = "FAIL-OPEN" if n_fail else ("ok" if measurable else "not measurable")
        print(f"== {lane}: {verdict}")
        print(f"   first observed unserviceable : {result['observed_unserviceable_at']}")
        print(f"   first fail-open admission    : {result['first_failopen']}")
        print(f"   accounts/admission disagreements: {result['accounts_disagreements']}")
        for key in sorted(result["counts"]):
            print(f"   {key:42} {result['counts'][key]}")
        for ex in result["failopen_examples"]:
            print(f"   e.g. {ex}")
        print()

    if any_failopen:
        return 1
    if not any_measurable:
        print("not measurable: no lane was ever observed unserviceable in this ring")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
