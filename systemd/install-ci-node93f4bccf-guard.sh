#!/usr/bin/env bash
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

[[ $EUID -eq 0 ]] || refuse "installer must run as root"
[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]] || refuse "--source-ref must be a 40-hex commit"
[[ -n "$BUNDLE" && -f "$BUNDLE" ]] || refuse "--bundle must name the reviewed release tar"
# systemd-creds is deliberately NOT required here: no shipped file invokes
# it (the operator runs it by hand to provision the encrypted credential),
# and a missing credential fails the first tick loudly in run-ci-node93f4bccf-guard.sh,
# never as a silent skip. Requiring it would turn a provisioning-workstation
# difference into a failed install (the case_d2b472ea lesson).
for executable in /usr/bin/install /usr/bin/sha256sum /usr/bin/mktemp /usr/bin/tar /usr/bin/python3 /usr/bin/bash /usr/bin/flock /usr/bin/timeout /usr/bin/grep; do
  [[ -x "$executable" ]] || refuse "required executable missing: $executable"
done

WORK_DIR=$(/usr/bin/mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
/usr/bin/tar --extract --file "$BUNDLE" --directory "$WORK_DIR" --no-same-owner --no-same-permissions
[[ "$(<"$WORK_DIR/REVISION")" == "$SOURCE_REF" ]] || refuse "bundle revision does not match --source-ref"
(cd "$WORK_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)
for source in ci_node93f4bccf_guard.py test_ci_node93f4bccf_guard.py systemd/run-ci-node93f4bccf-guard.sh systemd/install-ci-node93f4bccf-guard.sh systemd/paperclip-ci-node93f4bccf-guard.service systemd/paperclip-ci-node93f4bccf-guard.timer systemd/ci-node93f4bccf-guard.env.example; do
  [[ -f "$WORK_DIR/$source" ]] || refuse "reviewed bundle is missing $source"
done

RELEASE_DIR="/usr/local/libexec/paperclip-ci-node93f4bccf-guard/$SOURCE_REF"
[[ ! -e "$RELEASE_DIR" ]] || refuse "release directory already exists: $RELEASE_DIR"
/usr/bin/install -d -o root -g root -m 0755 "$RELEASE_DIR" /etc/paperclip /var/lib/paperclip/ci-node93f4bccf-guard
/usr/bin/install -d -o root -g root -m 0700 /etc/credstore.encrypted
/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/ci_node93f4bccf_guard.py" "$RELEASE_DIR/"
/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/test_ci_node93f4bccf_guard.py" "$RELEASE_DIR/"
/usr/bin/install -o root -g root -m 0555 "$WORK_DIR/systemd/run-ci-node93f4bccf-guard.sh" "$RELEASE_DIR/"
/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/REVISION" "$RELEASE_DIR/"
(cd "$RELEASE_DIR" && /usr/bin/sha256sum ci_node93f4bccf_guard.py test_ci_node93f4bccf_guard.py run-ci-node93f4bccf-guard.sh REVISION > "$WORK_DIR/SHA256SUMS.release")
/usr/bin/install -o root -g root -m 0444 "$WORK_DIR/SHA256SUMS.release" "$RELEASE_DIR/SHA256SUMS"
/usr/bin/python3 -m py_compile "$RELEASE_DIR/ci_node93f4bccf_guard.py" "$RELEASE_DIR/test_ci_node93f4bccf_guard.py"
rm -rf "$RELEASE_DIR/__pycache__"
(cd "$RELEASE_DIR" && /usr/bin/sha256sum --check --strict SHA256SUMS)

/usr/bin/python3 - "$WORK_DIR/systemd/paperclip-ci-node93f4bccf-guard.service" "$SOURCE_REF" > "$WORK_DIR/rendered.service" <<'PY'
import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text()
rendered = source.replace("@SOURCE_REF@", sys.argv[2])
if "@SOURCE_REF@" in rendered:
    raise SystemExit("unrendered source-ref placeholder")
print(rendered, end="")
PY
/usr/bin/install -o root -g root -m 0644 "$WORK_DIR/rendered.service" /etc/systemd/system/paperclip-ci-node93f4bccf-guard.service
/usr/bin/install -o root -g root -m 0644 "$WORK_DIR/systemd/paperclip-ci-node93f4bccf-guard.timer" /etc/systemd/system/paperclip-ci-node93f4bccf-guard.timer
if [[ ! -e /etc/paperclip/ci-node93f4bccf-guard.env ]]; then
  /usr/bin/install -o root -g root -m 0600 "$WORK_DIR/systemd/ci-node93f4bccf-guard.env.example" /etc/paperclip/ci-node93f4bccf-guard.env
  printf 'CREATED nonsecret /etc/paperclip/ci-node93f4bccf-guard.env\n'
else
  if /usr/bin/grep -Eq '^[[:space:]]*(GH_TOKEN|nodecf677d86_GH_TOKEN)=' /etc/paperclip/ci-node93f4bccf-guard.env; then
    refuse "existing env file contains a forbidden token line"
  fi
  printf 'PRESERVED existing /etc/paperclip/ci-node93f4bccf-guard.env\n'
fi
/usr/bin/python3 - /etc/paperclip/ci-node93f4bccf-guard.env <<'PY'
import os, stat, sys
path = sys.argv[1]
mode = stat.S_IMODE(os.stat(path).st_mode)
if mode != 0o600:
    raise SystemExit(f"REFUSED: {path} mode is {mode:o}, expected 600")
PY
printf 'INSTALLED paperclip-ci-node93f4bccf-guard from %s\n' "$SOURCE_REF"
printf 'next: only after CISO approval of this pin/evidence, verify the existing authorized encrypted credential and nonsecret env file before daemon-reload and timer activation; no new credential/grant is authorized\n'
printf 'undo: systemctl disable --now paperclip-ci-node93f4bccf-guard.timer; delete CI_nodecf677d86_RUNNER via the guard or the API; remove the units and this release after retaining evidence\n'
