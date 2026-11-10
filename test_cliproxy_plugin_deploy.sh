#!/usr/bin/env bash
# Offline fixtures only. CI executes the suite; use a named test for reproduction.
set -euo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 -I "$script_dir/test_cliproxy_plugin_deploy.py" "$@"
