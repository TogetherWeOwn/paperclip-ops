#!/usr/bin/env python3
"""PR hygiene sweep for every non-archived TogetherWeOwn repo (operator timer, every 30 min; owner 2026-10-04:
"every PR babysat until merge"; was two-web-next / two-bot-next only).

Root cause it guards (2026-10-01): GitHub silently skips pull_request workflows on a PR that
conflicts with main, so only CodeQL runs. Agents read the missing `check` as "CI dispatch is not
authorized" and opened permission/receipt card chains instead of merging main.

For each open non-release PR:
  * dirty (conflicts)      -> comment on its open card telling the author to merge main and push
  * clean + required green -> comment telling the assignee to get it reviewed/merged
  * red CI on the head       -> comment naming the failing checks and telling the author to fix them
At most one nudge per (PR, head sha, kind) per 6h. A blocked card with no open blocker edge is moved
to todo so the comment actually dispatches. --dry-run prints only. PAUSE file stops it.
Owner: a card with a repo-qualified pull_request work product for the PR, or a card whose branch names it
and whose registered workspace/project repo is that PR's repo (owner_match.py). Body text and bare PR
numbers never choose an owner; ambiguous or unmatched PRs are logged and skipped.
"""
import json, os, re, subprocess, sys, time, urllib.request, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
from live_run_guard import issue_has_live_work  # never comment on a card with live work (comment cancels retries)
from owner_match import choose_owner
STATE = os.path.join(HERE, "state.json"); LOG = os.path.join(HERE, "sweep.log")
C = "00000000-0000-4000-8000-000000000004"
REPOS = None  # discovered each run: every non-archived org repo
DRY = "--dry-run" in sys.argv
if os.path.exists(os.path.join(HERE, "PAUSE")): sys.exit(0)
subprocess.run(["bash", os.path.expanduser("~/paperclip-enterprise-company/ops/github/mint-installation-token.sh")], check=True, capture_output=True)
T = open("/tmp/gh-inst-token").read().strip()
def gh(path):
    r = urllib.request.Request("https://api.github.com/" + path, headers={"Authorization": "Bearer " + T, "Accept": "application/vnd.github+json", "User-Agent": "op-pr-hygiene"})
    try: return json.load(urllib.request.urlopen(r, timeout=30))
    except urllib.error.HTTPError as e: return {"_err": e.code}
def sql(q):
    r = subprocess.run(["docker", "exec", "-i", "paperclip-db", "psql", "-U", "paperclip", "-d", "paperclip", "-At", "-F", "|", "-c", q], capture_output=True, text=True)
    if r.returncode: log(f"sql failed: {r.stderr.strip()[:200]}"); return ""
    return r.stdout.strip()
def api(method, path, body):
    out = subprocess.run(["curl", "-sS", "-K", os.path.expanduser("~/paperclip-enterprise-company/.pc.curl"), "-m", "90", "-X", method, "-H", "content-type: application/json",
                          f"http://127.0.0.1:3100/api/{path}", "-d", json.dumps(body)], capture_output=True, text=True).stdout
    try: return json.loads(out)
    except Exception: return {"_raw": out[:200]}
def api_get(path):
    out = subprocess.run(["curl", "-sS", "-K", os.path.expanduser("~/paperclip-enterprise-company/.pc.curl"), "-m", "90",
                          f"http://127.0.0.1:3100/api/{path}"], capture_output=True, text=True).stdout
    try: return json.loads(out)
    except Exception: return None
def card_facts(iid):
    issue = api_get(f"issues/{iid}")
    products = api_get(f"issues/{iid}/work-products")
    if not isinstance(issue, dict) or issue.get("id") != iid or not isinstance(products, list): return None
    workspace_id, project_id = issue.get("executionWorkspaceId"), issue.get("projectId")
    workspace = api_get(f"execution-workspaces/{workspace_id}") if workspace_id else {}
    project = api_get(f"projects/{project_id}") if project_id else {}
    if workspace_id and not (isinstance(workspace, dict) and workspace.get("id") == workspace_id): return None
    if project_id and not (isinstance(project, dict) and project.get("id") == project_id): return None
    return {"products": products, "workspace_repo": workspace.get("repoUrl"),
            "project_repos": [w.get("repoUrl") for w in project.get("workspaces") or []]}
def _plain(text):
    return re.sub(r"[^A-Za-z0-9._/()+-]", "_", str(text))[:80]
def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + msg
    print(line)
    if not DRY: open(LOG, "a").write(line + "\n")
state = json.load(open(STATE)) if os.path.exists(STATE) else {}
now = time.time()
state = {k: v for k, v in state.items() if now - v < 86400}
MSG = {
 "dirty": ("Operator PR-hygiene sweep: {repo} PR #{n} (`{ref}`, head {sha}) **conflicts with main** "
           "(mergeable_state=dirty, {behind} commits behind). GitHub does not run pull_request workflows on a conflicting PR, "
           "which is why only CodeQL ran and `check`/`secret-scan`/`pr-lint` are missing. This is NOT a CI-authorization, "
           "dispatch, credential or receipt problem and needs no new card: merge `origin/main` into the branch (or rebase), "
           "resolve conflicts, run the tests locally, push. CI starts by itself on the push. Then route the PR to review."),
 "red": ("Operator PR-hygiene sweep: {repo} PR #{n} (`{ref}`, head {sha}) has **failing CI**: {failing}. "
         "Fix the failing checks on this branch and push (done = merged). If a failure is unrelated flake, re-run that job once and say so here."),
 "green": ("Operator PR-hygiene sweep: {repo} PR #{n} (`{ref}`, head {sha}) is **mergeable and all checks are green**. "
           "Get it reviewed and squash-merged now (done = merged). If you are the reviewer, review it this run."),
}
REPOS = [r["name"] for page in range(1, 4) for r in (gh(f"orgs/TogetherWeOwn/repos?per_page=100&page={page}") or [])
         if isinstance(r, dict) and not r.get("archived")]
FIRST = ["two-web-next", "two-bot-next", "paperclip-ops", "kofra", "nntune"]
REPOS = sorted(REPOS, key=lambda r: (FIRST.index(r) if r in FIRST else len(FIRST), r))
MAX_NUDGES = 15  # per run (30 min): bounded wake load on the fleet; green first, then red, then dirty
pending = []
for repo in REPOS:
    prs = gh(f"repos/TogetherWeOwn/{repo}/pulls?state=open&per_page=100")
    if not isinstance(prs, list): log(f"{repo} list failed {prs}"); continue
    for p in prs:
        if p["head"]["ref"].startswith("release-please") or p.get("draft"): continue
        d = {}
        for _ in range(3):
            d = gh(f"repos/TogetherWeOwn/{repo}/pulls/{p['number']}")
            if d.get("mergeable_state") not in (None, "unknown"): break
            time.sleep(3)
        ms, sha = d.get("mergeable_state"), d["head"]["sha"]
        kind = None
        failing = []
        if ms == "dirty": kind = "dirty"
        elif ms == "clean": kind = "green"
        elif ms in ("unstable", "blocked"):
            runs = gh(f"repos/TogetherWeOwn/{repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
            failing = sorted({c["name"] for c in runs if c.get("conclusion") in ("failure", "timed_out", "startup_failure")})
            if failing: kind = "red"
        if not kind: continue
        key = f"{repo}#{p['number']}@{sha[:10]}:{kind}"
        if key in state and now - state[key] < 6 * 3600: continue
        refs = sorted(set(re.findall(r"TOG-\d+", (d.get("body") or "") + " " + d["head"]["ref"] + " " + d["title"])))
        if not refs: continue
        inlist = ",".join(f"'{r}'" for r in refs)
        rows = sql(f"select i.id,i.identifier,i.status,coalesce(a.name,''),"
                   f" exists(select 1 from issue_relations ir join issues b on b.id=ir.issue_id where ir.related_issue_id=i.id and ir.type='blocks' and b.status not in ('done','cancelled'))"
                   f" from issues i left join agents a on a.id=i.assignee_agent_id where i.company_id='{C}' and i.identifier in ({inlist})"
                   f" and i.status not in ('done','cancelled') and i.assignee_agent_id is not null order by i.identifier")
        cards = []
        for row in rows.splitlines():
            iid, ident, status, agent, has_edge = row.split("|")
            cards.append({"iid": iid, "identifier": ident, "status": status, "agent": agent, "has_edge": has_edge,
                          **(card_facts(iid) or {"products": None, "workspace_repo": None, "project_repos": []})})
        if not cards: continue
        head_repo = (d["head"].get("repo") or {}).get("full_name")
        owner, why = choose_owner({"repo": f"TogetherWeOwn/{repo}", "number": p["number"], "branch": d["head"]["ref"], "head_repo": head_repo}, cards)
        if owner is None:
            log(f"skip {repo}#{p['number']}: {why}")
            continue
        iid, ident, status, agent, has_edge = owner["iid"], owner["identifier"], owner["status"], owner["agent"], owner["has_edge"]
        behind = gh(f"repos/TogetherWeOwn/{repo}/compare/main...{sha}").get("behind_by", "?") if kind == "dirty" else 0
        body = {"comment": MSG[kind].format(repo=repo, n=p["number"], ref=_plain(d["head"]["ref"]), sha=sha[:8], behind=behind, failing=", ".join(_plain(name) for name in failing[:8]) or "?")}
        if status == "blocked" and has_edge == "f": body["status"] = "todo"
        pending.append((REPOS.index(repo), {"green": 0, "red": 1, "dirty": 2}[kind], kind, repo, p["number"], ident, status, agent, iid, body, key))
sent = attempts = 0
for _, _, kind, repo, n, ident, status, agent, iid, body, key in sorted(pending, key=lambda x: (x[1], x[0])):
    if attempts >= MAX_NUDGES: break
    log(f"{'DRY ' if DRY else ''}{kind} {repo}#{n} -> {ident} ({status}, {agent}){' ->todo' if 'status' in body else ''}")
    if not DRY and issue_has_live_work(iid):
        log(f"  deferred: {ident} has a live run / scheduled retry (a comment would cancel it)"); continue
    attempts += 1
    if DRY:
        sent += 1; continue
    r = api("PATCH", f"issues/{iid}", body)
    if r.get("id"): state[key] = now; sent += 1
    else: log(f"  patch failed {str(r)[:150]}")
log(f"{'DRY ' if DRY else ''}summary: {len(pending)} PRs need a nudge, {'would send' if DRY else 'sent'} {sent}")
if not DRY: json.dump(state, open(STATE, "w"))
