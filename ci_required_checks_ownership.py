#!/usr/bin/env python3
# ===========================================================================
# ci_required_checks_ownership.py -- does every REQUIRED check have a live
# owner, and has it passed recently?  (case_bab7b60c, child of case_0649a4e4)
#
# Existence ("is there a job that produces this check") is owned by
# ci_required_checks_audit.py (case_d0cad227). This tool adds the ownership layer
# on top of the same live ruleset data:
#
#   * every required check must be listed in required_checks_ownership.json
#     with a non-empty owner_role                       (else FAIL NO_OWNER)
#   * that owner_role must be in the matrix roster with active:true
#     (else FAIL DEPARTED_OWNER; mark departures active:false, never delete)
#   * the named producer check must have a successful job completion on any
#     branch/event within stale_days (default 14). Skips and whole-workflow
#     conclusions are not passing executions of that producer.
#                                                        (else WARN STALE_*)
#     Unreadable or bounded/incomplete history without a measured fresh pass
#     is WARN UNKNOWN_FRESHNESS, never STALE or OK.
#     History reading stops as soon as every producer needed from a workflow
#     has an in-window pass, and each repo has a hard read budget
#     (--max-calls / --max-seconds); exhausting it is UNKNOWN_FRESHNESS.
#
# MODES (read-only; it never writes to GitHub)
#   live, one repo:   --matrix M --repo OWNER/NAME [--branch BRANCH]
#   live, every repo in the matrix: --matrix M --all-repos
#   recorded rules:   --matrix M --repo OWNER/NAME --rules-json FILE
#                     (FILE = the output of
#                     `gh api repos/OWNER/REPO/rules/branches/BRANCH`;
#                     freshness is UNKNOWN without live workflow runs)
#
# EXIT  0 no FAIL   1 at least one FAIL (no live owner)   2 nothing measured
# `--strict` also exits 1 on WARN.
# ===========================================================================
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse

EXIT_OK, EXIT_FAIL, EXIT_UNMEASURED = 0, 1, 2


class InputError(Exception):
    """Bad arguments, an unreadable file or a failed `gh` call: exit 2."""


class BudgetExhausted(InputError):
    """The per-repo read budget ran out: the observation is incomplete."""


# Per-repo ceilings so a busy repo ends in UNKNOWN_FRESHNESS, never a hang.
MAX_CALLS_PER_REPO, MAX_SECONDS_PER_REPO, GH_TIMEOUT = 60, 60.0, 60


class ReadBudget:
    """Hard cap on `gh api` calls and wall-clock time for one repo's history."""

    def __init__(self, max_calls=MAX_CALLS_PER_REPO, max_seconds=MAX_SECONDS_PER_REPO,
                 clock=time.monotonic):
        self.max_calls, self.max_seconds, self.clock = max_calls, max_seconds, clock
        self.calls, self.deadline = 0, clock() + max_seconds

    def begin_call(self):
        """Charge one call; return the subprocess timeout that fits the budget."""
        remaining = self.deadline - self.clock()
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"read budget exhausted after {self.calls} call(s) (--max-calls {self.max_calls})")
        if remaining <= 0:
            raise BudgetExhausted(f"read budget exhausted after {self.max_seconds:g}s (--max-seconds)")
        self.calls += 1
        return max(1.0, min(float(GH_TIMEOUT), remaining))


def _load_json_file(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read {what} {path}: {exc}")


def load_matrix(path):
    doc = _load_json_file(path, "matrix")
    if not isinstance(doc, dict) or not isinstance(doc.get("owners"), dict) or not isinstance(doc.get("repos"), list):
        raise InputError(f"matrix {path}: need top-level `owners` map and `repos` list")
    for row in doc["repos"]:
        if not isinstance(row, dict) or not isinstance(row.get("repo"), str) or not row["repo"]:
            raise InputError(f"matrix {path}: every `repos` row needs a `repo` name")
    return doc


def _gh(gh_bin, *args, timeout=GH_TIMEOUT):
    try:
        proc = subprocess.run([gh_bin, *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # Do not print exception payloads (which may include process output).
        raise InputError(f"gh read unavailable: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise InputError(f"`gh {' '.join(args[:2])}` failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise InputError(f"`gh {' '.join(args[:2])}` returned non-JSON: {exc}")


def required_from_rules(rules):
    if not isinstance(rules, list):
        raise InputError("rules JSON is not a list (expected `gh api repos/OWNER/REPO/rules/branches/BRANCH`)")
    out, seen = [], set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise InputError(f"rules JSON row {i} is not an object")
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        checks = (params.get("required_status_checks") or []) if isinstance(params, dict) else None
        if not isinstance(checks, list):
            raise InputError(f"rules JSON row {i}: `parameters.required_status_checks` is not a list")
        for chk in checks:
            if not isinstance(chk, dict) or not isinstance(chk.get("context"), str) or not chk["context"]:
                raise InputError(f"rules JSON row {i}: a required check has no `context` name")
            key = (chk.get("context"), chk.get("integration_id"))
            if key not in seen:
                seen.add(key)
                out.append({"context": chk.get("context"), "integration_id": chk.get("integration_id")})
    return out


def fetch_required_live(repo, branch, gh_bin):
    if not branch:
        info = _gh(gh_bin, "api", f"repos/{repo}")
        branch = info.get("default_branch") if isinstance(info, dict) else None
        if not branch:
            raise InputError("repository default branch is unavailable")
    rules = _gh(gh_bin, "api", f"repos/{repo}/rules/branches/{branch}")
    return required_from_rules(rules), branch


PAGE_SIZE, MAX_HISTORY_PAGES = 100, 10


def _gh_pages(gh_bin, endpoint, key, budget=None, **filters):
    """Read all pages, or fail explicitly when the observation is incomplete."""
    seen = set()
    for page in range(1, MAX_HISTORY_PAGES + 1):
        query = urllib.parse.urlencode({**filters, "per_page": PAGE_SIZE, "page": page})
        url = f"{endpoint}?{query}"
        doc = _gh(gh_bin, "api", url, timeout=budget.begin_call()) if budget else _gh(gh_bin, "api", url)
        if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
            raise InputError(f"{key} response is malformed")
        total = doc.get("total_count")
        if type(total) is not int or total < 0:
            raise InputError(f"{key} total_count is unavailable")
        rows = doc[key]
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int:
                raise InputError(f"{key} row has no numeric id")
            if row["id"] not in seen:
                seen.add(row["id"])
                yield row
        if len(seen) >= total:
            return
        if not rows:
            raise InputError(f"{key} history incomplete: empty page before total_count")
    raise InputError(f"{key} history truncated after {MAX_HISTORY_PAGES} pages")


def _timestamp(value):
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        return stamp
    except (AttributeError, TypeError, ValueError) as exc:
        raise InputError("producer timestamp is unavailable or malformed") from exc


def _producer_history(repo, workflow_file, gh_bin, stale_days, now, needed=(), budget=None):
    """Observed in-window passing completion per emitted check name.

    Sources: docs.github.com/en/rest/actions/workflow-runs and workflow-jobs.
    Do not filter by workflow success or creation date: a passing producer can
    share a failed workflow, and an old-created run can be rerun today. Only
    jobs of runs updated within the window need reading, and all attempts of a
    run come back in one jobs read. Reading stops as soon as every name in
    `needed` has an in-window pass (that proves freshness; the busiest repos
    have thousands of runs), and a spent `budget` ends the read as incomplete.
    """
    endpoint = f"repos/{repo}/actions/workflows/{os.path.basename(workflow_file)}/runs"
    cutoff = now - dt.timedelta(days=stale_days)
    needed = set(needed)
    passes, errors = {}, []
    try:
        for run in _gh_pages(gh_bin, endpoint, "workflow_runs", budget):
            try:
                updated = _timestamp(run.get("updated_at"))
                if updated > now:
                    raise InputError("workflow update is in the future")
                if updated < cutoff:
                    continue
                jobs_endpoint = f"repos/{repo}/actions/runs/{run['id']}/jobs"
                for job in _gh_pages(gh_bin, jobs_endpoint, "jobs", budget, filter="all"):
                    if job.get("conclusion") != "success":
                        continue
                    name = job.get("name")
                    if not isinstance(name, str) or not name:
                        raise InputError("successful job has no check name")
                    completed = _timestamp(job.get("completed_at"))
                    if completed > now:
                        raise InputError("producer completion is in the future")
                    if completed >= cutoff and (name not in passes or completed > passes[name]):
                        passes[name] = completed
            except BudgetExhausted:
                raise
            except InputError as exc:
                errors.append(str(exc))
            if needed and needed <= passes.keys():
                break  # every wanted producer is fresh: later pages cannot change the verdict
    except InputError as exc:
        errors.append(str(exc))
    return passes, errors, needed


def latest_success_age_days(repo, workflow_file, producer_check, gh_bin,
                            stale_days=14, *, history=None, now=None,
                            needed=(), budget=None):
    """Return ok for an observed fresh producer pass, stale only for a complete
    negative observation, or unknown when unreadable/truncated. Never infer a
    producer pass from a whole-workflow conclusion or a skipped job.

    `needed` lists every producer name wanted from this workflow so one bounded
    history read serves them all; a cached read that stopped before looking for
    `producer_check` is read again rather than trusted as a negative.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if history is None:
        history = {}
    cached = history.get(workflow_file)
    if cached is not None and producer_check not in cached[0] and producer_check not in cached[2]:
        needed = set(needed) | cached[2]
        cached = None
    if cached is None:
        wanted = set(needed) | {producer_check}
        history[workflow_file] = _producer_history(repo, workflow_file, gh_bin, stale_days, now, wanted, budget)
    passes, errors, _ = history[workflow_file]
    completed = passes.get(producer_check)
    if completed is not None:
        age = (now - completed).total_seconds() / 86400
        suffix = "; incomplete history (positive execution measured)" if errors else ""
        return "ok", age, f"observed success of {producer_check!r}: {completed.isoformat()} ({age:.1f}d ago){suffix}"
    if errors:
        return "unknown", None, f"producer history unreadable/incomplete: {errors[0]}"
    return "stale", None, f"complete observed history has no successful job named {producer_check!r} in {stale_days}d"


def _producer_check(entry, ctx):
    # API job `name` is the emitted check context, not the YAML job id.
    # Reusable contexts include the caller prefix (e.g. ci / verify).
    return entry.get("producer_check") or ctx


def audit_repo(owners, matrix_repo, required, live, gh_bin, stale_days,
               max_calls=MAX_CALLS_PER_REPO, max_seconds=MAX_SECONDS_PER_REPO):
    """Findings for one repo: dicts with severity FAIL|WARN|INFO."""
    findings, history = [], {}
    now = dt.datetime.now(dt.timezone.utc)
    budget = ReadBudget(max_calls, max_seconds)
    indexed = {c["context"]: c for c in matrix_repo.get("checks", [])}
    wanted = {}  # producer workflow -> every producer name this audit will ask for
    for chk in required:
        entry = indexed.get(chk["context"]) or {}
        if entry.get("producer_workflow"):
            wanted.setdefault(entry["producer_workflow"], set()).add(_producer_check(entry, chk["context"]))

    def add(sev, code, context, detail):
        findings.append({"severity": sev, "code": code, "context": context, "detail": detail})

    for chk in required:
        ctx = chk["context"]
        entry = indexed.get(ctx)
        if entry is None or not (entry.get("owner_role") or "").strip():
            add("FAIL", "NO_OWNER", ctx, "required check has no matrix entry/owner: add it to required_checks_ownership.json")
            continue
        role = entry["owner_role"].strip()
        info = owners.get(role)
        if info is None:
            add("FAIL", "DEPARTED_OWNER", ctx, f"owner_role {role!r} is not in the matrix roster: add them or reassign the check")
            continue
        if not info.get("active", False):
            add("FAIL", "DEPARTED_OWNER", ctx, f"owner_role {role!r} is marked inactive in the roster: reassign the check")
            continue
        if not live:
            add("WARN", "UNKNOWN_FRESHNESS", ctx, f"owner {role} ({entry.get('area', '?')}); freshness not measured without live runs")
            continue
        wf = entry.get("producer_workflow") or ""
        if not wf:
            add("WARN", "UNKNOWN_FRESHNESS", ctx, f"owner {role}; no producer_workflow in matrix so freshness cannot be measured")
            continue
        state, age, detail = latest_success_age_days(
            matrix_repo["repo"], wf, _producer_check(entry, ctx), gh_bin, stale_days,
            history=history, now=now, needed=wanted.get(wf, ()), budget=budget,
        )
        owner_txt = f"owner {role} ({entry.get('area', '?')})"
        if state == "unknown":
            add("WARN", "UNKNOWN_FRESHNESS", ctx, f"{owner_txt}; {detail}")
        elif state == "stale":
            add("WARN", "STALE_NO_PASS", ctx, f"{owner_txt}; no passing producer run in {stale_days}d: {detail}")
        elif age > stale_days:
            add("WARN", "STALE_NO_PASS", ctx, f"{owner_txt}; {detail} (older than {stale_days}d)")
        else:
            add("INFO", "OK", ctx, f"{owner_txt}; {detail}")
    matrix_names = set(indexed)
    live_names = {c["context"] for c in required}
    for extra in sorted(matrix_names - live_names):
        e = indexed[extra]
        add("INFO", "NOT_REQUIRED", extra, f"matrix owner {e.get('owner_role')} but not currently required on {matrix_repo['branch']}")
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check required-check ownership and freshness (read-only).")
    ap.add_argument("--matrix", required=True, help="path to required_checks_ownership.json")
    ap.add_argument("--repo", help="OWNER/NAME: audit only this matrix repo (default source: live `gh api`)")
    ap.add_argument("--rules-json", help="recorded `gh api repos/OWNER/REPO/rules/branches/BRANCH` output (needs --repo; freshness is UNKNOWN)")
    ap.add_argument("--all-repos", action="store_true", help="every repo listed in the matrix, live via `gh api`")
    ap.add_argument("--branch", default=None, help="target branch (default: matrix branch / repo default)")
    ap.add_argument("--stale-days", type=int, default=None, help="staleness window (default: matrix stale_days)")
    ap.add_argument("--max-calls", type=int, default=MAX_CALLS_PER_REPO, help="hard `gh api` call budget per repo's history read (default %(default)s)")
    ap.add_argument("--max-seconds", type=float, default=MAX_SECONDS_PER_REPO, help="hard wall-clock budget per repo's history read (default %(default)s)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="exit 1 on WARN as well as FAIL")
    ap.add_argument("--gh-bin", default=os.environ.get("AUDIT_GH_BIN", "gh"))
    args = ap.parse_args(argv)

    try:
        if args.rules_json and args.all_repos:
            raise InputError("--rules-json cannot be combined with --all-repos (recorded rules cover one repo)")
        if args.max_calls < 1 or args.max_seconds <= 0:
            raise InputError("--max-calls and --max-seconds must be positive")
        doc = load_matrix(args.matrix)
        owners = doc["owners"]
        try:
            stale_days = args.stale_days if args.stale_days is not None else int(doc.get("stale_days", 14))
        except (TypeError, ValueError):
            raise InputError(f"matrix {args.matrix}: `stale_days` is not an integer")
        by_repo = {r["repo"]: r for r in doc["repos"]}

        targets = []
        if args.all_repos:
            for repo, mr in by_repo.items():
                required, branch = fetch_required_live(repo, args.branch or mr.get("branch", "main"), args.gh_bin)
                targets.append((mr, required, branch, True))
        elif args.repo:
            mr = by_repo.get(args.repo)
            if mr is None:
                raise InputError(f"{args.repo} is not in the matrix: add its checks first (a missing repo is a missing owner)")
            if args.rules_json:
                required = required_from_rules(_load_json_file(args.rules_json, "rules JSON"))
                branch = args.branch or mr.get("branch", "main")
                targets.append((mr, required, branch, False))
            else:
                required, branch = fetch_required_live(args.repo, args.branch or mr.get("branch", "main"), args.gh_bin)
                targets.append((mr, required, branch, True))
        elif args.rules_json:
            raise InputError("pick a matrix repo with --repo (or use --all-repos live)")
    except InputError as exc:
        print(f"ci_required_checks_ownership: {exc}", file=sys.stderr)
        return EXIT_UNMEASURED

    if not targets or all(len(req) == 0 for _, req, _, _ in targets):
        print("ci_required_checks_ownership: no required checks were read, so nothing was measured", file=sys.stderr)
        return EXIT_UNMEASURED

    all_findings, per_repo = [], []
    for mr, required, branch, live in targets:
        f = audit_repo(owners, mr, required, live, args.gh_bin, stale_days, args.max_calls, args.max_seconds)
        all_findings.extend(f)
        per_repo.append({"repo": mr["repo"], "branch": branch, "required": required, "findings": f})

    counts = {s: sum(1 for f in all_findings if f["severity"] == s) for s in ("FAIL", "WARN", "INFO")}
    if args.json:
        print(json.dumps({"stale_days": stale_days, "counts": counts, "repos": per_repo}, indent=2))
    else:
        for mr, required, branch, live in targets:
            print(f"== {mr['repo']}@{branch}: {len(required)} required check(s){'' if live else ' (recorded rules; freshness unknown)'}")
            rows = [x for x in per_repo if x["repo"] == mr["repo"]][0]["findings"]
            for f in rows:
                ctx = f'"{f["context"]}" ' if f["context"] else ""
                print(f'  {f["severity"]:4s} {f["code"]:15s} {ctx}{f["detail"]}')
        print(f'result: {counts["FAIL"]} FAIL, {counts["WARN"]} WARN, {counts["INFO"]} INFO')
    if counts["FAIL"] or (args.strict and counts["WARN"]):
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
