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

    def test_corrupt_unmatched_row_makes_valid_matching_evidence_unavailable(self):
        matched = ledger_row(flips=2, observations=5)
        invalid_rows = (
            None,
            {'repo': 'other-repo', 'job': 'other job'},
            ledger_row(repo='other-repo', flips=9, observations=5),
        )
        for invalid in invalid_rows:
            for rows in ([matched, invalid], [invalid, matched]):
                for envelope in (False, True):
                    with self.subTest(invalid=invalid, rows=rows, envelope=envelope):
                        content = json.dumps({'tests': rows}) if envelope else self.content(*rows)
                        result = self.query(content, 'two-bot-next', 'rust tests')
                        self.assertEqual(result.returncode, 2)
                        self.assertTrue(result.stdout.startswith('unavailable:'))
                        self.assertNotEqual(result.stdout.strip(), 'yes')
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


class RecordFlake(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('flake_ledger_under_test', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, 'ledger.jsonl')

    def entry(self, **overrides):
        base = {'repo': 'two-bot-next', 'workflow': 'check.yml',
                'job': 'rust tests', 'test': 'sample_test', 'run_id': 101,
                'attempt': 2, 'head_sha': 'a' * 40}
        base.update(overrides)
        return base

    def test_missing_file_creates_flips_one_observations_two(self):
        row = self.module.record_flake(self.path, self.entry())
        self.assertEqual((row['flips'], row['observations']), (1, 2))
        rows = self.module.read_ledger(self.path)
        self.assertEqual(len(rows), 1)

    def test_repeat_flake_increments_same_row(self):
        self.module.record_flake(self.path, self.entry())
        row = self.module.record_flake(self.path, self.entry())
        self.assertEqual((row['flips'], row['observations']), (2, 4))
        self.assertEqual(len(self.module.read_ledger(self.path)), 1)
        row = self.module.record_flake(self.path, self.entry())
        self.assertEqual((row['flips'], row['observations']), (3, 6))
        self.assertEqual(
            self.module.eligibility(
                self.module.read_ledger(self.path),
                'two-bot-next', 'sample_test'),
            'yes')

    def test_other_rows_are_preserved(self):
        other = ledger_row(repo='two-web-next', flips=2, observations=5)
        self.path.write_text(json.dumps(other) + '\n', encoding='utf-8')
        self.module.record_flake(self.path, self.entry())
        rows = self.module.read_ledger(self.path)
        self.assertEqual(len(rows), 2)
        kept = [row for row in rows if row['repo'] == 'two-web-next'][0]
        self.assertEqual((kept['flips'], kept['observations']), (2, 5))

    def test_corrupt_file_is_never_overwritten(self):
        self.path.write_text('{not-json}\n', encoding='utf-8')
        with self.assertRaises(self.module.LedgerWriteError):
            self.module.record_flake(self.path, self.entry())
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{not-json}\n')

    def test_bad_entry_rejected_without_touching_file(self):
        with self.assertRaises(self.module.LedgerWriteError):
            self.module.record_flake(self.path, {'repo': 'x'})
        self.assertFalse(self.path.exists())

    def test_client_verdict_never_raises(self):
        client = self.module.LedgerClient(path=str(self.path))
        self.assertEqual(client.verdict('two-bot-next', 'rust tests'), 'unavailable')
        client.record_flake(self.entry())
        self.assertEqual(client.verdict('two-bot-next', 'sample_test'), 'unavailable')


class QuarantineCard(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('flake_ledger_under_test', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.calls = []

    def post(self, method, path, payload):
        self.calls.append((method, path, dict(payload)))
        return ('201', json.dumps({'id': 'card-1'}))

    def payload(self):
        return {'title': '[flake] two-bot-next check.yml/rust tests flaked 3 times in 7 days',
                'repo': 'two-bot-next', 'workflow': 'check.yml',
                'job': 'rust tests', 'test': 'sample_test', 'count': 3,
                'window_days': 7, 'run_ids': ['101', '102', '103']}

    def test_create_when_no_card_exists(self):
        card_id = self.module.quarantine_card(
            find_card=lambda payload: None, post=self.post,
            company_id='company-1', payload=self.payload())
        self.assertEqual(card_id, 'card-1')
        self.assertEqual(len(self.calls), 1)
        method, path, body = self.calls[0]
        self.assertEqual((method, path), ('POST', 'companies/company-1/issues'))
        self.assertIn('[flake]', body['title'])

    def test_update_comments_existing_card_once(self):
        card_id = self.module.quarantine_card(
            find_card=lambda payload: 'card-9', post=self.post,
            company_id='company-1', payload=self.payload())
        self.assertEqual(card_id, 'card-9')
        self.assertEqual(len(self.calls), 1)
        method, path, body = self.calls[0]
        self.assertEqual(path, 'issues/card-9/comments')
        self.assertIn('3 flakes', body['body'])

    def test_non_flake_title_rejected(self):
        with self.assertRaises(ValueError):
            self.module.quarantine_card(
                find_card=lambda payload: None, post=self.post,
                company_id='company-1', payload={'title': 'regular card'})

    def test_failed_post_raises_for_retry(self):
        def failing_post(method, path, payload):
            return ('500', '{}')
        with self.assertRaises(self.module.LedgerWriteError):
            self.module.quarantine_card(
                find_card=lambda payload: None, post=failing_post,
                company_id='company-1', payload=self.payload())


if __name__ == '__main__':
    unittest.main()
