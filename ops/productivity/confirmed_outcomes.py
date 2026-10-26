#!/usr/bin/env python3
"""Small outcome vocabulary for the existing status-only reconciler writes.

The three endpoints return HTTP 200 on completion. Callers discard response
bodies and capture only curl's %{http_code}; a completed process alone is not
confirmation. This module does no I/O, HTTP, credential loading or retries.
"""
import argparse
from dataclasses import asdict, dataclass
import json
import re
import sys

VERSION = '1'
OUTCOMES = ('confirmed', 'definite_failure', 'unknown')


@dataclass(frozen=True)
class Outcome:
    outcome: str
    code: str
    next_action: str
    http_status: str | None = None

    @property
    def confirmed(self):
        return self.outcome == 'confirmed'


def from_curl(result=None):
    """None represents TimeoutExpired; never retain stdout/stderr or argv."""
    if result is None:
        return Outcome('unknown', 'process_timeout', 'reconcile_target_before_retry')
    rc = getattr(result, 'returncode', None)
    if rc == 28:
        return Outcome('unknown', 'curl_timeout', 'reconcile_target_before_retry')
    if rc in (5, 6, 7):
        return Outcome('definite_failure', f'curl_exit_{rc}', 'check_transport_before_next_tick')
    if rc != 0:
        return Outcome('unknown', 'curl_transport_error', 'reconcile_target_before_retry')
    text = result.stdout if isinstance(result.stdout, str) else ''
    status = text.strip()
    if not re.fullmatch(r'[1-5][0-9]{2}', status):
        return Outcome('unknown', 'invalid_http_status', 'reconcile_target_before_retry')
    if status == '200':
        return Outcome('confirmed', 'http_200', 'none', status)
    if status[0] in '45':
        return Outcome('definite_failure', f'http_{status}', 'inspect_refusal_before_next_tick', status)
    return Outcome('unknown', f'unexpected_http_{status}', 'reconcile_target_before_retry', status)


class Summary:
    def __init__(self, caller, enabled=False):
        self.caller = caller
        self.enabled = enabled
        self.actions = []
        self.planned = 0
        self.errors = []

    def add(self, action, target, outcome):
        self.actions.append({'action': action, 'target': target, **asdict(outcome)})

    def error(self, code):
        self.errors.append(code)

    def finish(self):
        counts = {key: 0 for key in ('attempted', *OUTCOMES)}
        for action in self.actions:
            counts['attempted'] += 1
            counts[action['outcome']] += 1
        failed = bool(counts['definite_failure'] or counts['unknown'] or self.errors)
        if self.enabled:
            print(json.dumps({'version': VERSION, 'caller': self.caller,
                              'actions': self.actions, 'counts': counts,
                              'planned': self.planned, 'errors': self.errors,
                              'next_action': 'inspect_action_results' if failed else 'none'},
                             separators=(',', ':')))
        return int(failed)


def options(description, *, apply=False, dry_run=False, json_summary=False,
            default_apply=False, argv=None):
    """Keep callable main() compatible; the actual CLI rejects unknown flags."""
    parser = argparse.ArgumentParser(description=description, allow_abbrev=False)
    parser.add_argument('--apply', action='store_true', help='apply eligible mutations; count only confirmed HTTP 200')
    parser.add_argument('--dry-run', action='store_true', help='plan without control-plane or success-state writes')
    parser.add_argument('--json-summary', action='store_true', help=f'append version {VERSION} JSON outcome summary')
    if argv is None:
        # Imported callers have historically exposed module-level flags. Unit
        # runners may own other argv entries; only explicit reconciler flags apply.
        argv = [arg for arg in sys.argv[1:] if arg in ('--apply', '--dry-run', '--json-summary', '--help', '-h')]
    args = parser.parse_args(argv)
    explicit_apply = apply or args.apply
    args.dry_run = dry_run or args.dry_run
    args.json_summary = json_summary or args.json_summary
    args.refused = explicit_apply and args.dry_run
    args.apply = (explicit_apply or default_apply) and not args.dry_run
    if args.refused:
        print('REFUSED: --apply and --dry-run are mutually exclusive', file=sys.stderr)
    return args


def selftest():
    """Executable vocabulary checks, with no stores or subprocesses."""
    from types import SimpleNamespace
    cases = [('200', 0, 'confirmed'), ('403', 0, 'definite_failure'),
             ('500', 0, 'definite_failure'), ('000', 7, 'definite_failure'),
             ('200', 7, 'definite_failure'), ('000', 28, 'unknown'),
             ('200', 28, 'unknown'), ('200', 56, 'unknown'),
             ('000', 0, 'unknown'),
             ('', 0, 'unknown'), ('OK', 0, 'unknown'), ('204', 0, 'unknown')]
    for stdout, returncode, expected in cases:
        result = from_curl(SimpleNamespace(stdout=stdout, returncode=returncode))
        if result.outcome != expected:
            raise SystemExit('outcome_selftest_failed')
    if from_curl().outcome != 'unknown':
        raise SystemExit('timeout_selftest_failed')
    print(f'confirmed outcomes version {VERSION}: {", ".join(OUTCOMES)}; {len(cases) + 1} checks passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selftest', action='store_true', required=True, help='run offline outcome checks')
    parser.parse_args()
    selftest()
