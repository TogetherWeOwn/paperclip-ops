#!/usr/bin/env python3
"""Platform watchdog detectors (case_311c4d5b, phase 1: read-only, propose-only).

Reads JSON snapshots, prints JSONL. No network, no credentials, no writes.
Findings (``type: finding``) route to owners and the single CEO hourly
routine. Proposals (``type: proposal``) use the recovery-writer schema from
case_db409e65; phase 1 emits ``mutation: none`` diagnostics only, so the writer
decides nothing and applies nothing. Auto-actions enable one detector at a
time on follow-up cards.

Snapshot shape (all sections optional; missing required sections emit
``unknown`` findings instead of a silent pass, and so does a section that is
not a list, a row that is not an object, or a number that cannot be read:
see ``watchdog/sections.py``)::

  {
    "now": "2026-10-03T14:00:00Z",
    "agents": [{"agentId": "...", "status": "error", "errorSince": "...",
                "subtype": "success", "terminalResultSeen": true,
                "owningLead": "cto"}],
    "hosts": [{"host": "192.0.2.190", "diskPct": 99, "load": 9.0,
               "vcpu": 4, "sustainedMin": 10}],
    "ciJobs": [{"repo": "two-web-next", "job": "test", "runtimeMin": 44,
                "baselineMin": 20}],
    "queueAge": [{"label": "self-hosted", "ageMin": 160}],
    "assignmentEvidence": {
      "schema": "watchdog.assignment-evidence.v1", "complete": true,
      "sources": {"issues": {"complete": true, "ref": "issues-export:sha"},
                  "focus": {"complete": true, "ref": "focus-export:sha"},
                  "reviews": {"complete": true, "ref": "reviews-export:sha"}}},
    "issues": [{"id": "uuid", "identifier": "case_8c5fe4cc",
                "assigneeAgentId": null, "inFocus": true, "inBacklogMin": 90,
                "isReview": false, "prAuthorAgentId": null,
                "provenance": {
                  "inFocus": {"source": "focus-membership", "ref": "focus:case_8c5fe4cc"},
                  "inBacklogMin": {"source": "status-history", "ref": "history:case_8c5fe4cc"},
                  "isReview": {"source": "review-inventory", "ref": "reviews:complete"}}}],
    "runEvents": [{"issueId": "uuid", "identifier": "case_8c5fe4cc",
                   "kind": "dispatch_stalled_issue",
                   "at": "2026-10-03T13:00:00Z"}],
    "parks": [{"issueId": "uuid", "identifier": "case_8c5fe4cc",
               "missingDispositionCount": 3}],
    "redMains": [{"repo": "paperclip-ops", "signature": "ci-fail:abc",
                  "since": "...", "incidentExists": false}],
    "secretHits": [{"source": "run-log:<id>", "patternName": "ghp_*",
                    "valueLen": 40, "at": "..."}],
    "reviews": [{"issueId": "uuid", "identifier": "case_8c5fe4cc", "status": "in_review",
                 "isReview": true,
                 "assigneeAgentId": "agent-x", "assigneeUserId": null,
                 "prAuthorAgentId": "agent-y",
                 "prAuthorProvenance": {"source": "review-request",
                                        "ref": "issue-comment:uuid"},
                 "prUrl": "https://github.com/o/r/pull/1",
                 "assignedSince": "2026-10-03T13:00:00Z",
                 "updatedAt": "2026-10-03T13:00:00Z"}],
    "openProposalSignatures": ["case_8c5fe4cc:unassigned"],
    "quotaLanes": [{"lane": "Muse-weekly-1",
                    "hoursToReset": 24, "headroomPct": 10}],
    "pendingInteractions": [{"issueId": "uuid", "identifier": "case_8c5fe4cc",
                    "interactionId": "uuid", "kind": "request_confirmation",
                    "createdAt": "2026-10-03T13:00:00Z",
                    "resolverAgentId": "agent-id",
                    "assigneeAgentId": "agent-id",
                    "mentionedAgentId": "agent-id"}],
    "supply": {"readyNow": 0, "target": 12, "belowMin": 35.0,
               "idleAgents": 2, "censusAt": "2026-10-03T14:00:00Z"},
    "autoscalerSlices": [{"sliceId": "uuid", "identifier": "case_9ae341a0",
                    "status": "backlog", "assigneeAgentId": null,
                    "unassignedSince": "2026-10-03T11:00:00Z"}],
    "shadowEmit": {"lastDecisionAt": "2026-10-03T13:55:00Z"},
    "orgStandard": {
      "canonical": {"templateSha": "t", "agentsSha": "a",
                    "contributingSha": "c"},
      "repos": [{"repo": "two-bot-next",
                 "templateSha": "t", "agentsSha": "a",
                 "contributingSha": "c",
                 "inheritsOrgDefault": false,
                 "requiresPrLint": true, "squashOnly": true}]
    }
    "assignedInProgress": [{"issueId": "uuid", "identifier": "case_8c5fe4cc",
                    "status": "in_progress", "assigneeAgentId": "agent-id",
                    "liveRunCount": 0, "queuedRunCount": 0,
                    "unresolvedBlockerCount": 0,
                    "monitorNextCheckAt": null, "activeRecoveryAction": null,
                    "newestComment": {"createdAt": "2026-10-04T16:11:43Z",
                    "body": "API Error: Request rejected (429) ..."}}],
    "agentRuns": [{"agentId": "agent-id",
                   "lastSucceededAt": "2026-10-04T20:43:19Z"}]
  }

The ``orgStandard`` section is built by the weekly snapshot job (case_b71083fd):
it records each active repo's sha256 for the canonical bundle files
(``.github/pull_request_template.md``, ``AGENTS.md``, ``CONTRIBUTING.md``)
plus whether the repo's own rules require the conventional-title check
(``requiresPrLint``) and allow only squash merges (``squashOnly``). The org
ruleset was withdrawn (case_8843860f), so its absence is not drift. ``None`` sha
means the file is absent; ``inheritsOrgDefault`` covers absence because the
public ``TogetherWeOwn/.github`` repo now supplies the community-health
defaults to any repo without its own copy.
The detector only reports drift; it never pushes.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import sys
import time

try:
    from watchdog.assignment_contract import (
        has_source_ref, valid_coverage, valid_provenance,
    )
    from watchdog.sections import SectionRead, read_float, read_int
except ImportError:  # run as a script: sys.path[0] is watchdog/, not the root
    from assignment_contract import (
        has_source_ref, valid_coverage, valid_provenance,
    )
    from sections import SectionRead, read_float, read_int

PHASE = "phase-1-read-only"
CEO_ROUTE = "ceo-hourly-routine"
CEO_DESK_ROUTE = "ceo-desk"  # CEO desk card case_d36e8c4a decides or asks the operator.

# Proposal vocabulary, not write authority: includes writer diagnostics and "none".
# verification/platform-watchdog-gate.sh checks the current candidate's boundaries.
WRITER_COMPATIBLE_MUTATIONS = frozenset({
    "none",
    "create_pause_hold",
    "assign_blocker_owner",
    "restore_blocked",
    "operator_decision",
    "clear_pin",
})

REQUIRED_HOSTS = ("192.0.2.190",)  # Coolify CI host must be present.

AGENT_ERROR_MIN = 10
DISK_PCT = 85
DISK_PCT_CRITICAL = 95
LOAD_X_VCPU = 1.5
LOAD_SUSTAINED_MIN = 5
CI_RUNTIME_X = 2.0
QUEUE_WARN_MIN = 60
QUEUE_HIGH_MIN = 150
BACKLOG_MIN = 60
SHADOW_GAP_MIN = 30
QUOTA_EXPIRY_HOURS = 48
QUOTA_HEADROOM_PCT = 25
REWOKE_COUNT = 3
REWOKE_WINDOW_H = 6
PARK_COUNT = 2
# Review cards beyond this age with no valid owner fire the stale-review
# detector. 60 minutes matches the assignment detector's backlog threshold
# (BACKLOG_MIN) so the two agree on what "stale" means; the wrong-owner
# shapes share the same age gate rather than a second clock.
STALE_REVIEW_MIN = 60
# Signatures are "<identifier>:<kind>"; the writer's decision fingerprint
# covers mutation text too, so a kind that stops being true must stop being
# proposed. Renaming a kind therefore changes the dedupe key on purpose.
STALE_REVIEW_KIND_UNASSIGNED = "unassigned"
STALE_REVIEW_KIND_AUTHOR = "author-owned"
STALE_REVIEW_KIND_USER = "user-held"
STALE_REVIEW_KIND_AUTHOR_UNKNOWN = "author-unknown"
# Snapshot adapters must cite actual authorship, not a shared bot identity or
# the actor who registered a PR. The detector is offline and resolves nothing.
STALE_REVIEW_AUTHOR_SOURCES = frozenset({"review-request", "code-authorship"})
# Pending interactions older than this age fire the D8 detector; the alert
# age also flips the backlog metric to high and routes to the CEO desk.
PENDING_MIN = 30
PENDING_ALERT_MIN = 120
SUPPLY_FAMINE_MIN = 30
AUTOSCALER_SLICE_UNASSIGNED_MIN = 120
# A row timestamp this far past the snapshot's `now` is future-dated: clock
# skew inside the tolerance reads as age zero, beyond it the row is unknown.
FUTURE_SKEW_MIN = 5

# Where detect() takes its clock (summary field `nowSource`).
NOW_SNAPSHOT = "snapshot"
NOW_WALLCLOCK = "wallclock"
NOW_UNREADABLE = "unreadable"

# case_1017ef76: an assigned in-progress card whose newest comment is the lane's
# 429 rejection, older than this, with nothing left to continue it.
STRANDED_429_PREFIX = "API Error: Request rejected (429)"
STRANDED_429_MIN = 30
# The assignee must have a succeeded run this recent, else the lane is still
# failing and a resume comment would only add another 429.
STRANDED_LANE_OK_MIN = 15
STRANDED_429_ACTION = "one resume comment from any agent"

# case_a36d7f28: assignment-detector violation slugs. The dedupe key is
# "watchdog/assignment:<violation>:<issueId>" — stable across polls (no
# timestamps or backlog minutes in the key) so the CEO routine opens exactly
# one card per card+violation. Finding and proposal for the same violation
# share the key.
ASSIGNMENT_VIOLATION_UNASSIGNED = "unassigned-backlog"
ASSIGNMENT_VIOLATION_REVIEW_AUTHOR = "review-author"

# Threshold/owner/route table for the assignment detector (mirrors
# docs/platform-watchdog.md detector 4). Resolver-less rows (missing
# snapshot, unreadable row) route to the CEO hourly routine.
ASSIGNMENT_ROUTES = {
    ASSIGNMENT_VIOLATION_UNASSIGNED: {"owner": "director", "route": "coo"},
    ASSIGNMENT_VIOLATION_REVIEW_AUTHOR: {"owner": "director",
                                         "route": "director"},
}


def assignment_dedupe_key(violation, issue_id):
    return f"watchdog/assignment:{violation}:{issue_id}"

# case_ad1c18ec: churn-detector violation slugs. The dedupe key is
# "watchdog/churn:<violation>:<issueId>" — stable across polls (no
# timestamps or counts in the key) so the CEO routine opens exactly one
# card per card+signature. Finding and proposal for the same violation
# share the key.
CHURN_VIOLATION_REWOKE = "rewoke-burst"
CHURN_VIOLATION_MISSING_DISPOSITION = "missing-disposition"

# Threshold/owner/route table for the churn detector (mirrors
# docs/platform-watchdog.md detector 5). Judgement items always route to
# the single CEO-owned hourly routine; the owner is the card's owning lead
# when the snapshot names one, else the CEO desk.
CHURN_ROUTES = {
    CHURN_VIOLATION_REWOKE: {"owner": "ceo", "route": CEO_ROUTE},
    CHURN_VIOLATION_MISSING_DISPOSITION: {"owner": "ceo",
                                          "route": CEO_ROUTE},
}


def churn_dedupe_key(violation, issue_id):
    return f"watchdog/churn:{violation}:{issue_id}"


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment
    except ValueError:
        return None


def _iso_z(moment):
    """Format an aware datetime as the UTC ``...Z`` stamp the join parses."""
    return moment.astimezone(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def now_source(snapshot):
    """Which clock detect() ages rows against. An absent `now` keeps the
    wall-clock default; a present but unreadable one (including null) is
    never replaced by the wall clock, or a replayed snapshot would age
    differently each day."""
    if "now" not in snapshot:
        return NOW_WALLCLOCK
    if parse_time(snapshot["now"]) is None:
        return NOW_UNREADABLE
    return NOW_SNAPSHOT


def _age_min(now, moment):
    """Minutes `moment` is behind `now`; None when it is future-dated by
    more than FUTURE_SKEW_MIN, so no caller ever sees a negative age."""
    if moment - now > datetime.timedelta(minutes=FUTURE_SKEW_MIN):
        return None
    return max((now - moment).total_seconds() / 60, 0.0)


def _future_dated(detector, owner, field, count):
    return _finding(
        detector, f"{count} row(s) with future-dated {field}", "unknown",
        owner, CEO_ROUTE,
        {"field": field, "count": count, "toleranceMin": FUTURE_SKEW_MIN},
        f"fix the {field} timestamps or the snapshot now; "
        "a future-dated row cannot be aged and is not counted healthy")


def _finding(detector, reason, severity, owner, route, evidence,
             suggested, issue_id=None, identifier=None, dedupe_key=None):
    record = {
        "type": "finding",
        "detector": f"watchdog/{detector}",
        "reason": reason,
        "severity": severity,
        "owner": owner,
        "route": route,
        "evidence": evidence,
        "suggestedAction": suggested,
        "phase": PHASE,
    }
    if issue_id is not None:
        record["issueId"] = issue_id
    if identifier is not None:
        record["identifier"] = identifier
    if dedupe_key is not None:
        record["dedupeKey"] = dedupe_key
    return record


def _proposal(detector, reason, issue_id, identifier, note,
              before=None, after=None, dedupe_key=None):
    record = {
        "type": "proposal",
        "detector": f"watchdog/{detector}",
        "reason": reason,
        "mutation": "none",
        "issueId": issue_id,
        "identifier": identifier,
        "before": before or {},
        "after": after or {},
        "note": note,
        "phase": PHASE,
    }
    if dedupe_key is not None:
        record["dedupeKey"] = dedupe_key
    return record


def detect_agent_error(now, agents):
    out = []
    if agents is None:
        return [_finding("agent_error", "agents snapshot missing",
                         "unknown", "cto", CEO_ROUTE,
                         {"section": "agents"},
                         "supply the agents snapshot; no verdict without it")]
    future = 0
    rows = SectionRead("agents", agents)
    for agent in rows:
        if agent.get("status") != "error":
            continue
        since = parse_time(agent.get("errorSince"))
        if since is None:
            out.append(_finding(
                "agent_error", "agent in error with unreadable errorSince",
                "unknown", str(agent.get("owningLead") or "owning-lead"),
                CEO_ROUTE, {"agentId": agent.get("agentId")},
                "fix the errorSince timestamp; cannot age the error"))
            continue
        if now is None:
            continue
        age_min = _age_min(now, since)
        if age_min is None:
            future += 1
            continue
        if age_min <= AGENT_ERROR_MIN:
            continue
        benign = (agent.get("subtype") == "success"
                  and agent.get("terminalResultSeen") is True)
        if benign:
            out.append(_finding(
                "agent_error", "benign error signature past 10m",
                "info", str(agent.get("owningLead") or "owning-lead"),
                CEO_ROUTE,
                {"agentId": agent.get("agentId"), "ageMin": round(age_min, 1),
                 "subtype": "success", "terminalResultSeen": True},
                "phase-2 candidate: auto-reset to idle; phase 1 takes no action"))
        else:
            out.append(_finding(
                "agent_error", "non-benign agent error past 10m",
                "high", str(agent.get("owningLead") or "owning-lead"),
                str(agent.get("owningLead") or "owning-lead"),
                {"agentId": agent.get("agentId"), "ageMin": round(age_min, 1),
                 "subtype": agent.get("subtype")},
                "owning lead triages; no auto-reset outside the benign signature"))
    if future:
        out.append(_future_dated("agent_error", "cto", "errorSince", future))
    out.extend(rows.unknown(_finding, "agent_error", "cto", CEO_ROUTE))
    return out


def detect_host_health(hosts):
    out = []
    if hosts is None:
        return [_finding("host_health", "hosts snapshot missing", "unknown",
                         "operator", CEO_ROUTE, {"section": "hosts"},
                         "supply the Beszel/Gatus snapshot; no verdict without it")]
    rows = SectionRead("hosts", hosts)
    seen = set()
    unreadable = {}  # host name -> health fields that could not be read
    for host in rows:
        name = str(host.get("host") or "?")
        seen.add(name)
        disk = read_float(host.get("diskPct"))
        if disk is not None and disk >= DISK_PCT:
            out.append(_finding(
                "host_health", "disk at or above 85%",
                "critical" if disk >= DISK_PCT_CRITICAL else "high",
                "operator", "operator" if disk < DISK_PCT_CRITICAL else "page-operator",
                {"host": name, "diskPct": disk},
                "reclaim disk now; production apps share the Coolify host"))
        load = read_float(host.get("load"))
        vcpu = read_float(host.get("vcpu"))
        sustained = read_float(host.get("sustainedMin", 0))
        bad = [field for field, value in (
            ("diskPct", disk), ("load", load), ("vcpu", vcpu),
            ("sustainedMin", sustained)) if value is None]
        if vcpu is not None and vcpu <= 0:
            bad.append("vcpu")
        if bad:
            rows.flag()
            unreadable.setdefault(name, set()).update(bad)
        if (load is not None and vcpu is not None and vcpu > 0
                and sustained is not None and sustained >= LOAD_SUSTAINED_MIN
                and load > LOAD_X_VCPU * vcpu):
            out.append(_finding(
                "host_health", "load above 1.5x vCPU sustained",
                "high", "operator", "operator",
                {"host": name, "load": load, "vcpu": vcpu,
                 "sustainedMin": sustained},
                "shed load or add capacity; check GARM queue depth"))
    for required in REQUIRED_HOSTS:
        if required not in seen:
            out.append(_finding(
                "host_health", "required host missing from snapshot",
                "unknown", "operator", CEO_ROUTE,
                {"host": required},
                "snapshot must cover EVERY host including Coolify; no pass without it"))
        elif required in unreadable:
            out.append(_finding(
                "host_health", "required host with unreadable health fields",
                "unknown", "operator", CEO_ROUTE,
                {"host": required,
                 "unreadableFields": sorted(unreadable[required])},
                "fix the Beszel/Gatus reading; no pass for the Coolify host "
                "without a readable disk and load"))
    out.extend(rows.unknown(_finding, "host_health", "operator", CEO_ROUTE))
    return out


def detect_ci_health(jobs, queues):
    out = []
    if jobs is None and queues is None:
        return [_finding("ci_health", "CI snapshot missing", "unknown",
                         "devops", CEO_ROUTE,
                         {"section": "ciJobs/queueAge"},
                         "supply CI runtimes and queue ages; no verdict without them")]
    job_rows = SectionRead("ciJobs", jobs)
    for job in job_rows:
        runtime = read_float(job.get("runtimeMin"))
        baseline = read_float(job.get("baselineMin"))
        label = f"{job.get('repo')}/{job.get('job')}"
        if runtime is None or baseline is None:
            out.append(_finding(
                "ci_health", "CI job with unreadable runtime or baseline",
                "unknown", "devops", CEO_ROUTE, {"job": label},
                "fix the baseline adapter; cannot ratio without it"))
        elif baseline <= 0:
            out.append(_finding(
                "ci_health", "CI job with non-positive baseline",
                "unknown", "devops", CEO_ROUTE,
                {"job": label, "baselineMin": baseline},
                "fix the baseline; a zero baseline makes every ratio infinite"))
        elif runtime > CI_RUNTIME_X * baseline:
            out.append(_finding(
                "ci_health", "CI job runtime above 2x baseline",
                "high", "devops", "devops",
                {"job": label, "runtimeMin": runtime,
                 "baselineMin": baseline,
                 "ratio": round(runtime / baseline, 2)},
                "inspect the runner and the job log tail; file an overflow request if queued"))
    queue_rows = SectionRead("queueAge", queues)
    for queue in queue_rows:
        age = read_float(queue.get("ageMin"))
        if age is None:
            queue_rows.flag()
            continue
        if age >= QUEUE_HIGH_MIN:
            sev, route = "high", "devops"
        elif age >= QUEUE_WARN_MIN:
            sev, route = "info", CEO_ROUTE
        else:
            continue
        out.append(_finding(
            "ci_health", "runner queue age elevated",
            sev, "devops", route,
            {"label": queue.get("label"), "ageMin": age},
            "file a CI overflow request per PROD_DELIVERY rules if saturation holds"))
    out.extend(job_rows.unknown(_finding, "ci_health", "devops", CEO_ROUTE))
    out.extend(queue_rows.unknown(_finding, "ci_health", "devops", CEO_ROUTE))
    return out


def detect_assignment(issues, assignment_evidence=None):
    out = []
    if issues is None:
        return [_finding("assignment", "issues snapshot missing", "unknown",
                         "director", CEO_ROUTE, {"section": "issues"},
                         "supply source-backed assignment evidence; no verdict without it")]
    rows = SectionRead("issues", issues)
    if not valid_coverage(assignment_evidence):
        out.append(_finding(
            "assignment", "assignment evidence incomplete", "unknown",
            "director", CEO_ROUTE, {"section": "issues"},
            "capture complete issue, focus, and review sources; no clean verdict "
            "without them"))

    for issue in rows:
        focused = issue.get("inFocus")
        if not isinstance(focused, bool) or not valid_provenance(issue, "inFocus"):
            rows.flag()
            continue
        if not focused:
            continue

        issue_id = issue.get("id")
        identifier = issue.get("identifier")
        if not has_source_ref(issue_id) or not has_source_ref(identifier):
            rows.flag()
            continue

        assignee = issue.get("assigneeAgentId")
        assignee_valid = (
            "assigneeAgentId" in issue
            and (assignee is None or has_source_ref(assignee))
        )
        if not assignee_valid:
            rows.flag()

        is_review = issue.get("isReview")
        review_valid = (
            isinstance(is_review, bool)
            and valid_provenance(issue, "isReview")
        )
        if not review_valid:
            rows.flag()

        backlog_value = issue.get("inBacklogMin")
        backlog_min = read_float(backlog_value)
        backlog_valid = (
            "inBacklogMin" in issue
            and backlog_min is not None and backlog_min >= 0
        )
        if backlog_valid and backlog_value is not None:
            backlog_valid = valid_provenance(issue, "inBacklogMin")
        if not backlog_valid:
            rows.flag()

        author = issue.get("prAuthorAgentId")
        author_valid = is_review is not True or (
            "prAuthorAgentId" in issue
            and has_source_ref(author)
            and valid_provenance(issue, "prAuthorAgentId")
        )
        if not author_valid:
            rows.flag()

        if assignee_valid and (assignee is None
                               or (backlog_valid and backlog_min > BACKLOG_MIN)):
            unassigned_key = assignment_dedupe_key(
                ASSIGNMENT_VIOLATION_UNASSIGNED, issue_id)
            unassigned_route = ASSIGNMENT_ROUTES[
                ASSIGNMENT_VIOLATION_UNASSIGNED]
            out.append(_finding(
                "assignment", "in-focus card unassigned or in backlog over 1h",
                "high", unassigned_route["owner"], unassigned_route["route"],
                {"identifier": identifier,
                 "assigneeAgentId": assignee,
                 "inBacklogMin": backlog_min if backlog_valid else None},
                "allocate ready work per owner priority order",
                issue_id, identifier, unassigned_key))
            out.append(_proposal(
                "assignment", "in-focus card needs an owner",
                issue_id, identifier,
                "Phase-2 candidate: assign_blocker_owner once the intended "
                "owner is first-class; phase 1 writes nothing.",
                dedupe_key=unassigned_key))

        if (review_valid and is_review is True and author_valid
                and assignee_valid and assignee
                and assignee == author):
            review_key = assignment_dedupe_key(
                ASSIGNMENT_VIOLATION_REVIEW_AUTHOR, issue_id)
            review_route = ASSIGNMENT_ROUTES[
                ASSIGNMENT_VIOLATION_REVIEW_AUTHOR]
            out.append(_finding(
                "assignment", "review card assigned to the PR author",
                "high", review_route["owner"], review_route["route"],
                {"identifier": identifier},
                "reassign to an independent reviewer; author never reviews own PR",
                issue_id, identifier, review_key))
            out.append(_proposal(
                "assignment", "review assigned to PR author",
                issue_id, identifier,
                "Phase-2 candidate: assign_blocker_owner to an independent "
                "reviewer once an owner resolver exists; phase 1 writes nothing.",
                dedupe_key=review_key))
    out.extend(rows.unknown(_finding, "assignment", "director", CEO_ROUTE))
    return out


def detect_churn(now, events, parks):
    out = []
    if events is None and parks is None:
        return [_finding("churn", "churn snapshot missing", "unknown",
                         "ceo", CEO_ROUTE, {"section": "runEvents/parks"},
                         "supply run events and park counts; no verdict without them")]
    counts = {}
    future = 0
    event_rows = SectionRead("runEvents", events)
    for event in event_rows:
        if event.get("kind") != "dispatch_stalled_issue":
            continue
        at = parse_time(event.get("at"))
        if at is None or now is None:
            continue  # no clock to window the event against
        age_min = _age_min(now, at)
        if age_min is None:
            future += 1
            continue
        if age_min > REWOKE_WINDOW_H * 60:
            continue
        if (not isinstance(event.get("issueId"), str) or not event["issueId"]
                or not isinstance(event.get("identifier") or "?", str)):
            event_rows.flag()
            continue
        key = (event.get("issueId"), event.get("identifier") or "?")
        counts[key] = counts.get(key, 0) + 1
    for (issue_id, identifier), count in counts.items():
        if count > REWOKE_COUNT and issue_id:
            rewoke_key = churn_dedupe_key(CHURN_VIOLATION_REWOKE, issue_id)
            rewoke_route = CHURN_ROUTES[CHURN_VIOLATION_REWOKE]
            out.append(_finding(
                "churn", "card re-woken more than 3 times in 6h",
                "high", rewoke_route["owner"], rewoke_route["route"],
                {"identifier": identifier, "rewokes": count},
                "fix the stall-sweep loop guard; stop re-waking parked cards",
                issue_id, identifier, rewoke_key))
            out.append(_proposal(
                "churn", "repeated stall re-wake",
                issue_id, identifier,
                "Phase-2 candidate: restore_blocked where a live descriptor "
                "exists; phase 1 writes nothing.",
                dedupe_key=rewoke_key))
    if future:
        out.append(_future_dated("churn", "ceo", "runEvents.at", future))
    park_rows = SectionRead("parks", parks)
    for park in park_rows:
        n = read_int(park.get("missingDispositionCount", 0))
        if n is None or (n > PARK_COUNT and not park.get("issueId")):
            park_rows.flag()
            continue
        if n > PARK_COUNT and park.get("issueId"):
            park_key = churn_dedupe_key(
                CHURN_VIOLATION_MISSING_DISPOSITION, park["issueId"])
            park_route = CHURN_ROUTES[CHURN_VIOLATION_MISSING_DISPOSITION]
            out.append(_finding(
                "churn", "card parked for missing disposition more than twice",
                "high", park_route["owner"], park_route["route"],
                {"identifier": park.get("identifier"),
                 "missingDispositionCount": n},
                "set a real disposition or monitor; repeated parks need a decision",
                park.get("issueId"), park.get("identifier"), park_key))
            out.append(_proposal(
                "churn", "repeated missing-disposition park",
                park["issueId"], park.get("identifier") or "?",
                "Phase-2 candidate: restore_blocked where a live descriptor "
                "exists; phase 1 writes nothing.",
                dedupe_key=park_key))
    out.extend(event_rows.unknown(_finding, "churn", "ceo", CEO_ROUTE))
    out.extend(park_rows.unknown(_finding, "churn", "ceo", CEO_ROUTE))
    return out


def stale_review_author(card):
    """Return a source-backed code author, never a publisher/ownership guess."""
    author = card.get("prAuthorAgentId")
    provenance = card.get("prAuthorProvenance")
    if not isinstance(author, str) or not author.strip():
        return None
    if not isinstance(provenance, dict):
        return None
    source = provenance.get("source")
    reference = provenance.get("ref")
    if (not isinstance(source, str)
            or source not in STALE_REVIEW_AUTHOR_SOURCES
            or not isinstance(reference, str) or not reference.strip()):
        return None
    return author


def stale_review_kind(card):
    """Classify a review card into one stale-routing kind, or None.

    Exactly one signature per card: an unassigned card is reported as
    ``unassigned`` even when its author field would also match, because the
    unassigned route (find an owner) subsumes the author route (replace the
    owner). A card held by a human user identity reads as ``user-held``:
    owner rules route ordinary work to agents, never to the owner user, so
    a review parked on a user id needs Director routing, not a silent wait.
    Missing authorship evidence is ``author-unknown``, not independent or
    author-owned. Non-review cards, unparseable cards, and cards with a valid
    agent owner distinct from the source-backed PR author return None.
    """
    if not isinstance(card, dict) or not card.get("isReview"):
        return None
    assignee = card.get("assigneeAgentId")
    if not assignee:
        return STALE_REVIEW_KIND_UNASSIGNED
    if card.get("assigneeUserId"):
        return STALE_REVIEW_KIND_USER
    author = stale_review_author(card)
    if author is None:
        return STALE_REVIEW_KIND_AUTHOR_UNKNOWN
    if assignee == author:
        return STALE_REVIEW_KIND_AUTHOR
    return None


def stale_review_moment(card):
    """When the current ownership state began, or None.

    ``assignedSince`` is the ownership clock when the adapter supplies it;
    ``updatedAt`` is the fallback (last touch of any kind). A card with
    neither timestamp is unmeasurable, not fresh: returning None forces the
    ``unknown`` path instead of a silent pass.
    """
    moment = parse_time(card.get("assignedSince"))
    if moment is None:
        moment = parse_time(card.get("updatedAt"))
    return moment


def detect_stale_reviews(now, reviews, open_signatures=None):
    """Flag review cards with no valid owner beyond STALE_REVIEW_MIN.

    Propose-only: every proposal carries ``mutation: none`` and routes to
    the Director (``director``), who picks the independent reviewer.
    Missing author provenance is a coverage gap: propose confirmation on
    existing source/review cards, never reassignment from an identity guess.
    The detector never reassigns, never enables the recovery writer, and never
    touches a card whose signature is already in ``open_signatures`` — a
    repeat poll on the same ``<identifier>:<kind>`` emits at most an
    info-level repeat finding, never a second proposal. A card whose shape
    changed (new kind) is a new signature and fires normally.
    """
    out = []
    if reviews is None:
        return [_finding("stale_reviews", "reviews snapshot missing",
                         "unknown", "director", CEO_ROUTE,
                         {"section": "reviews"},
                         "supply the review-card snapshot; no verdict without it")]
    known = set(open_signatures or [])
    future = 0
    for card in reviews or []:
        if not isinstance(card, dict):
            continue
        identifier = card.get("identifier") or card.get("issueId") or "?"
        issue_id = card.get("issueId")
        kind = stale_review_kind(card)
        if kind is None:
            continue
        moment = stale_review_moment(card)
        if moment is None:
            out.append(_finding(
                "stale_reviews",
                "stale-review candidate with unreadable ownership clock",
                "unknown", "director", CEO_ROUTE,
                {"identifier": identifier, "kind": kind},
                "fix the assignedSince/updatedAt timestamp; cannot age "
                "the ownership state"))
            continue
        if now is None:
            continue  # snapshot_clock already reports the unreadable now.
        age = _age_min(now, moment)
        if age is None:
            future += 1
            continue
        if age <= STALE_REVIEW_MIN:
            continue
        signature = f"{identifier}:{kind}"
        evidence = {"identifier": identifier, "kind": kind,
                    "ageMin": round(age, 1)}
        if kind != STALE_REVIEW_KIND_UNASSIGNED:
            evidence["assigneeAgentId"] = card.get("assigneeAgentId")
            evidence["assigneeUserId"] = card.get("assigneeUserId")
        author = stale_review_author(card)
        evidence["prAuthorAgentId"] = author
        evidence["authorStatus"] = "known" if author is not None else "unknown"
        if author is not None:
            evidence["prAuthorProvenance"] = {
                key: card["prAuthorProvenance"][key] for key in ("source", "ref")}
        if card.get("prUrl"):
            evidence["prUrl"] = card.get("prUrl")
        if signature in known:
            out.append(_finding(
                "stale_reviews", "stale review already proposed",
                "info", "director", CEO_ROUTE, evidence,
                "proposal already open for this card and signature; "
                "no duplicate proposal"))
            continue
        if kind == STALE_REVIEW_KIND_AUTHOR_UNKNOWN:
            out.append(_finding(
                "stale_reviews", "stale review author provenance unknown",
                "unknown", "director", "director", evidence,
                "Director confirms the code author from the existing source or "
                "review request before assessing independence; no reassignment "
                "or new review card from publisher identity",
                issue_id, identifier))
            if issue_id:
                out.append(_proposal(
                    "stale_reviews", "stale review authorship confirmation",
                    issue_id, identifier,
                    "Coverage gap only: confirm authorship on existing source/"
                    "review cards; do not reassign or open another review card. "
                    "Phase 1 writes nothing.",
                    before={"signature": signature},
                    after={"signature": signature, "route": "director",
                           "authorStatus": "unknown"}))
            continue
        reasons = {
            STALE_REVIEW_KIND_UNASSIGNED:
                "stale review with no owner",
            STALE_REVIEW_KIND_AUTHOR:
                "stale review owned by the PR author",
            STALE_REVIEW_KIND_USER:
                "stale review held by a user identity",
        }
        actions = {
            STALE_REVIEW_KIND_UNASSIGNED:
                "Director assigns an independent reviewer; the author "
                "never reviews their own PR",
            STALE_REVIEW_KIND_AUTHOR:
                "Director reassigns to an independent reviewer; the "
                "author never merges or reviews their own PR",
            STALE_REVIEW_KIND_USER:
                "Director moves the review to an agent reviewer; ordinary "
                "work is never parked on the owner user",
        }
        out.append(_finding(
            "stale_reviews", reasons[kind],
            "high", "director", "director", evidence,
            actions[kind], issue_id, identifier))
        if issue_id:
            out.append(_proposal(
                "stale_reviews", f"stale review route to Director ({kind})",
                issue_id, identifier,
                "Phase-2 candidate: assign_blocker_owner to the Director-chosen "
                "independent reviewer; phase 1 writes nothing.",
                before={"signature": signature},
                after={"signature": signature, "route": "director"}))
    if future:
        out.append(_future_dated("stale_reviews", "director",
                                 "assignedSince/updatedAt", future))
    return out


def detect_pending_interactions(now, pending):
    out = []
    if pending is None:
        return [_finding("pending_interactions",
                         "pending-interactions snapshot missing",
                         "unknown", "ceo", CEO_ROUTE,
                         {"section": "pendingInteractions"},
                         "supply pending issue_thread_interactions; "
                         "no verdict without them")]
    ages = []
    future = 0
    rows = SectionRead("pendingInteractions", pending)
    for item in rows:
        created = parse_time(item.get("createdAt"))
        identifier = item.get("identifier") or item.get("issueId") or "?"
        if created is None:
            out.append(_finding(
                "pending_interactions",
                "pending interaction with unreadable createdAt",
                "unknown", "ceo", CEO_ROUTE,
                {"identifier": identifier,
                 "interactionId": item.get("interactionId")},
                "fix the createdAt timestamp; cannot age the interaction"))
            continue
        if now is None:
            continue
        age_min = _age_min(now, created)
        if age_min is None:
            future += 1
            continue
        ages.append(age_min)
        if age_min <= PENDING_MIN:
            continue
        issue_id = item.get("issueId")
        resolver = (item.get("resolverAgentId")
                    or item.get("assigneeAgentId")
                    or item.get("mentionedAgentId"))
        alert = age_min >= PENDING_ALERT_MIN
        evidence = {"identifier": identifier,
                    "interactionId": item.get("interactionId"),
                    "kind": item.get("kind"),
                    "ageMin": round(age_min, 1)}
        if resolver:
            out.append(_finding(
                "pending_interactions",
                "pending interaction older than 30m with addressed resolver",
                "high" if alert else "info",
                str(resolver), str(resolver), evidence,
                "wake the addressed resolver; over 2h also alerts "
                "the CEO desk via the backlog metric",
                issue_id, identifier))
        else:
            out.append(_finding(
                "pending_interactions",
                "pending interaction older than 30m with no clear resolver",
                "high" if alert else "info",
                "ceo", CEO_DESK_ROUTE, evidence,
                "route to the CEO desk card case_d36e8c4a, which decides "
                "or asks the operator",
                issue_id, identifier))
        if issue_id:
            out.append(_proposal(
                "pending_interactions", "stale pending interaction",
                issue_id, identifier,
                "Phase-2 candidate: wake the addressed resolver once a "
                "wake mutation exists; phase 1 writes nothing."))
    if ages:
        over_alert = sum(1 for age in ages if age >= PENDING_ALERT_MIN)
        out.append(_finding(
            "pending_interactions", "pending-interaction backlog metric",
            "high" if over_alert else "info", "ceo",
            CEO_DESK_ROUTE if over_alert else CEO_ROUTE,
            {"count": len(ages),
             "medianAgeMin": round(statistics.median(ages), 1),
             "over2h": over_alert},
            "alert: a pending interaction is over 2h; the CEO desk "
            "decides or asks the operator"
            if over_alert else
            "count and median age of pending interactions for "
            "the CEO hourly routine"))
    if future:
        out.append(_future_dated("pending_interactions", "ceo", "createdAt",
                                 future))
    out.extend(rows.unknown(_finding, "pending_interactions", "ceo",
                            CEO_ROUTE))
    return out


def detect_autoscaler_slices(now, slices):
    out = []
    if slices is None:
        return [_finding("autoscaler_slices",
                         "autoscaler-slices snapshot missing",
                         "unknown", "coo", CEO_ROUTE,
                         {"section": "autoscalerSlices"},
                         "supply the autoscaler slice assignment snapshot; "
                         "no verdict without it")]
    future = 0
    rows = SectionRead("autoscalerSlices", slices)
    for item in rows:
        identifier = (item.get("identifier") or item.get("sliceId")
                      or "?")
        if item.get("assigneeAgentId"):
            continue
        since = parse_time(item.get("unassignedSince"))
        if since is None:
            out.append(_finding(
                "autoscaler_slices",
                "autoscaler slice with unreadable unassignedSince",
                "unknown", "coo", CEO_ROUTE,
                {"identifier": identifier,
                 "sliceId": item.get("sliceId")},
                "fix the unassignedSince timestamp; cannot age the slice"))
            continue
        if now is None:
            continue
        age_min = _age_min(now, since)
        if age_min is None:
            future += 1
            continue
        if age_min <= AUTOSCALER_SLICE_UNASSIGNED_MIN:
            continue
        slice_id = item.get("sliceId")
        out.append(_finding(
            "autoscaler_slices",
            "autoscaler slice unassigned for over 2h",
            "high", "coo", CEO_ROUTE,
            {"identifier": identifier, "sliceId": slice_id,
             "status": item.get("status"),
             "ageMin": round(age_min, 1)},
            "COO allocates the slice per the case_24119081 joint-controller "
            "admission rules, or the CEO hourly routine decides; "
            "card-level backlog stays on case_a36d7f28",
            slice_id, identifier))
        if slice_id:
            out.append(_proposal(
                "autoscaler_slices",
                "unassigned autoscaler slice past 2h",
                slice_id, identifier,
                "Phase-2 candidate: assign_blocker_owner to the resolved "
                "owner once an owner resolver exists; phase 1 writes nothing."))
    if future:
        out.append(_future_dated("autoscaler_slices", "coo",
                                 "unassignedSince", future))
    out.extend(rows.unknown(_finding, "autoscaler_slices", "coo", CEO_ROUTE))
    return out


def _count(value):
    """A snapshot count as a non-negative int, else None (bool is no count)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def stranded_429_dedupe_key(issue_id, commented):
    """Stable across polls of one episode, new once a later 429 lands."""
    stamp = commented.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return f"watchdog/stranded-429:{issue_id}:{stamp}"


def _stranded_lane(agent_runs):
    """agentId -> (lastSucceededAt or None, unreadable); newest row wins.

    Only non-empty strings count as ids. A row whose agentId is anything
    else (a list, an object, a blank) is flagged unreadable instead of
    reaching a dict lookup, where an unhashable id would raise and stop
    every detector family for the cycle.
    """
    lane = {}
    for row in agent_runs:
        agent_id = row.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            agent_runs.flag()
            continue
        raw = row.get("lastSucceededAt")
        at = parse_time(raw)
        prev = lane.get(agent_id)
        if prev is None or (at is not None
                            and (prev[0] is None or at > prev[0])):
            lane[agent_id] = (at, raw is not None and at is None)
    return lane


def detect_stranded_429(now, cards, agent_runs):
    """Assigned in-progress cards whose run ended on a 429 and went nowhere.

    Propose-only. A card is flagged only when every continuation path is
    absent (no live or queued run, no future monitor, no unresolved blocker,
    no active recovery action), its newest comment is the 429 rejection and
    older than STRANDED_429_MIN, and the assignee's lane has recovered
    (a succeeded run within STRANDED_LANE_OK_MIN). While the lane still
    fails the card is silent: waking it would only add another 429. A
    missing section or a field the adapter left out or garbled is
    `unknown`, never a pass. A comment or lane stamp dated more than
    FUTURE_SKEW_MIN after `now` is one aggregate `unknown`, and an
    unreadable `now` skips the aging (snapshot_clock reports it). The
    comment body is never echoed.
    """
    out = []
    if cards is None:
        out.append(_finding(
            "stranded_429", "assignedInProgress snapshot missing",
            "unknown", "director", CEO_ROUTE,
            {"section": "assignedInProgress"},
            "supply the assignedInProgress snapshot; no verdict without it"))
    if agent_runs is None:
        out.append(_finding(
            "stranded_429", "agentRuns snapshot missing", "unknown",
            "director", CEO_ROUTE, {"section": "agentRuns"},
            "supply the agentRuns snapshot; no verdict without it"))
    if out:
        return out
    card_rows = SectionRead("assignedInProgress", cards)
    lane_rows = SectionRead("agentRuns", agent_runs)
    lane = _stranded_lane(lane_rows)
    future_comments = 0
    future_lanes = 0
    for card in card_rows:
        if (card.get("status") != "in_progress"
                or not card.get("assigneeAgentId")):
            continue
        comment = card.get("newestComment")
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str) \
                or not body.lstrip().startswith(STRANDED_429_PREFIX):
            continue
        issue_id = card.get("issueId")
        identifier = card.get("identifier") or issue_id or "?"
        assignee = card["assigneeAgentId"]
        commented = (parse_time(comment.get("createdAt"))
                     if isinstance(comment, dict) else None)
        if commented is None or not issue_id:
            out.append(_finding(
                "stranded_429",
                "429-ended card with unreadable issueId or comment time",
                "unknown", "director", CEO_ROUTE,
                {"identifier": identifier},
                "fix the issueId/newestComment.createdAt fields; cannot "
                "age the card", issue_id, identifier))
            continue
        if now is None:
            continue
        age_min = _age_min(now, commented)
        if age_min is None:
            future_comments += 1
            continue
        if age_min <= STRANDED_429_MIN:
            continue
        unreadable = []
        continued = False
        for field in ("liveRunCount", "queuedRunCount",
                      "unresolvedBlockerCount"):
            count = _count(card.get(field))
            if count is None:
                unreadable.append(field)
            elif count > 0:
                continued = True
        if "activeRecoveryAction" not in card:
            unreadable.append("activeRecoveryAction")
        elif card["activeRecoveryAction"]:
            continued = True
        if "monitorNextCheckAt" not in card:
            unreadable.append("monitorNextCheckAt")
        elif card["monitorNextCheckAt"] is not None:
            due = parse_time(card["monitorNextCheckAt"])
            if due is None:
                unreadable.append("monitorNextCheckAt")
            elif due > now:
                continued = True
        if not isinstance(assignee, str) or not assignee:
            unreadable.append("assigneeAgentId")
        if continued:
            continue
        if unreadable:
            out.append(_finding(
                "stranded_429",
                "429-ended card with unreadable continuation fields",
                "unknown", "director", CEO_ROUTE,
                {"identifier": identifier, "fields": unreadable},
                "fix the adapter fields; cannot tell whether a run, "
                "monitor, blocker or recovery action continues the card",
                issue_id, identifier))
            continue
        if assignee not in lane:
            out.append(_finding(
                "stranded_429", "429-ended card whose assignee has no "
                "agentRuns row", "unknown", "director", CEO_ROUTE,
                {"identifier": identifier, "assigneeAgentId": assignee},
                "add one agentRuns row per assignee; cannot tell whether "
                "the lane has recovered", issue_id, identifier))
            continue
        succeeded, bad_stamp = lane[assignee]
        if bad_stamp:
            out.append(_finding(
                "stranded_429", "429-ended card whose assignee has an "
                "unreadable lastSucceededAt", "unknown", "director",
                CEO_ROUTE,
                {"identifier": identifier, "assigneeAgentId": assignee},
                "fix the lastSucceededAt timestamp; cannot tell whether "
                "the lane has recovered", issue_id, identifier))
            continue
        if succeeded is None:
            continue  # no success on record: the lane has not recovered
        lane_age = _age_min(now, succeeded)
        if lane_age is None:
            future_lanes += 1
            continue
        if lane_age > STRANDED_LANE_OK_MIN:
            continue  # lane still failing; resume waits
        key = stranded_429_dedupe_key(issue_id, commented)
        out.append(_finding(
            "stranded_429", "assigned in-progress card stranded after a "
            "429 with no continuation", "high", "director", CEO_ROUTE,
            {"identifier": identifier, "assigneeAgentId": assignee,
             "ageMin": round(age_min, 1),
             "laneSucceededAgeMin": round(lane_age, 1)},
            f"{STRANDED_429_ACTION} (resume:true on the card)",
            issue_id, identifier, key))
        out.append(_proposal(
            "stranded_429", "stranded after a 429 with no continuation",
            issue_id, identifier,
            f"Suggested action: {STRANDED_429_ACTION}. Phase 1 writes "
            "nothing; the assignee's lane has succeeded within "
            f"{STRANDED_LANE_OK_MIN} minutes.",
            before={"status": "in_progress", "assigneeAgentId": assignee,
                    "ageMin": round(age_min, 1)},
            after={"suggestedAction": STRANDED_429_ACTION},
            dedupe_key=key))
    if future_comments:
        out.append(_future_dated("stranded_429", "director",
                                 "newestComment.createdAt", future_comments))
    if future_lanes:
        out.append(_future_dated("stranded_429", "director",
                                 "lastSucceededAt", future_lanes))
    out.extend(card_rows.unknown(_finding, "stranded_429", "director",
                                 CEO_ROUTE))
    out.extend(lane_rows.unknown(_finding, "stranded_429", "director",
                                 CEO_ROUTE))
    return out


def _with_provenance(evidence, source):
    if isinstance(source, dict):
        return evidence | {"provenance": source}
    return evidence


def detect_red_main(reds, source=None):
    out = []
    if reds is None:
        return [_finding("red_main", "red-main snapshot missing", "unknown",
                         "ceo", CEO_ROUTE, {"section": "redMains"},
                         "supply branch status per repo; no verdict without it")]
    rows = SectionRead("redMains", reds)
    for red in rows:
        repo = red.get("repo") or "?"
        sig = red.get("signature") or "?"
        if red.get("incidentExists"):
            out.append(_finding(
                "red_main", "red main already tracked by an incident",
                "info", "repo-lead", CEO_ROUTE,
                _with_provenance({"repo": repo, "signature": sig}, source),
                "no new incident; fix main first"))
        else:
            out.append(_finding(
                "red_main", "red main with no incident",
                "high", "repo-lead", "repo-lead",
                _with_provenance({"repo": repo, "signature": sig,
                                  "since": red.get("since")}, source),
                "CEO routine opens exactly ONE incident card per repo and red "
                "signature; the writer has no issue-create mutation"))
    out.extend(rows.unknown(_finding, "red_main", "ceo", CEO_ROUTE))
    return out


def detect_secret_hit(hits):
    out = []
    if hits is None:
        return [_finding("secret_hit", "secret-scan snapshot missing",
                         "unknown", "ciso", CEO_ROUTE,
                         {"section": "secretHits"},
                         "supply the scan snapshot; no verdict without it")]
    rows = SectionRead("secretHits", hits)
    for hit in rows:
        # Never echo values: accept names and lengths only.
        out.append(_finding(
            "secret_hit", "secret pattern hit in run log or transcript",
            "critical", "ciso", "ciso",
            {"source": hit.get("source"),
             "patternName": hit.get("patternName"),
             "valueLen": hit.get("valueLen"),
             "at": hit.get("at")},
            "Hindsight scrub of the affected memory; this is not rotation"))
    out.extend(rows.unknown(_finding, "secret_hit", "ciso", CEO_ROUTE))
    return out


def detect_quota_expiry(lanes):
    """Flag Muse lanes close to weekly reset with thin headroom.

    Fires only when BOTH hold: hoursToReset < QUOTA_EXPIRY_HOURS (48)
    and headroomPct < QUOTA_HEADROOM_PCT (25). Non-Muse lanes are
    skipped silently; other detectors own them. Finding plus a
    mutation:none proposal, so phase 1 changes no pin, runs no host
    script, and arms no timer (case_9e33a5c2 retires host timers).
    """
    if lanes is None:
        return [_finding("quota_expiry", "quota-lanes snapshot missing",
                         "unknown", "cto", CEO_ROUTE,
                         {"section": "quotaLanes"},
                         "supply Muse lane hours-to-reset and headroom; "
                         "no verdict without it")]
    if not isinstance(lanes, list):
        return [_finding("quota_expiry", "quotaLanes snapshot unreadable",
                         "unknown", "cto", CEO_ROUTE,
                         {"section": "quotaLanes"},
                         "supply a quotaLanes array; no verdict without it")]
    out = []
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        name = str(lane.get("lane") or "?")
        if "muse" not in name.lower():
            continue
        try:
            hours = float(lane.get("hoursToReset"))
            headroom = float(lane.get("headroomPct"))
        except (TypeError, ValueError):
            hours = headroom = None
        if hours is None or headroom is None or hours < 0 or headroom < 0:
            out.append(_finding(
                "quota_expiry", "Muse lane with unreadable quota numbers",
                "unknown", "cto", CEO_ROUTE,
                {"lane": name},
                "fix the hoursToReset/headroomPct reading; "
                "cannot judge expiry without it"))
            continue
        if hours >= QUOTA_EXPIRY_HOURS or headroom >= QUOTA_HEADROOM_PCT:
            continue
        evidence = {"lane": name, "hoursToReset": hours,
                    "headroomPct": headroom}
        out.append(_finding(
            "quota_expiry", "Muse lane under 48h to reset with under "
            "25% headroom",
            "high", "cto", "cto", evidence,
            "pace use-before-expiry toward this lane; no pin change in "
            "phase 1"))
        out.append(_proposal(
            "quota_expiry", "Muse lane nearing quota expiry",
            lane.get("issueId") or name, name,
            "Phase-2 candidate: shift admission share toward the expiring "
            "lane once a pacing owner resolver exists; phase 1 writes "
            "nothing.",
            before=evidence, after=evidence))
    return out


def detect_supply_famine(supply):
    out = []
    if supply is None:
        return [_finding("supply_famine", "supply snapshot missing",
                         "unknown", "coo", CEO_ROUTE, {"section": "supply"},
                         "supply the ready-now census; no verdict without it")]
    if not isinstance(supply, dict):
        return [_finding("supply_famine", "supply snapshot unreadable",
                         "unknown", "coo", CEO_ROUTE, {"section": "supply"},
                         "fix the supply adapter; cannot compare without it")]
    ready = read_int(supply.get("readyNow"))
    target = read_int(supply.get("target"))
    below = read_float(supply.get("belowMin"))
    if ready is None or target is None or below is None:
        return [_finding("supply_famine", "supply snapshot unreadable",
                         "unknown", "coo", CEO_ROUTE, {"section": "supply"},
                         "fix the supply adapter; cannot compare without it")]
    idle = read_int(supply.get("idleAgents", 0))
    if idle is None:
        idle = 0
    if ready >= target:
        return []
    evidence = {"readyNow": ready, "target": target,
                "belowMin": round(below, 1), "idleAgents": idle,
                "censusAt": supply.get("censusAt")}
    if below < SUPPLY_FAMINE_MIN:
        out.append(_finding(
            "supply_famine", "ready-now below target under 30m",
            "info", "coo", CEO_ROUTE, evidence,
            "watching: ready-now below target; page only if sustained 30m"))
        return out
    out.append(_finding(
        "supply_famine", "ready-now below target sustained 30m",
        "high", "ceo", CEO_ROUTE, evidence,
        "comment the famine on case_d36e8c4a (state change only, <1200 chars, "
        "max 1 per 2h) with CEO mention"))
    out.append(_proposal(
        "supply_famine", "sustained supply famine pages via CEO routine",
        None, "case_d36e8c4a",
        "Phase-2 candidate: cross-issue comment mutation does not exist; "
        "the page stays a CEO-routine action, same shape as red_main "
        "incident creation; phase 1 writes nothing."))
    return out


def detect_shadow_emit_gap(now, shadow):
    """Alert when no shadow decisions are recorded for > SHADOW_GAP_MIN.

    Covers the writeTextAtomic timeout / oversized-line drop failure modes
    (case_541b0671 owns the write-path fix; this detector only alerts).
    Finding-only: no proposal, so no phase-2 auto-action follows from it.
    """
    if shadow is None:
        return []  # optional section; absence is not a gap.
    if not isinstance(shadow, dict):
        return [_finding("shadow_emit_gap",
                         "shadowEmit snapshot unreadable",
                         "unknown", "cto", CEO_ROUTE,
                         {"section": "shadowEmit"},
                         "supply lastDecisionAt; no verdict without it")]
    last = parse_time(shadow.get("lastDecisionAt"))
    if last is None:
        return [_finding("shadow_emit_gap",
                         "shadow lastDecisionAt unreadable",
                         "unknown", "cto", CEO_ROUTE,
                         {"section": "shadowEmit"},
                         "fix the lastDecisionAt timestamp; cannot age the gap")]
    if now is None:
        return []  # snapshot_clock already reports the unreadable now.
    gap_min = _age_min(now, last)
    if gap_min is None:
        return [_future_dated("shadow_emit_gap", "cto", "lastDecisionAt", 1)]
    if gap_min <= SHADOW_GAP_MIN:
        return []
    return [_finding("shadow_emit_gap",
                     "no shadow decisions recorded past 30m",
                     "high", "cto", "cto",
                     {"lastDecisionAt": shadow.get("lastDecisionAt"),
                      "gapMin": round(gap_min, 1)},
                     "triage the shadow write path (case_541b0671 owns the fix); "
                     "suspect writeTextAtomic timeout or oversized-line drops")]


STANDARD_FILES = ("templateSha", "agentsSha", "contributingSha")


def detect_org_standard_drift(org):
    out = []
    if org is None:
        return [_finding("org_standard", "org-standard snapshot missing",
                         "unknown", "director", CEO_ROUTE,
                         {"section": "orgStandard"},
                         "supply the weekly standard snapshot; no verdict without it")]
    canonical = org.get("canonical") if isinstance(org, dict) else None
    if not isinstance(canonical, dict) or not all(
            canonical.get(key) for key in STANDARD_FILES):
        return [_finding("org_standard", "canonical bundle SHAs missing",
                         "unknown", "director", CEO_ROUTE,
                         {"section": "orgStandard.canonical"},
                         "pin the canonical SHAs from ops-tooling origin/main; "
                         "no comparison without them")]
    repos = org.get("repos")
    if not isinstance(repos, list):
        return [_finding("org_standard", "org-standard repo list missing",
                         "unknown", "director", CEO_ROUTE,
                         {"section": "orgStandard.repos"},
                         "supply one entry per active repo; no verdict without it")]
    for entry in repos:
        if not isinstance(entry, dict) or not entry.get("repo"):
            continue
        repo = entry.get("repo")
        inherited = entry.get("inheritsOrgDefault") is True
        drifted = [key for key in STANDARD_FILES
                   if entry.get(key) != canonical.get(key)
                   and not (inherited and entry.get(key) is None)]
        if drifted:
            out.append(_finding(
                "org_standard", "repo template bundle drifted from canonical",
                "high", "director", CEO_ROUTE,
                {"repo": repo, "driftedFiles": sorted(drifted),
                 "inheritsOrgDefault": inherited},
                "open a sync PR from the canonical bundle; the detector never pushes"))
            out.append(_proposal(
                "org_standard", "repo template bundle drifted from canonical",
                f"org-standard:{repo}", repo,
                "Phase-2 candidate: open a sync PR once a writer mutation "
                "for it exists; phase 1 writes nothing.",
                before={"driftedFiles": sorted(drifted)},
                after={"driftedFiles": []}))
        for flag, reason, action in (
                ("requiresPrLint",
                 "repo does not require the conventional-title check",
                 "add pr-lint (or ci-ok) to the repo ruleset's required checks"),
                ("squashOnly", "repo allows non-squash merges",
                 "restrict the repo ruleset to squash merges")):
            if entry.get(flag) is True:
                continue
            out.append(_finding(
                "org_standard", reason, "high", "director", CEO_ROUTE,
                {"repo": repo, "flag": flag, "value": entry.get(flag)},
                action + "; if drift is real across repos, file ONE "
                "evaluate-mode org ruleset without the approvals rule "
                "(case_8843860f decision)"))
            out.append(_proposal(
                "org_standard", reason, f"org-standard:{repo}", repo,
                "Phase-2 candidate: operator_decision to fix the repo "
                "ruleset; phase 1 writes nothing."))
    return out

def detect(snapshot, stats=None):
    """Run every detector family over one snapshot.

    Returns the record list as before. When ``stats`` is a dict it is
    filled with the emission-side per-detector metric (case_07aa689b):
    ``perDetector`` maps each family to its finding/proposal counts plus
    detection latency in milliseconds, and ``elapsedMs`` totals the cycle.
    The optional out-param keeps the existing ``detect(snapshot)`` call
    shape working for every current caller and test.

    Clock: ``now_source`` picks the age clock. An absent ``now`` uses the
    wall clock; a present but unreadable one is never replaced by it, so a
    replayed snapshot does not age differently each day.
    """
    source = now_source(snapshot)
    if source == NOW_SNAPSHOT:
        now = parse_time(snapshot["now"])
    elif source == NOW_WALLCLOCK:
        now = datetime.datetime.now(datetime.timezone.utc)
    else:
        now = None
    records = []
    if now is None:
        # Never the wall clock: the age-based detectors below skip their
        # aging, so a replay cannot differ by the day it runs.
        records.append(_finding(
            "snapshot_clock", "snapshot now unreadable", "unknown", "ceo",
            CEO_ROUTE,
            {"field": "now", "valueType": type(snapshot["now"]).__name__},
            "fix the snapshot adapter's now timestamp; agent_error, churn, "
            "pending_interactions, autoscaler_slices, stranded_429, "
            "stale_reviews and shadow_emit_gap cannot age rows without it"))
    families = [
        ("agent_error",
         lambda: detect_agent_error(now, snapshot.get("agents"))),
        ("shadow_emit_gap",
         lambda: detect_shadow_emit_gap(now, snapshot.get("shadowEmit"))),
        ("host_health", lambda: detect_host_health(snapshot.get("hosts"))),
        ("ci_health", lambda: detect_ci_health(snapshot.get("ciJobs"),
                                               snapshot.get("queueAge"))),
        ("assignment", lambda: detect_assignment(
            snapshot.get("issues"), snapshot.get("assignmentEvidence"))),
        ("churn", lambda: detect_churn(now, snapshot.get("runEvents"),
                                       snapshot.get("parks"))),
        ("pending_interactions",
         lambda: detect_pending_interactions(
             now, snapshot.get("pendingInteractions"))),
        ("autoscaler_slices",
         lambda: detect_autoscaler_slices(
             now, snapshot.get("autoscalerSlices"))),
        ("stranded_429",
         lambda: detect_stranded_429(
             now, snapshot.get("assignedInProgress"),
             snapshot.get("agentRuns"))),
        ("red_main", lambda: detect_red_main(
            snapshot.get("redMains"), snapshot.get("redMainsSource"))),
        ("secret_hit", lambda: detect_secret_hit(snapshot.get("secretHits"))),
        ("stale_reviews",
         lambda: detect_stale_reviews(now, snapshot.get("reviews"),
                                      snapshot.get("openProposalSignatures"))),
        ("quota_expiry",
         lambda: detect_quota_expiry(snapshot.get("quotaLanes"))),
        ("supply_famine",
         lambda: detect_supply_famine(snapshot.get("supply"))),
        ("org_standard",
         lambda: detect_org_standard_drift(snapshot.get("orgStandard"))),
    ]
    per_detector = {}
    total_ms = 0.0
    for name, run in families:
        start = time.perf_counter()
        out = run()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        total_ms += elapsed_ms
        records.extend(out)
        per_detector[name] = {
            "findings": sum(1 for r in out if r.get("type") == "finding"),
            "proposals": sum(1 for r in out if r.get("type") == "proposal"),
            "latencyMs": round(elapsed_ms, 2),
        }
    if stats is not None:
        stats["perDetector"] = per_detector
        stats["elapsedMs"] = round(total_ms, 2)
    # Emission stamp for delivery-latency math (case_07aa689b): the snapshot
    # instant, so output stays byte-identical for a pinned ``now``. An absent
    # ``now`` stays unstamped here; main() stamps those with the wall clock.
    # An unreadable ``now`` stays unstamped everywhere, so a replay cannot
    # differ by the day it runs. Metadata only; no decision reads it.
    if source == NOW_SNAPSHOT:
        emitted_at = _iso_z(now)
        for record in records:
            record["emittedAt"] = emitted_at
    for record in records:
        mutation = record.get("mutation")
        if mutation is not None and mutation not in WRITER_COMPATIBLE_MUTATIONS:
            raise ValueError(f"detector emitted outside-allowlist mutation {mutation!r}")
        if record.get("type") == "finding":
            evidence = json.dumps(record.get("evidence") or {})
            # Pattern names like "ghp_*" are fine; value-shaped strings
            # (prefix plus 8 or more alphanumerics) are never allowed out.
            if re.search(r"ghp_[A-Za-z0-9]{8,}", evidence) \
                    or re.search(r"github_pat_[A-Za-z0-9_]{8,}", evidence):
                raise ValueError(
                    "finding evidence carries a possible secret value")
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Platform watchdog detectors: read-only, propose-only.")
    parser.add_argument("--snapshot", required=True,
                        help="JSON file with agents/hosts/CI/issues/churn/"
                             "pendingInteractions/autoscalerSlices/"
                             "assignedInProgress/agentRuns/"
                             "redMains/secretHits/reviews/supply")
    parser.add_argument("--delivery-metrics", action="store_true",
                        help="add per-detector counts and latency "
                             "(elapsedMs, perDetector) to the summary row "
                             "for watchdog/delivery.py; off by default so "
                             "output stays byte-identical for a pinned now")
    args = parser.parse_args(argv)
    try:
        with open(args.snapshot, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"watchdog: cannot read snapshot: {exc}", file=sys.stderr)
        return 2
    if not isinstance(snapshot, dict):
        print("watchdog: snapshot must be a JSON object", file=sys.stderr)
        return 2
    stats: dict = {}
    records = detect(snapshot, stats if args.delivery_metrics else None)
    # Absent ``now`` is aged on the wall clock, so stamp that same clock.
    # Unreadable ``now`` must stay free of it: two runs on different days
    # have to print the same bytes.
    if now_source(snapshot) != NOW_UNREADABLE:
        wall_clock = _iso_z(datetime.datetime.now(datetime.timezone.utc))
        for record in records:
            record.setdefault("emittedAt", wall_clock)
    for record in records:
        print(json.dumps(record, sort_keys=True))
    summary = {"type": "summary", "detector": "watchdog/cycle",
               "records": len(records), "phase": PHASE,
               "nowSource": now_source(snapshot)}
    if args.delivery_metrics:
        summary["elapsedMs"] = stats.get("elapsedMs", 0)
        summary["perDetector"] = stats.get("perDetector", {})
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
