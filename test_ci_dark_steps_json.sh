#!/usr/bin/env bash
# Acceptance suite for the ci_dark_steps.sh JSON verdict (--json).
#
# THE CONTRACT THIS PINS. The dark-step reporter names the steps a fail-fast
# abort registered but never executed, as step-summary markdown plus one
# ::error line — so a triage agent that needs WHICH steps never ran must
# scrape prose. The --json flag adds a machine surface to the EXISTING
# `report --job-json FILE` offline mode: exactly one JSON document on stdout:
#
#   { "schema_version": 1,
#     "job": "Offline suites",
#     "totals": { "total": 93, "ran": 13, "failed": 1, "dark": 78 },
#     "culprit": { "number": 12, "name": "OmniRoute rehearsal package suite" },
#     "dark_steps": [ { "number": 13, "name": "Install pinned ..." } ] }
#
# - totals.ran counts success-conclusion steps (the text tally's "passed");
#   totals.dark counts registered-but-never-executed steps under the SAME
#   exemption as text: the reporter's own step (`Report steps that never
#   ran`) and the runner's `Post ` teardown steps are not gates and never
#   appear in dark_steps.
# - culprit is null (JSON null, key present) when no step concluded failure.
# - dark_steps is complete and ascending by step number; entries carry
#   number+name only, in the same vocabulary as the job object's steps.
# - Exit 0 on every measured report, like text. Unmeasured input (empty or
#   missing step list, malformed job JSON, missing or empty file) REFUSES
#   exit 3 with REFUSED on stderr and no verdict on stdout — parity with
#   text, never a clean-looking answer over data that was not measured.
# - Default text output stays byte-identical: --json only adds the JSON
#   shape on stdout. The existing text suite runs as a regression control.
# - JSON carries names/numbers/counts only — no step logs, no conclusion or
#   timestamp fields, no echoed job payload.
#
# HERMETIC BY CONSTRUCTION. Recorded fixtures via --job-json plus disposable
# synthetic job objects built in temp dirs at runtime. No network (a stub curl
# proves the offline path dials nothing), no credential, no live API, no
# production data. Requires bash and jq. A missing dependency or fixture is a
# hard error, never a skip.
#
# RED/GREEN. On the base commit this suite is RED (--json is an unknown flag).
# After the paired build it must be GREEN without any edit to this file: the
# Builder builds on this branch and never edits this test module.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ci_dark_steps.sh"
TEXT_SUITE="$HERE/test_ci_dark_steps.sh"
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
# cannot be pinned by any mutation.
assert() {
  local status="$1" label="$2" diag="${3:-}"
  if [[ "$status" -eq 0 ]]; then ok "$label"; else bad "$label"; [[ -n "$diag" ]] && printf '        %s\n' "$diag"; fi
  return 0
}

command -v jq >/dev/null 2>&1 || { echo "test_ci_dark_steps_json: jq is required" >&2; exit 2; }
[[ -x "$TOOL" ]] || { echo "test_ci_dark_steps_json: $TOOL is not executable" >&2; exit 2; }
[[ -x "$TEXT_SUITE" ]] || { echo "test_ci_dark_steps_json: $TEXT_SUITE is not executable (regression control)" >&2; exit 2; }
# A missing fixture is a hard error, never a skip. A skipped test is a deleted
# test that still prints a zero exit.
for f in "$DARK" "$GREEN" "$ABORT_NULL" "$LIVE_GREEN"; do
  [[ -s "$f" ]] || { echo "test_ci_dark_steps_json: recorded fixture missing or empty: $f" >&2; exit 2; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- runners (hermetic: never touch a real step summary) ----------------------
run_text() { # $1=job.json $2=stdout $3=stderr -> exit of the tool
  env -u GITHUB_STEP_SUMMARY "$TOOL" report --job-json "$1" >"$2" 2>"$3"
}
run_json() { # $1=job.json $2=stdout $3=stderr -> exit of the tool
  env -u GITHUB_STEP_SUMMARY "$TOOL" report --job-json "$1" --json >"$2" 2>"$3"
}

# --- text parsers (the text side of every parity assertion) -------------------
text_tally() { # "Executed: R passed, F failed. Not run: D." -> "R F D"
  sed -n 's/^Executed: \([0-9][0-9]*\) passed, \([0-9][0-9]*\) failed\. Not run: \([0-9][0-9]*\)\.$/\1 \2 \3/p' "$1"
}
text_dark_of_total() { # "### ... N of M steps never ran" -> "N M"
  sed -n 's/^### .* \([0-9][0-9]*\) of \([0-9][0-9]*\) steps never ran$/\1 \2/p' "$1"
}
text_all_executed() { # "### ... all M steps executed" -> "M"
  sed -n 's/^### .* all \([0-9][0-9]*\) steps executed$/\1/p' "$1"
}
text_culprit() { # 'aborted at step N, "NAME"' -> "N NAME" (first match)
  sed -n 's/.*aborted at step \([0-9][0-9]*\), "\([^"]*\)".*/\1 \2/p' "$1" | head -n 1
}

# --- json helpers -------------------------------------------------------------
# stdout holds exactly one JSON document: parses, and holds no second value
# and no trailing prose (a second doc or trailing markdown fails the slurp).
is_one_json_doc() { # $1=file -> 0 iff exactly one JSON document
  jq -s -e 'length == 1' "$1" >/dev/null 2>&1
}
json_has_no_prose() { # $1=file -> 0 iff no markdown or annotation leaked in
  ! grep -q -e '::error' -e '^###' -e '```' -e '<details>' "$1"
}

# --- disposable fixtures (synthetic, built at runtime, never recorded) --------
cat > "$TMP/job-no-culprit.json" <<'JSON'
{"name": "Synthetic no-culprit job", "conclusion": "cancelled", "status": "completed",
 "steps": [
  {"name": "Set up job", "conclusion": "success", "number": 1, "status": "completed"},
  {"name": "First gate", "conclusion": "success", "number": 2, "status": "completed"},
  {"name": "Second gate", "conclusion": "skipped", "number": 3, "status": "completed"},
  {"name": "Secret scan", "conclusion": "skipped", "number": 4, "status": "completed"},
  {"name": "Third gate (mutation-gated)", "conclusion": "cancelled", "number": 5,
   "status": "completed", "run_log": "MARKER-no-log-echo-7f3a9c tail of a step log"}
 ]}
JSON
NOCULPRIT="$TMP/job-no-culprit.json"

cat > "$TMP/job-exempt-mix.json" <<'JSON'
{"name": "Synthetic exemption mix", "conclusion": "failure", "status": "completed",
 "steps": [
  {"name": "Set up job", "conclusion": "success", "number": 1, "status": "completed"},
  {"name": "Failing gate", "conclusion": "failure", "number": 2, "status": "completed"},
  {"name": "Gated suite A", "conclusion": "skipped", "number": 3, "status": "completed"},
  {"name": "Gated suite B", "conclusion": null, "number": 4, "status": "completed"},
  {"name": "Report steps that never ran", "conclusion": null, "number": 5, "status": "in_progress"},
  {"name": "Post Run actions/checkout@v4", "conclusion": null, "number": 6, "status": "queued"}
 ]}
JSON
EXEMPT_MIX="$TMP/job-exempt-mix.json"

echo '{"name":"x","steps":[]}' > "$TMP/empty-steps.json"
echo '{"name":"x"}' > "$TMP/no-steps-key.json"
printf '{not valid json\n' > "$TMP/malformed.json"
: > "$TMP/empty-file.json"
MISSING="$TMP/does-not-exist.json"

# ---------------------------------------------------------------------------
hdr "The green run — all executed, culprit null"

run_json "$GREEN" "$TMP/green.stdout" "$TMP/green.stderr"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a fully-executed json run exits 0" \
  "got exit $RC — the json report must not replace the real verdict's attribution"

is_one_json_doc "$TMP/green.stdout"
assert $? "a fully-executed run emits exactly one json document on stdout" \
  "stdout is empty, invalid, or holds more than one document"

[[ ! -s "$TMP/green.stderr" ]]
assert $? "a fully-executed json run keeps stderr silent" \
  "$(head -c 200 "$TMP/green.stderr" | tr '\n' ' ')"

jq -e '.schema_version == 1' "$TMP/green.stdout" >/dev/null 2>&1
assert $? "the green envelope carries schema_version 1" \
  "no pinned schema version; consumers cannot detect drift"

jq -e '.job == "Offline suites"' "$TMP/green.stdout" >/dev/null 2>&1
assert $? "the green envelope names the job" \
  "job attribution is missing or wrong"

jq -e '.totals.total == 95 and .totals.ran == 95 and .totals.failed == 0 and .totals.dark == 0' \
  "$TMP/green.stdout" >/dev/null 2>&1
assert $? "the green totals pin 95 executed of 95 with none dark" \
  "counts are wrong on a clean run"

jq -e 'has("culprit") and .culprit == null' "$TMP/green.stdout" >/dev/null 2>&1
assert $? "the green culprit is null when no step failed" \
  "culprit must be JSON null with the key present, not missing or empty"

jq -e '.dark_steps == []' "$TMP/green.stdout" >/dev/null 2>&1
assert $? "the green dark_steps list is empty" \
  "a clean run reports un-run steps"

json_has_no_prose "$TMP/green.stdout"
assert $? "the green json stdout carries no markdown or annotation" \
  "prose leaked into the machine surface; parsers must not strip markdown"

run_text "$GREEN" "$TMP/green.text" "$TMP/green.texterr"; RC_T=$?
[[ "$RC_T" -eq 0 ]] && grep -q 'all 95 steps executed' "$TMP/green.text" \
  && ! grep -q '::error' "$TMP/green.text"
assert $? "text agrees on green: all 95 steps executed with no annotation" \
  "the text control moved; parity below would compare against drift"

T_ALL="$(text_all_executed "$TMP/green.text")"
J_TOTAL="$(jq -r '.totals.total // empty' "$TMP/green.stdout" 2>/dev/null)"
J_DARK="$(jq -r '.totals.dark // empty' "$TMP/green.stdout" 2>/dev/null)"
[[ -n "$T_ALL" && "$T_ALL" == "$J_TOTAL" && "$J_DARK" == "0" ]]
assert $? "text/json parity on green: total and zero dark match" \
  "text says all ${T_ALL:-?} executed, json says total=${J_TOTAL:-?} dark=${J_DARK:-?}"

# ---------------------------------------------------------------------------
hdr "The outage — fail-fast abort with culprit and dark tail"

run_json "$DARK" "$TMP/dark.stdout" "$TMP/dark.stderr"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a dark json run exits 0" \
  "got exit $RC — the json report must not replace the real failure's attribution"

is_one_json_doc "$TMP/dark.stdout"
assert $? "a dark run emits exactly one json document on stdout" \
  "stdout is empty, invalid, or holds more than one document"

[[ ! -s "$TMP/dark.stderr" ]]
assert $? "a dark json run keeps stderr silent" \
  "$(head -c 200 "$TMP/dark.stderr" | tr '\n' ' ')"

jq -e '.schema_version == 1 and .job == "Offline suites"' "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark envelope carries schema_version 1 and the job name" \
  "version or attribution is missing"

# 13 success + 1 failure + 78 dark (+ 1 exempted Post teardown) = 93.
jq -e '.totals.total == 93 and .totals.ran == 13 and .totals.failed == 1 and .totals.dark == 78' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark totals pin 13 ran, 1 failed, 78 dark of 93" \
  "counts are wrong on the outage run"

jq -e '.culprit.number == 12 and .culprit.name == "OmniRoute rehearsal package suite"' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark culprit pins step 12 by number and name" \
  "the aborting step is not attributed in json"

jq -e '.dark_steps | length == 78' "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark list holds all 78 un-run steps" \
  "the json list is cut short; a partial list reads as a full one"

jq -e '[.dark_steps[].number] == ([.dark_steps[].number] | sort)' "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark list is ascending by step number" \
  "unordered steps force every consumer to re-sort"

jq -e '[.dark_steps[].name] | index("Secret scan") != null' "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark json list names Secret scan" \
  "Secret scan is missing from the machine-readable un-run list"

jq -e '[.dark_steps[].name | select(test("mutation-gated"; "i"))] | length > 0' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark json list names the mutation gates" \
  "no mutation-gated step is reported, so vacuity protection looks intact when it is not"

jq -e '[.dark_steps[].name | select(startswith("Post "))] | length == 0' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the dark json list exempts runner Post teardown" \
  "teardown is not a gate; listing it cries wolf"

! grep -q 'Post Run actions/setup-node@v4' "$TMP/dark.stdout"
assert $? "the exempted teardown name appears nowhere in json" \
  "the exemption dropped a name the text report also drops"

json_has_no_prose "$TMP/dark.stdout"
assert $? "the dark json stdout carries no markdown or annotation" \
  "prose leaked into the machine surface"

run_text "$DARK" "$TMP/dark.text" "$TMP/dark.texterr"; RC_T=$?
[[ "$RC_T" -eq 0 ]] \
  && grep -q '78 of 93 steps never ran' "$TMP/dark.text" \
  && grep -q 'OmniRoute rehearsal package suite' "$TMP/dark.text" \
  && grep -q 'Executed: 13 passed, 1 failed. Not run: 78.' "$TMP/dark.text"
assert $? "text agrees on the outage: count, culprit and tally" \
  "the text control moved; parity below would compare against drift"

read -r T_RAN T_FAILED T_DARK <<< "$(text_tally "$TMP/dark.text")"
J_RAN="$(jq -r '.totals.ran // empty' "$TMP/dark.stdout" 2>/dev/null)"
J_FAILED="$(jq -r '.totals.failed // empty' "$TMP/dark.stdout" 2>/dev/null)"
J_DARK="$(jq -r '.totals.dark // empty' "$TMP/dark.stdout" 2>/dev/null)"
[[ -n "${T_RAN:-}" && "$T_RAN" == "$J_RAN" && "$T_FAILED" == "$J_FAILED" && "$T_DARK" == "$J_DARK" ]]
assert $? "text/json parity on the outage: ran, failed and dark match" \
  "text tally ${T_RAN:-?}/${T_FAILED:-?}/${T_DARK:-?}, json ${J_RAN:-?}/${J_FAILED:-?}/${J_DARK:-?}"

T_CULPRIT="$(text_culprit "$TMP/dark.text")"
J_CULPRIT="$(jq -r '"\(.culprit.number) \(.culprit.name)"' "$TMP/dark.stdout" 2>/dev/null)"
[[ -n "$T_CULPRIT" && "$T_CULPRIT" == "$J_CULPRIT" ]]
assert $? "text/json parity on the outage: culprit number and name match" \
  "text culprit '${T_CULPRIT:-?}', json culprit '${J_CULPRIT:-?}'"

# ---------------------------------------------------------------------------
hdr "The at-report-time abort — null conclusions are dark in json too"

run_json "$ABORT_NULL" "$TMP/abort.stdout" "$TMP/abort.stderr"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "an aborted json run exits 0" \
  "got exit $RC — the json report must not replace the real failure's attribution"

is_one_json_doc "$TMP/abort.stdout"
assert $? "an aborted run emits exactly one json document on stdout" \
  "stdout is empty, invalid, or holds more than one document"

# The false green this reporter exists to kill, restated for json: all 55
# unreached steps still carry conclusion null at report time, so a json
# selector that only counts "skipped" reads ZERO dark here.
jq -e '.totals.total == 136 and .totals.ran == 80 and .totals.failed == 1 and .totals.dark == 55' \
  "$TMP/abort.stdout" >/dev/null 2>&1
assert $? "the abort totals pin 80 ran, 1 failed, 55 dark of 136" \
  "null-conclusion steps were not counted dark; the false green is back"

jq -e '.culprit.number == 78 and .culprit.name == "Rehearsal package expiry probe"' \
  "$TMP/abort.stdout" >/dev/null 2>&1
assert $? "the abort culprit pins step 78 by number and name" \
  "the aborting step is not attributed in json"

jq -e '.dark_steps | length == 55' "$TMP/abort.stdout" >/dev/null 2>&1
assert $? "the abort list holds all 55 un-run steps" \
  "the json list is cut short"

jq -e '[.dark_steps[].name] | index("Secret scan") != null' "$TMP/abort.stdout" >/dev/null 2>&1
assert $? "the abort json list names Secret scan" \
  "Secret scan is missing from the machine-readable un-run list"

jq -e '[.dark_steps[].name | select(test("mutation-gated"; "i"))] | length > 0' \
  "$TMP/abort.stdout" >/dev/null 2>&1
assert $? "the abort json list names the mutation gates" \
  "no mutation-gated step is reported"

json_has_no_prose "$TMP/abort.stdout"
assert $? "the abort json stdout carries no markdown or annotation" \
  "prose leaked into the machine surface"

run_text "$ABORT_NULL" "$TMP/abort.text" "$TMP/abort.texterr"; RC_T=$?
[[ "$RC_T" -eq 0 ]] \
  && grep -q '55 of 136 steps never ran' "$TMP/abort.text" \
  && grep -q 'Executed: 80 passed, 1 failed. Not run: 55.' "$TMP/abort.text" \
  && ! grep -q 'all .* steps executed' "$TMP/abort.text"
assert $? "text agrees on the abort: count and tally, never the clean banner" \
  "the text control moved; parity below would compare against drift"

read -r T_RAN T_FAILED T_DARK <<< "$(text_tally "$TMP/abort.text")"
J_RAN="$(jq -r '.totals.ran // empty' "$TMP/abort.stdout" 2>/dev/null)"
J_FAILED="$(jq -r '.totals.failed // empty' "$TMP/abort.stdout" 2>/dev/null)"
J_DARK="$(jq -r '.totals.dark // empty' "$TMP/abort.stdout" 2>/dev/null)"
[[ -n "${T_RAN:-}" && "$T_RAN" == "$J_RAN" && "$T_FAILED" == "$J_FAILED" && "$T_DARK" == "$J_DARK" ]]
assert $? "text/json parity on the abort: ran, failed and dark match" \
  "text tally ${T_RAN:-?}/${T_FAILED:-?}/${T_DARK:-?}, json ${J_RAN:-?}/${J_FAILED:-?}/${J_DARK:-?}"

T_CULPRIT="$(text_culprit "$TMP/abort.text")"
J_CULPRIT="$(jq -r '"\(.culprit.number) \(.culprit.name)"' "$TMP/abort.stdout" 2>/dev/null)"
[[ -n "$T_CULPRIT" && "$T_CULPRIT" == "$J_CULPRIT" ]]
assert $? "text/json parity on the abort: culprit number and name match" \
  "text culprit '${T_CULPRIT:-?}', json culprit '${J_CULPRIT:-?}'"

# ---------------------------------------------------------------------------
hdr "Abort with no failure step — culprit null, dark tail present"

run_json "$NOCULPRIT" "$TMP/nocul.stdout" "$TMP/nocul.stderr"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a no-culprit json run exits 0" "got exit $RC"

is_one_json_doc "$TMP/nocul.stdout"
assert $? "a no-culprit run emits exactly one json document on stdout" \
  "stdout is empty, invalid, or holds more than one document"

# cancelled counts dark exactly like skipped: neither ran to a verdict.
jq -e '.totals.total == 5 and .totals.ran == 2 and .totals.failed == 0 and .totals.dark == 3' \
  "$TMP/nocul.stdout" >/dev/null 2>&1
assert $? "the no-culprit totals pin 2 ran, 0 failed, 3 dark of 5" \
  "counts are wrong when nothing concluded failure"

jq -e 'has("culprit") and .culprit == null' "$TMP/nocul.stdout" >/dev/null 2>&1
assert $? "the no-culprit culprit is null with the key present" \
  "with no failure step the culprit must be JSON null, not missing or guessed"

jq -e '.dark_steps == [{"number":3,"name":"Second gate"},{"number":4,"name":"Secret scan"},{"number":5,"name":"Third gate (mutation-gated)"}]' \
  "$TMP/nocul.stdout" >/dev/null 2>&1
assert $? "the no-culprit dark list pins every un-run number and name" \
  "the complete small list is the proof the large lists are complete too"

! grep -q 'MARKER-no-log-echo-7f3a9c' "$TMP/nocul.stdout"
assert $? "the json carries no step-log field from the job object" \
  "a job payload field leaked into the verdict; names/numbers/counts only"

json_has_no_prose "$TMP/nocul.stdout"
assert $? "the no-culprit json stdout carries no markdown or annotation" \
  "prose leaked into the machine surface"

run_text "$NOCULPRIT" "$TMP/nocul.text" "$TMP/nocul.texterr"; RC_T=$?
[[ "$RC_T" -eq 0 ]] \
  && grep -q '3 of 5 steps never ran' "$TMP/nocul.text" \
  && grep -q 'Executed: 2 passed, 0 failed. Not run: 3.' "$TMP/nocul.text" \
  && ! grep -q 'aborted at step' "$TMP/nocul.text"
assert $? "text agrees with no culprit: dark tail, tally, no abort line" \
  "the text control moved; parity below would compare against drift"

read -r T_RAN T_FAILED T_DARK <<< "$(text_tally "$TMP/nocul.text")"
J_RAN="$(jq -r '.totals.ran // empty' "$TMP/nocul.stdout" 2>/dev/null)"
J_FAILED="$(jq -r '.totals.failed // empty' "$TMP/nocul.stdout" 2>/dev/null)"
J_DARK="$(jq -r '.totals.dark // empty' "$TMP/nocul.stdout" 2>/dev/null)"
[[ -n "${T_RAN:-}" && "$T_RAN" == "$J_RAN" && "$T_FAILED" == "$J_FAILED" && "$T_DARK" == "$J_DARK" ]]
assert $? "text/json parity with no culprit: ran, failed and dark match" \
  "text tally ${T_RAN:-?}/${T_FAILED:-?}/${T_DARK:-?}, json ${J_RAN:-?}/${J_FAILED:-?}/${J_DARK:-?}"

[[ -z "$(text_culprit "$TMP/nocul.text")" ]] \
  && jq -e '.culprit == null' "$TMP/nocul.stdout" >/dev/null 2>&1
assert $? "text/json parity with no culprit: neither side attributes one" \
  "one side names a culprit the other side does not"

# ---------------------------------------------------------------------------
hdr "The live view — the reporter exempts itself and teardown in json too"

run_json "$LIVE_GREEN" "$TMP/live.stdout" "$TMP/live.stderr"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "a live-green json run exits 0" "got exit $RC"

is_one_json_doc "$TMP/live.stdout"
assert $? "a live-green run emits exactly one json document on stdout" \
  "stdout is empty, invalid, or holds more than one document"

jq -e '.totals.total == 6 and .totals.ran == 3 and .totals.failed == 0 and .totals.dark == 0' \
  "$TMP/live.stdout" >/dev/null 2>&1
assert $? "the live-green totals pin 3 ran of 6 with none dark" \
  "the reporter counted itself or teardown as dark"

jq -e '.culprit == null and .dark_steps == []' "$TMP/live.stdout" >/dev/null 2>&1
assert $? "the live-green culprit is null and the dark list is empty" \
  "a clean live view reports un-run steps"

run_text "$LIVE_GREEN" "$TMP/live.text" "$TMP/live.texterr"; RC_T=$?
[[ "$RC_T" -eq 0 ]] && grep -q 'all 6 steps executed' "$TMP/live.text" \
  && ! grep -q '::error' "$TMP/live.text"
assert $? "text agrees live-green: all 6 executed with no annotation" \
  "the text control moved; parity below would compare against drift"

T_ALL="$(text_all_executed "$TMP/live.text")"
J_TOTAL="$(jq -r '.totals.total // empty' "$TMP/live.stdout" 2>/dev/null)"
J_DARK="$(jq -r '.totals.dark // empty' "$TMP/live.stdout" 2>/dev/null)"
[[ -n "$T_ALL" && "$T_ALL" == "$J_TOTAL" && "$J_DARK" == "0" ]]
assert $? "text/json parity live-green: total and zero dark match" \
  "text says all ${T_ALL:-?} executed, json says total=${J_TOTAL:-?} dark=${J_DARK:-?}"

# The mixed case proves the exemption INSIDE a dark run: two gates never ran
# while the reporter's own in-flight step and one Post teardown are live.
run_json "$EXEMPT_MIX" "$TMP/mix.stdout" "$TMP/mix.stderr"; RC=$?
[[ "$RC" -eq 0 ]]
assert $? "an exemption-mix json run exits 0" "got exit $RC"

is_one_json_doc "$TMP/mix.stdout"
assert $? "an exemption-mix run emits exactly one json document on stdout" \
  "stdout is empty, invalid, or holds more than one document"

jq -e '.totals.total == 6 and .totals.ran == 1 and .totals.failed == 1 and .totals.dark == 2' \
  "$TMP/mix.stdout" >/dev/null 2>&1
assert $? "the exemption-mix totals pin 1 ran, 1 failed, 2 dark of 6" \
  "the reporter or teardown was counted as a dark gate"

jq -e '.culprit.number == 2 and .culprit.name == "Failing gate"' "$TMP/mix.stdout" >/dev/null 2>&1
assert $? "the exemption-mix culprit pins step 2 by number and name" \
  "the aborting step is not attributed in json"

jq -e '.dark_steps == [{"number":3,"name":"Gated suite A"},{"number":4,"name":"Gated suite B"}]' \
  "$TMP/mix.stdout" >/dev/null 2>&1
assert $? "the exemption-mix dark list pins exactly the two gates" \
  "exempt steps leaked in, or a gate is missing"

! grep -q -e 'Report steps that never ran' -e 'Post Run actions/checkout@v4' "$TMP/mix.stdout"
assert $? "exempt step names appear nowhere in the mix json" \
  "the exemption dropped names the verdict still carries"

run_text "$EXEMPT_MIX" "$TMP/mix.text" "$TMP/mix.texterr"; RC_T=$?
[[ "$RC_T" -eq 0 ]] \
  && grep -q '2 of 6 steps never ran' "$TMP/mix.text" \
  && grep -q 'Executed: 1 passed, 1 failed. Not run: 2.' "$TMP/mix.text"
assert $? "text agrees on the mix: dark count and tally" \
  "the text control moved; parity below would compare against drift"

read -r T_RAN T_FAILED T_DARK <<< "$(text_tally "$TMP/mix.text")"
J_RAN="$(jq -r '.totals.ran // empty' "$TMP/mix.stdout" 2>/dev/null)"
J_FAILED="$(jq -r '.totals.failed // empty' "$TMP/mix.stdout" 2>/dev/null)"
J_DARK="$(jq -r '.totals.dark // empty' "$TMP/mix.stdout" 2>/dev/null)"
[[ -n "${T_RAN:-}" && "$T_RAN" == "$J_RAN" && "$T_FAILED" == "$J_FAILED" && "$T_DARK" == "$J_DARK" ]]
assert $? "text/json parity on the mix: ran, failed and dark match" \
  "text tally ${T_RAN:-?}/${T_FAILED:-?}/${T_DARK:-?}, json ${J_RAN:-?}/${J_FAILED:-?}/${J_DARK:-?}"

# ---------------------------------------------------------------------------
hdr "Refusal parity — unmeasured input refuses like text, never reads clean"

refusal_case() { # $1=label $2=job.json (must REFUSE in both modes)
  local label="$1" job="$2" text_rc json_rc
  run_text "$job" "$TMP/ref.text" "$TMP/ref.texterr"; text_rc=$?
  [[ "$text_rc" -eq 3 ]] && grep -q 'REFUSED' "$TMP/ref.texterr"
  assert $? "$label refuses in text mode (exit 3, REFUSED)" \
    "got exit $text_rc — the control moved, so json parity below is vacuous"
  run_json "$job" "$TMP/ref.stdout" "$TMP/ref.stderr"; json_rc=$?
  [[ "$json_rc" -eq 3 ]]
  assert $? "$label refuses in json mode with exit 3" \
    "got exit $json_rc — unmeasured input must refuse, never report"
  grep -q 'REFUSED' "$TMP/ref.stderr"
  assert $? "$label json refusal says so in words" \
    "the refusal is not labelled REFUSED"
  [[ ! -s "$TMP/ref.stdout" ]]
  assert $? "$label json refusal leaves stdout empty" \
    "a verdict-shaped stdout over unmeasured data reads as an answer"
  ! grep -q '"dark": 0' "$TMP/ref.stdout" "$TMP/ref.stderr" 2>/dev/null
  assert $? "$label json refusal never reports zero dark" \
    "zero dark over no measurement is the false green this gate forbids"
}

refusal_case "an empty step list" "$TMP/empty-steps.json"
refusal_case "a job object with no steps key" "$TMP/no-steps-key.json"
refusal_case "malformed job json" "$TMP/malformed.json"
refusal_case "a missing job file" "$MISSING"
refusal_case "an empty job file" "$TMP/empty-file.json"

# ---------------------------------------------------------------------------
hdr "Shape and hygiene — names, numbers and counts only"

jq -e '(. | keys | sort) == ["culprit","dark_steps","job","schema_version","totals"]' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the envelope carries exactly the five documented keys" \
  "extra top-level keys echo job payload fields consumers must ignore"

jq -e 'has("steps") | not' "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the envelope never echoes the job step list" \
  "a steps array in the verdict duplicates the input it summarizes"

jq -e '(.totals | keys | sort) == ["dark","failed","ran","total"]' "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "totals carries exactly total, ran, failed and dark" \
  "count keys drifted; consumers pin these four"

jq -e '[.totals.total,.totals.ran,.totals.failed,.totals.dark | type] | all(. == "number")' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "every total is a json number, never a string" \
  "string counts break arithmetic in every consumer"

jq -e '(.culprit | keys | sort) == ["name","number"]' "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "the culprit carries number and name only" \
  "extra culprit keys echo payload fields"

jq -e 'all(.dark_steps[]; ((keys | sort | join(",")) == "name,number"))' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "every dark entry carries number and name only" \
  "extra entry keys echo payload fields"

# Every json dark entry is a real (number, name) pair from the job object: the
# verdict speaks the job's own step vocabulary, never a paraphrase of it.
jq --slurpfile job "$DARK" -e \
  '([.dark_steps[] | "\(.number)\t\(.name)"] - [$job[0].steps[] | "\(.number)\t\(.name)"] | length == 0)' \
  "$TMP/dark.stdout" >/dev/null 2>&1
assert $? "every dark json entry matches a job step number and name" \
  "a verdict entry names no step the job registered"

jq --slurpfile job "$ABORT_NULL" -e \
  '([.dark_steps[] | "\(.number)\t\(.name)"] - [$job[0].steps[] | "\(.number)\t\(.name)"] | length == 0)' \
  "$TMP/abort.stdout" >/dev/null 2>&1
assert $? "every abort json entry matches a job step number and name" \
  "a verdict entry names no step the job registered"

for needle in '"conclusion"' '"status"' '"started_at"' '"completed_at"' '"run_log"' '"logs_url"'; do
  ! grep -qF -- "$needle" "$TMP/dark.stdout" "$TMP/abort.stdout" "$TMP/nocul.stdout" "$TMP/green.stdout"
  assert $? "no verdict carries the $needle payload field" \
    "$needle leaked from the job object into the verdict"
done

# ---------------------------------------------------------------------------
hdr "Flags, docs, hermeticity — refused loudly, served offline"

"$TOOL" help >"$TMP/help.txt" 2>&1
grep -q -- '--json' "$TMP/help.txt"
assert $? "help documents --json" \
  "the flag exists nowhere a triage agent looks"
grep -q -e 'schema_version' "$TMP/help.txt"
assert $? "help names the json verdict schema version" \
  "no schema cue in help; the shape is undiscoverable"

# --json works in any flag position and means the same bytes either way.
env -u GITHUB_STEP_SUMMARY "$TOOL" report --json --job-json "$DARK" \
  >"$TMP/flagfirst.stdout" 2>"$TMP/flagfirst.stderr"; RC_F=$?
[[ "$RC_F" -eq 0 ]] && is_one_json_doc "$TMP/flagfirst.stdout"
assert $? "--json works before --job-json" "got exit $RC_F"
cmp -s "$TMP/dark.stdout" "$TMP/flagfirst.stdout"
assert $? "flag position changes no json byte" \
  "the same report parses differently by flag order"

env -u GITHUB_STEP_SUMMARY "$TOOL" report --job-json "$DARK" --json --bogus-flag \
  >"$TMP/bogus.stdout" 2>"$TMP/bogus.stderr"; RC_B=$?
[[ "$RC_B" -eq 2 ]]
assert $? "an unknown flag with --json is a usage error" "got exit $RC_B"

# A curl that refuses the network: if the offline json path dials out, the
# call is recorded and fails loudly instead of hanging or leaking.
mkdir -p "$TMP/stubbin"
cat > "$TMP/stubbin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' "$@" >> "$CURL_ARGV_LOG"
echo "stub curl: network is refused in hermetic tests" >&2
exit 99
STUB
chmod +x "$TMP/stubbin/curl"
: > "$TMP/argv-json.log"
env -u BASH_ENV -u GITHUB_STEP_SUMMARY PATH="$TMP/stubbin:$PATH" CURL_ARGV_LOG="$TMP/argv-json.log" \
  "$TOOL" report --job-json "$DARK" --json >"$TMP/stub.stdout" 2>"$TMP/stub.stderr"; RC_ST=$?
[[ "$RC_ST" -eq 0 ]] && is_one_json_doc "$TMP/stub.stdout"
assert $? "the offline json path reports with network refused" "got exit $RC_ST"
[[ ! -s "$TMP/argv-json.log" ]]
assert $? "the offline json path makes zero network calls" \
  "curl was invoked; the offline path dialed out"

# A set step summary must not pollute the machine surface: stdout stays one
# json document whatever the summary file receives.
GITHUB_STEP_SUMMARY="$TMP/summary.md" run_json "$DARK" "$TMP/sum.stdout" "$TMP/sum.stderr"; RC_SM=$?
[[ "$RC_SM" -eq 0 ]] && is_one_json_doc "$TMP/sum.stdout" && json_has_no_prose "$TMP/sum.stdout"
assert $? "json stdout stays one clean document with a summary file set" "got exit $RC_SM"

# ---------------------------------------------------------------------------
hdr "Regression control — the prose report still works"

"$TEXT_SUITE" >"$TMP/regression.log" 2>&1; RC_R=$?
[[ "$RC_R" -eq 0 ]]
assert $? "test_ci_dark_steps.sh runs green as regression control" \
  "$(tail -n 3 "$TMP/regression.log" | tr '\n' ' ')"

hdr "Result"
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[[ "$PASS" -gt 0 ]] || { printf '  a suite that ran no assertions is not a green suite\n'; exit 1; }
[[ "$FAIL" -eq 0 ]] || exit 1
