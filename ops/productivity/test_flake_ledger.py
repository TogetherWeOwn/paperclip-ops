import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('flake_ledger.py')


def ledger_row(*, repo='two-bot-next', job='rust tests', test='sample_test', flips=0, observations=5):
    return {'repo': repo, 'job': job, 'test': test, 'flips': flips, 'observations': observations}


class EligibilityQuery(unittest.TestCase):
    def query(self, content, *args):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, 'ledger.jsonl')
            if content is not None:
                ledger.write_text(content, encoding='utf-8')
            env = dict(os.environ, FLAKE_LEDGER_PATH=str(ledger))
            return subprocess.run([sys.executable, str(SCRIPT), 'is_flaky', *args],
                                  capture_output=True, text=True, env=env, check=False)

    @staticmethod
    def content(*rows):
        return ''.join(json.dumps(row) + '\n' for row in rows)

    def test_exactly_point_two_is_not_flaky(self):
        result = self.query(self.content(ledger_row(flips=1, observations=5)), 'two-bot-next', 'rust tests')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'no'))

    def test_rate_above_point_two_is_flaky(self):
        result = self.query(self.content(ledger_row(flips=2, observations=5)), 'two-bot-next', 'rust tests')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'yes'))

    def test_insufficient_runs_are_unavailable_not_no(self):
        result = self.query(self.content(ledger_row(flips=1, observations=4)), 'two-bot-next', 'rust tests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_missing_ledger_is_unavailable_not_no(self):
        result = self.query(None, 'two-bot-next', 'rust tests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_corrupt_jsonl_is_unavailable_not_no(self):
        result = self.query('{not-json}\n', 'two-bot-next', 'rust tests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_unreadable_ledger_path_is_unavailable_not_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, FLAKE_LEDGER_PATH=tmp)
            result = subprocess.run([sys.executable, str(SCRIPT), 'is_flaky', 'two-bot-next', 'rust tests'],
                                    capture_output=True, text=True, env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_invalid_ledger_record_is_unavailable_not_no(self):
        row = ledger_row(flips=6, observations=5)
        result = self.query(self.content(row), 'two-bot-next', 'rust tests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_query_matches_test_name_as_well_as_job_name(self):
        result = self.query(self.content(ledger_row(job='rust tests', test='sample_test', flips=2)),
                            'two-bot-next', 'sample_test')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'yes'))

    def test_unrelated_evidence_does_not_become_no(self):
        result = self.query(self.content(ledger_row(repo='other-repo')), 'two-bot-next', 'rust tests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())

    def test_ambiguous_job_rows_are_unavailable_not_combined_as_runs(self):
        content = self.content(
            ledger_row(test='first_test', flips=0),
            ledger_row(test='second_test', flips=0),
        )
        result = self.query(content, 'two-bot-next', 'rust tests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_reads_uploaded_ledger_json_envelope(self):
        row = ledger_row(job='rust tests', test='job rust tests', flips=2, observations=5)
        content = json.dumps({'tests': [row]})
        result = self.query(content, 'two-bot-next', 'rust tests')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'yes'))

    def test_test_and_job_collision_is_unavailable(self):
        content = self.content(
            ledger_row(job='other', test='nightly', flips=5, observations=5),
            ledger_row(job='nightly', test='job nightly', flips=0, observations=5),
        )
        result = self.query(content, 'two-bot-next', 'nightly')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'yes')
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_job_summary_takes_precedence_over_same_job_details(self):
        content = self.content(
            ledger_row(job='nightly', test='job nightly', flips=2, observations=5),
            ledger_row(job='nightly', test='a_detail_test', flips=0, observations=5),
        )
        result = self.query(content, 'two-bot-next', 'nightly')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'yes'))

    def test_duplicate_job_summaries_are_unavailable(self):
        content = self.content(
            ledger_row(job='nightly', test='job nightly', flips=2, observations=5),
            ledger_row(job='nightly', test='job nightly', flips=0, observations=5),
        )
        result = self.query(content, 'two-bot-next', 'nightly')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotEqual(result.stdout.strip(), 'no')

    def test_override_path_works_without_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, 'ledger.jsonl')
            ledger.write_text(self.content(ledger_row(flips=2, observations=5)), encoding='utf-8')
            env = dict(os.environ, FLAKE_LEDGER_PATH=str(ledger))
            env.pop('HOME', None)
            result = subprocess.run([sys.executable, str(SCRIPT), 'is_flaky', 'two-bot-next', 'rust tests'],
                                    capture_output=True, text=True, env=env, check=False)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'yes'))

    def test_missing_home_without_override_is_unavailable(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location('flake_ledger_under_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(module.Path, 'home', side_effect=RuntimeError('no home')):
                with self.assertRaises(module.LedgerUnavailable):
                    module.resolve_ledger_path()


if __name__ == '__main__':
    unittest.main()
