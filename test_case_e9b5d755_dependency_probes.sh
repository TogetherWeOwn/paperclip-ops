#!/usr/bin/env bash
# ===========================================================================
# test_case_e9b5d755_dependency_probes.sh — offline suite for the case_2d984d9b
# dependency probes (ops/case_658bf6b9/push-workers-error-rate.sh,
# ops/case_658bf6b9/push-github-rate-limit.sh + their gatus-config.yaml entries).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. WORKERS HEALTHY QUIET — stub analytics returns 0.05% errors on both
#      workers: exit 0, exactly one push, success=true.
#   2. WORKERS ERROR SPIKE — prod at 4% over the 1% default: exit 1, push
#      success=false naming the prod script.
#   3. WORKERS FAIL-CLOSED — HTTP 500, malformed JSON, zero invocations each
#      push success=false (exit 1), never a silent green.
#   4. WORKERS REFUSALS — missing CF_API_TOKEN or a malformed threshold
#      refuses (exit 2) with ZERO pushes and ZERO analytics calls.
#   5. WORKERS NON-DISCLOSURE — the CF token never reaches curl argv nor the
#      recorded push query string; the failure text names the worker only.
#   6. GITHUB HEALTHY QUIET — 55/60 remaining: exit 0, success=true.
#   7. GITHUB EXHAUSTED — 2/60 below the low-water mark: exit 1,
#      success=false.
#   8. GITHUB FAIL-CLOSED — HTTP 500 and malformed JSON each push
#      success=false; a malformed threshold refuses (exit 2), zero pushes.
#   9. GITHUB TOKEN-FREE — the stub records no Authorization header on any
#      /rate_limit call: the probe measures the unauthenticated quota and
#      can never leak a credential it does not carry.
#  10. CONFIG REGISTRY — both endpoints are declared in gatus-config.yaml
#      with token, heartbeat and alerts; no secret-shaped string lands in
#      the new scripts or the config additions.
#
# Hermetic: no network beyond 127.0.0.1, no credentials. A stub HTTP server
# serves fixtures and records pushes; a curl shim records argv then execs
# the real curl against the stub server.
# Refs: case_66436e78
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
CFG="$SRC/gatus-config.yaml"
WORKERS_SH="$SRC/push-workers-error-rate.sh"
GITHUB_SH="$SRC/push-github-rate-limit.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$WORKERS_SH" ]] || { echo "REFUSE: $WORKERS_SH missing or not executable"; exit 3; }
[[ -x "$GITHUB_SH" ]] || { echo "REFUSE: $GITHUB_SH missing or not executable"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }
command -v jq >/dev/null || { echo "REFUSE: jq required"; exit 3; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

# --- stub server: fixtures per $MODE_FILE, records pushes + auth sightings --
cat > "$WORK/server.py" <<'PY'
import http.server, json, os
MODE = os.environ["MODE_FILE"]
PUSH = os.environ["PUSH_FILE"]
class H(http.server.BaseHTTPRequestHandler):
    def _mode(self):
        try:
            with open(MODE) as f: return json.load(f)
        except Exception: return {}
    def _rec(self, obj):
        with open(PUSH, "a") as f: f.write(json.dumps(obj) + "\n")
    def _send(self, code, obj=None, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        if self.path.startswith("/api/v1/endpoints/"):
            self._rec({"kind": "push", "path": self.path,
                       "auth": self.headers.get("Authorization")})
            return self._send(200, {"ok": True})
        if self.path == "/cf/client/v4/graphql":
            mode = self._mode().get("cf", "healthy")
            if mode == "http500": return self._send(500, {"error": "boom"})
            if mode == "malformed": return self._send(200, raw=b"not json{{{")
            if mode == "nodata":
                return self._send(200, {"data": {"viewer": {"accounts": []}}})
            reqs, errs = (10000, 5)
            if mode == "spike" and "prod-script" in body: reqs, errs = (10000, 400)
            if mode == "zero": reqs, errs = (0, 0)
            return self._send(200, {"data": {"viewer": {"accounts": [
                {"workersInvocationsAdaptive": [{"sum": {
                    "requests": reqs, "errors": errs}}]}]}}})
        return self._send(404, {"error": "unknown"})
    def do_GET(self):
        if self.path == "/gh/rate_limit":
            self._rec({"kind": "ratelimit",
                       "auth": self.headers.get("Authorization")})
            mode = self._mode().get("gh", "healthy")
            if mode == "http500": return self._send(500, {"error": "boom"})
            if mode == "malformed": return self._send(200, raw=b"not json{{{")
            rem = 2 if mode == "exhausted" else 55
            return self._send(200, {"resources": {"core": {
                "limit": 60, "remaining": rem, "reset": 1785715200}}})
        return self._send(404, {"error": "unknown"})
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(__import__("sys").argv[1])), H).serve_forever()
PY
PORT="${case_e9b5d755_TEST_PORT:-8801}"
MODE_FILE="$WORK/mode.json"; PUSH_FILE="$WORK/pushes.jsonl"
export MODE_FILE PUSH_FILE
printf '{"cf":"healthy","gh":"healthy"}' > "$MODE_FILE"
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

BASE_ENV="GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=test-external-token"
CF_ENV="CF_API_BASE=http://127.0.0.1:$PORT/cf CF_API_TOKEN=test-cf-token-for-offline-suite CF_ACCOUNT_ID=test-acct WORKERS_STAGING_SCRIPT=staging-script WORKERS_PROD_SCRIPT=prod-script"
GH_ENV="GITHUB_API_BASE=http://127.0.0.1:$PORT/gh"
set_mode() { printf '{"cf":"%s","gh":"%s"}' "$1" "$2" > "$MODE_FILE"; : > "$PUSH_FILE"; : > "$WORK/argv.log"; }
pushes() { grep -c '"kind": "push"' "$PUSH_FILE" 2>/dev/null || true; }
push_success() { python3 -c "
import json,sys
ok=None
for line in open('$PUSH_FILE'):
    r=json.loads(line)
    if r.get('kind')=='push':
        ok=('success=true' in r['path'])
print('true' if ok else ('false' if ok is False else 'none'))"; }
analytics_calls() { grep -c "client/v4/graphql" "$WORK/argv.log" 2>/dev/null || true; }
export PATH="$WORK/bin:$PATH"

# === 1. WORKERS HEALTHY QUIET ================================================
set_mode healthy healthy
# shellcheck disable=SC2086
if env $BASE_ENV $CF_ENV "$WORKERS_SH" >"$WORK/out.txt" 2>&1; then
  [[ "$(pushes)" == "1" && "$(push_success)" == "true" ]] \
    && ok "workers healthy quiet pushes success=true once" \
    || bad "workers healthy push shape" "$(cat "$PUSH_FILE")"
else
  bad "workers healthy quiet exits 0" "$(cat "$WORK/out.txt")"
fi

# === 2. WORKERS ERROR SPIKE ===================================================
set_mode spike healthy
if env $BASE_ENV $CF_ENV "$WORKERS_SH" >"$WORK/out.txt" 2>&1; then
  bad "workers spike must exit nonzero" "$(cat "$WORK/out.txt")"
else
  if [[ "$(push_success)" == "false" ]] && grep -q "prod-script" "$WORK/out.txt"; then
    ok "workers spike fails closed naming prod"
  else
    bad "workers spike fail-closed shape" "$(cat "$WORK/out.txt") $(cat "$PUSH_FILE")"
  fi
fi

# === 3. WORKERS FAIL-CLOSED (500 / malformed / zero) ==========================
for m in http500 malformed zero nodata; do
  set_mode "$m" healthy
  if env $BASE_ENV $CF_ENV "$WORKERS_SH" >"$WORK/out.txt" 2>&1; then
    bad "workers $m must exit nonzero" "$(cat "$WORK/out.txt")"
  else
    [[ "$(push_success)" == "false" ]] \
      && ok "workers $m fails closed" \
      || bad "workers $m fail-closed shape" "$(cat "$PUSH_FILE")"
  fi
done

# === 4. WORKERS REFUSALS ======================================================
set_mode healthy healthy
if env $BASE_ENV CF_API_BASE=http://127.0.0.1:$PORT/cf CF_ACCOUNT_ID=test-acct \
    WORKERS_STAGING_SCRIPT=staging-script WORKERS_PROD_SCRIPT=prod-script \
    "$WORKERS_SH" >/dev/null 2>&1; then
  bad "workers missing token must refuse" "exited 0"
else
  rc=$?
  [[ $rc == 2 && "$(pushes)" == "0" && "$(analytics_calls)" == "0" ]] \
    && ok "workers missing token refuses exit 2, zero pushes, zero calls" \
    || bad "workers missing-token refusal" "rc=$rc pushes=$(pushes) calls=$(analytics_calls)"
fi
set_mode healthy healthy
if env $BASE_ENV $CF_ENV WORKERS_ERROR_THRESHOLD_PCT=banana \
    "$WORKERS_SH" >/dev/null 2>&1; then
  bad "workers bad threshold must refuse" "exited 0"
else
  [[ $? == 2 && "$(pushes)" == "0" ]] \
    && ok "workers malformed threshold refuses exit 2" \
    || bad "workers bad-threshold refusal" "rc=$? pushes=$(pushes)"
fi

# === 5. WORKERS NON-DISCLOSURE ================================================
set_mode spike healthy
env $BASE_ENV $CF_ENV "$WORKERS_SH" >/dev/null 2>&1 || true
if grep -q "test-cf-token-for-offline-suite" "$WORK/argv.log" "$PUSH_FILE" 2>/dev/null; then
  bad "workers token value must never reach argv or wire" "token sighted"
else
  ok "workers token value absent from argv and wire"
fi
if grep -q "Bearer" "$WORK/argv.log"; then
  bad "workers no Bearer literal on argv" "$(cat "$WORK/argv.log")"
else
  ok "workers no Bearer literal on argv (config-file transport)"
fi

# === 6. GITHUB HEALTHY QUIET ==================================================
set_mode healthy healthy
if env $BASE_ENV $GH_ENV "$GITHUB_SH" >"$WORK/out.txt" 2>&1; then
  [[ "$(pushes)" == "1" && "$(push_success)" == "true" ]] \
    && ok "github healthy quiet pushes success=true once" \
    || bad "github healthy push shape" "$(cat "$PUSH_FILE")"
else
  bad "github healthy quiet exits 0" "$(cat "$WORK/out.txt")"
fi

# === 7. GITHUB EXHAUSTED ======================================================
set_mode healthy exhausted
if env $BASE_ENV $GH_ENV "$GITHUB_SH" >"$WORK/out.txt" 2>&1; then
  bad "github exhausted must exit nonzero" "$(cat "$WORK/out.txt")"
else
  [[ "$(push_success)" == "false" ]] \
    && ok "github exhausted fails closed" \
    || bad "github exhausted fail-closed shape" "$(cat "$PUSH_FILE")"
fi

# === 8. GITHUB FAIL-CLOSED + REFUSAL ==========================================
for m in http500 malformed; do
  set_mode healthy "$m"
  if env $BASE_ENV $GH_ENV "$GITHUB_SH" >/dev/null 2>&1; then
    bad "github $m must exit nonzero" "exited 0"
  else
    [[ "$(push_success)" == "false" ]] \
      && ok "github $m fails closed" \
      || bad "github $m fail-closed shape" "$(cat "$PUSH_FILE")"
  fi
done
set_mode healthy healthy
if env $BASE_ENV $GH_ENV GITHUB_RATELIMIT_MIN_REMAINING=many \
    "$GITHUB_SH" >/dev/null 2>&1; then
  bad "github bad threshold must refuse" "exited 0"
else
  [[ $? == 2 && "$(pushes)" == "0" ]] \
    && ok "github malformed threshold refuses exit 2" \
    || bad "github bad-threshold refusal" "rc=$? pushes=$(pushes)"
fi

# === 9. GITHUB TOKEN-FREE =====================================================
set_mode healthy healthy
env $BASE_ENV $GH_ENV "$GITHUB_SH" >/dev/null 2>&1 || true
# The Gatus push itself carries the external token by design; only the
# /rate_limit reads must be Authorization-free.
if python3 -c "
import json, sys
rows = [json.loads(l) for l in open('$PUSH_FILE') if l.strip()]
rl = [r for r in rows if r.get('kind') == 'ratelimit']
sys.exit(0 if rl and all(r.get('auth') in (None, '') for r in rl) else 1)"; then
  ok "github probe sends no Authorization header"
else
  bad "github probe must send no Authorization header" "$(cat "$PUSH_FILE")"
fi

# === 10. CONFIG REGISTRY + NO SECRETS =========================================
for ep in workers-error-rate github-rate-limit; do
  if python3 - "$CFG" "$ep" <<'PY'
import re, sys
cfg, ep = sys.argv[1], sys.argv[2]
text = open(cfg).read()
blocks = re.split(r"(?m)^  - name: ", text)
hit = [b for b in blocks if b.startswith(ep + "\n")]
assert len(hit) == 1, f"endpoint {ep} must appear exactly once"
b = hit[0]
for needle in ['token: "${GATUS_EXTERNAL_TOKEN}"', "heartbeat:", "interval: 10m",
               "type: discord", "type: custom"]:
    assert needle in b, f"{ep} missing {needle}"
PY
  then
    ok "config declares platform_$ep with token, heartbeat, alerts"
  else
    bad "config declares platform_$ep" "see $CFG"
  fi
done
if grep -nE "https://discord.com/api/webhooks|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|AKMATORI_WEBHOOK_SECRET=|BEGIN [A-Z ]*PRIVATE KEY" \
    "$WORKERS_SH" "$GITHUB_SH" "$CFG" 2>/dev/null; then
  bad "no secret-shaped strings in new scripts or config" "match above"
else
  ok "no secret-shaped strings in new scripts or config"
fi

printf '\npass=%d fail=%d\n' "$PASS" "$FAIL"
[[ "$FAIL" == "0" ]]
