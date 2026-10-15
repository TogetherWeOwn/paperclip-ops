#!/usr/bin/env bash
# test_case_f1c0153c_db_backup_freshness.sh — offline suite for the case_66436e78
# backup-freshness Gatus module (coverage-matrix gap #1: case_c05e4728 names
# backup freshness the one invariant with no owner — case_654dd2ec owns six
# SQL views; backups are files on disk, probed by db_backup_stall.sh).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. FRESH pushes true — a usable archive 30min old exits 0 with
#      success=true. Without this, a suite that only asserts failures
#      could pass against a script that fails everything.
#   2. STALE pushes false naming the age — a 3h-old archive exits 1 with
#      success=false. The positive control is §1: the same shape fresh
#      exits 0.
#   3. TRUNCATED fails closed — a 4-byte .gz (empty-frame residue, the
#      2026-09-05 signature) pushes false, never healthy.
#   4. EMPTY fails closed — no usable archive pushes false naming it.
#   5. UNMEASURABLE fails closed — a missing backup dir (detector exit 2,
#      inconclusive) pushes false, never healthy.
#   6. REFUSALS — missing GATUS_URL, missing token, missing detector,
#      non-numeric MAX_BACKUP_AGE_HOURS each exit 2 with zero pushes.
#   7. NON-DISCLOSURE — the Bearer token never reaches curl argv; it
#      travels in the 0600 curl config only.
#   8. ENDPOINT config — one external-endpoint carrying name, group,
#      shared-token indirection, 10m heartbeat, discord+custom alerts,
#      threshold + owner + runbook pointer, and no URL literal.
#   9. NOT VACUOUS — a mutant with the fail-closed branch neutered scores
#      the stale fixture healthy, proving §2 asserts something.
#
# Hermetic: no network, no credentials, no database. The REAL reviewed
# detector (db_backup_stall.sh) runs against mktemp backup dirs; a stub
# curl records argv + config and answers 200.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/ops/case_66436e78/push-db-backup-freshness.sh"
YAML="$HERE/ops/case_66436e78/gatus-endpoints.yaml"
DETECTOR="$HERE/db_backup_stall.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$TOOL" ]] || { echo "REFUSE: $TOOL missing or not executable" >&2; exit 3; }
[[ -f "$DETECTOR" ]] || { echo "REFUSE: $DETECTOR missing" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_backup_fresh.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
export ARGV_LOG="$WORK/argv.log" CFG_LOG="$WORK/cfg.log"
: > "$ARGV_LOG"; : > "$CFG_LOG"

# Stub curl: records argv and every --config file's contents, answers 200.
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

run_tool() { # env-assignments... ; echoes exit code
  : > "$ARGV_LOG"; : > "$CFG_LOG"
  local rc
  set +e
  "$@" >"$WORK/out.txt" 2>"$WORK/err.txt"; rc=$?
  set -e
  printf '%s' "$rc"
}
push_success() { # prints success= value pushed for the endpoint
  grep -o "endpoints/paperclip_db-backup-freshness/external?success=[a-z]*" "$CFG_LOG" | sed 's/.*success=//' | head -1
}
push_count() { grep -c "endpoints/" "$CFG_LOG" || true; }
cfg_has() { # <pattern> greps the URL-decoded curl configs (reasons are encoded)
  # Herestring (no producer to strand under pipefail): SIGPIPE gate case_c25c4d3f.
  grep -q "$1" <<<"$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(open("'"$CFG_LOG"'").read()))')"
}

export GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-token-not-a-secret"

mkbackup() { # <dir> <age> — a 3 KiB usable archive of the given age
  local d="$1" age="$2"
  mkdir -p "$d"
  dd if=/dev/urandom of="$d/paperclip-20261004-060000.sql.gz" bs=1024 count=3 2>/dev/null
  touch -d "$age" "$d/paperclip-20261004-060000.sql.gz"
}

# --- §1 fresh ---------------------------------------------------------------
mkbackup "$WORK/fresh" '30 minutes ago'
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/fresh" "$TOOL")"
[[ "$rc" == "0" ]] && ok "1. fresh archive exits 0" || bad "1. fresh exit" "got $rc: $(cat "$WORK/err.txt")"
[[ "$(push_success)" == "true" ]] && ok "1b. fresh pushes success=true" || bad "1b. fresh push" "$(cat "$CFG_LOG")"

# --- §2 stale ----------------------------------------------------------------
mkbackup "$WORK/stale" '3 hours ago'
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/stale" "$TOOL")"
[[ "$rc" == "1" ]] && ok "2. stale archive exits 1" || bad "2. stale exit" "got $rc"
[[ "$(push_success)" == "false" ]] && ok "2b. stale pushes success=false" || bad "2b. stale push" "$(cat "$CFG_LOG")"
cfg_has "180" && ok "2c. reason names the age" || bad "2c. age named" "$(cat "$CFG_LOG")"

# --- §3 truncated ------------------------------------------------------------
mkdir -p "$WORK/trunc"
printf 'tiny' > "$WORK/trunc/paperclip-20261004-060000.sql.gz"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/trunc" "$TOOL")"
[[ "$rc" == "1" ]] && ok "3. truncated archive exits 1" || bad "3. truncated exit" "got $rc"
[[ "$(push_success)" == "false" ]] && ok "3b. truncated pushes false, never healthy" || bad "3b. truncated push" "$(cat "$CFG_LOG")"

# --- §4 empty -----------------------------------------------------------------
mkdir -p "$WORK/empty"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/empty" "$TOOL")"
[[ "$rc" == "1" ]] && ok "4. empty dir exits 1" || bad "4. empty exit" "got $rc"
[[ "$(push_success)" == "false" ]] && ok "4b. empty pushes false" || bad "4b. empty push" "$(cat "$CFG_LOG")"

# --- §5 unmeasurable ------------------------------------------------------------
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/nonexistent" "$TOOL")"
[[ "$rc" == "1" ]] && ok "5. missing dir exits 1 (inconclusive pages)" || bad "5. missing-dir exit" "got $rc"
[[ "$(push_success)" == "false" ]] && ok "5b. inconclusive pushes false, never healthy" || bad "5b. inconclusive push" "$(cat "$CFG_LOG")"
cfg_has "inconclusive" && ok "5c. reason says inconclusive" || bad "5c. inconclusive named" "$(cat "$CFG_LOG")"

# --- §6 refusals, zero pushes ---------------------------------------------------
reset_rc=0
: > "$ARGV_LOG"; : > "$CFG_LOG" # clear BEFORE the refusal: this arm bypasses run_tool, which clears for the others
set +e; (unset GATUS_URL; GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/fresh" "$TOOL" >/dev/null 2>&1); reset_rc=$?; set -e
[[ "$reset_rc" == "2" ]] && ok "6. missing GATUS_URL refuses (exit 2)" || bad "6. GATUS_URL refusal" "got $reset_rc"
[[ "$(push_count)" == "0" ]] && ok "6b. refusal makes zero pushes" || bad "6b. zero pushes" "$(cat "$CFG_LOG")"
rc="$(run_tool env -u GATUS_EXTERNAL_TOKEN GATUS_URL="$GATUS_URL" PAPERCLIP_DB_BACKUP_DIR="$WORK/fresh" "$TOOL")"
[[ "$rc" == "2" ]] && ok "6c. missing token refuses" || bad "6c. token refusal" "got $rc"
[[ "$(push_count)" == "0" ]] && ok "6c2. token refusal makes zero pushes" || bad "6c2. zero pushes" "$(cat "$CFG_LOG")"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/fresh" DB_BACKUP_STALL_BIN="$WORK/nonexistent.sh" "$TOOL")"
[[ "$rc" == "2" ]] && ok "6d. missing detector refuses" || bad "6d. detector refusal" "got $rc"
[[ "$(push_count)" == "0" ]] && ok "6d2. detector refusal makes zero pushes" || bad "6d2. zero pushes" "$(cat "$CFG_LOG")"
rc="$(run_tool env GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/fresh" MAX_BACKUP_AGE_HOURS="soon" "$TOOL")"
[[ "$rc" == "2" ]] && ok "6e. non-numeric threshold refuses" || bad "6e. threshold refusal" "got $rc"
[[ "$(push_count)" == "0" ]] && ok "6e2. threshold refusal makes zero pushes" || bad "6e2. zero pushes" "$(cat "$CFG_LOG")"

# --- §7 non-disclosure ----------------------------------------------------------
: > "$ARGV_LOG"; : > "$CFG_LOG"
set +e; GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/fresh" "$TOOL" >/dev/null 2>&1; set -e
if grep -q "Bearer" "$ARGV_LOG"; then bad "7. token in argv" "$(cat "$ARGV_LOG")"; else ok "7. no Bearer in curl argv"; fi
grep -q "Authorization: Bearer" "$CFG_LOG" && ok "7b. token travels in curl config" || bad "7b. config transport" "$(cat "$CFG_LOG")"

# --- §8 endpoint config -----------------------------------------------------------
[[ "$(grep -c '^  - name: ' "$YAML")" == "1" ]] \
  && ok "8. exactly one endpoint" || bad "8. endpoint count" "$(grep -c '^  - name: ' "$YAML")"
grep -q "name: db-backup-freshness" "$YAML" && ok "8b. endpoint name" || bad "8b. name" ""
grep -q "group: paperclip" "$YAML" && ok "8c. endpoint group" || bad "8c. group" ""
grep -q 'token: "${GATUS_EXTERNAL_TOKEN}"' "$YAML" && ok "8d. shared-token indirection" || bad "8d. token" ""
grep -q "interval: 10m" "$YAML" && ok "8e. 10m heartbeat" || bad "8e. heartbeat" ""
{ grep -q "type: discord" "$YAML" && grep -q "type: custom" "$YAML"; } \
  && ok "8f. discord+akmatori alerts" || bad "8f. alerts" ""
{ grep -q "THRESHOLD" "$YAML" && grep -q "OWNER director" "$YAML" && grep -q "RUNBOOK" "$YAML"; } \
  && ok "8g. threshold+owner+runbook in description" || bad "8g. description" ""
grep -q "docs/runbooks/database-backup-stall.md" "$YAML" \
  && ok "8h. runbook pointer" || bad "8h. runbook" ""
if grep -En '://' "$YAML"; then bad "8i. yaml carries a URL literal" "$(grep -En '://' "$YAML")"; else ok "8i. no URL literals"; fi
# Finding 1 (Paperclip Review 4/5 on afdee30): the install line pushed every
# 10m against a 10m heartbeat with no env file, so one late run paged and an
# as-written install never pushed at all. Pin the */5 + env-file install.
DOC="$HERE/ops/case_66436e78/README.md"
{ grep -q '\*/5' "$DOC" && grep -q 'case_60330d6f\.env' "$DOC"; } \
  && ok "8j. README install pushes every 5m with env file" || bad "8j. README install" "$(grep -n 'Cron' "$DOC")"
# Paperclip Review 4/5 on 79e18d5: case_60330d6f.env has no export lines, so the
# install must export via set -a or nothing is ever pushed. Pin it in the
# README and both script-header copies.
{ grep -q 'set -a; \. /root/.config/paperclip/case_60330d6f\.env' "$DOC" && grep -q 'set -a; \. /root/.config/paperclip/case_60330d6f\.env' "$TOOL"; } \
  && ok "8k. install exports env via set -a" || bad "8k. set -a export" "$(grep -n 'case_60330d6f\.env' "$DOC" "$TOOL")"

# --- §9 mutation gate --------------------------------------------------------------
cp "$TOOL" "$WORK/mutant.sh"
# The copy resolves its helper and detector relatively, so repoint both at
# the real files before running it.
python3 - "$WORK/mutant.sh" "$HERE/ops/case_658bf6b9/_gatus_push.sh" <<'PY'
import sys
p, helper = sys.argv[1], sys.argv[2]
s = open(p).read()
old = "if (( rc == 0 )); then"
assert old in s, "anchor for the fail-closed branch moved — update this gate"
s = s.replace(old, "if true; then # MUTANT: fail-closed branch neutered")
s = s.replace('GATUS_HELPER="$HERE/../case_658bf6b9/_gatus_push.sh"',
              f'GATUS_HELPER="{helper}"')
open(p, "w").write(s)
PY
chmod +x "$WORK/mutant.sh"
set +e
GATUS_URL="$GATUS_URL" GATUS_EXTERNAL_TOKEN="$GATUS_EXTERNAL_TOKEN" PAPERCLIP_DB_BACKUP_DIR="$WORK/stale" DB_BACKUP_STALL_BIN="$DETECTOR" "$WORK/mutant.sh" >/dev/null 2>&1; mrc=$?
set -e
if [[ "$mrc" == "0" ]]; then ok "9. mutant scores stale healthy (gate is load-bearing)"; else bad "9. mutation gate" "neutered script still exits $mrc — §2 asserts nothing"; fi

printf '\ndb-backup-freshness: %d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
