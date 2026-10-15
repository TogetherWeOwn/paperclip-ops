#!/usr/bin/env bash
set -Eeuo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

RUNNER_USER_PREFIX="gha-runner"
RUNNER_LABEL="two-selfhosted"
RUNNER_GROUP="Default"
RUNNER_ROOT="/opt/actions-runners"
SYSTEMD_DIR="/etc/systemd/system"
RUNTIME_ROOT="/run/user"
SYSTEMCTL="systemctl"
EXPECTED_SCOPE="TogetherWeOwn"
NAME_PREFIX="coolify-vps"
RUNNER_COUNT=5
ALLOW_SHARED_IDENTITY=0
LISTENERS=()

usage() {
  cat <<'USAGE'
Usage: verify-fleet.sh [--scope OWNER[/REPO]] [options]

Fleet shape (choose one):
  --runner-count N            Derived fleet: NAME_PREFIX-1..N each on RUNNER_USER_PREFIX-1..N
                              in the bundle install layout (the reviewed install target).
  --listener NAME:USER[:HOME] Explicit listener, repeatable. Use this to audit a fleet that was
                              installed by hand, whose accounts or home directories do not follow
                              the bundle layout. Repeating a USER is shared-identity drift and is
                              refused unless --allow-shared-identity is given.

Options:
  --name-prefix PREFIX        Derived-fleet runner name prefix (default: coolify-vps).
  --runner-user-prefix PREFIX Derived-fleet account prefix (default: gha-runner).
  --runner-label LABEL        Required custom label (default: two-selfhosted).
  --runner-group GROUP        Organization runner group (default: Default).
  --runner-root DIR           Bundle fleet root (default: /opt/actions-runners).
  --systemd-dir DIR           systemd unit directory (default: /etc/systemd/system).
  --runtime-root DIR          Rootless runtime root (default: /run/user).
  --systemctl PATH            systemctl binary to interrogate.
  --allow-shared-identity     Audit a fleet whose listeners share one account. The drift is still
                              reported; it stops being fatal so the rest of the audit still runs.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) EXPECTED_SCOPE="${2:-}"; shift 2 ;;
    --runner-count) RUNNER_COUNT="${2:-}"; shift 2 ;;
    --listener) LISTENERS+=("${2:-}"); shift 2 ;;
    --allow-shared-identity) ALLOW_SHARED_IDENTITY=1; shift ;;
    --name-prefix) NAME_PREFIX="${2:-}"; shift 2 ;;
    --runner-user-prefix) RUNNER_USER_PREFIX="${2:-}"; shift 2 ;;
    --runner-label) RUNNER_LABEL="${2:-}"; shift 2 ;;
    --runner-group) RUNNER_GROUP="${2:-}"; shift 2 ;;
    --runner-root) RUNNER_ROOT="${2:-}"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="${2:-}"; shift 2 ;;
    --runtime-root) RUNTIME_ROOT="${2:-}"; shift 2 ;;
    --systemctl) SYSTEMCTL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) refuse "unknown argument: $1" ;;
  esac
done

[[ "$EXPECTED_SCOPE" =~ ^([A-Za-z0-9_.-]+)(/[A-Za-z0-9_.-]+)?$ ]] \
  || refuse "--scope must be OWNER or OWNER/REPOSITORY"
SCOPE_SLUG="${EXPECTED_SCOPE//\//.}"
[[ "$NAME_PREFIX" =~ ^[A-Za-z0-9._-]+$ ]] || refuse "--name-prefix contains unsafe characters"
[[ "$RUNNER_USER_PREFIX" =~ ^[a-z_][a-z0-9_-]*$ ]] \
  || refuse "--runner-user-prefix contains unsafe characters"
[[ "$RUNNER_COUNT" =~ ^[1-9][0-9]*$ && "$RUNNER_COUNT" -le 32 ]] \
  || refuse "--runner-count must be an integer from 1 through 32"
[[ -x "$SYSTEMCTL" ]] || refuse "--systemctl is not executable"
for executable in find grep id python3 seq sort tr uniq wc; do
  command -v "$executable" >/dev/null || refuse "required executable missing: $executable"
done

# Resolve the fleet into parallel arrays: listener name, account, home override ("" = bundle layout).
LISTENER_NAMES=()
LISTENER_USERS=()
LISTENER_HOMES=()
if [[ ${#LISTENERS[@]} -gt 0 ]]; then
  for entry in "${LISTENERS[@]}"; do
    listener_name="${entry%%:*}"
    listener_rest="${entry#*:}"
    [[ "$listener_rest" != "$entry" ]] || refuse "--listener must be NAME:USER[:HOME], got: $entry"
    listener_user="${listener_rest%%:*}"
    listener_home=""
    [[ "$listener_rest" == "$listener_user" ]] || listener_home="${listener_rest#*:}"
    [[ "$listener_name" =~ ^[A-Za-z0-9._-]+$ ]] \
      || refuse "--listener name contains unsafe characters: $entry"
    [[ "$listener_user" =~ ^[a-z_][a-z0-9_-]*[$]?$ && "$listener_user" != root ]] \
      || refuse "--listener account must name a non-root local account: $entry"
    [[ -z "$listener_home" || "$listener_home" == /* ]] \
      || refuse "--listener home must be absolute: $entry"
    LISTENER_NAMES+=("$listener_name")
    LISTENER_USERS+=("$listener_user")
    LISTENER_HOMES+=("$listener_home")
  done
  RUNNER_COUNT=${#LISTENER_NAMES[@]}
  [[ "$RUNNER_COUNT" -le 32 ]] || refuse "--listener may be repeated at most 32 times"
  [[ $(printf '%s\n' "${LISTENER_NAMES[@]}" | sort -u | wc -l) -eq "$RUNNER_COUNT" ]] \
    || refuse "--listener names must be unique"
else
  for index in $(seq 1 "$RUNNER_COUNT"); do
    LISTENER_NAMES+=("${NAME_PREFIX}-${index}")
    LISTENER_USERS+=("${RUNNER_USER_PREFIX}-${index}")
    LISTENER_HOMES+=("")
  done
fi

DISTINCT_IDENTITIES=$(printf '%s\n' "${LISTENER_USERS[@]}" | sort -u | wc -l)
if [[ "$DISTINCT_IDENTITIES" -eq "$RUNNER_COUNT" ]]; then
  ISOLATION_POSTURE="isolated-identities"
  printf 'github-runner-fleet-isolation: OK listeners=%d distinct-identities=%d\n' \
    "$RUNNER_COUNT" "$DISTINCT_IDENTITIES"
else
  ISOLATION_POSTURE="shared-identity"
  SHARED_ACCOUNTS=$(printf '%s\n' "${LISTENER_USERS[@]}" | sort | uniq -d | tr '\n' ' ')
  printf 'github-runner-fleet-isolation: DRIFT listeners=%d distinct-identities=%d shared-accounts=[%s]; same-uid jobs can read sibling work and credential state\n' \
    "$RUNNER_COUNT" "$DISTINCT_IDENTITIES" "${SHARED_ACCOUNTS% }" >&2
  [[ "$ALLOW_SHARED_IDENTITY" -eq 1 ]] \
    || refuse "fleet listeners share an account; pass --allow-shared-identity to audit the drifted fleet anyway"
  printf 'github-runner-fleet-isolation: DRIFT acknowledged by --allow-shared-identity; audit continues\n'
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
expected_units=""
for listener_name in "${LISTENER_NAMES[@]}"; do
  expected_units+="actions.runner.${SCOPE_SLUG}.${listener_name}.service"$'\n'
done
expected_units=$(printf '%s' "$expected_units" | sort -u)

unit_files=$(find "$SYSTEMD_DIR" -maxdepth 1 -type f -name 'actions.runner.*.service' -printf '%f\n')
loaded_units=$(
  "$SYSTEMCTL" list-units --all --type=service --plain --no-legend 'actions.runner.*.service' \
    | while IFS= read -r line; do [[ -z "$line" ]] || printf '%s\n' "${line%% *}"; done
)
actual_units=$(printf '%s\n%s\n' "$unit_files" "$loaded_units" | grep -v '^$' | sort -u)
[[ "$actual_units" == "$expected_units" ]] \
  || refuse "Actions runner unit set differs from the exact $RUNNER_COUNT-listener fleet; expected=[$(tr '\n' ' ' <<<"$expected_units")] actual=[$(tr '\n' ' ' <<<"$actual_units")]"

# Audit every listener before reporting: one bad listener must not hide the rest of the fleet.
FLEET_FAILURES=0
FAILED_LISTENERS=""
for index in $(seq 1 "$RUNNER_COUNT"); do
  name="${LISTENER_NAMES[$((index - 1))]}"
  user="${LISTENER_USERS[$((index - 1))]}"
  home="${LISTENER_HOMES[$((index - 1))]}"
  service="actions.runner.${SCOPE_SLUG}.${name}.service"
  uid=$(id -u "$user" 2>/dev/null || true)
  args=(
    --scope "$EXPECTED_SCOPE"
    --runner-name "$name"
    --runner-user "$user"
    --runner-label "$RUNNER_LABEL"
    --runner-group "$RUNNER_GROUP"
    --runner-root "$RUNNER_ROOT"
    --service-name "$service"
    --systemd-dir "$SYSTEMD_DIR"
    --systemctl "$SYSTEMCTL"
  )
  [[ -z "$home" ]] || args+=(--runner-home "$home")
  [[ ! "$uid" =~ ^[0-9]+$ ]] || args+=(--runtime-dir "$RUNTIME_ROOT/$uid")
  if "$HERE/verify.sh" "${args[@]}"; then
    :
  else
    FLEET_FAILURES=$((FLEET_FAILURES + 1))
    FAILED_LISTENERS+="$name "
    printf 'github-runner-fleet-listener: FAIL %s\n' "$name" >&2
  fi
done

# Probe the filesystem that actually holds runner work trees, not a layout assumed to exist.
DISK_PROBE=""
for candidate in ${LISTENER_HOMES[@]+"${LISTENER_HOMES[@]}"} "$RUNNER_ROOT/$SCOPE_SLUG" "$RUNNER_ROOT" /; do
  if [[ -n "$candidate" && -d "$candidate" ]]; then
    DISK_PROBE="$candidate"
    break
  fi
done
[[ -n "$DISK_PROBE" ]] || DISK_PROBE=/

python3 - "$RUNNER_COUNT" "$DISK_PROBE" "$DISTINCT_IDENTITIES" "$ISOLATION_POSTURE" <<'PY'
import json
import os
import pathlib
import shutil
import sys

runner_count = int(sys.argv[1])
disk_probe = pathlib.Path(sys.argv[2])
distinct_identities = int(sys.argv[3])
isolation_posture = sys.argv[4]
mem = {}
for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
    key, value = line.split(":", 1)
    mem[key] = int(value.strip().split()[0]) * 1024
disk = shutil.disk_usage(disk_probe)
result = {
    "runnerCount": runner_count,
    "perRunnerConcurrency": 1,
    "fleetConcurrency": runner_count,
    "distinctRunnerIdentities": distinct_identities,
    "isolationPosture": isolation_posture,
    "logicalCpuCount": os.cpu_count(),
    "loadAverage": list(os.getloadavg()),
    "memoryTotalBytes": mem["MemTotal"],
    "memoryAvailableBytes": mem["MemAvailable"],
    "memoryAvailablePercent": round(100 * mem["MemAvailable"] / mem["MemTotal"], 2),
    "runnerFilesystemPath": str(disk_probe),
    "runnerFilesystemTotalBytes": disk.total,
    "runnerFilesystemFreeBytes": disk.free,
    "runnerFilesystemFreePercent": round(100 * disk.free / disk.total, 2),
}
print("github-runner-fleet-headroom: " + json.dumps(result, sort_keys=True))
PY

if (( FLEET_FAILURES > 0 )); then
  printf 'github-runner-fleet-verify: FAIL scope=%s runners=%d failing-listeners=[%s]\n' \
    "$EXPECTED_SCOPE" "$RUNNER_COUNT" "${FAILED_LISTENERS% }" >&2
  exit 1
fi
printf 'github-runner-fleet-verify: PASS scope=%s runners=%d per-runner-concurrency=1 fleet-concurrency=%d isolation=%s\n' \
  "$EXPECTED_SCOPE" "$RUNNER_COUNT" "$RUNNER_COUNT" "$ISOLATION_POSTURE"
