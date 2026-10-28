#!/usr/bin/env bash
# ===========================================================================
# Acceptance suite for machine-readable migration_squawk findings (--format).
#
# THE CONTRACT THIS PINS. migration_squawk.sh gates migration files against
# the file standard plus squawk, but reports only FAIL:/WARN:/ok:/SKIP: prose,
# so an agent fixing a file must scrape stderr. The --format flag adds a
# machine-readable envelope beside the prose:
#
#   migration_squawk.sh --format json [gate flags] [file.sql ...]
#
#   { "schema_version": 1,
#     "verdict": "pass" | "fail" | "refused",
#     "files": [ { "file": "<path as given>",
#                  "verdict": "pass" | "fail",
#                  "findings": [ { "rule": "<rule-id>",
#                                  "line": <int> | null,
#                                  "severity": "error" | "warning",
#                                  "message": "<short, no SQL body>" } ],
#                  "squawk": { "status": "pass" | "fail" | "skip",
#                              "diagnostics": "<bounded passthrough>",
#                              "truncated": true | false } } ],
#     "summary": { "total_files": N, "failed_files": M,
#                  "error_findings": E, "warning_findings": W,
#                  "squawk": { "passed": P, "failed": F, "skipped": S } },
#     "error": { "code": "<stable kebab-case>", "message": "<short>" } }
#
# Rule ids: file-name, phase-header, concurrent-index, not-valid-constraint
# (all severity error) and split-add-validate (severity warning: same file
# both ADDs and VALIDATEs a constraint, which warns but never fails).
# `line` carries the statement line where the rule has one, else null.
# A file fails when it has an error finding or its squawk step fails; a
# warning alone never fails the file. `error` appears only when the verdict
# is refused. Exit codes are unchanged: 0 pass, 1 fail, 2 refused (including
# --strict with no squawk binary). Default text output stays byte-identical:
# `--format text` is accepted and matches the no-flag bytes exactly.
#
# BOUNDS. Finding messages are at most 300 chars; per-file squawk
# diagnostics are at most 2000 chars with truncated=true when cut. The JSON
# document never carries SQL body bytes or credential values: rule ids,
# line numbers, counts and bounded diagnostic tags only.
#
# HERMETIC BY CONSTRUCTION. Fixture migrations in a throwaway temp dir, fake
# squawk binaries, no database, no network, no live board. Requires bash,
# jq and python3. This module is immutable once the paired build starts:
# the Builder implements against it and never edits it.
# ===========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/migration_squawk.sh"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

command -v jq >/dev/null 2>&1 || { echo "test_migration_squawk_json: jq is required" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "test_migration_squawk_json: python3 is required" >&2; exit 2; }
[[ -x "$GATE" ]] || { echo "test_migration_squawk_json: $GATE is not executable" >&2; exit 2; }

# jq predicate over a captured stdout file; prints $3 on failure only.
jassert() {
  local filter="$1" file="$2" label="$3" diag="${4:-}"
  if jq -e "$filter" "$file" >/dev/null 2>&1; then
    ok "$label"
  else
    bad "$label"
    [[ -n "$diag" ]] && printf '        %s\n' "$diag"
  fi
  return 0
}

# Exactly one JSON document on stdout: python's json.loads rejects trailing
# bytes that a streaming jq read would silently accept as a second document.
single_doc() {
  python3 -c 'import sys,json; json.loads(open(sys.argv[1]).read())' "$1" >/dev/null 2>&1
  return $?
}

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/migs" "$WORK/bin"
OUT="$WORK/out"; ERR="$WORK/err"; mkdir -p "$OUT" "$ERR"

# Distinctive body token: lives in fixture DDL only, never in a file name,
# so any occurrence in JSON output proves a SQL-body leak.
PROBE="zz_squawkjson_body_probe"

cat > "$WORK/migs/0100_clean.sql" <<EOF
-- phase: expand
CREATE INDEX CONCURRENTLY IF NOT EXISTS member_search_idx ON $PROBE (email);
ALTER TABLE $PROBE ADD CONSTRAINT members_email_format CHECK (email LIKE '%@%') NOT VALID;
EOF
cat > "$WORK/badname.sql" <<'EOF'
-- phase: expand
SELECT 1;
EOF
cat > "$WORK/migs/0102_no_phase.sql" <<'EOF'
CREATE INDEX CONCURRENTLY member_other_idx ON members (id);
EOF
cat > "$WORK/migs/0103_locking_index.sql" <<EOF
-- phase: expand
CREATE INDEX bad_probe_idx ON $PROBE (email);
EOF
cat > "$WORK/migs/0104_plain_constraint.sql" <<EOF
-- phase: expand
ALTER TABLE members ADD CONSTRAINT members_nick_probe CHECK (nickname <> '$PROBE');
EOF
cat > "$WORK/migs/0105_add_and_validate.sql" <<'EOF'
-- phase: expand
ALTER TABLE members ADD CONSTRAINT members_email_format CHECK (email LIKE '%@%') NOT VALID;
ALTER TABLE members VALIDATE CONSTRAINT members_email_format;
EOF

# Deterministic squawk shims: green, red with a rule tag, red with a flood.
cat > "$WORK/bin/squawk-ok" <<'FAKE'
#!/usr/bin/env bash
exit 0
FAKE
cat > "$WORK/bin/squawk-bad" <<'FAKE'
#!/usr/bin/env bash
echo "error: prefer-concurrent-index ($1)" >&2
exit 1
FAKE
cat > "$WORK/bin/squawk-flood" <<'FAKE'
#!/usr/bin/env bash
i=1
while [[ "$i" -le 300 ]]; do
  echo "error: filler diagnostic line $i padding 0123456789abcdef0123456789"
  i=$((i+1))
done
echo "SQUAWKJSON_TAIL_SENTINEL"
exit 1
FAKE
chmod +x "$WORK/bin/squawk-ok" "$WORK/bin/squawk-bad" "$WORK/bin/squawk-flood"

# Guard against a vacuous no-leak assertion: the probe must really be there.
if grep -rq "$PROBE" "$WORK/migs/0100_clean.sql" "$WORK/migs/0103_locking_index.sql" \
    "$WORK/migs/0104_plain_constraint.sql"; then
  ok "probe token lives in fixture bodies (no-leak checks are load-bearing)"
else
  bad "probe token lives in fixture bodies (no-leak checks are load-bearing)"
fi

# 1. Clean file + passing squawk: exit 0, one envelope, verdict pass.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/migs/0100_clean.sql" >"$OUT/clean.json" 2>"$ERR/clean.txt" || rc=$?
[[ "$rc" -eq 0 ]] && ok "clean json run exits 0" || bad "clean json run exits 0 (got $rc)"
single_doc "$OUT/clean.json" \
  && ok "clean run prints exactly one JSON document on stdout" \
  || bad "clean run prints exactly one JSON document on stdout"
jassert '.schema_version == 1' "$OUT/clean.json" \
  "the envelope carries schema_version 1"
jassert '.verdict == "pass"' "$OUT/clean.json" \
  "clean run verdict is pass"
jassert '.files | length == 1' "$OUT/clean.json" \
  "clean run reports one file result"
jassert '.files[0].file | contains("0100_clean.sql")' "$OUT/clean.json" \
  "the file result names the checked file"
jassert '.files[0].verdict == "pass"' "$OUT/clean.json" \
  "clean file verdict is pass"
jassert '.files[0].findings == []' "$OUT/clean.json" \
  "clean file carries no findings"
jassert '.files[0].squawk.status == "pass"' "$OUT/clean.json" \
  "passing squawk step reads pass"
jassert '.summary == {total_files: 1, failed_files: 0, error_findings: 0,
  warning_findings: 0, squawk: {passed: 1, failed: 0, skipped: 0}}' \
  "$OUT/clean.json" "clean run summary counts pin every field"

# 2. Bad file name: rule file-name, severity error, no line.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/badname.sql" >"$OUT/badname.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "bad-name json run exits 1" || bad "bad-name json run exits 1 (got $rc)"
jassert '.verdict == "fail"' "$OUT/badname.json" "bad-name verdict is fail"
jassert '.files[0].verdict == "fail"' "$OUT/badname.json" "bad-name file verdict is fail"
jassert '.files[0].findings | length == 1' "$OUT/badname.json" \
  "bad name yields exactly one finding"
jassert '.files[0].findings[0].rule == "file-name"' "$OUT/badname.json" \
  "bad-name finding carries rule file-name"
jassert '.files[0].findings[0].severity == "error"' "$OUT/badname.json" \
  "bad-name finding severity is error"
jassert '.files[0].findings[0].line == null' "$OUT/badname.json" \
  "bad-name finding has no line"

# 3. Missing phase header: rule phase-header.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/migs/0102_no_phase.sql" >"$OUT/nophase.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "no-phase json run exits 1" || bad "no-phase json run exits 1 (got $rc)"
jassert '.files[0].findings | length == 1
  and .[0].rule == "phase-header" and .[0].severity == "error"
  and .[0].line == null' "$OUT/nophase.json" \
  "missing phase header yields one phase-header error without a line"

# 4. Locking index: rule concurrent-index with the statement line.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/migs/0103_locking_index.sql" >"$OUT/locking.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "locking-index json run exits 1" || bad "locking-index json run exits 1 (got $rc)"
jassert '.files[0].findings | length == 1
  and .[0].rule == "concurrent-index" and .[0].severity == "error"
  and .[0].line == 2' "$OUT/locking.json" \
  "locking index yields one concurrent-index error on line 2"

# 5. Plain ADD CONSTRAINT: rule not-valid-constraint with the statement line.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/migs/0104_plain_constraint.sql" >"$OUT/constraint.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "plain-constraint json run exits 1" || bad "plain-constraint json run exits 1 (got $rc)"
jassert '.files[0].findings | length == 1
  and .[0].rule == "not-valid-constraint" and .[0].severity == "error"
  and .[0].line == 2' "$OUT/constraint.json" \
  "plain ADD CONSTRAINT yields one not-valid-constraint error on line 2"

# 6. Same-file ADD + VALIDATE: a warning, never a failure.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/migs/0105_add_and_validate.sql" >"$OUT/warn.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 0 ]] && ok "add-and-validate json run exits 0" || bad "add-and-validate json run exits 0 (got $rc)"
jassert '.verdict == "pass" and .files[0].verdict == "pass"' "$OUT/warn.json" \
  "same-file VALIDATE keeps verdict pass"
jassert '.files[0].findings | length == 1
  and .[0].rule == "split-add-validate" and .[0].severity == "warning"' \
  "$OUT/warn.json" "same-file VALIDATE yields one split-add-validate warning"
jassert '.summary.warning_findings == 1 and .summary.error_findings == 0' \
  "$OUT/warn.json" "warning counts land in warning_findings, not errors"

# 7. Squawk absent without --strict: SKIP-pass, every step skip.
rc=0
PATH="$WORK/bin:/usr/bin:/bin" SQUAWK_BIN="" "$GATE" --format json \
  --migrations-dir "$WORK/migs" "$WORK/migs/0100_clean.sql" \
  >"$OUT/skip.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 0 ]] && ok "absent-squawk json run exits 0" || bad "absent-squawk json run exits 0 (got $rc)"
jassert '.verdict == "pass"' "$OUT/skip.json" "absent squawk keeps verdict pass"
jassert '[.files[].squawk.status] == ["skip"]' "$OUT/skip.json" \
  "absent squawk marks the step skip"
jassert '.summary.squawk == {passed: 0, failed: 0, skipped: 1}' "$OUT/skip.json" \
  "absent squawk counts one skipped step"

# 8. Squawk absent with --strict: exit 2 with a refused envelope.
rc=0
PATH="$WORK/bin:/usr/bin:/bin" SQUAWK_BIN="" "$GATE" --format json --strict \
  --migrations-dir "$WORK/migs" "$WORK/migs/0100_clean.sql" \
  >"$OUT/refused.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 2 ]] && ok "strict absent-squawk json run exits 2" || bad "strict absent-squawk json run exits 2 (got $rc)"
single_doc "$OUT/refused.json" \
  && ok "strict refusal still prints one JSON document" \
  || bad "strict refusal still prints one JSON document"
jassert '.verdict == "refused"' "$OUT/refused.json" "strict refusal verdict is refused"
jassert '.error.code | type == "string" and test("^[a-z0-9-]+$")' "$OUT/refused.json" \
  "strict refusal carries a stable kebab-case error code"
rc2=0
PATH="$WORK/bin:/usr/bin:/bin" SQUAWK_BIN="" "$GATE" --format json --strict \
  --migrations-dir "$WORK/migs" "$WORK/migs/0100_clean.sql" \
  >"$OUT/refused2.json" 2>/dev/null || rc2=$?
if [[ "$rc2" -eq 2 ]] && cmp -s \
    <(jq -r '.error.code' "$OUT/refused.json") \
    <(jq -r '.error.code' "$OUT/refused2.json"); then
  ok "strict refusal code is stable across runs"
else
  bad "strict refusal code is stable across runs"
fi

# 9. Red squawk fails the file with its diagnostic tag passed through.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-bad" \
  "$WORK/migs/0100_clean.sql" >"$OUT/red.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "red-squawk json run exits 1" || bad "red-squawk json run exits 1 (got $rc)"
jassert '.verdict == "fail" and .files[0].verdict == "fail"' "$OUT/red.json" \
  "red squawk fails the verdict and the file"
jassert '.files[0].findings == []' "$OUT/red.json" \
  "red squawk adds no file-standard findings"
jassert '.files[0].squawk.status == "fail"' "$OUT/red.json" \
  "red squawk step reads fail"
jassert '.files[0].squawk.diagnostics | contains("prefer-concurrent-index")' \
  "$OUT/red.json" "failing diagnostic tag passes through bounded output"
jassert '.files[0].squawk.truncated == false' "$OUT/red.json" \
  "short diagnostics are not marked truncated"
jassert '.summary == {total_files: 1, failed_files: 1, error_findings: 0,
  warning_findings: 0, squawk: {passed: 0, failed: 1, skipped: 0}}' \
  "$OUT/red.json" "red-squawk summary blames the step, not the rules"

# 10. Flooding squawk is bounded: at most 2000 chars, tail cut, still parses.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-flood" \
  "$WORK/migs/0100_clean.sql" >"$OUT/flood.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "flood-squawk json run exits 1" || bad "flood-squawk json run exits 1 (got $rc)"
single_doc "$OUT/flood.json" \
  && ok "flood run still prints exactly one JSON document" \
  || bad "flood run still prints exactly one JSON document"
jassert '.files[0].squawk.diagnostics | length <= 2000' "$OUT/flood.json" \
  "flood diagnostics fit the 2000-char bound"
jassert '.files[0].squawk.diagnostics | contains("SQUAWKJSON_TAIL_SENTINEL") | not' \
  "$OUT/flood.json" "flood diagnostics drop the trailing sentinel"
jassert '.files[0].squawk.truncated == true' "$OUT/flood.json" \
  "cut diagnostics are marked truncated"

# 11. No SQL-body bytes in any JSON output.
leak=0
for j in "$OUT"/clean.json "$OUT"/locking.json "$OUT"/constraint.json \
         "$OUT"/red.json "$OUT"/flood.json "$OUT"/skip.json; do
  if [[ -s "$j" ]] && grep -q "$PROBE" "$j"; then
    leak=1
  fi
done
[[ "$leak" -eq 0 ]] && ok "no JSON output leaks SQL-body bytes" || bad "no JSON output leaks SQL-body bytes"

# Finding shape holds on every captured envelope: known rules, known
# severities, int-or-null lines, short non-empty messages.
shape_ok=0
for j in "$OUT"/clean.json "$OUT"/badname.json "$OUT"/nophase.json \
         "$OUT"/locking.json "$OUT"/constraint.json "$OUT"/warn.json \
         "$OUT"/red.json "$OUT"/skip.json; do
  [[ -s "$j" ]] || { shape_ok=1; break; }
  jq -e '[.files[].findings[]? | .rule
    | IN("file-name", "phase-header", "concurrent-index",
         "not-valid-constraint", "split-add-validate")] | all' "$j" >/dev/null 2>&1 || { shape_ok=1; break; }
  jq -e '[.files[].findings[]? | .severity | IN("error", "warning")] | all' "$j" >/dev/null 2>&1 || { shape_ok=1; break; }
  jq -e '[.files[].findings[]? | .line | . == null or type == "number"] | all' "$j" >/dev/null 2>&1 || { shape_ok=1; break; }
  jq -e '[.files[].findings[]? | .message | type == "string" and length > 0 and length <= 300] | all' "$j" >/dev/null 2>&1 || { shape_ok=1; break; }
  jq -e '[.files[].squawk.status | IN("pass", "fail", "skip")] | all' "$j" >/dev/null 2>&1 || { shape_ok=1; break; }
  jq -e '[.files[].squawk.diagnostics | type == "string" and length <= 2000] | all' "$j" >/dev/null 2>&1 || { shape_ok=1; break; }
  jq -e '[.files[].squawk.truncated | type == "boolean"] | all' "$j" >/dev/null 2>&1 || { shape_ok=1; break; }
done
[[ "$shape_ok" -eq 0 ]] && ok "every envelope honors the finding/squawk shape" || bad "every envelope honors the finding/squawk shape"

# 12. Text/JSON verdict parity on every fixture class: same args minus
# --format must exit identically, and the JSON verdict must match the exit.
parity_fail=0
check_parity() {
  local label="$1"; shift
  local rc_text=0 rc_json=0
  "$GATE" "$@" >"$OUT/par_text.out" 2>"$ERR/par_text.err" || rc_text=$?
  "$GATE" --format json "$@" >"$OUT/par_json.out" 2>/dev/null || rc_json=$?
  local want="fail"
  [[ "$rc_text" -eq 0 ]] && want="pass"
  [[ "$rc_text" -eq 2 ]] && want="refused"
  local got=""
  got="$(jq -r '.verdict // empty' "$OUT/par_json.out" 2>/dev/null || true)"
  if [[ "$rc_text" -ne "$rc_json" || "$got" != "$want" ]]; then
    parity_fail=1
    printf '        parity break on %s: text=%s json=%s verdict=%s (want %s)\n' \
      "$label" "$rc_text" "$rc_json" "${got:-<unparseable>}" "$want"
  fi
  return 0
}
check_parity "clean" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0100_clean.sql"
check_parity "bad-name" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/badname.sql"
check_parity "no-phase" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0102_no_phase.sql"
check_parity "locking" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0103_locking_index.sql"
check_parity "constraint" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0104_plain_constraint.sql"
check_parity "warning" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0105_add_and_validate.sql"
check_parity "red-squawk" --squawk-bin "$WORK/bin/squawk-bad" "$WORK/migs/0100_clean.sql"
PATH="$WORK/bin:/usr/bin:/bin" SQUAWK_BIN="" check_parity "skip" --migrations-dir "$WORK/migs" "$WORK/migs/0100_clean.sql"
PATH="$WORK/bin:/usr/bin:/bin" SQUAWK_BIN="" check_parity "strict" --strict --migrations-dir "$WORK/migs" "$WORK/migs/0100_clean.sql"
[[ "$parity_fail" -eq 0 ]] && ok "text and JSON verdicts agree on every fixture" || bad "text and JSON verdicts agree on every fixture"

# 13. --format text is accepted and byte-identical to the default prose.
rc_def=0; rc_txt=0
"$GATE" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0104_plain_constraint.sql" \
  >"$OUT/def.out" 2>"$ERR/def.err" || rc_def=$?
"$GATE" --format text --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0104_plain_constraint.sql" \
  >"$OUT/txt.out" 2>"$ERR/txt.err" || rc_txt=$?
if [[ "$rc_def" -eq "$rc_txt" ]] && cmp -s "$OUT/def.out" "$OUT/txt.out" \
    && cmp -s "$ERR/def.err" "$ERR/txt.err"; then
  ok "explicit --format text matches default bytes and exit"
else
  bad "explicit --format text matches default bytes and exit"
fi
if grep -q "FAIL:" "$OUT/def.out" "$ERR/def.err" 2>/dev/null; then
  ok "default prose still carries FAIL: markers (not JSON)"
else
  bad "default prose still carries FAIL: markers (not JSON)"
fi

# 14. Unknown formats and missing values refuse with exit 2.
rc=0
"$GATE" --format yaml "$WORK/migs/0100_clean.sql" >/dev/null 2>&1 || rc=$?
[[ "$rc" -eq 2 ]] && ok "unknown --format value refuses with exit 2" || bad "unknown --format value refuses with exit 2 (got $rc)"
rc=0
"$GATE" --format >/dev/null 2>&1 || rc=$?
[[ "$rc" -eq 2 ]] && ok "missing --format value refuses with exit 2" || bad "missing --format value refuses with exit 2 (got $rc)"

# 15. Multi-file run: argv order preserved, summary recomputes from files.
rc=0
"$GATE" --format json --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/migs/0100_clean.sql" "$WORK/migs/0104_plain_constraint.sql" \
  >"$OUT/multi.json" 2>/dev/null || rc=$?
[[ "$rc" -eq 1 ]] && ok "multi-file json run exits 1" || bad "multi-file json run exits 1 (got $rc)"
jassert '[.files[].file] | .[0] | contains("0100_clean.sql")' "$OUT/multi.json" \
  "multi-file order follows argv (first)"
jassert '[.files[].file] | .[1] | contains("0104_plain_constraint.sql")' "$OUT/multi.json" \
  "multi-file order follows argv (second)"
jassert '.summary.total_files == (.files | length)' "$OUT/multi.json" \
  "summary total_files recomputes from files"
jassert '.summary.failed_files == ([.files[] | select(.verdict == "fail")] | length)' \
  "$OUT/multi.json" "summary failed_files recomputes from files"
jassert '.summary.error_findings == ([.files[].findings[]? | select(.severity == "error")] | length)' \
  "$OUT/multi.json" "summary error_findings recomputes from findings"
jassert '.summary.warning_findings == ([.files[].findings[]? | select(.severity == "warning")] | length)' \
  "$OUT/multi.json" "summary warning_findings recomputes from findings"
jassert '.summary.squawk.passed == ([.files[] | select(.squawk.status == "pass")] | length)
  and .summary.squawk.failed == ([.files[] | select(.squawk.status == "fail")] | length)
  and .summary.squawk.skipped == ([.files[] | select(.squawk.status == "skip")] | length)' \
  "$OUT/multi.json" "summary squawk counts recompute from steps"

# 16. Usage and the canonical migration-standard pointer document the format.
if grep -q -- "--format" "$GATE"; then
  ok "gate usage documents --format"
else
  bad "gate usage documents --format"
fi
if grep -q "format json" "$HERE/docs/migration-standard.md" 2>/dev/null; then
  ok "migration-standard docs point at the JSON format"
else
  bad "migration-standard docs point at the JSON format"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
