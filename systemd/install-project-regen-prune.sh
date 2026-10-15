#!/usr/bin/env bash
# Installs the project regen pruner release (case_60e07cc2) from the reviewed
# bundle built by build-project-regen-prune-bundle.sh. Installs the units and
# a nonsecret env template; it does NOT enable the timer and does NOT run the
# pruner. Disabling the previous host user timer (project-worktree-prune) is
# a separate Operator step in the runbook: installing both timers would prune
# twice per hour (idempotent but wasteful), so the old one stays until the
# new unit has produced its first cap-0 report.
set -Eeuo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

SOURCE_REF=""
BUNDLE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-ref) SOURCE_REF="${2:-}"; shift 2;;
    --bundle) BUNDLE="${2:-}"; shift 2;;
    *) refuse "unknown argument: $1";;
  esac
done

# A test-only install root lets the offline suite run this exact script without
# root. In production mode the override is refused outright, so it can never
# redirect a real install.
case "${WORKTREE_PRUNE_INSTALL_TEST_MODE:-0}" in
  0)
    [[ $EUID -eq 0 ]] || refuse "installer must run as root"
    [[ -z "${WORKTREE_PRUNE_INSTALL_ROOT:-}" ]] || refuse "WORKTREE_PRUNE_INSTALL_ROOT is test-only"
    DEST=""
    OWNER=(-o root -g root)
    STATE_OWNER=(-o ubuntu -g ubuntu)
    ;;
  1)
    DEST="${WORKTREE_PRUNE_INSTALL_ROOT:?set WORKTREE_PRUNE_INSTALL_ROOT in test mode}"
    [[ "$DEST" == /* && "$DEST" != / ]] || refuse "test install root must be an absolute non-root path"
    OWNER=()
    STATE_OWNER=()
    ;;
  *) refuse "WORKTREE_PRUNE_INSTALL_TEST_MODE must be 0 or 1";;
esac
[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]] || refuse "--source-ref must be a 40-hex commit"
[[ -n "$BUNDLE" && -f "$BUNDLE" ]] || refuse "--bundle must name the reviewed release tar"
for executable in /usr/bin/install /usr/bin/sha256sum /usr/bin/mktemp /usr/bin/stat /usr/bin/tar /usr/bin/python3 /usr/bin/bash; do
  [[ -x "$executable" ]] || refuse "required executable missing: $executable"
done

WORK_DIR=$(/usr/bin/mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
/usr/bin/tar --extract --file "$BUNDLE" --directory "$WORK_DIR" --no-same-owner --no-same-permissions
[[ "$(<"$WORK_DIR/REVISION")" == "$SOURCE_REF" ]] || refuse "bundle revision does not match --source-ref"
(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)
for source in ops/case_60e07cc2/project_worktree_regen_prune.py ops/case_b5184419/terminal_worktree_cache_reclaim.py ops/lib/atomic_state.py systemd/install-project-regen-prune.sh systemd/paperclip-project-regen-prune.service systemd/paperclip-project-regen-prune.timer systemd/project-regen-prune.env.example; do
  [[ -f "$WORK_DIR/$source" ]] || refuse "reviewed bundle is missing $source"
done
# Preflight the reviewed bytes before anything is installed.
/usr/bin/python3 - "$WORK_DIR/ops/lib/atomic_state.py" "$WORK_DIR/ops/case_60e07cc2/project_worktree_regen_prune.py" "$WORK_DIR/ops/case_b5184419/terminal_worktree_cache_reclaim.py" <<'PY' || refuse "Python syntax preflight failed"
import pathlib, sys
for path in sys.argv[1:]:
    compile(pathlib.Path(path).read_bytes(), path, "exec")
PY

# An env file the operator already provisioned is never rewritten, but it carries
# the API key and the API URL, so a loose mode or a non-root owner is refused
# BEFORE any release byte is installed.
ENV_FILE="$DEST/etc/paperclip/project-regen-prune.env"
if [[ -e "$ENV_FILE" ]]; then
  env_mode=$(/usr/bin/stat -c %a "$ENV_FILE")
  [[ "$env_mode" == 600 ]] || refuse "$ENV_FILE mode is $env_mode, expected 600"
  if [[ -z "$DEST" ]]; then
    [[ "$(/usr/bin/stat -c %u "$ENV_FILE")" == 0 ]] || refuse "$ENV_FILE is not owned by root"
  fi
fi

RELEASE_DIR="$DEST/usr/local/libexec/paperclip-project-regen-prune/$SOURCE_REF"
[[ ! -e "$RELEASE_DIR" ]] || refuse "release directory already exists: $RELEASE_DIR"
/usr/bin/install -d "${OWNER[@]}" -m 0755 \
  "$RELEASE_DIR" "$RELEASE_DIR/ops" "$RELEASE_DIR/ops/lib" "$RELEASE_DIR/ops/case_b5184419" "$RELEASE_DIR/ops/case_60e07cc2" \
  "$DEST/etc/paperclip" "$DEST/etc/systemd/system"
/usr/bin/install "${OWNER[@]}" -m 0444 "$WORK_DIR/ops/lib/atomic_state.py" "$RELEASE_DIR/ops/lib/"
/usr/bin/install "${OWNER[@]}" -m 0444 "$WORK_DIR/ops/case_b5184419/terminal_worktree_cache_reclaim.py" "$RELEASE_DIR/ops/case_b5184419/"
/usr/bin/install "${OWNER[@]}" -m 0444 "$WORK_DIR/ops/case_60e07cc2/project_worktree_regen_prune.py" "$RELEASE_DIR/ops/case_60e07cc2/"
/usr/bin/install "${OWNER[@]}" -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/"
# The bundle manifest covers bundle-scope paths (systemd/*) that are verified in
# WORK_DIR above but never installed. Copying it into the release directory
# would make the unit's ExecStartPre `sha256sum --check` fail on every start.
# The release manifest covers exactly the installed byte set.
(cd "$RELEASE_DIR" && /usr/bin/sha256sum ops/lib/atomic_state.py ops/case_b5184419/terminal_worktree_cache_reclaim.py ops/case_60e07cc2/project_worktree_regen_prune.py REVISION > "$WORK_DIR/SHA256SUMS.release")
/usr/bin/install "${OWNER[@]}" -m 0444 "$WORK_DIR/SHA256SUMS.release" "$RELEASE_DIR/SHA256SUMS"
# The unit runs this exact check before every start; prove it passes now.
(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)

# The service runs as ubuntu and writes manifests here; the directory must
# exist and be ubuntu-writable before the first firing.
STATE_DIR="$DEST/var/lib/paperclip/project-regen-prune"
/usr/bin/install -d "${STATE_OWNER[@]}" -m 0755 "$STATE_DIR"

/usr/bin/python3 - "$WORK_DIR/systemd/paperclip-project-regen-prune.service" "$SOURCE_REF" > "$WORK_DIR/rendered.service" <<'PY'
import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text()
rendered = source.replace("@SOURCE_REF@", sys.argv[2])
if "@SOURCE_REF@" in rendered:
    raise SystemExit("unrendered source-ref placeholder")
print(rendered, end="")
PY
/usr/bin/install "${OWNER[@]}" -m 0644 "$WORK_DIR/rendered.service" "$DEST/etc/systemd/system/paperclip-project-regen-prune.service"
/usr/bin/install "${OWNER[@]}" -m 0644 "$WORK_DIR/systemd/paperclip-project-regen-prune.timer" "$DEST/etc/systemd/system/paperclip-project-regen-prune.timer"

if [[ ! -e "$ENV_FILE" ]]; then
  /usr/bin/install "${OWNER[@]}" -m 0600 "$WORK_DIR/systemd/project-regen-prune.env.example" "$ENV_FILE"
  printf 'CREATED nonsecret template %s (API key is a placeholder)\n' "$ENV_FILE"
else
  printf 'PRESERVED existing %s\n' "$ENV_FILE"
fi
printf 'INSTALLED paperclip-project-regen-prune from %s\n' "$SOURCE_REF"
printf 'next: set PAPERCLIP_API_KEY in %s (read-capable service credential, never on argv), systemctl daemon-reload, run the service ONCE by hand at cap 0 and read the report, then enable --now the timer and disable the old user timer (see docs/runbooks/project-regen-prune.md)\n' "$ENV_FILE"
printf 'undo: systemctl disable --now paperclip-project-regen-prune.timer; remove the units and this release after retaining evidence. Nothing here deletes a regen dir: removals happen only in a run with PROJECT_REGEN_PRUNE_MAX_REMOVE > 0, and every removed dir rebuilds via its package manager.\n'
