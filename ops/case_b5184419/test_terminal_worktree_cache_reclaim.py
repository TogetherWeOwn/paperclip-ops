#!/usr/bin/env python3
# ===========================================================================
# test_terminal_worktree_cache_reclaim.py — offline tests for
# terminal_worktree_cache_reclaim.py (case_f2c6fc48, parent case_b5184419).
#
# These are written to FAIL a plausible-but-wrong implementation, not to
# confirm the one that exists. A suite that only feeds well-formed input
# validates the author's own dialect and nothing else — so every section below
# is either a shape the live board actually produced on 2026-10-03, or the
# specific shortcut a reimplementation would take.
#
# Deterministic: no network, no credentials, no clock reads. Git-touching
# paths run against throwaway local repos (file:// remotes where a remote is
# needed); the API boundary is a fake exposing issue_status() only.
#
#   python3 ops/case_b5184419/test_terminal_worktree_cache_reclaim.py
# ===========================================================================
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from terminal_worktree_cache_reclaim import (  # noqa: E402
    CARGO_TARGET_NAMES,
    DEP_CACHE_NAMES,
    REGISTRY_DEFAULT,
    REGISTRY_WRITE_TARGET,
    apply_deletes,
    branch_stash_hits,
    cache_dir_ok,
    deletable,
    dir_bytes,
    dir_size,
    evaluate_worktree,
    find_cache_dirs,
    live_run,
    nested_repo,
    parse_argv,
    path_referenced,
    recent_activity,
    registry_apply_decision,
    resolve_mode,
    rev_list_argv,
    rev_list_stdin,
    run_tool,
    subtract_proven_pushed,
    summarize,
    terminal_gate,
    unpushed_after_proof,
    SafetyRefusal,
    ReclaimError,
)
import terminal_worktree_cache_reclaim as reclaim_mod  # noqa: E402


def quiet_refs(api):
    """A board with no live run and no live process: the pre-delete refresh
    reports nothing, so a refusal in a test can only come from the gate under
    test."""
    return {"run_paths": set(), "run_issue_ids": set(), "run_ws_ids": set(),
            "proc_paths": set()}


def quiet_run(args, cwd=None, timeout=300, stdin_text=None):
    """Every git/find call succeeds and prints nothing: ignored, untracked,
    nothing recent, nothing unpushed, no stash."""
    return 0, ""


def scripted_run(**answers):
    """quiet_run, except argv containing a keyed token answers (rc, out).

    Keys are matched against the space-joined argv ("check-ignore",
    "ls-files", "stash list", "symbolic-ref", "find", "log HEAD",
    "ls-remote"); the first key that appears wins.
    """
    def run(args, cwd=None, timeout=300, stdin_text=None):
        joined = " ".join(args)
        for key, answer in answers.items():
            if key.replace("_", " ") in joined:
                return answer
        return 0, ""
    return run


def git(*args, cwd):
    proc = subprocess.run(["git"] + list(args), cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


def card(identifier, status):
    return {"identifier": identifier, "status": status}


def make_old(root, seconds_ago=7200):
    """Backdate every non-.git file: fixtures are born seconds ago, but the
    30-minute recent-activity gate must see them as idle."""
    stamp = __import__("time").time() - seconds_ago
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (stamp, stamp))


class TerminalGateTests(unittest.TestCase):
    # The mutant this section kills: `any(c done) -> proceed` instead of
    # `all(c terminal) -> proceed`. One open card among three linked must
    # keep the worktree, or a reopened parent's cache is deleted mid-run.

    def test_all_terminal_proceeds(self):
        self.assertIsNone(terminal_gate([card("case_8c5fe4cc", "done"),
                                         card("case_d02d0252", "cancelled")]))

    def test_one_open_card_keeps(self):
        reason, det = terminal_gate([card("case_8c5fe4cc", "done"),
                                     card("case_d02d0252", "in_progress")])
        self.assertEqual(reason, "open-card")
        self.assertEqual(det["card"], "case_d02d0252")

    def test_blocked_is_not_terminal(self):
        # Live shape: a parked card sits in blocked for days; blocked is an
        # open state and must not confer eligibility.
        reason, _ = terminal_gate([card("case_8c5fe4cc", "blocked")])
        self.assertEqual(reason, "open-card")

    def test_unknown_card_keeps(self):
        # Live shape: a DB row references an issue id the issues list never
        # returned (deleted/renamed card). None status must not read as done.
        reason, _ = terminal_gate([{"identifier": "case_f052904a", "status": None}])
        self.assertEqual(reason, "card-unknown")

    def test_no_linked_cards_keeps(self):
        for empty in (None, [], ()):
            reason, _ = terminal_gate(empty)
            self.assertEqual(reason, "no-linked-card")


class PathReferenceTests(unittest.TestCase):
    # The mutant this section kills: a bare startswith, which matches the
    # sibling /w/case_de259302 when the live reference is to /w/case_8c5fe4cc.

    def test_exact_and_child_match(self):
        self.assertTrue(path_referenced("/w/case_8c5fe4cc", {"/w/case_8c5fe4cc"}))
        self.assertTrue(path_referenced("/w/case_8c5fe4cc", {"/w/case_8c5fe4cc/node_modules/x"}))

    def test_sibling_prefix_does_not_match(self):
        self.assertFalse(path_referenced("/w/case_8c5fe4cc", {"/w/case_de259302"}))
        self.assertFalse(path_referenced("/w/case_8c5fe4cc", {"/w/case_8c5fe4cc-backup"}))

    def test_empty_set_is_clear(self):
        self.assertFalse(path_referenced("/w/case_8c5fe4cc", set()))


class RevListPinTests(unittest.TestCase):
    # PINNED BUG (case_b5184419 first dry run): `git rev-list HEAD --not
    # --remotes --stdin` inverts the stdin operands and produced negative
    # counts. The fixed form carries exclusions on stdin with no --not.

    def test_argv_has_no_not_no_remotes(self):
        argv = rev_list_argv()
        self.assertIn("HEAD", argv)
        self.assertIn("--stdin", argv)
        self.assertNotIn("--not", argv)
        self.assertNotIn("--remotes", argv)

    def test_stdin_is_caret_lines(self):
        self.assertEqual(rev_list_stdin({"b", "a"}), "^a\n^b\n")

    def test_subtract_is_intersection(self):
        # unpushed {u1,u2}, rev-list(HEAD, ^remote) prints {u2, x}: only u2
        # is still unpushed. The difference (u1) would bless a commit
        # rev-list never saw; the raw output (x) would condemn a commit the
        # unpushed listing never named.
        self.assertEqual(subtract_proven_pushed({"u1", "u2"}, {"u2", "x"}),
                         {"u2"})

    def test_subtract_empty_remote_proves_nothing(self):
        self.assertEqual(subtract_proven_pushed({"u1"}, set()), set())

    def test_subtract_live_shape(self):
        # Live shape (case_b5184419 second pass): 272 locally-unpushed worktrees,
        # 242 fully proven by advertised PR/branch shas.
        unpushed = {"h%d" % i for i in range(5)}
        out = {"h0", "h1", "h2", "h3", "h4"}
        self.assertEqual(subtract_proven_pushed(unpushed, out), unpushed)
        self.assertEqual(subtract_proven_pushed(unpushed, set()), set())


class StashGuardTests(unittest.TestCase):
    # The mutant this section kills: matching the branch name as a substring
    # ("case_8c5fe4cc" matching "WIP on case_de259302: ...") or ignoring stashes entirely.

    def test_names_branch_keeps(self):
        lines = ["stash@{0}: WIP on case_de259302: abc1234 wip",
                 "stash@{1}: On case_4ee01500: something else"]
        self.assertEqual(branch_stash_hits(lines, "case_de259302"), 1)

    def test_substring_branch_does_not_keep(self):
        lines = ["stash@{0}: WIP on case_4e5328d7: abc1234 wip"]
        self.assertEqual(branch_stash_hits(lines, "case_de259302"), 0)

    def test_detached_head_any_stash_keeps(self):
        lines = ["stash@{0}: WIP on whatever: abc1234 wip"]
        self.assertEqual(branch_stash_hits(lines, ""), 1)

    def test_unrelated_stash_does_not_keep(self):
        lines = ["stash@{0}: WIP on case_7054ff5e: abc1234 wip"]
        self.assertEqual(branch_stash_hits(lines, "case_de259302"), 0)

    def test_no_stashes(self):
        self.assertEqual(branch_stash_hits([], "case_de259302"), 0)


class CacheDirTests(unittest.TestCase):
    # The mutants this section kills: deleting a tracked dir (check-ignore
    # skipped), deleting on ls-files error (assumed untracked), deleting a
    # symlink (rm -rf follows it out of the worktree).

    def test_ignored_untracked_dir_ok(self):
        ok, reason = cache_dir_ok(is_symlink=False, check_ignore_rc=0,
                                  ls_files_rc=0, ls_files_out="")
        self.assertEqual((ok, reason), (True, None))

    def test_not_ignored_kept(self):
        ok, reason = cache_dir_ok(is_symlink=False, check_ignore_rc=1,
                                  ls_files_rc=0, ls_files_out="")
        self.assertEqual((ok, reason), (False, "not-ignored"))

    def test_tracked_inside_kept(self):
        ok, reason = cache_dir_ok(is_symlink=False, check_ignore_rc=0,
                                  ls_files_rc=0, ls_files_out="node_modules/a.js\n")
        self.assertEqual((ok, reason), (False, "tracked-inside"))

    def test_ls_files_error_fails_closed(self):
        ok, reason = cache_dir_ok(is_symlink=False, check_ignore_rc=0,
                                  ls_files_rc=128, ls_files_out="")
        self.assertEqual((ok, reason), (False, "ls-files-error"))

    def test_symlink_skipped(self):
        ok, reason = cache_dir_ok(is_symlink=True, check_ignore_rc=0,
                                  ls_files_rc=0, ls_files_out="")
        self.assertEqual((ok, reason), (False, "symlink"))


class LocalRepoTests(unittest.TestCase):
    """End-to-end gate proofs against throwaway local git repos."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.origin = os.path.join(self.tmp.name, "origin")
        os.mkdir(self.origin)
        git("init", "-q", cwd=self.origin)
        git("config", "user.email", "t@t", cwd=self.origin)
        git("config", "user.name", "t", cwd=self.origin)
        git("config", "commit.gpgsign", "false", cwd=self.origin)
        with open(os.path.join(self.origin, "a.txt"), "w") as handle:
            handle.write("a\n")
        git("add", "a.txt", cwd=self.origin)
        git("commit", "-qm", "base", cwd=self.origin)
        self.wt = os.path.join(self.tmp.name, "case_8c5fe4cc")
        git("clone", "-q", self.origin, self.wt, cwd=self.tmp.name)
        git("config", "user.email", "t@t", cwd=self.wt)
        git("config", "user.name", "t", cwd=self.wt)
        git("config", "commit.gpgsign", "false", cwd=self.wt)
        # node_modules ignored, matching the tool's DEP set.
        with open(os.path.join(self.wt, ".gitignore"), "w") as handle:
            handle.write("node_modules\n")
        git("add", ".gitignore", cwd=self.wt)
        git("commit", "-qm", "ignore", cwd=self.wt)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.wt)
        os.mkdir(os.path.join(self.wt, "node_modules"))
        with open(os.path.join(self.wt, "node_modules", "x.js"), "w") as f:
            f.write("x\n")
        make_old(self.wt)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, cards, ws_state="active"):
        ews = [{"id": "ws-1", "status": ws_state, "sourceIssueId": "id-1"}]
        return {
            "ew_by_cwd": {self.wt: ews},
            "issues": {"id-1": {"id": "id-1", "identifier": "case_8c5fe4cc",
                                "status": cards, "executionWorkspaceId": "ws-1"}},
            "by_ident": {"case_8c5fe4cc": {"id": "id-1", "identifier": "case_8c5fe4cc",
                                   "status": cards,
                                   "executionWorkspaceId": "ws-1"}},
            "issues_by_ew": {"ws-1": [{"id": "id-1", "identifier": "case_8c5fe4cc",
                                       "status": cards,
                                       "executionWorkspaceId": "ws-1"}]},
            "run_paths": set(), "run_issue_ids": set(), "run_ws_ids": set(),
            "proc_paths": set(),
        }

    def test_pushed_head_with_pr_ref_proves_pushed(self):
        # Live shape: the branch ref was auto-deleted after squash-merge, but
        # origin still advertises the commit under refs/pull/1/head.
        git("checkout", "-qb", "feature", cwd=self.wt)
        with open(os.path.join(self.wt, "b.txt"), "w") as handle:
            handle.write("b\n")
        git("add", "b.txt", cwd=self.wt)
        git("commit", "-qm", "feat", cwd=self.wt)
        head = git("rev-parse", "HEAD", cwd=self.wt).strip()
        # Land the commit in the origin repo first (a fetchable object),
        # then advertise it under a PR head + branch ref like a merged-PR
        # squash whose head branch was auto-deleted.
        git("fetch", "-q", self.wt,
            "HEAD:refs/heads/pr-proven", cwd=self.origin)
        git("update-ref", "refs/pull/1/head", head, cwd=self.origin)
        # Fetch the PR head into a NON-remotes ref: this is the live shape
        # (auto-deleted head branch, commit advertised only under
        # refs/pull/N/head), where `git log HEAD --not --remotes` still
        # calls HEAD unpushed. Fetching into refs/remotes/* would create a
        # remote-tracking ref and over-prove the fixture.
        git("fetch", "-q", "origin",
            "+refs/pull/1/head:refs/pr/1/head", cwd=self.wt)
        remaining, proven = unpushed_after_proof(live_run, self.wt)
        self.assertEqual(remaining, 0)
        self.assertGreaterEqual(proven, 1)

    def test_genuinely_unpushed_head_is_kept(self):
        git("checkout", "-qb", "local-only", cwd=self.wt)
        with open(os.path.join(self.wt, "c.txt"), "w") as handle:
            handle.write("c\n")
        git("add", "c.txt", cwd=self.wt)
        git("commit", "-qm", "local", cwd=self.wt)
        remaining, proven = unpushed_after_proof(live_run, self.wt)
        self.assertGreaterEqual(remaining, 1)
        self.assertEqual(proven, 0)

    def test_open_card_keeps_whole_worktree(self):
        res = evaluate_worktree(live_run, self.ctx("in_progress"), self.wt, 30)
        self.assertEqual(res["reason"], "open-card")
        self.assertEqual(res["caches"], [])

    def test_done_card_finds_ignored_cache(self):
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)
        self.assertEqual(len(res["caches"]), 1)
        self.assertTrue(res["caches"][0]["p"].endswith("node_modules"))
        self.assertTrue(deletable(res))

    def test_nested_repo_in_cache_is_not_offered(self):
        os.makedirs(os.path.join(self.wt, "node_modules", "pkg", ".git"))
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["caches"], [], res)
        self.assertEqual(res["nested_repo"], ["node_modules"], res)

    def test_unverifiable_cache_is_counted_not_passed(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[0] == "find" and "HEAD" in args:
                return 1, ""
            return live_run(args, cwd=cwd, timeout=timeout, stdin_text=stdin_text)
        res = evaluate_worktree(run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["caches"], [], res)
        self.assertEqual(res["ls-error"], ["node_modules"], res)
        self.assertEqual(summarize([res])["n_errors"], 1)

    def test_find_error_is_refused_and_counted(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[0] == "find" and "-mmin" in args:
                return 1, ""
            return live_run(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        res = evaluate_worktree(run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["reason"], "find-error", res)
        self.assertEqual(summarize([res])["n_errors"], 1)

    def test_bare_repo_in_cache_is_not_offered(self):
        mirror = os.path.join(self.wt, "node_modules", "mirror")
        for d in ("objects", "refs"):
            os.makedirs(os.path.join(mirror, d))
        with open(os.path.join(mirror, "HEAD"), "w") as handle:
            handle.write("ref: refs/heads/main\n")
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["caches"], [], res)
        self.assertEqual(res["nested_repo"], ["node_modules"], res)

    def test_objects_dir_without_head_is_not_a_repo(self):
        os.makedirs(os.path.join(self.wt, "node_modules", "pkg", "lib",
                                 "objects"))
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertEqual(len(res["caches"]), 1, res)
        self.assertNotIn("nested_repo", res)

    def test_unexpected_failure_is_an_eval_error(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "log"]:
                raise subprocess.TimeoutExpired(args, timeout)
            return live_run(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        res = evaluate_worktree(run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["reason"], "eval-error", res)
        self.assertEqual(summarize([res])["n_errors"], 1)

    def test_check_ignore_fatal_is_unverifiable(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "check-ignore"]:
                return 128, ""
            return live_run(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        res = evaluate_worktree(run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["ls-error"], ["node_modules"], res)
        self.assertEqual(res["caches"], [], res)

    def test_symbolic_ref_error_is_git_error(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "symbolic-ref"]:
                return 128, ""
            return live_run(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        res = evaluate_worktree(run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["reason"], "git-error", res)

    def test_second_stash_read_error_is_git_error(self):
        calls = []

        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "stash"]:
                calls.append(1)
                if len(calls) == 1:
                    return 0, "stash@{0}: WIP on main: x\n"
                return 1, ""
            return live_run(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        res = evaluate_worktree(run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["reason"], "git-error", res)

    def test_unexpected_checks_before_an_eval_error_are_kept(self):
        import unittest.mock as mock
        a = os.path.join(self.wt, "node_modules")
        b = os.path.join(self.wt, ".venv")

        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "check-ignore"]:
                return (128, "") if args[-1] == "node_modules" else (0, "")
            if args[:2] == ["git", "ls-files"]:
                raise subprocess.TimeoutExpired(args, timeout)
            return live_run(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        with mock.patch.object(reclaim_mod, "find_cache_dirs",
                               return_value=[a, b]):
            res = evaluate_worktree(run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["reason"], "eval-error", res)
        self.assertEqual(res["ls-error"], ["node_modules"], res)
        self.assertEqual(res["caches"], [], res)

    def test_branch_stash_keeps(self):
        git("checkout", "-qb", "stashy", cwd=self.wt)
        with open(os.path.join(self.wt, "d.txt"), "w") as handle:
            handle.write("d\n")
        # -u: the file is untracked, and a bare stash ignores untracked
        # files ("No local changes to save", empty stash list). The tool
        # reads `git stash list`, so the fixture must create a real entry.
        git("stash", "-q", "-u", cwd=self.wt)
        make_old(self.wt)
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)
        self.assertEqual(res["git"]["branch_stash"], 1)
        self.assertFalse(deletable(res))

    def test_tracked_dep_name_kept(self):
        # A node_modules dir that is TRACKED (force-added) must not delete
        # even though the name is in the DEP set. Measured git semantics:
        # once anything under the dir is tracked, `git check-ignore` on the
        # dir reports NOT ignored (rc 1) — so it lands in not_ignored, and
        # either way it must never reach `caches`.
        git("add", "-f", "node_modules/x.js", cwd=self.wt)
        git("commit", "-qm", "track cache (test only)", cwd=self.wt)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.wt)
        make_old(self.wt)
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["caches"], [])
        kept = res.get("not_ignored", []) + res.get("tracked_inside", [])
        self.assertIn("node_modules", kept)

    def test_symlink_cache_skipped(self):
        real = os.path.join(self.tmp.name, "real_nm")
        os.mkdir(real)
        target = os.path.join(self.wt, "node_modules")
        subprocess.run(["rm", "-rf", "--", target], check=True)
        os.symlink(real, target)
        make_old(self.wt)
        make_old(real)
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["caches"], [])
        self.assertIn("node_modules", res.get("skipped_symlink", []))

    # --- gate WIRING inside evaluate_worktree -------------------------------
    # The pure helpers are covered above; these prove each gate is actually
    # consulted and refuses. Mutant for each: delete that gate's `if` (or its
    # conjunct) in evaluate_worktree — the otherwise-eligible done-card
    # worktree below would then reach `reason is None` and the cache would be
    # offered for deletion under a running agent.

    def kept(self, ctx, reason, worktree=None):
        res = evaluate_worktree(live_run, ctx, worktree or self.wt, 30)
        self.assertEqual(res["reason"], reason, res)
        self.assertEqual(res["caches"], [])
        self.assertFalse(deletable(res))
        return res

    def test_baseline_done_worktree_is_eligible(self):
        # Positive control: every kept-case below differs from this by ONE
        # input, so a refusal can only come from that gate.
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)
        self.assertTrue(deletable(res))

    def test_live_run_path_keeps(self):
        ctx = self.ctx("done")
        ctx["run_paths"] = {os.path.join(self.wt, "src")}
        self.kept(ctx, "live-run-ref")

    def test_live_run_issue_id_keeps(self):
        ctx = self.ctx("done")
        ctx["run_issue_ids"] = {"id-1"}
        self.kept(ctx, "live-run-ref")

    def test_live_run_workspace_id_keeps(self):
        ctx = self.ctx("done")
        ctx["run_ws_ids"] = {"ws-1"}
        self.kept(ctx, "live-run-ref")

    def test_live_process_keeps(self):
        ctx = self.ctx("done")
        ctx["proc_paths"] = {os.path.join(self.wt, "node_modules", ".bin")}
        self.kept(ctx, "live-process")

    def test_odd_workspace_state_keeps(self):
        self.kept(self.ctx("done", ws_state="provisioning"), "ws-state-odd")

    def test_recent_activity_keeps(self):
        # A source file touched just now: the 30-minute idle gate must fire.
        with open(os.path.join(self.wt, "fresh.txt"), "w") as handle:
            handle.write("fresh\n")
        self.kept(self.ctx("done"), "recent-activity")

    def test_recent_activity_inside_dep_cache_is_ignored(self):
        # The gate prunes dep-cache dirs: an install writing into
        # node_modules must not read as agent activity, or no cache ever
        # becomes reclaimable.
        with open(os.path.join(self.wt, "node_modules", "new.js"), "w") as f:
            f.write("n\n")
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)

    def test_no_db_workspace_keeps(self):
        ctx = self.ctx("done")
        ctx["ew_by_cwd"] = {}
        self.kept(ctx, "no-db-workspace")

    def test_non_git_dir_keeps(self):
        plain = os.path.join(self.tmp.name, "case_d02d0252")
        os.mkdir(plain)
        os.mkdir(os.path.join(plain, "node_modules"))
        self.kept(self.ctx("done"), "not-a-git-worktree", worktree=plain)

    def test_evaluate_output_feeds_apply_reread(self):
        # End-to-end handoff: the `linked`/`linked_ids` evaluate_worktree
        # emits are exactly what apply_deletes re-reads. Mutant: `linked`
        # never populated -> apply iterates nothing and deletes blind.
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)
        self.assertEqual(res["linked"], ["case_8c5fe4cc"])
        self.assertEqual(res["linked_ids"], {"case_8c5fe4cc": "id-1"})
        cache = os.path.join(self.wt, "node_modules")
        api = ApplyRereadTests.FakeApi({"id-1": "in_progress"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(api.calls, ["id-1"])
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertEqual(report["skipped_worktrees_card_reopened"], 1)
        self.assertTrue(os.path.isdir(cache))
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(api.calls, ["id-1"])
        self.assertEqual(report["deleted_dirs"], 1)
        self.assertFalse(os.path.exists(cache))

    # --- case_cfed961c: the same repo changes AFTER the scan, BEFORE the delete -
    # Real git, real files: the scan offers the worktree, then one thing
    # changes, and apply must keep it. Control: with no change it deletes.

    def scan_then_apply(self):
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)
        self.assertTrue(deletable(res), res)
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        return res, apply_deletes(live_run, api, [res], refresh=quiet_refs)

    def test_recheck_control_unchanged_worktree_deletes(self):
        res, report = self.scan_then_apply()
        self.assertEqual(report["deleted_dirs"], 1, report)

    def test_commit_after_scan_keeps_cache(self):
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertTrue(deletable(res), res)
        git("checkout", "-qb", "late-work", cwd=self.wt)
        with open(os.path.join(self.wt, "late.txt"), "w") as handle:
            handle.write("late\n")
        git("add", "late.txt", cwd=self.wt)
        git("commit", "-qm", "late local commit", cwd=self.wt)
        make_old(self.wt)
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertEqual(report["skipped_worktrees_by_reason"],
                         {"unpushed-head": 1})
        self.assertTrue(os.path.isdir(os.path.join(self.wt, "node_modules")))

    def test_stash_after_scan_keeps_cache(self):
        git("checkout", "-qb", "stashy", cwd=self.wt)
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertTrue(deletable(res), res)
        with open(os.path.join(self.wt, "wip.txt"), "w") as handle:
            handle.write("wip\n")
        git("stash", "-q", "-u", cwd=self.wt)
        make_old(self.wt)
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertEqual(report["skipped_worktrees_by_reason"],
                         {"branch-stash": 1})
        self.assertTrue(os.path.isdir(os.path.join(self.wt, "node_modules")))

    def test_write_after_scan_keeps_cache(self):
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertTrue(deletable(res), res)
        with open(os.path.join(self.wt, "now.txt"), "w") as handle:
            handle.write("now\n")
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertEqual(report["skipped_worktrees_by_reason"],
                         {"recent-activity": 1})

    def test_cache_force_staged_after_scan_keeps_cache(self):
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertTrue(deletable(res), res)
        git("add", "-f", "node_modules/x.js", cwd=self.wt)
        make_old(self.wt)
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertTrue(os.path.isdir(os.path.join(self.wt, "node_modules")))

    def test_ignore_rule_dropped_after_scan_keeps_cache(self):
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertTrue(deletable(res), res)
        with open(os.path.join(self.wt, ".gitignore"), "w") as handle:
            handle.write("# node_modules no longer ignored\n")
        make_old(self.wt)
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertEqual(report["skipped_dirs_by_reason"], {"not-ignored": 1})
        self.assertTrue(os.path.isdir(os.path.join(self.wt, "node_modules")))

    # --- case_160a06b2: Cargo target is reported, never offered -----------------

    def add_target(self, ignored=True, nested_cache=False):
        if ignored:
            with open(os.path.join(self.wt, ".gitignore"), "a") as handle:
                handle.write("target\n")
            git("add", ".gitignore", cwd=self.wt)
            git("commit", "-qm", "ignore target", cwd=self.wt)
            git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.wt)
        os.makedirs(os.path.join(self.wt, "target", "debug"))
        with open(os.path.join(self.wt, "target", "debug", "bin"), "w") as f:
            f.write("elf\n")
        if nested_cache:
            os.makedirs(os.path.join(self.wt, "target", "doc", "node_modules"))
        make_old(self.wt)

    def test_ignored_target_is_excluded_not_offered(self):
        self.add_target()
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)
        self.assertEqual([c["name"] for c in res["caches"]], ["node_modules"])
        self.assertEqual(
            [os.path.relpath(c["p"], self.wt)
             for c in res["cargo_target_excluded"]], ["target"])

    def test_unignored_target_is_still_excluded_not_flagged(self):
        # No .gitignore line for target: it must land in the Cargo bucket,
        # not in not_ignored/ls-error where a later change could revive it.
        self.add_target(ignored=False)
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertEqual([c["name"] for c in res["caches"]], ["node_modules"])
        self.assertNotIn("target", res.get("not_ignored", []))
        self.assertEqual(len(res["cargo_target_excluded"]), 1)

    def test_scan_does_not_descend_into_target(self):
        # A node_modules buried in target/ is part of the Cargo tree: neither
        # offered nor double-counted.
        self.add_target(nested_cache=True)
        names = [os.path.relpath(p, self.wt)
                 for p in find_cache_dirs(live_run, self.wt)]
        self.assertEqual(sorted(names), ["node_modules", "target"])

    def test_writes_inside_target_do_not_read_as_activity(self):
        # A build writing target/ is not agent activity (same as node_modules);
        # otherwise no done-card worktree with a target dir ever qualifies.
        self.add_target()
        with open(os.path.join(self.wt, "target", "debug", "new"), "w") as f:
            f.write("n\n")
        self.assertFalse(recent_activity(live_run, self.wt, 30))

    def test_apply_removes_cache_and_leaves_target(self):
        self.add_target()
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        api = ApplyRereadTests.FakeApi({"id-1": "done"})
        report = apply_deletes(live_run, api, [res], refresh=quiet_refs)
        self.assertEqual(report["deleted_dirs"], 1, report)
        self.assertFalse(os.path.exists(os.path.join(self.wt, "node_modules")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.wt, "target", "debug", "bin")))

    # --- review additions (case_1d192771): pin gates whose mutants survived -----

    def commit_proven_by_pr_ref_only(self):
        # Same shape as the PR-ref test: HEAD is advertised only under
        # refs/pull/1/head, so `--not --remotes` calls it unpushed and only
        # the ls-remote / cat-file / rev-list proof can clear it.
        git("checkout", "-qb", "feature", cwd=self.wt)
        with open(os.path.join(self.wt, "b.txt"), "w") as handle:
            handle.write("b\n")
        git("add", "b.txt", cwd=self.wt)
        git("commit", "-qm", "feat", cwd=self.wt)
        head = git("rev-parse", "HEAD", cwd=self.wt).strip()
        git("fetch", "-q", self.wt, "HEAD:refs/heads/pr-proven", cwd=self.origin)
        git("update-ref", "refs/pull/1/head", head, cwd=self.origin)
        git("fetch", "-q", "origin", "+refs/pull/1/head:refs/pr/1/head",
            cwd=self.wt)

    def failing_git(self, *prefix):
        # live_run, except argv starting with `prefix` exits 128 with no output.
        def run(args, cwd=None, timeout=300, stdin_text=None):
            if tuple(args[:len(prefix)]) == prefix:
                return 128, ""
            return live_run(args, cwd=cwd, timeout=timeout,
                            stdin_text=stdin_text)
        return run

    def test_untracked_not_ignored_dep_dir_kept(self):
        # Data-loss gate. A dep-named dir that no .gitignore covers can hold
        # uncommitted work; `git check-ignore` is the only thing separating it
        # from a regenerable cache (ls-files is empty for untracked files).
        # Mutant: drop the check-ignore `continue` in evaluate_worktree.
        os.mkdir(os.path.join(self.wt, "vendor"))
        with open(os.path.join(self.wt, "vendor", "patch.go"), "w") as handle:
            handle.write("uncommitted work\n")
        make_old(self.wt)
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertIsNone(res["reason"], res)
        self.assertEqual(res.get("not_ignored"), ["vendor"])
        # Control: the ignored sibling in the same worktree is still offered.
        self.assertEqual([c["name"] for c in res["caches"]], ["node_modules"])

    def test_proof_control_proves_when_every_git_call_works(self):
        # Positive control for the fail-closed test below: with no injected
        # fault the same fixture IS proven pushed, so a nonzero remainder
        # there can only come from the injected error.
        self.commit_proven_by_pr_ref_only()
        self.assertEqual(unpushed_after_proof(live_run, self.wt)[0], 0)

    def test_proof_errors_fail_closed(self):
        # Docstring contract: a git/ls-remote error in the pushed-HEAD proof
        # is an error, never a subtraction. Mutants: a proof error read as
        # "proven", or as an ordinary unpushed count.
        self.commit_proven_by_pr_ref_only()
        for prefix in (("git", "ls-remote"), ("git", "cat-file"),
                       ("git", "rev-list")):
            with self.subTest(failing=prefix[1]):
                with self.assertRaises(ReclaimError):
                    unpushed_after_proof(self.failing_git(*prefix), self.wt)

    def test_empty_advertised_set_keeps_local_commit(self):
        # Distinct from the error path: ls-remote SUCCEEDS but origin
        # advertises no usable ref, so fetch_remote_shas returns an empty set.
        # Docstring contract: no subtraction, every locally-unpushed commit
        # still counts. Mutant: `empty set -> proven` (None stays handled).
        git("checkout", "-qb", "local-only", cwd=self.wt)
        with open(os.path.join(self.wt, "c.txt"), "w") as handle:
            handle.write("c\n")
        git("add", "c.txt", cwd=self.wt)
        git("commit", "-qm", "local", cwd=self.wt)
        refs = git("for-each-ref", "--format=%(refname)", "refs/heads",
                   "refs/pull", cwd=self.origin).split()
        self.assertTrue(refs)  # control: origin did advertise something
        for ref in refs:
            git("update-ref", "-d", ref, cwd=self.origin)
        self.assertEqual(
            git("ls-remote", "origin", "refs/pull/*/head", "refs/heads/*",
                cwd=self.wt).strip(), "")
        remaining, proven = unpushed_after_proof(live_run, self.wt)
        self.assertGreaterEqual(remaining, 1)
        self.assertEqual(proven, 0)

    def test_unreachable_origin_fails_closed(self):
        self.commit_proven_by_pr_ref_only()
        git("remote", "set-url", "origin",
            os.path.join(self.tmp.name, "gone"), cwd=self.wt)
        make_old(self.wt)
        res = evaluate_worktree(live_run, self.ctx("done"), self.wt, 30)
        self.assertEqual(res["reason"], "remote-unreadable", res)
        self.assertFalse(deletable(res))
        self.assertEqual(summarize([res])["n_errors"], 0)

    def test_head_listing_error_is_loud(self):
        with self.assertRaises(ReclaimError):
            unpushed_after_proof(self.failing_git("git", "log", "HEAD"),
                                 self.wt)


class SummarizeTests(unittest.TestCase):
    # The mutant this section kills: delete-bucket == report-bucket, i.e. the
    # strict card rule (informational) leaking into the branch-scoped apply
    # set, or vice versa.

    def base_result(self, **over):
        res = {"w": "/w/case_8c5fe4cc", "reason": None, "det": {},
               "caches": [{"p": "/w/case_8c5fe4cc/node_modules",
                           "name": "node_modules", "bytes": 100}],
               "git": {"repo_unpushed": 0, "repo_stash": 0,
                       "branch_unpushed": 0, "branch": "b", "branch_stash": 0,
                       "pr_proven": 0}}
        res.update(over)
        return res

    def test_clean_worktree_in_both_buckets(self):
        out = summarize([self.base_result()])
        self.assertEqual(out["strict_card_rule"]["worktrees"], 1)
        self.assertEqual(out["branch_scoped_rule"]["worktrees"], 1)

    def test_repo_wide_unpushed_leaves_branch_bucket(self):
        # Live shape: repo-wide --branches --not --remotes noise must not
        # block the branch-scoped delete (that is the whole case_b5184419 fix).
        res = self.base_result()
        res["git"] = dict(res["git"], repo_unpushed=3)
        out = summarize([res])
        self.assertEqual(out["strict_card_rule"]["worktrees"], 0)
        self.assertEqual(out["branch_scoped_rule"]["worktrees"], 1)

    def test_branch_unpushed_leaves_both_buckets(self):
        res = self.base_result()
        res["git"] = dict(res["git"], branch_unpushed=2)
        out = summarize([res])
        self.assertEqual(out["strict_card_rule"]["worktrees"], 0)
        self.assertEqual(out["branch_scoped_rule"]["worktrees"], 0)

    def test_branch_stash_blocks_delete_not_report(self):
        res = self.base_result()
        res["git"] = dict(res["git"], branch_stash=1)
        self.assertFalse(deletable(res))

    def test_unreadable_size_in_a_run_is_counted(self):
        import unittest.mock as mock
        cache = {"p": "/w/case_8c5fe4cc/node_modules", "name": "node_modules"}
        ok = {"w": "/w/case_8c5fe4cc", "reason": None, "det": {}, "caches": [cache],
              "git": {"repo_unpushed": 0, "repo_stash": 0,
                      "branch_unpushed": 0, "branch": "b", "branch_stash": 0,
                      "pr_proven": 0}}
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(reclaim_mod, "build_context",
                                  return_value={}), \
                mock.patch.object(reclaim_mod, "enumerate_worktrees",
                                  return_value=["/w/case_8c5fe4cc"]), \
                mock.patch.object(reclaim_mod, "evaluate_worktree",
                                  return_value=ok), \
                mock.patch.object(reclaim_mod, "dir_size",
                                  return_value=(0, False)):
            summary = run_tool(False, quiet_run, None, out, "/root", 30,
                               1)["summary"]
        self.assertEqual(summary["n_errors"], 1, summary)

    def test_nested_repo_dirs_are_reported(self):
        out = summarize([self.base_result(nested_repo=["node_modules",
                                                       ".venv"])])
        self.assertEqual(out["nested_repo_dirs"], 2)
        self.assertEqual(out["n_errors"], 0)

    def test_find_error_counts_as_error(self):
        out = summarize([self.base_result(reason="find-error", caches=[])])
        self.assertEqual(out["n_errors"], 1)
        self.assertEqual(out["excluded_by_rule"], {"find-error": 1})

    def test_n_errors_counts_git_errors_and_unverifiable_dirs(self):
        out = summarize([
            self.base_result(),
            self.base_result(reason="git-error", caches=[]),
            self.base_result(**{"ls-error": ["node_modules", ".venv"]}),
        ])
        self.assertEqual(out["n_errors"], 3)
        self.assertEqual(summarize([self.base_result()])["n_errors"], 0)


class ApplyRereadTests(unittest.TestCase):
    # The mutant this section kills: apply trusting the scan-time statuses.
    # The re-read must run per worktree and any non-terminal (INCLUDING a
    # re-read error, status "?") must skip.

    class FakeApi:
        def __init__(self, statuses):
            self.statuses = statuses
            self.calls = []

        def issue_status(self, issue_id):
            self.calls.append(issue_id)
            status = self.statuses.get(issue_id)
            if status == "RAISE":
                raise IOError("board unreachable")
            return status

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, ident="case_8c5fe4cc", iid="id-1", cache="node_modules"):
        """A real worktree dir tree plus the candidate dict a fresh scan emits."""
        wt = os.path.join(self.tmp.name, ident)
        os.makedirs(os.path.join(wt, ".git"))
        os.makedirs(os.path.join(wt, cache))
        with open(os.path.join(wt, cache, "x.js"), "w") as handle:
            handle.write("x\n")
        return {"w": wt, "reason": None,
                "linked": [ident], "linked_ids": {ident: iid},
                "ws_ids": ["ws-" + iid],
                "caches": [{"p": os.path.join(wt, cache), "name": cache,
                            "bytes": 10}],
                "git": {"branch_unpushed": 0, "branch_stash": 0}}

    def apply(self, api, cands, **kw):
        kw.setdefault("refresh", quiet_refs)
        return apply_deletes(kw.pop("run", quiet_run), api, cands, **kw)

    def test_reread_runs_and_error_skips(self):
        api = self.FakeApi({"id-1": "done", "id-2": "RAISE",
                            "id-3": "in_progress"})
        cands = [self.candidate("case_8c5fe4cc", "id-1"),
                 self.candidate("case_d02d0252", "id-2"),
                 self.candidate("case_a6dbab15", "id-3")]
        report = self.apply(api, cands)
        self.assertEqual(sorted(api.calls), ["id-1", "id-2", "id-3"])
        self.assertEqual(report["deleted_dirs"], 1)
        self.assertEqual(report["skipped_worktrees_card_reopened"], 2)
        self.assertFalse(os.path.exists(cands[0]["caches"][0]["p"]))
        self.assertTrue(os.path.isdir(cands[1]["caches"][0]["p"]))
        self.assertTrue(os.path.isdir(cands[2]["caches"][0]["p"]))

    def test_missing_link_id_skips(self):
        api = self.FakeApi({})
        cand = self.candidate()
        del cand["linked_ids"]
        report = self.apply(api, [cand])
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertEqual(report["skipped_worktrees_card_reopened"], 1)
        self.assertTrue(os.path.isdir(cand["caches"][0]["p"]))

    def test_empty_or_missing_linked_fails_closed(self):
        # No linked card means nothing to re-read, which is not a pass: the
        # worktree is skipped. Mutant: `still_ok = True` with an empty loop.
        for linked in ([], None):
            api = self.FakeApi({})
            cand = self.candidate()
            cand["linked"] = linked
            report = self.apply(api, [cand])
            self.assertEqual(report["deleted_dirs"], 0, linked)
            self.assertEqual(report["skipped_worktrees_card_reopened"], 1,
                             linked)
            self.assertEqual(api.calls, [])
            self.assertTrue(os.path.isdir(cand["caches"][0]["p"]))
            subprocess.run(["rm", "-rf", "--", cand["w"]], check=True)


class PreDeleteRecheckTests(unittest.TestCase):
    """case_cfed961c: every gate is re-proven immediately before each delete.

    The scan can be hours old when a worktree's turn comes (the whole
    evaluation precedes the first rm). Each case below differs from the
    positive control by ONE input, so a refusal can only come from that gate;
    the mutant for each is deleting that gate from recheck_worktree /
    recheck_cache, after which the cache is removed under a live reference.
    """

    FakeApi = ApplyRereadTests.FakeApi

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = self.FakeApi({"id-1": "done"})

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, ident="case_8c5fe4cc", iid="id-1", cache="node_modules"):
        return ApplyRereadTests.candidate(self, ident, iid, cache)

    def go(self, cand, run=quiet_run, refresh=quiet_refs, api=None):
        return apply_deletes(run, api or self.api, [cand], refresh=refresh)

    def assertKept(self, cand, report, wt_reason=None, dir_reason=None):
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertTrue(os.path.isdir(cand["caches"][0]["p"]))
        if wt_reason:
            self.assertEqual(report["skipped_worktrees_by_reason"],
                             {wt_reason: 1}, report)
        if dir_reason:
            self.assertEqual(report["skipped_dirs_by_reason"],
                             {dir_reason: 1}, report)

    def test_positive_control_deletes(self):
        cand = self.candidate()
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 1, report)
        self.assertEqual(report["skipped_worktrees_by_reason"], {})
        self.assertFalse(os.path.exists(cand["caches"][0]["p"]))

    def test_run_path_appearing_after_scan_keeps(self):
        cand = self.candidate()
        refs = dict(quiet_refs(None),
                    run_paths={os.path.join(cand["w"], "src")})
        self.assertKept(cand, self.go(cand, refresh=lambda api: refs),
                        "live-run-ref")

    def test_run_issue_id_appearing_after_scan_keeps(self):
        cand = self.candidate()
        refs = dict(quiet_refs(None), run_issue_ids={"id-1"})
        self.assertKept(cand, self.go(cand, refresh=lambda api: refs),
                        "live-run-ref")

    def test_run_workspace_id_appearing_after_scan_keeps(self):
        cand = self.candidate()
        refs = dict(quiet_refs(None), run_ws_ids={"ws-id-1"})
        self.assertKept(cand, self.go(cand, refresh=lambda api: refs),
                        "live-run-ref")

    def test_process_appearing_after_scan_keeps(self):
        cand = self.candidate()
        refs = dict(quiet_refs(None),
                    proc_paths={os.path.join(cand["w"], "node_modules", ".b")})
        self.assertKept(cand, self.go(cand, refresh=lambda api: refs),
                        "live-process")

    def test_refresh_error_keeps(self):
        cand = self.candidate()

        def boom(api):
            raise IOError("live-runs unreachable")
        self.assertKept(cand, self.go(cand, refresh=boom), "recheck-error")

    def test_refresh_error_counts_as_error(self):
        cand = self.candidate()

        def boom(api):
            raise IOError("live-runs unreachable")
        self.assertEqual(self.go(cand, refresh=boom)["n_errors"], 1)

    def test_card_reread_error_counts_as_error(self):
        cand = self.candidate()
        report = self.go(cand, api=self.FakeApi({"id-1": "RAISE"}))
        self.assertKept(cand, report, "card-reread-error")
        self.assertEqual(report["n_errors"], 1, report)

    def test_card_not_terminal_is_not_an_error(self):
        cand = self.candidate()
        report = self.go(cand, api=self.FakeApi({"id-1": "in_progress"}))
        self.assertKept(cand, report, "card-not-terminal")
        self.assertEqual(report["n_errors"], 0, report)

    def test_rm_failure_counts_as_error(self):
        import unittest.mock as mock
        cand = self.candidate()
        failed = subprocess.CompletedProcess(args=[], returncode=1)
        with mock.patch.object(reclaim_mod.subprocess, "run",
                               return_value=failed):
            report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertEqual(report["errors"], [cand["caches"][0]["p"]], report)
        self.assertEqual(report["n_errors"], 1, report)
        self.assertTrue(os.path.isdir(cand["caches"][0]["p"]))

    def test_rm_launch_failure_counts_as_error(self):
        import unittest.mock as mock
        cand = self.candidate()
        with mock.patch.object(reclaim_mod.subprocess, "run",
                               side_effect=OSError("no rm")):
            report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0, report)
        self.assertEqual(report["n_errors"], 1, report)
        self.assertTrue(os.path.isdir(cand["caches"][0]["p"]))

    def test_check_ignore_fatal_at_apply_keeps_and_counts(self):
        cand = self.candidate()

        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "check-ignore"]:
                return 128, ""
            return 0, ""
        report = self.go(cand, run=run)
        self.assertKept(cand, report, dir_reason="check-ignore-error")
        self.assertEqual(report["n_errors"], 1, report)

    def test_symbolic_ref_error_at_apply_is_git_error(self):
        cand = self.candidate()

        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[:2] == ["git", "symbolic-ref"]:
                return 128, ""
            return 0, ""
        report = self.go(cand, run=run)
        self.assertKept(cand, report, "git-error")
        self.assertEqual(report["n_errors"], 1, report)

    def test_nested_repo_in_cache_keeps(self):
        # Mutant: dropping the nested-repo gate deletes a pip editable
        # checkout (.venv/src/pkg/.git) whose unpushed commits no HEAD
        # proof covers.
        cand = self.candidate(cache=".venv")
        cache = cand["caches"][0]["p"]

        def run(args, cwd=None, timeout=300, stdin_text=None):
            if args[0] == "find" and "HEAD" in args:
                return 0, os.path.join(cache, "src", "pkg", ".git") + "\n"
            return 0, ""
        self.assertKept(cand, self.go(cand, run=run), dir_reason="nested-repo")

    def test_refresh_runs_once_per_worktree_not_once_per_pass(self):
        # Mutant: hoisting the refresh out of the loop reads one snapshot for
        # the whole pass, which is the staleness this card removes.
        calls = []

        def counting(api):
            calls.append(1)
            return quiet_refs(api)
        cands = [self.candidate("case_8c5fe4cc", "id-1"),
                 self.candidate("case_d02d0252", "id-2")]
        api = self.FakeApi({"id-1": "done", "id-2": "done"})
        apply_deletes(quiet_run, api, cands, refresh=counting)
        self.assertEqual(len(calls), 2)

    def test_card_reread_precedes_refresh(self):
        # A reopened card must not even cost a live-refs fetch.
        calls = []
        cand = self.candidate()
        api = self.FakeApi({"id-1": "in_progress"})
        apply_deletes(quiet_run, api, [cand],
                      refresh=lambda a: calls.append(1) or quiet_refs(a))
        self.assertEqual(calls, [])

    def test_recent_write_after_scan_keeps(self):
        cand = self.candidate()
        run = scripted_run(find=(0, os.path.join(cand["w"], "fresh.txt\n")))
        self.assertKept(cand, self.go(cand, run=run), "recent-activity")

    def test_find_error_is_refused_and_counted_at_apply(self):
        # An unreadable subtree is unknown, not idle. Mutant: ignore rc.
        cand = self.candidate()
        run = scripted_run(find=(1, ""))
        report = self.go(cand, run=run)
        self.assertKept(cand, report, "find-error")
        self.assertEqual(report["n_errors"], 1, report)

    def test_commit_made_after_scan_keeps(self):
        # `git log HEAD --not --remotes` lists a commit, and the remote proof
        # cannot be read (ls-remote fails): kept, and counted as an error.
        cand = self.candidate()
        run = scripted_run(**{"ls-remote": (128, ""),
                              "log HEAD": (0, "a" * 40 + "\n")})
        report = self.go(cand, run=run)
        self.assertKept(cand, report, "remote-unreadable")
        self.assertEqual(report["n_errors"], 0, report)

    def test_head_listing_error_keeps(self):
        cand = self.candidate()
        run = scripted_run(**{"log HEAD": (128, "")})
        self.assertKept(cand, self.go(cand, run=run), "recheck-error")

    def test_stash_saved_after_scan_keeps(self):
        cand = self.candidate()
        run = scripted_run(**{"symbolic-ref": (0, "feat\n"),
                              "stash list": (0, "stash@{0}: WIP on feat: a b\n")})
        self.assertKept(cand, self.go(cand, run=run), "branch-stash")

    def test_other_branch_stash_does_not_keep(self):
        # Control for the branch-scoped rule: a stash naming another branch
        # is repo noise, not this worktree's work.
        cand = self.candidate()
        run = scripted_run(**{"symbolic-ref": (0, "feat\n"),
                              "stash list": (0, "stash@{0}: WIP on other: a b\n")})
        self.assertEqual(self.go(cand, run=run)["deleted_dirs"], 1)

    def test_detached_head_any_stash_keeps(self):
        cand = self.candidate()
        run = scripted_run(**{"symbolic-ref": (1, ""),
                              "stash list": (0, "stash@{0}: WIP on x: a b\n")})
        self.assertKept(cand, self.go(cand, run=run), "branch-stash")

    def test_stash_list_error_keeps(self):
        cand = self.candidate()
        run = scripted_run(**{"stash list": (128, "")})
        self.assertKept(cand, self.go(cand, run=run), "git-error")

    def test_git_dir_gone_keeps(self):
        cand = self.candidate()
        subprocess.run(["rm", "-rf", "--", os.path.join(cand["w"], ".git")],
                       check=True)
        self.assertKept(cand, self.go(cand), "not-a-git-worktree")

    def test_missing_workspace_ids_keeps(self):
        # A candidate without the ids the live-run gate needs is not proven.
        cand = self.candidate()
        del cand["ws_ids"]
        self.assertKept(cand, self.go(cand), "no-link-ids")

    # --- per-directory gate, last thing before rm ---------------------------

    def test_dir_no_longer_ignored_keeps(self):
        cand = self.candidate()
        run = scripted_run(**{"check-ignore": (1, "")})
        self.assertKept(cand, self.go(cand, run=run), dir_reason="not-ignored")

    def test_dir_became_tracked_keeps(self):
        cand = self.candidate()
        run = scripted_run(**{"ls-files": (0, "node_modules/x.js\n")})
        self.assertKept(cand, self.go(cand, run=run),
                        dir_reason="tracked-inside")

    def test_ls_files_error_keeps(self):
        cand = self.candidate()
        run = scripted_run(**{"ls-files": (128, "")})
        self.assertKept(cand, self.go(cand, run=run),
                        dir_reason="ls-files-error")

    def test_dir_swapped_for_symlink_after_scan_keeps(self):
        # rm -rf on a symlinked node_modules would follow it out of the
        # worktree; the swap between scan and delete must be caught.
        cand = self.candidate()
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(outside)
        with open(os.path.join(outside, "precious"), "w") as handle:
            handle.write("keep\n")
        path = cand["caches"][0]["p"]
        subprocess.run(["rm", "-rf", "--", path], check=True)
        os.symlink(outside, path)
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertEqual(report["skipped_dirs_by_reason"], {"symlink": 1})
        self.assertTrue(os.path.exists(os.path.join(outside, "precious")))

    def test_dir_already_gone_is_skipped_not_counted(self):
        cand = self.candidate()
        subprocess.run(["rm", "-rf", "--", cand["caches"][0]["p"]], check=True)
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertEqual(report["skipped_dirs_by_reason"], {"gone": 1})

    def test_path_escaping_worktree_keeps(self):
        cand = self.candidate()
        outside = os.path.join(self.tmp.name, "case_8c5fe4cc-sibling", "node_modules")
        os.makedirs(outside)
        cand["caches"][0]["p"] = outside
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertTrue(os.path.isdir(outside))
        self.assertEqual(report["skipped_dirs_by_reason"],
                         {"escapes-worktree": 1})

    # --- Cargo target never reaches rm (case_160a06b2) --------------------------

    def test_cargo_target_candidate_is_refused_by_name(self):
        # Defence in depth: even if a stale or handcrafted candidate names a
        # `target` dir, with every other gate green, it must survive.
        # Mutant: drop the name guard in recheck_cache.
        cand = self.candidate(cache="target")
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertEqual(report["skipped_dirs_by_reason"],
                         {"not-a-reclaimable-name": 1})
        self.assertTrue(os.path.isdir(cand["caches"][0]["p"]))

    def test_name_path_mismatch_is_refused(self):
        cand = self.candidate()
        cand["caches"][0]["name"] = "vendor"
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertEqual(report["skipped_dirs_by_reason"],
                         {"not-a-reclaimable-name": 1})

    def test_arbitrary_dir_name_is_refused(self):
        cand = self.candidate(cache="src")
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 0)
        self.assertTrue(os.path.isdir(cand["caches"][0]["p"]))

    def test_mixed_worktree_keeps_target_removes_cache(self):
        cand = self.candidate()
        target = os.path.join(cand["w"], "target")
        os.makedirs(target)
        cand["caches"].append({"p": target, "name": "target", "bytes": 5})
        report = self.go(cand)
        self.assertEqual(report["deleted_dirs"], 1)
        self.assertTrue(os.path.isdir(target))
        self.assertFalse(os.path.exists(os.path.join(cand["w"], "node_modules")))


class CargoTargetExclusionTests(unittest.TestCase):
    """case_160a06b2 CTO condition 2: Cargo `target/` is out of the aggregate."""

    def test_target_is_not_a_dep_cache_name(self):
        self.assertNotIn("target", DEP_CACHE_NAMES)
        self.assertIn("target", CARGO_TARGET_NAMES)
        self.assertFalse(DEP_CACHE_NAMES & CARGO_TARGET_NAMES)

    def test_summarize_reports_excluded_target_bytes(self):
        res = {"w": "/w/case_8c5fe4cc", "reason": None, "det": {},
               "caches": [{"p": "/w/case_8c5fe4cc/node_modules",
                           "name": "node_modules", "bytes": 100}],
               "cargo_target_excluded": [{"p": "/w/case_8c5fe4cc/target",
                                          "name": "target", "bytes": 7000}],
               "git": {"repo_unpushed": 0, "repo_stash": 0,
                       "branch_unpushed": 0, "branch": "b", "branch_stash": 0,
                       "pr_proven": 0}}
        out = summarize([res])
        self.assertEqual(out["cargo_target_excluded"],
                         {"worktrees": 1, "dirs": 1, "bytes": 7000})
        # The excluded bytes must not leak into any reclaim total.
        self.assertEqual(out["total_ignored_cache_bytes_before_git_gate"], 100)
        self.assertEqual(out["branch_scoped_rule"]["bytes"], 100)
        self.assertEqual(out["strict_card_rule"]["bytes"], 100)

    def test_target_only_worktree_is_not_deletable(self):
        res = {"w": "/w/case_8c5fe4cc", "reason": None, "caches": [],
               "cargo_target_excluded": [{"p": "/w/case_8c5fe4cc/target",
                                          "name": "target"}],
               "git": {"branch_unpushed": 0, "branch_stash": 0}}
        self.assertFalse(deletable(res))


class RecentActivityFailClosedTests(unittest.TestCase):
    # Mutant: ignore find's exit status. An unreadable subtree then reads as
    # "nothing recent" and a possibly-busy worktree becomes deletable.

    def test_find_error_raises(self):
        with self.assertRaises(ReclaimError):
            recent_activity(scripted_run(find=(1, "")), "/w", 30)

    def test_clean_empty_find_is_idle(self):
        self.assertFalse(recent_activity(scripted_run(find=(0, "")), "/w", 30))

    def test_hit_is_activity(self):
        self.assertTrue(recent_activity(
            scripted_run(find=(0, "/w/a.txt\n")), "/w", 30))


def reg_entry(**over):
    entry = {"id": "routine-x", "state": "active", "role": "writer",
             "writes": [REGISTRY_WRITE_TARGET]}
    entry.update(over)
    return entry


class NestedRepoTests(unittest.TestCase):
    # A git repo inside a dep cache keeps the cache. A find error or a
    # timeout means the cache could not be proven free of one.

    def test_no_git_below_cache_is_clean(self):
        self.assertFalse(nested_repo(quiet_run, "/w/node_modules"))

    def test_git_below_cache_is_nested(self):
        self.assertTrue(nested_repo(
            scripted_run(find=(0, "/w/.venv/src/pkg/.git\n")), "/w/.venv"))

    def test_find_error_raises(self):
        with self.assertRaises(ReclaimError):
            nested_repo(scripted_run(find=(1, "")), "/w/.venv")

    def test_find_timeout_raises(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            raise subprocess.TimeoutExpired(args, timeout)
        with self.assertRaises(subprocess.TimeoutExpired):
            nested_repo(run, "/w/.venv")


    def test_find_cache_dirs_error_raises(self):
        with self.assertRaises(ReclaimError):
            find_cache_dirs(scripted_run(find=(1, "")), "/w")


class SizingTests(unittest.TestCase):
    # dir_bytes is shared with the prune tool: its int contract is pinned.

    def test_dir_bytes_contract_is_unchanged(self):
        self.assertEqual(
            dir_bytes(scripted_run(du=(0, "4096 /w/x\n")), "/w/x"), 4096)
        self.assertEqual(dir_bytes(scripted_run(du=(0, "")), "/w/x"), 0)

    def test_du_timeout_is_unknown_not_a_crash(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            raise subprocess.TimeoutExpired(args, timeout)
        self.assertEqual(dir_size(run, "/w/node_modules"), (0, False))

    def test_du_failure_is_partial_and_unknown(self):
        self.assertEqual(
            dir_size(scripted_run(du=(1, "123 /w/x\n")), "/w/x"), (123, False))

    def test_du_total_is_parsed(self):
        self.assertEqual(
            dir_size(scripted_run(du=(0, "4096 /w/x\n")), "/w/x"), (4096, True))

    def test_du_empty_output_is_unknown_not_zero_pass(self):
        self.assertEqual(dir_size(scripted_run(du=(0, "")), "/w/x"), (0, False))

    def test_non_utf8_output_is_unknown(self):
        def run(args, cwd=None, timeout=300, stdin_text=None):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
        self.assertEqual(dir_size(run, "/w/x"), (0, False))

    def test_unknown_size_is_counted_not_zero_silently(self):
        res = {"w": "/w/case_8c5fe4cc", "reason": None, "det": {},
               "git": {"repo_unpushed": 0, "repo_stash": 0,
                       "branch_unpushed": 0, "branch": "b",
                       "branch_stash": 0, "pr_proven": 0},
               "caches": [{"p": "/w/case_8c5fe4cc/node_modules",
                           "name": "node_modules", "bytes": 0,
                           "size_error": True}]}
        self.assertEqual(summarize([res])["n_errors"], 1)


    def test_head_file_without_objects_and_refs_is_not_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = os.path.join(tmp, "HEAD")
            open(head, "w").close()
            self.assertFalse(nested_repo(scripted_run(find=(0, head + "\n")), tmp))


class RegistryGateTests(unittest.TestCase):
    # case_cfed961c: a recurring firing deletes only when the reviewed WS7
    # registry says so. Everything else is a dry-run. Each case flips ONE
    # field of the apply-enabling entry.

    def test_active_writer_with_target_applies(self):
        apply, why = registry_apply_decision([reg_entry()], "routine-x")
        self.assertTrue(apply, why)

    def test_detector_is_dry_run(self):
        apply, _ = registry_apply_decision(
            [reg_entry(role="detector", writes=[])], "routine-x")
        self.assertFalse(apply)

    def test_detector_with_stray_write_target_is_dry_run(self):
        # The role is the switch; a write target alone must not enable apply.
        apply, _ = registry_apply_decision(
            [reg_entry(role="detector")], "routine-x")
        self.assertFalse(apply)

    def test_non_active_states_are_dry_run(self):
        for state in ("planned", "paused", "retire-pending", "retired", None):
            with self.subTest(state=state):
                apply, _ = registry_apply_decision(
                    [reg_entry(state=state)], "routine-x")
                self.assertFalse(apply)

    def test_writer_without_the_target_is_dry_run(self):
        apply, _ = registry_apply_decision(
            [reg_entry(writes=["fs:something-else"])], "routine-x")
        self.assertFalse(apply)
        apply, _ = registry_apply_decision(
            [reg_entry(writes=None)], "routine-x")
        self.assertFalse(apply)

    def test_missing_or_duplicate_entry_is_dry_run(self):
        self.assertFalse(registry_apply_decision([], "routine-x")[0])
        self.assertFalse(registry_apply_decision(
            [reg_entry(id="other")], "routine-x")[0])
        self.assertFalse(registry_apply_decision(
            [reg_entry(), reg_entry()], "routine-x")[0])

    def test_malformed_registry_is_dry_run(self):
        for bad in (None, {}, "x", [None], [1, "x"]):
            with self.subTest(bad=bad):
                self.assertFalse(registry_apply_decision(bad, "routine-x")[0])

    def write_registry(self, tmp, doc):
        path = os.path.join(tmp, "registry.json")
        with open(path, "w") as handle:
            handle.write(doc if isinstance(doc, str) else json.dumps(doc))
        return path

    def test_resolve_mode_defaults_to_dry_run(self):
        self.assertEqual(resolve_mode(parse_argv([]))[0], False)
        self.assertEqual(resolve_mode(parse_argv(["--apply"]))[0], True)

    def test_resolve_mode_reads_the_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = self.write_registry(tmp, {"entries": [reg_entry()]})
            argv = ["--registry-gate", "routine-x", "--registry", writer]
            self.assertTrue(resolve_mode(parse_argv(argv))[0])
            detector = self.write_registry(
                tmp, {"entries": [reg_entry(role="detector")]})
            argv = ["--registry-gate", "routine-x", "--registry", detector]
            self.assertFalse(resolve_mode(parse_argv(argv))[0])

    def test_resolve_mode_unreadable_registry_is_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = [os.path.join(tmp, "absent.json"),
                     self.write_registry(tmp, "not json{"),
                     self.write_registry(tmp, ["a", "list"]),
                     self.write_registry(tmp, {"entries": "nope"})]
            for path in cases:
                with self.subTest(path=os.path.basename(path)):
                    argv = ["--registry-gate", "routine-x",
                            "--registry", path]
                    apply, why = resolve_mode(parse_argv(argv))
                    self.assertFalse(apply, why)

    def test_gate_and_apply_are_mutually_exclusive(self):
        # Mutant: allowing both lets an explicit --apply override a registry
        # that says dry-run.
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                parse_argv(["--apply", "--registry-gate", "routine-x"])
        self.assertEqual(caught.exception.code, 2)

    def test_shipped_registry_entry_is_an_apply_writer(self):
        # Pins the shipped registry. Dropping back to dry-run is the rollback,
        # and it is a reviewed edit of THIS assertion in the same change.
        with open(REGISTRY_DEFAULT, encoding="utf-8") as handle:
            entries = json.load(handle)["entries"]
        entry = next(e for e in entries
                     if e["id"] == "routine-worktree-cache-reclaim")
        self.assertEqual(entry["state"], "active")
        self.assertEqual(entry["role"], "writer")
        self.assertEqual(entry["writes"], [REGISTRY_WRITE_TARGET])
        apply, why = registry_apply_decision(
            entries, "routine-worktree-cache-reclaim")
        self.assertTrue(apply, why)


class RunToolCountTests(unittest.TestCase):
    # One count to escalate on: the apply report's n_errors includes the
    # evaluation errors printed in the summary.

    def test_apply_report_counts_evaluation_errors_too(self):
        import unittest.mock as mock
        ok = {"w": "/w/case_8c5fe4cc", "reason": None, "det": {}, "caches": [],
              "git": None}
        bad = {"w": "/w/case_d02d0252", "reason": "find-error", "det": {},
               "caches": [], "git": None}

        def fake_eval(run, ctx, w, minutes):
            return bad if w.endswith("2") else ok
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(reclaim_mod, "build_context",
                                  return_value={}), \
                mock.patch.object(reclaim_mod, "enumerate_worktrees",
                                  return_value=["/w/case_8c5fe4cc", "/w/case_d02d0252"]), \
                mock.patch.object(reclaim_mod, "evaluate_worktree",
                                  side_effect=fake_eval), \
                mock.patch.object(reclaim_mod, "apply_deletes",
                                  return_value={"deleted_dirs": 0,
                                                "n_errors": 2}):
            applied = run_tool(True, quiet_run, None, out, "/root", 30,
                               1)["applied"]
        self.assertEqual(applied["evaluation_n_errors"], 1, applied)
        self.assertEqual(applied["n_errors"], 3, applied)

    def test_dryrun_write_failure_keeps_old_file_and_prints_summary(self):
        summary = {"n_errors": 0}
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "dryrun.json")
            with mock.patch.object(reclaim_mod, "build_context", return_value={}), \
                    mock.patch.object(reclaim_mod, "enumerate_worktrees", return_value=[]), \
                    mock.patch.object(reclaim_mod, "summarize", return_value=summary):
                run_tool(False, quiet_run, None, out, "/root", 30, 1)
            with open(path) as handle:
                self.assertEqual(json.load(handle)["summary"]["n_errors"], 0)
            previous = b"previous dryrun bytes\\n"
            with open(path, "wb") as handle:
                handle.write(previous)
            stdout = io.StringIO()
            with mock.patch.object(reclaim_mod, "build_context", return_value={}), \
                    mock.patch.object(reclaim_mod, "enumerate_worktrees", return_value=[]), \
                    mock.patch.object(reclaim_mod, "summarize", return_value=summary), \
                    mock.patch.object(reclaim_mod.os, "replace",
                                      side_effect=OSError(errno.ENOSPC, "No space")), \
                    contextlib.redirect_stdout(stdout):
                with self.assertRaises(ReclaimError) as raised:
                    run_tool(False, quiet_run, None, out, "/root", 30, 1)
            self.assertEqual(json.loads(stdout.getvalue())["n_errors"], 0)
            self.assertIn(path, str(raised.exception))
            self.assertIn("errno=28", str(raised.exception))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), previous)
            self.assertEqual(os.listdir(out), ["dryrun.json"])

    def test_apply_write_failure_keeps_old_file_and_prints_report(self):
        summary = {"n_errors": 0}
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "apply.json")
            previous = b"previous apply bytes\\n"
            with open(path, "wb") as handle:
                handle.write(previous)
            real_replace = os.replace
            calls = 0

            def fail_second_replace(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.ENOSPC, "No space")
                return real_replace(source, target)

            stdout = io.StringIO()
            with mock.patch.object(reclaim_mod, "build_context", return_value={}), \
                    mock.patch.object(reclaim_mod, "enumerate_worktrees", return_value=[]), \
                    mock.patch.object(reclaim_mod, "summarize", return_value=summary), \
                    mock.patch.object(reclaim_mod, "apply_deletes",
                                      return_value={"n_errors": 0}), \
                    mock.patch.object(reclaim_mod.os, "replace",
                                      side_effect=fail_second_replace), \
                    contextlib.redirect_stdout(stdout):
                with self.assertRaises(ReclaimError) as raised:
                    run_tool(True, quiet_run, None, out, "/root", 30, 1)
            decoder = json.JSONDecoder()
            output = stdout.getvalue()
            first, end = decoder.raw_decode(output)
            while end < len(output) and output[end].isspace():
                end += 1
            second, _ = decoder.raw_decode(output, end)
            self.assertEqual(first["n_errors"], 0)
            self.assertEqual(second["n_errors"], 0)
            self.assertIn(path, str(raised.exception))
            self.assertIn("errno=28", str(raised.exception))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), previous)
            self.assertEqual(sorted(os.listdir(out)), ["apply.json", "dryrun.json"])


class ArgvDefaultTests(unittest.TestCase):
    # The mutant this section kills: --apply defaulting true. The tool must
    # delete nothing unless the flag is passed.

    def test_dry_run_is_default(self):
        self.assertFalse(parse_argv([]).apply)

    def test_apply_is_opt_in(self):
        self.assertTrue(parse_argv(["--apply"]).apply)

    def test_apply_refuses_without_api_env(self):
        # A blind apply (no live board to re-read cards from) is worse than
        # no apply: it must refuse LOUDLY even when --out-dir is fine.
        import contextlib
        import io
        import unittest.mock as mock
        from terminal_worktree_cache_reclaim import main
        with tempfile.TemporaryDirectory() as out:
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SafetyRefusal):
                        main(argv=["--apply", "--out-dir", out],
                             run=lambda *a, **k: (0, ""), api=None)
            self.assertIn("mode: apply (--apply)", stderr.getvalue())
            self.assertFalse(os.path.exists(os.path.join(out, "dryrun.json")))
            self.assertFalse(os.path.exists(os.path.join(out, "apply.json")))

    def test_dry_run_needs_api_env_too(self):
        # The scan-time card join reads the live board: no API env means no
        # evaluation at all, not an empty report that reads as "all clear".
        import unittest.mock as mock
        from terminal_worktree_cache_reclaim import main
        with tempfile.TemporaryDirectory() as out:
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ReclaimError):
                    main(argv=["--out-dir", out],
                         run=lambda *a, **k: (0, ""), api=None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
