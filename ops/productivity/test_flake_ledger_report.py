"""Acceptance tests for the flake-ledger ranked triage report (M11.2).

Covers the read-only ``report`` subcommand of ``flake_ledger.py``::

    flake_ledger.py report <repo> [--top N] [--min-observations M] [--json] [--dry-run]

Contract pinned by these tests (paired build card implements to this file):

* Rows matching ``<repo>`` exactly are ranked by flip rate (flips /
  observations) descending. Ties break deterministically: observations
  descending, then job name ascending, then test name ascending.
* ``--top N`` defaults to 10 and accepts integers 1-100 only.
* ``--min-observations M`` defaults to 5 and accepts positive integers only.
  Rows below the gate are listed as ``insufficient`` with counts and never
  appear in the ranked set, regardless of ``--top``.
* Strict validity, same as ``is_flaky``: every ledger row must be
  well-formed (including other repos' rows); duplicate (job, test) pairs or
  test-vs-job collisions within the repo's rows make the whole report
  unavailable (nonzero exit + explicit reason), never silently combined.
  A test value colliding with a job value inside the SAME row is fine and
  mirrors ``is_flaky`` (same-row self-match is unambiguous).
* Text form prints one row per line (ranked rows, then insufficient rows
  each marked with the literal token ``insufficient``) with no timestamps:
  byte-deterministic across runs. Each ranked line carries flips/observations
  counts; greppable, no prose scraping required.
* ``--json`` prints a pure-JSON envelope on stdout: schema_version 1, repo,
  generated_at (ISO-8601), top, min_observations, ranked ``rows`` (stable
  id + job/test + flips/observations/rate), ``insufficient`` rows with
  counts, ``excluded`` counts with reasons, and ``error`` (null on success;
  {code, retryable, next_action} on failure).
* Read-only: ledger bytes are unchanged after every invocation. ``--dry-run``
  is a no-op alias (byte-identical text output). Unknown flags, missing or
  blank repo, and extra positionals refuse with usage + exit 2.

Every test run asserts the ledger bytes are unchanged, so read-onlyness is
proven by the suite itself, not by a single dedicated test.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('flake_ledger.py')
REPO = 'two-bot-next'


def ledger_row(*, repo=REPO, job='rust tests', test='sample_test', flips=0, observations=5):
    return {'repo': repo, 'job': job, 'test': test, 'flips': flips, 'observations': observations}


def jsonl_content(*rows):
    return ''.join(json.dumps(row) + '\n' for row in rows)


class ReportCase(unittest.TestCase):
    """Black-box ``report`` runs with automatic read-only proof."""

    def run_report(self, content, *args):
        """Run ``report`` against a fixture ledger; fail if ledger bytes change.

        ``content`` is the fixture ledger text, or None for a missing file.
        Returns the CompletedProcess. Every invocation proves read-onlyness.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Path(tmp.name, 'ledger.jsonl')
        before = None
        if content is not None:
            ledger.write_bytes(content.encode('utf-8'))
            before = ledger.read_bytes()
        env = dict(os.environ, FLAKE_LEDGER_PATH=str(ledger))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), 'report', *args],
            capture_output=True, text=True, env=env, check=False)
        if before is not None:
            self.assertEqual(ledger.read_bytes(), before,
                             'report must never modify the ledger')
            leftovers = [p.name for p in Path(tmp.name).iterdir()
                         if p.name != 'ledger.jsonl']
            self.assertEqual(leftovers, [],
                             'report must not leave sidecar files behind')
        return result

    def run_report_twice(self, content, *args):
        """Run ``report`` twice against one persistent fixture; prove read-only."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Path(tmp.name, 'ledger.jsonl')
        ledger.write_text(content, encoding='utf-8')
        before = ledger.read_bytes()
        env = dict(os.environ, FLAKE_LEDGER_PATH=str(ledger))
        first = subprocess.run(
            [sys.executable, str(SCRIPT), 'report', *args],
            capture_output=True, text=True, env=env, check=False)
        second = subprocess.run(
            [sys.executable, str(SCRIPT), 'report', *args],
            capture_output=True, text=True, env=env, check=False)
        self.assertEqual(ledger.read_bytes(), before,
                         'report must never modify the ledger')
        return first, second

    @staticmethod
    def combined(result):
        return result.stdout + result.stderr

    def assert_unavailable(self, result):
        self.assertNotEqual(result.returncode, 0,
                            'invalid evidence must fail, never rank')
        self.assertIn('unavailable', self.combined(result).lower(),
                      'unavailable reports must say so explicitly')

    def assert_usage_refused(self, result):
        self.assertEqual(result.returncode, 2,
                         'usage errors refuse with exit 2')
        self.assertIn('usage', self.combined(result).lower(),
                      'usage errors must print usage')
        self.assertIn('report', self.combined(result),
                      'usage must document the report subcommand')

    def line_with(self, result, token):
        matches = [line for line in result.stdout.splitlines() if token in line]
        self.assertEqual(len(matches), 1,
                         f'expected exactly one output line with {token!r}:\n'
                         f'{result.stdout}')
        return matches[0]

    def assert_ranked_before(self, result, first, second):
        """Assert two row names land on distinct lines in rank order."""
        first_line = self.line_with(result, first)
        second_line = self.line_with(result, second)
        lines = result.stdout.splitlines()
        self.assertLess(lines.index(first_line), lines.index(second_line),
                        f'{first!r} must rank above {second!r}:\n{result.stdout}')

    def parse_envelope(self, result):
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f'--json stdout must be pure JSON:\n{result.stdout!r}\n'
                      f'stderr:\n{result.stderr!r}')


class ReportRanking(ReportCase):
    def test_ranks_by_flip_rate_descending(self):
        content = jsonl_content(
            ledger_row(test='middling_test', flips=2, observations=10),
            ledger_row(test='flakiest_test', flips=9, observations=10),
            ledger_row(test='cleanest_test', flips=0, observations=10),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertNotIn('unavailable', self.combined(result).lower())
        self.assert_ranked_before(result, 'flakiest_test', 'middling_test')
        self.assert_ranked_before(result, 'middling_test', 'cleanest_test')

    def test_clean_rows_still_rank_when_inside_top(self):
        # Ranking covers every eligible row, not only rate > 0.2 rows.
        content = jsonl_content(
            ledger_row(test='flaky_one', flips=4, observations=5),
            ledger_row(test='clean_one', flips=1, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.line_with(result, 'flaky_one')
        self.line_with(result, 'clean_one')
        self.assert_ranked_before(result, 'flaky_one', 'clean_one')

    def test_rate_tie_breaks_on_observations_descending(self):
        content = jsonl_content(
            ledger_row(test='few_obs_test', flips=2, observations=5),
            ledger_row(test='many_obs_test', flips=4, observations=10),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assert_ranked_before(result, 'many_obs_test', 'few_obs_test')

    def test_full_tie_breaks_on_name_ascending(self):
        content = jsonl_content(
            ledger_row(test='zulu_test', flips=2, observations=5),
            ledger_row(test='alpha_test', flips=2, observations=5),
            ledger_row(test='mike_test', flips=2, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assert_ranked_before(result, 'alpha_test', 'mike_test')
        self.assert_ranked_before(result, 'mike_test', 'zulu_test')

    def test_tie_across_jobs_breaks_on_job_then_test_ascending(self):
        content = jsonl_content(
            ledger_row(job='zulu job', test='aaa_test', flips=2, observations=5),
            ledger_row(job='alpha job', test='zzz_test', flips=2, observations=5),
            ledger_row(job='alpha job', test='aaa_test', flips=2, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        envelope = self.parse_envelope(self.run_report(content, REPO, '--json'))
        names = [(row['job'], row['test']) for row in envelope['rows']]
        self.assertEqual(names, [('alpha job', 'aaa_test'),
                                 ('alpha job', 'zzz_test'),
                                 ('zulu job', 'aaa_test')])

    def test_ranked_text_lines_carry_flips_and_observations(self):
        content = jsonl_content(
            ledger_row(test='counted_test', flips=7, observations=23),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        line = self.line_with(result, 'counted_test')
        self.assertIn('7', line)
        self.assertIn('23', line)

    def test_job_summary_and_detail_rows_coexist_unranked_together(self):
        # Unlike is_flaky precedence, the report ranks rows as rows: a
        # summary row plus its detail rows are all listed, uncombined.
        content = jsonl_content(
            ledger_row(job='nightly', test='job nightly', flips=4, observations=5),
            ledger_row(job='nightly', test='a_detail_test', flips=0, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assert_ranked_before(result, 'job nightly', 'a_detail_test')

    def test_same_row_test_equals_job_is_not_a_collision(self):
        # Mirrors is_flaky: a row matching as both test and job is one row,
        # unambiguous when it is the same row.
        content = jsonl_content(
            ledger_row(job='nightly', test='nightly', flips=4, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.line_with(result, 'nightly')

    def test_only_requested_repo_rows_rank(self):
        content = jsonl_content(
            ledger_row(repo=REPO, test='ours_test', flips=1, observations=5),
            ledger_row(repo='other-repo', test='theirs_test', flips=5, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.line_with(result, 'ours_test')
        self.assertNotIn('theirs_test', result.stdout)

    def test_repo_match_is_exact_and_case_sensitive(self):
        content = jsonl_content(ledger_row(test='ours_test', flips=4, observations=5))
        result = self.run_report(content, REPO.upper())
        self.assert_unavailable(result)

    def test_text_output_is_deterministic_across_runs(self):
        content = jsonl_content(
            ledger_row(test='b_test', flips=3, observations=8),
            ledger_row(test='a_test', flips=3, observations=8),
            ledger_row(test='c_test', flips=0, observations=2),
        )
        first, second = self.run_report_twice(content, REPO)
        self.assertEqual(first.returncode, 0, self.combined(first))
        self.assertEqual(second.stdout, first.stdout)


class ReportTop(ReportCase):
    def twelve_rows(self):
        rows = []
        for index in range(12):
            rows.append(ledger_row(test=f'ranked_{index:02d}_test',
                                   flips=12 - index, observations=20))
        return jsonl_content(*rows)

    def test_default_top_is_ten(self):
        result = self.run_report(self.twelve_rows(), REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        for index in range(10):
            self.line_with(result, f'ranked_{index:02d}_test')
        self.assertNotIn('ranked_10_test', result.stdout)
        self.assertNotIn('ranked_11_test', result.stdout)

    def test_top_truncates_to_rate_prefix(self):
        result = self.run_report(self.twelve_rows(), REPO, '--top', '3')
        self.assertEqual(result.returncode, 0, self.combined(result))
        for index in range(3):
            self.line_with(result, f'ranked_{index:02d}_test')
        self.assertNotIn('ranked_03_test', result.stdout)

    def test_top_larger_than_row_count_lists_everything(self):
        content = jsonl_content(
            ledger_row(test='first_test', flips=4, observations=5),
            ledger_row(test='second_test', flips=0, observations=5),
        )
        result = self.run_report(content, REPO, '--top', '100')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.line_with(result, 'first_test')
        self.line_with(result, 'second_test')

    def test_top_bounds_reject_zero_negative_over_hundred_and_text(self):
        content = jsonl_content(ledger_row(flips=4, observations=5))
        for bad in ('0', '-1', '101', 'abc', '3.5', ''):
            with self.subTest(top=bad):
                result = self.run_report(content, REPO, '--top', bad)
                self.assertEqual(result.returncode, 2,
                                 f'--top {bad!r} must refuse with exit 2')
                self.assertIn('top', self.combined(result).lower(),
                              '--top refusal must name the flag')

    def test_top_missing_value_refuses(self):
        content = jsonl_content(ledger_row(flips=4, observations=5))
        result = self.run_report(content, REPO, '--top')
        self.assertEqual(result.returncode, 2, self.combined(result))
        self.assertIn('top', self.combined(result).lower())


class ReportGating(ReportCase):
    def test_sub_gate_rows_list_as_insufficient_never_ranked(self):
        content = jsonl_content(
            ledger_row(test='eligible_test', flips=4, observations=5),
            ledger_row(test='newbie_test', flips=1, observations=2),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.line_with(result, 'eligible_test')
        line = self.line_with(result, 'newbie_test')
        self.assertIn('insufficient', line)
        self.assertIn('1', line)
        self.assertIn('2', line)

    def test_insufficient_list_ignores_top_truncation(self):
        rows = [ledger_row(test=f'eligible_{index}_test', flips=4, observations=5)
                for index in range(2)]
        rows += [ledger_row(test=f'newbie_{index}_test', flips=0, observations=1)
                 for index in range(3)]
        result = self.run_report(jsonl_content(*rows), REPO, '--top', '1')
        self.assertEqual(result.returncode, 0, self.combined(result))
        for index in range(3):
            line = self.line_with(result, f'newbie_{index}_test')
            self.assertIn('insufficient', line)

    def test_all_rows_insufficient_is_success_with_empty_ranking(self):
        content = jsonl_content(
            ledger_row(test='newbie_test', flips=0, observations=1),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        line = self.line_with(result, 'newbie_test')
        self.assertIn('insufficient', line)
        envelope = self.parse_envelope(self.run_report(content, REPO, '--json'))
        self.assertEqual(envelope['rows'], [])
        self.assertEqual(len(envelope['insufficient']), 1)

    def test_zero_observation_row_is_insufficient_not_an_error(self):
        content = jsonl_content(
            ledger_row(test='unobserved_test', flips=0, observations=0),
        )
        result = self.run_report(content, REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        line = self.line_with(result, 'unobserved_test')
        self.assertIn('insufficient', line)

    def test_custom_min_observations_moves_rows_across_the_gate(self):
        content = jsonl_content(
            ledger_row(test='five_obs_test', flips=4, observations=5),
            ledger_row(test='twelve_obs_test', flips=1, observations=12),
        )
        strict = self.run_report(content, REPO, '--min-observations', '10')
        self.assertEqual(strict.returncode, 0, self.combined(strict))
        line = self.line_with(strict, 'five_obs_test')
        self.assertIn('insufficient', line)
        ranked = self.line_with(strict, 'twelve_obs_test')
        self.assertNotIn('insufficient', ranked)
        loose = self.run_report(content, REPO, '--min-observations', '1')
        self.assertEqual(loose.returncode, 0, self.combined(loose))
        self.assertNotIn('insufficient', self.line_with(loose, 'five_obs_test'))

    def test_huge_min_observations_lists_everything_insufficient(self):
        content = jsonl_content(ledger_row(test='old_test', flips=4, observations=5))
        result = self.run_report(content, REPO, '--min-observations', '1000000')
        self.assertEqual(result.returncode, 0, self.combined(result))
        line = self.line_with(result, 'old_test')
        self.assertIn('insufficient', line)

    def test_min_observations_rejects_non_positive_integers(self):
        content = jsonl_content(ledger_row(flips=4, observations=5))
        for bad in ('0', '-3', 'abc', '2.5', ''):
            with self.subTest(min_observations=bad):
                result = self.run_report(content, REPO, '--min-observations', bad)
                self.assertEqual(result.returncode, 2,
                                 '--min-observations must refuse with exit 2')
                self.assertIn('min-observations', self.combined(result).lower())


class ReportValidity(ReportCase):
    def test_test_vs_job_collision_is_unavailable(self):
        content = jsonl_content(
            ledger_row(job='other', test='nightly', flips=5, observations=5),
            ledger_row(job='nightly', test='job nightly', flips=0, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assert_unavailable(result)
        self.assertIn('nightly', self.combined(result))

    def test_identical_duplicate_rows_are_unavailable_not_deduped(self):
        row = ledger_row(flips=4, observations=5)
        content = jsonl_content(row, dict(row))
        result = self.run_report(content, REPO)
        self.assert_unavailable(result)

    def test_duplicate_keys_with_different_evidence_are_unavailable(self):
        content = jsonl_content(
            ledger_row(flips=4, observations=5),
            ledger_row(flips=0, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assert_unavailable(result)
        combined = self.combined(result)
        self.assertTrue('sample_test' in combined or 'rust tests' in combined,
                        'duplicate reason must name the row')

    def test_malformed_rows_are_unavailable(self):
        good = ledger_row(flips=4, observations=5)
        malformed = [
            {'repo': REPO, 'job': 'rust tests'},
            ledger_row(flips=9, observations=5),
            ledger_row(flips=-1, observations=5),
            ledger_row(flips='2', observations=5),
            ledger_row(flips=2, observations=5, test='  '),
            'just a string',
            None,
        ]
        for bad in malformed:
            with self.subTest(bad=bad):
                content = jsonl_content(good, bad)
                result = self.run_report(content, REPO)
                self.assert_unavailable(result)

    def test_malformed_row_in_other_repo_is_unavailable(self):
        # Same strict validity as is_flaky: every ledger row must be valid,
        # even rows outside the requested repo.
        content = jsonl_content(
            ledger_row(flips=4, observations=5),
            {'repo': 'other-repo', 'job': 'other job'},
        )
        result = self.run_report(content, REPO)
        self.assert_unavailable(result)

    def test_corrupt_json_is_unavailable(self):
        result = self.run_report('{not-json}\n', REPO)
        self.assert_unavailable(result)

    def test_missing_ledger_is_unavailable(self):
        result = self.run_report(None, REPO)
        self.assert_unavailable(result)

    def test_empty_ledger_is_unavailable(self):
        for content in ('', '  \n\t\n', '[]\n', '{"tests": []}\n'):
            with self.subTest(content=repr(content)):
                result = self.run_report(content, REPO)
                self.assert_unavailable(result)

    def test_repo_without_rows_is_unavailable(self):
        content = jsonl_content(
            ledger_row(repo='other-repo', test='theirs_test', flips=4, observations=5),
        )
        result = self.run_report(content, REPO)
        self.assert_unavailable(result)
        self.assertIn(REPO, self.combined(result))

    def test_reads_json_envelope_fixtures(self):
        row = ledger_row(test='envelope_test', flips=4, observations=5)
        result = self.run_report(json.dumps({'tests': [row]}), REPO)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.line_with(result, 'envelope_test')


class ReportJson(ReportCase):
    def fixture(self):
        return jsonl_content(
            ledger_row(test='flakiest_test', flips=9, observations=10),
            ledger_row(test='middling_test', flips=2, observations=10),
            ledger_row(test='newbie_test', flips=1, observations=2),
        )

    def test_envelope_schema_on_success(self):
        result = self.run_report(self.fixture(), REPO, '--json')
        self.assertEqual(result.returncode, 0, self.combined(result))
        envelope = self.parse_envelope(result)
        self.assertIs(type(envelope['schema_version']), int)
        self.assertEqual(envelope['schema_version'], 1)
        self.assertEqual(envelope['repo'], REPO)
        self.assertEqual(envelope['top'], 10)
        self.assertEqual(envelope['min_observations'], 5)
        generated = envelope['generated_at']
        self.assertIsInstance(generated, str)
        stamp = generated[:-1] + '+00:00' if generated.endswith('Z') else generated
        datetime.datetime.fromisoformat(stamp)
        self.assertIsNone(envelope['error'])

    def test_ranked_rows_carry_stable_ids_and_exact_counts(self):
        result = self.run_report(self.fixture(), REPO, '--json')
        self.assertEqual(result.returncode, 0, self.combined(result))
        rows = self.parse_envelope(result)['rows']
        self.assertEqual([row['test'] for row in rows],
                         ['flakiest_test', 'middling_test'])
        for row in rows:
            for key in ('id', 'job', 'test', 'flips', 'observations', 'rate'):
                self.assertIn(key, row)
            self.assertIsInstance(row['id'], str)
            self.assertTrue(row['id'], 'stable IDs must be non-empty')
            self.assertIn(row['job'], row['id'])
            self.assertIn(row['test'], row['id'])
            self.assertIs(type(row['flips']), int)
            self.assertIs(type(row['observations']), int)
            self.assertAlmostEqual(row['rate'],
                                   row['flips'] / row['observations'])
        self.assertEqual(rows[0]['flips'], 9)
        self.assertEqual(rows[0]['observations'], 10)
        ids = [row['id'] for row in rows]
        self.assertEqual(len(set(ids)), len(ids), 'IDs must be unique')

    def test_insufficient_rows_listed_with_counts_and_excluded(self):
        result = self.run_report(self.fixture(), REPO, '--json')
        self.assertEqual(result.returncode, 0, self.combined(result))
        envelope = self.parse_envelope(result)
        short = envelope['insufficient']
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0]['test'], 'newbie_test')
        self.assertEqual((short[0]['flips'], short[0]['observations']), (1, 2))
        ranked_ids = {row['id'] for row in envelope['rows']}
        short_ids = {row['id'] for row in short}
        self.assertTrue(ranked_ids.isdisjoint(short_ids),
                        'insufficient rows must never appear in the ranking')
        excluded = envelope['excluded']
        self.assertIsInstance(excluded, list)
        bucket = [entry for entry in excluded
                  if entry.get('reason') == 'insufficient_observations']
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket[0]['count'], 1)

    def test_no_exclusions_yields_empty_excluded_list(self):
        content = jsonl_content(ledger_row(flips=4, observations=5))
        result = self.run_report(content, REPO, '--json')
        self.assertEqual(result.returncode, 0, self.combined(result))
        envelope = self.parse_envelope(result)
        self.assertEqual(envelope['insufficient'], [])
        self.assertEqual(envelope['excluded'], [])

    def test_json_respects_top_and_min_observations(self):
        rows = [ledger_row(test=f'ranked_{index}_test',
                           flips=10 - index, observations=10)
                for index in range(6)]
        rows.append(ledger_row(test='newbie_test', flips=0, observations=3))
        content = jsonl_content(*rows)
        result = self.run_report(content, REPO, '--json',
                                 '--top', '2', '--min-observations', '10')
        self.assertEqual(result.returncode, 0, self.combined(result))
        envelope = self.parse_envelope(result)
        self.assertEqual(envelope['top'], 2)
        self.assertEqual(envelope['min_observations'], 10)
        self.assertEqual([row['test'] for row in envelope['rows']],
                         ['ranked_0_test', 'ranked_1_test'])
        short = {row['test'] for row in envelope['insufficient']}
        self.assertIn('newbie_test', short)

    def test_unavailable_json_carries_coded_error(self):
        row = ledger_row(flips=4, observations=5)
        content = jsonl_content(row, dict(row))
        result = self.run_report(content, REPO, '--json')
        self.assert_unavailable(result)
        envelope = self.parse_envelope(result)
        self.assertEqual(envelope['schema_version'], 1)
        self.assertEqual(envelope['repo'], REPO)
        self.assertEqual(envelope['rows'], [])
        error = envelope['error']
        self.assertIsInstance(error, dict)
        self.assertTrue(error.get('code'), 'error must carry a code')
        self.assertIsInstance(error.get('retryable'), bool)
        self.assertTrue(error.get('next_action'),
                        'error must say what to do next')

    def test_json_is_deterministic_except_generated_at(self):
        first, second = self.run_report_twice(self.fixture(), REPO, '--json')
        self.assertEqual(first.returncode, 0, self.combined(first))
        self.assertEqual(second.returncode, 0, self.combined(second))
        first_doc = json.loads(first.stdout)
        second_doc = json.loads(second.stdout)
        del first_doc['generated_at']
        del second_doc['generated_at']
        self.assertEqual(first_doc, second_doc,
                         'stable IDs and ordering must not drift between runs')


class ReportCli(ReportCase):
    def fixture(self):
        return jsonl_content(ledger_row(test='cli_test', flips=4, observations=5))

    def test_unknown_flags_refuse_with_usage_and_exit_2(self):
        for argv in (['--bogus'], ['--json', '--bogus'], ['--top', '3', '--nope']):
            with self.subTest(argv=argv):
                result = self.run_report(self.fixture(), REPO, *argv)
                self.assert_usage_refused(result)

    def test_missing_repo_refuses_with_usage_and_exit_2(self):
        result = self.run_report(self.fixture())
        self.assert_usage_refused(result)

    def test_blank_repo_refuses_with_usage_and_exit_2(self):
        result = self.run_report(self.fixture(), '   ')
        self.assert_usage_refused(result)

    def test_extra_positionals_refuse_with_usage_and_exit_2(self):
        result = self.run_report(self.fixture(), REPO, 'extra')
        self.assert_usage_refused(result)

    def test_report_help_exits_zero_with_usage(self):
        result = self.run_report(self.fixture(), '--help')
        self.assertEqual(result.returncode, 0, self.combined(result))
        combined = self.combined(result).lower()
        for token in ('report', '--top', '--min-observations', '--json'):
            self.assertIn(token, combined)

    def test_dry_run_is_a_noop_alias_for_text_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, 'ledger.jsonl')
            ledger.write_text(self.fixture(), encoding='utf-8')
            before = ledger.read_bytes()
            env = dict(os.environ, FLAKE_LEDGER_PATH=str(ledger))
            plain = subprocess.run(
                [sys.executable, str(SCRIPT), 'report', REPO],
                capture_output=True, text=True, env=env, check=False)
            flagged = subprocess.run(
                [sys.executable, str(SCRIPT), 'report', REPO, '--dry-run'],
                capture_output=True, text=True, env=env, check=False)
            self.assertEqual(ledger.read_bytes(), before,
                             'report must never modify the ledger')
        self.assertEqual(plain.returncode, 0, self.combined(plain))
        self.assertEqual(flagged.returncode, 0, self.combined(flagged))
        self.assertEqual(flagged.stdout, plain.stdout)


if __name__ == '__main__':
    unittest.main()
