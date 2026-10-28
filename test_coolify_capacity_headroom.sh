#!/usr/bin/env bash
# ===========================================================================
# test_coolify_capacity_headroom.sh — offline suite for the Coolify
# decommission capacity check (case_4dbfd258).
#
# No network, no host, no credential, no gh call: every sample below is a
# fabricated jobs array written under mktemp, with runner names and clocks
# invented for the test. The tool reads only the file it is given.
#
# WHAT THIS SUITE IS BUILT TO CATCH, beyond the happy path:
#
#  * A NO-RUNNER QUEUE ARTIFACT COUNTED AS COMPUTE (case_f19c6787). The reference
#    12h sample held ~15,730 no-runner timestamp minutes beside ~17,037 real
#    runner minutes. Section 1 pins that three 30-minute queued/cancelled jobs
#    with empty runner_name contribute 0.00 minutes while one 10-minute
#    coolify job contributes 10.00. A counter that summed timestamp span
#    regardless of runner assignment would report 100.00 here and pass a
#    capacity verdict off queue artifacts.
#
#  * A HOST CLASS MISREAD. `ci-node7b1ff338-iso-1` and `garm-two-abc` must not land in
#    the coolify bucket: section 2 pins the per-class split, so a glob that
#    matched every self-hosted name as Coolify load fails the test.
#
#  * A THIN SAMPLE REPORTED AS GO. Section 3 pins INCONCLUSIVE on a 2-job
#    sample even when the peak fits, per the case_1fe7979a adequacy rule (>=24h
#    span and >=10 matched attempts). A verdict that skipped adequacy would
#    read GO here.
#
#  * A PEAK THAT FITS REPORTED, AND ONE THAT DOES NOT. Sections 4-5 pin GO
#    (peak 2 <= 3 slots) and NO-GO (peak 2 > 1 slot) on separate complete
#    comparable fixtures, not the incomplete accounting sample. Each arm has
#    7 attempts over >700h, one cohort and an enclosing asserted window.
#    Both verdict and exit status are checked: refusal is not a measured result.
#    Sections 3b and 5b keep incomplete evidence INCONCLUSIVE.
#
#  * AN UNMEASURED POOL REPORTED HEALTHY. Section 6 asserts refusal (exit 2)
#    on an empty array, a non-array, a missing file, and a sample with zero
#    runner-assigned jobs. Zero measured jobs is "never ran", not "fits".
# ===========================================================================
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/coolify_capacity_headroom.sh"
PASS=0; FAIL=0
WORK="$(mktemp -d "${PAPERCLIP_RUN_SCRATCH_DIR:-${PAPERCLIP_SCRATCH_DIR:-${TMPDIR:-/tmp}}}/capacity-headroom-test.XXXXXX")" || exit 1
trap 'rm -rf "$WORK"' EXIT

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

# mk_jobs <file> — fabricated 30-day-span jobs; callers slice what they need.
mk_jobs() {
  cat > "$1" <<'EOF'
[
  {"name": "suite-a", "runner_name": "coolify-vps-2", "started_at": "2026-09-03T10:00:00Z", "completed_at": "2026-09-03T10:10:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]},
  {"name": "queued-never-landed", "runner_name": "", "started_at": "2026-09-03T10:00:00Z", "completed_at": "2026-09-03T10:30:00Z", "conclusion": "cancelled", "labels": ["self-hosted", "two-isolated"]},
  {"name": "cancelled-no-runner", "runner_name": "", "started_at": "2026-09-04T10:00:00Z", "completed_at": "2026-09-04T10:30:00Z", "conclusion": "cancelled", "labels": ["self-hosted", "two-isolated"]},
  {"name": "gitleaks-cancel-no-runner", "runner_name": "", "started_at": "2026-09-05T10:00:00Z", "completed_at": "2026-09-05T10:30:00Z", "conclusion": "cancelled", "labels": ["self-hosted", "two-selfhosted"]},
  {"name": "suite-b", "runner_name": "ci-node7b1ff338-iso-1", "started_at": "2026-09-10T10:00:00Z", "completed_at": "2026-09-10T10:20:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]},
  {"name": "suite-c", "runner_name": "garm-two-abc", "started_at": "2026-09-20T10:00:00Z", "completed_at": "2026-09-20T10:05:00Z", "conclusion": "success", "labels": ["self-hosted", "two-ephemeral"]},
  {"name": "suite-d", "runner_name": "coolify-vps-3", "started_at": "2026-10-03T09:00:00Z", "completed_at": "2026-10-03T09:15:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]},
  {"name": "suite-e", "runner_name": "coolify-vps-4", "started_at": "2026-10-03T09:05:00Z", "completed_at": "2026-10-03T09:25:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]},
  {"name": "suite-f", "runner_name": "coolify-vps-5", "started_at": "2026-10-03T11:00:00Z", "completed_at": "2026-10-03T11:10:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]},
  {"name": "suite-g", "runner_name": "coolify-vps-6", "started_at": "2026-10-03T12:00:00Z", "completed_at": "2026-10-03T12:10:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]},
  {"name": "suite-h", "runner_name": "coolify-vps-7", "started_at": "2026-10-03T13:00:00Z", "completed_at": "2026-10-03T13:10:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]},
  {"name": "skewed-clock", "runner_name": "coolify-vps-1", "started_at": "2026-10-03T14:10:00Z", "completed_at": "2026-10-03T14:00:00Z", "conclusion": "success", "labels": ["self-hosted", "two-isolated"]}
]
EOF
}

# mk_complete_jobs <file> <cohort> — 7 distinct attempts per arm over 720h10m.
# Candidate peak 2 and incumbent peak 2 occur at different hours; combined is 2.
# No accounting exclusions, malformed clocks, or unrelated runner classes.
mk_complete_jobs() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

path, cohort = sys.argv[1:]
jobs = []
for index, day in enumerate(("2026-09-03", "2026-09-03", "2026-09-10",
                             "2026-09-15", "2026-09-20", "2026-09-25", "2026-10-03")):
    for offset, runner, hour in ((100, "coolify-vps-synthetic", "10"),
                                 (200, "garm-synthetic", "11")):
        jobs.append({
            "id": offset + index, "name": f"synthetic-{offset + index}",
            "runner_name": f"{runner}-{index}", "conclusion": "success",
            "labels": ["self-hosted"], "cohort_id": cohort,
            "started_at": f"{day}T{hour}:00:00Z",
            "completed_at": f"{day}T{hour}:10:00Z",
        })
with open(path, "w") as stream:
    json.dump({"jobs": jobs, "evidence": {
        "cohort_id": cohort, "complete": True, "arms": ["coolify", "garm"],
        "window": {"start": "2026-09-03T00:00:00Z", "end": "2026-10-04T00:00:00Z"},
    }}, stream)
PY
}

# ---------------------------------------------------------------- section 1
# case_f19c6787: no-runner minutes are queue artifacts, not compute.
F="$WORK/s1.json"; mk_jobs "$F"
OUT="$("$TOOL" --jobs "$F" --min-hours 700 --min-attempts 100 2>/dev/null)"
if [[ "$OUT" == *"runner_minutes_total=100.00"* && "$OUT" == *"coolify=75.00"* \
   && "$OUT" == *"excluded_no_runner=3"* && "$OUT" == *"excluded_clock=1"* ]]; then
  ok "1 no-runner queue/cancel minutes excluded; skewed clock excluded by cause"
else
  bad "1 no-runner exclusion" "$OUT"
fi

# ---------------------------------------------------------------- section 2
# Host-class split: node7b1ff338 and garm minutes must not read as Coolify load.
if [[ "$OUT" == *"node7b1ff338=20.00"* && "$OUT" == *"garm=5.00"* && "$OUT" == *"other=0.00"* ]]; then
  ok "2 per-class split pins node7b1ff338 and garm outside the coolify bucket"
else
  bad "2 per-class split" "$OUT"
fi

# ---------------------------------------------------------------- section 3
# Thin sample is INCONCLUSIVE even when the peak fits (case_1fe7979a adequacy).
F="$WORK/s3.json"
cat > "$F" <<'EOF'
[
  {"name": "one", "runner_name": "coolify-vps-1", "started_at": "2026-10-03T10:00:00Z", "completed_at": "2026-10-03T10:10:00Z", "conclusion": "success"},
  {"name": "two", "runner_name": "coolify-vps-2", "started_at": "2026-10-03T11:00:00Z", "completed_at": "2026-10-03T11:10:00Z", "conclusion": "success"}
]
EOF
OUT="$("$TOOL" --jobs "$F" 2>/dev/null)"; CODE=$?
if (( CODE == 0 )) && [[ "$OUT" == *"verdict=INCONCLUSIVE"* && "$OUT" == *"adequacy bound"* ]]; then
  ok "3 thin sample INCONCLUSIVE with adequacy reason, still exit 0"
else
  bad "3 thin-sample INCONCLUSIVE" "code=$CODE out=$OUT"
fi

# ---------------------------------------------------------------- section 3b
# The accounting fixture has only one GARM attempt, no provenance, and a bad
# clock. Its global span/count must not authorize either measured verdict.
for slots in 2 0; do
  OUT="$("$TOOL" --jobs "$WORK/s1.json" --node7b1ff338-max 1 --node575e13db-max "$slots" --min-hours 700 --min-attempts 7 2>/dev/null)"; CODE=$?
  if (( CODE == 0 )) && [[ "$OUT" == *"verdict=INCONCLUSIVE"* ]]; then
    ok "3b incomplete accounting sample INCONCLUSIVE with $((slots+1)) slots, exit 0"
  else
    bad "3b incomplete accounting sample" "code=$CODE out=$OUT"
  fi
done

# ---------------------------------------------------------------- section 4
# GO: complete comparable evidence; combined peak 2 fits the same 3 slots.
mk_complete_jobs "$WORK/complete-go.json" "synthetic-capacity-go"
OUT="$("$TOOL" --jobs "$WORK/complete-go.json" --node7b1ff338-max 1 --node575e13db-max 2 --min-hours 700 --min-attempts 7 2>/dev/null)"; CODE=$?
if (( CODE == 0 )) && [[ "$OUT" == *"verdict=GO"* ]]; then
  ok "4 complete comparable evidence GO with fitting peak, exit 0"
else
  bad "4 GO verdict" "code=$CODE out=$OUT"
fi

# ---------------------------------------------------------------- section 5
# NO-GO: separate complete comparable evidence against the same 1 slot;
# candidate and combined peak 2 exceed it. A measured NO-GO is still exit 0.
mk_complete_jobs "$WORK/complete-no-go.json" "synthetic-capacity-no-go"
OUT="$("$TOOL" --jobs "$WORK/complete-no-go.json" --node7b1ff338-max 1 --node575e13db-max 0 --min-hours 700 --min-attempts 7 2>/dev/null)"; CODE=$?
if (( CODE == 0 )) && [[ "$OUT" == *"verdict=NO-GO"* ]]; then
  ok "5 complete comparable evidence NO-GO with exceeding peak, exit 0"
else
  bad "5 NO-GO verdict" "code=$CODE out=$OUT"
fi

# ---------------------------------------------------------------- section 5b
# Adequate attempts/span alone are insufficient: explicitly incomplete evidence
# stays INCONCLUSIVE with either slot count, even without malformed rows.
python3 - "$WORK/complete-go.json" "$WORK/incomplete.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as stream:
    data = json.load(stream)
data["evidence"]["complete"] = False
with open(sys.argv[2], "w") as stream:
    json.dump(data, stream)
PY
for slots in 2 0; do
  OUT="$("$TOOL" --jobs "$WORK/incomplete.json" --node7b1ff338-max 1 --node575e13db-max "$slots" --min-hours 700 --min-attempts 7 2>/dev/null)"; CODE=$?
  if (( CODE == 0 )) && [[ "$OUT" == *"verdict=INCONCLUSIVE"* ]]; then
    ok "5b adequate but incomplete evidence INCONCLUSIVE with $((slots+1)) slots, exit 0"
  else
    bad "5b adequate but incomplete evidence" "code=$CODE out=$OUT"
  fi
done

# ---------------------------------------------------------------- section 6
# Refusals: empty, non-array, missing, and all-no-runner inputs exit 2.
echo '[]' > "$WORK/empty.json"
echo '{"jobs": []}' > "$WORK/empty-obj.json"
echo '[1,2]' > "$WORK/nonobj.json"
cat > "$WORK/norunner.json" <<'EOF'
[{"name": "queued", "runner_name": "", "started_at": "2026-10-03T10:00:00Z", "completed_at": "2026-10-03T10:30:00Z", "conclusion": "cancelled"}]
EOF
REF_OK=1
for bad_in in "$WORK/empty.json" "$WORK/empty-obj.json" "$WORK/nonobj.json" "$WORK/norunner.json" "$WORK/does-not-exist.json"; do
  "$TOOL" --jobs "$bad_in" >/dev/null 2>&1; CODE=$?
  (( CODE == 2 )) || REF_OK=0
done
if (( REF_OK == 1 )); then
  ok "6 empty/non-array/missing/all-no-runner inputs refuse with exit 2"
else
  bad "6 refusal cases" "at least one bad input did not exit 2"
fi

# ---------------------------------------------------------------- section 7
# The `gh api .../jobs` object shape is accepted, not just a bare array.
python3 -c "import json; print(json.dumps({'jobs': json.load(open('$WORK/s1.json'))}))" > "$WORK/obj.json"
OUT_BARE="$("$TOOL" --jobs "$WORK/s1.json" --min-hours 700 --min-attempts 100 2>/dev/null)"
OUT_OBJ="$("$TOOL" --jobs "$WORK/obj.json" --min-hours 700 --min-attempts 100 2>/dev/null)"
if [[ "$OUT_BARE" == "$OUT_OBJ" ]]; then
  ok "7 object-with-jobs shape measures identically to the bare array"
else
  bad "7 jobs-object shape" "bare: $OUT_BARE | obj: $OUT_OBJ"
fi

printf '\ncoolify headroom suite: %d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
