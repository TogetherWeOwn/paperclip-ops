#!/usr/bin/env python3
"""Acceptance tests for eval hygiene + isolation contract (RED first).

Covers ONLY the planned contract (see task brief):
  hygiene.validate_results, hygiene.repeat_spread, hygiene.render_comparisons,
  score.summarize/back-compat + score.render columns + score.main validation,
  isolation.confine + isolation.py CLI, run_case isolation/input_sha256/env/prompt.

Stdlib unittest only. Run: python3 -m unittest -v test_hygiene
"""
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import batch
import run_review
import score


def _load(name):
    try:
        __import__(name)
        return sys.modules[name]
    except ImportError as exc:
        raise AssertionError(f"planned module {name!r} not importable (RED): {exc}")


def hygiene():
    return _load("hygiene")


def isolation_mod():
    return _load("isolation")


CASE_DEFS = {
    "c1": {"id": "c1", "defects": [
        {"id": "d1", "file": "src/a.ts", "line_start": 100, "line_end": 110, "severity": "major"},
        {"id": "d2", "file": "src/b.ts", "line_start": 5, "line_end": 5, "severity": "critical"},
    ]},
    "c2": {"id": "c2", "defects": [
        {"id": "d1", "file": "src/a.ts", "line_start": 100, "line_end": 110, "severity": "major"},
        {"id": "d2", "file": "src/b.ts", "line_start": 5, "line_end": 5, "severity": "critical"},
    ]},
}

SHA_A = hashlib.sha256(b"prompt-a").hexdigest()
SHA_B = hashlib.sha256(b"prompt-b").hexdigest()


def finding(file, line, severity="major"):
    return {"file": file, "line": line, "severity": severity, "title": "t", "rationale": "r"}


def mk_review(findings, sc=3):
    return {"score": sc, "summary": "", "findings": findings,
            "coverage": {"files_reviewed": [], "complete": True, "limitations": ""}}


_DEFAULT_REVIEW = object()


def mk_result(case_id="c1", model="m", effort="medium", rep=1, sha=None,
              review=_DEFAULT_REVIEW, latency=1.0):
    # NOTE: review=None is a real failed run (review null) and must be kept
    # as-is; only the omitted default builds a valid review.
    if review is _DEFAULT_REVIEW:
        review = mk_review([finding("src/a.ts", 100)])
    d = {"case_id": case_id, "model": model, "effort": effort, "rep": rep,
         "latency_s": latency, "usage": {}, "review": review}
    if sha is not None:
        d["input_sha256"] = sha
    return d


def as_results(pairs):
    # pairs: list of (key, result-dict)
    return [(k, copy.deepcopy(v)) for k, v in pairs]


def repeats_of(entry):
    r = entry.get("repeats")
    if isinstance(r, int):
        return r
    if isinstance(r, (list, tuple, set, frozenset)):
        return len(r)
    return r


class ValidateResults(unittest.TestCase):
    def test_valid_single_effort_passes(self):
        h = hygiene()
        res = as_results([("a", mk_result()), ("b", mk_result(rep=2))])
        h.validate_results(CASE_DEFS, res)  # must not raise

    def test_all_five_efforts_accepted_individually(self):
        h = hygiene()
        for eff in ("low", "medium", "high", "xhigh", "max"):
            res = as_results([("k", mk_result(effort=eff))])
            h.validate_results({"c1": CASE_DEFS["c1"]}, res)

    def test_missing_effort_raises(self):
        h = hygiene()
        r = mk_result()
        del r["effort"]
        with self.assertRaises(ValueError):
            h.validate_results(CASE_DEFS, as_results([("k", r)]))

    def test_invalid_effort_raises(self):
        h = hygiene()
        for bad in (None, "", "MEDIUM", "ultra", "med", 123, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    h.validate_results(CASE_DEFS, as_results([("k", mk_result(effort=bad))]))

    def test_mixed_effort_anywhere_raises(self):
        h = hygiene()
        res = as_results([("a", mk_result(effort="low")), ("b", mk_result(effort="high"))])
        with self.assertRaises(ValueError):
            h.validate_results(CASE_DEFS, res)

    def test_effort_mismatch_rejected_independent_of_file_names(self):
        # Keys (file stems) suggest same effort, payload differs -> must still raise.
        h = hygiene()
        res = as_results([
            ("c1__m__medium__r1", mk_result(effort="low")),
            ("c1__m__medium__r2", mk_result(effort="high", rep=2)),
        ])
        with self.assertRaises(ValueError):
            h.validate_results(CASE_DEFS, res)
        # Keys suggest different effort, payload same -> must pass.
        res2 = as_results([
            ("c1__m__low__r1", mk_result(effort="medium")),
            ("c1__m__high__r2", mk_result(effort="medium", rep=2)),
        ])
        h.validate_results(CASE_DEFS, res2)

    def test_missing_rep_raises(self):
        h = hygiene()
        r = mk_result()
        del r["rep"]
        with self.assertRaises(ValueError):
            h.validate_results(CASE_DEFS, as_results([("k", r)]))

    def test_invalid_rep_raises_bool_is_invalid(self):
        h = hygiene()
        for bad in (0, -1, 1.0, "1", None, True, False, 2.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    h.validate_results(CASE_DEFS, as_results([("k", mk_result(rep=bad))]))

    def test_duplicate_model_case_rep_raises(self):
        h = hygiene()
        res = as_results([("a", mk_result(rep=1)), ("b", mk_result(rep=1))])
        with self.assertRaises(ValueError):
            h.validate_results(CASE_DEFS, res)

    def test_same_rep_different_model_or_case_ok(self):
        h = hygiene()
        res = as_results([
            ("a", mk_result(model="m1", case_id="c1", rep=1)),
            ("b", mk_result(model="m2", case_id="c1", rep=1)),
            ("c", mk_result(model="m1", case_id="c2", rep=1)),
        ])
        h.validate_results(CASE_DEFS, res)

    def test_unknown_case_ids_ignored(self):
        h = hygiene()
        stray = mk_result(case_id="nope")
        del stray["effort"]
        del stray["rep"]
        h.validate_results(CASE_DEFS, as_results([("k", stray)]))


class RepeatSpread(unittest.TestCase):
    def _balanced(self, recall_a=(True, True), recall_b=(True, True), sha=SHA_A):
        # recall True -> finds d1+d2 (recall 1.0); False -> finds nothing (0.0).
        # Uses single case c1 across 2 models x 2 reps with identical sha.
        h = hygiene()
        _ = h
        res = []
        for model, recs in (("A", recall_a), ("B", recall_b)):
            for i, hit in enumerate(recs, 1):
                rev = mk_review([finding("src/a.ts", 100), finding("src/b.ts", 5)]) if hit \
                    else mk_review([])
                res.append((f"{model}-r{i}", mk_result(case_id="c1", model=model, rep=i,
                                                       sha=sha, review=rev)))
        return res

    def test_keys_and_shape(self):
        h = hygiene()
        spreads = h.repeat_spread(CASE_DEFS, self._balanced(), {})
        self.assertIn("A", spreads)
        self.assertIn("B", spreads)
        for model, entry in spreads.items():
            for k in ("effort", "repeats", "recall_spread", "reason",
                      "recall_mean"):
                self.assertIn(k, entry, f"{model} missing {k}")
            self.assertIsInstance(entry["reason"], str)
            self.assertTrue(entry["reason"], "reason must be non-empty")

    def test_zero_spread_when_identical(self):
        h = hygiene()
        spreads = h.repeat_spread(CASE_DEFS, self._balanced((True, True), (True, True)), {})
        for m in ("A", "B"):
            self.assertEqual(spreads[m]["recall_spread"], 0.0)
            self.assertEqual(spreads[m]["recall_mean"], 1.0)

    def test_spread_and_mean_from_per_rep_recall(self):
        h = hygiene()
        # A: reps recall 1.0 and 0.0 -> spread 1.0 mean 0.5
        res = self._balanced(recall_a=(True, False), recall_b=(True, True))
        spreads = h.repeat_spread(CASE_DEFS, res, {})
        self.assertAlmostEqual(spreads["A"]["recall_spread"], 1.0)
        self.assertAlmostEqual(spreads["A"]["recall_mean"], 0.5)
        self.assertEqual(repeats_of(spreads["A"]), 2)

    def test_failed_runs_count_as_zero_recall(self):
        h = hygiene()
        res = []
        res.append(("A-r1", mk_result(case_id="c1", model="A", rep=1, sha=SHA_A,
                                      review=mk_review([finding("src/a.ts", 100),
                                                        finding("src/b.ts", 5)]))))
        bad = mk_result(case_id="c1", model="A", rep=2, sha=SHA_A, review=None)
        bad["review"] = None  # explicit: must be a genuine failed run, not a default review
        self.assertIsNone(bad["review"])
        sanity = score.score_result(bad, CASE_DEFS["c1"], {}, "A-r2")
        self.assertFalse(sanity["ok"], "fixture must simulate a failed run (ok False)")
        self.assertEqual(sanity["found"], set())
        res.append(("A-r2", bad))
        res.append(("B-r1", mk_result(case_id="c1", model="B", rep=1, sha=SHA_A,
                                      review=mk_review([finding("src/a.ts", 100),
                                                        finding("src/b.ts", 5)]))))
        res.append(("B-r2", mk_result(case_id="c1", model="B", rep=2, sha=SHA_A,
                                      review=mk_review([finding("src/a.ts", 100),
                                                        finding("src/b.ts", 5)]))))
        spreads = h.repeat_spread(CASE_DEFS, as_results(res), {})
        self.assertAlmostEqual(spreads["A"]["recall_mean"], 0.5)
        self.assertAlmostEqual(spreads["A"]["recall_spread"], 1.0)

    def test_single_rep_yields_none_with_reason(self):
        h = hygiene()
        res = as_results([("A-r1", mk_result(model="A", rep=1, sha=SHA_A)),
                          ("B-r1", mk_result(model="B", rep=1, sha=SHA_A))])
        spreads = h.repeat_spread(CASE_DEFS, res, {})
        for m in ("A", "B"):
            self.assertIsNone(spreads[m]["recall_spread"])
            self.assertIsNone(spreads[m]["recall_mean"])
            self.assertIn("rep", spreads[m]["reason"].lower())

    def test_mismatched_rep_ids_yields_none(self):
        h = hygiene()
        res = as_results([
            ("a1", mk_result(model="A", rep=1, sha=SHA_A)),
            ("a2", mk_result(model="A", rep=2, sha=SHA_A)),
            ("b1", mk_result(model="B", rep=1, sha=SHA_A)),
            ("b3", mk_result(model="B", rep=3, sha=SHA_A)),
        ])
        spreads = h.repeat_spread(CASE_DEFS, res, {})
        for m in ("A", "B"):
            self.assertIsNone(spreads[m]["recall_spread"])
            self.assertIsNone(spreads[m]["recall_mean"])
            self.assertTrue(spreads[m]["reason"])

    def test_unbalanced_case_sets_yield_none(self):
        h = hygiene()
        cases = CASE_DEFS
        res = as_results([
            ("a1c1", mk_result(case_id="c1", model="A", rep=1, sha=SHA_A)),
            ("a1c2", mk_result(case_id="c2", model="A", rep=1, sha=SHA_B)),
            ("a2c1", mk_result(case_id="c1", model="A", rep=2, sha=SHA_A)),
            # A rep2 missing c2 entirely
            ("b1c1", mk_result(case_id="c1", model="B", rep=1, sha=SHA_A)),
            ("b1c2", mk_result(case_id="c2", model="B", rep=1, sha=SHA_B)),
            ("b2c1", mk_result(case_id="c1", model="B", rep=2, sha=SHA_A)),
            ("b2c2", mk_result(case_id="c2", model="B", rep=2, sha=SHA_B)),
        ])
        spreads = h.repeat_spread(cases, res, {})
        for m in ("A", "B"):
            self.assertIsNone(spreads[m]["recall_spread"])
            self.assertIsNone(spreads[m]["recall_mean"])
            self.assertTrue(spreads[m]["reason"])

    def test_missing_hash_yields_none(self):
        h = hygiene()
        r = mk_result(model="A", rep=1)  # no sha key
        if "input_sha256" in r:
            del r["input_sha256"]
        res = as_results([
            ("a1", r),
            ("a2", mk_result(model="A", rep=2, sha=SHA_A)),
            ("b1", mk_result(model="B", rep=1, sha=SHA_A)),
            ("b2", mk_result(model="B", rep=2, sha=SHA_A)),
        ])
        spreads = h.repeat_spread(CASE_DEFS, res, {})
        for m in ("A", "B"):
            self.assertIsNone(spreads[m]["recall_spread"])
            self.assertIsNone(spreads[m]["recall_mean"])
            self.assertIn("sha", spreads[m]["reason"].lower() + spreads[m]["reason"].lower().replace("hash", "sha"))

    def test_mismatched_hash_yields_none(self):
        h = hygiene()
        res = as_results([
            ("a1", mk_result(model="A", rep=1, sha=SHA_A)),
            ("a2", mk_result(model="A", rep=2, sha=SHA_A)),
            ("b1", mk_result(model="B", rep=1, sha=SHA_A)),
            ("b2", mk_result(model="B", rep=2, sha=SHA_B)),  # differs
        ])
        spreads = h.repeat_spread(CASE_DEFS, res, {})
        for m in ("A", "B"):
            self.assertIsNone(spreads[m]["recall_spread"])
            self.assertIsNone(spreads[m]["recall_mean"])

    def test_invalid_hash_format_yields_none(self):
        h = hygiene()
        res = as_results([
            ("a1", mk_result(model="A", rep=1, sha="not-a-hash")),
            ("a2", mk_result(model="A", rep=2, sha="not-a-hash")),
            ("b1", mk_result(model="B", rep=1, sha="not-a-hash")),
            ("b2", mk_result(model="B", rep=2, sha="not-a-hash")),
        ])
        spreads = h.repeat_spread(CASE_DEFS, res, {})
        for m in ("A", "B"):
            self.assertIsNone(spreads[m]["recall_spread"])

    def test_uses_score_module_for_recall(self):
        # repeat_spread must delegate per-rep recall to score.score_result
        h = hygiene()
        with mock.patch.object(score, "score_result", wraps=score.score_result) as sp:
            h.repeat_spread(CASE_DEFS, self._balanced(), {})
            self.assertTrue(sp.called, "repeat_spread must use score.score_result")


class RenderComparisons(unittest.TestCase):
    def _spreads(self, mean_a, spread_a, mean_b, spread_b):
        return {
            "A": {"effort": "medium", "repeats": 2, "recall_spread": spread_a,
                  "reason": "ok", "recall_mean": mean_a},
            "B": {"effort": "medium", "repeats": 2, "recall_spread": spread_b,
                  "reason": "ok", "recall_mean": mean_b},
        }

    def test_clear_winner_shows_numeric_delta(self):
        h = hygiene()
        text = h.render_comparisons(self._spreads(1.0, 0.0, 0.0, 0.0))
        self.assertIn("1.0", text)
        self.assertNotIn("withheld", text.lower())

    def test_overlapping_withheld_and_no_numeric_delta(self):
        h = hygiene()
        # means 0.80 vs 0.60 diff 0.20; spreads 0.40+0.40=0.80 -> withheld.
        # 0.20 is unique (not equal to either mean or spread).
        text = h.render_comparisons(self._spreads(0.80, 0.40, 0.60, 0.40))
        self.assertIn("withheld", text.lower())
        self.assertNotIn("0.20", text)

    def test_equal_threshold_withheld(self):
        h = hygiene()
        # diff 0.40 == sum 0.40 -> strictly-larger rule withholds
        text = h.render_comparisons(self._spreads(0.60, 0.20, 0.20, 0.20))
        self.assertIn("withheld", text.lower())

    def test_unavailable_spread_withheld(self):
        h = hygiene()
        sp = {"A": {"effort": "medium", "repeats": 1, "recall_spread": None,
                    "reason": "need >=2 reps", "recall_mean": None},
              "B": {"effort": "medium", "repeats": 1, "recall_spread": None,
                    "reason": "need >=2 reps", "recall_mean": None}}
        text = h.render_comparisons(sp)
        self.assertIn("withheld", text.lower())

    def test_returns_str(self):
        h = hygiene()
        self.assertIsInstance(h.render_comparisons(self._spreads(1.0, 0.0, 0.0, 0.0)), str)


class ScoreIntegration(unittest.TestCase):
    def _results(self, effort="medium"):
        # Two positive runs: each default review finds d1 of 2 defects, so the
        # aggregate is found=2 over defects=4 with no failures.
        return as_results([
            ("a", mk_result(model="m", effort=effort, rep=1, sha=SHA_A)),
            ("b", mk_result(model="m", effort=effort, rep=2, sha=SHA_A)),
        ])

    def test_summarize_backward_compatible_plus_metadata(self):
        res = self._results()
        m = score.summarize(CASE_DEFS, res, {})["m"]
        for k in ("runs", "failed", "found", "defects", "findings", "matched"):
            self.assertIn(k, m)
        self.assertEqual((m["found"], m["defects"], m["failed"]), (2, 4, 0))
        # new metadata
        self.assertIn("effort", m)
        self.assertEqual(m["effort"], "medium")
        has_variance = any(k in m for k in ("recall_spread", "repeat_spread", "repeats",
                                            "recall_mean", "variance", "spread"))
        self.assertTrue(has_variance, f"per-model variance metadata missing: {sorted(m)}")

    def test_render_single_table_same_row_count_with_new_columns(self):
        runs = as_results([("a", mk_result(model="x", effort="medium", rep=1, sha=SHA_A)),
                           ("b", mk_result(model="y", effort="medium", rep=1, sha=SHA_A))])
        table = score.render(score.summarize({"c1": CASE_DEFS["c1"]}, runs, {}))
        self.assertEqual(len(table.splitlines()), 4)  # header+sep+2 rows
        header = table.splitlines()[0].lower()
        self.assertIn("effort", header)
        self.assertTrue("spread" in header or "repeat" in header)

    def _write_main_fixture(self, tmp, results):
        cases_p = os.path.join(tmp, "cases.json")
        with open(cases_p, "w", encoding="utf-8") as fh:
            json.dump({"cases": [dict(CASE_DEFS["c1"], kind="real", title="t", body="b",
                                      head_sha="abc")]}, fh)
        resdir = os.path.join(tmp, "results")
        os.makedirs(resdir)
        for key, r in results:
            with open(os.path.join(resdir, key + ".json"), "w", encoding="utf-8") as fh:
                json.dump(r, fh)
        return cases_p, resdir

    def test_main_rejects_mixed_effort_with_code_2_and_stderr(self):
        results = as_results([
            ("c1__m__low__r1", mk_result(model="m", effort="low", rep=1, sha=SHA_A)),
            ("c1__m__high__r1b", mk_result(model="m", effort="high", rep=2, sha=SHA_A)),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            cases_p, resdir = self._write_main_fixture(tmp, results)
            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out):
                rc = score.main(["--cases", cases_p, "--results", resdir])
            self.assertEqual(rc, 2)
            self.assertTrue(err.getvalue().strip(), "stderr must explain invalid comparison")
            self.assertIn("effort", err.getvalue().lower())
            self.assertEqual(out.getvalue().strip(), "", "must print no report on invalid")

    def test_main_rejects_bad_rep_with_code_2(self):
        r = mk_result(model="m", effort="medium", rep=1, sha=SHA_A)
        r["rep"] = True  # bool invalid
        with tempfile.TemporaryDirectory() as tmp:
            cases_p, resdir = self._write_main_fixture(tmp, as_results([("k", r)]))
            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out):
                rc = score.main(["--cases", cases_p, "--results", resdir])
            self.assertEqual(rc, 2)
            self.assertTrue(err.getvalue().strip())
            self.assertEqual(out.getvalue().strip(), "")

    def test_main_appends_comparisons_on_valid(self):
        h = hygiene()
        _ = h
        results = as_results([
            ("A-r1", mk_result(model="A", effort="medium", rep=1, sha=SHA_A)),
            ("A-r2", mk_result(model="A", effort="medium", rep=2, sha=SHA_A,
                               review=mk_review([]))),
            ("B-r1", mk_result(model="B", effort="medium", rep=1, sha=SHA_A)),
            ("B-r2", mk_result(model="B", effort="medium", rep=2, sha=SHA_A,
                               review=mk_review([]))),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            cases_p, resdir = self._write_main_fixture(tmp, results)
            # cases fixture needs both models' case; c1 present is enough
            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out):
                rc = score.main(["--cases", cases_p, "--results", resdir])
            self.assertEqual(rc, 0)
            body = out.getvalue().lower()
            self.assertTrue("withheld" in body or "compar" in body or "spread" in body or
                            "repeat" in body or "delta" in body or "difference" in body,
                            f"main must append hygiene.render_comparisons output, got: {out.getvalue()[:500]}")
            # Pin the footer itself: table columns alone (effort/spread/repeat)
            # must not satisfy this test if the render_comparisons call is removed.
            self.assertIn("## Recall comparisons", out.getvalue(),
                          "score.main must print the hygiene.render_comparisons footer")


class IsolationConfine(unittest.TestCase):
    # Child programs are built with real newlines (a compound statement such
    # as try/except can never follow a semicolon) and compile-checked in the
    # parent before spawning, so a failure is a real confinement result.
    def _run_child(self, runtime, probe):
        lines = [
            "import sys",
            f"sys.path.insert(0, {HERE!r})",
            "import isolation",
            f"isolation.confine({runtime!r})",
            probe,
        ]
        code = "\n".join(lines) + "\n"
        compile(code, "<confinement-child>", "exec")  # precheck, never executes confine here
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)

    def test_runtime_read_write_works_under_confinement(self):
        iso = isolation_mod()
        self.assertTrue(callable(iso.confine))
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "rt")
            os.makedirs(rt)
            probe = ("open(%r + '/ok.txt','w').write('hi'); "
                     "assert open(%r + '/ok.txt').read() == 'hi'; "
                     "print('RT_OK')" % (rt, rt))
            p = self._run_child(rt, probe)
            self.assertIn("RT_OK", p.stdout, f"child failed: rc={p.returncode} err={p.stderr[:2000]}")

    def test_outside_files_cannot_be_read_or_overwritten(self):
        isolation_mod()  # RED if missing
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "rt")
            os.makedirs(rt)
            secret = os.path.join(tmp, "grader-secret.txt")
            with open(secret, "w", encoding="utf-8") as fh:
                fh.write("ORIGINAL-SECRET")
            arm_out = os.path.join(tmp, "arm-other.json")
            with open(arm_out, "w", encoding="utf-8") as fh:
                fh.write('{"winner": "other-arm"}')
            link = os.path.join(rt, "evil-link")
            try:
                os.symlink(secret, link)
            except OSError:
                self.fail("cannot create symlink for test")
            probe = "\n".join([
                "import os",
                f"secret={secret!r}",
                f"arm={arm_out!r}",
                f"link={link!r}",
                "read_blocked=False",
                "write_blocked=False",
                "link_blocked=False",
                "try:",
                "    open(secret).read()",
                "except Exception:",
                "    read_blocked=True",
                "try:",
                "    open(secret,'w').write('PWNED')",
                "except Exception:",
                "    write_blocked=True",
                "try:",
                "    open(link).read()",
                "except Exception:",
                "    link_blocked=True",
                "try:",
                "    open(arm,'w').write('PWNED')",
                "except Exception:",
                "    pass",
                "assert read_blocked, 'outside read was NOT blocked'",
                "assert write_blocked, 'outside overwrite was NOT blocked'",
                "assert link_blocked, 'symlink escape was NOT blocked'",
                "print('CONFINED')",
            ])
            p = self._run_child(rt, probe)
            self.assertIn("CONFINED", p.stdout, f"confinement failed: rc={p.returncode} err={p.stderr[:2000]}")
            with open(secret, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "ORIGINAL-SECRET")
            with open(arm_out, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '{"winner": "other-arm"}')

    def test_tls_bootstrap_allowlist_readable_but_etc_not(self):
        # Regression for the offline launcher failure (node OpenSSL
        # "Permission denied fopen(/etc/ssl/openssl.cnf)"): the confined
        # child must read the TLS/bootstrap allowlist read-only, while an
        # unrelated /etc file stays denied (proof /etc was NOT granted whole).
        # O_WRONLY without O_TRUNC/O_CREAT never modifies the target: on
        # success the fd is closed and the test fails; nothing is written.
        isolation_mod()  # RED until the allowlist lands in isolation.py
        allow_files = [p for p in ("/etc/ssl/openssl.cnf", "/etc/hosts",
                                   "/etc/resolv.conf", "/etc/nsswitch.conf")
                       if os.path.isfile(p)]
        if "/etc/hosts" not in allow_files:
            self.skipTest("CI standard Linux is expected to provide /etc/hosts")
        certs = "/etc/ssl/certs" if os.path.isdir("/etc/ssl/certs") else None
        deny = next((p for p in ("/etc/hostname", "/etc/fstab") if os.path.isfile(p)), None)
        if deny is None:
            self.skipTest("no unrelated /etc file present to prove /etc is not granted whole")
        before = {}
        for p in allow_files + [deny]:
            with open(p, "rb") as fh:
                before[p] = fh.read()
        probe = "\n".join([
            "import os",
            f"allow={allow_files!r}",
            f"certs={certs!r}",
            f"deny={deny!r}",
            "for p in allow:",
            "    assert open(p, 'rb').read(1) is not None, 'allowlist unreadable: ' + p",
            "if certs is not None:",
            "    names=os.listdir(certs)",
            "    assert names, 'certs dir lists nothing'",
            "    assert open(os.path.join(certs, names[0]), 'rb').read(1) is not None",
            "for p in allow:",
            "    try:",
            "        fd=os.open(p, os.O_WRONLY)",
            "    except Exception:",
            "        pass",
            "    else:",
            "        os.close(fd)",
            "        raise AssertionError('allowlist file writable (must be read-only): ' + p)",
            "try:",
            "    open(deny, 'rb').read(1)",
            "except Exception:",
            "    pass",
            "else:",
            "    raise AssertionError('unrelated /etc file readable (whole /etc granted?): ' + deny)",
            "print('TLS_ALLOWLIST_OK')",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "rt")
            os.makedirs(rt)
            p = self._run_child(rt, probe)
            self.assertIn("TLS_ALLOWLIST_OK", p.stdout,
                          f"TLS allowlist regression: rc={p.returncode} err={p.stderr[:2000]}")
        for pth, blob in before.items():
            with open(pth, "rb") as fh:
                self.assertEqual(fh.read(), blob, f"host file modified: {pth}")

    def test_dotdot_escape_blocked(self):
        isolation_mod()
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "rt")
            os.makedirs(os.path.join(rt, "sub"))
            secret = os.path.join(tmp, "top-secret.txt")
            with open(secret, "w", encoding="utf-8") as fh:
                fh.write("TOP")
            probe = "\n".join([
                f"evil={os.path.join(rt, 'sub', '..', '..', 'top-secret.txt')!r}",
                "blocked=False",
                "try:",
                "    open(evil).read()",
                "except Exception:",
                "    blocked=True",
                "assert blocked, '../ escape was NOT blocked'",
                "print('DOTDOT_OK')",
            ])
            p = self._run_child(rt, probe)
            self.assertIn("DOTDOT_OK", p.stdout, f"rc={p.returncode} err={p.stderr[:2000]}")

    def test_unsupported_fails_closed_no_silent_skip_on_linux(self):
        import platform
        iso = isolation_mod()
        self.assertTrue(callable(getattr(iso, "confine", None)))
        if platform.system() != "Linux":
            self.skipTest("Linux-only strictness check")
        # On Linux the confinement test above must genuinely enforce; reaching
        # here without OSError support means fail-closed is testable via child.
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "rt")
            os.makedirs(rt)
            probe = "print('IMPORT_OK')"
            p = self._run_child(rt, probe.replace("print('IMPORT_OK')",
                "import isolation as I; assert callable(I.confine); print('IMPORT_OK')"))
            self.assertIn("IMPORT_OK", p.stdout, f"err={p.stderr[:2000]}")
        # confine must raise OSError (fail closed) when Landlock ABI unsupported:
        # at minimum, OSError must be a possible outcome, not a silent pass.
        self.assertTrue(issubclass(OSError, Exception))

    def test_forced_unsupported_platform_fail_closed(self):
        # Forced fail-closed: even on a Linux host, a non-Linux platform (and,
        # where the implementation exposes one, a forced-unsupported ABI/libc
        # boundary) must raise OSError, never silently confine-or-skip.
        # Runs in the child: confine() cannot be undone in this process.
        isolation_mod()  # RED if missing
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "rt")
            os.makedirs(rt)
            probe = "\n".join([
                "import sys",
                "import platform",
                "from unittest import mock",
                "from contextlib import ExitStack",
                "import isolation as I",
                f"rt={rt!r}",
                "targets=[mock.patch.object(platform, 'system', return_value='Darwin'),",
                "    mock.patch.object(sys, 'platform', 'darwin')]",
                "iso_plat=getattr(I, 'platform', None)",
                "if iso_plat is not None and iso_plat is not platform and hasattr(iso_plat, 'system'):",
                "    targets.append(mock.patch.object(iso_plat, 'system', return_value='Darwin'))",
                "if callable(getattr(I, 'system', None)):",
                "    targets.append(mock.patch.object(I, 'system', return_value='Darwin'))",
                "raised=False",
                "with ExitStack() as st:",
                "    for c in targets:",
                "        st.enter_context(c)",
                "    try:",
                "        I.confine(rt)",
                "    except OSError:",
                "        raised=True",
                "    except Exception as exc:",
                "        raise AssertionError('wrong exc for unsupported platform: %r' % (exc,))",
                "assert raised, 'confine did not fail closed (OSError) on unsupported platform'",
                "print('FAIL_CLOSED_FORCED_OK')",
            ])
            p = self._run_child(rt, probe)
            self.assertIn("FAIL_CLOSED_FORCED_OK", p.stdout,
                          f"forced fail-closed failed: rc={p.returncode} err={p.stderr[:2000]}")

    def test_descendant_process_escape_blocked(self):
        # Landlock policy must cover descendants: a grandchild spawned by the
        # confined child must also be denied outside read/write.
        isolation_mod()  # RED if missing
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "rt")
            os.makedirs(rt)
            secret = os.path.join(tmp, "grader-secret.txt")
            with open(secret, "w", encoding="utf-8") as fh:
                fh.write("ORIGINAL-DESCENDANT-SECRET")
            grandchild = "\n".join([
                f"secret={secret!r}",
                "read_blocked=False",
                "write_blocked=False",
                "try:",
                "    open(secret).read()",
                "except Exception:",
                "    read_blocked=True",
                "try:",
                "    open(secret,'w').write('PWNED')",
                "except Exception:",
                "    write_blocked=True",
                "assert read_blocked, 'grandchild read NOT blocked'",
                "assert write_blocked, 'grandchild write NOT blocked'",
                "print('GRANDCHILD_BLOCKED')",
            ])
            compile(grandchild, "<grandchild>", "exec")  # precheck grandchild too
            probe = "\n".join([
                "import subprocess, sys",
                f"gc={grandchild!r}",
                "r=subprocess.run([sys.executable, '-c', gc], capture_output=True, text=True, timeout=60)",
                "assert 'GRANDCHILD_BLOCKED' in r.stdout, 'descendant escape: rc=%d out=%s err=%s' % (r.returncode, r.stdout[:500], r.stderr[:500])",
                "print('DESCENDANT_CONFINED')",
            ])
            p = self._run_child(rt, probe)
            self.assertIn("DESCENDANT_CONFINED", p.stdout,
                          f"descendant confinement failed: rc={p.returncode} err={p.stderr[:2000]}")
            with open(secret, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "ORIGINAL-DESCENDANT-SECRET")


class RunCaseIsolation(unittest.TestCase):
    def _patch_harness(self, prompt_text="PROMPT-BODY"):
        case = {"id": "c1", "kind": "real", "head_sha": "abc123",
                "title": "Fix thing", "body": "body text",
                "defects": [{"id": "d1", "file": "src/a.ts", "line_start": 1,
                             "line_end": 2, "severity": "critical",
                             "description": "secret defect label"}]}
        seen = {}
        fake_proc = mock.Mock()
        fake_proc.stdout = ""
        fake_proc.returncode = 0
        last_path = os.path.join(tempfile.mkdtemp(), "last.json")
        with open(last_path, "w", encoding="utf-8") as fh:
            json.dump(mk_review([finding("src/a.ts", 1)]), fh)

        # Hermetic boundary: every subprocess.run in run_case (the isolated
        # launcher AND the finally-block git worktree removal) lands here; no
        # real codex, git, or network ever executes.
        def fake_run(args, **kw):
            seen.setdefault("calls", []).append((list(args), dict(kw)))
            if "input" not in kw:
                return fake_proc  # cleanup call (git worktree remove): observe, don't adopt
            seen["args"] = args
            seen["kw"] = kw
            # Observe the private-runtime TMPDIR while the child env is live:
            # it must exist at launch (the scratch tree is removed on return).
            _tenv = kw.get("env", {})
            seen["tmpdir_isdir_at_launch"] = bool(_tenv.get("TMPDIR")) and os.path.isdir(_tenv["TMPDIR"])
            # emulate codex writing its final message
            for i, a in enumerate(args):
                if a == "-o" and i + 1 < len(args):
                    try:
                        with open(last_path, encoding="utf-8") as fh:
                            content = fh.read()
                        with open(args[i + 1], "w", encoding="utf-8") as out:
                            out.write(content)
                    except OSError:
                        pass
            return fake_proc

        patches = [
            mock.patch.object(run_review, "prepare_worktree", lambda repo, case, dest: None),
            mock.patch.object(run_review, "sh",
                              lambda *a, **k: "deadbeef diff" if "--patch" in a else "src/a.ts"),
            mock.patch.object(run_review, "read_git_file", lambda cwd, path: "file text"),
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch.object(run_review.time, "time", return_value=1000.0),
        ]
        return case, seen, last_path, patches

    def test_launch_uses_isolation_cli_with_private_cwd(self):
        case, seen, last_path, patches = self._patch_harness()
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "CLIPROXY_KEY_ENV_VALUE": "k"}, clear=False):
                res = run_review.run_case("/repo", case, "muse-spark-1.3", "medium",
                                          "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in patches:
                p.stop()
        # Hermetic: the launcher observation must come from the input=prompt
        # call, never the finally-block cleanup; every call stayed in-process.
        calls = seen.get("calls", [])
        self.assertTrue(calls, "expected mocked subprocess calls, got none")
        launchers = [c for c in calls if "input" in c[1]]
        self.assertEqual(len(launchers), 1, f"expected exactly one launcher call, got {len(launchers)}")
        for argv, _kw in calls:
            self.assertNotEqual(argv[0] if argv else None, "codex",
                                "must never exec real codex directly; launcher goes via isolation.py")
        args = seen.get("args", [])
        blob = json.dumps(args)
        self.assertIn("isolation.py", blob, f"must launch via isolation.py CLI, got {args}")
        self.assertIn("--", args, "CLI must separate runtime from command with --")
        self.assertTrue(any("codex" in str(a) for a in args), "codex command must be present")
        cwd = seen.get("kw", {}).get("cwd", "")
        self.assertTrue(cwd, "child cwd must be set")
        # cwd must be the private runtime dir, not the scratch wt dir
        self.assertIn("runtime", cwd.lower() + blob.lower(),
                      f"cwd should be private runtime, got cwd={cwd!r} args={args}")

    def test_result_records_input_sha256_and_effort(self):
        case, seen, last_path, patches = self._patch_harness()
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "CLIPROXY_KEY_ENV_VALUE": "k"}, clear=False):
                res = run_review.run_case("/repo", case, "m1", "high",
                                          "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in patches:
                p.stop()
        prompt = seen.get("kw", {}).get("input", "")
        self.assertTrue(prompt, "prompt must be passed as subprocess input")
        expect = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.assertIn("input_sha256", res, "result must record input_sha256")
        self.assertEqual(res["input_sha256"], expect)
        self.assertRegex(res["input_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(res.get("effort"), "high")

    def test_child_env_stays_minimal(self):
        case, seen, last_path, patches = self._patch_harness()
        for p in patches:
            p.start()
        try:
            env = {"PATH": "/usr/bin:/bin", "CLIPROXY_KEY_ENV_VALUE": "sekret",
                   "LEAK_ME": "should-not-pass", "HOME": "/home/u"}
            with mock.patch.dict(os.environ, env, clear=True):
                os.environ["PATH"] = "/usr/bin:/bin"
                os.environ["CLIPROXY_KEY_ENV_VALUE"] = "sekret"
                os.environ["LEAK_ME"] = "should-not-pass"
                run_review.run_case("/repo", case, "m1", "medium",
                                    "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in patches:
                p.stop()
        child_env = seen.get("kw", {}).get("env", {})
        self.assertNotIn("LEAK_ME", child_env)
        self.assertNotIn("CLIPROXY_KEY_ENV_VALUE", child_env)
        # The key reaches the child ONLY as OPENAI_API_KEY (the documented
        # transport); it must not leak under any other name or into argv.
        self.assertEqual(child_env.get("OPENAI_API_KEY"), "sekret")
        for k, v in child_env.items():
            if k != "OPENAI_API_KEY":
                self.assertNotIn("sekret", str(v), f"key leaked via {k}")
        self.assertNotIn("sekret", json.dumps(seen.get("args", [])),
                         "raw key must never appear in child argv")
        allowed = {"PATH", "HOME", "CODEX_HOME", "OPENAI_API_KEY", "TMPDIR"}
        self.assertEqual(set(child_env), allowed,
                         f"child env must stay minimal (5 keys), got {sorted(child_env)}")
        # TMPDIR must be the same private runtime, an existing directory at
        # launch, never the shared global /tmp.
        cwd = seen.get("kw", {}).get("cwd", "")
        tmpdir = child_env.get("TMPDIR")
        self.assertTrue(tmpdir, "TMPDIR must be set in the child env")
        self.assertNotIn(tmpdir.rstrip("/"), ("/tmp",),
                         "TMPDIR must not be the global /tmp")
        self.assertTrue(tmpdir == cwd or tmpdir.startswith(cwd.rstrip("/") + "/"),
                        f"TMPDIR must live under the private runtime cwd={cwd!r}, got {tmpdir!r}")
        self.assertTrue(seen.get("tmpdir_isdir_at_launch"),
                        f"TMPDIR must exist at launch, got {tmpdir!r}")

    def test_missing_parent_path_falls_back(self):
        # A controller without PATH in its environment must not crash the
        # launch with KeyError; the child gets a fixed minimal fallback.
        case, seen, last_path, patches = self._patch_harness()
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"CLIPROXY_KEY_ENV_VALUE": "k"}, clear=True):
                assert "PATH" not in os.environ
                run_review.run_case("/repo", case, "m1", "medium",
                                    "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in patches:
                p.stop()
        child_env = seen.get("kw", {}).get("env", {})
        self.assertEqual(child_env.get("PATH"), "/usr/local/bin:/usr/bin:/bin")

    def test_effort_reaches_codex_argv(self):
        case, seen, last_path, patches = self._patch_harness()
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "CLIPROXY_KEY_ENV_VALUE": "k"}, clear=False):
                run_review.run_case("/repo", case, "m1", "xhigh",
                                    "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in patches:
                p.stop()
        blob = json.dumps(seen.get("args", []))
        self.assertIn("xhigh", blob, f"effort must reach codex argv, got {seen.get('args')}")

    def test_schema_copied_to_runtime_not_grader_dir(self):
        case, seen, last_path, patches = self._patch_harness()
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "CLIPROXY_KEY_ENV_VALUE": "k"}, clear=False):
                run_review.run_case("/repo", case, "m1", "medium",
                                    "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in patches:
                p.stop()
        blob = json.dumps(seen.get("args", []))
        grader_schema = os.path.join(run_review.HERE, "output_schema.json")
        # some schema-looking arg must live under the runtime cwd, not the grader dir
        candidates = [a for a in (seen.get("args", []) or []) if str(a).endswith(".json")]
        self.assertTrue(candidates, f"expected a schema path in argv, got {seen.get('args')}")
        cwd = seen.get("kw", {}).get("cwd", "")
        self.assertTrue(any(str(c).startswith(str(cwd)) for c in candidates),
                        f"schema must be copied under runtime cwd={cwd!r}, got {candidates}")
        self.assertNotIn(grader_schema, blob,
                         "must not reference the original grader schema dir")

    def test_prompt_contains_no_defect_labels_or_other_arm_results(self):
        case, seen, last_path, patches = self._patch_harness()
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "CLIPROXY_KEY_ENV_VALUE": "k"}, clear=False):
                run_review.run_case("/repo", case, "m1", "medium",
                                    "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in patches:
                p.stop()
        prompt = seen.get("kw", {}).get("input", "")
        self.assertNotIn("d1", prompt, "prompt must not leak defect id labels")
        self.assertNotIn("secret defect label", prompt)
        self.assertNotIn("other-arm", prompt.lower())
        self.assertNotIn("valid-other", prompt.lower())


class EffortMaxCLI(unittest.TestCase):
    # Both launchers accept --effort max (choices come from
    # run_review.EFFORTS). Mocks only; no model, no subprocess, no network.
    def test_efforts_constant_includes_max(self):
        self.assertEqual(set(run_review.EFFORTS), {"low", "medium", "high", "xhigh", "max"})

    def test_run_review_main_accepts_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_p = os.path.join(tmp, "cases.json")
            with open(cases_p, "w", encoding="utf-8") as fh:
                json.dump({"cases": [{"id": "c1"}]}, fh)
            out = os.path.join(tmp, "out")
            canned = {"case_id": "c1", "model": "m", "effort": "max",
                      "latency_s": 1.0, "usage": {},
                      "review": mk_review([finding("src/a.ts", 1)])}
            with mock.patch.object(run_review, "run_case", return_value=dict(canned)) as rc, \
                    mock.patch.dict(os.environ, {"K": "sekret"}):
                got = run_review.main(["--repo", "/repo", "--case", cases_p, "--case-id", "c1",
                                       "--model", "m", "--effort", "max",
                                       "--key-env", "K", "--out", out])
            self.assertEqual(got, 0)
            self.assertEqual(rc.call_args[0][3], "max")
            self.assertTrue(os.path.isfile(os.path.join(out, "c1__m__max__r1.json")))

    def test_batch_main_accepts_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_p = os.path.join(tmp, "cases.json")
            with open(cases_p, "w", encoding="utf-8") as fh:
                json.dump({"cases": []}, fh)
            out = os.path.join(tmp, "out")
            with mock.patch.object(batch, "run",
                                   return_value={"planned": 0, "done": 0,
                                                 "failed": 0, "seconds": 0.0}) as br:
                got = batch.main(["--repo", "/repo", "--cases", cases_p, "--models", "m",
                                  "--effort", "max", "--key-env", "K", "--out", out])
            self.assertEqual(got, 0)
            self.assertEqual(br.call_args[0][3], "max")


class ModelValidation(unittest.TestCase):
    # A known-case result with a missing, empty, or non-string model cannot be
    # assigned to an arm and must never be silently discarded: raise a useful
    # ValueError naming the model, not KeyError/TypeError.
    BAD_MODELS = ["<missing>", None, "", 123, True]

    def _result_with_model(self, bad):
        r = mk_result(case_id="c1", model="m", effort="medium", rep=1, sha=SHA_A)
        if bad == "<missing>":
            del r["model"]
        else:
            r["model"] = bad
        return r

    def test_repeat_spread_rejects_bad_model(self):
        h = hygiene()
        for bad in self.BAD_MODELS:
            with self.subTest(model=bad):
                with self.assertRaises(ValueError) as ctx:
                    h.repeat_spread(CASE_DEFS, as_results([("k", self._result_with_model(bad))]), {})
                self.assertIn("model", str(ctx.exception).lower())

    def test_summarize_rejects_bad_model(self):
        for bad in self.BAD_MODELS:
            with self.subTest(model=bad):
                with self.assertRaises(ValueError) as ctx:
                    score.summarize(CASE_DEFS, as_results([("k", self._result_with_model(bad))]), {})
                self.assertIn("model", str(ctx.exception).lower())


class CheckTableParser(unittest.TestCase):
    # Hermetic synthetic regression for the shell check_table parser: public
    # CI skips the historical raw-data path, so without this the header-based
    # parser (named columns + comparisons footer) would execute nowhere.
    # Extracts the embedded check_table.py from the shell test, runs it
    # against a synthetic per-arm stub scorer emitting the current
    # named-column table plus footer: a matching table must pass and one
    # altered recall cell must reject. No git, no model, no network.
    SH_NAME = "test_case_61df0b37_review_bakeoff.sh"
    ARM_MODELS = (("opus", "claude-opus-5-5"), ("sonnet", "claude-sonnet-5-5"),
                  ("astra", "gpt-6-astra"), ("sol", "gpt-6.1-sol"),
                  ("muse", "muse-spark-1.3"), ("glm", "glm-5.3"),
                  ("codex", "codex-auto-review"))

    def _extract_parser(self, dest):
        sh_path = os.path.join(os.path.dirname(os.path.dirname(HERE)), self.SH_NAME)
        if not os.path.isfile(sh_path):
            self.skipTest(f"{self.SH_NAME} absent from this checkout")
        with open(sh_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        marker = 'cat > "$T/check_table.py" <<\'PY\''
        try:
            start = next(i for i, ln in enumerate(lines) if ln == marker)
            end = next(i for i in range(start + 1, len(lines)) if lines[i] == "PY")
        except StopIteration:
            self.fail("check_table.py heredoc markers missing in shell test")
        body = "\n".join(lines[start + 1:end]) + "\n"
        compile(body, "<check_table>", "exec")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(body)
        return dest

    def _stub_base(self, root):
        base = os.path.join(root, "base")
        os.makedirs(os.path.join(base, "dataset"))
        for arm, _model in self.ARM_MODELS:
            os.makedirs(os.path.join(base, "results", f"{arm}-medium-r1"))
        stub = "\n".join([
            "import sys",
            "MODELS = {",
        ] + [
            f"    {arm!r}: {model!r}," for arm, model in self.ARM_MODELS
        ] + [
            "}",
            "resdir = sys.argv[sys.argv.index('--results') + 1].rstrip('/')",
            "arm = resdir.split('/')[-1].split('-')[0]",
            "m = MODELS[arm]",
            "print('| model | effort (requested) | recall repeat spread | reps | runs | failed | recall (found/defects) | recall 95% CI | diff-only recall | needs-context recall | matched/findings | precision (lower bound) | severity exact | false-pass 5/5 | critical missed | lat p50 s | lat p95 s | tokens/review |')",
            "print('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')",
            "print(f'| {m} | medium | unmeasured: need >=2 | 1 | 19 | 0 | 11/24 = 0.46 | 0.28-0.65 | 4/16 | 7/8 | 20/30 | 0.10 | 0.67 | 3/19 | 0/2 | 16.7 | 31.7 | 31000 |')",
            "print()",
            "print('## Recall comparisons')",
            "print()",
            "print('- a / b: withheld (repeat spread unavailable).')",
            "",
        ])
        compile(stub, "<stub-score>", "exec")
        with open(os.path.join(base, "score.py"), "w", encoding="utf-8") as fh:
            fh.write(stub)
        return base

    def _committed_table(self, root):
        path = os.path.join(root, "table.md")
        with open(path, "w", encoding="utf-8") as fh:
            for _arm, model in self.ARM_MODELS:
                fh.write(f"| Arm (`{model}`) | medium | stuff | 11/24 = 0.46 | x | 3/19 | y |\n")
        return path

    def test_matching_table_passes_and_altered_recall_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._extract_parser(os.path.join(tmp, "check_table.py"))
            base = self._stub_base(tmp)
            good = self._committed_table(tmp)
            proc = subprocess.run([sys.executable, parser, base, good],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             f"parser must accept the matching table: {proc.stderr[:1000]}")
            bad = os.path.join(tmp, "mutated.md")
            with open(good, encoding="utf-8") as fh:
                content = fh.read().replace("11/24 = 0.46", "12/24 = 0.50", 1)
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write(content)
            proc = subprocess.run([sys.executable, parser, base, bad],
                                  capture_output=True, text=True, timeout=120)
            self.assertNotEqual(proc.returncode, 0,
                                "parser must reject a table with one altered recall cell")


if __name__ == "__main__":
    unittest.main()
