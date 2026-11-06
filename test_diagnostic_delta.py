#!/usr/bin/env python3
"""Independent, immutable acceptance contract for one AX-lint identity ratchet.

Run: python3 -B -m unittest -v test_diagnostic_delta test_diagnostic_delta_workflow
The builder owns implementation and CI wiring, not edits to these test files.

Minimal public contract (not a generic diagnostic adapter framework):
  python3 scripts/quality-gates/diagnostic_delta.py --repo TREE --base FULL_SHA
      --head FULL_SHA
  diagnostic_delta.main(argv) -> exit code, one JSON object on stdout.

Exit/status: 0 clean, 1 finding, 2 unmeasurable; absent BASE policy is
0 not_configured with enforced=false (onboarding, NOT evidence of enforcement).
Configured reports use enforced=true, base_sha/head_sha/policy_sha, counts
{before, after, new, resolved}, sorted new/resolved identity lists, and coverage
{base: {skipped, unknown}, head: {skipped, unknown}}. Errors name a nonempty
code/next_action and boolean retryable. Counts refer to in-scope occurrences.

BASE's .diagnostic-delta.json is a strict JSON object with version=1,
producer="ax-lint", include=[repo-relative glob, ...] (1..64 entries). Unknown
fields, producers, versions, invalid paths and non-string globs fail closed.
** matches nested paths. HEAD's policy never changes this run's measurement.
Compare the supplied exact snapshots, NOT the working tree or merge base.

Identity = {rule_id, path, subject}; subject="file" for agents-map/* and
script-surface/no-json-or-dry-run. Workflow YAML is not a diagnostic input:
any selected .github/workflows/**/*.yml or *.yaml in either snapshot is
unmeasurable before producer execution, regardless of its contents or coverage.
The CI workflow remains an execution wrapper, not a measured diagnostic input.
Standalone producer dispatch controls below retain their independent contract.
Paths are canonical relative POSIX paths: reject absolute/dot/dot-dot/empty
segments and backslashes; preserve case and Unicode. Ignore report root,
line shifts, order and message/remediation prose.
No rename credit: even a byte-identical Git rename is resolved+new. Duplicate
identities are unmeasurable, never set-deduped or assigned unstable ordinals.
This deliberately cannot distinguish multiple file-subject occurrences.

Use the SAME trusted templates/org-github/ax-lint/ax_lint.py with --root TREE
--mode warn --json for both snapshots via subprocess.run, timeout <=30 seconds.
Validate the complete producer report (including coverage and summary), reject
unknown rules/unsafe identities and >1 MiB reports. Unsupported inputs and
unknown/unreadable coverage in scope are unmeasurable, even if unchanged.
A source-read failure during the real script scan must not become empty clean
coverage. The scanner is a bounded heuristic, not a semantic shell/Python parser
or proof that scripts implement their advertised flags. No scanned code executes.

Fault injection below changes producer process outcomes or a source read inside
the real producer scan; Git, extraction, policy validation, identity comparison
and JSON reporting stay real.
No implementation is imported at collection: missing code is an assertion
failure naming the absent deliverable, not a misleading import error.
"""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts/quality-gates/diagnostic_delta.py"
PRODUCER = ROOT / "templates/org-github/ax-lint/ax_lint.py"
POLICY = ".diagnostic-delta.json"
WORKFLOW_PATH = ".github/workflows/ship.yml"
SCRIPT_RULE = "script-surface/no-json-or-dry-run"
BAD_SCRIPT = "#!/usr/bin/env bash\n# deploy --target X\n"
GOOD_SCRIPT = "#!/usr/bin/env bash\n# usage: deploy --target X --json\n"
POLICY_VALUE = {"version": 1, "producer": "ax-lint",
                "include": ["AGENTS.md", "scripts/**"]}
WORKFLOW_POLICY = {**POLICY_VALUE,
                   "include": [*POLICY_VALUE["include"], ".github/workflows/**"]}

# These are opaque diagnostic inputs, not syntax for a new YAML recognizer.
# Keep the seven review families, including the typed flow false-positive case.
WORKFLOW_VARIANTS = {
    "bom": "﻿on:\n  workflow_dispatch:\n    inputs:\n      target:\n        description: destination\n",
    "indented_root": "  on:\n    workflow_dispatch:\n      inputs:\n        target:\n          description: destination\n",
    "next_line_scalar": 'on:\n  workflow_dispatch:\n    inputs:\n      target:\n        description:\n          "Choose target\n        type: string\n        done"\n',
    "anchored_quoted_scalar": 'on:\n  workflow_dispatch:\n    inputs:\n      target:\n        description: &desc "Choose target\n        type: string\n        done"\n',
    "anchored_block_scalar": 'jobs:\n  build:\n    steps:\n      - run: &shell |\n          echo "stage: \'ready"\non:\n  workflow_dispatch:\n    inputs:\n      target:\n        description: destination\n',
    "multiline_event_flow": "on: {\n  workflow_dispatch: {inputs: {target: {description: destination}}}\n  }\n",
    "next_line_event_flow": "on:\n  {workflow_dispatch: {inputs: {target: {description: destination}}}}\n",
    "next_line_dispatch_flow": "on:\n  workflow_dispatch:\n    {inputs: {target: {description: destination}}}\n",
    "whole_document_flow": '{"on": {"workflow_dispatch": {"inputs": {"target": {"description": "destination"}}}}}\n',
    "explicit_input_key": "on:\n  workflow_dispatch:\n    ? inputs\n    : target:\n        description: destination\n",
    "anchored_input_key": "on:\n  workflow_dispatch:\n    &inputs_key inputs:\n      target:\n        description: destination\n",
    "embedded_hash": "on: {push: {branches: [feature#1]}, workflow_dispatch: {inputs: {target: {description: destination}}}}\n",
    "typed_next_line_flow": "on:\n  workflow_dispatch:\n    inputs:\n      target:\n        {description: destination, type: string}\n",
    "no_dispatch": "name: tests\non: push\njobs: {}\n",
    "not_yaml": "not: [a complete YAML document\n",
    "not_utf8": b"\xff\xfe\x00",
}

SOURCE_READ_ERROR = r'''
import json
import os
from pathlib import Path
import runpy
import sys

producer, relative_path, fail_at, events = sys.argv[1:5]
sys.argv = [producer, *sys.argv[5:]]
root = Path(sys.argv[sys.argv.index("--root") + 1])
target = root / relative_path
reads = 0

def deny_source_read(event, args):
    global reads
    if event != "open" or not isinstance(args[0], (str, bytes)):
        return
    if Path(os.fsdecode(args[0])) == target and not args[2] & os.O_WRONLY:
        reads += 1
        if reads >= int(fail_at):
            with open(events, "a", encoding="utf-8") as log:
                log.write(json.dumps({"path": relative_path, "read": reads}) + "\n")
            raise PermissionError("fixture source read denied")

sys.addaudithook(deny_source_read)
runpy.run_path(producer, run_name="__main__")
'''


def dispatch(*names, typed=False, prefix="", comments=False):
    text = prefix + "name: ship\non:\n  workflow_dispatch:\n    inputs:\n"
    for name in names:
        text += f"      {name}:" + (" # incidental comment" if comments else "") + "\n"
        text += "        description: deployment target\n"
        if typed:
            text += "        type: string\n"
    return text


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="diagnostic-delta-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.put("AGENTS.md", "# map\n")
        self.put(POLICY, json.dumps(POLICY_VALUE))
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root,
                                       env=self.env, text=True, timeout=10).strip()

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m",
                 "test: fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>")
        return self.git("rev-parse", "HEAD")

    def producer_report(self, root=None):
        result = subprocess.run([sys.executable, "-I", "-B", str(PRODUCER),
                                 "--root", str(root or self.root), "--mode", "warn", "--json"],
                                capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def argv(self, base, head, repo=None):
        return ["--repo", str(repo or self.root), "--base", base, "--head", head]

    def check(self, exit_code, *, base=None, head=None, repo=None):
        self.assertTrue(SCRIPT.is_file(), "Missing implementation: " + str(SCRIPT.relative_to(ROOT)))
        base, head = base or self.base, head or self.commit()
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT),
                                 *self.argv(base, head, repo)],
                                capture_output=True, text=True, env=self.env, timeout=45)
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail("Expected one machine-readable report, got: " + repr(result.stdout + result.stderr))
        self.assertIsInstance(report, dict)
        if exit_code == 2:
            self.assert_error(report)
        else:
            self.assertEqual(report["base_sha"], base)
            self.assertEqual(report["head_sha"], head)
            self.assertEqual(report["policy_sha"], base)
            self.assertIs(report["enforced"], report["status"] != "not_configured")
        return report

    def assert_error(self, report):
        self.assertEqual(report["status"], "unmeasurable", report)
        for field in ("code", "next_action"):
            self.assertIsInstance(report[field], str)
            self.assertTrue(report[field].strip(), report)
        self.assertIsInstance(report["retryable"], bool)

    def assert_counts(self, report, before, after, new, resolved):
        self.assertEqual(report["counts"], {"before": before, "after": after,
                                           "new": new, "resolved": resolved})
        self.assertEqual(len(report["new"]), new)
        self.assertEqual(len(report["resolved"]), resolved)
        self.assertEqual(report["status"], "finding" if new else "clean")

    def identity(self, path, *, rule=SCRIPT_RULE, subject="file"):
        return {"rule_id": rule, "path": path, "subject": subject}

    def main_with_producer(self, transform=None, *, failure=None, failure_at=1,
                           producer_run=None):
        """Faults at the producer process/read boundary, never the comparison."""
        self.assertTrue(SCRIPT.is_file(), "Missing implementation: " + str(SCRIPT.relative_to(ROOT)))
        spec = importlib.util.spec_from_file_location("diagnostic_delta_acceptance", SCRIPT)
        gate = importlib.util.module_from_spec(spec)
        # Standard import semantics also support implementation dataclasses.
        with patch.dict(sys.modules, {spec.name: gate}):
            spec.loader.exec_module(gate)
        head = self.commit()
        real_run = subprocess.run
        calls = []

        def run(args, *positional, **kwargs):
            if isinstance(args, (list, tuple)) and any(
                    Path(str(arg)).name == "ax_lint.py" for arg in args):
                calls.append((list(args), kwargs))
                if failure is not None and len(calls) == failure_at:
                    raise failure
                result = (producer_run or real_run)(args, *positional, **kwargs)
                stdout, returncode = transform(result.stdout, len(calls)) if transform else (result.stdout, result.returncode)
                return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=result.stderr)
            return real_run(args, *positional, **kwargs)

        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(subprocess, "run", side_effect=run):
            with redirect_stdout(output):
                code = gate.main(self.argv(self.base, head))
        self.assertTrue(calls, "Contract requires the pinned AX producer subprocess, not a second scanner")
        report = json.loads(output.getvalue())
        return code, report, calls

    @staticmethod
    def rewrite_report(stdout, mutate):
        binary = isinstance(stdout, bytes)
        report = json.loads(stdout)
        mutate(report)
        value = json.dumps(report)
        return value.encode() if binary else value


class ProducerFixtureControls(RepositoryFixture):
    """Green controls: the real existing producer sees the intended defects."""

    def test_real_producer_empty_findings_is_a_valid_report(self):
        report = self.producer_report()
        self.assertEqual(report["tool"], "ax-lint")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["summary"]["findings"], 0)
        self.assertEqual(report["coverage"], {"skipped": [], "unknown": []})

    def test_real_producer_distinguishes_dispatch_occurrences(self):
        self.put(WORKFLOW_PATH, dispatch("region", "target"))
        report = self.producer_report()
        self.assertEqual([(f["rule_id"], f["path"], f["evidence"])
                          for f in report["findings"]],
                         [("dispatch-inputs/untyped", WORKFLOW_PATH, "region:"),
                          ("dispatch-inputs/untyped", WORKFLOW_PATH, "target:")])

    def test_real_producer_emits_file_subject_and_explicit_coverage(self):
        self.put("scripts/deploy.sh", BAD_SCRIPT)
        self.put(WORKFLOW_PATH, "on:\n  workflow_dispatch:\n    inputs: &inputs\n      target: {description: x}\n")
        report = self.producer_report()
        self.assertEqual([(f["rule_id"], f["path"]) for f in report["findings"]],
                         [(SCRIPT_RULE, "scripts/deploy.sh")])
        self.assertEqual([e["path"] for e in report["coverage"]["unknown"]], [WORKFLOW_PATH])


class IdentityDelta(RepositoryFixture):
    def test_clean_exact_snapshots(self):
        report = self.check(0, head=self.base)
        self.assert_counts(report, 0, 0, 0, 0)

    def test_unchanged_legacy_debt_passes_but_stays_visible(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        self.put("unrelated.txt", "new text\n")
        self.assert_counts(self.check(0), 1, 1, 0, 0)

    def test_removed_debt_passes_and_is_reported(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        self.put("scripts/old.sh", GOOD_SCRIPT)
        report = self.check(0)
        self.assert_counts(report, 1, 0, 0, 1)
        self.assertEqual(report["resolved"], [self.identity("scripts/old.sh")])

    def test_new_identity_fails_when_total_is_unchanged(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        self.put("scripts/old.sh", GOOD_SCRIPT)
        self.put("scripts/new.sh", BAD_SCRIPT)
        report = self.check(1)
        self.assert_counts(report, 1, 1, 1, 1)
        self.assertEqual(report["new"], [self.identity("scripts/new.sh")])

    def test_new_identity_fails_even_when_total_falls(self):
        for name in ("one", "two", "three"):
            self.put(f"scripts/{name}.sh", BAD_SCRIPT)
        self.base = self.commit()
        for name in ("one", "two", "three"):
            (self.root / f"scripts/{name}.sh").unlink()
        self.put("scripts/four.sh", BAD_SCRIPT)
        report = self.check(1)
        self.assert_counts(report, 3, 1, 1, 3)
        self.assertEqual(report["new"], [self.identity("scripts/four.sh")])

    def test_file_rule_does_not_use_flag_text_as_occurrence_identity(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        self.put("scripts/old.sh", "#!/usr/bin/env bash\n\n# different --environment X\n")
        self.assert_counts(self.check(0), 1, 1, 0, 0)

    def test_missing_map_and_oversized_map_are_different_rule_identities(self):
        (self.root / "AGENTS.md").unlink()
        self.base = self.commit()
        self.put("AGENTS.md", "line\n" * 121)
        report = self.check(1)
        self.assert_counts(report, 1, 1, 1, 1)
        self.assertEqual(report["new"], [self.identity("AGENTS.md", rule="agents-map/oversize")])

    def test_pure_git_rename_is_conservatively_new_identity(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        self.git("mv", "scripts/old.sh", "scripts/renamed.sh")
        head = self.commit()
        self.assertIn("R100", self.git("diff", "--name-status", "-M", self.base, head))
        report = self.check(1, head=head)
        self.assert_counts(report, 1, 1, 1, 1)
        self.assertEqual(report["new"], [self.identity("scripts/renamed.sh")])

    def test_paths_with_spaces_unicode_and_case_remain_distinct(self):
        for name in ("scripts/A.sh", "scripts/a.sh", "scripts/a b.sh", "scripts/☃.sh", "scripts/é.sh", "scripts/é.sh"):
            self.put(name, BAD_SCRIPT)
        report = self.check(1)
        self.assert_counts(report, 0, 6, 6, 0)
        self.assertEqual([item["path"] for item in report["new"]],
                         sorted(["scripts/A.sh", "scripts/a.sh", "scripts/a b.sh", "scripts/☃.sh", "scripts/é.sh", "scripts/é.sh"]))

    def test_repeated_runs_are_deterministic(self):
        self.put("scripts/z.sh", BAD_SCRIPT)
        self.put("scripts/a.sh", BAD_SCRIPT)
        head = self.commit()
        first = self.check(1, head=head)
        second = self.check(1, head=head)
        self.assertEqual(first, second)
        self.assertEqual(first["new"], [self.identity("scripts/a.sh"), self.identity("scripts/z.sh")])

class WorkflowDiagnosticRefusal(RepositoryFixture):
    """Selected workflow paths are refused before their syntax or reports matter."""

    def setUp(self):
        super().setUp()
        self.put(POLICY, json.dumps(WORKFLOW_POLICY))
        self.base = self.commit()

    def test_reducing_workflow_debt_is_refused_not_reported_as_resolution(self):
        self.put(WORKFLOW_PATH, dispatch("one", "two", "three"))
        self.base = self.commit()
        self.put(WORKFLOW_PATH, dispatch("four"))
        self.check(2)

    def test_replacing_dispatch_input_is_refused_not_compared(self):
        self.put(WORKFLOW_PATH, dispatch("old_target"))
        self.base = self.commit()
        self.put(WORKFLOW_PATH, dispatch("new_target"))
        self.check(2)

    def test_adding_dispatch_input_is_refused_not_deduplicated(self):
        self.put(WORKFLOW_PATH, dispatch("target"))
        self.base = self.commit()
        self.put(WORKFLOW_PATH, dispatch("target", "region"))
        self.check(2)

    def test_dispatch_rule_change_is_refused_not_compared(self):
        self.put(WORKFLOW_PATH, dispatch("target"))
        self.base = self.commit()
        self.put(WORKFLOW_PATH, dispatch("target", typed=True).replace("type: string", "type: choice"))
        self.check(2)

    def test_dispatch_formatting_is_refused_not_grandfathered(self):
        self.put(WORKFLOW_PATH, dispatch("target", "region"))
        self.base = self.commit()
        self.put(WORKFLOW_PATH, dispatch("region", "target", prefix="# inserted\n\n", comments=True))
        self.check(2)

    def test_dispatch_input_case_is_refused_not_normalized(self):
        self.put(WORKFLOW_PATH, dispatch("Target"))
        self.base = self.commit()
        self.put(WORKFLOW_PATH, dispatch("target"))
        self.check(2)

    def test_duplicate_dispatch_inputs_use_the_same_workflow_refusal(self):
        ordinary = self.put(WORKFLOW_PATH, dispatch("target"))
        report = self.check(2)
        ordinary.write_text(dispatch("target", "target"), encoding="utf-8")
        duplicate = self.check(2)
        self.assertEqual(duplicate["code"], report["code"])

    def test_unknown_workflow_is_refused_even_when_unchanged(self):
        self.put(WORKFLOW_PATH, "on:\n  workflow_dispatch:\n    inputs: &inputs\n      target: {description: x}\n")
        self.base = self.commit()
        self.check(2, head=self.base)

    def test_empty_inline_workflow_is_refused_not_retained_as_a_skip(self):
        self.put(WORKFLOW_PATH, "on:\n  workflow_dispatch:\n    inputs: {}\n")
        self.base = self.commit()
        self.check(2, head=self.base)

    def test_nonempty_inline_workflow_is_refused_not_certified_by_a_skip(self):
        self.put(WORKFLOW_PATH, "on:\n  workflow_dispatch: {inputs: {target: {description: x}}}\n")
        self.check(2)

    def test_reviewed_yaml_variants_are_refused_in_base_head_and_both(self):
        codes = set()
        empty = self.base
        for suffix in (".yml", ".yaml"):
            path = str(Path(WORKFLOW_PATH).with_suffix(suffix))
            for variant, source in WORKFLOW_VARIANTS.items():
                self.put(path, source)
                present = self.commit()
                (self.root / path).unlink()
                removed = self.commit()
                for side, base, head in (("base", present, removed),
                                         ("head", empty, present),
                                         ("both", present, present)):
                    with self.subTest(variant=variant, suffix=suffix, side=side):
                        report = self.check(2, base=base, head=head)
                        codes.add(report["code"])
                empty = removed
        self.assertEqual(len(codes), 1, "One stable path-refusal code, not syntax-specific verdicts")

    def test_exact_or_broad_policy_cannot_silently_filter_selected_workflows(self):
        self.put(WORKFLOW_PATH, "name: build\non: push\njobs: {}\n")
        for include in ([WORKFLOW_PATH], [".github/workflows/**"], [".github/**"], ["**"]):
            with self.subTest(include=include):
                self.put(POLICY, json.dumps({**WORKFLOW_POLICY, "include": include}))
                self.base = self.commit()
                self.check(2, head=self.base)

    def test_head_policy_cannot_remove_trusted_workflow_scope(self):
        self.put(WORKFLOW_PATH, dispatch("target"))
        self.base = self.commit()
        self.put(POLICY, json.dumps(POLICY_VALUE))
        self.check(2)

    def test_selected_workflow_cannot_be_cleared_by_supplied_coverage(self):
        empty_report = self.producer_report()
        self.put(WORKFLOW_PATH, dispatch("target"))
        head = self.commit()
        self.assertTrue(SCRIPT.is_file(), "Missing implementation: " + str(SCRIPT.relative_to(ROOT)))
        spec = importlib.util.spec_from_file_location("diagnostic_delta_workflow_refusal", SCRIPT)
        gate = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: gate}):
            spec.loader.exec_module(gate)
        real_run = subprocess.run
        producer_calls = []

        def run(args, *positional, **kwargs):
            if isinstance(args, (list, tuple)) and any(
                    Path(str(arg)).name == "ax_lint.py" for arg in args):
                producer_calls.append(list(args))
                stdout = json.dumps(empty_report)
                if not kwargs.get("text", False):
                    stdout = stdout.encode()
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
            return real_run(args, *positional, **kwargs)

        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(subprocess, "run", side_effect=run):
            with redirect_stdout(output):
                code = gate.main(self.argv(self.base, head))
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2, report)
        self.assert_error(report)
        self.assertEqual(producer_calls, [], "Reject selected workflow paths before consuming any producer report")


class ExactSnapshotsAndPolicy(RepositoryFixture):
    def test_head_deletion_weakening_or_malformed_policy_cannot_bypass(self):
        for policy in (None, {"version": 1, "producer": "ax-lint", "include": ["docs/**"]}, "bad json"):
            with self.subTest(policy=policy):
                self.put("scripts/new.sh", BAD_SCRIPT)
                if policy is None:
                    (self.root / POLICY).unlink(missing_ok=True)
                else:
                    self.put(POLICY, policy if isinstance(policy, str) else json.dumps(policy))
                self.assert_counts(self.check(1), 0, 1, 1, 0)

    def test_absent_policy_is_not_configured_not_enforced(self):
        (self.root / POLICY).unlink()
        self.base = self.commit()
        self.put("scripts/new.sh", BAD_SCRIPT)
        self.put(POLICY, json.dumps(POLICY_VALUE))
        report = self.check(0)
        self.assertEqual(report["status"], "not_configured")
        self.assertIs(report["enforced"], False)

    def test_target_policy_not_common_ancestor_controls_scope(self):
        self.put("scripts/new.sh", BAD_SCRIPT)
        head = self.commit()
        self.git("checkout", "--detach", self.base)
        self.put("scripts/new.sh", BAD_SCRIPT)
        self.put("target-only.txt", "unique target commit\n")
        target = self.commit()
        self.assertEqual(self.git("merge-base", target, head), self.base)
        report = self.check(0, base=target, head=head)
        self.assert_counts(report, 1, 1, 0, 0)

    def test_target_policy_not_merge_base_policy_selects_paths(self):
        self.put("other/deploy.sh", BAD_SCRIPT)
        head = self.commit()
        self.git("checkout", "--detach", self.base)
        self.put(POLICY, json.dumps({**POLICY_VALUE, "include": ["other/**"]}))
        target = self.commit()
        self.assertEqual(self.git("merge-base", target, head), self.base)
        report = self.check(1, base=target, head=head)
        self.assert_counts(report, 0, 1, 1, 0)
        self.assertEqual(report["new"], [self.identity("other/deploy.sh")])

    def test_head_policy_cannot_expand_scope_to_refuse_unselected_workflows(self):
        self.put(WORKFLOW_PATH, dispatch("target"))
        self.put(POLICY, json.dumps(WORKFLOW_POLICY))
        self.put("scripts/new.sh", BAD_SCRIPT)
        self.assert_counts(self.check(1), 0, 1, 1, 0)

    def test_out_of_scope_debt_is_not_counted(self):
        self.put("other/deploy.sh", BAD_SCRIPT)
        self.put("scripts/deeply/nested/deploy.sh", BAD_SCRIPT)
        report = self.check(1)
        self.assert_counts(report, 0, 1, 1, 0)
        self.assertEqual(report["new"], [self.identity("scripts/deeply/nested/deploy.sh")])

    def test_dirty_worktree_index_and_untracked_policy_are_not_inputs(self):
        self.put("scripts/new.sh", BAD_SCRIPT)
        head = self.commit()
        self.put("scripts/new.sh", GOOD_SCRIPT)
        self.put(POLICY, "malformed dirty policy")
        self.git("add", "-A")
        self.put("scripts/untracked.sh", BAD_SCRIPT)
        before = self.git("status", "--porcelain=v1", "-uall")
        self.assert_counts(self.check(1, head=head), 0, 1, 1, 0)
        self.assertEqual(self.git("status", "--porcelain=v1", "-uall"), before)
        self.assertEqual((self.root / POLICY).read_text(), "malformed dirty policy")

    def test_head_producer_script_cannot_replace_trusted_scanner(self):
        marker = Path(self.tmp.name) / "executed"
        self.put("templates/org-github/ax-lint/ax_lint.py",
                 f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.put("scripts/new.sh", BAD_SCRIPT)
        self.assert_counts(self.check(1), 0, 1, 1, 0)
        self.assertFalse(marker.exists(), "Never execute the PR's producer copy")

    def test_scanned_scripts_and_generators_are_not_executed_or_mutated(self):
        marker = Path(self.tmp.name) / "executed"
        script = f"#!/usr/bin/env bash\ntouch '{marker}'\n# deploy --target X\n"
        self.put("scripts/new.sh", script).chmod(0o755)
        self.put("Makefile", f"all:\n\ttouch '{marker}'\n")
        head = self.commit()
        tree_before = {str(p.relative_to(self.root)): p.read_bytes()
                       for p in self.root.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.root).parts}
        self.check(1, head=head)
        self.assertFalse(marker.exists())
        tree_after = {str(p.relative_to(self.root)): p.read_bytes()
                      for p in self.root.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.root).parts}
        self.assertEqual(tree_before, tree_after)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_invalid_mutable_and_unavailable_commit_refs_are_unmeasurable(self):
        for ref in ("main", "HEAD", "--help", "a" * 39, "A" * 40, "0" * 40):
            for side in ("base", "head"):
                with self.subTest(ref=ref, side=side):
                    self.check(2, **{side: ref})

    def test_blob_sha_is_not_a_source_snapshot(self):
        blob = self.git("rev-parse", self.base + ":AGENTS.md")
        self.check(2, base=blob, head=self.base)
        self.check(2, head=blob)

    def test_unavailable_repository_is_unmeasurable(self):
        self.check(2, repo=Path(self.tmp.name) / "missing", head=self.base)

    def test_invalid_base_policy_fails_closed(self):
        values = ["", "{", "[]", "null", json.dumps({}),
                  json.dumps({**POLICY_VALUE, "version": True}),
                  json.dumps({**POLICY_VALUE, "version": 2}),
                  json.dumps({**POLICY_VALUE, "producer": "another-tool"}),
                  json.dumps({**POLICY_VALUE, "silent_option": True})]
        for value in values:
            with self.subTest(value=value):
                self.put(POLICY, value)
                self.base = self.commit()
                self.check(2, head=self.base)

    def test_invalid_or_unbounded_scope_is_unmeasurable(self):
        for include in ([], "scripts/**", [1], [""], ["../**"], ["/tmp/**"],
                        ["scripts/../**"], ["./scripts/**"], ["scripts\\**"],
                        ["scripts//**"], ["scripts/\x00*"], [f"p{i}/**" for i in range(65)]):
            with self.subTest(include=include):
                self.put(POLICY, json.dumps({**POLICY_VALUE, "include": include}))
                self.base = self.commit()
                self.check(2, head=self.base)

    def test_duplicate_json_policy_keys_are_unmeasurable(self):
        self.put(POLICY, '{"version":1,"producer":"ax-lint","include":["scripts/**"],"include":["docs/**"]}')
        self.base = self.commit()
        self.check(2, head=self.base)

    def test_symlink_policy_and_scoped_symlink_do_not_follow_external_files(self):
        external = Path(self.tmp.name) / "outside"
        external.write_text(json.dumps(POLICY_VALUE))
        (self.root / POLICY).unlink()
        (self.root / POLICY).symlink_to(external)
        self.base = self.commit()
        self.check(2, head=self.base)
        (self.root / POLICY).unlink()
        self.put(POLICY, json.dumps(POLICY_VALUE))
        self.base = self.commit()
        (self.root / "scripts").mkdir()
        (self.root / "scripts/link.sh").symlink_to(external)
        self.check(2)

    def test_non_utf8_scoped_source_is_not_silently_cleared(self):
        self.put("scripts/binary.sh", b"#!/bin/sh\n\xff\xfe\n")
        self.check(2)


class CoverageAndReportIntegrity(RepositoryFixture):
    def test_unknown_coverage_outside_policy_does_not_disable_scoped_ratchet(self):
        self.put(POLICY, json.dumps({**POLICY_VALUE, "include": ["scripts/**"]}))
        self.put(WORKFLOW_PATH, "on:\n  workflow_dispatch:\n    inputs: &inputs\n      target: {description: x}\n")
        self.base = self.commit()
        self.put("scripts/new.sh", BAD_SCRIPT)
        self.assert_counts(self.check(1), 0, 1, 1, 0)

    def test_missing_empty_truncated_non_json_unknown_tool_and_nonzero_reports_fail(self):
        for value, exit_code in (("", 0), (" ", 0), ("{", 0), ("{}", 0), ("null", 0),
                                 ("[]", 0), ('{"tool":"other","findings":[]}', 0), (None, 7)):
            for side in (1, 2):
                with self.subTest(value=value, exit_code=exit_code, side=side):
                    def transform(stdout, call):
                        if call != side:
                            return stdout, 0
                        invalid = stdout if value is None else (value.encode() if isinstance(stdout, bytes) else value)
                        return invalid, exit_code
                    code, report, _ = self.main_with_producer(transform)
                    self.assertEqual(code, 2, report)
                    self.assert_error(report)

    def test_producer_timeouts_and_os_errors_are_unmeasurable(self):
        for failure in (subprocess.TimeoutExpired("ax-lint", 30), OSError("producer unavailable")):
            for side in (1, 2):
                with self.subTest(failure=type(failure).__name__, side=side):
                    code, report, _ = self.main_with_producer(failure=failure, failure_at=side)
                    self.assertEqual(code, 2, report)
                    self.assert_error(report)

    def test_same_pinned_producer_and_bounded_command_are_used_twice(self):
        code, report, calls = self.main_with_producer()
        self.assertEqual(code, 0, report)
        self.assertEqual(len(calls), 2)
        for args, kwargs in calls:
            paths = [Path(str(arg)).resolve() for arg in args if Path(str(arg)).name == "ax_lint.py"]
            self.assertEqual(paths, [PRODUCER.resolve()])
            self.assertEqual(args[args.index("--mode") + 1], "warn")
            self.assertIn("--json", args)
            self.assertIn("--root", args)
            self.assertGreater(kwargs.get("timeout", 0), 0)
            self.assertLessEqual(kwargs["timeout"], 30)
            self.assertFalse(kwargs.get("shell", False))

    def test_report_order_root_lines_and_prose_are_not_identity(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.put("scripts/target.sh", BAD_SCRIPT)
        self.put("scripts/region.sh", BAD_SCRIPT)
        self.base = self.commit()

        def transform(stdout, call):
            def mutate(report):
                report["root"] = f"/incidental/checkout-{call}"
                report["findings"].reverse()
                for finding in report["findings"]:
                    finding["line"] = 1000 + call
                    finding["message"] = f"new wording {call}"
                    finding["remediation"] = f"new remediation {call}"
            return self.rewrite_report(stdout, mutate), 0

        code, report, _ = self.main_with_producer(transform)
        self.assertEqual(code, 0, report)
        self.assert_counts(report, 3, 3, 0, 0)

    def test_malformed_report_schema_and_summary_cannot_claim_clean(self):
        mutations = [lambda r: r.pop("coverage"), lambda r: r.pop("findings"),
                     lambda r: r.pop("summary"), lambda r: r.update(findings={}),
                     lambda r: r.update(coverage={"skipped": [], "unknown": "unreadable"}),
                     lambda r: r.update(coverage={"skipped": []}),
                     lambda r: r["summary"].update(findings=99),
                     lambda r: r["summary"].update(findings=True),
                     lambda r: r.update(mode="error"),
                     lambda r: r.update(tool="another-tool")]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                code, report, _ = self.main_with_producer(
                    lambda stdout, call: (self.rewrite_report(stdout, mutate), 0))
                self.assertEqual(code, 2, report)
                self.assert_error(report)

    def test_unsafe_or_unknown_finding_identities_are_not_normalized_into_clean(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        mutations = [{"rule_id": "new-tool/new-rule"}, {"rule_id": ""}, {"path": "../scripts/old.sh"},
                     {"path": "/scripts/old.sh"}, {"path": "scripts/./old.sh"},
                     {"path": "scripts//old.sh"}, {"path": "scripts\\old.sh"}, {"path": ""},
                     {"path": "scripts/\x00old.sh"}, {"path": 3}]
        for values in mutations:
            with self.subTest(values=values):
                code, report, _ = self.main_with_producer(lambda stdout, call:
                    (self.rewrite_report(stdout, lambda r: r["findings"][0].update(values)), 0))
                self.assertEqual(code, 2, report)
                self.assert_error(report)

    def test_dispatch_finding_cannot_supply_measured_script_coverage(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        for rule in ("dispatch-inputs/untyped", "dispatch-inputs/choice-without-options"):
            with self.subTest(rule=rule):
                def transform(stdout, call):
                    return self.rewrite_report(stdout, lambda r: r["findings"][0].update(
                        rule_id=rule, evidence="target:")), 0
                code, report, _ = self.main_with_producer(transform)
                self.assertEqual(code, 2, report)
                self.assert_error(report)

    def test_duplicate_report_identity_never_silently_collapses(self):
        self.put("scripts/old.sh", BAD_SCRIPT)
        self.base = self.commit()
        for side in (1, 2):
            with self.subTest(side=side):
                def transform(stdout, call):
                    def mutate(r):
                        duplicate = copy.deepcopy(r["findings"][0])
                        duplicate["line"] = 99
                        r["findings"].append(duplicate)
                        r["summary"]["findings"] = 2
                    return (self.rewrite_report(stdout, mutate) if call == side else stdout), 0
                code, report, _ = self.main_with_producer(transform)
                self.assertEqual(code, 2, report)
                self.assert_error(report)

    def test_unreadable_unknown_or_malformed_coverage_entries_cannot_clear(self):
        for entry in ({"path": "scripts/unreadable.sh", "line": None, "category": "unknown", "reason": "permission denied"},
                      {"path": "../escape", "line": 1, "category": "unknown", "reason": "unsupported"},
                      {"path": "scripts/x.sh", "line": 1, "category": "unexpected", "reason": "unsupported"}):
            with self.subTest(entry=entry):
                code, report, _ = self.main_with_producer(lambda stdout, call:
                    (self.rewrite_report(stdout, lambda r: r["coverage"]["unknown"].append(entry)), 0))
                self.assertEqual(code, 2, report)
                self.assert_error(report)

    def test_oversized_report_is_unmeasurable(self):
        code, report, _ = self.main_with_producer(lambda stdout, call:
            (self.rewrite_report(stdout, lambda r: r.update(root="x" * (1024 * 1024 + 1))), 0))
        self.assertEqual(code, 2, report)
        self.assert_error(report)


class RealProducerReadErrors(RepositoryFixture):
    """Fail actual source I/O after projection, never fabricate unknown coverage."""

    def assert_source_read_failure(self, fail_at):
        self.put(POLICY, json.dumps({**POLICY_VALUE, "include": ["scripts/quality-gates/**"]}))
        path = "scripts/quality-gates/new.sh"
        self.put(path, GOOD_SCRIPT)
        self.base = self.commit()
        self.put(path, BAD_SCRIPT)
        head = self.commit()
        self.assert_counts(self.check(1, head=head), 0, 1, 1, 0)
        real_run = subprocess.run
        for side in (1, 2):
            with self.subTest(snapshot="base" if side == 1 else "head", fail_at=fail_at):
                events = Path(self.tmp.name) / f"source-read-{side}-{fail_at}.jsonl"
                attempts = 0

                def producer_run(args, *positional, **kwargs):
                    nonlocal attempts
                    attempts += 1
                    if attempts != side:
                        return real_run(args, *positional, **kwargs)
                    producer_index = next(i for i, arg in enumerate(args)
                                          if Path(str(arg)).name == "ax_lint.py")
                    command = [sys.executable, "-I", "-B", "-c", SOURCE_READ_ERROR,
                               str(PRODUCER), path, str(fail_at), str(events),
                               *args[producer_index + 1:]]
                    return real_run(command, *positional, **kwargs)

                code, report, _ = self.main_with_producer(producer_run=producer_run)
                self.assertTrue(events.is_file(), "The source-read fault must reach the real producer scan")
                reads = [json.loads(line) for line in events.read_text().splitlines()]
                self.assertTrue(reads, "The scan must actually encounter a denied source read")
                self.assertEqual(reads[0], {"path": path, "read": fail_at})
                self.assertEqual(code, 2, report)
                self.assert_error(report)

    def test_source_read_failure_during_candidate_detection_is_unmeasurable(self):
        self.assert_source_read_failure(1)

    def test_source_read_failure_after_candidate_detection_is_unmeasurable(self):
        self.assert_source_read_failure(2)


if __name__ == "__main__":
    unittest.main()
