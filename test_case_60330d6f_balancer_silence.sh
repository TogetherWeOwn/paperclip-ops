#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_balancer_silence.sh — offline suite for the case_e6def006
# balancer-silence notification binding
# (ops/case_658bf6b9/gatus-balancer-silence.yaml, push-balancer-silence.sh,
# MODULES.md registry).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRAGMENT SHAPE — the dedicated platform/balancer-silence external
#      endpoint declares token/heartbeat/alerts; alerts are known types
#      (discord|custom); heartbeat 10m (2x the 5m push); hourly reminder
#      while the breach holds (established queue-age setting).
#   2. NO SECRETS — token/URLs are ${ENV} only; Bearer travels in a 0600
#      curl config, never argv.
#   3. REFUSALS — missing GATUS_URL/token/guard refuses (exit 2) with zero
#      pushes; the 10m heartbeat miss then pages, never a silent green.
#   4. FAIL-CLOSED — stale (193-min gap fixture) pushes success=false
#      (exit 1); missing/empty/garbage log pushes success=false (exit 1),
#      never success=true. Fresh pushes success=true (exit 0): the recovery
#      signal that closes the incident. Stale plus a valid retirement file
#      pushes success=true as "balancer retired:"; a malformed file stays
#      false, and a fresh log stays fresh however the file reads.
#   5. DELIVERY FAILURE VISIBLE — a 500 from Gatus exits nonzero.
#   6. REGISTRY — MODULES.md lists the endpoint key, fragment, push script
#      and this suite.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. Stub-server /
# curl-shim pattern from test_case_60330d6f_cliproxy_lanes.sh.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
FRAG="$SRC/gatus-balancer-silence.yaml"
PUSH="$SRC/push-balancer-silence.sh"
REG="$SRC/MODULES.md"
GUARD="$HERE/balancer_silence_guard.py"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$FRAG" ]] || { echo "REFUSE: $FRAG not found"; exit 3; }
[[ -f "$PUSH" ]] || { echo "REFUSE: $PUSH not found"; exit 3; }
[[ -f "$REG" ]] || { echo "REFUSE: $REG not found"; exit 3; }
[[ -f "$GUARD" ]] || { echo "REFUSE: $GUARD not found"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }

# Verbatim rows around the 2026-10-04 193-min silence (from
# test_balancer_silence_guard.py GAP_ROWS).
cat > "$WORK/gap.log" <<'LOG'
{"at": "2026-10-04T14:11:11.431481+00:00", "model": "gpt-6.1-sol", "reason": "muse_collective_95", "changed": 0}
{"at": "2026-10-04T14:14:31.672897+00:00", "model": "gpt-6.1-sol", "reason": "muse_collective_95", "changed": 0}
{"at": "2026-10-04T17:28:14.425776+00:00", "model": "muse-spark-1.3-contributor(xhigh)", "reason": "meta_weekly_below_98", "changed": 0}
{"at": "2026-10-04T17:29:20.794090+00:00", "model": "muse-spark-1.3-contributor(xhigh)", "reason": "meta_weekly_below_98", "changed": 0}
LOG
echo '{}' > "$WORK/state.json"
python3 - <<PY > "$WORK/fresh.log"
from datetime import datetime, timedelta, timezone
end = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
for n in range(5, 0, -1):
    print('{"at": "%s", "model": "m", "reason": "r"}' % (end - timedelta(minutes=n)).isoformat())
print('{"at": "%s", "model": "m", "reason": "r"}' % end.isoformat())
PY

# --- stub Gatus server: records pushes to $WORK/pushes.jsonl ----------------
MODE="${case_14a0b693_STUB_MODE:-ok}"
cat > "$WORK/server.py" <<'PY'
import http.server, json, os, sys
PUSH = os.environ["PUSH_FILE"]
MODE = os.environ.get("case_14a0b693_STUB_MODE", "ok")
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with open(PUSH, "a") as f:
            f.write(json.dumps({"path": self.path,
                                "auth": self.headers.get("Authorization"),
                                "body": body.decode("utf-8", "replace")}) + "\n")
        self.send_response(500 if MODE == "fail" else 200)
        self.end_headers(); self.wfile.write(b"ERR" if MODE == "fail" else b"OK")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
PORT="${case_14a0b693_TEST_PORT:-8799}"
PUSH_FILE="$WORK/pushes.jsonl"
export PUSH_FILE case_14a0b693_STUB_MODE="$MODE"
: > "$PUSH_FILE"
python3 "$WORK/server.py" "$PORT" & SRV_PID=$!
sleep 0.5
kill -0 "$SRV_PID" 2>/dev/null || { echo "REFUSE: stub server did not start"; exit 3; }

# --- curl shim: records argv, execs real curl --------------------------------
REAL_CURL="$(command -v curl)"
mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$WORK/argv.log"
exec "$REAL_CURL" "\$@"
SHIM
chmod +x "$WORK/bin/curl"
: > "$WORK/argv.log"
export PATH="$WORK/bin:$PATH"

run_push() {  # env overrides via BALANCER_* + GATUS_* already exported; returns rc
  # NOTE: the push history APPENDS (truncated once at setup). Truncating
  # here would erase the stale push before the wire-text check below can
  # see it, making that check structurally vacuous (case_c9a76c61 review).
  # Snapshot the record count first: 4a-4e assert on the record written by
  # THEIR OWN run (index RUN_PUSH_BEFORE), so a run that pushes nothing
  # reads MISSING instead of a stale record (Paperclip Review 4/5).
  RUN_PUSH_BEFORE=$(wc -l < "$PUSH_FILE")
  : > "$WORK/argv.log"
  "$PUSH" >"$WORK/last.out" 2>&1
}

own_push_path() {  # path of the first record appended by the last run_push, or MISSING
  python3 - "$PUSH_FILE" "${RUN_PUSH_BEFORE:-0}" <<'PY' 2>/dev/null
import json, sys
try:
    print(json.loads(open(sys.argv[1]).read().splitlines()[int(sys.argv[2])])["path"])
except (IndexError, ValueError, KeyError):
    print("MISSING")
PY
}

base_env() {
  export GATUS_URL="http://127.0.0.1:$PORT"
  export GATUS_EXTERNAL_TOKEN="test-token"
  export BALANCER_SILENCE_GUARD="$GUARD"
  export BALANCER_SILENCE_STATE="$WORK/state.json"
  unset BALANCER_SILENCE_NOW || true
  unset BALANCER_SILENCE_RETIREMENT || true
}

# === 1. FRAGMENT SHAPE ========================================================
shape_out="$(python3 - "$FRAG" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
errs = []
if "name: balancer-silence" not in text: errs.append("missing name balancer-silence")
if "group: platform" not in text: errs.append("missing group platform")
if 'token: "${GATUS_EXTERNAL_TOKEN}"' not in text: errs.append("token must be ${GATUS_EXTERNAL_TOKEN}")
if "interval: 10m" not in text: errs.append("heartbeat must be 10m")
atypes = set(re.findall(r"-\s*type:\s*(\S+)", text))
if atypes - {"discord", "custom"}: errs.append(f"unknown alert types {sorted(atypes)}")
if {"discord", "custom"} - atypes: errs.append(f"need discord+custom, got {sorted(atypes)}")
if "minimum-reminder-interval: 60m" not in text: errs.append("need 60m reminder (queue-age setting)")
print("ERRORS:" + ";".join(errs) if errs else "SHAPE-OK")
PY
)"
if [[ "$shape_out" == SHAPE-OK ]]; then ok "1. fragment shape: dedicated platform/balancer-silence, 10m heartbeat, discord+custom, 60m reminder";
else bad "1. fragment shape" "$shape_out"; fi
# positive control: alert-less mutant must be caught
mut="$WORK/nalert.yaml"
python3 - "$FRAG" "$mut" <<'PY'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
out, i, dropped = [], 0, False
while i < len(lines):
    if not dropped and re.match(r"^    alerts:\s*$", lines[i]):
        dropped = True; i += 1
        while i < len(lines) and re.match(r"^      ", lines[i]): i += 1
        continue
    out.append(lines[i]); i += 1
open(sys.argv[2], "w").write("\n".join(out) + "\n")
PY
mut_out="$(python3 - "$mut" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
atypes = set(re.findall(r"-\s*type:\s*(\S+)", text))
print("ERRORS:alerts stripped" if {"discord","custom"} - atypes else "SHAPE-OK")
PY
)"
if [[ "$mut_out" == ERRORS* ]]; then ok "1c. alert-less fragment mutant goes red";
else bad "1c. alert-less fragment mutant" "mutant still reads clean"; fi
# dedicated key: code lines (not comments) must use platform_balancer-silence
# and no other endpoint key (capture-first: no producer pipes into an
# early-exiting grep under pipefail)
push_code="$(grep -vE '^\s*#' "$PUSH")"
if grep -q 'ENDPOINT_KEY="platform_balancer-silence"' "$PUSH" \
  && ! grep -qE 'platform_(oom-kills|discord-bot|ci-queue-age|memory|lxc-ci-vms|tls-expiry|garm-pool|garm-reaper)' <<<"$push_code"; then
  ok "1d. dedicated endpoint key platform_balancer-silence, no reused key"
else bad "1d. dedicated endpoint key" "pusher must pin platform_balancer-silence only"; fi

# === 2. NO SECRETS ============================================================
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|Bearer [A-Za-z0-9._~-]{8,}' "$FRAG" "$PUSH" || true)"
if [[ -n "$sec_hits" ]]; then bad "2. live secret in module" "$sec_hits";
else ok "2. no secret-shaped strings in module (placeholders only)"; fi
if grep -q '\${GATUS_EXTERNAL_TOKEN}' "$FRAG" \
  && grep -q 'GATUS_EXTERNAL_TOKEN' "$PUSH"; then
  ok "2b. token via \${ENV} only (fragment + env check)"
else bad "2b. env indirection" "expected \${GATUS_EXTERNAL_TOKEN} references"; fi
if grep -qE 'curl .*(-H|Authorization: Bearer \$)' <<<"$(grep -vE '^\s*#' "$PUSH")"; then
  bad "2d. argv credential in push script" "Bearer must travel in curl config only"
else ok "2d. push script passes no credential in argv (curl config only)"; fi

# === 3. REFUSALS (exit 2, zero pushes) ========================================
base_env
GATUS_URL="" bash -c 'export GATUS_URL=""; export GATUS_EXTERNAL_TOKEN=x; export BALANCER_SILENCE_GUARD="'"$GUARD"'"; "'"$PUSH"'"' >"$WORK/r.out" 2>&1; rc=$?
if [[ $rc -eq 2 ]]; then ok "3a. missing GATUS_URL refuses (exit 2)";
else bad "3a. missing GATUS_URL refuses" "rc=$rc"; fi
base_env
GATUS_EXTERNAL_TOKEN="" bash -c 'export GATUS_URL="http://127.0.0.1:'"$PORT"'"; export GATUS_EXTERNAL_TOKEN=""; export BALANCER_SILENCE_GUARD="'"$GUARD"'"; "'"$PUSH"'"' >"$WORK/r.out" 2>&1; rc=$?
if [[ $rc -eq 2 ]]; then ok "3b. missing token refuses (exit 2)";
else bad "3b. missing token refuses" "rc=$rc"; fi
base_env
BALANCER_SILENCE_GUARD="$WORK/absent.py" bash -c 'export GATUS_URL="http://127.0.0.1:'"$PORT"'"; export GATUS_EXTERNAL_TOKEN=x; export BALANCER_SILENCE_GUARD="'"$WORK"'/absent.py"; "'"$PUSH"'"' >"$WORK/r.out" 2>&1; rc=$?
if [[ $rc -eq 2 ]]; then ok "3c. missing guard refuses (exit 2)";
else bad "3c. missing guard refuses" "rc=$rc"; fi

# === 4. FAIL-CLOSED SEMANTICS (mocked transport) ==============================
base_env
export BALANCER_SILENCE_LOG="$WORK/fresh.log"
export BALANCER_SILENCE_NOW="2026-10-05T05:00:00+00:00"
run_push; rc=$?
push_path="$(own_push_path)"
if [[ $rc -eq 0 && "$push_path" == *"/api/v1/endpoints/platform_balancer-silence/external?success=true"* ]]; then
  ok "4a. fresh log pushes success=true (exit 0, recovery signal)"
else bad "4a. fresh log pushes success=true" "rc=$rc path=$push_path"; fi

export BALANCER_SILENCE_LOG="$WORK/gap.log"
export BALANCER_SILENCE_NOW="2026-10-04T17:45:00+00:00"
run_push; rc=$?
push_path="$(own_push_path)"
if [[ $rc -eq 1 && "$push_path" == *"success=false"* && "$push_path" == *"platform_balancer-silence"* ]]; then
  ok "4b. 193-min gap fixture pushes success=false (exit 1, stale never healthy)"
else bad "4b. stale pushes success=false" "rc=$rc path=$push_path"; fi
# positive control: a mutant mapping stale to true must go red here
if grep -q 'gatus_push "$ENDPOINT_KEY" false "balancer stale' "$PUSH"; then
  ok "4c. stale branch pins success=false (mutant to true would flip this line)"
else bad "4c. stale branch" "stale push line missing/false expected"; fi

for case in missing empty garbage; do
  case "$case" in
    missing) export BALANCER_SILENCE_LOG="$WORK/absent.log" ;;
    empty) : > "$WORK/empty.log"; export BALANCER_SILENCE_LOG="$WORK/empty.log" ;;
    garbage) printf 'not json\n{no at}\n' > "$WORK/garbage.log"; export BALANCER_SILENCE_LOG="$WORK/garbage.log" ;;
  esac
  unset BALANCER_SILENCE_NOW || true
  run_push; rc=$?
  push_path="$(own_push_path)"
  if [[ $rc -eq 1 && "$push_path" == *"success=false"* ]]; then
    ok "4d-$case. $case log pushes success=false (exit 1, fail-closed)"
  else bad "4d-$case. $case log fail-closed" "rc=$rc path=$push_path"; fi
done
# recovery: stale then fresh — the second push must be true (incident closes)
export BALANCER_SILENCE_LOG="$WORK/fresh.log"
export BALANCER_SILENCE_NOW="2026-10-05T05:00:00+00:00"
run_push; rc=$?
push_path="$(own_push_path)"
if [[ $rc -eq 0 && "$push_path" == *"success=true"* ]]; then
  ok "4e. recovery push is success=true (fresh after stale closes the incident)"
else bad "4e. recovery push" "rc=$rc path=$push_path"; fi
# wire text carries the verdict, never raw model rows. Non-vacuity is
# structural, not assumed (case_c9a76c61 review): the gap fixture MUST contain
# a muse-spark row (danger present in the input), the push history MUST
# contain both verdicts (the assertion channel sees push content), and
# every record MUST carry the captured body (full wire content, not
# path+auth only). Only then does absence of the model string mean no leak.
if ! grep -q "muse-spark" "$WORK/gap.log"; then
  bad "4f. wire text" "gap.log lacks muse-spark rows — absence would be vacuous"
elif ! grep -q "success=false" "$PUSH_FILE" || ! grep -q "success=true" "$PUSH_FILE"; then
  bad "4f. wire text" "push history lacks both verdicts — assertion sees nothing"
elif ! python3 -c 'import json,sys; recs=[json.loads(l) for l in open(sys.argv[1])]; assert recs and all("body" in r for r in recs)' "$PUSH_FILE" 2>/dev/null; then
  bad "4f. wire text" "push records lack captured bodies — stub sees path+auth only"
elif grep -q "muse-spark" "$PUSH_FILE"; then
  bad "4f. wire text" "raw model string reached the push record"
else ok "4f. wire text carries verdict only (no raw model rows)"; fi

# --- retirement fixtures: explicit evidence converts stale to retired -------
cat > "$WORK/retired.json" <<'JSON'
{"retired": true, "producer": "fleet-quota-balancer", "successor": "native-routing",
 "approved_by": "retirement-approver-sentinel", "approved_at": "2026-10-04T17:40:00+00:00"}
JSON
echo 'not json{' > "$WORK/retired-bad.json"
own_push_text() {  # URL-decoded query text of the record appended by the last run_push, or MISSING
  python3 - "$PUSH_FILE" "${RUN_PUSH_BEFORE:-0}" <<'PY' 2>/dev/null
import json, sys, urllib.parse
try:
    rec = json.loads(open(sys.argv[1]).read().splitlines()[int(sys.argv[2])])
    print(urllib.parse.unquote(rec["path"]))
except (IndexError, ValueError, KeyError):
    print("MISSING")
PY
}

export BALANCER_SILENCE_LOG="$WORK/gap.log"
export BALANCER_SILENCE_NOW="2026-10-04T17:45:00+00:00"
export BALANCER_SILENCE_RETIREMENT="$WORK/retired.json"
run_push; rc=$?
push_path="$(own_push_path)"
push_text="$(own_push_text)"
if [[ $rc -eq 0 && "$push_path" == *"success=true"* && "$push_text" == *"balancer retired:"* ]]; then
  ok "4g. stale + valid retirement pushes success=true (exit 0, approved stillness)"
else bad "4g. retired pushes success=true" "rc=$rc path=$push_path text=$push_text"; fi
# positive control: a mutant mapping retired to false must go red here
if grep -q 'gatus_push "$ENDPOINT_KEY" true "balancer retired' "$PUSH"; then
  ok "4h. retired branch pins success=true (mutant to false would flip this line)"
else bad "4h. retired branch" "retired push line missing/true expected"; fi

export BALANCER_SILENCE_RETIREMENT="$WORK/retired-bad.json"
run_push; rc=$?
push_path="$(own_push_path)"
if [[ $rc -eq 1 && "$push_path" == *"success=false"* ]]; then
  ok "4i. stale + malformed retirement pushes success=false (exit 1, fail-closed)"
else bad "4i. malformed retirement fail-closed" "rc=$rc path=$push_path"; fi

export BALANCER_SILENCE_LOG="$WORK/fresh.log"
export BALANCER_SILENCE_NOW="2026-10-05T05:00:00+00:00"
export BALANCER_SILENCE_RETIREMENT="$WORK/retired.json"
run_push; rc=$?
push_path="$(own_push_path)"
push_text="$(own_push_text)"
if [[ $rc -eq 0 && "$push_path" == *"success=true"* && "$push_text" == *"balancer fresh:"* && "$push_text" != *"retired"* ]]; then
  ok "4j. fresh + valid retirement still pushes fresh (marker excuses silence only)"
else bad "4j. fresh with marker stays fresh" "rc=$rc path=$push_path text=$push_text"; fi
unset BALANCER_SILENCE_RETIREMENT || true
# retirement detail never reaches the wire: the approver/successor strings
# stay in the journal. Structural non-vacuity as in 4f: the marker MUST
# carry the sentinel, the decoded history MUST contain a retired push, every
# record MUST carry a body — only then does absence mean no leak. (The
# history is URL-encoded on the wire, so decode before asserting either way:
# an encoded leak must fail exactly like a literal one.)
python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open(sys.argv[1]).read()))' \
  "$PUSH_FILE" > "$WORK/pushes.decoded"
if ! grep -q "retirement-approver-sentinel" "$WORK/retired.json"; then
  bad "4k. retirement non-disclosure" "marker lacks sentinel — absence would be vacuous"
elif ! grep -q "balancer retired:" "$WORK/pushes.decoded"; then
  bad "4k. retirement non-disclosure" "history lacks a retired push — assertion sees nothing"
elif grep -q "retirement-approver-sentinel" "$WORK/pushes.decoded"; then
  bad "4k. retirement non-disclosure" "approver string reached the push record"
elif grep -q '"successor"' "$WORK/pushes.decoded"; then
  bad "4k. retirement non-disclosure" "marker field reached the push record"
else ok "4k. retirement detail stays local (verdict line only on the wire)"; fi

# === 5. DELIVERY FAILURE + NON-DISCLOSURE =====================================
if grep -q "test-token" "$WORK/argv.log" 2>/dev/null; then
  bad "5a. argv non-disclosure" "token reached curl argv"
else ok "5a. Bearer never reaches curl argv (curl config only)"; fi
# restart stub in fail mode for one push
kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null || true
case_14a0b693_STUB_MODE=fail PUSH_FILE="$PUSH_FILE" python3 "$WORK/server.py" "$PORT" & SRV_PID=$!
sleep 0.5
export BALANCER_SILENCE_LOG="$WORK/fresh.log"
export BALANCER_SILENCE_NOW="2026-10-05T05:00:00+00:00"
run_push; rc=$?
if [[ $rc -ne 0 ]]; then ok "5b. Gatus 500 exits nonzero (delivery failure visible, rc=$rc)";
else bad "5b. Gatus 500 visible" "rc=0 on HTTP 500"; fi
# restore ok stub for any later run
kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null || true
case_14a0b693_STUB_MODE=ok PUSH_FILE="$PUSH_FILE" python3 "$WORK/server.py" "$PORT" & SRV_PID=$!
sleep 0.5

# === 6. REGISTRY ==============================================================
if grep -q "balancer-silence" "$REG" \
  && grep -q "push-balancer-silence.sh" "$REG" \
  && grep -q "gatus-balancer-silence.yaml" "$REG" \
  && grep -q "test_case_60330d6f_balancer_silence.sh" "$REG"; then
  ok "6. MODULES.md registers endpoint, fragment, pusher and suite"
else bad "6. MODULES.md registry" "endpoint/fragment/pusher/suite entry missing"; fi

printf '\npass=%d fail=%d\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
