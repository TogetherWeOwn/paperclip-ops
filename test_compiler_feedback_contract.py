#!/usr/bin/env python3
"""Acceptance contract for a READ-ONLY compiler-feedback report, not a repair runner.

CLI (no compiler, shell, installer, network, edit or apply operation):
  python3 scripts/quality-gates/compiler_feedback.py --repo REPO \
      --base FULL_SHA --head FULL_SHA --trace TRACE.json

stdout: exactly one JSON object, schema ``compiler-feedback-report.v1``.
Exit/status: 0/repaired, 1/exhausted, 2/unmeasurable. Every report includes
code, dimension (null unless exhausted), consumed, paths, metrics, retryable
and next_action. Non-success has a stable code and nonempty next_action.
Measured reports echo base_sha, head_sha, policy_sha and tool_sha.

Trust inputs: --base/--head are full immutable commit SHAs. Read the version-1
policy below from BASE:.compiler-feedback.json, never the worktree or candidate.
policy_sha and tool_sha must both equal BASE in this bounded v1 contract; the
caller must pin the deployed evaluator to that tool revision. Trace and every
compiler record repeat these refs. Do not resolve branches, tags or replace refs.
Every candidate must be a direct single-parent child of its stated parent;
first parent_sha is BASE, subsequent parent_sha is the preceding candidate,
and last candidate is HEAD. No merge commits or omitted intermediate commits.
A final tree with no source change from BASE is unmeasurable/no_repair_change.

Trace schema: see trace(), compiler() below. Unknown keys, duplicate JSON keys,
invalid UTF-8/JSON, non-finite numbers, missing fields, bool-as-number, missing
commit objects and incomplete results fail closed. Attempts are 1-based and
contiguous. Duration is finite, nonnegative seconds for the WHOLE observed
attempt (including its compiler run); do not substitute evaluator wall time.
The initial failed compiler result is not a repair attempt or attempt duration.
Require a failed-before nonempty diagnostic, then failed intermediate compilers
and a final complete pass (exit 0, no diagnostics). A pass terminates a trace.
An unfinished failing trace below every ceiling is unmeasurable/incomplete_trace.

Policy budgets are inclusive: success AT all ceilings is repaired; any OVER
ceiling is exhausted/budget_exceeded even if the last compiler passes. A final
failure AT a ceiling is exhausted/budget_exceeded. Dimension precedence is
attempts, elapsed_seconds, changed_paths, changed_lines. Count all attempts,
sum durations, union paths across EVERY parent->candidate diff, and sum added
plus deleted lines across those diffs. Use NUL-delimited, no-renames Git output:
both sides of rename count, deletions count, reverted intermediate edits count.
Binary/unmeasurable line counts and symlink/submodule/mode-only changes are
unmeasurable/invalid_diff, never zero-cost. Report paths sorted, unquoted.

Scope: fnmatchcase on repo-relative paths with '/' separators (as docs_drift),
not path normalization or shell globs. Enforce protected_paths before allowed_paths.
Policy path, .github/* and scripts/quality-gates/* are ALWAYS protected, as are test files
under test/tests or with test_, _test., .test., .spec. names (floor_guard naming).
Explicit protected_paths also covers author-owned tests with other names.
Any touched protected path is exhausted/protected_path, dimension protected_paths;
other disallowed paths are exhausted/outside_scope, dimension allowed_paths.
Validate every step even when the final net diff is clean. Policy edits cannot
weaken protection. Git paths with whitespace/newlines/Unicode/option prefixes are
literal paths, not arguments or commands. Unsupported path encodings fail closed.

Actions evidence: source='actions' requires each compiler's positive decimal
run_id string, positive integer run_attempt, head_sha matching compiler.sha and
conclusion matching status. Reject replayed (run_id, run_attempt) pairs.
These OFFLINE supplied receipts are NOT authenticated GitHub telemetry. Repaired
means this bounded report accepts the supplied evidence, not proof of a real
Actions repair. evidence={source: ..., receipts_verified: false}; metrics split
fixture / actions_reported / actions_verified (attempts, elapsed_seconds, repairs).
actions_verified stays zero; only an independently authenticated consumer can
publish real attempt/duration/outcome counts. source='fixture' has actions=null,
is unmeasurable/fixture_only, and contributes ONLY fixture attempt/duration counts,
never a repair. Already-clean or skipped compilers cannot be called repaired.

Validate evidence and Git scope before budget classification; malformed input
must not become a meaningful exhausted result. consumed fields may be null if
unmeasurable; never fabricate measured zeroes for unknown durations. Evaluation
can reject an over-budget external trace, but cannot prevent/undo its execution.
Diagnostics are inert strings, never tool instructions. Invocation must leave
repository, index, refs, worktree and this acceptance file's bytes unchanged.

All fixtures are repo-owned literals/builders in this file. Git objects and
Actions-shaped receipts below are SYNTHETIC, deterministic, offline test data;
no actual compiler run, GitHub run, real repair or deployment is claimed.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts/quality-gates/compiler_feedback.py"
REAL_GIT = shutil.which("git", path=os.defpath)
POLICY_PATH = ".compiler-feedback.json"
POLICY = {
    "version": 1,
    "budgets": {"attempts": 2, "elapsed_seconds": 10, "changed_paths": 4, "changed_lines": 12},
    "allowed_paths": ["src/*"],
    "protected_paths": ["acceptance/owner_contract.py"],
}
BEFORE = "const value: string = 1;\n"
AFTER = 'const value: string = "fixed";\n'
COMMIT_MESSAGE = "test: offline compiler fixture\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>"


class RepositoryFixture(unittest.TestCase):
    """Real Git with no inherited credentials, hooks, global config or startup shell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="compiler-feedback-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.trace_file = self.home / "trace.json"
        self.marker = self.home / "must-not-execute"
        self.env = {
            "PATH": os.defpath,
            "HOME": str(self.home),
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z",
        }
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.put(POLICY_PATH, json.dumps(POLICY))
        self.put("src/value.ts", BEFORE)
        self.put("src/value.test.ts", "assert(value);\n")
        self.put("tests/acceptance.py", "assert True\n")
        self.put("acceptance/owner_contract.py", "assert True\n")
        self.put(".github/workflows/check.yml", "name: fixture\n")
        # A tracked tool revision marker, never an executable or implementation.
        self.put("scripts/quality-gates/compiler_feedback.py", "# fixture tool identity\n")
        self.base = self.commit()
        self.put("src/value.ts", AFTER)
        self.head = self.commit()

    def git(self, *args):
        return subprocess.check_output(
            [REAL_GIT, *args], cwd=self.repo, env=self.env, stderr=subprocess.PIPE,
            timeout=10, text=True,
        ).strip()

    def put(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "commit", "-qm", COMMIT_MESSAGE)
        return self.git("rev-parse", "HEAD")

    def candidate(self):
        # Amend only this disposable fixture's candidate, never an execution branch.
        if self.git("rev-parse", "HEAD") == self.base:
            return self.commit()
        self.git("add", "-A")
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "commit", "--amend", "-qm", COMMIT_MESSAGE)
        return self.git("rev-parse", "HEAD")

    def compiler(self, sha, *, passed=False, run_id="100"):
        return {
            "sha": sha, "policy_sha": self.base, "tool_sha": self.base,
            "status": "passed" if passed else "failed", "complete": True,
            "exit_code": 0 if passed else 1,
            "diagnostics": [] if passed else ["src/value.ts(1,7): error TS2322: number is not string"],
            "actions": {"run_id": run_id, "run_attempt": 1, "head_sha": sha,
                        "conclusion": "success" if passed else "failure"},
        }

    def trace(self, *, head=None, passed=True, elapsed=3):
        head = head or self.head
        return {
            "schema": "compiler-feedback-trace.v1", "source": "actions",
            "base_sha": self.base, "head_sha": head,
            "policy_sha": self.base, "tool_sha": self.base,
            "before": self.compiler(self.base),
            "attempts": [{"index": 1, "parent_sha": self.base, "candidate_sha": head,
                          "elapsed_seconds": elapsed,
                          "compiler": self.compiler(head, passed=passed, run_id="101")}],
        }

    def add_attempt(self, trace, *, passed=True, elapsed=4):
        previous = trace["attempts"][-1]["candidate_sha"]
        candidate = self.commit()
        index = len(trace["attempts"]) + 1
        trace["attempts"].append({
            "index": index, "parent_sha": previous, "candidate_sha": candidate,
            "elapsed_seconds": elapsed,
            "compiler": self.compiler(candidate, passed=passed, run_id=str(100 + index)),
        })
        trace["head_sha"] = candidate
        return candidate

    def reset_policy(self, change):
        # Fixture-only history construction, never invoked by the evaluator.
        self.git("reset", "--hard", self.base)
        policy = copy.deepcopy(POLICY)
        change(policy)
        self.put(POLICY_PATH, json.dumps(policy))
        self.base = self.commit()
        self.put("src/value.ts", AFTER)
        self.head = self.commit()

    def snapshot(self):
        result = {}
        for path in self.repo.rglob("*"):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                payload = os.readlink(path).encode()
            elif stat.S_ISREG(mode):
                payload = path.read_bytes()
            else:
                payload = b""
            result[str(path.relative_to(self.repo))] = (stat.S_IMODE(mode), hashlib.sha256(payload).hexdigest())
        result["acceptance-file"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return result

    def evaluate(self, trace=None, *, status="repaired", code="repaired", dimension=None,
                 base=None, head=None, raw=None, env=None, trace_path=None):
        trace = self.trace() if trace is None else trace
        if raw is None:
            raw = json.dumps(trace, ensure_ascii=False).encode("utf-8")
        self.trace_file.write_bytes(raw)
        before = self.snapshot()
        self.assertTrue(SCRIPT.is_file(), "Missing compiler-feedback evaluator: test-first contract, implementation not delivered")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo),
             "--base=" + (base if base is not None else self.base),
             "--head=" + (head if head is not None else trace.get("head_sha", self.head)),
             "--trace", str(trace_path or self.trace_file)],
            env=env or self.env, capture_output=True, timeout=15,
        )
        self.assertEqual(self.snapshot(), before, "report evaluation mutated repository/test bytes")
        self.assertEqual(self.trace_file.read_bytes(), raw, "report evaluation rewrote input evidence")
        self.assertFalse(self.marker.exists(), "data, Git hooks or compiler instructions were executed")
        expected_exit = {"repaired": 0, "exhausted": 1, "unmeasurable": 2}[status]
        self.assertEqual(result.returncode, expected_exit, repr(result.stdout) + repr(result.stderr))
        try:
            report = json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            self.fail(f"CLI must emit one structured JSON report, not a traceback: {exc}; {result.stderr!r}")
        self.assertIsInstance(report, dict)
        self.assertEqual(report["schema"], "compiler-feedback-report.v1")
        self.assertEqual(report["status"], status, report)
        self.assertEqual(report["code"], code, report)
        self.assertEqual(report["dimension"], dimension, report)
        self.assertIsInstance(report["retryable"], bool)
        if status != "repaired":
            self.assertIsInstance(report["next_action"], str)
            self.assertTrue(report["next_action"].strip(), report)
        for key in ("attempts", "elapsed_seconds", "changed_paths", "changed_lines"):
            self.assertIn(key, report["consumed"])
        for key in ("fixture", "actions_reported", "actions_verified"):
            self.assertIn(key, report["metrics"])
        self.assertEqual(report["metrics"]["actions_verified"],
                         {"attempts": 0, "elapsed_seconds": 0, "repairs": 0})
        return report

    def unmeasurable(self, trace=None, code="invalid_trace", **kwargs):
        return self.evaluate(trace, status="unmeasurable", code=code, **kwargs)

    def exhausted(self, trace, dimension, code="budget_exceeded", **kwargs):
        return self.evaluate(trace, status="exhausted", code=code, dimension=dimension, **kwargs)

    def assert_consumed(self, report, attempts=1, seconds=3, paths=1, lines=2):
        self.assertEqual(report["consumed"], {"attempts": attempts, "elapsed_seconds": seconds,
                                             "changed_paths": paths, "changed_lines": lines})

    def git_fault(self, *, stdout=None, exit_code=0, selector="--numstat"):
        # Replace only Git's diff transport; all other Git commands stay real.
        directory = self.home / "bin"
        directory.mkdir(exist_ok=True)
        wrapper = directory / "git"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os,sys\n"
            f"if 'diff' in sys.argv and {selector!r} in sys.argv:\n"
            f"    sys.stdout.buffer.write({stdout or b''!r})\n    sys.exit({exit_code})\n"
            f"os.execv({REAL_GIT!r}, [{REAL_GIT!r}] + sys.argv[1:])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return {**self.env, "PATH": str(directory) + os.pathsep + os.defpath}


class FixtureControls(RepositoryFixture):
    """Passing controls verify the harness, NOT the missing report implementation."""

    def test_real_git_fixture_has_two_changed_lines_and_one_path(self):
        self.assertEqual(self.git("diff", "--numstat", self.base, self.head), "1\t1\tsrc/value.ts")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("show", self.base + ":" + POLICY_PATH), json.dumps(POLICY))

    def test_snapshot_detects_index_refs_and_test_bytes(self):
        original = self.snapshot()
        self.put("tests/acceptance.py", "assert False\n")
        changed = self.snapshot()
        self.assertNotEqual(original["tests/acceptance.py"], changed["tests/acceptance.py"])
        self.git("add", "tests/acceptance.py")
        self.assertNotEqual(original[".git/index"], self.snapshot()[".git/index"])
        self.commit()
        self.assertNotEqual(original[".git/refs/heads/main"], self.snapshot()[".git/refs/heads/main"])

    def test_receipts_are_synthetic_and_fixture_can_be_explicitly_labeled(self):
        trace = self.trace()
        self.assertEqual(trace["before"]["actions"]["run_id"], "100")
        self.assertEqual(trace["attempts"][0]["compiler"]["actions"]["run_id"], "101")
        self.assertEqual(trace["attempts"][0]["elapsed_seconds"], 3)
        self.assertFalse(self.marker.exists())


class CompilerFeedbackContract(RepositoryFixture):
    # Each case names an observable break: false repair, lost budget, accepted
    # untrusted evidence/scope, executed data, or mutation of the evaluated repo.

    def test_failed_before_then_complete_pass_is_repaired_with_exact_refs(self):
        report = self.evaluate()
        self.assert_consumed(report)
        self.assertEqual(report["paths"], ["src/value.ts"])
        for key, expected in (("base_sha", self.base), ("head_sha", self.head),
                              ("policy_sha", self.base), ("tool_sha", self.base)):
            self.assertEqual(report[key], expected)
        self.assertEqual(report["evidence"], {"source": "actions", "receipts_verified": False})
        self.assertEqual(report["metrics"]["actions_reported"],
                         {"attempts": 1, "elapsed_seconds": 3, "repairs": 1})
        self.assertEqual(report["metrics"]["fixture"], {"attempts": 0, "elapsed_seconds": 0, "repairs": 0})

    def test_two_attempt_success_sums_time_and_line_churn_not_net_diff(self):
        trace = self.trace(passed=False)
        self.put("src/value.ts", AFTER + "// corrected again\n")
        self.add_attempt(trace)
        self.assert_consumed(self.evaluate(trace), attempts=2, seconds=7, paths=1, lines=3)

    def test_success_exactly_at_all_four_ceilings_is_accepted(self):
        self.reset_policy(lambda p: p["budgets"].update(
            attempts=1, elapsed_seconds=3, changed_paths=1, changed_lines=2))
        self.assert_consumed(self.evaluate())

    def test_zero_duration_is_known_not_missing(self):
        self.assert_consumed(self.evaluate(self.trace(elapsed=0)), seconds=0)

    def test_fractional_duration_is_summed_without_integer_truncation(self):
        trace = self.trace(passed=False, elapsed=0.25)
        self.put("src/value.ts", AFTER + "// second\n")
        self.add_attempt(trace, elapsed=0.5)
        self.assert_consumed(self.evaluate(trace), attempts=2, seconds=0.75, lines=3)

    def test_over_attempt_ceiling_rejects_even_final_success(self):
        trace = self.trace(passed=False)
        self.put("src/value.ts", AFTER + "// still failing\n")
        self.add_attempt(trace, passed=False, elapsed=1)
        self.put("src/value.ts", AFTER + "// final pass\n")
        self.add_attempt(trace, elapsed=1)
        self.assert_consumed(self.exhausted(trace, "attempts"), attempts=3, seconds=5, lines=5)

    def test_over_time_ceiling_rejects_success(self):
        report = self.exhausted(self.trace(elapsed=10.001), "elapsed_seconds")
        self.assert_consumed(report, seconds=10.001)

    def test_over_changed_path_ceiling_uses_git_not_a_claimed_path_list(self):
        for name in ("src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"):
            self.put(name, "new\n")
        trace = self.trace(head=self.candidate())
        report = self.exhausted(trace, "changed_paths")
        self.assert_consumed(report, paths=5, lines=6)
        self.assertEqual(report["paths"], ["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts", "src/value.ts"])

    def test_over_changed_line_ceiling_counts_added_and_deleted(self):
        self.put("src/value.ts", "".join(f"line {i}\n" for i in range(12)))
        report = self.exhausted(self.trace(head=self.candidate()), "changed_lines")
        self.assert_consumed(report, lines=13)

    def test_failed_compiler_exactly_at_each_ceiling_is_exhausted(self):
        ceilings = [("attempts", 1), ("elapsed_seconds", 3), ("changed_paths", 1), ("changed_lines", 2)]
        for dimension, limit in ceilings:
            with self.subTest(dimension=dimension):
                self.reset_policy(lambda p: p["budgets"].update({dimension: limit}))
                self.assert_consumed(self.exhausted(self.trace(passed=False), dimension))

    def test_multiple_exhaustions_have_deterministic_dimension_precedence(self):
        self.reset_policy(lambda p: p["budgets"].update(attempts=1, elapsed_seconds=1, changed_lines=1))
        trace = self.trace(passed=False)
        self.put("src/value.ts", AFTER + "// next\n")
        self.add_attempt(trace)
        self.exhausted(trace, "attempts")

    def test_final_failure_below_ceilings_is_incomplete_not_repaired(self):
        self.unmeasurable(self.trace(passed=False), "incomplete_trace")

    def test_nonfinite_negative_bool_null_and_string_durations_fail_closed(self):
        for duration in (-1, True, None, "3", float("nan"), float("inf"), -float("inf")):
            with self.subTest(duration=duration):
                report = self.unmeasurable(self.trace(elapsed=duration), "invalid_duration")
                self.assertIsNone(report["consumed"]["elapsed_seconds"])

    def test_missing_duration_is_not_fabricated_as_zero(self):
        trace = self.trace()
        del trace["attempts"][0]["elapsed_seconds"]
        report = self.unmeasurable(trace, "invalid_duration")
        self.assertIsNone(report["consumed"]["elapsed_seconds"])

    def test_empty_or_wrong_attempt_container_is_invalid(self):
        for attempts in ([], None, {}, "one"):
            with self.subTest(attempts=attempts):
                trace = self.trace()
                trace["attempts"] = attempts
                self.unmeasurable(trace)

    def test_attempt_index_must_be_one_based_contiguous_integer(self):
        for index in (0, 2, True, "1"):
            with self.subTest(index=index):
                trace = self.trace()
                trace["attempts"][0]["index"] = index
                self.unmeasurable(trace)

    def test_already_clean_before_is_not_a_repair(self):
        trace = self.trace()
        trace["before"] = self.compiler(self.base, passed=True)
        self.unmeasurable(trace, "no_failed_before")

    def test_failed_before_requires_a_nonempty_diagnostic(self):
        for diagnostics in ([], [""], ["   "]):
            with self.subTest(diagnostics=diagnostics):
                trace = self.trace()
                trace["before"]["diagnostics"] = diagnostics
                self.unmeasurable(trace, "no_failed_before")

    def test_skipped_incomplete_or_inconsistent_final_compiler_is_not_success(self):
        mutations = [{"status": "skipped"}, {"complete": False}, {"complete": None},
                     {"exit_code": None}, {"exit_code": True}, {"exit_code": 1},
                     {"diagnostics": ["still failing"]}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                trace = self.trace()
                trace["attempts"][0]["compiler"].update(mutation)
                self.unmeasurable(trace, "incomplete_compiler")

    def test_missing_final_compiler_evidence_fails_closed(self):
        trace = self.trace()
        del trace["attempts"][0]["compiler"]
        self.unmeasurable(trace)

    def test_missing_initial_evidence_fails_closed(self):
        trace = self.trace()
        del trace["before"]
        self.unmeasurable(trace)

    def test_failed_intermediate_with_zero_exit_is_invalid(self):
        trace = self.trace(passed=False)
        trace["attempts"][0]["compiler"]["exit_code"] = 0
        self.put("src/value.ts", AFTER + "// pass\n")
        self.add_attempt(trace)
        self.unmeasurable(trace, "incomplete_compiler")

    def test_attempt_after_success_is_invalid_not_an_extra_repair(self):
        trace = self.trace()
        self.put("src/value.ts", AFTER + "// unnecessary\n")
        self.add_attempt(trace)
        self.unmeasurable(trace)

    def test_unrelated_candidate_cannot_claim_a_valid_parent(self):
        tree = self.git("rev-parse", self.head + "^{tree}")
        orphan = self.git("commit-tree", tree, "-m", COMMIT_MESSAGE)
        self.unmeasurable(self.trace(head=orphan), "invalid_trace")

    def test_parent_chain_must_include_preceding_attempt(self):
        trace = self.trace(passed=False)
        self.put("src/value.ts", AFTER + "// pass\n")
        self.add_attempt(trace)
        trace["attempts"][1]["parent_sha"] = self.base
        self.unmeasurable(trace)

    def test_cli_and_trace_head_must_match(self):
        self.unmeasurable(self.trace(), "invalid_provenance", head=self.base)

    def test_compiler_ref_must_match_its_candidate(self):
        trace = self.trace()
        trace["attempts"][0]["compiler"]["sha"] = self.base
        self.unmeasurable(trace, "invalid_provenance")

    def test_base_compiler_ref_must_match_failed_before(self):
        trace = self.trace()
        trace["before"]["sha"] = self.head
        self.unmeasurable(trace, "invalid_provenance")

    def test_policy_and_tool_refs_are_pinned_for_trace_and_all_compilers(self):
        for location in ("trace", "before", "final"):
            for field in ("policy_sha", "tool_sha"):
                with self.subTest(location=location, field=field):
                    trace = self.trace()
                    record = trace if location == "trace" else (
                        trace["before"] if location == "before" else trace["attempts"][0]["compiler"])
                    record[field] = self.head
                    self.unmeasurable(trace, "invalid_provenance")

    def test_branch_short_and_option_like_refs_are_not_immutable(self):
        for ref in ("main", self.head[:12], "--help", "HEAD", "../HEAD"):
            with self.subTest(ref=ref):
                self.unmeasurable(code="invalid_sha", head=ref)

    def test_missing_commit_object_is_unmeasurable(self):
        self.unmeasurable(self.trace(head="0" * 40), "git_failed")

    def test_actions_receipt_fields_are_required_and_typed(self):
        mutations = [{"run_id": None}, {"run_id": ""}, {"run_id": "0"}, {"run_id": "x"},
                     {"run_id": 101}, {"run_attempt": 0}, {"run_attempt": True},
                     {"run_attempt": "1"}, {"head_sha": self.base}, {"conclusion": "failure"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                trace = self.trace()
                trace["attempts"][0]["compiler"]["actions"].update(mutation)
                self.unmeasurable(trace, "invalid_provenance")

    def test_missing_actions_receipts_on_initial_or_final_result_is_not_real_evidence(self):
        for location in ("before", "final"):
            with self.subTest(location=location):
                trace = self.trace()
                record = trace["before"] if location == "before" else trace["attempts"][0]["compiler"]
                record["actions"] = None
                self.unmeasurable(trace, "invalid_provenance")

    def test_replayed_actions_run_attempt_cannot_prove_two_results(self):
        trace = self.trace()
        trace["attempts"][0]["compiler"]["actions"]["run_id"] = "100"
        self.unmeasurable(trace, "invalid_provenance")

    def test_fixture_only_results_do_not_increment_actions_or_repair_metrics(self):
        trace = self.trace()
        trace["source"] = "fixture"
        trace["before"]["actions"] = None
        trace["attempts"][0]["compiler"]["actions"] = None
        report = self.unmeasurable(trace, "fixture_only")
        self.assertEqual(report["evidence"], {"source": "fixture", "receipts_verified": False})
        self.assertEqual(report["metrics"]["fixture"], {"attempts": 1, "elapsed_seconds": 3, "repairs": 0})
        self.assertEqual(report["metrics"]["actions_reported"], {"attempts": 0, "elapsed_seconds": 0, "repairs": 0})

    def test_unknown_evidence_source_is_not_treated_as_actions(self):
        trace = self.trace()
        trace["source"] = "live"
        self.unmeasurable(trace)

    def test_untrusted_path_claim_cannot_replace_git_evidence(self):
        trace = self.trace()
        trace["attempts"][0]["changed_paths"] = []
        self.unmeasurable(trace)

    def test_every_conventional_test_name_is_protected_even_inside_allowed_src(self):
        for name in ("src/test_value.py", "src/value_test.py", "src/nested/value.test.ts",
                     "src/value.spec.ts", "src/tests/value.py", "src/test/value.py"):
            with self.subTest(name=name):
                self.git("reset", "--hard", self.head)
                self.put(name, "assert True\n")
                self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")

    def test_author_owned_nonconventional_test_is_protected_by_base_policy(self):
        self.put("acceptance/owner_contract.py", "assert False\n")
        report = self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")
        self.assertIn("acceptance/owner_contract.py", report["paths"])

    def test_test_deletion_cannot_hide_behind_successful_compiler(self):
        (self.repo / "tests/acceptance.py").unlink()
        self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")

    def test_test_renamed_to_allowed_source_checks_old_path(self):
        self.git("mv", "tests/acceptance.py", "src/renamed.py")
        report = self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")
        self.assertIn("tests/acceptance.py", report["paths"])
        self.assertIn("src/renamed.py", report["paths"])

    def test_allowed_source_renamed_to_test_checks_new_path(self):
        self.git("mv", "src/value.ts", "tests/renamed.ts")
        self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")

    def test_source_rename_counts_both_paths_and_all_line_churn(self):
        self.git("reset", "--hard", self.base)
        self.git("mv", "src/value.ts", "src/renamed.ts")
        report = self.evaluate(self.trace(head=self.candidate()))
        self.assert_consumed(report, paths=2, lines=2)
        self.assertEqual(report["paths"], ["src/renamed.ts", "src/value.ts"])

    def test_allowed_source_deletion_counts_removed_lines(self):
        (self.repo / "src/value.ts").unlink()
        self.assert_consumed(self.evaluate(self.trace(head=self.candidate())), lines=1)

    def test_outside_scope_path_is_rejected(self):
        self.put("elsewhere/value.ts", "new\n")
        self.exhausted(self.trace(head=self.candidate()), "allowed_paths", "outside_scope")

    def test_reverted_intermediate_protected_edit_cannot_disappear_in_net_diff(self):
        self.put("tests/acceptance.py", "assert False\n")
        first = self.candidate()
        trace = self.trace(head=first, passed=False)
        self.put("tests/acceptance.py", "assert True\n")
        self.add_attempt(trace)
        self.assertNotIn("tests/acceptance.py", self.git("diff", "--name-only", self.base, trace["head_sha"]))
        self.exhausted(trace, "protected_paths", "protected_path")

    def test_reverted_source_churn_still_consumes_lines_and_union_paths(self):
        trace = self.trace(passed=False)
        self.put("src/value.ts", BEFORE)
        self.put("src/additional.ts", "export {};\n")
        self.add_attempt(trace)
        report = self.evaluate(trace)
        self.assert_consumed(report, attempts=2, seconds=7, paths=2, lines=5)

    def test_policy_or_tool_edit_and_policy_deletion_cannot_weaken_scope(self):
        for path, content in ((POLICY_PATH, json.dumps({**POLICY, "protected_paths": []})),
                              ("scripts/quality-gates/compiler_feedback.py", "# replaced\n"),
                              (POLICY_PATH, None)):
            with self.subTest(path=path, deleted=content is None):
                self.git("reset", "--hard", self.head)
                if content is None:
                    (self.repo / path).unlink()
                else:
                    self.put(path, content)
                self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")

    def test_worktree_policy_weakening_is_ignored_and_left_unchanged(self):
        self.put("src/large.ts", "line\n" * 12)
        trace = self.trace(head=self.candidate())
        self.put(POLICY_PATH, json.dumps({**POLICY, "budgets": {"attempts": 999}}))
        self.exhausted(trace, "changed_lines")

    def test_policy_is_required_at_base_not_candidate(self):
        self.git("reset", "--hard", self.base)
        (self.repo / POLICY_PATH).unlink()
        self.base = self.commit()
        self.put(POLICY_PATH, json.dumps(POLICY))
        self.head = self.commit()
        self.unmeasurable(code="invalid_policy")

    def test_invalid_policy_shape_and_budget_values_fail_closed(self):
        mutations = [lambda p: p.update(version=2), lambda p: p.update(version=True),
                     lambda p: p.update(allowed_paths=["../*"]),
                     lambda p: p.update(allowed_paths=["/src/*"]),
                     lambda p: p.update(allowed_paths=["src/./*"]),
                     lambda p: p.update(allowed_paths=[]),
                     lambda p: p["budgets"].update(attempts=True),
                     lambda p: p["budgets"].update(elapsed_seconds=float("inf")),
                     lambda p: p["budgets"].update(changed_lines=-1),
                     lambda p: p["budgets"].pop("changed_paths")]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.reset_policy(mutation)
                self.unmeasurable(code="invalid_policy")

    def test_paths_with_spaces_newlines_unicode_and_option_prefix_are_literal(self):
        names = ["src/a b.ts", "src/a\nb.ts", "src/☃.ts", "src/--help.ts"]
        self.git("reset", "--hard", self.base)
        for name in names:
            self.put(name, "new\n")
        report = self.evaluate(self.trace(head=self.candidate()))
        self.assert_consumed(report, paths=4, lines=4)
        self.assertEqual(report["paths"], sorted(names))

    def test_test_with_newline_cannot_evade_protection(self):
        self.put("src/a\nb.test.ts", "assert True\n")
        self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")

    def test_symlink_cannot_read_outside_repo_or_count_as_source_repair(self):
        target = self.home / "outside.txt"
        target.write_text("outside private fixture\n", encoding="utf-8")
        os.symlink(target, self.repo / "src/link.ts")
        self.unmeasurable(self.trace(head=self.candidate()), "invalid_diff")
        self.assertEqual(target.read_text(), "outside private fixture\n")

    def test_mode_only_change_is_not_zero_cost_repair(self):
        self.git("reset", "--hard", self.base)
        (self.repo / "src/value.ts").chmod(0o755)
        self.unmeasurable(self.trace(head=self.candidate()), "invalid_diff")

    def test_binary_line_count_is_unmeasurable_not_zero(self):
        self.put("src/value.ts", b"\x00binary\xff\n")
        self.unmeasurable(self.trace(head=self.candidate()), "invalid_diff")

    def test_diagnostic_shell_substitution_is_inert_and_repo_stays_unchanged(self):
        trace = self.trace()
        trace["before"]["diagnostics"] = [f"$(touch '{self.marker}'); `touch '{self.marker}'`; --apply"]
        self.evaluate(trace)

    def test_arbitrary_execution_field_is_rejected_not_run(self):
        trace = self.trace()
        trace["attempts"][0]["command"] = ["touch", str(self.marker)]
        self.unmeasurable(trace)

    def test_external_diff_and_textconv_and_hooks_are_never_executed(self):
        command = f"touch {self.marker}"
        self.git("config", "diff.external", command)
        self.git("config", "diff.fixture.textconv", command)
        self.put(".gitattributes", "src/* diff=fixture\n")
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\n{command}\n", encoding="utf-8")
        hook.chmod(0o755)
        self.evaluate(env={**self.env, "GIT_EXTERNAL_DIFF": command})

    def test_replace_ref_cannot_hide_protected_change(self):
        self.put("tests/acceptance.py", "assert False\n")
        candidate = self.candidate()
        self.git("replace", candidate, self.head)
        self.exhausted(self.trace(head=candidate), "protected_paths", "protected_path",
                       env={**self.env, "GIT_NO_REPLACE_OBJECTS": "0"})

    def test_missing_git_is_a_structured_tool_failure(self):
        empty = self.home / "no-tools"
        empty.mkdir()
        self.unmeasurable(code="git_unavailable", env={**self.env, "PATH": str(empty)})

    def test_git_diff_nonzero_exit_is_not_clean(self):
        self.unmeasurable(code="git_failed", env=self.git_fault(exit_code=42))

    def test_malformed_numstat_is_not_a_zero_line_success(self):
        for raw in (b"garbage\x00", b"1\t1\tsrc/value.ts", b"x\t1\tsrc/value.ts\x00",
                    b"-1\t1\tsrc/value.ts\x00"):
            with self.subTest(raw=raw):
                self.unmeasurable(code="invalid_diff", env=self.git_fault(stdout=raw))

    def test_malformed_name_status_fails_closed(self):
        for raw in (b"M\x00src/value.ts", b"Q\x00src/value.ts\x00", b"M\x00../outside.ts\x00"):
            with self.subTest(raw=raw):
                self.unmeasurable(code="invalid_diff", env=self.git_fault(stdout=raw, selector="--name-status"))

    def test_malformed_json_and_utf8_are_structured_parser_failures(self):
        for raw in (b"{", b"{} trailing", b"[]", b"null", b"\xff", b'{"schema":1,"schema":2}'):
            with self.subTest(raw=raw):
                self.unmeasurable(raw=raw)

    def test_missing_trace_file_is_structured_not_a_traceback(self):
        self.unmeasurable(code="trace_unavailable", trace_path=self.home / "missing.json")

    def test_unknown_schema_and_top_level_fields_cannot_change_the_policy(self):
        for mutation in ({"schema": "compiler-feedback-trace.v2"}, {"budgets": {"attempts": 999}},
                         {"apply": True}):
            with self.subTest(mutation=mutation):
                trace = self.trace()
                trace.update(mutation)
                self.unmeasurable(trace)

    def test_omitted_intermediate_commit_cannot_hide_test_tampering(self):
        self.put("tests/acceptance.py", "assert False\n")
        self.commit()
        self.put("tests/acceptance.py", "assert True\n")
        # No receipt for the hidden first commit: v1 requires direct parents.
        self.unmeasurable(self.trace(head=self.commit()))

    def test_merge_candidate_is_not_a_linear_observed_attempt(self):
        tree = self.git("rev-parse", self.head + "^{tree}")
        other = self.git("commit-tree", tree, "-p", self.base, "-m", COMMIT_MESSAGE)
        merge = self.git("commit-tree", tree, "-p", self.base, "-p", other, "-m", COMMIT_MESSAGE)
        self.unmeasurable(self.trace(head=merge))

    def test_no_source_change_cannot_be_called_a_repair(self):
        self.git("reset", "--hard", self.base)
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "commit", "--allow-empty", "-qm", COMMIT_MESSAGE)
        self.unmeasurable(self.trace(head=self.git("rev-parse", "HEAD")), "no_repair_change")

    def test_duration_sum_overflow_is_unknown_not_a_measured_exhaustion(self):
        trace = self.trace(passed=False, elapsed=1e308)
        self.put("src/value.ts", AFTER + "// final\n")
        self.add_attempt(trace, elapsed=1e308)
        report = self.unmeasurable(trace, "invalid_duration")
        self.assertIsNone(report["consumed"]["elapsed_seconds"])

    def test_actions_receipt_is_required_on_intermediate_failure_too(self):
        trace = self.trace(passed=False)
        trace["attempts"][0]["compiler"]["actions"] = None
        self.put("src/value.ts", AFTER + "// final\n")
        self.add_attempt(trace)
        self.unmeasurable(trace, "invalid_provenance")

    def test_trace_base_must_match_the_trusted_cli_base(self):
        trace = self.trace()
        trace["base_sha"] = self.head
        self.unmeasurable(trace, "invalid_provenance")

    def test_workflow_is_protected_even_when_policy_does_not_list_it(self):
        self.put(".github/workflows/check.yml", "name: disabled\n")
        self.exhausted(self.trace(head=self.candidate()), "protected_paths", "protected_path")

    def test_rename_to_outside_scope_cannot_hide_the_destination(self):
        (self.repo / "elsewhere").mkdir()
        self.git("mv", "src/value.ts", "elsewhere/value.ts")
        self.exhausted(self.trace(head=self.candidate()), "allowed_paths", "outside_scope")

    def test_gitlink_cannot_be_counted_as_a_one_line_source_change(self):
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.base + ",src/module")
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "commit", "--amend", "-qm", COMMIT_MESSAGE)
        self.unmeasurable(self.trace(head=self.git("rev-parse", "HEAD")), "invalid_diff")

    def test_dirty_index_and_test_worktree_do_not_replace_immutable_evidence(self):
        self.put("src/value.ts", BEFORE)
        self.put("tests/acceptance.py", "assert False\n")
        self.git("add", "tests/acceptance.py")
        self.put("src/untracked.ts", "not candidate evidence\n")
        self.assert_consumed(self.evaluate())

    def test_malformed_evidence_wins_over_exceeded_budget(self):
        trace = self.trace(elapsed=100)
        trace["attempts"][0]["compiler"]["actions"] = None
        self.unmeasurable(trace, "invalid_provenance")


if __name__ == "__main__":
    unittest.main()
