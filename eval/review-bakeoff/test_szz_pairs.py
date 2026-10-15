#!/usr/bin/env python3
"""Tests for szz_pairs.py. Each case builds a throwaway repo and asserts on the
mined pairs, not on printed text. Run: python3 -m unittest -v test_szz_pairs.py
"""
import os
import subprocess
import tempfile
import unittest

import szz_pairs

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class Repo:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = self._tmp.name
        self.git("init", "-q", "-b", "main")
        self.clock = 1_700_000_000
        self.head = None

    def git(self, *args, input=None):
        return subprocess.run(["git", "-C", self.path, *args], input=input,
                              check=True, capture_output=True, env=ENV).stdout

    def commit(self, subject, files, days_after_start=0):
        for name, body in files.items():
            full = os.path.join(self.path, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(body)
            self.git("add", name)
        when = self.clock + int(days_after_start * 86400)
        tree = self.git("write-tree").decode().strip()
        message = subject.encode("utf-8")
        # Dates travel in-band: agent git wrappers may drop GIT_*_DATE env.
        stream = (
            f"commit refs/heads/main\n"
            f"author t <t@example.invalid> {when} +0000\n"
            f"committer t <t@example.invalid> {when} +0000\n"
            f"data {len(message)}\n"
        ).encode() + message + b"\n"
        if self.head is not None:
            stream += f"from {self.head}\n".encode()
        # Import the staged tree, including any rename staged by a test.
        stream += f'M 040000 {tree} ""\n\n'.encode()
        self.git("fast-import", "--quiet", input=stream)
        actual = int(self.git("show", "-s", "--format=%ct", "HEAD"))
        if actual != when:
            raise AssertionError(
                f"SZZ fixture commit {subject!r}: expected %ct={when}, got {actual}"
            )
        self.head = self.git("rev-parse", "HEAD").decode().strip()

    def close(self):
        self._tmp.cleanup()


class SzzPairs(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def mine(self, **kw):
        return szz_pairs.mine(self.repo.path, "main", kw.get("lag", 14.0), kw.get("cap", 800))

    def test_fix_blames_the_introducing_pr(self):
        r = self.repo
        r.commit("feat(a): add thing (#1)", {"src/a.ts": "one\ntwo\nthree\n"}, 0)
        r.commit("feat(b): unrelated (#2)", {"src/b.ts": "x\n"}, 1)
        r.commit("fix(a): correct two (#3)", {"src/a.ts": "one\nTWO\nthree\n"}, 2)
        pairs = self.mine()
        self.assertEqual([(p["introducing_pr"], p["fix_pr"]) for p in pairs], [(1, 3)])
        self.assertEqual(pairs[0]["src_lines"], 1)
        self.assertEqual(pairs[0]["category"], "functional")

    def test_non_fix_commits_are_never_fix_side(self):
        r = self.repo
        r.commit("feat(a): add thing (#1)", {"src/a.ts": "one\ntwo\n"}, 0)
        r.commit("refactor(a): rename (#2)", {"src/a.ts": "one\nTWO\n"}, 1)
        self.assertEqual(self.mine(), [])

    def test_test_only_fix_is_ci_break(self):
        r = self.repo
        r.commit("feat(a): add (#1)", {"test/a.test.ts": "expect(1)\n"}, 0)
        r.commit("fix(a): repair test (#2)", {"test/a.test.ts": "expect(2)\n"}, 1)
        pairs = self.mine()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["category"], "ci-break")
        self.assertEqual(pairs[0]["src_lines"], 0)

    def test_lag_beyond_window_is_dropped(self):
        r = self.repo
        r.commit("feat(a): add (#1)", {"src/a.ts": "one\n"}, 0)
        r.commit("fix(a): late (#2)", {"src/a.ts": "ONE\n"}, 30)
        self.assertEqual(self.mine(lag=14.0), [])
        self.assertEqual(len(self.mine(lag=60.0)), 1)

    def test_oversized_introducing_pr_is_dropped(self):
        r = self.repo
        r.commit("feat(a): big (#1)", {"src/a.ts": "".join(f"l{i}\n" for i in range(50))}, 0)
        r.commit("fix(a): small (#2)", {"src/a.ts": "".join(f"l{i}\n" if i else "L0\n" for i in range(50))}, 1)
        self.assertEqual(len(self.mine(cap=100)), 1)
        self.assertEqual(self.mine(cap=10), [])

    def test_self_fix_inside_one_pr_is_not_a_pair(self):
        r = self.repo
        r.commit("fix(a): first (#1)", {"src/a.ts": "one\n"}, 0)
        r.commit("fix(a): second (#1)", {"src/a.ts": "ONE\n"}, 1)
        self.assertEqual(self.mine(), [])

    def test_deleted_sql_comment_is_not_read_as_a_file_header(self):
        # "-- note" removed renders as "--- note"; it must not reset the path.
        r = self.repo
        r.commit("feat(db): add (#1)", {"db/m.sql": "-- note\nselect 1;\nselect 2;\n"}, 0)
        r.commit("fix(db): drop note and fix (#2)", {"db/m.sql": "select 1;\nselect 3;\n"}, 1)
        ranges = szz_pairs.deleted_ranges(r.path, subprocess.run(
            ["git", "-C", r.path, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
        self.assertIn("db/m.sql", ranges)
        self.assertEqual(len(self.mine()), 1)

    def test_location_is_the_line_number_in_the_introducing_tree(self):
        # A later PR prepends two lines, shifting the defect down; the reviewer of
        # the introducing PR saw it at line 2, so that is what must be recorded.
        r = self.repo
        r.commit("feat(a): add (#1)", {"src/a.ts": "one\nBAD\nthree\n"}, 0)
        r.commit("chore(a): header (#2)", {"src/a.ts": "h1\nh2\none\nBAD\nthree\n"}, 1)
        r.commit("fix(a): repair (#3)", {"src/a.ts": "h1\nh2\none\nGOOD\nthree\n"}, 2)
        [pair] = self.mine()
        self.assertEqual((pair["introducing_pr"], pair["fix_pr"]), (1, 3))
        self.assertEqual(pair["locations"], [{"file": "src/a.ts", "line": 2, "kind": "src"}])

    def test_location_follows_a_rename_to_the_path_at_the_introducing_commit(self):
        r = self.repo
        r.commit("feat(a): add (#1)", {"src/old.ts": "one\nBAD\nthree\n"}, 0)
        r.git("mv", "src/old.ts", "src/new.ts")
        r.commit("refactor(a): rename (#2)", {"src/new.ts": "one\nBAD\nthree\n"}, 1)
        r.commit("fix(a): repair (#3)", {"src/new.ts": "one\nGOOD\nthree\n"}, 2)
        [pair] = self.mine()
        self.assertEqual(pair["introducing_pr"], 1)
        self.assertEqual(pair["locations"][0]["file"], "src/old.ts")

    def test_shallow_clone_is_refused(self):
        r = self.repo
        r.commit("feat(a): add (#1)", {"src/a.ts": "one\n"}, 0)
        r.commit("fix(a): fix (#2)", {"src/a.ts": "ONE\n"}, 1)
        shallow = tempfile.mkdtemp()
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + r.path, shallow],
                       check=True, capture_output=True, env=ENV)
        self.assertEqual(szz_pairs.main(["--repo", shallow, "--ref", "origin/main"]), 2)


if __name__ == "__main__":
    unittest.main()
