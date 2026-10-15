#!/usr/bin/env bash
# push-workers-error-rate.sh — cron: Cloudflare Workers error ratio
# (staging + prod) → Gatus `platform_workers-error-rate`.
#
# Queries the Cloudflare GraphQL analytics API for
# `workersInvocationsAdaptive` (sum of requests + errors per script over the
# trailing window) and fails when either worker's error ratio exceeds
# WORKERS_ERROR_THRESHOLD_PCT. Read-only: one analytics query per worker,
# no writes, no deploys, no config changes.
#
# FAIL-CLOSED: a transport failure, a non-2xx API answer, an unparseable
# payload, or a window with zero observed invocations all push
# success=false — an unverifiable error rate is itself an incident for a
# status page that claims dependency health. Missing CF_API_TOKEN /
# CF_ACCOUNT_ID / script names refuse (exit 2) with zero pushes: the 10m
# heartbeat miss then pages, never a silent green.
#
# NON-DISCLOSURE: the API token travels in a 0600 curl config file, never
# in argv — /proc/<pid>/cmdline is world-readable on the shared host. Same
# pattern as _gatus_push.sh. The token value is never printed, even on
# failure paths; error text names the worker and the ratio only.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), CF_API_TOKEN,
# CF_ACCOUNT_ID, WORKERS_STAGING_SCRIPT, WORKERS_PROD_SCRIPT,
# WORKERS_ERROR_THRESHOLD_PCT (default 1, percent), WORKERS_WINDOW_MIN
# (default 30), CF_API_BASE (default https://api.cloudflare.com, override
# for offline tests), CURL_BIN (default curl), plus jq and python3.
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-workers-error-rate.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

ENDPOINT_KEY="platform_workers-error-rate"
THRESHOLD_PCT="${WORKERS_ERROR_THRESHOLD_PCT:-1}"
WINDOW_MIN="${WORKERS_WINDOW_MIN:-30}"
API_BASE="${CF_API_BASE:-https://api.cloudflare.com}"
CURL_BIN="${CURL_BIN:-curl}"
STAGING_SCRIPT="${WORKERS_STAGING_SCRIPT:-}"
PROD_SCRIPT="${WORKERS_PROD_SCRIPT:-}"

[[ -n "${CF_API_TOKEN:-}" ]] \
  || { echo "push-workers-error-rate: refusing — CF_API_TOKEN is unset" >&2; exit 2; }
[[ -n "${CF_ACCOUNT_ID:-}" ]] \
  || { echo "push-workers-error-rate: refusing — CF_ACCOUNT_ID is unset" >&2; exit 2; }
[[ -n "$STAGING_SCRIPT" ]] \
  || { echo "push-workers-error-rate: refusing — WORKERS_STAGING_SCRIPT is unset" >&2; exit 2; }
[[ -n "$PROD_SCRIPT" ]] \
  || { echo "push-workers-error-rate: refusing — WORKERS_PROD_SCRIPT is unset" >&2; exit 2; }
[[ "$THRESHOLD_PCT" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || { echo "push-workers-error-rate: refusing — WORKERS_ERROR_THRESHOLD_PCT must be numeric" >&2; exit 2; }
[[ "$WINDOW_MIN" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-workers-error-rate: refusing — WORKERS_WINDOW_MIN must be a positive integer" >&2; exit 2; }
command -v "$CURL_BIN" >/dev/null \
  || { echo "push-workers-error-rate: refusing — curl binary '$CURL_BIN' not found" >&2; exit 2; }
command -v jq >/dev/null \
  || { echo "push-workers-error-rate: refusing — jq not found" >&2; exit 2; }
command -v python3 >/dev/null \
  || { echo "push-workers-error-rate: refusing — python3 not found" >&2; exit 2; }

fail_closed() {
  gatus_push "$ENDPOINT_KEY" false "$1" || true
  echo "push-workers-error-rate: FAIL-CLOSED: $1" >&2
  exit 1
}

# One analytics query for one worker script. Prints "<requests> <errors>".
query_worker() {
  local script="$1" since_iso now_iso gql resp http body
  now_iso="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
  since_iso="$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes='"$WINDOW_MIN"')).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
  gql="$(python3 - "$CF_ACCOUNT_ID" "$script" "$since_iso" "$now_iso" <<'PY'
import json, sys
account, script, since, now = sys.argv[1:5]
print(json.dumps({"query": """{
  viewer {
    accounts(filter: {accountTag: "%s"}) {
      workersInvocationsAdaptive(
        filter: {scriptName: "%s", datetime_gt: "%s", datetime_lt: "%s"}
        limit: 100
      ) {
        sum { requests errors }
      }
    }
  }
}""" % (account, script, since, now)}))
PY
)"
  local cfg_dir cfg datafile
  cfg_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/workers_err.XXXXXXXX")" \
    || fail_closed "could not create a private temp directory"
  # Expand now: the trap runs after this function returns, when locals are gone.
  # shellcheck disable=SC2064
  trap "rm -rf '$cfg_dir'" RETURN
  cfg="$(umask 077; mktemp "$cfg_dir/curlcfg.XXXXXXXX")" \
    || fail_closed "could not create a curl config file"
  # The query body rides in a 0600 sibling file referenced with @: a curl
  # config `data = ...` value with unquoted whitespace is truncated at the
  # first space (curl warns and mis-sends), so inline data is never used.
  datafile="$(umask 077; mktemp "$cfg_dir/query.XXXXXXXX.json")" \
    || fail_closed "could not create a query body file"
  printf '%s' "$gql" > "$datafile"
  chmod 0600 "$datafile"
  {
    printf 'url = "%s/client/v4/graphql"\n' "$API_BASE"
    printf 'request = "POST"\n'
    printf 'header = "Authorization: Bearer %s"\n' "$CF_API_TOKEN"
    printf 'header = "Content-Type: application/json"\n'
    printf 'data-binary = "@%s"\n' "$datafile"
    printf 'max-time = "%s"\n' "${CF_API_TIMEOUT:-15}"
    printf 'silent\nshow-error\n'
  } > "$cfg"
  chmod 0600 "$cfg"
  resp="$("$CURL_BIN" --config "$cfg" -w '\n%{http_code}')" \
    || fail_closed "analytics query for worker '$script' failed at transport"
  http="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  [[ "$http" =~ ^2 ]] \
    || fail_closed "analytics query for worker '$script' answered HTTP $http"
  printf '%s' "$body" | jq -r '
    (.data.viewer.accounts[0].workersInvocationsAdaptive[0].sum // null)
    | if . == null then "NODATA"
      else "\(.requests // "NAN") \(.errors // "NAN")" end' 2>/dev/null \
    || fail_closed "analytics answer for worker '$script' is unparseable"
}

check_worker() {
  local label="$1" script="$2" out reqs errs ratio
  # query_worker fail-closes (pushes false) itself; the `|| exit 1` keeps
  # the parent from continuing onto a success push after a subshell exit.
  out="$(query_worker "$script")" || exit 1
  [[ "$out" == "NODATA" ]] \
    && fail_closed "worker '$label/$script': no invocations observed in trailing ${WINDOW_MIN}m"
  read -r reqs errs <<<"$out"
  [[ "$reqs" =~ ^[0-9]+$ && "$errs" =~ ^[0-9]+$ ]] \
    || fail_closed "worker '$label/$script': non-numeric counts (requests='$reqs' errors='$errs')"
  (( reqs > 0 )) \
    || fail_closed "worker '$label/$script': zero requests in trailing ${WINDOW_MIN}m"
  ratio="$(python3 -c "print($errs / $reqs * 100)")"
  if python3 -c "import sys; sys.exit(0 if $ratio > $THRESHOLD_PCT else 1)"; then
    fail_closed "worker '$label/$script': error ratio ${ratio}% exceeds ${THRESHOLD_PCT}% (${errs}/${reqs} in trailing ${WINDOW_MIN}m)"
  fi
  printf '%s/%s %.4f%% (%s/%s)\n' "$label" "$script" "$ratio" "$errs" "$reqs"
}

# check_worker fail-closes (pushes false) itself; `|| exit 1` stops the
# sibling check and the success push below after a subshell exit.
STAGING_LINE="$(check_worker staging "$STAGING_SCRIPT")" || exit 1
PROD_LINE="$(check_worker prod "$PROD_SCRIPT")" || exit 1
gatus_push "$ENDPOINT_KEY" true || {
  echo "push-workers-error-rate: push to Gatus failed" >&2
  exit 1
}
echo "push-workers-error-rate: healthy — $STAGING_LINE; $PROD_LINE"
