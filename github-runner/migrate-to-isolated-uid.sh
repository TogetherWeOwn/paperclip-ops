#!/usr/bin/env bash
# Host operator only: no registration token, deregistration, service stop or start.
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/isolated-migration.py" "$@"
