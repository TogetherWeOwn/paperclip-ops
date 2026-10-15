#!/usr/bin/env python3
"""Repair closeout ledger/snapshot information defects (case_f03f7f10).

Two verified defects in the case_de994047 closeout artifacts, per the CTO 19:17Z
research delta on case_24119081:

  1. NULL CHECK NAMES. The runtime snapshot holds check rows whose `name` is
     null (53 rows on the first six merged web PRs), while the canonical
     ledger retains the same checks as dictionary keys for the same heads.
     Conversion loss, not missing GitHub data: the null rows keep `id`,
     `sha`, `conclusion` and `completedAt`. The repair fills each null name
     from the ledger dict key joined by check `id` (plus `sha`/`completedAt`
     agreement), so required names (`check`, `gitleaks`, `pr-lint`) join by
     name again. A null row with no agreeing ledger entry stays null: an
     invented name would be worse than a marked gap, and verification fails
     loudly on it.

  2. MISLABELED PROGRESS CLOCKS. Records whose evidence text claims a
     "successful exact-head core-check completion timestamp" must carry the
     `completedAt` of a same-PR, same-head, successful check named exactly
     `check`. Bot #114/#137 borrowed their `gitleaks` success clocks while
     the retained core `check` FAILED; ops #490's clock matches no retained
     completion at all. The repair re-stamps from the latest qualifying core
     success, or unsets the clock (UNKNOWN progress) with evidence naming the
     actual events (check name + conclusion + completedAt) when no such event
     exists. This does not imply every security-check completion is
     meaningless -- only the recorded claim mismatched.

  3. CHARACTER-SPLIT REQUIRED CHECKS. Found while re-verifying the 19:48Z
     checkpoint: web #307's requiredChecks was list("UNKNOWN"), seven
     one-letter "required checks". The repair rejoins it to the sentinel
     string; verification names the shape instead of reporting seven
     unjoinable letters.

WHY A REPAIR TOOL, NOT A HAND EDIT. The Director's hourly closeout pass
writes these bytes by hand; a hand fix re-introduces the same drift next
pass. This tool makes the re-emission deterministic and verifiable: repair,
verify, emit. It never invents GitHub evidence and never touches merge,
review, admission or plan state.

   python3 pr_closeout_snapshot_repair.py --ledger ledger.json \\
       --snapshot snapshot.json --ledger-out ledger_fixed.json \\
       --snapshot-out snapshot_fixed.json --report-out report.json

   python3 pr_closeout_snapshot_repair.py --ledger ledger.json \\
       --zip-in runtime-inputs.zip --zip-out runtime-inputs-fixed.zip \\
       --ledger-out ledger_fixed.json --report-out report.json

Exit codes: 0 repaired (or already clean) and verified, 1 defects remain
after repair, 2 usage/error, 5 COULD NOT MEASURE (unreadable or empty
inputs). Green here means the REPAIR verified -- it is never independent
GitHub verification, which this sandbox cannot reach.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import zipfile
from typing import Any

# Matches every recorded wording of the core-success claim: "exact-head"
# (19:02Z checkpoint), "bound-head" (19:48Z checkpoint) and a bare
# "successful core-check completion". The 19:48Z checkpoint repeated the
# borrowed-gitleaks-clock defect under the new wording; a pattern pinned to
# one phrasing would have passed it unexamined.
CORE_SUCCESS_CLAIM_RE = re.compile(
    r"successful\s+(?:[a-z]+-head\s+)?core-check\s+completion", re.IGNORECASE)


def claims_core_success(evidence: Any) -> bool:
    """Whether a record's evidence text claims a core-check success clock."""
    return bool(isinstance(evidence, str)
                and CORE_SUCCESS_CLAIM_RE.search(evidence) is not None)


class RepairError(RuntimeError):
    pass


def parse_time(value: Any) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ledger_key_for_snapshot_key(snapshot_key: str) -> str:
    """Snapshot targets carry the owner prefix; ledger records do not."""
    return snapshot_key.split("/", 1)[1] if "/" in snapshot_key else snapshot_key


def rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def ledger_name_index(record: dict[str, Any]) -> dict[int, tuple[str, Any, Any]]:
    """Map check id -> (dict-key name, headSha, completedAt).

    The canonical ledger keeps checks as dict entries keyed by name (merged
    rows) or as list rows already carrying names (live rows); both shapes
    index the same way. First writer wins; ids are unique per target.
    """
    index: dict[int, tuple[str, Any, Any]] = {}
    entries = record.get("latestExactHeadChecks")
    if isinstance(entries, dict):
        items = entries.items()
    elif isinstance(entries, list):
        items = [
            (row.get("name"), row)
            for row in entries if isinstance(row, dict)]
    else:
        return index
    for name, entry in items:
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        check_id = entry.get("id")
        if isinstance(check_id, int) and check_id not in index:
            index[check_id] = (
                name, entry.get("headSha"), entry.get("completedAt"))
    return index


def repair_check_names(
    snapshot_doc: dict[str, Any], ledger_doc: dict[str, Any]
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Fill null snapshot check names from the ledger dict key.

    Returns (filled count, repairs, unfilled). A present name is never
    overwritten; a null row joins only on check id with agreeing sha and
    completedAt, otherwise it stays null for verification to refuse.
    """
    records = ledger_doc.get("records")
    snapshots = snapshot_doc.get("snapshots")
    if not isinstance(records, dict) or not records:
        raise RepairError("ledger holds no records; refusing to report a clean board")
    if not isinstance(snapshots, dict) or not snapshots:
        raise RepairError("snapshot holds no snapshots; refusing to report a clean board")
    filled = 0
    repairs: list[dict[str, Any]] = []
    unfilled: list[dict[str, Any]] = []
    for target, snap in snapshots.items():
        if not isinstance(snap, dict):
            continue
        record = records.get(ledger_key_for_snapshot_key(str(target)))
        index = ledger_name_index(record) if isinstance(record, dict) else {}
        for row in rows(snap.get("checks")):
            if isinstance(row.get("name"), str):
                continue
            check_id = row.get("id")
            named = index.get(check_id) if isinstance(check_id, int) else None
            if (named is not None and row.get("sha") == named[1]
                    and row.get("completedAt") == named[2]):
                row["name"] = named[0]
                filled += 1
                repairs.append({"target": target, "id": check_id,
                                "name": named[0]})
            else:
                unfilled.append({"target": target, "id": check_id})
    return filled, repairs, unfilled


def split_string_required(value: Any) -> str | None:
    """The string a character-split requiredChecks list was built from.

    The 19:48Z snapshot carried `["U","N","K","N","O","W","N"]` for web #307:
    list("UNKNOWN"). Two or more single-character entries are never a real
    required-check set; they rejoin to the sentinel the producer meant, which
    the ledger evaluator reads as "no required checks named" -- UNKNOWN, never
    green. Returns None for any other shape.
    """
    if (isinstance(value, list) and len(value) >= 2
            and all(isinstance(item, str) and len(item) == 1 for item in value)):
        return "".join(value)
    return None


def repair_required_checks(snapshot_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Rejoin character-split requiredChecks lists; returns the repairs."""
    snapshots = snapshot_doc.get("snapshots")
    if not isinstance(snapshots, dict) or not snapshots:
        raise RepairError("snapshot holds no snapshots; refusing to report a clean board")
    repairs: list[dict[str, Any]] = []
    for target, snap in snapshots.items():
        if not isinstance(snap, dict):
            continue
        joined = split_string_required(snap.get("requiredChecks"))
        if joined is not None:
            snap["requiredChecks"] = joined
            repairs.append({"target": target, "requiredChecks": joined})
    return repairs


def core_success_completions(
    snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """Same-head successful checks named exactly `check`, latest last."""
    head = snapshot.get("headSha")
    if not isinstance(head, str):
        return []
    matching = [
        row for row in rows(snapshot.get("checks"))
        if row.get("name") == "check"
        and row.get("conclusion") == "success" and row.get("sha") == head
        and parse_time(row.get("completedAt")) is not None
    ]
    matching.sort(key=lambda row: str(row.get("completedAt")))
    return matching


def borrowed_clock_owner(snapshot: dict[str, Any], clock: Any) -> str:
    """Name the actual event a mislabeled clock was borrowed from, if any."""
    for row in rows(snapshot.get("checks")):
        if row.get("completedAt") == clock:
            return (f"{row.get('name')}/{row.get('conclusion')} "
                    f"completedAt {clock}")
    return f"no retained completion at {clock}"


def repair_progress_clocks(
    ledger_doc: dict[str, Any], snapshot_doc: dict[str, Any]
) -> tuple[int, int, list[dict[str, Any]]]:
    """Re-stamp core-success clocks; unset the ones with no such event.

    Returns (restamped count, unset count, repairs). Records whose evidence
    does not claim core-check success are untouched, as are records whose
    clock already equals the LATEST qualifying core success (an older joined
    success is restamped forward: a later completion is later progress).
    """
    records = ledger_doc.get("records")
    snapshots = snapshot_doc.get("snapshots")
    if not isinstance(records, dict) or not records:
        raise RepairError("ledger holds no records; refusing to report a clean board")
    if not isinstance(snapshots, dict) or not snapshots:
        raise RepairError("snapshot holds no snapshots; refusing to report a clean board")
    restamped = 0
    unset = 0
    repairs: list[dict[str, Any]] = []
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        if not claims_core_success(record.get("lastProgressEvidence")):
            continue
        ledger_key = ledger_key_for_snapshot_key(str(key))
        snapshot = None
        for target, snap in snapshots.items():
            if ledger_key_for_snapshot_key(str(target)) == ledger_key and isinstance(snap, dict):
                snapshot = snap
                break
        if snapshot is None:
            continue
        candidates = core_success_completions(snapshot)
        clock = record.get("lastProgressAt")
        if candidates and clock == candidates[-1].get("completedAt"):
            continue  # The claim already joins; preserve the event identity.
        if candidates:
            latest = candidates[-1].get("completedAt")
            record["lastProgressAt"] = latest
            restamped += 1
            repairs.append({"record": key, "action": "restamped",
                            "lastProgressAt": latest,
                            "event": f"check/success completedAt {latest}"})
            continue
        # No successful exact-head core check exists: the clock stays unset
        # rather than borrowing an unrelated check's timestamp.
        retained = [row for row in rows(snapshot.get("checks"))
                    if row.get("name") == "check"
                    and row.get("sha") == snapshot.get("headSha")]
        if retained:
            # A result-less attempt is still in flight: it sorts newest.
            latest_retained = sorted(
                retained, key=lambda row: (row.get("completedAt") is None,
                                           str(row.get("completedAt"))))[-1]
            retained_identity = (
                f"check/{latest_retained.get('conclusion') or 'pending'} "
                f"completedAt {latest_retained.get('completedAt')}")
        else:
            retained_identity = "no retained core `check` attempt at this head"
        record["lastProgressAt"] = None
        record["lastProgressEvidence"] = (
            "UNKNOWN progress: no same-head core `check` success retained; "
            f"latest core attempt: {retained_identity}; prior clock {clock} was "
            f"{borrowed_clock_owner(snapshot, clock)}, not core "
            "progress. A security-check completion is not meaningless -- "
            "only the recorded core-success claim mismatched.")
        unset += 1
        repairs.append({"record": key, "action": "unset",
                        "priorLastProgressAt": clock,
                        "retainedCore": retained_identity})
    return restamped, unset, repairs


def verify(
    snapshot_doc: dict[str, Any], ledger_doc: dict[str, Any]
) -> list[str]:
    """Acceptance: (a) zero null names with required-name joinability;
    (b) every core-success-claimed clock joins to a qualifying event."""
    errors: list[str] = []
    snapshots = snapshot_doc.get("snapshots")
    records = ledger_doc.get("records")
    if not isinstance(snapshots, dict) or not snapshots:
        return ["snapshot holds no snapshots; refusing to report a clean board"]
    if not isinstance(records, dict) or not records:
        return ["ledger holds no records; refusing to report a clean board"]
    for target, snap in snapshots.items():
        if not isinstance(snap, dict):
            errors.append(f"{target}: snapshot entry is not an object")
            continue
        head = snap.get("headSha")
        nulls = sum(
            1 for row in rows(snap.get("checks"))
            if not isinstance(row.get("name"), str))
        if nulls:
            errors.append(f"{target}: {nulls} check rows with null names")
        required = snap.get("requiredChecks")
        if split_string_required(required) is not None:
            errors.append(
                f"{target}: requiredChecks is a character-split string "
                f"{split_string_required(required)!r}, not a check list")
            required = []
        required = [c for c in required if isinstance(c, str)] \
            if isinstance(required, list) else []
        for name in required:
            joined = any(
                row.get("name") == name and row.get("sha") == head
                for row in rows(snap.get("checks")))
            if not joined:
                errors.append(
                    f"{target}: required check {name} not joinable by name at exact head")
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        if not claims_core_success(record.get("lastProgressEvidence")):
            continue
        ledger_key = ledger_key_for_snapshot_key(str(key))
        snapshot = next(
            (snap for target, snap in snapshots.items()
             if ledger_key_for_snapshot_key(str(target)) == ledger_key
             and isinstance(snap, dict)),
            None)
        if snapshot is None:
            errors.append(f"{key}: core-success claim with no snapshot target")
            continue
        clock = record.get("lastProgressAt")
        joined = any(
            row.get("completedAt") == clock
            for row in core_success_completions(snapshot))
        if not joined:
            errors.append(
                f"{key}: lastProgressAt {clock} claims core-check success "
                "but joins to no same-head successful `check`")
    return errors


def load_doc(path: str, what: str) -> tuple[Any, bytes]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        raise RepairError(f"{what} unreadable: {error}") from error
    try:
        return json.loads(raw.decode("utf-8")), raw
    except (ValueError, UnicodeDecodeError) as error:
        raise RepairError(f"{what} is not JSON: {error}") from error


def dump_doc(doc: Any) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=False) + "\n").encode("utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair closeout null check names and mislabeled progress clocks (case_f03f7f10).")
    parser.add_argument("--ledger", required=True, help="canonical ledger JSON")
    parser.add_argument("--snapshot", default=None,
                        help="runtime snapshot JSON (unused with --zip-in)")
    parser.add_argument("--zip-in", default=None,
                        help="runtime-input ZIP holding snapshot.json")
    parser.add_argument("--ledger-out", required=True, help="repaired ledger JSON")
    parser.add_argument("--snapshot-out", default=None, help="repaired snapshot JSON")
    parser.add_argument("--zip-out", default=None, help="re-emitted runtime-input ZIP")
    parser.add_argument("--report-out", required=True, help="repair report JSON")
    parser.add_argument("--check-only", action="store_true",
                        help="verify inputs without writing repairs")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        ledger_doc, ledger_raw = load_doc(args.ledger, "ledger")
        records = ledger_doc.get("records") if isinstance(ledger_doc, dict) else None
        if not isinstance(ledger_doc, dict) or not isinstance(records, dict) or not records:
            raise RepairError("ledger holds no records; refusing to report a clean board")
        zip_members: list[tuple[str, bytes]] | None = None
        if args.zip_in:
            try:
                with zipfile.ZipFile(args.zip_in) as archive:
                    zip_members = [(info.filename, archive.read(info.filename))
                                   for info in archive.infolist()]
            except (OSError, zipfile.BadZipFile) as error:
                raise RepairError(f"input ZIP unreadable: {error}") from error
            names = [name for name, _ in zip_members]
            if "snapshot.json" not in names:
                raise RepairError("input ZIP holds no snapshot.json member")
            try:
                snapshot_doc = json.loads(
                    dict(zip_members)["snapshot.json"].decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                raise RepairError(f"ZIP snapshot.json is not JSON: {error}") from error
            snapshot_raw = dict(zip_members)["snapshot.json"]
        else:
            if not args.snapshot:
                raise RepairError("--snapshot or --zip-in is required")
            snapshot_doc, snapshot_raw = load_doc(args.snapshot, "snapshot")
        snapshots = snapshot_doc.get("snapshots") if isinstance(snapshot_doc, dict) else None
        if not isinstance(snapshot_doc, dict) or not isinstance(snapshots, dict) or not snapshots:
            raise RepairError("snapshot holds no snapshots; refusing to report a clean board")
    except RepairError as error:
        print(f"pr_closeout_snapshot_repair: {error}", file=sys.stderr)
        return 5
    if args.check_only:
        errors = verify(snapshot_doc, ledger_doc)
        if errors:
            print("defects remain:")
            for error in errors:
                print(f"  - {error}")
            return 1
        print("verified: 0 null names, all core-success clocks join")
        return 0
    if not args.snapshot_out and not args.zip_out:
        print("pr_closeout_snapshot_repair: --snapshot-out or --zip-out is required",
              file=sys.stderr)
        return 2
    filled, name_repairs, unfilled = repair_check_names(snapshot_doc, ledger_doc)
    required_repairs = repair_required_checks(snapshot_doc)
    restamped, unset, clock_repairs = repair_progress_clocks(ledger_doc, snapshot_doc)
    errors = verify(snapshot_doc, ledger_doc)
    report = {
        "generatedAt": utcnow_iso(),
        "inputs": {
            "ledgerSha256": sha256_hex(ledger_raw),
            "snapshotSha256": sha256_hex(snapshot_raw),
        },
        "nameRepairs": {"filled": filled, "repairs": name_repairs, "unfilled": unfilled},
        "requiredChecksRepairs": required_repairs,
        "clockRepairs": {"restamped": restamped, "unset": unset,
                         "repairs": clock_repairs},
        "verificationErrors": errors,
    }
    try:
        with open(args.ledger_out, "wb") as handle:
            handle.write(dump_doc(ledger_doc))
        if args.snapshot_out:
            with open(args.snapshot_out, "wb") as handle:
                handle.write(dump_doc(snapshot_doc))
        if args.zip_out:
            assert zip_members is not None
            with zipfile.ZipFile(args.zip_out, "w",
                                  compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in zip_members:
                    archive.writestr(
                        name, dump_doc(snapshot_doc) if name == "snapshot.json" else data)
        with open(args.report_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as error:
        print(f"pr_closeout_snapshot_repair: cannot write output: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"names filled: {filled} (unfilled: {len(unfilled)})")
        print(f"requiredChecks rejoined: {len(required_repairs)}")
        print(f"clocks restamped: {restamped}, unset: {unset}")
        if errors:
            print("verification FAILED:")
            for error in errors:
                print(f"  - {error}")
        else:
            print("verified: 0 null names, all core-success clocks join")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
