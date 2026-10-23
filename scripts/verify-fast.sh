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
#   scripts/verify-fast.sh [--parse-only] [--plan|--dry-run [--json]] [--list [--json]] [--no-prepare] [--help]
#   make verify-fast | make verify-fast-parse | make verify-fast-plan
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
PREPARE=1
WANT_PARSE=0
WANT_PLAN=0
WANT_LIST=0
WANT_JSON=0
WANT_NO_PREPARE=0
WANT_HELP=0

usage() {
  echo "usage: $0 [--parse-only] [--plan|--dry-run [--json]] [--list [--json]] [--no-prepare] [--help]" >&2
}

print_help() {
  cat <<'HELP'
usage: verify-fast.sh [--parse-only] [--plan|--dry-run [--json]] [--list [--json]] [--no-prepare] [--help]

Fast verification tier. The default run executes the parse gate plus the fast
suites; --parse-only executes the parse gate only.

Modes (plan and list modes are read-only; execution modes are not):
  (default)            run parse + fast suites, preparing the checkout as needed
  --parse-only         run the parse gate only
  --plan, --dry-run    print the suite plan without running anything (read-only)
  --list               print one stable suite id per line (read-only)
  --no-prepare         run without preparing; refuse (exit 3) if prerequisites are missing

Options:
  --json               machine-readable output for --plan, --dry-run or --list
  -h, --help           print this help

Examples:
  ./scripts/verify-fast.sh --plan --json
  ./scripts/verify-fast.sh --dry-run
  ./scripts/verify-fast.sh --list
  ./scripts/verify-fast.sh --no-prepare
  ./scripts/verify-fast.sh --parse-only
  make verify-fast-plan

Normal execution is not read-only: it may restore toolkit files, install the
pinned TypeScript compiler, and write bytecode caches. Only --plan, --dry-run
and --list make no changes.
HELP
}

while (($# > 0)); do
  arg="$1"; shift
  case "$arg" in
    --parse-only) WANT_PARSE=1 ;;
    --plan|--dry-run) WANT_PLAN=1 ;;
    --list) WANT_LIST=1 ;;
    --json) WANT_JSON=1 ;;
    --no-prepare) WANT_NO_PREPARE=1 ;;
    --help|-h) WANT_HELP=1 ;;
    *)
      echo "verify-fast: unknown flag: $arg" >&2
      usage
      exit 2
      ;;
  esac
done

if (( WANT_JSON )) && (( ! WANT_PLAN )) && (( ! WANT_LIST )); then
  echo "verify-fast: --json requires --plan, --dry-run or --list" >&2
  usage
  exit 2
fi
if (( WANT_PLAN )) && (( WANT_LIST )); then
  echo "verify-fast: cannot combine --plan/--dry-run with --list (pick one output)" >&2
  usage
  exit 2
fi
if (( WANT_NO_PREPARE )) && (( WANT_PLAN || WANT_LIST )); then
  echo "verify-fast: cannot combine --no-prepare with --plan/--dry-run/--list (plan and list modes never prepare)" >&2
  usage
  exit 2
fi

if (( WANT_HELP )); then
  print_help
  exit 0
fi
if (( WANT_PARSE )); then
  MODE="parse"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

INVENTORY_FILE="$REPO_ROOT/scripts/verify-fast-inventory.json"
PLAN_HELPER="$REPO_ROOT/scripts/verify-fast-plan.py"
TIER_ARGS=()
if (( WANT_PARSE )); then
  TIER_ARGS=(--tier parse)
fi

# "--list" prints one stable suite id per line from the shared inventory.
if (( WANT_LIST )); then
  if (( WANT_JSON )); then
    python3 "$PLAN_HELPER" --inventory "$INVENTORY_FILE" --emit-list-json "${TIER_ARGS[@]}"
  else
    python3 "$PLAN_HELPER" --inventory "$INVENTORY_FILE" --emit-list "${TIER_ARGS[@]}"
  fi
  exit 0
fi

if (( WANT_PLAN )); then
  if (( WANT_JSON )); then
    python3 "$PLAN_HELPER" --inventory "$INVENTORY_FILE" --emit-json "${TIER_ARGS[@]}"
  else
    python3 "$PLAN_HELPER" --inventory "$INVENTORY_FILE" --emit-text "${TIER_ARGS[@]}"
  fi
  exit 0
fi

if (( WANT_NO_PREPARE )); then
  PREPARE=0
  missing="$(python3 "$PLAN_HELPER" --inventory "$INVENTORY_FILE" --missing-prereqs "${TIER_ARGS[@]}")"
  if [[ -n "$missing" ]]; then
    count="$(printf '%s\n' "$missing" | wc -l | tr -d ' ')"
    {
      echo "verify-fast: cannot run with --no-prepare: ${count} prerequisite(s) missing"
      echo "missing: $(printf '%s' "$missing" | tr '\n' ' ' | sed 's/ $//')"
      echo "next step: run the same command without --no-prepare to restore and install, or provision the missing prerequisites and retry"
    } >&2
    exit 3
  fi
fi

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
  ops/productivity/*.py ops/lib/*.py ops/paperclip/*.py ops/agent-tooling/serena-pilot-b/*.py || fail "python parse error in ops/productivity, ops/lib, ops/paperclip or ops/agent-tooling/serena-pilot-b"
sh -n ops/agent-tooling/serena-pilot-b/code-nav || fail "sh parse error: ops/agent-tooling/serena-pilot-b/code-nav"
python3 -c 'import ast, sys; [ast.parse(open(path, "rb").read(), path) for path in sys.argv[1:]]' \
  ops/pr-hygiene/sweep.py ops/pr-hygiene/owner_match.py ops/lib/live_run_guard.py \
  ops/case_af881f62/operator_workspace_reconciler.py ops/case_af881f62/test_operator_workspace_reconciler.py \
  ops/case_af881f62/operator_workspace_sweep.py ops/case_af881f62/test_operator_workspace_sweep.py \
  ops/case_0b2823ef/feed_contract.py ops/case_0b2823ef/test_feed_contract.py \
  ops/case_629b4da2/check_operator_copy_risk.py ops/case_629b4da2/test_check_operator_copy_risk.py \
  ops/case_0ce22efa/test_node7b1ff338_pressure_gate.py \
  ops/case_f2010cb7/beszel_adapter.py ops/case_f2010cb7/forward_beszel_discord.py \
  || fail "python parse error: ops/pr-hygiene, ops/lib, ops/case_af881f62, ops/case_0b2823ef, ops/case_629b4da2, ops/case_0ce22efa or ops/case_f2010cb7"
rm -rf omniroute/__pycache__ __pycache__
for f in mcp/*.mjs mcp/test/*.mjs; do
  node --check --input-type=module < "$f" || fail "node ESM parse error: $f"
done
# No manifest-vendored file beyond the tracked allowlist may be committed
# (fast hermetic git read; also runs inside sync-toolkit.sh for CI steps).
bash scripts/check-toolkit-vendored-tracked.sh || fail "vendored-tracked gate failed"
# Every tracked test suite must be wired into CI or exempted in
# verification/orphan_suites_allowlist.json. Hermetic (git ls-files plus
# workflow/script text reads), so it runs in the parse tier.
python3 verification/orphan_suites.py || fail "orphan-suite ratchet failed"
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
if (( PREPARE )) && ! python3 verification/offline_ci_contract.py --check-restored >/dev/null 2>&1; then
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
# Activation contract: exclusive lock, verify-only hardening, pinned
# bindings. Hermetic: stub docker and stub drain.sh, temp fixtures only.
./paperclip-upgrade/test-activation-offline.sh || fail "activation contract suite failed"
python3 -m unittest test_reharden_relative_targets || fail "reharden relative-target suite failed"
python3 -m unittest test_deploy_window_manifest || fail "deploy-window manifest gate suite failed"
./test_credential_chain_lockdown.sh || fail "credential-chain lockdown fixture suite failed"
python3 -m unittest test_ci_required_checks_audit || fail "required-checks audit failed"
# case_6f2e8a41. Deterministic test-missing gate: unit contract plus
# staged-copy/CLI regression. Hermetic stdlib unittest, no network, no
# clock, temp fixtures only.
python3 -m unittest test_blocker_test_checks test_test_missing_org_template \
  || fail "test-missing gate suites failed"
python3 -B test_beszel_adapter.py || fail "beszel adapter suite failed"
python3 -B test_beszel_discord_forwarder.py || fail "beszel discord forwarder suite failed"
bash test_fetch_beszel_normalized.sh || fail "beszel fetch wrapper suite failed"
python3 -m unittest test_private_mutation_gates || fail "private mutation harness failed"
python3 -m unittest test_quality_gates test_floor_guard test_no_pull_request_target \
  || fail "quality-gate helper suites failed"
./test_mutmut_changed.sh || fail "mutmut budget-kill fixture suite failed"
./test_ci_gating.sh || fail "change-gating contract failed"
./test_readme_script_index.sh || fail "readme script-index gate failed"
./test_public_comment_hygiene_gate.sh || fail "public-comment hygiene gate suite failed"
./test_acceptance_transport.sh || fail "acceptance-transport reply suite failed"
./test_upstream_prepush_hook.sh || fail "upstream pre-push hook suite failed"
./test_cliproxy_mgmt_recon_argv.sh || fail "cliproxy recon argv suite failed"
./test_case_b2acfa5c_selfhosted_runner.sh || fail "runner migration contract failed"
python3 -m unittest test_pr6_acceptance || fail "pr6 acceptance failed"
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
./test_op_queue_reject.sh || fail "queue-reject verb suite failed"
# PR #2 landing acceptance: pins the CI wiring-gate/push-key contracts
# (case_f3500d44/case_6d874ecd) red-to-green. Offline, stdlib only.
python3 -m unittest test_pr2_acceptance || fail "pr2 acceptance suite failed"
python3 -m unittest test_alert_queue || fail "alert-queue suite failed"
python3 -m unittest test_watchdog_snapshot_builder || fail "watchdog snapshot-builder suite failed"
python3 -m unittest test_case_da79e241_fleet_run_stall || fail "fleet-run-stall suite failed"
python3 -m unittest test_case_dcf07273_assignment_bundle || fail "assignment host-source suite failed"
python3 -m unittest test_orphan_suites || fail "orphan-suite ratchet fixtures failed"
python3 -m unittest test_pr3_stop_guard_landing || fail "stop-guard landing suite failed"
python3 -m unittest test_warn_first_inventory || fail "warn-first inventory suite failed"
./ops/case_34c2c99f/test_regen_dry_run_receipt.sh || fail "regen dry-run receipt suite failed"
node --test plugins/pr-tracker/test/collect-rest.test.mjs plugins/pr-tracker/test/poll-prs.test.mjs plugins/pr-tracker/test/setup.test.mjs plugins/pr-tracker/test/upstream-watcher.test.mjs plugins/pr-tracker/test/view-model.test.mjs \
  || fail "pr-tracker suite failed"
node --test paperclip-upgrade/settings-bind-rehearsal.test.mjs || fail "settings-bind rehearsal suite failed"
(cd ops/productivity && python3 -B -m unittest test_review_error_rerequest test_review_dedup test_backlog_promoter test_board_jobs_bounds test_pr_supply test_flow_guard test_confirmed_mutation_outcomes test_host_ok test_orphan_prs test_resolve_outdated_threads test_stuck_run_reaper test_subprocess_bounds test_connection_intent_sweeper test_hide_noop_reconcile test_two_web_promote test_flake_ledger test_pr_controller_backstop test_pr_controller_wakes) \
  || fail "review-error, review-dedup, PR-supply, flow-guard, flake-ledger and pr-controller suites failed"
(cd ops/paperclip && python3 -B -m unittest test_focus_gate_bounds) || fail "focus-gate bounds suite failed"
(cd ops/lib && python3 -m unittest test_atomic_state) || fail "atomic state suite failed"
(cd ops/agent-tooling/serena-pilot-b && python3 -B -m unittest test_code_nav) || fail "code-nav suite failed"
(cd ops/productivity && python3 -m unittest test_stuck_run_state) || fail "stuck-run state suite failed"
(cd ops/productivity && python3 -B -m unittest test_idle_waiters_state test_idle_waiter_live_path test_idle_waiters_cli) || fail "idle-waiter state/live-path/cli suites failed"
(cd ops/owner-guard && python3 -m unittest test_owner_guard_state) || fail "owner-guard state suite failed"
(cd ops/review_tier1 && python3 -m unittest test_review_tier1) || fail "review-tier1 suite failed"
(cd ops/analysis/review-findings && python3 -B -m unittest test_classify) || fail "review-findings classifier suite failed"
(cd ops/case_1ccef1dc/reviewer-eval/v2 && python3 -m unittest test_survival_all_atomic) || fail "survival atomic-write suite failed"
(cd ops/case_1ccef1dc/reviewer-eval/observational && python3 -m unittest test_collect_atomic) || fail "observational-collect atomic-write suite failed"
(cd ops/pr-hygiene && python3 -m unittest test_owner_match) || fail "pr-hygiene owner-match suite failed"
(cd ops/lib && python3 -m unittest test_live_run_guard) || fail "live-run guard suite failed"
(cd ops/productivity && python3 -m unittest test_nongit_quarantine_custody) || fail "nongit quarantine custody suite failed"
./test_review_route.sh || fail "review-route suite failed"
./test_shell_subst_guard.sh || fail "shell-subst guard suite failed"
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
if (( PREPARE )) && ! node -e "require.resolve('typescript/bin/tsc')" >/dev/null 2>&1; then
  echo "verify-fast: installing pinned typescript@5.9.3 (shared npm cache)..."
  npm install --no-save --ignore-scripts typescript@5.9.3 || fail "typescript install failed"
fi
node --test test_omniroute_rehearsal_translator.mjs || fail "translator fixture failed"
python3 test_staged_drift.py || fail "staged Tier-0 drift gate failed"
# Shared migration safety gates (docs/migration-standard.md): timeout
# preamble, squawk + file standard, prod-clone rehearsal, ledger drift
# gate. Fully offline, same suites the CI `migration safety gates suites`
# step runs.
./test_migration_timeout_preamble.sh || fail "migration timeout-preamble suite failed"
./test_migration_squawk.sh || fail "migration squawk suite failed"
./test_migration_rehearsal.sh || fail "migration rehearsal suite failed"
python3 -m unittest test_migration_drift_gate || fail "migration drift-gate suite failed"
python3 -m unittest test_migration_evidence_ax || fail "migration evidence-AX suite failed"
# Plan/list/no-prepare contract plus the strict inventory-to-execution drift
# check and the PREPARE-guard check. Offline: stdlib unittest, synthetic stubs.
python3 -B -m unittest test_verify_fast_plan test_verify_fast_plan_gates || fail "verify-fast plan suites failed"

if [[ "$rc" -ne 0 ]]; then
  echo "verify-fast: FAILED"
  exit "$rc"
fi
echo "verify-fast: PASS"
