#!/usr/bin/env python3
# ===========================================================================
# test_terminal_worktree_prune.py — offline tests for terminal_worktree_prune.py
# (case_b1d141af).
#
# Written to FAIL a plausible-but-wrong implementation, not to confirm the one
# that exists. Every section names the mutant it kills. This tool DELETES
# directories on a production host, so the tests that matter most are the ones
# proving it refuses: a reopened card, a live run, a dirty tree, a fresh card,
# a shared project root, a locked or mid-rebase worktree, an unpushed commit,
# and a race the pre-checks missed (git itself must still refuse; --force
# must never appear).
#
# Deterministic: no network, no credentials. Git-touching paths run against
# throwaway local repos (a bare origin plus a clone); the API boundary is a
# fake. Real `git worktree remove` is exercised, never mocked.
#
#   python3 ops/case_b1d141af/test_terminal_worktree_prune.py
# ===========================================================================
import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import terminal_worktree_prune as tool  # noqa: E402
from terminal_worktree_prune import (  # noqa: E402
    PruneError,
    SafetyRefusal,
    age_gate,
    apply_removals,
    card_terminal_ts,
    dirty_gate,
    evaluate_prune,
    kill_switch,
    linked_worktree_gate,
    operation_gate,
    order_for_removal,
    parse_argv,
    parse_ts,
    remove_argv,
    removable_result,
    summarize,
    workspace_row_gate,
    write_report,
)

LIVE_RUN = tool.reclaim.live_run
HOUR = 3600


def iso(hours_ago, now=None):
    stamp = (now if now is not None else time.time()) - hours_ago * HOUR
    return datetime.datetime.fromtimestamp(
        stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def git(*args, cwd):
    proc = subprocess.run(["git"] + list(args), cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


def make_old(root, seconds_ago=7200):
    """Backdate every non-.git file so the 30-minute activity gate sees idle."""
    stamp = time.time() - seconds_ago
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (stamp, stamp))


def ws_row(ident, cwd, **over):
    row = {"id": "ws-" + ident, "status": "active", "cwd": cwd,
           "providerRef": cwd, "sourceIssueId": "id-" + ident,
           "providerType": "git_worktree", "mode": "isolated_workspace",
           "lastUsedAt": iso(48)}
    row.update(over)
    return row


def issue_row(ident, status="done", **over):
    row = {"id": "id-" + ident, "identifier": ident, "status": status,
           "completedAt": iso(48), "cancelledAt": None,
           "updatedAt": iso(48), "executionWorkspaceId": "ws-" + ident}
    row.update(over)
    return row


def make_ctx(rows, issues):
    ew_by_cwd, issues_by_ew = {}, {}
    for row in rows:
        ew_by_cwd.setdefault(os.path.realpath(row["cwd"]), []).append(row)
    for row in issues:
        issues_by_ew.setdefault(row["executionWorkspaceId"], []).append(row)
    return {"ew_by_cwd": ew_by_cwd,
            "issues": {row["id"]: row for row in issues},
            "by_ident": {row["identifier"]: row for row in issues},
            "issues_by_ew": issues_by_ew,
            "run_paths": set(), "run_issue_ids": set(), "run_ws_ids": set(),
            "proc_paths": set()}


class FakeApi:
    """Board fake: fresh per-card status, live runs, and the scan listings."""

    def __init__(self, statuses=None, live=(), status_error=False,
                 live_error=False, issues=(), workspaces=()):
        self.statuses = dict(statuses or {})
        self.live = list(live)
        self.status_error = status_error
        self.live_error = live_error
        self.issues = list(issues)
        self.workspaces = list(workspaces)
        self.status_reads = []

    def issue_status(self, issue_id):
        self.status_reads.append(issue_id)
        if self.status_error:
            raise OSError("board unreachable")
        return self.statuses[issue_id]

    def live_runs(self):
        if self.live_error:
            raise OSError("board unreachable")
        return self.live

    def execution_workspaces(self):
        return self.workspaces

    def all_issues(self):
        return {row["id"]: row for row in self.issues}

    def heartbeat_runs(self):
        return []


class RepoFixture:
    """A bare origin, a main clone under the Paperclip project layout, and
    helpers that add linked worktrees the way the realizer does."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.root = os.path.join(base, "instance")
        self.origin = os.path.join(base, "origin.git")
        self.main = os.path.join(self.root, "projects", "co", "proj", "repo")
        os.makedirs(os.path.dirname(self.main))
        git("init", "-q", "--bare", self.origin, cwd=base)
        git("clone", "-q", self.origin, self.main, cwd=base)
        for key, val in (("user.email", "t@t"), ("user.name", "t"),
                         ("commit.gpgsign", "false")):
            git("config", key, val, cwd=self.main)
        with open(os.path.join(self.main, ".gitignore"), "w") as handle:
            handle.write("target\nnode_modules\n*.log\n.paperclip\n")
        with open(os.path.join(self.main, "Cargo.toml"), "w") as handle:
            handle.write("[package]\nname = \"x\"\n")
        git("add", ".", cwd=self.main)
        git("commit", "-qm", "base", cwd=self.main)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.main)
        git("fetch", "-q", "origin", cwd=self.main)
        self.wtdir = os.path.join(self.main, ".paperclip", "worktrees")
        os.makedirs(self.wtdir)

    def close(self):
        self.tmp.cleanup()

    def add(self, name, target_mb=0):
        path = os.path.join(self.wtdir, name)
        git("worktree", "add", "-q", "-b", name, path, "origin/main",
            cwd=self.main)
        if target_mb:
            os.makedirs(os.path.join(path, "target", "debug"))
            with open(os.path.join(path, "target", "debug", "blob"),
                      "wb") as handle:
                handle.write(b"\0" * target_mb * 1024 * 1024)
        make_old(path)
        return os.path.realpath(path)

    def listed(self):
        out = git("worktree", "list", "--porcelain", cwd=self.main)
        return [line.split(" ", 1)[1] for line in out.splitlines()
                if line.startswith("worktree ")]

    def branch_exists(self, name):
        return subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "refs/heads/" + name],
            cwd=self.main, capture_output=True).returncode == 0


def evaluate(fx, path, ident, *, issue=None, row=None, now=None, min_age=24):
    issue = issue or issue_row(ident)
    row = row or ws_row(ident, path)
    ctx = make_ctx([row], [issue])
    return evaluate_prune(LIVE_RUN, ctx, path, 30,
                          now if now is not None else time.time(), min_age)


# ---------------------------------------------------------------------------
class TimestampTests(unittest.TestCase):
    # Kills: treating an unparseable stamp as "very old" (fail OPEN), or
    # reading a naive string in local time.

    def test_zulu_offset_and_naive_agree(self):
        base = parse_ts("2026-10-04T12:00:00Z")
        self.assertEqual(parse_ts("2026-10-04T12:00:00.000Z"), base)
        self.assertEqual(parse_ts("2026-10-04T14:00:00+02:00"), base)
        self.assertEqual(parse_ts("2026-10-04T12:00:00"), base)

    def test_unusable_values_are_none(self):
        for bad in (None, "", "   ", "garbage", 17, 1.5, [], {}):
            self.assertIsNone(parse_ts(bad), bad)

    def test_done_prefers_completed_over_a_later_comment_touch(self):
        # Kills: using updatedAt first. A closed card keeps getting touched
        # (comments, relation edits); its worktree is still idle.
        card = {"status": "done", "completedAt": "2026-10-01T00:00:00Z",
                "updatedAt": "2026-10-04T00:00:00Z"}
        self.assertEqual(card_terminal_ts(card),
                         parse_ts("2026-10-01T00:00:00Z"))

    def test_cancelled_uses_cancelled_at(self):
        card = {"status": "cancelled", "cancelledAt": "2026-10-02T00:00:00Z",
                "completedAt": "2026-10-01T00:00:00Z",
                "updatedAt": "2026-10-04T00:00:00Z"}
        self.assertEqual(card_terminal_ts(card),
                         parse_ts("2026-10-02T00:00:00Z"))

    def test_missing_terminal_stamp_falls_back_to_updated(self):
        card = {"status": "done", "completedAt": None,
                "updatedAt": "2026-10-03T00:00:00Z"}
        self.assertEqual(card_terminal_ts(card),
                         parse_ts("2026-10-03T00:00:00Z"))

    def test_no_stamp_at_all_is_none(self):
        self.assertIsNone(card_terminal_ts({"status": "done"}))


class AgeGateTests(unittest.TestCase):
    # Kills: `>` instead of `>=`, oldest-card-governs instead of newest,
    # a missing stamp passing, and ignoring the workspace's own activity.

    NOW = 1_800_000_000.0

    def card(self, hours, status="done"):
        key = "completedAt" if status == "done" else "cancelledAt"
        return {"identifier": "case_8c5fe4cc", "status": status,
                key: iso(hours, self.NOW), "updatedAt": iso(hours, self.NOW)}

    def row(self, hours):
        return {"id": "ws", "lastUsedAt": iso(hours, self.NOW)}

    def test_exactly_the_window_proceeds(self):
        self.assertIsNone(age_gate([self.card(24)], [self.row(24)],
                                   self.NOW, 24))

    def test_just_inside_the_window_keeps(self):
        reason, det = age_gate([self.card(23.9)], [self.row(48)],
                               self.NOW, 24)
        self.assertEqual(reason, "card-too-recent")
        self.assertLess(det["age_hours"], 24)

    def test_newest_card_governs(self):
        # Two cards share the worktree; one closed an hour ago.
        reason, _ = age_gate([self.card(200), self.card(1)],
                             [self.row(300)], self.NOW, 24)
        self.assertEqual(reason, "card-too-recent")

    def test_cancelled_counts_like_done(self):
        self.assertIsNone(age_gate([self.card(30, "cancelled")],
                                   [self.row(30)], self.NOW, 24))

    def test_card_without_any_stamp_keeps(self):
        reason, _ = age_gate([{"identifier": "case_f052904a", "status": "done"}],
                             [self.row(99)], self.NOW, 24)
        self.assertEqual(reason, "no-card-timestamp")

    def test_empty_card_list_keeps(self):
        reason, _ = age_gate([], [self.row(99)], self.NOW, 24)
        self.assertEqual(reason, "no-card-timestamp")

    def test_recently_used_workspace_keeps_even_when_card_is_old(self):
        reason, _ = age_gate([self.card(500)], [self.row(2)], self.NOW, 24)
        self.assertEqual(reason, "workspace-recently-used")

    def test_unreadable_last_used_keeps(self):
        reason, _ = age_gate([self.card(500)],
                             [{"id": "ws", "lastUsedAt": None}], self.NOW, 24)
        self.assertEqual(reason, "no-last-used")

    def test_one_recent_row_among_many_keeps(self):
        reason, _ = age_gate([self.card(500)],
                             [self.row(500), self.row(1)], self.NOW, 24)
        self.assertEqual(reason, "workspace-recently-used")


class RowGateTests(unittest.TestCase):
    # Kills: any() instead of all() over rows; trusting providerType alone;
    # accepting a shared/local_fs row (a project ROOT) as a removable checkout.

    GOOD = {"providerType": "git_worktree", "mode": "isolated_workspace"}

    def test_isolated_git_worktree_passes(self):
        self.assertIsNone(workspace_row_gate([self.GOOD]))

    def test_no_row_keeps(self):
        self.assertEqual(workspace_row_gate([])[0], "no-db-workspace")

    def test_shared_local_fs_root_keeps(self):
        reason, det = workspace_row_gate(
            [{"providerType": "local_fs", "mode": "shared_workspace"}])
        self.assertEqual(reason, "not-an-isolated-worktree-row")
        self.assertEqual(det["providerType"], "local_fs")

    def test_git_worktree_provider_but_shared_mode_keeps(self):
        reason, _ = workspace_row_gate(
            [{"providerType": "git_worktree", "mode": "shared_workspace"}])
        self.assertEqual(reason, "not-an-isolated-worktree-row")

    def test_one_shared_row_among_isolated_rows_keeps(self):
        reason, _ = workspace_row_gate(
            [self.GOOD, {"providerType": "local_fs",
                         "mode": "shared_workspace"}])
        self.assertEqual(reason, "not-an-isolated-worktree-row")

    def test_adapter_managed_keeps(self):
        reason, _ = workspace_row_gate(
            [{"providerType": "local_fs", "mode": "adapter_managed"}])
        self.assertEqual(reason, "not-an-isolated-worktree-row")


class DirtyGateTests(unittest.TestCase):
    # Kills: reading a git failure as clean; treating "nothing printed on a
    # failed command" as clean.

    def test_clean(self):
        self.assertIsNone(dirty_gate(0, ""))
        self.assertIsNone(dirty_gate(0, "\n  \n"))

    def test_git_failure_is_not_clean(self):
        self.assertEqual(dirty_gate(128, "")[0], "git-status-error")
        self.assertEqual(dirty_gate(1, "")[0], "git-status-error")

    def test_modified_and_untracked_are_dirty(self):
        reason, det = dirty_gate(0, " M a.txt\n?? new.txt\n")
        self.assertEqual(reason, "dirty")
        self.assertEqual(det["entries"], 2)
        self.assertEqual(det["sample"], ["a.txt", "new.txt"])

    def test_staged_is_dirty(self):
        self.assertEqual(dirty_gate(0, "A  staged.txt\n")[0], "dirty")


class OperationGateTests(unittest.TestCase):
    # Kills: ignoring a half-finished rebase/merge, whose state lives only in
    # the per-worktree git dir that removal deletes.

    def test_each_marker_keeps(self):
        for marker in tool.IN_PROGRESS_MARKERS:
            hit = operation_gate("/g", exists=lambda p, m=marker:
                                 p == "/g/" + m)
            self.assertEqual(hit[0], "operation-in-progress", marker)
            self.assertEqual(hit[1]["marker"], marker)

    def test_none_proceeds(self):
        self.assertIsNone(operation_gate("/g", exists=lambda p: False))

    def test_markers_cover_the_big_four(self):
        for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                     "CHERRY_PICK_HEAD"):
            self.assertIn(name, tool.IN_PROGRESS_MARKERS)


class RemoveArgvPinTests(unittest.TestCase):
    # PINNED. Git refuses to remove a modified/untracked/locked worktree
    # unless forced. If --force ever appears, every race the pre-checks miss
    # becomes data loss. This is the only place the argv is built.

    def test_never_force(self):
        for bare in (False, True):
            argv = remove_argv("/r", "/w", bare)
            self.assertNotIn("--force", argv)
            self.assertNotIn("-f", argv)
            self.assertNotIn("-ff", argv)
            self.assertEqual(argv[-2:], ["--", "/w"])
            self.assertIn("worktree", argv)
            self.assertIn("remove", argv)

    def test_source_has_no_force_literal_near_remove(self):
        with open(os.path.join(HERE, "terminal_worktree_prune.py")) as handle:
            code = [line for line in handle.read().splitlines()
                    if not line.lstrip().startswith(("#", '"', "-", "git "))]
        for line in code:
            self.assertNotIn('"--force"', line)
            self.assertNotIn('"-f"', line)

    def test_bare_uses_git_dir(self):
        self.assertEqual(remove_argv("/r.git", "/w", True)[:3],
                         ["git", "--git-dir", "/r.git"])

    def test_normal_uses_dash_c(self):
        self.assertEqual(remove_argv("/r", "/w")[:3], ["git", "-C", "/r"])


class KillSwitchTests(unittest.TestCase):
    # Kills: only honoring the env var, or truthy-matching "0"/"false".

    def test_env_truthy_values_trip(self):
        for val in ("1", "true", "TRUE", " yes ", "on"):
            self.assertTrue(kill_switch({"WORKTREE_PRUNE_DISABLED": val},
                                        "/r", lambda p: False)[0], val)

    def test_env_falsy_values_do_not_trip(self):
        for val in ("", "0", "false", "off", "no"):
            self.assertFalse(kill_switch({"WORKTREE_PRUNE_DISABLED": val},
                                         "/r", lambda p: False)[0], val)
        self.assertFalse(kill_switch({}, "/r", lambda p: False)[0])

    def test_marker_files_trip(self):
        for name in (tool.KILL_FILE_NAME, tool.CUTOVER_FILE_NAME):
            tripped, why = kill_switch({}, "/r",
                                       lambda p, n=name: p == "/r/" + n)
            self.assertTrue(tripped, name)
            self.assertIn(name, why)


class OrderAndArgvTests(unittest.TestCase):
    def test_largest_first_then_path(self):
        rows = [{"w": "/b", "bytes": 5}, {"w": "/a", "bytes": 5},
                {"w": "/c", "bytes": 9}, {"w": "/d"}]
        self.assertEqual([r["w"] for r in order_for_removal(rows)],
                         ["/c", "/a", "/b", "/d"])

    def test_defaults_are_dry_and_bounded(self):
        opts = parse_argv([])
        self.assertFalse(opts.apply)
        self.assertEqual(opts.min_age_hours, 24)
        self.assertEqual(opts.max_remove, tool.MAX_REMOVE_DEFAULT)

    def test_apply_is_opt_in(self):
        self.assertTrue(parse_argv(["--apply"]).apply)

    def test_negative_bounds_rejected(self):
        for bad in (["--max-remove", "-1"], ["--min-age-hours", "-2"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_argv(bad)

    def test_removable_result_needs_clean_branch_state(self):
        base = {"reason": None, "git": {"branch_unpushed": 0,
                                        "branch_stash": 0}}
        self.assertTrue(removable_result(base))
        self.assertFalse(removable_result(
            {"reason": None, "git": {"branch_unpushed": 1,
                                     "branch_stash": 0}}))
        self.assertFalse(removable_result(
            {"reason": None, "git": {"branch_unpushed": 0,
                                     "branch_stash": 2}}))
        self.assertFalse(removable_result({"reason": "dirty", "git": None}))


# ---------------------------------------------------------------------------
class EvaluateWithRealGitTests(unittest.TestCase):

    def setUp(self):
        self.fx = RepoFixture()

    def tearDown(self):
        self.fx.close()

    def test_idle_terminal_worktree_is_a_candidate(self):
        path = self.fx.add("case_8c5fe4cc")
        res = evaluate(self.fx, path, "case_8c5fe4cc")
        self.assertIsNone(res["reason"], res)
        self.assertEqual(res["prune"]["main_root"],
                         os.path.realpath(self.fx.main))
        self.assertFalse(res["prune"]["bare"])

    def test_ignored_build_output_is_not_dirty(self):
        # Kills: counting ignored files as dirt. target/ is the point.
        path = self.fx.add("case_d02d0252", target_mb=1)
        res = evaluate(self.fx, path, "case_d02d0252")
        self.assertIsNone(res["reason"], res)

    def test_open_card_keeps(self):
        path = self.fx.add("case_a6dbab15")
        res = evaluate(self.fx, path, "case_a6dbab15",
                       issue=issue_row("case_a6dbab15", status="in_progress"))
        self.assertEqual(res["reason"], "open-card")

    def test_fresh_card_keeps(self):
        path = self.fx.add("case_4ac9f045")
        res = evaluate(self.fx, path, "case_4ac9f045",
                       issue=issue_row("case_4ac9f045", completedAt=iso(2),
                                       updatedAt=iso(2)))
        self.assertEqual(res["reason"], "card-too-recent")

    def test_recently_used_workspace_keeps(self):
        path = self.fx.add("case_0dd2d987")
        res = evaluate(self.fx, path, "case_0dd2d987",
                       row=ws_row("case_0dd2d987", path, lastUsedAt=iso(1)))
        self.assertEqual(res["reason"], "workspace-recently-used")

    def test_shared_project_root_row_keeps(self):
        path = self.fx.add("case_15089482")
        res = evaluate(self.fx, path, "case_15089482",
                       row=ws_row("case_15089482", path, providerType="local_fs",
                                  mode="shared_workspace"))
        self.assertEqual(res["reason"], "not-an-isolated-worktree-row")

    def test_modified_tracked_file_is_dirty(self):
        path = self.fx.add("case_4ee01500")
        with open(os.path.join(path, "Cargo.toml"), "a") as handle:
            handle.write("# local edit\n")
        make_old(path)
        res = evaluate(self.fx, path, "case_4ee01500")
        self.assertEqual(res["reason"], "dirty")
        self.assertEqual(res["det"]["entries"], 1)

    def test_untracked_file_is_dirty(self):
        # Kills: `git status -uno`, which hides exactly the unsaved scratch
        # work this tool must never delete.
        path = self.fx.add("case_1ef046c6")
        with open(os.path.join(path, "notes.md"), "w") as handle:
            handle.write("not committed\n")
        make_old(path)
        res = evaluate(self.fx, path, "case_1ef046c6")
        self.assertEqual(res["reason"], "dirty")

    def test_untracked_file_inside_new_directory_is_dirty(self):
        # Kills: collapsed untracked directories hiding nested files.
        path = self.fx.add("case_f052904a")
        os.makedirs(os.path.join(path, "scratch", "deep"))
        with open(os.path.join(path, "scratch", "deep", "f"), "w") as handle:
            handle.write("x\n")
        make_old(path)
        res = evaluate(self.fx, path, "case_f052904a")
        self.assertEqual(res["reason"], "dirty")

    def test_unpushed_commit_keeps(self):
        path = self.fx.add("case_38d056f3")
        git("config", "user.email", "t@t", cwd=path)
        git("config", "user.name", "t", cwd=path)
        with open(os.path.join(path, "new.txt"), "w") as handle:
            handle.write("work\n")
        git("add", "new.txt", cwd=path)
        git("commit", "-qm", "unpushed work", cwd=path)
        make_old(path)
        res = evaluate(self.fx, path, "case_38d056f3")
        self.assertEqual(res["reason"], "unpushed-head")

    def test_pushed_commit_is_a_candidate(self):
        path = self.fx.add("case_5ab07325")
        with open(os.path.join(path, "new.txt"), "w") as handle:
            handle.write("work\n")
        git("add", "new.txt", cwd=path)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
            "pushed", cwd=path)
        git("push", "-q", "origin", "HEAD:refs/heads/case_5ab07325", cwd=path)
        make_old(path)
        res = evaluate(self.fx, path, "case_5ab07325")
        self.assertIsNone(res["reason"], res)

    def test_stash_naming_the_branch_keeps(self):
        # Kills: dropping the branch-stash rule; the tree is CLEAN after a
        # stash, so only the stash list reveals the parked work.
        path = self.fx.add("case_de259302")
        with open(os.path.join(path, "Cargo.toml"), "a") as handle:
            handle.write("# parked\n")
        git("-c", "user.email=t@t", "-c", "user.name=t", "stash", "push",
            "-q", "-m", "parked", cwd=path)
        make_old(path)
        res = evaluate(self.fx, path, "case_de259302")
        self.assertEqual(res["reason"], "branch-stash")

    def test_rebase_in_progress_keeps(self):
        path = self.fx.add("case_0a5489df")
        git_dir = git("rev-parse", "--git-dir", cwd=path).strip()
        os.makedirs(os.path.join(path, git_dir, "rebase-merge"))
        res = evaluate(self.fx, path, "case_0a5489df")
        self.assertEqual(res["reason"], "operation-in-progress")

    def test_locked_worktree_keeps(self):
        path = self.fx.add("case_2a0ef960")
        git("worktree", "lock", "--reason", "pinned", path, cwd=self.fx.main)
        res = evaluate(self.fx, path, "case_2a0ef960")
        self.assertEqual(res["reason"], "locked")

    def test_project_root_clone_keeps(self):
        # Kills: removing a full clone. A directory .git is a root, never a
        # linked checkout. Even with every board gate satisfied.
        make_old(self.fx.main)
        res = evaluate(self.fx, self.fx.main, "case_5c700a98")
        self.assertEqual(res["reason"], "not-a-linked-worktree")

    def test_git_error_inside_evaluation_keeps(self):
        # Kills: letting one broken checkout abort (or bless) the whole pass.
        path = self.fx.add("case_7aede21a")

        def broken(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "status"]:
                raise OSError("disk fell off")
            return LIVE_RUN(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        ctx = make_ctx([ws_row("case_7aede21a", path)], [issue_row("case_7aede21a")])
        res = evaluate_prune(broken, ctx, path, 30, time.time(), 24)
        self.assertEqual(res["reason"], "eval-error")

    def test_no_db_row_keeps(self):
        path = self.fx.add("case_d468cad2")
        res = evaluate_prune(LIVE_RUN, make_ctx([], []), path, 30,
                             time.time(), 24)
        self.assertEqual(res["reason"], "no-db-workspace")

    def test_linked_gate_on_a_plain_directory(self):
        plain = os.path.join(self.fx.root, "plain")
        os.makedirs(plain)
        gate, info = linked_worktree_gate(LIVE_RUN, plain)
        self.assertEqual(gate[0], "not-a-linked-worktree")
        self.assertIsNone(info)

    def test_second_open_card_on_shared_worktree_keeps(self):
        path = self.fx.add("case_2761dc9b")
        shared = issue_row("case_6febe3f5", status="todo",
                           executionWorkspaceId="ws-case_2761dc9b")
        ctx = make_ctx([ws_row("case_2761dc9b", path)],
                       [issue_row("case_2761dc9b"), shared])
        res = evaluate_prune(LIVE_RUN, ctx, path, 30, time.time(), 24)
        self.assertEqual(res["reason"], "open-card")


# ---------------------------------------------------------------------------
class RecordingRun:
    """Delegates to real git/du/find and records every argv."""

    def __init__(self, before_remove=None):
        self.calls = []
        self.before_remove = before_remove

    def __call__(self, args, cwd=None, timeout=300, stdin_text=None):
        self.calls.append(list(args))
        if (self.before_remove and "worktree" in args and "remove" in args):
            self.before_remove(args)
        return LIVE_RUN(args, cwd=cwd, timeout=timeout, stdin_text=stdin_text)

    def removal_argvs(self):
        return [c for c in self.calls if "worktree" in c and "remove" in c]


class ApplyTests(unittest.TestCase):

    def setUp(self):
        self.fx = RepoFixture()

    def tearDown(self):
        self.fx.close()

    def candidates(self, names, **sizes):
        rows, issues, results = [], [], []
        paths = {}
        for name in names:
            paths[name] = self.fx.add(name, target_mb=sizes.get(name, 0))
            rows.append(ws_row(name, paths[name]))
            issues.append(issue_row(name))
        ctx = make_ctx(rows, issues)
        for name in names:
            res = evaluate_prune(LIVE_RUN, ctx, paths[name], 30,
                                 time.time(), 24)
            self.assertIsNone(res["reason"], res)
            res["bytes"] = tool.reclaim.dir_bytes(LIVE_RUN, paths[name])
            res["cargo_target_bytes"] = tool.cargo_target_bytes(
                LIVE_RUN, paths[name])
            results.append(res)
        statuses = {"id-" + n: "done" for n in names}
        return paths, results, statuses

    def apply(self, results, api, run=None, max_remove=10):
        return apply_removals(run or LIVE_RUN, api, results, max_remove, 30)

    def test_removes_worktree_and_target_but_keeps_branch(self):
        paths, results, statuses = self.candidates(["case_8c5fe4cc"], **{"case_8c5fe4cc": 2})
        self.assertGreater(results[0]["cargo_target_bytes"], 2 * 1024 * 1024
                           - 1)
        report = self.apply(results, FakeApi(statuses))
        self.assertEqual(report["removed"], 1)
        self.assertEqual(report["n_errors"], 0)
        self.assertFalse(os.path.exists(paths["case_8c5fe4cc"]))
        self.assertNotIn(paths["case_8c5fe4cc"], self.fx.listed())
        self.assertTrue(self.fx.branch_exists("case_8c5fe4cc"))
        self.assertEqual(report["branch_missing_after_remove"], [])
        self.assertGreaterEqual(report["removed_cargo_target_bytes"],
                                2 * 1024 * 1024)
        self.assertGreater(report["removed_bytes"],
                           report["removed_cargo_target_bytes"] - 1)

    def test_main_checkout_survives(self):
        paths, results, statuses = self.candidates(["case_d02d0252"])
        self.apply(results, FakeApi(statuses))
        self.assertTrue(os.path.isdir(os.path.join(self.fx.main, ".git")))
        self.assertTrue(os.path.exists(os.path.join(self.fx.main,
                                                    "Cargo.toml")))

    def test_card_reopened_between_scan_and_removal_keeps(self):
        # Kills: removing from the stored scan instead of a fresh re-read.
        paths, results, statuses = self.candidates(["case_a6dbab15"])
        statuses["id-case_a6dbab15"] = "in_progress"
        api = FakeApi(statuses)
        report = self.apply(results, api)
        self.assertEqual(report["removed"], 0)
        self.assertEqual(report["skipped"], {"card-reopened": 1})
        self.assertTrue(os.path.isdir(paths["case_a6dbab15"]))
        self.assertIn("id-case_a6dbab15", api.status_reads)

    def test_every_linked_card_is_reread(self):
        paths, results, statuses = self.candidates(["case_4ac9f045"])
        results[0]["linked_ids"]["case_4e311658"] = "id-case_4e311658"
        statuses["id-case_4e311658"] = "blocked"
        report = self.apply(results, FakeApi(statuses))
        self.assertEqual(report["skipped"], {"card-reopened": 1})
        self.assertTrue(os.path.isdir(paths["case_4ac9f045"]))

    def test_live_run_appearing_keeps(self):
        paths, results, statuses = self.candidates(["case_0dd2d987"])
        api = FakeApi(statuses, live=[{"issueId": "id-case_0dd2d987"}])
        report = self.apply(results, api)
        self.assertEqual(report["skipped"], {"live-run-appeared": 1})
        self.assertTrue(os.path.isdir(paths["case_0dd2d987"]))

    def test_card_reread_error_keeps(self):
        # Kills: swallowing an API error as "still terminal".
        paths, results, statuses = self.candidates(["case_15089482"])
        report = self.apply(results, FakeApi(statuses, status_error=True))
        self.assertEqual(report["skipped"], {"card-reread-failed": 1})
        self.assertTrue(os.path.isdir(paths["case_15089482"]))

    def test_live_runs_unreadable_keeps(self):
        paths, results, statuses = self.candidates(["case_4ee01500"])
        report = self.apply(results, FakeApi(statuses, live_error=True))
        self.assertEqual(report["skipped"], {"live-runs-unreadable": 1})
        self.assertTrue(os.path.isdir(paths["case_4ee01500"]))

    def test_no_linked_card_keeps(self):
        paths, results, statuses = self.candidates(["case_1ef046c6"])
        results[0]["linked_ids"] = {}
        report = self.apply(results, FakeApi(statuses))
        self.assertEqual(report["skipped"], {"no-linked-card": 1})
        self.assertTrue(os.path.isdir(paths["case_1ef046c6"]))

    def test_dirtied_after_scan_keeps(self):
        paths, results, statuses = self.candidates(["case_f052904a"])
        with open(os.path.join(paths["case_f052904a"], "scratch.txt"), "w") as handle:
            handle.write("an agent just wrote this\n")
        make_old(paths["case_f052904a"])
        report = self.apply(results, FakeApi(statuses))
        self.assertEqual(report["skipped"], {"became-dirty": 1})
        self.assertTrue(os.path.isfile(os.path.join(paths["case_f052904a"],
                                                    "scratch.txt")))

    def test_active_after_scan_keeps(self):
        # An ignored log written a minute ago: `git status` is clean, so only
        # the activity gate can see the agent is alive.
        paths, results, statuses = self.candidates(["case_38d056f3"])
        with open(os.path.join(paths["case_38d056f3"], "run.log"), "w") as handle:
            handle.write("still running\n")
        report = self.apply(results, FakeApi(statuses))
        self.assertEqual(report["skipped"], {"became-active": 1})
        self.assertTrue(os.path.isdir(paths["case_38d056f3"]))

    def test_race_after_revalidation_is_refused_by_git_not_forced(self):
        # THE --force mutant killer. The file appears AFTER every pre-check
        # and just before the removal command. Git must refuse; the tool must
        # report an error, keep the directory and the file, and never force.
        paths, results, statuses = self.candidates(["case_5ab07325"])
        target = os.path.join(paths["case_5ab07325"], "late.txt")

        def late_write(_argv):
            with open(target, "w") as handle:
                handle.write("arrived in the race window\n")
        rec = RecordingRun(before_remove=late_write)
        report = self.apply(results, FakeApi(statuses), run=rec)
        self.assertEqual(report["removed"], 0)
        self.assertEqual(report["n_errors"], 1)
        self.assertTrue(os.path.isfile(target))
        self.assertIn(paths["case_5ab07325"], self.fx.listed())
        for argv in rec.removal_argvs():
            self.assertNotIn("--force", argv)
            self.assertNotIn("-f", argv)

    def test_removal_failure_is_reported_loudly(self):
        paths, results, statuses = self.candidates(["case_de259302"])

        def fake(args, cwd=None, timeout=300, stdin_text=None):
            if "worktree" in args and "remove" in args:
                return 1, ""
            return LIVE_RUN(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        report = self.apply(results, FakeApi(statuses), run=fake)
        self.assertEqual(report["removed"], 0)
        self.assertEqual(report["errors"][0]["rc"], 1)
        self.assertTrue(os.path.isdir(paths["case_de259302"]))

    def test_timeout_is_an_error_not_a_crash(self):
        paths, results, statuses = self.candidates(["case_0a5489df"])

        def fake(args, cwd=None, timeout=300, stdin_text=None):
            if "worktree" in args and "remove" in args:
                raise subprocess.TimeoutExpired(args, timeout)
            return LIVE_RUN(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        report = self.apply(results, FakeApi(statuses), run=fake)
        self.assertEqual(report["n_errors"], 1)
        self.assertEqual(report["errors"][0]["rc"], 124)

    def test_max_remove_cap_takes_largest_first(self):
        paths, results, statuses = self.candidates(
            ["case_2a0ef960", "case_5c700a98", "case_7aede21a"],
            **{"case_2a0ef960": 1, "case_5c700a98": 4, "case_7aede21a": 2})
        report = self.apply(results, FakeApi(statuses), max_remove=2)
        self.assertEqual(report["removed"], 2)
        self.assertEqual(report["skipped"], {"over-max-remove": 1})
        self.assertFalse(os.path.exists(paths["case_5c700a98"]))
        self.assertFalse(os.path.exists(paths["case_7aede21a"]))
        self.assertTrue(os.path.isdir(paths["case_2a0ef960"]))

    def test_max_remove_zero_is_observe_only(self):
        paths, results, statuses = self.candidates(["case_d468cad2"])
        rec = RecordingRun()
        report = self.apply(results, FakeApi(statuses), run=rec,
                            max_remove=0)
        self.assertEqual(report["removed"], 0)
        self.assertEqual(rec.removal_argvs(), [])
        self.assertTrue(os.path.isdir(paths["case_d468cad2"]))

    def test_every_removal_is_preceded_by_a_card_reread(self):
        paths, results, statuses = self.candidates(["case_2761dc9b", "case_6febe3f5"])
        api = FakeApi(statuses)
        self.apply(results, api)
        self.assertEqual(sorted(api.status_reads),
                         ["id-case_2761dc9b", "id-case_6febe3f5"])


# ---------------------------------------------------------------------------
class EndToEndTests(unittest.TestCase):

    def setUp(self):
        self.fx = RepoFixture()
        self.out = os.path.join(self.fx.root, "data", "worktree-prune")

    def tearDown(self):
        self.fx.close()

    def board(self, names, **over):
        rows, issues, paths = [], [], {}
        for name in names:
            paths[name] = self.fx.add(name, target_mb=1)
            rows.append(ws_row(name, paths[name]))
            issues.append(issue_row(name))
        statuses = {"id-" + n: "done" for n in names}
        api = FakeApi(statuses, issues=issues, workspaces=rows)
        return paths, api

    def run_main(self, argv, api, env=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(tool.reclaim, "scan_container_processes",
                                  return_value=set()):
            code = tool.main(["--root", self.fx.root, "--out-dir", self.out,
                              "--workers", "2"] + argv, api=api,
                             env=env if env is not None else {})
        return code, json.loads(stdout.getvalue())

    def test_dry_run_removes_nothing_and_reports(self):
        paths, api = self.board(["case_8c5fe4cc", "case_d02d0252"])
        rec = RecordingRun()
        with mock.patch.object(tool.reclaim, "live_run", rec):
            code, summary = self.run_main([], api)
        self.assertEqual(code, 0)
        self.assertEqual(summary["candidates"], 2)
        self.assertGreater(summary["candidate_bytes"], 2 * 1024 * 1024)
        self.assertGreaterEqual(summary["candidate_cargo_target_bytes"],
                                2 * 1024 * 1024)
        self.assertIsNone(summary["apply"])
        self.assertEqual(rec.removal_argvs(), [])
        for path in paths.values():
            self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.isfile(summary["report"]))
        self.assertEqual(len(summary["sample"]), 2)
        self.assertIsNotNone(summary["disk_used_pct_if_all_candidates_removed"])

    def test_apply_removes_and_reports_freed_bytes(self):
        paths, api = self.board(["case_a6dbab15", "case_4ac9f045"])
        code, summary = self.run_main(["--apply", "--max-remove", "5"], api)
        self.assertEqual(code, 0)
        self.assertEqual(summary["apply"]["removed"], 2)
        self.assertGreater(summary["apply"]["removed_bytes"], 0)
        self.assertIn("disk_after", summary)
        for path in paths.values():
            self.assertFalse(os.path.exists(path))
        self.assertTrue(self.fx.branch_exists("case_a6dbab15"))
        self.assertTrue(self.fx.branch_exists("case_4ac9f045"))

    def test_kill_switch_file_forces_dry_run(self):
        paths, api = self.board(["case_0dd2d987"])
        with open(os.path.join(self.fx.root, tool.KILL_FILE_NAME), "w"):
            pass
        code, summary = self.run_main(["--apply"], api)
        self.assertEqual(code, 0)
        self.assertIn(tool.KILL_FILE_NAME, summary["kill_switch"])
        self.assertIsNone(summary["apply"])
        self.assertTrue(os.path.isdir(paths["case_0dd2d987"]))

    def test_kill_switch_env_forces_dry_run(self):
        paths, api = self.board(["case_15089482"])
        code, summary = self.run_main(
            ["--apply"], api, env={"WORKTREE_PRUNE_DISABLED": "1"})
        self.assertIsNone(summary["apply"])
        self.assertEqual(summary["kill_switch"], "env WORKTREE_PRUNE_DISABLED")
        self.assertTrue(os.path.isdir(paths["case_15089482"]))

    def test_cutover_marker_forces_dry_run(self):
        paths, api = self.board(["case_4ee01500"])
        with open(os.path.join(self.fx.root, tool.CUTOVER_FILE_NAME), "w"):
            pass
        _, summary = self.run_main(["--apply"], api)
        self.assertIsNone(summary["apply"])
        self.assertTrue(os.path.isdir(paths["case_4ee01500"]))

    def test_open_card_worktree_survives_a_full_apply(self):
        paths, api = self.board(["case_1ef046c6", "case_f052904a"])
        api.issues[1]["status"] = "in_progress"
        api.statuses["id-case_f052904a"] = "in_progress"
        _, summary = self.run_main(["--apply"], api)
        self.assertEqual(summary["apply"]["removed"], 1)
        self.assertTrue(os.path.isdir(paths["case_f052904a"]))
        self.assertFalse(os.path.exists(paths["case_1ef046c6"]))
        self.assertEqual(summary["kept_by_reason"], {"open-card": 1})

    def test_dirty_worktree_is_reported_not_removed(self):
        paths, api = self.board(["case_38d056f3"])
        with open(os.path.join(paths["case_38d056f3"], "mine.txt"), "w") as handle:
            handle.write("unsaved\n")
        make_old(paths["case_38d056f3"])
        _, summary = self.run_main(["--apply"], api)
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(summary["dirty_reported"], 1)
        self.assertEqual(summary["dirty_sample"][0]["w"], paths["case_38d056f3"])
        self.assertTrue(os.path.isfile(os.path.join(paths["case_38d056f3"],
                                                    "mine.txt")))

    def test_failed_removal_exits_loudly(self):
        paths, api = self.board(["case_5ab07325"])
        real = tool.reclaim.live_run

        def failing(args, cwd=None, timeout=300, stdin_text=None):
            if "worktree" in args and "remove" in args:
                return 1, ""
            return real(args, cwd=cwd, timeout=timeout,
                        stdin_text=stdin_text)
        with mock.patch.object(tool.reclaim, "live_run", failing):
            code, summary = self.run_main(
                ["--apply"], api)
        self.assertEqual(code, 3)
        self.assertEqual(summary["apply"]["n_errors"], 1)

    def test_main_checkout_is_never_enumerated_for_removal(self):
        paths, api = self.board(["case_de259302"])
        _, summary = self.run_main(["--apply"], api)
        self.assertTrue(os.path.isdir(os.path.join(self.fx.main, ".git")))
        self.assertEqual(summary["worktree_dirs_scanned"], 1)

    def test_blind_apply_refuses(self):
        # No API env: no card re-read is possible. Refuse, do not guess.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SafetyRefusal):
                tool.main(["--apply", "--root", self.fx.root,
                           "--out-dir", self.out],
                          run=lambda *a, **k: (0, ""), api=None, env={})

    def test_dry_run_without_api_env_is_an_error_not_an_empty_report(self):
        with self.assertRaises(PruneError):
            tool.main(["--root", self.fx.root, "--out-dir", self.out],
                      run=lambda *a, **k: (0, ""), api=None, env={})

    def test_api_failure_during_scan_is_fatal_not_empty(self):
        class Down(FakeApi):
            def execution_workspaces(self):
                raise OSError("board down")
        with mock.patch.object(tool.reclaim, "scan_container_processes",
                               return_value=set()):
            with self.assertRaises(PruneError):
                tool.main(["--root", self.fx.root, "--out-dir", self.out],
                          api=Down(), env={})


class ReportTests(unittest.TestCase):

    def test_reports_rotate(self):
        with tempfile.TemporaryDirectory() as out:
            for day in range(1, tool.KEEP_REPORTS + 5):
                write_report(out, {"n": day}, "202610%02dT000000Z" % day)
            names = sorted(os.listdir(out))
            self.assertEqual(len(names), tool.KEEP_REPORTS)
            self.assertEqual(names[-1],
                             "prune-202610%02dT000000Z.json"
                             % (tool.KEEP_REPORTS + 4))

    def test_report_is_parseable_and_replace_failure_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as out:
            stamp = "20261009T000000Z"
            path = os.path.join(out, "prune-%s.json" % stamp)
            report = {"summary": {"n": 2}, "results": []}
            self.assertEqual(write_report(out, report, stamp), path)
            with open(path) as handle:
                self.assertEqual(json.load(handle), report)
            previous = b"previous report bytes\\n"
            with open(path, "wb") as handle:
                handle.write(previous)
            with mock.patch.object(tool.os, "replace",
                                   side_effect=OSError(28, "No space")):
                with self.assertRaises(OSError):
                    write_report(out, report, stamp)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), previous)
            self.assertEqual(os.listdir(out), [os.path.basename(path)])

    def test_report_failure_prints_summary_before_error(self):
        with tempfile.TemporaryDirectory() as root:
            out = os.path.join(root, "reports")
            opts = parse_argv(["--root", root, "--out-dir", out])
            stdout = io.StringIO()
            with mock.patch.object(tool.reclaim, "build_context", return_value={}), \
                    mock.patch.object(tool, "enumerate_worktrees", return_value=[]), \
                    mock.patch.object(tool, "disk_snapshot", return_value=None), \
                    mock.patch.object(tool, "summarize", return_value={"apply": None}), \
                    mock.patch.object(tool, "write_json_atomic",
                                      side_effect=OSError(28, "No space")), \
                    contextlib.redirect_stdout(stdout):
                with self.assertRaises(PruneError) as raised:
                    tool.run_tool(opts, lambda *a, **k: (0, ""), None, {},
                                  clock=lambda: 1000)
            summary = json.loads(stdout.getvalue())
            self.assertIn("took_s", summary)
            self.assertIn(out, str(raised.exception))
            self.assertIn("errno=28", str(raised.exception))

    def test_rotation_ignores_foreign_files(self):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "notes.txt"), "w") as handle:
                handle.write("keep me\n")
            for day in range(1, tool.KEEP_REPORTS + 3):
                write_report(out, {}, "202610%02dT000000Z" % day)
            self.assertIn("notes.txt", os.listdir(out))

    def test_summary_projects_disk_use_the_way_df_counts_it(self):
        # Kills: dividing by `total`. df reports used/(used+avail); reserved
        # blocks (100 here) must not dilute the figure operators watch.
        disk = {"total_bytes": 1100, "used_bytes": 900, "free_bytes": 100,
                "used_pct": 90.0}
        cands = [{"w": "/a", "reason": None, "bytes": 50,
                  "cargo_target_bytes": 0, "det": {"cards": ["case_8c5fe4cc"]}}]
        summary = summarize(cands, cands, disk, None, None, 24, 10)
        self.assertEqual(summary["disk_used_pct_if_all_candidates_removed"],
                         85.0)
        self.assertEqual(summary["candidates"], 1)

    def test_disk_snapshot_matches_df_percent(self):
        usage = type("U", (), {"total": 1100, "used": 900, "free": 100})()
        with mock.patch.object(tool.shutil, "disk_usage",
                               return_value=usage):
            snap = tool.disk_snapshot("/x")
        self.assertEqual(snap["used_pct"], 90.0)

    def test_disk_snapshot_unreadable_is_none(self):
        with mock.patch.object(tool.shutil, "disk_usage",
                               side_effect=OSError("gone")):
            self.assertIsNone(tool.disk_snapshot("/x"))

    def test_summary_counts_kept_reasons(self):
        results = [{"w": "/a", "reason": "dirty", "det": {"entries": 1,
                                                          "sample": ["x"]}},
                   {"w": "/b", "reason": "open-card", "det": {}},
                   {"w": "/c", "reason": "open-card", "det": {}}]
        summary = summarize(results, [], None, None, None, 24, 10)
        self.assertEqual(summary["kept_by_reason"],
                         {"dirty": 1, "open-card": 2})
        self.assertEqual(summary["dirty_reported"], 1)
        self.assertIsNone(summary["disk_used_pct_if_all_candidates_removed"])


class HostDelegationTests(unittest.TestCase):
    # Mutant: main() ignores PAPERCLIP_API_ALLOWLIST_CHECKED_BASE, so the
    # container run fails closed on the missing host file (or honors a merely
    # similar base, or drops the delegation on the way to ApiClient). Hermetic
    # via the hatch: HATCH=1 with a non-test base refuses before any file
    # read, so only the exact-match skip reaches the evaluation — which is
    # stubbed, so no network and no filesystem scan happens either way. (The
    # ApiClient side of the delegation is pinned in the reclaim suite.)
    BASE = "https://board.example.com"

    def base_env(self):
        return {"PAPERCLIP_API_URL": self.BASE,
                "PAPERCLIP_API_KEY": "k",
                "PAPERCLIP_COMPANY_ID": "c",
                "PAPERCLIP_API_ALLOWLIST_CHECKED_BASE": self.BASE}

    def drive(self, env):
        with mock.patch.dict(os.environ,
                             {"PAPERCLIP_API_ALLOWLIST_TEST": "1"}):
            with mock.patch.object(tool.reclaim, "ApiClient") as api_cls, \
                    mock.patch.object(tool, "run_tool",
                                      return_value={}) as run_tool:
                rc = tool.main(argv=[], run=None, api=None, env=env)
        return rc, api_cls, run_tool

    def test_exact_match_skips_and_completes(self):
        rc, api_cls, run_tool = self.drive(self.base_env())
        self.assertEqual(rc, 0)
        api_cls.assert_called_once_with(self.BASE, "c", "k",
                                        allowlist_checked_base=self.BASE)
        run_tool.assert_called_once()

    def test_mismatched_delegation_refuses(self):
        env = self.base_env()
        env["PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"] = \
            "https://other.example.com"
        with mock.patch.dict(os.environ,
                             {"PAPERCLIP_API_ALLOWLIST_TEST": "1"}):
            with mock.patch.object(tool.reclaim, "ApiClient") as api_cls:
                with self.assertRaises(PruneError) as raised:
                    tool.main(argv=[], run=None, api=None, env=env)
        self.assertIn("hatch", str(raised.exception))
        api_cls.assert_not_called()

    def test_unset_delegation_refuses(self):
        env = self.base_env()
        del env["PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"]
        with mock.patch.dict(os.environ,
                             {"PAPERCLIP_API_ALLOWLIST_TEST": "1"}):
            with mock.patch.object(tool.reclaim, "ApiClient") as api_cls:
                with self.assertRaises(PruneError) as raised:
                    tool.main(argv=[], run=None, api=None, env=env)
        self.assertIn("hatch", str(raised.exception))
        api_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
