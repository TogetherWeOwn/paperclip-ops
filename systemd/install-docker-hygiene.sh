#!/usr/bin/env bash
# Installs the Docker hygiene release (case_b0e822f6) from the reviewed bundle
# built by build-docker-hygiene-bundle.sh. Installs the units, the payload
# and a nonsecret env template; it does NOT enable the timer and does NOT
# prune anything. The runbook requires one manual dry-run first.
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
case "${DOCKER_HYGIENE_INSTALL_TEST_MODE:-0}" in
  0)
    [[ $EUID -eq 0 ]] || refuse "installer must run as root"
    [[ -z "${DOCKER_HYGIENE_INSTALL_ROOT:-}" ]] || refuse "DOCKER_HYGIENE_INSTALL_ROOT is test-only"
    DEST=""
    OWNER=(-o root -g root)
    ;;
  1)
    DEST="${DOCKER_HYGIENE_INSTALL_ROOT:?set DOCKER_HYGIENE_INSTALL_ROOT in test mode}"
    [[ "$DEST" == /* && "$DEST" != / ]] || refuse "test install root must be an absolute non-root path"
    OWNER=()
    ;;
  *) refuse "DOCKER_HYGIENE_INSTALL_TEST_MODE must be 0 or 1";;
esac
[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]] || refuse "--source-ref must be a 40-hex commit"
[[ -n "$BUNDLE" && -f "$BUNDLE" ]] || refuse "--bundle must name the reviewed release tar"
for executable in /usr/bin/install /usr/bin/sha256sum /usr/bin/mktemp /usr/bin/stat /usr/bin/tar /usr/bin/bash; do
  [[ -x "$executable" ]] || refuse "required executable missing: $executable"
done

WORK_DIR=$(/usr/bin/mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
/usr/bin/tar --extract --file "$BUNDLE" --directory "$WORK_DIR" --no-same-owner --no-same-permissions
[[ "$(<"$WORK_DIR/REVISION")" == "$SOURCE_REF" ]] || refuse "bundle revision does not match --source-ref"
(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)
for source in docker_hygiene.sh systemd/install-docker-hygiene.sh systemd/paperclip-docker-hygiene.service systemd/paperclip-docker-hygiene.timer systemd/docker-hygiene.env.example; do
  [[ -f "$WORK_DIR/$source" ]] || refuse "reviewed bundle is missing $source"
done
# Preflight the reviewed bytes before anything is installed.
/usr/bin/bash -n "$WORK_DIR/docker_hygiene.sh" || refuse "shell syntax preflight failed"

# This payload carries no credential, so its env template is nonsecret: a
# present env file is kept byte-identical (operator knobs win), a missing one
# is created 0644 root-owned from the reviewed template.
ENV_FILE="$DEST/etc/paperclip/docker-hygiene.env"
if [[ -e "$ENV_FILE" ]]; then
  if [[ -z "$DEST" ]]; then
    [[ "$(/usr/bin/stat -c %u "$ENV_FILE")" == 0 ]] || refuse "$ENV_FILE is not owned by root"
  fi
else
  /usr/bin/install "${OWNER[@]}" -m 0644 -D "$WORK_DIR/systemd/docker-hygiene.env.example" "$ENV_FILE"
fi

RELEASE_DIR="$DEST/usr/local/libexec/paperclip-docker-hygiene/$SOURCE_REF"
[[ ! -e "$RELEASE_DIR" ]] || refuse "release directory already exists: $RELEASE_DIR"
/usr/bin/install -d "${OWNER[@]}" -m 0755 \
  "$RELEASE_DIR" \
  "$DEST/etc/systemd/system"
/usr/bin/install "${OWNER[@]}" -m 0555 "$WORK_DIR/docker_hygiene.sh" "$RELEASE_DIR/"
/usr/bin/install "${OWNER[@]}" -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/"
# The bundle manifest covers bundle-scope paths (systemd/*) that are verified
# in WORK_DIR above but never installed. Copying it into the release directory
# would make the unit's ExecStartPre `sha256sum --check` fail on every start.
# The release manifest covers exactly the installed byte set.
(cd "$RELEASE_DIR" && /usr/bin/sha256sum docker_hygiene.sh REVISION > "$WORK_DIR/SHA256SUMS.release")
/usr/bin/install "${OWNER[@]}" -m 0444 "$WORK_DIR/SHA256SUMS.release" "$RELEASE_DIR/SHA256SUMS"
# The unit runs this exact check before every start; prove it passes now.
(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)

/usr/bin/python3 - "$WORK_DIR/systemd/paperclip-docker-hygiene.service" "$SOURCE_REF" > "$WORK_DIR/rendered.service" <<'PY'
import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text()
rendered = source.replace("@SOURCE_REF@", sys.argv[2])
if "@SOURCE_REF@" in rendered:
    raise SystemExit("unrendered source-ref placeholder")
print(rendered, end="")
PY
/usr/bin/install "${OWNER[@]}" -m 0644 "$WORK_DIR/rendered.service" "$DEST/etc/systemd/system/paperclip-docker-hygiene.service"
/usr/bin/install "${OWNER[@]}" -m 0644 "$WORK_DIR/systemd/paperclip-docker-hygiene.timer" "$DEST/etc/systemd/system/paperclip-docker-hygiene.timer"
printf 'INSTALLED paperclip-docker-hygiene from %s\n' "$SOURCE_REF"
printf 'next: run the service ONCE by hand WITHOUT the timer (systemctl start paperclip-docker-hygiene.service) in a host window — the unit always runs --apply — then read the journal report and enable --now the timer\n'
printf 'undo: systemctl disable --now paperclip-docker-hygiene.timer; remove the units and this release after retaining evidence. Only dangling (untagged) images and regenerable build cache are ever pruned; tagged rollback images are never candidates.\n'
