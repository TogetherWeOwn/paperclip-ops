#!/usr/bin/env bash
# ===========================================================================
# sweeper_watchdog_check.sh — case_5bc31800 host-cron replacement (case_4ea7d92e).
#
# Converts the "case_5bc31800 sweeper watchdog" agent routine into an unattended
# systemd --user timer. On node7b1ff338 the case_bd594600 session-secret sweep runs as the
# host user unit session-secret-sweeper.{timer,service}; this wrapper inspects
# that unit and posts a card ONLY when there is something to report -- never
# on a clean, healthy tick. See ../INSTALL.md.
#
# What is checked (case_d376bf4e_SWEEPER_UNIT, default session-secret-sweeper):
#   - <unit>.timer   is loaded and active (the sweep is still scheduled);
#   - <unit>.service is loaded, has a recorded last run, that run ended with
#     Result=success and ExecMainStatus=0, and it STARTED no more than
#     case_d376bf4e_SWEEPER_MAX_AGE_SEC ago (default 3600);
#   - SWEEP-ALERT.md / WATCHDOG-GAP.md in the sweeper's ops dir. sweep_daemon.sh
#     raises SWEEP-ALERT.md on runs that still exit 0 (records it could not
#     read, a summary it could not parse), so a green unit is not enough.
#
# READ-ONLY. It never starts, stops or restarts anything and never runs the
# sweeper: a repair on the host is the operator's call, and a watchdog that
# restarts what it watches hides the failure it exists to report. It does not
# read the unit's journal, so no sweeper output reaches the board from here.
#
# Earlier revisions drove ops/case_bd594600/sweep_daemon.sh check/stop/start. That
# is wrong on the host: the daemon's pid file holds a container pid, which
# names nothing (or something else) in the host pid namespace.
#
# The routine's old "step 0" (self-heal a stuck prior EXECUTION ISSUE from a
# 429 storm, case_3da94c50) does not apply here: a host-cron tick creates no
# execution issue at all, so there is nothing of that shape to get stuck.
#
# Exit contract of this wrapper itself: 0 always, once a finding (if any) has
# been posted or a clean run confirmed. Failures to even run the underlying
# checks are themselves findings (never silently "clean").
# ===========================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
# shellcheck source=lib/post_finding.sh
source "${SCRIPT_DIR}/lib/post_finding.sh"

OPS_DIR="${case_d376bf4e_OPS_DIR:-/paperclip/instances/default/companies/${PAPERCLIP_COMPANY_ID}/ops}"
SWEEP_DIR="${OPS_DIR}/case_bd594600"
GAP_FILE="${SWEEP_DIR}/WATCHDOG-GAP.md"
ALERT_FILE="${SWEEP_DIR}/SWEEP-ALERT.md"
UNIT="${case_d376bf4e_SWEEPER_UNIT:-session-secret-sweeper}"
MAX_AGE="${case_d376bf4e_SWEEPER_MAX_AGE_SEC:-3600}"

TAG="[case_d376bf4e-sweeper-watchdog]"
findings=()

# read_props <unit> <property>... — fills PROPS from `systemctl --user show`.
# Returns systemctl's exit code; on failure PROPS[_error] holds its output.
# LC_ALL=C keeps the timestamp format parseable by `date -d`.
declare -A PROPS=()
read_props() {
  local unit="$1"; shift
  local args=() p out line rc=0
  for p in "$@"; do args+=(-p "$p"); done
  PROPS=()
  out="$(LC_ALL=C systemctl --user show "${args[@]}" -- "$unit" 2>&1)" || rc=$?
  if [[ $rc -ne 0 ]]; then
    PROPS[_error]="$out"
    return "$rc"
  fi
  while IFS= read -r line; do
    [[ "$line" == *=* ]] && PROPS["${line%%=*}"]="${line#*=}"
  done <<<"$out"
  return 0
}

if [[ ! "$MAX_AGE" =~ ^[0-9]+$ ]]; then
  findings+=("case_d376bf4e_SWEEPER_MAX_AGE_SEC='${MAX_AGE}' is not a number of seconds -- freshness of ${UNIT}.service NOT measured")
  MAX_AGE=""
fi

if ! command -v systemctl >/dev/null 2>&1; then
  findings+=("systemctl not found on PATH -- ${UNIT}.timer/.service could NOT be inspected; INDETERMINATE, not clean")
else
  rc=0
  read_props "${UNIT}.timer" LoadState ActiveState || rc=$?
  if [[ $rc -ne 0 ]]; then
    findings+=("systemctl --user show ${UNIT}.timer FAILED (exit ${rc}): ${PROPS[_error]:-} -- INDETERMINATE, not clean")
  elif [[ "${PROPS[LoadState]:-}" != "loaded" ]]; then
    findings+=("${UNIT}.timer is not loaded (LoadState=${PROPS[LoadState]:-?}) -- the sweep is NOT scheduled")
  elif [[ "${PROPS[ActiveState]:-}" != "active" ]]; then
    findings+=("${UNIT}.timer is not active (ActiveState=${PROPS[ActiveState]:-?}) -- the sweep is NOT scheduled")
  fi

  rc=0
  read_props "${UNIT}.service" LoadState ActiveState Result ExecMainStatus ExecMainStartTimestamp || rc=$?
  if [[ $rc -ne 0 ]]; then
    findings+=("systemctl --user show ${UNIT}.service FAILED (exit ${rc}): ${PROPS[_error]:-} -- INDETERMINATE, not clean")
  elif [[ "${PROPS[LoadState]:-}" != "loaded" ]]; then
    findings+=("${UNIT}.service is not loaded (LoadState=${PROPS[LoadState]:-?}) -- nothing is sweeping")
  else
    started="${PROPS[ExecMainStartTimestamp]:-}"
    if [[ -z "$started" || "$started" == "n/a" ]]; then
      findings+=("${UNIT}.service has no recorded run since the user manager started -- last sweep UNKNOWN, not clean")
    else
      result="${PROPS[Result]:-}"
      status="${PROPS[ExecMainStatus]:-}"
      if [[ "$result" != "success" || "$status" != "0" ]]; then
        findings+=("${UNIT}.service last run FAILED (Result=${result:-?} ExecMainStatus=${status:-?} ActiveState=${PROPS[ActiveState]:-?}, started ${started}) -- inspect: journalctl --user -u ${UNIT}.service -n 50")
      fi
      started_epoch=""
      started_epoch="$(LC_ALL=C date -d "$started" +%s 2>/dev/null)" || started_epoch=""
      if [[ ! "$started_epoch" =~ ^[0-9]+$ ]]; then
        findings+=("could not parse ${UNIT}.service ExecMainStartTimestamp='${started}' -- freshness NOT measured")
      elif [[ -n "$MAX_AGE" ]]; then
        age=$(( $(date +%s) - started_epoch ))
        if (( age > MAX_AGE )); then
          findings+=("${UNIT}.service is STALE: last run started ${age}s ago (${started}), budget ${MAX_AGE}s -- the timer is not firing or runs are hanging")
        fi
      fi
    fi
  fi
fi

# Name the alert and its headline only; the file itself stays on the host.
# A missing or unreadable alert dir is itself a finding: without it a
# SWEEP-ALERT.md the sweeper wrote would never be seen, and a healthy unit
# would exit 0 over an unwatched sweep (fail-open, case_3cd9412c B1).
if [[ ! -d "$SWEEP_DIR" ]]; then
  findings+=("sweeper alert dir ${SWEEP_DIR} (case_bd594600 under case_d376bf4e_OPS_DIR) is missing or not a directory -- SWEEP-ALERT.md/WATCHDOG-GAP.md could NOT be checked; INDETERMINATE, not clean")
elif [[ ! -r "$SWEEP_DIR" || ! -x "$SWEEP_DIR" ]]; then
  findings+=("sweeper alert dir ${SWEEP_DIR} is not readable/searchable -- SWEEP-ALERT.md/WATCHDOG-GAP.md could NOT be checked; INDETERMINATE, not clean")
else
  if [[ -f "$GAP_FILE" ]]; then
    findings+=("WATCHDOG-GAP.md present at ${GAP_FILE}: $(head -n 1 "$GAP_FILE" 2>/dev/null || echo '(unreadable)')")
  fi

  if [[ -f "$ALERT_FILE" ]]; then
    reason="$(grep -m1 '^\*\*' "$ALERT_FILE" 2>/dev/null || true)"
    findings+=("SWEEP-ALERT.md present at ${ALERT_FILE} -- escalate to CISO: ${reason:-$(head -n 1 "$ALERT_FILE" 2>/dev/null || echo '(unreadable)')}")
  fi
fi

if [[ ${#findings[@]} -eq 0 ]]; then
  exit 0
fi

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
{
  echo "case_5bc31800 sweeper watchdog (host-cron, case_4ea7d92e) reported ${#findings[@]} finding(s):"
  echo
  for f in "${findings[@]}"; do
    printf -- '- %s\n' "$f"
  done
  echo
  echo "Read-only check: nothing was restarted. Never a secret value here -- unit state and file names only."
} > "$body_file"

post_finding "$TAG" "sweeper watchdog finding" "$body_file"
exit 0
