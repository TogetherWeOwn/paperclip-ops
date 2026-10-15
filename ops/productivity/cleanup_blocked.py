#!/usr/bin/env python3
"""Productivity Phase 1 one-time blocked cleanup (owner GO 2026-10-04). Every touched card is logged to
cleanup-<ts>.tsv with its before-state (status, blocker ids) for rollback.
  --phase nowake : legacy bare-blocked -> backlog; all-terminal cards: strip terminal edges (status unchanged here);
                   cards with cancelled + open blockers: drop only the cancelled edges; backfill NULL blocked_transition_at.
  --phase wake   : in-focus bare-blocked and edge-stripped all-terminal cards -> todo, at most N per run, only if load < 50.
Excludes owner roots case_8208302f/1269/277/392/456, Operator/owner/human/legal titles, pending interactions, descriptors, monitors."""
import json, os, subprocess, sys, time

ROOT = '/home/ubuntu/paperclip-enterprise-company'
C = '00000000-0000-4000-8000-000000000004'
EXCL = "('case_8208302f','case_4527a605','case_04f6ef4a','case_354a4d84','case_28fdd65f')"
FOCUS = "i.project_id::text ~ '^(5af2f7e7|1b2f7551|f2dc52a4|176a1793|b84aa5fe)'"
PSQL_TIMEOUT = 60
CURL_TIMEOUT = 60
SAFE = f"""i.company_id='{C}' and i.identifier not in {EXCL} and i.assignee_agent_id is not null
 and i.title !~* '(^operator|owner|human|legal)'
 and not exists(select 1 from issue_thread_interactions t where t.issue_id=i.id and t.status='pending')
 and not exists(select 1 from heartbeat_runs h where h.context_snapshot->>'issueId'=i.id::text and h.status in ('running','queued'))"""


def psql(sql):
    return subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'paperclip',
                           '-d', 'paperclip', '-At', '-F', '|'], input=sql, text=True, capture_output=True,
                          check=True, timeout=PSQL_TIMEOUT).stdout


def rows(sql):
    return [l.split('|') for l in psql(sql).splitlines() if l]


def patch(iid, body):
    try:
        r = subprocess.run(['curl', '-sS', '-K', f'{ROOT}/.pc.curl', '-X', 'PATCH',
                            f'http://127.0.0.1:3100/api/issues/{iid}',
                            '-H', 'content-type: application/json', '--data-binary', '@-'],
                           input=json.dumps(body), text=True, capture_output=True, timeout=CURL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'_timeout': f'PATCH timed out after {CURL_TIMEOUT}s (unmeasured)'}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {'_raw': r.stdout[:200]}


def blockers(iid):
    return rows(f"select bl.id, bl.status from issue_relations r join issues bl on bl.id=r.issue_id "
                f"where r.related_issue_id='{iid}' and r.type='blocks'")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    phase = args[args.index('--phase') + 1]
    limit = int(args[args.index('--limit') + 1]) if '--limit' in args else 10
    apply = '--apply' in args
    tsv = f"{ROOT}/ops/productivity/cleanup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{phase}.tsv"
    log = open(tsv, 'a') if apply else None

    applied = {'count': 0, 'update_attempted': False}

    def record(ident, iid, before_status, before_blockers, action):
        line = f"{ident}\t{iid}\t{before_status}\t{','.join(before_blockers)}\t{action}"
        print(line)
        if log: log.write(line + '\n'); log.flush()
        if apply and not action.startswith('FAILED'):
            applied['count'] += 1

    try:
        run_phase(phase, limit, apply, record, applied)
    except subprocess.TimeoutExpired:
        if apply and (applied['count'] > 0 or applied['update_attempted']):
            print(f'cleanup-blocked: FAILED: db query timed out after {PSQL_TIMEOUT}s '
                  f'(partially applied, see TSV {tsv})', file=sys.stderr)
        else:
            print(f'cleanup-blocked: FAILED: db query timed out after {PSQL_TIMEOUT}s '
                  f'(unmeasured; nothing applied)', file=sys.stderr)
        sys.exit(1)
    finally:
        if log: log.close()
        if apply:
            print(f'TSV: {tsv}')
    if not apply:
        print('TSV: (dry run)')


def run_phase(phase, limit, apply, record, tracker=None):
    if phase == 'nowake':
        # 1) legacy bare blocked -> backlog
        legacy = rows(f"""select i.id, i.identifier from issues i where {SAFE} and i.status='blocked'
          and not exists(select 1 from issue_relations r where r.related_issue_id=i.id and r.type='blocks')
          and i.unblock_descriptor is null and i.monitor_next_check_at is null and (i.project_id is null or not ({FOCUS}))""")
        for iid, ident in legacy:
            if apply:
                r = patch(iid, {'status': 'backlog'})
                if r.get('status') != 'backlog': record(ident, iid, 'blocked', [], f"FAILED backlog {str(r)[:80]}"); continue
            record(ident, iid, 'blocked', [], 'legacy-bare -> backlog')
        # 2) all-terminal: strip edges (status stays blocked; wake phase moves in-focus to todo, legacy to backlog now)
        allterm = rows(f"""select i.id, i.identifier, ({FOCUS})::text from issues i where {SAFE} and i.status='blocked'
          and exists(select 1 from issue_relations r where r.related_issue_id=i.id and r.type='blocks')
          and not exists(select 1 from issue_relations r join issues bl on bl.id=r.issue_id where r.related_issue_id=i.id
                         and r.type='blocks' and bl.status not in ('done','cancelled'))""")
        for iid, ident, infocus in allterm:
            bl = [b[0] for b in blockers(iid)]
            body = {'blockedByIssueIds': []}
            if infocus != 'true': body['status'] = 'backlog'
            if apply:
                r = patch(iid, body)
                if 'id' not in r: record(ident, iid, 'blocked', bl, f"FAILED strip {str(r)[:80]}"); continue
            record(ident, iid, 'blocked', bl, 'all-terminal: edges stripped' + ('' if infocus == 'true' else ' -> backlog'))
        # 3) cancelled edges alongside open blockers: drop only cancelled edges (card stays blocked)
        mixed = rows(f"""select i.id, i.identifier from issues i where {SAFE} and i.status='blocked'
          and exists(select 1 from issue_relations r join issues bl on bl.id=r.issue_id where r.related_issue_id=i.id and r.type='blocks' and bl.status='cancelled')
          and exists(select 1 from issue_relations r join issues bl on bl.id=r.issue_id where r.related_issue_id=i.id and r.type='blocks' and bl.status not in ('done','cancelled'))""")
        for iid, ident in mixed:
            bl = blockers(iid)
            keep = [b[0] for b in bl if b[1] != 'cancelled']
            if apply:
                r = patch(iid, {'blockedByIssueIds': keep})
                if 'id' not in r: record(ident, iid, 'blocked', [b[0] for b in bl], f"FAILED dropcancelled {str(r)[:80]}"); continue
            record(ident, iid, 'blocked', [b[0] for b in bl], f'dropped {len(bl) - len(keep)} cancelled edge(s)')
        # 4) backfill NULL blocked_transition_at (timestamp only)
        if apply:
            if tracker is not None:
                tracker['update_attempted'] = True
            n = psql(f"update issues set blocked_transition_at=updated_at where company_id='{C}' and status='blocked' "
                     f"and blocked_transition_at is null returning 1;").count('1')
            print(f'backfilled blocked_transition_at on {n} cards')

    elif phase == 'wake':
        load = float(open('/proc/loadavg').read().split()[0])
        if load >= 50:
            print(f'load {load} >= 50: deferring wakes'); sys.exit(0)
        cand = rows(f"""select i.id, i.identifier from issues i where {SAFE} and i.status='blocked' and {FOCUS}
          and not exists(select 1 from issue_relations r join issues bl on bl.id=r.issue_id where r.related_issue_id=i.id
                         and r.type='blocks' and bl.status not in ('done'))
          and i.unblock_descriptor is null and i.monitor_next_check_at is null
          and not exists(select 1 from heartbeat_runs h where h.context_snapshot->>'issueId'=i.id::text and h.status in ('running','queued'))
          order by i.priority='critical' desc, i.priority='high' desc, i.updated_at limit {limit}""")
        for iid, ident in cand:
            if apply:
                r = patch(iid, {'status': 'todo'})
                if r.get('status') != 'todo': record(ident, iid, 'blocked', [], f"FAILED todo {str(r)[:80]}"); continue
            record(ident, iid, 'blocked', [], 'in-focus unblocked -> todo (wake)')
        print(f'{len(cand)} woken (load {load})')


if __name__ == '__main__':
    main()
