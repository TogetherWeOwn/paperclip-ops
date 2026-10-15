#!/usr/bin/env bash
# test_regen_dry_run_receipt.sh — offline tests for regen_dry_run_receipt.sh (case_90645769).
#
# Written to FAIL the three defects of the refused queued wrapper
# (req-20261011T003115Z-1704370), not to confirm the replacement:
# a missing EXIT trap leaves PAUSE absent after a failed run; `tr -d [:space:]`
# concatenation counts two custody UUIDs as one invalid line; and a grep for
# fail_closed rejects a valid `"fail_closed": null` success receipt.
# Every case also proves PAUSE is present afterwards, because a dry run that
# leaves the host unpaused is a finding even when the receipt is valid.
#
# Deterministic: no network, no credentials, no host paths. The pruner is a
# stub python script printing canned JSON; sleeps are killed by timeout.
#
#   bash ops/case_34c2c99f/test_regen_dry_run_receipt.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$HERE/regen_dry_run_receipt.sh"

UUID_A="00000000-0000-4000-8000-000000000003"
UUID_B="00000000-0000-4000-8000-000000000020"
UUID_C="00000000-0000-4000-8000-000000000087"

pass=0; fail=0
check() { # name expected actual — mutant: inverted assertions pass anything
  if [[ "$2" == "$3" ]]; then pass=$((pass+1));
  else fail=$((fail+1)); echo "FAIL: $1 (expected [$2], got [$3])"; fi
}

fresh() { # echoes tmpdir with stub tool, PAUSE present, keep with 2 annotated holds
  local tmp; tmp="$(mktemp -d)"
  cat > "$tmp/tool.py" <<'EOF'
import json, os, sys, time
open(os.environ["STUB_MARK"], "w").write("ran\n")
if os.environ.get("STUB_ARGV"):
    open(os.environ["STUB_ARGV"], "w").write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
sys.stdout.write(os.environ.get("STUB_JSON", "") + "\n")
sys.exit(int(os.environ.get("STUB_RC", "0")))
EOF
  printf '%s  # recovery custody hold\n%s # legacy annotated hold\n# comment\n\n' \
    "$UUID_A" "$UUID_B" > "$tmp/keep.txt"
  touch "$tmp/PAUSE"
  echo "$tmp"
}

run_wrapper() { # $1=tmp, rest appended as wrapper args; echoes exit code
  local tmp="$1"; shift
  STUB_MARK="$tmp/ran" STUB_ARGV="$tmp/argv" "$WRAPPER" --script "$tmp/tool.py" --pause "$tmp/PAUSE" \
    --keep "$tmp/keep.txt" "$@" > "$tmp/out.txt" 2> "$tmp/err.txt"
  echo $?
}

ok_json() { # $1=custody_held [$2=planned, default 2]
  local planned="${2:-2}"
  printf '{"mode": "dry-run", "removed": 0, "fail_closed": null, "custody_held": %s, "kept_by_reason": {}, "planned": %s, "manifest": "m.txt", "archive_verified_runs": 1, "archive_pending_runs": 0}' "$1" "$planned"
}
old_json() { # $1=custody_held — a pre-gate tool summary without archive keys
  printf '{"mode": "dry-run", "removed": 0, "fail_closed": null, "custody_held": %s, "kept_by_reason": {}, "manifest": null}' "$1"
}

# 1. Valid zero-delete receipt: null fail_closed is NOT an abort.
t="$(fresh)"
export STUB_JSON STUB_RC STUB_SLEEP
STUB_JSON="$(ok_json 2)"; STUB_RC=0; STUB_SLEEP=""
rc="$(STUB_JSON="$STUB_JSON" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "valid receipt exits 0" 0 "$rc"
check "PAUSE present after valid receipt" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
check "VALID verdict printed" 1 "$(grep -c 'receipt: VALID' "$t/out.txt")"
rm -rf "$t"

# 2. Nonempty fail_closed must refuse, and PAUSE must still be restored.
t="$(fresh)"
STUB_JSON="{\"mode\": \"dry-run\", \"removed\": 0, \"fail_closed\": \"custody holds unreadable\", \"custody_held\": 2}"
rc="$(STUB_JSON="$STUB_JSON" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "fail_closed string refused" 3 "$rc"
check "PAUSE restored after refusal" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
rm -rf "$t"

# 3. A dry run that removed anything is refused.
t="$(fresh)"
STUB_JSON="{\"mode\": \"dry-run\", \"removed\": 3, \"fail_closed\": null, \"custody_held\": 2}"
rc="$(STUB_JSON="$STUB_JSON" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "removed!=0 refused" 3 "$rc"
check "PAUSE restored after removed refusal" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
rm -rf "$t"

# 4. A paused-only receipt proves nothing was examined: refuse it.
t="$(fresh)"
STUB_JSON='{"mode": "dry-run", "paused": true}'
rc="$(STUB_JSON="$STUB_JSON" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "paused-only receipt refused" 3 "$rc"
check "PAUSE restored after paused refusal" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
rm -rf "$t"

# 5. Timeout refuses loudly; the EXIT trap restores PAUSE.
t="$(fresh)"
STUB_JSON="$(ok_json 2)"; STUB_RC=0
rc="$(STUB_JSON="$STUB_JSON" STUB_RC=0 STUB_SLEEP=30 run_wrapper "$t" --timeout 1)"
check "timeout refused" 3 "$rc"
check "PAUSE restored after timeout" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
rm -rf "$t"

# 6. Tool crash (exit 2) refuses; PAUSE restored.
t="$(fresh)"
rc="$(STUB_JSON='{}' STUB_RC=2 STUB_SLEEP="" run_wrapper "$t")"
check "tool crash refused" 3 "$rc"
check "PAUSE restored after crash" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
rm -rf "$t"

# 7. custody_held disagreeing with the keep file refuses.
t="$(fresh)"
rc="$(STUB_JSON="$(ok_json 1)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "custody mismatch refused" 3 "$rc"
rm -rf "$t"

# 8. Three annotated holds count as exactly three (kills whitespace-strip counting).
t="$(fresh)"
printf '%s\n%s  # inline note\n# full-line comment\n\n%s#attached\n' \
  "$UUID_A" "$UUID_B" "$UUID_C" > "$t/keep.txt"
rc="$(STUB_JSON="$(ok_json 3)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "three annotated holds accepted" 0 "$rc"
rm -rf "$t"

# 9. Whitespace-joined UUIDs are one invalid line, never one hold: refuse.
t="$(fresh)"
printf '%s%s\n' "$UUID_A" "$UUID_B" > "$t/keep.txt"
rc="$(STUB_JSON="$(ok_json 1)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "concatenated UUIDs refused" 3 "$rc"
check "PAUSE restored after concat refusal" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
rm -rf "$t"

# 10. PAUSE missing at start: refuse WITHOUT running the tool.
t="$(fresh)"
rm -f "$t/PAUSE"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "missing PAUSE exits 2" 2 "$rc"
check "tool not run when PAUSE missing" 0 "$([ -f "$t/ran" ] && echo 1 || echo 0)"
rm -rf "$t"

# 11. --expect-holds guards keep-file drift; --apply is never passed through.
t="$(fresh)"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" --expect-holds 2)"
check "matching expect-holds accepted" 0 "$rc"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" --expect-holds 5)"
check "mismatched expect-holds refused" 3 "$rc"
rm -f "$t/ran"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" -- --apply)"
check "--apply refused" 2 "$rc"
check "tool not run with --apply" 0 "$([ -f "$t/ran" ] && echo 1 || echo 0)"
rm -rf "$t"

# 11b. Argparse abbreviations of --apply (--a, --ap, --appl) are refused too:
# the pruner disables allow_abbrev, and the allowlist stops them here first.
for abbr in --a --ap --appl --apply=x; do
  t="$(fresh)"
  rm -f "$t/ran"
  rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" -- "$abbr")"
  check "$abbr refused" 2 "$rc"
  check "tool not run with $abbr" 0 "$([ -f "$t/ran" ] && echo 1 || echo 0)"
  rm -rf "$t"
done

# 11c. The allowlist still passes its own options through, space or = form.
t="$(fresh)"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" -- --time-budget 30)"
check "allowlisted --time-budget passes" 0 "$rc"
check "tool ran with allowlisted flag" 1 "$([ -f "$t/ran" ] && echo 1 || echo 0)"
rm -rf "$t"
t="$(fresh)"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" -- --max-remove=5 --runs-root /x)"
check "allowlisted = form passes" 0 "$rc"
rm -rf "$t"
t="$(fresh)"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" -- --time-budget)"
check "allowlisted flag without value refused" 2 "$rc"
check "tool not run without value" 0 "$([ -f "$t/ran" ] && echo 1 || echo 0)"
rm -rf "$t"

# 11d. Bare invocation passes zero tool args (not one empty string);
# allowlisted values arrive intact. Kills "${FILTERED[@]:-}" expansion.
t="$(fresh)"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "bare invocation exits 0" 0 "$rc"
check "bare invocation passes zero tool args" '[]' "$(cat "$t/argv")"
rm -rf "$t"
t="$(fresh)"
rc="$(STUB_JSON="$(ok_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t" -- --time-budget 30)"
check "allowlisted passthrough exits 0" 0 "$rc"
check "--time-budget arrives intact" '["--time-budget", "30"]' "$(cat "$t/argv")"
rm -rf "$t"

# 12. Unparseable tool output and malformed keep files refuse.
t="$(fresh)"
rc="$(STUB_JSON='not json at all' STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "non-JSON summary refused" 3 "$rc"
printf 'not-a-uuid\n' > "$t/keep.txt"
rc="$(STUB_JSON="$(ok_json 0)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "malformed keep file refused" 3 "$rc"
rm -rf "$t"

# 13. Archive accounting: a pre-gate summary, an empty plan, and a plan
# without a manifest are all refused, even with null fail_closed.
t="$(fresh)"
rc="$(STUB_JSON="$(old_json 2)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "pre-gate summary refused" 3 "$rc"
rc="$(STUB_JSON="$(ok_json 2 0)" STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "empty plan refused" 3 "$rc"
rc="$(STUB_JSON='{"mode": "dry-run", "removed": 0, "fail_closed": null, "custody_held": 2, "planned": 2, "manifest": null, "archive_verified_runs": 0, "archive_pending_runs": 2}' STUB_RC=0 STUB_SLEEP="" run_wrapper "$t")"
check "plan without manifest refused" 3 "$rc"
check "PAUSE restored after accounting refusal" 1 "$([ -f "$t/PAUSE" ] && echo 1 || echo 0)"
rm -rf "$t"

unset STUB_JSON STUB_RC STUB_SLEEP
echo "pass=$pass fail=$fail"
[[ "$fail" -eq 0 ]]
