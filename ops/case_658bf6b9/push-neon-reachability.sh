#!/usr/bin/env bash
# push-neon-reachability.sh — cron: Neon staging-branch reachability → Gatus
# `platform_neon-staging`.
#
# Two stages against the STAGING branch only: (1) a TCP connect to the
# parsed host:port, then (2) a lightweight `SELECT 1` through psql over a
# TLS-required session. Either stage failing pushes success=false (exit 1):
# probe failure = DOWN, fail closed. success=true only when `SELECT 1`
# returns exactly one row containing `1`.
#
# STAGING-ONLY GUARD: the connection string comes exclusively from
# NEON_STAGING_DATABASE_URL (operator-filled from the staging
# Hyperdrive/binding value). A missing value pushes DOWN (exit 1) — an
# unmonitored staging branch must not read green. The dialed host must be
# the pinned staging compute endpoint: NEON_STAGING_ENDPOINT_ID holds the
# endpoint's first DNS label without any "-pooler" suffix (e.g.
# "ep-abc123xyz" for ep-abc123xyz.us-east-2.aws.neon.tech); a host whose
# first label is neither the pin nor "<pin>-pooler" refuses to dial and
# pushes DOWN (exit 1). Neon hostnames never carry the branch name, so a
# production-branch URL pasted by typo or left after a rotation can only
# dial when its endpoint ID is the pinned staging one. A host or database
# name containing "prod" (any case) is kept only as a backstop and likewise
# refuses to dial — production is never probed from this script.
#
# NON-DISCLOSURE: the URL is parsed in-process; the password travels only
# in PGPASSWORD (process env, same-user/root only) and never in argv
# (/proc/<pid>/cmdline is world-readable). psql is invoked with -h/-p/-U/-d
# flags — no URL on the command line. Logs and push text carry the bare
# hostname only, never userinfo, password, or query string.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh),
# NEON_STAGING_DATABASE_URL (required, staging branch only),
# NEON_STAGING_ENDPOINT_ID (required: pinned staging endpoint first label,
# without "-pooler"), NEON_TCP_TIMEOUT (default 10, seconds, also caps
# stage 2), PSQL_BIN (default psql), PYTHON_BIN (default python3),
# TIMEOUT_BIN (default timeout, coreutils).
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-neon-reachability.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

PSQL_BIN="${PSQL_BIN:-psql}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TIMEOUT_BIN="${TIMEOUT_BIN:-timeout}"
TCP_TIMEOUT="${NEON_TCP_TIMEOUT:-10}"

command -v "$PSQL_BIN" >/dev/null 2>&1 \
  || { echo "push-neon-reachability: refusing — no '$PSQL_BIN' on PATH (operator install?)" >&2; exit 2; }
command -v "$PYTHON_BIN" >/dev/null 2>&1 \
  || { echo "push-neon-reachability: refusing — no '$PYTHON_BIN' on PATH" >&2; exit 2; }
command -v "$TIMEOUT_BIN" >/dev/null 2>&1 \
  || { echo "push-neon-reachability: refusing — no '$TIMEOUT_BIN' on PATH (operator install?)" >&2; exit 2; }
[[ "$TCP_TIMEOUT" =~ ^[1-9][0-9]*$ ]] \
  || { echo "push-neon-reachability: refusing — NEON_TCP_TIMEOUT must be a positive integer" >&2; exit 2; }

[[ -n "${NEON_STAGING_DATABASE_URL:-}" ]] \
  || { gatus_push platform_neon-staging false "NEON_STAGING_DATABASE_URL is unset"; exit 1; }

# Parse the URL in-process into libpq parts. Prints shell-quoted assignments
# or fails; nothing secret is echoed anywhere else.
parse_url() {
  "$PYTHON_BIN" - "$1" <<'PY'
import shlex, sys, urllib.parse
try:
    u = urllib.parse.urlsplit(sys.argv[1])
except Exception:
    sys.exit(3)
if u.scheme not in ("postgres", "postgresql"):
    sys.exit(3)
host = u.hostname or ""
port = u.port or 5432
user = urllib.parse.unquote(u.username or "")
password = urllib.parse.unquote(u.password or "")
db = (u.path or "").lstrip("/") or ""
if not host or not user or not db:
    sys.exit(3)
print("NEON_HOST=" + shlex.quote(host))
print("NEON_PORT=" + shlex.quote(str(port)))
print("NEON_USER=" + shlex.quote(user))
print("NEON_PASSWORD=" + shlex.quote(password))
print("NEON_DB=" + shlex.quote(urllib.parse.unquote(db)))
PY
}

parsed="$(parse_url "$NEON_STAGING_DATABASE_URL")" \
  || { gatus_push platform_neon-staging false "staging database URL unparseable"; exit 1; }
eval "$parsed"
# shellcheck disable=SC2154  # set by the eval above
{
  [[ -n "${NEON_HOST:-}" && -n "${NEON_USER:-}" && -n "${NEON_DB:-}" ]]
} || { gatus_push platform_neon-staging false "staging database URL incomplete"; exit 1; }

[[ -n "${NEON_STAGING_ENDPOINT_ID:-}" ]] \
  || { gatus_push platform_neon-staging false "NEON_STAGING_ENDPOINT_ID is unset"; exit 1; }
host_label="${NEON_HOST%%.*}"
[[ "$host_label" == "${NEON_STAGING_ENDPOINT_ID}-pooler" ]] && host_label="${NEON_STAGING_ENDPOINT_ID}"
if [[ "$host_label" != "$NEON_STAGING_ENDPOINT_ID" ]]; then
  gatus_push platform_neon-staging false "refusing — $NEON_HOST is not the pinned staging endpoint"
  exit 1
fi
shopt -s nocasematch
if [[ "$NEON_HOST" == *prod* || "$NEON_DB" == *prod* ]]; then
  gatus_push platform_neon-staging false "refusing — production-like target (host/db contains 'prod')"
  exit 1
fi
shopt -u nocasematch

# Stage 1: TCP connect (hostname only in any diagnostic).
"$PYTHON_BIN" - "$NEON_HOST" "$NEON_PORT" "$TCP_TIMEOUT" <<'PY' 2>/dev/null \
  || { gatus_push platform_neon-staging false "tcp connect failed: $NEON_HOST"; exit 1; }
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
s = socket.create_connection((host, port), timeout=timeout)
s.close()
PY

# Stage 2: SELECT 1 over a TLS-required session. Password via env only.
# No PGOPTIONS: pooled endpoints (PgBouncer) reject startup parameters such
# as statement_timeout, so the run is capped with `timeout` instead
# (PGCONNECT_TIMEOUT stays: it is client-side and pooler-safe).
out="$(PGHOST="$NEON_HOST" PGPORT="$NEON_PORT" PGUSER="$NEON_USER" \
  PGPASSWORD="$NEON_PASSWORD" PGDATABASE="$NEON_DB" PGSSLMODE=require \
  PGCONNECT_TIMEOUT="$TCP_TIMEOUT" \
  "$TIMEOUT_BIN" "$TCP_TIMEOUT" "$PSQL_BIN" -h "$NEON_HOST" -p "$NEON_PORT" \
  -U "$NEON_USER" -d "$NEON_DB" \
  -tA -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null)" \
  || { gatus_push platform_neon-staging false "SELECT 1 failed: $NEON_HOST"; exit 1; }
[[ "$(tr -d '[:space:]' <<<"$out")" == "1" ]] \
  || { gatus_push platform_neon-staging false "SELECT 1 unexpected result: $NEON_HOST"; exit 1; }

gatus_push platform_neon-staging true
