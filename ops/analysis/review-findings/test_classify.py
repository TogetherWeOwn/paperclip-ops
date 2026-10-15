"""Hermetic tests for the findings classifier.

No network. Run: (cd ops/analysis/review-findings && python3 -m unittest test_classify)
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from build_units import missing_fetch, unit_id
from classify import classify, rule_label, summarize
from eval_ci_wiring import covered_by_glob, discover_globs, unwired_tests
from score_docs import load_results, sent_ids


def row(headline, path="src/app.py", text="", severity="warning", ident=1):
    return {"id": ident, "repo": "o/r", "private": False, "pr": 1,
            "commit": "abc", "path": path, "line": 3, "severity": severity,
            "category": "correctness", "headline": headline, "text": text}


class RuleLabelTest(unittest.TestCase):
    def test_ci_wiring_beats_test_gap(self):
        label = rule_label(row("CI never runs the new suite, so its tests are untested."))
        self.assertEqual(label, ("K", "ci_wiring"))

    def test_test_gap(self):
        self.assertEqual(rule_label(row("Test 6b asserts nothing.")), ("K", "test_gap"))
        self.assertEqual(
            rule_label(row("The race fix is not pinned by any test: delete it and the suite stays green.")),
            ("K", "test_gap"))

    def test_pr_claim_needs_pr_text_in_headline_or_lead(self):
        self.assertEqual(
            rule_label(row("The PR description and the commit message describe a different change.")),
            ("J", "pr_claim"))

    def test_doc_path_is_b_and_code_path_is_not(self):
        self.assertEqual(rule_label(row("Wrong key.", path="docs/runbook.md")), ("B", "doc_path"))
        # install-lib.sh is code, not an INSTALL doc (regression: re.I matched it)
        self.assertEqual(rule_label(row("Ignores accounts.", path="host-ops/install-lib.sh")),
                         ("O", "code_defect"))

    def test_code_defect_default(self):
        self.assertEqual(rule_label(row("Crashes on null input.")), ("O", "code_defect"))


class ClassifyTest(unittest.TestCase):
    def test_override_wins_and_records_source(self):
        rows = [row("Wrong key.", path="docs/runbook.md", ident=7)]
        out = classify(rows, {"7": {"bucket": "B", "subtype": "doc_instruction_defect"}})[0]
        self.assertEqual((out["bucket"], out["subtype"], out["label_source"]),
                         ("B", "doc_instruction_defect", "override"))
        self.assertEqual(out["check"], "none")

    def test_gold_follows_severity_and_o_has_none(self):
        out = classify([row("Test asserts nothing.", severity="error")], {})[0]
        self.assertEqual(out["gold"], "block")
        warn = classify([row("Test asserts nothing.")], {})[0]
        self.assertEqual(warn["gold"], "hold")
        other = classify([row("Crashes on null input.", severity="error")], {})[0]
        self.assertEqual(other["gold"], "none")

    def test_carry_over_is_flagged(self):
        out = classify([row("Still open. The comment contradicts the code.")], {})[0]
        self.assertTrue(out["carry_over"])

    def test_ci_wiring_is_a_tier0_candidate_not_ac_evidence(self):
        out = classify([row("CI never runs the new suite.")], {})[0]
        self.assertEqual(out["check"], "ci_wiring")

    def test_summary_counts(self):
        rows = classify([row("Test asserts nothing.", ident=1),
                         row("Crashes on null.", ident=2)], {})
        self.assertIn("2 findings", summarize(rows))


def _added(path, body="+x"):
    return {path: {"status": "added", "added_lines": {1}, "patch": f"diff --git a/{path} b/{path}\n{body}"}}


class CiWiringRuleTest(unittest.TestCase):
    WORKFLOW = ".github/workflows/ci.yml"

    def test_flags_a_new_suite_no_workflow_line_names(self):
        self.assertEqual(unwired_tests(_added("test_new_thing.sh")), ["test_new_thing.sh"])

    def test_workflow_line_naming_the_suite_wires_it(self):
        files = _added("test_new_thing.sh")
        files.update(_added(self.WORKFLOW, "+        run: ./test_new_thing.sh"))
        self.assertEqual(unwired_tests(files), [])

    def test_module_stem_in_a_unittest_line_counts(self):
        files = _added("ops/lib/test_atomic_state.py")
        files.update(_added(self.WORKFLOW, "+          (cd ops/lib && python3 -m unittest -v test_atomic_state)"))
        self.assertEqual(unwired_tests(files), [])

    def test_discover_glob_and_package_tests_are_wired_by_existing_steps(self):
        globs = discover_globs("run: python3 -m unittest discover -s github-runner -p 'test_isolated_*.py'")
        self.assertEqual(globs, [("github-runner", "test_isolated_*.py")])
        self.assertTrue(covered_by_glob("github-runner/test_isolated_x.py", globs))
        self.assertFalse(covered_by_glob("github-runner/test_other.py", globs))
        self.assertTrue(covered_by_glob("pkg/test/a.test.mjs", []))
        self.assertEqual(unwired_tests(_added("github-runner/test_isolated_x.py"), globs), [])

    def test_modified_test_files_are_not_new_suites(self):
        files = {"test_old.sh": {"status": "modified", "added_lines": {1}, "patch": "diff --git a/test_old.sh b/test_old.sh\n+x"}}
        self.assertEqual(unwired_tests(files), [])


class UnitIdTest(unittest.TestCase):
    def test_stable_and_distinct_per_section(self):
        a = unit_id("o/r", 7, "abcdef123456", "docs/a.md", 3)
        self.assertEqual(a, unit_id("o/r", 7, "abcdef123456", "docs/a.md", 3))
        self.assertNotEqual(a, unit_id("o/r", 7, "abcdef123456", "docs/a.md", 9))
        self.assertTrue(a.startswith("r-7-abcdef12-"))


def private_labels_absent_from_public(root, path):
    root, path = Path(root), Path(path)
    marker = root / "README.md"
    return (not path.exists() and not path.is_symlink()
            and (root / ".github/scripts/public_safety.py").is_file()
            and marker.is_file()
            and "## Fresh-history publication candidate" in marker.read_text().splitlines())


class CommittedLabelsTest(unittest.TestCase):
    """Private historical labels remain checked when that artifact is available."""

    def test_labels_file_shape(self):
        path = os.path.join(os.path.dirname(__file__), "labels-20261009.json")
        if private_labels_absent_from_public(Path(__file__).resolve().parents[3], path):
            self.skipTest("historical private reviewer labels are excluded from the fresh public snapshot; synthetic classifier tests remain enabled")
        with open(path, encoding="utf-8") as handle:
            labels = json.load(handle)
        self.assertGreater(len(labels), 50)
        ids = [item["id"] for item in labels]
        self.assertEqual(len(ids), len(set(ids)))
        for item in labels:
            self.assertIn(item["bucket"], ("B", "J", "K"))
            self.assertIn(item["gold"], ("block", "hold"))
            self.assertIn(item["check"],
                          ("docs_vs_diff", "claims_vs_diff", "ac_evidence",
                           "ci_wiring", "none"))

    def test_skip_requires_both_markers_and_absent_labels(self):
        for scanner in (False, True):
            for marker in (False, True):
                for labels in (False, True):
                    with self.subTest(scanner=scanner, marker=marker, labels=labels), tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        path = root / "labels.json"
                        if scanner:
                            (root / ".github/scripts").mkdir(parents=True)
                            (root / ".github/scripts/public_safety.py").touch()
                        if marker:
                            (root / "README.md").write_text("## Fresh-history publication candidate\n")
                        if labels:
                            path.write_text("[]")  # malformed/empty datasets must still reach assertions
                        self.assertEqual(private_labels_absent_from_public(root, path), scanner and marker and not labels)


class FetchCompleteGateTest(unittest.TestCase):
    """missing_fetch must name every headline PR the cache cannot score."""

    def _cache(self, root, pr, final_head, with_diff=True):
        base = os.path.join(root, "o__r", str(pr))
        os.makedirs(base)
        with open(os.path.join(base, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump({"final_head": final_head}, handle)
        if with_diff:
            with open(os.path.join(base, final_head + ".diff"), "w", encoding="utf-8") as handle:
                handle.write("diff --git a/x b/x\n")

    def test_complete_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self._cache(root, 1, "abc")
            rows = [{"repo": "o/r", "pr": 1}]
            self.assertEqual(missing_fetch(rows, root), [])

    def test_missing_diff_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            self._cache(root, 1, "abc", with_diff=False)
            rows = [{"repo": "o/r", "pr": 1}]
            self.assertEqual(missing_fetch(rows, root), [("o/r", 1, "abc.diff")])

    def test_missing_pr_dir_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            rows = [{"repo": "o/r", "pr": 9}]
            self.assertEqual(missing_fetch(rows, root), [("o/r", 9, "meta.json")])

    def _doc_cache(self, root, pr, head, doc="docs/a.md", status="added",
                   with_doc=True, rows=None):
        base = os.path.join(root, "o__r", str(pr))
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump({"final_head": head}, handle)
        mode = "new file mode 100644" if status == "added" else (
            "deleted file mode 100644" if status == "deleted" else "index 000..111")
        diff = (f"diff --git a/{doc} b/{doc}\n{mode}\n"
                f"@@ -0,0 +1 @@\n+# A\n")
        with open(os.path.join(base, head + ".diff"), "w", encoding="utf-8") as handle:
            handle.write(diff)
        if with_doc:
            docdir = os.path.join(base, "docs", head)
            os.makedirs(docdir, exist_ok=True)
            with open(os.path.join(docdir, doc.replace("/", "__")),
                      "w", encoding="utf-8") as handle:
                handle.write("# A\n")
        if rows is None:
            rows = [{"repo": "o/r", "pr": pr, "check": "docs_vs_diff",
                     "carry_over": False, "location": "doc", "commit": head}]
        return rows

    def test_missing_final_head_doc_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            rows = self._doc_cache(root, 1, "abc", with_doc=False)
            self.assertEqual(missing_fetch(rows, root),
                             [("o/r", 1, "docs/abc/docs/a.md")])

    def test_present_doc_is_fetch_complete(self):
        with tempfile.TemporaryDirectory() as root:
            rows = self._doc_cache(root, 1, "abc", with_doc=True)
            self.assertEqual(missing_fetch(rows, root), [])

    def test_code_only_diff_needs_no_docs(self):
        with tempfile.TemporaryDirectory() as root:
            rows = self._doc_cache(root, 1, "abc", doc="src/app.py",
                                   with_doc=False)
            self.assertEqual(missing_fetch(rows, root), [])

    def test_deleted_doc_needs_no_fetch(self):
        with tempfile.TemporaryDirectory() as root:
            rows = self._doc_cache(root, 1, "abc", status="deleted",
                                   with_doc=False)
            self.assertEqual(missing_fetch(rows, root), [])

    def test_unscored_head_needs_no_docs(self):
        with tempfile.TemporaryDirectory() as root:
            rows = self._doc_cache(root, 1, "abc", with_doc=True)
            rows[0]["commit"] = "abc"
            base = os.path.join(root, "o__r", "1")
            with open(os.path.join(base, "def.diff"), "w", encoding="utf-8") as handle:
                handle.write("diff --git a/docs/b.md b/docs/b.md\nnew file mode 100644\n"
                             "@@ -0,0 +1 @@\n+# B\n")
            self.assertEqual(missing_fetch(rows, root), [])

    def test_finding_head_missing_diff_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            rows = self._doc_cache(root, 1, "abc", with_doc=True)
            rows[0]["commit"] = "def"
            self.assertEqual(missing_fetch(rows, root), [("o/r", 1, "def.diff")])


class SentUnitsTest(unittest.TestCase):
    """Hunks arms score only the units the send gate mailed."""

    HUNKS = {"u1": {"jev": {"p_true": 0.1}, "clef-flash": {"p_true": 0.9}, "clef": {"p_true": 0.9}},
             "u2": {"jev": {"p_true": 0.9}}}

    def test_intersects_providers(self):
        self.assertEqual(sent_ids(self.HUNKS, ("jev",)), {"u1", "u2"})
        self.assertEqual(sent_ids(self.HUNKS, ("jev", "clef-flash", "clef")), {"u1"})

    def test_empty_when_nothing_sent(self):
        self.assertEqual(sent_ids({}, ("jev",)), set())

    def test_public_result_filename_convention_loads_only_matching_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {"id": "u1", "provider": "jev", "answers": {"section_true": {"noul": 0.2}}, "usage": {"cost": 0.01}}
            (Path(tmp) / "review-docs-hunks-fixture.results.jsonl").write_text(json.dumps(record) + "\n")
            self.assertEqual(load_results(tmp, "hunks"), {"u1": {"jev": {"p_true": 0.2, "cost": 0.01}}})
            self.assertEqual(load_results(tmp, "full"), {})


if __name__ == "__main__":
    unittest.main()
