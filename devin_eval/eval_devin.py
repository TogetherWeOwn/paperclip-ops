#!/usr/bin/env python3
"""Read-only Devin Review measurement. No scheduler, posts, or DB access.

Collect (live, GET only):  python3 -I eval_devin.py collect --workdir W --round r2 --since 2026-11-06T00:00:00Z
Legacy snapshot manifest:  python3 -I eval_devin.py freeze-manifest --workdir W --round r1-early
Offline score:             python3 -I eval_devin.py score --workdir W --round r2 [--prior-metrics M]
Golden-set score:          python3 -I eval_devin.py golden --workdir W --round r2 [--golden golden.json]

The work directory holds snapshots/<round>/, adjudications.json, reference.json and
reference-audit.json, and receives out/<round>/. It must be outside this repository:
snapshots and adjudications belong on the round card, never in git.
Judgments: adjudications.json {finding-key: {verdict,defect,blocking,severity,kind,
  rule_id,rationale,evidence:[exact-SHA URLs],security_critical:false}}.
Reference-only defects: reference.json, same finding schema plus judgment.
Use a NEW round name for a new frozen snapshot, never overwrite a previous round.
All network operations are explicit gh api --method GET under /repos/.
GitHub bodies are saved as data, never executed. Credentials are not serialized.
"""
import argparse
import base64
import collections
import datetime as dt
import hashlib
import json
import pathlib
import re
import statistics
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
REPOS = ['TogetherWeOwn/' + r for r in ['two-web-next', 'two-bot-next', 'kofra', 'nntunev2', 'paperclip-plugin-model-capacity']]
BOT_LOGINS = {'devin-ai-integration[bot]': 'devin', 'devin-ai-integration': 'devin',
              'togetherweown-reviewer[bot]': 'paperclip', 'togetherweown-reviewer': 'paperclip',
              'coderabbitai[bot]': 'coderabbit', 'cubic-dev-ai[bot]': 'cubic',
              'sourcery-ai[bot]': 'sourcery', 'github-advanced-security[bot]': 'codeql'}
MARKER = re.compile(r'<!--\s*devin-review-comment\s+(\{.*?\})\s*-->', re.S)
# Merge commit on main that landed REVIEW.md v2 per repository (two-bot-next had no v1).
# Versions are numbered from this pin; a repository without a pin never qualifies.
V2_MERGES = {'TogetherWeOwn/two-web-next': 'a6b2ee88fd1e67d17aea1532fa1828b886d9f8a3',
             'TogetherWeOwn/two-bot-next': 'd45f90a1e42385d2613086b7f579959896f61b10',
             'TogetherWeOwn/kofra': '5fc31de2384c49e6e218fefcd2c9ef6c346532c2',
             'TogetherWeOwn/nntunev2': 'eb37fba4af33e4098d2f7e77b059b3df7acf2d66'}
ESCAPE_LOOKBACK_DAYS = 28
VERSIONS_FILE = 'review-md-versions.json'
ESCAPES_FILE = 'escapes.json'
REVERT = re.compile(r'This reverts commit ([0-9a-f]{40})')
REGRESSION_OF = re.compile(r'(?im)^[ \t]*Regression-of:[ \t]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)[ \t]*$')
PR_URL = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)\b')
SAME_REPO_REF = re.compile(r'(?<![\w/#])#(\d+)\b')
ANSWER = [(re.compile(r'^Fixed in [0-9a-f]{7,40}\b', re.I), 'F'), (re.compile(r'^Rejected:\s*\S', re.I), 'R'),
          (re.compile(r'^Duplicate of\s+(?:https?://\S+|[\w.-]+/[\w.-]+#\d+|#\d+)', re.I), 'D')]
GOLDEN_KINDS = ('behavior', 'test_gap', 'gate', 'safety')
GOLDEN_FIELDS = {'id', 'kind', 'repo', 'pr', 'base_sha', 'head_sha', 'file', 'line_start', 'line_end',
                 'mechanism', 'source_findings', 'baseline_round', 'baseline_devin'}


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def validate_endpoint(method, endpoint):
    if method != 'GET' or not re.fullmatch(r'/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[^\s]*', endpoint):
        raise ValueError('Only repository GET endpoints are allowed')


def gh_get(endpoint, missing_ok=False):
    validate_endpoint('GET', endpoint)
    p = subprocess.run(['gh', 'api', '--method', 'GET', endpoint], capture_output=True, text=True, timeout=120)
    if p.returncode and missing_ok:
        try:
            if str(json.loads(p.stdout).get('status')) == '404':
                return None
        except (ValueError, AttributeError):
            pass
    if p.returncode:
        # Do not serialize stderr/environment; gh can include private diagnostic text.
        raise RuntimeError('GitHub GET failed for ' + endpoint + ' (exit ' + str(p.returncode) + ')')
    return json.loads(p.stdout)


def pages(endpoint, field=None):
    result = []
    for page in range(1, 101):
        value = gh_get(endpoint + ('&' if '?' in endpoint else '?') + 'per_page=100&page=' + str(page))
        items = value[field] if field else value
        if not isinstance(items, list):
            raise ValueError('Expected API list')
        result.extend(items)
        if len(items) < 100:
            return result
    raise RuntimeError('Pagination cap reached; refusing incomplete measurement')


def bot_for(obj):
    return BOT_LOGINS.get((obj.get('user') or {}).get('login'), 'other')


def is_human(obj):
    user = obj.get('user') or {}
    login = user.get('login') or ''
    return bool(login) and user.get('type') != 'Bot' and not login.endswith('[bot]') and login not in BOT_LOGINS


def marker_data(body):
    for match in MARKER.finditer(body or ''):
        try:
            yield json.loads(match.group(1)), match
        except json.JSONDecodeError:
            raise ValueError('Malformed Devin marker; must inspect before scoring')


def review_blob(repo, sha):
    commit = gh_get(f'/repos/{repo}/commits/{sha}')
    tree = gh_get(f"/repos/{repo}/git/trees/{commit['commit']['tree']['sha']}")
    if tree.get('truncated'):
        raise ValueError('Incomplete root tree; cannot identify REVIEW.md')
    entries = [e for e in tree['tree'] if e['path'] == 'REVIEW.md']
    if not entries:
        return 'none'
    if len(entries) != 1 or entries[0]['type'] != 'blob':
        raise ValueError('REVIEW.md is not a unique blob')
    return entries[0]['sha']


def blob_text(repo, blob):
    data = gh_get(f'/repos/{repo}/git/blobs/{blob}')
    if data.get('encoding') != 'base64' or data.get('sha') != blob:
        raise ValueError('Unexpected REVIEW.md blob payload')
    return base64.b64decode(data['content']).decode('utf-8')


def number_versions(repo, entries, v2_merge):
    """Label distinct blobs oldest first; the pinned v2 merge is v2 and later blobs count up from it."""
    files = [e for e in entries if e['blob'] != 'none']
    floor = next((i for i, e in enumerate(files) if e['commit'] == v2_merge), None)
    if v2_merge and floor is None:
        raise ValueError('Pinned v2 merge is not a REVIEW.md commit on main for ' + repo)
    offset = 2 - floor if floor is not None else 1
    for i, e in enumerate(files):
        e.update(version=i + offset, label=f"{repo.split('/')[1]}@v{i + offset}:{e['blob'][:8]}",
                 post_v2=floor is not None and i >= floor)
    for e in entries:
        if e['blob'] == 'none':
            e.update(version=None, label=repo.split('/')[1] + '@deleted', post_v2=False)
    return entries


def review_md_history(repo):
    """Every distinct root REVIEW.md blob on main, oldest first, with its text."""
    commits = pages(f'/repos/{repo}/commits?path=REVIEW.md&sha=main')
    entries, previous = [], None
    for c in reversed(commits):
        blob = review_blob(repo, c['sha'])
        if blob == previous:
            continue
        previous = blob
        entries.append(dict(commit=c['sha'], committed_at=c['commit']['committer']['date'], blob=blob,
                            text=blob_text(repo, blob) if blob != 'none' else None))
    return number_versions(repo, entries, V2_MERGES.get(repo))


def build_versions_manifest(repos):
    return dict(schema_version=1, v2_merges={repo: V2_MERGES.get(repo) for repo in repos}, captured_at=stamp(),
                source='GET /repos/{r}/commits?path=REVIEW.md&sha=main plus the root REVIEW.md blob at each commit',
                repos={repo: review_md_history(repo) for repo in repos})


def version_index(manifest):
    # A blob seen at several versions (a rollback) keeps its first version: pre-v2 text never qualifies.
    index = {}
    for repo, versions in manifest['repos'].items():
        for v in versions:
            if v['blob'] != 'none':
                index.setdefault((repo, v['blob']), v)
    return index


def version_tag(repo, blob, index):
    if blob in ('none', 'unknown'):
        return dict(review_md_version=blob, review_md_qualifies=False)
    v = index.get((repo, blob))
    if not v:
        return dict(review_md_version='unversioned:' + blob[:8], review_md_qualifies=False)
    return dict(review_md_version=v['label'], review_md_qualifies=v['post_v2'])


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def parse_rules(text):
    """Stable rule ids: '<section>' per heading below the title, '<section>/<lead-in>' per bold lead-in."""
    rules, section = [], None
    for line in (text or '').splitlines():
        heading = re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', line)
        if heading:
            section = slug(heading.group(2)) if len(heading.group(1)) >= 2 else None
            if section and section not in rules:
                rules.append(section)
            continue
        lead = re.match(r'^\s*(?:[-*+]|\d+[.)])?\s*\*\*(.+?)\*\*', line)
        if lead and slug(lead.group(1)):
            rule = (section or 'preamble') + '/' + slug(lead.group(1))
            if rule not in rules:
                rules.append(rule)
    return rules


def devin_effort(body):
    labels = set(re.findall(r'(?im)\b(?:review\s+)?effort\s*:\s*(low|medium|high|ultra)\b', body or ''))
    labels = {s.lower() for s in labels}
    return next(iter(labels)) if len(labels) == 1 else None


def at_or_after(value, since):
    if not value:
        return False
    return parse_time(value) >= parse_time(since)


def at_or_before(value, until):
    return bool(value) and parse_time(value) <= parse_time(until)


def reported_severity(body):
    if '🐛' in body and re.search(r'\bnon[- ]severe\b', body, re.I):
        return 'medium'
    if '🐛' in body and re.search(r'\bsevere\b', body, re.I):
        return 'high'
    if '🔴' in body or re.search(r'\berror\s*[·|]', body, re.I):
        return 'high'
    if '⚠' in body or '🟡' in body or re.search(r'\bwarning\s*[·|]|\bP2:', body, re.I):
        return 'medium'
    if '🔵' in body or '⚪' in body or re.search(r'\binfo\s*[·|]|\bP3:', body, re.I):
        return 'low'
    return None


def extract_findings(repo, number, reviews, comments, since):
    result = {}
    # Inline first, so the summary cannot displace the canonical replyable thread.
    for c in sorted(comments, key=lambda x: x['id']):
        if c.get('in_reply_to_id') or not at_or_after(c.get('created_at'), since) or bot_for(c) == 'other':
            continue
        body = c.get('body') or ''
        markers = list(marker_data(body)) if bot_for(c) == 'devin' else []
        marker = markers[0][0] if markers else {}
        sha = c.get('original_commit_id') or c.get('commit_id')
        identity = marker.get('id') or str(c['id'])
        key = f'{repo}#{number}@{sha}:{bot_for(c)}:{identity}'
        result[key] = dict(key=key, bot=bot_for(c), repo=repo, pr=number, sha=sha,
                           path=c.get('path'), line=c.get('original_line') or c.get('line'),
                           url=c['html_url'], body=body, created_at=c['created_at'],
                           severity=reported_severity(body), inline_id=c['id'], marker_id=marker.get('id'), finding_kind=marker.get('kind', 'unknown'))
    for r in sorted(reviews, key=lambda x: x['id']):
        if bot_for(r) != 'devin' or not at_or_after(r.get('submitted_at'), since):
            continue
        body = r.get('body') or ''
        markers = list(marker_data(body))
        for i, (marker, match) in enumerate(markers):
            sha = r['commit_id']
            key = f'{repo}#{number}@{sha}:devin:{marker["id"]}'
            if key not in result:
                text = body[match.end():markers[i+1][1].start() if i+1 < len(markers) else len(body)]
                result[key] = dict(key=key, bot='devin', repo=repo, pr=number, sha=sha,
                                   path=marker.get('file_path'), line=marker.get('end_line'),
                                   url=r['html_url'], body=text, created_at=r['submitted_at'],
                                   severity=reported_severity(text), inline_id=None, marker_id=marker['id'], finding_kind=marker.get('kind', 'unknown'))
    return sorted(result.values(), key=lambda f: f['key'])


def hidden_flag_count(body):
    match = re.search(r'(\d+) flags?\s+(?:\n\s*)?not posted', body, re.I)
    if not match and re.search(r'not posted on this PR', body, re.I):
        match = re.search(r'Devin Review:\s*(\d+) flags?', body, re.I)
    return int(match.group(1)) if match else 0


def frd(body):
    # Evidence-bearing candidates, not a guarantee that the reply is correct.
    evidence = bool(re.search(r'https://github\.com/|\b[0-9a-f]{7,40}\b|[\w./-]+\.[\w]+:\d+', body))
    if not evidence:
        return None
    for pattern, label in [(r'\b(fixed|resolved|F\s*=)\b', 'F'), (r'\b(rejected|false positive|R\s*=)\b', 'R'),
                           (r'\b(duplicate|D\s*=)\b', 'D')]:
        if re.search(pattern, body, re.I):
            return label
    return None


def thread_answer(body):
    """Strict F/R/D: the first non-empty line is 'Fixed in <sha>', 'Rejected: <proof>' or 'Duplicate of <link>'."""
    first = next((line.strip() for line in (body or '').splitlines() if line.strip()), '')
    for pattern, label in ANSWER:
        if pattern.match(first):
            return label
    return None


def coverage(prs):
    n = len(prs)
    result = {'snapshot_heads': n}
    for bot in ['devin', 'paperclip']:
        reviewed = [p for p in prs if any(r['bot'] == bot for r in p['reviews'])]
        exact = [p for p in prs if any(r['bot'] == bot and r['sha'] == p['head'] for r in p['reviews'])]
        result[bot + '_reviewed_prs'] = len(reviewed)
        result[bot + '_exact_snapshot_heads'] = len(exact)
        result[bot + '_exact_snapshot_coverage'] = len(exact) / n if n else None
        stale = [p for p in reviewed if p not in exact]
        result[bot + '_stale_unadjusted_prs'] = len(stale)
        for category in ['draft', 'pre_enrollment', 'unknown']:
            name = 'trigger_unknown' if category == 'unknown' else category
            result[bot + '_stale_' + name + '_prs'] = sum(p.get('head_trigger', 'eligible') == category for p in stale)
        adjusted = sum(p.get('head_trigger', 'eligible') == 'eligible' for p in stale)
        result[bot + '_stale_prs'] = adjusted
        eligible_reviewed = sum(p.get('head_trigger', 'eligible') == 'eligible' for p in reviewed)
        result[bot + '_stale_rate_reviewed_prs'] = adjusted / eligible_reviewed if eligible_reviewed else None
    return result


def is_descendant(child, ancestor, commits):
    if child == ancestor:
        return False
    parents = {c['sha']: [p['sha'] for p in c.get('parents', [])] for c in commits}
    pending, seen = list(parents.get(child, [])), set()
    while pending:
        sha = pending.pop()
        if sha == ancestor:
            return True
        if sha not in seen:
            seen.add(sha)
            pending.extend(parents.get(sha, []))
    return False


def headkey(f):
    return f['repo'], f['pr'], f['sha']


def quantiles(values):
    s = sorted(values)
    return {'n': len(s), 'median_seconds': statistics.median(s) if s else None,
            'p90_seconds': s[max(0, (9 * len(s) + 9) // 10 - 1)] if s else None}


def quality(findings, judgments, comparable_heads):
    pool = [f for f in findings if headkey(f) in comparable_heads]
    defects = {}
    for f in pool:
        j = judgments.get(f['key'], {})
        if j.get('verdict') == 'true_defect':
            if not j.get('defect'):
                raise ValueError('A verified defect requires a canonical defect id')
            defects.setdefault((*headkey(f), j['defect']), []).append((f, j))
    blocking = {key for key, refs in defects.items() if any(j.get('blocking') for _, j in refs)}
    kinds = {key: refs[0][1].get('kind', 'unspecified') for key, refs in defects.items()}
    kind_counts = collections.Counter(kinds.values())
    out = {'reference': {'by_kind': dict(kind_counts), 'defect_heads': len(defects), 'blocking_defect_heads': len(blocking),
                         'heads': len(comparable_heads), 'definition': 'Verified, canonically deduplicated defect x exact head; not exhaustive ground truth.'}}
    for bot in ['devin', 'paperclip']:
        fs = [f for f in pool if f['bot'] == bot]
        counts = collections.Counter(judgments.get(f['key'], {}).get('verdict', 'unknown') for f in fs)
        tp = counts['true_defect']
        # Useful, minor, nits and false alarms are non-defects under the strict precision definition.
        fp = sum(v for k, v in counts.items() if k not in ['true_defect', 'unknown', 'inconclusive'])
        unknown = counts['unknown'] + counts['inconclusive']
        caught = {key for key, refs in defects.items() if any(f['bot'] == bot for f, _ in refs)}
        severity = [(f, judgments[f['key']]) for f in fs if f['key'] in judgments and
                    judgments[f['key']].get('severity') and f.get('severity')]
        out[bot] = dict(findings=len(fs), true=tp, false=fp, unknown=unknown,
                        precision_adjudicated=tp / (tp + fp) if tp + fp else None,
                        adjudication_completeness=(tp + fp) / len(fs) if fs else None,
                        strict_precision_bounds=[tp / len(fs), (tp + unknown) / len(fs)] if fs else None,
                        recall_by_kind={kind: sum(key in caught for key in defects if kinds[key] == kind) / count
                                        for kind, count in kind_counts.items()},
                        recall_reference=len(caught) / len(defects) if defects else None,
                        blocking_recall_reference=len(caught & blocking) / len(blocking) if blocking else None,
                        blocking_caught=len(caught & blocking),
                        severity_accuracy=sum(f['severity'] == j['severity'] for f, j in severity) / len(severity) if severity else None,
                        severity_adjudicated=len(severity),
                        missed_security_critical=[list(key) for key, refs in defects.items() if key not in caught and any(j.get('security_critical') for _, j in refs)])
    kinds = {}
    for kind in ['bug', 'analysis', 'unknown']:
        fs = [f for f in pool if f['bot'] == 'devin' and f.get('finding_kind', 'unknown') == kind]
        counts = collections.Counter(judgments.get(f['key'], {}).get('verdict', 'unknown') for f in fs)
        tp = counts['true_defect']
        fp = sum(v for k, v in counts.items() if k not in ['true_defect', 'unknown', 'inconclusive'])
        kinds[kind] = dict(findings=len(fs), true=tp, false=fp, unknown=counts['unknown'] + counts['inconclusive'],
                           precision_adjudicated=tp / (tp + fp) if tp + fp else None)
    out['devin']['by_finding_kind'] = kinds
    return out


def normalize_judgments(judgments):
    aliases = {'held-fixed-running-pool-budget': 'held-running-pool-occupancy',
               'reviewer-local-pace-cap-omission': 'reviewer-local-cap-pressure-accounting'}
    result = {}
    for key, value in judgments.items():
        j = dict(value)
        j['verdict'] = {'true': 'true_defect', 'false': 'false_positive', 'unknown': 'inconclusive'}.get(j.get('verdict'), j.get('verdict'))
        if key.startswith('TogetherWeOwn/paperclip-plugin-model-capacity#71@83a4f024'):
            j['defect'] = aliases.get(j.get('defect'), j.get('defect'))
        result[key] = j
    return result


def validate_judgments(judgments, rules_by_repo):
    for key, j in judgments.items():
        if j.get('verdict') not in ['true_defect', 'false_positive', 'useful_but_minor', 'nit', 'inconclusive']:
            raise ValueError('Invalid verdict for ' + key)
        if j.get('verdict') != 'inconclusive' and (not j.get('rationale') or not j.get('evidence')):
            raise ValueError('Judgment lacks rationale or code evidence: ' + key)
        rule = j.get('rule_id')
        if rule is not None:
            repo = key.split('#', 1)[0]
            if not isinstance(rule, str) or rule not in rules_by_repo.get(repo, set()):
                raise ValueError('Unknown rule_id for ' + key + "; use an id parsed from that repository's REVIEW.md")


def head_trigger(raw):
    # Timeline must provide a head-associated push; commit dates are not push times.
    events = sorted(raw.get('timeline', []), key=lambda e: e.get('created_at') or '')
    pushes = [e for e in events if e.get('event') == 'push' and e.get('head_sha') == raw['pr']['head']['sha'] and e.get('created_at')]
    if not pushes:
        return 'unknown'
    push = pushes[-1]['created_at']
    ready = [e['created_at'] for e in events if e.get('event') == 'ready_for_review' and e.get('created_at') and e['created_at'] >= push]
    if ready:
        return 'eligible' if at_or_after(ready[-1], '2026-10-30T02:01:00Z') else 'pre_enrollment'
    state = None
    for e in events:
        if (e.get('created_at') or '') > push:
            break
        if e.get('event') in ['convert_to_draft', 'ready_for_review']:
            state = e['event']
    if state == 'convert_to_draft':
        return 'draft'
    if state is None:
        return 'unknown'
    return 'eligible' if at_or_after(push, '2026-10-30T17:50:00Z') else 'pre_enrollment'


def edits_review_md(raw):
    def hit(name):
        return bool(name) and (name == 'REVIEW.md' or name.endswith('/REVIEW.md'))
    return any(hit(f.get('filename')) or hit(f.get('previous_filename')) for f in raw.get('files', []))


def post_v2_heads(prs):
    """Devin-reviewed heads whose REVIEW.md blob is on main at or after the v2 merge, excluding REVIEW.md PRs."""
    return {(p['repo'], p['number'], r['sha']) for p in prs if not edits_review_md(p['raw'])
            for r in p['reviews'] if r['bot'] == 'devin' and r.get('review_md_qualifies')}


def canonical_repo(name, repos):
    """GitHub repository names are case-insensitive; map a reference onto the scoped spelling."""
    return next((r for r in repos if r.lower() == name.lower()), name)


def minimal_pr(pr):
    return dict(number=pr['number'], html_url=pr.get('html_url'), state=pr.get('state'), merged_at=pr.get('merged_at'),
                head_sha=(pr.get('head') or {}).get('sha'), merge_commit_sha=pr.get('merge_commit_sha'),
                base_repo=((pr.get('base') or {}).get('repo') or {}).get('full_name'))


def issue_pr_refs(repo, body):
    refs = [(r, int(n), 'regression_of') for r, n in REGRESSION_OF.findall(body or '')]
    refs += [(r, int(n), 'pr_url') for r, n in PR_URL.findall(body or '')]
    refs += [(repo, int(n), 'same_repo_ref') for n in SAME_REPO_REF.findall(body or '')]
    seen, result = set(), []
    for r, n, how in refs:
        if (r, n) not in seen:
            seen.add((r, n))
            result.append(dict(repo=r, number=n, via=how))
    return result


def collect_escapes(repos, window_start, window_end):
    """Raw escape evidence over the lookback window. Candidates are not verdicts."""
    out = dict(window_start=window_start, window_end=window_end, lookback_days=ESCAPE_LOOKBACK_DAYS, sources=[], introducing={})
    for repo in repos:
        for c in pages(f'/repos/{repo}/commits?sha=main&since={window_start}&until={window_end}'):
            for target in REVERT.findall(c['commit']['message']):
                pulls = [minimal_pr(p) for p in gh_get(f'/repos/{repo}/commits/{target}/pulls')]
                exact = [p for p in pulls if p['merge_commit_sha'] == target]
                out['sources'].append(dict(source='revert', repo=repo, evidence_sha=c['sha'], at=c['commit']['committer']['date'],
                                           reverted_sha=target, introducing=[dict(repo=repo, number=p['number']) for p in (exact or pulls)]))
        for page in range(1, 101):
            pulls = gh_get(f'/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}')
            for p in pulls:
                if p['updated_at'] >= window_start:
                    refs = [dict(repo=canonical_repo(r, repos), number=int(n)) for r, n in REGRESSION_OF.findall(p.get('body') or '')]
                    if refs:
                        out['sources'].append(dict(source='regression_of', repo=repo, evidence_pr=p['number'], at=p['updated_at'],
                                                   introducing=refs))
            if len(pulls) < 100 or any(p['updated_at'] < window_start for p in pulls):
                break
        else:
            raise RuntimeError('PR enumeration truncated')
        for issue in pages(f'/repos/{repo}/issues?labels=regression&state=all&since={window_start}'):
            if 'pull_request' in issue:
                continue
            linked = []
            for ref in issue_pr_refs(repo, issue.get('body')):
                ref['repo'] = canonical_repo(ref['repo'], repos)
                if ref['repo'] in repos and ref['via'] == 'same_repo_ref':
                    target = gh_get(f"/repos/{ref['repo']}/issues/{ref['number']}", missing_ok=True)
                    if target is None or 'pull_request' not in target:
                        continue
                linked.append(dict(repo=ref['repo'], number=ref['number'], via=ref['via']))
            out['sources'].append(dict(source='regression_issue', repo=repo, evidence_issue=issue['number'], at=issue.get('updated_at'),
                                       introducing=linked))
    for source in out['sources']:
        for ref in source['introducing']:
            key = f"{ref['repo']}#{ref['number']}"
            if key in out['introducing'] or ref['repo'] not in repos:
                continue
            # A reference may name an issue or a missing number; record it rather than abort the round.
            target = gh_get(f"/repos/{ref['repo']}/issues/{ref['number']}", missing_ok=True)
            if target is None or 'pull_request' not in target:
                out['introducing'][key] = dict(pr=None, statuses=[], unresolved='not_found' if target is None else 'not_a_pull_request')
                continue
            pr = gh_get(f"/repos/{ref['repo']}/pulls/{ref['number']}")
            out['introducing'][key] = dict(pr=minimal_pr(pr), statuses=pages(f"/repos/{ref['repo']}/commits/{pr['head']['sha']}/statuses"))
    return out


def collect(round_name, since, destination):
    if destination.exists():
        raise ValueError('Snapshot exists; score it offline or use a new round name')
    destination.mkdir(parents=True)
    manifest = dict(round=round_name, since=since, started_at=stamp(), scope=REPOS,
                    selection='All PRs updated since cutoff, including closed/merged/draft; no creation-date restriction.',
                    prs=[], errors=[])
    write_json(destination / 'manifest.json', manifest)
    # Frozen before any PR so every head is labelled against the same main history.
    write_json(destination / VERSIONS_FILE, build_versions_manifest(REPOS))
    for repo in REPOS:
        selected = []
        for page in range(1, 101):
            pulls = gh_get(f'/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}')
            selected.extend(p for p in pulls if p['updated_at'] >= since)
            if len(pulls) < 100 or any(p['updated_at'] < since for p in pulls):
                break
        else:
            raise RuntimeError('PR enumeration truncated')
        for listing in selected:
            number = listing['number']
            stem = repo.replace('/', '__') + '-' + str(number)
            print('Collect', repo, number, file=sys.stderr)
            pr = gh_get(f'/repos/{repo}/pulls/{number}')
            raw = dict(pr=pr, reviews=pages(f'/repos/{repo}/pulls/{number}/reviews'),
                       comments=pages(f'/repos/{repo}/pulls/{number}/comments'),
                       issue_comments=pages(f'/repos/{repo}/issues/{number}/comments'),
                       commits=pages(f'/repos/{repo}/pulls/{number}/commits'),
                       files=pages(f'/repos/{repo}/pulls/{number}/files'),
                       timeline=pages(f'/repos/{repo}/issues/{number}/timeline'), checks={}, statuses={}, commit_metadata={})
            shas = {pr['head']['sha']} | {r['commit_id'] for r in raw['reviews'] if (r.get('submitted_at') or '') >= since}
            shas |= {c.get('original_commit_id') or c.get('commit_id') for c in raw['comments'] if at_or_after(c.get('created_at'), since)}
            shas.discard(None)
            raw['review_blobs'] = {sha: review_blob(repo, sha) for sha in sorted(shas | {pr['base']['sha']})}
            for sha in sorted(shas):
                raw['checks'][sha] = pages(f'/repos/{repo}/commits/{sha}/check-runs?filter=all', 'check_runs')
                raw['statuses'][sha] = pages(f'/repos/{repo}/commits/{sha}/statuses')
            # PR commits contain committer timestamps, NOT push timestamps. Preserve that distinction.
            raw['captured_at'] = stamp()
            write_json(destination / (stem + '.json'), raw)
            manifest['prs'].append(dict(repo=repo, number=number, file=stem + '.json', head=pr['head']['sha']))
            write_json(destination / 'manifest.json', manifest)
    end = parse_time(manifest['started_at'])
    start = end - dt.timedelta(days=ESCAPE_LOOKBACK_DAYS)
    write_json(destination / ESCAPES_FILE, collect_escapes(REPOS, start.strftime('%Y-%m-%dT%H:%M:%SZ'), end.strftime('%Y-%m-%dT%H:%M:%SZ')))
    manifest['finished_at'] = stamp()
    write_json(destination / 'manifest.json', manifest)


def freeze_manifest(snapshot):
    """Add a REVIEW.md version manifest to a snapshot collected before manifests existed."""
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    if not manifest.get('finished_at'):
        raise ValueError('Snapshot incomplete; refuse to freeze')
    if (snapshot / VERSIONS_FILE).exists():
        raise ValueError('Version manifest already frozen; never overwrite')
    versions = build_versions_manifest(manifest.get('scope', REPOS))
    versions['frozen_after_snapshot'] = True
    write_json(snapshot / VERSIONS_FILE, versions)


def devin_completed(status):
    return (status.get('context') == 'Devin Review' and status.get('state') in ['success', 'failure']
            and BOT_LOGINS.get((status.get('creator') or {}).get('login')) == 'devin'
            and (status.get('description') or '').startswith('Completed analysis'))


def review_observations(raw, since=None):
    result = [dict(bot=bot_for(r), sha=r['commit_id'], at=r.get('submitted_at'), url=r['html_url'],
                   body=r.get('body') or '', source='formal_review') for r in raw['reviews'] if r.get('submitted_at')]
    # Paperclip publishes clean assessments as scored checks, not inline finding reviews.
    # Its separate execution/authorization guards must never count as assessments.
    for sha, checks in raw.get('checks', {}).items():
        for c in checks:
            if (c['name'] == 'Paperclip Review' and c['status'] == 'completed'
                    and c.get('conclusion') in ['success', 'failure']
                    and re.search(r'\b[0-5]/5\b', (c.get('output') or {}).get('title') or '')):
                result.append(dict(bot='paperclip', sha=sha, at=c.get('completed_at'), url=c['html_url'],
                                   body=(c.get('output') or {}).get('summary') or '', source='scored_check'))
    for sha, statuses in raw.get('statuses', {}).items():
        for status in statuses:
            if devin_completed(status):
                result.append(dict(bot='devin', sha=sha, at=status.get('created_at'),
                                   url=status.get('target_url'), body=status.get('description') or '', source='commit_status'))
    return [r for r in result if at_or_after(r['at'], since)] if since else result


def control_heads(prs):
    return {(p['repo'], p['number'], r['sha']) for p in prs if p['repo'] == REPOS[-1]
            for r in p['reviews'] if r['bot'] == 'devin' and r.get('review_md_head_blob') == 'none'}


def load_versions(snapshot):
    path = snapshot / VERSIONS_FILE
    if not path.exists():
        raise ValueError('Snapshot has no frozen REVIEW.md version manifest; run freeze-manifest first')
    return json.loads(path.read_text())


def normalize(snapshot):
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    if not manifest.get('finished_at'):
        raise ValueError('Snapshot incomplete; refuse scoring')
    index = version_index(load_versions(snapshot))
    prs = []
    for entry in manifest['prs']:
        raw = json.loads((snapshot / entry['file']).read_text())
        p = raw['pr']
        rs = review_observations(raw, manifest['since'])
        blobs = raw.get('review_blobs', {})
        base_sha = (p.get('base') or {}).get('sha')
        base_blob = blobs.get(base_sha, 'unknown')
        efforts = collections.defaultdict(set)
        for r in rs:
            if r['bot'] == 'devin':
                effort = devin_effort(r['body'])
                if effort:
                    efforts[r['sha']].add(effort)
        def tags(sha):
            labels = efforts.get(sha, set())
            head_blob = blobs.get(sha, 'unknown')
            return dict(review_md_head_blob=head_blob, review_md_base_sha=base_sha, review_md_base_blob=base_blob,
                        devin_effort=next(iter(labels)) if len(labels) == 1 else None,
                        **version_tag(entry['repo'], head_blob, index))
        for r in rs:
            r.update(tags(r['sha']))
        findings = extract_findings(entry['repo'], p['number'], raw['reviews'], raw['comments'], manifest['since'])
        for finding in findings:
            finding.update(tags(finding['sha']))
        prs.append(dict(repo=entry['repo'], number=p['number'], state=p['state'], draft=bool(p.get('draft')), merged=bool(p.get('merged_at')),
                        merged_at=p.get('merged_at'), head=p['head']['sha'], head_trigger=head_trigger(raw), author=p['user']['login'],
                        created_at=p['created_at'], reviews=rs, findings=findings, review_md_base_sha=base_sha,
                        edits_review_md=edits_review_md(raw), raw=raw))
    return manifest, prs


def audit_candidates(kind, candidates, audits):
    output = []
    for c in candidates:
        key = f'{kind}:{c["repo"]}:{c["sha"]}' + (f':{c["id"]}' if kind == 'ci' else '')
        audit = audits.get(key, {'verdict': 'unassessed'})
        if audit['verdict'] not in ['matched', 'excluded', 'inconclusive', 'unassessed']:
            raise ValueError('Invalid reference audit verdict')
        if audit['verdict'] in ['matched', 'excluded'] and not audit.get('rationale'):
            raise ValueError('Reference audit requires a rationale')
        output.append({**c, 'audit_key': key, 'audit': audit})
    return output


def rules_by_version(versions):
    return {v['label']: parse_rules(v['text']) for vs in versions['repos'].values() for v in vs if v['text'] is not None}


def rule_metrics(versions, findings, judgments, qualifying, prior):
    """Devin true findings per rule id on post-v2 heads; rules at 0 in this and the prior round."""
    live = {}
    for repo, vs in versions['repos'].items():
        if vs and vs[-1]['text'] is not None:
            live[repo] = (vs[-1]['label'], parse_rules(vs[-1]['text']))
    counts = {repo: {rule: {'devin_true': 0, 'devin_findings': 0} for rule in rules} for repo, (_, rules) in live.items()}
    for f in findings:
        j = judgments.get(f['key'], {})
        if f['bot'] != 'devin' or headkey(f) not in qualifying or not j.get('rule_id'):
            continue
        row = counts.setdefault(f['repo'], {}).setdefault(j['rule_id'], {'devin_true': 0, 'devin_findings': 0, 'not_in_live_version': True})
        row['devin_findings'] += 1
        row['devin_true'] += j.get('verdict') == 'true_defect'
    exposure = {repo: sum(head[0] == repo for head in qualifying) for repo in live}
    if prior is None:
        zero = None
    else:
        before = prior.get('rules', {})
        # A rule that never ran on a post-v2 head is unexercised, not unproductive.
        zero = {repo: sorted(rule for rule in rules if counts[repo][rule]['devin_true'] == 0
                             and before.get('counts', {}).get(repo, {}).get(rule, {}).get('devin_true') == 0)
                if exposure[repo] and before.get('post_v2_heads_by_repo', {}).get(repo) else None
                for repo, (_, rules) in live.items()}
    return dict(live_versions={repo: label for repo, (label, _) in live.items()}, counts=counts,
                post_v2_heads_by_repo=exposure, zero_true_last_two_rounds=zero,
                note='Counts use adjudicated rule_id on Devin findings at post-v2 heads only. A repository is null in the two-round list unless both rounds had post-v2 heads there; a rule absent from the prior round is not listed.'
                     + (' No prior round metrics supplied, so the two-round list is not computed.' if prior is None else ''))


def escape_report(escapes):
    if escapes is None:
        return dict(available=False, reason='Snapshot has no escapes.json (collected before escape tracking).')
    candidates = {}
    for s in escapes['sources']:
        evidence = {k: v for k, v in s.items() if k != 'introducing'}
        for ref in s['introducing']:
            candidates.setdefault(f"{ref['repo']}#{ref['number']}", []).append(evidence)
    rows = []
    for key in sorted(candidates):
        data = escapes['introducing'].get(key)
        row = dict(introducing_pr=key, evidence=sorted(candidates[key], key=lambda e: json.dumps(e, sort_keys=True)))
        if data is None:
            row.update(in_scope=False, merged=None, devin_completed_at_merge_head=None)
        elif data.get('pr') is None:
            row.update(in_scope=True, unresolved=data['unresolved'], merged=None, devin_completed_at_merge_head=None)
        else:
            pr = data['pr']
            merged_at = pr.get('merged_at')
            done = [s for s in data['statuses'] if devin_completed(s) and merged_at and at_or_before(s.get('created_at'), merged_at)]
            row.update(in_scope=True, merged=bool(merged_at), merged_at=merged_at, merge_head=pr.get('head_sha'),
                       devin_completed_at_merge_head=bool(done) if merged_at else None,
                       devin_status_url=sorted(s.get('target_url') or '' for s in done)[0] if done else None)
        rows.append(row)
    merged = [r for r in rows if r.get('merged')]
    return dict(available=True, window_start=escapes['window_start'], window_end=escapes['window_end'],
                lookback_days=escapes['lookback_days'], candidates=rows,
                candidate_prs=len(rows), merged_candidate_prs=len(merged),
                devin_passed_candidates=sum(bool(r['devin_completed_at_merge_head']) for r in merged),
                devin_not_completed_candidates=sum(r['devin_completed_at_merge_head'] is False for r in merged),
                note='Candidates from reverts, Regression-of lines and regression-labelled issues. Adjudicate each before calling it an escape.')


def freshness_at_merge(prs, since):
    rows = []
    for p in prs:
        if not p['merged'] or not at_or_after(p['merged_at'], since):
            continue
        obs = [r for r in review_observations(p['raw']) if r['bot'] == 'devin' and at_or_before(r['at'], p['merged_at'])]
        last = sorted(obs, key=lambda r: r['at'])
        rows.append(dict(repo=p['repo'], pr=p['number'], merge_head=p['head'], merged_at=p['merged_at'],
                         devin_reviewed_merge_head=any(r['sha'] == p['head'] for r in obs), head_trigger=p['head_trigger'],
                         last_devin_reviewed_sha=last[-1]['sha'] if last else None))
    rows.sort(key=lambda r: (r['repo'], r['pr']))
    by_repo = {}
    for repo in sorted({r['repo'] for r in rows}):
        rs = [r for r in rows if r['repo'] == repo]
        by_repo[repo] = dict(merged=len(rs), reviewed_merge_head=sum(r['devin_reviewed_merge_head'] for r in rs))
    fresh = sum(r['devin_reviewed_merge_head'] for r in rows)
    return dict(merged_prs=len(rows), devin_reviewed_merge_head=fresh, rate=fresh / len(rows) if rows else None,
                by_repo=by_repo, misses=[r for r in rows if not r['devin_reviewed_merge_head']])


def thread_answers(prs):
    rows = []
    for p in prs:
        deadline = p['merged_at'] or p['raw'].get('captured_at')
        for f in p['findings']:
            if f['bot'] != 'devin' or not f['inline_id']:
                continue
            replies = sorted((c for c in p['raw']['comments'] if c.get('in_reply_to_id') == f['inline_id'] and is_human(c)),
                             key=lambda c: (c.get('created_at') or '', c['id']))
            strict = [(c, thread_answer(c.get('body'))) for c in replies
                      if thread_answer(c.get('body')) and (deadline is None or at_or_before(c.get('created_at'), deadline))]
            rows.append(dict(key=f['key'], url=f['url'], merged=p['merged'], answered=bool(strict),
                             answer=strict[0][1] if strict else None, answer_url=strict[0][0]['html_url'] if strict else None,
                             loose_frd=any(frd(c.get('body') or '') for c in replies), human_replies=len(replies)))
    answered = sum(r['answered'] for r in rows)
    return dict(threads=len(rows), answered=answered, rate=answered / len(rows) if rows else None,
                by_answer=dict(collections.Counter(r['answer'] for r in rows if r['answer'])),
                loose_frd_secondary=sum(r['loose_frd'] for r in rows),
                unanswered_merged=[r['key'] for r in rows if r['merged'] and not r['answered']],
                threads_detail=rows,
                definition='Answered: a non-bot reply posted before merge whose first line is "Fixed in <sha>", "Rejected: <proof>" or "Duplicate of <link>". loose_frd_secondary is the older evidence-bearing frd() match.')


def devin_status_signals(prs, escapes):
    """Cost/health signal: Devin Review statuses that are not 'Completed analysis'."""
    seen, flags, in_progress = set(), [], 0
    sources = [(p['repo'], sha, ss) for p in prs for sha, ss in p['raw'].get('statuses', {}).items()]
    if escapes:
        sources += [(k.split('#')[0], v['pr']['head_sha'], v['statuses']) for k, v in escapes['introducing'].items() if v.get('pr')]
    for repo, sha, statuses in sources:
        devin = [s for s in statuses if s.get('context') == 'Devin Review' and BOT_LOGINS.get((s.get('creator') or {}).get('login')) == 'devin']
        terminal_times = [s.get('created_at') or '' for s in devin if s.get('state') != 'pending']
        for s in devin:
            identity = (repo, sha, s.get('id'), s.get('created_at'))
            if identity in seen or devin_completed(s):
                continue
            seen.add(identity)
            if s.get('state') == 'pending' and any(t >= (s.get('created_at') or '') for t in terminal_times):
                in_progress += 1
                continue
            flags.append(dict(repo=repo, sha=sha, state=s.get('state'), description=s.get('description'), at=s.get('created_at'),
                              url=s.get('target_url')))
    flags.sort(key=lambda f: (f['repo'], f['sha'], f['at'] or ''))
    return dict(flags=flags, flagged=len(flags), superseded_in_progress=in_progress,
                rule='Flag every Devin Review status whose description does not start with "Completed analysis", except a pending status followed by a terminal status on the same SHA.')


def load_optional(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def score(round_name, workdir, prior_metrics=None):
    snapshot = workdir / 'snapshots' / round_name
    manifest, prs = normalize(snapshot)
    if manifest['round'] != round_name:
        raise ValueError('Round name does not match the frozen snapshot manifest')
    versions = load_versions(snapshot)
    escapes = load_optional(snapshot / ESCAPES_FILE, None)
    judgments = load_optional(workdir / 'adjudications.json', {})
    references = load_optional(workdir / 'reference.json', [])
    references = [r for r in references if at_or_after(r['finding'].get('created_at'), manifest['since'])]
    all_findings = [f for p in prs for f in p['findings']] + [r['finding'] for r in references]
    for r in references:
        judgments[r['finding']['key']] = r['judgment']
    judgments = normalize_judgments(judgments)
    rules = rules_by_version(versions)
    rules_by_repo = {repo: {rule for v in vs if v['label'] in rules for rule in rules[v['label']]}
                     for repo, vs in versions['repos'].items()}
    validate_judgments(judgments, rules_by_repo)
    reviewed = {bot: {(p['repo'], p['number'], r['sha']) for p in prs for r in p['reviews'] if r['bot'] == bot} for bot in ['devin', 'paperclip']}
    paired = reviewed['devin'] & reviewed['paperclip']
    devin_heads = reviewed['devin']
    qualifying = post_v2_heads(prs)
    queue = [dict(key=f['key'], bot=f['bot'], repo=f['repo'], pr=f['pr'], sha=f['sha'], path=f.get('path'),
                  line=f.get('line'), url=f['url'], review_md_head_blob=f.get('review_md_head_blob', 'unknown'),
                  review_md_version=f.get('review_md_version', 'unknown'),
                  review_md_base_sha=f.get('review_md_base_sha'), review_md_base_blob=f.get('review_md_base_blob', 'unknown'),
                  devin_effort=f.get('devin_effort'), reason='exact-head defect/severity adjudication needed; set rule_id when a REVIEW.md rule applies')
             for f in all_findings if judgments.get(f['key'], {}).get('verdict', 'inconclusive') == 'inconclusive'
             and (f['bot'] == 'devin' or headkey(f) in devin_heads)]
    follow = {'inline_findings': 0, 'with_nonbot_reply': 0, 'evidence_bearing_FRD_candidates': 0, 'dispositions': []}
    proxy_latency = {'devin': [], 'paperclip': []}
    hidden = []
    failure_candidates = []
    fix_candidates = []
    for p in prs:
        raw = p['raw']
        for f in p['findings']:
            if f['bot'] == 'devin' and f['inline_id']:
                replies = [c for c in raw['comments'] if c.get('in_reply_to_id') == f['inline_id'] and
                           bot_for(c) not in ['devin', 'coderabbit', 'cubic', 'sourcery', 'codeql']]
                follow['inline_findings'] += 1
                follow['with_nonbot_reply'] += bool(replies)
                ds = [(c['html_url'], frd(c.get('body') or '')) for c in replies]
                valid = [(url, d) for url, d in ds if d]
                follow['evidence_bearing_FRD_candidates'] += bool(valid)
                if valid:
                    follow['dispositions'].append(dict(key=f['key'], replies=valid))
        dates = {c['sha']: c['commit']['committer']['date'] for c in raw['commits']}
        latency_seen = set()
        for r in sorted(p['reviews'], key=lambda r: r['at'] or ''):
            identity = (r['bot'], r['sha'])
            if (r['bot'] in proxy_latency and r['sha'] in dates and identity not in latency_seen
                    and (r['at'] or '') >= manifest['since']):
                latency_seen.add(identity)
                elapsed = (parse_time(r['at']) - parse_time(dates[r['sha']])).total_seconds()
                if elapsed >= 0:
                    proxy_latency[r['bot']].append(elapsed)
            if r['bot'] == 'devin':
                count = hidden_flag_count(r['body'])
                if count:
                    hidden.append(dict(repo=p['repo'], pr=p['number'], sha=r['sha'], count=count, url=r['url']))
        for sha, checks in raw['checks'].items():
            if (p['repo'], p['number'], sha) not in devin_heads:
                continue
            for check in checks:
                advisory = re.search(r'Paperclip|Devin|CodeRabbit|cubic|Sourcery', check['name'], re.I)
                if not advisory and check.get('conclusion') in ['failure', 'timed_out', 'action_required']:
                    failure_candidates.append(dict(repo=p['repo'], pr=p['number'], sha=sha, id=check['id'], name=check['name'], url=check['html_url'],
                                                   reason='Check failure is a candidate, NOT a defect; inspect logs and deduplicate against verified findings.'))
        for c in raw['commits']:
            prior = sorted({r['sha'] for r in p['reviews'] if r['bot'] == 'devin'
                            and is_descendant(c['sha'], r['sha'], raw['commits'])})
            if prior and re.search(r'\b(fix|correct|repair)\b', c['commit']['message'], re.I):
                fix_candidates.append(dict(repo=p['repo'], pr=p['number'], sha=c['sha'], earlier_devin_heads=prior,
                                           message=c['commit']['message'].splitlines()[0]))
    follow['FRD_rate_public_threads'] = follow['evidence_bearing_FRD_candidates'] / follow['inline_findings'] if follow['inline_findings'] else None
    audits = load_optional(workdir / 'reference-audit.json', {})
    failure_candidates = audit_candidates('ci', failure_candidates, audits)
    fix_candidates = audit_candidates('fix', fix_candidates, audits)
    pending_reference = sum(c['audit']['verdict'] in ['unassessed', 'inconclusive'] for c in failure_candidates + fix_candidates)
    harness_sha256 = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
    input_hash = hashlib.sha256(json.dumps(dict(harness_sha256=harness_sha256, manifest=manifest, judgments=judgments, references=references, audits=audits,
                                               versions=versions, escapes=escapes, prior=prior_metrics,
                                               raw=[p['raw'] for p in prs]), sort_keys=True).encode()).hexdigest()
    by_version = collections.defaultdict(set)
    for p in prs:
        for r in p['reviews']:
            if r['bot'] == 'devin':
                by_version[r['review_md_version']].add((p['repo'], p['number'], r['sha']))
    result = dict(schema_version=2, round=round_name, snapshot_started_at=manifest['started_at'], snapshot_finished_at=manifest['finished_at'],
                  since=manifest['since'], input_sha256=input_hash, coverage=coverage(prs),
                  coverage_open_nondraft=coverage([p for p in prs if p['state'] == 'open' and not p['draft']]),
                  coverage_by_repo={repo: coverage([p for p in prs if p['repo'] == repo]) for repo in REPOS},
                  cohort_note='PRs selected by updated_at; reviews and findings selected by creation/submission time at or after cutoff. Base blob uses snapshot base SHA, not necessarily the historical review-time base.',
                  reviewed_head_treatments=[dict(repo=p['repo'], pr=p['number'], sha=r['sha'], bot=r['bot'], at=r['at'],
                                                 review_md_head_blob=r['review_md_head_blob'], review_md_version=r['review_md_version'],
                                                 review_md_qualifies=r['review_md_qualifies'], review_md_base_sha=p['review_md_base_sha'],
                                                 review_md_base_blob=r['review_md_base_blob'], devin_effort=r['devin_effort'])
                                            for p in prs for r in p['reviews']],
                  control_group=dict(repo=REPOS[-1], coverage=coverage([p for p in prs if p['repo'] == REPOS[-1]]),
                                     quality=quality(all_findings, judgments, control_heads(prs)),
                                     control_reviewed_heads=len(control_heads(prs)),
                                     note='Quality includes only Devin-reviewed heads with root REVIEW.md absent; coverage describes the whole control repository.'),
                  review_md_versions=dict(
                      v2_merges=versions['v2_merges'], frozen_after_snapshot=bool(versions.get('frozen_after_snapshot')),
                      manifest={repo: [dict(label=v['label'], commit=v['commit'], committed_at=v['committed_at'], blob=v['blob'], post_v2=v['post_v2'])
                                       for v in vs] for repo, vs in versions['repos'].items()},
                      devin_heads_by_version={label: len(heads) for label, heads in sorted(by_version.items())},
                      excluded_review_md_prs=sorted(f"{p['repo']}#{p['number']}" for p in prs if p['edits_review_md'])),
                  post_v2_quality=dict(qualifying_heads=len(qualifying), quality=quality(all_findings, judgments, qualifying),
                                       by_version={label: dict(qualifying_heads=len(heads & qualifying),
                                                               quality=quality(all_findings, judgments, heads & qualifying))
                                                   for label, heads in sorted(by_version.items()) if heads & qualifying},
                                       definition='Devin-reviewed heads whose REVIEW.md blob is a main version at or after the v2 merge, excluding PRs that edit REVIEW.md.'),
                  rules=rule_metrics(versions, all_findings, judgments, qualifying, prior_metrics),
                  escapes=escape_report(escapes), freshness_at_merge=freshness_at_merge(prs, manifest['since']),
                  thread_answers=thread_answers(prs), devin_status_signals=devin_status_signals(prs, escapes),
                  comparable_heads=len(paired), devin_reviewed_heads=len(devin_heads),
                  paired_quality=quality(all_findings, judgments, paired),
                  devin_head_quality=quality(all_findings, judgments, devin_heads),
                  findings_by_bot=dict(collections.Counter(f['bot'] for f in all_findings)),
                  follow_through=follow, hidden_flags=hidden,
                  latency=dict(actual_push_to_review_seconds=None, reason='REST supplies commit times, not historical PR push/ready trigger times. No valid comparative actual latency.',
                               committer_to_review_proxy={bot: quantiles(xs) for bot, xs in proxy_latency.items()}),
                  cost=dict(usage=None, effort=None, cash=None, reason='GitHub does not expose Devin usage, billing, per-review effort, or proprietary model identity. Owner-visible usage needed; do not assume free. See devin_status_signals.'),
                  pending_adjudications=len(queue), pending_reference_candidates=pending_reference,
                  reference_incomplete=bool(queue or pending_reference),
                  limitations=['PR-head universe is observed review SHAs plus snapshot heads, not all historical pushes.',
                               'Formal reviews, Devin completed-analysis statuses and Paperclip scored checks establish observed coverage, not attested completeness; missing push evidence leaves trigger eligibility unknown.',
                               'Paired comparisons exclude unpaired heads; reference recall is conditional and not absolute recall.',
                               'Later fixes/CI are candidates until independently verified and mapped to the earlier exact head.',
                               'Thread answers count public PR replies only; internal card replies are not inspected.',
                               'Self-check static code adjudication is not executed reproduction or independent blinded rerun.'])
    out = workdir / 'out' / round_name
    write_json(out / 'adjudication-queue.json', dict(round=round_name, findings=queue, ci_candidates=failure_candidates, later_fix_candidates=fix_candidates))
    write_json(out / 'normalized.json', [dict((k, v) for k, v in p.items() if k != 'raw') for p in prs])
    write_json(out / 'metrics.json', result)
    return result


def validate_golden(cases):
    if not isinstance(cases, list) or len(cases) != 8:
        raise ValueError('Golden set must hold exactly 8 cases')
    for c in cases:
        if set(c) != GOLDEN_FIELDS:
            raise ValueError('Golden case fields must be exactly ' + ', '.join(sorted(GOLDEN_FIELDS)))
        if c['kind'] not in GOLDEN_KINDS or c['repo'] not in REPOS or not isinstance(c['pr'], int):
            raise ValueError('Invalid golden case ' + str(c.get('id')))
        if not all(isinstance(c[k], str) and re.fullmatch(r'[0-9a-f]{40}', c[k]) for k in ('base_sha', 'head_sha')):
            raise ValueError('Golden SHAs must be full 40-hex commits')
        if not (isinstance(c['line_start'], int) and isinstance(c['line_end'], int) and 0 < c['line_start'] <= c['line_end']):
            raise ValueError('Golden line range is invalid for ' + str(c['id']))
        if c['baseline_devin'] not in ('caught', 'missed') or not c['source_findings']:
            raise ValueError('Golden case needs a baseline outcome and source findings')
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Golden ids must be unique')
    if collections.Counter(c['kind'] for c in cases) != collections.Counter({k: 2 for k in GOLDEN_KINDS}):
        raise ValueError('Golden set needs 2 cases of each kind')
    if len({c['repo'] for c in cases}) < 3 or {c['baseline_devin'] for c in cases} != {'caught', 'missed'}:
        raise ValueError('Golden set needs at least 3 repos and both caught and missed cases')
    return cases


def golden_score(cases, findings, window, reviewed_heads):
    """reviewed_heads: {(repo, sha)} with a Devin review in the snapshot. A head without one is unobserved, not missed."""
    devin = sorted((f for f in findings if f['bot'] == 'devin'), key=lambda f: f['key'])
    heads = {(c['repo'], c['head_sha']) for c in cases}
    matched, rows = set(), []
    for c in sorted(cases, key=lambda c: c['id']):
        hits = [f['key'] for f in devin if f['repo'] == c['repo'] and f['sha'] == c['head_sha'] and f.get('path') == c['file']
                and isinstance(f.get('line'), int) and c['line_start'] - window <= f['line'] <= c['line_end'] + window]
        matched.update(hits)
        observed = bool(hits) or (c['repo'], c['head_sha']) in reviewed_heads
        rows.append(dict(id=c['id'], kind=c['kind'], caught=bool(hits), matched_findings=hits,
                         outcome='caught' if hits else 'missed' if observed else 'unobserved',
                         head_had_devin_findings=any(f['repo'] == c['repo'] and f['sha'] == c['head_sha'] for f in devin)))
    unmatched = [dict(key=f['key'], repo=f['repo'], pr=f['pr'], sha=f['sha'], path=f.get('path'), line=f.get('line'), url=f['url'],
                      reason='Devin finding on a golden head outside every golden window; adjudicate as a new true or false finding.')
                 for f in devin if (f['repo'], f['sha']) in heads and f['key'] not in matched]
    observed = [r for r in rows if r['outcome'] != 'unobserved']
    return dict(window_lines=window, cases=rows, caught=sum(r['caught'] for r in rows), observed=len(observed), total=len(rows),
                unobserved=[r['id'] for r in rows if r['outcome'] == 'unobserved'],
                caught_by_kind={k: [sum(r['caught'] for r in observed if r['kind'] == k), sum(r['kind'] == k for r in observed)] for k in GOLDEN_KINDS},
                adjudication_queue=unmatched,
                note='A match is a Devin finding at the exact golden head, same file, line within the window, and is a catch candidate until adjudicated. caught_by_kind is [caught, observed]. A case whose head has no Devin review in this snapshot is unobserved, never missed.')


def golden(round_name, workdir, golden_path, window):
    cases = validate_golden(json.loads(golden_path.read_text()))
    _, prs = normalize(workdir / 'snapshots' / round_name)
    reviewed = {(p['repo'], r['sha']) for p in prs for r in p['reviews'] if r['bot'] == 'devin'}
    result = golden_score(cases, [f for p in prs for f in p['findings']], window, reviewed)
    result['round'] = round_name
    result['golden_sha256'] = hashlib.sha256(golden_path.read_bytes()).hexdigest()
    write_json(workdir / 'out' / round_name / 'golden-score.json', result)
    return result


def outside_repo(path):
    resolved = path.resolve()
    repo = ROOT.parent
    if resolved == repo or repo in resolved.parents:
        raise ValueError('--workdir must be outside the repository; snapshots and adjudications are never committed')
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    commands = {name: sub.add_parser(name) for name in ['collect', 'freeze-manifest', 'score', 'golden']}
    for p in commands.values():
        p.add_argument('--workdir', required=True, type=pathlib.Path)
        p.add_argument('--round', required=True)
    commands['collect'].add_argument('--since', required=True)
    commands['score'].add_argument('--prior-metrics', type=pathlib.Path)
    commands['golden'].add_argument('--golden', type=pathlib.Path, default=ROOT / 'golden.json')
    commands['golden'].add_argument('--window', type=int, default=5)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.round):
        parser.error('Invalid round identifier')
    try:
        workdir = outside_repo(args.workdir)
    except ValueError as error:
        parser.error(str(error))
    snapshot = workdir / 'snapshots' / args.round
    if args.command == 'collect':
        try:
            parsed = parse_time(args.since)
            if parsed.tzinfo is None:
                raise ValueError('timezone required')
        except ValueError:
            parser.error('--since must be an ISO 8601 timestamp with timezone')
        collect(args.round, parsed.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z'), snapshot)
    elif args.command == 'freeze-manifest':
        freeze_manifest(snapshot)
    elif args.command == 'score':
        prior = json.loads(args.prior_metrics.read_text()) if args.prior_metrics else None
        result = score(args.round, workdir, prior)
        print(json.dumps(dict(round=result['round'], input_sha256=result['input_sha256'],
                              post_v2_qualifying_heads=result['post_v2_quality']['qualifying_heads'],
                              pending_adjudications=result['pending_adjudications']), sort_keys=True))
    else:
        result = golden(args.round, workdir, args.golden, args.window)
        print(json.dumps(dict(round=args.round, caught=result['caught'], observed=result['observed'], total=result['total'],
                              queued=len(result['adjudication_queue'])), sort_keys=True))


if __name__ == '__main__':
    main()
