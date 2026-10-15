#!/usr/bin/env bash
# ===========================================================================
# case_d5bc7950-ci-gating.sh -- structural gate for the CI standard (case_6b39c80f /
# case_d5bc7950) in .github/workflows/ci.yml.
#
# THE BUG CLASS THIS EXISTS FOR. Job-level gating has one fail-dangerous
# direction: a job whose `needs: changes` is deleted while its
# `if: needs.changes.outputs.*` stays keeps evaluating against an UNDEFINED
# output -- '' is never 'true', so the job skips on EVERY pr and its required
# check reports success without running. The reverse (deleted `if:`) merely
# wastes runners. This gate pins the load-bearing half: every heavy job names
# `changes` in `needs:` AND tests its area output in `if:`; ci-ok aggregates
# with `if: ${{ !cancelled() }}`; the triggers stay full-run capable; and no
# workflow-level `paths:` can silence a required check (standard rule 2).
#
# Usage: verification/case_d5bc7950-ci-gating.sh [workflow-file]
# Exit 0 gate holds, 1 gate broken. Offline, stdlib python3 only.
# ===========================================================================
set -Eeuo pipefail

WORKFLOW="${1:-.github/workflows/ci.yml}"
[[ -f "$WORKFLOW" ]] || { printf 'FAIL: missing %s\n' "$WORKFLOW" >&2; exit 1; }

# The job-level gate cannot detect a deleted individual private producer.
python3 "$(dirname "${BASH_SOURCE[0]}")/offline_ci_contract.py" "$WORKFLOW"

python3 - "$WORKFLOW" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text()

# Job name -> area outputs the job must test (standard rule 2: job-level
# gating, never workflow-level paths on a required-check producer).
# case_a2b959d4: the nine small change-gated areas run as step-gated blocks of
# the single `suites` job (one VM boot per run), so one job tests nine areas.
# case_1d1195e1: `upstream` is the tenth area (upstream-pipeline family gated off
# the per-PR core path but still running on main/nightly via the full latch).
HEAVY = {
    "privilege-suites": ["privilege"],
    "suites": ["core", "runbook", "omniroute", "dispatch",
               "cliproxy", "capture", "mcp", "hostops", "upstream"],
    "private-mutation-gates": ["private_mutation"],
    "trivy-scan": ["trivy"],
}

jobs_match = re.search(r"(?ms)^jobs:\n(?P<jobs>.*)\Z", text)
if not jobs_match:
    raise SystemExit("FAIL: jobs block is missing")
jobs_text = jobs_match.group("jobs")
bodies = {}
for name in re.findall(r"(?m)^  ([a-zA-Z0-9_-]+):\n", jobs_text):
    m = re.search(r"(?ms)^  " + re.escape(name) + r":\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", jobs_text)
    bodies[name] = m.group("body") if m else ""

checks = {}


def check(name, ok):
    checks[name] = bool(ok)
    print(("PASS" if ok else "FAIL") + ": " + name)


for job, body in bodies.items():
    check(f"{job} has a positive timeout",
          re.search(r"(?m)^    timeout-minutes:\s*[1-9][0-9]*\s*$", body) is not None)

for job, areas in HEAVY.items():
    body = bodies.get(job)
    check(f"{job} exists", body is not None)
    if body is None:
        continue
    m = re.search(r"(?m)^    needs:\s*(?P<val>\[.*?\]|\S.*?)\s*$", body)
    needs_val = m.group("val") if m else ""
    # Flow (`needs: [changes]`) or block (subsequent `      - changes`).
    block = "\n".join(
        line for line in body.splitlines()
        if re.match(r"^      - ", line))
    check(f"{job} needs changes",
          "changes" in needs_val or "changes" in block)
    im = re.search(r"(?m)^    if:\s*(?P<val>.+?)\s*$", body)
    if_val = im.group("val") if im else ""
    for area in areas:
        check(f"{job} tests its area output {area}",
              f"needs.changes.outputs.{area}" in if_val
              and "needs.changes.outputs.full" in if_val)
    if job == "private-mutation-gates":
        check("private-mutation-gates is PR-only",
              "github.event_name == 'pull_request'" in if_val)
        check("private-mutation-gates restores toolkit sources",
              "scripts/sync-toolkit.sh" in body)
        check("private-mutation-gates runs its targeted script",
              "verification/private-mutation-gates.sh" in body)

# The detector itself: always runs (no job-level if), publishes outputs,
# and diffs with a two-dot diff against the PR base SHA. merge-base is
# banned here: on this shallow checkout it exits 1 on PR merge refs (no
# common ancestor in the fetched history) and, under `set -euo pipefail`,
# kills the step before any output is written -- parking every heavy suite
# and ci-ok behind a red `changes` job on every PR (review case_13d22ce3 P1).
# The word "merge-base" in prose is fine; the `git merge-base` COMMAND is
# what must never come back.
changes = bodies.get("changes")
check("changes job exists", changes is not None)
if changes is not None:
    check("changes has no job-level if",
          re.search(r"(?m)^    if:\s*", changes) is None)
    check("changes publishes outputs",
          re.search(r"(?m)^    outputs:\s*$", changes) is not None)
    check("changes publishes private_mutation",
          re.search(r"(?m)^      private_mutation: \$\{\{ steps\.detect\.outputs\.private_mutation \}\}$",
                    changes) is not None)
    check("changes runs the detector",
          "ci_changes.py" in changes)
    check("changes uses two-dot diff, never git merge-base",
          "git merge-base" not in changes)
    # Pin the executable producer, not a comment mentioning the flag:
    # rename detection hides the deleted source when code moves into docs/.
    check("changes disables rename detection",
          re.search(r'(?m)^\s+git diff --name-only --no-renames "\$base" HEAD'
                    r'\s*\|\s*python3 \.github/scripts/ci_changes\.py\b',
                    changes) is not None)
    # case_e41d7023: push to main diffs against the previous main SHA instead of
    # latching the full run, so each merge runs only its affected suites and
    # finishes. The null SHA (main (re)created) fails closed to full. Pin the
    # executable assignment, not a comment mentioning the expression.
    check("push events diff against before",
          re.search(r'(?m)^\s+base="\$\{\{\s*github\.event\.before\s*\}\}"\s*$',
                    changes) is not None)
    check("push null-SHA fails closed",
          "0000000000000000000000000000000000000000" in changes)
    # Schedule and merge_group keep the full-run latch (standard rule 5):
    # a dedicated non-PR, non-push branch emits all-true.
    check("schedule and merge_group keep the full latch",
          re.search(r'(?ms)else\n\s+# schedule, merge_group: full suite',
                    changes) is not None)

# The aggregator: runs unless cancelled, needs every gated job plus the detector and
# the always-run secret scan.
ciok = bodies.get("ci-ok")
check("ci-ok exists", ciok is not None)
if ciok is not None:
    # !cancelled(), never always(): always() ignores cancellation, so a
    # superseded run's ci-ok queues on the saturated pool and holds the PR
    # concurrency group for the new head (review case_13d22ce3).
    check("ci-ok runs unless cancelled",
          re.search(r"(?m)^    if:\s*\$\{\{\s*!cancelled\(\)\s*\}\}\s*$", ciok) is not None)
    check("ci-ok never uses always()",
          re.search(r"(?m)^    if:.*always\(\)", ciok) is None)
    # Exact needs-list membership, NOT substring: every job's runs-on carries
    # its own overflow key, so `"suites" in ciok` is true even with suites
    # dropped from needs (case_a2b959d4 caught this blind spot while consolidating).
    ciok_needs = [m.group(1) for m in
                  re.finditer(r"(?m)^      - ([a-zA-Z0-9_-]+)\s*$", ciok)]
    check("ci-ok needs every gated job",
          all(j in ciok_needs for j in list(HEAVY) + ["changes", "secret-scan"]))

# Secrets always run (CI standard rule 6): no needs, no if. The isolation
# gate pins its job/event opt-in overflow route and isolated fallback.
scan = bodies.get("secret-scan")
check("secret-scan exists", scan is not None)
if scan is not None:
    check("secret-scan has no job-level if",
          re.search(r"(?m)^    if:\s*", scan) is None)
    check("secret-scan needs nothing",
          re.search(r"(?m)^    needs:\s*", scan) is None)
    check("secret-scan runs the scan script",
          "secret-scan.sh" in scan)
    # Pipeline review 2026-10-10 (#7): the repo-split guards run as steps
    # of this always-run job; losing one would let a moved path re-land.
    check("secret-scan runs the model-selection moved guard",
          "Fail if the moved plugin path is touched" in scan)
    check("secret-scan runs the public-owned freeze guards",
          "Fail if a toolkit-owned path is touched" in scan
          and "Fail if the freeze list drifted from its generator" in scan)

# Draft PRs skip heavy jobs but ci-ok still reports: the detector tests the
# draft flag and emits all-false (never all-true, never empty).
check("draft PRs skip heavy jobs",
      "github.event.pull_request.draft" in text)

# Triggers: push-to-main is change-gated (case_e41d7023) while the nightly
# schedule keeps the FULL suite (standard rule 5); pull_request carries
# activity types but NO paths filter, so no path filter can silence a
# required check on some PRs (standard rule 2);
# ready_for_review is pinned so a draft that flips to ready re-runs the
# gated set (CISO case_f11bc226: without it a ready PR keeps its all-skipped
# draft verdict with ci-ok green).
check("push to main trigger present",
      re.search(r"(?m)^  push:\s*$", text) is not None
      and "branches: [main]" in text)
check("nightly schedule present",
      re.search(r"(?m)^  schedule:\s*$", text) is not None
      and re.search(r"(?m)^\s+-\s*cron:", text) is not None)
# case_ec2539bf: the nightly is the backstop for the change-gated push path, so a
# push to main must not be able to cancel it. A schedule/dispatch run on main
# has github.ref == refs/heads/main; sharing the push group with
# cancel-in-progress false still replaces the ONE pending run in that group
# (2026-10-06: the 02:41 nightly was cancelled with zero jobs by the next
# push). The pre-release full run is a workflow_dispatch that rides the same
# full latch, so it needs the same isolation.
conc = re.search(r"(?ms)^concurrency:\n(?P<b>.*?)^\S", text + "\nx")
group_line = re.search(r"(?m)^  group: (?P<g>.+)$", conc.group("b")) if conc else None
group_expr = group_line.group("g") if group_line else ""
check("workflow_dispatch trigger present (pre-release full run)",
      re.search(r"(?m)^  workflow_dispatch:\s*$", text) is not None)
check("nightly and dispatch runs have their own concurrency group",
      "github.event_name == 'schedule'" in group_expr
      and "github.event_name == 'workflow_dispatch'" in group_expr
      and "github.run_id" in group_expr)
check("push keeps the ref-keyed concurrency group",
      group_expr.rstrip().endswith("|| github.ref }}")
      and "github.event.pull_request.number" in group_expr)
pr_trig = re.search(r"(?ms)^  pull_request:\n(?P<block>.*?)^  merge_group:",
                    text)
pr_block = pr_trig.group("block") if pr_trig else ""
check("pull_request trigger has no paths filter",
      pr_trig is not None and "paths:" not in pr_block)
check("pull_request re-runs on ready_for_review",
      re.search(r"(?m)^\s+types:\s*\[.*ready_for_review", pr_block) is not None)

# The drain keeps describing red runs; it must not become a required gate.
# And it must SEE every heavy suite (case_8089241f): `if: failure()` only fires
# on NEEDED jobs, so a suite missing from `needs:` never triggers the drain
# on a run where only it failed (cliproxy-insight-suite was the case).
drain = bodies.get("failure-log-drain")
check("failure-log-drain exists", drain is not None)
if drain is not None:
    check("failure-log-drain still runs on failure only",
          re.search(r"(?m)^    if:\s*failure\(\)\s*$", drain) is not None)
    drain_needs = [m.group(1) for m in
                   re.finditer(r"(?m)^      - ([a-zA-Z0-9_-]+)\s*$", drain)]
    check("failure-log-drain needs every heavy suite",
          all(j in drain_needs for j in HEAVY))
    # PR #857: the repo-split guards run as steps of always-run secret-scan,
    # which no other drain-needed job needs -- without this edge a run where
    # only a guard fails produces no failure bundle.
    check("failure-log-drain needs secret-scan",
          "secret-scan" in drain_needs)

failed = [n for n, ok in checks.items() if not ok]
if failed:
    raise SystemExit(1)
PY
