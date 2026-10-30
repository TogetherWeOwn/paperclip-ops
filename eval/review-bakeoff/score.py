#!/usr/bin/env python3
"""Score bake-off results against ground truth and print one table per model.

  python3 score.py --cases dataset/cases.json --results results/ [--judgments judgments.json]

Matching is deterministic and location-based: a finding matches a defect when it
names the same file (path suffix match) and its line lies within
[line_start - SLACK, line_end + SLACK]. A finding that matches no defect is
`unmatched`, NOT `false positive`: an unmatched finding may be a real defect the
labelers did not know about. `judgments.json` ({"<result>#<index>": "valid-other"
| "invalid" | "d1"...}) resolves them after a blind human or judge pass; until
then precision is reported as a lower bound.

Pure stdlib; reads files only.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
import sys
from collections import defaultdict

import hygiene

SLACK = 15
SEVERITY_RANK = {"minor": 1, "major": 2, "critical": 3}


def file_matches(finding_file: str, defect_file: str) -> bool:
    a = finding_file.strip().lstrip("./").replace("\\", "/")
    b = defect_file.strip().lstrip("./").replace("\\", "/")
    return bool(a) and (a == b or a.endswith("/" + b) or b.endswith("/" + a))


def match_defect(finding: dict, defects: list[dict]) -> str | None:
    for d in defects:
        if file_matches(finding["file"], d["file"]) and \
                d["line_start"] - SLACK <= finding["line"] <= d["line_end"] + SLACK:
            return d["id"]
    return None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - margin) / denom, (centre + margin) / denom)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def score_result(result: dict, case: dict, judgments: dict[str, str], key: str) -> dict:
    review = result.get("review")
    out = {"ok": review is not None, "found": set(), "findings": 0, "matched": 0,
           "valid_other": 0, "invalid": 0, "unjudged": 0, "sev_exact": 0, "sev_within1": 0,
           "sev_compared": 0, "score": review["score"] if review else None}
    if not review:
        return out
    defects = case["defects"]
    by_id = {d["id"]: d for d in defects}
    for i, finding in enumerate(review["findings"]):
        out["findings"] += 1
        did = match_defect(finding, defects)
        verdict = judgments.get(f"{key}#{i}")
        if did is None and verdict in by_id:
            did = verdict
        if did is not None:
            out["matched"] += 1
            out["found"].add(did)
            gt, got = SEVERITY_RANK[by_id[did]["severity"]], SEVERITY_RANK[finding["severity"]]
            out["sev_compared"] += 1
            out["sev_exact"] += gt == got
            out["sev_within1"] += abs(gt - got) <= 1
        elif verdict == "valid-other":
            out["valid_other"] += 1
        elif verdict == "invalid":
            out["invalid"] += 1
        else:
            out["unjudged"] += 1
    return out


def load_results(path: str) -> list[tuple[str, dict]]:
    items = []
    for fp in sorted(glob.glob(os.path.join(path, "*.json"))):
        if os.path.basename(fp).startswith("_"):
            continue
        with open(fp, encoding="utf-8") as fh:
            items.append((os.path.splitext(os.path.basename(fp))[0], json.load(fh)))
    return items


def summarize(cases: dict[str, dict], results: list[tuple[str, dict]], judgments: dict[str, str]) -> dict:
    per_model: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    lat: dict[str, list[float]] = defaultdict(list)
    toks: dict[str, list[int]] = defaultdict(list)
    for key, res in results:
        case = cases.get(res["case_id"])
        if case is None:
            continue
        model = res.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{key}: model must be recorded")
        m = per_model[model]
        s = score_result(res, case, judgments, key)
        for d in case["defects"]:
            kind = "diff-only" if d.get("detectability", "diff-only") == "diff-only" else "needs-context"
            m[f"def_{kind}"] += 1
            m[f"found_{kind}"] += d["id"] in s["found"]
        m["runs"] += 1
        m["failed"] += not s["ok"]
        m["defects"] += len(case["defects"])
        m["found"] += len(s["found"])
        m["findings"] += s["findings"]
        m["matched"] += s["matched"]
        m["valid_other"] += s["valid_other"]
        m["invalid"] += s["invalid"]
        m["unjudged"] += s["unjudged"]
        m["sev_exact"] += s["sev_exact"]
        m["sev_within1"] += s["sev_within1"]
        m["sev_compared"] += s["sev_compared"]
        m["defective_runs"] += s["ok"]
        m["false_pass"] += s["ok"] and s["score"] == 5
        crit = {d["id"] for d in case["defects"] if d["severity"] == "critical"}
        if s["ok"]:
            m["critical_total"] += len(crit)
            m["critical_missed"] += len(crit - s["found"])
        lat[res["model"]].append(res["latency_s"])
        total = sum(v for k, v in res.get("usage", {}).items() if k in ("input_tokens", "output_tokens"))
        if total:
            toks[res["model"]].append(total)
    spreads = hygiene.repeat_spread(cases, results, judgments)
    for model, m in per_model.items():
        m["lat"] = lat[model]
        m["tokens"] = toks[model]
        m.update(spreads[model])
    return per_model


def render(per_model: dict) -> str:
    cols = ["model", "effort (requested)", "recall repeat spread", "reps", "runs", "failed", "recall (found/defects)", "recall 95% CI", "diff-only recall",
            "needs-context recall", "matched/findings", "precision (lower bound)", "severity exact",
            "false-pass 5/5", "critical missed", "lat p50 s", "lat p95 s", "tokens/review"]
    rows = ["| " + " | ".join(cols) + " |", "|" + " --- |" * len(cols)]

    def split(m: dict, kind: str) -> str:
        n = int(m[f"def_{kind}"])
        return f"{int(m[f'found_{kind}'])}/{n}" if n else "n/a"

    def num(value) -> str:
        return "n/a" if value is None else f"{value}"

    for model in sorted(per_model):
        m = per_model[model]
        spread = m.get("recall_spread")
        evidence = [model, m.get("effort") or "unrecorded",
                    f"{spread:.4f}" if spread is not None else f"unmeasured: {m.get('reason', 'no repeat evidence')}",
                    m.get("repeats", 0), int(m["runs"]), int(m["failed"])]
        if not m["defects"]:
            cells = evidence + ["no defects"] + ["n/a"] * (len(cols) - len(evidence) - 1)
            rows.append("| " + " | ".join(str(c) for c in cells) + " |")
            continue
        lo, hi = wilson(int(m["found"]), int(m["defects"]))
        cells = evidence + [
            f"{int(m['found'])}/{int(m['defects'])} = {m['found'] / m['defects']:.2f}",
            f"{lo:.2f}-{hi:.2f}", split(m, "diff-only"), split(m, "needs-context"),
            f"{int(m['matched'])}/{int(m['findings'])}",
            f"{m['matched'] / m['findings']:.2f}" if m["findings"] else "n/a",
            f"{m['sev_exact'] / m['sev_compared']:.2f}" if m["sev_compared"] else "n/a",
            f"{int(m['false_pass'])}/{int(m['defective_runs'])}",
            f"{int(m['critical_missed'])}/{int(m['critical_total'])}",
            num(percentile(m["lat"], 0.5)), num(percentile(m["lat"], 0.95)),
            f"{statistics.mean(m['tokens']):.0f}" if m["tokens"] else "n/a",
        ]
        rows.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--judgments")
    args = ap.parse_args(argv)
    cases = {c["id"]: c for c in json.load(open(args.cases, encoding="utf-8"))["cases"]}
    judgments = json.load(open(args.judgments, encoding="utf-8")) if args.judgments else {}
    results = load_results(args.results)
    if not results:
        print("no results", file=sys.stderr)
        return 2
    try:
        hygiene.validate_results(cases, results)
    except ValueError as exc:
        print(f"comparison rejected: {exc}", file=sys.stderr)
        return 2
    summary = summarize(cases, results, judgments)
    print(render(summary))
    print("\n" + hygiene.render_comparisons(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
