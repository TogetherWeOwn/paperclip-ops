#!/usr/bin/env python3
"""Tests for docs_scope.py: the docs-only detector fails closed."""
import unittest

from docs_scope import classify, is_prose_path, parse_name_status


class TestIsProsePath(unittest.TestCase):
    def test_root_markdown_is_prose(self):
        self.assertTrue(is_prose_path("README.md"))

    def test_docs_dir_is_prose(self):
        self.assertTrue(is_prose_path("docs/ci-docs-fast-path.md"))
        self.assertTrue(is_prose_path("docs/notes.txt"))

    def test_root_text_inputs_require_full_ci(self):
        for path in ("kofra_preview_parity_allowlist.txt", "public-owned-paths.txt",
                     "quota_brake_exempt.txt", "staged_root_scripts.txt",
                     "ops-tooling-runtime-manifest.txt", "notes.txt", "-x.txt", "NOTES.TXT"):
            with self.subTest(path=path):
                self.assertFalse(is_prose_path(path))
                self.assertFalse(classify(["README.md", path]))

    def test_nested_markdown_outside_docs_is_code(self):
        self.assertFalse(is_prose_path("src/guide.md"))

    def test_workflow_is_code(self):
        self.assertFalse(is_prose_path(".github/workflows/ci.yml"))

    def test_script_is_code(self):
        self.assertFalse(is_prose_path("tool_drift.sh"))

    def test_empty_is_code(self):
        self.assertFalse(is_prose_path(""))

    def test_license_prefix_lookalikes_are_code(self):
        # case_e5882bc5: exact bare names only, no prefix match.
        self.assertFalse(is_prose_path("licensed-code.py"))
        self.assertFalse(is_prose_path("license-check.sh"))
        self.assertTrue(is_prose_path("LICENSE"))
        self.assertTrue(is_prose_path("license-mit"))


class TestClassify(unittest.TestCase):
    def test_all_prose_is_docs_only(self):
        self.assertTrue(classify(["README.md", "docs/runbook.md"]))

    def test_one_code_file_fails_closed(self):
        self.assertFalse(classify(["README.md", "tool_drift.sh"]))

    def test_empty_list_fails_closed(self):
        self.assertFalse(classify([]))

    def test_github_prose_still_runs_full_suite(self):
        self.assertFalse(classify([".github/pull_request_template.md"]))


class TestParseNameStatus(unittest.TestCase):
    def test_rename_contributes_both_names(self):
        paths = parse_name_status(["R100\tsrc/check.sh\tdocs/check.md"])
        self.assertIn("src/check.sh", paths)
        self.assertIn("docs/check.md", paths)
        self.assertFalse(classify(paths))

    def test_name_only_passes_through(self):
        self.assertEqual(parse_name_status(["README.md"]), ["README.md"])


if __name__ == "__main__":
    unittest.main()
