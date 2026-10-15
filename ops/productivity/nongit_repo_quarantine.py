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
  custody recheck and held through the move. The lock owner is a holder
  process that stays alive for the whole critical section, so the restore
  writer's dead-pid stale check never sees this lock as stale; the lock key
  hashes the realpath-resolved workspace root, like the writer. Lock
  contention blocks the move. Release removes the lock only when the
  ownership token this run wrote is still present, so a lock the writer
  has since taken is never removed. Stale-owner takeover by process-signal
  probing across namespaces is never attempted.
- Quarantined contents are preserved for the retention window; this tool
  never repairs a nested .git and never writes into a live workspace.
"""
import collections
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import select
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
LOCK_TOKEN_FILENAME = 'quarantine-owner.token'
LOCK_READY_MARKER = 'LOCK-READY'
# Upper bound on one critical section. If this tool dies mid-move the holder
# still exits on its own, after which the writer's stale check can reclaim.
LOCK_HOLDER_SECONDS = 600

Decision = collections.namedtuple('Decision', ['proceed', 'reason'])
LockHandle = collections.namedtuple('LockHandle', ['lock_path', 'token', 'process'])


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


def lock_path_for_workspace(workspace_root, instance_root=None):
    """(lock_path, hex_digest) for a canonical workspace root.

    Matches the restore writer: sha256 of the realpath-resolved container
    path, under <instanceRoot>/locks/directory-merge/<digest>.lock. Callers
    resolve the root with resolve_workspace_realpath first; this function
    hashes exactly what it gets.
    """
    if instance_root is None:
        instance_root = INSTANCE_ROOT
    digest = hashlib.sha256(workspace_root.encode('utf-8')).hexdigest()
    lock = os.path.join(str(instance_root), LOCK_SUBDIR, digest + '.lock')
    return (lock, digest)


def build_lock_holder_script():
    """Shell script for the lock holder: $1 lock dir, $2 ownership token.

    Atomic mkdir wins the lock, then owner.json records this shell's pid
    and the token file records the ownership token. The shell then prints
    the ready marker and execs sleep, keeping the same pid alive for the
    whole critical section — so the writer's process.kill(pid, 0) stale
    check never treats this lock as stale. Exits 11 when the lock is held.
    """
    return (
        'lock="$1"; token="$2"; '
        'mkdir -p "$(dirname "$lock")" || exit 12; '
        'mkdir "$lock" 2>/dev/null || exit 11; '
        'printf \'{"pid":%s,"createdAt":"%s"}\' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
        '> "$lock/owner.json" && printf %s "$token" > "$lock/' + LOCK_TOKEN_FILENAME + '" '
        '|| { rm -rf "$lock"; exit 13; }; '
        'echo ' + LOCK_READY_MARKER + '; '
        'exec sleep ' + str(LOCK_HOLDER_SECONDS)
    )


def _sql_escape_literal(value):
    return str(value).replace("'", "''")


def workspace_lookup_sql(workspace_root):
    """Rows for the workspace owning a nested dir. Fail closed on no rows."""
    esc = _sql_escape_literal(workspace_root)
    return ("select id::text, cwd, provider_ref, source_issue_id::text, status "
            "from execution_workspaces "
            f"where cwd = '{esc}' or provider_ref = '{esc}'")


def _in_list(values):
    return ", ".join("'%s'" % _sql_escape_literal(v) for v in (values or []))


def live_runs_sql(workspace_ids, issue_ids):
    """Live runs sharing workspaces or linked issues, including retries.

    Covers context issue and task keys, the execution-workspace key, native
    issue keys, queued/running/scheduled_retry states and future scheduled
    retries. An empty id list drops its predicates entirely (``false``):
    a sentinel ``in ('')`` would match every live run whose coalesced key
    is empty and over-block orphaned workspaces. Callers treat any query
    error as live.
    """
    predicates = []
    if issue_ids:
        iss_list = _in_list(issue_ids)
        predicates += [
            f"coalesce(r.context_snapshot->>'issueId','') in ({iss_list})",
            f"coalesce(r.context_snapshot->>'taskId','') in ({iss_list})",
            f"coalesce(r.context_snapshot->>'nativeIssueId','') in ({iss_list})",
            f"coalesce(r.native_issue_id::text,'') in ({iss_list})",
        ]
    if workspace_ids:
        ws_list = _in_list(workspace_ids)
        predicates.append(
            f"coalesce(r.context_snapshot->>'executionWorkspaceId','') in ({ws_list})")
    identity = " or ".join(predicates) if predicates else "false"
    return (
        "select count(*) from heartbeat_runs r where ("
        "r.status in ('queued','running','scheduled_retry') or "
        "(r.scheduled_retry_at is not null and r.scheduled_retry_at > now())) "
        f"and ({identity})"
    )


def lease_check_sql(workspace_ids):
    """Unexpired runtime leases on the workspaces. Missing table => error."""
    if not workspace_ids:
        return ("select count(*) from execution_workspace_runtime_leases "
                "where false")
    return ("select count(*) from execution_workspace_runtime_leases "
            f"where workspace_id::text in ({_in_list(workspace_ids)}) "
            "and expires_at > now()")


def identity_is_ambiguous(workspace_rows):
    """True when workspace identity is missing or empty (fail closed)."""
    return not workspace_rows


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


def resolve_workspace_realpath(workspace_root, runner=None):
    """Realpath-resolved workspace root inside the container, or None.

    The restore writer keys its lock on fs.realpath(targetDir), so the
    lock key must hash the resolved path, not the raw FIND path. Any
    failure returns None and the caller blocks the move (fail closed).
    """
    if runner is None:
        runner = sh
    try:
        out = runner(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
                      'realpath -- "$1" 2>/dev/null || readlink -f -- "$1"',
                      '_', workspace_root], timeout=60)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    lines = [line.strip() for line in (out.stdout or '').splitlines() if line.strip()]
    if not lines or not lines[-1].startswith('/'):
        return None
    return lines[-1]


def _read_ready_line(proc, timeout=60):
    """First stdout line from the holder, bounded. '' when none arrives."""
    stream = getattr(proc, 'stdout', None)
    if stream is None:
        return ''
    try:
        fd = stream.fileno()
    except Exception:
        fd = None
    if fd is not None:
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except Exception:
            ready = [fd]
        if not ready:
            return ''
    try:
        return stream.readline() or ''
    except Exception:
        return ''


def _stop_process(proc):
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for stream in (getattr(proc, 'stdout', None), getattr(proc, 'stderr', None)):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def acquire_restore_lock(canonical_root, popen=None):
    """Acquire the restore writer lock. Never steals an existing lock.

    Returns a LockHandle whose holder process stays alive (same pid as
    recorded in owner.json) until release_restore_lock stops it, or None
    when another writer holds the lock or acquisition fails. No
    stale-owner takeover is attempted: a host-side PID probe cannot
    validate a container-namespace owner PID.
    """
    if popen is None:
        popen = subprocess.Popen
    lock, _ = lock_path_for_workspace(canonical_root)
    token = secrets.token_hex(16)
    try:
        proc = popen(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
                      build_lock_holder_script(), '_', lock, token],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception:
        return None
    if _read_ready_line(proc).strip() != LOCK_READY_MARKER:
        _stop_process(proc)
        return None
    return LockHandle(lock_path=lock, token=token, process=proc)


def release_restore_lock(handle, runner=None):
    """Release a lock this run acquired. Never removes another writer.

    The lock directory is removed only while the ownership token this run
    wrote is still present; if the writer has reclaimed the path, its lock
    is left alone. The holder is stopped last, so its pid stays live (and
    the lock non-stale) throughout the token check and removal.
    """
    if handle is None:
        return
    if runner is None:
        runner = sh
    lock_path = handle.lock_path
    try:
        out = runner(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
                       'cat "$1" 2>/dev/null', '_',
                       lock_path + '/' + LOCK_TOKEN_FILENAME], timeout=60)
        if out.returncode == 0 and (out.stdout or '').strip() == handle.token:
            runner(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
                    'rm -rf "$1"', '_', lock_path], timeout=60)
    except Exception:
        pass
    finally:
        _stop_process(handle.process)


def nested_still_quarantinable(nested_dir):
    """True when the path still exists and still has no .git."""
    try:
        out = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
                  'test -d "$1" && test ! -e "$1/.git"', '_', nested_dir],
                 timeout=60)
    except Exception:
        return False
    return out.returncode == 0


def do_move(nested_dir, dest):
    """(ok, error_text) moving nested_dir to dest inside the container."""
    try:
        r = sh(['docker', 'exec', '-u', 'node', 'paperclip', 'sh', '-c',
                'mkdir -p "$1" && mv "$2" "$3"', '_', Q, nested_dir, dest])
    except Exception as exc:
        return (False, str(exc)[:200])
    if r.returncode == 0:
        return (True, '')
    return (False, (r.stderr or '').strip()[:200])


def quarantine_one(nested_dir, root, fallback, dest, apply_move,
                   live_check=None, resolve=None, acquire=None,
                   release=None, still=None, move=None):
    """One candidate end to end. Returns (outcome, detail).

    Order is the safety contract: custody precheck, then realpath, then
    acquire the restore lock, then custody recheck while holding it, then
    move, then release. Dependencies are injectable so the offline suite
    can pin this order and every skip branch without a container.
    """
    if live_check is None:
        live_check = workspace_live
    if resolve is None:
        resolve = resolve_workspace_realpath
    if acquire is None:
        acquire = acquire_restore_lock
    if release is None:
        release = release_restore_lock
    if still is None:
        still = nested_still_quarantinable
    if move is None:
        move = do_move
    label = fallback or root
    try:
        live_before = live_check(root, fallback)
    except Exception:
        return ('skip', f'skip {label}: custody check failed (fail closed)')
    if live_before:
        return ('skip', f'skip {label}: live run')
    if not apply_move:
        return ('would-move', f'would move {label}: {nested_dir}')
    canonical = resolve(root)
    if canonical is None:
        return ('skip', f'skip {label}: canonical workspace path unresolved (fail closed)')
    handle = acquire(canonical)
    if handle is None:
        return ('skip', f'skip {label}: restore writer lock held')
    try:
        try:
            final_live = live_check(root, fallback)
        except Exception:
            final_live = True
        verdict = decide(live_before, True, final_live)
        if not verdict.proceed:
            return ('skip', f'skip {label}: {verdict.reason}')
        if not still(nested_dir):
            return ('skip', f'skip {label}: path changed under lock')
        ok, err = move(nested_dir, dest)
        if ok:
            return ('moved', f'moved {label} non-git nested repo to {dest}')
        return ('failed', f'FAILED {label}: {err}')
    finally:
        release(handle)


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
        dest = f'{Q}/{fallback + "-" if fallback else ""}{d.rsplit("/", 1)[1]}-{stamp}'
        outcome, msg = quarantine_one(d, root, fallback, dest, APPLY)
        print(msg)
        if outcome in ('moved', 'failed'):
            with open(LOG, 'a') as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} nongit-quarantine: {msg}\n")


if __name__ == '__main__':
    main()
