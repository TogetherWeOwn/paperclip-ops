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
Existing invocation MUTATES: no flags (or --apply) runs PATCH and mutating child
steps. --dry-run performs no reads, PATCH, child steps or state/log writes.
Only confirmed HTTP 200 responses are logged as armed/reset; --json-summary
adds versioned outcomes. --apply with --dry-run refuses before any I/O.
"""
import json, os, subprocess, sys, time

import confirmed_outcomes as outcomes

APPLY = '--apply' in sys.argv
DRY_RUN = '--dry-run' in sys.argv
JSON_SUMMARY = '--json-summary' in sys.argv
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
        result = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At', '-F', '\t'],
                                input=sql, capture_output=True, text=True, timeout=PSQL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log(f'psql read timed out after {PSQL_TIMEOUT_SECONDS}s; could not read, no derived changes')
        return None
    if result.returncode:
        log('psql read failed; could not read, no derived changes')
        return None
    return result.stdout


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
        result = subprocess.run(['curl', '-sK', f'{ROOT}/.pc.curl', '-m', '30', f'http://127.0.0.1:3100/api/issues/{iid}'],
                                capture_output=True, text=True, timeout=CURL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log(f'issue {iid} read timed out after {CURL_TIMEOUT_SECONDS}s; could not read')
        return None
    if result.returncode:
        return None
    try:
        issue = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(issue, dict) or 'executionPolicy' not in issue:
        return None
    return issue if issue['executionPolicy'] is None or isinstance(issue['executionPolicy'], dict) else None


def patch(path, body):
    try:
        result = subprocess.run(['curl', '-sK', f'{ROOT}/.pc.curl', '-o', '/dev/null', '-w', '%{http_code}', '-X', 'PATCH',
                                 '-H', 'content-type: application/json', f'http://127.0.0.1:3100/api/{path}',
                                 '--data-binary', json.dumps(body)], capture_output=True, text=True,
                                timeout=CURL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return outcomes.from_curl()
    except OSError:
        return outcomes.Outcome('definite_failure', 'curl_unavailable', 'check_transport_before_next_tick')
    return outcomes.from_curl(result)


def run_step(step, *args):
    try:
        return subprocess.run(['python3', f'{ROOT}/ops/productivity/{step}.py', '--apply', *args],
                              capture_output=True, text=True, timeout=STEP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log(f'{step} timed out after {STEP_TIMEOUT_SECONDS}s; outcome unknown, not retried this tick')
        return None


def main(argv=None):
    args = outcomes.options('Flow guard MUTATES by default: PATCH plus mutating child steps; only confirmed writes complete.',
                            apply=APPLY, dry_run=DRY_RUN, json_summary=JSON_SUMMARY,
                            default_apply=True, argv=argv)
    summary = outcomes.Summary('flow_guard', args.json_summary)
    if args.refused:
        summary.error('contradictory_flags')
        summary.finish()
        return 2
    if args.dry_run:
        print('flow-guard: dry-run; would inspect continuation loops and transient agents, PATCH and run child steps; no I/O performed')
        return summary.finish()
    try:
        guard(summary)
    except (OSError, ValueError):
        summary.error('read_or_log_failed')
        print('flow-guard: FAILED: read or log unavailable; inspect state before retry', file=sys.stderr)
    return summary.finish()


def guard(summary):
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(f'{STATE}/PAUSE'):
        return
    loop_rows = psql(f"""
select i.id, i.identifier, count(*) from heartbeat_runs r join issues i on i.id::text=r.context_snapshot->>'issueId'
where i.company_id='{CID}' and i.status='in_progress' and r.created_at>now()-interval '30 minutes'
  and r.context_snapshot->>'wakeReason'='issue_continuation_needed'
  and coalesce(i.execution_state->'monitor'->>'status','')<>'scheduled'
group by 1,2 having count(*)>=6;""")
    if loop_rows is None:
        summary.error('loop_read_failed')
    loops = [] if loop_rows is None else [l.split('\t') for l in loop_rows.splitlines() if l]
    now = time.time()
    seen = set()
    for iid, ident, n in loops:
        if iid in seen:
            continue
        seen.add(iid)
        issue = read_issue(iid)
        if issue is None:
            log(f'continuation loop on {ident}: executionPolicy unreadable, card not changed')
            summary.error('issue_read_failed')
            continue
        body = {'executionPolicy': armed_policy(issue['executionPolicy'], now)}
        outcome = patch(f'issues/{iid}', body)
        summary.add('arm_monitor', f'{ident}:{iid}', outcome)
        if outcome.confirmed:
            log(f'continuation loop on {ident} ({n} wakes/30min): armed 2h monitor http={outcome.http_status}; '
                'rollback: PATCH executionPolicy back to its pre-write value')
        else:
            log(f'PATCH issues/{iid}: outcome {outcome.outcome} code={outcome.code}; '
                f'next_action={outcome.next_action}; not retried')
    # 3. Errored agents whose last failure is a known transient class never pick up work again on their own
    #    (memory errored-agents-silently-stall-the-board). Reset only those, to idle.
    agent_rows = psql(f"""
select a.id, a.name, coalesce((select r.error from heartbeat_runs r where r.agent_id=a.id and r.status='failed'
  order by r.created_at desc limit 1),'') from agents a where a.company_id='{CID}' and a.status='error';""")
    if agent_rows is None:
        summary.error('agent_read_failed')
    agents = [] if agent_rows is None else [l.split('\t') for l in agent_rows.splitlines() if l]
    seen = set()
    for aid, name, err in agents:
        if aid in seen:
            continue
        seen.add(aid)
        if any(t in err for t in TRANSIENT):
            outcome = patch(f'agents/{aid}', {'status': 'idle'})
            summary.add('reset_agent', aid, outcome)
            if outcome.confirmed:
                log(f'errored agent {name} reset to idle (last failure transient) http={outcome.http_status}')
            else:
                log(f'PATCH agents/{aid}: outcome {outcome.outcome} code={outcome.code}; '
                    f'next_action={outcome.next_action}; not retried')
    # 4. Idle waiters (owner GO 2026-10-05): cards polling with no-op wakes are parked on the card doing the work,
    #    or flagged once to the CEO when nobody is doing it. See idle_waiters.py.
    for step, args in [('idle_waiters', ()), ('hide_noop_comments', ()),
                       ('orphan_prs', ()), ('reconcile_replay_blocked', ('--wake', '5'))]:
        result = run_step(step, *args)
        if result is None:
            summary.error(f'{step}_timeout')
        elif result.returncode:
            log(f'{step} failed rc={result.returncode}; next_action=inspect_child_result')
            summary.error(f'{step}_failed')
        # Child stdout/stderr may contain response bodies or run errors. They
        # are not endpoint confirmations and must not leak into our summary.


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
