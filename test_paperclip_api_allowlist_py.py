#!/usr/bin/env python3
# ===========================================================================
# test_paperclip_api_allowlist_py.py -- hermetic suite for the shared Python
# API host allowlist (lib/paperclip_api_allowlist.py, warn-first shadow).
#
# Pins: exact-origin match after normalization, the attacker-case suite
# (sibling domains, userinfo, ports, trailing-path, case, trailing-dot),
# fail-closed file behavior (missing/unreadable/malformed, loud line
# numbers, never silent skip), the shadow WARNING + counter, the hatch
# accept/refuse matrix, the bridge marker + loopback rule, mode handling,
# and the --check dry-run. Fully offline: fixture host files in a temp dir,
# explicit environ dicts (never ambient os.environ), no network.
#
#   python3 -B -m unittest -v test_paperclip_api_allowlist_py
# ===========================================================================

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import paperclip_api_allowlist as allowlist
from paperclip_api_allowlist import AllowlistRefused

HERE = os.path.dirname(os.path.abspath(__file__))
SENDER = "suite_probe"
MISSING = os.path.join("definitely", "not", "here", "allowed-api-hosts")


def check(base, sender=SENDER, path=None, env=None, quiet=False):
    return allowlist.check_api_base_allowed(
        base, sender, allowlist_path=path or MISSING,
        environ={} if env is None else env, quiet=quiet)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        allowlist.SHADOW_COUNT = 0

    def fixture(self, *lines):
        path = os.path.join(self.tmp.name, "allowed-api-hosts")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + ("\n" if lines else ""))
        return path

    def warned(self, base, path, env=None):
        err = io.StringIO()
        with redirect_stderr(err):
            verdict = check(base, path=path, env=env)
        self.assertEqual(verdict, "shadow")
        return err.getvalue()


class NormalizationAndMatch(FixtureCase):
    def test_exact_match_allows(self):
        path = self.fixture("https://board.example.invalid")
        self.assertEqual(check("https://board.example.invalid", path=path),
                         "file_match")

    def test_case_insensitive_host_matches(self):
        path = self.fixture("https://board.example.invalid")
        self.assertEqual(check("https://BOARD.Example.Invalid", path=path),
                         "file_match")

    def test_default_port_and_trailing_slash_match(self):
        path = self.fixture("https://board.example.invalid")
        for base in ("https://board.example.invalid:443",
                     "https://board.example.invalid/",
                     "https://board.example.invalid:443/"):
            with self.subTest(base=base):
                self.assertEqual(check(base, path=path), "file_match")

    def test_explicit_default_port_entry_matches_bare_candidate(self):
        path = self.fixture("https://board.example.invalid:443")
        self.assertEqual(check("https://board.example.invalid", path=path),
                         "file_match")

    def test_non_default_port_never_matches_portless_entry(self):
        path = self.fixture("https://board.example.invalid")
        self.warned("https://board.example.invalid:8443", path)

    def test_port_bearing_entry_matches_same_port(self):
        path = self.fixture("https://board.example.invalid:8443")
        self.assertEqual(
            check("https://board.example.invalid:8443", path=path),
            "file_match")
        self.warned("https://board.example.invalid", path)
        self.warned("https://board.example.invalid:9443", path)

    def test_comments_blanks_and_duplicates(self):
        path = self.fixture(
            "# operator-owned hosts",
            "",
            "   ",
            "https://board.example.invalid",
            "https://board.example.invalid  ",
            "\t# trailing comment",
        )
        self.assertEqual(check("https://board.example.invalid", path=path),
                         "file_match")

    def test_empty_file_is_valid_and_matches_nothing(self):
        path = self.fixture()
        self.warned("https://board.example.invalid", path)

    def test_ipv6_origin_key_round_trip(self):
        parsed = allowlist._parse_origin("http://[::1]:8080/")
        self.assertEqual(allowlist._origin_key(parsed), "http://[::1]:8080")


class AttackerSuite(FixtureCase):
    def setUp(self):
        super().setUp()
        self.path = self.fixture("https://board.example.invalid")

    def test_sibling_domains_warn_in_shadow(self):
        for base in ("https://board.example.invalid.evil.invalid",
                     "https://evilboard.example.invalid",
                     "https://boardxexample.invalid",
                     "https://example.invalid",
                     "https://board.example.invalidcom"):
            with self.subTest(base=base):
                self.warned(base, self.path)

    def test_userinfo_refuses_in_every_mode(self):
        for env in ({}, {"PAPERCLIP_API_ALLOWLIST_MODE": "enforce"}):
            with self.subTest(env=env), self.assertRaises(AllowlistRefused):
                check("https://user@board.example.invalid/", path=self.path,
                      env=env)

    def test_trailing_path_never_matches(self):
        err = self.warned("https://board.example.invalid/evil", self.path)
        self.assertIn("reason=path-not-matched", err)

    def test_trailing_dot_never_matches(self):
        self.warned("https://board.example.invalid./", self.path)

    def test_mismatches_refuse_in_enforce(self):
        env = {"PAPERCLIP_API_ALLOWLIST_MODE": "enforce"}
        for base in ("https://board.example.invalid.evil.invalid",
                     "https://board.example.invalid:8443",
                     "https://board.example.invalid/evil",
                     "https://board.example.invalid./"):
            with self.subTest(base=base), self.assertRaises(AllowlistRefused):
                check(base, path=self.path, env=env)

    def test_matches_still_allow_in_enforce(self):
        env = {"PAPERCLIP_API_ALLOWLIST_MODE": "enforce"}
        self.assertEqual(
            check("https://board.example.invalid", path=self.path, env=env),
            "file_match")


class FileFailureModes(FixtureCase):
    def test_missing_file_refuses_in_every_mode(self):
        for env in ({}, {"PAPERCLIP_API_ALLOWLIST_MODE": "enforce"}):
            with self.subTest(env=env):
                with self.assertRaises(AllowlistRefused) as raised:
                    check("https://board.example.invalid", env=env)
                self.assertIn("missing or unreadable", str(raised.exception))

    def test_directory_as_file_refuses(self):
        with self.assertRaises(AllowlistRefused):
            check("https://board.example.invalid", path=self.tmp.name)

    def test_undecodable_file_refuses_as_allowlist_refused(self):
        path = os.path.join(self.tmp.name, "allowed-api-hosts")
        with open(path, "wb") as handle:
            handle.write(b"https://board.example.invalid\n\xff\xfe\n")
        with self.assertRaises(AllowlistRefused) as raised:
            check("https://board.example.invalid", path=path)
        self.assertIn("missing or unreadable", str(raised.exception))

    def test_malformed_lines_refuse_naming_the_line(self):
        bad_lines = [
            "http://board.example.invalid",
            "https://board.example.invalid/evil",
            "https://user@board.example.invalid/",
            "https://board.example.invalid?x=1",
            "https://board.example.invalid./",
            "notaurl",
            "https://board.example.invalid{,}",
            "https://board.example.invalid # inline comment",
        ]
        for bad in bad_lines:
            path = self.fixture("https://board.example.invalid", bad,
                                "https://other.example.invalid")
            with self.subTest(line=bad):
                with self.assertRaises(AllowlistRefused) as raised:
                    check("https://board.example.invalid", path=path)
                message = str(raised.exception)
                self.assertIn(":2:", message)
                self.assertIn("malformed origin", message)

    def test_malformed_line_refuses_even_a_listed_candidate(self):
        path = self.fixture("https://board.example.invalid", "garbage{{{")
        with self.assertRaises(AllowlistRefused):
            check("https://board.example.invalid", path=path)


class ShadowLogAndCounter(FixtureCase):
    def test_warning_format_and_counter(self):
        path = self.fixture("https://board.example.invalid")
        err = self.warned("https://evil.example.invalid", path)
        self.assertIn(
            "WARNING allowlist_shadow base_host=evil.example.invalid "
            "sender=%s base=https://evil.example.invalid mode=warn" % SENDER,
            err)
        self.assertEqual(allowlist.SHADOW_COUNT, 1)
        self.warned("https://evil.example.invalid:8443", path)
        self.assertEqual(allowlist.SHADOW_COUNT, 2)

    def test_matches_bridge_and_hatch_do_not_warn_or_count(self):
        path = self.fixture("https://board.example.invalid")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check("https://board.example.invalid",
                                   path=path), "file_match")
            self.assertEqual(
                check("http://127.0.0.1:38035", path=path,
                      env={"PAPERCLIP_API_BRIDGE_MODE": "queue_v1"}), "bridge")
            self.assertEqual(
                check("https://board.invalid", path=path,
                      env={"PAPERCLIP_API_ALLOWLIST_TEST": "1"}), "hatch")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(allowlist.SHADOW_COUNT, 0)

    def test_counter_file_increments(self):
        counter = os.path.join(self.tmp.name, "shadow.count")
        path = self.fixture("https://board.example.invalid")
        env = {"PAPERCLIP_API_ALLOWLIST_COUNTER_FILE": counter}
        self.warned("https://evil.example.invalid", path, env)
        self.warned("https://evil.example.invalid", path, env)
        with open(counter, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "2\n")

    def test_counter_file_failure_never_fails_the_send(self):
        path = self.fixture("https://board.example.invalid")
        env = {"PAPERCLIP_API_ALLOWLIST_COUNTER_FILE":
               os.path.join(self.tmp.name, "no-such-dir", "c")}
        self.warned("https://evil.example.invalid", path, env)


class HatchMatrix(FixtureCase):
    HATCH = {"PAPERCLIP_API_ALLOWLIST_TEST": "1"}

    def test_hatch_allows_test_only_bases_without_a_file(self):
        for base in ("http://127.0.0.1:9/api",
                     "https://127.0.0.1/api",
                     "http://127.0.0.2:8080/",
                     "http://127.255.255.255/",
                     "http://localhost:3000/api",
                     "https://localhost/",
                     "http://[::1]:9/api",
                     "https://[::1]/",
                     "http://board.invalid/api",
                     "https://board.invalid/",
                     "https://deep.sub.board.invalid/"):
            with self.subTest(base=base):
                # MISSING proves the hatch never consults the host file.
                self.assertEqual(check(base, env=dict(self.HATCH)), "hatch")

    def test_hatch_refuses_anything_else(self):
        for base in ("https://board.example.com",
                     "https://example.com/",
                     "http://128.0.0.1/",
                     "http://localhost./",
                     "http://invalid/",
                     "https://board.example.invalid.com/"):
            with self.subTest(base=base):
                with self.assertRaises(AllowlistRefused) as raised:
                    check(base, env=dict(self.HATCH))
                self.assertIn("test hatch with non-test base",
                              str(raised.exception))

    def test_hatch_is_exact_value_1(self):
        path = self.fixture("https://board.example.invalid")
        for value in ("0", "true", "yes", " 1", "1 "):
            with self.subTest(value=value):
                # Not the hatch: loopback falls through to the file (warn).
                self.warned("http://127.0.0.1:9/", path,
                            {"PAPERCLIP_API_ALLOWLIST_TEST": value})

    def test_no_hatch_means_file_path_for_loopback(self):
        path = self.fixture("https://board.example.invalid")
        self.warned("http://127.0.0.1:9/", path)


class BridgeMatrix(FixtureCase):
    MARKER = {"PAPERCLIP_API_BRIDGE_MODE": "queue_v1"}

    def test_marker_plus_loopback_allows_without_a_file(self):
        for base in ("http://127.0.0.1:38035",
                     "http://127.0.0.1:1/",
                     "http://127.0.0.1:65535/api"):
            with self.subTest(base=base):
                self.assertEqual(check(base, env=dict(self.MARKER)),
                                 "bridge")

    def test_marker_widens_nothing_else(self):
        path = self.fixture("https://board.example.invalid")
        # https loopback, neighbor loopback, portless loopback, and hostile
        # hosts fall through.
        self.warned("https://127.0.0.1:38035/", path, dict(self.MARKER))
        self.warned("http://127.0.0.2:38035/", path, dict(self.MARKER))
        self.warned("http://127.0.0.1/", path, dict(self.MARKER))
        self.warned("https://evil.example.invalid/", path, dict(self.MARKER))
        # A listed host still matches by file (marker ignored, not needed).
        self.assertEqual(
            check("https://board.example.invalid", path=path,
                  env=dict(self.MARKER)), "file_match")

    def test_wrong_marker_value_is_ignored(self):
        path = self.fixture("https://board.example.invalid")
        for value in ("1", "true", "queue_v2", " queue_v1"):
            with self.subTest(value=value):
                self.warned("http://127.0.0.1:38035/", path,
                            {"PAPERCLIP_API_BRIDGE_MODE": value})


class ModeHandling(FixtureCase):
    def test_default_and_empty_mode_is_warn(self):
        path = self.fixture("https://board.example.invalid")
        self.warned("https://evil.example.invalid", path, {})
        self.warned("https://evil.example.invalid", path,
                    {"PAPERCLIP_API_ALLOWLIST_MODE": ""})

    def test_unknown_mode_refuses(self):
        path = self.fixture("https://board.example.invalid")
        with self.assertRaises(AllowlistRefused) as raised:
            check("https://evil.example.invalid", path=path,
                  env={"PAPERCLIP_API_ALLOWLIST_MODE": "disabled"})
        self.assertIn("unknown PAPERCLIP_API_ALLOWLIST_MODE",
                      str(raised.exception))

    def test_quiet_precheck_reports_nothing(self):
        path = self.fixture("https://board.example.invalid")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(
                check("https://evil.example.invalid", path=path, quiet=True),
                "shadow")
            with self.assertRaises(AllowlistRefused):
                check("https://evil.example.invalid", path=path,
                      env={"PAPERCLIP_API_ALLOWLIST_MODE": "enforce"},
                      quiet=True)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(allowlist.SHADOW_COUNT, 0)


class CheckCommand(FixtureCase):
    def run_check(self, *args, env=None):
        cmd = [sys.executable, os.path.join(HERE, "lib",
                                            "paperclip_api_allowlist.py")]
        run_env = {"PATH": "/usr/bin:/bin"}
        if env:
            run_env.update(env)
        return subprocess.run(cmd + list(args), env=run_env,
                              capture_output=True, text=True)

    def test_allow(self):
        path = self.fixture("https://board.example.invalid")
        proc = self.run_check("--check", "https://board.example.invalid",
                              "--file", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ALLOW", proc.stdout)
        self.assertIn("verdict=file_match", proc.stdout)

    def test_shadow_allows_with_warning(self):
        path = self.fixture("https://board.example.invalid")
        proc = self.run_check("--check", "https://evil.example.invalid",
                              "--file", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ALLOW", proc.stdout)
        self.assertIn("allowlist_shadow", proc.stderr)

    def test_enforce_refuses(self):
        path = self.fixture("https://board.example.invalid")
        proc = self.run_check("--check", "https://evil.example.invalid",
                              "--file", path, "--mode", "enforce")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REFUSE", proc.stdout)

    def test_malformed_file_refuses(self):
        path = self.fixture("https://board.example.invalid", "garbage{{{")
        proc = self.run_check("--check", "https://board.example.invalid",
                              "--file", path)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REFUSE", proc.stdout)
        self.assertIn("malformed origin", proc.stderr)

    def test_hatch_allows_with_missing_file(self):
        # The dry-run mirrors the senders: hatch/bridge verdicts never
        # consult the file, so a missing file must not flip them to REFUSE.
        missing = os.path.join(self.tmp.name, "definitely-not-here")
        proc = self.run_check("--check", "https://board.example.invalid",
                              "--file", missing,
                              env={"PAPERCLIP_API_ALLOWLIST_TEST": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ALLOW", proc.stdout)
        self.assertIn("verdict=hatch", proc.stdout)
        self.assertIn("entries=?", proc.stdout)

    def test_bridge_allows_with_missing_file(self):
        missing = os.path.join(self.tmp.name, "definitely-not-here")
        proc = self.run_check("--check", "http://127.0.0.1:38035/",
                              "--file", missing,
                              env={"PAPERCLIP_API_BRIDGE_MODE": "queue_v1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ALLOW", proc.stdout)
        self.assertIn("verdict=bridge", proc.stdout)

    def test_missing_file_still_refuses_without_hatch(self):
        missing = os.path.join(self.tmp.name, "definitely-not-here")
        proc = self.run_check("--check", "https://evil.example.com",
                              "--file", missing)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REFUSE", proc.stdout)
        self.assertIn("entries=?", proc.stdout)

    def test_usage_error(self):
        proc = self.run_check("--bogus")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
