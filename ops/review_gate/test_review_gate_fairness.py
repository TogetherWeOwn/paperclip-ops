#!/usr/bin/env python3
"""Sanitized source-acquisition artifact; no installation or runtime configuration included."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_gate as rg

def heads(repo, numbers):
    full = f'example-org/{repo}'
    return [(f'{full}#{n}@h{n}', f'https://x/{repo}/{n}', f'h{n}', full, n) for n in numbers]

def prep(*groups):
    cands = [c for g in groups for c in g]
    due = [(k, u, h) for k, u, h, _, _ in cands]
    info = {k: (f, n) for k, _, _, f, n in cands}
    return (due, info)

class FairOrderTest(unittest.TestCase):

    def names(self, order, info):
        return [(info[d[0]][0].split('/')[1], info[d[0]][1]) for d in order]

    def test_second_same_rank_repo_is_not_starved_by_batch_cut(self):
        due, info = prep(heads('primary-service-a', range(1, 31)), heads('primary-service-b', range(101, 153)))
        top = self.names(rg.fair_order(due, info, set())[:15], info)
        self.assertIn('primary-service-b', {r for r, _ in top})
        self.assertIn('primary-service-a', {r for r, _ in top})
        self.assertEqual(sum((1 for r, _ in top if r == 'primary-service-b')), 7)

    def test_order_within_each_repo_is_unchanged(self):
        due, info = prep(heads('primary-service-a', [5, 3, 9]), heads('primary-service-b', [8, 2]))
        got = self.names(rg.fair_order(due, info, set()), info)
        self.assertEqual([n for r, n in got if r == 'primary-service-a'], [5, 3, 9])
        self.assertEqual([n for r, n in got if r == 'primary-service-b'], [8, 2])

    def test_rank_order_still_wins_and_pinned_stay_first(self):
        prio = {('example-org/last-service-api', 7)}
        due, info = prep(heads('last-service-api', [7, 8]), heads('tertiary-service', [1]), heads('secondary-api', [1]), heads('primary-service-a', [1, 2]), heads('primary-service-b', [1]))
        got = self.names(rg.fair_order(due, info, prio), info)
        self.assertEqual(got[0], ('last-service-api', 7))
        self.assertEqual([r for r, _ in got[1:]], ['primary-service-a', 'primary-service-b', 'primary-service-a', 'secondary-api', 'tertiary-service', 'last-service-api'])

    def test_single_repo_and_empty_are_unchanged(self):
        due, info = prep(heads('primary-service-b', [4, 2, 6]))
        self.assertEqual(rg.fair_order(due, info, set()), due)
        self.assertEqual(rg.fair_order([], {}, set()), [])

    def test_no_head_dropped_or_duplicated(self):
        due, info = prep(heads('primary-service-a', range(1, 8)), heads('primary-service-b', range(1, 4)), heads('secondary', [1]))
        out = rg.fair_order(due, info, set())
        self.assertEqual(sorted((d[0] for d in out)), sorted((d[0] for d in due)))
if __name__ == '__main__':
    unittest.main()
