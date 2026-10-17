#!/usr/bin/env bash
# monitor-health.sh — on-call read-only monitor self-health (case_66436e78 slice 7).
#
# Invoked ONLY through the two exact-match `monitor-health beszel|gatus`
# arms of ro-shell.sh (forced command, deny-by-default). <name> is one of
# the two keys below; it maps to a hardcoded localhost health URL — the
# key never becomes a URL except through this allow-list.
#
# READ-ONLY, SECRET-FREE: both surfaces are the public, unauthenticated
# health routes of the localhost-only services (Beszel hub is PocketBase:
# GET /api/health; Gatus: GET /health). Their authenticated APIs are NOT
# used here: Gatus /v1/endpoints/statuses is a protected route once
# security is set, and the Beszel systems list needs user auth — those
# need a credentialed follow-up slice, never a token in this helper.
#
# NON-DISCLOSURE: the response body is discarded (-o /dev/null) and only
# the fixed lines `beszel: up`, `gatus: DOWN (http N)` or
# `<name>: DOWN (unreachable)` are printed — upstream body/stderr text,
# which is outside our control, never crosses the SSH forced command.
#
# FAIL CLOSED: an unreachable service, a non-200 status, or an
# unparseable status exits 1 (page, never report healthy); an unknown
# key or a non-localhost URL exits 2 without any probe.
# MONITOR_HEALTH_BESZEL_URL / MONITOR_HEALTH_GATUS_URL override the URLs
# in tests only; production always uses the hardcoded localhost defaults
# (client input cannot reach them — ro-shell matches the whole command
# literally and passes a hardcoded key), and even an override must stay
# on 127.0.0.1.
set -euo pipefail

case "${1:-}" in
  beszel) NAME="beszel"; URL="${MONITOR_HEALTH_BESZEL_URL:-http://127.0.0.1:8090/api/health}" ;;
  gatus) NAME="gatus"; URL="${MONITOR_HEALTH_GATUS_URL:-http://127.0.0.1:8080/health}" ;;
  *) echo "monitor-health: refusing — unknown name '${1:-<empty>}'" >&2; exit 2 ;;
esac

[[ "$URL" =~ ^http://127\.0\.0\.1:[0-9]+/[A-Za-z0-9/_-]+$ ]] \
  || { echo "monitor-health: refusing — URL for $NAME is not a localhost health URL" >&2; exit 2; }

CODE=""
if ! CODE="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 "$URL" 2>/dev/null)"; then
  echo "$NAME: DOWN (unreachable)"
  exit 1
fi
if [[ "$CODE" == "200" ]]; then
  echo "$NAME: up"
elif [[ "$CODE" =~ ^[0-9]{3}$ ]]; then
  echo "$NAME: DOWN (http $CODE)"
  exit 1
else
  echo "$NAME: DOWN (unreachable)"
  exit 1
fi
