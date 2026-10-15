#!/usr/bin/env python3
"""Score a deterministic new-test-file CI wiring heuristic.

For an explicit-step-per-suite workflow, flag newly added tests not named in
added workflow lines or covered by known discover/package conventions.
This is an evaluation heuristic, not proof that a suite actually executed.
Provide labels and cached diffs explicitly; no private evaluation data is bundled.
"""

import argparse
import fnmatch
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "review_tier1"))

from corpus import parse_diff  # noqa: E402
from eval import score  # noqa: E402

TEST_FILE = re.compile(r"(^|/)(test_[\w-]+\.(sh|py|mjs)|[\w-]+\.test\.(mjs|js|ts))$")
WORKFLOW = re.compile(r"^\.github/workflows/[\w.-]+\.ya?ml$")


def added_text(info):
    return "\n".join(line[1:] for line in info["patch"].splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


_DISCOVER = re.compile(r"unittest discover\s+-s\s+(\S+)\s+-p\s+['\"]([^'\"]+)['\"]")


def discover_globs(workflow_text):
    """[(dir, pattern)] from `unittest discover -s DIR -p 'PAT'` steps."""
    return _DISCOVER.findall(workflow_text or "")


def covered_by_glob(path, globs):
    """Model existing discover patterns and nested JS package conventions.

    The nested *.test.mjs exemption applies with or without --workflow;
    --workflow only adds the repository's unittest discover globs.
    """
    for directory, pattern in globs:
        if path.startswith(directory.rstrip("/") + "/") and fnmatch.fnmatch(os.path.basename(path), pattern):
            return True
    # a nested package's *.test.mjs is wired by that package's `npm test` step
    return path.count("/") >= 2 and path.endswith(".test.mjs")


def unwired_tests(files, globs=()):
    """Added test files not named by an added workflow line. Returns [path]."""
    workflow_text = "\n".join(added_text(i) for p, i in files.items() if WORKFLOW.match(p))
    out = []
    for path, info in sorted(files.items()):
        if info["status"] != "added" or not TEST_FILE.search(path):
            continue
        base = os.path.basename(path)
        stem = base.rsplit(".", 1)[0]
        if base in workflow_text or re.search(r"\b" + re.escape(stem) + r"\b", workflow_text):
            continue
        if covered_by_glob(path, globs):
            continue
        out.append(path)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headlines", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", help="current ci.yml; its discover globs count as wiring "
                        "(tuned on this eval set, so the resulting precision is optimistic)")
    args = parser.parse_args()
    globs = []
    if args.workflow:
        with open(args.workflow, encoding="utf-8") as handle:
            globs = discover_globs(handle.read())
    with open(args.headlines, encoding="utf-8") as handle:
        rows = json.load(handle)
    gold = {(r["pr"], r["commit"]) for r in rows
            if r["repo"] == args.repo and r["check"] == "ci_wiring" and not r["carry_over"]}
    base_dir = os.path.join(args.cache, args.repo.replace("/", "__"))
    heads = set(gold)
    for entry in sorted(os.listdir(base_dir)) if os.path.isdir(base_dir) else []:
        meta_path = os.path.join(base_dir, entry, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as handle:
                heads.add((int(entry), json.load(handle)["final_head"]))
    scored, rows_out, misses, fps = 0, [], [], []
    for pr, sha in sorted(heads):
        path = os.path.join(base_dir, str(pr), f"{sha}.diff")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            hits = unwired_tests(parse_diff(handle.read()), globs)
        is_pos = (pr, sha) in gold
        scored += 1
        rows_out.append({"check": "ci_wiring", "provider": "deterministic",
                         "gold": "hold" if is_pos else "none",
                         "verdict": "hold" if hits else "none", "cost": 0.0,
                         "pr": f"{args.repo}#{pr}"})
        if is_pos and not hits:
            misses.append((pr, sha[:8]))
        if hits and not is_pos:
            fps.append((pr, sha[:8], hits))
    cell = score(rows_out)["ci_wiring"]["deterministic"]
    print(f"{args.repo}: {scored} heads scored, {cell['tp'] + cell['fn']} positive heads")
    print(f"  flagged {cell['flagged']}  TP {cell['tp']}  FP {cell['fp']}  FN {cell['fn']}")
    print(f"  precision {cell['precision']:.2f} (95% CI {cell['precision_ci'][0]:.2f}-{cell['precision_ci'][1]:.2f})"
          f"  recall {cell['recall']:.2f} (95% CI {cell['recall_ci'][0]:.2f}-{cell['recall_ci'][1]:.2f})")
    print("  missed positives:", misses)
    print("  false positives:", fps)


if __name__ == "__main__":
    main()
