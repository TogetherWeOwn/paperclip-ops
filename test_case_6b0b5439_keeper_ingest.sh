#!/usr/bin/env bash
# ===========================================================================
# test_case_6b0b5439_keeper_ingest.sh — offline suite for the case_5605c29e Keeper
# ingest freshness Gatus module (ops/case_5605c29e/, parent case_66436e78 item 5,
# dependencies).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. HEALTHY pushes true — a 60s-old ingest exits 0 with
#      keeper_ingest-freshness success=true. Without this, a suite that
#      only asserts failures could pass against a script that fails
#      everything. ISO-8601 timestamps parse too.
#   2. WARN-LAG fails closed — 2000s lag (over warn 1800, under crit
#      3600) exits 1, endpoint false naming "warning". The positive
#      control is §1: the same probe with a fresh ingest answers healthy.
#   3. CRIT-LAG fails closed — 5000s lag exits 1, endpoint false naming
#      "critical".
#   4. BOUNDARY-EXACT — lag exactly at warn (1800s) pushes false: warn
#      wins ties (>=), so a boundary ingest cannot read green.
#   5. MISSING fails closed — 200 with no last_successful_ingest exits 1,
#      endpoint false naming "missing".
#   6. MALFORMED fails closed — 200 with a non-timestamp value exits 1,
#      endpoint false naming "malformed".
#   7. NON-200 fails closed — HTTP 500 exits 1, endpoint false naming
#      the status.
#   8. TIMEOUT fails closed — a curl exit (timeout) exits 1, false.
#   9. FUTURE fails closed — a timestamp 3600s ahead exits 1, endpoint
#      false naming "clock-anomaly".
#  10. STAGING-ONLY — a prod-like host pushes DOWN (exit 1) WITHOUT
#      dialing (the probe config never mentions the host); a missing or
#      unparseable URL pushes DOWN (exit 1).
#  11. REFUSALS — missing Gatus token, inverted thresholds, and an unknown
#      argument each exit 2 with zero pushes.
#  12. NON-DISCLOSURE — the freshness bearer travels in curl configs,
#      never argv; the URL and payload rows never reach the push configs,
#      stdout, or stderr. Push messages carry lag seconds + bound only.
#  13. DEFINITIONS — one endpoint with threshold + owner + runbook anchor;
#      the README registry names the key; no secret-shaped strings
#      committed (with a planted-secret positive control).
#  14. NOT VACUOUS — a mutant with the staleness gate neutered scores the
#      crit fixture healthy, proving §§2-4 assert something.
#
# Hermetic: no network, no credentials. A stub curl answers the freshness
# probe from fixture files and records every push; PROBE_* env controls
# its HTTP code, body, and exit code.
#
# TIME-RELATIVE FIXTURES: lag is now-relative, so static JSON cannot pin
# it. Fixtures carry @NOW<offset> markers (epoch seconds) and
# @NOW-ISO-<offset> markers (UTC ISO-8601); resolve_body expands them at
# test time. The WARN/CRIT defaults (1800/3600) are the suite's frame —
# a fixture and a bound changed together would still agree, which is why
# §4 pins the exact-boundary tie-break and §14 pins the gate itself.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOD="$HERE/ops/case_5605c29e"
TOOL="$MOD/push-keeper-ingest.sh"
YAML="$MOD/gatus-endpoints.yaml"
OPDOC="$MOD/OPERATOR.md"
MODREADME="$MOD/README.md"
FIX="$MOD/fixtures"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$TOOL" ]] || { echo "REFUSE: $TOOL missing or not executable" >&2; exit 3; }
[[ -f "$YAML" ]] || { echo "REFUSE: $YAML missing" >&2; exit 3; }
[[ -f "$OPDOC" ]] || { echo "REFUSE: $OPDOC missing" >&2; exit 3; }
[[ -f "$MODREADME" ]] || { echo "REFUSE: $MODREADME missing" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required" >&2; exit 3; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_keeper_ingest.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export ARGV_LOG="$WORK/argv.log" PUSH_LOG="$WORK/push.log" PROBE_LOG="$WORK/probe.log"
: > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"

# Stub curl: records argv always; push configs (Gatus external URLs) go to
# PUSH_LOG and answer 200; anything else is the freshness probe — its
# config goes to PROBE_LOG, the fixture body lands in its output file,
# and the printed code mimics curl's write-out.
cat > "$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ARGV_LOG"
cfg=""
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cfg="${!j}"; fi
done
[[ -n "$cfg" ]] || { echo "stub curl: no --config" >&2; exit 99; }
if grep -q "/api/v1/endpoints/" "$cfg" 2>/dev/null; then
  cat "$cfg" >> "$PUSH_LOG"; printf '\n---\n' >> "$PUSH_LOG"
  printf '200'
  exit 0
fi
cat "$cfg" >> "$PROBE_LOG"; printf '\n---\n' >> "$PROBE_LOG"
if [[ "${PROBE_CURL_EXIT:-0}" != "0" ]]; then
  echo "curl: operation timed out" >&2
  exit "$PROBE_CURL_EXIT"
fi
out="$(grep '^output = ' "$cfg" | sed 's/^output = "//;s/"$//')"
cat "${PROBE_BODY_FILE:-/dev/null}" > "$out"
printf '%s' "${PROBE_HTTP:-200}"
exit 0
STUB
chmod +x "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"

BASE_ENV=(GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-gatus-token-14035"
  KEEPER_FRESHNESS_URL="http://127.0.0.1:9/staging/freshness"
  KEEPER_FRESHNESS_BEARER="test-keeper-bearer-14035")
MARKER="records_since_restart"

# resolve_body <fixture> — expands @NOW<off> to epoch and @NOW-ISO-<off>
# to UTC ISO-8601, prints the resolved path in $WORK.
resolve_body() {
  python3 - "$1" "$WORK/body.json" <<'PY'
import datetime, re, sys
src, dst = sys.argv[1], sys.argv[2]
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
def sub(m):
    off = int(m.group(1))
    if m.group(0).startswith("@NOW-ISO"):
        dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=off)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(int(now) + off)
text = open(src, encoding="utf-8").read()
text = re.sub(r"@NOW-ISO-([+-]?\d+)", sub, text)
text = re.sub(r"@NOW([+-]?\d+)", sub, text)
open(dst, "w", encoding="utf-8").write(text)
PY
  printf '%s' "$WORK/body.json"
}

run_tool() { # [extra env assignments...] — runs TOOL, echoes exit code
  : > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"
  local rc
  set +e
  env "${BASE_ENV[@]}" PROBE_BODY_FILE="$(resolve_body "$FIX/fresh.json")" "$@" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  set -e
  printf '%s' "$rc"
}
run_fixture() { # <fixture> [extra env...] — runs TOOL against a fixture
  local fx="$1"; shift
  : > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"
  local rc
  set +e
  env "${BASE_ENV[@]}" PROBE_BODY_FILE="$(resolve_body "$FIX/$fx")" "$@" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  set -e
  printf '%s' "$rc"
}
push_hits() { # <key> <true|false> — count of matching pushes
  grep -cF "endpoints/$1/external?success=$2" "$PUSH_LOG" || true
}
push_has() { # <pattern> — greps the URL-decoded push configs
  local decoded # capture first: a producer piped into grep -q can take SIGPIPE under pipefail
  decoded="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open("'"$PUSH_LOG"'").read()))')"
  grep -q "$1" <<<"$decoded"
}

# --- §1 healthy ------------------------------------------------------------
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "1. fresh fixture exits 0" || bad "1. fresh exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness true)" == "1" ]] \
  && ok "1b. freshness pushed true" || bad "1b. freshness push" "$(cat "$PUSH_LOG")"
rc="$(run_fixture fresh-iso.json)"
[[ "$rc" == "0" ]] && ok "1c. ISO-8601 timestamp parses healthy" || bad "1c. ISO exit" "got $rc"

# --- §2 warn lag fails closed -----------------------------------------------
rc="$(run_fixture stale-warn.json)"
[[ "$rc" == "1" ]] && ok "2. warn-lag exits 1" || bad "2. warn exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "2b. freshness pushed false" || bad "2b. warn push" "$(cat "$PUSH_LOG")"
push_has "warning" && ok "2c. reason names warning" || bad "2c. warning named" "$(cat "$PUSH_LOG")"

# --- §3 crit lag fails closed -----------------------------------------------
rc="$(run_fixture stale-crit.json)"
[[ "$rc" == "1" ]] && ok "3. crit-lag exits 1" || bad "3. crit exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "3b. freshness pushed false" || bad "3b. crit push" "$(cat "$PUSH_LOG")"
push_has "critical" && ok "3c. reason names critical" || bad "3c. critical named" "$(cat "$PUSH_LOG")"

# --- §4 exact-boundary tie goes warn -----------------------------------------
rc="$(run_fixture boundary-exact.json)"
[[ "$rc" == "1" ]] && ok "4. exact-warn-boundary exits 1" || bad "4. boundary exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "4b. boundary pushed false (warn wins ties)" || bad "4b. boundary push" "$(cat "$PUSH_LOG")"

# --- §5 missing timestamp fails closed ----------------------------------------
rc="$(run_fixture missing.json)"
[[ "$rc" == "1" ]] && ok "5. missing timestamp exits 1" || bad "5. missing exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "5b. freshness pushed false" || bad "5b. missing push" "$(cat "$PUSH_LOG")"
push_has "missing" && ok "5c. reason names missing" || bad "5c. missing named" "$(cat "$PUSH_LOG")"

# --- §6 malformed fails closed --------------------------------------------------
rc="$(run_fixture malformed.json)"
[[ "$rc" == "1" ]] && ok "6. malformed exits 1" || bad "6. malformed exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "6b. freshness pushed false" || bad "6b. malformed push" "$(cat "$PUSH_LOG")"
push_has "malformed" && ok "6c. reason names malformed" || bad "6c. malformed named" "$(cat "$PUSH_LOG")"

# --- §7 HTTP 500 fails closed -----------------------------------------------------
rc="$(run_fixture fresh.json PROBE_HTTP=500)"
[[ "$rc" == "1" ]] && ok "7. HTTP 500 exits 1" || bad "7. 500 exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "7b. freshness pushed false" || bad "7b. 500 push" "$(cat "$PUSH_LOG")"
push_has "HTTP 500" && ok "7c. reason names the status" || bad "7c. status named" "$(cat "$PUSH_LOG")"

# --- §8 curl timeout fails closed --------------------------------------------------
rc="$(run_fixture fresh.json PROBE_CURL_EXIT=28)"
[[ "$rc" == "1" ]] && ok "8. curl timeout exits 1" || bad "8. timeout exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "8b. freshness pushed false" || bad "8b. timeout push" "$(cat "$PUSH_LOG")"

# --- §9 future timestamp fails closed ------------------------------------------------
rc="$(run_fixture future.json)"
[[ "$rc" == "1" ]] && ok "9. future timestamp exits 1" || bad "9. future exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "9b. freshness pushed false" || bad "9b. future push" "$(cat "$PUSH_LOG")"
push_has "clock-anomaly" && ok "9c. reason names clock-anomaly" || bad "9c. anomaly named" "$(cat "$PUSH_LOG")"

# --- §10 staging-only guard ------------------------------------------------------------
reset_push() { : > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"; }
reset_push
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$(resolve_body "$FIX/fresh.json")" \
  KEEPER_FRESHNESS_URL="http://keeper-prod-01:8080/freshness" \
  "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "1" ]] && ok "10. prod-like host pushes DOWN (exit 1)" || bad "10. prod guard exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "10b. prod-like target pushed false" || bad "10b. prod push" "$(cat "$PUSH_LOG")"
if grep -q "keeper-prod-01" "$PROBE_LOG"; then
  bad "10c. prod host dialed" "$(cat "$PROBE_LOG")"
else ok "10c. prod host never dialed"; fi
reset_push
set +e
env "${BASE_ENV[@]}" KEEPER_FRESHNESS_URL= "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "1" ]] && ok "10d. missing URL pushes DOWN (exit 1)" || bad "10d. missing URL exit" "got $rc"
[[ "$(push_hits keeper_ingest-freshness false)" == "1" ]] \
  && ok "10e. missing URL pushed false" || bad "10e. missing URL push" "$(cat "$PUSH_LOG")"
reset_push
set +e
env "${BASE_ENV[@]}" KEEPER_FRESHNESS_URL="not a url" "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "1" ]] && ok "10f. unparseable URL pushes DOWN (exit 1)" || bad "10f. bad URL exit" "got $rc"

# --- §11 refusals, zero pushes -------------------------------------------------------------
reset_push
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$(resolve_body "$FIX/fresh.json")" GATUS_EXTERNAL_TOKEN= "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "11. missing Gatus token refuses (exit 2)" || bad "11. token refusal" "got $rc"
[[ "$(grep -cF "endpoints/" "$PUSH_LOG" || true)" == "0" ]] \
  && ok "11b. refusal makes zero pushes" || bad "11b. zero pushes" "$(cat "$PUSH_LOG")"
reset_push
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$(resolve_body "$FIX/fresh.json")" \
  KEEPER_WARN_LAG_S=3600 KEEPER_CRIT_LAG_S=1800 "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "11c. inverted thresholds refuse (exit 2)" || bad "11c. threshold refusal" "got $rc"
reset_push
set +e
env "${BASE_ENV[@]}" "$TOOL" --bogus >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "11d. unknown argument refuses (exit 2)" || bad "11d. argv refusal" "got $rc"

# --- §12 non-disclosure --------------------------------------------------------------------------
: > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$(resolve_body "$FIX/fresh.json")" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
if grep -q "test-keeper-bearer-14035" "$ARGV_LOG" || grep -q "test-gatus-token-14035" "$ARGV_LOG"; then
  bad "12. bearer in argv" "$(cat "$ARGV_LOG")"
else ok "12. no bearer in curl argv"; fi
grep -q "Authorization: Bearer" "$PROBE_LOG" \
  && ok "12b. freshness bearer travels in probe config" || bad "12b. probe transport" ""
grep -q "Authorization: Bearer" "$PUSH_LOG" \
  && ok "12c. Gatus token travels in push config" || bad "12c. push transport" ""
if grep -q "test-keeper-bearer-14035" "$PUSH_LOG"; then
  bad "12d. freshness bearer reached pushes" "$(cat "$PUSH_LOG")"
else ok "12d. freshness bearer stays on the probe leg"; fi
if grep -q "http://127.0.0.1:9/staging" "$PUSH_LOG"; then
  bad "12e. probe URL reached pushes" "$(cat "$PUSH_LOG")"
else ok "12e. no probe URL in pushes (lag seconds only)"; fi
if grep -q "$MARKER" "$WORK/out.txt" "$WORK/err.txt" || grep -q "test-keeper-bearer-14035" "$WORK/out.txt" "$WORK/err.txt"; then
  bad "12f. payload text or bearer on stdout/stderr" ""
else ok "12f. stdout/stderr carry no payload or bearer"; fi

# --- §13 definitions + registry + secret sweep ------------------------------------------------------------
[[ "$(grep -c '^  - name: ' "$YAML")" == "1" ]] \
  && ok "13. one gatus endpoint" || bad "13. endpoint count" ""
grep -q "name: ingest-freshness" "$YAML" && ok "13b. endpoint ingest-freshness" || bad "13b. endpoint" "missing"
grep -q "OPERATOR.md#ingest-freshness" "$YAML" && ok "13c. runbook pointer" || bad "13c. runbook" "missing"
grep -q "^### ingest-freshness$" "$OPDOC" && ok "13d. runbook anchor" || bad "13d. anchor" "missing"
grep -q "keeper_ingest-freshness" "$MODREADME" && ok "13e. registry names keeper_ingest-freshness" || bad "13e. registry" "missing"
grep -q "GATUS_EXTERNAL_TOKEN" "$YAML" && ok "13f. token only via env reference" || bad "13f. token reference" "missing"
if grep -qE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|KEEPER_FRESHNESS_URL|KEEPER_FRESHNESS_BEARER|GATUS_EXTERNAL_TOKEN)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$MOD" -r; then
  bad "13g. secret-shaped string committed" ""
else ok "13g. no secret-shaped strings in module"; fi
echo 'x="https://discord.com/api/webhooks/123/abc"' > "$WORK/planted.txt"
if grep -qE 'discord\.com/api/webhooks/[0-9]' "$WORK/planted.txt"; then
  ok "13h. secret-scan pattern fires on a planted webhook"
else bad "13h. secret-scan pattern" "pattern missed a planted webhook URL"; fi

# --- §14 not vacuous -----------------------------------------------------------------------------------
cp "$TOOL" "$WORK/mutant.sh"
sed -i 's/lag_s >= crit_s/lag_s >= 999999999/' "$WORK/mutant.sh"
sed -i 's/lag_s >= warn_s/lag_s >= 999999998/' "$WORK/mutant.sh"
# The copy lives outside the module dir: pin the helper absolutely so the
# mutant tests the gate, not the helper-path check (which §11 already owns).
sed -i "s|^GATUS_HELPER=.*|GATUS_HELPER=\"$MOD/../case_658bf6b9/_gatus_push.sh\"|" "$WORK/mutant.sh"
: > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$(resolve_body "$FIX/stale-crit.json")" bash "$WORK/mutant.sh" >/dev/null 2>&1; mrc=$?
set -e
if [[ "$mrc" == "0" ]] && [[ "$(push_hits keeper_ingest-freshness true)" == "1" ]]; then
  ok "14. neutered gate scores crit-lag healthy (suite can go red)"
else bad "14. mutant control" "rc=$mrc pushes=$(cat "$PUSH_LOG")"; fi

echo "--- $PASS pass, $FAIL fail ---"
[[ "$FAIL" == "0" ]]
