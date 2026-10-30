#!/usr/bin/env python3
"""Launcher-contract regressions: real isolation CLI + run_case launch args.

Covers the two gaps left by test_hygiene.py:
  * isolation.py main() must confine() before exec (helper-only tests and a
    mocked run_case transport both survive a CLI confine deletion), so these
    tests execute the real CLI
    ``isolation.py PRIVATE_RUNTIME -- /absolute/python -c PROBE`` with
    bounded timeouts and sequential processes.
  * run_case must copy the exact public schema bytes into the private
    runtime and keep ``--skip-git-repo-check`` (argv-shape tests survive a
    copyfile/Git-bypass deletion), so these tests observe the real
    ``--output-schema`` path at launch and compare its bytes.

Stdlib unittest only. Synthetic files only: no secrets, no model calls,
no network. Run: python3 -m unittest -v test_launcher_contract
"""
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_review

# Absolute interpreter for the confined command (the contract requires
# isolation.py PRIVATE_RUNTIME -- /absolute/python ...).
PY = os.path.realpath(sys.executable)
assert os.path.isabs(PY), "confined command needs an absolute python path"
ISOLATION = os.path.join(HERE, "isolation.py")
TIMEOUT = 60


def _cli(runtime, probe):
    """Run the real isolation CLI with a compile-checked probe. Sequential."""
    compile(probe, "<launcher-contract-probe>", "exec")  # precheck, never executes here
    return subprocess.run([PY, ISOLATION, runtime, "--", PY, "-c", probe],
                          capture_output=True, text=True, timeout=TIMEOUT)


class IsolationCLI(unittest.TestCase):
    def setUp(self):
        if platform.system() != "Linux":
            self.skipTest("Landlock confinement is Linux-only")
        # Fail closed on Linux: no skip for missing Landlock support; a
        # refused confinement must fail these tests, never pass silently.

    def test_cli_private_runtime_read_write_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "runtime")
            os.makedirs(rt)
            target = os.path.join(rt, "ok.txt")
            probe = "\n".join([
                f"target={target!r}",
                "open(target, 'w').write('synthetic-ok')",
                "assert open(target).read() == 'synthetic-ok'",
                "print('RT_OK')",
            ])
            proc = _cli(rt, probe)
            self.assertIn("RT_OK", proc.stdout,
                          f"runtime unusable under CLI confinement: rc={proc.returncode} "
                          f"err={proc.stderr[:2000]}")

    def test_cli_grader_and_other_arm_unreadable_and_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "runtime")
            os.makedirs(os.path.join(rt, "sub"))
            grader = os.path.join(tmp, "grader-secret.txt")
            with open(grader, "w", encoding="utf-8") as fh:
                fh.write("SYNTHETIC-GRADER-ORIGINAL")
            other = os.path.join(tmp, "arm-other.json")
            with open(other, "w", encoding="utf-8") as fh:
                fh.write('{"winner": "other-arm"}')
            link = os.path.join(rt, "evil-link")
            try:
                os.symlink(grader, link)
            except OSError:
                self.fail("cannot create symlink for test")
            # .. traversal that resolves to real existing files (rt/sub
            # exists, so no ENOENT ambiguity with or without confinement).
            trav_grader = os.path.join(rt, "sub", "..", "..", "grader-secret.txt")
            trav_other = os.path.join(rt, "sub", "..", "..", "arm-other.json")
            probe = "\n".join([
                "import os",
                f"grader={grader!r}",
                f"other={other!r}",
                f"link={link!r}",
                f"trav_grader={trav_grader!r}",
                f"trav_other={trav_other!r}",
                "flags = {}",
                "def blocked(name, fn):",
                "    try:",
                "        fn()",
                "    except Exception:",
                "        flags[name] = True",
                "    else:",
                "        flags[name] = False",
                "blocked('grader_read', lambda: open(grader).read())",
                "blocked('grader_write', lambda: open(grader, 'w').write('PWNED'))",
                "blocked('other_read', lambda: open(other).read())",
                "blocked('other_write', lambda: open(other, 'w').write('PWNED'))",
                "blocked('link', lambda: open(link).read())",
                "blocked('trav_grader', lambda: open(trav_grader).read())",
                "blocked('trav_other', lambda: open(trav_other, 'w').write('PWNED'))",
                "bad = sorted(k for k, v in flags.items() if not v)",
                "assert not bad, 'NOT blocked under CLI confinement: %s' % bad",
                "print('GRADER_CONFINED')",
            ])
            proc = _cli(rt, probe)
            self.assertIn("GRADER_CONFINED", proc.stdout,
                          f"CLI confinement failed: rc={proc.returncode} "
                          f"err={proc.stderr[:2000]}")
            with open(grader, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "SYNTHETIC-GRADER-ORIGINAL")
            with open(other, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '{"winner": "other-arm"}')

    def test_cli_descendant_process_stays_confined(self):
        # The Landlock policy must cover descendants: a grandchild spawned
        # by the confined child must also be denied outside read/write.
        with tempfile.TemporaryDirectory() as tmp:
            rt = os.path.join(tmp, "runtime")
            os.makedirs(rt)
            grader = os.path.join(tmp, "grader-secret.txt")
            with open(grader, "w", encoding="utf-8") as fh:
                fh.write("SYNTHETIC-DESCENDANT-ORIGINAL")
            grandchild = "\n".join([
                f"grader={grader!r}",
                "read_blocked=False",
                "write_blocked=False",
                "try:",
                "    open(grader).read()",
                "except Exception:",
                "    read_blocked=True",
                "try:",
                "    open(grader, 'w').write('PWNED')",
                "except Exception:",
                "    write_blocked=True",
                "assert read_blocked, 'grandchild read NOT blocked'",
                "assert write_blocked, 'grandchild write NOT blocked'",
                "print('GRANDCHILD_BLOCKED')",
            ])
            compile(grandchild, "<launcher-contract-grandchild>", "exec")
            probe = "\n".join([
                "import subprocess",
                f"gc={grandchild!r}",
                f"py={PY!r}",
                "r = subprocess.run([py, '-c', gc], capture_output=True, text=True, timeout=60)",
                "assert 'GRANDCHILD_BLOCKED' in r.stdout, "
                "'descendant escape: rc=%d out=%s err=%s' % (r.returncode, r.stdout[:500], r.stderr[:500])",
                "print('DESCENDANT_CONFINED')",
            ])
            proc = _cli(rt, probe)
            self.assertIn("DESCENDANT_CONFINED", proc.stdout,
                          f"descendant confinement failed: rc={proc.returncode} "
                          f"err={proc.stderr[:2000]}")
            with open(grader, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "SYNTHETIC-DESCENDANT-ORIGINAL")


class RunCaseLaunchContract(unittest.TestCase):
    """Observe the real --output-schema path at launch; mock transport/git only."""

    def _launch(self):
        case = {"id": "c1", "kind": "real", "head_sha": "abc123",
                "title": "Fix thing", "body": "body text"}
        with open(os.path.join(HERE, "output_schema.json"), "rb") as fh:
            public = fh.read()
        canned = json.dumps({"score": 3, "summary": "s",
                             "findings": [{"file": "src/a.ts", "line": 1,
                                           "severity": "major", "title": "t", "rationale": "r"}],
                             "coverage": {"files_reviewed": ["src/a.ts"],
                                          "complete": True, "limitations": ""}})
        seen: dict = {}
        fake_proc = mock.Mock()
        fake_proc.stdout = ""
        fake_proc.returncode = 0

        def fake_run(args, **kw):
            seen.setdefault("calls", []).append((list(args), dict(kw)))
            if "input" not in kw:
                return fake_proc  # cleanup call (git worktree remove): observe, don't execute
            argv = list(args)
            seen["args"] = argv
            seen["kw"] = kw
            if "--output-schema" in argv:
                spath = argv[argv.index("--output-schema") + 1]
                seen["schema_path"] = spath
                try:
                    with open(spath, "rb") as fh:
                        seen["schema_bytes"] = fh.read()
                except OSError as exc:
                    seen["schema_error"] = str(exc)
            for i, arg in enumerate(argv):
                if arg == "-o" and i + 1 < len(argv):
                    try:
                        with open(argv[i + 1], "w", encoding="utf-8") as out:
                            out.write(canned)
                    except OSError:
                        pass
            return fake_proc

        patches = [
            mock.patch.object(run_review, "prepare_worktree",
                              lambda repo, case, dest: None),
            mock.patch.object(run_review, "sh",
                              lambda *a, **k: "deadbeef diff" if "--patch" in a else "src/a.ts"),
            mock.patch.object(run_review, "read_git_file", lambda cwd, path: "file text"),
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch.object(run_review.time, "time", return_value=1000.0),
        ]
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ,
                                 {"PATH": "/usr/bin:/bin",
                                  "CLIPROXY_KEY_ENV_VALUE": "k"}, clear=False):
                res = run_review.run_case("/repo", case, "m1", "medium",
                                          "CLIPROXY_KEY_ENV_VALUE", 30)
        finally:
            for p in reversed(patches):
                p.stop()
        return seen, res, public

    def test_output_schema_exact_bytes_already_in_runtime(self):
        # The public schema must be copied into the private runtime BEFORE
        # launch with byte-identical content; the grader dir must never be
        # referenced. Deleting the copyfile survives argv-shape checks but
        # fails here (missing bytes at launch).
        seen, _res, public = self._launch()
        argv = seen.get("args", [])
        self.assertIn("--output-schema", argv)
        spath = seen.get("schema_path")
        self.assertTrue(spath, "no --output-schema path observed at launch")
        cwd = seen.get("kw", {}).get("cwd", "")
        self.assertTrue(cwd, "child cwd must be set")
        self.assertTrue(str(spath).startswith(str(cwd)),
                        f"schema must live under runtime cwd={cwd!r}, got {spath!r}")
        self.assertNotEqual(spath, os.path.join(run_review.HERE, "output_schema.json"),
                            "must not reference the original grader schema dir")
        self.assertNotIn("schema_error", seen,
                         f"schema bytes missing at launch: {seen.get('schema_error')}")
        self.assertIn("schema_bytes", seen)
        self.assertEqual(seen["schema_bytes"], public,
                         "schema bytes in runtime differ from the public schema")

    def test_skip_git_repo_check_reaches_launch_argv(self):
        # The confined child must not consult repo Git state; removing the
        # bypass flag survives all other checks but fails here.
        seen, _res, _public = self._launch()
        argv = seen.get("args", [])
        self.assertIn("isolation.py", json.dumps(argv),
                      f"must launch via isolation.py CLI, got {argv}")
        self.assertIn("--skip-git-repo-check", argv,
                      f"Git bypass flag missing from codex argv: {argv}")


if __name__ == "__main__":
    unittest.main()
