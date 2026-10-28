#!/usr/bin/env python3
"""Hermetic reproducibility contracts; all cases and outputs are synthetic."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import replay
import score
from test_score import CASE, finding, result


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.results = self.source / "results"
        self.results.mkdir()
        self.cases = {"cases": [copy.deepcopy(CASE)]}
        self.write("cases.json", self.cases)
        for name in ("prompt.md", "harness.py", "schema.json"):
            (self.source / name).write_text("synthetic " + name + "\n")
        self.write("results/ok.json", dict(result([finding("src/a.ts", 100)]), effort="medium", rep=1))
        self.write("results/failed.json", dict(result([]), review=None, effort="medium", rep=2))
        self.config = {
            "schema": "review-bakeoff-config/v1",
            "source": {"revision": "synthetic-v1", "provenance": "synthetic fixture"},
            "cutoff": "2026-01-02T00:00:00Z",
            "artifacts": {"cases": "cases.json", "prompt": "prompt.md",
                          "harness": "harness.py", "output_schema": "schema.json"},
            "result_dirs": ["results"],
            "diagnostic_result_dirs": [],
            "case_metadata": {"c1": {"observed_at": "2026-01-02T00:00:00Z",
                                      "date_source": "synthetic event", "exposure": "unexposed",
                                      "related_group": "g1", "group_source": "synthetic provenance"}},
        }
        self.config_path = self.source / "config.json"
        self.out = self.root / "bundle"

    def write(self, name, data):
        (self.source / name).write_text(json.dumps(data, sort_keys=True) + "\n")

    def build(self, output=None):
        self.write("config.json", self.config)
        return replay.bundle(self.config_path, output or self.out)

    def assert_error(self, code, function, *args):
        with self.assertRaises(replay.ContractError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_bundle_is_deterministic_portable_and_idempotent(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.run", side_effect=AssertionError("runner forbidden")):
            report = self.build()
            self.assertEqual(report, replay.verify(self.out))
            self.assertEqual(report, self.build())
            other = self.root / "other"
            self.build(other)
        first = {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        second = {str(p.relative_to(other)): p.read_bytes() for p in other.rglob("*") if p.is_file()}
        self.assertEqual(first, second)
        proc = subprocess.run([sys.executable, str(self.out / "tool/replay.py"), "verify", "--bundle", str(self.out)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), report)

    def test_failed_runs_retain_denominators_and_missing_generation_evidence_is_explicit(self):
        report = self.build()
        self.assertEqual(report["coverage"], {"eligible": 2, "replayed": 2, "failed": 1, "diagnostic": 0})
        self.assertEqual(report["inference_calls"], 0)
        expected = score.render(score.summarize({"c1": CASE}, score.load_results(str(self.results)), {})) + "\n"
        self.assertEqual((self.out / "scores.md").read_text(), expected)
        manifest = json.loads((self.out / "manifest.json").read_text())
        for record in manifest["results"]:
            self.assertEqual(record["generation_reproducibility"], "unknown")
            self.assertIn("rendered_prompt", record["missing_generation_evidence"])
            self.assertIn(record["sidecar"], manifest["files"])
        self.assertEqual(report["holdout"]["c1"]["partition"], "holdout")
        self.assertEqual(report["holdout"]["c1"]["status"], "clean")

    def test_each_bound_artifact_tamper_is_refused(self):
        self.build()
        for name in ("inputs/cases.json", "inputs/prompt.md", "inputs/harness.py", "inputs/output_schema.json",
                     "inputs/config.json", "results/ok.json", "tool/score.py", "tool/replay.py", "scores.md",
                     "report.json", "contracts/ok.manifest.json"):
            with self.subTest(name=name):
                path = self.out / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assert_error("hash_mismatch", replay.verify, self.out)
                path.write_bytes(original)

    def test_hidden_and_underscore_sidecars_match_legacy_selection(self):
        self.write("results/_sidecar.json", {"schema": "not-a-result"})
        self.write("results/.hidden.json", {"schema": "not-a-result"})
        self.assertEqual(len(score.load_results(str(self.results))), 2)
        self.assertEqual(self.build()["coverage"]["eligible"], 2)

    def test_manifest_semantics_are_recomputed_not_trusted(self):
        self.build()
        path = self.out / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["results"][0]["generation_reproducibility"] = "complete"
        path.write_bytes(replay.canonical(manifest))
        self.assert_error("manifest_mismatch", replay.verify, self.out)

    def test_inventory_rejects_missing_and_extra_files(self):
        self.build()
        path = self.out / "results/ok.json"
        original = path.read_bytes()
        path.unlink()
        self.assert_error("inventory_mismatch", replay.verify, self.out)
        path.write_bytes(original)
        (self.out / "results/extra.json").write_bytes(original)
        self.assert_error("inventory_mismatch", replay.verify, self.out)

    def test_duplicate_result_identities_and_unknown_cases_are_refused(self):
        good = json.loads((self.results / "ok.json").read_text())
        self.write("results/copy.json", good)
        self.assert_error("duplicate_result", self.build)
        (self.results / "copy.json").unlink()
        good["case_id"] = "missing"
        self.write("results/ok.json", good)
        self.assert_error("unknown_case", self.build)

    def test_partial_labels_duplicate_cases_and_bad_results_are_refused(self):
        bad = copy.deepcopy(self.cases)
        del bad["cases"][0]["defects"][0]["severity"]
        self.write("cases.json", bad)
        self.assert_error("invalid_case", self.build)
        self.write("cases.json", {"cases": [CASE, CASE]})
        self.assert_error("duplicate_case", self.build)
        self.write("cases.json", self.cases)
        bad_result = result([])
        bad_result["latency_s"] = float("nan")
        self.write("results/ok.json", bad_result)
        self.assert_error("invalid_json", self.build)

    def test_diagnostic_retries_are_bound_but_not_primary_denominators(self):
        (self.source / "diagnostic").mkdir()
        self.write("diagnostic/retry.json", dict(result([]), effort="high", rep=1))
        self.config["diagnostic_result_dirs"] = ["diagnostic"]
        report = self.build()
        self.assertEqual(report["coverage"], {"eligible": 2, "replayed": 2, "failed": 1, "diagnostic": 1})
        self.assertTrue((self.out / "diagnostic/retry.json").is_file())

    def test_unknown_dates_and_prior_exposure_never_make_clean_holdout(self):
        for update, status in [({"observed_at": None}, "unknown"), ({"date_source": ""}, "unknown"),
                               ({"exposure": "unknown"}, "unknown"), ({"exposure": "exposed"}, "contaminated"),
                               ({"group_source": ""}, "unknown")]:
            with self.subTest(update=update):
                original = copy.deepcopy(self.config["case_metadata"])
                self.config["case_metadata"]["c1"].update(update)
                report = self.build(self.root / ("variant-" + str(len(list(self.root.glob('variant-*'))))))
                self.assertEqual(report["holdout"]["c1"]["status"], status)
                self.assertNotEqual(report["holdout"]["c1"]["partition"], "holdout")
                self.config["case_metadata"] = original

    def test_related_and_exact_duplicate_cases_cannot_cross_cutoff(self):
        for declared_group in ("g1", "different"):
            with self.subTest(declared_group=declared_group):
                duplicate = copy.deepcopy(CASE)
                duplicate["id"] = "c2"
                self.write("cases.json", {"cases": [CASE, duplicate]})
                self.config["case_metadata"]["c2"] = dict(self.config["case_metadata"]["c1"],
                    observed_at="2026-01-01T23:59:59Z", related_group=declared_group)
                report = self.build(self.root / declared_group)
                for value in report["holdout"].values():
                    self.assertEqual(value["status"], "contaminated")
                    self.assertEqual(value["partition"], "quarantine")

    def test_duplicate_labels_cannot_escape_grouping_by_changing_description(self):
        first = dict(copy.deepcopy(CASE), description="first wording")
        second = dict(copy.deepcopy(CASE), id="c2", description="changed wording")
        self.write("cases.json", {"cases": [first, second]})
        self.config["case_metadata"]["c2"] = dict(self.config["case_metadata"]["c1"],
            observed_at="2026-01-01T23:59:59Z", related_group="other")
        report = self.build()
        self.assertTrue(all(v["partition"] == "quarantine" for v in report["holdout"].values()))

    def test_same_description_does_not_merge_distinct_labelled_tasks(self):
        first = dict(copy.deepcopy(CASE), description="shared metadata")
        second = dict(copy.deepcopy(CASE), id="c2", description="shared metadata")
        for defect in second["defects"]:
            defect["file"] = "different/" + defect["file"]
        self.write("cases.json", {"cases": [first, second]})
        self.config["case_metadata"]["c2"] = dict(self.config["case_metadata"]["c1"],
            observed_at="2026-01-01T23:59:59Z", related_group="other")
        report = self.build()
        self.assertEqual(report["holdout"]["c1"]["partition"], "holdout")
        self.assertEqual(report["holdout"]["c2"]["partition"], "train")

    def test_empty_slice_invalid_cutoff_and_paths_fail_closed(self):
        self.config["result_dirs"] = []
        self.assert_error("empty_results", self.build)
        self.config["result_dirs"] = ["results"]
        self.config["cutoff"] = "2026-01-02"
        self.assert_error("invalid_config", self.build)
        self.config["cutoff"] = "2026-01-02T00:00:00Z"
        self.config["artifacts"]["prompt"] = "../outside"
        self.assert_error("unsafe_path", self.build)

    def test_different_existing_bundle_is_never_overwritten(self):
        self.build()
        path = self.out / "scores.md"
        path.write_text("preserve evidence\n")
        self.assert_error("output_conflict", self.build)
        self.assertEqual(path.read_text(), "preserve evidence\n")

    def test_legacy_loader_does_not_read_sidecars(self):
        self.build()
        self.assertEqual(len(score.load_results(str(self.out / "results"))), 2)

    def test_judgments_are_bound_and_unknown_finding_references_refuse(self):
        self.write("judgments.json", {"ok#0": "d1"})
        self.config["artifacts"]["judgments"] = "judgments.json"
        self.build()
        path = self.out / "inputs/judgments.json"
        path.write_text('{}\n')
        self.assert_error("hash_mismatch", replay.verify, self.out)
        self.write("judgments.json", {"unknown#0": "invalid"})
        self.assert_error("invalid_judgment", self.build, self.root / "invalid-judgment")

    def test_before_cutoff_is_train_and_timezone_equality_is_holdout(self):
        meta = self.config["case_metadata"]["c1"]
        meta["observed_at"] = "2026-01-01T23:59:59+00:00"
        self.assertEqual(self.build()["holdout"]["c1"]["partition"], "train")
        meta["observed_at"] = "2026-01-02T02:00:00+02:00"
        self.assertEqual(self.build(self.root / "equal")["holdout"]["c1"]["partition"], "holdout")

    def test_symlink_inputs_and_existing_output_are_refused(self):
        (self.source / "linked.md").symlink_to(self.source / "prompt.md")
        self.config["artifacts"]["prompt"] = "linked.md"
        self.assert_error("unsafe_path", self.build)
        self.config["artifacts"]["prompt"] = "prompt.md"
        self.out.symlink_to(self.results, target_is_directory=True)
        self.assert_error("unsafe_path", self.build)
        self.assertEqual(len(list(self.results.glob('*.json'))), 2)

    def test_malformed_unhashable_severity_and_overflow_are_typed_errors(self):
        malformed = copy.deepcopy(self.cases)
        malformed["cases"][0]["defects"][0]["severity"] = []
        self.write("cases.json", malformed)
        self.assert_error("invalid_case", self.build)
        self.write("cases.json", self.cases)
        malformed_result = result([finding("src/a.ts", 100)])
        malformed_result["review"]["findings"][0]["severity"] = []
        self.write("results/ok.json", malformed_result)
        self.assert_error("invalid_result", self.build)
        (self.results / "ok.json").write_text('{"latency_s": 1e999}')
        self.assert_error("invalid_json", self.build)
        overflow = result([])
        overflow["latency_s"] = 10 ** 400
        self.write("results/ok.json", overflow)
        self.assert_error("invalid_result", self.build)
        self.write("results/ok.json", dict(result([]), usage={"input_tokens": 10 ** 400}))
        self.assert_error("invalid_result", self.build)
        self.config["artifacts"]["prompt"] = "bad" + chr(0) + "path"
        self.assert_error("unsafe_path", self.build)

    def test_concurrent_same_input_writers_are_idempotent(self):
        script = str(Path(replay.__file__))
        self.write("config.json", self.config)
        first = subprocess.Popen([sys.executable, script, "bundle", "--config", str(self.config_path),
                                  "--output", str(self.out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen([sys.executable, script, "bundle", "--config", str(self.config_path),
                                   "--output", str(self.out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout)["coverage"]["eligible"], 2)
        self.assertEqual(replay.verify(self.out)["coverage"]["replayed"], 2)

    def test_concurrent_different_input_writers_never_replace_evidence(self):
        script = str(Path(replay.__file__))
        self.write("config.json", self.config)
        other = copy.deepcopy(self.config)
        other["source"]["revision"] = "synthetic-v2"
        self.write("other-config.json", other)
        processes = [subprocess.Popen([sys.executable, script, "bundle", "--config", str(path),
                                       "--output", str(self.out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for path in (self.config_path, self.source / "other-config.json")]
        codes = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            codes.append(process.returncode)
            if process.returncode:
                self.assertEqual(json.loads(stderr)["error"], "output_conflict")
            else:
                self.assertEqual(json.loads(stdout)["coverage"]["eligible"], 2)
        self.assertEqual(sorted(codes), [0, 2])
        self.assertEqual(replay.verify(self.out)["coverage"]["replayed"], 2)

    def test_cli_errors_are_json_and_nonzero(self):
        proc = subprocess.run([sys.executable, str(Path(replay.__file__)), "verify", "--bundle", str(self.out)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr)["error"], "missing_artifact")


if __name__ == "__main__":
    unittest.main()
