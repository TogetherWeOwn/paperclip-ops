#!/usr/bin/env python3
"""beszel_adapter.py — Beszel-native (PocketBase) payloads -> normalized JSON.

Producer for push-beszel-hosts.sh, which consumes a normalized per-host
array but ships no way to build one from a real Beszel hub. This adapter
reads three PocketBase list responses from a directory (fetched by
fetch-beszel-normalized.sh, or fixture files in tests):

  systems.json        GET /api/collections/systems/records
  stats-latest.json   newest system_stats record per system
  stats-history.json  coarse (480m/120m) system_stats for the disk forecast

Each file may be a PocketBase list envelope ({"items": [...]}) or a bare
array. Output on stdout is the normalized array push-beszel-hosts.sh
documented, restricted to fields Beszel actually records.

FIELD PROVENANCE (verified against henrygd/beszel source, 2026-10-17):
  systems record: name, status ("up"|"down"|"paused"|"pending"), info{}
  info:  c=cores, t=threads, sv=[totalServices, numFailedServices],
         la=[load1, load5, load15]
  system_stats record: system=<systems id>, type, created, stats{}
  stats: cpu=cpu %, mp=mem %, dp=disk %, d=disk total GiB, du=disk used GiB,
         la=[1,5,15]  (GiB = bytes / 2**30, agent/utils BytesToGigabytes)
  Normalized mapping:
    cpu_used_percent  <- stats.cpu
    mem_used_percent  <- stats.mp
    disk_used_percent <- stats.dp
    load15_per_cpu    <- (stats.la[2] or info.la[2]) / (info.t or info.c)
    failed_units      <- info.sv[1]            (count only; names are not
                                                 in the systems record)
    disk_total_bytes  <- stats.d * 2**30
    disk_history[]    <- history stats.du * 2**30 at record created time
  NOT emitted, ever: inode_used_percent and container_restarts_24h.
  Beszel records neither (no inode field; container stats carry no
  restart count). push-beszel-hosts.sh scores those only when a payload
  from some other producer supplies them; this adapter must not invent
  them, or every threshold on them becomes fiction.

FAIL-CLOSED RULES (mirror the consumer):
  - unreadable/empty systems payload: exit 2, no output.
  - a host that is not status "up", has no stats record, has stats older
    than --max-age seconds, or lacks a numeric cpu/mp/dp/la/threads is
    emitted as {"name": ...} only, so the consumer pushes it false
    naming the missing fields. Stale or down hosts never look healthy.
  - failed_units is omitted when info.sv is absent (agent without
    systemd monitoring): the consumer then fails that host closed,
    which is the honest state — failed units are unmeasured, not zero.
  - a nameless system record cannot be emitted: warn on stderr; if no
    host survives, exit 2.

Usage: beszel_adapter.py [--max-age SECONDS] [--now EPOCH] PAYLOAD_DIR
Exit: 0 normalized array printed; 2 refusal before any health claim.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GIB = 2**30
HISTORY_CAP = 200


def die(msg):
    print(f"beszel_adapter: refusing — {msg}", file=sys.stderr)
    sys.exit(2)


def load_records(path: Path, required: bool):
    if not path.is_file():
        if required:
            die(f"missing {path.name}")
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        die(f"{path.name} is not readable JSON")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    die(f"{path.name} holds no record list (want [...] or {{\"items\": [...]}})")


def as_obj(value):
    """PocketBase json fields arrive as objects, but tolerate a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_time(value):
    """PocketBase created stamps: '2026-01-01 00:00:00.000Z' or ISO-8601."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace(" ", "T", 1) if value[10:11] == " " else value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def newest_per_system(records):
    best = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        sid, created = rec.get("system"), parse_time(rec.get("created"))
        if not isinstance(sid, str) or created is None:
            continue
        if sid not in best or created > best[sid][0]:
            best[sid] = (created, rec)
    return best


def build_host(system, latest, history_by_system, now, max_age):
    """Return (record, measured). Unmeasured hosts get {"name": ...} only."""
    name = system.get("name")
    record = {"name": name}
    if system.get("status") != "up" or latest is None:
        return record, False
    created, stats_rec = latest
    if now - created > max_age:
        return record, False
    info = as_obj(system.get("info"))
    stats = as_obj(stats_rec.get("stats"))
    cpu, mem, disk = num(stats.get("cpu")), num(stats.get("mp")), num(stats.get("dp"))
    la = stats.get("la") if isinstance(stats.get("la"), list) else info.get("la")
    load15 = num(la[2]) if isinstance(la, list) and len(la) >= 3 else None
    divisor = num(info.get("t")) or num(info.get("c"))
    if cpu is None or mem is None or disk is None or load15 is None or not divisor or divisor <= 0:
        return record, False
    record.update(
        cpu_used_percent=cpu,
        mem_used_percent=mem,
        disk_used_percent=disk,
        load15_per_cpu=load15 / divisor,
    )
    sv = info.get("sv")
    if isinstance(sv, list) and len(sv) >= 2 and num(sv[1]) is not None:
        record["failed_units"] = int(sv[1])
    total_gib = num(stats.get("d"))
    if total_gib is not None and total_gib > 0:
        record["disk_total_bytes"] = round(total_gib * GIB)
        points = []
        for created_h, rec_h in sorted(history_by_system.get(system.get("id"), [])):
            used = num(as_obj(rec_h.get("stats")).get("du"))
            if used is not None:
                points.append({"t": int(created_h), "used_bytes": round(used * GIB)})
        if len(points) >= 2:
            record["disk_history"] = points[-HISTORY_CAP:]
    return record, True


def main():
    ap = argparse.ArgumentParser(description="Beszel native -> normalized adapter")
    ap.add_argument("payload_dir")
    ap.add_argument("--max-age", type=float, default=600.0,
                    help="newest-stats age in seconds before a host is unmeasured")
    ap.add_argument("--now", type=float, default=None, help="epoch override (tests)")
    args = ap.parse_args()
    if args.max_age <= 0:
        die("--max-age must be positive")
    base = Path(args.payload_dir)
    systems = load_records(base / "systems.json", required=True)
    if not systems:
        die("systems payload is empty — proves nothing")
    latest = newest_per_system(load_records(base / "stats-latest.json", required=True))
    history = {}
    for rec in load_records(base / "stats-history.json", required=False):
        if isinstance(rec, dict) and isinstance(rec.get("system"), str):
            created = parse_time(rec.get("created"))
            if created is not None:
                history.setdefault(rec["system"], []).append((created, rec))
    now = args.now if args.now is not None else time.time()
    out, nameless = [], 0
    for system in systems:
        if not isinstance(system, dict) or not isinstance(system.get("name"), str) or not system["name"]:
            nameless += 1
            continue
        record, _measured = build_host(system, latest.get(system.get("id")), history, now, args.max_age)
        out.append(record)
    if nameless:
        print(f"beszel_adapter: skipped {nameless} nameless system record(s)", file=sys.stderr)
    if not out:
        die("no named host in systems payload")
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
