#!/usr/bin/env python3
"""terminal_worktree_prune.py — remove idle git worktrees of done/cancelled cards. (case_b1d141af.)

WHY THIS SCRIPT EXISTS.

node7b1ff338 /home hit 94% on 2026-10-04 (45 GB free) with 2,474 active git_worktree
execution workspaces on disk, 1,444 of them owned only by cards that closed
more than 24h ago. The recurring dep-cache reclaim (ops/case_b5184419) deliberately
never deletes a worktree itself, so every checkout (~20 MB each, 29.7 GiB for
the 1,444) stays forever. Paperclip archives a workspace when its card turns
terminal for 212 rows only; the rest were never cleaned. This tool removes the
worktree DIRECTORY (and with it any cargo `target/` and other ignored caches)
and keeps the branch, its commits, and every ref in the shared repository.

WHAT IT REUSES. Every card/live-run/process/pushed-HEAD gate is the reviewed
`evaluate_worktree` from ops/case_b5184419 (case_f2c6fc48, PR #522), imported, not
copied. A worktree reaches the removal stage only when that function reports
no exclusion reason. This tool then adds, in order:

- Isolated-worktree rows only. Every execution-workspace row for the path must
  be `providerType=git_worktree` + `mode=isolated_workspace`. A shared/local_fs
  row means the path is a project root: keep. `.git` must be a FILE (a linked
  worktree); a directory `.git` is a full clone or a project root: keep.
- Terminal for longer than --min-age-hours (default 24). Age is measured from
  completedAt (done) / cancelledAt (cancelled), falling back to updatedAt, and
  the NEWEST linked card governs. The workspace row's lastUsedAt must also be
  older than the window. An unparseable or missing timestamp keeps the
  worktree (fail closed).
- Clean. `git status --porcelain --untracked-files=all` must be empty and rc 0.
  A dirty worktree is REPORTED, never removed. Ignored files (node_modules,
  target/) do not count as dirty: they are regenerable and go with the dir.
- No unpushed HEAD commits and no stash naming the branch (the 12910 rule,
  proven against refs/pull/*/head so a merged-and-auto-deleted branch counts
  as pushed). The branch survives removal either way; this is belt and braces.
- Not locked (`<gitdir>/locked`), not the main working tree.

WHY EACH SAFETY PROPERTY IS STRUCTURAL.

- Dry-run is the default. NOTHING is removed without `--apply`, and `--apply`
  re-evaluates everything fresh in the same process (never a stored report).
- Immediately before each removal the tool re-reads every linked card, the
  /live-runs list, the clean-tree check and the recent-activity check. A card
  reopened between scan and removal, a live run, a freshly dirty tree, or any
  error in those re-reads skips the worktree.
- `git worktree remove` is called WITHOUT --force/-f. Git itself refuses a
  modified/untracked/locked worktree, so a race the checks missed still ends in
  a refusal, not a loss. `remove_argv()` pins this.
- A kill switch forces dry-run: env WORKTREE_PRUNE_DISABLED=1, the file
  `<root>/.worktree-prune.disabled`, or the case_5e07b8c4 cutover marker
  `<root>/.case_e129e5c6-cutover-active`. Any agent inside the container can create
  the file; delete it to resume. The report says `kill_switch` loudly.
- Bounded: --max-remove caps removals per pass (largest checkouts first).
- Fail closed: no API env, an API error during the fresh evaluation, or any
  git error means keep. A blind apply is refused.

MODES.

  python3 ops/case_b1d141af/terminal_worktree_prune.py
      Dry-run (default): scan, size, print the summary, write a report.
      Removes nothing. Needs read API access + git.

  python3 ops/case_b1d141af/terminal_worktree_prune.py --apply --max-remove N
      Re-evaluate, re-read each card right before removal, remove up to N
      worktrees, write the report with the freed bytes.

Exit codes: 0 report/apply complete, 1 safety refusal (blind apply), 2 usage or
fatal error (API unreachable, no out dir), 3 apply finished but at least one
removal command failed (LOUD in the journal).

No secrets in output: stdout carries only worktree paths, card identifiers,
statuses, counts and byte totals. Credentials are read from the environment,
never printed, never on argv.

Rollback: nothing pushed is lost. `git worktree add <path> <branch>` recreates
a worktree from the surviving branch (Paperclip also re-realizes a workspace
when a card is reopened). Revert the commit to stop the tool.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "case_b5184419"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "lib"))

import terminal_worktree_cache_reclaim as reclaim  # noqa: E402
from atomic_state import write_json_atomic  # noqa: E402

TERMINAL_STATUSES = reclaim.TERMINAL_STATUSES
MIN_AGE_HOURS_DEFAULT = 24
MAX_REMOVE_DEFAULT = 200
RECENT_MINUTES_DEFAULT = reclaim.RECENT_MINUTES_DEFAULT
EVAL_WORKERS_DEFAULT = reclaim.EVAL_WORKERS_DEFAULT
KEEP_REPORTS = 14
KILL_FILE_NAME = ".worktree-prune.disabled"
CUTOVER_FILE_NAME = ".case_e129e5c6-cutover-active"
SAMPLE_SIZE = 10
DIRTY_LIST_LIMIT = 50


class PruneError(RuntimeError):
    """Loud, fatal, non-safety failure: API error, usage error, I/O error."""


class SafetyRefusal(RuntimeError):
    """Loud safety refusal: blind apply."""


def _valid_api_base(base):
    # BASE host validation: the bearer below is sent ONLY to this base.
    # Refuse a base that is not an http(s) URL with a non-empty bare host:
    # no userinfo (`user@host` misdirects the credential), no control
    # characters or DEL -- raw anywhere, or %-encoded in the authority
    # (urllib decodes those before the transport, which then raises outside
    # the module's error contract) -- no query or fragment (the appended
    # /api path would be swallowed into them), a real hostname (an
    # empty `:port`-only authority is not a host), a numeric port, and no
    # `{`/`}` anywhere (curl expands `{a,b}` globs, so one base is one host
    # -- parity with the shell gate). A well-formed hostile host still
    # passes; blocking that needs an operator-owned host allowlist, not a
    # per-script guess.
    if not base.startswith(("http://", "https://")):
        return False
    if "@" in base:
        return False
    if any(ord(char) < 0x21 or ord(char) == 0x7f for char in base):
        return False
    if "{" in base or "}" in base:
        return False
    try:
        parts = urllib.parse.urlsplit(base)
    except ValueError:
        return False
    if parts.username or parts.password:
        return False
    if parts.query or parts.fragment:
        return False
    decoded_netloc = urllib.parse.unquote(parts.netloc)
    if any(ord(char) < 0x21 or ord(char) == 0x7f for char in decoded_netloc):
        return False
    host = parts.hostname or ""
    if not host:
        return False
    if any(char in "[]{}," for char in host):
        return False
    if not parts.netloc.startswith("[") and parts.netloc.count(":") > 1:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None and parts.netloc.endswith(":"):
        return False
    return True


# ---------------------------------------------------------------------------
# PURE CORE. No network, no clock reads (callers pass `now`), no environment.
# The offline suite drives every branch with fixtures and throwaway git repos.
# ---------------------------------------------------------------------------

def parse_ts(value):
    """Epoch seconds from an ISO-8601 string, or None when unusable."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def card_terminal_ts(card):
    """When the card became terminal: completedAt / cancelledAt, else updatedAt."""
    status = card.get("status")
    key = {"done": "completedAt", "cancelled": "cancelledAt"}.get(status)
    stamp = parse_ts(card.get(key)) if key else None
    if stamp is None:
        stamp = parse_ts(card.get("updatedAt"))
    return stamp


def age_gate(cards, workspace_rows, now, min_age_hours):
    """Every card terminal for >= min_age_hours AND workspace idle as long.

    The NEWEST terminal timestamp across all linked cards governs, so one
    freshly closed sibling keeps the shared worktree. Returns None to proceed,
    else (reason, detail). Missing/unparseable timestamps keep (fail closed).
    """
    window = min_age_hours * 3600
    newest = None
    for card in cards:
        stamp = card_terminal_ts(card)
        if stamp is None:
            return ("no-card-timestamp", {"card": card.get("identifier")})
        newest = stamp if newest is None else max(newest, stamp)
    if newest is None:
        return ("no-card-timestamp", {})
    if now - newest < window:
        return ("card-too-recent",
                {"age_hours": round((now - newest) / 3600, 1)})
    for row in workspace_rows:
        used = parse_ts(row.get("lastUsedAt"))
        if used is None:
            return ("no-last-used", {"workspace": row.get("id")})
        if now - used < window:
            return ("workspace-recently-used",
                    {"age_hours": round((now - used) / 3600, 1)})
    return None


def workspace_row_gate(rows):
    """Only isolated git_worktree rows may be removed; anything else is a root."""
    if not rows:
        return ("no-db-workspace", {})
    for row in rows:
        if (row.get("providerType") != "git_worktree"
                or row.get("mode") != "isolated_workspace"):
            return ("not-an-isolated-worktree-row",
                    {"providerType": row.get("providerType"),
                     "mode": row.get("mode")})
    return None


def dirty_gate(status_rc, status_out):
    """A worktree with tracked changes or untracked files is reported, not removed."""
    if status_rc != 0:
        return ("git-status-error", {})
    lines = [line for line in (status_out or "").splitlines() if line.strip()]
    if lines:
        return ("dirty", {"entries": len(lines),
                          "sample": [line[3:] for line in lines[:3]]})
    return None


def removable_result(res):
    """Branch-scoped removal rule layered on the 12910 evaluation."""
    git = res.get("git") or {}
    return (res["reason"] is None
            and not git.get("branch_unpushed")
            and not git.get("branch_stash"))


def remove_argv(main_root_or_gitdir, worktree, bare=False):
    """The ONE place the removal argv is built. PINNED: never --force / -f.

    Git refuses to remove a modified, untracked-containing or locked worktree
    unless forced. Forcing would turn every race the pre-checks missed into
    data loss, so the flag must never appear here.
    """
    if bare:
        return ["git", "--git-dir", main_root_or_gitdir, "worktree", "remove",
                "--", worktree]
    return ["git", "-C", main_root_or_gitdir, "worktree", "remove", "--",
            worktree]


def kill_switch(env, root, exists=os.path.exists):
    """(tripped, why) from the env var and the two marker files."""
    if str(env.get("WORKTREE_PRUNE_DISABLED", "")).strip().lower() in (
            "1", "true", "yes", "on"):
        return (True, "env WORKTREE_PRUNE_DISABLED")
    for name in (KILL_FILE_NAME, CUTOVER_FILE_NAME):
        if exists(os.path.join(root, name)):
            return (True, "file %s" % os.path.join(root, name))
    return (False, None)


def order_for_removal(candidates):
    """Largest checkouts first, then oldest path: most GB per bounded removal."""
    return sorted(candidates, key=lambda r: (-r.get("bytes", 0), r["w"]))


def parse_argv(argv):
    parser = argparse.ArgumentParser(
        description="Remove idle git worktrees of done/cancelled cards. "
                    "Dry-run unless --apply.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually remove (re-evaluates everything fresh).")
    parser.add_argument("--root", default="/paperclip/instances/default",
                        help="Paperclip instance dir to scan.")
    parser.add_argument("--min-age-hours", type=float,
                        default=MIN_AGE_HOURS_DEFAULT)
    parser.add_argument("--max-remove", type=int, default=MAX_REMOVE_DEFAULT,
                        help="Cap on removals per pass (0 = observe only).")
    parser.add_argument("--recent-minutes", type=int,
                        default=RECENT_MINUTES_DEFAULT)
    parser.add_argument("--workers", type=int, default=EVAL_WORKERS_DEFAULT)
    parser.add_argument("--out-dir", default=None,
                        help="Report directory (default: <root>/data/"
                             "worktree-prune).")
    opts = parser.parse_args(argv)
    if opts.max_remove < 0 or opts.min_age_hours < 0:
        parser.error("--max-remove and --min-age-hours must be >= 0")
    return opts


# ---------------------------------------------------------------------------
# LIVE SHELL.
# ---------------------------------------------------------------------------

def git_dirs(run, worktree):
    """(git_dir, common_dir) as real paths, or None on any git error."""
    rc1, gd = run(["git", "rev-parse", "--git-dir"], cwd=worktree)
    rc2, cd = run(["git", "rev-parse", "--git-common-dir"], cwd=worktree)
    if rc1 != 0 or rc2 != 0 or not gd.strip() or not cd.strip():
        return None
    return (os.path.realpath(os.path.join(worktree, gd.strip())),
            os.path.realpath(os.path.join(worktree, cd.strip())))


def linked_worktree_gate(run, worktree):
    """Return (None, info) for a linked, unlocked, non-main worktree.

    Else ((reason, detail), None). Never raises.
    """
    git_file = os.path.join(worktree, ".git")
    if not os.path.isfile(git_file) or os.path.islink(git_file):
        return (("not-a-linked-worktree", {}), None)
    dirs = git_dirs(run, worktree)
    if dirs is None:
        return (("git-error", {}), None)
    git_dir, common = dirs
    if git_dir == common or not git_dir.startswith(common + os.sep):
        return (("not-a-linked-worktree", {}), None)
    if os.path.exists(os.path.join(git_dir, "locked")):
        return (("locked", {}), None)
    bare = os.path.basename(common) != ".git"
    main_root = common if bare else os.path.dirname(common)
    if os.path.realpath(main_root) == os.path.realpath(worktree):
        return (("main-working-tree", {}), None)
    return (None, {"common": common, "main_root": main_root, "bare": bare,
                   "git_dir": git_dir})


IN_PROGRESS_MARKERS = ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                       "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
                       "sequencer")


def operation_gate(git_dir, exists=os.path.exists):
    """A half-finished rebase/merge/cherry-pick keeps its state only here."""
    for name in IN_PROGRESS_MARKERS:
        if exists(os.path.join(git_dir, name)):
            return ("operation-in-progress", {"marker": name})
    return None


def evaluate_prune(run, ctx, worktree, minutes, now, min_age_hours):
    """All gates for one worktree: the 12910 evaluation, then the prune gates.

    Never raises: any unexpected error keeps the worktree (fail closed).
    """
    try:
        return _evaluate_prune(run, ctx, worktree, minutes, now,
                               min_age_hours)
    except Exception as exc:
        return {"w": worktree, "reason": "eval-error",
                "det": {"error": type(exc).__name__}, "prune": None,
                "git": None}


def _evaluate_prune(run, ctx, worktree, minutes, now, min_age_hours):
    res = reclaim.evaluate_worktree(run, ctx, worktree, minutes)
    res["prune"] = None
    if res["reason"] is not None:
        return res
    ews = ctx["ew_by_cwd"].get(worktree, [])
    cards = [ctx["issues"].get(i) for i in res["linked_ids"].values()]
    gate = workspace_row_gate(ews)
    if gate is None and (not cards or not all(cards)):
        gate = ("card-unknown", {})
    if gate is None:
        gate = age_gate(cards, ews, now, min_age_hours)
    if gate is None:
        gate, info = linked_worktree_gate(run, worktree)
        res["prune"] = info
    if gate is None:
        gate = operation_gate(info["git_dir"])
    if gate is None:
        gate = dirty_gate(*run(["git", "status", "--porcelain",
                                "--untracked-files=all"], cwd=worktree))
    if gate is None and not removable_result(res):
        gate = (("unpushed-head", {}) if (res.get("git") or {}).get(
            "branch_unpushed") else ("branch-stash", {}))
    if gate is not None:
        res["reason"], res["det"] = gate[0], dict(gate[1])
        res["det"]["cards"] = sorted(res["linked_ids"])
    return res


def cargo_target_bytes(run, worktree):
    target = os.path.join(worktree, "target")
    if (os.path.isdir(target) and not os.path.islink(target)
            and os.path.exists(os.path.join(worktree, "Cargo.toml"))):
        return reclaim.dir_bytes(run, target)
    return 0


def size_candidates(run, candidates, workers):
    def one(res):
        return (reclaim.dir_bytes(run, res["w"]),
                cargo_target_bytes(run, res["w"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for res, (total, cargo) in zip(candidates,
                                       pool.map(one, candidates)):
            res["bytes"], res["cargo_target_bytes"] = total, cargo


def revalidate(run, api, res, minutes):
    """Fresh proof immediately before removal. Returns None or a skip reason."""
    linked = res.get("linked_ids") or {}
    if not linked:
        return "no-linked-card"
    try:
        live_issue_ids = {row.get("issueId") for row in api.live_runs()
                          if row.get("issueId")}
    except Exception:
        return "live-runs-unreadable"
    for ident, issue_id in sorted(linked.items()):
        try:
            status = api.issue_status(issue_id)
        except Exception:
            return "card-reread-failed"
        if status not in TERMINAL_STATUSES:
            return "card-reopened"
        if issue_id in live_issue_ids:
            return "live-run-appeared"
    worktree = res["w"]
    gate = dirty_gate(*run(["git", "status", "--porcelain",
                            "--untracked-files=all"], cwd=worktree))
    if gate is not None:
        return "became-" + gate[0]
    if reclaim.recent_activity(run, worktree, minutes):
        return "became-active"
    return None


def apply_removals(run, api, candidates, max_remove, minutes):
    """Remove up to max_remove worktrees, re-validating each one first."""
    removed, errors, skipped = [], [], collections.Counter()
    for res in order_for_removal(candidates):
        if len(removed) >= max_remove:
            skipped["over-max-remove"] += 1
            continue
        why = revalidate(run, api, res, minutes)
        if why is not None:
            skipped[why] += 1
            continue
        info = res["prune"]
        branch = (res.get("git") or {}).get("branch") or ""
        argv = remove_argv(info["common"] if info["bare"]
                           else info["main_root"], res["w"], info["bare"])
        try:
            rc, _ = run(argv, cwd=None, timeout=900)
        except subprocess.TimeoutExpired:
            rc = 124
        if rc != 0 or os.path.exists(res["w"]):
            errors.append({"w": res["w"], "rc": rc})
            continue
        branch_kept = True
        if branch:
            rc2, _ = run(["git", "--git-dir", info["common"], "rev-parse",
                          "--verify", "-q", "refs/heads/" + branch])
            branch_kept = rc2 == 0
        removed.append({"w": res["w"], "bytes": res.get("bytes", 0),
                        "cargo_target_bytes": res.get("cargo_target_bytes", 0),
                        "branch": branch, "branch_kept": branch_kept})
    return {"removed": len(removed),
            "removed_bytes": sum(r["bytes"] for r in removed),
            "removed_cargo_target_bytes": sum(r["cargo_target_bytes"]
                                              for r in removed),
            "branch_missing_after_remove": [r["w"] for r in removed
                                            if not r["branch_kept"]],
            "skipped": dict(skipped), "errors": errors[:20],
            "n_errors": len(errors),
            "removed_sample": [r["w"] for r in removed[:SAMPLE_SIZE]]}


def disk_snapshot(root):
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None
    # `df` reports used / (used + avail): reserved blocks fall out of both
    # sides. Mirror it so the % here matches the number operators watch.
    return {"total_bytes": usage.total, "used_bytes": usage.used,
            "free_bytes": usage.free, "df_total_bytes": usage.used + usage.free,
            "used_pct": round(100.0 * usage.used
                              / (usage.used + usage.free), 2)}


def summarize(results, candidates, disk, killed, apply_report, min_age_hours,
              max_remove):
    excluded = collections.Counter(r["reason"] for r in results if r["reason"])
    total = sum(r.get("bytes", 0) for r in candidates)
    cargo = sum(r.get("cargo_target_bytes", 0) for r in candidates)
    dirty = [{"w": r["w"], **{k: v for k, v in r["det"].items()
                              if k in ("entries", "sample", "cards")}}
             for r in results if r["reason"] == "dirty"]
    projected = None
    if disk:
        projected = round(100.0 * (disk["used_bytes"] - total)
                          / (disk["used_bytes"] + disk["free_bytes"]), 2)
    return {
        "worktree_dirs_scanned": len(results),
        "min_age_hours": min_age_hours,
        "max_remove": max_remove,
        "kill_switch": killed,
        "candidates": len(candidates),
        "candidate_bytes": total,
        "candidate_gib": round(total / 2 ** 30, 2),
        "candidate_cargo_target_bytes": cargo,
        "candidate_cargo_target_gib": round(cargo / 2 ** 30, 2),
        "kept_by_reason": dict(excluded),
        "dirty_reported": len(dirty),
        "dirty_sample": dirty[:DIRTY_LIST_LIMIT],
        "sample": [{"w": r["w"], "gib": round(r.get("bytes", 0) / 2 ** 30, 3),
                    "cards": r["det"].get("cards")}
                   for r in order_for_removal(candidates)[:SAMPLE_SIZE]],
        "disk_before": disk,
        "disk_used_pct_if_all_candidates_removed": projected,
        "apply": apply_report,
    }


def enumerate_worktrees(root):
    return reclaim.enumerate_worktrees(root)


def write_report(out_dir, report, stamp):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "prune-%s.json" % stamp)
    write_json_atomic(path, report)
    reports = sorted(name for name in os.listdir(out_dir)
                     if re.fullmatch(r"prune-\d{8}T\d{6}Z\.json", name))
    for old in reports[:-KEEP_REPORTS]:
        os.remove(os.path.join(out_dir, old))
    return path


def run_tool(opts, run, api, env, clock=time.time):
    started = clock()
    killed, why = kill_switch(env, opts.root)
    apply_requested = opts.apply
    if killed:
        print("KILL-SWITCH (%s): apply disabled, dry-run only" % why,
              file=sys.stderr)
        apply_requested = False
    out_dir = opts.out_dir or os.path.join(opts.root, "data",
                                           "worktree-prune")
    ctx = reclaim.build_context(api)
    dirs = enumerate_worktrees(opts.root)
    print("worktree dirs on disk: %d" % len(dirs), file=sys.stderr)
    now = clock()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=opts.workers) as pool:
        results = list(pool.map(
            lambda w: evaluate_prune(run, ctx, w, opts.recent_minutes, now,
                                     opts.min_age_hours), dirs))
    candidates = [r for r in results if r["reason"] is None]
    size_candidates(run, candidates, opts.workers)
    disk = disk_snapshot(opts.root)
    apply_report = None
    if apply_requested:
        apply_report = apply_removals(run, api, candidates, opts.max_remove,
                                      opts.recent_minutes)
    summary = summarize(results, candidates, disk, why if killed else None,
                        apply_report, opts.min_age_hours, opts.max_remove)
    summary["took_s"] = round(clock() - started)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started))
    if apply_report is not None:
        summary["disk_after"] = disk_snapshot(opts.root)
    report_path = os.path.join(out_dir, "prune-%s.json" % stamp)
    try:
        summary["report"] = write_report(
            out_dir, {"summary": summary, "results": results}, stamp)
    except OSError as exc:
        print(json.dumps(summary, indent=1), flush=True)
        raise PruneError("%s: errno=%s" % (report_path, exc.errno)) from exc
    print(json.dumps(summary, indent=1))
    return summary


def main(argv=None, run=None, api=None, env=None):
    opts = parse_argv(argv if argv is not None else sys.argv[1:])
    env = os.environ if env is None else env
    run = run or reclaim.live_run
    if api is None:
        base = env.get("PAPERCLIP_API_URL")
        company = env.get("PAPERCLIP_COMPANY_ID")
        key = env.get("PAPERCLIP_API_KEY")
        if not (base and company and key):
            if opts.apply:
                raise SafetyRefusal(
                    "refusing --apply without PAPERCLIP_API_URL/COMPANY_ID/"
                    "KEY: the per-removal card re-read would be impossible")
            raise PruneError("API env is required even for dry-run (the "
                             "card join reads the live board)")
        if not _valid_api_base(base.rstrip("/")):
            if opts.apply:
                raise SafetyRefusal(
                    "refusing --apply with a PAPERCLIP_API_URL that is not "
                    "a clean http(s) base: the bearer would misdirect")
            raise PruneError(
                "refusing: PAPERCLIP_API_URL is not a clean http(s) base")
        api = reclaim.ApiClient(base, company, key)
    try:
        summary = run_tool(opts, run, api, env)
    except SafetyRefusal:
        raise
    except Exception as exc:  # API error during the fresh evaluation etc.
        raise PruneError("%s: %s" % (type(exc).__name__, exc)) from exc
    applied = summary.get("apply")
    return 3 if applied and applied["n_errors"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SafetyRefusal as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        sys.exit(1)
    except PruneError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(2)
