#!/usr/bin/env python3
"""Hermetic tests for review_carry.py (ops/sdlc).

No network, no credentials, no host: pure-function checks over in-memory
patches/check payloads, plus fixture HOME dirs for the optional hint files.
sdlc_lib is absent from the repo (host-only), so it is stubbed in
sys.modules before import; docs_lane is stubbed the same way. Exit codes
and degraded-outcome strings are pinned; human-readable text is not.
"""
import importlib
import json
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
          title="5/5", external_id="endpoint:73:" + HEAD, name="Paperclip Review",
          app_id=5191093):
    return {
        "id": check_id,
        "name": name,
        "app": {"id": app_id},
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "output": {"title": title},
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


class MintStdoutTests(unittest.TestCase):
    """parse_mint_stdout: exactly one credential object, pino lines tolerated.

    Regression: the in-container preflight's stdout gained a pino NDJSON log
    record ahead of the credential JSON, and whole-stream json.loads rejected
    every mint with "reviewer App credential response invalid". Only pino
    {level,time} records (with an optional string msg) are skipped; any other line, a missing
    credential, or a second credential object fails closed. The placeholder
    below is not a credential.
    """

    PINO = ('{"level":30,"time":1761097920000,"pid":1,"hostname":"h",'
            '"msg":"retained file logging enabled"}')
    CRED = ('{"token":"test-token-placeholder","app_id":5191093,'
            '"installation_id":1,"expires_at":"2030-01-01T00:00:00Z"}')

    def test_pino_line_plus_credential_returns_token(self):
        self.assertEqual(
            rc.parse_mint_stdout(self.PINO + "\n" + self.CRED + "\n"),
            "test-token-placeholder")

    def test_credential_only_still_returns_token(self):
        self.assertEqual(rc.parse_mint_stdout(self.CRED + "\n"),
                         "test-token-placeholder")

    def test_pino_unicode_msg_tolerated(self):
        pino = '{"level":30,"time":1761097920000,"msg":"r\\u00e9sum\\ncaf\\u00e9"}'
        self.assertEqual(rc.parse_mint_stdout(pino + "\n" + self.CRED + "\n"),
                         "test-token-placeholder")

    def test_raw_unicode_separators_in_message(self):
        for separator in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                log = json.dumps({"level": 30, "time": 1, "msg": "a" + separator + "b"}, ensure_ascii=False)
                self.assertEqual(rc.parse_mint_stdout(log + "\n" + self.CRED), "test-token-placeholder")

    def test_app_id_context_and_message_less_logs(self):
        for log in ('{"level":30,"time":1,"msg":"preflight","app_id":1234}',
                    '{"level":30,"time":1,"app_id":5191093}'):
            with self.subTest(log=log):
                self.assertEqual(rc.parse_mint_stdout(log + "\n" + self.CRED), "test-token-placeholder")

    def test_individual_malformed_log_fields_fail_closed(self):
        for log in ('{"level":"30","time":1,"msg":"x"}',
                    '{"time":1,"msg":"x"}',
                    '{"level":30,"time":"1","msg":"x"}',
                    '{"level":30,"msg":"x"}',
                    '{"level":30,"time":1,"msg":123}',
                    '{"level":true,"time":1,"msg":"x"}',
                    '{"level":30,"time":false,"msg":"x"}',
                    '{"level":30,"time":NaN,"msg":"x"}',
                    '{"level":30,"time":Infinity,"msg":"x"}',
                    '{"level":30,"time":-Infinity,"msg":"x"}'):
            with self.subTest(log=log), self.assertRaises(ValueError):
                rc.parse_mint_stdout(log + "\n" + self.CRED)

    def test_non_string_token_fails_closed(self):
        for token in (123, True, [1], {"value": "x"}):
            with self.subTest(token=token), self.assertRaises(ValueError):
                rc.parse_mint_stdout(json.dumps({"app_id": rc.APP_ID, "token": token}))

    def test_blank_lines_tolerated(self):
        self.assertEqual(
            rc.parse_mint_stdout("\n" + self.CRED + "\n\n"),
            "test-token-placeholder")

    def test_second_credential_object_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout(self.CRED + "\n" + self.CRED + "\n")

    def test_non_pino_object_line_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout('{"foo": 1}\n' + self.CRED + "\n")

    def test_plain_text_line_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout("starting preflight\n" + self.CRED + "\n")

    def test_non_object_json_line_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout("[1, 2]\n" + self.CRED + "\n")

    def test_malformed_json_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout(self.CRED + "\n" + '{"token": "truncated\n')

    def test_missing_credential_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout(self.PINO + "\n")

    def test_wrong_app_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout(self.CRED.replace("5191093", "1234") + "\n")

    def test_empty_token_fails_closed(self):
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout(
                self.CRED.replace("test-token-placeholder", "") + "\n")

    def test_token_line_is_never_skipped_as_log(self):
        # A token-bearing line counts as the credential even when it also
        # carries log-shaped keys; a second one still fails closed.
        loggy = ('{"level":30,"time":1,"msg":"x",'
                 '"token":"test-token-placeholder","app_id":5191093}')
        self.assertEqual(rc.parse_mint_stdout(loggy + "\n"),
                         "test-token-placeholder")
        with self.assertRaises(ValueError):
            rc.parse_mint_stdout(loggy + "\n" + self.CRED + "\n")


class MintContractTests(unittest.TestCase):
    """mint() maps mocked transport outcomes to the two Unsafe branches."""

    def run_mint(self, stdout, returncode=0):
        completed = mock.Mock(stdout=stdout, returncode=returncode)
        with mock.patch.object(rc.subprocess, "run",
                               return_value=completed), \
                mock.patch.object(rc.L, "load_cfg", return_value={},
                                  create=True):
            return rc.mint("o/r")

    def test_pino_prefixed_stdout_mints(self):
        token = self.run_mint(MintStdoutTests.PINO + "\n" +
                              MintStdoutTests.CRED + "\n")
        self.assertEqual(token, "test-token-placeholder")

    def test_invalid_log_and_token_map_to_unsafe(self):
        for stdout in ('{"level":true,"time":1,"msg":"x"}\n' + MintStdoutTests.CRED,
                       '{"level":30,"time":NaN,"msg":"x"}\n' + MintStdoutTests.CRED,
                       '{"app_id":5191093,"token":123}'):
            with self.subTest(stdout=stdout), self.assertRaisesRegex(rc.Unsafe, "response invalid"):
                self.run_mint(stdout)

    def test_raw_unicode_log_and_context_mint(self):
        log = json.dumps({"level": 30, "time": 1, "msg": "a\u2028b", "app_id": 1234}, ensure_ascii=False)
        self.assertEqual(self.run_mint(log + "\n" + MintStdoutTests.CRED), "test-token-placeholder")

    def test_garbage_line_fails_response_invalid(self):
        with self.assertRaisesRegex(rc.Unsafe, "response invalid"):
            self.run_mint("garbage\n" + MintStdoutTests.CRED + "\n")

    def test_nonzero_exit_fails_preflight_without_fallback(self):
        with self.assertRaisesRegex(rc.Unsafe, "preflight failed"):
            self.run_mint("", returncode=1)


if __name__ == "__main__":
    unittest.main()
