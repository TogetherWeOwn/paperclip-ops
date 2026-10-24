#!/usr/bin/env python3
"""Title-fallback acceptance: resolve_outdated_threads.review_evidence (TOG-39901).

Test-only handoff for PR #106 (fix/review-verdict-title-fallback). Pins the
missing/empty-title -> first-summary-line fallback WITHOUT implementing it.

Desired (PR #106): when output.title is missing/None/empty/whitespace-only,
accept iff output.summary's FIRST line is exactly "5/5". Any other shape
refuses with incomplete_score (existing reason), zero mutations.

Scope: review_evidence/head_is_5of5 only. Token, pagination, ledger paths are
covered by test_resolve_outdated_threads.py and
test_resolve_outdated_incomplete_evidence.py.

RED expectation on main (no fallback): the 6 *pass* cases FAIL (current code
returns incomplete_score because it ignores summary); all *refuse* cases and
controls PASS. GREEN once the fallback lands.

Wiring pointer for the Land owner (do NOT wire here; test-only card):
  cd ops/productivity && python3 -m unittest -v test_resolve_outdated_title_fallback
  Add `test_resolve_outdated_title_fallback` to the ops/productivity unittest
  list in .github/workflows/ci.yml (same step as test_resolve_outdated_threads)
  plus the offline-CI contract pin (preview --propose, apply --write).
"""
import unittest

import resolve_outdated_threads as r

HEAD = 'a' * 40
TRUSTED = r.TRUSTED_REVIEW_APP_ID

MISSING = object()  # sentinel: omit the key from output entirely


def cr(title=MISSING, summary=MISSING, started='2026-10-06T12:00:00Z',
       conclusion='success', status='completed', head=HEAD, app_id=TRUSTED):
    """One Paperclip Review check run with explicit title/summary control."""
    output = {}
    if title is not MISSING:
        output['title'] = title
    if summary is not MISSING:
        output['summary'] = summary
    run = {'name': 'Paperclip Review', 'started_at': started,
           'conclusion': conclusion, 'output': output, 'status': status,
           'head_sha': head}
    if app_id is not None:
        run['app'] = {'id': app_id, 'slug': 'togetherweown-reviewer'}
    return run


def evidence(title=MISSING, summary=MISSING, **kw):
    return r.review_evidence([cr(title=title, summary=summary, **kw)], HEAD)


class TitleFallbackPasses(unittest.TestCase):
    """Missing/empty title + exact first-line 5/5 MUST pass (RED on main)."""

    def test_missing_title_key_first_line_55(self):
        ok, reason = evidence(summary='5/5')
        self.assertTrue(ok, f'expected pass, got {reason}')

    def test_empty_title_first_line_55(self):
        ok, reason = evidence(title='', summary='5/5')
        self.assertTrue(ok, f'expected pass, got {reason}')

    def test_none_title_first_line_55(self):
        ok, reason = evidence(title=None, summary='5/5')
        self.assertTrue(ok, f'expected pass, got {reason}')

    def test_whitespace_title_first_line_55(self):
        # Assumption: whitespace-only title counts as empty (existing code
        # strips titles, so strip()=="" triggers the fallback).
        ok, reason = evidence(title='   ', summary='5/5')
        self.assertTrue(ok, f'expected pass, got {reason}')

    def test_missing_title_multiline_first_line_55(self):
        ok, reason = evidence(summary='5/5\n## details\nmore')
        self.assertTrue(ok, f'expected pass, got {reason}')

    def test_empty_title_multiline_first_line_55(self):
        ok, reason = evidence(title='', summary='5/5\nsecond line')
        self.assertTrue(ok, f'expected pass, got {reason}')


class TitleFallbackRefuses(unittest.TestCase):
    """Variants that MUST refuse even with the fallback (guards, green on main)."""

    def assert_refuses(self, title=MISSING, summary=MISSING, **kw):
        ok, reason = evidence(title=title, summary=summary, **kw)
        self.assertFalse(ok, f'expected refuse, got pass for title={title!r} summary={summary!r}')
        self.assertEqual(reason, 'incomplete_score')
        self.assertFalse(r.head_is_5of5([cr(title=title, summary=summary, **kw)], HEAD))

    def test_leading_space_refuses(self):
        self.assert_refuses(summary=' 5/5')

    def test_trailing_space_refuses(self):
        self.assert_refuses(summary='5/5 ')

    def test_suffix_refuses(self):
        self.assert_refuses(summary='5/5 approved')

    def test_colon_suffix_refuses(self):
        self.assert_refuses(summary='5/5: all good')

    def test_prefix_refuses(self):
        self.assert_refuses(summary='Score: 5/5')

    def test_second_line_refuses(self):
        self.assert_refuses(summary='Review complete\n5/5')

    def test_empty_first_line_refuses(self):
        self.assert_refuses(summary='\n5/5')

    def test_typical_heading_refuses(self):
        # The live summary shape "## Paperclip Review — 5/5" contains 5/5 but
        # is not exactly "5/5": it must NOT satisfy the fallback.
        self.assert_refuses(summary='## Paperclip Review — 5/5')

    def test_present_title_4of5_with_55_summary_refuses(self):
        # Fallback applies ONLY when title is missing/empty; a present
        # non-5/5 title takes precedence and refuses.
        self.assert_refuses(title='4/5', summary='5/5')

    def test_present_title_reviewed_with_55_summary_refuses(self):
        self.assert_refuses(title='reviewed', summary='5/5')

    def test_empty_summary_refuses(self):
        self.assert_refuses(summary='')

    def test_missing_summary_refuses(self):
        self.assert_refuses(title='', summary=MISSING)

    def test_zero_width_adverse_refuses(self):
        self.assert_refuses(summary='5/5\u200b')

    def test_broken_score_refuses(self):
        self.assert_refuses(summary='5 /5')


class TitleControls(unittest.TestCase):
    """Existing title-only behavior, unchanged by the fallback."""

    def test_title_55_still_passes(self):
        ok, _ = evidence(title='5/5', summary='## Paperclip Review — 5/5')
        self.assertTrue(ok)

    def test_title_55_with_null_summary_still_passes(self):
        ok, _ = evidence(title='5/5', summary=None)
        self.assertTrue(ok)

    def test_title_4of5_still_refuses(self):
        ok, reason = evidence(title='4/5')
        self.assertFalse(ok)
        self.assertEqual(reason, 'incomplete_score')


if __name__ == '__main__':
    unittest.main()
