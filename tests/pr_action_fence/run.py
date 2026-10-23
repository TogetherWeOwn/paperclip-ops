#!/usr/bin/env python3
"""Run offline acceptance; RED always exits nonzero. No expected-failure bypass.

The compact JSON summary omits raw transport argv, API bodies and filesystem
tracebacks. Use the named unittest/node commands for detailed local diagnostics.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.dont_write_bytecode = True


def test_identity(test):
    return test.test_case.id() if hasattr(test, 'test_case') else test.id()


def python_suite(controls=False):
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern='test_*.py')
    if controls:
        def leaves(value):
            for item in value:
                if isinstance(item, unittest.TestSuite):
                    yield from leaves(item)
                else:
                    yield item
        suite = unittest.TestSuite(test for test in leaves(suite) if (
            test.id().startswith('test_python_boundaries.') and 'unrelated_' in test.id())
            or 'ContainmentSelfTest.' in test.id())
    result = unittest.TestResult()
    suite.run(result)
    failure_ids = sorted({test_identity(test) for test, _ in result.failures})
    error_ids = sorted({test_identity(test) for test, _ in result.errors})
    return {'tests': result.testsRun, 'passed': result.testsRun - len(failure_ids) - len(error_ids),
            'failed_tests': len(failure_ids), 'failure_records_including_subtests': len(result.failures),
            'errors': len(result.errors), 'skipped': len(result.skipped),
            'behavioral_failures': [name for name in failure_ids if name.startswith('test_python_boundaries.')],
            'unimplemented_contract_failures': [name for name in failure_ids if not name.startswith('test_python_boundaries.')],
            'error_tests': error_ids, 'successful': result.wasSuccessful()}


def node_suite(scratch, controls=False):
    env = {key: os.environ[key] for key in ['PATH', 'HOME'] if key in os.environ}
    env.update(PAPERCLIP_SCRATCH_DIR=scratch, TMPDIR=scratch)
    argv = ['node', '--import', './tests/pr_action_fence/offline-preload.mjs',
            '--test', '--test-reporter=tap']
    if controls:
        argv += ['--test-name-pattern=positive control|unrelated.*control|unrelated positive|unrelated subjects|disable-only|delayed old-head|offline containment']
    argv += [str(path.relative_to(ROOT)) for path in sorted(HERE.glob('*.test.mjs'))]
    result = subprocess.run(argv, cwd=ROOT, env=env, text=True, capture_output=True, timeout=120)
    totals = {name: int(re.search(r'^# ' + name + r' (\d+)$', result.stdout, re.M).group(1))
              for name in ['tests', 'pass', 'fail', 'cancelled', 'skipped']
              if re.search(r'^# ' + name + r' (\d+)$', result.stdout, re.M)}
    failures = re.findall(r'^not ok \d+ - (.*)$', result.stdout, re.M)
    return {**totals, 'failure_tests': failures, 'successful': result.returncode == 0,
            'harness_error': not bool(totals), 'exit_code': result.returncode}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['all', 'python', 'node'], default='all')
    parser.add_argument('--controls', action='store_true', help='diagnostic controls only, NOT full acceptance')
    parser.add_argument('--summary-json', type=Path)
    args = parser.parse_args()
    scratch = os.environ.get('PAPERCLIP_RUN_SCRATCH_DIR') or os.environ.get('PAPERCLIP_SCRATCH_DIR')
    if not scratch or not Path(scratch).is_dir():
        parser.error('Set PAPERCLIP_SCRATCH_DIR to an existing run-owned scratch directory')
    report = {'schema_version': 1, 'offline_only': True, 'runtime_installation_proof': False,
              'controls_only': args.controls}
    if args.suite in ['all', 'python']:
        report['python'] = python_suite(args.controls)
    if args.suite in ['all', 'node']:
        report['node'] = node_suite(scratch, args.controls)
    output = json.dumps(report, indent=2) + '\n'
    print(output, end='')
    if args.summary_json:
        target = args.summary_json.resolve()
        if not target.is_relative_to(Path(scratch).resolve()):
            parser.error('summary output must be inside run-owned scratch')
        target.write_text(output)
    return int(any(not value['successful'] for key, value in report.items() if key in ['python', 'node']))


if __name__ == '__main__':
    raise SystemExit(main())
