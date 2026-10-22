"""Track controller comment wakes until the server actually claims a run.

The host supplies its existing read-only SQL and comment API clients. No wake
configuration, issue disposition, review policy, or database writes live here.
"""
import calendar
import json
import subprocess
import time
from uuid import UUID

# Bounded wake retries: a persistently skipped/failed/cancelled wake must not
# repost one comment per controller pass forever. Retries use exponential
# backoff (1h, 2h, 4h, ...) capped at 24h, and stop after WAKE_MAX_RETRIES
# attempts for the same event key. State resets on claim or head/owner change.
WAKE_MAX_RETRIES = 5
WAKE_RETRY_BASE_DELAY_S = 3600
WAKE_RETRY_MAX_DELAY_S = 24 * 3600


def _parse_wake_ts(value):
    """Normalize a controller timestamp to epoch seconds, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith('Z'):
                core = text[:-1]
                if '.' in core:
                    core = core.split('.')[0]
                return calendar.timegm(time.strptime(core, '%Y-%m-%dT%H:%M:%S'))
        except (ValueError, OverflowError):
            return None
        try:
            import datetime
            parsed = datetime.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return int(parsed.timestamp())
        except (ValueError, OverflowError):
            return None
    return None


def _format_wake_ts(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(epoch)))


def _wake_retry_blocked(rec, key, now):
    """True when backoff has not elapsed or the retry cap is reached."""
    retries = rec.get('wake_retries')
    if not retries:
        return False
    entry = retries.get(key)
    if not entry:
        return False
    try:
        count = int(entry.get('count', 0))
    except (TypeError, ValueError):
        return False
    if count >= WAKE_MAX_RETRIES:
        return True
    next_allowed = entry.get('next_allowed')
    if not next_allowed:
        return False
    try:
        current = now()
    except Exception:
        return False
    cur_epoch = _parse_wake_ts(current)
    nxt_epoch = _parse_wake_ts(next_allowed)
    if cur_epoch is not None and nxt_epoch is not None:
        return cur_epoch < nxt_epoch
    try:
        return str(current) < str(next_allowed)
    except Exception:
        return False


def _record_wake_retry(rec, key, now):
    """Accumulate one bounded retry; return the new attempt count."""
    retries = rec.setdefault('wake_retries', {})
    entry = retries.get(key) or {}
    try:
        count = int(entry.get('count', 0)) + 1
    except (TypeError, ValueError):
        count = 1
    delay = WAKE_RETRY_BASE_DELAY_S * (2 ** max(0, count - 1))
    if delay > WAKE_RETRY_MAX_DELAY_S:
        delay = WAKE_RETRY_MAX_DELAY_S
    next_allowed = None
    try:
        current = now()
    except Exception:
        current = None
    cur_epoch = _parse_wake_ts(current)
    if cur_epoch is not None:
        next_allowed = _format_wake_ts(cur_epoch + delay)
    retries[key] = {'count': count, 'next_allowed': next_allowed}
    return count


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def reconcile_wake(rec, key, issue_id, text, read, now):
    """Return pending/claimed/retry/new; migrate legacy skipped acknowledgements.

    Queued requests are observed, not duplicated. A skipped request is retried
    regardless of its reason, subject to per-key backoff and a retry cap. A
    coalesced request with a run id counts as claimed: the server attached the
    wake to an already-queued run instead of stamping a new claim. A successful
    comment POST alone is never delivery.
    """
    issue_id = str(UUID(issue_id))
    pending = rec.get('pending_wake')
    if pending and (pending.get('key') != key or pending.get('issue_id') != issue_id):
        old_key = pending.get('key')
        if old_key:
            (rec.get('wake_retries') or {}).pop(old_key, None)
        if pending.get('issue_id') != issue_id:
            (rec.get('wake_retries') or {}).pop(key, None)
        rec.pop('pending_wake', None)
        pending = None
        rec.pop('woke', None)
    if not pending and rec.get('woke') != key:
        if _wake_retry_blocked(rec, key, now):
            return 'pending'
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
        if _wake_retry_blocked(rec, key, now):
            return 'pending'  # Keep pending to re-query; do not repost.
        _record_wake_retry(rec, key, now)
        rec.pop('pending_wake', None)
        rec.pop('woke', None)
        return 'retry'
    if len(rows[0]) != 4:
        return 'pending'
    comment_id, status, run_id, claimed = rows[0]
    if (run_id and claimed in ('t', 'true')) or (status == 'coalesced' and run_id):
        rec['woke'] = key
        rec['wake_claimed'] = {'key': key, 'issue_id': issue_id, 'run_id': run_id, 'comment_id': comment_id}
        rec.pop('pending_wake', None)
        (rec.get('wake_retries') or {}).pop(key, None)
        # Start the stale-reminder window at actual claim, not comment acceptance.
        if pending:
            rec.setdefault('rewake', {})[key] = now()
        return 'claimed'
    rec.pop('woke', None)
    if status in ('skipped', 'failed', 'cancelled'):
        if _wake_retry_blocked(rec, key, now):
            rec.pop('pending_wake', None)
            return 'pending'
        _record_wake_retry(rec, key, now)
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
