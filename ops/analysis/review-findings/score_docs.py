#!/usr/bin/env python3
"""Score docs-vs-diff arms against explicitly supplied labelled units.

Print precision/recall (Wilson 95%) and cost per arm. A unit that never reached
an arm is excluded from that arm's denominator. No model calls are made, and
private labels, responses and measurements are not included with this source.

Usage: score_docs.py --units INPUT.json --results-dir DIR [--threshold 0.5]
Result filenames use the public `review-docs-{arm}-*.results.jsonl` convention.
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "review_tier1"))

from eval import cost_per_pr, score  # noqa: E402

RESULTS = "./results"
PRICE_PER_TOKEN = {"clef-flash": 0.09e-6, "clef": 0.24e-6}  # historical fallback, $ per input token


def load_results(results_dir, arm):
    """Return {unit_id: {provider: {"p_true": float, "cost": float}}}."""
    out = {}
    for path in sorted(glob.glob(os.path.join(results_dir, f"review-docs-{arm}-*.results.jsonl"))):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if "error" in row or "answers" not in row:
                    continue
                noul = row["answers"].get("section_true", {}).get("noul")
                if noul is None:
                    continue
                usage = row.get("usage", {})
                cost = usage.get("cost")
                if cost is None:
                    cost = usage.get("input_tokens", 0) * PRICE_PER_TOKEN.get(row["provider"], 0.0)
                out.setdefault(row["id"], {})[row["provider"]] = {
                    "p_true": float(noul), "cost": float(cost)}
    return out


HUNKS_PROVIDERS = ("jev", "clef-flash", "clef")


def sent_ids(hunks, providers):
    """Unit ids holding a result for every listed provider: the units the
    per-hunk send gate actually mailed. Anything else never reached a model,
    so scoring it would cap recall before any model ran."""
    ids = None
    for provider in providers:
        have = {unit for unit, per in hunks.items() if provider in per}
        ids = have if ids is None else ids & have
    return ids or set()


def rows_for(units, fired, provider, cost_of):
    rows = []
    for unit in units:
        verdict = "hold" if fired(unit) else "none"
        rows.append({"check": "docs_vs_diff", "provider": provider, "gold": unit["gold"],
                     "verdict": verdict, "cost": cost_of(unit), "pr": f"{unit['repo']}#{unit['pr']}"})
    return rows


def table(rows):
    cells = score(rows)["docs_vs_diff"]
    lines = ["| arm | units | positives | flagged | TP | FP | FN | precision (95% CI) | recall (95% CI) | cost $ | cost / PR $ |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm, c in sorted(cells.items()):
        per_pr = cost_per_pr([r for r in rows if r["provider"] == arm])
        lines.append(
            f"| {arm} | {c['n']} | {c['tp'] + c['fn']} | {c['flagged']} | {c['tp']} | {c['fp']} | {c['fn']} "
            f"| {c['precision']:.2f} ({c['precision_ci'][0]:.2f}-{c['precision_ci'][1]:.2f}) "
            f"| {c['recall']:.2f} ({c['recall_ci'][0]:.2f}-{c['recall_ci'][1]:.2f}) | {c['cost']:.4f} | {per_pr:.5f} |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--units", required=True)
    parser.add_argument("--results-dir", default=RESULTS)
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="fire when P(section true) is below this")
    args = parser.parse_args()
    with open(args.units, encoding="utf-8") as handle:
        units = json.load(handle)["units"]
    public = [u for u in units if not u["private"]]
    hunks = load_results(args.results_dir, "hunks")
    full = load_results(args.results_dir, "full")
    t = args.threshold
    rows = []
    rows += rows_for(units, lambda u: bool(u.get("det_stale")), "det-stale", lambda u: 0.0)
    rows += rows_for(units, lambda u: bool(u["overlap"]), "prefilter", lambda u: 0.0)
    full_sent = {unit for unit, per in full.items() if "jev" in per}
    rows += rows_for(
        [u for u in public if u["unit_id"] in full_sent],
        lambda u: full[u["unit_id"]]["jev"]["p_true"] < t,
        "jev-full", lambda u: full[u["unit_id"]]["jev"].get("cost", 0.0))
    for provider in HUNKS_PROVIDERS:
        sent = sent_ids(hunks, (provider,))
        rows += rows_for(
            [u for u in public if u["unit_id"] in sent],
            lambda u, p=provider: hunks[u["unit_id"]][p]["p_true"] < t,
            f"{provider}-hunks",
            lambda u, p=provider: hunks[u["unit_id"]][p].get("cost", 0.0))

    def vote(u):
        votes = [hunks[u["unit_id"]][p]["p_true"] < t for p in HUNKS_PROVIDERS]
        return sum(votes) >= 2
    vote_sent = sent_ids(hunks, HUNKS_PROVIDERS)
    rows += rows_for([u for u in public if u["unit_id"] in vote_sent], vote,
                     "vote3-hunks",
                     lambda u: sum(v["cost"] for v in hunks[u["unit_id"]].values()))
    print(f"threshold {t}; {len(units)} units ({len(public)} public); "
          f"model results: hunks {len(hunks)} units, full {len(full)} units; "
          f"hunks sent {len(vote_sent)} units\n")
    print(table(rows))
    pos = [u for u in public if u["gold"] != "none"]
    print(f"\nprefilter keeps {sum(bool(u['overlap']) for u in pos)} of {len(pos)} public positives")


if __name__ == "__main__":
    main()
