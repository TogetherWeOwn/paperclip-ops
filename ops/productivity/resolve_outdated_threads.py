#!/usr/bin/env python3
"""Resolve OUTDATED Paperclip Review threads on open TogetherWeOwn PRs (owner ruling 2026-10-06, item 7).

The org rulesets require review-thread resolution, so outdated reviewer threads keep blocking PRs whose current head
already scored 5/5 (seen on ops-tooling #737/#752/#753/#764). A thread is resolved only when ALL hold:
  - the thread is unresolved AND isOutdated (its lines changed in a later push);
  - its first comment is by togetherweown-reviewer[bot];
  - the latest "Paperclip Review" check run on the PR's CURRENT head concluded success with score "5/5".
It replies first ("Outdated: superseded by head <sha>, which scored 5/5."), then resolves. Never touches threads that
are not outdated or by anyone else. Cap MAX_RESOLVE per tick. Token: ./gh_token.sh App token via env()
(bounded TOKEN_TIMEOUT, fail closed on timeout/exit/empty), never on argv, never printed.
Pause: touch ~/.local/state/resolve-outdated-threads/PAUSE. Usage: resolve_outdated_threads.py [--dry-run]
Undo: unresolve the thread in the GitHub UI (the reply stays as a record).

Review evidence (case_0a5a55e4): a check NAME is claimable by any App with write access, so the name alone
authenticates nothing. The resolver accepts only the latest check run that carries ALL of:
  - name "Paperclip Review" from the trusted review App (id 5191093, slug togetherweown-reviewer);
  - head_sha equal to the PR's current headRefOid (the expected head);
  - status "completed", conclusion "success", output title "5/5" (or first summary line
    only when the title is missing or empty; whitespace is stripped, as before).
Anything else refuses with a diagnostic and zero mutations: missing_head (no expected head to bind to),
missing_identity (same-name check with no App id), no_trusted_evidence (no same-name check from the
trusted App), wrong_head (trusted checks exist but none on the expected head), or incomplete_* (latest
trusted on-head run is not completed/success/5/5). A newer success from any other App never overrides a
trusted failure: only trusted-App runs are candidates.

Token helper (bounded at TOKEN_TIMEOUT, 30s): TimeoutExpired, a non-zero exit,
a missing helper, or empty stdout refuses with a single "token helper: ..."
line and exits non-zero with zero gh calls and zero mutations, so gh can never
fall back to an ambient credential. The token reaches gh only through the
child-process env, never on argv, and is never printed.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/home/ubuntu/paperclip-enterprise-company')
ORG = 'TogetherWeOwn'
REVIEWER_LOGINS = {'togetherweown-reviewer', 'togetherweown-reviewer[bot]'}
CHECK_NAME = 'Paperclip Review'
# Trusted review App: the App id is what authenticates who produced a check run.
# Live-verified 2026-10-10 via GET /repos/{paperclip-ops,two-bot-next,two-web-next}/rules/branches/main:
# required_status_checks context "Paperclip Review" is pinned to integration_id 5191093 in each repo
# (org ruleset on ops-tooling; repo+org rulesets on the other two), with slug "togetherweown-reviewer"
# on live check runs. A prior policy row is not proof: re-check the live ruleset if this constant changes.
TRUSTED_REVIEW_APP_ID = 5191093
TRUSTED_REVIEW_APP_SLUG = 'togetherweown-reviewer'
MAX_RESOLVE = 30
MAX_PRS = 300
TOKEN_TIMEOUT = 30
STATE_DIR = Path.home() / '.local/state/resolve-outdated-threads'
PAUSE = STATE_DIR / 'PAUSE'
LEDGER = ROOT / 'ops/productivity/resolve-outdated-threads.jsonl'
ACTIONS_LOG = ROOT / 'ops/worker-vps/operator-actions.log'

QUERY = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
 headRefOid reviewThreads(first:100){nodes{id isResolved isOutdated
  comments(first:1){nodes{databaseId author{login}}}}}}}}"""


class TokenHelperError(RuntimeError):
    """Token helper failed: the caller must refuse with zero gh calls (fail closed)."""


def env():
    """Mint the App token. Fail closed: TimeoutExpired, a non-zero exit, a
    missing helper, or empty stdout raises TokenHelperError whose message is
    the single refusal line. The token travels only in the gh child-process
    env, never on argv, and is never printed or logged."""
    cmd = [str(ROOT / 'gh_token.sh'), 'token']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TOKEN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise TokenHelperError('token helper: timeout')
    except OSError:
        raise TokenHelperError('token helper: exit 127')
    if r.returncode:
        raise TokenHelperError(f'token helper: exit {r.returncode}')
    tok = (r.stdout or '').strip()
    if not tok:
        raise TokenHelperError('token helper: empty')
    return dict(os.environ, GH_TOKEN=tok)


def gh(e, *args, input_=None):
    r = subprocess.run(['gh', *args], capture_output=True, text=True, env=e, input=input_, timeout=60)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    return r.stdout


def candidates(threads):
    """Pure filter: unresolved, outdated, first comment by the reviewer bot."""
    out = []
    for t in threads:
        c = ((t.get('comments') or {}).get('nodes') or [{}])[0]
        login = ((c.get('author') or {}).get('login') or '')
        if not t.get('isResolved') and t.get('isOutdated') and login in REVIEWER_LOGINS and c.get('databaseId'):
            out.append({'id': t['id'], 'comment_id': c['databaseId']})
    return out


def trusted_review_runs(check_runs):
    """Same-name check runs produced by the trusted review App. Anything else is a claim, not evidence."""
    out = []
    for c in check_runs or []:
        if c.get('name') != CHECK_NAME:
            continue
        if ((c.get('app') or {}).get('id') != TRUSTED_REVIEW_APP_ID):
            continue
        out.append(c)
    return out


def review_evidence(check_runs, head):
    """Authenticate the 5/5 claim for the expected head. Returns (ok, reason).

    ok is True only for the latest trusted-App run on `head` with status completed,
    conclusion success and score 5/5 in the title or, when missing/empty, first summary
    line. Every other shape refuses with a machine-readable reason and the caller
    must make zero mutations.
    """
    if not head:
        return False, 'missing_head'
    runs = check_runs or []
    trusted = trusted_review_runs(runs)
    if not trusted:
        named = [c for c in runs if c.get('name') == CHECK_NAME]
        if named and all(((c.get('app') or {}).get('id') is None) for c in named):
            return False, 'missing_identity'
        return False, 'no_trusted_evidence'
    on_head = [c for c in trusted if c.get('head_sha') == head]
    if not on_head:
        if any(not c.get('head_sha') for c in trusted):
            return False, 'missing_head'
        return False, 'wrong_head'
    latest = max(on_head, key=lambda c: c.get('started_at') or '')
    if latest.get('status') != 'completed':
        return False, 'incomplete_status'
    if latest.get('conclusion') != 'success':
        return False, 'incomplete_conclusion'
    output = latest.get('output') or {}
    score = output.get('title')
    if score is None or score == '':
        summary = output.get('summary')
        score = summary.split('\n', 1)[0] if isinstance(summary, str) else None
    if not isinstance(score, str) or score.strip() != '5/5':
        return False, 'incomplete_score'
    return True, 'ok'


def head_is_5of5(check_runs, head):
    """Latest trusted-App Paperclip Review run on `head` (by started_at) is completed/success/5/5."""
    ok, _ = review_evidence(check_runs, head)
    return ok


def open_prs(e):
    prs, page = [], 1
    while len(prs) < MAX_PRS:
        d = json.loads(gh(e, 'api', f'search/issues?q=org:{ORG}+is:pr+is:open&per_page=100&page={page}'))
        items = d.get('items') or []
        for it in items:
            repo = it['repository_url'].split('/repos/')[1]
            prs.append((repo, it['number']))
        if len(items) < 100:
            break
        page += 1
    return prs


def log(line, rec):
    with ACTIONS_LOG.open('a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} resolve-outdated-threads: {line}\n")
    with LEDGER.open('a') as f:
        f.write(json.dumps(rec) + '\n')


def run(dry=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if PAUSE.exists():
        print('paused')
        return
    try:
        e = env()
    except TokenHelperError as err:
        print(err)
        raise SystemExit(1)
    done, report = 0, []
    for repo, num in open_prs(e):
        if done >= MAX_RESOLVE:
            break
        owner, name = repo.split('/')
        try:
            d = json.loads(gh(e, 'api', 'graphql', '-f', f'query={QUERY}', '-F', f'o={owner}', '-F', f'r={name}',
                              '-F', f'n={num}'))
        except RuntimeError:
            continue
        pr = (((d.get('data') or {}).get('repository') or {}).get('pullRequest') or {})
        cands = candidates(((pr.get('reviewThreads') or {}).get('nodes')) or [])
        if not cands:
            continue
        head = pr.get('headRefOid') or ''
        if not head:
            report.append({'pr': f'{repo}#{num}', 'outdated': len(cands), 'skipped': 'missing_head'})
            continue
        try:
            checks = json.loads(gh(e, 'api', f'repos/{repo}/commits/{head}/check-runs?per_page=100'))['check_runs']
        except (RuntimeError, KeyError, ValueError):
            continue
        ok, reason = review_evidence(checks, head)
        if not ok:
            report.append({'pr': f'{repo}#{num}', 'outdated': len(cands), 'skipped': reason})
            continue
        resolved = 0
        for c in cands:
            if done >= MAX_RESOLVE:
                break
            if not dry:
                try:
                    gh(e, 'api', '-X', 'POST', f'repos/{repo}/pulls/{num}/comments/{c["comment_id"]}/replies',
                       '-f', f'body=Outdated: superseded by head {head[:10]}, which scored 5/5.')
                    gh(e, 'api', 'graphql', '-f',
                       f'query=mutation{{resolveReviewThread(input:{{threadId:"{c["id"]}"}}){{thread{{isResolved}}}}}}')
                except RuntimeError as err:
                    report.append({'pr': f'{repo}#{num}', 'thread': c['id'], 'error': str(err)[:120]})
                    continue
            resolved += 1
            done += 1
        item = {'pr': f'{repo}#{num}', 'head': head[:10], 'resolved': resolved, 'dry': dry}
        report.append(item)
        if resolved and not dry:
            log(f"{repo}#{num}: replied+resolved {resolved} outdated reviewer thread(s); head {head[:10]} is 5/5. "
                f"UNDO: unresolve in GitHub.", {**item, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    print(json.dumps(report, indent=1))
    return report


if __name__ == '__main__':
    run(dry='--dry-run' in sys.argv)
