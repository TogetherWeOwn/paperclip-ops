#!/usr/bin/env bash
# check-staged-drift.sh — gate the staged Tier-0 port against source drift (case_34675099).
#
# Diffs the staged files (tier-0/tier-0-quality-gate.yml, tier-0/semgrep-rules/)
# against the source at the recorded pin (tier-0/SOURCE), ignoring only the
# documented header-only delta on the workflow file. Exits non-zero naming the
# drifted file(s) plus the re-port command.
#
# Offline: git objects only, no network. Wired into the fast tier
# (scripts/verify-fast.sh), so every local run and the CI Offline suites job
# enforce it identically.
#
# Usage:
#   ./templates/org-github/check-staged-drift.sh [source-ref]
#     no arg      diff against the SOURCE_SHA pin (the merge gate)
#     <ref>       diff against that commit/branch instead, e.g. the PR #824
#                 head, to report the true state (clean or drifted)
set -euo pipefail

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$ROOT" ]] || { echo "staged-drift check: not inside a git work tree" >&2; exit 2; }
REL="${STAGE_DIR#$ROOT/}"

SOURCE_FILE="$STAGE_DIR/tier-0/SOURCE"
[[ -f "$SOURCE_FILE" ]] || { echo "staged-drift check: pin record missing: ${REL}/tier-0/SOURCE" >&2; exit 2; }
# shellcheck disable=SC1090: pin record is KEY=value lines by construction
PIN="$(grep -E '^SOURCE_SHA=[0-9a-f]{40}$' "$SOURCE_FILE" | cut -d= -f2 || true)"
[[ -n "$PIN" ]] || { echo "staged-drift check: no valid SOURCE_SHA in ${REL}/tier-0/SOURCE" >&2; exit 2; }

REF="${1:-$PIN}"
git -C "$ROOT" cat-file -e "$REF^{commit}" 2>/dev/null \
  || { echo "staged-drift check: source ref '$REF' is not a local git object (fetch full history; this check performs no network)" >&2; exit 2; }

SRC_WF='.github/workflows/tier-0-quality-gate.yml'
STAGED_WF="$STAGE_DIR/tier-0/tier-0-quality-gate.yml"
STAGED_RULES="$STAGE_DIR/tier-0/semgrep-rules"
[[ -f "$STAGED_WF" ]] || { echo "staged-drift check: staged gate missing: ${REL}/tier-0/tier-0-quality-gate.yml" >&2; exit 2; }

# The staged header must name the recorded pin, so the human-readable record
# and the machine-readable pin cannot disagree (a re-port that bumps one but
# not the other fails here, whichever ref the body is diffed against).
HEADER_PIN="$(grep -m1 -oE 'paperclip-ops@[0-9a-f]{40}' "$STAGED_WF" | cut -d@ -f2 || true)"
[[ "$HEADER_PIN" == "$PIN" ]] \
  || { echo "staged-drift check: DRIFTED: ${REL}/tier-0/tier-0-quality-gate.yml header names ${HEADER_PIN:-nothing}, pin record says $PIN" >&2; exit 1; }

SCRATCH="${PAPERCLIP_RUN_SCRATCH_DIR:-${RUNNER_TEMP:-/tmp}}"
TMP="$(mktemp -d "$SCRATCH/staged-drift.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

drifted=()
note() { drifted+=("$1"); echo "staged-drift check: drifted: $1" >&2; }

# Workflow file: strip the documented header-only delta by anchoring on the
# first `name:` line. Everything above it must be comments/blanks on BOTH
# sides — any code above the anchor means the header convention changed and
# this script needs updating, not a silent pass.
git -C "$ROOT" show "$REF:$SRC_WF" > "$TMP/source.yml" 2>/dev/null \
  || { note "${REL}/tier-0/tier-0-quality-gate.yml (missing in source at $REF)"; }
if [[ -f "$TMP/source.yml" ]]; then
  for f in "$TMP/source.yml" "$STAGED_WF"; do
    grep -q -m1 '^name:' "$f" \
      || { echo "staged-drift check: ERROR: no ^name: anchor in $f (header convention changed)" >&2; exit 2; }
    # Capture-first: piping sed straight into `grep -q` under `pipefail`
    # misreads a match as a miss when grep exits early (SIGPIPE poisons the
    # pipeline; see the case_c25c4d3f verdict gate). A herestring has no producer
    # to strand, so the verdict is exact.
    header="$(sed -n '/^name:/,$!p' "$f")"
    if grep -qEv '^\s*(#.*)?$' <<<"$header"; then
      echo "staged-drift check: ERROR: non-comment line above ^name: in $f (header convention changed)" >&2
      exit 2
    fi
  done
  sed -n '/^name:/,$p' "$TMP/source.yml" > "$TMP/source-body.yml"
  sed -n '/^name:/,$p' "$STAGED_WF" > "$TMP/staged-body.yml"
  cmp -s "$TMP/source-body.yml" "$TMP/staged-body.yml" \
    || note "${REL}/tier-0/tier-0-quality-gate.yml (body differs from source at $REF)"
fi

# Semgrep rules: verbatim port. Compare the union of filenames so an added or
# removed rule file on either side counts as drift, not a silent pass.
SRC_FILES="$(git -C "$ROOT" ls-tree -r --name-only "$REF" -- semgrep-rules/ 2>/dev/null | sed 's|^semgrep-rules/||' || true)"
STAGED_FILES="$(cd "$STAGED_RULES" 2>/dev/null && printf '%s\n' * || true)"
ALL_FILES="$(printf '%s\n%s' "$SRC_FILES" "$STAGED_FILES" | grep . | sort -u)"
[[ -n "$ALL_FILES" ]] || { echo "staged-drift check: ERROR: no semgrep rule files on either side" >&2; exit 2; }
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  if ! git -C "$ROOT" show "$REF:semgrep-rules/$f" > "$TMP/src-rule" 2>/dev/null; then
    note "${REL}/tier-0/semgrep-rules/$f (staged but missing in source at $REF)"
  elif [[ ! -f "$STAGED_RULES/$f" ]]; then
    note "${REL}/tier-0/semgrep-rules/$f (missing staged, present in source at $REF)"
  elif ! cmp -s "$TMP/src-rule" "$STAGED_RULES/$f"; then
    note "${REL}/tier-0/semgrep-rules/$f (differs from source at $REF)"
  fi
done <<< "$ALL_FILES"

if [[ "${#drifted[@]}" -gt 0 ]]; then
  cat >&2 <<EOF
staged-drift check: STAGED PORT DRIFTED from source at $REF (${#drifted[@]} file(s)).
Re-port from the recorded pin, then re-apply the documented header-only delta:
  PIN=$PIN
  git show \$PIN:$SRC_WF > /tmp/src-gate.yml  # re-apply the staged org header, then move into ${REL}/tier-0/tier-0-quality-gate.yml
  for f in \$(git ls-tree -r --name-only \$PIN -- semgrep-rules/ | sed 's|^semgrep-rules/||'); do git show \$PIN:semgrep-rules/\$f > ${REL}/tier-0/semgrep-rules/\$f; done
  (then bump SOURCE_SHA if the pin itself moved)
EOF
  exit 1
fi
echo "staged-drift check: OK (staged matches source at $REF)"
