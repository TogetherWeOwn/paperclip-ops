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

    def test_undecodable_bytes_are_never_overwritten(self):
        self.path.write_bytes(b'\xff\xfe binary \x00 not utf-8 \n')
        with self.assertRaises(self.module.LedgerWriteError):
            self.module.record_flake(self.path, self.entry())
        self.assertEqual(self.path.read_bytes(),
                         b'\xff\xfe binary \x00 not utf-8 \n')

    def test_empty_and_whitespace_only_files_start_fresh(self):
        for content in ('', '  \n\t\n'):
            with self.subTest(content=repr(content)):
                path = Path(self.tmp.name, f'fresh-{len(content)}.jsonl')
                path.write_text(content, encoding='utf-8')
                row = self.module.record_flake(path, self.entry())
                self.assertEqual((row['flips'], row['observations']), (1, 2))
                self.assertEqual(len(self.module.read_ledger(path)), 1)

    def test_missing_test_maps_to_job_summary_row(self):
        row = self.module.record_flake(self.path, self.entry(test=None))
        self.assertEqual(row['test'], 'job rust tests')
        # 1 flip / 2 obs is below MIN_RUNS, so the row cannot yet qualify
        # a rerun on its own: the host cannot bootstrap a `yes`.
        with self.assertRaises(self.module.LedgerUnavailable):
            self.module.eligibility(
                self.module.read_ledger(self.path),
                'two-bot-next', 'rust tests')
        client = self.module.LedgerClient(path=str(self.path))
        self.assertEqual(client.verdict('two-bot-next', 'rust tests'),
                         'unavailable')

    def test_blank_test_maps_to_job_summary_row(self):
        row = self.module.record_flake(self.path, self.entry(test='   '))
        self.assertEqual(row['test'], 'job rust tests')

    def test_non_string_test_rejected_without_touching_file(self):
        with self.assertRaises(self.module.LedgerWriteError):
            self.module.record_flake(self.path, self.entry(test=42))
        self.assertFalse(self.path.exists())

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


class ReportUnit(unittest.TestCase):
    """White-box unit regressions for the ranked report helpers.

    The black-box CLI contract lives in test_flake_ledger_report.py (owned
    by the paired acceptance card; never edited here). These tests pin the
    pure helpers directly: ordering, gating, validity, IDs, and arg parsing.
    """

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('flake_ledger_under_test', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_rank_orders_rate_then_observations_then_job_then_test(self):
        rows = [
            ledger_row(job='b job', test='b_test', flips=2, observations=5),
            ledger_row(job='a job', test='z_test', flips=4, observations=10),
            ledger_row(job='a job', test='a_test', flips=4, observations=10),
            ledger_row(job='c job', test='top_test', flips=9, observations=10),
        ]
        ranked, insufficient, truncated = self.module.rank_report_rows(rows, 10, 5)
        self.assertEqual([row['test'] for row in ranked],
                         ['top_test', 'a_test', 'z_test', 'b_test'])
        self.assertEqual((insufficient, truncated), ([], 0))

    def test_gate_boundary_is_inclusive_and_top_counts_truncation(self):
        rows = [
            ledger_row(test='at_gate_test', flips=4, observations=5),
            ledger_row(test='above_gate_test', flips=5, observations=5),
            ledger_row(test='below_gate_test', flips=4, observations=4),
        ]
        ranked, insufficient, truncated = self.module.rank_report_rows(rows, 1, 5)
        self.assertEqual([row['test'] for row in ranked], ['above_gate_test'])
        self.assertEqual([row['test'] for row in insufficient], ['below_gate_test'])
        self.assertEqual(truncated, 1)

    def test_duplicate_keys_raise_naming_the_row(self):
        rows = [ledger_row(flips=4, observations=5),
                ledger_row(flips=0, observations=5)]
        with self.assertRaises(self.module.LedgerUnavailable) as raised:
            self.module.validate_report_rows(rows, 'two-bot-next')
        self.assertIn('sample_test', str(raised.exception))

    def test_cross_row_test_job_collision_raises_naming_the_value(self):
        rows = [ledger_row(job='other', test='nightly', flips=5, observations=5),
                ledger_row(job='nightly', test='job nightly', flips=0, observations=5)]
        with self.assertRaises(self.module.LedgerUnavailable) as raised:
            self.module.validate_report_rows(rows, 'two-bot-next')
        self.assertIn('nightly', str(raised.exception))

    def test_same_row_test_equals_job_is_not_a_collision(self):
        rows = [ledger_row(job='nightly', test='nightly', flips=4, observations=5)]
        self.assertEqual(self.module.validate_report_rows(rows, 'two-bot-next'), rows)

    def test_row_ids_are_stable_unique_and_name_the_row(self):
        first = self.module.report_row_id('two-bot-next', 'rust tests', 'sample_test')
        second = self.module.report_row_id('two-bot-next', 'rust tests', 'sample_test')
        other = self.module.report_row_id('two-bot-next', 'rust tests', 'other_test')
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertIn('rust tests', first)
        self.assertIn('sample_test', first)

    def test_text_format_marks_only_insufficient_lines(self):
        ranked = [ledger_row(test='ranked_test', flips=4, observations=5)]
        insufficient = [ledger_row(test='newbie_test', flips=1, observations=2)]
        text = self.module.format_report_text(
            ranked=ranked, insufficient=insufficient, min_observations=5)
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn('insufficient', lines[0])
        self.assertIn('insufficient', lines[1])
        self.assertIn('ranked_test', lines[0])
        self.assertIn('newbie_test', lines[1])

    def test_parse_rejects_unknown_flags_and_bad_positionals(self):
        with self.assertRaises(self.module.ReportUsageError):
            self.module._parse_report_args(['two-bot-next', '--bogus'])
        with self.assertRaises(self.module.ReportUsageError):
            self.module._parse_report_args([])
        with self.assertRaises(self.module.ReportUsageError):
            self.module._parse_report_args(['   '])
        with self.assertRaises(self.module.ReportUsageError):
            self.module._parse_report_args(['two-bot-next', 'extra'])
        repo, top, minimum, as_json = self.module._parse_report_args(
            ['two-bot-next', '--top', '3', '--min-observations', '7', '--json'])
        self.assertEqual((repo, top, minimum, as_json),
                         ('two-bot-next', 3, 7, True))


class ReportReviewRegressions(unittest.TestCase):
    """CLI regressions for component-ID aliasing and lost exclusion counts."""

    def report(self, rows, repo='r', *args):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, 'ledger.jsonl')
            ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows),
                              encoding='utf-8')
            before = ledger.read_bytes()
            env = dict(os.environ, FLAKE_LEDGER_PATH=str(ledger))
            documents = []
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), 'report', repo, '--json', *args],
                    capture_output=True, text=True, env=env, check=False,
                    timeout=15)
                self.assertEqual(ledger.read_bytes(), before,
                                 'report must leave ledger bytes unchanged')
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                                 ['ledger.jsonl'], 'report must not create sidecars')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                document = json.loads(result.stdout)
                self.assertIsNone(document['error'])
                for row in document['rows'] + document['insufficient']:
                    self.assertIsInstance(row['id'], str)
                    self.assertTrue(row['id'], 'row IDs must be nonempty')
                document.pop('generated_at')
                documents.append(document)
            self.assertEqual(documents[0], documents[1],
                             'IDs, counts, and ranking must survive repeated invocation')
            return documents[0]

    def test_slash_components_have_distinct_ids_and_preserve_ranked_evidence(self):
        # Raw slash joining aliases these different valid (job, test) tuples.
        rows = [ledger_row(repo='r', job='a', test='b/c', flips=1, observations=10),
                ledger_row(repo='r', job='a/b', test='c', flips=9, observations=10)]
        document = self.report(rows)
        ranked = document['rows']
        self.assertEqual([(row['job'], row['test'], row['flips'], row['observations'])
                          for row in ranked],
                         [('a/b', 'c', 9, 10), ('a', 'b/c', 1, 10)])
        self.assertEqual([row['rate'] for row in ranked], [0.9, 0.1])
        self.assertEqual(document['insufficient'], [])
        self.assertEqual(document['excluded'], [])
        self.assertEqual(len({row['id'] for row in ranked}), 2,
                         'different component tuples must not share an ID')
        reversed_document = self.report(list(reversed(rows)))
        self.assertEqual(reversed_document, document,
                         'IDs must not depend on ledger row order')

    def test_ids_stay_disjoint_and_stable_across_the_observation_gate(self):
        rows = [ledger_row(repo='r', job='a/b', test='c', flips=9, observations=10),
                ledger_row(repo='r', job='a', test='b/c', flips=1, observations=4)]
        loose = self.report(rows, 'r', '--min-observations', '1')
        gated = self.report(rows, 'r', '--min-observations', '5')
        self.assertEqual([(row['job'], row['test'], row['flips'], row['observations'])
                          for row in gated['rows']], [('a/b', 'c', 9, 10)])
        self.assertEqual([(row['job'], row['test'], row['flips'], row['observations'])
                          for row in gated['insufficient']], [('a', 'b/c', 1, 4)])
        self.assertEqual(gated['excluded'],
                         [{'reason': 'insufficient_observations', 'count': 1}])
        ranked_ids = {row['id'] for row in gated['rows']}
        short_ids = {row['id'] for row in gated['insufficient']}
        self.assertTrue(ranked_ids.isdisjoint(short_ids),
                        'sub-gate and ranked tuples must not share an ID')
        self.assertEqual([(row['job'], row['test']) for row in loose['rows']],
                         [('a/b', 'c'), ('a', 'b/c')])
        self.assertEqual({(row['job'], row['test']): row['id']
                          for row in loose['rows']},
                         {(row['job'], row['test']): row['id']
                          for row in gated['rows'] + gated['insufficient']},
                         'an ID identifies a tuple, not its eligibility bucket')

    def test_literal_percent_escape_aliases_remain_distinct_in_each_component(self):
        # Escaping slash alone would alias a literal "%2F" with an escaped slash.
        for component in ('repo', 'job', 'test'):
            with self.subTest(component=component):
                first = ledger_row(repo='r', job='worker', test='sample',
                                   flips=9, observations=10)
                second = ledger_row(repo='r', job='worker', test='sample',
                                    flips=1, observations=10)
                first[component] = 'a%2Fb'
                second[component] = 'a/b'
                rows = [second, first]
                if component == 'repo':
                    reported = (self.report(rows, first['repo'])['rows'] +
                                self.report(rows, second['repo'])['rows'])
                else:
                    reported = self.report(rows)['rows']
                self.assertEqual([(row['job'], row['test'], row['flips'], row['observations'])
                                  for row in reported],
                                 [(first['job'], first['test'], 9, 10),
                                  (second['job'], second['test'], 1, 10)])
                self.assertEqual(len({row['id'] for row in reported}), 2,
                                 'literal percent escapes and slash must not alias')

    def test_repository_and_job_and_test_boundaries_do_not_alias(self):
        rows = [ledger_row(repo='r/a', job='b', test='c', flips=8, observations=10),
                ledger_row(repo='r', job='a/b', test='c', flips=9, observations=10),
                ledger_row(repo='r', job='a', test='b/c', flips=1, observations=10)]
        nested_repo = self.report(rows, 'r/a')
        root_repo = self.report(rows)
        self.assertEqual(nested_repo['repo'], 'r/a')
        self.assertEqual(root_repo['repo'], 'r')
        self.assertEqual([(row['job'], row['test'], row['flips'])
                          for row in nested_repo['rows']], [('b', 'c', 8)])
        self.assertEqual([(row['job'], row['test'], row['flips'])
                          for row in root_repo['rows']], [('a/b', 'c', 9), ('a', 'b/c', 1)])
        combined = nested_repo['rows'] + root_repo['rows']
        self.assertEqual(len({row['id'] for row in combined}), 3,
                         'repo is an independent ID component, not a path prefix')

    def test_plain_names_remain_readable_in_ids(self):
        document = self.report([ledger_row(job='rust tests', test='sample_test')],
                               'two-bot-next')
        row_id = document['rows'][0]['id']
        self.assertIn('rust tests', row_id)
        self.assertIn('sample_test', row_id)

    @staticmethod
    def eligible_rows():
        return [ledger_row(repo='r', test=f'ranked_{index}_test',
                           flips=10 - index, observations=10)
                for index in range(6)]

    def test_top_truncation_and_insufficient_counts_are_separate(self):
        rows = self.eligible_rows() + [ledger_row(repo='r', test='newbie_test',
                                                flips=1, observations=4)]
        document = self.report(rows, 'r', '--top', '2', '--min-observations', '5')
        self.assertEqual(document['top'], 2)
        self.assertEqual([row['test'] for row in document['rows']],
                         ['ranked_0_test', 'ranked_1_test'])
        self.assertEqual([(row['test'], row['flips'], row['observations'])
                          for row in document['insufficient']], [('newbie_test', 1, 4)])
        self.assertCountEqual(document['excluded'],
                              [{'reason': 'top_truncated', 'count': 4},
                               {'reason': 'insufficient_observations', 'count': 1}])

    def test_top_at_or_above_eligible_count_has_only_insufficient_exclusion(self):
        rows = self.eligible_rows() + [ledger_row(repo='r', test='newbie_test',
                                                flips=1, observations=4)]
        for top in ('6', '100'):
            with self.subTest(top=top):
                document = self.report(rows, 'r', '--top', top)
                self.assertEqual([row['test'] for row in document['rows']],
                                 [f'ranked_{index}_test' for index in range(6)])
                self.assertEqual([row['test'] for row in document['insufficient']],
                                 ['newbie_test'])
                self.assertEqual(document['excluded'],
                                 [{'reason': 'insufficient_observations', 'count': 1}])

    def test_all_eligible_without_truncation_has_no_exclusions(self):
        for top in ('6', '100'):
            with self.subTest(top=top):
                document = self.report(self.eligible_rows(), 'r', '--top', top)
                self.assertEqual(len(document['rows']), 6)
                self.assertEqual(document['insufficient'], [])
                self.assertEqual(document['excluded'], [])


if __name__ == '__main__':
    unittest.main()
