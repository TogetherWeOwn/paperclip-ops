#!/usr/bin/env python3
"""Build corpus v2: v1 seeds + weakened tests, T second-seeds + weakened tests.

For every base PR this writes, under --out:
    <pr>-<seedid>/meta.json   (seed ground truth + weakened-test record; never shown to reviewers)
    <pr>-<seedid>/pr.diff     (base..head for this variant)
    <pr>-<seedid>/tree/       (detached worktree at head + seed patch + weakened-test patch)
    view/<variant>/           (reviewer sandbox: reviewer_view.json, pr.diff, plain tree export)

Variants: 16 v1 seeds (S01..S15 incl S02b) + 12 T seeds (T01..T12) = 28 seeded
variants across the same 12 base PRs (two seeds per variant family), plus
`<pr>-clean` per base. Stdlib only; shells out to git. Never touches the network.
"""
import argparse, json, os, re, subprocess, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "..", "..", "lib")))
from atomic_state import write_json_atomic, write_text_atomic  # noqa: E402

def run(cmd, cwd, check=True, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr[-800:]}")
    return p

def apply_replace(tree, file, old, new, tag):
    path = os.path.join(tree, file)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{tag}: old matches {count}x in {file} (want exactly 1)")
    # Atomic: an ENOSPC or interrupted write leaves the previous tree bytes in place.
    write_text_atomic(path, text.replace(old, new))
    return text[:text.index(old)].count("\n") + 1

# An added skip/todo line. Matches `it.skip(`, `describe.skip(`, `test.todo(`, `xit(`, `xdescribe(`.
SKIP_ADDED = re.compile(r"^\+.*(\.skip\b|\.todo\b|\bxit\(|\bxdescribe\()", re.M)


def subkey(seed_id):
    """Fold an edit key such as S15b / T03c onto its seed. Applied to EDIT keys only; a seed id is
    looked up exactly, so S02b (its own seed) never inherits the edits of S02."""
    return seed_id[:3] if len(seed_id) == 4 and seed_id[3:].isalpha() else seed_id

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--ws3", default=here, help="dir with v2_seeds.py + v2_weakened*.py (default: this dir)")
    ap.add_argument("--eval-dir", default=os.path.dirname(here), help="dir with bases.json + seeds.json (v1 corpus)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="comma-separated variant ids")
    a = ap.parse_args()
    a.repo = os.path.abspath(a.repo)
    a.out = os.path.abspath(a.out)
    a.ws3 = os.path.abspath(a.ws3)
    a.eval_dir = os.path.abspath(a.eval_dir)
    sys.path.insert(0, a.ws3)
    from v2_seeds import V2_SEEDS
    from v2_weakened import V2_WEAKENED
    from v2_weakened_T import V2_WEAKENED_T

    bases = json.load(open(os.path.join(a.eval_dir, "bases.json")))
    vseeds = json.load(open(os.path.join(a.eval_dir, "seeds.json")))["seeds"]
    only = set(a.only.split(",")) if a.only else None
    # normalize: every value is a list of weaken-edits; S15x/T03x sub-keys fold to base
    weak = {}
    for w in V2_WEAKENED:
        weak.setdefault(subkey(w["seed"]), []).append(w)
    weakt = {}
    for w in V2_WEAKENED_T:
        weakt.setdefault(subkey(w["seed"]), []).append(w)
    os.makedirs(a.out, exist_ok=True)
    prev_report = {}
    if only:
        try:
            for r in json.load(open(os.path.join(a.out, "corpus-report.json"))):
                prev_report[r["variant"]] = r
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    report = []

    for b in bases["bases"]:
        sha = b["sha"]
        full = run(["git", "rev-parse", sha], a.repo).stdout.strip()
        base = run(["git", "rev-parse", f"{sha}^"], a.repo).stdout.strip()
        title = run(["git", "show", "-s", "--format=%s", sha], a.repo).stdout.strip()
        body = run(["git", "show", "-s", "--format=%b", sha], a.repo).stdout.strip()
        variants = [("clean", None, [])]
        for s in vseeds:
            if s["pr"] == b["pr"]:
                variants.append((s["id"], s, weak.get(s["id"], [])))
        for t in V2_SEEDS:
            if t["pr"] == b["pr"]:
                variants.append((t["id"], t, weakt.get(t["id"], [])))
        for vid, seed, wlist in variants:
            name = f"{b['pr']}-{vid}"
            if only and name not in only:
                continue
            vdir = os.path.join(a.out, name)
            tree = os.path.join(vdir, "tree")
            if os.path.exists(vdir):
                run(["git", "worktree", "remove", "--force", tree], a.repo, check=False)
                run(["rm", "-rf", vdir], a.repo)
            os.makedirs(vdir)
            run(["git", "worktree", "add", "--detach", tree, full], a.repo)
            nm = os.path.join(a.repo, "node_modules")
            if os.path.isdir(nm):
                os.symlink(nm, os.path.join(tree, "node_modules"))
            entry = {"variant": name, "pr": b["pr"], "seed": seed["id"] if seed else None}
            seed_line = None
            if seed:
                seed_line = apply_replace(tree, seed["file"], seed["old"], seed["new"], seed["id"])
                applied = []
                for w in (wlist or []):
                    if w and w.get("file") and w.get("old"):
                        apply_replace(tree, w["file"], w["old"], w["new"], w["seed"] + "-weakened")
                        applied.append({"file": w["file"], "note": w.get("note", "")})
                entry["weakened"] = applied if applied else [{"file": None, "note": "no contradicting test"}]
            run(["git", "add", "-A", "-f", ":!node_modules"], tree, check=False)
            diff = run(["git", "diff", "--cached", "--no-color", base], tree).stdout
            write_text_atomic(os.path.join(vdir, "pr.diff"), diff)
            # What the seed and its test edits add on top of the PR head. A skipped test is a tell a
            # reviewer can read straight off the diff, so such variants are scored in their own tier.
            delta = run(["git", "diff", "--cached", "--no-color", full], tree).stdout
            visible = bool(SKIP_ADDED.search(delta))
            entry["weakening_visible"] = visible
            truth = None
            if seed:
                truth = {"id": seed["id"], "file": seed["file"],
                         "class": seed.get("klass") or seed.get("class"),
                         "severity": seed["severity"], "desc": seed["desc"], "line": seed_line}
            meta = {"repo": bases["repo"], "pr": b["pr"], "title": title, "body": body,
                    "base": base, "head": full, "variant": name, "seeded": seed is not None,
                    "truth": truth, "corpus": "v2",
                    "weakened_test": entry.get("weakened"),
                    "weakening_visible": visible,
                    "survival_scope": "vitest related, no database (recheck with DB pending)"}
            reviewer_view = {k: meta[k] for k in ("repo", "pr", "title", "body", "base", "head")}
            write_json_atomic(os.path.join(vdir, "meta.json"), meta)
            write_json_atomic(os.path.join(vdir, "reviewer_view.json"), reviewer_view)
            vview = os.path.join(a.out, "view", name)
            run(["rm", "-rf", vview], a.repo)
            os.makedirs(os.path.join(vview, "tree"))
            run(["bash", "-c", f"git ls-files -z | tar --null -T - -cf - | tar -x -C '{os.path.join(vview, 'tree')}'"], tree)
            write_text_atomic(os.path.join(vview, "pr.diff"), diff)
            write_json_atomic(os.path.join(vview, "reviewer_view.json"), reviewer_view)
            report.append(entry)
            print(json.dumps(entry), flush=True)
    if only:
        prev_report.update({r["variant"]: r for r in report})
        report = list(prev_report.values())
    write_json_atomic(os.path.join(a.out, "corpus-report.json"), report)
    return 0

if __name__ == "__main__":
    sys.exit(main())
