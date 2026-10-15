#!/usr/bin/env bash
# ===========================================================================
# test_case_705a486a_tmp_retention.sh — offline suite for classes 8 and 9 and the
# --tmp-only mode of ops/case_5866fa6f/disk-retention-sweep.sh (case_0ddff3ec: the
# controller root disk hit ENOSPC on 2026-10-04 with 9.1 GB of /tmp held by an
# orphaned pnpm store and 7.1 GB by unprefixed agent debris).
#
# What it pins (each guard has a mutant that proves the fixture exercises it):
#   1. REAP     — unlinked (link count 1), idle pnpm store content is reported
#                 in dry-run (and survives it) and deleted under --apply.
#   2. LINKED   — a store file a node_modules hard-links (link count 2) is kept
#                 even when old: it is in use.
#   3. IDLE     — content written inside PNPM_STORE_IDLE_HOURS is kept;
#                 SWEEP_PNPM_STORE_IDLE_HOURS=0 reclaims it (the emergency lever)
#                 while linked files still stay.
#   4. LIVENESS — a live process whose argv names pnpm (or pnpx, or a path ending
#                 in pnpm.cjs) keeps the whole store. The server's own
#                 /app/node_modules/.pnpm/.../esbuild must NOT: it is always
#                 running, and a substring match would keep the store forever.
#   5. FAIL CLOSED — no readable process cmdline keeps the whole store.
#   6. BOUNDARY — a symlinked .pnpm-store is never followed; files outside the
#                 store are never touched.
#   7. TMP-ONLY — --tmp-only skips classes 1-6 (a stale empty workspace husk that
#                 the full sweep reclaims survives it), and still runs 7-9.
#   8. SURVEY   — class 9 prints the owner-class table and lists unreferenced
#                 unprefixed entries older than the candidate bar, and never
#                 deletes anything, even under --apply. A referenced entry and a
#                 young one are not listed. Hostile names are printed sanitized.
#   9. ARGS     — an unknown flag exits 2 and deletes nothing.
#
# Hermetic: fake /proc and /tmp trees under mktemp via the SWEEP_* overrides;
# SWEEP_PAPERCLIP_HOME points classes 1-6 at a scratch home. No root, network,
# credential or real process table is used.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWEEP="$HERE/ops/case_5866fa6f/disk-retention-sweep.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -f "$SWEEP" ]] || { echo "REFUSE: $SWEEP not found — suite cannot attribute a result"; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required (class 9 survey)"; exit 3; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
NOW=$(date +%s)
OLD="@$((NOW - 7 * 3600))"      # older than PNPM_STORE_IDLE_HOURS=6
OLD50="@$((NOW - 50 * 3600))"   # older than SURVEY_CANDIDATE_HOURS=48
OLD15D="@$((NOW - 15 * 86400))" # older than EMPTY_DIR_AGE_DAYS=14

STORE="$WORK/tmp/.pnpm-store/v3/files"

mkfile() {  # <path> <MiB> <mtime-spec|NOW>
  mkdir -p "$(dirname "$1")"
  dd if=/dev/zero of="$1" bs=1048576 count="$2" status=none
  [[ "$3" == NOW ]] || touch -h -d "$3" "$1"
}

build_fixture() {
  rm -rf "$WORK/tmp" "$WORK/proc" "$WORK/home" "$WORK/elsewhere"
  mkdir -p "$WORK/tmp" "$WORK/proc" "$WORK/home/instances/default/workspaces" "$WORK/elsewhere"
  # --- pnpm store ---
  mkfile "$STORE/aa/orphan-old"   1 "$OLD"     # unlinked, idle      -> reaped
  mkfile "$STORE/bb/orphan-fresh" 1 NOW        # unlinked, just written -> kept (idle bar)
  mkfile "$STORE/cc/linked-old"   1 "$OLD"     # linked by a live install -> kept
  mkdir -p "$WORK/tmp/paperclip-run-live/node_modules"
  ln "$STORE/cc/linked-old" "$WORK/tmp/paperclip-run-live/node_modules/linked-old"
  # --- files that are not the store's ---
  mkfile "$WORK/elsewhere/outside" 1 "$OLD"
  # --- survey fixtures ---
  mkfile "$WORK/tmp/old-debris/blob"  2 "$OLD50"; touch -d "$OLD50" "$WORK/tmp/old-debris"
  mkfile "$WORK/tmp/live-debris/blob" 2 "$OLD50"; touch -d "$OLD50" "$WORK/tmp/live-debris"
  mkfile "$WORK/tmp/young-debris/blob" 2 NOW
  mkfile "$WORK/tmp/paperclip-run-ancient/blob" 2 "$OLD50"; touch -d "$OLD50" "$WORK/tmp/paperclip-run-ancient"
  mkfile "$WORK/tmp/$(printf 'evil\nname\tCANDIDATE')/blob" 1 "$OLD50"
  touch -d "$OLD50" "$WORK/tmp/$(printf 'evil\nname\tCANDIDATE')"
  # --- a stale empty workspace husk: class 1 reclaims it, --tmp-only must not ---
  mkdir -p "$WORK/home/instances/default/workspaces/husk"; touch -d "$OLD15D" "$WORK/home/instances/default/workspaces/husk"

  # --- fake process table: one process with readable environ and cmdline ---
  mkdir -p "$WORK/proc/101" "$WORK/proc/102"
  printf 'HOME=/paperclip\0PAPERCLIP_RUN_SCRATCH_DIR=/tmp/live-debris\0' > "$WORK/proc/101/environ"
  # the server's own long-running esbuild: carries ".pnpm" in its path, is not pnpm
  printf '/app/node_modules/.pnpm/@esbuild+linux-x64@0.28.2/node_modules/@esbuild/linux-x64/bin/esbuild\0--service=0.28.2\0--ping\0' \
    > "$WORK/proc/101/cmdline"
  printf 'HOME=/paperclip\0' > "$WORK/proc/102/environ"
  printf 'bash\0-c\0echo hello\0' > "$WORK/proc/102/cmdline"
}

run_sweep() {  # <script> [args...] → output in $OUT
  OUT=$(SWEEP_PAPERCLIP_HOME="$WORK/home" SWEEP_SCRATCH_ROOT="$WORK/tmp" \
        SWEEP_PROC_ROOT="$WORK/proc" bash "$1" "${@:2}" 2>&1)
}
exists() { [[ -e "$1" ]]; }
gone()   { [[ ! -e "$1" && ! -L "$1" ]]; }

# === 1. REAP + 2. LINKED + 3a. IDLE ==========================================
echo "=== 1. dry-run reports the unlinked idle file, deletes nothing ==="
build_fixture; run_sweep "$SWEEP"
grep -q 'WOULD-RM .*(1 unlinked store files)' <<<"$OUT" && ok "1a. exactly the one orphan reported" || bad "1a. orphan reported" "$OUT"
exists "$STORE/aa/orphan-old" && ok "1b. dry-run deleted nothing" || bad "1b. dry-run deleted nothing" "$OUT"

echo "=== 1-3. apply reclaims only unlinked idle content ==="
build_fixture; run_sweep "$SWEEP" --apply
gone "$STORE/aa/orphan-old" && ok "1c. unlinked idle store file deleted" || bad "1c. unlinked idle deleted" "$OUT"
exists "$STORE/cc/linked-old" && ok "2a. linked store file kept (in use)" || bad "2a. linked kept" "$OUT"
exists "$WORK/tmp/paperclip-run-live/node_modules/linked-old" && ok "2b. the node_modules link survives" || bad "2b. link survives" "$OUT"
exists "$STORE/bb/orphan-fresh" && ok "3a. fresh store file kept (idle bar)" || bad "3a. fresh kept" "$OUT"
exists "$WORK/elsewhere/outside" && ok "6a. file outside the store untouched" || bad "6a. outside untouched" "$OUT"

echo "=== 3b. emergency lever: 0 idle hours reclaims fresh unlinked content, still not linked ==="
build_fixture; SWEEP_PNPM_STORE_IDLE_HOURS=0 run_sweep "$SWEEP" --apply
OUT=$(SWEEP_PNPM_STORE_IDLE_HOURS=0 SWEEP_PAPERCLIP_HOME="$WORK/home" SWEEP_SCRATCH_ROOT="$WORK/tmp" SWEEP_PROC_ROOT="$WORK/proc" bash "$SWEEP" --apply 2>&1)
gone "$STORE/bb/orphan-fresh" && ok "3b. fresh unlinked file reclaimed at 0h" || bad "3b. fresh reclaimed at 0h" "$OUT"
exists "$STORE/cc/linked-old" && ok "3c. linked file still kept at 0h" || bad "3c. linked kept at 0h" "$OUT"

# === 4. LIVENESS ============================================================
echo "=== 4. a live pnpm keeps the whole store ==="
for argv in 'node\0/usr/local/bin/pnpm\0install\0' '/usr/local/bin/pnpm\0install\0--frozen-lockfile\0' \
            'node\0/usr/local/lib/node_modules/pnpm/bin/pnpm.cjs\0fetch\0' 'pnpx\0vitest\0'; do
  build_fixture; mkdir -p "$WORK/proc/103"; printf 'HOME=/paperclip\0' > "$WORK/proc/103/environ"
  printf "$argv" > "$WORK/proc/103/cmdline"
  run_sweep "$SWEEP" --apply
  exists "$STORE/aa/orphan-old" && grep -q 'keep all (live pnpm process' <<<"$OUT" \
    && ok "4a. kept under argv: $(tr '\0' ' ' < "$WORK/proc/103/cmdline")" || bad "4a. live pnpm keeps store ($argv)" "$OUT"
done
echo "=== 4b. the server's esbuild under /app/node_modules/.pnpm is not a live pnpm ==="
build_fixture; run_sweep "$SWEEP" --apply
gone "$STORE/aa/orphan-old" && ok "4b. .pnpm path substring does not keep the store" || bad "4b. substring false positive" "$OUT"

# === 5. FAIL CLOSED =========================================================
echo "=== 5. unreadable process table keeps the store ==="
build_fixture; rm -rf "$WORK/proc"/*; run_sweep "$SWEEP" --apply
exists "$STORE/aa/orphan-old" && grep -q 'liveness unverifiable' <<<"$OUT" \
  && ok "5a. empty /proc: nothing reaped" || bad "5a. empty /proc" "$OUT"
build_fixture; rm -f "$WORK/proc"/*/cmdline; run_sweep "$SWEEP" --apply
exists "$STORE/aa/orphan-old" && ok "5b. pid dirs without cmdline: nothing reaped" || bad "5b. pid dirs without cmdline" "$OUT"

# === 6. BOUNDARY ============================================================
echo "=== 6b. a symlinked .pnpm-store is never followed ==="
build_fixture; rm -rf "$WORK/tmp/.pnpm-store"
mkfile "$WORK/elsewhere/store/v3/files/aa/target" 1 "$OLD"
ln -s "$WORK/elsewhere/store" "$WORK/tmp/.pnpm-store"
run_sweep "$SWEEP" --apply
exists "$WORK/elsewhere/store/v3/files/aa/target" && ok "6b. symlink target untouched" || bad "6b. symlink followed" "$OUT"

# === 7. TMP-ONLY ============================================================
echo "=== 7. --tmp-only skips classes 1-6 ==="
build_fixture; run_sweep "$SWEEP" --apply
gone "$WORK/home/instances/default/workspaces/husk" && ok "7a. full sweep reclaims the husk (control)" || bad "7a. control" "$OUT"
build_fixture; run_sweep "$SWEEP" --tmp-only --apply
exists "$WORK/home/instances/default/workspaces/husk" && ok "7b. --tmp-only leaves the husk" || bad "7b. tmp-only skipped class 1" "$OUT"
gone "$STORE/aa/orphan-old" && ok "7c. --tmp-only still runs class 8" || bad "7c. tmp-only runs class 8" "$OUT"
grep -q '\[9\]' <<<"$OUT" && ok "7d. --tmp-only still runs class 9" || bad "7d. tmp-only runs class 9" "$OUT"

# === 8. SURVEY ==============================================================
echo "=== 8. class 9 is report-only ==="
build_fixture; run_sweep "$SWEEP" --tmp-only --apply
grep -q 'other (agent-made, unprefixed)' <<<"$OUT" && grep -q 'paperclip-run-\* (run scratch)' <<<"$OUT" \
  && grep -q '\.pnpm-store (pnpm content)' <<<"$OUT" && ok "8a. owner-class table printed" || bad "8a. class table" "$OUT"
grep -q 'CANDIDATE .*old-debris$' <<<"$OUT" && ok "8b. old unreferenced entry listed as candidate" || bad "8b. candidate" "$OUT"
! grep -q 'CANDIDATE .*live-debris$' <<<"$OUT" && ok "8c. live-referenced entry not listed" || bad "8c. live-referenced listed" "$OUT"
! grep -q 'CANDIDATE .*young-debris$' <<<"$OUT" && ok "8d. young entry not listed" || bad "8d. young listed" "$OUT"
! grep -q 'CANDIDATE .*paperclip-run-ancient$' <<<"$OUT" && ok "8e. run scratch is class 7's, never a survey candidate" || bad "8e. run scratch listed" "$OUT"
for d in old-debris live-debris young-debris paperclip-run-ancient; do
  exists "$WORK/tmp/$d/blob" || bad "8f. survey deleted $d under --apply" "$OUT"
done; exists "$WORK/tmp/old-debris/blob" && ok "8f. nothing the survey names is deleted, even under --apply" || true
# the hostile name's newline and tab must not forge a second CANDIDATE line
n=$(grep -c '^    CANDIDATE ' <<<"$OUT"); [[ "$n" == 2 ]] && ok "8g. hostile name prints as one sanitized candidate (2 candidates total)" || bad "8g. hostile name" "candidates=$n: $OUT"
grep -q 'evil?name?CANDIDATE' <<<"$OUT" && ok "8h. control characters printed as ?" || bad "8h. sanitized name" "$OUT"

# === 9. ARGS ================================================================
echo "=== 9. an unknown flag refuses ==="
build_fixture; OUT=$(SWEEP_PAPERCLIP_HOME="$WORK/home" SWEEP_SCRATCH_ROOT="$WORK/tmp" SWEEP_PROC_ROOT="$WORK/proc" bash "$SWEEP" --delete-everything 2>&1); rc=$?
[[ "$rc" == 2 ]] && exists "$STORE/aa/orphan-old" && ok "9a. unknown flag exits 2, deletes nothing" || bad "9a. unknown flag" "rc=$rc $OUT"

# === MUTANTS: each guard, removed, must flip a check above ===================
echo "=== mutants ==="
# mk_mutant <sed-expr>: $WORK/mutant.sh differs from the sweep in exactly one line
# and still parses; anything else is anchor drift and must not pass vacuously.
mk_mutant() {
  local m="$WORK/mutant.sh"
  sed "$1" "$SWEEP" > "$m" || return 1
  bash -n "$m" 2>/dev/null || return 1
  [[ "$(diff "$SWEEP" "$m" | grep -c '^>')" == 1 ]]
}
mutant() {  # <label> <sed-expr> <path that must then be gone> [fixture-tweak]
  mk_mutant "$2" || { bad "M. $1" "mutant is not a one-line parsable change (anchor drift)"; return; }
  build_fixture; [[ -n "${4:-}" ]] && eval "$4"
  run_sweep "$WORK/mutant.sh" --apply
  gone "$3" && ok "M. $1 mutant reaps $3" || bad "M. $1 mutant" "guard removal changed nothing: $OUT"
}
mutant "no link-count guard" '/store_find=(find/s/ -links 1//' "$STORE/cc/linked-old"
mutant "no idle bar"         '/store_find=(find/s/ -mmin +"\$((PNPM_STORE_IDLE_HOURS \* 60))"//' "$STORE/bb/orphan-fresh"
mutant "no live-pnpm check"  's/elif \[ -n "\$pnpm_live" \]; then/elif false; then/' "$STORE/aa/orphan-old" \
  'mkdir -p "$WORK/proc/103"; printf "HOME=/paperclip\0" > "$WORK/proc/103/environ"; printf "node\0/usr/local/bin/pnpm\0install\0" > "$WORK/proc/103/cmdline"'
mutant "no fail-closed"      's/if \[ "\$cmdline_read" = 0 \]; then/if false; then/' "$STORE/aa/orphan-old" 'rm -rf "$WORK/proc"/*'
mutant "no tmp-only skip"    's/^if \[ "\$TMP_ONLY" = 1 \]; then WS=.*$/true/' "$WORK/home/instances/default/workspaces/husk"
# the substring regression: matching "pnpm" anywhere keeps the store forever
if mk_mutant "s#grep -Eq '(^|/)pnp\[mx\](\\\\.\[cm\]?js)?\$' <<<\"\$cmd_txt\"#grep -Fqi pnpm <<<\"\$cmd_txt\"#"; then
  build_fixture; run_sweep "$WORK/mutant.sh" --apply
  exists "$STORE/aa/orphan-old" && ok "M. substring-match mutant keeps the store on the server's esbuild (the live regression)" \
    || bad "M. substring-match mutant" "store reclaimed: $OUT"
else bad "M. substring-match" "mutant is not a one-line parsable change (anchor drift)"; fi

# === EXIT GATE (last line: the script's status is the suite's status) ======
echo "---"
printf 'pass=%d fail=%d\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
