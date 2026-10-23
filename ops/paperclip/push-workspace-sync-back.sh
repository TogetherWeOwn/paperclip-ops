#!/usr/bin/env bash
# Independent alert observer. Never install in the SSH/sync execution chain.
# stdin: sanitized measurement JSON; stdout: classification JSON.
# Exit 0 after delivery, INCLUDING oversized/unknown observations. Nonzero means
# observer/delivery failure only, never permission to stop or alter sync-back.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

report="$(python3 "$HERE/workspace_sync_back_signal.py" 2>/dev/null)" || {
  printf '%s\n' 'workspace-sync-back: observer_failed; retry independent observation' >&2
  exit 1
}
printf '%s\n' "$report"

# Fixed helper and endpoint; never forward raw observation fields or parser errors.
# shellcheck source=../case_658bf6b9/_gatus_push.sh
if ! source "$HERE/../case_658bf6b9/_gatus_push.sh" 2>/dev/null; then
  printf '%s\n' 'workspace-sync-back: delivery_unavailable; restore shared alert helper' >&2
  exit 1
fi
success="$(python3 -c 'import json,sys; print(str(json.load(sys.stdin)["gatus"]["success"]).lower())' <<<"$report")" || exit 1
message="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["gatus"]["error"])' <<<"$report")" || exit 1
if ! gatus_push platform_workspace-sync-back "$success" "$message" 2>/dev/null; then
  printf '%s\n' 'workspace-sync-back: delivery_failed; check private Gatus configuration and retry observation' >&2
  exit 1
fi
printf '%s\n' 'workspace-sync-back: delivered' >&2
exit 0
