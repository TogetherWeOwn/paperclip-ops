#!/usr/bin/env python3
"""Watchdog proposal-delivery observability (case_07aa689b).

Offline join of one detector cycle and one recovery-writer cycle.
No network, no credentials, no board writes. Reads two JSONL files,
prints JSONL. Exit 0 when nothing needs a human, 1 when a drop/stall
needs one, 2 on unreadable input.

WHY THIS EXISTS.

Detectors propose and the single writer decides, but nothing measured the
handoff between them: a proposal could vanish (truncated proposals file,
writer never run, apply failed) and no row said so. This module closes
that gap without touching any decision or apply path:

- per-detector ``delivery_metric`` rows: emitted / delivered / clean /
  refused / deferred / failed / missing counts, success rate, and
  proposal-to-decision latency (median/max over matched pairs).
- ``delivery_alert`` rows with an alert route when a proposal drops or
  stalls: no writer receipt at all (young ones are info, ones older than
  ``--stall-after-min`` are high), or a receipt that records a failed
  delivery (failed apply, undecidable input, unreadable live state).
- a ``delivery_summary`` row with totals plus handoff bookkeeping.

JOIN KEY. Receipts carry the writer fingerprint, which is recomputed here
through ``recovery_writer.Decision`` itself -- not a copy of the formula --
so the join cannot drift from the writer. A ``reset_agent_error`` proposal
fingerprints the way ``decide_agent_reset`` does: empty issue id, target =
agent id, source = run id. A rate-limited receipt rewrites reason and
mutation, so its own fingerprint no longer matches; the writer stamps
``proposalFingerprint`` with the pre-rewrite value and the join accepts
either key. Detector proposals other than agent reset never carry a target
or source id, matching what the writer sees for them. Receipts for
undecidable input and unreadable live state carry no fingerprint (the
writer never got far enough to decide); those match by
(detector, issueId, identifier), first-fit in file order.

REFUSED IS NOT DROPPED. A refused receipt means the writer read the
proposal and the policy correctly declined it -- the policy working, not
a delivery failure. Refusals are counted in the metric and raise no
alert. An issueless ``mutation: none`` proposal (the supply-famine page)
is refused ``undecidable`` by design: counted as refused, not failed, and
not alerted, because no operator action can clear that page. A real
mutation that names no card, a failed apply, and unreadable live state
still alert. Only missing receipts and those failed deliveries alert.

SECRET HYGIENE. Alert rows carry identifiers, reasons, severities, routes
and ages only. Proposal notes and before/after payloads are never echoed:
a future detector note must be free to quote evidence without this module
becoming the leak path.

USAGE.

  python3 -m watchdog.delivery \\
    --detectors detector-out.jsonl --receipts writer-out.jsonl
  python3 -m watchdog.delivery \\
    --detectors detector-out.jsonl --receipts writer-out.jsonl \\
    --now 2026-10-03T16:00:00Z --stall-after-min 60

Both files are assumed to cover the same cycle. Receipts that match no
proposal are counted as orphans in the summary (different cycles or a
hand-fed writer) -- reported, never alerted.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys

try:
    from watchdog.detectors import CEO_ROUTE
    from watchdog.detectors import parse_time as _parse_time
except ImportError:  # invoked as watchdog/delivery.py, not -m
    from detectors import CEO_ROUTE  # type: ignore[no-redef]
    from detectors import parse_time as _parse_time  # type: ignore[no-redef]

try:
    from recovery_writer import Decision
except ImportError:  # direct-script mode: repo root is not on sys.path
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from recovery_writer import Decision  # type: ignore[no-redef]

PHASE = "phase-1-read-only"

ALERT_OWNER = "devops"
STALL_ROUTE = CEO_ROUTE  # single CEO hourly routine judges stalls
FAIL_ROUTE = "devops"  # failed applies go straight to the runner owner

CLEAN_DECISIONS = {"decided", "no_mutation"}
CLEAN_APPLIES = {"applied", "dry_run_held"}
FAILED_DECISIONS = {"refused:undecidable", "refused:live_unreadable"}


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def load_jsonl(path):
    """Read a JSONL file; each line must be a JSON object."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    rows = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path} line {lineno}: not JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path} line {lineno}: want an object")
        rows.append(record)
    return rows


def proposal_fingerprint(proposal):
    """Recompute the writer fingerprint for one detector proposal.

    Mirrors recovery_writer.decide() field handling through the writer's
    own Decision class. An absent mutation reads as "none". A
    ``reset_agent_error`` proposal is fingerprinted the way
    ``decide_agent_reset`` builds the Decision: issue id forced empty,
    target the agent id, source the run id, including the refused shapes.
    Other phase-1 proposals are target-less. A hand-fed proposal whose
    target the writer fills in by redirect is not reconstructed here;
    such rows surface as orphans, never as false matches.
    """
    issue_id = str(proposal.get("issueId") or "")
    identifier = str(proposal.get("identifier") or "?")
    detector = str(proposal.get("detector") or "unknown")
    reason = str(proposal.get("reason") or "unspecified")
    mutation = str(proposal.get("mutation") or "none")
    target = None
    source = None
    if mutation == "reset_agent_error":
        issue_id = ""
        raw_agent = proposal.get("agentId")
        target = str(raw_agent) if raw_agent else None
        raw_run = proposal.get("runId")
        source = str(raw_run) if raw_run else None
    return Decision(issue_id, identifier, detector, reason, mutation,
                    target, source, None, {}, {}, "", "").fingerprint


def _expected_issueless_refusal(proposal, decision):
    """True when an undecidable receipt is the policy, not a dropped handoff.

    ``supply_famine`` (and any other ``mutation: none`` proposal that names
    no card) cannot be decided. The writer records ``refused:undecidable``
    and exits 1. Paging DevOps for that on every sustained famine cycle is
    an alert no operator can clear. A real mutation that names no card is
    still a failed delivery.
    """
    if decision != "refused:undecidable":
        return False
    mutation = str(proposal.get("mutation") or "none")
    issue_id = str(proposal.get("issueId") or "")
    return mutation == "none" and issue_id == ""


def _age_min(now, stamp):
    moment = _parse_time(stamp)
    if moment is None:
        return None
    return (now - moment).total_seconds() / 60.0


def _metric(detector, checked_at):
    return {
        "type": "delivery_metric",
        "detector": detector,
        "emitted": 0,
        "delivered": 0,
        "clean": 0,
        "refused": 0,
        "deferred": 0,
        "failed": 0,
        "missing": 0,
        "successRate": None,
        "medianLatencyMs": None,
        "maxLatencyMs": None,
        "latencySamples": 0,
        "oldestMissingAgeMin": None,
        "phase": PHASE,
        "at": checked_at,
    }


def _alert(detector, reason, severity, owner, route, issue_id,
           identifier, detail, checked_at):
    return {
        "type": "delivery_alert",
        "detector": detector,
        "reason": reason,
        "severity": severity,
        "owner": owner,
        "route": route,
        "issueId": issue_id,
        "identifier": identifier,
        "detail": detail,
        "phase": PHASE,
        "at": checked_at,
    }


def check(detector_rows, writer_rows, now, stall_after_min, checked_at):
    """Join detector proposals to writer receipts.

    Returns (metrics, alerts, summary). Pure: no I/O, no clock reads --
    ``now`` and ``checked_at`` are passed in so tests pin them exactly.
    """
    proposals = [r for r in detector_rows if r.get("type") == "proposal"]
    receipts = [r for r in writer_rows if r.get("type") == "receipt"]

    by_fingerprint = {}
    unfingerprinted = []
    for receipt in receipts:
        keys = []
        fingerprint = receipt.get("fingerprint")
        if fingerprint:
            keys.append(str(fingerprint))
        # Rate-limited receipts rewrite reason and mutation, so their own
        # fingerprint no longer matches the proposal. The writer stamps the
        # pre-rewrite value; index that too, once.
        proposal_fp = receipt.get("proposalFingerprint")
        if proposal_fp and str(proposal_fp) not in keys:
            keys.append(str(proposal_fp))
        if not keys:
            unfingerprinted.append(receipt)
            continue
        for key in keys:
            by_fingerprint.setdefault(key, []).append(receipt)

    metrics = {}
    alerts = []
    latencies = {}  # detector -> [ms]
    consumed = set()

    for proposal in proposals:
        detector = str(proposal.get("detector") or "unknown")
        issue_id = str(proposal.get("issueId") or "")
        identifier = str(proposal.get("identifier") or "?")
        metric = metrics.setdefault(detector, _metric(detector, checked_at))
        metric["emitted"] += 1

        match = None
        for receipt in by_fingerprint.get(proposal_fingerprint(proposal), []):
            if id(receipt) not in consumed:
                match = receipt
                break
        if match is None:
            for receipt in unfingerprinted:
                if id(receipt) in consumed:
                    continue
                if (str(receipt.get("detector") or "unknown") == detector
                        and str(receipt.get("issueId") or "") == issue_id
                        and str(receipt.get("identifier") or "?")
                        == identifier):
                    match = receipt
                    break
        if match is not None:
            consumed.add(id(match))

        if match is None:
            metric["missing"] += 1
            age = _age_min(now, proposal.get("emittedAt"))
            if age is not None:
                oldest = metric["oldestMissingAgeMin"]
                if oldest is None or age > oldest:
                    metric["oldestMissingAgeMin"] = round(age, 1)
            if age is None:
                alerts.append(_alert(
                    detector, "proposal undelivered (no emission timestamp)",
                    "info", ALERT_OWNER, STALL_ROUTE, issue_id, identifier,
                    "No writer receipt and no emittedAt to age it by; "
                    "cannot tell stall from a writer that has not run yet.",
                    checked_at))
            elif age >= stall_after_min:
                alerts.append(_alert(
                    detector, "proposal stalled without writer receipt",
                    "high", ALERT_OWNER, STALL_ROUTE, issue_id, identifier,
                    f"No writer receipt {round(age, 1)} min after emission "
                    f"(threshold {stall_after_min} min); the handoff dropped "
                    "it or the writer never ran.",
                    checked_at))
            else:
                alerts.append(_alert(
                    detector, "proposal undelivered (writer may not have run)",
                    "info", ALERT_OWNER, STALL_ROUTE, issue_id, identifier,
                    f"No writer receipt {round(age, 1)} min after emission; "
                    f"below the {stall_after_min} min stall threshold.",
                    checked_at))
            continue

        metric["delivered"] += 1
        decision = str(match.get("decision") or "")
        apply_outcome = str(match.get("apply") or "")
        if _expected_issueless_refusal(proposal, decision):
            metric["refused"] += 1
        elif (decision in FAILED_DECISIONS
                or apply_outcome.startswith("failed:")):
            metric["failed"] += 1
            alerts.append(_alert(
                detector, "writer failed to deliver proposal",
                "high", ALERT_OWNER, FAIL_ROUTE, issue_id, identifier,
                f"Writer receipt records decision={decision!r} "
                f"apply={apply_outcome!r}; needs an operator look.",
                checked_at))
        elif decision == "rate_limited":
            metric["deferred"] += 1
        elif decision.startswith("refused:"):
            metric["refused"] += 1
        elif decision in CLEAN_DECISIONS or apply_outcome in CLEAN_APPLIES:
            metric["clean"] += 1
        else:
            metric["refused"] += 1

        latency_ms = _delivery_latency_ms(proposal.get("emittedAt"),
                                          match.get("at"))
        if latency_ms is not None:
            latencies.setdefault(detector, []).append(latency_ms)

    seen = set()
    orphans = 0
    for bucket in by_fingerprint.values():
        for receipt in bucket:
            if id(receipt) in consumed or id(receipt) in seen:
                continue
            seen.add(id(receipt))
            orphans += 1
    orphans += sum(1 for receipt in unfingerprinted
                   if id(receipt) not in consumed)

    ordered = []
    for detector in sorted(metrics):
        metric = metrics[detector]
        if metric["emitted"]:
            metric["successRate"] = round(
                metric["delivered"] / metric["emitted"], 3)
        samples = latencies.get(detector, [])
        if samples:
            metric["medianLatencyMs"] = round(statistics.median(samples), 1)
            metric["maxLatencyMs"] = round(max(samples), 1)
            metric["latencySamples"] = len(samples)
        ordered.append(metric)

    writer_consumed = None
    for row in writer_rows:
        if row.get("type") == "cycle" and isinstance(
                row.get("proposals"), int):
            writer_consumed = row["proposals"]
            break
    summary = {
        "type": "delivery_summary",
        "detectors": sorted(metrics),
        "emitted": sum(m["emitted"] for m in ordered),
        "delivered": sum(m["delivered"] for m in ordered),
        "failed": sum(m["failed"] for m in ordered),
        "missing": sum(m["missing"] for m in ordered),
        "alertsHigh": sum(1 for a in alerts if a["severity"] == "high"),
        "alertsInfo": sum(1 for a in alerts if a["severity"] == "info"),
        "orphanReceipts": orphans,
        "writerConsumed": writer_consumed,
        "phase": PHASE,
        "at": checked_at,
    }
    expected = _expected_proposals(detector_rows)
    if expected is not None and expected != summary["emitted"]:
        summary["detectorSummaryMismatch"] = {
            "summarySays": expected, "rowsSeen": summary["emitted"]}
    if orphans:
        summary["note"] = (
            f"{orphans} writer receipt(s) match no detector proposal; "
            "the files likely cover different cycles or the writer ran "
            "on hand-fed input.")
    return ordered, alerts, summary


def _delivery_latency_ms(emitted_at, receipt_at):
    start = _parse_time(emitted_at) if emitted_at else None
    end = _parse_time(receipt_at) if receipt_at else None
    if start is None or end is None:
        return None
    return (end - start).total_seconds() * 1000.0


def _expected_proposals(detector_rows):
    """Proposals count from the detector summary row, if present."""
    for row in detector_rows:
        if row.get("type") == "summary" and isinstance(
                row.get("perDetector"), dict):
            try:
                return sum(int(family.get("proposals", 0))
                           for family in row["perDetector"].values()
                           if isinstance(family, dict))
            except (TypeError, ValueError):
                return None
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Watchdog proposal-delivery observability: offline join "
                    "of detector output and writer receipts. No network, "
                    "no credentials, no writes.")
    parser.add_argument("--detectors", required=True,
                        help="detector JSONL stdout file")
    parser.add_argument("--receipts", required=True,
                        help="writer JSONL stdout file")
    parser.add_argument("--now", default=None,
                        help="reference time ISO (default: current UTC)")
    parser.add_argument("--stall-after-min", type=float, default=60,
                        help="missing older than this is a high stall alert")
    args = parser.parse_args(argv)
    if args.stall_after_min < 0:
        parser.error("--stall-after-min must be non-negative")
    now = _parse_time(args.now) if args.now else None
    if now is None and args.now:
        parser.error("--now is not a readable timestamp")
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    checked_at = _utcnow_iso()
    try:
        detector_rows = load_jsonl(args.detectors)
        writer_rows = load_jsonl(args.receipts)
    except ValueError as exc:
        print(f"delivery: {exc}", file=sys.stderr)
        return 2
    metrics, alerts, summary = check(
        detector_rows, writer_rows, now, args.stall_after_min, checked_at)
    for metric in metrics:
        print(json.dumps(metric, sort_keys=True))
    for alert in alerts:
        print(json.dumps(alert, sort_keys=True))
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary["alertsHigh"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
