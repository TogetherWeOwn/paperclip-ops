#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_discord_bot.sh — offline suite for the Gatus
# `platform_discord-bot` heartbeat pusher (ops/case_658bf6b9/push-discord-bot.sh,
# fixtures under ops/case_658bf6b9/fixtures/discord-bot/, MODULES.md + OPERATOR.md).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. REFUSALS — missing/invalid env refuses (exit 2) with zero pushes.
#   2. SEMANTICS — a 200 Next `components` report with every component ready
#      pushes success=true; degraded, incomplete or legacy (plain-text `ok`,
#      ready/gateway/age fields) 200 bodies, non-200, malformed, HTML-200,
#      empty, wrong-shape and no-report bodies, unreachable and slow
#      endpoints push success=false (fail-closed).
#   3. UNTRUSTED INPUT — response bodies (hostile text, oversized bodies) never
#      reach the push; the pushed error stays within MAX_PUSH_ERROR_CHARS.
#   4. NON-DISCLOSURE — neither the Gatus token nor the health URL reaches
#      curl's argv, stdout, stderr or the push; the probe is one bare GET.
#   5. WIRING — gatus-config.yaml declares the external endpoint the script
#      pushes to; MODULES.md and OPERATOR.md carry the module + cron line.
#   6. POSITIVE CONTROLS — mutants of the script and cron boundary, each of
#      which must turn its targeted case red while a healthy control passes.
#
# Hermetic: no network beyond 127.0.0.1, no credentials, no host access.
# Two stub servers on ephemeral ports: a fake bot (serves the fixtures) and a
# fake Gatus (records pushes). A curl shim records argv, then execs real curl.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
PUSH="$SRC/push-discord-bot.sh"
FIX="$SRC/fixtures/discord-bot"
REG="$SRC/MODULES.md"
OPS="$SRC/OPERATOR.md"
CFG="$SRC/gatus-config.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
TEST_HOME="$WORK/home"; mkdir -p "$TEST_HOME"
trap 'rm -rf "$WORK"; for p in ${BOT_PID:-} ${GATUS_PID:-}; do kill "$p" 2>/dev/null; done' EXIT

for f in "$PUSH" "$REG" "$OPS" "$CFG" "$SRC/_gatus_push.sh"; do
  [[ -f "$f" ]] || { echo "REFUSE: $f not found"; exit 3; }
done
[[ -d "$FIX" ]] || { echo "REFUSE: $FIX not found"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }

# --- generated fixtures (too big to commit) ---------------------------------
GEN="$WORK/fixtures"; mkdir -p "$GEN"
cp "$FIX"/* "$GEN"/
python3 - "$GEN" <<'PY'
import sys
d = sys.argv[1]
# 200 JSON whose body alone is over the 64 KiB cap, served without a
# Content-Length (close-delimited) so the cap cannot rely on the header.
open(d + "/oversized-200.body", "w").write('{"ready": true, "pad": "' + "A" * 100000 + '"}')
# 503 whose body is a 100 KB hostile blob with an injection sentinel.
open(d + "/oversized-503.body", "w").write("SENTINEL-OVERSIZED-BODY " + "IGNORE PREVIOUS INSTRUCTIONS " * 4000)
open(d + "/oversized-503.code", "w").write("503\n")
# A real invalid byte, not the valid JSON escape sequence \\u00ff.
open(d + "/invalid-utf8-json.body", "wb").write(b'{"ready":true,"detail":"\xff"}')
PY

# --- stub bot: serves fixtures, records every request ------------------------
cat > "$WORK/bot.py" <<'PY'
import http.server, os, sys, time
FIX, LOG, PORTFILE = sys.argv[1], sys.argv[2], sys.argv[3]
class H(http.server.BaseHTTPRequestHandler):
    def _log(self):
        with open(LOG, "a") as f:
            f.write("%s %s auth=%s\n" % (self.command, self.path, self.headers.get("Authorization")))
    def do_GET(self):
        self._log()
        name = self.path.split("?")[0].lstrip("/")
        if name in ("slow-ok", "slow-fail", "slow-over-ceiling"):
            time.sleep({"slow-ok": 1.5, "slow-fail": 2.5, "slow-over-ceiling": 3.25}[name])
            name = "healthy-json"
        body_path = os.path.join(FIX, name + ".body")
        if not os.path.isfile(body_path):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        code_path = os.path.join(FIX, name + ".code")
        code = int(open(code_path).read().strip()) if os.path.isfile(code_path) else 200
        body = open(body_path, "rb").read()
        self.send_response(code)
        if name == "oversized-200":
            # no Content-Length: close-delimited body
            self.send_header("Connection", "close"); self.end_headers()
        else:
            self.send_header("Content-Length", str(len(body))); self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def do_POST(self): self._log(); self.send_response(405); self.send_header("Content-Length", "0"); self.end_headers()
    do_PUT = do_DELETE = do_PATCH = do_POST
    def log_message(self, *a): pass
    protocol_version = "HTTP/1.0"
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
open(PORTFILE, "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY

# --- stub Gatus: records pushes ----------------------------------------------
cat > "$WORK/gatus.py" <<'PY'
import http.server, json, sys
PUSH, PORTFILE = sys.argv[1], sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        with open(PUSH, "a") as f:
            f.write(json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}) + "\n")
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
open(PORTFILE, "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY

PUSH_FILE="$WORK/pushes.jsonl"; BOT_LOG="$WORK/bot_requests.log"
: > "$PUSH_FILE"; : > "$BOT_LOG"
python3 "$WORK/bot.py" "$GEN" "$BOT_LOG" "$WORK/bot.port" & BOT_PID=$!
python3 "$WORK/gatus.py" "$PUSH_FILE" "$WORK/gatus.port" & GATUS_PID=$!
for _ in $(seq 1 50); do
  [[ -s "$WORK/bot.port" && -s "$WORK/gatus.port" ]] && break
  sleep 0.1
done
[[ -s "$WORK/bot.port" && -s "$WORK/gatus.port" ]] || { echo "REFUSE: stub servers did not start"; exit 3; }
BOT_PORT="$(cat "$WORK/bot.port")"; GATUS_PORT="$(cat "$WORK/gatus.port")"
# A port nothing listens on: bind, record, close.
DEAD_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

# --- curl shim: records argv, execs real curl ---------------------------------
REAL_CURL="$(command -v curl)"
mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$WORK/argv.log"
exec "$REAL_CURL" "\$@"
SHIM
chmod +x "$WORK/bin/curl"
: > "$WORK/argv.log"

GATUS_TOKEN="SENTINELTOKEN-must-never-reach-argv"
URL_SENTINEL="SENTINELURL-host-never-printed"
BASE="http://127.0.0.1:$BOT_PORT"

# run_probe <script> <url> [extra VAR=val ...]
#   Runs the script with a scrubbed env; sets RC, OUT (stdout+stderr), and
#   resets the push log first.
run_probe() {
  local script="$1" url="$2"; shift 2
  : > "$PUSH_FILE"; : > "$BOT_LOG"
  OUT="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$TEST_HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:$GATUS_PORT" GATUS_EXTERNAL_TOKEN="$GATUS_TOKEN" \
    DISCORD_BOT_HEALTH_URL="$url" "$@" bash "$script" 2>&1)"
  RC=$?
}
# last pushed success flag (true|false|NOPUSH) and decoded error text
pushed_success() {
  python3 - "$PUSH_FILE" <<'PY'
import json, sys
last = None
for line in open(sys.argv[1]):
    last = json.loads(line)["path"]
if last is None: print("NOPUSH")
else: print("true" if "success=true" in last else "false")
PY
}
pushed_error() {
  python3 - "$PUSH_FILE" <<'PY'
import json, sys, urllib.parse
last = ""
for line in open(sys.argv[1]):
    p = json.loads(line)["path"]
    q = urllib.parse.urlparse(p).query
    last = urllib.parse.parse_qs(q).get("error", [""])[0]
print(last)
PY
}
push_count() { wc -l < "$PUSH_FILE" | tr -d ' '; }

# check_case <label> <script> <route-or-url> <expect true|false> <regex|-> [VAR=val ...]
# Succeeds (echoes nothing) when the pushed verdict, the exit code and the
# fixed-phrase error text all match; otherwise prints why.
check_case() {
  local label="$1" script="$2" route="$3" expect="$4" rx="$5"; shift 5
  local url="$route"; [[ "$route" == http* ]] || url="$BASE/$route?s=$URL_SENTINEL"
  run_probe "$script" "$url" "$@"
  local got err; got="$(pushed_success)"; err="$(pushed_error)"
  local want_rc=1; [[ "$expect" == true ]] && want_rc=0
  if [[ "$got" != "$expect" ]]; then echo "$label: pushed success=$got want $expect (rc=$RC err='$err')"; return 1; fi
  if (( RC != want_rc )); then echo "$label: exit $RC want $want_rc"; return 1; fi
  if [[ "$(push_count)" != 1 ]]; then echo "$label: expected exactly 1 push, got $(push_count)"; return 1; fi
  if [[ "$rx" != "-" ]] && ! grep -Eq -- "$rx" <<<"$err"; then echo "$label: error '$err' !~ /$rx/"; return 1; fi
  return 0
}
expect_case() {  # ok/bad wrapper
  local out
  if out="$(check_case "$@")"; then ok "$1"; else bad "$1" "$out"; fi
}

probe_shape_problem() {  # <script> — exactly one successful, unauthenticated GET
  run_probe "$1" "$BASE/healthy-json"
  if (( RC != 0 )) || [[ "$(pushed_success)" != true || "$(push_count)" != 1 ]]; then
    echo "healthy probe did not succeed exactly once (rc=$RC)"; return 1
  fi
  if [[ "$(wc -l < "$BOT_LOG" | tr -d ' ')" != 1 ]] \
      || [[ "$(cat "$BOT_LOG")" != 'GET /healthy-json auth=None' ]]; then
    echo "expected exactly one bare GET; observed: $(cat "$BOT_LOG")"; return 1
  fi
}
curl_defaults_problem() {  # <script> — defaults must not change the health GET
  printf 'request = "POST"\nheader = "Authorization: SENTINEL-CURLRC-AUTH"\n' > "$TEST_HOME/.curlrc"
  local out rc=0
  out="$(probe_shape_problem "$1")" || rc=$?
  rm -f "$TEST_HOME/.curlrc"
  if (( rc != 0 )); then printf '%s\n' "$out"; return 1; fi
}

# === 1. REFUSALS ================================================================
refusal_problem() {  # <script> <VAR=val ...> — no request may precede refusal
  local script="$1"; shift
  : > "$PUSH_FILE"; : > "$BOT_LOG"
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$TEST_HOME" TMPDIR="$WORK" "$@" bash "$script" >/dev/null 2>&1
  local rc=$?
  if (( rc != 2 )) || [[ "$(push_count)" != 0 || -s "$BOT_LOG" ]]; then
    echo "rc=$rc pushes=$(push_count) requests=$(wc -l < "$BOT_LOG")"; return 1
  fi
}
refuse() {  # <label> <VAR=val ...> — env -i with ONLY those vars set
  local label="$1" out; shift
  if out="$(refusal_problem "$PUSH" "$@")"; then ok "1. $label refuses exit 2 with zero requests"
  else bad "1. $label refusal" "$out"; fi
}
timeout_ceiling_problem() {
  refusal_problem "$1" "$G" "$T" "$U" DISCORD_BOT_TIMEOUT_S=10
}
G="GATUS_URL=http://127.0.0.1:$GATUS_PORT"; T="GATUS_EXTERNAL_TOKEN=$GATUS_TOKEN"; U="DISCORD_BOT_HEALTH_URL=$BASE/healthy-json"
refuse "no env at all"
refuse "missing DISCORD_BOT_HEALTH_URL" "$G" "$T"
refuse "non-http health URL" "$G" "$T" "DISCORD_BOT_HEALTH_URL=ftp://127.0.0.1/x"
refuse "health URL with embedded quote" "$G" "$T" 'DISCORD_BOT_HEALTH_URL=http://127.0.0.1/x"y'
refuse "missing GATUS_URL" "$T" "$U"
refuse "missing GATUS_EXTERNAL_TOKEN" "$G" "$U"
refuse "timeout out of range" "$G" "$T" "$U" DISCORD_BOT_TIMEOUT_S=11
refuse "timeout above three seconds" "$G" "$T" "$U" DISCORD_BOT_TIMEOUT_S=4
refuse "former ten-second ceiling" "$G" "$T" "$U" DISCORD_BOT_TIMEOUT_S=10

# === 2. SEMANTICS ===============================================================
expect_case "2a. healthy Next components report pushes success=true, exit 0" "$PUSH" healthy-json true -
expect_case "2b. legacy ready/gateway JSON is retired: pushes false" "$PUSH" legacy-ready-json false 'carries no components report'
expect_case "2c. legacy plain-text 'ok' is retired: pushes false" "$PUSH" legacy-ok-text false 'not a JSON components report'
expect_case "2d. 200 with gateway down pushes false, names the component" "$PUSH" disconnected-json false 'component not ready: gateway'
expect_case "2e. 200 missing the gateway component pushes false" "$PUSH" missing-component-200 false 'missing a required component'
expect_case "2f. 200 with an empty components report pushes false" "$PUSH" empty-components-200 false 'carries no components report'
expect_case "2g. /readyz 503 pushes false, names the not-ready components" "$PUSH" not-ready-503 false 'answered HTTP 503 reason=not-ready:database,gateway'
expect_case "2h. 503 with an 'ok' body is still DOWN (status is authoritative)" "$PUSH" ok-503 false 'answered HTTP 503'
expect_case "2i. 404 pushes false" "$PUSH" no-such-route false 'answered HTTP 404'
expect_case "2j. malformed JSON pushes false" "$PUSH" malformed-json false 'malformed JSON'
expect_case "2k. HTML 200 (proxy/login page) pushes false" "$PUSH" html-200 false 'not a JSON components report'
expect_case "2l. empty 200 body pushes false" "$PUSH" empty-200 false 'not a JSON components report'
expect_case "2m. JSON array pushes false" "$PUSH" array-json false 'not a JSON components report'
expect_case "2n. JSON with no components report pushes false" "$PUSH" no-fields-json false 'carries no components report'
expect_case "2o. unreachable endpoint pushes false (curl exit 7)" "$PUSH" "http://127.0.0.1:$DEAD_PORT/healthy-json" false 'unreachable or timed out \(curl exit 7\)'
expect_case "2p. slow endpoint beyond the timeout pushes false (curl exit 28)" "$PUSH" slow-fail false 'curl exit 28' DISCORD_BOT_TIMEOUT_S=1
expect_case "2q. response within the default 3s budget pushes true" "$PUSH" slow-ok true -
if grep -qE '^TIMEOUT_S="\$\{DISCORD_BOT_TIMEOUT_S:-3\}"$' "$PUSH"; then ok "2r. default timeout is 3 seconds"
else bad "2r. default timeout" "TIMEOUT_S default is not 3"; fi
expect_case "2s. a 3.25s response exceeds the maximum allowed 3s budget" "$PUSH" slow-over-ceiling false 'curl exit 28' DISCORD_BOT_TIMEOUT_S=3
expect_case "2t. invalid UTF-8 cannot be repaired into a components report" "$PUSH" invalid-utf8-json false 'invalid UTF-8'
expect_case "2u. 200 missing the token_invalid component pushes false" "$PUSH" missing-token-200 false 'missing a required component'

# === 3. UNTRUSTED INPUT ==========================================================
expect_case "3a. 200 body over 64 KiB (no Content-Length) pushes false" "$PUSH" oversized-200 false 'exceeds 65536 bytes'
expect_case "3b. 503 with a 100 KB body pushes false" "$PUSH" oversized-503 false 'exceeds 65536 bytes'
run_probe "$PUSH" "$BASE/hostile-503"
err="$(pushed_error)"
if grep -qE 'answered HTTP 503$' <<<"$err" && ! grep -qE 'SENTINEL-HOSTILE-BODY|IGNORE PREVIOUS' <<<"$err$OUT"; then
  ok "3c. hostile 503 body is never echoed into the push or stdout/stderr"
else bad "3c. hostile body leaked" "err='$err'"; fi
run_probe "$PUSH" "$BASE/oversized-503"
err="$(pushed_error)"
if grep -qE 'SENTINEL-OVERSIZED-BODY|IGNORE PREVIOUS' <<<"$err$OUT"; then bad "3d. oversized body leaked" "$err"
else ok "3d. oversized body is never echoed"; fi
run_probe "$PUSH" "$BASE/html-200"
if grep -q 'SENTINEL-HTML-LOGIN' <<<"$(pushed_error)$OUT"; then bad "3e. HTML body leaked" "$(pushed_error)"
else ok "3e. HTML body is never echoed"; fi
# The helper's own cap is the final backstop for any future error text.
long="$(python3 -c 'print("X" * 5000)')"
: > "$PUSH_FILE"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$TEST_HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$GATUS_PORT" GATUS_EXTERNAL_TOKEN="$GATUS_TOKEN" \
  bash -c ". '$SRC/_gatus_push.sh'; gatus_push platform_discord-bot false '$long'" >/dev/null 2>&1
err="$(pushed_error)"
if (( ${#err} <= 500 + 20 )) && (( ${#err} > 400 )) && [[ "$err" == *"(truncated)" ]]; then
  ok "3f. a 5000-char error is truncated to MAX_PUSH_ERROR_CHARS by gatus_push (${#err} chars)"
else bad "3f. error truncation" "len=${#err}"; fi
# Every error the script can emit stays short and fixed-phrase.
maxlen=0
for r in disconnected-json missing-component-200 missing-token-200 empty-components-200 legacy-ready-json legacy-ok-text not-ready-503 ok-503 hostile-503 malformed-json html-200 no-fields-json array-json oversized-200 oversized-503; do
  run_probe "$PUSH" "$BASE/$r"; e="$(pushed_error)"; (( ${#e} > maxlen )) && maxlen=${#e}
done
if (( maxlen > 0 && maxlen <= 200 )); then ok "3g. longest emitted error is $maxlen chars (fixed phrases only)"
else bad "3g. error length" "max=$maxlen"; fi

# === 4. NON-DISCLOSURE ============================================================
disclosure_problem() {  # <script> → prints the leak, or nothing
  : > "$WORK/argv.log"
  run_probe "$1" "$BASE/healthy-json?s=$URL_SENTINEL"
  grep -qF "$GATUS_TOKEN" "$WORK/argv.log" && echo "Gatus token reached curl argv"
  grep -qF "$URL_SENTINEL" "$WORK/argv.log" && echo "health URL reached curl argv"
  grep -qF "$URL_SENTINEL" <<<"$OUT" && echo "health URL printed to stdout/stderr"
  grep -qF "$URL_SENTINEL" "$PUSH_FILE" && echo "health URL pushed to Gatus"
  # a failing probe prints its verdict: the URL must not ride along then either
  run_probe "$1" "$BASE/not-ready-503?s=$URL_SENTINEL"
  grep -qF "$URL_SENTINEL" <<<"$OUT$(cat "$PUSH_FILE")" && echo "health URL leaked on the failure path"
}
leak="$(disclosure_problem "$PUSH")"
if [[ -z "$leak" ]]; then ok "4a. token and health URL never reach argv, stdout, stderr or the push"
else bad "4a. disclosure" "$leak"; fi
if shape="$(probe_shape_problem "$PUSH")"; then
  ok "4b. the probe is exactly one bare GET (no auth header, no other request)"
else bad "4b. probe shape" "$shape"; fi
if shape="$(curl_defaults_problem "$PUSH")"; then
  ok "4c. hostile .curlrc cannot change the health GET or add authentication"
else bad "4c. default curl configuration" "$shape"; fi

# === 5. WIRING ====================================================================
# gatus-config.yaml must still declare the external endpoint this script pushes
# to — a rename there would leave the script feeding a dead key.
wire="$(python3 - "$CFG" "$PUSH" <<'PY'
import re, sys
cfg = open(sys.argv[1]).read()
push = open(sys.argv[2]).read()
m = re.search(r'^external-endpoints:\s*$(.*)', cfg, re.M | re.S)
errs = []
if not m:
    errs.append("no external-endpoints section")
else:
    block = re.search(r'-\s*name:\s*discord-bot\s*\n(?:(?!\n\s*-\s*name:).)*', m.group(1), re.S)
    if not block: errs.append("external endpoint discord-bot not declared")
    else:
        b = block.group(0)
        if not re.search(r'group:\s*platform', b): errs.append("discord-bot is not in group platform")
        if not re.search(r'heartbeat:\s*\n\s*interval:\s*10m', b): errs.append("discord-bot heartbeat is not 10m")
key = re.search(r'^ENDPOINT_KEY="([^"]+)"', push, re.M)
if not key or key.group(1) != "platform_discord-bot": errs.append("script ENDPOINT_KEY is not platform_discord-bot")
print("ERRORS:" + ";".join(errs) if errs else "WIRED")
PY
)"
if [[ "$wire" == WIRED ]]; then ok "5a. gatus-config declares external discord-bot (platform, 10m); script key matches"
else bad "5a. gatus-config wiring" "$wire"; fi
reg_bad=""
for name in push-discord-bot.sh test_case_60330d6f_discord_bot.sh platform_discord-bot "fixtures/discord-bot"; do
  grep -qF "$name" "$REG" || reg_bad="$reg_bad [MODULES.md missing $name]"
done
grep -qE '^[*0-9/ ]+ root .* /opt/status-page/current/ops/case_658bf6b9/push-discord-bot\.sh$' "$OPS" \
  || reg_bad="$reg_bad [OPERATOR.md cron block missing push-discord-bot.sh line]"
grep -qE '^DISCORD_BOT_HEALTH_URL=<paste' "$OPS" \
  || reg_bad="$reg_bad [OPERATOR.md env block missing DISCORD_BOT_HEALTH_URL placeholder]"
[[ -z "$reg_bad" ]] && ok "5b. MODULES.md lists key/script/suite/fixtures; OPERATOR.md has the cron line and env placeholder" \
  || bad "5b. registry and operator wiring" "$reg_bad"
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|Bearer [A-Za-z0-9._~-]{8,}|^DISCORD_BOT_HEALTH_URL=https?://' "$PUSH" "$FIX" 2>/dev/null || true)"
[[ -z "$sec_hits" ]] && ok "5c. no webhook, bearer or committed health host in script or fixtures" \
  || bad "5c. secret-shaped string" "$sec_hits"

# Execute the actual documented /bin/sh command, substituting only its paths.
# Ordinary assignments must reach the child; a failed source must not run it.
CRON="$(python3 - "$OPS" <<'PY'
import re, sys
lines = [line for line in open(sys.argv[1])
         if re.match(r'^[*0-9/ ]+ root ', line) and line.rstrip().endswith('/push-discord-bot.sh')]
if len(lines) != 1:
    sys.exit(1)
cmd = lines[0].split(' root ', 1)[1].strip()
print(cmd.replace('/root/.config/paperclip/case_60330d6f.env', '"$1"')
         .replace('/opt/status-page/current/ops/case_658bf6b9/push-discord-bot.sh', '"$2"'))
PY
)" || { bad "5d. cron command extraction" "expected exactly one documented cron line"; CRON=""; }
CRON_ENV="$WORK/cron.env"; BAD_CRON_ENV="$WORK/failing-cron.env"
printf '%s\n' "$G" "$T" "$U" > "$CRON_ENV"
cp "$CRON_ENV" "$BAD_CRON_ENV"; printf 'false\n' >> "$BAD_CRON_ENV"
run_cron() {  # <command> <env-file> — all paths point at loopback fixtures
  : > "$PUSH_FILE"; : > "$BOT_LOG"
  OUT="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$TEST_HOME" TMPDIR="$WORK" \
    /bin/sh -c "$1" cron "$2" "$PUSH" 2>&1)"
  RC=$?
}
cron_success_problem() {
  [[ -n "$1" ]] || { echo "no cron command"; return 1; }
  run_cron "$1" "$CRON_ENV"
  if (( RC != 0 )) || [[ "$(pushed_success)" != true || "$(push_count)" != 1 ]] \
      || [[ "$(cat "$BOT_LOG")" != 'GET /healthy-json auth=None' ]]; then
    echo "cron env not inherited: rc=$RC pushes=$(push_count)"; return 1
  fi
}
cron_source_failure_problem() {
  [[ -n "$1" ]] || { echo "no cron command"; return 1; }
  run_cron "$1" "$BAD_CRON_ENV"
  if (( RC == 0 )) || [[ "$(push_count)" != 0 || -s "$BOT_LOG" ]]; then
    echo "failed source still ran probe: rc=$RC pushes=$(push_count)"; return 1
  fi
}
if result="$(cron_success_problem "$CRON")"; then ok "5d. documented cron exports ordinary assignments into the pusher"
else bad "5d. cron export boundary" "$result"; fi
if result="$(cron_source_failure_problem "$CRON")"; then ok "5e. documented cron stops without requests when sourcing fails"
else bad "5e. cron source failure" "$result"; fi

# === 6. POSITIVE CONTROLS (mutants) ===============================================
# mutate <name> <old> <new> [<old> <new> ...]: copies the script + helper into
# $WORK/mut/<name>/ and replaces each <old> with <new> EXACTLY ONCE. A mutant
# whose anchor no longer matches is itself a failure — a silent no-op would be
# a vacuous control.
mutate() {
  local name="$1"; shift
  local dir="$WORK/mut/$name"
  mkdir -p "$dir"; cp "$SRC/_gatus_push.sh" "$dir/"
  python3 - "$PUSH" "$dir/push-discord-bot.sh" "$@" <<'PY' || return 1
import sys
src, dst, *pairs = sys.argv[1:]
s = open(src).read()
for old, new in zip(pairs[0::2], pairs[1::2]):
    if s.count(old) != 1:
        sys.stderr.write("anchor count %d != 1\n" % s.count(old)); sys.exit(1)
    s = s.replace(old, new)
open(dst, "w").write(s)
PY
  chmod +x "$dir/push-discord-bot.sh"
}
# control <name> <old> <new> <kill-case-spec...>
#   <kill-case-spec> = "route|expect|regex" — the mutant must FAIL this case
#   while still PASSING the healthy case (so it is red for the right reason).
control() {
  local name="$1" old="$2" new="$3" route="$4" expect="$5" rx="$6"; shift 6
  if ! mutate "$name" "$old" "$new" 2>"$WORK/mut.err"; then
    bad "6. mutant $name" "anchor drifted: $(cat "$WORK/mut.err")"; return
  fi
  local m="$WORK/mut/$name/push-discord-bot.sh"
  if [[ -n "$(check_case "healthy($name)" "$m" healthy-json true -)" ]]; then
    bad "6. mutant $name" "mutant no longer passes the healthy case — red for the wrong reason"; return
  fi
  if [[ -n "$(check_case "kill($name)" "$m" "$route" "$expect" "$rx" "$@")" ]]; then
    ok "6. mutant $name turns its targeted case red"
  else bad "6. mutant $name" "survived: targeted case still passes"; fi
}
control no-status-check 'if http != "200":' 'if False:' \
  ok-503 false 'answered HTTP 503'
control no-components-check 'if not REQUIRED_COMPONENTS.issubset(names):' 'if False:' \
  missing-component-200 false 'missing a required component'
control degraded-200-green "verdict(False, f\"discord-bot: component not ready: {','.join(not_ready)}\")" 'verdict(True, "")' \
  disconnected-json false 'component not ready'
control malformed-json-green 'verdict(False, "discord-bot: 200 body is malformed JSON (fail-closed)")' 'verdict(True, "")' \
  malformed-json false 'malformed JSON'
control html-200-green "verdict(False, \"discord-bot: 200 body is not a JSON components report (fail-closed)\")" 'verdict(True, "")' \
  html-200 false 'not a JSON components report'
control inverted-down-push 'gatus_push "$ENDPOINT_KEY" false "$message" || true' 'gatus_push "$ENDPOINT_KEY" true "$message" || true' \
  not-ready-503 false 'answered HTTP 503'
control no-timeout "  printf 'max-time = \"%s\"\\n' \"\$TIMEOUT_S\"" '  :' \
  slow-fail false 'curl exit 28' DISCORD_BOT_TIMEOUT_S=1
control body-echo 'suffix = f" reason={word}" if word in REASONS else ""' 'suffix = f" reason={word}"' \
  hostile-503 false 'answered HTTP 503$'
# Disclosure mutants: the 4a scan must catch each leak path.
disclosure_control() {  # <name> <old> <new> [<old> <new> ...]
  local name="$1"; shift
  if ! mutate "$name" "$@" 2>"$WORK/mut.err"; then
    bad "6. mutant $name" "anchor drifted: $(cat "$WORK/mut.err")"; return
  fi
  local m="$WORK/mut/$name/push-discord-bot.sh"
  if [[ -n "$(check_case "healthy($name)" "$m" healthy-json true -)" ]]; then
    bad "6. mutant $name" "mutant no longer passes the healthy case — red for the wrong reason"
  elif [[ -n "$(disclosure_problem "$m")" ]]; then ok "6. mutant $name turns the disclosure scan red"
  else bad "6. mutant $name" "survived: disclosure scan still clean"; fi
}
# URL moved from the 0600 curl config into curl's argv.
disclosure_control url-in-argv \
  "printf 'url = \"%s\"\\n' \"\$DISCORD_BOT_HEALTH_URL\"" ':' \
  'http="$(curl -q --config "$cfg" 2>/dev/null)"' 'http="$(curl -q --config "$cfg" "$DISCORD_BOT_HEALTH_URL" 2>/dev/null)"'
# URL echoed in the success line.
disclosure_control url-printed \
  'echo "push-discord-bot: healthy"' 'echo "push-discord-bot: healthy ($DISCORD_BOT_HEALTH_URL)"'

# Shape/config controls keep the normal healthy pusher working, then exercise
# the boundary the old suite failed to observe.
behavior_control() {  # <name> <old> <new> <oracle-function>
  local name="$1" old="$2" new="$3" oracle="$4"
  if ! mutate "$name" "$old" "$new" 2>"$WORK/mut.err"; then
    bad "6. mutant $name" "anchor drifted: $(cat "$WORK/mut.err")"; return
  fi
  local m="$WORK/mut/$name/push-discord-bot.sh" out
  if ! out="$(check_case "healthy($name)" "$m" healthy-json true -)"; then
    bad "6. mutant $name" "healthy control failed: $out"
  elif out="$("$oracle" "$m")"; then bad "6. mutant $name" "survived boundary oracle"
  else ok "6. mutant $name turns its targeted boundary red"; fi
}
behavior_control duplicate-health-get \
  'http="$(curl -q --config "$cfg" 2>/dev/null)"' \
  'curl -q --config "$cfg" >/dev/null 2>&1; http="$(curl -q --config "$cfg" 2>/dev/null)"' \
  probe_shape_problem
behavior_control default-curl-config \
  'http="$(curl -q --config "$cfg" 2>/dev/null)"' 'http="$(curl --config "$cfg" 2>/dev/null)"' \
  curl_defaults_problem
behavior_control ten-second-config '[[ "$TIMEOUT_S" =~ ^[1-3]$ ]]' \
  '[[ "$TIMEOUT_S" =~ ^([1-9]|10)$ ]]' timeout_ceiling_problem
control repaired-utf8-green 'raw.decode("utf-8", "strict")' 'raw.decode("utf-8", "replace")' \
  invalid-utf8-json false 'invalid UTF-8'
control probe-budget-widened "  printf 'max-time = \"%s\"\\n' \"\$TIMEOUT_S\"" \
  "  printf 'max-time = \"10\"\\n'" \
  slow-over-ceiling false 'curl exit 28' DISCORD_BOT_TIMEOUT_S=3
cron_control() {  # <name> <old> <new> <oracle-function>
  local name="$1" old="$2" new="$3" oracle="$4" command out
  if ! command="$(python3 - "$CRON" "$old" "$new" <<'PY'
import sys
cmd, old, new = sys.argv[1:]
if cmd.count(old) != 1:
    sys.exit(1)
print(cmd.replace(old, new))
PY
)"; then bad "6. mutant $name" "cron anchor drifted"; return; fi
  if ! out="$(check_case "healthy($name)" "$PUSH" healthy-json true -)"; then
    bad "6. mutant $name" "healthy pusher control failed: $out"
  elif out="$("$oracle" "$command")"; then bad "6. mutant $name" "survived cron oracle"
  else ok "6. mutant $name" "turns its cron boundary red"; fi
}
cron_control no-cron-export 'set -a;' '' cron_success_problem
cron_control ignore-source-failure '&&' ';' cron_source_failure_problem

echo "---"
printf 'pass=%d fail=%d\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
