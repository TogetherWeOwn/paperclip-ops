#!/usr/bin/env python3
"""Public CI regression checks; no network, host inventory or credentials."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

import public_safety

ROOT = Path(__file__).resolve().parents[2]


class PublicCI(unittest.TestCase):
    def test_all_runner_routes_are_standard_hosted(self):
        routes = []
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            for line in workflow.read_text().splitlines():
                if re.match(r"\s*runs-on(?:-json)?:", line):
                    routes.append(line)
                    self.assertIn("ubuntu-latest", line, str(workflow))
                    self.assertNotIn("self-hosted", line, str(workflow))
        self.assertTrue(routes)

    def test_required_producers_handle_merge_groups(self):
        for name in ("ci.yml", "pr-lint.yml", "public-safety.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertRegex(text, r"(?m)^  merge_group:", name)

    def test_lint_attests_hosted_environment_not_runner_name(self):
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        self.assertIn('"${RUNNER_ENVIRONMENT:-}" == github-hosted', text)
        self.assertNotIn('case "${RUNNER_NAME:-}"', text)

    def test_lint_checkout_includes_public_regression_inputs(self):
        text = (ROOT / ".github/workflows/pr-lint.yml").read_text()
        # The suite reads tracked ZIP fixtures and the adapted mutation gate.
        # A .github/vendor-only sparse checkout silently omits both.
        self.assertNotIn("sparse-checkout:", text)
        self.assertIn("test_public_ci.py", text)

    def test_explicit_tool_grant_map_absence_is_not_skipped(self):
        for relative in ("tool_grant_expectations.json", "absent-explicit-map.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("test_case_6c24eff5_check12_reconcile.sh",
                             "public_snapshot_test_support.py"):
                    (root / name).write_bytes((ROOT / name).read_bytes())
                (root / "README.md").write_text("## Fresh-history publication candidate\n")
                (root / ".github/scripts").mkdir(parents=True)
                (root / ".github/scripts/public_safety.py").touch()
                (root / "org_access_review.sh").touch()
                result = subprocess.run(
                    ["bash", str(root / "test_case_6c24eff5_check12_reconcile.sh")],
                    env={"PATH": os.environ["PATH"], "TOOL_GRANT_MAP": str(root / relative)},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("no readable tool_grant_expectations.json", result.stderr)
                self.assertNotIn("SKIP:", result.stdout)

    def test_contract_dependency_is_restored_before_contract_tests(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        suites = text.split("\n  suites:\n", 1)[1].split("\n  private-mutation-gates:\n", 1)[0]
        restore = suites.index("      - name: Restore toolkit-vendored files\n")
        contract = suites.index("      - name: change-gating contract holds\n")
        self.assertLess(restore, contract)
        step = suites[restore:].split("      - ", 2)[1]
        self.assertIn("bash scripts/sync-toolkit.sh", step)
        self.assertNotRegex(step, r"(?m)^\s*if:")

    def test_obsolete_split_guards_apply_only_to_private_source(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        for name in ("Fail if the moved plugin path is touched",
                     "Fail if the freeze list drifted from its generator",
                     "Fail if a toolkit-owned path is touched"):
            step = text.split("      - name: " + name + "\n", 1)[1].split("      - ", 1)[0]
            self.assertIn("        if: github.event.repository.private\n", step)
        # Public publication still runs both secret and privacy scanners.
        scan = text.split("      - name: Scan tracked files for committed secrets\n", 1)[1].split("      - ", 1)[0]
        self.assertIn("./verification/secret-scan.sh", scan)
        self.assertNotRegex(scan, r"(?m)^\s*if:")
        safety = (ROOT / ".github/workflows/public-safety.yml").read_text()
        self.assertIn("gitleaks", safety)
        self.assertIn("public_safety.py", safety)

    def test_metadata_protocol_constant_does_not_exempt_private_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "fixture.json"
            with mock.patch.object(public_safety, "ROOT", root):
                path.write_text('{"deny_target": "169.254.169.254"}')
                self.assertEqual(public_safety.inspect(path), [])
                for octets in ((10, 255, 255, 254), (192, 168, 255, 254),
                               (169, 254, 169, 253)):
                    path.write_text(".".join(map(str, octets)))
                    self.assertIn("non-example private network address", public_safety.inspect(path))

    def test_public_artifact_env_exception_is_exact_path_and_bytes(self):
        pin = (
            "# Pinned upstream artifact. Update version, URL, and SHA-256 together.\n"
            "RUNNER_VERSION=2.337.0\n"
            "RUNNER_ARCHIVE=actions-runner-linux-x64-2.337.0.tar.gz\n"
            "RUNNER_URL=https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz\n"
            "RUNNER_SHA256=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "github-runner/actions-runner.env"
            path.parent.mkdir()
            with mock.patch.object(public_safety, "ROOT", root):
                path.write_text(pin)
                self.assertEqual(public_safety.inspect(path), [])
                path.write_text(pin + "UNREVIEWED_VARIABLE=fixture\n")
                self.assertIn("private artifact or live environment file", public_safety.inspect(path))
                other = root / "other.env"
                other.write_text(pin)
                self.assertIn("private artifact or live environment file", public_safety.inspect(other))

    def test_reviewed_zip_exception_requires_exact_path_bytes_and_public_payload(self):
        for relative, (_, member, public_name) in public_safety.PUBLIC_ZIP_FIXTURES.items():
            source = ROOT / relative
            raw = source.read_bytes()
            payload = (source.parent / public_name).read_bytes()
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / relative
                path.parent.mkdir(parents=True)
                public = path.parent / public_name
                public.write_bytes(payload)
                path.write_bytes(raw)
                with mock.patch.object(public_safety, "ROOT", root):
                    self.assertEqual(public_safety.inspect(path), [])
                    other = root / "other.zip"
                    other.write_bytes(raw)
                    self.assertIn("unreviewed binary payload", public_safety.inspect(other))
                    path.write_bytes(raw + b"unreviewed trailer")
                    self.assertIn("unreviewed binary payload", public_safety.inspect(path))
                    path.write_bytes(raw)
                    public.write_bytes(payload + b" ")
                    self.assertIn("unreviewed binary payload", public_safety.inspect(path))
                    public.unlink()
                    self.assertIn("unreviewed binary payload", public_safety.inspect(path))

    def test_unreviewed_zip_with_matching_public_json_is_still_refused(self):
        import zipfile
        relative, (_, member, public_name) = next(iter(public_safety.PUBLIC_ZIP_FIXTURES.items()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / relative
            path.parent.mkdir(parents=True)
            (path.parent / public_name).write_text('{}')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr(member, '{}')
            with mock.patch.object(public_safety, "ROOT", root):
                self.assertIn("unreviewed binary payload", public_safety.inspect(path))

    def test_private_artifact_skip_is_named_marked_and_absence_only(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "public_snapshot_test_support", ROOT / "public_snapshot_test_support.py")
        support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(support)
        for relative in support.PRIVATE_ARTIFACTS:
            for scanner in (False, True):
                for marker in (False, True):
                    for artifact in (None, "empty", "partial", "directory", "dangling-symlink"):
                        with self.subTest(relative=relative, scanner=scanner, marker=marker,
                                          artifact=artifact), tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            (root / "README.md").write_text(
                                "## Fresh-history publication candidate\n" if marker else "private source\n")
                            if scanner:
                                (root / ".github/scripts").mkdir(parents=True)
                                (root / ".github/scripts/public_safety.py").touch()
                            path = root / relative
                            path.parent.mkdir(parents=True, exist_ok=True)
                            if artifact == "empty":
                                path.touch()
                            elif artifact == "partial":
                                path.write_text("invalid partial JSON")
                            elif artifact == "directory":
                                path.mkdir()
                            elif artifact == "dangling-symlink":
                                path.symlink_to(root / "absent-target")
                            self.assertEqual(support.private_artifact_absent(root, relative),
                                             scanner and marker and artifact is None)
        with self.assertRaises(ValueError):
            support.private_artifact_absent(ROOT, "unreviewed-fixture.json")
        with self.assertRaises(ValueError):
            support.private_artifact_absent(ROOT, "../tests/dead-feed-2026-08-26.jsonl")

    def test_auth_mutation_gate_rejects_unattributable_results(self):
        import importlib.util
        import json
        spec = importlib.util.spec_from_file_location(
            "auth_mutation_gate", ROOT / "verification/auth-denial-mutation-gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        baseline = dict(success=True, run=55, failures=[], errors=[], skipped=[],
                        loader_errors=[], expected_failures=0, unexpected_successes=0)
        cases = [
            (False, (), {}, True),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS)}, True),
            (False, (), {"skipped": sorted(gate.PRIVATE_TESTS)}, False),
            (True, ("named-control",), {"skipped": sorted(gate.PRIVATE_TESTS)}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS) + ["unexpected"]}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS)[:-1]}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS), "errors": ["startup-error"]}, False),
            (True, (), {"skipped": sorted(gate.PRIVATE_TESTS), "run": 5}, False),
            (False, ("named-control",), {"run": 0}, False),
            (False, ("named-control",), {"loader_errors": ["missing test"]}, False),
            (False, ("named-control",), {"expected_failures": 1}, False),
            (False, ("named-control",), {"unexpected_successes": 1}, False),
        ]
        for public_absence, names, changes, accepted in cases:
            result = {**baseline, **changes}
            proc = subprocess.CompletedProcess([], 0, json.dumps(result), "")
            with self.subTest(public_absence=public_absence, names=names, changes=changes), \
                    mock.patch.object(gate, "private_artifact_absent", return_value=public_absence), \
                    mock.patch.object(gate.subprocess, "run", return_value=proc):
                if accepted:
                    self.assertEqual(gate.run_suite("unused", names), result)
                else:
                    with self.assertRaises(RuntimeError):
                        gate.run_suite("unused", names)

    def test_mutation_stage_preserves_exact_private_artifact_boundary(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from public_snapshot_test_support import (
            private_artifact_absent, stage_private_artifact)
        relative = "tests/dead-feed-2026-08-26.jsonl"
        for public in (False, True):
            for state in ("absent", "empty", "partial", "directory", "dangling"):
                with self.subTest(public=public, state=state), tempfile.TemporaryDirectory() as tmp:
                    source, stage = Path(tmp) / "source", Path(tmp) / "stage"
                    source.mkdir()
                    (source / "public_snapshot_test_support.py").write_text(
                        (ROOT / "public_snapshot_test_support.py").read_text())
                    if public:
                        (source / "README.md").write_text("## Fresh-history publication candidate\n")
                        marker = source / ".github/scripts/public_safety.py"
                        marker.parent.mkdir(parents=True)
                        marker.write_text("# actual source marker\n")
                    artifact = source / relative
                    artifact.parent.mkdir(parents=True)
                    if state in ("empty", "partial"):
                        artifact.write_text("" if state == "empty" else "incomplete\n")
                    elif state == "directory":
                        artifact.mkdir()
                    elif state == "dangling":
                        artifact.symlink_to("absent-target")
                    if state in ("directory", "dangling") or (state == "absent" and not public):
                        with self.assertRaises(OSError):
                            stage_private_artifact(source, stage, relative)
                    else:
                        skipped = stage_private_artifact(source, stage, relative)
                        self.assertEqual(skipped, public and state == "absent")
                        self.assertEqual(private_artifact_absent(stage, relative), skipped)
                        if not skipped:
                            self.assertEqual((stage / relative).read_bytes(), artifact.read_bytes())
                        else:
                            self.assertEqual((stage / ".github/scripts/public_safety.py").read_bytes(),
                                             (source / ".github/scripts/public_safety.py").read_bytes())
                    with self.assertRaises(ValueError):
                        stage_private_artifact(source, stage, "../unreviewed")

    def test_private_bakeoff_skip_requires_both_markers_and_complete_absence(self):
        source = (ROOT / "test_case_61df0b37_review_bakeoff.sh").read_text()
        cases = [
            (True, True, None, 0, True),
            (True, True, None, 1, False),  # harness failures cannot be hidden
            (False, True, None, 0, False),
            (True, False, None, 0, False),
            (False, False, None, 0, False),
            (True, True, "dataset", 0, False),
            (True, True, "results", 0, False),
            (True, True, "RESULTS-r1.md", 0, True),  # report is not its private inputs
        ]
        for scanner, marker, artifact, harness_rc, success in cases:
            with self.subTest(scanner=scanner, marker=marker, artifact=artifact,
                              harness_rc=harness_rc), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "suite.sh").write_text(source)
                (root / "README.md").write_text(
                    "## Fresh-history publication candidate\n" if marker else "private source\n")
                if scanner:
                    (root / ".github/scripts").mkdir(parents=True)
                    (root / ".github/scripts/public_safety.py").touch()
                if artifact:
                    path = root / "eval/review-bakeoff" / artifact
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("partial bundle\n")
                # Only the expensive harness is stubbed here. The real shell's
                # marker/absence predicates and nonpublic failure path execute.
                (root / "bin").mkdir()
                stub = root / "bin/python3"
                stub.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = -m ] && [ \"$2\" = unittest ]; then\n"
                    "  printf 'harness-called\\n'\n"
                    f"  exit {harness_rc}\n"
                    "fi\nexit 77\n")
                stub.chmod(0o755)
                proc = subprocess.run(
                    ["bash", str(root / "suite.sh")], cwd=root, text=True,
                    capture_output=True, timeout=15,
                    env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.defpath})
                self.assertIn("harness-called", proc.stdout)
                self.assertEqual(proc.returncode == 0, success, proc.stdout + proc.stderr)
                skipped = scanner and marker and artifact in (None, "RESULTS-r1.md")
                self.assertEqual("SKIP: historical private bake-off" in proc.stdout, skipped)
                if not skipped:
                    self.assertIn("FAIL dataset shape", proc.stdout)
                    self.assertIn("FAIL no token-shaped string", proc.stdout)

    def test_renamed_archive_sources_use_public_paths(self):
        manifest = (ROOT / "vendor/toolkit-files.txt").read_text().splitlines()
        expected = {
            "verification/case_9e76f76d-recorded-command.sh": "verification/recorded-monitor-command-gate.sh",
            "verification/case_9e76f76d-recorded-command.txt": "verification/recorded-monitor-command.txt",
            "verification/case_46e26fa6-mutation-gate.sh": "verification/plugin-deploy-drift-detector-mutation-gate.sh",
            "verification/case_f909ca1f-mutation-gate.sh": "verification/grant-attribution-resolver-mutation-gate.sh",
        }
        for dest, source in expected.items():
            self.assertIn(f"{dest}|{source}", manifest)


if __name__ == "__main__":
    unittest.main()
