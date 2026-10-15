#!/usr/bin/env bash
# ===========================================================================
# test_case_c7afa8e6_runner_disk.sh — offline suite for the case_71e7a4e9
# platform_runner-disk Gatus module (ops/case_a814f742/, thresholds from the
# case_a814f742 document: warn 80 / crit 90).
#
# What it pins (each with a positive control that proves it can still fail):
#   1. HEALTHY pushes true — 60% host, 10% inodes, 33% pool, 40% VMs exits
#      0 with platform_runner-disk success=true. Without this, a suite that
#      only asserts failures could pass against a script that fails
#      everything. A 100% snap squashfs row rides along to prove non-root
#      mounts are ignored (the case_231b8224 signal is the host root).
#   2. WARN fails closed — host / at 85% exits 1, endpoint false naming
#      WARNING plus OWNER devops plus RUNBOOK docs/runbooks/runner-disk-triage.md.
#   3. CRIT fails closed — host / at 92% exits 1 naming CRITICAL.
#   4. INODE-CRIT fails closed — IUse 91% with bytes at 40% exits 1 naming
#      CRITICAL + inodes (bytes-free does not excuse exhausted inodes).
#   5. POOL-NEAR-FULL fails closed — pool used/total at 91% exits 1 naming
#      CRITICAL + pool (the case_e5735d46 tripwire).
#   6. VM-UNREACHABLE fails closed — one `lxc exec` failing exits 1 naming
#      UNKNOWN + the VM name (a blind spot never reads green).
#   7. MALFORMED fails closed — triage printing non-JSON exits 1 naming UNKNOWN.
#   8. BOUNDARIES — exactly 80 reads warning (DOWN, ties trip), 79 healthy;
#      exactly 90 reads critical, 89 warning.
#   9. MUTANTS — one weakened mutant per threshold boundary (warn: 80 reads
#      healthy; crit: 90 reads warning) that the suite catches: each mutant
#      must mis-score its boundary fixture, proving §§8 asserts something.
#      A mutant that still scores correctly fails this section (dead control).
#  10. DEFINITIONS — one endpoint platform_runner-disk, 10m heartbeat, warn
#      80 / crit 90, OWNER devops, RUNBOOK docs/runbooks/runner-disk-triage.md
#      in both alert descriptions, token ${GATUS_EXTERNAL_TOKEN}; MODULES.md
#      names the key; the push header carries the */5 cron line (twice the
#      heartbeat); the push exports LXC_BIN to the triage and skips the du
#      size scan.
#  11. NON-DISCLOSURE — no bearer literal or secret-shaped string in the
#      module (with a planted-secret positive control proving the grep live).
#  12. REFUSALS — missing Gatus token exits 2 with zero pushes; evaluator
#      with no --input exits 2.
#  13. TRIAGE FIELD — the triage `lxd_usage` read-only field carries the
#      fixture through `--format json` and the text report.
#  14. POOL FALLBACK + NO-LXD SKIP — `size/used` pairs in the `lxd` text
#      score when `lxd_usage` is absent; double-UNAVAILABLE skips the pool
#      check instead of paging a host with no LXD.
#  15. POOL FAILS CLOSED (reviewer CHANGES on 897149be) — lxc present but
#      `lxc storage info` failing is a blind spot, never a skip: a failed
#      single pool, a failed `storage list`, and p1 failed beside a healthy
#      p2 (glued and newline-separated) all read UNKNOWN naming the pool; a
#      zero-size pool and an unanchored sentinel in prose read UNKNOWN; two
#      healthy pools read healthy (positive control). One mutant restores the
#      substring skip, one drops the per-pool problem; each must mis-score its
#      fixture healthy. An end-to-end leg runs the real triage under a stub
#      lxc whose first pool fails and asserts the evaluator pages UNKNOWN.
#  16. LXC_BIN-ONLY LXD (Paperclip Review error on fd2aae77) — lxc reachable
#      only through LXC_BIN (snap outside cron PATH) still scores the pool:
#      the real push + real triage against a 97% pool pushes DOWN naming
#      CRITICAL + pool. A pre-fix triage would print the no-LXD sentinel and
#      read healthy, so this section fails against it.
#  17. SIZE SCAN SKIPPED (Paperclip Review warning on fd2aae77) — with
#      TRIAGE_SKIP_SIZE_SCAN the real triage reports no suspects and SKIPPED
#      container roots; without it the same tree lists the cache (the flag
#      is load-bearing, and the push exports it).
#
# Hermetic: stub curl records pushes (answers 200, no network), stub lxc
# answers per-VM `df` from fixture files, stub triage cats a fixture file.
# No credentials, no host LXD, no Gatus.
#
# Exit 0 all pass; 1 any failure.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOD="$HERE/ops/case_a814f742"
EVAL="$MOD/evaluate-runner-disk.sh"
PUSH="$MOD/push-runner-disk.sh"
YAML="$MOD/gatus-endpoints.yaml"
TRIAGE="$HERE/runner_disk_triage.sh"
REGISTRY="$HERE/ops/case_658bf6b9/MODULES.md"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

[[ -x "$EVAL" ]] || { echo "REFUSE: $EVAL missing or not executable" >&2; exit 3; }
[[ -x "$PUSH" ]] || { echo "REFUSE: $PUSH missing or not executable" >&2; exit 3; }
[[ -f "$YAML" ]] || { echo "REFUSE: $YAML missing" >&2; exit 3; }
[[ -f "$REGISTRY" ]] || { echo "REFUSE: $REGISTRY missing" >&2; exit 3; }
command -v python3 >/dev/null || { echo "REFUSE: python3 required" >&2; exit 3; }
bash -n "$EVAL" || { echo "REFUSE: $EVAL does not parse" >&2; exit 3; }
bash -n "$PUSH" || { echo "REFUSE: $PUSH does not parse" >&2; exit 3; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test_runner_disk.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/vmdf"
export PUSH_LOG="$WORK/push.log" ARGV_LOG="$WORK/argv.log"
: > "$PUSH_LOG"; : > "$ARGV_LOG"

# ---- stubs ------------------------------------------------------------------
cat > "$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ARGV_LOG"
cfg=""
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--config" ]]; then j=$((i+1)); cfg="${!j}"; fi
done
[[ -n "$cfg" ]] || { echo "stub curl: no --config" >&2; exit 99; }
cat "$cfg" >> "$PUSH_LOG"; printf '\n---\n' >> "$PUSH_LOG"
printf '200'
exit 0
STUB
chmod +x "$WORK/bin/curl"

# stub lxc: `exec <vm> -- df -P /` cats $LXC_VM_DF_DIR/<vm>.df, or exits 1
# when $LXC_VM_DF_DIR/<vm>.err exists (unreachable VM).
cat > "$WORK/bin/lxc" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "exec" ]]; then
  vm="${2:-}"
  if [[ -f "$LXC_VM_DF_DIR/$vm.err" ]]; then exit 1; fi
  if [[ -f "$LXC_VM_DF_DIR/$vm.df" ]]; then cat "$LXC_VM_DF_DIR/$vm.df"; exit 0; fi
  exit 1
fi
# `storage list --format csv -c n` lists $LXC_POOLS_FILE; `storage info <p>`
# cats $LXC_POOL_DIR/<p>.info, or exits 1 when <p>.err exists (failed pool).
if [[ "${1:-}" == "storage" && "${2:-}" == "list" ]]; then
  cat "${LXC_POOLS_FILE:-/dev/null}"; exit 0
fi
if [[ "${1:-}" == "storage" && "${2:-}" == "info" ]]; then
  pool="${3:-}"
  if [[ -f "${LXC_POOL_DIR:-/nonexistent}/$pool.err" ]]; then exit 1; fi
  if [[ -f "${LXC_POOL_DIR:-/nonexistent}/$pool.info" ]]; then cat "$LXC_POOL_DIR/$pool.info"; exit 0; fi
  exit 1
fi
echo "stub lxc: unexpected argv: $*" >&2; exit 99
STUB
chmod +x "$WORK/bin/lxc"

# stub triage: cats $TRIAGE_FIXTURE, or exits 1 with no output when
# $TRIAGE_FAIL is set (dead triage).
cat > "$WORK/bin/stub-triage.sh" <<'STUB'
#!/usr/bin/env bash
if [[ -n "${TRIAGE_FAIL:-}" ]]; then exit 1; fi
cat "${TRIAGE_FIXTURE:-/dev/null}"
STUB
chmod +x "$WORK/bin/stub-triage.sh"
export PATH="$WORK/bin:$PATH"

BASE_ENV=(GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-gatus-token-14358"
  LXC_VMS="ci-node7b1ff338 ci-node7b1ff338-iso" LXC_BIN="lxc" TRIAGE_BIN="$WORK/bin/stub-triage.sh"
  "LXC_VM_DF_DIR=$WORK/vmdf")

# ---- fixture builders ---------------------------------------------------------
df_at() { # <use%> — df -h with a 100% snap row (must be ignored) + / at $1
  printf 'Filesystem      Size  Used Avail Use%% Mounted on\n/dev/sda1        50G   30G   20G  %s%% /\n/dev/loop0       55M   55M     0 100%% /snap/core20/1\n' "$1"
}
dfi_at() { # <iuse%>
  printf 'Filesystem     Inodes IUsed IFree IUse%% Mounted on\n/dev/sda1       3276800 300000 2976800   %s%% /\n' "$1"
}
usage_at() { # <used GiB> <total GiB>
  printf '== pool lxd-pool ==\ninfo:\n  driver: zfs\n  space used: %sGiB\n  total space: %sGiB\n' "$1" "$2"
}
vmdf_at() { # <use%>
  printf 'Filesystem     1024-blocks  Used Available Capacity Mounted on\n/dev/sda1           20000000 8000000  12000000      %s%% /\n' "$1"
}
mkcombined() { # <file> <df%> <iuse%> <pool-used> <pool-total> <vm1%> <vm2|UNREACHABLE>
  python3 - "$1" "$(df_at "$2")" "$(dfi_at "$3")" "$(usage_at "$4" "$5")" \
    "$(vmdf_at "$6")" "$7" <<'PY'
import json, sys
def vmdf(p):
    return ("Filesystem     1024-blocks  Used Available Capacity Mounted on\n"
            "/dev/sda1           20000000 8000000  12000000      %s%% /\n" % p)
path, df, dfi, usage, v1, v2 = sys.argv[1:7]
vms = {"ci-node7b1ff338": {"df": v1}}
vms["ci-node7b1ff338-iso"] = ({"error": "lxc exec df failed"} if v2 == "UNREACHABLE"
                       else {"df": vmdf(v2)})
json.dump({"tool": "runner_disk_triage", "df": df, "df_inode": dfi,
           "lxd": "pool lxd-pool", "lxd_usage": usage, "vms": vms},
          open(path, "w"))
PY
}

run_push() { # <combined-fixture> — stub triage serves $1; prints exit code
  export TRIAGE_FIXTURE="$1" TRIAGE_FAIL=""
  : > "$PUSH_LOG"
  rm -f "$WORK/vmdf/"*.df "$WORK/vmdf/"*.err
  # Mirror the combined doc into the stub-lxc fixture dir: a per-VM .df
  # file answers `exec`, a .err file makes it fail (unreachable).
  python3 - "$1" "$WORK/vmdf" <<'PY'
import json, os, sys
doc = json.load(open(sys.argv[1]))
d = sys.argv[2]
for name, entry in doc.get("vms", {}).items():
    if isinstance(entry, dict) and entry.get("error"):
        open(os.path.join(d, name + ".err"), "w").write("unreachable")
    else:
        text = entry.get("df") if isinstance(entry, dict) else entry
        open(os.path.join(d, name + ".df"), "w").write(text or "")
PY
  OUT="$(env "${BASE_ENV[@]}" bash "$PUSH" 2>&1)"; echo "$?"
  printf '%s' "$OUT" > "$WORK/last_out.txt"
}
push_has() { grep -q "$1" "$PUSH_LOG"; }

# ---- 1. healthy pushes true ---------------------------------------------------
mkcombined "$WORK/healthy.json" 60 10 100 300 40 40
RC="$(run_push "$WORK/healthy.json")"
[[ "$RC" == "0" ]] && ok "healthy exits 0" || bad "healthy exit" "got $RC: $(cat "$WORK/last_out.txt")"
push_has "success=true" && ok "healthy pushes success=true" || bad "healthy push" "$(cat "$PUSH_LOG")"
push_has "platform_runner-disk" && ok "healthy pushes the module key" || bad "push key" "$(cat "$PUSH_LOG")"

# ---- 2. warn fails closed -----------------------------------------------------
mkcombined "$WORK/warn.json" 85 10 100 300 40 40
RC="$(run_push "$WORK/warn.json")"
[[ "$RC" == "1" ]] && ok "warn exits 1" || bad "warn exit" "got $RC"
push_has "success=false" && ok "warn pushes success=false" || bad "warn push" "$(cat "$PUSH_LOG")"
push_has "WARNING" && ok "warn names WARNING" || bad "warn text" "$(cat "$PUSH_LOG")"
# (the push URL-encodes the message, so OWNER%20devops: match the tokens
# that survive encoding — the full phrases are pinned byte-exact in §10
# against the fragment and in the evaluator reasons below.)
push_has "OWNER" && push_has "devops" && ok "warn names OWNER devops" || bad "warn owner" "$(cat "$PUSH_LOG")"
push_has "RUNBOOK" && push_has "runner-disk-triage.md" && ok "warn names RUNBOOK" || bad "warn runbook" "$(cat "$PUSH_LOG")"

# ---- 3. crit fails closed -----------------------------------------------------
mkcombined "$WORK/crit.json" 92 10 100 300 40 40
RC="$(run_push "$WORK/crit.json")"
[[ "$RC" == "1" ]] && ok "crit exits 1" || bad "crit exit" "got $RC"
push_has "CRITICAL" && ok "crit names CRITICAL" || bad "crit text" "$(cat "$PUSH_LOG")"

# ---- 4. inode-crit fails closed ----------------------------------------------
mkcombined "$WORK/inode.json" 40 91 100 300 40 40
RC="$(run_push "$WORK/inode.json")"
[[ "$RC" == "1" ]] && ok "inode-crit exits 1" || bad "inode-crit exit" "got $RC"
push_has "CRITICAL" && push_has "inode" && ok "inode-crit names CRITICAL inodes" \
  || bad "inode-crit text" "$(cat "$PUSH_LOG")"

# ---- 5. pool-near-full fails closed -------------------------------------------
mkcombined "$WORK/pool.json" 40 10 273 300 40 40
RC="$(run_push "$WORK/pool.json")"
[[ "$RC" == "1" ]] && ok "pool-near-full exits 1" || bad "pool exit" "got $RC"
push_has "CRITICAL" && push_has "pool" && ok "pool-near-full names CRITICAL pool" \
  || bad "pool text" "$(cat "$PUSH_LOG")"

# ---- 6. one VM unreachable fails closed ---------------------------------------
mkcombined "$WORK/vmunreach.json" 40 10 100 300 40 UNREACHABLE
RC="$(run_push "$WORK/vmunreach.json")"
[[ "$RC" == "1" ]] && ok "vm-unreachable exits 1" || bad "vm-unreachable exit" "got $RC"
push_has "UNKNOWN" && push_has "ci-node7b1ff338-iso" && ok "vm-unreachable names UNKNOWN vm" \
  || bad "vm-unreachable text" "$(cat "$PUSH_LOG")"

# ---- 7. malformed triage fails closed -----------------------------------------
printf 'this is not json{{{' > "$WORK/malformed.json"
export TRIAGE_FIXTURE="$WORK/malformed.json" TRIAGE_FAIL=""
: > "$PUSH_LOG"
OUT="$(env "${BASE_ENV[@]}" bash "$PUSH" 2>&1)"; RC=$?
[[ "$RC" == "1" ]] && ok "malformed exits 1" || bad "malformed exit" "got $RC: $OUT"
grep -q "success=false" "$PUSH_LOG" && ok "malformed pushes success=false" || bad "malformed push" "$(cat "$PUSH_LOG")"
grep -q "UNKNOWN" "$PUSH_LOG" && ok "malformed names UNKNOWN" || bad "malformed text" "$(cat "$PUSH_LOG")"

# dead triage (no output at all) also pushes DOWN
export TRIAGE_FAIL=1
: > "$PUSH_LOG"
OUT="$(env "${BASE_ENV[@]}" bash "$PUSH" 2>&1)"; RC=$?
[[ "$RC" == "1" ]] && grep -q "success=false" "$PUSH_LOG" && ok "dead triage pushes DOWN" \
  || bad "dead triage" "exit $RC: $OUT / $(cat "$PUSH_LOG")"
export TRIAGE_FAIL=""

# ---- 8. boundaries (ties trip the band) ---------------------------------------
mkcombined "$WORK/b79.json" 79 10 100 300 40 40
mkcombined "$WORK/b80.json" 80 10 100 300 40 40
mkcombined "$WORK/b89.json" 89 10 100 300 40 40
mkcombined "$WORK/b90.json" 90 10 100 300 40 40
check_verdict() { # <file> <want>
  got="$("$EVAL" --input "$1" 2>/dev/null)"; rc=$?
  v="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' <<<"$got" 2>/dev/null)"
  if [[ "$v" == "$2" ]]; then ok "$1 reads $2"
  else bad "$1 verdict" "want $2 got $v (exit $rc): $got"; fi
}
check_verdict "$WORK/b79.json" healthy
check_verdict "$WORK/b80.json" warning
check_verdict "$WORK/b89.json" warning
check_verdict "$WORK/b90.json" critical

# ---- 9. mutants: one weakened boundary each must mis-score --------------------
mutant_warn="$WORK/mutant_warn.sh"
python3 - "$EVAL" "$mutant_warn" <<'PY'
import sys
src = open(sys.argv[1]).read()
old = "    elif pct >= warn:"
assert src.count(old) == 1
open(sys.argv[2], "w").write(src.replace(old, "    elif pct > warn:"))
PY
bash -n "$mutant_warn" || bad "warn mutant parses" "not a valid control"
got="$(bash "$mutant_warn" --input "$WORK/b80.json" 2>/dev/null)"
v="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' <<<"$got" 2>/dev/null)"
if [[ "$v" == "healthy" ]]; then
  ok "warn mutant mis-scores 80% healthy (boundary is load-bearing)"
else
  bad "warn mutant control" "mutant still reads $v on the 80% fixture: $got"
fi

mutant_crit="$WORK/mutant_crit.sh"
python3 - "$EVAL" "$mutant_crit" <<'PY'
import sys
src = open(sys.argv[1]).read()
old = "    if pct >= crit:"
assert src.count(old) == 1
open(sys.argv[2], "w").write(src.replace(old, "    if pct > crit:"))
PY
bash -n "$mutant_crit" || bad "crit mutant parses" "not a valid control"
got="$(bash "$mutant_crit" --input "$WORK/b90.json" 2>/dev/null)"
v="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' <<<"$got" 2>/dev/null)"
if [[ "$v" == "warning" ]]; then
  ok "crit mutant mis-scores 90% warning (boundary is load-bearing)"
else
  bad "crit mutant control" "mutant still reads $v on the 90% fixture: $got"
fi

# ---- 10. definitions -----------------------------------------------------------
grep -q "name: runner-disk" "$YAML" && ok "fragment names runner-disk" || bad "fragment name" "$YAML"
grep -q "group: platform" "$YAML" && ok "fragment groups platform" || bad "fragment group" "$YAML"
grep -q "interval: 10m" "$YAML" && ok "fragment heartbeats every 10m" || bad "fragment heartbeat" "$YAML"
grep -q "warn 80% (crit 90%)" "$YAML" && ok "fragment states warn 80 / crit 90" || bad "fragment thresholds" "$YAML"
grep -q "OWNER devops" "$YAML" && ok "fragment names OWNER devops" || bad "fragment owner" "$YAML"
grep -q "RUNBOOK docs/runbooks/runner-disk-triage.md" "$YAML" && ok "fragment names RUNBOOK" || bad "fragment runbook" "$YAML"
grep -q 'token: "${GATUS_EXTERNAL_TOKEN}"' "$YAML" && ok "fragment token is an env ref" || bad "fragment token" "$YAML"
grep -q "platform_runner-disk" "$REGISTRY" && ok "MODULES.md registers the key" || bad "registry" "$REGISTRY"
grep -q "runner-disk" "$REGISTRY" && ok "MODULES.md names the module" || bad "registry module" "$REGISTRY"
grep -q '\*/5 \* \* \* \*' "$PUSH" && ok "push header carries the */5 cron line" || bad "cron line" "$PUSH"
grep -q '^export LXC_BIN$' "$PUSH" && ok "push exports LXC_BIN to the triage" || bad "LXC_BIN export" "$PUSH"
grep -q 'TRIAGE_SKIP_SIZE_SCAN' "$PUSH" && ok "push skips the triage size scan" || bad "size scan skip" "$PUSH"
grep -q "ci-node7b1ff338 ci-node7b1ff338-iso" "$PUSH" && ok "push defaults to the push-lxc-state VM list" || bad "vm default" "$PUSH"
grep -qF 'OWNER devops. RUNBOOK docs/runbooks/runner-disk-triage.md' "$PUSH" \
  && ok "push alert text is byte-exact (case_9b48cd0a)" || bad "push alert text" "$PUSH"

# ---- 11. non-disclosure ---------------------------------------------------------
# Capture-first: piping grep into an early-exiting grep under pipefail strands
# the producer (case_c25c4d3f). A herestring has no producer to strand.
BEARER_HITS="$(grep -rnE 'Bearer [A-Za-z0-9_./+=-]{8,}' "$MOD" --exclude-dir=fixtures || true)"
if [ -n "$BEARER_HITS" ] && grep -qv 'Bearer %s' <<<"$BEARER_HITS"; then
  bad "no bearer literal in module" "$(head -3 <<<"$BEARER_HITS")"
else
  ok "no bearer literal in module"
fi
# Positive control: the same grep must fire on a planted secret.
printf 'header = "Authorization: Bearer PLANTED_SECRET_MUST_TRIP_SWEEP_abc123"\n' > "$WORK/planted.txt"
if grep -qE 'Bearer [A-Za-z0-9_./+=-]{8,}' "$WORK/planted.txt"; then
  ok "secret sweep is live (planted bearer trips it)"
else
  bad "secret sweep pattern" "planted bearer did not trip the sweep"
fi
TOKEN_HITS="$(grep -rnE 'ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|sk-ant-[A-Za-z0-9_-]{8,}' "$MOD" || true)"
if grep -q . <<<"$TOKEN_HITS"; then
  bad "no token-shaped strings in module" "sweep tripped"
else
  ok "no token-shaped strings in module"
fi

# ---- 12. refusals ---------------------------------------------------------------
: > "$PUSH_LOG"
OUT="$(env -i PATH="$PATH" HOME="$WORK" GATUS_URL="http://127.0.0.1:9" bash "$PUSH" 2>&1)"; RC=$?
[[ "$RC" == "2" ]] && ok "missing Gatus token refuses (exit 2)" || bad "token refusal" "got $RC: $OUT"
[[ ! -s "$PUSH_LOG" ]] && ok "refusal pushes nothing" || bad "refusal push" "$(cat "$PUSH_LOG")"
OUT="$("$EVAL" 2>&1)"; RC=$?
[[ "$RC" == "2" ]] && ok "evaluator without --input refuses (exit 2)" || bad "evaluator refusal" "got $RC: $OUT"
OUT="$("$EVAL" --input "$WORK/healthy.json" --warn-pct 90 --crit-pct 80 2>&1)"; RC=$?
[[ "$RC" == "2" ]] && ok "inverted thresholds refuse (exit 2)" || bad "threshold refusal" "got $RC: $OUT"

# ---- 13. triage lxd_usage field ---------------------------------------------------
USE_FIX="$WORK/usage_fixture.txt"
usage_at 273 300 > "$USE_FIX"
JOUT="$(TRIAGE_WORKTREE_ROOT="$WORK/empty-wt" TRIAGE_DOCKER_ROOT="$WORK/empty-d" \
  TRIAGE_PODMAN_ROOT="$WORK/empty-p" TRIAGE_DF_FILE=/dev/null \
  TRIAGE_DFI_FILE=/dev/null TRIAGE_LXD_INFO_FILE=/dev/null \
  TRIAGE_LXD_USAGE_FILE="$USE_FIX" bash "$TRIAGE" --format json 2>&1)"; RC=$?
[[ "$RC" == "0" ]] && ok "triage with usage fixture exits 0" || bad "triage exit" "got $RC: $JOUT"
python3 -c 'import json,sys; assert "273" in json.load(sys.stdin)["lxd_usage"]' <<<"$JOUT" 2>/dev/null \
  && ok "triage json carries lxd_usage" || bad "triage lxd_usage" "$JOUT"
TOUT="$(TRIAGE_WORKTREE_ROOT="$WORK/empty-wt" TRIAGE_DOCKER_ROOT="$WORK/empty-d" \
  TRIAGE_PODMAN_ROOT="$WORK/empty-p" TRIAGE_DF_FILE=/dev/null \
  TRIAGE_DFI_FILE=/dev/null TRIAGE_LXD_INFO_FILE=/dev/null \
  TRIAGE_LXD_USAGE_FILE="$USE_FIX" bash "$TRIAGE" 2>&1)"
grep -q "273" <<<"$TOUT" && ok "triage text shows usage totals" || bad "triage text" "$TOUT"

# ---- 14. pool fallback + no-LXD skip ----------------------------------------------
python3 - "$WORK/fallback.json" <<'PY'
import json, sys
json.dump({"tool": "runner_disk_triage",
           "df": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 50G 20G 30G 40% /\n",
           "df_inode": "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/sda1 3M 1M 2M 10% /\n",
           "lxd": "pool lxd-pool driver zfs size 42GB used 38GB\n",
           "lxd_usage": "",
           "vms": {"ci-node7b1ff338": {"df": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 20000000 8000000 12000000 40% /\n"}}},
          open(sys.argv[1], "w"))
PY
check_verdict "$WORK/fallback.json" critical
python3 - "$WORK/nolxd.json" <<'PY'
import json, sys
json.dump({"tool": "runner_disk_triage",
           "df": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 50G 20G 30G 40% /\n",
           "df_inode": "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/sda1 3M 1M 2M 10% /\n",
           "lxd": "UNAVAILABLE: neither lxc nor zfs present on this host",
           "lxd_usage": "UNAVAILABLE: neither lxc nor zfs present on this host",
           "vms": {"ci-node7b1ff338": {"df": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 20000000 8000000 12000000 40% /\n"}}},
          open(sys.argv[1], "w"))
PY
check_verdict "$WORK/nolxd.json" healthy

# ---- 15. pool fails closed: a failed `lxc storage info` is never a skip ----------
mkusage() { # <file> <lxd_usage text> — healthy host and VM, only the pool varies
  python3 - "$1" "$2" <<'PY'
import json, sys
json.dump({"tool": "runner_disk_triage",
           "df": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 50G 20G 30G 40% /\n",
           "df_inode": "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/sda1 3M 1M 2M 10% /\n",
           "lxd": "pool lxd-pool", "lxd_usage": sys.argv[2],
           "vms": {"ci-node7b1ff338": {"df": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 20000000 8000000 12000000 40% /\n"}}},
          open(sys.argv[1], "w"))
PY
}
POOL_OK=$'info:\n  space used: 10GiB\n  total space: 100GiB\n'
check_reason() { # <file> <want verdict> <reason substring>
  got="$("$EVAL" --input "$1" 2>/dev/null)"
  v="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' <<<"$got" 2>/dev/null)"
  if [[ "$v" == "$2" ]] && grep -qF -- "$3" <<<"$got"; then ok "$(basename "$1") reads $2 naming '$3'"
  else bad "$(basename "$1") verdict" "want $2 + '$3' got: $got"; fi
}
mkusage "$WORK/pf_single.json" $'== pool p1 ==\ninfo unavailable\n'
check_reason "$WORK/pf_single.json" unknown "pool p1"
mkusage "$WORK/pf_list.json" "lxc storage info unavailable"
check_reason "$WORK/pf_list.json" unknown "LXD pool"
mkusage "$WORK/pf_glued.json" $'== pool p1 ==\ninfo unavailable== pool p2 ==\n'"$POOL_OK"
check_reason "$WORK/pf_glued.json" unknown "pool p1"
mkusage "$WORK/pf_split.json" $'== pool p1 ==\ninfo unavailable\n== pool p2 ==\n'"$POOL_OK"
check_reason "$WORK/pf_split.json" unknown "pool p1"
mkusage "$WORK/pf_zero.json" $'== pool p1 ==\ninfo:\n  space used: 0GiB\n  total space: 0GiB\n'
check_reason "$WORK/pf_zero.json" unknown "pool p1"
mkusage "$WORK/pf_prose.json" "note: UNAVAILABLE: neither lxc nor zfs present on this host"
check_reason "$WORK/pf_prose.json" unknown "LXD pool"
mkusage "$WORK/pf_twook.json" $'== pool p1 ==\n'"$POOL_OK"$'== pool p2 ==\n'"$POOL_OK"
check_verdict "$WORK/pf_twook.json" healthy

mutant_skip="$WORK/mutant_skip.sh"
python3 - "$EVAL" "$mutant_skip" <<'PY'
import sys
src = open(sys.argv[1]).read()
old = 'NO_LXD_RE = re.compile(r"^UNAVAILABLE: neither lxc nor zfs present", re.M)'
assert src.count(old) == 1
open(sys.argv[2], "w").write(src.replace(old, 'NO_LXD_RE = re.compile(r"unavailable", re.I)'))
PY
bash -n "$mutant_skip" || bad "skip mutant parses" "not a valid control"
for f in pf_list pf_prose; do
  got="$(bash "$mutant_skip" --input "$WORK/$f.json" 2>/dev/null)"
  v="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' <<<"$got" 2>/dev/null)"
  [[ "$v" == "healthy" ]] && ok "substring-skip mutant mis-scores $f healthy (anchor is load-bearing)" \
    || bad "skip mutant control $f" "mutant still reads $v: $got"
done

mutant_block="$WORK/mutant_block.sh"
python3 - "$EVAL" "$mutant_block" <<'PY'
import sys
src = open(sys.argv[1]).read()
old = 'problems.append(f"UNKNOWN LXD pool {name} used/total unparseable")'
assert src.count(old) == 1
open(sys.argv[2], "w").write(src.replace(old, "pass"))
PY
bash -n "$mutant_block" || bad "block mutant parses" "not a valid control"
for f in pf_glued pf_split; do
  got="$(bash "$mutant_block" --input "$WORK/$f.json" 2>/dev/null)"
  v="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' <<<"$got" 2>/dev/null)"
  [[ "$v" == "healthy" ]] && ok "per-pool mutant mis-scores $f healthy (every block must score)" \
    || bad "block mutant control $f" "mutant still reads $v: $got"
done

# end to end: the real triage under a stub lxc, first pool failing, second at 10%
mkdir -p "$WORK/pools"
printf 'p1\np2\n' > "$WORK/pools.list"
printf '%s' "$POOL_OK" > "$WORK/pools/p2.info"; printf '%s' "$POOL_OK" > "$WORK/pools/p1.info"
df_at 40 > "$WORK/e2e_df.txt"; dfi_at 10 > "$WORK/e2e_dfi.txt"
triage_e2e() { # <out.json> — real triage, stub lxc, fixture df
  TRIAGE_WORKTREE_ROOT="$WORK/empty-wt" TRIAGE_DOCKER_ROOT="$WORK/empty-d" \
    TRIAGE_PODMAN_ROOT="$WORK/empty-p" TRIAGE_DF_FILE="$WORK/e2e_df.txt" \
    TRIAGE_DFI_FILE="$WORK/e2e_dfi.txt" TRIAGE_LXD_INFO_FILE=/dev/null \
    LXC_POOLS_FILE="$WORK/pools.list" LXC_POOL_DIR="$WORK/pools" \
    bash "$TRIAGE" --format json > "$1" 2>/dev/null
  python3 - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["vms"] = {"ci-node7b1ff338": {"df": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 20000000 8000000 12000000 40% /\n"}}
json.dump(d, open(sys.argv[1], "w"))
PY
}
triage_e2e "$WORK/e2e_ok.json"
python3 -c 'import json,re,sys; u=json.load(open(sys.argv[1]))["lxd_usage"]; assert len(re.findall(r"^== pool ", u, re.M)) == 2, u' "$WORK/e2e_ok.json" 2>/dev/null \
  && ok "triage keeps each pool header on its own line" \
  || bad "triage pool headers" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lxd_usage"])' "$WORK/e2e_ok.json" 2>&1)"
check_verdict "$WORK/e2e_ok.json" healthy
touch "$WORK/pools/p1.err"
triage_e2e "$WORK/e2e_fail.json"
check_reason "$WORK/e2e_fail.json" unknown "pool p1"
rm -f "$WORK/pools/p1.err"

# ---- 16. lxc reachable only through LXC_BIN (Paperclip Review, fd2aae77) ----
# A snap lxc outside cron's PATH must still score the pool: the real push +
# real triage against a 97% pool pushes DOWN naming CRITICAL. Pre-fix the
# triage printed the no-LXD sentinel here and the push read healthy.
mkdir -p "$WORK/lxcbin" "$WORK/cleanbin" "$WORK/binonly_vmdf" "$WORK/binonly/pools"
cp "$WORK/bin/curl" "$WORK/cleanbin/curl"
cp "$WORK/bin/lxc" "$WORK/lxcbin/lxc"
chmod +x "$WORK/cleanbin/curl" "$WORK/lxcbin/lxc"
printf 'p1\n' > "$WORK/binonly/pools.list"
printf 'info:\n  space used: 97GiB\n  total space: 100GiB\n' > "$WORK/binonly/pools/p1.info"
df_at 40 > "$WORK/binonly_df.txt"; dfi_at 10 > "$WORK/binonly_dfi.txt"
vmdf_at 40 > "$WORK/binonly_vmdf/ci-node7b1ff338.df"
vmdf_at 40 > "$WORK/binonly_vmdf/ci-node7b1ff338-iso.df"
: > "$PUSH_LOG"
OUT="$(env GATUS_URL="http://127.0.0.1:9" GATUS_EXTERNAL_TOKEN="test-gatus-token-14358" \
  LXC_VMS="ci-node7b1ff338 ci-node7b1ff338-iso" LXC_BIN="$WORK/lxcbin/lxc" TRIAGE_BIN="$TRIAGE" \
  "LXC_VM_DF_DIR=$WORK/binonly_vmdf" "LXC_POOLS_FILE=$WORK/binonly/pools.list" \
  "LXC_POOL_DIR=$WORK/binonly/pools" "TRIAGE_WORKTREE_ROOT=$WORK/empty-wt" \
  "TRIAGE_DOCKER_ROOT=$WORK/empty-d" "TRIAGE_PODMAN_ROOT=$WORK/empty-p" \
  "TRIAGE_DF_FILE=$WORK/binonly_df.txt" "TRIAGE_DFI_FILE=$WORK/binonly_dfi.txt" \
  TRIAGE_LXD_INFO_FILE=/dev/null "PATH=$WORK/cleanbin:/usr/bin:/bin" "HOME=$WORK" \
  bash "$PUSH" 2>&1)"; RC=$?
[[ "$RC" == "1" ]] && ok "LXC_BIN-only lxc exits 1" || bad "LXC_BIN-only exit" "got $RC: $OUT"
push_has "success=false" && ok "LXC_BIN-only lxc pushes success=false" || bad "LXC_BIN-only push" "$(cat "$PUSH_LOG")"
if push_has "CRITICAL" && push_has "pool"; then
  ok "LXC_BIN-only lxc names the CRITICAL pool"
else
  bad "LXC_BIN-only text" "$(cat "$PUSH_LOG")"
fi

# ---- 17. size scan skipped (Paperclip Review warning, fd2aae77) --------------
mkdir -p "$WORK/scanwt/child/node_modules"
head -c 100000 /dev/zero > "$WORK/scanwt/child/node_modules/big.js"
scan_triage() { # <skip|full> — real triage over a tree with a cache dir
  if [[ "$1" == "skip" ]]; then export TRIAGE_SKIP_SIZE_SCAN=1; else unset TRIAGE_SKIP_SIZE_SCAN; fi
  TRIAGE_WORKTREE_ROOT="$WORK/scanwt" TRIAGE_DOCKER_ROOT="$WORK/empty-d" \
    TRIAGE_PODMAN_ROOT="$WORK/empty-p" TRIAGE_DF_FILE=/dev/null \
    TRIAGE_DFI_FILE=/dev/null TRIAGE_LXD_INFO_FILE=/dev/null \
    TRIAGE_LXD_USAGE_FILE=/dev/null \
    bash "$TRIAGE" --format json 2>/dev/null
  unset TRIAGE_SKIP_SIZE_SCAN
}
SKIP_OUT="$(scan_triage skip)"
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["suspects"]==[] and d["docker_root_kb"]=="SKIPPED" and d["podman_root_kb"]=="SKIPPED"' <<<"$SKIP_OUT" 2>/dev/null \
  && ok "skip-scan triage reports no sizes" || bad "skip-scan triage" "$SKIP_OUT"
FULL_OUT="$(scan_triage full)"
python3 -c 'import json,sys; d=json.load(sys.stdin); assert len(d["suspects"])==1 and "node_modules" in d["suspects"][0]["path"] and d["docker_root_kb"]!="SKIPPED"' <<<"$FULL_OUT" 2>/dev/null \
  && ok "full triage still sizes the cache (flag is load-bearing)" || bad "full triage" "$FULL_OUT"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" == "0" ]]
