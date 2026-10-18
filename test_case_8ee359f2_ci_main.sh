#!/usr/bin/env bash
# test_case_8ee359f2_ci_main.sh — offline suite for the case_c542accd CI
# main-conclusion + job-runtime Gatus module (ops/case_c542accd/).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. HEALTHY stays quiet — the healthy fixture exits 0; main, runtime and
#      runner-units each push success=true exactly once. Without this, a
#      suite that only asserts failures could pass against a script that
#      fails everything.
#   2. RED MAIN alerts exactly once — one failed check pushes the repo's
#      main endpoint success=false naming it, exit 1; two failed checks
#      still produce exactly ONE main push (one alert per red main).
#      Cancelled on main is red (ci.yml never cancels main runs, case_d0cad227).
#   3. RUNTIME 2x — a completed duration strictly over 2x baseline pushes
#      false naming the job; exactly 2x stays green (strictly-greater, not
#      >=); skipped checks with impossible timestamps are excluded, never
#      scored; an unlisted job breaches only past 2x the fallback. Live
#      checks age the same way: a stale in-flight check pages naming
#      "in-flight", while a young or future-started one (conclusion null)
#      stays green — the empty conclusion must never collapse the row into
#      "unparseable check timestamps" while CI runs on main.
#   4. FAIL CLOSED — empty check-runs (the case_b730ec29 vacuous case), an
#      unreadable API, garbage JSON, and unreadable/grossly-negative
#      timestamps all push false, never healthy. A small negative span
#      (-60s..0, a GitHub timestamp artefact on healthy mains) clamps to 0;
#      exactly -60 clamps green while -61 fails closed (span pins the exact
#      edge; the live-check age pin uses -65s margin so it cannot flake on a
#      wall-clock second ticking mid-run).
#   5. RUNNER UNITS — one inactive unit pushes ci_runner-units false naming
#      it, exit 1; all active pushes true.
#   6. REFUSALS — missing GATUS_URL, missing GH_REPOS, a bad factor, an
#      unreadable baselines file, missing gh/systemctl binaries, and
#      colliding endpoint slugs (o/a.b vs o/a-b: dots and dashes both map
#      to '-', so the last write would mask a red main) each exit 2 with
#      zero pushes (the 10m heartbeat miss then pages).
#   7. NON-DISCLOSURE — the Bearer token never reaches curl argv; it
#      travels in the 0600 curl config only.
#   8. DEFINITIONS — gatus-endpoints.yaml carries thirteen endpoints
#      (main + runtime for each of the six covered public repos, plus
#      runner-units), each with threshold + owner + runbook anchor;
#      the synthetic baselines fixture covers the same six repos with
#      per-entry seeds (the real measurement table stays host-side via
#      CI_BASELINES_FILE and is never committed); endpoint slugs are unique
#      and within the push-helper key alphabet; no secret-shaped strings
#      committed.
#   9. NOT VACUOUS — a mutant with the red-conclusion selector neutered
#      scores the red-main fixture healthy, proving §2 asserts something.
#
# Hermetic: no network, no credentials. A stub curl records argv + config
# contents and answers 200; stub gh feeds fixture files; stub systemctl
# fakes unit state.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ops/case_c542accd/push-ci-main.sh"
BASE="$HERE/ops/case_c542accd/job_baselines.json"
YAML="$HERE/ops/case_c542accd/gatus-endpoints.yaml"
OPDOC="$HERE/ops/case_c542accd/OPERATOR.md"
FIX="$HERE/ops/case_c542accd/fixtures"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$TOOL" ]] || { echo "REFUSE: $TOOL missing or not executable" >&2; exit 3; }
command -v jq >/dev/null || { echo "REFUSE: jq required" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_ci_main.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export ARGV_LOG="$WORK/argv.log" CFG_LOG="$WORK/cfg.log" GH_CALL_LOG="$WORK/gh.log"
: > "$ARGV_LOG"; : > "$CFG_LOG"; : > "$GH_CALL_LOG"

# Stub curl: records argv and every --config file's contents, answers 200.
cat > "$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ARGV_LOG"
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cat "${!j}" >> "$CFG_LOG"; echo >> "$CFG_LOG"; fi
done
printf '200'
exit 0
STUB
# Stub gh: GH_MODE=fixture serves $GH_FIXTURE_FILE and logs the call;
# GH_MODE=fail simulates an unreadable API; anything else refuses.
cat > "$WORK/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_CALL_LOG"
case "${GH_MODE:-fixture}" in
  fixture) cat "$GH_FIXTURE_FILE" ;;
  fail) echo "gh: boom" >&2; exit 1 ;;
  *) echo "gh stub: bad GH_MODE" >&2; exit 2 ;;
esac
STUB
# Stub systemctl: SYSTEMCTL_FIXTURE=all-active exits 0 for every unit;
# one-dead fails the coolify-vps-3 listener only.
cat > "$WORK/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
if [[ "${SYSTEMCTL_FIXTURE:-all-active}" == "one-dead" && "$*" == *coolify-vps-3* ]]; then
  exit 1
fi
exit 0
STUB
chmod +x "$WORK/bin/"*
export PATH="$WORK/bin:/usr/bin:/bin"

# Small baselines table for repo o/r: pr-lint 8s, Offline suites 6138s,
# Long mutation gates 4034s; unlisted jobs fall back to 3600s.
cat > "$WORK/baselines.json" <<'JSON'
{"fallback_s": 3600,
 "repos": {"o/r": {"jobs": {
   "pr-lint": {"baseline_s": 8, "seed": "test"},
   "Offline suites": {"baseline_s": 6138, "seed": "test"},
   "Long mutation gates": {"baseline_s": 4034, "seed": "test"}}}}}
JSON

run_tool() { # [extra env...] -- runs TOOL with the standard test env
  : > "$ARGV_LOG"; : > "$CFG_LOG"; : > "$WORK/out.txt"; : > "$WORK/err.txt"
  local rc
  set +e
  env GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-token-not-a-secret" \
    GH_REPOS="o/r" GH_BIN="$WORK/bin/gh" SYSTEMCTL_BIN="$WORK/bin/systemctl" \
    CI_BASELINES_FILE="$WORK/baselines.json" TMPDIR="$WORK" "$@" \
    bash "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  set -e
  printf '%s' "$rc"
}
pushes_for() { # <key> prints success= values pushed for that endpoint key
  grep -o "endpoints/$1/external?success=[a-z]*" "$CFG_LOG" | sed 's/.*success=//'
}
push_count() { # <key> prints how many pushes targeted that endpoint key
  grep -c "endpoints/$1/external" "$CFG_LOG" || true
}
cfg_has() { # <pattern> greps the URL-decoded curl configs (reasons are encoded)
  grep -q "$1" <<<"$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open("'"$CFG_LOG"'").read()))')"
}

export GH_MODE=fixture SYSTEMCTL_FIXTURE=all-active

# --- §1 healthy stays quiet ------------------------------------------------
export GH_FIXTURE_FILE="$FIX/healthy.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "1. healthy fixture exits 0" || bad "1. healthy exit" "got $rc"
[[ "$(pushes_for ci_main-o-r)" == "true" ]] && ok "1b. main pushed true" || bad "1b. main push" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] && ok "1c. runtime pushed true" || bad "1c. runtime push" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runner-units)" == "true" ]] && ok "1d. runner-units pushed true" || bad "1d. units push" "$(cat "$CFG_LOG")"

# --- §2 red main alerts exactly once ----------------------------------------
export GH_FIXTURE_FILE="$FIX/red-main.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "2. red-main fixture exits 1" || bad "2. red-main exit" "got $rc"
[[ "$(pushes_for ci_main-o-r)" == "false" ]] && ok "2b. main pushed false" || bad "2b. main push" "$(cat "$CFG_LOG")"
[[ "$(push_count ci_main-o-r)" == "1" ]] && ok "2c. exactly one main alert" || bad "2c. one alert" "pushes=$(push_count ci_main-o-r)"
cfg_has "Offline suites" && ok "2d. reason names the failed check" || bad "2d. check named" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] && ok "2e. runtime stays green under a red conclusion" || bad "2e. runtime push" "$(cat "$CFG_LOG")"
# two failed checks: still exactly one main push.
export GH_FIXTURE_FILE="$FIX/multi-red.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "2f. multi-red exits 1" || bad "2f. multi-red exit" "got $rc"
[[ "$(push_count ci_main-o-r)" == "1" ]] && ok "2g. two failures still one alert" || bad "2g. one alert" "pushes=$(push_count ci_main-o-r)"
# cancelled on main is red (ci.yml never cancels main runs).
export GH_FIXTURE_FILE="$FIX/cancelled.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "2h. cancelled check exits 1" || bad "2h. cancelled exit" "got $rc"
[[ "$(pushes_for ci_main-o-r)" == "false" ]] && ok "2i. cancelled main pushed false" || bad "2i. cancelled push" "$(cat "$CFG_LOG")"

# --- §2b queued null-start fails closed, conclusion stays green -------------
# F1: a queued check with started_at null must fail the runtime closed
# (untimed is not fast) while the conclusion stays green (live checks
# never redden the conclusion). Before the fix, date -d "" scored today
# 00:00 and paged or stayed quiet by time of day.
export GH_FIXTURE_FILE="$FIX/queued-null-start.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "2j. queued null-start exits 1" || bad "2j. null-start exit" "got $rc"
[[ "$(pushes_for ci_runtime-o-r)" == "false" ]] && ok "2k. null-start runtime pushed false" || bad "2k. null-start runtime" "$(cat "$CFG_LOG")"
cfg_has "unparseable check timestamps" && ok "2l. reason names untimed checks" || bad "2l. null-start reason" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_main-o-r)" == "true" ]] && ok "2m. queued null-start keeps conclusion green" || bad "2m. null-start main" "$(cat "$CFG_LOG")"
# F2: a lowercase GH_REPOS still resolves tuned baselines (pr-lint 8s):
# the same slow boundary-over shape must page under GH_REPOS=o/r too.
# (The standard env uses lowercase, but the lookup must not depend on it.)
export GH_FIXTURE_FILE="$FIX/boundary-over.json"
rc="$(run_tool GH_REPOS="O/R")"
[[ "$rc" == "1" ]] && ok "2n. mixed-case repo still resolves baselines" || bad "2n. case-insensitive lookup" "got $rc $(cat "$CFG_LOG")"
# A dotted repo name (TogetherWeOwn/.github shape) must push under a
# dashed key: the shared helper's key alphabet rejects dots, so an
# unmapped dot would refuse every push and exit nonzero.
export GH_FIXTURE_FILE="$FIX/healthy.json"
rc="$(run_tool GH_REPOS="o/.r")"
[[ "$rc" == "0" ]] && ok "2o. dotted repo exits 0" || bad "2o. dotted repo exit" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_main-o--r)" == "true" ]] && ok "2p. dotted repo pushes ci_main-o--r" || bad "2p. dotted slug key" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o--r)" == "true" ]] && ok "2q. dotted repo pushes ci_runtime-o--r" || bad "2q. dotted slug key" "$(cat "$CFG_LOG")"

# --- §3 runtime 2x ------------------------------------------------------------
export GH_FIXTURE_FILE="$FIX/slow-job.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "3. slow-job exits 1" || bad "3. slow-job exit" "got $rc"
[[ "$(pushes_for ci_runtime-o-r)" == "false" ]] && ok "3b. runtime pushed false" || bad "3b. runtime push" "$(cat "$CFG_LOG")"
cfg_has "Offline suites" && ok "3c. reason names the slow job" || bad "3c. job named" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_main-o-r)" == "true" ]] && ok "3d. main stays green under a slow success" || bad "3d. main push" "$(cat "$CFG_LOG")"
# exactly 2x stays green (strictly-greater boundary); 1s over pages.
export GH_FIXTURE_FILE="$FIX/boundary-exact.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "3e. exactly-2x stays green" || bad "3e. boundary exact" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] || bad "3e2. boundary runtime" "$(cat "$CFG_LOG")"
export GH_FIXTURE_FILE="$FIX/boundary-over.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "3f. 1s-over-2x pages" || bad "3f. boundary over" "got $rc"
[[ "$(pushes_for ci_runtime-o-r)" == "false" ]] || bad "3f2. boundary runtime" "$(cat "$CFG_LOG")"
# skipped checks with impossible timestamps are excluded, never scored.
export GH_FIXTURE_FILE="$FIX/skipped-weird.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "3g. skipped check excluded from timing" || bad "3g. skipped exclusion" "got $rc $(cat "$CFG_LOG")"
# unlisted job: 8000s past 2x3600 breach; 6800s within it stays green.
export GH_FIXTURE_FILE="$FIX/unknown-slow.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "3h. unlisted slow job breaches the fallback" || bad "3h. fallback breach" "got $rc"
export GH_FIXTURE_FILE="$FIX/unknown-ok.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "3i. unlisted fast job stays green (positive control)" || bad "3i. fallback control" "got $rc"
# in-flight age: a live check older than 2x baseline pages without waiting.
export GH_FIXTURE_FILE="$FIX/inflight.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "3j. stale in-flight check pages" || bad "3j. in-flight" "got $rc"
[[ "$(pushes_for ci_main-o-r)" == "true" ]] && ok "3k. live checks never redden the conclusion" || bad "3k. main push" "$(cat "$CFG_LOG")"
cfg_has "in-flight" && ok "3j2. stale in-flight reason names in-flight ageing" || bad "3j2. in-flight reason" "$(cat "$CFG_LOG")"
# Young live checks (conclusion null) stay green: the empty conclusion must
# not collapse the row into "unparseable check timestamps" while CI runs.
mk_inflight() { # <outfile> <started_at-offset-secs> writes a live Offline-suites + healthy pr-lint fixture
python3 - "$1" "$2" <<'PY'
import json, sys, time
out, offset = sys.argv[1], int(sys.argv[2])
start = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + offset))
json.dump({"check_runs": [
  {"name": "Offline suites", "status": "in_progress", "conclusion": None, "started_at": start, "completed_at": None},
  {"name": "pr-lint", "status": "completed", "conclusion": "success", "started_at": "2026-10-03T19:25:46Z", "completed_at": "2026-10-03T19:25:54Z"}],
  "total_count": 2}, open(out, "w"))
PY
}
mk_inflight "$WORK/inflight-young.json" -30
export GH_FIXTURE_FILE="$WORK/inflight-young.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "3l. young in-flight check stays green" || bad "3l. young in-flight" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] && ok "3m. young in-flight runtime pushed true" || bad "3m. young runtime" "$(cat "$CFG_LOG")"
# Future-started live check (clock skew +30s): age clamps to 0, stays green.
mk_inflight "$WORK/inflight-future.json" 30
export GH_FIXTURE_FILE="$WORK/inflight-future.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "3n. future-started in-flight check stays green" || bad "3n. future in-flight" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] && ok "3o. future in-flight runtime pushed true" || bad "3o. future runtime" "$(cat "$CFG_LOG")"

# --- §4 fail closed -------------------------------------------------------------
export GH_FIXTURE_FILE="$FIX/empty.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "4. empty check-runs exits 1 (vacuous is not green)" || bad "4. empty exit" "got $rc"
[[ "$(pushes_for ci_main-o-r)" == "false" ]] && ok "4b. empty main pushed false" || bad "4b. empty main" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "false" ]] && ok "4c. empty runtime pushed false" || bad "4c. empty runtime" "$(cat "$CFG_LOG")"
export GH_MODE=fail
rc="$(run_tool GH_FIXTURE_FILE="$FIX/healthy.json")"
[[ "$rc" == "1" ]] && ok "4d. unreadable API exits 1" || bad "4d. api failure" "got $rc"
[[ "$(pushes_for ci_main-o-r)" == "false" ]] || bad "4d2. api main" "$(cat "$CFG_LOG")"
export GH_MODE=fixture
echo 'not json{{{' > "$WORK/bad.json"
export GH_FIXTURE_FILE="$WORK/bad.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "4e. garbage JSON exits 1" || bad "4e. garbage exit" "got $rc"
export GH_FIXTURE_FILE="$FIX/bad-time.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "4f. grossly negative duration fails closed" || bad "4f. bad-time exit" "got $rc"
[[ "$(pushes_for ci_runtime-o-r)" == "false" ]] || bad "4f2. bad-time runtime" "$(cat "$CFG_LOG")"
[[ "$(pushes_for ci_main-o-r)" == "true" ]] && ok "4g. conclusion still evaluated under a bad timestamp" || bad "4g. bad-time main" "$(cat "$CFG_LOG")"
# Small negative span (-8s): a GitHub timestamp artefact on healthy mains;
# it clamps to 0 and never pages.
export GH_FIXTURE_FILE="$FIX/negative-span.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "4h. small negative span exits 0" || bad "4h. negative-span exit" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] && ok "4i. small negative span runtime pushed true" || bad "4i. negative-span runtime" "$(cat "$CFG_LOG")"
# Clamp boundaries: exactly -60s clamps green, -61s fails closed (span …).
export GH_FIXTURE_FILE="$FIX/span-minus-60.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "4h2. span exactly -60s clamps green" || bad "4h2. span -60 exit" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] && ok "4h3. span -60s runtime pushed true" || bad "4h3. span -60 runtime" "$(cat "$CFG_LOG")"
export GH_FIXTURE_FILE="$FIX/span-minus-61.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "4h4. span -61s fails closed" || bad "4h4. span -61 exit" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "false" ]] && ok "4h5. span -61s runtime pushed false" || bad "4h5. span -61 runtime" "$(cat "$CFG_LOG")"
cfg_has "unparseable check timestamps" && ok "4h6. span -61s reason names untimed checks" || bad "4h6. span -61 reason" "$(cat "$CFG_LOG")"
# … and age: a live check started 60s in the future clamps green; the
# fail-closed side pins with margin at 65s, not exactly 61s, so the pin is
# wall-clock independent (fixture clock and the tool's date +%s are read
# milliseconds apart; a second boundary ticking between them would turn an
# exact -61 into -60 and flake green). The exact -61 edge stays covered by
# the deterministic static span-minus-61 fixture (4h4/4h5/4h6) above.
mk_inflight "$WORK/inflight-age-60.json" 60
export GH_FIXTURE_FILE="$WORK/inflight-age-60.json"
rc="$(run_tool)"
[[ "$rc" == "0" ]] && ok "4h7. age -60s clamps green" || bad "4h7. age -60 exit" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "true" ]] && ok "4h8. age -60s runtime pushed true" || bad "4h8. age -60 runtime" "$(cat "$CFG_LOG")"
mk_inflight "$WORK/inflight-age-65.json" 65
export GH_FIXTURE_FILE="$WORK/inflight-age-65.json"
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "4h9. age -65s fails closed" || bad "4h9. age -65 exit" "got $rc $(cat "$CFG_LOG")"
[[ "$(pushes_for ci_runtime-o-r)" == "false" ]] && ok "4h10. age -65s runtime pushed false" || bad "4h10. age -65 runtime" "$(cat "$CFG_LOG")"

# --- §5 runner units --------------------------------------------------------------
export GH_FIXTURE_FILE="$FIX/healthy.json" SYSTEMCTL_FIXTURE=one-dead
rc="$(run_tool)"
[[ "$rc" == "1" ]] && ok "5. dead unit exits 1" || bad "5. dead unit exit" "got $rc"
[[ "$(pushes_for ci_runner-units)" == "false" ]] && ok "5b. units pushed false" || bad "5b. units push" "$(cat "$CFG_LOG")"
cfg_has "coolify-vps-3" && ok "5c. reason names the dead unit" || bad "5c. unit named" "$(cat "$CFG_LOG")"
export SYSTEMCTL_FIXTURE=all-active

# --- §6 refusals, zero pushes ------------------------------------------------------
reset_refusal() { : > "$CFG_LOG"; : > "$ARGV_LOG"; }
reset_refusal
set +e; env -u GATUS_URL PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_EXTERNAL_TOKEN="T" GH_REPOS="o/r" GH_BIN="$WORK/bin/gh" SYSTEMCTL_BIN="$WORK/bin/systemctl" \
  CI_BASELINES_FILE="$WORK/baselines.json" GH_MODE=fixture GH_FIXTURE_FILE="$FIX/healthy.json" \
  bash "$TOOL" >/dev/null 2>&1; rc=$?; set -e
[[ "$rc" == "2" ]] && [[ ! -s "$CFG_LOG" ]] && ok "6. missing GATUS_URL refuses (exit 2, zero pushes)" || bad "6. GATUS_URL refusal" "rc=$rc pushes=$(grep -c endpoints/ "$CFG_LOG" || true)"
for bad_env in 'GH_REPOS=o/a,o/b' 'GH_REPOS=o/..' 'GH_REPOS=' 'CI_RUNTIME_FACTOR=0' 'CI_RUNTIME_FACTOR=1h' 'CI_BASELINES_FILE=/nonexistent.json'; do
  reset_refusal
  set +e
  # shellcheck disable=SC2086
  env PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
    GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="T" GH_REPOS="o/r" \
    GH_BIN="$WORK/bin/gh" SYSTEMCTL_BIN="$WORK/bin/systemctl" \
    CI_BASELINES_FILE="$WORK/baselines.json" GH_MODE=fixture GH_FIXTURE_FILE="$FIX/healthy.json" \
    $bad_env bash "$TOOL" >/dev/null 2>&1; rc=$?
  set -e
  if [[ "$rc" == "2" ]] && [[ ! -s "$CFG_LOG" ]]; then ok "6b. refuses exit 2, no push: $bad_env"
  else bad "6b. refusal" "$bad_env rc=$rc pushes=$(grep -c endpoints/ "$CFG_LOG" || true)"; fi
done
reset_refusal
set +e; env PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="T" GH_REPOS="o/r" \
  GH_BIN="$WORK/bin/no-such-gh" SYSTEMCTL_BIN="$WORK/bin/systemctl" \
  CI_BASELINES_FILE="$WORK/baselines.json" bash "$TOOL" >/dev/null 2>&1; rc=$?; set -e
[[ "$rc" == "2" ]] && [[ ! -s "$CFG_LOG" ]] && ok "6c. missing gh refuses exit 2" || bad "6c. gh refusal" "rc=$rc"
reset_refusal
set +e; env PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="T" GH_REPOS="o/r" \
  GH_BIN="$WORK/bin/gh" SYSTEMCTL_BIN="$WORK/bin/no-such-systemctl" \
  CI_BASELINES_FILE="$WORK/baselines.json" bash "$TOOL" >/dev/null 2>&1; rc=$?; set -e
[[ "$rc" == "2" ]] && [[ ! -s "$CFG_LOG" ]] && ok "6d. missing systemctl refuses exit 2" || bad "6d. systemctl refusal" "rc=$rc"
# Dots and dashes both map to '-': distinct repos sharing one endpoint key
# would let the last write mask a red main, so the run refuses upfront.
export GH_FIXTURE_FILE="$FIX/healthy.json"
rc="$(run_tool GH_REPOS="o/a.b o/a-b")"
[[ "$rc" == "2" ]] && [[ ! -s "$CFG_LOG" ]] && ok "6e. slug collision refuses (exit 2, zero pushes)" || bad "6e. slug collision" "rc=$rc pushes=$(grep -c endpoints/ "$CFG_LOG" || true)"

# --- §7 non-disclosure -----------------------------------------------------------
: > "$ARGV_LOG"; : > "$CFG_LOG"
set +e; env PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="SENTINELTOKEN-must-never-reach-argv" \
  GH_REPOS="o/r" GH_BIN="$WORK/bin/gh" SYSTEMCTL_BIN="$WORK/bin/systemctl" \
  CI_BASELINES_FILE="$WORK/baselines.json" GH_MODE=fixture GH_FIXTURE_FILE="$FIX/healthy.json" \
  SYSTEMCTL_FIXTURE=all-active bash "$TOOL" >/dev/null 2>&1; set -e
if grep -q "SENTINELTOKEN" "$ARGV_LOG"; then bad "7. token in argv" "$(cat "$ARGV_LOG")"; else ok "7. no Bearer in curl argv"; fi
grep -q "Authorization: Bearer" "$CFG_LOG" && ok "7b. token travels in curl config" || bad "7b. config transport" "missing"

# --- §8 definitions ----------------------------------------------------------------
[[ "$(grep -c '^  - name: ' "$YAML")" == 13 ]] \
  && ok "8. thirteen gatus endpoints" || bad "8. endpoint count" "$(grep -c '^  - name: ' "$YAML")"
for e in main-togetherweown-two-bot-next main-togetherweown-two-web-next main-togetherweown-nntunev2 main-togetherweown-routeware-shadow-api main-togetherweown-paperclip-ops main-togetherweown--github runtime-togetherweown-two-bot-next runtime-togetherweown-two-web-next runtime-togetherweown-nntunev2 runtime-togetherweown-routeware-shadow-api runtime-togetherweown-paperclip-ops runtime-togetherweown--github runner-units; do
  grep -q "name: $e" "$YAML" && ok "endpoint $e" || bad "endpoint $e missing"
done
[[ "$(grep -c THRESHOLD "$YAML")" -ge 13 ]] && ok "8b. thresholds declared" || bad "8b. thresholds missing"
{ grep -q "OWNER on-call" "$YAML" && grep -q "OWNER devops" "$YAML"; } \
  && ok "8c. owners declared" || bad "8c. owners missing"
for a in main-conclusion job-runtime runner-units; do
  grep -q "OPERATOR.md#$a" "$YAML" && grep -q "^### $a$" "$OPDOC" \
    && ok "runbook pointer $a" || bad "runbook pointer $a missing"
done
if grep -En '://' "$YAML"; then bad "8d. yaml carries a URL literal"; else ok "8d. yaml has no URL literals"; fi
[[ "$(grep -c 'GATUS_EXTERNAL_TOKEN' "$YAML")" -ge 13 ]] \
  && ok "8e. token only via env reference" || bad "8e. token reference missing"
# The real measurement table stays host-side via CI_BASELINES_FILE and is never
# committed here; the synthetic fixture below proves the shape/slug/key/GH_REPOS
# agreement checks without publishing private job names or timings.
if [[ -e "$BASE" ]]; then
  bad "8f. real baselines file must stay out of the public snapshot" "$BASE"
else
  ok "8f0. no real baselines file committed"
fi
python3 - "$FIX/synthetic-baselines.json" "$YAML" "$OPDOC" <<'PY'
import json, re, sys
d = json.load(open(sys.argv[1]))
repos = d["repos"]
expected = {"TogetherWeOwn/.github", "TogetherWeOwn/nntunev2",
            "TogetherWeOwn/paperclip-ops", "TogetherWeOwn/routeware-shadow-api",
            "TogetherWeOwn/two-bot-next", "TogetherWeOwn/two-web-next"}
assert set(repos) == expected, sorted(repos)
assert isinstance(d["fallback_s"], int) and d["fallback_s"] > 0
for r, spec in repos.items():
    assert spec["jobs"], r
    for j, e in spec["jobs"].items():
        assert isinstance(e["baseline_s"], int) and e["baseline_s"] > 0, (r, j)
        assert e.get("seed"), (r, j)
# Every covered repo maps to a unique main-/runtime- endpoint in the YAML
# under the script's slug rule ('/' and '.' to '-'), and every resulting
# push key fits the shared helper's alphabet.
yaml_text = open(sys.argv[2]).read()
names = re.findall(r"^  - name: (\S+)$", yaml_text, re.M)
slugs = [r.lower().replace("/", "-").replace(".", "-") for r in repos]
assert len(slugs) == len(set(slugs)), "slug collision"
for s in slugs:
    assert f"main-{s}" in names and f"runtime-{s}" in names, s
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", f"ci_main-{s}"), s
assert len(names) == 2 * len(repos) + 1 and "runner-units" in names, names
# OPERATOR-adjacent GH_REPOS agreement is pinned via the endpoint set above;
# the host GH_REPOS list itself lives in the Operator install and is not
# checked in this source tree. An endpoint without a matching push pages
# on heartbeat miss (see OPERATOR.md).
print("baselines ok")
PY
[[ $? -eq 0 ]] && ok "8f. synthetic baselines cover six public repos; slugs unique, keys in alphabet" || bad "8f. baselines shape"
# Scoped to ops/case_c542accd only: this suite file itself carries the planted
# webhook positive control below, so scanning it would self-trip.
if grep -rnE 'discord\.com/api/webhooks/[0-9]|^(DISCORD_WEBHOOK_URL|AKMATORI_ALERTMANAGER_URL|AKMATORI_WEBHOOK_SECRET|GATUS_EXTERNAL_TOKEN)=[^<"$'"'"'[:space:]$]|Bearer [A-Za-z0-9._~-]{8,}' "$HERE/ops/case_c542accd" | grep -v SENTINELTOKEN; then
  bad "8g. live secret committed"
else ok "8g. no secret-shaped strings in the leaf"; fi
printf 'x="https://discord.com/api/webhooks/123/abc"' > "$WORK/planted.txt"
if grep -qE 'discord\.com/api/webhooks' "$WORK/planted.txt"; then ok "8h. secret-scan pattern fires (positive control)"; else bad "8h. scan pattern" "pattern missed a planted webhook"; fi

# --- §9 mutation gate ----------------------------------------------------------------
# Neuter the red-conclusion selector: no conclusion can match, so the
# red-main fixture must score healthy. The copy resolves its helper
# relatively, so repoint the tree the same way the tool expects it.
mkdir -p "$WORK/mut/ops/case_c542accd" "$WORK/mut/ops/case_658bf6b9"
cp "$HERE/ops/case_658bf6b9/_gatus_push.sh" "$WORK/mut/ops/case_658bf6b9/"
python3 - "$TOOL" "$WORK/mut/ops/case_c542accd/push-ci-main.sh" <<'PY'
import sys
s = open(sys.argv[1]).read()
old = 'IN("failure","timed_out","cancelled","action_required","startup_failure")'
assert old in s, "anchor for the red-conclusion selector moved — update this gate"
# No trailing comment: the selector lives inside a single-quoted jq program,
# where `#` would comment out the rest of the filter and fail closed.
s = s.replace(old, 'IN("zzz-neutered-no-conclusion-matches-this")')
open(sys.argv[2], "w").write(s)
PY
chmod +x "$WORK/mut/ops/case_c542accd/push-ci-main.sh"
: > "$CFG_LOG"
set +e
env PATH="$WORK/bin:/usr/bin:/bin" HOME="$HOME" TMPDIR="$WORK" \
  GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="T" GH_REPOS="o/r" \
  GH_BIN="$WORK/bin/gh" SYSTEMCTL_BIN="$WORK/bin/systemctl" \
  CI_BASELINES_FILE="$WORK/baselines.json" GH_MODE=fixture GH_FIXTURE_FILE="$FIX/red-main.json" \
  SYSTEMCTL_FIXTURE=all-active bash "$WORK/mut/ops/case_c542accd/push-ci-main.sh" >/dev/null 2>&1
set -e
if [[ "$(pushes_for ci_main-o-r)" == "true" ]]; then ok "9. mutant scores red-main healthy (selector is load-bearing)"; else bad "9. mutation gate" "neutered script still pushes $(pushes_for ci_main-o-r) — §2 asserts nothing"; fi

printf '\nci main module: %d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
