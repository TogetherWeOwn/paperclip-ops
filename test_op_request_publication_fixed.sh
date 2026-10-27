#!/usr/bin/env bash
# Offline fixed-source acceptance. Run:
#   env -u BASH_ENV bash test_op_request_publication_fixed.sh
# Uses run scratch/TMPDIR. Requires the Builder's strict one-entry product
# ops/operator-safety/wrappers/op-request.sha256 manifest BEFORE any execution.
# For verification only: --source /scratch/candidate/op-request (with manifest).
# No product source hash is pinned in this suite.
# Append success is not fsync/crash durability; output loss is not absence proof.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env -u BASH_ENV python3 -B "$HERE/tests/op_request_publication.py" fixed "$@"
