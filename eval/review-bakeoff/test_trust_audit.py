#!/usr/bin/env python3
"""Independent contract for trust_audit.py; synthetic inputs, no model calls.

Run: PYTHONPATH=eval/review-bakeoff python3 -B -m unittest -v test_trust_audit

Output: dimensions[name] = {status, denominator, value, reason}.
Dataset value counts cases/results/failed_results/findings; scoring value counts
 automatic_location/explicit_defect/valid_other/invalid/unjudged/conflicts.
Automatic credit and explicit labels are separate, potentially overlapping axes;
unjudged means no explicit label, even if location supplies automatic credit.
Queue: {total, items: [{kind, id, reason}], truncated}, at most 100 items.
Ambiguous identities and invalid inputs raise ValueError rather than being used.
CLI errors are JSON objects with a nonempty error, on stdout, and exit 2.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import run_review
import score
import trust_audit


CASE = {"id": "c1", "defects": [
    {"id": "d1", "file": "src/a.ts", "line_start": 100, "line_end": 110, "severity": "major"},
    {"id": "d2", "file": "src/b.ts", "line_start": 5, "line_end": 5, "severity": "critical"},
]}
DIMENSIONS = {
    "dataset_audit", "scoring_mechanism_attribution", "calibration", "ordering_bias",
    "verbosity_bias", "causal_mechanism_attribution", "uncertainty",
}
UNSUPPORTED = {"calibration", "ordering_bias", "verbosity_bias", "causal_mechanism_attribution"}
SCRIPT = Path(__file__).with_name("trust_audit.py")


def finding(file="src/a.ts", line=105):
    return {"file": file, "line": line, "severity": "major",
            "title": "synthetic finding", "rationale": "synthetic rationale"}


def result(findings, score_=3):
    return {"case_id": "c1", "model": "m", "latency_s": 10, "usage": {},
            "review": {"score": score_, "summary": "", "findings": findings,
                       "coverage": {"files_reviewed": [], "complete": True, "limitations": ""}}}


class AuditContract(unittest.TestCase):
    def audit(self, runs, labels=None, cases=None):
        inputs = (copy.deepcopy({"cases": [CASE]} if cases is None else cases),
                  copy.deepcopy(runs), copy.deepcopy({} if labels is None else labels))
        before = copy.deepcopy(inputs)
        report = trust_audit.audit(*inputs)
        self.assertEqual(inputs, before, "audit must not mutate any caller-owned input")
        json.dumps(report, allow_nan=False)
        self.assertTrue(DIMENSIONS <= report["dimensions"].keys())
        for name in DIMENSIONS:
            dimension = report["dimensions"][name]
            self.assertIn(dimension["status"], ("measured", "abstain"), name)
            self.assertIs(type(dimension["denominator"]), int, name)
            self.assertGreaterEqual(dimension["denominator"], 0, name)
            self.assertIsInstance(dimension["reason"], str, name)
            self.assertTrue(dimension["reason"].strip(), name)
            if dimension["status"] == "abstain":
                self.assertIsNone(dimension["value"], name)
            else:
                self.assertIsNotNone(dimension["value"], name)
        queue = report["queue"]
        self.assertIs(type(queue["total"]), int)
        self.assertGreaterEqual(queue["total"], len(queue["items"]))
        self.assertLessEqual(len(queue["items"]), 100)
        self.assertEqual(queue["truncated"], queue["total"] > len(queue["items"]))
        for item in queue["items"]:
            self.assertTrue(all(isinstance(item[key], str) and item[key].strip()
                                for key in ("kind", "id", "reason")))
        return report

    def attribution(self, report):
        dimension = report["dimensions"]["scoring_mechanism_attribution"]
        self.assertEqual(dimension["status"], "measured")
        return dimension["value"]

    def assert_rejected(self, cases, runs, labels, reason, identity=None):
        inputs = copy.deepcopy((cases, runs, labels))
        before = copy.deepcopy(inputs)
        with self.assertRaises(ValueError) as caught:
            trust_audit.audit(*inputs)
        self.assertEqual(inputs, before)
        message = str(caught.exception).lower()
        self.assertIn(reason, message)
        if identity is not None:
            self.assertIn(identity.lower(), message)

    def test_producer_score_boundaries_remain_valid_saved_reviews(self):
        for value in (0, 5):
            with self.subTest(score=value):
                run = result([finding()], score_=value)
                parsed, error = run_review.parse_review(json.dumps(run["review"]))
                self.assertIsNone(error)
                self.assertIsNotNone(parsed)
                run["review"] = parsed
                report = self.audit([("boundary", run)])
                self.assertEqual(report["dimensions"]["dataset_audit"]["value"],
                                 {"cases": 1, "results": 1, "failed_results": 0, "findings": 1})
                self.assertEqual(self.attribution(report)["automatic_location"], 1)

    def test_scores_outside_producer_domain_are_rejected(self):
        for value in (-1, 6, False, True, 0.0, 5.0, "3", None):
            with self.subTest(score=value):
                self.assert_rejected({"cases": [CASE]},
                                     [("bad-score", result([], score_=value))], {},
                                     "score", "bad-score")

    def test_invalid_location_hit_is_a_conflict_not_silent_credit(self):
        run = result([finding()])
        report = self.audit([("run", run)], {"run#0": "invalid"})
        counts = self.attribution(report)
        self.assertEqual((counts["automatic_location"], counts["invalid"], counts["conflicts"]),
                         (1, 1, 1))
        conflicts = [item for item in report["queue"]["items"]
                     if item["kind"] == "judgment_conflict"]
        self.assertEqual([item["id"] for item in conflicts], ["run#0"])
        # Historical scoring remains location-first; the audit must not rewrite it.
        legacy = score.score_result(run, CASE, {"run#0": "invalid"}, "run")
        self.assertEqual((legacy["matched"], legacy["invalid"]), (1, 0))

    def test_attribution_separates_automatic_and_explicit_observations(self):
        run = result([finding(), finding("other.ts", 1), finding("other.ts", 2),
                      finding("other.ts", 3), finding("other.ts", 4), finding()])
        labels = {"run#0": "invalid", "run#1": "valid-other", "run#2": "d1",
                  "run#3": "invalid", "run#5": "d1"}
        report = self.audit([("run", run)], labels)
        counts = self.attribution(report)
        expected = {"automatic_location": 2, "explicit_defect": 2, "valid_other": 1,
                    "invalid": 2, "unjudged": 1, "conflicts": 1}
        self.assertEqual({key: counts[key] for key in expected}, expected)
        self.assertEqual(report["dimensions"]["scoring_mechanism_attribution"]["denominator"], 6)

    def test_coverage_requires_nonconflicting_positive_explicit_evidence(self):
        # Independent expected counts, not a comparison with another audit call.
        examples = [
            ("src/a.ts", None, 0),
            ("src/a.ts", "d1", 1),
            ("src/a.ts", "d2", 0),
            ("src/a.ts", "invalid", 0),
            ("src/a.ts", "valid-other", 0),
            ("other.ts", None, 0),
            ("other.ts", "d1", 1),
            ("other.ts", "d2", 1),
            ("other.ts", "invalid", 0),
            ("other.ts", "valid-other", 1),
        ]
        for file, label, credit in examples:
            with self.subTest(file=file, label=label):
                labels = {} if label is None else {"run#0": label}
                report = self.audit([("run", result([finding(file)]))], labels)
                self.assertEqual(report["coverage"], {
                    "explicit_judgments": 0 if label is None else 1,
                    "findings": 1, "nonconflicting_explicit_credit": credit,
                })

    def test_mixed_coverage_does_not_restore_location_only_credit(self):
        run = result([finding(), finding("other.ts", 1), finding("other.ts", 2),
                      finding("other.ts", 3), finding("other.ts", 4), finding()])
        labels = {"run#0": "invalid", "run#1": "valid-other", "run#2": "d1",
                  "run#3": "invalid", "run#5": "d1"}
        report = self.audit([("run", run)], labels)
        self.assertEqual(report["coverage"], {"explicit_judgments": 5, "findings": 6,
                                            "nonconflicting_explicit_credit": 3})

    def test_other_disagreeing_labels_also_expose_location_conflicts(self):
        for label in ("valid-other", "d2"):
            with self.subTest(label=label):
                report = self.audit([("run", result([finding()]))], {"run#0": label})
                self.assertEqual(self.attribution(report)["conflicts"], 1)
                self.assertTrue(any(item["kind"] == "judgment_conflict" and item["id"] == "run#0"
                                    for item in report["queue"]["items"]))

    def test_unlabeled_findings_are_not_assumed_correct_or_incorrect(self):
        report = self.audit([("run", result([finding(), finding("other.ts", 1)]))])
        counts = self.attribution(report)
        self.assertEqual((counts["automatic_location"], counts["unjudged"], counts["invalid"]),
                         (1, 2, 0))
        self.assertEqual({item["id"] for item in report["queue"]["items"]
                          if item["kind"] == "unjudged"}, {"run#0", "run#1"})
        uncertainty = report["dimensions"]["uncertainty"]
        self.assertEqual((uncertainty["status"], uncertainty["denominator"], uncertainty["value"]),
                         ("abstain", 0, None))

    def test_matching_preserves_scorer_slack_suffix_and_file_boundaries(self):
        findings = [finding("repo/src/a.ts", line) for line in (84, 85, 125, 126)]
        findings += [finding("xsrc/a.ts"), finding("src/other.ts")]
        expected = sum(score.match_defect(item, CASE["defects"]) is not None for item in findings)
        self.assertEqual(expected, 2)
        report = self.audit([("run", result(findings))])
        self.assertEqual(self.attribution(report)["automatic_location"], expected)

    def test_failed_saved_run_is_counted_without_looking_successful(self):
        failed = result([])
        failed["review"] = None
        report = self.audit([("ok", result([finding()])), ("failed", failed)])
        dataset = report["dimensions"]["dataset_audit"]
        self.assertEqual(dataset["status"], "measured")
        expected = {"cases": 1, "results": 2, "failed_results": 1, "findings": 1}
        self.assertEqual({key: dataset["value"][key] for key in expected}, expected)
        self.assertTrue(any(item["kind"] == "failed_result" and item["id"] == "failed"
                            for item in report["queue"]["items"]))
        self.assertEqual(self.attribution(report)["automatic_location"], 1)

    def test_empty_input_explicitly_abstains(self):
        report = self.audit([], cases={"cases": []})
        for name in DIMENSIONS:
            dimension = report["dimensions"][name]
            self.assertEqual((dimension["status"], dimension["denominator"], dimension["value"]),
                             ("abstain", 0, None), name)
        self.assertEqual(report["queue"], {"total": 0, "items": [], "truncated": False})

    def test_labels_scores_and_observational_metadata_cannot_support_causal_claims(self):
        runs = [("short", result([finding()], score_=5)),
                ("long", result([finding("other.ts", 1)], score_=1))]
        runs[1][1]["review"]["summary"] = "synthetic verbose summary " * 200
        for index, (_, run) in enumerate(runs):
            run.update({"verdict": "approved", "merged": True, "survived": True,
                        "order": index, "confidence": 0.99})
        report = self.audit(runs, {"short#0": "d1", "long#0": "invalid"})
        for name in UNSUPPORTED | {"uncertainty"}:
            dimension = report["dimensions"][name]
            self.assertEqual((dimension["status"], dimension["value"]), ("abstain", None), name)
        for name in UNSUPPORTED:
            self.assertEqual(report["dimensions"][name]["denominator"], 0, name)

    def test_duplicate_case_result_and_local_defect_ids_are_explicit_errors(self):
        duplicate_defects = copy.deepcopy(CASE)
        duplicate_defects["defects"].append(copy.deepcopy(CASE["defects"][0]))
        examples = [
            ({"cases": [CASE, copy.deepcopy(CASE)]}, [("run", result([finding()]))], "c1"),
            ({"cases": [CASE]}, [("run", result([finding()])), ("run", result([]))], "run"),
            ({"cases": [duplicate_defects]}, [("run", result([finding()]))], "d1"),
        ]
        for cases, runs, identity in examples:
            with self.subTest(identity=identity):
                self.assert_rejected(cases, runs, {"run#0": "d1"}, "duplicate", identity)

    def test_defect_ids_are_scoped_to_their_case_not_globally_unique(self):
        other = copy.deepcopy(CASE)
        other["id"] = "c2"
        run = result([finding()])
        run["case_id"] = "c2"
        report = self.audit([("run", run)], {"run#0": "d1"}, {"cases": [CASE, other]})
        self.assertEqual(self.attribution(report)["explicit_defect"], 1)

    def test_unknown_case_and_orphan_judgments_are_explicit_errors(self):
        unknown = result([finding()])
        unknown["case_id"] = "missing-case"
        self.assert_rejected({"cases": [CASE]}, [("run", unknown)], {}, "case", "missing-case")
        for key in ("missing#0", "run#1", "run#-1"):
            with self.subTest(key=key):
                self.assert_rejected({"cases": [CASE]}, [("run", result([finding()]))],
                                     {key: "invalid"}, "judgment", key)

    def test_labels_must_name_allowed_verdict_or_a_defect_in_this_case(self):
        for label in ("d999", "valid", "", None, 1):
            with self.subTest(label=label):
                self.assert_rejected({"cases": [CASE]}, [("run", result([finding()]))],
                                     {"run#0": label}, "label", "run#0")

    def test_non_object_results_and_malformed_findings_are_not_saved_failures(self):
        for malformed in (None, [], "not a result", {},
                          {**result([]), "review": {}},
                          {**result([]), "review": {"score": 3, "findings": None}}):
            with self.subTest(result=malformed):
                self.assert_rejected({"cases": [CASE]}, [("bad", malformed)], {}, "result", "bad")
        for bad_finding in (None, {"line": 1}, finding(line="105")):
            with self.subTest(finding=bad_finding):
                self.assert_rejected({"cases": [CASE]}, [("bad", result([bad_finding]))],
                                     {}, "finding", "bad")

    def test_finding_lines_require_positive_nonboolean_integers(self):
        for value in (0, -1, False, True, 1.0, "1", None):
            with self.subTest(line=value):
                self.assert_rejected({"cases": [CASE]},
                                     [("bad-line", result([finding(line=value)]))], {},
                                     "positive integer", "finding bad-line#0 line")

    def test_both_defect_endpoints_require_positive_nonboolean_integers(self):
        for endpoint in ("line_start", "line_end"):
            for value in (0, -1, False, True, 1.0, "1", None):
                with self.subTest(endpoint=endpoint, value=value):
                    case = copy.deepcopy(CASE)
                    case["defects"][0][endpoint] = value
                    self.assert_rejected({"cases": [case]}, [], {},
                                         "positive integer", f"case c1 defect 0 {endpoint}")

    def test_severity_is_validated_on_findings_and_defects(self):
        for value in ("", "MAJOR", "unknown", None, 1, [], {}):
            with self.subTest(record="finding", severity=value):
                bad_finding = {**finding(), "severity": value}
                self.assert_rejected({"cases": [CASE]},
                                     [("bad-severity", result([bad_finding]))], {},
                                     "severity", "finding bad-severity#0")
            with self.subTest(record="defect", severity=value):
                case = copy.deepcopy(CASE)
                case["defects"][0]["severity"] = value
                self.assert_rejected({"cases": [case]}, [], {}, "severity", "case c1 defect 0")

    def test_judgment_verdict_names_cannot_be_defect_ids(self):
        for reserved in ("invalid", "valid-other"):
            with self.subTest(id=reserved):
                case = copy.deepcopy(CASE)
                case["defects"][0]["id"] = reserved
                self.assert_rejected({"cases": [case]}, [], {}, "reserved defect id", reserved)

    def test_positive_line_and_severity_boundaries_remain_valid(self):
        for severity in ("minor", "major", "critical"):
            with self.subTest(severity=severity):
                case = {"id": "c1", "defects": [{"id": "d1", "file": "src/a.ts",
                        "line_start": 1, "line_end": 1, "severity": severity}]}
                first_line = {**finding(line=1), "severity": severity}
                report = self.audit([("run", result([first_line]))], {"run#0": "d1"},
                                    {"cases": [case]})
                self.assertEqual(report["coverage"], {"explicit_judgments": 1, "findings": 1,
                                                    "nonconflicting_explicit_credit": 1})
                self.assertEqual(self.attribution(report)["automatic_location"], 1)

    def test_queue_is_bounded_deterministic_and_does_not_copy_review_text(self):
        sentinel = "SYNTHETIC_REVIEW_TEXT_NOT_FOR_AUDIT_OUTPUT"
        runs = []
        for key in ("z", "a"):
            run = result([finding("other.ts", n + 1) for n in range(137)])
            run["review"]["summary"] = sentinel
            for item in run["review"]["findings"]:
                item.update({"title": sentinel, "rationale": sentinel})
            runs.append((key, run))
        report = self.audit(runs)
        self.assertEqual(report["queue"]["total"], 274)
        self.assertTrue(report["queue"]["items"])
        self.assertTrue(report["queue"]["truncated"])
        self.assertNotIn(sentinel, json.dumps(report))
        self.assertEqual(report, self.audit(runs))
        self.assertEqual(report, self.audit(list(reversed(runs))))


class CommandLine(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cases = self.root / "cases.json"
        self.cases.write_text(json.dumps({"cases": [CASE]}), encoding="utf-8")
        self.results = self.root / "results"
        self.results.mkdir()
        self.run_path = self.results / "run.json"
        self.run_path.write_text(json.dumps(result([finding()])), encoding="utf-8")
        self.judgments = self.root / "judgments.json"
        self.judgments.write_text('{"run#0": "invalid"}', encoding="utf-8")

    def cli(self, *, cases=None, results=None, judgments=None):
        command = [sys.executable, "-B", str(SCRIPT), "--cases", str(cases or self.cases),
                   "--results", str(results or self.results)]
        if judgments is not None:
            command += ["--judgments", str(judgments)]
        completed = subprocess.run(command, text=True, capture_output=True, timeout=10)
        try:
            body = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self.fail(f"CLI must emit JSON stdout; exit={completed.returncode}; "
                      f"stdout={completed.stdout!r}; stderr={completed.stderr!r}")
        self.assertIsInstance(body, dict)
        return completed, body

    def assert_cli_error(self, **paths):
        completed, body = self.cli(**paths)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertTrue(body.get("error"), body)
        self.assertNotIn("dimensions", body, "invalid input must not look like a successful audit")
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)

    def test_cli_uses_basename_keys_skips_private_files_and_is_deterministic(self):
        (self.results / "_metadata.json").write_text("not JSON", encoding="utf-8")
        (self.results / "ignored.txt").write_text("not JSON", encoding="utf-8")
        (self.results / "nested").mkdir()
        (self.results / "nested" / "ignored.json").write_text("not JSON", encoding="utf-8")
        completed, body = self.cli(judgments=self.judgments)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(body, trust_audit.audit({"cases": [CASE]},
                                               [("run", result([finding()]))], {"run#0": "invalid"}))
        repeated, _ = self.cli(judgments=self.judgments)
        self.assertEqual(completed.stdout, repeated.stdout)

    def test_dangling_result_reference_rejects_the_whole_inventory(self):
        # Keep the valid run: losing one reference must not return partial success.
        (self.results / "lost.json").symlink_to(self.root / "missing-result.json")
        self.assert_cli_error()

    def test_readable_result_symlink_is_included_in_the_inventory(self):
        saved = self.root / "saved.json"
        saved.write_text(json.dumps(result([finding()])), encoding="utf-8")
        (self.results / "linked.json").symlink_to(saved)
        completed, body = self.cli()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(body["dimensions"]["dataset_audit"]["value"],
                         {"cases": 1, "results": 2, "failed_results": 0, "findings": 2})
        self.assertEqual({item["id"] for item in body["queue"]["items"]},
                         {"linked#0", "run#0"})

    def test_directory_and_ineligible_links_remain_excluded(self):
        nested = self.results / "nested.json"
        nested.mkdir()
        (nested / "bad.json").write_text("not JSON", encoding="utf-8")
        (self.results / "directory.json").symlink_to(nested, target_is_directory=True)
        for name in ("_private.json", "ignored.txt"):
            (self.results / name).symlink_to(self.root / "missing-result.json")
        completed, body = self.cli()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(body["dimensions"]["dataset_audit"]["value"],
                         {"cases": 1, "results": 1, "failed_results": 0, "findings": 1})

    def test_empty_results_directory_is_valid_but_missing_paths_are_errors(self):
        empty = self.root / "empty"
        empty.mkdir()
        completed, body = self.cli(results=empty)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(body["dimensions"]["scoring_mechanism_attribution"]["status"], "abstain")
        self.assert_cli_error(results=self.root / "missing")
        self.assert_cli_error(results=self.cases)
        self.assert_cli_error(cases=self.root / "missing.json")
        self.assert_cli_error(cases=self.results)
        self.assert_cli_error(judgments=self.root / "missing-judgments.json")

    def test_invalid_json_and_wrong_top_level_types_fail_on_every_input(self):
        for path, bad_values in ((self.cases, ("{", "null", "[]", '{"cases":null}')),
                                 (self.run_path, ("{", "null", "[]", '"text"')),
                                 (self.judgments, ("{", "null", "[]"))):
            original = path.read_text(encoding="utf-8")
            for raw in bad_values:
                with self.subTest(path=path.name, raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    self.assert_cli_error(judgments=self.judgments)
            path.write_text(original, encoding="utf-8")

    def test_duplicate_json_keys_fail_before_the_decoder_can_discard_them(self):
        duplicate_inputs = [
            (self.cases, '{"cases": [], "cases": []}'),
            (self.run_path, '{"case_id":"c1",' + json.dumps(result([finding()]))[1:]),
            (self.judgments, '{"run#0":"invalid", "run#0":"d1"}'),
            (self.run_path, json.dumps(result([finding()])).replace(
                '"line": 105', '"line": 105, "line": 106')),
        ]
        for path, raw in duplicate_inputs:
            with self.subTest(path=path.name, raw=raw):
                original = path.read_text(encoding="utf-8")
                path.write_text(raw, encoding="utf-8")
                self.assert_cli_error(judgments=self.judgments)
                path.write_text(original, encoding="utf-8")

    def test_malformed_case_defect_and_finding_cannot_become_zero_rows(self):
        malformed_cases = [{"cases": [None]}, {"cases": [{"id": "c1", "defects": None}]}]
        bad_case = copy.deepcopy(CASE)
        bad_case["defects"][0]["line_start"] = 111
        malformed_cases.append({"cases": [bad_case]})
        for cases in malformed_cases:
            with self.subTest(cases=cases):
                self.cases.write_text(json.dumps(cases), encoding="utf-8")
                self.assert_cli_error()
        self.cases.write_text(json.dumps({"cases": [CASE]}), encoding="utf-8")
        for findings in ([None], [{"line": 1}], [finding(line="105")]):
            with self.subTest(findings=findings):
                self.run_path.write_text(json.dumps(result(findings)), encoding="utf-8")
                self.assert_cli_error()


if __name__ == "__main__":
    unittest.main()
