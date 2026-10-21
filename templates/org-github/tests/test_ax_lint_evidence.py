#!/usr/bin/env python3
"""test_ax_lint_evidence.py — immutable builder acceptance contract (ax-lint-evidence).

Hermetic: stdlib + PyYAML (via existing suite), no network, no Docker,
no scanned-script execution. Synthetic fixtures only. Run from the repo root:

  python3 -B -m unittest discover -s templates/org-github/tests -p 'test_ax_*py'

That command runs this contract plus the existing AX template suite
(`test_ax_org_template.py`); the builder must keep both green on the same head.

Immutable: the builder starts from this pushed test branch/commit and never
edits this file. Do not weaken asserts to make the current implementation pass.

Red baseline (2026-10-21, base e5cfced0): nested harness, conservative
--json/--dry-run matching, located evidence (dispatch/script lines), stable
rule IDs + remediation, explicit skipped/unknown coverage, and --help/README
evidence assertions fail; preservation guards (advisory default, error opt-in,
JSON compat keys, .ax-source exclusion, AGENTS checks, idempotent read-only
scans) pass. See TOG-35167 for the exact SHA and counts.
"""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
LINT_SRC = os.path.join(BASE, "ax-lint", "ax_lint.py")

sys.path.insert(0, os.path.join(BASE, "ax-lint"))

import ax_lint  # noqa: E402


def read_staged(relpath):
    with open(os.path.join(BASE, relpath), encoding="utf-8") as fh:
        return fh.read()


class TempRootMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        # Every tree needs a minimal map so agents-map findings do not
        # pollute script/dispatch assertions unless the test says otherwise.
        self.write("AGENTS.md", "# map\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relpath, text, executable=False):
        path = os.path.join(self.root, relpath)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if executable:
            mode = os.stat(path).st_mode
            os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def read_tree_file(self, relpath):
        with open(os.path.join(self.root, relpath), encoding="utf-8") as fh:
            return fh.read()

    def run_json_report(self, extra_args=None):
        args = ["--root", self.root, "--json"] + list(extra_args or [])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ax_lint.main(args)
        self.assertEqual(code, 0, f"JSON warn run must exit 0, got {code}")
        try:
            report = json.loads(buf.getvalue())
        except json.JSONDecodeError as exc:
            self.fail(f"JSON report did not parse: {exc}\n{buf.getvalue()!r}")
        return report

    def coverage_entries(self, report):
        """Conservative accessor: builder may name this coverage/skipped/unknown.

        Returns (key, entries) for the first present coverage-style key, or
        (None, []) when the implementation reports no coverage at all (the
        current silent-clear behavior this contract forbids for skipped shapes).
        """
        for key in ("coverage", "skipped", "unknown", "heuristics"):
            if key in report:
                entries = report[key]
                if isinstance(entries, dict):
                    # Allow {"skipped": [...], "unknown": [...]} nesting.
                    flat = []
                    for value in entries.values():
                        if isinstance(value, list):
                            flat.extend(value)
                    return key, flat
                if isinstance(entries, list):
                    return key, entries
                return key, [entries]
        summary = report.get("summary", {})
        if isinstance(summary, dict):
            for key in ("skipped", "coverage", "unknown"):
                if key in summary:
                    value = summary[key]
                    return key, value if isinstance(value, list) else [value]
        return None, []

    def rule_id_of(self, finding):
        for key in ("rule_id", "rule", "ruleId"):
            if finding.get(key):
                return finding[key]
        return None

    def remediation_of(self, finding):
        for key in ("remediation", "next_action", "nextAction", "action", "fix"):
            if finding.get(key):
                return finding[key]
        return None


class NestedHarnessRecognitionTest(TempRootMixin, unittest.TestCase):
    """Nested test/tests harnesses are skipped; nearby real tools still warn."""

    def test_nested_tests_dir_excluded(self):
        self.write("tools/tests/check.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(findings, [],
                         "nested tests/ harness must be skipped like root tests/")

    def test_nested_test_singular_excluded(self):
        self.write("pkg/test/helper.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(findings, [],
                         "nested test/ harness must be skipped")

    def test_deeply_nested_harness_excluded(self):
        self.write("a/b/tests/c.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(findings, [],
                         "deeply nested tests/ harness must be skipped")

    def test_root_harness_still_excluded(self):
        self.write("tests/check.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        self.write("test/helper.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_adjacent_tool_warns_while_nested_harness_skipped(self):
        self.write("tools/tests/harness.sh",
                   "#!/usr/bin/env bash\n# --target X\n", executable=True)
        self.write("tools/deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        paths = sorted(f["path"] for f in findings)
        self.assertEqual(paths, ["tools/deploy.sh"],
                         "nested harness skipped, adjacent real tool warns")

    def test_lookalike_dirs_not_excluded(self):
        # Only exact `test`/`tests` path segments are harnesses; substrings
        # like mytests/ or contest.sh must still warn.
        self.write("mytests/deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        self.write("contest.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        paths = sorted(f["path"] for f in findings)
        self.assertEqual(paths, ["contest.sh", "mytests/deploy.sh"])


class ConservativeCliSurfaceTest(TempRootMixin, unittest.TestCase):
    """Only advertised top-level --json/--dry-run counts; incidental mentions warn.

    Compatible with the existing usage-comment passes in
    test_ax_org_template.py (``# usage: --json`` still passes). These negatives
    avoid ``usage:`` so the builder can distinguish advertised surface from
    TODO/prose/child-command/unrelated strings.
    """

    def test_todo_comment_json_does_not_certify(self):
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n"
                   "# TODO: maybe add --json later\n"
                   'if [[ "$1" == "--target" ]]; then echo hi; fi\n',
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(len(findings), 1,
                         "TODO/prose --json mention must not certify support")

    def test_todo_comment_dry_run_does_not_certify(self):
        self.write("promote.sh",
                   "#!/usr/bin/env bash\n"
                   "# NOTE: --dry-run would be nice someday\n"
                   'if [[ "$1" == "--target" ]]; then echo hi; fi\n',
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(len(findings), 1,
                         "TODO/prose --dry-run mention must not certify support")

    def test_child_command_json_does_not_certify(self):
        self.write("ship.sh",
                   "#!/usr/bin/env bash\n"
                   "# top-level takes --target\n"
                   'if [[ "$1" == "--target" ]]; then\n'
                   "  gh pr list --json number,title\n"
                   "fi\n",
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(len(findings), 1,
                         "child-command --json must not certify top-level support")

    def test_unrelated_string_json_does_not_certify(self):
        self.write("report.sh",
                   "#!/usr/bin/env bash\n"
                   'echo "--json is a format"\n'
                   'if [[ "$1" == "--target" ]]; then echo hi; fi\n',
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(len(findings), 1,
                         "unrelated --json string must not certify support")

    def test_usage_comment_still_passes(self):
        # Guard: preserves the existing contract (usage advertises support).
        self.write("status.sh",
                   "#!/usr/bin/env bash\n# usage: --json [--watch]\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_argparse_top_level_json_passes(self):
        self.write("tool.py",
                   "#!/usr/bin/env python3\n"
                   "import argparse\n"
                   "p = argparse.ArgumentParser(prog='tool')\n"
                   "p.add_argument('--target')\n"
                   "p.add_argument('--json', action='store_true')\n"
                   "p.parse_args()\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_case_dispatch_top_level_dry_run_passes(self):
        self.write("promote.sh",
                   "#!/usr/bin/env bash\n"
                   'case "$1" in\n'
                   "  --target) echo t ;;\n"
                   "  --dry-run) echo dry ;;\n"
                   "esac\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])

    def test_scanned_scripts_never_executed(self):
        marker = os.path.join(self.root, "PWNED_BY_SCAN")
        self.write("evil.sh",
                   "#!/usr/bin/env bash\n"
                   f"touch {marker}\n"
                   "# --target X\n",
                   executable=True)
        ax_lint.check_script_surface(self.root)
        with redirect_stdout(io.StringIO()):
            ax_lint.main(["--root", self.root, "--json"])
        self.assertFalse(os.path.exists(marker),
                         "static scan must never execute scanned scripts")


class LocatedEvidenceTest(TempRootMixin, unittest.TestCase):
    """Findings carry stable rule IDs, located evidence and remediation."""

    def write_dispatch(self, body):
        return self.write(".github/workflows/ship.yml", body)

    def test_dispatch_untyped_reports_entry_line(self):
        self.write_dispatch(
            "name: ship\non:\n  workflow_dispatch:\n"
            "    inputs:\n      target:\n"
            "        description: where to ship\n"
            "        required: true\n")
        findings = ax_lint.check_dispatch_inputs(self.root)
        self.assertEqual(len(findings), 1)
        line = findings[0].get("line")
        self.assertIsNotNone(line, "dispatch finding must preserve entry line")
        text = self.read_tree_file(".github/workflows/ship.yml").splitlines()
        self.assertGreaterEqual(line, 1)
        self.assertLessEqual(line, len(text))
        self.assertIn("target:", text[line - 1],
                      f"line {line} must point at the input entry")

    def test_dispatch_choice_reports_entry_line(self):
        self.write_dispatch(
            "name: ship\non:\n  workflow_dispatch:\n"
            "    inputs:\n      target:\n"
            "        description: where\n"
            "        type: choice\n")
        findings = ax_lint.check_dispatch_inputs(self.root)
        self.assertEqual(len(findings), 1)
        line = findings[0].get("line")
        self.assertIsNotNone(line, "choice finding must preserve entry line")
        text = self.read_tree_file(".github/workflows/ship.yml").splitlines()
        self.assertIn("target:", text[line - 1])

    def test_script_surface_reports_line_and_evidence(self):
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy tool\n# deploy --target X\n",
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        line = finding.get("line")
        self.assertIsNotNone(line, "script-surface must report a source line")
        text = self.read_tree_file("deploy.sh").splitlines()
        self.assertIn("--", text[line - 1],
                      "reported line must contain flag evidence")
        evidence = (finding.get("evidence") or finding.get("snippet")
                    or finding.get("message", ""))
        self.assertIn("--", str(evidence),
                      "finding must carry flag evidence, not just a path")

    def test_json_findings_carry_stable_rule_ids(self):
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        self.write_dispatch(
            "name: ship\non:\n  workflow_dispatch:\n"
            "    inputs:\n      target:\n"
            "        description: where\n")
        report = self.run_json_report()
        self.assertGreaterEqual(len(report["findings"]), 2)
        for finding in report["findings"]:
            # Compatibility: the historic `check` key stays.
            self.assertIn("check", finding)
            self.assertIn("path", finding)
            rule = self.rule_id_of(finding)
            self.assertIsNotNone(
                rule, f"finding needs a stable rule ID: {finding!r}")
            self.assertIsInstance(rule, str)
            self.assertTrue(rule.strip(), "rule ID must be non-empty")

    def test_rule_ids_stable_across_runs(self):
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        first = self.run_json_report()
        second = self.run_json_report()
        first_rules = sorted(str(self.rule_id_of(f)) for f in first["findings"])
        second_rules = sorted(str(self.rule_id_of(f)) for f in second["findings"])
        self.assertTrue(all(r and r != "None" for r in first_rules),
                        "rule IDs must be present before stability matters")
        self.assertEqual(first_rules, second_rules,
                         "rule IDs must be stable across repeat runs")

    def test_json_findings_carry_remediation(self):
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        report = self.run_json_report()
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        remediation = self.remediation_of(finding)
        self.assertIsNotNone(
            remediation,
            f"finding needs explicit remediation/next_action: {finding!r}")
        text = str(remediation)
        self.assertTrue(text.strip(), "remediation must be non-empty")
        self.assertTrue(
            any(token in text for token in
                ("--json", "--dry-run", "type:", "options:", "AGENTS.md",
                 "Add", "Declare", "See", "Run")),
            f"remediation must name a next action: {text!r}")


class CoverageHonestyTest(TempRootMixin, unittest.TestCase):
    """Skipped/unsupported shapes are reported, never silently cleared.

    Direct `check_*` calls may still return [] for skipped shapes (that keeps
    the existing `test_inline_dispatch_skipped` green); the JSON report must
    carry the explicit coverage so honest automation can see it.
    """

    def test_inline_dispatch_coverage_explicit_in_json(self):
        self.write(".github/workflows/ci.yml",
                   "name: ci\non:\n  workflow_dispatch: {}\n")
        # Existing contract still holds at the function layer.
        self.assertEqual(ax_lint.check_dispatch_inputs(self.root), [])
        report = self.run_json_report()
        key, entries = self.coverage_entries(report)
        self.assertIsNotNone(
            key, "JSON report must carry explicit skipped/unknown coverage")
        blob = json.dumps(entries)
        self.assertIn("ci.yml", blob,
                      "coverage must name the skipped workflow file")

    def test_unsupported_workflow_shape_coverage_explicit(self):
        # Anchors/aliases are outside the indentation scan; skipping is fine,
        # silent clearing is not.
        self.write(".github/workflows/anchored.yml",
                   "name: a\non:\n  workflow_dispatch:\n"
                   "    inputs: &in\n"
                   "      target: {description: x}\n"
                   "jobs:\n  j:\n    runs-on: ubuntu-latest\n"
                   "    steps: []\n")
        report = self.run_json_report()
        key, entries = self.coverage_entries(report)
        self.assertIsNotNone(
            key, "unsupported workflow shapes need explicit coverage")
        blob = json.dumps(entries)
        self.assertTrue(
            "anchored.yml" in blob or "workflow_dispatch" in blob,
            "coverage must reference the unsupported workflow shape")

    def test_unsupported_script_shape_not_silently_cleared(self):
        # Dynamic eval construction: current text search sees --json and
        # certifies; conservative matching must warn or mark unknown.
        self.write("dynamic.sh",
                   "#!/usr/bin/env bash\n"
                   "# top-level takes --target\n"
                   'eval "tool --json $FLAGS"\n',
                   executable=True)
        report = self.run_json_report()
        findings = report.get("findings", [])
        _, entries = self.coverage_entries(report)
        finding_paths = json.dumps(findings)
        coverage_blob = json.dumps(entries)
        self.assertTrue(
            "dynamic.sh" in finding_paths or "dynamic.sh" in coverage_blob,
            "unsupported shell shape must warn or appear in unknown coverage, "
            "never silently pass")

    def test_clean_tree_coverage_key_present(self):
        self.write("ok.sh",
                   "#!/usr/bin/env bash\n# usage: --json\n", executable=True)
        report = self.run_json_report()
        key, _ = self.coverage_entries(report)
        self.assertIsNotNone(
            key, "JSON report must always carry a coverage/skipped key, "
                 "even when empty")


class PreservationTest(TempRootMixin, unittest.TestCase):
    """Guards: advisory default, compat, exclusions, pins and read-only scans."""

    def test_warn_default_exits_zero_with_findings(self):
        self.write("run.sh", "#!/usr/bin/env bash\n# --force\n",
                   executable=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ax_lint.main(["--root", self.root, "--mode", "warn"])
        self.assertEqual(code, 0)
        self.assertIn("finding(s)", buf.getvalue())

    def test_error_opt_in_exits_one_then_zero(self):
        self.write("run.sh", "#!/usr/bin/env bash\n# --force\n",
                   executable=True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                ax_lint.main(["--root", self.root, "--mode", "error"]), 1)
        os.unlink(os.path.join(self.root, "run.sh"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                ax_lint.main(["--root", self.root, "--mode", "error"]), 0)

    def test_json_compatibility_keys(self):
        report = self.run_json_report()
        self.assertEqual(report["tool"], "ax-lint")
        self.assertIn("findings", report)
        self.assertIn("summary", report)
        self.assertIn("mode", report)
        self.assertIn("root", report)
        self.assertEqual(report["summary"]["findings"], len(report["findings"]))

    def test_ax_source_still_excluded(self):
        self.write(".ax-source/review-rules/tests/selftest.py",
                   "#!/usr/bin/env python3\n# --target X\n",
                   executable=True)
        self.assertEqual(ax_lint.check_script_surface(self.root), [])
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        findings = ax_lint.check_script_surface(self.root)
        self.assertEqual([f["path"] for f in findings], ["deploy.sh"])

    def test_agents_map_checks_preserved(self):
        os.unlink(os.path.join(self.root, "AGENTS.md"))
        missing = ax_lint.check_agents_map(self.root)
        self.assertEqual(len(missing), 1)
        self.assertIn("no AGENTS.md", missing[0]["message"])
        self.write("AGENTS.md", "\n".join(f"line {n}" for n in range(150)))
        oversize = ax_lint.check_agents_map(self.root)
        self.assertEqual(len(oversize), 1)
        self.assertIn("150 lines", oversize[0]["message"])
        self.write("AGENTS.md", "# map\n")
        self.assertEqual(ax_lint.check_agents_map(self.root), [])

    def test_workflow_pin_preserved(self):
        text = read_staged("ax-lint/ax-lint.yml")
        pin = ("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
               " # v4.2.2")
        self.assertEqual(text.count(pin), 2)
        self.assertNotIn("secrets.", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("job.workflow_ref", text)
        self.assertNotIn("github.workflow_ref", text)

    def test_scan_idempotent_and_read_only(self):
        self.write("deploy.sh",
                   "#!/usr/bin/env bash\n# deploy --target X\n",
                   executable=True)
        before = {}
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    before[full] = fh.read()
        first = ax_lint.lint(self.root)
        second = ax_lint.lint(self.root)
        self.assertEqual(first, second, "static scans must be idempotent")
        after = {}
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    after[full] = fh.read()
        self.assertEqual(before, after, "static scans must be read-only")

    def test_bounded_and_deterministic(self):
        for idx in range(50):
            self.write(f"tools/cmd{idx:02d}.sh",
                       "#!/usr/bin/env bash\n# usage: --json\n",
                       executable=True)
        start = time.monotonic()
        first = self.run_json_report()
        second = self.run_json_report()
        elapsed = time.monotonic() - start
        self.assertEqual(first, second)
        self.assertLess(elapsed, 5.0, "bounded synthetic tree must scan fast")

    def test_error_mode_does_not_mutate_or_activate(self):
        marker = os.path.join(self.root, "MUTATED_BY_ERROR_MODE")
        self.write("run.sh", "#!/usr/bin/env bash\n# --force\n",
                   executable=True)
        self.write(".github/workflows/ship.yml",
                   "name: ship\non:\n  workflow_dispatch:\n"
                   "    inputs:\n      target:\n"
                   "        description: x\n")
        before = set()
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                before.add(os.path.relpath(
                    os.path.join(dirpath, name), self.root))
        with redirect_stdout(io.StringIO()):
            code = ax_lint.main(["--root", self.root, "--mode", "error"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(marker))
        after = set()
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                after.add(os.path.relpath(
                    os.path.join(dirpath, name), self.root))
        self.assertEqual(before, after,
                         "--mode error must not mutate the tree or workflows")


class HelpAndDocsTest(unittest.TestCase):
    """Compact --help/rule examples and an offline verification command."""

    def test_help_lists_rules_with_examples(self):
        proc = subprocess.run(
            [sys.executable, "-I", LINT_SRC, "--help"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        help_text = proc.stdout
        for rule in ("agents-map", "script-surface", "dispatch-inputs"):
            self.assertIn(rule, help_text,
                          f"--help must name rule {rule}")
        self.assertIn("--json", help_text)
        self.assertIn("--mode", help_text)
        self.assertIn("example", help_text.lower(),
                      "--help must carry compact examples")
        self.assertLessEqual(len(help_text.splitlines()), 100,
                             "--help must stay compact")

    def test_readme_documents_offline_verification(self):
        readme = read_staged("README.md")
        self.assertTrue(
            "test_ax_lint_evidence" in readme or "test_ax_*" in readme,
            "canonical docs must name the offline evidence verification")
        self.assertIn("discover -s templates/org-github/tests", readme)


if __name__ == "__main__":
    unittest.main()
