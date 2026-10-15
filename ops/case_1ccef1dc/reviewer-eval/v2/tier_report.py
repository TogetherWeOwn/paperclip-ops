#!/usr/bin/env python3
"""Per-tier and per-variant view of one arm on the v2 corpus. Model-agnostic: never calls a model.

    tier_report.py --corpus corpus-v2 --findings findings --arm claude-sonnet-5-5 [--markdown]

Tiers (PROTOCOL-v2.md): `ci-visible` (some related test red, with or without a database),
`visible-weakening` (CI-silent, but the seed's test edit adds a skip/todo line to the diff) and
`stealth-silent` (CI-silent, no visible weakening: the headline). Clean variants are listed apart.

Per tier it reports seeded variants, **hits** (score.py's file + 15-line window, any severity) and
**gate hits** (a hit raised at P0/P1, i.e. a finding that on its own blocks the merge), each with a
Wilson 95% interval. A hit raised only at P2 on an APPROVE verdict is a find, not a block; the two
figures differ exactly there. Exit 0 on a well-formed corpus, 2 on unreadable input.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import score  # noqa: E402  (the v1 scorer, one directory up)


def tier_of(meta):
    if not meta.get("ci_silent"):
        return "ci-visible"
    return "visible-weakening" if meta.get("weakening_visible") else "stealth-silent"


def gate_hit(meta, result):
    truth = meta["truth"]
    return any(
        f.get("severity") in score.SEV_GATE
        and os.path.normpath(f.get("file", "")) == os.path.normpath(truth["file"])
        and abs(int(f.get("line", -10**9)) - int(truth["line"])) <= score.WINDOW
        for f in result.get("findings", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--findings", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--markdown", action="store_true")
    a = ap.parse_args()
    try:
        rows = []
        for name in sorted(os.listdir(a.corpus)):
            mp = os.path.join(a.corpus, name, "meta.json")
            fp = os.path.join(a.findings, a.arm, f"{name}.json")
            if not (os.path.isfile(mp) and os.path.isfile(fp)):
                continue
            meta, result = score.load(mp), score.load(fp)
            if not meta["seeded"]:
                rows.append({"variant": name, "tier": "clean", "verdict": result.get("verdict"),
                             "p0p1_findings": sum(1 for f in result.get("findings", []) if f.get("severity") in score.SEV_GATE)})
                continue
            s = score.score_variant(meta, result, {}, a.arm)
            rows.append({"variant": name, "tier": tier_of(meta), "seed": meta["truth"]["id"],
                         "seed_severity": meta["truth"]["severity"], "hit": bool(s["hit"]),
                         "gate_hit": gate_hit(meta, result), "verdict": result.get("verdict"),
                         "near_misses": len(s["near_miss"])})
    except (OSError, ValueError, KeyError) as exc:
        print(f"unreadable input: {exc}", file=sys.stderr)
        return 2
    tiers = {}
    for t in ("stealth-silent", "visible-weakening", "ci-visible"):
        sel = [r for r in rows if r["tier"] == t]
        h, g = sum(r["hit"] for r in sel), sum(r["gate_hit"] for r in sel)
        tiers[t] = {"n": len(sel), "hits": h, "hits_ci95": score.wilson(h, len(sel)),
                    "gate_hits": g, "gate_hits_ci95": score.wilson(g, len(sel))}
    silent = [r for r in rows if r["tier"] in ("stealth-silent", "visible-weakening")]
    tiers["ci-silent (both)"] = {"n": len(silent), "hits": sum(r["hit"] for r in silent),
                                 "gate_hits": sum(r["gate_hit"] for r in silent)}
    seeded = [r for r in rows if r["tier"] != "clean"]
    tiers["all seeded"] = {"n": len(seeded), "hits": sum(r["hit"] for r in seeded),
                           "gate_hits": sum(r["gate_hit"] for r in seeded)}
    clean = [r for r in rows if r["tier"] == "clean"]
    out = {"arm": a.arm, "tiers": tiers, "clean": {"n": len(clean),
           "changes": sum(1 for r in clean if r["verdict"] == "CHANGES"),
           "p0p1_findings": sum(r["p0p1_findings"] for r in clean)}, "variants": rows}
    if a.markdown:
        print(f"| tier | n | hits | hits 95% CI | gate hits (P0/P1) | gate hits 95% CI |")
        print("|---|---|---|---|---|---|")
        for t, v in tiers.items():
            lo, hi = v.get("hits_ci95", ("", ""))
            glo, ghi = v.get("gate_hits_ci95", ("", ""))
            print(f"| {t} | {v['n']} | {v['hits']} | {lo}-{hi} | {v['gate_hits']} | {glo}-{ghi} |"
                  if lo != "" else f"| {t} | {v['n']} | {v['hits']} |  | {v['gate_hits']} |  |")
        print()
        print("| variant | tier | seed | severity | hit | gate hit | verdict |")
        print("|---|---|---|---|---|---|---|")
        for r in rows:
            if r["tier"] != "clean":
                print(f"| {r['variant']} | {r['tier']} | {r['seed']} | {r['seed_severity']} | "
                      f"{'yes' if r['hit'] else 'no'} | {'yes' if r['gate_hit'] else 'no'} | {r['verdict']} |")
        print(f"\nClean variants: {out['clean']['n']}, CHANGES on {out['clean']['changes']}, "
              f"P0/P1 findings {out['clean']['p0p1_findings']}.")
    else:
        print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
