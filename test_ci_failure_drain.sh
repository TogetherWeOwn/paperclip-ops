#!/usr/bin/env bash
# Regression suite for ci_failure_drain.sh — the CI failure log-drain (case_821f1653).
#
# THE BUG THIS EXISTS FOR. When a CI run goes red, triage means opening every
# failed job and paging through thousands of log lines to find the one that
# names the cause. Reviewers re-run or guess instead. The drain bundles each
# failed job's trimmed log plus a one-line summary naming the job, the failed
# step, and the cause — so a red run triages from the summary in minutes.
#
# HERMETIC BY CONSTRUCTION. Recorded jobs-list fixtures plus recorded per-job
# log files, invoked via --jobs-json/--logs-dir. No network, no credential,
# nothing leaves the loopback. Requires bash, jq, curl (curl only for the
# live path, which this suite never takes).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ci_failure_drain.sh"
FIXTURES="$HERE/test/fixtures/ci_failure_drain"
RED="$FIXTURES/jobs-red.json"
GREEN="$FIXTURES/jobs-green.json"
LOGS="$FIXTURES/logs"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ONE label per assertion, printed identically green or red — the ci.yml
# mutation gate pins tests by label, so a label that changes with the outcome
# cannot be pinned by any mutation (case_ab86fd07).
assert() {
  local status="$1" label="$2" diag="${3:-}"
  if [[ "$status" -eq 0 ]]; then ok "$label"; else bad "$label"; [[ -n "$diag" ]] && printf '        %s\n' "$diag"; fi
  return 0
}

command -v jq >/dev/null 2>&1 || { echo "test_ci_failure_drain: jq is required" >&2; exit 2; }
[[ -x "$TOOL" ]] || { echo "test_ci_failure_drain: $TOOL is not executable" >&2; exit 2; }
# A missing fixture is a hard error, never a skip. A skipped test is a deleted
# test that still prints a zero exit (case_ab86fd07).
for f in "$RED" "$GREEN" "$LOGS/9002.log" "$LOGS/9003.log"; do
  [[ -s "$f" ]] || { echo "test_ci_failure_drain: recorded fixture missing or empty: $f" >&2; exit 2; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------
hdr "The red fixture — what a failing run must produce"

OUT="$("$TOOL" drain --jobs-json "$RED" --logs-dir "$LOGS" --out-dir "$TMP/drain" 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a red run still exits 0" \
  "got exit $RC — the drain must not replace the real failure's attribution"

[[ -s "$TMP/drain/failure-summary.md" ]]
assert $? "the drain writes a failure-summary.md" \
  "no summary file; there is nothing to triage from"

grep -q 'broker-suite' "$TMP/drain/failure-summary.md"
assert $? "the summary names the failing broker-suite job" \
  "the failing job is not named, so triage starts with a blind hunt"

grep -q 'capture-suite' "$TMP/drain/failure-summary.md"
assert $? "the summary names the failing capture-suite job" \
  "the second failing job is not named"

grep -q 'Broker suite' "$TMP/drain/failure-summary.md"
assert $? "the summary names the step that failed" \
  "no failed step attributed; the reader goes back to bisecting a log"

[[ -s "$TMP/drain/broker-suite.log" ]] && [[ -s "$TMP/drain/capture-suite.log" ]]
assert $? "each failed job gets its own trimmed log file" \
  "a per-job log is missing, so the bundle is a summary with no evidence"

# The one-line cause must name something that broke, not teardown noise.
grep -qi 'refuses an unknown credential source\|expected refusal' "$TMP/drain/failure-summary.md"
assert $? "the summary line carries the error-shaped cause" \
  "no cause line; the summary says which job failed but not why"

grep -q '::error title=' <<< "$OUT"
assert $? "a red drain emits a ::error annotation so it surfaces on the run summary" \
  "no ::error annotation; the drain is only visible to someone opening the artifact"

grep -q 'Offline suites' <<< "$OUT"; [[ $? -ne 0 ]]
assert $? "the drain does not name jobs that passed" \
  "a passing job appears in the failure drain, which trains reviewers to ignore it"

# ---------------------------------------------------------------------------
hdr "The green fixture — a clean run stays quiet"

OUT_G="$("$TOOL" drain --jobs-json "$GREEN" --logs-dir "$LOGS" --out-dir "$TMP/green" 2>&1)"; RC_G=$?
[[ "$RC_G" -eq 0 ]]
assert $? "a green run still exits 0" \
  "got exit $RC_G on a run with no failures"

grep -qi 'no failed jobs' "$TMP/green/failure-summary.md"
assert $? "a green run reports no failed jobs" \
  "the summary does not say the run was clean"

grep -q '::error title=' <<< "$OUT_G"; [[ $? -ne 0 ]]
assert $? "a green run raises no error annotation" \
  "an annotation on a clean run is a warning reviewers learn to scroll past"

# ---------------------------------------------------------------------------
hdr "Refusal — a drain that measured nothing must not read as clean (case_70ae2474)"

OUT_E="$("$TOOL" drain --jobs-json /dev/null --logs-dir "$LOGS" --out-dir "$TMP/empty" 2>&1)"; RC_E=$?
[[ "$RC_E" -eq 3 ]]
assert $? "an empty jobs file REFUSES" "got exit $RC_E"

printf '{"jobs":[]}' > "$TMP/nojobs.json"
OUT_N="$("$TOOL" drain --jobs-json "$TMP/nojobs.json" --logs-dir "$LOGS" --out-dir "$TMP/nojobs" 2>&1)"; RC_N=$?
[[ "$RC_N" -eq 3 ]]
assert $? "a jobs response with no jobs REFUSES" "got exit $RC_N"

# Half a fixture is a lie: jobs without logs (or logs without jobs) must not
# produce a partial bundle that reads as complete.
OUT_H="$("$TOOL" drain --jobs-json "$RED" --out-dir "$TMP/half" 2>&1)"; RC_H=$?
[[ "$RC_H" -ne 0 ]]
assert $? "jobs-json without logs-dir is a usage error, not a partial drain" "got exit $RC_H"

mkdir -p "$TMP/thinlogs"; cp "$LOGS/9002.log" "$TMP/thinlogs/"
OUT_T="$("$TOOL" drain --jobs-json "$RED" --logs-dir "$TMP/thinlogs" --out-dir "$TMP/thin" 2>&1)"; RC_T=$?
[[ "$RC_T" -eq 3 ]]
assert $? "a missing log for a failed job REFUSES" "got exit $RC_T"

# ---------------------------------------------------------------------------
hdr "Time bounds — every HTTP call carries a bound, and a hung endpoint refuses (case_e107fefc)"

# Stub curl: records argv, serves fixtures, and emulates curl's -w http-code
# contract. STUB_CURL_MODE=ok serves; =timeout emulates curl exit 28
# (operation timed out); =log-timeout serves the jobs list but times out log
# downloads. Nothing leaves the loopback: the URL is never fetched.
mkdir -p "$TMP/stubbin"
cat > "$TMP/stubbin/curl" <<'STUB'
#!/usr/bin/env bash
dest=""; url=""
printf '%s\0' "$@" >> "$CURL_ARGV_LOG"
prev=""
for a in "$@"; do
  if [[ "$prev" == "-o" ]]; then dest="$a"; prev=""; continue; fi
  case "$a" in
    -o) prev="-o" ;;
    -*) ;;
    *) url="$a" ;;
  esac
done
mode="${STUB_CURL_MODE:-ok}"
if [[ "$mode" == "timeout" ]] || { [[ "$mode" == "log-timeout" ]] && [[ "$url" != *"attempts"* ]]; }; then
  echo "curl: (28) Operation timed out after 30001 milliseconds" >&2
  printf '000'
  exit 28
fi
# case_32a28c0d: a timeout AFTER the '200' header still prints CODE 200 via -w
# while curl exits 28 with a truncated body. The helper must refuse on the
# transport status, never accept the truncated download as complete.
if [[ "$mode" == "truncated-200" ]] || { [[ "$mode" == "log-truncated-200" ]] && [[ "$url" != *"attempts"* ]]; }; then
  echo "curl: (28) Operation timed out after 30001 milliseconds" >&2
  printf '{"jobs": [{"name": "trun' > "$dest"
  printf '200'
  exit 28
fi
if [[ "$url" == *"attempts"* ]]; then
  cp "$STUB_JOBS_JSON" "$dest"
else
  printf 'fake log line\n' > "$dest"
fi
printf '200'
STUB
chmod +x "$TMP/stubbin/curl"

live_env() {
  local jobs="$1" mode="$2" argvlog="$3"; shift 3
  env -u BASH_ENV PATH="$TMP/stubbin:$PATH" \
    GITHUB_REPOSITORY="o/r" GITHUB_RUN_ID="1" GH_TOKEN="test-token-20984" \
    GITHUB_API_URL="https://api.invalid" \
    STUB_JOBS_JSON="$jobs" STUB_CURL_MODE="$mode" CURL_ARGV_LOG="$argvlog" \
    "$@"
}

: > "$TMP/argv-green.log"
live_env "$GREEN" ok "$TMP/argv-green.log" \
  "$TOOL" drain --out-dir "$TMP/live-green" >/dev/null 2>&1; RC_L=$?
[[ "$RC_L" -eq 0 ]]
assert $? "a live green drain exits 0 through the stub curl" "got exit $RC_L"
grep -q -- '--max-time' "$TMP/argv-green.log"
assert $? "the jobs-list call carries --max-time" "no --max-time in the recorded curl argv"
grep -q -- '--connect-timeout' "$TMP/argv-green.log"
assert $? "the jobs-list call carries --connect-timeout" "no --connect-timeout in the recorded curl argv"
grep -q "test-token-20984" "$TMP/argv-green.log"; [[ $? -ne 0 ]]
assert $? "the stubbed live path still keeps the token out of argv" "the token reached curl argv"

: > "$TMP/argv-red.log"
live_env "$RED" ok "$TMP/argv-red.log" \
  "$TOOL" drain --out-dir "$TMP/live-red" >/dev/null 2>&1; RC_R=$?
[[ "$RC_R" -eq 0 ]]
assert $? "a live red drain exits 0 through the stub curl" "got exit $RC_R"
N_BOUND="$(tr '\0' ' ' < "$TMP/argv-red.log" | grep -o -- '--max-time' | wc -l)"
[[ "$N_BOUND" -eq 3 ]]
assert $? "every HTTP call is bounded (1 jobs list + 2 log downloads)" "found $N_BOUND --max-time flags, want 3"
grep -q 'broker-suite' "$TMP/live-red/failure-summary.md"
assert $? "the stub-served red drain still names the failing job" "end-to-end drain broken by the bound flags"

live_env "$GREEN" timeout "$TMP/argv-timeout.log" \
  "$TOOL" drain --out-dir "$TMP/live-timeout" >"$TMP/timeout.out" 2>&1; RC_TO=$?
[[ "$RC_TO" -eq 3 ]]
assert $? "a hung jobs endpoint REFUSES (exit 3), never reads as clean" "got exit $RC_TO"
[[ ! -e "$TMP/live-timeout/failure-summary.md" ]]
assert $? "a timed-out jobs call writes no summary that could read as clean" "a summary exists over unmeasured data"
grep -q -- '--max-time' "$TMP/argv-timeout.log"
assert $? "even the timed-out call carried --max-time" "the bound was not applied to the hung call"

live_env "$RED" log-timeout "$TMP/argv-logtimeout.log" \
  "$TOOL" drain --out-dir "$TMP/live-logtimeout" >"$TMP/logtimeout.out" 2>&1; RC_LT=$?
[[ "$RC_LT" -eq 3 ]]
assert $? "a hung log download REFUSES (exit 3), never reads as drained" "got exit $RC_LT"

# case_32a28c0d: the download times out AFTER the '200' header, so -w prints 200
# with exit 28 and a truncated body. Only the transport check can refuse it.
live_env "$GREEN" truncated-200 "$TMP/argv-truncated200.log" \
  "$TOOL" drain --out-dir "$TMP/live-truncated200" >"$TMP/truncated200.out" 2>&1; RC_T2=$?
[[ "$RC_T2" -eq 3 ]]
assert $? "a '200' jobs download that times out mid-body REFUSES (exit 3)" "got exit $RC_T2"
grep -q 'transport failure' "$TMP/truncated200.out"
assert $? "the refusal names the transport failure, not the status code" "refusal does not name the transport failure"
[[ ! -e "$TMP/live-truncated200/failure-summary.md" ]]
assert $? "a truncated jobs body writes no summary that could read as clean" "a summary exists over truncated data"

live_env "$RED" log-truncated-200 "$TMP/argv-logtruncated200.log" \
  "$TOOL" drain --out-dir "$TMP/live-logtruncated200" >"$TMP/logtruncated200.out" 2>&1; RC_LT2=$?
[[ "$RC_LT2" -eq 3 ]]
assert $? "a '200' log download that times out mid-body REFUSES (exit 3)" "got exit $RC_LT2"
grep -q 'transport failure' "$TMP/logtruncated200.out"
assert $? "the log refusal names the transport failure, not the status code" "log refusal does not name the transport failure"

hdr "Result"
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[[ "$PASS" -gt 0 ]] || { printf '  a suite that ran no assertions is not a green suite\n'; exit 1; }
[[ "$FAIL" -eq 0 ]] || exit 1
