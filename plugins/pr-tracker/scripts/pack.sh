#!/bin/sh
# Pack the prebuilt bundles the manifest entrypoints reference.
# The plugin is dependency-free ESM, so packing mirrors the source tree
# into dist/ (worker.js + ui.js entries re-export their modules). The host
# imports ./dist/worker.js out-of-process and ./dist/ui.js for slot mounts.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$ROOT/dist"
mkdir -p "$ROOT/dist"
cp -r "$ROOT/src" "$ROOT/dist/src"
cat > "$ROOT/dist/worker.js" <<'EOF'
export { setup } from "./src/worker/setup.js";
EOF
cat > "$ROOT/dist/ui.js" <<'EOF'
export { PrSidebarPage, PrDetailTab, PrCountsWidget } from "./src/ui/entries.js";
EOF
echo "packed $ROOT/dist"
