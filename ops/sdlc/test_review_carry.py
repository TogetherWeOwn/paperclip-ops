#!/usr/bin/env python3
"""Hermetic tests for review_carry.py (ops/sdlc).

No network, no credentials, no host: pure-function checks over in-memory
patches/check payloads, plus fixture HOME dirs for the optional hint files.
sdlc_lib is absent from the repo (host-only), so it is stubbed in
sys.modules before import; docs_lane is stubbed the same way. Exit codes
and degraded-outcome strings are pinned; human-readable text is not.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))

# Stub the host-only sdlc_lib before review_carry imports it. Only STATE_DIR
# is touched at import time; the rest exists so attribute access fails loudly
# rather than succeeding silently if a tested path ever reaches it.
_STUB_STATE = Path(tempfile.gettempdir()) / "review-carry-test-state"


class _StubLib:
    STATE_DIR = _STUB_STATE
    CID = "test-company"


sys.modules.setdefault("sdlc_lib", _StubLib())

sys.path.insert(0, HERE)
rc = importlib.import_module("review_carry")


HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def check(check_id=1, head=HEAD, status="completed", conclusion="success",
          title="5/5", summary=None, include_title=True,
          external_id="endpoint:73:" + HEAD, name="Paperclip Review",
          app_id=5191093):
    output = {}
    if include_title:
        output["title"] = title
    if summary is not None:
        output["summary"] = summary
    return {
        "id": check_id,
        "name": name,
        "app": {"id": app_id},
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "output": output,
        "external_id": external_id,
    }


def patch(*bodies):
    """Join patch fragments with LF record separators like git emits."""
    return b"\n".join(bodies) + b"\n"


DIFF_A = b"diff --git a/f b/f"
INDEX = b"index 1111111..2222222 100644"


class NormalizedPatchTest(unittest.TestCase):
    def test_cr_bytes_inside_changed_lines_are_preserved(self):
        with_cr = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+a\rb")
        without_cr = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+ab")
        self.assertNotEqual(rc.normalized_patch(with_cr),
                            rc.normalized_patch(without_cr))
        self.assertNotEqual(rc.digest(with_cr), rc.digest(without_cr))

    def test_significant_whitespace_compares_unequal(self):
        plain = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+x")
        trailing_space = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+x ")
        self.assertNotEqual(rc.normalized_patch(plain),
                            rc.normalized_patch(trailing_space))

    def test_file_type_transition_keeps_both_records(self):
        # Git emits two diff --git records for a delete/add transition.
        transition = patch(DIFF_A, INDEX, b"@@ -1 +0,0 @@", b"-old",
                           DIFF_A, INDEX, b"@@ -0,0 +1 @@", b"+new")
        bodies = rc.normalized_patch(transition)
        self.assertEqual(len(bodies), 1)
        only = next(iter(bodies.values()))
        self.assertEqual(only.count(b"\x00file-record\x00"), 2)
        self.assertIn(b"-old\n", only)
        self.assertIn(b"+new\n", only)

    def test_hunk_offset_shift_compares_equal(self):
        left = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+same")
        right = patch(DIFF_A, INDEX, b"@@ -100 +100 @@", b"+same")
        self.assertEqual(rc.normalized_patch(left), rc.normalized_patch(right))
        self.assertEqual(rc.digest(left), rc.digest(right))

    def test_mode_only_change_compares_unequal(self):
        exec_bit = patch(DIFF_A, b"index 1111111..2222222 100755",
                         b"@@ -1 +1 @@", b"+same")
        no_exec = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+same")
        self.assertNotEqual(rc.normalized_patch(exec_bit),
                            rc.normalized_patch(no_exec))


class LatestReviewTest(unittest.TestCase):
    def test_accepts_bound_55(self):
        got = rc.latest_review([check()], HEAD, 73)
        self.assertIsNotNone(got)
        self.assertEqual(got["id"], 1)

    def test_accepts_first_summary_line_when_title_is_missing(self):
        got = rc.latest_review(
            [check(include_title=False, summary="5/5\nother text")], HEAD, 73)
        self.assertIsNotNone(got)

    def test_accepts_first_summary_line_when_title_is_empty(self):
        got = rc.latest_review(
            [check(title="", summary="5/5\nother text")], HEAD, 73)
        self.assertIsNotNone(got)

    def test_nonempty_title_takes_precedence_over_summary(self):
        self.assertIsNone(
            rc.latest_review([check(title="4/5", summary="5/5")], HEAD, 73))

    def test_rejects_same_head_different_pr(self):
        # Same head H reviewed 5/5 on PR #10 must not satisfy PR #11.
        other_pr = check(external_id="endpoint:10:" + HEAD)
        self.assertIsNone(rc.latest_review([other_pr], HEAD, 11))

    def test_rejects_missing_external_id(self):
        self.assertIsNone(rc.latest_review([check(external_id=None)], HEAD, 73))

    def test_newer_failed_invalidates_older_55(self):
        older = check(check_id=1)
        newer = check(check_id=2, conclusion="failure")
        self.assertIsNone(rc.latest_review([older, newer], HEAD, 73))

    def test_newer_other_pr_check_does_not_displace_own_55(self):
        # Same head H shared by PR #73 (valid 5/5, id 1) and PR #10 with a
        # newer check (id 2): PR #73's carry still resolves to its own check.
        own = check(check_id=1)
        other = check(check_id=2, external_id="endpoint:10:" + HEAD)
        got = rc.latest_review([own, other], HEAD, 73)
        self.assertIsNotNone(got)
        self.assertEqual(got["id"], 1)

    def test_mixed_prs_newer_failed_same_pr_still_invalidates(self):
        # Own newer failure (id 2) invalidates own older 5/5 (id 1) even
        # when another PR holds a newer 5/5 (id 3) on the same head.
        older = check(check_id=1)
        newer_failed = check(check_id=2, conclusion="failure")
        other = check(check_id=3, external_id="endpoint:10:" + HEAD)
        self.assertIsNone(
            rc.latest_review([older, newer_failed, other], HEAD, 73))

    def test_rejects_carry_prefixed(self):
        carried = check(external_id=rc.PREFIX + "0" * 40)
        self.assertIsNone(rc.latest_review([carried], HEAD, 73))

    def test_rejects_non_55_title(self):
        self.assertIsNone(
            rc.latest_review([check(title="4/5")], HEAD, 73))


class LatestDocsReviewTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def verify_carry(repo, number, head, base, token):
            self.calls.append((repo, number, head, base, token))

        stub = mock.Mock()
        stub.verify_carry.side_effect = verify_carry
        self._patch = mock.patch.dict(sys.modules, {"docs_lane": stub})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def docs_check(self, number=73, head=HEAD, base="c" * 40):
        return check(title=rc.DOCS_SOURCE_TITLE,
                     external_id=f"{rc.DOCS_SOURCE_PREFIX}{number}:{head}:{base}")

    def test_accepts_exact_binding(self):
        base = "c" * 40
        got = rc.latest_docs_review([self.docs_check()], "o/r", 73, HEAD, base,
                                    "tok")
        self.assertIsNotNone(got)
        self.assertEqual(self.calls, [("o/r", 73, HEAD, base, "tok")])

    def test_rejects_wrong_number(self):
        base = "c" * 40
        got = rc.latest_docs_review([self.docs_check(number=10)], "o/r", 11,
                                    HEAD, base, "tok")
        self.assertIsNone(got)
        self.assertEqual(self.calls, [])

    def test_rejects_wrong_head(self):
        base = "c" * 40
        got = rc.latest_docs_review([self.docs_check()], "o/r", 73,
                                    OTHER_HEAD, base, "tok")
        self.assertIsNone(got)

    def test_rejects_wrong_base(self):
        got = rc.latest_docs_review([self.docs_check()], "o/r", 73, HEAD,
                                    "d" * 40, "tok")
        self.assertIsNone(got)


class EventReposTest(unittest.TestCase):
    def run_with_home(self, setup):
        with tempfile.TemporaryDirectory() as home:
            setup(Path(home))
            with mock.patch.dict(os.environ, {"HOME": home}):
                return rc.event_repos()

    def test_absent_env_returns_absent(self):
        repos, status = self.run_with_home(lambda home: None)
        self.assertEqual((repos, status), (set(), "absent"))

    def test_unbalanced_quote_returns_invalid_config(self):
        def setup(home):
            drop = home / "secure-drop"
            drop.mkdir()
            (drop / "gh-event-capture.env").write_text('KEY="unterminated\n')

        repos, status = self.run_with_home(setup)
        self.assertEqual((repos, status), (set(), "invalid-config"))

    def test_non_utf8_returns_invalid_config(self):
        def setup(home):
            drop = home / "secure-drop"
            drop.mkdir()
            (drop / "gh-event-capture.env").write_bytes(b"\xff\xfe\n")

        repos, status = self.run_with_home(setup)
        self.assertEqual((repos, status), (set(), "invalid-config"))

    def test_missing_url_and_token_returns_unconfigured(self):
        def setup(home):
            drop = home / "secure-drop"
            drop.mkdir()
            (drop / "gh-event-capture.env").write_text("FOO=bar\n")

        repos, status = self.run_with_home(setup)
        self.assertEqual((repos, status), (set(), "unconfigured"))


class DecideTest(unittest.TestCase):
    def test_no_review_is_failure(self):
        conclusion, _ = rc.decide(None, None)
        self.assertEqual(conclusion, "failure")

    def test_changed_patch_is_failure(self):
        conclusion, _ = rc.decide(check(), {"equal": False})
        self.assertEqual(conclusion, "failure")

    def test_identical_patch_is_success(self):
        conclusion, _ = rc.decide(check(), {"equal": True})
        self.assertEqual(conclusion, "success")


if __name__ == "__main__":
    unittest.main()
