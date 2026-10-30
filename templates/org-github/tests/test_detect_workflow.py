#!/usr/bin/env python3
"""Offline regressions for the actual reusable step and its reference matcher."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflow-templates/changes.yml"
STARTER = ROOT / "workflow-templates/starter-ci.yml"
MANIFESTS = (
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "Gemfile", "Gemfile.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "pyproject.toml", "poetry.lock", "uv.lock", "requirements.txt",
    "requirements-dev.txt", "requirements.in", "setup.py", "setup.cfg",
    "Pipfile", "Pipfile.lock", "composer.json", "composer.lock",
)


def clean_env(root):
    # Synthetic repos only: never enter a managed Git launcher or inherit its
    # broker credentials, shell startup files, user config, templates or hooks.
    for name in ("home", "tmp", "empty-template"):
        (root / name).mkdir(exist_ok=True)
    return {"PATH": os.defpath, "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "home/.config"),
            "TMPDIR": str(root / "tmp"), "RUNNER_TEMP": str(root / "tmp"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TEMPLATE_DIR": str(root / "empty-template"),
            "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1"}


def system_git():
    candidate = shutil.which("git", path=os.defpath)
    if candidate is None:
        raise RuntimeError("fixture requires a system Git executable")
    target = Path(candidate).resolve(strict=True)
    with target.open("rb") as source:
        if source.read(2) == b"#!":
            raise RuntimeError("fixture Git must not resolve to a script wrapper")
    return str(target)


def diff_failure_git(path):
    target = system_git()
    if path.resolve() == Path(target):
        raise RuntimeError("fixture Git must not forward to itself")
    path.write_text('#!/bin/sh\n'
                    'if [ "${DETECT_FIXTURE_GIT_ACTIVE:-}" = 1 ]; then\n'
                    '  printf "fixture Git forwarding recursion\\n" >&2; exit 97\n'
                    'fi\nexport DETECT_FIXTURE_GIT_ACTIVE=1\n'
                    f'printf "%s\\n" "$1" >> {shlex.quote(str(path.with_suffix(".log")))}\n'
                    'if [ "$1" = diff ]; then exit 128; fi\n'
                    f'exec {shlex.quote(target)} "$@"\n')
    path.chmod(0o755)


def run_fixture(command, *, env, input=None, timeout=10, check=False, **kwargs):
    # A direct-parent timeout leaves Git/wrapper descendants running (or holding
    # pipes open). Every fixture command owns a session and a bounded cleanup.
    with subprocess.Popen(command, env=env, start_new_session=True,
                          stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # Do this even if the parent exited: a descendant may have
                # ignored SIGTERM and closed its copy of the captured pipes.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=1)
            raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def step_script(path):
    # The detector has exactly one run block; execute it verbatim, including
    # set -euo pipefail and output publication, instead of simulating Git.
    return textwrap.dedent(path.read_text().split("        run: |\n", 1)[1])


class FixtureSafetyTests(unittest.TestCase):
    def setUp(self):
        scratch = (os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
                   or os.environ.get("RUNNER_TEMP"))
        self.temp = tempfile.TemporaryDirectory(prefix="detect-safety-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = clean_env(self.root)

    def test_environment_is_synthetic_only(self):
        inherited = {"PATH": str(self.root / "managed"), "GH_TOKEN": "synthetic",
                     "PAPERCLIP_API_KEY": "synthetic", "BASH_ENV": "synthetic",
                     "PYTHONPATH": "synthetic", "GIT_DIR": "synthetic"}
        with patch.dict(os.environ, inherited):
            env = clean_env(self.root)
        self.assertEqual(env, self.env)
        self.assertEqual(env["PATH"], os.defpath)
        self.assertEqual(env["HOME"], str(self.root / "home"))
        for name in inherited.keys() - {"PATH"}:
            self.assertNotIn(name, env)

    def test_system_git_rejects_wrapper_and_self_resolution(self):
        fake = self.root / "git"
        fake.write_text("#!/bin/sh\nexit 91\n")
        fake.chmod(0o755)
        original = fake.read_bytes()
        with patch("shutil.which", return_value=str(fake)) as lookup:
            with self.assertRaisesRegex(RuntimeError, "script wrapper"):
                system_git()
            lookup.assert_called_once_with("git", path=os.defpath)
        with patch(f"{__name__}.system_git", return_value=str(fake)):
            with self.assertRaisesRegex(RuntimeError, "itself"):
                diff_failure_git(fake)
        self.assertEqual(fake.read_bytes(), original)

    def test_old_resolution_reenters_but_system_target_forwards_once(self):
        # Model ONLY the launcher's PATH removal, never invoke the real broker.
        # The tiny guard is installed before the old resolution can execute.
        managed = self.root / "managed"
        bindir = self.root / "bin"
        managed.mkdir()
        bindir.mkdir()
        trace = self.root / "trace"
        wrapper = managed / "git"
        wrapper.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f'''\
            import os, sys
            depth = int(os.environ.get("SYNTHETIC_DEPTH", "0"))
            if depth >= 2:
                sys.exit(93)
            os.environ["SYNTHETIC_DEPTH"] = str(depth + 1)
            with open({str(trace)!r}, "a") as log:
                log.write("wrapper\\n")
            os.environ["PATH"] = os.pathsep.join(
                p for p in os.environ["PATH"].split(os.pathsep) if p != {str(managed)!r})
            os.execvpe("git", ["git", *sys.argv[1:]], os.environ)
            '''))
        wrapper.chmod(0o755)
        fake = bindir / "git"
        ambient_path = str(managed) + os.pathsep + os.defpath
        old_target = shutil.which("git", path=ambient_path)
        self.assertEqual(old_target, str(wrapper))
        fake.write_text('#!/bin/sh\n'
                        f'printf "fixture\\n" >> {shlex.quote(str(trace))}\n'
                        f'exec {shlex.quote(old_target)} "$@"\n')
        fake.chmod(0o755)
        env = dict(self.env, PATH=str(bindir) + os.pathsep + ambient_path)
        old = run_fixture([str(fake), "--version"], env=env, timeout=2, text=True)
        self.assertEqual(old.returncode, 93, old.stderr)
        self.assertEqual(trace.read_text().splitlines(),
                         ["fixture", "wrapper", "fixture", "wrapper", "fixture"])
        trace.unlink()
        with patch.dict(os.environ, {"PATH": ambient_path}):
            diff_failure_git(fake)
        new = run_fixture([str(fake), "--version"], env=env, timeout=2, text=True)
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertTrue(new.stdout.startswith("git version "), new.stdout)
        self.assertFalse(trace.exists(), "corrected fixture entered synthetic wrapper")
        self.assertEqual(fake.with_suffix(".log").read_text(), "--version\n")

    def test_forwarding_guard_rejects_reentry(self):
        fake = self.root / "git"
        diff_failure_git(fake)
        result = run_fixture([str(fake), "--version"], timeout=2, text=True,
                             env=dict(self.env, DETECT_FIXTURE_GIT_ACTIVE="1"))
        self.assertEqual(result.returncode, 97)
        self.assertIn("forwarding recursion", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(fake.with_suffix(".log").exists())

    def test_timeout_terminates_and_reaps_synthetic_descendants(self):
        script = self.root / "tree.py"
        pids = self.root / "pids"
        script.write_text(textwrap.dedent(f'''\
            import os, signal, subprocess, sys
            child = None
            def stop(signum, frame):
                if child is not None:
                    child.wait(timeout=0.5)
                sys.exit(0)
            signal.signal(signal.SIGTERM, stop)
            with open({str(pids)!r}, "a") as log:
                log.write(str(os.getpid()) + "\\n")
            depth = int(sys.argv[1])
            if depth:
                child = subprocess.Popen([sys.executable, "-I", __file__, str(depth - 1)])
            while True:
                signal.pause()
            '''))
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_fixture([sys.executable, "-I", str(script), "2"], env=self.env, timeout=2)
        self.assertLess(time.monotonic() - started, 5)
        descendants = [int(pid) for pid in pids.read_text().splitlines()]
        self.assertEqual(len(descendants), 3)
        for pid in descendants:
            with self.assertRaises(ProcessLookupError, msg=f"fixture PID {pid} survived"):
                os.kill(pid, 0)

    def test_timeout_kills_a_process_ignoring_term(self):
        script = ("import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "print(os.getpid(), flush=True); time.sleep(30)")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as failure:
            run_fixture([sys.executable, "-I", "-c", script], env=self.env, timeout=0.5)
        self.assertLess(time.monotonic() - started, 4)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(failure.exception.output.strip()), 0)

    def test_runner_preserves_exit_and_output(self):
        command = ["bash", "-c", "printf out; printf err >&2; exit 37"]
        result = run_fixture(command, env=self.env, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (37, "out", "err"))
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            run_fixture(command, env=self.env, text=True, check=True)
        self.assertEqual(failure.exception.returncode, 37)


class DetectWorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = (os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
                   or os.environ.get("RUNNER_TEMP"))
        self.temp = tempfile.TemporaryDirectory(prefix="detect-workflow-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = clean_env(self.root)
        self.real_git = system_git()
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "user.name", "ci")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "diff.renames", "true")
        for path in ("README.md", "web/app.js", "web/é.js", "web/line\nbreak.js",
                     "web/tab\tfile.js", "web/trailing.js ", "tools/run.py",
                     os.fsdecode(b"web/invalid-\xff.js")):
            self.write(path)
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        # A file-system remote: no network, credentials, or caller checkout.
        self.git("remote", "add", "origin", str(self.repo))
        self.git("checkout", "-q", "-b", "fixture")
        starter = STARTER.read_text()
        self.areas = re.search(r"^      areas-json: '([^']+)'", starter, re.M).group(1)
        self.full = re.search(r"^      full-run-globs-json: '([^']+)'", starter, re.M).group(1)
        self.docs = re.search(r"^      docs-areas-json: '([^']+)'", starter, re.M).group(1)

    def git(self, *args):
        result = run_fixture([self.real_git, *args], cwd=self.repo, env=self.env,
                             text=True, errors="surrogateescape")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def write(self, path):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("fixture content\n" * 4)

    def commit(self, name):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", name)

    def reference(self, paths, *, file_input=False):
        raw = b"".join(os.fsencode(path) + b"\0" for path in paths)
        command = ["python3", "-I", str(ROOT / "scripts/detect_changes.py"),
                   "--areas-json", self.areas, "--full-run-json", self.full,
                   "--docs-areas-json", self.docs]
        if file_input:
            source = self.root / "paths.bin"
            source.write_bytes(raw)
            command += ["--files", str(source)]
        result = run_fixture(command, input=raw, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def workflow(self, *, base_ref="", event_base="main", extra_env=None):
        output = self.root / "github-output"
        output.write_text("")
        env = dict(self.env, AREAS_JSON=self.areas, FULL_RUN_JSON=self.full,
                   DOCS_AREAS_JSON=self.docs, BASE_REF=base_ref, EVENT_BASE=event_base,
                   RUNNER_TEMP=str(self.root), GITHUB_OUTPUT=str(output))
        if extra_env:
            env.update(extra_env)
        result = run_fixture(["bash", "--noprofile", "--norc", "-c", step_script(WORKFLOW)],
                             cwd=self.repo, env=env, text=True)
        fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
        parsed = {k: json.loads(v) for k, v in fields.items()}
        return result, parsed

    def assert_classification(self, expected):
        result, actual = self.workflow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual, expected)
        raw = run_fixture([self.real_git, "diff", "--name-only", "--no-renames", "-z",
                           self.base, "HEAD"], cwd=self.repo, env=self.env,
                          check=True).stdout
        paths = [os.fsdecode(path) for path in raw.split(b"\0") if path]
        self.assertEqual(self.reference(paths), expected)

    def test_docs_only(self):
        self.write("docs/guide.md")
        self.commit("docs")
        self.assert_classification({"areas": {"web": False, "bot": False, "docs": True},
                                    "full_run": False, "docs_only": True})

    def test_area_only(self):
        self.write("web/new.js")
        self.commit("web")
        self.assert_classification({"areas": {"web": True, "bot": False, "docs": False},
                                    "full_run": False, "docs_only": False})

    def test_bot_only(self):
        self.write("bot/main.py")
        self.commit("bot")
        self.assert_classification({"areas": {"web": False, "bot": True, "docs": False},
                                    "full_run": False, "docs_only": False})

    def test_root_and_nested_dependency_files_force_full_run(self):
        for manifest in MANIFESTS:
            for prefix in ("", "nested/"):
                with self.subTest(path=prefix + manifest):
                    self.git("reset", "--hard", self.base)
                    self.write(prefix + manifest)
                    self.commit("dependency")
                    self.assert_classification({
                        "areas": {"web": False, "bot": False, "docs": False},
                        "full_run": True, "docs_only": False,
                    })

    def test_caller_modules_cannot_replace_stdlib(self):
        self.write("package.json")
        self.write("web/changed.js")
        self.commit("dependency and web")
        expected = {"areas": {"web": True, "bot": False, "docs": False},
                    "full_run": True, "docs_only": False}
        result, fields = self.workflow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fields, expected)
        marker = self.root / "module-imported"
        module = self.repo / "fnmatch.py"
        module.write_text(f'open({str(marker)!r}, "w").write("imported")\n'
                          'def fnmatchcase(path, pattern): return False\n')
        # Positive control: the caller module really overrides an ordinary import.
        control = run_fixture(["python3", "-c", "import fnmatch; assert not fnmatch.fnmatchcase('web/x', 'web/**')"],
                              cwd=self.repo, env=self.env)
        self.assertEqual(control.returncode, 0, control.stderr.decode())
        self.assertEqual(marker.read_text(), "imported")
        marker.unlink()
        before = module.read_bytes()
        for extra_env in (None, {"PYTHONPATH": str(self.repo)}):
            with self.subTest(extra_env=extra_env):
                result, fields = self.workflow(extra_env=extra_env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(fields, expected)
                self.assertFalse(marker.exists(), "detector imported a caller module")
                self.assertEqual(module.read_bytes(), before)

    def test_workspace_and_build_inputs_force_full_run_even_in_code_areas(self):
        for path in ("pnpm-workspace.yaml", "go.work", "go.work.sum",
                     "tsconfig.json", ".cargo/config.toml"):
            for prefix in ("", "web/"):
                with self.subTest(path=prefix + path):
                    self.git("reset", "--hard", self.base)
                    self.write(prefix + path)
                    self.commit("cross-cutting config")
                    self.assert_classification({
                        "areas": {"web": bool(prefix), "bot": False, "docs": False},
                        "full_run": True, "docs_only": False,
                    })

    def test_unclassified_non_doc_paths_force_full_run(self):
        for path in ("tools/new.py", "future-stack/build.config", "notes/scratch.txt"):
            with self.subTest(path=path):
                self.git("reset", "--hard", self.base)
                self.write(path)
                self.commit("unclassified path")
                self.assert_classification({"areas": {"web": False, "bot": False, "docs": False},
                                            "full_run": True, "docs_only": False})

    def test_copyable_example_matches_starter_filters(self):
        usage = (ROOT / "scripts/detect_changes.py").read_text()
        full = re.search(r"--full-run-json '([^']+)'", usage).group(1)
        self.assertEqual(json.loads(full), json.loads(self.full))

    def test_github_and_shared_changes_force_full_run(self):
        for path in (".github/workflows/ci.yml", "shared/util.py", "common/util.py",
                     "build/config.py", "Dockerfile", "nested/Dockerfile",
                     "docker-compose.yml", "nested/docker-compose.yml"):
            with self.subTest(path=path):
                self.git("reset", "--hard", self.base)
                self.write(path)
                self.commit("full trigger")
                self.assert_classification({"areas": {"web": False, "bot": False, "docs": False},
                                            "full_run": True, "docs_only": False})

    def test_renames_preserve_unicode_controls_and_whitespace(self):
        for i, source in enumerate(("web/app.js", "web/é.js", "web/line\nbreak.js",
                                    "web/tab\tfile.js", "web/trailing.js ",
                                    os.fsdecode(b"web/invalid-\xff.js"))):
            with self.subTest(source=source):
                self.git("reset", "--hard", self.base)
                (self.repo / "docs").mkdir(exist_ok=True)
                self.git("mv", source, f"docs/moved-{i}.md")
                self.commit("rename")
                self.assert_classification({"areas": {"web": True, "bot": False, "docs": True},
                                            "full_run": False, "docs_only": False})

    def test_unknown_deleted_code_plus_docs_is_not_docs_only(self):
        (self.repo / "tools/run.py").unlink()
        self.write("docs/run.md")
        self.commit("delete unknown code")
        self.assert_classification({"areas": {"web": False, "bot": False, "docs": True},
                                    "full_run": True, "docs_only": False})

    def test_unknown_rename_into_docs_is_not_docs_only(self):
        (self.repo / "docs").mkdir()
        self.git("mv", "tools/run.py", "docs/run.md")
        self.commit("rename unknown code")
        self.assert_classification({"areas": {"web": False, "bot": False, "docs": True},
                                    "full_run": True, "docs_only": False})

    def test_docs_matching_a_code_area_are_not_docs_only(self):
        self.write("web/README.md")
        self.commit("overlapping areas")
        self.assert_classification({"areas": {"web": True, "bot": False, "docs": True},
                                    "full_run": False, "docs_only": False})

    def test_empty_diff(self):
        self.assert_classification({"areas": {"web": False, "bot": False, "docs": False},
                                    "full_run": False, "docs_only": False})

    def test_default_and_explicit_base(self):
        self.write("web/new.js")
        self.commit("web")
        for base_ref, event_base in (("main", ""), ("", "")):
            with self.subTest(base_ref=base_ref, event_base=event_base):
                result, fields = self.workflow(base_ref=base_ref, event_base=event_base)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(fields["areas"]["web"])

    def test_missing_base_fails_without_publishing(self):
        result, fields = self.workflow(base_ref="missing-ref")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fields, {})

    def test_no_merge_base_fails_without_publishing(self):
        self.git("checkout", "-q", "--orphan", "disconnected")
        self.git("rm", "-r", "--cached", ".")
        self.commit("disconnected root")
        result, fields = self.workflow()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fields, {})

    def test_diff_failure_fails_without_publishing(self):
        # Keep a valid-base positive control, then fail ONLY git diff.
        self.write("web/new.js")
        self.commit("web")
        result, fields = self.workflow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fields["areas"]["web"])
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "git"
        diff_failure_git(fake)
        result, fields = self.workflow(extra_env={"PATH": str(bindir) + os.pathsep + self.env["PATH"]})
        self.assertEqual(result.returncode, 128, result.stderr)
        self.assertEqual(fields, {})
        self.assertEqual(fake.with_suffix(".log").read_text().splitlines(),
                         ["fetch", "merge-base", "diff"])

    def test_binary_file_input_and_non_utf8(self):
        paths = ["web/é.js", "web/line\nbreak.js", "web/tab\tfile.js",
                 "web/trailing.js ", os.fsdecode(b"web/invalid-\xff.js")]
        self.assertEqual(self.reference(paths, file_input=True), self.reference(paths))
        self.assertTrue(self.reference(paths)["areas"]["web"])

    def test_fixture_cannot_mutate_inherited_repository(self):
        self.write("README.md")
        (self.repo / "README.md").write_text("dirty sentinel\n")
        before = {path: (self.repo / path).read_bytes()
                  for path in (".git/config", ".git/index", "README.md")}
        head = self.git("rev-parse", "HEAD").stdout
        status = self.git("status", "--porcelain").stdout
        env = dict(self.env, GIT_DIR=str(self.repo / ".git"), GIT_WORK_TREE=str(self.repo),
                   GIT_COMMON_DIR=str(self.repo / ".git"),
                   GIT_INDEX_FILE=str(self.repo / ".git/index"),
                   GIT_OBJECT_DIRECTORY=str(self.repo / ".git/objects"),
                   GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.worktree",
                   GIT_CONFIG_VALUE_0=str(self.repo), PAPERCLIP_RUN_SCRATCH_DIR=str(self.root))
        result = run_fixture(["bash", str(ROOT / "tests/test_detect_changes.sh")],
                             env=env, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)
        self.assertEqual(self.git("status", "--porcelain").stdout, status)
        for path, content in before.items():
            self.assertEqual((self.repo / path).read_bytes(), content, path)

    def test_fixture_ignores_inherited_git_templates(self):
        templates = self.root / "inherited-templates"
        hook = templates / "hooks/pre-commit"
        hook.parent.mkdir(parents=True)
        hook.write_text('#!/bin/sh\nprintf "called\\n" >> "$TEMPLATE_HOOK_MARKER"\n')
        hook.chmod(0o755)
        hook_content = hook.read_bytes()
        marker = self.root / "hook-called"
        env = dict(self.env, GIT_TEMPLATE_DIR=str(templates),
                   TEMPLATE_HOOK_MARKER=str(marker), PAPERCLIP_RUN_SCRATCH_DIR=str(self.root))
        # Positive control: this executable template really runs during commits.
        control = self.root / "template-control"
        control.mkdir()
        for args in (("init", "-q", "--initial-branch=main"),
                     ("-c", "user.name=ci", "-c", "user.email=ci@example.invalid",
                      "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "control")):
            result = run_fixture([self.real_git, *args], cwd=control, env=env,
                                 text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(), "called\n")
        marker.unlink()
        before_env = env.copy()
        head = self.git("rev-parse", "HEAD").stdout
        config = (self.repo / ".git/config").read_bytes()
        result = run_fixture(["bash", str(ROOT / "tests/test_detect_changes.sh")],
                             env=env, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(marker.exists(), "fixture executed an inherited template hook")
        self.assertEqual(hook.read_bytes(), hook_content)
        self.assertEqual(hook.stat().st_mode & 0o777, 0o755)
        self.assertEqual(env, before_env)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)
        self.assertEqual((self.repo / ".git/config").read_bytes(), config)

    def test_ci_ok_rejects_detection_failure(self):
        script = textwrap.dedent(STARTER.read_text().rsplit("      - run: |\n", 1)[1]
                                 .split("        env:\n", 1)[0])
        for changes_result, expected_exit in (("success", 0), ("failure", 1), ("cancelled", 1)):
            with self.subTest(changes_result=changes_result):
                needs = {"changes": {"result": changes_result}, "test-web": {"result": "skipped"},
                         "test-bot": {"result": "skipped"}}
                result = run_fixture(["bash", "-c", script],
                                     env=dict(self.env, NEEDS_JSON=json.dumps(needs)))
                self.assertEqual(result.returncode, expected_exit, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
