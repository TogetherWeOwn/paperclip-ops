#!/usr/bin/env bash
# Acceptance suite for public_comment_hygiene_gate.sh --json (hygiene-json).
#
# THE CONTRACT THIS PINS. The text gate prints `VIOLATION [...]` lines plus a
# `VERDICT:` line to stderr so a human can read it, but an agent about to post
# on a public repo must scrape that prose to learn WHICH internal IDs on WHICH
# body lines (or branch) violate policy. The --json flag adds a machine
# surface: exactly one JSON document on stdout per invocation:
#
#   { "schema_version": 1,
#     "verdict": "pass" | "violation" | "error",
#     "visibility": "<echo of --visibility>",
#     "context": "<echo of --context, default comment>",
#     "findings": [ { "source": "body", "line": 2, "matched": ["<PREFIX>-<digits>"] },
#                   { "source": "branch", "matched": ["<PREFIX>-<digits>"] } ],
#     "truncated": 0,
#     "error": { "code": "ok", "retryable": false, "next_action": "<token>" } }
#
# Exit codes never change: 0 pass, 1 violation, 2 usage/scan-failure. Verdict
# maps 1:1 to the exit (pass/0, violation/1, error/2) and text/JSON verdicts
# agree on every fixture. Default text output stays byte-identical: text mode
# writes nothing to stdout, and --json is the only new flag.
#
# Findings carry matched IDs + line numbers ONLY -- never the full body text.
# The list is bounded: a documented cap with a truncated count so an agent can
# tell a complete finding list from a cut one (len + truncated == total hits).
# Any scanner failure still refuses exit 2 with a stable machine code in JSON
# -- never PASS after a scan failure. The prefix set still honors
# INTERNAL_ID_PREFIXES so the two gates never disagree.
#
# HERMETIC BY CONSTRUCTION. Disposable temp fixtures with synthetic IDs only
# (prefix grammar via concatenation, never a literal tenant ID). No network,
# no credentials, no live board, no production data. Requires bash, jq and
# python3. A missing dependency is a hard error, never a skip.
#
# RED/GREEN. On the base commit this suite is RED (--json is an unknown flag).
# After the paired build it must be GREEN without any edit to this file: the
# Builder builds on this branch and never edits these test files. The existing
# text suite runs as a regression control at the end and must stay GREEN.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/public_comment_hygiene_gate.sh"
TEXT_SUITE="$HERE/test_public_comment_hygiene_gate.sh"
CLEAN_PATH="/usr/bin:/bin"
ENV_BIN="$(PATH="$CLEAN_PATH" type -P env)"
BASH_BIN="$(PATH="$CLEAN_PATH" type -P bash)"
PASS=0; FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ ! -x "$GATE" ]]; then
  echo "ERROR: $GATE not found or not executable" >&2
  exit 2
fi
if [[ -z "$ENV_BIN" || -z "$BASH_BIN" ]]; then
  echo "ERROR: env and bash are required on $CLEAN_PATH" >&2
  exit 2
fi
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required" >&2; exit 2; }
if [[ ! -x "$TEXT_SUITE" ]]; then
  echo "ERROR: $TEXT_SUITE not found or not executable (regression control)" >&2
  exit 2
fi
chmod +x "$GATE"

# Gate subprocesses must not inherit exported functions or BASH_ENV from the
# shell running this suite. Only the scanner-failure and prefix-override cases
# vary PATH or the environment, and those paths are still built from the known
# system directories above.
run_gate() {
  "$ENV_BIN" -i "PATH=$CLEAN_PATH" "$BASH_BIN" --noprofile --norc "$GATE" "$@"
}
run_gate_with_path() {
  local path="$1"; shift
  "$ENV_BIN" -i "PATH=$path" "$BASH_BIN" --noprofile --norc "$GATE" "$@"
}
run_gate_with_prefixes() {
  local prefixes="$1"; shift
  "$ENV_BIN" -i "PATH=$CLEAN_PATH" "INTERNAL_ID_PREFIXES=$prefixes" \
    "$BASH_BIN" --noprofile --norc "$GATE" "$@"
}

ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1"; [[ -n "${2:-}" ]] && printf '     %s\n' "$2"; }

# Synthetic IDs retain the real prefix grammar, never tenant IDs. Prefixes are
# built by concatenation so this file holds no literal internal ID.
PRIMARY_PREFIX="TO""G"
SECONDARY_PREFIX="PA""P"
TERTIARY_PREFIX="PA""PA"
CUSTOM_PREFIX="ACME"
T1="${PRIMARY_PREFIX}-9910001"
T2="${PRIMARY_PREFIX}-9910002"
T3="${PRIMARY_PREFIX}-9910003"
P1="${SECONDARY_PREFIX}-9910004"
PA1="${TERTIARY_PREFIX}-9910005"
C1="${CUSTOM_PREFIX}-9910006"

# --- JSON helpers ---------------------------------------------------------
# assert_one_json_doc <file> <label>: stdout holds exactly one JSON document.
assert_one_json_doc() {
  local file="$1" label="$2" diag
  diag="$(python3 - "$file" <<'PY' 2>&1
import json, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
if not text.strip():
    print("stdout is empty, want exactly one JSON document")
    sys.exit(1)
dec = json.JSONDecoder()
try:
    _, idx = dec.raw_decode(text.strip())
except ValueError as exc:
    print(f"stdout is not valid JSON: {exc}")
    sys.exit(1)
rest = text.strip()[idx:].strip()
if rest:
    print(f"trailing content after JSON document: {rest[:80]!r}")
    sys.exit(1)
PY
)"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then ok "$label";
  else bad "$label" "$diag"; fi
}

# json_ok <file> <jq-filter> <label> [diag]: jq filter must succeed.
json_ok() {
  local file="$1" filter="$2" label="$3" diag="${4:-}"
  if jq -e "$filter" "$file" >/dev/null 2>&1; then ok "$label";
  else bad "$label" "${diag:-filter failed: $filter :: $(head -c 300 "$file" 2>/dev/null)}"; fi
}

# capture_text / capture_json run the gate with stdout+stderr separated.
# Sets CAPTURE_RC; stdout in $WORK/<tag>.stdout, stderr in $WORK/<tag>.stderr.
capture_text() {
  local tag="$1"; shift
  rm -f "$WORK/$tag.stdout" "$WORK/$tag.stderr"
  run_gate "$@" >"$WORK/$tag.stdout" 2>"$WORK/$tag.stderr"; CAPTURE_RC=$?
}
capture_json() {
  local tag="$1"; shift
  rm -f "$WORK/$tag.stdout" "$WORK/$tag.stderr"
  run_gate "$@" --json >"$WORK/$tag.stdout" 2>"$WORK/$tag.stderr"; CAPTURE_RC=$?
}
capture_json_with_prefixes() {
  local tag="$1" prefixes="$2"; shift 2
  rm -f "$WORK/$tag.stdout" "$WORK/$tag.stderr"
  run_gate_with_prefixes "$prefixes" "$@" --json >"$WORK/$tag.stdout" 2>"$WORK/$tag.stderr"; CAPTURE_RC=$?
}
capture_json_with_path() {
  local tag="$1" path="$2"; shift 2
  rm -f "$WORK/$tag.stdout" "$WORK/$tag.stderr"
  run_gate_with_path "$path" "$@" --json >"$WORK/$tag.stdout" 2>"$WORK/$tag.stderr"; CAPTURE_RC=$?
}
capture_text_with_prefixes() {
  local tag="$1" prefixes="$2"; shift 2
  rm -f "$WORK/$tag.stdout" "$WORK/$tag.stderr"
  run_gate_with_prefixes "$prefixes" "$@" >"$WORK/$tag.stdout" 2>"$WORK/$tag.stderr"; CAPTURE_RC=$?
}
capture_text_with_path() {
  local tag="$1" path="$2"; shift 2
  rm -f "$WORK/$tag.stdout" "$WORK/$tag.stderr"
  run_gate_with_path "$path" "$@" >"$WORK/$tag.stdout" 2>"$WORK/$tag.stderr"; CAPTURE_RC=$?
}

# check_parity <label> <text-rc> <json_rc> <expected_rc>: exits agree.
check_parity() {
  local label="$1" text_rc="$2" json_rc="$3" expected="$4"
  if [[ "$text_rc" == "$expected" && "$json_rc" == "$expected" ]]; then
    ok "$label (exit $expected both modes)"
  else
    bad "$label" "want exit $expected in both modes, got text=$text_rc json=$json_rc"
  fi
}

# Required envelope fields for every JSON invocation (pass/violation/error).
check_envelope() {
  local tag="$1" label="$2"
  local f="$WORK/$tag.stdout"
  assert_one_json_doc "$f" "$label emits exactly one JSON document on stdout"
  json_ok "$f" '.schema_version == 1' "$label carries schema_version 1"
  json_ok "$f" '.verdict == "pass" or .verdict == "violation" or .verdict == "error"' \
    "$label verdict is pass|violation|error"
  json_ok "$f" '.visibility | type == "string" and length > 0' "$label echoes visibility"
  json_ok "$f" '.context | type == "string" and length > 0' "$label echoes context label"
  json_ok "$f" '.findings | type == "array"' "$label findings is an array"
  json_ok "$f" '.truncated | type == "number" and . >= 0 and floor == .' "$label truncated is a non-negative integer"
  json_ok "$f" '.error.code | type == "string" and length > 0' "$label error.code is a non-empty string"
  json_ok "$f" '.error.retryable == false' "$label error.retryable is false"
  json_ok "$f" '.error.next_action | type == "string" and length > 0' "$label error.next_action names a next step"
  json_ok "$f" '[.findings[] | select(.source != "body" and .source != "branch")] | length == 0' \
    "$label finding sources are body|branch only"
  json_ok "$f" '[.findings[] | select(.source == "body" and (has("line") | not))] | length == 0' \
    "$label every body finding carries a line number"
  json_ok "$f" '[.findings[] | select(.source == "branch" and has("line"))] | length == 0' \
    "$label branch findings carry no line number"
  json_ok "$f" '[.findings[] | select(.matched | type != "array" or length == 0)] | length == 0' \
    "$label every finding carries a non-empty matched ID array"
}

# Flatten findings to sorted "line:id" / "branch:id" rows for set comparison.
findings_rows() {
  jq -r '.findings[] | if .source == "body" then "\(.line):\(.matched[])" else "branch:\(.matched[])" end' "$1" 2>/dev/null | sort
}

MARKER="zebra-body-marker-99100"

# --- 1. usage/help and header document the JSON contract --------------------
HELP_OUT="$(run_gate --help 2>&1)"; HELP_RC=$?
[[ "$HELP_RC" -eq 0 ]] && ok "help exits 0" || bad "help exits 0" "got $HELP_RC"
for token in --json verdict findings truncated schema_version; do
  if grep -qF -- "$token" <<<"$HELP_OUT"; then ok "help documents $token";
  else bad "help documents $token" "usage text has no $token"; fi
done
for token in schema_version verdict findings truncated; do
  if grep -qF -- "$token" "$GATE"; then ok "gate source documents $token";
  else bad "gate source documents $token" "header/contract has no $token"; fi
done

# --- 2. multi-line body hits with exact line numbers -------------------------
cat > "$WORK/multi.txt" <<EOF
Review update $MARKER-multi: all green so far.
Fixes belong to the owner of $T1; re-check here after repair.
No IDs on this line.
See $P1 for context, and also $T2 here.
Trailing clean line.
EOF
capture_text multi-text --visibility public --body-file "$WORK/multi.txt"; TEXT_RC=$CAPTURE_RC
capture_json multi-json --visibility public --body-file "$WORK/multi.txt"; JSON_RC=$CAPTURE_RC
check_parity "multi-line body hits fire" "$TEXT_RC" "$JSON_RC" 1
check_envelope multi-json "multi-line body hits"
json_ok "$WORK/multi-json.stdout" '.verdict == "violation"' "multi-line verdict is violation"
json_ok "$WORK/multi-json.stdout" '.visibility == "public"' "multi-line visibility echoes public"
json_ok "$WORK/multi-json.stdout" '.context == "comment"' "multi-line default context is comment"
json_ok "$WORK/multi-json.stdout" '.truncated == 0' "multi-line truncated is 0"
json_ok "$WORK/multi-json.stdout" '.error.code == "ok"' "multi-line error.code is ok"
findings_rows "$WORK/multi-json.stdout" > "$WORK/multi.actual"
printf '%s\n' "2:$T1" "4:$P1" "4:$T2" | sort > "$WORK/multi.expected"
if cmp -s "$WORK/multi.actual" "$WORK/multi.expected"; then
  ok "multi-line findings pin exact line numbers and matched IDs"
else
  bad "multi-line findings pin exact line numbers and matched IDs" \
    "want $(tr '\n' ' ' <"$WORK/multi.expected") got $(tr '\n' ' ' <"$WORK/multi.actual")"
fi
if grep -qF -- "$MARKER-multi" "$WORK/multi-json.stdout"; then
  bad "multi-line JSON carries no body text" "distinctive body marker echoed in JSON"
else
  ok "multi-line JSON carries no body text"
fi
[[ ! -s "$WORK/multi-text.stdout" ]] && ok "multi-line text mode writes no stdout" \
  || bad "multi-line text mode writes no stdout" "text stdout must stay empty"

# --- 3. branch-only hits ------------------------------------------------------
printf '%s' "Review abc1234: CHANGES. Fixes belong to the PR author; re-check here." > "$WORK/clean.txt"
capture_text branch-text --visibility public --body-file "$WORK/clean.txt" --branch "fix/$T3-scrub"; TEXT_RC=$CAPTURE_RC
capture_json branch-json --visibility public --body-file "$WORK/clean.txt" --branch "fix/$T3-scrub"; JSON_RC=$CAPTURE_RC
check_parity "branch-only hit fires" "$TEXT_RC" "$JSON_RC" 1
check_envelope branch-json "branch-only hit"
json_ok "$WORK/branch-json.stdout" '.verdict == "violation"' "branch-only verdict is violation"
findings_rows "$WORK/branch-json.stdout" > "$WORK/branch.actual"
printf '%s\n' "branch:$T3" > "$WORK/branch.expected"
if cmp -s "$WORK/branch.actual" "$WORK/branch.expected"; then
  ok "branch-only finding pins the matched branch ID"
else
  bad "branch-only finding pins the matched branch ID" \
    "want $(cat "$WORK/branch.expected") got $(cat "$WORK/branch.actual")"
fi
json_ok "$WORK/branch-json.stdout" '[.findings[] | select(.source == "body")] | length == 0' \
  "branch-only findings carry no body rows"

# --- 4. combined body+branch hits ----------------------------------------------
printf '%s' "Fixes belong to the owner of $T1." > "$WORK/combo.txt"
capture_text combo-text --visibility public --body-file "$WORK/combo.txt" --branch "fix/$T2-ship"; TEXT_RC=$CAPTURE_RC
capture_json combo-json --visibility public --body-file "$WORK/combo.txt" --branch "fix/$T2-ship"; JSON_RC=$CAPTURE_RC
check_parity "combined body+branch hits fire" "$TEXT_RC" "$JSON_RC" 1
check_envelope combo-json "combined body+branch hits"
json_ok "$WORK/combo-json.stdout" '.verdict == "violation"' "combined verdict is violation"
findings_rows "$WORK/combo-json.stdout" > "$WORK/combo.actual"
printf '%s\n' "1:$T1" "branch:$T2" | sort > "$WORK/combo.expected"
if cmp -s "$WORK/combo.actual" "$WORK/combo.expected"; then
  ok "combined findings pin both body and branch sources"
else
  bad "combined findings pin both body and branch sources" \
    "want $(tr '\n' ' ' <"$WORK/combo.expected") got $(tr '\n' ' ' <"$WORK/combo.actual")"
fi

# --- 5. TOGETHER/PAPER negative trails pass ------------------------------------
TOGETHER_WORD="${PRIMARY_PREFIX}ETHERWEOWN"
PAPER_WORD="${SECONDARY_PREFIX}ER"
cat > "$WORK/negative.txt" <<EOF
From togetherweown[bot] via paper trail.
Upstream $TOGETHER_WORD and $PAPER_WORD trails are not IDs.
${PRIMARY_PREFIX}ETHER-123 and ${SECONDARY_PREFIX}ER-123 look close but match nothing.
EOF
capture_text neg-text --visibility public --body-file "$WORK/negative.txt"; TEXT_RC=$CAPTURE_RC
capture_json neg-json --visibility public --body-file "$WORK/negative.txt"; JSON_RC=$CAPTURE_RC
check_parity "TOGETHERWEOWN/PAPER trails pass" "$TEXT_RC" "$JSON_RC" 0
check_envelope neg-json "TOGETHERWEOWN/PAPER trails"
json_ok "$WORK/neg-json.stdout" '.verdict == "pass" and .findings == [] and .truncated == 0' \
  "negative trails yield pass with no findings"
json_ok "$WORK/neg-json.stdout" '.error.code == "ok"' "negative trails error.code is ok"

# --- 6. private-repo pass (IDs belong there; no scanning) ----------------------
cat > "$WORK/priv.txt" <<EOF
Fixes belong to the owner of $T1; see $P1 too.
EOF
capture_text priv-text --visibility private --body-file "$WORK/priv.txt" --branch "fix/$T2-ship"; TEXT_RC=$CAPTURE_RC
capture_json priv-json --visibility private --body-file "$WORK/priv.txt" --branch "fix/$T2-ship"; JSON_RC=$CAPTURE_RC
check_parity "private body+branch with IDs passes" "$TEXT_RC" "$JSON_RC" 0
check_envelope priv-json "private pass"
json_ok "$WORK/priv-json.stdout" '.verdict == "pass" and .visibility == "private" and .findings == [] and .truncated == 0' \
  "private pass carries empty findings without scanning"
# Private must pass even when the scanner pattern is malformed: nothing is scanned.
capture_text_with_prefixes privbad-text '[' --visibility private --body-file "$WORK/priv.txt"; TEXT_RC=$CAPTURE_RC
capture_json_with_prefixes privbad-json '[' --visibility private --body-file "$WORK/priv.txt"; JSON_RC=$CAPTURE_RC
check_parity "private pass skips the scanner" "$TEXT_RC" "$JSON_RC" 0
json_ok "$WORK/privbad-json.stdout" '.verdict == "pass"' "private malformed-pattern verdict stays pass" 2>/dev/null \
  || bad "private malformed-pattern verdict stays pass" "want verdict pass :: $(head -c 200 "$WORK/privbad-json.stdout" 2>/dev/null)"

# --- 7. unreadable body file refuses -------------------------------------------
printf '%s' "Fixes belong to the owner of $T1." > "$WORK/noperm.txt"
chmod 000 "$WORK/noperm.txt"
if [[ -r "$WORK/noperm.txt" ]]; then
  bad "unreadable fixture is actually unreadable" "chmod 000 left the file readable (privileged user?)"
else
  ok "unreadable fixture is actually unreadable"
fi
capture_text noperm-text --visibility public --body-file "$WORK/noperm.txt"; TEXT_RC=$CAPTURE_RC
capture_json noperm-json --visibility public --body-file "$WORK/noperm.txt"; JSON_RC=$CAPTURE_RC
check_parity "unreadable body refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope noperm-json "unreadable body"
json_ok "$WORK/noperm-json.stdout" '.verdict == "error" and .findings == []' \
  "unreadable body verdict is error with no findings"
json_ok "$WORK/noperm-json.stdout" '.error.code != "ok"' "unreadable body carries a stable machine code"
CODE_A="$(jq -r '.error.code // empty' "$WORK/noperm-json.stdout" 2>/dev/null)"
capture_json noperm-json2 --visibility public --body-file "$WORK/noperm.txt"
CODE_B="$(jq -r '.error.code // empty' "$WORK/noperm-json2.stdout" 2>/dev/null)"
if [[ -n "$CODE_A" && "$CODE_A" == "$CODE_B" ]]; then ok "unreadable machine code is stable across runs";
else bad "unreadable machine code is stable across runs" "got '$CODE_A' then '$CODE_B'"; fi
chmod 644 "$WORK/noperm.txt"

# --- 8. non-regular body files refuse -------------------------------------------
mkdir -p "$WORK/adir"
capture_text dir-text --visibility public --body-file "$WORK/adir"; TEXT_RC=$CAPTURE_RC
capture_json dir-json --visibility public --body-file "$WORK/adir"; JSON_RC=$CAPTURE_RC
check_parity "directory body refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope dir-json "directory body"
json_ok "$WORK/dir-json.stdout" '.verdict == "error"' "directory body verdict is error"
ln -sf "$WORK/does-not-exist-99100.txt" "$WORK/dangling.txt"
capture_text dang-text --visibility public --body-file "$WORK/dangling.txt"; TEXT_RC=$CAPTURE_RC
capture_json dang-json --visibility public --body-file "$WORK/dangling.txt"; JSON_RC=$CAPTURE_RC
check_parity "dangling symlink body refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope dang-json "dangling symlink body"
json_ok "$WORK/dang-json.stdout" '.verdict == "error" and .error.code != "ok"' \
  "dangling symlink verdict is error with a machine code"

# --- 9. invalid visibility refuses ----------------------------------------------
printf '%s' "clean body" > "$WORK/x.txt"
capture_text vis-text --visibility staging --body-file "$WORK/x.txt"; TEXT_RC=$CAPTURE_RC
capture_json vis-json --visibility staging --body-file "$WORK/x.txt"; JSON_RC=$CAPTURE_RC
check_parity "invalid visibility refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope vis-json "invalid visibility"
json_ok "$WORK/vis-json.stdout" '.verdict == "error" and .visibility == "staging"' \
  "invalid visibility echoes the bad value with verdict error"

# --- 10. INTERNAL_ID_PREFIXES override agreement --------------------------------
printf '%s' "See $C1 for context." > "$WORK/custom-hit.txt"
printf '%s' "Fixes belong to the owner of $T1." > "$WORK/custom-miss.txt"
printf '%s' "See $PA1 for context." > "$WORK/papa-hit.txt"
# Custom set fires on its own IDs in both modes.
capture_text_with_prefixes cust-text "$CUSTOM_PREFIX" --visibility public --body-file "$WORK/custom-hit.txt"; TEXT_RC=$CAPTURE_RC
capture_json_with_prefixes cust-json "$CUSTOM_PREFIX" --visibility public --body-file "$WORK/custom-hit.txt"; JSON_RC=$CAPTURE_RC
check_parity "custom prefix hit fires" "$TEXT_RC" "$JSON_RC" 1
check_envelope cust-json "custom prefix hit"
findings_rows "$WORK/cust-json.stdout" > "$WORK/cust.actual"
printf '%s\n' "1:$C1" > "$WORK/cust.expected"
cmp -s "$WORK/cust.actual" "$WORK/cust.expected" \
  && ok "custom prefix finding pins the matched custom ID" \
  || bad "custom prefix finding pins the matched custom ID" \
    "want $(cat "$WORK/cust.expected") got $(cat "$WORK/cust.actual")"
# The same override stops matching the default set in both modes.
capture_text_with_prefixes custmiss-text "$CUSTOM_PREFIX" --visibility public --body-file "$WORK/custom-miss.txt"; TEXT_RC=$CAPTURE_RC
capture_json_with_prefixes custmiss-json "$CUSTOM_PREFIX" --visibility public --body-file "$WORK/custom-miss.txt"; JSON_RC=$CAPTURE_RC
check_parity "custom prefix override ignores default IDs" "$TEXT_RC" "$JSON_RC" 0
json_ok "$WORK/custmiss-json.stdout" '.verdict == "pass" and .findings == []' \
  "override miss yields pass with no findings"
# Default set still matches the third prefix in both modes.
capture_text papa-text --visibility public --body-file "$WORK/papa-hit.txt"; TEXT_RC=$CAPTURE_RC
capture_json papa-json --visibility public --body-file "$WORK/papa-hit.txt"; JSON_RC=$CAPTURE_RC
check_parity "default third-prefix hit fires" "$TEXT_RC" "$JSON_RC" 1
findings_rows "$WORK/papa-json.stdout" > "$WORK/papa.actual"
printf '%s\n' "1:$PA1" > "$WORK/papa.expected"
cmp -s "$WORK/papa.actual" "$WORK/papa.expected" \
  && ok "third-prefix finding pins the matched ID" \
  || bad "third-prefix finding pins the matched ID" \
    "want $(cat "$WORK/papa.expected") got $(cat "$WORK/papa.actual")"

# --- 11. bounded findings cap + truncated count ---------------------------------
# 100 distinct single-hit lines: any reasonable documented cap (<100) truncates.
: > "$WORK/many.txt"
for i in $(seq 1 100); do
  printf 'Line %d needs %s-%s for review.\n' "$i" "$PRIMARY_PREFIX" "$(printf '992%04d' "$i")" >> "$WORK/many.txt"
done
capture_text many-text --visibility public --body-file "$WORK/many.txt"; TEXT_RC=$CAPTURE_RC
capture_json many-json --visibility public --body-file "$WORK/many.txt"; JSON_RC=$CAPTURE_RC
check_parity "many-hit body fires" "$TEXT_RC" "$JSON_RC" 1
check_envelope many-json "many-hit body"
json_ok "$WORK/many-json.stdout" '.verdict == "violation"' "many-hit verdict is violation"
LEN="$(jq -r '.findings | length' "$WORK/many-json.stdout" 2>/dev/null)"
TRUNC="$(jq -r '.truncated' "$WORK/many-json.stdout" 2>/dev/null)"
if [[ "$LEN" =~ ^[0-9]+$ && "$TRUNC" =~ ^[0-9]+$ && "$LEN" -lt 100 && "$TRUNC" -gt 0 && $((LEN + TRUNC)) -eq 100 ]]; then
  ok "many-hit findings are bounded with an exact truncated count (len=$LEN truncated=$TRUNC)"
else
  bad "many-hit findings are bounded with an exact truncated count" "want len<100, truncated>0, len+truncated==100; got len=$LEN truncated=$TRUNC"
fi
if grep -qiE 'cap|truncat' <<<"$HELP_OUT" && grep -qE '[0-9]+' <<<"$HELP_OUT"; then
  ok "help documents the findings cap"
else
  bad "help documents the findings cap" "usage names no numeric cap/truncation rule"
fi

# --- 12. context label echoes ----------------------------------------------------
printf '%s' "Fixes belong to the owner of $T1." > "$WORK/ctx.txt"
capture_text ctx-text --visibility public --body-file "$WORK/ctx.txt" --context review-submit; TEXT_RC=$CAPTURE_RC
capture_json ctx-json --visibility public --body-file "$WORK/ctx.txt" --context review-submit; JSON_RC=$CAPTURE_RC
check_parity "custom context hit fires" "$TEXT_RC" "$JSON_RC" 1
json_ok "$WORK/ctx-json.stdout" '.context == "review-submit"' "custom context echoes in JSON"
grep -qF '[review-submit' "$WORK/ctx-text.stderr" \
  && ok "custom context echoes in text stderr" \
  || bad "custom context echoes in text stderr" "text failure line lacks [review-submit"

# --- 13. --json works in any flag position ---------------------------------------
for pos in first last; do
  rm -f "$WORK/pos.stdout" "$WORK/pos.stderr"
  if [[ "$pos" == "first" ]]; then
    run_gate --json --visibility public --body-file "$WORK/ctx.txt" >"$WORK/pos.stdout" 2>"$WORK/pos.stderr"; POS_RC=$?
  else
    run_gate --visibility public --body-file "$WORK/ctx.txt" --json >"$WORK/pos.stdout" 2>"$WORK/pos.stderr"; POS_RC=$?
  fi
  if [[ "$POS_RC" -eq 1 ]] && jq -e '.verdict == "violation"' "$WORK/pos.stdout" >/dev/null 2>&1; then
    ok "--json $pos position yields a violation envelope"
  else
    bad "--json $pos position yields a violation envelope" "got exit $POS_RC"
  fi
done

# --- 14. scanner failures refuse in JSON too (never PASS) ------------------------
printf '%s' "clean body for scanner tests" > "$WORK/scan-clean.txt"
# 14a. malformed pattern breaks the scan -> 2 with a machine code.
capture_text_with_prefixes badpat-text '[' --visibility public --body-file "$WORK/scan-clean.txt"; TEXT_RC=$CAPTURE_RC
capture_json_with_prefixes badpat-json '[' --visibility public --body-file "$WORK/scan-clean.txt"; JSON_RC=$CAPTURE_RC
check_parity "malformed pattern refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope badpat-json "malformed pattern"
json_ok "$WORK/badpat-json.stdout" '.verdict == "error" and .error.code != "ok"' \
  "malformed pattern verdict is error with a machine code"
# 14b. injected body-scanner failure -> 2 with a machine code.
mkdir -p "$WORK/shim_all"
printf '#!/usr/bin/env bash\necho "simulated grep failure" >&2\nexit 2\n' > "$WORK/shim_all/grep"
chmod +x "$WORK/shim_all/grep"
capture_text_with_path shimbody-text "$WORK/shim_all:$CLEAN_PATH" --visibility public --body-file "$WORK/scan-clean.txt"; TEXT_RC=$CAPTURE_RC
capture_json_with_path shimbody-json "$WORK/shim_all:$CLEAN_PATH" --visibility public --body-file "$WORK/scan-clean.txt"; JSON_RC=$CAPTURE_RC
check_parity "body scanner error refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope shimbody-json "body scanner error"
json_ok "$WORK/shimbody-json.stdout" '.verdict == "error" and .error.code != "ok"' \
  "body scanner error verdict is error with a machine code"
# 14c. branch-path scanner failure -> 2 with a machine code.
REAL_GREP="$(PATH="$CLEAN_PATH" type -P grep)"
mkdir -p "$WORK/shim_branch"
printf '#!/usr/bin/env bash\nfor a in "$@"; do if [[ "$a" == -*n* ]]; then exec "%s" "$@"; fi; done\necho "simulated branch-scan failure" >&2\nexit 2\n' "$REAL_GREP" > "$WORK/shim_branch/grep"
chmod +x "$WORK/shim_branch/grep"
capture_text_with_path shimbranch-text "$WORK/shim_branch:$CLEAN_PATH" --visibility public --body-file "$WORK/scan-clean.txt" --branch "fix/scrub-bot-comments"; TEXT_RC=$CAPTURE_RC
capture_json_with_path shimbranch-json "$WORK/shim_branch:$CLEAN_PATH" --visibility public --body-file "$WORK/scan-clean.txt" --branch "fix/scrub-bot-comments"; JSON_RC=$CAPTURE_RC
check_parity "branch scanner error refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope shimbranch-json "branch scanner error"
json_ok "$WORK/shimbranch-json.stdout" '.verdict == "error" and .error.code != "ok"' \
  "branch scanner error verdict is error with a machine code"

# --- 15. JSON encoder failure preserves escaped context and refuses --------------
# Catch a fallback that emits malformed/partial JSON, drops context characters,
# or reports success when jq fails. Only gate subprocesses see the failing jq;
# envelope assertions below still use the real system encoder/parser.
mkdir -p "$WORK/shim_jq"
printf '#!/usr/bin/env bash\necho "simulated jq failure" >&2\nexit 2\n' > "$WORK/shim_jq/jq"
chmod +x "$WORK/shim_jq/jq"
JQ_FAILURE_CONTEXT=$'quote " backslash \\ tab\tnewline\ntrailing newline\n'
capture_json_with_path jqfail-json "$WORK/shim_jq:$CLEAN_PATH" \
  --visibility public --body-file "$WORK/scan-clean.txt" --context "$JQ_FAILURE_CONTEXT"
if [[ "$CAPTURE_RC" -eq 2 ]]; then
  ok "JSON encoder failure refuses (exit 2)"
else
  bad "JSON encoder failure refuses" "want exit 2, got $CAPTURE_RC"
fi
check_envelope jqfail-json "JSON encoder failure"
json_ok "$WORK/jqfail-json.stdout" '.verdict == "error" and .error.code == "json_encoding_failed"' \
  "JSON encoder failure reports its stable error code"
json_ok "$WORK/jqfail-json.stdout" '.visibility == "public" and .findings == [] and .truncated == 0' \
  "JSON encoder failure preserves visibility with no findings"
if jq -e --arg expected "$JQ_FAILURE_CONTEXT" '.context == $expected' "$WORK/jqfail-json.stdout" >/dev/null 2>&1; then
  ok "JSON encoder failure round-trips quote, backslash, tab and newlines in context"
else
  bad "JSON encoder failure round-trips quote, backslash, tab and newlines in context" \
    "decoded context differs from the input (including its trailing newline)"
fi

# --- 16. binary body with an ID refuses as a scanner failure ---------------------
# grep reports a binary match with no line/ID data, so the JSON gate must
# refuse that incomplete scan as body_scan_failed / fix_scanner -- never PASS,
# and never degrade to the serializer code (json_encoding_failed).
printf 'Fixes belong to %s\000after a NUL byte.\n' "$T1" > "$WORK/binary.txt"
if python3 -c 'import sys; sys.exit(0 if b"\x00" in open(sys.argv[1], "rb").read() else 1)' "$WORK/binary.txt"; then
  ok "binary fixture actually carries a NUL byte"
else
  bad "binary fixture actually carries a NUL byte" "printf octal escape produced no NUL"
fi
capture_text binary-text --visibility public --body-file "$WORK/binary.txt"; TEXT_RC=$CAPTURE_RC
capture_json binary-json --visibility public --body-file "$WORK/binary.txt"; JSON_RC=$CAPTURE_RC
if [[ "$JSON_RC" -eq 2 ]]; then
  ok "binary body JSON mode refuses (exit 2)"
else
  bad "binary body JSON mode refuses" "want exit 2, got $JSON_RC"
fi
if [[ "$TEXT_RC" -eq 1 ]]; then
  ok "binary body text mode never passes (violation, historical rendering)"
else
  bad "binary body text mode never passes" "want exit 1, got $TEXT_RC"
fi
check_envelope binary-json "binary body"
json_ok "$WORK/binary-json.stdout" '.verdict == "error" and .findings == [] and .truncated == 0' \
  "binary body verdict is error with no findings"
json_ok "$WORK/binary-json.stdout" '.error.code == "body_scan_failed"' \
  "binary body reports the scanner code, not the serializer code"
json_ok "$WORK/binary-json.stdout" '.error.next_action == "fix_scanner"' \
  "binary body next action is fix_scanner"

# --- 17. error envelope clears partial findings -----------------------------------
# A body hit is recorded before the branch scan runs; when that scan fails the
# error envelope must carry findings: [] and truncated 0, never the partial list.
printf '%s' "Fixes belong to the owner of $T1." > "$WORK/reset-hit.txt"
# Control: the same body really does produce a finding when nothing fails.
capture_json reset-control --visibility public --body-file "$WORK/reset-hit.txt" --branch "fix/scrub-bot-comments"
json_ok "$WORK/reset-control.stdout" '.verdict == "violation" and (.findings | length) == 1' \
  "error-reset control body produces one finding without a failure"
capture_text_with_path reset-text "$WORK/shim_branch:$CLEAN_PATH" --visibility public --body-file "$WORK/reset-hit.txt" --branch "fix/scrub-bot-comments"; TEXT_RC=$CAPTURE_RC
capture_json_with_path reset-json "$WORK/shim_branch:$CLEAN_PATH" --visibility public --body-file "$WORK/reset-hit.txt" --branch "fix/scrub-bot-comments"; JSON_RC=$CAPTURE_RC
check_parity "body hit plus failing branch scan refuses" "$TEXT_RC" "$JSON_RC" 2
check_envelope reset-json "error envelope after a recorded body hit"
json_ok "$WORK/reset-json.stdout" '.verdict == "error"' \
  "body hit plus failing branch scan verdict is error"
json_ok "$WORK/reset-json.stdout" '.findings == [] and .truncated == 0' \
  "error envelope clears the recorded body finding and truncated count"
json_ok "$WORK/reset-json.stdout" '.error.code == "branch_scan_failed" and .error.next_action == "fix_scanner"' \
  "failing branch scan reports branch_scan_failed / fix_scanner"

# --- 18. findings cap is a total across body and branch ---------------------------
# 60 single-hit body lines + 2 branch IDs = 62 occurrences: exactly the 50-cap
# in body-first scan order, with 12 omitted -- not 50 body + 2 branch.
: > "$WORK/cap60.txt"
for i in $(seq 1 60); do
  printf 'Line %d needs %s-%s for review.\n' "$i" "$PRIMARY_PREFIX" "$(printf '993%04d' "$i")" >> "$WORK/cap60.txt"
done
CAP_BRANCH="fix/${PRIMARY_PREFIX}-9930061-and-${PRIMARY_PREFIX}-9930062"
capture_text cap-text --visibility public --body-file "$WORK/cap60.txt" --branch "$CAP_BRANCH"; TEXT_RC=$CAPTURE_RC
capture_json cap-json --visibility public --body-file "$WORK/cap60.txt" --branch "$CAP_BRANCH"; JSON_RC=$CAPTURE_RC
check_parity "cross-source capped body+branch hits fire" "$TEXT_RC" "$JSON_RC" 1
check_envelope cap-json "cross-source capped hits"
json_ok "$WORK/cap-json.stdout" '.verdict == "violation"' "cross-source capped verdict is violation"
CAP_LEN="$(jq -r '.findings | length' "$WORK/cap-json.stdout" 2>/dev/null)"
CAP_TRUNC="$(jq -r '.truncated' "$WORK/cap-json.stdout" 2>/dev/null)"
if [[ "$CAP_LEN" == "50" && "$CAP_TRUNC" == "12" ]]; then
  ok "cross-source cap keeps exactly 50 findings with 12 omitted"
else
  bad "cross-source cap keeps exactly 50 findings with 12 omitted" "want len=50 truncated=12; got len=$CAP_LEN truncated=$CAP_TRUNC"
fi
CAP_FIRST="${PRIMARY_PREFIX}-$(printf '993%04d' 1)"
CAP_50TH="${PRIMARY_PREFIX}-$(printf '993%04d' 50)"
if jq -e --arg first "$CAP_FIRST" --arg last "$CAP_50TH" \
  '.findings[0] == {source: "body", matched: [$first], line: 1}
   and .findings[49] == {source: "body", matched: [$last], line: 50}
   and ([.findings[] | select(.source == "branch")] | length) == 0' \
  "$WORK/cap-json.stdout" >/dev/null 2>&1; then
  ok "cross-source cap retains body lines 1-50 in scan order before any branch hit"
else
  bad "cross-source cap retains body lines 1-50 in scan order before any branch hit" \
    "want first=$CAP_FIRST line 1, 50th=$CAP_50TH line 50, no branch rows :: $(head -c 300 "$WORK/cap-json.stdout" 2>/dev/null)"
fi

# --- 19. regression control: the text suite still passes -------------------------
if "$TEXT_SUITE" >"$WORK/regression.log" 2>&1; then
  ok "text suite runs green as regression control"
else
  bad "text suite runs green as regression control" "$(tail -n 3 "$WORK/regression.log" | tr '\n' ' ')"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ "$PASS" -gt 0 ]] || { echo "a suite that ran no assertions is not a green suite"; exit 1; }
[[ "$FAIL" -eq 0 ]]
