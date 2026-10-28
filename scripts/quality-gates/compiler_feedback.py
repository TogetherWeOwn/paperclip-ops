#!/usr/bin/env python3
"""Evaluate bounded compiler-feedback evidence offline; never execute a repair.

Policy is read from the base commit. Supplied Actions receipts remain reported,
not authenticated, evidence. See docs/compiler-feedback.md for the v1 contract.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


POLICY_PATH = ".compiler-feedback.json"
DIMENSIONS = ("attempts", "elapsed_seconds", "changed_paths", "changed_lines")
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DECIMAL = re.compile(r"[0-9]+\Z")
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


def number(value: object) -> bool:
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


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

    try:
        # Preserve nonfinite numeric tokens here so a duration gets its specific
        # invalid_duration verdict; validators reject them everywhere else too.
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise Unmeasurable(code) from None


class Git:
    """Fixed read-only Git operations, without replacement refs or diff drivers."""

    def __init__(self, repo: str):
        self.repo = repo
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8",
        })
        self.commits = {}

    def run(self, *args: str, scratch: str | None = None) -> bytes:
        env = self.env if scratch is None else {**self.env, "GIT_CEILING_DIRECTORIES": scratch}
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
                 "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
                 "-c", "protocol.allow=never", "-C", scratch or self.repo, *args],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=False,
            )
        except FileNotFoundError:
            raise Unmeasurable("git_unavailable") from None
        except (OSError, subprocess.TimeoutExpired):
            raise Unmeasurable("git_failed") from None
        require(result.returncode in ((0, 1) if scratch is not None else (0,)), "git_failed")
        return result.stdout

    def commit(self, sha: str) -> tuple[str, list[str]]:
        require(immutable(sha), "invalid_sha")
        if sha not in self.commits:
            require(self.run("cat-file", "-t", sha) == b"commit\n", "git_failed")
            raw = self.run("cat-file", "commit", sha)
            try:
                # Author names may be Unicode; only tree/parent headers matter.
                headers = [line.decode("ascii") for line in raw.split(b"\n\n", 1)[0].splitlines()
                           if line.startswith((b"tree ", b"parent "))]
            except UnicodeError:
                raise Unmeasurable("git_failed") from None
            trees = [line[5:] for line in headers if line.startswith("tree ")]
            parents = [line[7:] for line in headers if line.startswith("parent ")]
            require(len(trees) == 1 and immutable(trees[0]) and all(map(immutable, parents)), "git_failed")
            self.commits[sha] = (trees[0], parents)
        return self.commits[sha]

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
                    before, after = (b"" if set(oid) == {"0"} else self.run("cat-file", "blob", oid)
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
    policy = load_json(git.run("cat-file", "blob", base + ":" + POLICY_PATH), "invalid_policy")
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
    git = Git(args.repo)
    base_tree, _ = git.commit(args.base)
    head_tree, _ = git.commit(args.head)
    policy = policy_at(git, args.base)
    try:
        raw = Path(args.trace).read_bytes()
    except OSError:
        raise Unmeasurable("trace_unavailable") from None
    trace = load_json(raw, "invalid_trace")
    fields(trace, ("schema", "source", "base_sha", "head_sha", "policy_sha", "tool_sha", "before", "attempts"))
    require(trace["schema"] == "compiler-feedback-trace.v1" and trace["source"] in ("actions", "fixture"))
    source = trace["source"]
    report["evidence"]["source"] = source
    require(trace["base_sha"] == args.base and trace["head_sha"] == args.head and
            trace["policy_sha"] == args.base and trace["tool_sha"] == args.base, "invalid_provenance")
    report.update(base_sha=args.base, head_sha=args.head, policy_sha=args.base, tool_sha=args.base)
    attempts = trace["attempts"]
    require(isinstance(attempts, list) and bool(attempts))
    receipts = set()
    before = compiler(trace["before"], args.base, args.base, source, receipts)
    require(before == "failed" and any(item.strip() for item in trace["before"]["diagnostics"]), "no_failed_before")
    parent = args.base
    seconds = 0
    paths = set()
    lines = 0
    for index, attempt in enumerate(attempts, 1):
        require(isinstance(attempt, dict))
        require("elapsed_seconds" in attempt and number(attempt["elapsed_seconds"]), "invalid_duration")
        fields(attempt, ("index", "parent_sha", "candidate_sha", "elapsed_seconds", "compiler"))
        require(type(attempt["index"]) is int and attempt["index"] == index and attempt["parent_sha"] == parent)
        candidate = attempt["candidate_sha"]
        _, parents = git.commit(candidate)
        require(parents == [parent])
        status = compiler(attempt["compiler"], candidate, args.base, source, receipts)
        require(status != "passed" or index == len(attempts))
        try:
            seconds += attempt["elapsed_seconds"]
            require(number(seconds), "invalid_duration")
        except OverflowError:
            raise Unmeasurable("invalid_duration") from None
        touched, churn = git.diff(parent, candidate)
        paths.update(touched)
        lines += churn
        parent = candidate
    require(parent == args.head, "invalid_provenance")
    report["consumed"] = dict(zip(DIMENSIONS, (len(attempts), seconds, len(paths), lines)))
    report["paths"] = sorted(paths)
    bucket = report["metrics"]["fixture" if source == "fixture" else "actions_reported"]
    bucket.update(attempts=len(attempts), elapsed_seconds=seconds)
    # Scope is immutable and validated at every step, including reverted edits.
    if any(protected(path, policy["protected_paths"]) for path in paths):
        return "exhausted", "protected_path", "protected_paths"
    if any(not any(fnmatch.fnmatchcase(path, pattern) for pattern in policy["allowed_paths"]) for path in paths):
        return "exhausted", "outside_scope", "allowed_paths"
    for dimension in DIMENSIONS:
        consumed = report["consumed"][dimension]
        limit = policy["budgets"][dimension]
        if consumed > limit or (status == "failed" and consumed == limit):
            return "exhausted", "budget_exceeded", dimension
    require(base_tree != head_tree, "no_repair_change")
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
