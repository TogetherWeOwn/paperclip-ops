#!/usr/bin/env python3
"""Immutable acceptance: dispatch staged lifecycle (bounded smoke + serialize).

Bundle: dispatch-staged-lifecycle. Public entrypoint: scripts/dispatch_deploy.sh.
Consumer: AI agents. Status: IMMUTABLE ACCEPTANCE - Builder must not edit.

Contract pinned (build must satisfy, tests must not change runtime):
- Bounded staged smoke by validated finite deadline, bounded TERM-to-KILL
  escalation, bounded retained output. Stuck/noisy child cannot hang wrapper
  indefinitely or consume unbounded captured memory. Timeout/nonzero refuses
  before live HEAD, anchor refs or deployment record change; stable reason
  without exposing arbitrary smoke output in structured errors.
- Owned temp worktree + registration cleanup on normal failure, timeout and
  catchable INT/TERM. Reap invocation's smoke descendants, remove only own
  temp resources; never remove another worktree, never global destructive
  prune, never claim SIGKILL runs a trap. Failed cleanup observable nonzero.
- Serialize deploy and rollback for same target with one shared transaction
  lock acquired before reading previous HEAD or selecting implicit rollback
  SHA, then revalidate clean-tree/object guards under lock. Contention refuses
  or waits only within documented finite bound. Concurrent fixtures leave HEAD,
  anchors, record coherent; different explicit test targets do not block.
- Versioned --json outcomes with stable stage/error codes, target/commit IDs,
  retryable, next_action, preserving text + documented exits. --dry-run as
  static no-write plan before lock/worktree/ref/record/smoke; reject
  conflicting/missing/unknown flags before mutation. Keep test-only target
  override, explicit full commit identity, no-fetch, staged-smoke-before-live,
  atomic record publication.

Pinned CLI surface (Builder implements exactly):
- --smoke-timeout <seconds>: finite positive integer, 1..600. Invalid rejects
  exit 2 before mutation with stable validation message containing
  "smoke-timeout". Default when flag absent: finite (<=10s in fixtures, <=60s
  documented for production). TERM escalation to KILL within +5s.
- --json: stdout is single versioned JSON object (schemaVersion 1), stderr
  retains human text. Exit meanings preserved (0 ok, 1 drift, 2 refused).
  Required fields: schemaVersion, command, stage, exitCode, target, retryable,
  next_action. Deploy/rollback add deployedSha/previousSha (or commit). Error
  adds error.code (snake_case, stable), error.message (stable reason, no raw
  smoke output). outputTail (if present) <=8k.
- --dry-run: exit 0 plan to stdout (text) or JSON (with --json), zero writes,
  smoke never executed. Rejects misplaced flags (e.g. status --commit).
- --help/-h documents --json/--dry-run/--smoke-timeout, lock, timeout,
  cleanup, exit codes, with concise examples.

Expected on current source (head 1123a887, deploy source 9aca858f):
- RED (intentional, deliverable): bounded smoke, timeout/INT/TERM cleanup,
  serialization of same-target concurrent transactions, --json/--dry-run/help.
- GREEN (positive controls): successful deploy/status/rollback, failing smoke
  leaves HEAD/record/anchors untouched + no leak, explicit identity, no-fetch
  refusal, dirty-tree refusal, missing smoke refusal, unknown-flag zero writes,
  independent targets, sibling preservation, atomic record JSON, exit codes
  without --json, no SIGKILL trap claim.

Safety: only temp git repos beneath run-owned fixture root, always with
DISPATCH_DEPLOY_TEST_MODE=1 and explicit fixture targets. Bounded fixture
subprocesses with outer watchdogs. Git shim fails closed on DEFAULT_TARGET or
paths outside fixture root. Hard fail-safe asserts live
/paperclip/plugin-packages-root/dispatch-src untouched. Never run against
deployed plugin or production. Never live network/host/service.

Run:
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_dispatch_staged_lifecycle
  bash test_dispatch_deploy.sh   # with run-owned TMPDIR
with run-owned TMPDIR.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "scripts" / "dispatch_deploy.sh"
DEFAULT_TARGET = "/paperclip/plugin-packages-root/dispatch-src"
REF_CURRENT = "refs/dispatch-deploy/current"
REF_ROLLBACK = "refs/dispatch-deploy/rollback"

# Pinned bounds (fixtures use tight values; production documents <=60s default).
SMOKE_SLEEP_STUCK = 15          # stuck fixture sleep (s)
EXPECTED_DEFAULT_DEADLINE = 10  # wrapper must kill stuck smoke within this (s)
OUTER_WATCHDOG = 14             # outer fail-safe for stuck tests (s)
ESCALATION_OUTER = 16           # TERM-ignore outer (s)
INT_TERM_OUTER = 12             # INT/TERM wrapper exit bound (s)
CONCURRENCY_OUTER = 30          # concurrent transactions bound (s)
QUICK_OUTER = 12                # normal commands bound (s)
STDERR_BOUND = 32 * 1024        # failing-smoke wrapper stderr must be < this
TAIL_BOUND = 8 * 1024           # record outputTail / JSON tail must be <= this
NOISY_BYTES = 2_000_000         # noisy fixture output (2MB)
JSON_SCHEMA_VERSION = 1


def _real_git():
    # Prefer the ELF system git, bypassing any Paperclip launcher shims on
    # PATH. A PATH shim named `git` that execs a launcher would recurse
    # (launcher re-resolves `git` via PATH and finds the shim again).
    for cand in ("/usr/bin/git", "/bin/git"):
        try:
            with open(cand, "rb") as handle:
                if handle.read(4) == b"\x7fELF":
                    return cand
        except OSError:
            continue
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "git")
        try:
            with open(candidate, "rb") as handle:
                if handle.read(4) == b"\x7fELF":
                    return candidate
        except OSError:
            continue
    path = shutil.which("git")
    assert path, "git not found on PATH"
    return path


REAL_GIT = _real_git()


def _git_env(extra=None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    if extra:
        env.update(extra)
    return env


class DispatchLifecycleBase(unittest.TestCase):
    """Fixture root, fail-closed git shim, bounded wrapper runner."""

    def setUp(self):
        self.fixture_root = Path(tempfile.mkdtemp(prefix="dispatch-lifecycle-"))
        self.addCleanup(shutil.rmtree, str(self.fixture_root), True)
        # Hard fail-safe: fixture root must never be default target or parent.
        self.assertNotEqual(str(self.fixture_root), DEFAULT_TARGET)
        self.assertFalse(
            DEFAULT_TARGET.startswith(str(self.fixture_root) + os.sep),
            "fixture root must not contain default target",
        )
        self.assertFalse(
            str(self.fixture_root).startswith(DEFAULT_TARGET + os.sep),
            "fixture root must not be inside live default target",
        )
        # Record live target state (if present) to prove untouched.
        self._live_state = self._snapshot_live()
        # Fail-closed git shim: refuses DEFAULT_TARGET or paths outside root.
        self.shim_dir = self.fixture_root / "bin"
        self.shim_dir.mkdir()
        shim = self.shim_dir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            f'REAL_GIT="{REAL_GIT}"\n'
            f'FIXTURE_ROOT="{self.fixture_root}"\n'
            f'DEFAULT_TARGET="{DEFAULT_TARGET}"\n'
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            '    "$DEFAULT_TARGET"|"$DEFAULT_TARGET"/*)\n'
            '      echo "FAIL-CLOSED: refused live default target: $arg" >&2; exit 99;;\n'
            '  esac\n'
            'done\n'
            'prev=""\n'
            'for arg in "$@"; do\n'
            '  if [ "$prev" = "-C" ]; then\n'
            '    case "$arg" in\n'
            '      "$FIXTURE_ROOT"/*) ;;\n'
            '      *) echo "FAIL-CLOSED: git -C outside fixture root: $arg" >&2; exit 99;;\n'
            '    esac\n'
            '  fi\n'
            '  prev="$arg"\n'
            'done\n'
            'exec "$REAL_GIT" "$@"\n'
        )
        shim.chmod(0o755)
        self._live_children = []

    def tearDown(self):
        # Reap any lingering fixture children we tracked.
        for pid in self._live_children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        # Hard fail-safe: live target untouched.
        try:
            after = self._snapshot_live()
        except Exception as exc:  # pragma: no cover - fail closed
            self.fail(f"live target snapshot failed: {exc}")
        self.assertEqual(after, self._live_state, "live default target was touched")

    def _snapshot_live(self):
        p = Path(DEFAULT_TARGET)
        if not p.exists():
            return ("absent",)
        git = p / ".git"
        if git.exists():
            try:
                head = subprocess.run(
                    [REAL_GIT, "-C", DEFAULT_TARGET, "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                    env=_git_env(),
                )
                cur = subprocess.run(
                    [REAL_GIT, "-C", DEFAULT_TARGET, "for-each-ref",
                     "--format=%(objectname)", REF_CURRENT],
                    capture_output=True, text=True, timeout=5, env=_git_env(),
                )
                rb = subprocess.run(
                    [REAL_GIT, "-C", DEFAULT_TARGET, "for-each-ref",
                     "--format=%(objectname)", REF_ROLLBACK],
                    capture_output=True, text=True, timeout=5, env=_git_env(),
                )
                rec = Path(DEFAULT_TARGET + ".deployment.json")
                rec_bytes = rec.read_bytes() if rec.exists() else b"<absent>"
                return ("git", head.stdout.strip(), cur.stdout.strip(),
                        rb.stdout.strip(), rec_bytes)
            except Exception:
                st = p.stat()
                return ("git-unreadable", st.st_mtime_ns, st.st_size)
        # Non-git live path: record listing signature without reading contents.
        try:
            names = sorted(x.name for x in p.iterdir())
            st = p.stat()
            return ("dir", tuple(names[:50]), len(names), st.st_mtime_ns)
        except Exception:
            return ("unreadable",)

    # -- repo fixtures ----------------------------------------------------
    def make_target(self, name="target", commits=3):
        target = self.fixture_root / name
        target.mkdir(parents=True)
        env = _git_env()
        subprocess.run([REAL_GIT, "-C", str(target), "init", "-q"],
                       check=True, env=env, timeout=10)
        subprocess.run([REAL_GIT, "-C", str(target), "config", "user.name", "t"],
                       check=True, env=env, timeout=10)
        subprocess.run([REAL_GIT, "-C", str(target), "config", "user.email",
                        "t@example.invalid"], check=True, env=env, timeout=10)
        shas = []
        for i in range(commits):
            (target / "f").write_text(f"v{i}\n")
            subprocess.run([REAL_GIT, "-C", str(target), "add", "f"],
                           check=True, env=env, timeout=10)
            subprocess.run([REAL_GIT, "-C", str(target), "commit", "-qm", f"v{i}"],
                           check=True, env=env, timeout=10)
            out = subprocess.run([REAL_GIT, "-C", str(target), "rev-parse", "HEAD"],
                                 check=True, capture_output=True, text=True,
                                 env=env, timeout=10)
            shas.append(out.stdout.strip())
        # Detach at first commit, anchor both refs there (mirrors shell suite).
        subprocess.run([REAL_GIT, "-C", str(target), "checkout", "-q",
                        "--detach", shas[0]], check=True, env=env, timeout=10)
        subprocess.run([REAL_GIT, "-C", str(target), "update-ref",
                        REF_CURRENT, shas[0]], check=True, env=env, timeout=10)
        subprocess.run([REAL_GIT, "-C", str(target), "update-ref",
                        REF_ROLLBACK, shas[0]], check=True, env=env, timeout=10)
        return target, shas

    def git(self, target, *args, timeout=10):
        self._assert_fixture_target(target)
        return subprocess.run(
            [REAL_GIT, "-C", str(target), *args],
            capture_output=True, text=True, timeout=timeout, env=_git_env(),
        )

    def _assert_fixture_target(self, target):
        s = str(target)
        self.assertTrue(s.startswith(str(self.fixture_root) + os.sep),
                        f"target outside fixture root: {s}")
        self.assertNotEqual(s, DEFAULT_TARGET)

    def head(self, target):
        r = self.git(target, "rev-parse", "HEAD")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def anchor(self, target, ref):
        r = self.git(target, "for-each-ref", "--format=%(objectname)", ref)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def record_bytes(self, target):
        p = Path(str(target) + ".deployment.json")
        return p.read_bytes() if p.exists() else None

    def worktrees(self, target):
        r = self.git(target, "worktree", "list", "--porcelain")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = []
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                out.append(line[len("worktree "):])
        return out

    def leaked_registrations(self, target):
        main = str(target)
        return [w for w in self.worktrees(target) if w != main]

    # -- wrapper runner ---------------------------------------------------
    def wrapper_env(self, target, extra=None):
        self._assert_fixture_target(target)
        env = _git_env({
            "PATH": str(self.shim_dir) + os.pathsep + os.environ.get("PATH", ""),
            "DISPATCH_DEPLOY_TEST_MODE": "1",
            "DISPATCH_DEPLOY_TARGET": str(target),
            "TMPDIR": str(self.fixture_root / "tmp"),
        })
        (self.fixture_root / "tmp").mkdir(exist_ok=True)
        if extra:
            env.update(extra)
        return env

    def run_wrapper(self, target, args, timeout=QUICK_OUTER, extra_env=None):
        """Run actual entrypoint with outer watchdog, new process group."""
        env = self.wrapper_env(target, extra_env)
        proc = subprocess.Popen(
            ["bash", str(DEPLOY), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(
                proc.args, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = "", ""
            raise

    def start_wrapper(self, target, args, extra_env=None):
        env = self.wrapper_env(target, extra_env)
        return subprocess.Popen(
            ["bash", str(DEPLOY), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, start_new_session=True,
        )

    def kill_group(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass

    def pid_alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def wait_for_file(self, path, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if Path(path).exists():
                return True
            time.sleep(0.05)
        return False


class PositiveControls(DispatchLifecycleBase):
    """Current valid behavior; must stay GREEN before and after build."""

    def test_deploy_success_moves_head_and_record(self):
        target, shas = self.make_target("happy")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEPLOYED", r.stdout)
        self.assertEqual(self.head(target), shas[1])
        self.assertEqual(self.anchor(target, REF_CURRENT), shas[1])
        self.assertEqual(self.anchor(target, REF_ROLLBACK), shas[0])
        raw = self.record_bytes(target)
        self.assertIsNotNone(raw)
        data = json.loads(raw.decode())
        self.assertEqual(data["deployedSha"], shas[1])
        self.assertEqual(data["previousSha"], shas[0])
        self.assertEqual(self.leaked_registrations(target), [])

    def test_failing_smoke_leaves_head_record_anchors_untouched(self):
        target, shas = self.make_target("failing")
        before_head = self.head(target)
        before_cur = self.anchor(target, REF_CURRENT)
        before_rb = self.anchor(target, REF_ROLLBACK)
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "false"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("smoke check failed", r.stderr)
        self.assertEqual(self.head(target), before_head)
        self.assertEqual(self.anchor(target, REF_CURRENT), before_cur)
        self.assertEqual(self.anchor(target, REF_ROLLBACK), before_rb)
        self.assertIsNone(self.record_bytes(target))
        self.assertEqual(self.leaked_registrations(target), [])

    def test_rollback_restores_anchor(self):
        target, shas = self.make_target("rollback")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_wrapper(target, ["rollback", "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(target), shas[0])

    def test_status_ok_and_drift_preserved(self):
        target, shas = self.make_target("status")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_wrapper(target, ["status"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK:", r.stdout)
        # Stray checkout => drift exit 1.
        self.git(target, "checkout", "-q", "--detach", shas[0])
        # git() returns CompletedProcess; ensure checkout worked via head.
        self.assertEqual(self.head(target), shas[0])
        r = self.run_wrapper(target, ["status"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("stray checkout", r.stdout)

    def test_target_override_is_test_only(self):
        target, shas = self.make_target("testonly")
        env = _git_env({"PATH": os.environ.get("PATH", ""),
                        "DISPATCH_DEPLOY_TARGET": str(target)})
        env.pop("DISPATCH_DEPLOY_TEST_MODE", None)
        proc = subprocess.run(
            ["bash", str(DEPLOY), "status"],
            capture_output=True, text=True, timeout=QUICK_OUTER, env=env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("DISPATCH_DEPLOY_TARGET is test-only",
                      proc.stdout + proc.stderr)

    def test_explicit_full_commit_identity(self):
        target, shas = self.make_target("identity")
        for bad in ("master", shas[1][:10], "not-a-sha"):
            r = self.run_wrapper(target, ["deploy", "--commit", bad,
                                         "--smoke-cmd", "true"])
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("40-hex", r.stdout + r.stderr)
        self.assertEqual(self.head(target), shas[0])
        self.assertIsNone(self.record_bytes(target))

    def test_unknown_object_refused_no_fetch(self):
        target, shas = self.make_target("nofetch")
        fake = "1" * 40
        # Prove fake is absent and wrapper refuses without fetching it.
        probe = self.git(target, "cat-file", "-t", fake)
        self.assertNotEqual(probe.returncode, 0)
        r = self.run_wrapper(target, ["deploy", "--commit", fake,
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not an available object", r.stdout + r.stderr)
        probe2 = self.git(target, "cat-file", "-t", fake)
        self.assertNotEqual(probe2.returncode, 0,
                            "wrapper must never fetch missing objects")
        self.assertEqual(self.head(target), shas[0])

    def test_dirty_tree_and_missing_smoke_refused(self):
        target, shas = self.make_target("dirty")
        (target / "f").write_text("dirty\n")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not clean", r.stdout + r.stderr)
        target2, shas2 = self.make_target("nosmoke")
        r = self.run_wrapper(target2, ["deploy", "--commit", shas2[1]])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--smoke-cmd is required", r.stdout + r.stderr)

    def test_unknown_flag_zero_writes(self):
        target, shas = self.make_target("unknownflag")
        before = (self.head(target), self.anchor(target, REF_CURRENT),
                  self.anchor(target, REF_ROLLBACK),
                  self.record_bytes(target))
        canary = self.fixture_root / "smoke-ran"
        r = self.run_wrapper(
            target, ["deploy", "--commit", shas[1], "--smoke-cmd",
                     f"touch {canary}; true", "--bogus-flag"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("unknown argument", r.stdout + r.stderr)
        after = (self.head(target), self.anchor(target, REF_CURRENT),
                 self.anchor(target, REF_ROLLBACK),
                 self.record_bytes(target))
        self.assertEqual(before, after)
        self.assertFalse(canary.exists(), "smoke must not run on flag refusal")
        self.assertEqual(self.leaked_registrations(target), [])

    def test_independent_targets_do_not_block(self):
        a, sha_a = self.make_target("indep-a", commits=2)
        b, sha_b = self.make_target("indep-b", commits=2)
        barrier = self.fixture_root / "barrier-indep"
        barrier.mkdir()
        # Each smoke waits briefly for the peer, proving overlap is allowed.
        smoke_a = (f"touch {barrier}/a-started; "
                   f"for i in $(seq 1 50); do [ -f {barrier}/b-started ] && break; "
                   "sleep 0.1; done; exit 0")
        smoke_b = (f"touch {barrier}/b-started; "
                   f"for i in $(seq 1 50); do [ -f {barrier}/a-started ] && break; "
                   "sleep 0.1; done; exit 0")
        results = {}
        def one(key, tgt, sha, smoke):
            try:
                results[key] = self.run_wrapper(
                    tgt, ["deploy", "--commit", sha, "--smoke-cmd", smoke],
                    timeout=CONCURRENCY_OUTER)
            except subprocess.TimeoutExpired:
                results[key] = "TIMEOUT"
        ta = threading.Thread(target=one, args=("a", a, sha_a[1], smoke_a))
        tb = threading.Thread(target=one, args=("b", b, sha_b[1], smoke_b))
        ta.start(); tb.start(); ta.join(CONCURRENCY_OUTER + 5); tb.join(5)
        self.assertFalse(ta.is_alive() or tb.is_alive(),
                         "independent targets must not block each other")
        self.assertEqual(results["a"].returncode, 0,
                         str(getattr(results["a"], "stderr", results["a"])))
        self.assertEqual(results["b"].returncode, 0,
                         str(getattr(results["b"], "stderr", results["b"])))
        self.assertEqual(self.head(a), sha_a[1])
        self.assertEqual(self.head(b), sha_b[1])

    def test_sibling_worktree_preserved_on_failure(self):
        target, shas = self.make_target("sibling")
        sibling = self.fixture_root / "sibling-wt"
        r = self.git(target, "worktree", "add", "--detach", str(sibling), shas[0])
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = sibling / "keep-me"
        marker.write_text("keep\n")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "false"])
        self.assertEqual(r.returncode, 2)
        self.assertTrue(marker.exists(), "own failure must not remove sibling")
        self.assertIn(str(sibling), self.worktrees(target))
        # Cleanup sibling for tidiness (fixture-owned).
        self.git(target, "worktree", "remove", "--force", str(sibling))

    def test_no_sigkill_trap_claim(self):
        target, shas = self.make_target("sigkilldoc")
        r = self.run_wrapper(target, ["--help"])
        text = r.stdout + r.stderr
        self.assertNotIn("SIGKILL", text,
                         "must never claim SIGKILL runs a trap")

    def test_atomic_record_is_valid_json(self):
        target, shas = self.make_target("atomic")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        raw = self.record_bytes(target)
        data = json.loads(raw.decode())
        for field in ("schemaVersion", "deployedSha", "previousSha",
                      "deployedAt", "path"):
            self.assertIn(field, data)
        # No temp leftovers beside record.
        parent = Path(str(target)).parent
        tmps = [p for p in parent.glob(".tmp.*") if p.exists()]
        self.assertEqual(tmps, [])

    def test_noisy_passing_smoke_record_tail_bounded(self):
        target, shas = self.make_target("noisypass")
        noisy = ("python3 -c \"import sys; sys.stdout.write('N'*"
                 f"{NOISY_BYTES}); sys.stdout.flush()\"; exit 0")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", noisy],
                             timeout=QUICK_OUTER)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        raw = self.record_bytes(target)
        data = json.loads(raw.decode())
        tail = data.get("smoke", {}).get("outputTail", "")
        self.assertLessEqual(len(tail.encode()), TAIL_BOUND)


class BoundedSmoke(DispatchLifecycleBase):
    """Stuck/noisy smoke must be bounded; RED on current source."""

    def test_stuck_smoke_bounded_deadline(self):
        target, shas = self.make_target("stuck")
        before = (self.head(target), self.anchor(target, REF_CURRENT),
                  self.anchor(target, REF_ROLLBACK),
                  self.record_bytes(target))
        start = time.time()
        try:
            r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                         "--smoke-cmd",
                                         f"sleep {SMOKE_SLEEP_STUCK}"],
                                 timeout=OUTER_WATCHDOG)
        except subprocess.TimeoutExpired:
            self.fail(f"wrapper hung beyond outer watchdog ({OUTER_WATCHDOG}s); "
                      "stuck smoke must be killed by finite deadline")
        elapsed = time.time() - start
        self.assertLessEqual(
            elapsed, EXPECTED_DEFAULT_DEADLINE + 2,
            f"wrapper must bound stuck smoke within ~{EXPECTED_DEFAULT_DEADLINE}s")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("timeout", (r.stdout + r.stderr).lower())
        after = (self.head(target), self.anchor(target, REF_CURRENT),
                 self.anchor(target, REF_ROLLBACK),
                 self.record_bytes(target))
        self.assertEqual(before, after,
                         "timeout must refuse before HEAD/anchors/record change")
        self.assertEqual(self.leaked_registrations(target), [])

    def test_term_ignoring_smoke_escalates_to_kill(self):
        target, shas = self.make_target("termignore")
        pidfile = self.fixture_root / "termignore.pid"
        # Sleep 30 so an unbounded wrapper hangs beyond the 16s outer and
        # fails red; a bounded wrapper must TERM then KILL within ~15s.
        smoke = (f"echo $$ > {pidfile}; trap '' TERM; sleep 30")
        start = time.time()
        try:
            r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                         "--smoke-cmd", smoke],
                                 timeout=ESCALATION_OUTER)
        except subprocess.TimeoutExpired:
            # Reap fixture child we can identify, then fail red.
            try:
                pid = int(pidfile.read_text().strip())
                self._live_children.append(pid)
            except Exception:
                pass
            self.fail("TERM-ignoring smoke was not escalated to KILL in bound")
        elapsed = time.time() - start
        self.assertLessEqual(elapsed, EXPECTED_DEFAULT_DEADLINE + 7,
                             "TERM-to-KILL escalation must be bounded")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("timeout", (r.stdout + r.stderr).lower())
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
            except ValueError:
                pid = None
            if pid:
                time.sleep(0.5)
                self.assertFalse(self.pid_alive(pid),
                                 "smoke descendant must be reaped after KILL")
        self.assertEqual(self.leaked_registrations(target), [])

    def test_noisy_failing_smoke_bounded_output(self):
        target, shas = self.make_target("noisyfail")
        noisy = ("python3 -c \"import sys; sys.stdout.write('X'*"
                 f"{NOISY_BYTES}); sys.stdout.flush(); sys.exit(1)\"")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", noisy],
                             timeout=QUICK_OUTER)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr[:500])
        self.assertLess(
            len(r.stderr.encode()), STDERR_BOUND,
            f"failing noisy smoke must not emit unbounded stderr "
            f"(got {len(r.stderr.encode())} bytes)")
        self.assertIn("smoke", (r.stdout + r.stderr).lower())
        self.assertEqual(self.head(target), shas[0])
        self.assertIsNone(self.record_bytes(target))

    def test_smoke_timeout_flag_validated(self):
        target, shas = self.make_target("timeoutflag")
        before = (self.head(target), self.record_bytes(target))
        canary = self.fixture_root / "timeout-smoke-ran"
        for bad in ("0", "-5", "abc", "", "9999999"):
            r = self.run_wrapper(
                target, ["deploy", "--commit", shas[1], "--smoke-cmd",
                         f"touch {canary}; true", "--smoke-timeout", bad],
                timeout=QUICK_OUTER)
            text = r.stdout + r.stderr
            self.assertEqual(r.returncode, 2, f"value={bad!r}: " + text)
            self.assertNotIn("unknown argument", text,
                             f"value={bad!r}: flag must be known, with stable "
                             "validation (not 'unknown argument')")
            self.assertIn("smoke-timeout", text,
                          f"value={bad!r} must give stable validation reason")
            self.assertTrue("must be" in text or "invalid" in text.lower(),
                            f"value={bad!r}: validation must state the constraint, "
                            f"got: {text!r}")
        self.assertEqual((self.head(target), self.record_bytes(target)), before)
        self.assertFalse(canary.exists())
        self.assertEqual(self.leaked_registrations(target), [])

    def test_smoke_timeout_flag_honored(self):
        target, shas = self.make_target("timeouthonor")
        started = self.fixture_root / "honor-started"
        smoke = f"touch {started}; sleep {SMOKE_SLEEP_STUCK}"
        start = time.time()
        try:
            r = self.run_wrapper(
                target, ["deploy", "--commit", shas[1], "--smoke-cmd", smoke,
                         "--smoke-timeout", "2"],
                timeout=OUTER_WATCHDOG)
        except subprocess.TimeoutExpired:
            self.fail("--smoke-timeout 2 did not bound sleep-15 smoke")
        elapsed = time.time() - start
        self.assertTrue(started.exists(),
                        "smoke must start before timeout kills it")
        self.assertLessEqual(elapsed, 8,
                             "explicit 2s timeout must refuse quickly")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("timeout", (r.stdout + r.stderr).lower())
        self.assertEqual(self.head(target), shas[0])
        self.assertIsNone(self.record_bytes(target))


class CleanupOnInterrupt(DispatchLifecycleBase):
    """Timeout/INT/TERM must clean owned worktree + reap; RED on current."""

    def _signal_test(self, sig):
        target, shas = self.make_target(f"sig-{sig.name.lower()}")
        pidfile = self.fixture_root / f"sig-{sig.name.lower()}.pid"
        started = self.fixture_root / f"sig-{sig.name.lower()}.started"
        smoke = (f"echo $$ > {pidfile}; touch {started}; "
                 f"sleep {SMOKE_SLEEP_STUCK}")
        proc = self.start_wrapper(target, ["deploy", "--commit", shas[1],
                                           "--smoke-cmd", smoke])
        try:
            self.assertTrue(self.wait_for_file(started, timeout=10),
                            "smoke did not start; cannot signal wrapper")
            time.sleep(0.3)
            # Signal ONLY the wrapper PID, not the group: the smoke child
            # must be reaped by the wrapper's trap, not by our signal.
            os.kill(proc.pid, sig)
            try:
                out, err = proc.communicate(timeout=INT_TERM_OUTER)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                self.kill_group(proc)
                self.fail(f"wrapper did not exit within {INT_TERM_OUTER}s "
                          f"after {sig.name}")
            self.assertNotEqual(rc, 0,
                                f"wrapper {sig.name} must exit nonzero")
            text = (out or "") + (err or "")
            self.assertNotIn("DEPLOYED", text)
            self.assertEqual(self.head(target), shas[0])
            self.assertIsNone(self.record_bytes(target))
            self.assertEqual(self.leaked_registrations(target), [],
                             f"{sig.name} must clean owned smoke worktree")
            if pidfile.exists():
                try:
                    pid = int(pidfile.read_text().strip())
                except ValueError:
                    pid = None
                if pid:
                    time.sleep(0.5)
                    if self.pid_alive(pid):
                        self._live_children.append(pid)
                    self.assertFalse(self.pid_alive(pid),
                                     "smoke descendant must be reaped")
        finally:
            if proc.poll() is None:
                self.kill_group(proc)
            # Defensive reap of recorded child if wrapper leaked it.
            if pidfile.exists():
                try:
                    pid = int(pidfile.read_text().strip())
                    if self.pid_alive(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
                except ValueError:
                    pass

    def test_term_cleans_worktree_and_reaps(self):
        self._signal_test(signal.SIGTERM)

    def test_int_cleans_worktree_and_reaps(self):
        self._signal_test(signal.SIGINT)

    def test_timeout_cleans_registration(self):
        # Covered behaviorally in BoundedSmoke, but assert explicitly here
        # that a timed-out invocation leaves zero registrations.
        target, shas = self.make_target("timeoutclean")
        try:
            r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                         "--smoke-cmd",
                                         f"sleep {SMOKE_SLEEP_STUCK}"],
                                 timeout=OUTER_WATCHDOG)
        except subprocess.TimeoutExpired:
            self.fail("wrapper hung; timeout cleanup never ran")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.leaked_registrations(target), [])

    def test_cleanup_failure_observable_nonzero(self):
        target, shas = self.make_target("cleanfail")
        # Force BOTH cleanup paths to fail: failbin/git refuses
        # `worktree remove`, failbin/rm refuses the fallback. Wrapper must
        # report cleanup failure nonzero with stable reason, never DEPLOYED.
        failbin = self.fixture_root / "failbin"
        failbin.mkdir()
        rm_shim = failbin / "rm"
        rm_shim.write_text("#!/bin/sh\necho 'shim rm refusing' >&2\nexit 1\n")
        rm_shim.chmod(0o755)
        git_fail = failbin / "git"
        git_fail.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *"worktree remove"*) echo "shim git refusing worktree remove" >&2; exit 1;;\n'
            f'  *) exec "{REAL_GIT}" "$@";;\n'
            "esac\n"
        )
        git_fail.chmod(0o755)
        env_extra = {"PATH": str(failbin) + os.pathsep + str(self.shim_dir)
                     + os.pathsep + os.environ.get("PATH", "")}
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "false"],
                             timeout=QUICK_OUTER, extra_env=env_extra)
        leaked = self.leaked_registrations(target)
        try:
            self.assertTrue(leaked,
                            "fixture must force a leaked registration to test "
                            "cleanup-failure observability")
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("DEPLOYED", r.stdout + r.stderr)
            self.assertIn("cleanup", (r.stdout + r.stderr).lower(),
                          "failed cleanup must report a stable cleanup reason, "
                          f"got: {(r.stdout + r.stderr)!r}")
        finally:
            for wt in leaked:
                self.git(target, "worktree", "remove", "--force", wt)
                shutil.rmtree(wt, ignore_errors=True)


class Serialization(DispatchLifecycleBase):
    """Same-target deploy/rollback must serialize; RED on current source."""

    def _barrier_smoke(self, barrier, name, peer):
        return (f"touch {barrier}/{name}-started; "
                f"for i in $(seq 1 60); do [ -f {barrier}/{peer}-started ] "
                "&& break; sleep 0.1; done; exit 0")

    def _assert_coherent(self, target):
        raw = self.record_bytes(target)
        self.assertIsNotNone(raw, "record must exist after serialized success")
        data = json.loads(raw.decode())
        head = self.head(target)
        cur = self.anchor(target, REF_CURRENT)
        rb = self.anchor(target, REF_ROLLBACK)
        self.assertEqual(head, data["deployedSha"],
                         f"HEAD {head} != record {data['deployedSha']}")
        self.assertEqual(cur, data["deployedSha"])
        self.assertEqual(rb, data["previousSha"])
        # Both SHAs must still be commit objects.
        for sha in (data["deployedSha"], data["previousSha"]):
            r = self.git(target, "cat-file", "-t", sha)
            self.assertEqual(r.stdout.strip(), "commit")

    def test_concurrent_same_sha_serialized(self):
        target, shas = self.make_target("same-sha")
        barrier = self.fixture_root / "barrier-same"
        barrier.mkdir()
        smoke_a = self._barrier_smoke(barrier, "a", "b")
        smoke_b = self._barrier_smoke(barrier, "b", "a")
        results = {}
        def one(key, smoke):
            try:
                results[key] = self.run_wrapper(
                    target, ["deploy", "--commit", shas[1],
                             "--smoke-cmd", smoke],
                    timeout=CONCURRENCY_OUTER)
            except subprocess.TimeoutExpired:
                results[key] = "TIMEOUT"
        ta = threading.Thread(target=one, args=("a", smoke_a))
        tb = threading.Thread(target=one, args=("b", smoke_b))
        ta.start(); tb.start(); ta.join(CONCURRENCY_OUTER + 5); tb.join(5)
        self.assertFalse(ta.is_alive() or tb.is_alive(),
                         "serialized transactions must finish in bound")
        self.assertNotEqual(results.get("a"), "TIMEOUT")
        self.assertNotEqual(results.get("b"), "TIMEOUT")
        rcs = sorted([results["a"].returncode, results["b"].returncode])
        # Exactly one winner; loser refuses already-deployed or lock contention.
        self.assertEqual(rcs, [0, 2],
                         f"same-SHA concurrency must serialize 1 win/1 refuse, "
                         f"got {rcs}: a={results['a'].stdout+results['a'].stderr!r} "
                         f"b={results['b'].stdout+results['b'].stderr!r}")
        loser = results["a"] if results["a"].returncode == 2 else results["b"]
        text = (loser.stdout + loser.stderr).lower()
        self.assertTrue("already deployed" in text or "lock" in text,
                        f"loser must name already-deployed or lock, got: {text!r}")
        self._assert_coherent(target)
        self.assertEqual(self.leaked_registrations(target), [])

    def test_concurrent_implicit_rollbacks_serialized(self):
        target, shas = self.make_target("implicit-rb")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # HEAD=shas[1], rollback anchor=shas[0]. Two implicit rollbacks race.
        barrier = self.fixture_root / "barrier-rb"
        barrier.mkdir()
        smoke_a = self._barrier_smoke(barrier, "a", "b")
        smoke_b = self._barrier_smoke(barrier, "b", "a")
        results = {}
        def one(key, smoke):
            try:
                results[key] = self.run_wrapper(
                    target, ["rollback", "--smoke-cmd", smoke],
                    timeout=CONCURRENCY_OUTER)
            except subprocess.TimeoutExpired:
                results[key] = "TIMEOUT"
        ta = threading.Thread(target=one, args=("a", smoke_a))
        tb = threading.Thread(target=one, args=("b", smoke_b))
        ta.start(); tb.start(); ta.join(CONCURRENCY_OUTER + 5); tb.join(5)
        self.assertFalse(ta.is_alive() or tb.is_alive())
        self.assertNotEqual(results.get("a"), "TIMEOUT")
        self.assertNotEqual(results.get("b"), "TIMEOUT")
        rcs = sorted([results["a"].returncode, results["b"].returncode])
        self.assertEqual(rcs, [0, 2],
                         f"implicit rollbacks must serialize 1 win/1 refuse, got {rcs}")
        loser = results["a"] if results["a"].returncode == 2 else results["b"]
        text = (loser.stdout + loser.stderr).lower()
        self.assertTrue("already deployed" in text or "lock" in text,
                        f"loser must name already-deployed or lock, got: {text!r}")
        self._assert_coherent(target)

    def test_concurrent_deploy_and_rollback_coherent(self):
        target, shas = self.make_target("deploy-vs-rb", commits=4)
        # shas[0] anchored; deploy shas[1] first so rollback anchor is shas[0].
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Race: deploy shas[2] vs rollback --commit shas[0] (explicit).
        barrier = self.fixture_root / "barrier-mixed"
        barrier.mkdir()
        smoke_d = self._barrier_smoke(barrier, "d", "r")
        smoke_r = self._barrier_smoke(barrier, "r", "d")
        results = {}
        def run_d():
            try:
                results["d"] = self.run_wrapper(
                    target, ["deploy", "--commit", shas[2],
                             "--smoke-cmd", smoke_d],
                    timeout=CONCURRENCY_OUTER)
            except subprocess.TimeoutExpired:
                results["d"] = "TIMEOUT"
        def run_r():
            try:
                results["r"] = self.run_wrapper(
                    target, ["rollback", "--commit", shas[0],
                             "--smoke-cmd", smoke_r],
                    timeout=CONCURRENCY_OUTER)
            except subprocess.TimeoutExpired:
                results["r"] = "TIMEOUT"
        td = threading.Thread(target=run_d)
        tr = threading.Thread(target=run_r)
        td.start(); tr.start(); td.join(CONCURRENCY_OUTER + 5); tr.join(5)
        self.assertFalse(td.is_alive() or tr.is_alive(),
                         "deploy vs rollback must finish in bound")
        self.assertNotEqual(results.get("d"), "TIMEOUT")
        self.assertNotEqual(results.get("r"), "TIMEOUT")
        # Either serialized sequence (both 0, coherent final) or one refused
        # on lock contention; never torn HEAD/anchors/record.
        for key in ("d", "r"):
            self.assertIn(results[key].returncode, (0, 2),
                          f"{key}: {results[key].stdout+results[key].stderr!r}")
        self._assert_coherent(target)
        self.assertEqual(self.leaked_registrations(target), [])


class JsonDryRunHelp(DispatchLifecycleBase):
    """Versioned --json, --dry-run, help; RED on current source."""

    def _parse_json_stdout(self, result):
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"stdout must be single JSON object with --json: {exc}\n"
                      f"stdout={result.stdout!r}\nstderr={result.stderr!r}")

    def test_deploy_json_success(self):
        target, shas = self.make_target("json-ok")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true", "--json"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self._parse_json_stdout(r)
        self.assertEqual(data.get("schemaVersion"), JSON_SCHEMA_VERSION)
        self.assertEqual(data.get("command"), "deploy")
        self.assertEqual(data.get("exitCode"), 0)
        self.assertIn("stage", data)
        self.assertEqual(data.get("target"), str(target))
        self.assertEqual(data.get("deployedSha"), shas[1])
        self.assertEqual(data.get("previousSha"), shas[0])
        self.assertIn("retryable", data)
        self.assertIn("next_action", data)
        # Text preserved on stderr.
        self.assertIn("DEPLOYED", r.stderr)

    def test_deploy_json_smoke_failure(self):
        target, shas = self.make_target("json-fail")
        marker = "SECRET_SMOKE_MARKER_7f3a"
        smoke = f"echo {marker}-part1; echo {marker}-part2; exit 1"
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", smoke, "--json"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        data = self._parse_json_stdout(r)
        self.assertEqual(data.get("schemaVersion"), JSON_SCHEMA_VERSION)
        self.assertEqual(data.get("exitCode"), 2)
        self.assertEqual(data.get("target"), str(target))
        err = data.get("error", {})
        self.assertRegex(err.get("code", ""), r"^[a-z][a-z0-9_]*$")
        self.assertIn("stage", data)
        self.assertIn(data.get("retryable"), (True, False))
        self.assertIsInstance(data.get("next_action"), str)
        blob = json.dumps(data)
        self.assertNotIn(marker, blob,
                         "structured errors must not expose raw smoke output")
        if "outputTail" in data:
            self.assertLessEqual(len(str(data["outputTail"]).encode()), TAIL_BOUND)
        self.assertEqual(self.head(target), shas[0])

    def test_status_json_ok_and_drift(self):
        target, shas = self.make_target("json-status")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_wrapper(target, ["status", "--json"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self._parse_json_stdout(r)
        self.assertEqual(data.get("exitCode"), 0)
        self.assertEqual(data.get("command"), "status")
        # Drift => exit 1 with JSON.
        self.git(target, "checkout", "-q", "--detach", shas[0])
        r = self.run_wrapper(target, ["status", "--json"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        data = self._parse_json_stdout(r)
        self.assertEqual(data.get("exitCode"), 1)

    def test_json_preserves_exit_meanings(self):
        target, shas = self.make_target("json-exits")
        # Success 0, refusal 2 without --json (positive part).
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0)
        # Same meanings with --json.
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true", "--json"])
        # Already deployed => refusal 2 with JSON.
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        data = self._parse_json_stdout(r)
        self.assertEqual(data.get("exitCode"), 2)

    def test_dry_run_no_writes_no_smoke(self):
        target, shas = self.make_target("dryrun")
        before = (self.head(target), self.anchor(target, REF_CURRENT),
                  self.anchor(target, REF_ROLLBACK),
                  self.record_bytes(target))
        canary = self.fixture_root / "dryrun-smoke-ran"
        r = self.run_wrapper(
            target, ["deploy", "--commit", shas[1], "--smoke-cmd",
                     f"touch {canary}; true", "--dry-run"],
            timeout=QUICK_OUTER)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plan = r.stdout + r.stderr
        self.assertIn(shas[1][:12], plan)
        self.assertIn("dry", plan.lower())
        after = (self.head(target), self.anchor(target, REF_CURRENT),
                 self.anchor(target, REF_ROLLBACK),
                 self.record_bytes(target))
        self.assertEqual(before, after, "--dry-run must be zero-write")
        self.assertFalse(canary.exists(), "--dry-run must not run smoke")
        self.assertEqual(self.leaked_registrations(target), [])

    def test_dry_run_json_plan(self):
        target, shas = self.make_target("dryrunjson")
        r = self.run_wrapper(
            target, ["deploy", "--commit", shas[1], "--smoke-cmd", "true",
                     "--dry-run", "--json"],
            timeout=QUICK_OUTER)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self._parse_json_stdout(r)
        self.assertEqual(data.get("schemaVersion"), JSON_SCHEMA_VERSION)
        self.assertTrue(data.get("dryRun") is True or
                        data.get("stage") == "plan" or
                        "plan" in json.dumps(data).lower())
        self.assertEqual(self.head(target), shas[0])
        self.assertIsNone(self.record_bytes(target))

    def test_misplaced_flags_rejected_before_mutation(self):
        target, shas = self.make_target("misplaced")
        r = self.run_wrapper(target, ["deploy", "--commit", shas[1],
                                     "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        before = (self.head(target), self.record_bytes(target))
        # status takes no --commit/--smoke-cmd; must refuse before reads(?),
        # at minimum before any write, with stable reason.
        r = self.run_wrapper(target, ["status", "--commit", shas[0]])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("commit", (r.stdout + r.stderr).lower())
        self.assertEqual((self.head(target), self.record_bytes(target)), before)
        r = self.run_wrapper(target, ["status", "--smoke-cmd", "true"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_help_documents_contract(self):
        target, shas = self.make_target("helpdoc")
        r = self.run_wrapper(target, ["--help"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = r.stdout + r.stderr
        for token in ("--json", "--dry-run", "--smoke-timeout", "lock",
                      "timeout", "cleanup", "exit"):
            self.assertIn(token, text.lower(),
                          f"help must document {token}")
        # Concise examples present.
        self.assertIn("deploy", text.lower())
        self.assertIn("example", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
