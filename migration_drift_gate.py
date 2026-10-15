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

Output: one line per check on stdout ("ok: ..." / "FAIL: ..."); never row
data beyond tags and counts. No secrets are read or printed.

Exit: 0 all gates pass | 1 a gate failed | 2 bad input (REFUSED).
"""
import argparse
import datetime
import json
import re
import sys

HEX64 = re.compile(r"[0-9a-f]{64}")
HASH_LINE = re.compile(r"([0-9a-f]{64})\s+\*?(?:.*/)?([^/\s]+)\.sql")


def refuse(msg):
    print(f"REFUSED: migration_drift_gate: {msg}", file=sys.stderr)
    sys.exit(2)


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
                    refuse(f"unparseable --sql-hashes line: {line[:80]}")
                if m.group(2) in tags:
                    refuse(f"duplicate migration tag in --sql-hashes: {m.group(2)}")
                tags[m.group(2)] = m.group(1)
    except OSError as exc:
        refuse(f"cannot read --sql-hashes {path}: {exc.__class__.__name__}")
    if not tags:
        refuse("--sql-hashes lists no migrations")
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
                    refuse("ledger row without exactly one sha256 hash field")
                hashes.append(found[0])
    except OSError as exc:
        refuse(f"cannot read --ledger {path}: {exc.__class__.__name__}")
    if not hashes:
        refuse("ledger is empty: refusing to treat an empty or unreadable "
               "migration table as evidence")
    return hashes


def check_evidence(path, max_age_hours):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        refuse(f"unreadable evidence {path}: {exc.__class__.__name__}")
    if doc.get("status") != "passed":
        print(f"FAIL: rehearsal evidence status is {doc.get('status')!r}, want 'passed'")
        return 1
    try:
        finished = datetime.datetime.strptime(
            doc["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (KeyError, TypeError, ValueError):
        print("FAIL: rehearsal evidence has no parseable finished_at")
        return 1
    age_hours = (datetime.datetime.now(datetime.timezone.utc) - finished).total_seconds() / 3600
    if age_hours > max_age_hours:
        print(f"FAIL: rehearsal evidence is {age_hours:.1f}h old, older than {max_age_hours}h")
        return 1
    count = len(doc.get("migrations", []))
    print(f"ok: rehearsal evidence fresh ({age_hours:.1f}h < {max_age_hours}h), "
          f"{count} migration(s) passed")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--sql-hashes", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--max-age-hours", type=int, default=24)
    args = ap.parse_args(argv)
    if args.max_age_hours < 0:
        refuse("--max-age-hours must be >= 0")

    candidate = load_candidate(args.sql_hashes)
    applied = set(load_ledger(args.ledger))

    rc = 0
    pending = sorted(t for t, h in candidate.items() if h not in applied)
    if pending:
        print(f"FAIL: {len(pending)} candidate migration(s) not in the production "
              f"ledger; first: {pending[0]}")
        rc = 1
    else:
        print(f"ok: all {len(candidate)} candidate migrations are in the production ledger")

    known = set(candidate.values())
    unknown = [h for h in applied if h not in known]
    if unknown:
        print(f"FAIL: {len(unknown)} production ledger migration(s) unknown to the "
              f"candidate repo: possible out-of-band DDL")
        rc = 1
    else:
        print("ok: no out-of-band production migrations (ledger matches candidate)")

    if args.evidence:
        rc = check_evidence(args.evidence, args.max_age_hours) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
