#!/usr/bin/env python3
"""Audit saved bake-off inputs without changing historical scores or calling models.

  python3 trust_audit.py --cases cases.json --results results/ [--judgments judgments.json]

Counts describe scoring mechanisms, not independently verified correctness.
See TRUST_AUDIT.md for denominators, input validation and abstention limits.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import sys

import score

QUEUE_LIMIT = 100


def _text(value, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: expected nonempty string")
    return value


def _positive_int(value, context: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{context}: expected positive integer")
    return value


def _location(row, context: str, *, defect: bool = False) -> None:
    if not isinstance(row, dict):
        raise ValueError(f"{context}: expected object")
    _text(row.get("file"), f"{context} file")
    if defect:
        start = _positive_int(row.get("line_start"), f"{context} line_start")
        end = _positive_int(row.get("line_end"), f"{context} line_end")
        if start > end:
            raise ValueError(f"{context}: reversed defect range")
    else:
        _positive_int(row.get("line"), f"{context} line")
    if not isinstance(row.get("severity"), str) or row["severity"] not in score.SEVERITY_RANK:
        raise ValueError(f"{context}: invalid severity")


def _dimension(denominator: int, value, reason: str) -> dict:
    return {"status": "abstain" if value is None else "measured",
            "denominator": denominator, "value": value, "reason": reason}


def audit(cases_document: dict, results: list[tuple[str, dict]],
          judgments: dict[str, str]) -> dict:
    """Validate all rows, then report deterministic counts and an identifier-only queue.

    Ambiguous/invalid input raises ValueError, never silently drops rows. The
    caller's objects are only read. Unknown metadata is not evidence provenance.
    """
    if not isinstance(cases_document, dict) or not isinstance(cases_document.get("cases"), list):
        raise ValueError("cases: expected object with cases list")
    if not isinstance(results, list):
        raise ValueError("results: expected list of (key, result) pairs")
    if not isinstance(judgments, dict):
        raise ValueError("judgments: expected object")

    cases = {}
    defect_ids = {}
    for index, case in enumerate(cases_document["cases"]):
        if not isinstance(case, dict):
            raise ValueError(f"case row {index}: expected object")
        cid = _text(case.get("id"), f"case row {index} id")
        if cid in cases:
            raise ValueError(f"duplicate case id: {cid}")
        if not isinstance(case.get("defects"), list):
            raise ValueError(f"case {cid}: expected defects list")
        ids = set()
        for i, defect in enumerate(case["defects"]):
            context = f"case {cid} defect {i}"
            _location(defect, context, defect=True)
            did = _text(defect.get("id"), f"{context} id")
            if did in ids:
                raise ValueError(f"duplicate defect id in case {cid}: {did}")
            if did in {"invalid", "valid-other"}:
                raise ValueError(f"case {cid}: reserved defect id {did}")
            ids.add(did)
        cases[cid] = case
        defect_ids[cid] = ids

    runs = {}
    finding_ids = set()
    for row in results:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("result row: expected (key, result) pair")
        key, res = row
        _text(key, "result key")
        if key in runs:
            raise ValueError(f"duplicate result key: {key}")
        if not isinstance(res, dict):
            raise ValueError(f"result {key}: expected object")
        cid = _text(res.get("case_id"), f"result {key} case_id")
        if cid not in cases:
            raise ValueError(f"result {key}: unknown case {cid}")
        if "review" not in res:
            raise ValueError(f"result {key}: missing review")
        review = res["review"]
        if review is not None:
            if not isinstance(review, dict) or not isinstance(review.get("findings"), list):
                raise ValueError(f"result {key}: expected review object with findings list or null")
            if type(review.get("score")) is not int or not 0 <= review["score"] <= 5:
                raise ValueError(f"result {key}: expected integer review score in 0..5")
            for i, finding in enumerate(review["findings"]):
                identity = f"{key}#{i}"
                _location(finding, f"finding {identity}")
                finding_ids.add(identity)
        runs[key] = res

    for identity, label in judgments.items():
        _text(identity, "judgment key")
        if identity not in finding_ids:
            raise ValueError(f"orphan judgment: {identity}")
        key = identity.rsplit("#", 1)[0]
        allowed = defect_ids[runs[key]["case_id"]] | {"invalid", "valid-other"}
        if not isinstance(label, str) or label not in allowed:
            raise ValueError(f"invalid label for judgment {identity}")

    counts = dict.fromkeys(("automatic_location", "explicit_defect", "valid_other",
                            "invalid", "unjudged", "conflicts"), 0)
    dataset = {"cases": len(cases), "results": len(runs), "failed_results": 0,
               "findings": len(finding_ids)}
    queue = []
    queue_total = 0
    explicit_credit = 0

    def enqueue(kind: str, identity: str, reason: str) -> None:
        nonlocal queue_total
        queue_total += 1
        if len(queue) < QUEUE_LIMIT:
            queue.append({"kind": kind, "id": identity, "reason": reason})

    # Sort runs only; preserve finding indices and scorer's first-match order.
    for key, res in sorted(runs.items()):
        review = res["review"]
        if review is None:
            dataset["failed_results"] += 1
            enqueue("failed_result", key, "Saved run has no parsed review.")
            continue
        case = cases[res["case_id"]]
        for i, finding in enumerate(review["findings"]):
            identity = f"{key}#{i}"
            automatic = score.match_defect(finding, case["defects"])
            label = judgments.get(identity)
            counts["automatic_location"] += automatic is not None
            if label is None:
                counts["unjudged"] += 1
                enqueue("unjudged", identity, "No explicit finding judgment; location is not correctness.")
            elif label in {"invalid", "valid-other"}:
                counts["invalid" if label == "invalid" else "valid_other"] += 1
            else:
                counts["explicit_defect"] += 1
            if label is not None and label != "invalid" and (automatic is None or automatic == label):
                explicit_credit += 1
            if automatic is not None and label is not None and automatic != label:
                counts["conflicts"] += 1
                enqueue("judgment_conflict", identity, "Explicit judgment disagrees with automatic location credit.")

    n = dataset["findings"]
    inventory_rows = dataset["cases"] + dataset["results"]
    dimensions = {
        "dataset_audit": _dimension(
            inventory_rows, dataset if inventory_rows else None,
            "Counts of validated case and result rows (denominator: cases + results), including failed runs."
            if inventory_rows else "No case or result rows supplied."),
        "scoring_mechanism_attribution": _dimension(
            n, counts if n else None,
            "Finding counts; automatic location and explicit adjudication are overlapping axes, not correctness."
            if n else "No findings available for scoring mechanism attribution."),
        "calibration": _dimension(0, None,
            "No documented prediction target with independent outcomes; five-point quality scores are not probabilities."),
        "ordering_bias": _dimension(0, None,
            "No supported matched order comparisons with configuration provenance; list order is not an intervention."),
        "verbosity_bias": _dimension(0, None,
            "No supported matched verbosity comparisons with configuration provenance; text length is not an intervention."),
        "causal_mechanism_attribution": _dimension(0, None,
            "Scoring paths are descriptive; saved observational runs provide no controlled causal comparison."),
        "uncertainty": _dimension(0, None,
            "No supported independent correctness sample; explicit labels have no independence or sampling provenance. No interval estimated."),
    }
    return {"dimensions": dimensions,
            "coverage": {"explicit_judgments": len(judgments), "findings": n,
                         "nonconflicting_explicit_credit": explicit_credit},
            "queue": {"total": queue_total, "items": queue,
                      "truncated": queue_total > len(queue)}}


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            # Do not copy arbitrary JSON keys (possibly private text) into errors.
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError("non-finite JSON number")


def _load(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv: list[str]) -> int:
    parser = _Parser(description=__doc__.split("\n")[0])
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--judgments", type=Path)
    try:
        args = parser.parse_args(argv)
        cases = _load(args.cases)
        judgments = _load(args.judgments) if args.judgments is not None else {}
        if not args.results.is_dir():
            raise ValueError("results path: expected an existing directory")
        # stat() follows links and raises for lost targets instead of silently
        # omitting them like is_file(). Keep directories and special files out.
        results = [(path.stem, _load(path)) for path in sorted(args.results.iterdir())
                   if path.suffix == ".json" and not path.name.startswith("_")
                   and stat.S_ISREG(path.stat().st_mode)]
        report = audit(cases, results, judgments)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
