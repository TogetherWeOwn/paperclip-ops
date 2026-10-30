#!/usr/bin/env python3
"""Offline tests for prune_operator_regen_dirs.py (case_34c2c99f E6).

Real git, find, filesystem and a real /proc probe of child processes. Only the
sudo prefixes and the database boundary are replaced.
Run: python3 ops/case_34c2c99f/test_prune_operator_regen_dirs.py
"""
import contextlib
import fcntl
import hashlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import prune_operator_regen_dirs as tool  # noqa: E402

PROBE_FILE = os.path.join(HERE, "regen_probe.py")

for _key in [k for k in os.environ if k.startswith("GIT_")]:
    del os.environ[_key]

RID = "00000000-0000-4000-8000-000000000003"
RID2 = "00000000-0000-4000-8000-000000000020"
PY = sys.executable
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
REAL_DB_SNAPSHOT = tool.db_snapshot
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
REAL_LIVE_REFS = tool.live_refs


def probe_view():
    out = subprocess.run([PY, "-I", PROBE_FILE], capture_output=True, text=True, timeout=120).stdout
    return json.loads(out)["refs"]


def git(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def backdate(path, seconds=7200):
    old = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(path):
        # Tolerate transient files (e.g. git's .git/*.lock left behind
        # `git commit`) vanishing between os.walk and os.utime: that raced
        # 2/187 cases on CI with FileNotFoundError. .git internals stay
        # covered on purpose: folder_idle_block uses find -maxdepth 3, so
        # their mtimes are part of the idle signal.
        for name in dirnames + filenames:
            # Tolerate transient entries vanishing between os.walk and
            # os.utime (e.g. git's .git/*.lock or background maintenance
            # under a loaded CI runner raced 2/187 cases on CI): a missing
            # entry has no timestamp to set, so skip it. Broad OSError covers
            # vanishing entries plus other transient utime failures.
            try:
                os.utime(os.path.join(dirpath, name), (old, old), follow_symlinks=False)
            except OSError:
                continue
    os.utime(path, (old, old))


class RegenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="regen-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "runs")
        os.makedirs(self.root)
        self.pause = os.path.join(self.tmp, "prune-operator-regen-dirs.PAUSE")
        self.log = os.path.join(self.tmp, "actions.log")
        self.db = {}
        self.procs = []
        self.addCleanup(self.kill_procs)
        self.patch(tool, "OP_PREFIX", [])
        self.patch(tool, "live_refs", side_effect=probe_view)
        self.patch(tool, "MANIFEST_DIR", os.path.join(self.tmp, "manifests"))
        self.patch(tool, "LOG_PATH", self.log)
        self.patch(tool, "PAUSE_PATH", self.pause)
        self.patch(tool, "LOCK_PATH", os.path.join(self.tmp, "regen.lock"))
        self.patch(tool, "FAILED_PATH", os.path.join(self.tmp, "regen.failed.json"))
        self.keep = os.path.join(self.tmp, "regen-prune-keep.txt")
        write(self.keep, "# no custody holds\n")
        self.patch(tool, "KEEP_PATH", self.keep)
        # Archive-first stub lib (case_90645769): implements the exact
        # offsite_put/offsite_get contract the tool requires, backed by a
        # tmpdir store. FAIL_PUT=1 refuses the upload; POISON_GET=1 serves
        # garbage on re-download so the readback check must catch it.
        self.store = os.path.join(self.tmp, "offsite-store")
        os.makedirs(self.store)
        self.archlib = os.path.join(self.tmp, "archive_lib.sh")
        write(self.archlib,
              "STORE=%s\n"
              "offsite_put() {\n"
              "  local name=\"$1\"; shift\n"
              "  local dest=\"$STORE/$(printf '%%s' \"$name\" | tr '/' '_').bin\"\n"
              "  if [[ -n \"${FAIL_PUT:-}\" ]]; then echo 'stub: upload refused' >&2; return 1; fi\n"
              "  \"$@\" > \"$dest\"\n"
              "  local rc=$?\n"
              "  if [[ $rc -ne 0 ]]; then rm -f \"$dest\"; return $rc; fi\n"
              "  printf 'put\\n' >> \"$STORE/puts.log\"\n"
              "  sha256sum \"$dest\" | cut -d' ' -f1\n"
              "}\n"
              "offsite_get() {\n"
              "  if [[ -n \"${POISON_GET:-}\" ]]; then echo 'stub: not-an-archive'; return 0; fi\n"
              "  if [[ -n \"${FAIL_GET_RC:-}\" ]]; then echo 'stub: transport failed' >&2; return 1; fi\n"
              "  cat \"$STORE/$(printf '%%s' \"$1\" | tr '/' '_').bin\"\n"
              "  printf 'get\\n' >> \"$STORE/gets.log\"\n"
              "}\n" % shlex.quote(self.store))
        self.default_archive_lib = tool.ARCHIVE_LIB
        self.patch(tool, "ARCHIVE_LIB", self.archlib)
        self.patch(tool, "home_pct", return_value=50)
        self.patch(tool, "db_snapshot", side_effect=self.fake_db)

    def patch(self, target, name, new=None, **kwargs):
        patcher = (mock.patch.object(target, name, new, **kwargs) if new is not None
                   else mock.patch.object(target, name, **kwargs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_db(self, run_ids):
        return {rid: self.db[rid] for rid in run_ids if rid in self.db}

    def kill_procs(self):
        for proc in self.procs:
            proc.kill()
            proc.wait()
            proc.stdout.close()

    def spawn(self, code, cwd, *args):
        proc = subprocess.Popen([PY, "-c", code, *args], cwd=cwd, stdout=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def make_run(self, rid=RID, status="failed", retry=False, lease=False, repo=True):
        run = os.path.join(self.root, rid)
        ws = os.path.join(run, "ws")
        write(os.path.join(ws, "src.txt"), "source\n")
        write(os.path.join(ws, ".gitignore"), "node_modules/\ntarget/\n")
        write(os.path.join(ws, "node_modules", "pkg", "index.js"))
        write(os.path.join(ws, "target", "debug", "bin"))
        if repo:
            git(ws, "init", "-q")
            git(ws, "add", "src.txt", ".gitignore")
            git(ws, "commit", "-q", "-m", "init")
        backdate(run)
        self.db[rid] = (status, retry, lease)
        return run, ws

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tool.main(["--runs-root", self.root, *argv])
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def mutate_before_revalidation(self, run, mutation):
        original = tool.revalidate_run

        def wrapper(rid, run_real, idle_min):
            mutation()
            backdate(run)
            return original(rid, run_real, idle_min)

        self.patch(tool, "revalidate_run", side_effect=wrapper)

    def test_dry_run_is_the_default_and_removes_nothing(self):
        _, ws = self.make_run()
        rc, s = self.run_main()
        self.assertEqual((rc, s["mode"], s["planned"], s["removed"]), (0, "dry-run", 2, 0))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        with open(s["manifest"]) as fh:
            self.assertEqual(sorted(fh.read().split()),
                             sorted([os.path.join(ws, "node_modules"), os.path.join(ws, "target")]))

    def test_apply_removes_only_the_ignored_regenerable_dirs(self):
        _, ws = self.make_run()
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 2))
        self.assertFalse(os.path.exists(os.path.join(ws, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(ws, "target")))
        self.assertTrue(os.path.isfile(os.path.join(ws, "src.txt")))
        self.assertTrue(os.path.isdir(os.path.join(ws, ".git")))
        with open(self.log) as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)

    def test_pause_file_stops_before_any_io(self):
        self.make_run()
        write(self.pause)
        self.patch(tool, "op", side_effect=AssertionError("no io while paused"))
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["paused"]), (0, True))
        self.assertFalse(os.path.exists(tool.MANIFEST_DIR))

    def test_pause_file_created_during_apply_stops_removals(self):
        run, ws = self.make_run()
        self.mutate_before_revalidation(run, lambda: write(self.pause))
        rc, s = self.run_main("--apply")
        self.assertEqual(s["refused_by_reason"], {"paused": 2})
        self.assertEqual(s["removed"], 0)
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_tracked_file_under_a_regenerable_name_keeps_that_dir(self):
        run, ws = self.make_run()
        git(ws, "add", "-f", "node_modules/pkg/index.js")
        backdate(run)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"tracked-files": 1})
        self.assertTrue(os.path.isfile(os.path.join(ws, "node_modules", "pkg", "index.js")))
        self.assertFalse(os.path.exists(os.path.join(ws, "target")))

    def test_regenerable_name_that_is_not_git_ignored_is_kept(self):
        run, ws = self.make_run()
        write(os.path.join(ws, ".gitignore"), "target/\n")
        backdate(run)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"not-ignored": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_run_without_a_repository_is_unprovable_and_kept(self):
        _, ws = self.make_run(repo=False)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"git-unprovable": 2})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_enclosing_repo_outside_the_folder_does_not_vouch_for_it(self):
        _, ws = self.make_run(repo=False)
        git(self.tmp, "init", "-q")
        write(os.path.join(self.tmp, ".gitignore"), "node_modules/\ntarget/\n")
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"repo-outside-folder": 2})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_nested_repo_at_any_depth_inside_the_folder_is_stripped(self):
        run, ws = self.make_run()
        nested = os.path.join(ws, ".paperclip", "worktrees", "x")
        write(os.path.join(nested, ".gitignore"), "node_modules/\n")
        write(os.path.join(nested, "a.txt"))
        git(nested, "init", "-q")
        git(nested, "add", ".gitignore", "a.txt")
        git(nested, "commit", "-q", "-m", "a")
        write(os.path.join(nested, "node_modules", "p", "i.js"))
        backdate(run)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["removed"], 3)
        self.assertFalse(os.path.exists(os.path.join(nested, "node_modules")))

    def test_candidate_that_is_itself_a_repository_is_kept(self):
        run, ws = self.make_run()
        git(os.path.join(ws, "node_modules"), "init", "-q")
        write(os.path.join(ws, "node_modules", "unpushed.txt"))
        backdate(run)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"nested-repo": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules", ".git")))

    def test_recent_write_deep_inside_a_candidate_keeps_it(self):
        run, ws = self.make_run()
        os.makedirs(os.path.join(ws, "node_modules", "pkg", "deep", "x"))
        backdate(run)
        write(os.path.join(ws, "node_modules", "pkg", "deep", "x", "still-writing.js"))
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"recent-write": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(ws, "target")))

    def test_emergency_home_usage_waives_the_idle_clock(self):
        _, ws = self.make_run()
        write(os.path.join(ws, "src.txt"), "touched now\n")
        rc, s = self.run_main()
        self.assertEqual((s["planned"], s["kept_by_reason"]), (0, {"recent-write": 1}))
        self.patch(tool, "home_pct", return_value=95)
        rc, s = self.run_main()
        self.assertEqual((s["planned"], s["idle_min"]), (2, None))

    def test_live_process_with_cwd_in_the_folder_keeps_the_run(self):
        _, ws = self.make_run()
        proc = self.spawn("import time; print('ready', flush=True); time.sleep(120)", ws)
        proc.stdout.readline()
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"live-process-ref": 1})
        self.assertEqual(s["planned"], 0)
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_open_fd_inside_a_candidate_keeps_the_run(self):
        _, ws = self.make_run()
        held = os.path.join(ws, "node_modules", "pkg", "index.js")
        code = ("import sys, time\n"
                "held = open(sys.argv[1])\n"
                "print('ready', flush=True)\n"
                "time.sleep(120)\n")
        proc = self.spawn(code, self.tmp, held)
        proc.stdout.readline()
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"live-process-ref": 1})
        self.assertTrue(os.path.isfile(held))

    def test_real_probe_sees_child_cwd_and_open_fd(self):
        cwd_dir = os.path.join(self.tmp, "cwd")
        held = os.path.join(self.tmp, "held.txt")
        os.makedirs(cwd_dir)
        write(held)
        code = ("import sys, time\n"
                "held = open(sys.argv[1])\n"
                "print('ready', flush=True)\n"
                "time.sleep(120)\n")
        proc = self.spawn(code, cwd_dir, held)
        proc.stdout.readline()
        refs = probe_view()
        self.assertIn(cwd_dir, refs)
        self.assertIn(held, refs)

    def test_non_terminal_run_is_kept(self):
        _, ws = self.make_run(status="running")
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"run-not-terminal": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_unknown_run_is_kept_while_known_runs_are_stripped(self):
        _, ws = self.make_run()
        _, unknown_ws = self.make_run(rid=RID2)
        self.db.pop(RID2)
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"], s["kept_by_reason"]), (0, 2, {"run-unknown": 1}))
        self.assertFalse(os.path.exists(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(unknown_ws, "node_modules")))

    def test_pending_retry_keeps_the_run(self):
        _, ws = self.make_run(retry=True)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"retry-pending": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_active_lease_keeps_the_run(self):
        _, ws = self.make_run(lease=True)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"lease-active": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_custody_hold_keeps_a_terminal_idle_run(self):
        _, ws = self.make_run()
        write(self.keep, "%s\n" % RID)
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["planned"], s["removed"], s["custody_held"]), (0, 0, 0, 1))
        self.assertEqual(s["kept_by_reason"], {"custody-hold": 1})
        self.assertIsNone(s["manifest"])
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_custody_hold_file_supports_comments_blank_lines_and_whitespace(self):
        _, ws = self.make_run()
        write(self.keep, "# recovery copies must not be stripped\n\n   \n  %s  \n# trailing comment\n" % RID)
        rc, s = self.run_main("--apply")
        self.assertEqual((s["planned"], s["removed"], s["custody_held"]), (0, 0, 1))
        self.assertEqual(s["kept_by_reason"], {"custody-hold": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_hold_added_between_plan_and_apply_is_refused(self):
        run, ws = self.make_run()
        self.mutate_before_revalidation(run, lambda: write(self.keep, "%s\n" % RID))
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["refused_by_reason"]), (0, {"custody-hold": 2}))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_hold_added_during_candidate_validation_is_refused_before_rm(self):
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")),
                (RID, run, os.path.join(ws, "target"))]
        validate = tool.candidate_block

        def hold_then_validate(run_real, cand, idle_min):
            write(self.keep, "%s\n" % RID)
            return validate(run_real, cand, idle_min)

        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "candidate_block", side_effect=hold_then_validate)
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() + 100, {})
        self.assertEqual((removed, dict(refused), abort), (0, {"custody-hold": 2}, None))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_unreadable_custody_holds_fail_closed(self):
        _, ws = self.make_run()
        # A directory is not readable as a hold file for any uid, root included.
        self.patch(tool, "KEEP_PATH", self.tmp)
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("custody", s["fail_closed"])
        self.assertIsNone(s.get("manifest"))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_malformed_custody_hold_fails_closed(self):
        _, ws = self.make_run()
        write(self.keep, "%s\nnot-a-run-id\n" % RID)
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("custody", s["fail_closed"])
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_legacy_annotated_holds_preserve_exact_set_and_both_runs(self):
        workspaces = [self.make_run(rid=rid)[1] for rid in (RID, RID2)]
        # Synthetic version of the host shape: header plus two annotated IDs.
        text = "# recovery copies\n%s # first copy\n  %s\t# second # note\n" % (RID, RID2)
        write(self.keep, text)
        legacy_set = {line.split("#", 1)[0].strip() for line in text.splitlines()
                      if line.split("#", 1)[0].strip()}
        self.assertEqual(legacy_set, {RID, RID2})
        self.assertEqual(tool.read_keep(), legacy_set)
        for argv in ((), ("--apply",)):
            with self.subTest(argv=argv):
                rc, s = self.run_main(*argv)
                self.assertEqual((rc, s["planned"], s["removed"], s["custody_held"]),
                                 (0, 0, 0, 2))
                self.assertEqual(s["kept_by_reason"], {"custody-hold": 2})
                self.assertIsNone(s["manifest"])
                for ws in workspaces:
                    self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
                    self.assertTrue(os.path.isdir(os.path.join(ws, "target")))
        with open(self.keep) as handle:
            self.assertEqual(handle.read(), text)

    def test_malformed_prefix_before_annotation_fails_closed(self):
        _, ws = self.make_run()
        for entry in ("not-a-run-id", RID + "x", RID + " extra", RID + " " + RID2):
            with self.subTest(entry=entry):
                write(self.keep, "%s # valid hold\n%s # recovery copy\n" % (RID, entry))
                rc, s = self.run_main("--apply")
                self.assertEqual(rc, 2)
                self.assertIn("custody", s["fail_closed"])
                self.assertIsNone(s.get("manifest"))
                self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
                self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_annotated_hold_added_during_validation_refuses_before_rm(self):
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")),
                (RID, run, os.path.join(ws, "target"))]
        validate = tool.candidate_block

        def hold_then_validate(run_real, cand, idle_min):
            write(self.keep, "%s # recovery copy\n" % RID)
            return validate(run_real, cand, idle_min)

        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "candidate_block", side_effect=hold_then_validate)
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() + 100, {})
        self.assertEqual((removed, dict(refused), abort), (0, {"custody-hold": 2}, None))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_empty_hold_file_fails_closed(self):
        _, ws = self.make_run()
        write(self.keep, "")
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("custody", s["fail_closed"])
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_blank_only_hold_file_fails_closed(self):
        _, ws = self.make_run()
        write(self.keep, "\n   \n")
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("custody", s["fail_closed"])
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_malformed_hold_during_validation_aborts_apply_fail_closed(self):
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")),
                (RID, run, os.path.join(ws, "target"))]
        validate = tool.candidate_block

        def malformed_then_validate(run_real, cand, idle_min):
            write(self.keep, "%s\nnot-a-run-id\n" % RID)
            return validate(run_real, cand, idle_min)

        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "candidate_block", side_effect=malformed_then_validate)
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() + 100, {})
        self.assertEqual(removed, 0)
        self.assertIn("custody", abort)
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_unreadable_hold_at_run_reread_aborts_apply_fail_closed(self):
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")),
                (RID, run, os.path.join(ws, "target"))]
        self.patch(tool, "revalidate_run", return_value=None)
        # A directory is not readable as a hold file for any uid, root included.
        self.patch(tool, "KEEP_PATH", self.tmp)
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() + 100, {})
        self.assertEqual(removed, 0)
        self.assertIn("custody", abort)
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_symlinked_regenerable_name_is_not_a_candidate(self):
        run, ws = self.make_run()
        outside = os.path.join(self.tmp, "outside")
        write(os.path.join(outside, "keep.txt"))
        shutil.rmtree(os.path.join(ws, "node_modules"))
        os.symlink(outside, os.path.join(ws, "node_modules"))
        backdate(run)
        rc, s = self.run_main("--apply")
        self.assertEqual((s["planned"], s["removed"]), (1, 1))
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")))
        self.assertTrue(os.path.islink(os.path.join(ws, "node_modules")))

    def test_path_swapped_for_a_symlink_after_planning_is_refused(self):
        run, ws = self.make_run()
        outside = os.path.join(self.tmp, "outside")
        write(os.path.join(outside, "node_modules", "keep.txt"))

        def swap():
            shutil.move(ws, ws + ".moved")
            os.symlink(outside, ws)

        self.mutate_before_revalidation(run, swap)
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["refused_by_reason"]), (0, {"symlinked-path": 2}))
        self.assertTrue(os.path.isfile(os.path.join(outside, "node_modules", "keep.txt")))

    def test_parent_swapped_after_validation_is_refused_before_rm(self):
        run, ws = self.make_run()
        outside = os.path.join(self.tmp, "outside")
        write(os.path.join(outside, "node_modules", "keep.txt"))
        plan = [(RID, run, os.path.join(ws, "node_modules")),
                (RID, run, os.path.join(ws, "target"))]
        shutil.move(ws, ws + ".moved")
        os.symlink(outside, ws)
        # Simulate stale validation: the planning-time gates pass, so only the
        # last-second symlink check before rm stands between the swap and deletion.
        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "candidate_block", return_value=None)
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() + 100, {})
        self.assertEqual((removed, dict(refused), abort), (0, {"symlinked-path": 2}, None))
        self.assertTrue(os.path.isfile(os.path.join(outside, "node_modules", "keep.txt")))
        self.assertTrue(os.path.islink(ws))

    def test_candidate_tracked_after_planning_is_refused(self):
        run, ws = self.make_run()
        self.mutate_before_revalidation(run, lambda: git(ws, "add", "-f", "target/debug/bin"))
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["refused_by_reason"]), (1, {"tracked-files": 1}))
        self.assertTrue(os.path.isfile(os.path.join(ws, "target", "debug", "bin")))

    def test_run_that_turns_live_after_planning_is_not_touched(self):
        run, ws = self.make_run()
        self.mutate_before_revalidation(run, lambda: self.db.__setitem__(RID, ("running", False, False)))
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["refused_by_reason"]), (0, {"run-not-terminal": 2}))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_process_that_starts_after_planning_keeps_the_run(self):
        run, ws = self.make_run()
        self.mutate_before_revalidation(run, lambda: self.spawn("import time; time.sleep(120)", ws))
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["refused_by_reason"]), (0, {"live-process-ref": 2}))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_max_remove_bounds_one_firing(self):
        run, ws = self.make_run()
        rc, s = self.run_main("--apply", "--max-remove", "1")
        self.assertEqual((s["planned"], s["removed"]), (1, 1))
        self.assertFalse(os.path.exists(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))
        backdate(run)
        rc, s = self.run_main("--apply", "--max-remove", "1")
        self.assertEqual(s["removed"], 1)
        self.assertFalse(os.path.exists(os.path.join(ws, "target")))

    def test_zero_time_budget_plans_nothing(self):
        _, ws = self.make_run()
        rc, s = self.run_main("--apply", "--time-budget", "0")
        self.assertEqual((rc, s["examined"], s["planned"], s["removed"]), (0, 0, 0, 0))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_zero_time_budget_plans_nothing_on_a_frozen_clock(self):
        # Determinism guard (case_2f45f437): on a coarse clock the two monotonic()
        # calls around `deadline = monotonic() + 0` can return the same tick,
        # so a strict `>` check examines one run. A frozen clock reproduces it.
        _, ws = self.make_run()
        self.patch(tool, "time", types.SimpleNamespace(monotonic=lambda: 1000.0, time=time.time))
        rc, s = self.run_main("--apply", "--time-budget", "0")
        self.assertEqual((rc, s["examined"], s["planned"], s["removed"]), (0, 0, 0, 0))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_zero_time_budget_stops_at_the_deadline_itself(self):
        _, ws = self.make_run()
        self.patch(tool, "time", types.SimpleNamespace(monotonic=lambda: 1000.0, time=time.time))
        rc, s = self.run_main("--apply", "--time-budget", "0")
        self.assertEqual((rc, s["examined"], s["planned"], s["removed"]), (0, 0, 0, 0))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_apply_group_refuses_at_the_budget_instant_without_revalidating(self):
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")), (RID, run, os.path.join(ws, "target"))]
        self.patch(tool, "time", types.SimpleNamespace(monotonic=lambda: 1000.0, time=time.time))
        self.patch(tool, "revalidate_run", side_effect=AssertionError("revalidated at the budget instant"))
        removed, refused, abort = tool.apply_plan(plan, 10, 1000.0, {})
        self.assertEqual((removed, dict(refused), abort), (0, {"time-budget": 2}, None))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_apply_candidate_refuses_once_the_budget_is_reached_between_candidates(self):
        # Archive gate pinned by its own tests; isolate the timing path.
        self.patch(tool, "ensure_archived", return_value=(True, None))
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")), (RID, run, os.path.join(ws, "target"))]
        clock = {"now": 999.0}

        def reach_budget(run_real, cand, idle_min):
            clock["now"] = 1000.0
            return None

        self.patch(tool, "time", types.SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "candidate_block", side_effect=reach_budget)
        self.patch(tool, "symlink_block", return_value=None)
        self.patch(tool, "op", side_effect=lambda *cmd, timeout=120: subprocess.CompletedProcess(cmd, 0, "", ""))
        removed, refused, abort = tool.apply_plan(plan, 10, 1000.0, {})
        self.assertEqual((removed, dict(refused), abort), (1, {"time-budget": 1}, None))

    def test_scan_adds_no_candidate_once_the_budget_is_reached(self):
        _, ws = self.make_run()
        clock = {"now": 0.0}
        real_candidates = tool.candidates

        def scan_reaches_budget(run_real):
            clock["now"] = 10.0
            return real_candidates(run_real)

        self.patch(tool, "time", types.SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
        self.patch(tool, "candidates", side_effect=scan_reaches_budget)
        rc, s = self.run_main("--apply", "--time-budget", "10")
        self.assertEqual((rc, s["examined"], s["planned"], s["removed"]), (0, 1, 0, 0))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_nan_time_budget_is_refused_before_any_run(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tool.parse_args(["--time-budget", "nan"])

    @unittest.skipIf(IS_ROOT, "chmod does not block root")
    def test_rm_failure_is_counted_and_cooled_before_the_next_firing(self):
        run, ws = self.make_run()
        cand = os.path.join(ws, "node_modules")
        pkg = os.path.join(cand, "pkg")
        os.chmod(pkg, 0o500)

        def restore_permissions():
            if os.path.isdir(pkg):
                os.chmod(pkg, 0o700)

        self.addCleanup(restore_permissions)
        preconditions = {"euid": os.geteuid(), "pkg_mode": oct(os.stat(pkg).st_mode & 0o777)}
        commands = []
        real_op = tool.op

        def observed_op(*cmd, timeout=120):
            # Preserve real guards and rm. An empty refusal map alone cannot
            # distinguish a pre-rm safety keep from successful removal.
            result = real_op(*cmd, timeout=timeout)
            commands.append({"cmd": cmd, "rc": result.returncode,
                             "stdout": result.stdout, "stderr": result.stderr})
            return result

        self.patch(tool, "op", side_effect=observed_op)
        rc, s = self.run_main("--apply")
        evidence = json.dumps({"preconditions": preconditions, "rc": rc,
                               "summary": s, "commands": commands}, sort_keys=True)
        self.assertEqual(s["refused_by_reason"], {"rm-failed": 1}, evidence)
        self.assertEqual((rc, s["idle_min"], s["planned"], s["removed"]), (0, 10, 2, 1), evidence)
        self.assertEqual((s["kept_by_reason"], s["fail_closed"], s["ledger_error"]),
                         ({}, None, None), evidence)
        self.assertEqual((s["archive_verified_runs"], s["archive_pending_runs"]), (1, 0), evidence)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "index.js")), evidence)
        self.assertFalse(os.path.exists(os.path.join(ws, "target")), evidence)
        self.assertTrue(os.path.isfile(tool.FAILED_PATH), evidence)
        with open(tool.FAILED_PATH) as fh:
            failed = json.load(fh)
        self.assertEqual(set(failed), {cand}, evidence)
        self.assertIsInstance(failed[cand], (int, float), evidence)
        self.assertGreater(failed[cand], 0, evidence)

        backdate(run)
        commands.clear()
        rc, s = self.run_main("--apply")
        evidence = json.dumps({"preconditions": preconditions, "rc": rc,
                               "summary": s, "commands": commands, "failed": failed}, sort_keys=True)
        self.assertEqual((rc, s["planned"], s["removed"], s["kept_by_reason"]),
                         (0, 0, 0, {"rm-failed-cooldown": 1}), evidence)
        self.assertEqual((s["refused_by_reason"], s["fail_closed"]), ({}, None), evidence)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "index.js")), evidence)
        with open(tool.FAILED_PATH) as fh:
            self.assertEqual(json.load(fh), failed, evidence)

    @unittest.skipIf(IS_ROOT, "chmod does not block root")
    def test_unreadable_subtree_keeps_the_run(self):
        run, ws = self.make_run()
        locked = os.path.join(ws, "locked")
        write(os.path.join(locked, "f.txt"))
        backdate(run)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["planned"], 0)
        self.assertTrue(set(s["kept_by_reason"]) & {"idle-unknown", "scan-error"})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_probe_reporting_unreadable_processes_fails_closed(self):
        _, ws = self.make_run()
        probe = os.path.join(self.tmp, "fake_probe.py")
        write(probe, "import json\nprint(json.dumps({'errors': 1, 'refs': []}))\n")
        self.patch(tool, "live_refs", REAL_LIVE_REFS)
        self.patch(tool, "PROBE_CMD", [PY, probe])
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("process view", s["fail_closed"])
        self.assertIsNone(s.get("manifest"))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_unreadable_run_state_fails_closed(self):
        _, ws = self.make_run()
        self.patch(tool, "db_snapshot", side_effect=tool.Unverifiable("run state unreadable"))
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_missing_runs_root_fails_closed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tool.main(["--runs-root", os.path.join(self.tmp, "absent"), "--apply"])
        self.assertEqual(rc, 2)
        self.assertIn("runs root", json.loads(out.getvalue())["fail_closed"])

    def test_run_state_rows_parse_and_malformed_rows_fail_closed(self):
        ok = subprocess.CompletedProcess([], 0, f"{RID}|failed|t|f\n{RID2}|succeeded|f|f\n", "")
        with mock.patch.object(tool, "run", return_value=ok):
            snap = REAL_DB_SNAPSHOT([RID, RID2])
        self.assertEqual(snap, {RID: ("failed", True, False), RID2: ("succeeded", False, False)})
        bad = subprocess.CompletedProcess([], 0, "garbage\n", "")
        with mock.patch.object(tool, "run", return_value=bad), self.assertRaises(tool.Unverifiable):
            REAL_DB_SNAPSHOT([RID])

    def test_db_block_reasons(self):
        self.assertEqual(tool.db_block(None), "run-unknown")
        self.assertEqual(tool.db_block(("running", False, False)), "run-not-terminal")
        self.assertEqual(tool.db_block(("failed", True, False)), "retry-pending")
        self.assertEqual(tool.db_block(("failed", False, True)), "lease-active")
        self.assertIsNone(tool.db_block(("cancelled", False, False)))
        self.assertIsNone(tool.db_block(("timed_out", False, False)))

    def test_inside_requires_a_path_boundary(self):
        self.assertTrue(tool.inside("/a/b", "/a/b"))
        self.assertTrue(tool.inside("/a/b/c", "/a/b"))
        self.assertFalse(tool.inside("/a/bc", "/a/b"))

    def test_only_canonical_uuid_folders_are_listed(self):
        for name in (RID, "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "not-a-run", RID + "x",
                     "11111111-1111-4111-8111-11111111111"):
            os.makedirs(os.path.join(self.root, name))
        self.assertEqual(tool.run_dir_names(self.root), [RID])

    def test_candidate_names_are_exact_and_git_internals_are_pruned(self):
        run = os.path.join(self.root, RID)
        for name in ("node_modules", "target", ".next", ".turbo", ".pnpm-store", "dist", "venv", ".venv", "build"):
            write(os.path.join(run, "ws", name, "f"))
        write(os.path.join(run, "ws", ".git", "target", "x"))
        found = tool.candidates(run)
        self.assertEqual(sorted(os.path.basename(p) for p in found),
                         sorted(["node_modules", "target", ".next", ".turbo", ".pnpm-store"]))

    def test_probe_runs_isolated_so_the_script_directory_cannot_shadow_json(self):
        self.assertEqual(tool.PROBE_CMD, ["sudo", "-n", "/usr/bin/python3", "-I",
                                          "/usr/local/libexec/paperclip-regen/regen_probe.py"])
        shadow = os.path.join(self.tmp, "shadow")
        marker = os.path.join(self.tmp, "shadowed")
        os.makedirs(shadow)
        write(os.path.join(shadow, "json.py"), "open(%r, 'w').close()\n" % marker)
        shutil.copy(PROBE_FILE, os.path.join(shadow, "regen_probe.py"))
        r = subprocess.run([PY, "-I", os.path.join(shadow, "regen_probe.py")], cwd=shadow,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("refs", json.loads(r.stdout))

    def test_known_folders_with_no_run_state_row_fail_closed(self):
        _, ws = self.make_run()
        self.patch(tool, "db_snapshot", return_value={})
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertEqual(s["fail_closed"], "run state matched no folder")
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_run_state_query_stops_on_sql_error_and_skips_psqlrc(self):
        self.assertIn("ON_ERROR_STOP=1", tool.DB_CMD[-1])
        self.assertIn(" -X ", tool.DB_CMD[-1])

    def test_manifest_that_cannot_be_written_deletes_nothing(self):
        _, ws = self.make_run()
        blocker = os.path.join(self.tmp, "not-a-dir")
        write(blocker)
        self.patch(tool, "MANIFEST_DIR", os.path.join(blocker, "manifests"))
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("manifest not written", s["fail_closed"])
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_log_that_cannot_be_appended_is_reported_not_fatal(self):
        _, ws = self.make_run()
        self.patch(tool, "LOG_PATH", os.path.join(self.tmp, "absent", "actions.log"))
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 2))
        self.assertIsNotNone(s["log_error"])

    def test_firing_that_finds_the_lock_held_is_skipped(self):
        _, ws = self.make_run()
        fd = os.open(tool.LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["skipped"]), (0, "another firing holds the lock"))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_rm_that_hits_its_timeout_is_counted_and_cooled(self):
        run, ws = self.make_run()
        real_op = tool.op

        def fake_op(*cmd, timeout=120):
            if cmd and cmd[0] == tool.TIMEOUT:
                return subprocess.CompletedProcess(cmd, 124, "", "")
            return real_op(*cmd, timeout=timeout)

        self.patch(tool, "op", side_effect=fake_op)
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["refused_by_reason"]), (0, {"rm-timeout": 2}))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        rc, s = self.run_main("--apply")
        self.assertEqual((s["planned"], s["kept_by_reason"]), (0, {"rm-failed-cooldown": 2}))

    def test_missing_binary_is_not_reported_as_a_timeout(self):
        self.assertEqual(tool.run(["/nonexistent/regen-binary"], 5).returncode, 127)

    def test_hard_deadline_refuses_to_spawn_a_process(self):
        self.addCleanup(setattr, tool, "_deadline", None)
        tool._deadline = time.monotonic() - 1
        self.assertTrue(tool.hard_expired())
        self.assertEqual(tool.run(["true"], 5).returncode, 124)

    def test_git_dir_at_any_depth_inside_a_candidate_keeps_it(self):
        run, ws = self.make_run()
        write(os.path.join(ws, "node_modules", "a", "b", "file.txt"))
        os.makedirs(os.path.join(ws, "node_modules", "a", "b", "vendored", ".git"))
        backdate(run)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"nested-repo": 1})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules", "a", "b", "vendored", ".git")))

    def test_df_failure_keeps_the_idle_clock(self):
        _, ws = self.make_run()
        write(os.path.join(ws, "src.txt"), "touched now\n")
        self.patch(tool, "home_pct", return_value=None)
        rc, s = self.run_main("--apply")
        self.assertEqual((s["idle_min"], s["planned"], s["kept_by_reason"]), (10, 0, {"recent-write": 1}))

    def test_write_to_the_folder_after_planning_is_refused(self):
        run, ws = self.make_run()
        late = os.path.join(ws, "late.txt")
        preconditions = {"euid": os.geteuid(), "db_row": self.db[RID],
                         "late_exists_before_plan": os.path.exists(late)}
        commands = []
        real_op = tool.op

        def observed_op(*cmd, timeout=120):
            # Trace every operator command so a plan-time keep (empty plan,
            # empty refusals) stays distinguishable from a revalidation
            # refusal: the find outputs show what each phase saw.
            result = real_op(*cmd, timeout=timeout)
            commands.append({"cmd": cmd, "rc": result.returncode,
                             "stdout": result.stdout, "stderr": result.stderr})
            return result

        self.patch(tool, "op", side_effect=observed_op)
        preconditions["idle_before_plan"] = tool.folder_idle_block(run, tool.IDLE_MINUTES)
        commands.append({"event": "precheck-idle", "result": preconditions["idle_before_plan"]})
        original = tool.revalidate_run
        calls = []

        def wrapper(rid, run_real, idle_min):
            # Revalidation runs only for planned runs: a plan-time keep means
            # this never fires and late.txt never exists, which is exactly how
            # an empty-plan outcome shows itself in the evidence below.
            calls.append(rid)
            commands.append({"event": "revalidate", "call": len(calls)})
            write(late)
            return original(rid, run_real, idle_min)

        self.patch(tool, "revalidate_run", side_effect=wrapper)
        rc, s = self.run_main("--apply")
        evidence = json.dumps({"preconditions": preconditions, "rc": rc,
                               "summary": s, "commands": commands,
                               "revalidate_calls": len(calls),
                               "late_exists_after_apply": os.path.exists(late)}, sort_keys=True)
        self.assertIsNone(preconditions["idle_before_plan"], evidence)
        self.assertEqual((rc, s["idle_min"], s["planned"], s["removed"]), (0, 10, 2, 0), evidence)
        self.assertEqual((s["kept_by_reason"], s["fail_closed"], s["ledger_error"]),
                         ({}, None, None), evidence)
        self.assertEqual(s["refused_by_reason"], {"recent-write": 2}, evidence)
        self.assertEqual((s["archive_verified_runs"], s["archive_pending_runs"]), (0, 1), evidence)
        self.assertEqual(len(calls), 1, evidence)
        self.assertTrue(os.path.exists(late), evidence)
        marker = next(i for i, e in enumerate(commands) if e.get("event") == "revalidate")
        self.assertTrue(any(e.get("stdout", "").strip() for e in commands[marker:]
                            if "-newermt" in e.get("cmd", ())), evidence)
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")), evidence)

    def test_process_running_a_binary_from_the_folder_keeps_the_run(self):
        run, ws = self.make_run()
        exe = os.path.join(ws, "target", "debug", "sleeper")
        shutil.copy("/bin/sleep", exe)
        backdate(run)
        proc = subprocess.Popen([exe, "120"], cwd=self.tmp)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["kept_by_reason"], {"live-process-ref": 1})
        self.assertTrue(os.path.isfile(exe))

    def test_process_that_has_exited_releases_the_run(self):
        _, ws = self.make_run()
        self.spawn("pass", ws).wait()
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["kept_by_reason"]), (2, {}))

    def test_apply_stops_at_its_time_budget_and_at_the_hard_deadline(self):
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")), (RID, run, os.path.join(ws, "target"))]
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() - 1, {})
        self.assertEqual((removed, dict(refused), abort), (0, {"time-budget": 2}, None))
        self.addCleanup(setattr, tool, "_deadline", None)
        tool._deadline = time.monotonic() - 1
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() + 100, {})
        self.assertEqual((removed, dict(refused), abort), (0, {"hard-deadline": 2}, None))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))


    def test_mount_point_candidate_is_kept_and_the_rest_removed(self):
        _, ws = self.make_run()
        mounted = os.path.join(ws, "node_modules")
        real_block = tool.mount_block
        self.patch(tool, "mount_block",
                   side_effect=lambda cand: "mount-point" if cand == mounted else real_block(cand))
        rc, s = self.run_main("--apply")
        self.assertEqual((s["removed"], s["kept_by_reason"]), (1, {"mount-point": 1}))
        self.assertTrue(os.path.isdir(mounted))

    def test_mount_table_matches_escaped_mounts_and_mounts_below_the_candidate(self):
        table = (
            "40 1 0:40 / /srv/store\\040dir/node_modules rw - tmpfs tmpfs rw\n"
            "41 1 0:41 / /srv/tree/node_modules/bind rw - tmpfs tmpfs rw\n"
            "42 1 0:42 /elsewhere /srv/other rw - tmpfs tmpfs rw\n"
        )
        with mock.patch("builtins.open", side_effect=lambda *a, **k: io.StringIO(table)):
            self.assertEqual(tool.mount_block("/srv/store dir/node_modules"), "mount-point")
            self.assertEqual(tool.mount_block("/srv/tree/node_modules"), "mount-point")
            self.assertIsNone(tool.mount_block("/srv/store"))
            self.assertIsNone(tool.mount_block("/elsewhere"))
            self.assertIsNone(tool.mount_block("/srv/other-tree/node_modules"))
        with mock.patch("builtins.open", side_effect=lambda *a, **k: io.StringIO("malformed\n")):
            self.assertEqual(tool.mount_block("/srv/store dir/node_modules"), "mount-table-unreadable")
        with mock.patch("builtins.open", side_effect=OSError("no mount table")):
            self.assertEqual(tool.mount_block("/srv/store dir/node_modules"), "mount-table-unreadable")

    def test_rm_is_bounded_by_the_remaining_hard_budget(self):
        # Archive gate pinned by its own tests; isolate the timing path.
        self.patch(tool, "ensure_archived", return_value=(True, None))
        run, ws = self.make_run()
        cand = os.path.join(ws, "node_modules")
        bounds = []

        def fake_op(*cmd, timeout=120):
            if cmd[0] == tool.TIMEOUT:
                bounds.append(int(cmd[3]))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self.patch(tool, "candidate_block", return_value=None)
        self.patch(tool, "symlink_block", return_value=None)
        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "op", side_effect=fake_op)
        self.addCleanup(setattr, tool, "_deadline", None)
        tool._deadline = time.monotonic() + 10.5
        removed, refused, abort = tool.apply_plan([(RID, run, cand)], 10, time.monotonic() + 100, {})
        self.assertEqual((removed, dict(refused), abort), (1, {}, None))
        self.assertEqual(bounds, [10])
        tool._deadline = time.monotonic() + 0.5
        removed, refused, abort = tool.apply_plan([(RID, run, cand)], 10, time.monotonic() + 100, {})
        self.assertEqual((removed, dict(refused)), (0, {"hard-deadline": 1}))
        self.assertEqual(bounds, [10])
        tool._deadline = None
        tool.apply_plan([(RID, run, cand)], 10, time.monotonic() + 100, {})
        self.assertEqual(bounds, [10, 300])

    def test_time_budget_running_out_between_candidates_keeps_the_rest(self):
        # Archive gate pinned by its own tests; isolate the timing path.
        self.patch(tool, "ensure_archived", return_value=(True, None))
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")), (RID, run, os.path.join(ws, "target"))]
        clock = {"now": 0.0}

        def fake_op(*cmd, timeout=120):
            clock["now"] += 100.0
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self.patch(tool, "time", types.SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
        self.patch(tool, "candidate_block", return_value=None)
        self.patch(tool, "symlink_block", return_value=None)
        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "op", side_effect=fake_op)
        removed, refused, abort = tool.apply_plan(plan, 10, 50.0, {})
        self.assertEqual((removed, dict(refused), abort), (1, {"time-budget": 1}, None))

    def test_hard_deadline_running_out_between_candidates_keeps_the_rest(self):
        # Archive gate pinned by its own tests; isolate the timing path.
        self.patch(tool, "ensure_archived", return_value=(True, None))
        run, ws = self.make_run()
        plan = [(RID, run, os.path.join(ws, "node_modules")), (RID, run, os.path.join(ws, "target"))]
        clock = {"now": 0.0}
        examined = []

        def fake_op(*cmd, timeout=120):
            clock["now"] += 600.0
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self.patch(tool, "time", types.SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
        self.patch(tool, "candidate_block", side_effect=lambda run_real, cand, idle_min: examined.append(cand))
        self.patch(tool, "symlink_block", return_value=None)
        self.patch(tool, "revalidate_run", return_value=None)
        self.patch(tool, "op", side_effect=fake_op)
        self.addCleanup(setattr, tool, "_deadline", None)
        tool._deadline = 500.0
        removed, refused, abort = tool.apply_plan(plan, 10, 10 ** 9, {})
        self.assertEqual((removed, dict(refused), abort), (1, {"hard-deadline": 1}, None))
        self.assertEqual(examined, [plan[0][2]])

    def test_time_budget_is_reported_before_the_run_is_revalidated(self):
        run, ws = self.make_run(lease=True)
        plan = [(RID, run, os.path.join(ws, "node_modules")), (RID, run, os.path.join(ws, "target"))]
        removed, refused, abort = tool.apply_plan(plan, 10, time.monotonic() - 1, {})
        self.assertEqual((removed, dict(refused), abort), (0, {"time-budget": 2}, None))


    # Archive-before-delete (case_90645769): the uploader succeeding is never
    # enough; only a re-download-verified sha plus a ledger record makes a
    # run eligible, and anything unprovable keeps its local paths.

    def archive_puts(self):
        path = os.path.join(self.store, "puts.log")
        if not os.path.isfile(path):
            return 0
        with open(path) as fh:
            return fh.read().count("put\n")

    def archive_gets(self):
        path = os.path.join(self.store, "gets.log")
        if not os.path.isfile(path):
            return 0
        with open(path) as fh:
            return fh.read().count("get\n")

    def ledger_lines(self):
        with open(os.path.join(self.tmp, "manifests",
                               "regen-strip-archive-records.tsv")) as fh:
            return [line for line in fh.read().splitlines() if line.strip()]

    def test_apply_uploads_verifies_and_records_before_removal(self):
        _, ws = self.make_run()
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 2))
        self.assertEqual(self.archive_puts(), 1)
        self.assertGreaterEqual(self.archive_gets(), 1)
        lines = self.ledger_lines()
        self.assertEqual(len(lines), 1)
        rid, name, sha, _stamp = lines[0].split("\t")
        self.assertEqual(rid, RID)
        self.assertTrue(name.startswith("regen-strip/") and name.endswith(".tar.zst"))
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertEqual((s["archive_verified_runs"], s["archive_pending_runs"]), (1, 0))
        blob = os.path.join(self.store, name.replace("/", "_") + ".bin")
        with open(blob, "rb") as fh:
            self.assertEqual(sha, hashlib.sha256(fh.read()).hexdigest())

    def test_archive_upload_failure_retains_every_local_path(self):
        _, ws = self.make_run()
        with mock.patch.dict(os.environ, {"FAIL_PUT": "1"}):
            rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 0))
        self.assertEqual(s["refused_by_reason"], {"archive-unverified": 2})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_archive_readback_mismatch_retains_every_local_path(self):
        # The upload "succeeds" but the re-downloaded bytes hash differently.
        _, ws = self.make_run()
        with mock.patch.dict(os.environ, {"POISON_GET": "1"}):
            rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 0))
        self.assertEqual(s["refused_by_reason"], {"archive-unverified": 2})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv")))

    def assert_no_doubled_ops(self, path):
        parts = os.path.normpath(path).split(os.sep)
        self.assertNotIn(("ops", "ops"), list(zip(parts, parts[1:])))

    def test_default_archive_lib_path_has_no_doubled_ops(self):
        self.assertTrue(self.default_archive_lib.endswith(
            os.path.join("ops", "offsite", "archive_lib.sh")))
        self.assert_no_doubled_ops(self.default_archive_lib)

    def test_doubled_ops_guard_matches_components_not_repository_suffix(self):
        # Hosted checkout names end in -ops; that is not an ops directory.
        self.assert_no_doubled_ops(os.path.join(
            os.sep, "tmp", "paperclip-ops", "paperclip-ops", "ops", "offsite", "archive_lib.sh"))
        with self.assertRaises(AssertionError):
            self.assert_no_doubled_ops(os.path.join(
                os.sep, "tmp", "paperclip-ops", "ops", "ops", "offsite", "archive_lib.sh"))

    def test_seeded_archive_record_skips_reupload(self):
        _, ws = self.make_run()
        os.makedirs(os.path.join(self.tmp, "manifests"))
        write(os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv"),
              "%s\tregen-strip/2026-10-11/%s-20261011T000000Z.tar.zst\t%s\t20261011T000000Z\n"
              % (RID, RID, "ab" * 32))
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 2))
        self.assertEqual(self.archive_puts(), 0)
        self.assertEqual((s["archive_verified_runs"], s["archive_pending_runs"]), (1, 0))

    def test_foreign_class_ledger_line_is_rejected(self):
        self.make_run()
        os.makedirs(os.path.join(self.tmp, "manifests"))
        write(os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv"),
              "%s\tworktrees/2026-10-11/retire-x.tar.zst\t%s\t20261011T000000Z\n"
              % (RID, "ab" * 32))
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("archive ledger", s["fail_closed"])
        self.assertEqual(s["removed"], 0)

    def test_run_turning_live_during_archive_is_refused(self):
        run, ws = self.make_run()

        def archive_then_start(_rid, _run_real, _stamp, _deadline):
            self.spawn("import time; time.sleep(30)", run)
            return True, None

        self.patch(tool, "ensure_archived", side_effect=archive_then_start)
        rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 0))
        self.assertEqual(s["refused_by_reason"], {"live-process-ref": 2})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))

    def test_readback_transport_failure_retains_every_local_path(self):
        _, ws = self.make_run()
        with mock.patch.dict(os.environ, {"FAIL_GET_RC": "1"}):
            rc, s = self.run_main("--apply")
        self.assertEqual((rc, s["removed"]), (0, 0))
        self.assertEqual(s["refused_by_reason"], {"archive-unverified": 2})
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv")))

    def test_ensure_archived_with_no_budget_refuses_without_uploading(self):
        run, _ws = self.make_run()
        covered, why = tool.ensure_archived(RID, run, "20261011T000000Z", time.monotonic() - 1)
        self.assertEqual((covered, why), (False, "time-budget"))
        self.assertEqual(self.archive_puts(), 0)

    def test_missing_archive_lib_aborts_apply_fail_closed(self):
        self.make_run()
        self.patch(tool, "ARCHIVE_LIB", os.path.join(self.tmp, "no-such-lib.sh"))
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("archive lib", s["fail_closed"])
        self.assertEqual(s["removed"], 0)

    def test_malformed_archive_ledger_aborts_apply_fail_closed(self):
        self.make_run()
        os.makedirs(os.path.join(self.tmp, "manifests"))
        write(os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv"),
              "not\ta\tledger\n")
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("archive ledger", s["fail_closed"])
        self.assertEqual(s["removed"], 0)

    def test_apply_malformed_archive_ledger_aborts_with_empty_plan(self):
        _, ws = self.make_run()
        os.makedirs(os.path.join(self.tmp, "manifests"))
        write(os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv"),
              "not\ta\tledger\n")
        rc, s = self.run_main("--apply", "--max-remove", "0")
        self.assertEqual(rc, 2)
        self.assertIn("archive ledger", s["fail_closed"])
        self.assertEqual((s["planned"], s["removed"]), (0, 0))
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_apply_malformed_archive_ledger_aborts_when_revalidation_refuses_every_candidate(self):
        run, ws = self.make_run()
        os.makedirs(os.path.join(self.tmp, "manifests"))
        write(os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv"),
              "not\ta\tledger\n")
        self.mutate_before_revalidation(run, lambda: self.db.__setitem__(RID, ("running", False, False)))
        rc, s = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("archive ledger", s["fail_closed"])
        self.assertEqual(s["removed"], 0)
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_dry_run_malformed_archive_ledger_reports_pending_without_abort(self):
        _, ws = self.make_run()
        os.makedirs(os.path.join(self.tmp, "manifests"))
        write(os.path.join(self.tmp, "manifests", "regen-strip-archive-records.tsv"),
              "not\ta\tledger\n")
        rc, s = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIsNone(s["fail_closed"])
        self.assertEqual(s["removed"], 0)
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_apply_empty_plan_with_missing_ledger_succeeds(self):
        _, ws = self.make_run()
        rc, s = self.run_main("--apply", "--max-remove", "0")
        self.assertEqual((rc, s["planned"], s["removed"]), (0, 0, 0))
        self.assertIsNone(s["fail_closed"])
        self.assertTrue(os.path.isdir(os.path.join(ws, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(ws, "target")))

    def test_dry_run_reports_coverage_without_uploading(self):
        run, ws = self.make_run()
        rc, s = self.run_main()
        self.assertEqual((rc, s["planned"]), (0, 2))
        self.assertEqual((s["archive_verified_runs"], s["archive_pending_runs"]), (0, 1))
        self.assertEqual(self.archive_puts(), 0)
        rc, s = self.run_main("--apply")
        self.assertEqual(s["removed"], 2)
        write(os.path.join(ws, "node_modules", "pkg", "index2.js"))
        backdate(run)
        rc, s = self.run_main()
        self.assertEqual((s["archive_verified_runs"], s["archive_pending_runs"]), (1, 0))
        self.assertEqual(self.archive_puts(), 1)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RegenSqlPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_bin = os.environ.get("REGEN_PG_BIN", "/usr/lib/postgresql/16/bin")
        cls.tools = {name: os.path.join(pg_bin, name) for name in ("initdb", "pg_ctl", "psql")}
        if not all(os.path.exists(path) for path in cls.tools.values()):
            message = f"PostgreSQL binaries not found in {pg_bin}"
            if os.environ.get("REGEN_REQUIRE_POSTGRES") == "1":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        cls.tmp = tempfile.mkdtemp(prefix="regen-pg-")
        cls.data = os.path.join(cls.tmp, "data")
        cls.port = free_port()
        cls.env = dict(os.environ, PGHOST="127.0.0.1", PGPORT=str(cls.port), PGUSER="postgres", PGDATABASE="postgres")
        try:
            subprocess.run([cls.tools["initdb"], "-D", cls.data, "-U", "postgres", "-A", "trust", "-E", "UTF8"],
                           check=True, capture_output=True)
            subprocess.run([cls.tools["pg_ctl"], "-D", cls.data, "-w", "-l", os.path.join(cls.tmp, "pg.log"),
                            "-o", f"-p {cls.port} -h 127.0.0.1 -k {cls.tmp} -c dynamic_shared_memory_type=mmap", "start"],
                           check=True, capture_output=True)
            cls.psql("""
                create table heartbeat_runs (id uuid primary key, status text not null, retry_of_run_id uuid);
                create table environment_leases (id uuid primary key default gen_random_uuid(),
                    heartbeat_run_id uuid not null, status text not null, expires_at timestamptz);
            """)
        except BaseException:
            cls.stop()
            raise

    @classmethod
    def stop(cls):
        subprocess.run([cls.tools["pg_ctl"], "-D", cls.data, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    @classmethod
    def psql(cls, sql):
        r = subprocess.run([cls.tools["psql"], "-X", "-v", "ON_ERROR_STOP=1", "-At"], input=sql,
                           env=cls.env, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        return r.stdout

    def test_snapshot_sql_decides_each_gate_on_real_rows(self):
        ids = {name: str(uuid.uuid4()) for name in (
            "ok", "lease_null", "lease_future", "lease_past", "lease_released",
            "retry_sched", "retry_running", "retry_done", "running", "missing")}
        child = {name: str(uuid.uuid4()) for name in ("sched", "running", "done")}
        self.psql("\n".join([
            *(f"insert into heartbeat_runs (id, status) values ('{ids[n]}', 'failed');"
              for n in ("ok", "lease_null", "lease_future", "lease_past", "lease_released",
                        "retry_sched", "retry_running", "retry_done")),
            f"insert into heartbeat_runs (id, status) values ('{ids['running']}', 'running');",
            f"insert into heartbeat_runs (id, status, retry_of_run_id) values ('{child['sched']}', 'scheduled_retry', '{ids['retry_sched']}');",
            f"insert into heartbeat_runs (id, status, retry_of_run_id) values ('{child['running']}', 'running', '{ids['retry_running']}');",
            f"insert into heartbeat_runs (id, status, retry_of_run_id) values ('{child['done']}', 'succeeded', '{ids['retry_done']}');",
            f"insert into environment_leases (heartbeat_run_id, status, expires_at) values ('{ids['lease_null']}', 'active', null);",
            f"insert into environment_leases (heartbeat_run_id, status, expires_at) values ('{ids['lease_future']}', 'active', now() + interval '1 day');",
            f"insert into environment_leases (heartbeat_run_id, status, expires_at) values ('{ids['lease_past']}', 'active', now() - interval '1 day');",
            f"insert into environment_leases (heartbeat_run_id, status, expires_at) values ('{ids['lease_released']}', 'released', null);",
        ]))
        expected = {
            ids["ok"]: ("failed", False, False),
            ids["lease_null"]: ("failed", False, True),
            ids["lease_future"]: ("failed", False, True),
            ids["lease_past"]: ("failed", False, False),
            ids["lease_released"]: ("failed", False, False),
            ids["retry_sched"]: ("failed", True, False),
            ids["retry_running"]: ("failed", True, False),
            ids["retry_done"]: ("failed", False, False),
            ids["running"]: ("running", False, False),
        }
        out = self.psql(tool.snapshot_sql(list(expected) + [ids["missing"]]))
        self.assertEqual(tool.parse_snapshot(out), expected)


class ParseArgsTest(unittest.TestCase):
    def test_apply_abbreviations_are_rejected(self):
        # allow_abbrev=False: --a/--ap/--appl must not mean --apply, or the
        # dry-run wrapper's refusal could be bypassed before reaching it.
        for abbr in ("--a", "--ap", "--appl", "--apply=x"):
            with self.assertRaises(SystemExit, msg=abbr):
                with contextlib.redirect_stderr(io.StringIO()):
                    tool.parse_args([abbr])

    def test_full_apply_still_parses(self):
        self.assertTrue(tool.parse_args(["--apply"]).apply)
        self.assertFalse(tool.parse_args([]).apply)


class SudoGrantTest(RegenTest):
    """Archive identity boundary: orchestration as invoking user, tar as operator.

    The rest of the suite runs with OP_PREFIX=[] so sudo is never executed.
    These tests keep the real prefix and stub only the sudo binary itself:
    the stub permits exactly the absolute binaries the runbook grants
    paperclip-operator (realpath, find, git, rm, timeout, tar) and refuses
    everything else, mirroring a host sudoers file. Archive orchestration
    (source <lib>; offsite_put/get) runs as the invoking unit user with no
    sudo, so the host-verified unreadable ubuntu lib as operator
    (test -r exit 1) no longer blocks; only the tar producer uses sudo.
    There is no bash-as-operator grant.
    """

    GRANT = ("/usr/bin/realpath", "/usr/bin/find", "/usr/bin/git",
             "/usr/bin/rm", "/usr/bin/timeout", "/usr/bin/tar")

    def run_with_stub_sudo(self):
        bindir = os.path.join(self.tmp, "stubbin")
        os.makedirs(bindir)
        granted = " ".join(self.GRANT)
        write(os.path.join(bindir, "sudo"),
              "#!/usr/bin/env bash\n"
              "if [[ \"$1\" != '-n' || \"$2\" != '-u' || \"$3\" != 'paperclip-operator' ]]; then\n"
              "  echo 'stub-sudo: only -n -u paperclip-operator' >&2; exit 1\n"
              "fi\n"
              "shift 3\n"
              "case \" %s \" in\n"
              "  *\" $1 \"*) exec \"$@\" ;;\n"
              "esac\n"
              "echo \"stub-sudo: refused [$1]\" >&2; exit 1\n" % granted)
        os.chmod(os.path.join(bindir, "sudo"), 0o755)
        path_patch = mock.patch.dict(
            os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]})
        path_patch.start()
        self.addCleanup(path_patch.stop)
        self.patch(tool, "OP_PREFIX", ["sudo", "-n", "-u", "paperclip-operator"])

    def test_archive_round_trip_under_documented_grant(self):
        self.run_with_stub_sudo()
        tool.check_archive_lib()
        run, _ws = self.make_run()
        covered, why = tool.ensure_archived(RID, run, "20261011T000000Z",
                                           time.monotonic() + 300)
        self.assertEqual((covered, why), (True, None))
        self.assertEqual(self.archive_puts(), 1)
        self.assertGreaterEqual(self.archive_gets(), 1)
        self.assertEqual(len(self.ledger_lines()), 1)

    def test_operator_bash_refused_still_archives(self):
        # No bash-as-operator grant exists: a sudo stub refusing
        # /usr/bin/bash must NOT block the archive, because orchestration
        # runs as the invoking user and only tar uses sudo.
        self.run_with_stub_sudo()
        tool.check_archive_lib()
        run, _ws = self.make_run()
        covered, why = tool.ensure_archived(RID, run, "20261011T000000Z",
                                           time.monotonic() + 300)
        self.assertEqual((covered, why), (True, None))

    def test_archive_orchestration_uses_invoking_user_not_operator(self):
        # check/upload/readback must call run() (invoking ubuntu publisher),
        # never op() (operator sudo). A host operator that cannot read the
        # ubuntu lib (test -r exit 1) still archives.
        calls = []

        def fake_run(cmd, timeout, stdin=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "ab" * 32 + "\n", "")

        with mock.patch.object(tool, "run", side_effect=fake_run):
            with mock.patch.object(
                    tool, "op",
                    side_effect=AssertionError("archive must not use operator sudo")):
                tool.check_archive_lib()
                sha = tool.offsite_upload(
                    "regen-strip/2026-10-11/%s-20261011T000000Z.tar.zst" % RID,
                    self.tmp, "offsite-store", 60)
                self.assertRegex(sha, r"^[0-9a-f]{64}$")
                digest = tool.offsite_readback_sha(
                    "regen-strip/2026-10-11/%s-20261011T000000Z.tar.zst" % RID, 60)
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertGreaterEqual(len(calls), 3)
        for cmd in calls:
            self.assertEqual(cmd[0], tool.BASH)

    def test_tar_producer_runs_as_constrained_operator(self):
        # The offsite_put producer must be the existing constrained
        # `sudo -n -u paperclip-operator /usr/bin/tar` with the same
        # excludes; the outer orchestration itself is unsudoed BASH.
        self.patch(tool, "OP_PREFIX", ["sudo", "-n", "-u", "paperclip-operator"])
        captured = {}

        def fake_run(cmd, timeout, stdin=None):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "ab" * 32 + "\n", "")

        with mock.patch.object(tool, "run", side_effect=fake_run):
            sha = tool.offsite_upload(
                "regen-strip/2026-10-11/%s-20261011T000000Z.tar.zst" % RID,
                "/parent", "base-uuid", 60)
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], tool.BASH)
        self.assertEqual(cmd[1], "-c")
        script = cmd[2]
        self.assertIn("source ", script)
        self.assertIn("offsite_put", script)
        self.assertIn("sudo", script)
        self.assertIn("paperclip-operator", script)
        self.assertIn(tool.TAR_BIN, script)
        for exc in tool.TAR_EXCLUDES:
            self.assertIn(exc, script)
        self.assertNotIn("bash -c", script.split("offsite_put", 1)[1])


if __name__ == "__main__":
    unittest.main()
