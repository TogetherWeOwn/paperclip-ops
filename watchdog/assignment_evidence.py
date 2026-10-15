#!/usr/bin/env python3
"""Normalize a local, source-backed assignment bundle for watchdog/detectors.py.

This command does no discovery: it copies explicit evidence from a local JSON
bundle and records its source references. Missing or incomplete evidence is
preserved as a gap for the detector to report as unknown.
"""

import argparse
import json
import math
import os
import sys

try:
    from watchdog.assignment_contract import (
        INPUT_SCHEMA, ISSUE_FIELDS, OUTPUT_SCHEMA, PROVENANCE_SOURCES,
        SOURCE_NAMES,
    )
except ImportError:  # run as a script: sys.path[0] is watchdog/, not the root
    from assignment_contract import (
        INPUT_SCHEMA, ISSUE_FIELDS, OUTPUT_SCHEMA, PROVENANCE_SOURCES,
        SOURCE_NAMES,
    )


def _source_record(value):
    if not isinstance(value, dict):
        return {"complete": False, "ref": None}
    ref = value.get("ref")
    valid_ref = isinstance(ref, str) and bool(ref.strip())
    complete = value.get("complete") is True and valid_ref
    return {"complete": complete, "ref": ref if valid_ref else None}


def _incomplete_sources():
    return {name: {"complete": False, "ref": None} for name in SOURCE_NAMES}


def _valid_issue_value(field, value):
    if field in ("id", "identifier"):
        return isinstance(value, str)
    if field in ("assigneeAgentId", "prAuthorAgentId"):
        return value is None or isinstance(value, str)
    if field in ("inFocus", "isReview"):
        return isinstance(value, bool)
    if field == "inBacklogMin":
        if value is None or isinstance(value, (str, int)):
            return not isinstance(value, bool)
        return isinstance(value, float) and math.isfinite(value)
    return False


def _provenance_row(value):
    if not isinstance(value, dict):
        return None
    normalized = {}
    for field in PROVENANCE_SOURCES:
        item = value.get(field)
        if not isinstance(item, dict):
            continue
        entry = {key: item[key] for key in ("source", "ref")
                 if isinstance(item.get(key), str)}
        if entry:
            normalized[field] = entry
    return normalized


def _issue_rows(value):
    if not isinstance(value, list):
        return None
    rows = []
    for row in value:
        if not isinstance(row, dict):
            # Keep the unreadable row visible without copying arbitrary input.
            rows.append(None)
            continue
        normalized = {
            key: row[key] for key in ISSUE_FIELDS
            if key in row and _valid_issue_value(key, row[key])
        }
        if "provenance" in row:
            normalized_provenance = _provenance_row(row["provenance"])
            if normalized_provenance is not None:
                normalized["provenance"] = normalized_provenance
        rows.append(normalized)
    return rows


def build_snapshot(bundle):
    """Return a partial detector snapshot without inferring assignment fields."""
    if not isinstance(bundle, dict):
        bundle = {}
        schema_valid = False
    else:
        schema_valid = bundle.get("schema") == INPUT_SCHEMA

    rows = _issue_rows(bundle.get("issues"))
    sources_in = bundle.get("sources")
    sources = {
        name: _source_record(sources_in.get(name)
                             if isinstance(sources_in, dict) else None)
        for name in SOURCE_NAMES
    }
    coverage_complete = (
        schema_valid and rows is not None
        and all(source["complete"] for source in sources.values())
    )
    evidence = {
        "schema": OUTPUT_SCHEMA,
        "complete": coverage_complete,
        "sources": sources,
    }
    now = bundle.get("now")
    return {
        "now": now if isinstance(now, str) else None,
        "issues": rows,
        "assignmentEvidence": evidence,
    }


def _reject_nonstandard_constant(value):
    raise ValueError(f"non-standard JSON constant: {value}")


def _read_bundle(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream, parse_constant=_reject_nonstandard_constant), True
    except (OSError, UnicodeError, ValueError):
        return None, False


def _write_new_snapshot(path, snapshot):
    contents = json.dumps(snapshot, ensure_ascii=True, allow_nan=False,
                          sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(contents)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Normalize a local assignment-evidence bundle; no network access.")
    parser.add_argument("--input", required=True,
                        help="local JSON source-evidence bundle")
    parser.add_argument("--out", required=True,
                        help="new snapshot path (existing files are never overwritten)")
    args = parser.parse_args(argv)

    bundle, readable = _read_bundle(args.input)
    snapshot = build_snapshot(bundle)
    try:
        _write_new_snapshot(args.out, snapshot)
    except OSError:
        print("watchdog-assignment-evidence: cannot create output", file=sys.stderr)
        return 2
    if not readable:
        print("watchdog-assignment-evidence: input unreadable; wrote incomplete evidence",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
