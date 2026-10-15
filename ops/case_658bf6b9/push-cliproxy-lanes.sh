#!/usr/bin/env bash
# push-cliproxy-lanes.sh — cron: CLIProxy per-lane health + quota windows → Gatus.
#
# Reads the canonical quota telemetry JSON (cliproxy_quota_contract.py
# contract_version 4: records[] with provider/health/windows[]) and pushes one
# external endpoint per lane:
#   cliproxy_lane-claude  cliproxy_lane-codex
#   cliproxy_lane-zai     cliproxy_lane-opencode-go
#
# A lane is DOWN when its worst account reports health != "healthy", when any
# allowance window is exhausted (utilization >= 1), or when a window reset has
# passed without fresh telemetry. The push message records the binding
# (worst) window's remaining allowance and reset time, so the status page
# shows quota-window remaining alongside up/down.
#
# FAIL-CLOSED: unreadable or malformed telemetry pushes success=false for
# EVERY lane and exits 1 — a blind monitor never reads green. Missing
# GATUS_URL/token refuses (exit 2) before touching the network.
#
# READ-ONLY: this script only reads the telemetry file and POSTs booleans to
# Gatus. It never touches the CLIProxy management API and carries no
# management credential. Quota percentages and reset times are operational
# metadata, not secrets, and account_keys are already opaque identifiers.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh),
#   CLIPROXY_TELEMETRY (path to canonical contract JSON, required).
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-cliproxy-lanes.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

# Canonical lanes: the quota-contract provider set. Static on purpose — if
# telemetry cannot be read we still know exactly which endpoints to mark DOWN.
LANES="claude codex zai opencode-go"

: "${CLIPROXY_TELEMETRY:-}" \
  || { echo "push-cliproxy-lanes: refusing — CLIPROXY_TELEMETRY is unset" >&2; exit 2; }

command -v python3 >/dev/null 2>&1 \
  || { echo "push-cliproxy-lanes: refusing — python3 required" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-cliproxy-lanes: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-cliproxy-lanes: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
[[ -r "$CLIPROXY_TELEMETRY" ]] \
  || {
    echo "push-cliproxy-lanes: telemetry unreadable ($CLIPROXY_TELEMETRY) — fail-closed, marking all lanes DOWN" >&2
    # shellcheck disable=SC2086
    for lane in $LANES; do
      gatus_push "cliproxy_lane-${lane//_/-}" false "telemetry unreadable: $CLIPROXY_TELEMETRY (fail-closed DOWN)" || true
    done
    exit 1
  }

# Evaluate every lane in one python3 pass (stdlib only): emit
# "<lane>\t<true|false>\t<message>" per line for the shell to push.
EVAL="$(python3 - "$CLIPROXY_TELEMETRY" <<'PY'
import datetime, json, sys

lanes = "claude codex zai opencode-go".split()
try:
    with open(sys.argv[1], encoding="utf-8") as h:
        doc = json.load(h)
    records = doc["records"]
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a non-empty array")
    now = datetime.datetime.now(datetime.timezone.utc)
    observed_raw = doc.get("observedAt", "")
    observed = datetime.datetime.fromisoformat(observed_raw[:-1] + "+00:00") \
        if isinstance(observed_raw, str) and observed_raw.endswith("Z") else None
except Exception as exc:
    for lane in lanes:
        print(f"{lane}\tfalse\ttelemetry malformed ({exc}); fail-closed DOWN")
    sys.exit(0)

by_lane = {lane: [] for lane in lanes}
unknown = set()
for rec in records:
    provider = rec.get("provider") if isinstance(rec, dict) else None
    if provider in by_lane:
        by_lane[provider].append(rec)
    else:
        unknown.add(str(provider))

def window_state(rec):
    """Return (down_reason_or_None, remaining, window_name, resets_at)."""
    worst = None  # (remaining, name, resets_at, exhausted_bool)
    for w in rec.get("windows", []):
        try:
            name, role = w["name"], w["role"]
            util = float(w["utilization"])
            weight = float(w.get("allowance_weight", 1.0))
            resets_at = w["resets_at"]
            reset = datetime.datetime.fromisoformat(resets_at[:-1] + "+00:00")
        except Exception:
            return (f"account {rec.get('account_key')}: malformed window entry", 0.0, "?", "?")
        if reset <= now:
            return (f"account {rec.get('account_key')}: window {name} reset passed without fresh telemetry", 0.0, name, resets_at)
        if role != "allowance":
            continue
        remaining = max(0.0, 1.0 - util) * weight
        if worst is None or remaining < worst[0]:
            worst = (remaining, name, resets_at, util >= 1.0)
    if worst is None:
        return (f"account {rec.get('account_key')}: no allowance window", 0.0, "?", "?")
    remaining, name, resets_at, exhausted = worst
    if exhausted:
        return (f"account {rec.get('account_key')}: window {name} exhausted (utilization>=1)", remaining, name, resets_at)
    return (None, remaining, name, resets_at)

for lane in lanes:
    recs = by_lane[lane]
    if not recs:
        print(f"{lane}\tfalse\tno telemetry for lane; fail-closed DOWN")
        continue
    down_reasons, best_healthy = [], None
    for rec in recs:
        health = rec.get("health")
        reason, remaining, wname, reset = window_state(rec)
        if health != "healthy":
            down_reasons.append(f"account {rec.get('account_key')}: health={health}")
        elif reason is not None:
            down_reasons.append(reason)
        if best_healthy is None or remaining < best_healthy[0]:
            best_healthy = (remaining, wname, reset)
    if down_reasons:
        remaining, wname, reset = best_healthy
        print(f"{lane}\tfalse\t{'; '.join(sorted(set(down_reasons)))} | window {wname} remaining={remaining:.3f} reset={reset}")
    else:
        remaining, wname, reset = best_healthy
        print(f"{lane}\ttrue\twindow {wname} remaining={remaining:.3f} reset={reset}")
PY
)" || {
  echo "push-cliproxy-lanes: evaluator failed — fail-closed, marking all lanes DOWN" >&2
  # shellcheck disable=SC2086
  for lane in $LANES; do
    gatus_push "cliproxy_lane-${lane//_/-}" false "evaluator failure (fail-closed DOWN)" || true
  done
  exit 1
}

rc=0
while IFS=$'\t' read -r lane success msg; do
  [[ -n "$lane" ]] || continue
  if [[ "$success" == "true" ]]; then
    gatus_push "cliproxy_lane-${lane//_/-}" true || rc=1
  else
    gatus_push "cliproxy_lane-${lane//_/-}" false "$msg" || rc=1
    rc=1  # a DOWN lane is an incident: cron must report it
  fi
done <<<"$EVAL"
exit "$rc"
