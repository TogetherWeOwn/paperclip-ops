#!/usr/bin/env bash
# Compatibility entry point for the consolidated toolkit.
# The retained canonical gate covers the original SIGPIPE mechanism, match/miss,
# planted-violation and no-pipefail controls, plus broader producer and stale-
# exemption controls. Reuse that gate rather than duplicate its deliberately
# unsafe positive-control pipeline or introduce another scanner exemption.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HERE/case_c25c4d3f-sigpipe-verdict-gate.sh" "$@"
