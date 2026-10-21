#!/usr/bin/env python3
"""migration_drift_gate.py -- preflight gate: candidate repo digests vs the
production migration ledger (shared migration safety gates).

WHY THIS EXISTS. A build whose migrations are ahead of the production ledger
ships code that requires unapplied migrations; a ledger ahead of the repo
means out-of-band DDL happened in production. Both are refused here, before
deploy, while rollback and live-SHA redeploy paths waive this gate inside
their own scripts so recovery never waits on it.

Keys match the runtime the way paperclip-upgrade/migration_gate.py does:
files are keyed by sha256 of content, compared against ledger hashes.

Inputs (all files, so callers fetch them with their own read-only
principals; this gate never touches the network or a database):
  --sql-hashes FILE   sha256sum output over every <tag>.sql of the
                      candidate (extra whitespace-separated fields ignored
                      is NOT allowed here: strict sha256sum format so a
                      truncated digest cannot compare equal)
  --ledger FILE       production ledger hashes, one sha256 per line
                      (extra whitespace-separated fields ignored, so raw
                      `select hash ...` output works)
  --evidence FILE     rehearsal evidence document from migration_rehearsal.sh
  --max-age-hours N   rehearsal evidence freshness requirement (default 24)
  --json              emit one machine-readable verdict object on stdout
                      instead of text lines (see JSON_SCHEMA_VERSION)

Output: one line per check on stdout ("ok: ..." / "FAIL: ..."); never row
data beyond tags and counts. No secrets are read or printed. With --json,
stdout is exactly one versioned object; diagnostics never carry a traceback
or raw evidence content.

Exit: 0 all gates pass | 1 a gate failed | 2 bad input (REFUSED).
"""
import argparse
import datetime
import json
import re
import sys

HEX64 = re.compile(r"[0-9a-f]{64}")
HASH_LINE = re.compile(r"([0-9a-f]{64})\s+\*?(?:.*/)?([^/\s]+)\.sql")

JSON_SCHEMA_VERSION = 1

# Stable reason -> actionable next step. Every non-ok reason the gate can
# emit has exactly one entry here; verdict objects reference reasons only.
NEXT_ACTIONS = {
    "pending_without_evidence":
        "Rehearse the pending migrations and attach fresh evidence, "
        "or narrow the candidate to already-ledgered tags.",
    "unknown_ledger_rows":
        "Inspect the production ledger for out-of-band DDL and reconcile "
        "the candidate repo before retrying.",
    "evidence_status_not_passed":
        "Re-run the rehearsal until status is passed, then re-submit "
        "the evidence.",
    "evidence_cleanup_unconfirmed":
        "Delete the rehearsal branch and confirm cleanup before "
        "re-submitting.",
    "evidence_target_mismatch":
        "Re-run the rehearsal against the created branch so apply_target "
        "matches.",
    "evidence_unparseable_time":
        "Re-run the rehearsal to record a parseable finished_at timestamp.",
    "evidence_future":
        "Check the rehearsal clock: finished_at is in the future; re-run "
        "the rehearsal with correct time.",
    "evidence_stale":
        "Re-run the rehearsal for fresh evidence within max-age "
        "(24h default) and re-submit.",
    "evidence_not_applied":
        "Re-run the rehearsal until every migration result is applied.",
    "evidence_unbound":
        "Rehearse the exact pending chain so evidence tags and digest "
        "hashes are bound to this candidate.",
    "evidence_order_mismatch":
        "Re-emit evidence with migrations in filename sort order.",
    "evidence_invalid_json":
        "Fix the evidence file so it parses as JSON and re-submit.",
    "evidence_invalid_root":
        "Fix the evidence shape: top-level JSON must be an object.",
    "evidence_invalid_version":
        "Fix the evidence shape: set version to 1 and re-submit.",
    "evidence_invalid_migrations":
        "Fix the evidence shape: migrations must be a list of rows.",
    "evidence_invalid_row":
        "Fix the evidence shape: each migration row must be an object "
        "with tag, sha256 and result.",
    "evidence_invalid_field":
        "Fix the evidence field types and shape: each migration row needs "
        "a string tag and 64-hex sha256.",
    "evidence_duplicate_tag":
        "Remove duplicate migration tags from the evidence and re-run "
        "the rehearsal.",
    "input_empty_candidate":
        "Provide a non-empty candidate sql-hashes file and retry.",
    "input_malformed_hashes":
        "Fix the candidate sql-hashes file: each line needs a 64-hex "
        "hash and tag.",
    "input_empty_ledger":
        "Provide a non-empty production ledger file and retry.",
    "input_malformed_ledger":
        "Fix the ledger file so every row carries exactly one 64-hex hash.",
    "input_invalid_max_age":
        "Pass --max-age-hours >= 0 and retry.",
}

_MISSING = object()


class _InputError(Exception):
    """Bad input: fails closed with exit 2 (REFUSED in text, refused in JSON)."""

    def __init__(self, reason, message, check_id="input_files"):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.check_id = check_id
        self.checks = []
        self.pending_count = 0
        self.unknown_count = 0


def load_candidate(path):
    """tag -> sha256 from sha256sum output; refuses empty/malformed files."""
    tags = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                m = HASH_LINE.fullmatch(line)
                if not m:
                    raise _InputError(
                        "input_malformed_hashes",
                        f"unparseable --sql-hashes line: {line[:80]}")
                if m.group(2) in tags:
                    raise _InputError(
                        "input_malformed_hashes",
                        f"duplicate migration tag in --sql-hashes: {m.group(2)}")
                tags[m.group(2)] = m.group(1)
    except OSError as exc:
        raise _InputError(
            "input_malformed_hashes",
            f"cannot read --sql-hashes {path}: {exc.__class__.__name__}") from None
    if not tags:
        raise _InputError(
            "input_empty_candidate", "--sql-hashes lists no migrations")
    return tags


def load_ledger(path):
    hashes = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                fields = raw.split()
                if not fields:
                    continue
                found = [f for f in fields if HEX64.fullmatch(f)]
                if len(found) != 1:
                    raise _InputError(
                        "input_malformed_ledger",
                        "ledger row without exactly one sha256 hash field")
                hashes.append(found[0])
    except OSError as exc:
        raise _InputError(
            "input_malformed_ledger",
            f"cannot read --ledger {path}: {exc.__class__.__name__}") from None
    if not hashes:
        raise _InputError(
            "input_empty_ledger",
            "ledger is empty: refusing to treat an empty or unreadable "
            "migration table as evidence")
    return hashes


def load_evidence_document(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise _InputError(
            "evidence_invalid_json",
            f"unreadable evidence {path}: {exc.__class__.__name__}",
            "evidence_schema") from None


def validate_evidence_shape(doc):
    """Fail closed on wrong-shaped evidence before any gate evaluates it.

    Only known-field types are checked; unknown fields are ignored so
    forward-compatible evidence never breaks here. Raises _InputError.
    """
    if not isinstance(doc, dict):
        raise _InputError(
            "evidence_invalid_root",
            "evidence root must be a JSON object, "
            f"got {doc.__class__.__name__}",
            "evidence_schema")
    version = doc.get("version", _MISSING)
    if (version is _MISSING or isinstance(version, bool)
            or not isinstance(version, int) or version != 1):
        raise _InputError(
            "evidence_invalid_version",
            "evidence version must be the integer 1",
            "evidence_schema")
    for field in ("status", "provider", "branch", "apply_target",
                  "cleanup", "finished_at"):
        if field in doc and not isinstance(doc[field], str):
            raise _InputError(
                "evidence_invalid_field",
                f"evidence field {field!r} must be a string",
                "evidence_schema")
    if "migrations" not in doc or not isinstance(doc["migrations"], list):
        raise _InputError(
            "evidence_invalid_migrations",
            "evidence migrations must be a list of rows",
            "evidence_schema")
    seen = set()
    for row in doc["migrations"]:
        if not isinstance(row, dict):
            raise _InputError(
                "evidence_invalid_row",
                "evidence migration row must be an object",
                "evidence_schema")
        tag = row.get("tag")
        digest = row.get("sha256")
        if not isinstance(tag, str) or not tag:
            raise _InputError(
                "evidence_invalid_field",
                "evidence migration row tag must be a non-empty string",
                "evidence_schema")
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise _InputError(
                "evidence_invalid_field",
                "evidence migration row sha256 must be 64 hex chars",
                "evidence_schema")
        if "result" in row and not isinstance(row["result"], str):
            raise _InputError(
                "evidence_invalid_field",
                "evidence migration row result must be a string",
                "evidence_schema")
        if tag in seen:
            raise _InputError(
                "evidence_duplicate_tag",
                f"duplicate migration tag in evidence: {tag}",
                "evidence_schema")
        seen.add(tag)


def evaluate_evidence(doc, max_age_hours, candidate):
    """Evidence freshness plus binding to the candidate pending chain.

    Tags and digests must match exactly, in the same filename-sorted migration
    order used by the rehearsal; evidence for another chain or order fails.

    Returns (rc, text_lines, checks) where checks are (id, status, reason)
    in evaluation order, short-circuiting at the first failure like the
    legacy text gate. Callers must run validate_evidence_shape first.
    """
    checks = [("evidence_schema", "pass", "ok")]
    lines = []
    if doc.get("status") != "passed":
        lines.append(f"FAIL: rehearsal evidence status is {doc.get('status')!r}, want 'passed'")
        checks.append(("evidence_status", "fail", "evidence_status_not_passed"))
        return 1, lines, checks
    checks.append(("evidence_status", "pass", "ok"))
    provider, cleanup = doc.get("provider"), doc.get("cleanup")
    if provider == "planetscale" and cleanup != "deleted":
        lines.append("FAIL: PlanetScale rehearsal branch cleanup is not confirmed deleted")
        checks.append(("evidence_cleanup", "fail", "evidence_cleanup_unconfirmed"))
        return 1, lines, checks
    if provider == "pitr" and cleanup != "delegated":
        lines.append("FAIL: PITR cleanup must be recorded as delegated expiration")
        checks.append(("evidence_cleanup", "fail", "evidence_cleanup_unconfirmed"))
        return 1, lines, checks
    checks.append(("evidence_cleanup", "pass", "ok"))
    if (provider not in {"planetscale", "pitr"} or not doc.get("branch")
            or doc.get("apply_target") != doc.get("branch")):
        lines.append("FAIL: rehearsal evidence target does not match its created branch")
        checks.append(("evidence_target", "fail", "evidence_target_mismatch"))
        return 1, lines, checks
    checks.append(("evidence_target", "pass", "ok"))
    try:
        finished = datetime.datetime.strptime(
            doc["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (KeyError, TypeError, ValueError):
        lines.append("FAIL: rehearsal evidence has no parseable finished_at")
        checks.append(("evidence_freshness", "fail", "evidence_unparseable_time"))
        return 1, lines, checks
    age_hours = (datetime.datetime.now(datetime.timezone.utc) - finished).total_seconds() / 3600
    if age_hours < -5 / 60:
        lines.append(f"FAIL: rehearsal evidence finished_at is in the future ({-age_hours:.1f}h ahead)")
        checks.append(("evidence_freshness", "fail", "evidence_future"))
        return 1, lines, checks
    if age_hours > max_age_hours:
        lines.append(f"FAIL: rehearsal evidence is {age_hours:.1f}h old, older than {max_age_hours}h")
        checks.append(("evidence_freshness", "fail", "evidence_stale"))
        return 1, lines, checks
    checks.append(("evidence_freshness", "pass", "ok"))
    migs = doc.get("migrations", [])
    not_applied = sorted(m.get("tag", "?") for m in migs if m.get("result") != "applied")
    if not_applied:
        lines.append(f"FAIL: rehearsal evidence claims passed but {len(not_applied)} migration(s) "
                     f"were not applied; first: {not_applied[0]}")
        checks.append(("evidence_binding", "fail", "evidence_not_applied"))
        return 1, lines, checks
    if candidate is not None:
        evidenced = {m.get("tag"): m.get("sha256") for m in migs}
        if evidenced != candidate:
            only_ev = sorted(set(evidenced) - set(candidate))
            only_cand = sorted(set(candidate) - set(evidenced))
            mism = sorted(t for t in set(evidenced) & set(candidate)
                          if evidenced[t] != candidate[t])
            detail = (only_ev[:1] + only_cand[:1] + mism[:1]) or ["empty evidence"]
            lines.append("FAIL: rehearsal evidence is not bound to this candidate chain "
                         f"(first divergence: {detail[0]})")
            checks.append(("evidence_binding", "fail", "evidence_unbound"))
            return 1, lines, checks
        if [m.get("tag") for m in migs] != sorted(candidate):
            lines.append("FAIL: rehearsal evidence migration order does not match filename order")
            checks.append(("evidence_binding", "pass", "ok"))
            checks.append(("evidence_order", "fail", "evidence_order_mismatch"))
            return 1, lines, checks
        checks.append(("evidence_binding", "pass", "ok"))
        checks.append(("evidence_order", "pass", "ok"))
        lines.append(f"ok: rehearsal evidence bound to the candidate chain ({len(migs)} migration(s))")
    lines.append(f"ok: rehearsal evidence fresh ({age_hours:.1f}h < {max_age_hours}h), "
                 f"{len(migs)} migration(s) passed")
    return 0, lines, checks


def check_evidence(path, max_age_hours, candidate=None):
    """Legacy text entry point: validate shape, then evaluate and print."""
    doc = load_evidence_document(path)
    validate_evidence_shape(doc)
    rc, lines, _ = evaluate_evidence(doc, max_age_hours, candidate)
    for line in lines:
        print(line)
    return rc


def _verdict_object(checks, pending_count, unknown_count):
    verdict = "pass"
    if any(status == "refused" for _, status, _ in checks):
        verdict = "refused"
    elif any(status == "fail" for _, status, _ in checks):
        verdict = "fail"
    next_action = ""
    # Match the verdict precedence above: a refusal outranks a gate failure,
    # so point the caller at the refused check first (e.g. an evidence-shape
    # refusal appended after an already-failed ledger check).
    ranked = ([c for c in checks if c[1] == "refused"]
              + [c for c in checks if c[1] == "fail"])
    if ranked:
        next_action = NEXT_ACTIONS[ranked[0][2]]
    return {
        "version": JSON_SCHEMA_VERSION,
        "verdict": verdict,
        "pending_count": pending_count,
        "unknown_count": unknown_count,
        "checks": [{"id": cid, "status": status, "reason": reason}
                   for cid, status, reason in checks],
        "next_action": next_action,
    }


def _run(args):
    if args.max_age_hours < 0:
        raise _InputError(
            "input_invalid_max_age", "--max-age-hours must be >= 0")

    candidate = load_candidate(args.sql_hashes)
    applied = set(load_ledger(args.ledger))

    lines = []
    checks = [("input_files", "pass", "ok")]
    rc = 0
    pending = {tag: digest for tag, digest in candidate.items() if digest not in applied}
    if pending:
        if args.evidence:
            lines.append(f"ok: {len(pending)} pending migration(s) require bound rehearsal evidence")
            checks.append(("candidate_chain", "pass", "ok"))
        else:
            first = sorted(pending)[0]
            lines.append(f"FAIL: {len(pending)} candidate migration(s) are pending without "
                         f"rehearsal evidence; first: {first}")
            checks.append(("candidate_chain", "fail", "pending_without_evidence"))
            rc = 1
    else:
        lines.append(f"ok: all {len(candidate)} candidate migrations are in the production ledger")
        checks.append(("candidate_chain", "pass", "ok"))

    known = set(candidate.values())
    unknown = [h for h in applied if h not in known]
    if unknown:
        lines.append(f"FAIL: {len(unknown)} production ledger migration(s) unknown to the "
                     f"candidate repo: possible out-of-band DDL")
        checks.append(("ledger_unknown", "fail", "unknown_ledger_rows"))
        rc = 1
    else:
        lines.append("ok: no out-of-band production migrations (ledger matches candidate)")
        checks.append(("ledger_unknown", "pass", "ok"))

    if args.evidence:
        # For a rollout, rehearse exactly the migrations absent from the
        # ledger. With no pending migrations, retain full-chain validation.
        evidence_chain = pending or candidate
        try:
            doc = load_evidence_document(args.evidence)
            validate_evidence_shape(doc)
        except _InputError as exc:
            exc.checks = checks
            exc.pending_count = len(pending)
            exc.unknown_count = len(unknown)
            raise
        rc_ev, ev_lines, ev_checks = evaluate_evidence(
            doc, args.max_age_hours, evidence_chain)
        lines.extend(ev_lines)
        checks.extend(ev_checks)
        rc = rc_ev or rc

    if args.json:
        print(json.dumps(_verdict_object(checks, len(pending), len(unknown)),
                         sort_keys=True))
    else:
        for line in lines:
            print(line)
    return rc


def main(argv):
    ap = argparse.ArgumentParser(
        add_help=True,
        description=("Preflight gate: candidate repo digests vs the production "
                     "migration ledger. Exit 0 when every check passes, 1 when a "
                     "gate fails, 2 on bad input (REFUSED). With --json, stdout "
                     "is one versioned verdict object (see JSON_SCHEMA_VERSION) "
                     "with pending/unknown counts, stable check and reason IDs, "
                     "and an actionable next_action on refusal."),
        epilog=("JSON contract: {version, verdict, pending_count, unknown_count, "
                "checks[{id, status, reason}], next_action}. The verdict maps to "
                "the exit code: pass 0, fail 1, refused 2. "
                "See docs/migration-standard.md for the full contract."))
    ap.add_argument("--sql-hashes", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--max-age-hours", type=int, default=24)
    ap.add_argument("--json", action="store_true",
                    help="emit one machine-readable JSON verdict object on "
                         "stdout instead of text lines")
    args = ap.parse_args(argv)
    try:
        return _run(args)
    except _InputError as exc:
        if args.json:
            refused = list(exc.checks)
            refused.append((exc.check_id, "refused", exc.reason))
            print(json.dumps(_verdict_object(refused, exc.pending_count,
                                             exc.unknown_count),
                             sort_keys=True))
            return 2
        print(f"REFUSED: migration_drift_gate: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
