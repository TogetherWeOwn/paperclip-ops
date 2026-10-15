#!/usr/bin/env python3
"""Build a watchdog.assignment-evidence-input.v1 bundle from assignment_evidence.sql rows.

Reads the JSON lines the SQL prints and writes two new files (mode 0600, never
overwritten): the bundle and an optional report with the ids that matched each
focus scope. No database, no network, no credential.

Exit 0: all three sources complete. Exit 1: bundle written, a source incomplete.
Exit 2: usage or output error. Anything that cannot be decided is left out of the
bundle, so watchdog/detectors.py reads it as unknown.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from watchdog.assignment_contract import INPUT_SCHEMA  # noqa: E402

FOCUS_IDENTIFIER = "case_58fddbd4"
FOCUS_KEY = "focus-membership"
ROW_KINDS = ("focusDocument", "project", "issue")
LIFECYCLE_OPEN = frozenset({"todo", "in_progress", "in_review", "blocked"})
LIFECYCLE_CLOSED = frozenset({"done", "cancelled"})
BACKLOG = "backlog"
MAX_CHAIN_HOPS = 64
MEMBERSHIP_ROW = re.compile(
    r"^\|\s*([PR])\s*\|\s*`([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`\s*\|")
MEMBERSHIP_LOOKALIKE = re.compile(
    r"^\|\s*[A-Za-z]\s*\||^\|.*`[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}`")


class Unreadable(ValueError):
    pass


class FocusPolicy:
    def __init__(self, revision_id, projects, roots):
        self.revision_id = revision_id
        self.ref = f"issue-doc:{FOCUS_IDENTIFIER}/{FOCUS_KEY}#{revision_id}"
        self.projects = projects
        self.roots = roots


def _reject_constant(value):
    raise ValueError(f"non-standard JSON constant: {value}")


def read_rows(lines):
    rows = {kind: [] for kind in ROW_KINDS}
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text:
            continue
        try:
            doc = json.loads(text, parse_constant=_reject_constant)
        except ValueError as exc:
            raise Unreadable(f"line {number} is not JSON") from exc
        kind = doc.get("kind") if isinstance(doc, dict) else None
        if kind not in rows:
            raise Unreadable(f"line {number} has an unknown row kind")
        rows[kind].append(doc)
    return rows


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.search(r"[+-]\d{2}$", text):
        text += ":00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def _iso(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def focus_policy(rows, projects_by_id, issues_by_id):
    """Return (policy or None, problems). Problems name a reason, never a value."""
    docs = rows["focusDocument"]
    if len(docs) != 1:
        return None, ["focus document did not resolve"]
    doc = docs[0]
    revision_id, body = doc.get("revisionId"), doc.get("body")
    if not isinstance(revision_id, str) or not revision_id or not isinstance(body, str):
        return None, ["focus revision unreadable"]
    projects, roots, lookalikes = set(), set(), 0
    for line in body.splitlines():
        text = line.strip()
        match = MEMBERSHIP_ROW.match(text)
        if match:
            (projects if match.group(1) == "P" else roots).add(match.group(2))
        elif MEMBERSHIP_LOOKALIKE.match(text):
            lookalikes += 1
    problems = []
    if lookalikes:
        problems.append(f"{lookalikes} membership row(s) do not parse")
    if not projects or not roots:
        problems.append("membership lists no project or no root")
    unresolved_projects = projects - set(projects_by_id)
    if unresolved_projects:
        problems.append(f"{len(unresolved_projects)} listed project(s) do not resolve")
    unresolved_roots = roots - set(issues_by_id)
    if unresolved_roots:
        problems.append(f"{len(unresolved_roots)} listed root(s) do not resolve")
    return FocusPolicy(revision_id, projects, roots), problems


def root_chain(issue_id, issues_by_id):
    """Ids from issue_id up to its top, or None when the chain cannot be read."""
    chain, seen, current = [], set(), issue_id
    for _ in range(MAX_CHAIN_HOPS + 1):
        if current in seen or current not in issues_by_id:
            return None
        seen.add(current)
        chain.append(current)
        parent = issues_by_id[current].get("parentId")
        if parent is None:
            return chain
        current = parent
    return None


def lifecycle(issue):
    status = issue.get("status")
    assigned = bool(issue.get("assigneeAgentId") or issue.get("assigneeUserId"))
    if status in LIFECYCLE_OPEN:
        return True
    if status == BACKLOG:
        return assigned
    if status in LIFECYCLE_CLOSED:
        return False
    return None


def scopes(issue, policy, chain):
    return {
        "project": issue.get("projectId") in policy.projects,
        "root": chain is not None and any(node in policy.roots for node in chain),
        "review": issue.get("readyPullRequestId") is not None,
    }


def in_focus(issue, policy, chain):
    """True/False, or None when the lifecycle status is not one the rule knows."""
    if issue.get("hiddenAt") is not None:
        return False
    life = lifecycle(issue)
    if life is None:
        return None
    return bool(life and any(scopes(issue, policy, chain).values()))


def backlog_minutes(issue, now):
    """(minutes, ref) from the status history, or None when the history cannot say.

    Cards not in backlog are 0 minutes whatever their history says, so no
    history is read for them: checkout logs only issue.checked_out and assignee
    changes bump status_version, neither leaving a status move behind. Backlog
    cards with no logged move read since created_at for the same reason;
    status_version is never the "never moved" test.
    """
    status = issue.get("status")
    if status != BACKLOG:
        return 0.0, f"issue:{issue.get('id')}#status"
    if issue.get("lastMoveId") is not None:
        entered = parse_time(issue.get("lastMoveAt"))
        if entered is None or issue.get("lastMoveTo") != status:
            return None
        ref = f"activity_log:{issue['lastMoveId']}"
    else:
        entered = parse_time(issue.get("createdAt"))
        if entered is None:
            return None
        ref = f"issue:{issue.get('id')}#created"
    age = (now - entered).total_seconds() / 60
    if age < 0:
        return None
    return round(age, 2), ref


def build(rows, now, sql_sha):
    """Return (bundle, report). rows=None means the input was unreadable."""
    issues_list = rows["issue"] if rows is not None else []
    projects_by_id = {p["id"]: p for p in (rows or {}).get("project", [])
                      if isinstance(p.get("id"), str)}
    issues_by_id = {i["id"]: i for i in issues_list if isinstance(i.get("id"), str)}
    issues_ok = (rows is not None and bool(issues_list)
                 and len(issues_by_id) == len(issues_list))
    issues_ref = f"host-sql:assignment_evidence.sql@{sql_sha}#issues"
    reviews_ref = f"host-sql:assignment_evidence.sql@{sql_sha}#reviews"

    policy, focus_problems = (None, ["input unreadable"]) if rows is None \
        else focus_policy(rows, projects_by_id, issues_by_id)
    chains = {iid: root_chain(iid, issues_by_id) for iid in issues_by_id}
    chain_errors = sum(1 for chain in chains.values() if chain is None)
    if chain_errors:
        focus_problems.append(f"{chain_errors} parent chain(s) cannot be read")
    focus_ok = policy is not None and not focus_problems

    out_rows = []
    counts = {"issues": len(issues_list), "hidden": 0, "focused": 0,
              "lifecycleUnknown": 0, "backlogUnmeasured": 0}
    scope_ids = {"project": [], "root": [], "review": []}
    for issue in issues_list if issues_ok else []:
        issue_id = issue["id"]
        row = {"id": issue_id, "identifier": issue.get("identifier")}
        provenance = {}
        if issue.get("hiddenAt") is not None:
            counts["hidden"] += 1
        focus = in_focus(issue, policy, chains.get(issue_id)) if focus_ok else None
        if focus is None and focus_ok:
            counts["lifecycleUnknown"] += 1
        if focus is not None:
            row["inFocus"] = focus
            provenance["inFocus"] = {"source": "focus-membership", "ref": policy.ref}
        if focus is True:
            counts["focused"] += 1
            matched = scopes(issue, policy, chains.get(issue_id))
            for name, hit in matched.items():
                if hit:
                    scope_ids[name].append(issue_id)
            measured = backlog_minutes(issue, now)
            if measured is None:
                counts["backlogUnmeasured"] += 1
            else:
                row["inBacklogMin"] = measured[0]
                provenance["inBacklogMin"] = {"source": "status-history", "ref": measured[1]}
        agent, user = issue.get("assigneeAgentId"), issue.get("assigneeUserId")
        if agent or not user:
            row["assigneeAgentId"] = agent or None
        ready = issue.get("readyPullRequestId")
        if ready is not None:
            row["isReview"] = True
            provenance["isReview"] = {"source": "review-request",
                                      "ref": f"issue-work-product:{ready}"}
        else:
            row["isReview"] = False
            provenance["isReview"] = {"source": "review-inventory", "ref": reviews_ref}
        row["provenance"] = provenance
        out_rows.append(row)

    sources = {
        "issues": {"complete": issues_ok, "ref": issues_ref if issues_ok else None},
        "focus": {"complete": focus_ok,
                  "ref": policy.ref if policy is not None else None},
        "reviews": {"complete": issues_ok, "ref": reviews_ref if issues_ok else None},
    }
    bundle = {"schema": INPUT_SCHEMA, "now": _iso(now), "sources": sources,
              "issues": out_rows if issues_ok else None}
    report = {
        "generatedAt": _iso(now),
        "sources": {name: source["complete"] for name, source in sources.items()},
        "focus": {"revisionId": policy.revision_id if policy is not None else None,
                  "problems": focus_problems,
                  "membership": {"projects": len(policy.projects) if policy else 0,
                                 "roots": len(policy.roots) if policy else 0}},
        "counts": dict(counts, chainErrors=chain_errors,
                       focusedByScope={k: len(v) for k, v in scope_ids.items()}),
        "scopeIds": scope_ids,
    }
    return bundle, report


def _write_new(path, text):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a watchdog assignment-evidence bundle from SQL rows; no network.")
    parser.add_argument("--rows", required=True,
                        help="JSON lines from assignment_evidence.sql, or - for stdin")
    parser.add_argument("--sql", required=True,
                        help="the SQL file the rows came from (its hash goes into the refs)")
    parser.add_argument("--bundle", required=True, help="new bundle path")
    parser.add_argument("--report", help="new report path with the ids per scope (0600)")
    parser.add_argument("--now", help="snapshot clock, ISO 8601 (default: wall clock, UTC)")
    args = parser.parse_args(argv)

    now = parse_time(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    if now is None:
        print("watchdog-assignment-bundle: --now is not an ISO 8601 time", file=sys.stderr)
        return 2
    try:
        with open(args.sql, "rb") as stream:
            sql_sha = hashlib.sha256(stream.read()).hexdigest()[:12]
    except OSError:
        print("watchdog-assignment-bundle: cannot read --sql", file=sys.stderr)
        return 2

    rows = None
    try:
        if args.rows == "-":
            rows = read_rows(sys.stdin)
        else:
            with open(args.rows, encoding="utf-8") as stream:
                rows = read_rows(stream)
    except (OSError, UnicodeError, Unreadable) as exc:
        print(f"watchdog-assignment-bundle: input unreadable ({exc.__class__.__name__}); "
              "wrote incomplete evidence", file=sys.stderr)

    bundle, report = build(rows, now, sql_sha)
    try:
        _write_new(args.bundle, json.dumps(bundle, ensure_ascii=True, allow_nan=False,
                                           sort_keys=True) + "\n")
        if args.report:
            _write_new(args.report, json.dumps(report, ensure_ascii=True, allow_nan=False,
                                               sort_keys=True, indent=2) + "\n")
    except OSError:
        print("watchdog-assignment-bundle: cannot create output (exists or unwritable)",
              file=sys.stderr)
        return 2
    print(json.dumps({"sources": report["sources"], "counts": report["counts"]},
                     sort_keys=True))
    return 0 if all(report["sources"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
