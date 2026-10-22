#!/usr/bin/env python3
"""Owner focus gate (owner directive 2026-09-28 ~19:35Z): only two-web and two-bot work may START.
Narrowed 2026-09-30 ~17:50Z (owner: "Next only, full stop"): only two-web-next / two-bot-next work; the legacy
TWO Web and Community Platform projects remain parked until cutover.
Owner 2026-10-02: ALL router/cost-control and scoped CI/autoscaling/hosthook work
is admitted ahead of Next; priority closure is shared with idle_waker.

Running runs are never touched. Every tick, queued runs whose issue is outside the focus set are
cancelled through the board cancel route (operator-stamped, so recovery stands down), and each one
is recorded in STATE/cancelled.tsv so the cards can be re-woken when the owner lifts the focus.

Focus set = issues in the focus projects (TWO Web Next, TWO Bot Next) or whose title names
two-web-next/two-bot-next, plus identifiers in STATE/allow.txt, then closed over: descendants, ancestors,
and open transitive blockers (infra/review cards a TWO card waits on are TWO work).
Runs with no issue are allowed only for the CEO.

Historical policy, scoped to the configured company CID; source repair does not authorize
activation of historical focus restrictions contrary to current owner priorities.
Active only while STATE/ACTIVE exists. Preview by default; --help describes the contract.
See docs/operator/focus-gate.md before any separately authorized operation.

Every docker/API subprocess call below is bounded (FOCUS_GATE_DB_TIMEOUT_S /
FOCUS_GATE_API_TIMEOUT_S, env-overridable; curl keeps its own -m 60 with the
process backstop above it). A timeout is unmeasured, never a cancel: it logs
event="error" and the loop keeps going. A db timeout returns (None, 0) so a
one-shot run prints "unmeasured" instead of a measured zero.
"""
import argparse
import json, os, subprocess, sys, time, urllib.request

CID = "00000000-0000-4000-8000-000000000004"
CEO = "00000000-0000-4000-8000-000000000031"
# TWO Web Next, TWO Bot Next only (owner 2026-09-30). Legacy TWO Web 867f7323 and Community Platform 4c57214d
# were in the set until 2026-09-30 17:50Z.
FOCUS_PROJECTS = ("00000000-0000-4000-8000-000000000032", "00000000-0000-4000-8000-000000000033",
                  # Owner 2026-10-08 ~22:50Z: Model Capacity Plugin is the #1 priority above all projects.
                  "00000000-0000-4000-8000-000000000034",
                  # Owner 2026-10-01 13:2xZ: model router work runs in parallel with TWO Next.
                  # "00000000-0000-4000-8000-000000000035",  # Model Router Plugin PAUSED 2026-10-08 (owner)
                  # Owner 2026-10-01 20:2xZ: kagi-shinobi (agent search/scrape gateway) adopted and maintained.
                  "00000000-0000-4000-8000-000000000036",
                  # Owner 2026-10-03 ~20:45Z: Opportunity Studio approved ("yes get it implemented").
                  "00000000-0000-4000-8000-000000000037")
# Owner 2026-10-02: platform bottleneck work precedes Next; unrelated legacy stays parked.
PLATFORM_PROJECTS = (# "00000000-0000-4000-8000-000000000035",  # Model Router Plugin PAUSED 2026-10-08 (owner): superseded by Model Capacity Plugin
                     "00000000-0000-4000-8000-000000000038",  # Model Cost Control
                     # Owner 2026-10-03: decisions must never wait on a human (Decisions page router plugin).
                     "00000000-0000-4000-8000-000000000039",  # Decision Router Plugin
                     # Owner 2026-10-05: generic code goes public (repo split routing fix).
                     "00000000-0000-4000-8000-000000000040",  # Operator Toolkit (public)
                     # 2026-10-05: legacy projects are parked natively (cards -> backlog, park_projects.py), so the
                     # platform repos are admitted whole. Topic-only admission was cancelling platform work such as
                     # PR reviews and upstream audits (case_88bfa467 x12, case_37206910, case_f38b5e34).
                     "00000000-0000-4000-8000-000000000005",  # Ops Tooling
                     "00000000-0000-4000-8000-000000000041")  # Paperclip Fork
PLATFORM_CARDS = ("case_91cf972c", "case_84e31ed9", "case_5467b6f7", "case_de994047", "case_24119081",
                  "case_9e61bb31", "case_be6ca7bf", "case_03778cec", "case_6dfea8e7", "case_acd5b66d", "case_6bb27f43")
# Topic admission is scoped: never admit all Ops Tooling or legacy product CI by project alone.
PLATFORM_TOPIC_PROJECTS = ("00000000-0000-4000-8000-000000000005",  # Ops Tooling
                           "00000000-0000-4000-8000-000000000041",  # Paperclip Fork
                           "00000000-0000-4000-8000-000000000032",  # TWO Web Next
                           "00000000-0000-4000-8000-000000000033") # TWO Bot Next
STATE = os.path.expanduser("~/.local/state/paperclip-focus")
REPO = "/home/ubuntu/paperclip-enterprise-company"
API = "http://127.0.0.1:3100/api"
APPLY = False  # CLI flags are validated in main(), never at import time.


def _seconds(name, default):
    try:
        v = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        raise SystemExit(f'{name} must be seconds in (0, 3600], got {os.environ.get(name)!r}')
    if not 0 < v <= 3600:
        raise SystemExit(f'{name} must be seconds in (0, 3600], got {v}')
    return v


# A hung docker daemon or API call stalled the whole gate in --loop mode until
# someone killed it by hand. Both subprocess calls are bounded; a timeout is
# unmeasured, never a cancel.
DB_TIMEOUT_S = _seconds('FOCUS_GATE_DB_TIMEOUT_S', 60)
CURL_MAX_TIME_S = 60  # curl's own -m; the process backstop below always sits above it
API_TIMEOUT_S = _seconds('FOCUS_GATE_API_TIMEOUT_S', 70)
if API_TIMEOUT_S <= CURL_MAX_TIME_S:
    raise SystemExit(f'FOCUS_GATE_API_TIMEOUT_S must exceed curl -m {CURL_MAX_TIME_S:g}s, got {API_TIMEOUT_S:g}s')


def db(sql):
    out = subprocess.run(["docker", "exec", "-i", "paperclip-db", "psql", "-U", "paperclip", "-d", "paperclip",
                          "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1"], input=sql, capture_output=True, text=True, check=True,
                         timeout=DB_TIMEOUT_S)
    return [l.split("\t") for l in out.stdout.splitlines() if l]


def api(method, path, body=None):
    cmd = ["curl", "-sK", f"{REPO}/.pc.curl", "-m", str(CURL_MAX_TIME_S), "-X", method, "-H", "content-type: application/json",
           "-o", "/tmp/focus-gate-resp.json", "-w", "%{http_code}", API + path]
    if body is not None:
        cmd[1:1] = ["--data-binary", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=API_TIMEOUT_S)
    return out.stdout if out.returncode == 0 else "000"


def log(**kw):
    if not os.path.isdir(STATE):
        return  # A preview must not create enforcement state.
    kw["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(f"{STATE}/gate.jsonl", "a") as f:
        f.write(json.dumps(kw) + "\n")


def allowed_sql(platform_only=False):
    allow = []
    p = f"{STATE}/allow.txt"
    if os.path.exists(p):
        allow = [l.strip() for l in open(p) if l.strip() and not l.startswith("#")]
    allow_list = ",".join("'" + a.replace("'", "") + "'" for a in allow) or "''"
    # Supply tier (owner "GO SUPPLY" 2026-10-07): supply_tier.py admits next-priority projects only while paid
    # allowance is under pace and the focus scope has no ready work. Missing/invalid file = tier 0.
    supply = []
    try:
        st = json.load(open(f"{STATE}/supply_tier.json"))
        supply = [x for x in st.get("project_ids", []) if all(c in "0123456789abcdef-" for c in x)]
    except (OSError, ValueError, AttributeError):
        supply = []
    supply_list = ",".join(f"'{x}'" for x in supply) or "''"
    projects = ",".join(f"'{x}'" for x in FOCUS_PROJECTS)
    platform_projects = ','.join(f"'{x}'" for x in PLATFORM_PROJECTS)
    topic_projects = ','.join(f"'{x}'" for x in PLATFORM_TOPIC_PROJECTS)
    cards = ','.join(f"'{x}'" for x in PLATFORM_CARDS)
    platform = rf"""project_id in ({platform_projects}) or identifier in ({cards})
      or ((project_id in ({topic_projects}) or project_id is null)
          and identifier not in ('case_1a8b3477','case_d9fc684a') -- legacy deploy umbrella / mixed-priority master are not topic roots
          and title ~* '\mCI\M|autoscal|host.?hook|\mGARM\M|model.selection|model.router|\mrouter\M|runner|agent.admission')"""
    scope = platform if platform_only else f"""({platform}) or project_id in ({projects})
      or title ~* '(two-web-next|two-bot-next|two web next|two bot next)' or identifier in ({allow_list})
      or project_id::text in ({supply_list})
      or (origin_kind='routine_execution' and origin_id='00000000-0000-4000-8000-000000000042')
      or title ~* '^Improvement experiment:'
      -- 2026-10-08: fresh no-project cards are current governance work (COO admissions, CTO/CISO decisions,
      -- host-operator cards); the gate cancelled them on first wake. Legacy backlog always carries a project.
      or (project_id is null and created_at > now() - interval '72 hours')
      -- owner Discord chat (2026-10-07): native endpoints allow only the linked owner, so these are owner asks
      or id in (select c.issue_id from chat_conversations c join chat_endpoints e on e.id=c.endpoint_id
                where e.provider='discord' and c.issue_id is not null)"""
    return f"""
with recursive roots as (
  select id from issues where company_id='{CID}' and ({scope})),
down as (select id from roots union select i.id from issues i join down d on i.parent_id=d.id),
up as (select id from down union select i.parent_id from issues i join up u on i.id=u.id where i.parent_id is not null),
blk as (select id from up union
  select r.issue_id from issue_relations r join blk b on r.related_issue_id=b.id join issues x on x.id=r.issue_id
  where r.type='blocks' and x.status not in ('done','cancelled'))"""


def result_summary():
    return {"version": 1, "company_id": CID, "dry_run": not APPLY,
            "measurement": "inactive", "error_code": None,
            "counts": {"candidates": 0, "refused": 0, "confirmed": 0},
            "runs": [], "next_action": "leave_inactive"}


def tick(summary=None):
    # Optional result sink; existing callers still receive the same two-tuple.
    if summary is None:
        summary = result_summary()
    summary.update(measurement="unmeasured", next_action="inspect_query_failure")
    try:
        rows = db(f"""{allowed_sql()}
, q as (select r.id, r.agent_id, r.company_id, r.context_snapshot->>'issueId' iid, r.context_snapshot->>'wakeReason' why
        from heartbeat_runs r where r.status='queued')
select q.id, q.agent_id, coalesce(q.iid,'-'), coalesce(i.identifier,'-'), coalesce(q.why,'-'), coalesce(i.status,'-'), q.company_id
from q left join issues i on i.id::text=q.iid
where q.company_id='{CID}' and not (
  (q.iid is null and q.agent_id='{CEO}') or q.iid in (select id::text from blk));""")
    except subprocess.TimeoutExpired:
        summary["error_code"] = "query_timeout"
        log(event="error", error=f"db timeout after {DB_TIMEOUT_S:g}s; unmeasured, nothing cancelled")
        return None, 0
    except Exception:
        summary["error_code"] = "query_failed"
        raise  # Preserve the direct tick() caller's existing failure contract.
    summary.update(measurement="measured", next_action="none")
    for row in rows:
        # Legacy adapters supply six already-scoped columns. The live SQL now
        # projects company_id as column seven (including an empty NULL field).
        if len(row) == 6:
            row = [*row, CID]
        company = row[6] if len(row) == 7 else None
        entry = {"run_id": row[0] if row else None, "company_id": company,
                 "decision": "refused", "scope_code": "inconsistent_company",
                 "error_code": None, "next_action": "inspect_candidate_adapter"}
        summary["runs"].append(entry)
        if len(row) != 7 or not company:
            entry["error_code"] = "inconsistent_row"
        elif company != CID:
            entry.update(scope_code="foreign_company", error_code="company_mismatch")
        else:
            entry.update(decision="candidate", scope_code="configured_company_excluded",
                         next_action="review_current_policy_before_apply")
        # Refuse even an inconsistent adapter result before calling a mutation.
        if entry["decision"] == "refused":
            summary["counts"]["refused"] += 1
            continue
        run_id, agent, iid, ident, why, istatus, _ = row
        summary["counts"]["candidates"] += 1
        try:
            code = api("POST", f"/heartbeat-runs/{run_id}/cancel") if APPLY else "dry"
        except (subprocess.TimeoutExpired, OSError):
            entry.update(decision="unknown", error_code="cancel_transport_unknown",
                         next_action="verify_run_state_before_retry")
            log(event="error", run=run_id, agent=agent, issue=iid, ident=ident,
                error="api outcome unknown; not recorded as cancelled")
            continue
        log(event="cancel", run=run_id, agent=agent, issue=iid, ident=ident, why=why, issueStatus=istatus, code=code)
        if code == "200":
            entry.update(decision="confirmed", next_action="none")
            summary["counts"]["confirmed"] += 1
            with open(f"{STATE}/cancelled.tsv", "a") as f:
                f.write("\t".join([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), run_id, agent, iid, ident, why]) + "\n")
        elif APPLY:
            if isinstance(code, str) and len(code) == 3 and code.isdigit() and code.startswith("4"):
                entry.update(decision="refused", error_code="cancel_refused",
                             next_action="inspect_refusal_no_credential_retry")
                summary["counts"]["refused"] += 1
            else:
                entry.update(decision="unknown", error_code="cancel_response_unknown",
                             next_action="verify_run_state_before_retry")
    if any(r["decision"] == "unknown" for r in summary["runs"]):
        summary["next_action"] = "verify_run_state_before_retry"
    elif summary["counts"]["refused"]:
        summary["next_action"] = "inspect_refusals"
    elif not APPLY and summary["counts"]["candidates"]:
        summary["next_action"] = "review_current_policy_before_apply"
    return summary["counts"]["candidates"], summary["counts"]["confirmed"]


def rewake():
    seen = set()
    with open(f"{STATE}/cancelled.tsv") as f:
        lines = f.readlines()
    for line in lines:
        _, _, agent, iid, ident, _ = line.rstrip("\n").split("\t")
        if iid == "-" or iid in seen:
            continue
        seen.add(iid)
        try:
            st = db(f"select status, coalesce(assignee_agent_id::text,'') from issues where id='{iid}' and company_id='{CID}'")
        except subprocess.TimeoutExpired:
            log(event="error", issue=iid, ident=ident,
                error=f"db timeout after {DB_TIMEOUT_S:g}s; rewake unmeasured, skipped")
            continue
        if not st or st[0][0] not in ("todo", "in_progress") or st[0][1] != agent:
            log(event="rewake_skip", ident=ident, state=st[0] if st else None)
            continue
        try:
            code = api("POST", f"/agents/{agent}/wakeup", {"source": "on_demand", "reason": "focus_lifted",
                       "payload": {"issueId": iid}}) if APPLY else "dry"
        except subprocess.TimeoutExpired:
            log(event="error", issue=iid, ident=ident, agent=agent,
                error=f"api timeout after {API_TIMEOUT_S:g}s; rewake unmeasured")
            continue
        log(event="rewake", ident=ident, agent=agent, code=code)


def arguments():
    parser = argparse.ArgumentParser(
        description="Historical focus gate: only the configured company CID is in scope. "
                    "Company restriction precedes the existing focus exclusion; foreign or "
                    "inconsistent company rows are refused before cancellation.",
        epilog="This does not authorize activation of historical focus restrictions "
               "contrary to current owner priorities. No host, DB or cross-company authority "
               "is granted by this source change. See docs/operator/focus-gate.md.\n\n"
               "Offline check: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover "
               "-s ops/paperclip -p 'test_focus*.py'",
        formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply only separately authorized operations")
    mode.add_argument("--dry-run", action="store_true", help="preview only (default); never cancel or wake")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="text (compatible default) or version 1 JSON summary per tick")
    parser.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="repeat at this positive interval")
    parser.add_argument("--rewake", action="store_true", help="preview recorded open cards; requires --apply to wake")
    args = parser.parse_args()
    if args.loop < 0:
        parser.error("--loop must be zero (one shot) or a positive interval")
    if args.rewake and args.format == "json":
        parser.error("--format json describes cancellation candidates, not --rewake")
    return args


def main():
    global APPLY
    args = arguments()  # Validate all flags before filesystem, DB or API effects.
    previous_apply = APPLY
    APPLY = False if args.dry_run else args.apply or APPLY
    try:
        if APPLY:
            os.makedirs(STATE, exist_ok=True)
        if args.rewake:
            rewake()
            return 0
        while True:
            summary = result_summary()
            if os.path.exists(f"{STATE}/ACTIVE"):
                try:
                    n, cancelled = tick(summary)
                    if args.format == "text":
                        if n is None:
                            if not args.loop:
                                print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} unmeasured (db timeout; nothing cancelled)",
                                      flush=True)
                        elif n or not args.loop:
                            outcome = f"cancelled: {cancelled}" if APPLY else "dry run"
                            print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} non-focus queued: {n} ({outcome})", flush=True)
                except Exception:  # keep the loop alive, without leaking raw subprocess errors
                    summary.update(measurement="unmeasured",
                                   error_code=summary["error_code"] or "query_or_processing_failed",
                                   next_action="inspect_failure_before_retry")
                    log(event="error", error=summary["error_code"])
                    if args.format == "text" and not args.loop:
                        print("unmeasured (query or processing failed; inspect diagnostic log)", flush=True)
            if args.format == "json":
                print(json.dumps(summary, sort_keys=True), flush=True)
            if not args.loop:
                return int(bool(summary["error_code"] or summary["counts"]["refused"] or
                                any(r["decision"] == "unknown" for r in summary["runs"])))
            time.sleep(args.loop)
    finally:
        APPLY = previous_apply


if __name__ == "__main__":
    sys.exit(main())
