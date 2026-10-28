"""Acceptance contract for the orphan-suite checker's ``--json`` mode.

These tests freeze the machine-readable contract of
``verification/orphan_suites.py --json`` before it is implemented. They
exercise only the external CLI (plus the pre-existing ``check()``
compatibility surface) against hermetic miniature git repositories, so
the implementation may be structured any way that satisfies them.

Frozen contract
===============

``--json`` is opt-in. With it, a completed or failed *check* prints
exactly one JSON object to stdout (nothing else on stdout, nothing on
stderr) with exactly these keys:

- ``schema``: the string ``"orphan-suites.v1"``
- ``outcome``: ``"clean"``, ``"findings"`` or ``"unmeasurable"``
- ``check_complete``: ``true`` iff the analysis ran to completion
  (``clean``/``findings``), ``false`` iff ``unmeasurable``
- ``suite_count``, ``exemption_count``, ``finding_count``: integers
  when the check completed; all three ``null`` when unmeasurable
  (unknown counts are never published as invented zeroes)
- ``findings``: a list, sorted by ``(path, code)``, of objects with
  exactly the keys ``code``, ``path``, ``retryable``, ``next_action``.
  Empty when clean and when unmeasurable.
- ``diagnostic``: ``null`` when the check completed; otherwise one
  object with exactly the keys ``code``, ``path``, ``retryable``,
  ``next_action`` describing why the check could not complete.

Finding codes (each finding's ``path`` is the repo-relative suite or
exemption path, always a string; ``retryable`` is ``false``):

- ``orphan-suite``: a tracked suite wired nowhere and not exempt
- ``exemption-invalid-reason``: exemption reason missing, blank,
  not a string, or spanning lines
- ``exemption-stale-not-a-suite``: exemption names a path that is not
  a tracked unwired suite (deleted, or never a suite)
- ``exemption-stale-now-wired``: exemption names a suite that is
  now wired

Unmeasurable diagnostic codes (``path`` is the repo-relative allowlist
path for allowlist failures when it resolves under the root, else
``null``):

- ``allowlist-missing`` (retryable ``false``)
- ``allowlist-unreadable`` (retryable ``false``)
- ``allowlist-malformed``: not valid JSON, or not a JSON object
  (retryable ``false``)
- ``root-invalid``: root does not exist, is not a directory, or is
  not inside a git work tree (retryable ``false``)
- ``git-failed``: git inspection of an apparent repository failed
  (retryable ``true``)
- ``analysis-failed``: any other failure that prevents the analysis
  from finishing (retryable ``false``)

Exit codes stay as today: 0 clean, 1 findings or unmeasurable,
2 usage errors. Usage boundaries: ``--help``/``-h`` always print the
text usage (which names ``--json``) and exit 0, even combined with
``--json``; usage errors always print text and exit 2, never JSON,
even when ``--json`` is present. Default text mode is unchanged.
Output is deterministic: the same tree yields byte-identical stdout.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import test_orphan_suites as fx
from verification import orphan_suites as ratchet

SCRIPT = ROOT / "verification" / "orphan_suites.py"
SCHEMA = "orphan-suites.v1"
ALLOWLIST_REL = "verification/orphan_suites_allowlist.json"
TIMEOUT = 30

TOP_KEYS = {"schema", "outcome", "check_complete", "suite_count",
            "exemption_count", "finding_count", "findings", "diagnostic"}
ENTRY_KEYS = {"code", "path", "retryable", "next_action"}

# code -> (retryable, next_action): the frozen actionable contract.
FINDING_ACTIONS = {
    "orphan-suite": (
        False,
        "wire the suite into CI or add an exemption with a one-line reason"),
    "exemption-invalid-reason": (
        False, "give the exemption a one-line reason"),
    "exemption-stale-not-a-suite": (False, "remove the exemption"),
    "exemption-stale-now-wired": (False, "remove the exemption"),
}
DIAGNOSTIC_ACTIONS = {
    "allowlist-missing": (
        False, "create the allowlist file or pass --allowlist"),
    "allowlist-unreadable": (
        False, "make the allowlist file readable"),
    "allowlist-malformed": (
        False, "fix the allowlist to be a JSON object of path to reason"),
    "root-invalid": (
        False, "pass an existing repository directory as --root"),
    "git-failed": (
        True, "fix git access for the root and retry"),
    "analysis-failed": (
        False, "fix the input the checker names and rerun"),
}


def clean_env(path=None):
    """A sanitized subprocess environment (no inherited git state)."""
    return {
        "PATH": os.environ["PATH"] if path is None else path,
        "HOME": tempfile.mkdtemp(prefix="orphan-json-home-"),
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def run_cli(args, env=None):
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT)] + list(args),
        capture_output=True, timeout=TIMEOUT,
        env=clean_env() if env is None else env)
    return (proc.returncode,
            proc.stdout.decode("utf-8"),
            proc.stderr.decode("utf-8"))


def json_run(args, env=None):
    """Run with --json semantics and return (exit, parsed, stdout, stderr).

    ``json.loads`` over the *entire* stdout proves there is exactly one
    JSON document and that control characters in paths are escaped.
    """
    code, stdout, stderr = run_cli(args, env=env)
    return code, json.loads(stdout), stdout, stderr


class JsonContractCase(unittest.TestCase):
    def assert_entry(self, entry, actions, code, path):
        retryable, next_action = actions[code]
        self.assertEqual(set(entry), ENTRY_KEYS)
        self.assertEqual(entry["code"], code)
        self.assertEqual(entry["path"], path)
        self.assertIs(entry["retryable"], retryable)
        self.assertEqual(entry["next_action"], next_action)

    def assert_complete(self, obj, outcome, suites, exemptions, findings):
        self.assertEqual(set(obj), TOP_KEYS)
        self.assertEqual(obj["schema"], SCHEMA)
        self.assertEqual(obj["outcome"], outcome)
        self.assertIs(obj["check_complete"], True)
        self.assertEqual(obj["suite_count"], suites)
        self.assertEqual(obj["exemption_count"], exemptions)
        self.assertEqual(obj["finding_count"], len(findings))
        self.assertIsNone(obj["diagnostic"])
        self.assertEqual(len(obj["findings"]), len(findings))
        keys = [(f["path"], f["code"]) for f in obj["findings"]]
        self.assertEqual(keys, sorted(keys),
                         "findings must be sorted by (path, code)")
        for entry, (code, path) in zip(obj["findings"], findings):
            self.assert_entry(entry, FINDING_ACTIONS, code, path)

    def assert_unmeasurable(self, obj, code, path):
        self.assertEqual(set(obj), TOP_KEYS)
        self.assertEqual(obj["schema"], SCHEMA)
        self.assertEqual(obj["outcome"], "unmeasurable")
        self.assertIs(obj["check_complete"], False)
        self.assertIsNone(obj["suite_count"])
        self.assertIsNone(obj["exemption_count"])
        self.assertIsNone(obj["finding_count"])
        self.assertEqual(obj["findings"], [])
        self.assert_entry(obj["diagnostic"], DIAGNOSTIC_ACTIONS, code, path)

    def assert_json_run(self, args, expected_exit, env=None):
        try:
            code, obj, stdout, stderr = json_run(args, env=env)
        except json.JSONDecodeError:
            code, stdout, stderr = run_cli(args, env=env)
            self.fail(f"stdout is not a single JSON document "
                      f"(exit {code}): {stdout[:200]!r} {stderr[:200]!r}")
        self.assertEqual(code, expected_exit)
        self.assertEqual(stderr, "")
        self.assertNotIn("Traceback", stdout)
        self.assertNotIn("PASS:", stdout)
        self.assertNotIn("FAIL:", stdout)
        return obj


class CleanAndFindingsTest(JsonContractCase):
    def test_clean_wired_suite_json(self):
        root = fx.repo_with(runs="./test_alpha.sh",
                            suites=["test_alpha.sh"], allowlist={})
        obj = self.assert_json_run(["--json", "--root", str(root)], 0)
        self.assert_complete(obj, "clean", 1, 0, [])

    def test_clean_valid_exemption_json_flag_last(self):
        root = fx.repo_with(suites=["test_alpha.sh"],
                            allowlist={"test_alpha.sh": "needs a database"})
        obj = self.assert_json_run(["--root", str(root), "--json"], 0)
        self.assert_complete(obj, "clean", 1, 1, [])

    def test_orphan_suite_finding_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"], allowlist={})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(obj, "findings", 1, 0,
                             [("orphan-suite", "test_alpha.sh")])

    def test_empty_reason_finding_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"],
                            allowlist={"test_alpha.sh": ""})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 1, 1,
            [("exemption-invalid-reason", "test_alpha.sh")])

    def test_multiline_reason_finding_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"],
                            allowlist={"test_alpha.sh": "one\ntwo"})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 1, 1,
            [("exemption-invalid-reason", "test_alpha.sh")])

    def test_deleted_suite_exemption_finding_json(self):
        root = fx.repo_with(allowlist={"test_gone.sh": "host-only"})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 0, 1,
            [("exemption-stale-not-a-suite", "test_gone.sh")])

    def test_non_suite_exemption_finding_json(self):
        root = fx.repo_with(allowlist={"README.md": "not a suite"})
        (root / "README.md").write_text("docs\n")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True,
                       capture_output=True, timeout=TIMEOUT)
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 0, 1,
            [("exemption-stale-not-a-suite", "README.md")])

    def test_now_wired_exemption_finding_json(self):
        root = fx.repo_with(runs="./test_alpha.sh",
                            suites=["test_alpha.sh"],
                            allowlist={"test_alpha.sh": "needs a database"})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 1, 1,
            [("exemption-stale-now-wired", "test_alpha.sh")])

    def test_two_findings_share_one_path_in_code_order(self):
        root = fx.repo_with(runs="./test_alpha.sh",
                            suites=["test_alpha.sh"],
                            allowlist={"test_alpha.sh": " "})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 1, 1,
            [("exemption-invalid-reason", "test_alpha.sh"),
             ("exemption-stale-now-wired", "test_alpha.sh")])

    def test_findings_sorted_by_path_then_code_with_counts(self):
        root = fx.repo_with(suites=["test_zulu.sh", "test_alpha.sh"],
                            allowlist={"test_gone.sh": "host-only"})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 2, 1,
            [("orphan-suite", "test_alpha.sh"),
             ("exemption-stale-not-a-suite", "test_gone.sh"),
             ("orphan-suite", "test_zulu.sh")])

    def test_unicode_space_and_control_paths_round_trip(self):
        names = ["test_café space.py", "test_line\nbreak.sh"]
        root = fx.repo_with(suites=names, allowlist={})
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_complete(
            obj, "findings", 2, 0,
            [("orphan-suite", "test_café space.py"),
             ("orphan-suite", "test_line\nbreak.sh")])

    def test_json_output_is_byte_identical_across_runs(self):
        root = fx.repo_with(suites=["test_zulu.sh", "test_alpha.sh"],
                            allowlist={"test_gone.sh": "host-only"})
        self.assert_json_run(["--json", "--root", str(root)], 1)
        first = run_cli(["--json", "--root", str(root)])
        second = run_cli(["--json", "--root", str(root)])
        self.assertEqual(first, second)

    def test_json_run_writes_nothing_in_checked_tree(self):
        root = fx.repo_with(suites=["test_alpha.sh"], allowlist={})

        def snapshot():
            found = {}
            for path in sorted(root.rglob("*")):
                if ".git" in path.parts or not path.is_file():
                    continue
                found[path.relative_to(root).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest())
            return found

        before = snapshot()
        self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assertEqual(snapshot(), before)


class UnmeasurableTest(JsonContractCase):
    def test_missing_allowlist_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"])
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_unmeasurable(obj, "allowlist-missing", ALLOWLIST_REL)

    def test_missing_explicit_allowlist_outside_root_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"])
        outside = Path(tempfile.mkdtemp()) / "absent.json"
        obj = self.assert_json_run(
            ["--json", "--root", str(root),
             "--allowlist", str(outside)], 1)
        self.assert_unmeasurable(obj, "allowlist-missing", None)

    def test_malformed_allowlist_json_syntax(self):
        root = fx.repo_with(suites=["test_alpha.sh"])
        (root / ALLOWLIST_REL).write_text("{ not json\n")
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_unmeasurable(obj, "allowlist-malformed", ALLOWLIST_REL)

    def test_malformed_allowlist_not_an_object(self):
        root = fx.repo_with(suites=["test_alpha.sh"])
        (root / ALLOWLIST_REL).write_text('["test_alpha.sh"]\n')
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_unmeasurable(obj, "allowlist-malformed", ALLOWLIST_REL)

    def test_unreadable_allowlist_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"])
        (root / ALLOWLIST_REL).mkdir()
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_unmeasurable(obj, "allowlist-unreadable", ALLOWLIST_REL)

    def test_missing_root_json(self):
        missing = Path(tempfile.mkdtemp()) / "no-such-dir"
        obj = self.assert_json_run(["--json", "--root", str(missing)], 1)
        self.assert_unmeasurable(obj, "root-invalid", None)

    def test_file_root_json(self):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        obj = self.assert_json_run(["--json", "--root", name], 1)
        self.assert_unmeasurable(obj, "root-invalid", None)

    def test_nonrepository_root_json(self):
        root = Path(tempfile.mkdtemp(prefix="orphan-json-nonrepo-"))
        (root / "verification").mkdir()
        (root / ALLOWLIST_REL).write_text("{}\n")
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_unmeasurable(obj, "root-invalid", None)

    def test_failing_git_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"], allowlist={})
        shim = Path(tempfile.mkdtemp(prefix="orphan-json-shim-"))
        git = shim / "git"
        git.write_text("#!/bin/sh\nexit 3\n")
        git.chmod(git.stat().st_mode | stat.S_IXUSR)
        env = clean_env(path=f"{shim}:/usr/bin:/bin")
        obj = self.assert_json_run(
            ["--json", "--root", str(root)], 1, env=env)
        self.assert_unmeasurable(obj, "git-failed", None)

    def test_missing_git_binary_json(self):
        root = fx.repo_with(suites=["test_alpha.sh"], allowlist={})
        empty = tempfile.mkdtemp(prefix="orphan-json-nopath-")
        obj = self.assert_json_run(
            ["--json", "--root", str(root)], 1, env=clean_env(path=empty))
        self.assert_unmeasurable(obj, "git-failed", None)

    def test_analysis_cannot_finish_json(self):
        root = fx.make_repo({
            ".github/workflows/ci.yml": fx.WORKFLOW.format(runs="echo x"),
            "scripts/verify-fast.sh": "#!/usr/bin/env bash\necho stub\n",
            ALLOWLIST_REL: "{}\n",
            "test_alpha.sh": "#!/usr/bin/env bash\necho suite\n",
        })
        obj = self.assert_json_run(["--json", "--root", str(root)], 1)
        self.assert_unmeasurable(obj, "analysis-failed", None)

    def test_nonrepository_root_text_mode_has_no_traceback(self):
        root = Path(tempfile.mkdtemp(prefix="orphan-json-nonrepo-"))
        (root / "verification").mkdir()
        (root / ALLOWLIST_REL).write_text("{}\n")
        code, stdout, stderr = run_cli(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", stderr)
        self.assertTrue(stdout.startswith("FAIL:"))


class UsageBoundaryTest(JsonContractCase):
    def test_help_is_text_and_documents_json_flag(self):
        for args in (["--help"], ["-h"], ["--json", "--help"],
                     ["--help", "--json"]):
            with self.subTest(args=args):
                code, stdout, stderr = run_cli(args)
                self.assertEqual(code, 0)
                self.assertIn("usage:", stdout)
                self.assertIn("--json", stdout)
                self.assertRaises(json.JSONDecodeError,
                                  json.loads, stdout)

    def test_usage_errors_are_text_never_json(self):
        for args in (["--bogus"], ["--json", "--bogus"], ["extra"],
                     ["--json", "extra"], ["--root"], ["--json", "--root"],
                     ["--allowlist"], ["--json", "--allowlist"]):
            with self.subTest(args=args):
                code, stdout, stderr = run_cli(args)
                self.assertEqual(code, 2)
                self.assertIn("usage:", stdout)
                self.assertRaises(json.JSONDecodeError,
                                  json.loads, stdout)

    def test_usage_error_text_documents_json_flag(self):
        code, stdout, stderr = run_cli(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("--json", stdout)


class PositiveControlTest(unittest.TestCase):
    """Behavior that already holds today and must not regress."""

    def test_text_clean_exit_zero(self):
        root = fx.repo_with(runs="./test_alpha.sh",
                            suites=["test_alpha.sh"], allowlist={})
        code, stdout, stderr = run_cli(["--root", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("PASS:", stdout)

    def test_text_orphan_exit_one_and_parity_paths(self):
        root = fx.repo_with(suites=["test_zulu.sh", "test_alpha.sh"],
                            allowlist={"test_gone.sh": "host-only"})
        code, stdout, stderr = run_cli(["--root", str(root)])
        self.assertEqual(code, 1)
        for path in ("test_alpha.sh", "test_gone.sh", "test_zulu.sh"):
            self.assertIn(path, stdout)
        self.assertEqual(stdout.count("FAIL:"), 3)

    def test_text_missing_allowlist_exit_one(self):
        root = fx.repo_with(suites=["test_alpha.sh"])
        code, stdout, stderr = run_cli(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL:", stdout)

    def test_text_help_and_usage_exits(self):
        code, stdout, _ = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", stdout)
        code, stdout, _ = run_cli(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", stdout)

    def test_check_function_compatibility(self):
        wired = fx.repo_with(runs="./test_alpha.sh",
                             suites=["test_alpha.sh"])
        self.assertEqual(ratchet.check(wired, {}), [])
        orphan = fx.repo_with(suites=["test_alpha.sh"])
        errors = ratchet.check(orphan, {})
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], str)
        self.assertIn("test_alpha.sh", errors[0])


class TextJsonParityTest(JsonContractCase):
    def test_exit_and_finding_count_parity(self):
        root = fx.repo_with(suites=["test_zulu.sh", "test_alpha.sh"],
                            allowlist={"test_gone.sh": "host-only"})
        text_code, text_out, _ = run_cli(["--root", str(root)])
        obj = self.assert_json_run(["--json", "--root", str(root)],
                                   text_code)
        self.assertEqual(obj["finding_count"], text_out.count("FAIL:"))
        for finding in obj["findings"]:
            self.assertIn(finding["path"], text_out)

    def test_clean_parity_exit_zero(self):
        root = fx.repo_with(runs="./test_alpha.sh",
                            suites=["test_alpha.sh"], allowlist={})
        text_code, _, _ = run_cli(["--root", str(root)])
        obj = self.assert_json_run(["--json", "--root", str(root)],
                                   text_code)
        self.assertEqual(obj["outcome"], "clean")


if __name__ == "__main__":
    unittest.main()
