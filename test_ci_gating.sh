#!/usr/bin/env bash
# test_ci_gating.sh -- CI entry point for the case_d5bc7950 gating contract.
# 1. The change-area classifier unit tests.
# 2. The structural gate over the real .github/workflows/ci.yml.
# 3. Positive controls: mutated workflow copies that MUST turn the gate red
#    (a gate that has only ever seen the healthy file has never shown it can
#    fail). Exit 0 all green, 1 anything red.
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

printf '# explicit workflow job permissions and acceptance fixtures\n'
python3 "$ROOT/check_workflow_job_permissions.py"
python3 -m unittest -v test_check_workflow_job_permissions

GATE="$ROOT/verification/case_d5bc7950-ci-gating.sh"
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

printf '# retained/removed offline producers and negative controls\n'
python3 -m unittest -v test_offline_ci_contract test_toolkit_sync

printf '# classifier unit tests\n'
python3 "$ROOT/.github/scripts/test_ci_changes.py" 2>&1 | tail -3

printf '# structural gate over the real workflow\n'
expect_pass baseline "$WORKFLOW"

printf '# positive controls: each mutant must turn the gate red\n'
# The fail-dangerous direction: needs deleted, area if stays -> the job would
# skip on every PR while its required check reports success.
mutate needs-deleted '  suites:
    needs: [changes]' '  suites:
    # needs dropped'
expect_fail needs-deleted "$MUTANT_FILE"
# The area test deleted: the job would run on every PR (wasteful, and the
# gate must still catch the drift).
mutate area-if-deleted "needs.changes.outputs.dispatch" "needs.changes.outputs.full"
expect_fail area-if-deleted "$MUTANT_FILE"
# The aggregator conditional: without a status function, ci-ok implies
# success() and skips whenever a needed job failed, so the merge gate hangs
# instead of going red. The anchor carries the ci-ok needs tail so the mutant
# lands on the aggregator, not on an older step-level `if: always()` line.
mutate ci-ok-not-status-fn '    if: ${{ !cancelled() }}
    steps:
      - name: Aggregate suite verdicts' '    if: success()
    steps:
      - name: Aggregate suite verdicts'
expect_fail ci-ok-not-status-fn "$MUTANT_FILE"
# Review case_13d22ce3: always() on the aggregator ignores cancellation, so a
# superseded run's ci-ok can outlive the cancellation and hold the PR
# concurrency group against the new head.
mutate ci-ok-always '    if: ${{ !cancelled() }}
    steps:
      - name: Aggregate suite verdicts' '    if: always()
    steps:
      - name: Aggregate suite verdicts'
expect_fail ci-ok-always "$MUTANT_FILE"
# Even the lightweight aggregator needs a bounded runtime.
mutate ci-ok-timeout-dropped '  ci-ok:
    name: ci-ok
    timeout-minutes: 5' '  ci-ok:
    name: ci-ok'
expect_fail ci-ok-timeout-dropped "$MUTANT_FILE"
# Standard rule 2: a workflow-level paths filter on pull_request silences
# required checks on PRs outside it.
mutate workflow-paths '    types: [opened, synchronize, reopened, ready_for_review]
  merge_group:' '    types: [opened, synchronize, reopened, ready_for_review]
    paths:
      - plugins/**
  merge_group:'
expect_fail workflow-paths "$MUTANT_FILE"
# CISO case_f11bc226: dropping ready_for_review strands a draft that flips to
# ready on its all-skipped draft verdict with ci-ok green.
mutate ready-for-review-dropped 'types: [opened, synchronize, reopened, ready_for_review]' 'types: [opened, synchronize, reopened]'
expect_fail ready-for-review-dropped "$MUTANT_FILE"
# Review case_13d22ce3 P1: reintroducing git merge-base kills the changes job
# on shallow PR merge refs (exit 1 under pipefail) -- parking every suite.
mutate merge-base-reintroduced 'base="${{ github.event.pull_request.base.sha }}"' 'base="$(git merge-base HEAD "${{ github.event.pull_request.base.sha }}")"'
expect_fail merge-base-reintroduced "$MUTANT_FILE"
# Rename detection reports only the destination: moving code into docs/
# would hide the source path and skip the affected suite.
mutate no-renames-dropped 'git diff --name-only --no-renames "$base" HEAD | python3 .github/scripts/ci_changes.py' 'git diff --name-only "$base" HEAD | python3 .github/scripts/ci_changes.py'
expect_fail no-renames-dropped "$MUTANT_FILE"
# The fail-dangerous direction on the privilege job itself (CISO F1
# context): needs deleted, area if stays -> the enforcement suite skips on
# every PR while its required check reports success.
mutate privilege-needs-deleted '  privilege-suites:
    needs: [changes]' '  privilege-suites:
    # needs dropped'
expect_fail privilege-needs-deleted "$MUTANT_FILE"
# case_572c6391: the same fail-dangerous direction on the new scan job.
mutate trivy-needs-deleted '  trivy-scan:
    name: Trivy filesystem scan
    needs: [changes]' '  trivy-scan:
    name: Trivy filesystem scan
    # needs dropped'
expect_fail trivy-needs-deleted "$MUTANT_FILE"
# Private mutants are intentionally PR-only and must gate the required aggregate.
mutate private-mutation-pr-only-dropped "if: github.event_name == 'pull_request' && (needs.changes.outputs.full == 'true' || needs.changes.outputs.private_mutation == 'true')" "if: needs.changes.outputs.full == 'true' || needs.changes.outputs.private_mutation == 'true'"
expect_fail private-mutation-pr-only-dropped "$MUTANT_FILE"
mutate private-mutation-restore-dropped '  private-mutation-gates:
    name: Private mutation gates
    needs: [changes]
    if: github.event_name == '\''pull_request'\'' && (needs.changes.outputs.full == '\''true'\'' || needs.changes.outputs.private_mutation == '\''true'\'')
    runs-on: ubuntu-latest
    timeout-minutes: 50
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with:
          fetch-depth: 1
          clean: true
      - name: Restore toolkit-vendored files
        run: bash scripts/sync-toolkit.sh' '  private-mutation-gates:
    name: Private mutation gates
    needs: [changes]
    if: github.event_name == '\''pull_request'\'' && (needs.changes.outputs.full == '\''true'\'' || needs.changes.outputs.private_mutation == '\''true'\'')
    runs-on: ubuntu-latest
    timeout-minutes: 50
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with:
          fetch-depth: 1
          clean: true
      - name: Restore toolkit-vendored files
        run: true'
expect_fail private-mutation-restore-dropped "$MUTANT_FILE"
# Standard rule 5: without the nightly full run, a slipped regression waits
# for the next main push to be caught.
mutate schedule-deleted '  schedule:' '  # schedule deleted:'
expect_fail schedule-deleted "$MUTANT_FILE"
# case_ec2539bf: the nightly back on the shared ref-keyed group. A push to main
# then replaces the pending nightly (2026-10-06: cancelled with zero jobs five
# minutes after it queued) and the change-gated push path loses its backstop.
mutate nightly-shares-push-group "group: ci-\${{ github.workflow }}-\${{ github.event.pull_request.number || ((github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && format('{0}-{1}', github.event_name, github.run_id)) || github.ref }}" 'group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}'
expect_fail nightly-shares-push-group "$MUTANT_FILE"
# Only the nightly isolated, a dispatched pre-release run still collides.
mutate dispatch-shares-push-group "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'" "github.event_name == 'schedule'"
expect_fail dispatch-shares-push-group "$MUTANT_FILE"
# The push group widened to a unique key: main serialisation (case_d0cad227) gone.
mutate push-group-widened "|| github.ref }}
  cancel-in-progress" "|| github.run_id }}
  cancel-in-progress"
expect_fail push-group-widened "$MUTANT_FILE"
# No manual pre-release full run.
mutate dispatch-trigger-deleted '  workflow_dispatch:
' '  # workflow_dispatch deleted
'
expect_fail dispatch-trigger-deleted "$MUTANT_FILE"
# case_e41d7023: the push branch restored to the old full-run latch -- every main
# push would run the full suite again and starve exact-main green.
mutate push-latch-restored 'base="${{ github.event.before }}"' 'for a in full privilege core private_mutation runbook omniroute dispatch cliproxy capture mcp hostops upstream trivy; do echo "$a=true" >> "$GITHUB_OUTPUT"; done # push latch restored'
expect_fail push-latch-restored "$MUTANT_FILE"
# case_e41d7023: the null-SHA guard dropped -- a (re)created main would diff the
# null SHA and fail the changes job instead of failing closed to full.
mutate push-null-guard-dropped 'if [[ "$base" == "0000000000000000000000000000000000000000" ]]; then' 'if [[ "$base" == "never-matches-anything" ]]; then'
expect_fail push-null-guard-dropped "$MUTANT_FILE"
# case_e41d7023: schedule/merge_group folded back into the push-gated path -- the
# nightly would run only affected suites and stop being the full backstop.
mutate schedule-latch-dropped '          else
            # schedule, merge_group: full suite (standard rule 5: the nightly' '          else
            # schedule latch dropped (standard rule 5: the nightly'
expect_fail schedule-latch-dropped "$MUTANT_FILE"
# The detector gated: a changes job with its own if can skip, and every
# gated job then evaluates against undefined outputs (silent skip).
mutate changes-gated '  changes:
    name: Change detection' '  changes:
    if: github.event_name == '"'"'pull_request'"'"'
    name: Change detection'
expect_fail changes-gated "$MUTANT_FILE"
mutate private-mutation-output-dropped '      private_mutation: ${{ steps.detect.outputs.private_mutation }}' '      # private mutation output dropped'
expect_fail private-mutation-output-dropped "$MUTANT_FILE"
# Secrets gated: a secret-scan job with needs/if can skip on exactly the
# docs-only PRs where every heavy suite also skips -- the committed secret
# would then merge with ci-ok green.
mutate scan-gated '  secret-scan:
    name: Secret scan' '  secret-scan:
    needs: [changes]
    if: needs.changes.outputs.full == '"'"'true'"'"'
    name: Secret scan'
expect_fail scan-gated "$MUTANT_FILE"
# Pipeline review 2026-10-10 (#7): the folded repo-split guards dropped from
# the always-run secret-scan job -- a moved path could silently re-land.
mutate moved-guard-dropped '      - name: Fail if the moved plugin path is touched' '      - name: moved guard renamed away'
expect_fail moved-guard-dropped "$MUTANT_FILE"
mutate freeze-guard-dropped '      - name: Fail if a toolkit-owned path is touched' '      - name: freeze guard renamed away'
expect_fail freeze-guard-dropped "$MUTANT_FILE"
# ci-ok blind to secrets: secret-scan dropped from needs, so a red scan no
# longer fails the aggregator on a docs-only PR.
mutate ciok-blind-to-scan '      - secret-scan
      - trivy-scan
      - privilege-suites' '      - trivy-scan
      - privilege-suites'
expect_fail ciok-blind-to-scan "$MUTANT_FILE"
mutate ciok-blind-to-private-mutations '      - suites
      - private-mutation-gates
    # `!cancelled()`' '      - suites
    # `!cancelled()`'
expect_fail ciok-blind-to-private-mutations "$MUTANT_FILE"
# case_8089241f: the drain blind to one suite. suites dropped from the drain's
# needs, so a run where ONLY suites fails never triggers the drain (the
# `if: failure()` only sees needed jobs). ci-ok and the drain carry the
# identical needs list, so the anchor carries the drain-only `if: failure()`
# tail; without it the mutant lands on ci-ok and proves nothing.
mutate drain-blind-to-suites '      - suites
      - private-mutation-gates
      - secret-scan
    if: failure()' '      - private-mutation-gates
      - secret-scan
    if: failure()'
expect_fail drain-blind-to-suites "$MUTANT_FILE"
mutate drain-blind-to-private-mutations '      - suites
      - private-mutation-gates
      - secret-scan
    if: failure()' '      - suites
      - secret-scan
    if: failure()'
expect_fail drain-blind-to-private-mutations "$MUTANT_FILE"
# PR #857 review: secret-scan carries the folded repo-split guards and no
# other drain-needed job needs it, so dropping it from the drain's needs
# silences the failure bundle on exactly the runs where only a guard fails.
mutate drain-blind-to-secret-scan '      - private-mutation-gates
      - secret-scan
    if: failure()' '      - private-mutation-gates
    if: failure()'
expect_fail drain-blind-to-secret-scan "$MUTANT_FILE"
# The aggregator blind to the consolidated job: suites dropped from ci-ok's
# needs, so a red suites run no longer fails the required check. ci-ok and the
# drain share most of the list; the anchor carries the ci-ok-only comment tail
# (the drain's list ends in `if: failure()`), so the mutant lands on ci-ok.
mutate ciok-blind-to-suites '      - suites
      - private-mutation-gates
    # `!cancelled()`' '      - private-mutation-gates
    # `!cancelled()`'
expect_fail ciok-blind-to-suites "$MUTANT_FILE"
# The suites job runs ten areas behind one job-level `if:`. Dropping the LAST
# folded area from it skips that area's steps on exactly the PRs that touch it
# while the required check reports success; the gate must test every area, not
# only the first.
mutate suites-fold-area-dropped " || needs.changes.outputs.upstream == 'true'
    name: suites" "
    name: suites"
expect_fail suites-fold-area-dropped "$MUTANT_FILE"
# Dropping a middle area is the same failure in another position: that
# area's steps would skip on exactly the PRs that touch it while the
# required check reports success.
mutate suites-fold-middle-area-dropped " || needs.changes.outputs.hostops == 'true' || needs.changes.outputs.upstream" " || needs.changes.outputs.upstream"
expect_fail suites-fold-middle-area-dropped "$MUTANT_FILE"
# The whole job-level `if:` gone: suites would run on every PR, including the
# docs-only ones the gating exists to keep cheap.
mutate suites-if-deleted '  suites:
    needs: [changes]
    if: ' '  suites:
    needs: [changes]
    # if: '
expect_fail suites-if-deleted "$MUTANT_FILE"

printf '%s passed, %s failed\n' "$pass" "$fail"
(( fail == 0 ))
