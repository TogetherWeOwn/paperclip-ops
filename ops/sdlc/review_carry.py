#!/usr/bin/env python3
"""Carry a reviewed PR's exact patch onto a GitHub merge-group commit.

Only App 5191093 may publish. Credentials stay in the existing Paperclip vault;
a repository-scoped, short-lived token is captured over a private subprocess
pipe and is never persisted. No repository code is executed. The minute timer
is authoritative; optional gh-event-capture events prioritize the same scan.

Changed patches, missing authenticated source evidence and stale snapshots fail
closed. The default path requires a genuine 5/5 review. A separate default-off
docs path re-proves deterministic eligibility and never claims an LLM score.
Transport failures leave the gate unanswered and are retried next minute. No
rulesets, PR merges, deploys, or reviewer configuration are changed here.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import sdlc_lib as L

APP_ID = 5191093
CHECK = 'Paperclip Review'
PREFIX = 'paperclip-review-carry:v1:'
DOCS_SOURCE_PREFIX = 'paperclip-docs-lane:v1:'
DOCS_SOURCE_TITLE = 'Docs-only fast lane: deterministic T2 pass'
DOCS_CARRY_PREFIX = 'paperclip-docs-carry:v1:'
STATE = L.STATE_DIR / 'review-carry'
REPO_RX = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
SHA_RX = re.compile(r'[0-9a-f]{40}\Z')
QUEUE_RX = re.compile(r'refs/heads/gh-readonly-queue/(.+)/pr-([1-9][0-9]*)-([0-9a-f]{7,40})\Z')

# Native resolver enforces the active endpoint and audits secret access. Keys
# never leave the container. Verify the App with GitHub before minting a token.
# Locate tsx through the package store rather than pinning a runtime version.
MINT_JS = r"""
import {loadConfig} from './src/config.ts';
import {createDb,closeRegisteredClients} from '@paperclipai/db';
import {githubBotCredentials} from './src/services/chat-github-client.ts';
const input=JSON.parse(process.env.REVIEW_CARRY_INPUT);
const cfg=loadConfig();
if(!cfg.databaseUrl) throw new Error('Database configuration unavailable');
const db=createDb(cfg.databaseUrl);
async function api(jwt,path,body) {
  const r=await fetch('https://api.github.com'+path, {
    method:body?'POST':'GET',redirect:'error',signal:AbortSignal.timeout(20000),
    headers:{Authorization:'Bearer '+jwt,Accept:'application/vnd.github+json',
      'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'},
    ...(body?{body:JSON.stringify(body)}:{})});
  if(!r.ok) throw new Error('GitHub credential preflight HTTP '+r.status);
  return r.json();
}
try {
  const c=await githubBotCredentials(db,input.company,input.endpoint);
  if(c.credentials.appId!==String(input.app)) throw new Error('Wrong vaulted App');
  const app=await api(c.appJwt,'/app');
  if(app.id!==input.app || app.permissions.checks!=='write') throw new Error('App identity/permission mismatch');
  const installation=await api(c.appJwt,'/app/installations/'+c.credentials.installationId);
  const [owner,name]=input.repo.split('/');
  if(installation.app_id!==input.app || installation.suspended_at ||
     installation.account.login.toLowerCase()!==owner.toLowerCase()) throw new Error('Wrong/suspended installation');
  const minted=await api(c.appJwt,'/app/installations/'+c.credentials.installationId+'/access_tokens',{
    repositories:[name],permissions:{checks:'write',contents:'read',pull_requests:'read',metadata:'read'}});
  if(minted.permissions.checks!=='write') throw new Error('Checks write unavailable');
  console.log(JSON.stringify({token:minted.token,app_id:app.id,installation_id:installation.id,expires_at:minted.expires_at}));
} catch(e) { console.error('Reviewer credential preflight failed'); process.exitCode=1; }
finally {await closeRegisteredClients(cfg.databaseUrl);}
"""
MINT_BOOTSTRAP = r"""import {readdirSync} from 'node:fs';
const stores=readdirSync('/app/node_modules/.pnpm').filter(n=>n.startsWith('tsx@')).sort();
if(!stores.length) process.exit(1);
const {spawnSync}=await import('node:child_process');
const r=spawnSync(process.execPath,['--import','/app/node_modules/.pnpm/'+stores.at(-1)+'/node_modules/tsx/dist/loader.mjs','--input-type=module','-e',process.env.REVIEW_CARRY_JS],{stdio:'inherit'});
process.exit(r.status??1);
"""


class Unsafe(Exception):
    """An evidence/identity boundary failed; never carry."""


class APIError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(token, path, body=None, method=None):
    if not path.startswith('/') or path.startswith('//'):
        raise Unsafe('invalid GitHub API path')
    req = urllib.request.Request('https://api.github.com' + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ('POST' if body is not None else 'GET'),
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28',
                 'User-Agent': 'paperclip-review-carry'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=25) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        raise APIError(f'GitHub HTTP {exc.code}') from None
    except (OSError, ValueError) as exc:
        raise APIError('GitHub transport/JSON error') from None


def pages(token, path, key=None):
    result = []
    for page in range(1, 101):
        sep = '&' if '?' in path else '?'
        response = api(token, f'{path}{sep}per_page=100&page={page}')
        rows = response.get(key) if key else response
        if not isinstance(rows, list):
            raise APIError('Unexpected paginated GitHub response')
        result.extend(rows)
        if len(rows) < 100:
            return result
    raise Unsafe('pagination bound exceeded')


def mint(repo):
    if not REPO_RX.fullmatch(repo):
        raise Unsafe('invalid repository')
    cfg = L.load_cfg().get('review_carry', {})
    request = {'app': APP_ID, 'repo': repo, 'company': L.CID,
               'endpoint': cfg.get('endpoint_id', '213544ef-08cb-4f1d-82d4-bf9bf3dd3ad6')}
    env = dict(os.environ, REVIEW_CARRY_INPUT=json.dumps(request), REVIEW_CARRY_JS=MINT_JS)
    result = subprocess.run(['docker', 'exec', '-e', 'REVIEW_CARRY_INPUT', '-e', 'REVIEW_CARRY_JS',
        '-w', '/app/server', 'paperclip', 'node', '--input-type=module', '-e', MINT_BOOTSTRAP],
        capture_output=True, text=True, env=env, timeout=75)
    # Neither stderr nor stdout may be logged on parse failure (they may contain credentials).
    if result.returncode:
        raise Unsafe('reviewer App credential preflight failed; no fallback credential permitted')
    try:
        data = json.loads(result.stdout)
        if data['app_id'] != APP_ID or not data['token']:
            raise ValueError()
        return data['token']
    except (ValueError, KeyError):
        raise Unsafe('reviewer App credential response invalid') from None


def normalized_patch(patch):
    """File-by-file bytes, ignoring context, blob IDs, and hunk line offsets.

    Use git --no-renames --binary --full-index --unified=0. Preserve changed-line
    whitespace, file names, additions/deletions, modes, binary payloads and EOF
    markers. Deliberately do NOT use patch-id (it ignores significant whitespace).
    """
    files = {}
    current = None
    # Git patch records are LF-delimited. bytes.splitlines also splits embedded
    # CR bytes inside changed lines, potentially hiding changed payload bytes.
    lines = patch.split(b'\n')
    for position, line in enumerate(lines):
        if position < len(lines) - 1:
            line += b'\n'
        if line.startswith(b'diff --git '):
            current = line.rstrip(b'\n')
            # Git emits two records for a file-type transition (delete/add).
            # Preserve their order and boundaries rather than dropping either.
            files.setdefault(current, []).append(b'\x00file-record\x00')
        elif current is None:
            if line.strip():
                raise Unsafe('unrecognized patch preamble')
        elif line.startswith(b'index '):
            index = re.fullmatch(rb'index [0-9a-f]+\.\.[0-9a-f]+(?: ([0-7]{6}))?\n?', line)
            if not index:
                raise Unsafe('invalid Git index header')
            if index.group(1):
                files[current].append(b'file mode ' + index.group(1) + b'\n')
        elif line.startswith((b'@@ ', b' ')):
            continue
        else:
            files[current].append(line)
    return {name: b''.join(lines) for name, lines in files.items()}


def digest(patch):
    h = hashlib.sha256()
    for name, body in sorted(normalized_patch(patch).items()):
        for value in (name, body):
            h.update(len(value).to_bytes(8, 'big'))
            h.update(value)
    return h.hexdigest()


class GitCache:
    def __init__(self, repo, token, root=STATE / 'repos'):
        if not REPO_RX.fullmatch(repo):
            raise Unsafe('invalid repository')
        self.path = Path(root) / (repo.replace('/', '--') + '.git')
        self.url = 'https://github.com/' + repo + '.git'
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_COUNT='1',
                        GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                        GIT_CONFIG_VALUE_0='Authorization: Basic ' +
                        base64.b64encode(('x-access-token:' + token).encode()).decode())
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.run('init', '--bare', str(self.path), outside=True)

    def run(self, *args, outside=False):
        cmd = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'protocol.file.allow=never',
               '-c', 'protocol.ext.allow=never']
        if not outside:
            cmd += ['--git-dir=' + str(self.path)]
        result = subprocess.run(cmd + list(args), env=self.env, capture_output=True, timeout=120)
        if result.returncode:
            raise APIError('git operation failed: ' + args[0])
        return result.stdout

    def fetch(self, *shas):
        missing = []
        for sha in sorted(set(shas)):
            if not SHA_RX.fullmatch(sha):
                raise Unsafe('invalid commit SHA')
            try:
                self.run('cat-file', '-e', sha + '^{commit}')
            except APIError:
                missing.append(sha)
        if missing:
            self.run('fetch', '--no-tags', '--no-recurse-submodules', self.url, *missing)

    def parent(self, sha):
        words = self.run('rev-list', '--parents', '-n', '1', sha).decode().split()
        if len(words) < 2:
            raise Unsafe('merge-group commit has no parent')
        return words[1]

    def patch(self, before, after):
        return self.run('diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                        '--binary', '--full-index', '--unified=0', '--no-color',
                        '--src-prefix=a/', '--dst-prefix=b/', before, after, '--')

    def compare(self, head, base, group):
        self.fetch(head, base, group)
        parent = self.parent(group)
        bases = self.run('merge-base', '--all', head, base).decode().split()
        if len(bases) != 1:
            raise Unsafe('ambiguous or missing PR merge base')
        pr_patch = self.patch(bases[0], head)
        mg_patch = self.patch(parent, group)
        return {'equal': normalized_patch(pr_patch) == normalized_patch(mg_patch),
                'pr_patch_sha256': digest(pr_patch), 'group_patch_sha256': digest(mg_patch),
                'pr_merge_base': bases[0], 'group_parent': parent,
                'files': len(normalized_patch(pr_patch))}


def latest_review(checks, head, number):
    matches = [c for c in checks if c.get('name') == CHECK and
               c.get('app', {}).get('id') == APP_ID and c.get('head_sha') == head]
    # Bind to the queued PR before selecting latest: the check-runs API lists
    # runs for the head ref, so a newer check for another PR sharing this
    # head must not displace this PR's own latest check.
    bound = [c for c in matches
             if str(c.get('external_id') or '').endswith(f':{number}:{head}')]
    if not bound:
        return None
    # A newer queued/failed assessment invalidates an older successful one.
    latest = max(bound, key=lambda c: int(c['id']))
    external_id = str(latest.get('external_id') or '')
    output = latest.get('output') or {}
    score = output.get('title')
    if score is None or (isinstance(score, str) and score.strip() == ''):
        summary = output.get('summary')
        first = summary.split('\n', 1)[0] if isinstance(summary, str) else None
        # CRLF summaries end the first line with \r; drop it so '5/5\r\n…'
        # matches exactly like '5/5\n…' (mirrors github.js /\r?\n/ split).
        score = first.removesuffix('\r') if isinstance(first, str) else None
    if (latest.get('status') != 'completed' or latest.get('conclusion') != 'success'
            or not isinstance(score, str) or score != '5/5'
            or external_id.startswith(PREFIX)):
        return None
    return latest


def latest_docs_review(checks, repo, number, head, base, token):
    """Separate authenticated provenance, never a forged 5/5 or a ledger lookup."""
    matches = [c for c in checks if c.get('name') == CHECK and
               c.get('app', {}).get('id') == APP_ID and c.get('head_sha') == head]
    if not matches:
        return None
    latest = max(matches, key=lambda c: int(c['id']))
    if (latest.get('status') != 'completed' or latest.get('conclusion') != 'success'
            or latest.get('output', {}).get('title') != DOCS_SOURCE_TITLE
            or latest.get('external_id') != f'{DOCS_SOURCE_PREFIX}{number}:{head}:{base}'):
        return None
    # Import only for an authenticated deterministic source. Ordinary review
    # carry remains independent of this optional feature and its configuration.
    try:
        module_dir = str(Path(__file__).resolve().parent.parent / 'productivity')
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)
        import docs_lane
        docs_lane.verify_carry(repo, number, head, base, token)
    except Exception:
        # Config/transport/helper details can contain credentials; never echo.
        raise Unsafe('deterministic provenance could not be reverified; normal review required') from None
    return latest


def checks_for(token, repo, sha):
    return pages(token, f'/repos/{repo}/commits/{sha}/check-runs?check_name=Paperclip%20Review&filter=all', 'check_runs')


def queue_refs(token, repo):
    return pages(token, f'/repos/{repo}/git/matching-refs/heads/gh-readonly-queue/')


def ref_sha(token, repo, branch):
    result = api(token, f'/repos/{repo}/git/ref/' + urllib.parse.quote(branch.removeprefix('refs/'), safe='/'))
    return result['object']['sha']


def decide(review, comparison):
    if not review:
        return 'failure', 're-review required: current PR head has no latest successful 5/5 review'
    if not comparison['equal']:
        return 'failure', 're-review required: diff changed in merge group'
    return 'success', '5/5 carried: identical reviewed patch'


def process_group(token, repo, ref, dry_run=False):
    match = QUEUE_RX.fullmatch(ref.get('ref', ''))
    if not match:
        raise Unsafe('unrecognized merge-group branch')
    base_name, number, _ = match.groups()
    group = ref['object']['sha']
    if not SHA_RX.fullmatch(group):
        raise Unsafe('invalid merge-group SHA')
    pr = api(token, f'/repos/{repo}/pulls/{number}')
    if (pr['state'] != 'open' or pr['base']['ref'] != base_name or
            pr['base']['repo']['full_name'].lower() != repo.lower()):
        raise Unsafe('queue branch no longer corresponds to an open PR/base')
    head, base = pr['head']['sha'], pr['base']['sha']
    source_checks = checks_for(token, repo, head)
    review = latest_review(source_checks, head, int(number))
    deterministic = False
    if review is None:
        review = latest_docs_review(source_checks, repo, int(number), head, base, token)
        deterministic = review is not None
    comparison = GitCache(repo, token).compare(head, base, group) if review else None
    if deterministic:
        conclusion, title = (('success', 'Docs-only T2 carried: identical deterministic patch')
                             if comparison['equal'] else
                             ('failure', 'Normal review required: deterministic docs patch changed in merge group'))
    else:
        conclusion, title = decide(review, comparison)
    receipt = {'at': L.utcnow(), 'repo': repo, 'pr': int(number), 'group': group,
               'head': head, 'base': base, 'review_check_id': review['id'] if review else None,
               'score': '5/5' if review and not deterministic else None, 'conclusion': conclusion,
               'source_kind': 'deterministic-docs' if deterministic else 'review',
               'reason': title, 'comparison': comparison, 'dry_run': dry_run}
    if dry_run:
        return receipt
    # Re-read immediately before publishing. Never trust a branch name/event's
    # old PR head or reuse a cached successful assessment across a new head.
    fresh = api(token, f'/repos/{repo}/pulls/{number}')
    if (fresh['state'] != 'open' or fresh['head']['sha'] != head or
            fresh['base']['sha'] != base or fresh['base']['ref'] != base_name or
            ref_sha(token, repo, ref['ref']) != group):
        raise Unsafe('PR/base/merge-group moved during verification; retry next tick')
    fresh_checks = checks_for(token, repo, head)
    fresh_review = (latest_docs_review(fresh_checks, repo, int(number), head, base, token)
                    if deterministic else latest_review(fresh_checks, head, int(number)))
    if (fresh_review or {}).get('id') != (review or {}).get('id'):
        raise Unsafe('review changed during verification; retry next tick')
    if deterministic and ref_sha(token, repo, ref['ref']) != group:
        raise Unsafe('merge-group moved during deterministic revalidation; retry next tick')
    external_id = (DOCS_CARRY_PREFIX if deterministic else PREFIX) + group
    existing = [c for c in checks_for(token, repo, group) if c.get('app', {}).get('id') == APP_ID
                and c.get('external_id') == external_id]
    evidence = ('evidence **deterministic docs-only T2 (not an LLM review)**' if deterministic
                else f'score **{receipt["score"] or "not eligible"}**')
    summary = (f'Carried source: [{repo}#{number}](https://github.com/{repo}/pull/{number}), '
               f'current PR head `{head}`, {evidence}.\n\n'
               f'Source App: `{APP_ID}`; source check: `{receipt["review_check_id"]}`.\n\n'
               f'Merge-group SHA: `{group}`. {title}.\n\n'
               f'Comparison evidence: `{json.dumps(comparison, sort_keys=True)}`.\n\n'
               'This is deterministic patch equivalence, not a new code review. All other required checks remain required.')
    payload = {'name': CHECK, 'head_sha': group, 'external_id': external_id,
               'status': 'completed', 'conclusion': conclusion, 'completed_at': L.utcnow(),
               'details_url': f'https://github.com/{repo}/pull/{number}',
               'output': {'title': title, 'summary': summary}}
    latest = max(existing, key=lambda c: int(c['id'])) if existing else None
    if latest and latest.get('status') == 'completed' and latest.get('conclusion') == conclusion and latest.get('output', {}).get('summary') == summary:
        receipt['check_id'] = latest['id']
        receipt['publication'] = 'unchanged'
        return receipt
    if latest:
        payload.pop('head_sha')
        saved = api(token, f'/repos/{repo}/check-runs/{latest["id"]}', payload, 'PATCH')
    else:
        saved = api(token, f'/repos/{repo}/check-runs', payload)
    if (saved.get('app', {}).get('id') != APP_ID or saved.get('head_sha') != group or
            saved.get('conclusion') != conclusion or saved.get('name') != CHECK):
        raise Unsafe('published check receipt failed verification')
    receipt['check_id'] = saved['id']
    receipt['publication'] = 'updated' if latest else 'created'
    L.log('review-carry', f'{repo}#{number} group={group} head={head} score={receipt["score"]} '
          f'{conclusion} check={saved["id"]}. UNDO: PATCH this same App-owned check to failure; never alter rulesets.')
    return receipt


def event_repos():
    """Consume the existing capture query API as hints, never as review evidence."""
    path = Path.home() / 'secure-drop/gh-event-capture.env'
    if not path.exists():
        return set(), 'absent'
    values = {}
    try:
        for line in path.read_text().splitlines():
            line = line.removeprefix('export ').strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, raw = line.split('=', 1)
            parts = shlex.split(raw)
            if len(parts) == 1:
                values[key.strip()] = parts[0]
        url = values.get('GH_CAPTURE_URL')
        token = values.get('GH_CAPTURE_TOKEN') or values.get('QUERY_TOKEN')
        if not url:
            url_path = Path.home() / 'secure-drop/gh-event-capture.url'
            url = url_path.read_text().strip() if url_path.exists() else None
    except (OSError, ValueError):
        # Hints only prioritize the authoritative timer scan; a malformed or
        # unreadable hint file degrades to no hints instead of aborting the
        # whole reconciliation run. (UnicodeDecodeError is a ValueError.)
        return set(), 'invalid-config'
    if not url or not token:
        return set(), 'unconfigured'
    if urllib.parse.urlparse(url).scheme != 'https':
        return set(), 'invalid-url'
    # Existing capture installations may predate the merge_group subscription.
    # check_suite also wakes discovery for queue builds without changing App
    # subscriptions; authoritative branch scanning covers dropped/old events.
    req = urllib.request.Request(url.rstrip('/') + '/events?limit=100',
                                 headers={'Authorization': 'Bearer ' + token,
                                          'User-Agent': 'paperclip-review-carry/1.0 (+https://github.com/TogetherWeOwn)'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as res:
            data = json.load(res)
        rows = data if isinstance(data, list) else data.get('events', data.get('data', []))
        names = set()
        for row in rows:
            if row.get('event') not in ('merge_group', 'check_suite'):
                continue
            repo = row.get('repo') or row.get('repository') or row.get('repo_full_name')
            if isinstance(repo, dict):
                repo = repo.get('full_name')
            if isinstance(repo, str) and REPO_RX.fullmatch(repo):
                names.add(repo)
        return names, 'ok'
    except (OSError, ValueError, TypeError, AttributeError):
        return set(), 'unavailable'


def reconcile(repo_filter=None, dry_run=False):
    env = L.rr.gh_env()
    repos = L.discover_repos(env)
    if not repos:
        raise APIError('dynamic repository discovery returned no repositories')
    if repo_filter:
        repos = [r for r in repos if r.lower() == repo_filter.lower()]
        if not repos:
            raise Unsafe('repository is not in SDLC discovery')
    hints, event_status = event_repos()
    repos.sort(key=lambda r: (r.lower() not in {x.lower() for x in hints}, r.lower()))
    receipts, errors = [], []
    for repo in repos:
        try:
            refs = queue_refs(env['GH_TOKEN'], repo)
            if not refs:
                continue
            token = mint(repo)
            for ref in refs:
                try:
                    receipts.append(process_group(token, repo, ref, dry_run))
                except (Unsafe, APIError, subprocess.TimeoutExpired) as exc:
                    errors.append({'repo': repo, 'branch': ref.get('ref'), 'error': str(exc) if not isinstance(exc, subprocess.TimeoutExpired) else 'operation timed out'})
        except (Unsafe, APIError, subprocess.TimeoutExpired) as exc:
            errors.append({'repo': repo, 'error': str(exc) if not isinstance(exc, subprocess.TimeoutExpired) else 'operation timed out'})
    result = {'at': L.utcnow(), 'repos_scanned': len(repos), 'capture': event_status,
              'capture_repo_hints': sorted(hints), 'receipts': receipts, 'errors': errors}
    L.save_state('review-carry/latest.json', result)
    with (STATE / 'receipts.jsonl').open('a') as f:
        for receipt in receipts:
            f.write(json.dumps(receipt, sort_keys=True) + '\n')
    print(json.dumps(result, sort_keys=True), flush=True)
    return bool(errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', help='narrow the dynamically discovered set (never expands it)')
    parser.add_argument('--dry-run', action='store_true', help='verify without publishing checks')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            return int(reconcile(args.repo, args.dry_run))
        except (Unsafe, APIError, subprocess.TimeoutExpired):
            print('review-carry: reconciliation failed closed; inspect credentials/API availability', file=sys.stderr)
            return 1


if __name__ == '__main__':
    sys.exit(main())
