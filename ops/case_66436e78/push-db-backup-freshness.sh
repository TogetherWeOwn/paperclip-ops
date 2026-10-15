#!/usr/bin/env bash
# push-db-backup-freshness.sh — cron: Paperclip DB backup freshness → Gatus.
#
# case_66436e78 item 4 (backup-freshness invariant; coverage-matrix gap #1:
# case_c05e4728 names it the one invariant view with no owner — case_654dd2ec owns
# the other six). Source-only: no host deploy rides this change; the
# operator installs the cron line separately. Sibling leaves own the six
# SQL invariant views (case_654dd2ec, ops/case_654dd2ec) and every other Gatus
# module; this leaf is the backup-freshness endpoint only.
#
# What it does, once per run (cron: */5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env; ...):
#   1. Runs the reviewed db_backup_stall.sh detector in --staleness-only
#      mode: is there ANY recent usable archive (any producer, at least
#      MIN_BACKUP_BYTES, within MAX_BACKUP_AGE_HOURS). The detector is
#      read-only: it stats the backup directory; it never writes a backup,
#      never deletes one, never restarts anything.
#   2. Pushes one Gatus external endpoint (`paperclip_db-backup-freshness`,
#      success=true|false) via the shared _gatus_push.sh helper, so the
#      existing Gatus custom provider carries backup pages into Akmatori.
#
# Availability-only on purpose (--staleness-only, --no-health): the
# in-server scheduler wedge is case_703527a6's standing clock and the host
# fallback writer already lands in the same directory, so this endpoint
# answers "is there a recent restorable backup at all" regardless of which
# writer produced it. No bearer is sent anywhere; the only credential in
# play is the Gatus push token, which travels in a 0600 curl config.
#
# FAIL CLOSED: detector exit 1 (stale/truncated/absent) and exit 2
# (inconclusive/unmeasurable) both push success=false. A probe that cannot
# read must page, never report healthy.
#
# Cron (operator): */5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env; /opt/ops/paperclip-ops/ops/case_66436e78/push-db-backup-freshness.sh
# (5-minute pushes against the 10-minute heartbeat leave one missed run of
# slack; set -a exports GATUS_URL and GATUS_EXTERNAL_TOKEN from the env file,
# which has no export lines.)
#
# Env:
#   GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh),
#   PAPERCLIP_DB_BACKUP_DIR (backup dir; detector default when unset),
#   MAX_BACKUP_AGE_HOURS (default 2, same as the detector),
#   DB_BACKUP_STALL_BIN (detector path seam; default ../../db_backup_stall.sh)
# Exit: 0 fresh and pushed; 1 stale/inconclusive (pushed false); 2 refusal
# (bad env, missing tooling) before any health claim.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-db-backup-freshness: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
. "$GATUS_HELPER"

ENDPOINT_KEY="paperclip_db-backup-freshness"
DETECTOR="${DB_BACKUP_STALL_BIN:-$HERE/../../db_backup_stall.sh}"
MAX_AGE="${MAX_BACKUP_AGE_HOURS:-2}"

[[ "$MAX_AGE" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-db-backup-freshness: refusing — MAX_BACKUP_AGE_HOURS must be a positive integer" >&2; exit 2; }
[[ -f "$DETECTOR" ]] \
  || { echo "push-db-backup-freshness: refusing — detector $DETECTOR not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-db-backup-freshness: refusing — python3 required" >&2; exit 2; }
command -v curl >/dev/null 2>&1 \
  || { echo "push-db-backup-freshness: refusing — curl required" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-db-backup-freshness: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-db-backup-freshness: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }

extra=()
[[ -n "${PAPERCLIP_DB_BACKUP_DIR:-}" ]] && extra+=(--backup-dir "$PAPERCLIP_DB_BACKUP_DIR")

rc=0
out="$(bash "$DETECTOR" --staleness-only --no-health --json \
  --max-backup-age-hours "$MAX_AGE" "${extra[@]}" 2>&1)" || rc=$?

parsed="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("PARSE_FAIL")
    sys.exit(0)
print(d.get("verdict", ""))
print(d.get("newestUsableBackup", "none"))
print(d.get("newestUsableBackupAgeMinutes", -1))
rs = d.get("reasons") or []
print(rs[0] if rs else "")
print(d.get("exitCode", -1))
' "$out")"

if [[ "$parsed" == "PARSE_FAIL"* ]]; then
  gatus_push "$ENDPOINT_KEY" false "inconclusive: detector output unparseable" || true
  echo "push-db-backup-freshness: detector output unparseable" >&2
  exit 1
fi

verdict="$(sed -n '1p' <<<"$parsed")"
bname="$(sed -n '2p' <<<"$parsed")"
bage="$(sed -n '3p' <<<"$parsed")"
reason0="$(sed -n '4p' <<<"$parsed")"

if (( rc == 0 )); then
  gatus_push "$ENDPOINT_KEY" true || exit 1
  echo "push-db-backup-freshness: fresh ($bname, ${bage}min old)"
else
  if (( rc == 2 )); then
    err="inconclusive: backup directory could not be measured"
  else
    err="${verdict}: ${bname} ${bage}min old"
    [[ -n "$reason0" ]] && err="$err — $reason0"
  fi
  gatus_push "$ENDPOINT_KEY" false "$err" || true
  echo "push-db-backup-freshness: $err" >&2
  exit 1
fi
