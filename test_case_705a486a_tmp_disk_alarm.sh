#!/usr/bin/env bash
# ===========================================================================
# test_case_705a486a_tmp_disk_alarm.sh — offline suite for ops/case_0ddff3ec/tmp-disk-alarm.sh
# (case_0ddff3ec: the controller root disk reached 100% on 2026-10-04 and 13 runs died
# with ENOSPC; the existing coverage paged at 90% on a 5-minute poll and had no warn
# tier at all).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. HEALTHY  — 40% pushes success=true to both endpoints; exit 0. Without this a
#                 suite that only asserts failures passes against a script that
#                 fails everything.
#   2. WARN     — exactly 80% pushes warn=false, page=true; 79.9% pushes both true.
#   3. PAGE     — exactly 90% pushes both false; 89.9% warns but does not page.
#   4. FORMULA  — the percentage is used/(used+avail), the number `df` prints: the
#                 real 2026-10-04 reading (78.9 GB used, 20.7 GB free) is 79.2%.
#   5. FAIL CLOSED — a failing source, empty output, garbage and a zero-size table
#                 each push the page endpoint false and exit 1; never healthy.
#   6. REFUSALS — warn >= crit, non-numeric thresholds, a missing GATUS_URL or token
#                 exit 2 with zero pushes; --dry-run needs neither and pushes nothing.
#   7. TOP CONSUMERS — appended to the reason only on a breach, control characters
#                 become '?', the line is bounded, and printed rows survive a
#                 nonzero top-command exit (du exits 1 on vanishing entries).
#   8. NON-DISCLOSURE — the Bearer token never reaches curl argv.
#   9. EMIT-YAML — both endpoints, group controller, OWNER devops and the runbook;
#                 only the page endpoint alerts Akmatori (custom).
#  10. MUTANTS — each guard, removed, flips a check above.
#  11. UNITS   — the four systemd units point at scripts that exist, run the sweep
#                with --tmp-only --apply, carry Persistent timers, and the env
#                example holds no inner quotes (systemd strips them).
#  12. GROWTH  — case_76fe8329: a jump of TMPDISK_GROWTH_WARN_BYTES (default 5 GiB)
#                in used bytes between two consecutive samples trips warn even
#                below the 80% level (the 2026-10-04 spike lasted ~80 s inside a
#                5-minute sample gap, so level tiers alone miss it). Pinned by
#                three offline fixture series: a spike series (warns), a
#                slow-ramp series (follows the existing level rules, stays quiet
#                below 80%), and a flat series (stays quiet). Missing, corrupt,
#                future-dated or stale (> TMPDISK_GROWTH_MAX_AGE_SEC, default
#                1800 s) previous readings skip the growth check; --dry-run
#                never touches the state file; removing the growth check flips
#                the spike fixture back to healthy (mutation). Multi-run chains
#                pin the hold: a jump pushes warn=false on exactly
#                ALERT_FAILURE_THRESHOLD (3) consecutive runs, which is what
#                Gatus needs to alert; a second jump restarts it; page never
#                pushes false on growth.
#
# Hermetic: no network, no credentials. A stub curl records argv and every --config
# file's contents and answers 200; readings come from printf fixtures.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ops/case_0ddff3ec/tmp-disk-alarm.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$TOOL" ]] || { echo "REFUSE: $TOOL missing or not executable" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_tmp_disk_alarm.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export ARGV_LOG="$WORK/argv.log" CFG_LOG="$WORK/cfg.log"
: > "$ARGV_LOG"; : > "$CFG_LOG"

cat > "$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ARGV_LOG"
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cat "${!j}" >> "$CFG_LOG"; fi
done
printf '200'
exit 0
STUB
chmod +x "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"

export GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-token-not-a-secret"
WARN=controller_tmp-disk-warn; PAGE=controller_tmp-disk
# Hermetic history: every run below reads and writes history here, never the
# operator's real ~/.cache state. run_tool starts with no history (a first run
# after install); run_seeded replays a previous sample.
export TMPDISK_STATE_FILE="$WORK/state.prev"

# df_table <used-bytes> <avail-bytes>: a POSIX `df -P -B1` table
df_table() {
  printf 'Filesystem 1-blocks Used Available Capacity Mounted on\noverlay %s %s %s 0%% /\n' "$(( $1 + $2 ))" "$1" "$2"
}
# pct_table <pct>: a table whose used/(used+avail) is exactly <pct> (integer)
pct_table() { df_table "$1" "$((100 - $1))"; }

reset_logs() { : > "$ARGV_LOG"; : > "$CFG_LOG"; : > "$WORK/out.txt"; : > "$WORK/err.txt"; }
run_tool() {  # command...; echoes the exit code, output in $WORK/out.txt and err.txt
  reset_logs
  rm -f "$TMPDISK_STATE_FILE"   # a first run: no history, so no growth check
  local rc
  "$@" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  printf '%s' "$rc"
}
seed_state() { printf '%s %s\n' "$1" "$2" > "$TMPDISK_STATE_FILE"; }  # <epoch> <used-bytes>
run_seeded() {  # <prev-epoch> <prev-used-bytes> command...; replays a previous sample
  reset_logs
  seed_state "$1" "$2"; shift 2
  local rc
  "$@" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  printf '%s' "$rc"
}
pushes_for() { grep -o "endpoints/$1/external?success=[a-z]*" "$CFG_LOG" | sed 's/.*success=//'; }
decoded_cfg() { python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open(sys.argv[1]).read()))' "$CFG_LOG"; }
cfg_has() { local out; out="$(decoded_cfg)"; grep -q -- "$1" <<<"$out"; }
push_count() { grep -c "endpoints/" "$CFG_LOG" || true; }
src() { printf 'cat %s' "$1"; }   # TMPDISK_SOURCE_CMD that replays a fixture file

# --- §1 healthy -------------------------------------------------------------
echo "=== 1. healthy ==="
pct_table 40 > "$WORK/t"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$rc" == 0 ]] && ok "1a. exit 0" || bad "1a. exit" "rc=$rc $(cat "$WORK/err.txt")"
[[ "$(pushes_for "$WARN")" == true && "$(pushes_for "$PAGE")" == true ]] && ok "1b. both endpoints true" || bad "1b. pushes" "$(cat "$CFG_LOG")"

# --- §2 warn boundary ---------------------------------------------------------
echo "=== 2. warn boundary ==="
pct_table 80 > "$WORK/t"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$rc" == 0 ]] && ok "2a. 80% exits 0 (the breach is carried by Gatus, not the exit status)" || bad "2a. exit" "rc=$rc"
[[ "$(pushes_for "$WARN")" == false ]] && ok "2b. 80% pushes warn=false" || bad "2b. warn at 80" "$(cat "$CFG_LOG")"
[[ "$(pushes_for "$PAGE")" == true ]] && ok "2c. 80% does not page" || bad "2c. page at 80" "$(cat "$CFG_LOG")"
df_table 799 201 > "$WORK/t"   # 79.9%
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == true ]] && ok "2d. 79.9% stays healthy (positive control for 2b)" || bad "2d. warn at 79.9" "$(cat "$CFG_LOG")"

# --- §3 page boundary ---------------------------------------------------------
echo "=== 3. page boundary ==="
pct_table 90 > "$WORK/t"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == false && "$(pushes_for "$PAGE")" == false ]] && ok "3a. 90% pushes both false" || bad "3a. pushes at 90" "$(cat "$CFG_LOG")"
cfg_has "used 90" && cfg_has "GB free of" && ok "3b. reason carries the reading" || bad "3b. reason" "$(decoded_cfg)"
df_table 899 101 > "$WORK/t"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$(pushes_for "$PAGE")" == true && "$(pushes_for "$WARN")" == false ]] && ok "3c. 89.9% warns but does not page (positive control for 3a)" || bad "3c. 89.9" "$(cat "$CFG_LOG")"

# --- §4 formula ---------------------------------------------------------------
echo "=== 4. used/(used+avail), the number df prints ==="
# The 2026-10-04 22:33Z reading from metrics/improvement/latest.json diskBytes.
df_table 78938828800 20700196864 > "$WORK/t"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL" --dry-run)"
grep -q 'used 79.2' "$WORK/out.txt" && ok "4a. incident reading scores 79.2% (used/total would score 75.2%)" || bad "4a. formula" "$(cat "$WORK/out.txt")"
[[ "$(push_count)" == 0 ]] && ok "4b. --dry-run pushed nothing" || bad "4b. dry-run pushes" "$(cat "$CFG_LOG")"
df_table 80000000000 17000000000 > "$WORK/t"   # 82.5% on the usable basis, 76.2% of a 105 GB disk
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == false ]] && ok "4c. a reserve-skewed reading warns on the usable basis" || bad "4c. reserve" "$(cat "$CFG_LOG")"

# --- §5 fail closed -----------------------------------------------------------
echo "=== 5. an unreadable sensor never reads healthy ==="
for cmd in "false" "true" "echo garbage" "printf 'Filesystem 1-blocks Used Available Capacity Mounted on\noverlay 0 0 0 0%% /\n'"; do
  rc="$(run_tool env TMPDISK_SOURCE_CMD="$cmd" "$TOOL")"
  [[ "$rc" == 1 && "$(pushes_for "$PAGE")" == false ]] && ok "5. [$cmd] pushes page=false, exit 1" || bad "5. [$cmd]" "rc=$rc $(cat "$CFG_LOG") $(cat "$WORK/err.txt")"
done
[[ -z "$(pushes_for "$WARN")" ]] && ok "5b. an unreadable sensor pages once, not twice" || bad "5b. warn pushed on unreadable" "$(cat "$CFG_LOG")"

# --- §6 refusals ---------------------------------------------------------------
echo "=== 6. refusals ==="
pct_table 40 > "$WORK/t"
for envs in "TMPDISK_WARN_PCT=90 TMPDISK_CRIT_PCT=80" "TMPDISK_WARN_PCT=80 TMPDISK_CRIT_PCT=80" "TMPDISK_WARN_PCT=abc" "TMPDISK_CRIT_PCT=101" "TMPDISK_WARN_PCT=0"; do
  # shellcheck disable=SC2086
  rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" $envs "$TOOL")"
  [[ "$rc" == 2 && "$(push_count)" == 0 ]] && ok "6a. [$envs] exits 2, zero pushes" || bad "6a. [$envs]" "rc=$rc pushes=$(push_count)"
done
rc="$(run_tool env -u GATUS_URL TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$rc" == 2 && "$(push_count)" == 0 ]] && ok "6b. missing GATUS_URL exits 2, zero pushes" || bad "6b. GATUS_URL" "rc=$rc"
rc="$(run_tool env -u GATUS_EXTERNAL_TOKEN TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
[[ "$rc" == 2 && "$(push_count)" == 0 ]] && ok "6c. missing token exits 2, zero pushes" || bad "6c. token" "rc=$rc"
for envs in "TMPDISK_GROWTH_WARN_BYTES=abc" "TMPDISK_GROWTH_WARN_BYTES=0" "TMPDISK_GROWTH_WARN_BYTES=-5" "TMPDISK_GROWTH_MAX_AGE_SEC=0" "TMPDISK_GROWTH_MAX_AGE_SEC=nope"; do
  # shellcheck disable=SC2086
  rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" $envs "$TOOL")"
  [[ "$rc" == 2 && "$(push_count)" == 0 ]] && ok "6a2. [$envs] exits 2, zero pushes" || bad "6a2. [$envs]" "rc=$rc pushes=$(push_count)"
done
rc="$(run_tool env -u GATUS_URL -u GATUS_EXTERNAL_TOKEN TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL" --dry-run)"
[[ "$rc" == 0 ]] && ok "6d. --dry-run needs no credentials" || bad "6d. dry-run creds" "rc=$rc"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL" --frobnicate)"
[[ "$rc" == 2 ]] && ok "6e. unknown flag exits 2" || bad "6e. unknown flag" "rc=$rc"

# --- §7 top consumers ----------------------------------------------------------
echo "=== 7. top consumers ==="
pct_table 85 > "$WORK/t"
printf '9111\t/tmp/.pnpm-store\n2780\t/tmp/d3-upstream\n1\t/tmp/evil\001\002name\n' > "$WORK/top"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" TMPDISK_TOP_CMD="$(src "$WORK/top")" "$TOOL")"
cfg_has "pnpm-store" && ok "7a. a breach names the top consumers" || bad "7a. top on breach" "$(decoded_cfg)"
cfg_has 'evil??name' && ok "7b. control characters become ?" || bad "7b. sanitized" "$(decoded_cfg)"
pct_table 40 > "$WORK/t"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" TMPDISK_TOP_CMD="$(src "$WORK/top")" "$TOOL")"
! cfg_has "pnpm-store" && ok "7c. a healthy reading runs no top command" || bad "7c. top when healthy" "$(decoded_cfg)"
pct_table 95 > "$WORK/t"
python3 -c 'print("9\t/tmp/" + "x" * 900)' > "$WORK/top"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" TMPDISK_TOP_CMD="$(src "$WORK/top")" "$TOOL")"
longest="$(decoded_cfg | grep -o 'x\+' | awk '{ if (length($0) > m) m = length($0) } END { print m + 0 }')"
(( longest > 0 && longest < 400 )) && ok "7d. an oversized line is bounded (longest run of x: $longest)" || bad "7d. bound" "longest=$longest"
pct_table 85 > "$WORK/t"
cat > "$WORK/top_rc1" <<'EOF'
#!/usr/bin/env bash
printf '9111\t/tmp/.pnpm-store\n2780\t/tmp/d3-upstream\n'
echo "du: cannot access '/tmp/tmp.vanished': No such file or directory" >&2
exit 1
EOF
chmod +x "$WORK/top_rc1"
rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" TMPDISK_TOP_CMD="$WORK/top_rc1" "$TOOL")"
cfg_has "pnpm-store" && ok "7e. rows survive a nonzero top-command exit (du vanishing entries)" || bad "7e. top on rc=1" "$(decoded_cfg)"

# --- §8 non-disclosure ----------------------------------------------------------
echo "=== 8. non-disclosure ==="
pct_table 85 > "$WORK/t"; rc="$(run_tool env TMPDISK_SOURCE_CMD="$(src "$WORK/t")" "$TOOL")"
! grep -q "$GATUS_EXTERNAL_TOKEN" "$ARGV_LOG" && grep -q "$GATUS_EXTERNAL_TOKEN" "$CFG_LOG" \
  && ok "8a. token only in the 0600 curl config, never argv" || bad "8a. token placement" "$(cat "$ARGV_LOG")"

# --- §9 emit-yaml ----------------------------------------------------------------
echo "=== 9. emit-gatus-yaml ==="
YAML="$("$TOOL" --emit-gatus-yaml)"
grep -q 'name: tmp-disk-warn' <<<"$YAML" && grep -q 'name: tmp-disk$' <<<"$YAML" && grep -q 'group: controller' <<<"$YAML" \
  && ok "9a. both endpoints, group controller (key controller_tmp-disk[-warn])" || bad "9a. endpoints" "$YAML"
grep -q 'OWNER devops' <<<"$YAML" && grep -q 'docs/runbooks/controller-tmp-disk.md' <<<"$YAML" && ok "9b. OWNER and RUNBOOK named" || bad "9b. owner/runbook" "$YAML"
warn_block="$(sed -n '/name: tmp-disk-warn/,/name: tmp-disk$/p' <<<"$YAML")"
! grep -q 'type: custom' <<<"$warn_block" && grep -q 'type: discord' <<<"$warn_block" && ok "9c. warn alerts Discord only" || bad "9c. warn alert types" "$warn_block"
page_block="$(sed -n '/name: tmp-disk$/,$p' <<<"$YAML")"
grep -q 'type: custom' <<<"$page_block" && grep -q 'type: discord' <<<"$page_block" && ok "9d. page alerts Discord and Akmatori" || bad "9d. page alerts" "$page_block"
grep -q 'failure-threshold: 3' <<<"$warn_block" && grep -q 'fast growth between samples' <<<"$warn_block" \
  && ok "9e. warn alert pins failure-threshold 3 and names the growth trigger" || bad "9e. warn threshold/description" "$warn_block"

# --- §10 mutants -----------------------------------------------------------------
echo "=== 10. mutants ==="
MUTROOT="$WORK/mutant"
MUT="$MUTROOT/ops/case_0ddff3ec/tmp-disk-alarm.sh"
mk_mutant() {  # <sed-expr>: exactly one changed line that still parses
  rm -rf "$MUTROOT"; mkdir -p "$MUTROOT/ops/case_0ddff3ec" "$MUTROOT/ops/case_658bf6b9"
  cp "$HERE/ops/case_658bf6b9/_gatus_push.sh" "$MUTROOT/ops/case_658bf6b9/_gatus_push.sh"
  sed "$1" "$TOOL" > "$MUT" || return 1
  chmod +x "$MUT"
  bash -n "$MUT" 2>/dev/null || return 1
  [[ "$(diff "$TOOL" "$MUT" | grep -c '^>')" == 1 ]]
}
mutant_flip() {  # <label> <sed-expr> <fixture-file|CMD:...> <warn|page> <value the mutant must push>
  if ! mk_mutant "$2"; then bad "M. $1" "mutant is not a one-line parsable change (anchor drift)"; return; fi
  local srccmd rc
  if [[ "$3" == CMD:* ]]; then srccmd="${3#CMD:}"; else srccmd="$(src "$3")"; fi
  rc="$(run_tool env TMPDISK_SOURCE_CMD="$srccmd" "$MUT")"
  case "$4" in
    warn) [[ "$(pushes_for "$WARN")" == "$5" ]] && ok "M. $1 mutant flips the warn push" || bad "M. $1 mutant" "warn=$(pushes_for "$WARN")" ;;
    page) [[ "$(pushes_for "$PAGE")" == "$5" ]] && ok "M. $1 mutant flips the page push" || bad "M. $1 mutant" "page=$(pushes_for "$PAGE") rc=$rc" ;;
  esac
}
pct_table 80 > "$WORK/t80"; pct_table 90 > "$WORK/t90"
mutant_flip "strict > at the warn boundary" 's/float(sys.argv\[1\]) >= float(sys.argv\[2\])/float(sys.argv[1]) > float(sys.argv[2])/' "$WORK/t80" warn true
mutant_flip "strict > at the page boundary" 's/float(sys.argv\[1\]) >= float(sys.argv\[2\])/float(sys.argv[1]) > float(sys.argv[2])/' "$WORK/t90" page true
mutant_flip "unreadable sensor reads healthy" 's/gatus_push "\$CRIT_KEY" false "\$msg" || true/gatus_push "$CRIT_KEY" true || true/' "CMD:false" page true
pct_table 82 > "$WORK/t82"   # warns on the usable basis; a 5% larger divisor (a reserve-blind total) would not
mutant_flip "reserve-blind divisor" 's|100.0 \* used / (used + avail)|100.0 * used / (used + avail + (used + avail) // 20)|' "$WORK/t82" warn true

# --- §11 units -------------------------------------------------------------------
echo "=== 11. systemd units ==="
U="$HERE/ops/case_0ddff3ec/systemd"
[[ -f "$HERE/ops/case_5866fa6f/disk-retention-sweep.sh" ]] && grep -q 'StandardInput=file:%h/paperclip-ops/ops/case_5866fa6f/disk-retention-sweep.sh' "$U/paperclip-tmp-sweep.service" \
  && ok "11a. sweep service streams the repo's sweep script" || bad "11a. sweep script path" "$(cat "$U/paperclip-tmp-sweep.service")"
grep -q 'bash -s -- --tmp-only --apply' "$U/paperclip-tmp-sweep.service" && ok "11b. sweep service runs --tmp-only --apply" || bad "11b. sweep args" "$(cat "$U/paperclip-tmp-sweep.service")"
grep -q 'ExecStart=%h/paperclip-ops/ops/case_0ddff3ec/tmp-disk-alarm.sh$' "$U/paperclip-tmp-disk-alarm.service" && [[ -x "$TOOL" ]] \
  && ok "11c. alarm service runs the executable alarm script" || bad "11c. alarm path" "$(cat "$U/paperclip-tmp-disk-alarm.service")"
for t in sweep disk-alarm; do
  grep -q '^OnCalendar=' "$U/paperclip-tmp-$t.timer" && grep -q '^Persistent=true' "$U/paperclip-tmp-$t.timer" \
    && ok "11d. $t timer has OnCalendar and Persistent" || bad "11d. $t timer" "$(cat "$U/paperclip-tmp-$t.timer")"
done
env_quoted="$(grep -E "^[A-Z_]+=.*[\"']" "$HERE/ops/case_0ddff3ec/case_705a486a.env.example" | grep -v '^#')"
! grep -q . <<<"$env_quoted" \
  && ok "11e. env example has no quote characters in a value" || bad "11e. quotes in env example" "$(grep -E '["'"'"']' "$HERE/ops/case_0ddff3ec/case_705a486a.env.example")"
env_secret="$(grep -E 'GATUS_EXTERNAL_TOKEN=[A-Za-z0-9]{16,}' "$HERE/ops/case_0ddff3ec/case_705a486a.env.example")"
! grep -q . <<<"$env_secret" \
  && ok "11f. env example carries no secret-shaped value" || bad "11f. secret in env example" ""

# --- §12 growth between consecutive samples (case_76fe8329) -----------------------
# Fixture disk: 100 GB usable. T0 = 1000000. Default growth threshold is
# 5368709120 bytes (5 GiB).
echo "=== 12. growth between samples ==="
T0=1000000
# 12a. spike series: 40% then +6 GB still under the 80% level -> warns on growth.
df_table 40000000000 60000000000 > "$WORK/g0"
df_table 46000000000 54000000000 > "$WORK/g1"
rc="$(run_seeded "$T0" 40000000000 env TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/g1")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == false ]] && ok "12a. spike series (+6 GB in 300 s at 46%) trips warn" || bad "12a. spike warns" "$(cat "$CFG_LOG")"
[[ "$(pushes_for "$PAGE")" == true ]] && ok "12a2. spike series does not page on growth alone" || bad "12a2. spike pages" "$(cat "$CFG_LOG")"
cfg_has "growth" && ok "12a3. warn reason names the growth" || bad "12a3. growth reason" "$(decoded_cfg)"
# 12b. exact-threshold boundary: +5368709120 trips, one byte less stays quiet.
df_table 45368709120 54631290880 > "$WORK/gthr"
rc="$(run_seeded "$T0" 40000000000 env TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/gthr")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == false ]] && ok "12b. exactly +5 GiB trips warn" || bad "12b. threshold trips" "$(cat "$CFG_LOG")"
df_table 45368709119 54631290881 > "$WORK/gunder"
rc="$(run_seeded "$T0" 40000000000 env TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/gunder")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == true ]] && ok "12b2. one byte under +5 GiB stays quiet (positive control)" || bad "12b2. under threshold" "$(cat "$CFG_LOG")"
# 12c. slow-ramp series: +0.5 GB per sample follows the level rules (quiet at 78.5%).
df_table 78000000000 22000000000 > "$WORK/r0"
df_table 78500000000 21500000000 > "$WORK/r1"
rc="$(run_seeded "$T0" 78000000000 env TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/r1")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == true && "$(pushes_for "$PAGE")" == true ]] && ok "12c. slow ramp (+0.5 GB, 78.5%) stays quiet" || bad "12c. slow ramp" "$(cat "$CFG_LOG")"
# 12d. slow ramp crossing the level still warns by level (growth is not the only path).
df_table 79500000000 20500000000 > "$WORK/r2"
df_table 80000000000 20000000000 > "$WORK/r3"
rc="$(run_seeded "$T0" 79500000000 env TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/r3")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == false && "$(pushes_for "$PAGE")" == true ]] && ok "12d. ramp to 80% warns by level" || bad "12d. ramp to 80" "$(cat "$CFG_LOG")"
# 12e. flat series: identical readings stay quiet.
rc="$(run_seeded "$T0" 40000000000 env TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/g0")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == true && "$(pushes_for "$PAGE")" == true ]] && ok "12e. flat series stays quiet" || bad "12e. flat" "$(cat "$CFG_LOG")"
# 12f. history hygiene: missing history (first run) skips growth; the good
# reading is persisted for the next run.
rc="$(run_tool env TMPDISK_NOW="$T0" TMPDISK_SOURCE_CMD="$(src "$WORK/g0")" "$TOOL")"
[[ "$(pushes_for "$WARN")" == true ]] && ok "12f. first run (no history) stays quiet" || bad "12f. first run" "$(cat "$CFG_LOG")"
[[ "$(cat "$TMPDISK_STATE_FILE")" == "$T0 40000000000 0" ]] && ok "12f2. first run persists its reading" || bad "12f2. state persist" "$(cat "$TMPDISK_STATE_FILE" 2>/dev/null)"
# 12g. corrupt, future-dated and stale history each skip the growth check (and
# the run repairs the file with its own good reading).
printf 'garbage\n' > "$TMPDISK_STATE_FILE"
reset_logs
TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/g1")" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
[[ "$(pushes_for "$WARN")" == true ]] && ok "12g. corrupt history skips growth" || bad "12g. corrupt history" "$(cat "$CFG_LOG")"
printf '%s %s\n' "$((T0 + 360))" 40000000000 > "$TMPDISK_STATE_FILE"
reset_logs
TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/g1")" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
[[ "$(pushes_for "$WARN")" == true ]] && ok "12g2. future-dated history skips growth" || bad "12g2. future history" "$(cat "$CFG_LOG")"
printf '%s %s\n' "$T0" 40000000000 > "$TMPDISK_STATE_FILE"
reset_logs
TMPDISK_NOW="$((T0 + 1801))" TMPDISK_SOURCE_CMD="$(src "$WORK/g1")" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
[[ "$(pushes_for "$WARN")" == true ]] && ok "12g3. stale history (age 1801 s > 1800 s) skips growth" || bad "12g3. stale history" "$(cat "$CFG_LOG")"
reset_logs
printf '%s %s\n' "$T0" 40000000000 > "$TMPDISK_STATE_FILE"
TMPDISK_NOW="$((T0 + 1800))" TMPDISK_SOURCE_CMD="$(src "$WORK/g1")" "$TOOL" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
[[ "$(pushes_for "$WARN")" == false ]] && ok "12g4. history at exactly the max age still counts (positive control)" || bad "12g4. max-age boundary" "$(cat "$CFG_LOG")"
# 12h. --dry-run with a tripping jump pushes nothing and leaves history alone
# (run_tool would delete the seed first, so drive the tool directly here).
seed_state "$T0" 40000000000
reset_logs
TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/g1")" "$TOOL" --dry-run >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
[[ "$(push_count)" == 0 ]] && ok "12h. dry-run pushes nothing on a tripping jump" || bad "12h. dry-run pushes" "$(cat "$CFG_LOG")"
[[ "$(cat "$TMPDISK_STATE_FILE")" == "$T0 40000000000" ]] && ok "12h2. dry-run leaves existing history alone" || bad "12h2. dry-run state" "$(cat "$TMPDISK_STATE_FILE" 2>/dev/null)"
rm -f "$TMPDISK_STATE_FILE"
reset_logs
TMPDISK_NOW="$T0" TMPDISK_SOURCE_CMD="$(src "$WORK/g0")" "$TOOL" --dry-run >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
[[ ! -f "$TMPDISK_STATE_FILE" ]] && ok "12h3. dry-run creates no history" || bad "12h3. dry-run creates state" "$(cat "$TMPDISK_STATE_FILE")"
# 12i. a non-numeric TMPDISK_NOW refuses loudly instead of silently skipping growth.
rc="$(run_tool env TMPDISK_NOW="not-an-epoch" TMPDISK_SOURCE_CMD="$(src "$WORK/g0")" "$TOOL")"
[[ "$rc" == 2 && "$(push_count)" == 0 ]] && ok "12i. bad TMPDISK_NOW exits 2, zero pushes" || bad "12i. TMPDISK_NOW" "rc=$rc pushes=$(push_count)"
# 12j. MUTANT: with the growth check removed, the spike fixture reads healthy.
if mk_mutant 's/growth_warn=1/growth_warn=0/'; then
  rc="$(run_seeded "$T0" 40000000000 env TMPDISK_NOW="$((T0 + 300))" TMPDISK_SOURCE_CMD="$(src "$WORK/g1")" "$MUT")"
  [[ "$(pushes_for "$WARN")" == true ]] && ok "M. growth check removed flips the spike fixture to healthy" || bad "M. growth removed" "warn=$(pushes_for "$WARN")"
else
  bad "M. growth check removed" "mutant is not a one-line parsable change (anchor drift)"
fi

# 12k-12m. MULTI-RUN CHAINS: each step is a real run that reads the state the
# previous run wrote. chain_pushes prints "<warn pushes>|<page pushes>", one word
# per run. A jump must hold warn=false on exactly three consecutive runs, the
# Gatus failure-threshold; a single false push is never enough to alert.
chain_pushes() {  # <tool> <epoch:fixture>...
  local tool="$1" step warn="" page=""; shift
  rm -f "$TMPDISK_STATE_FILE"
  for step in "$@"; do
    reset_logs
    TMPDISK_NOW="${step%%:*}" TMPDISK_SOURCE_CMD="$(src "${step#*:}")" "$tool" >"$WORK/out.txt" 2>"$WORK/err.txt" || true
    warn+="$(pushes_for "$WARN") "; page+="$(pushes_for "$PAGE") "
  done
  printf '%s|%s' "$warn" "$page"
}
# 12k. the 2026-10-04 shape (+2.3 GB ramp, then +6.3 GB, then flat): the ramp stays quiet,
# the jump holds warn false for three runs, then the endpoint recovers.
df_table 40000000000 60000000000 > "$WORK/c0"
df_table 42300000000 57700000000 > "$WORK/c1"
df_table 48600000000 51400000000 > "$WORK/c2"
df_table 54900000000 45100000000 > "$WORK/c3"
out="$(chain_pushes "$TOOL" "$T0:$WORK/c0" "$((T0+300)):$WORK/c1" "$((T0+600)):$WORK/c2" "$((T0+900)):$WORK/c2" "$((T0+1200)):$WORK/c2" "$((T0+1500)):$WORK/c2")"
[[ "${out%%|*}" == "true true false false false true " ]] && ok "12k. a 6.3 GB jump holds warn false for three consecutive runs, then recovers" || bad "12k. hold length" "$out"
[[ "${out#*|}" != *false* ]] && ok "12k2. growth never pages across the hold" || bad "12k2. page on growth" "$out"
# 12l. a second jump inside the hold restarts it: five consecutive false pushes, then healthy.
out="$(chain_pushes "$TOOL" "$T0:$WORK/c0" "$((T0+300)):$WORK/c2" "$((T0+600)):$WORK/c2" "$((T0+900)):$WORK/c3" "$((T0+1200)):$WORK/c3" "$((T0+1500)):$WORK/c3" "$((T0+1800)):$WORK/c3")"
[[ "${out%%|*}" == "true false false false false false true " ]] && ok "12l. a second jump during the hold restarts it" || bad "12l. restart" "$out"
# 12m. MUTANTS: a one-push jump, a hold that never continues, and a threshold that
# drifts from the emitted failure-threshold must each change the 12k/12l series.
if mk_mutant 's/growth_hold=\$GROWTH_HOLD_PUSHES/growth_hold=1/'; then
  out="$(chain_pushes "$MUT" "$T0:$WORK/c0" "$((T0+300)):$WORK/c1" "$((T0+600)):$WORK/c2" "$((T0+900)):$WORK/c2" "$((T0+1200)):$WORK/c2" "$((T0+1500)):$WORK/c2")"
  [[ "${out%%|*}" != "true true false false false true " ]] && ok "M. a one-push jump (no hold) is caught by 12k" || bad "M. one-push survives" "$out"
else
  bad "M. one-push" "mutant is not a one-line parsable change (anchor drift)"
fi
if mk_mutant 's/elif (( PREV_HOLD > 0 )); then/elif false; then/'; then
  out="$(chain_pushes "$MUT" "$T0:$WORK/c0" "$((T0+300)):$WORK/c2" "$((T0+600)):$WORK/c2" "$((T0+900)):$WORK/c2" "$((T0+1200)):$WORK/c2")"
  [[ "${out%%|*}" != "true false false false true " ]] && ok "M. a hold that never continues is caught by 12k" || bad "M. no-continue survives" "$out"
else
  bad "M. no-continue" "mutant is not a one-line parsable change (anchor drift)"
fi
if mk_mutant 's/^ALERT_FAILURE_THRESHOLD=3$/ALERT_FAILURE_THRESHOLD=2/'; then
  out="$(chain_pushes "$MUT" "$T0:$WORK/c0" "$((T0+300)):$WORK/c2" "$((T0+600)):$WORK/c2" "$((T0+900)):$WORK/c2" "$((T0+1200)):$WORK/c2" "$((T0+1500)):$WORK/c2")"
  [[ "${out%%|*}" != "true true false false false true " ]] && ok "M. a threshold of 2 is caught by 12k" || bad "M. threshold-2 survives" "$out"
else
  bad "M. threshold" "mutant is not a one-line parsable change (anchor drift)"
fi

echo "---"
printf 'pass=%d fail=%d\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
