#!/usr/bin/env python3
"""Immutable CLI acceptance for machine-readable required-check audit outcomes.

Run: python3 -B -m unittest -v test_required_checks_unmeasurable_json_acceptance

Consumer contract for the new unmeasurable envelope (all fields top-level):
  schema_version: 1; outcome: "unmeasurable"; measurement_state: "unmeasured";
  error_code: one of the codes in UnmeasurableJSON; retryable: false;
  next_action: an actionable repair instruction; branch: the known branch;
  source: {"kind": "manifest" | "rules-json", "path": the supplied path}.
Counts must be absent/null, not a fabricated zero-finding measurement. Existing
measured branch/required/counts/findings remain compatible; additive fields are
allowed. Three families are exercised: measured pass, measured findings and
unmeasurable. Parser syntax errors deliberately remain argparse stderr/exit 2,
with no JSON stdout (unknown flag, missing value/source, conflicting sources).

This pins the external contract, not implementation helpers. No implementation
imports, live --repo calls or changed helper signatures. The real script is
executed by Python in a credential-free child with a sitecustomize audit guard.
The guard refuses writes, sockets and external processes before they occur;
PATH contains only poison commands. Inputs are fingerprinted around each run.
The Builder must import this file unchanged, wire it into the existing required
verification inventory/CI contract, and supply docs/help in the implementation PR.
"""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


HERE = Path(__file__).resolve().parent
TOOL = HERE / "ci_required_checks_audit.py"
BRANCH = "release/acceptance"
DENIED = "ACCEPTANCE_SIDE_EFFECT_DENIED:"

# Test-only containment, loaded before the actual CLI entrypoint. A denial is
# also visible on stderr even if the program catches PermissionError.
GUARD = r'''
import os
import sys

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_MUTATIONS = {
    "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.link", "os.symlink",
    "os.chmod", "os.chown", "os.truncate", "os.utime", "os.putenv",
    "os.unsetenv", "shutil.copyfile", "shutil.copymode", "shutil.copystat",
    "ctypes.dlopen",
}

def deny(event):
    os.write(2, ("ACCEPTANCE_SIDE_EFFECT_DENIED:" + event + "\n").encode())
    raise PermissionError("offline acceptance forbids " + event)

def guard(event, args):
    if event == "open":
        mode, flags = args[1:3]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        ):
            deny(event)
    if event in _MUTATIONS or event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")):
        deny(event)
    if event in {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.startfile"}:
        deny(event)

sys.addaudithook(guard)
'''


class OfflineCLI(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="required-checks-acceptance-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.guard_dir = self.root / "guard"
        self.guard_dir.mkdir()
        self.write("guard/sitecustomize.py", GUARD)
        for name in ("gh", "git", "curl", "wget", "sh", "bash"):
            stub = self.write(
                "bin/" + name,
                "#!/bin/sh\nprintf 'POISON_COMMAND_CALLED\\n' >&2\nexit 97\n",
            )
            stub.chmod(0o755)
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "PYTHONPATH": str(self.guard_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "LC_ALL": "C.UTF-8",
        }
        self.workflows = self.root / "workflows"
        self.workflows.mkdir()
        self.write(
            "workflows/unit.yml",
            "on: pull_request\njobs:\n  unit:\n    name: unit\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - run: true\n",
        )
        self.manifest = self.write_json(
            "manifest.json",
            {"branch": BRANCH, "required": [{"context": "unit", "integration_id": 15368}]},
        )
        self.rules = self.write_json(
            "rules.json",
            [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "unit", "integration_id": 15368}],
            }}],
        )

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def write_json(self, relative, value):
        return self.write(relative, json.dumps(value))

    def fingerprint(self):
        return {
            str(p.relative_to(self.root)): (
                p.stat().st_mode,
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None,
            )
            for p in self.root.rglob("*")
        }

    def child(self, arguments):
        before = self.fingerprint()
        proc = subprocess.run(
            [sys.executable, "-B", *map(str, arguments)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(self.fingerprint(), before, "CLI mutated offline scratch inputs")
        return proc

    def run_cli(self, *args):
        proc = self.child([TOOL, *args, "--gh-bin", self.bin / "gh"])
        self.assertNotIn(DENIED, proc.stderr, "offline CLI attempted a forbidden side effect")
        self.assertNotIn("POISON_COMMAND_CALLED", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr, proc.stderr)
        return proc

    def source_args(self, kind="manifest", path=None, workflows=True, branch=True):
        args = ["--" + kind, path or (self.manifest if kind == "manifest" else self.rules)]
        if workflows:
            args += ["--workflows", self.workflows]
        if branch:
            args += ["--branch", BRANCH]
        return args

    def parse_object(self, proc):
        try:
            # loads rejects extra objects, trailing prose, and whitespace-only output.
            doc = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail(f"expected exactly one JSON object, got {proc.stdout!r}; stderr={proc.stderr!r}: {exc}")
        self.assertIsInstance(doc, dict, "JSON result must be an object, not an array/scalar")
        return doc

    def assert_unmeasurable(self, args, code, kind, path, target):
        text = self.run_cli(*args)
        machine = self.run_cli(*args, "--json")
        self.assertEqual(text.returncode, 2, text.stdout + text.stderr)
        self.assertEqual(machine.returncode, text.returncode, machine.stdout + machine.stderr)
        self.assertEqual(text.stdout, "", "unmeasured text diagnostics stay on stderr")
        self.assertTrue(text.stderr.startswith("ci_required_checks_audit:"), text.stderr)
        doc = self.parse_object(machine)
        self.assertEqual(machine.stderr, "", "JSON diagnostics belong in the envelope")
        self.assertEqual(type(doc.get("schema_version")), int)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc.get("outcome"), "unmeasurable")
        self.assertEqual(doc.get("measurement_state"), "unmeasured")
        self.assertEqual(doc.get("error_code"), code)
        self.assertIs(doc.get("retryable"), False, "these local input repairs are not silent retries")
        self.assertEqual(doc.get("source"), {"kind": kind, "path": str(path)})
        self.assertEqual(doc.get("branch"), BRANCH)
        self.assertIsNone(doc.get("counts"), "no measurement means no fabricated zero counts")
        self.assertIn(doc.get("findings"), (None, []), "input failure is not workflow evidence")
        action = doc.get("next_action")
        self.assertIsInstance(action, str)
        words = set(re.findall(r"[a-z]+", action.lower()))
        self.assertTrue(words & {"provide", "supply", "fix", "repair", "check", "correct", "add", "restore", "select", "verify", "pass", "use", "ensure", "specify"}, action)
        self.assertIn(target, action.lower(), "next_action must identify what needs repair")
        # Same invocation twice: stable code/version/source/branch, no retry policy drift.
        repeated = self.run_cli(*args, "--json")
        self.assertEqual(repeated.returncode, 2)
        self.assertEqual(repeated.stderr, "")
        self.assertEqual(self.parse_object(repeated), doc)
        return doc


class UnmeasurableJSON(OfflineCLI):
    def test_manifest_missing_or_unreadable_has_stable_repair(self):
        for path in (self.root / "missing.json", self.root / "directory"):
            with self.subTest(path=path.name):
                if path.name == "directory":
                    path.mkdir()
                self.assert_unmeasurable(self.source_args(path=path), "MANIFEST_UNREADABLE", "manifest", path, "manifest")

    def test_manifest_malformed_or_wrong_shaped_has_stable_repair(self):
        for name, content in (
            ("malformed", "{"),
            ("missing-list", "{}"),
            ("object-list", '{"required": {}}'),
            ("string-list", '{"required": "unit"}'),
            ("missing-context", '{"required": [{"integration_id": 15368}]}'),
        ):
            with self.subTest(case=name):
                path = self.write(name + ".json", content)
                self.assert_unmeasurable(self.source_args(path=path), "MANIFEST_INVALID", "manifest", path, "manifest")

    def test_rules_missing_or_unreadable_has_stable_repair(self):
        for path in (self.root / "missing-rules.json", self.root / "rules-directory"):
            with self.subTest(path=path.name):
                if path.name == "rules-directory":
                    path.mkdir()
                self.assert_unmeasurable(self.source_args("rules-json", path), "RULES_UNREADABLE", "rules-json", path, "rules")

    def test_rules_malformed_or_wrong_shaped_has_stable_repair(self):
        for name, content in (("malformed-rules", "{"), ("object-rules", '{"message": "Not Found"}')):
            with self.subTest(case=name):
                path = self.write(name + ".json", content)
                self.assert_unmeasurable(self.source_args("rules-json", path), "RULES_INVALID", "rules-json", path, "rules")

    def test_non_utf8_input_is_an_invalid_local_document(self):
        for kind, code, target in (
            ("manifest", "MANIFEST_INVALID", "manifest"),
            ("rules-json", "RULES_INVALID", "rules"),
        ):
            with self.subTest(source=kind):
                path = self.root / (kind + "-non-utf8.json")
                path.write_bytes(b"\xff\xfe\xff")
                self.assert_unmeasurable(self.source_args(kind, path), code, kind, path, target)

    def test_missing_workflows_argument_identifies_the_required_input(self):
        for kind, path in (("manifest", self.manifest), ("rules-json", self.rules)):
            with self.subTest(source=kind):
                self.assert_unmeasurable(self.source_args(kind, workflows=False), "WORKFLOWS_REQUIRED", kind, path, "--workflows")

    def test_empty_required_checks_are_unmeasured_not_a_clean_audit(self):
        manifest = self.write_json("empty-manifest.json", {"branch": BRANCH, "required": []})
        empty_rules = self.write_json("empty-rules.json", [])
        other_rules = self.write_json("other-rules.json", [{"type": "deletion"}])
        status_rules = self.write_json("empty-status-rules.json", [{"type": "required_status_checks", "parameters": {"required_status_checks": []}}])
        for kind, path in (("manifest", manifest), ("rules-json", empty_rules), ("rules-json", other_rules), ("rules-json", status_rules)):
            with self.subTest(source=kind, path=path.name):
                self.assert_unmeasurable(self.source_args(kind, path), "NO_REQUIRED_CHECKS", kind, path, "required")

    def test_zero_workflow_inventory_is_unmeasured_for_both_sources(self):
        empty = self.root / "empty-workflows"
        empty.mkdir()
        self.write("empty-workflows/README.md", "Not a workflow.\n")
        for directory in (empty, self.root / "absent-workflows"):
            for kind, path in (("manifest", self.manifest), ("rules-json", self.rules)):
                with self.subTest(directory=directory.name, source=kind):
                    args = self.source_args(kind, workflows=False) + ["--workflows", directory]
                    self.assert_unmeasurable(args, "NO_WORKFLOWS", kind, path, "workflow")

    def test_known_manifest_branch_survives_without_an_override(self):
        args = self.source_args(workflows=False, branch=False)
        self.assert_unmeasurable(args, "WORKFLOWS_REQUIRED", "manifest", self.manifest, "--workflows")

    def test_strict_does_not_turn_unmeasured_into_a_findings_exit(self):
        for kind, path in (("manifest", self.manifest), ("rules-json", self.rules)):
            with self.subTest(source=kind):
                args = self.source_args(kind, workflows=False) + ["--strict"]
                self.assert_unmeasurable(args, "WORKFLOWS_REQUIRED", kind, path, "--workflows")

    def test_unknown_manifest_branch_is_not_invented_on_invalid_input(self):
        path = self.write("invalid-branch.json", "{")
        proc = self.run_cli(*self.source_args(path=path, branch=False), "--json")
        self.assertEqual(proc.returncode, 2)
        doc = self.parse_object(proc)
        self.assertEqual(doc.get("error_code"), "MANIFEST_INVALID")
        self.assertIsNone(doc.get("branch"), "unreadable metadata is not evidence of a branch")
        self.assertEqual(doc.get("source"), {"kind": "manifest", "path": str(path)})


class CompatibilityControls(OfflineCLI):
    def measured(self, kind="manifest", *extra):
        args = self.source_args(kind)
        text = self.run_cli(*args, *extra)
        proc = self.run_cli(*args, *extra, "--json")
        self.assertEqual(proc.returncode, text.returncode)
        self.assertEqual(proc.stderr, "")
        doc = self.parse_object(proc)
        self.assertEqual(doc["branch"], BRANCH)
        self.assertEqual(doc["required"], [{"context": "unit", "integration_id": 15368}])
        self.assertEqual(set(doc["counts"]), {"FAIL", "WARN", "INFO"})
        self.assertIsInstance(doc["findings"], list)
        for row in doc["findings"]:
            self.assertTrue({"severity", "code", "context", "detail"} <= row.keys())
        return proc.returncode, doc, text

    def test_measured_pass_keeps_fields_exit_zero_and_text(self):
        for kind in ("manifest", "rules-json"):
            with self.subTest(source=kind):
                rc, doc, text = self.measured(kind)
                self.assertEqual(rc, 0)
                self.assertEqual(doc["counts"], {"FAIL": 0, "WARN": 0, "INFO": 0})
                self.assertEqual(doc["findings"], [])
                self.assertEqual(text.stderr, "")
                self.assertEqual(text.stdout, f"required checks: 1 on {BRANCH}; workflows read: 1; unparseable: 0\nresult: 0 FAIL, 0 WARN, 0 INFO\n")

    def test_measured_missing_producer_stays_a_finding_not_input_error(self):
        self.write("workflows/unit.yml", "on: pull_request\njobs:\n  gone:\n    name: gone\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n")
        for kind in ("manifest", "rules-json"):
            with self.subTest(source=kind):
                rc, doc, text = self.measured(kind)
                self.assertEqual(rc, 1)
                self.assertEqual(doc["counts"], {"FAIL": 1, "WARN": 0, "INFO": 1})
                self.assertEqual({(f["severity"], f["code"], f["context"]) for f in doc["findings"]}, {("FAIL", "NO_PRODUCER", "unit"), ("INFO", "NOT_REQUIRED", "gone")})
                self.assertIn("NO_PRODUCER", text.stdout)
                self.assertEqual(text.stderr, "")

    def test_warning_keeps_findings_and_strict_only_changes_the_exit(self):
        self.write("workflows/unit.yml", "on: pull_request\njobs:\n  unit:\n    name: unit\n    if: github.event_name == 'push'\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n")
        for kind in ("manifest", "rules-json"):
            with self.subTest(source=kind):
                rc, normal, text = self.measured(kind)
                strict_rc, strict, strict_text = self.measured(kind, "--strict")
                self.assertEqual(rc, 0)
                self.assertEqual(strict_rc, 1)
                self.assertEqual(normal["counts"], {"FAIL": 0, "WARN": 1, "INFO": 0})
                self.assertEqual(strict["counts"], normal["counts"])
                self.assertEqual(strict["findings"], normal["findings"])
                self.assertEqual([(f["severity"], f["code"], f["context"]) for f in normal["findings"]], [("WARN", "JOB_CAN_SKIP", "unit")])
                self.assertIn("JOB_CAN_SKIP", text.stdout)
                self.assertEqual(text.stdout, strict_text.stdout)

    def test_invalid_workflow_remains_measured_unparseable_exit_one(self):
        self.write("workflows/unit.yml", "on: pull_request\n")
        rc, doc, _ = self.measured()
        self.assertEqual(rc, 1)
        self.assertIn("UNPARSEABLE", {row["code"] for row in doc["findings"]})
        self.assertGreater(doc["counts"]["FAIL"], 0)

    def test_parser_syntax_errors_remain_argparse_stderr_exit_two(self):
        for args in (
            [],
            ["--manifest"],
            ["--manifest", self.manifest, "--unknown-acceptance-flag"],
            ["--manifest", self.manifest, "--rules-json", self.rules],
        ):
            with self.subTest(args=args):
                proc = self.run_cli(*args, "--json")
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertIn("usage:", proc.stderr)
                self.assertIn("error:", proc.stderr)

    def test_text_input_errors_keep_the_existing_diagnostic(self):
        for kind, path, expected in (
            ("manifest", self.root / "missing.json", "cannot read manifest"),
            ("rules-json", self.root / "missing-rules.json", "cannot read rules JSON"),
        ):
            with self.subTest(source=kind):
                proc = self.run_cli(*self.source_args(kind, path))
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(proc.stderr.startswith("ci_required_checks_audit: " + expected), proc.stderr)
                self.assertIn(str(path), proc.stderr)

    def test_explicit_branch_overrides_manifest_and_rules_default(self):
        for kind in ("manifest", "rules-json"):
            with self.subTest(source=kind):
                args = self.source_args(kind, branch=False)
                proc = self.run_cli(*args, "--json", "--branch", "other/branch")
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(self.parse_object(proc)["branch"], "other/branch")


class ContainmentControls(OfflineCLI):
    def test_guard_rejects_external_processes_before_the_poison_gh_runs(self):
        proc = self.child(["-c", "import subprocess; subprocess.run(['gh', 'api', 'unexpected'])"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(DENIED + "subprocess.Popen", proc.stderr)
        self.assertNotIn("POISON_COMMAND_CALLED", proc.stderr)

    def test_guard_rejects_network_before_socket_creation(self):
        proc = self.child(["-c", "import socket; socket.socket()"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(DENIED + "socket.__new__", proc.stderr)

    def test_guard_rejects_file_writes_before_mutation(self):
        proc = self.child(["-c", "open('forbidden-write', 'w').write('unexpected')"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(DENIED + "open", proc.stderr)
        self.assertFalse((self.root / "forbidden-write").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
