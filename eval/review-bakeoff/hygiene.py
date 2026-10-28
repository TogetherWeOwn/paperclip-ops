"""Comparable-effort validation and conservative, saved-result repeat statistics.

No inference calls. A singleton, incomplete cohort or unknown prompt identity is
unknown variance, not zero variance. Wilson intervals are not repeat spread.
"""
from __future__ import annotations

import itertools
import re
import statistics
from collections import defaultdict

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def validate_results(cases: dict, results: list[tuple[str, dict]]) -> None:
    """Reject ambiguous comparison cells; trust payload metadata, not filenames."""
    efforts, seen = set(), set()
    for key, result in results:
        if result.get("case_id") not in cases:
            continue
        effort, rep = result.get("effort"), result.get("rep")
        if effort not in EFFORTS:
            raise ValueError(f"{key}: missing or invalid reasoning effort")
        if type(rep) is not int or rep < 1:
            raise ValueError(f"{key}: rep must be a positive integer")
        model = result.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{key}: model must be recorded")
        cell = (model, result["case_id"], rep)
        if cell in seen:
            raise ValueError(f"{key}: duplicate model/case/rep cell")
        seen.add(cell)
        efforts.add(effort)
    if len(efforts) > 1:
        raise ValueError("comparison rejected: mismatched reasoning effort; score each effort separately")


def repeat_spread(cases: dict, results: list[tuple[str, dict]], judgments: dict) -> dict:
    """Full max-minus-min recall range over balanced, identical-prompt repeats.

    Cohorts may be a subset of the case catalog, but must be identical across
    every arm/repetition. Failed reviews remain zero-found observations.
    """
    # Local import keeps the legacy score_result/summarize API available to the
    # saved-result trust audit without a module-import cycle.
    import score

    cells = defaultdict(lambda: defaultdict(dict))
    efforts = defaultdict(set)
    for key, result in results:
        if result.get("case_id") in cases:
            model = result.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ValueError(f"{key}: model must be recorded")
            rep = result.get("rep")
            cells[model][rep if type(rep) is int else None][result["case_id"]] = (key, result)
            efforts[model].add(result.get("effort"))
    output = {
        model: {"effort": next(iter(efforts[model])) if len(efforts[model]) == 1 else "mixed",
                "repeats": len(reps), "recall_spread": None, "recall_mean": None,
                "reason": "repeat evidence unavailable"}
        for model, reps in cells.items()
    }
    if not output:
        return output

    def unavailable(reason):
        for entry in output.values():
            entry["reason"] = reason
        return output

    try:
        validate_results(cases, results)
    except ValueError as exc:
        return unavailable(str(exc))
    rep_ids = set(next(iter(cells.values())))
    if len(rep_ids) < 2 or any(len(reps) < 2 for reps in cells.values()):
        return unavailable("at least two independent reps per arm required")
    if any(set(reps) != rep_ids for reps in cells.values()):
        return unavailable("rep IDs differ across arms")
    first = next(iter(cells.values()))
    cohort = set(next(iter(first.values())))
    if any(set(batch) != cohort for reps in cells.values() for batch in reps.values()):
        return unavailable("case cohort differs across arms/reps")
    hashes = {}
    for reps in cells.values():
        for batch in reps.values():
            for case_id, (_, result) in batch.items():
                digest = result.get("input_sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    return unavailable("missing or invalid input SHA-256 hash")
                if case_id in hashes and hashes[case_id] != digest:
                    return unavailable("input SHA-256 hashes differ across arms/reps")
                hashes[case_id] = digest
    denominator = sum(len(cases[case_id]["defects"]) for case_id in cohort)
    if not denominator:
        return unavailable("recall undefined: cohort has no defects")
    for model, reps in cells.items():
        recalls = [sum(len(score.score_result(result, cases[case_id], judgments, key)["found"])
                       for case_id, (key, result) in batch.items()) / denominator
                   for batch in reps.values()]
        output[model].update(recall_spread=max(recalls) - min(recalls),
                             recall_mean=statistics.mean(recalls),
                             reason="observed full max-min range; not a confidence interval")
    return output


def render_comparisons(spreads: dict) -> str:
    """Never print a numeric gap unless it exceeds both full ranges combined."""
    lines = ["## Recall comparisons", "",
             "Differences require matched effort/inputs and exceed the sum of both observed full ranges."]
    for a, b in itertools.combinations(sorted(spreads), 2):
        left, right = spreads[a], spreads[b]
        if left["effort"] != right["effort"] or left["effort"] not in EFFORTS:
            lines.append(f"- {a} / {b}: withheld (mismatched or unrecorded effort).")
        elif any(entry.get(k) is None for entry in (left, right)
                 for k in ("recall_spread", "recall_mean")):
            lines.append(f"- {a} / {b}: withheld (repeat spread unavailable).")
        else:
            gap = left["recall_mean"] - right["recall_mean"]
            threshold = left["recall_spread"] + right["recall_spread"]
            if abs(gap) > threshold and not abs(abs(gap) - threshold) < 1e-12:
                lines.append(f"- {a} / {b}: recall difference {gap:+.2f}; "
                             f"combined observed spread {threshold:.2f} (descriptive, not significance).")
            else:
                lines.append(f"- {a} / {b}: withheld (difference does not exceed observed spread).")
    return "\n".join(lines)
