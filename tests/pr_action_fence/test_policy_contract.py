"""Prospective shared-evaluator contract. Absence is a named contract failure.

The real sender suites supply behavioral RED independently of these new APIs.
Fixture manifests are test injection only, never release/install attestations.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock

from support import (ACTIONS, ALLOW, DENY, HEAD, INVALID, NEW_HEAD, REPOSITORY,
                     ROOT, SOURCE_SHA, contained, decision_code, scratch)

HELPER = ROOT / 'ops/productivity/pr_action_policy.py'
POLICY_REL = 'ops/productivity/pr_action_policy.json'
HELPER_REL = 'ops/productivity/pr_action_policy.py'
MANIFEST_REL = 'ops/productivity/pr_action_release.json'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def valid_policy():
    return {'schema_version': 1, 'subjects': [{
        'repository': 'togetherweown/cliproxyapi', 'pull_request': 1,
        'mode': 'review_only', 'denied_actions': list(ACTIONS)}]}


class PolicyContract(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contained())

    def helper(self):
        self.assertTrue(HELPER.is_file(),
                        'Shared evaluator contract unavailable; real sender RED is in the boundary suites')
        spec = importlib.util.spec_from_file_location('fence_policy_under_test', HELPER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertTrue(callable(getattr(module, 'evaluate', None)), 'evaluate API missing')
        self.assertTrue(callable(getattr(module, 'load_bundle', None)), 'strict bundle loader API missing')
        return module

    @contextlib.contextmanager
    def bundle(self, module, *, policy=None, raw=None, corrupt=None):
        with scratch() as tmp:
            release = Path(tmp)
            product = release / 'ops/productivity'
            product.mkdir(parents=True)
            (release / HELPER_REL).write_bytes(HELPER.read_bytes())
            policy_file = release / POLICY_REL
            policy_file.write_bytes(raw if raw is not None else encoded(valid_policy() if policy is None else policy))
            policy_file.chmod(0o644)
            manifest = {'schema_version': 1, 'source_commit': SOURCE_SHA, 'files': {
                POLICY_REL: sha(policy_file.read_bytes()),
                HELPER_REL: sha((release / HELPER_REL).read_bytes()),
            }}
            manifest_file = release / MANIFEST_REL
            manifest_file.write_bytes(encoded(manifest))
            expected = sha(manifest_file.read_bytes())
            if corrupt == 'policy-digest':
                policy_file.write_bytes(policy_file.read_bytes() + b' ')
            elif corrupt == 'evaluator-digest':
                (release / HELPER_REL).write_bytes(b'# wrong approved bytes\n')
            elif corrupt == 'manifest-digest':
                expected = '0' * 64
            elif corrupt == 'missing':
                policy_file.unlink()
            elif corrupt == 'unreadable':
                policy_file.chmod(0)
            elif corrupt == 'world-writable':
                policy_file.chmod(0o666)
            elif corrupt == 'symlink':
                other = product / 'other.json'
                policy_file.rename(other)
                policy_file.symlink_to(other.name)
            try:
                try:
                    bundle = module.load_bundle(release_dir=release, expected_manifest_sha256=expected)
                except (OSError, ValueError, RuntimeError) as error:
                    # A strict loader may raise; evaluate(None) must still deny.
                    # Production consumers independently have fail-closed tests.
                    bundle = None
                yield bundle
            finally:
                if policy_file.exists() and not policy_file.is_symlink():
                    policy_file.chmod(0o600)

    def evaluate(self, module, bundle, repository=REPOSITORY, number=1, action='owner_wake',
                 expected=HEAD, observed=HEAD, consumer='offline-acceptance'):
        return module.evaluate(bundle, repository, number, action, expected, observed, consumer)

    def test_all_six_actions_bind_to_subject_not_known_sha_or_score(self):
        module = self.helper()
        with self.bundle(module) as bundle:
            for action in ACTIONS:
                for repository in (REPOSITORY, 'TOGETHERWEOWN/CLIProxyAPI', '  TogetherWeOwn/CLIProxyAPI  '):
                    for head in (HEAD, NEW_HEAD):
                        with self.subTest(action=action, repository=repository, head=head):
                            self.assertEqual(DENY, decision_code(self.evaluate(
                                module, bundle, repository, action=action, expected=head, observed=head)))

    def test_exact_identity_positive_controls_are_not_blanket_deny(self):
        module = self.helper()
        with self.bundle(module) as bundle:
            self.assertIsNotNone(bundle, 'valid fixture bundle failed to load')
            for repository, number in [('example/other', 1), (REPOSITORY, 2), (REPOSITORY, 10),
                                       (REPOSITORY, 11), ('TogetherWeOwn/CLIProxyAPI-extra', 1)]:
                for action in ACTIONS:
                    with self.subTest(repository=repository, number=number, action=action):
                        self.assertEqual(ALLOW, decision_code(self.evaluate(module, bundle, repository, number, action)))

    def test_invalid_identity_never_becomes_unrelated(self):
        module = self.helper()
        with self.bundle(module) as bundle:
            for repository in [None, '', ' /CLIProxyAPI', 'TogetherWeOwn/',
                               'https://github.com/TogetherWeOwn/CLIProxyAPI',
                               'TogetherWeOwn/CLIProxyAPI/extra', 'TogetherWeOwn%2fCLIProxyAPI',
                               'TogetherWeOwn/CLIProxyAPI.git', 'TogetherWeOwn/CLIProxyАPI',
                               '../CLIProxyAPI', 'TogetherWeOwn\\CLIProxyAPI', 'TogetherWeOwn/CLIProxyAPI\0']:
                with self.subTest(repository=repository):
                    self.assertEqual(INVALID, decision_code(self.evaluate(module, bundle, repository)))
            for number in [None, True, False, 0, -1, 1.5, '1', '01', '+1', '1e0', '1/extra']:
                with self.subTest(number=number):
                    self.assertEqual(INVALID, decision_code(self.evaluate(module, bundle, 'example/other', number)))

    def test_missing_stale_or_malformed_head_context_denies_unrelated_subject(self):
        module = self.helper()
        with self.bundle(module) as bundle:
            for expected, observed in [(None, HEAD), (HEAD, None), ('', HEAD), ('bad', HEAD),
                                       (HEAD, NEW_HEAD), (HEAD, 'z' * 40)]:
                with self.subTest(expected=expected, observed=observed):
                    self.assertEqual(INVALID, decision_code(self.evaluate(
                        module, bundle, 'example/other', expected=expected, observed=observed)))
            for action in [None, '', 'close', 'allow_all', 'MERGE']:
                self.assertEqual(INVALID, decision_code(self.evaluate(module, bundle, 'example/other', action=action)))

    def test_missing_bundle_denies_every_action_for_every_subject(self):
        module = self.helper()
        for repository in (REPOSITORY, 'example/other'):
            for action in ACTIONS:
                self.assertEqual(INVALID, decision_code(self.evaluate(module, None, repository, action=action)))

    def test_invalid_policy_schema_denies_all_covered_actions_not_only_target(self):
        module = self.helper()
        valid = valid_policy()
        policies = [None, {}, [], {'schema_version': 2, 'subjects': valid['subjects']},
                    {'schema_version': 1, 'subjects': []}, {'schema_version': 1},
                    {**valid, 'default': 'allow'},
                    {'schema_version': 1, 'subjects': [dict(valid['subjects'][0], mode='allow')]},
                    {'schema_version': 1, 'subjects': [dict(valid['subjects'][0], repository='example/other')]},
                    {'schema_version': 1, 'subjects': valid['subjects'] * 2},
                    {'schema_version': 1, 'subjects': [dict(valid['subjects'][0], pull_request=True)]},
                    {'schema_version': 1, 'subjects': [dict(valid['subjects'][0], denied_actions=['owner_wake'])]},
                    {'schema_version': 1, 'subjects': [dict(valid['subjects'][0], denied_actions=list(ACTIONS) + ['close'])]},
                    {'schema_version': 1, 'subjects': [dict(valid['subjects'][0], override=True)]}]
        for index, policy in enumerate(policies):
            with self.subTest(policy=index), self.bundle(module, raw=encoded(policy)) as bundle:
                for repository in (REPOSITORY, 'example/other'):
                    for action in ACTIONS:
                        self.assertEqual(INVALID, decision_code(self.evaluate(module, bundle, repository, action=action)))

    def test_truncated_invalid_duplicate_key_and_oversized_json_are_denials(self):
        module = self.helper()
        for raw in [b'{', b'not-json', b'{"schema_version":1,"schema_version":2,"subjects":[]}', b' ' * 1048577]:
            with self.subTest(size=len(raw)), self.bundle(module, raw=raw) as bundle:
                self.assertEqual(INVALID, decision_code(self.evaluate(module, bundle, 'example/other')))

    def test_release_digest_and_safe_file_loading_fail_closed(self):
        module = self.helper()
        for corrupt in ['policy-digest', 'evaluator-digest', 'manifest-digest', 'missing',
                        'unreadable', 'world-writable', 'symlink']:
            with self.subTest(corrupt=corrupt), self.bundle(module, corrupt=corrupt) as bundle:
                for action in ACTIONS:
                    self.assertEqual(INVALID, decision_code(self.evaluate(module, bundle, 'example/other', action=action)))

    def test_receipt_is_diagnostic_complete_and_not_a_reusable_token(self):
        module = self.helper()
        with self.bundle(module) as bundle:
            receipt = self.evaluate(module, bundle)
            if not isinstance(receipt, dict):
                receipt = vars(receipt)
            self.assertEqual(DENY, receipt['decision'])
            for field in ['schema_version', 'policy_digest', 'consumer', 'repository',
                          'pull_request', 'action', 'expected_head', 'observed_head', 'reason']:
                self.assertIn(field, receipt)
            self.assertEqual('togetherweown/cliproxyapi', receipt['repository'])
            self.assertEqual(1, receipt['pull_request'])
            self.assertEqual('owner_wake', receipt['action'])
            self.assertEqual(HEAD, receipt['expected_head'])
            self.assertEqual(HEAD, receipt['observed_head'])
            self.assertRegex(receipt['policy_digest'], r'^[0-9a-f]{64}$')
            self.assertEqual(INVALID, decision_code(self.evaluate(module, receipt, 'example/other')),
                             'a previous decision receipt is not a valid policy bundle')

    def cli(self, repository, number, action='owner_wake', head=HEAD):
        self.helper()
        args = [str(HELPER), '--repository', repository, '--pr-number', str(number),
                '--action', action, '--expected-head', head, '--observed-head', head,
                '--consumer', 'offline-cli-acceptance']
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with scratch() as tmp, mock.patch.object(sys, 'argv', args), \
                mock.patch('os.getcwd', return_value=tmp), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                runpy.run_path(str(HELPER), run_name='__main__')
            except SystemExit as error:
                code = error.code or 0
        lines = stdout.getvalue().splitlines()
        self.assertEqual(1, len(lines), 'CLI must emit one bounded JSON decision, not banners/raw payloads')
        return code, json.loads(lines[0])

    def test_cli_shell_contract_denied_exits_never_look_like_success(self):
        for action in ACTIONS:
            code, result = self.cli(REPOSITORY, '1', action)
            self.assertEqual(10, code)
            self.assertEqual(DENY, result['decision'])
        for number in ['0', '-1', '+1', '1.0', '1e0', '01', 'true']:
            code, result = self.cli('example/other', number)
            self.assertEqual(20, code)
            self.assertEqual(INVALID, result['decision'])
        code, result = self.cli('example/other', '1')
        self.assertEqual(0, code)
        self.assertEqual(ALLOW, result['decision'])


if __name__ == '__main__':
    unittest.main()
