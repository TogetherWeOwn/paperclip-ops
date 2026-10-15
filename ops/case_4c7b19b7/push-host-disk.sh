#!/usr/bin/env bash
# push-host-disk.sh — cron: worst local filesystem pressure → Gatus `platform_host-disk`.
#
# case_4c7b19b7 (D8, slice of case_311c4d5b). Source-only: no host deploy or cron
# install rides this change; the operator installs the cron line separately.
# Reads the worst measured filesystem from host_disk_detector.sh stdout and
# pushes one boolean: success=true when no filesystem breaches warn
# (HOST_DISK_WARN_PCT), success=false naming the worst breacher otherwise.
# A skipped metric would be indistinguishable from an unknown one: zero
# breaches is the clean signal the Gatus heartbeat carries. A detector
# refusal (exit 2), an unpushable result, or any transport failure is a
# failure push (exit 1) — disk invisibility on the host that hit 99% is
# itself the incident.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh); thresholds pass
# through (HOST_DISK_WARN_PCT default 85, HOST_DISK_PAGE_PCT default 95),
# plus HOST_DISK_EXCLUDE_TYPES / HOST_DISK_DF_CMD / HOST_DISK_FIXTURE /
# HOST_DISK_HOST for the detector. Cron (operator):
#   */5 * * * * /opt/status-page/current/ops/case_4c7b19b7/push-host-disk.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-host-disk: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
# shellcheck source=/dev/null
. "$GATUS_HELPER"

DETECTOR="${HOST_DISK_DETECTOR:-$HERE/host_disk_detector.sh}"
[[ -x "$DETECTOR" ]] \
  || { echo "push-host-disk: refusing — detector $DETECTOR missing or not executable" >&2; exit 2; }
command -v jq >/dev/null 2>&1 \
  || { echo "push-host-disk: refusing — jq required" >&2; exit 2; }

out="$("$DETECTOR" 2>&1)" && rc=0 || rc=$?
if (( rc == 2 )); then
  gatus_push platform_host-disk false "detector refused: $(head -c 200 <<<"$out" | tr '\n' ' ')"
  exit 1
fi

# A clean detector prints a summary and zero findings; a breach prints both.
# Either way the summary must be present — output with neither is unparseable.
# (jq -c is compact: no space after the colon.)
grep -q '"type":"summary"' <<<"$out" \
  || { gatus_push platform_host-disk false "detector output has no summary row"; exit 1; }

if (( rc == 0 )); then
  # A push that cannot authenticate must fail loudly so cron reports it,
  # never exit 0 over an unpushed metric.
  gatus_push platform_host-disk true || exit 1
  exit 0
fi

(( rc == 1 )) \
  || { gatus_push platform_host-disk false "detector exited $rc"; exit 1; }

# Slurp (-s) wraps the JSONL rows in an array, so select runs over .[].
worst="$(jq -rs '
  [ .[] | select(.type == "finding")
    | {pct: .evidence.diskPct, mount: .evidence.mount,
       page: (.reason | test("page"; "i"))} ]
  | max_by(.pct // -1)' <<<"$out" 2>/dev/null)" \
  || { gatus_push platform_host-disk false "detector output unparseable"; exit 1; }

[[ -n "$worst" && "$worst" != "null" ]] \
  || { gatus_push platform_host-disk false "breach reported but no finding row"; exit 1; }

[[ "$(jq -r '.pct // "ABSENT"' <<<"$worst" 2>/dev/null)" =~ ^[0-9]+$ ]] \
  || { gatus_push platform_host-disk false "worst-filesystem reading is not a whole percent"; exit 1; }

mount="$(jq -r '.mount // "?"' <<<"$worst")"
pct="$(jq -r '.pct' <<<"$worst")"
if [[ "$(jq -r '.page' <<<"$worst")" == "true" ]]; then
  gatus_push platform_host-disk false "page: $mount at ${pct}% (CEO desk escalated)"
else
  gatus_push platform_host-disk false "warn: $mount at ${pct}%"
fi
exit 1
