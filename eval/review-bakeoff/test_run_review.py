#!/usr/bin/env python3
"""Tests for the pure parts of run_review.py. Model calls are never made here.
Run: python3 -m unittest -v test_run_review.py
"""
import json
import unittest

import run_review as rr

GOOD = {"score": 3, "summary": "s",
        "findings": [{"file": "a.ts", "line": 4, "severity": "major", "title": "t", "rationale": "r"}],
        "coverage": {"files_reviewed": ["a.ts"], "complete": True, "limitations": ""}}


class ParseReview(unittest.TestCase):
    def test_plain_json(self):
        obj, err = rr.parse_review(json.dumps(GOOD))
        self.assertIsNone(err)
        self.assertEqual(obj["score"], 3)

    def test_fenced_json(self):
        obj, err = rr.parse_review("```json\n" + json.dumps(GOOD) + "\n```")
        self.assertIsNone(err)
        self.assertEqual(len(obj["findings"]), 1)

    def test_json_wrapped_in_prose(self):
        obj, err = rr.parse_review("Here is my review:\n" + json.dumps(GOOD) + "\nDone.")
        self.assertIsNone(err)
        self.assertEqual(obj["score"], 3)

    def test_rejects_non_json_and_non_objects(self):
        self.assertIsNotNone(rr.parse_review("no braces here")[1])
        self.assertIsNotNone(rr.parse_review("[1, 2]")[1])

    def test_score_must_be_an_int_in_range_and_not_a_bool(self):
        for bad in (6, -1, "3", True, 2.5, None):
            self.assertIsNotNone(rr.parse_review(json.dumps({**GOOD, "score": bad}))[1], bad)

    def test_malformed_findings_are_rejected(self):
        bad_sev = {**GOOD, "findings": [{**GOOD["findings"][0], "severity": "high"}]}
        bad_line = {**GOOD, "findings": [{**GOOD["findings"][0], "line": "4"}]}
        self.assertIsNotNone(rr.parse_review(json.dumps(bad_sev))[1])
        self.assertIsNotNone(rr.parse_review(json.dumps(bad_line))[1])
        self.assertIsNotNone(rr.parse_review(json.dumps({**GOOD, "findings": "none"}))[1])

    def test_empty_findings_is_a_valid_clean_review(self):
        self.assertIsNone(rr.parse_review(json.dumps({**GOOD, "score": 5, "findings": []}))[1])


class ParseEvents(unittest.TestCase):
    def test_usage_is_summed_across_turns_and_junk_lines_are_skipped(self):
        out = "\n".join([
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}',
            "not json",
            '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1,"cached_input_tokens":3}}',
        ])
        ev = rr.parse_events(out)
        self.assertEqual(ev["usage"], {"input_tokens": 15, "output_tokens": 3, "cached_input_tokens": 3})

    def test_failures_are_collected(self):
        ev = rr.parse_events('{"type":"turn.failed","error":{"message":"429"}}')
        self.assertEqual(len(ev["errors"]), 1)


class Prompt(unittest.TestCase):
    TEMPLATE = "T={title}\nB={body}\nF={files}\nD={diff}\nC={context}"

    def test_braces_in_a_diff_survive(self):
        p = rr.build_prompt(self.TEMPLATE, "t", "b", ["a.ts"], "+const x = {a: 1};\n+{title}")
        self.assertIn("{a: 1}", p)
        self.assertIn("D=+const x", p)

    def test_values_are_not_rescanned_for_placeholders(self):
        # A title that spells another placeholder must stay literal; with a
        # sequential str.replace chain the body or diff would be pasted into it.
        p = rr.build_prompt(self.TEMPLATE, "see {diff}", "b {title}", [], "DIFFBODY")
        self.assertIn("T=see {diff}", p)
        self.assertIn("B=b {title}", p)

    def test_literal_braces_in_the_template_are_left_alone(self):
        p = rr.build_prompt('schema {"a": 1} {title}', "t", "b", [], "d")
        self.assertEqual(p, 'schema {"a": 1} t')

    def test_empty_body_is_labelled(self):
        self.assertIn("B=(none)", rr.build_prompt(self.TEMPLATE, "t", "", [], "d"))

    def test_oversized_diff_is_truncated_with_a_notice(self):
        p = rr.build_prompt(self.TEMPLATE, "t", "b", [], "x" * (rr.MAX_DIFF_CHARS + 10))
        self.assertIn("diff truncated", p)
        self.assertLess(len(p), rr.MAX_DIFF_CHARS + 500)


class ReadGitFile(unittest.TestCase):
    def setUp(self):
        import subprocess, tempfile, os
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        subprocess.run(["git", "-C", self.dir, "init", "-q"], check=True)
        with open(os.path.join(self.dir, "t.txt"), "w") as fh:
            fh.write("hello\n")
        with open(os.path.join(self.dir, "b.bin"), "wb") as fh:
            fh.write(b"ab\0cd")
        subprocess.run(["git", "-C", self.dir, "add", "."], check=True)
        subprocess.run(["git", "-C", self.dir, "commit", "-q", "-m", "x"], check=True, env=env)

    def test_text_file_is_returned(self):
        self.assertEqual(rr.read_git_file(self.dir, "t.txt"), "hello\n")

    def test_binary_and_missing_files_are_none(self):
        self.assertIsNone(rr.read_git_file(self.dir, "b.bin"))
        self.assertIsNone(rr.read_git_file(self.dir, "nope.txt"))


class Context(unittest.TestCase):
    def test_lines_are_numbered_from_one(self):
        ctx = rr.build_context(lambda p: "a\nb\n", ["x.ts"])
        self.assertIn("--- x.ts ---", ctx)
        self.assertIn("    1  a", ctx)
        self.assertIn("    2  b", ctx)

    def test_unreadable_and_deleted_files_are_skipped(self):
        ctx = rr.build_context(lambda p: None if p == "gone.ts" else "ok", ["gone.ts", "here.ts"])
        self.assertNotIn("gone.ts", ctx)
        self.assertIn("here.ts", ctx)

    def test_a_large_file_is_truncated_with_a_notice(self):
        ctx = rr.build_context(lambda p: "x" * (rr.MAX_FILE_CHARS + 50), ["big.ts"])
        self.assertIn("file truncated", ctx)

    def test_total_budget_omits_later_files_but_names_them(self):
        big = "y" * (rr.MAX_FILE_CHARS - 10)
        ctx = rr.build_context(lambda p: big, [f"f{i}.ts" for i in range(5)])
        self.assertIn("omitted", ctx)
        self.assertIn("f4.ts", ctx)
        self.assertLess(len(ctx), rr.MAX_CONTEXT_CHARS + 2 * rr.MAX_FILE_CHARS)

    def test_empty_context_is_labelled(self):
        self.assertIn("C=(none)", rr.build_prompt(Prompt.TEMPLATE, "t", "b", [], "d", ""))


class CodexArgs(unittest.TestCase):
    def test_model_effort_and_proxy_are_set_and_no_key_is_in_argv(self):
        args = rr.codex_args("gpt-6.1-sol", "high", "/s.json", "/o.json")
        joined = " ".join(args)
        self.assertIn("-m gpt-6.1-sol", joined)
        self.assertIn('model_reasoning_effort="high"', joined)
        self.assertIn("http://cliproxy:8317/v1", joined)
        self.assertIn('wire_api="responses"', joined)
        self.assertIn("-s read-only", joined)
        self.assertNotIn("Bearer", joined)
        self.assertEqual(args[-1], "-")  # prompt on stdin, never argv

    def test_every_pinned_candidate_has_a_served_id_and_a_lane(self):
        self.assertEqual(len(rr.CANDIDATES), 7)
        for label, (served, lane) in rr.CANDIDATES.items():
            self.assertTrue(served and lane, label)


class PublishGatedReview(unittest.TestCase):
    FILES = {"a.ts": "one\ntwo\nthree\n"}

    def read(self, path):
        return self.FILES.get(path)

    def review(self, *findings):
        return {"score": 3, "summary": "s", "findings": list(findings),
                "coverage": {"files_reviewed": ["a.ts"], "complete": True, "limitations": ""}}

    def finding(self, **kw):
        base = {"file": "a.ts", "line": 2, "severity": "major", "title": "t", "rationale": "r"}
        base.update(kw)
        return base

    def test_verified_findings_publish_and_refusal_count_is_zero(self):
        review = self.review(self.finding())
        gate = rr.publish_gated_review(review, "deadbeef", self.read)
        self.assertEqual(review["findings"], [self.finding()])
        self.assertEqual(gate["refusal_count"], 0)
        self.assertEqual(gate["refusal_rate"], 0.0)
        self.assertEqual(gate["withheld"], [])
        self.assertEqual(gate["head"], "deadbeef")

    def test_unverifiable_findings_are_stripped_counted_and_listed(self):
        good = self.finding()
        bad_line = self.finding(line=99)
        missing = self.finding(file="gone.ts")
        review = self.review(good, bad_line, missing)
        gate = rr.publish_gated_review(review, "deadbeef", self.read)
        self.assertEqual(review["findings"], [good])
        self.assertEqual(gate["total"], 3)
        self.assertEqual(gate["refusal_count"], 2)
        self.assertAlmostEqual(gate["refusal_rate"], 0.667, places=3)
        self.assertEqual([w["reason"] for w in gate["withheld"]], ["bad_line", "missing_file"])
        self.assertEqual(len(gate["rationale"]), 2)
        for line in gate["rationale"]:
            self.assertIn("unverified, withheld", line)

    def test_rationale_text_is_never_treated_as_a_quote(self):
        # rationale "three" must not match line 2 ("two") nor rescue the finding;
        # with no evidence/quote key the anchor alone decides.
        review = self.review(self.finding(line=2, rationale="three"))
        gate = rr.publish_gated_review(review, "deadbeef", self.read)
        self.assertEqual(len(review["findings"]), 1)
        self.assertEqual(gate["refusal_count"], 0)


if __name__ == "__main__":
    unittest.main()
