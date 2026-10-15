#!/usr/bin/env python3
"""Hide Paperclip-made "nothing new" comments (owner GO 2026-10-05 ~12:50Z) until fork fix case_4dbdd38c ships.

Paperclip 1001 turns a run's final message into an issue comment, even when the run was a no-event wake
(monitor due, continuation, stalled dispatch) and did nothing. Those notes are noise in every thread.
This tombstones ONLY comments that are all of:
  - created by the server from a run's final message (presentationDecision.commentId = the comment, action create),
  - from a run woken with no new event (WAKES below),
  - saying nothing happened (NOOP regex), and at least 2 minutes old.
Agent-written and human comments are never touched. Before tombstoning, the original body, presentation and
metadata go to BACKUP (0600) so `--restore all|<comment-id>` can put them back. Dry run by default; --apply.
Retire this once case_4dbdd38c is deployed.
Every psql call is bounded by PSQL_TIMEOUT below. A timeout aborts with a
non-zero exit before anything is hidden: unmeasured, never healthy-empty.
"""
import json, os, subprocess, sys, time

ROOT = '/home/ubuntu/paperclip-enterprise-company'
CID = '00000000-0000-4000-8000-000000000004'
BACKUP = f'{ROOT}/ops/productivity/hidden-noop-comments.jsonl'
STATE_DIR = os.path.expanduser('~/.local/state/flow-guard')
WAKES = ("issue_continuation_needed", "issue_monitor_due", "dispatch_stalled_issue", "issue_assignment_recovery",
         "issue_graph_liveness_backstop", "replay_block_reconciled")
NOOP = (r'(no-op|nothing new|no new (activity|input|external|evidence|comments?)|duplicate wake|handled quietly|'
        r'nothing actionable|no action taken|no change(s)? (made|needed)|no comment posted)')

# A hung psql used to stall the job until someone killed it by hand.
PSQL_TIMEOUT = 60


def psql(sql):
    try:
        p = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At',
                            '-v', 'ON_ERROR_STOP=1'], input=sql, capture_output=True, text=True,
                           timeout=PSQL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit(f'psql timed out after {PSQL_TIMEOUT}s (unmeasured; nothing hidden)')
    if p.returncode:
        raise SystemExit(f'psql failed: {p.stderr[-300:]}')
    return p.stdout


def lit(s):
    return "'" + s.replace("'", "''") + "'"


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    apply = '--apply' in args
    lookback = args[args.index('--lookback') + 1] if '--lookback' in args else '3 hours'
    if os.path.exists(f'{STATE_DIR}/PAUSE'):
        return 0

    if '--restore' in args:
        which = args[args.index('--restore') + 1]
        rows = [json.loads(l) for l in open(BACKUP)] if os.path.exists(BACKUP) else []
        rows = [r for r in rows if which in ('all', r['id'])]
        for r in rows:
            psql(f"""update issue_comments set body={lit(r['body'])},
                  presentation={lit(r['presentation']) + '::jsonb' if r['presentation'] else 'null'},
                  metadata={lit(r['metadata']) + '::jsonb' if r['metadata'] else 'null'},
                  deleted_at=null, deleted_by_type=null, deleted_by_user_id=null, deleted_by_agent_id=null,
                  deleted_by_run_id=null, updated_at=now() where id='{r['id']}';""")
        print(f'restored {len(rows)}')
        return 0

    wakes = ','.join(lit(w) for w in WAKES)
    rows = psql(f"""select json_build_object('id', c.id, 'issue', i.identifier, 'body', c.body,
          'presentation', c.presentation::text, 'metadata', c.metadata::text, 'run', r.id,
          'wake', r.context_snapshot->>'wakeReason', 'at', c.created_at)
      from issue_comments c join heartbeat_runs r on r.id=c.created_by_run_id join issues i on i.id=c.issue_id
      where c.company_id='{CID}' and c.deleted_at is null and c.author_agent_id is not null
        and c.created_at > now()-interval '{lookback}' and c.created_at < now()-interval '2 minutes'
        and r.result_json->'presentationDecision'->>'commentId'=c.id::text
        and r.result_json->'presentationDecision'->>'commentAction'='create'
        and r.context_snapshot->>'wakeReason' in ({wakes})
        and c.body ~* '{NOOP}';""").splitlines()
    rows = [json.loads(l) for l in rows if l]
    print(f'{len(rows)} Paperclip-made no-op comment(s) in the last {lookback}')
    if not apply or not rows:
        return 0
    owner = psql(f"select responsible_user_id from (select configuration->>'responsibleUserId' responsible_user_id "
                 f"from chat_github_configurations limit 1) x;").strip() or None
    fd = os.open(BACKUP, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    ids = ','.join(f"'{r['id']}'" for r in rows)
    psql(f"""begin;
update issue_comments set body='[hidden: no-op run note, see ops/productivity/hidden-noop-comments.jsonl]',
  presentation=null, metadata=null, deleted_at=now(), deleted_by_type='user',
  deleted_by_user_id={lit(owner) if owner else 'null'}, updated_at=now()
  where id in ({ids}) and deleted_at is null;
commit;""")
    with open(f'{ROOT}/ops/worker-vps/operator-actions.log', 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} hide-noop-comments: tombstoned {len(rows)} "
                f"Paperclip-made no-op run notes (backup {BACKUP}). Rollback: hide_noop_comments.py --restore all\n")
    print(f'hid {len(rows)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
