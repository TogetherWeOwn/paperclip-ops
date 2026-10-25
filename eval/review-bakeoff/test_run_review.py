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
        base = {"file": "a.ts", "line": 2, "severity": "major", "title": "t", "rationale": "r",
                "evidence": "two"}
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
        # Reasoning need not repeat the cited code; the explicit evidence
        # matches line 2, so rationale "three" does not invalidate it.
        review = self.review(self.finding(line=2, rationale="three"))
        gate = rr.publish_gated_review(review, "deadbeef", self.read)
        self.assertEqual(len(review["findings"]), 1)
        self.assertEqual(gate["refusal_count"], 0)


class RunCaseEvidence(unittest.TestCase):
    """Exercise publication with real Git; only the model transport is faked."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text("first\nold\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "src/app.py").write_text("first\nsecond\n")
        for name, text in {"vt.txt": "first\vsecond\n", "ls.txt": "first second\n",
                           "cr.txt": "first\rsecond\n", "crlf.txt": "first\r\nsecond\r\n",
                           "empty.txt": "", "last.txt": "last"}.items():
            (self.repo / name).write_bytes(text.encode())
        self.git("add", ".")
        self.git("commit", "-q", "-m", "reviewed change")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.case = {"id": "fixture", "kind": "real", "head_sha": self.head,
                     "title": "reviewed change", "body": ""}

    def git(self, *args):
        import subprocess

        return subprocess.run(["git", "-c", "user.name=fixture", "-c",
                               "user.email=fixture@example.invalid", *args], cwd=self.repo,
                              capture_output=True, text=True, check=True).stdout

    def finding(self, **kw):
        return {"file": "src/app.py", "line": 2, "severity": "major", "title": "defect",
                "rationale": "The cited operation is incorrect.", "evidence": "second", **kw}

    def run_model(self, findings=None, raw=None, case=None):
        import os
        import subprocess
        from pathlib import Path
        from unittest.mock import patch

        real_run = subprocess.run
        if raw is None:
            raw = json.dumps({**GOOD, "findings": findings})

        def transport(args, **kw):
            if args[0] != "codex":
                return real_run(args, **kw)
            Path(args[args.index("-o") + 1]).write_text(raw)
            return subprocess.CompletedProcess(args, 0, stdout='{"type":"turn.completed",'
                                               '"usage":{"output_tokens":12}}\n', stderr="")

        with patch.dict(os.environ, {"BAKEOFF_FIXTURE_KEY": "offline-fixture-not-a-key"}), \
                patch.object(rr.subprocess, "run", side_effect=transport):
            return rr.run_case(str(self.repo), case or self.case, "fixture-model", "medium",
                               "BAKEOFF_FIXTURE_KEY", 5)

    def test_schema_accepts_and_requires_quoted_evidence(self):
        from pathlib import Path

        schema = json.loads((Path(rr.HERE) / "output_schema.json").read_text())
        item = schema["properties"]["findings"]["items"]
        self.assertIn("evidence", item["required"])
        self.assertEqual(item["properties"]["evidence"]["type"], "string")
        self.assertEqual(set(self.finding()), set(item["properties"]))

    def test_run_case_strips_invalid_citations_and_returns_refusal_metrics(self):
        good = self.finding()
        findings = [good, self.finding(evidence="invented_call()"),
                    self.finding(file="missing.py"), self.finding(line=99),
                    self.finding(file="src", line=1, evidence="tree HEAD:src"),
                    self.finding(file="src\\app.py"), self.finding(file=" src/app.py ")]
        result = self.run_model(findings)
        self.assertIsNone(result["parse_error"])
        self.assertEqual(result["review"]["findings"], [good])
        self.assertEqual(result["evidence_gate"]["head"], self.head)
        self.assertEqual(result["evidence_gate"]["total"], 7)
        self.assertEqual(result["evidence_gate"]["refusal_count"], 6)
        self.assertEqual(result["evidence_gate"]["refusal_rate"], 0.857)
        self.assertEqual([w["reason"] for w in result["evidence_gate"]["withheld"]],
                         ["quote_mismatch", "missing_file", "bad_line", "missing_file",
                          "missing_file", "missing_file"])
        self.assertEqual(len(result["evidence_gate"]["rationale"]), 6)
        self.assertTrue(all("unverified, withheld" in r
                            for r in result["evidence_gate"]["rationale"]))

    def test_missing_or_blank_evidence_is_withheld_not_inferred_from_rationale(self):
        missing = self.finding()
        del missing["evidence"]
        findings = [missing] + [self.finding(evidence=value, rationale="second")
                                for value in ("", " \t", None, 42)]
        result = self.run_model(findings)
        self.assertEqual(result["review"]["findings"], [])
        self.assertEqual(result["evidence_gate"]["refusal_count"], 5)
        self.assertEqual([w["reason"] for w in result["evidence_gate"]["withheld"]],
                         ["missing_evidence"] * 5)

    def test_only_lf_creates_physical_source_lines(self):
        good, bad = [], []
        for name, separator in (("vt.txt", "\v"), ("ls.txt", " "), ("cr.txt", "\r")):
            text = f"first{separator}second\n"
            self.assertEqual(rr.read_git_file(str(self.repo), name), text)
            self.assertEqual(rr.number_lines(text), f"    1  first{separator}second")
            good.append(self.finding(file=name, line=1, evidence="first second"))
            bad.append(self.finding(file=name, line=2))
        self.assertEqual(rr.read_git_file(str(self.repo), "crlf.txt"), "first\r\nsecond\r\n")
        good.append(self.finding(file="crlf.txt", line=2))
        bad.extend([self.finding(file="crlf.txt", line=3),
                    self.finding(file="empty.txt", line=1),
                    self.finding(file="last.txt", line=2), self.finding(line=3)])
        result = self.run_model(good + bad)
        self.assertEqual(result["review"]["findings"], good)
        self.assertEqual(result["evidence_gate"]["refusal_count"], 7)
        self.assertEqual([w["reason"] for w in result["evidence_gate"]["withheld"]],
                         ["bad_line"] * 7)

    def test_directories_are_not_readable_file_blobs(self):
        self.assertIsNone(rr.read_git_file(str(self.repo), "src"))

    def test_planted_case_is_verified_against_its_new_committed_head(self):
        from pathlib import Path

        patch = Path(self.tmp.name) / "plant.patch"
        patch.write_text("--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n first\n-second\n+planted\n")
        planted = {**self.case, "kind": "planted", "plant_patch": str(patch)}
        good = self.finding(evidence="planted")
        result = self.run_model([good, self.finding()], case=planted)
        self.assertEqual(result["review"]["findings"], [good])
        self.assertNotEqual(result["evidence_gate"]["head"], self.head)
        self.assertEqual(len(result["evidence_gate"]["head"]), 40)
        self.assertEqual(result["evidence_gate"]["refusal_count"], 1)
        self.assertEqual(result["evidence_gate"]["withheld"][0]["reason"], "quote_mismatch")

    def test_parse_failure_has_no_gate_and_preserves_raw_response(self):
        result = self.run_model(raw="not a review")
        self.assertIsNone(result["review"])
        self.assertIsNone(result["evidence_gate"])
        self.assertIn("not JSON", result["parse_error"])
        self.assertEqual(result["raw_final"], "not a review")
        self.assertEqual(result["usage"], {"output_tokens": 12})


if __name__ == "__main__":
    unittest.main()
