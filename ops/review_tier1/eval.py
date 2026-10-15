"""Eval runner: precision/recall per check per provider, plus cost per PR.

Labelled rows (ops/analysis/review-findings): each row names the check that
should catch the finding and the gold verdict (block/hold/none). A check
"fires" on a row when its final verdict is block or hold. Recall is measured
on gold block/hold rows; precision needs human-judged negatives (sampled PRs
where the check fired but gold says none) — see the plan document.
"""

import math

SHIP_PRECISION = 0.9
SHIP_MIN_FLAGGED = 20  # a precision from fewer firings is an anecdote, not a gate


def wilson(successes, total, z=1.96):
    """95% Wilson score interval for a proportion; (0, 1) when total is 0."""
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def score(rows):
    """Aggregate gold-vs-verdict rows into per-(check, provider) tallies.

    Row: {check, provider, gold, verdict, input_tokens, cost}.
    gold/verdict in {block, hold, none}; verdict none means "did not fire".
    Returns {check: {provider: {tp, fp, fn, precision, recall, cost, n}}}.
    """
    table = {}
    for row in rows:
        check = row.get("check", "?")
        provider = row.get("provider", "?")
        cell = table.setdefault(check, {}).setdefault(
            provider, {"tp": 0, "fp": 0, "fn": 0, "cost": 0.0, "n": 0})
        gold = row.get("gold") in ("block", "hold")
        fired = row.get("verdict") in ("block", "hold")
        if gold and fired:
            cell["tp"] += 1
        elif not gold and fired:
            cell["fp"] += 1
        elif gold and not fired:
            cell["fn"] += 1
        cell["cost"] += float(row.get("cost") or 0.0)
        cell["n"] += 1
    for providers in table.values():
        for cell in providers.values():
            flagged = cell["tp"] + cell["fp"]
            positives = cell["tp"] + cell["fn"]
            cell["flagged"] = flagged
            cell["precision"] = cell["tp"] / flagged if flagged else 1.0
            cell["recall"] = cell["tp"] / positives if positives else 1.0
            cell["precision_ci"] = wilson(cell["tp"], flagged)
            cell["recall_ci"] = wilson(cell["tp"], positives)
    return table


def shippable(table, threshold=SHIP_PRECISION, min_flagged=SHIP_MIN_FLAGGED):
    """Return [(check, provider, precision, recall)] clearing the ship gate.

    The gate is precision >= threshold on at least min_flagged firings.
    """
    out = []
    for check, providers in table.items():
        for provider, cell in providers.items():
            if cell["flagged"] >= min_flagged and cell["precision"] >= threshold:
                out.append((check, provider, cell["precision"], cell["recall"]))
    return sorted(out)


def cost_per_pr(rows):
    """Total model cost divided by distinct PRs in the eval rows."""
    prs = {row.get("pr") for row in rows if row.get("pr")}
    total = sum(float(row.get("cost") or 0.0) for row in rows)
    return total / len(prs) if prs else 0.0
