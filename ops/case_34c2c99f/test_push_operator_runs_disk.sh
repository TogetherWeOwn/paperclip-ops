#!/usr/bin/env bash
# test_push_operator_runs_disk.sh — offline tests for push-operator-runs-disk.sh
# (case_34c2c99f).
#
# Written to FAIL a plausible-but-wrong wrapper, not to confirm the one that
# exists. Every case names the mutant it kills. The wrapper PUSHES a health
# boolean about /home pressure, so the tests that matter most prove it never
# reports healthy from a measurement it could not make: a refusing pruner, an
# unparseable summary, a summary without the footprint figure, a missing API
# env, a missing helper, and bad thresholds must all push false (or refuse
# loudly), never true. And the opposite: a stderr notice or a tripped kill
# switch must not turn a healthy footprint red.
#
# Deterministic: no network, no credentials. The pruner and the Gatus helper
# are stubs; push calls are captured to a file, never sent.
#
#   bash ops/case_34c2c99f/test_push_operator_runs_disk.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$HERE/push-operator-runs-disk.sh"

pass=0; fail=0
check() { # name expected actual — mutant: inverted assertions pass anything
  if [[ "$2" == "$3" ]]; then pass=$((pass+1));
  else fail=$((fail+1)); echo "FAIL: $1 (expected [$2], got [$3])"; fi
}

# Control-plane env the wrapper requires (values are dummies; the pruner is a stub).
export PAPERCLIP_API_URL=http://stub PAPERCLIP_COMPANY_ID=stub-company PAPERCLIP_API_KEY=stub-key

fresh() { # build stubs; echoes tmpdir. Mutant: stubs leak between cases.
  local tmp; tmp="$(mktemp -d)"
  cat > "$tmp/gatus_stub.sh" <<'EOF'
gatus_push() { printf '%s|%s|%s\n' "$1" "$2" "${3:-}" >> "$CAPTURE"; return "${STUB_PUSH_RC:-0}"; }
EOF
  echo "$tmp"
}

run_wrapper() { # extra env assignments as args; stdout+stderr discarded
  env OPERATOR_PRUNE_TOOL="$t/pruner" OPERATOR_RUNS_ROOT=/nonexistent GATUS_URL=x GATUS_EXTERNAL_TOKEN=y \
    "$@" bash "$WRAPPER" >/dev/null 2>&1
}

stub_pruner() { # body of a bash pruner stub on stdin
  { echo '#!/usr/bin/env bash'; cat; } > "$t/pruner"; chmod +x "$t/pruner"
}

# 1. Mutant: clean state pushes false (alert fatigue) or exits nonzero.
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo '{"removable_gib": 1.5, "trim_planned_gib": 0.5, "runs_total_gib": 2.0}'
EOF
run_wrapper; rc=$?
check "clean exits 0" 0 "$rc"
check "clean pushes true" "platform_operator-runs-disk|true|" "$(cat "$CAPTURE")"
rm -rf "$t"

# 2. Mutant: warn breach pushes true (pressure invisible).
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo '{"removable_gib": 1.0, "trim_planned_gib": 1.0, "runs_total_gib": 25.0}'
EOF
run_wrapper; rc=$?
check "warn exits 1" 1 "$rc"
check "warn pushes false+warn" "platform_operator-runs-disk|false|warn: operator run folders use 25.0 GiB" "$(cat "$CAPTURE")"
rm -rf "$t"

# 3. Mutant: page breach reads as a mere warn.
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo '{"removable_gib": 0, "trim_planned_gib": 0, "runs_total_gib": 55.0}'
EOF
run_wrapper; rc=$?
check "page exits 1" 1 "$rc"
check "page pushes false+page" "platform_operator-runs-disk|false|page: operator run folders use 55.0 GiB (CEO desk escalated)" "$(cat "$CAPTURE")"
rm -rf "$t"

# 4. Mutant: refusing pruner reads as healthy; its stderr (not stdout noise)
#    is the message.
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo "ERROR: live-runs unreadable" >&2; exit 2
EOF
run_wrapper; rc=$?
check "refusal exits 1" 1 "$rc"
check "refusal pushes false" "platform_operator-runs-disk|false|pruner refused (exit 2): ERROR: live-runs unreadable " "$(cat "$CAPTURE")"
rm -rf "$t"

# 5. Mutant: garbage summary reads as healthy.
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo 'not json'
EOF
run_wrapper; rc=$?
check "garbage exits 1" 1 "$rc"
check "garbage pushes false" "platform_operator-runs-disk|false|pruner summary unparseable or missing runs_total_gib" "$(cat "$CAPTURE")"
rm -rf "$t"

# 6. Mutant: missing helper proceeds anyway.
GATUS_HELPER=/nonexistent OPERATOR_PRUNE_TOOL=/nonexistent \
  bash "$WRAPPER" >/dev/null 2>&1; rc=$?
check "missing helper exits 2" 2 "$rc"

# 7. Mutant: warn >= page accepted (thresholds that can never page).
t="$(fresh)"; export GATUS_HELPER="$t/gatus_stub.sh"
OPERATOR_PRUNE_TOOL=/nonexistent OPERATOR_RUNS_WARN_GIB=50 OPERATOR_RUNS_PAGE_GIB=50 \
  bash "$WRAPPER" >/dev/null 2>&1; rc=$?
check "bad thresholds exit 2" 2 "$rc"
rm -rf "$t"

# 8. Mutant: valid JSON without the footprint figure defaults to 0 and pushes
#    healthy (the false green when the pruner kept everything as unverifiable).
for body in '{}' '{"removable_gib": 0, "trim_planned_gib": 0}' '{"runs_total_gib": null}' '{"runs_total_gib": "12"}' '{"runs_total_gib": -1}' '{"runs_total_gib": true}'; do
  t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
  stub_pruner <<EOF
echo '$body'
EOF
  run_wrapper; rc=$?
  check "figure-less summary $body exits 1" 1 "$rc"
  check "figure-less summary $body pushes false" "platform_operator-runs-disk|false|pruner summary unparseable or missing runs_total_gib" "$(cat "$CAPTURE")"
  rm -rf "$t"
done

# 9. Mutant: stdout and stderr merged — a pruner notice on stderr corrupts the
#    JSON, so a healthy footprint under an ACTIVE KILL SWITCH reads as
#    "unparseable" and the monitor goes red for a deliberate disable.
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo "KILL-SWITCH (env OPERATOR_RUN_PRUNE_DISABLED): apply disabled, dry-run only" >&2
echo '{"kill_switch": "env OPERATOR_RUN_PRUNE_DISABLED", "runs_total_gib": 3.0}'
EOF
run_wrapper; rc=$?
check "kill-switch healthy exits 0" 0 "$rc"
check "kill-switch healthy pushes true" "platform_operator-runs-disk|true|" "$(cat "$CAPTURE")"
rm -rf "$t"

# 10. A breach under the kill switch says so (nothing is reclaiming it).
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo "KILL-SWITCH (file x): apply disabled, dry-run only" >&2
echo '{"kill_switch": "file x", "runs_total_gib": 30.0}'
EOF
run_wrapper; rc=$?
check "kill-switch breach exits 1" 1 "$rc"
check "kill-switch breach names the switch" "platform_operator-runs-disk|false|warn: operator run folders use 30.0 GiB (prune kill switch is ON: nothing is being reclaimed)" "$(cat "$CAPTURE")"
rm -rf "$t"

# 11. Mutant: missing control-plane env is left to the pruner (opaque), or
#     worse, the pruner is invoked without it. Each var is named, none valued.
for var in PAPERCLIP_API_URL PAPERCLIP_COMPANY_ID PAPERCLIP_API_KEY; do
  t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
  stub_pruner <<EOF
touch "$t/pruner-ran"
echo '{"runs_total_gib": 1.0}'
EOF
  run_wrapper "$var="; rc=$?
  check "unset $var exits 1" 1 "$rc"
  check "unset $var pushes false naming it" "platform_operator-runs-disk|false|pruner env incomplete: $var is unset (cron env file must carry PAPERCLIP_API_*)" "$(cat "$CAPTURE")"
  [[ -e "$t/pruner-ran" ]] && ran=ran || ran=skipped
  check "unset $var never runs the pruner" skipped "$ran"
  rm -rf "$t"
done

# 12. Mutant: OPERATOR_RUNS_USER ignored — the pruner runs as the caller (root
#     in cron) and git refuses every folder as dubious ownership. The stub drop
#     command records its argv; the pruner must run under it as that user with
#     HOME set, and the API key must NEVER appear on that argv.
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
me="$(id -un)"; mygid="$(id -g)"
# Paths are baked in: the wrapper scrubs the env the drop command runs under.
cat > "$t/drop" <<EOF
#!/usr/bin/env bash
printf '%s\\n' "\$@" > "$t/drop-argv"
while [[ "\$1" != "--" ]]; do shift; done; shift
exec "\$@"
EOF
chmod +x "$t/drop"
stub_pruner <<EOF
env > "$t/pruner-env"
echo '{"runs_total_gib": 4.0}'
EOF
DROP_ARGV="$t/drop-argv"; PRUNER_ENV="$t/pruner-env"
run_wrapper OPERATOR_RUNS_USER="$me" OPERATOR_RUNS_DROP_CMD="$t/drop" \
  SOME_OTHER_ROOT_SECRET=hunter2; rc=$?
check "drop-priv healthy exits 0" 0 "$rc"
check "drop-priv healthy pushes true" "platform_operator-runs-disk|true|" "$(cat "$CAPTURE")"
check "drop-priv uses --reuid" "--reuid|$me|--regid|$mygid|--init-groups|--" "$(head -n 6 "$DROP_ARGV" | paste -sd'|')"
grep -q '^HOME=' "$DROP_ARGV" && check "drop-priv sets HOME" 0 0 || check "drop-priv sets HOME" 0 1
grep -q 'stub-key' "$DROP_ARGV" && check "api key never on drop argv" 0 1 || check "api key never on drop argv" 0 0
# Mutant: the dropped pruner inherits the whole sourced root env (Gatus token,
# every other secret) instead of a scrubbed one; it still needs the API env.
grep -q '^GATUS_EXTERNAL_TOKEN=' "$PRUNER_ENV" && check "gatus token not in dropped env" 0 1 || check "gatus token not in dropped env" 0 0
grep -q '^SOME_OTHER_ROOT_SECRET=' "$PRUNER_ENV" && check "other root secret not in dropped env" 0 1 || check "other root secret not in dropped env" 0 0
grep -q '^PAPERCLIP_API_KEY=stub-key$' "$PRUNER_ENV" && check "api env reaches dropped pruner" 0 0 || check "api env reaches dropped pruner" 0 1
rm -rf "$t"

# 13. Mutant: OPERATOR_RUNS_USER is not validated — an option-shaped or unknown
#     name reaches setpriv/id.
t="$(fresh)"; export CAPTURE="$t/calls" STUB_PUSH_RC=0 GATUS_HELPER="$t/gatus_stub.sh"
stub_pruner <<'EOF'
echo '{"runs_total_gib": 1.0}'
EOF
run_wrapper OPERATOR_RUNS_USER='--reuid=0'; rc=$?
check "option-shaped user exits 2" 2 "$rc"
run_wrapper OPERATOR_RUNS_USER=no_such_user_case_9e39ef38; rc=$?
check "unknown user exits 2" 2 "$rc"
check "bad user pushes nothing" "" "$(cat "$CAPTURE" 2>/dev/null)"
rm -rf "$t"

echo "push-wrapper tests: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
