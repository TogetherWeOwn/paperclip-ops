#!/usr/bin/env python3
"""Acceptance tests for the offline structured verdicts of action_pins.py.

These tests are the frozen paired contract for the `--json` report. They run
the checker as a hermetic subprocess against temporary synthetic inputs only;
they never touch the shared checkout, the network, or `gh`.

Run: python3 -B -m unittest discover -s .github/scripts \\
       -p 'test_action_pins_json_acceptance.py'

Frozen contract: schema `action-pins.v1`
------------------------------------------
Invocation: `action_pins.py [--json] [PATH ...]`; `--json` may appear before
or after the paths. The in-process `main(argv)` convention (argv includes the
program name at index 0) and the existing public helper signatures are kept.

With `--json`, a *check* run writes exactly one JSON object to stdout (UTF-8,
parseable by `json.loads` on the whole of stdout; no annotations, prose or
tracebacks on stdout or stderr) and exits 0 (clean) or 1 (anything else).
The object has at least these fields; additive fields are allowed, these
meanings are frozen:

- `schema`: exactly `"action-pins.v1"`.
- `ok`: bool. True only when the scan is complete and there are no findings
  and no errors. Missing evidence is never `ok`.
- `scan_complete`: bool. True only when at least one input was requested and
  every requested input was read and checked. A completed scan may still
  have findings (`ok` false, `scan_complete` true).
- `requested`, `checked`, `refused`: ints with
  `requested == checked + refused`. Explicit mode counts the path arguments;
  default mode counts the discovered files. `refused` counts requested inputs
  that could not be read/checked (one `errors` entry each).
- `findings`: pin violations, reusing the existing `Finding` detection as the
  source of truth, sorted by `(path, line, code, value)`. Each has:
  `code` (exactly `"unpinned-action"` in v1), `path` (explicit paths echoed
  verbatim; discovered paths as discovered), `line` (1-based int), `value`
  (the offending `uses:` value, a string, possibly empty for a missing
  value), `message` (non-empty string), `retryable` (false), and
  `next_action` (exactly `"pin_to_full_sha_or_digest"`).
- `errors`: input/scan failures, sorted by `(path or "", code)`. Each has
  `code`, `path` (string, or null only for the scan-level
  `no-inputs-discovered` error), `message` (non-empty string), `retryable`
  (false: every v1 failure is a deterministic local condition) and
  `next_action`. Frozen code -> next_action mapping:
    `input-missing` -> `fix_input_path`
    `input-unreadable` -> `fix_input_permissions`
    `input-invalid-encoding` -> `fix_input_encoding`
    `no-inputs-discovered` -> `provide_input_files`
  `no-inputs-discovered` is scan-level: `requested == checked == refused ==
  0`, `scan_complete` false, `ok` false, exit 1. Known findings from inputs
  that were checked survive a partial input failure.
- `retryable`: false in v1 (no v1 outcome is fixed by re-running unchanged).
- `next_action`: `"none"` when `ok`; otherwise the `next_action` of the
  first error if any, else of the first finding. Deterministic because both
  lists are sorted. Repeated identical runs produce identical stdout bytes.

Usage boundaries (stated, not accidental): `--help`/`-h` print plain-text
help to stdout and exit 0 -- never JSON, even combined with `--json`. The
help text names `--json` and the schema id. An unknown option is a usage
error *outside* the schema: exit 2, usage text on stderr, empty stdout, no
JSON object and no traceback (argparse-style `SystemExit(2)` from `main` is
acceptable). The 0/1 exits above govern check verdicts only.

Without `--json` the existing annotation/prose behaviour, pin allow/deny
rules (full-length SHA, `./...` local actions, `docker://...@sha256:<digest>`
pass; tags, short SHAs and missing values fail) and default discovery
(`.github/workflows/*`, local action files) are unchanged, with the same
0/1 verdicts as JSON mode on the same inputs. No mode writes any file.

Round-two delta: count unique paths; directory enumeration failures are extra
input-unreadable errors, not refused files. Discovery must succeed for a scan
to be complete. Both annotation sites escape file properties separately from
messages. Concrete nested-flow, escaped-key and explicit-key fixtures below
require real uses mappings without treating run strings/scalars as mappings.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("action_pins.py")
SHA = "11d5960a326750d5838078e36cf38b85af677262"
DIGEST = "a" * 64

CLEAN = f"steps:\n  - uses: actions/checkout@{SHA} # v4\n"
UNPINNED = "steps:\n  - uses: actions/checkout@v4\n"  # finding on line 2

SCRUB_PREFIXES = ("GIT_",)
SCRUB_KEYS = {
    "BASH_ENV", "ENV", "CDPATH", "PYTHONPATH", "PYTHONHOME",
    "PYTHONSTARTUP", "PYTHONUSERBASE",
}


def clean_env():
    env = {
        k: v for k, v in os.environ.items()
        if k not in SCRUB_KEYS and not k.startswith(SCRUB_PREFIXES)
    }
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def run_cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=cwd, env=clean_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )


ASCII_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "PYTHONUTF8": "0",
    "PYTHONCOERCECLOCALE": "0",
}


def run_cli_with_env(args, cwd, extra_env):
    env = clean_env()
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )


def _unprivileged_ids():
    try:
        import pwd
        entry = pwd.getpwnam("nobody")
        return entry.pw_uid, entry.pw_gid
    except Exception:
        return 65534, 65534


def run_cli_as_nobody(script_path, args, cwd):
    uid, gid = _unprivileged_ids()
    return subprocess.run(
        [sys.executable, "-B", str(script_path), *args],
        cwd=cwd, env=clean_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        user=uid, group=gid,
    )


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def write(root, rel, content):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(Path(root).rglob("*")) if p.is_file()
    }


class JsonContract(unittest.TestCase):
    # -- shared assertions -------------------------------------------------
    def parse_json(self, proc):
        self.assertEqual(b"", proc.stderr)
        self.assertNotIn(b"Traceback", proc.stdout)
        self.assertNotIn(b"::error", proc.stdout)
        obj = json.loads(proc.stdout.decode("utf-8"))
        self.assertIsInstance(obj, dict)
        return obj

    def assert_common(self, obj, *, ok, scan_complete, requested, checked,
                      refused):
        for key in ("schema", "ok", "scan_complete", "requested", "checked",
                    "refused", "findings", "errors", "retryable",
                    "next_action"):
            self.assertIn(key, obj)
        self.assertEqual("action-pins.v1", obj["schema"])
        self.assertIs(ok, obj["ok"])
        self.assertIs(scan_complete, obj["scan_complete"])
        self.assertEqual(requested, obj["requested"])
        self.assertEqual(checked, obj["checked"])
        self.assertEqual(refused, obj["refused"])
        self.assertEqual(obj["requested"], obj["checked"] + obj["refused"])
        self.assertIsInstance(obj["findings"], list)
        self.assertIsInstance(obj["errors"], list)
        self.assertIs(False, obj["retryable"])
        if ok:
            self.assertEqual("none", obj["next_action"])
            self.assertEqual([], obj["findings"])
            self.assertEqual([], obj["errors"])

    def assert_finding(self, finding, *, path=None, line=None, exact_path=True):
        for key in ("code", "path", "line", "value", "message", "retryable",
                    "next_action"):
            self.assertIn(key, finding)
        self.assertEqual("unpinned-action", finding["code"])
        if path is not None:
            if exact_path:
                self.assertEqual(path, finding["path"])
            else:
                self.assertTrue(
                    finding["path"].replace("\\", "/").endswith(path),
                    finding["path"])
        if line is not None:
            self.assertEqual(line, finding["line"])
        self.assertIsInstance(finding["line"], int)
        self.assertGreaterEqual(finding["line"], 1)
        self.assertIsInstance(finding["value"], str)
        self.assertTrue(finding["message"])
        self.assertIs(False, finding["retryable"])
        self.assertEqual("pin_to_full_sha_or_digest", finding["next_action"])

    def assert_error(self, error, *, code, next_action, path="__any__"):
        for key in ("code", "path", "message", "retryable", "next_action"):
            self.assertIn(key, error)
        self.assertEqual(code, error["code"])
        self.assertEqual(next_action, error["next_action"])
        if path != "__any__":
            self.assertEqual(path, error["path"])
        self.assertTrue(error["message"])
        self.assertIs(False, error["retryable"])

    # -- help and usage boundaries -----------------------------------------
    def test_help_is_discoverable_plain_text(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                proc = run_cli([flag], tmp)
                self.assertEqual(0, proc.returncode)
                self.assertEqual(b"", proc.stderr)
                text = proc.stdout.decode("utf-8")
                self.assertIn("--json", text)
                self.assertIn("action-pins.v1", text)
                self.assertIn("usage", text.lower())
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(text)

    def test_help_with_json_stays_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(["--json", "--help"], tmp)
            self.assertEqual(0, proc.returncode)
            self.assertIn("--json", proc.stdout.decode("utf-8"))
            with self.assertRaises(json.JSONDecodeError):
                json.loads(proc.stdout.decode("utf-8"))

    def test_usage_error_is_outside_json_schema(self):
        for args in (["--json", "--definitely-unknown"],
                     ["--definitely-unknown"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                proc = run_cli(args, tmp)
                self.assertEqual(2, proc.returncode)
                self.assertEqual(b"", proc.stdout)
                self.assertIn("usage", proc.stderr.decode("utf-8").lower())
                self.assertNotIn(b"Traceback", proc.stderr)

    # -- check verdicts ------------------------------------------------------
    def test_clean_explicit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", CLEAN)
            proc = run_cli(["--json", "wf.yml"], tmp)
            self.assertEqual(0, proc.returncode)
            self.assert_common(self.parse_json(proc), ok=True,
                               scan_complete=True, requested=1, checked=1,
                               refused=0)

    def test_clean_policy_shapes_pass_in_json_mode(self):
        text = (
            f"steps:\n  - uses: actions/checkout@{SHA} # v4\n"
            "  - uses: ./.github/actions/local\n"
            f"  - uses: docker://alpine:3@sha256:{DIGEST}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", text)
            proc = run_cli(["--json", "wf.yml"], tmp)
            self.assertEqual(0, proc.returncode)
            self.assert_common(self.parse_json(proc), ok=True,
                               scan_complete=True, requested=1, checked=1,
                               refused=0)

    def test_unpinned_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", UNPINNED)
            proc = run_cli(["--json", "wf.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=True,
                               requested=1, checked=1, refused=0)
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0], path="wf.yml", line=2)
            self.assertEqual("actions/checkout@v4",
                             obj["findings"][0]["value"])
            self.assertEqual("pin_to_full_sha_or_digest", obj["next_action"])

    def test_missing_value_is_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", "steps:\n  - uses:\n")
            proc = run_cli(["--json", "wf.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=True,
                               requested=1, checked=1, refused=0)
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0], path="wf.yml", line=2)

    def test_literal_missing_value_placeholder_stays_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", "steps:\n  - uses: (missing value)\n")
            proc = run_cli(["--json", "wf.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=True,
                               requested=1, checked=1, refused=0)
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0], path="wf.yml", line=2)
            self.assertEqual("(missing value)",
                             obj["findings"][0]["value"])
            write(tmp, "empty.yml", "steps:\n  - uses:\n")
            proc2 = run_cli(["--json", "empty.yml"], tmp)
            obj2 = self.parse_json(proc2)
            self.assertEqual("", obj2["findings"][0]["value"])

    def test_json_flag_after_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", UNPINNED)
            proc = run_cli(["wf.yml", "--json"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assertEqual(1, len(obj["findings"]))

    # -- refused inputs ------------------------------------------------------
    def test_explicit_missing_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(["--json", "does-not-exist.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=1, checked=0, refused=1)
            self.assertEqual([], obj["findings"])
            self.assertEqual(1, len(obj["errors"]))
            self.assert_error(obj["errors"][0], code="input-missing",
                              next_action="fix_input_path",
                              path="does-not-exist.yml")
            self.assertEqual("fix_input_path", obj["next_action"])

    def test_explicit_unreadable_input_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a-dir.yml").mkdir()
            proc = run_cli(["--json", "a-dir.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=1, checked=0, refused=1)
            self.assert_error(obj["errors"][0], code="input-unreadable",
                              next_action="fix_input_permissions",
                              path="a-dir.yml")

    def test_explicit_unreadable_input_permissions(self):
        # Root can read mode-0 files, so when the runner is root the
        # checker subprocess drops to an unprivileged user; permission
        # bits are then enforced and the refusal is a real EACCES.
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "wf.yml", CLEAN)
            path.chmod(0)
            if _is_root():
                os.chmod(tmp, 0o755)
                checker = Path(tmp, "checker.py")
                checker.write_bytes(SCRIPT.read_bytes())
                checker.chmod(0o644)
                try:
                    proc = run_cli_as_nobody(
                        checker, ["--json", "wf.yml"], tmp)
                finally:
                    os.chmod(tmp, 0o700)
                    path.chmod(0o644)
            else:
                try:
                    proc = run_cli(["--json", "wf.yml"], tmp)
                finally:
                    path.chmod(0o644)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=1, checked=0, refused=1)
            self.assert_error(obj["errors"][0], code="input-unreadable",
                              next_action="fix_input_permissions",
                              path="wf.yml")

    def test_explicit_invalid_encoding_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", b"steps:\n  - uses: \xff\xfe@v4\n")
            proc = run_cli(["--json", "wf.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=1, checked=0, refused=1)
            self.assert_error(obj["errors"][0],
                              code="input-invalid-encoding",
                              next_action="fix_input_encoding", path="wf.yml")

    def test_valid_utf8_under_ascii_locale_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml",
                  "steps:\n  - uses: actions/checkout@v4 # café\n")
            proc = run_cli_with_env(["--json", "wf.yml"], tmp, ASCII_ENV)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=True,
                               requested=1, checked=1, refused=0)
            self.assertEqual(1, len(obj["findings"]))
            self.assertEqual([], obj["errors"])

    def test_invalid_encoding_under_ascii_locale_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", b"steps:\n  - uses: \xff\xfe@v4\n")
            proc = run_cli_with_env(["--json", "wf.yml"], tmp, ASCII_ENV)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=1, checked=0, refused=1)
            self.assert_error(obj["errors"][0],
                              code="input-invalid-encoding",
                              next_action="fix_input_encoding", path="wf.yml")

    def test_partial_failure_preserves_known_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", UNPINNED)
            proc = run_cli(["--json", "missing.yml", "wf.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=2, checked=1, refused=1)
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0], path="wf.yml", line=2)
            self.assert_error(obj["errors"][0], code="input-missing",
                              next_action="fix_input_path", path="missing.yml")
            # Errors outrank findings for the top-level next action.
            self.assertEqual("fix_input_path", obj["next_action"])

    # -- default discovery ---------------------------------------------------
    def test_no_discovered_files_is_never_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(["--json"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=0, checked=0, refused=0)
            self.assertEqual([], obj["findings"])
            self.assertEqual(1, len(obj["errors"]))
            self.assert_error(obj["errors"][0], code="no-inputs-discovered",
                              next_action="provide_input_files", path=None)
            self.assertEqual("provide_input_files", obj["next_action"])

    def test_default_discovery_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/a.yml", CLEAN)
            proc = run_cli(["--json"], tmp)
            self.assertEqual(0, proc.returncode)
            self.assert_common(self.parse_json(proc), ok=True,
                               scan_complete=True, requested=1, checked=1,
                               refused=0)

    def test_default_discovery_unreadable_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/a.yml", CLEAN)
            Path(tmp, ".github/workflows/bad.yml").mkdir(parents=True)
            proc = run_cli(["--json"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=2, checked=1, refused=1)
            self.assertEqual(1, len(obj["errors"]))
            self.assertEqual("input-unreadable", obj["errors"][0]["code"])
            self.assertTrue(obj["errors"][0]["path"].replace(
                "\\", "/").endswith(".github/workflows/bad.yml"))

    def test_default_discovery_local_action_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/actions/foo/action.yml",
                  "runs:\n  steps:\n    - uses: actions/checkout@v4\n")
            proc = run_cli(["--json"], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assert_common(obj, ok=False, scan_complete=True,
                               requested=1, checked=1, refused=0)
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0],
                                path=".github/actions/foo/action.yml",
                                line=3, exact_path=False)

    # -- determinism, encoding, side effects ---------------------------------
    def test_findings_sorted_and_output_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "b.yml", UNPINNED)
            write(tmp, "a.yml",
                  "steps:\n  - uses: actions/setup-node@v4\n"
                  "  - uses: actions/checkout@v4\n")
            first = run_cli(["--json", "b.yml", "a.yml"], tmp)
            second = run_cli(["--json", "b.yml", "a.yml"], tmp)
            self.assertEqual(1, first.returncode)
            self.assertEqual(first.stdout, second.stdout)
            obj = self.parse_json(first)
            keys = [(f["path"], f["line"], f["code"], f["value"])
                    for f in obj["findings"]]
            self.assertEqual(sorted(keys), keys)
            self.assertEqual(
                [("a.yml", 2), ("a.yml", 3), ("b.yml", 2)],
                [(f["path"], f["line"]) for f in obj["findings"]])

    def test_unusual_filename_roundtrips_through_json(self):
        name = "sp ace-é\nquote\".yml"
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, name, UNPINNED)
            proc = run_cli(["--json", name], tmp)
            self.assertEqual(1, proc.returncode)
            obj = self.parse_json(proc)
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0], path=name, line=2)

    def test_json_mode_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", UNPINNED)
            before = snapshot(tmp)
            proc = run_cli(["--json", "wf.yml"], tmp)
            self.assertEqual(1, proc.returncode)
            self.parse_json(proc)  # the run really was in JSON mode
            self.assertEqual(before, snapshot(tmp))

    # -- text mode controls and parity ---------------------------------------
    def test_text_mode_controls_unchanged(self):
        """Positive controls: these already pass before the feature exists."""
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "bad.yml", UNPINNED)
            write(tmp, "good.yml", CLEAN)
            bad = run_cli(["bad.yml"], tmp)
            self.assertEqual(1, bad.returncode)
            self.assertIn(b"::error", bad.stdout)
            good = run_cli(["good.yml"], tmp)
            self.assertEqual(0, good.returncode)
            self.assertNotIn(b"::error", good.stdout)
            missing = run_cli(["missing.yml"], tmp)
            self.assertEqual(1, missing.returncode)
            self.assertIn(b"::error", missing.stdout)

    def test_text_and_json_verdict_parity(self):
        cases = []
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "bad.yml", UNPINNED)
            write(tmp, "good.yml", CLEAN)
            for label, args in (("clean", ["good.yml"]),
                                ("unpinned", ["bad.yml"]),
                                ("missing", ["missing.yml"])):
                with self.subTest(label=label):
                    text = run_cli(args, tmp)
                    structured = run_cli(["--json", *args], tmp)
                    self.assertEqual(text.returncode, structured.returncode)
                    obj = self.parse_json(structured)
                    self.assertIs(text.returncode == 0, obj["ok"])
                    cases.append(label)
        self.assertEqual(3, len(cases))

    # -- round-two regression guards (already fixed at the base) --------------
    def assert_fifo_guard(self, *, default):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = ".github/workflows/" if default else ""
            pipe = prefix + "a-pipe.yml"
            bad = prefix + "z-bad.yml"
            write(tmp, bad, UNPINNED)
            os.mkfifo(Path(tmp, pipe))  # Deliberately no writer, ever.
            args = [] if default else [pipe, bad, pipe, bad]
            structured = run_cli(["--json", *args], tmp)
            text = run_cli(args, tmp)
            obj = self.parse_json(structured)
            self.assertEqual(1, structured.returncode)
            self.assertEqual(structured.returncode, text.returncode)
            self.assertEqual(b"", text.stderr)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=2, checked=1, refused=1)
            self.assertEqual(1, len(obj["errors"]))
            self.assert_error(obj["errors"][0], code="input-unreadable",
                              next_action="fix_input_permissions", path=pipe)
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0], path=bad, line=2)
            self.assertEqual("actions/checkout@v4",
                             obj["findings"][0]["value"])
            self.assertEqual("fix_input_permissions", obj["next_action"])
            output = text.stdout.decode("utf-8")
            self.assertEqual(2, len(output.splitlines()))
            self.assertIn(f"::error file={pipe},line=1::", output)
            self.assertIn(f"::error file={bad},line=2::", output)
            self.assertIn("actions/checkout@v4", output)

    def test_fifo_explicit_refusal_preserves_later_finding(self):
        self.assert_fifo_guard(default=False)

    def test_fifo_default_refusal_preserves_later_finding(self):
        self.assert_fifo_guard(default=True)

    def assert_ascii_text_guard(self, name):
        value = "actions/checkout@café"
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, name, f"steps:\n  - uses: {value}\n")
            text = run_cli_with_env([name], tmp, ASCII_ENV)
            structured = run_cli_with_env(["--json", name], tmp, ASCII_ENV)
            self.assertEqual(b"", text.stderr)
            self.assertNotIn(b"UnicodeEncodeError", text.stderr)
            self.assertEqual(1, text.returncode)
            self.assertEqual(text.returncode, structured.returncode)
            output = text.stdout.decode("utf-8")
            self.assertEqual(1, len(output.splitlines()))
            self.assertIn("::error file=", output)
            self.assertIn(",line=2::", output)
            self.assertIn(value, output)
            obj = self.parse_json(structured)
            self.assert_common(obj, ok=False, scan_complete=True,
                               requested=1, checked=1, refused=0)
            self.assertEqual([], obj["errors"])
            self.assertEqual(1, len(obj["findings"]))
            self.assert_finding(obj["findings"][0], line=2)
            self.assertEqual(value, obj["findings"][0]["value"])
            # ASCII argv may use surrogateescape for a non-ASCII path.
            # Exact path round-tripping is asserted in the transport tests.

    def test_ascii_locale_text_non_ascii_value(self):
        self.assert_ascii_text_guard("wf.yml")

    def test_ascii_locale_text_non_ascii_path_and_value(self):
        self.assert_ascii_text_guard("café.yml")

    # -- round-two extraction: actual YAML keys, not shell text ---------------
    def assert_extracted(self, source, expected):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", source)
            structured = run_cli(["--json", "wf.yml"], tmp)
            text = run_cli(["wf.yml"], tmp)
            obj = self.parse_json(structured)
            self.assertEqual(1 if expected else 0, structured.returncode)
            self.assertEqual(structured.returncode, text.returncode)
            self.assertEqual(b"", text.stderr)
            self.assert_common(obj, ok=not expected, scan_complete=True,
                               requested=1, checked=1, refused=0)
            self.assertEqual([], obj["errors"])
            self.assertEqual(expected, [(f["line"], f["value"])
                                        for f in obj["findings"]])
            annotations = [line for line in text.stdout.decode("utf-8").splitlines()
                           if line.startswith("::error")]
            self.assertEqual(len(expected), len(annotations))
            for finding in obj["findings"]:
                self.assert_finding(finding, path="wf.yml")
                self.assertIn(
                    f"::error file=wf.yml,line={finding['line']}::",
                    text.stdout.decode("utf-8"))
                self.assertIn(finding["value"] or "(missing value)",
                              text.stdout.decode("utf-8"))

    def test_nested_single_line_flow_value_and_source_line(self):
        self.assert_extracted(
            "name: fixture\n"
            "jobs: { test: { steps: [{ uses: actions/checkout@v4 }] } }\n",
            [(2, "actions/checkout@v4")])

    def test_nested_flow_value_excludes_following_mapping_entries(self):
        self.assert_extracted(
            "jobs: {\n  test: { steps: [\n"
            "    { name: Checkout, uses: actions/checkout@v4, "
            "with: { fetch-depth: 1 } }\n  ] }\n}\n",
            [(3, "actions/checkout@v4")])

    def test_multiple_uses_on_one_flow_line_are_all_extracted(self):
        self.assert_extracted(
            "name: fixture\n"
            "jobs: { test: { steps: [{ uses: actions/checkout@v4 }, "
            "{ uses: actions/setup-node@v4 }] } }\n",
            [(2, "actions/checkout@v4"), (2, "actions/setup-node@v4")])

    def test_nested_flow_missing_value_is_empty(self):
        self.assert_extracted(
            "name: fixture\njobs: { test: { steps: [{ uses: }] } }\n",
            [(2, "")])

    def test_escaped_quoted_uses_keys_are_decoded(self):
        cases = (
            'steps:\n  - "\\u0075ses": actions/checkout@v4\n',
            'steps:\n  - "u\\u0073es": actions/checkout@v4\n',
            'jobs: { test: { steps: [{ "\\u0075ses": '
            'actions/checkout@v4 }] } }\n',
        )
        for source in cases:
            with self.subTest(source=source):
                self.assert_extracted(source, [
                    (1 if source.startswith("jobs:") else 2,
                     "actions/checkout@v4")])

    def test_explicit_uses_keys_preserve_key_line_and_value(self):
        cases = (
            "steps:\n  - ? uses\n    : actions/checkout@v4\n",
            'steps:\n  - ? "\\u0075ses"\n    : actions/checkout@v4\n',
            "name: fixture\njobs: { test: { steps: ["
            "{ ? uses : actions/checkout@v4 }] } }\n",
        )
        for source in cases:
            with self.subTest(source=source):
                self.assert_extracted(source, [(2, "actions/checkout@v4")])

    def test_explicit_uses_key_missing_value_is_empty(self):
        self.assert_extracted("steps:\n  - ? uses\n    :\n", [(2, "")])

    def test_supported_key_shapes_preserve_pinned_policy(self):
        shapes = (
            "jobs: {{ test: {{ steps: [{{ uses: {value} }}] }} }}\n",
            'steps:\n  - "\\u0075ses": {value}\n',
            "steps:\n  - ? uses\n    : {value}\n",
            "jobs: {{ test: {{ steps: [{{ ? uses : {value} }}] }} }}\n",
        )
        values = (
            f"actions/checkout@{SHA}",
            f"docker://alpine:3@sha256:{DIGEST}",
            "./.github/actions/local",
            "./.github/workflows/reusable.yml",
            f"octo/repo/.github/workflows/reusable.yml@{SHA}",
        )
        for shape in shapes:
            for value in values:
                with self.subTest(shape=shape, value=value):
                    self.assert_extracted(shape.format(value=value), [])

    def test_quoted_run_strings_with_flow_punctuation_are_not_uses(self):
        cases = (
            "steps:\n  - run: 'echo \"{ uses: pretend/action@v4, "
            "- uses: pretend/other@main }\"'\n",
            'steps:\n  - run: "echo uses: pretend/action@v4, '
            '{ uses: pretend/other@main } - uses: pretend/third@v2"\n',
            'jobs: { test: { steps: [{ run: \'echo "{ uses: '
            'pretend/action@v4, - uses: pretend/other@main }"\' }] } }\n',
        )
        for source in cases:
            with self.subTest(source=source):
                self.assert_extracted(source, [])

    def test_run_block_scalars_do_not_hide_the_next_real_uses(self):
        for marker in ("|", ">-"):
            with self.subTest(marker=marker):
                self.assert_extracted(
                    f"steps:\n  - run: {marker}\n"
                    '      echo "{ uses: pretend/action@v4, - uses: fake/step@v2 }"\n'
                    "      uses: pretend/other@main\n"
                    "  - uses: actions/checkout@v4\n",
                    [(5, "actions/checkout@v4")])

    # -- round-two annotation framing at both emitter sites -------------------
    @staticmethod
    def property_path_cases():
        # Literal expectations, independent of the emitter's escape helper.
        return (
            ("percent%0A.yml", "percent%250A.yml"),
            ("carriage\rreturn.yml", "carriage%0Dreturn.yml"),
            ("new\nline.yml", "new%0Aline.yml"),
            ("comma,name.yml", "comma%2Cname.yml"),
            ("colon:name.yml", "colon%3Aname.yml"),
            ("bad%0A,slot:line\r\n::warning title=trap::payload.yml",
             "bad%250A%2Cslot%3Aline%0D%0A%3A%3Awarning title=trap%3A%3Apayload.yml"),
        )

    def assert_annotation_transport(self, *, missing):
        value = "actions/checkout@tag%0A,slot:tail"
        for name, escaped in self.property_path_cases():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                if not missing:
                    write(tmp, name, f"steps:\n  - uses: '{value}'\n")
                structured = run_cli(["--json", name], tmp)
                text = run_cli([name], tmp)
                obj = self.parse_json(structured)
                self.assertEqual(1, structured.returncode)
                self.assertEqual(structured.returncode, text.returncode)
                self.assertEqual(b"", text.stderr)
                self.assert_common(obj, ok=False, scan_complete=not missing,
                                   requested=1, checked=0 if missing else 1,
                                   refused=1 if missing else 0)
                if missing:
                    self.assertEqual([], obj["findings"])
                    self.assertEqual(1, len(obj["errors"]))
                    entry = obj["errors"][0]
                    self.assert_error(entry, code="input-missing",
                                      next_action="fix_input_path", path=name)
                    line = 1
                else:
                    self.assertEqual([], obj["errors"])
                    self.assertEqual(1, len(obj["findings"]))
                    entry = obj["findings"][0]
                    self.assert_finding(entry, path=name, line=2)
                    self.assertEqual(value, entry["value"])
                    line = 2
                self.assertIn(name, entry["message"])
                # Messages do NOT use property escaping for commas/colons.
                message = entry["message"].replace("%", "%25").replace(
                    "\r", "%0D").replace("\n", "%0A")
                self.assertEqual(
                    f"::error file={escaped},line={line}::{message}\n",
                    text.stdout.decode("utf-8"))
                self.assertEqual(1, len(text.stdout.decode("utf-8").splitlines()))

    def test_finding_annotation_escapes_file_property_once(self):
        self.assert_annotation_transport(missing=False)

    def test_input_error_annotation_escapes_file_property_once(self):
        self.assert_annotation_transport(missing=True)

    def test_control_character_paths_have_unique_counts_and_stable_order(self):
        a, z, missing = "a\r\n:,.yml", "z,%25:.yml", "m,%0A:.yml"
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, a, UNPINNED)
            write(tmp, z, UNPINNED)
            args = [z, missing, a, z, missing, a]
            first = run_cli(["--json", *args], tmp)
            second = run_cli(["--json", *args], tmp)
            text = run_cli(args, tmp)
            text_again = run_cli(args, tmp)
            obj = self.parse_json(first)
            self.assertEqual(1, first.returncode)
            self.assertEqual(first.returncode, text.returncode)
            self.assertEqual(b"", text.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(b"", second.stderr)
            self.assertEqual(text.stdout, text_again.stdout)
            self.assertEqual(b"", text_again.stderr)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=3, checked=2, refused=1)
            self.assertEqual([(a, 2, "actions/checkout@v4"),
                              (z, 2, "actions/checkout@v4")],
                             [(f["path"], f["line"], f["value"])
                              for f in obj["findings"]])
            self.assertEqual(1, len(obj["errors"]))
            self.assert_error(obj["errors"][0], code="input-missing",
                              next_action="fix_input_path", path=missing)
            self.assertEqual(3, len(text.stdout.decode("utf-8").splitlines()))

    # -- round-two discovery: real enumeration failures, not refused files ----
    @contextlib.contextmanager
    def denied_directory_cli(self, tmp, *directories):
        checker = SCRIPT
        identity = {}
        if _is_root():
            checker = write(tmp, "checker.py", SCRIPT.read_bytes())
            os.chmod(tmp, 0o755)
            for path in Path(tmp).rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            uid, gid = _unprivileged_ids()
            identity = {"user": uid, "group": gid}
        modes = [(Path(tmp, rel), Path(tmp, rel).stat().st_mode & 0o7777)
                 for rel in directories]
        try:
            for path, _ in modes:
                path.chmod(0)
            for rel in directories:
                # Prove the fixture really gets EACCES as the checker user.
                # This is independent of whatever discovery reports.
                probe = subprocess.run(
                    [sys.executable, "-B", "-c",
                     "import errno, os, sys\n"
                     "try:\n"
                     "    with os.scandir(sys.argv[1]): pass\n"
                     "except PermissionError as exc:\n"
                     "    sys.exit(0 if exc.errno == errno.EACCES else 2)\n"
                     "sys.exit(1)\n", rel],
                    cwd=tmp, env=clean_env(), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=30, **identity)
                self.assertEqual(0, probe.returncode, probe.stderr)
                self.assertEqual(b"", probe.stderr)
            if _is_root():
                yield lambda args: run_cli_as_nobody(checker, args, tmp)
            else:
                yield lambda args: run_cli(args, tmp)
        finally:
            for path, mode in modes:
                path.chmod(mode)
            if _is_root():
                os.chmod(tmp, 0o700)

    def assert_discovery_failure(self, invoke, *, requested, checked, refused,
                                 errors, findings):
        first = invoke(["--json"])
        text = invoke([])
        second = invoke(["--json"])
        text_again = invoke([])
        obj = self.parse_json(first)
        self.assertEqual(1, first.returncode)
        self.assertEqual(first.returncode, text.returncode)
        self.assertEqual(b"", text.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(b"", second.stderr)
        self.assertEqual(text.stdout, text_again.stdout)
        self.assertEqual(b"", text_again.stderr)
        self.assert_common(obj, ok=False, scan_complete=False,
                           requested=requested, checked=checked,
                           refused=refused)
        self.assertEqual(errors, [(e["path"], e["code"], e["next_action"])
                                  for e in obj["errors"]])
        self.assertEqual(findings, [(f["path"], f["line"], f["value"])
                                    for f in obj["findings"]])
        self.assertEqual(errors[0][2], obj["next_action"])
        output = text.stdout.decode("utf-8")
        self.assertEqual(len(errors) + len(findings), len(output.splitlines()))
        for error, (path, code, next_action) in zip(obj["errors"], errors):
            self.assert_error(error, code=code, next_action=next_action, path=path)
            self.assertIn(f"::error file={path},line=1::", output)
            if error["code"] == "input-unreadable":
                self.assertRegex(error["message"].lower(),
                                 r"permission denied|eacces|errno 13")
                self.assertIn(error["path"], error["message"])
        for finding in obj["findings"]:
            self.assert_finding(finding)
            self.assertIn(f"::error file={finding['path']},"
                          f"line={finding['line']}::", output)
            self.assertIn(finding["value"], output)

    def assert_denied_workflows_with_readable_action(self, *, pinned):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/hidden.yml", UNPINNED)
            action = ".github/actions/readable/action.yml"
            value = "./local" if pinned else "actions/checkout@v4"
            write(tmp, action, "runs:\n  using: composite\n  steps:\n"
                  f"    - uses: {value}\n")
            with self.denied_directory_cli(tmp, ".github/workflows") as invoke:
                self.assert_discovery_failure(
                    invoke, requested=1, checked=1, refused=0,
                    errors=[(".github/workflows", "input-unreadable",
                             "fix_input_permissions")],
                    findings=[] if pinned else [(action, 4, value)])

    def test_denied_workflow_root_with_readable_pinned_action_is_incomplete(self):
        self.assert_denied_workflows_with_readable_action(pinned=True)

    def test_denied_workflow_root_preserves_readable_unpinned_action(self):
        self.assert_denied_workflows_with_readable_action(pinned=False)

    def test_denied_nested_action_directory_preserves_accessible_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/a.yml", CLEAN)
            denied = ".github/actions/outer/denied"
            write(tmp, denied + "/action.yml", UNPINNED)
            action = ".github/actions/readable/action.yaml"
            write(tmp, action, "runs:\n  using: composite\n  steps:\n"
                  "    - uses: actions/checkout@v4\n")
            with self.denied_directory_cli(tmp, denied) as invoke:
                self.assert_discovery_failure(
                    invoke, requested=2, checked=2, refused=0,
                    errors=[(denied, "input-unreadable", "fix_input_permissions")],
                    findings=[(action, 4, "actions/checkout@v4")])

    def test_discovery_failure_preserves_file_refusal_and_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/hidden.yml", UNPINNED)
            bad = ".github/actions/bad/action.yml"
            good = ".github/actions/readable/action.yml"
            write(tmp, bad, b"runs:\n  steps:\n    - uses: \xff\n")
            write(tmp, good, "runs:\n  steps:\n    - uses: actions/checkout@v4\n")
            with self.denied_directory_cli(tmp, ".github/workflows") as invoke:
                self.assert_discovery_failure(
                    invoke, requested=2, checked=1, refused=1,
                    errors=[(bad, "input-invalid-encoding", "fix_input_encoding"),
                            (".github/workflows", "input-unreadable",
                             "fix_input_permissions")],
                    findings=[(good, 3, "actions/checkout@v4")])

    def test_failed_enumeration_with_zero_files_is_not_successfully_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/hidden.yml", UNPINNED)
            Path(tmp, ".github/actions").mkdir()
            with self.denied_directory_cli(tmp, ".github/workflows") as invoke:
                self.assert_discovery_failure(
                    invoke, requested=0, checked=0, refused=0,
                    errors=[(".github/workflows", "input-unreadable",
                             "fix_input_permissions")], findings=[])

    def test_two_denied_roots_have_sorted_unique_errors_and_zero_file_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/hidden.yml", UNPINNED)
            write(tmp, ".github/actions/hidden/action.yml", UNPINNED)
            with self.denied_directory_cli(
                    tmp, ".github/workflows", ".github/actions") as invoke:
                self.assert_discovery_failure(
                    invoke, requested=0, checked=0, refused=0,
                    errors=[(".github/actions", "input-unreadable",
                             "fix_input_permissions"),
                            (".github/workflows", "input-unreadable",
                             "fix_input_permissions")], findings=[])

    def test_successfully_empty_existing_roots_report_no_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".github/workflows").mkdir(parents=True)
            Path(tmp, ".github/actions").mkdir()
            structured = run_cli(["--json"], tmp)
            text = run_cli([], tmp)
            obj = self.parse_json(structured)
            self.assertEqual(1, structured.returncode)
            self.assertEqual(structured.returncode, text.returncode)
            self.assertEqual(b"", text.stderr)
            self.assert_common(obj, ok=False, scan_complete=False,
                               requested=0, checked=0, refused=0)
            self.assertEqual([], obj["findings"])
            self.assertEqual(1, len(obj["errors"]))
            self.assert_error(obj["errors"][0], code="no-inputs-discovered",
                              next_action="provide_input_files", path=None)
            self.assertIn(b"::error ::", text.stdout)

    def test_absent_optional_root_is_not_an_enumeration_failure(self):
        for present in (".github/workflows/a.yml",
                        ".github/actions/readable/action.yml"):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as tmp:
                source = (CLEAN if present.startswith(".github/workflows/") else
                          "runs:\n  using: composite\n  steps:\n    - uses: ./local\n")
                write(tmp, present, source)
                structured = run_cli(["--json"], tmp)
                text = run_cli([], tmp)
                self.assertEqual(0, structured.returncode)
                self.assertEqual(structured.returncode, text.returncode)
                self.assertEqual(b"", text.stderr)
                self.assertNotIn(b"::error", text.stdout)
                self.assert_common(self.parse_json(structured), ok=True,
                                   scan_complete=True, requested=1, checked=1,
                                   refused=0)

    # -- preflight extraction regression guards -------------------------------
    def assert_preflight_extraction(self, source, expected):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", source)
            before = snapshot(tmp)
            structured = run_cli(["--json", "wf.yml"], tmp)
            text = run_cli(["wf.yml"], tmp)
            structured_again = run_cli(["--json", "wf.yml"], tmp)
            text_again = run_cli(["wf.yml"], tmp)
            self.assertEqual(before, snapshot(tmp))
            expected_exit = 1 if expected else 0
            with self.subTest(mode="json"):
                obj = self.parse_json(structured)
                self.assertEqual(expected_exit, structured.returncode)
                self.assertEqual(b"", structured_again.stderr)
                self.assertEqual(expected_exit, structured_again.returncode)
                self.assertEqual(structured.stdout, structured_again.stdout)
                self.assert_common(obj, ok=not expected, scan_complete=True,
                                   requested=1, checked=1, refused=0)
                self.assertEqual([], obj["errors"])
                self.assertEqual(expected, [(f["line"], f["value"])
                                            for f in obj["findings"]])
                self.assertEqual("pin_to_full_sha_or_digest" if expected else
                                 "none", obj["next_action"])
                for finding in obj["findings"]:
                    self.assert_finding(finding, path="wf.yml")
            with self.subTest(mode="text"):
                self.assertEqual(b"", text.stderr)
                self.assertEqual(expected_exit, text.returncode)
                self.assertEqual(structured.returncode, text.returncode)
                self.assertEqual(b"", text_again.stderr)
                self.assertEqual(expected_exit, text_again.returncode)
                self.assertEqual(text.stdout, text_again.stdout)
                output = text.stdout.decode("utf-8")
                self.assertNotIn("Traceback", output)
                if expected:
                    annotations = output.splitlines()
                    self.assertEqual(len(expected), len(annotations))
                    for annotation, (line, value) in zip(annotations, expected):
                        self.assertTrue(annotation.startswith(
                            f"::error file=wf.yml,line={line}::"), annotation)
                        self.assertIn(f"unpinned action `{value}`", annotation)
                else:
                    self.assertIn("Action pins OK", output)
                    self.assertNotIn("::error", output)
                    self.assertEqual(1, len(output.splitlines()))

    def test_sequence_block_scalar_preserves_unpinned_sibling(self):
        for marker in ("|", ">-"):
            with self.subTest(marker=marker):
                self.assert_preflight_extraction(
                    f"steps:\n  - name: {marker}\n"
                    "      Checkout\n"
                    "    uses: actions/checkout@v4\n",
                    [(4, "actions/checkout@v4")])

    def test_sequence_block_scalar_pinned_sibling_is_clean(self):
        for marker in ("|", ">-"):
            with self.subTest(marker=marker):
                self.assert_preflight_extraction(
                    f"steps:\n  - name: {marker}\n"
                    "      Checkout\n"
                    f"    uses: actions/checkout@{SHA}\n", [])

    def test_empty_flow_value_preserves_unpinned_sibling(self):
        self.assert_preflight_extraction(
            "steps: [{name: , uses: actions/checkout@v4}]\n",
            [(1, "actions/checkout@v4")])

    def test_empty_flow_value_pinned_sibling_is_clean(self):
        self.assert_preflight_extraction(
            f"steps: [{{name: , uses: actions/checkout@{SHA}}}]\n", [])

    def test_empty_flow_value_preserves_later_same_line_step_in_sorted_order(self):
        self.assert_preflight_extraction(
            "steps: [{name: , uses: actions/setup-node@v4}, "
            "{uses: actions/checkout@v4}]\n",
            [(1, "actions/checkout@v4"), (1, "actions/setup-node@v4")])

    def test_empty_flow_value_preserves_later_flow_line(self):
        self.assert_preflight_extraction(
            "steps: [\n"
            "  {name: , uses: actions/checkout@v4},\n"
            "  {uses: actions/setup-node@v4}\n"
            "]\n",
            [(2, "actions/checkout@v4"), (3, "actions/setup-node@v4")])

    def test_empty_flow_value_pinned_later_flow_line_is_clean(self):
        self.assert_preflight_extraction(
            "steps: [\n"
            f"  {{name: , uses: actions/checkout@{SHA}}},\n"
            f"  {{uses: actions/setup-node@{SHA}}}\n"
            "]\n", [])

    def test_empty_trailing_sequence_item_is_clean(self):
        self.assert_preflight_extraction("branches:\n  - \n", [])

    def test_empty_trailing_sequence_item_eof_and_newline_variants_are_clean(self):
        sources = (
            "branches:\n  - ",
            "branches:\r\n  - \r\n",
            "branches:\n  - \n\n",
            "branches:\n  -\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assert_preflight_extraction(source, [])

    def test_empty_sequence_preserves_later_unpinned_mapping_and_key_line(self):
        cases = (
            ("branches:\n  - \nuses: actions/checkout@v4\n",
             [(3, "actions/checkout@v4")]),
            ("steps:\n  - with:\n      branches:\n        - \n"
             "    uses: actions/checkout@v4\n",
             [(5, "actions/checkout@v4")]),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assert_preflight_extraction(source, expected)

    def test_empty_sequence_before_pinned_mapping_is_clean(self):
        sources = (
            f"branches:\n  - \nuses: actions/checkout@{SHA}\n",
            "steps:\n  - with:\n      branches:\n        - \n"
            f"    uses: actions/checkout@{SHA}\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assert_preflight_extraction(source, [])


if __name__ == "__main__":
    unittest.main()
