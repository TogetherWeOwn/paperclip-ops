#!/usr/bin/env bash
# push-discord-bot.sh — cron: Discord bot gateway liveness → Gatus
# `platform_discord-bot` (declared in gatus-config.yaml as a push-only
# external endpoint, heartbeat 10m; this is the pusher it was missing).
#
# WHICH BOT: the production bot, today two-bot-next (Coolify). Its readiness
# listener serves `/readyz` (two-bot-next crates/bot/src/server.rs): HTTP 200
# only when every listed component is ready, otherwise 503. The body is a
# JSON `components` report (two-bot-next crates/core/src/health.rs):
#   {"components":[["process","ready"],["gateway","ready"],
#                  ["database","ready"],["token_invalid","ready"]],...}
# Component statuses are the fixed lowercase words `ready`|`starting`|`down`.
# `/health`/`/healthz` stay green while the gateway is down, so point
# DISCORD_BOT_HEALTH_URL at `/readyz`. (The `two-bot-dk` PULL endpoint probes
# TWO_BOT_DK_URL; keep the two URLs distinct so "process up" and "gateway
# connected" stay separate signals.) The legacy two-bot container is retired:
# its plain-text `ok` and `ready`/`gateway_connected`/`status`/
# `last_event_age_s` fields are NOT recognised — a 200 without a healthy
# `components` report is DOWN, so a proxy in front of a dead bot can never
# read green.
#
# SUCCESS = HTTP 200 within DISCORD_BOT_TIMEOUT_S (default/ceiling 3) AND a
# body this script recognises as healthy: a JSON object whose `components`
# array is non-empty, holds only `[name, ready|starting|down]` pairs, names
# `process`, `gateway`, `database` and `token_invalid`, and reports every
# component `ready`.
# Anything else — an empty body, an HTML captive/login page behind a proxy, a
# JSON array, JSON with no `components` report, legacy fields, malformed
# JSON — is DOWN. A 200 from a proxy in front of a dead bot must never read
# green.
#
# FAIL-CLOSED: unreachable, timeout, non-200, oversized (>64 KiB) or
# malformed → success=false and exit 1. Missing config refuses (exit 2) with
# zero pushes: the 10m heartbeat miss then pages, never a silent green.
#
# READ-ONLY: one bare GET, no auth header, no token. The health URL carries
# infrastructure addressing, so it lives only in the root-only 0600 env file,
# travels in a 0600 curl config (never argv — /proc/<pid>/cmdline is
# world-readable) and is never printed or pushed.
#
# UNTRUSTED INPUT: the response body is never echoed. The pushed error text is
# built only from fixed phrases plus a 3-digit HTTP code, a curl exit code,
# and component names/statuses from a fixed allow-list.
# _gatus_push.sh additionally truncates to MAX_PUSH_ERROR_CHARS.
#
# Precondition (operator): two-bot-next publishes no public port, so
# DISCORD_BOT_HEALTH_URL must be a URL reachable from node7b1ff338 (the same
# prerequisite as TWO_BOT_DK_URL in OPERATOR.md §2). Unreachable is reported
# truthfully as DOWN, not hidden.
#
# Env: GATUS_URL, GATUS_EXTERNAL_TOKEN (see _gatus_push.sh),
#   DISCORD_BOT_HEALTH_URL (required, http(s)),
#   DISCORD_BOT_TIMEOUT_S (default 3, 1..3), plus curl and python3.
# Cron (operator): */5 * * * * /opt/status-page/current/ops/case_658bf6b9/push-discord-bot.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gatus_push.sh
. "$HERE/_gatus_push.sh"

ENDPOINT_KEY="platform_discord-bot"
TIMEOUT_S="${DISCORD_BOT_TIMEOUT_S:-3}"
MAX_BODY_BYTES=65536

[[ -n "${DISCORD_BOT_HEALTH_URL:-}" ]] \
  || { echo "push-discord-bot: refusing — DISCORD_BOT_HEALTH_URL is unset" >&2; exit 2; }
[[ "$DISCORD_BOT_HEALTH_URL" =~ ^https?://[^[:space:]\"\\]+$ ]] \
  || { echo "push-discord-bot: refusing — DISCORD_BOT_HEALTH_URL must be an http(s) URL" >&2; exit 2; }
[[ "$TIMEOUT_S" =~ ^[1-3]$ ]] \
  || { echo "push-discord-bot: refusing — DISCORD_BOT_TIMEOUT_S must be an integer 1..3" >&2; exit 2; }
[[ -n "${GATUS_URL:-}" ]] \
  || { echo "push-discord-bot: refusing — GATUS_URL is unset" >&2; exit 2; }
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || { echo "push-discord-bot: refusing — GATUS_EXTERNAL_TOKEN is unset" >&2; exit 2; }
command -v curl >/dev/null 2>&1 \
  || { echo "push-discord-bot: refusing — curl not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 \
  || { echo "push-discord-bot: refusing — python3 required" >&2; exit 2; }

work_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/discord_bot_push.XXXXXXXX")" \
  || { echo "push-discord-bot: refusing — could not create a private temp directory" >&2; exit 2; }
trap 'rm -rf "$work_dir"' EXIT
cfg="$(umask 077; mktemp "$work_dir/curlcfg.XXXXXXXX")" \
  || { echo "push-discord-bot: refusing — could not create a curl config file" >&2; exit 2; }
body_file="$work_dir/body"
{
  printf 'url = "%s"\n' "$DISCORD_BOT_HEALTH_URL"
  printf 'output = "%s"\n' "$body_file"
  printf 'write-out = "%%{http_code}"\n'
  printf 'max-time = "%s"\n' "$TIMEOUT_S"
  printf 'max-filesize = "%s"\n' "$MAX_BODY_BYTES"
  printf 'silent\n'
} > "$cfg"
chmod 0600 "$cfg"

# -q must be the first curl argument: ignore account .curlrc defaults that
# could add methods, auth or URLs. Curl stderr can echo the URL, so discard it;
# only its exit code and numeric status survive into the verdict.
http="$(curl -q --config "$cfg" 2>/dev/null)"
curl_rc=$?

# One evaluator pass (stdlib only). Prints "<true|false>\t<message>".
verdict="$(python3 - "$curl_rc" "$http" "$body_file" "$MAX_BODY_BYTES" <<'PY'
import json, re, sys

curl_rc, http, body_path, max_body = sys.argv[1:5]
curl_rc, max_body = int(curl_rc), int(max_body)
# Fixed vocabulary from the reviewed Next readiness contract
# (two-bot-next crates/core/src/health.rs, crates/bot/src/server.rs).
# Only these names and statuses may ever reach a push or stdout.
KNOWN_COMPONENTS = {"process", "gateway", "database", "token_invalid"}
REQUIRED_COMPONENTS = {"process", "gateway", "database", "token_invalid"}
STATUSES = {"ready", "starting", "down"}
# Legacy one-word /readyz reasons, still allow-listed on the non-200 path so
# a degraded answer names its cause without ever echoing body text.
REASONS = {"gateway_disconnected", "database_unreachable", "disconnected",
           "connecting", "starting", "resuming", "reconnecting"}

def verdict(ok, msg):
    print(("true" if ok else "false") + "\t" + msg)
    sys.exit(0)

def read_body():
    try:
        with open(body_path, "rb") as h:
            return h.read(max_body + 1)
    except OSError:
        return b""

def valid_entry(entry):
    return (isinstance(entry, list) and len(entry) == 2
            and isinstance(entry[0], str) and isinstance(entry[1], str)
            and entry[1] in STATUSES)

def not_ready_reason(doc):
    # " reason=..." built only from the fixed vocabulary, else "".
    if not isinstance(doc, dict):
        return ""
    comps = doc.get("components")
    if not isinstance(comps, list):
        return ""
    bad = set()
    for entry in comps:
        if not valid_entry(entry):
            return ""
        name, status = entry
        if name not in KNOWN_COMPONENTS:
            return ""
        if status != "ready":
            bad.add(name)
    if not bad:
        return ""
    return " reason=not-ready:" + ",".join(sorted(bad))

if curl_rc == 63:
    verdict(False, f"discord-bot: response body exceeds {max_body} bytes (fail-closed)")
if curl_rc != 0:
    verdict(False, f"discord-bot: health endpoint unreachable or timed out (curl exit {curl_rc})")
if not re.fullmatch(r"[0-9]{3}", http):
    verdict(False, "discord-bot: no usable HTTP status from health endpoint")

raw = read_body()
if len(raw) > max_body:
    verdict(False, f"discord-bot: response body exceeds {max_body} bytes (fail-closed)")
try:
    text = raw.decode("utf-8", "strict").strip()
except UnicodeDecodeError:
    verdict(False, "discord-bot: response body is invalid UTF-8 (fail-closed)")

if http != "200":
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    suffix = not_ready_reason(doc)
    if not suffix:
        word = text.lower()
        suffix = f" reason={word}" if word in REASONS else ""
    verdict(False, f"discord-bot: health endpoint answered HTTP {http}{suffix}")

if not text.startswith("{"):
    verdict(False, "discord-bot: 200 body is not a JSON components report (fail-closed)")
try:
    doc = json.loads(text)
except ValueError:
    verdict(False, "discord-bot: 200 body is malformed JSON (fail-closed)")
if not isinstance(doc, dict):
    verdict(False, "discord-bot: 200 body is not a JSON object (fail-closed)")
comps = doc.get("components")
if not isinstance(comps, list) or not comps:
    verdict(False, "discord-bot: 200 JSON body carries no components report (fail-closed)")
names = []
for entry in comps:
    if not valid_entry(entry):
        verdict(False, "discord-bot: 200 components entry is not a [name, ready|starting|down] pair (fail-closed)")
    names.append(entry[0])
if not REQUIRED_COMPONENTS.issubset(names):
    verdict(False, "discord-bot: components report is missing a required component (fail-closed)")
not_ready = sorted({name if name in KNOWN_COMPONENTS else "unrecognised"
                    for name, status in comps if status != "ready"})
if not_ready:
    verdict(False, f"discord-bot: component not ready: {','.join(not_ready)}")
verdict(True, "")
PY
)" || verdict=$'false\tdiscord-bot: evaluator failure (fail-closed)'

success="${verdict%%$'\t'*}"
message="${verdict#*$'\t'}"

if [[ "$success" == "true" ]]; then
  gatus_push "$ENDPOINT_KEY" true || {
    echo "push-discord-bot: push to Gatus failed" >&2
    exit 1
  }
  echo "push-discord-bot: healthy"
  exit 0
fi

gatus_push "$ENDPOINT_KEY" false "$message" || true
echo "push-discord-bot: FAIL-CLOSED: $message" >&2
exit 1
