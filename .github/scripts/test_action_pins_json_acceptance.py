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
"""
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


if __name__ == "__main__":
    unittest.main()
