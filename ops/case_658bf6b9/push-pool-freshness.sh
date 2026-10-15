#!/usr/bin/env bash
# push-pool-freshness.sh — cron: GARM pool heartbeat freshness → Gatus
# (`platform_pool-freshness`).
#
# Per pool in GARM_POOLS (space-separated FULL pool UUIDs, the same IDs as
# push-garm-pool.sh): reads `garm-cli pool show <pool> --format json` and
# takes the pool HEARTBEAT as the freshest instance activity — the max over
# every instance of max(created_at, updated_at), both casings accepted
# (`created_at // createdAt`, `updated_at // updatedAt`; the same timestamp
# vocabulary as github-runner/garm/audit_stale_instances.sh). FRESH exactly
# when that heartbeat is within GARM_FRESHNESS_THRESHOLD_MIN (default 15) of
# now; STALE past it. The worst pool names the page (pool short-ID + age in
# minutes), so the alert shows HOW stale the fleet is, not just up/down.
#
# A pool with zero instances and min-idle 0 reads healthy-idle ("no
# instances, none reserved") — that is today's fleet state on both installed
# pools, and push-garm-pool.sh already calls it HEALTHY. A pool with zero
# instances while min-idle is reserved reads STALE: there is no heartbeat
# at all where warm capacity was promised.
#
# THRESHOLD (env-tunable, operator owns the value): default alert at 15
# minutes. Why 15: the cron cadence is 5m, so 15m tolerates two consecutive
# quiet polls plus jitter, while staying well below the 60m eligible-queue
# breach in push-garm-pool.sh — a stuck pool pages here first. Caveat, stated
# plainly: this module reports OBSERVED activity. A long-idle low-traffic
# pool whose long-lived instances have not changed state in hours reads
# stale by design; the threshold is the operator's call for each pool's
# churn character, not a proof of breakage.
#
# JSON VERDICT: prints one line of case_c99d8d47-pool-freshness-v1 JSON to stdout
# (the cron log), so the module returns JSON on fixtures:
# {"schema":"case_c99d8d47-pool-freshness-v1","verdict":{...}}.
#
# FAIL-CLOSED: a garm-cli transport failure, an unparseable pool response,
# an instance with no parseable timestamp (unknown-age is never fresh),
# a disabled pool, more instances than live max_runners, or a reserved-
# but-empty pool pushes success=false; a DOWN endpoint exits 1 so cron
# reports it. Missing GARM_POOLS / GATUS_URL / GATUS_EXTERNAL_TOKEN, a bad
# pool ID, a non-positive threshold, a bad NOW_OVERRIDE clock, or a missing
# garm-cli / jq binary refuses (exit 2) before touching the network — the
# 10m heartbeat miss then pages, never a silent green.
#
# READ-ONLY / NO SECRETS / NO REMEDIATION: issues pool-show GETs and POSTs
# one boolean. Never deletes/destroys/provisions, never changes pool caps,
# never admits work. Bearer travels in a 0600 curl config, never argv
# (see _gatus_push.sh). NOW_OVERRIDE (epoch seconds, tests only) pins the
# clock.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), GARM_POOLS
# (space-separated FULL pool UUIDs, required),
# GARM_FRESHNESS_THRESHOLD_MIN (default 15),
# GARM_BIN (default garm-cli), NOW_OVERRIDE (tests only).
# Cron (operator, separate CEO Operator card — not in OPERATOR.md):
#   */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-pool-freshness.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

THRESHOLD_MIN="${GARM_FRESHNESS_THRESHOLD_MIN:-15}"
GARM_BIN="${GARM_BIN:-garm-cli}"
NOW_PIN="${NOW_OVERRIDE:-}"
KEY="platform_pool-freshness"

[[ "$THRESHOLD_MIN" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-pool-freshness: refusing — GARM_FRESHNESS_THRESHOLD_MIN must be a positive integer" >&2; exit 2; }
read -r -a POOLS <<<"${GARM_POOLS:-}"
(( ${#POOLS[@]} > 0 )) \
  || { echo "push-pool-freshness: refusing — GARM_POOLS (space-separated pool IDs) must be set" >&2; exit 2; }
for pool in "${POOLS[@]}"; do
  [[ "$pool" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || { echo "push-pool-freshness: refusing — bad pool ID '$pool'" >&2; exit 2; }
done
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-pool-freshness: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-pool-freshness: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
command -v "$GARM_BIN" >/dev/null 2>&1 \
  || { echo "push-pool-freshness: refusing — no '$GARM_BIN' on PATH (operator install?)" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-pool-freshness: refusing — jq required" >&2; exit 2; }
if [[ -n "$NOW_PIN" ]]; then
  [[ "$NOW_PIN" =~ ^[0-9]+$ ]] \
    || { echo "push-pool-freshness: refusing — NOW_OVERRIDE must be epoch seconds (tests only)" >&2; exit 2; }
  now="$NOW_PIN"
else
  now="$(date -u +%s)"
fi

fail_push() {  # $1 = reason — fail-closed: unreadable verdict JSON + DOWN push
  local reason="$1" v
  v="$(jq -cn --arg r "$reason" \
    '{schema:"case_c99d8d47-pool-freshness-v1",verdict:{state:"unreadable",pools:[],note:$r}}')"
  echo "push-pool-freshness: $v"
  gatus_push "$KEY" false "$reason" || true
  exit 1
}

declare -a POOL_JSON=()
declare -a NOTE_BITS=()
worst_pool=""; worst_age=0

for pool in "${POOLS[@]}"; do
  short="${pool:0:8}"
  pj="$("$GARM_BIN" pool show "$pool" --format json 2>&1)" \
    || fail_push "garm-cli pool show failed for $short"
  # Shape-guard the fields we decide on; anything else-shaped is UNKNOWN,
  # and UNKNOWN is never healthy. `| tostring`, not `// empty`: jq's //
  # treats false as missing, so `.enabled // empty` reads a DISABLED pool
  # as unreadable.
  enabled="$(jq -r '.enabled | tostring' <<<"$pj" 2>/dev/null)" \
    || fail_push "unparseable pool JSON for $short"
  min_idle="$(jq -r 'if has("min_idle_runners") then .min_idle_runners | tostring else "ABSENT" end' <<<"$pj" 2>/dev/null)"
  max_run="$(jq -r '.max_runners // empty' <<<"$pj" 2>/dev/null)"
  # Live GARM v0.2.1 `pool show` omits BOTH `min_idle_runners` and
  # `instances` when they are zero/empty (params.Pool tags both `omitempty`;
  # measured on both installed pools — see push-garm-pool.sh). Absent
  # min_idle means 0; absence is not a license for garbage (checked below).
  [[ "$min_idle" == "ABSENT" ]] && min_idle="0"
  [[ "$enabled" == "true" || "$enabled" == "false" ]] \
    || fail_push "unparseable pool enabled flag for $short"
  [[ "$min_idle" =~ ^[0-9]+$ && "$max_run" =~ ^[0-9]+$ ]] \
    || fail_push "unparseable pool min/max for $short"
  [[ "$enabled" == "true" ]] \
    || fail_push "pool $short disabled"
  inst_tsv="$(jq -r '.instances[]? | [(.status // "?"), (.runner_status // "?"), (.name // "?"), ((.created_at // .createdAt // "") | tostring), ((.updated_at // .updatedAt // "") | tostring)] | @tsv' <<<"$pj" 2>/dev/null)" \
    || fail_push "unparseable pool instances for $short"

  actual=0; idx=0; hb=0; have_hb=0
  while IFS=$'\t' read -r st rs nm cr up; do
    [[ -n "$st" ]] || continue
    actual=$((actual + 1)); idx=$((idx + 1))
    # VM names are operator-controlled; anything outside the safe alphabet
    # is addressed by index so a hostile name never reaches the page verbatim.
    [[ "$nm" =~ ^[A-Za-z0-9_.-]+$ ]] || nm="inst-$idx"
    best=0; have_stamp=0
    for ts in "$cr" "$up"; do
      [[ -n "$ts" ]] || continue
      s="$(date -u -d "$ts" +%s 2>/dev/null)" || continue
      have_stamp=1
      (( s > best )) && best="$s"
    done
    # Unknown-age fails closed: an instance with no parseable timestamp is
    # never evidence of freshness.
    (( have_stamp == 1 )) \
      || fail_push "pool $short/$nm: heartbeat unreadable (no parseable created/updated)"
    if (( have_hb == 0 || best > hb )); then hb="$best"; have_hb=1; fi
  done <<<"$inst_tsv"

  (( actual <= 10#$max_run )) \
    || fail_push "pool $short holds $actual instances over max $max_run"
  if (( actual == 0 )); then
    if (( 10#$min_idle == 0 )); then
      NOTE_BITS+=("$short idle (no instances, none reserved)")
      POOL_JSON+=("$(jq -cn --arg p "$short" '{pool:$p,heartbeatAgeMin:null,verdict:"idle"}')")
      continue
    fi
    fail_push "pool $short holds no instances while min-idle $min_idle reserved (no heartbeat)"
  fi
  age_min=$(( (now - hb) / 60 ))
  (( age_min < 0 )) && age_min=0   # future clock skew reads as newborn, never as stale
  if (( age_min > 10#$THRESHOLD_MIN )); then
    verdict="stale"
    if [[ -z "$worst_pool" ]] || (( age_min > worst_age )); then
      worst_pool="$short"; worst_age="$age_min"
    fi
  else
    verdict="fresh"
  fi
  NOTE_BITS+=("$short $verdict (${age_min}m)")
  POOL_JSON+=("$(jq -cn --arg p "$short" --argjson a "$age_min" --arg v "$verdict" \
    '{pool:$p,heartbeatAgeMin:$a,verdict:$v}')")
done

pools_json="$(printf '%s\n' "${POOL_JSON[@]}" | jq -cs '. | map(select(. != null))' 2>/dev/null)"
[[ -n "$pools_json" ]] || pools_json="[]"
if [[ -n "$worst_pool" ]]; then
  state="stale"
  msg="pool heartbeat stale: $worst_pool ${worst_age}m old (threshold ${THRESHOLD_MIN}m); per-pool ${NOTE_BITS[*]}"
else
  state="fresh"
  msg="pool heartbeats fresh within ${THRESHOLD_MIN}m; per-pool ${NOTE_BITS[*]}"
fi
VERDICT_JSON="$(jq -cn --arg s "$state" --argjson pools "$pools_json" --arg n "$msg" \
  '{schema:"case_c99d8d47-pool-freshness-v1",verdict:{state:$s,pools:$pools,note:$n}}')"

echo "push-pool-freshness: $VERDICT_JSON"  # verdict lands in the cron log, not just the push

rc=0
if [[ "$state" == "fresh" ]]; then
  gatus_push "$KEY" true || rc=1
else
  gatus_push "$KEY" false "$msg" || true
  rc=1  # a STALE endpoint is an incident: cron must report it
fi
exit "$rc"
