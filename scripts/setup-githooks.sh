#!/usr/bin/env bash
# setup-githooks.sh — point core.hooksPath at the versioned .githooks dir.
# Safe to re-run; touches only this repo's git config.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
git config core.hooksPath .githooks
echo "githooks enabled: $(git config core.hooksPath)"
ls -la .githooks/
