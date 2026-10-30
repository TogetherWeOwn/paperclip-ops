#!/usr/bin/env python3
"""Strip regenerable build output from finished Operator SSH run folders (case_34c2c99f E6).

Recovery-custody holds in regen-prune-keep.txt (one run UUID per line) are
never planned and never removed; the hold file is re-read before each run and
again immediately before each path's removal, so newly-added holds fail closed.

Archive-before-delete (case_90645769): in --apply mode no run folder is stripped
until its non-regenerable content is proven archived offsite through the
retention owner's established batch tar.zst -> offsite:archives/<class>/<date>/
-> re-download SHA256 verification -> manifest protocol (reference:
retire-terminal-worktrees.py, host artifact SHA256 973c5dd0...). Runs without
that proof keep every local path (refused `archive-unverified`). Apply validates
the archive ledger before removal, even when no paths are eligible; malformed
custody evidence aborts closed. Dry-run remains read-only and non-blocking.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter

ROOT = "/home/paperclip-operator/workspace/.paperclip-runtime/runs"
NAMES = ("node_modules", "target", ".next", ".turbo", ".pnpm-store")
TERMINAL = ("succeeded", "failed", "cancelled", "timed_out")
IDLE_MINUTES = 10
EMERGENCY_HOME_PCT = 90
MAX_REMOVE_DEFAULT = 200
TIME_BUDGET_DEFAULT = 240
HARD_BUDGET_SECONDS = 600
RM_TIMEOUT_SECONDS = 300
SCAN_TIMEOUT_SECONDS = 300
FAILED_COOLDOWN_SECONDS = 6 * 3600
HERE = os.path.dirname(os.path.abspath(__file__))
PAUSE_PATH = os.path.join(HERE, "prune-operator-regen-dirs.PAUSE")
LOCK_PATH = os.path.join(HERE, "prune-operator-regen-dirs.lock")
FAILED_PATH = os.path.join(HERE, "prune-operator-regen-dirs.failed.json")
MANIFEST_DIR = os.path.join(HERE, "regen-prune-manifests")
LOG_PATH = os.path.join(HERE, "operator-actions.log")
# Recovery-custody hold list kept next to the script (on the host:
# ops/worker-vps/regen-prune-keep.txt). Runs listed here are never planned
# and never removed; terminal status never releases custody.
KEEP_PATH = os.path.join(HERE, "regen-prune-keep.txt")
OP_PREFIX = ["sudo", "-n", "-u", "paperclip-operator"]
# A root-owned file at a fixed path, so the sudoers rule can name this exact command line.
PROBE_PATH = "/usr/local/libexec/paperclip-regen/regen_probe.py"
PROBE_CMD = ["sudo", "-n", "/usr/bin/python3", "-I", PROBE_PATH]
DB_CMD = ["docker", "exec", "-i", "paperclip-db", "sh", "-c",
          'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At']
FIND, GIT, REALPATH = "/usr/bin/find", "/usr/bin/git", "/usr/bin/realpath"
RM, TIMEOUT = "/usr/bin/rm", "/usr/bin/timeout"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Archive-before-delete (case_90645769). No second uploader is invented and no
# secret-bearing configuration is copied: upload auth lives inside the
# retention owner's archive_lib.sh, reached through the same
# `source <lib>; offsite_put ...` interface the reference implementation
# uses. Anything the archive step cannot prove keeps its local paths; archive
# infrastructure that cannot even be checked aborts the firing closed.
# Identity boundary (case_90645769 identity fix, host-verified 2026-10-11): the
# archive orchestration (source <lib>; offsite_put/offsite_get) runs as the
# invoking unit user (ubuntu publisher, the identity the service already runs
# as) through run(), so it can read the retention-owner lib at
# /home/ubuntu/... without widened home permissions or credential copy. The
# tar producer runs as paperclip-operator through OP_PREFIX sudo
# (`sudo -n -u paperclip-operator /usr/bin/tar`) inside the offsite_put
# producer args, so it reads exactly what rm may later delete using absolute
# /usr/bin/tar and the same excludes. Sudoers change versus the #871 grant:
# add /usr/bin/tar, drop the /usr/bin/bash entry (drop it on
# already-provisioned hosts); no User= change. A host without the lib
# fails closed with "archive lib missing offsite_put/offsite_get", never
# silently unarchived. Any privilege/content-exposure delta needs CISO review
# before activation.
# On the host this script lives at ops/worker-vps/, so ARCHIVE_ROOT resolves
# to the same tree the reference derives (ROOT/ops/offsite/archive_lib.sh).
HERE_ARCHIVE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_ROOT = os.path.dirname(os.path.dirname(HERE_ARCHIVE))
ARCHIVE_LIB = os.path.join(ARCHIVE_ROOT, "ops", "offsite", "archive_lib.sh")
ARCHIVE_CLASS = "regen-strip"
ARCHIVE_TIMEOUT_SECONDS = 600
# The reference archives everything EXCEPT regenerable build output and
# caches: exactly the content this strip never deletes, plus the git objects
# that prove it. Reused verbatim so the archive covers what the rm cannot.
TAR_EXCLUDES = ("node_modules", "target", ".next", ".turbo", ".pnpm-store",
                ".cache", "__pycache__", ".venv")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
# Ledger names are minted only by ensure_archived below; anything else
# (another class, a hand edit, a stale backup) must never count as coverage.
ARCHIVE_NAME_RE = re.compile(
    r"regen-strip/\d{4}-\d{2}-\d{2}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-"
    r"\d{8}T\d{6}Z\.tar\.zst")
TAR_BIN, SHA256SUM_BIN = "/usr/bin/tar", "/usr/bin/sha256sum"
# Absolute on purpose: tar runs as paperclip-operator through OP_PREFIX and
# bash/sha256sum run as the invoking unit user for archive orchestration,
# where a PATH shim must not change what executes (see runbook Bounds).
BASH = "/usr/bin/bash"


class Unverifiable(RuntimeError):
    pass


_deadline = None


def hard_expired():
    return _deadline is not None and time.monotonic() >= _deadline


def run(cmd, timeout, stdin=None):
    if _deadline is not None:
        timeout = min(timeout, _deadline - time.monotonic())
    if timeout <= 0:
        return subprocess.CompletedProcess(cmd, 124, "", "")
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              errors="surrogateescape", timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "")
    except OSError:
        return subprocess.CompletedProcess(cmd, 127, "", "")


def op(*cmd, timeout=120):
    return run([*OP_PREFIX, *cmd], timeout)


def inside(path, folder):
    return path == folder or path.startswith(folder + "/")


def canonical(path):
    r = op(REALPATH, "-e", "--", path)
    if r.returncode != 0 or not r.stdout.strip():
        raise Unverifiable("runs root not resolvable")
    return r.stdout.rstrip("\n")


def run_dir_names(root_real):
    r = op(FIND, root_real, "-mindepth", "1", "-maxdepth", "1", "-type", "d", "-print0",
           timeout=SCAN_TIMEOUT_SECONDS)
    if r.returncode != 0:
        raise Unverifiable("run root listing failed")
    names = [os.path.basename(p) for p in r.stdout.split("\0") if p]
    return sorted(n for n in names if UUID_RE.fullmatch(n))


def snapshot_sql(run_ids):
    ids = ",".join(f"'{rid}'" for rid in run_ids)
    return (
        "select r.id::text, r.status,"
        " exists (select 1 from heartbeat_runs c where c.retry_of_run_id = r.id"
        " and c.status not in ('succeeded','failed','cancelled','timed_out')),"
        " exists (select 1 from environment_leases l where l.heartbeat_run_id = r.id"
        " and l.status = 'active' and (l.expires_at is null or l.expires_at > now()))"
        f" from heartbeat_runs r where r.id::text in ({ids});"
    )


def parse_snapshot(text):
    snap = {}
    for line in text.splitlines():
        rid, status, retry, lease = line.split("|")
        snap[rid] = (status, retry == "t", lease == "t")
    return snap


def db_snapshot(run_ids):
    if not run_ids:
        return {}
    r = run(DB_CMD, 120, stdin=snapshot_sql(run_ids))
    if r.returncode != 0:
        raise Unverifiable("run state unreadable")
    try:
        return parse_snapshot(r.stdout)
    except ValueError:
        raise Unverifiable("run state malformed")


def live_refs():
    r = run(PROBE_CMD, 120)
    try:
        data = json.loads(r.stdout) if r.returncode == 0 else None
    except ValueError:
        data = None
    if not data or data.get("errors") != 0:
        raise Unverifiable("process view incomplete")
    return data["refs"]


def home_pct():
    r = run(["df", "--output=pcent", "/home"], 60)
    try:
        return int(r.stdout.split()[-1].rstrip("%")) if r.returncode == 0 else None
    except (ValueError, IndexError):
        return None


def db_block(entry):
    if entry is None:
        return "run-unknown"
    status, retry_pending, lease_held = entry
    if status not in TERMINAL:
        return "run-not-terminal"
    if retry_pending:
        return "retry-pending"
    if lease_held:
        return "lease-active"
    return None


def read_keep():
    """Recovery-custody hold set: one run UUID per line.

    Blank lines and `#` comments (full-line or inline) are ignored. The
    prefix before the first `#` must be blank or a complete UUID. A file that
    cannot be read at all, holds a non-UUID prefix, or holds no UUID
    and no `#` comment (empty or blank-only, e.g. a truncated editor write)
    fails closed: the firing deletes nothing. An intentional "no holds" file
    must contain a `#` comment. Terminal status never releases custody.
    """
    try:
        with open(KEEP_PATH, "r", encoding="utf-8", errors="surrogateescape") as fh:
            text = fh.read()
    except OSError:
        raise Unverifiable("custody holds unreadable")
    held = set()
    saw_comment = False
    for line in text.splitlines():
        prefix, marker, _ = line.partition("#")
        entry = prefix.strip()
        saw_comment = saw_comment or bool(marker)
        if not entry:
            continue
        if not UUID_RE.fullmatch(entry):
            raise Unverifiable("custody holds malformed")
        held.add(entry)
    if not held and not saw_comment:
        raise Unverifiable("custody holds empty")
    return held


def ref_block(run_real, refs):
    return "live-process-ref" if any(inside(ref, run_real) for ref in refs) else None


def mount_block(cand):
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as fh:
            fields = {line.split(" ")[4] for line in fh if line.strip()}
    except (OSError, IndexError):
        return "mount-table-unreadable"
    points = {re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f) for f in fields}
    return "mount-point" if any(p == cand or p.startswith(cand + "/") for p in points) else None


def folder_idle_block(run_real, idle_min):
    if idle_min is None:
        return None
    r = op(FIND, run_real, "-maxdepth", "3", "-newermt", f"-{idle_min} minutes",
           "-print", "-quit", timeout=SCAN_TIMEOUT_SECONDS)
    if r.returncode != 0:
        return "idle-unknown"
    return "recent-write" if r.stdout.strip() else None


def plan_run(rid, run_real, snapshot, refs, idle_min):
    return (db_block(snapshot.get(rid)) or ref_block(run_real, refs)
            or folder_idle_block(run_real, idle_min))


def candidates(run_real):
    name_expr = []
    for name in NAMES:
        name_expr += ["-o", "-name", name] if name_expr else ["-name", name]
    r = op(FIND, run_real, "-mindepth", "1", "-name", ".git", "-prune", "-o",
           "-type", "d", "(", *name_expr, ")", "-prune", "-print0", timeout=SCAN_TIMEOUT_SECONDS)
    if r.returncode != 0:
        raise Unverifiable("candidate scan failed")
    return sorted(p for p in r.stdout.split("\0") if p)


def git_proof(run_real, cand):
    parent, name = os.path.split(cand)
    top = op(GIT, "-C", parent, "rev-parse", "--show-toplevel", timeout=60)
    if top.returncode != 0 or not top.stdout.strip():
        return "git-unprovable"
    if not inside(top.stdout.rstrip("\n"), run_real):
        return "repo-outside-folder"
    tracked = op(GIT, "-C", parent, "ls-files", "--", name, timeout=60)
    if tracked.returncode != 0:
        return "git-unprovable"
    if tracked.stdout.strip():
        return "tracked-files"
    ign = op(GIT, "-C", parent, "check-ignore", "-q", "--", name, timeout=60)
    if ign.returncode == 1:
        return "not-ignored"
    if ign.returncode != 0:
        return "git-unprovable"
    return None


def symlink_block(run_real, cand):
    """Refuse any candidate reachable through a symlink (itself or a parent).

    Called during planning and again immediately before each rm, so a parent
    swapped for a symlink after validation cannot redirect deletion.
    """
    if os.path.basename(cand) not in NAMES or not inside(cand, run_real):
        return "not-candidate"
    for path in (run_real, cand):
        resolved = op(REALPATH, "-e", "--", path, timeout=60)
        if resolved.returncode != 0 or resolved.stdout.rstrip("\n") != path:
            return "symlinked-path"
    return None


def candidate_block(run_real, cand, idle_min):
    reason = symlink_block(run_real, cand)
    if reason:
        return reason
    real = op(FIND, cand, "-maxdepth", "0", "-type", "d", "-print", timeout=60)
    if real.returncode != 0 or real.stdout.rstrip("\n") != cand:
        return "not-a-directory"
    reason = mount_block(cand) or git_proof(run_real, cand)
    if reason:
        return reason
    if idle_min is None:
        expr = ["-name", ".git"]
    else:
        expr = ["(", "-name", ".git", "-o", "-newermt", f"-{idle_min} minutes", ")"]
    walk = op(FIND, cand, *expr, "-print", "-quit", timeout=SCAN_TIMEOUT_SECONDS)
    if walk.returncode != 0:
        return "walk-error"
    hit = walk.stdout.rstrip("\n")
    if not hit:
        return None
    return "nested-repo" if os.path.basename(hit) == ".git" else "recent-write"


def revalidate_run(rid, run_real, idle_min):
    return plan_run(rid, run_real, db_snapshot([rid]), live_refs(), idle_min)


def apply_plan(plan, idle_min, deadline, failed, stamp=None):
    if stamp is None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    groups = []
    for rid, run_real, cand in plan:
        if groups and groups[-1][0] == rid:
            groups[-1][2].append(cand)
        else:
            groups.append((rid, run_real, [cand]))
    removed, refused = 0, Counter()
    for rid, run_real, cands in groups:
        if hard_expired():
            refused["hard-deadline"] += len(cands)
            continue
        if os.path.exists(PAUSE_PATH):
            refused["paused"] += len(cands)
            continue
        if time.monotonic() >= deadline:
            refused["time-budget"] += len(cands)
            continue
        try:
            held_now = read_keep()
        except Unverifiable as exc:
            return removed, refused, str(exc)
        if rid in held_now:
            refused["custody-hold"] += len(cands)
            continue
        try:
            reason = revalidate_run(rid, run_real, idle_min)
        except Unverifiable as exc:
            return removed, refused, str(exc)
        if reason:
            refused[reason] += len(cands)
            continue
        # Archive-first (case_90645769): this run's non-regenerable content must
        # be proven offsite before any rm touches it. Unproven runs keep
        # every local path; an uncheckable archive path aborts closed.
        try:
            covered, why = ensure_archived(rid, run_real, stamp, deadline)
        except Unverifiable as exc:
            return removed, refused, str(exc)
        if not covered:
            refused[why] += len(cands)
            continue
        # The upload/readback above takes minutes, so the pre-archive
        # revalidation is stale: a build, lease, or retry row may have
        # appeared mid-archive. Re-prove run-level liveness AFTER the archive
        # and before any rm; only the archive proof itself may be reused.
        try:
            reason = revalidate_run(rid, run_real, idle_min)
        except Unverifiable as exc:
            return removed, refused, str(exc)
        if reason:
            refused[reason] += len(cands)
            continue
        for cand in cands:
            if hard_expired():
                refused["hard-deadline"] += 1
                continue
            if os.path.exists(PAUSE_PATH):
                refused["paused"] += 1
                continue
            if time.monotonic() >= deadline:
                refused["time-budget"] += 1
                continue
            reason = candidate_block(run_real, cand, idle_min)
            if reason:
                refused[reason] += 1
                continue
            reason = symlink_block(run_real, cand)
            if reason:
                refused[reason] += 1
                continue
            try:
                held_cand = read_keep()
            except Unverifiable as exc:
                return removed, refused, str(exc)
            if rid in held_cand:
                refused["custody-hold"] += 1
                continue
            left = _deadline - time.monotonic() if _deadline is not None else RM_TIMEOUT_SECONDS
            if left < 1:
                refused["hard-deadline"] += 1
                continue
            secs = int(min(RM_TIMEOUT_SECONDS, left))
            rc = op(TIMEOUT, "-s", "TERM", str(secs), RM, "-rf", "--one-file-system", "--",
                    cand, timeout=secs + 30).returncode
            if rc == 0:
                removed += 1
                failed.pop(cand, None)
            else:
                refused["rm-timeout" if rc == 124 else "rm-failed"] += 1
                failed[cand] = time.time()
    return removed, refused, None


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Strip regenerable dirs from finished Operator run folders; dry-run unless --apply.",
        # Abbreviations stay off: with the default True, --a/--ap/--appl all
        # mean --apply, which would defeat the dry-run wrapper's refusal.
        allow_abbrev=False)
    parser.add_argument("--apply", action="store_true", help="delete; default is a dry run")
    parser.add_argument("--runs-root", default=ROOT)
    parser.add_argument("--max-remove", type=int, default=MAX_REMOVE_DEFAULT)
    parser.add_argument("--time-budget", type=float, default=TIME_BUDGET_DEFAULT, help="seconds per phase")
    args = parser.parse_args(argv)
    if args.max_remove < 0 or not args.time_budget >= 0:  # NaN fails every comparison, so the negation catches it
        parser.error("limits must be >= 0")
    return args


def acquire_lock():
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def load_failed():
    try:
        with open(FAILED_PATH) as fh:
            data = json.load(fh)
        return {str(path): float(stamp) for path, stamp in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_failed(failed):
    now = time.time()
    keep = {path: stamp for path, stamp in failed.items() if now - stamp < FAILED_COOLDOWN_SECONDS}
    failed.clear()
    failed.update(keep)
    try:
        with open(FAILED_PATH + ".tmp", "w") as fh:
            json.dump(keep, fh, sort_keys=True)
        os.replace(FAILED_PATH + ".tmp", FAILED_PATH)
    except OSError as exc:
        return str(exc)
    return None


def archive_ledger_path():
    return os.path.join(MANIFEST_DIR, "regen-strip-archive-records.tsv")


def load_archive_records():
    """rid -> (offsite_name, sha256) for runs a previous firing verified.

    A missing ledger means no coverage, never an error. A ledger that cannot
    be read, or holds a malformed line, raises Unverifiable: coverage that
    cannot be proven keeps its local paths.
    """
    try:
        with open(archive_ledger_path(), "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise Unverifiable(f"archive ledger unreadable: {exc}")
    records = {}
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise Unverifiable("archive ledger malformed")
        rid, name, sha, _stamp = parts
        if (not UUID_RE.fullmatch(rid) or not HEX64_RE.fullmatch(sha)
                or not ARCHIVE_NAME_RE.fullmatch(name)):
            raise Unverifiable("archive ledger malformed")
        records[rid] = (name, sha)
    return records


def archive_source_snippet():
    return "source {}".format(shlex.quote(ARCHIVE_LIB))


def check_archive_lib(timeout=60):
    """Prove the retention owner's upload interface is reachable.

    Requires the exact two commands the reference implementation relies on
    (offsite_put for upload, offsite_get for the re-download readback).
    Anything missing aborts closed: the gate must never silently pass.
    Runs as the invoking unit user (ubuntu publisher on the host) so the
    retention-owner lib stays readable without widened home permissions.
    """
    if not os.path.isfile(ARCHIVE_LIB):
        raise Unverifiable("archive lib unavailable")
    r = run([BASH, "-c", "{}; command -v offsite_put >/dev/null"
             " && command -v offsite_get >/dev/null".format(archive_source_snippet())],
            timeout)
    if r.returncode != 0:
        raise Unverifiable("archive lib missing offsite_put/offsite_get")


def operator_producer_prefix():
    """OP_PREFIX with sudo resolved to an absolute path.

    The outer archive orchestration runs under `BASH_ENV`, which pins PATH
    for every `bash -c`; a bare `sudo` inside that script would not resolve
    (and must not depend on the caller PATH). Resolving here — via the
    invoking user's PATH, which also carries the offline-test stub — keeps
    the producer constrained to the operator sudo grant (which gains
    /usr/bin/tar over the #871 grant; see runbook Sudo).
    """
    if not OP_PREFIX or "/" in OP_PREFIX[0]:
        return list(OP_PREFIX)
    resolved = shutil.which(OP_PREFIX[0])
    if resolved:
        return [resolved, *OP_PREFIX[1:]]
    return list(OP_PREFIX)


def offsite_upload(name, parent, base, timeout):
    """tar parent/base (minus regenerable output) -> offsite_put.

    Returns the 64-hex upload sha, or None when the upload cannot be proven.
    Never merely because the uploader exited 0: the sha contract is required.
    The orchestration (source <lib>; offsite_put) runs as the invoking unit
    user (ubuntu publisher on the host); only the tar producer runs as
    paperclip-operator through OP_PREFIX sudo (/usr/bin/tar — a sudoers
    change from the #871 grant, reviewed by the Operator and CISO before
    step 6), so it reads exactly what rm may later delete.
    """
    producer_argv = [*operator_producer_prefix(), TAR_BIN, "-C", parent]
    producer_argv += ["--exclude={}".format(x) for x in TAR_EXCLUDES]
    producer_argv += ["-cf", "-", base]
    producer_shell = " ".join(shlex.quote(a) for a in producer_argv)
    r = run([BASH, "-c", "{}; offsite_put {} {}".format(
        archive_source_snippet(), shlex.quote(name), producer_shell)],
        timeout)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    sha = r.stdout.strip().splitlines()[-1].strip()
    return sha if HEX64_RE.fullmatch(sha) else None


def offsite_readback_sha(name, timeout):
    """Re-download the archive and hash the bytes received. Hex digest or None.

    pipefail: without it a failed offsite_get (empty stdout) would still
    exit 0 via sha256sum hashing empty input, misreporting transport
    failure as a (mismatching) hash. Runs as the invoking unit user
    (ubuntu publisher on the host), matching the upload orchestration.
    """
    r = run([BASH, "-c", "set -o pipefail; {}; offsite_get {} | {}".format(
        archive_source_snippet(), shlex.quote(name), SHA256SUM_BIN)],
        timeout)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    digest = r.stdout.strip().split()[0]
    return digest if HEX64_RE.fullmatch(digest) else None


def record_archive(rid, name, sha, stamp):
    """Append one verified record atomically (tmp file + rename).

    Validates the existing ledger first so a torn earlier write aborts
    instead of compounding. Single writer: the firing lock is held on every
    path that reaches here.
    """
    path = archive_ledger_path()
    load_archive_records()
    try:
        os.makedirs(MANIFEST_DIR, exist_ok=True)
        existing = ""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        except FileNotFoundError:
            pass
        if existing and not existing.endswith("\n"):
            existing += "\n"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(existing + "\t".join((rid, name, sha, stamp)) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise Unverifiable(f"archive ledger not written: {exc}")


def ensure_archived(rid, run_real, stamp, deadline):
    """Prove offsite coverage for one run folder: (True, None) or (False, reason).

    A ledger hit means a previous firing uploaded and re-download-verified
    this run; the hit is trusted without re-download (bounded blast radius:
    rm scope stays regenerable-only under freshly rechecked gates — see the
    runbook), while the name/sha shape is revalidated on every read.
    Otherwise upload now, re-download and compare, and only then record.
    Each phase is bounded by the firing's remaining budget, rechecked
    between phases; an expired budget refuses the run as time-budget.
    Raises Unverifiable when the archive path itself cannot be checked
    (missing lib, missing commands, unreadable/malformed ledger).
    """
    if rid in load_archive_records():
        return True, None
    left = deadline - time.monotonic()
    if left < 1:
        return False, "time-budget"
    check_archive_lib(timeout=min(60, int(left)))
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    name = "{}/{}/{}-{}.tar.zst".format(ARCHIVE_CLASS, day, rid, stamp)
    parent, base = os.path.split(run_real)
    # Compression and object naming are the lib's job: the tar stream shape
    # (-cf -, excludes, .tar.zst name) is identical to the reference, which
    # relies on offsite_put for the same handling.
    left = deadline - time.monotonic()
    sha = offsite_upload(name, parent, base, min(ARCHIVE_TIMEOUT_SECONDS, max(1, int(left))))
    if sha is None:
        return False, "archive-unverified"
    left = deadline - time.monotonic()
    if left < 1:
        return False, "time-budget"
    if offsite_readback_sha(name, min(ARCHIVE_TIMEOUT_SECONDS, max(1, int(left)))) != sha:
        return False, "archive-unverified"
    record_archive(rid, name, sha, stamp)
    return True, None


def firing(args, mode):
    try:
        root_real = canonical(args.runs_root)
        names = run_dir_names(root_real)
        snapshot = db_snapshot(names)
        if names and not snapshot:
            raise Unverifiable("run state matched no folder")
        refs = live_refs()
        held = read_keep()
    except Unverifiable as exc:
        print(json.dumps({"mode": mode, "fail_closed": str(exc)}, sort_keys=True))
        return 2
    pct = home_pct()
    idle_min = None if pct is not None and pct >= EMERGENCY_HOME_PCT else IDLE_MINUTES
    failed = load_failed()
    now = time.time()
    cooling = {path for path, stamp in failed.items() if now - stamp < FAILED_COOLDOWN_SECONDS}
    start = int(time.time() // 600) % len(names) if names else 0
    order = names[start:] + names[:start]
    deadline = time.monotonic() + args.time_budget
    kept, plan, examined = Counter(), [], 0
    for rid in order:
        if time.monotonic() >= deadline or hard_expired() or len(plan) >= args.max_remove:
            break
        examined += 1
        if rid in held:
            kept["custody-hold"] += 1
            continue
        run_real = os.path.join(root_real, rid)
        reason = plan_run(rid, run_real, snapshot, refs, idle_min)
        if reason:
            kept[reason] += 1
            continue
        try:
            found = candidates(run_real)
        except Unverifiable:
            kept["scan-error"] += 1
            continue
        for cand in found:
            if len(plan) >= args.max_remove or time.monotonic() >= deadline or hard_expired():
                break
            if cand in cooling:
                kept["rm-failed-cooldown"] += 1
                continue
            reason = candidate_block(run_real, cand, idle_min)
            if reason:
                kept[reason] += 1
            else:
                plan.append((rid, run_real, cand))
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = None
    if plan:
        try:
            os.makedirs(MANIFEST_DIR, exist_ok=True)
            manifest = os.path.join(MANIFEST_DIR, f"{stamp}-{mode}.txt")
            with open(manifest, "w", errors="surrogateescape") as fh:
                fh.write("\n".join(cand for _, _, cand in plan) + "\n")
        except OSError as exc:
            print(json.dumps({"mode": mode, "fail_closed": f"manifest not written: {exc}"}, sort_keys=True))
            return 2
    removed, refused, abort, ledger_error = 0, Counter(), None, None
    if args.apply:
        # Revalidation can refuse every candidate, so per-run archive checks
        # alone cannot prove the firing's custody evidence is readable.
        try:
            load_archive_records()
        except Unverifiable as exc:
            abort = str(exc)
    if args.apply and plan and not abort:
        removed, refused, abort = apply_plan(
            plan, idle_min, time.monotonic() + args.time_budget, failed, stamp)
        ledger_error = save_failed(failed)
    # Archive coverage for the planned runs, read after apply so newly
    # verified uploads count. A dry run never uploads: pending runs are
    # reported, not blocked, because a dry run removes nothing. A ledger
    # that cannot even be read reports every planned run pending here; the
    # apply path still aborts closed per run through ensure_archived.
    try:
        _records = load_archive_records()
    except Unverifiable:
        _records = {}
    _planned = {rid for rid, _, _ in plan}
    _covered = _planned & set(_records)
    summary = {
        "at": stamp, "mode": mode, "home_pct": pct, "idle_min": idle_min,
        "run_dirs": len(names), "examined": examined, "planned": len(plan),
        "removed": removed, "kept_by_reason": dict(kept),
        "refused_by_reason": dict(refused), "fail_closed": abort, "manifest": manifest,
        "custody_held": len(held),
        "archive_verified_runs": len(_covered),
        "archive_pending_runs": len(_planned - _covered),
        "ledger_error": ledger_error, "log_error": None,
    }
    if args.apply and plan:
        try:
            with open(LOG_PATH, "a") as fh:
                fh.write(json.dumps(summary, sort_keys=True) + "\n")
        except OSError as exc:
            summary["log_error"] = str(exc)
    print(json.dumps(summary, sort_keys=True))
    return 2 if abort else 0


def main(argv=None):
    global _deadline
    args = parse_args(sys.argv[1:] if argv is None else argv)
    mode = "apply" if args.apply else "dry-run"
    if os.path.exists(PAUSE_PATH):
        print(json.dumps({"mode": mode, "paused": True}, sort_keys=True))
        return 0
    try:
        lock = acquire_lock()
    except OSError as exc:
        print(json.dumps({"mode": mode, "fail_closed": f"lock unavailable: {exc}"}, sort_keys=True))
        return 2
    if lock is None:
        print(json.dumps({"mode": mode, "skipped": "another firing holds the lock"}, sort_keys=True))
        return 0
    _deadline = time.monotonic() + HARD_BUDGET_SECONDS
    try:
        return firing(args, mode)
    finally:
        _deadline = None
        os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
