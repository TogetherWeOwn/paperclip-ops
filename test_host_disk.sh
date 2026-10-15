#!/usr/bin/env bash
# test_host_disk.sh — offline suite for the case_4c7b19b7 host-disk detector (D8).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. PAGE fires — the 99% fixture exits 1 with a critical finding routing
#      to the CEO desk. Without this, a suite that only asserts refusals
#      could pass against a detector that breaches nothing.
#   2. PAGE BOUNDARY — exactly 95% pages; 94% warns. An off-by-one here is
#      the difference between a judgement item and a ticket.
#   3. WARN BOUNDARY — exactly 85% warns (owner DevOps, route DevOps); 84%
#      is clean. A warning at 84% would page-cry-wolf the fleet into muting.
#   4. CLEAN is silent-but-counted — 42% exits 0, summary carries
#      breachCount 0. The push side needs the summary row to exist.
#   5. EXCLUSIONS — a 99% tmpfs with a calm root disk exits 0. tmpfs is an
#      app problem, not a host-disk problem; firing on it is the false page
#      this suite exists to prevent.
#   6. GARBAGE FAILS CLOSED — prose, shifted headers, empty reads and
#      unreadable fixtures refuse (exit 2), never clean. The 99% sat
#      unnoticed behind exactly this shape of silence.
#   7. MOUNTS WITH SPACES — "/mnt/my volume" parses to the right row.
#   8. MUTANTS — shifted headers that reach the Use% column fail the run;
#      page-fires-warn refuses input goes red both directions.
#   9. PUSH SIDE — warn pushes false naming mount+pct (exit 1); page pushes
#      false naming the CEO desk; clean pushes true (exit 0); a detector
#      refusal pushes false; a missing detector summary pushes false.
#      Stub curl records argv + config so the secret never reaches argv.
#  10. NOT VACUOUS — a mutant that can never breach still exits 0 on the
#      99% fixture, proving §1 asserts something.
#  11. SCHEMA — findings carry detector/severity/owner/route/evidence and
#      phase; no finding proposes a board mutation (phase-1 findings-only).
#  12. EXAMPLE — the Gatus fragment parses structurally and carries no
#      secret-shaped string.
#
# Hermetic: no network, no credentials, no host disk, no Gatus. Detector
# inputs come from fixture files via HOST_DISK_FIXTURE; pushes go to a stub
# curl answering 200.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DET="$HERE/ops/case_4c7b19b7/host_disk_detector.sh"
PUSH="$HERE/ops/case_4c7b19b7/push-host-disk.sh"
FIX="$HERE/ops/case_4c7b19b7/fixtures"
EXAMPLE="$HERE/ops/case_4c7b19b7/examples/host-disk-endpoint.example.yaml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$DET" ]] || { echo "REFUSE: $DET missing or not executable" >&2; exit 3; }
[[ -x "$PUSH" ]] || { echo "REFUSE: $PUSH missing or not executable" >&2; exit 3; }
command -v jq >/dev/null || { echo "REFUSE: jq required" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_host_disk.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT

run_det() { # [env assignments] — detector stdout in $WORK/det.txt, echoes exit
  local rc
  set +e
  "$@" "$DET" >"$WORK/det.txt" 2>"$WORK/det.err"; rc=$?
  set -e
  printf '%s' "$rc"
}
det_jsonl() { cat "$WORK/det.txt"; }
findings() { det_jsonl | jq -c 'select(.type == "finding")'; }
summary() { det_jsonl | jq -c 'select(.type == "summary")'; }

# --- §1 page fires ----------------------------------------------------------
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-99pct.txt" HOST_DISK_HOST="ci-host-1")"
[[ "$rc" == "1" ]] && ok "1. 99% fixture exits 1" || bad "1. 99% exit" "got $rc"
[[ "$(findings | jq -r '.severity')" == "critical" ]] \
  && ok "1b. severity is critical" || bad "1b. severity" "$(findings)"
[[ "$(findings | jq -r '.route')" == "ceo-hourly-routine" ]] \
  && ok "1c. page routes to the CEO desk" || bad "1c. route" "$(findings)"
[[ "$(findings | jq -r '.owner')" == "devops" ]] \
  && ok "1d. owner is DevOps" || bad "1d. owner" "$(findings)"
[[ "$(summary | jq -r '.breachCount')" == "1" ]] \
  && ok "1e. summary breachCount is 1" || bad "1e. breachCount" "$(summary)"

# --- §2 page boundary -------------------------------------------------------
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-95pct.txt")"
[[ "$rc" == "1" ]] && ok "2. exactly 95% exits 1 (pages)" || bad "2. 95% exit" "got $rc"
[[ "$(findings | jq -r '.severity')" == "critical" ]] \
  && ok "2b. 95% is critical" || bad "2b. 95% severity" "$(findings)"
cat > "$WORK/near-page.txt" <<'DF'
Filesystem     Type     1K-blocks      Used Available Use% Mounted on
/dev/sda1      ext4     20511356  19300000   1100000  94% /
DF
rc="$(run_det env HOST_DISK_FIXTURE="$WORK/near-page.txt")"
[[ "$rc" == "1" ]] && ok "2c. 94% exits 1 (warns)" || bad "2c. 94% exit" "got $rc"
[[ "$(findings | jq -r '.severity')" == "high" ]] \
  && ok "2d. 94% is high, not critical" || bad "2d. 94% severity" "$(findings)"

# --- §3 warn boundary -------------------------------------------------------
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-85pct.txt")"
[[ "$rc" == "1" ]] && ok "3. exactly 85% exits 1 (warns)" || bad "3. 85% exit" "got $rc"
[[ "$(findings | jq -r '.severity')" == "high" ]] \
  && ok "3b. 85% is high" || bad "3b. 85% severity" "$(findings)"
[[ "$(findings | jq -r '.route')" == "devops" ]] \
  && ok "3c. warn routes to DevOps, not the CEO desk" || bad "3c. warn route" "$(findings)"
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-84pct.txt")"
[[ "$rc" == "0" ]] && ok "3d. 84% exits 0 (clean)" || bad "3d. 84% exit" "got $rc"
[[ -z "$(findings)" ]] \
  && ok "3e. 84% emits no findings" || bad "3e. 84% findings" "$(findings)"

# --- §4 clean is silent-but-counted -----------------------------------------
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-clean.txt")"
[[ "$rc" == "0" ]] && ok "4. clean fixture exits 0" || bad "4. clean exit" "got $rc"
[[ -z "$(findings)" ]] \
  && ok "4b. clean emits no findings" || bad "4b. clean findings" "$(findings)"
[[ "$(summary | jq -r '.breachCount')" == "0" ]] \
  && ok "4c. summary breachCount is 0" || bad "4c. clean breachCount" "$(summary)"
[[ -n "$(summary)" ]] \
  && ok "4d. clean still prints a summary row" || bad "4d. clean summary" "(none)"

# --- §5 exclusions ----------------------------------------------------------
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-tmpfs-hot.txt")"
[[ "$rc" == "0" ]] && ok "5. 99% tmpfs with calm root exits 0" || bad "5. tmpfs exit" "got $rc"
[[ -z "$(findings)" ]] \
  && ok "5b. excluded tmpfs emits no findings" || bad "5b. tmpfs findings" "$(findings)"

# --- §6 garbage fails closed ------------------------------------------------
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-garbage.txt")"
[[ "$rc" == "2" ]] && ok "6. prose fixture refuses (exit 2)" || bad "6. prose exit" "got $rc"
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-shifted.txt")"
[[ "$rc" == "2" ]] && ok "6b. Type-less header fixture refuses" || bad "6b. header exit" "got $rc"
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-shifted-row.txt")"
[[ "$rc" == "2" ]] && ok "6b2. shifted data row refuses" || bad "6b2. shifted-row exit" "got $rc"
: > "$WORK/empty.txt"
rc="$(run_det env HOST_DISK_FIXTURE="$WORK/empty.txt")"
[[ "$rc" == "2" ]] && ok "6c. empty read refuses, never clean" || bad "6c. empty exit" "got $rc"
rc="$(run_det env HOST_DISK_FIXTURE="$WORK/does-not-exist.txt")"
[[ "$rc" == "2" ]] && ok "6d. unreadable fixture refuses" || bad "6d. missing exit" "got $rc"
rc="$(run_det env HOST_DISK_WARN_PCT="abc" HOST_DISK_FIXTURE="$FIX/df-clean.txt")"
[[ "$rc" == "2" ]] && ok "6e. non-numeric warn refuses" || bad "6e. warn refusal" "got $rc"
rc="$(run_det env HOST_DISK_WARN_PCT="95" HOST_DISK_PAGE_PCT="95" HOST_DISK_FIXTURE="$FIX/df-clean.txt")"
[[ "$rc" == "2" ]] && ok "6f. warn==page refuses" || bad "6f. inverted refusal" "got $rc"

# --- §7 mounts with spaces --------------------------------------------------
cat > "$WORK/spaces.txt" <<'DF'
Filesystem     Type     1K-blocks      Used Available Use% Mounted on
/dev/sda1      ext4     20511356  19400000   1000000  99% /mnt/my volume
DF
rc="$(run_det env HOST_DISK_FIXTURE="$WORK/spaces.txt")"
[[ "$rc" == "1" ]] && ok "7. spaced-mount breach exits 1" || bad "7. spaced exit" "got $rc"
[[ "$(findings | jq -r '.evidence.mount')" == "/mnt/my volume" ]] \
  && ok "7b. mount parses whole" || bad "7b. mount" "$(findings)"

# --- §8 mutants ---------------------------------------------------------------
# Mutant A: swallow shifted rows instead of refusing (guard becomes `next`).
# On a shifted 99% row beside a clean row, the mutant exits 0 over an
# unmeasured breach — the silent-drop shape §6 exists to prevent. The suite
# asserting the mutant goes green-here proves the refusal is load-bearing.
cat > "$WORK/shifted-plus-clean.txt" <<'DF'
Filesystem     Type     1K-blocks      Used Available Use% Mounted on
/dev/sda1      20511356  19400000   1000000  99% /
/dev/sdb1      ext4     20511356   2000000  17500000  10% /data
DF
cp "$DET" "$WORK/mutant-a.sh"
python3 - "$WORK/mutant-a.sh" <<'PY'
import sys
p = sys.argv[1]
text = open(p).read()
guard = '{ print "SHIFTED:" $0 > "/dev/stderr"; exit 3 }'
assert guard in text, "guard anchor moved — update the mutant"
open(p, "w").write(text.replace(guard, '{ next }'))
PY
set +e
HOST_DISK_FIXTURE="$WORK/shifted-plus-clean.txt" "$WORK/mutant-a.sh" >"$WORK/mut-a.txt" 2>"$WORK/mut-a.err"; mut_rc=$?
set -e
[[ "$mut_rc" == "0" ]] \
  && ok "8. swallow-mutant exits 0 over an unmeasured breach (refusal is load-bearing)" \
  || bad "8. mutant A refuses" "got $mut_rc — mutant failed to demonstrate the drop"
set +e
HOST_DISK_FIXTURE="$WORK/shifted-plus-clean.txt" "$DET" >/dev/null 2>&1; real_rc=$?
set -e
[[ "$real_rc" == "2" ]] \
  && ok "8a. real detector refuses the same input (positive control)" \
  || bad "8a. real refusal" "got $real_rc"
# Mutant B: page threshold can never fire (page forced to 101).
set +e
HOST_DISK_FIXTURE="$FIX/df-99pct.txt" HOST_DISK_PAGE_PCT="100" "$DET" >"$WORK/mut-b.txt" 2>/dev/null; mut_b_rc=$?
set -e
[[ "$mut_b_rc" == "1" && "$(jq -c 'select(.type=="finding")' <"$WORK/mut-b.txt" | jq -r '.severity')" == "high" ]] \
  && ok "8b. page=100 mutant warns instead of paging (thresholds steer routing)" \
  || bad "8b. page mutant" "rc=$mut_b_rc"

# --- §9 push side --------------------------------------------------------------
mkdir -p "$WORK/bin"
export PUSH_CFG_LOG="$WORK/push-cfg.log" PUSH_ARGV_LOG="$WORK/push-argv.log"
: > "$PUSH_CFG_LOG"; : > "$PUSH_ARGV_LOG"
cat > "$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PUSH_ARGV_LOG"
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cat "${!j}" >> "$PUSH_CFG_LOG"; fi
done
printf '200'
exit 0
STUB
chmod +x "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"
export GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-token-not-a-secret"
push_cfg_has() { # URL-decoded grep over curl configs (reasons are encoded)
  grep -q "$1" <<<"$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open(sys.argv[1]).read()))' "$PUSH_CFG_LOG")"
}
push_success() { grep -o "endpoints/platform_host-disk/external?success=[a-z]*" "$PUSH_CFG_LOG" | sed 's/.*success=//'; }
run_push() { # [env...] — resets logs, echoes exit code
  : > "$PUSH_CFG_LOG"; : > "$PUSH_ARGV_LOG"
  local rc
  set +e
  "$@" "$PUSH" >"$WORK/push-out.txt" 2>"$WORK/push-err.txt"; rc=$?
  set -e
  printf '%s' "$rc"
}

rc="$(run_push env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" HOST_DISK_FIXTURE="$FIX/df-86pct.txt")"
[[ "$rc" == "1" ]] && ok "9. warn fixture pushes exit 1" || bad "9. warn push exit" "got $rc"
[[ "$(push_success)" == "false" ]] && ok "9b. warn pushes success=false" || bad "9b. warn push value" "$(cat "$PUSH_CFG_LOG")"
push_cfg_has "/" && ok "9c. warn names the mount" || bad "9c. mount named" "$(cat "$PUSH_CFG_LOG")"
push_cfg_has "86" && ok "9d. warn names the percent" || bad "9d. pct named" "$(cat "$PUSH_CFG_LOG")"
rc="$(run_push env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" HOST_DISK_FIXTURE="$FIX/df-99pct.txt")"
[[ "$rc" == "1" ]] && ok "9e. page fixture pushes exit 1" || bad "9e. page push exit" "got $rc"
push_cfg_has "CEO desk" && ok "9f. page names the CEO desk escalation" || bad "9f. page escalation" "$(cat "$PUSH_CFG_LOG")"
rc="$(run_push env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" HOST_DISK_FIXTURE="$FIX/df-clean.txt")"
[[ "$rc" == "0" ]] && ok "9g. clean fixture pushes exit 0" || bad "9g. clean push exit" "got $rc"
[[ "$(push_success)" == "true" ]] && ok "9h. clean pushes success=true" || bad "9h. clean push value" "$(cat "$PUSH_CFG_LOG")"
rc="$(run_push env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" HOST_DISK_FIXTURE="$FIX/df-garbage.txt")"
[[ "$rc" == "1" ]] && ok "9i. detector refusal pushes false (exit 1)" || bad "9i. refusal push exit" "got $rc"
[[ "$(push_success)" == "false" ]] && ok "9j. refusal is a failure push, never clean" || bad "9j. refusal value" "$(cat "$PUSH_CFG_LOG")"
cp "$DET" "$WORK/no-summary.sh"
python3 - "$WORK/no-summary.sh" <<'PY'
import sys
p = sys.argv[1]
text = open(p).read()
anchor = "'{type: \"summary\","
assert anchor in text, "summary anchor moved — update the mutant"
open(p, "w").write(text.replace(anchor, "'{type: \"no_summary_here\","))
PY
chmod +x "$WORK/no-summary.sh"
# Run the REAL push in place with the detector seam pointed at the mutant:
# copying the push into $WORK would break its relative _gatus_push.sh path.
nosum_rc="$(run_push env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" HOST_DISK_DETECTOR="$WORK/no-summary.sh" HOST_DISK_FIXTURE="$FIX/df-99pct.txt")"
[[ "$nosum_rc" == "1" ]] \
  && ok "9k. summary-less detector output pushes false (positive control)" \
  || bad "9k. summary-less push" "got $nosum_rc"
grep -q "test-token-not-a-secret" "$PUSH_ARGV_LOG" \
  && bad "9l. token in argv" "$(cat "$PUSH_ARGV_LOG")" \
  || ok "9l. Bearer never reaches curl argv"
grep -q "Bearer" "$PUSH_CFG_LOG" \
  && ok "9m. Bearer travels in the 0600 curl config" \
  || bad "9m. Bearer in config" "(missing)"

# --- §10 not vacuous ------------------------------------------------------------
set +e
HOST_DISK_FIXTURE="$FIX/df-99pct.txt" HOST_DISK_WARN_PCT="100" HOST_DISK_PAGE_PCT="101" true >/dev/null 2>&1
HOST_DISK_FIXTURE="$FIX/df-99pct.txt" HOST_DISK_WARN_PCT="100" HOST_DISK_PAGE_PCT="100" "$DET" >"$WORK/vac.txt" 2>/dev/null; vac_rc=$?
set -e
# warn==page refuses (exit 2): the guard itself is the positive control that
# thresholds are validated, so §1's breach cannot be a hardcoded fixture echo.
[[ "$vac_rc" == "2" ]] \
  && ok "10. degenerate thresholds refuse (suite asserts real threshold logic)" \
  || bad "10. vacuity control" "got $vac_rc"

# --- §11 schema ---------------------------------------------------------------
rc="$(run_det env HOST_DISK_FIXTURE="$FIX/df-99pct.txt" HOST_DISK_HOST="ci-host-1")"
for field in detector reason severity owner route evidence suggestedAction phase; do
  findings | jq -e "has(\"$field\")" >/dev/null \
    || bad "11. finding field $field" "$(findings)"
done
ok "11. findings carry the detector contract fields"
[[ "$(findings | jq -r '.detector')" == "watchdog/host_disk" ]] \
  && ok "11b. detector name is watchdog/host_disk" || bad "11b. detector name" "$(findings)"
! grep -q '"mutation"' <<<"$(findings)" \
  && ok "11c. findings propose no board mutation (phase-1 findings-only)" \
  || bad "11c. mutation leak" "$(findings)"

# --- §12 example fragment -------------------------------------------------------
[[ -f "$EXAMPLE" ]] || bad "12. example fragment exists" "(missing)" && ok "12. example fragment exists"
python3 - "$EXAMPLE" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
assert "platform" in text and "host-disk" in text, "fragment must name platform_host-disk"
assert "heartbeat" in text and "interval: 10m" in text, "fragment must carry the 10m heartbeat"
assert "${GATUS_EXTERNAL_TOKEN}" in text, "fragment must reference the token via env"
PY
[[ "$?" == "0" ]] && ok "12b. fragment shape holds" || bad "12b. fragment shape" "(see above)"
# Realistic value lengths only: a short `sk-` alternative self-trips on
# "disk-endpoint", so each shape demands a length prose cannot reach.
grep -Eq 'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|xox[bap]-[A-Za-z0-9-]{12,}|sk-(proj|live)-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}' "$EXAMPLE" \
  && bad "12c. secret-shaped string in fragment" || ok "12c. no secret-shaped strings"

printf '\ntest_host_disk: pass=%d fail=%d\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
