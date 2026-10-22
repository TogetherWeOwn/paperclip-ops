#!/usr/bin/env python3
"""Query aggregated nextest flake evidence before deciding whether to rerun."""

import json
import os
import sys
from pathlib import Path

MIN_RUNS = 5
FLIP_RATE_THRESHOLD = 0.2


class LedgerUnavailable(Exception):
    """The evidence cannot support a safe flaky/non-flaky decision."""


class LedgerWriteError(Exception):
    """A flake record could not be persisted; the caller must queue and retry."""


def resolve_ledger_path():
    """Return the configured ledger path without touching HOME when overridden."""
    override = os.environ.get('FLAKE_LEDGER_PATH')
    if override is not None:
        return Path(override)
    try:
        return Path.home() / '.local/share/flake-ledger.json'
    except RuntimeError as exc:
        raise LedgerUnavailable(f'cannot determine default ledger path: {exc}') from exc


def read_ledger(path):
    """Read aggregate JSON or JSONL ledger records."""
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise LedgerUnavailable(f'cannot read {path}: {exc}') from exc
    if not text.strip():
        raise LedgerUnavailable(f'ledger is empty: {path}')

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        records = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LedgerUnavailable(f'invalid JSON on line {line_number}: {exc}') from exc
    else:
        if isinstance(document, dict) and 'tests' in document:
            records = document['tests']
        elif isinstance(document, (dict, list)):
            records = document if isinstance(document, list) else [document]
        else:
            raise LedgerUnavailable('ledger must contain an object, array, or JSONL records')
        if not isinstance(records, list):
            raise LedgerUnavailable('ledger tests field must be an array')

    rows = []
    for line_number, row in enumerate(records, 1):
        if not isinstance(row, dict):
            raise LedgerUnavailable(f'record {line_number} must be a JSON object')
        if not all(isinstance(row.get(key), str) and row[key].strip()
                   for key in ('repo', 'job', 'test')):
            raise LedgerUnavailable(f'record {line_number} is missing repo, job, or test')
        flips, observations = row.get('flips'), row.get('observations')
        if (type(flips) is not int or type(observations) is not int or
                flips < 0 or observations < 0 or flips > observations):
            raise LedgerUnavailable(f'record {line_number} has invalid flips or observations')
        rows.append(row)
    if not rows:
        raise LedgerUnavailable(f'ledger has no records: {path}')
    return rows


def eligibility(rows, repo, job_or_test):
    """Return yes/no for one unambiguous evidence row, otherwise raise."""
    repo_rows = [row for row in rows if row['repo'] == repo]
    test_matches = [row for row in repo_rows if row['test'] == job_or_test]
    job_matches = [row for row in repo_rows if row['job'] == job_or_test]
    job_summaries = [row for row in job_matches if row['test'] == f'job {job_or_test}']

    candidates = []
    if test_matches:
        if len(test_matches) != 1:
            raise LedgerUnavailable(
                f'ambiguous ledger evidence for {repo} / {job_or_test}: '
                f'{len(test_matches)} matching test rows'
            )
        candidates.append(test_matches[0])
    if job_matches:
        if job_summaries:
            if len(job_summaries) != 1:
                raise LedgerUnavailable(
                    f'ambiguous ledger evidence for {repo} / {job_or_test}: '
                    f'{len(job_summaries)} matching job summary rows'
                )
            candidates.append(job_summaries[0])
        else:
            if len(job_matches) != 1:
                raise LedgerUnavailable(
                    f'ambiguous ledger evidence for {repo} / {job_or_test}: '
                    f'{len(job_matches)} matching job rows'
                )
            candidates.append(job_matches[0])
    if not candidates:
        raise LedgerUnavailable(f'no ledger evidence for {repo} / {job_or_test}')
    if len(candidates) == 2 and candidates[0] is not candidates[1]:
        raise LedgerUnavailable(
            f'ambiguous ledger evidence for {repo} / {job_or_test}: '
            'test and job matches identify different rows'
        )

    flips = candidates[0]['flips']
    observations = candidates[0]['observations']
    if observations < MIN_RUNS:
        raise LedgerUnavailable(
            f'insufficient evidence for {repo} / {job_or_test}: '
            f'{observations} runs; need at least {MIN_RUNS}'
        )
    return 'yes' if flips / observations > FLIP_RATE_THRESHOLD else 'no'


def _coerce_flake_entry(entry):
    """Validate a pr_controller_ci flake entry into (repo, job, test)."""
    if not isinstance(entry, dict):
        raise LedgerWriteError('flake entry must be a JSON object')
    values = {}
    for key in ('repo', 'job', 'test'):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LedgerWriteError(f'flake entry is missing {key}')
        values[key] = value
    return values['repo'], values['job'], values['test']


def record_flake(path, entry):
    """Persist one fail-then-pass flake into the aggregate ledger.

    Each recorded flake contributes the failing run and the passing run, so
    the matching row gains two observations and one flip. A missing ledger
    file is created with a single flips=1/observations=2 row; a corrupt file
    raises LedgerWriteError so the caller queues the entry instead of
    overwriting evidence it cannot parse. Writes are atomic (temp + rename).
    Returns the updated row.
    """
    repo, job, test = _coerce_flake_entry(entry)
    path = Path(path)
    try:
        rows = read_ledger(path)
    except LedgerUnavailable as exc:
        message = str(exc)
        if 'cannot read' in message or 'ledger is empty' in message:
            rows = []
        else:
            raise LedgerWriteError(str(exc)) from exc
    matches = [row for row in rows
               if row['repo'] == repo and row['job'] == job
               and row['test'] == test]
    if len(matches) > 1:
        raise LedgerWriteError(
            f'ambiguous ledger evidence for {repo} / {job} / {test}: '
            f'{len(matches)} matching rows')
    if matches:
        row = matches[0]
        row['flips'] += 1
        row['observations'] += 2
    else:
        row = {'repo': repo, 'job': job, 'test': test,
               'flips': 1, 'observations': 2}
        rows.append(row)
    body = ''.join(json.dumps(item) + '\n' for item in rows)
    tmp = path.with_name(path.name + f'.tmp-{os.getpid()}')
    try:
        tmp.write_text(body, encoding='utf-8')
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise LedgerWriteError(f'cannot write {path}: {exc}') from exc
    return dict(row)


class LedgerClient:
    """Host adapter behind the verdict()/record_flake() contract.

    ``verdict`` never raises: missing or unreadable evidence returns
    'unavailable', which the controller treats fail-safe (rerun once, then
    wake). ``record_flake`` raises LedgerWriteError/LedgerUnavailable on
    failure so the caller can queue the entry for a later tick.
    """

    def __init__(self, path=None):
        self._path = Path(path) if path is not None else None

    def _resolve(self):
        if self._path is not None:
            return self._path
        return resolve_ledger_path()

    def verdict(self, repo, job_or_test):
        try:
            return eligibility(read_ledger(self._resolve()), repo, job_or_test)
        except LedgerUnavailable:
            return 'unavailable'

    def record_flake(self, entry):
        return record_flake(self._resolve(), entry)


def quarantine_card(*, find_card, post, company_id, payload):
    """Open or update the single [flake] card for one job/test.

    ``find_card(payload)`` returns the existing card id or None (host search
    for the payload title). ``post(method, path, body)`` follows the
    ``pr_controller_wakes.post_wake`` convention. Returns the card id.
    """
    if not isinstance(payload, dict) or '[flake]' not in str(
            payload.get('title') or ''):
        raise ValueError('quarantine payload must carry a [flake] title')
    existing = find_card(payload)
    if existing:
        code, _ = post('POST', f'issues/{existing}/comments', {
            'body': ('Flake count update: '
                     f"{payload.get('count')} flakes in "
                     f"{payload.get('window_days', 7)} days for "
                     f"{payload.get('repo')} {payload.get('workflow')}/"
                     f"{payload.get('job')}. Runs: "
                     f"{', '.join(payload.get('run_ids') or []) or 'n/a'}.")})
        if not str(code).startswith('2'):
            raise LedgerWriteError(f'flake card update failed: HTTP {code}')
        return existing
    code, body = post('POST', f'companies/{company_id}/issues', {
        'title': payload['title'],
        'description': (
            f"Flaky job/test: {payload.get('repo')} "
            f"{payload.get('workflow')}/{payload.get('job')}"
            + (f" / {payload.get('test')}"
               if payload.get('test') else '')
            + f"\n\n{payload.get('count')} fail-then-pass reruns in "
            f"{payload.get('window_days', 7)} days. Runs: "
            f"{', '.join(payload.get('run_ids') or []) or 'n/a'}.\n\n"
            'Fix or quarantine the test; the CI-rerun controller keeps '
            'rerunning it once per failure until then.'),
    })
    if not str(code).startswith('2'):
        raise LedgerWriteError(f'flake card create failed: HTTP {code}')
    try:
        return str(json.loads(body)['id'])
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise LedgerWriteError(
            f'flake card create returned no id: HTTP {code}') from exc


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != 'is_flaky' or not args[1].strip() or not args[2].strip():
        print('usage: flake_ledger.py is_flaky <repo> <job-or-test>', file=sys.stderr)
        return 2
    _, repo, job_or_test = args
    try:
        result = eligibility(read_ledger(resolve_ledger_path()), repo, job_or_test)
    except LedgerUnavailable as exc:
        print(f'unavailable: {exc}')
        return 2
    print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
