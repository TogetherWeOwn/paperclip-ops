#!/usr/bin/env python3
"""Supplemental READ-ONLY compiler-feedback acceptance, not a repair runner.

Run with the unchanged original suite:
  python3 -B -m unittest -v test_compiler_feedback_contract test_compiler_feedback_regressions

These independently specified regressions extend compiler-feedback-report.v1.
Real Git graph/file/blob oracles, not tree-ID inequality or supplied parent
strings, determine whether a source repair is possible. JSON decimal durations
are exact for classification and remain JSON numbers in the report.

Evaluator-owned module binding RESOURCE_LIMITS has exactly these defaults:
  trace_bytes=1048576, blob_bytes=1048576, total_blob_bytes=8388608,
  attempt_records=64, git_invocations=1024, git_stdout_bytes=4194304,
  evaluation_seconds=30.
No policy, trace or CLI input can override them. In-process tests replace only
this binding with tiny limits and time.monotonic with a controlled clock; real
parsing, Git, immutable policy lookup and report generation still execute.
Missing bindings are created only by mock.patch so the old evaluator actually
runs and demonstrates the ignored limit, rather than failing on a test import.

Count bytes (not Unicode characters), each immutable blob including policy,
all processed blob bytes (repeated reads count again unless cached), attempt
records before Git traversal, every Git invocation and each operation's stdout.
Bound reads by the remaining allowance plus one sentinel byte, not full capture
followed by a size check. Deadline covers the entire evaluation, not just one
Git timeout. Crossed limits yield exit 2, unmeasurable/resource_limit, dimension
null, retryable false, nonempty next_action, no invented consumed zeroes, no
repair metrics and no verified Actions metrics. Stop/drain child processes and
clean scratch. Rejection cannot undo an already executed external trace.

Only tiny synthetic local fixtures are used; there is no compiler execution,
network call, actual resource exhaustion, authenticated Actions evidence or
production probe. The frozen acceptance file is imported, never edited. Every
evaluation checks repository/index/refs/worktree/input and source non-mutation.
"""

import builtins
import contextlib
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest import mock

import test_compiler_feedback_contract as frozen


RESOURCE_LIMITS = {
    "trace_bytes": 1048576,
    "blob_bytes": 1048576,
    "total_blob_bytes": 8388608,
    "attempt_records": 64,
    "git_invocations": 1024,
    "git_stdout_bytes": 4194304,
    "evaluation_seconds": 30,
}
FROZEN_SHA256 = "821dd0d663e50e113fcdca49791b0ba397d027211ab6fc0ef120f4e7e002e928"
ZERO_METRICS = {"attempts": 0, "elapsed_seconds": 0, "repairs": 0}


class BoundedReader:
    """Tiny real stream that fails an unbounded/over-allowance read immediately."""

    def __init__(self, stream, limit, *, enforce=True, after_read=None):
        self.stream = stream
        self.limit = limit
        self.enforce = enforce
        self.after_read = after_read
        self.seen = 0

    def _size(self, size):
        if self.enforce and (size is None or size < 0 or size > self.limit + 1 - self.seen):
            raise AssertionError("evidence stream was buffered without a remaining-byte bound")
        return size

    def read(self, size=-1):
        data = self.stream.read(self._size(size))
        self.seen += len(data)
        if self.after_read is not None:
            self.after_read()
        return data

    def read1(self, size=-1):
        return self.read(size)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def readline(self, size=-1):
        data = self.stream.readline(self._size(size))
        self.seen += len(data)
        if self.after_read is not None:
            self.after_read()
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.stream.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class GuardedProcess:
    """Keep real Git, but prohibit full capture of one controlled boundary stream."""

    def __init__(self, process, limit):
        self.process = process
        if process.stdout is None:
            process.kill()
            process.wait(timeout=10)
            raise AssertionError("controlled Git evidence must use a bounded stdout stream")
        self.stdout = BoundedReader(process.stdout, limit)

    def communicate(self, *args, **kwargs):
        self.process.kill()
        self.process.wait(timeout=10)
        raise AssertionError("Git evidence was captured in full before its stdout/blob ceiling")

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None and self.process.poll() is None:
            self.process.kill()
        return self.process.__exit__(kind, value, traceback)

    def __getattr__(self, name):
        return getattr(self.process, name)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def expire(self):
        self.now = 131.0


class RegressionFixture(frozen.RepositoryFixture):
    object_format = "sha1"

    def setUp(self):
        super().setUp()
        self.scratch = self.home / "scratch"
        self.scratch.mkdir()
        self.env["TMPDIR"] = str(self.scratch)

    def git(self, *args):
        if args and args[0] == "init":
            args = (*args, "--object-format=" + self.object_format)
        try:
            return super().git(*args)
        except subprocess.CalledProcessError as exc:
            if args and args[0] == "init":
                self.fail(f"Git prerequisite {self.object_format} repository unavailable: {exc.stderr!r}")
            raise

    def git_bytes(self, *args, data=None, check=True):
        result = subprocess.run(
            [frozen.REAL_GIT, *args], cwd=self.repo, env=self.env,
            input=data, capture_output=True, timeout=10,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def snapshot(self):
        # Include dirty files, index, refs, objects, inputs, wrapper bytes and modes.
        result = {}
        for path in self.home.rglob("*"):
            mode = path.lstat().st_mode
            payload = (os.readlink(path).encode() if stat.S_ISLNK(mode) else
                       path.read_bytes() if stat.S_ISREG(mode) else b"")
            result[str(path.relative_to(self.home))] = (
                stat.S_IMODE(mode), hashlib.sha256(payload).hexdigest(),
            )
        for path in (Path(frozen.__file__), Path(__file__), frozen.SCRIPT):
            result["source:" + path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def load_evaluator(self):
        spec = importlib.util.spec_from_file_location("compiler_feedback_supplemental", frozen.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        # Dataclass/typing consumers may resolve their defining module at import.
        with mock.patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
        return module

    def evaluate(self, trace=None, *, status="repaired", code="repaired", dimension=None,
                 base=None, head=None, raw=None, env=None, in_process=False, limits=None,
                 guard_trace=False, guard_git=None, guard_git_limit=None, clock=None,
                 after_trace_read=None, extra_args=(), usage_error=False):
        trace = self.trace() if trace is None else trace
        raw = json.dumps(trace, ensure_ascii=False).encode() if raw is None else raw
        self.trace_file.write_bytes(raw)
        before = self.snapshot()
        argv = [str(frozen.SCRIPT), "--repo", str(self.repo),
                "--base=" + (base or self.base), "--head=" + (head or trace["head_sha"]),
                "--trace", str(self.trace_file), *extra_args]
        self.last_git_commands = []
        processes = []
        try:
            if in_process:
                module = self.load_evaluator()
                out, err = io.StringIO(), io.StringIO()
                original_popen = subprocess.Popen
                original_open, original_io_open, original_path_open = builtins.open, io.open, Path.open

                def popen(command, *args, **kwargs):
                    is_git = isinstance(command, (list, tuple)) and Path(str(command[0])).name == "git"
                    if is_git:
                        self.last_git_commands.append(tuple(map(str, command)))
                    process = original_popen(command, *args, **kwargs)
                    processes.append(process)
                    if is_git and guard_git is not None and guard_git(command):
                        return GuardedProcess(process, guard_git_limit if guard_git_limit is not None
                                              else limits["git_stdout_bytes"])
                    return process

                def observe_open(opener):
                    def opened(path, *args, **kwargs):
                        stream = opener(path, *args, **kwargs)
                        if not isinstance(path, int) and Path(path) == self.trace_file and not isinstance(stream, BoundedReader):
                            return BoundedReader(stream, limits["trace_bytes"], enforce=guard_trace,
                                                 after_read=after_trace_read)
                        return stream
                    return opened

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.dict(os.environ, env or self.env, clear=True))
                    stack.enter_context(mock.patch("tempfile.tempdir", str(self.scratch)))
                    stack.enter_context(mock.patch.object(sys, "argv", argv))
                    stack.enter_context(mock.patch("subprocess.Popen", side_effect=popen))
                    stack.enter_context(mock.patch.dict(sys.modules, {module.__name__: module}))
                    stack.enter_context(contextlib.redirect_stdout(out))
                    stack.enter_context(contextlib.redirect_stderr(err))
                    if limits is not None:
                        stack.enter_context(mock.patch.object(module, "RESOURCE_LIMITS", limits, create=True))
                    if clock is not None:
                        stack.enter_context(mock.patch("time.monotonic", clock))
                    if guard_trace or after_trace_read is not None:
                        stack.enter_context(mock.patch("builtins.open", observe_open(original_open)))
                        stack.enter_context(mock.patch("io.open", observe_open(original_io_open)))
                        stack.enter_context(mock.patch.object(Path, "open", observe_open(original_path_open)))
                    try:
                        exit_code = module.main()
                    except SystemExit as exc:
                        exit_code = exc.code
                result = subprocess.CompletedProcess(argv, exit_code, out.getvalue(), err.getvalue())
            else:
                result = subprocess.run([sys.executable, *argv], env=env or self.env,
                                        capture_output=True, timeout=20)
        finally:
            leaked = []
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                    leaked.append(process.args)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                        leaked.append("unclosed Git pipe")
            self.assertEqual(leaked, [], "evaluation left a child/pipe open; harness stopped it")
            self.assertEqual(self.snapshot(), before, "evaluation mutated repository/index/refs/worktree/input/source/scratch")
            self.assertEqual(self.trace_file.read_bytes(), raw, "evaluation rewrote supplied evidence")
            self.assertFalse(self.marker.exists(), "evaluation executed input data")
        self.last_stdout = result.stdout
        self.assertEqual(result.returncode, 2 if usage_error else {"repaired": 0, "exhausted": 1, "unmeasurable": 2}[status],
                         repr(result.stdout) + repr(result.stderr))
        if usage_error:
            self.assertFalse(result.stdout, "usage errors must not claim an evaluated repair")
            return None
        try:
            report = json.loads(result.stdout)
            self.decimal_report = json.loads(result.stdout, parse_float=Decimal,
                                             parse_constant=lambda token: self.fail("nonfinite JSON output: " + token))
        except (ValueError, UnicodeError) as exc:
            self.fail(f"expected exactly one JSON report, not a traceback: {exc}; {result.stderr!r}")
        self.assertIsInstance(report, dict)
        for key, want in (("schema", "compiler-feedback-report.v1"), ("status", status),
                          ("code", code), ("dimension", dimension)):
            self.assertEqual(report[key], want, report)
        self.assertIsInstance(report["retryable"], bool)
        if status != "repaired":
            self.assertIsInstance(report["next_action"], str)
            self.assertTrue(report["next_action"].strip(), report)
        for field in ("attempts", "elapsed_seconds", "changed_paths", "changed_lines"):
            self.assertIn(field, report["consumed"])
        self.assertEqual(report["metrics"]["actions_verified"], ZERO_METRICS)
        self.assertFalse(report["evidence"]["receipts_verified"])
        for bucket in ("fixture", "actions_reported", "actions_verified"):
            self.assertIn(bucket, report["metrics"])
            if status != "repaired":
                self.assertEqual(report["metrics"][bucket]["repairs"], 0, report)
        return report

    def decimal_trace(self, first, second, *, limit, passed=True):
        # Distinct tokens preserve JSON decimal lexemes that Python floats round.
        self.reset_policy(lambda p: p["budgets"].update(
            attempts=8, elapsed_seconds=limit, changed_paths=100, changed_lines=100))
        trace = self.trace(passed=False, elapsed="__first_decimal__")
        self.put("src/value.ts", frozen.AFTER + "// final\n")
        self.add_attempt(trace, passed=passed, elapsed="__second_decimal__")
        raw = json.dumps(trace).replace('"__first_decimal__"', first).replace('"__second_decimal__"', second)
        return trace, raw.encode()

    def assert_numeric_duration(self, report, want):
        for record in (report["consumed"], report["metrics"]["actions_reported"]):
            self.assertIs(type(record["elapsed_seconds"]), float)
        self.assertEqual(self.decimal_report["consumed"]["elapsed_seconds"], Decimal(want))
        self.assertEqual(self.decimal_report["metrics"]["actions_reported"]["elapsed_seconds"], Decimal(want))

    def resource(self, *, trace=None, limits=None, known_zero_seconds=False, **kwargs):
        report = self.evaluate(trace, status="unmeasurable", code="resource_limit", in_process=True,
                               limits={**RESOURCE_LIMITS, **(limits or {})}, **kwargs)
        self.assertFalse(report["retryable"])
        # Unknown measurements are null. Only the explicitly supplied zero-duration
        # fixture may truthfully report measured zero seconds.
        for field in ("attempts", "elapsed_seconds", "changed_paths", "changed_lines"):
            value = report["consumed"][field]
            if value is not None:
                self.assertIn(type(value), (int, float), report)
                if field == "elapsed_seconds" and known_zero_seconds:
                    self.assertGreaterEqual(value, 0, report)
                else:
                    self.assertGreater(value, 0, report)
        return report


class HistoryRegressions(RegressionFixture):
    def test_frozen_acceptance_bytes_stay_independent(self):
        self.assertEqual(hashlib.sha256(Path(frozen.__file__).read_bytes()).hexdigest(), FROZEN_SHA256)

    def test_late_parent_header_cannot_forge_git_ancestry(self):
        tree = self.git("rev-parse", self.head + "^{tree}")
        body = (f"tree {tree}\nauthor Fixture <fixture@example.invalid> 1735689600 +0000\n"
                f"committer Fixture <fixture@example.invalid> 1735689600 +0000\nparent {self.base}\n\n"
                + frozen.COMMIT_MESSAGE + "\n").encode()
        candidate = self.git_bytes("hash-object", "-t", "commit", "-w", "--stdin", data=body).stdout.decode().strip()
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", candidate), candidate)
        self.assertEqual(self.git_bytes("merge-base", "--is-ancestor", self.base, candidate, check=False).returncode, 1)
        self.unmeasurable(self.trace(head=candidate), "invalid_trace")

    def empty_subtree_candidate(self, base_tree):
        empty = self.git_bytes("mktree", data=b"").stdout.decode().strip()
        entries = self.git_bytes("ls-tree", "-z", base_tree).stdout
        changed = self.git_bytes("mktree", "-z", data=entries + f"040000 tree {empty}\tempty".encode() + b"\0").stdout.decode().strip()
        self.assertNotEqual(changed, base_tree)
        return self.git("commit-tree", changed, "-p", self.base, "-m", frozen.COMMIT_MESSAGE)

    def test_empty_subtree_tree_id_is_not_a_source_change(self):
        candidate = self.empty_subtree_candidate(self.git("rev-parse", self.base + "^{tree}"))
        for format_flag in ("--raw", "--name-status", "--numstat"):
            self.assertEqual(self.git_bytes("diff", "--no-renames", format_flag, "-z", self.base, candidate).stdout, b"")
        self.assertEqual(self.git_bytes("ls-tree", "-r", "-z", self.base).stdout,
                         self.git_bytes("ls-tree", "-r", "-z", candidate).stdout)
        self.unmeasurable(self.trace(head=candidate), "no_repair_change")

    def test_empty_commit_still_cannot_be_called_repaired(self):
        tree = self.git("rev-parse", self.base + "^{tree}")
        candidate = self.git("commit-tree", tree, "-p", self.base, "-m", frozen.COMMIT_MESSAGE)
        self.assertEqual(self.git_bytes("diff", "--raw", "-z", self.base, candidate).stdout, b"")
        self.unmeasurable(self.trace(head=candidate), "no_repair_change")

    def test_real_file_change_with_empty_subtree_is_measured(self):
        candidate = self.empty_subtree_candidate(self.git("rev-parse", self.head + "^{tree}"))
        report = self.evaluate(self.trace(head=candidate))
        self.assert_consumed(report)
        self.assertEqual(report["paths"], ["src/value.ts"])

    def test_cli_and_root_head_agree_but_attempt_chain_stops_before_protected_edit(self):
        trace = self.trace()
        attempted = trace["attempts"][-1]["candidate_sha"]
        self.put("tests/acceptance.py", "assert False\n")
        terminal = self.commit()
        trace["head_sha"] = terminal
        self.assertEqual(self.git("rev-parse", terminal + "^"), attempted)
        self.assertEqual(self.git("diff", "--name-only", attempted, terminal), "tests/acceptance.py")
        self.unmeasurable(trace, "invalid_provenance", head=terminal)


class DecimalRegressions(RegressionFixture):
    def test_decimal_point_one_plus_point_two_at_point_three_accepts_final_pass(self):
        trace, raw = self.decimal_trace("0.1", "0.2", limit=0.3)
        report = self.evaluate(trace, raw=raw)
        self.assert_numeric_duration(report, "0.3")
        self.assertEqual(report["consumed"]["attempts"], 2)
        self.assertEqual(report["metrics"]["actions_reported"]["repairs"], 1)

    def test_decimal_point_one_plus_point_seven_at_point_eight_exhausts_final_failure(self):
        trace, raw = self.decimal_trace("0.1", "0.7", limit=0.8, passed=False)
        report = self.exhausted(trace, "elapsed_seconds", raw=raw)
        self.assert_numeric_duration(report, "0.8")

    def test_strictly_over_decimal_cannot_hide_inside_a_float_tolerance(self):
        trace, raw = self.decimal_trace("0.1", "0.20000000000000001", limit=0.3)
        report = self.exhausted(trace, "elapsed_seconds", raw=raw)
        # Classification must honor the exact lexemes; finite JSON-number output
        # may use a rounded numeric representation, never a quoted Decimal.
        for record in (report["consumed"], report["metrics"]["actions_reported"]):
            self.assertIn(type(record["elapsed_seconds"]), (int, float))
            self.assertGreaterEqual(record["elapsed_seconds"], 0.3)

    def test_invalid_decimal_durations_keep_unknown_consumption(self):
        for literal in ("-0.1", "NaN", "Infinity", "-Infinity", "1e309", "true", "null", '"0.1"'):
            with self.subTest(literal=literal):
                trace = self.trace(elapsed="__duration__")
                raw = json.dumps(trace).replace('"__duration__"', literal).encode()
                report = self.unmeasurable(trace, "invalid_duration", raw=raw)
                self.assertIsNone(report["consumed"]["elapsed_seconds"])

    def test_decimal_sum_overflow_remains_unmeasurable_not_budget_exhaustion(self):
        trace, raw = self.decimal_trace("1e308", "1e308", limit=10)
        report = self.unmeasurable(trace, "invalid_duration", raw=raw)
        self.assertIsNone(report["consumed"]["elapsed_seconds"])


class EvidenceRegressions(RegressionFixture):
    def test_zero_padded_decimal_run_id_replays_the_same_receipt(self):
        trace = self.trace()
        trace["attempts"][0]["compiler"]["actions"]["run_id"] = "00100"
        self.assertEqual(trace["before"]["actions"]["run_id"], "100")
        self.unmeasurable(trace, "invalid_provenance")

    def test_zero_padded_run_id_with_different_attempt_is_not_a_replay(self):
        trace = self.trace()
        trace["attempts"][0]["compiler"]["actions"].update(run_id="00100", run_attempt=2)
        self.assert_consumed(self.evaluate(trace))

    def test_valid_name_status_omitting_protected_raw_path_is_invalid_diff(self):
        self.put("tests/acceptance.py", "assert False\n")
        candidate = self.candidate()
        raw = self.git_bytes("diff", "--raw", "-z", "--no-renames", self.base, candidate).stdout
        self.assertIn(b"tests/acceptance.py\0", raw)
        self.assertIn(b"src/value.ts\0", raw)
        # This is syntactically valid name-status, not the old malformed-parser case.
        env = self.git_fault(stdout=b"M\0src/value.ts\0", selector="--name-status")
        self.unmeasurable(self.trace(head=candidate), "invalid_diff", env=env)

    def test_simultaneous_content_and_mode_edit_is_invalid_even_with_distinct_blobs(self):
        (self.repo / "src/value.ts").chmod(0o755)
        candidate = self.candidate()
        raw = self.git_bytes("diff", "--raw", "--no-abbrev", self.base, candidate).stdout.decode()
        fields = raw.split()
        self.assertEqual(fields[:2], [":100644", "100755"])
        self.assertNotEqual(fields[2], fields[3])
        self.unmeasurable(self.trace(head=candidate), "invalid_diff")

    def test_dirty_index_refs_worktree_and_inputs_do_not_substitute_for_commit_evidence(self):
        self.put("tests/acceptance.py", "dirty protected fixture\n")
        self.git("add", "tests/acceptance.py")
        self.put("src/value.ts", "dirty unstaged fixture\n")
        self.put("src/untracked.ts", "untracked fixture\n")
        self.git("update-ref", "refs/fixture/unchanged", self.base)
        self.assert_consumed(self.evaluate())


class Sha256Regressions(RegressionFixture):
    object_format = "sha256"

    def test_real_sha256_policy_parent_blob_ids_and_report_refs_work_end_to_end(self):
        self.assertEqual(self.git("rev-parse", "--show-object-format"), "sha256")
        self.assertEqual(len(self.base), 64)
        self.assertEqual(len(self.head), 64)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", self.head), self.head + " " + self.base)
        self.assertEqual(self.git("show", self.base + ":" + frozen.POLICY_PATH), json.dumps(frozen.POLICY))
        raw = self.git_bytes("diff", "--raw", "--no-abbrev", self.base, self.head).stdout.decode().split()
        self.assertEqual(len(raw[2]), 64)
        self.assertEqual(len(raw[3]), 64)
        self.assertEqual(raw[2], self.git("rev-parse", self.base + ":src/value.ts"))
        self.assertEqual(raw[3], self.git("rev-parse", self.head + ":src/value.ts"))
        report = self.evaluate()
        self.assert_consumed(report)
        for field, want in (("base_sha", self.base), ("head_sha", self.head),
                            ("policy_sha", self.base), ("tool_sha", self.base)):
            self.assertEqual(report[field], want)
        self.assertEqual(report["paths"], ["src/value.ts"])
        self.assertEqual(report["metrics"]["actions_reported"], {"attempts": 1, "elapsed_seconds": 3, "repairs": 1})

    def test_sha256_truncated_chain_still_rejects_unobserved_protected_head(self):
        trace = self.trace()
        self.put("tests/acceptance.py", "assert False\n")
        trace["head_sha"] = self.commit()
        self.unmeasurable(trace, "invalid_provenance")


class ResourceRegressions(RegressionFixture):
    def test_evaluator_owned_production_ceilings_are_pinned(self):
        module = self.load_evaluator()
        self.assertEqual(getattr(module, "RESOURCE_LIMITS", None), RESOURCE_LIMITS,
                         "missing or incorrect evaluator-owned production ceilings")

    def test_small_in_process_control_exercises_real_cli_and_git(self):
        self.assert_consumed(self.evaluate(in_process=True, limits=dict(RESOURCE_LIMITS)))
        self.assertGreater(len(self.last_git_commands), 0)

    def test_trace_at_its_byte_ceiling_reaches_real_evaluation(self):
        raw = json.dumps(self.trace()).encode()
        self.assert_consumed(self.evaluate(raw=raw, in_process=True,
                                           limits={**RESOURCE_LIMITS, "trace_bytes": len(raw)}))

    def test_trace_one_byte_over_ceiling_is_refused_before_git_traversal(self):
        raw = json.dumps(self.trace()).encode()
        report = self.resource(raw=raw, limits={"trace_bytes": len(raw) - 1})
        self.assertTrue(all(value is None for value in report["consumed"].values()))
        self.assertEqual(self.last_git_commands, [])

    def test_trace_limit_counts_utf8_bytes_not_codepoints(self):
        trace = self.trace()
        trace["before"]["diagnostics"] = ["☃" * 8]
        raw = json.dumps(trace, ensure_ascii=False).encode()
        self.assertGreater(len(raw), len(raw.decode("utf-8")))
        self.resource(trace=trace, raw=raw, limits={"trace_bytes": len(raw.decode("utf-8"))})

    def test_trace_stream_is_bounded_before_full_buffering(self):
        raw = json.dumps(self.trace()).encode()
        self.assert_consumed(self.evaluate(raw=raw, in_process=True, guard_trace=True,
                                           limits={**RESOURCE_LIMITS, "trace_bytes": len(raw)}))

    def single_line_blob(self):
        # 401 bytes, one line; evidence path/line budgets do not bound blob bytes.
        content = "x" * 400 + "\n"
        self.put("src/value.ts", content)
        self.head = self.candidate()
        self.assertEqual(self.git("diff", "--numstat", self.base, self.head), "1\t1\tsrc/value.ts")
        return len(content.encode()), self.git("rev-parse", self.head + ":src/value.ts")

    def test_single_line_blob_at_ceiling_is_measured(self):
        size, _ = self.single_line_blob()
        self.assert_consumed(self.evaluate(in_process=True, limits={**RESOURCE_LIMITS, "blob_bytes": size}))

    def test_single_line_blob_one_byte_over_ceiling_is_resource_not_scope(self):
        size, _ = self.single_line_blob()
        self.resource(limits={"blob_bytes": size - 1})

    def test_oversized_blob_is_not_fully_captured_before_refusal(self):
        size, oid = self.single_line_blob()
        self.resource(limits={"blob_bytes": size - 1}, guard_git_limit=size - 1,
                      guard_git=lambda command: (("cat-file" in command and
                                                  ("blob" in command or "-p" in command) and oid in command)
                                                 or ("show" in command and self.head + ":src/value.ts" in command)))

    def test_immutable_policy_blob_also_obeys_blob_ceiling(self):
        policy_size = len(self.git_bytes("show", self.base + ":" + frozen.POLICY_PATH).stdout)
        self.assertGreater(policy_size, len(frozen.BEFORE.encode()))
        self.resource(limits={"blob_bytes": policy_size - 1})

    def cumulative_blob_fixture(self):
        self.git("reset", "--hard", self.base)
        self.put("src/value.ts", "a" * 255 + "\n")
        self.base = self.commit()
        self.put("src/value.ts", "b" * 255 + "\n")
        self.head = self.commit()
        policy_size = len(self.git_bytes("show", self.base + ":" + frozen.POLICY_PATH).stdout)
        return policy_size + 512

    def test_cumulative_blob_bytes_at_required_sum_are_measurable(self):
        total = self.cumulative_blob_fixture()
        self.assert_consumed(self.evaluate(in_process=True, limits={**RESOURCE_LIMITS, "total_blob_bytes": total}))

    def test_cumulative_blob_bytes_cannot_be_replaced_by_largest_blob_only(self):
        total = self.cumulative_blob_fixture()
        self.resource(limits={"total_blob_bytes": total - 1})

    def test_cumulative_budget_is_checked_before_next_blob_full_capture(self):
        total = self.cumulative_blob_fixture()
        oid = self.git("rev-parse", self.head + ":src/value.ts")
        self.resource(limits={"total_blob_bytes": total - 1}, guard_git_limit=256,
                      guard_git=lambda command: "cat-file" in command and "blob" in command and oid in command)

    def test_attempt_records_over_evaluator_limit_are_refused_before_git(self):
        self.reset_policy(lambda p: p["budgets"].update(attempts=100, elapsed_seconds=100, changed_lines=100))
        trace = self.trace(passed=False, elapsed=1)
        for index in range(2):
            self.put("src/value.ts", frozen.AFTER + f"// candidate {index}\n")
            self.add_attempt(trace, passed=index == 1, elapsed=1)
        self.resource(trace=trace, limits={"attempt_records": 2})
        self.assertEqual(self.last_git_commands, [])

    def test_attempt_record_limit_is_inclusive(self):
        trace = self.trace(passed=False, elapsed=1)
        self.put("src/value.ts", frozen.AFTER + "// final\n")
        self.add_attempt(trace, elapsed=1)
        report = self.evaluate(trace, in_process=True, limits={**RESOURCE_LIMITS, "attempt_records": 2})
        self.assert_consumed(report, attempts=2, seconds=2, lines=3)

    def test_zero_git_invocation_allowance_refuses_without_launching_a_process(self):
        self.resource(limits={"git_invocations": 0})
        self.assertEqual(self.last_git_commands, [])

    def test_git_invocation_ceiling_is_inclusive_at_real_fixture_work_count(self):
        self.evaluate(in_process=True, limits=dict(RESOURCE_LIMITS))
        # Count real process starts, independently of the evaluator's own counter.
        calls = len(self.last_git_commands)
        self.assertGreater(calls, 0)
        self.assert_consumed(self.evaluate(in_process=True, limits={**RESOURCE_LIMITS, "git_invocations": calls}))
        self.resource(limits={"git_invocations": calls - 1})
        self.assertLessEqual(len(self.last_git_commands), calls - 1)

    def stdout_fixture(self, length):
        payload = b"M\0src/" + b"x" * (length - 7) + b"\0"
        self.assertEqual(len(payload), length)
        return self.git_fault(stdout=payload, selector="--name-status")

    def test_git_stdout_at_ceiling_reaches_diff_integrity_not_resource_refusal(self):
        self.unmeasurable(code="invalid_diff", in_process=True,
                          limits={**RESOURCE_LIMITS, "git_stdout_bytes": 512}, env=self.stdout_fixture(512))

    def test_git_stdout_one_byte_over_ceiling_is_resource_not_parser_failure(self):
        self.resource(limits={"git_stdout_bytes": 512}, env=self.stdout_fixture(513))

    def test_git_stdout_stream_is_bounded_before_full_capture(self):
        self.resource(limits={"git_stdout_bytes": 512}, env=self.stdout_fixture(513),
                      guard_git=lambda command: "--name-status" in command)

    def test_whole_evaluation_deadline_includes_trace_reading_before_git(self):
        clock = FakeClock()
        self.resource(clock=clock, after_trace_read=clock.expire)
        # No Git timeout can cover the controlled 31 seconds spent before Git.
        self.assertEqual(self.last_git_commands, [])

    def test_attempt_duration_cannot_replace_the_evaluator_deadline_clock(self):
        self.reset_policy(lambda p: p["budgets"].update(elapsed_seconds=100000))
        clock = FakeClock()
        self.resource(trace=self.trace(elapsed=0), known_zero_seconds=True,
                      clock=clock, after_trace_read=clock.expire)
        self.assertEqual(self.last_git_commands, [])

    def test_trace_cannot_raise_evaluator_limits(self):
        trace = self.trace()
        trace["resource_limits"] = {key: value * 100 for key, value in RESOURCE_LIMITS.items()}
        self.unmeasurable(trace, "invalid_trace")

    def test_base_policy_cannot_raise_evaluator_limits(self):
        self.reset_policy(lambda p: p.update(resource_limits={key: value * 100 for key, value in RESOURCE_LIMITS.items()}))
        self.unmeasurable(code="invalid_policy")

    def test_cli_does_not_expose_a_resource_limit_override(self):
        self.evaluate(extra_args=("--resource-limits", json.dumps(RESOURCE_LIMITS)), usage_error=True)

    def test_environment_cannot_raise_evaluator_owned_limits(self):
        raw = json.dumps(self.trace()).encode()
        override = json.dumps({key: value * 100 for key, value in RESOURCE_LIMITS.items()})
        self.resource(raw=raw, limits={"trace_bytes": len(raw) - 1},
                      env={**self.env, "COMPILER_FEEDBACK_RESOURCE_LIMITS": override})


if __name__ == "__main__":
    unittest.main()
