#!/usr/bin/env bash
# push-enforce-readiness.sh — cron: enforce-readiness probe readout →
# Gatus (`router_enforce-readiness`).
#
# Reads one local file and pushes one boolean; that is all it does:
#   ENFORCE_READINESS (required): case_3d05301b enforce-readiness readout JSON
#     (schema case_1320d1f5-enforce-readiness-v1), already produced from
#     caller-observed roster rows by its own producer. This script never
#     builds a readout from a roster: it reads the readout's own verdict,
#     evaluatedAt, emptyTiers and blocking-gate statuses and reports them.
#     Until the live producer lands, the operator may point ENFORCE_READINESS
#     at an example fixture
#     fixtures/enforce-readiness-ready.example.json (or the -notready
#     variant) — labelled, not live. NOTE: fixture evaluatedAt values are
#     fixed, so under a live clock they read stale-by-design; fixtures
#     validate the HEALTHY/DEGRADED mapping with a pinned clock (the suite
#     pins NOW_OVERRIDE to the fixture evaluatedAt).
#
# READINESS THRESHOLD: READINESS_STALE_AFTER_SEC (default 3600 — one hour,
# the blind-monitor horizon: a readiness producer that stops writing reads
# as stale within the hour, never as healthy silence). A stale readout
# (evaluatedAt older than the horizon) pushes success=false, as does a
# future evaluatedAt (clock or producer corrupt).
#
# HEALTHY/DEGRADED MAPPING: HEALTHY (success=true) exactly when verdict is
# "ready" AND the readout is fresh. DEGRADED (success=false) on a not-ready
# verdict (the push message names the empty tiers and the failing blocking
# gates, so the page shows WHY enforce would refuse: e.g. "empty tiers:
# T2; failing blocking gates: no-empty-tier"), or on
# missing/malformed/wrong-schema input. An advisory-only gate failure
# (blocking=false, e.g. tier-labels-configured) never moves the verdict —
# the module reports the readout's own verdict, it does not recompute it.
#
# JSON VERDICT: the evaluator prints one line of case_080c50a9-enforce-readiness-v1
# JSON, which this script echoes to stdout (the cron log), so the module
# returns JSON on fixtures:
# {"schema":"case_080c50a9-enforce-readiness-v1","verdict":{...}}.
#
# FAIL-CLOSED: unreadable/malformed/wrong-schema ENFORCE_READINESS pushes
# success=false; an evaluator failure pushes false (a blind monitor never
# reads green); a DOWN endpoint exits 1 so cron reports it. Missing
# ENFORCE_READINESS, GATUS_URL or GATUS_EXTERNAL_TOKEN refuses (exit 2)
# before touching the network. ENFORCE_KEY_SUFFIX must be empty or -slug
# (path traversal guard); NOW_OVERRIDE (epoch seconds, tests only) pins the
# clock.
#
# READ-ONLY / NO SECRETS: reads one local file, POSTs a boolean. Never
# fetches, never resolves secrets, never writes ledgers, never admits work,
# never changes pacing or enforcement. Tier and gate names are operational
# metadata, not secrets. Bearer travels in a 0600 curl config, never argv
# (see _gatus_push.sh).
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh),
#   ENFORCE_READINESS (readout JSON path, required),
#   READINESS_STALE_AFTER_SEC (default 3600),
#   ENFORCE_KEY_SUFFIX (optional per-source fan-out), NOW_OVERRIDE (tests).
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-enforce-readiness.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

STALE_AFTER_SEC="${READINESS_STALE_AFTER_SEC:-3600}"
KEY_SUFFIX="${ENFORCE_KEY_SUFFIX:-}"
NOW_PIN="${NOW_OVERRIDE:-}"

[[ -n "${ENFORCE_READINESS:-}" ]] \
  || { echo "push-enforce-readiness: refusing — ENFORCE_READINESS is unset" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-enforce-readiness: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-enforce-readiness: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
[[ -z "$KEY_SUFFIX" || "$KEY_SUFFIX" =~ ^-[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
  || { echo "push-enforce-readiness: refusing — ENFORCE_KEY_SUFFIX must be empty or -slug" >&2; exit 2; }

READY_KEY="router_enforce-readiness${KEY_SUFFIX}"

command -v python3 >/dev/null 2>&1 \
  || { echo "push-enforce-readiness: refusing — python3 required" >&2; exit 2; }

# Evaluate the endpoint in one python3 pass (stdlib only). Stdout is one
# "<READY>\t<true|false>\t<message>" control line followed by the
# case_080c50a9-enforce-readiness-v1 verdict JSON line.
EVAL="$(NOW_OVERRIDE="$NOW_PIN" python3 - "$ENFORCE_READINESS" "$STALE_AFTER_SEC" <<'PY'
import json, os, sys

read_path, stale_s = sys.argv[1:3]
stale = int(stale_s)
now_pin = os.environ.get("NOW_OVERRIDE") or ""
now = int(now_pin) if now_pin.strip().isdigit() else None
if now is None:
    import time
    now = int(time.time())

try:
    with open(read_path, encoding="utf-8") as h:
        read = json.load(h)
    if not isinstance(read, dict):
        raise ValueError("readout root must be an object")
    if read.get("schema") != "case_1320d1f5-enforce-readiness-v1":
        raise ValueError(f"unexpected schema {json.dumps(read.get('schema'))}; want case_1320d1f5-enforce-readiness-v1")
    verdict = read.get("verdict")
    if verdict not in ("ready", "not-ready"):
        raise ValueError(f"unexpected verdict {json.dumps(verdict)}; want ready|not-ready")
    evaluated = read.get("evaluatedAt")
    if not isinstance(evaluated, (int, float)) or not (evaluated == evaluated and abs(evaluated) != float("inf")):
        raise ValueError("evaluatedAt must be a finite number")
    evaluated_s = int(evaluated // 1000)
except (OSError, ValueError) as exc:
    ctl = f"READY\tfalse\treadiness readout unreadable ({exc}); fail-closed DOWN"
    det = ("unreadable", None, None, str(exc))
else:
    age = now - evaluated_s
    empty = read.get("emptyTiers")
    empty_names = ", ".join(empty) if isinstance(empty, list) and empty else ""
    failing = [g.get("gate") for g in read.get("gates", []) if isinstance(g, dict) and g.get("blocking") and not g.get("pass")]
    failing_names = ", ".join(str(g) for g in failing if g)
    detail = f"empty tiers: {empty_names}" if empty_names else "no empty tiers"
    if failing_names:
        detail += f"; failing blocking gates: {failing_names}"
    if age < 0:
        ctl = f"READY\tfalse\treadiness readout is in the future ({age}s); clock or producer corrupt, fail-closed DOWN"
        det = ("future", age, verdict, detail)
    elif age > stale:
        ctl = f"READY\tfalse\treadiness readout stale: evaluated {age}s ago (> {stale}s); fail-closed DOWN"
        det = ("stale", age, verdict, detail)
    elif verdict == "ready":
        ctl = f"READY\ttrue\treadiness ready: {detail} (evaluated {age}s ago, staleness horizon {stale}s)"
        det = ("ready", age, verdict, detail)
    else:
        ctl = f"READY\tfalse\treadiness not-ready: {detail}; fail-closed DOWN"
        det = ("not-ready", age, verdict, detail)

print(ctl)
print(json.dumps({
    "schema": "case_080c50a9-enforce-readiness-v1",
    "verdict": {"state": det[0], "ageSeconds": det[1], "readoutVerdict": det[2], "note": det[3]},
}))
PY
)" || {
  echo "push-enforce-readiness: evaluator failed — fail-closed, marking endpoint DOWN" >&2
  gatus_push "$READY_KEY" false "evaluator failure (fail-closed DOWN)" || true
  exit 1
}

# Split the control line from the verdict JSON (the single {-leading line).
VERDICT="$(printf '%s\n' "$EVAL" | grep '^{' | tail -n 1)"
CTRL="$(printf '%s\n' "$EVAL" | grep -v '^{' || true)"
READY_LINE="$(grep '^READY' <<<"$CTRL" | tail -n 1)"
[[ -n "$READY_LINE" && -n "$VERDICT" ]] || {
  echo "push-enforce-readiness: evaluator output incomplete — fail-closed, marking endpoint DOWN" >&2
  gatus_push "$READY_KEY" false "evaluator output incomplete (fail-closed DOWN)" || true
  exit 1
}

echo "push-enforce-readiness: $VERDICT"  # verdict lands in the cron log, not just the push
ready_body="${READY_LINE#*$'\t'}"
ready_success="${ready_body%%$'\t'*}"; ready_msg="${ready_body#*$'\t'}"

rc=0
if [[ "$ready_success" == "true" ]]; then
  gatus_push "$READY_KEY" true || rc=1
else
  gatus_push "$READY_KEY" false "$ready_msg" || true
  rc=1  # a DOWN endpoint is an incident: cron must report it
fi
exit "$rc"
