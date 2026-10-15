#!/usr/bin/env python3
"""Quarantine nested non-git repository copies in card worktrees.

SSH sync-back can leave <worktree>/.paperclip-repositories/<repo> without
.git; the next run on that card fails setup because the path exists but is
not a git checkout. For each such dir with no live custody, move it (same
filesystem) to the quarantine directory so the runner re-clones.
Quarantined copies older than 7 days are deleted. Dry-run by default;
--apply moves. Rollback: move the quarantined dir back.

Safety properties (fail closed):

- An adjacent hold file inhibits move AND seven-day purge before any
  container calls. A missing hold file means proceed; a present, malformed
  or unparseable hold means hold. Only an expired hold releases.
- Custody is resolved from the actual execution workspace / canonical path,
  never from a card identifier parsed from a directory name alone. A
  missing, ambiguous or unreadable workspace identity blocks the move.
- All issues and runs sharing the workspace are checked: context issue and
  task keys, native issue keys, queued/running/scheduled-retry states and
  future scheduled retries, plus unexpired workspace leases where present.
  Any database error blocks the move.
- The move coordinates with the SSH restore writer lock: the same atomic
  directory lock the restore writer holds is acquired before the final
  custody recheck and held through the move. Lock contention blocks the
  move. An existing writer lock is never removed. Stale-owner takeover by
  process-signal probing across namespaces is never attempted.
- Quarantined contents are preserved for the retention window; this tool
  never repairs a nested .git and never writes into a live workspace.
"""
import collections
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time

APPLY = '--apply' in sys.argv
Q = '/paperclip/instances/default/quarantine-nongit'
LOG = '/home/ubuntu/paperclip-enterprise-company/ops/worker-vps/operator-actions.log'
# card worktrees, plus project-local worktrees (projects/<co>/<proj>/<repo>/.paperclip/worktrees/<name>/...); the
# latter must also be untouched for 30 min because the worktree name need not match the card that uses it.
FIND = ('for d in /paperclip/instances/default/worktrees/*/*/.paperclip-repositories/*; do '
        '[ -d "$d" ] && [ ! -e "$d/.git" ] && echo "$d"; done; '
        'for d in /paperclip/instances/default/projects/*/*/*/.paperclip/worktrees/*/.paperclip-repositories/*; do '
        '[ -d "$d" ] && [ ! -e "$d/.git" ] && [ -z "$(find "$d" -maxdepth 2 -newermt -30minutes -print -quit)" ] '
        '&& echo "$d"; done')

HOLD_FILENAME = 'nongit-quarantine.HOLD.json'
HOLD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), HOLD_FILENAME)
INSTANCE_ROOT = '/paperclip/instances/default'
LOCK_SUBDIR = 'locks/directory-merge'
LIVE_RUN_STATUSES = ('queued', 'running', 'scheduled_retry')

Decision = collections.namedtuple('Decision', ['proceed', 'reason'])


# ---------------------------------------------------------------------------
# PURE CORE. No subprocess, no clock reads except via arguments, no env.
# The offline suite drives every branch with synthetic fixtures.
# ---------------------------------------------------------------------------

def _parse_expiry_epoch(value):
    """Epoch seconds from an ISO-8601 string, or None when unusable."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    try:
        return stamp.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def hold_status(data, now_epoch):
    """(active, reason) for a hold document. Fail closed: malformed holds."""
    if data is None:
        return (False, 'no-hold-file')
    if not isinstance(data, dict):
        return (True, 'hold-malformed-not-an-object')
    expiry = None
    for key in ('expiry', 'expiresAt', 'expires_at'):
        if data.get(key) is not None:
            expiry = data.get(key)
            break
    epoch = _parse_expiry_epoch(expiry)
    if epoch is None:
        return (True, 'hold-malformed-expiry')
    if epoch > float(now_epoch):
        return (True, 'hold-active')
    return (False, 'hold-expired')


def load_hold_doc(path):
    """Hold document at path: None when missing, parsed JSON otherwise.

    Read or parse failures return a non-dict sentinel so hold_status treats
    the hold as active (fail closed). Never raises.
    """
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    except OSError:
        return {'__hold_read_error__': True}
    try:
        return json.loads(raw)
    except ValueError:
        return {'__hold_malformed__': True}


def workspace_root_for_nested(nested_dir):
    """Worktree root owning a nested checkout, or None when ambiguous.

    <root>/.paperclip-repositories/<repo> -> <root>. Any path without the
    marker is ambiguous and must block (fail closed).
    """
    if not nested_dir or not isinstance(nested_dir, str):
        return None
    marker = '/.paperclip-repositories/'
    if marker not in nested_dir:
        return None
    root = nested_dir.split(marker, 1)[0]
    if not root or root == nested_dir:
        return None
    return root


def lock_path_for_workspace(workspace_root, instance_root=INSTANCE_ROOT):
    """(lock_path, hex_digest) for a canonical workspace root.

    Matches the restore writer: sha256 of the canonical container path,
    under <instanceRoot>/locks/directory-merge/<digest>.lock. Callers pass
    the realpath-resolved root; this function hashes exactly what it gets.
    """
    digest = hashlib.sha256(workspace_root.encode('utf-8')).hexdigest()
    lock = os.path.join(str(instance_root), LOCK_SUBDIR, digest + '.lock')
    return (lock, digest)


def _sql_escape_literal(value):
    return str(value).replace("'", "''")


def workspace_lookup_sql(workspace_root):
    """Rows for the workspace owning a nested dir. Fail closed on no rows."""
    esc = _sql_escape_literal(workspace_root)
    return ("select id::text, cwd, provider_ref, source_issue_id::text, status "
            "from execution_workspaces "
            f"where cwd = '{esc}' or provider_ref = '{esc}'")


def live_runs_sql(workspace_ids, issue_ids):
    """Live runs sharing workspaces or linked issues, including retries.

    Covers context issue and task keys, the execution-workspace key, native
    issue keys, queued/running/scheduled_retry states and future scheduled
    retries. Callers treat any query error as live.
    """
    ws = ", ".join("'%s'" % _sql_escape_literal(v) for v in (workspace_ids or []))
    iss = ", ".join("'%s'" % _sql_escape_literal(v) for v in (issue_ids or []))
    ws_list = ws if ws else "''"
    iss_list = iss if iss else "''"
    return (
        "select count(*) from heartbeat_runs r where ("
        f"r.status in ('queued','running','scheduled_retry') or "
        f"(r.scheduled_retry_at is not null and r.scheduled_retry_at > now())) "
        f"and (coalesce(r.context_snapshot->>'issueId','') in ({iss_list}) "
        f"or coalesce(r.context_snapshot->>'taskId','') in ({iss_list}) "
        f"or coalesce(r.context_snapshot->>'executionWorkspaceId','') in ({ws_list}) "
        f"or coalesce(r.context_snapshot->>'nativeIssueId','') in ({iss_list}) "
        f"or coalesce(r.native_issue_id::text,'') in ({iss_list}))"
    )


def lease_check_sql(workspace_ids):
    """Unexpired runtime leases on the workspaces. Missing table => error."""
    ws = ", ".join("'%s'" % _sql_escape_literal(v) for v in (workspace_ids or []))
    ws_list = ws if ws else "''"
    return ("select count(*) from execution_workspace_runtime_leases "
            f"where workspace_id::text in ({ws_list}) and expires_at > now()")


def identity_is_ambiguous(workspace_rows):
    """True when workspace identity is missing or empty (fail closed)."""
    return not workspace_rows


def any_linked_live(linked):
    """True when any linked issue has live work. Empty mapping is not live."""
    if not linked:
        return False
    return any(bool(v) for v in linked.values())


_LINK_KEYS = ('issueId', 'taskId', 'nativeIssueId', 'native_issue_id',
              'executionWorkspaceId', 'execution_workspace_id')


def row_matches_linked(row, linked_ids):
    """True when a run row references any linked issue or workspace id."""
    if not row or not linked_ids:
        return False
    try:
        ids = set(linked_ids)
    except TypeError:
        return True
    for key in _LINK_KEYS:
        try:
            value = row.get(key)
        except AttributeError:
            return True
        if value is not None and value in ids:
            return True
    return False


def run_status_is_live(status, scheduled_retry_at):
    """True for queued/running/scheduled_retry or a future scheduled retry.

    A present but unparseable retry timestamp is live (fail closed).
    """
    if status in LIVE_RUN_STATUSES:
        return True
    if scheduled_retry_at is None or scheduled_retry_at == '':
        return False
    if not isinstance(scheduled_retry_at, str):
        return True
    epoch = _parse_expiry_epoch(scheduled_retry_at)
    if epoch is None:
        return True
    try:
        return epoch > time.time()
    except Exception:
        return True


def decide(live_before_lock, lock_acquired, live_under_lock):
    """Final move decision. Only a clean-clean-clean triple proceeds."""
    if live_before_lock:
        return Decision(False, 'live-run-ref')
    if not lock_acquired:
        return Decision(False, 'writer-lock-held')
    if live_under_lock:
        return Decision(False, 'live-run-ref-under-lock')
    return Decision(True, 'ok')


# ---------------------------------------------------------------------------
# LIVE SHELL.
# ---------------------------------------------------------------------------

def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def psql_atc(sql):
    """(ok, stdout) for one read-only query. Any error is not ok."""
    try:
        out = sh(['docker', 'exec', '-i', 'paperclip-db', 'psql', '-U', 'paperclip',
                  '-d', 'paperclip', '-Atc', sql], timeout=60)
    except Exception:
        return (False, '')
    if out.returncode != 0:
        return (False, out.stdout or '')
    return (True, out.stdout or '')


def live_runs(ident):
    """Legacy parent-identifier check, kept as an additional hint only.

    True means live or unknown (fail closed). This check alone is NOT
    sufficient: siblings can share the workspace while the parent is idle.
    """
    sql = ("select count(*) from heartbeat_runs r join issues i on i.id::text = r.context_snapshot->>'issueId' "
           f"where i.identifier = '{_sql_escape_literal(ident)}' and r.status in ('queued','running')")
    ok, stdout = psql_atc(sql)
    if not ok:
        return True
    try:
        return int((stdout or '').strip() or '1') > 0
    except ValueError:
        return True


def workspace_live(workspace_root, fallback_ident=None):
    """True when the workspace has live custody or identity is unusable.

    Checks all workspace rows for the canonical path, all linked issues and
    all runs sharing the workspace, plus unexpired leases. Any database
    error, missing row set or ambiguous identity returns True.
    """
    root = workspace_root
    if not root:
        return True
    ok, stdout = psql_atc(workspace_lookup_sql(root))
    if not ok:
        return True
    rows = [line for line in (stdout or '').splitlines() if line.strip()]
    if identity_is_ambiguous(rows):
        return True
    ws_ids = []
    linked_issue_ids = set()
    for line in rows:
        parts = line.split('|')
        if not parts or not parts[0].strip():
            return True
        ws_ids.append(parts[0].strip())
        if len(parts) > 3 and parts[3].strip() and parts[3].strip() != '':
            linked_issue_ids.add(parts[3].strip())
    # Linked issues via the issues table for these workspaces.
    if ws_ids:
        ws_list = ", ".join("'%s'" % _sql_escape_literal(v) for v in ws_ids)
        ok2, out2 = psql_atc(
            "select id::text from issues where execution_workspace_id::text in "
            f"({ws_list}) or id::text in ({ws_list})")
        if not ok2:
            return True
        for line in (out2 or '').splitlines():
            ident = line.strip()
            if ident:
                linked_issue_ids.add(ident)
    if fallback_ident:
        # Additional hint only; the workspace join above is authoritative.
        try:
            if live_runs(fallback_ident):
                return True
        except Exception:
            return True
    ok3, out3 = psql_atc(live_runs_sql(ws_ids, sorted(linked_issue_ids)))
    if not ok3:
        return True
    try:
        if int((out3 or '').strip() or '1') > 0:
            return True
    except ValueError:
        return True
    ok4, out4 = psql_atc(lease_check_sql(ws_ids))
    if not ok4:
        # A missing lease table reads as an error here; treat unknown lease
        # state as live only when the table is expected. An empty result
        # (no leases) is clear. Parse failure blocks.
        text = (out4 or '').strip().lower()
        if 'does not exist' in text or 'undefined_table' in text:
            pass
        else:
            return True
    else:
        try:
            if int((out4 or '').strip() or '0') > 0:
                return True
        except ValueError:
            return True
    return False


def acquire_restore_lock(workspace_root):
    """Acquire the restore writer lock. Never steals an existing lock.

    Returns (acquired, lock_path). Acquisition is one atomic mkdir inside
    the container; an existing directory means another writer holds it.
    No stale-owner takeover is attempted: a host-side PID probe cannot
    validate a container-namespace owner PID.
    """
    lock, _ = lock_path_for_workspace(workspace_root)
    esc_lock = lock.replace("'", "'\"'\"'")
    script = (
        "lock='" + esc_lock + "'; "
        "mkdir -p \"$(dirname \"$lock\")\" && "
        "if mkdir \"$lock\" 2>/dev/null; then "
        "printf '{\"pid\":%s,\"createdAt\":\"%s\"}' \"$$\" \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > \"$lock/owner.json\"; "
        "exit 0; else exit 11; fi"
    )
    try:
        out = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c', script],
                 timeout=60)
    except Exception:
        return (False, lock)
    return (out.returncode == 0, lock)


def release_restore_lock(lock_path, acquired):
    """Release a lock this run acquired. Never removes another writer."""
    if not acquired or not lock_path:
        return
    try:
        sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
            'rm -rf "$1"', '_', lock_path], timeout=60)
    except Exception:
        pass


def nested_still_quarantinable(nested_dir):
    """True when the path still exists and still has no .git."""
    try:
        out = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
                  'test -d "$1" && test ! -e "$1/.git"', '_', nested_dir],
                 timeout=60)
    except Exception:
        return False
    return out.returncode == 0


def main():
    # Hold first: no container calls when held. Preserves the installed
    # early-return behavior until the source fix is reviewed and deployed.
    try:
        hold_doc = load_hold_doc(HOLD_PATH)
        now_epoch = time.time()
        active, _ = hold_status(hold_doc, now_epoch)
    except Exception:
        print('skip: hold unreadable (fail closed)')
        return
    if active:
        print('hold active: quarantine and retention sweep paused')
        return
    dirs = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c', FIND]).stdout.split()
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    if APPLY:  # quarantined copies are regenerable sync-back leftovers; keep 7 days for evidence
        sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
            f'[ -d {Q} ] && find {Q} -mindepth 1 -maxdepth 1 -ctime +7 -exec rm -rf {{}} +'])
    for d in dirs:
        m = re.search(r'/(TOG-\d+)-', d)
        fallback = m.group(1) if m else None
        root = workspace_root_for_nested(d)
        if root is None:
            print(f'skip ambiguous workspace identity: {d}')
            continue
        try:
            live_before = workspace_live(root, fallback)
        except Exception:
            print(f'skip {fallback or root}: custody check failed (fail closed)')
            continue
        if live_before:
            print(f'skip {fallback or root}: live run')
            continue
        dest = f'{Q}/{fallback + "-" if fallback else ""}{d.rsplit("/", 1)[1]}-{stamp}'
        if not APPLY:
            print(f'would move {fallback or root}: {d}')
            continue
        acquired, lock = acquire_restore_lock(root)
        final_live = True
        try:
            if not acquired:
                print(f'skip {fallback or root}: restore writer lock held')
                continue
            try:
                final_live = workspace_live(root, fallback)
            except Exception:
                final_live = True
            verdict = decide(live_before, acquired, final_live)
            if not verdict.proceed:
                print(f'skip {fallback or root}: {verdict.reason}')
                continue
            if not nested_still_quarantinable(d):
                print(f'skip {fallback or root}: path changed under lock')
                continue
            r = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c', 'mkdir -p "$1" && mv "$2" "$3"', '_', Q, d, dest])
            msg = f'moved {fallback or root} non-git nested repo to {dest}' if r.returncode == 0 else f'FAILED {fallback or root}: {r.stderr.strip()[:200]}'
            print(msg)
            with open(LOG, 'a') as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} nongit-quarantine: {msg}\n")
        finally:
            release_restore_lock(lock, acquired)


if __name__ == '__main__':
    main()
