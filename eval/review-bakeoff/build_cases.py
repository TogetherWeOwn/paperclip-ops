#!/usr/bin/env python3
"""Assemble dataset/cases.json from labeller output, SZZ pairs and planted cases.

  python3 build_cases.py --repo CLONE --pairs pairs.json --verify verify-0.json verify-2.json \\
      --planted planted/planted-cases.json --out dataset/cases.json --decisions dataset/labels/decisions.json

Every accepted label is re-checked against the repository: the defect's file must
exist in the introducing commit and its lines must lie inside the file. Internal
ticket keys (TOG-nnn) are stripped from titles and bodies and the build FAILS if
one survives anywhere in the output, so the published dataset stays clean.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

TICKET = re.compile(r"\bTOG-\d+\b")
PR_SUFFIX = re.compile(r"\s*\(#\d+\)\s*$")
SEVERITIES = {"critical", "major", "minor"}


def git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def scrub(text: str) -> str:
    """Drop whole lines that mention a ticket key (e.g. `Refs: case_8c5fe4cc`), then any stray key."""
    lines = [ln for ln in text.splitlines() if not TICKET.search(ln)]
    return TICKET.sub("", "\n".join(lines)).strip()


def check_defect(repo: str, sha: str, defect: dict) -> list[str]:
    problems = []
    for key in ("id", "file", "line_start", "line_end", "severity", "description"):
        if key not in defect:
            problems.append(f"missing {key}")
    if problems:
        return problems
    if defect["severity"] not in SEVERITIES:
        problems.append(f"bad severity {defect['severity']!r}")
    if not (isinstance(defect["line_start"], int) and isinstance(defect["line_end"], int)
            and 1 <= defect["line_start"] <= defect["line_end"]):
        problems.append("bad line range")
        return problems
    try:
        content = git(repo, "show", f"{sha}:{defect['file']}")
    except RuntimeError:
        problems.append(f"file {defect['file']} not in {sha[:9]}")
        return problems
    if defect["line_end"] > len(content.splitlines()):
        problems.append(f"line_end {defect['line_end']} beyond end of {defect['file']}")
    return problems


def build(repo: str, pairs: list[dict], verdicts: list[dict], planted: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    by_pair = {(p["introducing_pr"], p["fix_pr"]): p for p in pairs}
    cases, decisions, errors = [], [], []
    for v in verdicts:
        key = (v["introducing_pr"], v["fix_pr"])
        pair = by_pair.get(key)
        if pair is None:
            errors.append(f"verdict for unknown pair {key}")
            continue
        decisions.append({k: v.get(k) for k in ("shuffle_rank", "introducing_pr", "fix_pr", "verdict", "reason")})
        if v["verdict"] != "accept":
            continue
        sha = pair["introducing_sha"]
        for d in v["defects"]:
            for problem in check_defect(repo, sha, d):
                errors.append(f"r-{v['introducing_pr']:04d}/{d.get('id')}: {problem}")
        cases.append({
            "id": f"r-{v['introducing_pr']:04d}",
            "kind": "real",
            "category": pair["category"],
            "repo": "TogetherWeOwn/two-web-next",
            "head_sha": sha,
            "introducing_pr": v["introducing_pr"],
            "fix_pr": v["fix_pr"],
            "title": scrub(PR_SUFFIX.sub("", pair["introducing_subject"])),
            "body": scrub(git(repo, "show", "-s", "--format=%b", sha)),
            "defects": v["defects"],
            "reviewer_hint": scrub(v.get("reviewer_hint", "")),
        })
    for p in planted:
        for d in p["defects"]:
            if d.get("severity") not in SEVERITIES:
                errors.append(f"{p['id']}/{d.get('id')}: bad severity")
        cases.append({**p, "title": scrub(p["title"]), "body": scrub(p.get("body", ""))})
    ids = [c["id"] for c in cases]
    for dup in {i for i in ids if ids.count(i) > 1}:
        errors.append(f"duplicate case id {dup}")
    blob = json.dumps(cases)
    if TICKET.search(blob):
        errors.append("a ticket key survived scrubbing")
    return cases, decisions, errors


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--verify", nargs="+", required=True)
    ap.add_argument("--planted")
    ap.add_argument("--out", required=True)
    ap.add_argument("--decisions", required=True)
    args = ap.parse_args(argv)
    pairs = json.load(open(args.pairs, encoding="utf-8"))
    verdicts = [v for path in args.verify for v in json.load(open(path, encoding="utf-8"))]
    planted = json.load(open(args.planted, encoding="utf-8")) if args.planted else []
    cases, decisions, errors = build(args.repo, pairs, verdicts, planted)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"cases": cases}, fh, indent=2)
        fh.write("\n")
    with open(args.decisions, "w", encoding="utf-8") as fh:
        json.dump(decisions, fh, indent=2)
        fh.write("\n")
    real = sum(1 for c in cases if c["kind"] == "real")
    print(f"{len(cases)} cases ({real} real, {len(cases) - real} planted), "
          f"{sum(len(c['defects']) for c in cases)} defects; {len(decisions)} decisions recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
