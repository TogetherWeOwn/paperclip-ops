#!/usr/bin/env python3
"""Fold a survival report into the v2 corpus so score.py can split CI-silent from CI-visible.

    merge_survival.py --corpus corpus-v2 --nodb corpus-v2/survival-report.json \
                      [--db survival-report-db.json]

`--nodb` is the vitest-related run with no DATABASE_URL (local default). `--db` is the same run
against the sanctioned test database; when given, a seed is CI-silent only if it stayed green in
BOTH runs (CI has a database, so a seed a DB suite kills is CI-visible). A DB row whose clean-tree
baseline was red (`baseline_exit` != 0) is unknown, not visible: the environment failed, not the
seed, and the variant keeps its no-database classification. Every seeded variant in
corpus-report.json must have a row in --nodb (and in --db when given) or the merge aborts, so a
half-finished survival run can never be read as "silent".

Writes `ci_silent` and `survival` into each seeded <variant>/meta.json and a summary to
`<corpus>/survival-summary.json`. Clean variants are not touched. Prints the silent count.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402


def rows(path):
    return {r["variant"]: r for r in json.load(open(path, encoding="utf-8"))}


def write_verdicts(path, data):
    """Persist merge verdicts atomically; fail loudly, never truncate.

    The verdicts go through a same-directory temp file plus fsync plus atomic
    rename, so an ENOSPC mid-write leaves the previous file in place. A
    failing write names the path and errno on stderr and exits non-zero.
    """
    try:
        write_json_atomic(path, data)
    except OSError as exc:
        print("merge_survival: write failed: %s: errno=%s"
              % (path, getattr(exc, "errno", "?")), file=sys.stderr)
        raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--nodb", required=True)
    ap.add_argument("--db")
    a = ap.parse_args()
    corpus = os.path.abspath(a.corpus)
    report = json.load(open(os.path.join(corpus, "corpus-report.json"), encoding="utf-8"))
    seeded = [e["variant"] for e in report if e.get("seed")]
    nodb = rows(a.nodb)
    db = rows(a.db) if a.db else None
    missing = [v for v in seeded if v not in nodb] + ([v for v in seeded if v not in db] if db else [])
    if missing:
        print(f"refusing: no survival row for {sorted(set(missing))}", file=sys.stderr)
        return 2
    summary = []
    for v in seeded:
        silent_nodb = bool(nodb[v]["ci_silent"])
        silent_db = bool(db[v]["ci_silent"]) if db else None
        # A database run that is red on the clean tree too is the environment, not the seed: unknown.
        if db and db[v].get("baseline_exit", 0) != 0:
            silent_db = None
        silent = silent_nodb and (silent_db is not False)
        path = os.path.join(corpus, v, "meta.json")
        meta = json.load(open(path, encoding="utf-8"))
        meta["ci_silent"] = silent
        meta["survival"] = {
            "no_database": {"exit": nodb[v]["exit"], "silent": silent_nodb},
            "with_database": ({"exit": db[v]["exit"], "silent": silent_db,
                               "baseline_exit": db[v].get("baseline_exit")} if db else None),
            "scope": "vitest related on the seeded file, weakened tests applied",
        }
        write_verdicts(path, meta)
        summary.append({"variant": v, "seed": nodb[v]["seed"], "ci_silent": silent,
                        "weakening_visible": bool(meta.get("weakening_visible")),
                        "silent_no_database": silent_nodb, "silent_with_database": silent_db})
    write_verdicts(os.path.join(corpus, "survival-summary.json"), summary)
    print(f"SILENT {sum(1 for s in summary if s['ci_silent'])}/{len(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
