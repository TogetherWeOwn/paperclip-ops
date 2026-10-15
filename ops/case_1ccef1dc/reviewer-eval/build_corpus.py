#!/usr/bin/env python3
"""Build the seeded-defect reviewer corpus from merged PRs of a local clone.

For every base PR (squash commit) this writes, under --out:

    <variant>/meta.json      PR title/body, base/head SHAs, seed ground truth (never shown to a reviewer)
    <variant>/pr.diff        base..head for this variant
    <variant>/tree/          a detached worktree at head (seeded variants carry the patch)
    view/<variant>/          the reviewer sandbox: reviewer_view.json, pr.diff and a plain
                             export of the head tree (no .git, no ground truth)

Variants: `<pr>-clean` for every base PR, `<pr>-<seedid>` for every usable seed.

A seed is usable only when it is CI-silent: the PR's own related tests still pass on the
seeded tree. `--check-survival` runs `vitest related` on the seeded file; a seed whose
tests go red is dropped, because a defect CI already catches is not what review is for.
Local runs skip DB-backed suites, so "silent" here means "silent without a database"
and is recorded as such in meta.json.

Stdlib only; shells out to git and (for survival) npx vitest. Never touches the network.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
from atomic_state import write_json_atomic, write_text_atomic  # noqa: E402


def run(cmd, cwd, check=True, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr[-800:]}")
    return p


def apply_seed(tree, seed):
    path = os.path.join(tree, seed["file"])
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    count = text.count(seed["old"])
    if count != 1:
        raise SystemExit(f"seed {seed['id']}: `old` matches {count} times in {seed['file']} (want exactly 1)")
    # Atomic even inside the worktree: an ENOSPC mid-write must not leave a
    # truncated seeded file behind for the survival run to read as data.
    write_text_atomic(path, text.replace(seed["old"], seed["new"]))
    # 1-based line of the changed region in the seeded file: the scoring anchor.
    return text[: text.index(seed["old"])].count("\n") + 1


def survival(tree, seed):
    p = run(["npx", "vitest", "related", seed["file"], "--run", "--reporter=dot"], tree, check=False, timeout=900)
    return {"exit": p.returncode, "tail": (p.stdout + p.stderr)[-400:]}


def reviewer_view(meta):
    """The only fields a reviewer arm may see. Ground truth is never in here."""
    return {k: meta[k] for k in ("repo", "pr", "title", "body", "base", "head")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="local clone of the base repository")
    ap.add_argument("--bases", default=os.path.join(os.path.dirname(__file__), "bases.json"))
    ap.add_argument("--seeds", default=os.path.join(os.path.dirname(__file__), "seeds.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--check-survival", action="store_true")
    ap.add_argument("--only", help="comma-separated variant ids")
    a = ap.parse_args()

    bases = json.load(open(a.bases))
    seeds = json.load(open(a.seeds))["seeds"]
    only = set(a.only.split(",")) if a.only else None
    os.makedirs(a.out, exist_ok=True)
    report = []

    for b in bases["bases"]:
        sha = b["sha"]
        full = run(["git", "rev-parse", sha], a.repo).stdout.strip()
        base = run(["git", "rev-parse", f"{sha}^"], a.repo).stdout.strip()
        title = run(["git", "show", "-s", "--format=%s", sha], a.repo).stdout.strip()
        body = run(["git", "show", "-s", "--format=%b", sha], a.repo).stdout.strip()
        variants = [("clean", None)] + [(s["id"], s) for s in seeds if s["pr"] == b["pr"]]
        for vid, seed in variants:
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
                seed_line = apply_seed(tree, seed)
                if a.check_survival:
                    entry["survival"] = survival(tree, seed)
                    entry["ci_silent"] = entry["survival"]["exit"] == 0
                else:
                    entry["ci_silent"] = seed.get("ci_silent")  # recorded in seeds.json by an earlier survival run
            # Diff shown to the reviewer: PR base against the (possibly seeded) head tree.
            run(["git", "add", "-A", "-f", ":!node_modules"], tree, check=False)
            diff = run(["git", "diff", "--cached", "--no-color", base], tree).stdout
            write_text_atomic(os.path.join(vdir, "pr.diff"), diff)
            meta = {"repo": bases["repo"], "pr": b["pr"], "title": title, "body": body, "base": base,
                    "head": full, "variant": name, "seeded": seed is not None,
                    "truth": ({**{k: seed[k] for k in ("id", "file", "class", "severity", "desc")}, "line": seed_line}
                              if seed else None),
                    "ci_silent": entry.get("ci_silent"), "survival_scope": "vitest related, no database"}
            write_json_atomic(os.path.join(vdir, "meta.json"), meta)
            write_json_atomic(os.path.join(vdir, "reviewer_view.json"), reviewer_view(meta))
            # Reviewer sandbox: only what a reviewer arm may see. No ground truth, no .git
            # (history would show the future), no node_modules.
            vview = os.path.join(a.out, "view", name)
            run(["rm", "-rf", vview], a.repo)
            os.makedirs(os.path.join(vview, "tree"))
            run(["bash", "-c", f"git ls-files -z | tar --null -T - -cf - | tar -x -C '{os.path.join(vview, 'tree')}'"], tree)
            write_text_atomic(os.path.join(vview, "pr.diff"), diff)
            write_json_atomic(os.path.join(vview, "reviewer_view.json"), reviewer_view(meta))
            report.append(entry)
            print(json.dumps({k: v for k, v in entry.items() if k != "survival"}), flush=True)

    write_json_atomic(os.path.join(a.out, "corpus-report.json"), report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
