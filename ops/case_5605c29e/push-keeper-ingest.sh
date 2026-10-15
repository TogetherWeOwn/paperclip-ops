#!/usr/bin/env bash
# push-keeper-ingest.sh — cron: Keeper ingest freshness → Gatus.
#
# case_5605c29e (parent case_66436e78 item 5, dependencies: Keeper ingest freshness).
# Source-only: no host deploy rides this change; the operator installs the
# cron line separately (see OPERATOR.md).
#
# What it does, once per run (cron: */5 * * * *):
#   1. GETs the Keeper freshness surface in KEEPER_FRESHNESS_URL (full URL,
#      no path invented here), bounded by KEEPER_PROBE_TIMEOUT_S
#      (default 15). The surface answers JSON carrying the latest
#      successful ingest timestamp under `last_successful_ingest`
#      (ISO-8601 UTC or unix epoch seconds).
#   2. Compares ingest lag (now minus that timestamp) against the warn/crit
#      bounds KEEPER_WARN_LAG_S (default 1800) and KEEPER_CRIT_LAG_S
#      (default 3600).
#   3. Pushes the Gatus external endpoint `keeper_ingest-freshness`
#      through the shared ../case_658bf6b9/_gatus_push.sh helper. Lag below
#      warn pushes success=true; lag at/over warn pushes success=false
#      naming "warning"; lag at/over crit pushes success=false naming
#      "critical". The message carries lag seconds + the bound only.
#
# FAIL-CLOSED: any non-200, curl failure, timeout, unparseable body,
# missing timestamp, or clock-anomaly timestamp pushes success=false
# (exit 1) — a blind or stale monitor never reads green. WARN also pushes
# DOWN on the single-boolean endpoint so the page fires early (same shape
# as the TLS-expiry pusher); the message escalates warning → critical.
# Missing Gatus env, missing helper/binaries, bad thresholds, or a
# production-like probe target refuses (exit 2) before any health claim —
# except a missing/unparseable KEEPER_FRESHNESS_URL, which pushes DOWN
# (exit 1): an unmonitored Keeper must not read green.
#
# STAGING/TEST-ONLY: the probe target comes exclusively from
# KEEPER_FRESHNESS_URL (operator-filled from the staging/test Keeper
# surface). A host containing "prod" (any case) is never dialed: the
# script pushes DOWN (exit 1) without a request. Production Keeper is
# never probed from this script, by typo or by rotation.
#
# READ-ONLY / NO SECRETS: the probe is one GET; it never writes Keeper.
# Push messages carry lag seconds and the bound — never payload rows,
# never the URL, never a bearer. When KEEPER_FRESHNESS_BEARER is set it
# travels in a 0600 curl config file, never argv (/proc/<pid>/cmdline is
# world-readable). The reply lands in a 0600 file too: it may hold Keeper
# payload content, which must never reach the status page or cron log.
# This script never prints a bearer.
#
# Env:
#   KEEPER_FRESHNESS_URL     full freshness-surface URL (required,
#                            staging/test only — see guard above)
#   KEEPER_FRESHNESS_BEARER  optional read bearer for that surface
#   KEEPER_WARN_LAG_S        warn bound, seconds (default 1800)
#   KEEPER_CRIT_LAG_S        crit bound, seconds (default 3600)
#   KEEPER_PROBE_TIMEOUT_S   probe timeout, seconds (default 15)
#   GATUS_URL, GATUS_EXTERNAL_TOKEN (via _gatus_push.sh)
#   GATUS_PUSH_TIMEOUT       Gatus push timeout, seconds (default 10)
#
# Exit: 0 fresh ingest pushed healthy; 1 DOWN pushed (stale, missing,
# malformed, clock anomaly, probe failure, or staging-guard breach);
# 2 refusal (bad env, bad thresholds, missing helper/tooling) before any
# claim. `push-keeper-ingest.sh [-h|--help]`
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../case_658bf6b9/_gatus_push.sh
GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"
[[ -f "$GATUS_HELPER" ]] \
  || { echo "push-keeper-ingest: refusing — shared helper $GATUS_HELPER missing" >&2; exit 2; }
# shellcheck disable=SC1090
. "$GATUS_HELPER"

for arg in "$@"; do
  case "$arg" in
    -h|--help) sed -n '2,42p' "$0"; exit 0 ;;
    *) echo "push-keeper-ingest: refusing — unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

command -v curl >/dev/null 2>&1 \
  || { echo "push-keeper-ingest: refusing — curl not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-keeper-ingest: refusing — python3 required" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-keeper-ingest: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-keeper-ingest: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }

WARN_S="${KEEPER_WARN_LAG_S:-1800}"
CRIT_S="${KEEPER_CRIT_LAG_S:-3600}"
TIMEOUT_S="${KEEPER_PROBE_TIMEOUT_S:-15}"
[[ "$WARN_S" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-keeper-ingest: refusing — KEEPER_WARN_LAG_S must be a positive integer" >&2; exit 2; }
[[ "$CRIT_S" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-keeper-ingest: refusing — KEEPER_CRIT_LAG_S must be a positive integer" >&2; exit 2; }
(( CRIT_S > WARN_S )) \
  || { echo "push-keeper-ingest: refusing — KEEPER_CRIT_LAG_S ($CRIT_S) must exceed KEEPER_WARN_LAG_S ($WARN_S)" >&2; exit 2; }
[[ "$TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-keeper-ingest: refusing — KEEPER_PROBE_TIMEOUT_S must be a positive integer" >&2; exit 2; }

# Missing or unparseable URL pushes DOWN (fail-closed): an unmonitored
# Keeper must not read green. The heartbeat would page too, but the
# explicit DOWN names the cause on the status page.
[[ -n "${KEEPER_FRESHNESS_URL:-}" ]] \
  || { gatus_push keeper_ingest-freshness false "KEEPER_FRESHNESS_URL is unset"; exit 1; }

probe_host="$(python3 - "$KEEPER_FRESHNESS_URL" <<'PY' 2>/dev/null || echo "UNPARSEABLE"
import sys, urllib.parse
try:
    u = urllib.parse.urlsplit(sys.argv[1])
except Exception:
    print("UNPARSEABLE"); sys.exit(0)
if u.scheme not in ("http", "https") or not u.hostname:
    print("UNPARSEABLE"); sys.exit(0)
print(u.hostname)
PY
)"
[[ "$probe_host" != "UNPARSEABLE" && -n "$probe_host" ]] \
  || { gatus_push keeper_ingest-freshness false "freshness URL unparseable"; exit 1; }

# Staging/test-only guard (mirrors the Neon pusher): never dial a
# production-like target. No request leaves this host on this path.
shopt -s nocasematch
if [[ "$probe_host" == *prod* ]]; then
  gatus_push keeper_ingest-freshness false "refusing — production-like target (host contains 'prod')"
  exit 1
fi
shopt -u nocasematch

# --- probe ---------------------------------------------------------------
# The bearer (when set) travels in a 0600 curl config — never argv. The
# reply lands in a 0600 file: it may hold Keeper payload content.
probe_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/keeper_ingest.XXXXXXXX")" \
  || { echo "push-keeper-ingest: refusing — no private temp dir" >&2; exit 2; }
# Top level: RETURN fires only in functions, so EXIT removes the dir
# holding the credential-bearing config and the payload reply on exit.
# shellcheck disable=SC2064
trap "rm -rf '$probe_dir'" EXIT
body_file="$probe_dir/body.txt"
probe_cfg="$probe_dir/curlcfg.probe"
: > "$body_file"
chmod 0600 "$body_file"
{
  printf 'url = "%s"\n' "$KEEPER_FRESHNESS_URL"
  printf 'request = "GET"\n'
  printf 'header = "Accept: application/json"\n'
  if [[ -n "${KEEPER_FRESHNESS_BEARER:-}" ]]; then
    printf 'header = "Authorization: Bearer %s"\n' "$KEEPER_FRESHNESS_BEARER"
  fi
  printf 'output = "%s"\n' "$body_file"
  printf 'write-out = "%%{http_code}"\n'
  printf 'max-time = "%s"\n' "$TIMEOUT_S"
  printf 'silent\nshow-error\n'
} > "$probe_cfg"
chmod 0600 "$probe_cfg"

probe_out="$(curl --config "$probe_cfg" 2>&1)" \
  || {
    gatus_push keeper_ingest-freshness false "probe failed: curl exit (fail-closed DOWN)" || true
    exit 1
  }
http="${probe_out%% *}"

if [[ "$http" != "200" ]]; then
  gatus_push keeper_ingest-freshness false "ingest probe HTTP $http (fail-closed DOWN)" || true
  exit 1
fi

# --- evaluate -------------------------------------------------------------
# The python pass prints one verdict line only — "<kind> <detail>" where
# kind is one of ok|warning|critical|missing|malformed|clock-anomaly.
# It prints lag seconds, never payload content.
verdict="$(python3 - "$body_file" "$WARN_S" "$CRIT_S" <<'PY' 2>&1
import datetime, json, sys
body_path, warn_s, crit_s = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    doc = json.load(open(body_path, encoding="utf-8"))
except Exception as exc:
    print(f"malformed unparseable reply ({exc})")
    sys.exit(0)
if not isinstance(doc, dict):
    print("malformed reply is not a JSON object")
    sys.exit(0)
raw = doc.get("last_successful_ingest")
if raw is None:
    print("missing no last_successful_ingest in reply")
    sys.exit(0)
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
ts = None
if isinstance(raw, (int, float)):
    ts = float(raw)
elif isinstance(raw, str):
    text = raw.strip()
    if text.isdigit():
        ts = float(text)
    else:
        try:
            iso = text.replace("Z", "+00:00")
            ts = datetime.datetime.fromisoformat(iso).timestamp()
        except ValueError:
            ts = None
if ts is None:
    print("malformed last_successful_ingest is not ISO-8601 or epoch")
    sys.exit(0)
lag = now - ts
if lag < -300:
    print(f"clock-anomaly ingest timestamp {int(-lag)}s in the future")
    sys.exit(0)
lag_s = max(0, int(lag))
if lag_s >= crit_s:
    print(f"critical ingest lag {lag_s}s at/over crit {crit_s}s")
elif lag_s >= warn_s:
    print(f"warning ingest lag {lag_s}s at/over warn {warn_s}s")
else:
    print(f"ok ingest lag {lag_s}s under warn {warn_s}s")
PY
)"
verdict_kind="${verdict%% *}"

if [[ "$verdict_kind" == "ok" ]]; then
  gatus_push keeper_ingest-freshness true || exit 1
  exit 0
fi
gatus_push keeper_ingest-freshness false "keeper $verdict (fail-closed DOWN)" || true
exit 1
