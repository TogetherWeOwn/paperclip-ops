#!/usr/bin/env python3
"""hung_job_eval.py - evaluator for CI jobs that STARTED and never complete.

Implements ops/case_e1e23b50/hung-job-detector.md sections 2-5 (spec case_e1e23b50,
evaluator case_703e1a90). Read-only: it GETs the Checks API and prints one JSON
verdict line; it cancels, re-runs and writes nothing.

Source. For every repo in --repos (owner/repo, whitespace separated) it reads

    repos/{o}/{r}/commits/main/check-runs?per_page=100          main head
    repos/{o}/{r}/pulls?state=open&sort=created&direction=desc&per_page=31
    repos/{o}/{r}/commits/{head_sha}/check-runs?per_page=100    each PR head

through `gh api` (the host credential shim). Those need checks:read plus the
read of open PRs; the Actions jobs/runs endpoints need an Actions read scope
the broker withholds (measured HTTP 403, spec section 2), so this file never
names one - test_case_f9bc8d5a_hung_job.py fails the build if it ever does.

Classifier (spec section 3). A check run is a candidate only when ALL hold:
  - status == "in_progress" (queued, waiting and every completed conclusion,
    timed_out and cancelled included, are excluded: queued is the queue-age
    signal, completed is not stuck);
  - started_at is set (null is excluded: that is the queued-side signal);
  - it belongs to the polled head SHA (a run reporting another head_sha is
    a superseded run and is ignored; only main and the newest PR_CAP open PR
    heads are polled at all, so a force-pushed PR's old SHA is never read);
  - age > AGE_FLOOR_SECONDS (5 min).
Candidates are then joined by job name to hung_job_thresholds.json
(check-run name -> job-level timeout-minutes):
  WARN  age > 0.75 x timeout      (triage: heading for its own timeout)
  HUNG  age > timeout + 15 min    (GitHub's timeout should already have
                                   completed it: completion-event loss)
A candidate with NO row is never silently healthy: it is judged against
GitHub's own default job timeout (DEFAULT_TIMEOUT_MINUTES, 360) and
reported as UNKNOWN-threshold with its name while younger than
default + 15 min, HUNG beyond that. The shared GH_REPOS env also lists repos
whose workflows have no table here, and a job without `timeout-minutes` has no
row either; the backstop is what lets those page at all.

Verdict. success is false only on HUNG or on a fail-closed condition: an
unreadable API (403 included), JSON that does not parse or has no check_runs
list, a malformed started_at, or a check-runs page that is full (100 runs, or
total_count above what came back - runs beyond the page are unseen). WARN and
UNKNOWN-threshold ride in the text with success still true. More than PR_CAP
open PRs is the specified bound, not a failure: it is noted in the text.
NOTE (verified against TwiN/gatus master api/external_endpoint.go): Gatus
stores the pushed `error` text only when success=false, so a WARN-only text is
visible in the script's stdout (cron log), not on the status page; it reaches
the page when a HUNG push carries it.

Untrusted input. The API is the only source of names, so names are reduced to
a safe alphabet and truncated, ids must be integers, SHAs must be 40 hex
(they are interpolated into an API path), and at most MAX_ITEMS entries per
class reach the text.

Output (stdout, one line): {"success": bool, "verdict": "HEALTHY|WARN|HUNG|
FAIL", "text": str, "hung": n, "warn": n, "unknown": n, "failures": n,
"polled": n}. Exit 0 = a verdict was produced (read `success`); 2 = refusal
before any health claim (bad arguments, unreadable thresholds, no gh).
Stdlib only. `--now` pins the clock for the offline suite.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

SCHEMA = "case_f9bc8d5a-hung-job-thresholds-v1"
WARN_SECONDS_PER_TIMEOUT_MINUTE = 45  # 0.75 x 60s
HUNG_GRACE_MINUTES = 15
DEFAULT_TIMEOUT_MINUTES = 360  # GitHub's job timeout when timeout-minutes is unset
AGE_FLOOR_SECONDS = 300
PR_CAP = 30
PAGE_SIZE = 100
MAX_ITEMS = 4
GH_TIMEOUT_SECONDS = 60

_REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)Z$")
_UNSAFE = re.compile(r"[^A-Za-z0-9 _.()/:+#-]")


def refuse(msg):
    print(f"hung_job_eval: refusing - {msg}", file=sys.stderr)
    sys.exit(2)


def parse_ts(value):
    """Strict Zulu timestamp -> epoch seconds, or None when unparseable."""
    if not isinstance(value, str):
        return None
    m = _TS.match(value)
    if not m:
        return None
    try:
        dt = datetime.datetime(*(int(g) for g in m.groups()), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def clean(value, limit=60):
    return _UNSAFE.sub("?", str(value))[:limit]


def load_thresholds(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        refuse(f"thresholds unreadable ({path}): {e}")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        refuse(f"thresholds schema is not {SCHEMA}")
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        refuse("thresholds `jobs` must be a non-empty object")
    for name, minutes in jobs.items():
        if not name.strip() or isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            refuse(f"thresholds row {name!r} must map to a positive whole number of minutes")
    return jobs


def gh_get(gh, path):
    """GET through gh. Returns (ok, text, why); `why` is the HTTP status only."""
    try:
        p = subprocess.run([gh, "api", path], capture_output=True, text=True,
                           timeout=GH_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False, "", "gh did not run"
    if p.returncode != 0:
        m = re.search(r"HTTP (\d{3})", p.stderr or "")
        return False, "", f"HTTP {m.group(1)}" if m else f"gh exit {p.returncode}"
    return True, p.stdout, ""


class Result:
    def __init__(self):
        self.hung, self.warn, self.unknown, self.failures, self.notes = [], [], [], [], []
        self.polled = 0


def poll_sha(gh, repo, label, ref, expect_sha, now, jobs, res):
    """Classify one head's check runs. Returns False when the API was unreadable."""
    where = f"{repo} {label}"
    path = f"repos/{repo}/commits/{ref}/check-runs?per_page={PAGE_SIZE}"
    ok, text, why = gh_get(gh, path)
    if not ok:
        res.failures.append(f"{where}: check-runs unreadable ({why})")
        return False
    try:
        doc = json.loads(text)
    except ValueError:
        res.failures.append(f"{where}: check-runs JSON does not parse")
        return True
    runs = doc.get("check_runs") if isinstance(doc, dict) else None
    if not isinstance(runs, list):
        res.failures.append(f"{where}: check-runs JSON has no check_runs list")
        return True
    res.polled += 1
    total = doc.get("total_count")
    if len(runs) >= PAGE_SIZE or (isinstance(total, int) and not isinstance(total, bool) and total > len(runs)):
        res.failures.append(f"{where}: check-runs page full ({len(runs)} of {total}); later runs unseen")
    for run in runs:
        if not isinstance(run, dict):
            res.failures.append(f"{where}: check-runs entry is not an object")
            continue
        if run.get("status") != "in_progress":
            continue
        started = run.get("started_at")
        if started is None:
            continue
        head = run.get("head_sha")
        if expect_sha is not None and head != expect_sha:
            continue
        ts = parse_ts(started)
        if ts is None:
            res.failures.append(f"{where}: unparseable started_at on an in_progress check run")
            continue
        age = now - ts
        if age <= AGE_FLOOR_SECONDS:
            continue
        name, rid = run.get("name"), run.get("id")
        if not isinstance(name, str) or isinstance(rid, bool) or not isinstance(rid, int):
            res.failures.append(f"{where}: in_progress check run without a string name and integer id")
            continue
        item = {"where": where, "sha": (head if isinstance(head, str) else ref)[:7],
                "name": clean(name), "id": rid, "age_m": age // 60}
        minutes = jobs.get(name)
        known = minutes is not None
        if not known:
            minutes = DEFAULT_TIMEOUT_MINUTES
        if age > (minutes + HUNG_GRACE_MINUTES) * 60:
            item["limit"] = f">{minutes + HUNG_GRACE_MINUTES}m" + ("" if known else " (no row: default)")
            res.hung.append(item)
        elif not known:
            res.unknown.append(item)
        elif age > minutes * WARN_SECONDS_PER_TIMEOUT_MINUTE:
            item["limit"] = f">{minutes * WARN_SECONDS_PER_TIMEOUT_MINUTE / 60:g}m"
            res.warn.append(item)
    return True


def poll_repo(gh, repo, now, jobs, res):
    if not poll_sha(gh, repo, "main", "main", None, now, jobs, res):
        return
    ok, text, why = gh_get(gh, f"repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page={PR_CAP + 1}")
    if not ok:
        res.failures.append(f"{repo}: open PR list unreadable ({why})")
        return
    try:
        prs = json.loads(text)
    except ValueError:
        prs = None
    if not isinstance(prs, list):
        res.failures.append(f"{repo}: open PR list does not parse as a list")
        return
    if len(prs) > PR_CAP:
        res.notes.append(f"{repo}: more than {PR_CAP} open PRs, older heads unpolled (specified cap)")
    for pr in prs[:PR_CAP]:
        number = pr.get("number") if isinstance(pr, dict) else None
        head = pr.get("head") if isinstance(pr, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        if isinstance(number, bool) or not isinstance(number, int) or not isinstance(sha, str) or not _SHA.match(sha):
            res.failures.append(f"{repo}: open PR entry without an integer number and 40-hex head sha")
            continue
        if not poll_sha(gh, repo, f"PR#{number}", sha, sha, now, jobs, res):
            return


def describe(items, with_limit):
    shown = []
    for it in items[:MAX_ITEMS]:
        s = f'{it["where"]}@{it["sha"]} "{it["name"]}" id={it["id"]} age={it["age_m"]}m'
        shown.append(s + (f' (limit {it["limit"]})' if with_limit else ""))
    if len(items) > MAX_ITEMS:
        shown.append(f"+{len(items) - MAX_ITEMS} more")
    return "; ".join(shown)


def render(res):
    parts = []
    if res.hung:
        parts.append(f"HUNG {len(res.hung)}: {describe(res.hung, True)}")
    if res.failures:
        parts.append(f"FAIL {len(res.failures)}: " + "; ".join(res.failures[:MAX_ITEMS]))
    if res.warn:
        parts.append(f"WARN {len(res.warn)}: {describe(res.warn, True)}")
    if res.unknown:
        parts.append(f"UNKNOWN-threshold {len(res.unknown)} (no row in hung_job_thresholds.json, "
                     f"pages above {DEFAULT_TIMEOUT_MINUTES + HUNG_GRACE_MINUTES}m): "
                     f"{describe(res.unknown, False)}")
    parts.extend(res.notes)
    return " | ".join(parts)


def evaluate(repos, jobs, now, gh):
    res = Result()
    for repo in repos:
        poll_repo(gh, repo, now, jobs, res)
    success = not (res.hung or res.failures)
    verdict = "HUNG" if res.hung else "FAIL" if res.failures else "WARN" if res.warn else "HEALTHY"
    return {"success": success, "verdict": verdict, "text": render(res), "hung": len(res.hung),
            "warn": len(res.warn), "unknown": len(res.unknown), "failures": len(res.failures),
            "polled": res.polled}


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repos", default=os.environ.get("GH_REPOS", ""),
                    help="whitespace-separated owner/repo list (default: $GH_REPOS)")
    ap.add_argument("--thresholds", default=os.path.join(here, "hung_job_thresholds.json"))
    ap.add_argument("--gh", default=os.environ.get("GH_BIN", "gh"))
    ap.add_argument("--now", help="pin the clock (YYYY-MM-DDTHH:MM:SSZ); offline suite only")
    args = ap.parse_args(argv)

    repos = args.repos.split()
    if not repos:
        refuse("--repos (or GH_REPOS) must name at least one owner/repo")
    for repo in repos:
        if not _REPO.match(repo) or repo.split("/", 1)[1] in (".", ".."):
            refuse(f"bad repo {repo!r} (want owner/repo)")
    if not shutil.which(args.gh):
        refuse(f"no {args.gh!r} on PATH")
    if args.now is not None:
        now = parse_ts(args.now)
        if now is None:
            refuse("--now must be YYYY-MM-DDTHH:MM:SSZ")
    else:
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    jobs = load_thresholds(args.thresholds)
    print(json.dumps(evaluate(repos, jobs, now, args.gh), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
