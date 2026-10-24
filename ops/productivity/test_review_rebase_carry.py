#!/usr/bin/env python3
"""Acceptance tests: the review gate carries 5/5 only across empty
range-diff rebase landings and re-reviews only the PR's own delta.

Baseline behavior under test (module ``review_rebase_carry``, stdlib only):

* Own-patch digest. For a PR head H on base B, the normalized patch of
  ``merge-base(B, H)..H``. The normalization is exactly the merge-group
  carry rule: ``git diff --no-renames --binary --full-index --unified=0``
  fed through ``normalized_patch()`` + ``digest()``. Deliberately not
  ``git patch-id``, which ignores significant whitespace.
* Rebase carry. The decision is ``carry`` only when every condition
  holds: the base branch is unchanged, the new head H2 has no review
  row, the newest review row on the previous head H1 is a successful
  5/5, any supplied compare data is complete, and
  ``digest(own-patch H1) == digest(own-patch H2)``. Anything else fails
  closed to a full ``review``. A carry ships proof fields
  ``{old_head, new_head, old_base, new_base, digest}`` plus the carried
  summary line.
* Diff-only re-review. When carry does not apply, the re-review file
  list is exactly the files whose normalized own-patch changed between
  H1 and H2, plus the files named in prior blocking findings. It is NOT
  the GitHub compare ``H1...H2``, which after a rebase includes every
  commit the base branch gained.
* Idempotency. Deciding twice for the same (repo, PR, H2) yields the
  same proof under the same idempotency key, so at most one carried
  check can be published. A newer head H3 invalidates the carry on H2.
* No secrets. No test needs secrets, and the module under test imports
  nothing that reads the reviewer App credential.

Contract the paired builder change implements (this docstring is the
interface; the tests pin it, the builder must not edit these tests):

* ``GitError(Exception)``: raised for git/evidence failures by
  ``merge_base``, ``own_patch`` and ``delta_files``. ``decide`` never
  raises it: evidence and git failures become ``('review', ...)``.
* ``normalized_patch(patch: bytes) -> dict[bytes, bytes]``: file bodies
  keyed by their ``diff --git`` header line. Skips ``@@`` hunk headers
  and context lines and the blob ids in ``index`` lines (mode bits are
  kept). Preserves changed-line bytes including whitespace and CRs,
  file names, additions/deletions, modes, binary payloads and EOF
  markers. Keeps both records of a delete+add (file-type transition)
  instead of collapsing them. Raises on a garbage preamble or an
  invalid index header.
* ``digest(patch: bytes) -> str``: lowercase hex sha256 over the
  length-prefixed, name-sorted ``(header, body)`` pairs.
* ``merge_base(repo, base_sha, head_sha) -> str``: the single
  merge-base, or ``GitError`` when there is none or more than one.
* ``own_patch(repo, base_sha, head_sha) -> bytes``: raw diff bytes of
  ``merge-base(base_sha, head_sha)..head_sha`` with the flags above
  plus ``--no-ext-diff --no-textconv --no-color --src-prefix=a/
  --dst-prefix=b/``. Raises ``GitError`` on any git failure.
* Review rows are mappings with at least ``id`` (int ordering, higher
  is newer), ``pr`` (int), ``head_sha``, ``status``, ``conclusion``
  (or None) and ``title`` (score text such as ``5/5``).
* ``latest_review(reviews, number, head)``: newest row scoped to
  ``pr == number`` and ``head_sha == head``, or None when there is no
  such row, when the newest row cannot be determined unambiguously (a
  tie for the greatest id with conflicting verdicts), or when the
  newest row is not ``completed`` + ``success`` + ``5/5``.
* ``carry_summary(old_head, new_head, digest) -> str``: exactly
  ``5/5 carried: identical reviewed patch after rebase (<H1> -> <H2>,
  digest <digest>)``.
* ``idempotency_key(repo, number, head) -> str``: stable per
  (repo, PR, head); the repo comparison is case-insensitive.
* ``carry_applies(proof, head) -> bool``: true only when
  ``proof['new_head'] == head``.
* ``decide(repo, number, old_head, new_head, old_base, new_base,
  reviews, compare=None)``: ``old_base``/``new_base`` are
  ``{'ref': branch, 'sha': base}`` mappings; ``compare`` is None or a
  mapping with ``truncated`` (bool), ``total_commits``/``commits`` and
  ``total_files``/``files``. Returns ``('carry', proof)`` with
  ``{old_head, new_head, old_base, new_base, digest, summary}`` or
  ``('review', {'reason': <non-empty str>})``. Never raises on
  evidence or git failures.
* ``delta_files(repo, old_head, new_head, old_base, new_base,
  findings=())``: sorted repo-relative paths whose normalized
  own-patch file body was added, removed or changed between H1 and H2,
  union the ``path`` of every mapping in ``findings``. Raises
  ``GitError`` on any git failure.

Hermetic: local git fixture repos only (built in temp dirs), in-memory
review rows, no network, no tokens, no App credential. Runs under both
``python3 -m unittest`` (repo CI) and ``python3 -m pytest``.

RED story: run against a naive stub that returns ``carry`` for every
input and derives the delta from the ``H1...H2`` compare. Every
fail-closed, delta-exactness and proof test below must fail on
assertions (not errors). GREEN story: run against the paired builder
change, which must satisfy every test without editing this file.

Until the builder module lands, this file skips via ``setUpModule`` (it
must stay CI-green as a tests-only change, including under the repo's
by-module-name unittest invocation). A present-but-broken module fails
loudly: only a missing ``review_rebase_carry`` module itself skips.
"""

import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PR = 7

# Git repository-location variables override the cwd of every git child, so
# a leaked GIT_DIR/GIT_WORK_TREE would aim fixture commands (and the helper
# under test, which inherits the test process env) at the caller's repo
# instead of the temp fixture. Strip them and point global config at
# os.devnull; system config stays suppressed via GIT_CONFIG_NOSYSTEM.
GIT_REPO_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
    # Bare GIT_CONFIG redirects git-config file I/O; GIT_TEMPLATE_DIR
    # injects hooks into `git init`. Neither matches the GIT_CONFIG_*
    # prefix, so they are listed exactly.
    "GIT_CONFIG", "GIT_TEMPLATE_DIR")
# Single shared exclusion used by both GIT_ENV and _hermetic_environ so the
# two filters cannot drift.
_GIT_STRIP_EXACT = frozenset(
    (*GIT_REPO_VARS, "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS"))


def _is_stripped_git_var(key):
    return key in _GIT_STRIP_EXACT or key.startswith("GIT_CONFIG_")


# Single shared forced-vars mapping used by both GIT_ENV and
# _apply_hermetic_environ so the two cannot drift.
_HERMETIC_FORCED = {
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}

GIT_ENV = {
    key: value for key, value in os.environ.items()
    if not _is_stripped_git_var(key)
}
GIT_ENV.update(_HERMETIC_FORCED)
GIT_BASE = ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def _apply_hermetic_environ():
    """Strip leaked git vars from os.environ in place (no save)."""
    for key in list(os.environ):
        if _is_stripped_git_var(key):
            del os.environ[key]
    os.environ.update(_HERMETIC_FORCED)


def _hermetic_environ():
    """Sanitize os.environ for helper children; return the restore.

    The restore only touches variables this module controls (stripped
    git vars + forced vars); unrelated environment changes made by other
    suites in a combined run are left alone.
    """
    saved = {key: os.environ.get(key) for key in os.environ
             if _is_stripped_git_var(key)}
    saved.update({key: os.environ.get(key) for key in _HERMETIC_FORCED})
    _apply_hermetic_environ()

    def _restore():
        for key in list(os.environ):
            if _is_stripped_git_var(key) and key not in saved:
                del os.environ[key]
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return _restore


# Import-time sanitization exists only to cover helper import-time
# environment snapshots; it is restored immediately after the import
# attempt so it cannot leak into other modules' imports/tests in
# combined runs. setUpModule takes a fresh save for this module's tests.
_IMPORT_RESTORE = _hermetic_environ()

try:
    import review_rebase_carry as rrc
except ImportError as exc:
    if getattr(exc, "name", None) == "review_rebase_carry":
        rrc = None
    else:
        raise
finally:
    _IMPORT_RESTORE()

_HERMETIC_RESTORE = None  # replaced by setUpModule before any test runs


def setUpModule():
    global _HERMETIC_RESTORE
    # Fresh save here, not at import: earlier modules' tests in the
    # combined invocation may have mutated os.environ since our import.
    _HERMETIC_RESTORE = _hermetic_environ()
    # Register here so the restore runs after THIS module even in combined
    # runs (the addModuleCleanup stack is process-global and drains after
    # the first module), and even when the SkipTest below fires (module
    # cleanups run on setUpModule failure, unlike tearDownModule).
    unittest.addModuleCleanup(_HERMETIC_RESTORE)
    # Skip (not error) until the builder module lands. The skip must raise
    # here, not at module top: a top-level `raise SkipTest` only converts
    # to a skip under unittest discovery, while the repo runs suites by
    # module name in one invocation (ci.yml, verify-fast.sh), where
    # loadTestsFromName would propagate it as a traceback and fail the
    # whole combined run.
    if rrc is None:
        raise unittest.SkipTest(
            "review_rebase_carry is not landed yet; these acceptance "
            "tests activate when the paired builder change adds the module")


def tearDownModule():
    # Backup restore for runners that honor module fixtures but not module
    # cleanups; idempotent under unittest (doModuleCleanups re-runs it).
    # Guarded: setUpModule always assigns this first under unittest, but a
    # runner that calls tearDownModule without setUpModule must not crash.
    if _HERMETIC_RESTORE is not None:
        _HERMETIC_RESTORE()


def run_git(repo, *args):
    return subprocess.run([*GIT_BASE, *args], cwd=repo, env=GIT_ENV,
                          capture_output=True)


def git(repo, *args):
    proc = run_git(repo, *args)
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed: {proc.stderr.decode()[:400]}")
    return proc.stdout.decode().strip()


class GitRepo:
    """A throwaway local git repo with deterministic identity config."""

    def __init__(self):
        self.dir = None

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rebase-carry-test-")
        # --template= ignores any inherited GIT_TEMPLATE_DIR (belt and
        # braces: it is also stripped from GIT_ENV).
        git(self.dir, "init", "--template=", "-b", "main", "-q")
        git(self.dir, "config", "user.email", "acceptance@example.com")
        git(self.dir, "config", "user.name", "Acceptance")
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
    """Base plus PR branch with commits A and B.

    Returns (old_base, h1, a, b).
    """
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
    """Simulate a rebase landing: fresh branch off the new base, then
    cherry-picks."""
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


def decide_scene(test, repo, old_base, new_base, h1, h2, reviews,
                 compare=None):
    test.assertEqual(git(repo.dir, "merge-base", old_base, h1), old_base)
    return rrc.decide(repo.dir, PR, h1, h2, base("main", old_base),
                      base("main", new_base), reviews, compare=compare)


def build_carry_scene():
    """Real `git rebase` of A+B onto an advanced main.

    Returns (cleanup, scene); scene holds repo/old_base/new_base/h1/h2.
    """
    repo = GitRepo()
    repo.__enter__()
    try:
        old_base, h1, _, _ = seed_pr(repo)
        new_base = advance_main(repo)
        git(repo.dir, "checkout", "-q", "pr")
        proc = run_git(repo.dir, "rebase", "main")
        assert proc.returncode == 0, proc.stderr.decode()[:400]
        h2 = repo.head()
    except Exception:
        repo.__exit__(None, None, None)
        raise
    scene = {"repo": repo, "old_base": old_base, "new_base": new_base,
             "h1": h1, "h2": h2}
    return repo.__exit__, scene


def patch(*bodies):
    """Join patch fragments the way git delimits records (LF)."""
    return b"\n".join(bodies) + b"\n"


DIFF_A = b"diff --git a/f b/f"
DIFF_B = b"diff --git a/g b/g"
INDEX = b"index 1111111..2222222 100644"

class CarryOnEmptyRangeDiff(unittest.TestCase):
    def test_empty_range_diff_carries_with_proof(self):
        cleanup, scene = build_carry_scene()
        try:
            repo = scene["repo"]
            action, detail = decide_scene(
                self, repo, scene["old_base"], scene["new_base"],
                scene["h1"], scene["h2"], [review_row(1, PR, scene["h1"])])
            self.assertEqual(action, "carry")
            self.assertEqual(detail["old_head"], scene["h1"])
            self.assertEqual(detail["new_head"], scene["h2"])
            self.assertEqual(detail["old_base"], scene["old_base"])
            self.assertEqual(detail["new_base"], scene["new_base"])
            self.assertRegex(detail["digest"], r"^[0-9a-f]{64}$")
            # The proof digest is the real own-patch digest on both sides.
            old_digest = rrc.digest(rrc.own_patch(
                repo.dir, scene["old_base"], scene["h1"]))
            new_digest = rrc.digest(rrc.own_patch(
                repo.dir, scene["new_base"], scene["h2"]))
            self.assertEqual(old_digest, new_digest)
            self.assertEqual(detail["digest"], old_digest)
            self.assertEqual(detail["summary"], rrc.carry_summary(
                scene["h1"], scene["h2"], old_digest))
        finally:
            cleanup(None, None, None)

    def test_range_diff_oracle_shows_no_content_change(self):
        # Independent oracle: git's own range-diff reports the rebased
        # commits as content-identical ('='), which is what "empty
        # range-diff" means here.
        cleanup, scene = build_carry_scene()
        try:
            proc = run_git(scene["repo"].dir, "range-diff",
                           f"{scene['old_base']}..{scene['h1']}",
                           f"{scene['new_base']}..{scene['h2']}")
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:400])
            out = proc.stdout.decode()
            commit_lines = [line for line in out.splitlines()
                            if re.match(r"^\d+:", line)]
            self.assertEqual(len(commit_lines), 2, out)
            for line in commit_lines:
                self.assertIn(" = ", line, out)
            self.assertNotIn("!", out)
        finally:
            cleanup(None, None, None)

    def test_decide_and_delta_leave_no_side_effects(self):
        # The pure decision path writes nothing: a single writer elsewhere
        # owns check publication.
        cleanup, scene = build_carry_scene()
        try:
            repo = scene["repo"]
            decide_scene(
                self, repo, scene["old_base"], scene["new_base"],
                scene["h1"], scene["h2"], [review_row(1, PR, scene["h1"])])
            rrc.delta_files(repo.dir, scene["h1"], scene["h2"],
                            base("main", scene["old_base"]),
                            base("main", scene["new_base"]))
            self.assertEqual(git(repo.dir, "status", "--porcelain"), "")
        finally:
            cleanup(None, None, None)

    def test_carry_summary_format_is_exact(self):
        h1, h2, digest = "a" * 40, "b" * 40, "c" * 64
        self.assertEqual(
            rrc.carry_summary(h1, h2, digest),
            f"5/5 carried: identical reviewed patch after rebase "
            f"({h1} -> {h2}, digest {digest})")


class NonEmptyRangeDiffForcesReview(unittest.TestCase):
    def test_changed_line_from_conflict_resolution_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            repo.commit("feature.py", "def f():\n    return 3\n",
                        "conflict resolution changes a line")
            h2 = repo.head()
            action, detail = decide_scene(
                self, repo, old_base, new_base, h1, h2,
                [review_row(1, PR, h1)])
            self.assertEqual(action, "review")
            self.assertTrue(detail["reason"])

    def test_added_commit_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            repo.commit("extra.py", "y = 2\n", "extra commit on the rebase")
            h2 = repo.head()
            action, _ = decide_scene(
                self, repo, old_base, new_base, h1, h2,
                [review_row(1, PR, h1)])
            self.assertEqual(action, "review")

    def test_dropped_commit_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, a, _ = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a)
            h2 = repo.head()
            action, _ = decide_scene(
                self, repo, old_base, new_base, h1, h2,
                [review_row(1, PR, h1)])
            self.assertEqual(action, "review")

    def test_whitespace_only_edit_on_changed_line_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            repo.commit("feature.py", "def f():\n    return 2 \n",
                        "trailing space on a changed line")
            h2 = repo.head()
            action, _ = decide_scene(
                self, repo, old_base, new_base, h1, h2,
                [review_row(1, PR, h1)])
            self.assertEqual(action, "review")
            # The rule is deliberately not patch-id: patch-id would call
            # these two own-patches equal, the digest must not.
            old_mb = git(repo.dir, "merge-base", old_base, h1)
            new_mb = git(repo.dir, "merge-base", new_base, h2)
            old_raw = run_git(repo.dir, "diff", old_mb, h1).stdout
            new_raw = run_git(repo.dir, "diff", new_mb, h2).stdout
            old_pid = subprocess.run(
                [*GIT_BASE, "patch-id", "--stable"], cwd=repo.dir,
                env=GIT_ENV, capture_output=True, input=old_raw)
            new_pid = subprocess.run(
                [*GIT_BASE, "patch-id", "--stable"], cwd=repo.dir,
                env=GIT_ENV, capture_output=True, input=new_raw)
            self.assertEqual(old_pid.returncode, 0)
            self.assertEqual(new_pid.returncode, 0)
            self.assertEqual(old_pid.stdout.split()[0],
                             new_pid.stdout.split()[0])
            self.assertNotEqual(
                rrc.digest(rrc.own_patch(repo.dir, old_base, h1)),
                rrc.digest(rrc.own_patch(repo.dir, new_base, h2)))

    def test_file_mode_change_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            git(repo.dir, "update-index", "--chmod=+x", "feature.py")
            git(repo.dir, "commit", "-q", "-m", "make feature executable")
            h2 = repo.head()
            action, _ = decide_scene(
                self, repo, old_base, new_base, h1, h2,
                [review_row(1, PR, h1)])
            self.assertEqual(action, "review")

    def test_binary_payload_change_forces_review(self):
        with GitRepo() as repo:
            repo.commit("app.py", "x\n", "base")
            old_base = repo.head()
            git(repo.dir, "checkout", "-q", "-b", "pr")
            repo.commit("blob.bin", b"\x00\x01\x02binary-payload-v1\xff",
                        "A: add binary")
            h1 = repo.head()
            git(repo.dir, "checkout", "-q", "main")
            repo.commit("app.py", "x2\n", "main moves")
            new_base = repo.head()
            replay_onto(self, repo, new_base, h1)
            repo.commit("blob.bin", b"\x00\x01\x02binary-payload-v2\xff",
                        "flip one payload byte")
            h2 = repo.head()
            old_patch = rrc.own_patch(repo.dir, old_base, h1)
            self.assertIn(b"GIT binary patch", old_patch)
            action, _ = decide_scene(
                self, repo, old_base, new_base, h1, h2,
                [review_row(1, PR, h1)])
            self.assertEqual(action, "review")

    def test_delete_plus_add_shown_as_rename_forces_review(self):
        with GitRepo() as repo:
            repo.commit("app.py", "x\n", "base")
            old_base = repo.head()
            git(repo.dir, "checkout", "-q", "-b", "pr")
            content = "".join(f"line {i} of shared content here\n"
                              for i in range(30))
            repo.commit("old_name.txt", content, "A: add old name")
            h1 = repo.head()
            git(repo.dir, "checkout", "-q", "main")
            repo.commit("app.py", "x2\n", "main moves")
            new_base = repo.head()
            replay_onto(self, repo, new_base, h1)
            git(repo.dir, "rm", "-q", "old_name.txt")
            repo.commit("new_name.txt", content, "rename via delete+add")
            h2 = repo.head()
            # Oracle: default rename detection collapses this to a rename,
            # but the carry rule runs with --no-renames and must see a
            # changed own-patch.
            name_status = git(repo.dir, "diff", "--name-status", h1, h2)
            self.assertTrue(
                any(line.startswith("R")
                    for line in name_status.splitlines()), name_status)
            action, _ = decide_scene(
                self, repo, old_base, new_base, h1, h2,
                [review_row(1, PR, h1)])
            self.assertEqual(action, "review")

class LatestReview(unittest.TestCase):
    H1 = "a" * 40
    H2 = "b" * 40

    def test_accepts_bound_successful_55(self):
        row = review_row(1, PR, self.H1)
        self.assertEqual(rrc.latest_review([row], PR, self.H1), row)

    def test_rejects_non_55_scores(self):
        for title in ("4/5", "3/5", "2/5", "1/5", ""):
            with self.subTest(title=title):
                self.assertIsNone(rrc.latest_review(
                    [review_row(1, PR, self.H1, title=title)], PR, self.H1))

    def test_rejects_failed_or_incomplete_rows(self):
        cases = [("completed", "failure"), ("completed", "cancelled"),
                 ("completed", None), ("in_progress", None),
                 ("queued", None)]
        for status, conclusion in cases:
            with self.subTest(status=status, conclusion=conclusion):
                self.assertIsNone(rrc.latest_review(
                    [review_row(1, PR, self.H1, status=status,
                                conclusion=conclusion)], PR, self.H1))

    def test_missing_review_is_none(self):
        self.assertIsNone(rrc.latest_review([], PR, self.H1))
        self.assertIsNone(rrc.latest_review(None, PR, self.H1))

    def test_newer_row_wins_over_older_55(self):
        older = review_row(1, PR, self.H1)
        newer = review_row(2, PR, self.H1, title="4/5")
        self.assertIsNone(rrc.latest_review([older, newer], PR, self.H1))
        self.assertIsNone(rrc.latest_review([newer, older], PR, self.H1))

    def test_newer_55_recovers_after_older_45(self):
        older = review_row(1, PR, self.H1, title="4/5")
        newer = review_row(2, PR, self.H1)
        self.assertEqual(rrc.latest_review([older, newer], PR, self.H1),
                         newer)

    def test_rows_on_other_heads_are_ignored(self):
        other = review_row(9, PR, self.H2, title="4/5")
        mine = review_row(1, PR, self.H1)
        self.assertEqual(rrc.latest_review([other, mine], PR, self.H1),
                         mine)
        self.assertIsNone(rrc.latest_review([other], PR, self.H1))

    def test_rows_on_other_prs_are_ignored(self):
        # The same head SHA can appear on two PRs; a 5/5 bound to another
        # PR must never satisfy this PR's gate.
        foreign = review_row(9, PR + 1, self.H1)
        self.assertIsNone(rrc.latest_review([foreign], PR, self.H1))
        mine = review_row(1, PR, self.H1)
        self.assertEqual(
            rrc.latest_review([foreign, mine], PR, self.H1), mine)

    def test_conflicting_tie_for_newest_is_ambiguous(self):
        good = review_row(2, PR, self.H1)
        bad = review_row(2, PR, self.H1, title="4/5")
        self.assertIsNone(rrc.latest_review([good, bad], PR, self.H1))
        twin_a = review_row(2, PR, self.H1)
        twin_b = review_row(2, PR, self.H1)
        self.assertIsNotNone(
            rrc.latest_review([twin_a, twin_b], PR, self.H1))


class FailClosedReviewRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cleanup, scene = build_carry_scene()
        cls.scene = scene
        cls.addClassCleanup(cls.cleanup, None, None, None)

    def decide_rows(self, reviews):
        scene = self.scene
        return decide_scene(
            self, scene["repo"], scene["old_base"], scene["new_base"],
            scene["h1"], scene["h2"], reviews)

    def test_45_on_h1_forces_review(self):
        action, detail = self.decide_rows(
            [review_row(1, PR, self.scene["h1"], title="4/5")])
        self.assertEqual(action, "review")
        self.assertTrue(detail["reason"])

    def test_failed_row_on_h1_forces_review(self):
        action, _ = self.decide_rows(
            [review_row(1, PR, self.scene["h1"], conclusion="failure")])
        self.assertEqual(action, "review")

    def test_missing_row_on_h1_forces_review(self):
        for reviews in ([], None):
            with self.subTest(reviews=reviews):
                action, _ = self.decide_rows(reviews)
                self.assertEqual(action, "review")

    def test_newer_45_after_older_55_forces_review(self):
        h1 = self.scene["h1"]
        action, _ = self.decide_rows(
            [review_row(1, PR, h1), review_row(2, PR, h1, title="4/5")])
        self.assertEqual(action, "review")

    def test_newer_55_after_older_45_still_carries(self):
        h1 = self.scene["h1"]
        action, _ = self.decide_rows(
            [review_row(1, PR, h1, title="4/5"), review_row(2, PR, h1)])
        self.assertEqual(action, "carry")

    def test_review_already_on_h2_means_no_carry(self):
        h1, h2 = self.scene["h1"], self.scene["h2"]
        rows = [review_row(2, PR, h2),
                review_row(2, PR, h2, title="4/5"),
                review_row(2, PR, h2, status="in_progress",
                           conclusion=None)]
        for row in rows:
            with self.subTest(row=row):
                action, _ = self.decide_rows([review_row(1, PR, h1), row])
                self.assertEqual(action, "review")

    def test_55_bound_to_another_pr_forces_review(self):
        h1 = self.scene["h1"]
        action, _ = self.decide_rows([review_row(1, PR + 1, h1)])
        self.assertEqual(action, "review")

    def test_conflicting_tie_for_newest_forces_review(self):
        h1 = self.scene["h1"]
        action, _ = self.decide_rows(
            [review_row(2, PR, h1), review_row(2, PR, h1, title="4/5")])
        self.assertEqual(action, "review")

class FailClosedEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cleanup, scene = build_carry_scene()
        cls.scene = scene
        cls.addClassCleanup(cls.cleanup, None, None, None)

    def decide_evidence(self, old_base=None, new_base=None, reviews=None,
                        compare=None, new_head=None):
        scene = self.scene
        old = old_base if old_base is not None else base(
            "main", scene["old_base"])
        new = new_base if new_base is not None else base(
            "main", scene["new_base"])
        rows = reviews if reviews is not None else [
            review_row(1, PR, scene["h1"])]
        return rrc.decide(scene["repo"].dir, PR, scene["h1"],
                          new_head or scene["h2"], old, new, rows,
                          compare=compare)

    def test_unresolvable_merge_base_forces_review(self):
        action, detail = self.decide_evidence(
            new_base=base("main", "0" * 40))
        self.assertEqual(action, "review")
        self.assertTrue(detail["reason"])

    def test_unrelated_history_base_forces_review(self):
        with GitRepo() as repo:
            old_base, h1, _, _ = seed_pr(repo)
            # Distinct new head with no review row, so decide cannot
            # short-circuit on an already-reviewed head before resolving
            # the merge-base. Empty, so the own-patch digest is unchanged.
            git(repo.dir, "commit", "-q", "--allow-empty", "-m",
                "descendant of h1")
            h2 = repo.head()
            git(repo.dir, "checkout", "-q", "--orphan", "alien")
            git(repo.dir, "rm", "-q", "-rf", ".")
            alien = repo.commit("stowaway.txt", "unrelated\n", "alien root")
            with self.assertRaises(rrc.GitError):
                rrc.merge_base(repo.dir, alien, h1)
            action, _ = rrc.decide(
                repo.dir, PR, h1, h2, base("main", old_base),
                base("main", alien), [review_row(1, PR, h1)])
            self.assertEqual(action, "review")

    def test_unrelated_history_with_empty_old_patch_forces_review(self):
        # Both own-patches are empty here, so a merge_base that wrongly
        # returns the head instead of raising GitError would compare two
        # empty patches as equal and carry a 5/5 onto a head that was
        # never actually compared.
        with GitRepo() as repo:
            repo.commit("app.py", "print('base')\n", "base")
            old_base = repo.head()
            git(repo.dir, "checkout", "-q", "-b", "pr")
            git(repo.dir, "commit", "-q", "--allow-empty", "-m", "A: noop")
            h1 = repo.head()
            git(repo.dir, "commit", "-q", "--allow-empty", "-m", "B: noop")
            h2 = repo.head()
            git(repo.dir, "checkout", "-q", "--orphan", "alien")
            git(repo.dir, "rm", "-q", "-rf", ".")
            alien = repo.commit("stowaway.txt", "unrelated\n", "alien root")
            with self.assertRaises(rrc.GitError):
                rrc.merge_base(repo.dir, alien, h2)
            action, _ = rrc.decide(
                repo.dir, PR, h1, h2, base("main", old_base),
                base("main", alien), [review_row(1, PR, h1)])
            self.assertEqual(action, "review")

    def test_ambiguous_merge_base_forces_review(self):
        with GitRepo() as repo:
            root = repo.commit("o", "o\n", "root")
            git(repo.dir, "checkout", "-q", "-b", "sidex")
            x = repo.commit("x", "x\n", "x")
            git(repo.dir, "checkout", "-q", "-b", "sidey", root)
            repo.commit("y", "y\n", "y")
            git(repo.dir, "checkout", "-q", "sidex")
            git(repo.dir, "merge", "-q", "--no-edit", "sidey")
            sidex = repo.head()
            git(repo.dir, "checkout", "-q", "sidey")
            git(repo.dir, "merge", "-q", "--no-edit", x)
            sidey = repo.head()
            proc = run_git(repo.dir, "merge-base", "--all", sidex, sidey)
            self.assertEqual(len(proc.stdout.decode().split()), 2)
            with self.assertRaises(rrc.GitError):
                rrc.merge_base(repo.dir, sidex, sidey)
            action, _ = rrc.decide(
                repo.dir, PR, sidex, sidey, base("main", root),
                base("main", sidex), [review_row(1, PR, sidex)])
            self.assertEqual(action, "review")

    def test_retargeted_base_branch_forces_review(self):
        scene = self.scene
        action, detail = self.decide_evidence(new_base=base(
            "develop", scene["new_base"]))
        self.assertEqual(action, "review")
        self.assertTrue(detail["reason"])

    def test_truncated_compare_forces_review(self):
        scene = self.scene
        compare = {"truncated": True, "total_commits": 2,
                   "commits": [{"sha": scene["h1"]}, {"sha": scene["h2"]}],
                   "total_files": 1, "files": [{"filename": "feature.py"}]}
        action, _ = self.decide_evidence(compare=compare)
        self.assertEqual(action, "review")

    def test_paginated_compare_forces_review(self):
        scene = self.scene
        paged_commits = {"truncated": False, "total_commits": 5,
                         "commits": [{"sha": scene["h1"]}],
                         "total_files": 1,
                         "files": [{"filename": "feature.py"}]}
        paged_files = {"truncated": False, "total_commits": 2,
                       "commits": [{"sha": scene["h1"]},
                                   {"sha": scene["h2"]}],
                       "total_files": 3, "files": [{"filename": "a.py"}]}
        for compare in (paged_commits, paged_files):
            with self.subTest(compare=compare):
                action, _ = self.decide_evidence(compare=compare)
                self.assertEqual(action, "review")

    def test_complete_compare_still_carries(self):
        scene = self.scene
        compare = {"truncated": False, "total_commits": 2,
                   "commits": [{"sha": scene["h1"]}, {"sha": scene["h2"]}],
                   "total_files": 1, "files": [{"filename": "feature.py"}]}
        action, _ = self.decide_evidence(compare=compare)
        self.assertEqual(action, "carry")

    def test_missing_repo_dir_returns_review_without_raising(self):
        scene = self.scene
        missing = os.path.join(scene["repo"].dir, "no-such-repo")
        action, detail = rrc.decide(
            missing, PR, scene["h1"], scene["h2"],
            base("main", scene["old_base"]),
            base("main", scene["new_base"]),
            [review_row(1, PR, scene["h1"])])
        self.assertEqual(action, "review")
        self.assertTrue(detail["reason"])

    def test_unknown_head_returns_review_without_raising(self):
        action, detail = self.decide_evidence(new_head="f" * 40)
        self.assertEqual(action, "review")
        self.assertTrue(detail["reason"])

    def test_decide_with_garbage_inputs_returns_review(self):
        scene = self.scene
        action, detail = rrc.decide(
            scene["repo"].dir, PR, "zz", "zz",
            {"ref": "", "sha": "zz"}, {"ref": "", "sha": "zz"}, [])
        self.assertEqual(action, "review")
        self.assertTrue(detail["reason"])
        with tempfile.NamedTemporaryFile(
                prefix="not-a-repo-") as handle:
            action, _ = rrc.decide(
                handle.name, PR, scene["h1"], scene["h2"],
                base("main", scene["old_base"]),
                base("main", scene["new_base"]),
                [review_row(1, PR, scene["h1"])])
            self.assertEqual(action, "review")

class DeltaSelection(unittest.TestCase):
    def test_rebase_plus_fix_lists_only_the_pr_delta(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            repo.commit("feature.py", "def f():\n    return 99\n",
                        "fix commit after rebase")
            h2 = repo.head()
            files = rrc.delta_files(
                repo.dir, h1, h2, base("main", old_base),
                base("main", new_base))
            self.assertEqual(files, ["feature.py"])
            # Oracle: the naive H1...H2 compare DOES include everything
            # main gained, which is why the delta must not use it.
            naive = git(repo.dir, "diff", "--name-only",
                        f"{h1}...{h2}", "--").split()
            self.assertIn("main_only.py", naive)
            self.assertIn("base.txt", naive)

    def test_prior_blocking_findings_are_always_included(self):
        with GitRepo() as repo:
            old_base, h1, a, b = seed_pr(repo)
            new_base = advance_main(repo)
            replay_onto(self, repo, new_base, a, b)
            repo.commit("feature.py", "def f():\n    return 99\n",
                        "fix commit after rebase")
            h2 = repo.head()
            findings = [{"path": "app.py", "severity": "blocking"},
                        {"path": "feature.py", "severity": "blocking"}]
            files = rrc.delta_files(
                repo.dir, h1, h2, base("main", old_base),
                base("main", new_base), findings=findings)
            # app.py is unchanged since H1 but was named by a prior
            # blocking finding, so it stays in the re-review list.
            self.assertEqual(files, ["app.py", "feature.py"])

    def test_identical_own_patch_yields_empty_delta(self):
        cleanup, scene = build_carry_scene()
        try:
            files = rrc.delta_files(
                scene["repo"].dir, scene["h1"], scene["h2"],
                base("main", scene["old_base"]),
                base("main", scene["new_base"]))
            self.assertEqual(files, [])
        finally:
            cleanup(None, None, None)

    def test_delta_files_raises_git_error_on_git_failure(self):
        cleanup, scene = build_carry_scene()
        try:
            with self.assertRaises(rrc.GitError):
                rrc.delta_files(
                    scene["repo"].dir, scene["h1"], "f" * 40,
                    base("main", scene["old_base"]),
                    base("main", scene["new_base"]))
            with self.assertRaises(rrc.GitError):
                rrc.delta_files(
                    os.path.join(scene["repo"].dir, "no-such-repo"),
                    scene["h1"], scene["h2"],
                    base("main", scene["old_base"]),
                    base("main", scene["new_base"]))
        finally:
            cleanup(None, None, None)


class Idempotency(unittest.TestCase):
    def test_deciding_twice_yields_one_proof_under_one_key(self):
        cleanup, scene = build_carry_scene()
        try:
            repo = scene["repo"]
            rows = [review_row(1, PR, scene["h1"])]
            first = decide_scene(
                self, repo, scene["old_base"], scene["new_base"],
                scene["h1"], scene["h2"], rows)
            second = decide_scene(
                self, repo, scene["old_base"], scene["new_base"],
                scene["h1"], scene["h2"], rows)
            self.assertEqual(first[0], "carry")
            self.assertEqual(first[1], second[1])
            key_kwargs = {"repo": "example/ops", "number": PR}
            self.assertEqual(
                rrc.idempotency_key(head=scene["h2"], **key_kwargs),
                rrc.idempotency_key(head=scene["h2"], **key_kwargs))
        finally:
            cleanup(None, None, None)

    def test_idempotency_key_tracks_repo_pr_and_head(self):
        h2, h3 = "b" * 40, "c" * 40
        key = rrc.idempotency_key("example/ops", PR, h2)
        self.assertNotEqual(key, rrc.idempotency_key("example/ops", PR, h3))
        self.assertNotEqual(
            key, rrc.idempotency_key("example/ops", PR + 1, h2))
        self.assertNotEqual(
            key, rrc.idempotency_key("example/other", PR, h2))
        self.assertEqual(
            key, rrc.idempotency_key("Example/Ops", PR, h2))

    def test_newer_head_invalidates_the_carry_on_h2(self):
        cleanup, scene = build_carry_scene()
        try:
            repo = scene["repo"]
            _, proof = decide_scene(
                self, repo, scene["old_base"], scene["new_base"],
                scene["h1"], scene["h2"],
                [review_row(1, PR, scene["h1"])])
            self.assertTrue(rrc.carry_applies(proof, scene["h2"]))
            git(repo.dir, "checkout", "-q", "pr")
            h3 = repo.commit("feature.py",
                             "def f():\n    return 100\n", "one more fix")
            self.assertNotEqual(h3, scene["h2"])
            self.assertFalse(rrc.carry_applies(proof, h3))
            self.assertNotEqual(
                rrc.idempotency_key("example/ops", PR, scene["h2"]),
                rrc.idempotency_key("example/ops", PR, h3))
        finally:
            cleanup(None, None, None)

class NoSecrets(unittest.TestCase):
    SECRET_ENV_HINTS = ("TOKEN", "SECRET", "KEY", "CREDENTIAL", "PASSWORD",
                        "GH_", "GITHUB")

    def test_import_and_digest_need_no_secrets(self):
        scrubbed = {
            key: value for key, value in os.environ.items()
            if not any(hint in key.upper()
                       for hint in self.SECRET_ENV_HINTS)}
        scrubbed["PATH"] = os.environ.get("PATH", "")
        module_dir = os.path.dirname(os.path.abspath(rrc.__file__))
        scrubbed["PYTHONPATH"] = module_dir
        code = ("import review_rebase_carry as m; "
                "print(m.digest(b''))")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            env=scrubbed, cwd=tempfile.gettempdir(), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[:500])
        self.assertRegex(proc.stdout.strip(), r"^[0-9a-f]{64}$")

    def test_module_imports_nothing_credentialed(self):
        source = Path(rrc.__file__).read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(part.name.split(".")[0]
                                for part in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        forbidden = {"sdlc_lib", "review_carry", "docs_lane"}
        self.assertTrue(forbidden.isdisjoint(imported),
                        f"host/credential modules imported: "
                        f"{sorted(forbidden & imported)}")
        stdlib = getattr(sys, "stdlib_module_names", None) or {
            "argparse", "ast", "base64", "collections", "contextlib",
            "copy", "dataclasses", "datetime", "enum", "fnmatch", "functools",
            "hashlib", "hmac", "io", "itertools", "json", "logging", "math",
            "os", "pathlib", "re", "shutil", "shlex", "stat", "string",
            "subprocess", "sys", "tempfile", "textwrap", "time", "typing",
            "unittest", "urllib",
        }
        sibling_dir = os.path.dirname(os.path.abspath(rrc.__file__))
        foreign = {name for name in imported
                   if name not in stdlib and not os.path.exists(
                       os.path.join(sibling_dir, name + ".py"))}
        self.assertEqual(foreign, set(),
                         f"non-stdlib, non-local imports: {sorted(foreign)}")
        self.assertNotIn("sdlc_lib", sys.modules)

    def test_module_source_names_no_credentials(self):
        text = Path(rrc.__file__).read_text().lower()
        markers = ("access_token", "accesstoken", "x-access-token",
                   "begin private", "botcredentials", "secure-drop",
                   "gh-event-capture")
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)


class PureSemantics(unittest.TestCase):
    def test_hunk_offset_shift_compares_equal(self):
        left = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+same")
        right = patch(DIFF_A, INDEX, b"@@ -100 +100 @@", b"+same")
        self.assertEqual(rrc.normalized_patch(left),
                         rrc.normalized_patch(right))
        self.assertEqual(rrc.digest(left), rrc.digest(right))

    def test_significant_whitespace_compares_unequal(self):
        plain = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+x")
        trailing_space = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+x ")
        self.assertNotEqual(rrc.normalized_patch(plain),
                            rrc.normalized_patch(trailing_space))
        self.assertNotEqual(rrc.digest(plain), rrc.digest(trailing_space))

    def test_cr_bytes_inside_changed_lines_are_preserved(self):
        with_cr = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+a\rb")
        without_cr = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+ab")
        self.assertNotEqual(rrc.normalized_patch(with_cr),
                            rrc.normalized_patch(without_cr))

    def test_mode_change_compares_unequal(self):
        exec_bit = patch(DIFF_A, b"index 1111111..2222222 100755",
                         b"@@ -1 +1 @@", b"+same")
        no_exec = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+same")
        self.assertNotEqual(rrc.normalized_patch(exec_bit),
                            rrc.normalized_patch(no_exec))
        mode_only = patch(DIFF_A, b"old mode 100644", b"new mode 100755")
        content_only = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+same")
        self.assertNotEqual(rrc.normalized_patch(mode_only),
                            rrc.normalized_patch(content_only))

    def test_file_type_transition_keeps_both_records(self):
        # Git emits two diff --git records for a delete/add transition.
        transition = patch(DIFF_A, INDEX, b"@@ -1 +0,0 @@", b"-old",
                           DIFF_A, INDEX, b"@@ -0,0 +1 @@", b"+new")
        bodies = rrc.normalized_patch(transition)
        self.assertEqual(len(bodies), 1)
        only = next(iter(bodies.values()))
        self.assertIn(b"-old\n", only)
        self.assertIn(b"+new\n", only)
        # Both records survive: the transition digest matches neither
        # single-record digest, so nothing was collapsed away.
        delete_only = patch(DIFF_A, INDEX, b"@@ -1 +0,0 @@", b"-old")
        add_only = patch(DIFF_A, INDEX, b"@@ -0,0 +1 @@", b"+new")
        self.assertNotEqual(rrc.digest(transition), rrc.digest(delete_only))
        self.assertNotEqual(rrc.digest(transition), rrc.digest(add_only))

    def test_digest_is_insensitive_to_file_order(self):
        file_a = patch(DIFF_A, INDEX, b"@@ -1 +1 @@", b"+aaa")
        file_b = patch(DIFF_B, INDEX, b"@@ -1 +1 @@", b"+bbb")
        self.assertEqual(rrc.digest(file_a + file_b),
                         rrc.digest(file_b + file_a))

    def test_empty_patch_has_empty_digest(self):
        self.assertEqual(rrc.normalized_patch(b""), {})
        self.assertEqual(rrc.digest(b""), hashlib.sha256().hexdigest())

    def test_garbage_patch_raises(self):
        with self.assertRaises(Exception):
            rrc.normalized_patch(b"this is not a patch\n" + DIFF_A + b"\n")
        with self.assertRaises(Exception):
            rrc.normalized_patch(patch(DIFF_A, b"index zzz", b"+x"))


if __name__ == "__main__":
    unittest.main()

