#!/usr/bin/env python3
"""prune_operator_run_dirs.py — retention for Operator SSH run folders. (case_34c2c99f.)

WHY THIS SCRIPT EXISTS.

On 2026-10-06 /home on node7b1ff338 grew ~150G in 13h (69% -> 90%). Cause: Paperclip's
SSH driver creates /home/paperclip-operator/workspace/.paperclip-runtime/runs/
<runId>/ for every Operator run and never deletes it. 259 folders held 168.8G,
and 17 runs that failed with ENOSPC during sync-back held 159G (~9.4G each):
node_modules (2.4G) plus nested worktrees .paperclip/worktrees (3.5G) and
.claude/worktrees (3.1G) per run. The host operator called the nested copies
duplicates; nothing here has verified that.

The host operator stopped the bleeding the same day (succeeded-run prune on a
timer plus a one-off regenerable-subtree trim, /home 90% -> 68%). This tool is
the durable retention policy so it does not recur:

- FULL-DIR removal. A run folder is removed only when ALL hold: its run is
  terminal, older than the per-status age bar (30 min for succeeded, 24 h for
  failed/cancelled/timed_out), no live run or scheduled retry references it,
  no live host process references it, it is idle, and every git repo inside
  it passes the git proof: the top level, each nested worktree and each
  nested repo are clean (tracked and untracked), have no stash, and have no
  commit unreachable from a remote (HEAD, detached HEAD and reflog included).
  Every git-ignored file must sit under a regenerable directory name, and
  every worktree git lists must be one of those repos. Anything unverifiable
  is kept and REPORTED, never removed.
- REGENERABLE TRIM. A terminal run folder that is kept (dirty, unpushed,
  young), or whose run row aged out of the API window, still gets its
  top-level git-ignored regenerable directories trimmed (node_modules, target,
  .venv, venv, __pycache__, .next, .turbo, .parcel-cache). Nested worktrees
  and repos are never trimmed, and the top-level workspace (.git, sources)
  stays. A trim removes build output and dependency caches without
  inspecting them first.

WHAT IT REUSES. Run liveness comes from the same control-plane reads as the
reviewed worktree pruners (ops/case_b1d141af, ops/case_b5184419): live-runs,
heartbeat-runs, per-run re-read, plus a host /proc reference scan. Git safety
extends the case_5866fa6f sweep's gates (clean, zero unpushed, zero stashes, fail
closed on any git error) to every git repo inside the folder.

SAFETY PROPERTIES (structural, not advisory).

- Dry-run is the default. NOTHING is removed without --apply, and --apply
  re-evaluates everything fresh in the same process (never a stored report).
- Immediately before each removal AND each trim the tool re-reads the run row,
  the live-run list, the process references and the idle check (a removal also
  re-proves the age bar and the git gates). A retry scheduled between scan and
  action, a live run, a freshly dirty tree, or any error in those re-reads
  skips it; every trimmed path is re-proven git-ignored just before it goes.
- Recovery-custody holds (`regen-prune-keep.txt`, shared with the regen
  strip): a held run is never removed and never trimmed, even when its run
  row aged out of the API window. An unreadable, malformed or empty hold
  file fails the whole pass closed (exit 2, nothing removed), and every
  removal and trim re-reads the file first.
- The host /proc scan is PARTIAL as a non-root user (other users' cwd/exe/fd
  are unreadable). That is counted and reported (`proc_view.denied_pids`),
  not fatal: failing closed would keep every folder forever, and the complete
  view needs root running git in agent-writable repos (core.fsmonitor in the
  repo's own .git/config is root code execution). An unlistable /proc keeps
  everything. See RUNBOOK section 6 for the accepted residual.
- Only UUID-named real directories directly under --runs-root are candidates.
  Anything else (files, symlinks, unexpected names) is kept with a reason.
- A kill switch forces dry-run: env OPERATOR_RUN_PRUNE_DISABLED=1 or the file
  <runs-root>/.operator-run-prune.disabled. The report says `kill_switch`.
- Bounded: --max-remove caps full-dir removals per pass (largest first);
  --max-trim caps subtree trims. Per-run API lookups are capped too.
- Fail closed: no API env, an API error during evaluation, an unreadable
  path, a git error or a timeout means keep. A blind --apply is refused.
- No secrets in output: stdout carries only run ids, reasons, counts and byte
  totals. Credentials travel by inherited environment, never on argv.

MODES.

  python3 ops/case_34c2c99f/prune_operator_run_dirs.py
      Dry-run (default): scan, size, print the summary. Removes nothing.
      Needs read API access. The summary's runs_total_gib is the whole
      run-folder footprint (kept folders included): what the disk monitor
      watches.

  python3 ops/case_34c2c99f/prune_operator_run_dirs.py --apply [--max-remove N]
      Re-evaluate fresh, re-read each run right before each action, remove up
      to N run folders and trim up to --max-trim regenerable subtrees. Caps of
      0 disable that action (--max-remove 0 still trims: use dry-run, or add
      --max-trim 0, for an observe-only pass).

Exit codes: 0 report/apply complete, 1 safety refusal (blind apply), 2 usage
or fatal error (API unreachable), 3 apply finished but at least one removal
or trim failed (LOUD in the journal).

Rollback: a removed folder is unrecoverable from disk. The git proof covers
every repo in the folder, and the ignored-file allowance covers only the
regenerable names. The tool does not compare the folder with any server-side
copy and claims no such comparison. Revert the commit / disable the timer to
stop the tool. A trim is recoverable by reinstalling dependencies.
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "case_b5184419"))

import terminal_worktree_cache_reclaim as reclaim  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402

TERMINAL_RUN_STATUSES = reclaim.DONE_RUN_STATUSES  # failed/succeeded/...
SUCCEEDED_AGE_HOURS_DEFAULT = 0.5   # succeeded runs: same bar as the host timer
FAILED_AGE_HOURS_DEFAULT = 24.0     # failed/cancelled/timed_out: the card's bar
TRIM_IDLE_MINUTES_DEFAULT = 60
MAX_REMOVE_DEFAULT = 50
MAX_TRIM_DEFAULT = 200
MAX_LOOKUPS_DEFAULT = 300
EVAL_WORKERS_DEFAULT = 8
KILL_FILE_NAME = ".operator-run-prune.disabled"
SAMPLE_SIZE = 10

HERE = os.path.dirname(os.path.abspath(__file__))

# Recovery-custody hold list shared with the regen strip (RUNBOOK-regen.md
# step 2c): runs listed in regen-prune-keep.txt are never removed and never
# trimmed. Terminal status never releases custody.
KEEP_PATH = os.path.join(HERE, "regen-prune-keep.txt")

# Regenerable dirs: trimmed at the top when git-ignored; never walked for nested repos.
REGENERABLE_SUBTREE_NAMES = (
    "node_modules",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".turbo",
    ".parcel-cache",
)

RUN_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HOLD_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}")


class PruneError(RuntimeError):
    """Loud, fatal, non-safety failure: API error, usage error, I/O error."""


class SafetyRefusal(RuntimeError):
    """Loud safety refusal: blind apply."""


class CustodyError(PruneError):
    """Recovery-custody hold file unreadable or malformed: fail closed."""


def _valid_api_base(base):
    # BASE host validation: the bearer below is sent ONLY to this base.
    # Refuse a base that is not an http(s) URL with a non-empty bare host --
    # no userinfo (`user@host` misdirects the credential), no whitespace (a
    # newline would inject a request directive). A well-formed hostile host
    # still passes; blocking that needs an operator-owned host allowlist,
    # not a per-script guess.
    if not base.startswith(("http://", "https://")):
        return False
    host = base.split("://", 1)[1].split("/", 1)[0]
    if not host:
        return False
    if "@" in base:
        return False
    return not any(char in " \t\n\r\x0b\x0c" for char in base)


def read_keep(path=None):
    """Recovery-custody hold set: one run UUID per line.

    Same contract as the regen strip's read_keep: blank lines and `#`
    comments (full-line or inline) are ignored; the prefix before the first
    `#` must be blank or a complete lowercase UUID. An unreadable file, a
    non-UUID prefix, or a file with no UUID and no `#` comment (empty or
    blank-only, e.g. a truncated editor write) fails closed. An intentional "no holds" file
    must contain a `#` comment. Terminal status never releases custody.
    """
    try:
        with open(path or KEEP_PATH, "r", encoding="utf-8",
                  errors="surrogateescape") as handle:
            text = handle.read()
    except OSError:
        raise CustodyError("custody holds unreadable")
    held = set()
    saw_comment = False
    for line in text.splitlines():
        prefix, marker, _ = line.partition("#")
        entry = prefix.strip()
        saw_comment = saw_comment or bool(marker)
        if not entry:
            continue
        if not HOLD_ID_RE.fullmatch(entry):
            raise CustodyError("custody holds malformed")
        held.add(entry)
    if not held and not saw_comment:
        raise CustodyError("custody holds empty")
    return held


# ---------------------------------------------------------------------------
# PURE CORE. No network, no clock reads (callers pass `now`), no environment.
# The offline suite drives every branch with fixture dirs and a fake API.
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


def run_terminal_age_hours(run, now):
    """(terminal, age_hours) for a run row. Unknown/missing stamps: (False, None).

    A run counts as terminal only when its status is terminal AND finishedAt
    parses. A terminal status with no finishedAt is unverifiable, not old.
    """
    if not run or run.get("status") not in TERMINAL_RUN_STATUSES:
        return (False, None)
    finished = parse_ts(run.get("finishedAt"))
    if finished is None:
        return (False, None)
    return (True, (now - finished) / 3600.0)


def retry_pending(run, now):
    """A scheduled retry in the future keeps the folder even when terminal."""
    stamp = parse_ts((run or {}).get("scheduledRetryAt"))
    return stamp is not None and stamp > now


def age_bar_hours(status):
    """Per-status age bar: fast for succeeded, 24h for failed/cancelled/etc."""
    if status == "succeeded":
        return SUCCEEDED_AGE_HOURS_DEFAULT
    return FAILED_AGE_HOURS_DEFAULT


def classify(full_dir_ok, terminal, age_hours, status):
    """Full-dir removal verdict from the evaluated gates.

    Returns None to proceed, else (reason, detail). Called only after the
    liveness gates (live run, retry, proc refs) and git gates passed.
    """
    if not terminal:
        return ("run-not-terminal", {})
    bar = age_bar_hours(status)
    if age_hours is None or age_hours < bar:
        return ("run-too-recent",
                {"age_hours": round(age_hours, 2) if age_hours is not None
                 else None, "bar_hours": bar})
    if not full_dir_ok:
        return ("workspace-not-verified", {})
    return None


def order_for_removal(candidates):
    """Largest run folders first: most GB per bounded removal."""
    return sorted(candidates, key=lambda r: (-r.get("bytes", 0), r["run_id"]))


def kill_switch(env, runs_root, exists=os.path.exists):
    """(tripped, why) from the env var and the marker file."""
    if str(env.get("OPERATOR_RUN_PRUNE_DISABLED", "")).strip().lower() in (
            "1", "true", "yes", "on"):
        return (True, "env OPERATOR_RUN_PRUNE_DISABLED")
    marker = os.path.join(runs_root, KILL_FILE_NAME)
    if exists(marker):
        return (True, "file %s" % marker)
    return (False, None)


def candidate_subtree_paths(run_dir):
    """Top-level regenerable directories to consider trimming in one run folder."""
    found = []
    try:
        top = sorted(os.listdir(run_dir))
    except OSError:
        return found
    for name in top:
        if name in REGENERABLE_SUBTREE_NAMES:
            full = os.path.join(run_dir, name)
            if os.path.isdir(full) and not os.path.islink(full):
                found.append(full)
    return found


def parse_argv(argv):
    parser = argparse.ArgumentParser(
        description="Retention for Operator SSH run folders. "
                    "Dry-run unless --apply.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually remove/trim (re-evaluates fresh).")
    parser.add_argument("--runs-root",
                        default="/home/paperclip-operator/workspace/"
                                ".paperclip-runtime/runs",
                        help="Run-folder parent to scan.")
    parser.add_argument("--min-age-hours", type=float, default=None,
                        help="Override the age bar for EVERY terminal status "
                             "(default: 30 min succeeded, 24 h "
                             "failed/cancelled/timed_out).")
    parser.add_argument("--max-remove", type=int, default=MAX_REMOVE_DEFAULT,
                        help="Cap on full-dir removals per pass.")
    parser.add_argument("--max-trim", type=int, default=MAX_TRIM_DEFAULT,
                        help="Cap on regenerable-subtree trims per pass.")
    parser.add_argument("--max-lookups", type=int,
                        default=MAX_LOOKUPS_DEFAULT,
                        help="Cap on per-run API lookups for run ids "
                             "outside the recent window.")
    parser.add_argument("--trim-idle-minutes", type=int,
                        default=TRIM_IDLE_MINUTES_DEFAULT)
    parser.add_argument("--workers", type=int, default=EVAL_WORKERS_DEFAULT)
    parser.add_argument("--manifest-dir", default=None,
                        help="Write a dated removal/trim manifest here.")
    opts = parser.parse_args(argv)
    for name in ("max_remove", "max_trim", "max_lookups", "trim_idle_minutes",
                 "workers"):
        if getattr(opts, name) < 0:
            parser.error("--%s must be >= 0" % name.replace("_", "-"))
    if opts.min_age_hours is not None and opts.min_age_hours < 0:
        parser.error("--min-age-hours must be >= 0")
    return opts


# ---------------------------------------------------------------------------
# LIVE SHELL.
# ---------------------------------------------------------------------------

class ProcRefs(set):
    """Paths referenced by live host processes, plus how blind the view was.

    `denied_pids` counts pids whose cwd/exe/fd links this user was not
    allowed to read (EACCES): their references are NOT in the set.
    """

    denied_pids = 0


def host_proc_refs():
    """Absolute paths referenced by live host processes (cwd/exe/fd).

    None when /proc itself is unlistable: callers treat that as unverifiable
    and keep. Otherwise a ProcRefs set. The view is partial by design when the
    tool runs as the folder owner (it cannot read other users' cwd/exe/fd):
    that is counted in `denied_pids` and reported, never treated as an error.
    Why not fail closed on it: as a non-root user some pid is always denied
    (every root service), so that would keep every folder forever; and the fix
    that makes the view complete — running as root — must not be taken,
    because root running git inside agent-writable repos is a root-code-
    execution path (core.fsmonitor in the repo's own .git/config), which is
    exactly what git's "dubious ownership" refusal prevents. The residual is a
    root-owned process holding an idle run folder (cwd/open fd only); a
    process that WRITES there is caught by the idle clock.
    """
    refs = ProcRefs()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        base = "/proc/%s" % pid
        denied = False
        for link in ("cwd", "exe"):
            try:
                refs.add(os.readlink(os.path.join(base, link)))
            except PermissionError:
                denied = True
            except OSError:
                pass
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except PermissionError:
            fds = []
            denied = True
        except OSError:
            fds = []
        for fd in fds:
            try:
                refs.add(os.readlink(os.path.join(base, "fd", fd)))
            except PermissionError:
                denied = True
            except OSError:
                pass
        if denied:
            refs.denied_pids += 1
    return refs


def dir_is_idle(run_dir, idle_minutes):
    """True when nothing under run_dir was modified within idle_minutes.

    Returns None when the tree cannot be read (unverifiable).
    """
    cutoff = time.time() - idle_minutes * 60
    unreadable = []
    try:
        for dirpath, dirnames, filenames in os.walk(
                run_dir, onerror=unreadable.append):
            # .git internals are excluded from the idle clock: git state is
            # judged by the git gates (clean/pushed/stash), not by mtime. A
            # fresh `git status` during evaluation must not keep a folder
            # alive, and real run activity always touches non-.git files
            # (or the run is young enough for the finishedAt age bar).
            if ".git" in dirnames:
                dirnames.remove(".git")
            # lstat: a dangling or outside-pointing link must not decide idleness.
            for name in filenames:
                try:
                    if os.lstat(os.path.join(dirpath, name)).st_mtime > cutoff:
                        return False
                except OSError:
                    return None
            try:
                if os.lstat(dirpath).st_mtime > cutoff:
                    return False
            except OSError:
                return None
    except OSError:
        return None
    return None if unreadable else True


def git_run(cwd, args, timeout):
    return subprocess.run(["git", "-C", cwd] + list(args),
                          capture_output=True, text=True, timeout=timeout)


def inside_folder(path, folder):
    return path == folder or path.startswith(folder + os.sep)


def owned_tree(root):
    """Files, nested repo roots and listability for one repo root."""
    files, nested, listable = [], [], True
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            listable = False
            continue
        for entry in entries:
            if entry.name == ".git":
                continue
            try:
                if entry.is_symlink():
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    files.append(entry.path)
                elif entry.name in REGENERABLE_SUBTREE_NAMES:
                    continue
                elif os.path.lexists(os.path.join(entry.path, ".git")):
                    nested.append(entry.path)
                else:
                    pending.append(entry.path)
            except OSError:
                listable = False
    return files, nested, listable


def ignored_files(root, files):
    """Subset of files git ignores; None when git cannot say."""
    if not files:
        return []
    payload = b"".join(os.fsencode(os.path.relpath(path, root)) + b"\0"
                       for path in files)
    probe = subprocess.run(
        ["git", "-C", root, "check-ignore", "--stdin", "-z"],
        input=payload, capture_output=True, timeout=300)
    if probe.returncode == 1:
        return []
    if probe.returncode != 0:
        return None
    return [item for item in probe.stdout.split(b"\0") if item]


def repo_git_verdict(root, folder):
    """(ok, reason, nested_roots) for one repo root inside folder; fail closed."""
    real = os.path.realpath(root)
    prefix = "" if real == folder else "nested-"
    probe = git_run(root, ["rev-parse", "--show-toplevel",
                           "--absolute-git-dir", "--git-common-dir"], 60)
    if probe.returncode != 0:
        # "dubious ownership" is a deploy mistake (wrong uid), named as such.
        if "dubious ownership" in (probe.stderr or ""):
            return (False, prefix + "git-dubious-ownership", [])
        return (False, prefix + "git-unreadable", [])
    lines = probe.stdout.splitlines()
    if len(lines) != 3:
        return (False, prefix + "git-unreadable", [])
    top, git_dir, common = lines
    if os.path.realpath(top) != real:
        return (False, prefix + "not-a-repo-root", [])
    if not (inside_folder(os.path.realpath(git_dir), folder)
            and inside_folder(os.path.realpath(os.path.join(root, common)),
                              folder)):
        return (False, prefix + "git-dir-outside-folder", [])
    files, nested, listable = owned_tree(root)
    if not listable:
        return (False, prefix + "unreadable", [])
    status = git_run(root, ["status", "--porcelain", "--untracked-files=all"],
                     120)
    if status.returncode != 0:
        return (False, prefix + "git-status-error", [])
    if status.stdout.strip():
        return (False, prefix + "dirty", [])
    stash = git_run(root, ["stash", "list"], 60)
    if stash.returncode != 0:
        return (False, prefix + "git-stash-error", [])
    if stash.stdout.strip():
        return (False, prefix + "stash-present", [])
    unpushed = git_run(root, ["rev-list", "-n", "1", "--all", "--reflog",
                              "--not", "--remotes"], 120)
    if unpushed.returncode != 0:
        return (False, prefix + "git-log-error", [])
    if unpushed.stdout.strip():
        return (False, prefix + "unpushed-commits", [])
    ignored = ignored_files(root, files)
    if ignored is None:
        return (False, prefix + "git-check-ignore-error", [])
    if ignored:
        return (False, prefix + "ignored-unlisted", [])
    return (True, None, nested)


def folder_git_verdict(run_dir):
    folder = os.path.realpath(run_dir)
    pending, seen = [run_dir], set()
    while pending:
        root = pending.pop()
        real = os.path.realpath(root)
        if real in seen:
            continue
        seen.add(real)
        ok, why, nested = repo_git_verdict(root, folder)
        if not ok:
            return (False, why)
        pending.extend(nested)
    listed = git_run(run_dir, ["worktree", "list", "--porcelain"], 60)
    if listed.returncode != 0:
        return (False, "git-worktree-list-error")
    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        if os.path.realpath(line[len("worktree "):]) not in seen:
            return (False, "worktree-unclassified")
    return (True, None)


def git_verdict(run_dir):
    """(ok, reason): may this run folder go as a whole? Any doubt is a keep."""
    try:
        return folder_git_verdict(run_dir)
    except Exception:  # noqa: BLE001 — unverifiable is never clean
        return (False, "git-verdict-error")


def path_is_git_ignored(run_dir, path):
    """True only when git itself considers path ignored. Errors -> False."""
    try:
        rel = os.path.relpath(path, run_dir)
    except ValueError:
        return False
    probe = subprocess.run(
        ["git", "-C", run_dir, "check-ignore", "-q", "--", rel],
        capture_output=True, timeout=60)
    return probe.returncode == 0


def dir_bytes(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def entry_bytes(path):
    """Bytes under one runs-root entry; a symlink counts 0 (never followed)."""
    if os.path.islink(path):
        return 0
    if os.path.isdir(path):
        return dir_bytes(path)
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def fetch_run(api, run_id):
    """One run row by id, or None on 404. Raises on other API errors."""
    try:
        return api._get("%s/heartbeat-runs/%s" % (api.plain, run_id))
    except Exception as exc:  # noqa: BLE001 — 404 vs real error below
        message = str(exc)
        if "404" in message or "Not Found" in message:
            return None
        raise


def build_run_index(api, run_ids, max_lookups):
    """Map run_id -> run row: recent window first, then bounded per-id GETs."""
    index = {}
    try:
        recent = api.heartbeat_runs()
    except Exception:  # noqa: BLE001 — fail closed upstream
        raise
    for row in recent or []:
        if not isinstance(row, dict):
            continue
        if row.get("id") in run_ids:
            index[row["id"]] = row
        ext = row.get("externalRunId")
        if ext in run_ids:
            index[ext] = row
    lookups = 0
    for run_id in sorted(run_ids):
        if run_id in index or lookups >= max_lookups:
            continue
        row = fetch_run(api, run_id)
        lookups += 1
        if row is not None:
            index[run_id] = row
    return index


def evaluate_run(run_dir, run_id, run, live_issue_ids, proc_refs, now,
                 min_age_hours_override, trim_idle_minutes, held=None):
    """Full verdict for one run folder. Never raises: errors keep (fail closed)."""
    res = {"run_id": run_id, "dir": run_dir, "reason": None, "det": {},
           "bytes": 0, "trim": []}
    try:
        return _evaluate_run(run_dir, run_id, run, live_issue_ids, proc_refs,
                             now, min_age_hours_override, trim_idle_minutes,
                             res, held)
    except Exception as exc:  # noqa: BLE001 — fail closed, loudly reported
        res["reason"] = "eval-error"
        res["det"] = {"error": type(exc).__name__}
        return res


def _evaluate_run(run_dir, run_id, run, live_issue_ids, proc_refs, now,
                  min_age_hours_override, trim_idle_minutes, res, held=None):
    if held and run_id in held:
        # Recovery-custody hold: never remove and never trim, even when the
        # run row aged out (unknown runs trim by default).
        res["reason"] = "custody-hold"
        return res
    if run is None:
        res["reason"] = "run-unknown"
    else:
        # A scheduled retry may resume INTO this folder: never remove it and
        # never trim it (trimming node_modules would force a reinstall on
        # retry). A non-terminal run may still be executing: hands off too.
        # Both return before the trim tail below.
        if retry_pending(run, now):
            res["reason"] = "retry-scheduled"
            return res
        terminal, age_hours = run_terminal_age_hours(run, now)
        if not terminal:
            res["reason"] = "run-not-terminal"
            res["det"] = {"status": run.get("status")}
            return res
        issue_id = run.get("issueId")
        if issue_id and issue_id in live_issue_ids:
            # A live run on the same issue may be sharing or resuming this
            # work: hands off entirely, no removal and no trim.
            res["reason"] = "live-run"
            res["det"] = {"issue": issue_id}
            return res
        bar = (min_age_hours_override
               if min_age_hours_override is not None
               else age_bar_hours(run.get("status")))
        if age_hours is None or age_hours < bar:
            res["reason"] = "run-too-recent"
            res["det"] = {"age_hours": round(age_hours, 2)
                          if age_hours is not None else None,
                          "bar_hours": bar,
                          "status": run.get("status")}
        else:
            ok, why = git_verdict(run_dir)
            if not ok:
                res["reason"] = why
                res["det"] = {"status": run.get("status")}
    if proc_refs is None:
        res["reason"] = "proc-unreadable"
        return res
    for ref in proc_refs:
        if ref == run_dir or ref.startswith(run_dir + os.sep):
            res["reason"] = "live-process-ref"
            return res
    idle = dir_is_idle(run_dir, trim_idle_minutes)
    if idle is not True:
        res["reason"] = ("run-active" if idle is False
                         else "unreadable")
        return res
    # Trim tail: every folder that reaches here is terminal-or-unknown (an
    # unknown run already carries reason "run-unknown": trim only, never
    # delete) with no scheduled retry or live run, idle, and unreferenced — so
    # one pass relieves pressure even when the whole dir must stay.
    res["trim"] = plan_trim(run_dir, run is not None)
    return res


def plan_trim(run_dir, run_known):
    """Regenerable-subtree trim plan. Git-ignored paths only, never tracked."""
    plans = []
    for path in candidate_subtree_paths(run_dir):
        if not path_is_git_ignored(run_dir, path):
            continue
        plans.append({"path": path, "bytes": dir_bytes(path),
                      "run_known": run_known})
    return plans


def revalidate(run_dir, run_id, api, trim_idle_minutes,
               min_age_hours_override=None, removal=True):
    """Fresh proof immediately before a removal or a trim.

    Returns None when the action may proceed, else a skip reason. Both modes
    re-read the run row, the live-run list, the scheduled-retry stamp, the
    host process references and the idle clock. `removal` adds what only a
    full-dir delete needs: a KNOWN terminal run past its age bar (honouring
    --min-age-hours, same as the evaluation) and a fresh git-clean proof. A
    trim of an unknown (aged-out) run is allowed, as in the evaluation.
    Both modes re-read the recovery-custody hold file first: a held run is
    skipped, and an unreadable, malformed or empty file skips the action
    instead of acting on a stale plan.
    """
    try:
        held_now = read_keep()
    except CustodyError:
        return "custody-hold-reread-failed"
    if run_id in held_now:
        return "custody-hold"
    try:
        run = fetch_run(api, run_id)
    except Exception:  # noqa: BLE001 — reread failed: keep
        return "run-reread-failed"
    now = time.time()
    if run is None:
        if removal:
            return "run-unknown"
    else:
        try:
            live_rows = api.live_runs()
        except Exception:  # noqa: BLE001 — keep
            return "live-runs-unreadable"
        live_issue_ids = {row.get("issueId") for row in live_rows or []
                          if row.get("issueId")}
        if run.get("issueId") and run.get("issueId") in live_issue_ids:
            return "live-run-appeared"
        if retry_pending(run, now):
            return "retry-scheduled"
        terminal, age_hours = run_terminal_age_hours(run, now)
        if not terminal:
            return "run-not-terminal-anymore"
        if removal:
            bar = (min_age_hours_override
                   if min_age_hours_override is not None
                   else age_bar_hours(run.get("status")))
            if age_hours is None or age_hours < bar:
                return "run-too-recent"
    if removal:
        ok, _ = git_verdict(run_dir)
        if not ok:
            return "became-unverifiable"
    refs = host_proc_refs()
    if refs is None:
        return "proc-unreadable"
    for ref in refs:
        if ref == run_dir or ref.startswith(run_dir + os.sep):
            return "live-process-ref"
    if dir_is_idle(run_dir, trim_idle_minutes) is not True:
        return "became-active"
    return None


def apply_plan(results, api, live_issue_ids, opts):
    """Remove/trim, re-validating each full-dir removal and each trim first.

    Nothing here acts on the scan-time verdict alone: a retry scheduled, a
    live run, fresh activity or a freshly dirty tree between scan and action
    skips it. Trims are revalidated once per run folder (never per path), and
    each path is re-proven git-ignored right before it is removed.
    """
    removed, trim_removed = [], []
    errors = []
    skipped = collections.Counter()
    trim_bytes_total = 0
    n_removed = 0
    for res in order_for_removal([r for r in results
                                  if r["reason"] is None]):
        if n_removed >= opts.max_remove:
            skipped["over-max-remove"] += 1
            continue
        why = revalidate(res["dir"], res["run_id"], api,
                         opts.trim_idle_minutes, opts.min_age_hours)
        if why is not None:
            skipped[why] += 1
            continue
        size = dir_bytes(res["dir"])
        try:
            shutil.rmtree(res["dir"])
        except OSError as exc:
            errors.append({"run_id": res["run_id"], "rc": str(exc)})
            continue
        if os.path.exists(res["dir"]):
            errors.append({"run_id": res["run_id"], "rc": "still-exists"})
            continue
        removed.append({"run_id": res["run_id"], "bytes": size})
        n_removed += 1
    n_trimmed = 0
    for res in results:
        plans = [p for p in res.get("trim", []) if os.path.isdir(p["path"])]
        if not plans:
            continue
        if n_trimmed >= opts.max_trim:
            skipped["over-max-trim"] += len(plans)
            continue
        why = revalidate(res["dir"], res["run_id"], api,
                         opts.trim_idle_minutes, removal=False)
        if why is not None:
            skipped["trim-" + why] += 1
            continue
        for plan in plans:
            if n_trimmed >= opts.max_trim:
                skipped["over-max-trim"] += 1
                continue
            if not path_is_git_ignored(res["dir"], plan["path"]):
                skipped["trim-not-ignored-anymore"] += 1
                continue
            try:
                shutil.rmtree(plan["path"])
            except OSError as exc:
                errors.append({"run_id": res["run_id"],
                               "path": plan["path"], "rc": str(exc)})
                continue
            trim_removed.append({"run_id": res["run_id"],
                                 "path": os.path.relpath(
                                     plan["path"], res["dir"]),
                                 "bytes": plan["bytes"]})
            trim_bytes_total += plan["bytes"]
            n_trimmed += 1
    return {"removed": len(removed),
            "removed_bytes": sum(r["bytes"] for r in removed),
            "removed_sample": [r["run_id"] for r in removed[:SAMPLE_SIZE]],
            "trimmed": len(trim_removed),
            "trimmed_bytes": trim_bytes_total,
            "trimmed_sample": ["%s:%s" % (r["run_id"], r["path"])
                               for r in trim_removed[:SAMPLE_SIZE]],
            "skipped": dict(skipped), "errors": errors[:20],
            "n_errors": len(errors)}


def summarize(results, apply_report, killed, opts):
    kept = collections.Counter(r["reason"] for r in results if r["reason"])
    removable = [r for r in results if r["reason"] is None]
    total = sum(r.get("bytes", 0) for r in removable)
    footprint = sum(r.get("bytes", 0) for r in results)
    trim_plans = sum(len(r.get("trim", [])) for r in results)
    trim_bytes = sum(p.get("bytes", 0) for r in results
                     for p in r.get("trim", []))
    return {
        "run_dirs_scanned": len(results),
        "max_remove": opts.max_remove,
        "max_trim": opts.max_trim,
        "kill_switch": killed,
        "removable": len(removable),
        "removable_bytes": total,
        "removable_gib": round(total / 2 ** 30, 2),
        "runs_total_bytes": footprint,
        "runs_total_gib": round(footprint / 2 ** 30, 2),
        "kept_by_reason": dict(kept),
        "trim_planned": trim_plans,
        "trim_planned_bytes": trim_bytes,
        "trim_planned_gib": round(trim_bytes / 2 ** 30, 2),
        "apply": apply_report,
    }


def scan_run_dirs(runs_root):
    """(candidates, kept) — candidates are (dir, run_id) pairs to evaluate;
    kept are result rows for entries that can never be removed."""
    candidates, kept = [], []
    try:
        entries = sorted(os.listdir(runs_root))
    except OSError as exc:
        raise PruneError("cannot list runs root %s: %s" % (runs_root, exc))
    for name in entries:
        if name == KILL_FILE_NAME:
            continue
        full = os.path.join(runs_root, name)
        if not RUN_ID_RE.match(name):
            kept.append({"run_id": name, "dir": full, "reason":
                         "unexpected-name", "det": {}, "bytes": 0,
                         "trim": []})
            continue
        if os.path.islink(full) or not os.path.isdir(full):
            kept.append({"run_id": name, "dir": full, "reason":
                         "not-a-directory", "det": {}, "bytes": 0,
                         "trim": []})
            continue
        candidates.append((full, name))
    return candidates, kept


def write_manifest(manifest_dir, payload, stamp):
    os.makedirs(manifest_dir, exist_ok=True)
    path = os.path.join(manifest_dir,
                        "operator-run-prune-%s.json" % stamp)
    write_json_atomic(path, payload)
    return path


def run_tool(opts, api, env, clock=time.time):
    started = clock()
    killed, why = kill_switch(env, opts.runs_root)
    apply_requested = opts.apply
    if killed:
        print("KILL-SWITCH (%s): apply disabled, dry-run only" % why,
              file=sys.stderr)
        apply_requested = False
    if not os.path.isdir(opts.runs_root) or os.path.islink(opts.runs_root):
        raise PruneError("runs root is not a real directory: %s"
                         % opts.runs_root)
    # Recovery-custody holds fail the whole pass closed before anything is
    # evaluated or removed. CustodyError is a PruneError: exit 2, loud.
    held = read_keep()
    try:
        live_rows = api.live_runs()
    except Exception as exc:
        raise PruneError("live-runs unreadable: %s: %s"
                         % (type(exc).__name__, exc)) from exc
    live_issue_ids = {row.get("issueId") for row in live_rows or []
                      if row.get("issueId")}
    candidates, kept_rows = scan_run_dirs(opts.runs_root)
    run_ids = {run_id for _, run_id in candidates}
    try:
        index = build_run_index(api, run_ids, opts.max_lookups)
    except Exception as exc:
        raise PruneError("run index unreadable: %s: %s"
                         % (type(exc).__name__, exc)) from exc
    proc_refs = host_proc_refs()
    now = clock()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=opts.workers) as pool:
        evaluated = list(pool.map(
            lambda item: evaluate_run(
                item[0], item[1], index.get(item[1]), live_issue_ids,
                proc_refs, now, opts.min_age_hours, opts.trim_idle_minutes,
                held),
            candidates))
    results = kept_rows + evaluated
    # Size EVERY entry: the footprint figure must count kept folders too
    # (dirty, unpushed, unverifiable), not only the reclaimable ones.
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=opts.workers) as pool:
        sizes = list(pool.map(lambda r: entry_bytes(r["dir"]), results))
    for res, size in zip(results, sizes):
        res["bytes"] = size
    apply_report = None
    if apply_requested:
        apply_report = apply_plan(results, api, live_issue_ids, opts)
    summary = summarize(results, apply_report, why if killed else None, opts)
    summary["custody_held"] = len(held)
    summary["proc_view"] = {
        "readable": proc_refs is not None,
        "denied_pids": getattr(proc_refs, "denied_pids", 0)}
    summary["took_s"] = round(clock() - started)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started))
    try:
        if opts.manifest_dir and apply_report is not None:
            summary["manifest"] = write_manifest(
                opts.manifest_dir,
                {"summary": summary,
                 "results": [{k: r[k] for k in ("run_id", "reason", "det",
                                               "bytes")}
                             for r in results]},
                stamp)
    except OSError as exc:
        print(json.dumps(summary, indent=1), flush=True)
        raise PruneError("write failed: %s: errno=%s" %
                         (os.path.join(opts.manifest_dir,
                                       "operator-run-prune-%s.json" % stamp),
                          exc.errno)) from exc
    print(json.dumps(summary, indent=1))
    return summary


def main(argv=None, api=None, env=None):
    opts = parse_argv(argv if argv is not None else sys.argv[1:])
    env = os.environ if env is None else env
    if api is None:
        base = env.get("PAPERCLIP_API_URL")
        company = env.get("PAPERCLIP_COMPANY_ID")
        key = env.get("PAPERCLIP_API_KEY")
        if not (base and company and key):
            if opts.apply:
                raise SafetyRefusal(
                    "refusing --apply without PAPERCLIP_API_URL/COMPANY_ID/"
                    "KEY: the per-removal run re-read would be impossible")
            raise PruneError("API env is required even for dry-run (run "
                             "statuses come from the control plane)")
        if not _valid_api_base(base.rstrip("/")):
            if opts.apply:
                raise SafetyRefusal(
                    "refusing --apply with a PAPERCLIP_API_URL that is not "
                    "a clean http(s) base: the bearer would misdirect")
            raise PruneError(
                "refusing: PAPERCLIP_API_URL is not a clean http(s) base")
        api = reclaim.ApiClient(base, company, key)
    try:
        summary = run_tool(opts, api, env)
    except SafetyRefusal:
        raise
    except Exception as exc:  # API error during evaluation etc.
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
