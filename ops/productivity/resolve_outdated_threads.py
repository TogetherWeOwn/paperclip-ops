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

Incomplete evidence (fail closed, never 5/5-by-omission): a search page with
incomplete_results, a MAX_PRS bound exit, or a failed/malformed search page
degrades the WHOLE tick to a single incomplete_search skip row with zero
mutations and zero per-PR detail reads. Check runs page explicitly (page=N, at
most MAX_CHECK_RUN_PAGES) and reconcile collected runs against the first page's
total_count; a missing/mismatched total, a failed page, or bound exhaustion
skips THAT PR as incomplete_check_runs while siblings continue.

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
MAX_CHECK_RUN_PAGES = 10
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
    if score is None or (isinstance(score, str) and score.strip() == ''):
        summary = output.get('summary')
        first = summary.split('\n', 1)[0] if isinstance(summary, str) else None
        # CRLF summaries end the first line with \r; drop it so '5/5\r\n…'
        # matches exactly like '5/5\n…' (mirrors github.js /\r?\n/ split).
        score = first.removesuffix('\r') if isinstance(first, str) else None
    if not isinstance(score, str) or score != '5/5':
        return False, 'incomplete_score'
    return True, 'ok'


def head_is_5of5(check_runs, head):
    """Latest trusted-App Paperclip Review run on `head` (by started_at) is completed/success/5/5."""
    ok, _ = review_evidence(check_runs, head)
    return ok


def open_prs(e):
    """Open org PRs with completeness. Returns (prs, complete).

    complete is False when a search page lacks explicit incomplete_results=False,
    when the MAX_PRS bound stops pagination (the bound governs: the set may be truncated
    however a further probe would read), or when a page fails, times out, or is
    malformed (non-list/missing items, malformed repository_url).
    The caller must degrade the whole tick on incomplete: an incomplete_search
    skip row with zero mutations and zero per-PR detail reads.
    """
    prs, page = [], 1
    try:
        while len(prs) < MAX_PRS:
            d = json.loads(gh(e, 'api', f'search/issues?q=org:{ORG}+is:pr+is:open&per_page=100&page={page}'))
            if d.get('incomplete_results') is not False:
                return [], False
            items = d.get('items')
            if not isinstance(items, list):
                return [], False
            for it in items:
                url_parts = it['repository_url'].split('/repos/', 1)
                repo = url_parts[1] if len(url_parts) == 2 else ''
                repo_parts = repo.split('/')
                if len(repo_parts) != 2 or not all(repo_parts):
                    return [], False
                prs.append((repo, it['number']))
            if len(items) < 100:
                return prs, True
            page += 1
    except (RuntimeError, ValueError, KeyError, AttributeError, TypeError, IndexError,
            subprocess.TimeoutExpired):
        return [], False
    return [], False


def check_run_shape_valid(run):
    """Validate verdict inputs; absent identity/head retain their specific refusal reasons."""
    if not isinstance(run, dict) or not isinstance(run.get('name'), str) or not run['name']:
        return False
    if not isinstance(run.get('started_at'), str):
        return False
    for field in ('head_sha', 'status', 'conclusion'):
        if run.get(field) is not None and not isinstance(run[field], str):
            return False
    app = run.get('app')
    if app is not None:
        if not isinstance(app, dict):
            return False
        if app.get('id') is not None and type(app['id']) is not int:
            return False
    output = run.get('output')
    if output is not None:
        if not isinstance(output, dict):
            return False
        if 'title' in output and not isinstance(output['title'], str):
            return False
    return True


def check_runs_for_head(e, repo, head):
    """Check runs for one head with reconciliation. Returns (runs, complete).

    Pages explicitly (page=N) up to MAX_CHECK_RUN_PAGES and reconciles collected
    runs against the first page's total_count. Incomplete (the caller skips that
    PR as incomplete_check_runs, never 5/5-by-omission) on: a missing/non-integer
    total_count, a non-list check_runs batch, malformed verdict inputs in a batch,
    collected != total_count, a failed/malformed page, a gh timeout, a disagreeing
    later total_count, or bound exhaustion without reconciliation. A later page
    that omits total_count is tolerated: the first page's total governs.
    """
    runs, page, total = [], 1, None
    try:
        while page <= MAX_CHECK_RUN_PAGES:
            d = json.loads(gh(e, 'api', f'repos/{repo}/commits/{head}/check-runs?per_page=100&page={page}'))
            batch = d.get('check_runs')
            if not isinstance(batch, list) or any(not check_run_shape_valid(x) for x in batch):
                return [], False
            if page == 1:
                total = d.get('total_count')
                if type(total) is not int or total < 0:
                    return [], False
            elif 'total_count' in d and (type(d['total_count']) is not int or d['total_count'] != total):
                return [], False
            runs.extend(batch)
            if len(batch) < 100 or len(runs) >= total:
                break
            page += 1
    except (RuntimeError, ValueError, KeyError, AttributeError, TypeError, IndexError,
            subprocess.TimeoutExpired):
        return [], False
    return runs, len(runs) == total


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
    prs, complete = open_prs(e)
    if not complete:
        report = [{'pr': '*', 'outdated': 0, 'skipped': 'incomplete_search'}]
        print(json.dumps(report, indent=1))
        return report
    for repo, num in prs:
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
        checks, complete = check_runs_for_head(e, repo, head)
        if not complete:
            report.append({'pr': f'{repo}#{num}', 'outdated': len(cands), 'skipped': 'incomplete_check_runs'})
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
