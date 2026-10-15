#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage:
  sudo ./host-ops/install.sh --source-ref <40-hex-commit> --public-key-out <absolute-path> --expected-public-key-sha256 <64-hex> --cliproxy-unit <host-captured-unit.service> [--paperclip-image-ref <expected-image>]

Installs broker bytes exported from the reviewed commit outside /paperclip. The
expected public-key hash is chosen before the install and verified against a
valid public key that matches the host-held private key. The script never prints
or exports the private key.

--paperclip-image-ref is an optional assertion of the image reference
image.inspect will attest. It is host state, never an override or default: the
canonical reference now comes from node7b1ff338's running Docker container
(`docker inspect paperclip --format '{{.Config.Image}}'`), never a Quadlet
Image=. It is written into both the exact-argv sudoers grant and config.json,
which the broker requires to agree. No tag, default or retag is used.
Before mutation, compare the observed running container's image ID and manifest
digest to the pinned image. The same check runs before every broker start.
This installer ships writesEnabled:false and testMode:false, even on upgrades.
Upgrade only: existing broker identity, protected directories and broker-owned
keys are required. No bootstrap, key metadata repair or carrier changes occur.
--public-key-out must be /etc/paperclip-host-ops/response-signing.pub.pem.
The transaction emits a protected recovery journal and rollback command.
Do not install from an unreviewed commit: the bytes on the host must be the
bytes the reviewer approved on the case_81816394 Docker-port PR.
USAGE
}

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

SOURCE_REF=""
PUBLIC_KEY_OUT=""
EXPECTED_PUBLIC_KEY_SHA256=""
CLIPROXY_UNIT=""
PAPERCLIP_IMAGE_REF=""
IMAGE_REF_ASSERTED=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-ref) SOURCE_REF="${2:-}"; shift 2;;
    --public-key-out) PUBLIC_KEY_OUT="${2:-}"; shift 2;;
    --expected-public-key-sha256) EXPECTED_PUBLIC_KEY_SHA256="${2:-}"; shift 2;;
    --cliproxy-unit) CLIPROXY_UNIT="${2:-}"; shift 2;;
    --paperclip-image-ref) PAPERCLIP_IMAGE_REF="${2:-}"; IMAGE_REF_ASSERTED=true; shift 2;;
    --help|-h) usage; exit 0;;
    *) refuse "unknown argument: $1";;
  esac
done

[[ $EUID -eq 0 ]] || refuse "installer must run as root"
[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]] || refuse "--source-ref must be a 40-hex commit"
[[ "$PUBLIC_KEY_OUT" == /* ]] || refuse "--public-key-out must be absolute"
[[ "$EXPECTED_PUBLIC_KEY_SHA256" =~ ^[0-9a-f]{64}$ ]] || refuse "--expected-public-key-sha256 must be 64 hex"
# E3 (CTO topology-evidence decision, case_477bab93 rev 8a515f6a): the exact
# CLIProxy unit name is host state the operator captures during the
# consolidated host window. It is required, pinned, never wildcarded and never
# defaulted: a systemd unit basename with a .service suffix, no path, no
# specifier, no colon (the sudoers run-as separator) and no second argv token.
# No lifecycle verb may reach the grant through this name; the suffix
# allow-list admits only `.service`.
[[ "$CLIPROXY_UNIT" != *":"* ]] || refuse "--cliproxy-unit must not carry a colon"
[[ "$CLIPROXY_UNIT" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || refuse "--cliproxy-unit must be a plain .service unit name captured from the host"
[[ "$CLIPROXY_UNIT" != *".."* && "$CLIPROXY_UNIT" != *"/"* && "$CLIPROXY_UNIT" != *" "* ]] || refuse "--cliproxy-unit must not carry a path"

# The existing host-held signing pair is never generated or rotated. The
# transaction validates these same fixed targets without changing their bytes.
PRIVATE_KEY=/var/lib/paperclip-host-ops/keys/response-signing.pem
PUBLIC_KEY=/var/lib/paperclip-host-ops/keys/response-signing.pub.pem

# The former direct-copy installer failed dirty on an output colliding with the
# signing public key (case_4f3123eb). Keep its early collision refusals even though
# the transaction now restricts output to one journaled destination.
#
# Compare NORMALISED paths, not strings: `.../keys/../keys/f` and `.../keys/./f`
# name the same file with different spellings, and a string equality test would
# wave both through to the dirty abort. Normalisation is lexical (pure bash) so
# this guard stays above the required-command loop and needs no new dependency.
normalize_path() {
  local path="$1" out=() part
  local -a parts
  IFS='/' read -r -a parts <<< "$path"
  for part in "${parts[@]}"; do
    case "$part" in
      ''|.) ;;
      ..) ((${#out[@]})) && unset 'out[-1]' ;;
      *) out+=("$part") ;;
    esac
  done
  local joined
  joined=$(IFS='/'; printf '%s' "${out[*]}")
  printf '/%s\n' "$joined"
}
[[ "$(normalize_path "$PUBLIC_KEY_OUT")" != "$(normalize_path "$PUBLIC_KEY")" ]] || \
  refuse "--public-key-out must not name the installer's own public key ($PUBLIC_KEY); pass a separate destination such as /etc/paperclip-host-ops/response-signing.pub.pem"
# A symlink or hard link is the same file under a genuinely different path, which
# lexical normalisation cannot see. bash's -ef compares device and inode, and it
# is exactly the condition GNU install refuses on.
[[ ! -e "$PUBLIC_KEY_OUT" || ! -e "$PUBLIC_KEY" || ! "$PUBLIC_KEY_OUT" -ef "$PUBLIC_KEY" ]] || \
  refuse "--public-key-out is another name for the installer's own public key ($PUBLIC_KEY); pass a separate destination"
# Mirrors IMAGE_REF_RE in broker.py: no whitespace, comma, backslash or quote can
# reach the sudoers grant and terminate or widen the authorized command.
if [[ "$IMAGE_REF_ASSERTED" == true ]]; then
  [[ "$PAPERCLIP_IMAGE_REF" =~ ^[a-z0-9][a-z0-9._/-]{0,159}(:[A-Za-z0-9._-]{1,127}|@sha256:[0-9a-f]{64})?$ ]] || \
    refuse "--paperclip-image-ref must be a plain image reference (canonical source requires @sha256)"
fi

[[ "$PUBLIC_KEY_OUT" == /etc/paperclip-host-ops/response-signing.pub.pem ]] || \
  refuse "--public-key-out must be the fixed journaled destination /etc/paperclip-host-ops/response-signing.pub.pem"

for command in git openssl sha256sum systemctl systemd-analyze visudo python3 mktemp tar getent id passwd cp chmod cmp; do
  command -v "$command" >/dev/null || refuse "required command missing: $command"
done

source_type=$(git --no-replace-objects cat-file -t "$SOURCE_REF" 2>/dev/null) || refuse "--source-ref does not name an available object"
[[ "$source_type" == commit ]] || refuse "--source-ref must name a commit object directly"
git --no-replace-objects cat-file -e "$SOURCE_REF:host-ops/broker.py" || refuse "--source-ref does not contain host-ops/broker.py"
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
git --no-replace-objects archive --format=tar "$SOURCE_REF" host-ops | tar -x -C "$WORK_DIR"
SOURCE_DIR="$WORK_DIR/host-ops"
for source in broker.py image_binding.py install_state.py install_transaction.py spool_provisioning.py RUNBOOK.md host-ops-broker.service host-ops-broker.tmpfiles host-ops-broker.sudoers config.example.json install-lib.sh; do
  [[ -f "$SOURCE_DIR/$source" && ! -L "$SOURCE_DIR/$source" ]] || refuse "reviewed commit is missing regular host-ops/$source"
done
# shellcheck source=host-ops/install-lib.sh
source "$SOURCE_DIR/install-lib.sh"

# Validate every exported artifact before mutating accounts, live files, sudo
# authority, tmpfiles state, or the running unit.
bash -n "$SOURCE_DIR/install-lib.sh"
python3 -m py_compile "$SOURCE_DIR/broker.py" "$SOURCE_DIR/image_binding.py" "$SOURCE_DIR/install_state.py" "$SOURCE_DIR/install_transaction.py" "$SOURCE_DIR/spool_provisioning.py"
rm -rf "$SOURCE_DIR/__pycache__"
image_assertion=()
if [[ "$IMAGE_REF_ASSERTED" == true ]]; then
  image_assertion=(--expected-ref "$PAPERCLIP_IMAGE_REF")
fi
PAPERCLIP_IMAGE_REF=$(python3 "$SOURCE_DIR/image_binding.py" source-ref "${image_assertion[@]}")
CONFIG_TMP="$WORK_DIR/config.json"
if [[ -f /etc/paperclip-host-ops/config.json ]]; then
  cp /etc/paperclip-host-ops/config.json "$CONFIG_TMP"
else
  cp "$SOURCE_DIR/config.example.json" "$CONFIG_TMP"
fi
render_disabled_config "$CONFIG_TMP" "$CONFIG_TMP" "$PAPERCLIP_IMAGE_REF"
python3 "$SOURCE_DIR/image_binding.py" check --config "$CONFIG_TMP"
SUDOERS_TMP="$WORK_DIR/sudoers"
# One canonical Quadlet pin drives both config and the exact-argv grant. The
# observed-container check above is separate from their string agreement.
render_sudoers_image_ref "$SOURCE_DIR/host-ops-broker.sudoers" "$SUDOERS_TMP" "$PAPERCLIP_IMAGE_REF"
render_sudoers_cliproxy_unit "$SUDOERS_TMP" "$SUDOERS_TMP" "$CLIPROXY_UNIT"
chmod 0440 "$SUDOERS_TMP"
visudo -cf "$SUDOERS_TMP" >/dev/null
UNIT_TMP="$WORK_DIR/paperclip-host-ops-broker.service"
cp "$SOURCE_DIR/host-ops-broker.service" "$UNIT_TMP"
python3 - "$UNIT_TMP" "$SOURCE_DIR/broker.py" "$SOURCE_DIR/config.example.json" <<'PY'
import sys
from pathlib import Path
unit = Path(sys.argv[1])
text = unit.read_text()
text = text.replace("/usr/local/libexec/paperclip-host-ops/image_binding.py", str(Path(sys.argv[2]).resolve().with_name("image_binding.py")))
text = text.replace("/usr/local/libexec/paperclip-host-ops/broker.py", str(Path(sys.argv[2]).resolve()))
text = text.replace("/etc/paperclip-host-ops/config.json", str(Path(sys.argv[3]).resolve()))
unit.write_text(text)
PY
systemd-analyze verify "$UNIT_TMP" >/dev/null
validate_response_signing_key "$PRIVATE_KEY" "$PUBLIC_KEY" "$EXPECTED_PUBLIC_KEY_SHA256" "$WORK_DIR"
identity_state=$(classify_service_identity)

# This bounded transaction supports existing installations only. Bootstrap and
# carrier/mapped-UID provisioning need separately journaled preparation; never
# create untracked accounts or mutate data-directory/key metadata as a shortcut.
[[ "$identity_state" == valid ]] || refuse "transactional upgrade requires the existing broker identity; bootstrap is not supported"
validate_service_identity
python3 -B "$SOURCE_DIR/install_transaction.py" apply \
  --source-dir "$SOURCE_DIR" --source-ref "$SOURCE_REF" \
  --config "$CONFIG_TMP" --sudoers "$SUDOERS_TMP"

printf 'INSTALLED disabled paperclip-host-ops-broker from %s\n' "$SOURCE_REF"
printf 'public key: %s (sha256 %s)\n' "$PUBLIC_KEY_OUT" "$ACTUAL_PUBLIC_KEY_SHA256"
printf 'Recovery journal and its pinned rollback entrypoint were printed above; preserve them.\n'
