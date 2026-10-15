#!/usr/bin/env python3
"""Read-only host timer inventory (case_30b51a0f). Operator-run; never run by an agent.

Prints one JSON document describing every systemd timer in the user and system
scopes, the service each one triggers, and any cron entries. It is the input to
`host_timer_registry_check.py --live`.

What it does NOT do:
  * start, stop, enable, disable, reload or mask anything: only `list-units`,
    `list-unit-files` and `show` are ever invoked, from the fixed allow-list below;
  * open or print an EnvironmentFile, a credential, a unit file body or the
    `Environment=` property: `EnvironmentFiles` is recorded as a path only, and
    `ExecStart` is reduced to its executable path (argv can carry a secret);
  * use a shell. Every command is an argv list with no interpolation.

A scope that cannot be read (no user bus, systemctl missing, command failed) is
recorded as `available: false` with the reason. The checker treats that as "could
not measure", never as clean.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TIMER_PROPS = ("Id", "ActiveState", "SubState", "UnitFileState", "Unit", "FragmentPath",
               "TimersCalendar", "TimersMonotonic", "NextElapseUSecRealtime",
               "LastTriggerUSec", "Persistent", "RandomizedDelayUSec")
SERVICE_PROPS = ("Id", "Type", "ActiveState", "Result", "ExecMainStatus", "ExecMainStartTimestamp",
                 "ExecMainExitTimestamp", "TimeoutStartUSec", "Restart", "User", "WorkingDirectory",
                 "EnvironmentFiles", "ExecStart", "FragmentPath", "NRestarts")
ALLOWED_VERBS = {"list-units", "list-unit-files", "show"}
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.timer$")
EXEC_PATH_RE = re.compile(r"path=([^ ;]+)")
ENVFILE_RE = re.compile(r"^(-?)(/[^ ]*|%[a-z]/[^ ]*)")
CRON_LINE_RE = re.compile(r"^\s*(@\w+|(?:\S+\s+){5})(.*)$")


def sysctl(scope, *argv):
    if argv[0] not in ALLOWED_VERBS:
        raise ValueError(f"verb {argv[0]!r} is not on the read-only allow-list")
    cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + list(argv)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} exited {res.returncode}: {res.stderr.strip()[:200]}")
    return res.stdout


def show(scope, unit, props):
    out = sysctl(scope, "show", unit, "--no-pager", "-p", ",".join(props))
    data = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            data[key] = value
    return data


def reduce_exec(raw):
    m = EXEC_PATH_RE.search(raw or "")
    return m.group(1) if m else None


def reduce_envfiles(raw):
    paths = []
    for token in (raw or "").split():
        m = ENVFILE_RE.match(token)
        if m:
            paths.append(token.split("(")[0])
    return paths


def fragment_sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def timer_record(scope, unit):
    t = show(scope, unit, TIMER_PROPS)
    rec = {"unit": unit, "activeState": t.get("ActiveState"), "subState": t.get("SubState"),
           "unitFileState": t.get("UnitFileState"), "triggers": t.get("Unit"),
           "fragmentPath": t.get("FragmentPath"), "fragmentSha256": fragment_sha(t.get("FragmentPath")),
           "calendar": t.get("TimersCalendar"), "monotonic": t.get("TimersMonotonic"),
           "nextElapse": t.get("NextElapseUSecRealtime"), "lastTrigger": t.get("LastTriggerUSec"),
           "persistent": t.get("Persistent"), "randomizedDelay": t.get("RandomizedDelayUSec")}
    svc_name = t.get("Unit")
    if svc_name:
        try:
            s = show(scope, svc_name, SERVICE_PROPS)
            rec["service"] = {
                "unit": svc_name, "type": s.get("Type"), "activeState": s.get("ActiveState"),
                "result": s.get("Result"), "execMainStatus": s.get("ExecMainStatus"),
                "lastStart": s.get("ExecMainStartTimestamp"), "lastExit": s.get("ExecMainExitTimestamp"),
                "timeoutStart": s.get("TimeoutStartUSec"), "restart": s.get("Restart"),
                "nRestarts": s.get("NRestarts"), "user": s.get("User") or None,
                "workingDirectory": s.get("WorkingDirectory") or None,
                "environmentFiles": reduce_envfiles(s.get("EnvironmentFiles")),
                "execPath": reduce_exec(s.get("ExecStart")),
                "fragmentPath": s.get("FragmentPath"),
                "fragmentSha256": fragment_sha(s.get("FragmentPath")),
            }
        except (RuntimeError, subprocess.SubprocessError) as exc:
            rec["service"] = {"unit": svc_name, "error": str(exc)[:200]}
    return rec


def capture_scope(scope):
    try:
        names = set()
        for verb, extra in (("list-units", ("--all",)), ("list-unit-files", ())):
            out = sysctl(scope, verb, "--type=timer", "--no-legend", "--plain", "--no-pager", *extra)
            for line in out.splitlines():
                parts = line.split()
                if parts and UNIT_RE.match(parts[0]):
                    names.add(parts[0])
        return {"available": True, "timers": [timer_record(scope, n) for n in sorted(names)]}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": str(exc)[:240], "timers": []}


def capture_cron(user_crontab_text, etc_root):
    entries = []
    for line in (user_crontab_text or "").splitlines():
        if line.lstrip().startswith("#") or not line.strip() or re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=", line):
            continue
        m = CRON_LINE_RE.match(line)
        if m:
            cmd = m.group(2).strip().split()
            entries.append({"schedule": m.group(1).strip(), "program": os.path.basename(cmd[0]) if cmd else ""})
    etc = {}
    for sub in ("cron.d", "cron.hourly", "cron.daily", "cron.weekly", "cron.monthly"):
        d = Path(etc_root) / sub
        etc[sub] = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    return {"userCrontab": entries, "etc": etc}


def read_user_crontab():
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else ""


def linger():
    try:
        user = os.environ.get("USER") or os.getlogin()
        res = subprocess.run(["loginctl", "show-user", user, "--property=Linger", "--value"],
                             capture_output=True, text=True, timeout=10)
        return res.stdout.strip() if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scope", action="append", choices=("user", "system"),
                    help="repeat to capture both (default: both)")
    ap.add_argument("--etc-root", default="/etc")
    args = ap.parse_args(argv)
    scopes = args.scope or ["user", "system"]
    doc = {"schemaVersion": 1,
           "capturedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "host": os.uname().nodename,
           "scopes": {s: capture_scope(s) for s in scopes},
           "cron": capture_cron(read_user_crontab(), args.etc_root),
           "linger": linger()}
    json.dump(doc, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
