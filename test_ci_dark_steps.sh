#!/usr/bin/env bash
# Regression suite for ci_dark_steps.sh — the dark-step reporter (case_50dca137).
#
# THE BUG THIS EXISTS FOR. `offline-suites` is fail-fast with 89 named steps.
# When the OmniRoute rehearsal package expired on 2026-09-03T00:00:00Z, step 12
# went red and 79 steps were skipped — `Secret scan` and every mutation gate
# among them — for ~21 hours, across every PR, while the check surface said
# nothing more specific than "Offline suites: failing". Two suites merged into
# `main` in that window having never executed in CI at all.
#
# WHY THE FIXTURES ARE RECORDED, NOT WRITTEN. A hand-authored job object is one
# that matches whatever the tool happens to read — it proves the tool agrees
# with the test author, not with GitHub. Both fixtures are the verbatim
# `actions/jobs/{id}` response for the two runs named in case_50dca137:
#
#   job-100695318458-dark.json    pristine main e9dea118 — 13 success, 1 failure,
#                                 79 skipped. The outage itself. One of the 79 is
#                                 the runner's `Post Run actions/setup-node@v4`
#                                 teardown, which case_8089241f exempts from the dark
#                                 set (teardown is not a gate), so the tool
#                                 reports 78 dark here.
#   job-100786202472-green.json   PR #198 d794f39c — 95 steps, 0 skipped. The
#                                 same job once the expiry was re-issued.
#   job-105959891748-abort-null.json  run 35466576547, Offline suites, case_eb29f309:
#                                 80 success, 1 failure, 55 conclusions still
#                                 `null` — the at-report-time view of a fail-fast
#                                 abort. CONSTRUCTED, not recorded: the finalised
#                                 job object rewrites those nulls to "skipped",
#                                 which is exactly the state that hid the bug, so
#                                 re-recording this fixture from the API would
#                                 un-write the test. Counts match the card.
#   job-live-green-reporter-inflight.json  case_8089241f: the at-report-time view of
#                                 a GREEN job — 3 success, the reporter itself
#                                 still in_progress (conclusion null), 2 `Post`
#                                 teardown steps still queued. CONSTRUCTED: the
#                                 finalised job object rewrites those nulls, so
#                                 re-recording it from the API would un-write the
#                                 test. Must read 0 dark with no ::error.
#
# Re-record with:
#   gh api repos/TogetherWeOwn/paperclip-ops/actions/jobs/<id> > <fixture>
#
# THE ASSERTION THAT MATTERS MOST is not "it counts 79". It is that the reporter
# stays SILENT on the green fixture. A reporter that warns on every run is one
# every reviewer learns to scroll past, which is the same failure as not having
# it. So the green case is pinned as hard as the dark one.
#
# Offline by construction: every case is served from a recorded file via
# --job-json. No network, no credential. Requires jq.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ci_dark_steps.sh"
FIXTURES="$HERE/test/fixtures/ci_dark_steps"
DARK="$FIXTURES/job-100695318458-dark.json"
GREEN="$FIXTURES/job-100786202472-green.json"
ABORT_NULL="$FIXTURES/job-105959891748-abort-null.json"
LIVE_GREEN="$FIXTURES/job-live-green-reporter-inflight.json"
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

command -v jq >/dev/null 2>&1 || { echo "test_ci_dark_steps: jq is required" >&2; exit 2; }
[[ -x "$TOOL" ]] || { echo "test_ci_dark_steps: $TOOL is not executable" >&2; exit 2; }
# A missing fixture is a hard error, never a skip. A skipped test is a deleted
# test that still prints a zero exit (case_ab86fd07).
for f in "$DARK" "$GREEN" "$ABORT_NULL" "$LIVE_GREEN"; do
  [[ -s "$f" ]] || { echo "test_ci_dark_steps: recorded fixture missing or empty: $f" >&2; exit 2; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------
hdr "The outage fixture — what a dark run must say"

OUT="$("$TOOL" report --job-json "$DARK" 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a dark run still exits 0" \
  "got exit $RC — the reporter must not replace the real failure's attribution"

# 79 skipped minus the exempted `Post Run actions/setup-node@v4` teardown
# (case_8089241f) = 78 reported dark.
grep -q '78 of 93 steps never ran' <<< "$OUT"
assert $? "the dark run names the count of steps that did not execute" \
  "expected '78 of 93 steps never ran'"

grep -q 'OmniRoute rehearsal package suite' <<< "$OUT"
assert $? "the dark run names the step that aborted the job" \
  "the culprit step is not attributed"

grep -q '::error title=' <<< "$OUT"
assert $? "a dark run emits a ::error annotation so it surfaces on the run summary" \
  "no ::error annotation; the report would only be visible to someone opening the log"

# The whole point of the card: these specific gates were dark and nobody knew.
grep -q 'Secret scan' <<< "$OUT"
assert $? "the dark list names Secret scan" \
  "Secret scan is missing from the report of un-run steps"

grep -qi 'mutation-gated' <<< "$OUT"
assert $? "the dark list names the mutation gates" \
  "no mutation-gated step is reported, so vacuity protection looks intact when it is not"

# 13 success + 1 failure + 78 dark (+ 1 exempted Post teardown) = 93. If the
# arithmetic drifts, the report is worse than nothing because it looks
# authoritative.
grep -q 'Executed: 13 passed, 1 failed. Not run: 78.' <<< "$OUT"
assert $? "the executed/not-run tally is reported exactly" \
  "tally line missing or wrong"

# ---------------------------------------------------------------------------
hdr "The fail-fast abort as the reporter sees it — null conclusions are dark (case_eb29f309)"

OUT_A="$("$TOOL" report --job-json "$ABORT_NULL" 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "an aborted run still exits 0" \
  "got exit $RC — the reporter must not replace the real failure's attribution"

grep -q '55 of 136 steps never ran' <<< "$OUT_A"
assert $? "the abort names the count of steps that did not execute" \
  "expected '55 of 136 steps never ran'"

grep -q 'Rehearsal package expiry probe' <<< "$OUT_A"
assert $? "the abort names the step that aborted the job" \
  "the culprit step is not attributed"

grep -q '::error title=' <<< "$OUT_A"
assert $? "an aborted run emits a ::error annotation so it surfaces on the run summary" \
  "no ::error annotation; the report would only be visible to someone opening the log"

grep -q 'Secret scan' <<< "$OUT_A"
assert $? "the abort list names Secret scan" \
  "Secret scan is missing from the report of un-run steps"

grep -qi 'mutation-gated' <<< "$OUT_A"
assert $? "the abort list names the mutation gates" \
  "no mutation-gated step is reported, so vacuity protection looks intact when it is not"

grep -q 'Executed: 80 passed, 1 failed. Not run: 55.' <<< "$OUT_A"
assert $? "the executed/not-run tally is reported exactly" \
  "tally line missing or wrong"

# The false green this card exists to kill: the old `conclusion=="skipped"`
# selector reads ZERO dark here (all unreached steps are still null), so the
# DARK -eq 0 branch prints "all N steps executed" over a 55-step abort.
! grep -q 'all .* steps executed' <<< "$OUT_A"
assert $? "an aborted run never prints the clean banner" \
  "the false green is back: 'all N steps executed' over unreached steps"

# ---------------------------------------------------------------------------
hdr "The green fixture — silence is the feature"

OUT_G="$("$TOOL" report --job-json "$GREEN" 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a fully-executed run exits 0" "got exit $RC"

! grep -q '::error' <<< "$OUT_G"
assert $? "a fully-executed run raises no error annotation" \
  "the reporter cries wolf on a clean run, which trains reviewers to ignore it"

! grep -qi 'never ran' <<< "$OUT_G"
assert $? "a fully-executed run reports no un-run steps" \
  "a clean run is being described as dark"

grep -q 'all 95 steps executed' <<< "$OUT_G"
assert $? "a fully-executed run states the number of steps it confirmed" \
  "the positive confirmation is missing, so 'no news' is ambiguous"

# ---------------------------------------------------------------------------
hdr "The live view — the reporter exempts itself and teardown (case_8089241f)"

# At report time the live job object always holds the reporter's OWN step
# (in_progress, conclusion null) plus queued `Post` teardown steps. Before the
# exemption every green job printed an ::error annotation about itself — the
# cry-wolf failure this entire tool exists to prevent.
OUT_L="$("$TOOL" report --job-json "$LIVE_GREEN" 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a live green run with the reporter in flight exits 0" "got exit $RC"

! grep -q '::error' <<< "$OUT_L"
assert $? "a live green run with the reporter in flight raises no error annotation" \
  "the reporter annotates itself on a clean run, which trains reviewers to ignore it"

! grep -qi 'never ran' <<< "$OUT_L"
assert $? "a live green run with the reporter in flight reports no un-run steps" \
  "the reporter counts itself or teardown as dark"

grep -q 'all 6 steps executed' <<< "$OUT_L"
assert $? "a live green run with the reporter in flight states the number of steps it confirmed" \
  "the positive confirmation is missing, so 'no news' is ambiguous"

# ---------------------------------------------------------------------------
hdr "Refusal — a check that did not happen must never read as clean (case_70ae2474)"

echo '{"name":"x","steps":[]}' > "$TMP/empty.json"
OUT_E="$("$TOOL" report --job-json "$TMP/empty.json" 2>&1)"; RC=$?
[[ "$RC" -eq 3 ]]
assert $? "an empty step list REFUSES rather than reporting zero dark steps" \
  "got exit $RC — 'no steps skipped' out of no data is a false green"
grep -q 'REFUSED' <<< "$OUT_E"
assert $? "the refusal says so in words" "refusal is not labelled"

echo '{"name":"x"}' > "$TMP/nosteps.json"
OUT_N="$("$TOOL" report --job-json "$TMP/nosteps.json" 2>&1)"; RC=$?
[[ "$RC" -eq 3 ]]
assert $? "a job object with no steps key REFUSES" "got exit $RC"

OUT_M="$("$TOOL" report --job-json "$TMP/does-not-exist.json" 2>&1)"; RC=$?
[[ "$RC" -eq 3 ]]
assert $? "a missing job file REFUSES" "got exit $RC"

OUT_U="$("$TOOL" 2>&1)"; RC=$?
[[ "$RC" -eq 2 ]]
assert $? "no arguments is a usage error" "got exit $RC"
OUT_B="$("$TOOL" bogus 2>&1)"; RC=$?
[[ "$RC" -eq 2 ]]
assert $? "an unknown subcommand is a usage error" "got exit $RC"

# ---------------------------------------------------------------------------
hdr "Time bounds — the API call carries a bound, and a hung endpoint refuses (case_e107fefc)"

# Stub curl: records argv and serves one jobs list, emulating curl's -w
# http-code contract. STUB_CURL_MODE=timeout emulates curl exit 28
# (operation timed out). Nothing leaves the loopback.
mkdir -p "$TMP/stubbin"
cat > "$TMP/stubbin/curl" <<'STUB'
#!/usr/bin/env bash
dest=""
printf '%s\0' "$@" >> "$CURL_ARGV_LOG"
prev=""
for a in "$@"; do
  if [[ "$prev" == "-o" ]]; then dest="$a"; prev=""; continue; fi
  case "$a" in
    -o) prev="-o" ;;
  esac
done
if [[ "${STUB_CURL_MODE:-ok}" == "timeout" ]]; then
  echo "curl: (28) Operation timed out after 30001 milliseconds" >&2
  printf '000'
  exit 28
fi
# case_32a28c0d: a timeout AFTER the '200' header still prints CODE 200 via -w
# while curl exits 28 with a truncated body. The helper must refuse on the
# transport status, never accept the truncated download as complete.
if [[ "${STUB_CURL_MODE:-ok}" == "truncated-200" ]]; then
  echo "curl: (28) Operation timed out after 30001 milliseconds" >&2
  printf '{"jobs": [{"name": "trun' > "$dest"
  printf '200'
  exit 28
fi
cp "$STUB_JOBS_JSON" "$dest"
printf '200'
STUB
chmod +x "$TMP/stubbin/curl"

cat > "$TMP/live-jobs.json" <<'JOBS'
{"jobs": [{"name": "test-job", "status": "completed", "conclusion": "failure",
  "steps": [{"name": "s1", "conclusion": "success", "number": 1},
            {"name": "s2", "conclusion": "failure", "number": 2}]}]}
JOBS

live_report() {
  env -u BASH_ENV PATH="$TMP/stubbin:$PATH" \
    GITHUB_REPOSITORY="o/r" GITHUB_RUN_ID="1" GITHUB_JOB="test-job" \
    GH_TOKEN="test-token-20984" GITHUB_API_URL="https://api.invalid" \
    STUB_JOBS_JSON="$TMP/live-jobs.json" STUB_CURL_MODE="$1" CURL_ARGV_LOG="$2" \
    "$TOOL" report
}

: > "$TMP/argv-ok.log"
OUT_LIVE="$(live_report ok "$TMP/argv-ok.log" 2>&1)"; RC_LIVE=$?
[[ "$RC_LIVE" -eq 0 ]]
assert $? "a live report exits 0 through the stub curl" "got exit $RC_LIVE"
grep -q 'all 2 steps executed' <<< "$OUT_LIVE"
assert $? "the stub-served live report states the confirmed step count" "bound flags broke the live path"
grep -q -- '--max-time' "$TMP/argv-ok.log"
assert $? "the jobs-list call carries --max-time" "no --max-time in the recorded curl argv"
grep -q -- '--connect-timeout' "$TMP/argv-ok.log"
assert $? "the jobs-list call carries --connect-timeout" "no --connect-timeout in the recorded curl argv"
grep -q "test-token-20984" "$TMP/argv-ok.log"; [[ $? -ne 0 ]]
assert $? "the stubbed live path still keeps the token out of argv" "the token reached curl argv"

OUT_LTO="$(live_report timeout "$TMP/argv-timeout.log" 2>&1)"; RC_LTO=$?
[[ "$RC_LTO" -eq 3 ]]
assert $? "a hung jobs endpoint REFUSES (exit 3), never prints the clean banner" "got exit $RC_LTO"
grep -q 'REFUSED' <<< "$OUT_LTO"
assert $? "the timeout refusal says so in words" "refusal is not labelled"
! grep -q 'all .* steps executed' <<< "$OUT_LTO"
assert $? "a timed-out report never prints the clean banner" "the false green is back over unmeasured data"

# case_32a28c0d: the download times out AFTER the '200' header, so -w prints 200
# with exit 28 and a truncated body. Only the transport check can refuse it.
OUT_LT2="$(live_report truncated-200 "$TMP/argv-truncated200.log" 2>&1)"; RC_LT2=$?
[[ "$RC_LT2" -eq 3 ]]
assert $? "a '200' download that times out mid-body REFUSES (exit 3)" "got exit $RC_LT2"
grep -q 'transport failure' <<< "$OUT_LT2"
assert $? "the refusal names the transport failure, not the status code" "refusal does not name the transport failure"
! grep -q 'all .* steps executed' <<< "$OUT_LT2"
assert $? "a truncated download never prints the clean banner" "the false green is back over truncated data"

# ---------------------------------------------------------------------------
hdr "The credential never reaches argv (case_3d790dcb)"

! grep -qE 'curl[^|]*(Authorization|\$GH_TOKEN|\$\{GH_TOKEN)' "$TOOL"
assert $? "the token is passed via a header file, never in argv" \
  "the token appears on a curl command line, where /proc exposes it"

# ---------------------------------------------------------------------------
hdr "The ci.yml wiring is load-bearing"

CI="$HERE/.github/workflows/ci.yml"
if [[ -s "$CI" ]]; then
  # Anchor on the STEP DEFINITION at its exact step indentation, not merely on
  # the step's name. The name also appears inside the mutation gate's own sed
  # expression earlier in this same file, and that text contains the literal
  # `if: failure()` the assertion is looking for — so a loose /- name: .../ range
  # matches the gate that is testing this assertion and passes by reading it,
  # staying green after the real wiring has lost its `if:`. The gate caught
  # exactly that. Keep the `^      - name:` anchor.
  STEP="$(awk '/^      - name: Report steps that never ran$/,/^$/' "$CI")"

  [[ -n "$STEP" ]]
  assert $? "the reporter step is present in ci.yml at step indentation" \
    "no step definition found, so the wiring assertions below would be vacuous"

  # A reporter that is an ordinary step in a fail-fast job is skipped by the
  # very abort it exists to describe — so it must run on failure. But it must
  # NOT run always(): since case_a2b959d4 the suites job gates each area's steps
  # with step-level `if:`, so unaffected areas read as "skipped" at report
  # time, and always() would annotate dark steps on every partial green run
  # (the cry-wolf failure this reporter exists to prevent). `if: failure()`
  # is the feature: silent on green, loud on red.
  grep -q 'if: failure()' <<< "$STEP"
  assert $? "the reporter step is wired with if: failure()" \
    "without it the reporter is skipped on exactly the runs that need it"
  if grep -q 'if: always()' <<< "$STEP"; then
    bad "the reporter step still carries if: always(), which false-alarms on area-skipped green runs"
  fi

  grep -q 'ci_dark_steps.sh' <<< "$STEP"
  assert $? "the reporter step invokes ci_dark_steps.sh" "the step does not call the tool"
else
  bad "ci.yml is readable for the wiring assertions"
fi

hdr "Result"
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[[ "$PASS" -gt 0 ]] || { printf '  a suite that ran no assertions is not a green suite\n'; exit 1; }
[[ "$FAIL" -eq 0 ]] || exit 1
