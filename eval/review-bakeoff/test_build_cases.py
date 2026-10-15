#!/usr/bin/env python3
"""Tests for build_cases.py. Run: python3 -m unittest -v test_build_cases.py"""
import os
import subprocess
import tempfile
import unittest

import build_cases as bc

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


class Scrub(unittest.TestCase):
    def test_whole_lines_with_a_ticket_key_are_dropped(self):
        self.assertEqual(bc.scrub("Summary\nRefs: case_de259302\nMore"), "Summary\nMore")

    def test_no_key_survives(self):
        self.assertNotIn("TOG-", bc.scrub("fix case_f052904a thing\nok"))

    def test_lowercase_and_longer_words_are_not_keys(self):
        self.assertEqual(bc.scrub("catalog-12 and TOGETHER-1"), "catalog-12 and TOGETHER-1")


class WithRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = self._tmp.name
        subprocess.run(["git", "-C", self.repo, "init", "-q", "-b", "main"], check=True)
        with open(os.path.join(self.repo, "a.ts"), "w") as fh:
            fh.write("1\n2\n3\n4\n5\n")
        subprocess.run(["git", "-C", self.repo, "add", "a.ts"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", "feat(a): thing (#7)\n\nBody line\nRefs: case_8c5fe4cc"],
                       check=True, env=ENV)
        self.sha = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()

    def pair(self):
        return {"introducing_pr": 7, "fix_pr": 9, "introducing_sha": self.sha, "category": "functional",
                "introducing_subject": "feat(a): thing (#7)"}

    def defect(self, **kw):
        d = {"id": "d1", "file": "a.ts", "line_start": 2, "line_end": 3, "severity": "minor",
             "description": "x"}
        d.update(kw)
        return d

    def verdict(self, defects, verdict="accept"):
        return {"shuffle_rank": 0, "introducing_pr": 7, "fix_pr": 9, "verdict": verdict, "defects": defects,
                "reviewer_hint": "hint case_d02d0252"}

    def test_accepted_label_becomes_a_clean_case(self):
        cases, decisions, errors = bc.build(self.repo, [self.pair()], [self.verdict([self.defect()])], [])
        self.assertEqual(errors, [])
        [c] = cases
        self.assertEqual((c["id"], c["kind"], c["head_sha"]), ("r-0007", "real", self.sha))
        self.assertEqual(c["title"], "feat(a): thing")
        self.assertEqual(c["body"], "Body line")
        self.assertEqual(c["reviewer_hint"], "")
        self.assertEqual(len(decisions), 1)

    def test_rejections_are_recorded_but_make_no_case(self):
        rej = {"shuffle_rank": 0, "introducing_pr": 7, "fix_pr": 9, "verdict": "reject", "reason": "noise"}
        cases, decisions, errors = bc.build(self.repo, [self.pair()], [rej], [])
        self.assertEqual((cases, errors), ([], []))
        self.assertEqual(decisions[0]["reason"], "noise")

    def test_line_beyond_end_of_file_is_an_error(self):
        _, _, errors = bc.build(self.repo, [self.pair()], [self.verdict([self.defect(line_end=99)])], [])
        self.assertTrue(any("beyond end" in e for e in errors))

    def test_last_line_of_the_file_is_valid_and_one_past_it_is_not(self):
        ok = bc.build(self.repo, [self.pair()], [self.verdict([self.defect(line_start=5, line_end=5)])], [])[2]
        bad = bc.build(self.repo, [self.pair()], [self.verdict([self.defect(line_start=6, line_end=6)])], [])[2]
        self.assertEqual(ok, [])
        self.assertTrue(bad)

    def test_missing_file_is_an_error(self):
        _, _, errors = bc.build(self.repo, [self.pair()], [self.verdict([self.defect(file="nope.ts")])], [])
        self.assertTrue(any("not in" in e for e in errors))

    def test_bad_severity_and_bad_range_are_errors(self):
        _, _, e1 = bc.build(self.repo, [self.pair()], [self.verdict([self.defect(severity="high")])], [])
        _, _, e2 = bc.build(self.repo, [self.pair()], [self.verdict([self.defect(line_start=3, line_end=2)])], [])
        self.assertTrue(e1 and e2)

    def test_verdict_for_an_unknown_pair_is_an_error(self):
        v = self.verdict([self.defect()])
        v["fix_pr"] = 99
        _, _, errors = bc.build(self.repo, [self.pair()], [v], [])
        self.assertTrue(any("unknown pair" in e for e in errors))

    def test_planted_cases_are_scrubbed_and_duplicates_rejected(self):
        planted = {"id": "p-1", "kind": "planted", "title": "t case_0dd2d987", "body": "b\nRefs: case_0dd2d987",
                   "defects": [self.defect(severity="critical")]}
        cases, _, errors = bc.build(self.repo, [], [], [planted, dict(planted)])
        self.assertTrue(any("duplicate" in e for e in errors))
        self.assertNotIn("TOG-", cases[0]["title"] + cases[0]["body"])

    def test_a_key_in_an_unscrubbed_field_fails_the_build(self):
        planted = {"id": "p-1", "kind": "planted", "title": "t", "defects": [self.defect(description="see case_4ee01500")]}
        _, _, errors = bc.build(self.repo, [], [], [planted])
        self.assertTrue(any("survived" in e for e in errors))

    def test_planted_with_a_bad_severity_is_an_error(self):
        planted = {"id": "p-1", "kind": "planted", "title": "t", "defects": [self.defect(severity="severe")]}
        _, _, errors = bc.build(self.repo, [], [], [planted])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
