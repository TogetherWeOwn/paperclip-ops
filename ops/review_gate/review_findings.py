#!/usr/bin/env python3
"""Sanitized source-acquisition artifact; no installation or runtime configuration included."""
import review_gate_runtime as runtime
import json
import re
import subprocess
import sys
import time
from pathlib import Path
ROOT = runtime.ROOT
EP = runtime.ENDPOINT_ID
CONFIG = runtime.FINDINGS_CONFIG
CTX_DIR = runtime.CONTEXT_DIR
FLAGS = runtime.FINDINGS_FLAGS
LOG = runtime.LOG
BLOCKING_SEV = {'error', 'warning'}
HIGH_CATEGORIES = {'security'}
HIGH_PATHS = ['secur|auth|login|oauth|token|secret|password|credential|crypt|rbac|permission|sandbox|isolate', 'migrat|/migrate|alembic|diesel|sqlx|\\.sql$|schema', 'bill|payment|stripe|price|subscri|invoice|checkout', 'deploy|dockerfile|docker-compose|\\.github/workflows|ruleset|caddyfile|terraform|\\.tf$|k8s|manifest', 'data-loss|destruct|wipe|drop-|purge']
_HIGH = re.compile('|'.join((f'(?:{p})' for p in HIGH_PATHS)), re.IGNORECASE)

def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)

def q(sql):
    r = sh(runtime.DB_COMMAND + ['-AtF', '\t', '-c', sql])
    if r.returncode != 0:
        raise RuntimeError(f'psql failed: {r.stderr.strip()[:200]}')
    return [l.split('\t') for l in r.stdout.splitlines() if l]

def gh_env():
    token = sh(runtime.TOKEN_COMMAND).stdout.strip()
    return {'GH_TOKEN': token, 'PATH': '/usr/bin:/bin'}

def norm_repo(repo):
    repo = (repo or '').strip().lower()
    if '/' not in repo:
        return f'{runtime.DEFAULT_OWNER}/{repo}'
    return repo

def short_name(full):
    return full.split('/', 1)[1] if '/' in full else full

def is_high(path, category=None):
    if (category or '').strip().lower() in HIGH_CATEGORIES:
        return True
    return bool(_HIGH.search(path or ''))

def in_scope(repo):
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return True
    try:
        if time.time() > time.mktime(time.strptime(cfg.get('canary_until', ''), '%Y-%m-%dT%H:%M:%SZ')):
            return True
    except (ValueError, TypeError, OverflowError):
        return True
    repos = {r.lower() for r in cfg.get('canary_repos', [])}
    return short_name(norm_repo(repo)) in repos

def prior(full, pr):
    rows = q(f"""select head_sha, coalesce(assessment->>'score',''), coalesce(conclusion,''),\n        to_char(created_at,'YYYY-MM-DD"T"HH24:MI:SS"Z"'),\n        coalesce((select jsonb_agg(jsonb_build_object('path',f->>'path','line',f->>'line',\n          'severity',f->>'severity','category',f->>'category','title',f->>'title',\n          'key',f->>'key','evidence',f->>'evidence'))\n          from jsonb_array_elements(coalesce(assessment->'findings','[]'::jsonb)) f\n          where f->>'severity' in ('error','warning'))::text, '[]')\n        from chat_github_reviews where endpoint_id='{EP}' and lower(repository)='{norm_repo(full)}'\n        and pull_number={int(pr)} and state='completed' order by created_at""")
    out = []
    for head, score, concl, ts, findings in rows:
        try:
            fl = json.loads(findings)
        except ValueError:
            fl = []
        out.append({'head': head, 'score': score, 'conclusion': concl, 'reviewed_at': ts, 'blocking': fl if isinstance(fl, list) else []})
    return out

def delta(full, base_head, head):
    env = gh_env()
    res = sh(['gh', 'api', f'repos/{norm_repo(full)}/compare/{base_head}...{head}', '--jq', '[.files[].filename]'], env=env)
    if res.returncode:
        raise RuntimeError(f'compare failed: {res.stderr.strip()[:160]}')
    try:
        files = json.loads(res.stdout or '[]')
    except ValueError:
        raise RuntimeError('compare returned non-JSON')
    return {'from': base_head, 'to': head, 'files': files}

def context(full, pr, head, write=True):
    full = norm_repo(full)
    prev = prior(full, pr)
    earlier = [r for r in prev if r['head'].lower() != head.lower()]
    if not earlier:
        return {'repo': full, 'pr': int(pr), 'head': head, 'round': len(prev) + 1, 'prior': None, 'delta': None, 'note': 'round 1: no prior review'}
    last = earlier[-1]
    try:
        d = delta(full, last['head'], head)
    except RuntimeError as e:
        d = {'from': last['head'], 'to': head, 'files': None, 'error': str(e)[:160]}
    ctx = {'repo': full, 'pr': int(pr), 'head': head, 'round': len(prev) + 1, 'prior': {'head': last['head'], 'score': last['score'], 'conclusion': last['conclusion'], 'blocking': last['blocking']}, 'delta': d}
    if write:
        CTX_DIR.mkdir(exist_ok=True)
        (CTX_DIR / f'{short_name(full)}-{pr}-{head[:12]}.json').write_text(json.dumps(ctx, indent=1))
    return ctx

def postcheck(full, pr, record=False):
    full = norm_repo(full)
    rows = prior(full, pr)
    if len(rows) < 2:
        return {'repo': full, 'pr': int(pr), 'round': len(rows), 'late_novel': [], 'round1_completeness': 1.0, 'note': 'fewer than 2 completed reviews: nothing to adjudicate'}
    latest, prev = (rows[-1], rows[-2])
    try:
        d = delta(full, prev['head'], latest['head'])
        dfiles = set(d['files'] or [])
    except RuntimeError as e:
        return {'repo': full, 'pr': int(pr), 'round': len(rows), 'head': latest['head'], 'prev_head': prev['head'], 'late_novel': [], 'round1_completeness': None, 'note': f'delta unavailable: {e}'}
    late = []
    for f in latest['blocking']:
        p = f.get('path') or ''
        if p in dfiles:
            continue
        late.append({'path': p, 'line': f.get('line'), 'severity': f.get('severity'), 'title': f.get('title'), 'tag': 'late-novel', 'high': is_high(p, f.get('category')), 'note': 'blocking finding on unchanged code: STAYS BLOCKING; reviewer must tag late-novel with evidence'})
    total = len(latest['blocking'])
    nonhigh = sum((1 for g in late if not g['high']))
    completeness = round(1 - nonhigh / total, 3) if total else 1.0
    out = {'repo': full, 'pr': int(pr), 'round': len(rows), 'head': latest['head'], 'prev_head': prev['head'], 'delta_files': len(dfiles), 'blocking_total': total, 'late_novel': late, 'round1_completeness': completeness}
    if record and late:
        with FLAGS.open('a') as fh:
            fh.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **out}) + '\n')
        with LOG.open('a') as fh:
            fh.write(f'{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} review-findings: {full}#{pr} round {len(rows)}: {len(late)} late-novel blocker(s) (round-1 completeness {completeness}): ' + '; '.join((f'{g['path']}:{g['line']}' for g in late)) + '\n')
    return out

def rounds_info(pairs):
    vals = ','.join((f"('{norm_repo(r)}',{int(p)})" for r, p in pairs))
    if not vals:
        return {}
    rows = q(f"select lower(repository), pull_number, count(*),\n        (array_agg(head_sha order by created_at desc))[1]\n        from chat_github_reviews where endpoint_id='{EP}' and state='completed'\n        and (lower(repository), pull_number) in ({vals})\n        group by 1, 2")
    return {(r, int(p)): (int(n), h) for r, p, n, h in rows}

def recent_round2(since_ts):
    rows = q(f"select lower(repository), pull_number from chat_github_reviews\n        where endpoint_id='{EP}' and state='completed'\n        and created_at > to_timestamp({float(since_ts)})\n        group by 1, 2 having count(*) >= 2")
    return [(r, int(p)) for r, p in rows]
