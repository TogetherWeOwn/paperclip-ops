#!/usr/bin/env bash
# garm_pool_health.sh — strict offline evaluator for ONE GARM pool payload.
#
# Reads a single `garm-cli pool show <id> --format json` document (file argument
# or stdin) and reports whether that pool is healthy. This is the fail-closed
# half of GARM pool monitoring (case_b24794c2, slice of case_66436e78 deliverable 3):
#
#   * every field the verdict depends on must be PRESENT and well-typed.
#     GARM's Go model tags `min_idle_runners` and `instances` with `omitempty`,
#     so live v0.2.1 omits both when they are zero/empty (measured 2026-10-02).
#     This evaluator treats an absent/null/mis-typed field as UNHEALTHY —
#     never as an implicit zero. Absence is not evidence of spare capacity.
#   * the aggregate cron push (ops/case_658bf6b9/push-garm-pool.sh) deliberately
#     keeps the measured absent-means-zero tolerance so today's min-idle-0
#     pools stay green there. That tolerance lives in exactly one place; it
#     must not leak into per-pool liveness. See README.md for the split.
#
# Runner buckets use the push script's InstanceStatus vocabulary:
#   idle = status `running` + runner_status `idle`
#   busy = status `running` + runner_status neither `idle` nor `failed`
#   stale = everything else (provisioning, draining, stopped, unknown)
# prov_fail counts status `error` / runner_status `failed`.
#
# Label attribution: GARM Pool JSON carries no GitHub-label field, so the
# eligible label is caller-supplied (GARM_ELIGIBLE_LABEL, default
# two-ephemeral — the same default the push script uses). The evaluator
# reports online/busy counts UNDER that label; it never discovers the mapping.
#
# Usage:
#   garm_pool_health.sh [--pool-json FILE] [--waiting] [--label LABEL]
#   ... | garm_pool_health.sh   # stdin
#
#   --waiting  eligible-label demand is currently waiting (composer: the queue
#              walk owned by push-garm-pool.sh / case_6bb27f43). Without demand,
#              broken VMs are log info, not a page — GARM self-heals idle
#              breakage. With demand, missing warm capacity or prov-fail VMs
#              are UNHEALTHY (the case_e26bcd57 stall signature).
#
# Exit: 0 HEALTHY (only then may a push report success) · 1 UNHEALTHY with the
#       reason on stdout · 2 REFUSED (unreadable input — also never healthy).
# Never prints secrets: the payload holds pool/instance names only.
set -uo pipefail

LABEL="${GARM_ELIGIBLE_LABEL:-two-ephemeral}"
POOL_JSON=""
WAITING=0

while (( $# > 0 )); do
  case "$1" in
    --pool-json) POOL_JSON="${2:-}"; shift 2 ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    --waiting) WAITING=1; shift ;;
    --help|-h) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "garm_pool_health: refusing — unknown argument '$1'" >&2; exit 2 ;;
  esac
done

[[ "$LABEL" =~ ^[A-Za-z0-9_.-]+$ ]] \
  || { echo "garm_pool_health: refusing — bad label '$LABEL'" >&2; exit 2; }

if [[ -n "$POOL_JSON" ]]; then
  [[ -r "$POOL_JSON" ]] \
    || { echo "garm_pool_health: refusing — cannot read '$POOL_JSON'" >&2; exit 2; }
  payload="$(cat "$POOL_JSON")" || { echo "garm_pool_health: refusing — cannot read '$POOL_JSON'" >&2; exit 2; }
else
  payload="$(cat)" || { echo "garm_pool_health: refusing — cannot read stdin" >&2; exit 2; }
fi

command -v jq >/dev/null 2>&1 \
  || { echo "garm_pool_health: refusing — jq required" >&2; exit 2; }

unhealthy() { echo "UNHEALTHY $1"; exit 1; }

# Object shape first: arrays, scalars and garbage are refused, not scored.
jq -e 'type == "object"' <<<"$payload" >/dev/null 2>&1 \
  || { echo "garm_pool_health: refusing — payload is not a JSON object" >&2; exit 2; }

# Typed probes. Each field is classified by jq `type`, never by `| tostring`:
# stringifying the raw field erases the JSON type, so `"enabled":"true"` or
# `"min_idle_runners":"1"` (quoted strings) would read as the typed value and
# pass. Anything absent, null, quoted, boolean-where-number, negative or
# fractional collapses to the sentinel `invalid`. Absence is NOT zero: this is
# the strict half of the split, WITHOUT the push script's absent-means-zero
# fallback. `has()` is kept only for `instances`, whose element type is probed
# separately below. A DISABLED pool (`false`) is a valid boolean, so the probe
# emits it rather than jq's `//` (which would treat false as missing).
enabled="$(jq -r 'if (.enabled | type) == "boolean" then (.enabled | tostring) else "invalid" end' <<<"$payload" 2>/dev/null)" \
  || { echo "garm_pool_health: refusing — payload unreadable" >&2; exit 2; }
min_idle_raw="$(jq -r 'def whole: type == "number" and . >= 0 and . == floor; if (.min_idle_runners | whole) then (.min_idle_runners | floor | tostring) else "invalid" end' <<<"$payload" 2>/dev/null)"
max_raw="$(jq -r 'def whole: type == "number" and . >= 0 and . == floor; if (.max_runners | whole) then (.max_runners | floor | tostring) else "invalid" end' <<<"$payload" 2>/dev/null)"
has_instances="$(jq -r 'has("instances") | tostring' <<<"$payload" 2>/dev/null)"
pool_id="$(jq -r '.id // "?"' <<<"$payload" 2>/dev/null)"
short="${pool_id:0:8}"

[[ "$enabled" == "true" || "$enabled" == "false" ]] \
  || unhealthy "pool $short: field 'enabled' missing or not boolean"
[[ "$enabled" == "true" ]] \
  || unhealthy "pool $short disabled"
# STRICT (case_b24794c2): absent min_idle_runners fails closed. The aggregate push
# maps absent→0 from the measured v0.2.1 omitempty shape; this evaluator does
# not — a pool that does not declare its floor has no verifiable floor.
[[ "$min_idle_raw" =~ ^[0-9]+$ ]] \
  || unhealthy "pool $short: field 'min_idle_runners' missing or not a whole number (fail closed)"
[[ "$max_raw" =~ ^[0-9]+$ ]] \
  || unhealthy "pool $short: field 'max_runners' missing or not a whole number (fail closed)"
[[ "$has_instances" == "true" ]] \
  || unhealthy "pool $short: field 'instances' missing (fail closed)"
jq -e '.instances | type == "array"' <<<"$payload" >/dev/null 2>&1 \
  || unhealthy "pool $short: field 'instances' is not an array (fail closed)"
# An element that is not an object with a string `status` (null, {}, a bare
# string) cannot be bucketed; counting it as `stale` would let a pool of
# unreadable entries read HEALTHY. Fail closed instead.
jq -e '.instances | all(type == "object" and (.status | type) == "string")' <<<"$payload" >/dev/null 2>&1 \
  || unhealthy "pool $short: an instance entry lacks a string 'status' (fail closed)"

inst_tsv="$(jq -r '.instances[] | [(.status // "?"), (.runner_status // "?"), (.name // "?")] | @tsv' <<<"$payload" 2>/dev/null)" \
  || unhealthy "pool $short: instances unreadable"

idle=0; busy=0; stale=0; prov_fail=0; creating=0; actual=0
prov_names=()
while IFS=$'\t' read -r st rs nm; do
  [[ -n "$st" ]] || continue
  actual=$((actual + 1))
  if [[ "$st" == "error" || "$rs" == "failed" ]]; then
    prov_fail=$((prov_fail + 1))
    (( ${#prov_names[@]} < 3 )) && prov_names+=("$short/$nm")
  elif [[ "$st" == "pending_delete" || "$st" == "pending_force_delete" || "$st" == "deleting" ]]; then
    stale=$((stale + 1))
  elif [[ "$st" == "running" && "$rs" == "idle" ]]; then
    idle=$((idle + 1))
  elif [[ "$st" == "running" ]]; then
    busy=$((busy + 1))
  else
    stale=$((stale + 1))
  fi
  case "$st" in
    pending_create|creating) creating=$((creating + 1)) ;;
  esac
  [[ "$rs" == "installing" ]] && creating=$((creating + 1))
done <<<"$inst_tsv"

(( actual <= 10#$max_raw )) \
  || unhealthy "pool $short holds $actual instances over max $max_raw"

if (( WAITING == 1 )); then
  (( idle >= 10#$min_idle_raw )) || (( creating > 0 )) \
    || unhealthy "pool $short label $LABEL: idle $idle < min-idle $min_idle_raw with eligible demand waiting and nothing provisioning"
  (( prov_fail == 0 )) \
    || unhealthy "pool $short label $LABEL: prov-fail instances (${prov_names[*]}) with eligible demand waiting"
fi

echo "HEALTHY pool=$short label=$LABEL idle=$idle busy=$busy stale=$stale min_idle=$min_idle_raw max=$max_raw actual=$actual prov_fail=$prov_fail"
