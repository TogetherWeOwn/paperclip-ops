#!/usr/bin/env python3
"""Acceptance tests: the call-site flow for review-only rebase carry.

Baseline behavior under test (module ``review_rebase_carry_flow``, stdlib
only). The flow decides whether a **required merge check** passes, so these
tests are the independent check on it. They were written against the issue
contract, not the implementation, and must fail against an always-carry or
always-skip stub: every group below pins both a positive case (carries,
skips, publishes, writes) and the fail-closed negatives around it.

* Fail-closed control. ``control_mode`` returns ``off`` for any absent,
  unreadable, oversized or malformed control file, for the kill flag, and
  for any env value but a lowering one. Only the file can enable.
* Genuine source only. ``genuine_rows`` keeps only this-App, this-name,
  this-head rows with a genuine ``external_id``; anything else from our App
  on the head is kept as ``untrusted`` so a forged newest row blocks carry
  instead of resurrecting an older 5/5.
* No chaining. A carried check is never review evidence. A later head may
  carry only by hopping back to the genuine source, which is re-verified
  live against the current rows and the current digest.
* Zero rows on the new head. Any row of any kind on H2 means no carry and
  no publish; a row appearing between the first read and the publish
  (TOCTOU) aborts the publish. Incomplete inventories mean review.
* TOCTOU. PR head, base sha, base ref, PR state and the source review row
  id are re-read immediately before publishing; any move means review and
  nothing published.
* Publish failure. A failed or unverifiable receipt PATCHes any check that
  may exist to failure, decides review, and the gate side then does not
  skip.
* Single flight. A second pass while one holds the lock gets ``Busy`` and
  skips without publishing; two sequential passes publish exactly once.
* Base snapshots. A head becomes a carry source only when its base was
  snapshotted within 900 s of its genuine review completing. Snapshots are
  write-once; a moving branch read later is never substituted; corrupt
  ledger state means review, never a crash; ledger caps hold.
* Eligibility. Drafts, forks, foreign repos and untrusted authors are
  skipped and log nothing. A retargeted base ref means review.
* Gate skip. ``gate_skips_review`` is true only in mode ``on`` with exactly
  one verified carried check on the head.
* Shadow publishes nothing. Shadow logs one would-carry/would-review line
  per head and writes nothing; off does not even touch the ledger.
* Delta record. When only the own-patch changed, ``on`` writes the exact
  changed-own-patch file list (removed files included); ``scoped_delta``
  returns files union prior-blocking paths, or None (keep the compare list)
  on any doubt. Never narrower than the union; never an empty list.
* Audit. Every decision line carries repo, pr, both heads, both bases,
  digest, reason and the exact UNDO text; control characters are
  neutralized and the line is bounded; a raising log never changes the
  decision.
* No credential material. The module and the three host patch files carry
  no secret-shaped tokens and import no credential/mint modules.
* Packet shape. Each patch is a one-file ``-p1`` unified diff for its
  pinned host file, adds no executable, and applies ``--fuzz=0`` to a
  synthetic file rebuilt from its own context lines; the gate patch inserts
  the skip test after the ci-green guard and before ``key =``; the findings
  patch classifies the merge-gate paths HIGH.

Hermetic: local git fixture repos only (built in temp dirs), in-memory
check rows, temp dirs for ``Paths``. No network, no tokens, no App
credential. Stdlib ``unittest`` only.

RED story: run against a naive stub that always carries (or always skips).
Every behavioral group below must fail on assertions (not errors): the
positive pins fail against always-skip, the fail-closed pins fail against
always-carry. GREEN story: run against the paired builder module, which
must satisfy every test without editing this file.

Until the builder module lands, this file skips via ``setUpModule`` (it
must stay CI-green as a tests-only change, including under the repo's
by-module-name unittest invocation). A present-but-broken module fails
loudly: only a missing ``review_rebase_carry_flow`` module itself skips.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = 'TogetherWeOwn/paperclip-ops'
PR = 7
APP_ID = 5191093
UNDO_TEXT = 'UNDO: PATCH this same App-owned check to failure'
PATCH_DIR = Path(__file__).resolve().parent / 'patches'
PINNED = {'ops/productivity/review_gate.py': 'review-rebase-carry-gate.patch',
          'ops/productivity/review_findings.py': 'review-rebase-carry-findings.patch',
          'ops/sdlc/review_carry.py': 'review-rebase-carry-carry.patch'}

# Git repository-location variables override the cwd of every git child, so
# a leaked GIT_DIR/GIT_WORK_TREE would aim fixture commands (and the module
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
    """Sanitize os.environ for module children; return the restore.

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


# Import-time sanitization exists only to cover module import-time
# environment snapshots; it is restored immediately after the import
# attempt so it cannot leak into other modules' imports/tests in
# combined runs. setUpModule takes a fresh save for this module's tests.
_IMPORT_RESTORE = _hermetic_environ()

try:
    import review_rebase_carry_flow as flow
    import review_rebase_carry as core
except ImportError as exc:
    if getattr(exc, "name", None) in ("review_rebase_carry_flow",
                                      "review_rebase_carry"):
        flow = None
        core = None
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
    if flow is None or core is None:
        raise unittest.SkipTest(
            "review_rebase_carry_flow is not landed yet; these acceptance "
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
        self.dir = tempfile.mkdtemp(prefix="rebase-carry-flow-test-")
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


def cherry_pick(test, repo, branch, base, *shas, message_suffix=None):
    # message_suffix amends the replayed tip message: replaying identical
    # trees onto the same base within one second would otherwise mint
    # identical SHAs (git timestamps have 1 s granularity, and cherry-pick
    # ignores GIT_COMMITTER_DATE) and collapse two heads into one. The
    # tree is untouched, so the own-patch is unaffected.
    git(repo.dir, "checkout", "-q", "-b", branch, base)
    for sha in shas:
        proc = run_git(repo.dir, "cherry-pick", sha)
        test.assertEqual(proc.returncode, 0, proc.stderr.decode()[:400])
    if message_suffix is not None:
        msg = git(repo.dir, "log", "-1", "--format=%s")
        git(repo.dir, "commit", "-q", "--amend", "-m", msg + message_suffix)
    return repo.head()


@contextlib.contextmanager
def carry_scene(test):
    """Real `git rebase` of A+B onto an advanced main.

    Yields {repo, old_base, new_base, h1, h2} with identical own-patches.
    """
    repo = GitRepo()
    repo.__enter__()
    try:
        old_base, h1, _, _ = seed_pr(repo)
        new_base = advance_main(repo)
        git(repo.dir, "checkout", "-q", "pr")
        proc = run_git(repo.dir, "rebase", "main")
        test.assertEqual(proc.returncode, 0, proc.stderr.decode()[:400])
        yield {"repo": repo, "old_base": old_base, "new_base": new_base,
               "h1": h1, "h2": repo.head()}
    finally:
        repo.__exit__(None, None, None)


@contextlib.contextmanager
def chain_scene(test):
    """H1 plus three independent rebases (H2/H3/H4) with identical patches."""
    repo = GitRepo()
    repo.__enter__()
    try:
        old_base, h1, a, b = seed_pr(repo)
        new_base = advance_main(repo)
        h2 = cherry_pick(test, repo, "pr2", new_base, a, b,
                         message_suffix=' [pr2]')
        h3 = cherry_pick(test, repo, "pr3", new_base, a, b,
                         message_suffix=' [pr3]')
        h4 = cherry_pick(test, repo, "pr4", new_base, a, b,
                         message_suffix=' [pr4]')
        test.assertEqual(len({h1, h2, h3, h4}), 4)
        yield {"repo": repo, "old_base": old_base, "new_base": new_base,
               "h1": h1, "h2": h2, "h3": h3, "h4": h4}
    finally:
        repo.__exit__(None, None, None)


@contextlib.contextmanager
def delta_scene(test, modify):
    """H1 = A+B on the old base; H2 = rebase plus `modify(repo)` committed."""
    repo = GitRepo()
    repo.__enter__()
    try:
        old_base, h1, a, b = seed_pr(repo)
        new_base = advance_main(repo)
        cherry_pick(test, repo, "pr2", new_base, a, b)
        modify(repo)
        yield {"repo": repo, "old_base": old_base, "new_base": new_base,
               "h1": h1, "h2": repo.head()}
    finally:
        repo.__exit__(None, None, None)


def commit_all(repo, message):
    git(repo.dir, "add", "-A")
    git(repo.dir, "commit", "-q", "-m", message)


def iso(offset=0):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + offset))


def make_pr(head, base_ref='main', base_sha=None, repo=REPO, state='open',
            draft=False, login='Rick7C2', utype='User', head_repo=None,
            base_repo=None):
    return {'state': state, 'draft': draft,
            'head': {'sha': head, 'repo': {'full_name': head_repo or repo}},
            'base': {'ref': base_ref, 'sha': base_sha or 'b' * 40,
                     'repo': {'full_name': base_repo or repo}},
            'user': {'login': login, 'type': utype}}


def make_row(row_id, pr, head, title='5/5', status='completed',
             conclusion='success', app_id=APP_ID, name='Paperclip Review',
             external_id=None, completed_at=None):
    if external_id is None:
        external_id = f'paperclip-review:v1:run:{pr}:{head}'
    out = {'id': row_id, 'name': name, 'head_sha': head, 'status': status,
           'conclusion': conclusion, 'external_id': external_id,
           'app': {'id': app_id}, 'output': {'title': title}}
    if completed_at is not None:
        out['completed_at'] = completed_at
    return out


def carried_row(repo, pr, head, **over):
    base = {'id': 500, 'name': 'Paperclip Review', 'head_sha': head,
            'status': 'completed', 'conclusion': 'success',
            'external_id': core.idempotency_key(repo, pr, head),
            'app': {'id': APP_ID},
            'output': {'title': '5/5 carried: identical reviewed patch test'}}
    base.update(over)
    return base


class FakeEnv:
    """Scriptable Env: queues pop per call, single values repeat."""

    def __init__(self, repo_dir, now=None):
        self.git_dir = repo_dir
        self.now = now or iso()
        self.prs = {}
        self.pr_queues = {}
        self.checks = {}
        self.check_queues = {}
        self.raise_on_checks = set()
        self.created = []
        self.patched = []
        self.logs = []
        self.ensured = []
        self.create_behavior = None  # None | 'raise' | callable(payload)->row
        self.publish_visible = False
        self.log_raises = False

    def get_pr(self, number):
        if number in self.pr_queues and len(self.pr_queues[number]) > 1:
            return self.pr_queues[number].pop(0)
        if number in self.pr_queues:
            return self.pr_queues[number][0]
        return self.prs[number]

    def checks_for(self, sha):
        if sha in self.raise_on_checks:
            raise RuntimeError('checks unavailable')
        if sha in self.check_queues and len(self.check_queues[sha]) > 1:
            return self.check_queues[sha].pop(0)
        if sha in self.check_queues:
            return self.check_queues[sha][0]
        return self.checks.get(sha, [])

    def ensure_objects(self, *shas):
        self.ensured.append(shas)

    def create_check(self, payload):
        if self.create_behavior == 'raise':
            raise RuntimeError('create failed')
        if callable(self.create_behavior):
            saved = self.create_behavior(payload)
        else:
            saved = {'id': 9000 + len(self.created), 'name': payload['name'],
                     'head_sha': payload['head_sha'],
                     'status': payload.get('status'),
                     'conclusion': payload.get('conclusion'),
                     'external_id': payload['external_id'],
                     'app': {'id': APP_ID},
                     'output': {'title': payload['output']['title']}}
        self.created.append((payload, saved))
        if self.publish_visible and isinstance(saved, dict):
            self.checks.setdefault(payload['head_sha'], []).append(saved)
        return saved

    def patch_check(self, check_id, payload):
        self.patched.append((check_id, payload))
        return {'id': check_id}

    def now_iso(self):
        return self.now

    def log(self, line):
        if self.log_raises:
            raise RuntimeError('log unavailable')
        self.logs.append(line)


def mktmp(test):
    d = tempfile.mkdtemp(prefix='rebase-carry-flow-test-')
    test.addCleanup(shutil.rmtree, d, True)
    return Path(d)


def run_pass(env, paths, mode='on', repo=REPO, number=PR):
    return flow.evaluate_pr(env, paths, repo, number, mode)


def observe_head(env, paths, head, base, completed):
    """First pass with the PR at `head` carrying a genuine 5/5 row."""
    env.prs[PR] = make_pr(head, base_sha=base)
    env.checks[head] = [make_row(100, PR, head, completed_at=completed)]
    out = run_pass(env, paths)
    assert out['decision'] == 'skip', out
    return out


HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse_patch(text):
    """(old_path, new_path, hunks); hunks are (old_start, [(tag, line)])."""
    old_path = new_path = None
    hunks = []
    body = None
    for line in text.splitlines():
        if line.startswith('--- '):
            old_path = line[4:].strip()
        elif line.startswith('+++ '):
            new_path = line[4:].strip()
        elif line.startswith('@@ '):
            match = HUNK_RE.match(line)
            assert match, f'unparseable hunk header: {line!r}'
            body = []
            hunks.append((int(match.group(1)), body))
        elif body is not None:
            if line.startswith('\\'):
                continue
            assert line[:1] in (' ', '-', '+'), f'bad hunk line: {line!r}'
            body.append((line[:1], line[1:]))
    return old_path, new_path, hunks


class ControlFailsClosed(unittest.TestCase):
    """Case 1: the control enables only on an explicit valid file."""

    def paths(self):
        return flow.Paths(mktmp(self))

    def test_absent_empty_and_malformed_are_off(self):
        paths = self.paths()
        cases = {'missing file': None,
                 'empty file': b'',
                 'invalid JSON': b'{oops',
                 'JSON list': b'["on"]',
                 'JSON string': b'"on"',
                 'mode missing': b'{}',
                 'mode ON (wrong case)': b'{"mode": "ON"}',
                 'mode true (bool)': b'{"mode": true}',
                 'mode enabled (wrong word)': b'{"mode": "enabled"}',
                 'mode null': b'{"mode": null}',
                 'mode 1 (number)': b'{"mode": 1}'}
        for name, content in cases.items():
            with self.subTest(name):
                if content is None:
                    if paths.control.exists():
                        paths.control.unlink()
                else:
                    paths.control.write_bytes(content)
                self.assertEqual(flow.control_mode(paths, {}), 'off')

    def test_shadow_and_on_work(self):
        paths = self.paths()
        paths.control.write_bytes(b'{"mode": "shadow"}')
        self.assertEqual(flow.control_mode(paths, {}), 'shadow')
        paths.control.write_bytes(b'{"mode": "on"}')
        self.assertEqual(flow.control_mode(paths, {}), 'on')

    def test_oversized_and_unreadable_are_off(self):
        paths = self.paths()
        paths.control.write_bytes(b'{"mode": "on"}' + b' ' * 5000)
        self.assertEqual(flow.control_mode(paths, {}), 'off')
        paths.control.unlink()
        paths.control.mkdir()
        try:
            self.assertEqual(flow.control_mode(paths, {}), 'off')
        finally:
            paths.control.rmdir()

    def test_exactly_4kib_still_parses(self):
        paths = self.paths()
        body = b'{"mode": "on"}'
        paths.control.write_bytes(body + b' ' * (4096 - len(body)))
        self.assertEqual(len(paths.control.read_bytes()), 4096)
        self.assertEqual(flow.control_mode(paths, {}), 'on')

    def test_kill_flag_forces_off(self):
        paths = self.paths()
        paths.control.write_bytes(b'{"mode": "on"}')
        paths.kill.write_text('stop')
        self.assertEqual(flow.control_mode(paths, {}), 'off')

    def test_env_lowers_but_never_raises(self):
        paths = self.paths()
        paths.control.write_bytes(b'{"mode": "on"}')
        self.assertEqual(
            flow.control_mode(paths, {'REVIEW_REBASE_CARRY': 'shadow'}),
            'shadow')
        self.assertEqual(
            flow.control_mode(paths, {'REVIEW_REBASE_CARRY': 'off'}), 'off')
        paths.control.write_bytes(b'{"mode": "shadow"}')
        self.assertEqual(
            flow.control_mode(paths, {'REVIEW_REBASE_CARRY': 'on'}), 'shadow')
        paths.control.unlink()
        self.assertEqual(
            flow.control_mode(paths, {'REVIEW_REBASE_CARRY': 'on'}), 'off')

    def test_garbage_env_is_off(self):
        paths = self.paths()
        paths.control.write_bytes(b'{"mode": "on"}')
        for value in ('ON', 'enabled', 'true', '', '2'):
            with self.subTest(value=value):
                self.assertEqual(
                    flow.control_mode(paths, {'REVIEW_REBASE_CARRY': value}),
                    'off')


class GenuineSourceOnly(unittest.TestCase):
    """Case 2: only a genuine newest 5/5 authorizes carry."""

    H1 = 'a' * 40

    def test_unrelated_rows_excluded_genuine_55_still_wins(self):
        # Rows that are not ours at all (other apps, other checks, other
        # heads) are filtered away; the genuine 5/5 still authorizes carry.
        good = make_row(100, PR, self.H1)
        cases = {'wrong App id': make_row(101, PR, self.H1, app_id=1),
                 'wrong name': make_row(101, PR, self.H1, name='CI'),
                 'wrong head': make_row(101, PR, 'c' * 40)}
        for name, foreign in cases.items():
            with self.subTest(name):
                rows = flow.genuine_rows([good, foreign], PR, self.H1)
                self.assertEqual(len(rows), 1)
                latest = core.latest_review(rows, PR, self.H1)
                self.assertIsNotNone(latest)
                self.assertEqual(latest['id'], 100)

    def test_forged_newest_from_our_app_blocks_carry(self):
        # A newest row from our App on this head with a foreign provenance
        # is kept as untrusted so it blocks carry instead of being
        # filtered away to resurrect the older 5/5.
        good = make_row(100, PR, self.H1)
        cases = {'foreign external_id': make_row(
                     101, PR, self.H1, external_id='someone-else:v1:x'),
                 'external_id wrong PR': make_row(
                     101, PR, self.H1,
                     external_id=f'paperclip-review:v1:run:999:{self.H1}'),
                 'merge-group carry namespace': make_row(
                     101, PR, self.H1,
                     external_id=f'paperclip-review-carry:v1:x:{PR}:{self.H1}'),
                 'docs carry namespace': make_row(
                     101, PR, self.H1,
                     external_id=f'paperclip-docs-carry:v1:x:{PR}:{self.H1}'),
                 'own rebase-carry namespace': make_row(
                     101, PR, self.H1,
                     external_id=core.idempotency_key(REPO, PR, self.H1))}
        for name, foreign in cases.items():
            with self.subTest(name):
                rows = flow.genuine_rows([good, foreign], PR, self.H1)
                self.assertTrue(
                    any(r['status'] == 'untrusted' for r in rows),
                    'forged row from our App stays visible as untrusted')
                self.assertEqual(
                    core.latest_review(rows, PR, self.H1), None,
                    'forged newest row must block carry, not be filtered away')

    def test_genuine_newest_55_wins(self):
        # Positive pin: fails against an always-skip stub.
        rows = flow.genuine_rows([make_row(100, PR, self.H1)], PR, self.H1)
        latest = core.latest_review(rows, PR, self.H1)
        self.assertIsNotNone(latest)
        self.assertEqual(latest['id'], 100)

    def test_newer_non_55_blocks_older_55(self):
        old = make_row(100, PR, self.H1)
        cases = {'newer failure 4/5': make_row(101, PR, self.H1, title='4/5',
                                              conclusion='failure'),
                 'newer queued': make_row(101, PR, self.H1, status='queued',
                                          conclusion=None, title=None),
                 'newer carried title': make_row(
                     101, PR, self.H1, title='5/5 carried: identical x',
                     external_id=f'paperclip-review:v1:run:{PR}:{self.H1}')}
        for name, newer in cases.items():
            with self.subTest(name):
                rows = flow.genuine_rows([old, newer], PR, self.H1)
                self.assertEqual(core.latest_review(rows, PR, self.H1), None)

    def test_malformed_rows_fall_back_to_review_never_raise(self):
        for name, bad in {'not a mapping': [None],
                          'id missing': [make_row_no_id()],
                          'id not an int': [make_row('abc', PR, self.H1)],
                          'id negative': [make_row(-5, PR, self.H1)]}.items():
            with self.subTest(name):
                with self.assertRaises(flow.FlowError):
                    flow.genuine_rows(bad, PR, self.H1)
                env = FakeEnv(str(mktmp(self)))
                env.prs[PR] = make_pr(self.H1)
                env.checks[self.H1] = bad
                out = run_pass(env, flow.Paths(mktmp(self)))
                self.assertEqual(out['decision'], 'review')

    def test_forged_newest_blocks_end_to_end_carry(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            env.prs[PR] = make_pr(scene['h1'], base_sha=scene['old_base'])
            env.checks[scene['h1']] = [
                make_row(100, PR, scene['h1'], completed_at=env.now),
                make_row(101, PR, scene['h1'], completed_at=env.now,
                         external_id='forged:v1:x')]
            out = run_pass(env, paths)
            self.assertNotEqual(out['decision'], 'carry')
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertNotEqual(out['decision'], 'carry')
            self.assertEqual(env.created, [])


def make_row_no_id():
    row = make_row(100, PR, 'a' * 40)
    del row['id']
    return row


class NoChaining(unittest.TestCase):
    """Case 3: a carried check is never evidence; the source re-verifies."""

    def test_h3_carries_only_through_live_h1(self):
        with chain_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            env.publish_visible = True
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            with flow.Ledger(paths) as ledger:
                item = ledger.find(REPO, PR, scene['h1'])
                self.assertIsNotNone(item)
                self.assertEqual(item['kind'], 'genuine')
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'carry', out)
            env.prs[PR] = make_pr(scene['h3'], base_sha=scene['new_base'])
            env.checks[scene['h3']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'carry', out)
            self.assertEqual(len(env.created), 2)
            # H1's newest row turned 4/5 after H2 was carried: H4 reviews.
            env.checks[scene['h1']] = [
                make_row(100, PR, scene['h1'], completed_at=env.now),
                make_row(101, PR, scene['h1'], title='4/5',
                         conclusion='failure', completed_at=env.now)]
            env.prs[PR] = make_pr(scene['h4'], base_sha=scene['new_base'])
            env.checks[scene['h4']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(len(env.created), 2)

    def test_forged_carry_on_h1_never_becomes_source(self):
        h1, h2 = 'a' * 40, 'b' * 40
        env = FakeEnv(str(mktmp(self)))
        paths = flow.Paths(mktmp(self))
        forged = carried_row(REPO, PR, h1)
        rows = flow.genuine_rows([forged], PR, h1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'untrusted')
        self.assertEqual(core.latest_review(rows, PR, h1), None)
        env.prs[PR] = make_pr(h1)
        env.checks[h1] = [forged]
        out = run_pass(env, paths)
        self.assertNotEqual(out['decision'], 'carry')
        env.prs[PR] = make_pr(h2)
        env.checks[h2] = []
        out = run_pass(env, paths)
        self.assertNotEqual(out['decision'], 'carry', out)
        self.assertEqual(env.created, [])


class ZeroRowsOnH2(unittest.TestCase):
    """Case 4: any row on H2, or an incomplete inventory, blocks carry."""

    H1, H2 = 'a' * 40, 'b' * 40

    def test_any_row_on_h2_means_no_carry_no_publish(self):
        cases = {'other app success': make_row(200, PR, self.H2, app_id=999),
                 'queued row': make_row(200, PR, self.H2, status='queued',
                                        conclusion=None, title=None),
                 'failure row': make_row(200, PR, self.H2, title='4/5',
                                         conclusion='failure'),
                 'genuine 5/5 already there': make_row(200, PR, self.H2)}
        for name, existing in cases.items():
            with self.subTest(name):
                env = FakeEnv(str(mktmp(self)))
                paths = flow.Paths(mktmp(self))
                env.prs[PR] = make_pr(self.H2)
                env.checks[self.H2] = [existing]
                out = run_pass(env, paths)
                self.assertNotEqual(out['decision'], 'carry', out)
                self.assertEqual(env.created, [])
                self.assertEqual(list(paths.delta_dir.glob('*'))
                                   if paths.delta_dir.exists() else [], [])

    def test_row_appearing_before_publish_aborts(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            intruder = make_row(200, PR, scene['h2'], app_id=999,
                                name='CI', title='build')
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.check_queues[scene['h2']] = [[], [intruder]]
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])

    def test_checks_for_raising_means_review(self):
        env = FakeEnv(str(mktmp(self)))
        env.prs[PR] = make_pr(self.H2)
        env.raise_on_checks.add(self.H2)
        out = run_pass(env, flow.Paths(mktmp(self)))
        self.assertEqual(out['decision'], 'review')

    def test_huge_inventory_means_review(self):
        env = FakeEnv(str(mktmp(self)))
        env.prs[PR] = make_pr(self.H2)
        env.checks[self.H2] = [None] * flow.MAX_ROWS
        out = run_pass(env, flow.Paths(mktmp(self)))
        self.assertEqual(out['decision'], 'review')


class Toctou(unittest.TestCase):
    """Case 5: anything moving between decision and publish means review."""

    def scripted_pass(self, scene, prs, check_queues=None):
        env = FakeEnv(scene['repo'].dir)
        paths = flow.Paths(mktmp(self))
        observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
        env.pr_queues[PR] = prs
        env.check_queues.update(check_queues or {})
        out = run_pass(env, paths)
        return env, out

    def test_pr_head_moved(self):
        with carry_scene(self) as scene:
            steady = make_pr(scene['h2'], base_sha=scene['new_base'])
            moved = make_pr('f' * 40, base_sha=scene['new_base'])
            env, out = self.scripted_pass(
                scene, [steady, moved], {scene['h2']: [[]]})
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])

    def test_base_sha_moved(self):
        with carry_scene(self) as scene:
            steady = make_pr(scene['h2'], base_sha=scene['new_base'])
            moved = make_pr(scene['h2'], base_sha='e' * 40)
            env, out = self.scripted_pass(
                scene, [steady, moved], {scene['h2']: [[]]})
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])

    def test_base_ref_changed(self):
        with carry_scene(self) as scene:
            steady = make_pr(scene['h2'], base_sha=scene['new_base'])
            moved = make_pr(scene['h2'], base_sha=scene['new_base'],
                            base_repo=REPO)
            moved['base']['ref'] = 'other'
            env, out = self.scripted_pass(
                scene, [steady, moved], {scene['h2']: [[]]})
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])

    def test_pr_closed(self):
        with carry_scene(self) as scene:
            steady = make_pr(scene['h2'], base_sha=scene['new_base'])
            closed = make_pr(scene['h2'], base_sha=scene['new_base'],
                             state='closed')
            env, out = self.scripted_pass(
                scene, [steady, closed], {scene['h2']: [[]]})
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])

    def test_source_row_id_changed(self):
        with carry_scene(self) as scene:
            first = [make_row(100, PR, scene['h1'], completed_at=iso())]
            changed = first + [make_row(101, PR, scene['h1'],
                                        completed_at=iso())]
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            env.prs[PR] = make_pr(scene['h1'], base_sha=scene['old_base'])
            env.check_queues[scene['h1']] = [first, first, changed]
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'skip', out)
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])


class PublishFailure(unittest.TestCase):
    """Case 6: a bad receipt PATCHes to failure, reviews, never skips."""

    def carry_setup(self, scene):
        env = FakeEnv(scene['repo'].dir)
        paths = flow.Paths(mktmp(self))
        observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
        env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
        env.checks[scene['h2']] = []
        return env, paths

    def gate_over(self, rows, head):
        def sh(argv, env=None):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({'total_count': len(rows),
                                   'check_runs': rows}))
        return flow.gate_skips_review(sh, {}, REPO, PR, head, 'on')

    def test_create_raising_patches_ghost_and_reviews(self):
        with carry_scene(self) as scene:
            env, paths = self.carry_setup(scene)
            key = core.idempotency_key(REPO, PR, scene['h2'])
            ghost = {'id': 777, 'external_id': key, 'app': {'id': APP_ID}}
            env.check_queues[scene['h2']] = [[], [], [ghost]]
            env.create_behavior = 'raise'
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(len(env.patched), 1)
            check_id, payload = env.patched[0]
            self.assertEqual(check_id, 777)
            self.assertEqual(payload['conclusion'], 'failure')
            self.assertFalse(self.gate_over([], scene['h2']))

    def test_bad_receipt_fields_review_and_never_skip(self):
        variants = {'wrong App': {'app': {'id': 1}},
                    'wrong head': {'head_sha': 'd' * 40},
                    'wrong conclusion': {'conclusion': 'failure'},
                    'wrong name': {'name': 'CI'},
                    'wrong external_id': {'external_id': 'bogus'},
                    'wrong title': {'output': {'title': '5/5'}}}
        with carry_scene(self) as scene:
            for name, mutation in variants.items():
                with self.subTest(name):
                    env, paths = self.carry_setup(scene)

                    def bad(payload, mutation=mutation):
                        saved = {'id': 9001, 'name': payload['name'],
                                 'head_sha': payload['head_sha'],
                                 'status': 'completed',
                                 'conclusion': 'success',
                                 'external_id': payload['external_id'],
                                 'app': {'id': APP_ID},
                                 'output': {'title':
                                            payload['output']['title']}}
                        saved.update(mutation)
                        return saved

                    env.create_behavior = bad
                    out = run_pass(env, paths)
                    self.assertEqual(out['decision'], 'review', out)
                    self.assertEqual(len(env.patched), 1)
                    self.assertEqual(env.patched[0][0], 9001)
                    self.assertEqual(env.patched[0][1]['conclusion'],
                                     'failure')
                    _, saved = env.created[0]
                    self.assertFalse(self.gate_over([saved], scene['h2']))

    def test_receipt_without_id_reviews_and_never_skips(self):
        with carry_scene(self) as scene:
            env, paths = self.carry_setup(scene)

            def no_id(payload):
                return {'name': payload['name'],
                        'head_sha': payload['head_sha'], 'status': 'completed',
                        'conclusion': 'success',
                        'external_id': payload['external_id'],
                        'app': {'id': APP_ID},
                        'output': {'title': payload['output']['title']}}

            env.create_behavior = no_id
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            _, saved = env.created[0]
            self.assertFalse(self.gate_over([saved], scene['h2']))

    def test_null_receipt_reviews(self):
        with carry_scene(self) as scene:
            env, paths = self.carry_setup(scene)
            env.create_behavior = lambda payload: None
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            self.assertFalse(self.gate_over([], scene['h2']))


class SingleFlight(unittest.TestCase):
    """Case 7: concurrent passes serialize; sequential ones publish once."""

    H2 = 'b' * 40

    def test_second_ledger_raises_busy(self):
        paths = flow.Paths(mktmp(self))
        with flow.Ledger(paths):
            with self.assertRaises(flow.Busy):
                with flow.Ledger(paths):
                    pass

    def test_evaluate_under_lock_skips_without_publishing(self):
        env = FakeEnv(str(mktmp(self)))
        env.prs[PR] = make_pr(self.H2)
        paths = flow.Paths(mktmp(self))
        with flow.Ledger(paths):
            out = run_pass(env, paths)
        self.assertEqual(out['decision'], 'skip')
        self.assertEqual(out['reason'], 'busy')
        self.assertEqual(env.created, [])

    def test_sequential_passes_publish_exactly_once(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            env.publish_visible = True
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            first = run_pass(env, paths)
            self.assertEqual(first['decision'], 'carry', first)
            second = run_pass(env, paths)
            self.assertEqual(second['decision'], 'skip')
            self.assertEqual(second['reason'], 'already carried')
            self.assertEqual(len(env.created), 1)


class BaseSnapshots(unittest.TestCase):
    """Case 8: fresh write-once snapshots; corrupt state reviews."""

    H1, H2 = 'a' * 40, 'b' * 40

    def test_stale_snapshot_blocks_next_head(self):
        paths = flow.Paths(mktmp(self))
        with flow.Ledger(paths) as ledger:
            item = ledger.observe(REPO, PR, self.H1,
                                  {'ref': 'main', 'sha': 'c' * 40},
                                  {'id': 1, 'completed_at': iso(-1000)})
            self.assertEqual(item['kind'], 'other')
            self.assertNotIn('base', item)
            future = ledger.observe(REPO, PR, 'd' * 40,
                                    {'ref': 'main', 'sha': 'c' * 40},
                                    {'id': 2, 'completed_at': iso(3600)})
            self.assertEqual(future['kind'], 'other')
            missing = ledger.observe(REPO, PR, 'e' * 40,
                                     {'ref': 'main', 'sha': 'c' * 40},
                                     {'id': 3, 'completed_at': None})
            self.assertEqual(missing['kind'], 'other')
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'],
                         iso(-1000))
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertNotEqual(out['decision'], 'carry', out)
            self.assertEqual(env.created, [])

    def test_fresh_snapshot_allows_carry(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            with flow.Ledger(paths) as ledger:
                item = ledger.find(REPO, PR, scene['h1'])
                self.assertIsNotNone(item)
                self.assertEqual(item['kind'], 'genuine')
                self.assertEqual(item['base']['sha'], scene['old_base'])
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'carry', out)

    def test_snapshot_is_write_once(self):
        paths = flow.Paths(mktmp(self))
        with flow.Ledger(paths) as ledger:
            first = ledger.observe(REPO, PR, self.H1,
                                   {'ref': 'main', 'sha': 'c' * 40},
                                   {'id': 1, 'completed_at': iso()})
            self.assertEqual(first['base']['sha'], 'c' * 40)
            second = ledger.observe(REPO, PR, self.H1,
                                    {'ref': 'main', 'sha': 'd' * 40},
                                    {'id': 2, 'completed_at': iso()})
            self.assertEqual(second['base']['sha'], 'c' * 40)
            self.assertEqual(second['check_id'], 1)

    def test_moved_branch_never_substituted(self):
        paths = flow.Paths(mktmp(self))
        with flow.Ledger(paths) as ledger:
            first = ledger.observe(REPO, PR, self.H1,
                                   {'ref': 'main', 'sha': 'c' * 40},
                                   {'id': 1, 'completed_at': iso(-2000)})
            self.assertEqual(first['kind'], 'other')
            second = ledger.observe(REPO, PR, self.H1,
                                    {'ref': 'main', 'sha': 'd' * 40},
                                    {'id': 1, 'completed_at': iso(-2000)})
            self.assertEqual(second['kind'], 'other')
            self.assertNotIn('base', second)

    def test_corrupt_oversized_wrong_version_review_never_crash(self):
        bodies = {'corrupt': b'{"v": 1, "prs": oops',
                  'oversized': b'x' * (flow.MAX_STATE_BYTES + 1),
                  'wrong version': b'{"v": 2, "prs": {}}',
                  'prs not a mapping': b'{"v": 1, "prs": []}'}
        for name, body in bodies.items():
            with self.subTest(name):
                paths = flow.Paths(mktmp(self))
                paths.state_dir.mkdir(parents=True)
                paths.ledger.write_bytes(body)
                env = FakeEnv(str(mktmp(self)))
                env.prs[PR] = make_pr(self.H2)
                out = run_pass(env, paths)
                self.assertEqual(out['decision'], 'review')

    def test_ledger_caps_hold(self):
        paths = flow.Paths(mktmp(self))
        with flow.Ledger(paths) as ledger:
            for i in range(flow.MAX_HEADS_PER_PR + 5):
                ledger.observe(REPO, PR, f'{i:040x}',
                               {'ref': 'main', 'sha': 'c' * 40}, None)
            self.assertLessEqual(len(ledger.heads(REPO, PR)),
                                 flow.MAX_HEADS_PER_PR)
            for pr in range(1, flow.MAX_PRS + 6):
                ledger.observe(REPO, pr, self.H1,
                               {'ref': 'main', 'sha': 'c' * 40}, None)
            self.assertLessEqual(len(ledger.data['prs']), flow.MAX_PRS)


class Eligibility(unittest.TestCase):
    """Case 9: ineligible PRs skip silently; retargets review."""

    H2 = 'b' * 40

    def test_ineligible_prs_skip_and_log_nothing(self):
        base = {'head': self.H2}
        cases = {'draft': dict(base, draft=True),
                 'fork': dict(base, head_repo='Evil/paperclip-ops'),
                 'foreign repo': dict(base, base_repo='Other/repo'),
                 'org login without bot suffix': dict(
                     base, login='togetherweown', utype='User'),
                 'user named like a bot': dict(
                     base, login='evil[bot]', utype='User'),
                 'bot login with user type': dict(
                     base, login='togetherweown[bot]', utype='User')}
        for name, kwargs in cases.items():
            with self.subTest(name):
                head = kwargs.pop('head')
                env = FakeEnv(str(mktmp(self)))
                env.prs[PR] = make_pr(head, **kwargs)
                out = run_pass(env, flow.Paths(mktmp(self)))
                self.assertEqual(out['decision'], 'review', out)
                self.assertEqual(env.logs, [])
                self.assertEqual(env.created, [])

    def test_trusted_authors_pass_view(self):
        for login, utype in (('Rick7C2', 'User'),
                             ('togetherweown[bot]', 'Bot'),
                             ('github-actions[bot]', 'Bot')):
            with self.subTest(login):
                head, base = flow._pr_view(
                    make_pr(self.H2, login=login, utype=utype), REPO)
                self.assertEqual(head, self.H2)

    def test_base_ref_change_means_review(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            retarget = make_pr(scene['h2'], base_sha=scene['new_base'])
            retarget['base']['ref'] = 'other'
            env.prs[PR] = retarget
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])


class GateSkip(unittest.TestCase):
    """Case 10: the gate skips only on one verified carried check."""

    H2 = 'b' * 40

    def gate(self, rows, total=None, returncode=0, stdout=None, mode='on',
             head=None, raises=None):
        def sh(argv, env=None):
            if raises is not None:
                raise raises
            body = stdout if stdout is not None else json.dumps(
                {'total_count': len(rows) if total is None else total,
                 'check_runs': rows})
            return SimpleNamespace(returncode=returncode, stdout=body)
        return flow.gate_skips_review(sh, {}, REPO, PR,
                                      self.H2 if head is None else head, mode)

    def test_exactly_one_verified_carry_skips(self):
        self.assertTrue(self.gate([carried_row(REPO, PR, self.H2)]))

    def test_any_doubt_does_not_skip(self):
        good = carried_row(REPO, PR, self.H2)
        other = make_row(201, PR, self.H2, app_id=999, name='CI')
        cases = {'mode shadow': dict(rows=[good], mode='shadow'),
                 'mode off': dict(rows=[good], mode='off'),
                 'zero rows': dict(rows=[]),
                 'second row of any kind': dict(rows=[good, other]),
                 'failure conclusion': dict(rows=[carried_row(
                     REPO, PR, self.H2, conclusion='failure')]),
                 'in-progress status': dict(rows=[carried_row(
                     REPO, PR, self.H2, status='in_progress',
                     conclusion=None)]),
                 'wrong external_id': dict(rows=[carried_row(
                     REPO, PR, self.H2, external_id='bogus')]),
                 'wrong App': dict(rows=[carried_row(
                     REPO, PR, self.H2, app={'id': 1})]),
                 'wrong head': dict(rows=[carried_row(
                     REPO, PR, self.H2, head_sha='d' * 40)]),
                 'genuine title not carried': dict(rows=[carried_row(
                     REPO, PR, self.H2,
                     output={'title': '5/5'})]),
                 'total_count mismatch': dict(rows=[good], total=2),
                 'non-zero returncode': dict(rows=[good], returncode=1),
                 'non-JSON stdout': dict(rows=[good], stdout='oops'),
                 'bad head sha': dict(rows=[good], head='zzz')}
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assertFalse(self.gate(**kwargs))

    def test_sh_raising_does_not_skip(self):
        self.assertFalse(self.gate([carried_row(REPO, PR, self.H2)],
                                   raises=RuntimeError('boom')))


class ShadowPublishesNothing(unittest.TestCase):
    """Case 11: shadow is log-only; off does not even touch the ledger."""

    def test_shadow_carry_path_logs_once_and_writes_nothing(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            env.prs[PR] = make_pr(scene['h1'], base_sha=scene['old_base'])
            env.checks[scene['h1']] = [make_row(100, PR, scene['h1'],
                                                completed_at=env.now)]
            out = flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            self.assertEqual(out['decision'], 'skip', out)
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            self.assertEqual(out['decision'], 'would-carry', out)
            self.assertEqual(env.created, [])
            self.assertFalse(any(paths.delta_dir.glob('*'))
                             if paths.delta_dir.exists() else False)
            self.assertEqual(len(env.logs), 1)
            self.assertIn('would-carry', env.logs[0])
            again = flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            self.assertEqual(again['decision'], 'would-carry')
            self.assertEqual(len(env.logs), 1)

    def test_shadow_review_path_logs_once_and_writes_nothing(self):
        def change(repo):
            path = os.path.join(repo.dir, 'feature.py')
            with open(path, 'w') as fh:
                fh.write("def f():\n    return 3\n")
            commit_all(repo, 'change line')

        with delta_scene(self, change) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            env.prs[PR] = make_pr(scene['h1'], base_sha=scene['old_base'])
            env.checks[scene['h1']] = [make_row(100, PR, scene['h1'],
                                                completed_at=env.now)]
            out = flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            self.assertEqual(out['decision'], 'skip', out)
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            self.assertEqual(out['decision'], 'review', out)
            self.assertEqual(env.created, [])
            self.assertFalse(paths.delta_dir.exists()
                             and any(paths.delta_dir.iterdir()))
            self.assertEqual(len(env.logs), 1)
            self.assertIn('would-review', env.logs[0])
            again = flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            self.assertEqual(again['decision'], 'review')
            self.assertEqual(len(env.logs), 1)

    def test_off_touches_nothing(self):
        env = FakeEnv(str(mktmp(self)))
        env.prs[PR] = make_pr('b' * 40)
        paths = flow.Paths(mktmp(self))
        out = flow.evaluate_pr(env, paths, REPO, PR, 'off')
        self.assertEqual(out, {'decision': 'off', 'reason': 'disabled'})
        self.assertFalse(paths.ledger.exists())
        self.assertFalse(paths.delta_dir.exists())
        self.assertEqual(env.logs, [])
        self.assertEqual(env.created, [])


class DeltaRecord(unittest.TestCase):
    """Case 12: exact own-patch delta; scoped union or None on doubt."""

    def review_paths(self, scene):
        env = FakeEnv(scene['repo'].dir)
        paths = flow.Paths(mktmp(self))
        observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
        env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
        env.checks[scene['h2']] = []
        out = run_pass(env, paths)
        self.assertEqual(out['decision'], 'review', out)
        return env, paths

    def written_record(self, paths):
        files = list(paths.delta_dir.glob('*.json'))
        self.assertEqual(len(files), 1, [p.name for p in files])
        return json.loads(files[0].read_text())

    def test_changed_line_writes_exact_paths(self):
        def change(repo):
            path = os.path.join(repo.dir, 'feature.py')
            with open(path, 'w') as fh:
                fh.write("def f():\n    return 3\n")
            commit_all(repo, 'change line')

        with delta_scene(self, change) as scene:
            _, paths = self.review_paths(scene)
            record = self.written_record(paths)
            self.assertEqual(record['files'], ['feature.py'])
            self.assertEqual(record['mode'], 'on')
            self.assertEqual(record['head'], scene['h2'])
            self.assertEqual(record['source_head'], scene['h1'])

    def test_added_commit_writes_new_paths(self):
        def change(repo):
            repo.commit('added.py', "x = 1\n", 'add file')

        with delta_scene(self, change) as scene:
            _, paths = self.review_paths(scene)
            self.assertEqual(self.written_record(paths)['files'],
                             ['added.py'])

    def test_dropped_commit_writes_changed_paths(self):
        repo = GitRepo()
        repo.__enter__()
        self.addCleanup(repo.__exit__, None, None, None)
        old_base, h1, a, _ = seed_pr(repo)
        new_base = advance_main(repo)
        h2 = cherry_pick(self, repo, 'pr2', new_base, a)
        scene = {'repo': repo, 'old_base': old_base, 'new_base': new_base,
                 'h1': h1, 'h2': h2}
        _, paths = self.review_paths(scene)
        self.assertEqual(self.written_record(paths)['files'], ['feature.py'])

    def test_mode_change_writes_path(self):
        def change(repo):
            os.chmod(os.path.join(repo.dir, 'feature.py'), 0o755)
            commit_all(repo, 'make executable')

        with delta_scene(self, change) as scene:
            _, paths = self.review_paths(scene)
            self.assertEqual(self.written_record(paths)['files'],
                             ['feature.py'])

    def test_binary_change_writes_path(self):
        repo = GitRepo()
        repo.__enter__()
        self.addCleanup(repo.__exit__, None, None, None)
        repo.commit('app.py', "print('base')\n", 'base')
        old_base = repo.head()
        git(repo.dir, 'checkout', '-q', '-b', 'pr')
        repo.commit('logo.bin', b'\x00\x01\x02\x03', 'add binary')
        h1 = repo.head()
        new_base = advance_main(repo)
        git(repo.dir, 'checkout', '-q', '-b', 'pr2', new_base)
        proc = run_git(repo.dir, 'cherry-pick', h1)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:400])
        with open(os.path.join(repo.dir, 'logo.bin'), 'wb') as fh:
            fh.write(b'\x00\x01\x02\x04')
        commit_all(repo, 'change binary')
        scene = {'repo': repo, 'old_base': old_base, 'new_base': new_base,
                 'h1': h1, 'h2': repo.head()}
        _, paths = self.review_paths(scene)
        self.assertEqual(self.written_record(paths)['files'], ['logo.bin'])

    def test_removed_file_included(self):
        def change(repo):
            git(repo.dir, 'rm', '-q', 'feature.py')
            git(repo.dir, 'commit', '-q', '-m', 'remove feature')

        with delta_scene(self, change) as scene:
            _, paths = self.review_paths(scene)
            self.assertEqual(self.written_record(paths)['files'],
                             ['feature.py'])

    def test_special_paths_round_trip(self):
        names = ['sp ace.py', 'quo"te.py', 'ünïcödé.py']
        repo = GitRepo()
        repo.__enter__()
        self.addCleanup(repo.__exit__, None, None, None)
        repo.commit('app.py', "print('base')\n", 'base')
        old_base = repo.head()
        git(repo.dir, 'checkout', '-q', '-b', 'pr')
        for name in names:
            repo.commit(name, 'v1\n', f'add {name}')
        h1 = repo.head()
        new_base = advance_main(repo)
        git(repo.dir, 'checkout', '-q', '-b', 'pr2', new_base)
        for sha in git(repo.dir, 'rev-list', '--reverse',
                       f'{old_base}..{h1}').split():
            proc = run_git(repo.dir, 'cherry-pick', sha)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:400])
        for name in names:
            with open(os.path.join(repo.dir, name), 'w') as fh:
                fh.write('v2\n')
        commit_all(repo, 'change special paths')
        scene = {'repo': repo, 'old_base': old_base, 'new_base': new_base,
                 'h1': h1, 'h2': repo.head()}
        _, paths = self.review_paths(scene)
        self.assertEqual(self.written_record(paths)['files'], sorted(names))

    def write_record(self, paths, head, source, files, blocking=(),
                     mode='on', repo=REPO, pr=PR):
        record = {'v': 1, 'mode': mode, 'repo': repo.lower(), 'pr': pr,
                  'head': head, 'source_head': source,
                  'old_base': {'ref': 'main', 'sha': 'c' * 40},
                  'new_base': {'ref': 'main', 'sha': 'd' * 40},
                  'files': files}
        flow.write_delta(paths, record)
        return record

    def test_scoped_delta_unions_files_and_blockers(self):
        h1, h2 = 'a' * 40, 'b' * 40
        paths = flow.Paths(mktmp(self))
        self.write_record(paths, h2, h1, ['b.py', 'a.py'])
        out = flow.scoped_delta(paths, 'on', REPO, PR, h1, h2,
                                [{'path': 'c.py'}, {'path': 'a.py'}])
        self.assertEqual(out, ['a.py', 'b.py', 'c.py'])

    def test_scoped_delta_none_on_any_doubt(self):
        h1, h2 = 'a' * 40, 'b' * 40
        # Same 12-char filename prefix, different full head: the record
        # file is found but its head field must not match.
        h2_same_prefix = h2[:12] + 'f' * 28
        good_blocking = [{'path': 'c.py'}]

        def setup(kind):
            paths = flow.Paths(mktmp(self))
            files = ['a.py']
            mode, repo, pr, head, source = 'on', REPO, PR, h2, h1
            blocking = good_blocking
            if kind == 'missing file':
                return paths, mode, repo, pr, source, head, blocking
            if kind == 'mode not on':
                mode = 'shadow'
            elif kind == 'wrong repo':
                repo = 'Other/repo'
            elif kind == 'wrong pr':
                pr = 999
            elif kind == 'wrong head':
                head = h2_same_prefix
            elif kind == 'source mismatch':
                source = 'e' * 40
            elif kind == 'empty files':
                files = []
            elif kind == 'oversized files':
                files = [f'f{i}.py' for i in range(flow.MAX_DELTA_FILES + 1)]
            elif kind == 'non-list files':
                files = 'a.py'
            elif kind == 'absolute path':
                files = ['/etc/passwd']
            elif kind == 'dotdot path':
                files = ['a/../../x.py']
            elif kind == 'empty segment':
                files = ['a//b.py']
            elif kind == 'blocking missing path':
                blocking = [{}]
            elif kind == 'blocking invalid path':
                blocking = [{'path': '/abs.py'}]
            elif kind == 'blocking not a list':
                blocking = {'path': 'c.py'}
            elif kind == 'blocking not mappings':
                blocking = ['c.py']
            # The record is always written under the true head; the
            # 'wrong head' query reuses its filename prefix.
            self.write_record(paths, h2, h1, files)
            if kind == 'malformed JSON':
                target = next(paths.delta_dir.glob('*.json'))
                target.write_text('{oops')
            elif kind == 'unreadable file':
                target = next(paths.delta_dir.glob('*.json'))
                target.unlink()
                target.mkdir()
                self.addCleanup(shutil.rmtree, target, True)
            return paths, mode, repo, pr, source, head, blocking

        kinds = ['mode not on', 'missing file', 'wrong repo', 'wrong pr',
                 'wrong head', 'source mismatch', 'empty files',
                 'oversized files', 'non-list files', 'absolute path',
                 'dotdot path', 'empty segment', 'blocking missing path',
                 'blocking invalid path', 'blocking not a list',
                 'blocking not mappings', 'malformed JSON', 'unreadable file']
        for kind in kinds:
            with self.subTest(kind):
                paths, mode, repo, pr, source, head, blocking = setup(kind)
                self.assertIsNone(flow.scoped_delta(
                    paths, mode, repo, pr, source, head, blocking))

    def test_scoped_delta_never_narrower_never_empty(self):
        h1, h2 = 'a' * 40, 'b' * 40
        paths = flow.Paths(mktmp(self))
        self.write_record(paths, h2, h1, ['a.py'])
        out = flow.scoped_delta(paths, 'on', REPO, PR, h1, h2, [])
        self.assertEqual(out, ['a.py'])
        self.assertTrue(out)


class AuditLines(unittest.TestCase):
    """Case 13: complete, neutralized, bounded lines; log never decides."""

    def assert_full_line(self, line, event, h1, h2, old_base, new_base,
                         digest_rx=r'[0-9a-f]{64}'):
        self.assertIn(f'rebase-carry {event}', line)
        self.assertIn(f'repo={REPO}', line)
        self.assertIn(f'pr={PR}', line)
        self.assertIn(f'old_head={h1}', line)
        self.assertIn(f'new_head={h2}', line)
        self.assertIn(f'old_base=main@{old_base}', line)
        self.assertIn(f'new_base=main@{new_base}', line)
        self.assertRegex(line, rf'digest={digest_rx}')
        self.assertIn('reason=', line)
        self.assertIn(UNDO_TEXT, line)
        self.assertNotIn('\n', line)

    def test_carry_line_is_complete(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'carry', out)
            self.assertEqual(len(env.logs), 1)
            self.assert_full_line(env.logs[0], 'carried', scene['h1'],
                                  scene['h2'], scene['old_base'],
                                  scene['new_base'])

    def test_shadow_lines_are_complete(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            env.prs[PR] = make_pr(scene['h1'], base_sha=scene['old_base'])
            env.checks[scene['h1']] = [make_row(100, PR, scene['h1'],
                                                completed_at=env.now)]
            flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = flow.evaluate_pr(env, paths, REPO, PR, 'shadow')
            self.assertEqual(out['decision'], 'would-carry', out)
            self.assert_full_line(env.logs[-1], 'would-carry', scene['h1'],
                                  scene['h2'], scene['old_base'],
                                  scene['new_base'])

    def test_review_required_line_is_complete(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            env.create_behavior = 'raise'
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'review', out)
            self.assert_full_line(env.logs[-1], 'review-required',
                                  scene['h1'], scene['h2'], scene['old_base'],
                                  scene['new_base'])

    def test_control_characters_and_secrets_neutralized(self):
        lines = []
        line = flow.audit(
            lines.append, 'carried', REPO, PR, 'a' * 40, 'b' * 40,
            {'ref': 'main', 'sha': 'c' * 40},
            {'ref': 'main', 'sha': 'd' * 40}, 'e' * 64,
            'leak\nBearer ghp_deadbeef\x00\x1b`$"x";|\\end')
        self.assertEqual(lines, [line])
        self.assertNotIn('\n', line)
        # The fixed UNDO suffix legitimately contains ';'; the hostile
        # characters must be gone from the caller-controlled reason part.
        reason = line.split('reason=', 1)[1].split('. UNDO:', 1)[0]
        for bad in ('\x00', '\x1b', '`', '$', '"', ';', '|', '\\'):
            self.assertNotIn(bad, reason)
        self.assertTrue(all(ord(c) >= 32 for c in line))
        self.assertIn(UNDO_TEXT, line)

    def test_line_is_bounded(self):
        lines = []
        line = flow.audit(lines.append, 'review-required', 'r' * 500, PR,
                          'a' * 200, 'b' * 200,
                          {'ref': 'm' * 500, 'sha': 'c' * 40},
                          {'ref': 'm' * 500, 'sha': 'd' * 40}, 'e' * 500,
                          'x' * 10000)
        self.assertLess(len(line), 1500)
        self.assertIn(UNDO_TEXT, line)

    def test_raising_log_does_not_change_decision(self):
        with carry_scene(self) as scene:
            env = FakeEnv(scene['repo'].dir)
            env.log_raises = True
            paths = flow.Paths(mktmp(self))
            observe_head(env, paths, scene['h1'], scene['old_base'], env.now)
            env.prs[PR] = make_pr(scene['h2'], base_sha=scene['new_base'])
            env.checks[scene['h2']] = []
            out = run_pass(env, paths)
            self.assertEqual(out['decision'], 'carry', out)
            self.assertEqual(len(env.created), 1)


class NoCredentialMaterial(unittest.TestCase):
    """Case 14: no secret-shaped tokens or credential imports ship."""

    FORBIDDEN = ['Authorization', 'Bearer', 'secure-drop', 'ghp_',
                 'github_pat_', 'mint']
    # '.env' is matched as a file reference (quoted or path), not a bare
    # substring: the module legitimately reads os.environ for the control
    # knob, and a bare '.env' would false-positive on that stdlib name.
    DOTENV_RE = re.compile(r'''["']\.env["']|/\.env(\b|["'])''')
    IMPORT_RE = re.compile(
        r'\b(import|from)\s+[\w.]*\b(sdlc_lib|review_carry|docs_lane)\b')

    def sources(self):
        module = Path(flow.__file__).read_text()
        patches = {name: (PATCH_DIR / name).read_text()
                   for name in PINNED.values()}
        return {'module': module, **patches}

    def test_no_secret_tokens(self):
        for name, text in self.sources().items():
            with self.subTest(name):
                for token in self.FORBIDDEN:
                    self.assertNotIn(token, text)
                self.assertIsNone(self.DOTENV_RE.search(text))

    def test_no_credential_imports(self):
        for name, text in self.sources().items():
            with self.subTest(name):
                self.assertIsNone(self.IMPORT_RE.search(text))


class PacketShape(unittest.TestCase):
    """Case 15: one-file -p1 patches, anchored, executable-free."""

    def test_each_patch_targets_exactly_one_pinned_file(self):
        seen = {}
        for pinned, name in PINNED.items():
            with self.subTest(name):
                text = (PATCH_DIR / name).read_text()
                old, new, hunks = parse_patch(text)
                self.assertTrue(hunks)
                self.assertTrue(old.startswith('a/'), old)
                self.assertTrue(new.startswith('b/'), new)
                starts = text.splitlines()
                self.assertEqual(
                    sum(1 for l in starts if l.startswith('--- a/')), 1)
                self.assertEqual(
                    sum(1 for l in starts if l.startswith('+++ b/')), 1)
                self.assertEqual(old[2:], pinned)
                self.assertEqual(new[2:], pinned)
                seen[pinned] = name
        self.assertEqual(set(seen), set(PINNED))

    def test_no_new_executable_path(self):
        for name in PINNED.values():
            with self.subTest(name):
                text = (PATCH_DIR / name).read_text()
                self.assertNotIn('new file mode', text)
                self.assertNotIn('100755', text)

    def test_patches_apply_fuzz0_to_synthetic_context(self):
        if shutil.which('patch') is None:
            self.skipTest('patch(1) unavailable')
        for pinned, name in PINNED.items():
            with self.subTest(name):
                text = (PATCH_DIR / name).read_text()
                _, _, hunks = parse_patch(text)
                lines, cursor = [], 1
                for start, body in hunks:
                    while cursor < start:
                        lines.append(f'synthetic anchor padding {cursor}')
                        cursor += 1
                    for tag, content in body:
                        if tag in (' ', '-'):
                            lines.append(content)
                            cursor += 1
                root = mktmp(self)
                target = root / pinned
                target.parent.mkdir(parents=True)
                target.write_text('\n'.join(lines) + '\n')
                patch_file = root / name
                patch_file.write_text(text)
                proc = subprocess.run(
                    ['patch', '--dry-run', '--fuzz=0', '-p1',
                     '-i', str(patch_file)],
                    cwd=root, capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_gate_patch_inserts_skip_after_guard_before_key(self):
        text = (PATCH_DIR / PINNED['ops/productivity/review_gate.py'])
        text = text.read_text()
        _, _, hunks = parse_patch(text)
        for _, body in hunks:
            tags = [(tag, content) for tag, content in body]
            added = [c for t, c in tags if t == '+']
            if any('gate_skips_review' in c for c in added):
                order = [c for _, c in tags]
                first_added = min(order.index(c) for c in added
                                  if 'gate_skips_review' in c)
                key_at = next(i for i, c in enumerate(order)
                              if c.strip().startswith('key ='))
                guard_at = next(i for i, c in enumerate(order)
                                if c.strip() == 'continue')
                self.assertLess(guard_at, first_added)
                self.assertLess(first_added, key_at)
                call = next(c for c in added if 'gate_skips_review' in c)
                self.assertIn('rebase_mode', call)
                break
        else:
            self.fail('gate patch adds no gate_skips_review call')

    def test_findings_patch_classifies_gate_paths_high(self):
        text = (PATCH_DIR / PINNED['ops/productivity/review_findings.py'])
        text = text.read_text()
        _, _, hunks = parse_patch(text)
        for _, body in hunks:
            added = [c for t, c in body if t == '+']
            entry = next((c for c in added if 'rebase_carry' in c
                          and 'review_gate' in c), None)
            if entry is not None:
                before = [c for t, c in body if t in (' ', '-')]
                self.assertTrue(any('data-loss|destruct' in c for c in before))
                match = re.search(r"r'([^']+)'", entry)
                self.assertIsNotNone(match, entry)
                rx = re.compile(match.group(1))
                self.assertTrue(rx.search(
                    'ops/productivity/review_rebase_carry_flow.py'))
                self.assertTrue(rx.search('ops/sdlc/review_carry.py'))
                self.assertIsNone(rx.search('ops/paperclip/foo.py'))
                break
        else:
            self.fail('findings patch adds no HIGH_PATHS entry')
