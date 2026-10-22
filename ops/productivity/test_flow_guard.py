import calendar
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import flow_guard as fg

NOW = calendar.timegm(time.strptime('2026-10-09T08:00:00Z', '%Y-%m-%dT%H:%M:%SZ'))
STAGE = {'type': 'review', 'participants': [{'agentId': '00000000-0000-4000-8000-000000000053'}]}


class ArmedPolicy(unittest.TestCase):
    def test_review_stage_and_mode_survive_the_arm(self):
        current = {'mode': 'normal', 'stages': [STAGE], 'commentRequired': True,
                   'monitor': {'kind': 'external_service', 'status': 'triggered'}}
        policy = fg.armed_policy(current, NOW)
        self.assertEqual(policy['stages'], [STAGE])
        self.assertTrue(policy['commentRequired'])
        self.assertEqual(policy['mode'], 'normal')

    def test_unknown_fields_survive_and_the_monitor_is_armed(self):
        policy = fg.armed_policy({'mode': 'normal', 'stages': [], 'commentRequired': False, 'futureField': 1}, NOW)
        self.assertEqual(policy['futureField'], 1)
        self.assertEqual(policy['monitor']['serviceName'], 'flow-guard')
        self.assertEqual(policy['monitor']['nextCheckAt'], '2026-10-09T10:00:00Z')

    def test_card_without_a_policy_gets_the_plain_normal_policy(self):
        policy = fg.armed_policy(None, NOW)
        self.assertEqual((policy['mode'], policy['stages'], policy['commentRequired']), ('normal', [], False))

    def test_the_card_snapshot_is_not_mutated(self):
        current = {'mode': 'normal', 'stages': [], 'commentRequired': False}
        fg.armed_policy(current, NOW)
        self.assertNotIn('monitor', current)


class LoopPass(unittest.TestCase):
    def run_pass(self, issue):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        def fake_psql(sql):
            return 'card-1\tTOG-900\t6\n' if 'issue_continuation_needed' in sql else ''

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fg, 'STATE', tmp), \
                mock.patch.object(fg, 'psql', side_effect=fake_psql), \
                mock.patch.object(fg, 'read_issue', return_value=issue), \
                mock.patch.object(fg, 'log'), \
                mock.patch.object(fg.subprocess, 'run', side_effect=fake_run), \
                mock.patch('builtins.print'):
            fg.main()
        return calls

    @staticmethod
    def issue_patches(calls):
        return [json.loads(a[a.index('--data-binary') + 1]) for a in calls
                if 'PATCH' in a and any('/api/issues/' in x for x in a)]

    def test_looping_card_is_patched_with_its_stages_kept(self):
        bodies = self.issue_patches(self.run_pass({'executionPolicy': {'mode': 'normal', 'stages': [STAGE],
                                                                       'commentRequired': True}}))
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]['executionPolicy']['stages'], [STAGE])
        self.assertEqual(bodies[0]['executionPolicy']['monitor']['serviceName'], 'flow-guard')

    def test_card_whose_policy_cannot_be_read_is_not_patched(self):
        self.assertEqual(self.issue_patches(self.run_pass(None)), [])


class TimeoutPass(unittest.TestCase):
    STEPS = ['idle_waiters', 'hide_noop_comments', 'orphan_prs', 'reconcile_replay_blocked']

    def run_pass(self, fail=lambda args, kwargs: False, loops='', agents='', returncode=0,
                 patch_returncode=None, child_stdout='0 rows\n', child_stderr='fixture error'):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if fail(args, kwargs):
                raise subprocess.TimeoutExpired(args, kwargs['timeout'], output=b'partial fixture output')
            if args[0] == 'docker':
                out = loops if 'issue_continuation_needed' in kwargs['input'] else agents
            elif args[0] == 'curl':
                out = '200' if 'PATCH' in args else json.dumps({'executionPolicy': {'stages': [STAGE]}})
            else:
                out = child_stdout
            code = patch_returncode if 'PATCH' in args and patch_returncode is not None else returncode
            return subprocess.CompletedProcess(args, code, stdout=out, stderr=child_stderr)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fg, 'STATE', tmp), \
                mock.patch.object(fg, 'log') as log, \
                mock.patch.object(fg.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(sys, 'argv', ['flow_guard', '--apply']), \
                mock.patch('builtins.print') as output:
            exit_code = fg.main()
            if exit_code is None:
                exit_code = 0
        return calls, log, output, exit_code

    def assert_steps_ran(self, calls):
        children = [a for a, _ in calls if a[0] == 'python3']
        self.assertEqual([Path(a[1]).stem for a in children], self.STEPS)
        self.assertEqual(children[-1][-3:], ['--apply', '--wake', '5'])

    def test_child_timeout_logs_step_and_continues(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                calls, log, _, _ = self.run_pass(lambda a, k: a[0] == 'python3' and Path(a[1]).stem == step)
                self.assert_steps_ran(calls)
                log.assert_any_call(f'{step} timed out after {fg.STEP_TIMEOUT_SECONDS}s; outcome unknown, not retried this tick')

    def test_psql_read_timeout_does_not_derive_patch(self):
        calls, log, _, _ = self.run_pass(lambda a, k: a[0] == 'docker', loops='card-1\tTOG-900\t6\n',
                                    agents='agent-1\tFixture\tprocess_lost\n')
        self.assertFalse(any('PATCH' in a for a, _ in calls))
        self.assert_steps_ran(calls)
        log.assert_any_call(f'psql read timed out after {fg.PSQL_TIMEOUT_SECONDS}s; could not read, no derived changes')

    def test_http_read_timeout_does_not_derive_patch(self):
        calls, log, _, _ = self.run_pass(lambda a, k: a[0] == 'curl' and 'PATCH' not in a,
                                    loops='card-1\tTOG-900\t6\n')
        self.assertFalse(any('PATCH' in a for a, _ in calls))
        self.assert_steps_ran(calls)
        log.assert_any_call('continuation loop on TOG-' '900: executionPolicy unreadable, card not changed')

    def test_failed_card_read_does_not_block_a_later_measured_card(self):
        calls, _, _, _ = self.run_pass(lambda a, k: a[0] == 'curl' and a[-1].endswith('/issues/card-1'),
                                  loops='card-1\tTOG-900\t6\ncard-2\tTOG-901\t6\n')
        patches = [a for a, _ in calls if 'PATCH' in a]
        self.assertEqual(len(patches), 1)
        self.assertTrue(any(x.endswith('/issues/card-2') for x in patches[0]))

    def test_patch_timeout_is_unknown_not_success_or_retried(self):
        calls, log, _, _ = self.run_pass(lambda a, k: 'PATCH' in a, loops='card-1\tTOG-900\t6\n',
                                    agents='agent-1\tFixture\tprocess_lost\n')
        self.assertEqual(sum('PATCH' in a for a, _ in calls), 2)
        self.assert_steps_ran(calls)
        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(sum('outcome unknown' in msg for msg in messages), 2)
        self.assertFalse(any('armed 2h monitor' in msg or 'reset to idle' in msg for msg in messages))

    def test_every_exercised_subprocess_has_its_named_bound(self):
        calls, _, _, _ = self.run_pass(loops='card-1\tTOG-900\t6\n', agents='agent-1\tFixture\tprocess_lost\n')
        self.assertEqual(len(calls), 9)
        for args, kwargs in calls:
            expected = {'docker': fg.PSQL_TIMEOUT_SECONDS, 'curl': fg.CURL_TIMEOUT_SECONDS,
                        'python3': fg.STEP_TIMEOUT_SECONDS}[args[0]]
            self.assertEqual(kwargs['timeout'], expected)

    def test_nonzero_children_log_sanitized_failures_including_replay(self):
        calls, log, output, exit_code = self.run_pass(
            returncode=7, child_stdout='raw-stdout-canary', child_stderr='raw-stderr-canary')
        self.assert_steps_ran(calls)
        self.assertNotEqual(exit_code, 0)
        for step in self.STEPS:
            log.assert_any_call(f'{step} failed rc=7; next_action=inspect_child_result')
        logged = '\n'.join(str(c) for c in log.call_args_list + output.call_args_list)
        self.assertNotIn('raw-stdout-canary', logged)
        self.assertNotIn('raw-stderr-canary', logged)
        output.assert_not_called()

    def test_nonzero_psql_reads_do_not_derive_patches_from_partial_rows(self):
        calls, log, _, exit_code = self.run_pass(returncode=7, loops='card-1\tcase_loop_fixture\t6\n',
                                               agents='agent-1\tFixture\tprocess_lost\n')
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(any('PATCH' in a for a, _ in calls))
        self.assert_steps_ran(calls)
        self.assertEqual(sum(c.args == ('psql read failed; could not read, no derived changes',)
                             for c in log.call_args_list), 2)

    def test_nonzero_patch_transport_with_stdout_200_never_arms_or_resets(self):
        for code in (7, 28):
            with self.subTest(returncode=code):
                calls, log, _, exit_code = self.run_pass(
                    patch_returncode=code, loops='card-1\tcase_loop_fixture\t6\n',
                    agents='agent-1\tFixture\tprocess_lost\n')
                self.assertEqual(sum('PATCH' in a for a, _ in calls), 2)
                self.assertNotEqual(exit_code, 0)
                messages = [c.args[0] for c in log.call_args_list]
                self.assertFalse(any('armed 2h monitor' in m or 'reset to idle' in m for m in messages))


class SafeFlags(unittest.TestCase):
    def test_dry_run_and_contradictory_flags_have_no_side_effects(self):
        for flags, succeeds in ((['--dry-run'], True), (['--apply', '--dry-run'], False),
                                (['--dry-run', '--apply'], False)):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp, 'state')
                with mock.patch.object(fg, 'STATE', str(state)), \
                        mock.patch.object(sys, 'argv', ['flow_guard', *flags]), \
                        mock.patch.object(fg.subprocess, 'run',
                                          side_effect=AssertionError('safe flags must not run subprocesses')) as run, \
                        mock.patch.object(fg, 'log') as log, \
                        mock.patch.object(fg.os, 'makedirs') as mkdir, \
                        mock.patch('builtins.print'):
                    try:
                        exit_code = fg.main()
                    except SystemExit as exc:
                        exit_code = exc.code
                if succeeds:
                    self.assertEqual(exit_code, 0)
                else:
                    self.assertNotEqual(exit_code, 0)
                run.assert_not_called()
                log.assert_not_called()
                mkdir.assert_not_called()
                self.assertEqual(list(Path(tmp).iterdir()), [])


class TimeoutConfig(unittest.TestCase):
    def test_module_constants_use_environment_overrides(self):
        env = {'FLOW_GUARD_PSQL_TIMEOUT_SECONDS': '11', 'FLOW_GUARD_CURL_TIMEOUT_SECONDS': '12',
               'FLOW_GUARD_STEP_TIMEOUT_SECONDS': '13'}
        spec = importlib.util.spec_from_file_location('flow_guard_config_fixture', fg.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(fg.os.environ, env):
            spec.loader.exec_module(module)
        self.assertEqual((module.PSQL_TIMEOUT_SECONDS, module.CURL_TIMEOUT_SECONDS, module.STEP_TIMEOUT_SECONDS),
                         (11, 12, 13))

    def test_invalid_timeouts_fail_before_any_subprocess(self):
        for value in ('0', '-1', 'nan', 'inf', 'not-a-number'):
            with self.subTest(value=value), mock.patch.dict(fg.os.environ, {'FLOW_GUARD_STEP_TIMEOUT_SECONDS': value}):
                spec = importlib.util.spec_from_file_location('flow_guard_invalid_fixture', fg.__file__)
                with mock.patch.object(fg.subprocess, 'run') as run, self.assertRaises(ValueError):
                    spec.loader.exec_module(importlib.util.module_from_spec(spec))
                run.assert_not_called()
