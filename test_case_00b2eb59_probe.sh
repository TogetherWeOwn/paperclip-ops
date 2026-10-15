#!/usr/bin/env bash
# test_case_00b2eb59_probe.sh — case_a2ab2937/case_3d2bbfac offline suite.
#
# No credential, no network, no host: static gates over the source-only
# probe bundle (push script + Gatus endpoint + runbook). The live round runs
# host-side per OPERATOR.md §4.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YAML="$HERE/ops/case_a2ab2937/gatus-endpoints.yaml"
PUSH="$HERE/ops/case_a2ab2937/push-staging-login-smoke.sh"
OPDOC="$HERE/ops/case_a2ab2937/OPERATOR.md"
PASS=0; FAIL=0
ok(){ echo "PASS $1"; PASS=$((PASS+1)); }
bad(){ echo "FAIL $1"; FAIL=$((FAIL+1)); }

# §1 — bundle exists and is executable.
for f in "$YAML" "$PUSH" "$OPDOC"; do
  [[ -f "$f" ]] && ok "$(basename "$f") exists" || bad "$(basename "$f") missing"
done
[[ -x "$PUSH" ]] && ok "push script executable" || bad "push script not executable"

# §2 — Gatus entry: one external endpoint, staging only, secret via env only.
[[ "$(grep -c '^  - name: ' "$YAML")" == 1 ]] \
  && ok "one gatus endpoint" || bad "endpoint count != 1"
grep -q "name: staging-login-smoke" "$YAML" && ok "endpoint name" || bad "endpoint name missing"
grep -q "group: two" "$YAML" && ok "endpoint group two" || bad "endpoint group missing"
grep -q 'interval: 10m' "$YAML" && ok "10m heartbeat" || bad "heartbeat missing"
grep -q "OPERATOR.md#staging-login-smoke" "$YAML" && ok "runbook pointer" || bad "runbook pointer missing"
grep -q "THRESHOLD 3 consecutive" "$YAML" && ok "threshold declared" || bad "threshold missing"
grep -q "OWNER on-call web" "$YAML" && ok "owner declared" || bad "owner missing"
grep -q 'GATUS_EXTERNAL_TOKEN' "$YAML" && ok "token via env reference" || bad "token reference missing"
if grep -En '://' "$YAML"; then bad "yaml carries a URL literal"; else ok "yaml has no URL literals"; fi
# No pipeline into an early-exiting grep (SIGPIPE under pipefail): capture first.
secret_hits="$(grep -Ein '\b(password|secret|bearer)\b' "$YAML" | grep -v 'GATUS_EXTERNAL_TOKEN' || true)"
if [[ -n "$secret_hits" ]] && grep -qv '^\s*#.*ONLY place' <<<"$secret_hits"; then
  : # fall through to the strict check below
fi
if grep -Ein 'QA_AUTH_TOKEN|DISCORD_WEBHOOK_URL|AKMATORI_WEBHOOK_SECRET|AKMATORI_ALERTMANAGER_URL' "$YAML"; then
  bad "yaml names a foreign secret"
else
  ok "yaml names no foreign secret"
fi

# §3 — push script: parses, staging-only, fail-closed, disclosure-safe.
bash -n "$PUSH" && ok "push script parses" || bad "push script does not parse"
grep -q 'next.togetherweown.com' "$PUSH" && ok "staging host pinned" || bad "staging host missing"
if grep -Ein 'two-web-prod|prod\.togetherweown|two-dashboard-prod|TWO_WEB_PROD' "$PUSH"; then
  bad "push script references a production endpoint"
else
  ok "staging only, never production"
fi
if grep -Fq '[[ "$BASE" == *next.togetherweown.com* ]]' "$PUSH"; then
  bad "old substring origin guard remains"
else
  ok "old substring origin guard removed"
fi
grep -q '_gatus_push.sh' "$PUSH" && ok "shared push helper sourced" || bad "shared helper missing"
grep -q 'gatus_push "$KEY" true' "$PUSH" && ok "green push path" || bad "green push missing"
grep -q 'fail_push' "$PUSH" && ok "fail-closed paths present" || bad "fail-closed missing"
if grep -En '(^|[[:space:]])-H[[:space:]]' "$PUSH"; then bad "push script passes -H on argv"; else ok "no argv header"; fi
grep -q -- '--config' "$PUSH" && ok "auth via curl config file" || bad "config-file auth missing"
grep -q 'chmod 0600' "$PUSH" && ok "0600 temp/config perms" || bad "0600 perms missing"
grep -q 'mktemp -d' "$PUSH" && ok "fresh jar dir per run" || bad "fresh jar missing"
grep -q 'max-redirs 0' "$PUSH" && ok "login POST maxRedirects 0" || bad "maxRedirects missing"
grep -q '429' "$PUSH" && ok "429 throttle path" || bad "429 path missing"
grep -q 'Retry-After' "$PUSH" && ok "Retry-After backoff" || bad "Retry-After missing"
grep -q '5000ms\|< 5.0\|5000' "$PUSH" && ok "login latency bound" || bad "login bound missing"
grep -q '3000ms\|< 3.0\|3000' "$PUSH" && ok "status/up latency bounds" || bad "status/up bounds missing"
grep -q '__Host-two_session' "$PUSH" && ok "session cookie asserted" || bad "cookie assert missing"
grep -q 'x-two-origin' "$PUSH" && ok "up origin header asserted" || bad "origin assert missing"
grep -q 'substitute another credential' "$PUSH" && ok "no-substitution rule" || bad "no-substitution note missing"
if grep -Ein '^[[:space:]]*echo.*TOKEN' "$PUSH"; then
  bad "push script can log a token"
else
  ok "push script never logs tokens"
fi

# §4 — runbook: cron, env, verify, triage, rollback.
grep -q '^###\? .*staging-login-smoke\|^## staging-login-smoke' "$OPDOC" \
  && ok "runbook anchor" || bad "runbook anchor missing"
grep -q '\*/10' "$OPDOC" && ok "10m cron pinned" || bad "cron missing"
grep -q 'QA_AUTH_TOKEN' "$OPDOC" && ok "QA token install documented" || bad "QA token docs missing"
grep -q 'chmod 0600' "$OPDOC" && ok "0600 env documented" || bad "0600 docs missing"
grep -q 'test_case_00b2eb59_probe.sh' "$OPDOC" && ok "offline verify documented" || bad "verify docs missing"
grep -q 'qa-member' "$OPDOC" && ok "session-collision triage" || bad "collision triage missing"
grep -q 'Rollback' "$OPDOC" && ok "rollback documented" || bad "rollback missing"
grep -q 'Never production\|never production\|Never commit' "$OPDOC" \
  && ok "staging-only + secrecy stated" || bad "staging/secrecy statement missing"

# §5 — execute the real script offline with synthetic secrets and a fail-closed curl.
STAGING_ORIGIN="https://next.togetherweown.com"
QA_SENTINEL="QA_SENTINEL_ONLY_TEST"
GATUS_SENTINEL="GATUS_SENTINEL_ONLY_TEST"
REAL_MKTEMP="$(command -v mktemp)"
SCRATCH_PARENT="${PAPERCLIP_RUN_SCRATCH_DIR:-${PAPERCLIP_SCRATCH_DIR:-${TMPDIR:-/tmp}}}"
TEST_ROOT=""
if [[ -d "$SCRATCH_PARENT" ]]; then
  TEST_ROOT="$("$REAL_MKTEMP" -d "${SCRATCH_PARENT%/}/case_00b2eb59-probe.XXXXXXXX" 2>/dev/null || true)"
fi
if [[ -z "$TEST_ROOT" ]]; then
  bad "behavioral test scratch directory unavailable"
else
  trap 'rm -rf "$TEST_ROOT"' EXIT
  STUB_BIN="$TEST_ROOT/bin"
  mkdir -p "$STUB_BIN"

  cat > "$STUB_BIN/curl" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
: "${CALL_LOG:?}"
: "${EXPECTED_BASE:?}"
printf 'call\n' >> "$CALL_LOG"
args=("$@")
config_file=""; header_file=""; body_file=""; target=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[i]}" in
    --config) config_file="${args[i+1]:-}" ;;
    -D) header_file="${args[i+1]:-}" ;;
    -o) body_file="${args[i+1]:-}" ;;
    http://*|https://*) target="${args[i]}" ;;
  esac
done
if [[ -z "$target" ]]; then
  # The shared helper's Gatus request carries its URL in a config file.
  [[ -n "$config_file" && -r "$config_file" ]] || { printf 'blocked\n' >&2; exit 97; }
  config_url="$(grep -m1 '^url = ' "$config_file" | cut -d '"' -f 2)"
  case "$config_url" in
    'http://127.0.0.1:8080/api/v1/endpoints/two_staging-login-smoke/external?success=true'*|\
    'http://127.0.0.1:8080/api/v1/endpoints/two_staging-login-smoke/external?success=false'*) ;;
    *) printf 'blocked\n' >&2; exit 97 ;;
  esac
  printf 'gatus\n' >> "$CALL_LOG"
  printf '204'
  exit 0
fi
printf 'url:%s\n' "$target" >> "$CALL_LOG"
case "$target" in
  "$EXPECTED_BASE/auth/qa/qa-member")
    [[ -n "$header_file" && -n "$body_file" ]] || exit 97
    printf 'HTTP/1.1 204 No Content\r\nSet-Cookie: __Host-two_session=synthetic; Path=/; Secure; HttpOnly\r\n\r\n' > "$header_file" || exit 97
    : > "$body_file" || exit 97
    printf '204 0.01'
    ;;
  "$EXPECTED_BASE/auth/status")
    [[ -n "$header_file" && -n "$body_file" ]] || exit 97
    printf 'HTTP/1.1 200 OK\r\n\r\n' > "$header_file" || exit 97
    printf '{"authenticated": true}\n' > "$body_file" || exit 97
    printf '200 0.01'
    ;;
  "$EXPECTED_BASE/up")
    [[ -n "$header_file" && -n "$body_file" ]] || exit 97
    printf 'HTTP/1.1 200 OK\r\nx-two-origin: two-web-next\r\n\r\n' > "$header_file" || exit 97
    : > "$body_file" || exit 97
    printf '200 0.01'
    ;;
  *)
    # Unexpected destinations are recorded but never sent to the network.
    printf 'blocked\n' >&2
    exit 97
    ;;
esac
SH
  cat > "$STUB_BIN/mktemp" <<'SH'
#!/usr/bin/env bash
set -uo pipefail
: "${MKTEMP_LOG:?}"
: "${REAL_MKTEMP:?}"
printf 'mktemp\n' >> "$MKTEMP_LOG"
exec "$REAL_MKTEMP" "$@"
SH
  chmod 0755 "$STUB_BIN/curl" "$STUB_BIN/mktemp"

  run_probe() {
    local script="$1" label="$2" base="$3" set_base="$4" rc=0
    local case_dir="$TEST_ROOT/$label"
    mkdir -p "$case_dir/tmp"
    : > "$case_dir/curl.log"; : > "$case_dir/mktemp.log"
    local -a clean_env=(
      "PATH=$STUB_BIN:/usr/bin:/bin"
      "TMPDIR=$case_dir/tmp"
      "CALL_LOG=$case_dir/curl.log"
      "MKTEMP_LOG=$case_dir/mktemp.log"
      "REAL_MKTEMP=$REAL_MKTEMP"
      "EXPECTED_BASE=$STAGING_ORIGIN"
      "QA_AUTH_TOKEN=$QA_SENTINEL"
      "GATUS_URL=http://127.0.0.1:8080"
      "GATUS_EXTERNAL_TOKEN=$GATUS_SENTINEL"
    )
    [[ "$set_base" == 1 ]] && clean_env+=("STAGING_BASE_URL=$base")
    if env -i "${clean_env[@]}" bash "$script" > "$case_dir/output" 2>&1; then
      rc=0
    else
      rc=$?
    fi
    PROBE_RC="$rc"
    PROBE_DIR="$case_dir"
  }

  refuses_before_io() {
    local script="$1" label="$2" base="$3" set_base="$4"
    run_probe "$script" "$label" "$base" "$set_base"
    [[ "$PROBE_RC" == 2 ]] || return 1
    [[ ! -s "$PROBE_DIR/curl.log" && ! -s "$PROBE_DIR/mktemp.log" ]] || return 1
    [[ -z "$(find "$PROBE_DIR/tmp" -mindepth 1 -print -quit)" ]] || return 1
    ! grep -Fq "$QA_SENTINEL" "$PROBE_DIR/output" || return 1
    ! grep -Fq "$GATUS_SENTINEL" "$PROBE_DIR/output" || return 1
  }

  reaches_canonical_staging() {
    local script="$1" label="$2" base="$3" set_base="$4"
    run_probe "$script" "$label" "$base" "$set_base"
    [[ "$PROBE_RC" == 0 ]] || return 1
    [[ "$(grep -c '^call$' "$PROBE_DIR/curl.log")" == 4 ]] || return 1
    [[ "$(grep -Fxc "url:$STAGING_ORIGIN/auth/qa/qa-member" "$PROBE_DIR/curl.log")" == 1 ]] || return 1
    [[ "$(grep -Fxc "url:$STAGING_ORIGIN/auth/status" "$PROBE_DIR/curl.log")" == 1 ]] || return 1
    [[ "$(grep -Fxc "url:$STAGING_ORIGIN/up" "$PROBE_DIR/curl.log")" == 1 ]] || return 1
    [[ "$(grep -Fxc 'gatus' "$PROBE_DIR/curl.log")" == 1 ]] || return 1
    [[ -z "$(find "$PROBE_DIR/tmp" -mindepth 1 -print -quit)" ]] || return 1
    ! grep -Fq "$QA_SENTINEL" "$PROBE_DIR/output" || return 1
    ! grep -Fq "$GATUS_SENTINEL" "$PROBE_DIR/output" || return 1
  }

  invalid_bases=(
    'https://next.togetherweown.com.attacker.example'
    'https://evil.next.togetherweown.com'
    'https://person-5fd0919e@example.invalid'
    'http://next.togetherweown.com'
    'https://next.togetherweown.com:443'
    'https://next.togetherweown.com:8443'
    'https://attacker.example/next.togetherweown.com'
    'https://attacker.example/?next=next.togetherweown.com'
    'https://next.togetherweown.com/path'
    'https://next.togetherweown.com/?x=1'
    'https://next.togetherweown.com/#fragment'
    ' https://next.togetherweown.com'
    'https://next.togetherweown.com '
    $'https://next.togetherweown.com\t'
    $'https://next.togetherweown.com\n'
    'https://next.togetherweown.com//'
    'HTTPS://next.togetherweown.com'
    'https://'
  )
  invalid_count=0
  for base in "${invalid_bases[@]}"; do
    invalid_count=$((invalid_count + 1))
    if refuses_before_io "$PUSH" "invalid-$invalid_count" "$base" 1; then
      ok "invalid origin $invalid_count refuses before curl/config creation"
    else
      bad "invalid origin $invalid_count was not refused before curl/config creation"
    fi
  done

  if reaches_canonical_staging "$PUSH" "positive-default" '' 0; then
    ok "unset base reaches the exact default staging URLs"
  else
    bad "unset base did not reach the exact default staging URLs"
  fi
  if reaches_canonical_staging "$PUSH" "positive-explicit" "$STAGING_ORIGIN" 1; then
    ok "explicit staging origin reaches the exact staging URLs"
  else
    bad "explicit staging origin did not reach the exact staging URLs"
  fi
  if reaches_canonical_staging "$PUSH" "positive-trailing-slash" "$STAGING_ORIGIN/" 1; then
    ok "one trailing slash normalizes to the exact staging origin"
  else
    bad "one trailing slash did not normalize to the exact staging origin"
  fi
  if reaches_canonical_staging "$PUSH" "positive-empty-default" '' 1; then
    ok "empty base preserves the default staging origin"
  else
    bad "empty base did not preserve the default staging origin"
  fi

  # Restore the old substring guard in a throwaway copy; the behavioral check above
  # must reject a foreign path that merely contains the staging hostname.
  MUTANT_ROOT="$TEST_ROOT/mutant"
  MUTANT="$MUTANT_ROOT/ops/case_a2ab2937/push-staging-login-smoke.sh"
  mkdir -p "$MUTANT_ROOT/ops/case_a2ab2937" "$MUTANT_ROOT/ops/case_658bf6b9"
  cp "$PUSH" "$MUTANT"
  cp "$HERE/ops/case_658bf6b9/_gatus_push.sh" "$MUTANT_ROOT/ops/case_658bf6b9/_gatus_push.sh"
  if python3 - "$MUTANT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
source = path.read_text()
new_guard = '''case "$BASE" in
  "$STAGING_ORIGIN"|"$STAGING_ORIGIN"/) BASE="$STAGING_ORIGIN" ;;
  *)
    echo "push-staging-login-smoke: refusing — STAGING_BASE_URL is not an allowed staging origin" >&2
    exit 2
    ;;
esac'''
old_guard = '''[[ "$BASE" == *next.togetherweown.com* ]] \\
  || { echo "push-staging-login-smoke: refusing — STAGING_BASE_URL is not the staging host" >&2; exit 2; }'''
if source.count(new_guard) == 1:
    source = source.replace(new_guard, old_guard, 1)
elif source.count(old_guard) != 1:
    raise SystemExit("could not construct the old-guard mutation")
path.write_text(source)
PY
  then
    if refuses_before_io "$MUTANT" "old-guard-mutant" 'https://attacker.example/next.togetherweown.com' 1; then
      bad "old substring guard survived the behavioral regression"
    else
      ok "old substring guard is killed by the offline behavioral regression"
    fi
  else
    bad "could not restore the old substring guard in a throwaway copy"
  fi
fi

echo "--- $PASS pass, $FAIL fail ---"
[[ "$FAIL" == 0 ]]
