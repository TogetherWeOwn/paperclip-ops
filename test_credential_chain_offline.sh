#!/usr/bin/env bash
# Offline fixture test for credential_chain_audit.sh (case_f7841f32).
#
# WHAT THIS PROTECTS. The audit enumerates every link in git's
# credential-helper resolution chain (case_965c83b3): the chain hands a live GitHub
# token to a helper program and every agent shares uid `node`, so the audit is
# a live security control. Its two verdict rules -- ownership is equivalent to
# writability, and unlink/rename is governed by the parent directory -- are
# exactly the kind of logic that rots silently. This suite pins both rules
# against fixture trees, plus the fail-open shape the audit's own header calls
# out (an absent config under a creatable parent counts, missing intermediate
# directories are not a boundary).
#
# WHY A SECOND SUITE. The vendored regression suite
# (test_credential_chain_audit.sh, restored from the toolkit pin) proves the
# audit against live host paths: real root-owned files, the container's real
# /paperclip chain, live write probes. That is its strength and its coupling:
# it assumes no GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM overrides in the
# environment and root-owned system paths, so it goes red in environments
# (like this container) where the harness exports GIT_CONFIG_GLOBAL=/dev/null
# or /etc/gitconfig is agent-owned -- for environmental reasons, not audit
# ones. This suite is the complement: fully fixture-rooted, hermetic, runnable
# by any unprivileged uid on any Linux host or runner with no live-path
# dependence at all.
#
# HERMETICITY (all asserted in section 7, not just documented):
#   * Every audit invocation runs under `env -i` with a tight PATH of
#     root-owned system dirs only. The live PATH carries agent-writable
#     entries (credential-injecting shims, the run runtime dir) that would
#     drag live paths into chain-walk outputs; the live GIT_CONFIG_GLOBAL
#     override would bypass the fixture --home entirely.
#   * HOME points at a sentinel directory that is asserted not to exist, so a
#     passing run proves HOME-independence rather than assuming it.
#   * No output may mention /paperclip or the real HOME.
#
# THE TWO PRINCIPALS. Most sections audit as the invoking user: the fixtures
# are files this process creates, so "can X substitute this" only asks the
# intended question when X is the maker. A "clean chain passes" fixture cannot
# be built that way -- anything this uid makes, it owns, and owned is
# substitutable by rule 1 -- so the clean chain is audited as `nobody`, a
# second unprivileged principal present on Linux hosts and runners. Fixture
# dirs are therefore mode 0755 (traversable by the second principal) while the
# negative fixtures stay at mktemp's default 0700.
#
# Offline and credential-free by construction: every case builds throwaway
# trees under /tmp with fabricated helpers. No network, no real secret, no
# writes outside the temp dirs, no chown (unprivileged), no live config read.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT="${CREDENTIAL_CHAIN_AUDIT_SH:-$HERE/credential_chain_audit.sh}"
PASS=0; FAIL=0

ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }
hdr() { printf '\n\033[1m%s\033[0m\n' "$1"; }
strip() { sed 's/\x1b\[[0-9;]*m//g'; }

[[ -x "$AUDIT" ]] || { echo "ERROR: $AUDIT is not executable" >&2; exit 1; }

AS_USER="$(id -un)"
if [[ "$(id -u "$AS_USER")" == "0" ]]; then
  echo "ERROR: running as root -- these assertions are about an unprivileged" >&2
  echo "       principal and root is not subject to the bits they measure." >&2
  exit 2
fi
if ! id -u nobody >/dev/null 2>&1 || [[ "$(id -u nobody)" == "0" ]]; then
  echo "ERROR: no unprivileged 'nobody' principal -- the clean-chain fixture" >&2
  echo "       needs a second uid that owns nothing under the fixture root." >&2
  exit 2
fi

# Two roots: WORK is owner-only (negative fixtures), CLEAN is traversable
# (the second principal must be able to stat inside it; the run-scratch root
# is owner-only, so both live directly under /tmp with no secrets in them).
WORK="$(mktemp -d /tmp/cc-offline-neg.XXXXXXXX)"; chmod 0700 "$WORK"
CLEAN="$(mktemp -d /tmp/cc-offline-clean.XXXXXXXX)"; chmod 0755 "$CLEAN"
cleanup() { chmod -R u+rwX "$WORK" "$CLEAN" 2>/dev/null; rm -rf "$WORK" "$CLEAN"; }
trap cleanup EXIT

# --- fixture builders ---------------------------------------------------------
# Negative tree: $WORK/home/.gitconfig names $WORK/bin/helper; $WORK/sys is
# the system scope; $WORK/empty-home has no config at all.
mkdir -p "$WORK/home" "$WORK/bin" "$WORK/empty-home"
printf '#!/bin/sh\nexit 0\n' > "$WORK/bin/helper"; chmod 0755 "$WORK/bin/helper"
printf '[credential "https://github.com"]\n\thelper = %s/bin/helper\n' "$WORK" > "$WORK/home/.gitconfig"
printf '[core]\n' > "$WORK/sys"
HELPER_SHA="$(sha256sum "$WORK/bin/helper" | cut -d' ' -f1)"
printf 'expected  %s  fixture helper\n' "$HELPER_SHA" > "$WORK/pins"

# Clean tree: same shape, traversable, nothing owned by the audited principal.
mkdir -p "$CLEAN/home" "$CLEAN/bin"; chmod 0755 "$CLEAN/home" "$CLEAN/bin"
printf '#!/bin/sh\nexit 0\n' > "$CLEAN/bin/helper"; chmod 0755 "$CLEAN/bin/helper"
printf '[credential "https://github.com"]\n\thelper = %s/bin/helper\n' "$CLEAN" > "$CLEAN/home/.gitconfig"
chmod 0644 "$CLEAN/home/.gitconfig"
printf '[core]\n' > "$CLEAN/sys"; chmod 0644 "$CLEAN/sys"
CLEAN_SHA="$(sha256sum "$CLEAN/bin/helper" | cut -d' ' -f1)"
printf 'expected  %s  fixture helper\n' "$CLEAN_SHA" > "$CLEAN/pins"
chmod 0644 "$CLEAN/pins"

# TIGHT_PATH -- root-owned system dirs only. The live PATH carries
# agent-writable entries (credential-injecting shims, the run runtime dir),
# which would drag live paths into every chain-walk output and trip the
# hermeticity assertions. This suite is about the config/helper links, not
# PATH, so every audit invocation runs under this PATH with the real git.
# GitHub-hosted /usr/local/bin can be writable; it is not a system trust root.
# Use the distribution-owned tools, never a host-local installation directory.
TIGHT_PATH=/usr/bin:/bin
# audit_self -- run the audit as the invoking user under env -i: host
# git-config overrides cannot leak in (they are dropped, not unset), and HOME
# points at a sentinel that must not exist.
audit_self() { env -i PATH="$TIGHT_PATH" HOME="$WORK/no-such-home" "$AUDIT" --as-uid "$AS_USER" "$@"; }
# audit_clean -- the clean-chain walk as the second principal, same sandbox.
audit_clean() { env -i PATH="$TIGHT_PATH" "$AUDIT" --as-uid nobody --home "$CLEAN/home" --system "$CLEAN/sys" --pins "$CLEAN/pins" "$@"; }
# Real git, resolved off the tight PATH (the live PATH may start with a
# credential-injecting shim owned by the invoking user).
GITBIN="$(env -i PATH="$TIGHT_PATH" bash -c 'command -v git')"
[[ -n "$GITBIN" ]] || { echo "ERROR: no git on $TIGHT_PATH" >&2; exit 1; }

OUT_ALL=""

hdr "1. Fixture HOME tree with a clean chain passes"
OUT_CLEAN="$(audit_clean 2>&1)"; RC_CLEAN=$?
OUT_ALL+="$OUT_CLEAN"
[[ $RC_CLEAN -eq 0 ]] && ok 'clean chain exits 0' || bad "clean chain exited $RC_CLEAN, want 0" "$OUT_CLEAN"
if grep -qF "$CLEAN/bin/helper" <<<"$(strip <<<"$OUT_CLEAN")" \
   && grep -q 'helper program' <<<"$(strip <<<"$OUT_CLEAN")"; then
  ok 'the helper program was actually examined (verdict is not vacuous)'
else
  bad 'helper program never examined -- an exit 0 that looked nowhere is not clean' "$OUT_CLEAN"
fi
if grep -q 'pin: matches the expected build' <<<"$(strip <<<"$OUT_CLEAN")"; then
  ok 'the pin check ran and matched the expected build'
else
  bad 'pin check did not confirm the expected build' "$OUT_CLEAN"
fi
if grep -q '0 hole(s), 0 indeterminate' <<<"$(strip <<<"$OUT_CLEAN")"; then
  ok 'summary reports 0 holes, 0 indeterminate'
else
  bad 'summary is not clean' "$OUT_CLEAN"
fi

hdr "2. Ownership is write permission, regardless of mode bits"
printf 'x' > "$WORK/owned-by-me"; chmod 0444 "$WORK/owned-by-me"
OUT_OWNED="$(audit_self --check-path "$WORK/owned-by-me" 2>&1)"; RC_OWNED=$?
OUT_ALL+="$OUT_OWNED"
[[ $RC_OWNED -eq 1 ]] && ok 'self-owned 0444 file exits 1' || bad "want exit 1, got $RC_OWNED" "$OUT_OWNED"
grep -qF "$WORK/owned-by-me" <<<"$(strip <<<"$OUT_OWNED")" \
  && grep -q '^HOLE' <<<"$(strip <<<"$OUT_OWNED")" \
  && ok 'self-owned 0444 file is a HOLE naming the link' \
  || bad 'self-owned 0444 file did not read as a named HOLE' "$OUT_OWNED"
# Rule-1 pin: the verdict must come from ownership, not the parent dir. A mutant
# that deletes the owner-counts-as-write rule still scores this HOLE (via the
# 0700 parent) but with a parent-dir reason -- asserting the reason kills it.
if grep -qF "owned by $AS_USER" <<<"$(strip <<<"$OUT_OWNED")"; then
  ok 'self-owned 0444 reason names ownership (rule 1, not the parent dir)'
else
  bad 'self-owned 0444 reason does not name ownership -- rule 1 is unpinned' "$OUT_OWNED"
fi
# Pairing guard: the same auditor must be able to return OK, or every HOLE
# above is vacuous (an audit that only says HOLE proves nothing). The guard
# binary must itself be foreign-owned -- asserted, not assumed.
if [[ "$(stat -L -c '%U' "$GITBIN")" == "$AS_USER" ]]; then
  bad "pairing-guard binary $GITBIN is owned by $AS_USER -- cannot prove OK is reachable"
else
  ok "pairing-guard binary $GITBIN is foreign-owned ($(stat -L -c '%U' "$GITBIN"))"
fi
OUT_GIT="$(audit_self --check-path "$GITBIN" 2>&1)"; RC_GIT=$?
OUT_ALL+="$OUT_GIT"
[[ $RC_GIT -eq 0 ]] && ok "foreign-owned $GITBIN exits 0 (HOLE verdicts are not vacuous)" \
  || bad "foreign-owned $GITBIN did not exit 0" "$OUT_GIT"

hdr "3. A writable-helper link fails, naming the link"
OUT_H="$(audit_self --check-path "$WORK/bin/helper" 2>&1)"; RC_H=$?
OUT_ALL+="$OUT_H"
[[ $RC_H -eq 1 ]] && ok 'writable helper exits 1' || bad "want exit 1, got $RC_H" "$OUT_H"
if grep -qF "$WORK/bin/helper" <<<"$(strip <<<"$OUT_H")" \
   && grep -q '^HOLE' <<<"$(strip <<<"$OUT_H")"; then
  ok 'writable helper is a HOLE naming the link'
else
  bad 'writable helper did not name the link' "$OUT_H"
fi
# Same rule-1 pin as section 2: this helper is self-owned, so the reason must
# name ownership. (The parent-dir rule alone is pinned separately in 4b.)
if grep -qF "owned by $AS_USER" <<<"$(strip <<<"$OUT_H")"; then
  ok 'writable helper reason names ownership (rule 1)'
else
  bad 'writable helper reason does not name ownership -- rule 1 is unpinned' "$OUT_H"
fi
OUT_CHAIN="$(audit_self --home "$WORK/home" --system "$WORK/sys" --pins "$WORK/pins" 2>&1)"
OUT_ALL+="$OUT_CHAIN"
if grep -q 'HOLE' <<<"$(strip <<<"$OUT_CHAIN")" \
   && grep -qF "$WORK/bin/helper" <<<"$(strip <<<"$OUT_CHAIN")" \
   && grep -q 'helper program' <<<"$(strip <<<"$OUT_CHAIN")"; then
  ok 'chain walk reports the helper program as a HOLE row'
else
  bad 'chain walk did not flag the helper program' "$OUT_CHAIN"
fi

hdr "4. A writable-config link fails"
OUT_CFG="$(audit_self --check-path "$WORK/home/.gitconfig" 2>&1)"; RC_CFG=$?
OUT_ALL+="$OUT_CFG"
[[ $RC_CFG -eq 1 ]] && ok 'writable config exits 1' || bad "want exit 1, got $RC_CFG" "$OUT_CFG"
if grep -q 'HOLE' <<<"$(strip <<<"$OUT_CFG")" \
   && grep -qF "$WORK/home/.gitconfig" <<<"$(strip <<<"$OUT_CFG")"; then
  ok 'writable config is a HOLE naming the link'
else
  bad 'writable config did not read as a named HOLE' "$OUT_CFG"
fi
# Rule-1 pin: this config is self-owned, so the reason must name ownership.
if grep -qF "owned by $AS_USER" <<<"$(strip <<<"$OUT_CFG")"; then
  ok 'writable config reason names ownership (rule 1)'
else
  bad 'writable config reason does not name ownership -- rule 1 is unpinned' "$OUT_CFG"
fi
if grep -q 'HOLE' <<<"$(strip <<<"$OUT_CHAIN")" \
   && grep -qF "$WORK/home/.gitconfig" <<<"$(strip <<<"$OUT_CHAIN")"; then
  ok 'chain walk reports the config as a HOLE row'
else
  bad 'chain walk did not flag the writable config' "$OUT_CHAIN"
fi

hdr "4b. Parent-dir rule without ownership (second principal)"
# Rule-2 pin, isolated from rule 1: the victim is 0644 owned by the invoking
# user, so for auditor `nobody` it is foreign and unwritable -- the only way to
# a HOLE is the parent dir's write bit. In a 0777 dir nobody can rename/unlink
# it (HOLE, "permits rename/unlink"); in a 1777 (sticky) dir nobody owns
# neither the dir nor the victim, so it holds (OK). A mutant that deletes the
# parent-dir check for existing files scores the 0777 case OK and fails here.
mkdir -p "$CLEAN/rwdir"; chmod 0755 "$CLEAN" "$CLEAN/rwdir"
printf 'x' > "$CLEAN/rwdir/victim"; chmod 0644 "$CLEAN/rwdir/victim"
chmod 0777 "$CLEAN/rwdir"
OUT_RW="$(env -i PATH="$TIGHT_PATH" "$AUDIT" --as-uid nobody --check-path "$CLEAN/rwdir/victim" 2>&1)"; RC_RW=$?
OUT_ALL+="$OUT_RW"
[[ $RC_RW -eq 1 ]] && ok 'foreign-owned 0644 in a 0777 dir exits 1' \
  || bad "want exit 1, got $RC_RW" "$OUT_RW"
if grep -q '^HOLE' <<<"$(strip <<<"$OUT_RW")" \
   && grep -qF "$CLEAN/rwdir/victim" <<<"$(strip <<<"$OUT_RW")" \
   && grep -q 'permits rename/unlink' <<<"$(strip <<<"$OUT_RW")"; then
  ok 'foreign-owned file in a writable dir is a HOLE via the parent dir (rule 2)'
else
  bad 'foreign-owned file in a writable dir did not read as a parent-dir HOLE' "$OUT_RW"
fi
if grep -q "owned by" <<<"$(strip <<<"$OUT_RW")"; then
  bad 'parent-dir HOLE names ownership -- rules 1 and 2 are not isolated' "$OUT_RW"
else
  ok 'parent-dir HOLE reason does not claim ownership (rules isolated)'
fi
chmod 1777 "$CLEAN/rwdir"
OUT_STICKY="$(env -i PATH="$TIGHT_PATH" "$AUDIT" --as-uid nobody --check-path "$CLEAN/rwdir/victim" 2>&1)"; RC_STICKY=$?
OUT_ALL+="$OUT_STICKY"
[[ $RC_STICKY -eq 0 ]] && ok 'same file in a 1777 sticky dir exits 0' \
  || bad "want exit 0, got $RC_STICKY" "$OUT_STICKY"
if grep -q '^OK' <<<"$(strip <<<"$OUT_STICKY")" \
   && grep -qF "$CLEAN/rwdir/victim" <<<"$(strip <<<"$OUT_STICKY")" \
   && grep -q 'parent dir holds' <<<"$(strip <<<"$OUT_STICKY")"; then
  ok 'sticky dir holds a foreign-owned victim (OK, parent dir holds)'
else
  bad 'sticky dir did not hold the foreign-owned victim' "$OUT_STICKY"
fi
chmod 0755 "$CLEAN/rwdir"

hdr "5. A creatable-but-absent config fails"
OUT_ABS="$(audit_self --home "$WORK/empty-home" --system "$WORK/sys" --pins "$WORK/pins" 2>&1)"
OUT_ALL+="$OUT_ABS"
if grep -qF "$WORK/empty-home/.gitconfig" <<<"$(strip <<<"$OUT_ABS")" \
   && grep -q 'HOLE' <<<"$(strip <<<"$OUT_ABS")" \
   && grep -qi 'creatable' <<<"$(strip <<<"$OUT_ABS")"; then
  ok 'absent config in an owned dir is a HOLE naming the path'
else
  bad 'absent config did not read as a named HOLE' "$OUT_ABS"
fi
# The historical fail-open shape: intermediate directories missing too, only
# the far ancestor is creatable (mkdir -p reaches it). Same verdict must hold.
OUT_DEEP="$(audit_self --check-path "$WORK/void/deep/config" 2>&1)"; RC_DEEP=$?
OUT_ALL+="$OUT_DEEP"
[[ $RC_DEEP -eq 1 ]] && ok 'absent config several mkdir -p levels down exits 1' \
  || bad "want exit 1, got $RC_DEEP" "$OUT_DEEP"
if grep -qF "$WORK/void/deep/config" <<<"$(strip <<<"$OUT_DEEP")" \
   && grep -q '^HOLE' <<<"$(strip <<<"$OUT_DEEP")"; then
  ok 'missing-intermediate config is a HOLE naming the path'
else
  bad 'missing-intermediate config did not read as a named HOLE' "$OUT_DEEP"
fi

hdr "6. MUTATION: relaxing the directory-owner rule must flip the deep fixture"
# Delete exactly the "owning the directory counts as write" line from a
# throwaway copy. The mutant must then score the section-5 deep fixture OK
# (exit 0) while the real audit scores it HOLE: that is what proves section 5
# is load-bearing rather than passing for an unrelated reason.
cp "$AUDIT" "$WORK/mutant.sh"
MUT_ANCHOR='"$AS_UID" ]]; then can_write=0'
if ! grep -qF "$MUT_ANCHOR" "$WORK/mutant.sh"; then
  bad 'mutation anchor absent -- audit changed shape, fixture needs review' "$MUT_ANCHOR"
else
  sed -i "s|$MUT_ANCHOR|\"\$AS_UID\" ]]; then can_write=1  # MUTANT: owner no longer counts|" "$WORK/mutant.sh"
  bash -n "$WORK/mutant.sh" && ok 'mutant still parses' || bad 'mutant does not parse'
  N_DIFF="$(diff "$AUDIT" "$WORK/mutant.sh" | grep -c '^[<>]')"
  [[ "$N_DIFF" -eq 2 ]] && ok 'mutant differs by exactly one line' \
    || bad "mutant differs by $N_DIFF diff lines, want 2 (one changed line)" "$(diff "$AUDIT" "$WORK/mutant.sh")"
  OUT_MUT="$(env -i PATH="$TIGHT_PATH" HOME="$WORK/no-such-home" bash "$WORK/mutant.sh" --as-uid "$AS_USER" --check-path "$WORK/void/deep/config" 2>&1)"; RC_MUT=$?
  if [[ $RC_MUT -eq 0 ]] && grep -q '^OK' <<<"$(strip <<<"$OUT_MUT")"; then
    ok 'mutant scores the deep fixture OK -- the owner rule is load-bearing'
  else
    bad "mutant did not miss the hole (rc=$RC_MUT) -- section 5 proves nothing" "$OUT_MUT"
  fi
  if [[ $RC_DEEP -eq 1 ]]; then
    ok 'control -- the unmutated audit scores the same fixture HOLE'
  else
    bad 'control failed -- unmutated audit did not flag the deep fixture'
  fi
fi

hdr "7. The test never touches the real HOME or /paperclip chain"
if [[ ! -e "$WORK/no-such-home" ]]; then
  ok 'HOME sentinel does not exist -- verdicts above are HOME-independent'
else
  bad 'HOME sentinel exists -- hermeticity proof is void'
fi
if grep -qF '/paperclip' <<<"$OUT_ALL"; then
  bad 'an audit output mentioned /paperclip -- a live path leaked in' "$(grep -F '/paperclip' <<<"$OUT_ALL")"
else
  ok 'no audit output mentions /paperclip'
fi
if grep -qF "$HOME" <<<"$OUT_ALL"; then
  bad "an audit output mentioned the real HOME ($HOME)" "$(grep -F "$HOME" <<<"$OUT_ALL")"
else
  ok 'no audit output mentions the real HOME'
fi
# The env -i sandbox above is load-bearing, not hygiene: host git-config
# overrides (GIT_CONFIG_GLOBAL etc.) would otherwise leak into the walk. Prove
# the sandbox drops them by exporting hostile vars in THIS shell, then running
# the isolated walk (which uses env -i) and asserting it still sees the
# fixture. This deliberately does NOT assert what an unisolated walk does: the
# audit currently prefers $GIT_CONFIG_GLOBAL over --home, which is a fail-open
# the audit may harden later, and pinning the blind-unisolated behavior here
# would turn that hardening red.
_OLD_GLOBAL="${GIT_CONFIG_GLOBAL-__unset__}"
_OLD_SYSTEM="${GIT_CONFIG_SYSTEM-__unset__}"
_OLD_XDG="${XDG_CONFIG_HOME-__unset__}"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null XDG_CONFIG_HOME=/dev/null
OUT_SANDBOXED="$(audit_self --home "$WORK/home" --system "$WORK/sys" --pins "$WORK/pins" 2>&1)"
if [[ "$_OLD_GLOBAL" == "__unset__" ]]; then unset GIT_CONFIG_GLOBAL; else export GIT_CONFIG_GLOBAL="$_OLD_GLOBAL"; fi
if [[ "$_OLD_SYSTEM" == "__unset__" ]]; then unset GIT_CONFIG_SYSTEM; else export GIT_CONFIG_SYSTEM="$_OLD_SYSTEM"; fi
if [[ "$_OLD_XDG" == "__unset__" ]]; then unset XDG_CONFIG_HOME; else export XDG_CONFIG_HOME="$_OLD_XDG"; fi
OUT_ALL+="$OUT_SANDBOXED"
if grep -qF "$WORK/bin/helper" <<<"$(strip <<<"$OUT_SANDBOXED")" \
   && grep -q 'HOLE' <<<"$(strip <<<"$OUT_SANDBOXED")"; then
  ok 'isolated walk still sees the fixture under hostile outer env (env -i drops overrides)'
else
  bad 'isolated walk lost the fixture under hostile outer env -- sandbox leaks' "$OUT_SANDBOXED"
fi
if grep -qF "$WORK/bin/helper" <<<"$(strip <<<"$OUT_CHAIN")"; then
  ok 'control -- the earlier isolated walk saw the same fixture'
else
  bad 'isolated walk did not see the fixture either -- sandbox proof proves nothing' "$OUT_CHAIN"
fi

hdr "8. Writable host-local PATH entries cannot contaminate the fixture"
mkdir -p "$CLEAN/writable-path"; chmod 0777 "$CLEAN/writable-path"
OUT_PATH_CLEAN="$(PATH="$CLEAN/writable-path:$PATH" audit_clean 2>&1)"; RC_PATH_CLEAN=$?
[[ $RC_PATH_CLEAN -eq 0 ]] && ok 'isolated clean walk ignores a writable outer PATH entry' \
  || bad 'outer PATH contaminated the clean fixture' "$OUT_PATH_CLEAN"
OUT_PATH_HOLE="$(env -i PATH="$CLEAN/writable-path:$TIGHT_PATH" "$AUDIT" --as-uid nobody --home "$CLEAN/home" --system "$CLEAN/sys" --pins "$CLEAN/pins" 2>&1)"; RC_PATH_HOLE=$?
if [[ $RC_PATH_HOLE -eq 1 ]] && grep -qF "$CLEAN/writable-path" <<<"$OUT_PATH_HOLE" \
   && grep -q 'PATH dir: nobody can add or replace executables' <<<"$OUT_PATH_HOLE"; then
  ok 'control: production audit still rejects the same writable PATH when supplied'
else
  bad 'writable PATH control failed; isolation must not weaken the production audit' "$OUT_PATH_HOLE"
fi

hdr "Result"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
