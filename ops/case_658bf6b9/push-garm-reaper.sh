#!/usr/bin/env bash
# push-garm-reaper.sh — cron: GARM orphan/stale audit verdict → Gatus
# `platform_garm-reaper`.
#
# Runs the read-only audit (github-runner/garm/audit_stale_instances.sh —
# pool-show reads only, never delete/destroy/remove) in --json mode and
# pushes its verdict. PROPOSE (candidates past TTL, no breach rule met)
# pushes success=true: a proposal is healthy signal, the log carries the
# Operator-card material. BREACH (orphan, stuck provisioning/drain,
# unknown-age, over-max, unreadable pool) pushes success=false. An audit
# that exits non-zero without parseable JSON also pushes false — an audit
# that cannot read is a finding, never a clean bill. Missing GARM_POOLS,
# a missing audit script, or missing jq refuses (exit 2) with zero pushes;
# missing Gatus env refuses the same way inside gatus_push — the 30m
# heartbeat miss then pages, never a silent green.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), GARM_POOLS
# (space-separated FULL pool UUIDs, same IDs as push-garm-pool.sh),
# plus the audit tunables GARM_BIN, GARM_INSTANCE_TTL_MIN,
# GARM_ORPHAN_MULT, GARM_PROV_STUCK_MIN, GARM_DRAIN_STUCK_MIN.
# Test seam: GARM_REAPER_AUDIT overrides the audit script path.
# Cron (operator): */15 * * * * /opt/status-page/current/ops/case_658bf6b9/push-garm-reaper.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

AUDIT="${GARM_REAPER_AUDIT:-$HERE/../../github-runner/garm/audit_stale_instances.sh}"

read -r -a POOLS <<<"${GARM_POOLS:-}"
(( ${#POOLS[@]} > 0 )) \
  || { echo "push-garm-reaper: refusing — GARM_POOLS (space-separated pool IDs) must be set" >&2; exit 2; }
[[ -x "$AUDIT" ]] \
  || { echo "push-garm-reaper: refusing — audit script not executable at $AUDIT" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-garm-reaper: refusing — jq required" >&2; exit 2; }

fail_push() { gatus_push platform_garm-reaper false "$1"; exit 1; }

err_file="$(mktemp "${TMPDIR:-/tmp}/reaper_err.XXXXXXXX")" \
  || { echo "push-garm-reaper: refusing — cannot create a temp file" >&2; exit 2; }
trap 'rm -f "$err_file"' EXIT
out="$("$AUDIT" --json 2>"$err_file")" && arc=$? || arc=$?
# The audit prints JSON on stdout in --json mode on every verdict path;
# anything else-shaped is UNKNOWN and fails closed.
verdict="$(jq -r '.verdict // empty' <<<"$out" 2>/dev/null)"
if [[ -z "$verdict" ]]; then
  fail_push "reaper audit unreadable (exit $arc)"
fi

case "$verdict" in
  OK)
    echo "push-garm-reaper: verdict OK — no actionable instances"
    gatus_push platform_garm-reaper true
    ;;
  PROPOSE)
    n="$(jq -r '.candidates | length' <<<"$out" 2>/dev/null)"
    echo "push-garm-reaper: verdict PROPOSE — $n candidate(s) past TTL, no breach (see cron log for the Operator-card material)"
    gatus_push platform_garm-reaper true
    ;;
  BREACH)
    reasons="$(jq -r '[.breachReasons[]?] | join("; ")' <<<"$out" 2>/dev/null)"
    [[ -n "$reasons" ]] || reasons="breach with no listed reason"
    echo "push-garm-reaper: verdict BREACH — $reasons" >&2
    fail_push "$reasons"
    ;;
  *)
    fail_push "reaper audit verdict '$verdict' unreadable"
    ;;
esac
