#!/usr/bin/env bash
# Restore the operator-toolkit-vendored files at their repo-relative paths.
#
# Slice 1 of the repo split deleted the toolkit-identical paths from this
# repo; the pinned toolkit release remains the source of truth for them.
# Anything that still needs those files at their old paths (CI jobs, host
# deploy scripts) runs this first.
#
# The pin is a commit SHA plus a tarball checksum (vendor/toolkit-pins.txt),
# never the mutable tag: re-pushing a tag cannot change what this restores.
# Restored files are byte-identical to the pinned commit by verification.
#
# Usage: bash scripts/sync-toolkit.sh [--tag vX.Y.Z] [--commit <sha>]
#        (default: main-533c7ae2 resolved through the pins file; a full-SHA
#        fallback label, since no release tag contains the fix yet)
# Env:   TOOLKIT_TAG overrides the default tag; TOOLKIT_COMMIT overrides the
#        commit lookup; TOOLKIT_SHA256 overrides the expected checksum;
#        TOOLKIT_URL overrides the archive base URL.
set -euo pipefail

TAG="${TOOLKIT_TAG:-main-533c7ae2}"
COMMIT="${TOOLKIT_COMMIT:-}"
SHA256="${TOOLKIT_SHA256:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)
      TAG="${2:?--tag needs a value}"; shift 2 ;;
    --tag=*)
      TAG="${1#--tag=}"; shift ;;
    --commit)
      COMMIT="${2:?--commit needs a value}"; shift 2 ;;
    --commit=*)
      COMMIT="${1#--commit=}"; shift ;;
    *)
      echo "sync-toolkit: unknown arg $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/vendor/toolkit-files.txt"
PINS="$ROOT/vendor/toolkit-pins.txt"
[ -f "$MANIFEST" ] || { echo "sync-toolkit: manifest missing: $MANIFEST" >&2; exit 1; }
[ -f "$PINS" ] || { echo "sync-toolkit: pins missing: $PINS" >&2; exit 1; }

if [ -z "$COMMIT" ]; then
  COMMIT="$(awk -v tag="$TAG" '$1 == tag { print $2 }' "$PINS")"
  [ -n "$COMMIT" ] || { echo "sync-toolkit: no pin for tag $TAG in $PINS" >&2; exit 1; }
fi
if [ -z "$SHA256" ]; then
  SHA256="$(awk -v commit="$COMMIT" '$2 == commit { print $3 }' "$PINS")"
  [ -n "$SHA256" ] || { echo "sync-toolkit: no checksum for commit $COMMIT in $PINS" >&2; exit 1; }
fi

BASE_URL="${TOOLKIT_URL:-https://github.com/TogetherWeOwn/paperclip-operator-toolkit/archive}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

declare -A archive_roots
fetch_archive() {
  local tag=$1 commit=$2 checksum=$3 dir
  dir="$(mktemp -d "$tmp/archive.XXXXXX")"
  [ -n "$commit" ] && [ -n "$checksum" ] \
    || { echo "sync-toolkit: no immutable pin for tag $tag" >&2; exit 1; }
  mkdir -p "$dir"
  # Bounded download (case_e107fefc): a hung archive endpoint must fail the sync,
  # never stall the CI job that restores the vendored tree. Retry transient
  # GitHub 5xx responses without allowing retries to extend the time bound.
  curl -fsSL --connect-timeout 10 --max-time 120 --retry 3 --retry-delay 1 \
    --retry-max-time 30 "$BASE_URL/$commit.tar.gz" -o "$dir/toolkit.tgz"
  printf '%s  %s\n' "$checksum" "$dir/toolkit.tgz" | sha256sum -c --status - \
    || { echo "sync-toolkit: checksum mismatch for commit $commit" >&2; exit 1; }
  tar xzf "$dir/toolkit.tgz" -C "$dir"
  local src="$dir/paperclip-operator-toolkit-$commit"
  [ -f "$src/LICENSE" ] || { echo "sync-toolkit: bad tarball, LICENSE missing for $commit" >&2; exit 1; }
  archive_roots["$tag"]="$src"
}
fetch_archive "$TAG" "$COMMIT" "$SHA256"
# A few retained private consumers need pre-scrub bytes. These are explicit
# manifest exceptions, never a fallback after a failed default download/pin.
# Validate every exception archive before copying any destination.
while IFS= read -r source_tag; do
  [ "$source_tag" = "$TAG" ] && continue
  source_commit="$(awk -v tag="$source_tag" '$1 == tag { print $2 }' "$PINS")"
  source_checksum="$(awk -v tag="$source_tag" '$1 == tag { print $3 }' "$PINS")"
  fetch_archive "$source_tag" "$source_commit" "$source_checksum"
done < <(awk -F'|' 'NF == 3 { print $3 }' "$MANIFEST" | sort -u)

want="$(grep -c . "$MANIFEST")"
got=0
skipped=0
compatibility=0
# case_a2b959d4: never overwrite a TRACKED file. A handful of manifest paths are
# knowingly tracked here (diverged from the tag, e.g. a spec kept in-tree);
# restoring over them would test the tag instead of the tree and silently
# discard PR edits.
# One listing up front; outside a git checkout the list is empty and every
# dest restores as before.
tracked_list="$tmp/tracked-files"
git -C "$ROOT" ls-files >"$tracked_list" 2>/dev/null || : >"$tracked_list"
while IFS= read -r line; do
  [ -n "$line" ] || continue
  # `dest`, `dest|src`, or an explicitly pinned private-compatibility
  # exception `dest|src|tag`. The tracked-file guard still keys on dest.
  IFS='|' read -r dest s source_tag extra <<< "$line"
  [ -z "$extra" ] || { echo "sync-toolkit: invalid manifest entry $line" >&2; exit 1; }
  [ -n "$s" ] || s="$dest"
  [ -n "$source_tag" ] || source_tag="$TAG"
  src="${archive_roots[$source_tag]:?unresolved manifest archive}"
  if grep -Fxq "$dest" "$tracked_list"; then
    echo "sync-toolkit: SKIP tracked $dest (tests the tree, not the tag)" >&2
    skipped=$((skipped + 1))
    continue
  fi
  if [ ! -f "$src/$s" ]; then
    echo "sync-toolkit: $source_tag has no $s (pin drift?)" >&2
    exit 1
  fi
  mkdir -p "$ROOT/$(dirname "$dest")"
  cp "$src/$s" "$ROOT/$dest"
  got=$((got + 1))
  [ "$source_tag" = "$TAG" ] || compatibility=$((compatibility + 1))
done < "$MANIFEST"

# case_a2b959d4 (Paperclip Review on PR #753): restored files are UNTRACKED, and
# the loop above skips tracked ones — so a manifest path committed back by
# accident would be tested stale with nothing failing. Gate on it here so
# every CI sync step is covered (local runs get it via the verify-fast
# parse gate, which calls the same script).
bash "$ROOT/scripts/check-toolkit-vendored-tracked.sh"

echo "sync-toolkit: restored $got/$want files (default $TAG $COMMIT; $compatibility explicit compatibility; $skipped tracked skipped)"
