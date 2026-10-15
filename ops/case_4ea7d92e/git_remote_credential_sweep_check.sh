#!/usr/bin/env bash
# ===========================================================================
# git_remote_credential_sweep_check.sh — host-cron replacement (case_4ea7d92e) for
# the "Git remote-URL credential sweep" agent routine.
#
# Wraps the existing, unchanged detector at the root of this repo:
#   ./git_remote_credential_scan.sh [ROOT ...]  (exit 0 clean / 1 findings / 2 error)
#
# DETECTION ONLY. A finding is posted to the board and nothing else happens:
# no revoke, no scrub, no `git remote set-url`. Revoking or rotating a
# credential is owner-reserved (standing no-rotation rule), so the response to
# a finding is a CISO decision, not something a timer does unattended. An
# earlier revision ran gh_embedded_token_remediate.py (revoke -> scrub) on
# every finding; that path is gone, not switched off.
#
# Roots come from case_d376bf4e_GIT_SCAN_ROOTS (colon-separated). On the host they
# must be host paths; the detector's built-in defaults are container paths.
# A configured root that does not exist is a finding: the detector skips it
# silently, and a sweep that quietly looked in fewer places reports CLEAN.
#
# Never prints a credential value. Exit 2 (usage/internal error, including
# "no root exists") and INDET lines (unreadable configs) are NOT reported as
# clean -- they are surfaced as findings.
# ===========================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." &>/dev/null && pwd)"
# shellcheck source=lib/post_finding.sh
source "${SCRIPT_DIR}/lib/post_finding.sh"

SCAN_SCRIPT="${REPO_ROOT}/git_remote_credential_scan.sh"
TAG="[case_d376bf4e-git-remote-credential-sweep]"

roots=()
if [[ -n "${case_d376bf4e_GIT_SCAN_ROOTS:-}" ]]; then
  IFS=':' read -r -a roots <<<"$case_d376bf4e_GIT_SCAN_ROOTS"
fi

# No configured roots at all: the detector would fall back to its built-in
# container paths, which do not exist on the host, so the sweep would measure
# nothing. That is a finding, not clean (case_3cd9412c N1).
if [[ ${#roots[@]} -eq 0 ]]; then
  body_file="$(mktemp)"
  trap 'rm -f "$body_file"' EXIT
  echo "case_d376bf4e_GIT_SCAN_ROOTS is unset or empty -- no scan roots configured (see INSTALL.md §2). The detector's built-in defaults are container paths that do not exist on the host -- INDETERMINATE, not clean." > "$body_file"
  post_finding "$TAG" "no scan roots configured" "$body_file"
  exit 0
fi

# Roots the detector will skip are roots it never measured: a root that does
# not exist, and an existing root holding no `.git/config` at all (the
# detector then prints `CLEAN: scanned 0`, which is "looked nowhere", not
# "looked and found nothing" — fail-open, case_3cd9412c B2). Both post.
missing_roots=()
empty_roots=()
for r in ${roots[@]+"${roots[@]}"}; do
  if [[ ! -d "$r" ]]; then
    missing_roots+=("$r")
  elif ! find "$r" -type f -path '*/.git/config' -print -quit 2>/dev/null | grep -q .; then
    empty_roots+=("$r")
  fi
done

if [[ ! -x "$SCAN_SCRIPT" ]]; then
  body_file="$(mktemp)"
  trap 'rm -f "$body_file"' EXIT
  echo "git_remote_credential_scan.sh not found or not executable at ${SCAN_SCRIPT} -- treat as INDETERMINATE, not clean." > "$body_file"
  post_finding "$TAG" "scan script missing" "$body_file"
  exit 0
fi

scan_rc=0
scan_out="$("$SCAN_SCRIPT" ${roots[@]+"${roots[@]}"} 2>&1)" || scan_rc=$?

# Only the detector's own result lines go to the board. Its trailer advises
# running the revoking remediator, which this wrapper must not relay as the
# next step.
result_lines="$(printf '%s\n' "$scan_out" | grep -E '^(FINDING|INDET) ' || true)"

if [[ $scan_rc -eq 0 && -z "$result_lines" && ${#missing_roots[@]} -eq 0 && ${#empty_roots[@]} -eq 0 ]]; then
  exit 0
fi

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT

{
  if [[ $scan_rc -eq 1 ]]; then
    echo "Git remote-URL credential sweep found embedded credentials (exit 1)."
  elif [[ $scan_rc -eq 0 ]]; then
    echo "Git remote-URL credential sweep finished (exit 0) but its coverage is INCOMPLETE -- NOT reported as clean."
  else
    echo "git remote credential scan COULD NOT RUN (exit ${scan_rc}) -- NOT reported as clean."
  fi
  echo
  if [[ ${#missing_roots[@]} -gt 0 ]]; then
    echo "Configured scan roots that do not exist (skipped by the detector):"
    printf -- '- %s\n' "${missing_roots[@]}"
    echo
  fi
  if [[ ${#empty_roots[@]} -gt 0 ]]; then
    echo "Configured scan roots holding no .git/config at all (the detector scanned 0 configs there -- looked nowhere, not clean):"
    printf -- '- %s\n' "${empty_roots[@]}"
    echo
  fi
  echo '```'
  if [[ $scan_rc -eq 0 || $scan_rc -eq 1 ]]; then
    echo "${result_lines:-(no FINDING/INDET lines)}"
  else
    echo "$scan_out"
  fi
  echo '```'
  echo
  echo "Detection only: this check changed nothing. Revoking or rotating the credential is owner-reserved; the CISO decides the response."
  echo "Never a credential value above -- only repo path, line number, kind, host and perms."
} > "$body_file"

post_finding "$TAG" "embedded git remote credential" "$body_file"
exit 0
