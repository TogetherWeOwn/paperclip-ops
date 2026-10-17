#!/usr/bin/env bash
# ===========================================================================
# paperclip-upgrade/post-restart-reharden.sh — VERIFY-ONLY check of the
# agent-immutability baseline after a Paperclip container restart.
#
# This script used to re-apply ownership with recursive root ownership
# changes. That repair is now forbidden on the activation path: an
# activation must never change ownership under the Paperclip mounts,
# directly or through a helper. The hardened baseline is therefore a
# PRECONDITION installed out of band; this script only proves it still
# holds and fails closed when it does not. It never repairs metadata:
# a mismatch is a failed gate for the operator to resolve under separate
# authority, not something an upgrade quietly fixes.
#
# Baseline judged (host side, by stat; nothing is modified):
#   - $DATA/.local, $DATA/.gitconfig, $DATA/plugin-packages-root exist as
#     non-symlink paths owned by $EXPECT_OWNER (default 0:0) with no
#     group/other write bit;
#   - when the bounded Cargo pool is required (or present as a
#     mountpoint), its ROOT and policy.json are judged the same way,
#     NON-recursively: slot directories beneath the root stay
#     agent-writable and are never inspected for tightening;
#   - effective access as the agent uid (docker exec probes): every
#     protected container path must DENY writes, and a throwaway create
#     in the launcher bin must fail.
#
# Environment:
#   PAPERCLIP_DATA                          host data dir
#   PAPERCLIP_UPGRADE_EXPECT_OWNER          expected uid:gid (required by
#                                           the activation manifest; the
#                                           0:0 default exists only for
#                                           standalone operator use)
#   PAPERCLIP_UPGRADE_REQUIRE_CARGO_POOL    1: a missing/unmounted pool
#                                           FAILS; 0 (default): absent
#                                           pool is reported and skipped,
#                                           a present pool is still judged
#   PAPERCLIP_UPGRADE_SERVER_CONTAINER      container name (paperclip)
#   PAPERCLIP_UPGRADE_AGENT_UID             probe uid (1000)
#   DOCKER                                  docker binary
#
# Exit status: 0 baseline proven | 1 a check failed | 2 refused
# ===========================================================================
set -uo pipefail

ME="${BASH_SOURCE[0]##*/}"
DATA_DIR="${PAPERCLIP_DATA:-$HOME/stacks/paperclip/data}"
SERVER_CONTAINER="${PAPERCLIP_UPGRADE_SERVER_CONTAINER:-paperclip}"
AGENT_UID="${PAPERCLIP_UPGRADE_AGENT_UID:-1000}"
# ${VAR-default} (no colon): an explicitly empty value stays empty and is
# refused below instead of silently falling back to the default baseline.
EXPECT_OWNER="${PAPERCLIP_UPGRADE_EXPECT_OWNER-0:0}"
REQUIRE_POOL="${PAPERCLIP_UPGRADE_REQUIRE_CARGO_POOL:-0}"
DOCKER="${DOCKER:-docker}"

[[ -n "$EXPECT_OWNER" && "$EXPECT_OWNER" =~ ^[0-9]+:[0-9]+$ ]] \
  || { printf 'REFUSED: %s: PAPERCLIP_UPGRADE_EXPECT_OWNER must be uid:gid (the ownership baseline); refusing to guess\n' "$ME" >&2; exit 2; }
[[ "$REQUIRE_POOL" =~ ^[01]$ ]] \
  || { printf 'REFUSED: %s: PAPERCLIP_UPGRADE_REQUIRE_CARGO_POOL must be 0 or 1\n' "$ME" >&2; exit 2; }
[[ "$AGENT_UID" =~ ^[0-9]+$ ]] \
  || { printf 'REFUSED: %s: PAPERCLIP_UPGRADE_AGENT_UID must be numeric\n' "$ME" >&2; exit 2; }
[[ -d "$DATA_DIR" && ! -L "$DATA_DIR" ]] \
  || { printf 'REFUSED: %s: data dir missing or a symlink: %s\n' "$ME" "$DATA_DIR" >&2; exit 2; }
command -v "$DOCKER" >/dev/null \
  || { printf 'REFUSED: %s: docker runtime is missing\n' "$ME" >&2; exit 2; }
command -v mountpoint >/dev/null \
  || { printf 'REFUSED: %s: mountpoint is missing (pool mount state cannot be judged)\n' "$ME" >&2; exit 2; }

rc=0
check_path() { # PATH -- owned by EXPECT_OWNER, no group/other write, no symlink
  local path="$1" st uid gid mode
  if [[ -L "$path" || ! -e "$path" ]]; then
    printf 'FAIL: protected path missing or a symlink: %s (baseline not repaired; resolve out of band)\n' "$path"
    return 1
  fi
  st="$(stat -c '%u %g %a' -- "$path" 2>/dev/null)" \
    || { printf 'FAIL: cannot stat protected path: %s\n' "$path"; return 1; }
  read -r uid gid mode <<<"$st"
  if [[ "$uid:$gid" != "$EXPECT_OWNER" ]]; then
    printf 'FAIL: %s owned by %s:%s, want %s (ownership is never repaired on this path)\n' "$path" "$uid" "$gid" "$EXPECT_OWNER"
    return 1
  fi
  if (( (8#$mode & 8#022) != 0 )); then
    printf 'FAIL: %s mode %s carries a group/other write bit\n' "$path" "$mode"
    return 1
  fi
  printf 'ok (baseline): %s owner %s mode %s\n' "$path" "$EXPECT_OWNER" "$mode"
}

for t in "$DATA_DIR/.local" "$DATA_DIR/.gitconfig" "$DATA_DIR/plugin-packages-root"; do
  check_path "$t" || rc=1
done

# Bounded Cargo pool: root + policy.json only. Slot directories under the
# root must stay agent-writable; tightening them would break admission.
POOL="$DATA_DIR/.cache/two-bot-next-bounded"
if mountpoint -q "$POOL" 2>/dev/null || [[ -d "$POOL" && "$REQUIRE_POOL" == 1 ]]; then
  check_path "$POOL" || rc=1
  check_path "$POOL/policy.json" || rc=1
elif [[ "$REQUIRE_POOL" == 1 ]]; then
  printf 'FAIL: cargo pool required but missing or not a mountpoint: %s\n' "$POOL"
  rc=1
else
  printf 'skip (pool absent and not required): %s\n' "$POOL"
fi

# Effective access as the agent uid. Only test -w plus a throwaway create;
# never touch real files.
probe() {
  local path="$1"
  if "$DOCKER" exec -u "$AGENT_UID" "$SERVER_CONTAINER" sh -c "test -w '$path'" 2>/dev/null; then
    printf 'FAIL: agent uid can write %s\n' "$path"
    return 1
  fi
  printf 'ok (denied): %s\n' "$path"
}
for p in /paperclip/.local /paperclip/.local/bin /paperclip/.gitconfig \
         /paperclip/plugin-packages-root /opt/paperclip-plugin-packages \
         /paperclip/.cache/two-bot-next-bounded \
         /paperclip/.cache/two-bot-next-bounded/policy.json; do
  # The pool pair is probed only when the pool is in the baseline above.
  if [[ "$p" == /paperclip/.cache/* && "$REQUIRE_POOL" != 1 ]] && ! mountpoint -q "$POOL" 2>/dev/null; then
    printf 'skip (pool absent): probe %s\n' "$p"
    continue
  fi
  probe "$p" || rc=1
done
if "$DOCKER" exec -u "$AGENT_UID" "$SERVER_CONTAINER" sh -c 'echo x > /paperclip/.local/bin/.operator-probe 2>/dev/null'; then
  printf 'FAIL: agent uid created a file in /paperclip/.local/bin\n'
  "$DOCKER" exec "$SERVER_CONTAINER" rm -f /paperclip/.local/bin/.operator-probe
  rc=1
else
  printf 'ok (denied): create in /paperclip/.local/bin\n'
fi
exit "$rc"
