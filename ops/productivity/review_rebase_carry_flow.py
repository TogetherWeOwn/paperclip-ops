#!/usr/bin/env python3
"""Call-site logic for review-only rebase carry (SECURITY-SENSITIVE: decides a required check).

Pure of credentials and transport: every network or git-object operation is an injected
callable on `Env`, so the host passes its own authenticated client and tests pass fakes.
The equivalence proof itself lives in `review_rebase_carry`; this module adds what the proof
cannot see: App identity, genuine-source provenance, no chaining on a carried check, the
base-snapshot ledger, time-of-check/time-of-use re-reads, receipt verification, single-flight
publication, the fail-closed control, bounded audit lines, and the own-patch delta record the
review gate reads. Equivalence covers the REVIEW only; CI and every other required check
still gate the new head. It has no side effects at import and contains no credential code.
"""
import calendar
import fcntl
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

import review_rebase_carry as core

APP_ID = 5191093
CHECK = 'Paperclip Review'
FOREIGN_PREFIXES = ('paperclip-review-carry:v1:', 'paperclip-docs-carry:v1:',
                    'paperclip-docs-lane:v1:')
MODES = ('off', 'shadow', 'on')
ALLOWED_AUTHORS = {'Rick7C2': 'User', 'togetherweown[bot]': 'Bot', 'github-actions[bot]': 'Bot'}
MAX_ROWS = 1000            # check rows read for one head; at or over this the data is incomplete
SNAPSHOT_MAX_AGE = 900     # a base snapshot counts only within 15 min of the review completing
MAX_HEADS_PER_PR = 20
MAX_PRS = 500
MAX_DELTA_FILES = 500
MAX_STATE_BYTES = 2_000_000
UNDO = 'UNDO: PATCH this same App-owned check to failure; never alter rulesets or the 5/5 policy.'
REVIEW_ONLY = ('This is deterministic patch equivalence for the REVIEW only, not a new code review. '
               'CI and all other required checks remain required on this head.')
_SAFE = re.compile(r'[^A-Za-z0-9 _./:@#>=,()+\-]')


class FlowError(core.GitError):
    """Evidence or state unusable; the caller falls back to today's full review."""


class Busy(Exception):
    """Another pass holds the single-flight lock."""


class Paths:
    """Everything lives next to the other review-gate state files."""

    def __init__(self, productivity_dir):
        base = Path(productivity_dir)
        self.control = base / 'review-rebase-carry.json'
        self.kill = base / 'review-rebase-carry.OFF'
        self.state_dir = base / 'review-rebase-carry'
        self.ledger = self.state_dir / 'ledger.json'
        self.lock = self.state_dir / 'ledger.lock'
        self.delta_dir = base / 'review-rebase-delta'


def control_mode(paths, environ=None):
    """off | shadow | on. Absent, unreadable or malformed input means off, never on.

    The kill flag file and the env var can only restrict; only the control file can enable.
    """
    environ = os.environ if environ is None else environ
    try:
        if os.path.lexists(paths.kill):
            return 'off'
        with open(paths.control, 'rb') as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            return 'off'
        data = json.loads(raw.decode('utf-8'))
        mode = data.get('mode') if isinstance(data, dict) else None
        if mode not in ('shadow', 'on'):
            return 'off'
        cap = environ.get('REVIEW_REBASE_CARRY')
        if cap is not None:
            if cap not in MODES:
                return 'off'
            mode = MODES[min(MODES.index(mode), MODES.index(cap))]
        return mode
    except Exception:
        return 'off'


def _clean(value, limit=160):
    return _SAFE.sub('?', str(value))[:limit]


def audit(log, event, repo, number, old_head, new_head, old_base=None, new_base=None,
          patch_digest=None, reason=None):
    """One bounded line of SHAs, refs and a short reason. Never tokens, paths or payloads."""
    def base(b):
        return f"{_clean(b['ref'], 80)}@{b['sha']}" if isinstance(b, Mapping) else '-'
    line = (f'rebase-carry {event} repo={_clean(repo, 80)} pr={int(number)} '
            f'old_head={_clean(old_head, 40)} new_head={_clean(new_head, 40)} '
            f'old_base={base(old_base)} new_base={base(new_base)} '
            f'digest={_clean(patch_digest or "-", 64)} reason={_clean(reason or "-")}. {UNDO}')
    try:
        log(line)
    except Exception:
        pass  # a failing log must not change the decision
    return line


def _int(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    raise FlowError('malformed numeric id')


def _rows(checks):
    if not isinstance(checks, (list, tuple)) or len(checks) >= MAX_ROWS:
        raise FlowError('check inventory incomplete')
    for check in checks:
        if not isinstance(check, Mapping):
            raise FlowError('malformed check row')
    return checks


def _app_id(check):
    app = check.get('app')
    return app.get('id') if isinstance(app, Mapping) else None


def genuine_rows(checks, number, head, app_id=APP_ID):
    """Rows for core.latest_review: identity-scoped, NEWEST chosen before any verdict test.

    A row from our App on this head whose provenance is not a genuine review (wrong
    external_id, a carry namespace) is kept as status `untrusted`, so if it is the newest
    row it blocks carry instead of being filtered away to resurrect an older 5/5.
    """
    suffix = f':{number}:{head}'
    out = []
    for check in _rows(checks):
        if (check.get('name') != CHECK or _app_id(check) != app_id
                or check.get('head_sha') != head):
            continue
        ext = check.get('external_id')
        genuine = (isinstance(ext, str) and ext.endswith(suffix)
                   and not ext.startswith(FOREIGN_PREFIXES + (core.NAMESPACE,)))
        output = check.get('output')
        title = output.get('title') if isinstance(output, Mapping) else None
        out.append({'id': _int(check.get('id')), 'pr': number, 'head_sha': head,
                    'status': check.get('status') if genuine else 'untrusted',
                    'conclusion': check.get('conclusion'),
                    'title': title.strip() if isinstance(title, str) else None})
    return out


def any_rows(checks, number, head):
    """Every row of any kind (any app, status, conclusion or title) on this head."""
    return [{'id': _int(check.get('id')), 'pr': number, 'head_sha': head}
            for check in _rows(checks)]


def verify_carry_check(check, repo, number, head, app_id=APP_ID):
    """True only for the exact published carried check on this head (also the receipt test).

    C4: a receipt without a positive-int id never verifies; the caller then
    PATCHes to failure and falls back to a full review.
    """
    try:
        output = check.get('output') if isinstance(check, Mapping) else None
        check_id = check.get('id') if isinstance(check, Mapping) else None
        if type(check_id) is not int or check_id <= 0:
            return False
        return (check.get('name') == CHECK
                and _app_id(check) == app_id and check.get('head_sha') == head
                and check.get('status') == 'completed' and check.get('conclusion') == 'success'
                and check.get('external_id') == core.idempotency_key(repo, number, head)
                and isinstance(output, Mapping)
                and str(output.get('title', '')).startswith('5/5 carried: identical reviewed patch'))
    except Exception:
        return False


def _epoch(stamp):
    try:
        return calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError):
        return None


class Ledger:
    """Write-once per-head base snapshots, taken near the genuine review's completion.

    A `genuine` entry exists only when a genuine 5/5 was seen and the base snapshot was
    recorded within SNAPSHOT_MAX_AGE of that review completing. A moving branch read later
    is never substituted: such a head stays `other` and cannot be a carry source.
    """

    def __init__(self, paths, clock=time.time):
        self.paths, self.clock = paths, clock
        self.data = {'v': 1, 'prs': {}}
        self._held = None

    def __enter__(self):
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self._held = open(self.paths.lock, 'a')
        try:
            fcntl.flock(self._held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._held.close()
            self._held = None
            raise Busy() from None
        try:
            self._load()
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        if self._held:
            self._held.close()
        self._held = None

    def _load(self):
        try:
            raw = self.paths.ledger.read_bytes()
        except FileNotFoundError:
            return
        if len(raw) > MAX_STATE_BYTES:
            raise FlowError('ledger too large')
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or data.get('v') != 1 or not isinstance(data.get('prs'), dict):
            raise FlowError('ledger malformed')
        self.data = data

    def _save(self):
        prs = self.data['prs']
        if len(prs) > MAX_PRS:
            keep = sorted(prs, key=lambda k: prs[k].get('touched', 0))[-MAX_PRS:]
            self.data['prs'] = {k: prs[k] for k in keep}
        tmp = self.paths.ledger.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, sort_keys=True))
        os.replace(tmp, self.paths.ledger)

    def heads(self, repo, number):
        entry = self.data['prs'].get(f'{repo.lower()}#{number}') or {}
        return entry.get('heads', [])

    def _entry(self, repo, number):
        entry = self.data['prs'].setdefault(f'{repo.lower()}#{number}', {'heads': []})
        entry['touched'] = int(self.clock())
        return entry

    def find(self, repo, number, head):
        for item in self.heads(repo, number):
            if item.get('head') == head:
                return item
        return None

    def observe(self, repo, number, head, base, review):
        """Record `head` once; upgrade `other` to `genuine` only with a fresh snapshot.

        `review` is the newest genuine 5/5 row (with `completed_at`) or None.
        """
        entry = self._entry(repo, number)
        item = self.find(repo, number, head)
        fresh = False
        if review is not None:
            done = _epoch(review.get('completed_at'))
            fresh = done is not None and 0 <= self.clock() - done <= SNAPSHOT_MAX_AGE
        if item is None:
            item = {'head': head, 'kind': 'other', 'seen': int(self.clock())}
            entry['heads'] = (entry['heads'] + [item])[-MAX_HEADS_PER_PR:]
        if item['kind'] == 'other' and fresh:
            item.update(kind='genuine', base={'ref': base['ref'], 'sha': base['sha']},
                        check_id=review['id'])
        self._save()
        return item

    def mark_carried(self, repo, number, head, source, patch_digest, check_id):
        item = self.find(repo, number, head)
        if item is None or item['kind'] != 'other':
            raise FlowError('cannot mark carried')
        item.update(kind='carried', digest=patch_digest, check_id=check_id, source=source)
        self._save()

    def note(self, repo, number, head, token):
        """Remember the last logged decision so a repeated tick stays silent."""
        item = self.find(repo, number, head)
        if item is None or item.get('last') == token:
            return False
        item['last'] = token
        self._save()
        return True


def previous_source(ledger, repo, number, head):
    """(source_head, source_base) from the immediately previous observed head, or None.

    A carried previous head hops to ITS genuine source, which is re-verified live; a carried
    check itself is never accepted as review evidence, so there is no chaining.
    """
    prior = [i for i in ledger.heads(repo, number) if i.get('head') != head]
    if not prior:
        return None
    last = prior[-1]
    if last['kind'] == 'genuine':
        return last['head'], last['base']
    if last['kind'] == 'carried':
        source = last.get('source') or {}
        return source.get('head'), source.get('base')
    return None


class Env:
    """Injected capabilities. The host builds these from its authenticated client.

    The base methods all raise FlowError, so a bare Env() carries nothing and
    every decision falls back to today's full review.
    """
    git_dir = None

    def get_pr(self, number): raise FlowError('unconfigured environment')  # GET pulls/{n}
    def checks_for(self, sha): raise FlowError('unconfigured environment')  # ALL rows, complete or raise
    def ensure_objects(self, *shas): raise FlowError('unconfigured environment')  # fetch pinned commits
    def create_check(self, payload): raise FlowError('unconfigured environment')
    def patch_check(self, check_id, payload): raise FlowError('unconfigured environment')
    def now_iso(self): raise FlowError('unconfigured environment')
    def log(self, line): raise FlowError('unconfigured environment')


def _pr_view(pr, repo):
    if not isinstance(pr, Mapping) or pr.get('state') != 'open':
        raise FlowError('pull request not open')
    head, base = pr.get('head') or {}, pr.get('base') or {}
    user = pr.get('user') or {}
    base_repo = (base.get('repo') or {}).get('full_name')
    head_repo = (head.get('repo') or {}).get('full_name')
    if not (isinstance(base_repo, str) and isinstance(head_repo, str)
            and base_repo.lower() == repo.lower() == head_repo.lower()):
        raise FlowError('fork or foreign pull request')
    if ALLOWED_AUTHORS.get(user.get('login')) != user.get('type'):
        raise FlowError('author not trusted')
    if pr.get('draft'):
        raise FlowError('draft')
    new_base = {'ref': base.get('ref'), 'sha': base.get('sha')}
    core._base(new_base)
    core._sha(head.get('sha'))
    return head['sha'], new_base


def _delta_record(env, repo, number, source, head, new_base, mode):
    files = core.delta_files(env.git_dir, source['head'], head, source['base'], new_base, ())
    if not files or len(files) > MAX_DELTA_FILES:
        return None
    return {'v': 1, 'mode': mode, 'repo': repo.lower(), 'pr': number, 'head': head,
            'source_head': source['head'], 'old_base': source['base'], 'new_base': new_base,
            'files': files}


def write_delta(paths, record):
    paths.delta_dir.mkdir(parents=True, exist_ok=True)
    name = f"{record['repo'].split('/')[-1]}-{record['pr']}-{record['head'][:12]}.json"
    target = paths.delta_dir / name
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(record, sort_keys=True))
    os.replace(tmp, target)


def scoped_delta(paths, mode, repo, number, last_head, head, blocking, source_base=None):
    """Own-patch delta ∪ prior-blocking paths, or None meaning use the compare-based list.

    Union-only: never narrower than the changed own-patch files plus every prior blocker.
    `source_base` is the current base of the head that produced this record (`last_head`),
    as `{'ref':..., 'sha':...}`. When given, the record must have been computed against that
    same base or the call returns None, so a base that moved after the record was written
    falls back to the compare-based list instead of reusing a stale union. It defaults to
    None, which keeps the record's own `old_base` binding and leaves callers that cannot
    supply the source base on today's behavior.
    """
    try:
        if mode != 'on' or not isinstance(blocking, (list, tuple)):
            return None
        name = f"{repo.split('/')[-1].lower()}-{int(number)}-{core._sha(head)[:12]}.json"
        raw = (paths.delta_dir / name).read_bytes()
        if len(raw) > 262144:
            return None
        rec = json.loads(raw.decode('utf-8'))
        if (rec.get('v') != 1 or rec.get('mode') != 'on' or rec.get('repo') != repo.lower()
                or rec.get('pr') != int(number) or rec.get('head') != head
                or rec.get('source_head') != last_head
                or not isinstance(rec.get('files'), list) or not rec['files']
                or len(rec['files']) > MAX_DELTA_FILES):
            return None
        # The record is a record of one head pair on one source base. Bind it to that
        # base whenever the caller can name the source's current base, so a stale record
        # is never reused after the base moved.
        if source_base is not None:
            record_base = rec.get('old_base')
            if not isinstance(record_base, Mapping) or core._base(record_base) != core._base(source_base):
                return None
        files = {core._relative_path(f) for f in rec['files']}
        for finding in blocking:
            if not isinstance(finding, Mapping):
                return None
            files.add(core._relative_path(finding.get('path')))
        return sorted(files)
    except Exception:
        return None


def _publish(env, repo, number, source, head, base, proof, source_row):
    """TOCTOU re-read, publish once, verify the receipt. Returns ('carry'|'review', reason)."""
    key = core.idempotency_key(repo, number, head)
    if env.git_dir is None:
        return 'review', 'no git directory'
    new_head, new_base = _pr_view(env.get_pr(number), repo)
    if new_head != head or new_base != base:
        return 'review', 'pull request moved during verification'
    if any_rows(env.checks_for(head), number, head):
        return 'review', 'new head gained a check row during verification'
    fresh = core.latest_review(genuine_rows(env.checks_for(source['head']), number, source['head']),
                               number, source['head'])
    if fresh is None or fresh['id'] != source_row['id']:
        return 'review', 'source review changed during verification'
    summary = (f"{proof['summary']}\n\nSource: genuine Paperclip Review check `{source_row['id']}` "
               f"on `{source['head']}` (App `{APP_ID}`).\n\n"
               f"Old base: `{source['base']['ref']}@{proof['old_base']}`. "
               f"New base: `{base['ref']}@{proof['new_base']}`. Digest: `{proof['digest']}`.\n\n"
               f'{REVIEW_ONLY}')
    payload = {'name': CHECK, 'head_sha': head, 'external_id': key, 'status': 'completed',
               'conclusion': 'success', 'completed_at': env.now_iso(),
               'details_url': f'https://github.com/{repo}/pull/{number}',
               'output': {'title': proof['summary'], 'summary': summary}}
    try:
        saved = env.create_check(payload)
    except Exception:
        _invalidate(env, repo, number, head, key)
        return 'review', 'publication failed'
    if not verify_carry_check(saved, repo, number, head):
        _invalidate(env, repo, number, head, key, saved)
        return 'review', 'receipt failed verification'
    return 'carry', saved


def _invalidate(env, repo, number, head, key, saved=None):
    """Best effort UNDO: PATCH any check we may have created to failure."""
    try:
        ids = [saved.get('id')] if isinstance(saved, Mapping) and saved.get('id') else []
        if not ids:
            ids = [c.get('id') for c in env.checks_for(head)
                   if isinstance(c, Mapping) and c.get('external_id') == key and _app_id(c) == APP_ID]
        for check_id in ids:
            env.patch_check(check_id, {'status': 'completed', 'conclusion': 'failure',
                                       'completed_at': env.now_iso(),
                                       'output': {'title': 'Full re-review required',
                                                  'summary': 'Rebase carry invalidated; full review required.'}})
    except Exception:
        pass  # the gate still requests a review because no verified carry exists


def evaluate_pr(env, paths, repo, number, mode):
    """One decision for one PR. Never raises; any doubt is a full review (today's behavior)."""
    result = {'decision': 'off', 'reason': 'disabled'}
    if mode not in ('shadow', 'on'):
        return result
    head = old_head = None
    try:
        with Ledger(paths) as ledger:
            head, base = _pr_view(env.get_pr(number), repo)
            rows = env.checks_for(head)
            own = core.latest_review(genuine_rows(rows, number, head), number, head)
            if own is not None:
                done = next(c for c in rows if _int(c.get('id')) == own['id']).get('completed_at')
                own = {'id': own['id'], 'completed_at': done}
            ledger.observe(repo, number, head, base, own)
            if rows:
                carried = len(rows) == 1 and verify_carry_check(rows[0], repo, number, head)
                return {'decision': 'skip', 'reason': 'already carried' if carried else 'head has check rows'}
            source = previous_source(ledger, repo, number, head)
            if source is None or source[0] is None:
                return {'decision': 'skip', 'reason': 'no previous genuine source'}
            source = {'head': source[0], 'base': source[1]}
            old_head = source['head']
            old_rows = genuine_rows(env.checks_for(old_head), number, old_head)
            row = core.latest_review(old_rows, number, old_head)
            env.ensure_objects(old_head, source['base']['sha'], head, base['sha'])
            decision, info = core.decide(env.git_dir, number, old_head, head, source['base'], base,
                                         old_rows + any_rows(rows, number, head))
            if decision == 'review':
                reason = info['reason']
                record = None
                if reason == 'normalized own-patch changed' and mode == 'on':
                    record = _delta_record(env, repo, number, source, head, base, mode)
                    if record:
                        write_delta(paths, record)
                if ledger.note(repo, number, head, f'review:{reason}'):
                    audit(env.log, 'review-required' if mode == 'on' else 'would-review', repo, number,
                          old_head, head, source['base'], base, None,
                          reason + (f'; delta {len(record["files"])} file(s)' if record else ''))
                return {'decision': 'review', 'reason': reason}
            if mode == 'shadow':
                if ledger.note(repo, number, head, 'would-carry'):
                    audit(env.log, 'would-carry', repo, number, old_head, head, source['base'], base,
                          info['digest'], 'identical own-patch (shadow, nothing published)')
                return {'decision': 'would-carry', 'reason': 'shadow', 'proof': info}
            outcome, detail = _publish(env, repo, number, source, head, base, info, row)
            if outcome == 'carry':
                ledger.mark_carried(repo, number, head, source, info['digest'], detail.get('id'))
                audit(env.log, 'carried', repo, number, old_head, head, source['base'], base,
                      info['digest'], f"published check {detail.get('id')}")
                return {'decision': 'carry', 'reason': 'published', 'proof': info}
            audit(env.log, 'review-required', repo, number, old_head, head, source['base'], base,
                  info['digest'], detail)
            return {'decision': 'review', 'reason': detail}
    except Busy:
        return {'decision': 'skip', 'reason': 'busy'}
    except FlowError as exc:
        reason = str(exc)
    except Exception:
        reason = 'evidence error'
    if reason not in ('draft', 'author not trusted', 'fork or foreign pull request',
                      'pull request not open'):
        audit(env.log, 'review-required' if mode == 'on' else 'would-review', repo, number,
              old_head or '-', head or '-', None, None, None, reason)
    return {'decision': 'review', 'reason': reason}


def run_repo(env, paths, repo, numbers, mode):
    """Evaluate each listed open PR; returns counts. One PR's failure never stops the rest."""
    counts = {}
    for number in numbers:
        out = evaluate_pr(env, paths, repo, number, mode)
        counts[out['decision']] = counts.get(out['decision'], 0) + 1
    return counts


def gate_skips_review(sh, gh_env, repo, number, head, mode):
    """Gate side: True only in mode `on` with exactly one verified carried check on the head.

    Any doubt returns False and the gate requests a full review as it does today. `sh(argv,
    env=...)` is the gate's own subprocess helper (returncode/stdout).
    """
    try:
        if mode != 'on':
            return False
        core._sha(head)
        res = sh(['gh', 'api', f'repos/{repo}/commits/{head}/check-runs?check_name=Paperclip%20Review'
                  '&filter=all&per_page=100', '--jq',
                  '{total_count, check_runs: [.check_runs[] | {id, name, head_sha, status, conclusion, '
                  'external_id, app: {id: .app.id}, output: {title: .output.title}}]}'], env=gh_env)
        if res.returncode:
            return False
        data = json.loads(res.stdout)
        runs = data.get('check_runs')
        return (isinstance(runs, list) and len(runs) == 1 and data.get('total_count') == 1
                and verify_carry_check(runs[0], repo, int(number), head))
    except Exception:
        return False
