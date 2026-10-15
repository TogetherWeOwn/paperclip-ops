#!/usr/bin/env python3
"""review_routing_weekly.py — weekly expensive-review report (case_6bf2cca9).

Reads the router ledger (JSONL from review_route.sh --ledger) plus an
optional findings ledger (one row per expensive-review finding:
{"repo": "owner/name", "pr": N, "bucket": "<id from review-finding-buckets.json>",
"audit_only": true|false, "ts": "<ISO-8601 UTC, optional>"}) and prints the
three required metrics:

  1. expensive-review share (expensive PRs / all routed PRs);
  2. findings per expensive review;
  3. defects found by the audit sample (findings with audit_only=true).

Rules that keep the numbers honest:
  - A PR is identified by (repo, pr); PR numbers repeat across repos.
  - A PR routed more than once counts once; it is expensive if any of its
    decisions was expensive (the review spend was incurred).
  - Findings join only to expensive PRs of the same repo. A finding row
    without "repo" and "pr" is skipped with a warning.

Also prints the finding-bucket histogram and promotion candidates:
non-Tier-2 buckets with >= 3 findings in the last --promotion-days (default
90) days. A finding's date is its own "ts", else the ts of its PR's ledger
row; a finding with neither is counted (promoting early is the safe error).

Offline, stdlib only. Usage:
  review_routing_weekly.py --ledger LEDGER.jsonl [--findings FINDINGS.jsonl]
                           [--promotion-days 90] [--now 2026-10-09T00:00:00Z]
"""
import argparse
import collections
import datetime
import json
import sys

NON_TIER2_PROMOTABLE = {
    "tier0-semgrep", "tier0-lint", "tier0-tests",
    "tier1-docs-diff", "tier1-ac-evidence", "tier1-claims-diff",
}
PROMOTION_THRESHOLD = 3
PROMOTION_DAYS = 90


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"warn: {path}:{lineno}: skip unparsable row ({exc})",
                      file=sys.stderr)
                continue
            if not isinstance(row, dict):
                print(f"warn: {path}:{lineno}: skip non-object row",
                      file=sys.stderr)
                continue
            rows.append(row)
    return rows


def parse_ts(value):
    """ISO-8601 UTC ('...Z' or offset) to an aware datetime, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def pr_key(row):
    repo, pr = row.get("repo"), row.get("pr")
    if not isinstance(repo, str) or not repo or isinstance(pr, bool) \
            or not isinstance(pr, int):
        return None
    return (repo, pr)


def collapse_ledger(rows, path):
    """One record per (repo, pr): expensive if any decision was expensive."""
    prs = {}
    for lineno, row in enumerate(rows, 1):
        key = pr_key(row)
        if key is None:
            print(f"warn: {path}: skip ledger row {lineno} without repo/pr",
                  file=sys.stderr)
            continue
        rec = prs.setdefault(key, {"expensive": False, "ts": None})
        if row.get("route") == "expensive":
            rec["expensive"] = True
        ts = parse_ts(row.get("ts"))
        if ts is not None and (rec["ts"] is None or ts > rec["ts"]):
            rec["ts"] = ts
    return prs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--findings", default=None)
    ap.add_argument("--promotion-days", type=int, default=PROMOTION_DAYS)
    ap.add_argument("--now", default=None,
                    help="ISO-8601 UTC reference time (default: current time)")
    args = ap.parse_args()

    now = parse_ts(args.now) if args.now else datetime.datetime.now(
        datetime.timezone.utc)
    if now is None:
        ap.error(f"--now is not an ISO-8601 timestamp: {args.now}")
    cutoff = now - datetime.timedelta(days=args.promotion_days)

    prs = collapse_ledger(load_jsonl(args.ledger), args.ledger)
    total = len(prs)
    expensive = {k for k, v in prs.items() if v["expensive"]}
    share = (len(expensive) / total) if total else 0.0

    findings = []
    if args.findings:
        for lineno, row in enumerate(load_jsonl(args.findings), 1):
            if pr_key(row) is None:
                print(f"warn: {args.findings}: skip finding {lineno} "
                      "without repo/pr", file=sys.stderr)
                continue
            findings.append(row)
    exp_findings = [f for f in findings if pr_key(f) in expensive]
    per_review = (len(exp_findings) / len(expensive)) if expensive else 0.0
    audit_catches = [f for f in exp_findings if f.get("audit_only") is True]

    buckets = collections.Counter(f.get("bucket", "other") for f in findings)
    recent = collections.Counter()
    for f in findings:
        ts = parse_ts(f.get("ts")) or (prs.get(pr_key(f)) or {}).get("ts")
        if ts is None or ts >= cutoff:
            recent[f.get("bucket", "other")] += 1
    candidates = sorted(
        b for b, c in recent.items()
        if b in NON_TIER2_PROMOTABLE and c >= PROMOTION_THRESHOLD)

    print(f"routed_prs: {total}")
    print(f"expensive_reviews: {len(expensive)}")
    print(f"expensive_review_share: {share:.3f}")
    print(f"findings_per_expensive_review: {per_review:.2f}")
    print(f"audit_sample_defects: {len(audit_catches)}")
    print("bucket_histogram:")
    for bucket, count in sorted(buckets.items()):
        print(f"  {bucket}: {count}")
    if candidates:
        print(f"promotion_candidates (>={PROMOTION_THRESHOLD} in "
              f"{args.promotion_days}d, becomes Semgrep/lint rule or "
              "Tier-1 prompt):")
        for bucket in candidates:
            print(f"  {bucket}: {recent[bucket]}")
    else:
        print("promotion_candidates: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
