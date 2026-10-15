#!/usr/bin/env bash
# ===========================================================================
# test_case_79b3d149_scratch_reaper.sh — offline suite for class 7 of
# ops/case_5866fa6f/disk-retention-sweep.sh: orphaned per-run scratch under the
# Paperclip server's tmpdir (case_999adcc5, node7b1ff338 root disk at 94%).
#
# What it pins (each guard has a mutant that proves the fixture exercises it):
#   1. REAP     — a marked, unreferenced, idle paperclip-run-* dir is reported
#                 in dry-run (and survives it) and deleted under --apply.
#   2. LIVENESS — a dir named by any live process's environ (whole value or a
#                 colon-list entry), cwd or open fd is kept, even when idle.
#   3. BOUNDARY — a live reference to paperclip-run-abcd does not protect
#                 paperclip-run-abc; matching is per path component.
#   4. PROVENANCE — no marker, a marker without runId, a symlink, an
#                 unexpected name or a non-paperclip-run dir is never touched.
#   5. IDLE     — anything modified inside SCRATCH_IDLE_HOURS keeps the dir.
#   6. FAIL CLOSED — no readable process environ (empty /proc, or pid dirs
#                 without environ) keeps everything, even under --apply.
#
# Hermetic: fake /proc and /tmp trees under mktemp via the SWEEP_* overrides;
# SWEEP_PAPERCLIP_HOME points classes 1-6 at an empty dir. No root, network,
# credential or real process table is used.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWEEP="$HERE/ops/case_5866fa6f/disk-retention-sweep.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -f "$SWEEP" ]] || { echo "REFUSE: $SWEEP not found — suite cannot attribute a result"; exit 3; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
OLD="@$(( $(date +%s) - 7 * 3600 ))"   # older than SCRATCH_IDLE_HOURS=6

# scratch <name> [marker-json]: a scratch dir with one file, all aged to $OLD
scratch() {
  local d="$WORK/tmp/$1"
  mkdir -p "$d/sub"; echo data > "$d/sub/file"
  if [[ "${2:-}" == NONE ]]; then :
  elif [[ $# -ge 2 ]]; then printf '%s' "$2" > "$d/.paperclip-run-scratch.json"
  else printf '{"version":1,"runId":"%s"}' "$1" > "$d/.paperclip-run-scratch.json"; fi
  find "$d" -exec touch -h -d "$OLD" {} +
}

build_fixture() {
  rm -rf "$WORK/tmp" "$WORK/proc" "$WORK/home" "$WORK/elsewhere"
  mkdir -p "$WORK/tmp" "$WORK/proc" "$WORK/home" "$WORK/elsewhere"
  scratch paperclip-run-orphan
  scratch paperclip-run-abc                       # orphan; only abcd is live
  scratch paperclip-run-abcd
  scratch paperclip-run-envlive
  scratch paperclip-run-pathlive
  scratch paperclip-run-cwdlive
  scratch paperclip-run-fdlive
  scratch paperclip-run-recent; touch "$WORK/tmp/paperclip-run-recent/sub/file"
  scratch paperclip-run-nomarker NONE
  scratch paperclip-run-norunid '{"version":1,"agentId":"x"}'
  scratch paperclip-run-odd.name
  scratch not-a-run
  mkdir -p "$WORK/elsewhere/target"; echo keep > "$WORK/elsewhere/target/f"
  printf '{"runId":"r"}' > "$WORK/elsewhere/target/.paperclip-run-scratch.json"
  find "$WORK/elsewhere" -exec touch -h -d "$OLD" {} +
  ln -s "$WORK/elsewhere/target" "$WORK/tmp/paperclip-run-symlink"

  local T=/tmp   # container-namespace paths: matching is by basename
  mkdir -p "$WORK/proc/101" "$WORK/proc/102" "$WORK/proc/103/fd" "$WORK/proc/104" "$WORK/proc/105"
  printf 'HOME=/paperclip\0PAPERCLIP_RUN_SCRATCH_DIR=%s/paperclip-run-envlive\0' "$T" > "$WORK/proc/101/environ"
  printf 'PATH=/usr/bin:%s/paperclip-run-pathlive/bin:/bin\0' "$T" > "$WORK/proc/102/environ"
  printf 'HOME=/paperclip\0' > "$WORK/proc/103/environ"
  ln -s "$T/paperclip-run-fdlive/sub/file" "$WORK/proc/103/fd/3"
  printf 'HOME=/paperclip\0TMPDIR=%s/paperclip-run-abcd\0' "$T" > "$WORK/proc/104/environ"
  printf 'HOME=/paperclip\0' > "$WORK/proc/105/environ"
  ln -s "$T/paperclip-run-cwdlive/sub" "$WORK/proc/105/cwd"
}

run_sweep() {  # <script> [--apply] → output in $OUT
  OUT=$(SWEEP_PAPERCLIP_HOME="$WORK/home" SWEEP_SCRATCH_ROOT="$WORK/tmp" \
        SWEEP_PROC_ROOT="$WORK/proc" bash "$1" "${@:2}" 2>&1)
}
gone()    { [[ ! -e "$WORK/tmp/$1" && ! -L "$WORK/tmp/$1" ]]; }
present() { [[ -e "$WORK/tmp/$1" || -L "$WORK/tmp/$1" ]]; }

KEEP=(paperclip-run-abcd paperclip-run-envlive paperclip-run-pathlive paperclip-run-cwdlive
      paperclip-run-fdlive paperclip-run-recent paperclip-run-nomarker paperclip-run-norunid
      paperclip-run-odd.name paperclip-run-symlink not-a-run)

# === 1. REAP (dry-run then apply) ==========================================
echo "=== 1. dry-run reports, deletes nothing ==="
build_fixture; run_sweep "$SWEEP"
grep -q 'WOULD-RM .*run scratch paperclip-run-orphan$' <<<"$OUT" && ok "1a. orphan reported" || bad "1a. orphan reported" "$OUT"
grep -q 'WOULD-RM .*run scratch paperclip-run-abc$' <<<"$OUT" && ok "1b. prefix-sibling orphan reported" || bad "1b. prefix-sibling orphan reported" "$OUT"
present paperclip-run-orphan && ok "1c. dry-run deleted nothing" || bad "1c. dry-run deleted nothing" "orphan removed in dry-run"
n=$(grep -c 'WOULD-RM' <<<"$OUT"); [[ "$n" == 2 ]] && ok "1d. exactly 2 candidates" || bad "1d. exactly 2 candidates" "got $n: $OUT"

echo "=== 2-5. apply reaps only the orphans ==="
build_fixture; run_sweep "$SWEEP" --apply
gone paperclip-run-orphan && ok "2a. orphan deleted" || bad "2a. orphan deleted" "$OUT"
gone paperclip-run-abc && ok "3a. abc reaped despite live abcd (component boundary)" || bad "3a. boundary" "$OUT"
for k in "${KEEP[@]}"; do
  present "$k" && ok "2-5. kept $k" || bad "2-5. kept $k" "$OUT"
done
[[ -f "$WORK/elsewhere/target/f" ]] && ok "4a. symlink target untouched" || bad "4a. symlink target untouched" "$OUT"

# === 6. FAIL CLOSED =========================================================
echo "=== 6. unreadable process table keeps everything ==="
build_fixture; rm -rf "$WORK/proc"/*; run_sweep "$SWEEP" --apply
present paperclip-run-orphan && grep -q 'liveness unverifiable' <<<"$OUT" \
  && ok "6a. empty /proc: nothing reaped" || bad "6a. empty /proc" "$OUT"
build_fixture; rm -f "$WORK/proc"/*/environ; run_sweep "$SWEEP" --apply
present paperclip-run-orphan && present paperclip-run-envlive \
  && ok "6b. pid dirs without environ: nothing reaped" || bad "6b. pid dirs without environ" "$OUT"

# === MUTANTS: each guard, removed, must flip a check above ==================
echo "=== mutants ==="
# mk_mutant <sed-expr>: $WORK/mutant.sh differs from the sweep in exactly one line
# and still parses; anything else is anchor drift and must not pass vacuously.
mk_mutant() {
  local m="$WORK/mutant.sh"
  sed "$1" "$SWEEP" > "$m" || return 1
  bash -n "$m" 2>/dev/null || return 1
  [[ "$(diff "$SWEEP" "$m" | grep -c '^>')" == 1 ]]
}
mutant() {  # <label> <sed-expr> <dir that must then be reaped> [fixture-tweak]
  mk_mutant "$2" || { bad "M. $1" "mutant is not a one-line parsable change (anchor drift)"; return; }
  build_fixture; [[ -n "${4:-}" ]] && eval "$4"
  run_sweep "$WORK/mutant.sh" --apply
  gone "$3" && ok "M. $1 mutant reaps $3" || bad "M. $1 mutant" "guard removal changed nothing: $OUT"
}
mutant "no live-reference check" 's|<<<"\$live_refs"; then|<<<""; then|' paperclip-run-envlive
mutant "no idle bar"            's/\[ -z "\$recent" \] || \(.*SCRATCH_IDLE_HOURS\)/true || \1/' paperclip-run-recent
mutant "no marker check"        's|grep -Eqs .*SCRATCH_MARKER" \\$|true \\|' paperclip-run-nomarker
mutant "no fail-closed"         's|if \[ "\$environ_read" = 0 \]; then|if false; then|' paperclip-run-orphan 'rm -rf "$WORK/proc"/*'
# boundary: dropping the component terminator makes the live abcd protect abc
if mk_mutant 's#(\[/:\]|\\\$)"#"#'; then
  build_fixture; run_sweep "$WORK/mutant.sh" --apply
  present paperclip-run-abc && grep -q 'keep (live reference)   paperclip-run-abc$' <<<"$OUT" \
    && ok "M. no component boundary mutant keeps abc (prefix match)" \
    || bad "M. no component boundary mutant" "abc not kept as live: $OUT"
else bad "M. no component boundary" "mutant is not a one-line parsable change (anchor drift)"; fi

# === EXIT GATE (last line: the script's status is the suite's status) ======
echo "---"
printf 'pass=%d fail=%d\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
