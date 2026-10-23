#!/usr/bin/env bash
# Acceptance suite for the ci_failure_drain JSON failure summary.
#
# THE CONTRACT THIS PINS. ci_failure_drain.sh drains each failed job's log
# into failure-summary.md prose plus one ::error line, so a triage agent that
# needs WHICH step failed and the first error line per job must scrape the md.
# The --json flag adds a failure-summary.json envelope beside the md:
#
#   { "schema_version": 1, "run_id": "<run>",
#     "status": "drained" | "no_failures" | "refused",
#     "jobs": [ { "id": 9002, "name": "broker-suite",
#                 "failed_step": "Broker suite",
#                 "cause": "<first error-shaped line, 300-char rule>",
#                 "log_file": "broker-suite.log" } ],
#     "summary": { "total_jobs": 3, "failed_jobs": 2, "drained_jobs": 2 },
#     "error": { "code": "ok" | "refused_jobs_unobservable" | "refused_log_missing",
#                "retryable": false, "next_action": "<token>",
#                "unmeasured": null | "<which input was unmeasured>" } }
#
# Success rows carry stable Actions job ids, the failed-step name, the cause
# byte-identical to the md line (same 300-char trim, same timestamp strip),
# and the trimmed-log filename. Refusals still exit 3 and record WHICH input
# went unmeasured in the JSON instead of printing "no failures found". The md
# bytes, the ::error line and stdout are byte-identical with --json on or off.
#
# HERMETIC BY CONSTRUCTION. Recorded jobs-list fixtures plus recorded per-job
# log files, invoked via --jobs-json/--logs-dir. No network, no credential,
# nothing leaves the loopback; a refusing curl stub proves zero HTTP calls.
# Requires bash, jq, curl (curl only for the live path, never taken here).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ci_failure_drain.sh"
FIXTURES="$HERE/test/fixtures/ci_failure_drain"
RED="$FIXTURES/jobs-red.json"
GREEN="$FIXTURES/jobs-green.json"
SINGLE="$FIXTURES/jobs-single.json"
LONGCAUSE="$FIXTURES/jobs-longcause.json"
LOGS="$FIXTURES/logs"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ONE label per assertion, printed identically green or red — the ci.yml
# mutation gate pins tests by label, so a label that changes with the outcome
# cannot be pinned by any mutation.
assert() {
  local status="$1" label="$2" diag="${3:-}"
  if [[ "$status" -eq 0 ]]; then ok "$label"; else bad "$label"; [[ -n "$diag" ]] && printf '        %s\n' "$diag"; fi
  return 0
}

command -v jq >/dev/null 2>&1 || { echo "test_ci_failure_drain_json: jq is required" >&2; exit 2; }
[[ -x "$TOOL" ]] || { echo "test_ci_failure_drain_json: $TOOL is not executable" >&2; exit 2; }
# A missing fixture is a hard error, never a skip. A skipped test is a deleted
# test that still prints a zero exit.
for f in "$RED" "$GREEN" "$SINGLE" "$LONGCAUSE" \
         "$LOGS/9002.log" "$LOGS/9003.log" "$LOGS/9004.log" "$LOGS/9005.log"; do
  [[ -s "$f" ]] || { echo "test_ci_failure_drain_json: recorded fixture missing or empty: $f" >&2; exit 2; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------
hdr "The multi-failure envelope — what a red run must parse as"

"$TOOL" drain --jobs-json "$RED" --logs-dir "$LOGS" --out-dir "$TMP/drain-json" --json \
  >"$TMP/json.out" 2>&1; RC_J=$?
[[ "$RC_J" -eq 0 ]]
assert $? "a multi-failure json drain still exits 0" \
  "got exit $RC_J — the drain must not replace the real failure's attribution"

JSON="$TMP/drain-json/failure-summary.json"
[[ -s "$JSON" ]]
assert $? "the json drain writes a failure-summary.json beside the md" \
  "no json envelope; triage still scrapes prose"

jq -e . "$JSON" >/dev/null 2>&1
assert $? "the envelope is valid json" \
  "failure-summary.json does not parse"

jq -e '.schema_version == 1' "$JSON" >/dev/null 2>&1
assert $? "the envelope carries schema_version 1" \
  "no pinned schema version; consumers cannot detect drift"

jq -e '.run_id == "fixture"' "$JSON" >/dev/null 2>&1
assert $? "the hermetic envelope carries run_id fixture" \
  "run attribution is missing or wrong"

jq -e '.status == "drained"' "$JSON" >/dev/null 2>&1
assert $? "the envelope status is drained on a red run" \
  "status is not drained; the verdict is not machine-readable"

jq -e '[.jobs[].id] == [9002,9003]' "$JSON" >/dev/null 2>&1
assert $? "the envelope lists every failed job by stable id" \
  "job rows are missing, unordered, or keyed by something unstable"

jq -e '[.jobs[].failed_step] == ["Broker suite","Capture acceptance"]' "$JSON" >/dev/null 2>&1
assert $? "each row names the step that failed" \
  "no failed step attributed; the reader goes back to bisecting a log"

# The md is the source of truth for the cause rule: the 9002 log line is
# TAP-indented, so the cause keeps its two leading spaces.
jq -e '.jobs[0].cause == "  error: expected refusal, got issued token"' "$JSON" >/dev/null 2>&1
assert $? "the 9002 row carries the exact md cause" \
  "cause drifted from the md line; json and md would triage differently"

jq -e '.jobs[1].cause == "::error::capture evidence bundle is missing the render step"' "$JSON" >/dev/null 2>&1
assert $? "the 9003 row carries the exact md cause" \
  "cause drifted from the md line; json and md would triage differently"

C0="$(jq -r '.jobs[0].cause // empty' "$JSON" 2>/dev/null)"
C1="$(jq -r '.jobs[1].cause // empty' "$JSON" 2>/dev/null)"
[[ -n "$C0" && -n "$C1" ]] \
  && grep -qF -- "$C0" "$TMP/drain-json/failure-summary.md" \
  && grep -qF -- "$C1" "$TMP/drain-json/failure-summary.md"
assert $? "every json cause is byte-identical to the md cause" \
  "json carries cause text the md does not, or trims by a different rule"

jq -e '[.jobs[].log_file] == ["broker-suite.log","capture-suite.log"]' "$JSON" >/dev/null 2>&1
assert $? "each row names its trimmed log file" \
  "the evidence pointer is missing; a row without a log is a claim without proof"

jq -e '.summary.total_jobs == 3 and .summary.failed_jobs == 2 and .summary.drained_jobs == 2' "$JSON" >/dev/null 2>&1
assert $? "summary counts pin total, failed and drained" \
  "counts are wrong; an agent cannot tell a partial drain from a full one"

jq -e '.error.code == "ok" and .error.retryable == false and .error.next_action == "triage_failures"' "$JSON" >/dev/null 2>&1
assert $? "a drained run reports error ok, not retryable, triage next" \
  "the error triple is missing or wrong; the next step is not machine-readable"

jq -e 'has("refusal") | not' "$JSON" >/dev/null 2>&1
assert $? "a drained envelope carries no refusal record" \
  "a success envelope that also refuses trains consumers to ignore refusals"

[[ -s "$JSON" ]] && ! grep -qi 'no fail' "$JSON"
assert $? "the json holds no no-failures prose on a red run" \
  "a red envelope that reads clean is worse than no envelope"

GITHUB_RUN_ID=424242 "$TOOL" drain --jobs-json "$RED" --logs-dir "$LOGS" \
  --out-dir "$TMP/drain-runid" --json >"$TMP/runid.out" 2>&1; RC_RID=$?
[[ "$RC_RID" -eq 0 ]] && jq -e '.run_id == "424242"' "$TMP/drain-runid/failure-summary.json" >/dev/null 2>&1
assert $? "GITHUB_RUN_ID flows into run_id" \
  "got exit $RC_RID or run_id did not follow the run"

# ---------------------------------------------------------------------------
hdr "Single and no failures — the envelope at the edges"

"$TOOL" drain --jobs-json "$SINGLE" --logs-dir "$LOGS" --out-dir "$TMP/drain-single" --json \
  >"$TMP/single.out" 2>&1; RC_S=$?
[[ "$RC_S" -eq 0 ]]
assert $? "a single-failure json drain exits 0" "got exit $RC_S"

SJSON="$TMP/drain-single/failure-summary.json"
jq -e '.status == "drained" and [.jobs[].id] == [9004] and .jobs[0].failed_step == "Single step" and .jobs[0].cause == "AssertionError: single thing broke" and .jobs[0].log_file == "single-suite.log"' "$SJSON" >/dev/null 2>&1
assert $? "the single row pins id, step, cause and log" \
  "the one failing job is misattributed; a single failure is the common case"

jq -e '.summary.total_jobs == 2 and .summary.failed_jobs == 1 and .summary.drained_jobs == 1' "$SJSON" >/dev/null 2>&1
assert $? "single-failure counts pin one of two" \
  "counts are wrong on the smallest red run"

"$TOOL" drain --jobs-json "$GREEN" --logs-dir "$LOGS" --out-dir "$TMP/drain-green" --json \
  >"$TMP/green.out" 2>&1; RC_G=$?
[[ "$RC_G" -eq 0 ]]
assert $? "a green json drain exits 0" "got exit $RC_G"

GJSON="$TMP/drain-green/failure-summary.json"
jq -e '.status == "no_failures" and .jobs == []' "$GJSON" >/dev/null 2>&1
assert $? "a green envelope reports no_failures with no rows" \
  "a clean run must parse as clean, not as an empty drain"

jq -e '.summary.total_jobs == 2 and .summary.failed_jobs == 0 and .summary.drained_jobs == 0' "$GJSON" >/dev/null 2>&1
assert $? "a green envelope counts zero failures" \
  "counts are wrong on a clean run"

jq -e '.error.code == "ok" and .error.retryable == false and .error.next_action == "none"' "$GJSON" >/dev/null 2>&1
assert $? "a green run reports error ok with next_action none" \
  "a clean run that asks for triage trains agents to ignore next_action"

jq -e 'has("refusal") | not' "$GJSON" >/dev/null 2>&1
assert $? "a green envelope carries no refusal record" \
  "a clean run must not refuse anything"

[[ -s "$TMP/drain-green/failure-summary.md" ]] \
  && grep -qi 'no failed jobs' "$TMP/drain-green/failure-summary.md" \
  && ! grep -q '::error title=' "$TMP/green.out"
assert $? "a green json drain stays quiet in md and annotations" \
  "the json flag changed the quiet path"

# ---------------------------------------------------------------------------
hdr "The cause rule — 300 chars, timestamp stripped, same as the md"

"$TOOL" drain --jobs-json "$LONGCAUSE" --logs-dir "$LOGS" --out-dir "$TMP/drain-long" --json \
  >"$TMP/long.out" 2>&1; RC_L=$?
[[ "$RC_L" -eq 0 ]]
assert $? "a long-cause json drain exits 0" "got exit $RC_L"

LJSON="$TMP/drain-long/failure-summary.json"
jq -e '.jobs[0].cause | length == 300' "$LJSON" >/dev/null 2>&1
assert $? "a 500-char error line truncates to exactly 300 chars" \
  "the json trims by a different rule than the md; unbounded causes bloat triage"

LONG_EXPECTED="error: $(printf 'A%.0s' {1..293})"
[[ "$(jq -r '.jobs[0].cause // empty' "$LJSON" 2>/dev/null)" == "$LONG_EXPECTED" ]]
assert $? "the truncated cause is the head of the source line" \
  "truncation took the wrong 300 chars; the head names the breakage"

[[ -s "$JSON" ]] && CAUSES="$(jq -r '.jobs[].cause' "$JSON" 2>/dev/null)" \
  && ! grep -qE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z' <<<"$CAUSES"
assert $? "causes carry no Actions timestamp prefix" \
  "the timestamp strip was skipped; every cause doubles in width"

# ---------------------------------------------------------------------------
hdr "Refusals — unmeasured input is recorded, never read as clean"

"$TOOL" drain --jobs-json /dev/null --logs-dir "$LOGS" --out-dir "$TMP/ref-empty" --json \
  >"$TMP/ref-empty.out" 2>&1; RC_E=$?
[[ "$RC_E" -eq 3 ]]
assert $? "an empty jobs file with --json REFUSES" "got exit $RC_E"

EJSON="$TMP/ref-empty/failure-summary.json"
[[ -s "$EJSON" ]] && jq -e '.status == "refused"' "$EJSON" >/dev/null 2>&1
assert $? "the jobs refusal is recorded in json with status refused" \
  "no refusal envelope; the unmeasured run leaves no parseable trace"

jq -e '.error.code == "refused_jobs_unobservable"' "$EJSON" >/dev/null 2>&1
assert $? "the refusal code names the unobservable jobs list" \
  "generic refusal code; the agent cannot tell what to fix"

jq -e '.error.unmeasured | type == "string" and contains("jobs")' "$EJSON" >/dev/null 2>&1
assert $? "the refusal names the jobs input as unmeasured" \
  "unmeasured names nothing; triage cannot tell jobs from logs"

printf '{"jobs":[]}' > "$TMP/nojobs.json"
"$TOOL" drain --jobs-json "$TMP/nojobs.json" --logs-dir "$LOGS" --out-dir "$TMP/ref-nojobs" --json \
  >"$TMP/ref-nojobs.out" 2>&1; RC_N=$?
[[ "$RC_N" -eq 3 ]] && jq -e '.status == "refused"' "$TMP/ref-nojobs/failure-summary.json" >/dev/null 2>&1
assert $? "an empty job list with --json REFUSES with refusal json" "got exit $RC_N"

mkdir -p "$TMP/thinlogs"; cp "$LOGS/9002.log" "$TMP/thinlogs/"
"$TOOL" drain --jobs-json "$RED" --logs-dir "$TMP/thinlogs" --out-dir "$TMP/ref-thin" --json \
  >"$TMP/ref-thin.out" 2>&1; RC_T=$?
[[ "$RC_T" -eq 3 ]]
assert $? "a missing log with --json REFUSES" "got exit $RC_T"

TJSON="$TMP/ref-thin/failure-summary.json"
jq -e '.status == "refused" and .error.code == "refused_log_missing"' "$TJSON" >/dev/null 2>&1
assert $? "the missing-log refusal records status and code" \
  "the failed job with no log leaves no parseable refusal"

jq -e '.error.unmeasured | type == "string" and contains("9003")' "$TJSON" >/dev/null 2>&1
assert $? "the missing-log refusal names the unmeasured job id" \
  "unmeasured does not name 9003; the agent cannot tell which log to fetch"

[[ -s "$EJSON" && -s "$TJSON" ]] && ! grep -qil 'no fail' "$EJSON" "$TJSON"
assert $? "refusal envelopes never read as no failures" \
  "a refusal that reads clean is the silent lie this gate exists to forbid"

jq -e '.jobs == [] and .summary.drained_jobs == 0' "$EJSON" >/dev/null 2>&1 \
  && jq -e '.jobs == [] and .summary.drained_jobs == 0' "$TJSON" >/dev/null 2>&1
assert $? "refusal rows are empty and drained is zero" \
  "partial rows on a refusal read as a complete drain"

[[ ! -e "$TMP/ref-empty/failure-summary.md" ]]
assert $? "an unmeasured jobs list writes no md that could read as clean" \
  "a summary exists over unmeasured data"

"$TOOL" drain --jobs-json "$RED" --logs-dir "$TMP/thinlogs" --out-dir "$TMP/ref-thin-plain" \
  >"$TMP/ref-thin-plain.out" 2>&1; RC_TP=$?
[[ "$RC_TP" -eq 3 ]] && cmp -s "$TMP/ref-thin-plain/failure-summary.md" "$TMP/ref-thin/failure-summary.md"
assert $? "a missing-log refusal keeps md bytes identical to the no-json path" \
  "got exit $RC_TP or the json flag rewrote the refusal md"

# ---------------------------------------------------------------------------
hdr "Byte stability — the json flag adds one file and changes nothing else"

"$TOOL" drain --jobs-json "$RED" --logs-dir "$LOGS" --out-dir "$TMP/drain-plain" \
  >"$TMP/plain.out" 2>&1; RC_P=$?
[[ "$RC_P" -eq 0 ]]
assert $? "the no-json red drain still exits 0" "got exit $RC_P"

cmp -s "$TMP/drain-plain/failure-summary.md" "$TMP/drain-json/failure-summary.md"
assert $? "the md is byte-identical with --json on and off" \
  "the json flag rewrote the prose; two triage surfaces now disagree"

cmp -s "$TMP/plain.out" "$TMP/json.out"
assert $? "stdout is byte-identical with --json on and off" \
  "the json flag changed stdout; scrapers and humans see different runs"

grep '^::error' "$TMP/plain.out" >"$TMP/err-plain.txt" 2>/dev/null
grep '^::error' "$TMP/json.out" >"$TMP/err-json.txt" 2>/dev/null
[[ -s "$TMP/err-plain.txt" ]] && cmp -s "$TMP/err-plain.txt" "$TMP/err-json.txt"
assert $? "the ::error line is byte-identical with --json on and off" \
  "the annotation drifted; the run summary names different failures"

diff -r --exclude=failure-summary.json "$TMP/drain-plain" "$TMP/drain-json" >/dev/null 2>&1
assert $? "the json flag adds one file and changes no other byte" \
  "out-dir contents differ beyond the added envelope"

[[ ! -e "$TMP/drain-plain/failure-summary.json" ]]
assert $? "without --json no failure-summary.json is written" \
  "the default path grew a side effect; opt-in is no longer opt-in"

# ---------------------------------------------------------------------------
hdr "Flags, docs, hermeticity — refused loudly, served offline"

"$TOOL" help >"$TMP/help.txt" 2>&1
grep -q -- '--json' "$TMP/help.txt" && grep -q 'failure-summary.json' "$TMP/help.txt"
assert $? "help documents --json and failure-summary.json" \
  "the flag exists nowhere an agent looks"

"$TOOL" drain --json --jobs-json "$RED" --logs-dir "$LOGS" --out-dir "$TMP/drain-first" \
  >"$TMP/first.out" 2>&1; RC_F=$?
[[ "$RC_F" -eq 0 && -s "$TMP/drain-first/failure-summary.json" ]]
assert $? "--json works in any flag position" "got exit $RC_F"

"$TOOL" drain --jobs-json "$RED" --logs-dir "$LOGS" --out-dir "$TMP/drain-bogus" --json --bogus-flag \
  >"$TMP/bogus.out" 2>&1; RC_B=$?
[[ "$RC_B" -eq 2 ]]
assert $? "an unknown flag with --json refuses with exit 2" "got exit $RC_B"

"$TOOL" drain --jobs-json "$RED" --out-dir "$TMP/drain-half" --json \
  >"$TMP/half.out" 2>&1; RC_H=$?
[[ "$RC_H" -eq 2 ]]
assert $? "half a fixture with --json is a usage error" "got exit $RC_H"

# A curl that refuses the network: if the hermetic path dials out, the call is
# recorded and fails loudly instead of hanging or leaking.
mkdir -p "$TMP/stubbin"
cat > "$TMP/stubbin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' "$@" >> "$CURL_ARGV_LOG"
echo "stub curl: network is refused in hermetic tests" >&2
exit 99
STUB
chmod +x "$TMP/stubbin/curl"
: > "$TMP/argv-json.log"
env -u BASH_ENV PATH="$TMP/stubbin:$PATH" CURL_ARGV_LOG="$TMP/argv-json.log" \
  "$TOOL" drain --jobs-json "$RED" --logs-dir "$LOGS" --out-dir "$TMP/drain-stub" --json \
  >"$TMP/stub.out" 2>&1; RC_ST=$?
[[ "$RC_ST" -eq 0 ]]
assert $? "the hermetic json path drains with network refused" "got exit $RC_ST"

[[ ! -s "$TMP/argv-json.log" ]]
assert $? "the hermetic json path makes zero network calls" \
  "curl was invoked; the hermetic path dialed out"

# ---------------------------------------------------------------------------
hdr "Optional JSON publication — a failed write must not destroy prior evidence"

# Catch direct truncation, unchecked tool failures, leaked staging files and mv's
# directory-destination trap. Only envelope generation / publication is faulted:
# jobs classification, log handling and header-file creation use the real tools.
# The same matrix covers both refusal call sites as well as red and green runs.
REAL_JQ="$(command -v jq)"
REAL_MKTEMP="$(command -v mktemp)"
REAL_MV="$(command -v mv)"
mkdir -p "$TMP/publication-bin"
cat > "$TMP/publication-bin/io-tool" <<'STUB'
#!/usr/bin/env bash
case "${0##*/}" in
  jq)
    envelope=0; null_input=0; prev=""
    for arg in "$@"; do
      [[ "$arg" == "-n" ]] && null_input=1
      [[ "$prev" == "--argjson" && "$arg" == "schema_version" ]] && envelope=1
      prev="$arg"
    done
    if [[ "$null_input" -eq 1 && "$envelope" -eq 1 && "$PUBLICATION_FAULT" == "jq" ]]; then
      printf '{"partial":'
      exit 7
    fi
    exec "$REAL_JQ" "$@"
    ;;
  mktemp)
    for arg in "$@"; do
      case "$arg" in
        "$PUBLICATION_DIR"|"$PUBLICATION_DIR/"*|"--tmpdir=$PUBLICATION_DIR")
          [[ "$PUBLICATION_FAULT" == "mktemp" ]] && exit 7
          ;;
      esac
    done
    exec "$REAL_MKTEMP" "$@"
    ;;
  mv)
    if [[ "${@: -1}" == "$PUBLICATION_DIR/failure-summary.json" ]]; then
      source="${@: -2:1}"
      # Observe the actual staging file, not a private filename convention.
      if [[ "$source" != "$PUBLICATION_DIR/failure-summary.json" && -f "$source" ]] \
          && [[ "$(dirname "$source")" -ef "$PUBLICATION_DIR" ]] \
          && "$REAL_JQ" -e . "$source" >/dev/null 2>&1; then
        printf 'valid sibling\n' >> "$PUBLICATION_RENAME_LOG"
      else
        printf 'unsafe staging\n' >> "$PUBLICATION_RENAME_LOG"
      fi
      [[ "$PUBLICATION_FAULT" == "mv" ]] && exit 7
    fi
    exec "$REAL_MV" "$@"
    ;;
esac
exit 98
STUB
chmod +x "$TMP/publication-bin/io-tool"
for tool in jq mktemp mv; do ln -s io-tool "$TMP/publication-bin/$tool"; done
# Reuse the refusing curl; even an accidental fallback cannot reach a live API.
ln -s "$TMP/stubbin/curl" "$TMP/publication-bin/curl"

publication_env() {
  env -u BASH_ENV PATH="$TMP/publication-bin:$PATH" \
    REAL_JQ="$REAL_JQ" REAL_MKTEMP="$REAL_MKTEMP" REAL_MV="$REAL_MV" \
    PUBLICATION_DIR="$PUB_OUT" PUBLICATION_FAULT="$PUB_FAULT" \
    PUBLICATION_RENAME_LOG="$PUB_CASE/rename.log" \
    CURL_ARGV_LOG="$PUB_CASE/curl.log" TMPDIR="$PUB_CASE/tmpfiles" \
    GITHUB_RUN_ID=727272 GH_TOKEN="synthetic-publication-token-not-a-secret" "$@"
}

publication_case() {
  local scenario="$1" fault="$2" prior="$3" jobs logs expected_rc status code
  case "$scenario" in
    drained) jobs="$RED"; logs="$LOGS"; expected_rc=0; status=drained; code=ok ;;
    no_failures) jobs="$GREEN"; logs="$LOGS"; expected_rc=0; status=no_failures; code=ok ;;
    refused_jobs) jobs=/dev/null; logs="$LOGS"; expected_rc=3; status=refused; code=refused_jobs_unobservable ;;
    refused_log) jobs="$RED"; logs="$TMP/thinlogs"; expected_rc=3; status=refused; code=refused_log_missing ;;
  esac
  local label="publication $scenario $fault $prior"
  PUB_CASE="$TMP/publication-$scenario-$fault-$prior"
  PUB_OUT="$PUB_CASE/out"
  PUB_FAULT="$fault"
  mkdir -p "$PUB_OUT" "$PUB_CASE/tmpfiles"
  : > "$PUB_CASE/curl.log"
  : > "$PUB_CASE/rename.log"

  # Capture the no-json behavior at the SAME output path: stdout contains paths.
  # Keep prose/logs in place so the second invocation also tests safe overwrite.
  publication_env "$TOOL" drain --jobs-json "$jobs" --logs-dir "$logs" --out-dir "$PUB_OUT" \
    >"$PUB_CASE/plain.stdout" 2>"$PUB_CASE/plain.stderr"; local plain_rc=$?
  local control_rc=1
  [[ "$plain_rc" -eq "$expected_rc" && ! -e "$PUB_OUT/failure-summary.json" ]] && control_rc=0
  assert "$control_rc" "$label no-json control retains its exit and has no JSON side effect" "got exit $plain_rc"
  cp -a "$PUB_OUT" "$PUB_CASE/control"
  local final="$PUB_OUT/failure-summary.json"
  case "$prior" in
    prior)
      printf '%s\n' '{"schema_version":1,"run_id":"previous","status":"no_failures","jobs":[],"summary":{"total_jobs":1,"failed_jobs":0,"drained_jobs":0},"error":{"code":"ok","retryable":false,"next_action":"none","unmeasured":null}}' > "$final"
      cp "$final" "$PUB_CASE/previous.json"
      ;;
    directory) mkdir "$final" ;;
  esac
  PUB_FAULT="$fault"
  publication_env "$TOOL" drain --jobs-json "$jobs" --logs-dir "$logs" --out-dir "$PUB_OUT" --json \
    >"$PUB_CASE/json.stdout" 2>"$PUB_CASE/json.stderr"; local json_rc=$?
  assert "$((json_rc != expected_rc))" "$label preserves the drain exit" "got exit $json_rc, want $expected_rc"
  cmp -s "$PUB_CASE/plain.stdout" "$PUB_CASE/json.stdout"
  assert $? "$label preserves stdout including annotations"
  diff -r --exclude=failure-summary.json "$PUB_CASE/control" "$PUB_OUT" >/dev/null 2>&1
  assert $? "$label preserves Markdown and log bytes with no extra output files"
  local leftovers
  leftovers="$(find "$PUB_CASE/tmpfiles" -mindepth 1 -print)"
  [[ -z "$leftovers" ]]
  assert $? "$label leaves no temporary output outside the bundle" "$leftovers"

  if [[ "$fault" == "success" ]]; then
    jq -e --arg status "$status" --arg code "$code" \
      '.schema_version == 1 and .run_id == "727272" and .status == $status and .error.code == $code' \
      "$final" >/dev/null 2>&1
    assert $? "$label replaces stale evidence with a valid current envelope"
    grep -q '^valid sibling$' "$PUB_CASE/rename.log" \
      && ! grep -q '^unsafe staging$' "$PUB_CASE/rename.log"
    assert $? "$label publishes valid JSON by renaming a same-directory temporary file"
    cmp -s "$PUB_CASE/plain.stderr" "$PUB_CASE/json.stderr"
    assert $? "$label adds no spurious stderr warning"
  else
    case "$prior" in
      prior)
        cmp -s "$PUB_CASE/previous.json" "$final"
        assert $? "$label preserves the previous valid summary byte for byte"
        ;;
      absent)
        [[ ! -e "$final" && ! -L "$final" ]]
        assert $? "$label leaves the final path absent rather than partial or empty"
        ;;
      directory)
        leftovers="$(find "$final" -mindepth 1 -print 2>/dev/null)"
        [[ -d "$final" && -z "$leftovers" ]]
        assert $? "$label never moves a staging file inside the final-path directory" "$leftovers"
        ;;
    esac
    # Stubs emit no stderr. Remove the existing refusal before checking for a
    # NEW warning: "REFUSED: jobs JSON..." is not a publication diagnostic.
    # Word boundaries also prevent ci_failure_drain.sh / failure-summary.json
    # in a shell redirect error from masquerading as a warning. No fixed prose.
    grep -vxF -f "$PUB_CASE/plain.stderr" "$PUB_CASE/json.stderr" > "$PUB_CASE/added.stderr"
    grep -iE '(^|[^[:alnum:]_/-])(warn(ing)?|failed|failure|cannot|unable)([^[:alnum:]_/-]|$)' \
      "$PUB_CASE/added.stderr" > "$PUB_CASE/warnings.stderr"
    grep -qi 'json' "$PUB_CASE/warnings.stderr"
    assert $? "$label warns on stderr that optional JSON publication failed"
  fi
  if [[ "$expected_rc" -eq 3 ]]; then
    grep -qF REFUSED "$PUB_CASE/json.stderr"
    assert $? "$label still reports REFUSED for the unobservable input"
  fi
  ! grep -qF 'synthetic-publication-token-not-a-secret' \
    "$PUB_CASE/json.stdout" "$PUB_CASE/json.stderr"
  assert $? "$label keeps the credential out of stdout and stderr"
  [[ ! -s "$PUB_CASE/curl.log" ]]
  assert $? "$label makes zero network calls"
}

for scenario in drained no_failures refused_jobs refused_log; do
  publication_case "$scenario" success prior
  for fault in jq mktemp mv; do
    for prior in prior absent; do publication_case "$scenario" "$fault" "$prior"; done
  done
  # No forced mv error here: the REAL rename must refuse a directory target.
  publication_case "$scenario" directory directory
done

# ---------------------------------------------------------------------------
hdr "Regression control — the prose drain still works"

"$HERE/test_ci_failure_drain.sh" >"$TMP/regression.log" 2>&1; RC_R=$?
[[ "$RC_R" -eq 0 ]]
assert $? "test_ci_failure_drain.sh runs green as regression control" \
  "$(tail -n 3 "$TMP/regression.log" | tr '\n' ' ')"

hdr "Result"
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[[ "$PASS" -gt 0 ]] || { printf '  a suite that ran no assertions is not a green suite\n'; exit 1; }
[[ "$FAIL" -eq 0 ]] || exit 1
