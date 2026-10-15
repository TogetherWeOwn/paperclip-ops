#!/usr/bin/env bash
# test-akmatori-alert.sh — case_658bf6b9 phase-1 Akmatori test alert (secure transport).
#
# Sends the §5 pilot Alertmanager-format POST without ever placing the Bearer
# secret in argv (/proc/<pid>/cmdline is world-readable). Same curl-config
# pattern as _gatus_push.sh: the secret travels in a 0600 temp file passed
# as `curl --config <file>`, never as `-H "Authorization: Bearer $SECRET"`.
#
# Env (from /root/.config/paperclip/case_60330d6f.env, root-only 0600):
#   AKMATORI_ALERTMANAGER_URL  per-source webhook URL (Settings → Alert Sources)
#   AKMATORI_WEBHOOK_SECRET    per-source secret (Bearer)
# Usage:
#   test-akmatori-alert.sh [firing|resolved]
# Phase 1 only: proves incident open → Discord → resolve-close against the
# single §5.3 node7b1ff338 host (read-only under forced command; more hosts wait
# for the phase-2 go).
set -uo pipefail

STATUS="${1:-firing}"
[[ "$STATUS" == "firing" || "$STATUS" == "resolved" ]] \
  || { echo "test-akmatori-alert: refusing — arg must be firing|resolved" >&2; exit 2; }
[[ -n "${AKMATORI_ALERTMANAGER_URL:-}" ]] \
  || { echo "test-akmatori-alert: refusing — AKMATORI_ALERTMANAGER_URL is unset" >&2; exit 2; }
[[ -n "${AKMATORI_WEBHOOK_SECRET:-}" ]] \
  || { echo "test-akmatori-alert: refusing — AKMATORI_WEBHOOK_SECRET is unset" >&2; exit 2; }

BODY="$(cat <<EOF
{"receiver":"akmatori","status":"$STATUS","alerts":[{"status":"$STATUS",
"labels":{"alertname":"PilotTestAlert","severity":"warning","instance":"node7b1ff338"},
"annotations":{"summary":"case_658bf6b9 pilot test","description":"read-only probe, resolve after verify"}}]}
EOF
)"

CFG_DIR="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/akmatori_alert.XXXXXXXX")" \
  || { echo "test-akmatori-alert: could not create private temp dir" >&2; exit 2; }
trap 'rm -rf "$CFG_DIR"' EXIT
CFG="$(umask 077; mktemp "$CFG_DIR/curlcfg.XXXXXXXX")" \
  || { echo "test-akmatori-alert: could not create curl config" >&2; exit 2; }
{
  printf 'url = "%s"\n' "$AKMATORI_ALERTMANAGER_URL"
  printf 'request = "POST"\n'
  printf 'header = "Content-Type: application/json"\n'
  printf 'header = "Authorization: Bearer %s"\n' "$AKMATORI_WEBHOOK_SECRET"
  printf 'header = "X-Alertmanager-Secret: %s"\n' "$AKMATORI_WEBHOOK_SECRET"
  printf 'max-time = "%s"\n' "${AKMATORI_PUSH_TIMEOUT:-15}"
  printf 'silent\nshow-error\n'
} > "$CFG"
chmod 0600 "$CFG"
DATA="$(umask 077; mktemp "$CFG_DIR/data.XXXXXXXX")" \
  || { echo "test-akmatori-alert: could not create data file" >&2; exit 2; }
printf '%s' "$BODY" > "$DATA"
chmod 0600 "$DATA"

# NOTE: --config and --data-binary take FILE PATHS only — the secret itself
# never appears in this argv. Do not rewrite this as -H "Bearer $SECRET".
http="$(curl --config "$CFG" --data-binary "@$DATA" -o /dev/null -w '%{http_code}' 2>&1)" \
  || { echo "test-akmatori-alert: curl failed: $http" >&2; exit 1; }
case "$http" in
  2*) echo "test-akmatori-alert: $STATUS accepted (HTTP $http)"; exit 0 ;;
  4*|5*) echo "test-akmatori-alert: refused: HTTP $http (fail the pilot per OPERATOR.md §5)" >&2; exit 1 ;;
  *) echo "test-akmatori-alert: unexpected HTTP $http" >&2; exit 1 ;;
esac
