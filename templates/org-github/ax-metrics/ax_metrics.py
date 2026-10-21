#!/usr/bin/env python3
"""ax-metrics: weekly Agent Experience metrics reporter (v0 scaffold).

Collects the three AX outcome metrics over a trailing window and
prints one JSON report to stdout:

* ``first_push_green_rate`` — merged PRs whose FIRST commit already
  had green combined status, over merged PRs with any status. A v0
  proxy for first-attempt success, from the GitHub API.
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
"""

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

MAX_PRS_PER_REPO = 30
MAX_ISSUE_PAGES = 5
ISSUES_PER_PAGE = 100


def _http_get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _github_headers(token):
    return {"Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28"}


def fetch_merged_prs(repo, since_iso, token):
    """Merged PRs in window: [{number, merged_at}]. Truncated, flagged."""
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
    truncated = len(prs) > MAX_PRS_PER_REPO
    return prs[:MAX_PRS_PER_REPO], truncated


def fetch_first_commit_state(repo, pr_number, token):
    """Combined status state of the PR's first commit, or None."""
    commits_url = (f"https://api.github.com/repos/{repo}/pulls/"
                   f"{pr_number}/commits?per_page=1")
    try:
        commits = _http_get_json(commits_url, _github_headers(token))
        if not commits:
            return None
        sha = commits[0]["sha"]
        status_url = (f"https://api.github.com/repos/{repo}/commits/"
                      f"{sha}/status")
        status = _http_get_json(status_url, _github_headers(token))
        return status.get("state")
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


def build_report(github_rows, cost_tokens, issues, window_start, window_days,
                 paperclip_reason=None):
    """Assemble the report dict from fetched (or fixture) data."""
    merged_total = sum(r["merged_prs"] for r in github_rows)
    states = [s for r in github_rows for s in r["first_commit_states"]]
    green = compute_first_push_green_rate(states)
    esc = (compute_escalations_per_100(issues, window_start)
           if issues is not None else None)
    tpp = compute_tokens_per_pr(cost_tokens, merged_total)
    return {
        "tool": "ax-metrics",
        "window_days": window_days,
        "window_start": window_start,
        "repos": github_rows,
        "merged_prs_total": merged_total,
        "metrics": {
            "first_push_green_rate": {
                "value": green,
                "definition": "merged PRs green on their first commit / "
                              "merged PRs with any commit status (v0 proxy "
                              "for first-attempt success)",
                "provenance": "github commit status",
            },
            "tokens_per_merged_pr": {
                "value": tpp,
                "definition": "paperclip cost-window tokens / merged PRs "
                              "(experimental)",
                "provenance": ("paperclip costs api" if cost_tokens is not None
                               else (paperclip_reason or "no paperclip data")),
            },
            "escalations_per_100_cards": {
                "value": esc,
                "definition": "cards currently blocked per 100 cards "
                              "touched in window (v0 proxy for escalations)",
                "provenance": ("paperclip issues api" if issues is not None
                               else (paperclip_reason or "no paperclip data")),
            },
        },
    }


def collect_live(args, window_start):
    token = os.environ.get(args.github_token_env, "")
    if not token:
        raise RuntimeError(f"missing github token env {args.github_token_env}")
    github_rows = []
    for repo in args.repos:
        prs, truncated = fetch_merged_prs(repo, window_start, token)
        states = [fetch_first_commit_state(repo, pr["number"], token)
                  for pr in prs]
        github_rows.append({
            "repo": repo, "merged_prs": len(prs),
            "truncated": truncated, "first_commit_states": states,
        })
    base = os.environ.get(args.paperclip_url_env, "")
    key = os.environ.get(args.paperclip_key_env, "")
    company = os.environ.get(args.paperclip_company_env, "")
    cost_tokens, issues, reason = None, None, None
    if base and key and company:
        costs, costs_reason = paperclip_get(
            base, key, f"/api/companies/{company}/costs/summary")
        if isinstance(costs, dict):
            for candidate in ("totalTokens", "tokens"):
                if isinstance(costs.get(candidate), (int, float)):
                    cost_tokens = costs[candidate]
                    break
            else:
                inner = costs.get("summary", {})
                if isinstance(inner, dict) and isinstance(inner.get("totalTokens"), (int, float)):
                    cost_tokens = inner["totalTokens"]
        fetched, _, issues_reason = fetch_paperclip_issues(base, key, company)
        issues = fetched
        parts = [r for r in (costs_reason, issues_reason) if r]
        reason = "; ".join(parts) if parts else None
        if cost_tokens is None and not reason:
            reason = "costs api returned no token total (experimental)"
    else:
        reason = "paperclip credentials not configured"
    return build_report(github_rows, cost_tokens, issues, window_start,
                        args.days, paperclip_reason=reason)


def collect_from_file(path, days):
    fixture = json.load(open(path, encoding="utf-8"))
    return build_report(
        fixture.get("github_rows", []),
        fixture.get("cost_tokens"),
        fixture.get("issues"),
        fixture.get("window_start", ""), days,
        paperclip_reason=fixture.get("paperclip_reason"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ax_metrics.py",
        description="Weekly AX metrics reporter. Prints one JSON report "
                    "to stdout. GitHub metrics are live; Paperclip-backed "
                    "metrics degrade to null with a reason when credentials "
                    "are absent.",
        epilog="examples:\n"
               "  python3 ax_metrics.py --repos Example/a,Example/b --days 7\n"
               "  python3 ax_metrics.py --repos-file repos.txt --out report.json\n"
               "  python3 ax_metrics.py --from-file fixture.json --days 7\n")
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
        repos = [r.strip() for r in args.repos.split(",") if r.strip()]
        if args.repos_file:
            with open(args.repos_file, encoding="utf-8") as fh:
                repos.extend(ln.strip() for ln in fh
                             if ln.strip() and not ln.startswith("#"))
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
