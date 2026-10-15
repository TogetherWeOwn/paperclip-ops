#!/usr/bin/env bash
set -Eeuo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

SOURCE_REF=""
BUNDLE=""
TARGET_USER="${SUDO_USER:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-ref) SOURCE_REF="${2:-}"; shift 2;;
    --bundle) BUNDLE="${2:-}"; shift 2;;
    --target-user) TARGET_USER="${2:-}"; shift 2;;
    *) refuse "unknown argument: $1";;
  esac
done

[[ $EUID -eq 0 ]] || refuse "installer must run as root"
[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]] || refuse "--source-ref must be a 40-hex commit"
[[ -n "$BUNDLE" && -f "$BUNDLE" ]] || refuse "--bundle must name the reviewed release tar"
[[ -n "$TARGET_USER" && "$TARGET_USER" != root ]] || refuse "--target-user must name the non-root user that owns the user service"
for executable in /usr/bin/install /usr/bin/sha256sum /usr/bin/mktemp /usr/bin/tar /usr/bin/python3 /usr/bin/getent /usr/bin/cut /usr/bin/test /usr/bin/timeout; do
  [[ -x "$executable" ]] || refuse "required executable missing: $executable"
done
TARGET_UID=$(/usr/bin/getent passwd "$TARGET_USER" | /usr/bin/cut -d: -f3)
[[ "$TARGET_UID" =~ ^[0-9]+$ ]] || refuse "cannot resolve uid for --target-user"

WORK_DIR=$(/usr/bin/mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
/usr/bin/tar --extract --file "$BUNDLE" --directory "$WORK_DIR" --no-same-owner --no-same-permissions
[[ "$(<"$WORK_DIR/REVISION")" == "$SOURCE_REF" ]] || refuse "bundle revision does not match --source-ref"
for source in recovery_writer.py ops/lib/host_pressure.py systemd/run-recovery-writer.sh systemd/install-recovery-writer.sh systemd/paperclip-recovery-writer.service systemd/paperclip-recovery-writer.timer SHA256SUMS REVISION; do
  [[ -f "$WORK_DIR/$source" ]] || refuse "reviewed bundle is missing $source"
done
(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)
RELEASE_DIR="/usr/local/libexec/paperclip-recovery-writer/$SOURCE_REF"
if [[ -e "$RELEASE_DIR" ]]; then
  refuse "release directory already exists: $RELEASE_DIR"
fi
/usr/bin/install -d -o root -g root -m 0755 "$RELEASE_DIR" "$RELEASE_DIR/ops/lib" /etc/systemd/user
/usr/bin/install -o root -g root -m 0555 "$WORK_DIR/recovery_writer.py" "$RELEASE_DIR/recovery_writer.py"
/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/ops/lib/host_pressure.py" "$RELEASE_DIR/ops/lib/host_pressure.py"
/usr/bin/install -o root -g root -m 0555 "$WORK_DIR/systemd/run-recovery-writer.sh" "$RELEASE_DIR/run-recovery-writer.sh"
# The bundle manifest covers bundle-scope paths (systemd/*) that are verified
# in WORK_DIR above but never installed. Copying it into the release directory
# would make the unit's ExecStartPre `sha256sum --check` fail on every start
# with FAILED-open-or-read. The release manifest covers exactly the installed
# byte set, regenerated here from the verified bytes.
/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/REVISION"
(cd "$RELEASE_DIR" && /usr/bin/sha256sum recovery_writer.py ops/lib/host_pressure.py run-recovery-writer.sh REVISION > "$WORK_DIR/SHA256SUMS.release")
/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/SHA256SUMS.release" "$RELEASE_DIR/SHA256SUMS"
/usr/bin/python3 -m py_compile "$RELEASE_DIR/recovery_writer.py" "$RELEASE_DIR/ops/lib/host_pressure.py"
(cd "$RELEASE_DIR" && /usr/bin/python3 recovery_writer.py --help >/dev/null)
rm -rf "$RELEASE_DIR/__pycache__" "$RELEASE_DIR/ops/lib/__pycache__"
# The unit runs this exact check before every start; prove it passes now,
# before claiming INSTALLED, rather than at the first timer tick.
(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)

/usr/bin/python3 - "$WORK_DIR/systemd/paperclip-recovery-writer.service" "$SOURCE_REF" > "$WORK_DIR/rendered.service" <<'PY'
import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text()
rendered = source.replace("@SOURCE_REF@", sys.argv[2])
if "@SOURCE_REF@" in rendered:
    raise SystemExit("unrendered source-ref placeholder")
print(rendered, end="")
PY
/usr/bin/install -o root -g root -m 0644 "$WORK_DIR/rendered.service" /etc/systemd/user/paperclip-recovery-writer.service
/usr/bin/install -o root -g root -m 0644 "$WORK_DIR/systemd/paperclip-recovery-writer.timer" /etc/systemd/user/paperclip-recovery-writer.timer
printf 'INSTALLED paperclip-recovery-writer from %s for user %s (uid %s)\n' "$SOURCE_REF" "$TARGET_USER" "$TARGET_UID"
printf 'next: as %s, run systemctl --user daemon-reload and enable the timer\n' "$TARGET_USER"
printf 'undo: as %s, disable the timer and stop the service; then remove installed unit files and release after retaining evidence\n' "$TARGET_USER"
