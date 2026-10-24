#!/usr/bin/env python3
"""Query aggregated nextest flake evidence before deciding whether to rerun."""

import datetime
import json
import os
import sys
from pathlib import Path

MIN_RUNS = 5
FLIP_RATE_THRESHOLD = 0.2
REPORT_SCHEMA_VERSION = 1
REPORT_DEFAULT_TOP = 10
REPORT_MAX_TOP = 100


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
    """Validate a pr_controller_ci flake entry into (repo, job, test).

    A missing or blank test name is a job-level flake; it maps to the
    job-summary row convention (``job <job>``) that ``eligibility``
    already reads, so host check rows without a test name still persist.
    """
    if not isinstance(entry, dict):
        raise LedgerWriteError('flake entry must be a JSON object')
    values = {}
    for key in ('repo', 'job'):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LedgerWriteError(f'flake entry is missing {key}')
        values[key] = value
    test = entry.get('test')
    if test is None or (isinstance(test, str) and not test.strip()):
        test = f"job {values['job']}"
    elif not isinstance(test, str):
        raise LedgerWriteError('flake entry test must be a string')
    values['test'] = test
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
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        raw = ''
    except (OSError, UnicodeError) as exc:
        raise LedgerWriteError(f'cannot read {path}: {exc}') from exc
    if not raw.strip():
        rows = []
    else:
        try:
            rows = read_ledger(path)
        except LedgerUnavailable as exc:
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


FULL_USAGE = """usage: flake_ledger.py is_flaky <repo> <job-or-test>
       flake_ledger.py report <repo> [--top N] [--min-observations M] [--json] [--dry-run]

Rank ledger rows for <repo> by flip rate (flips/observations), highest first.
Ties break by observations (highest first), then job and test names (A-Z).

report options:
  --top N               rank at most N rows (integer 1-100, default 10)
  --min-observations M  only rank rows with M+ observations (positive integer,
                        default 5); rows below the gate list as insufficient
  --json                print a versioned JSON envelope (schema_version 1)
  --dry-run             no-op alias: report is always read-only
  -h, --help            print this usage

The report is read-only: it never writes the ledger, reruns tests, or files
cards. Duplicate or ambiguous rows make the whole report unavailable."""


class ReportUsageError(Exception):
    """A report invocation refused with usage (exit 2)."""


class ReportHelpRequested(Exception):
    """Internal signal: print usage on stdout and exit 0."""


def report_row_id(repo, job, test):
    """Escape percent and slash per component to preserve tuple boundaries."""
    return '/'.join(value.replace('%', '%25').replace('/', '%2F')
                    for value in (repo, job, test))


def validate_report_rows(rows, repo):
    """Return repo rows after strict report validity checks.

    Mirrors ``is_flaky``: duplicate (job, test) pairs or a value matching
    one row as a test and another row as a job make the whole report
    unavailable, never combined. A row whose own test equals its own job
    is one row, not a collision.
    """
    repo_rows = [row for row in rows if row['repo'] == repo]
    if not repo_rows:
        raise LedgerUnavailable(f'no ledger evidence for {repo}')
    seen = set()
    for row in repo_rows:
        key = (row['job'], row['test'])
        if key in seen:
            raise LedgerUnavailable(
                f'duplicate ledger evidence for {repo}: '
                f"job {row['job']!r} test {row['test']!r} appears more than once"
            )
        seen.add(key)
    values = set()
    for row in repo_rows:
        values.add(row['test'])
        values.add(row['job'])
    for value in sorted(values):
        test_hits = [index for index, row in enumerate(repo_rows)
                     if row['test'] == value]
        job_hits = [index for index, row in enumerate(repo_rows)
                    if row['job'] == value]
        if test_hits and job_hits and len(set(test_hits) | set(job_hits)) > 1:
            raise LedgerUnavailable(
                f'ambiguous ledger evidence for {repo} / {value}: '
                'test and job matches identify different rows'
            )
    return repo_rows


def rank_report_rows(repo_rows, top, min_observations):
    """Split repo rows into (ranked, insufficient, truncated_count).

    Eligible rows sort by flip rate descending, observations descending,
    then job and test names ascending; ``top`` truncates only the ranked
    set. Sub-gate rows sort by job/test ascending for determinism and are
    never ranked, regardless of ``top``.
    """
    eligible = [row for row in repo_rows
                if row['observations'] >= min_observations]
    insufficient = [row for row in repo_rows
                    if row['observations'] < min_observations]
    eligible.sort(key=lambda row: (-row['flips'] / row['observations'],
                                   -row['observations'],
                                   row['job'], row['test']))
    insufficient.sort(key=lambda row: (row['job'], row['test']))
    truncated = max(0, len(eligible) - top)
    return eligible[:top], insufficient, truncated


def report_envelope(*, repo, top, min_observations, ranked, insufficient,
                    excluded, error):
    """Build the versioned JSON envelope for a report result."""
    rows = [{'id': report_row_id(repo, row['job'], row['test']),
             'job': row['job'], 'test': row['test'],
             'flips': row['flips'], 'observations': row['observations'],
             'rate': row['flips'] / row['observations']}
            for row in ranked]
    short = [{'id': report_row_id(repo, row['job'], row['test']),
              'job': row['job'], 'test': row['test'],
              'flips': row['flips'], 'observations': row['observations']}
             for row in insufficient]
    return {
        'schema_version': REPORT_SCHEMA_VERSION,
        'repo': repo,
        'generated_at': datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        'top': top,
        'min_observations': min_observations,
        'rows': rows,
        'insufficient': short,
        'excluded': excluded,
        'error': error,
    }


def format_report_text(*, ranked, insufficient, min_observations):
    """Format ranked rows, then insufficient rows, one greppable line each."""
    lines = []
    for rank, row in enumerate(ranked, 1):
        rate = row['flips'] / row['observations']
        lines.append(
            f"{rank}. {row['job']} / {row['test']}: "
            f"flips={row['flips']} observations={row['observations']} "
            f"rate={rate:.4f}")
    for row in insufficient:
        lines.append(
            f"insufficient: {row['job']} / {row['test']}: "
            f"flips={row['flips']} observations={row['observations']} "
            f"(need >= {min_observations})")
    return ''.join(line + '\n' for line in lines)


def _parse_top(value):
    try:
        top = int(value, 10)
    except (TypeError, ValueError):
        raise ReportUsageError(
            f'invalid --top {value!r}: must be an integer 1-{REPORT_MAX_TOP}'
        ) from None
    if not 1 <= top <= REPORT_MAX_TOP:
        raise ReportUsageError(
            f'invalid --top {value!r}: must be an integer 1-{REPORT_MAX_TOP}'
        )
    return top


def _parse_min_observations(value):
    try:
        minimum = int(value, 10)
    except (TypeError, ValueError):
        raise ReportUsageError(
            f'invalid --min-observations {value!r}: must be a positive integer'
        ) from None
    if minimum < 1:
        raise ReportUsageError(
            f'invalid --min-observations {value!r}: must be a positive integer'
        )
    return minimum


def _parse_report_args(tokens):
    """Parse report args into (repo, top, min_observations, as_json).

    ``--dry-run`` is accepted and ignored: the report never writes, so the
    flag is a documented no-op alias. Raises ReportUsageError (exit 2) or
    ReportHelpRequested (exit 0).
    """
    top = REPORT_DEFAULT_TOP
    min_observations = MIN_RUNS
    as_json = False
    positionals = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, separator, inline = token.partition('=')
        if name in ('--top', '--min-observations'):
            if separator:
                value = inline
            else:
                index += 1
                if index >= len(tokens):
                    raise ReportUsageError(f'{name} requires a value')
                value = tokens[index]
            if name == '--top':
                top = _parse_top(value)
            else:
                min_observations = _parse_min_observations(value)
        elif token == '--json':
            as_json = True
        elif token == '--dry-run':
            pass
        elif token in ('-h', '--help'):
            raise ReportHelpRequested()
        elif token.startswith('-') and token != '-':
            raise ReportUsageError(f'unknown flag: {token}')
        else:
            positionals.append(token)
        index += 1
    if len(positionals) != 1 or not positionals[0].strip():
        raise ReportUsageError('report requires exactly one repo')
    return positionals[0], top, min_observations, as_json


def cmd_report(tokens):
    """Run the read-only ranked report; return the process exit code."""
    try:
        repo, top, min_observations, as_json = _parse_report_args(tokens)
    except ReportHelpRequested:
        print(FULL_USAGE)
        return 0
    except ReportUsageError as exc:
        print(f'{exc}', file=sys.stderr)
        print(FULL_USAGE, file=sys.stderr)
        return 2
    try:
        repo_rows = validate_report_rows(
            read_ledger(resolve_ledger_path()), repo)
    except LedgerUnavailable as exc:
        if as_json:
            print(json.dumps(report_envelope(
                repo=repo, top=top, min_observations=min_observations,
                ranked=[], insufficient=[], excluded=[],
                error={'code': 'unavailable', 'retryable': False,
                       'next_action': 'fix the ledger evidence and re-run '
                                      'the report; until then treat the '
                                      'failure as real',
                       'detail': str(exc)}), indent=2))
            print(f'unavailable: {exc}', file=sys.stderr)
        else:
            print(f'unavailable: {exc}')
        return 1
    ranked, insufficient, truncated = rank_report_rows(
        repo_rows, top, min_observations)
    excluded = []
    if insufficient:
        excluded.append({'reason': 'insufficient_observations',
                         'count': len(insufficient)})
    if truncated:
        excluded.append({'reason': 'top_truncated', 'count': truncated})
    if as_json:
        print(json.dumps(report_envelope(
            repo=repo, top=top, min_observations=min_observations,
            ranked=ranked, insufficient=insufficient, excluded=excluded,
            error=None), indent=2))
    else:
        sys.stdout.write(format_report_text(
            ranked=ranked, insufficient=insufficient,
            min_observations=min_observations))
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == 'is_flaky':
        if len(args) != 3 or not args[1].strip() or not args[2].strip():
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
    if args and args[0] == 'report':
        return cmd_report(args[1:])
    if args and args[0] in ('-h', '--help'):
        print(FULL_USAGE)
        return 0
    print(FULL_USAGE, file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
