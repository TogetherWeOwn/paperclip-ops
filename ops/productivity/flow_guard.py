#!/usr/bin/env python3
"""Flow guard (owner 2026-10-05: "fix the cards durably so it sticks"). Every 10 min, two compensations for Paperclip
behaviours that silently freeze or spin cards. Both are logged and reversible; retire once fixed upstream.

1. Continuation loop: an in_progress card whose monitor was consumed (triggered) and not re-armed gets
   `issue_continuation_needed` wakes every ~30s (case_40c3499e: 77 no-op runs/hour). If a card had >= 6 such wakes in
   30 min and has no scheduled monitor, arm a 2h external_service monitor that tells the agent to exit quietly.
   The card's whole executionPolicy is sent back with only its monitor replaced, so its stages and mode survive.
2. Replay-blocked: a cancelled run leaves a recovery row with replay='blocked' and every wake is skipped
   (execution_reconciliation_required). reconcile_replay_blocked.py releases these for focus/platform cards.
Stop: touch ~/.local/state/flow-guard/PAUSE.
"""
import json, os, subprocess, time
ROOT = '/home/ubuntu/paperclip-enterprise-company'
CID = '00000000-0000-4000-8000-000000000004'
STATE = os.path.expanduser('~/.local/state/flow-guard')
TRANSIENT = ('continuation_task_ownership_changed', 'Process lost', 'process_lost')


def timeout_seconds(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


PSQL_TIMEOUT_SECONDS = timeout_seconds('FLOW_GUARD_PSQL_TIMEOUT_SECONDS', 30)
CURL_TIMEOUT_SECONDS = timeout_seconds('FLOW_GUARD_CURL_TIMEOUT_SECONDS', 35)
STEP_TIMEOUT_SECONDS = timeout_seconds('FLOW_GUARD_STEP_TIMEOUT_SECONDS', 120)


def psql(sql):
    try:
        return subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At', '-F', '\t'],
                              input=sql, capture_output=True, text=True, timeout=PSQL_TIMEOUT_SECONDS).stdout
    except subprocess.TimeoutExpired:
        log(f'psql read timed out after {PSQL_TIMEOUT_SECONDS}s; could not read, no derived changes')
        return None


def log(msg):
    with open(f'{ROOT}/ops/worker-vps/operator-actions.log', 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} flow-guard: {msg}\n")


def armed_policy(current, now):
    policy = dict(current) if current else {'mode': 'normal', 'stages': [], 'commentRequired': False}
    policy['monitor'] = {'kind': 'external_service', 'serviceName': 'flow-guard',
                         'nextCheckAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 2 * 3600)),
                         'timeoutAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 26 * 3600)),
                         'maxAttempts': 12,
                         'notes': 'Flow guard: this card was re-woken every ~30s with nothing to do (consumed monitor, no live path). '
                                  'On a wake with nothing new: exit WITHOUT a comment and re-arm a monitor, block on the live leaf, or set todo/done.'}
    return policy


def read_issue(iid):
    try:
        out = subprocess.run(['curl', '-sK', f'{ROOT}/.pc.curl', '-m', '30', f'http://127.0.0.1:3100/api/issues/{iid}'],
                             capture_output=True, text=True, timeout=CURL_TIMEOUT_SECONDS).stdout
    except subprocess.TimeoutExpired:
        log(f'issue {iid} read timed out after {CURL_TIMEOUT_SECONDS}s; could not read')
        return None
    try:
        issue = json.loads(out)
    except ValueError:
        return None
    return issue if isinstance(issue, dict) and 'executionPolicy' in issue else None


def patch(path, body):
    try:
        return subprocess.run(['curl', '-sK', f'{ROOT}/.pc.curl', '-o', '/dev/null', '-w', '%{http_code}', '-X', 'PATCH',
                               '-H', 'content-type: application/json', f'http://127.0.0.1:3100/api/{path}',
                               '--data-binary', json.dumps(body)], capture_output=True, text=True,
                              timeout=CURL_TIMEOUT_SECONDS).stdout
    except subprocess.TimeoutExpired:
        log(f'PATCH {path} timed out after {CURL_TIMEOUT_SECONDS}s; outcome unknown, not retried')
        return None


def run_step(step, *args):
    try:
        return subprocess.run(['python3', f'{ROOT}/ops/productivity/{step}.py', '--apply', *args],
                              capture_output=True, text=True, timeout=STEP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log(f'{step} timed out after {STEP_TIMEOUT_SECONDS}s; outcome unknown, not retried this tick')
        return None


def main():
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(f'{STATE}/PAUSE'):
        return
    loop_rows = psql(f"""
select i.id, i.identifier, count(*) from heartbeat_runs r join issues i on i.id::text=r.context_snapshot->>'issueId'
where i.company_id='{CID}' and i.status='in_progress' and r.created_at>now()-interval '30 minutes'
  and r.context_snapshot->>'wakeReason'='issue_continuation_needed'
  and coalesce(i.execution_state->'monitor'->>'status','')<>'scheduled'
group by 1,2 having count(*)>=6;""")
    loops = [] if loop_rows is None else [l.split('\t') for l in loop_rows.splitlines() if l]
    now = time.time()
    for iid, ident, n in loops:
        issue = read_issue(iid)
        if issue is None:
            log(f'continuation loop on {ident}: executionPolicy unreadable, card not changed')
            continue
        body = {'executionPolicy': armed_policy(issue['executionPolicy'], now)}
        code = patch(f'issues/{iid}', body)
        if code is not None:
            log(f'continuation loop on {ident} ({n} wakes/30min): armed 2h monitor http={code}; '
                f'rollback: PATCH executionPolicy to {json.dumps(issue["executionPolicy"])}')
    # 3. Errored agents whose last failure is a known transient class never pick up work again on their own
    #    (memory errored-agents-silently-stall-the-board). Reset only those, to idle.
    agent_rows = psql(f"""
select a.id, a.name, coalesce((select r.error from heartbeat_runs r where r.agent_id=a.id and r.status='failed'
  order by r.created_at desc limit 1),'') from agents a where a.company_id='{CID}' and a.status='error';""")
    agents = [] if agent_rows is None else [l.split('\t') for l in agent_rows.splitlines() if l]
    for aid, name, err in agents:
        if any(t in err for t in TRANSIENT):
            code = patch(f'agents/{aid}', {'status': 'idle'})
            if code is not None:
                log(f'errored agent {name} reset to idle (last failure transient: {err[:60]}) http={code}')
    # 4. Idle waiters (owner GO 2026-10-05): cards polling with no-op wakes are parked on the card doing the work,
    #    or flagged once to the CEO when nobody is doing it. See idle_waiters.py.
    iw = run_step('idle_waiters')
    if iw is not None and iw.returncode:
        log(f'idle_waiters failed rc={iw.returncode}: {iw.stderr[-200:]}')
    # 6. Hide Paperclip-made no-op run notes (owner GO 2026-10-05) until fork fix case_4dbdd38c ships. Backed up, restorable.
    hn = run_step('hide_noop_comments')
    if hn is not None and hn.returncode:
        log(f'hide_noop_comments failed rc={hn.returncode}: {hn.stderr[-200:]}')
    # 5. Orphan PRs (owner 2026-10-05): every open PR in the watched repos gets a card that owns it until merge.
    op = run_step('orphan_prs')
    if op is not None and op.returncode:
        log(f'orphan_prs failed rc={op.returncode}: {op.stderr[-200:]}')
    replay = run_step('reconcile_replay_blocked', '--wake', '5')
    if replay is not None and not replay.stdout.startswith('0 '):
        print(replay.stdout.splitlines()[0] if replay.stdout else '')


if __name__ == '__main__':
    main()
