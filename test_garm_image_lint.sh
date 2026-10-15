#!/usr/bin/env bash
# Offline fixtures, gate-deletion mutants, profile assertions and synthetic
# archive layout test for case_696525a4. No image, network or host operations.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/test_garm_image_lint.py" "$@"
