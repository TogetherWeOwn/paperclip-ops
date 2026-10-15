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

Active only while STATE/ACTIVE exists. Usage: focus_gate.py [--apply] [--loop SECONDS]
Lift: rm STATE/ACTIVE, then focus_gate.py --rewake --apply (re-wakes recorded cards still open).

Every docker/API subprocess call below is bounded (FOCUS_GATE_DB_TIMEOUT_S /
FOCUS_GATE_API_TIMEOUT_S, env-overridable; curl keeps its own -m 60 with the
process backstop above it). A timeout is unmeasured, never a cancel: it logs
event="error" and the loop keeps going. A db timeout returns (None, 0) so a
one-shot run prints "unmeasured" instead of a measured zero.
"""
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
APPLY = "--apply" in sys.argv


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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=API_TIMEOUT_S).stdout


def log(**kw):
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


def tick():
    try:
        rows = db(f"""{allowed_sql()}
, q as (select r.id, r.agent_id, r.company_id, r.context_snapshot->>'issueId' iid, r.context_snapshot->>'wakeReason' why
        from heartbeat_runs r where r.status='queued')
select q.id, q.agent_id, coalesce(q.iid,'-'), coalesce(i.identifier,'-'), coalesce(q.why,'-'), coalesce(i.status,'-')
from q left join issues i on i.id::text=q.iid
where not (q.company_id='{CID}' and (
  (q.iid is null and q.agent_id='{CEO}') or q.iid in (select id::text from blk)));""")
    except subprocess.TimeoutExpired:
        # Unmeasured tick: the queue was never read, so nothing may be cancelled.
        # Return None (not 0) so a one-shot run cannot mistake this for a
        # measured all-clear; main() prints an explicit unmeasured line.
        log(event="error", error=f"db timeout after {DB_TIMEOUT_S:g}s; unmeasured, nothing cancelled")
        return None, 0
    cancelled = 0
    for run_id, agent, iid, ident, why, istatus in rows:
        try:
            code = api("POST", f"/heartbeat-runs/{run_id}/cancel") if APPLY else "dry"
        except subprocess.TimeoutExpired:
            # Unmeasured row: the cancel never got an answer, so it is not recorded.
            log(event="error", run=run_id, agent=agent, issue=iid, ident=ident,
                error=f"api timeout after {API_TIMEOUT_S:g}s; unmeasured, nothing cancelled")
            continue
        log(event="cancel", run=run_id, agent=agent, issue=iid, ident=ident, why=why, issueStatus=istatus, code=code)
        if code == "200":
            cancelled += 1
            with open(f"{STATE}/cancelled.tsv", "a") as f:
                f.write("\t".join([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), run_id, agent, iid, ident, why]) + "\n")
    return len(rows), cancelled


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
            st = db(f"select status, coalesce(assignee_agent_id::text,'') from issues where id='{iid}'")
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


def main():
    os.makedirs(STATE, exist_ok=True)
    if "--rewake" in sys.argv:
        rewake()
        return
    interval = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    while True:
        if os.path.exists(f"{STATE}/ACTIVE"):
            try:
                n, cancelled = tick()
                if n is None:
                    # Unmeasured tick (db timeout): never print a measured
                    # zero. One-shot runs say so visibly; loop mode stays
                    # quiet (the error is in gate.jsonl) and keeps going.
                    if not interval:
                        print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} unmeasured (db timeout; nothing cancelled)",
                              flush=True)
                elif n or not interval:
                    outcome = f"cancelled: {cancelled}" if APPLY else "dry run"
                    print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} non-focus queued: {n} ({outcome})", flush=True)
            except Exception as e:  # keep the loop alive through DB/API blips
                log(event="error", error=str(e)[:300])
        if not interval:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
