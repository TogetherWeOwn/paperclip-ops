#!/usr/bin/env bash
# Builds the reviewed release tar for the Docker hygiene timer (case_b0e822f6).
# Run as a normal user in a clean clone, BEFORE crossing the privilege
# boundary: the root installer reads only this tar, never the checkout.
set -Eeuo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

SOURCE_REF=""
OUTPUT=""
TRUSTED_LINE="origin/main"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-ref) SOURCE_REF="${2:-}"; shift 2;;
    --output) OUTPUT="${2:-}"; shift 2;;
    --trusted-line) TRUSTED_LINE="${2:-}"; shift 2;;
    *) refuse "unknown argument: $1";;
  esac
done

[[ "$SOURCE_REF" =~ ^[0-9a-f]{40}$ ]] || refuse "--source-ref must be a 40-hex commit"
[[ -n "$OUTPUT" ]] || refuse "--output is required"
source_type=$(git --no-replace-objects cat-file -t "$SOURCE_REF" 2>/dev/null) || refuse "--source-ref does not name an available object"
[[ "$source_type" == commit ]] || refuse "--source-ref must name a commit object directly"

[[ -n "$TRUSTED_LINE" ]] || refuse "--trusted-line must not be empty"
git --no-replace-objects rev-parse -q --verify "$TRUSTED_LINE^{commit}" >/dev/null 2>&1 \
  || refuse "--trusted-line $TRUSTED_LINE does not resolve to a commit; fetch it first"
git --no-replace-objects merge-base --is-ancestor "$SOURCE_REF" "$TRUSTED_LINE" \
  || refuse "$SOURCE_REF is not an ancestor of $TRUSTED_LINE; it was never merged by review. Build a commit that is on that line, or pass --trusted-line if the reviewed line is elsewhere."

WORK_DIR=$(mktemp -d)
OUTPUT=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$OUTPUT")
trap 'rm -rf "$WORK_DIR"' EXIT
files=(
  docker_hygiene.sh
  systemd/install-docker-hygiene.sh
  systemd/paperclip-docker-hygiene.service
  systemd/paperclip-docker-hygiene.timer
  systemd/docker-hygiene.env.example
)
for source in "${files[@]}"; do
  git --no-replace-objects cat-file -e "$SOURCE_REF:$source" || refuse "reviewed commit is missing $source"
done
git --no-replace-objects archive --format=tar "$SOURCE_REF" "${files[@]}" | tar -x -C "$WORK_DIR"
printf '%s\n' "$SOURCE_REF" > "$WORK_DIR/REVISION"
(
  cd "$WORK_DIR"
  sha256sum "${files[@]}" REVISION > SHA256SUMS
  tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
    -cf "$OUTPUT" "${files[@]}" REVISION SHA256SUMS
)
printf 'BUILT %s from %s\n' "$OUTPUT" "$SOURCE_REF"
