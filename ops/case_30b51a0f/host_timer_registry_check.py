#!/usr/bin/env python3
"""Offline gate for the host timer registry (case_30b51a0f, workstream 7 of case_dc8671fe).

The registry (host-timer-registry.json) lists every recurring clock that can act
on this installation. This script enforces what "consolidated" means, with no
network, no credentials and no clock read:

  I1  one primary per purpose        (redundancy and supersession must be declared)
  I2  one primary writer per target  (native upstream machinery is exempt: we cannot merge it)
  I3  every systemd timer is bounded (TimeoutStartSec or a timeout wrapper; Restart absent/no)
  I4  the registry matches the files  (schedules of repo-sourced timers, plugin jobs, workflows)
  I5  nothing recurring is unregistered (every *.timer, plugin job schedule and workflow cron in
                                         the repo must belong to an entry)
  I6  a transition is well formed     (supersededBy resolves, same purpose, retireGate present;
                                         retiredWith names the retire-pending entry it leaves with)

With --live <capture.json> (output of host_timer_capture.py) it also compares the
registry to what the host actually runs. With --end-state it fails while any
transition (supersededBy, redundancy-pending, planned, unverified) remains, which is
the acceptance test for "consolidation finished".

Exit: 0 clean | 1 violations | 2 usage or unreadable input | 3 could not measure
(a live capture scope was unavailable: that is not a clean result).
"""

import argparse
import configparser
import json
import re
import subprocess
import sys
from pathlib import Path

KINDS = {"systemd-timer", "plugin-job", "board-routine", "gh-actions-schedule", "native"}
ROLES = {"detector", "writer", "producer"}
STATES = {"active", "planned", "retire-pending", "retired", "paused"}
PRIMARY_STATES = {"active", "planned"}
IMPLEMENTED_WAIVERS = {"bounded-runtime"}
REQUIRED = ("id", "kind", "scope", "source", "purpose", "role", "writes", "reports",
            "schedule", "bound", "receipt", "state", "expectLive", "verified", "owner")
VENDOR_PREFIXES = ("/usr/lib/systemd/", "/lib/systemd/")
BAD_RESTART = {"always", "on-success", "on-failure", "on-abnormal", "on-abort", "on-watchdog"}
CRON_RE = re.compile(r"^\s*-?\s*cron:\s*['\"]([^'\"]+)['\"]", re.M)
SCHEDULE_RE = re.compile(r"\bschedule:\s*\"([^\"]+)\"")


def load_registry(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("schemaVersion") != 1 or not isinstance(doc.get("entries"), list):
        raise ValueError("registry: schemaVersion 1 with an entries list is required")
    return doc["entries"]


def parse_unit(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    return cp


def is_primary(entry):
    return (entry.get("state") in PRIMARY_STATES and not entry.get("supersededBy")
            and not entry.get("redundancyOf") and not entry.get("retiredWith"))


def waived(entry, check):
    return any(w.get("check") == check for w in entry.get("waivers", []))


def check_schema(entries, out):
    seen = set()
    for e in entries:
        eid = e.get("id", "<no id>")
        missing = [k for k in REQUIRED if k not in e]
        if missing:
            out.append(f"{eid}: missing field(s) {missing}")
            continue
        if eid in seen:
            out.append(f"{eid}: duplicate id")
        seen.add(eid)
        if e["kind"] not in KINDS:
            out.append(f"{eid}: kind {e['kind']!r} not in {sorted(KINDS)}")
        if e["role"] not in ROLES:
            out.append(f"{eid}: role {e['role']!r} not in {sorted(ROLES)}")
        if e["state"] not in STATES:
            out.append(f"{eid}: state {e['state']!r} not in {sorted(STATES)}")
        if e["role"] == "detector" and e["writes"]:
            out.append(f"{eid}: a detector must not write ({e['writes']}); declare it a writer")
        if e["role"] == "writer" and e["state"] in PRIMARY_STATES and e["bound"] in ("", "unknown") \
                and e["source"].get("type") not in ("host-only", "planned", "native"):
            out.append(f"{eid}: a writer must declare how its actions are bounded")
        for w in e.get("waivers", []):
            if not all(w.get(k) for k in ("check", "reason", "fix")):
                out.append(f"{eid}: a waiver needs check, reason and fix")


def check_purposes(entries, out):
    by_purpose = {}
    for e in entries:
        if is_primary(e):
            by_purpose.setdefault(e["purpose"], []).append(e["id"])
    for purpose, ids in sorted(by_purpose.items()):
        if len(ids) > 1:
            out.append(f"I1 purpose {purpose!r} has {len(ids)} primaries {ids}: "
                       "keep one and mark the others supersededBy or redundancyOf")


def check_writers(entries, out):
    owner_of = {}
    for e in entries:
        if not is_primary(e) or e["source"].get("type") == "native" or e["kind"] == "native":
            continue
        for target in e["writes"]:
            if target in owner_of and owner_of[target] != e["id"]:
                out.append(f"I2 target {target!r} has two primary writers: "
                           f"{owner_of[target]} and {e['id']}")
            owner_of.setdefault(target, e["id"])


def check_transitions(entries, out):
    ids = {e["id"]: e for e in entries if "id" in e}
    for e in entries:
        for key in ("supersededBy", "redundancyOf"):
            ref = e.get(key)
            if not ref:
                continue
            tgt = ids.get(ref)
            if tgt is None:
                out.append(f"I6 {e['id']}: {key} {ref!r} does not exist")
                continue
            if tgt["purpose"] != e["purpose"]:
                out.append(f"I6 {e['id']}: {key} {ref!r} serves purpose {tgt['purpose']!r}, not {e['purpose']!r}")
            if tgt["state"] in ("retired", "retire-pending"):
                out.append(f"I6 {e['id']}: {key} points at {ref!r}, which is itself {tgt['state']}")
        dep = e.get("retiredWith")
        if dep and (dep not in ids or ids[dep]["state"] != "retire-pending"):
            out.append(f"I6 {e['id']}: retiredWith {dep!r} must name a retire-pending entry")
        if e.get("supersededBy") and not e.get("retireGate"):
            out.append(f"I6 {e['id']}: supersededBy needs a retireGate (the evidence that lets it go)")
        if e.get("redundancyOf") and not e.get("redundancyReason"):
            out.append(f"I6 {e['id']}: redundancyOf needs a redundancyReason (a different failure domain)")
        if e.get("state") == "retire-pending" and not (e.get("supersededBy") or e.get("retiredWith")):
            out.append(f"I6 {e['id']}: retire-pending without supersededBy or retiredWith")


def sd_schedule(timer_cfg):
    keys = ("OnCalendar", "OnBootSec", "OnUnitActiveSec", "OnActiveSec", "OnStartupSec",
            "OnUnitInactiveSec")
    return {k: timer_cfg["Timer"][k] for k in keys if timer_cfg.has_option("Timer", k)}


def check_repo_timer(entry, root, out):
    timer_rel = entry["source"]["timer"]
    timer_path = root / timer_rel
    if not timer_path.is_file():
        out.append(f"I4 {entry['id']}: timer file {timer_rel} not found")
        return
    timer = parse_unit(timer_path)
    if sd_schedule(timer) != entry["schedule"]:
        out.append(f"I4 {entry['id']}: schedule {entry['schedule']} != {sd_schedule(timer)} in {timer_rel}")
    unit = timer.get("Timer", "Unit", fallback=None)
    if not unit:
        out.append(f"I4 {entry['id']}: {timer_rel} has no Unit=")
        return
    svc_path = timer_path.parent / unit
    if not svc_path.is_file():
        out.append(f"I4 {entry['id']}: service {unit} not found next to {timer_rel}")
        return
    svc = parse_unit(svc_path)
    if svc.get("Service", "Type", fallback="simple") != "oneshot":
        out.append(f"I3 {entry['id']}: {unit} is not Type=oneshot")
    restart = svc.get("Service", "Restart", fallback="no")
    if restart in BAD_RESTART:
        out.append(f"I3 {entry['id']}: {unit} has Restart={restart}; a timer-driven oneshot must not self-retry")
    exec_start = " ".join(svc.get("Service", k, fallback="") for k in ("ExecStart",))
    bounded = svc.has_option("Service", "TimeoutStartSec") or re.search(r"(^|/)timeout\s", exec_start)
    if not bounded and not waived(entry, "bounded-runtime"):
        out.append(f"I3 {entry['id']}: {unit} has no TimeoutStartSec and no timeout wrapper")


def read_schedules(path, root):
    text = (root / path).read_text(encoding="utf-8")
    return SCHEDULE_RE.findall(text)


def check_plugin_jobs(entries, root, out):
    declared = {}
    for e in entries:
        if e["kind"] == "plugin-job" and e["source"].get("type") == "repo":
            declared.setdefault(e["source"]["manifest"], []).extend(e["schedule"].get("jobs", []))
    for manifest in {str(p.relative_to(root)) for pat in ("plugins/*/src/manifest.ts",
                                                           "plugins/*/dist/manifest.js")
                     for p in root.glob(pat)}:
        src = manifest.replace("/dist/manifest.js", "/src/manifest.ts")
        # A plugin with both a src and a dist manifest is registered against src.
        if manifest.endswith("dist/manifest.js") and (root / src).is_file():
            continue
        live = sorted(read_schedules(manifest, root))
        if manifest not in declared:
            if live:
                out.append(f"I5 {manifest} declares {len(live)} job schedule(s) but no registry entry cites it")
            continue
        if sorted(declared[manifest]) != live:
            out.append(f"I4 {manifest}: registry jobs {sorted(declared[manifest])} != manifest {live}")


def check_workflows(entries, root, out):
    declared = {e["source"]["workflow"]: e["schedule"].get("cron")
                for e in entries if e["kind"] == "gh-actions-schedule"}
    for wf in sorted((root / ".github" / "workflows").glob("*.yml")):
        rel = str(wf.relative_to(root))
        crons = CRON_RE.findall(wf.read_text(encoding="utf-8"))
        if crons and rel not in declared:
            out.append(f"I5 {rel} has a cron schedule {crons} but no registry entry")
        elif rel in declared and crons != [declared[rel]]:
            out.append(f"I4 {rel}: registry cron {declared[rel]!r} != workflow {crons}")


def tracked_timers(root):
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "*.timer"],
                             capture_output=True, text=True, check=True)
        return sorted(res.stdout.split())
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*.timer"))


def check_unregistered_timers(entries, root, out):
    cited = {e["source"].get("timer") for e in entries if e["kind"] == "systemd-timer"}
    for rel in tracked_timers(root):
        if rel not in cited:
            out.append(f"I5 {rel} is not in the registry: classify it (purpose, role, writes) before it ships")
    for e in entries:
        if e["kind"] == "systemd-timer" and e["source"].get("type") == "repo":
            check_repo_timer(e, root, out)


def check_end_state(entries, out):
    for e in entries:
        if e.get("supersededBy") or e.get("retiredWith") or e.get("state") in ("retire-pending", "planned"):
            out.append(f"END {e['id']}: still a transition ({e['state']})")
        elif e["state"] != "retired" and not e["verified"]:
            out.append(f"END {e['id']}: not verified against a live capture")
        if e.get("redundancyOf") is None and e.get("waivers"):
            out.append(f"END {e['id']}: still carries waivers {[w['check'] for w in e['waivers']]}")


def unit_names(entry):
    names = set()
    if entry.get("unit"):
        names.add(entry["unit"])
    timer = entry["source"].get("timer")
    if timer:
        names.add(Path(timer).name)
    ev = re.findall(r"([A-Za-z0-9_.@-]+\.timer)", entry["source"].get("evidence", ""))
    names.update(ev)
    return names


def check_live(entries, capture, out):
    """Compare the registry to a host capture. Returns True when a scope could not be measured."""
    unmeasured = False
    live = {}
    for scope in ("user", "system"):
        sc = capture.get("scopes", {}).get(scope)
        if not sc or not sc.get("available"):
            out.append(f"LIVE scope {scope!r} was not measured ({(sc or {}).get('reason', 'absent')})")
            unmeasured = True
            continue
        for t in sc.get("timers", []):
            live[(scope, t["unit"])] = t
    by_unit = {}
    for e in entries:
        if e["kind"] != "systemd-timer":
            continue
        for n in unit_names(e):
            by_unit.setdefault((e["scope"], n), e)
    for (scope, unit), t in sorted(live.items()):
        e = by_unit.get((scope, unit))
        if e is None and str(t.get("fragmentPath") or "").startswith(VENDOR_PREFIXES):
            continue  # shipped by the distribution (apt-daily, logrotate, ...), not ours to consolidate
        if e is None:
            out.append(f"LIVE {scope}/{unit}: on the host, not in the registry (classify it)")
            continue
        if e["state"] == "retired":
            out.append(f"LIVE {scope}/{unit}: registry says retired, host still has it ({t.get('unitFileState')})")
        elif t.get("activeState") == "active" and t.get("service", {}).get("result") not in (None, "success", ""):
            out.append(f"LIVE {scope}/{unit}: last run result {t['service']['result']!r}")
        elif e["state"] in PRIMARY_STATES | {"retire-pending"} and e["expectLive"] is True \
                and t.get("unitFileState") not in ("enabled", "static", "enabled-runtime"):
            out.append(f"LIVE {scope}/{unit}: expected live but unitFileState={t.get('unitFileState')}")
    present = {n for (_, n) in live}
    for e in entries:
        if e["kind"] == "systemd-timer" and e["expectLive"] is True and e["state"] != "retired":
            if not (unit_names(e) & present):
                out.append(f"LIVE {e['id']}: expected live, absent from the host capture")
    return unmeasured


def run(args):
    root = Path(args.root).resolve()
    reg_path = Path(args.registry) if args.registry else root / "ops/case_30b51a0f/host-timer-registry.json"
    try:
        entries = load_registry(reg_path)
    except (OSError, ValueError) as exc:
        print(f"unreadable registry: {exc}", file=sys.stderr)
        return 2
    out = []
    check_schema(entries, out)
    if not out:
        check_purposes(entries, out)
        check_writers(entries, out)
        check_transitions(entries, out)
        check_unregistered_timers(entries, root, out)
        check_plugin_jobs(entries, root, out)
        check_workflows(entries, root, out)
    unmeasured = False
    if args.live:
        try:
            capture = json.loads(Path(args.live).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"unreadable capture: {exc}", file=sys.stderr)
            return 2
        unmeasured = check_live(entries, capture, out)
    if args.end_state:
        check_end_state(entries, out)
    for line in out:
        print(line)
    for e in entries:
        for w in e.get("waivers", []):
            print(f"WAIVED {e['id']} [{w.get('check')}]: {w.get('reason')} -> {w.get('fix')}")
    hard = [l for l in out if not l.startswith("LIVE scope")]
    print(f"entries={len(entries)} unverified={sum(1 for e in entries if not e.get('verified'))} "
          f"violations={len(out)}")
    if hard:
        return 1
    return 3 if unmeasured else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--registry")
    ap.add_argument("--live", help="host_timer_capture.py output to compare against")
    ap.add_argument("--end-state", action="store_true",
                    help="fail while any transition, planned entry or unverified entry remains")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
