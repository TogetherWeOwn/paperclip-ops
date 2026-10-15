#!/usr/bin/env bash
# push-github-rate-limit.sh — cron: GitHub REST API core rate-limit
# headroom → Gatus `platform_github-rate-limit`.
#
# Reads the TOKEN-FREE `GET /rate_limit` endpoint (no Authorization header
# is ever sent — the unauthenticated quota is exactly what this probe
# measures) and fails when `resources.core.remaining` drops below
# GITHUB_RATELIMIT_MIN_REMAINING. Read-only: one status query, no writes,
# no secret of any kind.
#
# FAIL-CLOSED: a transport failure, a non-2xx answer, or an unparseable
# payload pushes success=false — an unreadable quota is itself an incident
# for a status page whose CI probes depend on GitHub API headroom. A
# malformed threshold refuses (exit 2) with zero pushes: the 10m heartbeat
# miss then pages, never a silent green.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh),
# GITHUB_RATELIMIT_MIN_REMAINING (default 10, whole requests),
# GITHUB_API_BASE (default https://api.github.com, override for offline
# tests), CURL_BIN (default curl), plus jq.
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-github-rate-limit.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

ENDPOINT_KEY="platform_github-rate-limit"
MIN_REMAINING="${GITHUB_RATELIMIT_MIN_REMAINING:-10}"
API_BASE="${GITHUB_API_BASE:-https://api.github.com}"
CURL_BIN="${CURL_BIN:-curl}"

[[ "$MIN_REMAINING" =~ ^[0-9]+$ ]] \
  || { echo "push-github-rate-limit: refusing — GITHUB_RATELIMIT_MIN_REMAINING must be a whole number" >&2; exit 2; }
command -v "$CURL_BIN" >/dev/null \
  || { echo "push-github-rate-limit: refusing — curl binary '$CURL_BIN' not found" >&2; exit 2; }
command -v jq >/dev/null \
  || { echo "push-github-rate-limit: refusing — jq not found" >&2; exit 2; }

fail_closed() {
  gatus_push "$ENDPOINT_KEY" false "$1" || true
  echo "push-github-rate-limit: FAIL-CLOSED: $1" >&2
  exit 1
}

# Deliberately no Authorization header: this probe measures the
# token-free quota, and no credential value may ever ride this request.
resp="$("$CURL_BIN" -sS --max-time "${GITHUB_API_TIMEOUT:-15}" \
  -H "Accept: application/vnd.github+json" \
  "$API_BASE/rate_limit" -w '\n%{http_code}')" \
  || fail_closed "rate_limit query failed at transport"
http="${resp##*$'\n'}"
body="${resp%$'\n'*}"
[[ "$http" =~ ^2 ]] \
  || fail_closed "rate_limit query answered HTTP $http"
read -r remaining limit reset_epoch <<<"$(printf '%s' "$body" | jq -r '
  [.resources.core.remaining, .resources.core.limit, .resources.core.reset]
  | if (.[0] | type) != "number" or (.[1] | type) != "number"
      or (.[2] | type) != "number" then "BAD"
    else "\(.[0]) \(.[1]) \(.[2])" end' 2>/dev/null)" \
  || fail_closed "rate_limit answer is unparseable"
[[ "$remaining" != "BAD" && "$remaining" =~ ^[0-9]+$ ]] \
  || fail_closed "rate_limit answer carries no numeric core.remaining"
reset_human="$(TZ=UTC date -u -d "@$reset_epoch" '+%H:%MZ' 2>/dev/null || printf 'reset@%s' "$reset_epoch")"
if (( remaining < MIN_REMAINING )); then
  fail_closed "core quota exhausted: ${remaining}/${limit} remaining (resets ${reset_human} UTC)"
fi
gatus_push "$ENDPOINT_KEY" true || {
  echo "push-github-rate-limit: push to Gatus failed" >&2
  exit 1
}
echo "push-github-rate-limit: healthy — core ${remaining}/${limit} remaining (resets ${reset_human} UTC)"
