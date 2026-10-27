#!/usr/bin/env python3
"""Acceptance tests for the explained docs-scope verdict sidecar.

Contract under test (black-box, via the docs_scope.py CLI):

- Opt-in ``--results-json PATH`` writes one ``docs-scope-verdict.v1`` JSON
  object to PATH (written once, replacing any previous content). Stdout
  stays a single ``docs_only=true|false`` line in every mode, byte-identical
  with and without the flag, and the exit status is always 0.
- The object carries exactly: ``schema``, ``verdict`` (bool, equal to the
  printed line), ``decided_by``, ``counts``, ``caveats``, ``input_mode``
  and ``truncated``.
- ``decided_by`` lists the paths that decided the verdict, in input order,
  each entry exactly ``{"path": ..., "rule": ...}`` with rule one of
  ``prose-suffix`` / ``prose-dir`` / ``code-anchor`` / ``empty-list`` /
  ``unreadable`` / ``rename-side``. A true verdict is decided by its prose
  paths; a false verdict is decided by its code vetoes, so a veto is never
  hidden behind prose entries. At most 50 entries are listed; ``counts``
  (``prose`` / ``code`` / ``total``) always reflects every input path, and
  ``truncated`` says whether entries were dropped.
- ``input_mode`` is ``name-status`` (stdin) or ``files`` (``--files``).
- Ambiguous input is never a silent verdict: an empty list and an
  unreadable ``--files`` list keep verdict false and record a caveat
  naming the ambiguity; a bare ``--files`` list cannot prove rename
  handling, so it records a caveat naming renames. ``caveats`` is a list
  of strings, empty for unambiguous name-status input.
- Sidecar write failure still prints the verdict line, warns on stderr
  (no traceback) and exits 0.

Run: python3 -m unittest discover -s .github/scripts -p 'test_docs_scope_results_json.py'
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("docs_scope.py")
RESULTS_NAME = "verdict.json"
EXPECTED_TOP_LEVEL_KEYS = {
    "schema",
    "verdict",
    "decided_by",
    "counts",
    "caveats",
    "input_mode",
    "truncated",
}
PROSE_RULES = {"prose-suffix", "prose-dir"}


class DocsScopeCli:
    """Run docs_scope.py black-box helpers shared by the test cases."""

    def run_cli(self, args=(), stdin_text="", cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
        )

    def run_with_sidecar(self, tmp, args=(), stdin_text=""):
        sidecar = Path(tmp) / RESULTS_NAME
        proc = self.run_cli([*args, "--results-json", str(sidecar)], stdin_text)
        self.assertTrue(
            sidecar.is_file(),
            "sidecar not written: --results-json appears ignored "
            f"(stdout={proc.stdout!r}, stderr={proc.stderr!r})",
        )
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.fail(f"sidecar is not one JSON object at {sidecar}: {exc}")
        return proc, data

    def write_files_list(self, tmp, lines):
        list_path = Path(tmp) / "changed-files.txt"
        list_path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return list_path

    def assert_verdict_line(self, proc, expected):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"docs_only={str(expected).lower()}\n")

    def assert_caveat_names(self, data, needle):
        self.assertIsInstance(data["caveats"], list)
        self.assertTrue(
            all(isinstance(caveat, str) for caveat in data["caveats"]),
            f"caveats must be strings: {data['caveats']!r}",
        )
        self.assertTrue(
            any(needle in caveat.lower() for caveat in data["caveats"]),
            f"no caveat naming {needle!r}: {data['caveats']!r}",
        )


class TestVerdictObjectShape(DocsScopeCli, unittest.TestCase):
    def test_exact_top_level_fields_and_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(
                tmp, stdin_text="M\tREADME.md\nM\tdocs/diagram.png\n"
            )
        self.assert_verdict_line(proc, True)
        self.assertEqual(set(data), EXPECTED_TOP_LEVEL_KEYS)
        self.assertEqual(data["schema"], "docs-scope-verdict.v1")
        self.assertIs(data["verdict"], True)
        self.assertIs(data["truncated"], False)
        self.assertEqual(data["input_mode"], "name-status")
        self.assertEqual(set(data["counts"]), {"prose", "code", "total"})
        self.assertTrue(all(type(value) is int for value in data["counts"].values()))
        self.assertEqual(data["caveats"], [])
        self.assertEqual(proc.stderr, "")
        for entry in data["decided_by"]:
            self.assertEqual(set(entry), {"path", "rule"})

    def test_written_once_replacing_previous_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / RESULTS_NAME
            sidecar.write_text("SENTINEL-GARBAGE-" * 40, encoding="utf-8")
            proc, data = self.run_with_sidecar(tmp, stdin_text="M\tREADME.md\n")
            self.assert_verdict_line(proc, True)
            self.assertIs(data["verdict"], True)
            self.assertNotIn("SENTINEL", sidecar.read_text(encoding="utf-8"))

    def test_idempotent_reruns_are_byte_identical(self):
        outputs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                self.run_with_sidecar(
                    tmp, stdin_text="M\tREADME.md\nM\ttool_drift.sh\n"
                )
                outputs.append((Path(tmp) / RESULTS_NAME).read_bytes())
        self.assertEqual(outputs[0], outputs[1])


class TestDecidingPathsAndRules(DocsScopeCli, unittest.TestCase):
    def test_all_prose_lists_prose_paths_with_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(
                tmp, stdin_text="M\tREADME.md\nM\tdocs/diagram.png\n"
            )
        self.assert_verdict_line(proc, True)
        self.assertEqual(
            data["decided_by"],
            [
                {"path": "README.md", "rule": "prose-suffix"},
                {"path": "docs/diagram.png", "rule": "prose-dir"},
            ],
        )
        self.assertEqual(data["counts"], {"prose": 2, "code": 0, "total": 2})

    def test_docs_markdown_rule_is_a_prose_rule(self):
        # docs/*.md matches both the suffix and the directory prose rules;
        # either prose rule name is an honest explanation for it.
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(tmp, stdin_text="M\tdocs/guide.md\n")
        self.assertEqual(len(data["decided_by"]), 1)
        self.assertEqual(data["decided_by"][0]["path"], "docs/guide.md")
        self.assertIn(data["decided_by"][0]["rule"], PROSE_RULES)

    def test_license_bare_name_is_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(tmp, stdin_text="A\tLICENSE\n")
        self.assert_verdict_line(proc, True)
        self.assertEqual(data["counts"], {"prose": 1, "code": 0, "total": 1})
        self.assertEqual(data["decided_by"][0]["path"], "LICENSE")
        self.assertIn(data["decided_by"][0]["rule"], PROSE_RULES)

    def test_single_code_path_is_the_veto(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(
                tmp, stdin_text="M\t.github/workflows/ci.yml\n"
            )
        self.assert_verdict_line(proc, False)
        self.assertIs(data["verdict"], False)
        self.assertEqual(
            data["decided_by"],
            [{"path": ".github/workflows/ci.yml", "rule": "code-anchor"}],
        )
        self.assertEqual(data["counts"], {"prose": 0, "code": 1, "total": 1})
        self.assertEqual(data["caveats"], [])

    def test_github_prose_still_vetoes_as_code_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(
                tmp, stdin_text="M\t.github/pull_request_template.md\n"
            )
        self.assertIs(data["verdict"], False)
        self.assertEqual(
            data["decided_by"],
            [{"path": ".github/pull_request_template.md", "rule": "code-anchor"}],
        )

    def test_nested_markdown_outside_docs_is_a_code_veto(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(tmp, stdin_text="M\tsrc/guide.md\n")
        self.assertIs(data["verdict"], False)
        self.assertEqual(
            data["decided_by"], [{"path": "src/guide.md", "rule": "code-anchor"}]
        )

    def test_mixed_input_decided_by_only_the_veto(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(
                tmp, stdin_text="M\tREADME.md\nM\ttool_drift.sh\n"
            )
        self.assert_verdict_line(proc, False)
        self.assertEqual(
            data["decided_by"], [{"path": "tool_drift.sh", "rule": "code-anchor"}]
        )
        self.assertEqual(data["counts"], {"prose": 1, "code": 1, "total": 2})

    def test_vetoes_keep_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(
                tmp, stdin_text="M\tzzz_last.sh\nM\taaa_first.py\n"
            )
        self.assertEqual(
            [entry["path"] for entry in data["decided_by"]],
            ["zzz_last.sh", "aaa_first.py"],
        )

    def test_rename_code_side_decides_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(
                tmp, stdin_text="R100\tsrc/check.sh\tdocs/check.md\n"
            )
        self.assert_verdict_line(proc, False)
        # Both rename sides are counted; the code side is the veto and is
        # explained as the rename side a bare name list would have missed.
        self.assertEqual(data["counts"], {"prose": 1, "code": 1, "total": 2})
        self.assertEqual(
            data["decided_by"], [{"path": "src/check.sh", "rule": "rename-side"}]
        )

    def test_rename_prose_to_prose_counts_both_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(
                tmp, stdin_text="R100\tdocs/old.md\tdocs/new.md\n"
            )
        self.assert_verdict_line(proc, True)
        self.assertEqual(data["counts"], {"prose": 2, "code": 0, "total": 2})
        self.assertEqual(
            [entry["path"] for entry in data["decided_by"]],
            ["docs/old.md", "docs/new.md"],
        )
        for entry in data["decided_by"]:
            self.assertIn(entry["rule"], PROSE_RULES | {"rename-side"})


class TestTruncationAccounting(DocsScopeCli, unittest.TestCase):
    def test_sixty_prose_truncates_to_first_fifty(self):
        paths = [f"docs/page-{index:02d}.md" for index in range(60)]
        stdin_text = "".join(f"M\t{path}\n" for path in paths)
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(tmp, stdin_text=stdin_text)
        self.assert_verdict_line(proc, True)
        self.assertEqual(data["counts"], {"prose": 60, "code": 0, "total": 60})
        self.assertIs(data["truncated"], True)
        self.assertEqual(
            [entry["path"] for entry in data["decided_by"]], paths[:50]
        )

    def test_exactly_fifty_is_not_truncated(self):
        paths = [f"docs/page-{index:02d}.md" for index in range(50)]
        stdin_text = "".join(f"M\t{path}\n" for path in paths)
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(tmp, stdin_text=stdin_text)
        self.assertIs(data["truncated"], False)
        self.assertEqual(len(data["decided_by"]), 50)
        self.assertEqual(data["counts"]["total"], 50)

    def test_fifty_one_is_truncated(self):
        paths = [f"docs/page-{index:02d}.md" for index in range(51)]
        stdin_text = "".join(f"M\t{path}\n" for path in paths)
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(tmp, stdin_text=stdin_text)
        self.assertIs(data["truncated"], True)
        self.assertEqual(len(data["decided_by"]), 50)
        self.assertEqual(data["counts"]["total"], 51)

    def test_sixty_code_vetoes_truncate_to_first_fifty(self):
        paths = [f"src/tool-{index:02d}.py" for index in range(60)]
        stdin_text = "".join(f"M\t{path}\n" for path in paths)
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(tmp, stdin_text=stdin_text)
        self.assertIs(data["verdict"], False)
        self.assertEqual(data["counts"], {"prose": 0, "code": 60, "total": 60})
        self.assertIs(data["truncated"], True)
        self.assertEqual(
            [entry["path"] for entry in data["decided_by"]], paths[:50]
        )

    def test_late_veto_is_never_truncated_away(self):
        prose = [f"docs/page-{index:02d}.md" for index in range(55)]
        stdin_text = "".join(f"M\t{path}\n" for path in prose)
        stdin_text += "M\tsrc/late_veto.py\n"
        with tempfile.TemporaryDirectory() as tmp:
            _, data = self.run_with_sidecar(tmp, stdin_text=stdin_text)
        self.assertIs(data["verdict"], False)
        self.assertEqual(data["counts"], {"prose": 55, "code": 1, "total": 56})
        self.assertIs(data["truncated"], False)
        self.assertEqual(
            data["decided_by"], [{"path": "src/late_veto.py", "rule": "code-anchor"}]
        )


class TestCaveatsOnAmbiguousInput(DocsScopeCli, unittest.TestCase):
    def test_empty_stdin_fails_closed_with_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(tmp, stdin_text="")
        self.assert_verdict_line(proc, False)
        self.assertIs(data["verdict"], False)
        self.assertEqual(data["counts"], {"prose": 0, "code": 0, "total": 0})
        self.assertEqual(
            data["decided_by"], [{"path": None, "rule": "empty-list"}]
        )
        self.assertEqual(data["input_mode"], "name-status")
        self.assert_caveat_names(data, "empty")

    def test_empty_files_list_fails_closed_with_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            list_path = self.write_files_list(tmp, [])
            proc, data = self.run_with_sidecar(tmp, args=["--files", str(list_path)])
        self.assert_verdict_line(proc, False)
        self.assertEqual(data["counts"], {"prose": 0, "code": 0, "total": 0})
        self.assertEqual(
            data["decided_by"], [{"path": None, "rule": "empty-list"}]
        )
        self.assertEqual(data["input_mode"], "files")
        self.assert_caveat_names(data, "empty")

    def test_unreadable_files_list_fails_closed_with_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.txt"
            proc, data = self.run_with_sidecar(tmp, args=["--files", str(missing)])
        self.assert_verdict_line(proc, False)
        self.assertIs(data["verdict"], False)
        self.assertEqual(data["counts"], {"prose": 0, "code": 0, "total": 0})
        self.assertEqual(
            data["decided_by"], [{"path": str(missing), "rule": "unreadable"}]
        )
        self.assertEqual(data["input_mode"], "files")
        self.assert_caveat_names(data, "read")

    def test_bare_files_list_carries_rename_caveat(self):
        # A --files list holds bare names only: it cannot prove that no
        # rename hid a code-side path, so the explanation must say so.
        with tempfile.TemporaryDirectory() as tmp:
            list_path = self.write_files_list(tmp, ["README.md", "docs/guide.md"])
            proc, data = self.run_with_sidecar(tmp, args=["--files", str(list_path)])
        self.assert_verdict_line(proc, True)
        self.assertIs(data["verdict"], True)
        self.assertEqual(data["input_mode"], "files")
        self.assertEqual(data["counts"], {"prose": 2, "code": 0, "total": 2})
        self.assert_caveat_names(data, "rename")

    def test_files_mode_code_veto(self):
        with tempfile.TemporaryDirectory() as tmp:
            list_path = self.write_files_list(tmp, ["README.md", "tool_drift.sh"])
            proc, data = self.run_with_sidecar(tmp, args=["--files", str(list_path)])
        self.assert_verdict_line(proc, False)
        self.assertEqual(data["input_mode"], "files")
        self.assertEqual(
            data["decided_by"], [{"path": "tool_drift.sh", "rule": "code-anchor"}]
        )
        self.assertEqual(data["counts"], {"prose": 1, "code": 1, "total": 2})
        self.assertIsInstance(data["caveats"], list)
        self.assertTrue(
            all(isinstance(caveat, str) for caveat in data["caveats"]),
            f"caveats must be strings: {data['caveats']!r}",
        )


class TestStdoutParityAndGithubOutput(DocsScopeCli, unittest.TestCase):
    # (stdin_text, expected verdict) for name-status input.
    STDIN_MATRIX = [
        ("M\tREADME.md\n", True),
        ("M\tREADME.md\nM\tdocs/diagram.png\n", True),
        ("A\tLICENSE\n", True),
        ("M\ttool_drift.sh\n", False),
        ("M\tREADME.md\nM\ttool_drift.sh\n", False),
        ("M\t.github/workflows/ci.yml\n", False),
        ("R100\tsrc/check.sh\tdocs/check.md\n", False),
        ("R100\tdocs/old.md\tdocs/new.md\n", True),
        ("", False),
    ]

    def test_object_verdict_equals_printed_line_across_matrix(self):
        for stdin_text, expected in self.STDIN_MATRIX:
            with self.subTest(stdin=stdin_text), tempfile.TemporaryDirectory() as tmp:
                proc, data = self.run_with_sidecar(tmp, stdin_text=stdin_text)
                self.assert_verdict_line(proc, expected)
                self.assertIs(data["verdict"], expected)

    def test_stdout_and_exit_identical_with_and_without_flag(self):
        for stdin_text, expected in self.STDIN_MATRIX:
            with self.subTest(stdin=stdin_text), tempfile.TemporaryDirectory() as tmp:
                plain = self.run_cli(stdin_text=stdin_text)
                explained, _ = self.run_with_sidecar(tmp, stdin_text=stdin_text)
                self.assert_verdict_line(plain, expected)
                self.assertEqual(explained.stdout, plain.stdout)
                self.assertEqual(explained.returncode, plain.returncode)

    def test_files_mode_stdout_parity_with_and_without_flag(self):
        cases = [(["README.md", "docs/guide.md"], True), (["tool_drift.sh"], False), ([], False)]
        for lines, expected in cases:
            with self.subTest(lines=lines), tempfile.TemporaryDirectory() as tmp:
                list_path = self.write_files_list(tmp, lines)
                plain = self.run_cli(args=["--files", str(list_path)])
                explained, data = self.run_with_sidecar(
                    tmp, args=["--files", str(list_path)]
                )
                self.assert_verdict_line(plain, expected)
                self.assertEqual(explained.stdout, plain.stdout)
                self.assertEqual(explained.returncode, plain.returncode)
                self.assertIs(data["verdict"], expected)

    def test_no_sidecar_created_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(stdin_text="M\tREADME.md\n", cwd=tmp)
            self.assert_verdict_line(proc, True)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_github_output_shaped_consumption(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, data = self.run_with_sidecar(
                tmp, stdin_text="M\tREADME.md\nM\ttool_drift.sh\n"
            )
            github_output = Path(tmp) / "github-output.txt"
            with github_output.open("a", encoding="utf-8") as handle:
                handle.write(proc.stdout)
            lines = github_output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["docs_only=false"])
            sourced = subprocess.run(
                ["bash", "-c", 'source "$1"; printf %s "$docs_only"', "_", str(github_output)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(sourced.returncode, 0, sourced.stderr)
            self.assertEqual(sourced.stdout, "false")
            self.assertIs(data["verdict"], False)


class TestSidecarWriteFailure(DocsScopeCli, unittest.TestCase):
    def assert_write_failure_is_controlled(self, proc, expected):
        self.assert_verdict_line(proc, expected)
        self.assertNotEqual(
            proc.stderr.strip(),
            "",
            "sidecar write failure must warn on stderr",
        )
        self.assertNotIn("Traceback", proc.stderr)

    def test_sidecar_path_is_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(
                ["--results-json", tmp], stdin_text="M\tREADME.md\n"
            )
        self.assert_write_failure_is_controlled(proc, True)

    def test_sidecar_parent_directory_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "no-such-dir" / RESULTS_NAME)
            proc = self.run_cli(
                ["--results-json", target], stdin_text="M\ttool_drift.sh\n"
            )
        self.assert_write_failure_is_controlled(proc, False)


class TestDefaultBehaviorUnchanged(DocsScopeCli, unittest.TestCase):
    """Without the flag the shipped classification is the shipped behavior."""

    def test_default_verdicts_without_flag(self):
        cases = [
            ("M\tREADME.md\n", True),
            ("README.md\n", True),
            ("M\tsrc/guide.md\n", False),
            ("M\t.github/pull_request_template.md\n", False),
            ("R100\tsrc/check.sh\tdocs/check.md\n", False),
            ("", False),
        ]
        for stdin_text, expected in cases:
            with self.subTest(stdin=stdin_text):
                proc = self.run_cli(stdin_text=stdin_text)
                self.assert_verdict_line(proc, expected)

    def test_default_files_mode_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            list_path = self.write_files_list(tmp, ["README.md", "docs/guide.md"])
            proc = self.run_cli(args=["--files", str(list_path)])
        self.assert_verdict_line(proc, True)


if __name__ == "__main__":
    unittest.main()
