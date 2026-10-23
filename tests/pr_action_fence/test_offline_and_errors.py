"""Containment self-tests and Python final-sender evaluator-failure contracts."""
import contextlib
import socket
import subprocess
import unittest
import urllib.request
from unittest import mock

from support import (ApiSpy, HEAD, INVALID, ISSUE, captured, contained, context,
                     import_productivity, with_context)


class ContainmentSelfTest(unittest.TestCase):
    def test_network_and_process_fallbacks_throw_even_without_credentials(self):
        with contained():
            for call in [lambda: socket.socket(), lambda: socket.create_connection(('example.invalid', 443)),
                         lambda: urllib.request.urlopen('https://example.invalid'),
                         lambda: subprocess.run(['gh', 'pr', 'merge', '1'])]:
                with self.subTest(call=call), self.assertRaisesRegex(AssertionError, 'OFFLINE TRAP'):
                    call()


class PythonHelperFailures(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contained())

    def fail_evaluator(self, consumer, error):
        from support import ROOT
        self.assertTrue((ROOT / 'ops/productivity/pr_action_policy.py').is_file(),
                        'Python helper error contract awaits shared evaluator; sender behavioral RED is separate')
        helper = import_productivity('pr_action_policy')
        original = helper.evaluate
        self.stack.enter_context(mock.patch.object(helper, 'evaluate', side_effect=error))
        # Cover both "import module" and "from module import evaluate" idioms.
        for name, value in vars(consumer).copy().items():
            if value is original:
                self.stack.enter_context(mock.patch.object(consumer, name, side_effect=error))

    def drive(self, sender, error):
        name, fn_name = sender
        consumer = import_productivity(name)
        self.fail_evaluator(consumer, error)
        writes = []
        api = ApiSpy()
        ctx = context(repository='example/other')
        if fn_name == 'post_wake':
            rec = {'pr_context': ctx}
            result, output = captured(lambda: with_context(consumer.post_wake, rec,
                'draft_idle@' + HEAD, ISSUE, 'Offline fixture', api, pr_context=ctx))
            writes = api.calls
        elif fn_name == '_post':
            result, output = captured(lambda: with_context(consumer._post, api, ISSUE,
                'Offline fixture', pr_context=ctx))
            writes = api.calls
        elif fn_name == '_rerun':
            result, output = captured(lambda: with_context(consumer._rerun,
                lambda argv: writes.append(argv) or (0, ''), 101, pr_context=ctx))
        else:
            with mock.patch.object(subprocess, 'run', side_effect=lambda argv, **kwargs:
                                   writes.append(argv) or subprocess.CompletedProcess(argv, 0, stdout='200')):
                result, output = captured(lambda: with_context(consumer.comment, ISSUE,
                    'Offline fixture', pr_context=ctx))
        self.assertEqual([], writes, 'helper failure must deny unrelated subjects, not fall back to allow')
        self.assertIn(INVALID, str(result) + output, 'helper failure must produce a diagnostic denial')


def make_error_test(sender, error_type):
    def test(self):
        error = error_type('offline helper failure')
        self.drive(sender, error)
    return test


for _sender in [('pr_controller_wakes', 'post_wake'), ('pr_controller_ci', '_post'),
                ('pr_controller_ci', '_rerun'), ('review_error_rerequest', 'comment')]:
    for _error in (FileNotFoundError, TimeoutError, ValueError, RuntimeError):
        setattr(PythonHelperFailures, f'test_{_sender[0]}_{_sender[1]}_{_error.__name__}_denies',
                make_error_test(_sender, _error))


if __name__ == '__main__':
    unittest.main()
