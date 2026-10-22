#!/usr/bin/env python3
"""Park legacy projects natively (owner GO 2026-10-05): move their open cards to `backlog`.

Paperclip never dispatches, recovers or assignment-wakes a backlog card, so parked work stops costing runs
instead of being woken and then cancelled by focus_gate.py. Only cards the focus gate would cancel are moved
(same allow logic), never a card with a live/queued run. The original status of every moved card is appended
to STATE so `--unpark <project-name>` restores it. Dry-run by default; `--apply [--batch N]`.
"""
import json, os, subprocess, sys, time

ROOT = '/home/ubuntu/paperclip-enterprise-company'
CID = '00000000-0000-4000-8000-000000000004'
STATE = f'{ROOT}/ops/productivity/parked-cards.jsonl'
LOG = f'{ROOT}/ops/worker-vps/operator-actions.log'
PARKED = ('TWO Community & Growth', 'Community Platform', 'Fleet Intelligence & Evaluation', 'Here.Now Clone', 'Host AI Bridge (MCP)', 'Kofra',
          'NNTune', 'Onboarding', 'Prism Gateway', 'Routeware Shadow API', 'TWO Web', 'Wayselect',
          'cpa-plugin-zai-coding-plan')
APPLY = '--apply' in sys.argv
BATCH = int(sys.argv[sys.argv.index('--batch') + 1]) if '--batch' in sys.argv else 50

src = open(f'{ROOT}/ops/paperclip/focus_gate.py').read()
gate = {}
exec(src[:src.index('def tick(')], gate)  # constants + db/api/allowed_sql only; no side effects
db = gate['db']


def patch(issue_id, status):
    out = subprocess.run(['curl', '-sK', f'{ROOT}/.pc.curl', '-m', '60', '-o', '/dev/null', '-w', '%{http_code}',
                          '-X', 'PATCH', '-H', 'content-type: application/json',
                          f'http://127.0.0.1:3100/api/issues/{issue_id}', '--data-binary',
                          json.dumps({'status': status})], capture_output=True, text=True)
    return out.stdout


def log(msg):
    with open(LOG, 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} operator: park_projects {msg}\n")


if '--unpark' in sys.argv:
    name = sys.argv[sys.argv.index('--unpark') + 1]
    rows = [json.loads(l) for l in open(STATE)] if os.path.exists(STATE) else []
    todo = [r for r in rows if r['project'] == name]
    done = 0
    for r in todo:
        cur = db(f"select status from issues where id='{r['id']}'")
        if cur and cur[0][0] == 'backlog' and APPLY and patch(r['id'], r['from']).startswith('2'):
            done += 1
    print(f"{'restored' if APPLY else 'would restore'} {done if APPLY else len(todo)} cards in {name}")
    if APPLY:
        log(f'unpark {name}: restored {done} cards to their pre-park status')
    sys.exit(0)

names = ','.join("'" + n.replace("'", "''") + "'" for n in PARKED)
rows = db(f"""{gate['allowed_sql']()}
select i.id, i.identifier, i.status, p.name from issues i join projects p on p.id=i.project_id
where i.company_id='{CID}' and p.name in ({names})
  and i.status in ('todo','in_progress','blocked','in_review')
  and i.id not in (select id from blk)
  and not exists (select 1 from heartbeat_runs r where r.context_snapshot->>'issueId'=i.id::text
                  and r.status in ('running','queued'))
order by p.name, i.identifier;""")
by = {}
for _, _, st, pn in rows:
    by.setdefault(pn, {}).setdefault(st, 0)
    by[pn][st] += 1
for pn, sts in sorted(by.items()):
    print(f'{pn}: {sts}')
print(f'{len(rows)} cards eligible to park')
if not APPLY:
    sys.exit(0)
moved = 0
for iid, ident, st, pn in rows[:BATCH]:
    if patch(iid, 'backlog').startswith('2'):
        with open(STATE, 'a') as f:
            f.write(json.dumps({'id': iid, 'identifier': ident, 'from': st, 'project': pn,
                                'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}) + '\n')
        moved += 1
print(f'moved {moved} to backlog')
log(f'moved {moved}/{min(BATCH, len(rows))} parked-project cards to backlog (remaining eligible {len(rows) - moved}); '
    f'rollback: park_projects.py --unpark <project> --apply')
