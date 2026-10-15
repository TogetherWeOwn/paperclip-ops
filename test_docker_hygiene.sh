#!/usr/bin/env bash
# ===========================================================================
# test_docker_hygiene.sh — regression suite for the case_b0e822f6 Docker hygiene
# timer payload (docker_hygiene.sh).
#
# The payload's load-bearing properties, in the order they matter:
#
#   1. Tagged rollback images are NEVER candidates. The image prune argv is
#      pinned to `image prune -f --filter until=Nh` with NO -a/--all token:
#      without -a, docker removes DANGLING (untagged) images only, so a
#      tagged image is unselectable whatever its age. A `-a` mutant must go
#      red here. No `rmi`, `volume`, `container`, `network` or `system prune`
#      argv may exist either (grep-level audit, not just the stub log).
#   2. Dry-run is the default. Bare invocation prunes nothing (exit 0) while
#      still printing the would-run receipts.
#   3. The kill switch wins over --apply: DOCKER_HYGIENE_DISABLED=1 forces a
#      dry-run even when --apply is passed.
#   4. It FAILS CLOSED. A missing docker binary, a non-integer age, or a
#      failed prune step is exit 2 — never exit 0. A run that pruned nothing
#      it cannot prove must never read as clean.
#   5. Usage errors (unknown flags) refuse before touching docker at all.
#
# Fully offline: DOCKER_BIN points at a stub that logs argv and replays
# canned outputs; `df` output is the stub's, docker root is a mktemp dir.
# No host docker, no network, no credential, no root.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${DOCKER_HYGIENE_SH:-$HERE/docker_hygiene.sh}"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }

# --- fixture: stub docker -------------------------------------------------
FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT
STUB="$FIX/docker"
ARGV_LOG="$FIX/argv.log"
FAIL_IMAGE=0
FAIL_BUILDER=0
export ARGV_LOG FAIL_IMAGE FAIL_BUILDER
cat > "$STUB" <<'STUB_EOF'
#!/usr/bin/env bash
# stub docker: log argv, replay canned outputs. FAIL_IMAGE/FAIL_BUILDER=1
# make the matching prune step exit 1.
echo "$*" >> "$ARGV_LOG"
case "$1" in
  info) echo "/tmp/fake-docker-root" ;;
  system) printf 'TYPE\tTOTAL\tACTIVE\tSIZE\tRECLAIMABLE\nImages\t5\t2\t59GB\t40GB (67%%)\nBuild Cache\t10\t0\t5GB\t5GB\n' ;;
  image) [ "${FAIL_IMAGE:-0}" = 1 ] && exit 1; echo "Total reclaimed space: 1GB" ;;
  builder) [ "${FAIL_BUILDER:-0}" = 1 ] && exit 1; echo "Total reclaimed space: 500MB" ;;
  *) exit 1 ;;
esac
STUB_EOF
chmod +x "$STUB"

reset_log() { : > "$ARGV_LOG"; }

run_tool() {
  # run_tool <outvar_rc> <outvar_out> [env KEY=VAL ...] -- [args ...]
  local __rcvar="$1" __outvar="$2"; shift 2
  local envs=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  [ $# -gt 0 ] && shift
  local _out _rc
  set +e
  _out=$(env DOCKER_BIN="$STUB" "${envs[@]}" bash "$TOOL" "$@" 2>&1); _rc=$?
  set -e
  printf -v "$__rcvar" '%s' "$_rc"
  printf -v "$__outvar" '%s' "$_out"
}

echo "# 1. dry-run default prunes nothing"
reset_log
run_tool rc out
[ "$rc" = 0 ] && ok "dry-run exits 0" || bad "dry-run exits 0 (rc=$rc)"
if grep -q "prune" "$ARGV_LOG"; then bad "dry-run invoked no prune (log: $(cat "$ARGV_LOG"))"; else ok "dry-run invoked no prune"; fi
case "$out" in *"DRY-RUN COMPLETE"*) ok "dry-run prints completion receipt" ;; *) bad "dry-run prints completion receipt" ;; esac

echo "# 2. --apply prunes with the pinned dangling-only argv"
reset_log
run_tool rc out -- --apply
[ "$rc" = 0 ] && ok "--apply exits 0" || bad "--apply exits 0 (rc=$rc)"
grep -Fx "image prune -f --filter until=168h" "$ARGV_LOG" >/dev/null \
  && ok "image prune argv pinned (dangling only)" \
  || bad "image prune argv pinned (log: $(cat "$ARGV_LOG" 2>/dev/null))"
grep -Fx "builder prune -f --filter until=168h" "$ARGV_LOG" >/dev/null \
  && ok "builder prune argv pinned (cache only)" \
  || bad "builder prune argv pinned (log: $(cat "$ARGV_LOG" 2>/dev/null))"
case "$out" in *"APPLY COMPLETE"*) ok "--apply prints completion receipt" ;; *) bad "--apply prints completion receipt" ;; esac

echo "# 3. forbidden argv never appears (rollback-image safety)"
reset_log
run_tool rc out -- --apply
forbidden=0
if grep -E -q '(^|[[:space:]])(-a|--all)([[:space:]]|$)' "$ARGV_LOG"; then forbidden=1; fi
for token in rmi volume container network "system prune"; do
  if grep -Fq "$token" "$ARGV_LOG"; then forbidden=1; fi
done
[ "$forbidden" = 0 ] && ok "no -a/rmi/volume/container/network/system-prune in stub log" \
  || bad "forbidden token in stub log: $(cat "$ARGV_LOG")"
stripped="$(grep -v '^[[:space:]]*#' "$TOOL")"
if grep -E -q '(^|[[:space:]"])(\-a|--all)([[:space:]]|$)|rmi|volume prune|container prune|network prune|system prune' <<<"$stripped"; then
  bad "forbidden token inside the payload source itself (non-comment lines)"
else
  ok "payload source carries no forbidden prune token"
fi

echo "# 4. custom age flows into both filters"
reset_log
run_tool rc out DOCKER_HYGIENE_AGE_HOURS=24 -- --apply
[ "$rc" = 0 ] && ok "custom age exits 0" || bad "custom age exits 0 (rc=$rc)"
if grep -Fq "until=24h" "$ARGV_LOG" && ! grep -Fq "until=168h" "$ARGV_LOG"; then
  ok "age 24h used in both filters"
else
  bad "age 24h used in both filters (log: $(cat "$ARGV_LOG"))"
fi

echo "# 5. kill switch beats --apply"
reset_log
run_tool rc out DOCKER_HYGIENE_DISABLED=1 -- --apply
[ "$rc" = 0 ] && ok "kill-switched run exits 0" || bad "kill-switched run exits 0 (rc=$rc)"
if grep -q "prune" "$ARGV_LOG"; then bad "kill switch pruned nothing (log: $(cat "$ARGV_LOG"))"; else ok "kill switch pruned nothing"; fi
case "$out" in *"KILL-SWITCH"*) ok "kill switch announced" ;; *) bad "kill switch announced" ;; esac

echo "# 6. fail-closed cases"
reset_log
run_tool rc out DOCKER_HYGIENE_AGE_HOURS=banana -- --apply
[ "$rc" = 2 ] && ok "non-integer age exits 2" || bad "non-integer age exits 2 (rc=$rc)"
if grep -q "prune" "$ARGV_LOG"; then bad "bad age pruned nothing"; else ok "bad age pruned nothing"; fi
run_tool rc out DOCKER_HYGIENE_AGE_HOURS=0 -- --apply
[ "$rc" = 2 ] && ok "zero age exits 2" || bad "zero age exits 2 (rc=$rc)"
run_tool rc out -- --frobnicate
[ "$rc" = 2 ] && ok "unknown flag exits 2" || bad "unknown flag exits 2 (rc=$rc)"
if grep -q "frobnicate" "$ARGV_LOG"; then bad "unknown flag never reached docker"; else ok "unknown flag never reached docker"; fi
set +e
out=$(env DOCKER_BIN="$FIX/no-such-docker" bash "$TOOL" --apply 2>&1); rc=$?
set -e
[ "$rc" = 2 ] && ok "missing docker binary exits 2" || bad "missing docker binary exits 2 (rc=$rc)"

echo "# 7. failed prune step is loud"
reset_log
run_tool rc out -- --apply
[ "$rc" = 0 ] || bad "baseline apply green before failure control (rc=$rc)"
FAIL_IMAGE=1; export FAIL_IMAGE
reset_log
run_tool rc out -- --apply
[ "$rc" = 2 ] && ok "failed image prune exits 2" || bad "failed image prune exits 2 (rc=$rc)"
case "$out" in *"FAILURES"*) ok "failure announced" ;; *) bad "failure announced" ;; esac
FAIL_IMAGE=0; export FAIL_IMAGE
FAIL_BUILDER=1; export FAIL_BUILDER
reset_log
run_tool rc out -- --apply
[ "$rc" = 2 ] && ok "failed builder prune exits 2" || bad "failed builder prune exits 2 (rc=$rc)"
FAIL_BUILDER=0; export FAIL_BUILDER

echo
echo "docker-hygiene suite: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
