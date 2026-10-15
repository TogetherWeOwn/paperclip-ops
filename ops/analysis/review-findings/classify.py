#!/usr/bin/env python3
"""Assign Tier-1 buckets to explicitly supplied review findings.

Rules first (deterministic), then a reviewed override table
(`label-overrides.json`: {comment_id: {"bucket": ..., "subtype": ...}}). Every
row records its label_source so the eval can report rule-vs-override rates.
No harvested private findings or labels are included with this source.

Buckets:
  B  doc/comment statement is false against code or another changed doc
  J  PR description or commit message claims something the diff does not do
  K  test-evidence gap (no test, vacuous test, suite not wired into CI)
  O  everything else (code defects) - not addressable by the Tier-1 checks
"""

import argparse
import json
import re
from collections import Counter

DOC_PATH = re.compile(
    r"(\.mdx?$|(^|/)docs?/|(^|/)(README|RUNBOOK|OPERATOR|INSTALL|MODULES)\b"
    r"|\.example\.ya?ml$)")
J_TEXT = re.compile(
    r"\b(PR|pull request) (description|body|text)\b|\bcommit message\b"
    r"|\b(PR|description) and (the )?commit\b|\bthe PR (says|claims|states)\b",
    re.I)
CI_WIRING = re.compile(
    r"\bCI (never|does not|doesn't|will not) (run|execute)"
    r"|\bnever (run|executed) (by|in) (CI|any CI)\b"
    r"|\bnot wired into (CI|ci\.yml)"
    r"|\bno step in \.github/workflows"
    r"|\bnothing in CI (runs|calls)"
    r"|\b(never|not) collected by CI|\bnot part of any (vitest|test|pytest) project",
    re.I)
TEST_GAP = re.compile(
    r"\bno tests?\b(?! (is|are) needed)|\b(is|are) never (run|executed|exercised)"
    r"|\bnever run by any test|\basserts nothing\b|\bvacuous\b"
    r"|\bcannot catch\b|\bcan't catch\b|\bnot covered by (any )?tests?"
    r"|\buntested\b|\bno test (covers|pins|exercises)\b|\bpasses? for the wrong reason"
    r"|\bleaves every suite green|\bnothing now shows"
    r"|\b(not|never) pinned by (a|any) tests?\b|\btests? (still )?pass(es)? without\b"
    r"|\bno regression test\b|\b(suite|tests?) stays? green\b"
    r"|\bdeleting (it|any one|the \w+).{0,40}(still )?passes?\b"
    r"|\bnever runs the (controller|code|function|script)\b", re.I)
DOC_CLAIM = re.compile(
    r"\b(comment|header|docstring|doc|docs|documentation|runbook|readme|note|"
    r"table|record|page)\b.{0,80}\b(says|claims|states|contradicts?|stale|"
    r"wrong|no longer|still (says|describes|documents|lists)|does not match|"
    r"doesn'?t match|backwards|false)\b", re.I)
DOC_CLAIM_B = re.compile(
    r"\bdocs?\b.{0,60}\b(still describe|misdescribe|not true)\b"
    r"|\bdocumented (here|key|usage|behaviou?r)\b.{0,40}\b(wrong|false|fails)\b"
    r"|\b(list )?header\b.{0,60}\b(says|tells)\b|\bguarantee is not true\b"
    r"|\bmisdescribe", re.I)


CARRY_OVER = re.compile(
    r"^(Still (open|present)|This item from the prior review is still open"
    r"|This is the earlier finding)", re.I)


def severity_gold(severity):
    return "block" if severity == "error" else "hold"


def rule_label(row):
    head = row["headline"]
    text = row["text"][:700]
    path = row["path"]
    if CI_WIRING.search(head) or CI_WIRING.search(text[:300]):
        return "K", "ci_wiring"
    if J_TEXT.search(head) or J_TEXT.search(text[:300]):
        return "J", "pr_claim"
    if TEST_GAP.search(head):
        return "K", "test_gap"
    if DOC_PATH.search(path):
        return "B", "doc_path"
    if DOC_CLAIM.search(head) or DOC_CLAIM_B.search(head):
        return "B", "comment_claim"
    return "O", "code_defect"


CHECK_OF = {"B": "docs_vs_diff", "J": "claims_vs_diff",
            "K": "ac_evidence", "O": "none"}
# B subtypes a docs-vs-diff judge cannot decide from doc + diff alone
B_OUT_OF_SCOPE = {"doc_instruction_defect", "doc_gap_outside_diff"}


def classify(rows, overrides):
    out = []
    for row in rows:
        bucket, subtype = rule_label(row)
        source = "rule"
        override = overrides.get(str(row["id"]))
        if override:
            bucket = override["bucket"]
            subtype = override.get("subtype", subtype)
            source = "override"
        check = CHECK_OF[bucket]
        if bucket == "B" and subtype in B_OUT_OF_SCOPE:
            check = "none"
        # deterministic Tier-0 candidates are tracked, not scored as a model check
        if subtype == "ci_wiring":
            check = "ci_wiring"
        gold = severity_gold(row["severity"]) if bucket != "O" else "none"
        out.append({
            "id": row["id"], "repo": row["repo"], "private": row["private"],
            "pr": row["pr"], "commit": row["commit"], "path": row["path"],
            "line": row["line"], "severity": row["severity"],
            "category": row["category"], "headline": row["headline"],
            "bucket": bucket, "subtype": subtype, "check": check,
            "gold": gold, "label_source": source,
            "carry_over": bool(CARRY_OVER.search(row["headline"])),
            "location": "doc" if DOC_PATH.search(row["path"]) else "code",
        })
    return out


def summarize(rows):
    by_bucket = Counter(r["bucket"] for r in rows)
    by_subtype = Counter((r["bucket"], r["subtype"]) for r in rows)
    total = len(rows)
    lines = [f"{total} findings"]
    for bucket in ("B", "J", "K", "O"):
        lines.append(f"  {bucket}: {by_bucket[bucket]} "
                     f"({100 * by_bucket[bucket] / total:.0f}%)")
    for (bucket, subtype), count in sorted(by_subtype.items()):
        lines.append(f"    {bucket}/{subtype}: {count}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default="raw-20261009.json")
    parser.add_argument("--overrides", default="label-overrides.json")
    parser.add_argument("--out", default="reviewer-headlines.json")
    parser.add_argument("--labels-out", default="labels-20261009.json",
                        help="compact label file: in-scope rows only, no finding text")
    args = parser.parse_args()
    with open(args.raw, encoding="utf-8") as handle:
        raw = json.load(handle)["findings"]
    try:
        with open(args.overrides, encoding="utf-8") as handle:
            overrides = json.load(handle)
    except FileNotFoundError:
        overrides = {}
    rows = classify(raw, overrides)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=1, sort_keys=True)
        handle.write("\n")
    keep = ("id", "repo", "pr", "commit", "path", "line", "severity", "bucket",
            "subtype", "check", "gold", "carry_over", "location", "label_source")
    kept = [{k: row[k] for k in keep} for row in sorted(rows, key=lambda r: r["id"])
            if row["bucket"] != "O"]
    with open(args.labels_out, "w", encoding="utf-8") as handle:
        handle.write("[\n" + ",\n".join(
            json.dumps(row, sort_keys=True, separators=(",", ":"))
            for row in kept) + "\n]\n")
    print(summarize(rows))


if __name__ == "__main__":
    main()
