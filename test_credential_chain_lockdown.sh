#!/usr/bin/env bash
# Offline fixture test for credential_chain_lockdown.sh (case_368d4f39).
#
# What this proves, and what it deliberately does not touch:
#
#   * The script under test is the 420-line case_965c83b3 proposal an operator runs
#     as root (dry-run / apply / verify). It has zero test references anywhere
#     else in the repo. This suite is its first.
#   * Hermetic by construction. Every fixture lives under one mktemp tree
#     (under ${PAPERCLIP_RUN_SCRATCH_DIR:-$TMPDIR:-/tmp}, never /paperclip).
#     The only runs against the REAL script exercise argument handling and
#     read-only paths (unknown-arg, dry-run plan, the two --apply refusals);
#     a stub-PATH log proves those runs never reach chown/chmod/install/
#     runuser, and fixture checksums prove nothing was written. The --verify
#     verdict cases run against disposable COPIES with their /paperclip
#     constants rewritten into the fixture, so the real HOME (/paperclip),
#     the real git config and the real uid are never written, renamed, or
#     re-executed. No root, no network, no credentials, no git writes.
#   * The principal is always whoever runs this suite (uid 1000 node in the
#     agent container, the shared agent uid from case_e848a83d). Fixture files are
#     owned by that user, so "locked" is modelled with mode bits (0444/0555:
#     test -w / touch / mv all refuse) rather than with root ownership no
#     test process could synthesise. Same convention as
#     test_credential_chain_audit.sh's header.
#
# NOTE on the card text: the acceptance line says "--dry-run (default) ...
# exits nonzero". The script exits 0 on dry-run by design (it prints the plan
# and the "Dry run. Re-run with --apply" line; nonzero is reserved for
# --verify finding a hole and for usage errors, exit 2). Changing that would
# be a behaviour change, which is out of scope for this card, so this suite
# pins the actual contract -- exit 0 plus a byte-identical fixture -- and
# records the discrepancy here rather than asserting a failure.
#
# Sections:
#   1. argument handling (unknown flag exits 2; default is dry-run)
#   2. dry-run over a DIRTY fixture: plan reported, exit 0, fixture
#      byte-identical, no privileged call attempted
#   3. --apply refusals as non-root (provenance refusal without the flag,
#      root refusal with it)
#   4. --verify verdict logic on fixture copies (all-refused passes,
#      one writable link fails naming it, a failing audit folds in,
#      a renameable grandparent fails naming it -- case_0b186cde)
#
# Mutation proof (run by hand, not wired here): dull any single
# `rc_w=1` arm in a scratch copy and at least one section-4 case goes red;
# delete the provenance `exit 2` and section 3 goes red. Reported on the card.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCKDOWN="$HERE/credential_chain_lockdown.sh"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); shift; [[ $# -gt 0 ]] && sed 's/^/        /' <<<"$*"; }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }

TMP="$(mktemp -d "${PAPERCLIP_RUN_SCRATCH_DIR:-${TMPDIR:-/tmp}}/case_d815a870-XXXXXX")" || exit 2
cleanup() { chmod -R u+rwX "$TMP" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Guard PATH: any attempt to reach a privileged/write command is recorded.
# The real-script cases below (dry-run, both --apply refusals) must leave
# this log empty -- they refuse or report before the first mutation.
# ---------------------------------------------------------------------------
GUARD="$TMP/guard-bin"; mkdir -p "$GUARD"
GUARD_LOG="$TMP/guard.calls"; rm -f "$GUARD_LOG"
for cmd in chown chmod install runuser; do
  printf '#!/usr/bin/env bash\necho "CALLED: %s $*" >> "%s"\nexit 1\n' "$cmd" "$GUARD_LOG" > "$GUARD/$cmd"
  chmod +x "$GUARD/$cmd"
done
# The harness BASH_ENV hard-resets PATH in every child bash (measured
# 2026-10-10), so a PATH-prefix shim never survives into the script under
# test and would pass vacuously. Override BASH_ENV instead: the child
# sources OUR rc, which puts the shim dir first. BASH_ENV only affects bash
# children; dash/sh grandchildren inherit the exported PATH, which is what
# the fixture's `sh -c` probes run under.
GUARD_RC="$TMP/guard-rc.sh"
printf 'export PATH="%s:/usr/local/bin:/usr/bin:/bin"\n' "$GUARD" > "$GUARD_RC"
guarded() { BASH_ENV="$GUARD_RC" "$@"; }
guard_empty() {
  if [[ -s "$GUARD_LOG" ]]; then bad "$1" "$(cat "$GUARD_LOG")"; else ok "$1"; fi
  rm -f "$GUARD_LOG"
}
# Positive control: the tripwire must fire when a privileged call IS made,
# or every guard_empty below is an assertion about nothing. BASH_ENV only
# affects bash children, so the call goes through an explicit inner bash --
# exactly the shape the script-under-test runs in (shebang -> bash -> rc).
guarded bash -c "chown root:root $TMP/probe" 2>/dev/null || true
if grep -q 'CALLED: chown' "$GUARD_LOG" 2>/dev/null; then
  ok "guard tripwire records a privileged call (positive control)"
else
  bad "guard tripwire records a privileged call (positive control)"
fi
rm -f "$GUARD_LOG"

# run_case <desc> <want-rc> <command...> -- compares rc, stashes output in
# LAST_OUT. Must be called directly (never in $(...)): ok/bad increment the
# parent's counters, which a command-substitution subshell would swallow.
LAST_OUT=""
run_case() {
  local desc="$1" want="$2"; shift 2
  local out rc
  set +e
  out="$("$@" 2>&1)"; rc=$?
  set -e
  LAST_OUT="$out"
  if [[ $rc -eq "$want" ]]; then ok "$desc (exit $rc)"; else bad "$desc (want $want, got $rc)" "$out"; fi
}

# ---------------------------------------------------------------------------
hdr "1. Argument handling"
# ---------------------------------------------------------------------------
run_case "unknown argument exits 2" 2 guarded "$LOCKDOWN" --frobnicate
out="$LAST_OUT"
grep -q 'unknown argument' <<<"$out" \
  && ok "unknown argument names the flag" \
  || bad "unknown argument names the flag" "$out"
guard_empty "unknown-arg run attempted no privileged call"

run_case "default (no args) is a dry run printing the plan" 0 guarded "$LOCKDOWN"
out="$LAST_OUT"
grep -q 'Dry run. Re-run with --apply' <<<"$out" \
  && ok "default run reports the dry-run plan" \
  || bad "default run reports the dry-run plan" "$out"
guard_empty "default run attempted no privileged call"

# ---------------------------------------------------------------------------
hdr "2. Dry-run over a dirty fixture changes nothing"
# ---------------------------------------------------------------------------
# A fixture copy with /paperclip rewritten into $DIRTY, deliberately dirty
# (writable helper + writable helper dir), run with no args and with
# --dry-run. Both must print the plan, exit 0, and leave the tree identical.
DIRTY="$TMP/dirty"; mkdir -p "$DIRTY"
stage_copy() { # stage_copy <dest-dir> <fixture-root> -- rewrites paths, strips runuser
  local dest="$1" fix="$2"
  python3 - "$LOCKDOWN" "$dest" "$fix" <<'PY'
import sys
src, dest, fix = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(src).read()
n_paperclip = s.count('/paperclip')
s = s.replace('/paperclip', fix)
n_runuser = s.count('runuser -u node -- ')
s = s.replace('runuser -u node -- ', '')
open(dest, 'w').write(s)
print(f"{n_paperclip} {n_runuser}")
PY
}
counts="$(stage_copy "$DIRTY/lockdown.sh" "$DIRTY/root")"
[[ "$counts" == "31 5" ]] \
  && ok "fixture rewrite replaced all 31 /paperclip refs and 5 runuser probes" \
  || bad "fixture rewrite drifted (got '$counts', want '31 5') -- the script changed shape"
chmod +x "$DIRTY/lockdown.sh"
# The stub audit next to the copy: clean, so any verdict comes from probes.
printf '#!/usr/bin/env bash\necho "stub audit: clean"\nexit 0\n' > "$DIRTY/credential_chain_audit.sh"
chmod +x "$DIRTY/credential_chain_audit.sh"
mkdir -p "$DIRTY/root/.local/bin" "$DIRTY/root/.config/git"
printf 'placeholder helper\n' > "$DIRTY/root/.local/bin/gh-app-token.js"
chmod 0644 "$DIRTY/root/.local/bin/gh-app-token.js"   # writable link: dirty
chmod 0755 "$DIRTY/root/.local/bin"                    # accepts new files: dirty
printf '[credential]\n' > "$DIRTY/root/.gitconfig"
chmod 0644 "$DIRTY/root/.gitconfig"
printf '' > "$DIRTY/root/.config/git/config"
before="$(cd "$DIRTY" && find . -type f -exec sha256sum {} + | sort)"
for flag in "" "--dry-run"; do
  if [[ -z "$flag" ]]; then
    run_case "dirty-fixture dry run (no args) exits 0" 0 guarded "$DIRTY/lockdown.sh"
    out="$LAST_OUT"
  else
    run_case "dirty-fixture dry run (--dry-run) exits 0" 0 guarded "$DIRTY/lockdown.sh" --dry-run
    out="$LAST_OUT"
  fi
  grep -q 'Dry run. Re-run with --apply' <<<"$out" \
    && ok "dirty-fixture dry run reports the plan ($flag:-default)" \
    || bad "dirty-fixture dry run reports the plan ($flag:-default)" "$out"
  # The dirty state must be VISIBLE in the report: show_state prints each
  # fixture path with its mode bits, so the writable helper must appear WITH
  # its dirty mode. The `Plan` header alone proves nothing -- it prints
  # unconditionally (hdr "Plan", credential_chain_lockdown.sh) even when
  # show_state sees no dirt, so asserting on it is vacuous. A locked (0444)
  # helper would render `-r--r--r--`; the dirty 0644 fixture must render
  # `-rw-r--r--` on the helper's own line.
  if grep -E -q "gh-app-token\.js +-rw-r--r--" <<<"$out"; then
    ok "dirty-fixture dry run shows the writable helper with dirty mode ($flag:-default)"
  else
    bad "dirty-fixture dry run shows the writable helper with dirty mode ($flag:-default)" "$out"
  fi
done
after="$(cd "$DIRTY" && find . -type f -exec sha256sum {} + | sort)"
[[ "$before" == "$after" ]] \
  && ok "dry run left the dirty fixture byte-identical" \
  || bad "dry run left the dirty fixture byte-identical" "$(diff <(printf '%s' "$before") <(printf '%s' "$after") | head -10)"
guard_empty "dry-run pair attempted no privileged call"
[[ -e "$DIRTY/root/.local/bin/.case_03f8828b-probe" || -e "$DIRTY/root/.config/git/.case_03f8828b-probe" ]] \
  && bad "dry run left probe files behind" \
  || ok "dry run left no probe files behind"

# ---------------------------------------------------------------------------
hdr "3. --apply refusals as non-root"
# ---------------------------------------------------------------------------
# The provenance gate fires when the copy is owned by the agent uid (`node`).
# That holds in the agent container (checkout owned by node) but NOT on hosts
# with no `node` user (GitHub runners: checkout owned by `runner`, mode 644),
# where the gate correctly passes and --apply falls through to the root gate
# instead -- same exit 2, different output. The runner IS the agent here (the
# header convention: the principal is whoever runs this suite), so these two
# cases run under an id shim that answers `id -u node` with the invoking uid
# and delegates every other shape to the real binary. Section-local: no other
# section uses this PATH, and the mode arm below (0777 copy) needs no shim on
# any host.
IDSHIM="$TMP/idshim-bin"; mkdir -p "$IDSHIM"
for cmd in chown chmod install runuser; do cp "$GUARD/$cmd" "$IDSHIM/$cmd"; done
printf '#!/usr/bin/env bash\nif [[ $# -eq 2 && $1 == "-u" && $2 == "node" ]]; then exec /usr/bin/id -u; else exec /usr/bin/id "$@"\nfi\n' > "$IDSHIM/id"
chmod +x "$IDSHIM/id"
IDSHIM_RC="$TMP/idshim-rc.sh"
printf 'export PATH="%s:/usr/local/bin:/usr/bin:/bin"\n' "$IDSHIM" > "$IDSHIM_RC"
idshimmed() { BASH_ENV="$IDSHIM_RC" "$@"; }
# --apply without the flag must be refused before anything else runs.
run_case "--apply without the flag hits the provenance refusal" 2 idshimmed "$LOCKDOWN" --apply
out="$LAST_OUT"
grep -q 'REFUSING TO APPLY' <<<"$out" \
  && ok "provenance refusal says REFUSING TO APPLY" \
  || bad "provenance refusal says REFUSING TO APPLY" "$out"
grep -q 'i-have-verified-this-copy' <<<"$out" \
  && ok "provenance refusal names the re-run flag" \
  || bad "provenance refusal names the re-run flag" "$out"
guard_empty "provenance refusal attempted no privileged call"

# With the flag the provenance gate is skipped and the root gate fires: we
# are not root, so --apply must still refuse with exit 2 before writing.
# (Same shim PATH for uniformity; the flag skips the gate, so the shim is
# inert here -- bare `id -u` delegates to the real binary.)
run_case "--apply with the flag refuses without root" 2 idshimmed "$LOCKDOWN" --apply --i-have-verified-this-copy
out="$LAST_OUT"
grep -qi 'must run as root' <<<"$out" \
  && ok "verified-copy apply names the root requirement" \
  || bad "verified-copy apply names the root requirement" "$out"
guard_empty "root refusal attempted no privileged call"

# Agent-writable script copy: world-writable mode forces the provenance gate
# regardless of owner layout, so this also holds on hosts with no `node` user.
PROV="$TMP/prov"; mkdir -p "$PROV"
# 0777 keeps the exec bit (0666 would not run: exit 126, not the refusal)
# while the world-writable low bit forces the mode arm of the gate, so this
# refuses on any host layout, even one with no `node` user.
cp "$LOCKDOWN" "$PROV/lockdown.sh" && chmod 0777 "$PROV/lockdown.sh"
run_case "agent-writable copy is refused (provenance case)" 2 guarded "$PROV/lockdown.sh" --apply
out="$LAST_OUT"
grep -q 'REFUSING TO APPLY' <<<"$out" \
  && ok "writable-copy refusal says REFUSING TO APPLY" \
  || bad "writable-copy refusal says REFUSING TO APPLY" "$out"

# ---------------------------------------------------------------------------
hdr "4. --verify verdict logic on fixture copies"
# ---------------------------------------------------------------------------
# Fake-root id stub: `id -u` answers 0 so the verify root gate passes in the
# fixture, while `id -u <user>` still delegates to the real binary so the
# provenance arithmetic keeps its real shape.
FAKEBIN="$TMP/fakebin"; mkdir -p "$FAKEBIN"
printf '#!/usr/bin/env bash\nif [[ $# -eq 1 && $1 == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi\n' > "$FAKEBIN/id"
chmod +x "$FAKEBIN/id"
# Same BASH_ENV mechanism as the guard: the verify stages must also prove
# they never reach a write path, and that the runuser strip was complete --
# so the tripwire stubs ride along in this PATH too.
for cmd in chown chmod install runuser; do
  printf '#!/usr/bin/env bash\necho "CALLED: %s $*" >> "%s"\nexit 1\n' "$cmd" "$GUARD_LOG" > "$FAKEBIN/$cmd"
  chmod +x "$FAKEBIN/$cmd"
done
FAKE_RC="$TMP/fake-rc.sh"
printf 'export PATH="%s:/usr/local/bin:/usr/bin:/bin"\n' "$FAKEBIN" > "$FAKE_RC"
# Positive controls for the fake root (same inner-bash shape as above).
if [[ "$(BASH_ENV="$FAKE_RC" bash -c 'id -u')" == "0" ]]; then
  ok "fake root answers uid 0 to a bare id -u (positive control)"
else
  bad "fake root answers uid 0 to a bare id -u (positive control)"
fi
if [[ "$(BASH_ENV="$FAKE_RC" bash -c 'id -u node')" == "$(id -u node)" ]]; then
  ok "fake root delegates id -u <user> to the real uid (positive control)"
else
  bad "fake root delegates id -u <user> to the real uid (positive control)"
fi

# build_stage <name> <audit-rc> [overrides...] -- locked tree + stub audit.
# Each override is <relpath>:<mode> applied after the locked baseline.
build_stage() {
  local name="$1" audit_rc="$2"; shift 2
  # Two statements, not one: `local a=X b="$a/…"` reads the NEWLY-local
  # (unset) a on the RHS and dies under `set -u`.
  local st="$TMP/stage-$name"
  local fix="$st/root"
  rm -rf "$st"; mkdir -p "$st"
  stage_copy "$st/lockdown.sh" "$fix" >/dev/null
  chmod +x "$st/lockdown.sh"
  printf '#!/usr/bin/env bash\necho "stub audit (rc=%s)"\nexit %s\n' "$audit_rc" "$audit_rc" > "$st/credential_chain_audit.sh"
  chmod +x "$st/credential_chain_audit.sh"
  mkdir -p "$fix/.local/bin" "$fix/.config/git"
  printf 'placeholder helper\n' > "$fix/.local/bin/gh-app-token.js"
  printf '[credential]\n' > "$fix/.gitconfig"
  printf '' > "$fix/.config/git/config"
  # Locked baseline: no write bit anywhere the probes can reach.
  chmod 0444 "$fix/.local/bin/gh-app-token.js" "$fix/.gitconfig" "$fix/.config/git/config"
  chmod 0555 "$fix/.local/bin" "$fix/.local" "$fix/.config/git" "$fix/.config" "$fix"
  # while-shift, not `for ov in "$@"`: an empty "$@" is unbound under
  # `set -u` on older bash and aborts the suite.
  while [[ $# -gt 0 ]]; do
    chmod "${1##*:}" "$fix/${1%%:*}" || { bad "stage $name: cannot chmod $1"; return 1; }
    shift
  done
  printf '%s' "$st"
}
run_verify() { # run_verify <stage> -- BASH_ENV carries fake id + tripwires
  BASH_ENV="$FAKE_RC" "$1/lockdown.sh" --verify 2>&1
}

ST_CLEAN="$(build_stage clean 0)"
out=""; rc=0
set +e; out="$(run_verify "$ST_CLEAN")"; rc=$?; set -e
[[ $rc -eq 0 ]] \
  && ok "all-refused fixture verifies clean (exit 0)" \
  || bad "all-refused fixture verifies clean (want 0, got $rc)" "$out"
grep -q 'audit exit: 0' <<<"$out" \
  && ok "clean run consulted the stub audit" \
  || bad "clean run consulted the stub audit" "$out"

ST_Writable="$(build_stage writable 0 ".local/bin/gh-app-token.js:0644")"
out=""; rc=0
set +e; out="$(run_verify "$ST_Writable")"; rc=$?; set -e
[[ $rc -ne 0 ]] \
  && ok "one writable link fails verification (exit $rc)" \
  || bad "one writable link fails verification (want nonzero, got $rc)" "$out"
grep -q 'STILL WRITABLE' <<<"$out" && grep -q 'gh-app-token.js' <<<"$out" \
  && ok "writable-link failure names the helper" \
  || bad "writable-link failure names the helper" "$out"

ST_AUDIT="$(build_stage audit-hole 1)"
out=""; rc=0
set +e; out="$(run_verify "$ST_AUDIT")"; rc=$?; set -e
[[ $rc -ne 0 ]] \
  && ok "a failing audit folds into the verdict (exit $rc)" \
  || bad "a failing audit folds into the verdict (want nonzero, got $rc)" "$out"
grep -q 'audit exit: 1' <<<"$out" \
  && ok "audit-hole run reports the audit exit" \
  || bad "audit-hole run reports the audit exit" "$out"

ST_GRANDPARENT="$(build_stage grandparent 0 ".local:0755")"
out=""; rc=0
set +e; out="$(run_verify "$ST_GRANDPARENT")"; rc=$?; set -e
[[ $rc -ne 0 ]] \
  && ok "a renameable helper dir fails verification, case_0b186cde (exit $rc)" \
  || bad "a renameable helper dir fails verification, case_0b186cde (want nonzero, got $rc)" "$out"
grep -q 'STILL RENAMEABLE' <<<"$out" && grep -q '\.local/bin' <<<"$out" \
  && ok "grandparent failure names the helper dir" \
  || bad "grandparent failure names the helper dir" "$out"

# --verify must never reach a write path: the tripwire log must hold no
# chown/chmod/install call, and no runuser call (the fixture strip removed
# all five; any surviving one means the rewrite drifted).
if [[ -s "$GUARD_LOG" ]]; then
  bad "verify stages attempted no privileged call" "$(cat "$GUARD_LOG")"
else
  ok "verify stages attempted no privileged call"
fi
rm -f "$GUARD_LOG"
# Probes must clean up after themselves: no .case_03f8828b-probe / .probe strays,
# and the case_0b186cde rename must have been restored. One find per existing
# stage root: `find $TMP/stage-` names nothing that exists and exits 1,
# which aborts the suite under `set -e` before the tally.
leftovers=""
for st in "$ST_CLEAN" "$ST_Writable" "$ST_AUDIT" "$ST_GRANDPARENT"; do
  found="$(find "$st" \( -name '.case_03f8828b-probe' -o -name '*.probe' -o -name '*.case_a70ecfedprobe' \) 2>/dev/null)"
  [[ -n "$found" ]] && leftovers+="$found"$'\n'
done
[[ -z "$leftovers" ]] \
  && ok "verify probes left no strays and restored every rename" \
  || bad "verify probes left no strays and restored every rename" "$leftovers"
[[ -d "$TMP/stage-grandparent/root/.local/bin" ]] \
  && ok "case_0b186cde probe restored the helper dir" \
  || bad "case_0b186cde probe restored the helper dir"

# ---------------------------------------------------------------------------
printf '\n\033[1m%d passed, %d failed\033[0m\n\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
