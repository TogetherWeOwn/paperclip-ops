#!/usr/bin/env python3
"""Independent offline review-result acceptance; implementation is never copied.

Run unchanged against a repository checkout, not a directory of test edits:
    python3 verification/test_review_result_fallback.py --source-dir /path/to/checkout

Scope: the tracked REST CheckRun consumers resolve_outdated_threads.review_evidence
(and its head_is_5of5 wrapper) and review_carry.latest_review. Here 'body' means
GitHub CheckRun output.summary; output.text is not a second chance at a verdict.
Only missing, null or empty titles may use the first body line. A nonempty
adverse/unknown title is authoritative. All existing consumer gates must hold.

This is a test-author handoff, intentionally red before implementation. It does
not qualify the absent host PR-controller state machine, firstpass state-file
policy, deterministic docs-lane policy, or a review producer's rendering. The
only host-library double supplies the STATE_DIR needed to import review_carry;
no selection/parsing function is replaced. All transport is fenced or canned.
"""
import argparse
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.request

HEAD = 'a' * 40
OLD_HEAD = 'b' * 40
NUMBER = 17
APP_ID = 5191093
MISSING = object()
SOURCE_FILES = ('ops/productivity/resolve_outdated_threads.py', 'ops/sdlc/review_carry.py')


def forbidden(*args, **kwargs):
    raise AssertionError('offline acceptance forbids network/process/host actions')


def load_source(root, relative, alias):
    """Load an actual consumer from the selected tree, not an acceptance shim."""
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise AssertionError('source module escaped selected source directory')
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def check_run(title='5/5', body='Review details', **updates):
    output = {'summary': body, 'text': 'Detailed report, not the verdict'}
    if title is not MISSING:
        output['title'] = title
    return {'id': 100, 'name': 'Paperclip Review', 'app': {'id': APP_ID},
            'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success',
            'started_at': '2026-10-20T10:00:00Z',
            'external_id': f'review:{NUMBER}:{HEAD}', 'output': output, **updates}


def rows():
    """Hand-derived expectations: each row names a parsing/gate regression."""
    yield 'explicit_score_is_still_accepted', [check_run()], HEAD, True
    yield 'explicit_score_ignores_body', [check_run(body='CHANGES REQUIRED\n4/5')], HEAD, True
    yield 'trimmed_existing_title_still_accepted', [check_run(title=' 5/5 ')], HEAD, True
    for label, title in (('missing', MISSING), ('null', None), ('empty', '')):
        for ending, body in (('single_line', '5/5'), ('lf', '5/5\nDetails'),
                             ('crlf', '5/5\r\nDetails')):
            yield f'{label}_title_uses_first_body_line_{ending}', [check_run(title, body)], HEAD, True
        for case, body in (('absent_body', MISSING), ('null_body', None), ('empty_body', ''),
                           ('blank_first_line', '\n5/5'), ('crlf_blank_first_line', '\r\n5/5'),
                           ('unknown_first_line', 'Details\n5/5'),
                           ('adverse_first_line', 'CHANGES REQUIRED\n5/5'),
                           ('lower_score_first_line', '4/5\n5/5'),
                           ('score_with_adverse_verdict', '5/5 CHANGES REQUIRED'),
                           ('not_exact_score', '5/50')):
            run = check_run(title, None if body is MISSING else body)
            if body is MISSING:
                del run['output']['summary']
            # A favorable detailed report must not rescue the body or its first line.
            run['output']['text'] = '5/5'
            yield f'{label}_title_rejects_{case}', [run], HEAD, False
        for field, values in (('status', ('queued', 'in_progress', None, MISSING)),
                              ('conclusion', ('failure', 'action_required', 'neutral', 'skipped',
                                              'cancelled', 'timed_out', None, MISSING)),
                              ('head_sha', (OLD_HEAD, '', None, MISSING))):
            for index, value in enumerate(values):
                run = check_run(title, '5/5\nDetails')
                if value is MISSING:
                    del run[field]
                else:
                    run[field] = value
                yield f'{label}_fallback_preserves_{field}_gate_{index}', [run], HEAD, False
        for case, app in (('foreign_app', {'id': 1234567}), ('missing_app_id', {}),
                          ('null_app_id', {'id': None}),
                          ('spoofed_slug', {'id': 1234567, 'slug': 'togetherweown-reviewer'})):
            yield f'{label}_fallback_rejects_{case}', [check_run(title, '5/5', app=app)], HEAD, False
        run = check_run(title, '5/5')
        del run['app']
        yield f'{label}_fallback_rejects_missing_app', [run], HEAD, False
        yield f'{label}_fallback_rejects_wrong_check_name', [check_run(title, '5/5', name='ci-ok')], HEAD, False
        for case, head in (('empty', ''), ('null', None), ('stale', OLD_HEAD)):
            yield f'{label}_fallback_rejects_{case}_expected_head', [check_run(title, '5/5')], head, False
        # App id, not mutable slug, authenticates these two consumers.
        yield f'{label}_fallback_preserves_app_id_authority', [check_run(title, '5/5',
            app={'id': APP_ID, 'slug': 'renamed-review-app'})], HEAD, True
    for label, title in (('adverse', 'CHANGES REQUIRED'), ('lower_score', '4/5'),
                          ('unknown', 'Review complete'), ('whitespace', ' '),
                          ('label_only', 'Paperclip Review'), ('adverse_score', '5/5 CHANGES')):
        yield f'nonempty_{label}_title_cannot_be_rescued_by_body', [check_run(title, '5/5\nDetails')], HEAD, False
    old = check_run('', '5/5')
    for label, new in (
        ('adverse_title', check_run('CHANGES REQUIRED', '5/5')),
        ('unknown_title', check_run('Review complete', '5/5')),
        ('adverse_body', check_run('', 'CHANGES REQUIRED\n5/5')),
        ('queued', check_run('', '5/5', status='queued')),
        ('failure', check_run('', '5/5', conclusion='failure')),
    ):
        new.update(id=101, started_at='2026-10-20T11:00:00Z')
        yield f'latest_{label}_invalidates_older_fallback_pass', [old, new], HEAD, False
        yield f'latest_{label}_selection_is_not_input_order', [new, old], HEAD, False
    newer = check_run('', '5/5', id=101, started_at='2026-10-20T11:00:00Z')
    yield 'newer_fallback_can_replace_older_adverse_run', [check_run('4/5'), newer], HEAD, True
    foreign = check_run('', '5/5', id=102, started_at='2026-10-20T12:00:00Z', app={'id': 1234567})
    yield 'foreign_newer_fallback_cannot_override_trusted_failure', [check_run('4/5'), foreign], HEAD, False
    yield 'foreign_newer_failure_cannot_displace_trusted_fallback', [newer,
        check_run('CHANGES REQUIRED', id=102, started_at='2026-10-20T12:00:00Z', app={'id': 1234567})], HEAD, True
    yield 'stale_head_failure_cannot_displace_current_fallback', [newer,
        check_run('CHANGES REQUIRED', id=102, started_at='2026-10-20T12:00:00Z', head_sha=OLD_HEAD)], HEAD, True


class Acceptance(unittest.TestCase):
    resolver = None
    carry = None
    scratch = None

    def accepts(self, consumer, checks, head):
        try:
            if consumer == 'resolver':
                ok, _ = self.resolver.review_evidence(checks, head)
                self.assertIs(self.resolver.head_is_5of5(checks, head), ok)
                return ok
            return self.carry.latest_review(checks, head, NUMBER) is not None
        except Exception as exc:
            self.fail(f'{consumer} raised {type(exc).__name__} for a review-result fixture: {exc}')

    def test_carry_fallback_retains_pr_binding_and_nonrecursive_provenance(self):
        for external_id in (None, '', f'review:{NUMBER + 1}:{HEAD}',
                            f'review:{NUMBER}:{OLD_HEAD}',
                            f'paperclip-review-carry:v1:{NUMBER}:{HEAD}'):
            with self.subTest(external_id=external_id):
                run = check_run('', '5/5', external_id=external_id)
                self.assertIsNone(self.carry.latest_review([run], HEAD, NUMBER))

    def test_carry_selects_latest_for_queued_pr_before_latest_id(self):
        own = check_run('', '5/5')
        other = check_run('CHANGES REQUIRED', id=101, external_id=f'review:{NUMBER + 1}:{HEAD}')
        self.assertIs(self.carry.latest_review([own, other], HEAD, NUMBER), own)

    def drive_resolver(self, checks, *, login='togetherweown-reviewer[bot]', outdated=True, resolved=False):
        """Exercise real run() decisions; replace only I/O and credential boundaries."""
        module = self.resolver
        mutations = []
        payload = {'data': {'repository': {'pullRequest': {'headRefOid': HEAD,
            'reviewThreads': {'nodes': [{'id': 'thread-one', 'isOutdated': outdated,
                'isResolved': resolved, 'comments': {'nodes': [{'databaseId': 41,
                    'author': {'login': login}}]}}]}}}}}

        def transport(env, *args, input_=None):
            if args[:4] == ('api', '-X', 'POST', 'repos/example-org/service/pulls/17/comments/41/replies'):
                mutations.append('reply')
                return '{}'
            if args[:2] == ('api', 'graphql'):
                if any('resolveReviewThread' in arg for arg in args):
                    mutations.append('resolve')
                    return '{}'
                return json.dumps(payload)
            if args == ('api', f'repos/example-org/service/commits/{HEAD}/check-runs?per_page=100'):
                return json.dumps({'check_runs': checks})
            raise AssertionError('unexpected transport route')

        with ExitStack() as stack:
            for name, value in (('env', lambda: {}),
                                ('open_prs', lambda env: [('example-org/service', NUMBER)]),
                                ('gh', transport), ('log', lambda *args: None),
                                ('STATE_DIR', self.scratch), ('PAUSE', self.scratch / 'PAUSE')):
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(redirect_stdout(io.StringIO()))
            module.run(dry=False)
        return mutations

    def test_resolver_fallback_enables_outdated_reviewer_threads(self):
        self.assertEqual(self.drive_resolver([check_run('', '5/5\nDetails')]), ['reply', 'resolve'])

    def test_resolver_fallback_does_not_bypass_thread_author(self):
        self.assertEqual(self.drive_resolver([check_run('', '5/5')], login='unrelated-reviewer'), [])

    def test_resolver_fallback_does_not_bypass_outdated_gate(self):
        self.assertEqual(self.drive_resolver([check_run('', '5/5')], outdated=False), [])

    def test_resolver_fallback_does_not_bypass_resolved_gate(self):
        self.assertEqual(self.drive_resolver([check_run('', '5/5')], resolved=True), [])

    def test_carry_explicit_review_never_bypasses_identical_patch_gate(self):
        review = self.carry.latest_review([check_run()], HEAD, NUMBER)
        self.assertIsNotNone(review)
        conclusion, _ = self.carry.decide(review, {'equal': False})
        self.assertEqual(conclusion, 'failure')

    def test_carry_fallback_review_never_bypasses_identical_patch_gate(self):
        review = self.carry.latest_review([check_run('', '5/5')], HEAD, NUMBER)
        self.assertIsNotNone(review)
        conclusion, _ = self.carry.decide(review, {'equal': False})
        self.assertEqual(conclusion, 'failure')

    def test_resolver_adverse_title_never_mutates(self):
        self.assertEqual(self.drive_resolver([check_run('CHANGES REQUIRED', '5/5')]), [])

    def test_resolver_stale_fallback_never_mutates(self):
        self.assertEqual(self.drive_resolver([check_run('', '5/5', head_sha=OLD_HEAD)]), [])

    def test_resolver_failed_fallback_never_mutates(self):
        self.assertEqual(self.drive_resolver([check_run('', '5/5', conclusion='failure')]), [])


def install_cases():
    for consumer in ('resolver', 'carry'):
        for name, checks, head, expected in rows():
            def test(self, consumer=consumer, checks=checks, head=head, expected=expected):
                self.assertIs(self.accepts(consumer, deepcopy(checks), head), expected)
            test.__doc__ = f'{consumer}: {name}'
            setattr(Acceptance, f'test_{consumer}_{name}', test)


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', required=True, type=Path, help='repository source root')
    parser.add_argument('--json', type=Path, help='case-level evidence output')
    args = parser.parse_args()
    root = args.source_dir.resolve(strict=True)
    for relative in SOURCE_FILES:
        if not (root / relative).is_file():
            parser.error('missing consumer source: ' + relative)
    scratch = os.environ.get('PAPERCLIP_RUN_SCRATCH_DIR') or os.environ.get('PAPERCLIP_SCRATCH_DIR')
    if not scratch:
        parser.error('set PAPERCLIP_RUN_SCRATCH_DIR or PAPERCLIP_SCRATCH_DIR to owned scratch')
    source_hashes = {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
                     for relative in SOURCE_FILES}
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='review-result-offline-', dir=scratch) as tmp, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'HOME': tmp, 'REVIEW_GATE_ROOT': tmp}, clear=True))
        for obj, name in ((subprocess, 'run'), (subprocess, 'Popen'),
                          (socket, 'socket'), (socket, 'create_connection'), (socket, 'getaddrinfo'),
                          (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open'),
                          (os, 'system'), (os, 'popen'), (os, 'fork'), (os, 'posix_spawn'),
                          (os, 'execv'), (os, 'execve')):
            stack.enter_context(patch.object(obj, name, forbidden))
        # The public snapshot omits this host library. STATE_DIR is its only
        # import-time input; no production behavior is supplied by this double.
        host_lib = types.ModuleType('sdlc_lib')
        host_lib.STATE_DIR = Path(tmp)
        stack.enter_context(patch.dict(sys.modules, {'sdlc_lib': host_lib}))
        # Any new source-side sibling helper must also resolve from the chosen tree.
        stack.enter_context(patch.object(sys, 'path', [str(root / 'ops/productivity'),
            str(root / 'ops/sdlc'), str(root), *sys.path]))
        Acceptance.resolver = load_source(root, SOURCE_FILES[0], 'acceptance_resolver_source')
        Acceptance.carry = load_source(root, SOURCE_FILES[1], 'acceptance_carry_source')
        Acceptance.scratch = Path(tmp)
        install_cases()
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance)
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
        # Hash source-side helpers too, if the implementation introduces one.
        for module in list(sys.modules.values()):
            filename = getattr(module, '__file__', None)
            if filename:
                path = Path(filename).resolve()
                if (path.is_relative_to(root) and path.is_file() and path.suffix == '.py'
                        and path != Path(__file__).resolve()):
                    source_hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {'schema_version': 1,
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'tests': result.testsRun, 'passed': result.passed,
              'failures': [{'case': test.id(), 'trace': trace} for test, trace in result.failures],
              'errors': [{'case': test.id(), 'trace': trace} for test, trace in result.errors],
              'skipped': [{'case': test.id(), 'reason': reason} for test, reason in result.skipped],
              'source_sha256': source_hashes,
              'not_qualified': ['absent host PR-controller state machine', 'review producer rendering',
                                'firstpass state-file policy', 'deterministic docs-lane policy']}
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'tests': result.testsRun, 'passed': len(result.passed),
                      'failures': len(result.failures), 'errors': len(result.errors),
                      'source_sha256': source_hashes}, sort_keys=True))
    return 0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1


if __name__ == '__main__':
    sys.exit(main())
