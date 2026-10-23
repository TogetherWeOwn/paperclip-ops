#!/usr/bin/env python3
"""Read-only, offline convergence queries for an existing daily digest.

No collector, scheduler, network client or repair path. See
../docs/convergence-watches.md for the versioned private export contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path

BASE_SHA = "bba6a45f0d364cfd962ac59b3bdf2426a8222adb"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 5000
DAY = 86400
ROUTE = "existing_daily_digest"

# Parameters are JSON exports, never SQL fragments or database credentials.
QUERIES = {
    "rapid_reblock": """
        SELECT count(*) FROM json_each(:status_transitions) b
        WHERE json_extract(b.value, '$.status') = 'blocked'
          AND json_extract(b.value, '$.at') >= :start
          AND json_extract(b.value, '$.at') < :end
          AND EXISTS (
            SELECT 1 FROM json_each(:status_transitions) d
            WHERE json_extract(d.value, '$.issue_id') = json_extract(b.value, '$.issue_id')
              AND json_extract(d.value, '$.status') = 'done'
              AND (json_extract(d.value, '$.at') < json_extract(b.value, '$.at')
                   OR (json_extract(d.value, '$.at') = json_extract(b.value, '$.at')
                       AND json_extract(d.value, '$.sequence') < json_extract(b.value, '$.sequence')))
              AND json_extract(d.value, '$.at') >= json_extract(b.value, '$.at') - 300
          )""",
    "over_cap_starts": """
        SELECT count(*) FROM json_each(:run_intervals) r
        JOIN json_each(:cap_intervals) c
          ON json_extract(c.value, '$.agent_id') = json_extract(r.value, '$.agent_id')
         AND json_extract(c.value, '$.start') <= json_extract(r.value, '$.start')
         AND (json_extract(c.value, '$.end') IS NULL
              OR json_extract(c.value, '$.end') > json_extract(r.value, '$.start'))
        WHERE json_extract(r.value, '$.start') >= :start
          AND json_extract(r.value, '$.start') < :end
          AND (SELECT count(*) FROM json_each(:run_intervals) a
               WHERE json_extract(a.value, '$.agent_id') = json_extract(r.value, '$.agent_id')
                 AND json_extract(a.value, '$.start') <= json_extract(r.value, '$.start')
                 AND (json_extract(a.value, '$.end') IS NULL
                      OR json_extract(a.value, '$.end') > json_extract(r.value, '$.start'))
              ) > json_extract(c.value, '$.cap')""",
    "workspace_incoherence": """
        SELECT count(*) FROM json_each(:workspace_observations)
        WHERE json_extract(value, '$.at') >= :start
          AND json_extract(value, '$.at') < :end
          AND json_extract(value, '$.coherent') = 0""",
    "duplicate_review_heads": """
        SELECT count(*), coalesce(sum(assessed), 0), coalesce(sum(unassessed), 0) FROM (
          SELECT json_extract(value, '$.endpoint_id'), json_extract(value, '$.repository_id'),
                 json_extract(value, '$.pull_number'), json_extract(value, '$.head_sha'),
                 sum(json_extract(value, '$.assessed') = 1) AS assessed,
                 sum(json_extract(value, '$.assessed') = 0) AS unassessed
          FROM json_each(:reviews)
          WHERE json_extract(value, '$.at') < :end
          GROUP BY 1, 2, 3, 4
          HAVING count(*) > 1 AND max(json_extract(value, '$.at')) >= :start
        )""",
    "stdin_deadline_failures": """
        SELECT count(*) FROM json_each(:run_failures)
        WHERE json_extract(value, '$.at') >= :start
          AND json_extract(value, '$.at') < :end
          AND json_extract(value, '$.failed') = 1
          AND (instr(lower(json_extract(value, '$.error')), 'no stdin data received in 3s') > 0
               OR instr(lower(json_extract(value, '$.stderr')), 'no stdin data received in 3s') > 0)""",
}
SPECS = {
    "status_transitions": {"id": str, "issue_id": str, "at": int, "sequence": int, "status": str},
    "run_intervals": {"id": str, "agent_id": str, "start": int, "end": (int, type(None))},
    "cap_intervals": {"id": str, "agent_id": str, "start": int, "end": (int, type(None)), "cap": int},
    "workspace_observations": {"id": str, "at": int, "coherent": bool},
    "reviews": {"id": str, "endpoint_id": str, "repository_id": str, "pull_number": int,
                "head_sha": str, "at": int, "assessed": bool},
    "run_failures": {"id": str, "at": int, "failed": bool, "error": str, "stderr": str},
}
SOURCES = {
    "rapid_reblock": ("status_transitions",),
    "over_cap_starts": ("run_intervals", "cap_intervals"),
    "workspace_incoherence": ("workspace_observations",),
    "duplicate_review_heads": ("reviews",),
    "stdin_deadline_failures": ("run_failures",),
}


def require(condition):
    if not condition:
        raise ValueError("invalid_export")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def load_export(raw):
    require(len(raw) <= MAX_BYTES)
    return json.loads(raw, object_pairs_hook=strict_object,
                      parse_constant=lambda _: require(False))


def validate_manifest(doc):
    require(isinstance(doc, dict) and set(doc) == {
        "schema", "company_id", "artifact", "start", "end", "sources"})
    require(doc["schema"] == "paperclip.convergence-export.v1")
    require(isinstance(doc["company_id"], str) and re.fullmatch(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", doc["company_id"]))
    artifact = doc["artifact"]
    require(isinstance(artifact, dict) and set(artifact) == {"base_sha", "source_sha", "image_digest"})
    require(artifact["base_sha"] == BASE_SHA)
    require(isinstance(artifact["source_sha"], str) and re.fullmatch(r"[0-9a-f]{40}", artifact["source_sha"]))
    require(isinstance(artifact["image_digest"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["image_digest"]))
    require(type(doc["start"]) is int and type(doc["end"]) is int)
    require(0 <= doc["start"] < doc["end"] and doc["end"] - doc["start"] == DAY)
    require(isinstance(doc["sources"], dict) and not (set(doc["sources"]) - set(SPECS)))


def validate_source(name, source, start, end):
    require(isinstance(source, dict) and set(source) == {
        "complete", "boundary_complete", "start", "end", "rows"})
    require(source["complete"] is True and source["boundary_complete"] is True)
    lower = max(0, start - 300) if name == "status_transitions" else start
    require(type(source["start"]) is int and type(source["end"]) is int)
    require(0 <= source["start"] <= lower and source["end"] >= end)
    rows = source["rows"]
    require(isinstance(rows, list) and len(rows) <= MAX_ROWS)
    ids = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == set(SPECS[name]))
        for key, kind in SPECS[name].items():
            kinds = kind if isinstance(kind, tuple) else (kind,)
            require(type(row[key]) in kinds)
            if isinstance(row[key], str):
                require(len(row[key]) <= 65536)
            elif type(row[key]) is int:
                require(0 <= row[key] <= 2**53 - 1)
        require(row["id"] and row["id"] not in ids)
        ids.add(row["id"])
        if "agent_id" in row:
            require(bool(row["agent_id"]))
            require(row["end"] is None or row["end"] > row["start"])
        if name == "status_transitions":
            require(bool(row["issue_id"]))
            require(row["status"] in {"backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled"})
        elif name == "cap_intervals":
            require(row["cap"] > 0)
        elif name == "reviews":
            require(row["endpoint_id"] and row["repository_id"] and row["pull_number"] > 0)
            require(bool(re.fullmatch(r"[0-9a-f]{40}", row["head_sha"])))
    if name == "status_transitions":
        histories = {}
        for row in rows:
            histories.setdefault(row["issue_id"], []).append(row)
        for history in histories.values():
            history.sort(key=lambda r: r["sequence"])
            for left, right in zip(history, history[1:]):
                require(left["sequence"] < right["sequence"] and left["at"] <= right["at"])
                require(left["status"] != right["status"])
    return rows


def validate_caps(runs, caps, start, end):
    # A cap at digest time is not evidence of the cap when a run started.
    by_agent = {}
    for cap in caps:
        by_agent.setdefault(cap["agent_id"], []).append(cap)
    for intervals in by_agent.values():
        intervals.sort(key=lambda c: c["start"])
        for left, right in zip(intervals, intervals[1:]):
            require(left["end"] is not None and left["end"] <= right["start"])
    for run in runs:
        if start <= run["start"] < end:
            matches = [c for c in by_agent.get(run["agent_id"], [])
                       if c["start"] <= run["start"] and (c["end"] is None or c["end"] > run["start"])]
            require(len(matches) == 1)


def evaluate(doc):
    """Evaluate fixed SELECTs only. The caller owns collection and delivery."""
    validate_manifest(doc)
    start, end = doc["start"], doc["end"]
    rows, gaps = {}, set()
    for name in SPECS:
        try:
            rows[name] = validate_source(name, doc["sources"].get(name), start, end)
        except ValueError:
            gaps.add(name)
    if not gaps.intersection(SOURCES["over_cap_starts"]):
        try:
            validate_caps(rows["run_intervals"], rows["cap_intervals"], start, end)
        except ValueError:
            gaps.add("cap_intervals")
    parameters = {name: json.dumps(value) for name, value in rows.items()}
    parameters.update(start=start, end=end)
    results = []
    with sqlite3.connect(":memory:") as db:
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA query_only=ON")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        db.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
        for watch, query in QUERIES.items():
            count, reason = None, "source_missing_or_invalid"
            if not gaps.intersection(SOURCES[watch]):
                deadline = time.monotonic() + 2
                steps = 0

                def bounded_query():
                    nonlocal steps
                    steps += 1000
                    return int(steps > 2_000_000 or time.monotonic() > deadline)

                db.set_progress_handler(bounded_query, 1000)
                try:
                    counts = db.execute(query, parameters).fetchone()
                    count = counts[0]
                    reason = "regression" if count else "clear"
                except sqlite3.Error:
                    reason = "query_unavailable_or_budget_exceeded"
            result = {"watch": watch, "status": "unknown" if count is None else "finding" if count else "clear",
                      "count": count, "reason": reason, "route": ROUTE}
            if watch == "duplicate_review_heads" and count is not None:
                # Assessment presence is not completeness, a score or approval.
                result["assessed_rows"] = counts[1]
                result["unassessed_rows"] = counts[2]
            results.append(result)
    return {"schema": "paperclip.convergence-report.v1", "artifact": doc["artifact"],
            "start": start, "end": end, "results": results,
            "read_only": True, "runtime_qualified": False}


def render_digest(report):
    """A quiet healthy report is empty; only findings/gaps reach the digest."""
    lines = [f"- {r['watch']}: {r['count'] if r['count'] is not None else 'UNKNOWN'} ({r['reason']})"
             for r in report["results"] if r["status"] != "clear"]
    if not lines:
        return ""
    return "## Convergence regression watches\n\n" + "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="Private, complete normalized export; never credentials")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    try:
        # Validate the opened descriptor, not a pathname susceptible to a swap.
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(args.snapshot, flags), "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode))
            require(os.fstat(stream.fileno()).st_size <= MAX_BYTES)
            raw = stream.read(MAX_BYTES + 1)
        report = evaluate(load_export(raw))
        report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, RecursionError, OverflowError, sqlite3.Error):
        # Never echo input values, filesystem paths, raw stderr or exception text.
        error = {"schema": "paperclip.convergence-report.v1", "error": "invalid_export",
                 "retryable": False, "next_action": "supply_a_complete_v1_export_for_the_pinned_base",
                 "read_only": True, "runtime_qualified": False}
        print(json.dumps(error) if args.format == "json" else
              "## Convergence regression watches\n\n- UNKNOWN: invalid_export\n")
        return 2
    print(json.dumps(report, sort_keys=True) if args.format == "json" else render_digest(report), end="\n" if args.format == "json" else "")
    if any(r["status"] == "unknown" for r in report["results"]):
        return 2
    return int(any(r["status"] == "finding" for r in report["results"]))


if __name__ == "__main__":
    sys.exit(main())
