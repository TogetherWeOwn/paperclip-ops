#!/usr/bin/env python3
"""Score reviewer arms against the seeded corpus. Model-agnostic: it never calls a model.

Inputs
  --corpus  directory written by build_corpus.py (<variant>/meta.json)
  --findings  directory laid out as <arm>/<variant>.json, each file:
      {"verdict": "APPROVE" | "CHANGES",
       "findings": [{"file": str, "line": int, "severity": "P0"|"P1"|"P2", "title": str}],
       "usage": {"total_tokens": int, "seconds": float}}   # usage optional; input/output split if known
  --adjudication  optional JSON: {"<arm>|<variant>|<finding index>": "valid"|"invalid"}
      for findings the automatic rule cannot settle (see below)

Matching rule (fixed before any arm is run, see PROTOCOL.md)
  A finding hits a seed when its file equals the seed file and |line - seed line| <= WINDOW.
  Same file but outside the window is `near-miss`: it is reported, not credited, and is
  listed for manual adjudication. A finding on a clean variant, or a P0/P1 finding that does
  not hit its seed, is a candidate false positive until adjudicated.

Outputs a JSON object on stdout; --markdown prints a table instead. Exit 0 always for a
well-formed corpus; exit 2 for unreadable input.
"""
import argparse
import json
import math
import os
import sys

WINDOW = 15
SEV_GATE = {"P0", "P1"}


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def exact_binom_two_sided(b, c):
    """Exact McNemar p-value for discordant pair counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((centre - half) / d, 3), round((centre + half) / d, 3))


def score_variant(meta, result, adjudication, arm):
    findings = result.get("findings", [])
    out = {"hit": None, "extra_gate_findings": 0, "near_miss": [], "unadjudicated": [], "clean_fp": 0}
    truth = meta.get("truth")
    for i, f in enumerate(findings):
        sev = f.get("severity", "P2")
        key = f"{arm}|{meta['variant']}|{i}"
        verdict = adjudication.get(key)
        same_file = truth and os.path.normpath(f.get("file", "")) == os.path.normpath(truth["file"])
        near = same_file and abs(int(f.get("line", -10**9)) - int(truth["line"])) <= WINDOW
        if truth and near:
            out["hit"] = True  # a hit counts at any severity; severity is scored separately
            continue
        if truth and same_file:
            out["near_miss"].append(i)
        if sev in SEV_GATE:
            if verdict == "valid":
                continue
            if verdict == "invalid":
                out["extra_gate_findings"] += 1
                if not truth:
                    out["clean_fp"] += 1
            else:
                out["unadjudicated"].append(i)
    if truth and out["hit"] is None:
        out["hit"] = False
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--findings", required=True)
    ap.add_argument("--adjudication")
    ap.add_argument("--markdown", action="store_true")
    a = ap.parse_args()
    try:
        adjudication = load(a.adjudication) if a.adjudication else {}
        metas = {}
        for name in sorted(os.listdir(a.corpus)):
            mp = os.path.join(a.corpus, name, "meta.json")
            if os.path.isfile(mp):
                metas[name] = load(mp)
        arms = sorted(d for d in os.listdir(a.findings) if os.path.isdir(os.path.join(a.findings, d)))
    except (OSError, ValueError) as exc:
        print(f"unreadable input: {exc}", file=sys.stderr)
        return 2

    per = {}
    for arm in arms:
        rows = {}
        for variant, meta in metas.items():
            fp = os.path.join(a.findings, arm, f"{variant}.json")
            if not os.path.isfile(fp):
                continue
            rows[variant] = (meta, load(fp))
        seeded = {v: score_variant(m, r, adjudication, arm) for v, (m, r) in rows.items() if m["seeded"]}
        clean = {v: score_variant(m, r, adjudication, arm) for v, (m, r) in rows.items() if not m["seeded"]}
        hits = sum(1 for s in seeded.values() if s["hit"])
        gate_hits = sum(
            1 for v, s in seeded.items()
            if s["hit"] and any(
                f.get("severity") in SEV_GATE
                and os.path.normpath(f.get("file", "")) == os.path.normpath(metas[v]["truth"]["file"])
                and abs(int(f.get("line", -10**9)) - int(metas[v]["truth"]["line"])) <= WINDOW
                for f in rows[v][1].get("findings", [])))
        clean_changes = sum(1 for v, (m, r) in rows.items() if not m["seeded"] and r.get("verdict") == "CHANGES")
        seeded_changes = sum(1 for v, (m, r) in rows.items() if m["seeded"] and r.get("verdict") == "CHANGES")
        silent = {v: s for v, s in seeded.items() if metas[v].get("ci_silent")}
        silent_hits = sum(1 for s in silent.values() if s["hit"])
        # corpus v2: a seed whose test edit shows up as an added skip is a tell, so the stealth
        # tier holds the CI-silent seeds that carry no visible weakening (v1 metas have no flag).
        stealth = {v: s for v, s in silent.items() if not metas[v].get("weakening_visible")}
        stealth_hits = sum(1 for s in stealth.values() if s["hit"])
        tok = [(r.get("usage") or {}) for _, r in rows.values()]
        per[arm] = {
            "seeded_n": len(seeded), "seeded_hits": hits, "seeded_recall": round(hits / len(seeded), 3) if seeded else None,
            "seeded_recall_ci95": wilson(hits, len(seeded)),
            "ci_silent_n": len(silent), "ci_silent_hits": silent_hits,
            "ci_silent_recall": round(silent_hits / len(silent), 3) if silent else None,
            "ci_silent_recall_ci95": wilson(silent_hits, len(silent)),
            "stealth_silent_n": len(stealth), "stealth_silent_hits": stealth_hits,
            "stealth_silent_recall": round(stealth_hits / len(stealth), 3) if stealth else None,
            "stealth_silent_recall_ci95": wilson(stealth_hits, len(stealth)),
            "seeded_hits_at_P0P1": gate_hits,
            "seeded_changes_verdicts": seeded_changes,
            "clean_n": len(clean), "clean_changes_verdicts": clean_changes,
            "clean_p0p1_findings_unadjudicated": sum(len(s["unadjudicated"]) for s in clean.values()),
            "clean_fp_adjudicated": sum(s["clean_fp"] for s in clean.values()),
            "near_misses_to_adjudicate": sum(len(s["near_miss"]) for s in seeded.values()),
            "unadjudicated_gate_findings": sum(len(s["unadjudicated"]) for s in list(seeded.values()) + list(clean.values())),
            "input_tokens": sum(int(u.get("input_tokens") or 0) for u in tok),
            "output_tokens": sum(int(u.get("output_tokens") or 0) for u in tok),
            "total_tokens": sum(int(u.get("total_tokens") or 0) for u in tok),
            "reviews_with_usage": sum(1 for u in tok if u),
            "invalid_outputs": sum(1 for _, r in rows.values() if r.get("invalid")),
            "seconds": round(sum(float(u.get("seconds") or 0) for u in tok), 1),
            "_hit_by_variant": {v: bool(s["hit"]) for v, s in seeded.items()},
        }

    pairs = {}
    for i, x in enumerate(arms):
        for y in arms[i + 1:]:
            vs = sorted(set(per[x]["_hit_by_variant"]) & set(per[y]["_hit_by_variant"]))
            b = sum(1 for v in vs if per[x]["_hit_by_variant"][v] and not per[y]["_hit_by_variant"][v])
            c = sum(1 for v in vs if per[y]["_hit_by_variant"][v] and not per[x]["_hit_by_variant"][v])
            pairs[f"{x} vs {y}"] = {"common_seeded": len(vs), "only_first": b, "only_second": c,
                                    "mcnemar_exact_p": round(exact_binom_two_sided(b, c), 4)}
    for v in per.values():
        v.pop("_hit_by_variant")
    result = {"window": WINDOW, "arms": per, "paired": pairs}
    if a.markdown:
        print("| arm | CI-silent hits (headline) | stealth-silent hits | all seeded hits | recall, all (95% CI) | hits at P0/P1 | CHANGES on clean | clean P0/P1 unadjudicated | total tok | invalid |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for arm, v in per.items():
            lo, hi = v["seeded_recall_ci95"]
            print(f"| {arm} | {v['ci_silent_hits']}/{v['ci_silent_n']} | {v['stealth_silent_hits']}/{v['stealth_silent_n']} | {v['seeded_hits']}/{v['seeded_n']} | {v['seeded_recall']} ({lo}-{hi}) | "
                  f"{v['seeded_hits_at_P0P1']} | {v['clean_changes_verdicts']}/{v['clean_n']} | "
                  f"{v['clean_p0p1_findings_unadjudicated']} | {v['total_tokens']} | {v['invalid_outputs']} |")
    else:
        print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
