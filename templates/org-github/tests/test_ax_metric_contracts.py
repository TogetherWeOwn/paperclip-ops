#!/usr/bin/env python3
"""Immutable acceptance: versioned metric contracts and sampled green rates.

Builder: import this file unchanged; edit the implementation, not these tests.
All fixtures are synthetic. No installs, live services, or credentials needed.

  python3 -B -m unittest discover -s templates/org-github/tests -p 'test_ax_*py'

The contract version is independent of report schema v3. A numeric green rate
is a sample fraction over conclusive first-commit states, not automatically a
complete-population measure. These tests do not require a query redesign:
first-commit checks are observed at collection time, not historical first-push
verdicts; merged search uses a calendar-date lower bound only; cost windows
match dates, not instants; escalations remain a current blocked-state proxy.
Descriptions must explain the semantics but their exact prose is not pinned.
"""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "ax-metrics"))

import ax_metrics

WINDOW_START = "2026-10-13T00:00:00Z"
WINDOW_END = "2026-10-20T00:00:00Z"
CONTRACT_KEYS = {
    "id", "version", "unit", "row_grain", "time_semantics",
    "zero_semantics", "null_semantics",
}
METRIC_DIMENSIONS = {
    "first_push_green_rate": ("fraction", "repository_pull_request"),
    "tokens_per_merged_pr": (
        "tokens_per_pull_request",
        "company_token_total_per_repository_pull_request",
    ),
    "escalations_per_100_cards": (
        "blocked_cards_per_100_touched_cards", "company_issue",
    ),
}
LEGACY_METRIC_KEYS = {
    "value", "definition", "provenance", "validity", "reason_code",
    "reason", "next_action", "completeness", "source_window", "numerator",
    "denominator", "numerator_source", "denominator_source",
}


def github_row(total, states, truncated=False, repo="Example/a"):
    return {"repo": repo, "merged_prs": total, "truncated": truncated,
            "first_commit_states": list(states)}


def fixture(**overrides):
    data = {
        "github_rows": [github_row(2, ["success", "failure"])],
        "cost_tokens": 1000,
        "cost_evidence": {"tokens": 1000, "window_start": WINDOW_START,
                          "window_end": WINDOW_END},
        "issues": [
            {"status": "blocked", "updatedAt": "2026-10-19T00:00:00Z"},
            {"status": "done", "updatedAt": "2026-10-19T00:00:00Z"},
        ],
        "window_start": WINDOW_START,
    }
    data.update(overrides)
    return data


def report(**overrides):
    data = fixture(**overrides)
    return ax_metrics.build_report(
        data["github_rows"], data["cost_tokens"], data["issues"],
        data["window_start"], 7,
        paperclip_reason=data.get("paperclip_reason"),
        issues_truncated=data.get("issues_truncated", False),
        cost_evidence=data["cost_evidence"],
        issues_reason=data.get("issues_reason"))


class MetricContractAcceptanceTest(unittest.TestCase):
    def test_schema_v3_retains_legacy_report_and_metric_keys(self):
        result = report()
        self.assertIs(type(result["schema_version"]), int)
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["tool"], "ax-metrics")
        self.assertEqual(result["window_days"], 7)
        self.assertEqual(result["window_start"], WINDOW_START)
        self.assertEqual(result["window_end"], WINDOW_END)
        self.assertEqual(result["repos"], fixture()["github_rows"])
        self.assertEqual(result["merged_prs_total"], 2)
        self.assertIs(result["issues_truncated"], False)
        self.assertEqual(set(result["metrics"]), set(METRIC_DIMENSIONS))
        for name, metric in result["metrics"].items():
            with self.subTest(metric=name):
                self.assertLessEqual(LEGACY_METRIC_KEYS, set(metric))
                for key in ("definition", "provenance", "numerator_source",
                            "denominator_source"):
                    self.assertIsInstance(metric[key], str)
                    self.assertTrue(metric[key].strip())

    def test_exact_version_one_contract_on_every_metric_and_validity_path(self):
        cases = [
            {},
            {"github_rows": [github_row(8, ["success", "failure"], True)]},
            {"github_rows": [github_row(2, [None, "pending"])]},
            {"github_rows": [], "cost_tokens": None, "cost_evidence": None,
             "issues": None,
             "paperclip_reason": "paperclip credentials not configured",
             "issues_reason": "paperclip credentials not configured"},
            {"cost_evidence": {"tokens": 1000,
                               "window_start": "2026-10-01T00:00:00Z",
                               "window_end": "2026-10-08T00:00:00Z"},
             "issues_truncated": True},
        ]
        for index, overrides in enumerate(cases):
            result = report(**overrides)
            for name, (unit, grain) in METRIC_DIMENSIONS.items():
                with self.subTest(case=index, metric=name):
                    metric = result["metrics"][name]
                    self.assertIn("contract", metric)
                    contract = metric["contract"]
                    self.assertIs(type(contract), dict)
                    self.assertEqual(set(contract), CONTRACT_KEYS)
                    self.assertEqual(contract["id"], name)
                    self.assertIs(type(contract["version"]), int)
                    self.assertEqual(contract["version"], 1)
                    self.assertEqual(contract["unit"], unit)
                    self.assertEqual(contract["row_grain"], grain)
                    for field in ("time_semantics", "zero_semantics",
                                  "null_semantics"):
                        self.assertIsInstance(contract[field], str)
                        self.assertTrue(contract[field].strip(), field)

    def test_contract_mappings_are_distinct_within_a_report(self):
        metrics = report()["metrics"]
        contracts = []
        for name in METRIC_DIMENSIONS:
            self.assertIn("contract", metrics[name])
            contracts.append(metrics[name]["contract"])
        self.assertIsNot(contracts[0], contracts[1])
        self.assertIsNot(contracts[0], contracts[2])
        self.assertIsNot(contracts[1], contracts[2])

    def test_mutating_a_returned_contract_cannot_change_later_reports(self):
        for name in METRIC_DIMENSIONS:
            with self.subTest(metric=name):
                first = report()
                self.assertIn("contract", first["metrics"][name])
                contract = first["metrics"][name]["contract"]
                original = copy.deepcopy(contract)
                try:
                    contract["unit"] = "caller-modified"
                    contract["time_semantics"] = "caller-modified"
                    contract["extra"] = "must not leak"
                    later = report()
                    self.assertIsNot(
                        contract, later["metrics"][name]["contract"])
                    self.assertEqual(
                        later["metrics"][name]["contract"], original)
                finally:
                    # Avoid polluting other cases if a buggy implementation
                    # returned its shared definition mapping directly.
                    contract.clear()
                    contract.update(original)

    def test_build_report_is_deterministic_without_mutating_input(self):
        data = fixture()
        before = copy.deepcopy(data)
        first = report(**data)
        second = report(**data)
        self.assertEqual(data, before)
        self.assertEqual(json.dumps(first, allow_nan=False),
                         json.dumps(second, allow_nan=False))


class GreenRateCompletenessAcceptanceTest(unittest.TestCase):
    def green(self, rows):
        return report(github_rows=rows)["metrics"]["first_push_green_rate"]

    def assert_partial(self, metric, value, merged, sampled, conclusive):
        self.assertAlmostEqual(metric["value"], value)
        self.assertEqual(metric["validity"], "partial")
        self.assertEqual(metric["completeness"], "partial")
        self.assertEqual(metric["reason_code"], "sample_partial")
        for key in ("reason", "next_action"):
            self.assertIsInstance(metric[key], str)
            self.assertTrue(metric[key].strip())
        self.assert_counts(metric, merged, sampled, conclusive)

    def assert_counts(self, metric, merged, sampled, conclusive):
        self.assertIn("counts", metric)
        self.assertIs(type(metric["counts"]), dict)
        for key, expected in (("merged_prs", merged),
                              ("sampled_prs", sampled),
                              ("conclusive_prs", conclusive)):
            self.assertIn(key, metric["counts"])
            self.assertIs(type(metric["counts"][key]), int)
            self.assertEqual(metric["counts"][key], expected)

    def test_complete_conclusive_population_is_valid(self):
        metric = self.green([github_row(
            3, ["success", "failure", "error"])])
        self.assertAlmostEqual(metric["value"], 1 / 3)
        self.assertEqual(metric["validity"], "valid")
        self.assertEqual(metric["completeness"], "complete")
        self.assertEqual(metric["reason_code"], "ok")
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 3)

    def test_complete_multi_repo_fraction_is_not_an_average_of_repo_rates(self):
        metric = self.green([
            github_row(1, ["success"]),
            github_row(3, ["success", "failure", "error"], repo="Example/b"),
        ])
        self.assertEqual(metric["value"], 0.5)
        self.assertEqual(metric["validity"], "valid")
        self.assertEqual(metric["completeness"], "complete")
        self.assertEqual(metric["reason_code"], "ok")

    def test_subset_is_partial_even_without_a_truncation_flag(self):
        metric = self.green([github_row(10, ["success", "failure"])])
        self.assert_partial(metric, 0.5, merged=10, sampled=2, conclusive=2)
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 2)

    def test_pending_missing_and_unknown_states_never_enter_the_fraction(self):
        metric = self.green([github_row(
            6, ["success", "failure", "error", None, "pending", "unknown"])])
        self.assert_partial(metric, 1 / 3, merged=6, sampled=6, conclusive=3)
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 3)

    def test_counts_aggregate_all_repos_including_inconclusive_samples(self):
        metric = self.green([
            github_row(3, ["success", None]),
            github_row(5, ["success", "error", "pending"], repo="Example/b"),
        ])
        self.assert_partial(metric, 2 / 3, merged=8, sampled=5, conclusive=3)

    def test_any_truncated_row_prevents_complete_even_with_all_states_known(self):
        for index in (0, 1):
            with self.subTest(truncated_row=index):
                rows = [github_row(1, ["success"]),
                        github_row(1, ["failure"], repo="Example/b")]
                rows[index]["truncated"] = True
                metric = self.green(rows)
                self.assert_partial(metric, 0.5, merged=2, sampled=2,
                                    conclusive=2)

    def test_empty_truncated_repo_still_qualifies_other_repos_as_partial(self):
        metric = self.green([
            github_row(2, ["success", "failure"]),
            github_row(0, [], True, repo="Example/b"),
        ])
        self.assert_partial(metric, 0.5, merged=2, sampled=2, conclusive=2)

    def test_no_conclusive_evidence_for_merged_prs_is_unknown_not_complete(self):
        for truncated in (False, True):
            for states, sampled in (([], 0), ([None, "pending"], 2)):
                with self.subTest(truncated=truncated, states=states):
                    metric = self.green([github_row(5, states, truncated)])
                    self.assertIsNone(metric["value"])
                    self.assertEqual(metric["validity"], "withheld")
                    self.assertEqual(metric["reason_code"], "no_evidence")
                    self.assertEqual(metric["completeness"], "unknown")
                    self.assert_counts(metric, 5, sampled, 0)
                    for key in ("reason", "next_action"):
                        self.assertIsInstance(metric[key], str)
                        self.assertTrue(metric[key].strip())

    def test_no_merged_prs_is_complete_but_withheld_not_numeric_zero(self):
        for rows in ([], [github_row(0, [])]):
            with self.subTest(rows=rows):
                metric = self.green(rows)
                self.assertIsNone(metric["value"])
                self.assertEqual(metric["validity"], "withheld")
                self.assertEqual(metric["reason_code"], "no_prs")
                self.assertEqual(metric["completeness"], "complete")

    def test_fully_observed_zero_success_is_numeric_zero_not_null(self):
        metric = self.green([github_row(2, ["failure", "error"])])
        self.assertIs(type(metric["value"]), float)
        self.assertEqual(metric["value"], 0.0)
        self.assertEqual(metric["validity"], "valid")
        self.assertEqual(metric["completeness"], "complete")
        self.assertEqual(metric["reason_code"], "ok")

    def test_partial_zero_success_is_a_retained_sample_not_null(self):
        metric = self.green([github_row(5, ["failure", "error"], True)])
        self.assertIs(type(metric["value"]), float)
        self.assert_partial(metric, 0.0, merged=5, sampled=2, conclusive=2)

    def test_partial_green_evidence_does_not_degrade_other_two_metrics(self):
        metrics = report(github_rows=[github_row(
            10, ["success", "failure"], True)])["metrics"]
        self.assert_partial(metrics["first_push_green_rate"], 0.5,
                            merged=10, sampled=2, conclusive=2)
        self.assertEqual(metrics["tokens_per_merged_pr"]["value"], 100.0)
        self.assertEqual(metrics["escalations_per_100_cards"]["value"], 50.0)
        for name in ("tokens_per_merged_pr", "escalations_per_100_cards"):
            self.assertEqual(metrics[name]["validity"], "valid")
            self.assertEqual(metrics[name]["reason_code"], "ok")
            self.assertEqual(metrics[name]["completeness"], "complete")


class UnchangedMetricSemanticsAcceptanceTest(unittest.TestCase):
    def test_cost_window_still_matches_calendar_dates_not_instants(self):
        metric = report(cost_evidence={
            "tokens": 1000, "window_start": "2026-10-13T21:00:00Z",
            "window_end": "2026-10-20T23:00:00Z",
        })["metrics"]["tokens_per_merged_pr"]
        self.assertEqual(metric["value"], 500.0)
        self.assertEqual(metric["validity"], "valid")
        self.assertEqual(metric["reason_code"], "ok")

    def test_escalations_still_use_current_blocked_state_of_touched_cards(self):
        metric = report(issues=[
            {"status": "blocked", "updatedAt": "2026-10-19T00:00:00Z"},
            {"status": "done", "updatedAt": "2026-10-19T00:00:00Z"},
            {"status": "in_progress", "updatedAt": "2026-10-19T00:00:00Z"},
            {"status": "blocked", "updatedAt": "2026-01-01T00:00:00Z"},
        ])["metrics"]["escalations_per_100_cards"]
        self.assertAlmostEqual(metric["value"], 100 / 3)
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 3)
        self.assertEqual(metric["validity"], "valid")


class _UnreadableEnvironment(Mapping):
    def __getitem__(self, key):
        # argparse/gettext may inspect locale/terminal metadata, not credentials.
        metadata = {"LANGUAGE": "C", "LC_ALL": "C", "LC_MESSAGES": "C",
                    "LANG": "C", "COLUMNS": "80", "LINES": "24"}
        if key in metadata:
            return metadata[key]
        raise AssertionError("offline replay read environment credentials")

    def __iter__(self):
        raise AssertionError("offline replay iterated environment credentials")

    def __len__(self):
        raise AssertionError("offline replay inspected environment credentials")


class OfflineContractReplayAcceptanceTest(unittest.TestCase):
    def test_from_file_is_deterministic_without_network_or_environment_reads(self):
        data = fixture(github_rows=[github_row(5, ["success", "error"], True)])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "fixture.json")
            out_path = os.path.join(directory, "report.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    ax_metrics.os, "environ", _UnreadableEnvironment()))
                stack.enter_context(mock.patch(
                    "urllib.request.urlopen", side_effect=AssertionError(
                        "offline replay attempted an HTTP request")))
                stack.enter_context(mock.patch(
                    "socket.socket", side_effect=AssertionError(
                        "offline replay attempted network access")))
                direct = ax_metrics.collect_from_file(path, 7)
                outputs = []
                for _ in range(2):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = ax_metrics.main([
                            "--from-file", path, "--days", "7",
                            "--out", out_path])
                    self.assertEqual(code, 0)
                    self.assertEqual(stderr.getvalue(), "")
                    outputs.append(stdout.getvalue())
                self.assertEqual(outputs[0], outputs[1])
                with open(out_path, encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), outputs[0])
                self.assertEqual(json.loads(outputs[0]), direct)
                self.assertEqual(direct["schema_version"], 3)
                green = direct["metrics"]["first_push_green_rate"]
                self.assertEqual(green["value"], 0.5)
                self.assertEqual(green["validity"], "partial")
                self.assertEqual(green["completeness"], "partial")
                self.assertEqual(green["reason_code"], "sample_partial")
                self.assertEqual(green["counts"]["merged_prs"], 5)
                self.assertEqual(green["counts"]["sampled_prs"], 2)
                self.assertEqual(green["counts"]["conclusive_prs"], 2)
                for name, metric in direct["metrics"].items():
                    self.assertIn("contract", metric)
                    self.assertEqual(metric["contract"]["id"], name)
                    self.assertEqual(metric["contract"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
