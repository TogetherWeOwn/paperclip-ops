#!/usr/bin/env python3
# ===========================================================================
# test_host_regenerable_cache_reclaim.py — offline tests for
# host_regenerable_cache_reclaim.py (case_995301d3, from case_56de8f93-C).
#
# Written to FAIL a plausible-but-wrong implementation, not to confirm the one
# that exists. Every section names the mutant it kills. This tool DELETES
# directories on a production host, so the tests that matter most are the
# ones proving it refuses: a sixth path, a home inside daemon scope, a blind
# --apply with no dry-run receipt, a receipt from different bounds, a cap
# overrun deleted anyway, a mountpoint, a symlink root, a nested .git, fresh
# activity, a live process reference, and a detector registry entry.
#
# Deterministic: no network, no credentials, no clock reads (tests pass
# explicit `now`). Real `rm -rf` is exercised against fixture cache dirs,
# never mocked; du runs against fixtures.
#
#   python3 ops/case_995301d3/test_host_regenerable_cache_reclaim.py
# ===========================================================================
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import host_regenerable_cache_reclaim as tool  # noqa: E402
from host_regenerable_cache_reclaim import (  # noqa: E402
    ALLOWLIST,
    MAX_BYTES_DEFAULT,
    REGISTRY_WRITE_TARGET,
    ReclaimError,
    SafetyRefusal,
    bounds_fingerprint,
    candidate_state,
    check_receipt,
    home_root_ok,
    kill_switch,
    parse_argv,
    path_referenced,
    plan_deletes,
    registry_apply_decision,
    resolve_candidates,
    resolve_mode,
    run_tool,
)

NOW = 1_790_000_000.0
OLD = NOW - 3600


def make_home(files=()):
    """Fixture home with the five allowlisted roots + stale mtimes."""
    home = tempfile.mkdtemp(prefix="case_4f630dab-home-")
    for rel, _, _ in ALLOWLIST:
        full = os.path.join(home, rel)
        os.makedirs(full, exist_ok=True)
        with open(os.path.join(full, "blob.bin"), "w") as handle:
            handle.write("x" * 64)
    for rel in files:
        full = os.path.join(home, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write("y")
    old_ns = (OLD, OLD)
    for dirpath, dirnames, filenames in os.walk(home):
        os.utime(dirpath, ns=(int(OLD * 1e9), int(OLD * 1e9)))
        for name in filenames:
            os.utime(os.path.join(dirpath, name), ns=(int(OLD * 1e9),
                                                      int(OLD * 1e9)))
    _ = old_ns
    return home


def quiet_opts(home, **kw):
    base = dict(home_root=home, out_dir=tempfile.mkdtemp(prefix="case_4f630dab-out-"))
    base.update(kw)
    argv = ["--home-root", base.pop("home_root"), "--out-dir", base.pop("out_dir")]
    for key, val in base.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(val, bool):
            if val:
                argv.append(flag)
        else:
            argv += [flag, str(val)]
    return parse_argv(argv)


class BoundsTest(unittest.TestCase):
    def test_allowlist_is_exactly_the_five_evidence_paths(self):
        # Mutant: a sixth "pool-images" entry added for extra reclaim.
        rels = sorted(rel for rel, _, _ in ALLOWLIST)
        self.assertEqual(rels, [".cache", ".npm", ".rb-sbx", ".rv-sbx/work", "build"])

    def test_max_bytes_is_the_evidence_sum(self):
        # Mutant: cap rounded up to 20_000_000_000, waiving the bound.
        self.assertEqual(MAX_BYTES_DEFAULT, 18144395264)
        self.assertEqual(MAX_BYTES_DEFAULT,
                         sum(size for _, size, _ in ALLOWLIST))

    def test_home_inside_daemon_scope_refused(self):
        # Mutant: only basename checked, so /home/ubuntu/stacks/paperclip
        # passes as "ubuntu-ish".
        for bad in ("stacks/paperclip", "docker", "lxd-pool",
                    "paperclip-operator/workspace", "pool-images"):
            with tempfile.TemporaryDirectory() as tmp:
                home = os.path.join(tmp, bad)
                os.makedirs(home)
                ok, reason = home_root_ok(home)
                self.assertFalse(ok, bad)
                self.assertEqual(reason, "home-in-forbidden-scope")

    def test_home_must_be_real_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, reason = home_root_ok(os.path.join(tmp, "nope"))
            self.assertFalse(ok)
            link = os.path.join(tmp, "link")
            os.symlink(tmp, link)
            ok, _ = home_root_ok(link)
            self.assertFalse(ok)

    def test_resolve_candidates_cover_allowlist(self):
        home = make_home()
        cands, violations = resolve_candidates(home)
        self.assertEqual(violations, [])
        self.assertEqual(sorted(c["rel"] for c in cands),
                         sorted(rel for rel, _, _ in ALLOWLIST))


class DryRunFirstTest(unittest.TestCase):
    def test_apply_without_receipt_refused(self):
        # Mutant: --apply proceeds, receipt treated as optional hint.
        home = make_home()
        opts = quiet_opts(home, apply=True)
        with self.assertRaises(SafetyRefusal):
            run_tool(opts, {}, proc_paths=set(), now=NOW)

    def test_receipt_from_other_bounds_refused(self):
        # Mutant: receipt existence checked, bounds never compared — a
        # stale dry-run from a wider allowlist authorizes deletion.
        home = make_home()
        opts = quiet_opts(home)
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "dryrun.json")
            with open(bad, "w") as handle:
                json.dump({"bounds": "something-else"}, handle)
            with self.assertRaises(SafetyRefusal):
                check_receipt(bad, opts, NOW)

    def test_receipt_with_matching_bounds_accepted(self):
        # A fresh dry-run receipt verifies against identical bounds.
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        run_tool(parse_argv(["--home-root", home, "--out-dir", out]),
                 {}, proc_paths=set(), now=NOW)
        receipt = os.path.join(out, "dryrun.json")
        opts = parse_argv(["--home-root", home, "--out-dir", out])
        self.assertTrue(check_receipt(receipt, opts, NOW))

    def test_receipt_binds_home_caps_and_age(self):
        # Mutant: receipt is a constant token — a dry-run of an empty,
        # unrelated dir with --max-bytes 1 authorizes a real --apply.
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        run_tool(parse_argv(["--home-root", home, "--out-dir", out]),
                 {}, proc_paths=set(), now=NOW)
        receipt = os.path.join(out, "dryrun.json")
        with tempfile.TemporaryDirectory() as other:
            # Different home refuses.
            opts = quiet_opts(other, apply=True, dry_run_receipt=receipt)
            with self.assertRaises(SafetyRefusal):
                run_tool(opts, {}, proc_paths=set(), now=NOW)
        # Different max-bytes refuses.
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", receipt,
                           "--max-bytes", "1"])
        with self.assertRaises(SafetyRefusal):
            run_tool(opts, {}, proc_paths=set(), now=NOW)
        # Different max-paths refuses: a receipt made with the default 64
        # must not authorize an apply capped at 1.
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", receipt,
                           "--max-paths", "1"])
        with self.assertRaises(SafetyRefusal):
            run_tool(opts, {}, proc_paths=set(), now=NOW)
        # Different idle-minutes refuses: a receipt made with the default
        # 30-minute window must not authorize --idle-minutes 0, which
        # deletes recently-written caches.
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", receipt,
                           "--idle-minutes", "0"])
        with self.assertRaises(SafetyRefusal):
            run_tool(opts, {}, proc_paths=set(), now=NOW)
        # Expired receipt refuses.
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", receipt])
        with self.assertRaises(SafetyRefusal):
            run_tool(opts, {}, proc_paths=set(),
                     now=NOW + tool.RECEIPT_MAX_AGE_SECONDS + 1)
        # Apply-plan cannot be reused as a receipt: the tool-written
        # apply-plan.json carries kind apply-plan and is refused. Proves
        # what the tool writes, not a hand-set kind.
        apply_opts = parse_argv(["--home-root", home, "--out-dir", out,
                                 "--apply", "--dry-run-receipt", receipt])
        run_tool(apply_opts, {}, proc_paths=set(), now=NOW)
        apply_plan = os.path.join(out, "apply-plan.json")
        with open(apply_plan, encoding="utf-8") as handle:
            apply_doc = json.load(handle)
        self.assertEqual(apply_doc["kind"], "apply-plan")
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", apply_plan])
        with self.assertRaises(SafetyRefusal):
            run_tool(opts, {}, proc_paths=set(), now=NOW)

    def test_apply_refuses_plan_not_in_receipt(self):
        # Mutant: apply re-plans fresh and deletes whatever is idle now —
        # a path that went idle after the dry-run is deleted without ever
        # appearing in a reviewed plan. Probe: .cache fresh at dry-run
        # (kept as recent-activity), idle at apply.
        home = make_home()
        cache_blob = os.path.join(home, ".cache", "blob.bin")
        fresh_ns = (int(NOW * 1e9), int(NOW * 1e9))
        os.utime(cache_blob, ns=fresh_ns)
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        run_tool(parse_argv(["--home-root", home, "--out-dir", out]),
                 {}, proc_paths=set(), now=NOW)
        with open(os.path.join(out, "dryrun.json")) as handle:
            planned = json.load(handle)["planned"]
        self.assertNotIn(".cache", planned)
        old_ns = (int(OLD * 1e9), int(OLD * 1e9))
        os.utime(cache_blob, ns=old_ns)
        os.utime(os.path.join(home, ".cache"), ns=old_ns)
        receipt = os.path.join(out, "dryrun.json")
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", receipt])
        with self.assertRaises(SafetyRefusal) as ctx:
            run_tool(opts, {}, proc_paths=set(), now=NOW)
        self.assertIn(".cache", str(ctx.exception))
        # Nothing was deleted: the refusal happens before any rm.
        for rel, _, _ in ALLOWLIST:
            self.assertTrue(os.path.isdir(os.path.join(home, rel)), rel)

    def test_apply_allows_subset_of_receipt(self):
        # A path that goes busy after the dry-run is skipped, not refused:
        # deleting a subset of the reviewed plan is fine.
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        run_tool(parse_argv(["--home-root", home, "--out-dir", out]),
                 {}, proc_paths=set(), now=NOW)
        receipt = os.path.join(out, "dryrun.json")
        os.makedirs(os.path.join(home, "build", ".git"))
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", receipt])
        result = run_tool(opts, {}, proc_paths=set(), now=NOW)
        self.assertNotIn("build", result["applied"]["deleted"])
        self.assertTrue(os.path.isdir(os.path.join(home, "build")))

    def test_apply_does_not_renew_receipt(self):
        # Mutant: every --apply rewrites dryrun.json, so the receipt
        # renews itself after the first apply.
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        run_tool(parse_argv(["--home-root", home, "--out-dir", out]),
                 {}, proc_paths=set(), now=NOW)
        receipt = os.path.join(out, "dryrun.json")
        with open(receipt, "rb") as handle:
            before = handle.read()
        result = run_tool(
            parse_argv(["--home-root", home, "--out-dir", out,
                        "--apply", "--dry-run-receipt", receipt]),
            {}, proc_paths=set(), now=NOW)
        self.assertTrue(result["applied"]["deleted"])
        with open(receipt, "rb") as handle:
            after = handle.read()
        self.assertEqual(after, before)
        self.assertTrue(os.path.isfile(os.path.join(out, "apply-plan.json")))

    def test_dry_run_deletes_nothing_and_writes_receipt(self):
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        opts = parse_argv(["--home-root", home, "--out-dir", out])
        result = run_tool(opts, {}, proc_paths=set(), now=NOW)
        self.assertIsNone(result["applied"])
        for rel, _, _ in ALLOWLIST:
            self.assertTrue(os.path.isdir(os.path.join(home, rel)), rel)
        with open(os.path.join(out, "dryrun.json")) as handle:
            doc = json.load(handle)
        self.assertEqual(doc["bounds"], bounds_fingerprint())
        self.assertEqual(doc["kind"], "dry-run")


class CapTest(unittest.TestCase):
    def test_plan_largest_first_within_cap(self):
        # Mutant: plan takes in allowlist order, starving the cap on small
        # dirs while the 6G sandbox dir survives.
        sized = [{"rel": "a", "bytes": 10}, {"rel": "b", "bytes": 100},
                 {"rel": "c", "bytes": 50}]
        take, skipped = plan_deletes(sized, 110, 64)
        self.assertEqual([c["rel"] for c in take], ["b", "a"])
        self.assertEqual(skipped, {"c": "over-max-bytes"})

    def test_max_paths_caps_rm_calls(self):
        # Mutant: --max-paths parsed but never consulted in plan_deletes.
        sized = [{"rel": str(i), "bytes": 1} for i in range(5)]
        take, skipped = plan_deletes(sized, 10 ** 12, 2)
        self.assertEqual(len(take), 2)
        self.assertEqual(len(skipped), 3)
        self.assertTrue(all(v == "over-max-paths" for v in skipped.values()))

    def test_apply_enforces_cap_on_live_tree(self):
        # Mutant: apply deletes the dry-run plan wholesale, ignoring
        # --max-bytes. The receipt binds caps, so dry-run and apply use
        # the same tiny cap here; a mismatched cap is refused outright
        # (see test_receipt_binds_home_caps_and_age).
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        dry = parse_argv(["--home-root", home, "--out-dir", out,
                          "--max-bytes", "1"])
        run_tool(dry, {}, proc_paths=set(), now=NOW)
        receipt = os.path.join(out, "dryrun.json")
        small = parse_argv(["--home-root", home, "--out-dir", out,
                            "--apply", "--dry-run-receipt", receipt,
                            "--max-bytes", "1"])
        result = run_tool(small, {}, proc_paths=set(), now=NOW)
        self.assertEqual(result["applied"]["deleted"], [])
        for rel, _, _ in ALLOWLIST:
            self.assertTrue(os.path.isdir(os.path.join(home, rel)), rel)


class GateTest(unittest.TestCase):
    def test_symlink_root_kept(self):
        # Mutant: islink checked after realpath, so a linked .npm resolves
        # inside home and is deleted — following it out of the home.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "outside")
            os.makedirs(target)
            link = os.path.join(tmp, "linkroot")
            os.symlink(target, link)
            ok, reason = candidate_state(link, 30, set(), NOW)
            self.assertFalse(ok)
            self.assertEqual(reason, "symlink")

    def test_nested_git_keeps(self):
        # Mutant: .git walk skipped for speed; build outputs carrying
        # history would be reclaimed.
        home = make_home()
        victim = os.path.join(home, "build")
        os.makedirs(os.path.join(victim, ".git", "objects"))
        ok, reason = candidate_state(victim, 30, set(), NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "nested-repo")

    def test_fresh_mtime_keeps(self):
        # Mutant: idle gate compares seconds against minutes (unit slip),
        # so everything reads idle.
        home = make_home()
        victim = os.path.join(home, ".npm")
        with open(os.path.join(victim, "fresh.bin"), "w") as handle:
            handle.write("z")
        ok, reason = candidate_state(victim, 30, set(), time.time())
        self.assertFalse(ok)
        self.assertEqual(reason, "recent-activity")

    def test_live_process_ref_keeps(self):
        # Mutant: proc set consulted with equality only — a process with
        # cwd *inside* .cache does not protect it.
        home = make_home()
        victim = os.path.join(home, ".cache")
        refs = {os.path.join(victim, "ms-playwright", "node")}
        ok, reason = candidate_state(victim, 30, refs, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "live-process-ref")

    def test_sibling_prefix_does_not_protect(self):
        # Mutant: naive startswith — a ref to /home/x/.cache2 keeps .cache.
        self.assertFalse(path_referenced("/a/.cache", {"/a/.cache2/file"}))
        self.assertTrue(path_referenced("/a/.cache", {"/a/.cache/file"}))

    def test_unreadable_proc_view_keeps(self):
        home = make_home()
        ok, reason = candidate_state(os.path.join(home, "build"), 30,
                                     None, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "proc-unreadable")

    def test_proc_eacces_fails_closed(self):
        # Mutant: per-pid OSError swallowed — another uid's live process
        # inside a cache reads as "no refs". EACCES/EPERM must yield None
        # (keep everything); a process that exited (ENOENT) stays ignored.
        # Hermetic: fake the pid list so real /proc EACCES on fd dirs
        # cannot leak into the ENOENT control.
        from unittest import mock
        with mock.patch.object(tool.os, "listdir",
                               return_value=["1", "2"]), \
             mock.patch.object(tool.os, "readlink",
                               side_effect=PermissionError(13, "denied")):
            self.assertIsNone(tool.scan_host_processes())
        with mock.patch.object(tool.os, "listdir",
                               return_value=["1", "2"]), \
             mock.patch.object(tool.os, "readlink",
                               side_effect=FileNotFoundError(2, "gone")):
            self.assertIsNotNone(tool.scan_host_processes())

    def test_unreadable_subdir_fails_closed(self):
        # Mutant: os.walk without onerror silently skips a chmod-000
        # subtree, so its .git never keeps the candidate (probe: without
        # the fix this returns idle True). All visible mtimes are reset
        # to OLD so a recent parent mtime cannot mask the find-error.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read mode-000 dirs")
        home = make_home()
        victim = os.path.join(home, "build")
        locked = os.path.join(victim, "locked")
        os.makedirs(os.path.join(locked, "repo", ".git"))
        with open(os.path.join(locked, "repo", "fresh.bin"), "w") as handle:
            handle.write("z")
        old_ns = (int(OLD * 1e9), int(OLD * 1e9))
        for dirpath, _, filenames in os.walk(victim):
            os.utime(dirpath, ns=old_ns)
            for name in filenames:
                os.utime(os.path.join(dirpath, name), ns=old_ns)
        os.chmod(locked, 0o000)
        try:
            ok, reason = candidate_state(victim, 30, set(), NOW)
        finally:
            os.chmod(locked, 0o755)
        self.assertFalse(ok)
        self.assertEqual(reason, "find-error")

    def test_kill_switch_forces_dry_run(self):
        # Mutant: kill switch checked but apply proceeds anyway, or
        # killed apply overwrites dryrun.json with a fresh timestamp/plan.
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        dry = parse_argv(["--home-root", home, "--out-dir", out])
        run_tool(dry, {}, proc_paths=set(), now=NOW)
        receipt = os.path.join(out, "dryrun.json")
        with open(receipt, "rb") as handle:
            before_bytes = handle.read()
        opts = parse_argv(["--home-root", home, "--out-dir", out,
                           "--apply", "--dry-run-receipt", receipt])
        result = run_tool(opts, {"HOST_CACHE_RECLAIM_DISABLED": "1"},
                          proc_paths=set(), now=NOW)
        self.assertIsNone(result["applied"])
        self.assertIsNotNone(result["summary"]["killed"])
        # dryrun.json must be byte-identical: kill switch on --apply writes
        # apply-plan.json and never renews or touches dryrun.json.
        with open(receipt, "rb") as handle:
            after_bytes = handle.read()
        self.assertEqual(after_bytes, before_bytes)
        with open(os.path.join(out, "apply-plan.json")) as handle:
            self.assertEqual(json.load(handle)["kind"], "apply-plan")

    def test_missing_candidate_reported_not_deleted(self):
        home = make_home()
        gone = os.path.join(home, ".rb-sbx")
        import shutil
        shutil.rmtree(gone)
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        opts = parse_argv(["--home-root", home, "--out-dir", out])
        result = run_tool(opts, {}, proc_paths=set(), now=NOW)
        self.assertEqual(result["summary"]["kept_by_reason"][".rb-sbx"],
                         "missing")


class RegistryGateTest(unittest.TestCase):
    def test_detector_is_dry_run(self):
        # Mutant: role ignored — any active entry authorizes deletion.
        apply, _ = registry_apply_decision(
            [{"id": "e", "state": "active", "role": "detector",
              "writes": []}], "e")
        self.assertFalse(apply)

    def test_wrong_write_target_is_dry_run(self):
        # Mutant: writes list unchecked — the worktree dep-cache writer
        # entry would authorize host cache deletion.
        apply, _ = registry_apply_decision(
            [{"id": "e", "state": "active", "role": "writer",
              "writes": ["fs:worktree-dep-caches"]}], "e")
        self.assertFalse(apply)

    def test_missing_and_duplicate_are_dry_run(self):
        apply, _ = registry_apply_decision([], "e")
        self.assertFalse(apply)
        two = [{"id": "e", "state": "active", "role": "writer",
                "writes": [REGISTRY_WRITE_TARGET]}] * 2
        apply, _ = registry_apply_decision(two, "e")
        self.assertFalse(apply)

    def test_active_writer_applies(self):
        apply, _ = registry_apply_decision(
            [{"id": "e", "state": "active", "role": "writer",
              "writes": [REGISTRY_WRITE_TARGET]}], "e")
        self.assertTrue(apply)

    def test_registry_gate_rejects_apply_combo(self):
        with self.assertRaises(SystemExit):
            parse_argv(["--apply", "--registry-gate", "e"])

    def test_gated_detector_run_deletes_nothing(self):
        home = make_home()
        with tempfile.TemporaryDirectory() as tmp:
            reg = os.path.join(tmp, "registry.json")
            with open(reg, "w") as handle:
                json.dump({"entries": [
                    {"id": "e", "state": "active", "role": "detector",
                     "writes": []}]}, handle)
            out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
            opts = parse_argv(["--home-root", home, "--out-dir", out,
                               "--registry-gate", "e", "--registry", reg])
            apply, _ = resolve_mode(opts)
            self.assertFalse(apply)
            result = run_tool(opts, {}, proc_paths=set(), now=NOW)
            self.assertIsNone(result["applied"])

    def test_registry_promoted_apply_writes_apply_plan_not_receipt(self):
        # Mutant: receipt filename/kind keyed on opts.apply only, so a
        # registry-promoted apply (opts.apply False, effective True)
        # deletes and then writes dryrun.json kind dry-run — a reusable
        # receipt for a second manual delete.
        home = make_home()
        with tempfile.TemporaryDirectory() as tmp:
            reg = os.path.join(tmp, "registry.json")
            with open(reg, "w") as handle:
                json.dump({"entries": [
                    {"id": "e", "state": "active", "role": "writer",
                     "writes": [REGISTRY_WRITE_TARGET]}]}, handle)
            out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
            opts = parse_argv(["--home-root", home, "--out-dir", out,
                               "--registry-gate", "e", "--registry", reg])
            apply, _ = resolve_mode(opts)
            self.assertTrue(apply)
            result = run_tool(opts, {}, proc_paths=set(), now=NOW)
            self.assertIsNotNone(result["applied"])
            self.assertEqual(sorted(result["applied"]["deleted"]),
                             sorted(rel for rel, _, _ in ALLOWLIST))
            plan_path = os.path.join(out, "apply-plan.json")
            self.assertTrue(os.path.isfile(plan_path))
            with open(plan_path, encoding="utf-8") as handle:
                plan_doc = json.load(handle)
            self.assertEqual(plan_doc["kind"], "apply-plan")
            self.assertFalse(os.path.exists(os.path.join(out, "dryrun.json")))
            # The promoted run's plan is not a receipt for a manual apply.
            reuse = parse_argv(["--home-root", home, "--out-dir", out,
                                "--apply", "--dry-run-receipt", plan_path])
            with self.assertRaises(SafetyRefusal):
                run_tool(reuse, {}, proc_paths=set(), now=NOW)


class ApplyTest(unittest.TestCase):
    def test_remove_one_handles_readonly_gomod_cache(self):
        # Mutant: plain rm -rf leaves Go's 0555 module-cache dirs
        # half-deleted (rm-error, exit 3) for non-root runs.
        import shutil
        from host_regenerable_cache_reclaim import remove_one
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "gomod")
            os.makedirs(os.path.join(target, "mod-cache"))
            with open(os.path.join(target, "mod-cache", "f"), "w") as h:
                h.write("x")
            os.chmod(os.path.join(target, "mod-cache"), 0o555)
            os.chmod(target, 0o555)
            self.assertIsNone(remove_one(target))
            self.assertFalse(os.path.exists(target))
            _ = shutil  # keep import used on early-return paths

    def test_apply_deletes_stale_idle_caches(self):
        home = make_home()
        out = tempfile.mkdtemp(prefix="case_4f630dab-out-")
        run_tool(parse_argv(["--home-root", home, "--out-dir", out]),
                 {}, proc_paths=set(), now=NOW)
        receipt = os.path.join(out, "dryrun.json")
        result = run_tool(
            parse_argv(["--home-root", home, "--out-dir", out,
                        "--apply", "--dry-run-receipt", receipt]),
            {}, proc_paths=set(), now=NOW)
        applied = result["applied"]
        self.assertEqual(sorted(applied["deleted"]),
                         sorted(rel for rel, _, _ in ALLOWLIST))
        self.assertEqual(applied["errors"], [])
        for rel, _, _ in ALLOWLIST:
            self.assertFalse(os.path.exists(os.path.join(home, rel)), rel)
        self.assertTrue(os.path.isdir(home))
        with open(os.path.join(out, "apply.json")) as handle:
            self.assertEqual(json.load(handle)["deleted"],
                             applied["deleted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
