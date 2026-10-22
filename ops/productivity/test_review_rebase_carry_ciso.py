#!/usr/bin/env python3
"""CISO adversarial tests for the rebase 5/5 carry gate (TOG-36535).

Supplements the acceptance suite (`test_review_rebase_carry.py`, TOG-36533)
with attack-shaped cases it does not cover. Every test below asserts a fail
closed `review` decision (never `carry`) on the paired builder module
`review_rebase_carry`, pinning only `decide()`-level behavior from the
published contract:

* malformed review rows (None entries, missing keys, mixed-type ids that
  make newest-row ordering undeterminable) force `review` without raising:
  they are evidence failures, and `decide` never raises on evidence or git
  failures;
* a lying `GIT_EXTERNAL_DIFF` in the environment cannot hide a real change:
  the own-patch flags must include `--no-ext-diff`;
* a `.gitattributes` textconv driver cannot normalize a binary change away:
  the own-patch flags must include `--no-textconv`;
* resubmitting the already-reviewed head (H1 == H2) forces `review`: H2
  already has a review, so there is nothing to carry (no self-carry);
* a CRLF flip on a changed line forces `review` end to end: changed-line
  bytes (including CRs) are significant;
* a base ref that differs only by case forces `review`: ref comparison is
  exact because git refs are case-sensitive.

Hermetic: local git fixture repos only, in-memory review rows, no network, no
tokens. Runs under both `python3 -m unittest` and `python3 -m pytest`.

RED story: run against a naive stub whose `decide()` returns `carry` for
every input. Every test below must fail on assertions (not errors), proving
the cases are non-vacuous. GREEN story: run against the builder module.

Until the builder module lands, this file skips (it must stay CI-green as a
tests-only change). A present-but-broken module fails loudly: only a missing
`review_rebase_carry` module itself skips.
"""

import os
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import review_rebase_carry as rrc
except ImportError as exc:
    if getattr(exc, "name", None) == "review_rebase_carry":
        raise unittest.SkipTest(
            "review_rebase_carry is not landed yet; these adversarial tests "
            "activate when the paired builder change adds the module")
    raise

PR = 7

GIT_ENV = dict(os.environ, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true",
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
GIT_BASE = ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def run_git(repo, *args, env=None):
    return subprocess.run([*GIT_BASE, *args], cwd=repo,
                          env=env if env is not None else GIT_ENV,
                          capture_output=True)


def git(repo, *args, env=None):
    proc = run_git(repo, *args, env=env)
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed: {proc.stderr.decode()[:400]}")
    return proc.stdout.decode().strip()


class GitRepo:
    """A throwaway local git repo with deterministic identity config."""

    def __init__(self):
        self.dir = None

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rebase-carry-ciso-")
        git(self.dir, "init", "-b", "main", "-q")
        git(self.dir, "config", "user.email", "ciso@example.com")
        git(self.dir, "config", "user.name", "CISO")
        git(self.dir, "config", "commit.gpgsign", "false")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def commit(self, path, content, message):
        full = os.path.join(self.dir, path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb" if isinstance(content, bytes) else "w") as fh:
            fh.write(content)
        git(self.dir, "add", path)
        git(self.dir, "commit", "-q", "-m", message)
        return self.head()

    def head(self):
        return git(self.dir, "rev-parse", "HEAD")


def seed_pr(repo):
    repo.commit("app.py", "print('base')\n", "base")
    repo.commit("base.txt", "v1\n", "base data")
    old_base = repo.head()
    git(repo.dir, "checkout", "-q", "-b", "pr")
    a = repo.commit("feature.py", "def f():\n    return 1\n",
                    "A: add feature")
    b = repo.commit("feature.py", "def f():\n    return 2\n",
                    "B: tweak feature")
    return old_base, repo.head(), a, b


def advance_main(repo):
    git(repo.dir, "checkout", "-q", "main")
    repo.commit("base.txt", "v2\n", "main moves")
    repo.commit("main_only.py", "x = 1\n", "main adds file")
    return repo.head()


def replay_onto(test, repo, new_base, *shas):
    git(repo.dir, "checkout", "-q", "-b", "pr2", new_base)
    for sha in shas:
        proc = run_git(repo.dir, "cherry-pick", sha)
        test.assertEqual(proc.returncode, 0, proc.stderr.decode()[:400])


def review_row(row_id, pr, head, title="5/5", status="completed",
               conclusion="success"):
    return {"id": row_id, "pr": pr, "head_sha": head, "status": status,
            "conclusion": conclusion, "title": title}


def base(ref, sha):
    return {"ref": ref, "sha": sha}


def write_script(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write(body)
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class MalformedReviewRows(unittest.TestCase):
    def test_malformed_rows_force_review_without_raising(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            h2 = repo.head()
            # Sanity: with a well-formed 5/5 row this scene carries, so any
            # `review` below is forced by the malformed rows alone.
            action, _ = rrc.decide(
                repo.dir, PR, h1, h2, base("main", old_base),
                base("main", new_base), [review_row(1, PR, h1)])
            self.assertEqual(action, "carry")
            # Only cases where NO unambiguous genuine 5/5 exists are pinned:
            # tolerating ignorable cruft next to a genuine row is not a
            # vulnerability, but undeterminable ordering must fail closed.
            mixed_ids_a = [review_row("not-an-int", PR, h1),
                           review_row(2, PR, h1, title="4/5")]
            mixed_ids_b = list(reversed(mixed_ids_a))
            malformed = [
                [None],
                [{"pr": PR}],
                mixed_ids_a,
                mixed_ids_b,
            ]
            for rows in malformed:
                with self.subTest(rows=rows):
                    action, detail = rrc.decide(
                        repo.dir, PR, h1, h2, base("main", old_base),
                        base("main", new_base), rows)
                    self.assertEqual(action, "review")
                    self.assertTrue(detail["reason"])


class LyingDiffDrivers(unittest.TestCase):
    def test_external_diff_driver_cannot_hide_a_change(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            repo.commit("feature.py", "def f():\n    return 3\n",
                        "real change after rebase")
            h2 = repo.head()
            liar = write_script(repo.dir, "lie.sh",
                                "#!/bin/sh\nexit 0\n")
            # Oracle: under the lying driver a plain `git diff` reports
            # nothing, which is the attack (an always-equal diff).
            lying_env = dict(GIT_ENV, GIT_EXTERNAL_DIFF=liar)
            oracle = run_git(repo.dir, "diff",
                             git(repo.dir, "merge-base", new_base, h2),
                             h2, "--", env=lying_env)
            self.assertEqual(oracle.returncode, 0)
            self.assertEqual(oracle.stdout, b"",
                             "fixture needs a git that honors GIT_EXTERNAL_DIFF")
            old_env = os.environ.get("GIT_EXTERNAL_DIFF")
            os.environ["GIT_EXTERNAL_DIFF"] = liar
            try:
                action, detail = rrc.decide(
                    repo.dir, PR, h1, h2, base("main", old_base),
                    base("main", new_base), [review_row(1, PR, h1)])
            finally:
                if old_env is None:
                    os.environ.pop("GIT_EXTERNAL_DIFF", None)
                else:
                    os.environ["GIT_EXTERNAL_DIFF"] = old_env
            self.assertEqual(action, "review")
            self.assertTrue(detail["reason"])

    def test_textconv_attribute_cannot_normalize_a_binary_change(self):
        with GitRepo() as repo:
            constant = write_script(
                repo.dir, "constant.sh",
                "#!/bin/sh\necho CISO-TEXTCONV-CONSTANT\n")
            repo.commit("app.py", "x\n", "base")
            repo.commit(".gitattributes", "*.bin diff=lyingtext\n",
                        "attributes assigns the driver")
            old_base = repo.head()
            git(repo.dir, "config", "diff.lyingtext.textconv", constant)
            git(repo.dir, "checkout", "-q", "-b", "pr")
            repo.commit("blob.bin", b"\x00\x01\x02payload-v1\xff",
                        "A: add binary")
            h1 = repo.head()
            git(repo.dir, "checkout", "-q", "main")
            repo.commit("app.py", "x2\n", "main moves")
            new_base = repo.head()
            replay_onto(self, repo, new_base, h1)
            repo.commit("blob.bin", b"\x00\x01\x02payload-v2\xff",
                        "flip one payload byte")
            h2 = repo.head()
            # Oracle: a converting diff shows the constant instead of the
            # real bytes, which is the attack (two different blobs diffing
            # equal). This must hold or the fixture is vacuous.
            oracle = run_git(repo.dir, "diff",
                             git(repo.dir, "merge-base", new_base, h2),
                             h2, "--")
            self.assertEqual(oracle.returncode, 0)
            self.assertIn(b"CISO-TEXTCONV-CONSTANT", oracle.stdout,
                          "fixture needs a git that applies textconv here")
            # The carry rule runs with --no-textconv and must see raw bytes.
            own = rrc.own_patch(repo.dir, new_base, h2)
            self.assertIn(b"GIT binary patch", own)
            action, detail = rrc.decide(
                repo.dir, PR, h1, h2, base("main", old_base),
                base("main", new_base), [review_row(1, PR, h1)])
            self.assertEqual(action, "review")
            self.assertTrue(detail["reason"])


class HeadAndBaseIdentity(unittest.TestCase):
    def test_same_head_resubmission_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, _, _ = seed_pr(repo)
            action, detail = rrc.decide(
                repo.dir, PR, h1, h1, base("main", old_base),
                base("main", old_base), [review_row(1, PR, h1)])
            self.assertEqual(action, "review")
            self.assertTrue(detail["reason"])

    def test_base_ref_case_difference_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            h2 = repo.head()
            action, detail = rrc.decide(
                repo.dir, PR, h1, h2, base("main", old_base),
                base("Main", new_base), [review_row(1, PR, h1)])
            self.assertEqual(action, "review")
            self.assertTrue(detail["reason"])


class ChangedLineBytes(unittest.TestCase):
    def test_crlf_flip_on_changed_line_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            repo.commit("feature.py", b"def f():\n    return 2\r\n",
                        "crlf flip on a changed line")
            h2 = repo.head()
            self.assertNotEqual(
                rrc.digest(rrc.own_patch(repo.dir, old_base, h1)),
                rrc.digest(rrc.own_patch(repo.dir, new_base, h2)))
            action, detail = rrc.decide(
                repo.dir, PR, h1, h2, base("main", old_base),
                base("main", new_base), [review_row(1, PR, h1)])
            self.assertEqual(action, "review")
            self.assertTrue(detail["reason"])


if __name__ == "__main__":
    unittest.main()
