#!/usr/bin/env python3
"""Read-only, diff-scoped documentation accompaniment check; stdout is JSON."""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

POLICY = ".docs-drift.json"
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


class Unmeasurable(Exception):
    def __init__(self, code, next_action, retryable=False):
        self.code = code
        self.next_action = next_action
        self.retryable = retryable


def git(repo, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, timeout=30,
            env={**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                 "GIT_NO_REPLACE_OBJECTS": "1", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull},
        )
    except subprocess.TimeoutExpired as exc:
        raise Unmeasurable("git_timeout", "Retry on a healthy runner with complete local history.", retryable=True) from exc
    except OSError as exc:
        raise Unmeasurable("git_unavailable", "Install Git and select a readable repository.") from exc
    if result.returncode:
        raise Unmeasurable("git_failed", "Fetch both commits and their common history; rerun with full SHAs.")
    return result.stdout


def policy_at(repo, base):
    entry = git(repo, "ls-tree", "-z", base, "--", POLICY)
    if not entry:
        return None
    if not entry.startswith(b"100644 blob "):
        raise Unmeasurable("invalid_policy", "Commit a regular, non-executable .docs-drift.json file.")
    raw = git(repo, "show", base + ":" + POLICY)
    if len(raw) > 65536:
        raise Unmeasurable("invalid_policy", "Limit .docs-drift.json to 64 KiB.")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "rules"} or type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("schema")
        rules = value["rules"]
        if not isinstance(rules, list) or not 1 <= len(rules) <= 100:
            raise ValueError("rules")
        ids = set()
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {"id", "sources", "docs"}:
                raise ValueError("rule")
            name = rule["id"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) or name in ids:
                raise ValueError("id")
            ids.add(name)
            for key in ("sources", "docs"):
                patterns = rule[key]
                if not isinstance(patterns, list) or not 1 <= len(patterns) <= 50:
                    raise ValueError("patterns")
                for pattern in patterns:
                    if not isinstance(pattern, str) or not pattern or len(pattern) > 256 or pattern.startswith("/") or any(p in ("", ".", "..") for p in pattern.split("/")) or "\x00" in pattern:
                        raise ValueError("pattern")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Unmeasurable("invalid_policy", "Use the documented version-1 policy schema; fix the base policy in a reviewed PR.") from exc
    return rules


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def check(repo, base, head, explain=False):
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        raise Unmeasurable("invalid_sha", "Provide full lowercase 40- or 64-character commit SHAs.")
    for sha in (base, head):
        git(repo, "cat-file", "-e", sha + "^{commit}")
    bases = git(repo, "merge-base", "--all", base, head).decode("ascii").splitlines()
    if len(bases) != 1:
        raise Unmeasurable("ambiguous_base", "Reconcile the branch history to one merge base before checking.")
    merge_base = bases[0]
    # No rename heuristics: both sides of every rename are checked, even for
    # unusual path bytes. Never split Git's quoted, newline-delimited output.
    raw = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", merge_base, head, "--")
    parts = raw.split(b"\0")
    if parts[-1] != b"" or (len(parts) - 1) % 2:
        raise Unmeasurable("invalid_diff", "Inspect Git's NUL-delimited name-status output.")
    changes = [(os.fsdecode(parts[i]), os.fsdecode(parts[i + 1])) for i in range(0, len(parts) - 1, 2)]
    if len(changes) > 100000 or any(status not in {"A", "M", "D", "T"} for status, _ in changes):
        raise Unmeasurable("invalid_diff", "Reduce the diff below 100000 paths and resolve unsupported statuses.")
    # The target branch policy is authoritative, even when this PR deletes or
    # weakens its own policy. New policies take effect only after they merge.
    rules = policy_at(repo, base)
    report = {
        "schema": "docs-drift.v1", "base_sha": base, "head_sha": head,
        "merge_base_sha": merge_base, "policy_sha": base,
        "changed_paths": len(changes), "checked_rules": 0, "findings": [],
        "status": "not_configured" if rules is None else "clean",
    }
    if rules is None:
        if explain:
            report["explanations"] = []
        return report
    doc_candidates = {path for status, path in changes if status in {"A", "M"}}
    # A symlink/submodule or chmod-only change is not documentation evidence.
    docs = set()
    wanted = {path for path in doc_candidates if any(matches(path, rule["docs"]) for rule in rules)}
    if wanted:
        blobs = []
        for sha in (merge_base, head):
            tree = git(repo, "ls-tree", "-r", "-z", sha)
            blobs.append({os.fsdecode(row.split(b"\t", 1)[1]): row.split(b"\t", 1)[0].split()[2]
                          for row in tree.split(b"\0")
                          if row.startswith(b"100644 blob ") or row.startswith(b"100755 blob ")})
        before, after = blobs
        docs = {path for path in wanted & after.keys() if before.get(path) != after[path]}
    explanations = []
    for rule in rules:
        sources = sorted({path for _, path in changes if matches(path, rule["sources"])})
        if not sources:
            continue
        report["checked_rules"] += 1
        # One evaluated set per rule: the verdict and the explanation both
        # read this list, so an explanation can never disagree with a finding.
        qualifying = sorted(path for path in docs
                            if matches(path, rule["docs"]) and not matches(path, rule["sources"]))
        explanations.append({
            "rule": rule["id"], "matched_source_paths": sources,
            "qualifying_doc_paths": qualifying,
            "status": "satisfied" if qualifying else "missing",
        })
        if not qualifying:
            report["findings"].append({
                "code": "docs_required", "rule": rule["id"], "paths": sources,
                "docs": rule["docs"], "retryable": False,
                "next_action": "Add or edit a separate mapped documentation file in this patch.",
            })
    if report["findings"]:
        report["status"] = "finding"
    if explain:
        report["explanations"] = explanations
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="example: docs_drift.py --repo . --base <full-target-sha> --head <full-pr-head-sha> --explain")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True, help="full target branch commit SHA (policy snapshot)")
    parser.add_argument("--head", required=True, help="full PR head commit SHA")
    parser.add_argument("--explain", action="store_true",
                        help="also report, per triggered rule, the matched source paths and qualifying changed doc paths")
    args = parser.parse_args(argv)
    try:
        report = check(args.repo, args.base, args.head, explain=args.explain)
    except Unmeasurable as exc:
        print(json.dumps({"schema": "docs-drift.v1", "status": "unmeasurable", "code": exc.code,
                          "retryable": exc.retryable, "next_action": exc.next_action}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 1 if report["status"] == "finding" else 0


if __name__ == "__main__":
    sys.exit(main())
