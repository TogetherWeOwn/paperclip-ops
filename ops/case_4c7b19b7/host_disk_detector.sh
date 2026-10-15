#!/usr/bin/env bash
# host_disk_detector.sh — D8 host-disk detector (case_4c7b19b7, slice of case_311c4d5b).
#
# THE OUTAGE THIS EXISTS FOR. On 2026-10-03 the Coolify CI host reached 99%
# disk with production apps on it, and nothing noticed for hours. This
# detector measures local filesystems and proposes. It never writes to the
# board, never touches the network, never reads credentials.
#
# SEQUENCING. This detector lands AFTER platform-watchdog phase 1 (#535) and
# touches none of its files: no watchdog/ directory, no detectors.py edit.
# #535's host_health stays the board-routing half; this is the per-host
# measuring instrument plus the Gatus count metric (via push-host-disk.sh,
# which consumes this script's stdout).
#
# THRESHOLDS. Warn at HOST_DISK_WARN_PCT (default 85), page at
# HOST_DISK_PAGE_PCT (default 95) — the same numbers #535 routes on, so the
# instrument and the router agree. Warn routes to DevOps; page escalates to
# the CEO desk (hourly routine), because a full disk on a host carrying
# production apps is a judgement item, not a silent ticket.
#
# FAIL-CLOSED READING RULES (each learned from a sibling detector's scar).
# A missing, unreadable or unparseable input is UNKNOWN (exit 2), never
# clean: an unread source reporting "clean" is how the 99% sat unnoticed.
# Column shifts fail the run rather than misattribute a number to the wrong
# filesystem. An all-excluded or empty read is a refusal, not a clean bill.
#
# OUTPUT. JSONL findings in the recovery-writer schema (phase-1-read-only)
# plus one summary row carrying breachCount/warnCount/pageCount. Findings
# only: there is no board card for a disk breach, and the writer refuses
# proposals without an issue id, so emitting any would only produce refusal
# noise. Phase 2 adds operator_decision proposals once an incident card
# exists for the breach.
#
# EXIT. 0 clean · 1 breach · 2 refusal.
#
# ENV SEAMS (tests and the drill drive these; production sets thresholds only):
#   HOST_DISK_WARN_PCT / HOST_DISK_PAGE_PCT — whole percent, warn < page
#   HOST_DISK_EXCLUDE_TYPES — fs types skipped
#                             (default "tmpfs devtmpfs overlay squashfs shm")
#   HOST_DISK_DF_CMD — disk source (default "df -P -T", GNU df: needs Type col)
#   HOST_DISK_FIXTURE — file used instead of running df (tests/drill)
#   HOST_DISK_HOST — host label in evidence (default `hostname`)
set -uo pipefail

DETECTOR="watchdog/host_disk"
PHASE="phase-1-read-only"
CEO_ROUTE="ceo-hourly-routine"

WARN_PCT="${HOST_DISK_WARN_PCT:-85}"
PAGE_PCT="${HOST_DISK_PAGE_PCT:-95}"
EXCLUDE_TYPES="${HOST_DISK_EXCLUDE_TYPES:-tmpfs devtmpfs overlay squashfs shm}"
DF_CMD="${HOST_DISK_DF_CMD:-df -P -T}"
FIXTURE="${HOST_DISK_FIXTURE:-}"
HOST_LABEL="${HOST_DISK_HOST:-$(hostname 2>/dev/null || echo unknown)}"

refuse() { echo "host_disk_detector: REFUSED: $*" >&2; exit 2; }

[[ "$WARN_PCT" =~ ^[0-9]+$ ]] && (( 10#$WARN_PCT >= 1 && 10#$WARN_PCT <= 100 )) \
  || refuse "HOST_DISK_WARN_PCT='$WARN_PCT' is not a whole percent 1-100"
[[ "$PAGE_PCT" =~ ^[0-9]+$ ]] && (( 10#$PAGE_PCT >= 1 && 10#$PAGE_PCT <= 100 )) \
  || refuse "HOST_DISK_PAGE_PCT='$PAGE_PCT' is not a whole percent 1-100"
(( 10#$WARN_PCT < 10#$PAGE_PCT )) \
  || refuse "warn ($WARN_PCT) must sit below page ($PAGE_PCT)"

command -v jq >/dev/null 2>&1 || refuse "jq required"
command -v awk >/dev/null 2>&1 || refuse "awk required"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/host-disk-detector.XXXXXXXX")" \
  || refuse "could not create a scratch directory"
trap 'rm -rf "$WORK"' EXIT
DF_OUT="$WORK/df.txt"

if [[ -n "$FIXTURE" ]]; then
  [[ -r "$FIXTURE" ]] || refuse "fixture '$FIXTURE' is not readable"
  cat "$FIXTURE" >"$DF_OUT" || refuse "could not read fixture '$FIXTURE'"
else
  # Word-split on purpose: DF_CMD carries flags ("df -P -T").
  # shellcheck disable=SC2206
  DF_ARGV=($DF_CMD)
  (( ${#DF_ARGV[@]} > 0 )) || refuse "HOST_DISK_DF_CMD is empty"
  "${DF_ARGV[@]}" >"$DF_OUT" 2>"$WORK/df.err" \
    || refuse "disk source failed: $(head -c 300 "$WORK/df.err" | tr '\n' ' ')"
fi

[[ -s "$DF_OUT" ]] || refuse "disk source produced no output"
HEADER="$(head -n 1 "$DF_OUT")"
[[ "$HEADER" == *"Type"* ]] \
  || refuse "disk source has no Type column (need GNU 'df -P -T'); header: $(head -c 160 <<<"$HEADER")"

# One TAB-separated row per filesystem: type, whole-percent used, mount.
# The first five fields (source, type, size, used, avail) are stripped by
# pattern, so a mount point with SPACES survives intact; the Use% cap is the
# next field, so a device name with spaces cannot shift the number onto the
# wrong row either. A row that still will not parse refuses the whole run.
# A TAB anywhere in the raw source refuses too — TAB is the separator, so it
# must not also be data.
awk 'NR==1 { next }
/\011/ { print "TAB:" $0 > "/dev/stderr"; exit 3 }
($2 ~ /\// || $2 ~ /^[0-9]+$/) { print "SHIFTED:" $0 > "/dev/stderr"; exit 3 }
{
  rest = $0
  if (sub(/^[^ \t]+[ \t]+[^ \t]+[ \t]+[^ \t]+[ \t]+[^ \t]+[ \t]+[^ \t]+[ \t]+/, "", rest) != 1) {
    print "SHORT:" $0 > "/dev/stderr"; exit 3
  }
  n = split(rest, tail, /[ \t]+/)
  if (n < 2) { print "SHORT:" $0 > "/dev/stderr"; exit 3 }
  cap = tail[1]; sub(/%$/, "", cap)
  if (cap !~ /^[0-9]+$/) { print "BADCAP:" $0 > "/dev/stderr"; exit 3 }
  mount = substr(rest, index(rest, tail[2]))
  printf "%s\t%s\t%s\n", $2, cap, mount
}' "$DF_OUT" >"$WORK/rows.tsv" 2>"$WORK/parse.err" \
  || refuse "unparseable disk row: $(head -c 300 "$WORK/parse.err")"
[[ -s "$WORK/rows.tsv" ]] || refuse "disk source carried no filesystem rows"

is_excluded() { # <fstype> — exact-word match against EXCLUDE_TYPES
  local want="$1" word
  for word in $EXCLUDE_TYPES; do
    [[ "$want" == "$word" ]] && return 0
  done
  return 1
}

emit_finding() { # <severity> <owner> <route> <reason> <pct> <mount>
  local severity="$1" owner="$2" route="$3" reason="$4" pct="$5" mount="$6"
  jq -n -c \
    --arg detector "$DETECTOR" --arg reason "$reason" \
    --arg severity "$severity" --arg owner "$owner" --arg route "$route" \
    --arg phase "$PHASE" --arg host "$HOST_LABEL" \
    --argjson pct "$pct" --arg mount "$mount" \
    --argjson warn "$WARN_PCT" --argjson page "$PAGE_PCT" \
    '{
       type: "finding", detector: $detector, reason: $reason,
       severity: $severity, owner: $owner, route: $route,
       evidence: {host: $host, mount: $mount, diskPct: $pct,
                  warnPct: $warn, pagePct: $page},
       suggestedAction: (
         if $severity == "critical" then
           "reclaim disk now; production apps share this host — escalated to the CEO desk"
         else
           "reclaim disk or grow the volume before the page threshold; owner DevOps"
         end),
       phase: $phase
     }' || refuse "could not emit finding JSON"
}

checked_n=0; measured_n=0; warn_n=0; page_n=0
while IFS=$'\t' read -r fstype cap mount; do
  checked_n=$((checked_n + 1))
  is_excluded "$fstype" && continue
  measured_n=$((measured_n + 1))
  if (( 10#$cap >= 10#$PAGE_PCT )); then
    emit_finding critical devops "$CEO_ROUTE" \
      "disk at or above page ($PAGE_PCT%)" "$cap" "$mount"
    page_n=$((page_n + 1))
  elif (( 10#$cap >= 10#$WARN_PCT )); then
    emit_finding high devops devops \
      "disk at or above warn ($WARN_PCT%)" "$cap" "$mount"
    warn_n=$((warn_n + 1))
  fi
done <"$WORK/rows.tsv"
(( measured_n > 0 )) \
  || refuse "every filesystem was excluded — nothing was measured"

jq -n -c \
  --arg detector "$DETECTOR" --arg phase "$PHASE" --arg host "$HOST_LABEL" \
  --argjson checked "$checked_n" --argjson measured "$measured_n" \
  --argjson warn "$warn_n" --argjson page "$page_n" \
  '{type: "summary", detector: ($detector + "/cycle"), host: $host,
    phase: $phase, filesystemsChecked: $checked,
    filesystemsMeasured: $measured, warnCount: $warn, pageCount: $page,
    breachCount: ($warn + $page)}' \
  || refuse "could not emit summary JSON"

(( warn_n + page_n > 0 )) && exit 1
exit 0
