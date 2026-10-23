#!/usr/bin/env python3
# ===========================================================================
# test_kofra_preview_parity.py — hostile tests for the staging-vs-prod
# parity check (case_db6cb3f2).
#
# These are written to FAIL a plausible-but-wrong implementation, not to
# confirm the one that exists. A suite that only feeds matching configs
# validates the author's own dialect and nothing else — so every case below
# is either a shape Kofra's config actually carries, or the specific
# shortcut a reimplementation would take.
#
# THE CASES THAT CARRY THIS SUITE:
#
#   1. The worker name is SEMANTIC, not a diff row. A naive key-by-key diff
#      of staging vs prod reports worker.name as one more DRIFT line among
#      others — and an allowlist entry then silences the one divergence that
#      is an incident (a staging deploy landing on prod). This suite pins
#      that equal names are drift even when the names are allowlisted.
#
#   2. Var VALUES never enter output, on any path, in any mode. A tool that
#      prints a checked-in secret value trains operators to paste real ones.
#      This suite feeds a valued var and asserts the value string appears
#      nowhere in stdout or stderr, while the finding itself still fires.
#
#   3. A stale allowlist entry REFUSES. The covered divergence is fixed, the
#      entry stays, and the next real divergence on an adjacent path hides
#      behind a green run. The suite pins that an unmatched entry is exit 2,
#      not exit 0.
#
#   4. The prod side is STRUCTURAL. When env.production exists it is the prod
#      side; otherwise top-level is. A `--prod-env` flag would let a run
#      redefine prod until it matches, so there is no such flag and the suite
#      pins the switch on file content alone.
#
# Deterministic: every case builds its config in a temp dir. No network, no
# clock read, no credential, no write outside the temp dir. Exit status only;
# no assertion matches human-readable prose, which drifts.
# ===========================================================================
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kofra_preview_parity as parity  # noqa: E402

BASE = {
    "name": "kofra-prod",
    "main": "src/index.ts",
    "compatibility_date": "2026-09-20",
    "workers_dev": False,
    "preview_urls": False,
    "routes": [],
    "vars": {"RELEASE_SHA": ""},
}


def make_config(top_extra=None, staging_extra=None, with_prod_env=False,
                prod_env_extra=None, staging_vars=None, top_vars=None):
    cfg = dict(BASE)
    if top_vars is not None:
        cfg["vars"] = top_vars
    if top_extra:
        cfg.update(top_extra)
    # Staging mirrors the real checked-in shape: no main/compatibility_date
    # (inherited from top-level at deploy time — the inheritance case the
    # flatten fallback exists for).
    staging = {
        "name": "kofra-staging",
        "workers_dev": False,
        "preview_urls": False,
        "routes": [],
        "vars": dict(BASE["vars"]) if staging_vars is None else staging_vars,
    }
    if staging_extra:
        staging.update(staging_extra)
    cfg["env"] = {"staging": staging}
    if with_prod_env:
        prod = {
            "name": "kofra-prod",
            "workers_dev": False,
            "preview_urls": False,
            "routes": [],
            "vars": dict(BASE["vars"]),
        }
        if prod_env_extra:
            prod.update(prod_env_extra)
        cfg["env"]["production"] = prod
    return cfg


def write_config(cfg):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "wrangler.jsonc")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg))
    return p


def write_allowlist(entries=()):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "allowlist.txt")
    with open(p, "w", encoding="utf-8") as f:
        for path, reason in entries:
            f.write("%s\t%s\n" % (path, reason))
    return p


def run_check(cfg_path, allow_entries=()):
    # Drives cmd_check through argparse the way a shell would, capturing
    # exit codes and both streams. The valued-var test inspects the streams.
    allow = write_allowlist(allow_entries)
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        try:
            parity.main(["check", "--config", cfg_path, "--allowlist", allow])
            code = 0
        except SystemExit as e:
            code = e.code
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue(), err.getvalue()


class TestParity(unittest.TestCase):
    def test_identical_surfaces_exit_0(self):
        code, out, _ = run_check(write_config(make_config()))
        self.assertEqual(code, 0, out)

    def test_extra_staging_var_is_drift_exit_1(self):
        cfg = make_config(staging_vars={"RELEASE_SHA": "", "NEW_FLAG": ""})
        code, out, _ = run_check(write_config(cfg))
        self.assertEqual(code, 1, out)
        self.assertIn("vars.NEW_FLAG", out)

    def test_extra_prod_binding_is_drift_exit_1(self):
        cfg = make_config(top_extra={"d1_databases": [{"binding": "DB"}]})
        code, out, _ = run_check(write_config(cfg))
        self.assertEqual(code, 1, out)
        self.assertIn("bindings.d1_databases", out)

    def test_ingress_divergence_is_drift_exit_1(self):
        cfg = make_config(staging_extra={"workers_dev": True})
        code, out, _ = run_check(write_config(cfg))
        self.assertEqual(code, 1, out)
        self.assertIn("ingress.workers_dev", out)

    def test_equal_worker_names_are_drift(self):
        # The worker name is semantic: equal names are an incident (a
        # staging deploy would land on prod). worker.name is compared
        # NOWHERE, so differing names — the required state — never fire.
        cfg = make_config(staging_extra={"name": "kofra-prod"})
        code, out, _ = run_check(write_config(cfg))
        self.assertEqual(code, 1, out)
        self.assertIn("worker.name", out)

    def test_worker_name_allowlist_entry_refuses_exit_2(self):
        # Naming worker.name in the allowlist must REFUSE, not silence:
        # honouring it would be a standing permission for staging to aim
        # at prod. A key-by-key diff with an allowlist escape hatch
        # reports this as ALLOWED and exits 0.
        cfg = make_config(staging_extra={"name": "kofra-prod"})
        code, out, err = run_check(
            write_config(cfg), [("worker.name", "someone asked nicely")])
        self.assertEqual(code, 2, out + err)
        self.assertNotIn("ALLOWED worker.name", out)

    def test_env_level_main_override_is_compared(self):
        # Inheritance is a fallback, not a mask: a staging env that DOES
        # set main to something else must fire on entry.main.
        cfg = make_config(staging_extra={"main": "src/other.ts"})
        code, out, _ = run_check(write_config(cfg))
        self.assertEqual(code, 1, out)
        self.assertIn("entry.main", out)

    def test_valued_var_fires_without_printing_the_value(self):
        # A checked-in value is drift, named by var name alone. The value
        # string must appear in NO stream: printing it trains operators to
        # paste real secrets into config.
        secret = "sk-live-NOT-A-REAL-SECRET-12345"
        for stream_vars in (
                {"staging_vars": {"RELEASE_SHA": secret}},
                {"top_vars": {"RELEASE_SHA": secret}}):
            cfg = make_config(**stream_vars)
            code, out, err = run_check(write_config(cfg))
            self.assertEqual(code, 1, out)
            self.assertIn("vars.RELEASE_SHA", out)
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, err)

    def test_allowlisted_path_reports_allowed_and_exits_0(self):
        cfg = make_config(staging_extra={"workers_dev": True})
        code, out, _ = run_check(
            write_config(cfg),
            [("ingress.workers_dev", "case_db6cb3f2 baseline: preview ingress review")])
        self.assertEqual(code, 0, out)
        self.assertIn("ALLOWED ingress.workers_dev", out)

    def test_stale_allowlist_entry_refuses_exit_2(self):
        # The covered divergence is fixed but the entry stays: a hole
        # waiting for reuse. Exiting 0 here is the silent-green failure.
        code, out, err = run_check(
            write_config(make_config()),
            [("ingress.workers_dev", "fixed last week, entry left behind")])
        self.assertEqual(code, 2, out + err)

    def test_reasonless_allowlist_entry_refuses_exit_2(self):
        d = tempfile.mkdtemp()
        allow = os.path.join(d, "allowlist.txt")
        with open(allow, "w", encoding="utf-8") as f:
            f.write("ingress.workers_dev\n")
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            try:
                parity.main(["check", "--config", write_config(make_config()),
                             "--allowlist", allow])
                code = 0
            except SystemExit as e:
                code = e.code
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 2, out.getvalue() + err.getvalue())

    def test_explicit_prod_env_block_is_the_prod_side(self):
        # With env.production present, a top-level-only drift source must
        # NOT fire; a divergence against env.production must.
        cfg = make_config(with_prod_env=True,
                          top_extra={"compatibility_date": "1999-01-01"})
        code, out, _ = run_check(write_config(cfg))
        self.assertEqual(code, 0, out)
        cfg2 = make_config(with_prod_env=True,
                           prod_env_extra={"compatibility_date": "1999-01-01"})
        code2, out2, _ = run_check(write_config(cfg2))
        self.assertEqual(code2, 1, out2)
        self.assertIn("compat.date", out2)

    def test_missing_staging_env_refuses_exit_2(self):
        cfg = dict(BASE)
        code, out, err = run_check(write_config(cfg))
        self.assertEqual(code, 2, out + err)

    def test_unparseable_config_refuses_exit_2(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "wrangler.jsonc")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ not json, // broken")
        code, out, err = run_check(p)
        self.assertEqual(code, 2, out + err)

    def test_jsonc_comments_and_urls_parse(self):
        # The string-aware strip must survive // inside URLs: a regex strip
        # turns "https://..." into "https:" and blames parsing.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "wrangler.jsonc")
        cfg = make_config()
        text = ("// leading comment\n" + json.dumps(cfg)
                + '\n/* trailing {"url": "https://example.com/x"} */\n')
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        code, out, _ = run_check(p)
        self.assertEqual(code, 0, out)

    def test_script_runs_as_a_process(self):
        # The suite above drives functions in-process; this pins that the
        # file is executable end-to-end with exit codes intact through a
        # real argv boundary.
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "kofra_preview_parity.py")
        cfg = make_config(staging_vars={"RELEASE_SHA": "", "EXTRA": ""})
        cp, ap = write_config(cfg), write_allowlist()
        for args, want in ((["check", "--config", cp, "--allowlist", ap], 1),
                           (["surface", "--config", cp, "--allowlist", ap], 0)):
            try:
                r = subprocess.run([sys.executable, script] + args,
                                   capture_output=True, text=True, timeout=30)
            except OSError as e:
                self.fail("cannot execute script as a process: %s" % e)
            self.assertEqual(r.returncode, want, r.stdout + r.stderr)
        r = subprocess.run(
            [sys.executable, script, "surface", "--config", cp,
             "--allowlist", ap],
            capture_output=True, text=True, timeout=30)
        self.assertIn("staging\tvars.EXTRA\t", r.stdout)
        self.assertNotIn("sk-live", r.stdout + r.stderr)


def run_cli(args):
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        try:
            parity.main(args)
            code = 0
        except SystemExit as e:
            code = e.code
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue(), err.getvalue()


class TestJsonRegressions(unittest.TestCase):
    # Builder-owned pins for the preview-parity JSON envelope. The full
    # envelope contract lives in test_kofra_preview_parity_json.py (paired
    # acceptance, never edited here); these pin the three properties this
    # suite already cares about: exit codes stay the interface, values
    # never leak, and text mode stays untouched.

    def test_json_verdict_tracks_exit_code(self):
        # parity->0, drift->1, refused->2: the envelope never disagrees
        # with the exit code a caller gates on.
        parity_cfg = write_config(make_config())
        drift_cfg = write_config(
            make_config(staging_extra={"workers_dev": True}))
        allow = write_allowlist()
        for cfg_path, want_code, want_verdict in (
                (parity_cfg, 0, "parity"), (drift_cfg, 1, "drift")):
            code, out, err = run_cli(
                ["check", "--config", cfg_path, "--allowlist", allow,
                 "--json"])
            self.assertEqual(code, want_code, out + err)
            env = json.loads(out)
            self.assertEqual(env["verdict"], want_verdict)
            self.assertEqual(env["schema_version"], 1)
        code, out, err = run_cli(
            ["check", "--config", drift_cfg, "--allowlist", allow, "--json"])
        self.assertEqual(code, 1, out + err)
        self.assertEqual(json.loads(out)["summary"]["drift"], 1)

    def test_json_never_carries_checked_in_values(self):
        # A valued var is drift named by name alone — in JSON as in text.
        secret = "sk-live-BUILDER-REGRESSION-67890"
        cfg = make_config(staging_vars={"RELEASE_SHA": secret})
        code, out, err = run_cli(
            ["check", "--config", write_config(cfg),
             "--allowlist", write_allowlist(), "--json"])
        self.assertEqual(code, 1, out + err)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)
        env = json.loads(out)
        self.assertNotIn(secret, json.dumps(env))
        by_id = {r["id"]: r for r in env["rows"]}
        self.assertEqual(by_id["vars.RELEASE_SHA"]["staging"], '"present"')

    def test_dry_run_refused_on_both_subcommands(self):
        # Decided by the acceptance card: no plan mode. An exit-0 path
        # that measured nothing would be a gating lie.
        cfg = write_config(make_config())
        allow = write_allowlist()
        for sub in ("check", "surface"):
            code, out, err = run_cli(
                [sub, "--config", cfg, "--allowlist", allow, "--dry-run"])
            self.assertEqual(code, 2, out + err)
            self.assertNotIn("PARITY", out)
            self.assertNotIn("schema_version", out)
            self.assertIn("--dry-run", err)
            self.assertIn("usage", err.lower())

    def test_text_mode_has_no_envelope(self):
        # Byte-stability companion: without --json, neither stream gains
        # envelope keys — callers gating on exit codes see no new surface.
        for cfg, want_code in (
                (make_config(), 0),
                (make_config(staging_extra={"workers_dev": True}), 1)):
            with self.subTest(want_code=want_code):
                code, out, err = run_check(write_config(cfg))
                self.assertEqual(code, want_code, out + err)
                self.assertNotIn("schema_version", out + err)
                self.assertNotIn('"verdict"', out + err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
