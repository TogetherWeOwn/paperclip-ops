#!/usr/bin/env bash
# Fail if any toolkit-manifest path beyond the tracked allowlist is tracked.
#
# case_a2b959d4 (Paperclip Review on PR #753): sync-toolkit.sh restores ~280
# manifest files as UNTRACKED and skips tracked ones — so a manifest path
# committed back by accident would be tested stale with nothing failing.
# This gate fails instead. It is fast and hermetic (git reads only, no
# network), so both the verify-fast parse gate and sync-toolkit.sh call it:
# every local run, the pre-push hook, and all CI sync steps are covered.
# Outside a git checkout the tracked list is empty and this passes vacuously.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/vendor/toolkit-files.txt"
ALLOWLIST="$ROOT/vendor/toolkit-tracked-allowlist.txt"
[ -f "$MANIFEST" ] || { echo "vendored-tracked check: manifest missing: $MANIFEST" >&2; exit 1; }
[ -f "$ALLOWLIST" ] || { echo "vendored-tracked check: allowlist missing: $ALLOWLIST" >&2; exit 1; }

extras="$(comm -23 \
  <(comm -12 \
    <(git -C "$ROOT" ls-files 2>/dev/null | sort -u || true) \
    <(sed 's/|.*//' "$MANIFEST" | sort -u)) \
  <(grep -v '^[[:space:]]*#' "$ALLOWLIST" | grep . | sort -u) || true)"
if [ -n "$extras" ]; then
  echo "vendored-tracked check: TRACKED toolkit-vendored files outside vendor/toolkit-tracked-allowlist.txt:" >&2
  echo "$extras" | sed 's/^/vendored-tracked check:   /' >&2
  echo "vendored-tracked check: remove them from git, or extend the allowlist if the divergence is deliberate" >&2
  exit 1
fi

# case_a2b959d4 follow-up (PR #753 review): restored files must also be INVISIBLE,
# not just untracked — a fresh checkout shows ~280 of them after verify-fast.
# Every manifest dest must be tracked (allowlist above) or ignored
# (.gitignore generated block); anything else pollutes `git status` and can be
# committed back by accident.
# Outside a git checkout there is no index and no ignore rules, so this half
# passes vacuously like the tracked half above (the header contract).
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
untracked_manifest="$(comm -23 \
  <(sed 's/|.*//' "$MANIFEST" | sort -u) \
  <(git -C "$ROOT" ls-files 2>/dev/null | sort -u || true))"
if [ -n "$untracked_manifest" ]; then
  ignored="$(printf '%s\n' "$untracked_manifest" | git -C "$ROOT" check-ignore --stdin 2>/dev/null || true)"
  offenders="$(comm -23 \
    <(printf '%s\n' "$untracked_manifest") \
    <(printf '%s\n' "$ignored" | sort -u) || true)"
  if [ -n "$offenders" ]; then
    echo "vendored-tracked check: manifest files neither tracked nor ignored (regenerate the .gitignore block):" >&2
    echo "$offenders" | head -20 | sed 's/^/vendored-tracked check:   /' >&2
    exit 1
  fi
fi
fi
echo "vendored-tracked check: OK"
