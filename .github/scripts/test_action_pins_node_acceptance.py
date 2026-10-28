#!/usr/bin/env python3
"""Independent acceptance tests for bounded safe-node action-pin scans.

Run with the explicitly prepared PyYAML==6.0.2 interpreter:
    python3 -B -m unittest discover -s .github/scripts \\
        -p 'test_action_pins_node_acceptance.py'

Expectations are literal invocation positions, values and key lines, not an
alternate extractor. All scans are real, offline subprocesses with finite
limits and synthetic local inputs. Dependency absence/version fixtures affect
only subprocess imports; they never replace the production verdict. Existing
JSON acceptance assertions remain independently owned and unchanged.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_action_pins_json_acceptance as frozen

SCRIPT = Path(__file__).resolve().with_name("action_pins.py")
SHA = "11d5960a326750d5838078e36cf38b85af677262"
DIGEST = "a" * 64
CLEAN = "uses: ./local\n"
BAD = "uses: actions/checkout@v4\n"
ERROR_ACTIONS = {
    "input-missing": "fix_input_path",
    "input-unreadable": "fix_input_permissions",
    "input-invalid-encoding": "fix_input_encoding",
    "no-inputs-discovered": "provide_input_files",
    "input-parser-unavailable": "prepare_parser_dependency",
    "input-invalid-yaml": "fix_input_yaml",
    "input-unsupported-yaml": "simplify_input_yaml",
    "input-resource-limit": "reduce_input_complexity",
}
ASCII_ENV = {
    "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0",
    "PYTHONCOERCECLOCALE": "0",
}


def invoke(args, cwd, *, extra_env=None, flags=(), script=SCRIPT):
    env = frozen.clean_env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-B", *flags, str(script), *args], cwd=cwd,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )


def write(root, name, source):
    return frozen.write(root, name, source)


@contextlib.contextmanager
def deep_action_tree(root, depth):
    """Keep creation/cleanup stack-safe even when the scanner is not."""
    actions = Path(root, ".github/actions")
    actions.mkdir(parents=True)
    fd = os.open(actions, os.O_RDONLY | os.O_DIRECTORY)
    created = 0
    leaf_created = False
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            created += 1
        leaf = os.open("action.yml", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                       0o600, dir_fd=fd)
        leaf_created = True
        with os.fdopen(leaf, "wb") as stream:
            stream.write(b"runs:\n  steps:\n    - uses: actions/checkout@v4\n")
        yield ".github/actions/" + "d/" * depth + "action.yml"
    finally:
        if leaf_created:
            os.unlink("action.yml", dir_fd=fd)
        for _ in range(created):
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = parent
            os.rmdir("d", dir_fd=fd)
        os.close(fd)


class NodeContract(unittest.TestCase):
    def report(self, proc, *, requested, checked, refused, complete,
               findings=(), errors=()):
        self.assertEqual(b"", proc.stderr)
        self.assertNotIn(b"Traceback", proc.stdout)
        try:
            obj = json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self.fail(f"stdout is not one UTF-8 JSON object: {error}; "
                      f"first bytes={proc.stdout[:120]!r}")
        self.assertEqual("action-pins.v1", obj["schema"])
        self.assertEqual((requested, checked, refused),
                         (obj["requested"], obj["checked"], obj["refused"]))
        for key in ("requested", "checked", "refused"):
            self.assertIs(type(obj[key]), int)
        self.assertEqual(requested, checked + refused)
        self.assertIs(complete, obj["scan_complete"])
        ok = complete and not findings and not errors
        self.assertIs(ok, obj["ok"])
        self.assertIs(False, obj["retryable"])
        self.assertEqual(0 if ok else 1, proc.returncode)
        self.assertEqual(list(findings), [
            (f["path"], f["line"], f["value"]) for f in obj["findings"]])
        self.assertEqual(list(errors), [
            (e["path"], e["code"]) for e in obj["errors"]])
        self.assertEqual(obj["findings"], sorted(
            obj["findings"], key=lambda f: (f["path"], f["line"],
                                           f["code"], f["value"])))
        self.assertEqual(obj["errors"], sorted(
            obj["errors"], key=lambda e: (e["path"] or "", e["code"])))
        for finding in obj["findings"]:
            self.assertEqual("unpinned-action", finding["code"])
            self.assertIs(type(finding["line"]), int)
            self.assertGreater(finding["line"], 0)
            self.assertTrue(finding["message"])
            self.assertIs(False, finding["retryable"])
            self.assertEqual("pin_to_full_sha_or_digest", finding["next_action"])
        for error in obj["errors"]:
            self.assertTrue(error["message"])
            self.assertIs(False, error["retryable"])
            self.assertEqual(ERROR_ACTIONS[error["code"]], error["next_action"])
        expected_action = (ERROR_ACTIONS[errors[0][1]] if errors else
                           "pin_to_full_sha_or_digest" if findings else "none")
        self.assertEqual(expected_action, obj["next_action"])
        return obj

    def text_report(self, proc, *, exit_code, annotations):
        self.assertEqual(b"", proc.stderr)
        self.assertEqual(exit_code, proc.returncode)
        output = proc.stdout.decode("utf-8")
        self.assertNotIn("Traceback", output)
        lines = [line for line in output.splitlines() if line.startswith("::error")]
        self.assertEqual(annotations, len(lines), output)
        if not annotations:
            self.assertIn("Action pins OK", output)
        return output

    def supported(self, source, expected):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", source)
            before = frozen.snapshot(tmp)
            structured = invoke(["--json", "wf.yml"], tmp)
            text = invoke(["wf.yml"], tmp)
            self.assertEqual(before, frozen.snapshot(tmp))
            with self.subTest(mode="json"):
                self.report(structured, requested=1, checked=1, refused=0,
                            complete=True, findings=[
                                ("wf.yml", line, value) for line, value in expected])
            with self.subTest(mode="text"):
                output = self.text_report(text, exit_code=1 if expected else 0,
                                          annotations=len(expected))
                for line, _ in expected:
                    self.assertIn(f"::error file=wf.yml,line={line}::", output)

    def refused(self, source, code):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "wf.yml", source)
            before = frozen.snapshot(tmp)
            structured = invoke(["--json", "wf.yml"], tmp)
            text = invoke(["wf.yml"], tmp)
            self.assertEqual(before, frozen.snapshot(tmp))
            with self.subTest(mode="json"):
                self.report(structured, requested=1, checked=0, refused=1,
                            complete=False, errors=[("wf.yml", code)])
            with self.subTest(mode="text"):
                output = self.text_report(text, exit_code=1, annotations=1)
                self.assertIn("::error file=wf.yml", output)

    # A truncated/comment-confused scalar must not turn an invocation clean.
    def test_embedded_hash_is_scalar_content(self):
        self.supported("steps:\n  - uses: actions/checkout@v4#suffix\n",
                       [(2, "actions/checkout@v4#suffix")])
        self.supported("steps:\n  - uses: ./local#suffix\n", [])
        self.supported(f"steps:\n  - uses: actions/checkout@{SHA}#suffix\n",
                       [(2, f"actions/checkout@{SHA}#suffix")])
        self.supported(f"steps:\n  - uses: actions/checkout@{SHA} # comment\n", [])

    def test_multiline_plain_scalar_is_one_value(self):
        self.supported("steps:\n  - uses: actions/checkout@v4\n      suffix\n",
                       [(2, "actions/checkout@v4 suffix")])
        self.supported("steps:\n  - uses: ./local\n      suffix\n", [])

    def test_multiline_quoted_scalars_are_folded(self):
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                self.supported(f"steps:\n  - uses: {quote}actions/checkout@v4\n"
                               f"      suffix{quote}\n",
                               [(2, "actions/checkout@v4 suffix")])
                self.supported(f"steps:\n  - uses: {quote}./local\n"
                               f"      suffix{quote}\n", [])

    def test_escaped_physical_newline_decodes_before_pin_policy(self):
        self.supported('steps:\n  - uses: "actions/checkout@v\\\n      4"\n',
                       [(2, "actions/checkout@v4")])
        self.supported('steps:\n  - uses: "actions/checkout@'
                       + SHA[:20] + '\\\n      ' + SHA[20:] + '"\n', [])

    def test_empty_and_colonless_flow_entries_do_not_hide_uses(self):
        shapes = (
            "steps: [{}, {name: {}, uses: REF}]\n",
            "steps: [{name, uses: REF}]\n",
            "steps: [{name: [], uses: REF}]\n",
        )
        for shape in shapes:
            for ref in ("actions/checkout@v4", "./local", f"actions/checkout@{SHA}"):
                with self.subTest(shape=shape, ref=ref):
                    self.supported(shape.replace("REF", ref),
                                   [(1, ref)] if ref.endswith("@v4") else [])

    def test_shorthand_sequence_mapping_is_an_invocation(self):
        self.supported("steps: [uses: actions/checkout@v4]\n",
                       [(1, "actions/checkout@v4")])
        self.supported("steps: [uses: ./local]\n", [])

    def test_direct_anchored_literal_collections_are_supported(self):
        shapes = (
            ("steps: &steps\n  - &step {uses: REF}\n", 2),
            ("jobs: &jobs\n  build: &job\n    steps: &steps\n"
             "      - &step {uses: REF}\n", 4),
            ("runs: &run\n  steps: &steps\n    - &step\n      uses: &ref REF\n", 4),
        )
        for shape, line in shapes:
            for ref in ("actions/checkout@v4", "./local"):
                with self.subTest(shape=shape, ref=ref):
                    self.supported(shape.replace("REF", ref),
                                   [(line, ref)] if ref.endswith("@v4") else [])

    def test_bom_and_same_line_document_marker_controls(self):
        for shape, line in (("﻿steps:\n  - uses: REF\n", 2),
                            ("--- {steps: [{uses: REF}]}\n", 1)):
            for ref in ("actions/checkout@v4", "./local"):
                with self.subTest(shape=shape, ref=ref):
                    self.supported(shape.replace("REF", ref),
                                   [(line, ref)] if ref.endswith("@v4") else [])

    def test_block_scalar_invocation_values_keep_key_line(self):
        for style in ("|-", ">-"):
            with self.subTest(style=style):
                self.supported(f"steps:\n  - uses: {style}\n      actions/checkout@v4\n",
                               [(2, "actions/checkout@v4")])
                self.supported(f"steps:\n  - uses: {style}\n      ./local\n", [])

    def test_quoted_multiline_name_is_not_a_fake_step(self):
        for ref in ("actions/checkout@v4", "./local"):
            with self.subTest(ref=ref):
                self.supported('steps:\n  - name: "Checkout\n'
                               '      - uses: pretend/action@main"\n'
                               f"    uses: {ref}\n",
                               [(4, ref)] if ref.endswith("@v4") else [])

    def test_multiline_run_data_does_not_hide_following_real_action(self):
        sources = (
            ('steps:\n  - run: "echo hello\n'
             '      uses: pretend/action@main"\n  - uses: REF\n', 4),
            ("steps:\n  - run: 'echo hello\n"
             "      - uses: pretend/action@main'\n  - uses: REF\n", 4),
            ('steps:\n  - run: "echo hel\\\n'
             '      lo"\n  - uses: REF\n', 4),
        )
        for shape, line in sources:
            for ref in ("actions/checkout@v4", "./local"):
                with self.subTest(shape=shape, ref=ref):
                    self.supported(shape.replace("REF", ref),
                                   [(line, ref)] if ref.endswith("@v4") else [])

    # A data key named uses is not an invocation subtree.
    def test_workflow_positions_exclude_root_and_nested_data(self):
        self.supported(
            "uses: pretend/root@main\n"
            "steps: [{uses: pretend/root-step@main}]\n"
            "env: {uses: pretend/env@main}\n"
            "jobs:\n"
            "  call:\n"
            "    uses: octo/repo/.github/workflows/ci.yml@v1\n"
            "    with: {uses: pretend/input@main}\n"
            "  build:\n"
            "    env: {uses: pretend/job-env@main}\n"
            "    metadata: {uses: pretend/job-data@main}\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "        with: {uses: pretend/step-input@main}\n"
            "        env: {uses: pretend/step-env@main}\n"
            "      - run: 'uses: pretend/run@main'\n",
            [(6, "octo/repo/.github/workflows/ci.yml@v1"),
             (12, "actions/checkout@v4")])

    def test_pinned_and_local_workflow_positions_are_clean(self):
        self.supported(
            f"jobs:\n  call:\n    uses: octo/repo/.github/workflows/ci.yml@{SHA}\n"
            "  local:\n    uses: ./.github/workflows/ci.yml\n"
            "  build:\n    steps:\n      - uses: ./local\n"
            f"      - uses: docker://alpine:3@sha256:{DIGEST}\n"
            f"      - uses: actions/checkout@{SHA}\n", [])

    def test_action_positions_without_using_exclude_metadata(self):
        self.supported(
            "uses: pretend/root@main\n"
            "steps: [{uses: pretend/root-step@main}]\n"
            "runs:\n"
            "  env: {uses: pretend/run-env@main}\n"
            "  metadata: {uses: pretend/run-data@main}\n"
            "  steps:\n"
            "    - uses: actions/checkout@v4\n"
            "      with: {uses: pretend/input@main}\n",
            [(7, "actions/checkout@v4")])
        self.supported("runs:\n  steps:\n    - uses: ./local\n", [])

    def test_root_fragment_compatibility_shapes(self):
        cases = (
            ("uses: REF\n", 1),
            ("steps:\n  - uses: REF\n", 2),
            ("steps:\n  uses: REF\n", 2),
            ("- uses: REF\n", 1),
            ("branches:\n  - \nuses: REF\n", 3),
        )
        for shape, line in cases:
            for ref in ("actions/checkout@v4", "./local"):
                with self.subTest(shape=shape, ref=ref):
                    self.supported(shape.replace("REF", ref),
                                   [(line, ref)] if ref.endswith("@v4") else [])

    def test_root_metadata_env_and_with_are_data(self):
        self.supported(
            "metadata: {uses: pretend/meta@main, steps: [{uses: pretend/nested@main}]}\n"
            "env: {uses: pretend/env@main}\n"
            "with: {uses: pretend/input@main}\nuses: ./local\n", [])

    def test_duplicate_uses_pairs_preserve_every_occurrence_and_sort(self):
        self.supported(
            "steps:\n  - {uses: z/action@main, uses: ./local, uses: a/action@v1}\n"
            "  - uses: b/action@v2\n    uses: c/action@v3\n",
            [(2, "a/action@v1"), (2, "z/action@main"),
             (3, "b/action@v2"), (4, "c/action@v3")])

    def test_duplicate_context_pairs_do_not_hide_invocations(self):
        self.supported(
            "jobs:\n  build:\n    steps: [{uses: a/action@v1}]\n"
            "    steps: [{uses: b/action@v2}]\n"
            "  build:\n    uses: c/repo/.github/workflows/ci.yml@main\n"
            "jobs:\n  build:\n    steps: [{uses: d/action@v3}]\n",
            [(3, "a/action@v1"), (4, "b/action@v2"),
             (6, "c/repo/.github/workflows/ci.yml@main"), (9, "d/action@v3")])

    def test_all_yaml_documents_are_inspected_with_absolute_key_lines(self):
        self.supported(
            "---\nuses: ./local\n...\n"
            "---\nruns:\n  steps:\n    - uses: actions/checkout@v4\n"
            "---\njobs:\n  call:\n    uses: octo/repo/.github/workflows/ci.yml@main\n",
            [(7, "actions/checkout@v4"),
             (11, "octo/repo/.github/workflows/ci.yml@main")])

    def test_empty_missing_and_explicit_string_values_keep_frozen_semantics(self):
        self.supported("steps:\n  - uses:\n  - uses: ''\n  - uses: !!str 42\n",
                       [(2, ""), (3, ""), (4, "42")])

    def test_supported_public_helper_still_returns_finding_objects(self):
        import action_pins as ap
        findings = ap.lint_text("wf.yml", "steps: [uses: actions/checkout@v4]\n")
        self.assertIsInstance(findings, list)
        self.assertEqual([("wf.yml", 1, "actions/checkout@v4")],
                         [(f.file, f.line, f.value) for f in findings])
        self.assertEqual([], ap.lint_text("wf.yml", "steps: [uses: ./local]\n"))

    # Unsupported nodes must refuse the whole file, not masquerade as clean.
    def test_original_unquoted_run_bytes_are_invalid_yaml(self):
        self.refused("steps:\n  - run: echo uses: foo\n", "input-invalid-yaml")

    def test_valid_quoted_run_control_has_no_action(self):
        self.supported('steps:\n  - run: "echo uses: foo"\n', [])

    def test_invalid_yaml_refuses_even_when_earlier_action_is_valid(self):
        for source in ("steps: [\n", "uses: ./local\nmetadata: [\n",
                       "uses: actions/checkout@v4\n---\nsteps: [\n"):
            with self.subTest(source=source):
                self.refused(source, "input-invalid-yaml")

    def test_aliases_and_merge_keys_are_unsupported(self):
        for source in (
            "step: &step {uses: ./local}\nsteps: [*step]\n",
            "metadata: &data [one]\nother: *data\nuses: ./local\n",
            "loop: &loop [*loop]\nuses: ./local\n",
            "base: &base {uses: ./local}\nsteps: [{<<: *base}]\n",
            "steps: [{<<: {uses: ./local}}]\n",
        ):
            with self.subTest(source=source):
                self.refused(source, "input-unsupported-yaml")

    def test_nonstandard_tags_do_not_construct_objects_or_write_files(self):
        for source in (
            "metadata: !example value\nuses: ./local\n",
            "metadata: !!python/object/apply:builtins.open [constructed.txt, w]\n"
            "uses: ./local\n",
        ):
            with self.subTest(source=source):
                self.refused(source, "input-unsupported-yaml")

    def test_standard_scalar_tags_in_ordinary_data_are_supported(self):
        self.supported(
            "metadata:\n  bool: !!bool true\n  int: !!int 42\n"
            "  float: !!float 1.5\n  null: !!null null\n"
            "  time: !!timestamp 2026-01-01\n  bytes: !!binary YQ==\n"
            "uses: !!str ./local\n", [])

    def test_complex_mapping_keys_are_unsupported(self):
        self.refused("? [uses, other]\n: ./local\n", "input-unsupported-yaml")
        self.refused("metadata:\n  ? {a: b}\n  : value\nuses: ./local\n",
                     "input-unsupported-yaml")

    def test_collection_and_nonstring_invocation_values_are_unsupported(self):
        for value in ("[]", "{}", "[./local]", "{path: ./local}",
                      "true", "42", "1.5", "null", "!!int 42"):
            with self.subTest(value=value):
                self.refused(f"steps:\n  - uses: {value}\n", "input-unsupported-yaml")

    def test_nonstring_invocation_key_is_unsupported(self):
        self.refused("steps:\n  - !!int uses: ./local\n", "input-unsupported-yaml")

    def test_unsupported_invocation_collection_positions_are_not_skipped(self):
        sources = (
            "jobs: [{uses: ./local}]\n",
            "jobs:\n  build: [{uses: ./local}]\n",
            "jobs:\n  build:\n    steps: {uses: ./local}\n",
            "runs:\n  steps: {uses: ./local}\n",
            "steps: [[{uses: ./local}]]\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.refused(source, "input-unsupported-yaml")

    def test_error_sorting_precedence_and_other_files_findings_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "z-bad.yml", "steps: [\n")
            write(tmp, "b-unsupported.yml", "steps: [{uses: []}]\n")
            write(tmp, "d-encoding.yml", b"uses: \xff\n")
            write(tmp, "f-permissions.yml", CLEAN).chmod(0)
            write(tmp, "c-finding.yml", BAD)
            write(tmp, "e-clean.yml", CLEAN)
            args = ["z-bad.yml", "b-unsupported.yml", "a-missing.yml",
                    "d-encoding.yml", "f-permissions.yml", "c-finding.yml",
                    "e-clean.yml", "e-clean.yml"]
            run = lambda arguments: invoke(arguments, tmp)
            if frozen._is_root():
                # Same real uid-drop boundary as the frozen permission tests.
                os.chmod(tmp, 0o755)
                checker = Path(tmp, "checker.py")
                shutil.copyfile(SCRIPT, checker)
                checker.chmod(0o644)
                run = lambda arguments: frozen.run_cli_as_nobody(checker, arguments, tmp)
            try:
                first = run(["--json", *args])
                second = run(["--json", *reversed(args)])
                text = run(args)
            finally:
                Path(tmp, "f-permissions.yml").chmod(0o600)
                if frozen._is_root():
                    os.chmod(tmp, 0o700)
            self.assertEqual(first.stdout, second.stdout)
            self.report(first, requested=7, checked=2, refused=5, complete=False,
                        findings=[("c-finding.yml", 1, "actions/checkout@v4")],
                        errors=[("a-missing.yml", "input-missing"),
                                ("b-unsupported.yml", "input-unsupported-yaml"),
                                ("d-encoding.yml", "input-invalid-encoding"),
                                ("f-permissions.yml", "input-unreadable"),
                                ("z-bad.yml", "input-invalid-yaml")])
            self.text_report(text, exit_code=1, annotations=6)

    # -S removes site-packages without changing sys.executable or PYTHONPATH.
    def test_missing_parser_is_structured_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, source in (("a.yml", CLEAN), ("b.yml", BAD)):
                write(tmp, name, source)
            proc = invoke(["--json", "b.yml", "a.yml"], tmp, flags=("-S",))
            text = invoke(["b.yml", "a.yml"], tmp, flags=("-S",))
            self.report(proc, requested=2, checked=0, refused=2, complete=False,
                        errors=[("a.yml", "input-parser-unavailable"),
                                ("b.yml", "input-parser-unavailable")])
            self.text_report(text, exit_code=1, annotations=2)

    def test_mismatched_parser_version_is_structured(self):
        import yaml
        self.assertEqual("6.0.2", yaml.__version__)
        with tempfile.TemporaryDirectory() as tmp:
            # Real dependency code in scratch, changing only its version label.
            # -S ensures no global/user package or real verdict can hide this.
            package = Path(tmp, "yaml")
            shutil.copytree(Path(yaml.__file__).parent, package,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            init = package / "__init__.py"
            original = init.read_text(encoding="utf-8")
            changed, count = re.subn(r"(?m)^__version__ = ['\"]6\.0\.2['\"]$",
                                     "__version__ = '6.0.3'", original)
            self.assertEqual(1, count)
            init.write_text(changed, encoding="utf-8")
            checker = Path(tmp, "action_pins.py")
            shutil.copyfile(SCRIPT, checker)
            write(tmp, "wf.yml", CLEAN)
            proc = invoke(["--json", "wf.yml"], tmp, flags=("-S",), script=checker)
            text = invoke(["wf.yml"], tmp, flags=("-S",), script=checker)
            self.report(proc, requested=1, checked=0, refused=1, complete=False,
                        errors=[("wf.yml", "input-parser-unavailable")])
            self.text_report(text, exit_code=1, annotations=1)

    def test_parser_is_visible_with_pythonpath_scrubbed(self):
        import yaml
        self.assertEqual("6.0.2", yaml.__version__)
        self.assertNotIn("PYTHONPATH", frozen.clean_env())
        self.supported("steps: [uses: ./local]\n", [])

    def test_missing_parser_does_not_replace_file_prechecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "b-encoding.yml", b"uses: \xff\n")
            Path(tmp, "c-directory.yml").mkdir()
            os.mkfifo(Path(tmp, "d-fifo.yml"))
            args = ["d-fifo.yml", "c-directory.yml", "b-encoding.yml", "a-missing.yml"]
            proc = invoke(["--json", *args], tmp, flags=("-S",))
            text = invoke(args, tmp, flags=("-S",))
            self.report(proc, requested=4, checked=0, refused=4, complete=False,
                        errors=[("a-missing.yml", "input-missing"),
                                ("b-encoding.yml", "input-invalid-encoding"),
                                ("c-directory.yml", "input-unreadable"),
                                ("d-fifo.yml", "input-unreadable")])
            self.text_report(text, exit_code=1, annotations=4)

    def test_help_and_no_input_discovery_work_without_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            help_proc = invoke(["--json", "--help"], tmp, flags=("-S",))
            self.assertEqual(0, help_proc.returncode)
            self.assertEqual(b"", help_proc.stderr)
            self.assertIn("--json", help_proc.stdout.decode("utf-8"))
            self.report(invoke(["--json"], tmp, flags=("-S",)),
                        requested=0, checked=0, refused=0, complete=False,
                        errors=[(None, "no-inputs-discovered")])

    def test_discovery_without_parser_preserves_encoding_and_missing_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/a.yml", CLEAN)
            write(tmp, ".github/workflows/b.yml", b"uses: \xff\n")
            proc = invoke(["--json"], tmp, flags=("-S",))
            self.report(proc, requested=2, checked=0, refused=2, complete=False,
                        errors=[(".github/workflows/a.yml", "input-parser-unavailable"),
                                (".github/workflows/b.yml", "input-invalid-encoding")])

    # Inclusive ceilings; ordinary metadata is not a recursive action search.
    def test_source_byte_limit_boundary_is_inclusive(self):
        prefix = CLEAN.encode("utf-8") + b"#"
        source = prefix + b"x" * (4_194_304 - len(prefix))
        self.supported(source, [])

    def test_source_byte_limit_plus_one_is_controlled_refusal(self):
        prefix = CLEAN.encode("utf-8") + b"#"
        source = prefix + b"x" * (4_194_305 - len(prefix))
        self.refused(source, "input-resource-limit")

    def test_open_collection_limit_boundary_is_inclusive(self):
        # One root mapping plus 127 nested sequences = 128 open collections.
        self.supported("metadata: " + "[" * 127 + "0" + "]" * 127 + "\n" + CLEAN, [])

    def test_open_collection_limit_plus_one_is_controlled_refusal(self):
        self.refused("metadata: " + "[" * 128 + "0" + "]" * 128 + "\n" + CLEAN,
                     "input-resource-limit")

    def test_deep_flow_fails_closed_before_python_recursion_loss(self):
        self.refused("metadata: " + "[" * 1_050 + "0" + "]" * 1_050 + "\n" + CLEAN,
                     "input-resource-limit")

    def test_parser_event_limit_boundary_is_inclusive(self):
        # Stream/doc start/end (4), map start/end (2), sequence start/end (2),
        # metadata key, uses key, uses value (3): n scalar items + 11 events.
        source = "metadata: [" + ",".join(["0"] * 99_989) + "]\n" + CLEAN
        self.supported(source, [])

    def test_parser_event_limit_plus_one_is_controlled_refusal(self):
        source = "metadata: [" + ",".join(["0"] * 99_990) + "]\n" + CLEAN
        self.refused(source, "input-resource-limit")

    def test_parser_event_limit_is_per_file_not_per_document(self):
        # Two such documents have n1+n2+20 events including the shared stream.
        first = "---\nmetadata: [" + ",".join(["0"] * 49_990) + "]\n" + CLEAN
        second = "---\nmetadata: [" + ",".join(["0"] * 49_990) + "]\n" + CLEAN
        self.supported(first + second, [])  # Exactly 100,000 events.
        self.refused(first + second.replace("[0,", "[0,0,", 1),
                     "input-resource-limit")

    def test_resource_refusal_does_not_lose_other_files_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "a-limit.yml", "metadata: " + "[" * 128 + "0" + "]" * 128 + "\n")
            write(tmp, "b-finding.yml", BAD)
            self.report(invoke(["--json", "b-finding.yml", "a-limit.yml"], tmp),
                        requested=2, checked=1, refused=1, complete=False,
                        findings=[("b-finding.yml", 1, "actions/checkout@v4")],
                        errors=[("a-limit.yml", "input-resource-limit")])

    def test_deep_directory_discovery_is_stack_safe_and_observable(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/control.yml", CLEAN)
            with deep_action_tree(tmp, 1_050) as path:
                structured = invoke(["--json"], tmp)
                text = invoke([], tmp)
                self.report(structured, requested=2, checked=2, refused=0,
                            complete=True,
                            findings=[(path, 3, "actions/checkout@v4")])
                output = self.text_report(text, exit_code=1, annotations=1)
                self.assertIn(f"::error file={path},line=3::", output)

    def test_directory_symlink_cycle_does_not_duplicate_or_hide_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ".github/actions/pkg/action.yml"
            write(tmp, path, "runs:\n  steps:\n    - uses: actions/checkout@v4\n")
            os.symlink(".", Path(tmp, ".github/actions/cycle"), target_is_directory=True)
            self.report(invoke(["--json"], tmp), requested=1, checked=1, refused=0,
                        complete=True, findings=[(path, 3, "actions/checkout@v4")])

    # Transport must not use the consumer locale to repair producer identity.
    def test_valid_utf8_filename_identity_in_ascii_locale(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                name = "café%slot,name:part.yml"
                prop = "café%25slot%2Cname%3Apart.yml"
                if not missing:
                    write(tmp, name, BAD)
                structured = invoke(["--json", name], tmp, extra_env=ASCII_ENV)
                text = invoke([name], tmp, extra_env=ASCII_ENV)
                obj = self.report(
                    structured, requested=1, checked=0 if missing else 1,
                    refused=1 if missing else 0, complete=not missing,
                    findings=[] if missing else [(name, 1, "actions/checkout@v4")],
                    errors=[(name, "input-missing")] if missing else [])
                entry = (obj["errors"] if missing else obj["findings"])[0]
                self.assertEqual(name, entry["path"])
                output = self.text_report(text, exit_code=1, annotations=1)
                self.assertIn(f"::error file={prop},line=1::", output)
                self.assertNotIn("�", output)

    def test_genuinely_non_utf8_filename_has_reversible_escaped_identity(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                raw = b"byte-\xff.yml"
                if not missing:
                    with open(os.fsencode(tmp) + b"/" + raw, "wb") as stream:
                        stream.write(BAD.encode("utf-8"))
                arg = os.fsdecode(raw)
                structured = invoke(["--json", arg], tmp, extra_env=ASCII_ENV)
                text = invoke([arg], tmp, extra_env=ASCII_ENV)
                self.assertEqual(b"", structured.stderr)
                obj = json.loads(structured.stdout.decode("utf-8"))
                entries = obj["errors"] if missing else obj["findings"]
                self.assertEqual(1, len(entries))
                # The contract allows surrogateescape in JSON or either explicit
                # reversible byte/surrogate escape spelling; never replacement.
                rendered = entries[0]["path"]
                self.assertIn(rendered, ("byte-\udcff.yml", "byte-\\xff.yml",
                                         "byte-\\udcff.yml"))
                self.report(structured, requested=1, checked=0 if missing else 1,
                            refused=1 if missing else 0, complete=not missing,
                            findings=[] if missing else [(rendered, 1, "actions/checkout@v4")],
                            errors=[(rendered, "input-missing")] if missing else [])
                output = self.text_report(text, exit_code=1, annotations=1)
                self.assertTrue(any(f"::error file={name}" in output for name in
                                    ("byte-\\xff.yml", "byte-\\udcff.yml")), output)
                self.assertNotIn("�", output)
                self.assertEqual([] if missing else [raw], os.listdir(os.fsencode(tmp)))

    def test_both_output_modes_emit_utf8_despite_utf16_io_setting(self):
        cases = (("clean", "uses: ./café\n", [], []),
                 ("finding", "uses: actions/checkout@café\n",
                  [("café.yml", 1, "actions/checkout@café")], []),
                 ("refusal", "uses: [\n", [], [("café.yml", "input-invalid-yaml")]))
        env = {**ASCII_ENV, "PYTHONIOENCODING": "utf-16"}
        for label, source, findings, errors in cases:
            with self.subTest(outcome=label), tempfile.TemporaryDirectory() as tmp:
                write(tmp, "café.yml", source)
                before = frozen.snapshot(tmp)
                structured = invoke(["--json", "café.yml"], tmp, extra_env=env)
                text = invoke(["café.yml"], tmp, extra_env=env)
                self.assertEqual(before, frozen.snapshot(tmp))
                with self.subTest(mode="json"):
                    self.report(structured, requested=1, checked=0 if errors else 1,
                                refused=1 if errors else 0, complete=not errors,
                                findings=findings, errors=errors)
                    self.assertNotIn(b"\x00", structured.stdout)
                    self.assertTrue(structured.stdout.startswith(b"{"))
                with self.subTest(mode="text"):
                    output = self.text_report(text, exit_code=0 if label == "clean" else 1,
                                              annotations=0 if label == "clean" else 1)
                    self.assertNotIn(b"\x00", text.stdout)
                    if label != "clean":
                        self.assertIn("::error file=café.yml", output)


if __name__ == "__main__":
    unittest.main()
