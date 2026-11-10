#!/usr/bin/env bash
# Source-only wrapper; operational adapters are deliberately not bundled.
set -uo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
exec python3 -I "$script_dir/cliproxy_plugin_deploy.py" "$@"
