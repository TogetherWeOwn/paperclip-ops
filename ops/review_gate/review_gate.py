#!/usr/bin/env python3
"""Sanitized source-acquisition artifact; no installation or runtime configuration included."""
import review_gate_runtime as runtime
import json
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_dedup as dedup
import review_findings as findings
ROOT = runtime.ROOT
CID = runtime.COMPANY_ID
EP = runtime.ENDPOINT_ID
STEWARD = runtime.BATCH_EXECUTOR_ID
PAUSE = runtime.GATE_PAUSE
STATE = runtime.GATE_STATE
LOG = runtime.LOG
RETRY_AFTER = 3600
MAX_QUEUED = 20
FOCUS_REPOS = runtime.FOCUS_REPOS
GREEN = {'SUCCESS', 'NEUTRAL', 'SKIPPED'}
ADVISORY_BOTS = {'CodeRabbit'}
QUERY = 'query($o:String!,$r:String!,$cursor:String){repository(owner:$o,name:$r){isArchived pullRequests(states:OPEN,first:20,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}){totalCount pageInfo{hasNextPage endCursor} nodes{\nnumber isDraft isCrossRepository author{__typename login} headRefOid url headRef{target{... on Commit{oid statusCheckRollup{contexts(first:100){pageInfo{hasNextPage} nodes{\n... on CheckRun{name status conclusion startedAt checkSuite{workflowRun{workflow{name}}}} ... on StatusContext{context state createdAt}}}}}}}}}}}'
REVIEWER = runtime.REVIEWER_ID

def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)

def q(sql):
    out = sh(runtime.DB_COMMAND + ['-AtF', '\t', '-c', sql]).stdout
    return [l.split('\t') for l in out.splitlines() if l]

def log(msg):
    with LOG.open('a') as f:
        f.write(f'{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} review-gate: {msg}\n')

def gate_admit(inflight, free):
    try:
        return int(free) > 0 and int(inflight) < int(free)
    except (TypeError, ValueError):
        return False
REPO_RANK = runtime.REPO_RANK

def due_rank(full, num, priority):
    if (full.lower(), int(num)) in priority:
        return 0
    name = full.split('/')[-1].lower()
    for prefix, rank in REPO_RANK:
        if name.startswith(prefix):
            return rank
    return 4

def fair_order(due, info, priority):
    ranked = {}
    for d in due:
        ranked.setdefault(due_rank(*info[d[0]], priority), []).append(d)
    out = []
    for rank in sorted(ranked):
        if rank == 0:
            out.extend(ranked[rank])
            continue
        queues = {}
        for d in ranked[rank]:
            queues.setdefault(info[d[0]][0].lower(), []).append(d)
        lanes = list(queues.values())
        while lanes:
            out.extend((lane.pop(0) for lane in lanes))
            lanes = [lane for lane in lanes if lane]
    return out

def reviewer_active():
    rows = q(f"select count(*) from heartbeat_runs where agent_id='{REVIEWER}' and status in ('queued','running')")
    try:
        return int(rows[0][0])
    except (IndexError, ValueError):
        return None

def gate_batches_inflight():
    rows = q(f"select count(*) from issues where company_id='{CID}' and assignee_agent_id='{STEWARD}' and title like '[review-gate]%' and status in ('todo','in_progress')")
    try:
        return int(rows[0][0])
    except (IndexError, ValueError):
        return None

def ci_green(contexts, required):
    latest = {}
    for c in contexts:
        name = c.get('name') or c.get('context') or ''
        suite = c.get('checkSuite') or {}
        wf = ((suite.get('workflowRun') or {}).get('workflow') or {}).get('name') or (suite.get('app') or {}).get('slug') or ''
        key = (wf, name)
        stamp = c.get('startedAt') or c.get('createdAt') or ''
        if key not in latest or stamp >= (latest[key].get('startedAt') or latest[key].get('createdAt') or ''):
            latest[key] = c
    results = {}
    for (_, name), c in latest.items():
        if name == 'Paperclip Review':
            continue
        if 'state' in c:
            ok, done = (c['state'] == 'SUCCESS', c['state'] not in ('PENDING', 'EXPECTED'))
            if not done and name in ADVISORY_BOTS:
                continue
        else:
            if (c.get('conclusion') or '') in ('CANCELLED', 'STALE'):
                continue
            done = c.get('status') == 'COMPLETED'
            ok = done and (c.get('conclusion') or '') in GREEN
        if done and (not ok):
            return False
        results[name] = results.get(name, True) and ok
    if required:
        return all((results.get(r) is True for r in required))
    return bool(results) and all(results.values())

def required_checks(full, env, cache={}):
    if full not in cache:
        res = sh(['gh', 'api', f'repos/{full}', '--jq', '.default_branch'], env=env)
        branch = res.stdout.strip() or 'main'
        res = sh(['gh', 'api', f'repos/{full}/rules/branches/{branch}', '--jq', '[.[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context]'], env=env)
        try:
            cache[full] = {c for c in json.loads(res.stdout or '[]') if c != 'Paperclip Review'}
        except ValueError:
            cache[full] = set()
    return cache[full]

def scan_docs_lane(dry_run=False):
    empty = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'dry_run': dry_run, 'eligible': [], 'errors': [], 'scanned_prs': 0}
    try:
        import docs_lane as docs
        if docs.PAUSE.exists():
            empty['paused'] = True
            return (empty, set())
        cfg = docs.load_config()
        if not cfg['enabled']:
            return (empty, set())
        enabled = {r[0].lower() for r in q(f"select provider_resource_id from chat_endpoint_resources where endpoint_id='{EP}' and enabled and availability='available'")}
        repos = [r for r, p in cfg['repos'].items() if p['mode'] != 'off' and r.lower() in enabled]
        if not repos:
            return (empty, set())
        token_result = sh(runtime.TOKEN_COMMAND, timeout=60)
        if token_result.returncode or not token_result.stdout.strip():
            raise ValueError('GitHub read credentials unavailable')
        env = {'GH_TOKEN': token_result.stdout.strip(), 'PATH': '/usr/bin:/bin'}
        result = docs.scan(repos, env, QUERY, ci_green, dry_run=dry_run)
        if result['errors'] or docs.PAUSE.exists() or docs.load_config() != cfg:
            return (result, set())
        passed = set()
        for receipt in result['eligible']:
            if not dry_run and receipt.get('dry_run') is False and (receipt.get('mode') == 'live') and (receipt.get('eligible') is True) and (receipt.get('publication_verified') is True) and (receipt.get('publication') in ('created', 'unchanged')) and (type(receipt.get('check_id')) is int) and (receipt['check_id'] > 0) and (receipt.get('repo', '').lower() in {r.lower() for r in repos}) and (docs.policy_for(cfg, receipt['repo'])['mode'] == 'live') and (type(receipt.get('pr')) is int) and (receipt['pr'] > 0) and docs.carry.SHA_RX.fullmatch(receipt.get('head', '')) and docs.carry.SHA_RX.fullmatch(receipt.get('base', '')):
                passed.add((receipt['repo'].lower(), receipt['pr'], receipt['head']))
        return (result, passed)
    except Exception:
        empty['errors'].append({'reason': 'docs-lane unavailable; normal review retained'})
        return (empty, set())

def main():
    inventory_status = 0
    if PAUSE.exists():
        return inventory_status
    docs_passed = set()
    try:
        docs_result, docs_passed = scan_docs_lane()
        log(f'docs-lane: {len(docs_result['eligible'])} eligible; {len(docs_passed)} passed; {len(docs_result['errors'])} inventory errors')
    except Exception:
        docs_passed = set()
        try:
            log('docs-lane unavailable; existing review path unchanged')
        except OSError:
            pass
    if runtime.REVIEW_HOLD.exists():
        return inventory_status
    if PAUSE.exists():
        return inventory_status
    queued = q(f"select count(*) from heartbeat_runs where agent_id='{REVIEWER}' and status='queued'")
    queued_n = int(queued[0][0]) if queued and queued[0][0].isdigit() else 0
    focus_only = bool(queued) and queued_n >= MAX_QUEUED
    try:
        mcr_rows = q(f"select coalesce((runtime_config->'heartbeat'->>'maxConcurrentRuns')::int, 4) from agents where id='{REVIEWER}'")
        mcr = int(mcr_rows[0][0]) if mcr_rows else 4
    except (ValueError, IndexError):
        mcr = 4
    if mcr < 1:
        mcr = 1
    BATCH = min(15, max(4, 4 * mcr - queued_n))
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time()
    token = sh(runtime.TOKEN_COMMAND).stdout.strip()
    env = {'GH_TOKEN': token, 'PATH': '/usr/bin:/bin'}
    repos = [r[0] for r in q(f"select provider_resource_id from chat_endpoint_resources where endpoint_id='{EP}' and enabled and availability='available'")]
    reviewed = {(r[0].lower(), int(r[1]), r[2]) for r in q(f"select lower(g.repository), g.pull_number, g.head_sha from chat_github_reviews g left join heartbeat_runs h on h.id = g.run_id where g.endpoint_id='{EP}' and g.shadow_label is null and (g.state in ('queued','completed') or (g.state = 'running' and h.status in ('queued','running'))) and g.created_at > now() - interval '14 days'")}
    busy = {(r[0], int(r[1])) for r in q(f"select lower(g.repository), g.pull_number::text from chat_github_reviews g join heartbeat_runs h on h.id = g.run_id where g.endpoint_id='{EP}' and g.shadow_label is null and g.state='running' and h.status = 'running'") if r[1].isdigit()}
    busy |= {(r[0], int(r[1])) for r in q(f"select lower(split_part(c.external_thread_id, ':', 2)), split_part(c.external_thread_id, ':', 3) from heartbeat_runs h join chat_conversations c on c.issue_id::text = h.context_snapshot->>'issueId' where h.agent_id='{REVIEWER}' and h.status='queued' and h.context_snapshot->>'source'='chat:github' and c.external_thread_id like 'github:%:%' and (not exists (select 1 from chat_github_reviews g where g.run_id = h.id) or exists (select 1 from chat_github_reviews g where g.run_id = h.id and g.shadow_label is null))") if r[1].isdigit()}
    due = []
    cands = []
    for full in repos:
        owner, name = full.split('/', 1)
        if focus_only and name not in FOCUS_REPOS:
            continue
        try:
            from pr_controller import fetch_repository
            repo = fetch_repository(full, env, query=QUERY)
        except (ValueError, KeyError, TypeError):
            inventory_status = 1
            log(f'{full}: GraphQL inventory failed; no heads requested from partial data')
            continue
        if not repo or repo.get('isArchived'):
            continue
        for pr in repo['pullRequests']['nodes']:
            if pr['isDraft']:
                continue
            if pr.get('isCrossRepository') is not False:
                continue
            author = pr.get('author')
            if not isinstance(author, dict) or author.get('__typename') != 'User':
                continue
            login = author.get('login')
            if not isinstance(login, str) or not login.strip():
                continue
            head = pr['headRefOid']
            if (full.lower(), pr['number'], head) in docs_passed:
                continue
            if (full.lower(), pr['number'], head) in reviewed or (full.lower(), pr['number']) in busy:
                continue
            commit = (pr.get('headRef') or {}).get('target') or {}
            if commit.get('oid') != head:
                continue
            roll = commit.get('statusCheckRollup') or {}
            if (roll.get('contexts') or {}).get('pageInfo', {}).get('hasNextPage'):
                log(f'{full}#{pr['number']}: check inventory truncated; not requesting review')
                continue
            if not ci_green((roll.get('contexts') or {}).get('nodes') or [], required_checks(full, env)):
                continue
            key = f'{full}#{pr['number']}@{head}'
            if now - state.get(key, 0) < RETRY_AFTER:
                continue
            cands.append((key, pr['url'], head, full, pr['number']))
    if cands:
        try:
            cov = dedup.coverage([(full, n, head) for _, _, head, full, n in cands])
        except (ValueError, RuntimeError):
            log('dedup guard unreadable; requesting nothing this tick (fail closed)')
            return inventory_status
        for key, url, head, full, n in cands:
            st = cov.get((full.lower(), int(n), head.lower()), 'retryable')
            if st != 'retryable':
                continue
            due.append((key, url, head))
    try:
        last_pc = state.get('__findings_postcheck', 0)
        for full, n in findings.recent_round2(last_pc or now - 7200):
            if not findings.in_scope(full):
                continue
            try:
                res = findings.postcheck(full, n, record=False)
            except (ValueError, RuntimeError):
                continue
            head = res.get('head')
            seen = set(state.get('__findings_heads', []))
            if res.get('late_novel') and head and (head not in seen):
                findings.postcheck(full, n, record=True)
                seen.add(head)
                state['__findings_heads'] = sorted(seen)[-200:]
                log(f'findings-contract: {full}#{n} round {res.get('round')}: {len(res['late_novel'])} late-novel blocker(s), round-1 completeness {res.get('round1_completeness')}')
        state['__findings_postcheck'] = now
    except (ValueError, RuntimeError):
        log('findings post-check unreadable this tick (DB down); requesting continues')
    STATE.write_text(json.dumps({k: v for k, v in state.items() if k.startswith('__') or now - v < 7 * 86400}))
    if not due:
        return inventory_status
    _active = reviewer_active()
    _inflight = gate_batches_inflight()
    if _active is None or _inflight is None:
        log('reviewer load unreadable; requesting nothing this tick (fail closed)')
        return inventory_status
    _free = mcr - _active
    if not gate_admit(_inflight, _free):
        log(f'gate holds: {_inflight} batch(es) outstanding, reviewer {_active}/{mcr} (free {_free})')
        return inventory_status
    log(f'gate admits: {_inflight} batch(es) outstanding, reviewer {_active}/{mcr} (free {_free})')
    try:
        import firstpass_review as _fpp
        _prio = {(f.lower(), int(n)) for f, n in _fpp.parse_priority_prs(_fpp.load_config())}
    except (ImportError, OSError, ValueError, TypeError):
        _prio = set()
    _info = {key: (full, num) for key, _, _, full, num in cands}
    due = fair_order(due, _info, _prio)
    try:
        import firstpass_review as _fp
        _fp_cfg = _fp.load_config()
        _fp_on = _fp_cfg.get('enabled', True) and (not _fp.PAUSE.exists())
    except (ImportError, OSError, ValueError):
        _fp_on = False
        _fp_cfg = {}
    if _fp_on:
        _cands_by_key = {key: (full, num) for key, _, _, full, num in cands}
        try:
            _fp_recs = _fp.meta_records()
            _fp_meta_ok = _fp.meta_available(_fp_recs)
            _fp_l5, _fp_hp, _fp_m5, _fp_cpu = _fp.host_readings(_fp_recs)
            _fp_guards_ok = _fp.guards(_fp_l5, _fp_hp, _fp_m5, _fp_cpu)
        except (OSError, ValueError):
            _fp_meta_ok, _fp_guards_ok = (False, False)
        _kept, _held = ([], [])
        for key, url, head in due:
            _full, _num = _cands_by_key[key]
            try:
                _st = _fp.status(_full, _num, head)
            except (KeyError, TypeError, ValueError, OSError):
                _st = 'fallback'
            try:
                _dec = _fp.gate_decision(_full, _num, head, _fp_cfg, _st, _fp_meta_ok, _fp_guards_ok)
            except (AttributeError, TypeError, ValueError):
                _dec = 'request'
            if _dec == 'request':
                _kept.append((key, url, head))
            else:
                _held.append(key)
        if _held:
            log(f'first-pass hold: {len(_held)} head(s) await PASS: ' + ', '.join(_held[:5]))
        due = _kept
        if not due:
            return inventory_status
    due = due[:BATCH]
    lines = '\n'.join((f'{i + 1}. {url} (head {head[:12]})' + (' RE-TRIGGER' if key in state else '') for i, (key, url, head) in enumerate(due)))
    info_by_key = {key: (full, num) for key, _, _, full, num in cands}
    try:
        done = findings.rounds_info([info_by_key[k] for k, _, _ in due])
    except (ValueError, RuntimeError):
        done = {}
        log('findings rounds lookup failed; dispatching without round context')
    round_notes = []
    for key, url, head in due:
        full, num = info_by_key[key]
        try:
            scoped = findings.in_scope(full)
        except (OSError, ValueError):
            scoped = True
        if not scoped:
            continue
        n_done, _ = done.get((full.lower(), int(num)), (0, None))
        if not n_done:
            continue
        try:
            ctx = findings.context(full, num, head)
        except (ValueError, RuntimeError) as e:
            round_notes.append(f'- {url}: ROUND {n_done + 1} context unavailable ({e}); reviewer: verify prior findings and review only the delta per contract.')
            continue
        prev, d = (ctx['prior'], ctx['delta'])
        files = d.get('files') or []
        blockers = '; '.join((f'{b.get('path')}:{b.get('line')} {b.get('severity')} {b.get('title') or ''}'.strip() for b in (prev.get('blocking') or [])[:10])) or 'none'
        flist = ', '.join(files[:30]) + (f' (+{len(files) - 30} more)' if len(files) > 30 else '')
        round_notes.append(f'- {url}: ROUND {ctx['round']} (prev reviewed {prev['head'][:12]}, score {prev['score'] or '?'}; {len(prev.get('blocking') or [])} prior blocker(s)). Delta {d['from'][:12]}...{d['to'][:12]}: {len(files)} file(s): {flist or 'unavailable'}. Prior blockers: {blockers}. Machine-readable: {runtime.CONTEXT_DIR}/{findings.short_name(full)}-{num}-{head[:12]}.json')
    desc = f'Review once per head, only after CI green. Each PR below has a non-draft head with every CI check green and no Paperclip Review yet. Post exactly ONE comment `{runtime.REVIEW_REQUEST_TEXT}` on each, through the authorized GitHub connector (`add-issue-comment`). Skip a PR whose head changed since this list was made (the next gate tick re-lists it). A PR marked RE-TRIGGER already has a trigger comment on this exact head that started no review, so post a fresh one on it anyway. Then mark this card done. Do not post anything else.\n\n' + lines
    if round_notes:
        desc += '\n\nROUND CONTEXT (review findings contract 1.2 as amended 2026-10-15; round>=2 PRs only — verify the listed prior blockers at the new head and review ONLY the delta; late novel findings on unchanged files STAY BLOCKING, tag each `late-novel` with file:line evidence per the Reviewer instructions):\n' + '\n'.join(round_notes)
    project = q(f"select project_id from issues where identifier='{runtime.PROJECT_LOOKUP_ISSUE}'")[0][0]
    batch = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))
    body = {'title': f'[review-gate] {batch}: Request Paperclip Review on {len(due)} CI-green PR heads', 'description': desc, 'status': 'todo', 'priority': 'critical', 'projectId': project, 'assigneeAgentId': STEWARD}
    r = sh(['curl', '-sK', str(runtime.API_CURL_CONFIG), '-m', '90', '-X', 'POST', f'{runtime.API_BASE}/api/companies/{CID}/issues', '-H', 'content-type: application/json', '--data-binary', '@-'], input=json.dumps(body))
    try:
        receipt = json.loads(r.stdout)
        ident = receipt.get('identifier') if isinstance(receipt, dict) else None
        delivered = bool(ident) and receipt.get('title') == body['title'] and (receipt.get('status') in ('todo', 'in_progress')) and (receipt.get('assigneeAgentId') == STEWARD) and (desc in (receipt.get('description') or ''))
    except ValueError:
        ident, delivered = (None, False)
    if not delivered:
        log(f'batch {batch}: request delivery failed or returned a mismatched existing card ({ident}); no heads marked requested')
        return inventory_status
    if ident:
        for key, _, _ in due:
            state[key] = now
        STATE.write_text(json.dumps({k: v for k, v in state.items() if k.startswith('__') or now - v < 7 * 86400}))
        log(f'{ident}: requested review for {len(due)} CI-green heads: ' + ', '.join((k for k, _, _ in due)))
    return inventory_status

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs-lane-dry-run', action='store_true', help='inspect only the docs lane; no check publication, ledger writes, or review dispatch')
    args = parser.parse_args()
    if args.docs_lane_dry_run:
        result, _ = scan_docs_lane(dry_run=True)
        print(json.dumps(result, sort_keys=True))
        sys.exit(1 if result['errors'] else 0)
    sys.exit(main())
