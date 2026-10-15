#!/usr/bin/env python3
"""Deterministic backlog promoter (owner 2026-10-08 ~03:00Z: "get the work going again!!! I expect to see 50+ parallel
runs"). The LLM planners re-qualified 452 cards and promoted ~3 (strict READY); the idle waker then had 1 runnable card
and the fleet sat at 5-10 runs. This keeps at least TARGET_RUNNABLE unblocked todo cards assigned to active agents by
promoting backlog cards with NO open blocker edge in the admitted projects, highest priority and TWO first.

Rules: never touches cards with an open `blocks` edge, never changes production/security gates, skips Laravel-era Kofra
cards (owner: Kofra ships serverless), keeps an active assignee or assigns by project. Promotion = PATCH status todo (the
idle waker then wakes the assignee). At most MAX_PER_RUN per run. Dry-run by default; --apply acts. PAUSE file stops it.
Rollback: cards promoted are listed in operator-actions.log; PATCH them back to backlog.
Every subprocess is bounded (*_TIMEOUT_S below, env-overridable). Fail closed: a PR-state read that times out reads as
not open (the card stays in backlog); a psql read that times out stops the run before any further promotion.
"""
import calendar
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/home/ubuntu/paperclip-enterprise-company')
CID = '00000000-0000-4000-8000-000000000004'
API = 'http://127.0.0.1:3100/api'
LOG = ROOT / 'ops/worker-vps/operator-actions.log'
PAUSE = ROOT / 'ops/productivity/backlog-promoter.PAUSE'
APPLY = '--apply' in sys.argv
BOUNCE_H = 24  # a card its owner returned to backlog after our promotion waits for COO admission, not another push
TARGET_RUNNABLE = 40
MAX_PER_RUN = 25
PROJECTS = [  # (project, fallback assignee) in priority order
    ('TWO Bot Next', 'Founding Engineer'),
    ('TWO Web Next', 'Web Engineer'),
    ('Kofra', 'Web Engineer'),
    ('NNTune', 'Automation Engineer'),
    ('Routeware Shadow API', 'DevOps & Reliability Engineer'),
    # 2026-10-08 10:35Z: the five product projects ran out of unblocked backlog (runnable todo=3, fleet 23 running).
    # Lower-ranked fill: approved platform work. Kagi, legacy TWO Web and TWO Community & Growth (legacy two-bot repo,
    # replaced by two-bot-next) stay out (owner 10-06: free-for-all only after TWO prod; Next repos are a fresh start).
    ('Ops Tooling', 'Automation Engineer'),
    ('Paperclip Fork', 'Upstream & Public GitHub Steward'),
]
ANY_PROJECT_SKIP = re.compile(r'^Operator:|parked until|probe target|do not mutate', re.I)  # stale host-action cards; cards their owner parked
FILL_PROJECTS = {'Ops Tooling', 'Paperclip Fork'}
SKIP_TITLE = re.compile(r'laravel|coolify|\bphp\b|cashier|blade|livewire|mysql|mail::|filament', re.I)
PR_REF = re.compile(r'PR #(\d+)')
REPO_OF = {'TWO Bot Next': 'two-bot-next', 'TWO Web Next': 'two-web-next', 'Kofra': 'kofra', 'NNTune': 'nntune',
           'Routeware Shadow API': 'routeware-shadow-api'}
_pr_cache = {}


def _seconds(name, default):
    v = float(os.environ.get(name, default))
    if not 0 < v <= 3600:
        raise SystemExit(f'{name} must be seconds in (0, 3600], got {v}')
    return v


# A hung gh or psql call used to wedge the daily sweep until someone killed it by hand.
PSQL_TIMEOUT_S = _seconds('BACKLOG_PROMOTER_PSQL_TIMEOUT_S', 60)
GH_TIMEOUT_S = _seconds('BACKLOG_PROMOTER_GH_TIMEOUT_S', 60)  # gh_token.sh and gh pr view
CURL_TIMEOUT_S = _seconds('BACKLOG_PROMOTER_CURL_TIMEOUT_S', 30)  # curl --max-time; the process gets 10 s more to exit


def gh_out(cmd, env=None):
    """stdout of a bounded gh-side command; '' on timeout, which pr_open reads as not open."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=GH_TIMEOUT_S).stdout
    except subprocess.TimeoutExpired:
        print(f'{Path(cmd[0]).name} {cmd[1]} timed out after {GH_TIMEOUT_S:g}s')
        return ''


def pr_open(repo, n):
    k = (repo, n)
    if k not in _pr_cache:
        tok = gh_out([str(ROOT / 'gh_token.sh'), 'token']).strip()
        out = gh_out(['gh', 'pr', 'view', n, '-R', f'TogetherWeOwn/{repo}', '--json', 'state', '--jq', '.state'],
                     {'GH_TOKEN': tok, 'PATH': '/usr/bin:/bin'}).strip() if tok else ''  # no token: state unknown
        _pr_cache[k] = out == 'OPEN'
    return _pr_cache[k]
PRIO = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}


def psql(sql):
    out = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip',
                          '-AtF', '\t', '-c', sql], capture_output=True, text=True, check=True,
                         timeout=PSQL_TIMEOUT_S).stdout
    return [l.split('\t') for l in out.splitlines() if l]


def api(method, path, body):
    """HTTP status; 'timeout' when curl did not exit in time (the write may or may not have landed)."""
    try:
        r = subprocess.run(['curl', '-sK', str(ROOT / '.pc.curl'), '-X', method, f'{API}/{path}', '-H',
                            'content-type: application/json', '-d', json.dumps(body), '-o', '/dev/null',
                            '--max-time', f'{CURL_TIMEOUT_S:g}', '-w', '%{http_code}'],
                           capture_output=True, text=True, timeout=CURL_TIMEOUT_S + 10)
    except subprocess.TimeoutExpired:
        return 'timeout'
    return r.stdout.strip()


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from host_ok import host_ok
    ok, why = host_ok()
    if not ok:
        print(f'host under pressure ({why}): no new wakes this run')
        return
    if PAUSE.exists():
        return
    unblocked = """not exists (select 1 from issue_relations ir join issues b on b.id=ir.issue_id
        where ir.related_issue_id=i.id and ir.type='blocks' and b.status not in ('done','cancelled'))"""
    runnable = int(psql(f"""select count(*) from issues i join agents a on a.id=i.assignee_agent_id
        where i.company_id='{CID}' and i.status='todo' and a.status not in ('paused','terminated','error') and {unblocked}""")[0][0])
    need = min(MAX_PER_RUN, max(0, TARGET_RUNNABLE - runnable))
    print(f'runnable todo={runnable} target={TARGET_RUNNABLE} -> promote up to {need}')
    if not need:
        return
    bounced = set()
    for line in LOG.read_text().splitlines()[-4000:]:
        if 'backlog-promoter:' in line and 'backlog->todo' in line:
            try:
                ts = calendar.timegm(time.strptime(line[:20], '%Y-%m-%dT%H:%M:%SZ'))
            except ValueError:
                continue
            if time.time() - ts < BOUNCE_H * 3600:
                bounced.add(line.split('backlog-promoter: ', 1)[1].split(' ', 1)[0])
    agents = {r[0]: (r[1], r[2]) for r in psql(f"select name, id, status from agents where company_id='{CID}' and status<>'terminated'")}
    done = 0
    for proj, fallback in PROJECTS:
        if done >= need:
            break
        rows = psql(f"""select i.id, i.identifier, i.title, coalesce(i.priority,'medium'), coalesce(a.name,''), coalesce(a.status,'')
            from issues i join projects p on p.id=i.project_id left join agents a on a.id=i.assignee_agent_id
            where i.company_id='{CID}' and p.name='{proj}' and i.status='backlog' and {unblocked}
            order by i.updated_at desc""")
        rows.sort(key=lambda r: PRIO.get(r[3], 2))
        for iid, ident, title, prio, aname, astatus in rows:
            if done >= need:
                break
            if ident in bounced:
                continue  # promoted within BOUNCE_H and its owner put it back: needs COO admission
            if proj == 'Kofra' and SKIP_TITLE.search(title):
                continue
            if ANY_PROJECT_SKIP.search(title) or (proj in FILL_PROJECTS and SKIP_TITLE.search(title)):
                continue
            m = PR_REF.search(title)
            if m and proj in REPO_OF and not pr_open(REPO_OF[proj], m.group(1)):
                continue  # card is about a PR that is already merged or closed
            body = {'status': 'todo'}
            who = aname if aname and astatus not in ('paused', 'terminated') else fallback
            if who != aname:
                body['assigneeAgentId'] = agents[who][0]
            if not APPLY:
                print(f'would promote {ident} [{proj}/{prio}] -> {who}: {title[:70]}')
                done += 1
                continue
            code = api('PATCH', f'issues/{iid}', body)
            done += 1
            line = (f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} backlog-promoter: {ident} backlog->todo "
                    f"[{proj}/{prio}] assignee {who} HTTP {code}")
            print(line)
            with LOG.open('a') as f:
                f.write(line + '\n')
    print(f'promoted {done}')


if __name__ == '__main__':
    main()
