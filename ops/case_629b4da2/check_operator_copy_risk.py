#!/usr/bin/env python3
"""Copy-risk gate for Operator SSH runs (case_629b4da2).

The Paperclip SSH driver uploads a card's whole project workspace on every
run whose card carries one, then syncs it back. That full copy is what grew
/home/paperclip-operator/workspace/.paperclip-runtime/runs to 208G in 331
folders on 2026-10-11 (~30G per run) and caused the /home 100% + PostgreSQL
ENOSPC incident on 2026-10-10.

The reconciler (ops/case_af881f62, merged as PR #813) detaches project
workspaces from Operator cards. This gate proves the project-workspace
trigger is gone: it reads one reconciler dry-run JSON plan (``--json``
output) and fails when any non-terminal Operator card still carries a
workspace, i.e. when a future run could upload a full copy again.

Known residual (2026-10-11, run 385b9bb7 / case_fb0653d6): the Operator
``agent_default`` fallback workspace
(``/paperclip/instances/default/workspaces/be571a9f-...``, ~6.7 GB) is
uploaded in full per run even for gate-clean cards. Gate-clean is
necessary but not sufficient for the <500 MB uploaded target; the
controller-side needed-content-only sync fix must cover fallback
workspaces as well as project worktrees (see RUNBOOK.md section 3).

Hermetic: reads one local JSON file, makes no network calls, needs no
credentials. Exit 0 = clean, 1 = copy risk present, 2 = bad input.

Usage:
  python3 ops/case_af881f62/operator_workspace_reconciler.py --json \
      --backup-out /tmp/operator-backup.json > /tmp/operator-plan.json
  python3 ops/case_629b4da2/check_operator_copy_risk.py --plan /tmp/operator-plan.json

The plan must be the reconciler's stdout JSON ({counts, plan, backup}),
not the --backup-out file ({version, entries, ...}): the backup holds
candidates only and has no 'plan' list, so pointing --plan at it is
refused with exit 2.
"""
import argparse
import json
import sys

# Mirror of operator_workspace_reconciler.is_clean / WORKSPACE_FIELDS /
# FLIP_PREFERENCE (ops/case_af881f62). Duplicated, not imported, so this gate
# stays a single file the host timer can run without a package layout.
# ParityTest in test_check_operator_copy_risk.py imports both modules and
# fails when the mirror drifts; if the reconciler changes what "carries a
# workspace" means, update both.
WORKSPACE_FIELDS = (
    "executionWorkspacePreference",
    "executionWorkspaceId",
    "projectWorkspaceId",
    "executionWorkspaceSettings",
)
FLIP_PREFERENCE = "agent_default"
TERMINAL_STATUSES = ("done", "cancelled")


def is_clean(fields):
    """Pure: True when the card carries no project workspace (nothing to copy)."""
    if not isinstance(fields, dict):
        return False
    return (
        fields.get("executionWorkspacePreference") == FLIP_PREFERENCE
        and not fields.get("executionWorkspaceId")
        and not fields.get("projectWorkspaceId")
        and not fields.get("executionWorkspaceSettings")
    )


def original_of(entry):
    """Pure: the workspace fields recorded on one plan entry."""
    original = entry.get("original") if isinstance(entry, dict) else None
    if isinstance(original, dict):
        return {field: original.get(field) for field in WORKSPACE_FIELDS}
    return {field: None for field in WORKSPACE_FIELDS}


def at_risk_entries(plan):
    """Pure: non-terminal plan entries that still carry a workspace.

    Terminal cards never run again, so a workspace on a done/cancelled
    card cannot trigger a future copy and is not risk. Entries the
    reconciler could not read (verdict 'error': full-read-unreadable,
    run-list-unreadable) are risk too: their recorded fields come from
    the list row, which always nulls executionWorkspaceSettings, so a
    settings-only carrier looks clean exactly when its true state is
    unknown. Fail closed on unreadable entries, never open. Known
    residual: live-work-or-review-path entries keep the list-row fields
    (the reconciler refines candidates and apparently-clean cards only),
    so an in_progress/in_review card whose only carrier is a settings
    override reads as clean until it goes idle and is re-swept
    (see RUNBOOK.md section 2).
    """
    risky = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in TERMINAL_STATUSES:
            continue
        if entry.get("verdict") == "error":
            risky.append(entry)
            continue
        if not is_clean(original_of(entry)):
            risky.append(entry)
    return risky


def summarize(entry):
    """Pure: one human-readable line for a risky entry."""
    return "{} {} {}:{}".format(
        entry.get("identifier", "?"),
        entry.get("status", "?"),
        entry.get("verdict", "?"),
        entry.get("reason", "?"),
    )


def check(plan):
    """Pure: (exit_code, report_lines) for one reconciler plan list."""
    risky = at_risk_entries(plan)
    if not risky:
        return 0, ["clean: no non-terminal Operator card visibly carries a workspace"
                   " (list-row read; live-work settings-only carriers are a known"
                   " residual, see RUNBOOK.md section 2)"]
    lines = [
        "copy-risk: {} non-terminal Operator card(s) still carry a workspace".format(
            len(risky)
        )
    ]
    lines.extend(summarize(entry) for entry in risky)
    return 1, lines


def load_plan(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read plan {}: {}".format(path, exc)) from exc
    plan = data.get("plan") if isinstance(data, dict) else None
    if not isinstance(plan, list):
        raise ValueError("plan {}: no 'plan' list; want reconciler --json output".format(path))
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fail when any live Operator card could trigger a full workspace copy."
    )
    parser.add_argument("--plan", required=True,
                        help="reconciler dry-run plan JSON: the --json stdout, "
                             "not the --backup-out file")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
    except ValueError as exc:
        print("refused: {}".format(exc), file=sys.stderr)
        return 2
    code, lines = check(plan)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
