#!/usr/bin/env bash
# Regression suite for BASE host validation on PAPERCLIP_API_KEY senders.
#
# Every sender below derives its base from PAPERCLIP_API_URL and attaches the
# run/board bearer to it. A base that is not an http(s) URL with a non-empty
# bare host -- wrong scheme, userinfo, whitespace, query/fragment, curl-glob
# hosts, empty or non-numeric ports -- must be refused BEFORE any request is
# built, so the bearer is never sent to a base we refused to trust.
# A well-formed hostile host still passes by design (that needs an
# operator-owned host allowlist); this gate pins the malformed-env layer.
#
# Fully offline: curl is a recording stub on PATH, detectors/readers are
# stubs, no network, no credentials (canary values only).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       %s\n' "$2"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

for tool in jq python3 node; do
  command -v "$tool" >/dev/null || { echo "ERROR: $tool is required" >&2; exit 1; }
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_pc_base_validation.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export CURL_LOG="$WORK/curl.log" CFG_LOG="$WORK/cfg.log"
: > "$CURL_LOG"; : > "$CFG_LOG"

CANARY="pc_CANARY_base_validation_key"
# The allowlist test hatch: well-formed GOOD bases (.invalid, loopback)
# opt in explicitly so the allowlist layer lets the structural layer's
# still-delivers assertions through without a host file.
HATCH="PAPERCLIP_API_ALLOWLIST_TEST=1"
NL_BASE="$(printf 'https://paperclip.invalid/api\nheader = "X: injected"')"

# Recording curl stub. Serves every sender below by URL shape:
#   --config flow (notify, monitors, authenticated health): parses url/output/
#     request/data-binary out of the config; GET issue -> canned issue JSON,
#     PATCH issue -> echoes the requested nextCheckAt back (millis form, as
#     the server would), POST comment -> 201, /api/health -> canned health.
#   direct flow (red_main_poll board + gh reads): board -> `[]`+200 on
#     stdout, /commits/main -> canned sha into -o file + 200.
cat > "$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
# One marker per invocation: argv itself can contain a literal newline
# (`-w $'\n%{http_code}'`), so raw line counts would overcount calls.
printf -- '---CALL---\n%s\n' "$*" >> "$CURL_LOG"
cfg=""; out=""; url=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    --config) cfg="${args[$((i+1))]}" ;;
    -o) out="${args[$((i+1))]}" ;;
  esac
done
if [[ -n "$cfg" ]]; then
  cat "$cfg" >> "$CFG_LOG"
  url="$(sed -n 's/^url = "\(.*\)"$/\1/p' "$cfg")"
  if [[ -z "$url" ]]; then
    # Header-only config (detector health, red board): the URL rode argv.
    for ((i=${#args[@]}-1; i>=0; i--)); do
      [[ "${args[$i]}" == -* ]] || { url="${args[$i]}"; break; }
    done
  fi
  method="$(sed -n 's/^request = "\(.*\)"$/\1/p' "$cfg")"
  cfg_out="$(sed -n 's/^output = "\(.*\)"$/\1/p' "$cfg")"
  body_file="$(sed -n 's/^data-binary = "@\(.*\)"$/\1/p' "$cfg")"
  [[ -n "$cfg_out" ]] && out="$cfg_out"
  case "$url" in
    */api/health)
      printf '{"databaseBackup":{"status":"ok","latestBackup":{"name":"paperclip-stub.sql.gz","ageHours":"0.5"}}}'
      exit 0 ;;
    */api/companies/*)
      printf '[]\n200'; exit 0 ;;
    */comments)
      printf '201'; exit 0 ;;
    */api/issues/*)
      if [[ "$method" == "GET" ]]; then
        printf '{"id":"issue-stub"}' > "$out"
        printf '200'; exit 0
      fi
      next="$(jq -r '.executionPolicy.monitor.nextCheckAt // empty' "$body_file" 2>/dev/null)"
      stored="${next%Z}.000Z"
      printf '{"id":"issue-stub","monitorNextCheckAt":"%s"}' "$stored" > "$out"
      printf '200'; exit 0 ;;
  esac
  echo "stub curl: unserved config url $url" >&2; exit 90
fi
for ((i=${#args[@]}-1; i>=0; i--)); do
  [[ "${args[$i]}" == -* ]] || { url="${args[$i]}"; break; }
done
case "$url" in
  */api/companies/*) printf '[]\n200'; exit 0 ;;
  */commits/main) printf '{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' > "$out"; printf '200'; exit 0 ;;
esac
echo "stub curl: unserved url $url" >&2; exit 90
STUB
chmod +x "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"
# Every tool runs under `env -i`: a BASH_ENV hook in some harnesses resets
# PATH in each child shell, which would unshadow the curl stub. The stub dir
# stays first on the explicit PATH, so all curl calls stay hermetic.
mkdir -p "$WORK/tmp"
RUN_ENV=(env -i "PATH=$WORK/bin:/usr/bin:/bin" "CURL_LOG=$CURL_LOG" "CFG_LOG=$CFG_LOG" "TMPDIR=$WORK/tmp")

curl_calls() { grep -c '^---CALL---$' "$CURL_LOG" 2>/dev/null || true; }
reset_logs() { : > "$CURL_LOG"; : > "$CFG_LOG"; }

# Kept identical (modulo $NL_BASE) to MALFORMED in
# test_paperclip_api_base_validation_py.py: both gates refuse the same set.
MALFORMED=(
  "ftp://board.invalid/api"
  "file:///etc/passwd"
  "notaurl"
  "https://board@evil.invalid/api"
  "https://board.invalid/api with space"
  "http:///no-host"
  "$NL_BASE"
  "https://:8080/api"
  "http://?q"
  "http://#frag"
  "https://host.invalid/api?x=1"
  "https://host.invalid/api#frag"
  "http://{127.0.0.1,127.0.0.2}/api"
  "http://127.0.0.[1-2]/api"
  "https://host:badport/api"
  "http://ho,st.invalid/api"
  "https://host:/x"
  "http://host:80:90/"
  $'https://ho\x01st.invalid/api'
  "http://ho%09st.invalid/api"
)

# --- notify_paperclip_issue.sh: malformed -> exit 65, curl never called ------
hdr "notify: malformed bases refuse (exit 65) before curl"
PAYLOAD='{"requestId":"REQ-1","decision":"approved","recipientRole":"operator","notifyIssue":"issue-1","body":"hi"}'
for base in "${MALFORMED[@]}"; do
  reset_logs
  out="$("${RUN_ENV[@]}" PAPERCLIP_API_URL="$base" PAPERCLIP_API_KEY="$CANARY" \
    "$HERE/notify_paperclip_issue.sh" <<<"$PAYLOAD" 2>"$WORK/err.txt")"; rc=$?
  if [[ "$rc" -eq 65 && "$(curl_calls)" -eq 0 ]]; then
    ok "refuses $(printf '%s' "$base" | head -c 48)"
  else
    bad "refuses $(printf '%s' "$base" | head -c 48)" "exit=$rc curl_calls=$(curl_calls) err=$(cat "$WORK/err.txt")"
  fi
done

hdr "notify: well-formed base still delivers"
reset_logs
out="$("${RUN_ENV[@]}" "$HATCH" PAPERCLIP_API_URL="https://paperclip.invalid/api" PAPERCLIP_API_KEY="$CANARY" \
  "$HERE/notify_paperclip_issue.sh" <<<"$PAYLOAD" 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 0 ]] && ok "exit 0" || bad "exit 0" "exit=$rc err=$(cat "$WORK/err.txt")"
[[ "$(curl_calls)" -eq 1 ]] && ok "one curl call" || bad "one curl call" "calls=$(curl_calls)"
grep -qF "$CANARY" "$CFG_LOG" && ok "bearer reached curl by config" || bad "bearer reached curl by config"

hdr "notify: bracketed IPv6 base still delivers"
reset_logs
out="$("${RUN_ENV[@]}" "$HATCH" PAPERCLIP_API_URL="http://[::1]:9/api" PAPERCLIP_API_KEY="$CANARY" \
  "$HERE/notify_paperclip_issue.sh" <<<"$PAYLOAD" 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 0 ]] && ok "exit 0" || bad "exit 0" "exit=$rc err=$(cat "$WORK/err.txt")"
[[ "$(curl_calls)" -eq 1 ]] && ok "one curl call" || bad "one curl call" "calls=$(curl_calls)"

# --- db_backup_stall_monitor.sh: malformed -> exit 2, detector untouched ------
hdr "stall monitor: malformed bases refuse (exit 2) before detector/curl"
cat > "$WORK/bin/fakedetector" <<'DET'
#!/usr/bin/env bash
echo "detector ran" >> "$DETECTOR_CALL_LOG"
printf '{"verdict":"ok","reasons":[],"checkedAt":"2026-01-01T00:00:00Z"}\n'
exit 0
DET
chmod +x "$WORK/bin/fakedetector"
export DETECTOR_CALL_LOG="$WORK/detector.log"
for base in "${MALFORMED[@]}"; do
  reset_logs; : > "$DETECTOR_CALL_LOG"
  out="$("${RUN_ENV[@]}" PAPERCLIP_API_URL="$base" PAPERCLIP_API_KEY="$CANARY" PAPERCLIP_TASK_ID="issue-stub" \
    DB_BACKUP_STALL_SH="$WORK/bin/fakedetector" "DETECTOR_CALL_LOG=$DETECTOR_CALL_LOG" \
    "$HERE/db_backup_stall_monitor.sh" 2>"$WORK/err.txt")"; rc=$?
  if [[ "$rc" -eq 2 && "$(curl_calls)" -eq 0 && ! -s "$DETECTOR_CALL_LOG" ]]; then
    ok "refuses $(printf '%s' "$base" | head -c 48)"
  else
    bad "refuses $(printf '%s' "$base" | head -c 48)" "exit=$rc curl_calls=$(curl_calls) detector_ran=$([ -s "$DETECTOR_CALL_LOG" ] && echo yes || echo no)"
  fi
done

hdr "stall monitor: well-formed base still cycles"
reset_logs; : > "$DETECTOR_CALL_LOG"
out="$("${RUN_ENV[@]}" "$HATCH" PAPERCLIP_API_URL="https://paperclip.invalid/api" PAPERCLIP_API_KEY="$CANARY" PAPERCLIP_TASK_ID="issue-stub" \
  DB_BACKUP_STALL_SH="$WORK/bin/fakedetector" "DETECTOR_CALL_LOG=$DETECTOR_CALL_LOG" \
  "$HERE/db_backup_stall_monitor.sh" 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 0 ]] && ok "exit 0" || bad "exit 0" "exit=$rc err=$(cat "$WORK/err.txt")"
[[ "$(curl_calls)" -eq 2 ]] && ok "GET+PATCH curl calls" || bad "GET+PATCH curl calls" "calls=$(curl_calls)"

# --- dispatch_deploy_monitor.sh: malformed -> exit 2 --------------------------
hdr "deploy monitor: malformed bases refuse (exit 2)"
cat > "$WORK/bin/fakedeploy" <<'DEP'
#!/usr/bin/env bash
echo "deploy ran" >> "$DEPLOY_CALL_LOG"
echo "no drift (stub)"
exit 0
DEP
chmod +x "$WORK/bin/fakedeploy"
export DEPLOY_CALL_LOG="$WORK/deploy.log"
for base in "${MALFORMED[@]}"; do
  reset_logs; : > "$DEPLOY_CALL_LOG"
  out="$("${RUN_ENV[@]}" PAPERCLIP_API_URL="$base" PAPERCLIP_API_KEY="$CANARY" PAPERCLIP_TASK_ID="issue-stub" \
    DISPATCH_DEPLOY_SH="$WORK/bin/fakedeploy" "DEPLOY_CALL_LOG=$DEPLOY_CALL_LOG" \
    "$HERE/scripts/dispatch_deploy_monitor.sh" 2>"$WORK/err.txt")"; rc=$?
  if [[ "$rc" -eq 2 && "$(curl_calls)" -eq 0 && ! -s "$DEPLOY_CALL_LOG" ]]; then
    ok "refuses $(printf '%s' "$base" | head -c 48)"
  else
    bad "refuses $(printf '%s' "$base" | head -c 48)" "exit=$rc curl_calls=$(curl_calls)"
  fi
done

hdr "deploy monitor: well-formed base still cycles"
reset_logs; : > "$DEPLOY_CALL_LOG"
out="$("${RUN_ENV[@]}" "$HATCH" PAPERCLIP_API_URL="https://paperclip.invalid/api" PAPERCLIP_API_KEY="$CANARY" PAPERCLIP_TASK_ID="issue-stub" \
  DISPATCH_DEPLOY_SH="$WORK/bin/fakedeploy" "DEPLOY_CALL_LOG=$DEPLOY_CALL_LOG" \
  "$HERE/scripts/dispatch_deploy_monitor.sh" 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 0 ]] && ok "exit 0" || bad "exit 0" "exit=$rc err=$(cat "$WORK/err.txt")"
[[ "$(curl_calls)" -eq 2 ]] && ok "GET+PATCH curl calls" || bad "GET+PATCH curl calls" "calls=$(curl_calls)"

# --- db_backup_stall.sh: malformed -> skip health, verdict unaffected ---------
hdr "stall detector: malformed bases skip health (WARNING, no curl)"
mkdir -p "$WORK/good"; : > "$WORK/healthy.log"
for base in "${MALFORMED[@]}"; do
  reset_logs
  out="$("${RUN_ENV[@]}" PAPERCLIP_API_URL="$base" PAPERCLIP_API_KEY="$CANARY" \
    "$HERE/db_backup_stall.sh" --log "$WORK/healthy.log" --backup-dir "$WORK/good" --json 2>"$WORK/err.txt")"; rc=$?
  health="$(printf '%s' "$out" | jq -r '.healthStatus // "MISSING"' 2>/dev/null)"
  if [[ "$(curl_calls)" -eq 0 && "$health" == "not_checked" ]] && grep -q "WARNING" "$WORK/err.txt"; then
    ok "skips $(printf '%s' "$base" | head -c 48)"
  else
    bad "skips $(printf '%s' "$base" | head -c 48)" "curl_calls=$(curl_calls) health=$health err=$(cat "$WORK/err.txt")"
  fi
done

hdr "stall detector: well-formed base still reads health"
reset_logs
out="$("${RUN_ENV[@]}" "$HATCH" PAPERCLIP_API_URL="https://health-stub.invalid" PAPERCLIP_API_KEY="$CANARY" \
  "$HERE/db_backup_stall.sh" --log "$WORK/healthy.log" --backup-dir "$WORK/good" --json 2>"$WORK/err.txt")"; rc=$?
health="$(printf '%s' "$out" | jq -r '.healthStatus // "MISSING"' 2>/dev/null)"
[[ "$(curl_calls)" -ge 1 ]] && ok "health curl ran" || bad "health curl ran" "calls=$(curl_calls)"
[[ "$health" == "ok" ]] && ok "healthStatus ok" || bad "healthStatus ok" "got $health out=$out"

# --- red_main_poll.sh: malformed -> exit 3 (board unreadable, not clean) ------
hdr "red_main_poll: malformed bases read as unreadable board (exit 3)"
cat > "$WORK/bin/fakereader" <<'READER'
#!/usr/bin/env bash
printf '{"verdict":"pass","reason":"stub"}\n'
exit 0
READER
chmod +x "$WORK/bin/fakereader"
for base in "${MALFORMED[@]}"; do
  reset_logs
  out="$("${RUN_ENV[@]}" PAPERCLIP_API_URL="$base" PAPERCLIP_COMPANY_ID="co-stub" \
    RED_MAIN_API_KEY="$CANARY" GH_TOKEN="$CANARY" GH_CI_STATUS_BIN="$WORK/bin/fakereader" \
    "$HERE/red_main_poll.sh" snapshot o/r 2>"$WORK/err.txt")"; rc=$?
  if [[ "$rc" -eq 3 && "$(curl_calls)" -eq 0 ]]; then
    ok "exit 3 $(printf '%s' "$base" | head -c 48)"
  else
    bad "exit 3 $(printf '%s' "$base" | head -c 48)" "exit=$rc curl_calls=$(curl_calls) err=$(cat "$WORK/err.txt")"
  fi
done

hdr "red_main_poll: well-formed base still snapshots"
reset_logs
out="$("${RUN_ENV[@]}" "$HATCH" PAPERCLIP_API_URL="https://paperclip.invalid/api" PAPERCLIP_COMPANY_ID="co-stub" \
  RED_MAIN_API_KEY="$CANARY" GH_TOKEN="$CANARY" GH_CI_STATUS_BIN="$WORK/bin/fakereader" \
  "$HERE/red_main_poll.sh" snapshot o/r 2>"$WORK/err.txt")"; rc=$?
[[ "$rc" -eq 0 ]] && ok "exit 0" || bad "exit 0" "exit=$rc err=$(cat "$WORK/err.txt") out=$out"
grep -q "api/companies" "$CURL_LOG" && ok "board URL requested" || bad "board URL requested" "$(cat "$CURL_LOG")"

# --- post_finding.sh: malformed -> return 1, curl never called ----------------
hdr "post_finding: malformed bases refuse (return 1) before curl"
for base in "${MALFORMED[@]}"; do
  reset_logs
  out="$("${RUN_ENV[@]}" PAPERCLIP_API_URL="$base" PAPERCLIP_AGENT_API_KEY="$CANARY" PAPERCLIP_COMPANY_ID="co-stub" \
    PAPERCLIP_ASSIGNEE_AGENT_ID="agent-stub" \
    bash -c 'source "$0"; _pf_api GET "/api/test"' "$HERE/ops/case_4ea7d92e/lib/post_finding.sh" 2>"$WORK/err.txt")"; rc=$?
  if [[ "$rc" -eq 1 && "$(curl_calls)" -eq 0 ]]; then
    ok "refuses $(printf '%s' "$base" | head -c 48)"
  else
    bad "refuses $(printf '%s' "$base" | head -c 48)" "exit=$rc curl_calls=$(curl_calls) err=$(cat "$WORK/err.txt")"
  fi
done

hdr "post_finding: well-formed base still posts"
reset_logs
cat > "$WORK/bin/curl-board" <<'BOARDSTUB'
#!/usr/bin/env bash
printf -- '---CALL---\n%s\n' "$*" >> "$CURL_LOG"
printf '{"ok":true}\n200'
BOARDSTUB
chmod +x "$WORK/bin/curl-board"
# _pf_api uses direct curl args with -w; route through a board-shaped stub by
# shadowing curl only for this call.
mv "$WORK/bin/curl" "$WORK/bin/curl-main"; cp "$WORK/bin/curl-board" "$WORK/bin/curl"
out="$("${RUN_ENV[@]}" "$HATCH" PAPERCLIP_API_URL="https://paperclip.invalid/api" PAPERCLIP_AGENT_API_KEY="$CANARY" PAPERCLIP_COMPANY_ID="co-stub" \
  PAPERCLIP_ASSIGNEE_AGENT_ID="agent-stub" \
  bash -c 'source "$0"; _pf_api GET "/api/test"' "$HERE/ops/case_4ea7d92e/lib/post_finding.sh" 2>"$WORK/err.txt")"; rc=$?
mv "$WORK/bin/curl-main" "$WORK/bin/curl"
[[ "$rc" -eq 0 ]] && ok "exit 0" || bad "exit 0" "exit=$rc err=$(cat "$WORK/err.txt")"
[[ "$(curl_calls)" -eq 1 ]] && ok "one curl call" || bad "one curl call" "calls=$(curl_calls)"

# --- sibling_guard.sh: malformed -> blind (exit 3), board never curled ------
hdr "sibling_guard: malformed bases go blind (exit 3) before board curl"
mkdir -p "$WORK/empty-guard"
[[ -e "$WORK/bin/node" ]] || ln -s "$(command -v node)" "$WORK/bin/node"
GUARD_UUID="11111111-2222-3333-4444-555555555555"
# Board reads are the only curls that carry the board bearer; GH-detector
# curls (stubbed, GH_API only) may still be attempted and go blind.
board_calls() { grep -c "api/issues\|api/companies" "$CURL_LOG" 2>/dev/null || true; }
for base in "${MALFORMED[@]}"; do
  reset_logs
  out="$(cd "$WORK/empty-guard" && "${RUN_ENV[@]}" PAPERCLIP_API_URL="$base" PAPERCLIP_API_KEY="$CANARY" \
    PAPERCLIP_COMPANY_ID="co-stub" PAPERCLIP_AGENT_ID="agent-stub" PAPERCLIP_RUN_ID="run-stub" \
    "$HERE/sibling_guard.sh" --json "$GUARD_UUID" 2>"$WORK/err.txt")"; rc=$?
  verdict="$(printf '%s' "$out" | jq -r '.verdict // "MISSING"' 2>/dev/null)"
  if [[ "$rc" -eq 3 && "$(board_calls)" -eq 0 && "$verdict" == "indeterminate" ]]; then
    ok "blind $(printf '%s' "$base" | head -c 48)"
  else
    bad "blind $(printf '%s' "$base" | head -c 48)" "exit=$rc board_calls=$(board_calls) verdict=$verdict err=$(cat "$WORK/err.txt")"
  fi
done

printf '\nbase validation: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
