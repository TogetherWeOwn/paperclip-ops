#!/usr/bin/env python3
# ===========================================================================
# test_kofra_preview_parity_json.py — acceptance tests for the preview-parity
# versioned JSON verdict envelope (paired acceptance tests, roadmap M11.3).
#
# Paired with the build card. The Builder rebases onto this card's
# handoff SHA and MUST NEVER EDIT THIS FILE; CI wiring (.github/workflows)
# and the implementation land in the build PR, where the orphan-suite
# ratchet runs against the wired head. This branch never merges standalone.
#
# WHAT THIS PINS. `kofra_preview_parity.py check --json` emits a versioned
# envelope agents can parse instead of scraping the TSV/prose the tool
# forbids callers to rely on. Text mode and exit codes stay byte-identical.
#
# THE ENVELOPE (exact contract; key sets are closed — no extras):
#
#   {
#     "schema_version": 1,                    # strict int, not bool
#     "tool": "kofra_preview_parity.py",
#     "command": "check",
#     "verdict": "parity" | "drift" | "refused",
#     "prod_side": "top-level (no env.production block)" | "env.production",
#     "rows": [                               # sorted by id, each id once
#       {"id": "<surface-path>",               # stable ID = surface path
#        "staging": <canon>|null, "prod": <canon>|null,
#        "verdict": "match"|"drift"|"allowed"|"refused"}
#     ],
#     "semantic_gates": [                     # worker name + valued vars
#       {"id": "worker.name"|"vars.<NAME>",
#        "outcome": "pass"|"drift", "detail": "<non-empty, redacted>"}
#     ],
#     "summary": {"match": n, "drift": n, "allowed": n, "refused": n,
#                 "semantic_drift": n},        # strict ints, tallies rows/gates
#     "error": null | {"code": <stable>, "retryable": bool,
#                      "next_action": "<non-empty>"}
#   }
#
# ROW COVERAGE. Rows are exactly the union of both flattened surfaces minus
# `worker.name` (compared nowhere — differing names are the required state),
# each exactly once, sorted by id. Side values are the canonical surface
# strings (identical bytes to the `surface` TSV column) or null when the
# path is absent on that side. A row verdict of "refused" means an
# allowlist entry naming that path was refused: a stale entry rewrites its
# path's row to "refused"; a NEVER_ALLOWLISTED entry (`worker.name`)
# synthesizes a row carrying both worker names with verdict "refused".
#
# REFUSAL ENVELOPES. Exit-2 runs STILL print a parseable envelope on stdout
# (verdict "refused", error populated) — a gating agent must never have to
# scrape the REFUSED prose to learn the code. Parse-failure envelopes carry
# empty rows/gates and zeroed summaries.
#
# STABLE ERROR CODES (exact strings):
#   config_unreadable, config_unparseable, config_missing_staging,
#   allowlist_unreadable, allowlist_malformed, allowlist_forbidden_path,
#   allowlist_stale_entry
#
# VERDICT <-> EXIT: parity->0, drift->1, refused->2. `error` is null unless
# the verdict is "refused". `retryable` is false for every fixture refusal
# (all are file-content problems, never transient).
#
# REDACTION. Checked-in var VALUES never appear in JSON bytes: var rows
# carry exactly '"present"' (never the value), gates name the var only, and
# every JSON byte run is scanned for fixture sentinels.
#
# --dry-run DECISION (this card decides, per the roadmap): REFUSED
# explicitly. Any exit-0 output from this tool means "parity measured"; a
# plan mode would create an exit-0 path that measured nothing, directly
# contradicting "EXIT CODES are the interface" and inviting gating misuse.
# The offline equivalent already exists (`--config` reads one file for
# pre-checks). So `check --dry-run` exits 2, names --dry-run and the
# refusal on stderr with usage, and prints no verdict or JSON on stdout.
# `--help` documents the refusal. Unknown flags refuse with usage + exit 2.
#
# TEXT-MODE GOLDENS. The golden tests below pin CURRENT text bytes (tmp
# paths normalized to <CONFIG>/<ALLOWLIST>). Goldens are change detectors
# by construction — here that is the contracted behavior ("byte-identical
# when --json is absent"), and the break they catch is the Builder
# accidentally altering text output. The malformed-config golden pins only
# the stable prefix: the remainder is stdlib wording that varies by Python
# version. `surface --json` is deliberately untested (out of scope).
#
# Deterministic: temp-dir configs, no network, no clock, no credentials.
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

TOP_LEVEL_PROD = "top-level (no env.production block)"
ENV_PROD = "env.production"

STAGING_SENTINEL = "sk-live-PARITYJSON-SENTINEL-7f3a9c1e"
TOP_SENTINEL = "ghs_PARITYJSON-SENTINEL-9d2b4e55"

TOP_KEYS = {"schema_version", "tool", "command", "verdict", "prod_side",
            "rows", "semantic_gates", "summary", "error"}
ROW_KEYS = {"id", "staging", "prod", "verdict"}
GATE_KEYS = {"id", "outcome", "detail"}
SUMMARY_KEYS = {"match", "drift", "allowed", "refused", "semantic_drift"}
ERROR_KEYS = {"code", "retryable", "next_action"}

REFUSAL_CODES = {"config_unreadable", "config_unparseable",
                 "config_missing_staging", "allowlist_unreadable",
                 "allowlist_malformed", "allowlist_forbidden_path",
                 "allowlist_stale_entry"}

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


def write_file(content, name="wrangler.jsonc"):
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def write_config(cfg):
    return write_file(json.dumps(cfg))


def write_allowlist(entries=()):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "allowlist.txt")
    with open(p, "w", encoding="utf-8") as f:
        for path, reason in entries:
            f.write("%s\t%s\n" % (path, reason))
    return p


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


def run_check_json(cfg_path, allow_entries=()):
    allow = write_allowlist(allow_entries)
    code, out, err = run_cli(
        ["check", "--config", cfg_path, "--allowlist", allow, "--json"])
    return code, out, err


def run_check_text(cfg_path, allow_entries=(), raw_allowlist=None):
    allow = raw_allowlist or write_allowlist(allow_entries)
    code, out, err = run_cli(
        ["check", "--config", cfg_path, "--allowlist", allow])
    return code, out, err, cfg_path, allow


def normalize(text, cfg_path, allow_path):
    return text.replace(cfg_path, "<CONFIG>").replace(allow_path, "<ALLOWLIST>")


def surface_map(cfg_path):
    """{(side, path): canon-value} from the real `surface` subcommand."""
    allow = write_allowlist()
    code, out, err = run_cli(
        ["surface", "--config", cfg_path, "--allowlist", allow])
    assert code == 0, out + err
    values = {}
    for line in out.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        side, path, value = line.split("\t")
        if value == "HAS-CHECKED-IN-VALUE":
            continue
        values[(side, path)] = value
    return values


class JsonEnvelope(unittest.TestCase):
    maxDiff = 4000

    def assertStrictInt(self, value, msg=""):
        self.assertIs(type(value), int, "%s: %r is not a strict int" % (msg, value))

    def assertEnvelopeShape(self, env):
        self.assertEqual(set(env.keys()), TOP_KEYS)
        self.assertStrictInt(env["schema_version"], "schema_version")
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["tool"], "kofra_preview_parity.py")
        self.assertEqual(env["command"], "check")
        self.assertIn(env["verdict"], ("parity", "drift", "refused"))
        self.assertIn(env["prod_side"], (TOP_LEVEL_PROD, ENV_PROD))
        self.assertIsInstance(env["rows"], list)
        self.assertIsInstance(env["semantic_gates"], list)
        for row in env["rows"]:
            self.assertEqual(set(row.keys()), ROW_KEYS, row)
            self.assertIsInstance(row["id"], str)
            self.assertTrue(row["id"], "empty row id")
            for side in ("staging", "prod"):
                self.assertTrue(row[side] is None
                                or isinstance(row[side], str), row)
            self.assertIn(row["verdict"],
                          ("match", "drift", "allowed", "refused"), row)
        ids = [r["id"] for r in env["rows"]]
        self.assertEqual(ids, sorted(ids), "rows not sorted by stable id")
        self.assertEqual(len(ids), len(set(ids)), "duplicate row id")
        for gate in env["semantic_gates"]:
            self.assertEqual(set(gate.keys()), GATE_KEYS, gate)
            self.assertIn(gate["outcome"], ("pass", "drift"), gate)
            self.assertIsInstance(gate["detail"], str)
            self.assertTrue(gate["detail"], "empty gate detail: %r" % gate)
        self.assertEqual(set(env["summary"].keys()), SUMMARY_KEYS)
        for key, value in env["summary"].items():
            self.assertStrictInt(value, "summary.%s" % key)
            self.assertGreaterEqual(value, 0)
        tallies = {"match": 0, "drift": 0, "allowed": 0, "refused": 0}
        for row in env["rows"]:
            tallies[row["verdict"]] += 1
        for key in tallies:
            self.assertEqual(env["summary"][key], tallies[key],
                             "summary.%s does not tally rows" % key)
        tripped = sum(1 for g in env["semantic_gates"]
                      if g["outcome"] == "drift")
        self.assertEqual(env["summary"]["semantic_drift"], tripped)
        if env["verdict"] == "refused":
            self.assertIsInstance(env["error"], dict)
            self.assertEqual(set(env["error"].keys()), ERROR_KEYS)
            self.assertIn(env["error"]["code"], REFUSAL_CODES)
            self.assertIs(env["error"]["retryable"], False)
            self.assertIsInstance(env["error"]["next_action"], str)
            self.assertTrue(env["error"]["next_action"].strip(),
                            "empty next_action")
        else:
            self.assertIsNone(env["error"])

    def assertNoSentinel(self, *blobs):
        for blob in blobs:
            self.assertNotIn(STAGING_SENTINEL, blob)
            self.assertNotIn(TOP_SENTINEL, blob)
            self.assertNotIn("SENTINEL", blob)

    def test_json_parity_envelope_schema_and_exit_0(self):
        code, out, err = run_check_json(write_config(make_config()))
        self.assertEqual(code, 0, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "parity")
        self.assertEqual(env["prod_side"], TOP_LEVEL_PROD)
        self.assertTrue(env["rows"], "parity envelope has no rows")
        for row in env["rows"]:
            self.assertEqual(row["verdict"], "match", row)
        self.assertEqual(env["summary"]["semantic_drift"], 0)
        gate_ids = {g["id"] for g in env["semantic_gates"]}
        self.assertIn("worker.name", gate_ids)

    def test_json_single_path_drift_row_values_and_exit_1(self):
        cfg_path = write_config(
            make_config(staging_extra={"workers_dev": True}))
        code, out, err = run_check_json(cfg_path)
        self.assertEqual(code, 1, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "drift")
        by_id = {r["id"]: r for r in env["rows"]}
        self.assertIn("ingress.workers_dev", by_id)
        row = by_id["ingress.workers_dev"]
        self.assertEqual(row["verdict"], "drift")
        # Hand-derived canon values: True staging, False prod.
        self.assertEqual(row["staging"], "true")
        self.assertEqual(row["prod"], "false")
        self.assertEqual(env["summary"]["drift"], 1)
        self.assertEqual(env["summary"]["allowed"], 0)
        self.assertEqual(env["summary"]["refused"], 0)

    def test_json_multi_path_drift_order_and_summary(self):
        cfg_path = write_config(make_config(
            staging_extra={"workers_dev": True, "main": "src/other.ts"},
            top_extra={"d1_databases": [{"binding": "DB"}]}))
        code, out, err = run_check_json(cfg_path)
        self.assertEqual(code, 1, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        by_id = {r["id"]: r for r in env["rows"]}
        for path in ("ingress.workers_dev", "entry.main",
                     "bindings.d1_databases"):
            self.assertIn(path, by_id)
            self.assertEqual(by_id[path]["verdict"], "drift", path)
        self.assertEqual(env["summary"]["drift"], 3)

    def test_json_allowlisted_row_verdict_allowed_exit_0(self):
        cfg_path = write_config(
            make_config(staging_extra={"workers_dev": True}))
        code, out, err = run_check_json(
            cfg_path, [("ingress.workers_dev",
                        "case_db6cb3f2 baseline: preview ingress review")])
        self.assertEqual(code, 0, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "parity")
        by_id = {r["id"]: r for r in env["rows"]}
        self.assertEqual(by_id["ingress.workers_dev"]["verdict"], "allowed")
        self.assertEqual(by_id["ingress.workers_dev"]["staging"], "true")
        self.assertEqual(by_id["ingress.workers_dev"]["prod"], "false")
        self.assertEqual(env["summary"]["allowed"], 1)
        self.assertEqual(env["summary"]["drift"], 0)

    def test_json_rows_cover_surface_union_with_surface_bytes(self):
        # Completeness: every compared surface path appears exactly once,
        # with the identical bytes `surface` prints — an agent scoping a
        # fix sees the whole surface, not just the drifted rows.
        cfg_path = write_config(make_config(
            staging_extra={"workers_dev": True},
            top_extra={"d1_databases": [{"binding": "DB"}]}))
        code, out, err = run_check_json(cfg_path)
        self.assertEqual(code, 1, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        surf = surface_map(cfg_path)
        paths = sorted({p for (_, p) in surf} - {"worker.name"})
        self.assertEqual([r["id"] for r in env["rows"]], paths)
        for row in env["rows"]:
            self.assertEqual(row["staging"],
                             surf.get(("staging", row["id"])), row["id"])
            self.assertEqual(row["prod"],
                             surf.get(("prod", row["id"])), row["id"])

    def test_json_worker_name_equality_is_drift_not_a_row(self):
        cfg_path = write_config(
            make_config(staging_extra={"name": "kofra-prod"}))
        code, out, err = run_check_json(cfg_path)
        self.assertEqual(code, 1, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "drift")
        self.assertNotIn("worker.name", [r["id"] for r in env["rows"]])
        gates = {g["id"]: g for g in env["semantic_gates"]}
        self.assertIn("worker.name", gates)
        self.assertEqual(gates["worker.name"]["outcome"], "drift")
        self.assertIn("worker.name", gates["worker.name"]["detail"])
        self.assertEqual(env["summary"]["semantic_drift"], 1)

    def test_json_worker_name_allowlist_entry_refuses_stable_code(self):
        cfg_path = write_config(
            make_config(staging_extra={"name": "kofra-prod"}))
        code, out, err = run_check_json(
            cfg_path, [("worker.name", "someone asked nicely")])
        self.assertEqual(code, 2, out + err)
        env = json.loads(out)  # refusal still parses: no prose scraping
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertEqual(env["error"]["code"], "allowlist_forbidden_path")
        by_id = {r["id"]: r for r in env["rows"]}
        self.assertIn("worker.name", by_id)
        self.assertEqual(by_id["worker.name"]["verdict"], "refused")
        self.assertEqual(by_id["worker.name"]["staging"], '"kofra-prod"')
        self.assertEqual(by_id["worker.name"]["prod"], '"kofra-prod"')
        self.assertEqual(env["summary"]["refused"], 1)
        self.assertIn("worker.name", env["error"]["next_action"])

    def test_json_stale_allowlist_entry_refuses_stable_code(self):
        cfg_path = write_config(make_config())
        code, out, err = run_check_json(
            cfg_path, [("ingress.workers_dev",
                        "fixed last week, entry left behind")])
        self.assertEqual(code, 2, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertEqual(env["error"]["code"], "allowlist_stale_entry")
        by_id = {r["id"]: r for r in env["rows"]}
        self.assertEqual(by_id["ingress.workers_dev"]["verdict"], "refused")
        self.assertIn("ingress.workers_dev", env["error"]["next_action"])

    def test_json_reasonless_allowlist_entry_refuses_stable_code(self):
        d = tempfile.mkdtemp()
        allow = os.path.join(d, "allowlist.txt")
        with open(allow, "w", encoding="utf-8") as f:
            f.write("ingress.workers_dev\n")
        code, out, err = run_cli(
            ["check", "--config", write_config(make_config()),
             "--allowlist", allow, "--json"])
        self.assertEqual(code, 2, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["error"]["code"], "allowlist_malformed")

    def test_json_malformed_config_refuses_empty_envelope(self):
        cfg_path = write_file("{ not json, // broken")
        code, out, err = run_check_json(cfg_path)
        self.assertEqual(code, 2, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertEqual(env["error"]["code"], "config_unparseable")
        self.assertEqual(env["rows"], [])
        self.assertEqual(env["semantic_gates"], [])

    def test_json_missing_staging_env_refuses_stable_code(self):
        cfg_path = write_config(dict(BASE))
        code, out, err = run_check_json(cfg_path)
        self.assertEqual(code, 2, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["error"]["code"], "config_missing_staging")

    def test_json_missing_config_file_refuses_stable_code(self):
        missing = os.path.join(tempfile.mkdtemp(), "no-such.jsonc")
        code, out, err = run_check_json(missing)
        self.assertEqual(code, 2, out + err)
        env = json.loads(out)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["error"]["code"], "config_unreadable")

    def test_json_valued_var_redacted_both_sides(self):
        for kwargs in ({"staging_vars": {"RELEASE_SHA": STAGING_SENTINEL}},
                       {"top_vars": {"RELEASE_SHA": TOP_SENTINEL}}):
            with self.subTest(kwargs=sorted(kwargs)):
                cfg_path = write_config(make_config(**kwargs))
                code, out, err = run_check_json(cfg_path)
                self.assertEqual(code, 1, out + err)
                self.assertNoSentinel(out, err)
                env = json.loads(out)
                self.assertEnvelopeShape(env)
                self.assertNoSentinel(json.dumps(env))
                by_id = {r["id"]: r for r in env["rows"]}
                # The row carries presence, never the value — exact bytes.
                self.assertEqual(by_id["vars.RELEASE_SHA"]["staging"],
                                 '"present"')
                self.assertEqual(by_id["vars.RELEASE_SHA"]["prod"],
                                 '"present"')
                gates = {g["id"]: g for g in env["semantic_gates"]}
                self.assertIn("vars.RELEASE_SHA", gates)
                self.assertEqual(gates["vars.RELEASE_SHA"]["outcome"],
                                 "drift")
                self.assertIn("RELEASE_SHA",
                              gates["vars.RELEASE_SHA"]["detail"])

    def test_json_bytes_deterministic_across_runs(self):
        cfg_path = write_config(make_config(
            staging_extra={"workers_dev": True}))
        first = run_check_json(cfg_path)
        second = run_check_json(cfg_path)
        self.assertEqual(first[0], 1)
        self.assertEqual(first[1], second[1])
        # Stdout is exactly one JSON document: no leading blank space and
        # at most one trailing newline (stderr is unpinned by design).
        normalized = first[1].strip()
        if first[1].endswith("\n"):
            normalized += "\n"
        self.assertEqual(first[1], normalized)

    def test_json_prod_side_label_switches_on_env_production(self):
        cfg_path = write_config(make_config(
            with_prod_env=True, top_extra={"compatibility_date": "1999-01-01"}))
        code, out, err = run_check_json(cfg_path)
        self.assertEqual(code, 0, out + err)
        env = json.loads(out)
        self.assertEqual(env["prod_side"], ENV_PROD)
        cfg2_path = write_config(make_config(
            with_prod_env=True,
            prod_env_extra={"compatibility_date": "1999-01-01"}))
        code2, out2, err2 = run_check_json(cfg2_path)
        self.assertEqual(code2, 1, out2 + err2)
        env2 = json.loads(out2)
        self.assertEqual(env2["prod_side"], ENV_PROD)
        by_id = {r["id"]: r for r in env2["rows"]}
        self.assertEqual(by_id["compat.date"]["verdict"], "drift")
        self.assertEqual(by_id["compat.date"]["prod"], '"1999-01-01"')

    def test_json_end_to_end_as_a_process(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "kofra_preview_parity.py")
        ok_cfg = write_config(make_config())
        drift_cfg = write_config(
            make_config(staging_extra={"workers_dev": True}))
        allow = write_allowlist()
        for cfg_path, want_code, want_verdict in (
                (ok_cfg, 0, "parity"), (drift_cfg, 1, "drift")):
            with self.subTest(want_verdict=want_verdict):
                try:
                    r = subprocess.run(
                        [sys.executable, script, "check", "--config", cfg_path,
                         "--allowlist", allow, "--json"],
                        capture_output=True, text=True, timeout=30)
                except OSError as e:
                    self.fail("cannot execute script as a process: %s" % e)
                self.assertEqual(r.returncode, want_code,
                                 r.stdout + r.stderr)
                env = json.loads(r.stdout)
                self.assertEqual(env["verdict"], want_verdict)
                self.assertNoSentinel(r.stdout, r.stderr)


class TextModeStability(unittest.TestCase):
    maxDiff = 4000

    GOLDENS = {
        # (stdout, stderr, exit) with tmp paths normalized; recorded from
        # the pre-change implementation. The Builder's change must
        # reproduce these bytes exactly when --json is absent.
        "parity": (
            "kofra_preview_parity.py: file <CONFIG>\n"
            "kofra_preview_parity.py: staging worker \"kofra-staging\" "
            ":: prod worker \"kofra-prod\" "
            "(top-level (no env.production block))\n"
            "kofra_preview_parity.py: PARITY OK\n",
            "",
            0),
        "single_drift": (
            "kofra_preview_parity.py: file <CONFIG>\n"
            "kofra_preview_parity.py: staging worker \"kofra-staging\" "
            ":: prod worker \"kofra-prod\" "
            "(top-level (no env.production block))\n"
            "DRIFT ingress.workers_dev :: staging=true prod=false\n"
            "kofra_preview_parity.py: DRIFT FOUND (1 item)\n",
            "",
            1),
        "allowed": (
            "kofra_preview_parity.py: file <CONFIG>\n"
            "kofra_preview_parity.py: staging worker \"kofra-staging\" "
            ":: prod worker \"kofra-prod\" "
            "(top-level (no env.production block))\n"
            "ALLOWED ingress.workers_dev\n"
            "kofra_preview_parity.py: PARITY OK\n",
            "",
            0),
        "worker_equality": (
            "kofra_preview_parity.py: file <CONFIG>\n"
            "kofra_preview_parity.py: staging worker \"kofra-prod\" "
            ":: prod worker \"kofra-prod\" "
            "(top-level (no env.production block))\n"
            "DRIFT worker.name :: staging and prod resolve to the same "
            "worker name — a staging deploy would land on prod\n"
            "kofra_preview_parity.py: DRIFT FOUND (1 item)\n",
            "",
            1),
        "worker_allowlist_refusal": (
            "",
            "REFUSED kofra_preview_parity.py :: allowlist <ALLOWLIST> "
            "line 1 names worker.name, which can never be allowlisted "
            "— equal worker names are an incident, not parity\n",
            2),
        "stale_refusal": (
            "",
            "REFUSED kofra_preview_parity.py :: allowlist entry "
            "ingress.workers_dev covers no divergence in this run "
            "— remove it rather than carrying a standing exemption\n",
            2),
    }

    MALFORMED_PREFIX = ("REFUSED kofra_preview_parity.py :: <CONFIG> "
                        "is not valid JSONC: ")

    def golden_configs(self):
        allow = "case_db6cb3f2 baseline: preview ingress review"
        return {
            "parity": (make_config(), []),
            "single_drift": (make_config(staging_extra={"workers_dev": True}),
                              []),
            "allowed": (make_config(staging_extra={"workers_dev": True}),
                        [("ingress.workers_dev", allow)]),
            "worker_equality": (make_config(staging_extra={"name": "kofra-prod"}),
                                []),
            "worker_allowlist_refusal": (
                make_config(staging_extra={"name": "kofra-prod"}),
                [("worker.name", "someone asked nicely")]),
            "stale_refusal": (
                make_config(),
                [("ingress.workers_dev", "fixed last week, entry left behind")]),
        }

    def test_text_mode_byte_identical_without_json_flag(self):
        for name, (cfg, entries) in self.golden_configs().items():
            with self.subTest(golden=name):
                cfg_path = write_config(cfg)
                code, out, err, cp, ap = run_check_text(cfg_path, entries)
                want_out, want_err, want_code = self.GOLDENS[name]
                self.assertEqual(code, want_code, out + err)
                self.assertEqual(normalize(out, cp, ap), want_out)
                self.assertEqual(normalize(err, cp, ap), want_err)

    def test_text_mode_malformed_config_refusal_prefix_stable(self):
        # The remainder after the prefix is stdlib JSON wording, which
        # varies by Python version — pin the stable prefix, require that
        # a reason follows, and require exit 2 with empty stdout.
        cfg_path = write_file("{ not json, // broken")
        code, out, err, cp, ap = run_check_text(cfg_path)
        self.assertEqual(code, 2, out + err)
        self.assertEqual(out, "")
        got = normalize(err, cp, ap)
        self.assertTrue(got.startswith(self.MALFORMED_PREFIX), got)
        self.assertTrue(got.endswith("\n") and
                        len(got) > len(self.MALFORMED_PREFIX) + 1, got)

    def test_text_mode_carries_no_envelope(self):
        for name, (cfg, entries) in self.golden_configs().items():
            with self.subTest(golden=name):
                code, out, err, _, _ = run_check_text(
                    write_config(cfg), entries)
                self.assertNotIn("schema_version", out + err)


class CliContract(unittest.TestCase):
    def test_dry_run_is_refused_with_usage_and_exit_2(self):
        # DECIDED (see header): no plan mode. The break this catches is a
        # future --dry-run that exits 0 without measuring — an exit-0 path
        # callers could mistake for measured parity.
        cfg_path = write_config(make_config())
        code, out, err = run_cli(
            ["check", "--config", cfg_path,
             "--allowlist", write_allowlist(), "--dry-run"])
        self.assertEqual(code, 2, out + err)
        self.assertNotIn("PARITY", out)
        self.assertNotIn("DRIFT", out)
        self.assertNotIn("schema_version", out)
        self.assertIn("--dry-run", err)
        self.assertIn("refus", err.lower())
        self.assertIn("usage", err.lower())

    def test_surface_dry_run_also_refuses(self):
        cfg_path = write_config(make_config())
        code, out, err = run_cli(
            ["surface", "--config", cfg_path,
             "--allowlist", write_allowlist(), "--dry-run"])
        self.assertEqual(code, 2, out + err)

    def test_unknown_flag_refuses_with_usage_and_exit_2(self):
        cfg_path = write_config(make_config())
        code, out, err = run_cli(
            ["check", "--config", cfg_path,
             "--allowlist", write_allowlist(), "--bogus-flag"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("usage", err.lower())

    def test_help_documents_json_and_dry_run(self):
        code, out, err = run_cli(["check", "--help"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("--json", out)
        self.assertIn("--dry-run", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
