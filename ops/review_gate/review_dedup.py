#!/usr/bin/env python3
"""Sanitized source-acquisition artifact; no installation or runtime configuration included."""
import review_gate_runtime as runtime
import re
import subprocess
EP = runtime.ENDPOINT_ID
MAX_ERRORS = 2
QUEUED_STALE_H = 6
TERMINAL_FAIL = {'error', 'incomplete'}
_HEX = re.compile('\\A[0-9a-f]{4,64}\\Z')

def q(sql):
    r = subprocess.run(runtime.DB_COMMAND + ['-AtF', '\t', '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'psql failed: {r.stderr.strip()[:200]}')
    return [l.split('\t') for l in r.stdout.splitlines() if l]

def _check_pair(repo, pr, head):
    if not _HEX.match(head or ''):
        raise ValueError(f'bad head sha: {head!r}')
    return f"('{repo.lower().replace(chr(39), chr(39) * 2)}',{int(pr)},'{head.lower()}')"

def coverage(pairs):
    pairs = [(r.lower(), int(p), h.lower()) for r, p, h in pairs]
    if not pairs:
        return {}
    vals = ','.join((_check_pair(r, p, h) for r, p, h in pairs))
    rows = q(f"select lower(g.repository), g.pull_number, g.head_sha, g.state,\n        extract(epoch from g.created_at), coalesce(h.status,'')\n        from chat_github_reviews g left join heartbeat_runs h on h.id = g.run_id\n        where g.endpoint_id='{EP}' and g.shadow_label is null\n        and (lower(g.repository), g.pull_number, g.head_sha) in ({vals})\n        order by g.created_at")
    by_head = {}
    for repo, pr, head, state, ts, run in rows:
        by_head.setdefault((repo, int(pr), head), []).append((state, float(ts), run))
    import time
    now = time.time()
    out = {}
    for key in {(r, p, h) for r, p, h in pairs}:
        rs = by_head.get(key, [])
        if any((s == 'completed' for s, _, _ in rs)):
            out[key] = 'covered'
            continue
        if any((s in ('queued', 'running') and (run in ('queued', 'running') or (s == 'queued' and now - ts < QUEUED_STALE_H * 3600)) for s, ts, run in rs)):
            out[key] = 'in_flight'
            continue
        fails = sum((1 for s, _, _ in rs if s in TERMINAL_FAIL))
        stranded = any((s == 'running' for s, _, run in rs if run not in ('queued', 'running')))
        if fails + (1 if stranded else 0) >= MAX_ERRORS:
            out[key] = 'exhausted'
        else:
            out[key] = 'retryable'
    return out

def may_request(repo, pr, head):
    st = coverage([(repo, pr, head)])[repo.lower(), int(pr), head.lower()]
    if st == 'covered':
        return (False, 'completed review already exists for this head')
    if st == 'in_flight':
        return (False, 'a review for this head is already queued/running')
    if st == 'exhausted':
        return (False, f'{MAX_ERRORS} attempts already failed on this head; needs a human, not another request')
    return (True, 'no completed or in-flight review for this head')
