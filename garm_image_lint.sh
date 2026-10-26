#!/usr/bin/env bash
# Offline GARM Dockerfile policy gate (case_696525a4). Python stdlib parser joins
# Docker continuations before enforcing the nine GARM-* minimal-base rules.
# This checked-in structural subset needs no hadolint binary, credentials or
# network. It never builds, pulls or runs an image or executes Dockerfile text.
# See docs/garm-image-lint.md for rules, supported syntax and the --json contract.
# Exit 0 pass; 1 rule failure; 2 missing input/profile or usage error.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/garm_image_lint.py" "$@"
