#!/usr/bin/env python3
"""push-runner-census.py — cron: per-label runner census vs queued demand -> Gatus
`platform_runner-census` (case_ebbb469a; parent case_66436e78).

WHY. `push-ci-queue-age.sh` says THAT a job waits; nothing recorded, per label,
how many runners are registered, online and busy. On 2026-10-04 six PRs each
sat 2h+ with one job queued on `self-hosted,two-isolated` and the diagnosis
stayed UNKNOWN. This census makes the three causes distinguishable from its own
output: no online runner (starved), every online runner busy (draining), a
runner carrying both pool labels (mislabeled).

READS (read-only, no host action, no write anywhere but the Gatus push):
  * GET /orgs/<org>/actions/runners, paginated like ci_node93f4bccf_guard.list_runners
    but with total_count cross-checked, so a cut-off read is never a census.
    Needs the org "Self-hosted runners: read" permission. 403 => UNKNOWN.
  * --queued-json FILE|-   queued jobs in the shape push-ci-queue-age.sh walks:
    GET /repos/R/actions/runs/ID/jobs -> `.jobs[]` with status, created_at,
    labels, name. Accepted as a list of jobs, one `{"jobs": [...]}` response, or
    a list of such responses. Only `status == "queued"` jobs count, minus
    `--ignore-job` (default "Failure log drain": it waits on `needs:`, not on
    the fleet — the same exclusion push-ci-queue-age.sh makes).

PER LABEL (default two-isolated two-ephemeral two-selfhosted two-test
two-node93f4bccf): registered / online / busy (online only) / idle / offline, plus the
queued-job count and the age of the oldest queued job that asked for the label.

VERDICTS
  FAIL      starved   queued demand older than --grace-min (default 15) and no
                      online runner carries the label (registered or not).
  FAIL      dual      a registered runner carries BOTH two-isolated and
                      two-selfhosted: exactly one is allowed
                      (docs/private-self-hosted-runner.md, "exactly one of").
  UNKNOWN   an API 403, a malformed body, a truncated or inconsistent
            pagination, an unreadable/stale/malformed queued file, a
            future-dated queued job. Pushed as success=false: a census that
            could not be taken is never green.
  INFO      draining (online>0, all busy), waiting (demand within grace, none
            online), idle-with-queue (idle runner online, demand older than
            grace: look at labels/runner groups, not capacity), absent,
            offline-no-demand, ok. These never fail the push.

PUSH. One endpoint, key `platform_runner-census`, through the shared
`../case_658bf6b9/_gatus_push.sh` (Bearer in a 0600 curl config, never argv). The
host credential reaches this script by inherited environment only: GH_TOKEN for
the GitHub read, GATUS_URL + GATUS_EXTERNAL_TOKEN for the push. GH_TOKEN is
scrubbed from the push subprocess environment. Nothing in argv, stdout, stderr
or the pushed error text carries a credential; runner names are allow-listed
(`[A-Za-z0-9._-]`, 64 chars) before they reach any of those.

EXIT  0 census healthy, pushed success=true
      1 FAIL or UNKNOWN, pushed success=false (or the push itself failed)
      2 refusal before any API call or push (missing env, bad argument)

--no-push prints the census and exits 0/1 without touching Gatus (no GATUS_*
needed): the read an on-call human runs to classify a stalled pickup.

Env: GH_TOKEN, GATUS_URL, GATUS_EXTERNAL_TOKEN, RUNNER_CENSUS_ORG (default
TogetherWeOwn), RUNNER_CENSUS_LABELS (comma list), RUNNER_CENSUS_GRACE_MIN.
Cron (operator; wiring in OPERATOR.md):
  */5 * * * * /opt/ops/paperclip-ops/ops/case_ebbb469a/push-runner-census.py --queued-json /var/lib/status-page/queued-jobs.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PUSH_HELPER = os.path.join(HERE, "..", "case_658bf6b9", "_gatus_push.sh")

PUSH_KEY = "platform_runner-census"
ORG_DEFAULT = "TogetherWeOwn"
API_URL = "https://api.github.com"
DEFAULT_LABELS = ("two-isolated", "two-ephemeral", "two-selfhosted", "two-test", "two-node93f4bccf")
EXCLUSIVE_LABELS = ("two-isolated", "two-selfhosted")
GRACE_MIN_DEFAULT = 15
QUEUED_MAX_AGE_MIN_DEFAULT = 20
IGNORE_JOB_DEFAULT = "Failure log drain"
PER_PAGE = 100
MAX_PAGES = 50
FUTURE_SKEW_S = 300
REQUEST_TIMEOUT = 20
MAX_LABELS = 20

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CensusError(RuntimeError):
    """A census that could not be taken. The message is already log-safe."""


def utcnow():
    return datetime.now(timezone.utc)


def safe_name(value):
    text = str(value)
    return text if NAME_RE.fullmatch(text) else "?"


def parse_time(value, what):
    if not isinstance(value, str) or not value:
        raise CensusError(f"{what} timestamp missing")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CensusError(f"{what} timestamp unreadable") from exc
    if stamp.tzinfo is None:
        raise CensusError(f"{what} timestamp has no timezone")
    return stamp.astimezone(timezone.utc)


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path):
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "paperclip-ops/runner-census",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        req = urllib.request.Request(API_URL + path, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                payload = resp.read()
                return resp.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            raise CensusError(f"github HTTP {exc.code} on {method} {path}") from exc
        except OSError as exc:
            raise CensusError(f"github request failed on {method} {path}: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise CensusError(f"github response was not JSON on {method} {path}") from exc


def list_runners(gh, org):
    runners = []
    total = None
    page = 1
    while True:
        status, payload = gh.request(
            "GET", f"/orgs/{org}/actions/runners?per_page={PER_PAGE}&page={page}"
        )
        if status != 200 or not isinstance(payload, dict):
            raise CensusError(f"runner list returned HTTP {status}")
        batch = payload.get("runners")
        count = payload.get("total_count")
        if not isinstance(batch, list) or not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise CensusError("runner list response malformed (runners/total_count)")
        if total is not None and count != total:
            raise CensusError("runner list total_count changed between pages")
        total = count
        runners.extend(batch)
        if len(runners) >= total or not batch:
            break
        page += 1
        if page > MAX_PAGES:
            raise CensusError("runner list exceeded the page cap")
    if len(runners) != total:
        raise CensusError(f"runner list truncated: read {len(runners)} of {total}")
    return runners


def normalise_runners(raw):
    """Validate the org runner list into [{name, online, busy, labels}]."""
    runners = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise CensusError("runner entry malformed (name)")
        status = item.get("status")
        if status not in ("online", "offline"):
            raise CensusError("runner entry malformed (status)")
        busy = item.get("busy")
        if not isinstance(busy, bool):
            raise CensusError("runner entry malformed (busy)")
        raw_labels = item.get("labels")
        if not isinstance(raw_labels, list) or not all(
            isinstance(l, dict) and isinstance(l.get("name"), str) for l in raw_labels
        ):
            raise CensusError("runner entry malformed (labels)")
        runners.append({
            "name": item["name"],
            "online": status == "online",
            "busy": busy,
            "labels": frozenset(l["name"].strip().lower() for l in raw_labels),
        })
    return runners


def flatten_jobs(doc):
    if isinstance(doc, dict) and "jobs" in doc:
        doc = doc["jobs"]
    if not isinstance(doc, list):
        raise CensusError("queued-json is not a jobs list")
    jobs = []
    for item in doc:
        if not isinstance(item, dict):
            raise CensusError("queued-json entry is not an object")
        if "jobs" in item and "status" not in item:
            page = item["jobs"]
            if not isinstance(page, list) or not all(isinstance(j, dict) for j in page):
                raise CensusError("queued-json jobs page malformed")
            jobs.extend(page)
        else:
            jobs.append(item)
    return jobs


def load_queued(path, ignore_job, now, max_age_min):
    """Queued jobs as [{labels, created}]; raises CensusError on anything unreadable."""
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            age_s = now.timestamp() - os.stat(path).st_mtime
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise CensusError("queued-json unreadable") from exc
        if age_s > max_age_min * 60 or age_s < -FUTURE_SKEW_S:
            raise CensusError(f"queued-json stale or future-dated ({int(age_s // 60)}m old, max {max_age_min}m)")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise CensusError("queued-json is not JSON") from exc
    queued = []
    for job in flatten_jobs(doc):
        if job.get("status") != "queued" or job.get("name") == ignore_job:
            continue
        labels = job.get("labels")
        if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
            raise CensusError("queued job malformed (labels)")
        created = parse_time(job.get("created_at"), "queued job")
        if (created - now).total_seconds() > FUTURE_SKEW_S:
            raise CensusError("queued job is future-dated")
        queued.append({"labels": frozenset(l.strip().lower() for l in labels), "created": created})
    return queued


def evaluate(runners, queued, labels, grace_min, now):
    """Pure verdict: runners from normalise_runners, queued from load_queued."""
    grace_s = grace_min * 60
    rows = []
    reasons = []
    for label in labels:
        carrying = [r for r in runners if label in r["labels"]]
        online = [r for r in carrying if r["online"]]
        busy = [r for r in online if r["busy"]]
        asking = [j for j in queued if label in j["labels"]]
        oldest_s = max((max((now - j["created"]).total_seconds(), 0) for j in asking), default=None)
        row = {
            "label": label,
            "registered": len(carrying),
            "online": len(online),
            "busy": len(busy),
            "idle": len(online) - len(busy),
            "offline": len(carrying) - len(online),
            "queued": len(asking),
            "oldest_queued_min": None if oldest_s is None else int(oldest_s // 60),
        }
        if asking and not online and oldest_s > grace_s:
            row["state"] = "starved"
            reasons.append(
                f"{label} starved: reg={row['registered']} on=0 queued={row['queued']} "
                f"oldest={row['oldest_queued_min']}m grace={grace_min}m"
            )
        elif asking and not online:
            row["state"] = "waiting"
        elif online and len(busy) == len(online):
            row["state"] = "draining"
        elif online and asking and oldest_s > grace_s:
            row["state"] = "idle-with-queue"
        elif not carrying and not asking:
            row["state"] = "absent"
        elif not online:
            row["state"] = "offline-no-demand"
        else:
            row["state"] = "ok"
        rows.append(row)
    dual = sorted(safe_name(r["name"]) for r in runners if set(EXCLUSIVE_LABELS) <= r["labels"])
    if dual:
        reasons.append("dual-labeled " + "+".join(EXCLUSIVE_LABELS) + ": " + ",".join(dual))
    return {
        "verdict": "FAIL" if reasons else "PASS",
        "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grace_min": grace_min,
        "runners": len(runners),
        "labels": rows,
        "dual_labeled": dual,
        "reasons": reasons,
    }


def render_text(result):
    lines = [
        "runner-census now={now} grace={grace_min}m runners={runners}".format(**result)
    ]
    for row in result["labels"]:
        oldest = "-" if row["oldest_queued_min"] is None else f"{row['oldest_queued_min']}m"
        lines.append(
            "label={label} registered={registered} online={online} busy={busy} idle={idle} "
            "offline={offline} queued={queued} oldest_queued={oldest} state={state}".format(
                oldest=oldest, **row
            )
        )
    lines.append("dual_labeled=" + (",".join(result["dual_labeled"]) or "-"))
    lines.append("verdict=" + result["verdict"] + ("".join(f"\n  - {r}" for r in result["reasons"])))
    return "\n".join(lines)


def push(key, success, error):
    """POST through the shared helper. GH_TOKEN never reaches the push subprocess."""
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    proc = subprocess.run(
        ["bash", "-c", '. "$1" || exit 2; gatus_push "$2" "$3" "$4"',
         "gatus-push", PUSH_HELPER, key, "true" if success else "false", error or ""],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def parse_args(argv):
    ap = argparse.ArgumentParser(description="per-label runner census vs queued demand")
    ap.add_argument("--queued-json", required=True, metavar="FILE",
                    help="queued jobs JSON (jobs-API shape); '-' reads stdin")
    ap.add_argument("--org", default=os.environ.get("RUNNER_CENSUS_ORG", ORG_DEFAULT))
    ap.add_argument("--labels", default=os.environ.get("RUNNER_CENSUS_LABELS", ",".join(DEFAULT_LABELS)))
    ap.add_argument("--grace-min", default=os.environ.get("RUNNER_CENSUS_GRACE_MIN", str(GRACE_MIN_DEFAULT)))
    ap.add_argument("--queued-max-age-min", default=str(QUEUED_MAX_AGE_MIN_DEFAULT))
    ap.add_argument("--ignore-job", default=IGNORE_JOB_DEFAULT)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--no-push", action="store_true", help="print the census; never touch Gatus")
    return ap.parse_args(argv)


def refuse(message):
    print(f"push-runner-census: refusing — {message}", file=sys.stderr)
    return 2


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        return refuse("GH_TOKEN is unset (inherited environment only)")
    if not args.no_push and not (os.environ.get("GATUS_URL") and os.environ.get("GATUS_EXTERNAL_TOKEN")):
        return refuse("GATUS_URL and GATUS_EXTERNAL_TOKEN must be set (or use --no-push)")
    if not ORG_RE.fullmatch(args.org):
        return refuse("bad --org")
    for name, value in (("--grace-min", args.grace_min), ("--queued-max-age-min", args.queued_max_age_min)):
        if not re.fullmatch(r"[1-9][0-9]{0,4}", str(value)):
            return refuse(f"{name} must be a positive whole number of minutes")
    labels = []
    for raw in args.labels.split(","):
        label = raw.strip().lower()
        if not LABEL_RE.fullmatch(label):
            return refuse(f"bad label {safe_name(raw.strip())!r} in --labels")
        if label not in labels:
            labels.append(label)
    if not labels or len(labels) > MAX_LABELS:
        return refuse(f"--labels needs 1..{MAX_LABELS} labels")
    grace_min = int(args.grace_min)

    now = utcnow()
    try:
        queued = load_queued(args.queued_json, args.ignore_job, now, int(args.queued_max_age_min))
        runners = normalise_runners(list_runners(GitHub(token), args.org))
    except CensusError as exc:
        text = f"runner-census UNKNOWN: {exc}"
        print(json.dumps({"verdict": "UNKNOWN", "reasons": [str(exc)]}) if args.format == "json" else text)
        if args.no_push:
            return 1
        push(PUSH_KEY, False, text)
        return 1

    result = evaluate(runners, queued, labels, grace_min, now)
    print(json.dumps(result, sort_keys=True) if args.format == "json" else render_text(result))
    healthy = result["verdict"] == "PASS"
    if args.no_push:
        return 0 if healthy else 1
    error = "" if healthy else "runner-census FAIL: " + "; ".join(result["reasons"])
    rc = push(PUSH_KEY, healthy, error)
    if rc != 0:
        return 1
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
