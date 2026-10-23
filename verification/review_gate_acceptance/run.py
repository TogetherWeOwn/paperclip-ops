#!/usr/bin/env python3
"""Run the paired acceptance contract against an explicit sibling-module source directory."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULES = ('review_gate_runtime', 'review_dedup', 'review_findings',
           'pr_controller', 'firstpass_review', 'review_gate')


def forbidden(*args, **kw):
    raise AssertionError('offline harness forbids network/process/provider side effects')


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--select', action='append', help='test_contract class or class.method; repeatable')
    parser.add_argument('--legacy', action='store_true', help='also run all fifteen source-bundle baseline tests')
    parser.add_argument('--json', type=Path, help='write case-level result evidence')
    args = parser.parse_args()
    source = args.source_dir.resolve(strict=True)
    for name in MODULES:
        if not (source / (name + '.py')).is_file():
            parser.error('missing sibling source module: ' + name)
    scratch = os.environ.get('PAPERCLIP_RUN_SCRATCH_DIR') or os.environ.get('PAPERCLIP_SCRATCH_DIR')
    if not scratch:
        parser.error('set PAPERCLIP_RUN_SCRATCH_DIR or PAPERCLIP_SCRATCH_DIR to an owned scratch directory')
    with tempfile.TemporaryDirectory(prefix='review-gate-offline-', dir=scratch) as root, ExitStack() as stack:
        # Never inherit real runtime paths, transports, focus or principals from the caller.
        stack.enter_context(patch.dict(os.environ, {'REVIEW_GATE_ROOT': root}, clear=True))
        for obj, name in [(subprocess, 'run'), (subprocess, 'Popen'),
                          (socket, 'socket'), (socket, 'create_connection'), (socket, 'getaddrinfo'),
                          (os, 'system'), (os, 'popen'), (os, 'fork'), (os, 'posix_spawn'),
                          (os, 'execv'), (os, 'execve')]:
            stack.enter_context(patch.object(obj, name, forbidden))
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source))
        for name in MODULES:
            sys.modules.pop(name, None)
            module = importlib.import_module(name)
            if Path(module.__file__).resolve() != source / (name + '.py'):
                raise AssertionError('source import escaped explicit directory: ' + name)
        # Source insertion is intentional; acceptance tests are separately located and immutable.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        contract = importlib.import_module('test_contract')
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        if args.select:
            for selection in args.select:
                suite.addTests(loader.loadTestsFromName(selection, contract))
        else:
            suite.addTests(loader.loadTestsFromModule(contract))
        if args.legacy:
            for name in ('test_review_gate', 'test_review_gate_fairness'):
                module = importlib.import_module(name)
                if Path(module.__file__).resolve() != source / (name + '.py'):
                    raise AssertionError('legacy test import escaped explicit directory')
                suite.addTests(loader.loadTestsFromModule(module))
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
        report = {'tests': result.testsRun, 'passed': result.passed,
                  'failures': [{'case': t.id(), 'trace': text} for t, text in result.failures],
                  'errors': [{'case': t.id(), 'trace': text} for t, text in result.errors],
                  'skipped': [{'case': t.id(), 'reason': reason} for t, reason in result.skipped],
                  'source_sha256': {name + '.py': hashlib.sha256((source / (name + '.py')).read_bytes()).hexdigest()
                                    for name in MODULES}}
        if args.json:
            args.json.write_text(json.dumps(report, indent=2) + '\n')
        return 0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1


if __name__ == '__main__':
    sys.exit(main())
