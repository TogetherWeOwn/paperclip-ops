"""Shared format and provenance rules for offline assignment evidence."""

INPUT_SCHEMA = "watchdog.assignment-evidence-input.v1"
OUTPUT_SCHEMA = "watchdog.assignment-evidence.v1"
ISSUE_FIELDS = (
    "id", "identifier", "inFocus", "inBacklogMin", "assigneeAgentId",
    "isReview", "prAuthorAgentId",
)
SOURCE_NAMES = ("issues", "focus", "reviews")
PROVENANCE_SOURCES = {
    "inFocus": frozenset({"focus-membership", "explicit-focus-field"}),
    "inBacklogMin": frozenset({"status-history"}),
    "isReview": frozenset({"review-request", "review-inventory"}),
    "prAuthorAgentId": frozenset({"review-request", "code-authorship"}),
}


def has_source_ref(value):
    return isinstance(value, str) and bool(value.strip())


def valid_coverage(evidence):
    if not isinstance(evidence, dict) \
            or evidence.get("schema") != OUTPUT_SCHEMA \
            or evidence.get("complete") is not True:
        return False
    sources = evidence.get("sources")
    if not isinstance(sources, dict):
        return False
    return all(
        isinstance(sources.get(name), dict)
        and sources[name].get("complete") is True
        and has_source_ref(sources[name].get("ref"))
        for name in SOURCE_NAMES
    )


def valid_provenance(row, field):
    provenance = row.get("provenance")
    item = provenance.get(field) if isinstance(provenance, dict) else None
    if not isinstance(item, dict):
        return False
    source = item.get("source")
    if not isinstance(source, str) \
            or source not in PROVENANCE_SOURCES[field] \
            or not has_source_ref(item.get("ref")):
        return False
    if field == "isReview":
        expected = "review-request" if row.get("isReview") is True else "review-inventory"
        return source == expected
    return True
