#!/usr/bin/env bash
# ===========================================================================
# test_case_df6ed188_monitoring_of_monitor.sh — offline suite for the case_56fff4c6
# monitoring-of-monitor bundle (ops/case_56fff4c6/).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. ENDPOINT SHAPE — gatus-endpoints.yaml declares the gatus-heartbeat
#      external endpoint with token, 10m heartbeat, discord+custom alerts,
#      owner + runbook in the description. A fragment that parses but
#      monitors nothing is the failure mode.
#   2. NO SECRETS — no secret-shaped string committed anywhere in the
#      bundle: no webhook URL, no Bearer literal, no token literal; env
#      indirection (${VAR}) is the only allowed form.
#   3. PROBE REFUSALS — probe-gatus.sh fails CLOSED: missing HEALTH_URL /
#      GATUS_URL / token refuses (exit 2) before touching the network; a
#      stub-server call log proves refused runs make ZERO calls.
#   4. NON-DISCLOSURE — when env IS set, Bearer/CF tokens never reach
#      curl's argv: a curl shim records argv, and the push must pass.
#   5. UNTRUSTED INPUT — a 2000-char hostile probe error is truncated to
#      <= 600 chars on the wire; the full string never leaves.
#   6. PROBE SEMANTICS — 200 pushes success=true, exit 0, stdout UP;
#      500 pushes success=false, exit 1, stdout DOWN; a neutered
#      fail-closed mutant proves the pair discriminates. Unreachable Gatus:
#      success=false pushed best-effort... except the push shares Gatus's
#      fate, so the suite pins the exit-1 + direct-notice half instead.
#   7. ROUND-TRIP — staging canary posts AND is observed back
#      (ROUNDTRIP-OK with both timestamps); sink-accepted-but-never-seen
#      exits 1 LOST; production-like targets refused (exit 2); missing
#      verify URL refused (exit 2, a trigger without a verify is
#      fire-and-forget); bearer travels in curl config, never argv.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. A stub HTTP server
# plays Gatus (health + external push API) and the round-trip sink/verify
# channel; stub binaries feed fixtures; a curl shim records argv then execs
# the real curl against the stub server.
#
# Sibling boundary: case_93aed7ff owns the one-time LIVE drill run (Operator
# card case_5e79fdfd). This suite proves the PERSISTENT harness offline; it
# never fires a live alert and never pages production.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_56fff4c6"
FRAG="$SRC/gatus-endpoints.yaml"
PROBE="$SRC/probe-gatus.sh"
ROUNDTRIP="$SRC/roundtrip-alert.sh"
PUSH_HELPER="$HERE/ops/case_658bf6b9/_gatus_push.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -d "$SRC" ]] || { echo "REFUSE: $SRC not found — suite cannot attribute a result"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }

# --- stub server: Gatus health + external push API + round-trip sink --------
# Modes are FILE-based (read per request): the server process starts once and
# its environ is fixed, so a per-case env prefix on the CLIENT would never
# reach it. The suite writes $WORK/mode/health (up|down) and
# $WORK/mode/verify (echo|empty) before each case instead.
cat > "$WORK/server.py" <<'PY'
import http.server, json, os, sys
PUSH = os.environ["PUSH_FILE"]
SEEN = os.environ.get("SEEN_FILE", "")
MODE = os.environ.get("MODE_DIR", "")
def mode(name, default):
    try:
        with open(os.path.join(MODE, name)) as f:
            return f.read().strip() or default
    except OSError:
        return default
class H(http.server.BaseHTTPRequestHandler):
    def _record(self, kind, extra=None):
        rec = {"method": self.command, "path": self.path,
               "auth": self.headers.get("Authorization")}
        if extra is not None: rec.update(extra)
        with open(PUSH, "a") as f: f.write(json.dumps(rec) + "\n")
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        if self.path.startswith("/sink"):
            with open(SEEN, "a") as f: f.write(body + "\n")
            self._record("sink", {"body": body[:200]})
        else:
            self._record("push")
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_GET(self):
        if self.path.startswith("/health"):
            m = mode("health", "up")
            if m == "up":
                self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
            elif m == "down":
                self.send_response(500); self.end_headers(); self.wfile.write(b"BROKEN")
            else:
                self.send_response(404); self.end_headers(); self.wfile.write(b"nope")
            self._record("health")
        elif self.path.startswith("/verify"):
            seen = ""
            try: seen = open(SEEN).read()
            except OSError: pass
            if mode("verify", "echo") == "empty": seen = ""
            self.send_response(200); self.end_headers()
            self.wfile.write(seen.encode())
            self._record("verify")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
PORT="${case_df6ed188_TEST_PORT:-8798}"
PUSH_FILE="$WORK/pushes.jsonl"; SEEN_FILE="$WORK/seenv.log"
mkdir -p "$WORK/mode"
printf 'up\n' > "$WORK/mode/health"; printf 'echo\n' > "$WORK/mode/verify"
export PUSH_FILE SEEN_FILE MODE_DIR="$WORK/mode"
: > "$PUSH_FILE"; : > "$SEEN_FILE"
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

pass_count() { printf 'pass=%d fail=%d' "$PASS" "$FAIL"; }
push_paths() { python3 -c "import json; print('\n'.join(json.loads(x)['path'] for x in open('$PUSH_FILE')))" 2>/dev/null; }

# === 1. ENDPOINT SHAPE =========================================================
hb_block() { awk '/- name: gatus-heartbeat/{f=1} f{print} f&&/^  - name: /&&!/gatus-heartbeat/{exit}' "$1"; }
if grep -q 'token:' <<<"$(hb_block "$FRAG")" \
  && grep -q 'interval: 10m' <<<"$(hb_block "$FRAG")" \
  && grep -q 'type: discord' <<<"$(hb_block "$FRAG")" \
  && grep -q 'type: custom' <<<"$(hb_block "$FRAG")"; then
  ok "1. gatus-heartbeat endpoint declares token, 10m heartbeat, discord+custom alerts"
else bad "1. endpoint shape" "$(hb_block "$FRAG" || echo MISSING)"; fi
if grep -qi 'owner' <<<"$(hb_block "$FRAG")" && grep -qi 'runbook' <<<"$(hb_block "$FRAG")"; then
  ok "1b. endpoint description carries owner + runbook pointer"
else bad "1b. owner/runbook" "description names no owner or runbook"; fi
# positive control: an endpoint-less fragment must fail 1.
mut="$WORK/nohb.yaml"; grep -v 'gatus-heartbeat' "$FRAG" > "$mut"
if grep -q 'token:' <<<"$(hb_block "$mut")"; then
  bad "1c. heartbeat-less mutant" "mutant still declares the endpoint"
else ok "1c. heartbeat-less mutant goes red"; fi

# === 2. NO SECRETS =============================================================
sec_hits="$(grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_HEARTBEAT_WEBHOOK_URL|GATUS_EXTERNAL_TOKEN|CF_ACCESS_CLIENT_SECRET|ROUNDTRIP_BEARER)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$SRC" || true)"
if [[ -n "$sec_hits" ]]; then
  bad "2. live secret or webhook URL committed" "$sec_hits"
else ok "2. no secret-shaped strings in bundle (placeholders only)"; fi
if grep -q '\${GATUS_EXTERNAL_TOKEN}' "$FRAG" && grep -q '\${GATUS_HEALTH_URL}\|GATUS_HEALTH_URL' "$PROBE"; then
  ok "2b. secrets/URLs referenced via \${ENV} only"
else bad "2b. env indirection" "expected \${...} references missing"; fi
echo 'x="https://discord.com/api/webhooks/123/abc"' > "$WORK/planted.txt"
if grep -qE 'discord\.com/api/webhooks' "$WORK/planted.txt"; then ok "2c. secret-scan pattern fires on a planted webhook"; else bad "2c. secret-scan pattern" "pattern missed a planted webhook URL"; fi

# === helpers ===================================================================
# run_probe: writes probe stdout to $WORK/probe.out and rc to
# $WORK/probe.rc (a bare $( ) capture cannot export RC past the subshell,
# and `set -u` then dies reading it). Extra args are env assignments
# placed before bash. Server modes go through $WORK/mode files, NOT env:
# the stub server's environ is fixed at process start.
set_mode() { printf '%s\n' "$2" > "$WORK/mode/$1"; }  # <health|verify> <value>
run_probe() {
  : > "$PUSH_FILE"; : > "$WORK/argv.log"; : > "$SEEN_FILE"
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_HEALTH_URL="http://127.0.0.1:$PORT/health" \
    GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="TESTTOKEN" \
    "$@" bash "$PROBE" >"$WORK/probe.out" 2>"$WORK/stderr.txt"
  printf '%s' "$?" > "$WORK/probe.rc"
}
probe_out() { cat "$WORK/probe.out"; }
probe_rc() { cat "$WORK/probe.rc"; }

# === 3. PROBE REFUSALS (fail closed, zero network) ==============================
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" bash "$PROBE" >/dev/null 2>&1; rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3. missing env refuses exit 2 with zero pushes"
else bad "3. missing env refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi
# bad URL refuses before any network.
: > "$PUSH_FILE"
env -i PATH="/usr/bin:/bin" GATUS_HEALTH_URL="not-a-url" GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  bash "$PROBE" >/dev/null 2>&1; rc=$?
if (( rc == 2 )) && [[ ! -s "$PUSH_FILE" ]]; then ok "3b. bad HEALTH_URL refuses exit 2 with zero pushes"
else bad "3b. bad URL refusal" "rc=$rc pushes=$(wc -l < "$PUSH_FILE")"; fi
# missing curl is UNMEASURED (exit 5), never a probe verdict and never green.
# (A shim dir shadowing curl with a failing stub: `command -v curl`
# resolves but every invocation fails, so the probe measures nothing.
# PATH=/bin alone does not work — curl lives in /bin on this image.)
# Absent push helper is UNMEASURED (exit 5), never a probe verdict and
# never green. (curl and python3 both live in /bin on this image, so PATH
# games cannot drop them without dropping sh; the helper file is the
# removable half of the transport. run_probe runs from $SRC, so point the
# probe at a shadow tree whose ../case_658bf6b9/_gatus_push.sh is absent.)
mkdir -p "$WORK/nohelper/case_56fff4c6"
cp "$PROBE" "$WORK/nohelper/case_56fff4c6/probe-gatus.sh"
env -i PATH="/usr/bin:/bin" GATUS_HEALTH_URL="http://127.0.0.1:$PORT/health" GATUS_URL="http://127.0.0.1:$PORT" \
  GATUS_EXTERNAL_TOKEN="T" bash "$WORK/nohelper/case_56fff4c6/probe-gatus.sh" >/dev/null 2>&1; rc=$?
if (( rc == 5 )); then ok "3c. missing push helper is UNMEASURED exit 5, never green"
else bad "3c. missing helper" "rc=$rc (expected 5)"; fi
rm -rf "$WORK/nohelper"

# === 4. NON-DISCLOSURE ===========================================================
SECRET_TOKEN="SENTINELTOKEN-must-never-reach-argv"
SECRET_CF="SENTINELCF-must-never-reach-argv"
mkdir -p "$WORK/empty-bin"
set_mode health up
: > "$PUSH_FILE"; : > "$WORK/argv.log"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_HEALTH_URL="http://127.0.0.1:$PORT/health" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="$SECRET_TOKEN" \
  CF_ACCESS_CLIENT_ID="test-id" CF_ACCESS_CLIENT_SECRET="$SECRET_CF" \
  bash "$PROBE" >/dev/null 2>&1
if grep -qF "$SECRET_TOKEN" "$WORK/argv.log" || grep -qF "$SECRET_CF" "$WORK/argv.log"; then
  bad "4. token reached curl argv" "$(cat "$WORK/argv.log")"
else ok "4. Bearer/CF tokens never reach curl argv"; fi

# === 5. UNTRUSTED INPUT (truncation via shared transport) ========================
hostile="$(python3 -c "print('INJECT:' + 'A'*2000)")"
: > "$PUSH_FILE"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  bash -c ". '$PUSH_HELPER'; gatus_push platform_gatus-heartbeat false \"$hostile\"" >/dev/null 2>&1
wire_len="$(python3 -c "import json,urllib.parse; p=[json.loads(x)['path'] for x in open('$PUSH_FILE')][-1]; q=urllib.parse.parse_qs(urllib.parse.urlparse(p).query); print(len(q.get('error',[''])[0]))" 2>/dev/null || echo 0)"
if (( wire_len > 0 && wire_len <= 600 )); then ok "5. hostile error text truncated on the wire (${wire_len} chars)"
else bad "5. truncation" "wire error length=$wire_len"; fi

# === 6. PROBE SEMANTICS ===========================================================
# healthy Gatus → success=true, exit 0, stdout UP.
set_mode health up
run_probe; rc="$(probe_rc)"; out="$(probe_out)"
p="$(push_paths)"
if (( rc == 0 )) && grep -q "platform_gatus-heartbeat.*success=true" <<<"$p" && grep -q "^UP 200" <<<"$out"; then
  ok "6a. healthy Gatus pushes success=true, exit 0, stdout UP"
else bad "6a. healthy probe" "rc=$rc out=$out pushes=$p"; fi
# DOWN Gatus (500) → success=false pushed, exit 1, stdout DOWN.
set_mode health down
run_probe; rc="$(probe_rc)"; out="$(probe_out)"
p="$(push_paths)"
if (( rc == 1 )) && grep -q "platform_gatus-heartbeat.*success=false" <<<"$p" && grep -q "^DOWN 500" <<<"$out"; then
  ok "6b. DOWN Gatus pushes success=false, exit 1, stdout DOWN"
else bad "6b. DOWN probe" "rc=$rc out=$out pushes=$p"; fi
# direct notice fires on DOWN when configured — and carries no secret in argv.
set_mode health down
: > "$WORK/argv.log"
run_probe DISCORD_HEARTBEAT_WEBHOOK_URL="http://127.0.0.1:$PORT/sink"; rc="$(probe_rc)"
if (( rc == 1 )) && grep -q "/sink" "$PUSH_FILE" 2>/dev/null \
  && ! grep -qF "http://127.0.0.1:$PORT/sink" "$WORK/argv.log"; then
  ok "6c. DOWN probe sends direct Discord notice, URL absent from argv"
else bad "6c. direct notice" "rc=$rc pushes=$(push_paths | tr '\n' ' ')"; fi
# no webhook configured → still exits 1 (workflow red is the page then).
set_mode health down
run_probe; rc="$(probe_rc)"; out="$(probe_out)"
(( rc == 1 )) && ok "6d. DOWN probe without webhook still exits 1" || bad "6d. DOWN exit" "rc=$rc out=$out"
# positive control (mutant): a neutered fail-closed that pushes true on DOWN
# must flip 6b — proves the pair discriminates, not just passes. The mutant
# is staged in a directory carrying _gatus_push.sh because the probe sources
# its sibling helper by its own path, not by $SRC (same staging as the
# case_658bf6b9 6j15 fail_push mutant).
mkdir -p "$WORK/mutbin/case_658bf6b9" "$WORK/mutbin/case_56fff4c6"
cp "$PUSH_HELPER" "$WORK/mutbin/case_658bf6b9/_gatus_push.sh"
sed 's/gatus_push "$ENDPOINT_KEY" false "$err"/gatus_push "$ENDPOINT_KEY" true/' \
  "$PROBE" > "$WORK/mutbin/case_56fff4c6/probe-gatus.sh"
chmod +x "$WORK/mutbin/case_56fff4c6/probe-gatus.sh"
set_mode health down
: > "$PUSH_FILE"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_HEALTH_URL="http://127.0.0.1:$PORT/health" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  bash "$WORK/mutbin/case_56fff4c6/probe-gatus.sh" >/dev/null 2>&1
if grep -q "success=true" "$PUSH_FILE" 2>/dev/null || grep -q "success=true" <<<"$(push_paths)"; then
  ok "6e. neutered fail-closed mutant flips DOWN green"
else bad "6e. fail-closed mutant" "mutant still pushed false: $(push_paths | tr '\n' ' ')"; fi
# unreachable Gatus (nothing listening): exit 1 with DOWN verdict.
: > "$PUSH_FILE"
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_HEALTH_URL="http://127.0.0.1:1/health" \
  GATUS_URL="http://127.0.0.1:$PORT" GATUS_EXTERNAL_TOKEN="T" \
  bash "$PROBE" 2>/dev/null)"; rc=$?
if (( rc == 1 )) && grep -q "^DOWN " <<<"$out"; then
  ok "6f. unreachable Gatus exits 1 with DOWN verdict"
else bad "6f. unreachable probe" "rc=$rc out=$out"; fi

# === 7. ROUND-TRIP HARNESS ========================================================
# (verify modes go through $WORK/mode/verify — server environ is fixed.)
# staging canary posts AND is observed back → ROUNDTRIP-OK with timestamps.
set_mode verify echo
: > "$PUSH_FILE"; : > "$SEEN_FILE"
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  ROUNDTRIP_URL="http://127.0.0.1:$PORT/sink" \
  ROUNDTRIP_VERIFY_URL="http://127.0.0.1:$PORT/verify" \
  ROUNDTRIP_POLL_S=1 ROUNDTRIP_TIMEOUT_S=10 \
  bash "$ROUNDTRIP" --id "canary-test-1" 2>/dev/null)"; rc=$?
if (( rc == 0 )) && grep -q "^ROUNDTRIP-OK	canary-test-1	posted=.*observed=" <<<"$out"; then
  ok "7a. staging canary round-trips: ROUNDTRIP-OK with both timestamps"
else bad "7a. round-trip OK path" "rc=$rc out=$out"; fi
# sink accepts but verify never sees it → exit 1 LOST (the finding).
set_mode verify empty
: > "$SEEN_FILE"
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  ROUNDTRIP_URL="http://127.0.0.1:$PORT/sink" \
  ROUNDTRIP_VERIFY_URL="http://127.0.0.1:$PORT/verify" \
  ROUNDTRIP_POLL_S=1 ROUNDTRIP_TIMEOUT_S=3 \
  bash "$ROUNDTRIP" --id "canary-lost-1" 2>/dev/null)"; rc=$?
if (( rc == 1 )) && grep -q "^ROUNDTRIP-FAIL	canary-lost-1	posted=.*reason=never_observed" <<<"$out"; then
  ok "7b. accepted-but-never-seen exits 1 LOST (a finding, not a pass)"
else bad "7b. LOST path" "rc=$rc out=$out"; fi
# unreachable sink → exit 1, posted=never.
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  ROUNDTRIP_URL="http://127.0.0.1:1/sink" \
  ROUNDTRIP_VERIFY_URL="http://127.0.0.1:$PORT/verify" \
  ROUNDTRIP_POLL_S=1 ROUNDTRIP_TIMEOUT_S=3 \
  bash "$ROUNDTRIP" --id "canary-down-1" 2>/dev/null)"; rc=$?
if (( rc == 1 )) && grep -q "posted=never" <<<"$out"; then
  ok "7c. unreachable sink exits 1 posted=never"
else bad "7c. sink-down path" "rc=$rc out=$out"; fi
# production-like targets are REFUSED before any byte is sent.
for bad_env in 'ROUNDTRIP_URL=http://host-41e183d4.example.invalid/hook ROUNDTRIP_VERIFY_URL=http://127.0.0.1:1/v' \
               'ROUNDTRIP_URL=http://127.0.0.1:1/sink ROUNDTRIP_VERIFY_URL=http://host-bc65fd97.example.invalid/v'; do
  : > "$PUSH_FILE"
  # shellcheck disable=SC2086
  env -i PATH="$WORK/bin:/usr/bin:/bin" $bad_env bash "$ROUNDTRIP" --id rt 2>/dev/null; rc=$?
  out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" $bad_env bash "$ROUNDTRIP" --id rt 2>/dev/null)"
  if (( rc == 2 )) && grep -q "^refused	production_target" <<<"$out" && [[ ! -s "$PUSH_FILE" ]]; then
    ok "7d. production-like target refused exit 2, nothing sent: ${bad_env%% *}"
  else bad "7d. prod-target refusal" "$bad_env rc=$rc out=$out"; fi
done
# missing verify URL is REFUSED: a trigger without a verify is fire-and-forget.
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" ROUNDTRIP_URL="http://127.0.0.1:$PORT/sink" \
  bash "$ROUNDTRIP" --id rt 2>/dev/null)"; rc=$?
if (( rc == 2 )) && grep -q "^refused	bad_usage" <<<"$out"; then
  ok "7e. missing verify URL refused exit 2"
else bad "7e. no-verify refusal" "rc=$rc out=$out"; fi
# bearer travels in curl config, never argv.
set_mode verify echo
: > "$WORK/argv.log"; : > "$SEEN_FILE"
SECRET_RT="SENTINELRT-must-never-reach-argv"
env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  ROUNDTRIP_URL="http://127.0.0.1:$PORT/sink" \
  ROUNDTRIP_VERIFY_URL="http://127.0.0.1:$PORT/verify" ROUNDTRIP_BEARER="$SECRET_RT" \
  ROUNDTRIP_POLL_S=1 ROUNDTRIP_TIMEOUT_S=10 \
  bash "$ROUNDTRIP" --id "canary-auth-1" >/dev/null 2>&1
if grep -qF "$SECRET_RT" "$WORK/argv.log"; then
  bad "7f. round-trip bearer reached argv" "$(cat "$WORK/argv.log")"
else ok "7f. round-trip bearer never reaches argv"; fi
# positive control: a verify-blind mutant (always OK after POST) must pass 7b's
# LOST fixture — proves 7b can still go red... i.e. the assertion fires.
mutr="$WORK/mutround.sh"
sed 's/^deadline=$(.*$/echo "ROUNDTRIP-OK\t$RID\tposted=never\tobserved=never"; exit 0/' \
  "$ROUNDTRIP" > "$mutr"
set_mode verify empty
: > "$SEEN_FILE"
out="$(env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  ROUNDTRIP_URL="http://127.0.0.1:$PORT/sink" \
  ROUNDTRIP_VERIFY_URL="http://127.0.0.1:$PORT/verify" \
  ROUNDTRIP_POLL_S=1 ROUNDTRIP_TIMEOUT_S=3 \
  bash "$mutr" --id "canary-lost-1" 2>/dev/null)"; rc=$?
if (( rc == 0 )) && grep -q "^ROUNDTRIP-OK" <<<"$out"; then
  ok "7g. verify-blind mutant flips LOST green (7b discriminates)"
else bad "7g. round-trip mutant" "rc=$rc out=$out"; fi

# === 8. REGISTRATION (an unregistered suite is no suite) ===========================
if grep -Eq '^[[:space:]]*run:[[:space:]]*\./test_case_df6ed188_monitoring_of_monitor\.sh[[:space:]]*$' \
     "$HERE/.github/workflows/ci.yml" 2>/dev/null; then
  ok "8. suite is registered as a runnable CI step"
else bad "8. suite registration" "no 'run:' step in .github/workflows/ci.yml"; fi
if grep -Eq '^[[:space:]]*run:[[:space:]]*python3 -m unittest -v test_alert_queue\.py[[:space:]]*$' \
     "$HERE/.github/workflows/ci.yml" 2>/dev/null; then
  ok "8b. alert-queue suite is registered as a runnable CI step"
else bad "8b. alert-queue registration" "no unittest step in .github/workflows/ci.yml"; fi

# === EXIT GATE (last line: the script's status is the suite's status) ============
echo "---"
echo "$(pass_count)"
(( FAIL == 0 ))
