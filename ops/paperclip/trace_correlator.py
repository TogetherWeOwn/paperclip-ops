#!/usr/bin/env python3
"""Correlate explicit card/run/PR/head/deploy evidence into queryable traces.

Offline join of caller-assembled native evidence into per-run stage records
plus one aggregate lead-time SLO verdict. No network, no credentials, no
board reads or writes: the caller exports the evidence (Paperclip runs and
work products, PR head SHAs, deployment rows) and feeds one JSON document
on stdin. Prints one JSON report on stdout.

WHY THIS EXISTS. Card, run, PR, head and deploy IDs live in separate
surfaces. Nothing joined them, so "trace this card end to end" was manual
and latency numbers were quoted without their gaps. This module closes that
gap without touching any decision, deploy or apply path:

- ``traces``: one record per (run, PR-link) pair carrying the five IDs,
  stage latencies in seconds (admission-to-start, execution,
  merge-to-prod) and a named ``missing`` list for every unmeasured span.
  Missing spans stay null, never zero; negative spans (clock skew) stay
  null with the skew named.
- ``slo``: the G4 merge-to-prod aggregate (p50 under 60 min, p90 under
  3 h, 30-day windows) evaluated over eligible samples only. Zero
  eligible samples is verdict ``unknown``, never met. Thresholds are
  aggregate-only: no per-trace verdict exists by design.
- ``delivery``: the telemetry self-test. Join coverage per handoff
  (run->card, work-product->run, deploy->head) plus orphan and invalid
  counts, so a silent producer shows up as missing evidence, not as a
  clean board.

JOIN KEYS. Exact IDs only, matched case-sensitively:

- run rows accept both native shapes (REST ``runId``/``contextIssueId``
  and SDK-summary ``id``/``issueId``) and normalize them explicitly.
- a work-product row is a PR link only when it carries explicit ``repo``
  (``owner/name``) and ``pr`` (integer). Titles, URLs and timestamps are
  never linkage.
- a deploy joins a trace only on exact ``repo`` + full 40-hex ``sha``
  equality with the trace's explicit ``headSha`` or ``mergeSha`` (the
  merge commit is the production path; the PR head covers pre-merge
  preview deploys). Mutable refs (branch names, ``latest``) are
  rejected, never resolved. When several deploys share one sha, the
  earliest ``deployedAt`` wins; ties keep file order. A merge-sha match
  wins over a head-sha match; the winner is recorded as ``shaMatch``.

USAGE.

  python3 ops/paperclip/trace_correlator.py < evidence.json

Exit 0 when the SLO verdict is met, 1 when it is missed or unknown (look
at the report: a breach or an evidence gap needs a human), 2 on
unreadable or invalid input. The exit status is not the verdict;
consumers must read ``slo.verdict``. See docs/trace-correlation.md.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys

SCHEMA = "trace-correlation.v1"
SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1_048_576

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

# G4 merge-to-prod lead time, goal c753f77b-76ea-419c-867b-1578bdc9491a:
# 30-day windows, p50 under 60 minutes, p90 under 3 hours. Aggregate-only.
G4_P50_S = 3600.0
G4_P90_S = 10800.0

STAGE_ADMISSION = "admissionToStartS"
STAGE_EXECUTION = "executionS"
STAGE_MERGE_TO_PROD = "mergeToProdS"


class InputError(ValueError):
    pass


def parse_time(value):
    """Parse an ISO timestamp to aware UTC datetime; None when unreadable."""
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("nonfinite number")


def load_document(raw):
    """Parse and shape-check the evidence document; raises InputError."""
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError("input exceeds 1048576 bytes")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        # Validate ignored keys/values too: lone surrogates and overflowing
        # float exponents must not qualify as evidence.
        json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InputError(f"invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise InputError("document must be an object")
    if type(document.get("schema_version")) is not int or (
        document.get("schema_version") != SCHEMA_VERSION
    ):
        raise InputError("schema_version must be the integer 1")
    evidence = {}
    for key in ("runs", "workProducts", "deployments"):
        value = document.get(key, [])
        if not isinstance(value, list):
            raise InputError(f"{key} must be a list")
        evidence[key] = value
    return evidence


def span_seconds(start, end):
    """Seconds between two parsed datetimes; None when either is missing."""
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def percentile(sorted_values, pct):
    """Linear-interpolation percentile over an ascending list; None if empty."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def normalize_run(row):
    """Normalize one run row; (record, None) or (None, reason)."""
    if not isinstance(row, dict):
        return None, "run_not_an_object"
    run_id = row.get("runId", row.get("id"))
    card_id = row.get("contextIssueId", row.get("issueId"))
    if not run_id or not isinstance(run_id, str):
        return None, "run_missing_id"
    if not card_id or not isinstance(card_id, str):
        return None, "run_missing_card"
    return (
        {
            "run": run_id,
            "card": card_id,
            "created": parse_time(row.get("createdAt")),
            "started": parse_time(row.get("startedAt")),
            "finished": parse_time(row.get("finishedAt")),
        },
        None,
    )


def normalize_pr_link(row):
    """Normalize one work-product row; (link|None, reason|None).

    A row without explicit repo+pr is not a PR link: (None, None). A row
    that claims linkage but fails validation is (None, reason).
    """
    if not isinstance(row, dict):
        return None, "work_product_not_an_object"
    repo = row.get("repo")
    pr_number = row.get("pr")
    if repo is None and pr_number is None:
        return None, None
    card_id = row.get("issueId")
    run_id = row.get("createdByRunId")
    if not card_id or not isinstance(card_id, str):
        return None, "link_missing_card"
    if not run_id or not isinstance(run_id, str):
        return None, "link_missing_run"
    if not repo or not isinstance(repo, str) or not REPO_RE.match(repo):
        return None, "link_bad_repo"
    if type(pr_number) is not int or pr_number <= 0:
        return None, "link_bad_pr"
    head_sha = row.get("headSha")
    if head_sha is not None and (
        not isinstance(head_sha, str) or not FULL_SHA_RE.match(head_sha)
    ):
        return None, "link_head_not_exact_sha"
    merge_sha = row.get("mergeSha")
    if merge_sha is not None and (
        not isinstance(merge_sha, str) or not FULL_SHA_RE.match(merge_sha)
    ):
        return None, "link_merge_not_exact_sha"
    return (
        {
            "card": card_id,
            "run": run_id,
            "repo": repo,
            "pr": pr_number,
            "headSha": head_sha,
            "mergeSha": merge_sha,
        },
        None,
    )


def normalize_deploy(row, index):
    """Normalize one deployment row; (record, None) or (None, reason)."""
    if not isinstance(row, dict):
        return None, "deploy_not_an_object"
    repo = row.get("repo")
    sha = row.get("sha")
    if not repo or not isinstance(repo, str) or not REPO_RE.match(repo):
        return None, "deploy_bad_repo"
    if not sha or not isinstance(sha, str) or not FULL_SHA_RE.match(sha):
        return None, "deploy_sha_not_exact"
    deploy_id = row.get("deployId")
    if deploy_id is not None and (
        not isinstance(deploy_id, str) or not deploy_id
    ):
        return None, "deploy_bad_id"
    return (
        {
            "repo": repo,
            "sha": sha,
            "deployId": deploy_id,
            "merged": parse_time(row.get("mergedAt")),
            "deployed": parse_time(row.get("deployedAt")),
            "index": index,
        },
        None,
    )


def stage(seconds, negative_name):
    """Round a span; None stays None, negatives stay None with a skew marker."""
    if seconds is None:
        return None, None
    if seconds < 0:
        return None, negative_name
    return round(seconds, 3), None


def build_trace(run, link, deploy, sha_match):
    """Join one run (+ optional PR link and deploy) into a trace record."""
    missing = []
    for field, name in (
        ("created", "run.createdAt"),
        ("started", "run.startedAt"),
        ("finished", "run.finishedAt"),
    ):
        if run[field] is None:
            missing.append(name)
    admission, skew = stage(
        span_seconds(run["created"], run["started"]), "run.admission_negative"
    )
    if skew:
        missing.append(skew)
    execution, skew = stage(
        span_seconds(run["started"], run["finished"]), "run.execution_negative"
    )
    if skew:
        missing.append(skew)
    trace = {
        "card": run["card"],
        "run": run["run"],
        "repo": None,
        "pr": None,
        "headSha": None,
        "mergeSha": None,
        "deployId": None,
        "shaMatch": None,
        "stages": {
            STAGE_ADMISSION: admission,
            STAGE_EXECUTION: execution,
            STAGE_MERGE_TO_PROD: None,
        },
        "missing": missing,
    }
    if link is None:
        missing.append("pr_link")
        return trace
    trace["repo"] = link["repo"]
    trace["pr"] = link["pr"]
    trace["headSha"] = link["headSha"]
    trace["mergeSha"] = link["mergeSha"]
    if link["headSha"] is None and link["mergeSha"] is None:
        missing.append("pr.headSha")
        return trace
    if deploy is None:
        missing.append("deploy")
        return trace
    trace["shaMatch"] = sha_match
    trace["deployId"] = deploy["deployId"]
    if deploy["deployId"] is None:
        missing.append("deploy.deployId")
    if deploy["merged"] is None:
        missing.append("deploy.mergedAt")
    if deploy["deployed"] is None:
        missing.append("deploy.deployedAt")
    merge, skew = stage(
        span_seconds(deploy["merged"], deploy["deployed"]), "deploy.merge_negative"
    )
    if skew:
        missing.append(skew)
    else:
        trace["stages"][STAGE_MERGE_TO_PROD] = merge
    return trace


def correlate(evidence, checked_at):
    """Join evidence into the report. Pure: no I/O, no clock reads."""
    runs = []
    invalid = []
    for row in evidence["runs"]:
        record, reason = normalize_run(row)
        if record is None:
            invalid.append(reason)
        else:
            runs.append(record)

    links = []
    orphan_links = 0
    for row in evidence["workProducts"]:
        link, reason = normalize_pr_link(row)
        if link is None:
            if reason is not None:
                invalid.append(reason)
            continue
        links.append(link)

    deploys = []
    for index, row in enumerate(evidence["deployments"]):
        record, reason = normalize_deploy(row, index)
        if record is None:
            invalid.append(reason)
        else:
            deploys.append(record)

    runs_by_id = {}
    deduped = []
    for run in runs:
        if run["run"] in runs_by_id:
            invalid.append("run_duplicate_id")
            continue
        runs_by_id[run["run"]] = run
        deduped.append(run)
    runs = deduped
    links_by_run = {}
    for link in links:
        if link["run"] not in runs_by_id or (
            runs_by_id[link["run"]]["card"] != link["card"]
        ):
            orphan_links += 1
            continue
        links_by_run.setdefault(link["run"], []).append(link)

    deploys_by_head = {}
    for deploy in deploys:
        deploys_by_head.setdefault((deploy["repo"], deploy["sha"]), []).append(deploy)
    for candidates in deploys_by_head.values():
        # Earliest deployment wins; ties keep file order. Candidates without
        # a timestamp sort last but still join (deploy ID carried, span null).
        candidates.sort(
            key=lambda d: (
                d["deployed"] is None,
                d["deployed"] or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                d["index"],
            )
        )

    matched_deploys = set()
    traces = []
    for run in sorted(runs, key=lambda r: (r["card"], r["run"])):
        run_links = sorted(
            links_by_run.get(run["run"], []),
            key=lambda link: (link["repo"], link["pr"]),
        )
        if not run_links:
            traces.append(build_trace(run, None, None, None))
            continue
        for link in run_links:
            deploy = None
            sha_match = None
            # The merge commit is the production path; the PR head covers
            # pre-merge preview deploys. Merge match wins when both exist.
            for sha, match in (
                (link["mergeSha"], "merge"),
                (link["headSha"], "head"),
            ):
                if sha is None:
                    continue
                candidates = deploys_by_head.get((link["repo"], sha), [])
                if candidates:
                    deploy = candidates[0]
                    sha_match = match
                    matched_deploys.add(id(deploy))
                    break
            traces.append(build_trace(run, link, deploy, sha_match))

    orphan_deploys = sum(
        1 for deploy in deploys if id(deploy) not in matched_deploys
    )

    samples = sorted(
        t["stages"][STAGE_MERGE_TO_PROD]
        for t in traces
        if t["stages"][STAGE_MERGE_TO_PROD] is not None
    )
    p50 = percentile(samples, 50)
    p90 = percentile(samples, 90)
    if not samples:
        verdict = "unknown"
        unknown_reason = "zero_eligible_samples"
    else:
        verdict = "met" if (p50 <= G4_P50_S and p90 <= G4_P90_S) else "missed"
        unknown_reason = None
    slo = {
        "goal": "G4 merge-to-prod lead time (goal c753f77b-76ea-419c-867b-1578bdc9491a)",
        "windowNote": "cohort-assembled: the producer defines the window; "
        "G4 targets 30-day windows measured weekly",
        "thresholdP50S": G4_P50_S,
        "thresholdP90S": G4_P90_S,
        "samples": len(samples),
        "p50S": round(p50, 3) if p50 is not None else None,
        "p90S": round(p90, 3) if p90 is not None else None,
        "verdict": verdict,
        "unknownReason": unknown_reason,
        "at": checked_at,
    }

    joined_pr_links = sum(len(v) for v in links_by_run.values())
    delivery = {
        "runsSeen": len(evidence["runs"]),
        "runsValid": len(runs),
        "runsJoinedToCard": len(runs),
        "prLinksSeen": len(evidence["workProducts"]),
        "prLinksExplicit": len(links),
        "prLinksJoined": joined_pr_links,
        "deploysSeen": len(evidence["deployments"]),
        "deploysValid": len(deploys),
        "deploysJoined": len(matched_deploys),
        "orphanWorkProducts": orphan_links,
        "orphanDeployments": orphan_deploys,
        "invalidRows": len(invalid),
        "coverageRunCard": round(len(runs) / len(evidence["runs"]), 3)
        if evidence["runs"]
        else None,
        "coveragePrJoin": round(joined_pr_links / len(links), 3) if links else None,
        "coverageDeployJoin": round(len(matched_deploys) / len(deploys), 3)
        if deploys
        else None,
        "unknowns": sorted(set(invalid)),
        "at": checked_at,
    }
    return {
        "schema": SCHEMA,
        "traces": traces,
        "slo": slo,
        "delivery": delivery,
    }


def main(argv=None):
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        print(f"trace_correlator: cannot read stdin: {exc}", file=sys.stderr)
        return 2
    try:
        evidence = load_document(raw)
    except InputError as exc:
        print(f"trace_correlator: {exc}", file=sys.stderr)
        return 2
    checked_at = (
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    report = correlate(evidence, checked_at)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["slo"]["verdict"] == "met" else 1


if __name__ == "__main__":
    sys.exit(main())
