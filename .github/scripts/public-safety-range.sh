#!/bin/sh
# Print the commit range for the public-safety gitleaks history scan.
#
# The history scan must cover every commit introduced here, and it must never
# silently scan zero commits: `git log -p` prints no diff for a merge commit,
# so scanning a bare merge commit (e.g. `-1 HEAD` on a pull_request merge ref)
# reports clean while a secret added-and-deleted inside the PR stays in
# published history. Likewise gitleaks exits 0 with "0 commits scanned" on an
# invalid revision range, so an unverified range that git rejects also passes
# silently. When no non-empty range can be derived, this prints `--all`
# (fail closed: scan everything) instead of an empty or invalid range.
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

# Print the merge-base range when it covers at least one commit, else --all.
# Shared by every event with no usable pushed range, so a new-branch push
# scans the branch commits instead of failing closed over every branch the
# checkout fetched (which reds the gate on unrelated branches' fixtures).
merge_base_range() {
  mb="$(git merge-base "$base" HEAD 2>/dev/null || true)"
  if [ -n "$mb" ] && [ "$(git rev-list "$mb..HEAD" --count 2>/dev/null || echo 0)" != "0" ]; then
    printf '%s\n' "$mb..HEAD"
  else
    printf '%s\n' "--all"
  fi
}

if [ "$event" = "pull_request" ] || [ "$event" = "merge_group" ]; then
  # HEAD is the merge ref. Take the range from the merge base so every PR
  # commit's diff is scanned even when the base advanced after the merge ref
  # was created (then the base is no longer an ancestor of HEAD, and an
  # ancestor test would fall through to scanning the merge commit alone).
  merge_base_range
else
  # push / workflow_dispatch: scan what was pushed. The before SHA must name
  # an object actually present in this checkout: a verify of the string shape
  # alone accepts any well-formed hex, so after a force-push or rebase it
  # yields `<old-sha>..HEAD`, which git rejects and gitleaks then "scans" as
  # zero commits with a clean verdict. Verify with cat-file, require a
  # non-empty range, and otherwise fall back to the merge-base range.
  if [ -n "$before" ] && [ "$before" != "$empty_tree" ] && \
      git cat-file -e "$before^{commit}" 2>/dev/null; then
    count="$(git rev-list "$before..HEAD" --count 2>/dev/null || echo invalid)"
    if [ "$count" != "invalid" ] && [ "$count" != "0" ]; then
      printf '%s\n' "$before..HEAD"
    else
      merge_base_range
    fi
  else
    # New branch (all-zero before), missing object, or no before at all:
    # scan the branch commits via the merge base, failing closed to --all.
    merge_base_range
  fi
fi
