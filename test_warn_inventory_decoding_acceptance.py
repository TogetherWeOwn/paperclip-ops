#!/usr/bin/env python3
"""Immutable acceptance handoff for conservative inventory source decoding.

Contract selected for this slice: strict UTF-8 for Python, shell and workflow
source. Encoding cookies do not opt into legacy codecs; no guessing, replacement
or ignore fallback. Valid Unicode, including literal U+FFFD, remains source.
An undecodable in-scope file contributes only its skipped path, never hits.
Any skip makes sibling counts partial evidence: sorted skips, no HARD-flip
recommendations, and exit 2 in text and JSON. Existing scope and report shape
stay unchanged. Error reports must not disclose rejected source or exceptions.

Run offline from the repository root:
    python3 -B -m unittest -v test_warn_first_inventory test_warn_inventory_decoding_acceptance

Fixtures are disposable tracked repositories; scanned commands are never run.
Only the permission-denied control mocks an OS read (chmod is unreliable as
root). All scanner, recommendation, reporting and CLI behavior remains real.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().with_name("warn_first_inventory.py")
spec = importlib.util.spec_from_file_location("warn_decoding_subject", MODULE_PATH)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)

PY_HIT = b'import subprocess\nsubprocess.run(["echo", "fixture"])\n'
SH_HIT = b'#!/usr/bin/env bash\nvalue=$(collect 2>&1)\n'
WORKFLOW_HIT = (
    b'name: fixture\non: pull_request\njobs:\n  build:\n'
    b'    runs-on: ubuntu-latest\n    steps:\n'
    b'      - if: always()\n        uses: actions/upload-artifact@v4\n'
    b'        with:\n          name: debug\n          path: debug/\n'
)
TIMEOUT_RULES = ("py-subprocess-timeout", "semgrep-python-subprocess-no-timeout")
REPORT_KEYS = {
    "root", "by_rule", "total", "unique_locations", "skipped",
    "out_of_gate_scope", "recommend",
}
REJECTED_MARKER = "REJECTED_SOURCE_SENTINEL"
EXCEPTION_MARKER = "PRIVATE_EXCEPTION_SENTINEL"


class DecodingAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # Keep fixture git operations independent of the invoking worktree/hook.
        env = dict(os.environ)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(key, None)
        env["GIT_CEILING_DIRECTORIES"] = str(self.root.parent)
        self.env = env
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.git("-c", "init.templateDir=", "init", "-q")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, env=self.env,
            check=True, capture_output=True, timeout=15,
        )

    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def track(self):
        self.git("add", "-A")

    def report(self, include_hidden=False):
        counts = inventory.scan_tree(self.root, include_hidden=include_hidden)
        return inventory.build_report(
            self.root, counts, include_hidden=include_hidden,
        )

    def cli(self, output_format, include_hidden=False):
        args = [
            sys.executable, "-B", str(MODULE_PATH), "--root", str(self.root),
            "--format", output_format,
        ]
        if include_hidden:
            args.append("--include-hidden")
        return subprocess.run(
            args, cwd=self.root, env=self.env, capture_output=True,
            text=True, encoding="utf-8", errors="strict", timeout=30,
        )

    def assert_incomplete(self, report, paths):
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["skipped"], sorted(paths))
        self.assertEqual(report["recommend"], [])
        rendered = inventory.format_text(report)
        self.assertIn("incomplete scan", rendered)
        self.assertNotIn("flips next (zero hits)", rendered)

    def assert_sibling_hits(self, report):
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["unique_locations"], 2)
        for rule in TIMEOUT_RULES:
            row = report["by_rule"][rule]
            self.assertEqual(row["count"], 1)
            self.assertEqual(
                [(hit["file"], hit["line"]) for hit in row["hits"]],
                [("prod/good.py", 2)],
            )
        row = report["by_rule"]["bash-json-stderr-merge"]
        self.assertEqual(row["count"], 1)
        self.assertEqual(
            [(hit["file"], hit["line"]) for hit in row["hits"]],
            [("prod/good.sh", 2)],
        )
        self.assertEqual(report["by_rule"]["py-subprocess-timeout"]["origin"], "ast-grep")
        self.assertEqual(
            report["by_rule"][TIMEOUT_RULES[1]]["origin"],
            "semgrep (intended semantics, see header)",
        )

    def mixed_tree(self):
        # Deliberately not lexical creation order. Rejected source contains real
        # rule tokens, so lossy decoding cannot masquerade as partial evidence.
        self.write(
            "prod/z_bad.sh",
            b"#!/usr/bin/env bash\nvalue=$(collect " + REJECTED_MARKER.encode()
            + b" 2>&1) # \xff\n",
        )
        self.write("prod/good.py", PY_HIT)
        self.write(
            "prod/a_bad.py",
            b'import subprocess\nsubprocess.run(["echo", "'
            + REJECTED_MARKER.encode() + b'"]) # \xc3(\n',
        )
        self.write("prod/good.sh", SH_HIT)
        self.track()

    def test_valid_unicode_and_literal_replacement_character_are_not_skips(self):
        cases = (
            ("prod/café.py", PY_HIT, "py-subprocess-timeout"),
            ("prod/check.sh", SH_HIT, "bash-json-stderr-merge"),
            (".github/workflows/check.yml", WORKFLOW_HIT, "gha-artifact-always-unscrubbed"),
        )
        for rel, source, _rule in cases:
            self.write(rel, source + "# café 雪 \U0001f680 �\n".encode("utf-8"))
        self.track()
        report = self.report(include_hidden=True)
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["unique_locations"], 3)
        for rel, _source, rule in cases:
            self.assertEqual(report["by_rule"][rule]["count"], 1)
            self.assertEqual(report["by_rule"][rule]["hits"][0]["file"], rel)
        self.assertNotIn("gha-artifact-always-unscrubbed", report["recommend"])
        self.assertIn("gha-artifact-always-unscrubbed", report["out_of_gate_scope"])

    def test_clean_utf8_still_produces_normal_zero_hit_recommendations(self):
        self.write("prod/clean.py", 'label = "café 雪 �"\n'.encode("utf-8"))
        self.write("prod/clean.sh", '#!/bin/sh\nprintf "%s" "雪 �"\n'.encode("utf-8"))
        self.track()
        report = self.report()
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["unique_locations"], 0)
        self.assertTrue(report["recommend"])
        self.assertTrue(all(report["by_rule"][rule]["count"] == 0 for rule in report["recommend"]))

    def test_undecodable_python_shell_and_workflow_are_skipped_whole(self):
        for rel, source in (
            ("prod/bad.py", PY_HIT),
            ("prod/bad.sh", SH_HIT),
            (".github/workflows/bad.yml", WORKFLOW_HIT),
            (".github/workflows/bad.yaml", WORKFLOW_HIT),
        ):
            self.write(rel, source + b"# \xff rejected\n")
        self.track()
        report = self.report(include_hidden=True)
        self.assert_incomplete(report, [
            "prod/bad.py", "prod/bad.sh",
            ".github/workflows/bad.yml", ".github/workflows/bad.yaml",
        ])
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["unique_locations"], 0)

    def test_malformed_utf8_is_not_replaced_ignored_or_surrogate_decoded(self):
        cases = (b"\xff", b"\xc3(", b"\xe2\x82", b"\xed\xa0\x80", b"\xf4\x90\x80\x80")
        for index, invalid in enumerate(cases):
            self.write(f"prod/bad_{index}.py", PY_HIT + b"# " + invalid + b"\n")
        # An invalid byte inside a rule token must not silently hide that token.
        self.write("prod/broken_token.sh", b"cu\xffrl -s https://example.invalid/tool | bash\n")
        self.track()
        report = self.report()
        self.assert_incomplete(report, [
            *(f"prod/bad_{index}.py" for index in range(5)), "prod/broken_token.sh",
        ])
        self.assertEqual(report["total"], 0)

    def test_plain_directory_walk_also_records_undecodable_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "bad.py").write_bytes(PY_HIT + b"# \xff\n")
            (root / "good.py").write_bytes(PY_HIT)
            counts = inventory.scan_tree(root)
            report = inventory.build_report(root, counts)
            self.assert_incomplete(report, ["bad.py"])
            self.assertEqual(report["total"], 2)
            self.assertEqual(report["unique_locations"], 1)
            for rule in TIMEOUT_RULES:
                self.assertEqual(report["by_rule"][rule]["hits"][0]["file"], "good.py")

    def test_invalid_byte_beyond_binary_probe_is_still_a_gap(self):
        self.write("prod/tail.py", PY_HIT + b"#" + b"a" * 16384 + b"\xff\n")
        self.track()
        report = self.report()
        self.assert_incomplete(report, ["prod/tail.py"])
        self.assertEqual(report["total"], 0)

    def test_mixed_sources_keep_only_truthful_sibling_counts_and_sorted_skips(self):
        self.mixed_tree()
        report = self.report()
        self.assert_incomplete(report, ["prod/a_bad.py", "prod/z_bad.sh"])
        self.assert_sibling_hits(report)

    def test_json_cli_returns_two_with_partial_sibling_evidence(self):
        self.mixed_tree()
        proc = self.cli("json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "")
        report = json.loads(proc.stdout)
        self.assert_incomplete(report, ["prod/a_bad.py", "prod/z_bad.sh"])
        self.assert_sibling_hits(report)
        self.assertNotIn(REJECTED_MARKER, proc.stdout)

    def test_text_cli_returns_two_and_never_offers_hard_flips_on_decode_gap(self):
        self.mixed_tree()
        proc = self.cli("text")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("incomplete scan", proc.stdout)
        self.assertIn("prod/a_bad.py", proc.stdout)
        self.assertIn("prod/z_bad.sh", proc.stdout)
        self.assertNotIn("flips next (zero hits)", proc.stdout)
        self.assertNotIn(REJECTED_MARKER, proc.stdout)

    def test_complete_cli_stays_zero_in_both_formats(self):
        self.write("prod/good.py", PY_HIT)
        self.write("prod/good.sh", SH_HIT)
        self.track()
        for output_format in ("text", "json"):
            with self.subTest(format=output_format):
                proc = self.cli(output_format)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                if output_format == "json":
                    report = json.loads(proc.stdout)
                    self.assertEqual(report["skipped"], [])
                    self.assert_sibling_hits(report)
                else:
                    self.assertNotIn("incomplete scan", proc.stdout)
                    self.assertIn("py-subprocess-timeout", proc.stdout)

    def test_excluded_invalid_sources_do_not_poison_default_scope(self):
        self.write("prod/good.py", PY_HIT)
        for rel in (
            "tests/bad.py", "test_bad.py", ".hidden/bad.sh",
            ".github/workflows/bad.yml", "notes.txt",
        ):
            self.write(rel, b"\xff rejected\n")
        self.track()
        self.write("prod/untracked.py", b"\xff rejected\n")
        report = self.report()
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["unique_locations"], 1)
        self.assertTrue(report["recommend"])

    def test_include_hidden_makes_workflow_decoding_failure_visible(self):
        self.write("prod/good.py", PY_HIT)
        self.write(".github/workflows/bad.yml", WORKFLOW_HIT + b"#\xff\n")
        self.track()
        report = self.report(include_hidden=True)
        self.assert_incomplete(report, [".github/workflows/bad.yml"])
        self.assertEqual(report["total"], 2)
        proc = self.cli("json", include_hidden=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), report)

    def test_explicit_utf8_cookie_accepts_real_utf8(self):
        self.write("prod/declared.py", b"# coding: utf-8\n" + PY_HIT + "# 雪 �\n".encode("utf-8"))
        self.track()
        report = self.report()
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["by_rule"][TIMEOUT_RULES[0]]["hits"][0]["line"], 3)

    def test_cookie_never_enables_legacy_or_unknown_codec_fallback(self):
        for name, header in (
            ("legacy", b"# coding: latin-1\n"),
            ("unknown", b"# coding: no-such-codec\n"),
            ("false_utf8", b"# coding: utf-8\n"),
        ):
            self.write(f"prod/{name}.py", header + PY_HIT + b"# caf\xe9\n")
        self.track()
        report = self.report()
        self.assert_incomplete(report, [
            "prod/legacy.py", "prod/unknown.py", "prod/false_utf8.py",
        ])
        self.assertEqual(report["total"], 0)

    def test_binary_and_utf16_sources_keep_existing_incomplete_behavior(self):
        self.write("prod/nul.py", PY_HIT + b"\x00binary\n")
        self.write("prod/utf16.sh", SH_HIT.decode("ascii").encode("utf-16"))
        self.track()
        report = self.report()
        self.assert_incomplete(report, ["prod/nul.py", "prod/utf16.sh"])
        self.assertEqual(report["total"], 0)
        for output_format in ("text", "json"):
            with self.subTest(format=output_format):
                self.assertEqual(self.cli(output_format).returncode, 2)

    def test_unreadable_path_is_safe_and_does_not_erase_sibling_hits(self):
        denied = self.write("prod/denied.py", PY_HIT)
        self.write("prod/good.py", PY_HIT)
        self.write("prod/good.sh", SH_HIT)
        self.track()
        original = Path.read_bytes

        def read_bytes(path, *args, **kwargs):
            if path == denied:
                raise PermissionError(13, EXCEPTION_MARKER, str(denied))
            return original(path, *args, **kwargs)

        # Real main, scan, report and recommendation paths; only OS failure is
        # injected. Arbitrary exception messages are not safe diagnostics.
        with mock.patch.object(Path, "read_bytes", read_bytes):
            for output_format in ("text", "json"):
                with self.subTest(format=output_format):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = inventory.main([
                            "--root", str(self.root), "--format", output_format,
                        ])
                    self.assertEqual(code, 2)
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertNotIn(EXCEPTION_MARKER, stdout.getvalue())
                    if output_format == "json":
                        report = json.loads(stdout.getvalue())
                        self.assertTrue(any("prod/denied.py" in entry for entry in report["skipped"]))
                        self.assertEqual(report["recommend"], [])
                        self.assert_sibling_hits(report)
                    else:
                        self.assertIn("incomplete scan", stdout.getvalue())
                        self.assertNotIn("flips next (zero hits)", stdout.getvalue())

    def test_decode_diagnostics_do_not_echo_rejected_bytes_or_source(self):
        self.mixed_tree()
        # Safe diagnostics must remain safe even if a decoded sibling has hits.
        for output_format in ("text", "json"):
            with self.subTest(format=output_format):
                proc = self.cli(output_format)
                output = proc.stdout + proc.stderr
                self.assertNotIn(REJECTED_MARKER, output)
                self.assertNotIn("UnicodeDecodeError", output)
                self.assertNotIn("Traceback", output)
                self.assertNotIn("\\xff", output)
                self.assertNotIn("\\xc3", output)

    def test_repeated_scan_is_deterministic_and_read_only_even_when_incomplete(self):
        self.mixed_tree()

        def snapshot():
            return {
                str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()
            }

        before = snapshot()
        first = self.cli("json")
        second = self.cli("json")
        self.assertEqual(snapshot(), before)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stderr, second.stderr)
        self.assertEqual((first.returncode, second.returncode), (2, 2))
        self.assertEqual(json.loads(first.stdout)["recommend"], [])


if __name__ == "__main__":
    unittest.main()
