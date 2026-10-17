#!/usr/bin/env bash
# push-domain-expiry.sh — cron: registrar domain expiry → Gatus `platform_domain-expiry`.
#
# push-tls-expiry.sh measures the leaf certificate's notAfter; a certificate
# can be freshly renewed while the REGISTRATION underneath it lapses. This
# probe closes that gap: for each registered domain it reads the RDAP
# `expiration` event (RFC 9083) and pushes success=false when any domain is
# inside the warn window (DOMAIN_WARN_DAYS, default 21) or the crit window
# (DOMAIN_CRIT_DAYS, default 7) — the message says which. Both windows push
# DOWN so the single-boolean external endpoint pages early (warn) and the
# message escalates to "critical" inside 7 days. Thresholds mirror the TLS
# module on purpose: one mental model for both expiry probes.
#
# FAIL-CLOSED: any probe failure (HTTP not 200, oversized or unparseable
# body, no `expiration` event, bad date) pushes success=false and exits 1 —
# an unverifiable registration must never read green. No domain configured
# at all refuses (exit 2): the 26h heartbeat miss then pages.
#
# Domains come from RDAP_DOMAINS (space-separated registered domains, no
# port). When unset, hostnames are derived from TWO_WEB_PROD_URL and
# TWO_WEB_STAGING_URL and reduced to their last two labels — correct for
# single-label public suffixes (.com, .io, …); a domain under a multi-part
# public suffix (.co.uk, …) must be listed explicitly in RDAP_DOMAINS.
#
# RDAP_BASE_URL (default https://rdap.org/domain/) is the RDAP bootstrap
# redirector; the domain is appended verbatim after validation. The base
# must be https, except http on 127.0.0.1/localhost, which exists only as
# the hermetic test seam. Redirects are pinned to the base scheme.
#
# NON-DISCLOSURE: only bare domain names travel — no URL userinfo, no token.
# Error text names domains + days-left only. The RDAP body is parsed from a
# file, never argv.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh), RDAP_DOMAINS,
# TWO_WEB_PROD_URL, TWO_WEB_STAGING_URL, DOMAIN_WARN_DAYS (default 21),
# DOMAIN_CRIT_DAYS (default 7), RDAP_TIMEOUT (default 15, seconds per
# query), RDAP_BASE_URL, CURL_BIN (default curl).
# Cron (operator): 23 4 * * * /opt/status-page/current/ops/case_658bf6b9/push-domain-expiry.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

CURL_BIN="${CURL_BIN:-curl}"
WARN_DAYS="${DOMAIN_WARN_DAYS:-21}"
CRIT_DAYS="${DOMAIN_CRIT_DAYS:-7}"
TIMEOUT_S="${RDAP_TIMEOUT:-15}"
RDAP_BASE_URL="${RDAP_BASE_URL:-https://rdap.org/domain/}"
MAX_RDAP_BYTES=262144

command -v "$CURL_BIN" >/dev/null 2>&1 \
  || { echo "push-domain-expiry: refusing — no '$CURL_BIN' on PATH (operator install?)" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-domain-expiry: refusing — no 'python3' on PATH" >&2; exit 2; }
[[ "$WARN_DAYS" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-domain-expiry: refusing — DOMAIN_WARN_DAYS must be a positive integer" >&2; exit 2; }
[[ "$CRIT_DAYS" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-domain-expiry: refusing — DOMAIN_CRIT_DAYS must be a positive integer" >&2; exit 2; }
(( CRIT_DAYS < WARN_DAYS )) \
  || { echo "push-domain-expiry: refusing — DOMAIN_CRIT_DAYS ($CRIT_DAYS) must be below DOMAIN_WARN_DAYS ($WARN_DAYS)" >&2; exit 2; }
[[ "$TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-domain-expiry: refusing — RDAP_TIMEOUT must be a positive integer" >&2; exit 2; }
case "$RDAP_BASE_URL" in
  https://*) RDAP_PROTO=https ;;
  http://127.0.0.1*|http://localhost*) RDAP_PROTO=http ;;
  *) echo "push-domain-expiry: refusing — RDAP_BASE_URL must be https (http only on loopback)" >&2; exit 2 ;;
esac

# Derive default registered domains from the pull-check URLs: hostname
# reduced to its last two labels, deduplicated. Scheme, path, query and
# userinfo never leave this function.
derive_domains() {
  python3 - "$@" <<'PY'
import sys, urllib.parse
seen = set()
for raw in sys.argv[1:]:
    try:
        u = urllib.parse.urlsplit(raw)
    except Exception:
        continue
    if u.scheme not in ("http", "https") or not u.hostname:
        continue
    labels = u.hostname.rstrip(".").split(".")
    if len(labels) < 2:
        continue
    dom = ".".join(labels[-2:]).lower()
    if dom not in seen:
        seen.add(dom)
        print(dom)
PY
}

DOMAINS="${RDAP_DOMAINS:-}"
if [[ -z "${DOMAINS//[[:space:]]/}" ]]; then
  DOMAINS="$(derive_domains "${TWO_WEB_PROD_URL:-}" "${TWO_WEB_STAGING_URL:-}" | tr '\n' ' ')"
fi
[[ -n "${DOMAINS//[[:space:]]/}" ]] \
  || { echo "push-domain-expiry: refusing — no domains (set RDAP_DOMAINS or TWO_WEB_PROD_URL/TWO_WEB_STAGING_URL)" >&2; exit 2; }

read -r -a NAMES <<<"$DOMAINS"
for i in "${!NAMES[@]}"; do
  NAMES[i]="$(printf '%s' "${NAMES[i]}" | tr '[:upper:]' '[:lower:]')"
  [[ "${NAMES[i]}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$ ]] \
    || { echo "push-domain-expiry: refusing — bad domain '${NAMES[i]}' (want a registered domain, no port)" >&2; exit 2; }
done

days_left() {  # <domain> → prints whole days left, or FAIL
  local domain="$1"
  local body http
  body="$(mktemp "${TMPDIR:-/tmp}/rdap.XXXXXXXX")" || { echo "FAIL"; return; }
  # shellcheck disable=SC2064
  trap "rm -f '$body'" RETURN
  http="$("$CURL_BIN" -sS -L --max-redirs 3 \
    --proto "=$RDAP_PROTO" --proto-redir "=$RDAP_PROTO" \
    --max-time "$TIMEOUT_S" --max-filesize "$MAX_RDAP_BYTES" \
    -H 'Accept: application/rdap+json' \
    -o "$body" -w '%{http_code}' \
    "$RDAP_BASE_URL$domain" 2>/dev/null)" || { echo "FAIL"; return; }
  [[ "$http" == "200" ]] || { echo "FAIL"; return; }
  python3 - "$body" <<'PY' || echo "FAIL"
import json, math, sys
from datetime import datetime, timezone
try:
    with open(sys.argv[1], "rb") as fh:
        doc = json.load(fh)
    expiries = []
    for ev in doc.get("events") or []:
        if str(ev.get("eventAction", "")).lower() != "expiration":
            continue
        raw = str(ev.get("eventDate", "")).strip()
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        expiries.append(dt)
    if not expiries:
        print("FAIL"); sys.exit(0)
    delta = (min(expiries) - datetime.now(timezone.utc)).total_seconds()
    print(math.floor(delta / 86400))
except Exception:
    print("FAIL")
PY
}

worst_state=0   # 0 ok, 1 warn, 2 crit-or-error
declare -a warn_hits=() crit_hits=()
for d in "${NAMES[@]}"; do
  left="$(days_left "$d")"
  if [[ "$left" == "FAIL" ]] || [[ ! "$left" =~ ^-?[0-9]+$ ]]; then
    crit_hits+=("$d:unverifiable")
    worst_state=2
  elif (( left <= CRIT_DAYS )); then
    crit_hits+=("$d:${left}d")
    worst_state=2
  elif (( left <= WARN_DAYS )); then
    warn_hits+=("$d:${left}d")
    (( worst_state < 1 )) && worst_state=1
  fi
done

if (( worst_state == 2 )); then
  gatus_push platform_domain-expiry false "critical: ${crit_hits[*]} (crit<=${CRIT_DAYS}d)"
  exit 1
elif (( worst_state == 1 )); then
  gatus_push platform_domain-expiry false "warning: ${warn_hits[*]} (warn<=${WARN_DAYS}d)"
  exit 1
else
  gatus_push platform_domain-expiry true
fi
