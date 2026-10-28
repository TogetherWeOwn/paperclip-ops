#!/usr/bin/env python3
"""Structural guard for docs-fast-path.yml (case_a97273bd).

Each job in that workflow is a full GARM ephemeral VM lifecycle on the pool
that also serves the required `pr-lint` check. This pins the three properties
that keep it cheap: one job, a workflow-level `paths:` filter so code-only PRs
start nothing, and a filter that still covers everything `docs_scope.py` calls
prose so the fast path keeps working. Stdlib only (no PyYAML on the runner).
"""
import os
import re
import subprocess
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
        self.env = {
            **os.environ,
            "BASE_REF": "main",
            "RUNNER_TEMP": str(self.root / ".git"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
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
        for path in prose_samples() + ["public-paths-freeze.txt", "GUIDE.MD"]:
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

    def test_dash_named_unbalanced_prose_fails(self):
        # A leading dash must not let grep read the filename as an option.
        for path in ("-n.md", "-x.txt"):
            with self.subTest(path=path):
                self.write(path, "```python\nprint('unclosed')\n")
                self.assertTrue(self.commit_and_classify())
                result = self.run_checks()
                self.git("update-ref", "refs/remotes/origin/main", "HEAD")
                self.assertNotEqual(result.returncode, 0)

    def test_empty_prose_fails(self):
        self.write("notes.txt", "")
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


if __name__ == "__main__":
    unittest.main()
