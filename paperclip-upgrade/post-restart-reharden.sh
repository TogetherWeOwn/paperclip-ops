#!/usr/bin/env bash
# ===========================================================================
# paperclip-upgrade/post-restart-reharden.sh — re-apply agent-immutability
# ownership after a Paperclip container restart (case_893fabe5).
#
# PORT NOTES (from the live host post-restart-reharden.sh):
#   - Same hardening set: $DATA/.local, $DATA/.gitconfig,
#     $DATA/plugin-packages-root (recursive real-root), plus the bounded
#     two-bot-next Cargo pool root + policy.json NON-recursive (slot dirs
#     stay agent-writable or every Cargo admission fails).
#   - Verification probes run as the agent uid via `docker exec -u 1000`:
#     every hardened path must DENY writes, plus a throwaway-create probe
#     that is removed on success. Any writable path exits 1.
#   - Refuses (exit 2) on missing DATA dir or missing passwordless sudo
#     instead of half-hardening and reporting success.
#
# Exit status: 0 hardened and verified | 1 a probe failed | 2 refused
# ===========================================================================
set -uo pipefail

ME="${BASH_SOURCE[0]##*/}"
DATA_DIR="${PAPERCLIP_DATA:-$HOME/stacks/paperclip/data}"
SERVER_CONTAINER="${PAPERCLIP_UPGRADE_SERVER_CONTAINER:-paperclip}"
AGENT_UID="${PAPERCLIP_UPGRADE_AGENT_UID:-1000}"
DOCKER="${DOCKER:-docker}"

[[ -d "$DATA_DIR" ]] || { printf 'REFUSED: %s: data dir missing: %s\n' "$ME" "$DATA_DIR" >&2; exit 2; }
command -v "$DOCKER" >/dev/null || { printf 'REFUSED: %s: docker runtime is missing\n' "$ME" >&2; exit 2; }
sudo -n true 2>/dev/null || { printf 'REFUSED: %s: passwordless sudo is required for chown/chmod\n' "$ME" >&2; exit 2; }

targets=(
  "$DATA_DIR/.local"
  "$DATA_DIR/.gitconfig"
  "$DATA_DIR/plugin-packages-root"
)

for t in "${targets[@]}"; do
  if [[ ! -e "$t" ]]; then
    echo "skip (missing): $t"
    continue
  fi
  sudo -n chown -R --no-dereference 0:0 "$t"
  sudo -n chmod -R go-w "$t"
  echo "hardened: $t"
done

# Bounded two-bot-next Cargo pool: root + policy.json NON-recursive only.
POOL="$DATA_DIR/.cache/two-bot-next-bounded"
if sudo -n mountpoint -q "$POOL" 2>/dev/null; then
  sudo -n chown --no-dereference 0:0 "$POOL" "$POOL/policy.json"
  sudo -n chmod 0755 "$POOL"
  sudo -n chmod 0644 "$POOL/policy.json"
  echo "hardened (non-recursive): $POOL + policy.json"
else
  echo "skip (pool not mounted): $POOL"
fi

# Verify as the agent uid. Only test -w plus a throwaway create; never touch
# real files.
rc=0
probe() {
  local path="$1"
  if "$DOCKER" exec -u "$AGENT_UID" "$SERVER_CONTAINER" sh -c "test -w '$path'" 2>/dev/null; then
    echo "FAIL: agent uid can write $path"
    return 1
  fi
  echo "ok (denied): $path"
}
for p in /paperclip/.local /paperclip/.local/bin /paperclip/.gitconfig /paperclip/plugin-packages-root /opt/paperclip-plugin-packages /paperclip/.cache/two-bot-next-bounded /paperclip/.cache/two-bot-next-bounded/policy.json; do
  probe "$p" || rc=1
done
if "$DOCKER" exec -u "$AGENT_UID" "$SERVER_CONTAINER" sh -c 'echo x > /paperclip/.local/bin/.operator-probe 2>/dev/null'; then
  echo "FAIL: agent uid created a file in /paperclip/.local/bin"
  "$DOCKER" exec "$SERVER_CONTAINER" rm -f /paperclip/.local/bin/.operator-probe
  rc=1
else
  echo "ok (denied): create in /paperclip/.local/bin"
fi
exit "$rc"
