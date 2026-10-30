#!/usr/bin/env python3
"""Run a batch: every (model x case) not already in the results directory.

  python3 batch.py --repo CLONE --cases dataset/cases.json --models gpt-6.1-sol gpt-6-astra \\
      --effort medium --key-env ANTHROPIC_AUTH_TOKEN --out results/codex-arms --concurrency 3

Resumable: a result file that already exists is skipped, so a killed batch can be
restarted without paying for finished reviews twice. In-flight reviews are
capped at concurrency x models, dealt round-robin; a failed review is written as a
result (review=null) so it counts against the arm instead of vanishing.
Poll Keeper for the arm's lane before and after (docs/review-bot/bakeoff-protocol.md, 6).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time

import run_review


def result_path(out: str, case_id: str, model: str, effort: str, rep: int) -> str:
    return os.path.join(out, f"{case_id}__{model.replace('/', '_')}__{effort}__r{rep}.json")


def plan(cases: list[dict], models: list[str], effort: str, rep: int, out: str) -> list[tuple[str, str]]:
    return [(c["id"], m) for m in models for c in cases
            if not os.path.exists(result_path(out, c["id"], m, effort, rep))]


def run(repo, cases, models, effort, rep, key_env, out, concurrency, timeout, runner=run_review.run_case) -> dict:
    by_id = {c["id"]: c for c in cases}
    todo = plan(cases, models, effort, rep, out)
    os.makedirs(out, exist_ok=True)
    started = time.time()
    done = failed = 0

    def one(item):
        case_id, model = item
        try:
            res = runner(repo, by_id[case_id], model, effort, key_env, timeout)
        except Exception as exc:  # a crash is a failed review, not a vanished one
            res = {"case_id": case_id, "model": model, "effort": effort, "latency_s": 0.0,
                   "exit_code": None, "timed_out": False, "usage": {}, "errors": [str(exc)[:300]],
                   "prompt_chars": 0, "review": None, "parse_error": "harness error",
                   "evidence_gate": None, "raw_final": None}
        res["rep"] = rep
        with open(result_path(out, case_id, model, effort, rep), "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
        return res

    per_model = {m: [i for i in todo if i[1] == m] for m in models}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency * len(models))) as pool:
        # round-robin so every model gets at most `concurrency` reviews in flight at the start
        queue = []
        while any(per_model.values()):
            for m in models:
                if per_model[m]:
                    queue.append(per_model[m].pop(0))
        for res in pool.map(one, queue):
            done += 1
            failed += res["review"] is None
    return {"planned": len(todo), "done": done, "failed": failed, "seconds": round(time.time() - started, 1)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--effort", default="medium", choices=run_review.EFFORTS)
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--key-env", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--case-ids", nargs="*", help="restrict to these case ids")
    args = ap.parse_args(argv)
    if args.rep < 1:
        ap.error("--rep must be a positive integer")
    cases = json.load(open(args.cases, encoding="utf-8"))["cases"]
    if args.case_ids:
        cases = [c for c in cases if c["id"] in set(args.case_ids)]
    summary = run(args.repo, cases, args.models, args.effort, args.rep, args.key_env, args.out,
                  args.concurrency, args.timeout)
    print(json.dumps(summary))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
