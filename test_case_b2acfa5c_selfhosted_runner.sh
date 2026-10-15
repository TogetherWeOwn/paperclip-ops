#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
GATE="$ROOT/verification/case_221a4156-selfhosted-runner-gate.sh"
WORKFLOW="$ROOT/.github/workflows/ci.yml"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0
expect_pass() {
  local name=$1 file=$2
  if "$GATE" "$file" >"$TMP/out" 2>&1; then
    printf 'ok - %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'not ok - %s\n' "$name"
    cat "$TMP/out"
    fail=$((fail + 1))
  fi
}
expect_fail() {
  local name=$1 file=$2
  if "$GATE" "$file" >"$TMP/out" 2>&1; then
    printf 'not ok - %s (mutant survived)\n' "$name"
    cat "$TMP/out"
    fail=$((fail + 1))
  else
    printf 'ok - %s\n' "$name"
    pass=$((pass + 1))
  fi
}
mutate() {
  local name=$1 old=$2 new=$3
  MUTANT_FILE="$TMP/$name.yml"
  python3 - "$WORKFLOW" "$MUTANT_FILE" "$old" "$new" <<'PY'
from pathlib import Path
import sys

src, dst, old, new = sys.argv[1:]
text = Path(src).read_text()
if old not in text:
    raise SystemExit(f"mutation anchor absent: {old!r}")
mutant = text.replace(old, new, 1)
if mutant == text:
    raise SystemExit("mutation did not change the workflow")
Path(dst).write_text(mutant)
PY
}

expect_pass baseline "$WORKFLOW"
mutate wrong-label 'runs-on: [self-hosted, two-isolated]' 'runs-on: [self-hosted, other-runner]'
expect_fail wrong-label "$MUTANT_FILE"
mutate paid-fallback 'runs-on: [self-hosted, two-isolated]' 'runs-on: ubuntu-latest'
expect_fail paid-fallback "$MUTANT_FILE"
# case_10099fe0. The label now carries the isolation property: two-isolated runners
# have no DOCKER_HOST in their .env, two-selfhosted ones do. Quietly moving a job
# back onto the Docker pool would hand it the host socket again, which is exactly
# what case_221a4156 forbids, so that must turn this job red.
mutate docker-creep 'runs-on: [self-hosted, two-isolated]' 'runs-on: [self-hosted, two-selfhosted]'
expect_fail docker-creep "$MUTANT_FILE"
# A service container is the quiet way back to needing a daemon: adding one would
# re-create the hole that privilege-suites' ephemeral PostgreSQL cluster closed,
# without any runs-on line changing. That must turn this job red too.
mutate service-container-creep '    timeout-minutes: 20' '    services:
      db:
        image: postgres:16
    timeout-minutes: 20'
expect_fail service-container-creep "$MUTANT_FILE"
# case_bec120d3: the identity proof accepts every fleet prefix (static + GARM). The deletion
# mutant still kills it (red), and a narrowing mutant that drops either
# prefix must turn red too — stranding one fleet reads as success otherwise.
mutate no-positive-control '[[ "${RUNNER_NAME:-}" == coolify-vps-* || "${RUNNER_NAME:-}" == ci-node7b1ff338-iso-* || "${RUNNER_NAME:-}" == garm-* || "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]' 'true # runner identity proof deleted'
expect_fail no-positive-control "$MUTANT_FILE"
mutate narrow-drop-node7b1ff338 '[[ "${RUNNER_NAME:-}" == coolify-vps-* || "${RUNNER_NAME:-}" == ci-node7b1ff338-iso-* || "${RUNNER_NAME:-}" == garm-* || "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]' '[[ "${RUNNER_NAME:-}" == coolify-vps-* || "${RUNNER_NAME:-}" == garm-* || "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] # node7b1ff338 fleet stranded'
expect_fail narrow-drop-node7b1ff338 "$MUTANT_FILE"
mutate narrow-drop-coolify '[[ "${RUNNER_NAME:-}" == coolify-vps-* || "${RUNNER_NAME:-}" == ci-node7b1ff338-iso-* || "${RUNNER_NAME:-}" == garm-* || "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]' '[[ "${RUNNER_NAME:-}" == ci-node7b1ff338-iso-* || "${RUNNER_NAME:-}" == garm-* || "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] # coolify fleet stranded'
expect_fail narrow-drop-coolify "$MUTANT_FILE"
mutate narrow-drop-garm '[[ "${RUNNER_NAME:-}" == coolify-vps-* || "${RUNNER_NAME:-}" == ci-node7b1ff338-iso-* || "${RUNNER_NAME:-}" == garm-* || "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]' '[[ "${RUNNER_NAME:-}" == coolify-vps-* || "${RUNNER_NAME:-}" == ci-node7b1ff338-iso-* || "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] # GARM pools stranded'
expect_fail narrow-drop-garm "$MUTANT_FILE"
mutate dirty-checkout 'clean: true' 'clean: false'
expect_fail dirty-checkout "$MUTANT_FILE"
mutate renamed-check 'name: suites' 'name: suites tests'
expect_fail renamed-check "$MUTANT_FILE"
# case_d0cad227. A run on main must never be cancelled by the next push: every merge
# cancelled the previous main run, so main was verified for only 7 of 29 commits.
mutate cancel-main-runs 'cancel-in-progress: ${{ github.event_name == '"'"'pull_request'"'"' }}' 'cancel-in-progress: true'
expect_fail cancel-main-runs "$MUTANT_FILE"
mutate cancel-everything-again 'cancel-in-progress: ${{ github.event_name == '"'"'pull_request'"'"' }}' 'cancel-in-progress: ${{ github.event_name != '"'"'pull_request'"'"' }}'
expect_fail cancel-everything-again "$MUTANT_FILE"

# Owner 2026-10-04 overflow: the privilege/sudoers suites must never leave the
# isolated pool (GitHub-hosted runners have passwordless sudo), and a bare
# hosted label is still the forbidden paid fallback.
mutate privilege-overflow '    runs-on: [self-hosted, two-isolated]' "    runs-on: \${{ fromJSON((contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), 'privilege-suites') && contains(fromJSON(vars.CI_OVERFLOW_EVENTS || '[]'), github.event_name) && vars.CI_OVERFLOW_RUNNER) || '[\"self-hosted\",\"two-isolated\"]') }}"
expect_fail privilege-overflow "$MUTANT_FILE"
mutate private-mutation-docker-creep '  private-mutation-gates:
    name: Private mutation gates
    needs: [changes]
    if: github.event_name == '\''pull_request'\'' && (needs.changes.outputs.full == '\''true'\'' || needs.changes.outputs.private_mutation == '\''true'\'')
    runs-on: [self-hosted, two-isolated]' '  private-mutation-gates:
    name: Private mutation gates
    needs: [changes]
    if: github.event_name == '\''pull_request'\'' && (needs.changes.outputs.full == '\''true'\'' || needs.changes.outputs.private_mutation == '\''true'\'')
    runs-on: [self-hosted, two-selfhosted]'
expect_fail private-mutation-docker-creep "$MUTANT_FILE"
mutate bare-hosted-overflow "runs-on: \${{ fromJSON((contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), 'suites')" "runs-on: ubuntu-latest #"
expect_fail bare-hosted-overflow "$MUTANT_FILE"
# C6 cutover retired the case_c45ee603 shared routing key with the
# model-selection jobs (no job carries a foreign key anymore; the gate
# rejects any). The remaining foreign-key mutant below keeps that property
# pinned: no job may ride another job's opt-in.
# Widening must fail: no job may borrow another job's routing key and ride an
# operator selection meant for someone else.
mutate drain-shares-other-key "contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), 'failure-log-drain')" "contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), 'offline-suites')"
expect_fail drain-shares-other-key "$MUTANT_FILE"

# case_0bf02bac. The home-PC runners are persistent, not fresh VMs per job, so
# privilege-suites and private-mutation-gates must stay isolated; a bare
# two-node93f4bccf label is the same hole as a bare ubuntu-latest; and no job may opt
# in (the old long mutation job and model-selection suite moved to toolkit CI).
# Each must turn this job red.
mutate privilege-node93f4bccf 'runs-on: [self-hosted, two-isolated]' 'runs-on: ${{ fromJSON(vars.CI_nodecf677d86_RUNNER) }} #'
expect_fail privilege-node93f4bccf "$MUTANT_FILE"
mutate third-job-node93f4bccf "contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), 'suites')" "contains(fromJSON(vars.CI_nodecf677d86_JOBS || '[]'), 'suites')"
expect_fail third-job-node93f4bccf "$MUTANT_FILE"

# case_d5bc7950 + case_5236db70: the baseline positively proves all three lightweight
# jobs are hard-pinned to the ephemeral pool (not isolated, not a vars-driven
# expression the operator routing can defeat). Pin the literal so a docs-only
# PR cannot quietly go back to waiting on the saturated isolated pool.
for job in changes secret-scan ci-ok; do
  route='    runs-on: [self-hosted, two-ephemeral]'
  mutate "$job-back-to-isolated" "$route" '    runs-on: [self-hosted, two-isolated]'
  expect_fail "$job-back-to-isolated" "$MUTANT_FILE"
  # case_5236db70 regression: a vars-driven overflow expression reintroduced
  # (CI_OVERFLOW_JOBS currently lists these jobs with a two-isolated runner,
  # which would keep routing them to the saturated pool and defeat the fix).
  mutate "$job-overflow-reintroduced" "$route" "    runs-on: \${{ fromJSON((contains(fromJSON(vars.CI_OVERFLOW_JOBS || '[]'), '$job') && contains(fromJSON(vars.CI_OVERFLOW_EVENTS || '[]'), github.event_name) && vars.CI_OVERFLOW_RUNNER) || '[\"self-hosted\",\"two-ephemeral\"]') }}"
  expect_fail "$job-overflow-reintroduced" "$MUTANT_FILE"
  mutate "$job-fallback-changed" "$route" '    runs-on: [self-hosted, two-selfhosted]'
  expect_fail "$job-fallback-changed" "$MUTANT_FILE"
  mutate "$job-node93f4bccf" "$route" '    runs-on: [self-hosted, two-node93f4bccf]'
  expect_fail "$job-node93f4bccf" "$MUTANT_FILE"
done

printf '%s passed, %s failed\n' "$pass" "$fail"
(( fail == 0 ))
