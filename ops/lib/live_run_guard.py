"""Shared guard (owner productivity plan 2026-10-04): host scripts must never comment on a card that has a running or
queued run or a pending scheduled retry. On Paperclip 1001 a comment cancels the active retry
(issue_comment_scheduled_retry_superseded), so script nudges were killing the work they meant to help."""
import re
import subprocess

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def issue_has_live_work(issue_id):
    """True if the issue has a queued, running or scheduled_retry run, due or not. Fail-closed: errors count as live."""
    if not issue_id or not _UUID.fullmatch(str(issue_id)):
        return True
    q = ("select exists(select 1 from heartbeat_runs h where coalesce(h.context_snapshot->>'issueId', "
         "h.context_snapshot->>'taskId')='%s' and h.status in ('queued','running','scheduled_retry'))" % issue_id)
    try:
        out = subprocess.run(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-At', '-c', q],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return out != 'f'
    except Exception:
        return True
