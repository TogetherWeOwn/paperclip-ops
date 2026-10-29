#!/usr/bin/env python3
"""Tests for action_pins.py. Run: python3 -m unittest discover -s .github/scripts -p 'test_action_pins.py'"""
import contextlib
import io
import os
import tempfile
import unittest

import action_pins as ap

SHA = "11d5960a326750d5838078e36cf38b85af677262"
SHORT = SHA[:7]
DIGEST = "a" * 64


def findings(text):
    return ap.lint_text("wf.yml", text)


def values(text):
    return [f.value for f in findings(text)]


class PinShapes(unittest.TestCase):
    def test_tag_ref_fails(self):
        self.assertEqual(["actions/checkout@v4"],
                         values("steps:\n  - uses: actions/checkout@v4\n"))

    def test_short_sha_fails(self):
        self.assertEqual(
            [f"actions/checkout@{SHORT}"],
            values(f"steps:\n  - uses: actions/checkout@{SHORT} # v4\n"))

    def test_full_sha_passes(self):
        self.assertEqual(
            [], findings(f"steps:\n  - uses: actions/checkout@{SHA} # v4\n"))

    def test_local_action_passes(self):
        for ref in ("./local", "./.github/actions/foo", "./workflows/reusable.yml"):
            with self.subTest(ref=ref):
                self.assertEqual([], findings(f"steps:\n  - uses: {ref}\n"))

    def test_docker_digest_passes(self):
        self.assertEqual(
            [], findings(f"steps:\n  - uses: docker://alpine:3@sha256:{DIGEST}\n"))
        self.assertEqual(
            [f"docker://alpine:3"],
            values("steps:\n  - uses: docker://alpine:3\n"))
        self.assertEqual(
            ["docker://alpine:3@sha256:abc"],
            values("steps:\n  - uses: docker://alpine:3@sha256:abc\n"))

    def test_commented_out_uses_ignored(self):
        self.assertEqual([], findings("# uses: actions/checkout@v1\n"))
        self.assertEqual(
            [], findings("      # every other gate here uses: baseline must pass\n"))

    def test_reusable_workflow_with_path(self):
        self.assertEqual(
            [], findings(f"jobs:\n  call:\n    uses: octo/repo/.github/workflows/r.yml@{SHA}\n"))
        self.assertEqual(
            ["octo/repo/.github/workflows/r.yml@v1"],
            values("jobs:\n  call:\n    uses: octo/repo/.github/workflows/r.yml@v1\n"))
        self.assertEqual(
            [], findings("jobs:\n  call:\n    uses: ./.github/workflows/r.yml\n"))

    def test_failure_names_file_and_line(self):
        out = ap.lint_text("a/b.yml", "on: push\nsteps:\n  - uses: actions/checkout@v4\n")
        self.assertEqual(1, len(out))
        self.assertEqual("a/b.yml", out[0].file)
        self.assertEqual(3, out[0].line)
        self.assertIn("a/b.yml:3", out[0].message)

    def test_quoted_value_and_trailing_comment(self):
        self.assertEqual(
            [], findings(f"steps:\n  - uses: 'actions/checkout@{SHA}' # v4\n"))
        self.assertEqual(
            ["actions/checkout@v4"],
            values('steps:\n  - uses: "actions/checkout@v4"\n'))

    def test_flow_mapping_forms(self):
        self.assertEqual(
            ["actions/checkout@v4"],
            values("steps:\n  - { uses: actions/checkout@v4 }\n"))
        self.assertEqual(
            [], findings(f"steps:\n  - {{ uses: actions/checkout@{SHA} }}\n"))

    def test_uses_space_before_colon_fails(self):
        self.assertEqual(
            ["actions/checkout@v4"],
            values("steps:\n  - uses : actions/checkout@v4\n"))

    def test_quoted_key_fails(self):
        self.assertEqual(
            ["actions/checkout@v4"],
            values('steps:\n  "uses": actions/checkout@v4\n'))

    def test_empty_value_fails(self):
        out = findings("steps:\n  - uses:\n")
        self.assertEqual(1, len(out))
        out = findings("steps:\n  - uses: # v4\n")
        self.assertEqual(1, len(out))

    def test_run_value_containing_uses_is_ignored(self):
        self.assertEqual([], findings('steps:\n  - run: "echo uses: foo"\n'))


def run_main(args, cwd=None):
    buf = io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(buf):
            rc = ap.main(args)
    finally:
        os.chdir(old)
    return rc, buf.getvalue()


def write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)


class MainExitCode(unittest.TestCase):
    def test_tag_ref_returns_1_with_error_annotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = os.path.join(tmp, "wf.yml")
            write_tree(tmp, {"wf.yml": "steps:\n  - uses: actions/checkout@v4\n"})
            rc, out = run_main(["action_pins.py", wf])
            self.assertEqual(1, rc)
            self.assertIn("::error file=", out)
            self.assertIn("wf.yml", out)

    def test_pinned_tree_returns_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = os.path.join(tmp, "wf.yml")
            write_tree(
                tmp, {"wf.yml": f"steps:\n  - uses: actions/checkout@{SHA} # v4\n"}
            )
            rc, out = run_main(["action_pins.py", wf])
            self.assertEqual(0, rc)
            self.assertNotIn("::error", out)

    def test_missing_path_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = run_main(
                ["action_pins.py", os.path.join(tmp, "does-not-exist.yml")]
            )
            self.assertNotEqual(0, rc)
            self.assertIn("::error", out)

    def test_empty_default_dir_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = run_main(["action_pins.py"], cwd=tmp)
            self.assertNotEqual(0, rc)
            self.assertIn("::error", out)

    def test_default_glob_finds_yaml_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(
                tmp,
                {
                    ".github/workflows/a.yaml": (
                        "steps:\n  - uses: actions/checkout@v4\n"
                    )
                },
            )
            rc, out = run_main(["action_pins.py"], cwd=tmp)
            self.assertEqual(1, rc)
            self.assertIn("a.yaml", out)

    def test_default_glob_finds_local_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(
                tmp,
                {
                    ".github/actions/foo/action.yml": (
                        "runs:\n  steps:\n    - uses: actions/checkout@v4\n"
                    )
                },
            )
            rc, out = run_main(["action_pins.py"], cwd=tmp)
            self.assertEqual(1, rc)
            self.assertIn("action.yml", out)

    def test_default_glob_on_clean_tree_returns_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(
                tmp,
                {
                    ".github/workflows/a.yml": (
                        f"steps:\n  - uses: actions/checkout@{SHA} # v4\n"
                    )
                },
            )
            rc, out = run_main(["action_pins.py"], cwd=tmp)
            self.assertEqual(0, rc)
            self.assertNotIn("::error", out)

    def test_escape_pins_annotation_encoding(self):
        self.assertEqual("%25", ap._escape("%"))
        self.assertEqual("%0D", ap._escape("\r"))
        self.assertEqual("%0A", ap._escape("\n"))
        self.assertEqual("a%25b%0Dc%0Ad", ap._escape("a%b\rc\nd"))


if __name__ == "__main__":
    unittest.main()
