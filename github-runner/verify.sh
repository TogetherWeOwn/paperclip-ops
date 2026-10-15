#!/usr/bin/env bash
set -Eeuo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  FAILURES=$((FAILURES + 1))
}

pass() {
  printf 'PASS: %s\n' "$1"
}

RUNNER_USER=""
RUNNER_LABEL="two-selfhosted"
RUNNER_GROUP="Default"
RUNNER_ROOT="/opt/actions-runners"
RUNNER_HOME=""
SERVICE_NAME=""
SYSTEMD_DIR="/etc/systemd/system"
RUNTIME_DIR=""
EXPECTED_SCOPE=""
EXPECTED_NAME=""
SYSTEMCTL="systemctl"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope|--repository) EXPECTED_SCOPE="${2:-}"; shift 2 ;;
    --runner-name) EXPECTED_NAME="${2:-}"; shift 2 ;;
    --runner-user) RUNNER_USER="${2:-}"; shift 2 ;;
    --runner-label) RUNNER_LABEL="${2:-}"; shift 2 ;;
    --runner-group) RUNNER_GROUP="${2:-}"; shift 2 ;;
    --runner-root) RUNNER_ROOT="${2:-}"; shift 2 ;;
    --runner-home) RUNNER_HOME="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="${2:-}"; shift 2 ;;
    --runtime-dir) RUNTIME_DIR="${2:-}"; shift 2 ;;
    --systemctl) SYSTEMCTL="${2:-}"; shift 2 ;;
    *) refuse "unknown argument: $1" ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/actions-runner.env"

[[ "$RUNNER_LABEL" == two-selfhosted ]] || refuse "runner label must remain two-selfhosted"
[[ "$EXPECTED_SCOPE" =~ ^([A-Za-z0-9_.-]+)(/([A-Za-z0-9_.-]+))?$ ]] \
  || refuse "--scope OWNER or OWNER/REPOSITORY is required"
SCOPE_OWNER="${BASH_REMATCH[1]}"
SCOPE_REPOSITORY="${BASH_REMATCH[3]:-}"
SCOPE_KIND="organization"
[[ -z "$SCOPE_REPOSITORY" ]] || SCOPE_KIND="repository"
SCOPE_SLUG="${EXPECTED_SCOPE//\//.}"
[[ "$EXPECTED_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || refuse "--runner-name is required"
[[ "$RUNNER_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ && "$RUNNER_USER" != root ]] \
  || refuse "--runner-user must name a dedicated non-root account"
[[ "$RUNNER_GROUP" =~ ^[A-Za-z0-9._]+([[:space:]][A-Za-z0-9._]+)*$ ]] || refuse "--runner-group contains unsafe characters"
[[ "$RUNNER_ROOT" == /* ]] || refuse "--runner-root must be absolute"
[[ -n "$SERVICE_NAME" ]] || SERVICE_NAME="actions.runner.${SCOPE_SLUG}.${EXPECTED_NAME}.service"
[[ "$SERVICE_NAME" =~ ^actions\.runner\.[A-Za-z0-9_.@-]+\.service$ ]] \
  || refuse "--service-name must be an actions.runner.*.service unit"
[[ -x "$SYSTEMCTL" ]] || refuse "--systemctl is not executable"
for executable in id getent stat readlink sha256sum grep find runuser python3; do
  command -v "$executable" >/dev/null || refuse "required executable missing: $executable"
done

FAILURES=0
INSTANCE_ROOT="$RUNNER_ROOT/$SCOPE_SLUG/$EXPECTED_NAME"
CURRENT_LINK="$INSTANCE_ROOT/current"
[[ -n "$RUNNER_HOME" ]] || RUNNER_HOME="$CURRENT_LINK"
UNIT_PATH="$SYSTEMD_DIR/$SERVICE_NAME"

if getent passwd "$RUNNER_USER" >/dev/null && [[ $(id -u "$RUNNER_USER") -ne 0 ]]; then
  pass "runner identity is dedicated non-root user $RUNNER_USER"
else
  fail "runner identity $RUNNER_USER is missing or root"
fi
RUNNER_UID=$(id -u "$RUNNER_USER" 2>/dev/null || true)

if [[ -L "$CURRENT_LINK" ]]; then
  RELEASE_DIR=$(readlink -f "$CURRENT_LINK")
  if [[ "$RELEASE_DIR" == "$INSTANCE_ROOT/$RUNNER_VERSION" ]]; then
    pass "current release is pinned runner version $RUNNER_VERSION"
  else
    fail "current release $RELEASE_DIR is not $INSTANCE_ROOT/$RUNNER_VERSION"
  fi
elif [[ "$RUNNER_HOME" != "$CURRENT_LINK" && -d "$RUNNER_HOME" ]]; then
  RELEASE_DIR=$(readlink -f "$RUNNER_HOME")
  pass "auditing existing runner home $RELEASE_DIR; versioned bundle layout is not installed"
else
  fail "current release symlink is missing"
  RELEASE_DIR="$RUNNER_HOME"
fi

if [[ -f "$RELEASE_DIR/.runner" ]]; then
  if python3 - "$RELEASE_DIR/.runner" "$EXPECTED_SCOPE" "$EXPECTED_NAME" "$RUNNER_LABEL" "$RUNNER_GROUP" "$SCOPE_KIND" <<'PY'
import json
import pathlib
import sys
from urllib.parse import urlparse

metadata = json.loads(pathlib.Path(sys.argv[1]).read_text())
expected_scope, expected_name, expected_label, expected_group, scope_kind = sys.argv[2:]
server_url = metadata.get("gitHubUrl") or metadata.get("serverUrl") or ""
parsed = urlparse(server_url)
if parsed.netloc.lower() != "github.com":
    raise SystemExit("registration is not on github.com")
path = parsed.path.strip("/")
if path != expected_scope:
    raise SystemExit(f"runner is scoped to {path!r}, expected {expected_scope!r}")
if metadata.get("agentName") != expected_name:
    raise SystemExit("runner name mismatch")
labels = metadata.get("labels") or []
label_names = {label.get("name") if isinstance(label, dict) else label for label in labels}
allowed = {expected_label, "self-hosted", "Linux", "X64"}
if label_names and expected_label not in label_names:
    raise SystemExit(f"required label {expected_label!r} is absent: {sorted(label_names)}")
if label_names - allowed:
    raise SystemExit(f"unexpected labels: {sorted(label_names - allowed)}")
pool_name = metadata.get("poolName")
if scope_kind == "organization" and pool_name and pool_name != expected_group:
    raise SystemExit(f"runner group is {pool_name!r}, expected {expected_group!r}")
PY
  then
    pass "runner registration metadata matches $SCOPE_KIND scope, name, and required label"
  else
    fail "runner registration metadata is wrong"
  fi
else
  fail "runner metadata $RELEASE_DIR/.runner is missing"
fi

if runuser -u "$RUNNER_USER" -- find "$RELEASE_DIR" -xdev -mindepth 1 -maxdepth 1 \
    ! -name .runner ! -name .credentials ! -name .credentials_rsaparams \
    ! -name _work ! -name _diag -writable -print -quit 2>/dev/null | grep -q .; then
  fail "runner executable/persistent bytes are writable by the runner identity"
else
  pass "runner executable/persistent bytes are immutable to the runner identity"
fi

if [[ -f "$UNIT_PATH" ]]; then
  UNIT=$(<"$UNIT_PATH")
  EFFECTIVE=$(
    "$SYSTEMCTL" show "$SERVICE_NAME" \
      --property=FragmentPath --property=DropInPaths --property=User --property=Group \
      --property=SupplementaryGroups --property=Environment --property=ExecStart \
      --property=ReadWritePaths --property=BindReadOnlyPaths --property=MainPID
  ) || refuse "cannot read effective systemd service properties"
  effective_value() {
    local key="$1"
    grep -m1 -F "$key=" <<<"$EFFECTIVE" | cut -d= -f2-
  }
  [[ "$(effective_value FragmentPath)" == "$UNIT_PATH" ]] \
    || fail "effective systemd fragment is not $UNIT_PATH"
  [[ -z "$(effective_value DropInPaths)" ]] \
    || fail "effective systemd service has unexpected drop-ins: $(effective_value DropInPaths)"
  [[ "$(effective_value User)" == "$RUNNER_USER" ]] \
    || fail "effective service user is not $RUNNER_USER"
  [[ -z "$(effective_value SupplementaryGroups)" ]] \
    || fail "effective service has supplementary groups"
  EFFECTIVE_ENV=$(effective_value Environment)
  [[ "$EFFECTIVE_ENV" == *"DOCKER_HOST=unix:///run/user/$RUNNER_UID/docker.sock"* ]] \
    || fail "effective service Docker endpoint is not rootless runner uid socket"
  [[ "$EFFECTIVE_ENV" != *"DOCKER_HOST=unix:///var/run/docker.sock"* ]] \
    || fail "effective service selects the rootful Docker socket"
  [[ "$(effective_value ExecStart)" == *"$RUNNER_HOME/run.sh"* ]] \
    || fail "effective service does not execute $RUNNER_HOME/run.sh"
  [[ "$(effective_value BindReadOnlyPaths)" == *"/dev/null:/run/docker.sock"* \
    && "$(effective_value BindReadOnlyPaths)" == *"/dev/null:/var/run/docker.sock"* ]] \
    || fail "effective service does not mask both rootful Docker sockets"
  [[ "$(effective_value MainPID)" =~ ^[1-9][0-9]*$ ]] \
    || fail "effective service does not have exactly one live listener process"
  grep -Fxq "User=$RUNNER_USER" <<<"$UNIT" || fail "service does not run as $RUNNER_USER"
  grep -Fxq "Environment=XDG_RUNTIME_DIR=/run/user/$RUNNER_UID" <<<"$UNIT" \
    || fail "service XDG_RUNTIME_DIR does not match runner uid $RUNNER_UID"
  grep -Fxq "Environment=DOCKER_HOST=unix:///run/user/$RUNNER_UID/docker.sock" <<<"$UNIT" \
    || fail "service is not pinned to the runner uid's rootless Docker socket"
  grep -Fxq "ExecStartPre=/usr/bin/test -S /run/user/$RUNNER_UID/docker.sock" <<<"$UNIT" \
    || fail "service does not preflight the runner uid's rootless Docker socket"
  grep -Fq '%U' <<<"$UNIT" && fail "service retains system-manager %U instead of the runner uid"
  grep -Fq 'BindReadOnlyPaths=/dev/null:/run/docker.sock' <<<"$UNIT" \
    || fail "service does not mask /run/docker.sock"
  grep -Fq 'BindReadOnlyPaths=/dev/null:/var/run/docker.sock' <<<"$UNIT" \
    || fail "service does not mask /var/run/docker.sock"
  grep -Fxq "ExecStart=$RUNNER_HOME/run.sh" <<<"$UNIT" || fail "service does not execute $RUNNER_HOME/run.sh"
  if [[ "$RUNNER_HOME" == "$CURRENT_LINK" ]]; then
    grep -Fq "ReadWritePaths=$RUNNER_HOME/_diag $RUNNER_HOME/_work" <<<"$UNIT" \
      || fail "service writable paths are not limited to diagnostics and work"
  fi
  pass "service unit enforces one non-root listener and rootless Docker boundary"
else
  fail "service unit $UNIT_PATH is missing"
fi

"$SYSTEMCTL" is-enabled --quiet "$SERVICE_NAME" \
  && pass "runner service is enabled" \
  || fail "runner service is not enabled"
"$SYSTEMCTL" is-active --quiet "$SERVICE_NAME" \
  && pass "runner service is active" \
  || fail "runner service is not active"

[[ -n "$RUNTIME_DIR" ]] || RUNTIME_DIR="/run/user/$RUNNER_UID"
if [[ "$RUNNER_UID" =~ ^[0-9]+$ && -S "$RUNTIME_DIR/docker.sock" ]]; then
  SOCKET_UID=$(stat -c '%u' "$RUNTIME_DIR/docker.sock")
  [[ "$SOCKET_UID" == "$RUNNER_UID" ]] \
    && pass "rootless Docker socket exists and is owned by $RUNNER_USER" \
    || fail "rootless Docker socket is not owned by $RUNNER_USER"
else
  fail "rootless Docker socket $RUNTIME_DIR/docker.sock is absent"
fi

for socket_path in /run/docker.sock /var/run/docker.sock; do
  if [[ -S "$socket_path" ]] && runuser -u "$RUNNER_USER" -- test -w "$socket_path"; then
    fail "$RUNNER_USER can access rootful Docker socket $socket_path"
  fi
done
[[ $FAILURES -gt 0 ]] || pass "runner identity cannot access a rootful Docker socket"

if find "$SYSTEMD_DIR" "$RUNNER_HOME" -xdev -type f \
    \( -name '*.env' -o -name '*secret*' -o -name '*token*' \) -print -quit 2>/dev/null | grep -q .; then
  fail "credential-shaped file remains in the service or runner tree"
else
  pass "no host deployment secret or registration-token file remains"
fi

if (( FAILURES > 0 )); then
  printf 'github-runner-verify: FAIL (%d finding(s))\n' "$FAILURES" >&2
  exit 1
fi
printf 'github-runner-verify: PASS scope=%s runner=%s label=%s per-runner-concurrency=1\n' \
  "$EXPECTED_SCOPE" "$EXPECTED_NAME" "$RUNNER_LABEL"
