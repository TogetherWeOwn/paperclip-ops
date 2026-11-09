#!/usr/bin/env bash
# Disposable hosted-runner experiment only. Never source a candidate installer.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]] || exit 2
pin=a4721aca27ffcbedfd79c553c78f8969c29bbc63
sha=2c57d38748159f2ae161d8de349861daa1acd14cde7d138819184f7e6720265d
out="$RUNNER_TEMP/simgit-evidence"
mkdir "$out"
work=$(mktemp -d "$RUNNER_TEMP/simgit-eval.XXXXXX")
mkdir "$work/download" "$work/source" "$work/home" "$work/build"
# No repository/host credential is passed to candidate compilation or execution.
clean=(env -i "PATH=$PATH" "HOME=$work/home" "CARGO_HOME=${CARGO_HOME:-$HOME/.cargo}" "RUSTUP_HOME=${RUSTUP_HOME:-$HOME/.rustup}")
python3 -I - "$out/identity.json" <<'PY'
import json, os, platform, sys
from pathlib import Path
keys = ('ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH', 'RUNNER_ENVIRONMENT',
        'EVALUATED_HEAD', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REPOSITORY')
Path(sys.argv[1]).write_text(json.dumps({**{k: os.environ.get(k) for k in keys},
                                      'kernel': platform.release()}, indent=2) + '\n')
PY
limitation() {
  printf '%s\n' "$1" > "$out/limitation.txt"
  printf '%s\n' 'NO-GO: matrix and lifecycle cases UNRUN; capability/build prerequisite unmet.' >> "$out/limitation.txt"
}
# Do not install host packages or format a substitute filesystem to force a pass.
if ! command -v fuse-overlayfs > /dev/null || ! command -v fusermount3 > /dev/null || [[ ! -r /dev/fuse || ! -w /dev/fuse ]]; then
  limitation 'Hosted image lacks executable fuse-overlayfs/fusermount3 or readable+writable /dev/fuse.'
  command -v fuse-overlayfs fusermount3 > "$out/backend-paths.txt" || true
  ls -l /dev/fuse >> "$out/backend-paths.txt" 2>&1 || true
  exit 0
fi
findmnt -T "$work" -J > "$out/backing-filesystem.json"
if [[ $(findmnt -T "$work" -n -o FSTYPE) != ext4 ]]; then
  limitation 'Assigned disposable runner temporary directory is not ext4; no alternate filesystem is provisioned.'
  exit 0
fi
fuse-overlayfs --version > "$out/backend-version.txt" 2>&1
sha256sum "$(command -v fuse-overlayfs)" "$(command -v fusermount3)" > "$out/backend-sha256.txt"
git --version > "$out/git-version.txt"
rustc --version > "$out/rust-version.txt"
printf '%s\n' "$pin" > "$out/candidate-commit.txt"
if ! curl --fail --location --silent --show-error --max-time 90 \
  "https://codeload.github.com/abendrothj/simgit/tar.gz/$pin" -o "$work/download/source.tar.gz"; then
  limitation 'Exact candidate source download failed; no substitution or retry.'
  exit 0
fi
printf '%s  %s\n' "$sha" "$work/download/source.tar.gz" | sha256sum --check > "$out/source-checksum.txt"
tar -xzf "$work/download/source.tar.gz" -C "$work/source" --strip-components=1
# Invoke from our checkout, not from the downloaded tree. Cargo executes third-party
# build inputs only here, in the authorized disposable runner, with a locked graph.
if ! "${clean[@]}" timeout 300 cargo build --release --locked \
  --manifest-path "$work/source/Cargo.toml" --target-dir "$work/build" --bin simgit \
  > "$out/build.log" 2>&1; then
  limitation 'Exact locked candidate build failed or exceeded five minutes; see build.log.'
  exit 0
fi
sha256sum "$work/build/release/simgit" > "$out/candidate-binary-sha256.txt"
"${clean[@]}" "${work}/build/release/simgit" --version > "$out/candidate-version.txt"
# Python's finally block performs native cleanup and records residuals. On a
# hard job timeout runner disposal is containment, never an explicit-cleanup PASS.
"${clean[@]}" timeout --signal=TERM --kill-after=30 600 python3 -I \
  ops/eval/simgit/evaluate.py --binary "$work/build/release/simgit" \
  --root "$work/data" --output "$out"
