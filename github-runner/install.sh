#!/usr/bin/env bash
set -Eeuo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

info() {
  printf 'github-runner-install: %s\n' "$1"
}

RUNNER_USER=""
RUNNER_LABEL="two-selfhosted"
RUNNER_GROUP="Default"
RUNNER_ROOT="/opt/actions-runners"
SERVICE_NAME=""
SYSTEMD_DIR="/etc/systemd/system"
SCOPE_URL=""
RUNNER_NAME=""
TOKEN_FILE=""
ROLLBACK_TOKEN_FILE=""
ARCHIVE=""
DRY_RUN=0
ALLOW_REPLACE=0

usage() {
  cat <<'USAGE'
Usage: install.sh --scope-url URL --runner-name NAME --token-file FILE --rollback-token-file FILE [options]

Options:
  --scope-url URL       GitHub organization or private repository URL.
  --rollback-token-file FILE  Protected removal token used only if post-registration rollback is needed.
  --archive FILE        Use an already-downloaded pinned runner archive.
  --runner-user USER    Dedicated non-root account for this listener (required).
  --runner-label LABEL  Required custom label (default: two-selfhosted).
  --runner-group GROUP  Organization runner group (default: Default; org scope only).
  --runner-root DIR     Shared fleet root (default: /opt/actions-runners).
  --service-name NAME   systemd service name (default: derived from scope and runner name).
  --systemd-dir DIR     systemd unit directory (default: /etc/systemd/system).
  --allow-replace       Explicitly replace a remote runner with the same name.
  --dry-run             Validate and print actions without mutating the host.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope-url|--repository-url) SCOPE_URL="${2:-}"; shift 2 ;;
    --runner-name) RUNNER_NAME="${2:-}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --rollback-token-file) ROLLBACK_TOKEN_FILE="${2:-}"; shift 2 ;;
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --runner-user) RUNNER_USER="${2:-}"; shift 2 ;;
    --runner-label) RUNNER_LABEL="${2:-}"; shift 2 ;;
    --runner-group) RUNNER_GROUP="${2:-}"; shift 2 ;;
    --runner-root) RUNNER_ROOT="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="${2:-}"; shift 2 ;;
    --allow-replace) ALLOW_REPLACE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) refuse "unknown argument: $1" ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/actions-runner.env"

[[ "$SCOPE_URL" =~ ^https://github\.com/([A-Za-z0-9_.-]+)(/([A-Za-z0-9_.-]+))?/?$ ]] \
  || refuse "--scope-url must name one GitHub organization or private repository"
SCOPE_OWNER="${BASH_REMATCH[1]}"
SCOPE_REPOSITORY="${BASH_REMATCH[3]:-}"
SCOPE_KIND="organization"
[[ -z "$SCOPE_REPOSITORY" ]] || SCOPE_KIND="repository"
SCOPE_PATH="$SCOPE_OWNER"
[[ -z "$SCOPE_REPOSITORY" ]] || SCOPE_PATH="$SCOPE_OWNER/$SCOPE_REPOSITORY"
SCOPE_SLUG="${SCOPE_PATH//\//.}"
[[ "$RUNNER_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || refuse "--runner-name must be a safe non-empty name"
[[ "$RUNNER_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ && "$RUNNER_USER" != root ]] \
  || refuse "--runner-user must name a non-root local account"
# case_10099fe0. Two roles, and the label is what carries the difference:
#   two-selfhosted -> keeps a container daemon, for jobs with service containers
#   two-isolated   -> no container daemon reachable from jobs
# Any other value is still refused: the label is a security boundary, not a tag.
[[ "$RUNNER_LABEL" == two-selfhosted || "$RUNNER_LABEL" == two-isolated ]] \
  || refuse "--runner-label must be two-selfhosted or two-isolated"
[[ "$RUNNER_GROUP" =~ ^[A-Za-z0-9._]+([[:space:]][A-Za-z0-9._]+)*$ ]] || refuse "--runner-group contains unsafe characters"
if [[ "$SCOPE_KIND" == repository && "$RUNNER_GROUP" != Default ]]; then
  refuse "--runner-group applies only to organization-scoped runners"
fi
[[ "$RUNNER_ROOT" == /* ]] || refuse "--runner-root must be absolute"
[[ -n "$SERVICE_NAME" ]] || SERVICE_NAME="actions.runner.${SCOPE_SLUG}.${RUNNER_NAME}.service"
[[ "$SERVICE_NAME" =~ ^actions\.runner\.[A-Za-z0-9_.@-]+\.service$ ]] \
  || refuse "--service-name must be an actions.runner.*.service unit"
[[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] || refuse "--token-file must be a regular non-symlink file"
TOKEN_MODE=$(stat -c '%a' "$TOKEN_FILE") || refuse "cannot read token-file mode"
[[ "$TOKEN_MODE" == 400 || "$TOKEN_MODE" == 600 ]] \
  || refuse "--token-file mode must be 0400 or 0600"
[[ -s "$TOKEN_FILE" ]] || refuse "--token-file is empty"
[[ $(wc -l < "$TOKEN_FILE") -eq 1 ]] || refuse "--token-file must contain exactly one line"
[[ -f "$ROLLBACK_TOKEN_FILE" && ! -L "$ROLLBACK_TOKEN_FILE" ]] \
  || refuse "--rollback-token-file must be a regular non-symlink file"
ROLLBACK_TOKEN_MODE=$(stat -c '%a' "$ROLLBACK_TOKEN_FILE") || refuse "cannot read rollback-token-file mode"
[[ "$ROLLBACK_TOKEN_MODE" == 400 || "$ROLLBACK_TOKEN_MODE" == 600 ]] \
  || refuse "--rollback-token-file mode must be 0400 or 0600"
[[ -s "$ROLLBACK_TOKEN_FILE" ]] || refuse "--rollback-token-file is empty"
[[ $(wc -l < "$ROLLBACK_TOKEN_FILE") -eq 1 ]] \
  || refuse "--rollback-token-file must contain exactly one line"
[[ "$TOKEN_FILE" != "$ROLLBACK_TOKEN_FILE" ]] \
  || refuse "registration and rollback token files must be distinct"
if [[ -n "$ARCHIVE" ]]; then
  [[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || refuse "--archive must be a regular non-symlink file"
fi

for executable in curl sha256sum tar stat wc install id getent systemctl runuser mktemp rm python3 chown ln mv chmod find rmdir; do
  command -v "$executable" >/dev/null || refuse "required executable missing: $executable"
done

if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
  refuse "installer must run as root unless --dry-run is used"
fi

RUNNER_HOME_DIR=$(getent passwd "$RUNNER_USER" | cut -d: -f6 || true)
if getent passwd "$RUNNER_USER" >/dev/null; then
  RUNNER_UID=$(id -u "$RUNNER_USER")
  [[ "$RUNNER_UID" -ne 0 ]] || refuse "runner user resolves to uid 0"
  [[ "$RUNNER_HOME_DIR" == /* && -d "$RUNNER_HOME_DIR" ]] \
    || refuse "runner user home is missing or not absolute"
  if [[ $DRY_RUN -eq 0 ]]; then
    if [[ -S /run/docker.sock ]] && runuser -u "$RUNNER_USER" -- test -w /run/docker.sock; then
      refuse "$RUNNER_USER can access the rootful Docker socket /run/docker.sock"
    fi
    if [[ -S /var/run/docker.sock ]] && runuser -u "$RUNNER_USER" -- test -w /var/run/docker.sock; then
      refuse "$RUNNER_USER can access the rootful Docker socket /var/run/docker.sock"
    fi
  fi
elif [[ $DRY_RUN -eq 0 ]]; then
  refuse "dedicated account $RUNNER_USER does not exist; create it before installing"
fi

SCOPE_ROOT="$RUNNER_ROOT/$SCOPE_SLUG"
INSTANCE_ROOT="$SCOPE_ROOT/$RUNNER_NAME"
WORK_DIR=$(mktemp -d)
DOWNLOADED=0
STAGING_DIR=""
INSTALL_DIR=""
CURRENT_LINK=""
REGISTERED=0
on_exit() {
  rc=$?
  if [[ $rc -ne 0 && $REGISTERED -eq 1 ]]; then
    set +e
    systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1
    if [[ -x "$CURRENT_LINK/config.sh" && -f "$CURRENT_LINK/.runner" ]]; then
      runuser -u "$RUNNER_USER" -- bash -c '
        IFS= read -r token <&3
        [[ -n "$token" ]] || exit 64
        export ACTIONS_RUNNER_INPUT_TOKEN="$token"
        exec "$@"
      ' _ "$CURRENT_LINK/config.sh" remove --unattended 3<"$ROLLBACK_TOKEN_FILE"
    fi
    deregister_rc=$?
    if [[ $deregister_rc -eq 0 && ! -e "$CURRENT_LINK/.runner" ]]; then
      rm -f "$UNIT_PATH" "$CURRENT_LINK"
      systemctl daemon-reload >/dev/null 2>&1
      rm -rf "$INSTALL_DIR"
      rmdir "$INSTANCE_ROOT" "$SCOPE_ROOT" "$RUNNER_ROOT" 2>/dev/null || true
      rm -f "$TOKEN_FILE" "$ROLLBACK_TOKEN_FILE"
      printf 'ROLLED-BACK: post-registration failure removed the remote registration and local install\n' >&2
    else
      printf 'PARTIAL: automatic deregistration failed; local state and both protected token files were preserved\n' >&2
    fi
    set -e
  elif [[ $rc -ne 0 ]]; then
    [[ -z "$CURRENT_LINK" ]] || rm -f "$CURRENT_LINK"
    [[ -z "$INSTALL_DIR" ]] || rm -rf "$INSTALL_DIR"
    [[ -z "$STAGING_DIR" ]] || rm -rf "$STAGING_DIR"
    rmdir "$INSTANCE_ROOT" "$SCOPE_ROOT" "$RUNNER_ROOT" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
  exit "$rc"
}
trap on_exit EXIT

VALIDATED_ARCHIVE="$WORK_DIR/$RUNNER_ARCHIVE"
if [[ -n "$ARCHIVE" ]]; then
  if [[ $DRY_RUN -eq 0 ]]; then
    install -o root -g root -m 0600 "$ARCHIVE" "$VALIDATED_ARCHIVE"
  fi
else
  if [[ $DRY_RUN -eq 0 ]]; then
    curl --fail --location --proto '=https' --tlsv1.2 \
      --output "$VALIDATED_ARCHIVE" "$RUNNER_URL"
    chmod 0600 "$VALIDATED_ARCHIVE"
    DOWNLOADED=1
  fi
fi

if [[ $DRY_RUN -eq 0 ]]; then
  printf '%s  %s\n' "$RUNNER_SHA256" "$VALIDATED_ARCHIVE" | sha256sum --check --strict
else
  info "DRY-RUN would snapshot/download $RUNNER_URL and require sha256 $RUNNER_SHA256"
fi

INSTALL_DIR="$INSTANCE_ROOT/$RUNNER_VERSION"
STAGING_DIR="$INSTANCE_ROOT/.staging-$RUNNER_VERSION-$$"
CURRENT_LINK="$INSTANCE_ROOT/current"
UNIT_PATH="$SYSTEMD_DIR/$SERVICE_NAME"
DROPIN_DIR="$UNIT_PATH.d"
if [[ $DRY_RUN -eq 1 ]]; then
  info "DRY-RUN validated scope=$SCOPE_URL kind=$SCOPE_KIND runner=$RUNNER_NAME user=$RUNNER_USER label=$RUNNER_LABEL group=$RUNNER_GROUP"
  info "DRY-RUN would install $RUNNER_ARCHIVE into $INSTALL_DIR"
  info "DRY-RUN would register one $SCOPE_KIND runner with GitHub default labels plus $RUNNER_LABEL"
  info "DRY-RUN would install $UNIT_PATH with per-runner concurrency=1 and rootless Docker only"
  exit 0
fi

[[ ! -e "$INSTANCE_ROOT" && ! -L "$INSTANCE_ROOT" ]] || refuse "runner instance already exists: $INSTANCE_ROOT"
[[ ! -e "$UNIT_PATH" && ! -L "$UNIT_PATH" ]] || refuse "service already exists: $UNIT_PATH"
[[ ! -e "$DROPIN_DIR" && ! -L "$DROPIN_DIR" ]] || refuse "service drop-in path already exists: $DROPIN_DIR"

install -d -o root -g root -m 0755 "$RUNNER_ROOT" "$SCOPE_ROOT" "$INSTANCE_ROOT"
install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0750 "$STAGING_DIR"
tar --extract --gzip --file "$VALIDATED_ARCHIVE" --directory "$STAGING_DIR" \
  --no-same-owner --no-same-permissions
[[ -x "$STAGING_DIR/config.sh" && -x "$STAGING_DIR/run.sh" ]] \
  || refuse "runner archive is missing executable config.sh or run.sh"
chown -R "$RUNNER_USER:$RUNNER_USER" "$STAGING_DIR"
mv "$STAGING_DIR" "$INSTALL_DIR"
STAGING_DIR=""
ln -s "$INSTALL_DIR" "$CURRENT_LINK"

CONFIG_ARGS=(
  --unattended
  --url "$SCOPE_URL"
  --name "$RUNNER_NAME"
  --labels "$RUNNER_LABEL"
  --disableupdate
  --work _work
)
if [[ "$SCOPE_KIND" == organization ]]; then
  CONFIG_ARGS+=(--runnergroup "$RUNNER_GROUP")
fi
[[ $ALLOW_REPLACE -eq 0 ]] || CONFIG_ARGS+=(--replace)

# Root opens the protected file, then the runner user reads only the inherited
# descriptor. The secret is never an argv value; config.sh receives it through
# the runner's documented ACTIONS_RUNNER_INPUT_TOKEN input.
if ! runuser -u "$RUNNER_USER" -- bash -c '
  IFS= read -r token <&3
  [[ -n "$token" ]] || exit 64
  export ACTIONS_RUNNER_INPUT_TOKEN="$token"
  exec "$@"
' _ "$CURRENT_LINK/config.sh" "${CONFIG_ARGS[@]}" 3<"$TOKEN_FILE"; then
  rm -f "$CURRENT_LINK"
  rm -rf "$INSTALL_DIR"
  refuse "runner registration failed; install files were removed"
fi
REGISTERED=1
# Registration state remains runner-owned; freeze executable/persistent bytes so
# the same-uid rootless Docker daemon cannot bind-mount and rewrite the listener.
find "$INSTALL_DIR" -xdev -mindepth 1 -maxdepth 1 \
  ! -name .runner ! -name .credentials ! -name .credentials_rsaparams \
  ! -name _work ! -name _diag -exec chown -R root:root {} +
find "$INSTALL_DIR" -xdev -mindepth 1 -maxdepth 1 \
  ! -name .runner ! -name .credentials ! -name .credentials_rsaparams \
  ! -name _work ! -name _diag -type d -exec chmod 0755 {} +
find "$INSTALL_DIR" -xdev -mindepth 1 -maxdepth 1 \
  ! -name .runner ! -name .credentials ! -name .credentials_rsaparams \
  ! -name _work ! -name _diag -type f -exec chmod go-w {} +
install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0700 \
  "$INSTALL_DIR/_work" "$INSTALL_DIR/_diag"

UNIT_TMP="$WORK_DIR/$SERVICE_NAME"
python3 - "$HERE/actions-runner.service.in" "$RUNNER_USER" "$RUNNER_UID" "$RUNNER_HOME_DIR" "$CURRENT_LINK" > "$UNIT_TMP" <<'PY'
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text()
rendered = (source.replace("@RUNNER_USER@", sys.argv[2])
                  .replace("@RUNNER_UID@", sys.argv[3])
                  .replace("@USER_HOME@", sys.argv[4])
                  .replace("@RUNNER_HOME@", sys.argv[5]))
if "@RUNNER_" in rendered:
    raise SystemExit("unrendered runner unit placeholder")
print(rendered, end="")
PY
install -o root -g root -m 0644 "$UNIT_TMP" "$UNIT_PATH"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME" || refuse "runner service did not become active"
rm -f "$TOKEN_FILE" "$ROLLBACK_TOKEN_FILE"
info "installed runner $RUNNER_NAME at $CURRENT_LINK"
info "scope=$SCOPE_URL service=$SERVICE_NAME user=$RUNNER_USER label=$RUNNER_LABEL per-runner-concurrency=1"
info "configuration token was read from $TOKEN_FILE, never placed on argv, and the file was removed"
[[ $DOWNLOADED -eq 0 ]] || info "downloaded archive matched the pinned SHA-256"
