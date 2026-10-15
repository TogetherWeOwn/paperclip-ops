#!/usr/bin/env python3
"""Verify restoration of sealed rehearsal copies, never create a live backup."""
import argparse
import hashlib
import json
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path


class GateError(Exception):
    pass


def identity(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot(root, name):
    path = root / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GateError("Snapshots must be private regular files, not links")
    for suffix in ("-wal", "-shm", "-journal"):
        if (root / (name + suffix)).exists() or (root / (name + suffix)).is_symlink():
            raise GateError("Snapshot sidecars present; require a sealed consistent backup")
    return path


def inspect_snapshot(path, deadline):
    before = identity(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        if source.read(16) != b"SQLite format 3\x00":
            raise GateError("Snapshot is not a SQLite database")
        source.seek(0)
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise GateError("Snapshot verification deadline exceeded")
            digest.update(chunk)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise GateError("Snapshot integrity check failed")
            tables = db.execute(
                "SELECT count(*) FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT GLOB 'sqlite_*'"
            ).fetchone()[0]
            if not tables:
                raise GateError("Snapshot has no application tables")
    except sqlite3.Error:
        raise GateError("SQLite validation failed or deadline exceeded") from None
    if before != identity(path):
        raise GateError("Snapshot changed during verification")
    return {"sha256": digest.hexdigest(), "bytes": before[2], "tables": tables}


def verify(root, timeout=60):
    if not 0 < timeout <= 300:
        raise GateError("Verification timeout must be within 0–300 seconds")
    root = Path(root).absolute()
    if root != root.resolve() or not root.is_dir() or root == Path(root.anchor):
        raise GateError("Require a non-root rehearsal directory without symlinks")
    if root.stat().st_mode & 0o077:
        raise GateError("Rehearsal directory must deny group and other access")
    marker = root / ".keeper-rehearsal"
    marker_info = marker.lstat()
    if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink != 1:
        raise GateError("Rehearsal marker must be a regular file, not a link")
    with marker.open("rb") as source:
        if source.read(21) != b"KEEPER_REHEARSAL_V1\n":
            raise GateError("Rehearsal marker missing or invalid")
    backup = snapshot(root, "backup.db")
    restored = snapshot(root, "restored.db")
    if backup.samefile(restored):
        raise GateError("Backup and restoration must be separate files")
    before = (identity(backup), identity(restored))
    deadline = time.monotonic() + timeout
    baseline = inspect_snapshot(backup, deadline)
    result = inspect_snapshot(restored, deadline)
    if baseline != result:
        raise GateError("Restored snapshot differs from sealed backup")
    snapshot(root, "backup.db")
    snapshot(root, "restored.db")
    if before != (identity(backup), identity(restored)):
        raise GateError("Snapshots changed during verification")
    if time.monotonic() >= deadline:
        raise GateError("Snapshot verification deadline exceeded")
    return {
        "copyRestoration": "PASS",
        "backupSha256": baseline["sha256"],
        "sizeBytes": baseline["bytes"],
        "applicationTables": baseline["tables"],
        "liveBackupConsistency": "NOT_PROVEN",
        "migrationRehearsal": "NOT_RUN",
        "runtimeCompatibility": "NOT_RUN",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.root, args.timeout), sort_keys=True))
    except (GateError, OSError) as error:
        message = str(error) if isinstance(error, GateError) else "Rehearsal files unavailable"
        print(json.dumps({"copyRestoration": "FAIL", "reason": message}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
