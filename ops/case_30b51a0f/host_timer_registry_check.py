#!/usr/bin/env python3
"""Offline gate for the host timer registry (case_30b51a0f, workstream 7 of case_dc8671fe).

The registry (host-timer-registry.json) lists every recurring clock that can act
on this installation. This script enforces what "consolidated" means, with no
network, no credentials and no clock read:

  I1  one primary per purpose        (redundancy and supersession must be declared)
  I2  one primary writer per target  (native upstream machinery is exempt: we cannot merge it)
  I3  every systemd timer is bounded (finite positive TimeoutStartSec or timeout
      wrapper; Restart absent/no; 0/infinity/empty/malformed/unknown suffix fail)
  I4  the registry matches the files  (schedules of repo-sourced timers, plugin jobs, workflows)
  I5  nothing recurring is unregistered (every *.timer, plugin job schedule and workflow cron in
                                         the repo must belong to an entry; .yml and .yaml share one contract)
  I6  a transition is well formed     (supersededBy resolves, same purpose, retireGate present;
                                         retiredWith names the retire-pending entry it leaves with)

With --live <capture.json> (output of host_timer_capture.py) it also compares the
registry to what the host actually runs, retaining (scope, unit) identity: a timer
in one scope never satisfies an expected timer in the other. With --end-state it
fails while any transition (supersededBy, redundancy-pending, planned, unverified)
remains, which is the acceptance test for "consolidation finished".

With --json it emits a versioned envelope (schemaVersion 1) with stable
invariant/entry/scope/unit IDs, coverage, violation/unknown counts, kebab-case
error codes, per-finding retryable plus next_action, and a top-level next_action.
With --dry-run it runs the same offline read-only audit (identical exit/output);
it never captures from the host and never applies changes.

Exit: 0 clean | 1 violations | 2 usage or unreadable input | 3 could not measure
(a live capture scope was unavailable: that is not a clean result).
"""

import argparse
import configparser
import json
import math
import re
import shlex
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
JSON_SCHEMA_VERSION = 1

# systemd.time(7) suffixes in seconds; "" is bare seconds. "M" (month) is
# case-sensitive; everything else matches case-insensitively.
_DURATION_MULTIPLIERS = {
    "": 1.0, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "ms": 0.001, "msec": 0.001, "msecs": 0.001,
    "us": 0.000001, "usec": 0.000001, "usecs": 0.000001,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    "w": 604800.0, "week": 604800.0, "weeks": 604800.0,
    "month": 2592000.0, "months": 2592000.0,
    "y": 31536000.0, "year": 31536000.0, "years": 31536000.0,
}
_DURATION_COMPONENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-zµ]*)\s*")
_TIMEOUT_OPTS_WITH_VALUE = {"-s", "--signal", "-k", "--kill-after"}


def parse_finite_duration(value):
    """Return (finite_positive, reason). Unknown/ambiguous is never finite."""
    if value is None:
        return False, "missing"
    text = value.strip() if isinstance(value, str) else str(value).strip()
    if not text:
        return False, "empty"
    if text.lower() in ("infinity", "inf"):
        return False, "infinity"
    pos = 0
    total = 0.0
    seen = False
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        match = _DURATION_COMPONENT_RE.match(text, pos)
        if not match:
            return False, "malformed"
        number, suffix = match.group(1), match.group(2)
        if suffix == "M":
            mult = _DURATION_MULTIPLIERS["month"]
        else:
            mult = _DURATION_MULTIPLIERS.get(suffix.lower())
        if mult is None:
            return False, f"unknown suffix {suffix!r}"
        try:
            total += float(number) * mult
        except ValueError:
            return False, "malformed"
        seen = True
        pos = match.end()
    if not seen:
        return False, "malformed"
    if not math.isfinite(total):
        return False, "infinity"
    if total <= 0:
        return False, "zero"
    return True, "finite"


# Wrappers that may precede the timeout command itself (flock needs its lock
# operand skipped too). Anything else before `timeout` means timeout is an
# argument to another program, which bounds nothing.
_KNOWN_TIMEOUT_WRAPPERS = ("flock", "env", "nice")
_FLOCK_VALUE_OPTS = {"-w", "--timeout", "-E", "--conflict-code"}
_ENV_VALUE_OPTS = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string",
                   "-a", "--argv0"}


def _exec_basename(token):
    """Basename of an executable token, ignoring systemd execute prefixes."""
    return token.lstrip("-@: +!").rsplit("/", 1)[-1]


def _flock_command_index(tokens, pos):
    """Index of the command flock wraps, or None. Unknown options fail closed."""
    i = pos
    end_opts = False
    while i < len(tokens):
        tok = tokens[i]
        if not end_opts and tok == "--":
            end_opts = True
            i += 1
            continue
        if not end_opts and tok.startswith("-") and tok != "-":
            if tok in _FLOCK_VALUE_OPTS:
                i += 2
                continue
            if tok.startswith(("--timeout=", "--conflict-code=")):
                i += 1
                continue
            if len(tok) > 2 and tok[1] in "wE":
                i += 1  # attached value (-w5, -E2)
                continue
            if tok[1].isalpha() or tok[1] == "-":
                i += 1  # flag cluster (-n, -nx, --verbose)
                continue
            return None
        break
    if end_opts and i >= len(tokens):
        return None
    i += 1  # the lock file/dir operand
    return i if 0 <= i < len(tokens) else None


def _env_command_index(tokens, pos):
    """Index of the command env runs, or None. Unknown options fail closed."""
    i = pos
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok in ("-i", "--ignore-environment", "-0", "--null", "-"):
            i += 1
            continue
        if tok in _ENV_VALUE_OPTS:
            i += 2
            continue
        if tok.startswith(("--unset=", "--chdir=", "--split-string=", "--argv0=")):
            i += 1
            continue
        if "=" in tok and not tok.startswith("-"):
            i += 1  # VAR=VAL assignment
            continue
        break
    return i if 0 <= i < len(tokens) else None


def _nice_command_index(tokens, pos):
    """Index of the command nice runs, or None. Unknown options fail closed."""
    i = pos
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if re.match(r"^-\d+$", tok):
            i += 1  # legacy -N adjustment
            continue
        if tok in ("-n", "--adjustment"):
            i += 2
            continue
        if tok.startswith("--adjustment=") or (tok.startswith("-n") and len(tok) > 2):
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            return None
        break
    return i if 0 <= i < len(tokens) else None


_WRAPPER_COMMAND_INDEX = {
    "flock": _flock_command_index,
    "env": _env_command_index,
    "nice": _nice_command_index,
}


def _timeout_command_index(tokens):
    """Token index of a bounding `timeout`, or None.

    Only the executable itself (first token, modulo systemd -/@/+ prefixes)
    or the command run by a known leading wrapper counts: `timeout` anywhere
    else is an argument to another program and proves no bound.
    """
    first = _exec_basename(tokens[0])
    if first == "timeout":
        return 0
    if first in _WRAPPER_COMMAND_INDEX:
        cmd = _WRAPPER_COMMAND_INDEX[first](tokens, 1)
        if cmd is not None and _exec_basename(tokens[cmd]) == "timeout":
            return cmd
    return None


def timeout_wrapper_duration(exec_start):
    """Return (found, duration_text_or_None). Finds `timeout [opts] DURATION`."""
    if not exec_start or "timeout" not in exec_start:
        return False, None
    try:
        tokens = shlex.split(exec_start, posix=True)
    except ValueError:
        tokens = exec_start.split()
    if not tokens:
        return False, None
    if tokens[0].lstrip("-:+!").startswith("@") and len(tokens) > 1:
        tokens = [tokens[0]] + tokens[2:]  # systemd @: tokens[1] is argv[0]
    index = _timeout_command_index(tokens)
    if index is None:
        return False, None
    rest = tokens[index + 1:]
    pos = 0
    duration = None
    while pos < len(rest):
        tok = rest[pos]
        if tok == "--":
            pos += 1
            if pos < len(rest):
                duration = rest[pos]
            break
        if tok in _TIMEOUT_OPTS_WITH_VALUE:
            pos += 2
            continue
        if tok.startswith("--signal=") or tok.startswith("--kill-after="):
            pos += 1
            continue
        if tok.startswith("-") and not re.match(r"^-\d", tok):
            pos += 1
            continue
        duration = tok
        break
    return True, duration


def service_bound_detail(svc, exec_start, command_count=1):
    """Return (bounded, detail). Waivers are handled by the caller."""
    has_key = svc.has_option("Service", "TimeoutStartSec")
    timeout_value = svc.get("Service", "TimeoutStartSec", fallback=None) if has_key else None
    finite_timeout, timeout_reason = parse_finite_duration(timeout_value) if has_key else (False, "missing")
    if finite_timeout:
        return True, "bounded"
    if command_count > 1:
        if has_key:
            tail = f"TimeoutStartSec={timeout_value!r} is {timeout_reason}"
        else:
            tail = "no TimeoutStartSec"
        return False, (f"runs {command_count} start commands with {tail}: "
                       "a timeout wrapper on one command cannot bound the others")
    found_wrapper, duration_text = timeout_wrapper_duration(exec_start)
    finite_wrapper, wrapper_reason = parse_finite_duration(duration_text) if found_wrapper else (False, "missing")
    if finite_wrapper:
        return True, "bounded"
    if not has_key and not found_wrapper:
        return False, "has no TimeoutStartSec and no timeout wrapper"
    parts = []
    if has_key:
        parts.append(f"TimeoutStartSec={timeout_value!r} is {timeout_reason}, not a finite positive bound")
    if found_wrapper:
        parts.append(f"timeout wrapper duration {duration_text!r} is {wrapper_reason}, not a finite positive bound")
    return False, "; ".join(parts) if parts else "has no finite positive runtime bound"


class Findings:
    """Paired text lines plus structured JSON results (deterministic on demand)."""

    def __init__(self):
        self.out = []
        self.results = []

    def add(self, invariant, entry, scope, unit, severity, code, message,
            retryable, next_action):
        self.out.append(message)
        self.results.append({
            "invariant": invariant, "entry": entry, "scope": scope, "unit": unit,
            "severity": severity, "code": code, "message": message,
            "retryable": bool(retryable), "next_action": next_action,
        })

    def sorted_results(self):
        def key(r):
            return (r["invariant"] or "", r["entry"] or "", r["scope"] or "",
                    r["unit"] or "", r["code"] or "", r["message"] or "")
        return sorted(self.results, key=key)


def load_registry(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise ValueError("registry: top-level object with schemaVersion 1 and entries list is required")
    if doc.get("schemaVersion") != 1 or not isinstance(doc.get("entries"), list):
        raise ValueError("registry: schemaVersion 1 with an entries list is required")
    for entry in doc["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("registry: every entry must be an object")
    return doc["entries"]


def validate_capture(capture):
    if not isinstance(capture, dict):
        raise ValueError("capture: top-level object with a scopes map is required")
    scopes = capture.get("scopes")
    if not isinstance(scopes, dict):
        raise ValueError("capture: scopes must map user/system to availability plus timers")
    for scope in ("user", "system"):
        if scope not in scopes:
            continue
        entry = scopes[scope]
        if not isinstance(entry, dict):
            raise ValueError(f"capture: scopes.{scope} must be an object")
        timers = entry.get("timers", [])
        if not isinstance(timers, list):
            raise ValueError(f"capture: scopes.{scope}.timers must be a list")
        if "available" in entry and not isinstance(entry["available"], bool):
            raise ValueError(f"capture: scopes.{scope}.available must be a boolean")
        if entry.get("available") is True:
            for row in timers:
                if not isinstance(row, dict):
                    raise ValueError(f"capture: scopes.{scope}.timers rows must be objects")
                unit = row.get("unit")
                if not isinstance(unit, str) or not unit.strip():
                    raise ValueError(f"capture: scopes.{scope}.timers rows need a unit name")
    return capture


def parse_unit(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    return cp


_EXEC_DIRECTIVES = ("ExecStartPre", "ExecStart", "ExecStartPost")


def service_start_commands(svc_path):
    """Every [Service] start command in run order.

    configparser keeps only the last duplicate option, but systemd runs every
    ExecStart line of a oneshot service plus its ExecStartPre/Post lines, so
    the bound proof must see all of them. An empty assignment resets that
    directive's list, matching systemd semantics.
    """
    try:
        text = Path(svc_path).read_text(encoding="utf-8")
    except OSError:
        return []
    found = {directive: [] for directive in _EXEC_DIRECTIVES}
    in_service = False
    pending = None  # (directive, value) while joining a backslash continuation
    for raw in text.splitlines():
        stripped = raw.strip()
        if pending is not None:
            directive, value = pending
            if stripped.endswith("\\"):
                pending = (directive, (value + " " + stripped[:-1]).strip())
                continue
            value = (value + " " + stripped).strip()
            pending = None
            if value:
                found[directive].append(value)
            else:
                found[directive] = []
            continue
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("["):
            in_service = stripped == "[Service]"
            continue
        if not in_service or "=" not in stripped:
            continue
        directive, _, rhs = stripped.partition("=")
        directive = directive.strip()
        if directive not in found:
            continue
        rhs = rhs.strip()
        if rhs.endswith("\\"):
            pending = (directive, rhs[:-1].strip())
            continue
        if rhs:
            found[directive].append(rhs)
        else:
            found[directive] = []
    if pending is not None:
        directive, value = pending
        if value:
            found[directive].append(value)
    return [cmd for directive in _EXEC_DIRECTIVES for cmd in found[directive]]


def _member(value, allowed):
    """Set membership that never raises on unhashable raw JSON values."""
    return isinstance(value, str) and value in allowed


def is_primary(entry):
    return (_member(entry.get("state"), PRIMARY_STATES) and not entry.get("supersededBy")
            and not entry.get("redundancyOf") and not entry.get("retiredWith"))


def waived(entry, check):
    waivers = entry.get("waivers", [])
    return isinstance(waivers, list) and any(
        isinstance(w, dict) and w.get("check") == check for w in waivers)


def _schema_add(findings, eid, scope, message):
    findings.add("SCHEMA", eid if isinstance(eid, str) else None,
                 scope if isinstance(scope, str) else None, None,
                 "violation", "schema-violation", message, False,
                 "Fix the registry entry fields, then re-run the audit.")


def check_schema(entries, findings):
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue  # malformed-registry is raised by load_registry before this runs
        eid = entry.get("id", "<no id>")
        scope = entry.get("scope")
        missing = [k for k in REQUIRED if k not in entry]
        if missing:
            _schema_add(findings, eid if isinstance(eid, str) else "<no id>", scope,
                        f"{eid}: missing field(s) {missing}")
            continue
        if not isinstance(eid, str):
            _schema_add(findings, None, scope, f"<no id>: id must be a string")
            continue
        if eid in seen:
            _schema_add(findings, eid, scope, f"{eid}: duplicate id")
        seen.add(eid)
        if not _member(entry["kind"], KINDS):
            _schema_add(findings, eid, scope,
                        f"{eid}: kind {entry['kind']!r} not in {sorted(KINDS)}")
        if not _member(entry["role"], ROLES):
            _schema_add(findings, eid, scope,
                        f"{eid}: role {entry['role']!r} not in {sorted(ROLES)}")
        if not _member(entry["state"], STATES):
            _schema_add(findings, eid, scope,
                        f"{eid}: state {entry['state']!r} not in {sorted(STATES)}")
        if not isinstance(scope, str):
            _schema_add(findings, eid, scope, f"{eid}: scope must be a string")
        elif entry["kind"] == "systemd-timer" and scope not in ("user", "system"):
            _schema_add(findings, eid, scope,
                        f"{eid}: systemd-timer scope {scope!r} must be 'user' or 'system'")
        if not isinstance(entry.get("purpose"), str):
            _schema_add(findings, eid, scope, f"{eid}: purpose must be a string")
        for key in ("supersededBy", "redundancyOf", "retiredWith"):
            ref = entry.get(key)
            if ref and not isinstance(ref, str):
                _schema_add(findings, eid, scope,
                            f"{eid}: {key} must be an entry id string")
        source = entry.get("source")
        schedule = entry.get("schedule")
        writes = entry.get("writes")
        if not isinstance(source, dict) or not isinstance(schedule, dict) \
                or not isinstance(writes, list) or not isinstance(entry.get("reports"), list):
            _schema_add(findings, eid, scope,
                        f"{eid}: source/schedule must be objects and writes/reports lists")
            continue
        if any(not isinstance(target, str) for target in writes):
            _schema_add(findings, eid, scope,
                        f"{eid}: every writes item must be a string")
        if entry["kind"] == "systemd-timer" and "timer" in source \
                and not isinstance(source["timer"], str):
            _schema_add(findings, eid, scope,
                        f"{eid}: source.timer must be a string")
        if entry["role"] == "detector" and writes:
            _schema_add(findings, eid, scope,
                        f"{eid}: a detector must not write ({writes}); declare it a writer")
        if entry["role"] == "writer" and _member(entry["state"], PRIMARY_STATES) \
                and entry.get("bound") in ("", "unknown") \
                and source.get("type") not in ("host-only", "planned", "native"):
            _schema_add(findings, eid, scope,
                        f"{eid}: a writer must declare how its actions are bounded")
        waivers = entry.get("waivers", [])
        if "waivers" in entry and not isinstance(waivers, list):
            _schema_add(findings, eid, scope, f"{eid}: waivers must be a list")
            continue
        for waiver in waivers:
            if not isinstance(waiver, dict) or not all(waiver.get(k) for k in ("check", "reason", "fix")):
                _schema_add(findings, eid, scope,
                            f"{eid}: a waiver needs check, reason and fix")


def check_purposes(entries, findings):
    by_purpose = {}
    for entry in entries:
        if not isinstance(entry, dict) or not is_primary(entry):
            continue
        by_purpose.setdefault(entry.get("purpose"), []).append(entry.get("id"))
    for purpose, ids in sorted(by_purpose.items(), key=lambda kv: str(kv[0])):
        if len(ids) > 1:
            message = (f"I1 purpose {purpose!r} has {len(ids)} primaries {ids}: "
                       "keep one and mark the others supersededBy or redundancyOf")
            findings.add("I1", None, None, purpose if isinstance(purpose, str) else None,
                         "violation", "duplicate-purpose", message, False,
                         "Keep one primary for this purpose, mark the others "
                         "supersededBy/redundancyOf, then re-run the audit.")


def check_writers(entries, findings):
    owner_of = {}
    for entry in entries:
        if not isinstance(entry, dict) or not is_primary(entry):
            continue
        source = entry.get("source", {})
        if not isinstance(source, dict):
            continue
        if source.get("type") == "native" or entry.get("kind") == "native":
            continue
        writes = entry.get("writes", [])
        if not isinstance(writes, list):
            continue
        for target in writes:
            if target in owner_of and owner_of[target] != entry.get("id"):
                message = (f"I2 target {target!r} has two primary writers: "
                           f"{owner_of[target]} and {entry.get('id')}")
                findings.add("I2", None, None, target if isinstance(target, str) else None,
                             "violation", "duplicate-writer", message, False,
                             "Keep one primary writer per target, then re-run the audit.")
            owner_of.setdefault(target, entry.get("id"))


def check_transitions(entries, findings):
    ids = {entry["id"]: entry for entry in entries
           if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        eid = entry["id"]
        scope = entry.get("scope") if isinstance(entry.get("scope"), str) else None

        def transition(message):
            findings.add("I6", eid, scope, None, "violation",
                         "invalid-transition", message, False,
                         "Fix the transition references, then re-run the audit.")

        for key in ("supersededBy", "redundancyOf"):
            ref = entry.get(key)
            if not ref:
                continue
            tgt = ids.get(ref)
            if tgt is None:
                transition(f"I6 {eid}: {key} {ref!r} does not exist")
                continue
            if tgt.get("purpose") != entry.get("purpose"):
                transition(f"I6 {eid}: {key} {ref!r} serves purpose {tgt.get('purpose')!r}, "
                           f"not {entry.get('purpose')!r}")
            if tgt.get("state") in ("retired", "retire-pending"):
                transition(f"I6 {eid}: {key} points at {ref!r}, which is itself {tgt.get('state')}")
        dep = entry.get("retiredWith")
        if dep and (dep not in ids or ids[dep].get("state") != "retire-pending"):
            transition(f"I6 {eid}: retiredWith {dep!r} must name a retire-pending entry")
        if entry.get("supersededBy") and not entry.get("retireGate"):
            transition(f"I6 {eid}: supersededBy needs a retireGate (the evidence that lets it go)")
        if entry.get("redundancyOf") and not entry.get("redundancyReason"):
            transition(f"I6 {eid}: redundancyOf needs a redundancyReason (a different failure domain)")
        if entry.get("state") == "retire-pending" and not (entry.get("supersededBy") or entry.get("retiredWith")):
            transition(f"I6 {eid}: retire-pending without supersededBy or retiredWith")


def sd_schedule(timer_cfg):
    keys = ("OnCalendar", "OnBootSec", "OnUnitActiveSec", "OnActiveSec", "OnStartupSec",
            "OnUnitInactiveSec")
    return {k: timer_cfg["Timer"][k] for k in keys if timer_cfg.has_option("Timer", k)}


def check_repo_timer(entry, root, findings):
    source = entry.get("source", {})
    timer_rel = source.get("timer") if isinstance(source, dict) else None
    eid = entry.get("id")
    scope = entry.get("scope") if isinstance(entry.get("scope"), str) else None
    if not isinstance(timer_rel, str):
        return
    timer_path = root / timer_rel
    if not timer_path.is_file():
        findings.add("I4", eid, scope, timer_rel, "violation", "schedule-drift",
                     f"I4 {eid}: timer file {timer_rel} not found", False,
                     "Restore the timer file or fix the registry citation, then re-run the audit.")
        return
    timer = parse_unit(timer_path)
    if sd_schedule(timer) != entry.get("schedule"):
        findings.add("I4", eid, scope, timer_rel, "violation", "schedule-drift",
                     f"I4 {eid}: schedule {entry.get('schedule')} != {sd_schedule(timer)} in {timer_rel}",
                     False, "Align the registry schedule with the timer file, then re-run the audit.")
    unit = timer.get("Timer", "Unit", fallback=None)
    if not unit:
        findings.add("I4", eid, scope, timer_rel, "violation", "schedule-drift",
                     f"I4 {eid}: {timer_rel} has no Unit=", False,
                     "Add Unit= to the timer file, then re-run the audit.")
        return
    svc_path = timer_path.parent / unit
    if not svc_path.is_file():
        findings.add("I4", eid, scope, unit, "violation", "schedule-drift",
                     f"I4 {eid}: service {unit} not found next to {timer_rel}", False,
                     "Restore the service file next to the timer, then re-run the audit.")
        return
    svc = parse_unit(svc_path)
    if svc.get("Service", "Type", fallback="simple") != "oneshot":
        findings.add("I3", eid, scope, unit, "violation", "unbounded-service",
                     f"I3 {eid}: {unit} is not Type=oneshot", False,
                     "Make the timer-driven service Type=oneshot, then re-run the audit.")
    restart = svc.get("Service", "Restart", fallback="no")
    if restart in BAD_RESTART:
        findings.add("I3", eid, scope, unit, "violation", "unbounded-service",
                     f"I3 {eid}: {unit} has Restart={restart}; a timer-driven oneshot must not self-retry",
                     False, "Remove the self-retry Restart= policy, then re-run the audit.")
    commands = service_start_commands(svc_path)
    exec_start = " ".join(commands)
    bounded, detail = service_bound_detail(svc, exec_start, command_count=len(commands))
    if not bounded and not waived(entry, "bounded-runtime"):
        findings.add("I3", eid, scope, unit, "violation", "unbounded-service",
                     f"I3 {eid}: {unit} {detail}", False,
                     "Add a finite positive TimeoutStartSec (for example 5min) or a "
                     "positive timeout wrapper, then re-run the audit.")


def read_schedules(path, root):
    text = (root / path).read_text(encoding="utf-8")
    return SCHEDULE_RE.findall(text)


def check_plugin_jobs(entries, findings, root):
    declared = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "plugin-job":
            continue
        source = entry.get("source", {})
        schedule = entry.get("schedule", {})
        if not isinstance(source, dict) or not isinstance(schedule, dict):
            continue
        if source.get("type") == "repo" and isinstance(source.get("manifest"), str):
            declared.setdefault(source["manifest"], []).extend(schedule.get("jobs", []) or [])
    manifests = sorted({str(p.relative_to(root)) for pat in ("plugins/*/src/manifest.ts",
                                                              "plugins/*/dist/manifest.js")
                        for p in root.glob(pat)})
    for manifest in manifests:
        src = manifest.replace("/dist/manifest.js", "/src/manifest.ts")
        # A plugin with both a src and a dist manifest is registered against src.
        if manifest.endswith("dist/manifest.js") and (root / src).is_file():
            continue
        live = sorted(read_schedules(manifest, root))
        if manifest not in declared:
            if live:
                findings.add("I5", None, None, manifest, "violation",
                             "unregistered-source",
                             f"I5 {manifest} declares {len(live)} job schedule(s) but no registry entry cites it",
                             False, "Register the plugin manifest schedules, then re-run the audit.")
            continue
        if sorted(declared[manifest]) != live:
            findings.add("I4", None, None, manifest, "violation", "schedule-drift",
                         f"I4 {manifest}: registry jobs {sorted(declared[manifest])} != manifest {live}",
                         False, "Align the registry plugin jobs with the manifest, then re-run the audit.")


def check_workflows(entries, findings, root):
    declared = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "gh-actions-schedule":
            continue
        source = entry.get("source", {})
        schedule = entry.get("schedule", {})
        if not isinstance(source, dict) or not isinstance(schedule, dict):
            continue
        workflow = source.get("workflow")
        if isinstance(workflow, str):
            declared[workflow] = schedule.get("cron")
    wf_dir = root / ".github" / "workflows"
    files = []
    if wf_dir.is_dir():
        seen = set()
        for pattern in ("*.yml", "*.yaml"):
            for path in wf_dir.glob(pattern):
                rel = str(path.relative_to(root))
                if rel not in seen:
                    seen.add(rel)
                    files.append(path)
        files.sort(key=lambda p: str(p.relative_to(root)))
    for workflow_path in files:
        rel = str(workflow_path.relative_to(root))
        crons = CRON_RE.findall(workflow_path.read_text(encoding="utf-8"))
        if crons and rel not in declared:
            findings.add("I5", None, None, rel, "violation", "unregistered-source",
                         f"I5 {rel} has a cron schedule {crons} but no registry entry",
                         False, "Register the scheduled workflow, then re-run the audit.")
        elif rel in declared and crons != [declared[rel]]:
            findings.add("I4", None, None, rel, "violation", "schedule-drift",
                         f"I4 {rel}: registry cron {declared[rel]!r} != workflow {crons}",
                         False, "Align the registry cron with the workflow file, then re-run the audit.")


def tracked_timers(root):
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "*.timer"],
                             capture_output=True, text=True, check=True)
        return sorted(res.stdout.split())
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*.timer"))


def check_unregistered_timers(entries, findings, root):
    cited = {entry["source"].get("timer") for entry in entries
             if isinstance(entry, dict) and entry.get("kind") == "systemd-timer"
             and isinstance(entry.get("source"), dict)}
    for rel in tracked_timers(root):
        if rel not in cited:
            findings.add("I5", None, None, rel, "violation", "unregistered-source",
                         f"I5 {rel} is not in the registry: classify it (purpose, role, writes) before it ships",
                         False, "Register the timer file, then re-run the audit.")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "systemd-timer":
            continue
        source = entry.get("source", {})
        if isinstance(source, dict) and source.get("type") == "repo":
            check_repo_timer(entry, root, findings)


def check_end_state(entries, findings):
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        eid = entry["id"]
        scope = entry.get("scope") if isinstance(entry.get("scope"), str) else None
        if entry.get("supersededBy") or entry.get("retiredWith") \
                or entry.get("state") in ("retire-pending", "planned"):
            findings.add("END", eid, scope, None, "violation", "end-state-blocked",
                         f"END {eid}: still a transition ({entry.get('state')})", False,
                         "Finish the declared transition, then re-run with --end-state.")
        elif entry.get("state") != "retired" and not entry.get("verified"):
            findings.add("END", eid, scope, None, "violation", "end-state-blocked",
                         f"END {eid}: not verified against a live capture", False,
                         "Verify the entry against a live capture, then re-run with --end-state.")
        waivers = entry.get("waivers")
        if entry.get("redundancyOf") is None and waivers:
            checks = ([w.get("check") if isinstance(w, dict) else w for w in waivers]
                      if isinstance(waivers, list) else waivers)
            findings.add("END", eid, scope, None, "violation", "end-state-blocked",
                         f"END {eid}: still carries waivers {checks}", False,
                         "Remove the waiver by fixing the underlying check, then re-run with --end-state.")


def unit_names(entry):
    names = set()
    if isinstance(entry.get("unit"), str) and entry["unit"].strip():
        names.add(entry["unit"])
    source = entry.get("source", {})
    if isinstance(source, dict):
        timer = source.get("timer")
        if isinstance(timer, str) and timer.strip():
            names.add(Path(timer).name)
        evidence = source.get("evidence", "")
        if isinstance(evidence, str):
            names.update(re.findall(r"([A-Za-z0-9_.@-]+\.timer)", evidence))
    return names


def check_live(entries, capture, findings):
    """Compare the registry to a host capture. Returns True when a scope could not be measured."""
    unmeasured = False
    measured = []
    live = {}
    scopes = capture.get("scopes", {}) if isinstance(capture, dict) else {}
    if not isinstance(scopes, dict):
        scopes = {}
    for scope in ("user", "system"):
        entry = scopes.get(scope)
        if not isinstance(entry, dict) or not entry.get("available"):
            reason = entry.get("reason", "absent") if isinstance(entry, dict) else "absent"
            findings.add("LIVE", None, scope, None, "unknown", "scope-unmeasured",
                         f"LIVE scope {scope!r} was not measured ({reason})", True,
                         f"Re-run with the {scope} scope available or supply a fresh capture.")
            unmeasured = True
            continue
        measured.append(scope)
        timers = entry.get("timers", [])
        if not isinstance(timers, list):
            continue
        for timer in timers:
            if not isinstance(timer, dict) or not isinstance(timer.get("unit"), str):
                continue
            live[(scope, timer["unit"])] = timer
    by_unit = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "systemd-timer":
            continue
        scope = entry.get("scope")
        if not isinstance(scope, str):
            continue
        for name in unit_names(entry):
            by_unit.setdefault((scope, name), entry)
    for (scope, unit), timer in sorted(live.items()):
        entry = by_unit.get((scope, unit))
        fragment = timer.get("fragmentPath") or ""
        if entry is None and isinstance(fragment, str) and fragment.startswith(VENDOR_PREFIXES):
            continue  # shipped by the distribution (apt-daily, logrotate, ...), not ours to consolidate
        if entry is None:
            findings.add("LIVE", None, scope, unit, "violation",
                         "unregistered-live-timer",
                         f"LIVE {scope}/{unit}: on the host, not in the registry (classify it)",
                         False, "Classify the host timer in the registry, then re-run with a fresh capture.")
            continue
        eid = entry.get("id") if isinstance(entry.get("id"), str) else None
        service = timer.get("service") if isinstance(timer.get("service"), dict) else {}
        if entry.get("state") == "retired":
            findings.add("LIVE", eid, scope, unit, "violation",
                         "retired-still-present",
                         f"LIVE {scope}/{unit}: registry says retired, host still has it ({timer.get('unitFileState')})",
                         False, "Remove the retired timer from the host or fix the registry, "
                                "then re-run with a fresh capture.")
        elif timer.get("activeState") == "active" and service.get("result") not in (None, "success", ""):
            findings.add("LIVE", eid, scope, unit, "violation", "last-run-failed",
                         f"LIVE {scope}/{unit}: last run result {service.get('result')!r}",
                         False, "Investigate the failed last run, then re-run with a fresh capture.")
        elif _member(entry.get("state"), PRIMARY_STATES | {"retire-pending"}) \
                and entry.get("expectLive") is True \
                and timer.get("unitFileState") not in ("enabled", "static", "enabled-runtime"):
            findings.add("LIVE", eid, scope, unit, "violation",
                         "expected-live-disabled",
                         f"LIVE {scope}/{unit}: expected live but unitFileState={timer.get('unitFileState')}",
                         False, "Enable the expected timer, then re-run with a fresh capture.")
    present = set(live)
    measured_scopes = set(measured)
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "systemd-timer":
            continue
        if entry.get("expectLive") is not True or entry.get("state") == "retired":
            continue
        scope = entry.get("scope")
        if not isinstance(scope, str):
            continue
        if scope not in measured_scopes:
            continue  # unmeasured scope: "could not measure", never a false absence claim
        names = unit_names(entry)
        if not any((scope, name) in present for name in names):
            eid = entry.get("id") if isinstance(entry.get("id"), str) else None
            unit = entry.get("unit") if isinstance(entry.get("unit"), str) else None
            if unit is None and names:
                unit = sorted(names)[0]
            findings.add("LIVE", eid, scope, unit, "violation",
                         "expected-live-absent",
                         f"LIVE {entry.get('id')}: expected live, absent from the host capture",
                         False, "Install/enable the expected timer or correct expectLive, "
                                "then re-run with a fresh capture.")
    return unmeasured


def build_envelope(findings, entry_count, measured_scopes, exit_code, top_action=None):
    results = findings.sorted_results()
    violations = sum(1 for r in results if r["severity"] == "violation")
    unknown = sum(1 for r in results if r["severity"] == "unknown")
    if top_action is None:
        if exit_code == 2:
            top_action = "Fix the input shape described in results, then re-run the audit."
        elif violations:
            top_action = f"Fix {violations} violation(s) listed in results, then re-run the audit."
        elif unknown:
            top_action = "Re-run when the unmeasured scopes are available."
        else:
            top_action = "No action: audit clean."
    return {
        "schemaVersion": JSON_SCHEMA_VERSION,
        "violations": violations,
        "unknown": unknown,
        "coverage": {"entries": entry_count,
                     "liveScopesMeasured": sorted(measured_scopes)},
        "results": results,
        "next_action": top_action,
    }


def malformed_envelope(code, message, entry_count=0, retryable=False):
    findings = Findings()
    findings.add(None, None, None, None, "unknown", code, message, retryable,
                 "Fix the registry JSON shape (schemaVersion 1 with an entries list of objects), "
                 "then re-run the audit."
                 if code == "malformed-registry" else
                 "Fix the capture JSON shape (scopes.user/system with availability plus timers[]), "
                 "then re-run the audit.")
    return build_envelope(findings, entry_count, [], 2)


def run(args):
    want_json = bool(getattr(args, "json", False))
    root = Path(args.root).resolve()
    reg_path = Path(args.registry) if args.registry else root / "ops/case_30b51a0f/host-timer-registry.json"
    try:
        entries = load_registry(reg_path)
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        message = f"unreadable registry: {exc}"
        if want_json:
            print(json.dumps(malformed_envelope("malformed-registry", message), sort_keys=True))
            return 2
        print(message, file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:  # pragma: no cover - ValueError subclass, kept explicit
        message = f"unreadable registry: {exc}"
        if want_json:
            print(json.dumps(malformed_envelope("malformed-registry", message), sort_keys=True))
            return 2
        print(message, file=sys.stderr)
        return 2
    findings = Findings()
    check_schema(entries, findings)
    if not findings.out:
        check_purposes(entries, findings)
        check_writers(entries, findings)
        check_transitions(entries, findings)
        check_unregistered_timers(entries, findings, root)
        check_plugin_jobs(entries, findings, root)
        check_workflows(entries, findings, root)
    unmeasured = False
    measured_scopes = []
    if args.live:
        try:
            capture = json.loads(Path(args.live).read_text(encoding="utf-8"))
            validate_capture(capture)
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            message = f"unreadable capture: {exc}"
            if want_json:
                print(json.dumps(malformed_envelope(
                    "malformed-capture", message, entry_count=len(entries)), sort_keys=True))
                return 2
            print(message, file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:  # pragma: no cover - ValueError subclass
            message = f"unreadable capture: {exc}"
            if want_json:
                print(json.dumps(malformed_envelope(
                    "malformed-capture", message, entry_count=len(entries)), sort_keys=True))
                return 2
            print(message, file=sys.stderr)
            return 2
        unmeasured = check_live(entries, capture, findings)
        scopes = capture.get("scopes", {})
        measured_scopes = sorted(scope for scope in ("user", "system")
                                 if isinstance(scopes.get(scope), dict)
                                 and scopes[scope].get("available"))
    if args.end_state:
        check_end_state(entries, findings)
    hard = [r for r in findings.results if r["severity"] != "unknown"]
    if hard:
        exit_code = 1
    elif unmeasured:
        exit_code = 3
    else:
        exit_code = 0
    if want_json:
        print(json.dumps(build_envelope(findings, len(entries), measured_scopes, exit_code),
                         sort_keys=True))
        return exit_code
    for line in findings.out:
        print(line)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        waivers = entry.get("waivers", [])
        if not isinstance(waivers, list):
            continue
        for waiver in waivers:
            if not isinstance(waiver, dict):
                continue
            print(f"WAIVED {entry.get('id')} [{waiver.get('check')}]: {waiver.get('reason')} -> {waiver.get('fix')}")
    unverified = sum(1 for entry in entries
                     if isinstance(entry, dict) and not entry.get("verified"))
    print(f"entries={len(entries)} unverified={unverified} "
          f"violations={len(findings.out)}")
    return exit_code


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--registry")
    ap.add_argument("--live", help="host_timer_capture.py output to compare against")
    ap.add_argument("--end-state", action="store_true",
                    help="fail while any transition, planned entry or unverified entry remains")
    ap.add_argument("--json", dest="json", action="store_true",
                    help="emit versioned JSON results (schemaVersion 1) with stable IDs, "
                         "coverage, error codes and next_action; exit codes unchanged")
    ap.add_argument("--dry-run", action="store_true",
                    help="offline read-only audit alias: identical exit/output to the default run; "
                         "never host capture and never an apply mode")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
