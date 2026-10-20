"""Exercise the deployment patch against the pre-fix function or an explicit host source.

PR_CONTROLLER_SOURCE selects a full controller candidate without importing its host
clients. The default applies the reviewed, zero-fuzz patch to a portable fixture.
"""
import ast
import calendar
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import Mock

HERE = Path(__file__).resolve().parent
PATCH = HERE / 'patches/pr-controller-backstop.patch'
FIXTURE = HERE / 'fixtures/pr_controller_backstop_before.py'
NOW = 1800000000


def stamp(value):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(value))


def parse(value):
    return calendar.timegm(time.strptime(value, '%Y-%m-%dT%H:%M:%SZ'))


def patched_source():
    if os.environ.get('PR_CONTROLLER_SOURCE'):
        return Path(os.environ['PR_CONTROLLER_SOURCE']).read_text()
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'ops/productivity/pr_controller.py'
        target.parent.mkdir(parents=True)
        target.write_text(FIXTURE.read_text())
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                       cwd=directory, check=True, capture_output=True, text=True)
        return target.read_text()


class BackstopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(patched_source())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'ensure_backstop')
        cls.code = compile(ast.Module(body=[function], type_ignores=[]), '<controller>', 'exec')

    def setUp(self):
        self.mon = {'nextCheckAt': stamp(NOW + 3 * 3600), 'scheduledBy': 'board',
                    'serviceName': 'github-pr-controller', 'kind': 'external_service',
                    'notes': 'PR controller backstop: existing deadline', 'maxAttempts': 12}
        self.pol = {'mode': 'normal', 'stages': [], 'commentRequired': True, 'monitor': self.mon}
        self.status = 'in_review'
        self.next_at = NOW + 3 * 3600
        self.scheduled_by = 'board'
        self.api = Mock(return_value=('200', '{}'))
        self.running = Mock(return_value=False)
        self.env = {'json': json, 'rr': types.SimpleNamespace(psql=self.rows),
                    'NOW': NOW, 'BACKSTOP_H': 12, 'BACKSTOP_MIN_H': 6, 'SHADOW': False,
                    'ts': stamp, 'parse': parse, 'running': self.running,
                    'api': self.api, 'log': Mock()}
        exec(self.code, self.env)

    def rows(self, sql):
        row = [json.dumps(self.pol), self.status, str(self.next_at)]
        if 'monitor_scheduled_by' in sql:
            row.append(self.scheduled_by)
        return [row]

    def run_backstop(self):
        self.env['ensure_backstop']('example-issue', 'EXAMPLE-1', 'open PR', 'https://github.com/example/repo/pull/1')

    def assignee_monitor(self):
        self.scheduled_by = 'assignee'
        self.mon.update(scheduledBy='assignee', notes='Assignee requested a check', serviceName='example-ci')

    def test_pending_controller_deadline_is_not_rearmed(self):
        self.run_backstop()
        self.api.assert_not_called()

    def test_due_controller_deadline_is_left_for_scheduler(self):
        self.next_at = NOW - 60
        self.run_backstop()
        self.api.assert_not_called()

    def test_exact_deadline_is_left_for_scheduler(self):
        self.next_at = NOW
        self.run_backstop()
        self.api.assert_not_called()

    def test_legacy_controller_note_with_assignee_attribution_is_preserved(self):
        self.scheduled_by = 'assignee'
        self.mon['scheduledBy'] = 'assignee'
        self.run_backstop()
        self.api.assert_not_called()

    def test_unrelated_board_monitor_is_not_stretched(self):
        self.mon.update(serviceName='another-service', notes='Board check')
        self.run_backstop()
        self.api.assert_not_called()

    def test_unknown_scheduler_is_not_assumed_to_be_assignee(self):
        self.scheduled_by = ''
        self.mon.pop('scheduledBy')
        self.mon.pop('notes')
        self.run_backstop()
        self.api.assert_not_called()

    def test_assignee_monitor_under_twelve_hours_is_stretched_once(self):
        self.assignee_monitor()
        self.next_at = NOW + 10 * 3600
        self.mon.update(externalRef='https://example.com/ci', timeoutAt=stamp(NOW + 48 * 3600))
        self.run_backstop()
        self.api.assert_called_once()
        policy = self.api.call_args.args[2]['executionPolicy']
        monitor = policy['monitor']
        self.assertEqual(stamp(NOW + 12 * 3600), monitor['nextCheckAt'])
        self.assertEqual('board', monitor['scheduledBy'])
        self.assertEqual(12, monitor['maxAttempts'])
        self.assertEqual(self.mon['timeoutAt'], monitor['timeoutAt'])
        self.assertEqual(self.mon['externalRef'], monitor['externalRef'])
        self.assertEqual('example-ci', monitor['serviceName'])
        self.assertEqual(self.pol['stages'], policy['stages'])
        # Simulate the API's persisted attribution and a later inventory pass.
        self.pol = policy
        self.scheduled_by = 'board'
        self.next_at = NOW + 12 * 3600
        self.env['NOW'] = NOW + 7 * 3600
        self.api.reset_mock()
        self.run_backstop()
        self.api.assert_not_called()

    def test_assignee_monitor_at_or_over_twelve_hours_is_unchanged(self):
        self.assignee_monitor()
        for hours in (12, 24):
            with self.subTest(hours=hours):
                self.next_at = NOW + hours * 3600
                self.run_backstop()
                self.api.assert_not_called()

    def test_due_assignee_monitor_is_not_postponed(self):
        self.assignee_monitor()
        self.next_at = NOW - 1
        self.run_backstop()
        self.api.assert_not_called()

    def test_missing_review_monitor_is_created(self):
        self.pol.pop('monitor')
        self.next_at = 0
        self.scheduled_by = ''
        self.run_backstop()
        monitor = self.api.call_args.args[2]['executionPolicy']['monitor']
        self.assertEqual(stamp(NOW + 12 * 3600), monitor['nextCheckAt'])
        self.assertEqual('board', monitor['scheduledBy'])

    def test_missing_monitor_outside_review_is_not_created(self):
        self.pol.pop('monitor')
        self.next_at = 0
        for status in ('todo', 'in_progress'):
            self.status = status
            self.run_backstop()
        self.api.assert_not_called()

    def test_running_owner_is_not_modified(self):
        self.assignee_monitor()
        self.running.return_value = True
        self.run_backstop()
        self.api.assert_not_called()

    def test_shadow_never_writes(self):
        self.assignee_monitor()
        self.env['SHADOW'] = True
        self.run_backstop()
        self.api.assert_not_called()


class PacketImportTest(unittest.TestCase):
    def test_wake_import_hunk_applies_without_fuzz(self):
        patch = (HERE / 'patches/pr-controller-wakes.patch').read_text()
        # Exercise this integration boundary too: a hunk with no trailing context
        # can be interpreted as EOF and fail on the real, nonempty controller.
        lines = patch.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if line.startswith('@@')]
        import_hunk = ''.join(lines[:starts[1]])
        before = ("sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
                  "import review_error_rerequest as rr  # noqa: E402  (psql, gh_env, comment, card_hold, pr_url_patterns, live_run_guard)\n"
                  "from pr_supply import REPOS as SUPPLY_REPOS  # noqa: E402\n"
                  "\nROOT = rr.ROOT\nCID = rr.CID\n")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'ops/productivity/pr_controller.py'
            target.parent.mkdir(parents=True)
            target.write_text(before)
            result = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1'],
                                    input=import_hunk, cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            tree = ast.parse(target.read_text())
            imports = [n.module for n in tree.body if isinstance(n, ast.ImportFrom)]
            self.assertIn('pr_controller_wakes', imports)


if __name__ == '__main__':
    unittest.main()
