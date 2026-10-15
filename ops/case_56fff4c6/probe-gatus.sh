#!/usr/bin/env bash
# ===========================================================================
# probe-gatus.sh — off-host Gatus liveness probe (case_56fff4c6).
# ---------------------------------------------------------------------------
# THE FAILURE THIS EXISTS FOR. Gatus watches everything, and nothing watches
# Gatus: if the Gatus container on node7b1ff338 dies, every external-endpoint
# heartbeat goes stale silently — the status page cannot report its own
# death. The on-call audit on case_66436e78 lists "an off-host heartbeat (Gatus
# itself)" as deliverable 7; sibling case_93aed7ff owns the one-time staging
# drill, this file owns the persistent probe.
#
# WHAT IT DOES. Runs OFF the status-page host (CI runners / a second host —
# never node7b1ff338's /opt/status-page stack): GETs the Gatus health URL, then
# pushes the verdict into the `platform_gatus-heartbeat` external endpoint
# through the shared _gatus_push.sh transport. A missed push fails closed
# (Gatus marks the endpoint stale/DOWN, never healthy).
#
# THE MONITOR-OF-MONITOR PARADOX, STATED PLAINLY. When Gatus itself is down,
# no Gatus alert can fire — the push in step 2 fails too. So a failed probe
# ALSO (a) exits non-zero, turning the calling workflow red where GitHub
# itself notifies, and (b) POSTs a short direct notice to a Discord webhook
# when one is configured, bypassing Gatus entirely. The Gatus endpoint is
# the dashboard record; the exit code + direct notice are the actual page.
#
# THREE RULES THIS FILE ENCODES, EACH BECAUSE THE OBVIOUS VERSION IS WRONG.
#
# 1. THE PROBED URL IS ENV, NOT A DEFAULT. An upstream `/health` path baked
#    in here is a claim about a Gatus version this repo does not pin at the
#    probe site; if upstream renames it, a defaulted probe checks the wrong
#    thing and reports DOWN forever (or a 404 page as UP). The operator sets
#    GATUS_HEALTH_URL once (OPERATOR.md says how to confirm it with curl
#    first) and a missing URL refuses before any network.
#
# 2. A PROBE THAT CANNOT AUTHENTICATE MUST FAIL, NOT SKIP. Behind Cloudflare
#    Access an unauthenticated GET returns the login page (302/200 with no
#    relation to origin health) — reading that as UP is a false green, and
#    skipping the tick when tokens are absent is a silent pass. Absent
#    GATUS_URL/token refuses (exit 2, zero pushes); a failed probe pushes
#    success=false best-effort and exits 1.
#
# 3. SECRETS NEVER REACH ARGV. Same transport as _gatus_push.sh and
#    test-akmatori-alert.sh: Bearer/CF tokens travel in 0600 curl config
#    files (/proc/<pid>/cmdline is world-readable). The suite pins this with
#    a curl shim that records argv.
#
# EXIT CODES — distinct so a caller can tell the four apart.
#   0  Gatus healthy, heartbeat pushed success=true
#   1  PROBE FAILED (Gatus down or unhealthy): success=false pushed
#      best-effort, direct notice sent when configured. THIS IS A FINDING.
#   2  refused (bad usage, missing env, bad URL). Zero pushes.
#   5  UNMEASURED: push transport missing (no _gatus_push.sh, no curl).
#      NOT green, NOT a probe verdict.
#
# MACHINE CONTRACT. Stdout is one line: `UP <http-code>` or
#   `DOWN <http-code| curl-error>`. Stderr is human prose and may drift.
# ===========================================================================
set -uo pipefail

ME="$(basename "${BASH_SOURCE[0]}")"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENDPOINT_KEY="platform_gatus-heartbeat"
HEALTH_TIMEOUT="${GATUS_PROBE_TIMEOUT:-15}"
MAX_ERR_CHARS=500

die() { echo "$ME: refusing — $*" >&2; exit 2; }

[[ -n "${GATUS_HEALTH_URL:-}" ]] \
  || die "GATUS_HEALTH_URL is unset (operator sets it per OPERATOR.md §1)"
[[ "$GATUS_HEALTH_URL" =~ ^https?:// ]] \
  || die "GATUS_HEALTH_URL must be an http(s) URL"
[[ -n "${GATUS_URL:-}" ]] \
  || die "GATUS_URL is unset (heartbeat push target)"
[[ -n "${GATUS_EXTERNAL_TOKEN:-}" ]] \
  || die "GATUS_EXTERNAL_TOKEN is unset (heartbeat push credential)"
command -v curl >/dev/null 2>&1 \
  || { echo "$ME: UNMEASURED — curl is absent" >&2; exit 5; }
# python3 is load-bearing in two places: _gatus_push.sh URL-encodes the
# error text through it, and the direct Discord notice builds JSON with
# it. A probe that cannot encode its verdict or build its page must not
# claim a verdict — but python3 lives in /bin on most images, so absence
# is near-impossible; the absent-HELPER case below is the tested one.
command -v python3 >/dev/null 2>&1 \
  || { echo "$ME: UNMEASURED — python3 is absent" >&2; exit 5; }
[[ -f "$HERE/../case_658bf6b9/_gatus_push.sh" ]] \
  || { echo "$ME: UNMEASURED — ../case_658bf6b9/_gatus_push.sh is absent" >&2; exit 5; }
# shellcheck source=../case_658bf6b9/_gatus_push.sh
. "$HERE/../case_658bf6b9/_gatus_push.sh"

mkcfg() {  # mkcfg <var-name> → prints private path; caller fills it
  local dir cfg
  dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/gatus_probe.XXXXXXXX")" \
    || { echo "$ME: could not create a private temp directory" >&2; exit 5; }
  cfg="$(umask 077; mktemp "$dir/curlcfg.XXXXXXXX")" \
    || { echo "$ME: could not create a curl config file" >&2; rm -rf "$dir"; exit 5; }
  chmod 0600 "$cfg"
  printf '%s %s\n' "$dir" "$cfg"
}

# --- 1. probe ---------------------------------------------------------------
read -r PDIR PCFG <<<"$(mkcfg)"
trap 'rm -rf "${PDIR:-}" "${NDIR:-}"' EXIT
{
  printf 'url = "%s"\n' "$GATUS_HEALTH_URL"
  printf 'request = "GET"\n'
  # Cloudflare Access service token when the page sits behind it (see
  # OPERATOR.md §6 of case_658bf6b9). Absent tokens mean a direct-reachable page;
  # a login-page 200 is NOT accepted as healthy (rule 2) — see below.
  [[ -n "${CF_ACCESS_CLIENT_ID:-}" ]] \
    && printf 'header = "CF-Access-Client-Id: %s"\n' "$CF_ACCESS_CLIENT_ID"
  [[ -n "${CF_ACCESS_CLIENT_SECRET:-}" ]] \
    && printf 'header = "CF-Access-Client-Secret: %s"\n' "$CF_ACCESS_CLIENT_SECRET"
  printf 'max-time = "%s"\n' "$HEALTH_TIMEOUT"
  printf 'silent\nshow-error\n'
} > "$PCFG"
chmod 0600 "$PCFG"

http="$(curl --config "$PCFG" -o /dev/null -w '%{http_code}' 2>"$PDIR/curl.err")" \
  || http="curl-error: $(tail -c 200 "$PDIR/curl.err" | tr '\n' ' ')"

# --- 2. verdict + heartbeat push (best-effort: Gatus may be the thing down) --
if [[ "$http" == "200" ]]; then
  if gatus_push "$ENDPOINT_KEY" true; then
    echo "UP $http"
    exit 0
  else
    echo "$ME: probe UP but heartbeat push failed — Gatus push API unreachable" >&2
    echo "UP $http (heartbeat NOT recorded)"
    exit 1
  fi
fi

err="off-host probe: GET $GATUS_HEALTH_URL → $http"
(( ${#err} > MAX_ERR_CHARS )) && err="${err:0:MAX_ERR_CHARS}…(truncated)"
gatus_push "$ENDPOINT_KEY" false "$err" 2>/dev/null || true

# --- 3. direct notice bypassing Gatus (the actual page when Gatus is down) ---
if [[ -n "${DISCORD_HEARTBEAT_WEBHOOK_URL:-}" ]]; then
  read -r NDIR NCFG <<<"$(mkcfg)"
  {
    printf 'url = "%s"\n' "$DISCORD_HEARTBEAT_WEBHOOK_URL"
    printf 'request = "POST"\n'
    printf 'header = "Content-Type: application/json"\n'
    printf 'max-time = "%s"\n' "$HEALTH_TIMEOUT"
    printf 'silent\nshow-error\n'
  } > "$NCFG"
  chmod 0600 "$NCFG"
  NDATA="$NDIR/data.json"
  python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1][:400]}))' \
    "PAGE (staging-safe, no prod incident): $err" > "$NDATA" 2>/dev/null \
    || printf '{"content":"PAGE: Gatus off-host probe failed (detail truncated)"}' > "$NDATA"
  chmod 0600 "$NDATA"
  # NOTE: --config and --data-binary take FILE PATHS only — the webhook URL
  # never appears in this argv. Do not rewrite this with a bare URL argument.
  if grep -q '^2' <<<"$(curl --config "$NCFG" --data-binary "@$NDATA" -o /dev/null -w '%{http_code}' 2>/dev/null)"; then
    echo "$ME: direct Discord notice sent" >&2
  else
    echo "$ME: direct Discord notice FAILED (webhook misconfigured?)" >&2
  fi
else
  echo "$ME: no DISCORD_HEARTBEAT_WEBHOOK_URL — workflow exit code is the only page" >&2
fi

echo "DOWN $http"
exit 1
