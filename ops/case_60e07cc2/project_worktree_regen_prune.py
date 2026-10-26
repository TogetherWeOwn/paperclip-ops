#!/usr/bin/env python3
"""project_worktree_regen_prune.py — per-worktree regen strip for project checkouts. (case_60e07cc2, parent case_b0e822f6 item 2.)

Replaces the host hourly prune-project-worktree-regen.py (captured read-only as
[case_2c93d621](/TOG/issues/case_2c93d621) document host-prune-evidence, sha256
68293f19acf8521bcc4174af09ccb591f6a1af287b2f1ad56ba8d715884763cd): the same
candidates (side checkouts plus nested .paperclip/.claude worktrees under the
projects root), the same regen set (node_modules/target/.next/.turbo/
.pnpm-store), the same stale (done/cancelled, or no run for 24h) + 6h-idle +
git-ignored-and-untracked gates — but the liveness check is PER WORKTREE,
never per project.

THE BUG IT FIXES. The host script computes one `busy_project` flag per project
(any host cwd anywhere under <ROOT>/<company>/<project>, e.g. an SSH upload in
progress) and then skips EVERY stale worktree in that whole project. One live
upload pins gigabytes of dead regen output across all sibling worktrees. This
tool drops that flag: a worktree is kept only when a live reference points at
THAT worktree (its card in /live-runs, a run snapshot path under it, or a host
process cwd/exe/fd inside it). A cwd elsewhere in the same project protects
nothing. The offline suite kills the whole-project mutant by construction and
pins the old flag's absence in source.

WHAT IT REUSES. reclaim.path_referenced (the trailing-slash join that keeps a
ref to /w/case_8c5fe4cc from matching the sibling /w/case_de259302), reclaim.cache_dir_ok
(ignored AND untracked AND not-a-link), reclaim.ApiClient (issues,
heartbeat-runs, live-runs), reclaim.refresh_live_refs (live/queued/retrying
run paths and issue ids), and the kill-switch / manifest shapes proven by
ops/case_34c2c99f/prune_operator_run_dirs.py. What it does NOT reuse is
reclaim.evaluate_worktree: that gate needs execution-workspace DB rows, which
project side-checkouts do not have.

SAFETY (structural, suite-pinned).

- Dry-run is the default. NOTHING is removed without --apply, and --apply
  lives on argv only (the shipped unit carries it literally): no env file can
  supply the mode bit.
- Fail closed. An unreadable /proc, an API error during evaluation, a git
  error, an unknown card (a TOG name with no board row), or an open card with
  no run history inside the heartbeat window all read as keep, never strip.
  The last two are STRICTER than the host script (which strips an open card
  whose runs aged out of any window): unknown means keep here.
- Only the five host regen names, only when git itself proves each one ignored
  AND untracked; symlinks are recorded and never followed; every removal is
  containment-checked under its candidate.
- Bounded. --max-remove caps regen-dir removals per pass, largest first; the
  shipped unit defaults the cap to 0 (observe-only: full evaluation, zero
  removals) until the operator raises it after reading a report.
- A kill switch forces dry-run: env PROJECT_REGEN_PRUNE_DISABLED or the file
  <manifest-dir>/.project-regen-prune.disabled. The report says so loudly.
- Removal failures are LOUD (counted, exit 3). The host script ignored the
  rm return code; this tool does not.
- Runs as the project owner (ubuntu), never root: root running git inside
  agent-writable repos is a code-execution path (see host_proc_refs in
  case_34c2c99f), which is exactly what git's "dubious ownership" refusal
  prevents. Root-owned residue is therefore REPORTED, not silently skipped.

MODES.

  python3 ops/case_60e07cc2/project_worktree_regen_prune.py
      Dry-run (default): scan, print the summary JSON, write a manifest.
      Removes nothing. Needs read API access.

  python3 ops/case_60e07cc2/project_worktree_regen_prune.py --apply [--max-remove N]
      Re-evaluate everything fresh, re-read each card right before its
      removals, remove up to N regen dirs, print + write the apply report.
      Refuses without API env (a blind apply with no live re-read is worse
      than no apply).

Exit codes: 0 report/apply complete, 1 safety refusal (blind apply), 2 usage
or fatal error (API unreachable, no manifest dir), 3 apply finished but at
least one removal failed (LOUD in the journal).

No secrets in output: stdout carries only worktree paths, card identifiers,
statuses, counts and byte totals. Credentials travel by inherited environment,
never on argv.

Rollback: disable the timer (or touch the kill-switch file) and re-enable the
previous host user timer; deleted regen dirs rebuild via the package manager
(`npm ci`, `cargo fetch`, ...). No worktree itself is ever removed: full
worktree removal stays with the case_b1d141af pruner, same as the host script this
tool replaces (which also never removed a worktree).
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime
import json
import os
import re
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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "lib"))
from paperclip_api_allowlist import (
    AllowlistRefused, check_api_base_allowed)

# Host parity: the installed host script strips exactly these five names.
REGEN_NAMES = ("node_modules", "target", ".next", ".turbo", ".pnpm-store")
REGEN_SET = frozenset(REGEN_NAMES)
STALE_HOURS_DEFAULT = 24
IDLE_HOURS_DEFAULT = 6
# Host parity: the idle probe is `find <D> -maxdepth 3 -newermt -<IDLE>h -not
# -path "*/node_modules/*"`. Only node_modules subtrees are excluded from the
# clock; .git internals count, same as the host find. A rebuild touching
# target/ therefore keeps the candidate, which is the safe direction.
IDLE_FIND_MAXDEPTH = 3
IDLE_FIND_EXCLUDE = "node_modules"
MAX_REMOVE_DEFAULT = 200
EVAL_WORKERS_DEFAULT = 6
TERMINAL_STATUSES = reclaim.TERMINAL_STATUSES
# Host layout (the script this tool replaces runs on the host). Overridable
# for the offline suite and for nonstandard hosts via argv/env.
PROJECTS_ROOT_DEFAULT = (
    "/home/ubuntu/stacks/paperclip/data/instances/default/projects")
CONTAINER_PREFIX = "/paperclip"
HOST_DATA_PREFIX = "/home/ubuntu/stacks/paperclip/data"
MANIFEST_DIR_DEFAULT = "/var/lib/paperclip/project-regen-prune"
KILL_FILE_NAME = ".project-regen-prune.disabled"
KILL_ENV_VAR = "PROJECT_REGEN_PRUNE_DISABLED"
SAMPLE_SIZE = 10
TOG = re.compile(r"tog[-_]?(\d{3,6})", re.I)
UPGRADE_PREFIX = "upgrade-"
RUN_CREATED_KEYS = ("created_at", "createdAt", "started_at", "startedAt")


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
# The offline suite drives every branch with fixtures and throwaway local git
# repos. Every branch below has a test that kills its mutant — notably the
# per-worktree liveness gate, where restoring the host's whole-project skip
# must fail the suite.
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


def to_host(path):
    """Map a container-originated /paperclip path to its host path.

    Host /proc entries for bind-mounted container processes already carry
    host paths; only /paperclip-prefixed strings are rewritten, so the map
    is idempotent on host paths.
    """
    if path == CONTAINER_PREFIX:
        return HOST_DATA_PREFIX
    if path.startswith(CONTAINER_PREFIX + "/"):
        return HOST_DATA_PREFIX + path[len(CONTAINER_PREFIX):]
    return path


def worktree_busy(worktree, proc_refs):
    """True when a live host process references THIS worktree (per-worktree).

    There is deliberately NO project-level check here: a cwd elsewhere in
    the same project (e.g. an SSH upload into a sibling checkout) must not
    protect this worktree. That whole-project skip was the host bug.
    """
    return reclaim.path_referenced(worktree, proc_refs)


def stale_gate(status, hours_since_run, stale_hours, has_card, basename):
    """(stale, reason). Unknown cards and unknown run history keep.

    - done/cancelled -> stale (the card will not come back for its caches).
    - upgrade- checkouts carry no card -> stale (host parity).
    - open card, run inside the heartbeat window -> stale only when the
      newest run is older than the bar.
    - open card, no run inside the heartbeat window -> keep
      ("run-history-unknown"). STRICTER than the host script, which strips
      those: an unseen history is unverifiable, never old.
    - TOG name with no board row at all -> keep ("card-unknown").
    """
    if not has_card:
        if basename.startswith(UPGRADE_PREFIX):
            return (True, None)
        return (False, "card-unknown")
    if status in TERMINAL_STATUSES:
        return (True, None)
    if hours_since_run is None:
        return (False, "run-history-unknown")
    if hours_since_run >= stale_hours:
        return (True, None)
    return (False, "card-active")


def run_created_ts(row):
    """Best parsed creation stamp from a heartbeat-run row, else None."""
    for key in RUN_CREATED_KEYS:
        stamp = parse_ts(row.get(key))
        if stamp is not None:
            return stamp
    return None


def card_hours_since_run(heartbeat_rows, issue_id, now):
    """Hours since the card's newest run in the window, or None when unseen.

    The endpoint serves the newest ~200 runs only: a card with no row here
    has an UNVERIFIABLE history, which the stale gate reads as keep.
    """
    newest = None
    for row in heartbeat_rows or []:
        if not isinstance(row, dict):
            continue
        snap = row.get("contextSnapshot") or row.get("context_snapshot") or {}
        if snap.get("issueId") != issue_id and snap.get("issue_id") != issue_id:
            continue
        stamp = run_created_ts(row)
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    if newest is None:
        return None
    return (now - newest) / 3600.0


def kill_switch(env, manifest_dir, exists=os.path.exists):
    """(tripped, why) from the env var and the marker file."""
    if str(env.get(KILL_ENV_VAR, "")).strip().lower() in (
            "1", "true", "yes", "on"):
        return (True, "env %s" % KILL_ENV_VAR)
    if exists(os.path.join(manifest_dir, KILL_FILE_NAME)):
        return (True, "file %s" % os.path.join(manifest_dir,
                                               KILL_FILE_NAME))
    return (False, None)


def order_for_removal(candidates):
    """Largest regen dirs first, then path: most bytes per bounded removal."""
    return sorted(candidates,
                  key=lambda c: (-c.get("bytes", 0), c["p"]))


def card_idents(name):
    """Candidate board identifiers for a checkout basename, exact first."""
    match = TOG.search(name)
    if not match:
        return []
    digits = match.group(1)
    idents = ["TOG-%s" % digits]
    stripped = digits.lstrip("0")
    if stripped and stripped != digits:
        idents.append("TOG-%s" % stripped)
    return idents


def parse_argv(argv):
    """Argv parsing, isolated so the suite can pin dry-run-by-default."""
    parser = argparse.ArgumentParser(
        description="Strip regenerable build output from stale project "
                    "worktrees with per-worktree liveness. "
                    "Dry-run unless --apply.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually remove (re-evaluates everything fresh).")
    parser.add_argument("--projects-root", default=None,
                        help="Projects root to scan (default: host path "
                             "below, or $PROJECT_REGEN_PRUNE_PROJECTS_ROOT).")
    parser.add_argument("--manifest-dir", default=None,
                        help="Manifest directory (default: host path below, "
                             "or $PROJECT_REGEN_PRUNE_MANIFEST_DIR).")
    parser.add_argument("--stale-hours", type=float,
                        default=STALE_HOURS_DEFAULT)
    parser.add_argument("--idle-hours", type=float,
                        default=IDLE_HOURS_DEFAULT)
    parser.add_argument("--max-remove", type=int, default=MAX_REMOVE_DEFAULT,
                        help="Cap on regen-dir removals per pass "
                             "(0 = observe only).")
    parser.add_argument("--workers", type=int, default=EVAL_WORKERS_DEFAULT)
    opts = parser.parse_args(argv)
    if opts.max_remove < 0 or opts.stale_hours < 0 or opts.idle_hours < 0:
        parser.error("--max-remove, --stale-hours and --idle-hours "
                     "must be >= 0")
    if opts.projects_root is None:
        opts.projects_root = os.environ.get(
            "PROJECT_REGEN_PRUNE_PROJECTS_ROOT", PROJECTS_ROOT_DEFAULT)
    if opts.manifest_dir is None:
        opts.manifest_dir = os.environ.get(
            "PROJECT_REGEN_PRUNE_MANIFEST_DIR", MANIFEST_DIR_DEFAULT)
    return opts


# ---------------------------------------------------------------------------
# LIVE SHELL. Filesystem + git + control-plane reads. Git proof resolves the
# candidate's own gitdir (mapping container-originated worktree pointers to
# host paths) so no docker exec is needed; every git failure fails closed.
# ---------------------------------------------------------------------------

def enumerate_candidates(projects_root):
    """Side checkouts + nested worktrees under the projects root (host parity).

    Mirrors the installed host script: for each <root>/<company>/<project>,
    every direct child whose basename carries TOG-NNNN (or starts with
    upgrade-) is a side-checkout candidate, and every entry of
    <child>/.paperclip/worktrees and <child>/.claude/worktrees is a nested
    candidate. Unlistable directories are reported, never silently skipped.
    Returns (candidates, unlistable).
    """
    cands, unlistable = [], []
    try:
        companies = sorted(os.listdir(projects_root))
    except OSError:
        return [], [projects_root]
    for company in companies:
        company_dir = os.path.join(projects_root, company)
        if not os.path.isdir(company_dir) or os.path.islink(company_dir):
            continue
        try:
            projects = sorted(os.listdir(company_dir))
        except OSError:
            unlistable.append(company_dir)
            continue
        for project in projects:
            proj = os.path.join(company_dir, project)
            if not os.path.isdir(proj) or os.path.islink(proj):
                continue
            try:
                subs = sorted(os.listdir(proj))
            except OSError:
                unlistable.append(proj)
                continue
            for sub in subs:
                path = os.path.realpath(os.path.join(proj, sub))
                if not os.path.isdir(path):
                    continue
                if TOG.search(sub) or sub.startswith(UPGRADE_PREFIX):
                    cands.append(path)
                for nest in (".paperclip/worktrees", ".claude/worktrees"):
                    nest_dir = os.path.join(path, nest)
                    try:
                        names = sorted(os.listdir(nest_dir))
                    except OSError:
                        continue
                    for name in names:
                        entry = os.path.realpath(
                            os.path.join(nest_dir, name))
                        if os.path.isdir(entry):
                            cands.append(entry)
    return sorted(set(cands)), sorted(set(unlistable))


def recent_activity(candidate, idle_hours, now=None):
    """True (busy) when anything within maxdepth 3 changed inside idle_hours.

    Host parity: only node_modules subtrees are excluded from the clock
    (the node_modules dir entry itself still counts); .git internals count.
    An unreadable file/dir entry reads as unverifiable (None -> keep); an
    unlistable subtree is skipped the way the host find skips it.
    """
    if idle_hours <= 0:
        return False
    now = time.time() if now is None else now
    cutoff = now - idle_hours * 3600.0
    try:
        for dirpath, dirnames, filenames in os.walk(candidate):
            depth = 0 if dirpath == candidate else os.path.relpath(
                dirpath, candidate).count(os.sep) + 1
            if depth > IDLE_FIND_MAXDEPTH:
                dirnames[:] = []
                continue
            if IDLE_FIND_EXCLUDE in dirnames:
                # The dir entry itself counts (host parity); only the
                # subtree below it is excluded from the clock.
                try:
                    entry = os.path.join(dirpath, IDLE_FIND_EXCLUDE)
                    if os.lstat(entry).st_mtime > cutoff:
                        return True
                except OSError:
                    return None
                dirnames.remove(IDLE_FIND_EXCLUDE)
            for name in filenames:
                try:
                    if os.lstat(os.path.join(dirpath, name)).st_mtime > cutoff:
                        return True
                except OSError:
                    return None
            try:
                if os.lstat(dirpath).st_mtime > cutoff:
                    return True
            except OSError:
                return None
    except OSError:
        return None
    return False


def resolve_git_dir(candidate):
    """The gitdir for git proof, or None when it cannot be proven.

    Plain repos use <candidate>/.git directly. Linked worktrees carry a
    `.git` POINTER file whose gitdir is a container path (/paperclip/...) on
    hosts that realize worktrees inside the server container: map that
    prefix to the host data path and require the result to exist. A missing
    or unresolvable gitdir is unverifiable, never clean.
    """
    dotgit = os.path.join(candidate, ".git")
    if os.path.islink(dotgit):
        return None
    if os.path.isdir(dotgit):
        return os.path.realpath(dotgit)
    try:
        with open(dotgit, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    match = re.match(r"gitdir:\s*(\S+)", text)
    if not match:
        return None
    gitdir = to_host(match.group(1))
    if not os.path.isdir(gitdir):
        return None
    return os.path.realpath(gitdir)


def find_git_dir(start, stop):
    """Nearest gitdir at or above `start`, not above `stop`; None if unproven."""
    current = os.path.realpath(start)
    limit = os.path.realpath(stop)
    while True:
        dotgit = os.path.join(current, ".git")
        if os.path.islink(dotgit):
            return None
        if os.path.isdir(dotgit):
            return os.path.realpath(dotgit)
        if os.path.isfile(dotgit):
            return resolve_git_dir(current)
        if current == limit or current == os.path.dirname(current):
            return None
        current = os.path.dirname(current)


def git_proof(gitdir, work_tree, rel):
    """Git proof for one regen dir: (ignored, tracked_output, rc_ok).

    (True, "", True) only when `git check-ignore -q` accepts the path AND
    `git ls-files` lists nothing under it. Any git error fails closed
    upstream via rc_ok=False; a dubious-ownership refusal names itself.

    Both git calls name the candidate root explicitly (--work-tree with -C):
    with only --git-dir, git reads the invocation directory as the work
    tree, so a regen dir nested below the root was proven against the wrong
    root (a parent-local ignore stripped tracked dirs; a root-level ignore
    was missed and leaked).
    """
    argv_base = ["git", "-C", work_tree, "--git-dir", gitdir,
                 "--work-tree", work_tree]
    try:
        ign = subprocess.run(
            argv_base + ["check-ignore", "-q", "--", rel],
            capture_output=True, text=True, timeout=60)
        if ign.returncode == 1:
            return (False, "", True)
        if ign.returncode != 0:
            if "dubious ownership" in (ign.stderr or ""):
                return (False, "dubious-ownership", False)
            return (False, "", False)
        tracked = subprocess.run(
            argv_base + ["ls-files", "--", rel],
            capture_output=True, text=True, timeout=60)
        if tracked.returncode != 0:
            return (False, "", False)
        return (True, tracked.stdout, True)
    except (OSError, subprocess.TimeoutExpired):
        return (False, "", False)


def find_regen_dirs(candidate):
    """Regen dirs under the candidate without descending into them.

    Returns (found, skipped_symlink): symlinks are RECORDED, not followed
    (rm -rf on one would escape the candidate). Unlistable subtrees are
    skipped the way the host find skips them; anything verifiable still
    decides the candidate.

    The walk never descends into a nested checkout: any subdir carrying its
    own `.git` entry (a directory for plain repos and submodules, a FILE
    for linked worktrees) is a candidate in its own right, evaluated under
    its own card. Attributing its regen dirs to this parent would bypass
    the nested tree's liveness gate.
    """
    found, skipped = [], []
    prefix = candidate.rstrip("/") + "/"
    for dirpath, dirnames, _filenames in os.walk(candidate):
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if name == ".git":
                dirnames.remove(name)
            elif ((name not in REGEN_SET or not os.path.islink(full))
                    and os.path.lexists(os.path.join(full, ".git"))):
                dirnames.remove(name)
            elif name in REGEN_SET:
                dirnames.remove(name)
                if os.path.islink(full):
                    skipped.append(os.path.relpath(full, candidate))
                    continue
                real = os.path.realpath(full)
                if real != candidate and not real.startswith(prefix):
                    continue
                if os.path.isdir(real):
                    found.append(full)
            elif os.path.islink(full):
                dirnames.remove(name)
    return found, skipped


def dir_bytes(path):
    """Summed file bytes under path; a symlink counts 0, errors count 0."""
    if os.path.islink(path):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def host_proc_refs():
    """Absolute host paths referenced by live processes (cwd/exe/fd).

    None when /proc itself is unlistable: callers treat that as unverifiable
    and keep (host parity: "cannot read process cwds; fail closed").
    Permission-denied pids are skipped, not fatal: the tool runs as the
    project owner and can never see every pid; a writer a partial view
    misses is still caught by the idle clock.
    """
    refs = set()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        base = "/proc/%s" % pid
        for link in ("cwd", "exe"):
            try:
                refs.add(os.readlink(os.path.join(base, link)))
            except OSError:
                pass
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                refs.add(os.readlink(os.path.join(base, "fd", fd)))
            except OSError:
                pass
    return {to_host(p) for p in refs if p}


def build_context(api, now):
    """Control-plane state for one evaluation pass. Raises on API error."""
    try:
        issues = api.all_issues()
    except Exception as exc:
        raise PruneError("issues unreadable: %s" % exc) from exc
    by_ident = {row["identifier"]: row for row in issues.values()}
    try:
        refs = reclaim.refresh_live_refs(api)
    except Exception as exc:
        raise PruneError("live runs unreadable: %s" % exc) from exc
    try:
        heartbeat = api.heartbeat_runs()
    except Exception as exc:
        raise PruneError("heartbeat-runs unreadable: %s" % exc) from exc
    proc = host_proc_refs()
    if proc is None:
        raise PruneError("cannot read process cwds; fail closed")
    return {"by_ident": by_ident,
            "run_paths": {to_host(p) for p in refs["run_paths"]},
            "run_issue_ids": refs["run_issue_ids"],
            "proc_refs": proc, "heartbeat": heartbeat, "now": now}


def evaluate_candidate(ctx, candidate, stale_hours, idle_hours):
    """Every gate for one candidate. Never raises: doubt keeps (fail closed)."""
    res = {"w": candidate, "reason": None, "det": {}, "caches": []}
    try:
        return _evaluate_candidate(ctx, candidate, stale_hours, idle_hours,
                                   res)
    except Exception as exc:  # noqa: BLE001 — unverifiable is never stale
        res.update(reason="eval-error", caches=[],
                   det={"error": type(exc).__name__})
        return res


def _evaluate_candidate(ctx, candidate, stale_hours, idle_hours, res):
    basename = os.path.basename(candidate)
    card = None
    for ident in card_idents(basename):
        if ident in ctx["by_ident"]:
            card = ctx["by_ident"][ident]
            break
    hours = None
    if card is not None and card.get("status") not in TERMINAL_STATUSES:
        hours = card_hours_since_run(ctx["heartbeat"], card["id"],
                                     ctx["now"])
    stale, why = stale_gate(card.get("status") if card else None, hours,
                            stale_hours, card is not None, basename)
    if not stale:
        res["reason"] = why
        res["det"] = {"cards": [card["identifier"]] if card else []}
        return res
    res["det"] = {"cards": [card["identifier"]] if card else []}
    res["linked_ids"] = ({card["identifier"]: card["id"]} if card else {})
    if card is None:
        res["stale_basis"] = "cardless"
    elif card.get("status") in TERMINAL_STATUSES:
        res["stale_basis"] = "terminal"
    else:
        # Open card whose newest run is older than the bar: revalidate
        # cannot re-prove run recency and keeps every non-terminal card,
        # so summarize reports these as speculative, never as would-remove.
        res["stale_basis"] = "open-old-run"
    # PER-WORKTREE liveness: live run refs plus host process refs must point
    # at THIS candidate. There is no project-level flag: a busy sibling
    # checkout or an upload into the same project protects nothing here.
    # Cardless upgrade- checkouts consult the run-path set too: a run
    # starting under one between scan and removal must keep it.
    if ((card is not None and card["id"] in ctx["run_issue_ids"])
            or reclaim.path_referenced(candidate, ctx["run_paths"])):
        res["reason"] = "live-run-ref"
        return res
    if worktree_busy(candidate, ctx["proc_refs"]):
        res["reason"] = "live-process"
        return res
    busy = recent_activity(candidate, idle_hours)
    if busy is None:
        res["reason"] = "find-error"
        return res
    if busy:
        res["reason"] = "recent-activity"
        return res
    found, skipped = find_regen_dirs(candidate)
    if skipped:
        res["skipped_symlink"] = skipped
    for path in found:
        name = os.path.basename(path)
        if name != os.path.basename(path) or name not in REGEN_SET:
            continue
        parent = os.path.dirname(path)
        gitdir = find_git_dir(parent, candidate)
        if gitdir is None:
            res.setdefault("git-unreadable", []).append(
                os.path.relpath(path, candidate))
            continue
        rel = os.path.relpath(path, candidate)
        ignored, tracked, ok = git_proof(gitdir, candidate, rel)
        if not ok:
            res.setdefault("git-error", []).append(
                os.path.relpath(path, candidate))
            continue
        verdict, reason = reclaim.cache_dir_ok(
            is_symlink=False, check_ignore_rc=0 if ignored else 1,
            ls_files_rc=0, ls_files_out=tracked)
        if not verdict:
            res.setdefault(
                "tracked-inside" if reason == "tracked-inside"
                else "not-ignored", []).append(
                    os.path.relpath(path, candidate))
            continue
        res["caches"].append({"p": path, "name": name})
    return res


def revalidate(api, res):
    """Fresh proof immediately before removal. None = still removable.

    Re-reads every linked card (a reopen is a skip), re-checks live runs
    AND the run-path set (a run that started between scan and removal
    keeps the worktree — including cardless upgrade- checkouts, which
    have no card to reopen but do have paths), and re-scans host
    processes. Any error keeps.
    """
    linked = res.get("linked_ids") or {}
    try:
        refs = reclaim.refresh_live_refs(api)
    except Exception:  # noqa: BLE001 — unverifiable is never removable
        return "live-runs-unreadable"
    live_ids = refs.get("run_issue_ids") or set()
    run_paths = {to_host(p) for p in refs.get("run_paths") or set()}
    for _ident, issue_id in sorted(linked.items()):
        try:
            status = api.issue_status(issue_id)
        except Exception:  # noqa: BLE001
            return "card-reread-failed"
        if status not in TERMINAL_STATUSES:
            # Only terminal cards survive revalidation: an open card's run
            # history cannot be re-proven cheaply here, and a reopen between
            # scan and removal must keep the worktree.
            return "card-reopened"
        if issue_id in live_ids:
            return "live-run-appeared"
    if reclaim.path_referenced(res["w"], run_paths):
        return "run-path-appeared"
    try:
        fresh = host_proc_refs()
    except Exception:  # noqa: BLE001
        return "proc-unreadable"
    if fresh is None:
        return "proc-unreadable"
    if worktree_busy(res["w"], fresh):
        return "became-busy"
    return None


def summarize(results, unlistable, stale_hours, idle_hours, killed,
              apply_report, max_remove):
    """Report buckets for evaluated candidates.

    The headline regen_* totals count only candidates apply can actually
    free (terminal cards and cardless upgrade- checkouts: revalidate keeps
    every non-terminal card as card-reopened). Open cards whose newest run
    is older than the bar are reported separately as speculative_*: they
    were stale at scan time but will be skipped at apply time. Raising the
    removal cap is decided on the headline, never on the speculative line.
    """
    excluded = collections.Counter(r["reason"] for r in results if r["reason"])
    eligible = [r for r in results
                if r["reason"] is None and r.get("caches")]
    firm = [r for r in eligible
            if r.get("stale_basis") in ("terminal", "cardless")]
    speculative = [r for r in eligible
                   if r.get("stale_basis") not in ("terminal", "cardless")]
    by_name = collections.Counter()
    for res in firm:
        for cand in res["caches"]:
            by_name[cand["name"]] += cand.get("bytes", 0)
    total = sum(c.get("bytes", 0) for r in firm for c in r["caches"])
    spec_total = sum(c.get("bytes", 0) for r in speculative
                     for c in r["caches"])
    return {
        "candidates": len(results),
        "unlistable_dirs": unlistable,
        "stale_hours": stale_hours,
        "idle_hours": idle_hours,
        "max_remove": max_remove,
        "kill_switch": killed,
        "stale_candidates": sum(1 for r in results if r["reason"] is None),
        "regen_dirs": sum(len(r["caches"]) for r in firm),
        "regen_bytes": total,
        "regen_gib": round(total / 2 ** 30, 3),
        "regen_bytes_by_dirname": dict(by_name),
        "speculative_dirs": sum(len(r["caches"]) for r in speculative),
        "speculative_bytes": spec_total,
        "speculative_gib": round(spec_total / 2 ** 30, 3),
        "kept_by_reason": dict(excluded),
        "sample": [{"w": r["w"],
                    "gib": round(sum(c.get("bytes", 0)
                                     for c in r["caches"]) / 2 ** 30, 3),
                    "cards": r["det"].get("cards")}
                   for r in sorted(
                       firm,
                       key=lambda r: (-sum(c.get("bytes", 0)
                                           for c in r["caches"]),
                                      r["w"]))[:SAMPLE_SIZE]],
        "apply": apply_report,
    }


def write_manifest(manifest_dir, payload, stamp):
    """Dated manifest of removed/would-remove paths. Returns its path.

    Atomic via ops/lib/atomic_state (tmp + fsync + os.replace): under ENOSPC
    or an interrupted write the previous manifest bytes stay in place instead
    of a truncation reading as data. OSError propagates to run_tool, which
    fails closed.
    """
    os.makedirs(manifest_dir, exist_ok=True)
    path = os.path.join(manifest_dir, "project-regen-%s.json" % stamp)
    write_json_atomic(path, payload)
    return path


def apply_removals(api, evaluated, max_remove):
    """Remove regen dirs, re-proving every gate right before each rm."""
    removed, errors = [], []
    skipped = collections.Counter()
    ordered = [(res, cand) for res in evaluated
               for cand in res.get("caches", [])]
    ordered.sort(key=lambda pair: (-pair[1].get("bytes", 0), pair[1]["p"]))
    for res, cand in ordered:
        if len(removed) >= max_remove:
            skipped["over-max-remove"] += 1
            continue
        why = revalidate(api, res)
        if why is not None:
            skipped[why] += 1
            continue
        path, name = cand["p"], cand["name"]
        if name != os.path.basename(path) or name not in REGEN_SET:
            skipped["not-a-regen-name"] += 1
            continue
        try:
            if os.path.islink(path) or not os.path.isdir(path):
                skipped["gone-or-link"] += 1
                continue
            if not os.path.realpath(path).startswith(
                    res["w"].rstrip("/") + "/"):
                skipped["escapes-worktree"] += 1
                continue
            parent = os.path.dirname(path)
            gitdir = find_git_dir(parent, res["w"])
            if gitdir is None:
                skipped["git-unreadable"] += 1
                continue
            ignored, tracked, ok = git_proof(
                gitdir, res["w"], os.path.relpath(path, res["w"]))
            verdict, _reason = reclaim.cache_dir_ok(
                is_symlink=False, check_ignore_rc=0 if ignored else 1,
                ls_files_rc=0, ls_files_out=tracked)
            if not ok or not verdict:
                skipped["became-tracked"] += 1
                continue
        except Exception:  # noqa: BLE001 — doubt keeps
            skipped["recheck-error"] += 1
            continue
        try:
            proc = subprocess.run(["rm", "-rf", "--", path], timeout=600)
            gone = not os.path.exists(path)
            rc = proc.returncode
        except (OSError, subprocess.TimeoutExpired):
            rc, gone = 1, os.path.exists(path)
        if rc == 0 and gone:
            removed.append({"p": path, "bytes": cand.get("bytes", 0)})
        else:
            errors.append({"p": path, "rc": rc})
    freed = sum(r["bytes"] for r in removed)
    return {"removed_dirs": len(removed),
            "removed_bytes": freed,
            "removed_gib": round(freed / 2 ** 30, 3),
            "skipped_by_reason": dict(skipped),
            "errors": errors[:20],
            "n_errors": len(errors)}


def run_tool(opts, api, env, clock=time.time):
    """One full pass: fresh evaluation, sizing, report; delete only on apply."""
    started = clock()
    killed, why = kill_switch(env, opts.manifest_dir)
    apply_requested = opts.apply and not killed
    if killed and opts.apply:
        print("KILL-SWITCH (%s): apply disabled, dry-run only" % why,
              file=sys.stderr)
    ctx = build_context(api, clock())
    cands, unlistable = enumerate_candidates(opts.projects_root)
    print("project candidates on disk: %d" % len(cands), file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=opts.workers) as pool:
        results = list(pool.map(
            lambda c: evaluate_candidate(ctx, c, opts.stale_hours,
                                         opts.idle_hours), cands))
    for res in [r for r in results if r["reason"] is None]:
        for cand in res["caches"]:
            cand["bytes"] = dir_bytes(cand["p"])
    apply_report = None
    if apply_requested:
        apply_report = apply_removals(
            api,
            [r for r in results if r["reason"] is None and r.get("caches")],
            opts.max_remove)
    summary = summarize(results, unlistable, opts.stale_hours,
                        opts.idle_hours, why if killed else None,
                        apply_report, opts.max_remove)
    summary["took_s"] = round(clock() - started)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started))
    manifest = {"stamp": stamp,
                "mode": "apply" if apply_requested else "dry-run",
                "summary": summary,
                "results": results}
    manifest_path = os.path.join(opts.manifest_dir,
                                   "project-regen-%s.json" % stamp)
    try:
        summary["manifest"] = write_manifest(opts.manifest_dir, manifest,
                                             stamp)
    except OSError as exc:
        # Fail closed but loud: the summary is already printed so the pass
        # leaves evidence even though no manifest landed (exit 2 via
        # PruneError in main).
        print(json.dumps(summary, indent=1), flush=True)
        raise PruneError("%s: errno=%s" % (manifest_path,
                                           exc.errno)) from exc
    line = ("%s project-regen-prune: %s %d dirs (~%.2fG) from %d/%d "
            "stale candidates; manifest %s"
            % (stamp,
               "removed" if apply_requested else "would remove",
               (apply_report["removed_dirs"] if apply_report
                else summary["regen_dirs"]),
               ((apply_report["removed_bytes"] if apply_report
                 else summary["regen_bytes"]) / 2 ** 30),
               summary["stale_candidates"], summary["candidates"],
               summary["manifest"]))
    summary["line"] = line
    print(line)
    print(json.dumps(summary, indent=1))
    return summary


def main(argv=None, api=None, env=None):
    """Entry point. Raises SafetyRefusal / PruneError instead of exiting."""
    opts = parse_argv(argv if argv is not None else sys.argv[1:])
    env = os.environ if env is None else env
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    if api is None:
        base = env.get("PAPERCLIP_API_URL")
        company = env.get("PAPERCLIP_COMPANY_ID")
        key = env.get("PAPERCLIP_API_KEY")
        if opts.apply and not (base and company and key):
            raise SafetyRefusal(
                "refusing --apply without PAPERCLIP_API_URL/COMPANY_ID/KEY: "
                "the per-removal card re-read would be impossible")
        if not (base and company and key):
            raise PruneError("API env is required even for dry-run (the "
                             "card join reads the live board)")
        if opts.apply and not _valid_api_base(base.rstrip("/")):
            raise SafetyRefusal(
                "refusing --apply with a PAPERCLIP_API_URL that is not "
                "a clean http(s) base: the bearer would misdirect")
        if not _valid_api_base(base.rstrip("/")):
            raise PruneError(
                "refusing: PAPERCLIP_API_URL is not a clean http(s) base")
        # Quiet pre-check: ApiClient below owns the log + counter, so a
        # shadow mismatch warns once per send, not twice.
        try:
            check_api_base_allowed(base.rstrip("/"),
                                   sender="project_worktree_regen_prune",
                                   quiet=True)
        except AllowlistRefused as exc:
            if opts.apply:
                raise SafetyRefusal(str(exc)) from exc
            raise PruneError(str(exc)) from exc
        api = reclaim.ApiClient(base, company, key)
    try:
        summary = run_tool(opts, api, env)
    except SafetyRefusal:
        raise
    except PruneError:
        raise
    except Exception as exc:  # API error during the fresh evaluation etc.
        raise PruneError("%s: %s" % (type(exc).__name__, exc)) from exc
    applied = summary.get("apply")
    if opts.apply and applied and applied["n_errors"]:
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SafetyRefusal as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        sys.exit(1)
    except PruneError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(2)
