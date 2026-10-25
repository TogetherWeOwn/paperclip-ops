#!/usr/bin/env python3
"""Adversarial checks for the source-base binding on the own-patch delta record.

Independent of the Test Author acceptance suite (`test_review_rebase_carry_flow.py`):
this file adds no coverage that suite owns, and never modifies it. Hermetic, stdlib
unittest only, no network and no credentials.

Pins the CodeRabbit review finding on PR #115: a delta record is named by repository,
PR and head, so a record written for an earlier base stays reachable when the base
moves. `scoped_delta` therefore takes an optional `source_base` and refuses a record
computed against a different base, falling back to the compare-based file list.
"""
import json
import shutil
import tempfile
import unittest

import review_rebase_carry_flow as flow

REPO = 'TogetherWeOwn/paperclip-ops'
PR = 7
H1 = 'a' * 40
H2 = 'b' * 40


def paths():
    return flow.Paths(tempfile.mkdtemp(prefix='rebase-carry-basebind-'))


def write(paths, old_sha, files=('feature.py',), repo=REPO, pr=PR, head=H2, source=H1):
    flow.write_delta(paths, {'v': 1, 'mode': 'on', 'repo': repo.lower(), 'pr': pr,
                             'head': head, 'source_head': source,
                             'old_base': {'ref': 'main', 'sha': old_sha},
                             'new_base': {'ref': 'main', 'sha': 'd' * 40},
                             'files': list(files)})


class BaseBinding(unittest.TestCase):
    def setUp(self):
        self.paths = paths()
        self.addCleanup(shutil.rmtree, self.paths.delta_dir, True)

    def test_no_source_base_argument_keeps_the_record(self):
        """Omitting `source_base` is today's behavior: the record's own base stands."""
        write(self.paths, 'c' * 40)
        self.assertEqual(flow.scoped_delta(self.paths, 'on', REPO, PR, H1, H2, []),
                         ['feature.py'])

    def test_matching_source_base_keeps_the_record(self):
        write(self.paths, 'c' * 40)
        self.assertEqual(flow.scoped_delta(self.paths, 'on', REPO, PR, H1, H2, [],
                                           {'ref': 'main', 'sha': 'c' * 40}),
                         ['feature.py'])

    def test_moved_source_base_sha_refuses_the_record(self):
        write(self.paths, 'c' * 40)
        self.assertIsNone(flow.scoped_delta(self.paths, 'on', REPO, PR, H1, H2, [],
                                            {'ref': 'main', 'sha': '9' * 40}))

    def test_moved_source_base_ref_refuses_the_record(self):
        """Same SHA on a different branch name is a different base."""
        write(self.paths, 'c' * 40)
        self.assertIsNone(flow.scoped_delta(self.paths, 'on', REPO, PR, H1, H2, [],
                                            {'ref': 'dev', 'sha': 'c' * 40}))

    def test_malformed_source_base_fails_closed(self):
        write(self.paths, 'c' * 40)
        for bad in ({'ref': 'main'}, {'sha': 'c' * 40}, 'main', 7,
                    {'ref': '', 'sha': 'c' * 40}, {'ref': 'main', 'sha': 'short'}):
            with self.subTest(bad=bad):
                self.assertIsNone(flow.scoped_delta(self.paths, 'on', REPO, PR, H1, H2, [],
                                                    bad))

    def test_record_missing_old_base_fails_closed(self):
        """A record with no base cannot be bound, so it is refused when one is named."""
        flow.write_delta(self.paths, {'v': 1, 'mode': 'on', 'repo': REPO.lower(), 'pr': PR,
                                      'head': H2, 'source_head': H1,
                                      'new_base': {'ref': 'main', 'sha': 'd' * 40},
                                      'files': ['feature.py']})
        self.assertIsNone(flow.scoped_delta(self.paths, 'on', REPO, PR, H1, H2, [],
                                            {'ref': 'main', 'sha': 'c' * 40}))

    def test_source_head_mismatch_still_refuses(self):
        """The pre-existing source-head check is unchanged by the base binding."""
        write(self.paths, 'c' * 40)
        self.assertIsNone(flow.scoped_delta(self.paths, 'on', REPO, PR, 'e' * 40, H2, [],
                                            {'ref': 'main', 'sha': 'c' * 40}))

    def test_union_with_blockers_is_unchanged(self):
        write(self.paths, 'c' * 40, files=['feature.py'])
        self.assertEqual(flow.scoped_delta(self.paths, 'on', REPO, PR, H1, H2,
                                           [{'path': 'other.py'}],
                                           {'ref': 'main', 'sha': 'c' * 40}),
                         ['feature.py', 'other.py'])

    def test_shadow_mode_still_refuses(self):
        write(self.paths, 'c' * 40)
        self.assertIsNone(flow.scoped_delta(self.paths, 'shadow', REPO, PR, H1, H2, [],
                                            {'ref': 'main', 'sha': 'c' * 40}))


class RecordShape(unittest.TestCase):
    """The record still carries both bases so a caller can bind it."""

    def setUp(self):
        self.paths = paths()
        self.addCleanup(shutil.rmtree, self.paths.delta_dir, True)

    def test_record_stores_both_bases(self):
        write(self.paths, 'c' * 40)
        record = json.loads(next(self.paths.delta_dir.glob('*.json')).read_text())
        self.assertEqual(record['old_base'], {'ref': 'main', 'sha': 'c' * 40})
        self.assertEqual(record['new_base'], {'ref': 'main', 'sha': 'd' * 40})


if __name__ == '__main__':
    unittest.main()
