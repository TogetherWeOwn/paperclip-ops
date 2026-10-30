#!/usr/bin/env python3
"""Offline acceptance tests for PR #133 finding: --rep must reject 0 and -1.

Both real CLI mains (run_review.main and batch.main) must reject --rep 0 and
--rep -1 with exit 2 BEFORE any runner scheduling/invocation and before any
result-file write. rep 1 (explicit) and the default rep (omitted flag) are
positive controls: they must produce real JSON output accepted by the real
scorer (score.summarize / hygiene.validate_results) on a synthetic
case/defect.

Hermetic: the inference runner is the ONLY thing replaced (a counting offline
stub). No provider, key, subprocess, or network operation is invoked. batch.run
has its default runner bound at definition time, so batch.main tests wrap the
real batch.run to inject the offline runner while keeping the real scheduling
and result-file writes under test.

Run (RED before the fix): python3 -m unittest -v test_rep_cli
Also discovered by the canonical wrapper via:
  python3 -m unittest discover -s eval/review-bakeoff -p 'test_*.py'
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import batch
import hygiene
import run_review
import score

CASE_ID = "syn-1"
MODEL = "m-syn"
EFFORT = "medium"

REVIEW = {
    "score": 3,
    "summary": "synthetic offline review",
    "findings": [
        {
            "file": "a.ts",
            "line": 11,
            "severity": "major",
            "title": "synthetic defect",
            "rationale": "offline fixture",
            "evidence": "needle",
        }
    ],
    "coverage": {"files_reviewed": ["a.ts"], "complete": True, "limitations": ""},
}


def _write_cases(tmpdir: str) -> str:
    cases = {
        "cases": [
            {
                "id": CASE_ID,
                "defects": [
                    {
                        "id": "d1",
                        "file": "a.ts",
                        "line_start": 10,
                        "line_end": 12,
                        "severity": "major",
                    }
                ],
            }
        ]
    }
    path = os.path.join(tmpdir, "cases.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cases, fh)
    return path


def _offline_result(case_id: str, model: str, effort: str) -> dict:
    return {
        "case_id": case_id,
        "model": model,
        "effort": effort,
        "input_sha256": "ab" * 32,
        "isolation": "offline-fixture",
        "latency_s": 0.1,
        "exit_code": 0,
        "timed_out": False,
        "usage": {"input_tokens": 5, "output_tokens": 7},
        "errors": [],
        "prompt_chars": 10,
        "review": json.loads(json.dumps(REVIEW)),
        "parse_error": None,
        "evidence_gate": None,
        "raw_final": None,
    }


def _call_main(func, argv: list[str]):
    """Run a CLI main with redirected output; normalize return/SystemExit to a code."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = func(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def _result_files(out_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(out_dir, "*.json"))) if os.path.isdir(out_dir) else []


def _assert_scorable(test: unittest.TestCase, cases_path: str, out_dir: str,
                     expected_files: int, expected_rep: int) -> None:
    paths = _result_files(out_dir)
    test.assertEqual(len(paths), expected_files, paths)
    with open(cases_path, encoding="utf-8") as fh:
        cases = {c["id"]: c for c in json.load(fh)["cases"]}
    results = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            results.append((os.path.splitext(os.path.basename(path))[0], json.load(fh)))
    for key, res in results:
        test.assertEqual(res.get("rep"), expected_rep, (key, res.get("rep")))
    # Real scorer acceptance: validation must not raise and the synthetic
    # defect must be found.
    hygiene.validate_results(cases, results)
    summary = score.summarize(cases, results, {})
    test.assertIn(MODEL, summary)
    test.assertGreaterEqual(int(summary[MODEL]["found"]), 1, summary[MODEL])
    key, res = results[0]
    scored = score.score_result(res, cases[CASE_ID], {}, key)
    test.assertEqual(scored["found"], {"d1"}, scored)


class RunReviewRepValidation(unittest.TestCase):
    def test_invalid_reps_rejected_before_runner_or_write(self):
        for rep in ("0", "-1"):
            with self.subTest(rep=rep):
                with tempfile.TemporaryDirectory() as tmp:
                    cases_path = _write_cases(tmp)
                    out_dir = os.path.join(tmp, "out")
                    calls: list = []

                    def offline(*args, **kwargs):
                        calls.append((args, kwargs))
                        return _offline_result(CASE_ID, MODEL, EFFORT)

                    with mock.patch.object(run_review, "run_case", side_effect=offline):
                        rc, _, _ = _call_main(run_review.main, [
                            "--repo", "dummy-repo",
                            "--case", cases_path,
                            "--case-id", CASE_ID,
                            "--model", MODEL,
                            "--effort", EFFORT,
                            "--rep", rep,
                            "--key-env", "BAKEOFF_FIXTURE_KEY",
                            "--out", out_dir,
                        ])
                    self.assertEqual(rc, 2, f"--rep {rep} must exit 2")
                    self.assertEqual(calls, [], "runner must not be invoked")
                    self.assertEqual(_result_files(out_dir), [], "no result JSON may be written")
                    self.assertFalse(os.path.exists(out_dir),
                                     "out dir must not be created for invalid rep")

    def test_rep_one_writes_scorable_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = _write_cases(tmp)
            out_dir = os.path.join(tmp, "out")

            def offline(repo, case, model, effort, key_env, timeout):
                return _offline_result(case["id"], model, effort)

            with mock.patch.object(run_review, "run_case", side_effect=offline):
                rc, _, _ = _call_main(run_review.main, [
                    "--repo", "dummy-repo",
                    "--case", cases_path,
                    "--case-id", CASE_ID,
                    "--model", MODEL,
                    "--effort", EFFORT,
                    "--rep", "1",
                    "--key-env", "BAKEOFF_FIXTURE_KEY",
                    "--out", out_dir,
                ])
            self.assertEqual(rc, 0, "rep 1 control must succeed")
            _assert_scorable(self, cases_path, out_dir, 1, 1)

    def test_default_rep_writes_scorable_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = _write_cases(tmp)
            out_dir = os.path.join(tmp, "out")

            def offline(repo, case, model, effort, key_env, timeout):
                return _offline_result(case["id"], model, effort)

            with mock.patch.object(run_review, "run_case", side_effect=offline):
                rc, _, _ = _call_main(run_review.main, [
                    "--repo", "dummy-repo",
                    "--case", cases_path,
                    "--case-id", CASE_ID,
                    "--model", MODEL,
                    "--effort", EFFORT,
                    "--key-env", "BAKEOFF_FIXTURE_KEY",
                    "--out", out_dir,
                ])
            self.assertEqual(rc, 0, "default-rep control must succeed")
            _assert_scorable(self, cases_path, out_dir, 1, 1)


class BatchRepValidation(unittest.TestCase):
    def _batch_main_offline(self, argv: list[str], calls: list):
        """Call the REAL batch.main while injecting an offline runner.

        batch.run's default runner was bound at def time to the real
        run_review.run_case, so patching run_review.run_case alone is not
        enough: wrap the real batch.run so scheduling/writes still execute.
        """
        real_run = batch.run

        def offline(repo, case, model, effort, key_env, timeout):
            calls.append((case["id"] if isinstance(case, dict) else case, model))
            return _offline_result(
                case["id"] if isinstance(case, dict) else case, model, effort)

        def wrapper(*args, **kwargs):
            kwargs["runner"] = offline
            return real_run(*args, **kwargs)

        with mock.patch.object(batch, "run", wrapper):
            return _call_main(batch.main, argv)

    def _batch_argv(self, cases_path: str, out_dir: str, rep: str | None) -> list[str]:
        argv = ["--repo", "dummy-repo",
                "--cases", cases_path,
                "--models", MODEL,
                "--effort", EFFORT,
                "--key-env", "K",
                "--out", out_dir,
                "--concurrency", "1",
                "--timeout", "5"]
        if rep is not None:
            argv += ["--rep", rep]
        return argv

    def test_invalid_reps_rejected_before_runner_or_write(self):
        for rep in ("0", "-1"):
            with self.subTest(rep=rep):
                with tempfile.TemporaryDirectory() as tmp:
                    cases_path = _write_cases(tmp)
                    out_dir = os.path.join(tmp, "out")
                    calls: list = []
                    rc, _, _ = self._batch_main_offline(
                        self._batch_argv(cases_path, out_dir, rep), calls)
                    self.assertEqual(rc, 2, f"--rep {rep} must exit 2")
                    self.assertEqual(calls, [], "runner must not be invoked")
                    self.assertEqual(_result_files(out_dir), [], "no result JSON may be written")
                    self.assertFalse(os.path.exists(out_dir),
                                     "out dir must not be created for invalid rep")

    def test_rep_one_writes_scorable_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = _write_cases(tmp)
            out_dir = os.path.join(tmp, "out")
            calls: list = []
            rc, _, _ = self._batch_main_offline(
                self._batch_argv(cases_path, out_dir, "1"), calls)
            self.assertEqual(rc, 0, "rep 1 control must succeed")
            self.assertEqual(len(calls), 1, calls)
            _assert_scorable(self, cases_path, out_dir, 1, 1)

    def test_default_rep_writes_scorable_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = _write_cases(tmp)
            out_dir = os.path.join(tmp, "out")
            calls: list = []
            rc, _, _ = self._batch_main_offline(
                self._batch_argv(cases_path, out_dir, None), calls)
            self.assertEqual(rc, 0, "default-rep control must succeed")
            self.assertEqual(len(calls), 1, calls)
            _assert_scorable(self, cases_path, out_dir, 1, 1)


if __name__ == "__main__":
    unittest.main()
