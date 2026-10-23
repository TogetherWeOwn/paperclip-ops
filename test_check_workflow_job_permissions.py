#!/usr/bin/env python3
"""Acceptance contract for explicit job-level workflow permissions.

Run with: python3 -m unittest -v test_check_workflow_job_permissions
The checker is intentionally not supplied by this test-only change. Its absence
is RED, never a skip. Fixtures and CLI expectations come from the gate contract,
not its implementation. All writes, including the live deletion mutant, stay in
scratch; the checked-in workflows are never edited by this suite.
"""
import importlib.util
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
COMPLIANT = """name: Acceptance
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    permissions: {}
    steps:
      - run: echo acceptance
"""


class PermissionTestCase(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get(
            "PAPERCLIP_SCRATCH_DIR"
        )
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-permissions-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)

    def fixture(self, text, name="fixture.yml"):
        path = self.cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def require_checker(self):
        self.assertTrue(
            CHECKER.is_file(),
            "baseline RED: check_workflow_job_permissions.py has not been implemented",
        )

    def check_file(self, path):
        self.require_checker()
        spec = importlib.util.spec_from_file_location("check_workflow_job_permissions", CHECKER)
        checker = importlib.util.module_from_spec(spec)
        # Match normal import semantics without keeping state between fixtures.
        previous = sys.modules.get(spec.name)
        sys.modules[spec.name] = checker
        try:
            spec.loader.exec_module(checker)
            findings, error = checker.check_file(path)
        finally:
            if previous is None:
                sys.modules.pop(spec.name, None)
            else:
                sys.modules[spec.name] = previous
        self.assertIsInstance(findings, list)
        return findings, error

    def cli(self, *paths, cwd=None):
        self.require_checker()
        return subprocess.run(
            [sys.executable, str(CHECKER), *(str(path) for path in paths)],
            cwd=cwd or self.cwd,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def assert_exit(self, result, expected):
        self.assertEqual(
            result.returncode, expected,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def assert_findings(self, text, expected):
        path = self.fixture(text)
        findings, error = self.check_file(path)
        self.assertFalse(error, error)
        self.assertEqual(findings, expected)
        result = self.cli(path)
        self.assert_exit(result, 1 if expected else 0)
        for _, job in expected:
            diagnostic = result.stdout + result.stderr
            self.assertIn(path.name, diagnostic)
            self.assertIn(job, diagnostic)

    def assert_invalid(self, text):
        path = self.fixture(text)
        _, error = self.check_file(path)
        self.assertTrue(error, "invalid workflow must return an error, not missing-permission findings")
        result = self.cli(path)
        self.assert_exit(result, 2)
        self.assertIn(path.name, result.stdout + result.stderr)


class WorkflowPermissionFixtures(PermissionTestCase):
    def test_explicit_empty_mapping_grants_nothing(self):
        self.assert_findings(COMPLIANT, [])

    def test_explicit_scoped_read_write_none_maps(self):
        for permissions in (
            "{contents: read}",
            "{contents: read, packages: write, actions: none}",
            "{contents: none, id-token: write}",
            "\n      contents: read\n      pull-requests: write\n      actions: none",
        ):
            with self.subTest(permissions=permissions):
                self.assert_findings(COMPLIANT.replace("permissions: {}", f"permissions: {permissions}"), [])

    def test_top_level_permissions_do_not_satisfy_a_job(self):
        self.assert_findings(
            "permissions: {contents: read}\njobs:\n  build:\n    runs-on: ubuntu-latest\n",
            [(3, "build")],
        )

    def test_all_jobs_are_reported_with_one_based_key_lines(self):
        self.assert_findings(
            "name: Acceptance\njobs:\n  first:\n    runs-on: ubuntu-latest\n"
            "  compliant:\n    permissions: {}\n"
            "  last:\n    uses: ./.github/workflows/reusable.yml\n",
            [(3, "first"), (7, "last")],
        )

    def test_non_mapping_permissions_are_missing_declarations(self):
        for value in ("", "null", "~", "[]", "[contents]", "read-all", "write-all", "false", "0", '"{}"'):
            with self.subTest(value=value):
                self.assert_findings(COMPLIANT.replace("permissions: {}", f"permissions: {value}"), [(4, "build")])

    def test_reusable_callers_also_need_an_explicit_map(self):
        for declaration, expected in (("", [(2, "call")]), ("    permissions: {}\n", [])):
            with self.subTest(declaration=declaration):
                self.assert_findings(
                    "jobs:\n  call:\n    uses: ./.github/workflows/reusable.yml\n" + declaration,
                    expected,
                )

    def test_quoted_job_keys_have_decoded_names_and_key_lines(self):
        self.assert_findings(
            'jobs:\n  "quoted-build":\n    runs-on: ubuntu-latest\n'
            "  'quoted-test': {runs-on: ubuntu-latest}\n",
            [(2, "quoted-build"), (4, "quoted-test")],
        )

    def test_fully_inline_workflow_mapping_is_understood(self):
        self.assert_findings(
            '{jobs: {good: {permissions: {}}, "inline-build": {runs-on: ubuntu-latest}}}\n',
            [(1, "inline-build")],
        )

    def test_multiple_inline_jobs_can_share_a_line(self):
        self.assert_findings(
            'jobs: {first: {}, good: {permissions: {}}, last: {permissions: null}}\n',
            [(1, "first"), (1, "last")],
        )

    def test_inline_job_permission_mapping_can_be_nonempty(self):
        self.assert_findings(
            'jobs: {build: {permissions: {contents: read, packages: write, actions: none}}}\n', [],
        )

    def test_alias_as_an_explicit_permission_mapping_is_valid(self):
        self.assert_findings(
            "x-scopes: &scopes {contents: read}\njobs:\n  build:\n    permissions: *scopes\n", [],
        )

    def test_step_permission_mapping_cannot_masquerade_as_job_permissions(self):
        self.assert_findings(
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: Not a job declaration\n        permissions: {contents: read}\n"
            "        run: echo acceptance\n",
            [(2, "build")],
        )

    def test_run_string_cannot_masquerade_as_job_permissions(self):
        self.assert_findings(
            "jobs:\n  build:\n    steps:\n      - run: |\n"
            "          permissions:\n            contents: read\n"
            "          jobs:\n            pretend: {permissions: {}}\n",
            [(2, "build")],
        )

    def test_comments_and_nested_env_permissions_do_not_count(self):
        self.assert_findings(
            "jobs:\n  build:\n    # permissions: {}\n    env:\n"
            "      permissions: '{contents: read}'\n    steps: []\n",
            [(2, "build")],
        )

    def test_later_top_level_keys_are_not_jobs(self):
        self.assert_findings(
            COMPLIANT + "permissions: {contents: read}\nenv: {MARKER: acceptance}\n"
            "concurrency: {group: acceptance}\ndefaults: {run: {shell: bash}}\n", [],
        )

    def test_empty_or_non_mapping_workflow_is_invalid(self):
        for text in ("", "# only a comment\n", "null\n", "[]\n", "workflow\n", "42\n"):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_empty_missing_or_non_mapping_jobs_are_invalid(self):
        for text in (
            "name: no-jobs\n", "jobs:\n", "jobs: null\n", "jobs: ~\n",
            "jobs: {}\n", "jobs: []\n", "jobs: build\n",
        ):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_non_mapping_job_body_is_invalid(self):
        for body in ("null", "[]", "build"):
            with self.subTest(body=body):
                self.assert_invalid(f"jobs:\n  build: {body}\n")

    def test_malformed_yaml_is_invalid_even_after_a_compliant_job(self):
        for text in ("jobs: [unterminated\n", COMPLIANT + "env: [unterminated\n"):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_multiple_yaml_documents_fail_closed(self):
        self.assert_invalid(COMPLIANT + "---\n" + COMPLIANT)

    def test_duplicate_jobs_key_fails_closed(self):
        self.assert_invalid("jobs: {hidden: {}}\njobs: {good: {permissions: {}}}\n")

    def test_duplicate_job_names_fail_closed(self):
        for text in (
            'jobs:\n  build: {}\n  "build": {permissions: {}}\n',
            "jobs: {build: {}, build: {permissions: {}}}\n",
        ):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_duplicate_job_permission_keys_fail_closed(self):
        for text in (
            "jobs:\n  build:\n    permissions: null\n    permissions: {}\n",
            'jobs: {build: {permissions: {}, "permissions": {contents: read}}}\n',
        ):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_duplicate_scope_keys_fail_closed(self):
        for text in (
            "jobs:\n  build:\n    permissions:\n      contents: read\n      contents: write\n",
            "jobs: {build: {permissions: {contents: read, contents: write}}}\n",
        ):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_duplicate_top_level_permission_keys_fail_closed(self):
        self.assert_invalid("permissions: {}\npermissions: {contents: read}\n" + COMPLIANT)

    def test_undefined_alias_is_invalid(self):
        self.assert_invalid("jobs: {build: {permissions: *undefined}}\n")


class WorkflowPermissionCLI(PermissionTestCase):
    def test_explicit_path_outside_workflow_directory(self):
        path = self.fixture(COMPLIANT, "elsewhere/custom-workflow.yaml")
        self.fixture("not: [yaml", ".github/workflows/unselected.yml")
        self.assert_exit(self.cli(path), 0)

    def test_multiple_explicit_paths_report_the_missing_job(self):
        good = self.fixture(COMPLIANT, "good.yml")
        bad = self.fixture("jobs: {forgotten-job: {runs-on: ubuntu-latest}}\n", "bad.yaml")
        for paths in ((good, bad), (bad, good)):
            with self.subTest(paths=paths):
                result = self.cli(*paths)
                self.assert_exit(result, 1)
                self.assertIn(bad.name, result.stdout + result.stderr)
                self.assertIn("forgotten-job", result.stdout + result.stderr)

    def test_relative_explicit_path_resolves_from_cwd(self):
        self.fixture(COMPLIANT, "relative.yml")
        self.assert_exit(self.cli("relative.yml"), 0)

    def test_default_discovery_checks_new_yml_and_yaml_files(self):
        self.fixture(COMPLIANT, ".github/workflows/existing.yml")
        self.assert_exit(self.cli(), 0)
        for extension in ("yml", "yaml"):
            with self.subTest(extension=extension):
                # Add a new filename AFTER a passing invocation. A fixed list
                # or discovery cached outside cwd must not miss it.
                path = self.fixture(
                    "jobs:\n  newly-added-job: {runs-on: ubuntu-latest}\n"
                    "env: {MARKER: after-jobs}\npermissions: {contents: read}\n",
                    f".github/workflows/newly-added.{extension}",
                )
                result = self.cli()
                self.assert_exit(result, 1)
                self.assertIn(path.name, result.stdout + result.stderr)
                self.assertIn("newly-added-job", result.stdout + result.stderr)
                path.write_text(COMPLIANT, encoding="utf-8")
                self.assert_exit(self.cli(), 0)

    def test_default_discovery_is_direct_and_ignores_other_extensions(self):
        self.fixture(COMPLIANT, ".github/workflows/new-name.yaml")
        for name in ("nested/not-a-workflow.yml", "notes.txt", "backup.yml.bak"):
            self.fixture("jobs: [broken", ".github/workflows/" + name)
        self.assert_exit(self.cli(), 0)

    def test_default_discovery_reports_invalid_new_yaml(self):
        self.fixture(COMPLIANT, ".github/workflows/existing.yml")
        bad = self.fixture("jobs: [broken", ".github/workflows/new-invalid.yaml")
        result = self.cli()
        self.assert_exit(result, 2)
        self.assertIn(bad.name, result.stdout + result.stderr)

    def test_missing_file_and_directory_return_read_errors(self):
        directory = self.cwd / "directory.yml"
        directory.mkdir()
        for path in (self.cwd / "absent.yml", directory):
            with self.subTest(path=path):
                _, error = self.check_file(path)
                self.assertTrue(error)
                result = self.cli(path)
                self.assert_exit(result, 2)
                self.assertIn(path.name, result.stdout + result.stderr)

    def test_invalid_utf8_is_an_error_not_a_traceback_or_pass(self):
        path = self.cwd / "unreadable.yaml"
        path.write_bytes(b"jobs: {build: {permissions: {}}}\n\xff")
        _, error = self.check_file(path)
        self.assertTrue(error)
        result = self.cli(path)
        self.assert_exit(result, 2)
        self.assertIn(path.name, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_unavailable_yaml_parser_returns_exit_two(self):
        self.require_checker()
        path = self.fixture(COMPLIANT)
        # Fault-inject only the dependency import in a fresh interpreter. The
        # production CLI still parses argv, handles the error and sets its exit.
        deny_yaml = """import builtins, runpy, sys
original_import = builtins.__import__
def import_without_yaml(name, *args, **kwargs):
    if name == 'yaml' or name.startswith('yaml.'):
        raise ImportError('acceptance: YAML parser unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_without_yaml
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        result = subprocess.run(
            [sys.executable, "-c", deny_yaml, str(CHECKER), str(path)],
            cwd=self.cwd, text=True, capture_output=True, timeout=15,
        )
        self.assert_exit(result, 2)
        self.assertRegex((result.stdout + result.stderr).lower(), "yaml|parser")
        self.assertNotIn("Traceback", result.stderr)


class LiveWorkflowPermissions(PermissionTestCase):
    def workflow_files(self):
        directory = ROOT / ".github/workflows"
        paths = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
        self.assertTrue(paths, "live-workflow coverage must not be vacuous")
        return paths

    def test_all_live_workflows_have_explicit_job_permissions(self):
        self.workflow_files()
        self.assert_exit(self.cli(cwd=ROOT), 0)

    def test_live_deletion_mutation_fails_and_names_the_job(self):
        # Copy the actual workflows, then remove exactly one existing block
        # declaration using YAML source marks, not a synthetic replacement.
        selected = None
        for source in self.workflow_files():
            destination = self.cwd / ".github/workflows" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if selected is not None:
                continue
            document = yaml.compose(source.read_text(encoding="utf-8"))
            jobs = next(value for key, value in document.value if key.value == "jobs")
            for job, body in jobs.value:
                for key, value in body.value:
                    if key.value == "permissions" and isinstance(value, yaml.MappingNode):
                        # Only a stand-alone block declaration can be removed
                        # linewise without also deleting inline job fields.
                        line = source.read_text(encoding="utf-8").splitlines()[key.start_mark.line]
                        if line.strip().startswith("permissions:"):
                            selected = (destination, job.value, key, value)
                            break
                if selected is not None:
                    break
        self.assertIsNotNone(selected, "need a real explicit permission declaration to delete")
        path, job_name, key, value = selected
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        end = value.end_mark.line
        if value.flow_style and value.end_mark.column:
            end += 1  # Include the closing line of `permissions: {}`.
        mutated = "".join(lines[:key.start_mark.line] + lines[end:])
        self.assertNotEqual(mutated, original)
        path.write_text(mutated, encoding="utf-8")
        result = self.cli(cwd=self.cwd)
        path.write_text(original, encoding="utf-8")
        pristine = self.cli(cwd=self.cwd)
        self.assert_exit(pristine, 0)
        self.assert_exit(result, 1)
        self.assertIn(path.name, result.stdout + result.stderr)
        self.assertIn(job_name, result.stdout + result.stderr)
        self.assertEqual((ROOT / ".github/workflows" / path.name).read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
