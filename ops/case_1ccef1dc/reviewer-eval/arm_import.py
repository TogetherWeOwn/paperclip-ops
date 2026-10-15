#!/usr/bin/env python3
"""Turn an arm's raw outputs from an opaque arena into the layout score.py reads.

An arena is a directory of opaque ids (r01, r02, ...) so that a reviewer can never learn from a
path whether a variant is seeded. `arena_map.json` (kept OUTSIDE the arena) maps id -> variant.
Raw outputs are `<raw>/<id>.json`, the object `reviewer_prompt_v1.md` asks for.

    arm_import.py --map arena_map.json --raw findings_raw --arm claude-sonnet-5-5 \
                  --out findings [--usage usage.json]

`usage.json` is optional: {"<id>": {"input_tokens": n, "output_tokens": n, "seconds": s}}.
A missing or unparseable raw file is written as an APPROVE with no findings and
"invalid": true, because an arm that returns nothing has approved by default; score.py counts
it, and the protocol's kill rule reads the invalid count. Exit 0; prints the invalid count.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "lib"))
from atomic_state import write_json_atomic  # noqa: E402


def parse_raw(path):
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    for candidate in (text, *re.findall(r"\{.*\}", text, flags=re.S)[:1]):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("verdict") in ("APPROVE", "CHANGES") and isinstance(obj.get("findings"), list):
            return obj
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--usage")
    a = ap.parse_args()
    mapping = json.load(open(a.map))
    usage = json.load(open(a.usage)) if a.usage else {}
    dest = os.path.join(a.out, a.arm)
    os.makedirs(dest, exist_ok=True)
    invalid = 0
    for rid, variant in sorted(mapping.items()):
        obj = parse_raw(os.path.join(a.raw, rid + ".json"))
        if obj is None:
            invalid += 1
            obj = {"verdict": "APPROVE", "findings": [], "invalid": True}
        obj["usage"] = usage.get(rid, {})
        write_json_atomic(os.path.join(dest, variant + ".json"), obj)
    print(invalid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
