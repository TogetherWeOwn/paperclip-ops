"""Hermetic unit tests for the Tier-1 check scaffold (case_8c53addb).

No network, no credentials, no repo access. Run from the repo root:
  (cd ops/review_tier1 && python3 -m unittest test_review_tier1)
"""

import unittest

from corpus import (
    changed_sections,
    code_context,
    gold_for_sections,
    parse_diff,
    relevant_hunks,
    sections,
)
from deterministic import (
    ac_all_mapped,
    ac_coverage,
    changed_files,
    changed_symbols,
    changed_tests,
    claim_gaps,
    doc_sections_stale,
    identifiers,
    shared_identifiers,
)
from evidence_gate import gate_findings, normalize, verify
from eval import cost_per_pr, score, shippable, wilson
from schemas import (
    AC_EVIDENCE_SCHEMA,
    CLAIMS_VS_DIFF_SCHEMA,
    DOCS_VS_DIFF_SCHEMA,
    majority_vote,
    validate_output,
    validate_verdict,
)

DIFF = """diff --git a/ops/review_tier1/eval.py b/ops/review_tier1/eval.py
index 111..222 100644
--- a/ops/review_tier1/eval.py
+++ b/ops/review_tier1/eval.py
@@ -1,2 +1,3 @@ def score(rows):
+def cost_per_pr(rows):
+    return 0.0
diff --git a/ops/review_tier1/test_review_tier1.py b/ops/review_tier1/test_review_tier1.py
index 333..444 100644
--- a/ops/review_tier1/test_review_tier1.py
+++ b/ops/review_tier1/test_review_tier1.py
@@ -0,0 +1,2 @@
+def test_cost_per_pr_counts_distinct_prs(self):
+    pass
"""


class SchemasTest(unittest.TestCase):
    def test_never_approve(self):
        self.assertFalse(validate_verdict("approve"))
        self.assertFalse(validate_verdict("APPROVE"))
        for verdict in ("block", "hold", "pass"):
            self.assertTrue(validate_verdict(verdict))

    def test_output_requires_all_fields_and_legal_verdict(self):
        good = {"section": "s", "holds": True, "verdict": "pass",
                "evidence": "e"}
        self.assertTrue(validate_output(DOCS_VS_DIFF_SCHEMA, good))
        missing = {"section": "s", "holds": True, "verdict": "pass"}
        self.assertFalse(validate_output(DOCS_VS_DIFF_SCHEMA, missing))
        approving = dict(good, verdict="approve")
        self.assertFalse(validate_output(DOCS_VS_DIFF_SCHEMA, approving))
        self.assertFalse(validate_output(AC_EVIDENCE_SCHEMA, "not-a-dict"))
        self.assertTrue(validate_output(
            CLAIMS_VS_DIFF_SCHEMA,
            {"claim": "c", "supported": False, "verdict": "hold",
             "missing": ["x.py"]}))

    def test_majority_vote(self):
        self.assertEqual(majority_vote(["hold", "hold", "pass"]), "hold")
        self.assertEqual(majority_vote(["block", "block", "pass"]), "block")
        # ties and garbage fail closed to hold, never to approve
        self.assertEqual(majority_vote(["block", "pass", "hold"]), "hold")
        self.assertEqual(majority_vote(["approve", "bogus", None]), "hold")
        self.assertEqual(majority_vote([]), "hold")

    def test_majority_vote_any_invalid_vote_forces_hold(self):
        # case_41fcff80: invalid votes are not ignored in favour of a plurality
        # of the valid ones.
        self.assertEqual(majority_vote(["pass", "bogus", "bogus"]), "hold")
        self.assertEqual(majority_vote(["block", "block", "bogus"]), "hold")
        self.assertEqual(majority_vote(["pass", "pass", None]), "hold")


class DeterministicTest(unittest.TestCase):
    def test_changed_files_lists_both_paths(self):
        self.assertEqual(changed_files(DIFF), [
            "ops/review_tier1/eval.py",
            "ops/review_tier1/test_review_tier1.py",
        ])

    def test_changed_symbols_finds_added_function(self):
        self.assertIn("cost_per_pr", changed_symbols(DIFF))

    def test_changed_tests_finds_added_test(self):
        self.assertIn("test_cost_per_pr_counts_distinct_prs",
                      changed_tests(DIFF))

    def test_empty_diff_yields_empty_sets(self):
        self.assertEqual(changed_files(""), [])
        self.assertEqual(changed_symbols(None), [])
        self.assertEqual(changed_tests(""), [])

    def test_doc_section_naming_absent_path_is_flagged(self):
        doc = "## Consequences\nEdits ops/review_tier1/ship.py land here.\n"
        flagged = doc_sections_stale(
            doc, changed_files(DIFF), changed_symbols(DIFF))
        self.assertTrue(any(token == "ops/review_tier1/ship.py"
                            for _, token in flagged))

    def test_doc_section_naming_present_path_is_quiet(self):
        doc = "## Changed\nSee ops/review_tier1/eval.py for scoring.\n"
        self.assertEqual(
            doc_sections_stale(doc, changed_files(DIFF),
                               changed_symbols(DIFF)), [])

    def test_ac_mapping_requires_symbol_and_test(self):
        mapping = ac_coverage(
            ["cost_per_pr divides total cost by distinct PRs"],
            changed_files(DIFF), changed_symbols(DIFF),
            changed_tests(DIFF))
        symbol, test = mapping["cost_per_pr divides total cost by distinct PRs"]
        self.assertTrue(symbol)
        self.assertTrue(test)
        self.assertTrue(ac_all_mapped(mapping))

    def test_ac_rule_fails_below_100_percent(self):
        mapping = {"mapped one": ("sym", "test_x"),
                   "unmapped other": ("", "")}
        self.assertFalse(ac_all_mapped(mapping))
        self.assertFalse(ac_all_mapped({}))

    CHANGES = "## What Changed\n\n- Adds `ops/review_tier1/ship.py` and edits `ops/review_tier1/eval.py`.\n\n## Risks\n\nSee `docs/other.md`.\n"

    def test_claim_gaps_finds_a_path_the_change_section_names_but_the_diff_lacks(self):
        gaps = claim_gaps(self.CHANGES, changed_files(DIFF), changed_symbols(DIFF))
        self.assertEqual(gaps, ["ops/review_tier1/ship.py"])

    def test_claim_gaps_ignores_other_sections_urls_ratios_and_prose(self):
        body = ("## Thinking Path\n\n- We score `5/5` at `https://x.io/a/b.md` and mention `docs/other.md`.\n"
                "## What Changed\n\n- Edits `ops/review_tier1/eval.py`; scored 5/5 vs `main`.\n")
        self.assertEqual(claim_gaps(body, changed_files(DIFF)), [])

    def test_claim_gaps_quiet_without_a_change_section(self):
        self.assertEqual(claim_gaps("No sections, but `ops/x/missing.py`.", changed_files(DIFF)), [])


class EvalTest(unittest.TestCase):
    ROWS = [
        {"check": "docs", "provider": "jev", "gold": "block",
         "verdict": "block", "cost": 0.0001, "pr": "1"},
        {"check": "docs", "provider": "jev", "gold": "block",
         "verdict": "pass", "cost": 0.0001, "pr": "2"},
        {"check": "docs", "provider": "jev", "gold": "none",
         "verdict": "block", "cost": 0.0001, "pr": "3"},
        {"check": "docs", "provider": "jev", "gold": "none",
         "verdict": "pass", "cost": 0.0001, "pr": "3"},
    ]

    def test_precision_recall_math(self):
        cell = score(self.ROWS)["docs"]["jev"]
        self.assertAlmostEqual(cell["precision"], 0.5)
        self.assertAlmostEqual(cell["recall"], 0.5)
        self.assertAlmostEqual(cell["cost"], 0.0004)

    def test_ship_gate_blocks_below_threshold(self):
        self.assertEqual(shippable(score(self.ROWS)), [])
        perfect = [dict(r, verdict=r["gold"] if r["gold"] != "none" else "pass")
                   for r in self.ROWS]
        shipped = shippable(score(perfect), min_flagged=1)
        self.assertEqual(len(shipped), 1)
        self.assertEqual(shipped[0][0], "docs")

    def test_ship_gate_needs_enough_firings_by_default(self):
        perfect = [dict(r, verdict=r["gold"] if r["gold"] != "none" else "pass")
                   for r in self.ROWS]
        self.assertEqual(shippable(score(perfect)), [])  # 1 firing < 20

    def test_wilson_interval(self):
        low, high = wilson(9, 10)
        self.assertAlmostEqual(low, 0.596, places=2)
        self.assertAlmostEqual(high, 0.982, places=2)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))

    def test_cost_per_pr(self):
        self.assertAlmostEqual(cost_per_pr(self.ROWS), 0.0004 / 3)


DOC_DIFF = """diff --git a/docs/run.md b/docs/run.md
index 111..222 100644
--- a/docs/run.md
+++ b/docs/run.md
@@ -1,4 +1,6 @@
 # Run
 intro
+## Cron
+Run `*/10 * * * * push.sh`.
 ## Other
 stays
\\ No newline at end of file
diff --git a/src/app.py b/src/app.py
new file mode 100644
--- /dev/null
+++ b/src/app.py
@@ -0,0 +1,2 @@
+def push():
+    pass
"""

DOC_TEXT = "# Run\nintro\n## Cron\nRun `*/10 * * * * push.sh`.\n## Other\nstays\n"


class CorpusTest(unittest.TestCase):
    def test_parse_diff_tracks_new_side_added_lines_and_status(self):
        files = parse_diff(DOC_DIFF)
        self.assertEqual(files["docs/run.md"]["added_lines"], {3, 4})
        self.assertEqual(files["src/app.py"]["status"], "added")
        self.assertEqual(files["src/app.py"]["added_lines"], {1, 2})

    def test_sections_split_on_headings_and_ignore_fenced_hashes(self):
        text = "intro\n# A\nbody\n```\n# not a heading\n```\n## B\nmore\n"
        names = [s["heading"] for s in sections(text)]
        self.assertEqual(names, ["(preamble)", "A", "B"])
        a = sections(text)[1]
        self.assertEqual((a["start"], a["end"]), (2, 6))

    def test_changed_sections_only_return_touched_sections(self):
        got = changed_sections(DOC_TEXT, {4})
        self.assertEqual([s["heading"] for s in got], ["Cron"])

    def test_code_context_shares_budget_so_a_huge_file_cannot_starve_others(self):
        files = {"a.py": {"patch": "a" * 100}, "b.py": {"patch": "b" * 10000},
                 "c.md": {"patch": "doc"}}
        text, truncated = code_context(files, 400)
        self.assertTrue(truncated)
        self.assertIn("a" * 100, text)       # small file kept whole
        self.assertNotIn("doc", text)        # docs are not code context
        self.assertLessEqual(len(text), 420)

    def test_code_context_not_truncated_when_it_fits(self):
        text, truncated = code_context({"a.py": {"patch": "x"}}, 100)
        self.assertEqual((text, truncated), ("x", False))

    def test_gold_prefers_block_and_lists_finding_ids(self):
        sects = sections(DOC_TEXT)
        findings = [{"line": 4, "gold": "hold", "id": 1},
                    {"line": 4, "gold": "block", "id": 2}]
        gold = {s["heading"]: (g, ids) for s, g, ids in gold_for_sections(sects, findings)}
        self.assertEqual(gold["Cron"], ("block", [1, 2]))
        self.assertEqual(gold["Other"], ("none", []))


class PrefilterTest(unittest.TestCase):
    def test_identifiers_pick_code_like_tokens_only(self):
        ids = identifiers("Set `balancer_silence_log` or --max-trim; see run_sweep and TOKEN_PATH in sweep.mjs. the and for")
        self.assertTrue({"balancer_silence_log", "--max-trim", "run_sweep",
                         "token_path", "sweep.mjs"} <= ids)
        self.assertNotIn("the", ids)

    def test_shared_identifiers_requires_real_overlap(self):
        section = "The pusher reads `GATUS_URL` and run_sweep()."
        patch = "+  url = os.environ['GATUS_URL']\n+def run_sweep(): pass"
        self.assertEqual(shared_identifiers(section, patch), ["gatus_url", "run_sweep"])
        self.assertEqual(shared_identifiers("Plain prose with nothing code-like.", patch), [])

    def test_plain_words_never_count_even_when_backticked(self):
        self.assertEqual(shared_identifiers("Branch `main` is `null` when `unknown`.",
                                            "+  main = null  # unknown"), [])

    def test_long_doc_token_matches_inside_a_longer_diff_token(self):
        self.assertEqual(shared_identifiers("Flag `max_trim`.", "+  args.max_trim_planned = 3"),
                         ["max_trim"])


HUNK_DIFF = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,2 @@
-cron_line = '*/30'
+cron_line = '*/10'
@@ -20,2 +20,2 @@
-unrelated_x = 1
+unrelated_x = 2
"""


class RelevantHunksTest(unittest.TestCase):
    def test_keeps_only_hunks_that_name_a_section_identifier(self):
        files = parse_diff(HUNK_DIFF)
        text, truncated = relevant_hunks(files, "Set the `cron_line` value.", 4000)
        self.assertIn("*/10", text)
        self.assertNotIn("unrelated_x", text)
        self.assertFalse(truncated)

    def test_empty_when_nothing_overlaps_and_truncates_to_cap(self):
        files = parse_diff(HUNK_DIFF)
        self.assertEqual(relevant_hunks(files, "Nothing code-like here.", 4000), ("", False))
        text, truncated = relevant_hunks(files, "Set the `cron_line` and `unrelated_x` values.", 120)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 120)


GATE_FILES = {
    "src/app.py": "import os\n\n\ndef push():\n    return   os.environ['X']\n",
    "docs/run.md": "# Run\n",
}


def gate_read(path):
    return GATE_FILES.get(path)


class EvidenceGateTest(unittest.TestCase):
    def test_valid_anchor_without_quote_passes(self):
        ok, reason = verify({"file": "src/app.py", "line": 4}, gate_read)
        self.assertEqual((ok, reason), (True, "verified"))

    def test_quote_must_match_the_cited_line_not_a_neighbour(self):
        quote = "return os.environ['X']"
        ok, _ = verify({"file": "src/app.py", "line": 5, "evidence": quote}, gate_read)
        self.assertTrue(ok)
        # same quote against the adjacent line is a mismatch, not a pass
        ok, reason = verify({"file": "src/app.py", "line": 4, "evidence": quote}, gate_read)
        self.assertEqual((ok, reason), (False, "quote_mismatch"))

    def test_quote_comparison_is_whitespace_normalized(self):
        # file line 5 is `    return   os.environ['X']`; spacing must not matter
        ok, _ = verify({"file": "src/app.py", "line": 5, "evidence": "\treturn os.environ['X']  "}, gate_read)
        self.assertTrue(ok)
        self.assertEqual(normalize("  a\tb\nc "), "a b c")

    def test_quote_mismatch_is_withheld(self):
        ok, reason = verify({"file": "src/app.py", "line": 5, "evidence": "return os.environ['Y']"}, gate_read)
        self.assertEqual((ok, reason), (False, "quote_mismatch"))

    def test_empty_quote_falls_back_to_anchor_only(self):
        for quote in ("", "   ", None):
            ok, reason = verify({"file": "src/app.py", "line": 4, "evidence": quote}, gate_read)
            self.assertEqual((ok, reason), (True, "verified"), quote)

    def test_wrong_line_is_withheld(self):
        for line in (0, -1, 7, 99, "4", 4.0, True, None):
            ok, reason = verify({"file": "src/app.py", "line": line}, gate_read)
            self.assertEqual((ok, reason), (False, "bad_line"), line)

    def test_missing_file_is_withheld(self):
        ok, reason = verify({"file": "src/gone.py", "line": 1}, gate_read)
        self.assertEqual((ok, reason), (False, "missing_file"))

    def test_unresolvable_paths_are_withheld_not_read(self):
        seen = []
        for path in ("", "/etc/passwd", "../app.py", "src/../../x.py", None, 42):
            ok, reason = verify({"file": path, "line": 1}, lambda p: seen.append(p) or "x\n")
            self.assertEqual((ok, reason), (False, "missing_file"), path)
        self.assertEqual(seen, [])

    def test_non_string_file_content_is_withheld(self):
        ok, reason = verify({"file": "src/app.py", "line": 1}, lambda p: b"bytes\n")
        self.assertEqual((ok, reason), (False, "missing_file"))

    def test_production_shape_aliases(self):
        ok, _ = verify({"path": "src/app.py", "line": 5, "quote": "return os.environ['X']"}, gate_read)
        self.assertTrue(ok)
        ok, reason = verify({"path": "src/app.py", "line": 4, "quote": "return os.environ['X']"}, gate_read)
        self.assertEqual((ok, reason), (False, "quote_mismatch"))

    def test_malformed_finding_is_withheld(self):
        ok, reason = verify("not-a-finding", gate_read)
        self.assertEqual((ok, reason), (False, "malformed"))
        ok, reason = verify(None, gate_read)
        self.assertEqual((ok, reason), (False, "malformed"))

    def test_gate_partitions_counts_and_lists_withheld(self):
        findings = [
            {"file": "src/app.py", "line": 4},
            {"file": "src/app.py", "line": 99},
            {"file": "src/gone.py", "line": 1},
            {"file": "src/app.py", "line": 5, "evidence": "nope"},
        ]
        gated = gate_findings(findings, "abc123", gate_read)
        self.assertEqual(gated["verified"], [findings[0]])
        self.assertEqual(gated["refusal_count"], 3)
        self.assertEqual(gated["total"], 4)
        self.assertAlmostEqual(gated["refusal_rate"], 0.75)
        self.assertEqual(gated["head"], "abc123")
        reasons = [w["reason"] for w in gated["withheld"]]
        self.assertEqual(reasons, ["bad_line", "missing_file", "quote_mismatch"])
        self.assertEqual(len(gated["rationale"]), 3)
        for entry, line in zip(gated["withheld"], gated["rationale"]):
            self.assertIn("unverified, withheld", line)
            self.assertIn(str(entry["line"]), line)
            self.assertIn(entry["reason"], line)

    def test_gate_on_empty_findings_has_zero_rate(self):
        gated = gate_findings([], "abc123", gate_read)
        self.assertEqual((gated["verified"], gated["withheld"]), ([], []))
        self.assertEqual((gated["refusal_count"], gated["refusal_rate"]), (0, 0.0))
        self.assertEqual(gated["rationale"], [])

    def test_gate_does_not_mutate_input_findings(self):
        findings = [{"file": "src/app.py", "line": 99}]
        gate_findings(findings, "abc123", gate_read)
        self.assertEqual(findings, [{"file": "src/app.py", "line": 99}])


if __name__ == "__main__":
    unittest.main()
