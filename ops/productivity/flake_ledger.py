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
