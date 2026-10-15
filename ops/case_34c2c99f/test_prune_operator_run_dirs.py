#!/usr/bin/env python3
# ===========================================================================
# test_prune_operator_run_dirs.py — offline tests for prune-operator-run-dirs.py
# (case_34c2c99f).
#
# Written to FAIL a plausible-but-wrong implementation, not to confirm the one
# that exists. Every section names the mutant it kills. This tool DELETES run
# folders on a production host, so the tests that matter most are the ones
# proving it refuses: a live run, a scheduled retry, a dirty tree, an
# unpushed commit, a stash, a young failed run, a non-terminal run, an
# unknown run (trim-only, never delete), an unexpected name, a symlink, an
# unreadable /proc, an unreadable git repo, and a tracked dir wearing a
# regenerable name (git-ignored gate).
#
# Deterministic: no network, no credentials. Git-touching paths run against
# throwaway local repos (a bare origin plus a clone for the pushed case); the
# API boundary is a fake. Real `shutil.rmtree` is exercised on fixture dirs,
# never mocked.
#
#   python3 ops/case_34c2c99f/test_prune_operator_run_dirs.py
# ===========================================================================
import contextlib
import datetime
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import prune_operator_run_dirs as tool  # noqa: E402
from prune_operator_run_dirs import (  # noqa: E402
    PruneError,
    SafetyRefusal,
    age_bar_hours,
    build_run_index,
    candidate_subtree_paths,
    classify,
    dir_is_idle,
    evaluate_run,
    git_verdict,
    kill_switch,
    order_for_removal,
    parse_ts,
    path_is_git_ignored,
    plan_trim,
    retry_pending,
    revalidate,
    run_terminal_age_hours,
    scan_run_dirs,
)

HOUR = 3600


def iso(hours_ago, now=None):
    stamp = (now if now is not None else time.time()) - hours_ago * HOUR
    return datetime.datetime.fromtimestamp(
        stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# An ambient (even empty) GIT_AUTHOR_*/GIT_COMMITTER_* beats the repo-local
# user.name/user.email the fixtures set, so scrub them: hermetic everywhere.
GIT_ENV = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}


def git(*args, cwd):
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=GIT_ENV,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


RUN_A = "00000000-0000-4000-8000-000000000123"
RUN_B = "00000000-0000-4000-8000-000000000124"


class FakeApi:
    """Minimal control-plane fake: recent window, per-id rows, live runs."""

    def __init__(self, recent=(), by_id=None, live=()):
        self.recent = list(recent)
        self.by_id = dict(by_id or {})
        self.live = list(live)
        self.plain = "https://fake/api"

    def heartbeat_runs(self):
        return self.recent

    def live_runs(self):
        return self.live

    def _get(self, url):
        run_id = url.rsplit("/", 1)[-1]
        if run_id in self.by_id:
            return self.by_id[run_id]
        raise Exception("404 Not Found: %s" % run_id)


def run_row(status, finished_hours_ago=None, issue="issue-1", retry=None):
    row = {"id": RUN_A, "status": status, "issueId": issue,
           "scheduledRetryAt": None}
    if finished_hours_ago is not None:
        row["finishedAt"] = iso(finished_hours_ago)
    if retry is not None:
        row["scheduledRetryAt"] = iso(-retry)  # negative = future
    return row


def backdate(root, seconds_ago=7200):
    """Backdate every non-.git file so the idle gate sees idle."""
    stamp = time.time() - seconds_ago
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        try:
            os.utime(dirpath, (stamp, stamp))
        except OSError:
            pass
        for name in filenames:
            try:
                os.utime(os.path.join(dirpath, name), (stamp, stamp))
            except OSError:
                pass


def patch_empty_holds(testcase, text="# no custody holds\n"):
    """Point tool.KEEP_PATH at a temp hold file; returns its path.

    run_tool, revalidate and apply_plan read the hold file, so any test
    reaching them needs a real one: without this the suite would read the
    repo checkout (no keep file there) and fail closed.
    """
    keepdir = tempfile.TemporaryDirectory()
    testcase.addCleanup(keepdir.cleanup)
    path = os.path.join(keepdir.name, "regen-prune-keep.txt")
    with open(path, "w") as handle:
        handle.write(text)
    patcher = mock.patch.object(tool, "KEEP_PATH", path)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return path


def make_repo(path, pushed=True):
    """Throwaway repo at path. pushed=False leaves commits unpushed."""
    os.makedirs(path, exist_ok=True)
    git("init", "-b", "main", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    with open(os.path.join(path, ".gitignore"), "w") as handle:
        handle.write("node_modules/\n.claude/\n.paperclip/\ntarget/\n*.local\n")
    with open(os.path.join(path, "README.md"), "w") as handle:
        handle.write("hello\n")
    git("add", "-A", cwd=path)
    git("commit", "-m", "init", cwd=path)
    if pushed:
        origin = path + "-origin.git"
        git("init", "--bare", origin, cwd=path)
        git("remote", "add", "origin", origin, cwd=path)
        git("push", "-u", "origin", "main", cwd=path)


class PureCoreTest(unittest.TestCase):
    # Mutant: parse failures read as old.
    def test_parse_ts_rejects_garbage(self):
        self.assertIsNone(parse_ts(None))
        self.assertIsNone(parse_ts(""))
        self.assertIsNone(parse_ts("not-a-date"))
        self.assertIsNotNone(parse_ts("2026-10-06T12:00:00.000Z"))

    # Mutant: terminal status without finishedAt counts as old.
    def test_terminal_requires_finished_at(self):
        now = time.time()
        terminal, age = run_terminal_age_hours(
            {"status": "failed"}, now)
        self.assertFalse(terminal)
        self.assertIsNone(age)
        terminal, age = run_terminal_age_hours(
            {"status": "running", "finishedAt": iso(99, now)}, now)
        self.assertFalse(terminal)

    def test_terminal_age(self):
        now = time.time()
        terminal, age = run_terminal_age_hours(
            {"status": "failed", "finishedAt": iso(30, now)}, now)
        self.assertTrue(terminal)
        self.assertAlmostEqual(age, 30, places=1)

    # Mutant: a past retry keeps the folder forever.
    def test_retry_pending_only_future(self):
        now = time.time()
        self.assertTrue(retry_pending(
            {"scheduledRetryAt": iso(-2, now)}, now))
        self.assertFalse(retry_pending(
            {"scheduledRetryAt": iso(2, now)}, now))
        self.assertFalse(retry_pending({}, now))

    # Mutant: one age bar for every status (succeeded runs pile up, or
    # failed runs vanish after 30 minutes).
    def test_age_bars(self):
        self.assertEqual(age_bar_hours("succeeded"), 0.5)
        self.assertEqual(age_bar_hours("failed"), 24.0)
        self.assertEqual(age_bar_hours("cancelled"), 24.0)
        self.assertEqual(age_bar_hours("timed_out"), 24.0)

    # Mutant: young failed runs are removable.
    def test_classify_young_failed_kept(self):
        reason, _ = classify(True, True, 2.0, "failed")
        self.assertEqual(reason, "run-too-recent")
        self.assertIsNone(classify(True, True, 25.0, "failed"))
        reason, _ = classify(True, True, 0.1, "succeeded")
        self.assertEqual(reason, "run-too-recent")
        self.assertIsNone(classify(True, True, 0.6, "succeeded"))

    # Mutant: non-terminal or unverified workspaces are removable.
    def test_classify_requires_terminal_and_verified(self):
        self.assertEqual(classify(True, False, None, "running")[0],
                         "run-not-terminal")
        self.assertEqual(classify(False, True, 99.0, "failed")[0],
                         "workspace-not-verified")

    # Mutant: smallest-first removal wastes the bounded pass.
    def test_order_largest_first(self):
        rows = [{"run_id": "b", "bytes": 1}, {"run_id": "a", "bytes": 9}]
        self.assertEqual([r["run_id"] for r in
                          order_for_removal(rows)], ["a", "b"])

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as root:
            tripped, _ = kill_switch({"OPERATOR_RUN_PRUNE_DISABLED": "1"},
                                     root)
            self.assertTrue(tripped)
            tripped, _ = kill_switch({}, root)
            self.assertFalse(tripped)
            open(os.path.join(root, tool.KILL_FILE_NAME), "w").close()
            tripped, _ = kill_switch({}, root)
            self.assertTrue(tripped)


class GitGatesTest(unittest.TestCase):
    # Mutant: dirty trees read as clean.
    def test_dirty_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            with open(os.path.join(repo, "DIRTY.txt"), "w") as handle:
                handle.write("x")
            ok, why = git_verdict(repo)
            self.assertFalse(ok)
            self.assertEqual(why, "dirty")

    # Mutant: unpushed commits read as pushed.
    def test_unpushed_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo, pushed=False)
            ok, why = git_verdict(repo)
            self.assertFalse(ok)
            self.assertEqual(why, "unpushed-commits")

    # Mutant: `git log --branches --not --remotes` only (no HEAD): a commit
    # made on a DETACHED HEAD is on no branch, so it reads as pushed and the
    # folder holding the only copy is deleted.
    def test_detached_head_commit_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            git("checkout", "--detach", cwd=repo)
            with open(os.path.join(repo, "only-copy.txt"), "w") as handle:
                handle.write("work that exists nowhere else\n")
            git("add", "-A", cwd=repo)
            git("commit", "-m", "detached work", cwd=repo)
            ok, why = git_verdict(repo)
            self.assertFalse(ok)
            self.assertEqual(why, "unpushed-commits")

    # Mutant: a git refusal for the wrong owner reads as a generic error (the
    # operator cannot tell a deploy mistake from rot) or as clean.
    def test_dubious_ownership_named(self):
        refusal = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="",
            stderr="fatal: detected dubious ownership in repository at '/x'")
        with mock.patch.object(tool.subprocess, "run", return_value=refusal):
            self.assertEqual(git_verdict("/x"),
                             (False, "git-dubious-ownership"))

    # Mutant: stashes read as absent.
    def test_stash_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            with open(os.path.join(repo, "wip.txt"), "w") as handle:
                handle.write("wip")
            git("stash", "-u", cwd=repo)
            ok, why = git_verdict(repo)
            self.assertFalse(ok)
            self.assertEqual(why, "stash-present")

    # Mutant: missing/broken git reads as clean (fail open).
    def test_no_repo_unverifiable(self):
        with tempfile.TemporaryDirectory() as root:
            ok, why = git_verdict(root)
            self.assertFalse(ok)
            self.assertEqual(why, "git-unreadable")

    def test_clean_pushed_ok(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            self.assertEqual(git_verdict(repo), (True, None))

    # Mutant: ignored-path gate inverted — tracked content trimmed.
    def test_tracked_dir_never_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            tracked = os.path.join(repo, "node_modules")
            os.makedirs(tracked)
            with open(os.path.join(tracked, "real.js"), "w") as handle:
                handle.write("// shipped vendored dep\n")
            git("add", "-f", "node_modules/real.js", cwd=repo)
            git("commit", "-m", "vendor", cwd=repo)
            self.assertFalse(path_is_git_ignored(repo, tracked))
            self.assertEqual(plan_trim(repo, True), [])

    # Mutant: nested worktree copies trimmed without any content check (F1).
    def test_nested_worktrees_never_planned(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            nested = os.path.join(repo, ".claude", "worktrees", "x")
            os.makedirs(nested)
            with open(os.path.join(nested, "f"), "w") as handle:
                handle.write("copy\n")
            mods = os.path.join(repo, "node_modules")
            os.makedirs(mods)
            with open(os.path.join(mods, "dep.js"), "w") as handle:
                handle.write("dep\n")
            plans = {os.path.relpath(p["path"], repo)
                     for p in plan_trim(repo, True)}
            self.assertEqual(plans, {"node_modules"})

    def test_candidate_paths_skip_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            link = os.path.join(repo, "node_modules")
            os.symlink("/tmp", link)
            self.assertEqual(candidate_subtree_paths(repo), [])


class NestedRepoGateTest(unittest.TestCase):
    """Every git repo inside a run folder needs its own proof (F1, F3)."""

    def _folder_with_worktree(self, root, branch="feat"):
        repo = os.path.join(root, "repo")
        make_repo(repo)
        nested = os.path.join(repo, ".claude", "worktrees", branch)
        git("worktree", "add", "-b", branch, nested, cwd=repo)
        return repo, nested

    # Mutant: only the top level is checked, so a nested worktree's detached
    # commit that no remote has is invisible and the folder is deleted (F1).
    # The top level's --all already reaches linked-worktree HEADs on some git
    # versions, so the reason label may be either; the outcome must be a keep.
    def test_nested_detached_commit_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo, nested = self._folder_with_worktree(root)
            git("checkout", "--detach", cwd=nested)
            with open(os.path.join(nested, "only-copy.txt"), "w") as handle:
                handle.write("work that exists nowhere else\n")
            git("add", "-A", cwd=nested)
            git("commit", "-m", "detached work", cwd=nested)
            ok, why = git_verdict(repo)
            self.assertFalse(ok)
            self.assertTrue(why.endswith("unpushed-commits"), why)

    # An independent nested clone has its own object store; the top level's
    # refs cannot reach its commits, so only its own verdict can keep it.
    def test_nested_clone_detached_commit_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            clone = os.path.join(repo, ".paperclip", "worktrees", "clone")
            os.makedirs(os.path.dirname(clone))
            git("clone", "-q", "-b", "main", repo + "-origin.git", clone,
                cwd=root)
            git("config", "user.email", "test@example.com", cwd=clone)
            git("config", "user.name", "Test", cwd=clone)
            git("checkout", "--detach", cwd=clone)
            with open(os.path.join(clone, "only-copy.txt"), "w") as handle:
                handle.write("work that exists nowhere else\n")
            git("add", "-A", cwd=clone)
            git("commit", "-m", "detached work", cwd=clone)
            self.assertEqual(git_verdict(repo),
                             (False, "nested-unpushed-commits"))

    def test_clean_pushed_nested_worktree_ok(self):
        with tempfile.TemporaryDirectory() as root:
            repo, _nested = self._folder_with_worktree(root)
            self.assertEqual(git_verdict(repo), (True, None))

    # Mutant: nested worktree dirt ignored.
    def test_nested_dirty_tree_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo, nested = self._folder_with_worktree(root)
            with open(os.path.join(nested, "wip.txt"), "w") as handle:
                handle.write("unsaved\n")
            self.assertEqual(git_verdict(repo), (False, "nested-dirty"))

    # Mutant: ignored-file check skipped; *.local holds credentials (F3).
    def test_ignored_local_file_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            with open(os.path.join(repo, "host-f0f72aea.example.invalid"), "w") as handle:
                handle.write("placeholder\n")
            self.assertEqual(git_verdict(repo), (False, "ignored-unlisted"))

    def test_ignored_local_file_in_nested_worktree_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo, nested = self._folder_with_worktree(root)
            with open(os.path.join(nested, "host-f0f72aea.example.invalid"), "w") as handle:
                handle.write("placeholder\n")
            self.assertEqual(git_verdict(repo),
                             (False, "nested-ignored-unlisted"))

    def test_regenerable_ignored_dirs_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            repo, nested = self._folder_with_worktree(root)
            for base in (repo, nested):
                deps = os.path.join(base, "node_modules", "dep")
                os.makedirs(deps)
                with open(os.path.join(deps, "index.js"), "w") as handle:
                    handle.write("module.exports = 1;\n")
            self.assertEqual(git_verdict(repo), (True, None))

    # case_2a899bc9 shape: a nested checkout with content and no .git.
    def test_nested_copy_without_git_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            copy = os.path.join(repo, ".claude", "worktrees", "copy", "src")
            os.makedirs(copy)
            with open(os.path.join(copy, "app.ts"), "w") as handle:
                handle.write("export {};\n")
            self.assertEqual(git_verdict(repo), (False, "ignored-unlisted"))

    # Mutant: an unlistable directory is read as clean (fail open).
    def test_unreadable_nested_dir_kept(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads every directory; chmod cannot block it")
        with tempfile.TemporaryDirectory() as root:
            repo, nested = self._folder_with_worktree(root)
            blind = os.path.join(nested, "blind")
            os.makedirs(blind)
            os.chmod(blind, 0)
            try:
                self.assertEqual(git_verdict(repo),
                                 (False, "nested-unreadable"))
            finally:
                os.chmod(blind, 0o700)

    # Mutant: os.walk without onerror; an unreadable subtree looks idle.
    def test_unreadable_subtree_is_not_idle(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads every directory; chmod cannot block it")
        with tempfile.TemporaryDirectory() as root:
            blind = os.path.join(root, "blind")
            os.makedirs(blind)
            backdate(root)
            os.chmod(blind, 0)
            try:
                self.assertIsNone(dir_is_idle(root, 60))
            finally:
                os.chmod(blind, 0o700)

    # Mutant: HEAD and branches only; a detached commit abandoned by checkout
    # lives only in the reflog and reads as pushed.
    def test_abandoned_detached_commit_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            git("checkout", "--detach", cwd=repo)
            with open(os.path.join(repo, "abandoned.txt"), "w") as handle:
                handle.write("abandoned work\n")
            git("add", "-A", cwd=repo)
            git("commit", "-m", "abandoned", cwd=repo)
            git("checkout", "main", cwd=repo)
            self.assertEqual(git_verdict(repo), (False, "unpushed-commits"))

    # Mutant: git's worktree list never cross-checked; a linked worktree
    # outside the folder is never classified.
    def test_worktree_outside_folder_kept(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            git("worktree", "add", "-b", "outside",
                os.path.join(root, "elsewhere"), cwd=repo)
            self.assertEqual(git_verdict(repo),
                             (False, "worktree-unclassified"))


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        patch_empty_holds(self)

    def _run_dir(self, root, run_id, old=True):
        path = os.path.join(root, run_id)
        make_repo(path)
        if old:
            backdate(path)
        return path

    # Mutant: live runs are removable.
    def test_live_run_kept(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            run = run_row("failed", 30, issue="issue-1")
            res = evaluate_run(path, RUN_A, run, {"issue-1"}, set(),
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "live-run")

    # Mutant: scheduled retries are removable.
    def test_retry_kept(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            run = run_row("failed", 30, retry=5)
            res = evaluate_run(path, RUN_A, run, set(), set(),
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "retry-scheduled")

    # Mutant: young failed runs are removable.
    def test_young_failed_kept(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            run = run_row("failed", 2)
            res = evaluate_run(path, RUN_A, run, set(), set(),
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "run-too-recent")

    # Mutant: a retry-scheduled run is trimmed (the retry would resume into
    # a folder whose node_modules just vanished).
    def test_retry_never_trimmed(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            mods = os.path.join(path, "node_modules")
            os.makedirs(mods)
            with open(os.path.join(mods, "d.js"), "w") as handle:
                handle.write("x")
            backdate(path)
            run = run_row("failed", 30, retry=5)
            res = evaluate_run(path, RUN_A, run, set(), set(),
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "retry-scheduled")
            self.assertEqual(res["trim"], [])

    # Mutant: running runs are removable.
    def test_running_kept(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            run = {"id": RUN_A, "status": "running", "issueId": None,
                   "finishedAt": None, "scheduledRetryAt": None}
            res = evaluate_run(path, RUN_A, run, set(), set(),
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "run-not-terminal")

    # Mutant: live process references are ignored.
    def test_proc_ref_kept(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            run = run_row("failed", 30)
            res = evaluate_run(path, RUN_A, run, set(), {path},
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "live-process-ref")

    # Mutant: unreadable /proc reads as unreferenced (fail open).
    def test_proc_unreadable_kept(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            run = run_row("failed", 30)
            res = evaluate_run(path, RUN_A, run, set(), None,
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "proc-unreadable")

    # Mutant: unknown runs are fully removable (the recurrence case must
    # trim, never delete, when the run row aged out).
    def test_unknown_run_trim_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            mods = os.path.join(path, "node_modules")
            os.makedirs(mods)
            with open(os.path.join(mods, "d.js"), "w") as handle:
                handle.write("x")
            backdate(path)
            res = evaluate_run(path, RUN_A, None, set(), set(),
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "run-unknown")
            self.assertTrue(any(p["path"].endswith("node_modules")
                                for p in res["trim"]))

    def test_old_failed_clean_removable(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._run_dir(root, RUN_A)
            run = run_row("failed", 30)
            res = evaluate_run(path, RUN_A, run, set(), set(),
                               time.time(), None, 60)
            self.assertIsNone(res["reason"])

    def test_scan_rejects_names_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "not-a-run"))
            link = os.path.join(root, RUN_B)
            os.symlink(root, link)
            candidates, kept = scan_run_dirs(root)
            self.assertEqual(candidates, [])
            by_id = {s["run_id"]: s["reason"] for s in kept}
            self.assertEqual(by_id["not-a-run"], "unexpected-name")
            self.assertEqual(by_id[RUN_B], "not-a-directory")

    # Mutant: revalidation skipped — a retry between scan and removal deletes.
    def test_revalidate_catches_retry(self):
        api = FakeApi(by_id={RUN_A: run_row("failed", 30, retry=5)})
        self.assertEqual(revalidate("/nonexistent", RUN_A, api, 60),
                         "retry-scheduled")

    def test_build_run_index_caps_lookups(self):
        api = FakeApi(recent=[dict(run_row("failed", 30), id=RUN_A)],
                      by_id={RUN_B: run_row("failed", 30)})
        index = build_run_index(api, {RUN_A, RUN_B}, max_lookups=0)
        self.assertIn(RUN_A, index)
        self.assertNotIn(RUN_B, index)


class IdleTest(unittest.TestCase):
    # Mutant: .git internals reset the idle clock (evaluation's own `git
    # status` keeps every folder alive forever).
    def test_git_internals_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            backdate(repo)
            git("status", cwd=repo)  # touches .git only
            self.assertTrue(dir_is_idle(repo, 60))

    def test_fresh_file_not_idle(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "repo")
            make_repo(repo)
            backdate(repo)
            with open(os.path.join(repo, "fresh.txt"), "w") as handle:
                handle.write("new\n")
            self.assertFalse(dir_is_idle(repo, 60))

    def _repo_with_link(self, root, target):
        repo = os.path.join(root, "repo")
        make_repo(repo)
        backdate(repo)
        link = os.path.join(repo, "linked")
        os.symlink(target, link)
        stamp = time.time() - 7200
        os.utime(link, (stamp, stamp), follow_symlinks=False)
        os.utime(repo, (stamp, stamp))
        return repo

    # Mutant: getmtime follows links, so a dangling link reads as unreadable.
    def test_dangling_symlink_does_not_block_idle(self):
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo_with_link(root, os.path.join(root, "gone", "bin.js"))
            self.assertTrue(dir_is_idle(repo, 60))

    # Mutant: getmtime follows links, so a link to a fresh outside file keeps the folder active.
    def test_symlink_to_fresh_outside_file_does_not_block_idle(self):
        with tempfile.TemporaryDirectory() as root:
            outside = os.path.join(root, "outside.txt")
            with open(outside, "w") as handle:
                handle.write("fresh\n")
            repo = self._repo_with_link(root, outside)
            self.assertTrue(dir_is_idle(repo, 60))


class ProcViewTest(unittest.TestCase):
    def setUp(self):
        patch_empty_holds(self)

    # host_proc_refs under a non-root user: other users' cwd/exe/fd links
    # raise PermissionError. The view is partial BY DESIGN (fail-closed would
    # keep every folder forever, and the complete view needs root running git
    # in agent-writable repos). It must still keep everything it CAN see, and
    # report how blind it was.
    PIDS = ["1", "42", "77", "self", "net"]

    def _view(self, links, fd_denied=(), fd_links=None, listdir_error=None,
              fd_link_denied=()):
        """host_proc_refs() against a fake /proc. links: {path: target|exc};
        fd_denied: pids whose fd dir is unlistable; fd_link_denied:
        (pid, fd) links that are listed but unreadable."""
        real_listdir, real_readlink = os.listdir, os.readlink
        fd_links = fd_links or {}

        def fake_listdir(path):
            if path == "/proc":
                if listdir_error:
                    raise listdir_error
                return list(self.PIDS)
            if path.startswith("/proc/") and path.endswith("/fd"):
                pid = path.split("/")[2]
                if pid in fd_denied:
                    raise PermissionError(13, "denied", path)
                return sorted(n for (p, n) in fd_links if p == pid)
            return real_listdir(path)

        def fake_readlink(path):
            if path in links:
                got = links[path]
                if isinstance(got, Exception):
                    raise got
                return got
            if path.startswith("/proc/"):
                parts = path.split("/")
                if len(parts) == 5 and parts[3] == "fd":
                    if (parts[2], parts[4]) in fd_link_denied:
                        raise PermissionError(13, "denied", path)
                    return fd_links[(parts[2], parts[4])]
                raise FileNotFoundError(2, "gone", path)
            return real_readlink(path)

        with mock.patch.object(tool.os, "listdir", fake_listdir), \
                mock.patch.object(tool.os, "readlink", fake_readlink):
            return tool.host_proc_refs()

    # Mutant: a PermissionError aborts the scan (or returns None), so one
    # root-owned pid blinds the whole gate / makes every folder "unreadable".
    def test_denied_pid_keeps_the_rest_of_the_view(self):
        view = self._view({
            "/proc/1/cwd": PermissionError(13, "denied"),
            "/proc/1/exe": PermissionError(13, "denied"),
            "/proc/42/cwd": "/home/paperclip-operator/run/x",
            "/proc/42/exe": "/usr/bin/python3",
        }, fd_denied={"1"}, fd_links={("42", "3"): "/runs/A/file"})
        self.assertIsNotNone(view)
        self.assertIn("/home/paperclip-operator/run/x", view)
        self.assertIn("/runs/A/file", view)
        self.assertEqual(view.denied_pids, 1)

    # Mutant: the denial count is dropped, so a blind view looks complete.
    # Each path counts a pid once, whichever of cwd/exe/fd-dir denied it.
    def test_each_denial_path_counts_once_per_pid(self):
        denied = PermissionError(13, "denied")
        self.assertEqual(
            self._view({"/proc/42/cwd": denied}).denied_pids, 1)
        self.assertEqual(
            self._view({"/proc/42/exe": denied}).denied_pids, 1)
        self.assertEqual(self._view({}, fd_denied={"42"}).denied_pids, 1)
        # all three denied for one pid, plus a second pid: two, not four.
        both = self._view({"/proc/1/cwd": denied, "/proc/1/exe": denied,
                           "/proc/42/exe": denied},
                          fd_denied={"1", "42"})
        self.assertEqual(both.denied_pids, 2)

    # Mutant: a fd link PermissionError (fd dir listable, one link not)
    # is swallowed without being counted.
    def test_denied_fd_link_is_counted(self):
        links = {"/proc/42/cwd": "/a"}
        fds = {("42", "3"): "/runs/A/f", ("42", "4"): "/runs/A/g"}
        clear = self._view(links, fd_links=fds)
        self.assertEqual(clear.denied_pids, 0)
        self.assertIn("/runs/A/f", clear)
        partial = self._view(links, fd_links=fds,
                             fd_link_denied={("42", "4")})
        self.assertEqual(partial.denied_pids, 1)
        self.assertIn("/runs/A/f", partial)
        self.assertNotIn("/runs/A/g", partial)

    # Mutant: a pid that exited mid-scan (ENOENT) is counted as denied, so
    # the report cries blind on every busy host.
    def test_exited_pid_is_not_denial(self):
        view = self._view({"/proc/42/cwd": FileNotFoundError(2, "gone"),
                           "/proc/42/exe": ProcessLookupError(3, "gone")})
        self.assertEqual(view.denied_pids, 0)

    # Mutant: an unlistable /proc yields an empty set (nothing referenced).
    def test_unlistable_proc_is_none(self):
        self.assertIsNone(self._view({}, listdir_error=OSError(5, "io")))

    # Mutant: a partial view disables the reference gate for what it CAN see.
    def test_visible_reference_still_keeps_with_denied_pids(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, RUN_A)
            make_repo(path)
            backdate(path)
            view = tool.ProcRefs([os.path.join(path, "sub")])
            view.denied_pids = 5
            res = evaluate_run(path, RUN_A, run_row("failed", 30), set(),
                               view, time.time(), None, 60)
            self.assertEqual(res["reason"], "live-process-ref")

    # Pins the ACCEPTED residual (RUNBOOK section 6): a denied pid is not
    # evidence of a reference, so an otherwise-proven folder stays removable
    # and the blindness is only reported. If this is ever changed to fail
    # closed, the pruner can never act as a non-root user.
    def test_denied_pids_alone_do_not_keep_a_proven_folder(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, RUN_A)
            make_repo(path)
            backdate(path)
            view = tool.ProcRefs(["/somewhere/else"])
            view.denied_pids = 5
            res = evaluate_run(path, RUN_A, run_row("failed", 30), set(),
                               view, time.time(), None, 60)
            self.assertIsNone(res["reason"])

    # Mutant: the summary hides how blind the scan was.
    def test_summary_reports_proc_view(self):
        with tempfile.TemporaryDirectory() as root:
            view = tool.ProcRefs()
            view.denied_pids = 7
            with mock.patch.object(tool, "host_proc_refs",
                                   return_value=view):
                summary = tool.run_tool(
                    tool.parse_argv(["--runs-root", root]), FakeApi(), {})
            self.assertEqual(summary["proc_view"],
                             {"readable": True, "denied_pids": 7})
            with mock.patch.object(tool, "host_proc_refs",
                                   return_value=None):
                summary = tool.run_tool(
                    tool.parse_argv(["--runs-root", root]), FakeApi(), {})
            self.assertEqual(summary["proc_view"],
                             {"readable": False, "denied_pids": 0})


class ApplyTest(unittest.TestCase):
    def setUp(self):
        patch_empty_holds(self)

    # End-to-end: dry-run removes nothing, --apply removes exactly the
    # proven folder and trims the regenerable subtree of the kept one.
    def test_dry_run_then_apply(self):
        with tempfile.TemporaryDirectory() as root:
            old_failed = os.path.join(root, RUN_A)
            make_repo(old_failed)
            backdate(old_failed)
            young_failed = os.path.join(
                root, "00000000-0000-4000-8000-000000000125")
            make_repo(young_failed)
            mods = os.path.join(young_failed, "node_modules")
            os.makedirs(mods)
            with open(os.path.join(mods, "d.js"), "w") as handle:
                handle.write("x")
            backdate(young_failed)
            api = FakeApi(
                by_id={RUN_A: run_row("failed", 30),
                       "00000000-0000-4000-8000-000000000125":
                           run_row("failed", 2)})
            opts = tool.parse_argv(["--runs-root", root])
            summary = tool.run_tool(opts, api, {})
            self.assertEqual(summary["removable"], 1)
            self.assertTrue(os.path.isdir(old_failed))
            opts = tool.parse_argv(["--runs-root", root, "--apply"])
            summary = tool.run_tool(opts, api, {})
            self.assertEqual(summary["apply"]["removed"], 1)
            self.assertFalse(os.path.exists(old_failed))
            self.assertTrue(os.path.isdir(young_failed))
            self.assertFalse(os.path.exists(mods))
            self.assertEqual(summary["apply"]["trimmed"], 1)

    # Mutant: blind --apply without API env proceeds.
    def test_blind_apply_refused(self):
        with self.assertRaises(SafetyRefusal):
            tool.main(argv=["--apply"], api=None, env={})


class ApplyRevalidationTest(unittest.TestCase):
    """The scan-time verdict is never the last word: every removal AND every
    trim is re-proven against the live control plane first."""

    def setUp(self):
        patch_empty_holds(self)

    def _old_failed_with_mods(self, root):
        path = os.path.join(root, RUN_A)
        make_repo(path)
        mods = os.path.join(path, "node_modules")
        os.makedirs(mods)
        with open(os.path.join(mods, "d.js"), "w") as handle:
            handle.write("x")
        backdate(path)
        return path, mods

    def _opts(self, root, *extra):
        return tool.parse_argv(["--runs-root", root, "--apply"] + list(extra))

    # Mutant: the trim loop ignores revalidation, so a retry scheduled between
    # scan and apply still loses its node_modules (the retry resumes into a
    # folder that vanished under it).
    def test_trim_revalidated_retry_between_scan_and_apply(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            self.assertIsNone(res["reason"])
            self.assertTrue(res["trim"])
            api.by_id[RUN_A] = run_row("failed", 30, retry=5)
            report = tool.apply_plan([res], api, set(), self._opts(root))
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["trimmed"], 0)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.isdir(mods))
            self.assertEqual(report["skipped"].get("retry-scheduled"), 1)
            self.assertEqual(report["skipped"].get("trim-retry-scheduled"), 1)

    # Mutant: a run that became live between scan and apply is still trimmed.
    def test_trim_skipped_when_live_run_appears(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            api.live = [{"issueId": "issue-1"}]
            report = tool.apply_plan([res], api, set(),
                                     self._opts(root, "--max-remove", "0"))
            self.assertEqual(report["trimmed"], 0)
            self.assertTrue(os.path.isdir(mods))
            self.assertEqual(report["skipped"].get("trim-live-run-appeared"),
                             1)

    # Mutant: a trim reread failure falls through and trims anyway.
    def test_trim_skipped_when_run_reread_fails(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)

            def boom(url):
                raise RuntimeError("500 server error")
            api._get = boom
            report = tool.apply_plan([res], api, set(),
                                     self._opts(root, "--max-remove", "0"))
            self.assertEqual(report["trimmed"], 0)
            self.assertTrue(os.path.isdir(mods))

    # Mutant: an unknown (aged-out) run is never trimmed — the recurrence
    # case this tool exists for would leak disk forever.
    def test_unknown_run_trim_proceeds(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            api = FakeApi()  # row 404s now and at scan time
            res = evaluate_run(path, RUN_A, None, set(), set(),
                               time.time(), None, 60)
            report = tool.apply_plan([res], api, set(), self._opts(root))
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["trimmed"], 1)
            self.assertFalse(os.path.exists(mods))
            self.assertTrue(os.path.isdir(path))

    # Mutant: the ignored-path proof is only taken at scan time, so content
    # that became tracked since is trimmed.
    def test_trim_reproves_git_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            git("add", "-f", "node_modules/d.js", cwd=path)
            git("commit", "-m", "track it", cwd=path)
            report = tool.apply_plan([res], api, set(),
                                     self._opts(root, "--max-remove", "0"))
            self.assertEqual(report["trimmed"], 0)
            self.assertTrue(os.path.isfile(os.path.join(mods, "d.js")))
            self.assertEqual(
                report["skipped"].get("trim-not-ignored-anymore"), 1)

    # Mutant: revalidation hard-codes the 24h bar, so lowering
    # --min-age-hours selects folders at scan time that --apply then always
    # skips ("run-too-recent") and nothing is ever removed.
    def test_min_age_override_honoured_at_apply(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, RUN_A)
            make_repo(path)
            backdate(path)
            api = FakeApi(by_id={RUN_A: run_row("failed", 2)})
            summary = tool.run_tool(
                tool.parse_argv(["--runs-root", root, "--apply",
                                 "--min-age-hours", "1"]), api, {})
            self.assertEqual(summary["apply"]["removed"], 1)
            self.assertFalse(os.path.exists(path))

    # The override only LOWERS the bar for runs that pass it; it never lets a
    # run younger than the override through.
    def test_min_age_override_still_bars_younger_runs(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, RUN_A)
            make_repo(path)
            backdate(path)
            api = FakeApi(by_id={RUN_A: run_row("failed", 0.5)})
            summary = tool.run_tool(
                tool.parse_argv(["--runs-root", root, "--apply",
                                 "--min-age-hours", "1"]), api, {})
            self.assertEqual(summary["apply"]["removed"], 0)
            self.assertTrue(os.path.isdir(path))

    # RUNBOOK contract: --max-remove 0 --max-trim 0 under --apply acts on
    # nothing. (--max-remove 0 alone still trims; the runbook says so.)
    def test_zero_caps_are_observe_only(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            summary = tool.run_tool(
                tool.parse_argv(["--runs-root", root, "--apply",
                                 "--max-remove", "0", "--max-trim", "0"]),
                api, {})
            self.assertEqual(summary["apply"]["removed"], 0)
            self.assertEqual(summary["apply"]["trimmed"], 0)
            self.assertTrue(os.path.isdir(mods))

    # Mutant: the trim cap is checked once per run folder, not per path.
    def test_max_trim_counts_paths(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            target = os.path.join(path, "target")
            os.makedirs(target)
            with open(os.path.join(target, "o"), "w") as handle:
                handle.write("x")
            backdate(path)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            self.assertEqual(len(res["trim"]), 2)
            report = tool.apply_plan([res], api, set(),
                                     self._opts(root, "--max-remove", "0",
                                                "--max-trim", "1"))
            self.assertEqual(report["trimmed"], 1)
            self.assertEqual(report["skipped"].get("over-max-trim"), 1)

    # Mutant: a live run on the same issue is hands-off for removal but its
    # folder is still trimmed (the evaluation's own comment promised no trim).
    def test_live_run_folder_never_trimmed(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            run = run_row("failed", 30, issue="issue-1")
            res = evaluate_run(path, RUN_A, run, {"issue-1"}, set(),
                               time.time(), None, 60)
            self.assertEqual(res["reason"], "live-run")
            self.assertEqual(res["trim"], [])


class FootprintTest(unittest.TestCase):
    """runs_total_gib is the monitor's figure: the whole footprint, kept
    folders included — not only what could be reclaimed this pass."""

    def setUp(self):
        patch_empty_holds(self)

    # Mutant: footprint sums removable folders only, so dirty/unpushed/
    # unverifiable folders (the ones that actually fill /home) never count
    # and the monitor stays green while disk fills.
    def test_kept_folders_count_toward_footprint(self):
        with tempfile.TemporaryDirectory() as root:
            dirty = os.path.join(root, RUN_A)
            make_repo(dirty)
            with open(os.path.join(dirty, "big.bin"), "wb") as handle:
                handle.write(b"\0" * 3_000_000)
            backdate(dirty)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            summary = tool.run_tool(tool.parse_argv(["--runs-root", root]),
                                    api, {})
            self.assertEqual(summary["kept_by_reason"].get("dirty"), 1)
            self.assertEqual(summary["removable_bytes"], 0)
            self.assertGreaterEqual(summary["runs_total_bytes"], 3_000_000)
            self.assertIn("runs_total_gib", summary)

    def test_entry_bytes_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            big = os.path.join(root, "big")
            os.makedirs(big)
            with open(os.path.join(big, "f"), "wb") as handle:
                handle.write(b"\0" * 100_000)
            link = os.path.join(root, "link")
            os.symlink(big, link)
            self.assertEqual(tool.entry_bytes(link), 0)
            self.assertGreaterEqual(tool.entry_bytes(big), 100_000)


class CustodyHoldTest(unittest.TestCase):
    """Recovery-custody holds shared with the regen strip (RUNBOOK-regen.md).

    Mutants: the held check removed (a held run is removed or trimmed), the
    plan-time hold read removed (a malformed hold file still prunes), the
    per-action hold re-read removed (a hold added between scan and apply is
    ignored), an unreadable re-read falling through to deletion.
    """

    def _old_failed_with_mods(self, root):
        path = os.path.join(root, RUN_A)
        make_repo(path)
        mods = os.path.join(path, "node_modules")
        os.makedirs(mods)
        with open(os.path.join(mods, "d.js"), "w") as handle:
            handle.write("x")
        backdate(path)
        return path, mods

    # Mutant: held runs are removable.
    def test_held_run_kept_without_removal_or_trim(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, RUN_A)
            make_repo(path)
            backdate(path)
            res = evaluate_run(path, RUN_A, run_row("failed", 30), set(),
                               set(), time.time(), None, 60, {RUN_A})
            self.assertEqual(res["reason"], "custody-hold")
            self.assertEqual(res["trim"], [])

    # Mutant: a held run whose row aged out is still trimmed (unknown runs
    # trim by default, so the hold must win before the unknown branch).
    def test_held_unknown_run_never_trimmed(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, RUN_A)
            make_repo(path)
            backdate(path)
            res = evaluate_run(path, RUN_A, None, set(), set(),
                               time.time(), None, 60, {RUN_A})
            self.assertEqual(res["reason"], "custody-hold")
            self.assertEqual(res["trim"], [])

    # Mutant: the plan-time hold read removed (a missing file still prunes).
    def test_unreadable_hold_fails_whole_pass_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(tool, "KEEP_PATH",
                                   os.path.join(root, "missing.txt")):
                with self.assertRaises(PruneError):
                    tool.run_tool(tool.parse_argv(["--runs-root", root]),
                                  FakeApi(), {})

    # Mutant: malformed hold lines are skipped instead of failing closed.
    def test_malformed_hold_fails_closed_before_any_removal(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, RUN_A)
            make_repo(path)
            backdate(path)
            patch_empty_holds(self, "%s\nnot-a-run-id\n" % RUN_A)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            with self.assertRaises(PruneError):
                tool.run_tool(
                    tool.parse_argv(["--runs-root", root, "--apply"]),
                    api, {})
            self.assertTrue(os.path.isdir(path))

    # Mutant: an empty hold file reads as "no holds" (a truncated editor
    # write releases every held run).
    def test_empty_hold_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            patch_empty_holds(self, "")
            with self.assertRaises(PruneError):
                tool.run_tool(tool.parse_argv(["--runs-root", root]),
                              FakeApi(), {})

    # Mutant: annotated holds are rejected or dropped from the protected set.
    def test_legacy_annotated_holds_preserve_exact_set_and_both_runs(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, rid) for rid in (RUN_A, RUN_B)]
            for path in paths:
                make_repo(path)
                os.makedirs(os.path.join(path, "node_modules"))
                backdate(path)
            text = "# recovery copies\n%s # first copy\n  %s\t# second # note\n" % (RUN_A, RUN_B)
            keep = patch_empty_holds(self, text)
            legacy_set = {line.split("#", 1)[0].strip() for line in text.splitlines()
                          if line.split("#", 1)[0].strip()}
            self.assertEqual(legacy_set, {RUN_A, RUN_B})
            self.assertEqual(tool.read_keep(), legacy_set)
            api = FakeApi(by_id={rid: dict(run_row("failed", 30), id=rid)
                                 for rid in (RUN_A, RUN_B)})
            for flags in ([], ["--apply"]):
                with self.subTest(flags=flags):
                    report = tool.run_tool(
                        tool.parse_argv(["--runs-root", root] + flags), api, {})
                    self.assertEqual(report["custody_held"], 2)
                    self.assertEqual(report["removable"], 0)
                    self.assertEqual(report["trim_planned"], 0)
                    self.assertEqual(report["kept_by_reason"]["custody-hold"], 2)
                    if flags:
                        self.assertEqual(report["apply"]["removed"], 0)
                        self.assertEqual(report["apply"]["trimmed"], 0)
                    else:
                        self.assertIsNone(report["apply"])
                    for path in paths:
                        self.assertTrue(os.path.isdir(path))
                        self.assertTrue(os.path.isdir(os.path.join(path, "node_modules")))
            with open(keep) as handle:
                self.assertEqual(handle.read(), text)

    # Mutants: hold UUIDs accept uppercase, or are silently lowercased.
    def test_uppercase_hold_fails_closed_before_removal_or_trim(self):
        for suffix in ("", " # recovery copy"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, RUN_B)
                make_repo(path)
                mods = os.path.join(path, "node_modules")
                os.makedirs(mods)
                marker = os.path.join(mods, "d.js")
                with open(marker, "w") as handle:
                    handle.write("custody fixture")
                backdate(path)
                patch_empty_holds(self, RUN_B.upper() + suffix + "\n")
                api = FakeApi(by_id={RUN_B: dict(run_row("failed", 30), id=RUN_B)})
                with self.assertRaises(PruneError):
                    tool.read_keep()
                for flags in ([], ["--apply"]):
                    with self.subTest(flags=flags):
                        with self.assertRaises(PruneError):
                            tool.run_tool(tool.parse_argv(["--runs-root", root] + flags),
                                          api, {})
                        self.assertTrue(os.path.isdir(path))
                        self.assertTrue(os.path.isdir(mods))
                        with open(marker) as handle:
                            self.assertEqual(handle.read(), "custody fixture")

    # Mutant: a malformed prefix before # is silently ignored.
    def test_malformed_prefix_before_annotation_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            keep = patch_empty_holds(self)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            for entry in ("not-a-run-id", RUN_A + "x", RUN_A + " extra", RUN_A + " " + RUN_B):
                with self.subTest(entry=entry):
                    with open(keep, "w") as handle:
                        handle.write("%s # valid hold\n%s # recovery copy\n" % (RUN_B, entry))
                    with self.assertRaises(PruneError):
                        tool.run_tool(tool.parse_argv(["--runs-root", root, "--apply"]),
                                      api, {})
                    self.assertTrue(os.path.isdir(path))
                    self.assertTrue(os.path.isdir(mods))

    def test_annotated_hold_added_between_scan_and_apply_skips_both(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            keep = patch_empty_holds(self)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            self.assertIsNone(res["reason"])
            self.assertTrue(res["trim"])
            with open(keep, "w") as handle:
                handle.write("%s # recovery copy\n" % RUN_A)
            report = tool.apply_plan(
                [res], api, set(),
                tool.parse_argv(["--runs-root", root, "--apply"]))
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["trimmed"], 0)
            self.assertEqual(report["skipped"].get("custody-hold"), 1)
            self.assertEqual(report["skipped"].get("trim-custody-hold"), 1)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.isdir(mods))

    # Mutant: the per-action hold re-read removed (a hold added between
    # scan and apply still loses the whole folder and its node_modules).
    def test_hold_added_between_scan_and_apply_skips_both(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            keep = patch_empty_holds(self)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            self.assertIsNone(res["reason"])
            self.assertTrue(res["trim"])
            with open(keep, "w") as handle:
                handle.write("%s\n" % RUN_A)
            report = tool.apply_plan(
                [res], api, set(),
                tool.parse_argv(["--runs-root", root, "--apply"]))
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["trimmed"], 0)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.isdir(mods))
            self.assertEqual(report["skipped"].get("custody-hold"), 1)
            self.assertEqual(report["skipped"].get("trim-custody-hold"), 1)

    # Mutant: an unreadable hold re-read falls through and deletes anyway.
    def test_unreadable_hold_between_scan_and_apply_skips(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            keep = patch_empty_holds(self)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            self.assertIsNone(res["reason"])
            os.unlink(keep)
            report = tool.apply_plan(
                [res], api, set(),
                tool.parse_argv(["--runs-root", root, "--apply"]))
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["trimmed"], 0)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.isdir(mods))
            self.assertEqual(
                report["skipped"].get("custody-hold-reread-failed"), 1)
            self.assertEqual(
                report["skipped"].get("trim-custody-hold-reread-failed"), 1)

    # Mutant: a malformed hold re-read falls through and deletes anyway.
    def test_malformed_hold_between_scan_and_apply_skips(self):
        with tempfile.TemporaryDirectory() as root:
            path, mods = self._old_failed_with_mods(root)
            keep = patch_empty_holds(self)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            res = evaluate_run(path, RUN_A, api.by_id[RUN_A], set(), set(),
                               time.time(), None, 60)
            self.assertIsNone(res["reason"])
            with open(keep, "w") as handle:
                handle.write("garbage\n")
            report = tool.apply_plan(
                [res], api, set(),
                tool.parse_argv(["--runs-root", root, "--apply"]))
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["trimmed"], 0)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.isdir(mods))

    # Mutant: revalidation skips the hold re-read (direct pin).
    def test_revalidate_refuses_held_run(self):
        with tempfile.TemporaryDirectory() as root:
            patch_empty_holds(self, "%s\n" % RUN_A)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            self.assertEqual(revalidate("/nonexistent", RUN_A, api, 60),
                             "custody-hold")


class ManifestAtomicWriteTest(unittest.TestCase):
    def setUp(self):
        patch_empty_holds(self)

    def test_manifest_is_parseable_and_replace_failure_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as out:
            stamp = "20261009T000000Z"
            path = os.path.join(out, "operator-run-prune-%s.json" % stamp)
            payload = {"summary": {"removed": 1}, "results": []}
            self.assertEqual(tool.write_manifest(out, payload, stamp), path)
            with open(path) as handle:
                self.assertEqual(json.load(handle), payload)
            previous = b"previous manifest bytes\\n"
            with open(path, "wb") as handle:
                handle.write(previous)
            with mock.patch.object(tool.os, "replace",
                                   side_effect=OSError(28, "No space")):
                with self.assertRaises(OSError):
                    tool.write_manifest(out, payload, stamp)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), previous)
            self.assertEqual(os.listdir(out), [os.path.basename(path)])

    def test_manifest_failure_prints_summary_before_error(self):
        fixed = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(fixed))
        with tempfile.TemporaryDirectory() as root:
            old_failed = os.path.join(root, RUN_A)
            make_repo(old_failed)
            backdate(old_failed)
            api = FakeApi(by_id={RUN_A: run_row("failed", 30)})
            with tempfile.TemporaryDirectory() as manifests:
                path = os.path.join(
                    manifests, "operator-run-prune-%s.json" % stamp)
                previous = b"previous manifest bytes\n"
                with open(path, "wb") as handle:
                    handle.write(previous)
                opts = tool.parse_argv(
                    ["--runs-root", root, "--apply",
                     "--manifest-dir", manifests])
                stdout = io.StringIO()
                with mock.patch.object(
                        tool.os, "replace",
                        side_effect=OSError(errno.ENOSPC, "No space")), \
                        contextlib.redirect_stdout(stdout):
                    with self.assertRaises(PruneError) as raised:
                        tool.run_tool(opts, api, {}, clock=lambda: fixed)
                summary = json.loads(stdout.getvalue())
                self.assertIn("took_s", summary)
                self.assertIn(path, str(raised.exception))
                self.assertIn("errno=%d" % errno.ENOSPC,
                              str(raised.exception))
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), previous)
                self.assertEqual(os.listdir(manifests),
                                 [os.path.basename(path)])


if __name__ == "__main__":
    unittest.main()
