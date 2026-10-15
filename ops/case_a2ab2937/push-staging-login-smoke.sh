#!/usr/bin/env bash
# push-staging-login-smoke.sh — cron: staging login-journey smoke → Gatus
# `two_staging-login-smoke`. Spec: case_a2ab2937 document `probe-spec`.
#
# Journey (staging ONLY, default https://next.togetherweown.com):
#   1. POST /auth/qa/qa-member, headers X-TWO-QA-Auth + Origin,
#      maxRedirects 0 → 204 + Set-Cookie __Host-two_session (Secure, HttpOnly)
#   2. GET /auth/status with the fresh jar → 200 {"authenticated": true}
#   3. GET /up, no auth → 200 + header x-two-origin: two-web-next
#
# Latency bounds: step 1 < 5000 ms, steps 2-3 < 3000 ms.
#
# FAIL CLOSED: missing env, missing curl/python3, transport error, unexpected
# status, slow step, or bad cookie/header pushes success=false and exits 1.
# Refusals (bad env shape or non-allowlisted staging origin) exit 2 with zero
# pushes — the 10m heartbeat miss then pages, never a silent green.
#
# THROTTLE: 429 on step 1 spends the shared qa-login budget (10/min per
# runner IP). The probe sleeps per Retry-After (capped at 120s), retries once,
# and on a second 429 pushes success=true (throttled, NOT a page) and exits 0.
# Gatus failure-threshold 3 pages only on consecutive non-429 failures.
#
# NON-DISCLOSURE: QA_AUTH_TOKEN and GATUS_EXTERNAL_TOKEN travel in the
# environment (root-only 0600 env file; see OPERATOR.md) and reach curl only
# through 0600 config files, never argv. Transport-error text echoes request
# headers, so this script logs status codes and latencies only — never bodies,
# never headers, never tokens. A missing/refused credential blocks; never
# substitute another credential.
#
# Env in:  QA_AUTH_TOKEN (mandatory), GATUS_URL + GATUS_EXTERNAL_TOKEN
#          (mandatory, via _gatus_push.sh), STAGING_BASE_URL (optional,
#          default https://next.togetherweown.com; only that exact HTTPS origin
#          or the same origin with one trailing slash is accepted), GATUS_PUSH_TIMEOUT
#          (seconds, default 10).
# Cron (operator): */10 * * * * root . /root/.config/paperclip/case_60330d6f.env;
#   /opt/status-page/current/ops/case_a2ab2937/push-staging-login-smoke.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
. "$HERE/../case_658bf6b9/_gatus_push.sh"

KEY="two_staging-login-smoke"
STAGING_ORIGIN="https://next.togetherweown.com"
BASE="${STAGING_BASE_URL:-$STAGING_ORIGIN}"

# Validate the destination before credential checks or private config creation.
case "$BASE" in
  "$STAGING_ORIGIN"|"$STAGING_ORIGIN"/) BASE="$STAGING_ORIGIN" ;;
  *)
    echo "push-staging-login-smoke: refusing — STAGING_BASE_URL is not an allowed staging origin" >&2
    exit 2
    ;;
esac

fail_push() { gatus_push "$KEY" false "$1"; exit 1; }

[[ -n "${QA_AUTH_TOKEN:-}" ]] \
  || { echo "push-staging-login-smoke: refusing — QA_AUTH_TOKEN is unset" >&2; exit 2; }
[[ "$QA_AUTH_TOKEN" =~ ^[A-Za-z0-9_.\-~+/=]+$ ]] \
  || { echo "push-staging-login-smoke: refusing — QA_AUTH_TOKEN has an unsafe shape" >&2; exit 2; }
command -v curl >/dev/null 2>&1 \
  || { echo "push-staging-login-smoke: refusing — curl not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-staging-login-smoke: refusing — python3 not found" >&2; exit 2; }

TMPD="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/login_smoke.XXXXXXXX")" \
  || { echo "push-staging-login-smoke: refusing — no private temp dir" >&2; exit 2; }
trap 'rm -rf "$TMPD"' EXIT
JAR="$TMPD/jar"; HDR="$TMPD/hdr"; BODY="$TMPD/body"; CFG="$TMPD/curlcfg"
: > "$JAR"; chmod 0600 "$JAR"

# Auth header travels in a 0600 curl config file, never argv.
{
  printf 'header = "X-TWO-QA-Auth: %s"\n' "$QA_AUTH_TOKEN"
  printf 'header = "Origin: https://next.togetherweown.com"\n'
} > "$CFG"
chmod 0600 "$CFG"

# run_capped <out-prefix> <max-secs> -- <curl args...>: single curl call with
# headers dumped to $HDR, body to $BODY; prints "<http-code> <seconds>".
run_capped() {
  local prefix="$1" max_secs="$2"; shift 2
  : > "$HDR"; : > "$BODY"
  local out
  out="$(curl -sS --config "$CFG" -D "$HDR" -o "$BODY" \
    --max-time "$max_secs" -w '%{http_code} %{time_total}' "$@" 2>"$TMPD/curlerr")" \
    || { echo "push-staging-login-smoke: transport failed at $prefix" >&2; return 1; }
  printf '%s' "$out"
}

# --- step 1: login POST ------------------------------------------------------
step1_once() {
  run_capped "login-POST" 20 --max-redirs 0 -X POST "$BASE/auth/qa/qa-member"
}

STEP1="$(step1_once)" || fail_push "login POST transport failure"
CODE="${STEP1%% *}"; SECS="${STEP1##* }"
if [[ "$CODE" == "429" ]]; then
  RA="$(grep -i -m1 '^Retry-After:' "$HDR" | tr -dc '0-9' || true)"
  [[ "$RA" =~ ^[0-9]+$ ]] || RA="60"
  (( 10#$RA > 120 )) && RA="120"
  echo "push-staging-login-smoke: throttled (429), backing off ${RA}s per Retry-After" >&2
  sleep "$RA"
  : > "$JAR"; chmod 0600 "$JAR"
  STEP1="$(step1_once)" || fail_push "login POST transport failure on retry"
  CODE="${STEP1%% *}"; SECS="${STEP1##* }"
  if [[ "$CODE" == "429" ]]; then
    echo "push-staging-login-smoke: still throttled after backoff; not paging" >&2
    gatus_push "$KEY" true
    exit 0
  fi
fi
[[ "$CODE" == "204" ]] \
  || fail_push "login POST status $CODE (want 204)"
if ! grep -qi '__Host-two_session' "$HDR" \
  || ! grep -qi 'Secure' "$HDR" \
  || ! grep -qi 'HttpOnly' "$HDR"; then
  fail_push "login POST missing Secure+HttpOnly __Host-two_session cookie"
fi
if ! python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) < 5.0 else 1)' "$SECS"; then
  fail_push "login POST ${SECS}s over 5000ms bound"
fi

# --- step 2: session check ---------------------------------------------------
STEP2="$(run_capped "auth-status" 15 -b "$JAR" "$BASE/auth/status")" \
  || fail_push "auth/status transport failure"
CODE="${STEP2%% *}"; SECS="${STEP2##* }"
[[ "$CODE" == "200" ]] \
  || fail_push "auth/status status $CODE (want 200)"
grep -q '"authenticated"[[:space:]]*:[[:space:]]*true' "$BODY" \
  || fail_push "auth/status body not authenticated:true"
if ! python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) < 3.0 else 1)' "$SECS"; then
  fail_push "auth/status ${SECS}s over 3000ms bound"
fi

# --- step 3: liveness ---------------------------------------------------------
STEP3="$(run_capped "up" 15 --cookie '' "$BASE/up")" \
  || fail_push "up transport failure"
CODE="${STEP3%% *}"; SECS="${STEP3##* }"
[[ "$CODE" == "200" ]] \
  || fail_push "up status $CODE (want 200)"
grep -qi '^x-two-origin:[[:space:]]*two-web-next' "$HDR" \
  || fail_push "up missing x-two-origin: two-web-next header"
if ! python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) < 3.0 else 1)' "$SECS"; then
  fail_push "up ${SECS}s over 3000ms bound"
fi

echo "push-staging-login-smoke: login ${STEP1%% *}/${STEP1##* }s, status ${STEP2%% *}/${STEP2##* }s, up ${STEP3%% *}/${STEP3##* }s"
gatus_push "$KEY" true
