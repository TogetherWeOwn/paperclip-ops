#!/usr/bin/env bash
set -Eeuo pipefail

[[ -n "${CREDENTIALS_DIRECTORY:-}" ]] || {
  printf 'REFUSED: systemd credential directory is unavailable\n' >&2
  exit 2
}
CREDENTIAL_PATH="${CREDENTIALS_DIRECTORY}/ci-node93f4bccf-guard-gh-token"
[[ -r "$CREDENTIAL_PATH" ]] || {
  printf 'REFUSED: systemd home-PC guard credential is unavailable\n' >&2
  exit 2
}
# The token travels in the child environment only, never on argv:
# /proc/*/cmdline is world-readable and this host is shared.
export GH_TOKEN
GH_TOKEN="$(cat "$CREDENTIAL_PATH")"
[[ -n "$GH_TOKEN" ]] || {
  printf 'REFUSED: home-PC guard credential is empty\n' >&2
  exit 2
}
# Resolve only the guard beside this reviewed wrapper, not an env-selected file.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$SCRIPT_DIR/ci_node93f4bccf_guard.py" --mode apply
