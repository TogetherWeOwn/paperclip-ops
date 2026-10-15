#!/usr/bin/env bash
# case_f03f7f10 mutation gate: prove test_pr_closeout_snapshot_repair.py is
# load-bearing.
#
# The repair tool exists because two hand-produced closeout artifacts passed
# review while wrong: 53 snapshot check rows had lost their names, and three
# progress clocks claimed core-check success while pointing at gitleaks or at
# nothing. A green suite can hide the same thing again. This gate breaks the
# tool on purpose, one guard at a time, and requires the suite to go red in
# the NAMED test for each. Several mutants re-introduce the exact defect the
# tool repairs (fill counted but name dropped, gitleaks accepted as core,
# requiredChecks re-split into characters).
#
# Every mutation runs against a private staged copy, never the shared
# checkout: in a reused worktree a dirty tool fails every sibling sharing the
# tree, and this gate's whole job is to dirty it.
#
# Completeness is policed at the bottom: every test method in the suite must
# be named by at least one mutant.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

SUITE=test_pr_closeout_snapshot_repair.py
TOOL=pr_closeout_snapshot_repair.py

reset_stage() {
  cp "$ROOT/$SUITE" "$STAGE/"
  cp "$ROOT/$TOOL" "$STAGE/"
}

run_suite() {
  (cd "$STAGE" && python3 -m unittest "$SUITE" 2>&1)
}

ran_count() {
  printf '%s\n' "$1" | grep -oE 'Ran [0-9]+ tests' | grep -oE '[0-9]+' | tail -1 || true
}

reset_stage
baseline="$(run_suite)" || {
  printf 'FAIL: unmutated suite is already red\n%s\n' "$baseline" >&2
  exit 1
}
baseline_count="$(ran_count "$baseline")"
[[ -n "$baseline_count" && "$baseline_count" -gt 0 ]] || { echo 'FAIL: baseline ran zero tests' >&2; exit 1; }

covered=()

# Every mutation must keep the suite running the same number of tests (a
# mutant that collapses the suite proves nothing) and must fail in the NAMED
# test rather than some other one.
assert_caught() {
  local name="$1" expected="$2" rc="$3" output="$4"
  local count
  count="$(ran_count "$output")"
  if [[ "$rc" -eq 0 || "$count" != "$baseline_count" || "$output" != *"FAIL: $expected"* ]]; then
    printf 'FAIL: %s was not caught by %s\n%s\n' "$name" "$expected" "$output" >&2
    exit 1
  fi
  covered+=("$expected")
  printf 'PASS: %s\n' "$name"
}

mutate() {
  local name="$1" old="$2" new="$3" expected="$4"
  reset_stage
  OLD="$old" NEW="$new" FILE="$STAGE/$TOOL" python3 - <<'PY'
import os, pathlib, sys
path = pathlib.Path(os.environ["FILE"])
source = path.read_text()
old = os.environ["OLD"]
count = source.count(old)
if count != 1:
    print(f"mutation target appears {count} times, expected exactly 1", file=sys.stderr)
    raise SystemExit(1)
path.write_text(source.replace(old, os.environ["NEW"]))
PY
  python3 -m py_compile "$STAGE/$TOOL"
  local output rc=0
  output="$(run_suite 2>&1)" || rc=$?
  assert_caught "$name" "$expected" "$rc" "$output"
}

# --- Finding 1: null names fill from the ledger key, and only on a full join. ---
mutate fill-counted-but-name-dropped \
  '                row["name"] = named[0]' \
  '                row["name"] = row.get("name")' \
  test_null_name_fills_from_ledger_dict_key
mutate only-first-ledger-entry-indexed \
  '        if isinstance(check_id, int) and check_id not in index:' \
  '        if isinstance(check_id, int) and not index:' \
  test_required_names_join_after_fill
mutate null-name-count-skipped \
  '            if not isinstance(row.get("name"), str))' \
  '            if row.get("name") == "")' \
  test_unknown_id_stays_null_for_verification
mutate sha-disagreement-ignored \
  'row.get("sha") == named[1]' \
  'True' \
  test_sha_mismatch_stays_null
mutate completed-at-disagreement-ignored \
  'row.get("completedAt") == named[2]' \
  'True' \
  test_completed_at_mismatch_stays_null
mutate present-name-overwritten \
  '            if isinstance(row.get("name"), str):
                continue' \
  '            if False:
                continue' \
  test_present_name_is_never_overwritten

# --- Finding 2: a core-success clock joins to a same-head `check` success. ---
mutate gitleaks-accepted-as-core \
  '        if row.get("name") == "check"
        and row.get("conclusion") == "success"' \
  '        if row.get("name") in ("check", "gitleaks")
        and row.get("conclusion") == "success"' \
  test_gitleaks_clock_with_failed_core_unsets
mutate orphan-clock-kept \
  '        record["lastProgressAt"] = None' \
  '        record["lastProgressAt"] = clock' \
  test_orphan_clock_with_no_retained_completion_unsets
mutate oldest-core-success-chosen \
  '    matching.sort(key=lambda row: str(row.get("completedAt")))' \
  '    matching.sort(key=lambda row: str(row.get("completedAt")), reverse=True)' \
  test_stale_clock_restamps_to_latest_core_success
mutate joining-clock-rewritten \
  '        if candidates and clock == candidates[-1].get("completedAt"):' \
  '        if False:' \
  test_joining_clock_is_preserved
mutate off-head-core-success-counted \
  '        and row.get("conclusion") == "success" and row.get("sha") == head' \
  '        and row.get("conclusion") == "success"' \
  test_off_head_core_success_does_not_count
mutate non-claiming-record-repaired \
  '        if not claims_core_success(record.get("lastProgressEvidence")):
            continue
        ledger_key = ledger_key_for_snapshot_key(str(key))
        snapshot = None' \
  '        ledger_key = ledger_key_for_snapshot_key(str(key))
        snapshot = None' \
  test_merge_receipt_clock_is_untouched
mutate missing-evidence-read-as-claim \
  '    return bool(isinstance(evidence, str)' \
  '    return bool(evidence is None or isinstance(evidence, str)' \
  test_missing_evidence_is_untouched
mutate empty-inputs-accepted \
  '        raise RepairError("snapshot holds no snapshots; refusing to report a clean board")
    filled = 0' \
  '        pass
    filled = 0' \
  test_empty_inputs_refuse_clean
mutate unjoined-clock-verified \
  '        joined = any(
            row.get("completedAt") == clock' \
  '        joined = True or any(
            row.get("completedAt") == clock' \
  test_check_only_detects_both_defects

# --- The 19:48Z recurrence: reworded claim, pending and absent core checks. ---
mutate claim-pinned-to-one-wording \
  'r"successful\s+(?:[a-z]+-head\s+)?core-check\s+completion"' \
  'r"successful\s+(?:exact-head\s+)?core-check\s+completion"' \
  test_every_recorded_wording_is_a_core_claim
mutate pending-core-rendered-as-none \
  "f\"check/{latest_retained.get('conclusion') or 'pending'} \"" \
  "f\"check/{latest_retained.get('conclusion')} \"" \
  test_bound_head_gitleaks_clock_with_pending_core_unsets
mutate absent-core-row-unnamed \
  '            retained_identity = "no retained core `check` attempt at this head"' \
  '            retained_identity = "unknown"' \
  test_bound_head_clock_with_no_core_row_unsets

# --- Character-split requiredChecks. ---
mutate required-checks-re-split \
  '            snap["requiredChecks"] = joined' \
  '            snap["requiredChecks"] = list(joined)' \
  test_character_split_required_rejoins
mutate single-name-list-joined \
  '    if (isinstance(value, list) and len(value) >= 2' \
  '    if (isinstance(value, list) and len(value) >= 1' \
  test_real_required_list_is_untouched
mutate split-shape-unnamed \
  '        if split_string_required(required) is not None:' \
  '        if False:' \
  test_verify_names_character_split_shape

# --- Joinability and the re-emitted ZIP. ---
mutate off-head-required-name-joins \
  '                row.get("name") == name and row.get("sha") == head' \
  '                row.get("name") == name' \
  test_unjoinable_required_name_is_flagged
mutate claim-without-target-passes \
  '            errors.append(f"{key}: core-success claim with no snapshot target")
            continue' \
  '            continue' \
  test_core_claim_without_snapshot_target_is_flagged
mutate zip-keeps-stale-snapshot \
  'dump_doc(snapshot_doc) if name == "snapshot.json" else data' \
  'data' \
  test_zip_reemits_repaired_snapshot_and_keeps_other_members

# Completeness. Every test in the suite must be named by at least one mutant
# above. Without this, an assertion added with no mutant is indistinguishable
# from an assertion that cannot fail.
reset_stage
UNCOVERED="$(COVERED="$(printf '%s\n' "${covered[@]}")" SUITE_PATH="$STAGE/$SUITE" python3 - <<'PY'
import os, pathlib, re
covered = set(os.environ["COVERED"].split())
names = re.findall(r"(?m)^    def (test_\w+)\(", pathlib.Path(os.environ["SUITE_PATH"]).read_text())
assert names, "found no test methods to check coverage against"
print("\n".join(sorted(set(names) - covered)))
PY
)"
if [[ -n "$UNCOVERED" ]]; then
  printf 'FAIL: assertions with no mutant proving they are load-bearing:\n%s\n' "$UNCOVERED" >&2
  exit 1
fi

printf 'PASS: %s snapshot-repair mutations detected, covering all %s assertions\n' \
  "${#covered[@]}" "$baseline_count"
