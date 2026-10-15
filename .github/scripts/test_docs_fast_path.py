#!/usr/bin/env python3
"""Structural guard for docs-fast-path.yml (case_a97273bd).

Each job in that workflow is a full GARM ephemeral VM lifecycle on the pool
that also serves the required `pr-lint` check. This pins the three properties
that keep it cheap: one job, a workflow-level `paths:` filter so code-only PRs
start nothing, and a filter that still covers everything `docs_scope.py` calls
prose so the fast path keeps working. Stdlib only (no PyYAML on the runner).
"""
import re
import unittest
from pathlib import Path

from docs_scope import PROSE_DIRS, PROSE_SUFFIXES, is_prose_path

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

    def test_job_stays_on_ephemeral_pool(self):
        self.assertIn("runs-on: [self-hosted, two-ephemeral]", self.text)


if __name__ == "__main__":
    unittest.main()
