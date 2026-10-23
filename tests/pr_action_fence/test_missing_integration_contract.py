"""NON-RUNTIME contracts for absent controller/legacy/direct/queue material.

No private source is reconstructed here. A missing adapter is a coverage-gap
assertion, not a missing-import error or proof of an installed runtime. A future
adapter must bind to reviewed sanitized source or an exact-baseline packet.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

from support import (ALLOW, DENY, HEAD, INVALID, NEW_HEAD, REPOSITORY, ROOT,
                     contained, context)

INTEGRATION = ROOT / 'ops/productivity/pr_action_integration.py'
REQUIRED = {
    'controller_initial': 'owner_wake',
    'controller_draft_idle': 'owner_wake',
    'controller_rewake': 'owner_wake',
    'legacy_auto_merge': 'enable_auto_merge',
    'pump_direct_merge': 'merge',
    'shepherd_direct_merge': 'merge',
    'queue_submit': 'enqueue_merge',
    'queue_retry': 'enqueue_merge',
    'queue_final_merge': 'merge',
}
GATES = {'ci_green': True, 'exact_head_review': True, 'hold_clear': True,
         'owner_idle': True, 'branch_protected': True, 'queue_eligible': True}


class MissingIntegrationContract(unittest.TestCase):
    def setUp(self):
        self.offline = contained()
        self.offline.__enter__()
        self.addCleanup(self.offline.__exit__, None, None, None)

    def integration(self):
        self.assertTrue(INTEGRATION.is_file(),
                        'NON-RUNTIME COVERAGE GAP: canonical controller/legacy/direct/queue material '
                        'and baseline-bound offline adapters are not available; installation HOLD remains')
        spec = importlib.util.spec_from_file_location('fence_integration_under_test', INTEGRATION)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def boundary(self, name):
        module = self.integration()
        self.assertTrue(callable(getattr(module, 'offline_boundary', None)), 'offline_boundary contract unavailable')
        descriptor = module.offline_boundary(name)
        self.assertIsInstance(descriptor, dict)
        self.assertIn(descriptor.get('coverage'), ['canonical_source', 'packet_non_runtime'])
        self.assertIs(descriptor.get('runtime_integrated'), False,
                      'offline fixtures never attest installed consumer linkage')
        source = descriptor.get('source_path')
        self.assertIsInstance(source, str)
        path = (ROOT / source).resolve()
        self.assertTrue(path.is_relative_to(ROOT) and path.is_file(), 'canonical integration source/packet unavailable')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), descriptor.get('source_sha256'))
        self.assertTrue(callable(descriptor.get('invoke')), 'adapter must exercise the mapped final boundary')
        if descriptor['coverage'] == 'packet_non_runtime':
            self.assertRegex(descriptor.get('before_sha256', ''), r'^[0-9a-f]{64}$')
            self.assertRegex(descriptor.get('after_sha256', ''), r'^[0-9a-f]{64}$')
            self.assertIs(descriptor.get('no_fuzz'), True)
            self.assertTrue(descriptor.get('exact_anchors'), 'packet must name exact baseline application anchors')
        return descriptor['invoke']

    def work(self, action, repository=REPOSITORY, **overrides):
        value = context(repository=repository, action=action)
        value.update(stage='consume', method='squash', gates=copy.deepcopy(GATES))
        value.update(overrides)
        return value

    def invoke(self, boundary, work, *, observed=None):
        writes, reads = [], []

        def resolve_pr(repository, number):
            reads.append((repository, number))
            return {'repository': repository, 'pull_request': number, 'head_sha': observed or work['observed_head'],
                    'state': 'OPEN', 'draft': True, 'merged': False, 'review_score': 5, 'ci_green': True,
                    'auto_merge_request': None, 'queue_entry': None}

        def transport(action, *args, **kwargs):
            writes.append((action, args, kwargs))
            return {'ok': True}

        result = boundary(copy.deepcopy(work), resolve_pr=resolve_pr, transport=transport)
        return result, writes, reads

    def test_generation_carries_subject_head_action_and_policy_digest(self):
        self.integration()
        for name, action in REQUIRED.items():
            with self.subTest(boundary=name):
                boundary = self.boundary(name)
                denied, writes, _ = self.invoke(boundary, self.work(action, stage='generate'))
                self.assertEqual([], writes)
                self.assertIn(DENY, json.dumps(denied))
                planned, writes, _ = self.invoke(boundary, self.work(action, 'example/other', stage='generate'))
                self.assertEqual([], writes)
                work = planned['work_item']
                self.assertEqual('example/other', work['repository'])
                self.assertEqual(1, work['pr_number'])
                self.assertEqual(HEAD, work['expected_head'])
                self.assertEqual(action, work['action'])
                self.assertRegex(work['policy_digest'], r'^[0-9a-f]{64}$')

    def test_direct_consumer_injection_denies_squash_rebase_merge_and_both_queue_ends(self):
        self.integration()
        for name, action in REQUIRED.items():
            for method in ('squash', 'rebase', 'merge') if action == 'merge' else ('squash',):
                with self.subTest(boundary=name, method=method):
                    result, writes, _ = self.invoke(self.boundary(name), self.work(action, method=method))
                    self.assertEqual([], writes, 'injected final consumer reached fake wake/merge/enqueue')
                    self.assertIn(DENY, json.dumps(result))

    def test_new_head_and_future_review_success_never_unfence_subject(self):
        self.integration()
        for name, action in REQUIRED.items():
            with self.subTest(boundary=name):
                result, writes, _ = self.invoke(self.boundary(name), self.work(
                    action, expected_head=NEW_HEAD, observed_head=NEW_HEAD, review_score=5))
                self.assertEqual([], writes)
                self.assertIn(DENY, json.dumps(result))

    def test_retry_and_final_consumer_quarantine_head_or_policy_drift(self):
        self.integration()
        for name, action in REQUIRED.items():
            for stage in ('consume', 'retry', 'final'):
                for overrides, observed in [({}, NEW_HEAD), ({'policy_digest': '0' * 64}, HEAD)]:
                    with self.subTest(boundary=name, stage=stage, drift=overrides):
                        result, writes, _ = self.invoke(self.boundary(name), self.work(
                            action, 'example/other', stage=stage, **overrides), observed=observed)
                        self.assertEqual([], writes, 'queued/stale decision receipt acted as authorization')
                        self.assertIn(INVALID, json.dumps(result))

    def test_unrelated_controls_keep_existing_gates_and_exact_head_precondition(self):
        self.integration()
        for name, action in REQUIRED.items():
            with self.subTest(boundary=name):
                boundary = self.boundary(name)
                result, writes, reads = self.invoke(boundary, self.work(action, 'example/other'))
                self.assertEqual(1, len(writes), 'blanket deny cannot satisfy this contract')
                self.assertTrue(reads, 'consumer must refresh trusted PR identity/head')
                if action == 'merge':
                    self.assertIn(HEAD, json.dumps(writes), 'otherwise eligible merge must be exact-head bound')
                for gate in GATES:
                    held = dict(GATES, **{gate: False})
                    _, writes, _ = self.invoke(boundary, self.work(action, 'example/other', gates=held))
                    self.assertEqual([], writes, f'additive fence weakened original gate {gate}')

    def test_shell_and_python_consumers_deny_helper_failure_not_only_policy_match(self):
        self.integration()
        for name, action in REQUIRED.items():
            for failure in ('missing', 'timeout', 'nonzero', 'invalid-json', 'runtime-error'):
                with self.subTest(boundary=name, failure=failure):
                    # Failure injection is part of the offline adapter only;
                    # never a runtime env/policy override or a permissive matcher.
                    result, writes, _ = self.invoke(self.boundary(name), self.work(
                        action, 'example/other', helper_failure=failure))
                    self.assertEqual([], writes)
                    self.assertIn(INVALID, json.dumps(result))

    def test_exact_baseline_packet_application_refuses_wrong_anchor_or_digest(self):
        module = self.integration()
        self.assertTrue(callable(getattr(module, 'verify_packet', None)))
        for name in REQUIRED:
            with self.subTest(boundary=name):
                for mismatch in ('before-digest', 'missing-anchor', 'fuzzy-context', 'after-digest'):
                    result = module.verify_packet(name, injected_fault=mismatch, offline_only=True)
                    self.assertIs(result['applicable'], False)
                    self.assertIs(result['runtime_integrated'], False)
                    self.assertTrue(result['hold'])

    def test_cleanup_executor_stub_is_disable_dequeue_only_with_after_state_proof(self):
        module = self.integration()
        self.assertTrue(callable(getattr(module, 'remediation_plan', None)))
        for auto, queued in [(False, False), (True, False), (False, True), (True, True)]:
            before = {'repository': REPOSITORY, 'pull_request': 1, 'head_sha': HEAD,
                      'state': 'OPEN', 'draft': True, 'merged': False,
                      'auto_merge_request': {'id': 'fixture-auto'} if auto else None,
                      'queue_entry': {'id': 'fixture-queue'} if queued else None}
            calls = []

            def executor(operation, subject):
                calls.append((operation, subject))
                self.assertIn(operation, ('disable_auto_merge', 'remove_queue_entry'))
                self.assertEqual(REPOSITORY.lower(), subject['repository'].lower())
                self.assertEqual(1, subject['pull_request'])
                self.assertEqual(HEAD, subject['head_sha'])
                return dict(before, auto_merge_request=None, queue_entry=None)

            result = module.remediation_plan(copy.deepcopy(before), executor=executor, offline_only=True)
            self.assertEqual(int(auto) + int(queued), len(calls), 'absence must be a no-op')
            self.assertIs(result['hold'], True)
            self.assertEqual(HEAD, result['after']['head_sha'])
            self.assertIs(result['after']['draft'], True)
            self.assertIs(result['after']['merged'], False)
            self.assertIsNone(result['after']['auto_merge_request'])
            self.assertIsNone(result['after']['queue_entry'])

    def test_cleanup_failure_unknown_identity_or_head_drift_preserves_hold(self):
        module = self.integration()
        writes = []
        for reason in ('permission', 'state-unknown', 'head-drift', 'queue-removal-unavailable'):
            result = module.remediation_plan({
                'repository': REPOSITORY, 'pull_request': 1, 'head_sha': HEAD,
                'state': 'OPEN', 'draft': True, 'merged': False,
                'auto_merge_request': {'id': 'fixture'}, 'queue_entry': {'id': 'fixture'},
                'injected_fault': reason},
                executor=lambda *args: writes.append(args), offline_only=True)
            self.assertTrue(result['hold'])
            self.assertFalse(result['compliant'])
        self.assertEqual([], writes, 'uncertain cleanup must not mutate or fall back to enabling/merge')

    def test_rollback_never_restarts_pre_fence_or_recreates_promotion_work(self):
        module = self.integration()
        self.assertTrue(callable(getattr(module, 'rollback_plan', None)))
        calls = []
        result = module.rollback_plan({'previous_fence_capable': False},
                                      executor=lambda *args: calls.append(args), offline_only=True)
        self.assertTrue(result['hold'])
        self.assertTrue(result['quiesced'])
        self.assertEqual([], calls, 'no safe previous bundle means no restore/restart mutation')
        result = module.rollback_plan({'previous_fence_capable': True, 'verified': True},
                                      executor=lambda *args: calls.append(args), offline_only=True)
        for operation, *_ in calls:
            self.assertIn(operation, ('restore_verified_fence_bundle', 'verify_denials'))
        self.assertTrue(result['hold'])


if __name__ == '__main__':
    unittest.main()
