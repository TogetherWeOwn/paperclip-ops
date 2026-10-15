#!/usr/bin/env bash
# ===========================================================================
# transcript_mode_audit_check.sh — transcript sidecar mode-drift audit,
# host-cron replacement (case_4ea7d92e) for the agent routine of the same name.
#
# Wraps the existing detector (case_d9917646) unchanged:
#   <company>/ops/case_d9917646/transcript-mode-audit.sh --root "$case_d376bf4e_TRANSCRIPT_ROOT"
# (container default /paperclip/.claude/projects; on the host, the data dir path)
# Exit 0 = clean (no card, ever). Exit 1 = drift found. Exit 2 = indeterminate
# -- NEVER treated as clean, always surfaced.
# ===========================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
# shellcheck source=lib/post_finding.sh
source "${SCRIPT_DIR}/lib/post_finding.sh"

OPS_DIR="${case_d376bf4e_OPS_DIR:-/paperclip/instances/default/companies/${PAPERCLIP_COMPANY_ID}/ops}"
AUDIT_SCRIPT="${OPS_DIR}/case_d9917646/transcript-mode-audit.sh"
AUDIT_ROOT="${case_d376bf4e_TRANSCRIPT_ROOT:-/paperclip/.claude/projects}"
CISO_AGENT_ID="${PAPERCLIP_CISO_AGENT_ID:-00000000-0000-4000-8000-000000000022}"

TAG="[case_d376bf4e-transcript-mode-audit]"

if [[ ! -x "$AUDIT_SCRIPT" ]]; then
  body_file="$(mktemp)"
  trap 'rm -f "$body_file"' EXIT
  echo "transcript-mode-audit.sh not found or not executable at ${AUDIT_SCRIPT} -- treat as INDETERMINATE, not clean." > "$body_file"
  post_finding "$TAG" "audit script missing" "$body_file"
  exit 0
fi

audit_rc=0
audit_out="$("$AUDIT_SCRIPT" --root "$AUDIT_ROOT" 2>&1)" || audit_rc=$?

if [[ $audit_rc -eq 0 ]]; then
  exit 0
fi

# --fix is a chmod sweep. On node7b1ff338 host uid 1000 is the container's `node`, the
# owner of these files, so the host-side chmod is what the container sees.
fix_out=""
fix_rc=0
if [[ $audit_rc -eq 1 ]]; then
  fix_out="$("$AUDIT_SCRIPT" --root "$AUDIT_ROOT" --fix 2>&1)" || fix_rc=$?
fi

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
{
  if [[ $audit_rc -eq 1 ]]; then
    echo "Transcript sidecar mode drift (exit 1). @[Chief Security & Trust Officer / CISO](agent://${CISO_AGENT_ID})"
  else
    echo "Transcript mode audit INDETERMINATE (exit ${audit_rc}) -- root could not be measured, NOT reported as clean. @[Chief Security & Trust Officer / CISO](agent://${CISO_AGENT_ID})"
  fi
  echo
  echo '```'
  echo "$audit_out"
  echo '```'
  if [[ $audit_rc -eq 1 ]]; then
    echo
    echo "Ran --fix to contain current drift (writes an owner-only rollback snapshot), exit ${fix_rc}. Output:"
    echo '```'
    echo "$fix_out"
    echo '```'
  fi
  echo
  echo "Durable writer fix remains case_7e1fcd78. Never print file contents; only counts/verdict above."
} > "$body_file"

post_finding "$TAG" "transcript mode drift" "$body_file"
exit 0
