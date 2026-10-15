#!/usr/bin/env python3
"""Offline snapshot builder for the platform watchdog (case_d42263d6).

The hourly judgement used to rebuild the detector snapshot by hand: only
``issues`` and ``redMains`` got a rough fill, every other section read
``snapshot missing``, and a hand-built ``inBacklogMin`` that counted a todo card
as backlog produced four false assignment findings on 2026-10-07. This module
turns API exports that are already on disk into the snapshot
``watchdog/detectors.py`` reads::

  python3 watchdog/snapshot_builder.py --out snapshot.json \\
      --issues issues.json --agents agents.json --runs heartbeat-runs.json \\
      [--recovery-events events.json] [--comments newest-comments.json] \\
      [--red-mains red-mains.json --red-mains-rc 1]
  python3 watchdog/detectors.py --snapshot snapshot.json

No network, no credentials, no board writes: it opens the input files and the
one output file and nothing else. Fetching the exports is the caller's job
(docs/watchdog-snapshot-builder.md has the recipe).

THE RULE: NEVER INVENT DATA. A section is built only from evidence the exports
carry. When it cannot be built (no export, unreadable file, a row of the wrong
type, a list that hit the API cap, coverage shorter than the detector window)
the section stays ABSENT, so the detector still says ``unknown`` for it. A
partial section would read as a clean bill for the rows it dropped. The reason
goes to stderr and to the JSON report on stdout; reasons name sections, counts
and field names, never a value.

Sections and what builds them:

  issues              --issues. ``inFocus`` = priority high or critical AND
                      status todo, in_progress or in_review. ``inBacklogMin``
                      is set only for status ``backlog`` (a todo card is not in
                      backlog), measured from ``updatedAt``, which is never
                      older than the card's entry into backlog. ``isReview``
                      and ``prAuthorAgentId`` are not in an issues export, so
                      they stay unset.
  agents              --agents. Status per agent and ``owningLead`` (the
                      manager's ``urlKey``). No export carries ``errorSince``,
                      so an agent in ``error`` stays unaged and the detector
                      says ``unknown`` for it, not "healthy".
  runEvents           --runs (plus optional --recovery-events). Accepts the
                      capped company-wide heartbeat-runs export or complete
                      per-agent pages from the roster; a full page is used only
                      when that page proves six-hour coverage. One
                      ``dispatch_stalled_issue`` row per sweep wake in the last
                      6 hours; issue_commented events are diagnostic. selfWake
                      is omitted without --wake-comments author evidence.
  parks               --runs. Per-issue runCount6h, issueCommentedCount,
                      setupFailedCount (errorCode=setup_failed), and
                      missingDispositionCount (issue_disposition_repair wakes).
                      selfWakeCount is included only when --wake-comments
                      attributes every comment wake to its exported author.
  assignedInProgress  --issues, complete company-wide or per-agent ``--runs``,
                      --comments. Live/queued rows need issueId context so they
                      can be attributed without trusting the non-exclusive issue
                      run route.
  agentRuns           --runs. Newest succeeded ``finishedAt`` per agent; full
                      company or per-agent pages cannot prove all-time completion
                      history.
  redMains            --red-mains and --red-mains-rc, the output and exit code
                      of red_main_poll.sh. Only 0 (clean) and 1 (red) mean the
                      poller saw every repo; 3 is a partial snapshot.

The existing churn detector reads only dispatch-stall events and missing-
disposition counts. Comment/setup-failure counters are diagnostic evidence,
not new detector families or auto-actions. The remaining sections (hosts, CI,
secrets, reviews, quota, supply, pending interactions, autoscaler slices, org
standard) have no export here and stay ``unknown``.

Exit 0 whenever the snapshot was written, even when sections are absent, so a
chained ``&& detectors.py`` still runs and reports the ``unknown``. Exit 2 for
a usage error or an output that could not be written.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import stat
import sys
import tempfile

try:
    from watchdog.detectors import REWOKE_WINDOW_H, parse_time
except ImportError:  # run as a script: sys.path[0] is watchdog/, not the root
    from detectors import REWOKE_WINDOW_H, parse_time

# The heartbeat-runs route clamps explicit limits at 1,000 rows. A page with
# >=1,000 rows and <=1,000 distinct runs may be capped; >1,000 distinct runs
# prove it is not.
LIST_CAP = 1000
FOCUS_PRIORITIES = frozenset({"high", "critical"})
FOCUS_STATUSES = frozenset({"todo", "in_progress", "in_review"})
# Paperclip's shared AGENT_STATUSES / HEARTBEAT_RUN_STATUSES export contract.
AGENT_STATUSES = frozenset({"active", "paused", "idle", "running", "error",
                            "pending_approval", "terminated"})
RUN_STATUSES = frozenset({"queued", "scheduled_retry", "running", "succeeded",
                         "interrupted", "failed", "cancelled", "timed_out"})
SWEEP_WAKE_REASON = "dispatch_stalled_issue"
DISPOSITION_WAKE_REASON = "issue_disposition_repair"
# Enough of a comment for `startswith("API Error: Request rejected (429)")`;
# the rest of a comment body has no business in a snapshot file.
COMMENT_KEEP_CHARS = 200
# red_main_poll.sh: 0 = every repo observed and green, 1 = every repo observed
# and at least one red. 2 (usage) and 3 (a repo unobservable) are not complete.
RED_MAIN_COMPLETE_RC = (0, 1)
# The host timer rewrites its handoff every 15 minutes (ops/case_e77a1e99); two
# missed ticks make the measurement too old to call green.
RED_MAIN_HANDOFF_SCHEMA = "watchdog.red-main-handoff.v1"
RED_MAIN_HANDOFF_MAX_AGE = datetime.timedelta(minutes=30)
RED_MAIN_HANDOFF_SKEW = datetime.timedelta(minutes=5)
RED_MAIN_HANDOFF_MAX_BYTES = 4 * 1024 * 1024
# One row per polled repo. A longer list is refused, so a forged envelope
# cannot file unbounded cards.
RED_MAIN_HANDOFF_MAX_ROWS = 32
# Card text is built from these two fields only; they must be well formed.
RED_MAIN_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
RED_MAIN_SIGNATURE = re.compile(r"[0-9a-f]{8}")

FLAGS = {"issues": "--issues", "agents": "--agents", "runs": "--runs",
         "recoveryEvents": "--recovery-events", "comments": "--comments",
         "wakeComments": "--wake-comments", "redMains": "--red-mains"}


class Unbuildable(Exception):
    """A section cannot be built from what was supplied.

    The message goes to stderr and the report, so it names sections, counts and
    field names. It never carries a value from an export.
    """


Run = collections.namedtuple(
    "Run", "id agent status created finished issue wake error comment")


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _time(value):
    moment = parse_time(value)
    try:
        return None if moment is None else moment.astimezone(datetime.timezone.utc)
    except (OverflowError, ValueError):
        return None


def _clock(moment):
    try:
        utc = moment.astimezone(datetime.timezone.utc)
        utc - datetime.timedelta(hours=REWOKE_WINDOW_H)
    except (OverflowError, ValueError):
        raise ValueError("clock cannot support a six-hour UTC window") from None
    return utc


def _iso(moment):
    return moment.astimezone(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _minutes(since, now):
    return max(0.0, (now - since).total_seconds() / 60)


def _need(inputs, name):
    if name not in inputs:
        raise Unbuildable(f"no {FLAGS[name]} supplied")
    value = inputs[name]
    if isinstance(value, Unbuildable):
        raise value
    return value


def _rows(value, label, cap=True):
    """The export as a non-empty list, or Unbuildable.

    An empty export reads as a failed fetch: a board or run list with no rows at
    all is far likelier a wrong filter than a quiet company, and an empty list
    would pass every detector.
    """
    if not isinstance(value, list):
        raise Unbuildable(f"{label} export is {type(value).__name__}, "
                          "not a list")
    if not value:
        raise Unbuildable(f"{label} export is empty; read as a failed fetch")
    if cap and len(value) >= LIST_CAP:
        raise Unbuildable(f"{label} export is a full page ({len(value)} rows) "
                          "and may be truncated; completeness is unproven")
    return value


def _unreadable(label, bad, total):
    return Unbuildable(f"{label}: {bad} of {total} rows unreadable")


def _issue_rows(inputs):
    """Validated issue rows. One unreadable row stops the whole section."""
    rows = _rows(_need(inputs, "issues"), "issues")
    bad = 0
    for row in rows:
        ok = (isinstance(row, dict)
              and _text(row.get("id")) is not None
              and _text(row.get("status")) in (
                  "todo", "in_progress", "in_review", "backlog", "blocked",
                  "done", "cancelled")
              and _text(row.get("priority")) in ("low", "medium", "high", "critical")
              and (row.get("identifier") is None
                   or _text(row.get("identifier")) is not None)
              and "assigneeAgentId" in row
              and (row.get("assigneeAgentId") is None
                   or _text(row.get("assigneeAgentId")) is not None))
        if ok and row["status"] == "backlog":
            ok = _time(row.get("updatedAt")) is not None
        bad += not ok
    if bad:
        raise _unreadable("issues", bad, len(rows))
    return rows


def _parse_run_rows(rows):
    """Validate and coalesce one heartbeat-runs response page."""
    runs, bad = [], 0
    for row in rows:
        if not isinstance(row, dict):
            bad += 1
            continue
        context = row.get("contextSnapshot")
        finished = row.get("finishedAt")
        ok = (_text(row.get("id")) is not None
              and _text(row.get("agentId")) is not None
              and _text(row.get("status")) in RUN_STATUSES
              and _time(row.get("createdAt")) is not None
              and (finished is None or _time(finished) is not None)
              and (context is None or isinstance(context, dict))
              and "errorCode" in row
              and (row["errorCode"] is None or _text(row["errorCode"])))
        if ok and context:
            ok = all(context.get(key) is None or _text(context.get(key))
                     for key in ("issueId", "wakeReason", "wakeCommentId",
                                 "commentId"))
        if not ok:
            bad += 1
            continue
        context = context or {}
        runs.append(Run(row["id"], row["agentId"], row["status"],
                        _time(row["createdAt"]), _time(finished),
                        context.get("issueId"), context.get("wakeReason"),
                        row["errorCode"],
                        context.get("wakeCommentId") or context.get("commentId")))
    if bad:
        raise _unreadable("runs", bad, len(rows))
    by_id = {}
    for run in runs:
        if run.id in by_id and run != by_id[run.id]:
            raise Unbuildable("runs: conflicting duplicate id")
        by_id[run.id] = run
    return list(by_id.values())


def _run_page_may_be_capped(rows, runs):
    return len(rows) >= LIST_CAP and len(runs) <= LIST_CAP


def _check_run_coverage(rows, runs, now, need_min, label="runs export"):
    if need_min is not None and _run_page_may_be_capped(rows, runs):
        covered = _minutes(min(run.created for run in runs), now)
        if covered < need_min:
            raise Unbuildable(
                f"{label} is a full page covering {covered:.3f} min; "
                f"this section needs {need_min:.0f}")


def _check_run_completeness(rows, runs, reason, label):
    # Duplicates count toward a response limit but cannot prove it was exceeded.
    if reason and _run_page_may_be_capped(rows, runs):
        raise Unbuildable(f"{label}: full page {reason}")


def _run_rows(inputs, now, need_min=None, require_complete=None):
    """Validated heartbeat runs, newest-first order not assumed.

    A bare array is the company-wide page. A ``byAgent`` bundle is accepted
    only when it contains one page for every validated agent. ``need_min`` is
    the history required from pages that may be capped; all-time callers reject
    those same ambiguous pages.
    """
    export = _need(inputs, "runs")
    if isinstance(export, list):
        rows = _rows(export, "runs", cap=False)
        runs = _parse_run_rows(rows)
        _check_run_completeness(rows, runs, require_complete, "runs")
        _check_run_coverage(rows, runs, now, need_min)
        return runs
    if not isinstance(export, dict) or set(export) != {"byAgent"}:
        raise Unbuildable("runs export must be an array or a byAgent bundle")
    pages = export["byAgent"]
    if not isinstance(pages, dict):
        raise Unbuildable("runs byAgent must be an object keyed by agent id")
    agents = _agent_rows(inputs)
    agent_ids = {row["id"] for row in agents}
    if set(pages) != agent_ids:
        raise Unbuildable("runs byAgent pages do not match the complete agents roster")
    runs = []
    for agent_id in sorted(agent_ids):
        rows = pages[agent_id]
        if not isinstance(rows, list):
            raise Unbuildable("runs byAgent page is not an array")
        page_runs = _parse_run_rows(rows)
        if any(run.agent != agent_id for run in page_runs):
            raise Unbuildable("runs byAgent row does not match its page agent")
        _check_run_completeness(rows, page_runs, require_complete,
                                "runs byAgent")
        _check_run_coverage(rows, page_runs, now, need_min,
                            label="runs byAgent page")
        runs.extend(page_runs)
    return runs


def _completion_run_rows(inputs, now):
    return _run_rows(inputs, now,
                     require_complete="cannot prove completion history")


def _issue_run_rows(inputs, cards, now):
    if not cards:
        return []
    runs = _run_rows(inputs, now,
                     require_complete="cannot prove live/queued counts")
    if any(run.status in ("running", "queued", "scheduled_retry")
           and run.issue is None for run in runs):
        raise Unbuildable(
            "runs: live or queued row lacks contextSnapshot.issueId")
    return runs


def _identifiers(inputs):
    """issue id -> identifier from whatever issue rows are readable."""
    rows = inputs.get("issues")
    found = {}
    for row in rows if isinstance(rows, list) else []:
        if (isinstance(row, dict) and _text(row.get("id"))
                and _text(row.get("identifier"))):
            found[row["id"]] = row["identifier"]
    return found


def build_issues(inputs, now):
    out = []
    for row in _issue_rows(inputs):
        entry = {"id": row["id"], "status": row["status"],
                 "assigneeAgentId": row.get("assigneeAgentId"),
                 "inFocus": (row["priority"] in FOCUS_PRIORITIES
                             and row["status"] in FOCUS_STATUSES)}
        if row.get("identifier"):
            entry["identifier"] = row["identifier"]
        if row["status"] == "backlog":
            entry["inBacklogMin"] = _minutes(_time(row["updatedAt"]), now)
        out.append(entry)
    return out


def _agent_rows(inputs):
    """Validated complete agent roster used to scope per-agent run pages."""
    rows = _rows(_need(inputs, "agents"), "agents")
    bad = sum(not (isinstance(row, dict)
                   and _text(row.get("id")) is not None
                   and _text(row.get("status")) in AGENT_STATUSES
                   and (row.get("reportsTo") is None
                        or _text(row.get("reportsTo")) is not None)
                   and (row.get("urlKey") is None
                        or _text(row.get("urlKey")) is not None))
              for row in rows)
    if bad:
        raise _unreadable("agents", bad, len(rows))
    if len({row["id"] for row in rows}) != len(rows):
        raise Unbuildable("agents: duplicate id in roster")
    return rows


def build_agents(inputs, now):
    rows = _agent_rows(inputs)
    lead_key = {row["id"]: row["urlKey"] for row in rows if row.get("urlKey")}
    out = []
    for row in rows:
        entry = {"agentId": row["id"], "status": row["status"]}
        if lead_key.get(row.get("reportsTo")):
            entry["owningLead"] = lead_key[row["reportsTo"]]
        out.append(entry)
    return out


def _recovery_events(inputs):
    """Sweep wakes from --recovery-events as (issueId, at, runId, identifier)."""
    if "recoveryEvents" not in inputs:
        return []
    rows = _rows(_need(inputs, "recoveryEvents"), "recovery events", cap=False)
    events, bad = [], 0
    for row in rows:
        if not isinstance(row, dict):
            bad += 1
            continue
        kind = row.get("kind") if row.get("kind") is not None \
            else row.get("reason")
        raw_at = row.get("createdAt") if row.get("createdAt") is not None \
            else row.get("at")
        at = _time(raw_at)
        ok = (_text(kind) is not None and at is not None
              and _text(row.get("issueId")) is not None
              and all(row.get(key) is None or _text(row.get(key))
                      for key in ("runId", "identifier")))
        if not ok:
            bad += 1
        elif kind == SWEEP_WAKE_REASON:
            events.append((row["issueId"], at, row.get("runId"),
                           row.get("identifier")))
    if bad:
        raise _unreadable("recovery events", bad, len(rows))
    by_id = {}
    for issue_id, at, run_id, _ in events:
        if run_id is not None:
            identity = (issue_id, at)
            if run_id in by_id and identity != by_id[run_id]:
                raise Unbuildable("recovery events: conflicting duplicate run id")
            by_id[run_id] = identity
    return events


def _self_wake(inputs, run):
    """Attribute a comment wake only to its exported comment author."""
    if run.wake != "issue_commented" or "wakeComments" not in inputs:
        return None
    comments = _need(inputs, "wakeComments")
    if not isinstance(comments, dict):
        raise Unbuildable("wake-comments must be an object keyed by comment id")
    if not run.comment or run.comment not in comments:
        return None
    comment = comments[run.comment]
    if (not isinstance(comment, dict) or "authorAgentId" not in comment
            or (comment["authorAgentId"] is not None
                and not _text(comment["authorAgentId"]))):
        raise Unbuildable("wake-comments: unreadable authorAgentId")
    return comment["authorAgentId"] == run.agent


def build_run_events(inputs, now):
    runs = _run_rows(inputs, now, REWOKE_WINDOW_H * 60)
    recovered = _recovery_events(inputs)
    known = {run.id: run for run in runs}
    for issue_id, at, run_id, _ in recovered:
        if run_id in known:
            run = known[run_id]
            if (run.issue != issue_id or run.created != at
                    or run.wake != SWEEP_WAKE_REASON):
                raise Unbuildable("recovery events: conflicting identity with runs")
    start = now - datetime.timedelta(hours=REWOKE_WINDOW_H)
    names = _identifiers(inputs)
    for issue_id, _, _, identifier in recovered:
        if identifier:
            names.setdefault(issue_id, identifier)
    out = []
    counted = set()
    for run in runs:
        if not run.issue or not start <= run.created <= now:
            continue
        if run.wake in (SWEEP_WAKE_REASON, "issue_commented"):
            entry = {"issueId": run.issue, "kind": run.wake,
                     "at": _iso(run.created), "runId": run.id}
            if run.issue in names:
                entry["identifier"] = names[run.issue]
            self_wake = _self_wake(inputs, run)
            if self_wake is not None:
                entry["selfWake"] = self_wake
            out.append(entry)
            counted.add(run.id)
    for issue_id, at, run_id, _ in recovered:
        if start <= at <= now and (run_id is None or run_id not in counted):
            entry = {"issueId": issue_id, "kind": SWEEP_WAKE_REASON,
                     "at": _iso(at)}
            if issue_id in names:
                entry["identifier"] = names[issue_id]
            if run_id:
                entry["runId"] = run_id
                counted.add(run_id)
            out.append(entry)
    return sorted(out, key=lambda row: (_time(row["at"]), row["issueId"],
                                       row.get("runId", "")))


def build_parks(inputs, now):
    runs = _run_rows(inputs, now, REWOKE_WINDOW_H * 60)
    start = now - datetime.timedelta(hours=REWOKE_WINDOW_H)
    names = _identifiers(inputs)
    by_issue = collections.defaultdict(list)
    for run in runs:
        if run.issue and start <= run.created <= now:
            by_issue[run.issue].append(run)
    out = []
    for issue_id, issue_runs in sorted(by_issue.items()):
        comment_runs = [r for r in issue_runs if r.wake == "issue_commented"]
        self_wakes = [_self_wake(inputs, r) for r in comment_runs]
        entry = {"issueId": issue_id, "runCount6h": len(issue_runs),
                 "missingDispositionCount": sum(
                     r.wake == DISPOSITION_WAKE_REASON for r in issue_runs),
                 "issueCommentedCount": len(comment_runs),
                 "setupFailedCount": sum(
                     r.error == "setup_failed" for r in issue_runs)}
        if all(value is not None for value in self_wakes):
            entry["selfWakeCount"] = sum(self_wakes)
        if issue_id in names:
            entry["identifier"] = names[issue_id]
        out.append(entry)
    return out


def _comment(value):
    """A newest-comment mapping value as {createdAt, body}, None, or Unbuildable."""
    if value is None:
        return None
    if (isinstance(value, dict) and _time(value.get("createdAt"))
            and isinstance(value.get("body"), str)):
        return {"createdAt": value["createdAt"],
                "body": value["body"].lstrip()[:COMMENT_KEEP_CHARS]}
    raise Unbuildable("comments: a newest comment is unreadable")


def build_assigned_in_progress(inputs, now):
    issues = _issue_rows(inputs)
    comments = _need(inputs, "comments")
    if not isinstance(comments, dict):
        raise Unbuildable(f"comments export is {type(comments).__name__}, "
                          "not an object keyed by issue id")
    cards = [row for row in issues
             if row["status"] == "in_progress" and row.get("assigneeAgentId")]
    missing = sum(row["id"] not in comments for row in cards)
    if missing:
        raise Unbuildable(f"comments: {missing} of {len(cards)} in_progress "
                          "cards have no newest-comment entry")
    runs = _issue_run_rows(inputs, cards, now)
    live = collections.Counter(r.issue for r in runs
                               if r.status == "running" and r.created <= now)
    queued = collections.Counter(
        r.issue for r in runs
        if r.status in ("queued", "scheduled_retry") and r.created <= now)
    out = []
    for row in cards:
        active = row.get("activeRun")
        if active is not None and (not isinstance(active, dict)
                                   or _text(active.get("status")) not in RUN_STATUSES):
            raise Unbuildable("issues: activeRun status unreadable")
        active = active.get("status") if isinstance(active, dict) else None
        entry = {"issueId": row["id"], "status": "in_progress",
                 "assigneeAgentId": row["assigneeAgentId"],
                 "liveRunCount": max(live[row["id"]], int(active == "running")),
                 "queuedRunCount": max(
                     queued[row["id"]],
                     int(active in ("queued", "scheduled_retry")))}
        if row.get("identifier"):
            entry["identifier"] = row["identifier"]
        # A continuation field the export lacks or garbles is left out: the
        # detector then says `unknown` for a card that needs it, never a guess.
        attention = row.get("blockerAttention")
        blockers = attention.get("unresolvedBlockerCount") \
            if isinstance(attention, dict) else None
        if isinstance(blockers, int) and not isinstance(blockers, bool) \
                and blockers >= 0:
            entry["unresolvedBlockerCount"] = blockers
        if "monitorNextCheckAt" in row and (
                row["monitorNextCheckAt"] is None
                or _time(row["monitorNextCheckAt"]) is not None):
            entry["monitorNextCheckAt"] = row["monitorNextCheckAt"]
        if "activeRecoveryAction" in row:
            recovery = row["activeRecoveryAction"]
            if recovery is None:
                entry["activeRecoveryAction"] = None
            elif isinstance(recovery, dict) and recovery:
                entry["activeRecoveryAction"] = True
        newest = _comment(comments[row["id"]])
        if newest is not None:
            entry["newestComment"] = newest
        out.append(entry)
    return out


def build_agent_runs(inputs, now):
    runs = _completion_run_rows(inputs, now)
    latest = {run.agent: None for run in runs}
    for run in runs:
        if (run.status == "succeeded" and run.finished is not None
                and run.created <= run.finished <= now) and (
                latest[run.agent] is None or run.finished > latest[run.agent]):
            latest[run.agent] = run.finished
    issues = inputs.get("issues")
    for row in issues if isinstance(issues, list) else []:
        if (isinstance(row, dict) and row.get("status") == "in_progress"
                and _text(row.get("assigneeAgentId"))):
            latest.setdefault(row["assigneeAgentId"], None)
    return [{"agentId": agent,
             "lastSucceededAt": None if when is None else _iso(when)}
            for agent, when in sorted(latest.items())]


def _fresh_red_main_handoff(measured, now):
    age = now - measured
    if age < -RED_MAIN_HANDOFF_SKEW:
        raise Unbuildable("red-main handoff is dated after this firing")
    if age > RED_MAIN_HANDOFF_MAX_AGE:
        raise Unbuildable(
            f"red-main handoff is {int(age.total_seconds() // 60)} min old; "
            f"the limit is {int(RED_MAIN_HANDOFF_MAX_AGE.total_seconds() // 60)} min")


def _red_main_row_ok(row):
    repo, signature = row.get("repo"), row.get("signature")
    return (isinstance(repo, str) and RED_MAIN_REPO.fullmatch(repo) is not None
            and isinstance(signature, str)
            and RED_MAIN_SIGNATURE.fullmatch(signature) is not None
            and isinstance(row.get("incidentExists"), bool))


def build_red_mains(inputs, now):
    payload = _need(inputs, "redMains")
    code = inputs.get("redMainsRc")
    if type(code) is not int:
        raise Unbuildable("red_main_poll.sh exit code missing or unreadable")
    if code not in RED_MAIN_COMPLETE_RC:
        raise Unbuildable(f"red_main_poll.sh exited {code}; only 0 (clean) "
                          "and 1 (red) mean every repo was observed")
    rows = payload.get("redMains") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise Unbuildable("red-mains export has no redMains list")
    if len(rows) > RED_MAIN_HANDOFF_MAX_ROWS:
        raise Unbuildable(f"red-mains export has more than "
                          f"{RED_MAIN_HANDOFF_MAX_ROWS} rows")
    bad = sum(not (isinstance(row, dict) and _red_main_row_ok(row))
              for row in rows)
    if bad:
        raise _unreadable("redMains", bad, len(rows))
    if bool(rows) != (code == 1):
        raise Unbuildable(f"red_main_poll.sh exit {code} disagrees with "
                          f"{len(rows)} entries")
    handoff = inputs.get("redMainsHandoff")
    if handoff is not None:
        _fresh_red_main_handoff(handoff["measuredAt"], now)
    out = []
    for row in rows:
        entry = {key: row[key] for key in ("repo", "signature",
                                           "incidentExists")}
        since = _time(row.get("since"))
        if since is not None:
            entry["since"] = _iso(since)
        out.append(entry)
    return out


SECTIONS = (
    ("issues", build_issues),
    ("agents", build_agents),
    ("runEvents", build_run_events),
    ("parks", build_parks),
    ("assignedInProgress", build_assigned_in_progress),
    ("agentRuns", build_agent_runs),
    ("redMains", build_red_mains),
)


def build_snapshot(inputs, now):
    """Return ``(snapshot, absent)``; pure, no I/O, ``inputs`` is not changed.

    ``inputs`` maps ``issues``/``agents``/``runs``/``recoveryEvents``/
    ``comments``/``redMains`` to parsed exports (or an ``Unbuildable`` for one
    that could not be read) and ``redMainsRc`` to the poller's exit code.
    ``absent`` maps each section that could not be built to its reason.
    """
    now = _clock(now)
    snapshot = {"now": _iso(now)}
    absent = {}
    for name, build in SECTIONS:
        try:
            snapshot[name] = build(inputs, now)
        except Unbuildable as exc:
            absent[name] = str(exc)
    handoff = inputs.get("redMainsHandoff")
    if handoff is not None and "redMains" in snapshot:
        snapshot["redMainsSource"] = {
            "source": "red-main-handoff",
            "measuredAt": _iso(handoff["measuredAt"]),
            "ref": handoff["ref"]}
    return snapshot, absent


def _load(flag, path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError and UnicodeDecodeError. The message
        # is dropped: it can carry the file's path or bytes.
        return Unbuildable(f"cannot read {flag}: {type(exc).__name__}")


def _reject_constant(value):
    raise ValueError(f"non-standard JSON constant: {value}")


def _read_red_main_handoff(path):
    # O_NONBLOCK keeps a writer-less FIFO from blocking; S_ISREG refuses it.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        return handle.read(RED_MAIN_HANDOFF_MAX_BYTES + 1)


def _load_red_main_handoff(path):
    """Return ``(redMains, exitCode, handoff)`` or an ``Unbuildable``."""
    try:
        raw = _read_red_main_handoff(path)
        if len(raw) > RED_MAIN_HANDOFF_MAX_BYTES:
            return Unbuildable("cannot read --red-mains-handoff: file too large")
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError) as exc:
        # The message is dropped: it can carry the file's path or bytes.
        return Unbuildable(
            f"cannot read --red-mains-handoff: {type(exc).__name__}")
    if not isinstance(doc, dict) or doc.get("schema") != RED_MAIN_HANDOFF_SCHEMA:
        return Unbuildable(
            f"red-main handoff is not a {RED_MAIN_HANDOFF_SCHEMA} envelope")
    measured = _time(doc.get("measuredAt"))
    if measured is None:
        return Unbuildable("red-main handoff has no readable measuredAt")
    code = doc.get("exitCode")
    if type(code) is not int:
        return Unbuildable("red-main handoff exitCode missing or unreadable")
    rows = doc.get("redMains")
    if not isinstance(rows, list):
        return Unbuildable("red-main handoff has no redMains list")
    digest = hashlib.sha256(raw).hexdigest()
    return ({"redMains": rows}, code,
            {"measuredAt": measured, "ref": f"sha256:{digest}"})


def _write(path, snapshot):
    directory = os.path.dirname(os.path.abspath(path))
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".snapshot-",
        suffix=".tmp", delete=False)
    try:
        with handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True,
                      allow_nan=False)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the platform watchdog snapshot from API exports on "
                    "disk: offline, read-only, no credentials. A section that "
                    "cannot be built stays absent so the detector says unknown.")
    parser.add_argument("--out", required=True, help="snapshot JSON to write")
    parser.add_argument("--now", help="ISO time base (default: the clock)")
    parser.add_argument("--issues", help="GET issues?limit=1000&status=backlog,todo,"
                                         "in_progress,in_review")
    parser.add_argument("--agents", help="GET agents")
    parser.add_argument(
        "--runs", help="normalized company-wide list or complete byAgent bundle")
    parser.add_argument("--recovery-events", help="sweep recovery events "
                        "(issueId, kind or reason, createdAt or at, runId?)")
    parser.add_argument("--comments", help="JSON object: in_progress issue id "
                        "-> newest comment {createdAt, body} or null")
    parser.add_argument("--wake-comments", help="optional JSON object: triggering "
                        "comment id -> {authorAgentId} for self-wake attribution")
    parser.add_argument("--red-mains", help="stdout of red_main_poll.sh "
                                            "snapshot")
    parser.add_argument("--red-mains-rc", type=int,
                        help="exit code of that red_main_poll.sh run")
    parser.add_argument("--red-mains-handoff",
                        help="red-main handoff envelope from the host timer "
                             "(ops/case_e77a1e99); replaces --red-mains and "
                             "--red-mains-rc, and is unknown past 30 min")
    args = parser.parse_args(argv)
    if (args.red_mains is None) != (args.red_mains_rc is None):
        parser.error("--red-mains and --red-mains-rc go together: the poller's "
                     "exit code says whether it saw every repo")
    if args.red_mains_handoff is not None and (
            args.red_mains is not None or args.red_mains_rc is not None):
        parser.error("--red-mains-handoff replaces --red-mains and "
                     "--red-mains-rc")
    if args.now is not None:
        now = _time(args.now)
        if now is None:
            parser.error("--now is not a supported UTC ISO time")
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
    try:
        now = _clock(now)
    except ValueError:
        parser.error("--now cannot support a six-hour UTC window")
    inputs = {}
    for name, path in (("issues", args.issues), ("agents", args.agents),
                       ("runs", args.runs),
                       ("recoveryEvents", args.recovery_events),
                       ("comments", args.comments),
                       ("wakeComments", args.wake_comments),
                       ("redMains", args.red_mains)):
        if path is not None:
            inputs[name] = _load(FLAGS[name], path)
    if args.red_mains_rc is not None:
        inputs["redMainsRc"] = args.red_mains_rc
    if args.red_mains_handoff is not None:
        loaded = _load_red_main_handoff(args.red_mains_handoff)
        if isinstance(loaded, Unbuildable):
            inputs["redMains"] = loaded
        else:
            payload, code, handoff = loaded
            inputs["redMains"] = payload
            inputs["redMainsRc"] = code
            inputs["redMainsHandoff"] = handoff
    snapshot, absent = build_snapshot(inputs, now)
    try:
        _write(args.out, snapshot)
    except OSError as exc:
        print(f"snapshot_builder: cannot write snapshot: "
              f"{type(exc).__name__}", file=sys.stderr)
        return 2
    for section, reason in sorted(absent.items()):
        print(f"snapshot_builder: absent {section}: {reason}", file=sys.stderr)
    built = sorted(name for name, _ in SECTIONS if name in snapshot)
    print(json.dumps({"out": args.out, "built": built, "absent": absent},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
