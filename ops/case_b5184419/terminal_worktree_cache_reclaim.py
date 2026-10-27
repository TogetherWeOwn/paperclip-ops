#!/usr/bin/env python3
"""terminal_worktree_cache_reclaim.py — reclaim regenerable dep caches in terminal-card worktrees. (case_f2c6fc48, parent case_b5184419.)

WHY THIS SCRIPT EXISTS.

The retention sweep (ops/case_5866fa6f/disk-retention-sweep.sh) only walks
`instances/default/workspaces/*`. Per-issue worktrees
(`instances/default/worktrees/*/*` and `projects/*/*/.paperclip/worktrees/*`)
are not swept, and they hold the bulk of the git-ignored dep caches: 1,741
terminal-card worktrees held 86.9 GiB at 01:45Z on 2026-10-03. Two manual
passes (case_879c1a87 + the case_b5184419 second pass) took /home from 91% to 82%
(-85 GB). At ~12 GB/day of churn that returns to 90% in about 5 days unless
the pass recurs. This tool is the recurring form of the second-pass script.

WHY EACH SAFETY PROPERTY IS STRUCTURAL, NOT PROSE.

- Terminal-card join, not age. A worktree is eligible only when EVERY linked
  card (DB execution-workspace rows plus a TOG-NNNN basename match) is
  done/cancelled. Open card, unknown card, no DB row, or non-git dir = keep.
  Age is irrelevant: a done card's cache is regenerable the day it lands.
- Live references fail closed. Newest heartbeat runs (plus the authoritative
  /live-runs issue ids), queued runs, scheduled retries, and a /proc scan of
  this container must all be clear of the worktree, and nothing outside dep
  caches may be modified in the last 30 minutes.
- Cargo `target/` is never reclaimed here (case_160a06b2: the root-host audit and
  an approved exact-path list come first). It is found, sized and reported as
  `cargo_target_excluded`; `target` is not in DEP_CACHE_NAMES and the delete
  site refuses any name outside that set.
- Only ignored, untracked dep caches. Each candidate dir must be reported
  ignored by `git check-ignore` AND contain zero tracked files (`git ls-files`
  empty); symlinks are skipped; deletion is containment-checked
  (realpath stays under the worktree).
- Pushed-HEAD proof (case_b5184419 lesson). `git log HEAD --not --remotes`
  counts a commit as unpushed when its branch ref was auto-deleted after the
  PR merged, even though origin still advertises it under refs/pull/N/head.
  So a commit is treated as pushed only if it is an ancestor of a sha origin
  advertises under `refs/pull/*/head` or `refs/heads/*`. Any git/ls-remote
  error fails closed (keep). PINNED BUG (first dry run): `git rev-list HEAD
  --not --remotes --stdin` inverts the stdin operands (negative counts). The
  tool uses `git rev-list HEAD --stdin` with `^sha` lines and intersects the
  output with the unpushed set. `rev_list_argv()` pins the argv; the suite
  kills the `--not` form.
- Branch-scoped stash guard. A stash naming the worktree's branch keeps it;
  on a detached HEAD any stash keeps it (fail closed).
- Dry-run is the default. NOTHING is deleted without `--apply`, and `--apply`
  re-runs the FULL evaluation fresh in the same process (it never consumes a
  stale dryrun.json) and re-reads every linked card's status from the API
  immediately before each delete. A card reopened between scan and delete is
  skipped; a re-read that errors is skipped, never assumed terminal.
- Every other gate is re-proven right before the delete too (case_cfed961c). The
  evaluation of ~3,000 worktrees precedes the first rm and can be hours old, so
  per worktree apply re-checks live runs/processes (fresh API read + /proc),
  recent activity, the pushed-HEAD proof and the branch stash, and per
  directory the name, symlink, containment, ignored and untracked gates. Any
  error while re-checking skips; the report counts skips by reason.

MODES.

  python3 ops/case_b5184419/terminal_worktree_cache_reclaim.py
      Dry-run (default): scan, print the summary JSON (n_errors counts
      evaluation errors), write dryrun.json. Deletes nothing. Needs read API
      access + git.

  python3 ops/case_b5184419/terminal_worktree_cache_reclaim.py --apply
      Re-evaluate everything fresh, re-read each card per worktree right
      before deleting, remove eligible dep-cache dirs, print + write
      apply.json. Needs the same access; refuses without API env (a blind
      apply with no live re-read is worse than no apply).

  python3 ops/case_b5184419/terminal_worktree_cache_reclaim.py --registry-gate ID
      Recurring-routine mode: reads the reviewed WS7 registry and applies
      only when entry ID is an active writer of fs:worktree-dep-caches. The
      default registry is the sibling ops/case_30b51a0f/host-timer-registry.json
      next to this tool; in distributions where that private reviewed file
      does not ship, pass its installed location explicitly via --registry.
      A detector/paused/planned/retired entry is an intentional dry-run. A
      missing/duplicate entry or an unreadable/malformed registry is also a
      dry-run, but it is a gate ERROR: the summary records mode, mode_reason,
      registry_gate and registry_gate_error, and n_errors counts it. Cannot
      be combined with --apply. The first stderr line records the chosen mode.

Exit codes: 0 success (dry-run report / apply complete, even when the gate
refused apply — check mode_reason and n_errors), 1 safety refusal or abort
(blind apply, API unreachable during fresh evaluation — LOUD on stderr),
2 usage error.

No secrets in output: stdout carries only worktree paths, card identifiers,
statuses, counts and byte totals. Env credentials are read, never printed.

Rollback: revert the commit. Deleted caches are regenerable by the package
manager (`npm ci`, `cargo fetch`, `pip install`, ...); no worktree itself is
ever deleted.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "lib"))
from paperclip_api_allowlist import (
    AllowlistRefused, check_api_base_allowed)

DEP_CACHE_NAMES = frozenset({
    "node_modules", "vendor", ".venv", "venv", "__pycache__",
    ".next", ".turbo", ".parcel-cache",
})
# case_160a06b2 (CTO condition 2): per-worktree Cargo `target/` is NOT regenerable
# reclaim. Pruning it needs the root-host audit under an admission hold and an
# approved exact-path list, so it never joins the aggregate. It is still found
# (the scan must not descend into it) and sized, reported as
# `cargo_target_excluded`, and refused by name at the delete site.
CARGO_TARGET_NAMES = frozenset({"target"})
SCAN_PRUNE_NAMES = DEP_CACHE_NAMES | CARGO_TARGET_NAMES
TERMINAL_STATUSES = frozenset({"done", "cancelled"})
REGISTRY_WRITE_TARGET = "fs:worktree-dep-caches"
REGISTRY_DEFAULT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "case_30b51a0f",
    "host-timer-registry.json"))
RECENT_MINUTES_DEFAULT = 30
EVAL_WORKERS_DEFAULT = 6
# Execution-workspace rows in any other state are not understood: keep.
LIVE_WORKSPACE_STATES = frozenset({"active", "archived", "cleanup_failed"})
DONE_RUN_STATUSES = frozenset({"failed", "succeeded", "cancelled", "timed_out"})
# Skips where the tool could not check, as opposed to a deliberate refusal.
TOOL_ERROR_REASONS = frozenset({"card-reread-error", "recheck-error",
                                "git-error", "ls-files-error",
                                "check-ignore-error", "find-error"})
EVAL_ERROR_REASONS = frozenset({"git-error", "find-error", "eval-error"})


class ReclaimError(RuntimeError):
    """Loud, fatal, non-safety failure: API error, usage error, I/O error."""


class RemoteUnreadable(ReclaimError):
    """The origin cannot be read: kept as a refusal, not counted as an error."""


class SafetyRefusal(RuntimeError):
    """Loud safety refusal: blind apply, fresh evaluation impossible."""


# ---------------------------------------------------------------------------
# PURE CORE. No network, no clock reads, no environment — the offline suite
# drives all of this through fixtures and throwaway local git repos, and every
# branch below has a test that kills its mutant (e.g. an `any`-vs-`all` slip
# in the terminal join, or the `--not --stdin` inversion in rev-list).
# ---------------------------------------------------------------------------

def terminal_gate(linked_cards):
    """Every linked card must be done/cancelled.

    linked_cards: iterable of mappings with `identifier` and `status`
    (status None = the card id was referenced but the board has no such row).
    Returns None when deletion may proceed, else (reason, detail).
    """
    linked_cards = list(linked_cards or [])
    if not linked_cards:
        return ("no-linked-card", {})
    for card in linked_cards:
        status = card.get("status")
        if status is None:
            return ("card-unknown", {"card": card.get("identifier")})
        if status not in TERMINAL_STATUSES:
            return ("open-card", {"card": card.get("identifier"),
                                  "status": status})
    return None


def path_referenced(path, pathset):
    """True when `path` itself or anything beneath it appears in `pathset`.

    The trailing-slash join is the load-bearing part: a naive startswith
    matches the sibling /w/case_de259302 from a reference to /w/case_8c5fe4cc.
    """
    prefix = path.rstrip("/") + "/"
    return any(p == path or p.startswith(prefix) for p in pathset)


def rev_list_argv():
    """PINNED (case_b5184419): prove pushed-ancestry with exclusions on stdin.

    `git rev-list HEAD --not --remotes --stdin` INVERTS the stdin operands
    (the first dry run produced negative counts). The fixed form takes `^sha`
    exclusion lines on stdin with no `--not`/`--remotes` flags. This function
    is the single place the argv is built so the suite can pin it.
    """
    return ["git", "rev-list", "HEAD", "--stdin"]


def rev_list_stdin(remote_shas):
    """Exclusion lines for rev-list stdin: one `^sha` per advertised remote sha."""
    return "".join("^%s\n" % sha for sha in sorted(remote_shas))


def subtract_proven_pushed(unpushed_shas, rev_list_output_shas):
    """Commits still unpushed = unpushed ∩ rev-list(HEAD, ^remote...).

    rev-list prints the commits reachable from HEAD after exclusions, so the
    remainder is the INTERSECTION with the unpushed set — not the difference
    (which would bless everything rev-list never saw) and not the raw output
    (which would condemn proven commits the unpushed listing missed).
    """
    return set(unpushed_shas) & set(rev_list_output_shas)


def branch_stash_hits(stash_lines, branch):
    """Stash entries that protect this worktree's branch.

    Counts entries whose message names `branch` ("WIP on <branch>:" / "On
    <branch>:"). On a detached HEAD (branch falsy) ANY stash counts — fail
    closed, because there is no branch name to scope the check to.
    """
    hits = 0
    for line in stash_lines or []:
        message = line.split(": ", 1)[1] if ": " in line else line
        if not branch or re.match(r"(WIP on|On) " + re.escape(branch) + r":",
                                  message):
            hits += 1
    return hits


def cache_dir_ok(*, is_symlink, check_ignore_rc, ls_files_rc, ls_files_out):
    """A dep-cache dir may go only if ignored AND untracked AND not a link.

    Returns (True, None) or (False, reason). An ls-files error fails closed:
    an unverifiable dir is "unknown", never "untracked".
    """
    if is_symlink:
        return (False, "symlink")
    if check_ignore_rc == 1:
        return (False, "not-ignored")
    if check_ignore_rc != 0:
        return (False, "check-ignore-error")
    if ls_files_rc != 0:
        return (False, "ls-files-error")
    if (ls_files_out or "").strip():
        return (False, "tracked-inside")
    return (True, None)


def registry_apply_decision_ex(entries, entry_id):
    """(apply, why, gate_error) for a recurring firing, from the WS7 registry.

    Apply needs ALL of: the entry exists exactly once, its state is `active`,
    its role is `writer`, and it declares the one write target this tool
    touches. A detector/paused/planned/retired entry or a writer for another
    target is an intentional dry-run (gate_error False). A missing/duplicate
    entry, a malformed entries list, or a present-but-non-list `writes`
    shape is a gate ERROR (gate_error True): the firing still dry-runs,
    but the summary counts it. A missing `writes` field declares nothing,
    like an empty list (intentional dry-run). Flipping a routine from
    reporting to deleting is a registry change through exact-head review and
    CI, not a routine-text edit.
    """
    if not isinstance(entries, list):
        return (False, "registry has no entries list", True)
    matches = [e for e in entries
               if isinstance(e, dict) and e.get("id") == entry_id]
    if len(matches) != 1:
        return (False, "registry entry %r found %d times" % (entry_id,
                                                             len(matches)),
                True)
    entry = matches[0]
    if entry.get("state") != "active":
        return (False, "entry state is %r, not active" % entry.get("state"),
                False)
    if entry.get("role") != "writer":
        return (False, "entry role is %r, not writer" % entry.get("role"),
                False)
    writes = entry.get("writes")
    if writes is None:
        writes = []
    if not isinstance(writes, list):
        return (False, "entry writes is not a list", True)
    if REGISTRY_WRITE_TARGET not in writes:
        return (False, "entry does not declare %s" % REGISTRY_WRITE_TARGET,
                False)
    return (True, "registry entry %r is an active writer" % entry_id, False)


def registry_apply_decision(entries, entry_id):
    """(apply, why) for a recurring firing, from the reviewed WS7 registry.

    Backward-compatible wrapper: see registry_apply_decision_ex for the
    gate-error classification. Apply needs ALL of: the entry exists, its
    state is `active`, its role is `writer`, and it declares the one write
    target this tool touches.
    """
    apply, why, _ = registry_apply_decision_ex(entries, entry_id)
    return (apply, why)


def resolve_mode_ex(opts):
    """(apply, why, gate_error). Fail-closed: an unreadable registry is dry-run.

    gate_error is True only when --registry-gate was requested and the
    registry itself could not be used (unreadable file, malformed JSON, or a
    missing/duplicate entry). An intentional dry-run (no gate, --apply, or a
    detector/paused entry) reports gate_error False.
    """
    if not opts.registry_gate:
        return (opts.apply, "--apply" if opts.apply else "default", False)
    try:
        with open(opts.registry, encoding="utf-8") as handle:
            entries = json.load(handle).get("entries")
    except (OSError, ValueError, AttributeError) as exc:
        return (False, "registry unreadable: %s" % exc, True)
    return registry_apply_decision_ex(entries, opts.registry_gate)


def resolve_mode(opts):
    """(apply, why). Fail-closed: a registry that cannot be read is dry-run."""
    apply, why, _ = resolve_mode_ex(opts)
    return (apply, why)


def parse_argv(argv):
    """Argv parsing, isolated so the suite can pin dry-run-by-default."""
    parser = argparse.ArgumentParser(
        description="Reclaim regenerable dep caches in terminal-card "
                    "worktrees. Dry-run unless --apply.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="Actually delete (re-evaluates everything fresh).")
    mode.add_argument("--registry-gate", metavar="ENTRY_ID", default=None,
                      help="Recurring-routine mode: apply only when the WS7 "
                           "registry entry ENTRY_ID is an active writer of "
                           "%s; otherwise dry-run (a registry ERROR still "
                           "dry-runs but counts in n_errors)."
                           % REGISTRY_WRITE_TARGET)
    parser.add_argument("--registry", default=REGISTRY_DEFAULT,
                        help="WS7 registry JSON for --registry-gate "
                             "(default: sibling ops/case_30b51a0f/ "
                             "host-timer-registry.json; in distributions "
                             "where that private file does not ship, pass "
                             "its reviewed installed location explicitly).")
    parser.add_argument("--root", default="/paperclip/instances/default",
                        help="Paperclip home/container view to scan.")
    parser.add_argument("--recent-minutes", type=int,
                        default=RECENT_MINUTES_DEFAULT)
    parser.add_argument("--workers", type=int, default=EVAL_WORKERS_DEFAULT)
    parser.add_argument("--out-dir", default=None,
                        help="Where to write dryrun.json/apply.json "
                             "(default: $PAPERCLIP_SCRATCH_DIR).")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# LIVE SHELL. Git/API/process/filesystem access. Every git-touching helper
# takes `run` (a (args, cwd, timeout, stdin) -> (rc, stdout) callable) so the
# suite can drive the decision logic against fakes and throwaway local repos.
# ---------------------------------------------------------------------------

def live_run(args, cwd=None, timeout=300, stdin_text=None):
    """Run a subprocess, capturing stdout. Never raises on nonzero exit."""
    proc = subprocess.run(args, cwd=cwd, input=stdin_text,
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout


def fetch_remote_shas(run, worktree):
    """SHAs origin advertises under refs/pull/*/head + refs/heads/* that exist
    locally. Raises RemoteUnreadable when origin cannot be read, and
    ReclaimError when a local git call fails."""
    rc, out = run(["git", "rev-parse", "--git-common-dir"], cwd=worktree)
    if rc != 0:
        raise ReclaimError("rev-parse failed in %s" % worktree)
    try:
        rc, out = run(["git", "ls-remote", "origin",
                       "refs/pull/*/head", "refs/heads/*"],
                      cwd=worktree, timeout=120)
    except subprocess.TimeoutExpired:
        raise RemoteUnreadable("ls-remote timed out in %s" % worktree)
    if rc != 0:
        raise RemoteUnreadable("ls-remote failed in %s" % worktree)
    advertised = sorted({line.split()[0] for line in out.splitlines()
                         if line.strip()})
    if not advertised:
        return set()
    rc, out = run(["git", "cat-file",
                   "--batch-check=%(objectname) %(objecttype)"],
                  cwd=worktree, stdin_text="\n".join(advertised) + "\n")
    if rc != 0:
        raise ReclaimError("cat-file failed in %s" % worktree)
    return {parts[0] for line in out.splitlines()
            for parts in [line.split()]
            if len(parts) == 2 and parts[1] == "commit"}


def unpushed_after_proof(run, worktree):
    """(remaining_unpushed, proven_pushed) for this worktree's HEAD.

    Raises ReclaimError when HEAD or the remote proof cannot be read. An
    empty advertised set subtracts nothing: every unpushed commit counts.
    """
    rc, out = run(["git", "log", "HEAD", "--not", "--remotes",
                   "--format=%H"], cwd=worktree)
    if rc != 0:
        raise ReclaimError("git log HEAD failed in %s" % worktree)
    unpushed = {line.split()[0] for line in out.splitlines() if line.strip()}
    if not unpushed:
        return (0, 0)
    shas = fetch_remote_shas(run, worktree)
    if not shas:
        return (len(unpushed), 0)
    rc, out = run(rev_list_argv(), cwd=worktree,
                  stdin_text=rev_list_stdin(shas))
    if rc != 0:
        raise ReclaimError("rev-list failed in %s" % worktree)
    remaining = subtract_proven_pushed(
        unpushed, {line.strip() for line in out.splitlines() if line.strip()})
    return (len(remaining), len(unpushed) - len(remaining))


def find_cache_dirs(run, worktree):
    """Candidate dep-cache dirs under the worktree (never descends into them).

    Matches symlinks too (`-type l`): a symlinked node_modules must be
    RECORDED as skipped, not silently absent — `rm -rf` on it would follow
    the link out of the worktree.
    """
    args = ["find", worktree, "-name", ".git", "-prune", "-o", "(",
            "-type", "d", "-o", "-type", "l", ")", "("]
    first = True
    for name in sorted(SCAN_PRUNE_NAMES):
        if not first:
            args.append("-o")
        args += ["-name", name]
        first = False
    args += [")", "-print", "-prune"]
    rc, out = run(args, cwd=worktree, timeout=900)
    if rc != 0:
        raise ReclaimError("find failed under %s" % worktree)
    prefix = worktree.rstrip("/") + "/"
    kept = []
    for line in out.splitlines():
        if not line:
            continue
        # Symlinks are kept WITHOUT the realpath containment check: the
        # target usually escapes the worktree (that is why the link is
        # dangerous), and the islink skip downstream is what records them.
        # They are never deleted: evaluate_worktree skips links before rm.
        if os.path.islink(line) or os.path.realpath(line).startswith(prefix):
            kept.append(line)
    return kept


def recent_activity(run, worktree, minutes):
    """Any non-dep-cache, non-.git file modified within `minutes`?

    Raises ReclaimError when find errors: activity cannot be ruled out."""
    args = ["find", worktree, "(", "-name", ".git"]
    for name in sorted(SCAN_PRUNE_NAMES):
        args += ["-o", "-name", name]
    args += [")", "-prune", "-o", "-type", "f", "-mmin", "-%d" % minutes,
             "-print", "-quit"]
    rc, out = run(args, cwd=worktree, timeout=900)
    # An unreadable subtree is never "idle": the caller refuses the worktree.
    if rc != 0:
        raise ReclaimError("find failed under %s" % worktree)
    return bool(out.strip())


def nested_repo(run, path):
    # A nested or bare repo's unpushed commits are outside the HEAD proof.
    rc, out = run(["find", path, "(", "-name", ".git", "-o", "-name", "HEAD",
                   "-type", "f", ")", "-print"], timeout=900)
    if rc != 0:
        raise ReclaimError("find failed under %s" % path)
    for hit in out.splitlines():
        if os.path.basename(hit) == ".git":
            return True
        parent = os.path.dirname(hit)
        if all(os.path.isdir(os.path.join(parent, d))
               for d in ("objects", "refs")):
            return True
    return False


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


class ApiClient:
    """Minimal Paperclip control-plane reader (issues, workspaces, runs)."""

    def __init__(self, base_url, company_id, api_key,
                 allowlist_checked_base=None):
        base = (base_url or "").rstrip("/")
        if not _valid_api_base(base):
            raise ReclaimError(
                "refusing: PAPERCLIP_API_URL is not a clean http(s) base")
        # Container delegation (see run-worktree-prune.sh): the host wrapper
        # rendered the allowlist verdict on this exact base string, because
        # the container cannot read the operator file. Honor it only on exact
        # string equality with the base in use; unset, empty, or merely
        # similar values run the normal check and fail closed.
        if allowlist_checked_base is None or allowlist_checked_base != base_url:
            try:
                check_api_base_allowed(base, sender="terminal_worktree_cache_reclaim")
            except AllowlistRefused as exc:
                raise ReclaimError(str(exc)) from exc
        self.base = base + "/api/companies/" + company_id
        self.plain = base + "/api"
        self.headers = {"Authorization": "Bearer " + api_key}

    def _get(self, url, timeout=120):
        req = urllib.request.Request(url, headers=self.headers)
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    def paged_issues(self, status):
        """All issues of one status (the list caps at 1000: page it)."""
        seen, offset = {}, 0
        while True:
            rows = self._get("%s/issues?status=%s&limit=1000&offset=%d"
                             % (self.base, status, offset))
            if not rows:
                break
            for row in rows:
                seen[row["id"]] = row
            offset += 1000
            if len(rows) < 1000:
                break
        return seen

    def all_issues(self):
        issues = {}
        for status in ("backlog", "todo", "in_progress", "in_review",
                       "blocked", "done", "cancelled"):
            issues.update(self.paged_issues(status))
        return issues

    def execution_workspaces(self):
        return self._get(self.base + "/execution-workspaces")

    def heartbeat_runs(self):
        # The endpoint ignores status/offset filters: newest ~200 only.
        return self._get(self.base + "/heartbeat-runs?limit=200")

    def live_runs(self):
        # Authoritative for issue ids of active runs.
        return self._get(self.base + "/live-runs")

    def issue_status(self, issue_id):
        """Fresh single-card status for the pre-delete re-read."""
        return self._get("%s/issues/%s" % (self.plain, issue_id))["status"]


def scan_container_processes():
    """Paths referenced by live container processes (/proc cwd/exe/fd/maps)."""
    paths = set()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        base = "/proc/%s" % pid
        for link in ("cwd", "exe", "root"):
            try:
                paths.add(os.readlink(os.path.join(base, link)))
            except OSError:
                pass
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            fds = []
        for fd in fds:
            try:
                paths.add(os.readlink(os.path.join(base, "fd", fd)))
            except OSError:
                pass
        for name in ("cmdline", "environ"):
            try:
                with open(os.path.join(base, name), "rb") as handle:
                    data = handle.read().decode("utf8", "replace")
            except OSError:
                continue
            for match in re.findall(r"/paperclip/instances/default/[^\x00\s\"']+",
                                    data):
                paths.add(match)
        try:
            with open(os.path.join(base, "maps")) as handle:
                maps = handle.read()
        except OSError:
            continue
        for line in maps.splitlines():
            if "/paperclip/" in line:
                paths.add(line.split(None, 5)[-1].strip())
    return paths


def refresh_live_refs(api, run_paths_extra=()):
    """Everything that can still be using a worktree right now: live/queued/
    retrying runs (paths, issue ids, workspace ids) plus this container's
    processes. One function for the scan AND the pre-delete re-check, so the
    two can never drift apart."""
    run_paths, run_issue_ids, run_ws_ids = set(run_paths_extra), set(), set()
    for row in api.heartbeat_runs():
        if row.get("status") in DONE_RUN_STATUSES and not (
                row.get("scheduledRetryAt") and row.get("scheduledRetryAt") >
                time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())):
            continue
        blob = json.dumps(row.get("contextSnapshot") or {})
        run_paths.update(re.findall(r'/paperclip/[^"\\\s]+', blob))
        for key in ("issueId", "executionWorkspaceId", "projectWorkspaceId"):
            for match in re.findall(r'"%s":\s*"([0-9a-f-]{36})"' % key, blob):
                (run_issue_ids if key == "issueId"
                 else run_ws_ids).add(match)
    for row in api.live_runs():
        if row.get("issueId"):
            run_issue_ids.add(row["issueId"])
    return {"run_paths": run_paths, "run_issue_ids": run_issue_ids,
            "run_ws_ids": run_ws_ids, "proc_paths": scan_container_processes()}


def build_context(api, run_paths_extra=()):
    """Fetch control-plane state once per evaluation pass."""
    ew = api.execution_workspaces()
    issues = api.all_issues()
    by_ident = {row["identifier"]: row for row in issues.values()}
    ew_by_cwd = collections.defaultdict(list)
    for row in ew:
        for key in (row.get("cwd"), row.get("providerRef")):
            if key:
                ew_by_cwd[os.path.realpath(key)].append(row)
    issues_by_ew = collections.defaultdict(list)
    for row in issues.values():
        if row.get("executionWorkspaceId"):
            issues_by_ew[row["executionWorkspaceId"]].append(row)
    refs = refresh_live_refs(api, run_paths_extra)
    return {"ew_by_cwd": ew_by_cwd, "issues": issues, "by_ident": by_ident,
            "issues_by_ew": issues_by_ew, "run_paths": refs["run_paths"],
            "run_issue_ids": refs["run_issue_ids"],
            "run_ws_ids": refs["run_ws_ids"],
            "proc_paths": refs["proc_paths"]}


def evaluate_worktree(run, ctx, worktree, minutes):
    """Every gate for one worktree. An unexpected failure is an eval-error."""
    res = {"w": worktree, "reason": None, "det": {}, "caches": [],
           "git": None}
    try:
        return _evaluate_worktree(run, ctx, worktree, minutes, res)
    except Exception as exc:
        res.update(reason="eval-error", caches=[],
                   det={"error": type(exc).__name__})
        return res


def _evaluate_worktree(run, ctx, worktree, minutes, res):
    """Apply every gate to one worktree, filling res in place."""
    if not os.path.exists(os.path.join(worktree, ".git")):
        res["reason"] = "not-a-git-worktree"
        return res
    ews = ctx["ew_by_cwd"].get(worktree, [])
    if not ews:
        res["reason"] = "no-db-workspace"
        return res
    linked = {}
    for row in ews:
        if row.get("sourceIssueId"):
            linked[row["sourceIssueId"]] = ctx["issues"].get(
                row["sourceIssueId"])
        for item in ctx["issues_by_ew"].get(row["id"], []):
            linked[item["id"]] = item
    match = re.match(r"(TOG-\d+)", os.path.basename(worktree))
    if match and match.group(1) in ctx["by_ident"]:
        item = ctx["by_ident"][match.group(1)]
        linked[item["id"]] = item
    gate = terminal_gate([{"identifier": c.get("identifier") if c else None,
                           "status": c.get("status") if c else None}
                          for c in linked.values()] or None)
    if gate is None and not linked:
        gate = ("no-linked-card", {})
    if gate is not None:
        res["reason"], res["det"] = gate
        return res
    res["det"] = {"cards": sorted(c["identifier"] for c in linked.values())}
    res["linked_ids"] = {c["identifier"]: iid
                         for iid, c in linked.items() if c}
    res["ws_ids"] = [row["id"] for row in ews]
    if any(row.get("status") not in LIVE_WORKSPACE_STATES for row in ews):
        res["reason"] = "ws-state-odd"
        return res
    if (path_referenced(worktree, ctx["run_paths"])
            or any(i in ctx["run_issue_ids"] for i in linked)
            or any(row["id"] in ctx["run_ws_ids"] for row in ews)):
        res["reason"] = "live-run-ref"
        return res
    if path_referenced(worktree, ctx["proc_paths"]):
        res["reason"] = "live-process"
        return res
    try:
        busy = recent_activity(run, worktree, minutes)
    except ReclaimError:
        res["reason"] = "find-error"
        return res
    if busy:
        res["reason"] = "recent-activity"
        return res
    rc, out = run(["git", "rev-parse", "--git-common-dir"], cwd=worktree)
    if rc != 0:
        res["reason"] = "git-error"
        return res
    rc1, up = run(["git", "log", "--branches", "--not", "--remotes",
                   "--oneline"], cwd=worktree)
    rc2, st = run(["git", "stash", "list"], cwd=worktree)
    if rc1 != 0 or rc2 != 0:
        res["reason"] = "git-error"
        return res
    repo_unpushed = len([line for line in up.splitlines() if line])
    repo_stash = len([line for line in st.splitlines() if line])
    try:
        branch_unpushed, proven = unpushed_after_proof(run, worktree)
    except RemoteUnreadable:
        res["reason"] = "remote-unreadable"
        return res
    except ReclaimError:
        res["reason"] = "git-error"
        return res
    rc, name = run(["git", "symbolic-ref", "-q", "--short", "HEAD"],
                   cwd=worktree)
    if rc not in (0, 1):
        res["reason"] = "git-error"
        return res
    branch = name.strip()
    branch_stash = 0
    if repo_stash:
        rc, lines = run(["git", "stash", "list"], cwd=worktree)
        if rc != 0:
            res["reason"] = "git-error"
            return res
        branch_stash = branch_stash_hits(lines.splitlines(), branch)
    res["git"] = {"repo_unpushed": repo_unpushed, "repo_stash": repo_stash,
                  "branch_unpushed": branch_unpushed, "branch": branch,
                  "branch_stash": branch_stash, "pr_proven": proven}
    res["linked"] = res["det"].get("cards")
    for cand in find_cache_dirs(run, worktree):
        rel = os.path.relpath(cand, worktree)
        if os.path.basename(cand) in CARGO_TARGET_NAMES:
            # Reported and sized, never offered (case_160a06b2). Checked before
            # the symlink/ignore gates so no later branch can promote it.
            res.setdefault("cargo_target_excluded", []).append(
                {"p": cand, "name": os.path.basename(cand)})
            continue
        if os.path.islink(cand):
            res.setdefault("skipped_symlink", []).append(rel)
            continue
        rc, _ = run(["git", "check-ignore", "-q", "--", rel], cwd=worktree)
        if rc == 1:
            res.setdefault("not_ignored", []).append(rel)
            continue
        if rc != 0:
            res.setdefault("ls-error", []).append(rel)
            continue
        rc, tracked = run(["git", "ls-files", "--", rel], cwd=worktree)
        ok, reason = cache_dir_ok(is_symlink=False, check_ignore_rc=0,
                                  ls_files_rc=rc, ls_files_out=tracked)
        if not ok:
            res.setdefault("tracked_inside" if reason == "tracked-inside"
                           else "ls-error", []).append(rel)
            continue
        try:
            nested = nested_repo(run, cand)
        except Exception:
            res.setdefault("ls-error", []).append(rel)
            continue
        if nested:
            res.setdefault("nested_repo", []).append(rel)
            continue
        res["caches"].append({"p": cand, "name": os.path.basename(cand)})
    return res


def deletable(result):
    """Branch-scoped delete rule: no unpushed HEAD commits, no branch stash."""
    git = result.get("git") or {}
    return (result["reason"] is None and result.get("caches")
            and not git.get("branch_unpushed") and not git.get("branch_stash"))


def summarize(results):
    """Split evaluated worktrees into strict-report vs branch-delete buckets."""
    eligible = [r for r in results
                if r["reason"] is None and r.get("caches")]
    strict, branch = [], []
    hits = collections.Counter()
    for res in eligible:
        git = res["git"]
        if git["repo_stash"]:
            hits["stash(repo-wide)"] += 1
        if git["repo_unpushed"]:
            hits["unpushed(repo-wide, --branches --not --remotes)"] += 1
        if git["branch_unpushed"]:
            hits["unpushed(this worktree HEAD)"] += 1
        if git["branch_stash"]:
            hits["stash naming this worktree branch"] += 1
        if (not git["repo_stash"] and not git["repo_unpushed"]
                and not git["branch_unpushed"]):
            strict.append(res)
        if deletable(res):
            branch.append(res)
    by_name = collections.Counter()
    for res in strict:
        for cand in res["caches"]:
            by_name[cand["name"]] += cand.get("bytes", 0)
    excluded = collections.Counter(r["reason"] for r in results if r["reason"])
    cargo = [r for r in results
             if r["reason"] is None and r.get("cargo_target_excluded")]
    # Fail-closed evaluation errors are kept and counted, never passed.
    n_errors = (sum(1 for r in results if r["reason"] in EVAL_ERROR_REASONS)
                + sum(len(r.get("ls-error", [])) for r in results)
                + sum(1 for r in results
                      for c in r.get("caches", [])
                      + r.get("cargo_target_excluded", [])
                      if c.get("size_error")))
    return {
        "worktree_dirs": len(results),
        "n_errors": n_errors,
        "nested_repo_dirs": sum(len(r.get("nested_repo", []))
                                for r in results),
        "excluded_by_rule": dict(excluded),
        "passed_card_live_gates": len([r for r in results
                                       if r["reason"] is None]),
        "git_gate_hits_among_passed": dict(hits),
        "worktrees_with_ignored_caches": len(eligible),
        "total_ignored_cache_bytes_before_git_gate": sum(
            c.get("bytes", 0) for r in eligible for c in r["caches"]),
        "strict_card_rule": {
            "worktrees": len(strict),
            "dirs": sum(len(r["caches"]) for r in strict),
            "bytes": sum(c.get("bytes", 0) for r in strict
                         for c in r["caches"])},
        "branch_scoped_rule": {
            "worktrees": len(branch),
            "dirs": sum(len(r["caches"]) for r in branch),
            "bytes": sum(c.get("bytes", 0) for r in branch
                         for c in r["caches"])},
        "strict_bytes_by_dirname": dict(by_name),
        "cargo_target_excluded": {
            "worktrees": len(cargo),
            "dirs": sum(len(r["cargo_target_excluded"]) for r in cargo),
            "bytes": sum(c.get("bytes", 0) for r in cargo
                         for c in r["cargo_target_excluded"])},
    }


def dir_bytes(run, path):
    rc, out = run(["du", "-sB1", "--", path], timeout=1800)
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return 0


def dir_size(run, path):
    """(bytes, ok): ok is False when du could not give a complete total."""
    try:
        rc, out = run(["du", "-sB1", "--", path], timeout=1800)
        total = int(out.split()[0])
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        return 0, False
    return total, rc == 0


def enumerate_worktrees(root):
    """Every per-issue worktree dir on disk under the Paperclip home."""
    cands = set(glob.glob(os.path.join(root, "worktrees", "*", "*")))
    cands |= set(glob.glob(os.path.join(
        root, "projects", "*", "*", ".paperclip", "worktrees", "*")))
    cands |= set(glob.glob(os.path.join(
        root, "projects", "*", "*", "*", ".paperclip", "worktrees", "*")))
    return sorted(path for cand in cands
                  for path in [os.path.realpath(cand)]
                  if os.path.isdir(path))


def card_gate_reason(api, res):
    """None when every linked card re-reads terminal, else the skip reason.

    A re-read error is a tool error, never "assumed terminal".
    """
    linked = res.get("linked") or []
    if not linked:
        return "card-not-terminal"
    for ident in linked:
        issue_id = res.get("linked_ids", {}).get(ident)
        if issue_id is None:
            return "card-not-terminal"
        try:
            status = api.issue_status(issue_id)
        except Exception:
            return "card-reread-error"
        if status not in TERMINAL_STATUSES:
            return "card-not-terminal"
    return None


def recheck_worktree(run, api, res, minutes, refresh):
    """Re-run every non-card gate on one worktree, right before its deletes.

    The scan can be hours old by the time a worktree's turn comes (the full
    evaluation precedes the first rm), so a run started, a file written, a
    commit made or a stash saved since then must keep the worktree. Returns
    None when it is still deletable, else a short reason. Any error while
    re-checking is a refusal, never a pass.
    """
    worktree = res["w"]
    try:
        if not os.path.exists(os.path.join(worktree, ".git")):
            return "not-a-git-worktree"
        ws_ids = res.get("ws_ids") or []
        issue_ids = list((res.get("linked_ids") or {}).values())
        if not ws_ids or not issue_ids:
            return "no-link-ids"
        refs = refresh(api)
        if (path_referenced(worktree, refs["run_paths"])
                or any(i in refs["run_issue_ids"] for i in issue_ids)
                or any(w in refs["run_ws_ids"] for w in ws_ids)):
            return "live-run-ref"
        if path_referenced(worktree, refs["proc_paths"]):
            return "live-process"
        try:
            busy = recent_activity(run, worktree, minutes)
        except ReclaimError:
            return "find-error"
        if busy:
            return "recent-activity"
        rc, name = run(["git", "symbolic-ref", "-q", "--short", "HEAD"],
                       cwd=worktree)
        if rc not in (0, 1):
            return "git-error"
        branch = name.strip()
        try:
            unpushed, _ = unpushed_after_proof(run, worktree)
        except RemoteUnreadable:
            return "remote-unreadable"
        if unpushed:
            return "unpushed-head"
        rc, stash = run(["git", "stash", "list"], cwd=worktree)
        if rc != 0:
            return "git-error"
        if branch_stash_hits(stash.splitlines(), branch):
            return "branch-stash"
    except Exception:
        return "recheck-error"
    return None


def recheck_cache(run, worktree, cand):
    """Last gate on ONE dir, immediately before its rm. None = still ok.

    Name first: only the explicit dep-cache set may ever reach rm, so a Cargo
    `target` (or any stale/handcrafted candidate) is refused here even if
    every earlier stage were bypassed.
    """
    path = cand["p"]
    name = os.path.basename(path)
    if name != cand.get("name") or name not in DEP_CACHE_NAMES:
        return "not-a-reclaimable-name"
    try:
        if os.path.islink(path):
            return "symlink"
        if not os.path.isdir(path):
            return "gone"
        if not os.path.realpath(path).startswith(worktree.rstrip("/") + "/"):
            return "escapes-worktree"
        rel = os.path.relpath(path, worktree)
        ignore_rc, _ = run(["git", "check-ignore", "-q", "--", rel],
                           cwd=worktree)
        ls_rc, tracked = run(["git", "ls-files", "--", rel], cwd=worktree)
        nested = nested_repo(run, path)
    except Exception:
        return "recheck-error"
    ok, reason = cache_dir_ok(is_symlink=False, check_ignore_rc=ignore_rc,
                              ls_files_rc=ls_rc, ls_files_out=tracked)
    if not ok:
        return reason
    return "nested-repo" if nested else None


def apply_deletes(run, api, candidates, minutes=RECENT_MINUTES_DEFAULT,
                  refresh=refresh_live_refs):
    """Delete eligible caches, re-proving every gate right before each rm.

    `candidates` is a fresh evaluation (never a stored dryrun.json). Per
    worktree: the card re-read, then live runs/processes, recent activity,
    pushed-HEAD proof and stash (recheck_worktree). Per directory: name,
    symlink, containment, ignored, untracked (recheck_cache). `api` must
    provide `issue_status(issue_id)` plus what `refresh` reads; anything
    unproven — INCLUDING an error — skips, never deletes.
    """
    freed, deleted, errors = collections.Counter(), 0, []
    card_skips = 0
    wt_skips, cache_skips = collections.Counter(), collections.Counter()
    for res in candidates:
        worktree = res["w"]
        card_reason = card_gate_reason(api, res)
        if card_reason:
            card_skips += 1
            wt_skips[card_reason] += 1
            continue
        reason = recheck_worktree(run, api, res, minutes, refresh)
        if reason:
            wt_skips[reason] += 1
            continue
        for cand in res["caches"]:
            reason = recheck_cache(run, worktree, cand)
            if reason:
                cache_skips[reason] += 1
                continue
            try:
                rc = subprocess.run(["rm", "-rf", "--", cand["p"]]).returncode
            except OSError:
                rc = 1
            if rc == 0 and not os.path.exists(cand["p"]):
                freed[cand["name"]] += cand.get("bytes", 0)
                deleted += 1
            else:
                errors.append(cand["p"])
    tool_errors = sum(n for counts in (wt_skips, cache_skips)
                      for reason, n in counts.items()
                      if reason in TOOL_ERROR_REASONS)
    return {"deleted_dirs": deleted,
            "skipped_worktrees_card_reopened": card_skips,
            "skipped_worktrees_by_reason": dict(wt_skips),
            "skipped_dirs_by_reason": dict(cache_skips),
            "errors": errors[:20], "n_errors": len(errors) + tool_errors,
            "alloc_bytes_by_dirname": dict(freed),
            "alloc_bytes_total": sum(freed.values())}


def run_tool(apply, run, api, out_dir, root, minutes, workers,
             mode_why=None, registry_gate=None, gate_error=False):
    """One full pass: fresh evaluation, sizing, report; delete only on apply.

    mode_why/registry_gate/gate_error record the --registry-gate decision in
    the machine-readable summary (the first stderr line is human-readable
    only). A gate ERROR (unreadable/malformed/missing registry entry) counts
    in n_errors so a misconfigured firing cannot report n_errors 0; an
    intentional dry-run (detector/paused/etc.) leaves n_errors unchanged.
    """
    started = time.time()
    ctx = build_context(api)
    dirs = enumerate_worktrees(root)
    print("worktree dirs on disk: %d" % len(dirs), file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers) as pool:
        results = list(pool.map(
            lambda w: evaluate_worktree(run, ctx, w, minutes), dirs))
    eligible = [r for r in results
                if r["reason"] is None and r.get("caches")]
    paths = [c for r in eligible for c in r["caches"]]
    paths += [c for r in results if r["reason"] is None
              for c in r.get("cargo_target_excluded", [])]
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers) as pool:
        sizes = list(pool.map(lambda c: dir_size(run, c["p"]), paths))
    for cand, (size, ok) in zip(paths, sizes):
        cand["bytes"] = size
        if not ok:
            cand["size_error"] = True
    summary = summarize(results)
    summary["took_s"] = round(time.time() - started)
    summary["mode"] = "apply" if apply else "dry-run"
    if mode_why is not None:
        summary["mode_reason"] = mode_why
    if registry_gate is not None:
        summary["registry_gate"] = registry_gate
        summary["registry_gate_error"] = bool(gate_error)
        if gate_error:
            summary["n_errors"] += 1
    os.makedirs(out_dir, exist_ok=True)
    dryrun_path = os.path.join(out_dir, "dryrun.json")
    try:
        write_json_atomic(dryrun_path, {"summary": summary, "results": results})
    except OSError as exc:
        print(json.dumps(summary, indent=1), flush=True)
        raise ReclaimError("%s: errno=%s" % (dryrun_path, exc.errno)) from exc
    print(json.dumps(summary, indent=1))
    if not apply:
        return {"summary": summary, "applied": None}
    # --apply re-evaluates everything fresh above, then re-reads each card
    # per worktree inside apply_deletes. A stored dryrun.json is never input.
    report = apply_deletes(run, api, [r for r in results if deletable(r)],
                           minutes=minutes)
    # One count to escalate on: the apply report includes evaluation errors.
    report["evaluation_n_errors"] = summary["n_errors"]
    report["n_errors"] += summary["n_errors"]
    apply_path = os.path.join(out_dir, "apply.json")
    try:
        write_json_atomic(apply_path, report)
    except OSError as exc:
        print(json.dumps(report, indent=1), flush=True)
        raise ReclaimError("%s: errno=%s" % (apply_path, exc.errno)) from exc
    print(json.dumps(report, indent=1))
    return {"summary": summary, "applied": report}


def main(argv=None, run=None, api=None):
    opts = parse_argv(argv if argv is not None else sys.argv[1:])
    out_dir = opts.out_dir or os.environ.get("PAPERCLIP_SCRATCH_DIR")
    if not out_dir:
        raise ReclaimError("no --out-dir and $PAPERCLIP_SCRATCH_DIR is unset")
    run = run or live_run
    apply, why, gate_error = resolve_mode_ex(opts)
    print("mode: %s (%s)" % ("apply" if apply else "dry-run", why),
          file=sys.stderr)
    if api is None:
        base = os.environ.get("PAPERCLIP_API_URL")
        company = os.environ.get("PAPERCLIP_COMPANY_ID")
        key = os.environ.get("PAPERCLIP_API_KEY")
        if apply and not (base and company and key):
            raise SafetyRefusal(
                "refusing --apply without PAPERCLIP_API_URL/COMPANY_ID/KEY: "
                "the per-delete card re-read would be impossible")
        if not (base and company and key):
            raise ReclaimError("API env is required even for dry-run "
                               "(card join reads the live board)")
        # Host-delegated verdict (run-worktree-cache-reclaim.sh): ApiClient
        # honors it only on exact equality with the base, else it runs the
        # normal check and fails closed.
        api = ApiClient(base, company, key,
                        allowlist_checked_base=os.environ.get(
                            "PAPERCLIP_API_ALLOWLIST_CHECKED_BASE"))
    return run_tool(apply, run, api, out_dir, opts.root,
                    opts.recent_minutes, opts.workers,
                    mode_why=why, registry_gate=opts.registry_gate,
                    gate_error=gate_error)


if __name__ == "__main__":
    try:
        main()
    except SafetyRefusal as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        sys.exit(1)
    except ReclaimError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(2)
