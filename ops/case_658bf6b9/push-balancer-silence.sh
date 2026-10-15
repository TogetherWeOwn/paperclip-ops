#!/usr/bin/env bash
# push-balancer-silence.sh — systemd timer: fleet-quota-balancer silence → Gatus
# `platform_balancer-silence` (case_e6def006, parent install case_f4b931e7).
#
# Runs the reviewed guard (balancer_silence_guard.py, case_7edd6ab6) and pushes its
# verdict to a DEDICATED external endpoint. Stale (exit 1) and unmeasurable
# (exit 2) both push success=false — silence or blindness never reads healthy.
# Fresh (exit 0) pushes success=true so Gatus resolves the incident.
#
# FAIL-CLOSED: unreadable/missing/empty/garbage log, bad --now/threshold, or
# a guard crash pushes success=false and exits 1. Missing GATUS_URL/token,
# missing guard, or missing python3 refuses (exit 2) with zero pushes: the
# 10m heartbeat miss then pages, never a silent green.
#
# DELIVERY FAILURE IS VISIBLE: a failed gatus_push exits 1, so the systemd
# unit reports failed and the journal records it; the next 5m tick retries.
# The Bearer token travels in a 0600 curl config via _gatus_push.sh, never in
# argv. Push text is the guard's own verdict line only (fixed phrases plus
# numbers), never raw log rows — the log's model/reason strings stay local.
#
# DEDICATED KEY: platform_balancer-silence only. Never reuse memory
# (platform_oom-kills etc.) or another monitor's key.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (root-only 0600 env file, see
# OPERATOR.md §2 — never in the unit file or argv),
#   BALANCER_SILENCE_LOG (default /paperclip/shared/host-bridge/fleet-quota-balancer.log),
#   BALANCER_SILENCE_STATE (default .../fleet-quota-balancer.state.json),
#   BALANCER_SILENCE_MAX_AGE_MIN (default 10), BALANCER_SILENCE_WINDOW_H (default 24),
#   BALANCER_SILENCE_GUARD (default <this-dir>/../../balancer_silence_guard.py),
#   BALANCER_SILENCE_NOW (tests only: forwarded as --now).
# Cron (operator, as root per OPERATOR.md §4 — a --user manager could not
# read the root-only 0600 env file): `set -a; . /root/.config/paperclip/
# case_60330d6f.env` (the env file has no `export` lines) plus
# BALANCER_SILENCE_LOG/STATE set to the REAL host mount paths, every 5m.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

ENDPOINT_KEY="platform_balancer-silence"
LOG="${BALANCER_SILENCE_LOG:-/paperclip/shared/host-bridge/fleet-quota-balancer.log}"
STATE="${BALANCER_SILENCE_STATE:-/paperclip/shared/host-bridge/fleet-quota-balancer.state.json}"
MAX_AGE="${BALANCER_SILENCE_MAX_AGE_MIN:-10}"
WINDOW_H="${BALANCER_SILENCE_WINDOW_H:-24}"
GUARD="${BALANCER_SILENCE_GUARD:-$HERE/../../balancer_silence_guard.py}"
NOW_OVERRIDE="${BALANCER_SILENCE_NOW:-}"

[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-balancer-silence: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-balancer-silence: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
[[ -f "$GUARD" ]] \
  || { echo "push-balancer-silence: refusing — guard not found at $GUARD" >&2; exit 2; }
[[ "$MAX_AGE" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || { echo "push-balancer-silence: refusing — BALANCER_SILENCE_MAX_AGE_MIN must be a positive number" >&2; exit 2; }
[[ "$WINDOW_H" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || { echo "push-balancer-silence: refusing — BALANCER_SILENCE_WINDOW_H must be a positive number" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-balancer-silence: refusing — python3 required" >&2; exit 2; }

args=(--log "$LOG" --state "$STATE" --max-age-min "$MAX_AGE" --window-h "$WINDOW_H")
if [[ -n "$NOW_OVERRIDE" ]]; then
  args+=(--now "$NOW_OVERRIDE")
fi

out_file="$(umask 077; mktemp "${TMPDIR:-/tmp}/balancer_silence_push.XXXXXXXX")" \
  || { echo "push-balancer-silence: refusing — could not create temp file" >&2; exit 2; }
trap 'rm -f "$out_file"' EXIT

# The guard is read-only (log + state in, verdict out). Its nonzero exits are
# the signal, not a failure to hide: capture both streams for the verdict line.
python3 "$GUARD" "${args[@]}" >"$out_file" 2>&1
rc=$?

# Verdict line only: STALE / FRESH / UNMEASURABLE plus numbers. GAP detail
# (model/reason rows) stays in the journal, never on the wire.
verdict="$(grep -aE '^(STALE|FRESH|UNMEASURABLE):' "$out_file" | tail -1 || true)"
if [[ -z "$verdict" ]]; then
  # Guard printed no verdict (crash/traceback): fail closed with a fixed phrase.
  verdict="UNMEASURABLE: guard produced no verdict (rc=$rc)"
  rc=2
fi
# Bound the wire text: fixed guard phrases plus numbers; _gatus_push.sh caps
# at MAX_PUSH_ERROR_CHARS downstream as well.
verdict="${verdict:0:300}"

case "$rc" in
  0)
    gatus_push "$ENDPOINT_KEY" true "balancer fresh: $verdict" || exit 1
    exit 0
    ;;
  1)
    gatus_push "$ENDPOINT_KEY" false "balancer stale: $verdict" || exit 1
    exit 1
    ;;
  *)
    gatus_push "$ENDPOINT_KEY" false "balancer unmeasurable: $verdict" || exit 1
    exit 1
    ;;
esac
