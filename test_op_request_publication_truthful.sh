#!/usr/bin/env bash
# Offline known-bad baseline evidence, never a green CI gate. Run:
#   env -u BASH_ENV bash test_op_request_publication_truthful.sh
# Uses run scratch/TMPDIR. Fixture bytes remain hard-pinned to SHA256
# 30b9f2ff45d2cc54fa8cb52da7da7436ef49029e789d690c528a847035f30e6d.
# Expected: P0 provenance, P1 happy path and P2 validation pass; F1 cp, F2 chown,
# F3 chmod and F4 log append fail (pass=3 fail=4, exit 1). The fixed entrypoint
# shares this fail-closed harness but consumes the Builder's product manifest.
# A successful log append is not a storage-durability guarantee.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env -u BASH_ENV python3 -B "$HERE/tests/op_request_publication.py" truthful "$@"
