#!/usr/bin/env bash
# Offline fixtures, gate-deletion mutants, profile assertions and synthetic
# archive layout test for case_696525a4. No image, network or host operations.
set -euo pipefail
# The unfiltered CI path includes the independent scan-evidence contract.
# Explicit unittest selectors still forward unchanged to the original suite.
if (( $# == 0 )); then
  python3 -B "$(dirname "${BASH_SOURCE[0]}")/test_garm_image_lint_scan_evidence_v1.py"
fi
exec python3 "$(dirname "${BASH_SOURCE[0]}")/test_garm_image_lint.py" "$@"
