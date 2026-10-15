#!/usr/bin/env python3
"""Scheduled re-sweep of Operator workspace drift (case_9c941ce5, parent case_af881f62).

One run for the host timer:

1. Dry-run: classify every Operator card (read-only).
2. Apply, only when the dry-run found flip candidates, consuming that
   dry-run's backup. The reconciler re-verifies each card before its PATCH,
   so a plan that went stale in between can only skip a card, never flip one
   that moved.
3. Re-plan (read-only). A candidate or unreadable card left behind is a
   failure; so is an empty Operator card list, which must never read as clean.

The final plan is written to <state-dir>/last-plan.json in the reconciler's
--json shape, so the copy-risk gate (ops/case_629b4da2) can read it. Backups and
restore files also live in <state-dir>.

Exit: 0 clean, 1 refused, aborted or residue, 2 usage.
"""
import argparse
import contextlib
import json
import os
import sys

import operator_workspace_reconciler as rec

PLAN_NAME = "last-plan.json"


@contextlib.contextmanager
def in_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def write_json(path, data):
    staging = path + ".tmp"
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(staging, path)


def unreadable_ids(plan):
    return [entry["identifier"] for entry in plan if entry["verdict"] == "error"]


def residue_ids(plan):
    return [
        entry["identifier"] for entry in plan
        if entry["verdict"] in ("candidate", "error")
    ]


def read_plan(client, fetch_workers):
    _, plan = rec.run_dry_run(client, rec.OPERATOR_AGENT_ID, workers=fetch_workers)
    if not plan:
        raise rec.ReconcileError(
            "Operator card list came back empty; refusing to report clean")
    return plan


def clear_plan(state_dir):
    try:
        os.remove(os.path.join(os.path.abspath(state_dir), PLAN_NAME))
    except FileNotFoundError:
        pass


def sweep(client, state_dir, rate_limit_seconds, fetch_workers, stamp):
    state_dir = os.path.abspath(state_dir)
    os.makedirs(state_dir, exist_ok=True)
    operator_agent_id = rec.OPERATOR_AGENT_ID
    plan_path = os.path.join(state_dir, PLAN_NAME)
    plan = read_plan(client, fetch_workers)
    counts = rec.plan_counts(plan)
    if not counts["candidates"] and not unreadable_ids(plan):
        write_json(plan_path, {"counts": counts, "plan": plan, "backup": None})
        print(f"sweep: no flip candidates among {len(plan)} Operator cards; "
              "nothing applied")
        return 0

    backup_path = None
    if counts["candidates"]:
        backup_path = os.path.join(state_dir, f"sweep-backup-{stamp}.json")
        write_json(backup_path, rec.build_backup(plan, stamp, operator_agent_id))
        print(f"sweep: {counts['candidates']} flip candidate(s) of {len(plan)} "
              f"Operator cards; backup -> {backup_path}")
        apply_args = rec.parse_args([
            "--apply", "--backup", backup_path,
            "--rate-limit-seconds", str(rate_limit_seconds),
        ])
        with in_directory(state_dir):
            rec.do_apply(client, apply_args)
        plan = read_plan(client, fetch_workers)
        counts = rec.plan_counts(plan)

    write_json(plan_path, {"counts": counts, "plan": plan, "backup": backup_path})
    left = residue_ids(plan)
    if left:
        raise rec.ReconcileError(
            f"{len(left)} card(s) still need attention after the sweep: "
            + ", ".join(left))
    print("sweep: 0 flip candidates and 0 unreadable cards remain")
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Re-sweep Operator workspace drift (case_9c941ce5).")
    parser.add_argument("--state-dir", required=True,
                        help="directory for the sweep backup, restore and "
                        "last-plan files")
    parser.add_argument("--rate-limit-seconds", type=float, default=1.0,
                        help="sleep between mutating calls (default 1.0)")
    parser.add_argument("--fetch-workers", type=int, default=8,
                        help="bounded thread pool for per-card reads (default 8)")
    args = parser.parse_args(argv)
    if args.rate_limit_seconds < 0:
        parser.error("--rate-limit-seconds must be >= 0")
    if args.fetch_workers < 1:
        parser.error("--fetch-workers must be >= 1")
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    clear_plan(args.state_dir)
    try:
        client = rec.PaperclipClient(
            os.environ.get("PAPERCLIP_API_URL"),
            os.environ.get("PAPERCLIP_API_KEY"),
            os.environ.get("PAPERCLIP_COMPANY_ID"),
            os.environ.get("PAPERCLIP_RUN_ID"),
        )
        return sweep(client, args.state_dir, args.rate_limit_seconds,
                     args.fetch_workers, rec.utcnow().strftime("%Y%m%dT%H%M%SZ"))
    except rec.ReconcileError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
