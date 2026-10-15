#!/usr/bin/env python3
"""Per-model reviewer statistics from a dataset.json written by collect.py.

What this can and cannot say. It reports cost, latency and verdict mix per model from real
Code Reviewer runs. It does NOT measure review quality: a verdict has no ground truth here,
each head SHA is normally reviewed once (the review-economy rule), so two reviewers almost
never disagree, and the router does not assign models at random, so PR difficulty differs by
model. Treat every row as descriptive. Quality comes from the seeded benchmark.

Usage: analyze.py dataset.json [--markdown]
"""
import collections as C
import datetime as dt
import json
import re
import statistics as st
import sys

VERDICT = re.compile(r"Review\s+([0-9a-f]{7,40})\s*[:\-—]?\s*\**\s*(APPROVE|CHANGES)", re.I)


def ts(x):
    return dt.datetime.fromisoformat(x.replace("Z", "+00:00")) if x else None


def events(ds):
    runs, issues = ds["runs"], ds["issues"]
    ev = []
    for card in issues.values():
        for c in card["comments"]:
            m = VERDICT.search(c["body"][:200])
            if not m:
                continue
            run = runs.get(c.get("run") or "")
            ev.append({
                "issue": card["ident"], "sha": m.group(1)[:7].lower(), "v": m.group(2).upper(), "at": c["at"],
                "model": run["model"] if run else None,
                "in": run and run.get("in"), "out": run and run.get("out"),
                "dur": (ts(run["fin"]) - ts(run["start"])).total_seconds() if run and run.get("fin") and run.get("start") else None})
    return ev


def table(ev):
    by = C.defaultdict(list)
    for e in ev:
        by[e["model"]].append(e)
    cards = C.defaultdict(list)
    for e in sorted(ev, key=lambda e: e["at"]):
        cards[e["issue"]].append(e)
    rows = []
    med = lambda a: int(st.median(a)) if a else None
    for model, lst in sorted(by.items(), key=lambda kv: -len(kv[1])):
        first_changes = [c for c in cards.values() if c[0]["model"] == model and c[0]["v"] == "CHANGES"]
        converged = sum(1 for c in first_changes if any(x["v"] == "APPROVE" and x["sha"] != c[0]["sha"] for x in c[1:]))
        rows.append({
            "model": model or "(outside run window)", "verdicts": len(lst),
            "changes": sum(e["v"] == "CHANGES" for e in lst),
            "first_pass_changes": len(first_changes), "of_which_later_approved_at_new_sha": converged,
            "median_in_tokens": med([e["in"] for e in lst if e["in"]]),
            "median_out_tokens": med([e["out"] for e in lst if e["out"]]),
            "median_seconds": med([e["dur"] for e in lst if e["dur"]])})
    by_sha = C.defaultdict(list)
    for e in ev:
        by_sha[(e["issue"], e["sha"])].append(e)
    conflicts = [k for k, v in by_sha.items() if len({x["v"] for x in v}) > 1]
    return rows, {"card_sha_pairs": len(by_sha), "conflicting_verdicts": len(conflicts)}


def main():
    ds = json.load(open(sys.argv[1]))
    rows, agree = table(events(ds))
    if "--markdown" in sys.argv:
        print("| model | verdicts | CHANGES | first-pass CHANGES | later approved at new SHA | median in tok | median out tok | median s |")
        print("|---|---|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['model']} | {r['verdicts']} | {r['changes']} | {r['first_pass_changes']} | "
                  f"{r['of_which_later_approved_at_new_sha']} | {r['median_in_tokens']} | {r['median_out_tokens']} | {r['median_seconds']} |")
        print(f"\n{agree['conflicting_verdicts']} of {agree['card_sha_pairs']} (card, head) pairs carry conflicting verdicts.")
    else:
        print(json.dumps({"models": rows, "agreement": agree}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
