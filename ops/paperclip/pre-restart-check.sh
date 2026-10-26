#!/usr/bin/env bash
# Compatibility entry point: keep every activation pre-restart guard in one place.
# Install the paperclip-upgrade helper directory from the same reviewed revision.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 2
exec bash "$HERE/../../paperclip-upgrade/pre-restart-check.sh" "$@"
