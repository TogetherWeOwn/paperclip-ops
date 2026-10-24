#!/usr/bin/env python3
# ===========================================================================
# test_project_worktree_regen_prune.py — offline tests for
# project_worktree_regen_prune.py (case_60e07cc2).
#
# Written to FAIL a plausible-but-wrong implementation, not to confirm the one
# that exists. Every section names the mutant it kills. This tool DELETES
# directories on a production host, so the tests that matter most are the ones
# proving it refuses: a busy sibling must not protect an idle worktree (and a
# busy project must not either — that was the host bug), an unknown card, an
# open card with no run history, a tracked or non-ignored dir, a symlink, a
# fresh tree, and a card reopened between scan and removal.
#
# Deterministic: no network, no credentials. Git-touching paths run against
# throwaway local repos (a bare origin plus clones and real `git worktree`
# checkouts); the API boundary is a fake. Real `rm -rf` is exercised against
# fixture regen dirs, never mocked.
#
#   python3 ops/case_60e07cc2/test_project_worktree_regen_prune.py
# ===========================================================================
import datetime
import inspect
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

import project_worktree_regen_prune as tool  # noqa: E402
from project_worktree_regen_prune import (  # noqa: E402
    PruneError,
    SafetyRefusal,
    apply_removals,
    card_hours_since_run,
    card_idents,
    enumerate_candidates,
    evaluate_candidate,
    find_git_dir,
    order_for_removal,
    parse_argv,
    recent_activity,
    revalidate,
    run_tool,
    stale_gate,
    to_host,
    worktree_busy,
)

HOUR = 3600
NOW = time.time()


def iso(hours_ago, now=NOW):
    stamp = now - hours_ago * HOUR
    return datetime.datetime.fromtimestamp(
        stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def git(*args, cwd):
    # Hermetic identity: an empty GIT_AUTHOR_* / GIT_COMMITTER_* environment
    # (as some harnesses export) overrides repo-local config with an empty
    # ident and breaks every fixture commit, so fall back explicitly.
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        if not env[key]:
            env[key] = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"}[key]
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


def make_old(root, seconds_ago=8 * HOUR, include_git=False):
    """Backdate the tree so the 6h idle gate sees idle.

    Every visited dir is backdated (a fresh intermediate dir is activity).
    .git internals are backdated only with include_git=True: a fresh clone's
    .git is genuinely fresh, so clone-based tests opt in to model "idle for
    days" while worktree tests keep the default (their git writes land in
    the main repo, outside the candidate).
    """
    stamp = time.time() - seconds_ago
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames and not include_git:
            dirnames.remove(".git")
        for name in filenames:
            try:
                os.utime(os.path.join(dirpath, name), (stamp, stamp))
            except OSError:
                pass
        try:
            os.utime(dirpath, (stamp, stamp))
        except OSError:
            pass
    try:
        os.utime(root, (stamp, stamp))
    except OSError:
        pass


def issue_row(ident, status="done", **over):
    row = {"id": "id-" + ident, "identifier": ident, "status": status,
           "completedAt": iso(48), "cancelledAt": None,
           "updatedAt": iso(48)}
    row.update(over)
    return row


def run_row(issue_id, hours_ago=1):
    return {"id": "run-%s-%s" % (issue_id, hours_ago),
            "status": "succeeded",
            "created_at": iso(hours_ago),
            "contextSnapshot": {"issueId": issue_id}}


class FakeApi:
    """Board fake: full issue list, heartbeat window, live runs, re-reads."""

    def __init__(self, issues=(), heartbeat=(), live=(), reread=None,
                 dead=False):
        self.issues = list(issues)
        self.heartbeat = list(heartbeat)
        self.live = list(live)
        self.reread = dict(reread) if reread is not None else None
        self.dead = dead

    def _boom(self):
        if self.dead:
            raise OSError("board unreachable")

    def all_issues(self):
        self._boom()
        return {row["id"]: row for row in self.issues}

    def heartbeat_runs(self):
        self._boom()
        return self.heartbeat

    def live_runs(self):
        self._boom()
        return self.live

    def issue_status(self, issue_id):
        self._boom()
        if self.reread is not None:
            return self.reread[issue_id]
        for row in self.issues:
            if row["id"] == issue_id:
                return row["status"]
        raise KeyError(issue_id)


def make_ctx(api, now=NOW, proc_refs=frozenset(), run_paths=frozenset(),
             run_issue_ids=frozenset()):
    issues = api.all_issues()
    return {"by_ident": {r["identifier"]: r for r in issues.values()},
            "run_paths": set(run_paths),
            "run_issue_ids": set(run_issue_ids),
            "proc_refs": set(proc_refs),
            "heartbeat": api.heartbeat_runs(),
            "now": now}


class ProjectFixture:
    """A projects-root layout with real git repos: a primary checkout, two
    nested worktrees of one project, a side checkout and an upgrade dir."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.root = os.path.join(base, "projects")
        self.origin = os.path.join(base, "origin.git")
        self.primary = os.path.join(self.root, "co", "proj", "repo")
        os.makedirs(os.path.dirname(self.primary))
        git("init", "-q", "--bare", self.origin, cwd=base)
        git("clone", "-q", self.origin, self.primary, cwd=base)
        for key, val in (("user.email", "t@t"), ("user.name", "t"),
                         ("commit.gpgsign", "false")):
            git("config", key, val, cwd=self.primary)
        with open(os.path.join(self.primary, ".gitignore"), "w") as handle:
            handle.write("node_modules\ntarget\n.next\n.turbo\n"
                         ".pnpm-store\n")
        with open(os.path.join(self.primary, "app.py"), "w") as handle:
            handle.write("print(1)\n")
        git("add", ".", cwd=self.primary)
        git("commit", "-qm", "base", cwd=self.primary)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.primary)
        git("fetch", "-q", "origin", cwd=self.primary)
        self.wtdir = os.path.join(self.primary, ".paperclip", "worktrees")
        os.makedirs(self.wtdir)
        self.worktrees = {}
        for name in ("TOG-" "99701-alpha", "TOG-" "99702-beta"):
            path = os.path.join(self.wtdir, name)
            git("worktree", "add", "-q", "-b", name, path, "origin/main",
                cwd=self.primary)
            self.worktrees[name] = os.path.realpath(path)
        self.side = os.path.join(self.root, "co", "proj", "TOG-" "99703-side")
        git("clone", "-q", self.origin, self.side, cwd=base)
        for key, val in (("user.email", "t@t"), ("user.name", "t"),
                         ("commit.gpgsign", "false")):
            git("config", key, val, cwd=self.side)
        self.upgrade = os.path.join(self.root, "co", "proj",
                                    "upgrade-widget")
        git("clone", "-q", self.origin, self.upgrade, cwd=base)
        for key, val in (("user.email", "t@t"), ("user.name", "t"),
                         ("commit.gpgsign", "false")):
            git("config", key, val, cwd=self.upgrade)
        self.api = None

    def close(self):
        self.tmp.cleanup()

    def add_regen(self, candidate, name="node_modules", size=3):
        """An ignored, untracked regen dir with real bytes, backdated idle."""
        target = os.path.join(candidate, name)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "blob"), "wb") as handle:
            handle.write(b"\0" * size)
        make_old(candidate)
        return target

    def evaluate(self, candidate, issues, **kw):
        api = FakeApi(issues=issues,
                      heartbeat=kw.pop("heartbeat", ()),
                      live=kw.pop("live", ()))
        ctx = make_ctx(api, proc_refs=kw.pop("proc_refs", frozenset()),
                       run_paths=kw.pop("run_paths", frozenset()),
                       run_issue_ids=kw.pop("run_issue_ids", frozenset()))
        return evaluate_candidate(ctx, candidate, 24, 6)


def done_issues(*idents):
    return [issue_row(i) for i in idents]


# ---------------------------------------------------------------------------
class StaleGateTests(unittest.TestCase):
    # Kills: treating every open card as stale, treating unknown cards as
    # stale (host parity keeps them), or dropping the upgrade- carve-out.

    def test_done_and_cancelled_are_stale(self):
        self.assertEqual(stale_gate("done", None, 24, True, "TOG-" "99601"),
                         (True, None))
        self.assertEqual(stale_gate("cancelled", 0, 24, True, "TOG-" "99601"),
                         (True, None))

    def test_open_with_recent_run_is_active(self):
        self.assertEqual(stale_gate("in_progress", 1, 24, True, "TOG-" "99601"),
                         (False, "card-active"))

    def test_open_with_old_run_is_stale(self):
        self.assertEqual(stale_gate("todo", 25, 24, True, "TOG-" "99601"),
                         (True, None))

    def test_exactly_the_bar_is_stale(self):
        self.assertEqual(stale_gate("todo", 24, 24, True, "TOG-" "99601"),
                         (True, None))

    def test_open_with_no_run_in_window_keeps(self):
        # STRICTER than the host script: an unseen history is unverifiable.
        self.assertEqual(stale_gate("todo", None, 24, True, "TOG-" "99601"),
                         (False, "run-history-unknown"))

    def test_unknown_tog_keeps(self):
        self.assertEqual(stale_gate(None, None, 24, False, "TOG-" "99799-x"),
                         (False, "card-unknown"))

    def test_upgrade_without_card_is_stale(self):
        self.assertEqual(stale_gate(None, None, 24, False, "upgrade-x"),
                         (True, None))

    def test_upgrade_prefix_needs_the_prefix(self):
        self.assertEqual(stale_gate(None, None, 24, False, "notupgrade"),
                         (False, "card-unknown"))


class CardIdentTests(unittest.TestCase):
    # Kills: zero-padding slips (TOG-" "099604 vs TOG-" "99604) in the card join.

    def test_exact_digits_first_then_stripped(self):
        self.assertEqual(card_idents("TOG-" "099604-x"), ["TOG-" "099604", "TOG-" "99604"])
        self.assertEqual(card_idents("tog_" "99605"), ["TOG-" "99605"])

    def test_no_tog_is_empty(self):
        self.assertEqual(card_idents("upgrade-x"), [])
        self.assertEqual(card_idents("repo"), [])

    def test_join_prefers_exact(self):
        fx = ProjectFixture()
        try:
            card = issue_row("TOG-" "099604")
            res = fx.evaluate(fx.worktrees["TOG-" "99701-alpha"],
                              [card, issue_row("TOG-" "99604")],
                              heartbeat=[run_row("id-TOG-" "99604", 1)])
            # Neither card matches this worktree: unknown keeps it.
            self.assertEqual(res["reason"], "card-unknown")
        finally:
            fx.close()


class PerWorktreeLivenessTests(unittest.TestCase):
    # Kills: the host whole-project skip (busy_project) in any form — a cwd
    # anywhere else in the project must not protect THIS worktree, and a cwd
    # inside this worktree must not protect its sibling either.

    def test_busy_sibling_does_not_protect_idle_worktree(self):
        fx = ProjectFixture()
        try:
            busy = fx.worktrees["TOG-" "99701-alpha"]
            idle = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(idle)
            make_old(busy)
            issues = done_issues("TOG-" "99701", "TOG-" "99702")
            busy_res = fx.evaluate(busy, issues,
                                   proc_refs={os.path.join(busy, "app.py")})
            idle_res = fx.evaluate(idle, issues,
                                   proc_refs={os.path.join(busy, "app.py")})
            self.assertEqual(busy_res["reason"], "live-process")
            self.assertIsNone(idle_res["reason"])
            self.assertEqual(len(idle_res["caches"]), 1)
        finally:
            fx.close()

    def test_project_cwd_outside_any_candidate_protects_nothing(self):
        # The exact host failure: an SSH upload into the primary checkout
        # skipped every stale worktree in the project. Here it skips none.
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(idle)
            issues = done_issues("TOG-" "99702")
            res = fx.evaluate(idle, issues,
                              proc_refs={os.path.join(fx.primary, "app.py")})
            self.assertIsNone(res["reason"])
            self.assertEqual(len(res["caches"]), 1)
        finally:
            fx.close()

    def test_prefix_sibling_is_not_a_match(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(idle)
            issues = done_issues("TOG-" "99702")
            # A ref to TOG-" "99604 (prefix of nothing here) must not touch
            # TOG-" "99702; and a TOG-" "99702 ref must not touch a TOG-" "99702x name.
            res = fx.evaluate(idle, issues, run_paths={
                os.path.join(fx.primary, ".paperclip", "worktrees",
                             "TOG-" "99604")})
            self.assertIsNone(res["reason"])
        finally:
            fx.close()

    def test_live_run_ref_keeps_this_worktree_only(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(idle)
            issues = done_issues("TOG-" "99702")
            res = fx.evaluate(idle, issues,
                              run_issue_ids={"id-TOG-" "99702"})
            self.assertEqual(res["reason"], "live-run-ref")
        finally:
            fx.close()

    def test_run_snapshot_path_under_candidate_keeps(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(idle)
            issues = done_issues("TOG-" "99702")
            res = fx.evaluate(
                idle, issues,
                run_paths={os.path.join(idle, "node_modules", "blob")})
            self.assertEqual(res["reason"], "live-run-ref")
        finally:
            fx.close()

    def test_cardless_upgrade_checkout_consults_run_paths(self):
        # Warning pin: the live-run gate only ran for linked cards, so a
        # run starting under a cardless upgrade- checkout between scan and
        # removal was missed at both gates.
        fx = ProjectFixture()
        try:
            fx.add_regen(fx.upgrade)
            res = fx.evaluate(
                fx.upgrade, [],
                run_paths={os.path.join(fx.upgrade, "node_modules",
                                        "blob")})
            self.assertEqual(res["reason"], "live-run-ref")
            self.assertEqual(res["caches"], [])
        finally:
            fx.close()

    def test_source_has_no_whole_project_skip(self):
        # Mutation pin: restoring the host `busy_project` / proj_dir gate
        # must fail this suite. The docstring may discuss the old flag; the
        # CODE may not compute one.
        source = inspect.getsource(tool)
        code = source.split('"""', 2)[-1] if source.count('"""') >= 2 else source
        # Strip comments as well: only live code is pinned.
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
        self.assertNotIn("busy_project", code)
        self.assertNotIn("busy-project", code)
        self.assertIn("worktree_busy(candidate", source)


class ToHostTests(unittest.TestCase):
    # Kills: container/host prefix confusion in the liveness join.

    def test_container_paths_map(self):
        self.assertEqual(to_host("/paperclip/a/b"),
                         tool.HOST_DATA_PREFIX + "/a/b")
        self.assertEqual(to_host("/paperclip"), tool.HOST_DATA_PREFIX)

    def test_host_paths_pass_through(self):
        self.assertEqual(to_host("/home/ubuntu/x"), "/home/ubuntu/x")
        self.assertEqual(to_host("/paperclip-evil/a"), "/paperclip-evil/a")

    def test_worktree_busy_is_per_worktree(self):
        self.assertTrue(worktree_busy("/w/TOG-" "99601", {"/w/TOG-" "99601/app.py"}))
        self.assertFalse(worktree_busy("/w/TOG-" "99601", {"/w/TOG-" "99602/app.py"}))
        self.assertFalse(worktree_busy("/w/TOG-" "99601", {"/w/TOG-" "996012"}))


class IdleTests(unittest.TestCase):
    # Kills: a rebuild resetting the idle clock, or .git noise exemptions
    # that would keep every worktree forever (host parity counts .git).

    def test_fresh_source_file_is_busy(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            with open(os.path.join(cand, "app.py"), "w") as handle:
                handle.write("print(2)\n")
            self.assertTrue(recent_activity(cand, 6))
        finally:
            fx.close()

    def test_in_place_modify_under_node_modules_is_idle(self):
        # Host parity: only node_modules SUBTREES are excluded from the
        # clock, so a modify-in-place (no dir mtime change) is idle while a
        # fresh entry (fresh dir mtime) is busy. A reinstall rewriting file
        # contents must not reset the idle clock on a tree nobody edits.
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            target = fx.add_regen(cand)
            blob = os.path.join(target, "blob")
            with open(blob, "ab") as handle:
                handle.write(b"more")
            self.assertFalse(recent_activity(cand, 6))
        finally:
            fx.close()

    def test_fresh_entry_under_node_modules_is_busy(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            target = fx.add_regen(cand)
            with open(os.path.join(target, "fresh"), "w") as handle:
                handle.write("x")
            self.assertTrue(recent_activity(cand, 6))
        finally:
            fx.close()

    def test_fresh_target_keeps_host_parity(self):
        # Only node_modules is excluded from the clock: a fresh target/
        # keeps the candidate. Safe direction, host parity.
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand, name="target")
            with open(os.path.join(cand, "target", "fresh"), "w") as handle:
                handle.write("x")
            self.assertTrue(recent_activity(cand, 6))
        finally:
            fx.close()

    def test_deep_change_beyond_maxdepth_is_idle(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            deep = os.path.join(cand, "a", "b", "c", "d")
            os.makedirs(deep)
            with open(os.path.join(deep, "fresh"), "w") as handle:
                handle.write("x")
            make_old(cand, seconds_ago=8 * HOUR)
            # Re-freshen ONLY the depth-4 file: ancestors stay backdated.
            with open(os.path.join(deep, "fresh"), "ab") as handle:
                handle.write(b"y")
            self.assertFalse(recent_activity(cand, 6))
        finally:
            fx.close()

    def test_zero_idle_hours_is_idle(self):
        self.assertFalse(recent_activity("/nonexistent", 0))


class EnumerateTests(unittest.TestCase):
    # Kills: missing side checkouts, missing nested worktrees, or crashes
    # on unlistable directories.

    def test_finds_sides_nested_and_upgrades(self):
        fx = ProjectFixture()
        try:
            cands, unlistable = enumerate_candidates(fx.root)
            self.assertEqual(unlistable, [])
            self.assertIn(fx.worktrees["TOG-" "99701-alpha"], cands)
            self.assertIn(fx.worktrees["TOG-" "99702-beta"], cands)
            self.assertIn(os.path.realpath(fx.side), cands)
            self.assertIn(os.path.realpath(fx.upgrade), cands)
            self.assertNotIn(os.path.realpath(fx.primary), cands)
        finally:
            fx.close()

    def test_missing_root_is_reported(self):
        cands, unlistable = enumerate_candidates("/nonexistent-root-xyz")
        self.assertEqual(cands, [])
        self.assertEqual(unlistable, ["/nonexistent-root-xyz"])

    def test_claude_nesting_is_found(self):
        fx = ProjectFixture()
        try:
            extra = os.path.join(fx.primary, ".claude", "worktrees",
                                 "TOG-" "99704-x")
            os.makedirs(extra)
            cands, _ = enumerate_candidates(fx.root)
            self.assertIn(os.path.realpath(extra), cands)
        finally:
            fx.close()


class GitProofTests(unittest.TestCase):
    # Kills: stripping tracked or non-ignored dirs, following symlinks, or
    # running git proof against the wrong repo.

    def test_ignored_untracked_regen_is_a_cache(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertIsNone(res["reason"])
            self.assertEqual([c["name"] for c in res["caches"]],
                             ["node_modules"])
        finally:
            fx.close()

    def test_tracked_dir_is_never_a_cache(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            target = os.path.join(cand, "node_modules")
            os.makedirs(target)
            with open(os.path.join(target, "blob"), "w") as handle:
                handle.write("x")
            git("add", "-f", "node_modules", cwd=cand)
            git("commit", "-qm", "tracked regen", cwd=cand)
            make_old(cand)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            # Per-dir refusals land in refusal lists with empty caches;
            # only worktree-level gates set reason. Git reports a dir with
            # tracked files as not-ignored (rc=1); the ls-files backstop
            # below covers git versions that answer otherwise.
            self.assertIsNone(res["reason"])
            self.assertEqual(res["caches"], [])
            self.assertEqual(res.get("not-ignored"), ["node_modules"])
        finally:
            fx.close()

    def test_non_ignored_dir_is_never_a_cache(self):
        fx = ProjectFixture()
        try:
            cand = fx.side
            target = os.path.join(cand, "target")
            os.makedirs(target)
            with open(os.path.join(target, "blob"), "w") as handle:
                handle.write("x")
            # The side clone has no .gitignore: target/ is not ignored.
            # A fresh clone's .git is genuinely fresh: backdate it to model
            # an idle checkout (the host probe counts .git, same as here).
            make_old(cand, include_git=True)
            res = fx.evaluate(cand, done_issues("TOG-" "99703"),
                              heartbeat=[run_row("id-TOG-" "99703", 30)])
            self.assertIsNone(res["reason"])
            self.assertEqual(res["caches"], [])
            self.assertEqual(res.get("not-ignored"), ["target"])
        finally:
            fx.close()

    def test_symlink_regen_is_recorded_never_deleted(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            real = os.path.join(fx.tmp.name, "outside-store")
            os.makedirs(real)
            with open(os.path.join(real, "blob"), "w") as handle:
                handle.write("x" * 16)
            link = os.path.join(cand, "node_modules")
            os.symlink(real, link)
            make_old(cand)
            # A fresh link entry itself is activity (host parity); backdate
            # the link so the test judges the symlink rule, not the clock.
            stamp = time.time() - 8 * HOUR
            os.utime(link, (stamp, stamp), follow_symlinks=False)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertEqual(res.get("skipped_symlink"), ["node_modules"])
            self.assertEqual(res["caches"], [])
            self.assertTrue(os.path.islink(
                os.path.join(cand, "node_modules")))
        finally:
            fx.close()

    def test_container_pointer_gitdir_resolves(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            dotgit = os.path.join(cand, ".git")
            # The real pointer resolves without any mapping.
            self.assertTrue(os.path.isdir(tool.resolve_git_dir(cand)))
            # A container-style pointer maps through the (patched) prefix.
            with mock.patch.object(tool, "HOST_DATA_PREFIX", fx.tmp.name):
                with mock.patch.object(tool, "CONTAINER_PREFIX",
                                       "/paperclip"):
                    mapped = os.path.join(fx.tmp.name, "gd")
                    os.makedirs(mapped)
                    with open(dotgit, "w") as handle:
                        handle.write("gitdir: /paperclip/gd\n")
                    self.assertEqual(tool.resolve_git_dir(cand),
                                     os.path.realpath(mapped))
                    with open(dotgit, "w") as handle:
                        handle.write("gitdir: /paperclip/missing\n")
                    self.assertIsNone(tool.resolve_git_dir(cand))
        finally:
            fx.close()

    def test_all_five_host_names_are_strippable(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            for name in tool.REGEN_NAMES:
                fx.add_regen(cand, name=name)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertIsNone(res["reason"])
            self.assertEqual(sorted(c["name"] for c in res["caches"]),
                             sorted(tool.REGEN_NAMES))
        finally:
            fx.close()

    def test_root_level_ignore_reaches_a_nested_regen_dir(self):
        # Error pin: git_proof ran with --git-dir <root/.git> -C <regen
        # parent> and no --work-tree, so the parent directory counted as
        # the repo root and a root-level ignore never matched a nested
        # regen dir (reclaimable space silently missed).
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            nested = os.path.join(cand, "packages", "web", "node_modules")
            os.makedirs(nested)
            with open(os.path.join(nested, "blob"), "wb") as handle:
                handle.write(b"\0" * 3)
            make_old(cand)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertIsNone(res["reason"])
            self.assertEqual([c["name"] for c in res["caches"]],
                             ["node_modules"])
        finally:
            fx.close()

    def test_parent_local_ignore_with_a_tracked_file_keeps(self):
        # Error pin (mirror image): with the parent as the work tree, a
        # parent-local ignore reported a force-added tracked file as
        # ignored-and-untracked and stripped it. Proven against the
        # candidate root, git sees the tracked file again, so the dir
        # reports not-ignored (a dir with tracked files is never a
        # cache) and is kept.
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            vend = os.path.join(cand, "packages", "vend")
            target = os.path.join(vend, "node_modules")
            os.makedirs(target)
            with open(os.path.join(vend, ".gitignore"), "w") as handle:
                handle.write("node_modules\n")
            with open(os.path.join(target, "keep.js"), "w") as handle:
                handle.write("x\n")
            git("add", "-f", "packages/vend/node_modules",
                "packages/vend/.gitignore", cwd=cand)
            git("commit", "-qm", "tracked vend", cwd=cand)
            make_old(cand)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertIsNone(res["reason"])
            self.assertEqual(res["caches"], [])
            self.assertEqual(res.get("not-ignored"),
                             ["packages/vend/node_modules"])
        finally:
            fx.close()

    def test_nested_worktree_regen_is_not_attributed_to_the_parent(self):
        # Error pin: the walk descended into linked worktrees (whose .git
        # is a FILE, never a pruned dir) and charged their regen dirs to
        # the parent candidate, bypassing the nested tree's own liveness
        # gate. The nested checkout's live card must decide its own dirs.
        fx = ProjectFixture()
        try:
            parent = os.path.join(fx.root, "co", "proj", "TOG-" "99706-main")
            git("clone", "-q", fx.origin, parent, cwd=fx.tmp.name)
            nested = os.path.join(parent, ".paperclip", "worktrees",
                                  "TOG-" "99707")
            os.makedirs(os.path.dirname(nested))
            git("worktree", "add", "-q", "-b", "TOG-" "99707", nested,
                "origin/main", cwd=parent)
            nested = os.path.realpath(nested)
            parent = os.path.realpath(parent)
            target = os.path.join(nested, "node_modules")
            os.makedirs(target)
            with open(os.path.join(target, "blob"), "wb") as handle:
                handle.write(b"\0" * 3)
            make_old(parent, include_git=True)
            issues = [issue_row("TOG-" "99706", "done"),
                      issue_row("TOG-" "99707", "todo")]
            heartbeat = [run_row("id-TOG-" "99706", 30),
                         run_row("id-TOG-" "99707", 1)]
            parent_res = fx.evaluate(parent, issues, heartbeat=heartbeat)
            self.assertIsNone(parent_res["reason"])
            self.assertEqual(parent_res["caches"], [])
            nested_res = fx.evaluate(nested, issues, heartbeat=heartbeat)
            self.assertEqual(nested_res["reason"], "card-active")
            self.assertEqual(nested_res["caches"], [])
        finally:
            fx.close()


class CacheDirOkTests(unittest.TestCase):
    # Kills: ignored-but-tracked slipping through, symlink handling slips,
    # and unverifiable-ls-files reading as untracked. Direct unit pins for
    # the shared reclaim gate this tool reuses.

    def test_ignored_and_untracked_passes(self):
        ok, reason = tool.reclaim.cache_dir_ok(
            is_symlink=False, check_ignore_rc=0, ls_files_rc=0,
            ls_files_out="")
        self.assertEqual((ok, reason), (True, None))

    def test_tracked_inside_fails(self):
        ok, reason = tool.reclaim.cache_dir_ok(
            is_symlink=False, check_ignore_rc=0, ls_files_rc=0,
            ls_files_out="node_modules/blob\n")
        self.assertEqual((ok, reason), (False, "tracked-inside"))

    def test_not_ignored_fails(self):
        ok, reason = tool.reclaim.cache_dir_ok(
            is_symlink=False, check_ignore_rc=1, ls_files_rc=0,
            ls_files_out="")
        self.assertEqual((ok, reason), (False, "not-ignored"))

    def test_symlink_fails(self):
        ok, reason = tool.reclaim.cache_dir_ok(
            is_symlink=True, check_ignore_rc=0, ls_files_rc=0,
            ls_files_out="")
        self.assertEqual((ok, reason), (False, "symlink"))

    def test_check_ignore_error_fails_closed(self):
        ok, reason = tool.reclaim.cache_dir_ok(
            is_symlink=False, check_ignore_rc=128, ls_files_rc=0,
            ls_files_out="")
        self.assertEqual((ok, reason), (False, "check-ignore-error"))

    def test_ls_files_error_fails_closed(self):
        ok, reason = tool.reclaim.cache_dir_ok(
            is_symlink=False, check_ignore_rc=0, ls_files_rc=128,
            ls_files_out="")
        self.assertEqual((ok, reason), (False, "ls-files-error"))


class EvaluateEndToEndTests(unittest.TestCase):
    # Kills: stale-gate wiring slips (open/recent/unknown keeping or not).

    def test_open_recent_run_keeps(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            res = fx.evaluate(cand, [issue_row("TOG-" "99702", "in_progress")],
                              heartbeat=[run_row("id-TOG-" "99702", 1)])
            self.assertEqual(res["reason"], "card-active")
            self.assertEqual(res["caches"], [])
        finally:
            fx.close()

    def test_open_old_run_strips(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            res = fx.evaluate(cand, [issue_row("TOG-" "99702", "todo")],
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertIsNone(res["reason"])
            self.assertEqual(len(res["caches"]), 1)
        finally:
            fx.close()

    def test_open_no_history_keeps(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            res = fx.evaluate(cand, [issue_row("TOG-" "99702", "todo")],
                              heartbeat=[run_row("id-OTHER", 30)])
            self.assertEqual(res["reason"], "run-history-unknown")
        finally:
            fx.close()

    def test_upgrade_checkout_strips_without_card(self):
        fx = ProjectFixture()
        try:
            with open(os.path.join(fx.upgrade, ".gitignore"), "w") as handle:
                handle.write("node_modules\n")
            fx.add_regen(fx.upgrade)
            make_old(fx.upgrade, include_git=True)
            res = fx.evaluate(fx.upgrade, [])
            self.assertIsNone(res["reason"])
            self.assertEqual(len(res["caches"]), 1)
        finally:
            fx.close()

    def test_fixture_without_git_proof_keeps(self):
        fx = ProjectFixture()
        try:
            plain = os.path.join(fx.root, "co", "proj", "TOG-" "99705-plain")
            os.makedirs(os.path.join(plain, "node_modules"))
            with open(os.path.join(plain, "node_modules", "b"), "w") as h:
                h.write("x")
            make_old(plain)
            res = fx.evaluate(plain, done_issues("TOG-" "99705"),
                              heartbeat=[run_row("id-TOG-" "99705", 30)])
            self.assertIsNone(res["reason"])
            self.assertEqual(res["caches"], [])
            self.assertEqual(res.get("git-unreadable"), ["node_modules"])
        finally:
            fx.close()


class CardHoursTests(unittest.TestCase):
    # Kills: newest-run selection slips and timestamp-key brittleness.

    def test_newest_run_wins(self):
        rows = [run_row("id-A", 30), run_row("id-A", 2),
                run_row("id-B", 1)]
        hours = card_hours_since_run(rows, "id-A", NOW)
        self.assertTrue(1.9 < hours < 2.1)

    def test_unseen_card_is_none(self):
        self.assertIsNone(card_hours_since_run([run_row("id-A", 1)],
                                               "id-B", NOW))

    def test_camel_case_keys_parse(self):
        rows = [{"id": "r", "status": "succeeded",
                 "createdAt": iso(5),
                 "contextSnapshot": {"issueId": "id-A"}}]
        hours = card_hours_since_run(rows, "id-A", NOW)
        self.assertTrue(4.9 < hours < 5.1)

    def test_unparseable_rows_are_ignored(self):
        rows = [{"id": "r", "contextSnapshot": {"issueId": "id-A"}}]
        self.assertIsNone(card_hours_since_run(rows, "id-A", NOW))


class ArgvTests(unittest.TestCase):
    # Kills: apply-by-default and unbounded/negative argv slips.

    def test_dry_run_is_default(self):
        opts = parse_argv([])
        self.assertFalse(opts.apply)
        self.assertEqual(opts.max_remove, tool.MAX_REMOVE_DEFAULT)

    def test_env_overrides_paths(self):
        with mock.patch.dict(os.environ,
                             {"PROJECT_REGEN_PRUNE_PROJECTS_ROOT": "/r",
                              "PROJECT_REGEN_PRUNE_MANIFEST_DIR": "/m"}):
            opts = parse_argv([])
            self.assertEqual(opts.projects_root, "/r")
            self.assertEqual(opts.manifest_dir, "/m")

    def test_argv_beats_env(self):
        with mock.patch.dict(os.environ,
                             {"PROJECT_REGEN_PRUNE_PROJECTS_ROOT": "/r"}):
            self.assertEqual(parse_argv(["--projects-root", "/p"]).
                             projects_root, "/p")

    def test_negative_bounds_rejected(self):
        for argv in (["--max-remove", "-1"], ["--stale-hours", "-1"],
                     ["--idle-hours", "-1"]):
            with self.assertRaises(SystemExit):
                parse_argv(argv)

    def test_order_is_largest_first(self):
        cands = [{"p": "/a", "bytes": 1}, {"p": "/b", "bytes": 9},
                 {"p": "/c", "bytes": 9}]
        self.assertEqual([c["p"] for c in order_for_removal(cands)],
                         ["/b", "/c", "/a"])


class KillSwitchTests(unittest.TestCase):
    # Kills: an env-var typo that never trips, or a marker that trips wrong.

    def test_env_truthy_values_trip(self):
        for value in ("1", "true", "yes", "on", " TRUE "):
            tripped, _why = tool.kill_switch({"PROJECT_REGEN_PRUNE_"
                                              "DISABLED": value}, "/m")
            self.assertTrue(tripped, value)

    def test_env_falsy_values_do_not_trip(self):
        for value in ("0", "false", "no", "", "off"):
            tripped, _why = tool.kill_switch({"PROJECT_REGEN_PRUNE_"
                                              "DISABLED": value}, "/m",
                                             exists=lambda _p: False)
            self.assertFalse(tripped, value)

    def test_marker_file_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, tool.KILL_FILE_NAME)
            with open(marker, "w") as handle:
                handle.write("hold\n")
            tripped, why = tool.kill_switch({}, tmp)
            self.assertTrue(tripped)
            self.assertIn(tool.KILL_FILE_NAME, why)


class ApplyTests(unittest.TestCase):
    # Kills: apply deleting the wrong thing, ignoring the cap, or skipping
    # the pre-delete re-read (a reopened card must survive).

    def run_pass(self, fx, api, cap, proc_patch=True):
        manifest = os.path.join(fx.tmp.name, "manifests")
        opts = parse_argv(["--apply", "--projects-root", fx.root,
                           "--manifest-dir", manifest,
                           "--max-remove", str(cap)])
        patches = []
        if proc_patch:
            patches.append(mock.patch.object(tool, "host_proc_refs",
                                             return_value=set()))
        patches.append(mock.patch.object(tool.reclaim, "refresh_live_refs",
                                         return_value={
                                             "run_paths": set(),
                                             "run_issue_ids": set(),
                                             "run_ws_ids": set(),
                                             "proc_paths": set()}))
        for patcher in patches:
            patcher.start()
        try:
            summary = run_tool(opts, api, {}, clock=lambda: NOW)
        finally:
            for patcher in patches:
                patcher.stop()
        return summary, manifest

    def test_apply_removes_only_the_stale_regen(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            doomed = fx.add_regen(idle)
            busy = fx.worktrees["TOG-" "99701-alpha"]
            fx.add_regen(busy)
            issues = done_issues("TOG-" "99701", "TOG-" "99702")
            api = FakeApi(issues=issues,
                          heartbeat=[run_row("id-TOG-" "99701", 30),
                                     run_row("id-TOG-" "99702", 30)])
            with mock.patch.object(tool, "host_proc_refs",
                                   return_value={os.path.join(busy,
                                                              "app.py")}):
                manifest = os.path.join(fx.tmp.name, "manifests")
                opts = parse_argv(["--apply", "--projects-root", fx.root,
                                   "--manifest-dir", manifest,
                                   "--max-remove", "200"])
                with mock.patch.object(
                        tool.reclaim, "refresh_live_refs",
                        return_value={"run_paths": set(),
                                      "run_issue_ids": set(),
                                      "run_ws_ids": set(),
                                      "proc_paths": set()}):
                    summary = run_tool(opts, api, {}, clock=lambda: NOW)
            self.assertFalse(os.path.exists(doomed))
            self.assertTrue(os.path.exists(
                os.path.join(busy, "node_modules", "blob")))
            self.assertEqual(summary["apply"]["removed_dirs"], 1)
            self.assertTrue(os.path.isfile(summary["manifest"]))
            stored = json.load(open(summary["manifest"]))
            self.assertEqual(stored["mode"], "apply")
        finally:
            fx.close()

    def test_cap_zero_removes_nothing(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            doomed = fx.add_regen(idle)
            api = FakeApi(issues=done_issues("TOG-" "99702"),
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            summary, _manifest = self.run_pass(fx, api, 0)
            self.assertTrue(os.path.exists(doomed))
            self.assertEqual(summary["apply"]["removed_dirs"], 0)
            self.assertEqual(summary["regen_dirs"], 1)
        finally:
            fx.close()

    def test_reopened_card_survives_apply(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            doomed = fx.add_regen(idle)
            scan_api = FakeApi(
                issues=done_issues("TOG-" "99702"),
                heartbeat=[run_row("id-TOG-" "99702", 30)],
                reread={"id-TOG-" "99702": "in_progress"})
            summary, _manifest = self.run_pass(fx, scan_api, 200)
            self.assertTrue(os.path.exists(doomed))
            self.assertEqual(
                summary["apply"]["skipped_by_reason"].get("card-reopened"),
                1)
        finally:
            fx.close()

    def test_reread_error_skips_loudly(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            doomed = fx.add_regen(idle)
            scan_api = FakeApi(issues=done_issues("TOG-" "99702"),
                               heartbeat=[run_row("id-TOG-" "99702", 30)],
                               dead=False)
            with mock.patch.object(FakeApi, "issue_status",
                                   side_effect=OSError("down")):
                summary, _manifest = self.run_pass(fx, scan_api, 200)
            self.assertTrue(os.path.exists(doomed))
            self.assertEqual(summary["apply"]["skipped_by_reason"].get(
                "card-reread-failed"), 1)
        finally:
            fx.close()

    def test_failed_rm_is_loud_exit_3(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            doomed = fx.add_regen(idle)
            os.chmod(idle, 0o555)
            api = FakeApi(issues=done_issues("TOG-" "99702"),
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            manifest = os.path.join(fx.tmp.name, "manifests")
            opts = parse_argv(["--apply", "--projects-root", fx.root,
                               "--manifest-dir", manifest,
                               "--max-remove", "200"])
            with mock.patch.object(tool, "host_proc_refs",
                                   return_value=set()):
                with mock.patch.object(
                        tool.reclaim, "refresh_live_refs",
                        return_value={"run_paths": set(),
                                      "run_issue_ids": set(),
                                      "run_ws_ids": set(),
                                      "proc_paths": set()}):
                    rc = tool.main(argv=["--apply", "--projects-root",
                                         fx.root, "--manifest-dir", manifest,
                                         "--max-remove", "200"],
                                   api=api, env={})
            os.chmod(idle, 0o755)
            self.assertEqual(rc, 3)
            self.assertTrue(os.path.exists(doomed))
        finally:
            fx.close()

    def test_kill_switch_forces_dry_run(self):
        fx = ProjectFixture()
        try:
            idle = fx.worktrees["TOG-" "99702-beta"]
            doomed = fx.add_regen(idle)
            api = FakeApi(issues=done_issues("TOG-" "99702"),
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            manifest = os.path.join(fx.tmp.name, "manifests")
            os.makedirs(manifest)
            with open(os.path.join(manifest, tool.KILL_FILE_NAME),
                      "w") as handle:
                handle.write("hold\n")
            opts = parse_argv(["--apply", "--projects-root", fx.root,
                               "--manifest-dir", manifest,
                               "--max-remove", "200"])
            with mock.patch.object(tool, "host_proc_refs",
                                   return_value=set()):
                with mock.patch.object(
                        tool.reclaim, "refresh_live_refs",
                        return_value={"run_paths": set(),
                                      "run_issue_ids": set(),
                                      "run_ws_ids": set(),
                                      "proc_paths": set()}):
                    summary = run_tool(opts, api, {}, clock=lambda: NOW)
            self.assertTrue(os.path.exists(doomed))
            self.assertIsNotNone(summary["kill_switch"])
            self.assertIsNone(summary["apply"])
        finally:
            fx.close()


class RevalidateTests(unittest.TestCase):
    # Kills: a run starting between scan and removal being missed. The
    # pre-delete re-check must consult fresh run paths as well as cards —
    # for cardless upgrade- checkouts paths are the ONLY live signal.

    def live_refs(self, run_paths=frozenset(), run_issue_ids=frozenset()):
        return {"run_paths": set(run_paths),
                "run_issue_ids": set(run_issue_ids),
                "run_ws_ids": set(), "proc_paths": set()}

    def check(self, api, res, refs):
        with mock.patch.object(tool.reclaim, "refresh_live_refs",
                               return_value=refs):
            with mock.patch.object(tool, "host_proc_refs",
                                   return_value=set()):
                return revalidate(api, res)

    def test_fresh_run_path_under_candidate_skips(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertIsNone(res["reason"])
            api = FakeApi(issues=done_issues("TOG-" "99702"),
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            why = self.check(api, res, self.live_refs(
                run_paths={os.path.join(cand, "node_modules", "blob")}))
            self.assertEqual(why, "run-path-appeared")
        finally:
            fx.close()

    def test_fresh_live_issue_id_skips(self):
        fx = ProjectFixture()
        try:
            cand = fx.worktrees["TOG-" "99702-beta"]
            fx.add_regen(cand)
            res = fx.evaluate(cand, done_issues("TOG-" "99702"),
                              heartbeat=[run_row("id-TOG-" "99702", 30)])
            self.assertIsNone(res["reason"])
            api = FakeApi(issues=done_issues("TOG-" "99702"),
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            why = self.check(api, res, self.live_refs(
                run_issue_ids={"id-TOG-" "99702"}))
            self.assertEqual(why, "live-run-appeared")
        finally:
            fx.close()

    def test_cardless_candidate_rechecks_paths_without_a_card(self):
        fx = ProjectFixture()
        try:
            res = {"w": fx.upgrade, "linked_ids": {}}
            api = FakeApi(issues=[])
            why = self.check(api, res, self.live_refs(
                run_paths={os.path.join(fx.upgrade, "app.py")}))
            self.assertEqual(why, "run-path-appeared")
            why = self.check(api, res, self.live_refs())
            self.assertIsNone(why)
        finally:
            fx.close()


class SummarizeTests(unittest.TestCase):
    # Kills: the dry-run report overstating what apply will free. Apply
    # always skips non-terminal cards as card-reopened, so open-card-old-run
    # caches are speculative, never headline. The removal cap is raised on
    # the headline.

    def dry_run(self, fx, api):
        manifest = os.path.join(fx.tmp.name, "manifests")
        opts = parse_argv(["--projects-root", fx.root,
                           "--manifest-dir", manifest,
                           "--max-remove", "200"])
        with mock.patch.object(tool, "host_proc_refs",
                               return_value=set()):
            with mock.patch.object(
                    tool.reclaim, "refresh_live_refs",
                    return_value={"run_paths": set(),
                                  "run_issue_ids": set(),
                                  "run_ws_ids": set(),
                                  "proc_paths": set()}):
                return run_tool(opts, api, {}, clock=lambda: NOW)

    def test_open_old_run_is_speculative_never_headline(self):
        fx = ProjectFixture()
        try:
            fx.add_regen(fx.worktrees["TOG-" "99702-beta"])
            api = FakeApi(issues=[issue_row("TOG-" "99702", "todo")],
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            summary = self.dry_run(fx, api)
            self.assertEqual(summary["stale_candidates"], 1)
            self.assertEqual(summary["regen_dirs"], 0)
            self.assertEqual(summary["speculative_dirs"], 1)
            self.assertGreater(summary["speculative_bytes"], 0)
        finally:
            fx.close()

    def test_terminal_card_stays_headline(self):
        fx = ProjectFixture()
        try:
            fx.add_regen(fx.worktrees["TOG-" "99702-beta"])
            api = FakeApi(issues=done_issues("TOG-" "99702"),
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            summary = self.dry_run(fx, api)
            self.assertEqual(summary["regen_dirs"], 1)
            self.assertEqual(summary["speculative_dirs"], 0)
        finally:
            fx.close()

    def test_cardless_upgrade_is_headline_not_speculative(self):
        fx = ProjectFixture()
        try:
            with open(os.path.join(fx.upgrade, ".gitignore"), "w") as handle:
                handle.write("node_modules\n")
            fx.add_regen(fx.upgrade)
            make_old(fx.upgrade, include_git=True)
            api = FakeApi(issues=[])
            summary = self.dry_run(fx, api)
            self.assertEqual(summary["regen_dirs"], 1)
            self.assertEqual(summary["speculative_dirs"], 0)
        finally:
            fx.close()


class EntryPointTests(unittest.TestCase):
    # Kills: blind applies and missing-env dry runs slipping through.

    def test_blind_apply_is_refused(self):
        with self.assertRaises(SafetyRefusal):
            tool.main(argv=["--apply"], api=None, env={})

    def test_dry_run_without_env_is_an_error(self):
        with self.assertRaises(PruneError):
            tool.main(argv=[], api=None, env={})

    def test_dead_board_is_an_error(self):
        api = FakeApi(dead=True)
        with self.assertRaises(PruneError):
            tool.main(argv=["--projects-root", "/nonexistent-xyz"],
                      api=api, env={})

    def test_line_reports_mode_and_counts(self):
        fx = ProjectFixture()
        try:
            fx.add_regen(fx.worktrees["TOG-" "99702-beta"])
            api = FakeApi(issues=done_issues("TOG-" "99702"),
                          heartbeat=[run_row("id-TOG-" "99702", 30)])
            manifest = os.path.join(fx.tmp.name, "manifests")
            opts = parse_argv(["--projects-root", fx.root,
                               "--manifest-dir", manifest,
                               "--max-remove", "200"])
            with mock.patch.object(tool, "host_proc_refs",
                                   return_value=set()):
                with mock.patch.object(
                        tool.reclaim, "refresh_live_refs",
                        return_value={"run_paths": set(),
                                      "run_issue_ids": set(),
                                      "run_ws_ids": set(),
                                      "proc_paths": set()}):
                    summary = run_tool(opts, api, {}, clock=lambda: NOW)
            stored = json.load(open(summary["manifest"]))
            self.assertEqual(stored["mode"], "dry-run")
            self.assertIn("would remove 1 dirs", summary["line"])
            self.assertIn("manifest", summary["line"])
        finally:
            fx.close()


class AllowlistEnforceWiring(unittest.TestCase):
    def test_enforce_refuses_before_any_transport(self):
        # Wiring pin: deleting both the quiet pre-check and the ApiClient
        # check must fail this test (main would proceed to run_tool and
        # the network).
        env = {"PAPERCLIP_API_URL": "https://evil.example.com",
               "PAPERCLIP_API_KEY": "k",
               "PAPERCLIP_COMPANY_ID": "c"}
        run_env = dict(os.environ)
        run_env.pop("PAPERCLIP_API_ALLOWLIST_TEST", None)
        run_env.pop("PAPERCLIP_API_BRIDGE_MODE", None)
        run_env["PAPERCLIP_API_ALLOWLIST_MODE"] = "enforce"
        with mock.patch.dict(os.environ, run_env, clear=True), \
                mock.patch("paperclip_api_allowlist.load_allowlist",
                           return_value=set()), \
                mock.patch("urllib.request.urlopen") as urlopen, \
                mock.patch.object(tool.reclaim, "ApiClient") as api_cls, \
                mock.patch.object(tool, "run_tool") as run_tool:
            with self.assertRaises(PruneError) as raised:
                tool.main(argv=[], api=None, env=env)
        self.assertIn("mode=enforce", str(raised.exception))
        urlopen.assert_not_called()
        api_cls.assert_not_called()
        run_tool.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
