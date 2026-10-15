#!/usr/bin/env bash
# push-garm-pool.sh — cron: GARM pool state + eligible-label queue age → Gatus
# `platform_garm-pool`.
#
# Per pool in GARM_POOLS (space-separated GARM pool IDs): enabled,
# desired (min_idle_runners) / actual instances, idle / busy / stale buckets,
# provision-failure + deletion-pending counts, live max_runners and provider.
# Eligible demand is the oldest runner-waiting (`queued`) job carrying
# GARM_ELIGIBLE_LABEL (default two-ephemeral) across GH_REPOS — job-level,
# threshold GARM_QUEUE_THRESHOLD_MIN (default 60), using the same server-side
# `--created` walk as push-ci-queue-age.sh (case_6bb27f43: newest-first `--limit`
# alone buries the oldest runs). Ineligible queue depth (queued jobs WITHOUT
# the label) is info text only and never drives success.
#
# HEALTHY rule (case_6ab2cd4d D2): zero VMs with min-idle 0 AND no eligible-label
# breach => success. Failure pushes (exit 1): a disabled pool; any garm-cli /
# gh transport or parse failure; an eligible waiting job older than the
# threshold; idle capacity below min_idle while eligible jobs wait and nothing
# is provisioning; an error/failed instance while eligible jobs wait (broken
# VM + waiting demand is the case_e26bcd57 stall signature); more actual instances
# than live max_runners. Error/failed instances with NO waiting demand stay
# info text — GARM self-heals idle breakage, and the page stays green.
# A missing garm-cli / gh / jq binary, or missing GARM_POOLS / GH_REPOS,
# refuses (exit 2) with zero pushes — the 10m heartbeat miss then pages, never
# a silent green.
#
# Buckets (InstanceStatus vocabulary per cloudbase/garm-provider-common/params;
# live runner_status strings vary by version, so busy is defined by exclusion):
#   idle = provider status `running` AND runner_status `idle` (spare capacity)
#   busy = provider status `running` AND runner_status neither `idle` nor
#          `failed` (executing or finishing a job — occupied either way)
#   stale = everything else (pending_create/creating/installing provisioning;
#           pending_delete/deleting draining; stopped/unknown)
# prov_fail counts status `error` / runner_status `failed` (a VM that errored
# will never pick up a job); del_pending counts pending_delete/deleting.
# NOTE (LXD 5.21): there is no `-c v` column — this script never shells to lxc.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), GARM_POOLS
# (space-separated FULL pool UUIDs, operator-filled from `garm-cli pool list`
# — short 8-char prefixes are rejected with HTTP 400 by the installed
# garm-cli, measured 2026-10-02),
# GH_REPOS (owner/repo list), GARM_ELIGIBLE_LABEL (default two-ephemeral),
# GARM_QUEUE_THRESHOLD_MIN (default 60), GARM_RUN_LIMIT (default 1000, per
# status per repo), GARM_DEPTH_RUN_LIMIT (default 10, info snapshot only),
# GARM_IGNORE_JOB (default "Failure log drain"), GARM_BIN (default garm-cli),
# GH_BIN (default gh), plus whatever the gh credential shim and garm-cli's own
# config need. The host gh must support `run list --created`; an older gh
# fails closed, never green.
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-garm-pool.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

THRESHOLD_MIN="${GARM_QUEUE_THRESHOLD_MIN:-60}"
RUN_LIMIT="${GARM_RUN_LIMIT:-1000}"
DEPTH_LIMIT="${GARM_DEPTH_RUN_LIMIT:-10}"
ELIGIBLE_LABEL="${GARM_ELIGIBLE_LABEL:-two-ephemeral}"
IGNORE_JOB="${GARM_IGNORE_JOB:-Failure log drain}"
GARM_BIN="${GARM_BIN:-garm-cli}"
GH_BIN="${GH_BIN:-gh}"
[[ "$THRESHOLD_MIN" =~ ^[0-9]+$ ]] \
  || { echo "push-garm-pool: refusing — GARM_QUEUE_THRESHOLD_MIN must be whole minutes" >&2; exit 2; }
[[ "$RUN_LIMIT" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-garm-pool: refusing — GARM_RUN_LIMIT must be a positive integer" >&2; exit 2; }
[[ "$DEPTH_LIMIT" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-garm-pool: refusing — GARM_DEPTH_RUN_LIMIT must be a positive integer" >&2; exit 2; }
[[ "$ELIGIBLE_LABEL" =~ ^[A-Za-z0-9_.-]+$ ]] \
  || { echo "push-garm-pool: refusing — bad eligible label '$ELIGIBLE_LABEL'" >&2; exit 2; }
read -r -a POOLS <<<"${GARM_POOLS:-}"
(( ${#POOLS[@]} > 0 )) \
  || { echo "push-garm-pool: refusing — GARM_POOLS (space-separated pool IDs) must be set" >&2; exit 2; }
for pool in "${POOLS[@]}"; do
  [[ "$pool" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || { echo "push-garm-pool: refusing — bad pool ID '$pool'" >&2; exit 2; }
done
read -r -a REPOS <<<"${GH_REPOS:-${GH_REPO:-}}"
(( ${#REPOS[@]} > 0 )) \
  || { echo "push-garm-pool: refusing — GH_REPOS (or GH_REPO) must be set" >&2; exit 2; }
for repo in "${REPOS[@]}"; do
  [[ "$repo" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$ && "${repo#*/}" != "." && "${repo#*/}" != ".." ]] \
    || { echo "push-garm-pool: refusing — bad repo '$repo' (want owner/repo)" >&2; exit 2; }
done

command -v "$GARM_BIN" >/dev/null 2>&1 \
  || { echo "push-garm-pool: refusing — no '$GARM_BIN' on PATH (operator install?)" >&2; exit 2; }
command -v "$GH_BIN" >/dev/null 2>&1 \
  || { echo "push-garm-pool: refusing — no '$GH_BIN' on PATH" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-garm-pool: refusing — jq required" >&2; exit 2; }

fail_push() { gatus_push platform_garm-pool false "$1"; exit 1; }

now="$(date +%s)"
# 10# forces base-10 (same octal trap as push-ci-queue-age.sh, case_0b2d9007).
cutoff="$(date -u -d "@$(( now - 10#$THRESHOLD_MIN * 60 ))" +%Y-%m-%dT%H:%M:%SZ)"

# --- pool state ---------------------------------------------------------------
pool_summary=()
total_idle=0; total_min_idle=0; total_creating=0
prov_fail_names=()
fail_reasons=()
for pool in "${POOLS[@]}"; do
  short="${pool:0:8}"
  pj="$("$GARM_BIN" pool show "$pool" --format json 2>&1)" \
    || fail_push "garm-cli pool show failed for $short"
  # Shape-guard the fields we decide on; anything else-shaped is UNKNOWN,
  # and UNKNOWN is never healthy.
  # `| tostring`, not `// empty`: jq's // treats false as missing, so
  # `.enabled // empty` reads a DISABLED pool as unreadable.
  enabled="$(jq -r '.enabled | tostring' <<<"$pj" 2>/dev/null)" \
    || fail_push "unparseable pool JSON for $short"
  min_idle="$(jq -r 'if has("min_idle_runners") then .min_idle_runners | tostring else "ABSENT" end' <<<"$pj" 2>/dev/null)"
  max_run="$(jq -r '.max_runners // empty' <<<"$pj" 2>/dev/null)"
  provider="$(jq -r '.provider_name // "?"' <<<"$pj" 2>/dev/null)"
  # Live GARM v0.2.1 `pool show` omits BOTH `min_idle_runners` and
  # `instances` when they are zero/empty (params.Pool tags both `omitempty`;
  # measured on both installed pools, min-idle 0 + zero instances). `[]?`
  # already tolerates an absent `instances` array (zero actual instances);
  # absent `min_idle_runners` means min-idle 0. A non-numeric present value
  # still fails closed below — absence is not a license for garbage.
  if [[ "$min_idle" == "ABSENT" ]]; then
    min_idle="0"
  fi
  inst_tsv="$(jq -r '.instances[]? | [(.status // "?"), (.runner_status // "?"), (.name // "?")] | @tsv' <<<"$pj" 2>/dev/null)" \
    || fail_push "unparseable pool instances for $short"
  [[ "$enabled" == "true" || "$enabled" == "false" ]] \
    || fail_push "unparseable pool enabled flag for $short"
  [[ "$min_idle" =~ ^[0-9]+$ && "$max_run" =~ ^[0-9]+$ ]] \
    || fail_push "unparseable pool min/max for $short"
  [[ "$enabled" == "true" ]] \
    || fail_push "pool $short disabled (provider $provider)"

  idle=0; busy=0; stale=0; prov_fail=0; del_pending=0; actual=0
  while IFS=$'\t' read -r st rs nm; do
    [[ -n "$st" ]] || continue
    actual=$((actual + 1))
    if [[ "$st" == "error" || "$rs" == "failed" ]]; then
      prov_fail=$((prov_fail + 1))
      (( ${#prov_fail_names[@]} < 3 )) && prov_fail_names+=("$short/$nm")
    elif [[ "$st" == "pending_delete" || "$st" == "pending_force_delete" || "$st" == "deleting" ]]; then
      stale=$((stale + 1)); del_pending=$((del_pending + 1))
    elif [[ "$st" == "running" && "$rs" == "idle" ]]; then
      idle=$((idle + 1))
    elif [[ "$st" == "running" ]]; then
      busy=$((busy + 1))
    else
      stale=$((stale + 1))
    fi
    case "$st" in
      pending_create|creating) total_creating=$((total_creating + 1)) ;;
    esac
    [[ "$rs" == "installing" ]] && total_creating=$((total_creating + 1))
  done <<<"$inst_tsv"

  (( actual <= 10#$max_run )) \
    || fail_push "pool $short holds $actual instances over max $max_run"
  total_idle=$((total_idle + idle))
  total_min_idle=$((total_min_idle + 10#$min_idle))
  pool_summary+=("$short en max$max_run des$min_idle act$actual idle$idle busy$busy stale$stale prov$prov_fail del$del_pending")
done

# --- eligible demand: oldest waiting job carrying the eligible label ---------
oldest=""
oldest_repo=""
jobs_unreadable=0
for repo in "${REPOS[@]}"; do
  queued_runs="$("$GH_BIN" run list --repo "$repo" --status queued --created "<$cutoff" \
                   --limit "$RUN_LIMIT" --json databaseId,createdAt 2>&1)" \
    || fail_push "gh run list (queued) failed for $repo"
  active_runs="$("$GH_BIN" run list --repo "$repo" --status in_progress --created "<$cutoff" \
                   --limit "$RUN_LIMIT" --json databaseId,createdAt 2>&1)" \
    || fail_push "gh run list (in_progress) failed for $repo"
  runs="$(jq -rs --arg c "$cutoff" --argjson lim "$RUN_LIMIT" '
            if any(.[]; length >= $lim) then error("truncated") else . end
            | add | map(select(.createdAt < $c)) | unique_by(.databaseId)
            | sort_by(.createdAt) | .[] | "\(.createdAt) \(.databaseId)"' \
          <<<"$queued_runs$active_runs" 2>&1)" \
    || { [[ "$runs" == *truncated* ]] \
           && fail_push "run list hit GARM_RUN_LIMIT=$RUN_LIMIT for $repo; oldest runs unseen" \
           || fail_push "unparseable run list for $repo"; }
  while read -r created rid; do
    [[ -n "$rid" ]] || continue
    [[ -n "$oldest" && ! "$created" < "$oldest" ]] && break
    jobs="$("$GH_BIN" api "repos/$repo/actions/runs/$rid/jobs?per_page=100" 2>&1)" \
      || { jobs_unreadable=$((jobs_unreadable + 1)); continue; }
    cand="$(jq -r --arg ignore "$IGNORE_JOB" --arg label "$ELIGIBLE_LABEL" \
      '[.jobs[] | select(.status == "queued" and .name != $ignore
                         and ((.labels // []) | index($label)))
        | .created_at] | sort | first // empty' \
      <<<"$jobs")" \
      || { jobs_unreadable=$((jobs_unreadable + 1)); continue; }
    if [[ -n "$cand" ]] && { [[ -z "$oldest" ]] || [[ "$cand" < "$oldest" ]]; }; then
      oldest="$cand"
      oldest_repo="$repo"
    fi
  done <<<"$runs"
done

if [[ -z "$oldest" ]]; then
  (( jobs_unreadable == 0 )) \
    || fail_push "jobs API unreadable for $jobs_unreadable run(s)"
else
  oldest_s="$(date -d "$oldest" +%s 2>/dev/null)" \
    || fail_push "unparseable timestamp $oldest"
  age_min=$(( (now - oldest_s) / 60 ))
  (( age_min <= 10#$THRESHOLD_MIN )) \
    || fail_push "oldest $ELIGIBLE_LABEL job ${age_min}m old in ${oldest_repo} (threshold ${THRESHOLD_MIN}m)"
  # Waiting past-cutoff demand with broken or missing warm capacity: the pool
  # is not serving its label. Only demand in runs older than the threshold
  # reaches this branch (the run-level --created filter above excludes fresh
  # runs entirely); a below-threshold job in an old run still counts as demand
  # for the warm/prov checks even though it passes the age check.
  (( total_idle >= total_min_idle )) \
    || (( total_creating > 0 )) \
    || fail_push "idle $total_idle < min-idle $total_min_idle with $ELIGIBLE_LABEL demand waiting in ${oldest_repo}"
  (( ${#prov_fail_names[@]} == 0 )) \
    || fail_push "prov-fail instances (${prov_fail_names[*]}) with $ELIGIBLE_LABEL demand waiting in ${oldest_repo}"
fi

# --- ineligible depth snapshot (info only, best-effort, never paging) --------
ineligible=0; depth_unknown=0; depth_capped=0
for repo in "${REPOS[@]}"; do
  for st in queued in_progress; do
    list="$("$GH_BIN" run list --repo "$repo" --status "$st" \
             --limit "$DEPTH_LIMIT" --json databaseId 2>&1)" \
      || { depth_unknown=1; continue; }
    ids="$(jq -r '.[].databaseId' <<<"$list" 2>/dev/null)" \
      || { depth_unknown=1; continue; }
    n=0
    while read -r rid; do
      [[ -n "$rid" ]] || continue
      n=$((n + 1))
      jobs="$("$GH_BIN" api "repos/$repo/actions/runs/$rid/jobs?per_page=100" 2>&1)" \
        || continue
      c="$(jq --arg ignore "$IGNORE_JOB" --arg label "$ELIGIBLE_LABEL" \
        '[.jobs[] | select(.status == "queued" and .name != $ignore
                           and (((.labels // []) | index($label)) | not))] | length' \
        <<<"$jobs" 2>/dev/null)" || continue
      ineligible=$((ineligible + c))
    done <<<"$ids"
    (( n >= 10#$DEPTH_LIMIT )) && depth_capped=1
  done
done
depth_txt="$ineligible"
(( depth_capped == 1 )) && depth_txt="${ineligible}+"
(( depth_unknown == 1 )) && depth_txt="$depth_txt?"

# --- verdict ------------------------------------------------------------------
# Reaching here: pools enabled, readable, within cap; no eligible breach; warm
# commitment met or provisioning or demand-free. Detail rides the cron log —
# green carries no error text (same bare-true pattern as the sibling pushes).
# Broken VMs with no waiting demand stay log info, not a page.
echo "push-garm-pool: [${pool_summary[*]}] prov-fail: ${prov_fail_names[*]:-none}; eligible $ELIGIBLE_LABEL waiting: ${oldest:-none}; ineligible depth ${depth_txt} (info only)"
gatus_push platform_garm-pool true
