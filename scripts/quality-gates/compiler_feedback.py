#!/usr/bin/env python3
"""Evaluate bounded compiler-feedback evidence offline; never execute a repair.

Policy is read from the base commit. Supplied Actions receipts remain reported,
not authenticated, evidence. See docs/compiler-feedback.md for the v1 contract.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import fnmatch
from fractions import Fraction
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import subprocess
import tempfile
import time


POLICY_PATH = ".compiler-feedback.json"
DIMENSIONS = ("attempts", "elapsed_seconds", "changed_paths", "changed_lines")
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DECIMAL = re.compile(r"[0-9]+\Z")
# Evaluator-owned ceilings on the resources one evaluation may consume. They
# are fixed here so evidence budgets cannot be laundered into unbounded local
# work: no CLI option, trace field, base policy or environment value can
# raise them. A crossed ceiling is a structured resource_limit refusal.
RESOURCE_LIMITS = {
    "trace_bytes": 1048576,
    "blob_bytes": 1048576,
    "total_blob_bytes": 8388608,
    "attempt_records": 64,
    "git_invocations": 1024,
    "git_stdout_bytes": 4194304,
    "evaluation_seconds": 30,
}
ACTIONS = {
    "invalid_trace": "Supply a complete v1 trace with contiguous observed attempts and no unknown fields.",
    "invalid_duration": "Supply finite, nonnegative whole-attempt durations, including every compiler run.",
    "invalid_policy": "Pin a valid v1 policy in the base commit; do not use a candidate or worktree policy.",
    "invalid_sha": "Supply full immutable commit IDs, not branches, tags or abbreviated refs.",
    "git_failed": "Make the exact commit objects available locally and inspect the failed Git operation.",
    "git_unavailable": "Provision Git before evaluating; no tools are installed by this evaluator.",
    "invalid_diff": "Supply valid textual, regular-file Git diffs with measurable scope.",
    "invalid_provenance": "Supply matching immutable refs and unique Actions run/attempt receipts for every result.",
    "incomplete_compiler": "Supply complete, consistent compiler results; skipped or partial checks cannot prove repair.",
    "no_failed_before": "Supply a complete failed-before result with a nonempty diagnostic.",
    "no_repair_change": "Supply a candidate with an actual source change from the base tree.",
    "incomplete_trace": "Finish the bounded observation; the last compiler still failed below every ceiling.",
    "fixture_only": "Keep fixture results separate; obtain real Actions evidence before claiming a real repair.",
    "trace_unavailable": "Provide a readable UTF-8 JSON trace file.",
    "resource_limit": "Submit smaller evidence for one evaluation; evaluator resource ceilings cannot be raised by the trace or policy.",
    "protected_path": "Reject this trace: protected files were touched. Preserve independently authored tests and policy.",
    "outside_scope": "Reject this trace: changes exceeded the base policy's allowed paths.",
    "budget_exceeded": "Stop accepting further attempts in this bounded trace; record exhaustion without weakening the policy.",
}


class Unmeasurable(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str = "invalid_trace") -> None:
    if not condition:
        raise Unmeasurable(code)


def fields(value: object, names: tuple[str, ...], code: str = "invalid_trace") -> None:
    require(isinstance(value, dict) and set(value) == set(names), code)


def finite_fraction(value: Fraction) -> bool:
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def as_fraction(value: object) -> Fraction | None:
    """Exact value of a JSON number, or None when it is not a usable duration.

    Integers and Decimal-parsed fractional lexemes stay exact, so ceiling
    comparisons never depend on binary floating-point rounding. A number is
    usable only when finite, nonnegative and inside the float range the JSON
    report can emit; booleans, strings and nonfinite tokens are refused.
    """
    if type(value) is int:
        result = Fraction(value)
    elif type(value) is Decimal:
        if not value.is_finite():
            return None
        result = Fraction(value)
    elif type(value) is float:
        if not math.isfinite(value):
            return None
        result = Fraction(value)
    else:
        return None
    if result < 0 or not finite_fraction(result):
        return None
    return result


def number(value: object) -> bool:
    return as_fraction(value) is not None


def json_number(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


def immutable(value: object) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def literal_path(value: str) -> bool:
    return bool(value) and not value.startswith("/") and "\x00" not in value and all(
        part not in ("", ".", "..") for part in value.split("/")
    ) and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def load_json(raw: bytes, code: str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, code)
            result[key] = value
        return result

    def decimal(lexeme: str):
        try:
            return Decimal(lexeme)
        except ArithmeticError:
            # An exponent outside Decimal's range is still just an unusable
            # number; keep it nonfinite so validators refuse it structurally.
            return math.inf

    try:
        # Preserve nonfinite numeric tokens here so a duration gets its specific
        # invalid_duration verdict; validators reject them everywhere else too.
        # Fractional lexemes parse as Decimal so duration sums stay exact.
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_float=decimal)
    except (ValueError, UnicodeError, RecursionError, ArithmeticError):
        raise Unmeasurable(code) from None


class Budget:
    """Resource accounting for one evaluation, under RESOURCE_LIMITS."""

    def __init__(self, limits: dict):
        self.limits = limits
        self.started = time.monotonic()
        self.git_invocations = 0
        self.blob_bytes_total = 0

    def check_deadline(self) -> None:
        if time.monotonic() - self.started > self.limits["evaluation_seconds"]:
            raise Unmeasurable("resource_limit")

    def remaining_seconds(self) -> float:
        return self.limits["evaluation_seconds"] - (time.monotonic() - self.started)

    def read_trace(self, path: str) -> bytes:
        cap = self.limits["trace_bytes"]
        try:
            stream = open(path, "rb")
        except OSError:
            raise Unmeasurable("trace_unavailable") from None
        with stream:
            data = bytearray()
            try:
                # Read at most the remaining allowance plus one sentinel byte,
                # so an oversized trace is refused without being buffered.
                while len(data) <= cap:
                    chunk = stream.read(min(65536, cap + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                raise Unmeasurable("trace_unavailable") from None
        require(len(data) <= cap, "resource_limit")
        return bytes(data)


class Git:
    """Fixed read-only Git operations, without replacement refs or diff drivers."""

    def __init__(self, repo: str, budget: Budget):
        self.repo = repo
        self.budget = budget
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8",
        })
        self.commits = {}

    def run(self, *args: str, scratch: str | None = None, blob: bool = False) -> bytes:
        budget = self.budget
        limits = budget.limits
        budget.check_deadline()
        require(budget.git_invocations < limits["git_invocations"], "resource_limit")
        cap = limits["git_stdout_bytes"]
        if blob:
            # One immutable blob is bounded by its per-blob ceiling and by
            # what remains of the cumulative blob allowance, so an oversized
            # blob is refused before its bytes are captured or copied out.
            cap = min(cap, limits["blob_bytes"],
                      limits["total_blob_bytes"] - budget.blob_bytes_total)
        env = self.env if scratch is None else {**self.env, "GIT_CEILING_DIRECTORIES": scratch}
        budget.git_invocations += 1
        try:
            process = subprocess.Popen(
                ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
                 "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
                 "-c", "protocol.allow=never", "-C", scratch or self.repo, *args],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise Unmeasurable("git_unavailable") from None
        except OSError:
            raise Unmeasurable("git_failed") from None
        try:
            data = bytearray()
            stream = process.stdout
            read = stream.read1 if hasattr(stream, "read1") else stream.read
            with selectors.DefaultSelector() as selector:
                selector.register(stream, selectors.EVENT_READ)
                while len(data) <= cap:
                    # At most the remaining allowance plus one sentinel byte
                    # is ever read; silence beyond the per-operation wait (or
                    # the whole-evaluation deadline) stops the process.
                    if not selector.select(min(10.0, max(0.0, budget.remaining_seconds()))):
                        budget.check_deadline()
                        raise Unmeasurable("git_failed")
                    chunk = read(min(65536, cap + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
            require(len(data) <= cap, "resource_limit")
            try:
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                raise Unmeasurable("git_failed") from None
            require(process.returncode in ((0, 1) if scratch is not None else (0,)), "git_failed")
        finally:
            # Every path reaps the child and closes the pipe; an over-limit
            # or failed operation never leaves a process writing to nobody.
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        if blob:
            budget.blob_bytes_total += len(data)
        budget.check_deadline()
        return bytes(data)

    def commit(self, sha: str) -> tuple[str, list[str]]:
        require(immutable(sha), "invalid_sha")
        if sha not in self.commits:
            require(self.run("cat-file", "-t", sha) == b"commit\n", "git_failed")
            raw = self.run("cat-file", "commit", sha)
            lines = raw.split(b"\n\n", 1)[0].splitlines()
            # Git's graph grammar reads tree first, then the consecutive
            # parent headers that follow it. A parent header appearing after
            # author/committer is not a parent to Git, so it cannot prove
            # ancestry here either.
            require(bool(lines) and lines[0].startswith(b"tree "), "git_failed")
            try:
                tree = lines[0][5:].decode("ascii")
                parents = []
                for line in lines[1:]:
                    if not line.startswith(b"parent "):
                        break
                    parents.append(line[7:].decode("ascii"))
            except UnicodeError:
                raise Unmeasurable("git_failed") from None
            require(immutable(tree) and all(map(immutable, parents)), "git_failed")
            self.commits[sha] = (tree, parents)
        return self.commits[sha]

    def file_listing(self, sha: str) -> bytes:
        return self.run("ls-tree", "-r", "-z", "--full-tree", sha)

    def diff(self, parent: str, candidate: str) -> tuple[set[str], int]:
        args = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--text",
                "--diff-algorithm=myers", "--no-indent-heuristic", "--ignore-submodules=none",
                "--no-relative", "--src-prefix=a/", "--dst-prefix=b/")
        raw, blobs = parse_raw(self.run(*args, "--raw", "--no-abbrev", "-z", parent, candidate, "--"))
        names = parse_names(self.run(*args, "--name-status", "-z", parent, candidate, "--"))
        require(names == raw, "invalid_diff")
        lines = 0
        # Even --text leaves numstat subject to worktree/info attributes. Diff
        # immutable blob bytes in a disposable non-repository instead; neither
        # repository attributes nor local diff configuration can affect churn.
        try:
            with tempfile.TemporaryDirectory(prefix="compiler-feedback-blobs-") as scratch:
                for old_oid, new_oid in blobs.values():
                    before, after = (b"" if set(oid) == {"0"} else self.run("cat-file", "blob", oid, blob=True)
                                     for oid in (old_oid, new_oid))
                    require(b"\x00" not in before and b"\x00" not in after, "invalid_diff")
                    if before == after:
                        continue
                    Path(scratch, "before").write_bytes(before)
                    Path(scratch, "after").write_bytes(after)
                    counts = self.run(*args, "--no-index", "--numstat", "-z", "--", "before", "after",
                                      scratch=scratch)
                    lines += parse_blob_count(counts)
        except OSError:
            raise Unmeasurable("git_failed") from None
        return set(names), lines


def nul_tokens(raw: bytes) -> list[bytes]:
    if not raw:
        return []
    require(raw.endswith(b"\x00"), "invalid_diff")
    tokens = raw[:-1].split(b"\x00")
    require(all(tokens), "invalid_diff")
    return tokens


def path_token(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeError:
        raise Unmeasurable("invalid_diff") from None
    require(literal_path(path), "invalid_diff")
    return path


def parse_names(raw: bytes) -> dict[str, str]:
    tokens = nul_tokens(raw)
    require(len(tokens) % 2 == 0, "invalid_diff")
    result = {}
    for status, token in zip(tokens[::2], tokens[1::2]):
        require(status in (b"A", b"M", b"D"), "invalid_diff")
        path = path_token(token)
        require(path not in result, "invalid_diff")
        result[path] = status.decode("ascii")
    return result


def parse_blob_count(raw: bytes) -> int:
    match = re.fullmatch(rb"([0-9]+)\t([0-9]+)\t\x00before\x00after\x00", raw)
    require(match is not None, "invalid_diff")
    try:
        return int(match[1]) + int(match[2])
    except ValueError:
        raise Unmeasurable("invalid_diff") from None


def parse_raw(raw: bytes) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    tokens = nul_tokens(raw)
    require(len(tokens) % 2 == 0, "invalid_diff")
    result = {}
    blobs = {}
    for header, token in zip(tokens[::2], tokens[1::2]):
        match = re.fullmatch(rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}|[0-9a-f]{64}) "
                             rb"([0-9a-f]{40}|[0-9a-f]{64}) ([AMD])", header)
        require(match is not None, "invalid_diff")
        old_mode, new_mode, old_oid, new_oid, status = match.groups()
        if status == b"A":
            valid = old_mode == b"000000" and new_mode in (b"100644", b"100755") and set(old_oid) == {48}
        elif status == b"D":
            valid = new_mode == b"000000" and old_mode in (b"100644", b"100755") and set(new_oid) == {48}
        else:
            valid = old_mode == new_mode and old_mode in (b"100644", b"100755") and old_oid != new_oid
        require(valid, "invalid_diff")
        path = path_token(token)
        require(path not in result, "invalid_diff")
        result[path] = status.decode("ascii")
        blobs[path] = (old_oid.decode("ascii"), new_oid.decode("ascii"))
    return result, blobs


def policy_at(git: Git, base: str) -> dict:
    entry = git.run("ls-tree", "-z", base, "--", POLICY_PATH)
    require(re.fullmatch(rb"100(?:644|755) blob [0-9a-f]{40}(?:[0-9a-f]{24})?\t"
                         rb"\.compiler-feedback\.json\x00", entry) is not None, "invalid_policy")
    policy = load_json(git.run("cat-file", "blob", base + ":" + POLICY_PATH, blob=True), "invalid_policy")
    fields(policy, ("version", "budgets", "allowed_paths", "protected_paths"), "invalid_policy")
    require(type(policy["version"]) is int and policy["version"] == 1, "invalid_policy")
    fields(policy["budgets"], DIMENSIONS, "invalid_policy")
    for key, value in policy["budgets"].items():
        require(number(value) and (key == "elapsed_seconds" or type(value) is int), "invalid_policy")
    for key in ("allowed_paths", "protected_paths"):
        patterns = policy[key]
        require(isinstance(patterns, list) and (key != "allowed_paths" or bool(patterns)), "invalid_policy")
        require(all(isinstance(pattern, str) and literal_path(pattern) for pattern in patterns), "invalid_policy")
    return policy


def protected(path: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    return (path == POLICY_PATH or path.startswith((".github/", "scripts/quality-gates/")) or
            any(part in ("test", "tests") for part in path.split("/")[:-1]) or
            name.startswith("test_") or any(part in name for part in ("_test.", ".test.", ".spec.")) or
            any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns))


def compiler(record: object, sha: str, base: str, source: str,
             receipts: set[tuple[str, int]]) -> str:
    fields(record, ("sha", "policy_sha", "tool_sha", "status", "complete", "exit_code", "diagnostics", "actions"))
    require(record["sha"] == sha and record["policy_sha"] == base and record["tool_sha"] == base,
            "invalid_provenance")
    status = record["status"]
    diagnostics = record["diagnostics"]
    require(status in ("failed", "passed") and record["complete"] is True and
            type(record["exit_code"]) is int and isinstance(diagnostics, list) and
            all(isinstance(item, str) for item in diagnostics), "incomplete_compiler")
    require((status == "passed" and record["exit_code"] == 0 and not diagnostics) or
            (status == "failed" and record["exit_code"] > 0), "incomplete_compiler")
    receipt = record["actions"]
    if source == "fixture":
        require(receipt is None, "invalid_provenance")
    else:
        fields(receipt, ("run_id", "run_attempt", "head_sha", "conclusion"), "invalid_provenance")
        run = receipt["run_id"]
        attempt = receipt["run_attempt"]
        require(isinstance(run, str) and DECIMAL.fullmatch(run) is not None and bool(run.lstrip("0")) and
                type(attempt) is int and attempt > 0 and receipt["head_sha"] == sha and
                receipt["conclusion"] == ("success" if status == "passed" else "failure"), "invalid_provenance")
        pair = (run.lstrip("0"), attempt)
        require(pair not in receipts, "invalid_provenance")
        receipts.add(pair)
    return status


def initial_report() -> dict:
    return {
        "schema": "compiler-feedback-report.v1", "status": "unmeasurable",
        "code": "invalid_trace", "dimension": None,
        "consumed": dict.fromkeys(DIMENSIONS), "paths": [],
        "metrics": {key: {"attempts": 0, "elapsed_seconds": 0, "repairs": 0}
                    for key in ("fixture", "actions_reported", "actions_verified")},
        "evidence": {"source": None, "receipts_verified": False},
        "retryable": False, "next_action": ACTIONS["invalid_trace"],
    }


def evaluate(args: argparse.Namespace, report: dict) -> tuple[str, str, str | None]:
    require(immutable(args.base) and immutable(args.head), "invalid_sha")
    budget = Budget(RESOURCE_LIMITS)
    # Evidence is admitted before any Git work: the trace read is byte-bounded
    # and deadline-checked, and an over-long attempt list is refused before a
    # single Git process launches. Trace content errors are only raised after
    # the Git/policy stage below, preserving the established code precedence
    # for missing objects and invalid policies.
    raw = budget.read_trace(args.trace)
    budget.check_deadline()
    trace = None
    trace_error = None
    try:
        trace = load_json(raw, "invalid_trace")
    except Unmeasurable as exc:
        trace_error = exc
    if isinstance(trace, dict) and isinstance(trace.get("attempts"), list):
        require(len(trace["attempts"]) <= budget.limits["attempt_records"], "resource_limit")
    git = Git(args.repo, budget)
    base_tree, _ = git.commit(args.base)
    head_tree, _ = git.commit(args.head)
    policy = policy_at(git, args.base)
    if trace_error is not None:
        raise trace_error
    fields(trace, ("schema", "source", "base_sha", "head_sha", "policy_sha", "tool_sha", "before", "attempts"))
    require(trace["schema"] == "compiler-feedback-trace.v1" and trace["source"] in ("actions", "fixture"))
    source = trace["source"]
    report["evidence"]["source"] = source
    require(trace["base_sha"] == args.base and trace["head_sha"] == args.head and
            trace["policy_sha"] == args.base and trace["tool_sha"] == args.base, "invalid_provenance")
    report.update(base_sha=args.base, head_sha=args.head, policy_sha=args.base, tool_sha=args.base)
    attempts = trace["attempts"]
    require(isinstance(attempts, list) and bool(attempts))
    require(len(attempts) <= budget.limits["attempt_records"], "resource_limit")
    receipts = set()
    before = compiler(trace["before"], args.base, args.base, source, receipts)
    require(before == "failed" and any(item.strip() for item in trace["before"]["diagnostics"]), "no_failed_before")
    parent = args.base
    seconds = Fraction(0)
    paths = set()
    lines = 0
    for index, attempt in enumerate(attempts, 1):
        require(isinstance(attempt, dict))
        duration = as_fraction(attempt["elapsed_seconds"]) if "elapsed_seconds" in attempt else None
        require(duration is not None, "invalid_duration")
        fields(attempt, ("index", "parent_sha", "candidate_sha", "elapsed_seconds", "compiler"))
        require(type(attempt["index"]) is int and attempt["index"] == index and attempt["parent_sha"] == parent)
        candidate = attempt["candidate_sha"]
        _, parents = git.commit(candidate)
        require(parents == [parent])
        status = compiler(attempt["compiler"], candidate, args.base, source, receipts)
        require(status != "passed" or index == len(attempts))
        seconds += duration
        require(finite_fraction(seconds), "invalid_duration")
        touched, churn = git.diff(parent, candidate)
        paths.update(touched)
        lines += churn
        parent = candidate
    require(parent == args.head, "invalid_provenance")
    exact = dict(zip(DIMENSIONS, (len(attempts), seconds, len(paths), lines)))
    report["consumed"] = {**exact, "elapsed_seconds": json_number(seconds)}
    report["paths"] = sorted(paths)
    bucket = report["metrics"]["fixture" if source == "fixture" else "actions_reported"]
    bucket.update(attempts=len(attempts), elapsed_seconds=json_number(seconds))
    # Scope is immutable and validated at every step, including reverted edits.
    if any(protected(path, policy["protected_paths"]) for path in paths):
        return "exhausted", "protected_path", "protected_paths"
    if any(not any(fnmatch.fnmatchcase(path, pattern) for pattern in policy["allowed_paths"]) for path in paths):
        return "exhausted", "outside_scope", "allowed_paths"
    for dimension in DIMENSIONS:
        consumed = exact[dimension]
        limit = as_fraction(policy["budgets"][dimension])
        if consumed > limit or (status == "failed" and consumed == limit):
            return "exhausted", "budget_exceeded", dimension
    # A different tree ID is not proof of a source change: an added empty
    # subtree changes no file blob. Require the recursive file listing at
    # HEAD to differ from BASE, so only an actual file change can repair.
    require(base_tree != head_tree and git.file_listing(args.base) != git.file_listing(args.head),
            "no_repair_change")
    require(status == "passed", "incomplete_trace")
    require(source != "fixture", "fixture_only")
    bucket["repairs"] = 1
    return "repaired", "repaired", None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--trace", required=True)
    args = parser.parse_args(argv)
    report = initial_report()
    try:
        status, code, dimension = evaluate(args, report)
    except Unmeasurable as exc:
        status, code, dimension = "unmeasurable", exc.code, None
        if code == "invalid_duration":
            report["consumed"]["elapsed_seconds"] = None
    report.update(status=status, code=code, dimension=dimension,
                  retryable=code in ("git_failed", "git_unavailable", "trace_unavailable", "incomplete_trace"),
                  next_action="" if status == "repaired" else ACTIONS[code])
    print(json.dumps(report, ensure_ascii=True, allow_nan=False, sort_keys=True))
    return {"repaired": 0, "exhausted": 1, "unmeasurable": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
