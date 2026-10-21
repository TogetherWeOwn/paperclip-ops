#!/usr/bin/env bash
# fetch-beszel-normalized.sh — Beszel hub API -> normalized JSON on stdout.
#
# Producer half of the Beszel bridge: authenticates to the Beszel hub
# (PocketBase), fetches the systems list, the newest stats record per
# system and the coarse history, then hands the three payloads to
# beszel_adapter.py. Wire it as the consumer's source:
#   BESZEL_SOURCE_CMD=".../fetch-beszel-normalized.sh" push-beszel-hosts.sh
#
# Env (from the root-only 0600 env file the operator installs):
#   BESZEL_HUB_URL      e.g. https://beszel.example.com, or
#                       http://127.0.0.1:8090 for a loopback hub (no path is
#                       invented beyond the PocketBase collection API below;
#                       plain http to a non-loopback host is refused)
#   BESZEL_IDENTITY     hub user email for auth-with-password
#   BESZEL_PASSWORD     that user's password (read-only scope suffices)
#   BESZEL_FETCH_TIMEOUT  per-request seconds (default 15)
#   BESZEL_MAX_STATS_AGE_SEC  adapter freshness bound (default 600)
#   BESZEL_HISTORY_DAYS history window for the forecast (default 30)
#
# NON-DISCLOSURE: password and auth token travel only in 0600 curl
# config / data files inside a 0700 temp dir, never argv
# (/proc/<pid>/cmdline is world-readable) and never echoed. The temp
# dir is removed on every exit path.
# FAIL-CLOSED: any auth/fetch/adapter failure exits 2 with nothing on
# stdout, so the consumer refuses instead of scoring partial data.
#
# Exit: 0 normalized JSON on stdout; 2 refusal.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

refuse() { echo "fetch-beszel-normalized: refusing — $1" >&2; exit 2; }

command -v curl >/dev/null 2>&1 || refuse "curl required"
command -v jq >/dev/null 2>&1 || refuse "jq required"
command -v python3 >/dev/null 2>&1 || refuse "python3 required"
[[ -f "$HERE/beszel_adapter.py" ]] || refuse "beszel_adapter.py missing next to this script"
[[ -n "${BESZEL_HUB_URL:-}" ]] || refuse "BESZEL_HUB_URL is unset"
[[ -n "${BESZEL_IDENTITY:-}" ]] || refuse "BESZEL_IDENTITY is unset"
[[ -n "${BESZEL_PASSWORD:-}" ]] || refuse "BESZEL_PASSWORD is unset"
# The auth body below reads these through jq's env (never argv). Export
# explicitly: if this script is ever sourced after plain assignments,
# unexported vars would read as null and every fetch would refuse.
export BESZEL_IDENTITY BESZEL_PASSWORD
HUB="${BESZEL_HUB_URL%/}"
# Plain http to a remote hub sends the password and token in the clear and
# lets an on-path attacker answer with a hostile token (see the charset
# gate below). Refuse unless the hub is https or loopback. The authority
# is parsed, not globbed: any userinfo ("@" — e.g. "127.0.0.1@host" or
# "user:pass@127.0.0.1") is refused outright, bracketed literals are
# unwrapped before comparison (so "[::1]" matches but "::1" never
# reaches curl unbracketed), and 127/8 must be a strict dotted quad with
# validated octets (so "127.evil.example" and "2130706433" refuse).
hub_scheme="${HUB%%://*}"
if [[ "${hub_scheme,,}" != "https" ]]; then
  hub_rest="${HUB#*://}"
  case "$hub_rest" in
    *@*) refuse "BESZEL_HUB_URL must not embed credentials (userinfo)" ;;
  esac
  if [[ "$hub_rest" == "["* ]]; then
    hub_host="${hub_rest%%]*}"
    hub_host="${hub_host#[}"
    case "$hub_host" in
      ::1) ;;
      *) refuse "BESZEL_HUB_URL must be https unless the hub is loopback" ;;
    esac
  else
    hub_host="${hub_rest%%[/:]*}"
    case "${hub_host,,}" in
      localhost) ;;
      *)
        if [[ "$hub_host" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] \
          && (( 10#${BASH_REMATCH[1]} == 127 && 10#${BASH_REMATCH[2]} <= 255 \
            && 10#${BASH_REMATCH[3]} <= 255 && 10#${BASH_REMATCH[4]} <= 255 )); then
          :
        else
          refuse "BESZEL_HUB_URL must be https unless the hub is loopback"
        fi
        ;;
    esac
  fi
fi
TIMEOUT="${BESZEL_FETCH_TIMEOUT:-15}"
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || refuse "BESZEL_FETCH_TIMEOUT must be a whole number"
MAX_AGE="${BESZEL_MAX_STATS_AGE_SEC:-600}"
[[ "$MAX_AGE" =~ ^[0-9]+(\.[0-9]+)?$ ]] || refuse "BESZEL_MAX_STATS_AGE_SEC must be numeric"
HISTORY_DAYS="${BESZEL_HISTORY_DAYS:-30}"
[[ "$HISTORY_DAYS" =~ ^[0-9]+$ ]] || refuse "BESZEL_HISTORY_DAYS must be a whole number"

work="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/beszel_fetch.XXXXXXXX")" \
  || refuse "no private temp dir"
# Top level: RETURN fires only in functions, so EXIT removes the dir
# holding the credentials on every path, including refusals.
# shellcheck disable=SC2064
trap "rm -rf '$work'" EXIT
chmod 0700 "$work"

# --- authenticate (password in a 0600 data file, token into a variable) -----
# Password via jq's env, never --arg: --arg values are argv and would sit
# in /proc/<pid>/cmdline for the life of the jq process.
jq -n '{identity: env.BESZEL_IDENTITY, password: env.BESZEL_PASSWORD}' \
  > "$work/auth.json" || refuse "cannot build auth body"
chmod 0600 "$work/auth.json"
{
  printf 'url = "%s/api/collections/users/auth-with-password"\n' "$HUB"
  printf 'header = "Content-Type: application/json"\n'
  printf 'data-binary = "@%s/auth.json"\n' "$work"
  printf 'max-time = "%s"\nsilent\nshow-error\nfail\n' "$TIMEOUT"
} > "$work/authcfg"
chmod 0600 "$work/authcfg"
auth_resp="$(curl --config "$work/authcfg" 2>/dev/null)" || refuse "Beszel auth failed"
token="$(jq -r '.token // empty' <<<"$auth_resp" 2>/dev/null)"
[[ -n "$token" ]] || refuse "Beszel auth returned no token"
# The token is interpolated into a curl config below, where a quote plus a
# newline would inject arbitrary curl directives (e.g. output = <path>)
# running with this account's privileges. A real PocketBase token is
# base64url/dot only; refuse anything outside a tight charset instead.
[[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] \
  || refuse "Beszel auth returned a token with unexpected characters"
# The password body has served its purpose; do not keep it around.
rm -f "$work/auth.json"

# --- fetch helper: token only in the 0600 config ------------------------------
fetch() { # $1 = path+query, $2 = output file
  {
    printf 'url = "%s%s"\n' "$HUB" "$1"
    printf 'header = "Authorization: Bearer %s"\n' "$token"
    printf 'max-time = "%s"\nsilent\nshow-error\nfail\n' "$TIMEOUT"
  } > "$work/fetchcfg"
  chmod 0600 "$work/fetchcfg"
  curl --config "$work/fetchcfg" > "$2" 2>/dev/null
}

fetch "/api/collections/systems/records?perPage=500" "$work/systems.json" \
  || refuse "systems fetch failed"
jq -e '(.items // .) | type == "array" and length > 0' "$work/systems.json" >/dev/null 2>&1 \
  || refuse "systems payload empty or unreadable"

# Newest 1m stats per system: one bounded request per enrolled system id,
# merged into a single envelope for the adapter.
: > "$work/stats-parts.jsonl"
while read -r sid; do
  [[ -n "$sid" ]] || continue
  enc_sid="$(jq -rn --arg v "$sid" '$v | @uri')"
  fetch "/api/collections/system_stats/records?perPage=1&sort=-created&filter=system%3D%27${enc_sid}%27%26%26type%3D%271m%27" \
    "$work/part.json" || refuse "latest-stats fetch failed for a system"
  jq -c '(.items // .)[0] // empty' "$work/part.json" >> "$work/stats-parts.jsonl" 2>/dev/null \
    || refuse "latest-stats payload unreadable"
done < <(jq -r '(.items // .)[].id // empty' "$work/systems.json")
jq -s '{items: .}' "$work/stats-parts.jsonl" > "$work/stats-latest.json" \
  || refuse "cannot merge latest stats"

# Coarse history for the disk forecast (480m buckets; the adapter tolerates
# any type, and perPage=500 covers HISTORY_DAYS at 3 points/day/host for a
# small fleet — newest-first sort keeps the freshest window on truncation;
# the adapter re-sorts by time). A truncated window only weakens the
# forecast, never scores.
since="$(date -u -d "-${HISTORY_DAYS} days" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)" \
  || refuse "cannot compute history window"
enc_since="$(jq -rn --arg v "$since" '$v | @uri')"
fetch "/api/collections/system_stats/records?perPage=500&sort=-created&filter=type%3D%27480m%27%26%26created%3E%3D%27${enc_since}%27" \
  "$work/stats-history.json" || refuse "history fetch failed"

python3 "$HERE/beszel_adapter.py" --max-age "$MAX_AGE" "$work" \
  || refuse "adapter refused the fetched payloads"
