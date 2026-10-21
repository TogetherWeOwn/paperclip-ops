#!/usr/bin/env python3
"""Read-only planner for scripts/verify-fast.sh (bundle verification-plan).

Reads the single repo-owned suite inventory (scripts/verify-fast-inventory.json),
evaluates prerequisite readiness with read-only probes (PATH lookup or a local
check command; no suite execution, downloads, cache cleanup, filesystem writes,
network or credentials), and emits the plan.

Internal helper: invoked only by verify-fast.sh for --plan/--dry-run/--list
and the --no-prepare readiness check. Stdlib only. Deterministic output.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:  # Die quietly on a closed pipe (e.g. `make verify-fast-plan | head`).
    from signal import SIG_DFL, SIGPIPE, signal
    signal(SIGPIPE, SIG_DFL)
except (ImportError, AttributeError, OSError):
    pass

PROBE_TIMEOUT = 10


def fail(message):
    print(f"verify-fast-plan: {message}", file=sys.stderr)
    return 2


def load_inventory(path):
    try:
        text = Path(path).read_text()
    except OSError as exc:
        return None, f"cannot read inventory {path}: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"inventory {path} is not valid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("suites"), list):
        return None, f"inventory {path} must be an object with a suites list"
    return data, None


def probe_ready(catalog, name, cache, root):
    """Evaluate one named prerequisite probe; results cached per run."""
    if name in cache:
        return cache[name]
    spec = (catalog.get(name) or {}).get("probe") or {}
    ready = False
    if "which" in spec:
        ready = shutil.which(spec["which"]) is not None
    elif "argv" in spec and isinstance(spec["argv"], list) and spec["argv"]:
        try:
            completed = subprocess.run(
                [str(a) for a in spec["argv"]],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT, cwd=str(root),
            )
            ready = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ready = False
    cache[name] = ready
    return ready


def build_plan(inventory, root, tier=None):
    """Return (plan_dict, readiness_cache); pure read-only evaluation."""
    catalog = inventory.get("prerequisite_catalog") or {}
    cache = {}
    suites = []
    for entry in inventory.get("suites", []):
        if not isinstance(entry, dict):
            continue
        if tier is not None and entry.get("tier") != tier:
            continue
        names = entry.get("prerequisites") or []
        prereqs = [
            {"name": name, "ready": probe_ready(catalog, name, cache, root)}
            for name in names
        ]
        actions = list((entry.get("preparation") or {}).get("actions") or [])
        suites.append({
            "id": entry.get("id"),
            "command": entry.get("command"),
            "tier": entry.get("tier"),
            "prerequisites": prereqs,
            "preparation": {
                "required": bool(actions) and any(not p["ready"] for p in prereqs),
                "actions": actions,
            },
        })
    return {"schema_version": inventory.get("schema_version", 1), "suites": suites}, cache


def command_text(command):
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(c) for c in command)
    return ""


def emit_human(plan):
    lines = [f"verify-fast plan (schema {plan['schema_version']}): "
             f"{len(plan['suites'])} suites"]
    for suite in plan["suites"]:
        missing = [p["name"] for p in suite["prerequisites"] if not p["ready"]]
        readiness = "ready=yes" if not missing else f"ready=no missing={','.join(missing)}"
        lines.append(f"- {suite['id']} [{suite['tier']}] {readiness} "
                     f":: {command_text(suite['command'])}")
    return "\n".join(lines) + "\n"


def main(argv):
    inventory_path = Path(__file__).resolve().parent / "verify-fast-inventory.json"
    mode = None
    tier = None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--inventory" and args:
            inventory_path = Path(args.pop(0))
        elif arg in ("--emit-json", "--emit-text", "--emit-list",
                     "--emit-list-json", "--missing-prereqs"):
            mode = arg
        elif arg == "--tier" and args:
            tier = args.pop(0)
            if tier not in ("parse", "fast"):
                return fail(f"invalid --tier {tier!r}: want parse or fast")
        else:
            return fail(f"unknown flag {arg!r}")
    if mode is None:
        return fail("need one of --emit-json, --emit-text, --emit-list, "
                    "--emit-list-json, --missing-prereqs")
    inventory, error = load_inventory(inventory_path)
    if error is not None:
        return fail(error)
    root = inventory_path.resolve().parent.parent
    plan, _ = build_plan(inventory, root, tier)
    if mode == "--emit-json":
        sys.stdout.write(json.dumps(plan, indent=2, sort_keys=False) + "\n")
    elif mode == "--emit-text":
        sys.stdout.write(emit_human(plan))
    elif mode == "--emit-list":
        for suite in plan["suites"]:
            sys.stdout.write(f"{suite['id']}\n")
    elif mode == "--emit-list-json":
        sys.stdout.write(json.dumps([s["id"] for s in plan["suites"]]) + "\n")
    elif mode == "--missing-prereqs":
        seen = set()
        for suite in plan["suites"]:
            for prereq in suite["prerequisites"]:
                if not prereq["ready"] and prereq["name"] not in seen:
                    seen.add(prereq["name"])
                    sys.stdout.write(f"{prereq['name']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
