#!/usr/bin/env python3
"""Tests for score.py. Run: python3 -m unittest -v test_score.py"""
import unittest

import score

CASE = {
    "id": "c1",
    "defects": [
        {"id": "d1", "file": "src/a.ts", "line_start": 100, "line_end": 110, "severity": "major"},
        {"id": "d2", "file": "src/b.ts", "line_start": 5, "line_end": 5, "severity": "critical"},
    ],
}


def finding(file, line, severity="major"):
    return {"file": file, "line": line, "severity": severity, "title": "t", "rationale": "r"}


def result(findings, model="m", score_=3, latency=10.0, usage=None):
    return {"case_id": "c1", "model": model, "latency_s": latency, "usage": usage or {},
            "review": {"score": score_, "summary": "", "findings": findings,
                       "coverage": {"files_reviewed": [], "complete": True, "limitations": ""}}}


class Matching(unittest.TestCase):
    def test_slack_boundaries_are_inclusive(self):
        d = CASE["defects"]
        self.assertEqual(score.match_defect(finding("src/a.ts", 100 - score.SLACK), d), "d1")
        self.assertEqual(score.match_defect(finding("src/a.ts", 110 + score.SLACK), d), "d1")
        self.assertIsNone(score.match_defect(finding("src/a.ts", 100 - score.SLACK - 1), d))
        self.assertIsNone(score.match_defect(finding("src/a.ts", 110 + score.SLACK + 1), d))

    def test_same_line_in_a_different_file_does_not_match(self):
        self.assertIsNone(score.match_defect(finding("src/other.ts", 105), CASE["defects"]))

    def test_path_suffix_matches_but_a_substring_does_not(self):
        self.assertTrue(score.file_matches("repo/src/a.ts", "src/a.ts"))
        self.assertTrue(score.file_matches("./src/a.ts", "src/a.ts"))
        self.assertFalse(score.file_matches("xsrc/a.ts", "src/a.ts"))
        self.assertFalse(score.file_matches("", "src/a.ts"))


class ScoreResult(unittest.TestCase):
    def test_two_findings_on_one_defect_find_it_once_but_count_two_matches(self):
        s = score.score_result(result([finding("src/a.ts", 101), finding("src/a.ts", 109)]), CASE, {}, "k")
        self.assertEqual(s["found"], {"d1"})
        self.assertEqual(s["matched"], 2)

    def test_unmatched_is_unjudged_until_a_judgment_exists(self):
        res = result([finding("src/zzz.ts", 1), finding("src/zzz.ts", 2), finding("src/zzz.ts", 3)])
        s = score.score_result(res, CASE, {"k#0": "valid-other", "k#1": "invalid"}, "k")
        self.assertEqual((s["valid_other"], s["invalid"], s["unjudged"], s["matched"]), (1, 1, 1, 0))

    def test_a_judgment_naming_a_defect_credits_it(self):
        s = score.score_result(result([finding("src/zzz.ts", 1)]), CASE, {"k#0": "d2"}, "k")
        self.assertEqual(s["found"], {"d2"})

    def test_severity_exact_and_within_one(self):
        res = result([finding("src/a.ts", 100, "minor"), finding("src/b.ts", 5, "major")])
        s = score.score_result(res, CASE, {}, "k")
        self.assertEqual((s["sev_exact"], s["sev_within1"], s["sev_compared"]), (0, 2, 2))

    def test_two_ranks_apart_is_not_within_one(self):
        res = result([finding("src/b.ts", 5, "minor")])  # defect d2 is critical
        s = score.score_result(res, CASE, {}, "k")
        self.assertEqual((s["sev_exact"], s["sev_within1"], s["sev_compared"]), (0, 0, 1))

    def test_failed_run_finds_nothing(self):
        bad = {"case_id": "c1", "model": "m", "latency_s": 1.0, "usage": {}, "review": None}
        s = score.score_result(bad, CASE, {}, "k")
        self.assertFalse(s["ok"])
        self.assertEqual(s["found"], set())


class Summaries(unittest.TestCase):
    def test_recall_counts_every_run_including_failed_ones(self):
        runs = [("a", result([finding("src/a.ts", 100)])),
                ("b", {"case_id": "c1", "model": "m", "latency_s": 1.0, "usage": {}, "review": None})]
        m = score.summarize({"c1": CASE}, runs, {})["m"]
        self.assertEqual((m["found"], m["defects"], m["failed"]), (1, 4, 1))

    def test_false_pass_counts_a_five_on_a_defective_case(self):
        runs = [("a", result([], score_=5)), ("b", result([], score_=3)), ("c", result([], score_=4))]
        m = score.summarize({"c1": CASE}, runs, {})["m"]
        self.assertEqual((m["false_pass"], m["defective_runs"]), (1, 3))

    def test_critical_defects_missed_are_counted_per_defect(self):
        runs = [("a", result([finding("src/a.ts", 100)]))]  # finds d1, misses critical d2
        m = score.summarize({"c1": CASE}, runs, {})["m"]
        self.assertEqual((m["critical_missed"], m["critical_total"]), (1, 1))
        runs = [("a", result([finding("src/b.ts", 5, "critical")]))]
        m = score.summarize({"c1": CASE}, runs, {})["m"]
        self.assertEqual((m["critical_missed"], m["critical_total"]), (0, 1))

    def test_tokens_sum_input_and_output_only(self):
        runs = [("a", result([], usage={"input_tokens": 1000, "output_tokens": 50,
                                        "cached_input_tokens": 900, "reasoning_output_tokens": 30}))]
        m = score.summarize({"c1": CASE}, runs, {})["m"]
        self.assertEqual(m["tokens"], [1050])

    def test_results_for_unknown_cases_are_ignored(self):
        stray = result([]); stray["case_id"] = "nope"
        self.assertEqual(dict(score.summarize({"c1": CASE}, [("a", stray)], {})), {})

    def test_recall_is_split_by_detectability(self):
        case = {"id": "c1", "defects": [
            {"id": "d1", "file": "a.ts", "line_start": 1, "line_end": 1, "severity": "minor",
             "detectability": "diff-only"},
            {"id": "d2", "file": "b.ts", "line_start": 1, "line_end": 1, "severity": "minor",
             "detectability": "needs-repo-context"},
            {"id": "d3", "file": "c.ts", "line_start": 1, "line_end": 1, "severity": "minor"}]}
        res = result([finding("a.ts", 1), finding("b.ts", 1)])
        m = score.summarize({"c1": case}, [("a", res)], {})["m"]
        self.assertEqual((m["found_diff-only"], m["def_diff-only"]), (1, 2))  # d3 defaults to diff-only
        self.assertEqual((m["found_needs-context"], m["def_needs-context"]), (1, 1))
        row = score.render({"m": m}).splitlines()[2]
        self.assertIn("| 1/2 | 1/1 |", row)

    def test_render_has_one_row_per_model(self):
        runs = [("a", result([], model="x")), ("b", result([], model="y"))]
        table = score.render(score.summarize({"c1": CASE}, runs, {}))
        self.assertEqual(len(table.splitlines()), 4)


class Stats(unittest.TestCase):
    def test_wilson_interval_bounds(self):
        lo, hi = score.wilson(0, 0)
        self.assertEqual((lo, hi), (0.0, 0.0))
        lo, hi = score.wilson(5, 10)
        self.assertTrue(0.2 < lo < 0.5 < hi < 0.8)
        lo, hi = score.wilson(10, 10)
        self.assertGreater(lo, 0.65)
        self.assertGreater(hi, 0.99)

    def test_percentile_picks_ordered_values(self):
        self.assertEqual(score.percentile([5, 1, 3], 0.5), 3)
        self.assertEqual(score.percentile([1, 2, 3, 4], 0.95), 4)
        self.assertIsNone(score.percentile([], 0.5))


if __name__ == "__main__":
    unittest.main()
