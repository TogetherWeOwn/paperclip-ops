#!/usr/bin/env python3
"""Shuffle a built corpus into an arena of opaque ids, so a path never says seeded or clean.

    make_arena.py --corpus <dir written by build_corpus.py> --arena <new dir> \
                  --map <arena_map.json, outside the arena> --seed <int> [--variants a,b,c]

Copies <corpus>/view/<variant> to <arena>/rNN in an order fixed by --seed and writes
{rNN: variant} to --map. The map is ground-truth-adjacent: keep it out of the arena and out
of any directory a reviewer arm can read. A seed is spent once an arm has used the arena;
use a new seed (and so a new order and new ids) for every further arm or repetition.
Refuses to run if the arena directory already exists.
"""
import argparse
import os
import random
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--arena", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--seed", required=True, type=int)
    ap.add_argument("--variants")
    a = ap.parse_args()
    if os.path.exists(a.arena):
        print(f"refusing: {a.arena} already exists", file=sys.stderr)
        return 2
    if os.path.realpath(a.map).startswith(os.path.realpath(a.arena) + os.sep):
        print("refusing: the map must live outside the arena", file=sys.stderr)
        return 2
    src = os.path.join(a.corpus, "view")
    variants = sorted(os.listdir(src))
    if a.variants:
        want = a.variants.split(",")
        missing = [v for v in want if v not in variants]
        if missing:
            print(f"unknown variants: {missing}", file=sys.stderr)
            return 2
        variants = sorted(want)
    random.Random(a.seed).shuffle(variants)
    mapping = {}
    os.makedirs(a.arena)
    for i, v in enumerate(variants, 1):
        rid = f"r{i:02d}"
        shutil.copytree(os.path.join(src, v), os.path.join(a.arena, rid), symlinks=True)
        mapping[rid] = v
    try:
        write_json_atomic(a.map, mapping)
    except OSError as e:
        print(f"failed to write arena map {a.map}: {e}", file=sys.stderr)
        return 1
    print(len(mapping))
    return 0


if __name__ == "__main__":
    sys.exit(main())
