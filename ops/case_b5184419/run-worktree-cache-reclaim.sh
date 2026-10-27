#!/usr/bin/env bash
# run-worktree-cache-reclaim.sh — the host clock for terminal_worktree_cache_reclaim.py.
#
# Sibling of ops/case_b1d141af/run-worktree-prune.sh and built the same way; read
# its header for the reasoning. Short version of what is shared:
# - The reclaim runs INSIDE the Paperclip server container (the worktrees, their
#   git repositories and the API gates are visible there only), as `node`, from a
#   deterministic tar of reviewed host bytes streamed into a private 0700 temp dir
#   that is deleted afterwards. Nothing executable is left in the container.
# - The allowlist verdict is rendered HERE, on the host, against
#   /etc/paperclip/allowed-api-hosts for the exact base being forwarded, and the
#   checked base is forwarded as PAPERCLIP_API_ALLOWLIST_CHECKED_BASE. The
#   container has no /etc/paperclip, so the in-container gate fails closed on its
#   own. The value is always assigned here after a passing check, so
#   an outer preset can never smuggle an unchecked base past the gate.
# - Credentials travel by inherited environment (`podman exec -e NAME`), never on
#   argv.
#
# What differs from the pruner:
# - APPLY OR DRY-RUN IS THE REGISTRY'S DECISION, not this script's and not the
#   environment's: argv carries `--registry-gate routine-worktree-cache-reclaim`
#   and the tool applies only while that entry is an active writer of
#   fs:worktree-dep-caches; a detector/paused entry or an unreadable registry is a
#   dry-run. `--apply` never appears in this file. The tool is the only deleter
#   and only ever removes ignored, untracked dep caches of terminal-card
#   worktrees (cargo target/ is excluded by name).
# - The reviewed registry is a private file that does not ship in this repository.
#   The operator installs it (root-owned, from private main at an exact SHA) at
#   PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY (default
#   /etc/paperclip/host-timer-registry.json). It is streamed in the same tar and
#   handed to the tool with --registry. A missing registry refuses here (exit 2).
# - One optional argument, `--dry-run`, drops the gate and runs the tool with no
#   mode flags at all (its default is dry-run): a way to take a report without any
#   chance of a deletion. It can only reduce what the run may do.
#
# Environment (same env file as the pruner, /etc/paperclip/worktree-prune.env):
#   PAPERCLIP_WORKTREE_PRUNE_CONTAINER, PAPERCLIP_API_URL, PAPERCLIP_API_KEY,
#   PAPERCLIP_COMPANY_ID (required); PAPERCLIP_WORKTREE_PRUNE_ENGINE and
#   PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT (optional, as for the pruner);
#   PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY and
#   PAPERCLIP_WORKTREE_CACHE_RECLAIM_BOUND_SECONDS (optional).
#
# Refusal contract: a missing container, credential, module or registry, an
# invalid bound, an unknown argument or a host allowlist refusal aborts LOUDLY
# (exit 2) before anything runs.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
RELEASE_ROOT="${PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT:-$(cd -- "$SCRIPT_DIR/../.." &>/dev/null && pwd)}"
CORE_REL="ops/case_b5184419/terminal_worktree_cache_reclaim.py"
LIB_REL="ops/lib/atomic_state.py"
ALLOWLIST_REL="lib/paperclip_api_allowlist.py"
ALLOWLIST_SH_REL="lib/paperclip_api_allowlist.sh"
REGISTRY="${PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY:-/etc/paperclip/host-timer-registry.json}"
REGISTRY_MEMBER="registry/host-timer-registry.json"
REGISTRY_GATE_ID="routine-worktree-cache-reclaim"
OUT_DIR="/paperclip/instances/default/data/worktree-cache-reclaim"
ENGINE="${PAPERCLIP_WORKTREE_PRUNE_ENGINE:-/usr/bin/podman}"
# Backstop inside the container: an exec that outlives its client keeps running.
BOUND_SECONDS="${PAPERCLIP_WORKTREE_CACHE_RECLAIM_BOUND_SECONDS:-1500}"

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

DRY_RUN_ONLY=0
case "$#" in
  0) ;;
  1) [[ "$1" == "--dry-run" ]] || refuse "unknown argument '$1' (only --dry-run is accepted)"; DRY_RUN_ONLY=1 ;;
  *) refuse "too many arguments (only an optional --dry-run is accepted)" ;;
esac

[[ -f "$RELEASE_ROOT/$CORE_REL" ]] || refuse "reclaim core is missing: $RELEASE_ROOT/$CORE_REL"
[[ -f "$RELEASE_ROOT/$LIB_REL" ]] || refuse "atomic-state lib is missing: $RELEASE_ROOT/$LIB_REL"
[[ -f "$RELEASE_ROOT/$ALLOWLIST_REL" ]] || refuse "allowlist lib is missing: $RELEASE_ROOT/$ALLOWLIST_REL"
[[ -f "$RELEASE_ROOT/$ALLOWLIST_SH_REL" ]] || refuse "shell allowlist lib is missing: $RELEASE_ROOT/$ALLOWLIST_SH_REL"
[[ -x "$ENGINE" ]] || refuse "container engine is not executable: $ENGINE"
[[ -n "${PAPERCLIP_WORKTREE_PRUNE_CONTAINER:-}" ]] \
  || refuse "PAPERCLIP_WORKTREE_PRUNE_CONTAINER is unset (env file not loaded?)"
[[ "$PAPERCLIP_WORKTREE_PRUNE_CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
  || refuse "PAPERCLIP_WORKTREE_PRUNE_CONTAINER is not a plain container name"
[[ -n "${PAPERCLIP_API_URL:-}" ]] || refuse "PAPERCLIP_API_URL is unset (env file not loaded?)"
[[ -n "${PAPERCLIP_API_KEY:-}" ]] || refuse "PAPERCLIP_API_KEY is unset (env file not loaded?)"
[[ -n "${PAPERCLIP_COMPANY_ID:-}" ]] || refuse "PAPERCLIP_COMPANY_ID is unset (env file not loaded?)"
[[ "$BOUND_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || refuse "PAPERCLIP_WORKTREE_CACHE_RECLAIM_BOUND_SECONDS='$BOUND_SECONDS' is not a positive integer"
# The registry is only consulted by the gated run, but a host that cannot supply
# it is misconfigured: refuse in both modes so a dry-run proves the real setup.
[[ "$(basename -- "$REGISTRY")" == "host-timer-registry.json" ]] \
  || refuse "PAPERCLIP_WORKTREE_CACHE_RECLAIM_REGISTRY must name a file called host-timer-registry.json"
[[ -f "$REGISTRY" && -r "$REGISTRY" ]] \
  || refuse "reviewed registry is missing or unreadable: $REGISTRY"

# Host-side allowlist verdict (see header): the container cannot read the
# operator file, so the verdict is rendered here, on the exact forwarded base.
source "$RELEASE_ROOT/$ALLOWLIST_SH_REL"
paperclip_allowlist_check "$PAPERCLIP_API_URL" "run-worktree-cache-reclaim" \
  || refuse "API base refused by the host allowlist verdict (see log above)"
# The forwarded delegation is always the just-checked base; an outer preset of
# this variable is overwritten here and can never reach the container.
export PAPERCLIP_API_ALLOWLIST_CHECKED_BASE="$PAPERCLIP_API_URL"

# Runs in the container under `python3 -c`. Reads a tar of exactly the four
# reviewed members from stdin, writes them into a private temp dir, runs the
# reclaim there with the remaining argv ("@ROOT@" is replaced by the temp dir),
# and always removes the dir. Anything else in the stream is refused.
BOOTSTRAP='
import io, os, runpy, shutil, sys, tarfile, tempfile
want = {"ops/case_b5184419/terminal_worktree_cache_reclaim.py",
        "ops/lib/atomic_state.py",
        "lib/paperclip_api_allowlist.py",
        "registry/host-timer-registry.json"}
data = sys.stdin.buffer.read()
root = tempfile.mkdtemp(prefix="wtreclaim-")
try:
    os.chmod(root, 0o700)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = tar.getmembers()
        if {m.name for m in members} != want or not all(m.isreg() for m in members):
            raise SystemExit("REFUSED: bundle members are not the reviewed set")
        for m in members:
            dest = os.path.join(root, m.name)
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            with open(dest, "wb") as out:
                out.write(tar.extractfile(m).read())
    tool = os.path.join(root, "ops/case_b5184419/terminal_worktree_cache_reclaim.py")
    sys.argv = [tool] + [a.replace("@ROOT@", root) for a in sys.argv[1:]]
    runpy.run_path(tool, run_name="__main__")
finally:
    shutil.rmtree(root, ignore_errors=True)
'

if [[ $DRY_RUN_ONLY -eq 1 ]]; then
  TOOL_ARGS=(--out-dir "$OUT_DIR")
else
  TOOL_ARGS=(--registry-gate "$REGISTRY_GATE_ID" --registry "@ROOT@/$REGISTRY_MEMBER" --out-dir "$OUT_DIR")
fi

ENV_ARGS=(-e PAPERCLIP_API_URL -e PAPERCLIP_API_KEY -e PAPERCLIP_COMPANY_ID -e PAPERCLIP_API_ALLOWLIST_CHECKED_BASE)

# A deterministic tar (sorted, fixed owner and mtime) of the reviewed members
# only; the registry is renamed to its fixed member name. stdin of
# `podman exec -i` is that tar; the tool reads no other stdin.
tar --create --format=ustar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime='@0' \
    --transform="s,^host-timer-registry\.json\$,$REGISTRY_MEMBER," \
    --directory "$RELEASE_ROOT" "$ALLOWLIST_REL" "$CORE_REL" "$LIB_REL" \
    --directory "$(dirname -- "$REGISTRY")" host-timer-registry.json \
  | "$ENGINE" exec -i -u node "${ENV_ARGS[@]}" "$PAPERCLIP_WORKTREE_PRUNE_CONTAINER" \
      timeout --signal=TERM --kill-after=30s "$BOUND_SECONDS" \
      python3 -c "$BOOTSTRAP" "${TOOL_ARGS[@]}"
