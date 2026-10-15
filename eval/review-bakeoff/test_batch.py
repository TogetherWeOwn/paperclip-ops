#!/usr/bin/env python3
"""Tests for batch.py with a stub runner (no model is called). Run: python3 -m unittest -v test_batch.py"""
import json
import os
import tempfile
import threading
import unittest

import batch

CASES = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]
OK = {"score": 4, "summary": "", "findings": [], "coverage": {"files_reviewed": [], "complete": True, "limitations": ""}}


def good(repo, case, model, effort, key_env, timeout):
    return {"case_id": case["id"], "model": model, "effort": effort, "latency_s": 1.0, "usage": {},
            "review": OK, "parse_error": None}


class Batch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = os.path.join(self._tmp.name, "r")

    def go(self, runner, models=("m1", "m2"), cases=CASES, **kw):
        return batch.run("repo", cases, list(models), "medium", 1, "K", self.out, kw.get("concurrency", 2), 5, runner)

    def test_every_model_case_pair_gets_a_result_file(self):
        summary = self.go(good)
        self.assertEqual((summary["planned"], summary["done"], summary["failed"]), (6, 6, 0))
        self.assertEqual(len(os.listdir(self.out)), 6)

    def test_existing_results_are_skipped_on_restart(self):
        self.go(good)
        calls = []
        def counting(*a):
            calls.append(a[1]["id"]); return good(*a)
        os.remove(batch.result_path(self.out, "c2", "m1", "medium", 1))
        summary = self.go(counting)
        self.assertEqual((summary["planned"], calls), (1, ["c2"]))

    def test_a_crash_is_recorded_as_a_failed_review_not_dropped(self):
        def boom(*a):
            raise RuntimeError("worktree add failed")
        summary = self.go(boom, models=("m1",), cases=CASES[:1])
        self.assertEqual((summary["done"], summary["failed"]), (1, 1))
        res = json.load(open(batch.result_path(self.out, "c1", "m1", "medium", 1)))
        self.assertIsNone(res["review"])
        self.assertEqual(res["parse_error"], "harness error")

    def test_a_failed_parse_counts_as_failed(self):
        def bad(repo, case, model, effort, key_env, timeout):
            r = good(repo, case, model, effort, key_env, timeout); r["review"] = None; return r
        self.assertEqual(self.go(bad, models=("m1",))["failed"], 3)

    def test_reps_are_separate_files(self):
        self.assertNotEqual(batch.result_path("o", "c", "m", "medium", 1), batch.result_path("o", "c", "m", "medium", 2))

    def test_model_ids_with_a_slash_are_safe_in_filenames(self):
        self.assertNotIn("/", os.path.basename(batch.result_path("o", "c", "zai/glm-5.3", "medium", 1)))

    def test_concurrency_is_bounded_per_model(self):
        live, peak, lock = 0, 0, threading.Lock()
        def tracking(*a):
            nonlocal live, peak
            with lock:
                live += 1; peak = max(peak, live)
            import time; time.sleep(0.05)
            with lock:
                live -= 1
            return good(*a)
        self.go(tracking, models=("m1",), cases=[{"id": f"c{i}"} for i in range(8)], concurrency=2)
        self.assertLessEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
