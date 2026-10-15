#!/usr/bin/env bash
# push-fleet-run-stall.sh — cron: fleet run-stall probe → Gatus
# (`paperclip_fleet-run-stall`). case_b79f6544.
#
# Runs fleet_run_stall.sql once through a read-only session and pushes ONE
# boolean. The SQL returns zero rows while the fleet is dispatching and one
# row when no heartbeat_runs row has been created or started for 20 minutes
# while an assigned todo/in_progress card waits. Nothing here needs an agent
# to run, which is the point: on 2026-10-08 no run was created or started for
# about three hours while the control plane was up, so a watchdog that needs
# an agent to run could not report it.
#
# PUSH MAPPING: zero rows → success=true. One row → success=false with the
# gap, the last run activity and the waiting-card count in the error text, so
# the page says HOW long and HOW much is waiting.
#
# FAIL CLOSED: a missing psql, an unreadable database, an unexpected row
# shape or more than one row pushes success=false and exits 1; a probe that
# cannot read must page, never report healthy. Missing env refuses (exit 2)
# before touching the network, and the 10 minute Gatus heartbeat then pages
# on a silent cron.
#
# READ-ONLY / NO REMEDIATION: the session sets default_transaction_read_only
# through PGOPTIONS and the only statement is the checked-in SELECT. The
# script never restarts, wakes, patches or admits anything.
#
# NON-DISCLOSURE: PAPERCLIP_RO_DB_URL reaches libpq as PG* environment
# variables (lib/pcsql.sh), never on argv, where /proc/<pid>/cmdline is
# world-readable on the shared host. In container-local mode
# PAPERCLIP_MONITOR_RO_PASSWORD is mapped to PGPASSWORD process-locally for
# the single `docker exec -e` call the same way, never on argv. Never store a
# bare PGPASSWORD in the shared env file (see OPERATOR.md).
# The Gatus Bearer travels in a 0600 curl config (ops/case_658bf6b9/_gatus_push.sh).
# This script never prints any of these values, only their names.
#
# Env (one DB mode is mandatory):
#   FLEET_RUN_STALL_DB_MODE=url|docker-ro (explicit; wins when set).
#   Unset: docker-ro is preferred whenever its probe-specific vars are present
#     so the shared PAPERCLIP_RO_DB_URL other probes need can stay.
#   TCP mode (url): PAPERCLIP_RO_DB_URL (read-only role URL),
#   container-local mode (docker-ro; case_d8c8a10f: host TCP auth fails but
#     `docker exec` as monitor_ro works; same authorized boundary as op-db-read):
#     PAPERCLIP_RO_DB_USER (must be monitor_ro),
#     PAPERCLIP_MONITOR_RO_PASSWORD (monitor_ro password; mapped to PGPASSWORD
#       process-locally for the one docker exec call only — never a bare
#       PGPASSWORD in the shared env file),
#     PAPERCLIP_RO_DB_CTR (default paperclip-db),
#     PAPERCLIP_RO_DB_NAME (default paperclip).
#   Always: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh),
#      GATUS_PUSH_TIMEOUT (seconds, default 10).
# Cron (operator, separate Operator card after merge; see OPERATOR.md):
#   */5 * * * * root set -a; . /root/.config/paperclip/case_60330d6f.env && exec /opt/ops/paperclip-ops/ops/case_b79f6544/push-fleet-run-stall.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
. "$HERE/../case_658bf6b9/_gatus_push.sh"
# shellcheck source=../../lib/pcsql.sh
. "$HERE/../../lib/pcsql.sh" \
  || { echo "push-fleet-run-stall: refusing — lib/pcsql.sh missing" >&2; exit 2; }

KEY="paperclip_fleet-run-stall"
SQL_FILE="$HERE/fleet_run_stall.sql"

need() {
  [[ -n "${!1:-}" ]] || { echo "push-fleet-run-stall: refusing — $1 is unset" >&2; return 1; }
}
need GATUS_URL || exit 2
need GATUS_EXTERNAL_TOKEN || exit 2
[[ -f "$SQL_FILE" ]] \
  || { echo "push-fleet-run-stall: refusing — $SQL_FILE missing" >&2; exit 2; }

fail_push() {  # $1 = reason — fail-closed: DOWN push, exit 1
  echo "push-fleet-run-stall: $1" >&2
  gatus_push "$KEY" false "fleet-run-stall probe could not read: $1" || true
  exit 1
}

# DB transport is explicit, never sniffed from the shared URL (lib/pcsql.sh
# rule). FLEET_RUN_STALL_DB_MODE wins when set; when unset, docker-ro is
# preferred whenever its probe-specific vars are present so the shared
# PAPERCLIP_RO_DB_URL that other probes need can stay. Container-local mode is
# the same authorized boundary as host op-db-read: `docker exec` into the DB
# container as explicit monitor_ro with a read-only session. No other user
# is accepted here or in lib/pcsql.sh; the password travels by environment
# only and this script never prints it.
MODE="${FLEET_RUN_STALL_DB_MODE:-}"
if [[ -z "$MODE" ]]; then
  if [[ -n "${PAPERCLIP_RO_DB_USER:-}${PAPERCLIP_MONITOR_RO_PASSWORD:-}" ]]; then
    MODE="docker-ro"
  elif [[ -n "${PAPERCLIP_RO_DB_URL:-}" ]]; then
    MODE="url"
  fi
fi
case "$MODE" in
  url)
    need PAPERCLIP_RO_DB_URL || exit 2
    command -v psql >/dev/null 2>&1 \
      || { echo "push-fleet-run-stall: refusing — psql not found" >&2; exit 2; }
    # libpq reads the connection from PG* variables derived from the URL.
    export PAPERCLIP_SQL_BACKEND=psql
    export DATABASE_URL="$PAPERCLIP_RO_DB_URL"
    export PGOPTIONS='-c default_transaction_read_only=on'
    ;;
  docker-ro)
    need PAPERCLIP_RO_DB_USER || exit 2
    [[ "${PAPERCLIP_RO_DB_USER}" == "monitor_ro" ]] \
      || { echo "push-fleet-run-stall: refusing — PAPERCLIP_RO_DB_USER must be monitor_ro" >&2; exit 2; }
    need PAPERCLIP_MONITOR_RO_PASSWORD || exit 2
    command -v docker >/dev/null 2>&1 \
      || { echo "push-fleet-run-stall: refusing — docker not found" >&2; exit 2; }
    export PAPERCLIP_SQL_BACKEND=docker-ro
    export PGOPTIONS='-c default_transaction_read_only=on'
    # Map the probe-specific password to the name the docker-ro backend reads,
    # process-locally only; never store a bare PGPASSWORD in the shared file.
    export PGPASSWORD="$PAPERCLIP_MONITOR_RO_PASSWORD"
    ;;
  *)
    if [[ -n "${FLEET_RUN_STALL_DB_MODE:-}" ]]; then
      echo "push-fleet-run-stall: refusing — FLEET_RUN_STALL_DB_MODE must be url or docker-ro" >&2
    else
      echo "push-fleet-run-stall: refusing — no DB credential (set FLEET_RUN_STALL_DB_MODE=url with PAPERCLIP_RO_DB_URL, or docker-ro with PAPERCLIP_RO_DB_USER/PAPERCLIP_MONITOR_RO_PASSWORD)" >&2
    fi
    exit 2
    ;;
esac

# stderr is left alone on purpose: psql diagnostics land in the cron log and
# can never be mistaken for a result row.
out="$(pcsql_run -tA -F '|' -v ON_ERROR_STOP=1 < "$SQL_FILE")" \
  || fail_push "database read failed"

# A trailing newline is not a row. Anything else is.
rows="$(printf '%s' "$out" | grep -c . || true)"
if (( rows == 0 )); then
  echo "push-fleet-run-stall: fleet is dispatching (no stall row)"
  gatus_push "$KEY" true || exit 1
  exit 0
fi
(( rows == 1 )) || fail_push "expected at most one row, got $rows"

IFS='|' read -r waiting last_at stalled_min extra <<<"$out"
[[ -z "${extra:-}" ]] || fail_push "unexpected extra column in the stall row"
[[ "$waiting" =~ ^[0-9]+$ ]] || fail_push "waiting_cards is not a number"
if [[ -n "$stalled_min" && ! "$stalled_min" =~ ^[0-9]+$ ]]; then
  fail_push "stalled_min is not a number"
fi
# last_at is a Postgres timestamp; keep it to the characters one can hold.
ts_re='^[0-9: .+-]+$'
if [[ -n "$last_at" && ! "$last_at" =~ $ts_re ]]; then
  fail_push "last_run_activity_at is not a timestamp"
fi

if [[ -z "$last_at" ]]; then
  msg="no heartbeat_runs row exists; ${waiting} assigned todo/in_progress cards waiting"
else
  msg="no heartbeat_runs row created or started for ${stalled_min}m (last ${last_at}); ${waiting} assigned todo/in_progress cards waiting"
fi
echo "push-fleet-run-stall: STALLED: $msg"
gatus_push "$KEY" false "$msg" || true
exit 1  # a stall is an incident: cron must report it
