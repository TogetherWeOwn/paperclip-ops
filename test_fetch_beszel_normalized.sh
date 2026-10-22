#!/usr/bin/env bash
# test_fetch_beszel_normalized.sh — offline suite for the case_f2010cb7
# Beszel fetch wrapper (fetch-beszel-normalized.sh).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. HAPPY PATH — against a stub curl serving a fake PocketBase hub, the
#      wrapper exits 0 with a one-host normalized JSON array on stdout
#      (cpu mapped, no invented inode/restarts keys). Without this, a
#      suite that only asserts refusals could pass against a script that
#      refuses everything.
#   2. NON-DISCLOSURE — the hub password travels in the 0600 data file
#      (present in the stub-recorded data-binary content) and never in
#      curl argv; the token likewise never appears in argv.
#   3. HOSTILE TOKEN — a hub answer whose token carries a quote plus a
#      newline (a curl-config injection) is refused with exit 2 before any
#      fetch, and the injected `output =` path is never created.
#   4. REMOTE HTTP REFUSED — plain http to a non-loopback hub exits 2
#      with no curl call at all; loopback http (§1) and remote https
#      (positive control) still fetch.
#   5. TOKENLESS AUTH — an auth answer with no token exits 2 with empty
#      stdout, so the consumer scores nothing.
#   6. LOOPBACK GATE — the http-vs-https gate parses the URL authority,
#      it does not glob it: userinfo forms ("127.0.0.1@host",
#      "user:pass@127.0.0.1"), lookalike hosts ("127.evil.example",
#      hex/octal or decimal-integer IPv4), and a bare "::1" are refused
#      with no curl call; "localhost" and bracketed "[::1]" fetch.
#      (Without this, a misconfigured URL sends the password in the
#      clear to a remote host.)
#
# Hermetic: no network, no credentials. A stub curl answers from fixture
# JSON and records argv + config + data-file contents. Fake secret-shaped
# values only (TEST-*-not-a-secret).
set -uo pipefail

# A BASH_ENV hook in some harnesses resets PATH in child shells and unshadows
# this suite's stub curl; drop it so the hermetic stub holds for every run.
unset BASH_ENV

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ops/case_f2010cb7/fetch-beszel-normalized.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$TOOL" ]] || { echo "REFUSE: $TOOL missing or not executable" >&2; exit 3; }
command -v curl >/dev/null || { echo "REFUSE: curl required" >&2; exit 3; }
command -v jq >/dev/null || { echo "REFUSE: jq required" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }

FAKE_PASSWORD="TEST-password-not-a-secret"
FAKE_TOKEN="TEST-token-not-a-secret"

# --- fake hub payloads -------------------------------------------------------
new_stamp() { date -u '+%Y-%m-%d %H:%M:%S.000Z'; }
old_stamp() { date -u -d "@$(( $(date +%s) - $1 ))" '+%Y-%m-%d %H:%M:%S.000Z'; }

make_hub() { # $1 = dir; writes systems/1m/history envelopes with fresh stamps
  local d="$1" now
  now="$(new_stamp)"
  cat > "$d/systems.json" <<'JSON'
{"items": [{"id": "sys000000000001", "name": "testhub-1", "status": "up",
  "info": {"c": 2, "t": 4, "sv": [312, 0], "la": [2.4, 2.8, 3.2]}}]}
JSON
  cat > "$d/stats-1m.json" <<JSON
{"items": [{"id": "stat00000000001", "system": "sys000000000001", "type": "1m",
  "created": "$now",
  "stats": {"cpu": 25.0, "mp": 60.0, "dp": 42.5, "d": 100.0, "du": 42.5,
            "la": [2.4, 2.8, 3.2]}}]}
JSON
  cat > "$d/stats-hist.json" <<JSON
{"items": [
  {"id": "h1", "system": "sys000000000001", "type": "480m", "created": "$(old_stamp 28800)",
   "stats": {"cpu": 20.0, "mp": 55.0, "dp": 40.0, "d": 100.0, "du": 40.0, "la": [1.0, 1.0, 1.0]}},
  {"id": "h2", "system": "sys000000000001", "type": "480m", "created": "$(old_stamp 57600)",
   "stats": {"cpu": 21.0, "mp": 56.0, "dp": 41.0, "d": 100.0, "du": 41.0, "la": [1.1, 1.1, 1.1]}}
]}
JSON
}

# stub curl: invoked as `curl --config FILE`; answers from the config URL and
# records argv, config text, and any data-binary file content.
make_stub() { # $1 = dir
  cat > "$1/curl" <<'STUB'
#!/usr/bin/env bash
logdir="${STUB_LOGDIR:?}"
cfg="$2"
printf '%s\n' "$*" >> "$logdir/argv.log"
cat "$cfg" >> "$logdir/config.log"
printf '\n---\n' >> "$logdir/config.log"
url="$(grep '^url = "' "$cfg" | head -1 | sed 's/^url = "//; s/"$//')"
bin="$(grep '^data-binary = "' "$cfg" | head -1 | sed 's/^data-binary = "@//; s/"$//')"
if [[ -n "$bin" && -f "$bin" ]]; then
  cat "$bin" >> "$logdir/data.log"
  printf '\n---\n' >> "$logdir/data.log"
fi
case "$url" in
  *auth-with-password*) printf '%s' "${STUB_AUTH_RESP:?}";;
  */systems/records*) cat "${STUB_SYSTEMS:?}";;
  *%271m%27*) cat "${STUB_STATS_1M:?}";;
  *system_stats*) cat "${STUB_STATS_HIST:?}";;
esac
exit 0
STUB
  chmod 0755 "$1/curl"
}

run_tool() { # $1 = test dir; rest = env overrides; runs wrapper, captures out/err/rc
  local d="$1"; shift
  STUB_LOGDIR="$d" STUB_AUTH_RESP="$(cat "$d/auth.json")" STUB_SYSTEMS="$d/systems.json" \
    STUB_STATS_1M="$d/stats-1m.json" STUB_STATS_HIST="$d/stats-hist.json" \
    PATH="$d:$PATH" TMPDIR="$d/tmp" \
    BESZEL_IDENTITY="test@example.invalid" BESZEL_PASSWORD="$FAKE_PASSWORD" \
    env "$@" "$TOOL" >"$d/stdout" 2>"$d/stderr"
  echo "$?" > "$d/rc"
}

fresh_case() { # $1 = name; prints dir; sets up hub + stub + default auth
  local d
  d="$(mktemp -d "${TMPDIR:-/tmp}/beszel_fetch_test.XXXXXXXX")"
  mkdir -p "$d/tmp"
  : > "$d/argv.log"; : > "$d/config.log"; : > "$d/data.log"
  make_hub "$d"
  printf '{"token": "%s"}' "$FAKE_TOKEN" > "$d/auth.json"
  make_stub "$d"
  printf '%s' "$d"
}

# --- §1 happy path (loopback http) + §2 non-disclosure ------------------------
D="$(fresh_case happy)"
run_tool "$D" BESZEL_HUB_URL="http://127.0.0.1:8090"
rc="$(cat "$D/rc")"
if [[ "$rc" == "0" ]] && jq -e 'type == "array" and length == 1' "$D/stdout" >/dev/null 2>&1; then
  ok "happy path exits 0 with a one-host array"
else
  bad "happy path exits 0 with a one-host array" "rc=$rc stderr=$(head -c 300 "$D/stderr")"
fi
if jq -e '.[0].cpu_used_percent == 25' "$D/stdout" >/dev/null 2>&1; then
  ok "happy path maps cpu from the 1m stats record"
else
  bad "happy path maps cpu from the 1m stats record" "$(head -c 300 "$D/stdout")"
fi
if jq -e '.[0] | has("inode_used_percent") or has("container_restarts_24h") | not' \
    "$D/stdout" >/dev/null 2>&1; then
  ok "happy path invents no inode/restarts keys"
else
  bad "happy path invents no inode/restarts keys" "$(head -c 300 "$D/stdout")"
fi
if grep -qF "$FAKE_PASSWORD" "$D/data.log" && ! grep -qF "$FAKE_PASSWORD" "$D/argv.log"; then
  ok "password travels in the data file, never argv"
else
  bad "password travels in the data file, never argv" "see $D/argv.log + data.log"
fi
if ! grep -qF "$FAKE_TOKEN" "$D/argv.log"; then
  ok "token never appears in argv"
else
  bad "token never appears in argv" "see $D/argv.log"
fi

# --- §4 remote https fetches (positive control for the scheme gate) -----------
D="$(fresh_case https)"
run_tool "$D" BESZEL_HUB_URL="https://beszel.example.invalid"
if [[ "$(cat "$D/rc")" == "0" ]] \
    && jq -e 'type == "array" and length == 1' "$D/stdout" >/dev/null 2>&1; then
  ok "remote https hub fetches"
else
  bad "remote https hub fetches" "rc=$(cat "$D/rc") stderr=$(head -c 300 "$D/stderr")"
fi

# --- §4 remote http refused before any curl call ------------------------------
D="$(fresh_case remote-http)"
run_tool "$D" BESZEL_HUB_URL="http://beszel.example.invalid:8090"
if [[ "$(cat "$D/rc")" == "2" ]] && ! [[ -s "$D/stdout" ]] \
    && grep -q "must be https" "$D/stderr" && ! [[ -s "$D/argv.log" ]]; then
  ok "remote http refused with no fetch"
else
  bad "remote http refused with no fetch" "rc=$(cat "$D/rc") stderr=$(head -c 300 "$D/stderr")"
fi

# --- §3 hostile token refused, injected path never created --------------------
D="$(fresh_case hostile)"
hostile_token='x"
output = "'"$D"'/pwned"
header = "X: y'
jq -n --arg t "$hostile_token" '{token: $t}' > "$D/auth.json"
run_tool "$D" BESZEL_HUB_URL="http://127.0.0.1:8090"
if [[ "$(cat "$D/rc")" == "2" ]] && ! [[ -s "$D/stdout" ]] \
    && grep -q "unexpected characters" "$D/stderr" && ! [[ -e "$D/pwned" ]]; then
  ok "hostile token refused, injected path never created"
else
  bad "hostile token refused, injected path never created" \
      "rc=$(cat "$D/rc") stderr=$(head -c 300 "$D/stderr") pwned=$([[ -e "$D/pwned" ]] && echo yes || echo no)"
fi

# --- §5 tokenless auth refused with empty stdout ------------------------------
D="$(fresh_case no-token)"
printf '{"user": {"id": "abc"}}' > "$D/auth.json"
run_tool "$D" BESZEL_HUB_URL="http://127.0.0.1:8090"
if [[ "$(cat "$D/rc")" == "2" ]] && ! [[ -s "$D/stdout" ]] \
    && grep -q "no token" "$D/stderr"; then
  ok "tokenless auth refused with empty stdout"
else
  bad "tokenless auth refused with empty stdout" "rc=$(cat "$D/rc") stderr=$(head -c 300 "$D/stderr")"
fi

# --- §6 loopback gate parses the authority ----------------------------------
# Refused (exit 2, no fetch): each URL must die in the gate before curl.
for evil_url in \
    "http://127.0.0.1@host.example.invalid/" \
    "http://operator:TEST-password-not-a-secret@127.0.0.1:8090/" \
    "http://127.0.0.1:8090@host.example.invalid/" \
    "http://[::1]@host.example.invalid/" \
    "http://127.evil.example.invalid/" \
    "http://127.0.0.1.evil.example.invalid/" \
    "http://0x7f.0.0.1:8090/" \
    "http://2130706433/" \
    "http://[::1%25lo]:8090/" \
    "http://::1:8090/" \
    "http://192.0.2.1:8090/"; do
  D="$(fresh_case gate-refused)"
  run_tool "$D" BESZEL_HUB_URL="$evil_url"
  if [[ "$(cat "$D/rc")" == "2" ]] && ! [[ -s "$D/stdout" ]] \
      && grep -q "must be https unless the hub is loopback\|must not embed credentials" "$D/stderr" \
      && ! [[ -s "$D/argv.log" ]]; then
    ok "gate refuses $evil_url with no fetch"
  else
    bad "gate refuses $evil_url with no fetch" "rc=$(cat "$D/rc") stderr=$(head -c 300 "$D/stderr")"
  fi
done

# Accepted: localhost and bracketed [::1] still fetch over plain http.
for good_url in "http://localhost:8090" "http://[::1]:8090"; do
  D="$(fresh_case gate-accepted)"
  run_tool "$D" BESZEL_HUB_URL="$good_url"
  if [[ "$(cat "$D/rc")" == "0" ]] \
      && jq -e 'type == "array" and length == 1' "$D/stdout" >/dev/null 2>&1; then
    ok "gate accepts $good_url"
  else
    bad "gate accepts $good_url" "rc=$(cat "$D/rc") stderr=$(head -c 300 "$D/stderr")"
  fi
done

echo "fetch-beszel-normalized: $PASS passed, $FAIL failed"
[[ "$FAIL" == "0" ]]
