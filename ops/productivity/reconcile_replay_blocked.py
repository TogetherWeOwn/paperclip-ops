#!/usr/bin/env python3
"""Release cards frozen by 'execution_reconciliation_required' (owner 2026-10-05: fix idle cards durably).

When a run is cancelled mid-execution (focus_gate cancels, restarts, drains), Paperclip leaves a RESOLVED
active_run_watchdog recovery row with evidence.automaticRecovery.replay='blocked'. Every later wake on that card is
skipped, so the card is frozen forever. For cards in focus/platform projects (not parked, not workspace-held) this
marks those rows operator_reconciled (agents re-verify state before acting), then wakes up to --wake N todo or
in_progress cards. Backs up the rows first. Dry-run unless --apply.
Every subprocess is bounded (PSQL_TIMEOUT / CURL_TIMEOUT below). A timed-out psql read aborts the run before
anything is applied. An internal run deadline (RUN_DEADLINE_SECONDS, below the flow-guard step bound) stops
starting new wakes, and each row starts only if a full CURL_TIMEOUT wake still fits inside the parent step
bound, so a slow-wake tick defers late rows instead of stranding them marked-but-unwoken under the step SIGKILL.
The server skips a wake while its row's hold is active, so each row is marked
operator_reconciled just before its wake. A wake that times out, fails or starts no run restores that one row
to 'blocked' for the next tick and is never counted as woken. A row begun before the deadline can still outlast
the parent step bound (its mark, wake and restore can total 160 s), so a parent SIGKILL or a psql timeout during
one row's mark or restore strands at most that row: its card is released from the hold without a wake, and its
`attempting` log line names it. A timed-out wake may already have started a run; the next tick releases that
re-held card again once the run ends, which at worst wakes it a second time.
"""
import json, os, subprocess, sys, time
ROOT = '/home/ubuntu/paperclip-enterprise-company'
CID = '00000000-0000-4000-8000-000000000004'
PARKED = ('TWO Community & Growth', 'Fleet Intelligence & Evaluation', 'Here.Now Clone', 'Host AI Bridge (MCP)', 'Kofra',
          'NNTune', 'Onboarding', 'Prism Gateway', 'Routeware Shadow API', 'TWO Web', 'Wayselect', 'cpa-plugin-zai-coding-plan')
HELD = ('case_6ceef68e', 'case_fa4fcb64', 'case_b8566208')


def timeout_seconds(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


# A hung psql or wakeup POST used to stall the board job until someone killed it by hand.
PSQL_TIMEOUT = timeout_seconds('RECONCILE_REPLAY_PSQL_TIMEOUT_SECONDS', 60)
CURL_TIMEOUT = timeout_seconds('RECONCILE_REPLAY_CURL_TIMEOUT_SECONDS', 40)  # 30 s curl --max-time, plus 10 s for the process to exit
# Internal write deadline: stop starting new wakes before the parent
# flow-guard step bound (FLOW_GUARD_STEP_TIMEOUT_SECONDS=120) can SIGKILL us.
# The deadline is validated below to sit strictly under that bound, so an
# env override cannot silently reopen the kill window.
RUN_DEADLINE_SECONDS = timeout_seconds('RECONCILE_REPLAY_RUN_DEADLINE_SECONDS', 100)
PARENT_STEP_BOUND_SECONDS = timeout_seconds('FLOW_GUARD_STEP_TIMEOUT_SECONDS', 120)
if RUN_DEADLINE_SECONDS >= PARENT_STEP_BOUND_SECONDS:
    raise ValueError(f'RECONCILE_REPLAY_RUN_DEADLINE_SECONDS ({RUN_DEADLINE_SECONDS}) must be below '
                     f'FLOW_GUARD_STEP_TIMEOUT_SECONDS ({PARENT_STEP_BOUND_SECONDS})')


def psql(sql):
    return subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At', '-F', '\t',
                           '-v', 'ON_ERROR_STOP=1'], input=sql, capture_output=True, text=True, check=True,
                          timeout=PSQL_TIMEOUT).stdout


def wake(aid, iid, ident):
    """POST one agent wakeup; returns the started run's id, or None when no run started.

    curl's own --max-time fires before CURL_TIMEOUT, exiting 28 with empty
    stdout while subprocess.run returns normally: that is a failure too, so any
    non-zero curl exit reads as None. A held or refused wake answers
    {"status": "skipped"} with a 202: that is a failure, never a wake."""
    try:
        p = subprocess.run(['curl', '-sK', f'{ROOT}/.pc.curl', '-X', 'POST', '-H', 'content-type: application/json',
                               f'http://127.0.0.1:3100/api/agents/{aid}/wakeup', '--data-binary',
                               json.dumps({'source': 'on_demand', 'reason': 'replay_block_reconciled', 'payload': {'issueId': iid}}),
                               '--max-time', '30'],
                              capture_output=True, text=True, timeout=CURL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f'{ident} wake FAILED: timed out after {CURL_TIMEOUT}s (unmeasured; retry next run)')
        return None
    if p.returncode != 0:
        print(f'{ident} wake FAILED: curl exited {p.returncode} (unmeasured; retry next run)')
        return None
    try:
        body = json.loads(p.stdout)
    except ValueError:
        body = None
    run_id = body.get('id') if isinstance(body, dict) and body.get('status') != 'skipped' else None
    if not run_id:
        print(f'{ident} wake FAILED: no run started, response {p.stdout[:80]!r} (retry next run)')
        return None
    return run_id


def reconcile_note(ts):
    return f" | operator reconciled {ts}: run was cancelled by host focus gate/restart; agent re-verifies state before acting"


def mark_reconciled(rid, ts):
    """Release one row's replay hold (single-row UPDATE), just before its wake."""
    psql(f"""update issue_recovery_actions set evidence=jsonb_set(evidence,'{{automaticRecovery,replay}}','"operator_reconciled"'),
  resolution_note=coalesce(resolution_note,'')||'{reconcile_note(ts)}',
  updated_at=now() where id='{rid}';""")


def restore_blocked(rid, ts):
    """Re-hold one row whose wake started no run (single-row UPDATE), so the next tick retries it."""
    psql(f"""update issue_recovery_actions set evidence=jsonb_set(evidence,'{{automaticRecovery,replay}}','"blocked"'),
  resolution_note=nullif(replace(coalesce(resolution_note,''),'{reconcile_note(ts)}',''),''),
  updated_at=now() where id='{rid}';""")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    apply = '--apply' in args
    wake_n = int(args[args.index('--wake') + 1]) if '--wake' in args else 10
    start = time.monotonic()
    parked = ','.join("'" + p.replace("'", "''") + "'" for p in PARKED)
    held = ','.join(f"'{h}'" for h in HELD)
    rows = [l.split('\t') for l in psql(f"""
select ra.id, i.id, i.identifier, i.status, coalesce(i.assignee_agent_id::text,'') from issue_recovery_actions ra
join issues i on i.id=ra.source_issue_id left join projects p on p.id=i.project_id
where i.company_id='{CID}' and i.status in ('todo','in_progress','in_review','blocked')
  and ra.evidence->'automaticRecovery'->>'replay'='blocked'
  and coalesce(p.name,'') not in ({parked}) and i.identifier not in ({held})
  and not exists (select 1 from heartbeat_runs r where r.context_snapshot->>'issueId'=i.id::text and r.status in ('running','queued'));""").splitlines() if l]
    print(f'{len(rows)} replay-blocked recovery rows on {len({r[1] for r in rows})} eligible cards')
    if not apply:
        for r in rows: print(' ', r[2], r[3])
        return 0
    ts = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    ids = ','.join(f"'{r[0]}'" for r in rows) or "''"
    open(f'{ROOT}/ops/productivity/replay-blocked-backup-{ts}.json', 'w').write(
        psql(f"select json_agg(ra) from issue_recovery_actions ra where id in ({ids});"))
    with open(f'{ROOT}/ops/worker-vps/operator-actions.log', 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} operator: reconcile_replay_blocked attempting "
                f"{len(rows)} row(s); backup ops/productivity/replay-blocked-backup-{ts}.json; "
                f"outcome unknown if interrupted.\n")
    # Rows that need no wake (not todo/in_progress, or no assignee) are
    # released up front with one atomic UPDATE: no wake is left for a kill to skip.
    idle_ids = [r[0] for r in rows if r[3] not in ('todo', 'in_progress') or not r[4]]
    if idle_ids:
        fids = ','.join(f"'{i}'" for i in idle_ids)
        psql(f"""update issue_recovery_actions set evidence=jsonb_set(evidence,'{{automaticRecovery,replay}}','"operator_reconciled"'),
  resolution_note=coalesce(resolution_note,'')||'{reconcile_note(ts)}',
  updated_at=now() where id in ({fids});""")
    # Rows that need a wake are released just before their wake and re-held if
    # it starts no run. Deferred rows are never touched, so they stay 'blocked'.
    woken = 0
    reconciled = len(idle_ids)
    failed, deferred = [], []
    for rid, iid, ident, st, aid in rows:
        if st not in ('todo', 'in_progress') or not aid:
            continue
        if woken >= wake_n:
            deferred.append(ident)
            continue
        elapsed = time.monotonic() - start
        if elapsed >= RUN_DEADLINE_SECONDS:
            print(f'run deadline {RUN_DEADLINE_SECONDS}s reached; {ident} deferred to next tick')
            deferred.append(ident)
            continue
        if elapsed + CURL_TIMEOUT > PARENT_STEP_BOUND_SECONDS:
            print(f'insufficient step budget for a {CURL_TIMEOUT}s wake inside the '
                  f'{PARENT_STEP_BOUND_SECONDS}s step bound; {ident} deferred to next tick')
            deferred.append(ident)
            continue
        with open(f'{ROOT}/ops/worker-vps/operator-actions.log', 'a') as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} operator: reconcile_replay_blocked attempting "
                    f"{ident} (row {rid}) before its replay mark; outcome unknown if interrupted.\n")
        mark_reconciled(rid, ts)
        run_id = wake(aid, iid, ident)
        if run_id is None:
            restore_blocked(rid, ts)
            failed.append(ident)
            continue
        reconciled += 1
        print(f'{ident} woke run {run_id}'); woken += 1
    with open(f'{ROOT}/ops/worker-vps/operator-actions.log', 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} operator: reconcile_replay_blocked reconciled {reconciled} rows "
                f"({len({r[1] for r in rows})} cards), woke {woken}; backup ops/productivity/replay-blocked-backup-{ts}.json; "
                f"rollback: restore evidence.automaticRecovery.replay='blocked' from backup\n")
        if failed:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} operator: reconcile_replay_blocked wake FAILED for "
                    f"{len(failed)} card(s) ({', '.join(failed)}); rows left 'blocked', retry next run\n")
        if deferred:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} operator: reconcile_replay_blocked deferred "
                    f"{len(deferred)} card(s) ({', '.join(deferred)}); rows left 'blocked', retry next run\n")
    return 0


if __name__ == '__main__':
    try:
        main()
    except subprocess.TimeoutExpired as e:
        print(f'reconcile-replay-blocked: FAILED: {e} (unmeasured)', file=sys.stderr)
        raise SystemExit(1)
