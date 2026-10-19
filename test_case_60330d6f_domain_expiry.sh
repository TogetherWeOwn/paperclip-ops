#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_domain_expiry.sh — registrar domain-expiry probe
# (push-domain-expiry.sh, the RDAP complement to push-tls-expiry.sh).
#
# What is pinned here:
#   1. CONFIG — the domain-expiry external endpoint declares token, its 12h
#              heartbeat, immediate alert thresholds, a 24h reminder re-page
#              on both discord+custom alerts, and the module-wiring gate
#              passes (script + cron + registry all present).
#   2. SEMANTICS — stub RDAP server: far expiry pushes true; 10d pushes
#              false "warning:"; 3d and expired push false "critical:"; a
#              response with no expiration event, bad JSON or HTTP 500
#              pushes false "unverifiable" (exit 1). Worst domain wins.
#   3. DERIVE — no RDAP_DOMAINS: hostnames from TWO_WEB_*_URL are reduced to
#              registered domains and deduplicated before any RDAP query.
#   4. CONFIG REFUSALS — bad domain, domain with a port, inverted
#              thresholds, non-loopback http RDAP base, loopback-lookalike
#              http RDAP bases (127.evil.com, localhost.evil.com,
#              127.0.0.1.evil.com) and userinfo http RDAP bases
#              (localhost@evil, localhost:x@evil, 127.0.0.1:x@evil),
#              no domains at all and a missing curl binary all exit 2
#              and push NOTHING.
#   5. NON-DISCLOSURE — the Gatus token never appears in curl argv.
#   6. MUTANTS — parse-anchor, HTTP-gate and warn-default mutants of the
#              probe each break the far-expiry green, proving the suite is
#              not vacuous.
#
# Hermetic: one stub HTTP server serves both Gatus pushes and RDAP GETs on
# 127.0.0.1; RDAP dates are computed relative to now at request time. No
# network beyond loopback, no credentials.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
CFG="$SRC/gatus-config.yaml"
PROBE="$SRC/push-domain-expiry.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null' EXIT

[[ -f "$PROBE" ]] || { echo "REFUSE: $PROBE not found"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }

# --- stub server: Gatus POST recorder + RDAP GET fixture -------------------
cat > "$WORK/server.py" <<'PY'
import http.server, json, os, sys
from datetime import datetime, timedelta, timezone
PUSH = os.environ["PUSH_FILE"]; RDAPQ = os.environ["RDAP_FILE"]
MODE = os.environ["MODE_FILE"]

def mode_for(domain):
    try:
        raw = open(MODE).read().strip()
    except OSError:
        return "far"
    if "=" not in raw:
        return raw or "far"
    table = dict(p.split("=", 1) for p in raw.split(",") if "=" in p)
    return table.get(domain, "far")

def rdap_doc(mode):
    days = {"far": 90, "warn": 10, "crit": 3, "expired": -2}.get(mode)
    if mode == "noexpiry":
        events = [{"eventAction": "registration",
                   "eventDate": "2020-01-01T00:00:00Z"}]
    else:
        exp = datetime.now(timezone.utc) + timedelta(days=days or 90)
        events = [{"eventAction": "registration",
                   "eventDate": "2020-01-01T00:00:00Z"},
                  {"eventAction": "expiration",
                   "eventDate": exp.strftime("%Y-%m-%dT%H:%M:%SZ")}]
    return {"objectClassName": "domain", "events": events}

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n: self.rfile.read(n)
        with open(PUSH, "a") as f:
            f.write(json.dumps({"path": self.path}) + "\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_GET(self):
        domain = self.path.rstrip("/").rsplit("/", 1)[-1]
        with open(RDAPQ, "a") as f:
            f.write(domain + "\n")
        mode = mode_for(domain)
        if mode == "http500":
            self.send_response(500); self.end_headers()
            self.wfile.write(b"boom"); return
        body = b"{not json" if mode == "badjson" \
            else json.dumps(rdap_doc(mode)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/rdap+json")
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
PORT="${case_60330d6f_RDAP_TEST_PORT:-8804}"
export PUSH_FILE="$WORK/pushes.jsonl" RDAP_FILE="$WORK/rdap.txt" MODE_FILE="$WORK/mode.txt"
: > "$PUSH_FILE"; : > "$RDAP_FILE"; echo far > "$MODE_FILE"
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

T="PATH=$WORK/bin:/usr/bin:/bin HOME=$HOME TMPDIR=$WORK GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=T RDAP_BASE_URL=http://127.0.0.1:$PORT/domain/ DOMAIN_WARN_DAYS= DOMAIN_CRIT_DAYS="
fresh() { : > "$PUSH_FILE"; : > "$RDAP_FILE"; : > "$WORK/argv.log"; }
last_push() {
  [[ -s "$PUSH_FILE" ]] || { echo "EMPTY"; return; }
  python3 -c "import json,sys; print([json.loads(x)['path'] for x in open(sys.argv[1])][-1])" "$PUSH_FILE"
}
run_probe() { fresh; env $T "$@" bash "$PROBE" >/dev/null 2>&1; echo "rc=$?"; }

# === 1. CONFIG ================================================================
ep_block() {
  awk -v name="$2" '
    /^  - name: / { if (f) exit; if ($0 == "  - name: " name) f=1 }
    f { print }' "$1"
}
blk="$(ep_block "$CFG" domain-expiry)"
if grep -q 'token:' <<<"$blk" && grep -q 'interval: 12h' <<<"$blk" \
  && [[ "$(grep -c 'failure-threshold: 1' <<<"$blk")" -eq 2 ]] \
  && [[ "$(grep -c 'success-threshold: 1' <<<"$blk")" -eq 2 ]] \
  && [[ "$(grep -c 'minimum-reminder-interval: 24h' <<<"$blk")" -eq 2 ]] \
  && grep -Fq 'no push in 12h (detection within 24h)' <<<"$blk" \
  && grep -q 'type: discord' <<<"$blk" && grep -q 'type: custom' <<<"$blk"; then
  ok "1a. endpoint declares token, 12h heartbeat, immediate discord+custom alerts with 24h re-page"
else bad "1a. domain-expiry endpoint" "$(ep_block "$CFG" domain-expiry || echo MISSING)"; fi
mut="$WORK/nodomain.yaml"
awk '/- name: domain-expiry/{skip=1} skip&&/^  - name: /&&!/- name: domain-expiry/{skip=0} !skip' "$CFG" > "$mut"
if grep -q 'token:' <<<"$(ep_block "$mut" domain-expiry)"; then
  bad "1b. endpoint-less mutant" "mutant still declares the endpoint"
else ok "1b. endpoint-less mutant goes red"; fi
if python3 "$SRC/check_module_wiring.py" --root "$HERE" >/dev/null 2>&1; then
  ok "1c. module-wiring gate passes (script + cron + registry)"
else bad "1c. module-wiring gate" "$(python3 "$SRC/check_module_wiring.py" --root "$HERE" 2>&1 | head -5)"; fi

# === 2. SEMANTICS ==============================================================
echo far > "$MODE_FILE"; out="$(run_probe RDAP_DOMAINS='example.com')"; p="$(last_push)"
[[ "$out" == "rc=0" ]] && grep -q 'platform_domain-expiry/external?success=true' <<<"$p" \
  && ok "2a. far expiry pushes true (rc 0)" || bad "2a. far expiry" "$out push=$p"

echo warn > "$MODE_FILE"; out="$(run_probe RDAP_DOMAINS='example.com')"; p="$(last_push)"
[[ "$out" == "rc=1" ]] && grep -q 'success=false' <<<"$p" && grep -qi 'warning' <<<"$p" \
  && ok "2b. 10d pushes false warning (rc 1)" || bad "2b. warn window" "$out push=$p"

echo crit > "$MODE_FILE"; out="$(run_probe RDAP_DOMAINS='example.com')"; p="$(last_push)"
[[ "$out" == "rc=1" ]] && grep -qi 'critical' <<<"$p" \
  && ok "2c. 3d pushes false critical" || bad "2c. crit window" "$out push=$p"

echo expired > "$MODE_FILE"; out="$(run_probe RDAP_DOMAINS='example.com')"; p="$(last_push)"
[[ "$out" == "rc=1" ]] && grep -qi 'critical' <<<"$p" \
  && ok "2d. expired pushes false critical" || bad "2d. expired" "$out push=$p"

for m in noexpiry badjson http500; do
  echo "$m" > "$MODE_FILE"; out="$(run_probe RDAP_DOMAINS='example.com')"; p="$(last_push)"
  [[ "$out" == "rc=1" ]] && grep -q 'unverifiable' <<<"$p" \
    && ok "2e-$m. $m pushes false unverifiable" || bad "2e-$m. $m fail-closed" "$out push=$p"
done

echo 'alpha.example=warn,beta.example=crit' > "$MODE_FILE"
out="$(run_probe RDAP_DOMAINS='alpha.example beta.example')"; p="$(last_push)"
[[ "$out" == "rc=1" ]] && grep -qi 'critical' <<<"$p" && grep -q 'beta' <<<"$p" \
  && ok "2f. worst domain wins (crit over warn)" || bad "2f. worst domain" "$out push=$p"

echo far > "$MODE_FILE"
out="$(run_probe RDAP_DOMAINS=$'alpha.example\nbeta.example')"
got="$(sort -u "$RDAP_FILE" | tr '\n' ' ')"
[[ "$out" == "rc=0" && "$got" == "alpha.example beta.example " ]] \
  && ok "2g. newline-separated RDAP_DOMAINS checks every domain" \
  || bad "2g. newline-separated domains" "$out rdap=$got"

out="$(run_probe RDAP_DOMAINS=$'alpha.example\r\nbeta.example')"
got="$(sort -u "$RDAP_FILE" | tr '\n' ' ')"
[[ "$out" == "rc=0" && "$got" == "alpha.example beta.example " ]] \
  && ok "2h. CRLF-separated RDAP_DOMAINS checks every domain" \
  || bad "2h. CRLF-separated domains" "$out rdap=$got"

# === 3. DERIVE ==================================================================
echo far > "$MODE_FILE"
out="$(run_probe TWO_WEB_PROD_URL='https://app.example.com/path' TWO_WEB_STAGING_URL='https://next.example.com/')"
got="$(sort -u "$RDAP_FILE" | tr '\n' ' ')"
[[ "$out" == "rc=0" && "$got" == "example.com " ]] \
  && ok "3. URL hosts reduce to one deduplicated registered domain" \
  || bad "3. derive domains" "$out rdap=$got"

# === 4. CONFIG REFUSALS (exit 2, nothing pushed) =================================
refuse() {  # <label> <env...>
  local label="$1"; shift
  fresh; env $T "$@" bash "$PROBE" >/dev/null 2>&1; local rc=$?
  [[ $rc -eq 2 && "$(last_push)" == "EMPTY" ]] \
    && ok "4-$label. refuses (rc 2, no push)" || bad "4-$label. refusal" "rc=$rc push=$(last_push)"
}
refuse baddomain RDAP_DOMAINS='not a domain!'
refuse withport RDAP_DOMAINS='example.com:443'
refuse inverted RDAP_DOMAINS='example.com' DOMAIN_WARN_DAYS=7 DOMAIN_CRIT_DAYS=21
refuse httpremote RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://rdap.example.net/domain/'
refuse loopbackprefix127 RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://127.evil.com/domain/'
refuse loopbackprefixlocal RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://localhost.evil.com/domain/'
refuse loopbackprefixip RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://127.0.0.1.evil.com/domain/'
refuse userinfoat RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://localhost@evil.example.net/domain/'
refuse userinfoport RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://localhost:x@evil.example.net/domain/'
refuse userinfoipport RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://127.0.0.1:x@evil.example.net/domain/'
refuse nodomains TWO_WEB_PROD_URL='' TWO_WEB_STAGING_URL=''
refuse nocurl RDAP_DOMAINS='example.com' CURL_BIN="$WORK/bin/no-such-curl"
refuse barenonslashlookalike RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://localhostexample.com'
refuse bareslashlookalike RDAP_DOMAINS='example.com' RDAP_BASE_URL='http://localhostexample.com/domain/'

# Bare loopback / slash-less path bases must JOIN with a slash, never fuse
# host+domain into a non-loopback host (http://localhost + example.com once
# requested http://localhostexample.com over plaintext).
echo far > "$MODE_FILE"
out="$(run_probe RDAP_DOMAINS='example.com' RDAP_BASE_URL="http://127.0.0.1:$PORT")"; p="$(last_push)"
[[ "$out" == "rc=0" ]] && grep -q 'platform_domain-expiry/external?success=true' <<<"$p" \
  && grep -qx 'example.com' "$RDAP_FILE" \
  && ok "4-bareloopback. bare http://127.0.0.1:PORT joins to loopback path (rc 0)" \
  || bad "4-bareloopback. slash-less loopback join" "$out push=$p rdap=$(cat "$RDAP_FILE" 2>/dev/null)"
echo far > "$MODE_FILE"
out="$(run_probe RDAP_DOMAINS='example.com' RDAP_BASE_URL="http://127.0.0.1:$PORT/domain")"; p="$(last_push)"
[[ "$out" == "rc=0" ]] && grep -qx 'example.com' "$RDAP_FILE" \
  && ok "4-noslashpath. slash-less path base joins to loopback path (rc 0)" \
  || bad "4-noslashpath. slash-less path join" "$out push=$p rdap=$(cat "$RDAP_FILE" 2>/dev/null)"

# === 5. NON-DISCLOSURE ============================================================
echo far > "$MODE_FILE"; fresh
env $T RDAP_DOMAINS='example.com' bash "$PROBE" >/dev/null 2>&1
# The token T is one char and appears in ordinary words; assert on the
# Authorization material instead: no argv line may carry a Bearer header.
if grep -qi 'Bearer' "$WORK/argv.log"; then
  bad "5. token off argv" "$(grep -i Bearer "$WORK/argv.log" | head -1)"
else ok "5. Gatus token never on curl argv"; fi

# === 6. MUTANTS ====================================================================
mutant() {  # <label> <sed-expr>
  local label="$1"
  local expr="$2"
  local mdir="$WORK/mut-$label"
  mkdir -p "$mdir"
  sed "$expr" "$PROBE" > "$mdir/push-domain-expiry.sh"
  cp "$SRC/_gatus_push.sh" "$mdir/_gatus_push.sh"
  if cmp -s "$PROBE" "$mdir/push-domain-expiry.sh"; then
    bad "6-$label. mutant differs" "sed anchor did not change the probe"; return
  fi
  echo far > "$MODE_FILE"; fresh
  env $T RDAP_DOMAINS='example.com' bash "$mdir/push-domain-expiry.sh" >/dev/null 2>&1 \
    && [[ "$(last_push)" == *'success=true'* ]] \
    && bad "6-$label. mutant killed" "mutant still pushes green on far expiry" \
    || ok "6-$label. mutant killed (far no longer green)"
}
mutant anchor 's/"expiration"/"expiry"/'
mutant httpgate 's/\[\[ "$http" == "200" \]\]/[[ "$http" == "500" ]]/'
mutant warndefault 's/DOMAIN_WARN_DAYS:-21/DOMAIN_WARN_DAYS:-200/'

echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
