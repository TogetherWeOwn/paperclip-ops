#!/usr/bin/env python3
"""Build docs-vs-diff evaluation units from an explicitly supplied cache.

Unit = one changed .md section at one head. Gold comes from labelled
findings at that exact head whose line falls inside the section. Other changed
sections are negatives under the caller's evaluation design, not independently
verified absence of defects. Private caches and labels are not bundled.

Usage: build_units.py --headlines INPUT.json --cache CACHE_DIR --out OUTPUT.json
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "review_tier1"))

from corpus import changed_sections, code_context, gold_for_sections, parse_diff  # noqa: E402
from deterministic import changed_symbols, doc_sections_stale, shared_identifiers  # noqa: E402

CODE_CAP = 24000  # chars of non-doc patch sent as context (about 6k tokens)


def unit_id(repo, number, sha, doc, start):
    """Stable across rebuilds, so a re-run never re-sends or mismatches results."""
    tag = hashlib.sha1(f"{doc}:{start}".encode()).hexdigest()[:6]
    return f"{repo.split('/')[1]}-{number}-{sha[:8]}-{tag}"


def load(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def finding_heads(rows):
    """(repo, pr, commit) triples build() scores positives on."""
    return {(r["repo"], r["pr"], r["commit"]) for r in rows
            if r.get("check") == "docs_vs_diff" and not r.get("carry_over")
            and r.get("location") == "doc" and r.get("commit")}


def missing_fetch(rows, cache):
    """Name incomplete cache inputs rather than silently dropping scored units.

    Requires meta.json, every scored-head diff, and every document build() reads.
    Returns sorted (repo, pr, missing_path) triples; empty means fetch-complete.
    """
    heads = finding_heads(rows)
    missing = []
    for repo, number in sorted({(r["repo"], r["pr"]) for r in rows}):
        base = os.path.join(cache, repo.replace("/", "__"), str(number))
        meta_path = os.path.join(base, "meta.json")
        if not os.path.exists(meta_path):
            missing.append((repo, number, "meta.json"))
            continue
        meta = json.loads(load(meta_path))
        final = meta["final_head"]
        if not os.path.exists(os.path.join(base, final + ".diff")):
            missing.append((repo, number, final + ".diff"))
            continue
        scored = sorted({final} | {c for (rp, n, c) in heads
                                   if rp == repo and n == number})
        for sha in scored:
            diff_path = os.path.join(base, f"{sha}.diff")
            if not os.path.exists(diff_path):
                missing.append((repo, number, sha + ".diff"))
                continue
            try:
                files = parse_diff(load(diff_path))
            except Exception:
                continue  # malformed diffs are build()'s problem; this gate checks fetch completeness
            for path, info in sorted(files.items()):
                if not path.endswith((".md", ".mdx")) or info.get("status") == "deleted":
                    continue
                if not os.path.exists(os.path.join(base, "docs", sha, path.replace("/", "__"))):
                    missing.append((repo, number, f"docs/{sha}/{path}"))
    return missing


def build(rows, cache):
    findings = {}
    for row in rows:
        if (row["check"] == "docs_vs_diff" and not row["carry_over"]
                and row["location"] == "doc" and row["commit"]):
            findings.setdefault((row["repo"], row["pr"], row["commit"], row["path"]),
                                []).append({"id": row["id"], "line": row["line"],
                                            "gold": row["gold"]})
    prs = sorted({(r["repo"], r["pr"], r["private"]) for r in rows})
    units, skipped = [], Counter()
    placed = set()
    for repo, number, private in prs:
        base = os.path.join(cache, repo.replace("/", "__"), str(number))
        meta_path = os.path.join(base, "meta.json")
        if not os.path.exists(meta_path):
            skipped["no_meta"] += 1
            continue
        meta = json.loads(load(meta_path))
        heads = {meta["final_head"]} | {k[2] for k in findings
                                         if k[0] == repo and k[1] == number}
        for sha in sorted(heads):
            diff_path = os.path.join(base, f"{sha}.diff")
            if not os.path.exists(diff_path):
                skipped["no_diff"] += 1
                continue
            files = parse_diff(load(diff_path))
            ctx, truncated = code_context(files, CODE_CAP)
            diff_files, diff_symbols = sorted(files), changed_symbols(load(diff_path))
            for path, info in sorted(files.items()):
                if not path.endswith((".md", ".mdx")) or info["status"] == "deleted":
                    continue
                doc_path = os.path.join(base, "docs", sha, path.replace("/", "__"))
                if not os.path.exists(doc_path):
                    skipped["no_doc"] += 1
                    continue
                sects = changed_sections(load(doc_path), info["added_lines"])
                pos = findings.get((repo, number, sha, path), [])
                for sect, gold, ids in gold_for_sections(sects, pos):
                    placed.update(ids)
                    overlap = shared_identifiers(sect["text"], ctx)
                    probe = sect["text"] if sect["heading"] != "(preamble)" else "# x\n" + sect["text"]
                    stale = [t for _, t in doc_sections_stale(probe, diff_files, diff_symbols)]
                    units.append({
                        "unit_id": unit_id(repo, number, sha, path, sect["start"]),
                        "repo": repo, "private": private, "pr": number, "sha": sha,
                        "is_final_head": sha == meta["final_head"],
                        "doc": path, "heading": sect["heading"],
                        "start": sect["start"], "end": sect["end"],
                        "gold": gold, "finding_ids": ids,
                        "ctx_chars": len(ctx), "ctx_truncated": truncated,
                        "section_chars": len(sect["text"]),
                        "overlap": overlap, "det_stale": stale,
                    })
    unplaced = sorted(i["id"] for v in findings.values() for i in v) if findings else []
    unplaced = [i for i in unplaced if i not in placed]
    return units, skipped, unplaced


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headlines", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    rows = json.loads(load(args.headlines))
    missing = missing_fetch(rows, args.cache)
    if missing:
        by_pr = {}
        for repo, number, path in missing:
            by_pr.setdefault((repo, number), []).append(path)
        for (repo, number), paths in sorted(by_pr.items()):
            shown = ", ".join(paths[:3])
            more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
            print(f"fetch-incomplete: {repo}#{number} lacks {shown}{more}; "
                  f"finish fetch_diffs.py first", file=sys.stderr)
        sys.exit(1)
    units, skipped, unplaced = build(rows, args.cache)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"units": units, "unplaced_finding_ids": unplaced}, handle,
                  indent=1, sort_keys=True)
    pos = [u for u in units if u["gold"] != "none"]
    print(f"{len(units)} units, {len(pos)} positive, {len(unplaced)} doc findings "
          f"outside a changed section/head; skipped {dict(skipped)}")
    for private in (False, True):
        sub = [u for u in units if u["private"] == private]
        print(f"  private={private}: {len(sub)} units, "
              f"{sum(u['gold'] != 'none' for u in sub)} positive, "
              f"{sum(bool(u['overlap']) for u in sub)} pass the prefilter, "
              f"{sum(bool(u['overlap']) for u in sub if u['gold'] != 'none')} positives pass, "
              f"{sum(u['ctx_truncated'] for u in sub)} truncated")


if __name__ == "__main__":
    main()
