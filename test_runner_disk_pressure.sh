#!/usr/bin/env bash
# ===========================================================================
# test_runner_disk_pressure.sh — regression suite for the case_d8c3515a runner
# disk-pressure detector.
#
# The detector's load-bearing properties, in the order they matter:
#
#   1. It FIRES at the case_231b8224 shape: a host at 99% reads CRITICAL, exit 1.
#      A detector that cannot return 1 is an alarm that is always off.
#   2. It STAYS QUIET on a healthy host. A detector that cannot return 0 is
#      an alarm that is always on -- and a hardcoded `exit 1` passes every
#      other case here and fails only this one. This is the test that keeps
#      the suite honest.
#   3. It FAILS CLOSED. An unmeasurable path or unparseable df is exit 2
#      (inconclusive), never exit 0. Reporting health from a measurement the
#      detector could not make is the exact failure that let the real
#      incident run undetected until jobs could no longer write.
#   4. The du breakdown NEVER moves the verdict. A failed breakdown is a
#      note, not a pass and not a finding.
#   5. The CI wiring survives `bash -e`. The runner executes steps with
#      errexit, so a capture of the form `out="$(...detector...)"; rc=$?`
#      without `set +e` dies silently on the exact exits the step exists to
#      handle (rc 1/2) — no `rc=$?`, no `::error` annotation. case_dbfa63d9
#      caught exactly that on the first CI run. The suite pins `set +e`
#      bracketing in BOTH workflow steps and proves the real step bodies
#      reach the annotation under `bash -e`, plus a mutation control proving
#      the check can fail (stripped bracketing dies silent).
#
# Fully offline: `df` and `du` are DF_BIN/DU_BIN stubs, runner roots are
# mktemp fixtures, workflow step bodies run against the stubbed detector.
# No host, no network, no credential.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${RUNNER_DISK_PRESSURE_SH:-$HERE/runner_disk_pressure.sh}"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[[ -x "$TOOL" ]] || { echo "ERROR: $TOOL is not executable" >&2; exit 1; }
command -v jq >/dev/null || { echo "ERROR: jq is required" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_runner_disk_pressure.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/root/instance-a" "$WORK/root/instance-b"
echo data > "$WORK/root/instance-a/file"

# The df stub prints a POSIX two-line table for the requested path. The
# detector calls `df -P -- <path>`; the stub answers for that path and fails
# for anything else, which keeps multi-path confusion visible.
mkdf() {
  cat > "$WORK/bin/df" <<STUB
#!/usr/bin/env bash
path="\${*: -1}"
case "\$path" in
  "$1") printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 800000000 782468505 100000 99%% /\n' ;;
  *) printf 'df: %s: No such file or directory\n' "\$path" >&2; exit 1 ;;
esac
STUB
  chmod +x "$WORK/bin/df"
}

# run [detector args...] -> sets RC and OUT. The detector is pointed at the
# stubs by absolute path (DF_BIN/DU_BIN): a PATH shim does not survive this
# environment — every non-interactive bash re-sources BASH_ENV, which
# hard-resets PATH and voids the shim before the detector runs.
run() {
  OUT="$(DF_BIN="$WORK/bin/df" DU_BIN="$WORK/bin/du" "$TOOL" "$@" 2>&1)"
  RC=$?
}

# du defaults to the real binary until a case installs the failing stub.
ln -s "$(command -v du)" "$WORK/bin/du"

# --- 1. the real incident shape --------------------------------------------
hdr "Fires on the case_231b8224 shape (host at 99%)"
mkdf "/"
run --path / --runner-root "$WORK/root"
(( RC == 1 )) && ok "exit 1 at 99%" || bad "expected exit 1, got $RC ($OUT)"
grep -q "CRITICAL" <<<"$OUT" && ok "reads CRITICAL, not merely WARNING" || bad "no CRITICAL verdict"

hdr "Boundary: exactly at warn reads pressure, exactly at crit reads critical"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 80 20 80%% /\n'
STUB
chmod +x "$WORK/bin/df"
run --path / --warn-pct 80 --crit-pct 90
(( RC == 1 )) && ok "exit 1 at exactly warn" || bad "expected exit 1, got $RC"
grep -q "WARNING" <<<"$OUT" && ok "80 with crit 90 is WARNING" || bad "wrong severity ($OUT)"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 90 10 90%% /\n'
STUB
chmod +x "$WORK/bin/df"
run --path / --warn-pct 80 --crit-pct 90
grep -q "CRITICAL" <<<"$OUT" && ok "90 with crit 90 is CRITICAL" || bad "wrong severity ($OUT)"

# --- 2. the negative control -----------------------------------------------
hdr "Stays quiet on a healthy host (negative control)"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 46 54 46%% /\n'
STUB
chmod +x "$WORK/bin/df"
run --path /
(( RC == 0 )) && ok "exit 0 at 46%" || bad "expected exit 0, got $RC ($OUT)"
grep -q "^OK" <<<"$OUT" && ok "says OK" || bad "did not report healthy"

# --- 3. fail closed ----------------------------------------------------------
hdr "Fails closed on an unmeasurable path"
mkdf "/"
run --path /does-not-exist
(( RC == 2 )) && ok "exit 2 on df failure" || bad "expected exit 2, got $RC ($OUT)"

hdr "Fails closed on unparseable df output"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'something unexpected with no percent column\n'
STUB
chmod +x "$WORK/bin/df"
run --path /
(( RC == 2 )) && ok "exit 2 on unparseable df" || bad "expected exit 2, got $RC ($OUT)"

hdr "Refuses inverted thresholds instead of mis-scoring"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 46 54 46%% /\n'
STUB
chmod +x "$WORK/bin/df"
run --path / --warn-pct 90 --crit-pct 80
(( RC == 2 )) && ok "exit 2 on warn >= crit" || bad "expected exit 2, got $RC ($OUT)"

# --- 4. breakdown is corroborating only --------------------------------------
hdr "A failed breakdown never moves the verdict"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 99 1 99%% /\n'
STUB
chmod +x "$WORK/bin/df"
rm -f "$WORK/bin/du" # symlink to the real du; `cat >` would follow it
cat > "$WORK/bin/du" <<'STUB'
#!/usr/bin/env bash
echo "permission denied" >&2; exit 1
STUB
chmod +x "$WORK/bin/du"
run --path / --runner-root "$WORK/root"
(( RC == 1 )) && ok "still exit 1 when du fails" || bad "expected exit 1, got $RC ($OUT)"
grep -qi "note:" <<<"$OUT" && ok "failed breakdown is a note" || bad "no note for failed breakdown"
ln -sf "$(command -v du)" "$WORK/bin/du"
run --path / --runner-root "$WORK/root"
grep -q "instance-" <<<"$OUT" && ok "working breakdown names consumers" || bad "no consumer breakdown ($OUT)"

# --- 5. json shape -------------------------------------------------------------
hdr "JSON output parses and carries the verdict"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 85 15 85%% /\n'
STUB
chmod +x "$WORK/bin/df"
run --path / --runner-root "$WORK/root" --json
STATUS="$(jq -r .status <<<"$OUT" 2>/dev/null)"
[[ "$STATUS" == "pressure" ]] && ok "status=pressure at 85/80/90" || bad "wrong status ($STATUS)"
jq -e '.use_pct == 85 and .warn_pct == 80 and .crit_pct == 90 and (.top_consumers | length) > 0' <<<"$OUT" >/dev/null \
  && ok "thresholds and consumers present" || bad "json shape wrong ($OUT)"

hdr "A note containing spaces stays one JSON string, not fragments"
cat > "$WORK/bin/df" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 46 54 46%% /\n'
STUB
chmod +x "$WORK/bin/df"
run --path / --runner-root "$WORK/does-not-exist" --json
jq -e '.notes | length == 1' <<<"$OUT" >/dev/null \
  && ok "one note, not word fragments" || bad "note was word-split ($OUT)"
jq -e '.notes[0] | contains("runner root")' <<<"$OUT" >/dev/null \
  && ok "note text intact" || bad "note text broken ($OUT)"

# --- 6. CI wiring survives bash -e (case_dbfa63d9) ----------------------------------
hdr "CI wiring survives bash -e (case_dbfa63d9 regression guard)"
extract_step() { # $1=workflow file $2=step name -> body with indent stripped
  # Body lines carry 10 spaces of YAML indent, deeper shell nesting more;
  # match the 10-space PREFIX (not exactly-10-then-nonspace) or the
  # 12-space `summary=` lines inside the if-block end extraction early and
  # the proof runs a truncated body. First non-blank line under 10 spaces
  # (the next step or key) ends the body.
  awk -v name="$2" '
    index($0, "- name: " name) {found=1; next}
    found && index($0, "run: |") {inbody=1; next}
    inbody && /^$/ {print; next}
    inbody && /^          / {sub(/^          /, ""); print; next}
    inbody {exit}
  ' "$1"
}
CI_STEP="$(extract_step "$HERE/.github/workflows/ci.yml" "runner disk-pressure preflight")"
SKIP_PRIVATE_LIVE=0
if python3 - "$HERE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from public_snapshot_test_support import private_artifact_absent
raise SystemExit(0 if private_artifact_absent(
    sys.argv[1], ".github/workflows/runner-disk-pressure.yml") else 1)
PY
then
  SKIP_PRIVATE_LIVE=1
  printf '%s\n' 'SKIP: private live-host disk-pressure workflow is excluded from the fresh public snapshot; real public CI preflight and its mutation controls remain enabled'
else
  LIVE_STEP="$(extract_step "$HERE/.github/workflows/runner-disk-pressure.yml" "Check runner host disk pressure")"
  [[ -n "$LIVE_STEP" ]] && ok "live body extracted from workflow" || bad "live body empty"
fi
[[ -n "$CI_STEP" ]] && ok "preflight body extracted from ci.yml" || bad "preflight body empty"
# bracketed <body> -> 0 iff a `set +e` line precedes the rc=$? capture and a
# `set -e` line follows it. Comment lines (starting with #) never count.
bracketed() {
  awk '
    /^set \+e$/ {plus=1; next}
    plus && !seen && /rc=\$\?/ {seen=1; next}
    seen && /^set -e$/ {ok=1; exit}
    END {exit !ok}
  ' <<<"$1"
}
bracketed "$CI_STEP" && ok "preflight brackets the capture in set +e/set -e" || bad "preflight capture unbracketed"
if (( ! SKIP_PRIVATE_LIVE )); then
  bracketed "$LIVE_STEP" && ok "live step brackets the capture in set +e/set -e" || bad "live capture unbracketed"
fi
# A 99% df forces the REAL detector to rc=1 through the REAL step bodies.
cat > "$WORK/bin/df99" <<'STUB'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/md5 100 99 1 99%% /\n'
STUB
chmod +x "$WORK/bin/df99"
printf '%s\n' "$CI_STEP" > "$WORK/ci_step.sh"
STEP_OUT="$(cd "$HERE" && DF_BIN="$WORK/bin/df99" RUNNER_NAME="coolify-vps-1" bash -e "$WORK/ci_step.sh" 2>&1)"; STEP_RC=$?
(( STEP_RC == 1 )) && ok "preflight body exits 1 on pressure under bash -e" || bad "preflight died silent or wrong rc=$STEP_RC ($STEP_OUT)"
grep -q "::error title=Runner disk pressure" <<<"$STEP_OUT" \
  && ok "preflight reaches the ::error annotation" || bad "no ::error annotation ($STEP_OUT)"
# --- 7. preflight is advisory on ephemeral workers (case_dbfa63d9 3rd verdict) -----
hdr "Preflight is advisory on ephemeral workers (case_dbfa63d9 fleet gate)"
grep -q 'RUNNER_NAME:-.*coolify-vps-\*' <<<"$CI_STEP" \
  && ok "preflight gates on the persistent-fleet prefixes" || bad "no fleet gate in preflight"
grep -q '::warning title=Runner disk pressure (advisory' <<<"$CI_STEP" \
  && ok "ephemeral path emits a ::warning, not ::error" || bad "no advisory ::warning path"
# Same 99% df, same body, but an ephemeral worker name: must exit 0.
STEP_OUT="$(cd "$HERE" && DF_BIN="$WORK/bin/df99" RUNNER_NAME="garm-ephemeral-1" bash -e "$WORK/ci_step.sh" 2>&1)"; STEP_RC=$?
(( STEP_RC == 0 )) && ok "preflight exits 0 on pressure on an ephemeral worker" || bad "ephemeral worker failed the job (rc=$STEP_RC)"
grep -q "::warning title=Runner disk pressure (advisory" <<<"$STEP_OUT" \
  && ok "ephemeral pressure still annotates (advisory)" || bad "no advisory annotation ($STEP_OUT)"
! grep -q "::error" <<<"$STEP_OUT" \
  && ok "ephemeral path emits no ::error" || bad "ephemeral path raised ::error"
# Mutant control: widen the gate so it also matches the ephemeral worker
# name, and that worker must then fail. (Narrowing the patterns would leave
# a non-matching name on the advisory path — no behavior change, no control.
# Deleting the RUNNER_NAME lines outright would mangle the `if` syntax.)
sed 's/coolify-vps-\*/garm-*/' "$WORK/ci_step.sh" > "$WORK/ci_step_nofleet.sh"
MUT_OUT="$(cd "$HERE" && DF_BIN="$WORK/bin/df99" RUNNER_NAME="garm-ephemeral-1" bash -e "$WORK/ci_step_nofleet.sh" 2>&1)"; MUT_RC=$?
(( MUT_RC == 1 )) && ok "mutant (fleet gate voided) fails on ephemeral — gate is not vacuous" \
  || bad "fleet-gate mutant control failed (rc=$MUT_RC, out=$MUT_OUT)"
if (( ! SKIP_PRIVATE_LIVE )); then
  printf '%s\n' "$LIVE_STEP" > "$WORK/live_step.sh"
  STEP_OUT="$(cd "$HERE" && DF_BIN="$WORK/bin/df99" bash -e "$WORK/live_step.sh" 2>&1)"; STEP_RC=$?
  (( STEP_RC == 1 )) && ok "live body exits 1 on pressure under bash -e" || bad "live step died silent or wrong rc=$STEP_RC ($STEP_OUT)"
  grep -q "reclaim per docs/runbooks/runner-disk-pressure.md" <<<"$STEP_OUT" \
    && ok "live annotation names the runbook" || bad "live annotation missing runbook pointer ($STEP_OUT)"
fi
# The guard is not vacuous: strip the bracketing and the body must die silent.
grep -v '^set [+-]e$' "$WORK/ci_step.sh" > "$WORK/ci_step_mutant.sh"
MUT_OUT="$(cd "$HERE" && DF_BIN="$WORK/bin/df99" bash -e "$WORK/ci_step_mutant.sh" 2>&1)"; MUT_RC=$?
{ (( MUT_RC == 1 )) && ! grep -q "::error" <<<"$MUT_OUT"; } \
  && ok "mutant (bracketing stripped) dies silent — the guard is not vacuous" \
  || bad "mutant control failed (rc=$MUT_RC, out=$MUT_OUT)"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
