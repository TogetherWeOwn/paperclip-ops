#!/usr/bin/env bash
# ===========================================================================
# Offline contract for acceptance_transport.sh's reply handling (case_6d842160).
# ===========================================================================
# acceptance_transport.sh drives the real MCP transport over HTTP and
# classifies each reply in call(): the HTTP status lands in CALL_CODE_F, the
# `.result.isError` flag in CALL_ERR_F, and the tool text on stdout. That
# classification is the trust boundary this suite actually turns on — and it
# shipped fail-open: a 200 carrying a non-JSON body left CALL_ERR_F empty,
# which ok_call read as a pass, so a broken transport scored green.
#
# NO NETWORK, NO CREDENTIALS, NO LIVE ORG: `curl` is shadowed by a stub on
# PATH with three modes (success / transport-failure / malformed), and the
# REAL rpc/call/ok_call/refused_call are sourced from acceptance_transport.sh
# through its TRANSPORT_SOURCE_ONLY seam. Fake PAPERCLIP_* env satisfies the
# lib's requires; nothing is fetched, no server is started.
#
# BASH_ENV resets PATH in every non-interactive bash (sandbox shim trap), so a
# child would lose this stub at the front of PATH and hit the real network
# instead. Unset it so the stub holds, for the same reason
# test_notify_transport.sh does.
unset BASH_ENV
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v jq >/dev/null || { echo "ERROR: jq required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 required" >&2; exit 1; }

# The seam must be present BEFORE sourcing: without it, sourcing would fall
# through into the live run (fetch the org, boot the server). A deleted seam
# must redden this suite, not execute a live suite with fake env.
grep -Fq 'TRANSPORT_SOURCE_ONLY' "$ROOT/acceptance_transport.sh" \
  || { echo "FAIL: TRANSPORT_SOURCE_ONLY seam missing from acceptance_transport.sh" >&2; exit 1; }

TDIR="$(mktemp -d)"
BIN="$TDIR/bin"; mkdir -p "$BIN"

# --- the seam: a curl with three transports ----------------------------------
# Emulates only the flags rpc() uses: -s -o FILE -w '%{http_code}' -X POST
# -H ... -d BODY URL. Mode comes from $STUB_MODE.
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
outfile=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" == "-o" ]] && outfile="${args[i+1]}"
done
case "${STUB_MODE:-}" in
  success)
    cat > "$outfile" <<'JSON'
{"jsonrpc":"2.0","id":1,"result":{"content":[{"text":"stub decision text"}],"isError":false}}
JSON
    printf '200'; exit 0 ;;
  transport-failure)
    printf '000'; exit 7 ;;
  malformed)
    printf 'this is not json{{' > "$outfile"
    printf '200'; exit 0 ;;
  jsonrpc-error)
    cat > "$outfile" <<'JSON'
{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Method not found"}}
JSON
    printf '200'; exit 0 ;;
  *)
    echo "stub curl: unknown STUB_MODE '${STUB_MODE:-}'" >&2; exit 99 ;;
esac
STUB
chmod +x "$BIN/curl"

# --- source the REAL helpers -------------------------------------------------
# Fake env satisfies acceptance_org_lib.sh's requires. Sourcing defines the
# helpers and returns at the seam; the live run below it never starts.
export PAPERCLIP_API_KEY="CANARY-NOT-A-KEY"
export PAPERCLIP_API_URL="https://paperclip.invalid"
export COMPANY_ID="00000000-0000-4000-8000-000000000003"
export TRANSPORT_SOURCE_ONLY=1
# shellcheck disable=SC1091
PATH="$BIN:$PATH" source "$ROOT/acceptance_transport.sh"
unset TRANSPORT_SOURCE_ONLY
LIB_TMP="$TMP"   # $TMP now names the lib's scratch, not the test's
trap 'rm -rf "$TDIR" "$LIB_TMP"' EXIT
export PATH="$BIN:$PATH"

AGENT="00000000-0000-4000-8000-000000000020"

# --- assertion helpers (ok/bad/hdr/eq come from the sourced lib) --------------
# run_check runs call (which touches no PASS/FAIL counter) in this shell.
# run_sut runs ok_call/refused_call in a SUBSHELL: their internal ok/bad
# verdicts are evidence as text, and must not move this suite's counters —
# an expected SUT refusal would otherwise read as a test failure.
run_check() {
  local out_f="$TDIR/check.out" err_f="$TDIR/check.err"
  set +e
  "$@" >"$out_f" 2>"$err_f"; CHECK_RC=$?
  set -e
  CHECK_OUT="$(cat "$out_f")"; CHECK_ERR="$(cat "$err_f")"
}
run_sut() {
  local err_f="$TDIR/sut.err"
  set +e
  SUT_OUT="$("$@" 2>"$err_f")"
  set -e
  SUT_ERR="$(cat "$err_f")"
}
# names_cause <fragment> <text>: the captured stderr is exactly one line and
# names the cause.
names_cause() {
  local lines; lines="$(printf '%s' "$2" | grep -c . || true)"
  [[ "$lines" -eq 1 ]] || { bad "stderr is $lines lines, want exactly one (got: $2)"; return; }
  grep -qF "$1" <<<"$2" \
    && ok "stderr names the cause ($1)" \
    || bad "stderr does not name '$1' (got: $2)"
}

# ===========================================================================
hdr "1. Success: a well-formed reply passes and yields its text"
export STUB_MODE=success
run_check call "$AGENT" read_my_requests '{}'
[[ "$CHECK_RC" -eq 0 ]] && ok "call exits 0 on success" || bad "call exited $CHECK_RC on success"
[[ "$CHECK_OUT" == "stub decision text" ]] \
  && ok "call prints the tool text" || bad "call printed '$CHECK_OUT'"
[[ -z "$CHECK_ERR" ]] && ok "call prints no stderr on success" || bad "unexpected stderr: $CHECK_ERR"
eq "call_code is 200" "$(call_code)" "200"
eq "call_err is false" "$(call_err)" "false"
reply_ok && ok "reply_ok holds" || bad "reply_ok does not hold on success"

run_sut ok_call "success is accepted" "$AGENT" read_my_requests '{}'
grep -q "PASS" <<<"$SUT_OUT" && ok "ok_call passes the success case" \
  || bad "ok_call failed the success case: $SUT_OUT"
[[ -z "$SUT_ERR" ]] && ok "ok_call prints no stderr on success" || bad "unexpected stderr: $SUT_ERR"
run_sut refused_call "success is not a refusal" "$AGENT" read_my_requests '{}'
grep -q "FAIL" <<<"$SUT_OUT" && ok "refused_call rejects the success case" \
  || bad "refused_call accepted the success case: $SUT_OUT"

# ===========================================================================
hdr "2. Transport failure fails closed and names curl's exit"
export STUB_MODE=transport-failure
run_check call "$AGENT" read_my_requests '{}'
[[ "$CHECK_RC" -ne 0 ]] && ok "call exits non-zero on transport failure (rc=$CHECK_RC)" \
  || bad "call exited 0 on transport failure"
names_cause "transport failure" "$CHECK_ERR"
eq "call_code is 000" "$(call_code)" "000"
reply_ok && bad "reply_ok holds after a transport failure" || ok "reply_ok does not hold"

run_sut ok_call "transport failure is not a pass" "$AGENT" read_my_requests '{}'
grep -q "unparseable reply" <<<"$SUT_OUT" && ok "ok_call fails the transport failure, naming the cause" \
  || bad "ok_call passed a transport failure or hid the cause: $SUT_OUT"
names_cause "transport failure" "$SUT_ERR"
run_sut refused_call "transport failure is not a refusal either" "$AGENT" read_my_requests '{}'
grep -q "FAIL" <<<"$SUT_OUT" && ok "refused_call fails the transport failure" \
  || bad "refused_call passed a transport failure: $SUT_OUT"

# ===========================================================================
hdr "3. Malformed reply fails closed and names the missing result"
# THE regression: before the case_6d842160 fix this section's ok_call read an
# empty CALL_ERR_F as a pass, so a 200 with a non-JSON body scored green.
export STUB_MODE=malformed
run_check call "$AGENT" read_my_requests '{}'
[[ "$CHECK_RC" -ne 0 ]] && ok "call exits non-zero on a malformed reply (rc=$CHECK_RC)" \
  || bad "call exited 0 on a malformed reply"
names_cause "malformed reply" "$CHECK_ERR"
eq "call_code is still the transport's 200" "$(call_code)" "200"
reply_ok && bad "reply_ok holds on a malformed reply" || ok "reply_ok does not hold"

run_sut ok_call "malformed reply is not a pass" "$AGENT" read_my_requests '{}'
grep -q "unparseable reply" <<<"$SUT_OUT" && ok "ok_call fails the malformed reply, naming the cause" \
  || bad "ok_call passed a malformed reply or hid the cause: $SUT_OUT"
names_cause "malformed reply" "$SUT_ERR"
run_sut refused_call "malformed reply is not a refusal either" "$AGENT" read_my_requests '{}'
grep -q "FAIL" <<<"$SUT_OUT" && ok "refused_call fails the malformed reply" \
  || bad "refused_call passed a malformed reply: $SUT_OUT"

# ===========================================================================
hdr "4. JSON-RPC error body fails closed and names the missing result"
# Valid JSON but no .result: the reviewer's repro (200 carrying an error
# object). Direct call() sites that skip reply_ok would read an empty
# CALL_ERR_F as a pass here, so this pins the same gate as section 3.
export STUB_MODE=jsonrpc-error
run_check call "$AGENT" read_my_requests '{}'
[[ "$CHECK_RC" -ne 0 ]] && ok "call exits non-zero on a JSON-RPC error body (rc=$CHECK_RC)" \
  || bad "call exited 0 on a JSON-RPC error body"
names_cause "malformed reply" "$CHECK_ERR"
eq "call_code is still the transport's 200" "$(call_code)" "200"
reply_ok && bad "reply_ok holds on a JSON-RPC error body" || ok "reply_ok does not hold"

run_sut ok_call "JSON-RPC error body is not a pass" "$AGENT" read_my_requests '{}'
grep -q "unparseable reply" <<<"$SUT_OUT" && ok "ok_call fails the JSON-RPC error body, naming the cause" \
  || bad "ok_call passed a JSON-RPC error body or hid the cause: $SUT_OUT"
names_cause "malformed reply" "$SUT_ERR"
run_sut refused_call "JSON-RPC error body is not a refusal either" "$AGENT" read_my_requests '{}'
grep -q "FAIL" <<<"$SUT_OUT" && ok "refused_call fails the JSON-RPC error body" \
  || bad "refused_call passed a JSON-RPC error body: $SUT_OUT"

printf '\nRESULT: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
