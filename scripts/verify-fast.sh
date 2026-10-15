#!/usr/bin/env bash
# verify-fast.sh — the fast tier for paperclip-ops (case_a2b959d4).
#
# One command, one source of truth: `make verify-fast` runs this script, and
# CI's Offline suites job calls this same script, so local and CI run the
# identical fast checks. Target: under 3 minutes with a warm cache.
#
# FAST TIER (must pass locally before any push):
#   --parse-only : bash -n, node --check, python py_compile, ESM check
#   full (default): parse + fast hermetic unit/contract gates (no DB, no
#     network beyond loopback, no credentials, no browser, no -race)
#
# HEAVY TIER (CI is authoritative; never claim locally):
#   service-container suites (Postgres), Playwright/browser, staging e2e,
#   -race, mutation gates, broker/dispatch/capture/mcp suites. The PR
#   readiness block reports these from CI.
#
# Usage:
#   scripts/verify-fast.sh [--parse-only] [--list]
#   make verify-fast
#
# Worktree setup: scratch worktrees have no node_modules. The translator
# fixture needs typescript; the script installs the pinned compiler with the
# shared npm cache (npm install --no-save --ignore-scripts with
# npm_config_cache). Rust is N/A in this repo (no cargo workspace).
set -euo pipefail

# case_a2b959d4 hook trap: under a real `git push`, the pre-push hook inherits an
# ABSOLUTE GIT_DIR pointing at this repo. Every suite below that shells `git`
# in a fixture dir (`git -C`, bare `git` with cwd) would then operate on THIS
# repo instead of its fixture — measured 2026-10-06: muse-stop-guard's
# findUnsavedWork fixture committed `base`+a.txt into the real tree and failed
# its own assertion, refusing every push with the hook enabled. Suspending the
# three repo-locating variables restores normal discovery (cwd) for all
# children; no step here relies on an inherited repo location.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

MODE="full"
if [[ "${1:-}" == "--parse-only" ]]; then
  MODE="parse"
elif [[ "${1:-}" == "--list" ]]; then
  echo "parse: bash -n, node --check, python py_compile, ESM check, vendored-tracked gate"
  echo "full: parse + external-disclosure, muse-stop-guard, disposition-stop-guard,"
  echo "      ci-required-checks-audit, ci-gating, private-mutation-harness, readme-script-index,"
  echo "      public-comment-hygiene,"
  echo "      case_b2acfa5c-runner-contract, dropchannel-fixture, approval-timeout-probe, acceptance-transport reply, gh-access-dispatch,"
  echo "      upstream pre-push hook,"
  echo "      alert-queue, watchdog-snapshot-builder, fleet-run-stall, assignment-host-source,"
  echo "      review-error-rerequest, backlog-promoter, board-jobs-bounds, focus-gate-bounds, pr-supply, flow-guard, host-ok, connection-intent-sweeper, hide-noop-reconcile,"
  echo "      guard-state (atomic-state, stuck-run-state, idle-waiter-state, owner-guard-state), feed-contract,"
  echo "      survival-atomic-write, corpus atomic-write, observational-collect-atomic-write, pr-hygiene owner-match, live-run guard, review-route, arm-import, merge-survival-atomic-write, operator-workspace reconciler and sweep, operator copy-risk gate,"
  echo "      translator-fixture (pinned tsc), deploy-window wrapper, deploy-window manifest gate, staged-tier0-drift gate,"
  echo "      org-snapshot offline suite"
  exit 0
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--parse-only] [--list]" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

rc=0
fail() { echo "::error::$1"; rc=1; }

echo "=== verify-fast: parse gate (bash/node/python/esm) ==="
# Slice-1 cutover deleted whole globs (e.g. scripts/*.js); a non-matching glob
# must expand to nothing, not to its own literal text.
shopt -s nullglob
for f in *.sh lib/*.sh mcp/deploy/*.sh omniroute/*.sh host-ops/*.sh; do
  bash -n "$f" || fail "bash parse error: $f"
done
for f in scripts/*.sh; do
  bash -n "$f" || fail "bash parse error: $f"
done
for f in templates/org-github/*.sh templates/org-github/tests/*.sh; do
  bash -n "$f" || fail "bash parse error: $f"
done
bash -n verification/private-mutation-gates.sh \
  || fail "bash parse error: verification/private-mutation-gates.sh"
for f in *.js scripts/*.js; do
  node --check "$f" || fail "node parse error: $f"
done
for f in omniroute/*.py *.py; do
  python3 -m py_compile "$f" || fail "python parse error: $f"
done
for f in ops/productivity/*.sh; do
  bash -n "$f" || fail "bash parse error: $f"
done
python3 -c 'import ast, sys; [ast.parse(open(p, "rb").read(), p) for p in sys.argv[1:]]' \
  ops/productivity/*.py ops/lib/*.py ops/paperclip/*.py || fail "python parse error in ops/productivity, ops/lib or ops/paperclip"
python3 -c 'import ast, sys; [ast.parse(open(path, "rb").read(), path) for path in sys.argv[1:]]' \
  ops/pr-hygiene/sweep.py ops/pr-hygiene/owner_match.py ops/lib/live_run_guard.py \
  ops/case_af881f62/operator_workspace_reconciler.py ops/case_af881f62/test_operator_workspace_reconciler.py \
  ops/case_af881f62/operator_workspace_sweep.py ops/case_af881f62/test_operator_workspace_sweep.py \
  ops/case_0b2823ef/feed_contract.py ops/case_0b2823ef/test_feed_contract.py \
  ops/case_629b4da2/check_operator_copy_risk.py ops/case_629b4da2/test_check_operator_copy_risk.py \
  ops/case_0ce22efa/test_node7b1ff338_pressure_gate.py \
  || fail "python parse error: ops/pr-hygiene, ops/lib, ops/case_af881f62, ops/case_0b2823ef, ops/case_629b4da2 or ops/case_0ce22efa"
rm -rf omniroute/__pycache__ __pycache__
for f in mcp/*.mjs mcp/test/*.mjs; do
  node --check --input-type=module < "$f" || fail "node ESM parse error: $f"
done
# No manifest-vendored file beyond the tracked allowlist may be committed
# (fast hermetic git read; also runs inside sync-toolkit.sh for CI steps).
bash scripts/check-toolkit-vendored-tracked.sh || fail "vendored-tracked gate failed"
if [[ "$rc" -ne 0 ]]; then
  echo "verify-fast: PARSE FAILED"
  exit "$rc"
fi
echo "verify-fast: parse OK"
if [[ "$MODE" == "parse" ]]; then
  exit 0
fi

# Warm checkouts can contain old ignored bytes. Check the complete pinned
# untracked closure, not a filename sentinel. A cold/stale checkout needs the
# archive download as preparation; all subsequent tests stay hermetic.
if ! python3 verification/offline_ci_contract.py --check-restored >/dev/null 2>&1; then
  echo "verify-fast: restoring stale or missing toolkit-vendored suite files..."
  bash scripts/sync-toolkit.sh || fail "toolkit sync failed"
  python3 verification/offline_ci_contract.py --check-restored \
    || fail "restored toolkit content differs from reviewed pin"
fi

echo "=== verify-fast: fast unit/contract gates ==="
node --test test/external-disclosure.test.mjs || fail "external-disclosure suite failed"
node --test hooks/muse-stop-guard/muse-stop-guard.test.mjs || fail "muse-stop-guard suite failed"
node --test hooks/disposition-stop-guard/disposition-stop-guard.test.mjs hooks/disposition-stop-guard/measure/census.test.mjs \
  || fail "disposition-stop-guard suites failed"
./test_deploy_window_open.sh || fail "deploy-window wrapper suite failed"
python3 -m unittest test_deploy_window_manifest || fail "deploy-window manifest gate suite failed"
./test_credential_chain_lockdown.sh || fail "credential-chain lockdown fixture suite failed"
python3 -m unittest test_ci_required_checks_audit || fail "required-checks audit failed"
python3 -m unittest test_private_mutation_gates || fail "private mutation harness failed"
./test_ci_gating.sh || fail "change-gating contract failed"
./test_readme_script_index.sh || fail "readme script-index gate failed"
./test_public_comment_hygiene_gate.sh || fail "public-comment hygiene gate suite failed"
./test_acceptance_transport.sh || fail "acceptance-transport reply suite failed"
./test_upstream_prepush_hook.sh || fail "upstream pre-push hook suite failed"
./test_cliproxy_mgmt_recon_argv.sh || fail "cliproxy recon argv suite failed"
./test_case_b2acfa5c_selfhosted_runner.sh || fail "runner migration contract failed"
./test_dropchannel_scan.sh || fail "dropchannel fixture suite failed"
# case_77f64939/case_8d3070e3. Tool-approval deadline probe: synthetic bundle + fake
# psql, so hermetic. Restored by the toolkit sync above in fresh checkouts.
# BASH_ENV is scrubbed for this one call: an inherited BASH_ENV that resets
# PATH would void the suite's stub-bin PATH prepend and turn its database
# half UNKNOWN (measured 2026-10-09). Absent elsewhere it is a no-op.
env -u BASH_ENV ./test_approval_timeout_probe.sh || fail "approval-timeout probe suite failed"
./test_credential_chain_offline.sh || fail "credential-chain offline fixture suite failed"
./test_gh_access_dispatch.sh || fail "gh-access dispatch suite failed"
./test_org_snapshot.sh || fail "org-snapshot offline suite failed"
# PR #2 landing acceptance: pins the CI wiring-gate/push-key contracts
# (case_f3500d44/case_6d874ecd) red-to-green. Offline, stdlib only.
python3 -m unittest test_pr2_acceptance || fail "pr2 acceptance suite failed"
python3 -m unittest test_alert_queue || fail "alert-queue suite failed"
python3 -m unittest test_watchdog_snapshot_builder || fail "watchdog snapshot-builder suite failed"
python3 -m unittest test_case_da79e241_fleet_run_stall || fail "fleet-run-stall suite failed"
python3 -m unittest test_case_dcf07273_assignment_bundle || fail "assignment host-source suite failed"
(cd ops/productivity && python3 -B -m unittest test_review_error_rerequest test_backlog_promoter test_board_jobs_bounds test_pr_supply test_flow_guard test_host_ok test_orphan_prs test_resolve_outdated_threads test_stuck_run_reaper test_subprocess_bounds test_connection_intent_sweeper test_hide_noop_reconcile test_two_web_promote) \
  || fail "review-error, PR-supply and flow-guard suites failed"
(cd ops/paperclip && python3 -B -m unittest test_focus_gate_bounds) || fail "focus-gate bounds suite failed"
(cd ops/lib && python3 -m unittest test_atomic_state) || fail "atomic state suite failed"
(cd ops/productivity && python3 -m unittest test_stuck_run_state) || fail "stuck-run state suite failed"
(cd ops/productivity && python3 -m unittest test_idle_waiters_state) || fail "idle-waiter state suite failed"
(cd ops/owner-guard && python3 -m unittest test_owner_guard_state) || fail "owner-guard state suite failed"
(cd ops/review_tier1 && python3 -m unittest test_review_tier1) || fail "review-tier1 suite failed"
(cd ops/analysis/review-findings && python3 -B -m unittest test_classify) || fail "review-findings classifier suite failed"
(cd ops/case_1ccef1dc/reviewer-eval/v2 && python3 -m unittest test_survival_all_atomic) || fail "survival atomic-write suite failed"
(cd ops/case_1ccef1dc/reviewer-eval/observational && python3 -m unittest test_collect_atomic) || fail "observational-collect atomic-write suite failed"
(cd ops/pr-hygiene && python3 -m unittest test_owner_match) || fail "pr-hygiene owner-match suite failed"
(cd ops/lib && python3 -m unittest test_live_run_guard) || fail "live-run guard suite failed"
./test_review_route.sh || fail "review-route suite failed"
(cd ops/case_1ccef1dc/reviewer-eval && python3 -m unittest test_arm_import) || fail "arm-import suite failed"
(cd ops/case_1ccef1dc/reviewer-eval && python3 -m unittest test_build_corpus_atomic) || fail "corpus atomic-write suite failed"
(cd ops/case_1ccef1dc/reviewer-eval/v2 && python3 -m unittest test_merge_survival_atomic) || fail "merge-survival atomic-write suite failed"
(cd ops/case_af881f62 && python3 -m unittest test_operator_workspace_reconciler test_operator_workspace_sweep) || fail "operator-workspace reconciler and sweep suite failed"
(cd ops/case_0b2823ef && python3 -B -m unittest test_feed_contract) || fail "cliproxy feed-contract suite failed"
(cd ops/case_629b4da2 && python3 -m unittest test_check_operator_copy_risk) || fail "operator copy-risk gate suite failed"
(cd ops/case_0ce22efa && python3 -B -m unittest test_node7b1ff338_pressure_gate) || fail "sweep-auto node7b1ff338 pressure-gate suite failed"

# Translator fixture needs the pinned TypeScript compiler. CI installs
# typescript@5.9.3 before this step; do the same locally with the shared
# npm cache so scratch worktrees (no node_modules) work identically.
if ! node -e "require.resolve('typescript/bin/tsc')" >/dev/null 2>&1; then
  echo "verify-fast: installing pinned typescript@5.9.3 (shared npm cache)..."
  npm install --no-save --ignore-scripts typescript@5.9.3 || fail "typescript install failed"
fi
node --test test_omniroute_rehearsal_translator.mjs || fail "translator fixture failed"
python3 test_staged_drift.py || fail "staged Tier-0 drift gate failed"

if [[ "$rc" -ne 0 ]]; then
  echo "verify-fast: FAILED"
  exit "$rc"
fi
echo "verify-fast: PASS"
