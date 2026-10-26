#!/usr/bin/env python3
"""review_routing_weekly.py — weekly expensive-review report (advisory analysis only).

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
    without a valid repo/pr identity is invalid evidence, excluded with
    a reason count (never silently skipped).
  - Decision rows are validated against the emitted routing vocabulary
    (route must be "expensive" or "standard"), positive repo-qualified
    PR identities (repo "owner/name", pr positive int, never bool), and
    field types (ts, reasons, audit_sample) before denominator/dedup
    arithmetic. Unknown/missing routes are invalid evidence, never
    silently cheap.
  - Finding bucket/audit/ts fields are validated before hashing/sorting.
    Missing bucket falls back to "other" deliberately; missing ts falls
    back to the decision date; truly undated findings retain the
    conservative promotion fallback (counted as recent). Wrong
    types/unsupported shapes are invalid evidence, excluded without
    crash or silent contamination.
  - Promotion evidence uses both lower cutoff and --now upper bound:
    dated findings count only when cutoff <= ts <= now (inclusive).
    Future findings never trigger last-N-days candidates.

Also prints the finding-bucket histogram and promotion candidates:
non-Tier-2 buckets with >= 3 findings in the last --promotion-days (default
90) days. A finding's date is its own "ts", else the ts of its PR's ledger
row; a finding with neither is counted (promoting early is the safe error).

This is an advisory analysis report, not an authoritative gate or
replacement for repository rules. It never changes which review gates run
and never automatically promotes a rule.

Offline, stdlib only. No router, rule writer, network, credential or board
mutation is ever invoked, including for invalid flags or --dry-run.
Usage:
  review_routing_weekly.py --ledger LEDGER.jsonl [--findings FINDINGS.jsonl]
                           [--promotion-days 90] [--now 2026-10-09T00:00:00Z]
                           [--json] [--dry-run] [--by-repo]
  --by-repo is valid only with --json and adds a versioned
  repository_breakdown projection over the same validated records; the
  default text/JSON output is unchanged without it.
Offline check:
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_review_report_input_integrity
  bash test_review_route.sh
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
MAX_PROMOTION_DAYS = 36500
VALID_ROUTES = {"expensive", "standard"}
SCHEMA_VERSION = 1


def parse_ts(value):
    """ISO-8601 UTC ('...Z' or offset) to an aware datetime, else None."""
    if not isinstance(value, str):
        return None
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def format_ts(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def valid_repo(repo):
    if not isinstance(repo, str):
        return False
    if not repo:
        return False
    if repo != repo.strip():
        return False
    for ch in repo:
        if ch.isspace():
            return False
    if repo.count("/") != 1:
        return False
    owner, name = repo.split("/", 1)
    if not owner or not name:
        return False
    return True


def valid_pr(pr):
    if isinstance(pr, bool):
        return False
    if type(pr) is not int:
        return False
    return pr >= 1


def valid_reasons(reasons):
    if isinstance(reasons, str):
        return True
    if isinstance(reasons, list):
        return all(isinstance(x, str) for x in reasons)
    return False


def valid_audit_sample(value):
    return value in ("yes", "no")


def validate_decision(row):
    """Return (ok, code, detail). First failure wins; codes are actionable."""
    repo = row.get("repo")
    if not valid_repo(repo):
        return (False, "invalid_repo", "repo must be 'owner/name'")
    pr = row.get("pr")
    # Missing pr -> .get returns None -> invalid_pr. Bool/float/str/<=0 invalid.
    if not valid_pr(pr):
        return (False, "invalid_pr", "pr must be a positive int (never bool)")
    route = row.get("route")
    if not isinstance(route, str) or route not in VALID_ROUTES:
        return (False, "invalid_route", "route must be 'expensive' or 'standard'")
    if "ts" in row:
        if parse_ts(row.get("ts")) is None:
            return (False, "invalid_field", "ts must be ISO-8601 or absent")
    if "reasons" in row:
        if not valid_reasons(row.get("reasons")):
            return (False, "invalid_field", "reasons must be str or [str]")
    if "audit_sample" in row:
        if not valid_audit_sample(row.get("audit_sample")):
            return (False, "invalid_field", "audit_sample must be 'yes'|'no' or absent")
    return (True, None, None)


def validate_finding(row):
    repo = row.get("repo")
    if not valid_repo(repo):
        return (False, "invalid_repo", "repo must be 'owner/name'")
    pr = row.get("pr")
    if not valid_pr(pr):
        return (False, "invalid_pr", "pr must be a positive int (never bool)")
    if "bucket" in row:
        bucket = row.get("bucket")
        if not isinstance(bucket, str) or not bucket or not bucket.strip():
            return (False, "invalid_bucket", "bucket must be a non-empty string or absent")
    if "audit_only" in row:
        if type(row.get("audit_only")) is not bool:
            return (False, "invalid_audit", "audit_only must be true|false or absent")
    if "ts" in row:
        if parse_ts(row.get("ts")) is None:
            return (False, "invalid_field", "ts must be ISO-8601 or absent")
    return (True, None, None)


def pr_key(row):
    repo, pr = row.get("repo"), row.get("pr")
    if not valid_repo(repo) or not valid_pr(pr):
        return None
    return (repo, pr)


def load_jsonl_tracked(path):
    """Return (objects, parse_errors). objects=[(lineno,row)]. Blank lines skipped.

    parse_errors=[(lineno, code, detail)]. Raises OSError if the file cannot
    be opened; caller converts to a structured missing_input result.
    """
    objects = []
    parse_errors = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors.append((lineno, "invalid_json", f"unparsable row ({exc})"))
                continue
            if not isinstance(row, dict):
                parse_errors.append((lineno, "non_object", "non-object row"))
                continue
            objects.append((lineno, row))
    return objects, parse_errors


def collapse_ledger(objects, path, reason_counts, errors_detail):
    """One record per (repo, pr) from validated rows; invalid excluded.

    reason_counts is a Counter mutated in place. errors_detail collects
    (code, detail) per excluded row for warnings. Returns (prs, invalid_count).
    prs maps (repo, pr) -> {"expensive": bool, "ts": datetime|None}.
    """
    prs = {}
    invalid = 0
    for lineno, row in objects:
        ok, code, detail = validate_decision(row)
        if not ok:
            invalid += 1
            reason_counts[code] += 1
            errors_detail.append((code, f"{path}:{lineno}: {detail}"))
            print(f"warn: {path}:{lineno}: skip invalid decision ({code}: {detail})",
                  file=sys.stderr)
            continue
        key = (row["repo"], row["pr"])
        rec = prs.setdefault(key, {"expensive": False, "ts": None})
        if row.get("route") == "expensive":
            rec["expensive"] = True
        ts = parse_ts(row.get("ts")) if "ts" in row else None
        if ts is not None and (rec["ts"] is None or ts > rec["ts"]):
            rec["ts"] = ts
    return prs, invalid


def collapse_findings(objects, path, reason_counts, errors_detail):
    """Validate findings; return (valid_list, invalid_count).

    valid_list entries: {"key": (repo,pr), "bucket": str, "audit_only": bool,
    "ts": datetime|None}. Missing bucket -> "other"; missing audit_only ->
    False; missing ts -> None (undated, falls back later).
    """
    valid = []
    invalid = 0
    for lineno, row in objects:
        ok, code, detail = validate_finding(row)
        if not ok:
            invalid += 1
            reason_counts[code] += 1
            errors_detail.append((code, f"{path}:{lineno}: {detail}"))
            print(f"warn: {path}:{lineno}: skip invalid finding ({code}: {detail})",
                  file=sys.stderr)
            continue
        key = (row["repo"], row["pr"])
        bucket = row.get("bucket", "other")
        if "bucket" not in row:
            bucket = "other"
        audit_only = row.get("audit_only", False)
        if "audit_only" not in row:
            audit_only = False
        ts = parse_ts(row.get("ts")) if "ts" in row else None
        valid.append({"key": key, "bucket": bucket, "audit_only": audit_only, "ts": ts})
    return valid, invalid


def repository_breakdown(status, counts_state, invalid_decisions,
                           invalid_findings, excluded_rows, repositories):
    """Versioned additive per-repository projection (schema 1)."""
    return {
        "schema_version": 1,
        "status": status,
        "counts_state": counts_state,
        "unattributed_counts": {
            "invalid_decisions": invalid_decisions,
            "invalid_findings": invalid_findings,
            "excluded_rows": excluded_rows,
        },
        "repositories": repositories,
    }


def not_evaluated_breakdown(counts_state="unknown", invalid_decisions=0,
                              invalid_findings=0, excluded_rows=0):
    """Breakdown unavailable: no invented repository rows."""
    return repository_breakdown(
        "not_evaluated", counts_state, invalid_decisions, invalid_findings,
        excluded_rows, [])


def aggregate_repositories(prs, expensive, findings_valid):
    """One deterministic row per repo in validated decisions/findings.

    Reuses the global semantics exactly: prs is already deduped by
    (repo, pr) with any-expensive-wins, findings are not deduped, and a
    finding matches only when its (repo, pr) is expensive-routed. Shares
    are recomputed from each repository's own denominators (null at zero).
    """
    routed = collections.Counter(repo for repo, _pr in prs)
    expensive_reviews = collections.Counter(repo for repo, _pr in expensive)
    matched = collections.Counter()
    unmatched = collections.Counter()
    audit_defects = collections.Counter()
    buckets = collections.defaultdict(collections.Counter)
    for f in findings_valid:
        repo = f["key"][0]
        buckets[repo][f["bucket"]] += 1
        if f["key"] in expensive:
            matched[repo] += 1
            if f["audit_only"] is True:
                audit_defects[repo] += 1
        else:
            unmatched[repo] += 1
    repos = sorted(set(routed) | set(buckets))
    rows = []
    for repo in repos:
        rows.append({
            "repo": repo,
            "counts": {
                "routed_prs": routed[repo],
                "expensive_reviews": expensive_reviews[repo],
                "matched_findings": matched[repo],
                "unmatched_findings": unmatched[repo],
            },
            "metrics": {
                "expensive_review_share": (
                    (expensive_reviews[repo] / routed[repo])
                    if routed[repo] else None),
                "findings_per_expensive_review": (
                    (matched[repo] / expensive_reviews[repo])
                    if expensive_reviews[repo] else None),
                "audit_sample_defects": audit_defects[repo],
            },
            "buckets": [{"id": b, "count": c}
                        for b, c in sorted(buckets[repo].items())],
        })
    return rows


class JsonErrorParser(argparse.ArgumentParser):
    """ArgumentParser that emits versioned JSON for usage errors when --json."""

    def error(self, message):  # noqa: D102 - argparse override
        if "--json" in sys.argv:
            data = {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid",
                "counts_state": "unknown",
                "counts": {
                    "routed_prs": 0,
                    "expensive_reviews": 0,
                    "matched_findings": 0,
                    "unmatched_findings": 0,
                    "invalid_decisions": 0,
                    "invalid_findings": 0,
                    "excluded_rows": 0,
                },
                "metrics": {
                    "expensive_review_share": None,
                    "findings_per_expensive_review": None,
                    "audit_sample_defects": 0,
                },
                "source": {
                    "kind": "supplied",
                    "ledger": None,
                    "findings": None,
                    "window_verified": False,
                },
                "window": {"now": None, "cutoff": None, "promotion_days": None},
                "prs": [],
                "buckets": [],
                "promotion_candidates": [],
                "reason_counts": {},
                "errors": [{"code": "invalid_argument", "message": message}],
                "retryable": False,
                "next_action": "Fix arguments and re-run; see errors.",
            }
            if "--by-repo" in sys.argv:
                data["repository_breakdown"] = not_evaluated_breakdown()
            print(json.dumps(data, sort_keys=True))
            self.exit(2)
        super().error(message)


def json_usage_error(message, ledger=None, findings=None, by_repo=False):
    data = {
        "schema_version": SCHEMA_VERSION,
        "status": "invalid",
        "counts_state": "unknown",
        "counts": {
            "routed_prs": 0,
            "expensive_reviews": 0,
            "matched_findings": 0,
            "unmatched_findings": 0,
            "invalid_decisions": 0,
            "invalid_findings": 0,
            "excluded_rows": 0,
        },
        "metrics": {
            "expensive_review_share": None,
            "findings_per_expensive_review": None,
            "audit_sample_defects": 0,
        },
        "source": {
            "kind": "supplied",
            "ledger": ledger,
            "findings": findings,
            "window_verified": False,
        },
        "window": {"now": None, "cutoff": None, "promotion_days": None},
        "prs": [],
        "buckets": [],
        "promotion_candidates": [],
        "reason_counts": {},
        "errors": [{"code": "invalid_argument", "message": message}],
        "retryable": False,
        "next_action": "Fix arguments and re-run; see errors.",
    }
    if by_repo:
        data["repository_breakdown"] = not_evaluated_breakdown()
    print(json.dumps(data, sort_keys=True))
    return 2


def json_missing_input(ledger, findings, detail, by_repo=False):
    data = {
        "schema_version": SCHEMA_VERSION,
        "status": "invalid",
        "counts_state": "unknown",
        "counts": {
            "routed_prs": 0,
            "expensive_reviews": 0,
            "matched_findings": 0,
            "unmatched_findings": 0,
            "invalid_decisions": 0,
            "invalid_findings": 0,
            "excluded_rows": 0,
        },
        "metrics": {
            "expensive_review_share": None,
            "findings_per_expensive_review": None,
            "audit_sample_defects": 0,
        },
        "source": {
            "kind": "supplied",
            "ledger": ledger,
            "findings": findings,
            "window_verified": False,
        },
        "window": {"now": None, "cutoff": None, "promotion_days": None},
        "prs": [],
        "buckets": [],
        "promotion_candidates": [],
        "reason_counts": {},
        "errors": [{"code": "missing_input", "message": detail}],
        "retryable": False,
        "next_action": "Supply the missing JSONL file and re-run.",
    }
    if by_repo:
        data["repository_breakdown"] = not_evaluated_breakdown()
    print(json.dumps(data, sort_keys=True))
    return 1


def build_parser():
    ap = JsonErrorParser(
        description=(
            "Weekly expensive-review report (advisory analysis only, not a gate). "
            "Validates supplied ledger evidence before arithmetic; invalid rows are "
            "excluded with reason counts, never silently cheap. Offline check: "
            "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v "
            "test_review_report_input_integrity"
        ),
        epilog=(
            "Advisory, not authoritative: this analysis never changes review gates "
            "and never promotes a rule automatically. --json emits versioned "
            "schema_version 1 with status/counts_state/counts/metrics/source/window/"
            "prs/buckets/promotion_candidates/reason_counts/errors/retryable/"
            "next_action. --dry-run is an offline read-only replay alias. "
            "Example: review_routing_weekly.py --ledger LEDGER.jsonl "
            "--findings FINDINGS.jsonl --json --by-repo"
        ),
    )
    ap.add_argument("--ledger", required=True, help="Decision JSONL path (required)")
    ap.add_argument("--findings", default=None, help="Findings JSONL path (optional)")
    ap.add_argument("--promotion-days", type=int, default=PROMOTION_DAYS,
                    help="Promotion evidence window in days (1..36500, default 90)")
    ap.add_argument("--now", default=None,
                    help="ISO-8601 UTC reference time (default: current time)")
    ap.add_argument("--json", action="store_true",
                    help="Emit versioned JSON v1 (stable IDs, provenance, errors)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Offline read-only replay alias (identical output, zero writes)")
    ap.add_argument("--by-repo", action="store_true",
                    help="With --json only: add versioned repository_breakdown "
                         "per-repository totals reconciling with global counts")
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    # Prompt argument validation, before any file reads. Invalid flags or
    # values never invoke a router, rule writer, network, credential or
    # board mutation (this report performs none).
    if args.by_repo and not args.json:
        print("error: --by-repo requires --json (repository breakdown is "
              "JSON-only)", file=sys.stderr)
        print("next_action: Re-run with --json --by-repo; see --help.",
              file=sys.stderr)
        return 2
    if not isinstance(args.promotion_days, int) or isinstance(args.promotion_days, bool):
        msg = "--promotion-days must be an int in 1..36500"
        if args.json:
            return json_usage_error(msg, args.ledger, args.findings, by_repo=args.by_repo)
        ap.error(msg)
    if args.promotion_days < 1 or args.promotion_days > MAX_PROMOTION_DAYS:
        msg = (f"--promotion-days (promotion window) must be 1..{MAX_PROMOTION_DAYS}, "
               f"got {args.promotion_days}")
        if args.json:
            return json_usage_error(msg, args.ledger, args.findings, by_repo=args.by_repo)
        ap.error(msg)

    if args.now is not None:
        now = parse_ts(args.now)
        if now is None:
            msg = f"--now is not an ISO-8601 timestamp: {args.now}"
            if args.json:
                return json_usage_error(msg, args.ledger, args.findings, by_repo=args.by_repo)
            ap.error(msg)
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
    try:
        cutoff = now - datetime.timedelta(days=args.promotion_days)
    except (OverflowError, ValueError, OSError) as exc:
        msg = f"--now/--promotion-days cutoff is unrepresentable ({exc})"
        if args.json:
            return json_usage_error(msg, args.ledger, args.findings, by_repo=args.by_repo)
        ap.error(msg)

    reason_counts = collections.Counter()
    errors_detail = []

    # Load ledgers. Missing/unreadable files are structured errors, not
    # tracebacks. --dry-run performs the same offline reads and no writes.
    try:
        ledger_objects, ledger_parse_errors = load_jsonl_tracked(args.ledger)
    except OSError as exc:
        detail = f"cannot read --ledger {args.ledger}: {exc}"
        print(f"error: invalid input: {detail}", file=sys.stderr)
        if args.json:
            return json_missing_input(args.ledger, args.findings, detail, by_repo=args.by_repo)
        print("status: invalid (missing input; partial report unavailable)")
        print(f"excluded_rows: 0")
        return 1
    for lineno, code, detail in ledger_parse_errors:
        reason_counts[code] += 1
        errors_detail.append((code, f"{args.ledger}:{lineno}: {detail}"))
        print(f"warn: {args.ledger}:{lineno}: skip {detail} ({code})", file=sys.stderr)
    invalid_decisions_parse = len(ledger_parse_errors)

    findings_objects = []
    findings_parse_errors = []
    if args.findings:
        try:
            findings_objects, findings_parse_errors = load_jsonl_tracked(args.findings)
        except OSError as exc:
            detail = f"cannot read --findings {args.findings}: {exc}"
            print(f"error: invalid input: {detail}", file=sys.stderr)
            if args.json:
                return json_missing_input(args.ledger, args.findings, detail, by_repo=args.by_repo)
            print("status: invalid (missing input; partial report unavailable)")
            print("excluded_rows: 0")
            return 1
        for lineno, code, detail in findings_parse_errors:
            reason_counts[code] += 1
            errors_detail.append((code, f"{args.findings}:{lineno}: {detail}"))
            print(f"warn: {args.findings}:{lineno}: skip {detail} ({code})",
                  file=sys.stderr)
    invalid_findings_parse = len(findings_parse_errors)

    # Validate findings before the empty-ledger early return: content-invalid
    # findings are excluded evidence with reason counts even when no decision
    # rows were supplied (never silently skipped).
    findings_valid, invalid_findings_content = collapse_findings(
        findings_objects, args.findings or "(findings)", reason_counts,
        errors_detail)
    invalid_findings = invalid_findings_parse + invalid_findings_content

    total_supplied_rows = len(ledger_objects) + invalid_decisions_parse
    # Empty decision ledger is unknown, never a verified zero-spend week.
    if total_supplied_rows == 0:
        if args.json:
            data = {
                "schema_version": SCHEMA_VERSION,
                "status": "unknown",
                "counts_state": "unknown",
                "counts": {
                    "routed_prs": 0,
                    "expensive_reviews": 0,
                    "matched_findings": 0,
                    "unmatched_findings": 0,
                    "invalid_decisions": 0,
                    "invalid_findings": invalid_findings,
                    "excluded_rows": invalid_findings,
                },
                "metrics": {
                    "expensive_review_share": None,
                    "findings_per_expensive_review": None,
                    "audit_sample_defects": 0,
                },
                "source": {
                    "kind": "supplied",
                    "ledger": args.ledger,
                    "findings": args.findings,
                    "window_verified": False,
                },
                "window": {
                    "now": format_ts(now),
                    "cutoff": format_ts(cutoff),
                    "promotion_days": args.promotion_days,
                },
                "prs": [],
                "buckets": [],
                "promotion_candidates": [],
                "reason_counts": dict(sorted(reason_counts.items())),
                "errors": [{"code": "empty_ledger",
                            "message": "empty decision ledger is unknown, not a verified zero-spend week"}],
                "retryable": False,
                "next_action": "Supply a non-empty decision ledger and re-run.",
            }
            # Preserve parse-error codes alongside empty_ledger when findings
            # also had parse failures.
            for code in sorted(reason_counts):
                if code not in [e["code"] for e in data["errors"]]:
                    data["errors"].append({"code": code, "count": reason_counts[code]})
            data["errors"] = sorted(data["errors"], key=lambda e: e["code"])
            if args.by_repo:
                data["repository_breakdown"] = not_evaluated_breakdown(
                    counts_state="unknown", invalid_decisions=0,
                    invalid_findings=invalid_findings,
                    excluded_rows=invalid_findings)
            print(json.dumps(data, sort_keys=True))
            return 1
        print("routed_prs: 0")
        print("expensive_reviews: 0")
        print("expensive_review_share: unknown")
        print("findings_per_expensive_review: unknown")
        print("audit_sample_defects: 0")
        print("bucket_histogram:")
        print("promotion_candidates: none")
        print("status: unknown (partial; empty ledger is not a verified zero-spend week)")
        print(f"excluded_rows: {invalid_findings}")
        if reason_counts:
            print("reason_counts: " + ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items())))
        return 1

    prs, invalid_decisions_content = collapse_ledger(
        ledger_objects, args.ledger, reason_counts, errors_detail)
    invalid_decisions = invalid_decisions_parse + invalid_decisions_content
    # findings_valid / invalid_findings computed above (before the
    # empty-ledger early return) so invalid findings are never skipped.

    total = len(prs)
    expensive = {k for k, v in prs.items() if v["expensive"]}
    share = (len(expensive) / total) if total else None

    exp_findings = [f for f in findings_valid if f["key"] in expensive]
    matched = len(exp_findings)
    unmatched = len(findings_valid) - matched
    per_review = (matched / len(expensive)) if expensive else None
    audit_catches = [f for f in exp_findings if f["audit_only"] is True]

    buckets = collections.Counter(f["bucket"] for f in findings_valid)
    recent = collections.Counter()
    for f in findings_valid:
        ts = f["ts"]
        if ts is None:
            ts = (prs.get(f["key"]) or {}).get("ts")
        if ts is None or (cutoff <= ts <= now):
            recent[f["bucket"]] += 1
    candidates = sorted(
        b for b, c in recent.items()
        if b in NON_TIER2_PROMOTABLE and c >= PROMOTION_THRESHOLD)

    excluded = invalid_decisions + invalid_findings
    has_invalid = excluded > 0

    if args.json:
        if has_invalid or total == 0:
            # Malformed supplied data prevents a complete report: explicitly
            # partial (or unknown when nothing valid remains), nonzero exit.
            # Zero valid rows is never a verified zero-spend week.
            if total == 0 and invalid_decisions == 0 and invalid_findings == 0:
                status, counts_state = "unknown", "unknown"
            elif total == 0 and excluded > 0:
                status, counts_state = "partial", "partial"
            else:
                status, counts_state = "partial", "partial"
        else:
            status, counts_state = "ok", "measured"
        prs_sorted = sorted(prs.items(), key=lambda kv: (kv[0][0], kv[0][1]))
        data = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "counts_state": counts_state,
            "counts": {
                "routed_prs": total,
                "expensive_reviews": len(expensive),
                "matched_findings": matched,
                "unmatched_findings": unmatched,
                "invalid_decisions": invalid_decisions,
                "invalid_findings": invalid_findings,
                "excluded_rows": excluded,
            },
            "metrics": {
                "expensive_review_share": share,
                "findings_per_expensive_review": per_review,
                "audit_sample_defects": len(audit_catches),
            },
            "source": {
                "kind": "supplied",
                "ledger": args.ledger,
                "findings": args.findings,
                "window_verified": False,
            },
            "window": {
                "now": format_ts(now),
                "cutoff": format_ts(cutoff),
                "promotion_days": args.promotion_days,
            },
            "prs": [{"id": f"{repo}#{pr}", "repo": repo, "pr": pr,
                     "expensive": prs[(repo, pr)]["expensive"]}
                    for (repo, pr), _ in prs_sorted],
            "buckets": [{"id": b, "count": c} for b, c in sorted(buckets.items())],
            "promotion_candidates": [{"id": b, "count": recent[b]} for b in candidates],
            "reason_counts": dict(sorted(reason_counts.items())),
            "errors": [{"code": code, "count": count}
                       for code, count in sorted(reason_counts.items())],
            "retryable": False,
            "next_action": (
                "No action required; advisory report only." if status == "ok"
                else "Fix invalid supplied rows (see errors/reason_counts) and re-run; "
                     "valid-row metrics are explicitly partial."
            ),
        }
        if args.by_repo:
            section_status = {"ok": "ok", "partial": "partial"}.get(
                status, "not_evaluated")
            repositories = (
                aggregate_repositories(prs, expensive, findings_valid)
                if section_status != "not_evaluated" else [])
            data["repository_breakdown"] = repository_breakdown(
                section_status, counts_state, invalid_decisions,
                invalid_findings, excluded, repositories)
        print(json.dumps(data, sort_keys=True))
        return 0 if status == "ok" else 1

    # Readable text output (existing labels preserved).
    print(f"routed_prs: {total}")
    print(f"expensive_reviews: {len(expensive)}")
    if share is None:
        print("expensive_review_share: unknown")
    else:
        print(f"expensive_review_share: {share:.3f}")
    if per_review is None:
        print("findings_per_expensive_review: unknown")
    else:
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
    if has_invalid or total == 0:
        if total == 0 and excluded == 0:
            print("status: unknown (partial; empty ledger is not a verified zero-spend week)")
        elif total == 0:
            print("status: partial (invalid; no valid PRs remain)")
        else:
            print("status: partial (invalid rows excluded; valid-row metrics only)")
        print(f"excluded_rows: {excluded}")
        print(f"invalid_decisions: {invalid_decisions}")
        print(f"invalid_findings: {invalid_findings}")
        if reason_counts:
            print("reason_counts: " + ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items())))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
