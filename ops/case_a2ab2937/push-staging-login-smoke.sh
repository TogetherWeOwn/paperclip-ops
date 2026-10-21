#!/usr/bin/env bash
# Staging-only login journey → two_staging-login-smoke. No installation authority.
# POST login → fresh __Host-two_session → GET auth/status → unauthenticated /up.
# Bounds: login < 5000ms; status/up < 3000ms. Never follow redirects.
# 429: Retry-After capped at 120s, one retry; repeated 429 remains non-paging
# (Gatus true / exit 0), but is NOT an authenticated journey in --json.
# Credentials use 0600 curl configs, never argv. Never dump responses or errors;
# a missing/refused credential blocks; never substitute another credential.
# Existing CISO/release/install HOLDs remain; source changes authorize no live call.
set -uo pipefail

KEY="two_staging-login-smoke"
JSON=false; DRY_RUN=false; HELP=false
OUTCOME=refused; ERROR_CODE=invalid_arguments; RETRYABLE=false
NEXT_ACTION="Use --help and supply only the documented flags."
TMPD=""; STEPS=(); PLAN=false

# Emit only allowlisted metadata, including on refusals. No response data crosses
# this boundary; metrics are validated before they enter STEPS.
finish() {
  local rc=$?
  [[ -z "$TMPD" ]] || rm -rf "$TMPD"
  if [[ "$JSON" == true ]]; then
    python3 - "$KEY" "$OUTCOME" "$ERROR_CODE" "$RETRYABLE" "$NEXT_ACTION" "$PLAN" "${STEPS[@]}" <<'PY'
import json, sys
key, outcome, code, retryable, action, plan, *metrics = sys.argv[1:]
ids = ("login", "auth_status", "up")
steps = []
for i, metric in enumerate(metrics):
    status, seconds = metric.split()
    steps.append({"id": ids[i], "http_status": int(status), "latency_ms": float(seconds) * 1000})
if plan == "true":
    steps = [{"id": id, "method": method, "path": path} for id, method, path in (
        ("login", "POST", "/auth/qa/qa-member"),
        ("auth_status", "GET", "/auth/status"), ("up", "GET", "/up"))]
result = {"schema_version": 1, "probe_id": key, "outcome": outcome, "steps": steps}
if outcome in ("refused", "failed", "throttled"):
    result["error"] = {"code": code, "retryable": retryable == "true", "next_action": action}
print(json.dumps(result, allow_nan=False, separators=(",", ":")))
PY
  fi
  return "$rc"
}
# Recognize structured mode even when another argument is invalid. Never echo it.
for flag in "$@"; do [[ "$flag" != --json ]] || JSON=true; done
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
seen_json=false
for flag in "$@"; do
  case "$flag" in
    --json) [[ "$seen_json" == false ]] || exit 2; seen_json=true ;;
    --dry-run) [[ "$DRY_RUN" == false ]] || exit 2; DRY_RUN=true ;;
    --help) [[ "$HELP" == false ]] || exit 2; HELP=true ;;
    *) echo "push-staging-login-smoke: refusing — unknown argument (see --help)" >&2; exit 2 ;;
  esac
done
if [[ "$HELP" == true ]]; then
  [[ "$JSON" == false && "$DRY_RUN" == false ]] || exit 2
  printf '%s\n' \
    'Usage: push-staging-login-smoke.sh [--json] [--dry-run] | --help' \
    'Default: run the separately authorized staging journey and existing Gatus push.' \
    '--json: version 1 redacted result; throttled is NOT journey success.' \
    '--dry-run: static plan only; no credentials, temp configs, curl, sleep or push.' \
    'Offline check (repository root):' \
    '  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_staging_login_journey' \
    '  PYTHONDONTWRITEBYTECODE=1 bash test_case_00b2eb59_probe.sh' \
    'No flag grants runtime authority or removes a CISO/install HOLD.'
  exit 0
fi

STAGING_ORIGIN="https://next.togetherweown.com"
BASE="${STAGING_BASE_URL:-$STAGING_ORIGIN}"
ERROR_CODE=staging_origin_refused
NEXT_ACTION="Use only the canonical staging origin; do not broaden its allowlist."
# Preserve the exact destination guard before credentials or private config I/O.
case "$BASE" in
  "$STAGING_ORIGIN"|"$STAGING_ORIGIN"/) BASE="$STAGING_ORIGIN" ;;
  *)
    echo "push-staging-login-smoke: refusing — STAGING_BASE_URL is not an allowed staging origin" >&2
    exit 2
    ;;
esac
if [[ "$DRY_RUN" == true ]]; then
  OUTCOME=dry_run; PLAN=true
  [[ "$JSON" == true ]] || printf '%s\n' \
    'Static staging journey: login POST → auth_status GET (fresh private jar) → up GET (no session).' \
    'No session, credential config, curl, sleep or Gatus push executed. All HOLDs remain.'
  exit 0
fi

refuse() {
  ERROR_CODE="$1"; NEXT_ACTION="$2"
  echo "push-staging-login-smoke: refusing — $ERROR_CODE" >&2
  exit 2
}
[[ -n "${QA_AUTH_TOKEN:-}" ]] || refuse qa_auth_missing 'Restore the existing scoped credential through its authorized owner; never substitute one.'
[[ "$QA_AUTH_TOKEN" =~ ^[A-Za-z0-9_.\-~+/=]+$ ]] || refuse qa_auth_shape 'Correct the scoped credential configuration without exposing its value.'
command -v curl >/dev/null 2>&1 || refuse curl_missing 'Install the existing curl prerequisite through the authorized operator.'
command -v python3 >/dev/null 2>&1 || refuse python_missing 'Install the existing python3 prerequisite through the authorized operator.'

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
. "$HERE/../case_658bf6b9/_gatus_push.sh"
# This umask also covers response bodies, headers and suppressed curl errors.
umask 077
TMPD="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/login_smoke.XXXXXXXX")" \
  || refuse temp_unavailable 'Restore private temporary-directory access before retrying.'
JAR="$TMPD/jar"; HDR="$TMPD/hdr"; BODY="$TMPD/body"; CFG="$TMPD/curlcfg"
{
  printf 'header = "X-TWO-QA-Auth: %s"\n' "$QA_AUTH_TOKEN"
  printf 'header = "Origin: https://next.togetherweown.com"\n'
} > "$CFG"
chmod 0600 "$CFG" || refuse temp_permissions 'Restore private temporary-file permissions before retrying.'

# Shared Gatus helper diagnostics can contain curl text: capture, never relay it.
push_result() {
  if ! {
    if [[ "$1" == true ]]; then
      gatus_push "$KEY" true
    else
      gatus_push "$KEY" false "${2:-}"
    fi
  } >"$TMPD/pushout" 2>"$TMPD/pusherr"; then
    OUTCOME=failed; ERROR_CODE=gatus_push_failed; RETRYABLE=true
    NEXT_ACTION='Inspect the existing Gatus configuration through its authorized owner; do not dump transport output.'
    echo 'push-staging-login-smoke: Gatus push failed (diagnostics suppressed)' >&2
    return 1
  fi
}
fail_push() {
  OUTCOME=failed; ERROR_CODE="$1"; RETRYABLE="${3:-false}"
  NEXT_ACTION='Inspect the failing step using offline checks; preserve all live-probe and installation HOLDs.'
  echo "push-staging-login-smoke: $2" >&2
  push_result false "$2"
  exit 1
}

# Clear response state for EVERY call. -q must be first: an ambient .curlrc
# cannot inject a session, redirect, verbose output or another destination.
run_capped() {
  local index="$1" max_secs="$2"; shift 2
  { : > "$HDR" && : > "$BODY"; } || return 1
  local out rc=0
  out="$(curl -q -sS --config "$CFG" -D "$HDR" -o "$BODY" \
    --max-redirs 0 --max-time "$max_secs" -w '%{http_code} %{time_total}' "$@" 2>"$TMPD/curlerr")" || rc=$?
  CODE=0; SECS=0
  if [[ "$out" =~ ^([0-9]{3})\ ([0-9]+([.][0-9]+)?)$ ]]; then
    CODE="${BASH_REMATCH[1]}"; SECS="${BASH_REMATCH[2]}"
  else
    rc=1
  fi
  STEPS[index]="$CODE $SECS"
  return "$rc"
}
step1_once() {
  : > "$JAR" || return 1
  chmod 0600 "$JAR" || return 1
  run_capped 0 20 -c "$JAR" -X POST "$BASE/auth/qa/qa-member"
}

# Parse the final HTTP header block only. Cookie attributes must occur as exact
# attributes on the exact session Set-Cookie record, never in an unrelated field.
verify_response() {
  python3 - "$1" "$HDR" "$BODY" <<'PY'
import json, pathlib, sys
kind, header_path, body_path = sys.argv[1:]
def reject(*args):
    raise ValueError("invalid response")
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject()
        result[key] = value
    return result
try:
    if kind == "auth_status":
        body = json.loads(pathlib.Path(body_path).read_text(), object_pairs_hook=unique_object, parse_constant=reject)
        valid = isinstance(body, dict) and body.get("authenticated") is True
    else:
        headers = []
        for line in pathlib.Path(header_path).read_bytes().decode("utf-8").split("\n"):
            line = line.removesuffix("\r")
            if line.startswith("HTTP/"):
                headers = []
            elif ":" in line:
                name, value = line.split(":", 1)
                headers.append((name.lower(), value.strip(" \t")))
        if kind == "up":
            valid = [value for name, value in headers if name == "x-two-origin"] == ["two-web-next"]
        else:
            cookies = []
            for name, value in headers:
                if name != "set-cookie":
                    continue
                record, *attributes = value.split(";")
                cookie_name, separator, cookie_value = record.partition("=")
                if cookie_name == "__Host-two_session":
                    attributes = [a.strip(" \t").lower() for a in attributes]
                    cookies.append(bool(separator and cookie_value) and "secure" in attributes and "httponly" in attributes
                                   and "path=/" in attributes and not any(a.split("=", 1)[0] == "domain" for a in attributes))
            valid = cookies == [True]
    sys.exit(0 if valid else 1)
except (OSError, ValueError, RecursionError):
    sys.exit(1)
PY
}
check_latency() {
  python3 -c 'import math,sys; s=float(sys.argv[1]); sys.exit(0 if math.isfinite(s) and 0 <= s < float(sys.argv[2]) else 1)' "$SECS" "$1" 2>/dev/null
}

step1_once || fail_push login_transport 'login POST transport failure' true
if [[ "$CODE" == "429" ]]; then
  RA="$(python3 - "$HDR" <<'PY'
import pathlib, re, sys
try:
    values = []
    for line in pathlib.Path(sys.argv[1]).read_bytes().decode("utf-8").split("\n"):
        line = line.removesuffix("\r")
        if line.startswith("HTTP/"):
            values = []
        elif ":" in line:
            key, value = line.split(":", 1)
            if key.lower() == "retry-after":
                values.append(value.strip(" \t"))
    value = values[0] if len(values) == 1 else ""
    if re.fullmatch(r"[0-9]+", value):
        value = value.lstrip("0") or "0"
        print(120 if len(value) > 3 else min(int(value), 120))
    else:
        print(60)
except (OSError, ValueError):
    sys.exit(1)
PY
)" || fail_push retry_after_invalid 'login Retry-After could not be parsed'
  echo "push-staging-login-smoke: throttled (429), backing off ${RA}s per Retry-After" >&2
  sleep "$RA" || fail_push backoff_failed 'login backoff failed' true
  step1_once || fail_push login_transport 'login POST transport failure on retry' true
  if [[ "$CODE" == "429" ]]; then
    OUTCOME=throttled; ERROR_CODE=login_throttled; RETRYABLE=true
    NEXT_ACTION='Wait for the existing scheduled probe; do not treat this as authenticated-journey evidence.'
    echo 'push-staging-login-smoke: still throttled after backoff; not paging' >&2
    push_result true || exit 1
    exit 0
  fi
fi
[[ "$CODE" == "204" ]] || fail_push login_status "login POST status $CODE (want 204)"
verify_response login || fail_push login_cookie 'login POST missing Secure+HttpOnly __Host-two_session cookie'
check_latency 5.0 || fail_push login_latency "login POST ${SECS}s over 5000ms bound"

run_capped 1 15 -b "$JAR" "$BASE/auth/status" || fail_push auth_status_transport 'auth/status transport failure' true
[[ "$CODE" == "200" ]] || fail_push auth_status_status "auth/status status $CODE (want 200)"
verify_response auth_status || fail_push auth_status_body 'auth/status body not authenticated:true'
check_latency 3.0 || fail_push auth_status_latency "auth/status ${SECS}s over 3000ms bound"

run_capped 2 15 --cookie '' "$BASE/up" || fail_push up_transport 'up transport failure' true
[[ "$CODE" == "200" ]] || fail_push up_status "up status $CODE (want 200)"
verify_response up || fail_push up_origin 'up missing exact x-two-origin: two-web-next header'
check_latency 3.0 || fail_push up_latency "up ${SECS}s over 3000ms bound"

OUTCOME=ok
[[ "$JSON" == true ]] || printf 'push-staging-login-smoke: login %s, status %s, up %s (HTTP/seconds)\n' "${STEPS[0]}" "${STEPS[1]}" "${STEPS[2]}"
push_result true || exit 1
