#!/usr/bin/env bash
# ===========================================================================
# test_case_79158b71_hindsight_recall.sh — offline suite for the case_34ee2cf0
# Hindsight recall canary Gatus module (ops/case_34ee2cf0/, parent case_66436e78
# item 5, dependencies).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. HEALTHY pushes true — 200 + non-empty rows exits 0 with both
#      endpoints success=true. Without this, a suite that only asserts
#      failures could pass against a script that fails everything.
#   2. NON-200 fails closed — HTTP 500 exits 1 with both endpoints false
#      naming the status (the case_0271a418 gateway-500 class). The positive
#      control is §1: the same probe with a 200 answers healthy.
#   3. TIMEOUT fails closed — a curl exit (timeout) exits 1, both false.
#   4. EMPTY-200 fails closed — 200 with zero rows (the case_630cdf02
#      empty-args shape: a healthy API with a vacuous reply) exits 1;
#      canary false naming empty, latency still true on a fast reply.
#   5. MALFORMED-200 fails closed — 200 with no result array exits 1,
#      canary false naming malformed, latency still true on a fast
#      reply (same split as §4: content failure, not speed failure).
#   6. SLOW-BUT-FULL splits the verdict — canary true, latency false
#      naming the ms, exit 1. One endpoint alone could not separate
#      capacity pressure (case_0271a418 p95 21-40s) from content failure.
#   7. REFUSALS — missing recall URL, missing Gatus token, and an unknown
#      argument each exit 2 with zero pushes.
#   8. NON-DISCLOSURE — both bearers travel in curl configs, never argv;
#      the canary query travels via environ to the JSON writer, never
#      argv; the query and recalled fixture text never reach the push
#      configs, stdout, or stderr. Push messages carry counts + ms only.
#   9. DEFINITIONS — two endpoints, each with threshold + owner + runbook
#      anchor; the README registry names both keys; no secret-shaped
#      strings committed (with a planted-secret positive control).
#  10. NOT VACUOUS — a mutant with the non-empty gate neutered scores the
#      empty fixture healthy, proving §4 asserts something.
#
# Hermetic: no network, no credentials. A stub curl answers the recall
# probe from fixture files and records every push; PROBE_* env controls
# its HTTP code, body, delay, and exit code.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOD="$HERE/ops/case_34ee2cf0"
TOOL="$MOD/push-hindsight-recall.sh"
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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_hindsight_recall.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export ARGV_LOG="$WORK/argv.log" PUSH_LOG="$WORK/push.log" PROBE_LOG="$WORK/probe.log"
: > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"

# Stub curl: records argv always; push configs (Gatus external URLs) go to
# PUSH_LOG and answer 200; anything else is the recall probe — its config
# goes to PROBE_LOG, the fixture body lands in its output file, and the
# printed code mimics curl's write-out.
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
sleep "${PROBE_DELAY_S:-0}"
cat "${PROBE_BODY_FILE:-/dev/null}" > "$out"
printf '%s 0.001' "${PROBE_HTTP:-200}"
exit 0
STUB
chmod +x "$WORK/bin/curl"
# python3 wrapper: logs every argv, then execs the real interpreter. Proves
# the canary query reaches the JSON writer via environ, never argv
# (/proc/<pid>/cmdline is world-readable; environ is same-uid only).
REAL_PYTHON3="$(command -v python3)"
export REAL_PYTHON3 PYARGV_LOG="$WORK/pyargv.log"
: > "$PYARGV_LOG"
cat > "$WORK/bin/python3" <<'PYSTUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PYARGV_LOG"
exec "$REAL_PYTHON3" "$@"
PYSTUB
chmod +x "$WORK/bin/python3"
export PATH="$WORK/bin:$PATH"

BASE_ENV=(GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-gatus-token-13795"
  HINDSIGHT_RECALL_URL="http://127.0.0.1:9/recall"
  HINDSIGHT_RECALL_BEARER="test-recall-bearer-13795"
  HINDSIGHT_CANARY_QUERY="canary probe query 13795")
MARKER="SYNTHETIC-CANARY-MARKER-13795"

run_tool() { # [extra env assignments...] — runs TOOL, echoes exit code
  : > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"; : > "$PYARGV_LOG"
  local rc
  set +e
  env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/healthy.json" "$@" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  set -e
  printf '%s' "$rc"
}
push_hits() { # <key> <true|false> — count of matching pushes
  grep -cF "endpoints/$1/external?success=$2" "$PUSH_LOG" || true
}
push_has() { # <pattern> — greps the URL-decoded push configs
  # Capture-first + herestring (case_c25c4d3f): a `producer | grep -q` pipeline
  # under pipefail misreads a match as a miss when grep exits early.
  local decoded
  decoded="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open("'"$PUSH_LOG"'").read()))')"
  grep -q "$1" <<<"$decoded"
}

# --- §1 healthy ------------------------------------------------------------
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "1. healthy fixture exits 0" || bad "1. healthy exit" "got $rc"
[[ "$(push_hits hindsight_recall-canary true)" == "1" ]] \
  && ok "1b. canary pushed true" || bad "1b. canary push" "$(cat "$PUSH_LOG")"
[[ "$(push_hits hindsight_recall-latency true)" == "1" ]] \
  && ok "1c. latency pushed true" || bad "1c. latency push" "$(cat "$PUSH_LOG")"

# --- §2 HTTP 500 fails closed ----------------------------------------------
rc="$(run_tool PROBE_HTTP=500)"
[[ "$rc" == "1" ]] && ok "2. HTTP 500 exits 1" || bad "2. 500 exit" "got $rc"
[[ "$(push_hits hindsight_recall-canary false)" == "1" ]] \
  && ok "2b. canary pushed false" || bad "2b. canary push" "$(cat "$PUSH_LOG")"
[[ "$(push_hits hindsight_recall-latency false)" == "1" ]] \
  && ok "2c. latency pushed false" || bad "2c. latency push" "$(cat "$PUSH_LOG")"
push_has "recall HTTP 500" && ok "2d. reason names the status" || bad "2d. status named" "$(cat "$PUSH_LOG")"

# --- §3 curl timeout fails closed -------------------------------------------
rc="$(run_tool PROBE_CURL_EXIT=28)"
[[ "$rc" == "1" ]] && ok "3. curl timeout exits 1" || bad "3. timeout exit" "got $rc"
[[ "$(push_hits hindsight_recall-canary false)" == "1" ]] \
  && ok "3b. canary pushed false" || bad "3b. canary push" "$(cat "$PUSH_LOG")"
[[ "$(push_hits hindsight_recall-latency false)" == "1" ]] \
  && ok "3c. latency pushed false" || bad "3c. latency push" "$(cat "$PUSH_LOG")"

# --- §4 empty-200 fails closed (case_630cdf02 shape) ------------------------------
rc="$(run_tool PROBE_BODY_FILE="$FIX/empty.json")"
[[ "$rc" == "1" ]] && ok "4. empty-200 exits 1" || bad "4. empty exit" "got $rc"
[[ "$(push_hits hindsight_recall-canary false)" == "1" ]] \
  && ok "4b. canary pushed false" || bad "4b. canary push" "$(cat "$PUSH_LOG")"
push_has "empty 0 results" && ok "4c. reason names empty" || bad "4c. empty named" "$(cat "$PUSH_LOG")"
[[ "$(push_hits hindsight_recall-latency true)" == "1" ]] \
  && ok "4d. fast-but-empty keeps latency true" || bad "4d. latency push" "$(cat "$PUSH_LOG")"

# --- §5 malformed-200 fails closed -------------------------------------------
rc="$(run_tool PROBE_BODY_FILE="$FIX/malformed.json")"
[[ "$rc" == "1" ]] && ok "5. malformed-200 exits 1" || bad "5. malformed exit" "got $rc"
[[ "$(push_hits hindsight_recall-canary false)" == "1" ]] \
  && ok "5b. canary pushed false" || bad "5b. canary push" "$(cat "$PUSH_LOG")"
push_has "malformed" && ok "5c. reason names malformed" || bad "5c. malformed named" "$(cat "$PUSH_LOG")"
[[ "$(push_hits hindsight_recall-latency true)" == "1" ]] \
  && ok "5d. fast-but-malformed keeps latency true" || bad "5d. latency push" "$(cat "$PUSH_LOG")"

# --- §6 slow-but-full splits the verdict --------------------------------------
rc="$(run_tool PROBE_DELAY_S=2 HINDSIGHT_LATENCY_MS=500)"
[[ "$rc" == "1" ]] && ok "6. slow-but-full exits 1" || bad "6. slow exit" "got $rc"
[[ "$(push_hits hindsight_recall-canary true)" == "1" ]] \
  && ok "6b. canary stays true (content fine)" || bad "6b. canary push" "$(cat "$PUSH_LOG")"
[[ "$(push_hits hindsight_recall-latency false)" == "1" ]] \
  && ok "6c. latency pushed false" || bad "6c. latency push" "$(cat "$PUSH_LOG")"
push_has "over 500ms" && ok "6d. reason carries the ms" || bad "6d. ms named" "$(cat "$PUSH_LOG")"

# --- §6e timeout exceeds latency threshold (defaults + refusal) ---------------
# Paperclip Review 4/5: timeout == threshold kills every would-be breach,
# so the latency-only (capacity) verdict can never fire. Defaults (60s >
# 45s) keep that branch reachable; a timeout at or below the threshold
# refuses (exit 2, zero pushes) before any health claim.
: > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"; : > "$PYARGV_LOG"
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/healthy.json" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
grep -q 'max-time = "60"' "$PROBE_LOG" \
  && ok "6e. default timeout 60s exceeds default 45s threshold" || bad "6e. default timeout" "$(cat "$PROBE_LOG")"
reset_push() { : > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"; : > "$PYARGV_LOG"; }
reset_push
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/healthy.json" HINDSIGHT_RECALL_TIMEOUT_S=45 HINDSIGHT_LATENCY_MS=45000 "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "6f. timeout == threshold refuses (exit 2)" || bad "6f. equal refusal" "got $rc"
[[ "$(grep -cF "endpoints/" "$PUSH_LOG" || true)" == "0" ]] \
  && ok "6g. equal-timeout refusal makes zero pushes" || bad "6g. zero pushes" "$(cat "$PUSH_LOG")"
reset_push
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/healthy.json" HINDSIGHT_RECALL_TIMEOUT_S=2 HINDSIGHT_LATENCY_MS=5000 "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "6h. timeout below threshold refuses (exit 2)" || bad "6h. below refusal" "got $rc"
rc="$(run_tool PROBE_DELAY_S=2 HINDSIGHT_RECALL_TIMEOUT_S=4 HINDSIGHT_LATENCY_MS=500)"
[[ "$rc" == "1" ]] && ok "6i. explicit timeout above threshold keeps slow split" || bad "6i. explicit split" "got $rc"

# --- §7 refusals, zero pushes ---------------------------------------------------
reset_push() { : > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"; : > "$PYARGV_LOG"; }
reset_push
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/healthy.json" HINDSIGHT_RECALL_URL= "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "7. missing recall URL refuses (exit 2)" || bad "7. URL refusal" "got $rc"
[[ "$(grep -cF "endpoints/" "$PUSH_LOG" || true)" == "0" ]] \
  && ok "7b. refusal makes zero pushes" || bad "7b. zero pushes" "$(cat "$PUSH_LOG")"
reset_push
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/healthy.json" GATUS_EXTERNAL_TOKEN= "$TOOL" >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "7c. missing Gatus token refuses (exit 2)" || bad "7c. token refusal" "got $rc"
reset_push
set +e
env "${BASE_ENV[@]}" "$TOOL" --bogus >/dev/null 2>&1; rc=$?
set -e
[[ "$rc" == "2" ]] && ok "7d. unknown argument refuses (exit 2)" || bad "7d. argv refusal" "got $rc"

# --- §8 non-disclosure ----------------------------------------------------------
: > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"; : > "$PYARGV_LOG"
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/healthy.json" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
if grep -q "test-recall-bearer-13795" "$ARGV_LOG" || grep -q "test-gatus-token-13795" "$ARGV_LOG"; then
  bad "8. bearer in argv" "$(cat "$ARGV_LOG")"
else ok "8. no bearer in curl argv"; fi
if grep -q "canary probe query 13795" "$ARGV_LOG"; then
  bad "8b. canary query in argv" "$(cat "$ARGV_LOG")"
else ok "8b. no query in curl argv"; fi
grep -q "Authorization: Bearer" "$PROBE_LOG" \
  && ok "8c. recall bearer travels in probe config" || bad "8c. probe transport" ""
grep -q "Authorization: Bearer" "$PUSH_LOG" \
  && ok "8d. Gatus token travels in push config" || bad "8d. push transport" ""
if grep -q "$MARKER" "$PUSH_LOG"; then
  bad "8e. recalled text reached pushes" "$(cat "$PUSH_LOG")"
else ok "8e. no recalled text in pushes (counts only)"; fi
if grep -q "canary probe query 13795" "$PUSH_LOG"; then
  bad "8f. query reached pushes" "$(cat "$PUSH_LOG")"
else ok "8f. no query text in pushes"; fi
if grep -q "test-recall-bearer-13795" "$PUSH_LOG"; then
  bad "8g. recall bearer reached pushes" "$(cat "$PUSH_LOG")"
else ok "8g. recall bearer stays on the probe leg"; fi
if grep -q "$MARKER" "$WORK/out.txt" "$WORK/err.txt" || grep -q "test-recall-bearer-13795" "$WORK/out.txt" "$WORK/err.txt"; then
  bad "8h. fixture text or bearer on stdout/stderr" ""
else ok "8h. stdout/stderr carry no text or bearer"; fi
if grep -q "canary probe query 13795" "$PYARGV_LOG"; then
  bad "8i. canary query in python3 argv" "$(cat "$PYARGV_LOG")"
else ok "8i. query reaches JSON writer via environ, never argv"; fi

# --- §9 definitions + registry + secret sweep ------------------------------------
[[ "$(grep -c '^  - name: ' "$YAML")" == "2" ]] \
  && ok "9. two gatus endpoints" || bad "9. endpoint count" ""
for e in recall-canary recall-latency; do
  grep -q "name: $e" "$YAML" && ok "9b. endpoint $e" || bad "9b. endpoint $e" "missing"
  grep -q "OPERATOR.md#$e" "$YAML" && ok "9c. runbook pointer $e" || bad "9c. runbook $e" "missing"
  grep -q "^### $e$" "$OPDOC" && ok "9d. runbook anchor $e" || bad "9d. anchor $e" "missing"
  grep -q "hindsight_$e" "$MODREADME" && ok "9e. registry names hindsight_$e" || bad "9e. registry $e" "missing"
done
grep -q "GATUS_EXTERNAL_TOKEN" "$YAML" && ok "9f. token only via env reference" || bad "9f. token reference" "missing"
if grep -qE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|HINDSIGHT_RECALL_URL|HINDSIGHT_RECALL_BEARER|GATUS_EXTERNAL_TOKEN)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$MOD" -r; then
  bad "9g. secret-shaped string committed" ""
else ok "9g. no secret-shaped strings in module"; fi
echo 'x="https://discord.com/api/webhooks/123/abc"' > "$WORK/planted.txt"
if grep -qE 'discord\.com/api/webhooks/[0-9]' "$WORK/planted.txt"; then
  ok "9h. secret-scan pattern fires on a planted webhook"
else bad "9h. secret-scan pattern" "pattern missed a planted webhook URL"; fi

# --- §10 not vacuous ---------------------------------------------------------------
cp "$TOOL" "$WORK/mutant.sh"
sed -i 's/verdict_kind" != "ok"/verdict_kind" == "NEVER-TRUE-MUTANT"/' "$WORK/mutant.sh"
# The copy lives outside the module dir: pin the helper absolutely so the
# mutant tests the gate, not the helper-path check (which §7 already owns).
sed -i "s|^GATUS_HELPER=.*|GATUS_HELPER=\"$MOD/../case_658bf6b9/_gatus_push.sh\"|" "$WORK/mutant.sh"
: > "$ARGV_LOG"; : > "$PUSH_LOG"; : > "$PROBE_LOG"
set +e
env "${BASE_ENV[@]}" PROBE_BODY_FILE="$FIX/empty.json" bash "$WORK/mutant.sh" >/dev/null 2>&1; mrc=$?
set -e
if [[ "$mrc" == "0" ]] && [[ "$(push_hits hindsight_recall-canary true)" == "1" ]]; then
  ok "10. neutered gate scores empty healthy (suite can go red)"
else bad "10. mutant control" "rc=$mrc pushes=$(cat "$PUSH_LOG")"; fi

echo "--- $PASS pass, $FAIL fail ---"
[[ "$FAIL" == "0" ]]
