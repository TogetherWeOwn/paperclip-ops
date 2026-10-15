#!/usr/bin/env python3
"""Scorer-side preflight only. The uncontained v4 issue dispatcher is retired.

A passed byte/manifest gate is not permission to launch a reviewer. No issue
creation or network call exists here; a sanctioned contained executor and an
independent re-check are required before evaluation resumes.

Usage: dispatch.py --cid C-10 --models claude-sonnet-5-5,glm-5.3 --dry-run
"""
import argparse
import hashlib
import json
import os
import sys

import stage

SCORER = stage.SCORER
ARENA = stage.ARENA
LEDGER = SCORER + "/evidence/dispatch_ledger.jsonl"
TITLE = "Pre-merge code review"

# Same organic review task; transport/governance instructions are not its prompt.
PROMPT = """You are reviewing one pull request as the independent reviewer. You did not write it.

Inputs, all read-only, in the review directory:
- `reviewer_view.json`: the PR title and description.
- `pr.diff`: the change under review (base to head).
- `tree/`: the repository checked out at the PR head. Use Read, Grep and Glob to follow the
  change into code it calls or is called by. Do not run anything.

Decide whether this change is safe to merge. Look for defects a careful maintainer would
block on: wrong or inverted logic, missing or weakened checks, security and data-exposure
problems, error handling that hides failure, off-by-one and boundary mistakes, units, state
and concurrency mistakes, and behaviour that contradicts the PR's own description.

Rules:
- Report only defects you can point to in the code. Cite the file and the line in `tree/`.
- Severity: P0 = data loss, security breach or outage; P1 = incorrect behaviour a user or
  operator will hit; P2 = minor or latent. Do not report style, naming or missing tests.
- If the change is sound, say so with an empty findings list. A clean review is a valid answer.
- Do not invent problems to look thorough.

Finish with one JSON object and nothing after it:

{"verdict": "APPROVE" | "CHANGES",
 "findings": [{"file": "<path in tree/>", "line": <int>, "severity": "P0"|"P1"|"P2",
               "title": "<one line>", "rationale": "<why it is wrong, one short paragraph>"}]}

`verdict` is CHANGES when any finding is P0 or P1, otherwise APPROVE."""

DESC = """Closed-book review exercise. Reference: {ref}

Review directory (read-only): {arena}/{cid}/

How to work:
- Use only Read, Grep and Glob on files under the review directory. No memory recall, retrieval, network, shell or other tool. Do not open any path outside the review directory.
- Do not build, run or test anything.

{prompt}

Return your result only. Do not call an API to post it; the controller persists it after the reviewer session ends, with no response fed back to the reviewer.
"""


def gate(cid, text):
    num = stage.MAP[cid]
    hits = stage.screen_text(text, stage.TREE_GLOBAL + stage.own_patterns(num))
    with open(stage.GREPTILE) as source:
        data = json.load(source)
    pr = next(pr for pr in data["prs"] if pr["number"] == num)
    return hits + stage.screen_text(text, stage.finding_phrases(pr), fixed=True)


def done_pairs():
    if not os.path.exists(LEDGER):
        return set()
    with open(LEDGER) as source:
        return {(j["cid"], j["model"]) for j in map(json.loads, source) if j.get("ok")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cid", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        sys.exit("HELD: v4 launch path retired; contained executor and re-check required")
    try:
        stage.verify_bundle(args.cid)
    except stage.GateError as e:
        sys.exit("GATE FAIL: " + str(e))
    description = DESC.format(arena=ARENA, cid=args.cid, prompt=PROMPT, ref="R-" + os.urandom(4).hex())
    if gate(args.cid, TITLE + "\n" + description):
        sys.exit("GATE FAIL: held-out strings in reviewer inputs")
    prompt_sha = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]
    dispatched = done_pairs()
    for model in args.models.split(","):
        if (args.cid, model) in dispatched:
            print(f"SKIP {args.cid} x {model}: already dispatched")
        else:
            print(f"DRY {args.cid} x {model} byte_gate=clean prompt_sha={prompt_sha} launch=HELD")


if __name__ == "__main__":
    main()
