#!/usr/bin/env python3
"""Sanitized source-acquisition artifact; no installation or runtime configuration included."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_gate as rg

class GateAdmitTest(unittest.TestCase):

    def test_admits_when_free(self):
        self.assertTrue(rg.gate_admit(0, 3))
        self.assertTrue(rg.gate_admit(1, 2))

    def test_holds_when_full(self):
        self.assertFalse(rg.gate_admit(2, 2))
        self.assertFalse(rg.gate_admit(3, 2))

    def test_holds_when_no_free(self):
        self.assertFalse(rg.gate_admit(0, 0))
        self.assertFalse(rg.gate_admit(0, -1))

    def test_bad_input_holds(self):
        self.assertFalse(rg.gate_admit(None, 2))
        self.assertFalse(rg.gate_admit('x', 2))
        self.assertFalse(rg.gate_admit(0, None))

class DueRankTest(unittest.TestCase):

    def test_pinned_pr_first(self):
        prio = {('example-org/primary-service-a', 1012)}
        self.assertEqual(rg.due_rank('Example-Org/primary-service-a', 1012, prio), 0)
        self.assertEqual(rg.due_rank('Example-Org/primary-service-a', 1013, prio), 1)

    def test_owner_project_order(self):
        names = ['last-service-api', 'other-service', 'tertiary-service', 'secondary-api', 'primary-service-b']
        ranked = sorted(names, key=lambda n: rg.due_rank(f'o/{n}', 1, set()))
        self.assertEqual(ranked, ['primary-service-b', 'secondary-api', 'tertiary-service', 'other-service', 'last-service-api'])

class CiGreenAdvisoryTest(unittest.TestCase):
    CI = {'name': 'selftest', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}

    def test_pending_advisory_bot_does_not_gate(self):
        rabbit = {'name': 'CodeRabbit', 'state': 'PENDING'}
        self.assertTrue(rg.ci_green([self.CI, rabbit], []))

    def test_failed_advisory_bot_still_red(self):
        rabbit = {'name': 'CodeRabbit', 'state': 'FAILURE'}
        self.assertFalse(rg.ci_green([self.CI, rabbit], []))

    def test_pending_real_ci_still_gates(self):
        pending = {'name': 'build', 'status': 'IN_PROGRESS', 'conclusion': None}
        self.assertFalse(rg.ci_green([self.CI, pending], []))

    def test_only_advisory_is_not_green(self):
        rabbit = {'name': 'CodeRabbit', 'state': 'PENDING'}
        self.assertFalse(rg.ci_green([rabbit], []))
class CiGreenReadinessSweepTest(unittest.TestCase):
    CI = {'name': 'selftest', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}

    def sweep(self, conclusion, name='readiness-sweep', workflow='Readiness Sweep'):
        return {'name': name, 'status': 'COMPLETED', 'conclusion': conclusion,
                'checkSuite': {'workflowRun': {'workflow': {'name': workflow}}}}

    def test_failed_repo_wide_sweep_does_not_hide_green_head(self):
        self.assertTrue(rg.ci_green([self.CI, self.sweep('FAILURE')], []))
        self.assertTrue(rg.ci_green([self.CI, self.sweep('FAILURE')], {'selftest'}))

    def test_failed_sweep_workflow_name_alone_is_ignored(self):
        self.assertTrue(rg.ci_green([self.CI, self.sweep('FAILURE', name='sweep')], []))

    def test_pending_sweep_does_not_gate(self):
        pending = {'name': 'readiness-sweep', 'status': 'IN_PROGRESS', 'conclusion': None}
        self.assertTrue(rg.ci_green([self.CI, pending], []))

    def test_only_sweep_is_not_green(self):
        self.assertFalse(rg.ci_green([self.sweep('SUCCESS')], []))

    def test_required_sweep_still_fails_closed(self):
        self.assertFalse(rg.ci_green([self.CI, self.sweep('FAILURE')], {'readiness-sweep'}))

    def test_other_failed_workflow_still_red(self):
        self.assertFalse(rg.ci_green([self.CI, self.sweep('FAILURE', name='lint', workflow='CI')], []))
if __name__ == '__main__':
    unittest.main()
