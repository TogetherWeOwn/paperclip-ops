"""Tests for cliproxy_config_gate.py (case_80989a33).

The migration cases mirror upstream internal/config/client_optimize_test.go at
v8.0.10, so a precedence change upstream shows up as a mismatch here first.
"""

import contextlib
import io
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cliproxy_config_gate as gate  # noqa: E402

SENTINEL = "sk-SENTINEL-must-never-print-0f3a9c"


class GateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name, body):
        path = os.path.join(self.dir.name, name)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(body))
        return path

    def run_gate(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def multi(self, body):
        code, out, _ = self.run_gate(self.write("c.yaml", body))
        return code, out


class MultiAgentMigration(GateTest):
    """client wins by presence, then oauth.providers, then providers, then flat."""

    def assertResolves(self, body, value, source):
        code, out = self.multi(body)
        self.assertIn(f"{gate.FLAG_MULTI_AGENT}={str(value).lower()} source={source}", out)
        self.assertEqual(code, 3 if value else 0)

    def test_absent_everywhere_defaults_false(self):
        self.assertResolves("port: 8317\n", False, "default")

    def test_flat_codex(self):
        self.assertResolves("codex:\n  optimize-multi-agent-v2: true\n",
                            True, "codex.optimize-multi-agent-v2")

    def test_providers_codex(self):
        self.assertResolves("providers:\n  codex:\n    optimize-multi-agent-v2: true\n",
                            True, "providers.codex.optimize-multi-agent-v2")

    def test_oauth_providers_codex(self):
        # The v8.0.8 spelling: a live true here keeps the flag ON after 8.0.10.
        self.assertResolves(
            "oauth:\n  providers:\n    codex:\n      optimize-multi-agent-v2: true\n",
            True, "oauth.providers.codex.optimize-multi-agent-v2")

    def test_old_false(self):
        self.assertResolves(
            "oauth:\n  providers:\n    codex:\n      optimize-multi-agent-v2: false\n",
            False, "oauth.providers.codex.optimize-multi-agent-v2")

    def test_client_false_wins_over_legacy_true(self):
        self.assertResolves("""\
            client:
              codex:
                optimize-multi-agent-v2: false
            oauth:
              providers:
                codex:
                  optimize-multi-agent-v2: true
            """, False, gate.FLAG_MULTI_AGENT)

    def test_client_true_wins_over_legacy_false(self):
        self.assertResolves("""\
            client:
              codex:
                optimize-multi-agent-v2: true
            codex:
              optimize-multi-agent-v2: false
            """, True, gate.FLAG_MULTI_AGENT)

    def test_client_null_wins_as_false(self):
        self.assertResolves("""\
            client:
              codex:
                optimize-multi-agent-v2:
            providers:
              codex:
                optimize-multi-agent-v2: true
            """, False, gate.FLAG_MULTI_AGENT)

    def test_oauth_wins_over_older_conflicts(self):
        self.assertResolves("""\
            oauth:
              providers:
                codex:
                  optimize-multi-agent-v2: false
            providers:
              codex:
                optimize-multi-agent-v2: true
            codex:
              optimize-multi-agent-v2: true
            """, False, "oauth.providers.codex.optimize-multi-agent-v2")

    def test_providers_wins_over_flat(self):
        self.assertResolves("""\
            providers:
              codex:
                optimize-multi-agent-v2: true
            codex:
              optimize-multi-agent-v2: false
            """, True, "providers.codex.optimize-multi-agent-v2")

    def test_yaml_merge_alias_is_followed(self):
        self.assertResolves("""\
            defaults: &codexdefaults
              optimize-multi-agent-v2: true
            oauth:
              providers:
                codex:
                  <<: *codexdefaults
            """, True, "oauth.providers.codex.optimize-multi-agent-v2")

    def test_legacy_source_is_flagged(self):
        _, out = self.multi("codex:\n  optimize-multi-agent-v2: false\n")
        self.assertIn("NOTE legacy spelling codex.optimize-multi-agent-v2", out)


class ApplyPatch(GateTest):
    def test_default_false(self):
        code, out = self.multi("port: 8317\n")
        self.assertIn(f"{gate.FLAG_APPLY_PATCH}=false source=default", out)
        self.assertEqual(code, 0)

    def test_client_true_fails(self):
        code, out = self.multi("client:\n  codex:\n    enable-apply-patch: true\n")
        self.assertIn(f"{gate.FLAG_APPLY_PATCH}=true", out)
        self.assertEqual(code, 3)

    def test_has_no_legacy_spelling(self):
        # A flat or provider spelling is NOT migrated for this flag upstream.
        code, out = self.multi("codex:\n  enable-apply-patch: true\n")
        self.assertIn(f"{gate.FLAG_APPLY_PATCH}=false source=default", out)
        self.assertEqual(code, 0)


class Refusals(GateTest):
    def test_non_boolean_is_unreadable(self):
        code, out, err = self.run_gate(
            self.write("c.yaml", "client:\n  codex:\n    enable-apply-patch: \"yes\"\n"))
        self.assertEqual(code, 2)
        self.assertIn("not a boolean", err)

    def test_parse_error_names_line_not_text(self):
        code, out, err = self.run_gate(
            self.write("c.yaml", f"api-keys:\n  - {SENTINEL}\n bad: [\n"))
        self.assertEqual(code, 2)
        self.assertIn("YAML parse error at line", err)
        self.assertNotIn(SENTINEL, out + err)

    def test_top_level_list_is_refused(self):
        code, _, err = self.run_gate(self.write("c.yaml", "- a\n- b\n"))
        self.assertEqual(code, 2)
        self.assertIn("not a mapping", err)

    def test_missing_file_is_unreadable(self):
        code, _, err = self.run_gate(os.path.join(self.dir.name, "absent.yaml"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)


class Baseline(GateTest):
    LIVE = f"""\
        port: 8317
        api-keys:
          - {SENTINEL}
        routing:
          request-retry: 3
        oauth:
          providers:
            codex:
              optimize-multi-agent-v2: false
        """

    def test_identical_copy_passes(self):
        live = self.write("live.yaml", self.LIVE)
        cand = self.write("cand.yaml", self.LIVE)
        code, out, err = self.run_gate(cand, "--baseline", live)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)
        self.assertNotIn("DELTA", out)

    def test_changed_secret_is_reported_by_path_only(self):
        live = self.write("live.yaml", self.LIVE)
        cand = self.write("cand.yaml", self.LIVE.replace(SENTINEL, SENTINEL + "-rotated"))
        code, out, err = self.run_gate(cand, "--baseline", live)
        self.assertEqual(code, 4)
        self.assertIn("DELTA changed api-keys[0]", out)
        self.assertNotIn(SENTINEL, out + err)

    def test_added_and_removed_paths(self):
        live = self.write("live.yaml", self.LIVE)
        cand = self.write("cand.yaml", self.LIVE.replace(
            "  request-retry: 3\n", "  max-retry-credentials: 0\n"))
        code, out, _ = self.run_gate(cand, "--baseline", live)
        self.assertEqual(code, 4)
        self.assertIn("DELTA added routing.max-retry-credentials", out)
        self.assertIn("DELTA removed routing.request-retry", out)

    def test_comment_only_change_is_still_a_delta(self):
        # The card preserves the config sha, so even a cosmetic edit needs review.
        live = self.write("live.yaml", self.LIVE)
        cand = self.write("cand.yaml", "# touched\n" + textwrap.dedent(self.LIVE))
        code, out, _ = self.run_gate(cand, "--baseline", live)
        self.assertEqual(code, 4)
        self.assertIn("DELTA bytes only", out)

    def test_flag_on_outranks_delta(self):
        live = self.write("live.yaml", self.LIVE)
        cand = self.write("cand.yaml", textwrap.dedent(self.LIVE)
                          + "client:\n  codex:\n    enable-apply-patch: true\n")
        code, out, _ = self.run_gate(cand, "--baseline", live)
        self.assertEqual(code, 3)
        self.assertIn("DELTA added client.codex.enable-apply-patch", out)

    def test_sentinel_never_printed_on_pass(self):
        live = self.write("live.yaml", self.LIVE)
        code, out, err = self.run_gate(live, "--baseline", live)
        self.assertEqual(code, 0)
        self.assertNotIn(SENTINEL, out + err)


if __name__ == "__main__":
    unittest.main()
