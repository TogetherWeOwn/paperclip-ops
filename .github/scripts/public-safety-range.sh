#!/bin/sh
# Print the commit range for the public-safety gitleaks history scan.
#
# The history scan must cover every commit introduced here, and it must never
# silently scan zero commits: `git log -p` prints no diff for a merge commit,
# so scanning a bare merge commit (e.g. `-1 HEAD` on a pull_request merge ref)
# reports clean while a secret added-and-deleted inside the PR stays in
# published history. When no non-empty range can be derived, this prints
# `--all` (fail closed: scan everything) instead of an empty range.
#
# Usage: public-safety-range.sh [event-name] [before-sha]
#   event-name defaults to $GITHUB_EVENT_NAME (or pull_request), before-sha to
#   $GITHUB_EVENT_BEFORE (empty unless a push event set it).
#   HISTORY_SCAN_BASE overrides the base ref (default origin/main); tests point
#   it at a local ref.
set -eu

event="${1:-${GITHUB_EVENT_NAME:-pull_request}}"
before="${2:-${GITHUB_EVENT_BEFORE:-}}"
base="${HISTORY_SCAN_BASE:-origin/main}"
empty_tree="0000000000000000000000000000000000000000"

if [ "$event" = "pull_request" ] || [ "$event" = "merge_group" ]; then
  # HEAD is the merge ref. Take the range from the merge base so every PR
  # commit's diff is scanned even when the base advanced after the merge ref
  # was created (then the base is no longer an ancestor of HEAD, and an
  # ancestor test would fall through to scanning the merge commit alone).
  mb="$(git merge-base "$base" HEAD 2>/dev/null || true)"
  if [ -n "$mb" ] && [ "$(git rev-list "$mb..HEAD" --count 2>/dev/null || echo 0)" != "0" ]; then
    printf '%s\n' "$mb..HEAD"
  else
    printf '%s\n' "--all"
  fi
else
  # push / workflow_dispatch: scan what was pushed. A new branch (all-zero
  # before) or a missing ref cannot yield a range, so scan everything.
  if [ -n "$before" ] && [ "$before" != "$empty_tree" ] && \
      git rev-parse --verify "$before" >/dev/null 2>&1; then
    printf '%s\n' "$before..HEAD"
  else
    printf '%s\n' "--all"
  fi
fi
