#!/usr/bin/env python3
"""Title-fallback acceptance: review_carry.latest_review (PR #106 follow-up acceptance).

Test-only handoff for PR #106 (fix/review-verdict-title-fallback). Pins the
missing/empty-title -> first-summary-line fallback WITHOUT implementing it.

Desired (PR #106): when output.title is missing/None/empty/whitespace-only,
accept iff output.summary's FIRST line is exactly "5/5". Any other shape
returns None (re-review required). PR binding, App pin, status/conclusion and
carry-prefix rules are unchanged (see test_review_carry.py).

RED expectation on main (no fallback): the 6 *pass* cases FAIL (current code
returns None because it ignores summary); all *refuse* cases and controls
PASS. GREEN once the fallback lands.

Wiring pointer for the Land owner (do NOT wire here; test-only card):
  cd ops/sdlc && python3 -m unittest -v test_review_carry_title_fallback
  Add `test_review_carry_title_fallback` to the ops/sdlc unittest step in
  .github/workflows/ci.yml (same step as test_review_carry) plus the
  offline-CI contract pin (preview --propose, apply --write).
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))

_STUB_STATE = Path(tempfile.gettempdir()) / "review-carry-title-fallback-test-state"


class _StubLib:
    STATE_DIR = _STUB_STATE
    CID = "test-company"


sys.modules.setdefault("sdlc_lib", _StubLib())

sys.path.insert(0, HERE)
rc = importlib.import_module("review_carry")

HEAD = "a" * 40
MISSING = object()  # sentinel: omit the key from output entirely


def check(title=MISSING, summary=MISSING, check_id=1, head=HEAD,
          status="completed", conclusion="success", number=73,
          name="Paperclip Review", app_id=5191093):
    output = {}
    if title is not MISSING:
        output["title"] = title
    if summary is not MISSING:
        output["summary"] = summary
    return {
        "id": check_id,
        "name": name,
        "app": {"id": app_id},
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "output": output,
        "external_id": f"endpoint:{number}:{head}",
    }


def latest(title=MISSING, summary=MISSING, **kw):
    number = kw.pop("number", 73)
    head = kw.pop("head", HEAD)
    return rc.latest_review([check(title=title, summary=summary, number=number, head=head, **kw)], head, number)


class TitleFallbackPasses(unittest.TestCase):
    """Missing/empty title + exact first-line 5/5 MUST resolve (RED on main)."""

    def test_missing_title_key_first_line_55(self):
        self.assertIsNotNone(latest(summary="5/5"))

    def test_empty_title_first_line_55(self):
        self.assertIsNotNone(latest(title="", summary="5/5"))

    def test_none_title_first_line_55(self):
        self.assertIsNotNone(latest(title=None, summary="5/5"))

    def test_whitespace_title_first_line_55(self):
        # Assumption: whitespace-only title counts as empty (existing code
        # strips titles, so strip()=="" triggers the fallback).
        self.assertIsNotNone(latest(title="   ", summary="5/5"))

    def test_missing_title_multiline_first_line_55(self):
        self.assertIsNotNone(latest(summary="5/5\n## details\nmore"))

    def test_empty_title_multiline_first_line_55(self):
        self.assertIsNotNone(latest(title="", summary="5/5\nsecond line"))


class TitleFallbackRefuses(unittest.TestCase):
    """Variants that MUST return None even with the fallback (green on main)."""

    def assert_refuses(self, title=MISSING, summary=MISSING, **kw):
        got = latest(title=title, summary=summary, **kw)
        self.assertIsNone(got, f"expected None for title={title!r} summary={summary!r}")

    def test_leading_space_refuses(self):
        self.assert_refuses(summary=" 5/5")

    def test_trailing_space_refuses(self):
        self.assert_refuses(summary="5/5 ")

    def test_suffix_refuses(self):
        self.assert_refuses(summary="5/5 approved")

    def test_colon_suffix_refuses(self):
        self.assert_refuses(summary="5/5: all good")

    def test_prefix_refuses(self):
        self.assert_refuses(summary="Score: 5/5")

    def test_second_line_refuses(self):
        self.assert_refuses(summary="Review complete\n5/5")

    def test_empty_first_line_refuses(self):
        self.assert_refuses(summary="\n5/5")

    def test_typical_heading_refuses(self):
        self.assert_refuses(summary="## Paperclip Review — 5/5")

    def test_present_title_4of5_with_55_summary_refuses(self):
        self.assert_refuses(title="4/5", summary="5/5")

    def test_present_title_reviewed_with_55_summary_refuses(self):
        self.assert_refuses(title="reviewed", summary="5/5")

    def test_empty_summary_refuses(self):
        self.assert_refuses(summary="")

    def test_missing_summary_refuses(self):
        self.assert_refuses(title="", summary=MISSING)

    def test_zero_width_adverse_refuses(self):
        self.assert_refuses(summary="5/5\u200b")

    def test_broken_score_refuses(self):
        self.assert_refuses(summary="5 /5")


class TitleControls(unittest.TestCase):
    """Existing title-only behavior, unchanged by the fallback."""

    def test_title_55_still_resolves(self):
        self.assertIsNotNone(latest(title="5/5", summary="## Paperclip Review — 5/5"))

    def test_title_55_with_null_summary_still_resolves(self):
        self.assertIsNotNone(latest(title="5/5", summary=None))

    def test_title_4of5_still_refuses(self):
        self.assertIsNone(latest(title="4/5"))


if __name__ == "__main__":
    unittest.main()
