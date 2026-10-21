#!/usr/bin/env python3
"""ax-metrics: weekly Agent Experience metrics reporter (v0 scaffold).

Collects the three AX outcome metrics over a trailing window and
prints one JSON report to stdout:

* ``first_push_green_rate`` — merged PRs whose FIRST commit already
  had green combined status and check runs, over merged PRs with any
  status or check run. A v0 proxy for first-attempt success, from
  the GitHub API.
* ``tokens_per_merged_pr`` — Paperclip cost-window tokens over merged
  PRs. Experimental: reported only when Paperclip credentials are
  present; otherwise null with a reason.
* ``escalations_per_100_cards`` — cards currently ``blocked`` per 100
  cards touched in the window. A v0 proxy for escalations, from the
  Paperclip API when credentials are present.

Every metric carries its definition and provenance in the output, so
a reader can tell a proxy from a direct measure. Fetch failures fail
the run (exit 1) with the failing source named; a missing optional
credential degrades that metric to null, never to zero. Hermetic
stdlib only. ``--from-file`` replays a fixture instead of fetching,
which is how the offline tests drive the compute path.

Every metric also carries a machine-readable validity envelope
(validity, reason_code, reason, next_action, completeness,
source_window, numerator/denominator plus their sources, and counts
when partial) so consumers never scrape prose. tokens_per_merged_pr
is valid only when the cost evidence carries an explicit
window_start/window_end demonstrably matching the report window;
bare totals, mismatching windows, and unsupported periods withhold
the ratio. Truncated or failed issue scans withhold the escalation
metric instead of yielding an ordinary complete-population value.
"""

import argparse
import datetime
import json
import math
import os
import sys
import urllib.parse
import urllib.request

SCHEMA_VERSION = 2

MAX_PRS_PER_REPO = 30
MAX_ISSUE_PAGES = 5
ISSUES_PER_PAGE = 100

# Period labels that can never verify a report window. Anything else
# without an explicit window_start/window_end pair is unverified, not
# inferred: weekly semantics are never read out of a bare total.
UNSUPPORTED_PERIODS = frozenset({
    "all-time", "all_time", "alltime", "all",
    "unknown", "monthly", "month", "yearly", "annual",
    "last-90-days", "last_90_days", "last90days", "90d",
    "last-month", "last_year",
})

_FAILURE_HINTS = ("fail", "error", "exception", "timeout", "timed out",
                  "500", "502", "503", "504", "400", "401", "403", "404")


def _http_get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _github_headers(token):
    return {"Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28"}


def fetch_merged_prs(repo, since_iso, token):
    """Merged PRs in window: (sample, truncated, total).

    ``sample`` holds up to MAX_PRS_PER_REPO [{number, merged_at}] for
    first-commit status sampling; ``total`` is the search response's
    total_count, which drives the merged_prs metric so windows over
    30 PRs are not undercounted. (Search caps total_count at 1000;
    weekly windows stay far below that.)
    """
    query = f"repo:{repo} is:pr is:merged merged:>={since_iso[:10]}"
    url = ("https://api.github.com/search/issues?q="
           + urllib.parse.quote(query) + "&per_page=100")
    try:
        data = _http_get_json(url, _github_headers(token))
    except Exception as exc:
        raise RuntimeError(f"github search failed for {repo}: {exc}")
    items = data.get("items", [])
    prs = [{"number": it["number"],
            "merged_at": (it.get("closed_at") or "")}
           for it in items if "number" in it]
    total = data.get("total_count", len(prs))
    if not isinstance(total, int):
        total = len(prs)
    sample = prs[:MAX_PRS_PER_REPO]
    return sample, total > len(sample), total


def combine_commit_state(status_payload, check_runs):
    """Merge legacy combined status with check runs into one state.

    The legacy /status endpoint rolls up commit statuses only: a
    commit whose CI runs purely on Actions reports ``pending`` with
    no statuses. Check runs carry the real signal there, so any
    non-success conclusion (failure, timed_out, cancelled, stale,
    startup_failure, action_required, or future values) is a
    failure vote; neutral/skipped cast no vote; queued or
    in-progress runs vote pending. Failure wins, success needs a
    unanimous ballot, and no evidence at all returns None.
    """
    status_payload = status_payload or {}
    votes = []
    legacy = status_payload.get("state")
    if legacy in ("success", "failure", "error"):
        votes.append("success" if legacy == "success" else "failure")
    elif legacy == "pending" and status_payload.get("total_count"):
        votes.append("pending")
    for run in check_runs or []:
        conclusion = run.get("conclusion")
        if conclusion == "success":
            votes.append("success")
        elif conclusion in ("neutral", "skipped"):
            continue
        elif conclusion is not None:
            votes.append("failure")
        elif (run.get("status") or "") in ("queued", "in_progress",
                                           "waiting", "pending",
                                           "requested"):
            votes.append("pending")
    if "failure" in votes:
        return "failure"
    if votes and all(v == "success" for v in votes):
        return "success"
    if votes:
        return "pending"
    return None


def fetch_first_commit_state(repo, pr_number, token):
    """First-commit state from legacy status plus check runs, or None."""
    commits_url = (f"https://api.github.com/repos/{repo}/pulls/"
                   f"{pr_number}/commits?per_page=1")
    try:
        commits = _http_get_json(commits_url, _github_headers(token))
        if not commits:
            return None
        sha = commits[0]["sha"]
        status_url = (f"https://api.github.com/repos/{repo}/commits/"
                      f"{sha}/status")
        runs_url = (f"https://api.github.com/repos/{repo}/commits/"
                    f"{sha}/check-runs?per_page=100")
        status = _http_get_json(status_url, _github_headers(token))
        runs = _http_get_json(runs_url, _github_headers(token))
        return combine_commit_state(status, runs.get("check_runs", []))
    except Exception as exc:
        raise RuntimeError(f"github status failed for {repo}#{pr_number}: {exc}")


def paperclip_get(base_url, api_key, path):
    """Defensive Paperclip GET: (parsed JSON or None, reason or None)."""
    url = base_url.rstrip("/") + path
    # The key travels in the header only; it is never logged or echoed.
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def fetch_paperclip_issues(base_url, api_key, company_id):
    """Bounded issue scan: (issues, truncated, reason)."""
    issues = []
    for page in range(1, MAX_ISSUE_PAGES + 1):
        path = (f"/api/companies/{company_id}/issues"
                f"?perPage={ISSUES_PER_PAGE}&page={page}")
        data, reason = paperclip_get(base_url, api_key, path)
        if data is None:
            return issues, True, reason
        batch = data if isinstance(data, list) else data.get("issues", [])
        issues.extend(batch)
        if len(batch) < ISSUES_PER_PAGE:
            return issues, False, None
    return issues, True, None


def compute_first_push_green_rate(pr_states):
    """Fraction of PRs green on first push; None when no evidence."""
    known = [s for s in pr_states if s in ("success", "failure", "error")]
    if not known:
        return None
    return sum(1 for s in known if s == "success") / len(known)


def compute_escalations_per_100(issues, window_start):
    """Blocked cards per 100 cards touched since window_start; None if none."""
    touched = [i for i in issues
               if str(i.get("updatedAt", "")) >= window_start]
    if not touched:
        return None
    blocked = [i for i in touched if i.get("status") == "blocked"]
    return 100.0 * len(blocked) / len(touched)


def compute_tokens_per_pr(cost_tokens, merged_count):
    """Tokens per merged PR; None when either side is missing/zero."""
    if cost_tokens is None or not merged_count:
        return None
    return cost_tokens / merged_count


def resolve_issue_scan(fetched, truncated, reason):
    """Failed scans degrade to None so the reason surfaces in provenance.

    A partial list from a failed scan must never masquerade as a healthy
    empty window. Truncation rides along either way, mirroring the
    per-repo `truncated` flag on the GitHub side.
    """
    if reason:
        return None, truncated
    return fetched, truncated


def parse_repos(repos_arg, repos_file):
    """Repo list from --repos plus --repos-file (# comments ok)."""
    repos = [r.strip() for r in repos_arg.split(",") if r.strip()]
    if repos_file:
        with open(repos_file, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    repos.append(stripped)
    return repos


def _iso_date(value):
    """YYYY-MM-DD prefix of an ISO timestamp, or '' when absent."""
    text = str(value or "")
    return text[:10] if len(text) >= 10 else ""


def _window_end_iso(window_start, days):
    """Report window end as ISO8601; '' when the start is unparsable."""
    text = str(window_start or "")
    try:
        stamp = text[:-1] + "+00:00" if text.endswith("Z") else text
        start = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    out = (start + datetime.timedelta(days=days)).isoformat()
    if out.endswith("+00:00"):
        out = out[:-6] + "Z"
    return out


def _window_days_between(start, end):
    """Whole days between two ISO timestamps, or None when unparsable."""
    try:
        first = str(start)
        last = str(end)
        first = first[:-1] + "+00:00" if first.endswith("Z") else first
        last = last[:-1] + "+00:00" if last.endswith("Z") else last
        delta = (datetime.datetime.fromisoformat(last)
                 - datetime.datetime.fromisoformat(first))
        return max(0, delta.days)
    except (ValueError, TypeError):
        return None


def _mentions_credentials(reason):
    return bool(reason) and "credential" in str(reason).lower()


def _looks_like_failure(reason):
    lowered = str(reason or "").lower()
    return any(hint in lowered for hint in _FAILURE_HINTS)


def _metric(value, definition, provenance, *, validity, reason_code,
            reason="", next_action="", completeness="complete",
            source_window=None, numerator=None, denominator=None,
            numerator_source="", denominator_source="", counts=None):
    """One metric with its legacy keys plus the validity envelope."""
    metric = {
        "value": value,
        "definition": definition,
        "provenance": provenance,
        "validity": validity,
        "reason_code": reason_code,
        "reason": reason,
        "next_action": next_action,
        "completeness": completeness,
        "source_window": source_window,
        "numerator": numerator,
        "denominator": denominator,
        "numerator_source": numerator_source,
        "denominator_source": denominator_source,
    }
    if counts is not None:
        metric["counts"] = counts
    return metric


def _as_token_total(value):
    """Token total as a finite number; None for bools/non-finite.

    The costs payload is external input: bool is an int subclass and
    json.loads accepts NaN/Infinity, so both are rejected here to keep
    emitted ratios standard-compliant JSON. Zero stays a real total.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _evidence_tokens(evidence):
    """Token total from a cost-evidence mapping; None when absent.

    Zero is a real total, so presence is tested with isinstance,
    never truthiness.
    """
    if not isinstance(evidence, dict):
        return None
    for key in ("tokens", "totalTokens"):
        value = _as_token_total(evidence.get(key))
        if value is not None:
            return value
    inner = evidence.get("summary")
    if isinstance(inner, dict):
        value = _as_token_total(inner.get("totalTokens"))
        if value is not None:
            return value
    return None


def _verify_cost_window(evidence, report_start, report_end):
    """Classify cost evidence against the report window.

    Returns (status, detail): "match" for an explicit window pair on
    the same dates, "mismatch" with the evidence pair, "unsupported"
    with a known non-window period label, "unverified" with other
    period info, or "missing" when no window or period info exists.
    """
    if not isinstance(evidence, dict):
        return "missing", None
    ev_start = evidence.get("window_start")
    ev_end = evidence.get("window_end")
    if ev_start and ev_end:
        if (_iso_date(ev_start) == _iso_date(report_start)
                and _iso_date(ev_end) == _iso_date(report_end)):
            return "match", None
        return "mismatch", (ev_start, ev_end)
    period = evidence.get("period")
    if period is None:
        if ev_start or ev_end:
            return "unverified", None
        return "missing", None
    label = str(period).strip().lower()
    if label in UNSUPPORTED_PERIODS:
        return "unsupported", period
    return "unverified", period


def _green_metric(states, merged_total, report_window):
    """first_push_green_rate with its validity envelope."""
    definition = ("merged PRs green on their first commit / merged PRs "
                  "with any commit status or check run (v0 proxy for "
                  "first-attempt success)")
    provenance = "github commit status + check runs"
    known = [s for s in states if s in ("success", "failure", "error")]
    if known:
        green = sum(1 for s in known if s == "success")
        return _metric(green / len(known), definition, provenance,
                       validity="valid", reason_code="ok",
                       completeness="complete",
                       source_window=dict(report_window),
                       numerator=green, denominator=len(known),
                       numerator_source=provenance,
                       denominator_source=provenance)
    if not merged_total:
        return _metric(None, definition, provenance,
                       validity="withheld", reason_code="no_prs",
                       reason="no merged PRs in the report window",
                       next_action="widen the window or add repos, "
                                   "then re-run",
                       completeness="complete",
                       source_window=dict(report_window),
                       numerator=0, denominator=0,
                       numerator_source=provenance,
                       denominator_source=provenance)
    return _metric(None, definition, provenance,
                   validity="withheld", reason_code="no_evidence",
                   reason="no commit-status or check-run evidence "
                          "for merged PRs",
                   next_action="check github status access, then re-run",
                   completeness="complete",
                   source_window=dict(report_window),
                   numerator=0, denominator=0,
                   numerator_source=provenance,
                   denominator_source=provenance)


def _tokens_metric(cost_tokens, cost_evidence, merged_total, report_window,
                   paperclip_reason):
    """tokens_per_merged_pr, valid only on a verified cost window."""
    definition = ("paperclip cost-window tokens / merged PRs "
                  "(experimental)")
    tokens = _evidence_tokens(cost_evidence)
    if tokens is None:
        tokens = _as_token_total(cost_tokens)
    provenance = ("paperclip costs api" if tokens is not None
                  else (paperclip_reason or "no paperclip data"))
    num_source = "paperclip costs api"
    if isinstance(cost_evidence, dict):
        claimed = str(cost_evidence.get("source") or "").strip()
        if claimed:
            num_source = claimed
    den_source = "github search total_count"
    report_start = report_window["start"]
    report_end = report_window["end"]
    if (tokens is None and not isinstance(cost_evidence, dict)
            and _mentions_credentials(paperclip_reason)):
        return _metric(None, definition, provenance,
                       validity="unavailable",
                       reason_code="credentials_missing",
                       reason="paperclip credentials not configured",
                       next_action="set the paperclip url/key/company env "
                                   "vars to enable cost metrics",
                       completeness="unknown", source_window=None,
                       numerator=None, denominator=merged_total,
                       numerator_source=num_source,
                       denominator_source=den_source)
    status, detail = _verify_cost_window(
        cost_evidence, report_start, report_end)
    if status == "match":
        if tokens is None:
            return _metric(
                None, definition, provenance, validity="unavailable",
                reason_code="no_evidence",
                reason="cost evidence window matches but carries "
                       "no token total",
                next_action="check the costs payload for a token total, "
                            "then re-run",
                completeness="unknown",
                source_window=dict(report_window),
                numerator=None, denominator=merged_total,
                numerator_source=num_source,
                denominator_source=den_source)
        if not merged_total:
            return _metric(
                None, definition, provenance, validity="withheld",
                reason_code="no_prs",
                reason="cost window verified but no merged PRs in the "
                       "report window; ratio undefined",
                next_action="widen the window or add repos, then re-run",
                completeness="complete",
                source_window=dict(report_window),
                numerator=tokens, denominator=0,
                numerator_source=num_source,
                denominator_source=den_source)
        return _metric(tokens / merged_total, definition, provenance,
                       validity="valid", reason_code="ok",
                       completeness="complete",
                       source_window=dict(report_window),
                       numerator=tokens, denominator=merged_total,
                       numerator_source=num_source,
                       denominator_source=den_source)
    if status == "mismatch":
        ev_start, ev_end = detail
        return _metric(
            None, definition, provenance, validity="withheld",
            reason_code="window_mismatch",
            reason=(f"cost evidence window {_iso_date(ev_start)}/"
                    f"{_iso_date(ev_end)} does not match report window "
                    f"{_iso_date(report_start)}/{_iso_date(report_end)}"),
            next_action="provide cost evidence for the report window, "
                        "or narrow the report window to the evidence dates",
            completeness="unknown",
            source_window={"start": ev_start, "end": ev_end,
                           "days": _window_days_between(ev_start, ev_end)},
            numerator=tokens, denominator=merged_total,
            numerator_source=num_source,
            denominator_source=den_source)
    if status == "unsupported":
        return _metric(
            None, definition, provenance, validity="withheld",
            reason_code="period_unsupported",
            reason=(f"cost period {detail!r} is not a verifiable "
                    "report window"),
            next_action="provide cost_evidence with explicit "
                        "window_start/window_end matching the report window",
            completeness="unknown", source_window=None,
            numerator=tokens, denominator=merged_total,
            numerator_source=num_source,
            denominator_source=den_source)
    if status == "unverified":
        if detail is None:
            why = ("cost evidence window cannot be verified against "
                   "the report window")
        else:
            why = (f"cost period {detail!r} cannot be verified against "
                   f"report window {_iso_date(report_start)}/"
                   f"{_iso_date(report_end)}")
        return _metric(
            None, definition, provenance, validity="withheld",
            reason_code="window_unverified",
            reason=why,
            next_action="provide cost_evidence with explicit "
                        "window_start/window_end matching the report "
                        "window, or verify the period covers it",
            completeness="unknown", source_window=None,
            numerator=tokens, denominator=merged_total,
            numerator_source=num_source,
            denominator_source=den_source)
    if tokens is not None:
        return _metric(
            None, definition, provenance, validity="withheld",
            reason_code="period_missing",
            reason="cost total has no verifiable window or period",
            next_action="provide cost_evidence with window_start/"
                        "window_end matching the report window",
            completeness="unknown", source_window=None,
            numerator=tokens, denominator=merged_total,
            numerator_source=num_source,
            denominator_source=den_source)
    return _metric(
        None, definition, provenance, validity="unavailable",
        reason_code="period_missing",
        reason="no cost evidence for the report window",
        next_action="provide cost_evidence with window_start/window_end "
                    "matching the report window, or configure paperclip "
                    "credentials for live costs",
        completeness="unknown", source_window=None,
        numerator=None, denominator=merged_total,
        numerator_source=num_source,
        denominator_source=den_source)


def _escalations_metric(issues, window_start, report_window,
                        paperclip_reason, issues_truncated):
    """escalations_per_100_cards; partial scans never read as complete."""
    definition = ("cards currently blocked per 100 cards touched in "
                  "window (v0 proxy for escalations)")
    provenance = ("paperclip issues api" if issues is not None
                  else (paperclip_reason or "no paperclip data"))
    num_source = "paperclip issues api"
    den_source = "paperclip issues api (touched in window)"
    if issues is None and _mentions_credentials(paperclip_reason):
        return _metric(None, definition, provenance,
                       validity="unavailable",
                       reason_code="credentials_missing",
                       reason="paperclip credentials not configured",
                       next_action="set the paperclip url/key/company env "
                                   "vars to enable card metrics",
                       completeness="unknown", source_window=None,
                       numerator=None, denominator=None,
                       numerator_source=num_source,
                       denominator_source=den_source)
    if issues is None:
        if _looks_like_failure(paperclip_reason):
            code = "scan_failed"
            why = f"issue scan failed: {paperclip_reason}"
            action = "retry the run; check paperclip api availability"
        elif issues_truncated:
            code = "scan_truncated"
            why = "issue scan hit the page cap before completing"
            action = ("narrow the window or re-run; treat counts as a "
                      "sample")
        elif paperclip_reason:
            code = "scan_incomplete"
            why = f"issue scan incomplete: {paperclip_reason}"
            action = "re-run the scan; narrow the window if it recurs"
        else:
            code = "no_evidence"
            why = "no issue evidence for the report window"
            action = ("re-run with paperclip credentials or provide "
                      "an issues fixture")
        return _metric(
            None, definition, provenance,
            validity="withheld" if code != "no_evidence" else "unavailable",
            reason_code=code, reason=why, next_action=action,
            completeness="partial" if code != "no_evidence" else "unknown",
            source_window=None, numerator=None, denominator=None,
            numerator_source=num_source, denominator_source=den_source,
            counts={"issues_scanned": 0, "touched_in_window": 0})
    touched = [i for i in issues
               if str(i.get("updatedAt", "")) >= window_start]
    blocked = [i for i in touched if i.get("status") == "blocked"]
    if issues_truncated:
        return _metric(
            None, definition, provenance, validity="withheld",
            reason_code="scan_truncated",
            reason=(f"issue scan hit the page cap "
                    f"({MAX_ISSUE_PAGES}x{ISSUES_PER_PAGE}); a sample of "
                    f"{len(issues)} cannot stand for the complete "
                    "population"),
            next_action="narrow the window or re-run; treat counts as a "
                        "sample, not a complete-population metric",
            completeness="partial",
            source_window=dict(report_window),
            numerator=len(blocked), denominator=len(touched),
            numerator_source=num_source, denominator_source=den_source,
            counts={"issues_scanned": len(issues),
                    "touched_in_window": len(touched),
                    "blocked_in_sample": len(blocked)})
    if not touched:
        return _metric(None, definition, provenance,
                       validity="withheld", reason_code="empty_window",
                       reason="no cards touched in the report window",
                       next_action="widen the window if a card population "
                                   "is expected",
                       completeness="complete",
                       source_window=dict(report_window),
                       numerator=0, denominator=0,
                       numerator_source=num_source,
                       denominator_source=den_source)
    return _metric(100.0 * len(blocked) / len(touched), definition,
                   provenance, validity="valid", reason_code="ok",
                   completeness="complete",
                   source_window=dict(report_window),
                   numerator=len(blocked), denominator=len(touched),
                   numerator_source=num_source,
                   denominator_source=den_source)


def build_report(github_rows, cost_tokens, issues, window_start, window_days,
                 paperclip_reason=None, issues_truncated=False,
                 cost_evidence=None):
    """Assemble the report dict from fetched (or fixture) data."""
    merged_total = sum(r["merged_prs"] for r in github_rows)
    states = [s for r in github_rows for s in r["first_commit_states"]]
    window_end = _window_end_iso(window_start, window_days)
    report_window = {"start": window_start, "end": window_end,
                     "days": window_days}
    return {
        "tool": "ax-metrics",
        "schema_version": SCHEMA_VERSION,
        "window_days": window_days,
        "window_start": window_start,
        "window_end": window_end,
        "repos": github_rows,
        "merged_prs_total": merged_total,
        "issues_truncated": issues_truncated,
        "metrics": {
            "first_push_green_rate": _green_metric(
                states, merged_total, report_window),
            "tokens_per_merged_pr": _tokens_metric(
                cost_tokens, cost_evidence, merged_total, report_window,
                paperclip_reason),
            "escalations_per_100_cards": _escalations_metric(
                issues, window_start, report_window, paperclip_reason,
                issues_truncated),
        },
    }


def collect_live(args, window_start):
    token = os.environ.get(args.github_token_env, "")
    if not token:
        raise RuntimeError(f"missing github token env {args.github_token_env}")
    github_rows = []
    for repo in args.repos:
        prs, truncated, total = fetch_merged_prs(repo, window_start, token)
        states = [fetch_first_commit_state(repo, pr["number"], token)
                  for pr in prs]
        github_rows.append({
            "repo": repo, "merged_prs": total,
            "truncated": truncated, "first_commit_states": states,
        })
    base = os.environ.get(args.paperclip_url_env, "")
    key = os.environ.get(args.paperclip_key_env, "")
    company = os.environ.get(args.paperclip_company_env, "")
    cost_tokens, issues, reason = None, None, None
    cost_evidence = None
    issues_truncated = False
    if base and key and company:
        # The costs path carries no query params: the endpoint's
        # period semantics are unverifiable, so the report withholds
        # the ratio unless the payload itself verifies the window.
        costs, costs_reason = paperclip_get(
            base, key, f"/api/companies/{company}/costs/summary")
        if isinstance(costs, dict):
            cost_tokens = _evidence_tokens(costs)
            cost_evidence = {
                "tokens": cost_tokens,
                "window_start": costs.get("window_start"),
                "window_end": costs.get("window_end"),
                "period": costs.get("period"),
                "source": "paperclip costs api",
            }
        fetched, truncated, issues_reason = fetch_paperclip_issues(
            base, key, company)
        issues, issues_truncated = resolve_issue_scan(
            fetched, truncated, issues_reason)
        parts = [r for r in (costs_reason, issues_reason) if r]
        reason = "; ".join(parts) if parts else None
        if cost_tokens is None and not reason:
            reason = "costs api returned no token total (experimental)"
    else:
        reason = "paperclip credentials not configured"
    return build_report(github_rows, cost_tokens, issues, window_start,
                        args.days, paperclip_reason=reason,
                        issues_truncated=issues_truncated,
                        cost_evidence=cost_evidence)


def collect_from_file(path, days):
    with open(path, encoding="utf-8") as fh:
        fixture = json.load(fh)
    return build_report(
        fixture.get("github_rows", []),
        fixture.get("cost_tokens"),
        fixture.get("issues"),
        fixture.get("window_start", ""), days,
        paperclip_reason=fixture.get("paperclip_reason"),
        issues_truncated=fixture.get("issues_truncated", False),
        cost_evidence=fixture.get("cost_evidence"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ax_metrics.py",
        description="Weekly AX metrics reporter. Prints one JSON report "
                    "to stdout. GitHub metrics are live; Paperclip-backed "
                    "metrics degrade to null with a reason when credentials "
                    "are absent.",
        epilog="examples:\n"
               "  python3 ax_metrics.py --repos Example/a,Example/b --days 7\n"
               "  python3 ax_metrics.py --repos-file repos.txt "
               "--out report.json\n"
               "  python3 ax_metrics.py --from-file fixture.json --days 7\n"
               "\n"
               "window verification:\n"
               "  tokens_per_merged_pr is valid only when the cost "
               "evidence\n"
               "  carries an explicit window_start/window_end matching "
               "the\n"
               "  report window dates. A bare total, a mismatching "
               "window,\n"
               "  or an unsupported period (all-time, monthly, ...) "
               "withholds\n"
               "  the ratio with a window_unverified style reason_code; "
               "a\n"
               "  truncated or failed issue scan withholds the "
               "escalation\n"
               "  metric with a scan_truncated style code. Never infer "
               "weekly\n"
               "  semantics from a total.\n"
               "\n"
               "validity envelope (every metric):\n"
               "  validity in {valid, withheld, partial, unavailable};\n"
               "  completeness in {complete, partial, unknown}; "
               "reason_code\n"
               "  is a stable snake_case token (ok when valid) with a "
               "human\n"
               "  reason plus next_action; source_window, numerator and\n"
               "  denominator carry provenance; counts quantifies "
               "partial\n"
               "  scans. Consume reason_code, never prose.\n"
               "\n"
               "report schema example:\n"
               "  {\"tool\": \"ax-metrics\", \"schema_version\": 2,\n"
               "   \"window_start\": ..., \"window_end\": ..., "
               "\"metrics\":\n"
               "   {\"tokens_per_merged_pr\": {\"value\": 500.0,\n"
               "    \"validity\": \"valid\", \"reason_code\": \"ok\",\n"
               "    \"completeness\": \"complete\",\n"
               "    \"source_window\": {\"start\": ..., \"end\": ..., "
               "\"days\": 7},\n"
               "    \"numerator\": 1000, \"denominator\": 2, ...}}}\n"
               "\n"
               "offline check (no network, no credentials):\n"
               "  python3 -B -m unittest discover "
               "-s templates/org-github/tests\n"
               "    -p 'test_ax_*py'\n")
    parser.add_argument("--repos", default="",
                        help="comma-separated owner/name list")
    parser.add_argument("--repos-file", default="",
                        help="file with one owner/name per line (# comments ok)")
    parser.add_argument("--days", type=int, default=7,
                        help="trailing window in days (default: 7)")
    parser.add_argument("--out", default="",
                        help="also write the JSON report to this path")
    parser.add_argument("--from-file", default="",
                        help="replay a fixture instead of fetching (offline)")
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    parser.add_argument("--paperclip-url-env", default="PAPERCLIP_API_URL")
    parser.add_argument("--paperclip-key-env", default="PAPERCLIP_API_KEY")
    parser.add_argument("--paperclip-company-env", default="PAPERCLIP_COMPANY_ID")
    args = parser.parse_args(argv)
    if args.days < 1:
        print("ax-metrics: error: --days must be >= 1", file=sys.stderr)
        return 2
    if args.from_file:
        report = collect_from_file(args.from_file, args.days)
    else:
        repos = parse_repos(args.repos, args.repos_file)
        if not repos:
            print("ax-metrics: error: no repos (use --repos or --repos-file)",
                  file=sys.stderr)
            return 2
        args.repos = repos
        window_start = (datetime.datetime.now(datetime.timezone.utc)
                        - datetime.timedelta(days=args.days)).isoformat()
        try:
            report = collect_live(args, window_start)
        except RuntimeError as exc:
            print(f"ax-metrics: error: {exc}", file=sys.stderr)
            return 1
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
