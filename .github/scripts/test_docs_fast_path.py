#!/usr/bin/env python3
"""Structural guard for docs-fast-path.yml (case_a97273bd).

Each job in that workflow is a full GARM ephemeral VM lifecycle on the pool
that also serves the required `pr-lint` check. This pins the three properties
that keep it cheap: one job, a workflow-level `paths:` filter so code-only PRs
start nothing, and a filter that still covers everything `docs_scope.py` calls
prose so the fast path keeps working. Stdlib only (no PyYAML on the runner).
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from docs_scope import PROSE_DIRS, PROSE_SUFFIXES, classify, is_prose_path, parse_name_status

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "docs-fast-path.yml"
BARE_LICENSE_NAMES = ("LICENSE", "LICENCE", "LICENSE-MIT", "LICENSE-APACHE")


def glob_to_regex(pattern: str) -> re.Pattern:
    """GitHub filter glob: `*` stays inside a path segment, `**` crosses them."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def trigger_paths(text: str, trigger: str) -> list[str]:
    """The `paths:` list under `on: <trigger>:`, or [] when it has none."""
    on = re.search(r"(?ms)^on:\n(?P<body>.*?)(?=^[A-Za-z])", text)
    assert on, "no block-form `on:` section"
    paths, in_trigger, in_paths = [], False, False
    for line in on.group("body").splitlines():
        m = re.match(r"^( *)([A-Za-z_-]+):", line)
        if m and len(m.group(1)) == 2:
            in_trigger, in_paths = m.group(2) == trigger, False
        elif m and in_trigger:
            in_paths = m.group(2) == "paths"
        elif in_trigger and in_paths:
            item = re.match(r"^\s+-\s+['\"]?(.+?)['\"]?\s*$", line)
            if item:
                paths.append(item.group(1))
    return paths


def prose_samples() -> list[str]:
    """One path per prose shape the classifier accepts, derived from its constants."""
    samples = [f"notes{suffix}" for suffix in PROSE_SUFFIXES]
    samples += [f"{d}guide.md" for d in PROSE_DIRS]
    samples += [f"{d}deep/nested/guide.md" for d in PROSE_DIRS]
    samples += [f"{d}no-extension" for d in PROSE_DIRS]
    samples += list(BARE_LICENSE_NAMES)
    return samples


class TestDocsFastPathWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()

    def test_samples_are_really_prose(self):
        # Guard the guard: if the classifier drops a shape, fail here, not silently.
        for path in prose_samples():
            self.assertTrue(is_prose_path(path), path)

    def test_exactly_one_job(self):
        jobs = re.search(r"(?ms)^jobs:\n(?P<body>.*)\Z", self.text)
        self.assertIsNotNone(jobs)
        names = re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\n", jobs.group("body"))
        self.assertEqual(names, ["docs-verdict"])

    def test_pull_request_and_push_both_filtered(self):
        for trigger in ("pull_request", "push"):
            self.assertTrue(trigger_paths(self.text, trigger), f"{trigger} has no paths filter")

    def test_filter_covers_every_prose_shape(self):
        for trigger in ("pull_request", "push"):
            patterns = [glob_to_regex(p) for p in trigger_paths(self.text, trigger)]
            for path in prose_samples():
                self.assertTrue(
                    any(p.match(path) for p in patterns),
                    f"{trigger} filter would not start for prose path {path!r}",
                )

    def test_filter_skips_code_only_prs(self):
        # The point of the filter: these must NOT start a VM.
        code = [
            "src/app.py",
            "ops/case_658bf6b9/push-ci-queue-age.sh",
            ".github/workflows/ci.yml",
            ".github/scripts/docs_scope.py",
            "src/guide.md",
            "package.json",
        ]
        for trigger in ("pull_request", "push"):
            patterns = [glob_to_regex(p) for p in trigger_paths(self.text, trigger)]
            for path in code:
                self.assertFalse(
                    any(p.match(path) for p in patterns),
                    f"{trigger} filter would start a VM for code path {path!r}",
                )

    def test_marked_non_required(self):
        # ci_policy.py exempts only marked workflows from its paths-filter warning.
        self.assertRegex(self.text, r"ci-policy:\s*non-required\b")

    def test_public_job_uses_github_hosted_runner(self):
        self.assertIn("runs-on: ubuntu-latest", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*runs-on:.*self-hosted")


class TestFastProseChecks(unittest.TestCase):
    """Run the real workflow shell against isolated git diffs, not a copied selector."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Git repository/config overrides can redirect writes into the caller;
        # shell startup files can change the extracted workflow's execution.
        self.env = {
            **{k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")},
            "BASE_REF": "main",
            "RUNNER_TEMP": str(self.root / ".git"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.write("README.md", "Baseline prose.\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        match = re.search(
            r"(?ms)^      - name: Fast prose checks .*?^        run: \|\n"
            r"(?P<body>.*?)(?=^      - name:)",
            WORKFLOW.read_text(),
        )
        self.assertIsNotNone(match)
        self.script = textwrap.dedent(match.group("body"))

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, env=self.env,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit_and_classify(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture change")
        lines = self.git("diff", "--name-status", "origin/main...HEAD").splitlines()
        return classify(parse_name_status(lines))

    def run_checks(self):
        return subprocess.run(
            ["bash", "-c", self.script], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=10,
        )

    def test_every_accepted_prose_shape_is_selected_and_checked(self):
        for path in prose_samples() + ["docs/notes.txt", "GUIDE.MD"]:
            with self.subTest(path=path):
                self.write(path, "Non-empty prose.\n")
                self.assertTrue(self.commit_and_classify())
                result = self.run_checks()
                # Keep each diff single-file even if a preceding subtest fails.
                self.git("update-ref", "refs/remotes/origin/main", "HEAD")
                self.assertEqual(result.returncode, 0, result.stderr)
                selected = (self.root / ".git/docs-files.txt").read_text().splitlines()
                self.assertEqual(selected, [path])

    def test_unbalanced_markdown_fails(self):
        self.write("README.md", "```python\nprint('unclosed')\n")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)

    def selected_paths(self):
        return (self.root / ".git/docs-files.txt").read_text().splitlines()

    def test_existing_markdown_edit_succeeds(self):
        self.write("README.md", "Updated prose.\n")
        self.assertTrue(self.commit_and_classify())
        result = self.run_checks()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.selected_paths(), ["README.md"])

    def test_dash_named_unbalanced_prose_fails(self):
        # A leading dash must not let grep read the filename as an option.
        self.write("-n.md", "```python\nprint('unclosed')\n")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)
        self.assertEqual(self.selected_paths(), ["-n.md"])

    def test_balanced_dash_named_markdown_succeeds(self):
        self.write("-n.md", "```python\nprint('closed')\n```\n")
        self.assertTrue(self.commit_and_classify())
        result = self.run_checks()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.selected_paths(), ["-n.md"])

    def test_all_files_are_validated_with_dash_named_markdown(self):
        self.write("-n.md", "Valid first file.\n")
        self.write("-x.md", "```python\nprint('unclosed')\n")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)
        self.assertEqual(self.selected_paths(), ["-n.md", "-x.md"])

    def test_later_malformed_markdown_is_rejected(self):
        self.write("README.md", "Valid first file.\n")
        self.write("z-last.md", "```python\nprint('unclosed')\n")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)
        self.assertEqual(self.selected_paths(), ["README.md", "z-last.md"])

    def test_non_markdown_literal_backticks_succeed(self):
        for path, content in (("notes.rst", "Example::\n\n``` quoted literal content\n"),
                              ("docs/notes.txt", "Literal backticks:\n```\n")):
            with self.subTest(path=path):
                self.write(path, content)
                self.assertTrue(self.commit_and_classify())
                result = self.run_checks()
                self.git("update-ref", "refs/remotes/origin/main", "HEAD")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.selected_paths(), [path])

    def test_uppercase_markdown_still_validates_fences(self):
        self.write("GUIDE.MD", "```python\nprint('unclosed')\n")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)
        self.assertEqual(self.selected_paths(), ["GUIDE.MD"])

    def test_symlink_to_directory_is_rejected(self):
        (self.root / "notes.rst").symlink_to(".")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)
        self.assertEqual(self.selected_paths(), ["notes.rst"])

    def test_symlink_to_external_regular_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as target_dir:
            target = Path(target_dir) / "target.md"
            target.write_text("Balanced external content.\n")
            (self.root / "notes.md").symlink_to(target)
            self.assertTrue(self.commit_and_classify())
            self.assertNotEqual(self.run_checks().returncode, 0)
            self.assertEqual(self.selected_paths(), ["notes.md"])

    def test_grep_read_error_is_not_a_balanced_file(self):
        self.write("README.md", "Updated prose.\n")
        self.assertTrue(self.commit_and_classify())
        # A read error can print a zero count; only exit 1 means no matches.
        self.write(".git/bin/grep", "#!/bin/sh\nprintf '0\\n'\nexit 2\n")
        (self.root / ".git/bin/grep").chmod(0o755)
        self.env["PATH"] = str(self.root / ".git/bin") + os.pathsep + self.env["PATH"]
        self.assertNotEqual(self.run_checks().returncode, 0)
        self.assertEqual(self.selected_paths(), ["README.md"])

    def test_non_markdown_read_error_is_rejected(self):
        self.write("notes.rst", "Regular nonempty prose.\n")
        self.assertTrue(self.commit_and_classify())
        self.write(".git/bin/cat", "#!/bin/sh\nexit 2\n")
        (self.root / ".git/bin/cat").chmod(0o755)
        self.env["PATH"] = str(self.root / ".git/bin") + os.pathsep + self.env["PATH"]
        self.assertNotEqual(self.run_checks().returncode, 0)
        self.assertEqual(self.selected_paths(), ["notes.rst"])

    def test_empty_machine_allowlist_uses_full_ci(self):
        self.write("kofra_preview_parity_allowlist.txt", "# No exemptions.\n")
        self.commit_and_classify()
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.write("README.md", "Updated documentation.\n")
        self.write("kofra_preview_parity_allowlist.txt", "")
        spec = importlib.util.spec_from_file_location(
            "parity", WORKFLOW.parents[2] / "kofra_preview_parity.py")
        parity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parity)
        self.assertEqual(parity.load_allowlist(self.root / "kofra_preview_parity_allowlist.txt"), {})
        self.assertFalse(self.commit_and_classify())

    def test_empty_prose_fails(self):
        self.write("notes.rst", "")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)

    def test_deleted_prose_fails(self):
        self.git("rm", "README.md")
        self.assertTrue(self.commit_and_classify())
        self.assertNotEqual(self.run_checks().returncode, 0)

    def test_prose_rename_validates_destination(self):
        (self.root / "docs").mkdir()
        self.git("mv", "README.md", "docs/renamed.md")
        self.assertTrue(self.commit_and_classify())
        result = self.run_checks()
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = (self.root / ".git/docs-files.txt").read_text().splitlines()
        self.assertEqual(selected, ["docs/renamed.md"])

    def test_mixed_code_is_not_docs_only(self):
        self.write("notes.txt", "Prose.\n")
        self.write("app.py", "print('code')\n")
        self.assertFalse(self.commit_and_classify())

    def test_code_renamed_to_prose_is_not_docs_only(self):
        self.write("app.py", "print('code')\n")
        self.commit_and_classify()
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("mv", "app.py", "notes.txt")
        self.assertFalse(self.commit_and_classify())


class TestFixtureIsolation(unittest.TestCase):
    def test_inherited_overrides_cannot_modify_caller_state(self):
        for override in ("index", "repository", "config", "template", "shell"):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                caller = root / "caller"
                caller.mkdir()
                env = {k: v for k, v in os.environ.items()
                       if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
                env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

                def git(*args):
                    return subprocess.run(
                        ["git", *args], cwd=caller, env=env, check=True,
                        capture_output=True, timeout=10).stdout

                git("init", "-q")
                git("config", "user.email", "test@example.com")
                git("config", "user.name", "Test")
                for name in ("README.md", "keep.txt"):
                    (caller / name).write_text("Caller content.\n")
                git("add", ".")
                git("-c", "commit.gpgsign=false", "commit", "-qm", "caller")
                # Also preserve staged but uncommitted caller changes.
                (caller / "keep.txt").write_text("Pending caller content.\n")
                git("add", "keep.txt")
                index = (caller / ".git/index").read_bytes()
                config = (caller / ".git/config").read_bytes()
                refs = git("show-ref", "--head")
                marker = root / "startup-ran"
                hook = root / "template" / "hooks" / "pre-commit"
                hook.parent.mkdir(parents=True)
                hook.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\n")
                hook.chmod(0o755)
                startup = root / "startup.sh"
                startup.write_text(f"printf ran > '{marker}'\n")
                hostile = env.copy()
                overrides = {
                    "index": {"GIT_INDEX_FILE": str(caller / ".git/index")},
                    "repository": {"GIT_DIR": str(caller / ".git"),
                                   "GIT_WORK_TREE": str(caller)},
                    "config": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                               "GIT_CONFIG_VALUE_0": str(hook.parent)},
                    "template": {"GIT_TEMPLATE_DIR": str(root / "template")},
                    "shell": {"BASH_ENV": str(startup), "ENV": str(startup)},
                }
                hostile.update(overrides[override])
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()),
                     "TestFastProseChecks.test_existing_markdown_edit_succeeds"],
                    cwd=caller, env=hostile, capture_output=True, text=True, timeout=30)
                self.assertEqual((caller / ".git/index").read_bytes(), index,
                                 f"{override} changed caller index")
                self.assertEqual((caller / ".git/config").read_bytes(), config,
                                 f"{override} changed caller config")
                self.assertEqual(git("show-ref", "--head"), refs,
                                 f"{override} changed caller refs")
                self.assertFalse(marker.exists(), f"{override} executed inherited startup code")
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
