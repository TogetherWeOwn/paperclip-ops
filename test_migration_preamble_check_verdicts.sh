#!/usr/bin/env bash
# ===========================================================================
# Immutable acceptance contract for `migration_timeout_preamble.sh check --json`.
#
# IMMUTABLE: the paired Builder starts from this pushed branch/commit and MUST
# NOT edit this file. Corrections return to the Test Author card by native
# coordination. No standalone test-only PR: this branch is handed to the
# paired source PR.
#
# FROZEN CONTRACT (what the Builder implements):
#
# `check --json` (threaded through parse_timeouts; every other unknown flag
# still refuses) prints exactly one `migration-preamble-verdict.v1` object to
# stdout and nothing else there. Default prose and exit codes (0/1/2) are
# byte-identical without the flag. `has_preamble` semantics (presence of each
# SET statement, any quoting, any value — never value equality) are unchanged.
#
# Top-level object, exactly these keys:
#   schema:             "migration-preamble-verdict.v1"
#   outcome:            "clean" | "findings" | "unmeasurable"
#   files:              argv-order entries for successfully checked files only:
#                         {path, ok, missing, next_action}
#                       path:        the argv string as given (not resolved)
#                       ok:          true iff both SET statements present
#                       missing:     [] iff ok; else the subset of
#                                    ["lock_timeout","statement_timeout"] that
#                                    is absent, in that fixed order
#                       next_action: "" iff ok; else 1..200 chars naming the
#                                    fix (mentions preamble|inject|timeout)
#   totals:             {files, ok, missing} ints over `files`:
#                         files == len(files), ok == count(ok),
#                         missing == files - ok
#   timeouts:           {lock_timeout, statement_timeout} strings echoing the
#                         configured values (defaults 10s/120s or overrides)
#   unmeasurable_path:  null unless outcome is unmeasurable; then the argv
#                         string of the first unreadable file
#
# Exit mapping: clean -> 0, findings -> 1, unmeasurable -> 2. Prose exits are
# unchanged and equal the JSON exits for the same file set.
#
# Unreadable files stay exit 2 REFUSED in both modes: stderr keeps the REFUSED
# line naming the file, and in JSON mode stdout is the verdict object with
# outcome unmeasurable — the refused file is never counted as clean and never
# appears as an ok entry. Files checked before the refusal keep their entries.
#
# Usage errors are text, never JSON (exit 2, REFUSED on stderr, empty stdout):
#   - `check` with no files: stderr names the missing files
#     ("needs at least one file", the existing message).
#   - `print --json` / `inject --json`: refused; the message names --json and
#     the check subcommand (machine verdicts cover `check` only).
#   - any other unknown flag: the existing `unknown flag: <flag>` refusal,
#     with or without --json alongside.
#
# No SQL content echo: paths, presence booleans and statement names only —
# migration bodies never appear in verdict stdout or stderr.
#
# Bundle (same source PR): emission, usage text mentioning --json, and a
# migration-gates contract note in docs/migration-standard.md naming the
# schema and this suite's focused command.
#
# RED BASELINE: `check --json` does not exist today; every JSON verdict
# assertion below fails with `REFUSED: ... unknown flag: --json` (exit 2).
# Prose controls pass unchanged. Do not weaken asserts to make RED pass.
#
# Runnable command (from repo root):
#   ./test_migration_preamble_check_verdicts.sh
#
# Fully offline: fixture .sql files in a throwaway temp dir, no database,
# no network, no credentials.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/migration_timeout_preamble.sh"
DOCS="$HERE/docs/migration-standard.md"
PASS=0; FAIL=0

ok()  { printf '  ok   %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL+1)); }

# ONE label per assertion, printed identically green or red.
assert() {
  local status="$1" label="$2" diag="${3:-}"
  if [[ "$status" -eq 0 ]]; then ok "$label"; else bad "$label"; [[ -n "$diag" ]] && printf '        %s\n' "$diag"; fi
  return 0
}

command -v jq >/dev/null 2>&1 || { echo "test_migration_preamble_check_verdicts: jq is required" >&2; exit 2; }
[[ -x "$GATE" ]] || { echo "test_migration_preamble_check_verdicts: $GATE is not executable" >&2; exit 2; }
[[ -f "$DOCS" ]] || { echo "test_migration_preamble_check_verdicts: missing $DOCS" >&2; exit 2; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
TMP="$(mktemp -d)"; trap 'rm -rf "$WORK" "$TMP"' EXIT

CANARY="CANARY-PREAMBLE-NEVER-ECHO-7f3a9c2e41"

# -- fixtures (.sql files only; every body carries the canary) ---------------
printf "SET lock_timeout = '10s';\nSET statement_timeout = '120s';\nSELECT '%s-clean';\n" "$CANARY" > "$WORK/good.sql"
printf "SET statement_timeout = '120s';\nSELECT '%s-no-lock';\n" "$CANARY" > "$WORK/missing_lock.sql"
printf "SET lock_timeout = '10s';\nSELECT '%s-no-stmt';\n" "$CANARY" > "$WORK/missing_statement.sql"
printf "SELECT '%s-bare';\n" "$CANARY" > "$WORK/bare.sql"
printf 'SET lock_timeout = "10s";\nSET statement_timeout = "120s";\nSELECT '"'"'%s-double'"'"';\n' "$CANARY" > "$WORK/double.sql"
printf "SET lock_timeout = '5s';\nSET statement_timeout = '30s';\nSELECT '%s-custom';\n" "$CANARY" > "$WORK/custom.sql"
MISSING="$WORK/no-such-file.sql"

GOOD="$WORK/good.sql"; NOLOCK="$WORK/missing_lock.sql"; NOSTMT="$WORK/missing_statement.sql"
BARE="$WORK/bare.sql"; DOUBLE="$WORK/double.sql"; CUSTOM="$WORK/custom.sql"

# -- helpers ---------------------------------------------------------------
# run_json <out> <err> -- <gate args...>: run, saving rc in RC.
run_json() {
  local out="$1" err="$2"; shift 2
  [[ "${1:-}" == "--" ]] && shift
  RC=0; "$GATE" "$@" >"$out" 2>"$err" || RC=$?
}
is_single_json() { jq -s -e 'length == 1' "$1" >/dev/null 2>&1; }
no_canary() { ! grep -q "$CANARY" "$1" "$2" 2>/dev/null; }
no_prose() { ! grep -qE '^(ok:|FAIL:)' "$1" 2>/dev/null; }

# ===========================================================================
echo "== clean: all-present file =="
run_json "$TMP/clean.json" "$TMP/clean.err" -- check --json "$GOOD"
[[ "$RC" -eq 0 ]]; assert $? "clean: check --json exits 0 on all-present" "got exit $RC"
is_single_json "$TMP/clean.json"; assert $? "clean: stdout is exactly one JSON document"
[[ ! -s "$TMP/clean.err" ]]; assert $? "clean: stderr is empty"
jq -e '.schema == "migration-preamble-verdict.v1"' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: schema pins migration-preamble-verdict.v1"
jq -e '.outcome == "clean"' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: outcome is clean"
jq -e --arg p "$GOOD" '.files | length == 1 and .[0].path == $p' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: per-file path echoes the argv string"
jq -e '.files[0].ok == true and .files[0].missing == []' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: per-file ok is true with empty missing"
jq -e '.files[0].next_action == ""' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: per-file next_action is empty when ok"
jq -e '.totals == {"files":1,"ok":1,"missing":0}' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: totals count one file ok"
jq -e '.timeouts == {"lock_timeout":"10s","statement_timeout":"120s"}' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: timeouts echo the 10s/120s defaults"
jq -e '.unmeasurable_path == null' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: unmeasurable_path is null"
no_prose "$TMP/clean.json"; assert $? "clean: stdout carries no prose verdict lines"
no_canary "$TMP/clean.json" "$TMP/clean.err"; assert $? "clean: migration bodies never echo"
jq -e '. | keys == ["files","outcome","schema","timeouts","totals","unmeasurable_path"]' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: top-level keys are exactly the frozen six"
jq -e '.files[0] | keys == ["missing","next_action","ok","path"]' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: per-file keys are exactly path/ok/missing/next_action"
jq -e '.totals | keys == ["files","missing","ok"]' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: totals keys are exactly files/ok/missing"
jq -e '.timeouts | keys == ["lock_timeout","statement_timeout"]' "$TMP/clean.json" >/dev/null 2>&1
assert $? "clean: timeouts keys are exactly lock_timeout/statement_timeout"
RC_TEXT=0; "$GATE" check "$GOOD" >"$TMP/clean.txt" 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq "$RC" ]]; assert $? "clean: exit parity prose vs JSON" "prose=$RC_TEXT json=$RC"

echo "== findings: missing lock only =="
run_json "$TMP/nolock.json" "$TMP/nolock.err" -- check --json "$NOLOCK"
[[ "$RC" -eq 1 ]]; assert $? "missing-lock: check --json exits 1" "got exit $RC"
jq -e '.outcome == "findings" and .files[0].ok == false and .files[0].missing == ["lock_timeout"]' "$TMP/nolock.json" >/dev/null 2>&1
assert $? "missing-lock: entry names lock_timeout only"
jq -e '.files[0].next_action | length >= 1 and length <= 200 and test("preamble|inject|timeout"; "i")' "$TMP/nolock.json" >/dev/null 2>&1
assert $? "missing-lock: next_action is bounded and names the fix"
jq -e '.totals == {"files":1,"ok":0,"missing":1} and .unmeasurable_path == null' "$TMP/nolock.json" >/dev/null 2>&1
assert $? "missing-lock: totals count one file missing"
no_canary "$TMP/nolock.json" "$TMP/nolock.err"; assert $? "missing-lock: migration bodies never echo"
RC_TEXT=0; "$GATE" check "$NOLOCK" >"$TMP/nolock.txt" 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq "$RC" ]]; assert $? "missing-lock: exit parity prose vs JSON" "prose=$RC_TEXT json=$RC"

echo "== findings: missing statement only =="
run_json "$TMP/nostmt.json" "$TMP/nostmt.err" -- check --json "$NOSTMT"
[[ "$RC" -eq 1 ]]; assert $? "missing-statement: check --json exits 1" "got exit $RC"
jq -e '.outcome == "findings" and .files[0].ok == false and .files[0].missing == ["statement_timeout"]' "$TMP/nostmt.json" >/dev/null 2>&1
assert $? "missing-statement: entry names statement_timeout only"
jq -e '.files[0].next_action | length >= 1 and length <= 200 and test("preamble|inject|timeout"; "i")' "$TMP/nostmt.json" >/dev/null 2>&1
assert $? "missing-statement: next_action is bounded and names the fix"
jq -e '.totals == {"files":1,"ok":0,"missing":1}' "$TMP/nostmt.json" >/dev/null 2>&1
assert $? "missing-statement: totals count one file missing"
RC_TEXT=0; "$GATE" check "$NOSTMT" >/dev/null 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq "$RC" ]]; assert $? "missing-statement: exit parity prose vs JSON" "prose=$RC_TEXT json=$RC"

echo "== findings: missing both =="
run_json "$TMP/bare.json" "$TMP/bare.err" -- check --json "$BARE"
[[ "$RC" -eq 1 ]]; assert $? "missing-both: check --json exits 1" "got exit $RC"
jq -e '.files[0].missing == ["lock_timeout","statement_timeout"]' "$TMP/bare.json" >/dev/null 2>&1
assert $? "missing-both: entry names both statements in fixed order"
no_canary "$TMP/bare.json" "$TMP/bare.err"; assert $? "missing-both: migration bodies never echo"
RC_TEXT=0; "$GATE" check "$BARE" >/dev/null 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq "$RC" ]]; assert $? "missing-both: exit parity prose vs JSON" "prose=$RC_TEXT json=$RC"

echo "== clean: double-quoted variants =="
run_json "$TMP/double.json" "$TMP/double.err" -- check --json "$DOUBLE"
[[ "$RC" -eq 0 ]]; assert $? "double-quoted: check --json exits 0" "got exit $RC"
jq -e '.outcome == "clean" and .files[0].ok == true' "$TMP/double.json" >/dev/null 2>&1
assert $? "double-quoted: double quotes still count as present"

echo "== clean: presence, not equality =="
run_json "$TMP/custom.json" "$TMP/custom.err" -- check --json "$CUSTOM"
[[ "$RC" -eq 0 ]]; assert $? "custom-values: check --json exits 0" "got exit $RC"
jq -e '.outcome == "clean" and .files[0].ok == true' "$TMP/custom.json" >/dev/null 2>&1
assert $? "custom-values: non-default values still count as present"
jq -e '.timeouts == {"lock_timeout":"10s","statement_timeout":"120s"}' "$TMP/custom.json" >/dev/null 2>&1
assert $? "custom-values: timeouts echo configured defaults, not file values"

echo "== findings: mixed multi-file run =="
run_json "$TMP/mixed.json" "$TMP/mixed.err" -- check --json "$GOOD" "$BARE"
[[ "$RC" -eq 1 ]]; assert $? "mixed: check --json exits 1" "got exit $RC"
jq -e --arg g "$GOOD" --arg b "$BARE" '.outcome == "findings" and (.files | length == 2) and .files[0].path == $g and .files[1].path == $b' "$TMP/mixed.json" >/dev/null 2>&1
assert $? "mixed: files keep argv order across entries"
jq -e '.files[0].ok == true and .files[1].ok == false' "$TMP/mixed.json" >/dev/null 2>&1
assert $? "mixed: per-entry ok flags match each file"
jq -e '.totals == {"files":2,"ok":1,"missing":1}' "$TMP/mixed.json" >/dev/null 2>&1
assert $? "mixed: totals count one ok and one missing"
RC_TEXT=0; "$GATE" check "$GOOD" "$BARE" >/dev/null 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq "$RC" ]]; assert $? "mixed: exit parity prose vs JSON" "prose=$RC_TEXT json=$RC"

echo "== configured timeout echo =="
run_json "$TMP/over.json" "$TMP/over.err" -- check --lock-timeout 5s --statement-timeout 30s --json "$GOOD"
[[ "$RC" -eq 0 ]]; assert $? "overrides: check --json exits 0 with overrides" "got exit $RC"
jq -e '.timeouts == {"lock_timeout":"5s","statement_timeout":"30s"} and .outcome == "clean"' "$TMP/over.json" >/dev/null 2>&1
assert $? "overrides: timeouts echo the configured overrides"
run_json "$TMP/badtime.json" "$TMP/badtime.err" -- check --lock-timeout forever --json "$GOOD"
[[ "$RC" -eq 2 ]]; assert $? "bad-timeout: check --json refuses a bad timeout" "got exit $RC"
grep -q "bad --lock-timeout" "$TMP/badtime.err" 2>/dev/null
assert $? "bad-timeout: refusal names the bad timeout"
[[ ! -s "$TMP/badtime.json" ]]; assert $? "bad-timeout: refusal emits no JSON on stdout"

echo "== flag threading =="
run_json "$TMP/trail.json" "$TMP/trail.err" -- check "$GOOD" --json
[[ "$RC" -eq 0 ]]; assert $? "trailing-flag: trailing --json still exits 0" "got exit $RC"
jq -e '.outcome == "clean"' "$TMP/trail.json" >/dev/null 2>&1
assert $? "trailing-flag: trailing --json still emits the verdict"

echo "== unmeasurable: unreadable file =="
run_json "$TMP/unmeas.json" "$TMP/unmeas.err" -- check --json "$MISSING"
[[ "$RC" -eq 2 ]]; assert $? "unreadable: check --json exits 2" "got exit $RC"
grep -q "REFUSED" "$TMP/unmeas.err" 2>/dev/null && grep -q "no-such-file.sql" "$TMP/unmeas.err" 2>/dev/null
assert $? "unreadable: stderr stays REFUSED naming the file"
is_single_json "$TMP/unmeas.json"; assert $? "unreadable: stdout is still one JSON document"
jq -e '.outcome == "unmeasurable"' "$TMP/unmeas.json" >/dev/null 2>&1
assert $? "unreadable: outcome is unmeasurable, never clean"
jq -e --arg p "$MISSING" '.unmeasurable_path == $p' "$TMP/unmeas.json" >/dev/null 2>&1
assert $? "unreadable: unmeasurable_path names the refused file"
jq -e '.totals.ok == 0 and ([.files[] | select(.ok == true)] | length == 0)' "$TMP/unmeas.json" >/dev/null 2>&1
assert $? "unreadable: refused file is never counted as clean"
jq -e '.timeouts == {"lock_timeout":"10s","statement_timeout":"120s"}' "$TMP/unmeas.json" >/dev/null 2>&1
assert $? "unreadable: timeouts still echo the configuration"
RC_TEXT=0; "$GATE" check "$MISSING" >/dev/null 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq "$RC" ]]; assert $? "unreadable: exit parity prose vs JSON" "prose=$RC_TEXT json=$RC"

echo "== unmeasurable: good file before the refusal =="
run_json "$TMP/part.json" "$TMP/part.err" -- check --json "$GOOD" "$MISSING"
[[ "$RC" -eq 2 ]]; assert $? "partial: check --json exits 2" "got exit $RC"
jq -e --arg g "$GOOD" --arg p "$MISSING" '.outcome == "unmeasurable" and (.files | length == 1) and .files[0].path == $g and .files[0].ok == true and .unmeasurable_path == $p' "$TMP/part.json" >/dev/null 2>&1
assert $? "partial: measured entries kept, refusal named"
grep -q "no-such-file.sql" "$TMP/part.err" 2>/dev/null
assert $? "partial: stderr names the refused file"

echo "== refusals: usage errors stay text, never JSON =="
run_json "$TMP/nofiles.json" "$TMP/nofiles.err" -- check --json
[[ "$RC" -eq 2 ]]; assert $? "no-files: check --json with no files exits 2" "got exit $RC"
grep -q "needs at least one file" "$TMP/nofiles.err" 2>/dev/null
assert $? "no-files: refusal names the missing files"
[[ ! -s "$TMP/nofiles.json" ]]; assert $? "no-files: refusal emits no JSON on stdout"
run_json "$TMP/print.json" "$TMP/print.err" -- print --json
[[ "$RC" -eq 2 ]]; assert $? "print-json: print --json exits 2" "got exit $RC"
grep -q -- "--json" "$TMP/print.err" 2>/dev/null && grep -q "check" "$TMP/print.err" 2>/dev/null
assert $? "print-json: refusal names --json and the check subcommand"
[[ ! -s "$TMP/print.json" ]]; assert $? "print-json: refusal emits no JSON on stdout"
run_json "$TMP/inject.json" "$TMP/inject.err" -- inject --json "$BARE"
[[ "$RC" -eq 2 ]]; assert $? "inject-json: inject --json exits 2" "got exit $RC"
grep -q -- "--json" "$TMP/inject.err" 2>/dev/null && grep -q "check" "$TMP/inject.err" 2>/dev/null
assert $? "inject-json: refusal names --json and the check subcommand"
[[ ! -s "$TMP/inject.json" ]]; assert $? "inject-json: refusal emits no JSON on stdout"
run_json "$TMP/bogus.json" "$TMP/bogus.err" -- check --bogus "$GOOD"
[[ "$RC" -eq 2 ]]; assert $? "unknown-flag: check --bogus exits 2" "got exit $RC"
grep -q "unknown flag: --bogus" "$TMP/bogus.err" 2>/dev/null
assert $? "unknown-flag: refusal names the bogus flag"
run_json "$TMP/bogusjson.json" "$TMP/bogusjson.err" -- check --json --bogus "$GOOD"
[[ "$RC" -eq 2 ]]; assert $? "unknown-flag-json: check --json --bogus exits 2" "got exit $RC"
grep -q "unknown flag: --bogus" "$TMP/bogusjson.err" 2>/dev/null
assert $? "unknown-flag-json: --json threads through, --bogus still refuses"
[[ ! -s "$TMP/bogusjson.json" ]]; assert $? "unknown-flag-json: refusal emits no JSON on stdout"

echo "== determinism =="
run_json "$TMP/det1.json" "$TMP/det1.err" -- check --json "$GOOD" "$BARE"
run_json "$TMP/det2.json" "$TMP/det2.err" -- check --json "$GOOD" "$BARE"
[[ -s "$TMP/det1.json" ]] && cmp -s "$TMP/det1.json" "$TMP/det2.json"
assert $? "determinism: the same file set yields byte-identical JSON"

echo "== prose controls (unchanged behavior) =="
RC_TEXT=0; "$GATE" check "$GOOD" >"$TMP/p-good.txt" 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq 0 ]] && grep -q "ok: preamble present" "$TMP/p-good.txt"
assert $? "prose: check passes a file carrying the preamble"
RC_TEXT=0; "$GATE" check "$BARE" >"$TMP/p-bare.txt" 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq 1 ]] && grep -q "bare.sql" "$TMP/p-bare.txt"
assert $? "prose: check fails a file without the preamble, naming it"
RC_TEXT=0; "$GATE" check "$MISSING" >"$TMP/p-miss.txt" 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq 2 ]] && grep -q "REFUSED" "$TMP/p-miss.txt"
assert $? "prose: check refuses an unreadable file"
! grep -q "migration-preamble-verdict" "$TMP/p-good.txt" "$TMP/p-bare.txt" 2>/dev/null
assert $? "prose: default output carries no verdict object"
RC_TEXT=0; "$GATE" check "$CUSTOM" >/dev/null 2>&1 || RC_TEXT=$?
[[ "$RC_TEXT" -eq 0 ]]; assert $? "prose: presence still ignores timeout values"

echo "== usage and contract note =="
RC_TEXT=0; "$GATE" >"$TMP/usage.txt" 2>&1 || RC_TEXT=$?
grep -q -- "--json" "$TMP/usage.txt" 2>/dev/null
assert $? "usage: usage text mentions --json"
grep -q "migration-preamble-verdict.v1" "$DOCS" 2>/dev/null
assert $? "docs: contract note names the verdict schema"
grep -q "test_migration_preamble_check_verdicts.sh" "$DOCS" 2>/dev/null
assert $? "docs: contract note names this suite's focused command"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
