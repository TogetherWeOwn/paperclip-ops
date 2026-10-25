#!/usr/bin/env python3
"""Immutable acceptance: execute the public AX summary, not a copied renderer.

Builder: import this file unchanged and fix the workflow, not these tests.
Fixtures are synthetic; the summary runs in a temporary cwd without credentials.
Only unittest and the existing PyYAML dependency are needed.

  python3 -B -m unittest discover -s templates/org-github/tests \
      -p 'test_ax_metric_summary.py' -v

Catches: collapsing partial and complete equal-valued rates, dropping evidence
counts or qualifications, confusing absent evidence with numeric zero, and
exposing private cost/card fields. Wording and Markdown layout are not pinned.
"""

import copy
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "ax-metrics"))
ax_metrics = importlib.import_module("ax_metrics")

WINDOW_START = "2026-10-13T00:00:00Z"
WINDOW_END = "2026-10-20T00:00:00Z"


def github_row(total, states, *, repo="Example/alpha", truncated=False):
    return {"repo": repo, "merged_prs": total, "truncated": truncated,
            "first_commit_states": list(states)}


def report_for(rows):
    # Use the real producer boundary; block accidental collection/network I/O.
    with mock.patch("urllib.request.urlopen", side_effect=AssertionError(
            "synthetic report attempted HTTP access")), mock.patch(
            "socket.socket", side_effect=AssertionError(
                "synthetic report attempted network access")):
        return ax_metrics.build_report(
            rows, 971354213,
            [{"status": "blocked", "updatedAt": "2026-10-19T00:00:00Z"},
             {"status": "done", "updatedAt": "2026-10-19T00:00:00Z"}],
            WINDOW_START, 7,
            cost_evidence={"tokens": 971354213,
                           "window_start": WINDOW_START,
                           "window_end": WINDOW_END})


class WorkflowSummaryAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = yaml.safe_load(
            (BASE / "ax-metrics" / "ax-metrics.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["ax-metrics"]["steps"]
        cls.summary_run = next(step["run"] for step in steps
                               if step.get("name") == "Summarize public-safe subset")

    def summarize(self, report):
        with tempfile.TemporaryDirectory(prefix="ax-summary-") as directory:
            cwd = Path(directory)
            (cwd / "ax-metrics-report.json").write_text(
                json.dumps(report, allow_nan=False), encoding="utf-8")
            summary_path = cwd / "summary.md"
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail",
                 "-c", self.summary_run],
                cwd=cwd, text=True, capture_output=True, timeout=10,
                env={"PATH": os.pathsep.join(
                         (str(Path(sys.executable).parent), "/usr/bin", "/bin")),
                     "HOME": directory, "LANG": "C.UTF-8",
                     "GITHUB_STEP_SUMMARY": str(summary_path)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertTrue(summary_path.is_file(), "workflow wrote no step summary")
            summary = summary_path.read_text(encoding="utf-8")
            self.assertTrue(summary.strip(), "workflow wrote an empty step summary")
            return summary

    def green_section(self, summary):
        # Tolerate natural-language labels, Markdown, and multi-line details.
        normalized = re.sub(r"[`*_]", " ", summary.lower())
        normalized = re.sub(r"[-]+", " ", normalized)
        start = re.search(r"first\s+push\s+green\s+rate", normalized)
        self.assertIsNotNone(start, summary)
        section = normalized[start.end():]
        end = re.search(r"tokens\s+per\s+merged\s+pr|escalations\s+per\s+100\s+cards",
                        section)
        return section[:end.start()] if end else section

    def assert_counts(self, summary, conclusive, merged):
        section = self.green_section(summary)
        self.assertRegex(section, r"\bconclusive\b", summary)
        ratio = re.search(
            rf"\b{conclusive}\s*(?:/|of|out\s+of)\s*{merged}\b", section)
        if ratio:
            return
        # A labelled pair is as valid as a ratio; do not prescribe a table.
        for label, count in (("conclusive", conclusive), ("merged", merged)):
            self.assertRegex(
                section,
                rf"(?:\b{label}(?:\s+prs)?\s*[:=|]?\s*{count}\b|"
                rf"\b{count}\s+{label}\b)", summary)

    def assert_qualification(self, summary, *, validity, completeness, reason):
        section = self.green_section(summary)
        for field, value in (("validity", validity),
                             ("completeness", completeness),
                             ("reason", reason.replace("_", " "))):
            with self.subTest(field=field):
                self.assertRegex(section, rf"\b{value}\b", summary)

    def assert_rate(self, summary, value):
        section = self.green_section(summary)
        patterns = {
            0.5: r"(?<![\d.])0?\.50*(?![\d.])|\b50(?:\.0+)?\s*%",
            0.25: r"(?<![\d.])0?\.250*(?![\d.])|\b25(?:\.0+)?\s*%",
            0.0: r"(?<![\d./])0(?:\.0+)?(?![\d./])",
        }
        self.assertRegex(section, patterns[value], summary)

    def test_equal_half_rates_have_distinct_partial_and_complete_summaries(self):
        partial = report_for([github_row(30, ["success", "failure"] + [None] * 28)])
        complete = report_for([github_row(30, ["success"] * 15 + ["failure"] * 15)])
        self.assertEqual(partial["metrics"]["first_push_green_rate"]["value"], 0.5)
        self.assertEqual(complete["metrics"]["first_push_green_rate"]["value"], 0.5)
        self.assertNotEqual(self.summarize(partial), self.summarize(complete),
                            "equal rates must not hide different evidence populations")

    def test_partial_half_discloses_qualification_and_two_of_thirty_conclusive(self):
        report = report_for([github_row(30, ["success", "failure"] + [None] * 28)])
        green = report["metrics"]["first_push_green_rate"]
        self.assertEqual((green["validity"], green["completeness"], green["reason_code"]),
                         ("partial", "partial", "sample_partial"))
        summary = self.summarize(report)
        self.assert_rate(summary, 0.5)
        self.assert_qualification(summary, validity="partial", completeness="partial",
                                  reason="sample_partial")
        self.assert_counts(summary, 2, 30)

    def test_complete_half_discloses_valid_complete_ok_and_thirty_of_thirty(self):
        report = report_for([github_row(30, ["success"] * 15 + ["failure"] * 15)])
        summary = self.summarize(report)
        self.assert_rate(summary, 0.5)
        self.assert_qualification(summary, validity="valid", completeness="complete",
                                  reason="ok")
        self.assert_counts(summary, 30, 30)

    def test_positive_population_without_conclusive_evidence_is_withheld_unknown(self):
        for states in ([], [None] * 30, ["pending", "unknown"] * 15):
            with self.subTest(states=states):
                report = report_for([github_row(30, states)])
                self.assertIsNone(report["metrics"]["first_push_green_rate"]["value"])
                summary = self.summarize(report)
                self.assert_qualification(summary, validity="withheld",
                                          completeness="unknown", reason="no_evidence")
                self.assert_counts(summary, 0, 30)

    def test_empty_population_is_no_prs_not_an_observed_zero(self):
        for rows in ([], [github_row(0, [])]):
            with self.subTest(rows=rows):
                report = report_for(rows)
                self.assertIsNone(report["metrics"]["first_push_green_rate"]["value"])
                summary = self.summarize(report)
                self.assert_qualification(summary, validity="withheld",
                                          completeness="complete", reason="no_prs")
                if "counts" in report["metrics"]["first_push_green_rate"]:
                    self.assert_counts(summary, 0, 0)

    def test_fully_observed_failure_population_keeps_numeric_zero(self):
        report = report_for([github_row(30, ["failure", "error"] * 15)])
        self.assertEqual(report["metrics"]["first_push_green_rate"]["value"], 0.0)
        summary = self.summarize(report)
        self.assert_rate(summary, 0.0)
        self.assertNotRegex(self.green_section(summary), r"\b(?:none|null|withheld)\b")
        self.assert_qualification(summary, validity="valid", completeness="complete",
                                  reason="ok")
        self.assert_counts(summary, 30, 30)

    def test_truncation_stays_partial_even_when_all_reported_states_are_conclusive(self):
        for states, conclusive in ((["success", "failure"], 2),
                                   (["success"] * 15 + ["failure"] * 15, 30)):
            with self.subTest(conclusive=conclusive):
                report = report_for([github_row(30, states, truncated=True)])
                summary = self.summarize(report)
                self.assert_rate(summary, 0.5)
                self.assert_qualification(summary, validity="partial",
                                          completeness="partial", reason="sample_partial")
                self.assert_counts(summary, conclusive, 30)

    def test_multiple_repositories_use_population_weighted_rate_and_counts(self):
        report = report_for([
            github_row(1, ["success"]),
            github_row(3, ["failure", "failure", "error"], repo="Example/beta"),
        ])
        summary = self.summarize(report)
        self.assertIn("Example/alpha", summary)
        self.assertIn("Example/beta", summary)
        self.assert_rate(summary, 0.25)
        self.assert_qualification(summary, validity="valid", completeness="complete",
                                  reason="ok")
        self.assert_counts(summary, 4, 4)

    def test_multi_repo_partial_counts_include_inconclusive_and_empty_truncated_rows(self):
        report = report_for([
            github_row(10, ["success", None] + [None] * 8),
            github_row(20, ["failure"] + [None] * 19, repo="Example/beta"),
            github_row(0, [], repo="Example/gamma", truncated=True),
        ])
        summary = self.summarize(report)
        for repo in ("Example/alpha", "Example/beta", "Example/gamma"):
            self.assertIn(repo, summary)
        self.assert_rate(summary, 0.5)
        self.assert_qualification(summary, validity="partial", completeness="partial",
                                  reason="sample_partial")
        self.assert_counts(summary, 2, 30)

    def test_private_metric_fields_never_enter_summary_for_present_or_absent_values(self):
        original = report_for([github_row(30, ["success"] * 15 + ["failure"] * 15)])
        for key, number in (("tokens_per_merged_pr", 87654321.125),
                            ("escalations_per_100_cards", 76.543219875)):
            for value in (number, None):
                with self.subTest(metric=key, value=value):
                    report = copy.deepcopy(original)
                    metric = report["metrics"][key]
                    marker = "synthetic-private-" + key
                    canaries = []
                    for field in ("definition", "provenance", "reason", "next_action",
                                  "numerator_source", "denominator_source"):
                        canary = marker + "-" + field
                        metric[field] = canary
                        canaries.append(canary)
                    metric["value"] = value
                    metric["numerator"] = 91357249
                    metric["denominator"] = 92368751
                    metric["validity"] = "withheld" if value is None else "valid"
                    metric["reason_code"] = "no_evidence" if value is None else "ok"
                    summary = self.summarize(report)
                    self.assertIn("first", summary.lower())
                    for canary in canaries + [str(number), "91357249", "92368751"]:
                        self.assertNotIn(canary, summary,
                                         "public summary exposed a private metric field")


if __name__ == "__main__":
    unittest.main()
