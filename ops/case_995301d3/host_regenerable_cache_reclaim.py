#!/usr/bin/env python3
"""host_regenerable_cache_reclaim.py — bounded reclaim for host regenerable caches. (case_995301d3, from case_56de8f93-C.)

WHY THIS SCRIPT EXISTS.

Read-only triage (case_96f77124, evidence doc
host-disk-attribution-20261011t050504) attributed the host-side /dev/md5
consumers behind node7b1ff338 92%. Candidate C is five regenerable caches under
/home/ubuntu (allocated bytes, du -B1, non-atomic):

  build        5,955,661,824 B   build outputs, gocache, gomod
  .npm         4,351,803,392 B   _cacache + _npx
  .cache       1,188,696,064 B   ms-playwright, node-gyp, repo-mirrors, pip
  .rv-sbx/work 6,091,550,720 B   sandbox work dirs (mirror.git/out NOT in scope)
  .rb-sbx        556,683,264 B   sandbox work/mirror/out (whole dir in scope)

Total: 18,144,395,264 B (~16.9 GiB). The worktree dep-cache routine
(ops/case_b5184419, owner case_f8172222) cannot touch these: they live outside any
worktree. This tool is the bounded extension owned by that routine owner —
NOT the pruner. Pool-images, databases, keeper, LXD, Docker, backups,
worktrees and any worktree itself are OUT (case_56de8f93 candidates A/B/D/E).

WHY EACH SAFETY PROPERTY IS STRUCTURAL, NOT PROSE.

- Explicit allowlist, nothing else. Candidates resolve under --home-root
  (default /home/ubuntu) and must equal one of the five relative paths
  above (for .rv-sbx only the work subdir). Anything else — including
  pool-images, pgdata, keeper, lxd-pool, docker, backups, stacks — is
  refused with exit 2 before anything is sized.
- Disjoint from custody holds by construction. --home-root inside the
  Operator workspace, stacks/paperclip, /home/docker or /home/lxd-pool is
  refused. The case_2a726c40/case_5c194d38 holds stay exactly as they are; this
  tool neither reads nor writes them.
- Dry-run is the default AND --apply needs a receipt. --apply requires
  --dry-run-receipt <dryrun.json> from THIS tool version with identical
  home/caps/idle bounds, the same planned set, and an age under 24h;
  without it the run refuses (exit 1). Apply runs write apply-plan.json
  and never renew dryrun.json. A manual --apply with no prior dry-run is
  worse than no apply.
- Bounded: --max-bytes (default the evidence total 18144395264) caps each
  pass; candidates apply largest-first, the rest is skipped as
  over-max-bytes, never deleted. --max-paths (default 64) caps rm calls.
- No mount crossing. A candidate that is a mountpoint is kept
  (mountpoint); every rm runs as `rm -rf --one-file-system --`, so the
  /dev/loop3 bind under stacks data can never be descended into even if
  the allowlist were misconfigured.
- Idle + live-process gates. A candidate modified within --idle-minutes
  (default 30) is kept (recent-activity); any host/container process with
  cwd/exe/fd under it keeps it (live-process-ref). An unreadable /proc
  entry (EACCES/EPERM, the normal non-root case for another uid's
  process) fails closed to proc-unreadable and keeps everything.
  Unreadable trees fail closed (find-error via os.walk onerror=raise),
  never "idle". Read-only cache dirs (Go 0555 module cache) are made
  user-writable with find -xdev before rm, same mount containment.
- Nested repos keep. A candidate containing .git at any depth is kept
  (nested-repo): build outputs must never carry history.
- Registry gate for any recurring wiring. --registry-gate ID applies only
  when the WS7 registry entry ID is an active writer of
  fs:host-regenerable-caches; anything else (detector, missing, wrong
  writes, unreadable registry) is a dry-run. No such entry exists today,
  so every gated firing is a dry-run: wiring a routine is a reviewed
  registry change, not a flag. Cannot be combined with --apply.
- Kill switch. HOST_CACHE_RECLAIM_DISABLED=1 (or <home>/.host-cache-
  reclaim.disabled) forces dry-run; the report says `killed`.
- Fail closed, loud. Exit 1 = safety refusal (blind apply, missing/bad
  receipt). Exit 2 = fatal (unreadable root, allowlist violation, walked
  tree error). Exit 3 = apply finished with at least one rm failure.
- No secrets in output: stdout carries only relative paths, reasons,
  counts and byte totals. No API, no credentials, no network.

MODES.

  python3 ops/case_995301d3/host_regenerable_cache_reclaim.py --home-root <dir>
      Dry-run (default): resolve, size, idle/proc/mount checks, print the
      summary JSON, write dryrun.json. Deletes nothing.

  python3 ops/case_995301d3/host_regenerable_cache_reclaim.py --home-root <dir> \\
      --apply --dry-run-receipt <dryrun.json> [--max-bytes N]
      Re-evaluate everything fresh in the same process (the receipt binds
      home/caps/idle/plan/age; its plan is never executed, only verified),
      re-prove idle/proc/mount/containment per candidate immediately before
      each rm. Writes apply-plan.json + apply.json, never dryrun.json.

  ... --registry-gate ENTRY_ID [--registry PATH]
      Recurring mode: apply only when the registry entry is an active
      writer of fs:host-regenerable-caches; otherwise dry-run. The first
      stderr line records the chosen mode.

Rollback: deleted caches regenerate with the package manager or the next
build (RUNBOOK.md table); verify with the per-path command, then compare
`du -sB1` before/after. Revert the commit / pause the (future) routine to
stop the tool. Activation of any live reclaim stays gated: case_955c423e
blocked on case_90645769 — a merged source repair does not authorize it.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402

TOOL_VERSION = "case_995301d3.1"
REGISTRY_WRITE_TARGET = "fs:host-regenerable-caches"
REGISTRY_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "case_30b51a0f",
    "host-timer-registry.json")
KILL_FILE_NAME = ".host-cache-reclaim.disabled"
IDLE_MINUTES_DEFAULT = 30
MAX_PATHS_DEFAULT = 64
# Dry-run receipts older than this refuse --apply: the tree may have changed.
RECEIPT_MAX_AGE_SECONDS = 86400

# (relative path, evidence bytes, regen hint key). Only the work subdir of
# .rv-sbx is in scope; its mirror.git/out stay. Whole .rb-sbx is in scope
# (556 MB total: mirror + work + out, all sandbox-regenerable).
ALLOWLIST = (
    ("build", 5955661824, "rebuild"),
    (".npm", 4351803392, "npm-cache"),
    (".cache", 1188696064, "tool-cache"),
    (os.path.join(".rv-sbx", "work"), 6091550720, "sandbox-work"),
    (".rb-sbx", 556683264, "sandbox-work"),
)
MAX_BYTES_DEFAULT = sum(size for _, size, _ in ALLOWLIST)

# Roots that must never be a --home-root (custody/pruner scope by
# construction: Operator runs + holds, live data, daemon storage).
FORBIDDEN_HOME_PARTS = (
    "stacks", "paperclip-operator", "docker", "lxd-pool",
    "paperclip-backups", "backups", "pool-images", "pgdata",
)


class ReclaimError(RuntimeError):
    """Loud, fatal failure: usage error, unreadable root, I/O error."""


class SafetyRefusal(RuntimeError):
    """Loud safety refusal: blind apply, missing/bad receipt."""


# ---------------------------------------------------------------------------
# PURE CORE. No network, no credentials, no clock reads (callers pass times).
# The offline suite drives every branch with fixture dirs and fake procs.
# ---------------------------------------------------------------------------

def bounds_fingerprint():
    """Canonical bounds string this tool version enforces."""
    return json.dumps({"tool": TOOL_VERSION,
                       "allow": [[rel, size] for rel, size, _ in ALLOWLIST],
                       "max_bytes_default": MAX_BYTES_DEFAULT},
                      sort_keys=True)


def home_root_ok(home):
    """(ok, reason): --home-root must be a plain ubuntu-style home dir."""
    real = os.path.realpath(home)
    if not os.path.isdir(real) or os.path.islink(home):
        return (False, "home-not-a-real-dir")
    parts = set(real.split(os.sep))
    if parts & set(FORBIDDEN_HOME_PARTS):
        return (False, "home-in-forbidden-scope")
    base = os.path.basename(real.rstrip(os.sep))
    if base in FORBIDDEN_HOME_PARTS:
        return (False, "home-in-forbidden-scope")
    return (True, None)


def resolve_candidates(home):
    """(candidates, violations): absolute paths for the five allowlisted
    relatives. A relative that escapes (should be impossible: the table is
    constant) is a violation, never a candidate."""
    cands, violations = [], []
    for rel, evidence, regen in ALLOWLIST:
        full = os.path.realpath(os.path.join(home, rel))
        prefix = os.path.realpath(home).rstrip(os.sep) + os.sep
        if full != os.path.join(os.path.realpath(home), rel) \
                or not full.startswith(prefix):
            violations.append(rel)
            continue
        cands.append({"rel": rel, "path": full, "evidence": evidence,
                      "regen": regen})
    return cands, violations


def registry_apply_decision(entries, entry_id):
    """(apply, why) from the reviewed WS7 registry. Apply needs ALL of: the
    entry exists exactly once, state active, role writer, and the one write
    target this tool touches. Everything else reads as dry-run."""
    if not isinstance(entries, list):
        return (False, "registry has no entries list")
    matches = [e for e in entries
               if isinstance(e, dict) and e.get("id") == entry_id]
    if len(matches) != 1:
        return (False, "registry entry %r found %d times" % (entry_id,
                                                             len(matches)))
    entry = matches[0]
    if entry.get("state") != "active":
        return (False, "entry state is %r, not active" % entry.get("state"))
    if entry.get("role") != "writer":
        return (False, "entry role is %r, not writer" % entry.get("role"))
    if REGISTRY_WRITE_TARGET not in (entry.get("writes") or []):
        return (False, "entry does not declare %s" % REGISTRY_WRITE_TARGET)
    return (True, "registry entry %r is an active writer" % entry_id)


def resolve_mode(opts):
    """(apply, why). Fail-closed: an unreadable registry is dry-run."""
    if not opts.registry_gate:
        return (opts.apply, "--apply" if opts.apply else "default")
    try:
        with open(opts.registry, encoding="utf-8") as handle:
            entries = json.load(handle).get("entries")
    except (OSError, ValueError, AttributeError) as exc:
        return (False, "registry unreadable: %s" % exc)
    return registry_apply_decision(entries, opts.registry_gate)


def kill_switch(env, home, exists=os.path.exists):
    """(tripped, why) from the env var and the marker file."""
    if str(env.get("HOST_CACHE_RECLAIM_DISABLED", "")).strip().lower() in (
            "1", "true", "yes", "on"):
        return (True, "env HOST_CACHE_RECLAIM_DISABLED")
    marker = os.path.join(home, KILL_FILE_NAME)
    if exists(marker):
        return (True, "file %s" % marker)
    return (False, None)


def check_receipt(path, opts, now=None):
    """Bounds fingerprint of a prior dry-run receipt. Raises SafetyRefusal
    when the receipt is missing, unreadable, from different bounds, from a
    different home/caps/idle window, older than RECEIPT_MAX_AGE_SECONDS,
    or when its planned set is missing.

    The receipt binds the realpath home_root, max_bytes, max_paths,
    idle_minutes, the planned rels and a timestamp. An --apply run never
    overwrites dryrun.json (it writes apply-plan.json instead), so a receipt
    cannot renew itself. `opts` is the current apply's parsed argv and is
    required: there is no constant-only check path. `now` defaults to
    time.time(). Returns the parsed receipt doc so callers can verify the
    fresh plan against `doc['planned']` after re-planning."""

    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SafetyRefusal("dry-run receipt unreadable: %s" % exc)
    want = bounds_fingerprint()
    if not isinstance(doc, dict) or doc.get("bounds") != want:
        raise SafetyRefusal("dry-run receipt bounds mismatch: re-run a "
                            "dry-run with this tool version first")
    home = os.path.realpath(opts.home_root)
    for key, want_val in (
            ("home_root", home),
            ("max_bytes", opts.max_bytes),
            ("max_paths", opts.max_paths),
            ("idle_minutes", opts.idle_minutes)):
        if doc.get(key) != want_val:
            raise SafetyRefusal(
                "dry-run receipt %s mismatch (%r != %r): re-run a dry-run "
                "with identical bounds first" % (key, doc.get(key),
                                                 want_val))
    created = doc.get("created_at")
    stamp = now if now is not None else time.time()
    if not isinstance(created, (int, float)) or stamp - created > \
            RECEIPT_MAX_AGE_SECONDS:
        raise SafetyRefusal("dry-run receipt expired: re-run a dry-run "
                            "first")
    if doc.get("kind") != "dry-run":
        raise SafetyRefusal("dry-run receipt kind mismatch (%r != 'dry-run'): "
                            "apply-plan cannot be reused as a receipt" %
                            doc.get("kind"))
    if not isinstance(doc.get("planned"), list):
        raise SafetyRefusal("dry-run receipt has no planned set: re-run a "
                            "dry-run first")
    return doc


def verify_apply_plan(receipt_doc, take_rels):
    """Refuse when the fresh apply plan lists paths the receipt never showed.

    Deleting a subset of the receipt is fine (a path may have gone busy,
    mounted, or over-cap since the dry-run). Any path in `take_rels` that is
    not in `receipt_doc['planned']` is a refusal: the operator never
    reviewed it. Raises SafetyRefusal; returns True otherwise."""
    planned = receipt_doc.get("planned")
    if not isinstance(planned, list):
        raise SafetyRefusal("dry-run receipt has no planned set: re-run a "
                            "dry-run first")
    extra = set(take_rels) - set(planned)
    if extra:
        raise SafetyRefusal(
            "apply plan has paths the dry-run receipt did not list (%s): "
            "re-run a dry-run first" % ", ".join(sorted(extra)))
    return True


def parse_argv(argv):
    """Argv parsing, isolated so the suite can pin dry-run-by-default."""
    parser = argparse.ArgumentParser(
        description="Bounded reclaim for host regenerable caches "
                    "(case_995301d3). Dry-run unless --apply.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="Actually delete (re-evaluates fresh; needs "
                           "--dry-run-receipt).")
    mode.add_argument("--registry-gate", metavar="ENTRY_ID", default=None,
                      help="Recurring mode: apply only when the WS7 "
                           "registry entry ENTRY_ID is an active writer of "
                           "%s; otherwise (or on ANY registry error) "
                           "dry-run." % REGISTRY_WRITE_TARGET)
    parser.add_argument("--registry", default=REGISTRY_DEFAULT,
                        help="WS7 registry JSON for --registry-gate.")
    parser.add_argument("--home-root", default="/home/ubuntu",
                        help="Host home dir holding the five caches.")
    parser.add_argument("--dry-run-receipt", default=None,
                        help="dryrun.json from this tool version (required "
                             "with --apply).")
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES_DEFAULT,
                        help="Cap on deleted bytes per pass.")
    parser.add_argument("--max-paths", type=int, default=MAX_PATHS_DEFAULT,
                        help="Cap on deleted paths per pass.")
    parser.add_argument("--idle-minutes", type=int,
                        default=IDLE_MINUTES_DEFAULT)
    parser.add_argument("--out-dir", default=None,
                        help="Where to write dryrun.json/apply.json "
                             "(default: $PAPERCLIP_SCRATCH_DIR).")
    opts = parser.parse_args(argv)
    for name in ("max_bytes", "max_paths", "idle_minutes"):
        if getattr(opts, name) < 0:
            parser.error("--%s must be >= 0" % name.replace("_", "-"))
    return opts


# ---------------------------------------------------------------------------
# LIVE SHELL. Filesystem/process access. Helpers take `run` /
# `proc_paths` / `now` so the suite drives them against fixtures.
# ---------------------------------------------------------------------------

def live_run(args, cwd=None, timeout=300, stdin_text=None):
    """Run a subprocess, capturing stdout. Never raises on nonzero exit."""
    proc = subprocess.run(args, cwd=cwd, input=stdin_text,
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout


def _proc_unreadable(exc):
    """True when a per-pid OSError means 'could not see', not 'gone'.

    ENOENT/ESRCH/ENOTDIR: the process exited between listdir and readlink —
    safe to ignore. EACCES/EPERM (another uid, e.g. running as ubuntu while
    a root sandbox holds a cache): the process may live inside a candidate
    and must keep it. Any other errno fails closed too."""
    code = getattr(exc, "errno", None)
    if code in (errno.ENOENT, errno.ESRCH, errno.ENOTDIR):
        return False
    return True


def scan_host_processes():
    """Absolute paths referenced by live processes (cwd/exe/fd).

    Returns None when the view is incomplete: /proc itself unreadable, or
    any pid/fd read fails with EACCES/EPERM (another uid's process, the
    normal case for a non-root runbook invocation). Callers keep every
    candidate as proc-unreadable on None, never as idle."""
    paths = set()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    saw_unreadable = False
    for pid in pids:
        if not pid.isdigit():
            continue
        base = "/proc/%s" % pid
        for link in ("cwd", "exe"):
            try:
                paths.add(os.readlink(os.path.join(base, link)))
            except OSError as exc:
                if _proc_unreadable(exc):
                    saw_unreadable = True
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError as exc:
            if _proc_unreadable(exc):
                saw_unreadable = True
            continue
        for fd in fds:
            try:
                paths.add(os.readlink(os.path.join(base, "fd", fd)))
            except OSError as exc:
                if _proc_unreadable(exc):
                    saw_unreadable = True
    if saw_unreadable:
        return None
    return paths


def path_referenced(path, pathset):
    """True when `path` itself or anything beneath it appears in `pathset`."""
    prefix = path.rstrip("/") + "/"
    return any(p == path or p.startswith(prefix) for p in pathset)


def candidate_state(path, idle_minutes, proc_paths, now=None):
    """(ok, reason): mount, symlink, nested-repo, idle and process gates
    for one candidate root. Unverifiable (find error, unreadable proc
    view) is a keep, never a pass."""
    if os.path.islink(path):
        return (False, "symlink")
    if not os.path.isdir(path):
        return (False, "missing")
    if os.path.ismount(path):
        return (False, "mountpoint")
    if proc_paths is None:
        return (False, "proc-unreadable")
    if path_referenced(os.path.realpath(path), proc_paths):
        return (False, "live-process-ref")
    cutoff = (now if now is not None else time.time()) - idle_minutes * 60

    def _raise(exc):
        raise exc

    try:
        for dirpath, dirnames, filenames in os.walk(path, onerror=_raise):
            if ".git" in dirnames or ".git" in filenames:
                return (False, "nested-repo")
            try:
                if os.lstat(dirpath).st_mtime > cutoff:
                    return (False, "recent-activity")
            except OSError:
                return (False, "find-error")
            for name in filenames:
                try:
                    if os.lstat(os.path.join(dirpath, name)).st_mtime > cutoff:
                        return (False, "recent-activity")
                except OSError:
                    return (False, "find-error")
    except OSError:
        return (False, "find-error")
    return (True, None)


def dir_bytes(path):
    """(bytes, ok): du -sB1 total; ok False when du fails."""
    try:
        proc = subprocess.run(["du", "-sB1", "--", path],
                              capture_output=True, text=True, timeout=1800)
        return int(proc.stdout.split()[0]), proc.returncode == 0
    except (OSError, ValueError, IndexError):
        return 0, False
    except subprocess.TimeoutExpired:
        return 0, False


def plan_deletes(sized, max_bytes, max_paths):
    """Largest-first bounded plan: take while both caps hold; the rest is
    skipped as over-max-bytes / over-max-paths, never deleted."""
    ordered = sorted(sized, key=lambda c: (-c.get("bytes", 0), c["rel"]))
    take, skipped, used, n = [], {}, 0, 0
    for cand in ordered:
        if n >= max_paths:
            skipped[cand["rel"]] = "over-max-paths"
            continue
        if used + cand.get("bytes", 0) > max_bytes:
            skipped[cand["rel"]] = "over-max-bytes"
            continue
        take.append(cand)
        used += cand.get("bytes", 0)
        n += 1
    return take, skipped


def remove_one(path):
    """rm -rf --one-file-system; returns None on success else a reason.

    Go writes module-cache dirs read-only (0555), so a plain rm -rf as the
    owning non-root user leaves build half-deleted. Best-effort
    `find -xdev -type d ! -perm -u+w -exec chmod u+w` first (same
    one-file-system containment, no symlink following); rm failure afterwards
    is still a loud rm-error, never silent."""
    try:
        subprocess.run(
            ["find", path, "-xdev", "-type", "d", "!", "-perm", "-u+w",
             "-exec", "chmod", "u+w", "{}", "+"],
            capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc = subprocess.run(
            ["rm", "-rf", "--one-file-system", "--", path], timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return "rm-error"
    if proc.returncode != 0 or os.path.exists(path):
        return "rm-error"
    return None


def run_tool(opts, env, proc_paths=None, now=None):
    """One full pass: resolve, gate, size, report; delete only on apply."""
    home = os.path.realpath(opts.home_root)
    ok, why = home_root_ok(opts.home_root)
    if not ok:
        raise ReclaimError("refusing home-root: %s" % why)
    killed, kill_why = kill_switch(env, home)
    apply, mode_why = resolve_mode(opts)
    if killed:
        apply = False
    print("mode: %s (%s)" % ("apply" if apply else "dry-run",
                             ("killed: %s" % kill_why) if killed else mode_why),
          file=sys.stderr)
    if opts.apply and not opts.registry_gate and not opts.dry_run_receipt:
        raise SafetyRefusal("refusing --apply without --dry-run-receipt: "
                            "run a dry-run first")
    stamp = now if now is not None else time.time()
    receipt_doc = None
    if apply and opts.dry_run_receipt:
        receipt_doc = check_receipt(opts.dry_run_receipt, opts, stamp)
    cands, violations = resolve_candidates(opts.home_root)
    if violations:
        raise ReclaimError("allowlist escape: %s" % ",".join(violations))
    injected_procs = proc_paths is not None
    if proc_paths is None:
        proc_paths = scan_host_processes()
    now = stamp
    results = []
    for cand in cands:
        ok, reason = candidate_state(cand["path"], opts.idle_minutes,
                                     proc_paths, now)
        results.append({**cand, "reason": None if ok else reason,
                        "bytes": 0, "size_ok": True})
    for res in results:
        if res["reason"] is not None:
            continue
        size, good = dir_bytes(res["path"])
        res["bytes"] = size
        res["size_ok"] = good
        if not good:
            res["reason"] = "size-error"
    eligible = [r for r in results if r["reason"] is None]
    take, skipped = plan_deletes(eligible, opts.max_bytes, opts.max_paths)
    take_rels = {c["rel"] for c in take}
    if receipt_doc is not None:
        verify_apply_plan(receipt_doc, take_rels)
    for res in eligible:
        if res["rel"] not in take_rels:
            res["reason"] = skipped.get(res["rel"], "over-max-bytes")
    summary = {
        "tool": TOOL_VERSION,
        "home_root": home,
        "killed": kill_why if killed else None,
        "candidates": len(results),
        "eligible_bytes": sum(r["bytes"] for r in eligible),
        "planned_paths": len(take),
        "planned_bytes": sum(c["bytes"] for c in take),
        "kept_by_reason": {r["rel"]: r["reason"] for r in results
                           if r["reason"]},
    }
    out_dir = opts.out_dir or env.get("PAPERCLIP_SCRATCH_DIR")
    if not out_dir:
        raise ReclaimError("no --out-dir and $PAPERCLIP_SCRATCH_DIR is unset")
    os.makedirs(out_dir, exist_ok=True)
    # Key kind + filename on requested OR effective apply: a manual --apply
    # (requested) and a registry-promoted apply (effective, opts.apply False)
    # both delete, so both must write apply-plan.json/kind apply-plan, never
    # a reusable dryrun.json. A kill-switched manual --apply still writes
    # apply-plan.json (opts.apply True) so dryrun.json is never renewed.
    is_apply_run = bool(opts.apply or apply)
    payload = {"tool": TOOL_VERSION, "bounds": bounds_fingerprint(),
               "kind": "apply-plan" if is_apply_run else "dry-run",
               "home_root": home,
               "max_bytes": opts.max_bytes,
               "max_paths": opts.max_paths,
               "idle_minutes": opts.idle_minutes,
               "created_at": now,
               "planned": sorted(c["rel"] for c in take),
               "summary": summary,
               "results": [{k: r[k] for k in ("rel", "reason", "bytes",
                                             "regen")} for r in results]}
    # An --apply run must never renew its own receipt: dry runs write
    # dryrun.json (the receipt --apply consumes); apply runs write their
    # fresh evaluation to apply-plan.json instead. Key the filename on
    # requested OR effective apply (opts.apply or apply), so a kill-switched
    # manual apply writes apply-plan.json and never overwrites dryrun.json,
    # and a registry-promoted apply (opts.apply False, effective True) also
    # writes apply-plan.json instead of a reusable dryrun.json.
    receipt_name = "apply-plan.json" if is_apply_run else "dryrun.json"
    try:
        write_json_atomic(os.path.join(out_dir, receipt_name), payload)
    except OSError as exc:
        print(json.dumps(summary, indent=1), flush=True)
        raise ReclaimError("write failed: errno=%s" % exc.errno) from exc
    print(json.dumps(summary, indent=1))
    if not apply:
        return {"summary": summary, "applied": None}
    # --apply re-proves every gate immediately before each rm (fresh
    # candidate_state on the live tree, never the dry-run verdict). The
    # offline suite injects proc_paths for determinism; live runs scan
    # fresh here too.
    fresh_procs = proc_paths if injected_procs else scan_host_processes()
    deleted, errors, skipped_rm = [], [], {}
    used = 0
    for cand in sorted(take, key=lambda c: (-c.get("bytes", 0), c["rel"])):
        ok, reason = candidate_state(cand["path"], opts.idle_minutes,
                                     fresh_procs, now)
        if not ok:
            skipped_rm[cand["rel"]] = reason
            continue
        if used + cand.get("bytes", 0) > opts.max_bytes:
            skipped_rm[cand["rel"]] = "over-max-bytes"
            continue
        err = remove_one(cand["path"])
        if err is None:
            deleted.append(cand["rel"])
            used += cand.get("bytes", 0)
        else:
            errors.append(cand["rel"])
    report = {"deleted": deleted, "deleted_bytes": used,
              "skipped": skipped_rm, "errors": errors,
              "n_errors": len(errors)}
    try:
        write_json_atomic(os.path.join(out_dir, "apply.json"), report)
    except OSError as exc:
        print(json.dumps(report, indent=1), flush=True)
        raise ReclaimError("write failed: errno=%s" % exc.errno) from exc
    print(json.dumps(report, indent=1))
    return {"summary": summary, "applied": report}


def main(argv=None, env=None):
    opts = parse_argv(argv if argv is not None else sys.argv[1:])
    env = os.environ if env is None else env
    try:
        out = run_tool(opts, env)
    except SafetyRefusal as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 1
    applied = out.get("applied")
    if applied and applied.get("errors"):
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReclaimError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(2)
