#!/usr/bin/env python3
"""case_d0c2375d: count open cards whose override pins a healthy main model but whose
sub-call env keys are still frozen on an exhausted lane.

Pages the full board (the list endpoint caps at 200 rows, so a single call
undercounts). For every open card carrying `assigneeAdapterOverrides`, reports:

  - `model` (the main pin) and whether it is on an exhausted lane
  - each of the six model-valued env keys and whether it is on an exhausted lane
  - the split that decides remediation: pin healthy + env dead is the class the
    balance pass does NOT rewrite, because `cheaper || incapable || busier` all
    read off the PIN, never off the env.

Exhausted lanes are passed in (`--dead-prefix`, repeatable) rather than probed,
so the sweep is reproducible from a capture after the lane recovers.
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ["PAPERCLIP_API_URL"].rstrip("/")
if BASE.endswith("/api"):
    BASE = BASE[: -len("/api")]
KEY = os.environ["PAPERCLIP_API_KEY"]
COMPANY = os.environ["PAPERCLIP_COMPANY_ID"]

MODEL_KEYS = [
    "PAPERCLIP_ASSIGNED_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
]
OPEN_STATUSES = {"todo", "in_progress", "in_review", "blocked"}


def call(path):
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": "Bearer " + KEY}
    )
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())


def env_value(entry):
    """A model-valued env entry is `{type: plain, value}`; a secret_ref has no
    readable value and is never counted as dead."""
    if isinstance(entry, dict):
        if entry.get("type") == "secret_ref":
            return None
        return entry.get("value")
    if isinstance(entry, str):
        return entry
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dead-prefix", action="append", default=[])
    ap.add_argument("--page-size", type=int, default=200)
    args = ap.parse_args()
    dead = tuple(args.dead_prefix) or ("gpt-5.6-", "cliproxy/gpt-5.6-")

    def is_dead(model_id):
        if not model_id:
            return False
        bare = model_id.split("/")[-1]
        return any(bare.startswith(p.split("/")[-1]) for p in dead)

    issues, offset = [], 0
    while True:
        page = call(
            f"/api/companies/{COMPANY}/issues?limit={args.page_size}&offset={offset}"
        )
        rows = page if isinstance(page, list) else page.get("issues", [])
        if not rows:
            break
        issues.extend(rows)
        if len(rows) < args.page_size:
            break
        offset += len(rows)

    out = {
        "total_issues_paged": len(issues),
        "open_with_override": 0,
        "pin_dead": 0,
        "pin_healthy_env_dead": 0,
        "fully_clean": 0,
        "dead_key_histogram": {k: 0 for k in MODEL_KEYS},
        "examples": [],
    }

    for issue in issues:
        if issue.get("status") not in OPEN_STATUSES:
            continue
        cfg = (issue.get("assigneeAdapterOverrides") or {}).get("adapterConfig")
        if not cfg:
            continue
        out["open_with_override"] += 1
        pin = cfg.get("model")
        env = cfg.get("env") or {}
        dead_keys = [k for k in MODEL_KEYS if is_dead(env_value(env.get(k)))]
        for k in dead_keys:
            out["dead_key_histogram"][k] += 1
        if is_dead(pin):
            out["pin_dead"] += 1
        elif dead_keys:
            out["pin_healthy_env_dead"] += 1
            if len(out["examples"]) < 5:
                out["examples"].append(
                    {
                        "identifier": issue.get("identifier"),
                        "status": issue.get("status"),
                        "pin": pin,
                        "dead_keys": dead_keys,
                    }
                )
        else:
            out["fully_clean"] += 1

    json.dump(out, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
