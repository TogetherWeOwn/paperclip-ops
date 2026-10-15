#!/usr/bin/env bash
# push-tls-expiry.sh — cron: TLS/domain-expiry → Gatus `platform_tls-expiry`.
#
# For each domain (host[:port]) it opens a TLS handshake, reads the leaf
# certificate's notAfter, and pushes success=false when any domain is within
# the warn window (TLS_WARN_DAYS, default 21) or the crit window
# (TLS_CRIT_DAYS, default 7) — the message says which. Both windows push
# DOWN so the single-boolean external endpoint pages early (warn) and the
# message escalates to "critical" inside 7 days. All domains healthy pushes
# success=true. Any probe failure (handshake, parse, clock) pushes
# success=false (exit 1): an unverifiable certificate must never read green.
#
# Domains come from TLS_DOMAINS (space-separated host[:port]); when unset,
# hosts are derived from TWO_WEB_PROD_URL and TWO_WEB_STAGING_URL (the same
# env the pull checks use). No domain configured at all refuses (exit 2) —
# the 26h heartbeat miss then pages, never a silent green.
#
# NON-DISCLOSURE: only bare hostnames travel — no URL, no userinfo, no
# token. Error text names hosts + days-left only.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), TLS_DOMAINS,
# TWO_WEB_PROD_URL, TWO_WEB_STAGING_URL, TLS_WARN_DAYS (default 21),
# TLS_CRIT_DAYS (default 7), TLS_TIMEOUT (default 15, seconds per handshake),
# OPENSSL_BIN (default openssl), TIMEOUT_BIN (default timeout).
# Cron (operator): 17 4 * * * /opt/status-page/current/ops/case_658bf6b9/push-tls-expiry.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

OPENSSL_BIN="${OPENSSL_BIN:-openssl}"
TIMEOUT_BIN="${TIMEOUT_BIN:-timeout}"
WARN_DAYS="${TLS_WARN_DAYS:-21}"
CRIT_DAYS="${TLS_CRIT_DAYS:-7}"
TIMEOUT_S="${TLS_TIMEOUT:-15}"

command -v "$OPENSSL_BIN" >/dev/null 2>&1 \
  || { echo "push-tls-expiry: refusing — no '$OPENSSL_BIN' on PATH (operator install?)" >&2; exit 2; }
command -v "$TIMEOUT_BIN" >/dev/null 2>&1 \
  || { echo "push-tls-expiry: refusing — no '$TIMEOUT_BIN' on PATH" >&2; exit 2; }
[[ "$WARN_DAYS" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-tls-expiry: refusing — TLS_WARN_DAYS must be a positive integer" >&2; exit 2; }
[[ "$CRIT_DAYS" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-tls-expiry: refusing — TLS_CRIT_DAYS must be a positive integer" >&2; exit 2; }
(( CRIT_DAYS < WARN_DAYS )) \
  || { echo "push-tls-expiry: refusing — TLS_CRIT_DAYS ($CRIT_DAYS) must be below TLS_WARN_DAYS ($WARN_DAYS)" >&2; exit 2; }
[[ "$TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-tls-expiry: refusing — TLS_TIMEOUT must be a positive integer" >&2; exit 2; }

# Derive default domains from the pull-check URLs (host or host:port only —
# scheme, path, query and userinfo never leave this function).
derive_hosts() {
  python3 - "$@" <<'PY'
import sys, urllib.parse
for raw in sys.argv[1:]:
    try:
        u = urllib.parse.urlsplit(raw)
    except Exception:
        continue
    if u.scheme not in ("http", "https") or not u.hostname:
        continue
    if u.port:
        print(f"{u.hostname}:{u.port}")
    else:
        print(u.hostname)
PY
}

DOMAINS="${TLS_DOMAINS:-}"
if [[ -z "${DOMAINS//[[:space:]]/}" ]]; then
  DOMAINS="$(derive_hosts "${TWO_WEB_PROD_URL:-}" "${TWO_WEB_STAGING_URL:-}" | tr '\n' ' ')"
fi
[[ -n "${DOMAINS//[[:space:]]/}" ]] \
  || { echo "push-tls-expiry: refusing — no domains (set TLS_DOMAINS or TWO_WEB_PROD_URL/TWO_WEB_STAGING_URL)" >&2; exit 2; }

read -r -a HOSTS <<<"$DOMAINS"
for h in "${HOSTS[@]}"; do
  [[ "$h" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]] \
    || { echo "push-tls-expiry: refusing — bad domain '$h' (want host[:port])" >&2; exit 2; }
  if [[ "$h" == *:* ]]; then
    port="${h##*:}"
    (( 10#$port >= 1 && 10#$port <= 65535 )) \
      || { echo "push-tls-expiry: refusing — bad port in '$h'" >&2; exit 2; }
  fi
done

days_left() {  # <host[:port]> → prints whole days left, or FAIL
  # NOTE: one `local` per line — `local a="$1" b="$a"` reads `a` before
  # `local` sets it and dies under `set -u`.
  local target="$1"
  local host="$target"
  local port=443
  [[ "$target" == *:* ]] && { host="${target%%:*}"; port="${target##*:}"; }
  local enddate end_s now_s
  enddate="$("$TIMEOUT_BIN" "$TIMEOUT_S" "$OPENSSL_BIN" s_client \
    -connect "$host:$port" -servername "$host" -verify_return_error \
    </dev/null 2>/dev/null \
    | "$OPENSSL_BIN" x509 -noout -enddate 2>/dev/null)" || { echo "FAIL"; return; }
  [[ "$enddate" == notAfter=* ]] || { echo "FAIL"; return; }
  end_s="$(date -d "${enddate#notAfter=}" +%s 2>/dev/null)" || { echo "FAIL"; return; }
  now_s="$(date +%s)"
  echo $(( (end_s - now_s) / 86400 ))
}

worst_state=0   # 0 ok, 1 warn, 2 crit-or-error
declare -a warn_hits=() crit_hits=()
for h in "${HOSTS[@]}"; do
  d="$(days_left "$h")"
  if [[ "$d" == "FAIL" ]] || [[ ! "$d" =~ ^-?[0-9]+$ ]]; then
    crit_hits+=("$h:unverifiable")
    worst_state=2
  elif (( d <= CRIT_DAYS )); then
    crit_hits+=("$h:${d}d")
    worst_state=2
  elif (( d <= WARN_DAYS )); then
    warn_hits+=("$h:${d}d")
    (( worst_state < 1 )) && worst_state=1
  fi
done

if (( worst_state == 2 )); then
  gatus_push platform_tls-expiry false "critical: ${crit_hits[*]} (crit<=${CRIT_DAYS}d)"
  exit 1
elif (( worst_state == 1 )); then
  gatus_push platform_tls-expiry false "warning: ${warn_hits[*]} (warn<=${WARN_DAYS}d)"
  exit 1
else
  gatus_push platform_tls-expiry true
fi
