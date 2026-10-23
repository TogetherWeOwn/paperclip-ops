#!/usr/bin/env python3
"""Immutable acceptance contract: hook_spawn_guard JSON verdict envelope.

IMMUTABLE: the Builder starts from this pushed branch/commit and MUST NOT
edit this file. Corrections return to the Test Author card. No standalone
test-only PR: this branch is handed to the paired build card, which wires
this suite into CI (add a ``run:`` step next to the existing
``./test_hook_spawn_guard.sh`` step in ``.github/workflows/ci.yml``) and
implements ``--format json`` until this suite is green.

Contract under test (``hook_spawn_guard.sh check``):

- ``--format text|json``, default ``text``. Default output is byte-identical
  to today (``FINDING <rule> <detail>`` lines on stdout, ``N finding(s)`` on
  stderr, ``hook_spawn_guard.sh: clean`` on stdout); ``--format text`` is
  byte-identical to the default (stdout + stderr + exit).
- ``--format json`` prints exactly ONE JSON object on stdout. Exit codes are
  unchanged in both formats: 0 clean, 2 refused, 3 finding.
- Envelope top-level keys, exactly::

      schema_version  strict int == 1
      tool            "hook_spawn_guard"
      command         "check"
      verdict         "clean" | "finding" | "refused"
      rows            [ {rule, location, detail, next_action} ... ] (exact keys)
      summary         {findings, detached_spawn, missing_timeout} (exact keys)
      error           null, or {code, retryable, next_action} (exact keys)
      next_action     "" when clean, non-empty otherwise

- ``verdict`` maps to exits clean->0, finding->3, refused->2.
- Row ``rule`` is a stable id: ``detached-spawn`` | ``missing-timeout``.
  ``location`` names the hook event for command/timeout findings and the
  body path for body findings. ``detail`` names the matched shape.
  Every row ``next_action`` is non-empty remediation.
- ``summary`` counts are consistent: findings == len(rows) ==
  detached_spawn + missing_timeout, and the per-rule counts match the rows.
- Refusals stay refusals: unreadable/unparseable settings and a
  missing/not-a-dir/unreadable hooks dir exit 2 with ``verdict`` refused,
  ``rows`` [], and a non-null ``error`` (stable ``code``, ``retryable``
  false, non-empty ``next_action``) -- never clean-with-zero-findings.
  Error codes: ``settings_unreadable``, ``settings_unparseable``,
  ``hooks_dir_unreadable``. A refusal wins over findings (fail closed).
- Text and JSON agree on every fixture: same exit, same rules in the same
  order as the text ``FINDING`` lines. No rule-semantics change.
- Disclosure: no body bytes beyond the existing finding detail are echoed
  (a canary line in a hook body appears nowhere); long commands stay
  truncated as in text mode. Commands carrying quotes/backslashes still
  yield parseable JSON.
- Unknown flags, an invalid ``--format`` value, and missing/invalid
  required args refuse with usage on stderr + exit 2.

Synthetic fixtures only. Fully offline: temp dirs, no network, no
credentials, no reads outside the fixtures (plus the guard script itself).

Runnable commands (from repo root)::

  python3 -B -m unittest -v test_hook_spawn_guard_json
  ./test_hook_spawn_guard.sh   # regression control, stays green

Red baseline (current source has no ``--format`` flag and no hooks-dir
readability check): every JSON test FAILS here (exit 2 usage / unparseable
stdout), and the unreadable-hooks-dir text assertions fail too. Text
goldens, usage refusals, and the regression control PASS. Do not weaken
asserts to make red pass.
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("HOOK_SPAWN_GUARD_SH", HERE / "hook_spawn_guard.sh"))
EXISTING_SUITE = HERE / "test_hook_spawn_guard.sh"

EXPECTED_SCHEMA_VERSION = 1
EXPECTED_TOOL = "hook_spawn_guard"
EXPECTED_COMMAND = "check"

VERDICTS = ("clean", "finding", "refused")
RULES = ("detached-spawn", "missing-timeout")

TOP_LEVEL_KEYS = frozenset({
    "schema_version", "tool", "command", "verdict",
    "rows", "summary", "error", "next_action",
})
ROW_KEYS = frozenset({"rule", "location", "detail", "next_action"})
SUMMARY_KEYS = frozenset({"findings", "detached_spawn", "missing_timeout"})
ERROR_KEYS = frozenset({"code", "retryable", "next_action"})
ERROR_CODES = frozenset({
    "settings_unreadable",
    "settings_unparseable",
    "hooks_dir_unreadable",
})

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_FINDING = 3

VERDICT_EXIT = {"clean": EXIT_OK, "finding": EXIT_FINDING, "refused": EXIT_REFUSED}

CANARY = "CANARY-HOOK-GUARD-NEVER-ECHO-7f2c9a41d0"

IS_ROOT = os.geteuid() == 0 if hasattr(os, "geteuid") else False


def _requires_non_root(reason):
    return unittest.skipIf(IS_ROOT, reason + " (root bypasses permission bits)")


class GuardHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hook-guard-json-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hooks_dir = self.root / "hooks"
        self.hooks_dir.mkdir()
        self.assertTrue(GUARD.is_file(), f"guard not found at {GUARD}")
        self.assertTrue(os.access(GUARD, os.X_OK), f"guard not executable: {GUARD}")

    # -- fixture helpers (synthetic only) ---------------------------------
    def write_body(self, name, content):
        p = self.hooks_dir / name
        p.write_text(content)
        return p

    def write_settings(self, entries, name="settings.json"):
        """entries: list of (event, command, timeout) tuples.

        timeout may be an int, None (JSON null), the string "MISSING" (key
        omitted), or any other JSON value to pin shape handling.
        """
        grouped = {}
        for event, command, timeout in entries:
            hook = {"type": "command", "command": command}
            if timeout != "MISSING":
                hook["timeout"] = timeout
            grouped.setdefault(event, []).append({"hooks": [hook]})
        doc = {"hooks": grouped}
        p = self.root / name
        p.write_text(json.dumps(doc))
        return p

    def run_guard(self, *args, cwd=None, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [str(GUARD), *args],
            cwd=str(cwd or self.root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

    def run_text(self, settings, hooks_dir=None, fmt=None):
        args = ["check", "--settings", str(settings)]
        if hooks_dir is not None:
            args += ["--hooks-dir", str(hooks_dir)]
        if fmt is not None:
            args += ["--format", fmt]
        return self.run_guard(*args)

    def run_json(self, settings, hooks_dir=None):
        proc = self.run_text(settings, hooks_dir, fmt="json")
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"JSON mode must never traceback (rc={proc.returncode}): {proc.stderr}",
        )
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            self.fail(
                f"JSON mode stdout must be exactly one JSON object "
                f"(rc={proc.returncode}): {e}\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
            )
        return proc, env

    # -- envelope shape ----------------------------------------------------
    def assertEnvelopeShape(self, env):
        self.assertIsInstance(env, dict, "envelope must be a JSON object")
        self.assertEqual(
            set(env.keys()), set(TOP_LEVEL_KEYS),
            f"envelope top-level keys must be exactly {sorted(TOP_LEVEL_KEYS)}",
        )
        self.assertIs(type(env["schema_version"]), int,
                        "schema_version must be a strict int")
        self.assertEqual(env["schema_version"], EXPECTED_SCHEMA_VERSION)
        self.assertEqual(env["tool"], EXPECTED_TOOL)
        self.assertEqual(env["command"], EXPECTED_COMMAND)
        self.assertIn(env["verdict"], VERDICTS)
        self.assertIsInstance(env["rows"], list)
        for row in env["rows"]:
            self.assertIsInstance(row, dict)
            self.assertEqual(set(row.keys()), set(ROW_KEYS),
                             f"row keys must be exactly {sorted(ROW_KEYS)}: {row}")
            self.assertIn(row["rule"], RULES)
            for key in ("location", "detail", "next_action"):
                self.assertIsInstance(row[key], str)
                self.assertTrue(row[key], f"row {key} must be non-empty: {row}")
            self.assertTrue(row["next_action"].strip(),
                            f"row next_action must be remediation, not blank: {row}")
        summary = env["summary"]
        self.assertIsInstance(summary, dict)
        self.assertEqual(set(summary.keys()), set(SUMMARY_KEYS),
                         f"summary keys must be exactly {sorted(SUMMARY_KEYS)}")
        for key in SUMMARY_KEYS:
            self.assertIs(type(summary[key]), int, f"summary.{key} must be int")
            self.assertGreaterEqual(summary[key], 0)
        self.assertEqual(summary["findings"], len(env["rows"]),
                         "summary.findings must equal len(rows)")
        by_rule = [r["rule"] for r in env["rows"]]
        self.assertEqual(summary["detached_spawn"], by_rule.count("detached-spawn"))
        self.assertEqual(summary["missing_timeout"], by_rule.count("missing-timeout"))
        self.assertEqual(summary["findings"],
                         summary["detached_spawn"] + summary["missing_timeout"])
        if env["verdict"] == "refused":
            self.assertEqual(env["rows"], [], "refused envelopes carry no rows")
            err = env["error"]
            self.assertIsInstance(err, dict, "refused envelopes carry an error object")
            self.assertEqual(set(err.keys()), set(ERROR_KEYS),
                             f"error keys must be exactly {sorted(ERROR_KEYS)}")
            self.assertIn(err["code"], ERROR_CODES)
            self.assertIs(err["retryable"], False, "refusals are not retryable")
            self.assertTrue(err["next_action"].strip(), "error next_action non-empty")
            self.assertTrue(env["next_action"].strip(),
                            "top-level next_action non-empty when refused")
        else:
            self.assertIsNone(env["error"], "error must be null unless refused")
            if env["verdict"] == "clean":
                self.assertEqual(env["rows"], [])
                self.assertEqual(summary["findings"], 0)
                self.assertEqual(env["next_action"], "",
                                 "top-level next_action is empty when clean")
            else:
                self.assertTrue(env["rows"], "finding verdict needs rows")
                self.assertTrue(env["next_action"].strip(),
                                "top-level next_action non-empty on findings")

    def assertVerdictExit(self, proc, env):
        self.assertEqual(proc.returncode, VERDICT_EXIT[env["verdict"]],
                         f"verdict {env['verdict']} must exit "
                         f"{VERDICT_EXIT[env['verdict']]}: rc={proc.returncode}\n"
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")

    def text_rules(self, proc):
        rules = []
        for line in proc.stdout.splitlines():
            if line.startswith("FINDING "):
                parts = line.split(" ", 2)
                self.assertGreaterEqual(len(parts), 3, f"malformed FINDING line: {line}")
                rules.append(parts[1])
        return rules

    # -- standard fixture bodies -------------------------------------------
    OOM_SH = """#!/usr/bin/env bash
tick() {
  setsid nohup bash "$HERE/sampler_run.sh" >/dev/null 2>>"$HERE/tick.log" &
  disown 2>/dev/null
}
tick
exit 0
"""
    PLAIN_CHAIN_SH = """#!/usr/bin/env bash
set -uo pipefail
command -v flock >/dev/null 2>&1 && check || fallback
result="$(probe 2>&1)" && report "$result" &> "$LOG"
exit 0
"""
    PROSE_ONLY_SH = """#!/usr/bin/env bash
# This hook deliberately does NOT setsid/nohup/disown anything: detaching
# children from a per-prompt hook leaks processes (case_ccf26154).
exit 0
"""
    REGEX_MJS = """#!/usr/bin/env node
const flagPattern = /(?:--model)(?:=|\\s+)("[^"]*"|[^\\s;&|()]+)/gi;
setTimeout(() => process.exit(0), 8000).unref();
"""
    DETACHED_MJS = """#!/usr/bin/env node
import { spawn } from "node:child_process";
spawn("node", ["worker.mjs"], { detached: true, stdio: "ignore" }).unref();
"""
    FORK_MJS = """#!/usr/bin/env node
import { fork } from "node:child_process";
fork("worker.mjs");
"""
    FORK_PY = """#!/usr/bin/env python3
import os
pid = os.fork()
if pid == 0:
    work()
"""
    SESSION_PY = """#!/usr/bin/env python3
import subprocess
subprocess.Popen(["worker"], start_new_session=True)
"""
    POPEN_PY = """#!/usr/bin/env python3
import subprocess
subprocess.Popen(["worker"])
"""

    # -- RED: detached-spawn via command -----------------------------------
    def test_json_detached_spawn_via_command(self):
        shapes = {
            "setsid": "setsid bash worker.sh",
            "nohup": "nohup bash worker.sh >/dev/null 2>&1",
            "disown": "bash worker.sh & disown",
            "bare_amp": "bash worker.sh >/dev/null 2>&1 &",
        }
        for shape, cmd in shapes.items():
            with self.subTest(shape=shape):
                settings = self.write_settings([("UserPromptSubmit", cmd, 5)])
                proc, env = self.run_json(settings)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "finding")
                self.assertVerdictExit(proc, env)
                self.assertEqual(len(env["rows"]), 1)
                row = env["rows"][0]
                self.assertEqual(row["rule"], "detached-spawn")
                self.assertIn("UserPromptSubmit", row["location"])
                self.assertIn("detach", row["detail"].lower())

    # -- RED: detached-spawn via body --------------------------------------
    def test_json_detached_spawn_via_sh_body(self):
        bodies = {
            "setsid_only": "#!/usr/bin/env bash\nsetsid bash worker.sh\n",
            "nohup_only": "#!/usr/bin/env bash\nnohup bash worker.sh >/dev/null 2>&1\n",
            "bare_amp_only": "#!/usr/bin/env bash\nbash worker.sh >/dev/null 2>&1 &\nwait\n",
            "disown_only": "#!/usr/bin/env bash\ndisown -a 2>/dev/null || true\n",
            "oom_shape": self.OOM_SH,
        }
        for shape, content in bodies.items():
            with self.subTest(shape=shape):
                body = self.write_body(f"hook-{shape}.sh", content)
                settings = self.write_settings(
                    [("UserPromptSubmit", f"bash {body}", 5)])
                proc, env = self.run_json(settings)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "finding")
                self.assertVerdictExit(proc, env)
                rules = [r["rule"] for r in env["rows"]]
                self.assertIn("detached-spawn", rules)
                body_rows = [r for r in env["rows"]
                             if r["rule"] == "detached-spawn"]
                self.assertTrue(
                    any(body.name in r["location"] for r in body_rows),
                    f"body finding location must name the body file: {body_rows}",
                )
                self.assertTrue(
                    all("detach" in r["detail"].lower() for r in body_rows),
                    f"body finding detail must name the detach shape: {body_rows}",
                )

    def test_json_detached_spawn_via_js_body(self):
        bodies = {"detached": self.DETACHED_MJS, "fork": self.FORK_MJS}
        for shape, content in bodies.items():
            with self.subTest(shape=shape):
                body = self.write_body(f"hook-{shape}.mjs", content)
                settings = self.write_settings(
                    [("UserPromptSubmit", f"node {body}", 10)])
                proc, env = self.run_json(settings)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "finding")
                self.assertVerdictExit(proc, env)
                rows = [r for r in env["rows"] if r["rule"] == "detached-spawn"]
                self.assertTrue(rows, f"js {shape} body must flag detached-spawn")
                self.assertIn(body.name, rows[0]["location"])
                if shape == "fork":
                    self.assertIn("fork", rows[0]["detail"].lower())

    def test_json_detached_spawn_via_py_body(self):
        bodies = {"os_fork": self.FORK_PY, "start_new_session": self.SESSION_PY}
        for shape, content in bodies.items():
            with self.subTest(shape=shape):
                body = self.write_body(f"hook-{shape}.py", content)
                settings = self.write_settings(
                    [("UserPromptSubmit", f"python3 {body}", 10)])
                proc, env = self.run_json(settings)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "finding")
                self.assertVerdictExit(proc, env)
                rows = [r for r in env["rows"] if r["rule"] == "detached-spawn"]
                self.assertTrue(rows, f"py {shape} body must flag detached-spawn")
                self.assertIn(body.name, rows[0]["location"])

    # -- RED: missing-timeout ----------------------------------------------
    def test_json_missing_timeout(self):
        body = self.write_body("plain.sh", self.PLAIN_CHAIN_SH)
        shapes = {"null": None, "absent": "MISSING", "string": "5"}
        for shape, timeout in shapes.items():
            with self.subTest(shape=shape):
                settings = self.write_settings(
                    [("UserPromptSubmit", f"bash {body}", timeout)])
                proc, env = self.run_json(settings)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "finding")
                self.assertVerdictExit(proc, env)
                self.assertEqual(len(env["rows"]), 1)
                row = env["rows"][0]
                self.assertEqual(row["rule"], "missing-timeout")
                self.assertIn("UserPromptSubmit", row["location"])
                self.assertIn("timeout", row["detail"].lower())
                self.assertIn("timeout", row["next_action"].lower())

    # -- RED: clean tree ----------------------------------------------------
    def test_json_clean_tree(self):
        chain = self.write_body("plain_chain.sh", self.PLAIN_CHAIN_SH)
        prose = self.write_body("prose_only.sh", self.PROSE_ONLY_SH)
        regex = self.write_body("regex_hook.mjs", self.REGEX_MJS)
        popen = self.write_body("plain_popen.py", self.POPEN_PY)
        settings = self.write_settings([
            ("UserPromptSubmit", f"bash {chain}", 5),
            ("UserPromptSubmit", f"bash {prose}", 5),
            ("UserPromptSubmit", f"node {regex}", 5),
            ("PreToolUse", f"python3 {popen}", 5),
        ])
        proc, env = self.run_json(settings)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "clean")
        self.assertVerdictExit(proc, env)
        self.assertNotIn("FINDING", proc.stdout)

    def test_json_settings_without_hooks_is_clean(self):
        """Pins existing semantics: no hooks object means nothing to check."""
        p = self.root / "no-hooks.json"
        p.write_text(json.dumps({"other": True}))
        proc, env = self.run_json(p)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "clean")
        self.assertVerdictExit(proc, env)

    # -- RED: refusals ------------------------------------------------------
    def test_json_missing_settings_file_refused(self):
        proc, env = self.run_json(self.root / "does-not-exist.json")
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertVerdictExit(proc, env)
        self.assertEqual(env["error"]["code"], "settings_unreadable")

    @_requires_non_root("unreadable-settings refusal needs permission bits")
    def test_json_unreadable_settings_file_refused(self):
        p = self.write_settings([("UserPromptSubmit", "bash run.sh", 5)])
        p.chmod(0)
        try:
            proc, env = self.run_json(p)
        finally:
            p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertVerdictExit(proc, env)
        self.assertEqual(env["error"]["code"], "settings_unreadable")

    def test_json_unparseable_settings_refused(self):
        p = self.root / "bad.json"
        p.write_text("not json")
        proc, env = self.run_json(p)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertVerdictExit(proc, env)
        self.assertEqual(env["error"]["code"], "settings_unparseable")

    def test_json_missing_hooks_dir_refused(self):
        settings = self.write_settings([("UserPromptSubmit", "bash run.sh", 5)])
        proc, env = self.run_json(settings, hooks_dir=self.root / "no-such-dir")
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertVerdictExit(proc, env)
        self.assertEqual(env["error"]["code"], "hooks_dir_unreadable")

    def test_json_file_as_hooks_dir_refused(self):
        settings = self.write_settings([("UserPromptSubmit", "bash run.sh", 5)])
        not_a_dir = self.root / "file-not-dir.txt"
        not_a_dir.write_text("x")
        proc, env = self.run_json(settings, hooks_dir=not_a_dir)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertVerdictExit(proc, env)
        self.assertEqual(env["error"]["code"], "hooks_dir_unreadable")

    @_requires_non_root("unreadable-hooks-dir refusal needs permission bits")
    def test_json_unreadable_hooks_dir_refused(self):
        """Unreadable hooks dir refuses even when findings exist (fail closed).

        A hooks dir the guard cannot read must never yield clean or a
        partial finding set: body findings would silently go missing.
        """
        settings = self.write_settings(
            [("UserPromptSubmit", "setsid bash worker.sh", 5)])
        self.hooks_dir.chmod(0)
        try:
            proc, env = self.run_json(settings, hooks_dir=self.hooks_dir)
            # Same refusal in text mode: exit 2 with the reason on stderr.
            text = self.run_text(settings, hooks_dir=self.hooks_dir)
        finally:
            self.hooks_dir.chmod(stat.S_IRWXU)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "refused")
        self.assertVerdictExit(proc, env)
        self.assertEqual(env["error"]["code"], "hooks_dir_unreadable")
        self.assertEqual(text.returncode, EXIT_REFUSED)
        self.assertTrue(text.stderr.strip(), "text refusal explains itself")

    # -- RED: exits in both formats -----------------------------------------
    def test_exit_codes_match_in_both_formats(self):
        body = self.write_body("plain.sh", self.PLAIN_CHAIN_SH)
        clean = self.write_settings([("UserPromptSubmit", f"bash {body}", 5)],
                                    name="clean.json")
        finding = self.write_settings(
            [("UserPromptSubmit", "setsid bash worker.sh", 5)],
            name="finding.json")
        refused = self.root / "does-not-exist.json"
        for name, settings, want_rc in (
            ("clean", clean, EXIT_OK),
            ("finding", finding, EXIT_FINDING),
            ("refused", refused, EXIT_REFUSED),
        ):
            with self.subTest(case=name, fmt="default-text"):
                proc = self.run_text(settings)
                self.assertEqual(proc.returncode, want_rc,
                                 f"text {name}: {proc.stdout} {proc.stderr}")
            with self.subTest(case=name, fmt="json"):
                proc, env = self.run_json(settings)
                self.assertEqual(proc.returncode, want_rc,
                                 f"json {name}: {proc.stdout} {proc.stderr}")
                self.assertVerdictExit(proc, env)

    # -- RED: text byte stability -------------------------------------------
    def test_format_text_is_byte_identical_to_default(self):
        body = self.write_body("plain.sh", self.PLAIN_CHAIN_SH)
        detaching = self.write_body("detaching.sh", self.OOM_SH)
        fixtures = {
            "finding": self.write_settings(
                [("UserPromptSubmit", f"bash {detaching}", 5)],
                name="finding.json"),
            "clean": self.write_settings(
                [("UserPromptSubmit", f"bash {body}", 5)],
                name="clean.json"),
            "refused": self.root / "does-not-exist.json",
        }
        for name, settings in fixtures.items():
            with self.subTest(case=name):
                default = self.run_text(settings)
                explicit = self.run_text(settings, fmt="text")
                self.assertEqual(explicit.stdout, default.stdout, "stdout bytes")
                self.assertEqual(explicit.stderr, default.stderr, "stderr bytes")
                self.assertEqual(explicit.returncode, default.returncode, "exit")

    # -- RED: determinism ----------------------------------------------------
    def test_json_deterministic_bytes(self):
        body = self.write_body("detaching.sh", self.OOM_SH)
        settings = self.write_settings([
            ("UserPromptSubmit", f"bash {body}", 5),
            ("UserPromptSubmit", "nohup bash worker.sh", None),
        ])
        first = self.run_text(settings, fmt="json")
        second = self.run_text(settings, fmt="json")
        self.assertEqual(first.returncode, EXIT_FINDING)
        self.assertEqual(second.stdout, first.stdout, "JSON bytes stable across runs")
        self.assertEqual(second.returncode, first.returncode)

    # -- RED: text/JSON parity ----------------------------------------------
    def test_text_json_verdict_parity(self):
        chain = self.write_body("plain_chain.sh", self.PLAIN_CHAIN_SH)
        detaching = self.write_body("detaching.sh", self.OOM_SH)
        forked = self.write_body("forker.mjs", self.FORK_MJS)
        fixtures = {
            "clean": [("UserPromptSubmit", f"bash {chain}", 5)],
            "command_detach": [("UserPromptSubmit", "setsid bash w.sh", 5)],
            "body_detach": [("UserPromptSubmit", f"bash {detaching}", 5)],
            "js_fork": [("UserPromptSubmit", f"node {forked}", 5)],
            "missing_timeout": [("UserPromptSubmit", f"bash {chain}", None)],
            "multi": [
                ("UserPromptSubmit", "nohup bash w.sh", None),
                ("PreToolUse", f"bash {detaching}", 5),
            ],
        }
        for name, entries in fixtures.items():
            with self.subTest(case=name):
                settings = self.write_settings(entries)
                text = self.run_text(settings)
                proc, env = self.run_json(settings)
                self.assertEnvelopeShape(env)
                self.assertEqual(proc.returncode, text.returncode,
                                 "JSON exit must equal text exit")
                self.assertEqual([r["rule"] for r in env["rows"]],
                                 self.text_rules(text),
                                 "JSON rows must match text FINDING lines in order")
                self.assertNotIn("FINDING", proc.stdout,
                                 "JSON stdout carries the envelope, not FINDING lines")

    # -- RED: negative controls stay clean in JSON ---------------------------
    def test_json_negative_controls_stay_clean(self):
        bodies = {
            "chain": ("plain.sh", self.PLAIN_CHAIN_SH, "bash"),
            "prose": ("prose.sh", self.PROSE_ONLY_SH, "bash"),
            "regex_unref": ("regex.mjs", self.REGEX_MJS, "node"),
            "plain_popen": ("popen.py", self.POPEN_PY, "python3"),
            "and_chain_cmd": (None, "check --x && report --y", None),
            "redirect_cmd": (None, "probe &> /tmp/out.log", None),
            "fd_dup_cmd": (None, 'result="$(probe 2>&1)"', None),
        }
        for shape, (fname, content, interp) in bodies.items():
            with self.subTest(shape=shape):
                if fname is None:
                    cmd = content
                else:
                    body = self.write_body(fname, content)
                    cmd = f"{interp} {body}"
                settings = self.write_settings([("UserPromptSubmit", cmd, 5)])
                proc, env = self.run_json(settings)
                self.assertEnvelopeShape(env)
                self.assertEqual(env["verdict"], "clean",
                                 f"{shape} must stay clean: {env['rows']}")
                self.assertVerdictExit(proc, env)

    # -- RED: hermetic hooks-dir lookup --------------------------------------
    def test_hooks_dir_basename_lookup(self):
        # The guard resolves the command's FIRST word under --hooks-dir, so
        # the hermetic fixture names the script bare (no interpreter word).
        body = self.write_body("detaching_tick.sh", self.OOM_SH)
        settings = self.write_settings(
            [("UserPromptSubmit", "detaching_tick.sh", 5)])
        # With --hooks-dir the bare basename resolves to the fixture body.
        proc, env = self.run_json(settings, hooks_dir=self.hooks_dir)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "finding")
        rows = [r for r in env["rows"] if r["rule"] == "detached-spawn"]
        self.assertTrue(rows, "basename lookup must find the fixture body")
        self.assertIn(body.name, rows[0]["location"])
        # Without --hooks-dir the same bare command has no body finding:
        # the guard reads no body it was not pointed at.
        proc2, env2 = self.run_json(settings)
        self.assertEnvelopeShape(env2)
        body_hits = [r for r in env2["rows"]
                     if r["rule"] == "detached-spawn" and body.name in r["location"]]
        self.assertEqual(body_hits, [],
                         "without --hooks-dir no body may be resolved")

    def test_no_reads_outside_fixtures(self):
        # A detaching body that lives OUTSIDE --hooks-dir is never read,
        # even when the command names its bare basename (the guard searches
        # only --hooks-dir for basenames -- not cwd, which is the fixture
        # root where the outsider sits).
        outsider = self.root / "outsider.sh"
        outsider.write_text(self.OOM_SH)
        settings = self.write_settings([("UserPromptSubmit", "outsider.sh", 5)],
                                       name="outsider.json")
        proc, env = self.run_json(settings, hooks_dir=self.hooks_dir)
        self.assertEnvelopeShape(env)
        body_hits = [r for r in env["rows"]
                     if r["rule"] == "detached-spawn"
                     and "outsider.sh" in r["location"]]
        self.assertEqual(body_hits, [],
                         "bodies outside --hooks-dir must not be read")
        # Results do not depend on HOME (no implicit fleet-settings read).
        chain = self.write_body("plain.sh", self.PLAIN_CHAIN_SH)
        clean = self.write_settings([("UserPromptSubmit", f"bash {chain}", 5)],
                                    name="clean.json")
        home_a = self.root / "home-a"
        home_b = self.root / "home-b"
        home_a.mkdir()
        home_b.mkdir()
        out_a = self.run_guard("check", "--settings", str(clean),
                               "--format", "json",
                               env_extra={"HOME": str(home_a)})
        out_b = self.run_guard("check", "--settings", str(clean),
                               "--format", "json",
                               env_extra={"HOME": str(home_b)})
        self.assertEqual(out_a.stdout, out_b.stdout, "HOME-independent bytes")
        self.assertEqual(out_a.returncode, out_b.returncode)

    # -- RED: disclosure -----------------------------------------------------
    def test_canary_body_bytes_never_echoed(self):
        body = self.write_body("detaching.sh", self.OOM_SH + f"\n# {CANARY}\n")
        settings = self.write_settings(
            [("UserPromptSubmit", f"bash {body}", 5)])
        text = self.run_text(settings)
        self.assertEqual(text.returncode, EXIT_FINDING)
        self.assertNotIn(CANARY, text.stdout + text.stderr,
                         "canary must not leak via text mode")
        proc, env = self.run_json(settings)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "finding")
        self.assertNotIn(CANARY, proc.stdout + proc.stderr,
                         "canary must not leak via JSON mode")

    def test_long_command_truncated_in_json(self):
        long_cmd = "setsid bash " + "w" * 200 + ".sh"
        settings = self.write_settings([("UserPromptSubmit", long_cmd, 5)])
        proc, env = self.run_json(settings)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "finding")
        tail = long_cmd[80:]
        self.assertTrue(tail, "fixture must exceed the 80-char text window")
        self.assertNotIn(tail, proc.stdout,
                         "JSON must not echo the command tail text truncates")

    def test_json_escapes_quotes_and_backslashes(self):
        cmd = 'setsid bash run.sh --label "nightly\\\\build"'
        settings = self.write_settings([("UserPromptSubmit", cmd, 5)])
        proc, env = self.run_json(settings)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "finding")
        row = env["rows"][0]
        self.assertEqual(row["rule"], "detached-spawn")
        self.assertIn("UserPromptSubmit", row["location"])

    # -- RED: multi-finding order + summary ----------------------------------
    def test_multi_finding_rows_order_and_summary(self):
        detaching = self.write_body("detaching.sh", self.OOM_SH)
        settings = self.write_settings([
            ("UserPromptSubmit", "nohup bash worker.sh", None),
            ("PreToolUse", f"bash {detaching}", 5),
        ])
        text = self.run_text(settings)
        proc, env = self.run_json(settings)
        self.assertEnvelopeShape(env)
        self.assertEqual(env["verdict"], "finding")
        self.assertVerdictExit(proc, env)
        self.assertGreaterEqual(len(env["rows"]), 2)
        self.assertEqual([r["rule"] for r in env["rows"]],
                         self.text_rules(text),
                         "row order follows text FINDING order")
        self.assertIn("detached-spawn", [r["rule"] for r in env["rows"]])
        self.assertIn("missing-timeout", [r["rule"] for r in env["rows"]])
        self.assertTrue(env["next_action"].strip())

    # -- RED: schema exactness across verdicts --------------------------------
    def test_envelope_schema_exact_keys(self):
        chain = self.write_body("plain.sh", self.PLAIN_CHAIN_SH)
        clean = self.write_settings([("UserPromptSubmit", f"bash {chain}", 5)],
                                    name="clean.json")
        finding = self.write_settings(
            [("UserPromptSubmit", "setsid bash worker.sh", 5)],
            name="finding.json")
        _, clean_env = self.run_json(clean)
        _, finding_env = self.run_json(finding)
        _, refused_env = self.run_json(self.root / "does-not-exist.json")
        for env in (clean_env, finding_env, refused_env):
            self.assertEnvelopeShape(env)
        self.assertEqual(clean_env["verdict"], "clean")
        self.assertEqual(finding_env["verdict"], "finding")
        self.assertEqual(refused_env["verdict"], "refused")

    # -- controls (green at HEAD): text goldens -------------------------------
    def test_text_clean_bytes_golden(self):
        body = self.write_body("plain.sh", self.PLAIN_CHAIN_SH)
        settings = self.write_settings([("UserPromptSubmit", f"bash {body}", 5)])
        proc = self.run_text(settings)
        self.assertEqual(proc.returncode, EXIT_OK)
        self.assertEqual(proc.stdout, "hook_spawn_guard.sh: clean\n")

    def test_text_finding_lines_shape(self):
        body = self.write_body("detaching.sh", self.OOM_SH)
        settings = self.write_settings(
            [("UserPromptSubmit", f"bash {body}", 5)])
        proc = self.run_text(settings)
        self.assertEqual(proc.returncode, EXIT_FINDING)
        lines = proc.stdout.splitlines()
        self.assertTrue(lines, "finding mode prints FINDING lines")
        for line in lines:
            self.assertTrue(line.startswith("FINDING "),
                            f"every stdout line is a FINDING line: {line}")
            rule = line.split(" ", 2)[1]
            self.assertIn(rule, RULES)
        self.assertIn("finding(s)", proc.stderr)

    def test_text_refusal_shape(self):
        proc = self.run_text(self.root / "does-not-exist.json")
        self.assertEqual(proc.returncode, EXIT_REFUSED)
        self.assertIn("not a file", proc.stderr)
        bad = self.root / "bad.json"
        bad.write_text("not json")
        proc2 = self.run_text(bad)
        self.assertEqual(proc2.returncode, EXIT_REFUSED)
        self.assertTrue(proc2.stderr.strip(), "refusal explains itself on stderr")

    def test_text_settings_without_hooks_is_clean(self):
        p = self.root / "no-hooks.json"
        p.write_text(json.dumps({"other": True}))
        proc = self.run_text(p)
        self.assertEqual(proc.returncode, EXIT_OK)
        self.assertEqual(proc.stdout, "hook_spawn_guard.sh: clean\n")

    # -- controls (green at HEAD): usage refusals ------------------------------
    def test_unknown_flags_refuse(self):
        settings = self.write_settings([("UserPromptSubmit", "bash run.sh", 5)])
        cases = [
            ["check", "--settings", str(settings), "--bogus"],
            ["check", "--settings", str(settings), "--format", "yaml"],
            ["check", "--settings", str(settings), "--format", "json", "--bogus"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                proc = self.run_guard(*argv)
                self.assertEqual(proc.returncode, EXIT_REFUSED,
                                 f"{argv}: {proc.stdout} {proc.stderr}")
                self.assertIn("usage:", proc.stderr)

    def test_missing_args_refuse(self):
        # NOTE: a trailing --settings/--hooks-dir with no value hangs the
        # current guard (shift past argc loops forever) -- a pre-existing
        # bug deliberately NOT pinned here (out of scope for this card; see
        # handoff). Only the new --format flag must terminate + refuse when
        # its value is missing.
        settings = self.write_settings([("UserPromptSubmit", "bash run.sh", 5)])
        cases = [
            [],  # no subcommand
            ["check"],  # no --settings
            ["check", "--settings", str(settings), "--format"],  # missing value
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                proc = self.run_guard(*argv)
                self.assertEqual(proc.returncode, EXIT_REFUSED,
                                 f"{argv}: {proc.stdout} {proc.stderr}")
                self.assertIn("usage:", proc.stderr)

    # -- regression control -----------------------------------------------------
    def test_regression_control_existing_suite_green(self):
        self.assertTrue(EXISTING_SUITE.is_file(), "existing suite must exist")
        proc = subprocess.run(
            [str(EXISTING_SUITE)],
            cwd=str(HERE),
            env={**os.environ, "HOOK_SPAWN_GUARD_SH": str(GUARD)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"existing suite must stay green:\n{proc.stdout}\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
