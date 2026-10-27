#!/usr/bin/env python3
# ===========================================================================
# test_ci_required_checks_nested_input.py -- wrong-shaped nested inputs stay
# inside the machine-readable contract.
#
# Top-level shape checks are not enough: a rules list containing null, a
# parameters object that is a string, a scalar check entry, or a manifest
# with a non-string context/branch must still yield exactly one JSON object
# (unmeasurable, exit 2), never a traceback. A workflow entry that cannot be
# opened or decoded stays a measured UNPARSEABLE finding (fail closed).
#
# Tests assert exit status and error/finding CODES, never message text.
# Offline: temp fixtures only, no network, no credential.
# ===========================================================================
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "ci_required_checks_audit.py")

WORKFLOW = """\
on:
  pull_request:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def run_tool(*args):
    return subprocess.run(
        [sys.executable, TOOL, *args], capture_output=True, text=True, timeout=60
    )


def one_json_object(stdout):
    return json.loads(stdout)


class NestedInput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="crcn-")
        self.addCleanup(self.tmp.cleanup)
        self.wf = os.path.join(self.tmp.name, "wf")
        os.makedirs(self.wf)
        with open(os.path.join(self.wf, "ci.yml"), "w", encoding="utf-8") as fh:
            fh.write(WORKFLOW)

    def write_json(self, name, obj):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def assert_unmeasurable(self, proc, error_code):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        doc = one_json_object(proc.stdout)
        self.assertEqual(doc["outcome"], "unmeasurable")
        self.assertEqual(doc["error_code"], error_code)
        self.assertEqual(doc["measurement_state"], "unmeasured")
        self.assertEqual(doc["schema_version"], 1)
        self.assertIs(doc["retryable"], False)
        self.assertIsNone(doc["counts"])
        self.assertIsNone(doc["findings"])
        self.assertTrue(doc["next_action"])
        self.assertEqual(proc.stderr, "")
        return doc

    def test_empty_offline_source_path_is_unreadable_not_usage_or_findings(self):
        for flag, kind, code in (
            ("--manifest", "manifest", "MANIFEST_UNREADABLE"),
            ("--rules-json", "rules-json", "RULES_UNREADABLE"),
        ):
            for workflows in ((), ("--workflows", self.wf)):
                with self.subTest(flag=flag, workflows=workflows):
                    proc = run_tool(flag, "", *workflows, "--json")
                    doc = self.assert_unmeasurable(proc, code)
                    self.assertEqual(doc["source"], {"kind": kind, "path": ""})
                    self.assertIsNone(doc["branch"])

    def test_empty_offline_source_path_text_is_prose_exit_2(self):
        for flag in ("--manifest", "--rules-json"):
            with self.subTest(flag=flag):
                proc = run_tool(flag, "", "--workflows", self.wf)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(proc.stderr.startswith("ci_required_checks_audit: "))
                self.assertNotIn("Traceback", proc.stderr)

    def test_empty_repo_is_prose_exit_2_without_invoking_gh(self):
        for workflows in ((), ("--workflows", self.wf)):
            for output in ((), ("--json",)):
                with self.subTest(workflows=workflows, output=output):
                    proc = run_tool(
                        "--repo", "", *workflows, *output,
                        "--gh-bin", os.path.join(self.tmp.name, "nonexistent-gh"),
                    )
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertTrue(proc.stderr.startswith("ci_required_checks_audit: "))
                    self.assertNotIn("Traceback", proc.stderr)

    def test_invalid_manifest_required_keeps_known_branch_without_override(self):
        manifest = self.write_json(
            "manifest.json", {"branch": "release/x", "required": [{"context": 123}]}
        )
        proc = run_tool("--manifest", manifest, "--workflows", self.wf, "--json")
        doc = self.assert_unmeasurable(proc, "MANIFEST_INVALID")
        self.assertEqual(doc["branch"], "release/x")
        self.assertEqual(doc["source"], {"kind": "manifest", "path": manifest})

    def test_invalid_manifest_branch_does_not_invent_a_branch(self):
        for branch in (None, 123, False, [], {}):
            with self.subTest(branch=branch):
                manifest = self.write_json(
                    "manifest.json", {"branch": branch, "required": [{"context": "build"}]}
                )
                proc = run_tool("--manifest", manifest, "--workflows", self.wf, "--json")
                doc = self.assert_unmeasurable(proc, "MANIFEST_INVALID")
                self.assertIsNone(doc["branch"])

    def test_loaded_rules_unmeasurable_outcomes_use_default_main_branch(self):
        empty_workflows = os.path.join(self.tmp.name, "empty-wf")
        os.makedirs(empty_workflows)
        for rules_doc, workflows, code in (
            ([self._valid_rule()], (), "WORKFLOWS_REQUIRED"),
            ([self._valid_rule()], ("--workflows", empty_workflows), "NO_WORKFLOWS"),
            ([], ("--workflows", self.wf), "NO_REQUIRED_CHECKS"),
        ):
            with self.subTest(code=code):
                rules = self.write_json("rules.json", rules_doc)
                proc = run_tool("--rules-json", rules, *workflows, "--json")
                doc = self.assert_unmeasurable(proc, code)
                self.assertEqual(doc["branch"], "main")
                self.assertEqual(doc["source"], {"kind": "rules-json", "path": rules})

    def test_non_object_manifest_is_invalid_in_json_and_text_modes(self):
        for value in ([], None, "x", 123, False):
            for output in ((), ("--json",)):
                with self.subTest(value=value, output=output):
                    manifest = self.write_json("manifest.json", value)
                    proc = run_tool("--manifest", manifest, "--workflows", self.wf, *output)
                    if output:
                        doc = self.assert_unmeasurable(proc, "MANIFEST_INVALID")
                        self.assertIsNone(doc["branch"])
                    else:
                        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                        self.assertEqual(proc.stdout, "")
                        self.assertTrue(proc.stderr.startswith("ci_required_checks_audit: "))
                        self.assertNotIn("Traceback", proc.stderr)

    def test_empty_context_is_invalid_for_both_offline_sources(self):
        rule = {"type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": ""}]}}
        for flag, name, value, code in (
            ("--manifest", "manifest.json", {"required": [{"context": ""}]}, "MANIFEST_INVALID"),
            ("--rules-json", "rules.json", [rule], "RULES_INVALID"),
        ):
            with self.subTest(flag=flag):
                path = self.write_json(name, value)
                proc = run_tool(flag, path, "--workflows", self.wf, "--json")
                self.assert_unmeasurable(proc, code)

    def test_rules_null_entry_is_RULES_INVALID(self):
        rules = self.write_json("rules.json", [None])
        proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
        self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_rules_string_parameters_is_RULES_INVALID(self):
        rules = self.write_json(
            "rules.json", [{"type": "required_status_checks", "parameters": "bad"}]
        )
        proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
        self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_rules_scalar_check_entry_is_RULES_INVALID(self):
        rules = self.write_json(
            "rules.json",
            [{"type": "required_status_checks",
              "parameters": {"required_status_checks": ["build"]}}],
        )
        proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
        self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_rules_int_context_entry_is_RULES_INVALID(self):
        rules = self.write_json(
            "rules.json",
            [{"type": "required_status_checks",
              "parameters": {"required_status_checks": [{"context": 123}]}}],
        )
        proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
        self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_rules_non_list_checks_is_RULES_INVALID(self):
        rules = self.write_json(
            "rules.json",
            [{"type": "required_status_checks",
              "parameters": {"required_status_checks": "build"}}],
        )
        proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
        self.assert_unmeasurable(proc, "RULES_INVALID")

    def _valid_rule(self):
        return {"type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "build", "integration_id": 15368}]}}

    def test_rules_rule_without_string_type_cannot_hide_behind_valid_rule(self):
        for bad in ({}, {"type": None}, {"type": 7}):
            with self.subTest(bad=bad):
                rules = self.write_json("rules.json", [bad, self._valid_rule()])
                proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
                self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_rules_falsy_parameters_or_checks_cannot_hide_behind_valid_rule(self):
        for params in (None, False, 0, "", []):
            with self.subTest(parameters=params):
                rules = self.write_json(
                    "rules.json",
                    [{"type": "required_status_checks", "parameters": params}, self._valid_rule()],
                )
                proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
                self.assert_unmeasurable(proc, "RULES_INVALID")
        for checks in (None, False, 0, "", {}):
            with self.subTest(checks=checks):
                rules = self.write_json(
                    "rules.json",
                    [{"type": "required_status_checks", "parameters": {"required_status_checks": checks}},
                     self._valid_rule()],
                )
                proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
                self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_rules_missing_checks_list_is_RULES_INVALID(self):
        for params in ({}, {"strict_required_status_checks_policy": True}):
            with self.subTest(params=params):
                rules = self.write_json(
                    "rules.json",
                    [{"type": "required_status_checks", "parameters": params},
                     self._valid_rule()],
                )
                proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
                self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_rules_non_integer_integration_id_is_RULES_INVALID(self):
        for bad in ([], {}, "15368", True, 1.5):
            with self.subTest(integration_id=bad):
                rules = self.write_json(
                    "rules.json",
                    [{"type": "required_status_checks",
                      "parameters": {"required_status_checks": [{"context": "build", "integration_id": bad}]}}],
                )
                proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
                self.assert_unmeasurable(proc, "RULES_INVALID")

    def test_manifest_non_integer_integration_id_is_MANIFEST_INVALID(self):
        for bad in ([], {}, "15368", True, 1.5):
            with self.subTest(integration_id=bad):
                manifest = self.write_json(
                    "manifest.json",
                    {"branch": "main", "required": [{"context": "build", "integration_id": bad}]},
                )
                proc = run_tool("--manifest", manifest, "--workflows", self.wf, "--json")
                self.assert_unmeasurable(proc, "MANIFEST_INVALID")

    def test_rules_valid_shape_still_measured(self):
        rules = self.write_json(
            "rules.json",
            [{"type": "required_status_checks",
              "parameters": {"required_status_checks": [{"context": "build"}]}}],
        )
        proc = run_tool("--rules-json", rules, "--workflows", self.wf, "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        doc = one_json_object(proc.stdout)
        self.assertEqual(doc["counts"]["FAIL"], 0)

    def test_rules_nested_shape_text_mode_is_prose_exit_2(self):
        rules = self.write_json("rules.json", [None])
        proc = run_tool("--rules-json", rules, "--workflows", self.wf)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_manifest_int_context_is_MANIFEST_INVALID(self):
        manifest = self.write_json(
            "manifest.json", {"branch": "main", "required": [{"context": 123}]}
        )
        proc = run_tool("--manifest", manifest, "--workflows", self.wf, "--json")
        self.assert_unmeasurable(proc, "MANIFEST_INVALID")

    def test_manifest_int_branch_is_MANIFEST_INVALID(self):
        manifest = self.write_json(
            "manifest.json", {"branch": 123, "required": [{"context": "build"}]}
        )
        proc = run_tool("--manifest", manifest, "--workflows", self.wf, "--json")
        self.assert_unmeasurable(proc, "MANIFEST_INVALID")

    def test_unopenable_workflow_entry_is_measured_UNPARSEABLE(self):
        os.makedirs(os.path.join(self.wf, "sub.yml"))
        manifest = self.write_json(
            "manifest.json", {"branch": "main", "required": [{"context": "build"}]}
        )
        proc = run_tool("--manifest", manifest, "--workflows", self.wf, "--json")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        doc = one_json_object(proc.stdout)
        self.assertIn(
            ("FAIL", "UNPARSEABLE"),
            {(f["severity"], f["code"]) for f in doc["findings"]},
        )

    def test_undecodable_workflow_file_is_measured_UNPARSEABLE(self):
        with open(os.path.join(self.wf, "bad.yml"), "wb") as fh:
            fh.write(b"on: \xff\xfe not utf-8\n")
        manifest = self.write_json(
            "manifest.json", {"branch": "main", "required": [{"context": "build"}]}
        )
        proc = run_tool("--manifest", manifest, "--workflows", self.wf, "--json")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        doc = one_json_object(proc.stdout)
        self.assertIn(
            ("FAIL", "UNPARSEABLE"),
            {(f["severity"], f["code"]) for f in doc["findings"]},
        )


if __name__ == "__main__":
    unittest.main()
