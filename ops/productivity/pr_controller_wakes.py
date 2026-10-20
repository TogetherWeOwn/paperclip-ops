"""Track controller comment wakes until the server actually claims a run.

The host supplies its existing read-only SQL and comment API clients. No wake
configuration, issue disposition, review policy, or database writes live here.
"""
import json
import subprocess
from uuid import UUID


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def reconcile_wake(rec, key, issue_id, text, read, now):
    """Return pending/claimed/retry/new; migrate legacy skipped acknowledgements.

    Queued requests are observed, not duplicated. A skipped request is retried
    regardless of its reason. A successful comment POST alone is never delivery.
    """
    issue_id = str(UUID(issue_id))
    pending = rec.get('pending_wake')
    if pending and (pending.get('key') != key or pending.get('issue_id') != issue_id):
        rec.pop('pending_wake', None)
        pending = None
        rec.pop('woke', None)
    if not pending and rec.get('woke') != key:
        return 'new'
    acknowledged = rec.get('wake_claimed') or {}
    if not pending and acknowledged.get('key') == key and acknowledged.get('issue_id') == issue_id:
        return 'claimed'
    if pending:
        try:
            comment_id = str(UUID(pending['comment_id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            return 'pending'  # Corrupt state is not permission to repost blindly.
        match = f"c.id='{comment_id}'"
    else:
        # Older state retained no comment id. Match the exact generated message,
        # including soft-hold suffixes and reminder prefixes, on this owner only.
        match = f'position({_literal(text)} in c.body)>0'
    sql = f"""select c.id, w.status, coalesce(w.run_id::text, ''), w.claimed_at is not null
        from issue_comments c join agent_wakeup_requests w on w.payload->>'commentId'=c.id::text
        where c.issue_id='{issue_id}' and w.payload->>'issueId'='{issue_id}' and {match}
        order by c.created_at desc, (w.run_id is not null and w.claimed_at is not null) desc, w.requested_at desc limit 1"""
    try:
        rows = read(sql)
    except (subprocess.SubprocessError, OSError, ValueError):
        print('PR controller: wake acknowledgement read failed; retry next pass')
        return 'pending'
    if not rows:
        if not pending:
            return 'legacy'  # Preserve old dedup, but do not claim delivery without evidence.
        rec.pop('pending_wake', None)
        rec.pop('woke', None)
        return 'retry'
    if len(rows[0]) != 4:
        return 'pending'
    comment_id, status, run_id, claimed = rows[0]
    if run_id and claimed in ('t', 'true'):
        rec['woke'] = key
        rec['wake_claimed'] = {'key': key, 'issue_id': issue_id, 'run_id': run_id, 'comment_id': comment_id}
        rec.pop('pending_wake', None)
        # Start the stale-reminder window at actual claim, not comment acceptance.
        if pending:
            rec.setdefault('rewake', {})[key] = now()
        return 'claimed'
    rec.pop('woke', None)
    if status in ('skipped', 'failed', 'cancelled'):
        rec.pop('pending_wake', None)
        return 'retry'
    rec['pending_wake'] = {'key': key, 'issue_id': issue_id, 'comment_id': comment_id}
    return 'pending'


def post_wake(rec, key, issue_id, text, post):
    """Persist a correlatable pending comment; leave delivery for reconcile_wake."""
    issue_id = str(UUID(issue_id))
    code, body = post('POST', f'issues/{issue_id}/comments', {'body': text})
    if code.startswith('2'):
        try:
            comment_id = str(UUID(json.loads(body)['id']))
        except (KeyError, TypeError, ValueError, AttributeError):
            return code  # Missing acknowledgement stays retryable, never delivered.
        rec['pending_wake'] = {'key': key, 'issue_id': issue_id, 'comment_id': comment_id}
    return code
