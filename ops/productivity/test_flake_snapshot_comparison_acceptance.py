"""Acceptance tests for comparing two supplied flake-ledger snapshots.

Paired build contract. This file is the immutable test artifact: it pins the
subprocess-visible behavior of a new read-only subcommand on the EXISTING
flake-ledger CLI::

    flake_ledger.py compare <repo> --before FILE --after FILE [--limit N] [--dry-run]

Inputs and identity
-------------------
* ``--before`` and ``--after`` are both mandatory explicit local files.
  Compare never falls back to ``HOME``, ``FLAKE_LEDGER_PATH``, live
  collection, or the ledger writer. The same path may be supplied twice.
* Both snapshots are read with the existing ledger acceptance rules
  (JSONL, a JSON array/object, or the ``{"tests": [...]}`` envelope) and
  every row in each snapshot must be well-formed, including other repos'
  rows. Within the requested repo, duplicate ``(job, test)`` pairs and a
  value identifying one row as a test and a different row as a job are
  ambiguous and refuse; a single row whose own test equals its own job is
  not a collision. Ambiguous rows are never combined.
* Each snapshot must contain at least one row for the exact requested
  repo. A repo absent from either snapshot is unavailable evidence, never
  a verified zero and never a set of added/missing rows.
* Row identity is the exact ``(repo, job, test)`` tuple. ``id`` reuses the
  existing report escaping: per component, ``%`` -> ``%25`` first, then
  ``/`` -> ``%2F``, joined with ``/``. Raw job/test values are preserved.

Default output (exactly one JSON object on stdout)
--------------------------------------------------
Top-level keys are exactly::

    schema, status, code, retryable, next_action, repo, limit,
    window_verified, continuity_verified, sources, total, returned,
    truncated, category_counts, rows

* ``schema`` is ``flake-snapshot-comparison.v1``; there is no timestamp,
  so identical inputs give byte-identical stdout across runs.
* ``window_verified`` and ``continuity_verified`` are always ``false``:
  even monotone counters do not prove the same cohort or window, an
  interval rate, causation, or a live trend.
* ``sources`` is exactly
  ``{"before": {"sha256": HEX}, "after": {"sha256": HEX}}`` where HEX is
  the SHA-256 of the exact supplied file bytes. No file paths appear
  anywhere in stdout or stderr.
* ``rows`` is the union of requested-repo tuples from both snapshots,
  sorted globally by ``(job, test)`` ascending regardless of category.
  ``--limit`` defaults to 100 and accepts integers 1-1000 only; it
  truncates that sorted list. ``total`` is the full union size,
  ``returned`` is ``len(rows)``, ``truncated`` is ``total - returned``,
  and ``category_counts`` has exactly the keys ``compared``,
  ``reset_suspected``, ``added_evidence`` and ``missing_evidence``,
  counted over the full set BEFORE truncation. Status is likewise
  computed before truncation.
* Each row has exactly the keys ``id``, ``job``, ``test``, ``category``,
  ``before``, ``after`` and ``change``. A present side is
  ``{"flips": int, "observations": int, "rate": float|null}`` where rate
  is the cumulative flips/observations, or null for zero observations.
  An absent side is null. ``change`` is always
  ``{"flips": int|null, "observations": int|null, "rate": float|null}``.
  No other input fields are projected into a row.

Categories and states
---------------------
* ``compared``: present in both snapshots and neither counter decreased.
  ``change`` carries the non-negative descriptive counter differences;
  ``change.rate`` is after-rate minus before-rate when both rates exist,
  else null.
* ``reset_suspected``: present in both, but flips or observations
  decreased. It is incomparable: every ``change`` value is null, never a
  negative count or an improvement claim.
* ``added_evidence`` / ``missing_evidence``: present only in the after /
  before snapshot. The absent side and every ``change`` value are null.
  These never mean a test was newly introduced or fixed.
* ``status``/``code``/exit: ``complete``/``compared``/0 when no row is
  ``reset_suspected`` (added/missing and null rates are fully described,
  not partial). ``partial``/``partial_reset_suspected``/1 when at least
  one row is ``reset_suspected``. ``retryable`` is false and
  ``next_action`` is a non-empty string in every JSON state; the partial
  next action names the suspected reset.

Refusals
--------
* Evidence/input refusals after argument parsing (missing, unreadable,
  empty, malformed, corrupt, duplicate or ambiguous snapshot, or no
  evidence for the repo in either snapshot) exit 2 and print one JSON
  object on stdout with the same keys as above EXCEPT ``sources`` is
  omitted; ``status`` is ``unavailable``, ``code`` is ``unavailable``,
  ``retryable`` is false, ``next_action`` tells the caller to supply or
  repair snapshot evidence, ``rows`` is empty, and all counts are zero.
  New errors never echo raw rows, extraneous input fields, parser
  exception bodies, or absolute paths.
* Usage errors (missing/blank repo or file value, extra positional,
  missing/duplicate ``--before``/``--after``/``--limit``, an invalid
  limit, or any flag other than ``--before``, ``--after``, ``--limit``,
  ``--dry-run``, ``-h``/``--help``) exit 2, print usage mentioning
  ``compare`` on stderr, and print nothing on stdout. Space and ``=``
  value forms are both accepted, and flags may surround the repo.
* ``compare --help`` and top-level ``--help`` exit 0 and document the
  compare command with ``--before``/``--after`` and ``--limit``.
* ``--dry-run`` is a documented no-op alias: byte-identical output.

Read-only proof
---------------
Compare never writes its inputs, sidecars, ledger state, cards or the
environment, and never invokes the writer, reruns, quarantine, HTTP, host
hooks or production commands - including for invalid flag combinations.
The harness below snapshots every file under the temp tree (inputs, an
``FLAKE_LEDGER_PATH`` decoy, and a ``HOME`` default-ledger decoy) and
fails if any byte changes or any file appears/disappears, so every
invocation in this suite proves read-onlyness and no fallback lookup.

Existing ``is_flaky``, ``report`` and ``record_flake`` behavior,
interfaces and thresholds are unchanged; the controls at the end pin the
current behavior and pass before the feature exists. Documentation in
``docs/flake-ledger.md`` must describe this comparison (schema, explicit
files, ``--limit``, the four categories, both ``*_verified`` flags, and
exit 1 partial / exit 2 refusal) as part of the same build.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('flake_ledger.py')
DOCS = Path(__file__).parents[2] / 'docs' / 'flake-ledger.md'
REPO = 'synthetic-repo'
OTHER_REPO = 'other-synthetic-repo'
SCHEMA = 'flake-snapshot-comparison.v1'
SECRET = 'DO_NOT_DISCLOSE_SYNTHETIC_MARKER'
DECOY_TEST = 'decoy_only_never_compare_this_test'
CATEGORIES = ('compared', 'reset_suspected', 'added_evidence', 'missing_evidence')
TOP_KEYS = {
    'schema', 'status', 'code', 'retryable', 'next_action', 'repo', 'limit',
    'window_verified', 'continuity_verified', 'sources', 'total', 'returned',
    'truncated', 'category_counts', 'rows',
}
ROW_KEYS = {'id', 'job', 'test', 'category', 'before', 'after', 'change'}
SIDE_KEYS = {'flips', 'observations', 'rate'}


def ledger_row(*, repo=REPO, job='ci job', test='sample_test', flips=0,
               observations=10, **extra):
    row = {'repo': repo, 'job': job, 'test': test, 'flips': flips,
           'observations': observations}
    row.update(extra)
    return row


def jsonl_content(*rows):
    return ''.join(json.dumps(row) + '\n' for row in rows)


def envelope_content(*rows):
    return json.dumps({'tests': list(rows)})


def expected_id(repo, job, test):
    return '/'.join(value.replace('%', '%25').replace('/', '%2F')
                    for value in (repo, job, test))


def sha256_hex(content):
    data = content if isinstance(content, bytes) else content.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


class CompareCase(unittest.TestCase):
    """Subprocess harness: every compare run proves read-only + no fallback."""

    @staticmethod
    def _tree(root):
        return {str(path.relative_to(root)): path.read_bytes()
                for path in sorted(root.rglob('*')) if path.is_file()}

    def run_compare(self, before_content, after_content, *extra_args,
                    repo=REPO, argv_factory=None, omit_home=False,
                    cwd_tmp=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = {'before': root / 'before.jsonl', 'after': root / 'after.jsonl'}
        for key, content in (('before', before_content),
                             ('after', after_content)):
            if content is not None:
                data = (content if isinstance(content, bytes)
                        else content.encode('utf-8'))
                paths[key].write_bytes(data)
        decoy = ledger_row(repo=REPO, test=DECOY_TEST, flips=9, observations=10)
        decoy_path = root / 'decoy-ledger.jsonl'
        decoy_path.write_text(jsonl_content(decoy), encoding='utf-8')
        home = root / 'home'
        default_ledger = home / '.local' / 'share' / 'flake-ledger.json'
        default_ledger.parent.mkdir(parents=True)
        default_ledger.write_text(jsonl_content(decoy), encoding='utf-8')

        tree_before = self._tree(root)
        env = dict(os.environ, FLAKE_LEDGER_PATH=str(decoy_path))
        if omit_home:
            env.pop('HOME', None)
        else:
            env['HOME'] = str(home)
        if argv_factory is not None:
            argv = argv_factory(paths)
        else:
            argv = ['compare', repo, '--before', str(paths['before']),
                    '--after', str(paths['after']), *extra_args]
        result = subprocess.run(
            [sys.executable, '-B', str(SCRIPT), *argv],
            capture_output=True, text=True, env=env,
            cwd=str(root) if cwd_tmp else None, check=False, timeout=15)
        self.assertEqual(self._tree(root), tree_before,
                         'compare must never create, delete or modify files')
        combined = result.stdout + result.stderr
        self.assertNotIn(str(root), combined,
                         'compare output must never carry absolute paths')
        self.assertNotIn(DECOY_TEST, combined,
                         'compare must use only the two explicit snapshots')
        self.assertNotIn(SECRET, combined,
                         'compare must never disclose extraneous input data')
        return result

    @staticmethod
    def combined(result):
        return result.stdout + result.stderr

    def parse_doc(self, result):
        try:
            doc = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail('compare stdout must be exactly one JSON object:\n'
                      f'stdout={result.stdout!r}\nstderr={result.stderr!r}\n'
                      f'exit={result.returncode}')
        self.assertIsInstance(doc, dict, 'compare stdout must be a JSON object')
        return doc

    def assert_common_state(self, doc, *, status, code, repo=REPO, limit=100):
        self.assertEqual(doc.get('schema'), SCHEMA)
        self.assertEqual(doc.get('status'), status)
        self.assertEqual(doc.get('code'), code)
        self.assertIs(doc.get('retryable'), False)
        self.assertIsInstance(doc.get('next_action'), str)
        self.assertTrue(doc['next_action'].strip(),
                        'every state must carry a next action')
        self.assertEqual(doc.get('repo'), repo)
        self.assertIs(type(doc.get('limit')), int)
        self.assertEqual(doc['limit'], limit)
        self.assertIs(doc.get('window_verified'), False)
        self.assertIs(doc.get('continuity_verified'), False)

    def assert_counts(self, doc, *, total, returned, **categories):
        self.assertIs(type(doc.get('total')), int)
        self.assertIs(type(doc.get('returned')), int)
        self.assertIs(type(doc.get('truncated')), int)
        self.assertEqual((doc['total'], doc['returned'], doc['truncated']),
                         (total, returned, total - returned))
        self.assertEqual(doc.get('category_counts'),
                         {name: categories.get(name, 0) for name in CATEGORIES})
        self.assertEqual(len(doc['rows']), returned)

    def assert_row_shapes(self, doc):
        seen_ids = set()
        for row in doc['rows']:
            self.assertEqual(set(row), ROW_KEYS,
                             'rows must project exactly the contract fields')
            self.assertEqual(row['id'],
                             expected_id(doc['repo'], row['job'], row['test']))
            self.assertNotIn(row['id'], seen_ids, 'row IDs must be unique')
            seen_ids.add(row['id'])
            self.assertIn(row['category'], CATEGORIES)
            for side in ('before', 'after'):
                value = row[side]
                if value is not None:
                    self.assertEqual(set(value), SIDE_KEYS)
                    self.assertIs(type(value['flips']), int)
                    self.assertIs(type(value['observations']), int)
                    if value['observations'] == 0:
                        self.assertIsNone(value['rate'])
                    else:
                        self.assertAlmostEqual(
                            value['rate'],
                            value['flips'] / value['observations'])
            self.assertEqual(set(row['change']), SIDE_KEYS)
        order = [(row['job'], row['test']) for row in doc['rows']]
        self.assertEqual(order, sorted(order),
                         'rows must use exact (job, test) ascending order')

    def assert_complete(self, result, *, repo=REPO, limit=100):
        self.assertEqual(result.returncode, 0, self.combined(result))
        doc = self.parse_doc(result)
        self.assertEqual(set(doc), TOP_KEYS)
        self.assert_common_state(doc, status='complete', code='compared',
                                 repo=repo, limit=limit)
        self.assert_row_shapes(doc)
        return doc

    def assert_partial(self, result, *, repo=REPO, limit=100):
        self.assertEqual(result.returncode, 1,
                         'reset-suspected evidence must be partial, exit 1:\n'
                         + self.combined(result))
        doc = self.parse_doc(result)
        self.assertEqual(set(doc), TOP_KEYS)
        self.assert_common_state(doc, status='partial',
                                 code='partial_reset_suspected',
                                 repo=repo, limit=limit)
        self.assertIn('reset', doc['next_action'].lower(),
                      'the partial next action must name the suspected reset')
        self.assert_row_shapes(doc)
        return doc

    def assert_unavailable(self, result, *, repo=REPO, limit=100):
        self.assertEqual(result.returncode, 2,
                         'input/evidence refusal must exit 2:\n'
                         + self.combined(result))
        doc = self.parse_doc(result)
        self.assertEqual(set(doc), TOP_KEYS - {'sources'})
        self.assert_common_state(doc, status='unavailable', code='unavailable',
                                 repo=repo, limit=limit)
        next_action = doc['next_action'].lower()
        self.assertTrue(any(word in next_action
                            for word in ('snapshot', 'evidence', 'ledger')),
                        'unavailable next action must point at the evidence')
        self.assert_counts(doc, total=0, returned=0)
        self.assertEqual(doc['rows'], [])
        return doc

    def assert_usage(self, result, *tokens):
        self.assertEqual(result.returncode, 2,
                         'usage errors must exit 2:\n' + self.combined(result))
        self.assertEqual(result.stdout, '',
                         'usage errors must not pollute stdout')
        lowered = result.stderr.lower()
        self.assertIn('usage', lowered)
        self.assertIn('compare', lowered)
        for token in tokens:
            self.assertIn(token.lower(), lowered)

    @staticmethod
    def rows_by_test(doc):
        return {row['test']: row for row in doc['rows']}

    def assert_sources(self, doc, before_content, after_content):
        self.assertEqual(doc.get('sources'), {
            'before': {'sha256': sha256_hex(before_content)},
            'after': {'sha256': sha256_hex(after_content)},
        })


def simple_pair(before_spec, after_spec):
    """Build (before, after) JSONL from {test: (flips, observations)} maps."""
    def content(spec):
        return jsonl_content(*(
            ledger_row(job=f'{test} job', test=test, flips=flips,
                       observations=observations)
            for test, (flips, observations) in spec.items()))
    return content(before_spec), content(after_spec)


class CompareCore(CompareCase):
    def test_default_json_has_exact_deltas_rates_and_sources(self):
        before = jsonl_content(
            ledger_row(job='d job', test='zero_start_test', flips=0,
                       observations=0),
            ledger_row(job='b job', test='increased_test', flips=1,
                       observations=10),
            ledger_row(job='a job', test='unchanged_test', flips=2,
                       observations=10, note=SECRET),
            ledger_row(job='c job', test='rate_down_test', flips=2,
                       observations=10),
            ledger_row(repo=OTHER_REPO, test='other_repo_test', flips=9,
                       observations=10),
        )
        after = jsonl_content(
            ledger_row(job='c job', test='rate_down_test', flips=2,
                       observations=20),
            ledger_row(job='a job', test='unchanged_test', flips=2,
                       observations=10),
            ledger_row(job='d job', test='zero_start_test', flips=1,
                       observations=5),
            ledger_row(job='b job', test='increased_test', flips=4,
                       observations=20),
            ledger_row(repo=OTHER_REPO, test='other_repo_test', flips=0,
                       observations=10),
        )
        result = self.run_compare(before, after)
        self.assertTrue(result.stdout.strip().startswith('{'))
        doc = self.assert_complete(result)
        self.assert_sources(doc, before, after)
        self.assert_counts(doc, total=4, returned=4, compared=4)
        self.assertEqual([row['test'] for row in doc['rows']],
                         ['unchanged_test', 'increased_test',
                          'rate_down_test', 'zero_start_test'])
        self.assertNotIn('other_repo_test', result.stdout)
        rows = self.rows_by_test(doc)

        unchanged = rows['unchanged_test']
        self.assertEqual(unchanged['before'],
                         {'flips': 2, 'observations': 10, 'rate': 0.2})
        self.assertEqual(unchanged['after'], unchanged['before'])
        self.assertEqual(unchanged['change'],
                         {'flips': 0, 'observations': 0, 'rate': 0.0})

        increased = rows['increased_test']
        self.assertEqual((increased['before']['flips'],
                          increased['before']['observations']), (1, 10))
        self.assertAlmostEqual(increased['before']['rate'], 0.1)
        self.assertEqual((increased['after']['flips'],
                          increased['after']['observations']), (4, 20))
        self.assertAlmostEqual(increased['after']['rate'], 0.2)
        self.assertEqual((increased['change']['flips'],
                          increased['change']['observations']), (3, 10))
        self.assertAlmostEqual(increased['change']['rate'], 0.1)

        rate_down = rows['rate_down_test']
        self.assertEqual((rate_down['change']['flips'],
                          rate_down['change']['observations']), (0, 10))
        self.assertAlmostEqual(rate_down['change']['rate'], -0.1)

        zero = rows['zero_start_test']
        self.assertIsNone(zero['before']['rate'],
                          'zero observations must give a null rate')
        self.assertAlmostEqual(zero['after']['rate'], 0.2)
        self.assertEqual((zero['change']['flips'],
                          zero['change']['observations']), (1, 5))
        self.assertIsNone(zero['change']['rate'],
                          'a rate difference needs both cumulative rates')

    def test_both_zero_counters_are_compared_with_null_rates(self):
        before, after = simple_pair({'idle_test': (0, 0)}, {'idle_test': (0, 0)})
        doc = self.assert_complete(self.run_compare(before, after))
        row = doc['rows'][0]
        self.assertEqual(row['category'], 'compared')
        self.assertIsNone(row['before']['rate'])
        self.assertIsNone(row['after']['rate'])
        self.assertEqual(row['change'],
                         {'flips': 0, 'observations': 0, 'rate': None})

    def test_envelope_and_array_formats_compare_with_byte_digests(self):
        rows = [
            ledger_row(job='b job', test='second_test', flips=1, observations=10),
            ledger_row(job='a job', test='first_test', flips=2, observations=10),
        ]
        before = envelope_content(*rows)
        after = json.dumps(list(reversed(rows)))
        doc = self.assert_complete(self.run_compare(before, after))
        self.assert_sources(doc, before, after)
        self.assertNotEqual(doc['sources']['before']['sha256'],
                            doc['sources']['after']['sha256'],
                            'digests bind to exact supplied bytes')
        self.assert_counts(doc, total=2, returned=2, compared=2)
        for row in doc['rows']:
            self.assertEqual(row['change']['flips'], 0)
            self.assertEqual(row['change']['observations'], 0)

    def test_same_file_may_be_supplied_for_both_snapshots(self):
        before, ignored_after = simple_pair(
            {'stable_test': (2, 10)}, {'ignored_other_test': (9, 10)})
        result = self.run_compare(
            before, ignored_after,
            argv_factory=lambda p: ['compare', REPO, '--before', str(p['before']),
                                    '--after', str(p['before'])])
        doc = self.assert_complete(result)
        self.assert_sources(doc, before, before)
        self.assertNotIn('ignored_other_test', result.stdout)

    def test_equals_forms_flag_order_and_relative_paths(self):
        before, after = simple_pair({'flag_test': (1, 10)}, {'flag_test': (2, 10)})
        result = self.run_compare(
            before, after, cwd_tmp=True,
            argv_factory=lambda p: ['compare', '--after=after.jsonl',
                                    '--limit=10', REPO,
                                    '--before=before.jsonl'])
        doc = self.assert_complete(result, limit=10)
        self.assertEqual(doc['rows'][0]['test'], 'flag_test')

    def test_output_is_byte_deterministic_and_dry_run_is_identical(self):
        before, after = simple_pair({'steady_test': (1, 10)},
                                    {'steady_test': (3, 20)})
        first = self.run_compare(before, after)
        second = self.run_compare(before, after)
        flagged = self.run_compare(before, after, '--dry-run')
        self.assert_complete(first)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(flagged.stdout, first.stdout)
        self.assertEqual(flagged.returncode, first.returncode)

    def test_escaping_preserves_percent_and_slash_tuple_boundaries(self):
        repo = 'synthetic/repo%name'
        specs = [
            ('a', 'b/c', (1, 10), (2, 10)),
            ('a/b', 'c', (3, 10), (3, 10)),
            ('a%2Fb', 'percent%test', (0, 0), (0, 10)),
            ('nightly', 'nightly', (2, 10), (2, 10)),
        ]
        before = jsonl_content(*(
            ledger_row(repo=repo, job=job, test=test, flips=before_pair[0],
                       observations=before_pair[1])
            for job, test, before_pair, _ in specs))
        after = jsonl_content(*(
            ledger_row(repo=repo, job=job, test=test, flips=after_pair[0],
                       observations=after_pair[1])
            for job, test, _, after_pair in specs))
        doc = self.assert_complete(self.run_compare(before, after, repo=repo),
                                   repo=repo)
        self.assert_counts(doc, total=4, returned=4, compared=4)
        self.assertEqual([(row['job'], row['test']) for row in doc['rows']],
                         sorted((job, test) for job, test, _, _ in specs))
        self.assertEqual(len({row['id'] for row in doc['rows']}), 4)


class CompareCategories(CompareCase):
    def test_added_and_missing_evidence_have_null_sides_and_deltas(self):
        before, after = simple_pair(
            {'anchor_test': (1, 10), 'gone_test': (2, 10)},
            {'anchor_test': (2, 20), 'seen_later_test': (1, 5)})
        doc = self.assert_complete(self.run_compare(before, after))
        self.assert_counts(doc, total=3, returned=3, compared=1,
                           added_evidence=1, missing_evidence=1)
        rows = self.rows_by_test(doc)
        self.assertEqual(rows['anchor_test']['category'], 'compared')
        added = rows['seen_later_test']
        self.assertEqual(added['category'], 'added_evidence')
        self.assertIsNone(added['before'])
        self.assertEqual((added['after']['flips'],
                          added['after']['observations']), (1, 5))
        self.assertEqual(added['change'],
                         {'flips': None, 'observations': None, 'rate': None})
        missing = rows['gone_test']
        self.assertEqual(missing['category'], 'missing_evidence')
        self.assertIsNone(missing['after'])
        self.assertEqual((missing['before']['flips'],
                          missing['before']['observations']), (2, 10))
        self.assertEqual(missing['change'],
                         {'flips': None, 'observations': None, 'rate': None})

    def test_disjoint_valid_snapshots_are_fully_described_not_partial(self):
        before, after = simple_pair({'only_before_test': (1, 10)},
                                    {'only_after_test': (1, 10)})
        doc = self.assert_complete(self.run_compare(before, after))
        self.assert_counts(doc, total=2, returned=2, added_evidence=1,
                           missing_evidence=1)

    def test_decreased_flips_are_reset_suspected_and_partial(self):
        before, after = simple_pair(
            {'reset_test': (5, 10), 'steady_test': (1, 10)},
            {'reset_test': (4, 10), 'steady_test': (2, 10)})
        doc = self.assert_partial(self.run_compare(before, after))
        self.assert_counts(doc, total=2, returned=2, compared=1,
                           reset_suspected=1)
        reset = self.rows_by_test(doc)['reset_test']
        self.assertEqual(reset['category'], 'reset_suspected')
        self.assertAlmostEqual(reset['before']['rate'], 0.5)
        self.assertAlmostEqual(reset['after']['rate'], 0.4)
        self.assertEqual(reset['change'],
                         {'flips': None, 'observations': None, 'rate': None},
                         'a decrease is never a negative or improving delta')

    def test_decreased_observations_alone_are_reset_suspected(self):
        before, after = simple_pair({'shrunk_test': (2, 10)},
                                    {'shrunk_test': (2, 9)})
        doc = self.assert_partial(self.run_compare(before, after))
        self.assert_counts(doc, total=1, returned=1, reset_suspected=1)
        self.assertEqual(doc['rows'][0]['change'],
                         {'flips': None, 'observations': None, 'rate': None})

    def test_decreased_flips_with_more_observations_are_reset_suspected(self):
        before, after = simple_pair({'mixed_test': (5, 10)},
                                    {'mixed_test': (4, 20)})
        doc = self.assert_partial(self.run_compare(before, after))
        self.assertEqual(doc['rows'][0]['category'], 'reset_suspected')


class CompareLimitAndOrdering(CompareCase):
    def test_rows_sort_globally_by_tuple_across_categories(self):
        before = jsonl_content(
            ledger_row(job='m job', test='shared_test', flips=5,
                       observations=10),
            ledger_row(job='z job', test='before_only_test', flips=1,
                       observations=10),
        )
        after = jsonl_content(
            ledger_row(job='a job', test='after_only_test', flips=1,
                       observations=10),
            ledger_row(job='m job', test='shared_test', flips=4,
                       observations=10),
        )
        doc = self.assert_partial(self.run_compare(before, after))
        self.assertEqual([(row['job'], row['category']) for row in doc['rows']],
                         [('a job', 'added_evidence'),
                          ('m job', 'reset_suspected'),
                          ('z job', 'missing_evidence')])

    def test_limit_truncates_after_sorting_but_counts_and_status_are_full_set(self):
        before, after = simple_pair(
            {'a_test': (1, 10), 'b_test': (1, 10), 'c_test': (5, 10)},
            {'a_test': (2, 10), 'b_test': (2, 10), 'c_test': (4, 10)})
        doc = self.assert_partial(
            self.run_compare(before, after, '--limit', '2'), limit=2)
        self.assert_counts(doc, total=3, returned=2, compared=2,
                           reset_suspected=1)
        self.assertEqual([row['test'] for row in doc['rows']],
                         ['a_test', 'b_test'],
                         'the truncated reset row still forces partial status')

    def test_default_limit_is_one_hundred(self):
        rows_before = [
            ledger_row(job=f'job-{index:03d}', test=f'test-{index:03d}',
                       flips=1, observations=10)
            for index in range(101)
        ]
        before = jsonl_content(*rows_before)
        after = jsonl_content(*reversed(rows_before))
        doc = self.assert_complete(self.run_compare(before, after))
        self.assert_counts(doc, total=101, returned=100, compared=101)
        self.assertEqual(doc['rows'][0]['test'], 'test-000')
        self.assertEqual(doc['rows'][-1]['test'], 'test-099')

    def test_limit_bounds_one_and_one_thousand_are_accepted(self):
        before, after = simple_pair({'one_test': (1, 10), 'two_test': (1, 10)},
                                    {'one_test': (1, 10), 'two_test': (1, 10)})
        one = self.assert_complete(
            self.run_compare(before, after, '--limit', '1'), limit=1)
        self.assert_counts(one, total=2, returned=1, compared=2)
        thousand = self.assert_complete(
            self.run_compare(before, after, '--limit=1000'), limit=1000)
        self.assert_counts(thousand, total=2, returned=2, compared=2)


class CompareValidity(CompareCase):
    def valid(self):
        return jsonl_content(ledger_row(test='valid_test', flips=1,
                                        observations=10))

    def test_missing_repo_in_either_snapshot_is_unavailable_not_zero(self):
        other_only = jsonl_content(
            ledger_row(repo=OTHER_REPO, test='theirs_test', flips=1,
                       observations=10))
        for before, after in ((other_only, self.valid()),
                              (self.valid(), other_only),
                              (other_only, other_only)):
            with self.subTest(before=before, after=after):
                self.assert_unavailable(self.run_compare(before, after))

    def test_repo_match_is_exact_and_case_sensitive(self):
        self.assert_unavailable(
            self.run_compare(self.valid(), self.valid(), repo=REPO.upper()),
            repo=REPO.upper())

    def test_missing_empty_and_directory_snapshots_are_unavailable(self):
        self.assert_unavailable(self.run_compare(None, self.valid()))
        self.assert_unavailable(self.run_compare(self.valid(), None))
        for content in ('', '  \n\t\n', '[]\n', '{"tests": []}\n'):
            with self.subTest(content=repr(content)):
                self.assert_unavailable(
                    self.run_compare(content, self.valid()))
        self.assert_unavailable(self.run_compare(
            self.valid(), self.valid(),
            argv_factory=lambda p: ['compare', REPO, '--before',
                                    str(p['before'].parent), '--after',
                                    str(p['after'])]))

    def test_malformed_rows_in_either_snapshot_are_unavailable(self):
        good = ledger_row(test='good_test', flips=1, observations=10)
        malformed_contents = [
            '{not-json}\n',
            b'\xff\xfe\x00 binary not utf-8\n',
            jsonl_content(ledger_row(flips=11, observations=10)),
            jsonl_content(ledger_row(flips=-1, observations=10)),
            jsonl_content(ledger_row(flips='2', observations=10)),
            jsonl_content(ledger_row(flips=True, observations=10)),
            jsonl_content(ledger_row(test='   ')),
            jsonl_content(good, 'just a string'),
            jsonl_content(good, {'repo': OTHER_REPO, 'job': 'other job'}),
            json.dumps({'tests': {'not': 'an array'}}),
        ]
        for bad in malformed_contents:
            with self.subTest(bad=repr(bad)[:60], side='before'):
                self.assert_unavailable(self.run_compare(bad, self.valid()))
            with self.subTest(bad=repr(bad)[:60], side='after'):
                self.assert_unavailable(self.run_compare(self.valid(), bad))

    def test_duplicate_tuples_are_unavailable_never_combined(self):
        row = ledger_row(test='duplicated_test', flips=1, observations=10,
                         note=SECRET)
        duplicate = jsonl_content(row, ledger_row(
            test='duplicated_test', flips=0, observations=10, note=SECRET))
        self.assert_unavailable(self.run_compare(duplicate, self.valid()))
        self.assert_unavailable(self.run_compare(self.valid(), duplicate))

    def test_test_job_collision_across_rows_is_unavailable(self):
        ambiguous = jsonl_content(
            ledger_row(job='other', test='nightly', flips=5, observations=10),
            ledger_row(job='nightly', test='job nightly', flips=0,
                       observations=10),
        )
        self.assert_unavailable(self.run_compare(ambiguous, self.valid()))
        self.assert_unavailable(self.run_compare(self.valid(), ambiguous))

    def test_errors_do_not_echo_raw_rows_or_parser_bodies(self):
        marker_token = 'UNIQUE_PARSER_BODY_TOKEN'
        corrupt = ('{"repo": "%s", "job": "x", "%s": "%s"\n'
                   % (REPO, marker_token, SECRET))
        result = self.run_compare(corrupt, self.valid())
        self.assert_unavailable(result)
        self.assertNotIn(marker_token, self.combined(result))


class CompareCli(CompareCase):
    def fixture_pair(self):
        return simple_pair({'cli_test': (1, 10)}, {'cli_test': (2, 10)})

    def test_compare_and_top_level_help_exit_zero(self):
        before, after = self.fixture_pair()
        for argv in (['compare', '--help'], ['compare', '-h'], ['--help'], ['-h']):
            with self.subTest(argv=argv):
                result = self.run_compare(
                    before, after, argv_factory=lambda p, a=argv: list(a))
                self.assertEqual(result.returncode, 0, self.combined(result))
                combined = self.combined(result)
                for token in ('compare', '--before', '--after', '--limit'):
                    self.assertIn(token, combined)

    def test_usage_errors_refuse_and_never_fall_back(self):
        before, after = self.fixture_pair()
        cases = {
            'no_arguments': lambda p: ['compare'],
            'missing_before': lambda p: ['compare', REPO, '--after',
                                         str(p['after'])],
            'missing_after': lambda p: ['compare', REPO, '--before',
                                        str(p['before'])],
            'blank_repo': lambda p: ['compare', '   ', '--before',
                                     str(p['before']), '--after',
                                     str(p['after'])],
            'blank_before': lambda p: ['compare', REPO, '--before', '',
                                       '--after', str(p['after'])],
            'blank_after': lambda p: ['compare', REPO, '--before',
                                      str(p['before']), '--after', '  '],
            'extra_positional': lambda p: ['compare', REPO, 'extra',
                                           '--before', str(p['before']),
                                           '--after', str(p['after'])],
            'unknown_flag': lambda p: ['compare', REPO, '--before',
                                       str(p['before']), '--after',
                                       str(p['after']), '--bogus'],
            'report_top_flag': lambda p: ['compare', REPO, '--before',
                                          str(p['before']), '--after',
                                          str(p['after']), '--top', '3'],
            'json_flag': lambda p: ['compare', REPO, '--before',
                                    str(p['before']), '--after',
                                    str(p['after']), '--json'],
            'duplicate_before': lambda p: ['compare', REPO, '--before',
                                           str(p['before']), '--before',
                                           str(p['before']), '--after',
                                           str(p['after'])],
            'duplicate_after': lambda p: ['compare', REPO, '--before',
                                          str(p['before']), '--after',
                                          str(p['after']), '--after',
                                          str(p['after'])],
            'duplicate_limit': lambda p: ['compare', REPO, '--before',
                                          str(p['before']), '--after',
                                          str(p['after']), '--limit', '2',
                                          '--limit', '3'],
            'missing_before_value': lambda p: ['compare', REPO, '--after',
                                               str(p['after']), '--before'],
            'missing_limit_value': lambda p: ['compare', REPO, '--before',
                                              str(p['before']), '--after',
                                              str(p['after']), '--limit'],
        }
        for name, factory in cases.items():
            with self.subTest(case=name):
                self.assert_usage(self.run_compare(
                    before, after, argv_factory=factory))

    def test_invalid_limits_refuse_with_usage(self):
        before, after = self.fixture_pair()
        for bad in ('0', '-1', '1001', 'abc', '1.5', ''):
            with self.subTest(limit=bad):
                self.assert_usage(
                    self.run_compare(before, after, '--limit', bad), '--limit')

    def test_explicit_files_work_without_home_and_ignore_env_ledger(self):
        before, after = self.fixture_pair()
        result = self.run_compare(before, after, omit_home=True)
        doc = self.assert_complete(result)
        self.assert_sources(doc, before, after)
        self.assert_counts(doc, total=1, returned=1, compared=1)


class CompareDocs(CompareCase):
    def test_docs_describe_the_snapshot_comparison_contract(self):
        text = DOCS.read_text(encoding='utf-8')
        index = text.find(SCHEMA)
        self.assertNotEqual(index, -1,
                            'docs must name the comparison schema')
        section = text[max(0, index - 2000): index + 6000]
        for token in ('compare', '--before', '--after', '--limit',
                      'window_verified', 'continuity_verified',
                      'reset_suspected', 'added_evidence', 'missing_evidence',
                      'explicit', 'exit 1', 'exit 2'):
            self.assertIn(token, section,
                          f'docs comparison section must mention {token!r}')


class ExistingBehaviourControls(unittest.TestCase):
    """Controls for behavior that already exists; these pass pre-feature."""

    def run_with_ledger(self, content, *args):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, 'ledger.jsonl')
            if content is not None:
                ledger.write_text(content, encoding='utf-8')
            before = ledger.read_bytes() if ledger.exists() else None
            env = dict(os.environ, FLAKE_LEDGER_PATH=str(ledger))
            result = subprocess.run(
                [sys.executable, '-B', str(SCRIPT), *args],
                capture_output=True, text=True, env=env, check=False,
                timeout=15)
            if before is not None:
                self.assertEqual(ledger.read_bytes(), before)
            return result

    @staticmethod
    def row(**overrides):
        base = {'repo': 'control-repo', 'job': 'rust tests',
                'test': 'sample_test', 'flips': 0, 'observations': 5}
        base.update(overrides)
        return base

    def content(self, *rows):
        return ''.join(json.dumps(row) + '\n' for row in rows)

    def test_is_flaky_threshold_is_unchanged(self):
        at_threshold = self.run_with_ledger(
            self.content(self.row(flips=1, observations=5)),
            'is_flaky', 'control-repo', 'rust tests')
        self.assertEqual((at_threshold.returncode, at_threshold.stdout.strip()),
                         (0, 'no'))
        above = self.run_with_ledger(
            self.content(self.row(flips=2, observations=5)),
            'is_flaky', 'control-repo', 'rust tests')
        self.assertEqual((above.returncode, above.stdout.strip()), (0, 'yes'))

    def test_is_flaky_unavailable_is_never_a_verdict(self):
        ambiguous = self.content(self.row(test='first_test'),
                                 self.row(test='second_test'))
        result = self.run_with_ledger(ambiguous, 'is_flaky', 'control-repo',
                                      'rust tests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unavailable', result.stdout.lower())
        self.assertNotIn(result.stdout.strip(), ('yes', 'no'))

    def test_report_output_is_not_reformatted(self):
        content = self.content(self.row(flips=4, observations=5))
        text = self.run_with_ledger(content, 'report', 'control-repo')
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertIn('sample_test', text.stdout)
        self.assertIn('flips=4', text.stdout)
        self.assertIn('observations=5', text.stdout)
        envelope = json.loads(self.run_with_ledger(
            content, 'report', 'control-repo', '--json').stdout)
        self.assertEqual(envelope['schema_version'], 1)
        self.assertIsNone(envelope['error'])
        self.assertEqual(envelope['rows'][0]['test'], 'sample_test')

    def test_record_flake_still_increments_and_preserves_corrupt_files(self):
        spec = importlib.util.spec_from_file_location(
            'flake_ledger_control', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'ledger.jsonl')
            entry = {'repo': 'control-repo', 'job': 'rust tests',
                     'test': 'sample_test'}
            first = module.record_flake(path, entry)
            self.assertEqual((first['flips'], first['observations']), (1, 2))
            second = module.record_flake(path, entry)
            self.assertEqual((second['flips'], second['observations']), (2, 4))
            corrupt = Path(tmp, 'corrupt.jsonl')
            corrupt.write_text('{not-json}\n', encoding='utf-8')
            with self.assertRaises(module.LedgerWriteError):
                module.record_flake(corrupt, entry)
            self.assertEqual(corrupt.read_text(encoding='utf-8'),
                             '{not-json}\n')


if __name__ == '__main__':
    unittest.main()
