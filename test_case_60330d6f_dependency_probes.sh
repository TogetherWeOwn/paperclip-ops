#!/usr/bin/env bash
# ===========================================================================
# test_case_60330d6f_dependency_probes.sh — offline suite for the case_66436e78
# dependencies slice (case_e9612979): push-tls-expiry.sh + push-neon-reachability.sh.
#
# What it pins (each with a positive control that proves it can still fail):
#   1. CONFIG — the neon-staging external endpoint declares token, a 10m
#              heartbeat and discord+custom alerts; the tls-expiry endpoint
#              still declares token, its 26h heartbeat and alerts (the TLS
#              pusher's target). Endpoint-less mutants go red.
#   2. TLS SEMANTICS — stub openssl: far expiry pushes true; 10d pushes
#              false naming "warning"; 3d and expired push false naming
#              "critical"; handshake failure and unparseable dates push
#              false (unverifiable, exit 1). Worst domain wins. The stub x509
#              drains its stdin, so the stub s_client | x509 pair never reads
#              as a handshake failure under pipefail (SIGPIPE, case_20b2d898); a
#              mutant restoring `grep -m1` must go red.
#   3. TLS CONFIG — bad domain, inverted thresholds, missing openssl binary
#              and missing GATUS env refuse (exit 2) with zero pushes.
#              Default domains derive from TWO_WEB_*_URL (host[:port] only).
#   4. NEON SEMANTICS — localhost TCP listener + stub psql: TCP refused
#              pushes false without touching psql; SELECT != 1 and psql
#              failure push false; `SELECT 1` → exactly `1` pushes true.
#   5. STAGING-ONLY — the dialed host must equal NEON_STAGING_ENDPOINT_ID
#              (first DNS label, "-pooler" suffix allowed); any other host
#              pushes DOWN (exit 1) WITHOUT dialing (listener connection
#              count unchanged on localhost targets). A prod-like host/db is
#              kept as a backstop; a missing pin, URL or binary pushes DOWN
#              (exit 1 / exit 2 with zero pushes).
#   6. NON-DISCLOSURE — the DB password and full URL never reach psql argv,
#              curl argv, or the push path; psql travels with -h/-p/-U/-d
#              flags and PGPASSWORD env only.
#
# Hermetic: stub Gatus server, stub openssl/psql binaries, one localhost
# TCP listener. No network beyond 127.0.0.1, no credentials.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/ops/case_658bf6b9"
CFG="$SRC/gatus-config.yaml"
TLS="$SRC/push-tls-expiry.sh"
NEON="$SRC/push-neon-reachability.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null; [[ -n "${TCP_PID:-}" ]] && kill "$TCP_PID" 2>/dev/null' EXIT

[[ -f "$TLS" ]] || { echo "REFUSE: $TLS not found"; exit 3; }
[[ -f "$NEON" ]] || { echo "REFUSE: $NEON not found"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required"; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required"; exit 3; }

# --- stub Gatus server: records pushes to $WORK/pushes.jsonl ----------------
cat > "$WORK/server.py" <<'PY'
import http.server, json, os, sys
PUSH = os.environ["PUSH_FILE"]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        with open(PUSH, "a") as f:
            f.write(json.dumps({"path": self.path,
                                "auth": self.headers.get("Authorization")}) + "\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
PORT="${case_1721294e_TEST_PORT:-8802}"
PUSH_FILE="$WORK/pushes.jsonl"
export PUSH_FILE
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

BASE_ENV="PATH=$WORK/bin:/usr/bin:/bin HOME=$HOME TMPDIR=$WORK GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=T"
last_push() {  # prints last push path, or EMPTY
  [[ -s "$PUSH_FILE" ]] || { echo "EMPTY"; return; }
  python3 -c "import json; print([json.loads(x)['path'] for x in open('$PUSH_FILE')][-1])"
}
fresh() { : > "$PUSH_FILE"; : > "$WORK/argv.log"; : > "$WORK/psql-argv.log"; : > "$WORK/pgoptions.log"; }

# --- stub openssl: s_client looks up OPENSSL_FIXTURE per host ---------------
# Fixture: "far|warn|crit|expired|fail|unparseable" (all hosts) or a map
# "host=far,host2=crit". s_client records the connect target and emits a
# STUB-NOTAFTER marker plus dummy PEM; the x509 branch parses that marker
# from stdin — data flows through the pipe exactly like the real
# handshake-to-cert path, so there is no cross-process state file to race
# on (a shared stash file let x509 read before s_client wrote).
mkdir -p "$WORK/tools"
cat > "$WORK/tools/openssl" <<'SH'
#!/usr/bin/env bash
days_for() {  # <key> → whole days or FAIL/UNPARSEABLE
  local host="$1"
  local fix="${OPENSSL_FIXTURE:-far}"
  local val=""
  if [[ "$fix" == *"="* ]]; then
    val="$(tr ',' '\n' <<<"$fix" | awk -F= -v h="$host" '$1==h{print $2}' | head -1)"
    [[ -n "$val" ]] || val="far"
  else val="$fix"; fi
  case "$val" in
    far) echo 60 ;; warn) echo 10 ;; crit) echo 3 ;; expired) echo -1 ;;
    fail) echo FAIL ;; unparseable) echo UNPARSEABLE ;; *) echo 60 ;;
  esac
}
if [[ "$*" == *x509* ]]; then
  # Drain the pipe: `grep -m1` exits at the first line while s_client is
  # still writing the PEM, the writer takes SIGPIPE (141) and the caller's
  # pipefail reads a handshake failure (case_20b2d898). Real x509 does NOT drain
  # (it stops after the first certificate); this stub drains on purpose so a
  # stub artefact can never fail the pipeline.
  marker="$(grep -a '^STUB-NOTAFTER:' | head -n1 || true)"
  if [[ "$marker" == "STUB-NOTAFTER:UNPARSEABLE" ]]; then echo "notAfter=bogus"; exit 0; fi
  [[ -n "$marker" ]] || exit 1
  echo "notAfter=${marker#STUB-NOTAFTER:}"
  exit 0
fi
conn="$(tr ' ' '\n' <<<"$*" | grep -A1 -- '-connect' | tail -1)"
host="${conn%%:*}"
echo "$conn" >> "${OPENSSL_STATE_DIR:-/tmp}/openssl_connects.log"
d="$(days_for "$host")"
if [[ "$d" == "FAIL" ]]; then echo "connect: boom" >&2; exit 1; fi
if [[ "$d" == "UNPARSEABLE" ]]; then echo "STUB-NOTAFTER:UNPARSEABLE"; else echo "STUB-NOTAFTER:$(date -u -d "+$d days" "+%b %d %H:%M:%S %Y GMT")"; fi
echo "-----BEGIN CERTIFICATE-----"; echo "STUB"; echo "-----END CERTIFICATE-----"
SH
chmod +x "$WORK/tools/openssl"

# --- stub psql: PSQL_FIXTURE ok|wrong|fail; records argv ----------------------
cat > "$WORK/tools/psql" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PSQL_ARGV_LOG:-/dev/null}"
printf 'PGOPTIONS=%s\n' "${PGOPTIONS:-UNSET}" >> "${PGOPTIONS_LOG:-/dev/null}"
case "${PSQL_FIXTURE:-ok}" in
  ok) echo "1" ;;
  wrong) echo "0" ;;
  fail) echo "psql: boom" >&2; exit 1 ;;
esac
SH
chmod +x "$WORK/tools/psql"

# --- localhost TCP listener: counts connections --------------------------------
cat > "$WORK/listener.py" <<'PY'
import socket, sys
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(5)
print(srv.getsockname()[1], flush=True)
count = open(sys.argv[1], "w")
n = 0
while True:
    c, _ = srv.accept()
    c.close()
    n += 1
    count.write(str(n)); count.flush()
PY
: > "$WORK/conns.txt"
python3 "$WORK/listener.py" "$WORK/conns.txt" > "$WORK/lport.txt" 2>/dev/null & TCP_PID=$!
sleep 0.5
LPORT="$(cat "$WORK/lport.txt")"
[[ -n "$LPORT" ]] || { echo "REFUSE: TCP listener did not start"; exit 3; }
conns() { cat "$WORK/conns.txt" 2>/dev/null || echo 0; }

# === 1. CONFIG ================================================================
ep_block() {  # <file> <name> → block lines (leading spaces are literal)
  awk -v name="$2" '
    /^  - name: / { if (f) exit; if ($0 == "  - name: " name) f=1 }
    f { print }' "$1"
}
neon_block="$(ep_block "$CFG" neon-staging)"
if grep -q 'token:' <<<"$neon_block" \
  && grep -q 'interval: 10m' <<<"$neon_block" \
  && grep -q 'type: discord' <<<"$neon_block" \
  && grep -q 'type: custom' <<<"$neon_block"; then
  ok "1. neon-staging endpoint declares token, 10m heartbeat, discord+custom alerts"
else bad "1. neon-staging endpoint" "$(ep_block "$CFG" neon-staging || echo MISSING)"; fi
mut="$WORK/noneon.yaml"
awk '/- name: neon-staging/{skip=1} skip&&/^  - name: /&&!/- name: neon-staging/{skip=0} !skip' "$CFG" > "$mut"
mut_block="$(ep_block "$mut" neon-staging)"
if grep -q 'token:' <<<"$mut_block"; then
  bad "1b. neon-less mutant" "mutant still declares the endpoint"
else ok "1b. neon-less mutant goes red"; fi
tls_block="$(ep_block "$CFG" tls-expiry)"
if grep -q 'token:' <<<"$tls_block" \
  && grep -q 'interval: 26h' <<<"$tls_block" \
  && grep -q 'type: discord' <<<"$tls_block"; then
  ok "1c. tls-expiry endpoint keeps token, 26h heartbeat, alerts"
else bad "1c. tls-expiry endpoint" "$(ep_block "$CFG" tls-expiry || echo MISSING)"; fi

# === 2. TLS SEMANTICS ==========================================================
T="PATH=$WORK/bin:$WORK/tools:/usr/bin:/bin HOME=$HOME TMPDIR=$WORK GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=T OPENSSL_BIN=$WORK/tools/openssl OPENSSL_STATE_DIR=$WORK"
run_tls() { : > "$WORK/openssl_connects.log"; fresh
  # shellcheck disable=SC2086
  env $T "$@" bash "$TLS" >/dev/null 2>&1; echo "rc=$?"; }

fresh; out="$(run_tls OPENSSL_FIXTURE=far TLS_DOMAINS="web-staging.invalid web-prod.invalid")"
p="$(last_push)"
{ grep -q "platform_tls-expiry/external?success=true" <<<"$p"; } \
  && ok "2a. far expiry pushes success=true" \
  || bad "2a. far expiry" "$out $p"

fresh; out="$(run_tls OPENSSL_FIXTURE=warn TLS_DOMAINS="web-staging.invalid")"
p="$(last_push)"; { grep -q "success=false" <<<"$p" && grep -q "warning" <<<"$p" \
  && grep -q "web-staging.invalid" <<<"$p" && grep -Eq "[0-9]+d" <<<"$p"; } \
  && [[ "$out" == "rc=1" ]] && ok "2b. 10d expiry pushes false naming warning+host+days, exit 1" \
  || bad "2b. warn mapping" "$out $p"

fresh; out="$(run_tls OPENSSL_FIXTURE=crit TLS_DOMAINS="web-prod.invalid")"
p="$(last_push)"; { grep -q "success=false" <<<"$p" && grep -q "critical" <<<"$p" \
  && grep -q "web-prod.invalid" <<<"$p" && grep -Eq "[0-9]+d" <<<"$p"; } \
  && [[ "$out" == "rc=1" ]] && ok "2c. 3d expiry pushes false naming critical+host+days, exit 1" \
  || bad "2c. crit mapping" "$out $p"

fresh; out="$(run_tls OPENSSL_FIXTURE=expired TLS_DOMAINS="web-prod.invalid")"
p="$(last_push)"; { grep -q "success=false" <<<"$p" && grep -q "critical" <<<"$p"; } \
  && ok "2d. expired cert pushes critical DOWN" \
  || bad "2d. expired mapping" "$out $p"

fresh; out="$(run_tls OPENSSL_FIXTURE=fail TLS_DOMAINS="web-prod.invalid")"
p="$(last_push)"; { grep -q "success=false" <<<"$p"; } && [[ "$out" == "rc=1" ]] \
  && ok "2e. handshake failure pushes DOWN, exit 1 (fail closed)" \
  || bad "2e. handshake failure" "$out $p"

fresh; out="$(run_tls OPENSSL_FIXTURE=unparseable TLS_DOMAINS="web-prod.invalid")"
p="$(last_push)"
{ grep -q "success=false" <<<"$p"; } && [[ "$out" == "rc=1" ]] \
  && ok "2f. unparseable date pushes DOWN, exit 1" \
  || bad "2f. unparseable date" "$out $p"

fresh; out="$(run_tls OPENSSL_FIXTURE="a.invalid=warn,b.invalid=crit" TLS_DOMAINS="a.invalid b.invalid")"
p="$(last_push)"; { grep -q "success=false" <<<"$p" && grep -q "critical" <<<"$p" \
  && grep -q "b.invalid" <<<"$p" && ! grep -q "warning" <<<"$p"; } \
  && ok "2g. worst domain wins (crit beats warn, no warning label)" \
  || bad "2g. worst-domain aggregation" "$out $p"

# 2h/2i. The stub's own pipe handling (case_20b2d898). push-tls-expiry.sh runs
# `s_client | x509` under pipefail, so a stub x509 that stops reading early
# kills the still-writing s_client with SIGPIPE (141) and a healthy host reads
# `unverifiable`. The natural race is rare and box-dependent (2/300 measured on
# one box, 0/1500 on another), too rare to be a verdict by itself, so the check
# has two legs:
#   - oversized writer: the marker is followed by 1 MiB. A pipe holds 64 KiB, so
#     an early-exit reader has closed it before the writer can finish, whatever
#     the scheduling: the loss is every run, not a coin flip.
#   - natural pipeline: the stub s_client | x509 pair, SIGPIPE_RUNS times.
SIGPIPE_RUNS="${case_0aa9a335_SIGPIPE_RUNS:-200}"
stub_pipe_failures() {  # <openssl stub> → prints how many runs read as failed
  local stub="$1"
  local fails=0
  local i
  if ! ( set -o pipefail
         { echo "STUB-NOTAFTER:Jan 01 00:00:00 2099 GMT"
           head -c 1048576 /dev/zero | tr '\0' x; echo
           echo "-----BEGIN CERTIFICATE-----"; echo "STUB"; echo "-----END CERTIFICATE-----"
         } | "$stub" x509 -noout -enddate >/dev/null 2>&1 ); then
    fails=$((fails+1))
  fi
  for ((i = 0; i < SIGPIPE_RUNS; i++)); do
    if ! ( set -o pipefail
           OPENSSL_STATE_DIR="$WORK" OPENSSL_FIXTURE=far
           export OPENSSL_STATE_DIR OPENSSL_FIXTURE
           "$stub" s_client -connect web-prod.invalid:443 2>/dev/null \
             | "$stub" x509 -noout -enddate >/dev/null 2>&1 ); then
      fails=$((fails+1))
    fi
  done
  echo "$fails"
}

f="$(stub_pipe_failures "$WORK/tools/openssl")"
[[ "$f" == "0" ]] \
  && ok "2h. stub s_client|x509 never SIGPIPEs under pipefail (0 failures in $((SIGPIPE_RUNS+1)) runs)" \
  || bad "2h. stub drains its pipe" "$f failures in $((SIGPIPE_RUNS+1)) runs"

# Mutant: restore `grep -m1`. It must differ from the stub, and the same check
# must go red on it — otherwise 2h could pass while measuring nothing.
mkdir -p "$WORK/tools-m1"
sed "s/grep -a '\^STUB-NOTAFTER:' | head -n1/grep -a -m1 '^STUB-NOTAFTER:'/" \
  "$WORK/tools/openssl" > "$WORK/tools-m1/openssl"
chmod +x "$WORK/tools-m1/openssl"
if cmp -s "$WORK/tools/openssl" "$WORK/tools-m1/openssl" \
   || ! grep -q "grep -a -m1 '^STUB-NOTAFTER:'" "$WORK/tools-m1/openssl"; then
  bad "2i. -m1 mutant" "mutation did not apply; the stub's marker line changed shape"
else
  f="$(stub_pipe_failures "$WORK/tools-m1/openssl")"
  (( f > 0 )) \
    && ok "2i. restoring grep -m1 goes red ($f failures)" \
    || bad "2i. -m1 mutant stays green" "0 failures in $((SIGPIPE_RUNS+1)) runs; the SIGPIPE check cannot see the race"
fi

# === 3. TLS CONFIG ==============================================================
fresh; out="$(run_tls OPENSSL_FIXTURE=far TLS_DOMAINS="bad_host!")"
{ [[ "$out" == "rc=2" ]] && [[ "$(last_push)" == "EMPTY" ]]; } \
  && ok "3a. bad domain refuses exit 2 with zero pushes" \
  || bad "3a. bad domain" "$out $(last_push)"

fresh; out="$(run_tls OPENSSL_FIXTURE=far TLS_DOMAINS="web-prod.invalid" TLS_WARN_DAYS=7 TLS_CRIT_DAYS=7)"
{ [[ "$out" == "rc=2" ]] && [[ "$(last_push)" == "EMPTY" ]]; } \
  && ok "3b. inverted thresholds refuse exit 2 with zero pushes" \
  || bad "3b. threshold validation" "$out $(last_push)"

fresh
env -i PATH="/usr/bin:/bin" bash "$TLS" >/dev/null 2>&1; rc=$?
{ (( rc == 2 )) && [[ "$(last_push)" == "EMPTY" ]]; } \
  && ok "3c. missing env refuses exit 2 with zero pushes" \
  || bad "3c. missing env refusal" "rc=$rc $(last_push)"

fresh; out="$(run_tls OPENSSL_BIN="$WORK/tools/no-such-openssl" TLS_DOMAINS="web-prod.invalid")"
{ [[ "$out" == "rc=2" ]] && [[ "$(last_push)" == "EMPTY" ]]; } \
  && ok "3d. missing openssl binary refuses exit 2" \
  || bad "3d. missing openssl" "$out $(last_push)"

fresh
# shellcheck disable=SC2086
env $T OPENSSL_FIXTURE=far TWO_WEB_PROD_URL="https://web-prod.invalid:8443/health" \
  TWO_WEB_STAGING_URL="https://web-staging.invalid/health" bash "$TLS" >/dev/null 2>&1
{ grep -q "web-prod.invalid:8443" "$WORK/openssl_connects.log" && grep -q "web-staging.invalid:443" "$WORK/openssl_connects.log"; } \
  && ok "3e. default domains derive host[:port] from TWO_WEB_*_URL" \
  || bad "3e. URL-derived domains" "$(cat "$WORK/openssl_connects.log" 2>/dev/null || echo NODIAL)"

# === 4. NEON SEMANTICS ===========================================================
PW="pw-$RANDOM$RANDOM-test"
N="PATH=$WORK/bin:$WORK/tools:/usr/bin:/bin HOME=$HOME TMPDIR=$WORK GATUS_URL=http://127.0.0.1:$PORT GATUS_EXTERNAL_TOKEN=T PSQL_BIN=$WORK/tools/psql PSQL_ARGV_LOG=$WORK/psql-argv.log PSQL_FIXTURE=ok PGOPTIONS_LOG=$WORK/pgoptions.log NEON_STAGING_ENDPOINT_ID=127"
STAGING_URL="postgres://staging_user:$PW@127.0.0.1:$LPORT/stagingdb"
run_neon() { fresh
  # shellcheck disable=SC2086
  env $N "$@" bash "$NEON" >/dev/null 2>&1; echo "rc=$?"; }

fresh; c0="$(conns)"; out="$(run_neon NEON_STAGING_DATABASE_URL="$STAGING_URL")"
p="$(last_push)"
{ grep -q "platform_neon-staging/external?success=true" <<<"$p"; } \
  && ok "4a. TCP ok + SELECT 1 pushes success=true" \
  || bad "4a. healthy staging" "$out $p"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="postgres://staging_user:$PW@127.0.0.1:1/stagingdb")"
p="$(last_push)"; { grep -q "success=false" <<<"$p" && grep -q "tcp" <<<"$p"; } \
  && [[ "$out" == "rc=1" ]] && [[ ! -s "$WORK/psql-argv.log" ]] \
  && ok "4b. TCP refused pushes DOWN without touching psql" \
  || bad "4b. TCP failure" "$out $p"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="$STAGING_URL" PSQL_FIXTURE=wrong)"
p="$(last_push)"; { grep -q "success=false" <<<"$p"; } && [[ "$out" == "rc=1" ]] \
  && ok "4c. unexpected SELECT result pushes DOWN, exit 1" \
  || bad "4c. wrong SELECT result" "$out $p"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="$STAGING_URL" PSQL_FIXTURE=fail)"
p="$(last_push)"
{ grep -q "success=false" <<<"$p"; } && [[ "$out" == "rc=1" ]] \
  && ok "4d. psql failure pushes DOWN, exit 1" \
  || bad "4d. psql failure" "$out $p"

# === 5. STAGING-ONLY ==============================================================
fresh; c0="$(conns)"
out="$(run_neon NEON_STAGING_DATABASE_URL="postgres://staging_user:$PW@127.0.0.1:$LPORT/proddb")"
p="$(last_push)"; c1="$(conns)"
{ grep -q "success=false" <<<"$p" && grep -qi "prod" <<<"$p"; } \
  && [[ "$out" == "rc=1" ]] && [[ "$c1" == "$c0" ]] \
  && ok "5a. prod-like db pushes DOWN without dialing (conns $c0→$c1)" \
  || bad "5a. prod db guard" "$out $p conns=$c0→$c1"

fresh; c0="$(conns)"
out="$(run_neon NEON_STAGING_DATABASE_URL="postgres://staging_user:${PW}@prod-primary.example.invalid:5432/stagingdb" NEON_STAGING_ENDPOINT_ID=prod-primary)"
c1="$(conns)"
p="$(last_push)"
{ grep -q "success=false" <<<"$p"; } && [[ "$c1" == "$c0" ]] \
  && ok "5b. prod-like host pushes DOWN without dialing (backstop)" \
  || bad "5b. prod host guard" "$out $p conns=$c0→$c1"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="not a url")"
p="$(last_push)"
{ grep -q "success=false" <<<"$p"; } && [[ "$out" == "rc=1" ]] \
  && ok "5c. unparseable URL pushes DOWN, exit 1" \
  || bad "5c. unparseable URL" "$out $p"

fresh
# shellcheck disable=SC2086
env $N bash -c 'unset NEON_STAGING_DATABASE_URL; exec bash "'"$NEON"'"' >/dev/null 2>&1; rc=$?
p="$(last_push)"
{ grep -q "success=false" <<<"$p"; } && (( rc == 1 )) \
  && ok "5d. missing URL pushes DOWN, exit 1 (never silent green)" \
  || bad "5d. missing URL" "rc=$rc $p"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="$STAGING_URL" PSQL_BIN="$WORK/tools/no-such-psql")"
{ [[ "$out" == "rc=2" ]] && [[ "$(last_push)" == "EMPTY" ]]; } \
  && ok "5e. missing psql binary refuses exit 2 with zero pushes" \
  || bad "5e. missing psql" "$out $(last_push)"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="$STAGING_URL" TIMEOUT_BIN="$WORK/tools/no-such-timeout")"
{ [[ "$out" == "rc=2" ]] && [[ "$(last_push)" == "EMPTY" ]]; } \
  && ok "5e2. missing timeout binary refuses exit 2 with zero pushes" \
  || bad "5e2. missing timeout" "$out $(last_push)"

fresh; c0="$(conns)"
out="$(run_neon NEON_STAGING_DATABASE_URL="$STAGING_URL" NEON_STAGING_ENDPOINT_ID=ep-otherbranch9)"
c1="$(conns)"
p="$(last_push)"
{ grep -q "success=false" <<<"$p" && grep -q "pinned" <<<"$p"; } \
  && [[ "$out" == "rc=1" ]] && [[ "$c1" == "$c0" ]] \
  && ok "5f. unpinned endpoint pushes DOWN without dialing (conns $c0→$c1)" \
  || bad "5f. endpoint pin guard" "$out $p conns=$c0→$c1"

fresh
# shellcheck disable=SC2086
env $N NEON_STAGING_DATABASE_URL="$STAGING_URL" bash -c 'unset NEON_STAGING_ENDPOINT_ID; exec bash "'"$NEON"'"' >/dev/null 2>&1; rc=$?
p="$(last_push)"
{ grep -q "success=false" <<<"$p"; } && (( rc == 1 )) \
  && ok "5g. missing endpoint pin pushes DOWN, exit 1 (never silent green)" \
  || bad "5g. missing endpoint pin" "rc=$rc $p"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="postgres://staging_user:${PW}@ep-staging123-pooler.example.invalid:5432/stagingdb" NEON_STAGING_ENDPOINT_ID=ep-staging123)"
p="$(last_push)"
{ grep -q "success=false" <<<"$p" && grep -q "tcp" <<<"$p"; } \
  && [[ "$out" == "rc=1" ]] \
  && ok "5h. -pooler host passes the pin and reaches stage 1" \
  || bad "5h. pooler suffix acceptance" "$out $p"

fresh; out="$(run_neon NEON_STAGING_DATABASE_URL="postgres://staging_user:${PW}@ep-bbb222.example.invalid:5432/stagingdb" NEON_STAGING_ENDPOINT_ID=ep-aaa111)"
p="$(last_push)"
{ grep -q "success=false" <<<"$p" && grep -q "pinned" <<<"$p"; } \
  && [[ "$out" == "rc=1" ]] \
  && ok "5i. wrong endpoint ID (no prod substring) still refuses to dial" \
  || bad "5i. endpoint mismatch refusal" "$out $p"

# === 6. NON-DISCLOSURE =============================================================
fresh; run_neon NEON_STAGING_DATABASE_URL="$STAGING_URL" >/dev/null
leak=""
grep -qF "$PW" "$WORK/psql-argv.log" && leak="$leak psql-argv"
grep -qF "$PW" "$WORK/argv.log" && leak="$leak curl-argv"
grep -qF "$STAGING_URL" "$WORK/psql-argv.log" && leak="$leak url-in-psql-argv"
grep -qF "$STAGING_URL" "$PUSH_FILE" && leak="$leak url-on-wire"
[[ -z "$leak" ]] \
  && ok "6. password and full URL in neither argv nor the push path" \
  || bad "6. secret containment" "leaked via:$leak"
grep -q "\-U" "$WORK/psql-argv.log" \
  && ok "6b. psql travels with -h/-p/-U/-d flags (password via env only)" \
  || bad "6b. psql invocation" "$(cat "$WORK/psql-argv.log")"
grep -qx "PGOPTIONS=UNSET" "$WORK/pgoptions.log" \
  && ok "6c. no PGOPTIONS startup parameter reaches psql (pooler-safe)" \
  || bad "6c. PGOPTIONS leak" "$(cat "$WORK/pgoptions.log" 2>/dev/null || echo NOLOG)"

echo "---"
echo "pass=$PASS fail=$FAIL"
(( FAIL == 0 ))
