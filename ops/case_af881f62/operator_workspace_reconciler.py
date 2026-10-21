#!/usr/bin/env python3
"""Operator workspace reconciler (owner directive case_af881f62, 2026-10-09).

The Operator agent runs over SSH (environment e8f6ba62) on the SAME host
(node7b1ff338). For every run on a card that carries a project workspace, Paperclip
uploads the whole workspace to the remote path and syncs it back into
container-tmp. The Paperclip Fork workspace is ~60G each way including nested
worktrees, and this caused the 98% / ENOSPC event at 12:03Z on 2026-10-09.
Other agents work in place with no copy.

The fix: Operator cards carry no project workspace --
`executionWorkspacePreference: agent_default` with `executionWorkspaceId`,
`projectWorkspaceId` and `executionWorkspaceSettings` all null. Peers proved
this by hand (17 Fork cards flipped; case_63f9656b is running that way now).
This script is the reconciler that applies the same default to every
Operator card that is safe to touch, plus the documented creation recipe
(see RUNBOOK.md) so new cards start that way.

SAFETY PROPERTIES (structural, not prose).

- Dry-run is the default. NOTHING mutates unless `--apply` is passed WITH
  the backup file the dry-run wrote, so the exact original workspace fields
  always exist before the first PATCH. Classification and recorded originals
  come from the full single-issue read: the company issue-list endpoint
  always returns `executionWorkspaceSettings: null`, so list rows would both
  miss settings-only cards and back up `null` over real settings.
- Never touches live work or review paths: only `todo`, `backlog` and
  `blocked` cards are candidates. `in_progress` and `in_review` cards are
  skipped unconditionally, and every candidate is re-read immediately before
  its PATCH: a card whose status moved, whose assignee changed, or which
  grew a live run since the dry-run is recorded as stale and skipped.
- A live run is checked per candidate (`GET /runs`: any row with null
  finishedAt and a non-terminal status). A card with a live run is never
  PATCHed: the checkout lock would 409, and the workspace path could be in
  use under the run.
- The first PATCH doubles as the no-wake probe: wake diagnostics are
  snapshotted before and re-read after, and any new wake aborts the apply.
  A bare field PATCH (no comment, no @-mention) creates no wake; this check
  proves it on the live server instead of asserting it.
- Every PATCH is verified by read-back: the card must show preference
  `agent_default` and null IDs afterwards, else the card is recorded as
  failed, not as flipped.
- Consecutive-failure abort (2) turns a systemic problem (auth, schema,
  server fault, dropped connection, undecodable body) into a stop, not a
  79-card blast radius. Every abort path still writes the restore file for
  cards flipped before the abort, so a partial apply always leaves a
  rollback artifact; raw transport errors that escape the API wrapper are
  counted as failures (never silent) for the same reason.
- A PATCH that raises without a definite HTTP 4xx rejection is never
  trusted: timeouts, dropped connections and 5xx/504 may commit
  server-side after the client stops waiting, so even a clean re-read
  proves nothing and the card is recorded as `flip-uncertain` with a
  restore entry from the pre-PATCH read. Only a definite 4xx rejection
  consults the re-read (old fields -> plain `error` with no restore;
  committed or unreadable re-read -> `flip-uncertain`).
- `--restore` re-applies the pre-PATCH workspace fields of flipped cards
  (captured from the fresh read `apply_one` makes just before each PATCH,
  not from the older dry-run values), so every flip is reversible; cards
  skipped as stale are left out of the restore file and never reverted.
  Restore re-checks each target before touching it (status, assignee, live
  run) and reports moved cards as stale instead of overwriting them.
- No quiescence gate (unlike model_pin_cleanup): there is no competing
  writer racing this script. New workspace-carrying cards appear only at
  creation (inheritance), which is drift the re-sweep / host timer covers,
  not a race the apply step must exclude.

MODES.

  python3 ops/case_af881f62/operator_workspace_reconciler.py
      Dry-run (default): page all Operator cards, classify, write a backup
      JSON of the four workspace fields for each flip candidate, print the
      plan table and counts. Exit 0. No mutation.

  python3 ops/case_af881f62/operator_workspace_reconciler.py --apply --backup <backup.json>
      Flip candidates one card at a time per the rules above. Requires the
      backup file, the no-wake probe on the first card, per-card read-back,
      rate limiting, and writes a restore file.

  python3 ops/case_af881f62/operator_workspace_reconciler.py --restore <backup-or-restore.json>
      Re-apply the exact original workspace fields. Rate-limited, aborts
      after 2 consecutive failures.

Credentials travel by inherited environment only (never argv):
PAPERCLIP_API_URL, PAPERCLIP_API_KEY, PAPERCLIP_COMPANY_ID, and optionally
PAPERCLIP_RUN_ID (sent as X-Paperclip-Run-Id on mutating calls for the run
audit trail).
"""
import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

OPERATOR_AGENT_ID = "00000000-0000-4000-8000-000000000052"
BACKUP_VERSION = 1
FLIP_PREFERENCE = "agent_default"
CANDIDATE_STATUSES = ("todo", "backlog", "blocked")
FAILURE_BUDGET = 2

# Mirror of sibling_guard.sh: a row is LIVE when unfinished AND non-terminal.
TERMINAL_RUN_STATUSES = frozenset({
    "succeeded", "failed", "cancelled", "canceled", "expired",
    "timed_out", "timeout", "errored", "error", "skipped",
})

WORKSPACE_FIELDS = (
    "executionWorkspacePreference",
    "executionWorkspaceId",
    "projectWorkspaceId",
    "executionWorkspaceSettings",
)


class ReconcileError(Exception):
    """Loud safety refusal or abort: stale backup, failed probe, budget spent."""


def utcnow():
    return datetime.now(timezone.utc)


def run_is_live(row):
    """Pure: True when a run-list row is an active run (not finished)."""
    if not isinstance(row, dict):
        return False
    if row.get("finishedAt"):
        return False
    status = str(row.get("status") or "").lower()
    return status not in TERMINAL_RUN_STATUSES


def original_fields(issue):
    """Pure: the four workspace fields as stored on the card (None when absent)."""
    if not isinstance(issue, dict):
        return {}
    return {field: issue.get(field) for field in WORKSPACE_FIELDS}


def is_clean(fields):
    """Pure: True when the card already carries no project workspace."""
    return (
        fields.get("executionWorkspacePreference") == FLIP_PREFERENCE
        and not fields.get("executionWorkspaceId")
        and not fields.get("projectWorkspaceId")
        and not fields.get("executionWorkspaceSettings")
    )


def classify(issue, operator_agent_id):
    """Pure: (verdict, reason) for one card.

    'candidate' means safe to flip once the live-run check also passes.
    Every other verdict is a skip the apply step must never touch.
    """
    if not isinstance(issue, dict):
        return ("skip", "not-an-issue-row")
    if issue.get("assigneeAgentId") != operator_agent_id:
        return ("skip", "not-operator-assigned")
    status = issue.get("status")
    if status in ("done", "cancelled"):
        return ("skip", "terminal")
    if status not in CANDIDATE_STATUSES:
        return ("skip", "live-work-or-review-path")
    if is_clean(original_fields(issue)):
        return ("skip", "already-agent-default")
    return ("candidate", "carries-workspace")


def build_flip_patch():
    """Pure: the PATCH body that detaches a card from project workspaces."""
    return {
        "executionWorkspacePreference": FLIP_PREFERENCE,
        "executionWorkspaceId": None,
        "projectWorkspaceId": None,
        "executionWorkspaceSettings": None,
    }


def readback_ok(issue):
    """Pure: True when a re-read card shows the flipped state."""
    return is_clean(original_fields(issue))


def wake_ids(diagnostics):
    """Pure: stable identity set of wake rows for before/after comparison."""
    if not isinstance(diagnostics, dict):
        return set()
    events = diagnostics.get("events")
    if not isinstance(events, list):
        return set()
    ids = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        key = event.get("id") or event.get("runId") or json.dumps(event, sort_keys=True)
        ids.add(str(key))
    return ids


def detect_new_wakes(before, after):
    """Pure: wake identities present after the probe PATCH but not before."""
    return set(after) - set(before)


def _valid_api_base(base):
    # BASE host validation: the bearer below is sent ONLY to this base.
    # Refuse a base that is not an http(s) URL with a non-empty bare host:
    # no userinfo (`user@host` misdirects the credential), no whitespace (a
    # newline would inject a request directive), no query or fragment (the
    # appended /api path would be swallowed into them), a real hostname (an
    # empty `:port`-only authority is not a host), a numeric port, and no
    # `{`/`}` anywhere (curl expands `{a,b}` globs, so one base is one host
    # -- parity with the shell gate). A well-formed hostile host still
    # passes; blocking that needs an operator-owned host allowlist, not a
    # per-script guess.
    if not base.startswith(("http://", "https://")):
        return False
    if "@" in base:
        return False
    if any(char in " \t\n\r\x0b\x0c" for char in base):
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


class PaperclipClient:
    def __init__(self, base_url, api_key, company_id, run_id=None):
        base = (base_url or "").rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        if not base or not api_key or not company_id:
            raise ReconcileError(
                "PAPERCLIP_API_URL, PAPERCLIP_API_KEY and PAPERCLIP_COMPANY_ID "
                "must all be set for live modes."
            )
        if not _valid_api_base(base):
            raise ReconcileError(
                "refusing: PAPERCLIP_API_URL is not a clean http(s) base")
        self.base = base
        self.key = api_key
        self.company = company_id
        self.run_id = run_id

    def _request(self, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.key}
        if self.run_id:
            headers["X-Paperclip-Run-Id"] = self.run_id
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")[:500]
            except Exception:
                detail = "<unreadable>"
            raise ReconcileError(
                f"API {method} {path} failed: HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                UnicodeDecodeError, http.client.HTTPException, OSError,
                ValueError) as exc:
            # OSError covers sockets, TLS and pipes; HTTPException covers
            # truncated/dropped HTTP exchanges that urllib does not wrap;
            # ValueError covers undecodable or unparsable bodies. All three
            # families are transport failures, so they become ReconcileError
            # (counted by the apply budget) instead of escaping past the
            # partial-restore write. (URLError/TimeoutError/JSONDecodeError
            # are listed explicitly although OSError/ValueError cover them.)
            raise ReconcileError(f"API {method} {path} failed: {exc}") from exc

    def get(self, path):
        return self._request("GET", path)

    def patch(self, path, body):
        return self._request("PATCH", path, body)

    def list_operator_issues(self, operator_agent_id, page_size=200, max_pages=1000):
        """Page every card assigned to the Operator (all statuses).

        Classification is client-side so a server filter change can only add
        work, never silently drop cards from the plan.
        """
        issues = []
        offset = 0
        for _ in range(max_pages):
            page = self.get(
                f"/api/companies/{self.company}/issues"
                f"?assigneeAgentId={operator_agent_id}"
                f"&limit={page_size}&offset={offset}"
            )
            rows = page if isinstance(page, list) else page.get("issues", [])
            if not rows:
                break
            issues.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < page_size:
                break
            offset += len(rows)
        else:
            raise ReconcileError("issue pagination exceeded max_pages; refusing")
        return issues

    def live_runs(self, issue_id):
        rows = self.get(f"/api/issues/{issue_id}/runs")
        if isinstance(rows, dict):
            rows = rows.get("runs", [])
        if not isinstance(rows, list):
            raise ReconcileError(
                f"GET /api/issues/{issue_id}/runs returned non-list; refusing"
            )
        return [row for row in rows if run_is_live(row)]

    def wake_diagnostics(self, issue_id):
        return self.get(f"/api/issues/{issue_id}/diagnostics/wakes")


def plan_counts(plan):
    counts = {
        "candidates": 0,
        "skipped_live_run": 0,
        "skipped": 0,
        "by_reason": {},
    }
    for entry in plan:
        verdict = entry["verdict"]
        reason = entry["reason"]
        counts["by_reason"].setdefault(f"{verdict}:{reason}", 0)
        counts["by_reason"][f"{verdict}:{reason}"] += 1
        if verdict == "candidate":
            counts["candidates"] += 1
        elif reason == "live-run":
            counts["skipped_live_run"] += 1
        else:
            counts["skipped"] += 1
    return counts


def check_candidate_live(client, entry):
    """Live-run gate for one candidate entry (read-only; thread-safe)."""
    try:
        live = client.live_runs(entry["issueId"])
    except ReconcileError as exc:
        entry["verdict"] = "error"
        entry["reason"] = f"run-list-unreadable: {exc}"
    else:
        if live:
            entry["verdict"] = "skip"
            entry["reason"] = "live-run"
    return entry


def refine_entry_from_full_read(client, entry, operator_agent_id):
    """Re-classify one entry from the full single-issue read (thread-safe).

    The company issue-list endpoint always returns
    `executionWorkspaceSettings: null` (server `issueListSelect`), so a card
    whose only leftover is a settings override looks clean on the list row,
    and a backup built from list rows would restore `null` over real
    settings. The single-issue GET returns the stored value, so the verdict
    and the recorded original both come from that read.
    """
    try:
        full = client.get(f"/api/issues/{entry['issueId']}")
    except ReconcileError as exc:
        entry["verdict"] = "error"
        entry["reason"] = f"full-read-unreadable: {exc}"
        return entry
    if not isinstance(full, dict):
        entry["verdict"] = "error"
        entry["reason"] = "full-read-unreadable: non-object response"
        return entry
    verdict, reason = classify(full, operator_agent_id)
    entry["verdict"] = verdict
    entry["reason"] = reason
    entry["status"] = full.get("status")
    entry["title"] = (full.get("title") or "")[:80]
    entry["original"] = original_fields(full)
    return entry


def run_dry_run(client, operator_agent_id, workers=8):
    """Read-only pass: classify every Operator card, live-run-check candidates.

    Classification and recorded originals come from the full single-issue
    read, not the list row: the list endpoint nulls
    `executionWorkspaceSettings`, which would both miss settings-only cards
    and back up `null` over real settings. Status/assignee filtering still
    uses the list row (those fields are real there), so only candidates and
    apparently-clean cards pay for the extra GET.

    The per-card reads are independent GETs and run in a bounded thread pool
    (reads are not rate-limited); plan order is preserved.
    """
    issues = client.list_operator_issues(operator_agent_id)
    plan = []
    for issue in issues:
        verdict, reason = classify(issue, operator_agent_id)
        plan.append({
            "issueId": issue.get("id"),
            "identifier": issue.get("identifier"),
            "status": issue.get("status"),
            "title": (issue.get("title") or "")[:80],
            "verdict": verdict,
            "reason": reason,
            "original": original_fields(issue),
        })
    refine_idx = [
        pos for pos, entry in enumerate(plan)
        if entry["verdict"] == "candidate"
        or entry["reason"] == "already-agent-default"
    ]
    if refine_idx:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [
                pool.submit(
                    refine_entry_from_full_read, client, plan[pos],
                    operator_agent_id)
                for pos in refine_idx
            ]
            for future in futures:
                future.result()
    candidate_idx = [
        pos for pos, entry in enumerate(plan)
        if entry["verdict"] == "candidate"
    ]
    if candidate_idx:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [
                pool.submit(check_candidate_live, client, plan[pos])
                for pos in candidate_idx
            ]
            for future in futures:
                future.result()
    return issues, plan


def build_backup(plan, stamp, operator_agent_id):
    entries = [
        {
            "issueId": entry["issueId"],
            "identifier": entry["identifier"],
            "status": entry["status"],
            "original": entry["original"],
        }
        for entry in plan
        if entry["verdict"] == "candidate"
    ]
    return {
        "version": BACKUP_VERSION,
        "tool": "operator_workspace_reconciler",
        "createdAt": stamp,
        "operatorAgentId": operator_agent_id,
        "originalsFrom": "single-issue-read",
        "flip": build_flip_patch(),
        "entries": entries,
    }


def render_plan_table(plan):
    lines = ["identifier status verdict/reason"]
    for entry in plan:
        if entry["verdict"] == "skip" and entry["reason"] in (
            "not-operator-assigned",
            "terminal",
            "already-agent-default",
        ):
            continue
        lines.append(
            f"{entry['identifier']} {entry['status']} "
            f"{entry['verdict']}:{entry['reason']}"
        )
    return "\n".join(lines)


def load_backup_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconcileError(f"cannot read backup {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BACKUP_VERSION:
        raise ReconcileError(
            f"backup {path}: unsupported version (want {BACKUP_VERSION}); refusing"
        )
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ReconcileError(f"backup {path}: no entries list; refusing")
    return data


def verify_no_wake(client, issue_id):
    """Empirical no-wake probe: snapshot wakes, caller PATCHes, we re-read.

    Returns the 'before' identity set; the caller compares after its PATCH.
    """
    before = wake_ids(client.wake_diagnostics(issue_id))
    return before


def do_dry_run(client, args):
    _, plan = run_dry_run(client, args.operator_agent_id,
                          workers=args.fetch_workers)
    counts = plan_counts(plan)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_path = args.backup_out or f"operator-workspace-backup-{stamp}.json"
    backup = build_backup(plan, stamp, args.operator_agent_id)
    try:
        with open(backup_path, "w", encoding="utf-8") as handle:
            json.dump(backup, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise ReconcileError(f"cannot write backup {backup_path}: {exc}") from exc

    if args.json:
        print(json.dumps(
            {"counts": counts, "plan": plan, "backup": backup_path},
            indent=2, sort_keys=True,
        ))
    else:
        print(f"operator cards paged: {len(plan)}")
        print(f"  flip candidates: {counts['candidates']}")
        print(f"  skipped (live run): {counts['skipped_live_run']}")
        print(f"  skipped (other): {counts['skipped']}")
        print("by verdict:reason:")
        for key in sorted(counts["by_reason"]):
            print(f"  {key}: {counts['by_reason'][key]}")
        print()
        print(render_plan_table(plan))
        print()
        print(f"backup of {len(backup['entries'])} candidate(s) -> {backup_path}")
        print("dry-run only: nothing was mutated. --apply requires this backup file.")
    return 0


def _http_status(exc):
    """Best-effort HTTP status for a PATCH failure (None when unknown).

    Walks the exception chain for an HTTPError-style `.code`, then falls
    back to parsing "HTTP {code}" out of the message (the API wrapper
    formats HTTPError that way). Timeouts, dropped connections and 5xx
    have either no code or a 5xx code; definite rejections carry 4xx.
    """
    import re
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        code = getattr(cur, "code", None)
        if isinstance(code, int):
            return code
        for attr in ("__cause__", "__context__"):
            nxt = getattr(cur, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)
    match = re.search(r"HTTP\s+(\d{3})", str(exc))
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def is_definite_rejection(exc):
    """True only for a definite HTTP 4xx rejection (the server refused).

    4xx means the server saw the request and said no, so nothing was
    committed. Anything else -- timeouts, dropped connections, 5xx/504,
    undecodable bodies, or an unknown error -- may have committed
    server-side while the response was lost (Cloudflare 504s routinely
    save after the client stops waiting), so the re-read cannot be
    trusted and the card must keep a restore entry.
    """
    code = _http_status(exc)
    return code is not None and 400 <= code < 500


def apply_one(client, entry, rate_limit_seconds, operator_agent_id):
    """Flip a single card after re-verification. Returns an outcome string.

    On the flip path the entry's `appliedOriginal` is set to the workspace
    fields of the fresh pre-PATCH read, so the restore file reverts to what
    was actually on the card at flip time, not to the possibly older dry-run
    values.

    A PATCH that raises with no definite rejection (timeouts, dropped
    connections, 5xx/504, unreadable bodies) is recorded as
    `flip-uncertain` whatever a re-read shows: the server may still commit
    after the re-read, so the card keeps a restore entry from the pre-PATCH
    read. Only a definite HTTP 4xx rejection consults the re-read:
    still showing the old fields -> plain `error`; committed (should not
    happen for 4xx) or an unreadable re-read -> `flip-uncertain`
    (conservative: a commit cannot be ruled out).
    """
    issue = client.get(f"/api/issues/{entry['issueId']}")
    verdict, reason = classify(issue, operator_agent_id)
    if verdict != "candidate":
        return f"stale ({verdict}:{reason})"
    if client.live_runs(entry["issueId"]):
        return "stale (live-run)"
    entry["appliedOriginal"] = original_fields(issue)
    try:
        updated = client.patch(
            f"/api/issues/{entry['issueId']}", build_flip_patch())
    except Exception as exc:
        if not is_definite_rejection(exc):
            # Uncertain transport/server failure: a 504 or timeout only
            # means the client stopped waiting -- the server may commit
            # after this point, so even a clean re-read proves nothing.
            return f"flip-uncertain (uncertain error, re-read untrusted): {exc}"
        try:
            reread = client.get(f"/api/issues/{entry['issueId']}")
        except Exception as reread_exc:
            return f"flip-uncertain (re-read failed: {reread_exc}): {exc}"
        if readback_ok(reread):
            return f"flip-uncertain (committed): {exc}"
        return f"error: {exc}"
    if not (isinstance(updated, dict) and WORKSPACE_FIELDS[0] in updated):
        # Minimal/empty PATCH echo: one confirmatory re-read, never infer.
        updated = client.get(f"/api/issues/{entry['issueId']}")
    if not readback_ok(updated):
        return "failed (read-back mismatch)"
    if rate_limit_seconds > 0:
        time.sleep(rate_limit_seconds)
    return "flipped"


def fresh_original(entry):
    """Restore source for one flipped entry: the pre-PATCH read wins."""
    applied = entry.get("appliedOriginal")
    if isinstance(applied, dict):
        return {field: applied.get(field) for field in WORKSPACE_FIELDS}
    original = entry.get("original")
    if isinstance(original, dict):
        return {field: original.get(field) for field in WORKSPACE_FIELDS}
    return {field: None for field in WORKSPACE_FIELDS}


def write_restore_file(entries, outcomes, operator_agent_id, applied_from):
    """Persist the restore file for flipped cards; returns its path.

    Covers every flipped outcome (including probe-flagged ones: the PATCH
    landed, so the card needs reverting) plus `flip-uncertain` outcomes (a
    PATCH raised and the re-read confirmed the commit, or ruled nothing
    out). Stale-skipped entries are excluded:
    they may have moved on since the dry-run and must never be reverted.
    """
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    restore_path = f"operator-workspace-restore-{stamp}.json"
    # "flip" (not "flipped"): flipped, flipped BUT-*, and flip-uncertain
    # all need reverting; failed/stale/error never wrote anything.
    flipped_ids = {
        o["issueId"] for o in outcomes if o["outcome"].startswith("flip")
    }
    flipped_entries = [e for e in entries if e.get("issueId") in flipped_ids]
    restore = {
        "version": BACKUP_VERSION,
        "tool": "operator_workspace_reconciler",
        "createdAt": stamp,
        "appliedFrom": applied_from,
        "operatorAgentId": operator_agent_id,
        "outcomes": outcomes,
        "entries": [
            {
                "issueId": e.get("issueId"),
                "identifier": e.get("identifier"),
                "status": e.get("status"),
                "original": fresh_original(e),
            }
            for e in flipped_entries
        ],
        "originals": {
            e.get("issueId"): fresh_original(e) for e in flipped_entries
        },
    }
    try:
        with open(restore_path, "w", encoding="utf-8") as handle:
            json.dump(restore, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise ReconcileError(
            f"cannot write restore {restore_path}: {exc}"
        ) from exc
    return restore_path


def do_apply(client, args):
    backup = load_backup_file(args.backup)
    entries = backup["entries"]
    if not entries:
        print("backup holds zero candidates; nothing to do.")
        return 0
    operator_agent_id = backup.get("operatorAgentId") or OPERATOR_AGENT_ID
    outcomes = []
    failures = 0
    probed = False
    aborted = None
    for entry in entries:
        if aborted is not None:
            break
        issue_id = entry.get("issueId")
        identifier = entry.get("identifier", "?")
        try:
            if not probed:
                before = verify_no_wake(client, issue_id)
            outcome = apply_one(client, entry, args.rate_limit_seconds,
                                operator_agent_id)
        except ReconcileError as exc:
            # Pre-PATCH failures only: apply_one converts a failed PATCH
            # itself into flip-uncertain/error via a confirmatory re-read,
            # so anything reaching here attempted no write.
            outcomes.append({
                "issueId": issue_id, "identifier": identifier,
                "outcome": f"error: {exc}",
            })
            failures += 1
            print(f"{identifier}: error: {exc}")
        except Exception as exc:
            # Safety net: a raw transport error that escaped the API
            # wrapper must still count against the failure budget so the
            # abort below writes the partial restore for cards flipped
            # before this failure, instead of propagating with no
            # rollback artifact.
            outcomes.append({
                "issueId": issue_id, "identifier": identifier,
                "outcome": f"error: unexpected {type(exc).__name__}: {exc}",
            })
            failures += 1
            print(f"{identifier}: error: unexpected "
                  f"{type(exc).__name__}: {exc}")
        else:
            if not probed and outcome == "flipped":
                # The probe only means something around a real PATCH: a
                # stale entry performs no write, so a before/after
                # comparison on it would pass vacuously. Stale entries
                # leave the probe armed for the next flipped card.
                probed = True
                try:
                    after = wake_ids(client.wake_diagnostics(issue_id))
                except Exception as exc:
                    # Broad on purpose: the PATCH above already landed, so
                    # this card needs a restore entry. Any probe-read
                    # failure (API or raw transport) records the flipped
                    # outcome and aborts with a partial restore.
                    probe_outcome = outcome + " BUT-PROBE-READ-FAILED"
                    outcomes.append({
                        "issueId": issue_id, "identifier": identifier,
                        "outcome": probe_outcome,
                    })
                    print(f"{identifier}: {probe_outcome}")
                    aborted = ReconcileError(
                        f"no-wake probe unreadable on {identifier}: {exc}; "
                        f"aborting"
                    )
                    break
                new = detect_new_wakes(before, after)
                if new:
                    outcomes.append({
                        "issueId": issue_id, "identifier": identifier,
                        "outcome": outcome + " BUT-NEW-WAKE-DETECTED",
                    })
                    aborted = ReconcileError(
                        f"no-wake probe failed on {identifier}: new wake(s) "
                        f"{sorted(new)} after a bare field PATCH; aborting"
                    )
                    break
            outcomes.append({
                "issueId": issue_id, "identifier": identifier, "outcome": outcome,
            })
            if outcome.startswith(("failed", "error", "flip-uncertain")):
                # Any failure signal — rejected PATCH, lost response, or
                # read-back mismatch — counts toward the consecutive
                # budget; only flipped/stale (progress or a moved card)
                # reset it. (Pre-PATCH "error:" outcomes arrive here too:
                # apply_one returns them instead of raising.)
                failures += 1
            else:
                failures = 0
            print(f"{identifier}: {outcome}")
        if failures >= FAILURE_BUDGET:
            aborted = ReconcileError(
                f"{FAILURE_BUDGET} consecutive failures; aborting apply "
                f"({len(outcomes)}/{len(entries)} attempted)"
            )
            break
    # The restore file covers flipped cards only: an entry skipped as
    # stale may have moved on (new status, new fields) since the dry-run,
    # and a blanket restore would overwrite those newer fields with older
    # originals. It is written on abort paths too, so cards flipped before
    # the abort always have a rollback artifact.
    if aborted is not None:
        try:
            partial_path = write_restore_file(
                entries, outcomes, operator_agent_id, args.backup)
        except ReconcileError as exc:
            raise ReconcileError(f"{aborted}; AND {exc}") from aborted
        print(f"aborted; partial restore -> {partial_path}")
        raise aborted
    if not probed:
        print("no card was flipped; the no-wake probe had nothing to verify.")
    restore_path = write_restore_file(
        entries, outcomes, operator_agent_id, args.backup)
    flipped = sum(1 for o in outcomes if o["outcome"] == "flipped")
    uncertain = sum(
        1 for o in outcomes if o["outcome"].startswith("flip-uncertain"))
    print(f"flipped {flipped}/{len(entries)}; "
          f"flip-uncertain {uncertain}; restore -> {restore_path}")
    return 0


def restore_precheck(issue, operator_agent_id):
    """Pure: (verdict, reason) guarding one restore target.

    Mirrors the apply-time safety checks minus the workspace test (a card
    awaiting restore carries the flipped state by definition, so `classify`
    would misread it). A card that has since moved to live work, grown a
    live run, been reassigned, or gone terminal must not be touched.
    """
    if not isinstance(issue, dict):
        return ("skip", "not-an-issue-row")
    if issue.get("assigneeAgentId") != operator_agent_id:
        return ("skip", "assignee-changed")
    status = issue.get("status")
    if status in ("done", "cancelled"):
        return ("skip", "terminal")
    if status in ("in_progress", "in_review"):
        return ("skip", "live-work-or-review-path")
    return ("ok", "safe-to-restore")


def do_restore(client, args):
    data = load_backup_file(args.restore)
    entries = data.get("entries", [])
    originals = data.get("originals")
    if not entries and originals:
        entries = [
            {"issueId": issue_id, "identifier": issue_id, "original": original}
            for issue_id, original in originals.items()
        ]
    if not entries:
        print("restore file holds zero entries; nothing to do.")
        return 0
    operator_agent_id = data.get("operatorAgentId") or OPERATOR_AGENT_ID
    failures = 0
    restored = 0
    skipped = 0
    for entry in entries:
        issue_id = entry.get("issueId")
        original = entry.get("original")
        identifier = entry.get("identifier", "?")
        if not isinstance(original, dict):
            print(f"{identifier}: error: no original fields; skipping")
            failures += 1
            continue
        try:
            current = client.get(f"/api/issues/{issue_id}")
            verdict, reason = restore_precheck(current, operator_agent_id)
            if verdict != "ok":
                print(f"{identifier}: stale ({verdict}:{reason})")
                skipped += 1
                failures = 0
                continue
            if client.live_runs(issue_id):
                print(f"{identifier}: stale (skip:live-run)")
                skipped += 1
                failures = 0
                continue
            body = {field: original.get(field) for field in WORKSPACE_FIELDS}
            client.patch(f"/api/issues/{issue_id}", body)
            reread = client.get(f"/api/issues/{issue_id}")
            if original_fields(reread) != {f: original.get(f) for f in WORKSPACE_FIELDS}:
                print(f"{identifier}: failed (read-back mismatch)")
                failures += 1
            else:
                print(f"{identifier}: restored")
                restored = restored + 1
                failures = 0
        except ReconcileError as exc:
            print(f"{identifier}: error: {exc}")
            failures += 1
        if failures >= FAILURE_BUDGET:
            raise ReconcileError(
                f"{FAILURE_BUDGET} consecutive failures; aborting restore"
            )
        if args.rate_limit_seconds > 0:
            time.sleep(args.rate_limit_seconds)
    print(f"restored {restored}/{len(entries)}; skipped {skipped}")
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Default Operator cards to no project workspace (case_af881f62)."
    )
    parser.add_argument("--apply", action="store_true",
                        help="flip candidates (requires --backup)")
    parser.add_argument("--backup", default=None,
                        help="backup JSON from a dry-run (required for --apply)")
    parser.add_argument("--restore", default=None,
                        help="backup/restore JSON to revert flips")
    parser.add_argument("--backup-out", default=None,
                        help="dry-run backup output path")
    parser.add_argument("--operator-agent-id", default=OPERATOR_AGENT_ID,
                        help="operator agent id (override for tests)")
    parser.add_argument("--rate-limit-seconds", type=float, default=1.0,
                        help="sleep between mutating calls (default 1.0)")
    parser.add_argument("--fetch-workers", type=int, default=8,
                        help="bounded thread pool for per-card run-list reads "
                        "(default 8; reads are not rate-limited)")
    parser.add_argument("--json", action="store_true",
                        help="dry-run: print the machine-readable plan")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.restore and args.apply:
        print("error: --restore and --apply are mutually exclusive", file=sys.stderr)
        return 2
    if args.apply and not args.backup:
        print("error: --apply requires --backup <backup.json> from a dry-run",
              file=sys.stderr)
        return 2
    if args.rate_limit_seconds < 0:
        print("error: --rate-limit-seconds must be >= 0", file=sys.stderr)
        return 2
    if args.fetch_workers < 1:
        print("error: --fetch-workers must be >= 1", file=sys.stderr)
        return 2
    if args.restore:
        client = PaperclipClient(
            os.environ.get("PAPERCLIP_API_URL"),
            os.environ.get("PAPERCLIP_API_KEY"),
            os.environ.get("PAPERCLIP_COMPANY_ID"),
            os.environ.get("PAPERCLIP_RUN_ID"),
        )
        return do_restore(client, args)
    client = PaperclipClient(
        os.environ.get("PAPERCLIP_API_URL"),
        os.environ.get("PAPERCLIP_API_KEY"),
        os.environ.get("PAPERCLIP_COMPANY_ID"),
        os.environ.get("PAPERCLIP_RUN_ID"),
    )
    if not args.apply:
        return do_dry_run(client, args)
    return do_apply(client, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReconcileError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
