#!/usr/bin/env bash
# Offline evidence replay only; the versioned contract lives in
# docs/capacity-evidence.md. No capture, credentials or host operations.
set -uo pipefail

refuse_runtime() {
  local arg machine=false
  for arg in "$@"; do
    [[ "$arg" != --json ]] || machine=true
  done
  if "$machine"; then
    printf '%s\n' '{"schema_version":1,"verdict":"ERROR","code":"UNREADABLE_INPUT","retryable":false,"reason":"offline replay runtime unavailable","next_action":"Install Python 3 and keep the readable .py sibling with the .sh entrypoint."}'
  else
    printf '%s\n' 'coolify_capacity_headroom: refusing — UNREADABLE_INPUT: offline replay runtime unavailable; Python 3 and the readable .py sibling are required.' >&2
  fi
  exit 2
}

command -v python3 >/dev/null 2>&1 || refuse_runtime "$@"
HERE="$(dirname -- "${BASH_SOURCE[0]}")"
[[ -f "$HERE/coolify_capacity_headroom.py" && -r "$HERE/coolify_capacity_headroom.py" ]] || refuse_runtime "$@"
exec python3 -I -B "$HERE/coolify_capacity_headroom.py" "$@"
