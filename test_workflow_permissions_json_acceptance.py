#!/usr/bin/env python3
"""Independent, immutable CLI acceptance for workflow-permission JSON output.

Run: python3 -B -m unittest -v test_workflow_permissions_json_acceptance
Requires Git on the parent PATH and the repository's already-pinned PyYAML;
never installs dependencies.

The v1 wire contract is fixed here before implementation:
  schema_version: 1 (integer, not bool)
  checked_files / unmeasurable_files: nonnegative integer file counts
  measurement: complete | inconclusive
  verdict: pass | fail | inconclusive
  findings: [{rule_id, code, file, job, line, retryable, next_action}]
  errors: [{code, file, retryable, next_action}]
Rule ID: workflow.job_permissions.required; code: missing_job_permissions.
Error codes: invalid_workflow, unreadable_workflow, yaml_parser_unavailable,
no_workflow_inputs, invalid_arguments. Global errors have file: null.
next_action is actionable remediation, not a claim of least privilege.
Findings sort by (file, line, job, rule_id); errors by (file or '', code).
Exit 2 outranks exit 1: incomplete evidence never yields a conclusive verdict.

Breaks caught: prose masquerading as JSON; success from missing evidence;
loss of observed findings on partial input; unstable identifiers/order; changing
job identity, alias/merge policy or presence-only scope; flag-as-path regressions;
wrong main(argv) convention; writes, external processes and network attempts.

Only the dependency-import fault and safety boundaries are injected. The real
entrypoint parses argv, reads fixtures, analyzes YAML and renders every verdict.
Child-only Python audit hooks deny writes, network and process launches even via
absolute paths. A poison PATH provides a second process boundary; all denied
attempts are recorded. These are regression fences, not a hostile-code sandbox.
No checker implementation or existing test/helper call signature is changed.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parent
CHECKER = ROOT / "check_workflow_job_permissions.py"
RULE_ID = "workflow.job_permissions.required"
MISSING_CODE = "missing_job_permissions"
GOOD = "jobs:\n  build:\n    permissions: {}\n    steps: []\n"
MISSING = "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n"

# This bootstrap supplies containment, not checker logic. It never calculates a
# finding, substitutes output, catches checker exceptions or changes exit codes.
CHILD = r'''
import builtins, os, runpy, sys
checker, mode, denied_fd, deny_yaml, *args = sys.argv[1:]
denied_fd = int(denied_fd)
write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
mutations = {
    'os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown',
    'os.link', 'os.symlink', 'os.truncate', 'os.utime', 'os.setxattr',
    'os.removexattr', 'shutil.copyfile', 'shutil.copymode', 'shutil.copystat',
}
processes = {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn',
             'os.spawn', 'os.fork', 'os.forkpty', 'pty.spawn'}
def audit(event, args):
    denied = (event in mutations or event in processes or
              event.startswith('socket.') or event == 'urllib.Request')
    if event == 'open':
        _, mode, flags = args
        denied = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(flags & write_flags)
    if denied:
        os.write(denied_fd, (event + '\n').encode())
        raise PermissionError('acceptance denied: ' + event)
sys.addaudithook(audit)
if deny_yaml == '1':
    original_import = builtins.__import__
    def without_yaml(name, *args, **kwargs):
        if name == 'yaml' or name.startswith('yaml.'):
            raise ImportError('acceptance: YAML parser unavailable')
        return original_import(name, *args, **kwargs)
    builtins.__import__ = without_yaml
if mode == 'probe':
    exec(args[0], {'__name__': '__main__'})
elif mode == 'main':
    # Preserve the existing public main convention, including argv[0].
    namespace = runpy.run_path(checker)
    raise SystemExit(namespace['main']([checker, *args]))
else:
    sys.argv = [checker, *args]
    runpy.run_path(checker, run_name='__main__')
'''


def checkout_state():
    """Requires Git on the parent PATH; observe outside the poison child fence."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git on parent PATH required for checkout-before/after evidence")
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    parts = []
    for args in (("status", "--porcelain=v1", "--untracked-files=all"),
                 ("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")):
        result = subprocess.run(
            [git, "-c", "core.fsmonitor=false", *args], cwd=ROOT, env=env,
            capture_output=True, timeout=15, check=True,
        )
        parts.append(result.stdout)
    paths = [CHECKER, Path(__file__), ROOT / "test_check_workflow_job_permissions.py"]
    paths.extend(sorted((ROOT / ".github/workflows").glob("*")))
    parts.extend(path.read_bytes() for path in paths if path.is_file())
    return hashlib.sha256(b"\0".join(parts)).hexdigest()


class CLIFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = checkout_state()

    @classmethod
    def tearDownClass(cls):
        if checkout_state() != cls.before:
            raise AssertionError("checker changed the checkout outside test scratch")

    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        self.temp = tempfile.TemporaryDirectory(prefix="permissions-json-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.bin = self.cwd / "bin"
        self.bin.mkdir()
        self.poison = self.cwd / "poison.log"
        for name in ("gh", "git", "curl", "wget", "ssh", "scp", "rsync", "nc",
                     "pip", "pip3", "python", "python3", "bash", "sh", "sudo"):
            path = self.bin / name
            path.write_text('#!/bin/sh\nprintf "forbidden command\\n" >> "$POISON_LOG"\nexit 97\n')
            path.chmod(0o755)
        # HOME and the environment contain no inherited credentials or endpoints.
        # Carry only the installed parser's package directory, not the user's HOME.
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.cwd), "TMPDIR": str(self.cwd),
            "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "POISON_LOG": str(self.poison),
            "PYTHONPATH": str(Path(yaml.__file__).resolve().parent.parent),
        }

    def fixture(self, text, name="fixture.yml"):
        path = self.cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def tree_state(self):
        return {
            str(path.relative_to(self.cwd)): (path.stat().st_mode, path.read_bytes())
            for path in self.cwd.rglob("*") if path.is_file()
        }

    def invoke(self, *args, mode="cli", deny_yaml=False, expected_denials=()):
        self.assertTrue(CHECKER.is_file(), "actual checker entrypoint must exist")
        before = self.tree_state()
        # An inherited file descriptor records denied operations without granting
        # the child any filesystem writes. The parent owns this temporary log.
        with tempfile.TemporaryFile(dir=self.cwd) as log:
            result = subprocess.run(
                [sys.executable, "-B", "-c", CHILD, str(CHECKER), mode,
                 str(log.fileno()), "1" if deny_yaml else "0", *map(str, args)],
                cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", capture_output=True, timeout=10,
                pass_fds=(log.fileno(),),
            )
            log.seek(0)
            denied = log.read().decode().splitlines()
        self.assertEqual(denied, list(expected_denials), "unexpected side effects attempted")
        self.assertFalse(self.poison.exists(), "checker attempted an external command")
        self.assertEqual(self.tree_state(), before, "checker changed local fixture state")
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        return result

    def assert_exit(self, result, expected):
        self.assertEqual(result.returncode, expected,
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def report(self, result, *, checked, unmeasurable, verdict):
        try:
            report = json.loads(result.stdout, object_pairs_hook=self.unique_keys,
                                parse_constant=self.reject_nonfinite)
        except (ValueError, UnicodeError) as error:
            self.fail(f"expected one JSON object on stdout, not prose/trailing objects: {error}; "
                      f"exit={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}")
        self.assertIsInstance(report, dict)
        self.assertEqual(result.stderr, "", "JSON mode must not mix a prose error channel into its envelope")
        self.assertIs(type(report.get("schema_version")), int)
        self.assertEqual(report["schema_version"], 1)
        for key, expected in (("checked_files", checked), ("unmeasurable_files", unmeasurable)):
            self.assertIs(type(report.get(key)), int, key)
            self.assertEqual(report[key], expected, key)
        self.assertEqual(report.get("measurement"), "inconclusive" if verdict == "inconclusive" else "complete")
        self.assertEqual(report.get("verdict"), verdict)
        self.assertIsInstance(report.get("findings"), list)
        self.assertIsInstance(report.get("errors"), list)
        for finding in report["findings"]:
            self.assertIsInstance(finding, dict)
            self.assertEqual(finding.get("rule_id"), RULE_ID)
            self.assertEqual(finding.get("code"), MISSING_CODE)
            self.assertIs(finding.get("retryable"), False)
            self.assertIsInstance(finding.get("file"), str)
            self.assertIsInstance(finding.get("job"), str)
            self.assertTrue(finding["file"])
            self.assertTrue(finding["job"])
            self.assertIs(type(finding.get("line")), int)
            self.assertGreater(finding["line"], 0)
            self.assert_action(finding)
            self.assertRegex(finding["next_action"].lower(), r"permissions")
        for error in report["errors"]:
            self.assertIsInstance(error, dict)
            self.assertIn(error.get("code"), {
                "invalid_workflow", "unreadable_workflow", "yaml_parser_unavailable",
                "no_workflow_inputs", "invalid_arguments",
            })
            self.assertIs(type(error.get("retryable")), bool)
            self.assertIn("file", error)
            self.assertTrue(error["file"] is None or isinstance(error["file"], str))
            self.assert_action(error)
        self.assertEqual(report["findings"], sorted(report["findings"], key=lambda f: (f["file"], f["line"], f["job"], f["rule_id"])))
        self.assertEqual(report["errors"], sorted(report["errors"], key=lambda e: (e["file"] or "", e["code"])))
        if verdict == "pass":
            self.assertNotRegex(result.stdout, r"(?i)least[ -]privilege",
                                "presence-only success cannot certify least privilege")
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["errors"], [])
        elif verdict == "fail":
            self.assertTrue(report["findings"])
            self.assertEqual(report["errors"], [])
        else:
            self.assertTrue(report["errors"], "inconclusive output needs a recoverable error")
        return report

    @staticmethod
    def reject_nonfinite(value):
        raise ValueError(f"non-JSON numeric constant: {value}")

    @staticmethod
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def assert_action(self, item):
        action = item.get("next_action")
        self.assertIsInstance(action, str)
        self.assertRegex(action.lower(), r"\b(add|declare|set|fix|repair|restore|check|provide|install|run|use|remove|select|read|verify|ensure|create)\b",
                         "next_action must name a corrective operation")
        self.assertNotRegex(action, r"[\x00-\x1f\x7f]")

    def pair(self, *args, exit_code, checked, unmeasurable=0, deny_yaml=False, mode="cli"):
        text = self.invoke(*args, deny_yaml=deny_yaml, mode=mode)
        self.assert_exit(text, exit_code)
        result = self.invoke("--json", *args, deny_yaml=deny_yaml, mode=mode)
        self.assert_exit(result, exit_code)
        verdict = {0: "pass", 1: "fail", 2: "inconclusive"}[exit_code]
        report = self.report(result, checked=checked, unmeasurable=unmeasurable, verdict=verdict)
        return text, report

    def identities(self, report):
        return [(f["file"], f["job"], f["line"]) for f in report["findings"]]

    def assert_error(self, report, code, path=None):
        self.assertEqual([(error["code"], error["file"]) for error in report["errors"]],
                         [(code, str(path) if path is not None else None)])


class OfflineFenceControls(CLIFixture):
    def test_guard_denies_relative_and_absolute_process_launches(self):
        for command in (("gh", "--version"), (sys.executable, "-V")):
            with self.subTest(command=command):
                probe = ("import subprocess\ntry:\n subprocess.run(" + repr(command) + ")\n"
                         "except PermissionError:\n pass\nelse:\n raise AssertionError('launch escaped')\n")
                self.assert_exit(self.invoke(probe, mode="probe", expected_denials=("subprocess.Popen",)), 0)

    def test_guard_denies_network_without_contacting_an_endpoint(self):
        probe = ("import socket\ntry:\n socket.socket()\nexcept PermissionError:\n pass\n"
                 "else:\n raise AssertionError('socket escaped')\n")
        self.assert_exit(self.invoke(probe, mode="probe", expected_denials=("socket.__new__",)), 0)

    def test_guard_denies_writes_even_inside_test_scratch(self):
        path = self.fixture("unchanged")
        probe = ("from pathlib import Path\ntry:\n Path(" + repr(str(path)) + ").write_text('changed')\n"
                 "except PermissionError:\n pass\nelse:\n raise AssertionError('write escaped')\n")
        self.assert_exit(self.invoke(probe, mode="probe", expected_denials=("open",)), 0)


class ExistingTextContract(CLIFixture):
    """Positive/negative baseline controls stay useful before JSON exists."""

    def test_three_existing_exits_and_diagnostics(self):
        for text, code in ((GOOD, 0), (MISSING, 1), ("jobs: [broken\n", 2)):
            with self.subTest(exit_code=code):
                path = self.fixture(text)
                result = self.invoke(path)
                self.assert_exit(result, code)
                if code:
                    self.assertIn(str(path), result.stdout + result.stderr)
                if code == 1:
                    self.assertIn("build", result.stdout + result.stderr)

    def test_explicit_paths_replace_default_discovery(self):
        self.fixture("jobs: [broken\n", ".github/workflows/unselected.yml")
        path = self.fixture(GOOD, "other directory/selected.yaml")
        self.assert_exit(self.invoke(path), 0)

    def test_public_main_keeps_argv_zero_in_the_argument_list(self):
        path = self.fixture(GOOD)
        self.assert_exit(self.invoke(path, mode="main"), 0)

    def test_parser_unavailability_is_an_input_error(self):
        self.assert_exit(self.invoke(self.fixture(GOOD), deny_yaml=True), 2)

    def test_missing_and_directory_paths_do_not_pass(self):
        directory = self.cwd / "directory.yaml"
        directory.mkdir()
        for path in (directory, self.cwd / "absent.yml"):
            with self.subTest(path=path):
                self.assert_exit(self.invoke(path), 2)

    def test_alias_merge_and_presence_only_policy_controls(self):
        fixtures = (
            ("x: &scopes {contents: read}\njobs:\n  build:\n    permissions: *scopes\n", 0),
            ("x: &defaults {permissions: {}}\njobs:\n  build:\n    <<: *defaults\n", 1),
            ("jobs: {build: {permissions: {unknown: not-validated}}}\n", 0),
            ("jobs: {build: {permissions: write-all}}\n", 1),
            ("jobs: {call: {uses: './offline.yml'}}\n", 1),
        )
        for text, code in fixtures:
            with self.subTest(text=text):
                result = self.invoke(self.fixture(text))
                self.assert_exit(result, code)
                self.assertNotRegex(result.stdout + result.stderr, r"(?i)least[ -]privilege")

    def test_missing_findings_survive_mixed_input_errors_in_prose(self):
        path = self.fixture(MISSING, "missing.yml")
        for args in ((path, self.cwd / "absent.yml"), (self.cwd / "absent.yml", path)):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assert_exit(result, 2)
                self.assertIn("build", result.stdout + result.stderr)
                self.assertIn(str(path), result.stdout + result.stderr)


class StructuredPermissionCLI(CLIFixture):
    def test_pass_is_one_versioned_object_with_counts(self):
        self.pair(self.fixture(GOOD), exit_code=0, checked=1)

    def test_missing_permission_names_the_exact_job_key_line(self):
        path = self.fixture(MISSING)
        _, report = self.pair(path, exit_code=1, checked=1)
        self.assertEqual(self.identities(report), [(str(path), "build", 2)])

    def test_json_flag_can_follow_paths_without_becoming_an_input_file(self):
        path = self.fixture(GOOD)
        result = self.invoke(path, "--json")
        self.assert_exit(result, 0)
        self.report(result, checked=1, unmeasurable=0, verdict="pass")

    def test_main_argv_keeps_its_public_convention_in_json_mode(self):
        self.pair(self.fixture(MISSING), exit_code=1, checked=1, mode="main")

    def test_invalid_empty_and_structurally_invalid_workflows_are_unmeasurable(self):
        for text in ("", "# comment only\n", "null\n", "[]\n", "name: no-jobs\n",
                     "jobs: {}\n", "jobs: []\n", "jobs: {build: null}\n", "jobs: [broken\n",
                     GOOD + "---\n" + GOOD):
            with self.subTest(text=text):
                path = self.fixture(text)
                _, report = self.pair(path, exit_code=2, checked=0, unmeasurable=1)
                self.assertEqual(report["findings"], [])
                self.assert_error(report, "invalid_workflow", path)

    def test_unreadable_and_missing_explicit_files_are_not_zero_inputs(self):
        directory = self.cwd / "directory.yml"
        directory.mkdir()
        invalid_utf8 = self.cwd / "not utf8.yaml"
        invalid_utf8.write_bytes(b"jobs: {build: {permissions: {}}}\n\xff")
        for path in (self.cwd / "absent.yml", directory, invalid_utf8):
            with self.subTest(path=path):
                _, report = self.pair(path, exit_code=2, checked=0, unmeasurable=1)
                self.assertEqual(report["findings"], [])
                self.assert_error(report, "unreadable_workflow", path)

    def test_real_read_permission_failure_is_not_missing_job_permissions(self):
        # EACCES is injected only at open, so this also works as root. The actual
        # checker's read-error handling and JSON rendering remain under test.
        path = self.fixture(GOOD)
        probe = ("import builtins, runpy, sys\noriginal = builtins.open\n"
                 "def denied(file, *args, **kwargs):\n"
                 " if str(file) == " + repr(str(path)) + ": raise PermissionError('synthetic EACCES')\n"
                 " return original(file, *args, **kwargs)\n"
                 "import io\nio.open = denied\nbuiltins.open = denied\n"
                 "sys.argv = [" + repr(str(CHECKER)) + ", '--json', " + repr(str(path)) + "]\n"
                 "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        result = self.invoke(probe, mode="probe")
        self.assert_exit(result, 2)
        report = self.report(result, checked=0, unmeasurable=1, verdict="inconclusive")
        self.assert_error(report, "unreadable_workflow", path)

    def test_mixed_valid_and_unreadable_retains_findings_and_exit_two(self):
        good = self.fixture(GOOD, "good.yml")
        missing = self.fixture(MISSING, "missing.yml")
        absent = self.cwd / "absent.yml"
        for args in ((good, missing, absent), (absent, missing, good)):
            with self.subTest(args=args):
                _, report = self.pair(*args, exit_code=2, checked=2, unmeasurable=1)
                self.assertEqual(self.identities(report), [(str(missing), "build", 2)])
                self.assert_error(report, "unreadable_workflow", absent)

    def test_mixed_valid_and_invalid_retains_findings_and_exit_two(self):
        missing = self.fixture(MISSING, "observed.yml")
        invalid = self.fixture("jobs: [broken\n", "invalid.yml")
        _, report = self.pair(invalid, missing, exit_code=2, checked=1, unmeasurable=1)
        self.assertEqual(self.identities(report), [(str(missing), "build", 2)])
        self.assert_error(report, "invalid_workflow", invalid)

    def test_compliant_file_does_not_mask_an_invalid_file(self):
        good = self.fixture(GOOD)
        invalid = self.fixture("jobs: [broken\n", "invalid.yml")
        _, report = self.pair(good, invalid, exit_code=2, checked=1, unmeasurable=1)
        self.assertEqual(report["findings"], [])
        self.assert_error(report, "invalid_workflow", invalid)

    def test_no_discovered_inputs_is_inconclusive_not_vacuous_success(self):
        for directory_exists in (False, True):
            with self.subTest(directory_exists=directory_exists):
                if directory_exists:
                    self.fixture("not a workflow", ".github/workflows/notes.txt")
                _, report = self.pair(exit_code=2, checked=0)
                self.assertEqual(report["findings"], [])
                self.assert_error(report, "no_workflow_inputs")

    def test_default_discovery_is_direct_dynamic_and_supports_both_extensions(self):
        self.fixture(GOOD, ".github/workflows/first.yml")
        for name in ("nested/ignored.yml", "notes.txt", "backup.yaml.bak"):
            self.fixture("jobs: [broken\n", ".github/workflows/" + name)
        self.pair(exit_code=0, checked=1)
        path = self.fixture(MISSING, ".github/workflows/new workflow ü.yaml")
        _, report = self.pair(exit_code=1, checked=2)
        self.assertEqual([(Path(f[0]).name, *f[1:]) for f in self.identities(report)],
                         [(path.name, "build", 2)])

    def test_explicit_input_overrides_discovery_in_both_formats(self):
        self.fixture("jobs: [broken\n", ".github/workflows/ignored.yml")
        path = self.fixture(GOOD, "outside workflow directory/selected ü.yaml")
        self.pair(path.relative_to(self.cwd), exit_code=0, checked=1)

    def test_missing_yaml_parser_has_a_global_recoverable_envelope(self):
        _, report = self.pair(self.fixture(GOOD), exit_code=2, checked=0,
                              unmeasurable=1, deny_yaml=True)
        self.assertEqual(report["findings"], [])
        self.assert_error(report, "yaml_parser_unavailable")
        self.assertRegex(report["errors"][0]["next_action"].lower(), r"yaml|parser|requirements")

    def test_special_paths_and_decoded_job_ids_survive_losslessly(self):
        path = self.fixture('jobs:\n  "deploy ü;$(touch injected)":\n    steps: []\n',
                            "directory ü with spaces/workflow ;$(touch injected).yaml")
        _, report = self.pair(path, exit_code=1, checked=1)
        self.assertEqual(self.identities(report), [(str(path), "deploy ü;$(touch injected)", 2)])

    def test_findings_are_deterministic_independent_of_argument_order(self):
        z = self.fixture('jobs: {last: {}, good: {permissions: {}}, first: {}}\n', "z.yml")
        a = self.fixture('jobs:\n  "ü job": {}\n  "a job": {}\n', "a ü.yaml")
        first = self.invoke("--json", z, a)
        second = self.invoke("--json", a, z)
        again = self.invoke("--json", z, a)
        for result in (first, second, again):
            self.assert_exit(result, 1)
        report = self.report(first, checked=2, unmeasurable=0, verdict="fail")
        self.assertEqual(self.identities(report), [
            (str(a), "ü job", 2), (str(a), "a job", 3),
            (str(z), "first", 1), (str(z), "last", 1),
        ])
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stdout, again.stdout)

    def test_error_codes_and_order_do_not_depend_on_parser_display_text(self):
        a = self.fixture("jobs: [broken\n", "a.yml")
        z = self.fixture("jobs: {\n", "z.yml")
        results = [self.invoke("--json", *args) for args in ((z, a), (a, z))]
        for result in results:
            self.assert_exit(result, 2)
        report = self.report(results[0], checked=0, unmeasurable=2, verdict="inconclusive")
        self.assertEqual([(e["file"], e["code"]) for e in report["errors"]],
                         [(str(a), "invalid_workflow"), (str(z), "invalid_workflow")])
        self.assertEqual(results[0].stdout, results[1].stdout)

    def test_help_is_local_discoverable_and_does_not_require_yaml(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                result = self.invoke(flag, deny_yaml=True)
                self.assert_exit(result, 0)
                help_text = result.stdout + result.stderr
                self.assertIn("--json", help_text)
                self.assertRegex(help_text.lower(), r"usage")
                self.assertRegex(help_text.lower(), r"presence|job.level")
                self.assertRegex(help_text.lower(), r"read.only|dry.run")
                self.assertLess(len(help_text), 6000, "help should be concise")

    def test_mutating_or_production_flags_are_never_an_execution_mode(self):
        path = self.fixture(GOOD)
        for flag in ("--fix", "--apply", "--prod"):
            with self.subTest(flag=flag):
                _, report = self.pair(flag, path, exit_code=2, checked=0)
                self.assertEqual(report["findings"], [])
                self.assert_error(report, "invalid_arguments")


class PresenceOnlyRegressionCLI(CLIFixture):
    def test_permission_maps_do_not_imply_scope_minimization_or_validation(self):
        for permission in ("{}", "{contents: read}", "{contents: write, id-token: write}",
                           "{unrecognized-scope: deliberately-not-validated}"):
            with self.subTest(permission=permission):
                self.pair(self.fixture(GOOD.replace("permissions: {}", "permissions: " + permission)),
                          exit_code=0, checked=1)

    def test_scalar_and_sequence_permissions_still_need_an_explicit_mapping(self):
        for permission in ("null", "read-all", "write-all", "[]", "false", '"{}"'):
            with self.subTest(permission=permission):
                path = self.fixture(GOOD.replace("permissions: {}", "permissions: " + permission))
                _, report = self.pair(path, exit_code=1, checked=1)
                self.assertEqual(self.identities(report), [(str(path), "build", 2)])

    def test_top_level_step_env_comment_and_script_keys_do_not_satisfy_a_job(self):
        fixtures = (
            "permissions: {}\njobs:\n  build:\n    steps: []\n",
            "jobs:\n  build:\n    steps:\n      - permissions: {}\n        run: echo local\n",
            "jobs:\n  build:\n    env:\n      permissions: '{}'\n    steps: []\n",
            "jobs:\n  build:\n    # permissions: {}\n    steps: []\n",
            "jobs:\n  build:\n    steps:\n      - run: |\n          permissions: {}\n          jobs: {fake: {permissions: {}}}\n",
        )
        for text in fixtures:
            with self.subTest(text=text):
                path = self.fixture(text)
                _, report = self.pair(path, exit_code=1, checked=1)
                self.assertEqual(self.identities(report), [(str(path), "build", 3 if text.startswith("permissions:") else 2)])

    def test_explicit_permission_alias_remains_valid(self):
        alias = "x-scopes: &scopes {contents: read}\njobs:\n  build:\n    permissions: *scopes\n"
        self.pair(self.fixture(alias), exit_code=0, checked=1)

    def test_merged_defaults_are_not_an_explicit_job_declaration(self):
        merge = "x-defaults: &defaults {permissions: {}}\njobs:\n  build:\n    <<: *defaults\n    steps: []\n"
        path = self.fixture(merge)
        _, report = self.pair(path, exit_code=1, checked=1)
        self.assertEqual(self.identities(report), [(str(path), "build", 3)])

    def test_reusable_callers_are_checked_locally_without_fetching_the_callee(self):
        for declaration, code in (("", 1), ("    permissions: {}\n", 0)):
            with self.subTest(declaration=declaration):
                text = "jobs:\n  call:\n    uses: example/offline/.github/workflows/reusable.yml@v1\n" + declaration
                path = self.fixture(text)
                _, report = self.pair(path, exit_code=code, checked=1)
                self.assertEqual(self.identities(report), [(str(path), "call", 2)] if code else [])

    def test_ambiguous_duplicate_job_identity_remains_invalid(self):
        for text in ('jobs: {build: {}, "build": {permissions: {}}}\n',
                     "jobs: {hidden: {}}\njobs: {good: {permissions: {}}}\n",
                     'jobs: {build: {permissions: {}, "permissions": null}}\n'):
            with self.subTest(text=text):
                path = self.fixture(text)
                _, report = self.pair(path, exit_code=2, checked=0, unmeasurable=1)
                self.assert_error(report, "invalid_workflow", path)


if __name__ == "__main__":
    unittest.main()
