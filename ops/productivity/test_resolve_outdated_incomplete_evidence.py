#!/usr/bin/env python3
"""Immutable acceptance contract: resolve-outdated never resolves on incomplete evidence.

M12.2 acceptance for the paired build card.
IMMUTABLE: the Builder builds on this card's branch and must never edit this file.
If a case is wrong, corrections return to the Test Author card; the Builder does not fix them here.

What this pins (observable via run() only -- internal reader signatures are the
Builder's choice; never call open_prs()/review_evidence() directly from these tests):

  SEARCH PATH (open_prs feeds the tick):
    - Any search page with ``incomplete_results: true`` degrades the WHOLE tick:
      zero reply/resolve mutations anywhere in the tick (never resolve from the
      truncated set, not even PRs from complete prefix pages), at least one report
      row with ``skipped == 'incomplete_search'``, and no row with ``resolved``.
      No per-PR detail reads (graphql / check-runs) for PRs from the truncated
      listing. run() returns normally (exit-0 path, no exception).
    - Hitting the MAX_PRS page bound (300 PRs across full 100-item pages) degrades
      identically, even if a page-4 probe would come back empty: the BOUND governs,
      not the probe. The stub serves an empty page 4 so a limit+1 probe is allowed
      (3-4 search calls); resolving after the probe is not.
    - A failed search page (gh raises) degrades identically, never propagates.
    - Empty listing is a clean empty tick (report [], zero mutations).
    - Complete multi-page listings (short last page, no flag) still resolve.

  CHECK-RUNS PATH (per-head evidence feeds the 5/5 verdict):
    - The reader pages explicitly via ``page=N`` query params (this stub cannot
      emulate ``gh --paginate`` Link-following, so the Builder must page explicitly
      like open_prs already does) and reconciles collected runs vs ``total_count``.
    - Reconciled multi-page 5/5 (counts match, latest trusted on-head run is
      completed/success/5/5) resolves normally.
    - ``total_count`` mismatch, missing ``total_count`` (fail closed), failed page,
      or over-bound reads degrade THAT PR ONLY (tick continues for siblings) to a
      report row with ``skipped == 'incomplete_check_runs'`` -- never 5/5-by-omission
      -- with zero mutations for that PR.
    - Over-bound fixture: total_count=5000 (50 pages). The Builder picks any bound
      that paginates (>=2 check-runs calls, proving single-page reads are gone) and
      stops before draining (calls < 50). A 50-page-per-commit bound would be
      abusive; anything below it satisfies this contract.
    - Single-page reconciled 5/5 control resolves; total_count=0 pins the existing
      ``no_trusted_evidence`` reason unchanged.

  PRESERVED: --dry-run counts without mutating; PAUSE prints 'paused' with zero gh
    calls; ledger/ACTIONS_LOG gain no new shape (skips never log, as today -- only
    resolved non-dry rows log); exit-0 on evidence refusal (only token-helper
    failures exit non-zero, per the existing suite).

  NETWORK: every test replaces r.gh with an argv-level stub and rigs r.subprocess.run
    to raise, so any real subprocess/gh spawn fails the test. Mutation argv
    (``/replies``, ``resolveReviewThread``) is counted exactly.

Run this file:
    cd ops/productivity && python3 -m unittest -v test_resolve_outdated_incomplete_evidence
Regression control (must stay green before and after the build):
    cd ops/productivity && python3 -m unittest test_resolve_outdated_threads
Builder wiring (Builder's edit, not this card's): add this module to the unittest
lists in .github/workflows/ci.yml and scripts/verify-fast.sh.

RED baseline (2026-10-22, HEAD 92092294): 11 fail (missing refusal/pagination),
5 pass (empty/complete-search, single-page 5/5, total-0, PAUSE controls).
"""

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import resolve_outdated_threads as r

HEAD = 'a' * 40
HEAD_B = 'c' * 40
OTHER = 'b' * 40
TRUSTED = r.TRUSTED_REVIEW_APP_ID
REPO = 'TogetherWeOwn/paperclip-ops'


def thread(resolved=False, outdated=True, login='togetherweown-reviewer', cid=1):
    return {'id': 'T', 'isResolved': resolved, 'isOutdated': outdated,
            'comments': {'nodes': [{'databaseId': cid, 'author': {'login': login}}]}}


def cr(started, conclusion, title, name='Paperclip Review', app_id=TRUSTED,
       head=HEAD, status='completed', slug='togetherweown-reviewer'):
    run = {'name': name, 'started_at': started, 'conclusion': conclusion,
           'output': {'title': title}, 'status': status, 'head_sha': head}
    if app_id is not None:
        run['app'] = {'id': app_id, 'slug': slug}
    return run


def search_item(number, repo=REPO):
    return {'number': number,
            'repository_url': f'https://api.github.com/repos/{repo}'}


_PAGE_RE = re.compile(r'[?&]page=(\d+)')
_HEAD_RE = re.compile(r'commits/([0-9a-f]+)/check-runs')


class GhStub:
    """Argv-level gh double. Serves search pages, per-PR graphql, per-head
    check-runs pages; records mutations and call counts. Raises on surprises."""

    def __init__(self, search_pages=None, pr_threads=None, check_pages=None):
        # search_pages: list of dicts (each: items + incomplete_results) or
        #   Exceptions, indexed by page-1. Pages past the end serve empty.
        self.search_pages = search_pages if search_pages is not None else [
            {'total_count': 0, 'incomplete_results': False, 'items': []}]
        # pr_threads: {pr_number: (head, [threads])}; default: empty threads.
        self.pr_threads = pr_threads or {}
        # check_pages: {head: [page_dict | Exception]}; page_dict carries
        #   total_count + check_runs. Missing head serves total-0 empty.
        self.check_pages = check_pages or {}
        self.search_calls = 0
        self.graphql_calls = 0
        self.check_calls = 0
        self.mutations = []

    def __call__(self, e, *args, input_=None):
        argv = list(args)
        if any('resolveReviewThread' in a for a in argv):
            self.mutations.append('resolve')
            return '{}'
        if any('/replies' in a for a in argv):
            self.mutations.append('reply')
            return '{}'
        joined = ' '.join(argv)
        if 'search/issues' in joined:
            self.search_calls += 1
            m = _PAGE_RE.search(joined)
            page = int(m.group(1)) if m else 1
            if page <= len(self.search_pages):
                payload = self.search_pages[page - 1]
            else:
                payload = {'total_count': 0, 'incomplete_results': False, 'items': []}
            if isinstance(payload, Exception):
                raise payload
            return json.dumps(payload)
        if 'check-runs' in joined:
            self.check_calls += 1
            m = _PAGE_RE.search(joined)
            page = int(m.group(1)) if m else 1
            hm = _HEAD_RE.search(joined)
            head = hm.group(1) if hm else HEAD
            pages = self.check_pages.get(head)
            if pages is None:
                return json.dumps({'total_count': 0, 'check_runs': []})
            if page <= len(pages):
                payload = pages[page - 1]
            else:
                payload = {'total_count': 0, 'check_runs': []}
            if isinstance(payload, Exception):
                raise payload
            return json.dumps(payload)
        if argv[:2] == ['api', 'graphql'] and not any('resolveReviewThread' in a for a in argv):
            self.graphql_calls += 1
            num = 1
            for a in argv:
                if a.startswith('n='):
                    num = int(a[2:])
            head, nodes = self.pr_threads.get(num, (HEAD, []))
            return json.dumps({'data': {'repository': {'pullRequest': {
                'headRefOid': head, 'reviewThreads': {'nodes': nodes}}}}})
        raise AssertionError(f'unexpected gh call: {argv[:4]}')


def run_tick(stub, dry=False, paused=False):
    """Run one tick against the stub. Returns (report, stub, log_calls, stdout).

    subprocess.run is rigged to raise: any real spawn fails the test, proving
    zero network beyond the stub. PAUSE/STATE_DIR are isolated in tmp.
    """
    tmp = tempfile.TemporaryDirectory()
    log_calls = []
    state = Path(tmp.name) / 'state'
    state.mkdir(parents=True, exist_ok=True)
    if paused:
        (state / 'PAUSE').write_text('paused by test')

    def no_subprocess(*a, **k):
        raise AssertionError('real subprocess call escaped the gh stub')

    patches = [mock.patch.object(r, 'env', lambda: {}),
               mock.patch.object(r, 'gh', stub),
               mock.patch.object(r, 'log', lambda line, rec: log_calls.append(rec)),
               mock.patch.object(r.subprocess, 'run', no_subprocess),
               mock.patch.object(r, 'STATE_DIR', state),
               mock.patch.object(r, 'PAUSE', state / 'PAUSE')]
    for p in patches:
        p.start()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            report = r.run(dry=dry)
    finally:
        for p in reversed(patches):
            p.stop()
        tmp.cleanup()
    return report, stub, log_calls, out.getvalue()


def would_resolve_pr(num=1, head=HEAD):
    """A PR whose evidence (single-page 5/5) resolves under complete reads."""
    return {num: (head, [thread()])}


def single_5of5(head=HEAD):
    return [{'total_count': 1,
             'check_runs': [cr('2026-10-06T11:00Z', 'success', '5/5', head=head)]}]


class SearchPath(unittest.TestCase):
    def assert_tick_degraded(self, report, stub, log_calls):
        self.assertEqual(stub.mutations, [],
                         'truncated search must yield zero reply/resolve mutations')
        skipped = [row.get('skipped') for row in report]
        self.assertIn('incomplete_search', skipped,
                      f'expected an incomplete_search skip row, got: {report!r}')
        self.assertFalse(any(row.get('resolved') for row in report),
                         f'no PR may resolve from a truncated set: {report!r}')
        self.assertEqual(stub.graphql_calls, 0,
                         'no per-PR detail reads from a truncated listing')
        self.assertEqual(stub.check_calls, 0,
                         'no check-runs reads from a truncated listing')
        self.assertEqual(log_calls, [],
                         'refusals never write ledger/ACTIONS_LOG lines')

    def test_incomplete_results_first_page_degrades_tick(self):
        stub = GhStub(
            search_pages=[{'total_count': 1, 'incomplete_results': True,
                           'items': [search_item(1)]}],
            pr_threads=would_resolve_pr(),
            check_pages={HEAD: single_5of5()})
        report, stub, log_calls, _ = run_tick(stub)
        self.assert_tick_degraded(report, stub, log_calls)

    def test_incomplete_results_second_page_degrades_tick(self):
        stub = GhStub(
            search_pages=[
                {'total_count': 2, 'incomplete_results': False,
                 'items': [search_item(n) for n in range(1, 101)]},
                {'total_count': 2, 'incomplete_results': True,
                 'items': [search_item(101)]}],
            pr_threads={101: (HEAD, [thread()])},
            check_pages={HEAD: single_5of5()})
        report, stub, log_calls, _ = run_tick(stub)
        self.assert_tick_degraded(report, stub, log_calls)

    def test_max_prs_bound_hit_degrades_tick(self):
        pages = [{'total_count': 300, 'incomplete_results': False,
                  'items': [search_item(n) for n in range(p * 100 + 1, p * 100 + 101)]}
                 for p in range(3)]
        stub = GhStub(search_pages=pages,
                       pr_threads={300: (HEAD, [thread()])},
                       check_pages={HEAD: single_5of5()})
        report, stub, log_calls, _ = run_tick(stub)
        # A limit+1 probe (empty page 4) is allowed; resolving after it is not.
        self.assertGreaterEqual(stub.search_calls, 3)
        self.assertLessEqual(stub.search_calls, 4)
        self.assert_tick_degraded(report, stub, log_calls)

    def test_failed_search_page_degrades_tick(self):
        stub = GhStub(search_pages=[RuntimeError('gh search/issues: 502')])
        try:
            report, stub, log_calls, _ = run_tick(stub)
        except RuntimeError:
            self.fail('search failure propagated instead of degrading to '
                      'an incomplete_search skip with zero mutations')
        self.assert_tick_degraded(report, stub, log_calls)

    def test_empty_search_is_clean_empty_tick(self):
        stub = GhStub()  # default: one empty page
        report, stub, log_calls, _ = run_tick(stub)
        self.assertEqual(report, [])
        self.assertEqual(stub.mutations, [])
        self.assertEqual(log_calls, [])

    def test_complete_multipage_search_still_resolves(self):
        stub = GhStub(
            search_pages=[
                {'total_count': 101, 'incomplete_results': False,
                 'items': [search_item(n) for n in range(1, 101)]},
                {'total_count': 101, 'incomplete_results': False,
                 'items': [search_item(101)]}],
            pr_threads={101: (HEAD, [thread()])},
            check_pages={HEAD: single_5of5()})
        report, stub, log_calls, _ = run_tick(stub)
        self.assertEqual(stub.mutations, ['reply', 'resolve'])
        self.assertTrue(any(row.get('resolved') == 1 for row in report),
                        f'complete listing must still resolve: {report!r}')
        self.assertEqual(len(log_calls), 1)
        self.assertEqual(log_calls[0]['resolved'], 1)


class CheckRunsPath(unittest.TestCase):
    def drive_single_pr(self, pages, head=HEAD, dry=False):
        stub = GhStub(
            search_pages=[{'total_count': 1, 'incomplete_results': False,
                           'items': [search_item(1)]}],
            pr_threads={1: (head, [thread()])},
            check_pages={head: pages})
        return run_tick(stub, dry=dry)

    def assert_pr_skipped_incomplete(self, report, stub, log_calls, pr='TogetherWeOwn/paperclip-ops#1'):
        rows = [row for row in report if row.get('pr') == pr]
        self.assertTrue(rows, f'expected a report row for {pr}, got: {report!r}')
        self.assertTrue(all(row.get('skipped') == 'incomplete_check_runs' for row in rows),
                        f'{pr} must skip as incomplete_check_runs, got: {rows!r}')
        self.assertFalse(any(row.get('resolved') for row in rows),
                         f'{pr} must never resolve by omission: {rows!r}')
        self.assertEqual(stub.mutations, [])
        self.assertEqual(log_calls, [])

    def older_runs(self, head, count, latest_title='4/5', base_hour=8):
        runs = [cr(f'2026-10-06T{base_hour:02d}:{i:02d}Z', 'success', '5/5', head=head)
                for i in range(count - 1)]
        runs.append(cr(f'2026-10-06T{base_hour + 1:02d}:00Z', 'success', latest_title, head=head))
        return runs

    def test_reconciled_multipage_5of5_resolves(self):
        page1 = self.older_runs(HEAD, 100, latest_title='4/5', base_hour=8)
        page2 = self.older_runs(HEAD, 49, latest_title='4/5', base_hour=10)
        page2.append(cr('2026-10-06T12:00Z', 'success', '5/5', head=HEAD))
        pages = [{'total_count': 150, 'check_runs': page1},
                 {'total_count': 150, 'check_runs': page2}]
        report, stub, log_calls, _ = self.drive_single_pr(pages)
        self.assertGreaterEqual(stub.check_calls, 2,
                                'reader must page past the first 100 runs')
        self.assertEqual(stub.mutations, ['reply', 'resolve'])
        self.assertTrue(any(row.get('resolved') == 1 for row in report),
                        f'reconciled 5/5 must resolve: {report!r}')
        self.assertEqual(len(log_calls), 1)

    def test_dry_run_reconciled_counts_without_mutations(self):
        page1 = self.older_runs(HEAD, 100, latest_title='4/5', base_hour=8)
        page2 = self.older_runs(HEAD, 49, latest_title='4/5', base_hour=10)
        page2.append(cr('2026-10-06T12:00Z', 'success', '5/5', head=HEAD))
        pages = [{'total_count': 150, 'check_runs': page1},
                 {'total_count': 150, 'check_runs': page2}]
        report, stub, log_calls, _ = self.drive_single_pr(pages, dry=True)
        self.assertEqual(stub.mutations, [])
        self.assertTrue(any(row.get('resolved') == 1 and row.get('dry') is True
                            for row in report),
                        f'dry-run must count the reconciled resolve: {report!r}')
        self.assertEqual(log_calls, [])

    def test_total_count_mismatch_skips(self):
        page1 = self.older_runs(HEAD, 99, latest_title='4/5', base_hour=8)
        page1.append(cr('2026-10-06T12:00Z', 'success', '5/5', head=HEAD))
        short = self.older_runs(HEAD, 30, latest_title='4/5', base_hour=6)
        pages = [{'total_count': 150, 'check_runs': page1},
                 {'total_count': 150, 'check_runs': short}]
        report, stub, log_calls, _ = self.drive_single_pr(pages)
        self.assert_pr_skipped_incomplete(report, stub, log_calls)

    def test_over_bound_check_runs_skip(self):
        pages = [{'total_count': 5000,
                  'check_runs': self.older_runs(HEAD, 100, base_hour=8)}]
        # Endless full pages: the reader must stop at its bound, not drain.
        template = pages[0]
        for _ in range(60):
            pages.append(template)
        report, stub, log_calls, _ = self.drive_single_pr(pages)
        self.assertGreaterEqual(stub.check_calls, 2,
                                'reader must attempt pagination before bounding')
        self.assertLess(stub.check_calls, 50,
                        'reader must stop at an explicit bound, not drain 5000 runs')
        self.assert_pr_skipped_incomplete(report, stub, log_calls)

    def test_failed_second_page_skips(self):
        page1 = self.older_runs(HEAD, 99, latest_title='4/5', base_hour=8)
        page1.append(cr('2026-10-06T12:00Z', 'success', '5/5', head=HEAD))
        pages = [{'total_count': 150, 'check_runs': page1},
                 RuntimeError('gh check-runs: 502')]
        report, stub, log_calls, _ = self.drive_single_pr(pages)
        self.assert_pr_skipped_incomplete(report, stub, log_calls)

    def test_missing_total_count_skips_fail_closed(self):
        pages = [{'check_runs': [cr('2026-10-06T12:00Z', 'success', '5/5', head=HEAD)]}]
        report, stub, log_calls, _ = self.drive_single_pr(pages)
        self.assert_pr_skipped_incomplete(report, stub, log_calls)

    def test_single_page_5of5_control_resolves(self):
        report, stub, log_calls, _ = self.drive_single_pr(single_5of5())
        self.assertEqual(stub.mutations, ['reply', 'resolve'])
        self.assertTrue(any(row.get('resolved') == 1 for row in report),
                        f'single-page control must resolve: {report!r}')
        self.assertEqual(len(log_calls), 1)

    def test_empty_check_runs_keeps_no_trusted_evidence(self):
        pages = [{'total_count': 0, 'check_runs': []}]
        report, stub, log_calls, _ = self.drive_single_pr(pages)
        rows = [row for row in report
                if row.get('pr') == 'TogetherWeOwn/paperclip-ops#1']
        self.assertTrue(rows, f'expected a report row, got: {report!r}')
        self.assertEqual(rows[0].get('skipped'), 'no_trusted_evidence')
        self.assertEqual(stub.mutations, [])
        self.assertEqual(log_calls, [])

    def test_mismatch_does_not_poison_sibling_pr(self):
        page1 = self.older_runs(HEAD, 99, latest_title='4/5', base_hour=8)
        page1.append(cr('2026-10-06T12:00Z', 'success', '5/5', head=HEAD))
        short = self.older_runs(HEAD, 30, latest_title='4/5', base_hour=6)
        stub = GhStub(
            search_pages=[{'total_count': 2, 'incomplete_results': False,
                           'items': [search_item(1), search_item(2)]}],
            pr_threads={1: (HEAD, [thread()]), 2: (HEAD_B, [thread(cid=2)])},
            check_pages={
                HEAD: [{'total_count': 150, 'check_runs': page1},
                       {'total_count': 150, 'check_runs': short}],
                HEAD_B: single_5of5(head=HEAD_B)})
        report, stub, log_calls, _ = run_tick(stub)
        by_pr = {row.get('pr'): row for row in report}
        self.assertEqual(by_pr.get('TogetherWeOwn/paperclip-ops#1', {}).get('skipped'),
                         'incomplete_check_runs')
        self.assertEqual(by_pr.get('TogetherWeOwn/paperclip-ops#2', {}).get('resolved'), 1)
        self.assertEqual(stub.mutations, ['reply', 'resolve'],
                         'only the clean sibling PR may mutate')
        self.assertEqual(len(log_calls), 1)
        self.assertEqual(log_calls[0]['pr'], 'TogetherWeOwn/paperclip-ops#2')


class PreservedSemantics(unittest.TestCase):
    def test_pause_still_pauses_without_search(self):
        stub = GhStub()
        report, stub, log_calls, out = run_tick(stub, paused=True)
        self.assertIsNone(report)
        self.assertIn('paused', out)
        self.assertEqual(stub.search_calls, 0)
        self.assertEqual(stub.mutations, [])


if __name__ == '__main__':
    unittest.main()
