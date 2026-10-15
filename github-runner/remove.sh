#!/usr/bin/env bash
set -Eeuo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

RUNNER_USER=""
RUNNER_ROOT="/opt/actions-runners"
RUNNER_HOME=""
RUNNER_NAME=""
EXPECTED_SCOPE=""
SERVICE_NAME=""
SYSTEMD_DIR="/etc/systemd/system"
TOKEN_FILE=""
DELETE_USER=0
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage: remove.sh --scope OWNER[/REPOSITORY] --runner-name NAME --token-file FILE [options]

The remove token is consumed from a protected file through ACTIONS_RUNNER_INPUT_TOKEN.
It is never placed on argv. The token file is deleted after config.sh remove runs.

Options:
  --scope OWNER[/REPOSITORY]  Used to derive the service name.
  --runner-name NAME          Fleet instance to unregister and remove.
  --runner-user USER           Dedicated account for this listener (required).
  --runner-root DIR           Shared fleet root (default: /opt/actions-runners).
  --runner-home DIR           Existing manual runner home instead of the fleet layout.
  --service-name NAME         Override the derived systemd service name.
  --systemd-dir DIR
  --delete-user               Delete the account only when no other fleet instance remains.
  --dry-run                   Print the rollback sequence without mutating the host.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope|--repository) EXPECTED_SCOPE="${2:-}"; shift 2 ;;
    --runner-name) RUNNER_NAME="${2:-}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --runner-user) RUNNER_USER="${2:-}"; shift 2 ;;
    --runner-root) RUNNER_ROOT="${2:-}"; shift 2 ;;
    --runner-home) RUNNER_HOME="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="${2:-}"; shift 2 ;;
    --delete-user) DELETE_USER=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) refuse "unknown argument: $1" ;;
  esac
done

[[ "$RUNNER_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ && "$RUNNER_USER" != root ]] \
  || refuse "--runner-user must name a dedicated non-root account"
[[ "$RUNNER_ROOT" == /* ]] || refuse "--runner-root must be absolute"
[[ "$RUNNER_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || refuse "--runner-name is required"
[[ "$EXPECTED_SCOPE" =~ ^([A-Za-z0-9_.-]+)(/[A-Za-z0-9_.-]+)?$ ]] \
  || refuse "--scope OWNER or OWNER/REPOSITORY is required"
SCOPE_SLUG="${EXPECTED_SCOPE//\//.}"
if [[ -z "$SERVICE_NAME" ]]; then
  SERVICE_NAME="actions.runner.${SCOPE_SLUG}.${RUNNER_NAME}.service"
fi
[[ "$SERVICE_NAME" =~ ^actions\.runner\.[A-Za-z0-9_.@-]+\.service$ ]] \
  || refuse "--service-name must be an actions.runner.*.service unit"
[[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] || refuse "--token-file must be a regular non-symlink file"
TOKEN_MODE=$(stat -c '%a' "$TOKEN_FILE") || refuse "cannot read token-file mode"
[[ "$TOKEN_MODE" == 400 || "$TOKEN_MODE" == 600 ]] \
  || refuse "--token-file mode must be 0400 or 0600"
[[ -s "$TOKEN_FILE" ]] || refuse "--token-file is empty"
[[ $(wc -l < "$TOKEN_FILE") -eq 1 ]] || refuse "--token-file must contain exactly one line"
for executable in stat wc find python3; do
  command -v "$executable" >/dev/null || refuse "required executable missing: $executable"
done
if [[ $DRY_RUN -eq 0 ]]; then
  for executable in systemctl runuser rm rmdir userdel getent; do
    command -v "$executable" >/dev/null || refuse "required executable missing: $executable"
  done
fi
if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
  refuse "remover must run as root unless --dry-run is used"
fi

SCOPE_ROOT="$RUNNER_ROOT/$SCOPE_SLUG"
INSTANCE_ROOT="$SCOPE_ROOT/$RUNNER_NAME"
CURRENT_LINK="$INSTANCE_ROOT/current"
MANUAL_HOME=0
if [[ -n "$RUNNER_HOME" ]]; then
  MANUAL_HOME=1
else
  RUNNER_HOME="$CURRENT_LINK"
fi
UNIT_PATH="$SYSTEMD_DIR/$SERVICE_NAME"
DROPIN_DIR="$UNIT_PATH.d"

if [[ -e "$DROPIN_DIR" || -L "$DROPIN_DIR" ]]; then
  refuse "service drop-in path exists; inspect and remove it explicitly before rollback: $DROPIN_DIR"
fi

if [[ $DELETE_USER -eq 1 && $DRY_RUN -eq 0 ]]; then
  while IFS= read -r other_unit_path; do
    [[ "$other_unit_path" == "$UNIT_PATH" ]] && continue
    other_unit=${other_unit_path##*/}
    other_user=$(systemctl show "$other_unit" --property=User --value 2>/dev/null || true)
    [[ "$other_user" != "$RUNNER_USER" ]] \
      || refuse "--delete-user would break another runner service using $RUNNER_USER: $other_unit"
  done < <(find "$SYSTEMD_DIR" -maxdepth 1 -type f -name 'actions.runner.*.service' -print)
fi

if [[ $DRY_RUN -eq 1 ]]; then
  printf 'github-runner-remove: DRY-RUN stop+disable %s\n' "$SERVICE_NAME"
  printf 'github-runner-remove: DRY-RUN unregister %s through token file %s, then delete that file\n' "$RUNNER_HOME" "$TOKEN_FILE"
  printf 'github-runner-remove: DRY-RUN delete fleet instance %s and all runner work/diagnostics\n' "$RUNNER_NAME"
  [[ $DELETE_USER -eq 0 ]] || printf 'github-runner-remove: DRY-RUN delete user %s after proving no other fleet instance exists\n' "$RUNNER_USER"
  exit 0
fi

[[ -x "$RUNNER_HOME/config.sh" ]] || refuse "runner config.sh is missing; cannot prove remote deregistration"
[[ -f "$RUNNER_HOME/.runner" ]] || refuse "runner metadata is missing; cannot prove remote deregistration"
python3 - "$RUNNER_HOME/.runner" "$EXPECTED_SCOPE" "$RUNNER_NAME" <<'PY' \
  || refuse "runner metadata does not match --scope and --runner-name; nothing was stopped"
import json
import pathlib
import sys
from urllib.parse import urlparse

metadata = json.loads(pathlib.Path(sys.argv[1]).read_text())
expected_scope, expected_name = sys.argv[2:]
url = metadata.get("gitHubUrl") or metadata.get("serverUrl") or ""
parsed = urlparse(url)
if parsed.netloc.lower() != "github.com" or parsed.path.strip("/") != expected_scope:
    raise SystemExit("scope mismatch")
if metadata.get("agentName") != expected_name:
    raise SystemExit("name mismatch")
PY

systemctl disable --now "$SERVICE_NAME"
if systemctl is-active --quiet "$SERVICE_NAME"; then
  refuse "runner service is still active; local and remote state were preserved"
fi

[[ -x "$RUNNER_HOME/config.sh" ]] || refuse "runner config.sh is missing; cannot prove remote deregistration"
[[ -f "$RUNNER_HOME/.runner" ]] || refuse "runner metadata is missing; cannot prove remote deregistration"
runuser -u "$RUNNER_USER" -- bash -c '
  IFS= read -r token <&3
  [[ -n "$token" ]] || exit 64
  export ACTIONS_RUNNER_INPUT_TOKEN="$token"
  exec "$@"
' _ "$RUNNER_HOME/config.sh" remove --unattended 3<"$TOKEN_FILE"
[[ ! -e "$RUNNER_HOME/.runner" ]] || refuse "config.sh returned success but runner metadata remains"

rm -f "$TOKEN_FILE"
rm -f "$UNIT_PATH"
systemctl daemon-reload
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
if [[ $MANUAL_HOME -eq 1 ]]; then
  rm -rf "$RUNNER_HOME"
else
  rm -rf "$INSTANCE_ROOT"
  rmdir "$SCOPE_ROOT" "$RUNNER_ROOT" 2>/dev/null || true
fi

if [[ $DELETE_USER -eq 1 ]] && getent passwd "$RUNNER_USER" >/dev/null; then
  userdel --remove "$RUNNER_USER"
fi

[[ ! -e "$UNIT_PATH" ]] || refuse "service unit survived removal"
[[ ! -e "$RUNNER_HOME" && ! -L "$RUNNER_HOME" ]] || refuse "runner home survived removal"
printf 'github-runner-remove: PASS runner=%s service, registration, work tree, and diagnostics removed\n' "$RUNNER_NAME"
