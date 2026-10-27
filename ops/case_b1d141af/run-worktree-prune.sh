#!/usr/bin/env bash
# run-worktree-prune.sh — the host clock for terminal_worktree_prune.py. (case_b1d141af.)
#
# Runs the pruner INSIDE the Paperclip server container, because the worktrees,
# the git repositories they belong to and the API the card/live-run gates read
# are all visible there and only there. The host cannot see the container's
# /paperclip paths, and the card gates need those exact paths.
#
# WHY THE MODE BIT LIVES HERE, ON ARGV. The sweep-auto drift was a unit
# labelled dry-run whose mode came from an EnvironmentFile meant for
# credentials. Here --apply is passed literally below, in reviewed bundle
# bytes no env file can supply. The env file can only set the VOLUME knob,
# PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE: 0 (the default) is observe-only, the
# full fresh evaluation with zero removals, provably incapable of a write.
#
# WHY THE CODE TRAVELS ON STDIN. Agents run inside that container as `node`
# and can write anywhere node can. A copy of the pruner left in the container
# would be mutable code executed with --apply. So this script streams the four
# reviewed modules from the root-owned, SHA256-checked host release into a
# private (0700) temp dir in the container, runs them there, and deletes them.
#
# Credentials travel by inherited environment, never on argv: /proc cmdline is
# world-readable. `podman exec -e NAME` (no value) forwards the variable named
# from THIS process's environment.
#
# WHY THE ALLOWLIST VERDICT IS RENDERED HERE, ON THE HOST. The pruner runs
# inside the container, where /etc/paperclip/allowed-api-hosts does not exist
# and the shared helper fails closed in every mode. So this wrapper checks the
# exact base it is about to forward against the host file BEFORE streaming
# (a missing file, malformed line, or enforce-mode mismatch refuses here,
# exit 2), then forwards the checked base verbatim as
# PAPERCLIP_API_ALLOWLIST_CHECKED_BASE. The in-container pruner honors that
# verdict only on exact string equality with the base it reads; anything else
# runs the normal check and fails closed. The value is always assigned here
# after a passing check, so an outer preset can never smuggle an unchecked
# base past the gate.
#
# Refusal contract: a missing container, credential or module, an invalid
# cap, or a host allowlist refusal aborts LOUDLY (exit 2) before anything
# runs. A run that measured nothing must never read as a clean bill of health.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
RELEASE_ROOT="${PAPERCLIP_WORKTREE_PRUNE_RELEASE_ROOT:-$(cd -- "$SCRIPT_DIR/../.." &>/dev/null && pwd)}"
TOOL_REL="ops/case_b1d141af/terminal_worktree_prune.py"
CORE_REL="ops/case_b5184419/terminal_worktree_cache_reclaim.py"
LIB_REL="ops/lib/atomic_state.py"
ALLOWLIST_REL="lib/paperclip_api_allowlist.py"
ALLOWLIST_SH_REL="lib/paperclip_api_allowlist.sh"
# The host runs the Paperclip server container under podman (README, runbooks)
# or, on some hosts, docker; both take the same `exec -i -u -e` argv used below.
# An explicit PAPERCLIP_WORKTREE_PRUNE_ENGINE wins; otherwise podman, then docker.
ENGINE="${PAPERCLIP_WORKTREE_PRUNE_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
  if [[ -x /usr/bin/podman ]]; then ENGINE=/usr/bin/podman; else ENGINE=/usr/bin/docker; fi
fi
# Backstop inside the container: an exec that outlives its client keeps running.
BOUND_SECONDS="${PAPERCLIP_WORKTREE_PRUNE_BOUND_SECONDS:-1500}"

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

[[ -f "$RELEASE_ROOT/$TOOL_REL" ]] || refuse "pruner module is missing: $RELEASE_ROOT/$TOOL_REL"
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

MAX_REMOVE="${PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE:-0}"
[[ "$MAX_REMOVE" =~ ^[0-9]+$ ]] \
  || refuse "PAPERCLIP_WORKTREE_PRUNE_MAX_REMOVE='$MAX_REMOVE' is not a non-negative integer"
[[ "$BOUND_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || refuse "PAPERCLIP_WORKTREE_PRUNE_BOUND_SECONDS='$BOUND_SECONDS' is not a positive integer"

# Host-side allowlist verdict (see header): the container cannot read the
# operator file, so the verdict is rendered here, on the exact forwarded base.
# Placed after the cheap local validations, still before anything runs.
source "$RELEASE_ROOT/$ALLOWLIST_SH_REL"
paperclip_allowlist_check "$PAPERCLIP_API_URL" "run-worktree-prune" \
  || refuse "API base refused by the host allowlist verdict (see log above)"
# The forwarded delegation is always the just-checked base; an outer preset of
# this variable is overwritten here and can never reach the container.
export PAPERCLIP_API_ALLOWLIST_CHECKED_BASE="$PAPERCLIP_API_URL"

# Runs in the container under `python3 -c`. Reads a tar of exactly the four
# reviewed modules from stdin, writes them into a private temp dir, runs the
# pruner there with the remaining argv, and always removes the dir. Anything
# else in the stream is refused: this is the only code path that turns bytes
# into code, so it accepts nothing it was not built to carry.
BOOTSTRAP='
import io, os, runpy, shutil, sys, tarfile, tempfile
want = {"ops/case_b1d141af/terminal_worktree_prune.py",
        "ops/case_b5184419/terminal_worktree_cache_reclaim.py",
        "ops/lib/atomic_state.py",
        "lib/paperclip_api_allowlist.py"}
data = sys.stdin.buffer.read()
root = tempfile.mkdtemp(prefix="wtprune-")
try:
    os.chmod(root, 0o700)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = tar.getmembers()
        if {m.name for m in members} != want or not all(m.isreg() for m in members):
            raise SystemExit("REFUSED: bundle members are not the reviewed quad")
        for m in members:
            dest = os.path.join(root, m.name)
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            with open(dest, "wb") as out:
                out.write(tar.extractfile(m).read())
    tool = os.path.join(root, "ops/case_b1d141af/terminal_worktree_prune.py")
    sys.argv = [tool] + sys.argv[1:]
    runpy.run_path(tool, run_name="__main__")
finally:
    shutil.rmtree(root, ignore_errors=True)
'

TOOL_ARGS=(
  --apply
  --max-remove "$MAX_REMOVE"
)

# The kill switch variable is forwarded only when the env file defines it
# (`-e NAME` with no value sets nothing when NAME is unset here).
ENV_ARGS=(-e PAPERCLIP_API_URL -e PAPERCLIP_API_KEY -e PAPERCLIP_COMPANY_ID -e PAPERCLIP_API_ALLOWLIST_CHECKED_BASE)
[[ -n "${WORKTREE_PRUNE_DISABLED:-}" ]] && ENV_ARGS+=(-e WORKTREE_PRUNE_DISABLED)

# A deterministic tar (sorted, fixed owner and mtime) of the four modules only.
# stdin of `podman exec -i` is that tar; the pruner reads no other stdin.
tar --create --format=ustar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime='@0' --directory "$RELEASE_ROOT" "$ALLOWLIST_REL" "$CORE_REL" "$TOOL_REL" "$LIB_REL" \
  | "$ENGINE" exec -i -u node "${ENV_ARGS[@]}" "$PAPERCLIP_WORKTREE_PRUNE_CONTAINER" \
      timeout --signal=TERM --kill-after=30s "$BOUND_SECONDS" \
      python3 -c "$BOOTSTRAP" "${TOOL_ARGS[@]}"
