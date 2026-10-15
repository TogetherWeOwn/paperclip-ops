#!/usr/bin/env bash
# tmp-disk-alarm.sh — cron: controller root-disk (/tmp) fill → Gatus, 80% warn / 90% page.
#
# case_0ddff3ec (slice of the case_66436e78 monitoring coverage). Source-only: the
# operator installs the timer (docs/runbooks/controller-tmp-disk.md); no host deploy rides
# this change.
#
# Why this exists: on 2026-10-04 the controller container's root overlay (which
# holds /tmp and sits on the host root disk) reached 100% for at least 80 s. 13
# runs across five agents died with ENOSPC, Web Engineer among them. Beszel's host
# alert watches disk used/total at 85%; the number that stops an unprivileged
# writer is used/(used+avail), because ext4 keeps a reserved share only root can
# use. `df` prints that one, so this alarm computes it the same way. Beszel
# stays the host-wide net; this is the in-container sensor with the 80/90 tiers.
#
# What it does, once per run (cron: */5 * * * *, see the timer):
#   1. Reads one `df -P -B1` table from $TMPDISK_SOURCE_CMD (default
#      `df -P -B1 /tmp`; on the host the operator sets it to run inside the
#      Paperclip container, e.g. `docker exec -u node <ctr> df -P -B1 /tmp`).
#   2. Computes pct = used / (used + avail) * 100.
#   2b. Compares used bytes against the previous run's reading kept in
#      $TMPDISK_STATE_FILE: a jump of TMPDISK_GROWTH_WARN_BYTES (default 5 GiB,
#      about 5% of the ~99 GB usable seen 2026-10-04) or more between two
#      consecutive samples also trips the warn endpoint, even when the absolute
#      level is still below TMPDISK_WARN_PCT, and holds warn false for three runs
#      (the Gatus failure threshold) so the Discord alert fires. A missing, corrupt or stale
#      (> TMPDISK_GROWTH_MAX_AGE_SEC, default 1800 s) previous reading skips
#      the growth check; the absolute tiers still apply. case_76fe8329: the
#      2026-10-04 spike lasted ~80 s inside a 5-minute sample gap, so level
#      tiers alone cannot see a fast ramp — the growth check catches the ramp
#      a spike leaves behind at the next sample.
#   3. Pushes two Gatus external endpoints through the shared _gatus_push.sh
#      helper (key is <group>_<name>, Gatus v5.37.0 config/key/key.go):
#        controller_tmp-disk-warn   success=false when pct >= TMPDISK_WARN_PCT (80)
#        controller_tmp-disk        success=false when pct >= TMPDISK_CRIT_PCT (90)
#      The warn endpoint alerts Discord only; the page endpoint alerts Discord and
#      Akmatori (--emit-gatus-yaml prints both stanzas).
#   4. On a breach, appends the six largest lines of $TMPDISK_TOP_CMD (optional;
#      `<kb><TAB><path>` lines, sorted here; e.g. `docker exec -u node <ctr> du -x
#      -d1 -k /tmp`, whose first line is the /tmp total) to the pushed reason, so
#      the page names what holds the bytes. No inner quotes: it travels through a
#      systemd EnvironmentFile, which strips them.
#
# FAIL CLOSED: an unreadable, empty, malformed or zero-size reading pushes the
# page endpoint false ("sensor measured nothing"); it never reads healthy.
#
# Exit: 0 reading taken and pushed (a breach is carried by Gatus, not by this
#       exit status, so a warn does not also turn the systemd unit red and
#       double-page through the failed-units check); 1 sensor unreadable or a
#       push failed; 2 refusal (bad env, missing credentials) before any claim.
#   tmp-disk-alarm.sh [--dry-run] [--emit-gatus-yaml]
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh, 0600 curl config,
#      never argv), TMPDISK_SOURCE_CMD, TMPDISK_TOP_CMD (optional),
#      TMPDISK_WARN_PCT=80, TMPDISK_CRIT_PCT=90,
#      TMPDISK_STATE_FILE (default ~/.cache/paperclip/tmp-disk-alarm.prev),
#      TMPDISK_GROWTH_WARN_BYTES=5368709120
#        (5 GiB), TMPDISK_GROWTH_MAX_AGE_SEC=1800. TMPDISK_NOW (epoch seconds)
#      is a test seam only; production uses date +%s.
# OWNER devops. RUNBOOK docs/runbooks/controller-tmp-disk.md.
# UNTRUSTED INPUT: the top-consumers output names agent-made directories; it is
# reduced to printable ASCII and bounded before it reaches a push.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "tmp-disk-alarm: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
# shellcheck source=../case_658bf6b9/_gatus_push.sh
. "$GATUS_HELPER"

DRY=0; EMIT=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --emit-gatus-yaml) EMIT=1 ;;
    -h|--help) sed -n '2,61p' "$0"; exit 0 ;;
    *) echo "tmp-disk-alarm: refusing — unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

WARN_PCT="${TMPDISK_WARN_PCT:-80}"
CRIT_PCT="${TMPDISK_CRIT_PCT:-90}"
GROWTH_WARN_BYTES="${TMPDISK_GROWTH_WARN_BYTES:-5368709120}"
GROWTH_MAX_AGE_SEC="${TMPDISK_GROWTH_MAX_AGE_SEC:-1800}"
# Gatus alerts after this many failing pushes in a row; a growth jump holds warn false that long.
ALERT_FAILURE_THRESHOLD=3
GROWTH_HOLD_PUSHES=$ALERT_FAILURE_THRESHOLD
STATE_FILE="${TMPDISK_STATE_FILE:-${HOME:-/tmp}/.cache/paperclip/tmp-disk-alarm.prev}"
for v in "$WARN_PCT" "$CRIT_PCT"; do
  [[ "$v" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
    || { echo "tmp-disk-alarm: refusing — thresholds must be numeric" >&2; exit 2; }
done
[[ "$GROWTH_WARN_BYTES" =~ ^[0-9]+$ ]] && (( GROWTH_WARN_BYTES > 0 )) \
  || { echo "tmp-disk-alarm: refusing — TMPDISK_GROWTH_WARN_BYTES must be a positive integer (got $GROWTH_WARN_BYTES)" >&2; exit 2; }
[[ "$GROWTH_MAX_AGE_SEC" =~ ^[0-9]+$ ]] && (( GROWTH_MAX_AGE_SEC > 0 )) \
  || { echo "tmp-disk-alarm: refusing — TMPDISK_GROWTH_MAX_AGE_SEC must be a positive integer (got $GROWTH_MAX_AGE_SEC)" >&2; exit 2; }
python3 -c 'import sys; w, c = map(float, sys.argv[1:]); sys.exit(0 if 0 < w < c <= 100 else 1)' "$WARN_PCT" "$CRIT_PCT" \
  || { echo "tmp-disk-alarm: refusing — need 0 < warn < crit <= 100 (got $WARN_PCT, $CRIT_PCT)" >&2; exit 2; }

WARN_KEY="controller_tmp-disk-warn"
CRIT_KEY="controller_tmp-disk"

if (( EMIT )); then
  for tier in "tmp-disk-warn|$WARN_PCT|discord" "tmp-disk|$CRIT_PCT|discord custom"; do
    name="${tier%%|*}"; rest="${tier#*|}"; pct="${rest%%|*}"; types="${rest#*|}"
    trigger="at or above ${pct}% used/(used+avail)"
    [[ "$name" == tmp-disk-warn ]] && trigger="$trigger, or fast growth between samples"
    printf '  - name: %s\n    group: controller\n    token: "${GATUS_EXTERNAL_TOKEN}"\n    heartbeat:\n      interval: 10m\n    alerts:\n' "$name"
    for t in $types; do
      printf '      - type: %s\n        failure-threshold: %s\n        description: "Controller root disk (/tmp) %s. THRESHOLD warn %s%% page %s%%. OWNER devops. RUNBOOK docs/runbooks/controller-tmp-disk.md"\n' \
        "$t" "$ALERT_FAILURE_THRESHOLD" "$trigger" "$WARN_PCT" "$CRIT_PCT"
    done
  done
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "tmp-disk-alarm: refusing — python3 required" >&2; exit 2; }
if (( ! DRY )); then
  [[ -n "${GATUS_URL:-}" ]] || { echo "tmp-disk-alarm: refusing — GATUS_URL is unset" >&2; exit 2; }
  [[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] || { echo "tmp-disk-alarm: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
fi

# --- read the sensor --------------------------------------------------------
SOURCE_CMD="${TMPDISK_SOURCE_CMD:-df -P -B1 /tmp}"
TABLE="$(timeout 60 bash -c "$SOURCE_CMD" 2>/dev/null)" && src_ok=1 || src_ok=0

# Prints "<pct> <used> <avail>" for a readable table; prints nothing and exits 1
# otherwise. POSIX df columns: Filesystem 1-blocks Used Available Capacity Mounted.
reading="$(python3 - "$TABLE" <<'PY'
import sys
rows = [l.split() for l in sys.argv[1].splitlines() if l.strip()]
for f in reversed(rows):
    if len(f) >= 6 and f[1].isdigit() and f[2].isdigit() and f[3].isdigit():
        used, avail = int(f[2]), int(f[3])
        if used + avail > 0:
            print("%.2f %d %d" % (100.0 * used / (used + avail), used, avail)); sys.exit(0)
sys.exit(1)
PY
)" && read_ok=1 || read_ok=0

overall=0
if (( ! src_ok || ! read_ok )); then
  msg="controller /tmp disk sensor measured nothing (source command failed or table unreadable): fail-closed"
  echo "tmp-disk-alarm: $msg" >&2
  if (( DRY )); then echo "dry-run: would push $CRIT_KEY false"; exit 1; fi
  gatus_push "$CRIT_KEY" false "$msg" || true
  exit 1
fi

read -r PCT USED AVAIL <<<"$reading"
NOW="${TMPDISK_NOW:-$(date +%s)}"
[[ "$NOW" =~ ^[0-9]+$ ]] \
  || { echo "tmp-disk-alarm: refusing — TMPDISK_NOW must be epoch seconds (got $NOW)" >&2; exit 2; }
gb() { python3 -c 'import sys; print("%.1f" % (int(sys.argv[1]) / 1e9))' "$1"; }
breach() { python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)' "$PCT" "$1"; }

# --- growth between consecutive samples (case_76fe8329) --------------------------
# A fast spike can start and end inside one 5-minute sample gap (2026-10-04:
# ~80 s). The level tiers above then both read healthy at the next sample.
# A jump of GROWTH_WARN_BYTES or more in used bytes since the previous good
# sample trips the warn endpoint even below the level threshold. The state is
# two integers, "<epoch> <used-bytes>"; a missing, malformed, future-dated or
# older-than-GROWTH_MAX_AGE_SEC file skips the growth check (a rebooted host
# or a timer gap must not warn on stale history), and the current good reading
# is always persisted for the next run. Writes go through a temp file plus
# rename so a killed run never leaves a half-written state.
growth_warn=0; growth_delta=0; growth_note=""; growth_hold=0
prev_raw=""
if [[ -f "$STATE_FILE" ]]; then
  prev_raw="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi
if [[ "$prev_raw" =~ ^[0-9]+\ [0-9]+(\ [0-9]+)?$ ]]; then
  read -r PREV_TS PREV_USED PREV_HOLD <<<"$prev_raw"
  age=$((NOW - PREV_TS))
  if (( age >= 0 && age <= GROWTH_MAX_AGE_SEC )); then
    growth_delta=$((USED - PREV_USED))
    if (( growth_delta >= GROWTH_WARN_BYTES )); then
      growth_hold=$GROWTH_HOLD_PUSHES
      growth_note="growth +$(gb "$growth_delta") GB since previous sample ${age}s ago"
    elif (( PREV_HOLD > 0 )); then
      growth_hold=$(( PREV_HOLD < GROWTH_HOLD_PUSHES ? PREV_HOLD : GROWTH_HOLD_PUSHES ))
      growth_note="fast growth from an earlier sample, still held"
    fi
    if (( growth_hold > 0 )); then growth_warn=1; fi
  fi
fi
# Persist the current good reading for the next run. A state dir that cannot
# be created is not fatal: the level tiers still push, only growth is skipped
# next run. Never fail the alarm because history could not be kept. --dry-run
# stays side-effect free and never touches the state file.
if (( ! DRY )); then
  if mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null; then
    tmp_state="$(umask 077; mktemp "$(dirname "$STATE_FILE")/.prev.XXXXXXXX" 2>/dev/null || true)"
    if [[ -n "${tmp_state:-}" ]]; then
      printf '%s %s %s\n' "$NOW" "$USED" "$(( growth_hold > 1 ? growth_hold - 1 : 0 ))" > "$tmp_state" 2>/dev/null \
        && mv -f "$tmp_state" "$STATE_FILE" 2>/dev/null \
        || rm -f "$tmp_state" 2>/dev/null || true
    fi
  fi
fi

top=""
if { breach "$WARN_PCT" || (( growth_warn == 1 )); } && [[ -n "${TMPDISK_TOP_CMD:-}" ]]; then
  # The producer (e.g. du over a busy /tmp) routinely exits non-zero after
  # printing rows — entries vanish mid-walk — so capture its output first and
  # tolerate its exit; only then sort and bound. A `producer | ... || top=""`
  # shape would discard the rows exactly when they are needed.
  top_raw="$(timeout 60 bash -c "$TMPDISK_TOP_CMD" 2>/dev/null || true)"
  if [[ -n "$top_raw" ]]; then
    top="$(printf '%s' "$top_raw" | sort -rn | head -n 6 | tr -c '[:print:]\n' '?' | tr '\n' ';' | cut -c1-300)"
  fi
fi
detail="used ${PCT}% (${AVAIL} bytes = $(gb "$AVAIL") GB free of $(gb "$((USED + AVAIL))") GB usable)"
[[ -z "$top" ]] || detail="$detail; top: $top"
[[ "$growth_warn" == 1 ]] && detail="$detail; $growth_note (threshold +$(gb "$GROWTH_WARN_BYTES") GB)"

echo "tmp-disk-alarm: $detail warn>=${WARN_PCT}% page>=${CRIT_PCT}% growth>=$(gb "$GROWTH_WARN_BYTES")GB"

push_tier() {  # <key> <threshold> <label>
  local key="$1" thr="$2" label="$3"
  if breach "$thr"; then
    echo "  $key: $label BREACH"
    (( DRY )) && return 0
    gatus_push "$key" false "controller /tmp disk $label at or above ${thr}%: $detail" || overall=1
  else
    echo "  $key: ok"
    (( DRY )) && return 0
    gatus_push "$key" true || overall=1
  fi
}
# Growth trips warn even below the level threshold (one push per endpoint per
# run: the warn reason names the growth). Growth never pages on its own: a
# page still needs the absolute tier.
if breach "$WARN_PCT"; then
  push_tier "$WARN_KEY" "$WARN_PCT" warn
elif (( growth_warn == 1 )); then
  echo "  $WARN_KEY: growth BREACH ($growth_note)"
  if (( ! DRY )); then
    gatus_push "$WARN_KEY" false "controller /tmp disk warn: fast growth between samples: $detail" || overall=1
  fi
else
  push_tier "$WARN_KEY" "$WARN_PCT" warn
fi
push_tier "$CRIT_KEY" "$CRIT_PCT" page
exit "$overall"
