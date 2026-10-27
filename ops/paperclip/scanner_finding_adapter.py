#!/usr/bin/env python3
"""Pure offline adapter: secret-scan outcome -> native finding payload.

Maps one completed run of ``verification/secret-scan.sh`` (pinned below by
git blob id) to a native issue-create or issue-update payload, or to a
refusal. The native issue is the finding record; this module invents no
packet format, database or exporter, holds no credential, and makes no
network calls. Submitting a payload is the caller's job, under the
caller's own authority.

Only the pinned scanner's documented exit contract is encoded
(``verification/secret-scan.sh``: "Exit 0 clean, 1 finding"). Every other
scanner state -- another exit code, spawn failure, incomplete or shallow
scan, truncated output, non-empty stderr, or output this parser cannot
account for line by line -- is UNKNOWN: the adapter emits a refusal,
creates nothing, closes nothing, and the CLI exits non-zero.

Payloads carry metadata only: the logical identity (repository slug +
rule id + repo-relative path), scanner name and pinned script blob,
immutable source head, bounded occurrence line numbers and count, and
caller-supplied evidence links. Matched content is parsed only far
enough to read the ``path:line:`` prefix of a ``git grep -n`` line and
is discarded immediately; neither matched content nor any hash of it
appears in the output.

Replay contract (bounded; perpetual uniqueness is NOT claimed): the
create idempotency key is derived deterministically from the logical
identity only -- scanner version, source head and line numbers are
revision evidence, not identity. That key deduplicates duplicate
deliveries inside the native create-key retention window (7 days).
After that window the adapter never creates blindly: a re-delivery must
bind to exactly one verified existing open finding id supplied by the
caller, and then only an update payload is emitted. Missing, unknown-age
or ambiguous bindings refuse.

Closure is never emitted on a clean scan alone. A closure payload
requires an explicit closure request carrying exactly one existing
finding id, an exact fix-head equal to this scan's source head, zero
occurrences for the identity in this scan, the same pinned scanner
blob as the original detection, and a non-empty independent-acceptance
reference. The adapter checks that the reference is present; verifying
that the acceptance is genuine stays with the submitter and the
accepting reviewer.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from urllib.parse import urlsplit

SCANNER_NAME = "secret-scan"
SCANNER_SCRIPT_PATH = "verification/secret-scan.sh"
# Git blob id of verification/secret-scan.sh at the pinned head. A scanner
# change must update this pin (and re-review the exit/output contract) in
# the same change; the test suite recomputes the blob from the file.
SCANNER_SCRIPT_BLOB = "5e438b9a6b7a10b53e68b41f6721b0767ceecbc7"

WINDOW_DAYS = 7
MAX_INPUT_BYTES = 1048576
MAX_FINDINGS = 100
MAX_LINES_LISTED = 50
MAX_EVIDENCE_LINKS = 10
MAX_PATH_CHARS = 512
MAX_LINE_NUMBER = 10000000

# The pinned scanner's stderr annotations, in emission order. Buffered
# ``git grep -n`` lines take the rule of the annotation that follows them.
ERROR_RULES = {
    "a GitHub-token-shaped string is committed": "github-token",
    "private key material is committed": "private-key",
    "a Discord-webhook-shaped URL is committed": "discord-webhook",
    "a Stripe-live-key-shaped string is committed": "stripe-live-key",
}
GITIGNORE_ERROR_PREFIX = ".gitignore no longer covers "
GITIGNORE_RULE = "gitignore-coverage"
GITIGNORE_PATH = ".gitignore"
CLEAN_LINE = "secret scan: clean"
RULE_IDS = frozenset(ERROR_RULES.values()) | {GITIGNORE_RULE}

REASONS = {
    "invalid_input": "input is not a single JSON object of the expected shape",
    "input_too_large": "input exceeds the size bound",
    "unknown_field": "input carries a field this adapter does not accept",
    "scanner_unknown": "scanner name is not the pinned scanner",
    "scanner_pin_mismatch": "scanner script blob is not the pinned blob",
    "scanner_spawn_failed": "scanner did not spawn successfully",
    "scanner_exit_unknown": "scanner exit code is outside the documented 0/1 contract",
    "scan_incomplete": "scan is not marked complete",
    "scan_shallow": "scan is marked shallow",
    "scan_truncated": "scanner output is marked truncated",
    "scanner_stderr": "scanner produced non-empty stderr",
    "scan_output_unrecognized": "scanner output contains a line this adapter cannot account for",
    "scan_exit_inconsistent": "scanner exit code does not match the parsed occurrences",
    "identity_invalid": "repository slug, rule id or path is not a valid finding identity",
    "head_invalid": "source head is not an immutable commit id",
    "evidence_invalid": "evidence links must be bounded https URLs",
    "responder_missing": "a create payload needs a responder parameter",
    "reference_invalid": "an opaque reference parameter is malformed",
    "delivery_invalid": "a delivery entry is malformed",
    "unknown_delivery_age": "delivery age is unknown, so the replay window cannot be proven",
    "ambiguous_delivery": "more than one delivery entry names the same identity",
    "unmatched_delivery": "a delivery entry names no finding in this scan",
    "ambiguous_binding": "more than one existing finding id is bound to one identity",
    "post_window_unbound": "re-delivery after the replay window has no verified existing finding id",
    "closure_binding": "closure needs exactly one existing finding id",
    "closure_head_mismatch": "closure fix head does not equal this scan's source head",
    "closure_scanner_version": "closure retest did not use the pinned scanner of the original detection",
    "closure_path_ambiguous": "closure path contains a colon, which the scanner's path:line: output cannot attribute unambiguously",
    "closure_still_present": "closure identity still has occurrences in this scan",
    "closure_acceptance_missing": "closure needs an independent-acceptance reference",
    "too_many_findings": "scan produced more logical findings than the adapter bound",
}


class Refusal(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _refuse(code):
    raise Refusal(code)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_fields(obj, allowed, required=()):
    if not isinstance(obj, dict):
        _refuse("invalid_input")
    if set(obj) - set(allowed):
        _refuse("unknown_field")
    if set(required) - set(obj):
        _refuse("invalid_input")


def _valid_opaque(value, max_chars=200):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= max_chars
        and not any(ch.isspace() or ord(ch) < 32 for ch in value)
    )


def _validate_repo_slug(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value
    ):
        _refuse("identity_invalid")
    return value


def _validate_head(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", value):
        _refuse("head_invalid")
    return value


def _validate_path(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_PATH_CHARS
        or value.startswith("/")
        or "\\" in value
        or any(ord(ch) < 32 for ch in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        # A pinned-scanner coverage finding names the file itself.
        if value != GITIGNORE_PATH:
            _refuse("identity_invalid")
    return value


def _validate_rule(value):
    if not isinstance(value, str) or value not in RULE_IDS:
        _refuse("identity_invalid")
    return value


def _is_https_ref(value):
    if not isinstance(value, str) or not value.startswith("https://"):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        # Accessing port also validates its syntax and range.
        parsed.port
        if not host:
            return False
        if ":" in host:
            # urlsplit validates bracketed IPv6 addresses.
            return parsed.netloc.startswith("[")
        host = host.encode("idna").decode("ascii").removesuffix(".")
    except (ValueError, UnicodeError):
        return False
    return len(host) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.split(".")
    )


def _validate_evidence(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_LINKS:
        _refuse("evidence_invalid")
    for link in value:
        if (
            not isinstance(link, str)
            or not _is_https_ref(link)
            or not _valid_opaque(link, max_chars=500)
        ):
            _refuse("evidence_invalid")
    return list(value)


def _group():
    return {"lines": set(), "count": 0}


def _add_lines(grouped, rule_id, path, lines):
    entry = grouped.setdefault((rule_id, path), _group())
    entry["lines"].update(lines)
    entry["count"] = len(entry["lines"]) if entry["lines"] else entry["count"] + 1


_GREP_LINE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>[0-9]{1,8}):")
_GREP_SPLIT = re.compile(r"(?=:[0-9]+:)")


def _parse_raw(raw):
    """Parse pinned scanner stdout into grouped occurrences.

    Only the ``path:line:`` prefix of a grep line is read; the matched
    content after it is never stored. Any line that is not a grep line,
    a known ``::error::`` annotation or the clean line refuses.
    """
    if not isinstance(raw, str) or len(raw.encode("utf-8", "ignore")) > MAX_INPUT_BYTES:
        _refuse("input_too_large" if isinstance(raw, str) else "invalid_input")
    grouped = {}
    buffered = []
    clean_seen = False
    # Split ONLY on LF: str.splitlines() also breaks on U+2028 etc., which
    # would let matched content forge a scanner line (and leak into a path).
    raw_lines = raw.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    for line in raw_lines:
        if line.endswith("\r"):
            line = line[:-1]
        if line == CLEAN_LINE:
            clean_seen = True
            continue
        if line.startswith("::error::"):
            message = line[len("::error::"):]
            if message in ERROR_RULES:
                rule_id = ERROR_RULES[message]
                for path, lineno in buffered:
                    _add_lines(grouped, rule_id, path, [lineno])
                buffered = []
            elif message.startswith(GITIGNORE_ERROR_PREFIX) and message != GITIGNORE_ERROR_PREFIX:
                _add_lines(grouped, GITIGNORE_RULE, GITIGNORE_PATH, [])
            else:
                _refuse("scan_output_unrecognized")
            continue
        match = _GREP_LINE.match(line)
        # A second split point may be in a colon-bearing filename or in
        # matched content. Neither attribution can be proven; refuse both.
        # Lookahead counts overlapping splits too (e.g. "a:5:2:x").
        if match is None or len(_GREP_SPLIT.findall(line)) != 1:
            _refuse("scan_output_unrecognized")
        lineno = int(match.group("line"))
        if not 1 <= lineno <= MAX_LINE_NUMBER:
            _refuse("scan_output_unrecognized")
        buffered.append((_validate_path(match.group("path")), lineno))
    if buffered:
        _refuse("scan_output_unrecognized")
    if clean_seen and grouped:
        _refuse("scan_exit_inconsistent")
    return grouped, clean_seen


def _parse_structured(occurrences):
    if not isinstance(occurrences, list):
        _refuse("invalid_input")
    grouped = {}
    for item in occurrences:
        _check_fields(item, {"rule_id", "path", "lines"}, {"rule_id", "path", "lines"})
        rule_id = _validate_rule(item["rule_id"])
        path = _validate_path(item["path"])
        lines = item["lines"]
        if not isinstance(lines, list) or len(lines) > 1000:
            _refuse("invalid_input")
        for lineno in lines:
            if not _is_int(lineno) or not 1 <= lineno <= MAX_LINE_NUMBER:
                _refuse("invalid_input")
        if rule_id == GITIGNORE_RULE:
            if path != GITIGNORE_PATH or lines:
                _refuse("identity_invalid")
        elif not lines:
            _refuse("invalid_input")
        _add_lines(grouped, rule_id, path, lines)
    return grouped


def _identity_marker(repo_slug, rule_id, path):
    return json.dumps(
        {"path": path, "repo_slug": repo_slug, "rule_id": rule_id},
        sort_keys=True,
        separators=(",", ":"),
    )


def _idempotency_key(repo_slug, rule_id, path):
    # Hash of the identity marker (metadata only: repository, rule, path).
    # Matched content never reaches this module's output, hashed or not.
    digest = hashlib.sha256(_identity_marker(repo_slug, rule_id, path).encode()).hexdigest()
    return f"scanner-finding:v1:{digest}"


def _finding_lines_text(entry):
    listed = sorted(entry["lines"])[:MAX_LINES_LISTED]
    if not listed:
        return "no line numbers (coverage finding)"
    text = ", ".join(str(n) for n in listed)
    if entry["count"] > len(listed):
        text += f" (first {len(listed)} of {entry['count']}; list truncated, finding stands)"
    return text


def _description(repo_slug, rule_id, path, entry, source_head, evidence_links):
    lines = [
        "Scanner finding (metadata only; matched content is never included).",
        "",
        f"- Finding identity: repo `{repo_slug}`, rule `{rule_id}`, path `{path}`",
        f"- Identity marker: `{_identity_marker(repo_slug, rule_id, path)}`",
        f"- Scanner: {SCANNER_NAME} (`{SCANNER_SCRIPT_PATH}`, blob `{SCANNER_SCRIPT_BLOB}`)",
        f"- Source head: `{source_head}`",
        f"- Occurrences: {entry['count']} (lines: {_finding_lines_text(entry)})",
    ]
    for link in evidence_links:
        lines.append(f"- Evidence: {link}")
    lines += [
        f"- Replay: creates carry a deterministic idempotency key derived from the "
        f"finding identity, deduplicated inside the native {WINDOW_DAYS}-day window; "
        "after that window, updates bind to a verified existing finding id only. "
        "Perpetual uniqueness is not claimed.",
        "- Closure: only an exact fix-head retest with zero occurrences for this "
        "identity, plus independent acceptance.",
    ]
    return "\n".join(lines)


def _parse_deliveries(raw, repo_slug):
    deliveries = {}
    if raw is None:
        return deliveries
    if not isinstance(raw, list) or len(raw) > MAX_FINDINGS:
        _refuse("delivery_invalid")
    for item in raw:
        _check_fields(
            item,
            {"rule_id", "path", "repo_slug", "age_days", "existing_issue_ids"},
            {"rule_id", "path", "age_days"},
            )
        if "repo_slug" in item and item["repo_slug"] != repo_slug:
            _refuse("delivery_invalid")
        key = (_validate_rule(item["rule_id"]), _validate_path(item["path"]))
        if key in deliveries:
            _refuse("ambiguous_delivery")
        age = item["age_days"]
        if age is None:
            _refuse("unknown_delivery_age")
        if isinstance(age, bool) or not isinstance(age, (int, float)) \
                or (isinstance(age, float) and not math.isfinite(age)) or age < 0:
            _refuse("delivery_invalid")
        ids = item.get("existing_issue_ids", [])
        if not isinstance(ids, list) or any(not _valid_opaque(i) for i in ids) \
                or len(set(ids)) != len(ids):
            _refuse("delivery_invalid")
        deliveries[key] = {"age_days": age, "existing_issue_ids": list(ids)}
    return deliveries


def _closure_payload(request, repo_slug, grouped, source_head):
    if request is None:
        return None
    _check_fields(
        request,
        {"rule_id", "path", "repo_slug", "existing_issue_ids", "fix_head",
         "original_scanner_script_blob", "independent_acceptance_ref"},
        {"rule_id", "path", "existing_issue_ids", "fix_head",
         "original_scanner_script_blob", "independent_acceptance_ref"},
    )
    if "repo_slug" in request and request["repo_slug"] != repo_slug:
        _refuse("identity_invalid")
    rule_id = _validate_rule(request["rule_id"])
    path = _validate_path(request["path"])
    if ":" in path:
        # ``git grep -n`` prints ``path:line:content``; a path holding
        # ``:<digits>:`` is read as a shorter path, so "zero occurrences"
        # for it cannot be proven from this output. Never close on it.
        _refuse("closure_path_ambiguous")
    ids = request["existing_issue_ids"]
    if not isinstance(ids, list) or len(ids) != 1 or not _valid_opaque(ids[0]):
        _refuse("closure_binding")
    if _validate_head(request["fix_head"]) != source_head:
        _refuse("closure_head_mismatch")
    if request["original_scanner_script_blob"] != SCANNER_SCRIPT_BLOB:
        # Same pinned scanner only: a different blob is unorderable offline
        # and its output contract is unreviewed, so it cannot close.
        _refuse("closure_scanner_version")
    acceptance = request["independent_acceptance_ref"]
    if not _is_https_ref(acceptance) or not _valid_opaque(acceptance, max_chars=500):
        _refuse("closure_acceptance_missing")
    if (rule_id, path) in grouped:
        _refuse("closure_still_present")
    comment = (
        "Closure retest (metadata only).\n\n"
        f"- Finding identity: repo `{repo_slug}`, rule `{rule_id}`, path `{path}`\n"
        f"- Fix head: `{source_head}` (equals this retest's source head)\n"
        f"- Scanner: {SCANNER_NAME} (`{SCANNER_SCRIPT_PATH}`, blob `{SCANNER_SCRIPT_BLOB}`), "
        "same pinned scanner as the original detection\n"
        "- Occurrences for this identity at the fix head: 0\n"
        f"- Independent acceptance: {acceptance}"
    )
    return {"issue_update": {"issueId": ids[0], "status": "done", "comment": comment}}


def adapt(outcome):
    """Map one scanner outcome to a payload / none / refusal dict."""
    try:
        return _adapt(outcome)
    except Refusal as refusal:
        return {
            "status": "refusal",
            "reason_code": refusal.code,
            "reason": REASONS.get(refusal.code, "refused"),
            "findings": [],
            "closure": None,
        }


def _adapt(outcome):
    _check_fields(
        outcome,
        {"schema_version", "repo_slug", "source_head", "scanner", "scan",
         "responder", "parent_ref", "project_ref", "evidence_links",
         "deliveries", "closure_request"},
        {"schema_version", "repo_slug", "source_head", "scanner", "scan"},
    )
    if not _is_int(outcome["schema_version"]) or outcome["schema_version"] != 1:
        _refuse("invalid_input")
    repo_slug = _validate_repo_slug(outcome["repo_slug"])
    source_head = _validate_head(outcome["source_head"])

    scanner = outcome["scanner"]
    _check_fields(scanner, {"name", "script_blob"}, {"name", "script_blob"})
    if scanner["name"] != SCANNER_NAME:
        _refuse("scanner_unknown")
    if scanner["script_blob"] != SCANNER_SCRIPT_BLOB:
        _refuse("scanner_pin_mismatch")

    scan = outcome["scan"]
    _check_fields(
        scan,
        {"exit_code", "spawn_ok", "complete", "shallow", "truncated",
         "stderr_nonempty", "raw_stdout", "occurrences"},
        {"exit_code", "spawn_ok", "complete", "shallow", "truncated", "stderr_nonempty"},
    )
    if scan["spawn_ok"] is not True:
        _refuse("scanner_spawn_failed")
    if scan["complete"] is not True:
        _refuse("scan_incomplete")
    if scan["shallow"] is not False:
        _refuse("scan_shallow")
    if scan["truncated"] is not False:
        _refuse("scan_truncated")
    if scan["stderr_nonempty"] is not False:
        _refuse("scanner_stderr")
    if not _is_int(scan["exit_code"]) or scan["exit_code"] not in (0, 1):
        _refuse("scanner_exit_unknown")
    if ("raw_stdout" in scan) == ("occurrences" in scan):
        _refuse("invalid_input")
    if "raw_stdout" in scan:
        grouped, clean_seen = _parse_raw(scan["raw_stdout"])
        if scan["exit_code"] == 0 and not clean_seen:
            _refuse("scan_exit_inconsistent")
        if scan["exit_code"] == 1 and clean_seen:
            _refuse("scan_exit_inconsistent")
    else:
        grouped = _parse_structured(scan["occurrences"])
    if (scan["exit_code"] == 0) != (not grouped):
        _refuse("scan_exit_inconsistent")
    if len(grouped) > MAX_FINDINGS:
        _refuse("too_many_findings")

    evidence_links = _validate_evidence(outcome.get("evidence_links"))
    responder = outcome.get("responder")
    if responder is not None and not _valid_opaque(responder):
        _refuse("reference_invalid")
    parent_ref = outcome.get("parent_ref")
    project_ref = outcome.get("project_ref")
    for ref in (parent_ref, project_ref):
        if ref is not None and not _valid_opaque(ref):
            _refuse("reference_invalid")

    deliveries = _parse_deliveries(outcome.get("deliveries"), repo_slug)
    for key in deliveries:
        if key not in grouped:
            _refuse("unmatched_delivery")

    closure = _closure_payload(outcome.get("closure_request"), repo_slug, grouped, source_head)

    findings = []
    for (rule_id, path), entry in sorted(grouped.items()):
        delivery = deliveries.get((rule_id, path))
        description = _description(repo_slug, rule_id, path, entry, source_head, evidence_links)
        finding = {
            "identity": {"repo_slug": repo_slug, "rule_id": rule_id, "path": path},
            "identity_marker": _identity_marker(repo_slug, rule_id, path),
            "occurrence_count": entry["count"],
            "lines": sorted(entry["lines"])[:MAX_LINES_LISTED],
            "lines_truncated": len(entry["lines"]) > MAX_LINES_LISTED,
            "scanner": {"name": SCANNER_NAME, "script_path": SCANNER_SCRIPT_PATH,
                        "script_blob": SCANNER_SCRIPT_BLOB},
            "source_head": source_head,
            "evidence_links": evidence_links,
        }
        if delivery is not None and len(delivery["existing_issue_ids"]) > 1:
            _refuse("ambiguous_binding")
        if delivery is not None and len(delivery["existing_issue_ids"]) == 1:
            finding["action"] = "update"
            finding["issue_update"] = {
                "issueId": delivery["existing_issue_ids"][0],
                "comment": "Scanner re-delivery (metadata only).\n\n" + description,
            }
        elif delivery is not None and delivery["age_days"] >= WINDOW_DAYS:
            _refuse("post_window_unbound")
        else:
            if responder is None:
                _refuse("responder_missing")
            create = {
                "title": f"Secret scan finding: {rule_id} in {path}"[:200],
                "description": description,
                "idempotencyKey": _idempotency_key(repo_slug, rule_id, path),
                "assigneeAgentId": responder,
            }
            if parent_ref is not None:
                create["parentId"] = parent_ref
            if project_ref is not None:
                create["projectId"] = project_ref
            finding["action"] = "create"
            finding["issue_create"] = create
        findings.append(finding)

    if not findings and closure is None:
        return {"status": "none", "findings": [], "closure": None}
    return {"status": "payload", "findings": findings, "closure": closure}


def _strict_loads(text):
    def no_duplicates(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj

    def no_constants(value):
        raise ValueError("nonfinite constant")

    return json.loads(text, object_pairs_hook=no_duplicates, parse_constant=no_constants)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Map a pinned secret-scan outcome to a native finding payload or refusal.")
    parser.add_argument("--input", help="read the outcome JSON from this path instead of stdin")
    args = parser.parse_args(argv)
    try:
        if args.input:
            with open(args.input, "rb") as handle:
                data = handle.read(MAX_INPUT_BYTES + 1)
        else:
            data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except OSError:
        data = None
    if data is None:
        result = adapt({})
    elif len(data) > MAX_INPUT_BYTES:
        result = {"status": "refusal", "reason_code": "input_too_large",
                  "reason": REASONS["input_too_large"], "findings": [], "closure": None}
    else:
        try:
            outcome = _strict_loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            outcome = {}
        result = adapt(outcome)
    # The refusal and payload shapes never echo unvalidated input, so it is
    # safe to print the result even when the input held matched content.
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in ("payload", "none") else 2


if __name__ == "__main__":
    sys.exit(main())
