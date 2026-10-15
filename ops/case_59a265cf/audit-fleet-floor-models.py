#!/usr/bin/env python3
"""Try to audit every agent's floor model (`adapterConfig.model`) from an agent token.

Root cause #1 on case_59a265cf rests on "23 active agents carry
adapterConfig.model=gpt-5.6-sol". This script exists to show that an agent
*cannot* re-derive that figure, and to make the reason unmistakable rather than
looking like a null result.

Two things the API does that will mislead you:

  1. `GET /companies/{id}/agents` (the list) omits `adapterConfig` entirely.
  2. `GET /agents/{id}` (the detail) returns the FULL `adapterConfig` for the
     calling agent and a bare `{}` for every other agent. That is redaction, not
     an unset field — reading it as "no floor model configured" inverts the
     finding.

Measured 2026-09-16 across all 26 agents: `adapterConfig.model` was non-null for
exactly 1, the caller. The discriminator this script prints is `self_visible` vs
`foreign_empty` — if those two buckets split cleanly on "is it me", the field is
redacted and the fleet-wide number has to come from the database or the operator.

Usage:  PAPERCLIP_API_URL=... PAPERCLIP_API_KEY=... PAPERCLIP_COMPANY_ID=... \
        PAPERCLIP_AGENT_ID=... python3 audit-fleet-floor-models.py
"""

import collections
import json
import os
import urllib.request

BASE = os.environ["PAPERCLIP_API_URL"].rstrip("/").removesuffix("/api")
KEY = os.environ["PAPERCLIP_API_KEY"]
COMPANY = os.environ["PAPERCLIP_COMPANY_ID"]
SELF = os.environ.get("PAPERCLIP_AGENT_ID")


def get(path):
    req = urllib.request.Request(
        f"{BASE}/api{path}", headers={"Authorization": f"Bearer {KEY}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def env_value(env, key):
    """adapterConfig.env values are {"type": "plain", "value": ...} or secret refs."""
    entry = (env or {}).get(key)
    return entry.get("value") if isinstance(entry, dict) else entry


def main():
    listing = get(f"/companies/{COMPANY}/agents")
    agents = listing if isinstance(listing, list) else listing.get("items", [])

    # The list view is the first trap: confirm adapterConfig is simply absent
    # there, so nobody concludes the fleet has no floor models from the cheap call.
    list_has_config = sum(1 for a in agents if a.get("adapterConfig") is not None)
    print(f"agents: {len(agents)}")
    print(f"list view carrying adapterConfig at all: {list_has_config}/{len(agents)}")

    buckets = collections.Counter()
    models = collections.Counter()
    rows = []

    for agent in agents:
        detail = get(f"/agents/{agent['id']}")
        config = detail.get("adapterConfig") or {}
        model = config.get("model")
        assigned = env_value(config.get("env"), "PAPERCLIP_ASSIGNED_MODEL")

        is_self = detail.get("id") == SELF
        if config:
            buckets["self_visible" if is_self else "foreign_visible"] += 1
        else:
            buckets["self_empty" if is_self else "foreign_empty"] += 1

        models[model] += 1
        rows.append((detail.get("name"), detail.get("status"), model, assigned))

    print(f"adapterConfig.model: {dict(models)}")
    print(f"visibility: {dict(buckets)}")
    for row in sorted(rows, key=lambda r: str(r[0])):
        print(f"  {row}")

    # The verdict the doc cites. A clean split proves redaction rather than absence.
    if buckets["foreign_visible"] == 0 and buckets["self_visible"] > 0:
        print(
            "\nVERDICT: adapterConfig is redacted for foreign agents. "
            "The fleet-wide floor-model figure is NOT auditable from an agent token."
        )
    else:
        print("\nVERDICT: foreign adapterConfig readable — re-derive the figure directly.")


if __name__ == "__main__":
    main()
