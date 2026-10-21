#!/usr/bin/env bash
# secret-scan.sh -- the repo's "no secrets, ever" assertion as a standalone
# script (case_d5bc7950, CI standard rule 6 / addition 11: secrets ALWAYS run).
# Scans TRACKED files only: anything .gitignore covers is by definition not in
# the repo, and scanning the working tree would fail on an operator's local
# scratch files. Suites assemble token-shaped canaries from fragments at
# runtime precisely so this scan does not trip on the suite.
# Exit 0 clean, 1 finding. Offline, git + grep only.
set -uo pipefail

rc=0
# git grep searches tracked files only, which is the scope we want.
# Live GitHub credentials of every prefix, and private key material.
if git grep -nIE '(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})'; then
  echo "::error::a GitHub-token-shaped string is committed"; rc=1
fi
if git grep -nIE 'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY'; then
  echo "::error::private key material is committed"; rc=1
fi
# case_f3ff4554 (blocker-ledger 'security' class): provider-shaped secrets the
# scan used to miss. Real shapes only — long numeric channel ids and long
# tokens — so the short test-fixture canaries suites plant at runtime
# (discord .../123/abc) never trip the scan.
if git grep -nE 'discord(app)?\.com/api/webhooks/[0-9]{10,}/[A-Za-z0-9_-]{20,}'; then
  echo "::error::a Discord-webhook-shaped URL is committed"; rc=1
fi
if git grep -nE '(sk_live_[A-Za-z0-9]{10,}|rk_live_[A-Za-z0-9]{10,})'; then
  echo "::error::a Stripe-live-key-shaped string is committed"; rc=1
fi
# The .gitignore entries other steps and the tools depend on. Losing
# one is silent until the day something lands in a commit.
# __pycache__/ and *.pyc are on this list because the parse gate in
# the Offline job COMPILES this repo's Python on every run and the
# broker suite imports host-ops/broker.py — so a runner reliably has
# untracked .pyc on disk. A .pyc carries its source, and this repo's
# Python reads a management key. (case_04f24e2d)
for pat in '*.env' '*.pem' '*.key' '*.jsonl' '__pycache__/' '*.pyc'; do
  grep -qxF "$pat" .gitignore || { echo "::error::.gitignore no longer covers $pat"; rc=1; }
done
[ $rc -eq 0 ] && echo "secret scan: clean"
exit $rc
