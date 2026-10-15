"""Fixed JSON schemas for the three Tier-1 cheap-model checks (case_8c53addb).

Every check verdict is one of {block, hold, pass}. Checks may only block or
hold — never approve. The schema layer enforces that: anything else fails
validation and the check output is discarded (fail-closed to today's behaviour).
"""

VERDICTS = ("block", "hold", "pass")

DOCS_VS_DIFF_SCHEMA = {
    "name": "docs_vs_diff",
    "version": 1,
    "fields": {
        "section": "changed doc section under judgement",
        "holds": "bool: section is still true of the code",
        "verdict": "block | hold | pass",
        "evidence": "changed symbol or path supporting the verdict",
    },
}

AC_EVIDENCE_SCHEMA = {
    "name": "ac_evidence",
    "version": 1,
    "fields": {
        "criterion": "acceptance criterion under judgement",
        "symbol": "changed symbol evidencing it (empty if none)",
        "test": "test name evidencing it (empty if none)",
        "mapped": "bool: both symbol and test present",
        "verdict": "block | hold | pass",
    },
}

CLAIMS_VS_DIFF_SCHEMA = {
    "name": "claims_vs_diff",
    "version": 1,
    "fields": {
        "claim": "PR description claim under judgement",
        "supported": "bool: claim is supported by the diff",
        "verdict": "block | hold | pass",
        "missing": "paths or symbols the claim names that the diff lacks",
    },
}


def validate_verdict(value):
    """Return True iff value is a legal check verdict (block/hold/pass only)."""
    return value in VERDICTS


def validate_output(schema, output):
    """Fail-closed validation: all schema fields present, verdict legal."""
    if not isinstance(output, dict):
        return False
    for field in schema["fields"]:
        if field not in output:
            return False
    return validate_verdict(output.get("verdict"))


def majority_vote(votes):
    """3-vote majority over verdicts. Ties and invalid votes -> hold.

    Any invalid vote fails closed to hold; invalid votes are never ignored
    in favour of a plurality of the valid ones. Never returns anything
    outside VERDICTS; never upgrades to approve
    (there is no approve to upgrade to).
    """
    votes = list(votes)
    if any(vote not in VERDICTS for vote in votes):
        return "hold"
    counts = {}
    for vote in votes:
        counts[vote] = counts.get(vote, 0) + 1
    if not counts:
        return "hold"
    top = max(counts.values())
    winners = [v for v, c in counts.items() if c == top]
    if len(winners) != 1:
        return "hold"
    return winners[0]
