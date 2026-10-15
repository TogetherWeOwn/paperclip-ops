#!/usr/bin/env python3
"""Board unblock classifier + staged waves (owner order 2026-10-05).

Dry-run by default: classifies EVERY open card (not done/cancelled) with no
running/queued run into a reason class and writes ops/productivity/board-unblock-20261005.md.
  --apply --wave N : execute at most N wakes/unblocks (focus+platform only, staged).
Relation direction (verified 10-05 via API cross-check on case_7d42322e/case_2da0ba49):
  issue_relations.issue_id = BLOCKER, related_issue_id = WAITER (blocked card).
Every subprocess is bounded: psql keeps its 120 s bound; the API curl is bounded
by API_TIMEOUT below (a process backstop above curl's own -m 90). A timed-out
API call reads as a failed action (ok=False), never as a success.
"""
import calendar, json, os, subprocess, sys, time

ROOT = '/home/ubuntu/paperclip-enterprise-company'
C = '00000000-0000-4000-8000-000000000004'
CEO = '00000000-0000-4000-8000-000000000031'
REVIEWER = '00000000-0000-4000-8000-000000000049'  # Code Reviewer, cap 5
APPLY = '--apply' in sys.argv
WAVE_N = int(sys.argv[sys.argv.index('--wave') + 1]) if '--wave' in sys.argv else 10
REPORT = f'{ROOT}/ops/productivity/board-unblock-20261005.md'
OPLOG = f'{ROOT}/ops/worker-vps/operator-actions.log'

# A hung wakeup/PATCH POST used to stall the wave until someone killed it by hand.
# Process backstop above curl's own -m 90; a timeout reads as a failed action.
API_TIMEOUT = 100

FOCUS_PROJECTS = ("00000000-0000-4000-8000-000000000032", "00000000-0000-4000-8000-000000000033",
                  "00000000-0000-4000-8000-000000000035", "00000000-0000-4000-8000-000000000036",
                  "00000000-0000-4000-8000-000000000037")
NEXT_PROJECTS = ("00000000-0000-4000-8000-000000000032", "00000000-0000-4000-8000-000000000033")
PLATFORM_PROJECTS = ("00000000-0000-4000-8000-000000000035", "00000000-0000-4000-8000-000000000038",
                     "00000000-0000-4000-8000-000000000039")
PLATFORM_CARDS = ("case_91cf972c", "case_84e31ed9", "case_5467b6f7", "case_de994047", "case_24119081",
                  "case_9e61bb31", "case_be6ca7bf", "case_03778cec", "case_6dfea8e7", "case_acd5b66d", "case_6bb27f43")
PLATFORM_TOPIC_PROJECTS = ("00000000-0000-4000-8000-000000000005", "00000000-0000-4000-8000-000000000041",
                            "00000000-0000-4000-8000-000000000032", "00000000-0000-4000-8000-000000000033")
# Hands off: other operator sessions own these (triage EXECUTE-NOW + in-flight).
HANDS_OFF = set("""case_47578da1 case_359e77c4 case_5ba3cc38 case_c7cca907 case_59b05d23 case_12659166 case_7090df45 case_b414e7bb
case_f717dd26 case_a6813f43 case_e41236f9 case_5d914740 case_bef0a356 case_da53cb91 case_33dc5da1 case_377fe21b case_562da95a case_b105ebf7
case_c96fe867 case_1a8a3d08 case_9f4faef4 case_f0620488 case_614ea8dc case_b68d08be case_6305d81f case_922cf670 case_5e79fdfd case_93a40242
case_98eb46a5 case_751ce304 case_2d57f51c case_38e11de6 case_c5f9fe24 case_84749e0d case_08090334 case_9ef31bbf case_724d557c case_240974e1
case_6ceef68e case_fa4fcb64 case_b8566208""".split())
# Coordinator exclusion 10-05: case_6ceef68e/10197/13416 carry active workspace-recovery holds
# (worktree list overflow, native fix pending PR #691 / fork PR #38); case_24caff95's workspace
# (9b2c2a8d) is shared by case_fa4fcb64. Never resolve, prune or wake any of them.
EXCLUDED_WORKSPACE = '00000000-0000-4000-8000-000000000050'


def psql(sql):
    r = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip',
                        '-d', 'paperclip', '-At', '-F', '\t', '-v', 'ON_ERROR_STOP=1'],
                       input=sql, text=True, capture_output=True, timeout=120)
    if r.returncode:
        raise RuntimeError(r.stderr[:300])
    return [l.split('\t') for l in r.stdout.splitlines() if l]


RESP_FILE = '/tmp/board-unblock-resp.json'


def api(method, path, body=None):
    """(http_code, resp); a curl-level or process timeout reads as failed (never ok).

    curl's own -m 90 always fires before API_TIMEOUT, exiting non-zero with
    code '000' and leaving the previous response file untouched: that path
    returns an empty resp so the wave records ok=False. The file is removed
    before each call so a stale body can never count as the current answer."""
    cmd = ['curl', '-sK', f'{ROOT}/.pc.curl', '-m', '90', '-X', method,
           '-H', 'content-type: application/json',
           '-o', RESP_FILE, '-w', '%{http_code}',
           'http://127.0.0.1:3100/api/' + path]
    if body is not None:
        cmd[1:1] = ['--data-binary', json.dumps(body)]
    try:
        os.unlink(RESP_FILE)
    except OSError:
        pass
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=API_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 'timeout', {}
    code = p.stdout.strip()
    if p.returncode != 0 or code == '000':
        return code, {}
    try:
        return code, json.load(open(RESP_FILE))
    except Exception:
        return code, {}


def scope_sql(platform_only=False):
    allow = [l.strip() for l in open(os.path.expanduser('~/.local/state/paperclip-focus/allow.txt'))
             if l.strip() and not l.startswith('#')] if os.path.exists(
             os.path.expanduser('~/.local/state/paperclip-focus/allow.txt')) else []
    allow_list = ",".join("'" + a.replace("'", "") + "'" for a in allow) or "''"
    projects = ",".join(f"'{x}'" for x in FOCUS_PROJECTS)
    pprojects = ','.join(f"'{x}'" for x in PLATFORM_PROJECTS)
    tprojects = ','.join(f"'{x}'" for x in PLATFORM_TOPIC_PROJECTS)
    cards = ','.join(f"'{x}'" for x in PLATFORM_CARDS)
    platform = rf"""project_id in ({pprojects}) or identifier in ({cards})
      or ((project_id in ({tprojects}) or project_id is null)
          and identifier not in ('case_1a8b3477','case_d9fc684a')
          and title ~* '\mCI\M|autoscal|host.?hook|\mGARM\M|model.selection|model.router|\mrouter\M|runner|agent.admission')"""
    scope = platform if platform_only else f"""({platform}) or project_id in ({projects})
      or title ~* '(two-web-next|two-bot-next|two web next|two bot next)' or identifier in ({allow_list})
      or (origin_kind='routine_execution' and origin_id='00000000-0000-4000-8000-000000000042')
      or title ~* '^Improvement experiment:'"""
    return f"""with recursive roots as (select id from issues where company_id='{C}' and ({scope})),
down as (select id from roots union select i.id from issues i join down d on i.parent_id=d.id),
up as (select id from down union select i.parent_id from issues i join up u on i.id=u.id where i.parent_id is not null),
blk as (select id from up union select r.issue_id from issue_relations r join blk b on r.related_issue_id=b.id
  join issues x on x.id=r.issue_id where r.type='blocks' and x.status not in ('done','cancelled'))
select id::text from blk"""




def main():
    print('loading board snapshot...', flush=True)
    focus_ids = set(r[0] for r in psql(scope_sql(False)))
    platform_ids = set(r[0] for r in psql(scope_sql(True)))
    agents = {r[0]: (r[1], r[2]) for r in psql(
        f"select id::text, name, status from agents where company_id='{C}'")}
    cards = psql(f"""select id::text, identifier, title, status, coalesce(project_id::text,''),
      coalesce(assignee_agent_id::text,''), coalesce(assignee_user_id,''),
      (unblock_descriptor is not null)::text, (monitor_next_check_at is not null and monitor_next_check_at > now())::text,
      updated_at::text, coalesce(execution_policy,'null'), origin_kind, coalesce(origin_id,''),
      coalesce(execution_workspace_id::text,'')
      from issues where company_id='{C}' and status not in ('done','cancelled')""")
    blockers, = {},  # waiter_id -> [(blocker_id, ident, status)]
    waiters_of = {}  # blocker_id -> [waiter_id]
    for wid, bid, bident, bstatus in psql(
            f"""select r.related_issue_id::text, r.issue_id::text, b.identifier, b.status
            from issue_relations r join issues b on b.id=r.issue_id
            where r.company_id='{C}' and r.type='blocks'"""):
        blockers.setdefault(wid, []).append((bid, bident, bstatus))
        waiters_of.setdefault(bid, []).append(wid)
    live = set(r[0] for r in psql(
        "select distinct context_snapshot->>'issueId' from heartbeat_runs "
        "where status in ('running','queued') and context_snapshot->>'issueId' is not null"))
    retry = set(r[0] for r in psql(
        "select distinct context_snapshot->>'issueId' from heartbeat_runs "
        "where scheduled_retry_at > now() and context_snapshot->>'issueId' is not null"))
    lastrun = {r[0]: (r[1], r[2]) for r in psql(
        """select distinct on (context_snapshot->>'issueId') context_snapshot->>'issueId', status,
        created_at::text from heartbeat_runs where context_snapshot->>'issueId' is not null
        order by 1, created_at desc""")}
    fail24 = {r[0]: int(r[1]) for r in psql(
        """select context_snapshot->>'issueId', count(*) from heartbeat_runs
        where created_at > now() - interval '24 hours' and status='failed'
        and context_snapshot->>'issueId' is not null group by 1""")}
    lastreview = {}
    for r in psql(f"""select i.id::text, max(h.created_at)::text from issues i left join heartbeat_runs h
        on h.context_snapshot->>'issueId'=i.id::text where i.company_id='{C}' and i.status='in_review'
        and i.status not in ('done','cancelled') group by 1"""):
        lastreview[r[0]] = r[1]
    pend_inter = {}
    for r in psql("select issue_id::text, count(*) from issue_thread_interactions "
                  "where status='pending' group by 1"):
        pend_inter[r[0]] = int(r[1])
    replay_blocked = set(r[0] for r in psql(
        "select distinct source_issue_id::text from issue_recovery_actions "
        "where evidence->'automaticRecovery'->>'replay'='blocked'"))
    active_rec = set(r[0] for r in psql(
        "select distinct source_issue_id::text from issue_recovery_actions where status in ('active','escalated')"))
    dead_lease = set(r[0] for r in psql(
        """select distinct l.issue_id::text from environment_leases l left join heartbeat_runs h
        on h.id=l.heartbeat_run_id where l.status='active' and l.issue_id is not null
        and (h.id is null or h.status in ('cancelled','failed','succeeded'))"""))
    review_part = {}  # issue_id -> agent_id of review-stage participant
    for r in psql(f"""select id::text, execution_policy from issues where company_id='{C}'
        and status='in_review' and execution_policy is not null"""):
        try:
            for s in (json.loads(r[1]) or {}).get('stages', []):
                if s.get('type') == 'review':
                    for p in s.get('participants', []):
                        if p.get('agentId'):
                            review_part[r[0]] = p['agentId']
        except Exception:
            pass

    # Next production path: Next-project cards + 1-hop open blockers of Next cards.
    next_ids = set(r[0] for r in psql(
        f"select id::text from issues where company_id='{C}' and status not in ('done','cancelled')"
        f" and project_id::text in ('{NEXT_PROJECTS[0]}','{NEXT_PROJECTS[1]}')"))
    prod_blockers = set()
    for r in psql(f"""select distinct r.issue_id::text from issue_relations r join issues w
        on w.id=r.related_issue_id where r.company_id='{C}' and r.type='blocks'
        and w.project_id::text in ('{NEXT_PROJECTS[0]}','{NEXT_PROJECTS[1]}')
        and w.status not in ('done','cancelled')"""):
        prod_blockers.add(r[0])

    CLASSES = ['operator-card', 'user-assigned', 'pending-interaction', 'recovery-hold-excluded', 'recovery-blocked',
               'lease-blocked', 'run-churn', 'errored-paused-assignee', 'dead-edge',
               'dead-edge-partial', 'blocked-on-blocked', 'real-live-blocker', 'edgeless-blocked',
               'descriptor-hold', 'monitor-hold', 'retry-pending', 'waiting-review-no-run',
               'in-review-active', 'in_progress-idle-no-monitor', 'in-progress-active',
               'todo-assigned-not-dispatched', 'unassigned', 'backlog', 'other']
    rows_out, counts, actions = [], {}, []


    def tier(cid, project, ident):
        if cid in next_ids or cid in prod_blockers:
            return 'next-prod'
        if cid in focus_ids:
            return 'platform' if cid in platform_ids and project not in NEXT_PROJECTS else 'focus'
        return 'parked'


    def is_operator(title):
        return title.lower().startswith('operator:')


    def dispatchable(aid):
        return bool(aid) and agents.get(aid, ('?', 'gone'))[1] in ('idle', 'running')


    for (cid, ident, title, status, project, aid, auid, has_desc, has_mon,
         updated, epolicy, okind, ooid, wsid) in cards:
        if wsid == EXCLUDED_WORKSPACE and ident != 'case_24caff95':
            cls, act, note = 'recovery-hold-excluded', None, 'shares case_24caff95 workspace; excluded 10-05'
            key = (cls, tier(cid, project, ident))
            counts[key] = counts.get(key, 0) + 1
            rows_out.append({'id': cid, 'identifier': ident, 'title': title[:100], 'status': status,
                             'class': cls, 'tier': tier(cid, project, ident),
                             'assignee': agents.get(aid, ('-',))[0] if aid else (auid[:8] if auid else '-'),
                             'action': '', 'note': note})
            continue
        bl = blockers.get(cid, [])
        live_bl = [b for b in bl if b[2] not in ('done', 'cancelled')]
        dead_bl = [b for b in bl if b[2] in ('done', 'cancelled')]
        t = tier(cid, project, ident)
        cls, act, note = None, None, ''
        if is_operator(title):
            cls = 'operator-card'
            note = 'HANDS-OFF' if ident in HANDS_OFF else 'needs host execution'
        elif auid:
            cls = 'user-assigned'; note = f'user {auid[:8]}; invisible to agent dispatch'
        elif pend_inter.get(cid):
            cls = 'pending-interaction'; note = f"{pend_inter[cid]} pending ask(s)"
        elif cid in replay_blocked or cid in active_rec:
            cls = 'recovery-blocked'; note = 'replay=blocked' if cid in replay_blocked else 'active recovery'
        elif cid in dead_lease:
            cls = 'lease-blocked'; note = 'active lease, run terminal'
        elif fail24.get(cid, 0) >= 5:
            cls = 'run-churn'; note = f"{fail24[cid]} failed runs/24h"
        elif cid in retry:
            cls = 'retry-pending'; note = 'scheduled retry in future'
        elif aid and agents.get(aid, ('?', 'gone'))[1] in ('paused', 'error'):
            cls = 'errored-paused-assignee'; note = f"{agents[aid][0]}={agents[aid][1]}"
        elif status == 'blocked':
            if not bl:
                if has_desc == 'true':
                    cls = 'descriptor-hold'
                elif has_mon == 'true':
                    cls = 'monitor-hold'
                else:
                    cls = 'edgeless-blocked'
                    if dispatchable(aid) and ident not in HANDS_OFF and t != 'parked':
                        act = ('todo', f'{ident}: edgeless blocked -> todo')
            elif not live_bl:
                cls = 'dead-edge'
                if ident not in HANDS_OFF and t != 'parked':
                    act = ('strip-todo', f'{ident}: strip {len(dead_bl)} terminal edge(s) -> todo')
            elif dead_bl:
                cls = 'dead-edge-partial'
                if ident not in HANDS_OFF and t != 'parked':
                    act = ('strip', f'{ident}: drop {len(dead_bl)} terminal edge(s), keep {len(live_bl)}')
            elif any(b[0] in blockers and any(g[2] not in ('done', 'cancelled')
                                             for g in blockers.get(b[0], []))
                     for b in live_bl):
                cls = 'blocked-on-blocked'
                note = 'chain: ' + ','.join(b[1] for b in live_bl if b[1])
            else:
                # chain check via waiter map: blocker itself has open blockers?
                chained = [b for b in live_bl if blockers.get(b[0]) and
                           any(g[2] not in ('done', 'cancelled') for g in blockers[b[0]])]
                if chained:
                    cls = 'blocked-on-blocked'; note = 'chain via ' + ','.join(b[1] for b in chained)
                else:
                    cls = 'real-live-blocker'; note = 'waits ' + ','.join(b[1] for b in live_bl)
        elif status == 'in_review':
            if has_mon == 'true':
                cls = 'in-review-active'; note = 'future monitor check owns it'
                key = (cls, t); counts[key] = counts.get(key, 0) + 1
                rows_out.append({'id': cid, 'identifier': ident, 'title': title[:100], 'status': status,
                                 'class': cls, 'tier': t, 'assignee': agents.get(aid, ('-',))[0] if aid else (auid[:8] if auid else '-'),
                                 'action': '', 'note': note})
                continue
            lr = lastreview.get(cid)
            stale = (not lr) or ((time.time() - calendar.timegm(time.strptime(lr[:19], '%Y-%m-%d %H:%M:%S'))) > 4 * 3600)
            if stale and ident not in HANDS_OFF:
                cls = 'waiting-review-no-run'
                who = review_part.get(cid, REVIEWER)
                if dispatchable(who) or who == REVIEWER:
                    act = ('review-wake', f'{ident}: wake reviewer {agents.get(who, ("?",))[0]}')
            else:
                cls = 'in-review-active'
        elif status == 'in_progress':
            if not aid and not auid:
                cls = 'unassigned'
            elif aid and dispatchable(aid) and cid not in lastrun and has_mon == 'false' \
                    and ident not in HANDS_OFF and t != 'parked':
                cls = 'in_progress-idle-no-monitor'
                act = ('wake', f'{ident}: wake assignee {agents[aid][0]}')
            else:
                cls = 'in-progress-active'
                if not aid:
                    cls = 'unassigned'
        elif status == 'todo':
            if not aid and not auid:
                cls = 'unassigned'
            elif aid and dispatchable(aid) and ident not in HANDS_OFF and t != 'parked':
                cls = 'todo-assigned-not-dispatched'
                act = ('wake', f'{ident}: wake assignee {agents[aid][0]}')
            else:
                cls = 'other' if aid else 'unassigned'
        elif status == 'backlog':
            cls = 'backlog'
        else:
            cls = 'other'
        if cls is None:
            cls = 'other'
        key = (cls, t)
        counts[key] = counts.get(key, 0) + 1
        if act and cid not in live:
            actions.append((t, cls, cid, ident, title[:90], aid, act))
        rows_out.append({'id': cid, 'identifier': ident, 'title': title[:100], 'status': status,
                         'class': cls, 'tier': t, 'assignee': agents.get(aid, ('-',))[0] if aid else (auid[:8] if auid else '-'),
                         'action': act[0] if act else '', 'note': note})

    print(f'{len(cards)} open cards classified, {len(live)} with live runs skipped from actions, '
          f'{len(actions)} candidate actions', flush=True)

    # order: next-prod, focus, platform; reviews/prod-path first within tier
    TIER_RANK = {'next-prod': 0, 'focus': 1, 'platform': 2}
    CLS_RANK = {'waiting-review-no-run': 0, 'dead-edge': 1, 'dead-edge-partial': 1, 'edgeless-blocked': 2,
                'todo-assigned-not-dispatched': 3, 'in_progress-idle-no-monitor': 3}
    actions.sort(key=lambda a: (TIER_RANK.get(a[0], 3), CLS_RANK.get(a[1], 9), a[3]))
    json.dump(rows_out, open('/tmp/board-unblock-rows.json', 'w'), indent=1)

    # ---- report ----
    el = [a for a in actions if a[0] != 'parked']
    lines = [f'# Board unblock 2026-10-05 (updated {time.strftime("%H:%MZ", time.gmtime())})',
             f'\nOpen: {len(cards)} · live runs on {len(live)} cards · retry-pending on {len(retry & set(r["id"] for r in rows_out))} · candidate actions (focus+platform): {len(el)}',
             '\n## Class counts (focus+platform eligible / parked)',
             '\n| class | eligible | parked | total |',
             '|---|---|---|---|']
    for c in CLASSES:
        e = sum(v for (k, t), v in counts.items() if k == c and t != 'parked')
        p = sum(v for (k, t), v in counts.items() if k == c and t == 'parked')
        if e + p:
            lines.append(f'| {c} | {e} | {p} | {e+p} |')
    lines += ['\n## Eligible action queue (top 30)',
              '\n| # | card | class | tier | action |',
              '|---|---|---|---|---|']
    for i, a in enumerate(el[:30]):
        lines.append(f'| {i+1} | {a[3]} {a[4]} | {a[1]} | {a[0]} | {a[6][0]} |')
    lines.append(f"\nFull rows: /tmp/board-unblock-rows.json ({len(rows_out)} cards). "
                 "Relation direction: issue_relations.issue_id=BLOCKER, related_issue_id=WAITER.")
    open(REPORT, 'w').write('\n'.join(lines) + '\n')
    print(f'report: {REPORT}', flush=True)

    # ---- apply one wave ----
    if APPLY:
        import re
        load5 = float(open('/proc/loadavg').read().split()[1])
        running = int(psql("select count(*) from heartbeat_runs where status='running'")[0][0])
        memfree = int([l for l in open('/proc/meminfo') if l.startswith('MemAvailable')][0].split()[1]) // 1024 // 1024
        print(f'gate: load5={load5} running={running} memAvail={memfree}G', flush=True)
        if not (load5 < 40 and running < 25 and memfree > 16):
            print('GATE HOLD: conditions not met, no wave applied');
            sys.exit(0)
        picked, reviewer_wakes = [], 0
        # A card the assignee already looked at and left as-is (no-op run) or the server refused to wake must not
        # be re-woken every wave: that burns a run per wave and pushes real candidates out of the batch.
        COOLDOWN_H = 6
        cool_path = os.path.expanduser('~/.local/state/board-unblock/acted.json')
        os.makedirs(os.path.dirname(cool_path), exist_ok=True)
        acted = json.load(open(cool_path)) if os.path.exists(cool_path) else {}
        now = time.time()
        acted = {k: v for k, v in acted.items() if now - v < COOLDOWN_H * 3600}
        # A psql timeout mid-wave must not silently drop the actions already
        # sent: stop the wave, then still persist acted.json and the
        # OPLOG/REPORT lines for everything in picked so far.
        wave_error = None
        for a in el:
            if len(picked) >= WAVE_N:
                break
            t, cls, cid, ident, title, aid, act = a
            if act[0] in ('wake', 'review-wake') and ident in acted:
                continue
            kind = act[0]
            try:
                if kind == 'review-wake':
                    who = review_part.get(cid, REVIEWER)
                    if who == REVIEWER:
                        if reviewer_wakes >= 2:
                            continue
                        rr = int(psql(f"select count(*) from heartbeat_runs where agent_id='{REVIEWER}' and status='running'")[0][0])
                        if rr >= 4:
                            continue
                    target, body = who, {"source": "on_demand", "reason": f"board-unblock-20261005: in_review with no reviewer run>4h ({ident})",
                                         "payload": {"issueId": cid}}
                    # failed last run -> retry_failed_run form
                    lr = lastrun.get(cid)
                    if lr and lr[0] == 'failed':
                        fr = psql(f"select id::text, agent_id::text from heartbeat_runs where context_snapshot->>'issueId'='{cid}' "
                                  "and status='failed' order by created_at desc limit 1")[0]
                        target, body = fr[1], {"source": "on_demand", "reason": "retry_failed_run",
                                               "triggerDetail": "manual", "failedRunId": fr[0]}
                    code, resp = api('POST', f'agents/{target}/wakeup', body)
                    ok = resp.get('status') in ('queued', 'started', 'scheduled') or code == '200'
                    picked.append((ident, kind, code, str(resp)[:100], ok))
                    if who == REVIEWER:
                        reviewer_wakes += 1
                elif kind == 'wake':
                    code, resp = api('POST', f'agents/{aid}/wakeup',
                                     {"source": "on_demand", "reason": f"board-unblock-20261005: {cls} ({ident})",
                                      "payload": {"issueId": cid}})
                    ok = resp.get('status') in ('queued', 'started', 'scheduled') or code == '200'
                    picked.append((ident, kind, code, str(resp)[:100], ok))
                elif kind in ('todo', 'strip', 'strip-todo'):
                    bl = blockers.get(cid, [])
                    keep = [b[0] for b in bl if b[2] not in ('done', 'cancelled')]
                    body = {}
                    if kind in ('strip', 'strip-todo'):
                        body['blockedByIssueIds'] = keep
                    if kind in ('todo', 'strip-todo'):
                        body['status'] = 'todo'
                    code, resp = api('PATCH', f'issues/{cid}', body)
                    ok = resp.get('id') == cid
                    picked.append((ident, kind, code, str(resp)[:100], ok))
            except subprocess.TimeoutExpired as e:
                wave_error = e
                break
            time.sleep(2)
        for p in picked:
            if p[1] in ('wake', 'review-wake'):
                acted[p[0]] = now
        json.dump(acted, open(cool_path, 'w'))
        ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        okc = sum(1 for p in picked if p[4])
        with open(OPLOG, 'a') as f:
            f.write(f"{ts} operator: board-unblock wave ({len(picked)} actions, {okc} ok): "
                    + ', '.join(f'{p[0]}:{p[1]}:{p[2]}' for p in picked) + '\n')
        with open(REPORT, 'a') as f:
            f.write(f"\n## Wave {ts}: {len(picked)} actions, {okc} ok "
                    f"(gate load5={load5} running={running} mem={memfree}G)\n")
            for p in picked:
                f.write(f"- {p[0]} {p[1]} http={p[2]} ok={p[4]} {p[3][:80]}\n")
        msg = f'wave done: {len(picked)} actions, {okc} ok'
        if wave_error is not None:
            msg += f' (stopped early: {wave_error}; applied actions above are recorded)'
        print(msg, flush=True)


if __name__ == '__main__':
    try:
        main()
    except subprocess.TimeoutExpired as e:
        print(f'board-unblock: FAILED: {e} (unmeasured; no report, no wave applied)', file=sys.stderr)
        raise SystemExit(1)
