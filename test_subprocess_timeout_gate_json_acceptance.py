"""Acceptance contract for the subprocess-timeout gate's ``--json`` mode.

These tests freeze the machine-readable contract of
``scripts/quality-gates/subprocess_timeout_gate.py --json`` before it is
implemented. They exercise only the external CLI, driving the real
command-line program with a stub ``--ast-grep`` executable (or an absent
path), so the implementation may be structured any way that satisfies
them. No network, no real ast-grep, no writes outside a temp directory.

Frozen contract
===============

``--json`` is opt-in and only switches output rendering; scanning,
backlog comparison, backlog format, ``--ast-grep`` behavior and exits
are unchanged. In compare mode with ``--json``, the gate prints exactly
one JSON object to stdout (nothing else on stdout, nothing on stderr)
with exactly these keys:

- ``schema``: the string ``"subprocess-timeout-verdict.v1"``
- ``outcome``: ``"clean"`` (scan completed, no new/grown paths versus
  the backlog), ``"findings"`` (scan completed, at least one new/grown
  path) or ``"unmeasurable"`` (no scan result: ast-grep missing, the
  scan failed, or its output was unparseable). Unmeasurable is never
  rendered as clean.
- ``counts``: an object mapping each counted path to its current
  finding count when the scan completed (``{}`` when nothing counted);
  ``null`` when unmeasurable (unknown counts are never published as
  invented zeroes).
- ``new_or_grown``: a list, sorted by ``path``, of objects with
  exactly the keys ``path``, ``before``, ``after``, ``next_action``.
  ``before`` is the backlog count for the path (``0`` when the path is
  absent from the backlog, i.e. a new file), ``after`` is the current
  count, and ``before < after`` always (shrunk or unchanged paths
  never appear). Empty when clean and when unmeasurable.
  ``next_action`` is one bounded single-line string, frozen below.
- ``scan``: an object with exactly the keys ``ast_grep_available``
  (``true`` iff the scan produced a result, ``false`` iff
  unmeasurable) and ``backlog_path`` (the backlog path in effect, as a
  string).
- ``totals``: an object with exactly the keys ``files_with_findings``
  (number of counted paths), ``findings`` (sum of current counts) and
  ``new_or_grown`` (number of new/grown entries); all three integers
  when the scan completed, all three ``null`` when unmeasurable.

A missing backlog file is not unmeasurable: as today it reads as an
empty backlog, so every counted path is a new file (``before`` 0).

Frozen ``next_action`` strings:

- new file (``before`` is 0): ``NEW_ACTION`` below
- grown file (``before`` > 0): ``GROWN_ACTION`` below

No content echo: the JSON carries only paths and counts. Matched
source text, ast-grep stderr and backlog comments never appear in
JSON-mode output (stdout or stderr).

Exits are identical to prose mode for the same inputs: compare mode is
warn-only, so clean, findings and unmeasurable all exit 0 in both
modes. ``--write-backlog --json`` is refused as a usage error (exit 2,
usage text on stderr naming both flags, no JSON on stdout, backlog
file untouched). ``--help`` always prints text usage naming ``--json``
and exits 0; other usage errors print text and exit 2, never JSON.
Default prose output (headline, ``::warning::`` lines, write-backlog
behavior) is byte-identical to today. JSON output is deterministic:
the same inputs yield byte-identical stdout across runs, and a JSON
run modifies no files.
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "quality-gates" / "subprocess_timeout_gate.py"
SCHEMA = "subprocess-timeout-verdict.v1"
RULE = "py-subprocess-timeout"
TIMEOUT = 30

TOP_KEYS = {"schema", "outcome", "counts", "new_or_grown", "scan", "totals"}
ENTRY_KEYS = {"path", "before", "after", "next_action"}
SCAN_KEYS = {"ast_grep_available", "backlog_path"}
TOTAL_KEYS = {"files_with_findings", "findings", "new_or_grown"}

NEW_ACTION = ("add timeout= to the subprocess call(s) in this file, "
              "or add the file to the backlog in a reviewed change")
GROWN_ACTION = ("add timeout= to the added subprocess call(s) in this "
                "file, or raise its backlog count in a reviewed change")

HEADLINE = re.compile(
    r"^subprocess-timeout gate: (\d+) files with findings, "
    r"(\d+) new/grown vs backlog$")
WARN_GROWN = re.compile(
    r"^::warning::(.+): subprocess-without-timeout grew (\d+) -> (\d+)$")
WARN_NEW = re.compile(
    r"^::warning::(.+): (\d+) subprocess call\(s\) without timeout= "
    r"\(new file, not in backlog\)$")


def clean_env():
    return {"PATH": os.environ["PATH"], "LC_ALL": "C.UTF-8"}


def finding(path, rule=RULE, text=None):
    item = {"ruleId": rule, "file": path}
    if text is not None:
        item["text"] = text
    return item


def findings_for(counts, text=None):
    """Expand a {path: n} mapping into stub ast-grep finding objects."""
    items = []
    for path, n in counts.items():
        items.extend(finding(path, text=text) for _ in range(n))
    return items


def _write_stub(tmp, body):
    stub = tmp / "fake-ast-grep"
    stub.write_text("#!/usr/bin/env python3\n" + body)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return stub


def stub_returning(tmp, findings):
    return _write_stub(
        tmp, "import json, sys\n"
             f"sys.stdout.write(json.dumps({json.dumps(findings)}))\n")


def stub_failing(tmp):
    return _write_stub(
        tmp, "import sys\n"
             "sys.stderr.write('SENTINEL_AST_GREP_STDERR boom\\n')\n"
             "sys.stdout.write('SENTINEL_NOT_JSON')\n"
             "sys.exit(3)\n")


def stub_junk(tmp):
    return _write_stub(
        tmp, "import sys\nsys.stdout.write('SENTINEL_JUNK not json')\n")


def backlog_text(rows):
    lines = ["# fixture backlog"]
    lines.extend(f"{path}: {n}" for path, n in rows.items())
    return "\n".join(lines) + "\n"


class GateCase(unittest.TestCase):
    """Base: one temp dir per test, with stub binary + backlog file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="stg-json-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.backlog = self.tmp / "backlog.txt"
        self.absent_ast_grep = self.tmp / "absent-ast-grep"

    def run_cli(self, args):
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)] + list(args),
            capture_output=True, timeout=TIMEOUT, cwd=str(ROOT),
            env=clean_env())
        return (proc.returncode,
                proc.stdout.decode("utf-8"),
                proc.stderr.decode("utf-8"))

    def compare_args(self, ast_grep, backlog=None, json_mode=False):
        args = ["--ast-grep", str(ast_grep),
                "--backlog", str(self.backlog if backlog is None
                                  else backlog)]
        if json_mode:
            args.append("--json")
        return args

    def run_json(self, args, expected_exit=0):
        """Run and require exactly one JSON document, silent stderr."""
        code, stdout, stderr = self.run_cli(args)
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError:
            self.fail(f"stdout is not a single JSON document "
                      f"(exit {code}): {stdout[:200]!r} {stderr[:200]!r}")
        self.assertEqual(code, expected_exit)
        self.assertEqual(stderr, "")
        self.assertNotIn("::warning::", stdout)
        self.assertNotIn("Traceback", stdout)
        return obj

    def assert_top(self, obj, outcome, ast_grep_available, backlog=None):
        self.assertEqual(set(obj), TOP_KEYS)
        self.assertEqual(obj["schema"], SCHEMA)
        self.assertEqual(obj["outcome"], outcome)
        scan = obj["scan"]
        self.assertEqual(set(scan), SCAN_KEYS)
        self.assertIs(scan["ast_grep_available"], ast_grep_available)
        expected_backlog = self.backlog if backlog is None else backlog
        self.assertEqual(scan["backlog_path"], str(expected_backlog))

    def assert_measurable(self, obj, outcome, counts, entries):
        """entries: list of (path, before, after), sorted by path."""
        self.assert_top(obj, outcome, True,
                        backlog=self._expected_backlog)
        self.assertEqual(obj["counts"], counts)
        self.assertEqual(obj["totals"], {
            "files_with_findings": len(counts),
            "findings": sum(counts.values()),
            "new_or_grown": len(entries),
        })
        got = obj["new_or_grown"]
        self.assertEqual([e["path"] for e in got],
                         sorted(e["path"] for e in got),
                         "new_or_grown must be sorted by path")
        self.assertEqual(len(got), len(entries))
        for entry, (path, before, after) in zip(got, entries):
            self.assertEqual(set(entry), ENTRY_KEYS)
            self.assertEqual(entry["path"], path)
            self.assertEqual(entry["before"], before)
            self.assertEqual(entry["after"], after)
            self.assertLess(before, after)
            self.assertEqual(
                entry["next_action"],
                NEW_ACTION if before == 0 else GROWN_ACTION)
            self.assertNotIn("\n", entry["next_action"])
            self.assertLessEqual(len(entry["next_action"]), 200)

    def assert_unmeasurable(self, obj):
        self.assert_top(obj, "unmeasurable", False)
        self.assertIsNone(obj["counts"])
        self.assertEqual(obj["new_or_grown"], [])
        self.assertEqual(obj["totals"], {
            "files_with_findings": None,
            "findings": None,
            "new_or_grown": None,
        })

    _expected_backlog = None  # set by measurable runs via use_backlog()

    def use_backlog(self, rows):
        self.backlog.write_text(backlog_text(rows))
        self._expected_backlog = self.backlog

    def use_missing_backlog(self):
        self._expected_backlog = self.backlog  # path never created
        self.assertFalse(self.backlog.exists())


class JsonCleanTest(GateCase):
    def test_unchanged_counts_vs_backlog_are_clean(self):
        counts = {"pkg/a.py": 2, "pkg/b.py": 3}
        self.use_backlog(counts)
        stub = stub_returning(self.tmp, findings_for(counts))
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(obj, "clean", counts, [])

    def test_empty_scan_with_backlog_rows_is_clean(self):
        self.use_backlog({"pkg/a.py": 2})
        stub = stub_returning(self.tmp, [])
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(obj, "clean", {}, [])

    def test_shrunk_count_is_clean_and_silent(self):
        self.use_backlog({"pkg/a.py": 3})
        stub = stub_returning(self.tmp, findings_for({"pkg/a.py": 1}))
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(obj, "clean", {"pkg/a.py": 1}, [])

    def test_test_paths_and_other_rules_do_not_count(self):
        self.use_backlog({"pkg/a.py": 1})
        stub = stub_returning(self.tmp, [
            finding("test_a.py"), finding("ops/tests/b.py"),
            finding("pkg/a.py", rule="other-rule"),
        ])
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(obj, "clean", {}, [])


class JsonFindingsTest(GateCase):
    def test_new_file_entry(self):
        self.use_backlog({"pkg/a.py": 1})
        stub = stub_returning(self.tmp, findings_for(
            {"pkg/a.py": 1, "pkg/new.py": 2}))
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(
            obj, "findings", {"pkg/a.py": 1, "pkg/new.py": 2},
            [("pkg/new.py", 0, 2)])

    def test_grown_count_entry(self):
        self.use_backlog({"pkg/a.py": 2})
        stub = stub_returning(self.tmp, findings_for({"pkg/a.py": 5}))
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(
            obj, "findings", {"pkg/a.py": 5}, [("pkg/a.py", 2, 5)])

    def test_mixed_new_grown_shrunk_unchanged(self):
        self.use_backlog({"pkg/a.py": 2, "pkg/b.py": 3, "pkg/c.py": 1})
        counts = {"pkg/a.py": 4, "pkg/b.py": 1, "pkg/c.py": 1,
                  "pkg/z.py": 1}
        stub = stub_returning(self.tmp, findings_for(counts))
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(
            obj, "findings", counts,
            [("pkg/a.py", 2, 4), ("pkg/z.py", 0, 1)])

    def test_entries_sorted_by_path_regardless_of_finding_order(self):
        self.use_backlog({})
        stub = stub_returning(self.tmp, [
            finding("pkg/z.py"), finding("pkg/m.py"), finding("pkg/z.py"),
        ])
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(
            obj, "findings", {"pkg/m.py": 1, "pkg/z.py": 2},
            [("pkg/m.py", 0, 1), ("pkg/z.py", 0, 2)])

    def test_missing_backlog_file_makes_every_path_new(self):
        self.use_missing_backlog()
        counts = {"pkg/a.py": 3, "pkg/b.py": 1}
        stub = stub_returning(self.tmp, findings_for(counts))
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(
            obj, "findings", counts,
            [("pkg/a.py", 0, 3), ("pkg/b.py", 0, 1)])

    def test_missing_backlog_with_empty_scan_is_clean(self):
        self.use_missing_backlog()
        stub = stub_returning(self.tmp, [])
        obj = self.run_json(self.compare_args(stub, json_mode=True))
        self.assert_measurable(obj, "clean", {}, [])


class JsonUnmeasurableTest(GateCase):
    def test_missing_ast_grep_binary_is_unmeasurable_exit_zero(self):
        self.use_backlog({"pkg/a.py": 1})
        obj = self.run_json(self.compare_args(
            self.absent_ast_grep, json_mode=True))
        self.assert_unmeasurable(obj)

    def test_failing_ast_grep_is_unmeasurable(self):
        self.use_backlog({"pkg/a.py": 1})
        obj = self.run_json(self.compare_args(
            stub_failing(self.tmp), json_mode=True))
        self.assert_unmeasurable(obj)

    def test_unparseable_ast_grep_output_is_unmeasurable(self):
        self.use_backlog({"pkg/a.py": 1})
        obj = self.run_json(self.compare_args(
            stub_junk(self.tmp), json_mode=True))
        self.assert_unmeasurable(obj)


class JsonStabilityAndEchoTest(GateCase):
    def test_findings_output_byte_identical_across_two_runs(self):
        self.use_backlog({"pkg/a.py": 1})
        stub = stub_returning(self.tmp, findings_for(
            {"pkg/z.py": 2, "pkg/a.py": 3, "pkg/m.py": 1}))
        args = self.compare_args(stub, json_mode=True)
        self.run_json(args)
        first = self.run_cli(args)
        second = self.run_cli(args)
        self.assertEqual(first, second)

    def test_unmeasurable_output_byte_identical_across_two_runs(self):
        self.use_backlog({"pkg/a.py": 1})
        args = self.compare_args(self.absent_ast_grep, json_mode=True)
        self.run_json(args)
        self.assertEqual(self.run_cli(args), self.run_cli(args))

    def test_json_run_does_not_modify_backlog(self):
        self.use_backlog({"pkg/a.py": 1})
        before = self.backlog.read_bytes()
        stub = stub_returning(self.tmp, findings_for({"pkg/a.py": 2}))
        self.run_json(self.compare_args(stub, json_mode=True))
        self.run_json(self.compare_args(
            self.absent_ast_grep, json_mode=True))
        self.assertEqual(self.backlog.read_bytes(), before)

    def test_no_matched_source_text_echoed(self):
        self.use_backlog({})
        stub = stub_returning(self.tmp, findings_for(
            {"pkg/a.py": 1}, text="SENTINEL_SOURCE_TEXT run(cmd)"))
        code, stdout, stderr = self.run_cli(
            self.compare_args(stub, json_mode=True))
        self.assertEqual(code, 0)
        self.assertNotIn("SENTINEL_SOURCE_TEXT", stdout)
        self.assertNotIn("SENTINEL_SOURCE_TEXT", stderr)
        self.assertIn("pkg/a.py", stdout)  # paths and counts only

    def test_no_ast_grep_stderr_echoed_when_unmeasurable(self):
        self.use_backlog({})
        code, stdout, stderr = self.run_cli(self.compare_args(
            stub_failing(self.tmp), json_mode=True))
        self.assertEqual(code, 0)
        for sentinel in ("SENTINEL_AST_GREP_STDERR", "SENTINEL_NOT_JSON"):
            self.assertNotIn(sentinel, stdout)
            self.assertNotIn(sentinel, stderr)
        code, stdout, stderr = self.run_cli(self.compare_args(
            stub_junk(self.tmp), json_mode=True))
        self.assertNotIn("SENTINEL_JUNK", stdout)
        self.assertNotIn("SENTINEL_JUNK", stderr)

    def test_no_backlog_comment_echoed(self):
        self.backlog.write_text(
            "# SENTINEL_BACKLOG_COMMENT\npkg/a.py: 1\n")
        self._expected_backlog = self.backlog
        stub = stub_returning(self.tmp, findings_for({"pkg/a.py": 2}))
        self.run_json(self.compare_args(stub, json_mode=True))
        code, stdout, stderr = self.run_cli(
            self.compare_args(stub, json_mode=True))
        self.assertEqual(code, 0)
        self.assertNotIn("SENTINEL_BACKLOG_COMMENT", stdout)
        self.assertNotIn("SENTINEL_BACKLOG_COMMENT", stderr)


class UsageBoundaryTest(GateCase):
    def test_write_backlog_with_json_is_refused(self):
        self.use_backlog({"pkg/a.py": 1})
        before = self.backlog.read_bytes()
        stub = stub_returning(self.tmp, findings_for({"pkg/a.py": 2}))
        for extra in (["--write-backlog", "--json"],
                      ["--json", "--write-backlog"]):
            with self.subTest(extra=extra):
                code, stdout, stderr = self.run_cli(
                    ["--ast-grep", str(stub),
                     "--backlog", str(self.backlog)] + extra)
                self.assertEqual(code, 2)
                self.assertRaises(json.JSONDecodeError,
                                  json.loads, stdout)
                self.assertIn("usage:", stderr)
                self.assertIn("--write-backlog", stderr)
                self.assertIn("--json", stderr)
                # A deliberate combination refusal, not a missing flag.
                self.assertNotIn("unrecognized arguments", stderr)
                self.assertEqual(self.backlog.read_bytes(), before)

    def test_help_is_text_and_documents_json(self):
        for args in (["--help"], ["-h"], ["--json", "--help"],
                     ["--help", "--json"]):
            with self.subTest(args=args):
                code, stdout, stderr = self.run_cli(args)
                self.assertEqual(code, 0)
                self.assertIn("usage:", stdout)
                self.assertIn("--json", stdout)
                self.assertRaises(json.JSONDecodeError,
                                  json.loads, stdout)

    def test_usage_errors_are_text_never_json(self):
        for args in (["--bogus"], ["--json", "--bogus"],
                     ["--json", "--backlog"], ["--json", "--ast-grep"]):
            with self.subTest(args=args):
                code, stdout, stderr = self.run_cli(args)
                self.assertEqual(code, 2)
                self.assertRaises(json.JSONDecodeError,
                                  json.loads, stdout)
                self.assertIn("usage:", stderr)


def parse_prose(stdout):
    """Return (headline tuple or None, {(path, before, after)})."""
    headline = None
    entries = set()
    for line in stdout.splitlines():
        if (m := HEADLINE.match(line)):
            headline = (int(m.group(1)), int(m.group(2)))
        elif (m := WARN_GROWN.match(line)):
            entries.add((m.group(1), int(m.group(2)), int(m.group(3))))
        elif (m := WARN_NEW.match(line)):
            entries.add((m.group(1), 0, int(m.group(2))))
    return headline, entries


class ProseJsonParityTest(GateCase):
    def parity(self, backlog_rows, counts, missing_backlog=False):
        if missing_backlog:
            self.use_missing_backlog()
        else:
            self.use_backlog(backlog_rows)
        stub = stub_returning(self.tmp, findings_for(counts))
        prose_code, prose_out, prose_err = self.run_cli(
            self.compare_args(stub))
        obj = self.run_json(
            self.compare_args(stub, json_mode=True),
            expected_exit=prose_code)
        headline, prose_entries = parse_prose(prose_out)
        self.assertIsNotNone(headline, prose_out)
        self.assertEqual(prose_err, "")
        json_entries = {(e["path"], e["before"], e["after"])
                        for e in obj["new_or_grown"]}
        self.assertEqual(json_entries, prose_entries)
        self.assertEqual(obj["totals"]["files_with_findings"],
                         headline[0])
        self.assertEqual(obj["totals"]["new_or_grown"], headline[1])
        self.assertEqual(
            obj["outcome"], "findings" if prose_entries else "clean")
        return obj

    def test_parity_clean(self):
        counts = {"pkg/a.py": 2}
        self.parity(counts, counts)

    def test_parity_findings_mixed(self):
        self.parity({"pkg/a.py": 2, "pkg/b.py": 3},
                    {"pkg/a.py": 3, "pkg/b.py": 1, "pkg/new.py": 1})

    def test_parity_missing_backlog(self):
        self.parity(None, {"pkg/a.py": 1}, missing_backlog=True)

    def test_parity_unmeasurable_exit_and_never_clean(self):
        self.use_backlog({"pkg/a.py": 1})
        prose_code, prose_out, _ = self.run_cli(
            self.compare_args(self.absent_ast_grep))
        self.assertIn("scan unavailable, skipping backlog comparison",
                      prose_out)
        obj = self.run_json(
            self.compare_args(self.absent_ast_grep, json_mode=True),
            expected_exit=prose_code)
        self.assert_unmeasurable(obj)


class PositiveControlTest(GateCase):
    """Prose behavior that already holds today and must not regress."""

    def test_prose_unchanged_headline_only(self):
        self.use_backlog({"pkg/a.py": 2, "pkg/b.py": 3})
        stub = stub_returning(self.tmp, findings_for(
            {"pkg/a.py": 2, "pkg/b.py": 3}))
        code, stdout, stderr = self.run_cli(self.compare_args(stub))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "subprocess-timeout gate: 2 files with "
                                 "findings, 0 new/grown vs backlog\n")

    def test_prose_new_and_grown_warnings(self):
        self.use_backlog({"pkg/a.py": 2, "pkg/b.py": 3})
        stub = stub_returning(self.tmp, findings_for(
            {"pkg/a.py": 3, "pkg/b.py": 1, "pkg/new.py": 1}))
        code, stdout, stderr = self.run_cli(self.compare_args(stub))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "subprocess-timeout gate: 3 files with findings, "
            "2 new/grown vs backlog\n"
            "::warning::pkg/a.py: subprocess-without-timeout "
            "grew 2 -> 3\n"
            "::warning::pkg/new.py: 1 subprocess call(s) without "
            "timeout= (new file, not in backlog)\n")

    def test_prose_missing_binary_warns_unavailable_exit_zero(self):
        self.use_backlog({"pkg/a.py": 1})
        code, stdout, stderr = self.run_cli(
            self.compare_args(self.absent_ast_grep))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(
            "::warning::ast-grep scan failed (FileNotFoundError):"))
        self.assertIn(str(self.absent_ast_grep), lines[0])
        self.assertEqual(
            lines[1],
            "subprocess-timeout gate: scan unavailable, "
            "skipping backlog comparison")

    def test_prose_missing_backlog_treats_paths_as_new(self):
        self.use_missing_backlog()
        stub = stub_returning(self.tmp, findings_for({"pkg/a.py": 1}))
        code, stdout, stderr = self.run_cli(self.compare_args(stub))
        self.assertEqual(code, 0)
        self.assertIn("1 files with findings, 1 new/grown", stdout)
        self.assertIn(
            "::warning::pkg/a.py: 1 subprocess call(s) without "
            "timeout= (new file, not in backlog)", stdout)

    def test_prose_empty_scan_headline(self):
        self.use_backlog({"pkg/a.py": 2})
        stub = stub_returning(self.tmp, [])
        code, stdout, stderr = self.run_cli(self.compare_args(stub))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "subprocess-timeout gate: 0 files with "
                                 "findings, 0 new/grown vs backlog\n")

    def test_write_backlog_prose_success(self):
        self.use_backlog({"pkg/old.py": 9})
        stub = stub_returning(self.tmp, findings_for({"pkg/z.py": 2}))
        code, stdout, stderr = self.run_cli(
            self.compare_args(stub) + ["--write-backlog"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, f"wrote 1 backlog rows to "
                                 f"{self.backlog}\n")
        written = self.backlog.read_text()
        self.assertIn("pkg/z.py: 2\n", written)
        self.assertNotIn("pkg/old.py", written)
        self.assertTrue(written.startswith("#"))

    def test_write_backlog_refuses_on_failed_scan(self):
        self.use_backlog({"pkg/a.py": 2})
        before = self.backlog.read_bytes()
        code, stdout, stderr = self.run_cli(
            self.compare_args(stub_failing(self.tmp))
            + ["--write-backlog"])
        self.assertNotEqual(code, 0)
        self.assertIn("refusing to rewrite backlog", stderr)
        self.assertEqual(self.backlog.read_bytes(), before)

    def test_usage_and_help_exits_today(self):
        code, stdout, stderr = self.run_cli(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", stderr)
        code, stdout, stderr = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", stdout)
        self.assertIn("--ast-grep", stdout)
        self.assertIn("--write-backlog", stdout)


if __name__ == "__main__":
    unittest.main()
