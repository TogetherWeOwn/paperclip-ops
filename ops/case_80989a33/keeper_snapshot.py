#!/usr/bin/env python3
"""Online WAL-consistent Keeper snapshot and isolated restore; never writes a live path."""
import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import sqlite3

    import keeper_restore_gate as gate
except ImportError as missing:
    print(json.dumps({"result": "FAIL", "step": "tool-preflight",
                      "reason": "Missing python module %s; STOP, do not install packages" % missing.name}))
    raise SystemExit(1) from None

MIN_PYTHON = (3, 8)
MIN_SQLITE = (3, 33, 0)
LOCAL_FSTYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "tmpfs"}
GIB = 1 << 30
MARKER = b"KEEPER_REHEARSAL_V1\n"
SNAPSHOT = "keeper-snapshot.db"
SNAPSHOT_RECEIPT = "snapshot.json"
RESTORE_RECEIPT = "restore.json"
GATE_RESULT = "gate.json"
RECEIPT = "receipt.json"
SNAPSHOT_METHOD = "sqlite-online-backup single-step read transaction from read-only source"
RESTORE_METHOD = "sqlite-online-backup from retained immutable snapshot into fresh isolated file"
# Python 3.12+: also refuse checkpoint-on-close. Older hosts rely on mode=ro alone.
NO_CKPT = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
GATE_KEYS = ("copyRestoration", "backupSha256", "sizeBytes", "applicationTables",
             "liveBackupConsistency", "migrationRehearsal", "runtimeCompatibility")


class ProtocolError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tool_preflight():
    if sys.version_info < MIN_PYTHON:
        raise ProtocolError("python3 >= 3.8 required; STOP, do not install packages")
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise ProtocolError("SQLite library >= 3.33.0 required; STOP, do not install packages")
    return {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "sqlite": sqlite3.sqlite_version,
        "noCheckpointOnClose": NO_CKPT is not None,
    }


def plain_dir(path, what):
    path = Path(path).absolute()
    if path != path.resolve() or path == Path(path.anchor):
        raise ProtocolError(what + " must be a non-root path without symlinks")
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ProtocolError(what + " must be a directory")
    return path


def chain(path):
    ids = set()
    for item in (path, *path.parents):
        info = item.stat()
        ids.add((info.st_dev, info.st_ino))
    return ids


def isolate(path, others, what):
    own = path.stat()
    for other in others:
        other = Path(other).resolve(strict=True)
        info = other.stat()
        if (info.st_dev, info.st_ino) in chain(path) or (own.st_dev, own.st_ino) in chain(other):
            raise ProtocolError(what + " must not be inside, equal to or contain a live or custody path")


def private_dir(path, what, others, empty):
    path = plain_dir(path, what)
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProtocolError(what + " must be owned by the operator with mode 0700")
    isolate(path, others, what)
    if empty and any(path.iterdir()):
        raise ProtocolError(what + " must be a fresh empty directory")
    return path


def unescape(field):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def mount_fstype(path, mountinfo="/proc/self/mountinfo"):
    best, fstype = "", None
    with open(mountinfo, encoding="utf-8", errors="replace") as source:
        for line in source:
            left, _, right = line.partition(" - ")
            point = unescape(left.split()[4])
            inside = point == "/" or str(path) == point or str(path).startswith(point + "/")
            if inside and len(point) >= len(best):
                best, fstype = point, right.split()[0]
    if fstype is None:
        raise ProtocolError("Cannot determine the live filesystem type")
    return fstype


def free_bytes(path):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def regular(path, what):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ProtocolError(what + " is missing") from None
    if not stat.S_ISREG(info.st_mode):
        raise ProtocolError(what + " must be a regular file, not a link")
    return info


def live_preflight(live_db, live_mounts):
    if not live_mounts:
        raise ProtocolError("Pass every Keeper container mount source with --live-mount")
    live_db = Path(live_db).absolute()
    if live_db != live_db.resolve():
        raise ProtocolError("Live database path must not contain symlinks")
    parents = chain(live_db.parent)
    mounts = [Path(mount).resolve(strict=True).stat() for mount in live_mounts]
    if not any((info.st_dev, info.st_ino) in parents for info in mounts):
        raise ProtocolError("Live database is not under a passed Keeper mount source")
    db = regular(live_db, "Live database")
    # A read-only reader creates -wal/-shm when absent; require the running WAL set.
    wal = regular(Path(str(live_db) + "-wal"), "Live WAL (is Keeper running?)")
    regular(Path(str(live_db) + "-shm"), "Live SHM (is Keeper running?)")
    fstype = mount_fstype(live_db.parent)
    if fstype not in LOCAL_FSTYPES:
        raise ProtocolError("Live database is not on a local filesystem; SQLite locking unsafe")
    return live_db, {"liveDbBytes": db.st_size, "liveWalBytes": wal.st_size, "liveFsType": fstype}


def budget(path, data_bytes):
    required = 2 * data_bytes + 2 * GIB
    available = free_bytes(path)
    if available < required:
        raise ProtocolError("Insufficient free space: need 2x(db+wal) + 2 GiB")
    return {"freeBytes": available, "requiredBytes": required}


def preflight(custody, live_db, live_mounts):
    tools = tool_preflight()
    live_db, sizes = live_preflight(live_db, live_mounts)
    custody = private_dir(custody, "Custody directory", live_mounts, empty=True)
    space = budget(custody, sizes["liveDbBytes"] + sizes["liveWalBytes"])
    return custody, live_db, {"preflight": "PASS", **tools, **sizes, **space}


def open_live_source(live_db, busy_seconds):
    # mode=ro: O_RDONLY db fd, so close can never take the exclusive lock a
    # checkpoint, WAL truncate or WAL delete needs. Never immutable=1 here: it
    # ignores the WAL. No SQL or PRAGMA runs on this connection.
    db = sqlite3.connect(Path(live_db).as_uri() + "?mode=ro", uri=True, timeout=busy_seconds)
    if NO_CKPT is not None:
        db.setconfig(NO_CKPT, True)
    return db


def open_sealed(path):
    return sqlite3.connect(Path(path).as_uri() + "?mode=ro&immutable=1", uri=True)


def copy_database(kind, source, dest, busy_seconds):
    src = open_live_source(source, busy_seconds) if kind == "live" else open_sealed(source)
    try:
        os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        dst = sqlite3.connect(dest)
        try:
            # pages=-1 copies in one step under one read transaction: consistent
            # while Keeper keeps writing. BUSY retries are bounded by the parent.
            src.backup(dst, pages=-1, sleep=0.25)
        finally:
            dst.close()
    finally:
        src.close()


def remove_partial(dest):
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(str(dest) + suffix)
        except FileNotFoundError:
            pass


def run_copy(kind, source, dest, busy_seconds, timeout):
    if os.path.lexists(dest):
        raise ProtocolError("Copy destination already exists; use a fresh directory")
    command = [sys.executable, "-B", "-E", "-s", str(Path(__file__).resolve()), "_copy",
               kind, str(source), str(dest), str(busy_seconds)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        remove_partial(dest)
        raise ProtocolError("Online backup exceeded its time budget; partial copy removed") from None
    if result.returncode:
        remove_partial(dest)
        code = result.stdout.decode("ascii", "replace").strip()[:64] or "error"
        raise ProtocolError("Online backup failed (%s); partial copy removed" % code)


def sha256(path, deadline):
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise ProtocolError("Checksum deadline exceeded")
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)


def write_json(path, value):
    write_new(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def read_json(path):
    regular(path, path.name)
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def bounded(value, upper, what):
    if not 0 < value <= upper:
        raise ProtocolError("%s must be within 0-%d seconds" % (what, upper))


def snapshot(custody, live_db, live_mounts, timeout=900, busy=15, verify_timeout=300):
    bounded(timeout, 3600, "Snapshot timeout")
    bounded(busy, 60, "Busy timeout")
    bounded(verify_timeout, 300, "Verification timeout")
    custody, live_db, checks = preflight(custody, live_db, live_mounts)
    dest = custody / SNAPSHOT
    started = utc_now()
    run_copy("live", live_db, dest, busy, timeout)
    finished = utc_now()
    try:
        regular(Path(str(live_db) + "-wal"), "Live WAL after snapshot")
        regular(Path(str(live_db) + "-shm"), "Live SHM after snapshot")
        gate.snapshot(custody, SNAPSHOT)
        info = gate.inspect_snapshot(dest, time.monotonic() + verify_timeout)
        dest.chmod(0o400)
    except (gate.GateError, ProtocolError):
        remove_partial(dest)
        raise
    record = {
        "step": "snapshot", "method": SNAPSHOT_METHOD, "startedAt": started,
        "finishedAt": finished, "sha256": info["sha256"], "bytes": info["bytes"],
        "applicationTables": info["tables"], "integrity": "ok", **checks,
    }
    write_json(custody / SNAPSHOT_RECEIPT, record)
    return record


def restore(custody, stage, live_mounts, timeout=900, verify_timeout=300):
    bounded(timeout, 3600, "Restore timeout")
    bounded(verify_timeout, 300, "Verification timeout")
    tools = tool_preflight()
    custody = private_dir(custody, "Custody directory", live_mounts, empty=False)
    if (custody / RESTORE_RECEIPT).exists():
        raise ProtocolError("This custody already holds a restore; start a new custody")
    source = gate.snapshot(custody, SNAPSHOT)
    recorded = read_json(custody / SNAPSHOT_RECEIPT)
    deadline = time.monotonic() + verify_timeout
    if sha256(source, deadline) != (recorded["sha256"], recorded["bytes"]):
        raise ProtocolError("Retained snapshot no longer matches its receipt")
    stage = private_dir(stage, "Stage directory", [*live_mounts, custody], empty=True)
    space = budget(stage, recorded["bytes"])
    write_new(stage / ".keeper-rehearsal", MARKER)
    started = utc_now()
    with open(source, "rb") as src, open(stage / "backup.db", "xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    os.chmod(stage / "backup.db", 0o600)
    deadline = time.monotonic() + verify_timeout
    if sha256(stage / "backup.db", deadline) != (recorded["sha256"], recorded["bytes"]):
        raise ProtocolError("Staged backup copy differs from the retained snapshot")
    run_copy("sealed", source, stage / "restored.db", 15, timeout)
    finished = utc_now()
    gate.snapshot(stage, "backup.db")
    gate.snapshot(stage, "restored.db")
    digest, size = sha256(stage / "restored.db", time.monotonic() + verify_timeout)
    record = {
        "step": "restore", "method": RESTORE_METHOD, "startedAt": started,
        "finishedAt": finished, "restoredSha256": digest, "restoredBytes": size,
        "stagedBackupSha256": recorded["sha256"], "python": tools["python"],
        "sqlite": tools["sqlite"], **space,
    }
    write_json(custody / RESTORE_RECEIPT, record)
    return record


def receipt(custody, service_version, commit, image_digest):
    if not re.fullmatch(r"\d+\.\d+\.\d+", service_version):
        raise ProtocolError("Service version must look like 1.15.7")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ProtocolError("Commit must be a full 40-hex SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise ProtocolError("Image digest must be sha256:<64 hex>, without registry path")
    custody = plain_dir(custody, "Custody directory")
    if stat.S_IMODE(custody.lstat().st_mode) != 0o700:
        raise ProtocolError("Custody directory must have mode 0700")
    taken = read_json(custody / SNAPSHOT_RECEIPT)
    restored = read_json(custody / RESTORE_RECEIPT)
    verdict = read_json(custody / GATE_RESULT)
    if verdict.get("copyRestoration") != "PASS":
        raise ProtocolError("Restore gate did not PASS")
    expected = (taken["sha256"], taken["bytes"], taken["applicationTables"])
    if (verdict["backupSha256"], verdict["sizeBytes"], verdict["applicationTables"]) != expected:
        raise ProtocolError("Gate result does not describe the retained snapshot")
    if (restored["restoredSha256"], restored["stagedBackupSha256"]) != (taken["sha256"],) * 2:
        raise ProtocolError("Restored file does not match the retained snapshot")
    keep = ("method", "startedAt", "finishedAt", "python", "sqlite")
    record = {
        "receipt": "KEEPER_SNAPSHOT_RESTORE_V1",
        "origin": {"service": "cpa-usage-keeper", "version": service_version,
                   "commit": commit, "imageDigest": image_digest},
        "snapshot": {**{k: taken[k] for k in keep}, "sha256": taken["sha256"],
                     "bytes": taken["bytes"], "applicationTables": taken["applicationTables"],
                     "integrity": taken["integrity"], "liveDbBytes": taken["liveDbBytes"],
                     "liveWalBytes": taken["liveWalBytes"], "liveFsType": taken["liveFsType"],
                     "noCheckpointOnClose": taken["noCheckpointOnClose"]},
        "restore": {**{k: restored[k] for k in keep}, "sha256": restored["restoredSha256"],
                    "bytes": restored["restoredBytes"]},
        "gate": {k: verdict[k] for k in GATE_KEYS},
    }
    text = json.dumps(record, sort_keys=True)
    if "/" in text or "\\" in text:
        raise ProtocolError("Receipt would contain a path separator")
    write_json(custody / RECEIPT, record)
    return record


def discard_stage(stage, custody, live_mounts):
    stage = private_dir(stage, "Stage directory", [*live_mounts, custody], empty=False)
    marker = regular(stage / ".keeper-rehearsal", "Rehearsal marker")
    if marker.st_size != len(MARKER) or (stage / ".keeper-rehearsal").read_bytes() != MARKER:
        raise ProtocolError("Rehearsal marker missing or invalid; refusing to remove")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ProtocolError("Platform rmtree follows symlinks; remove the stage by hand")
    shutil.rmtree(stage)
    return {"stageDiscarded": True, "custodyRetained": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "snapshot", "restore", "discard-stage"):
        command = commands.add_parser(name)
        command.add_argument("--live-mount", action="append", default=[], required=True)
        command.add_argument("--custody", type=Path, required=True)
        if name in ("preflight", "snapshot"):
            command.add_argument("--live-db", type=Path, required=True)
        if name in ("restore", "discard-stage"):
            command.add_argument("--stage", type=Path, required=True)
        if name in ("snapshot", "restore"):
            command.add_argument("--timeout", type=float, default=900)
            command.add_argument("--verify-timeout", type=float, default=300)
        if name == "snapshot":
            command.add_argument("--busy-timeout", type=float, default=15)
    command = commands.add_parser("receipt")
    command.add_argument("--custody", type=Path, required=True)
    command.add_argument("--service-version", required=True)
    command.add_argument("--commit", required=True)
    command.add_argument("--image-digest", required=True)
    copy = commands.add_parser("_copy")
    for name in ("kind", "source", "dest"):
        copy.add_argument(name)
    copy.add_argument("busy", type=float)
    args = parser.parse_args(argv)
    if args.command == "_copy":
        try:
            copy_database(args.kind, args.source, args.dest, args.busy)
        except Exception as error:
            print(type(error).__name__ + ":" + str(getattr(error, "sqlite_errorname", "")))
            return 1
        return 0
    try:
        if args.command == "preflight":
            result = preflight(args.custody, args.live_db, args.live_mount)[2]
        elif args.command == "snapshot":
            result = snapshot(args.custody, args.live_db, args.live_mount, args.timeout,
                              args.busy_timeout, args.verify_timeout)
        elif args.command == "restore":
            result = restore(args.custody, args.stage, args.live_mount, args.timeout,
                             args.verify_timeout)
        elif args.command == "receipt":
            result = receipt(args.custody, args.service_version, args.commit, args.image_digest)
        else:
            result = discard_stage(args.stage, args.custody, args.live_mount)
    except (ProtocolError, gate.GateError) as error:
        print(json.dumps({"result": "FAIL", "step": args.command, "reason": str(error)}))
        return 1
    except (OSError, KeyError, ValueError, sqlite3.Error) as error:
        code = errno.errorcode.get(getattr(error, "errno", None) or 0, type(error).__name__)
        print(json.dumps({"result": "FAIL", "step": args.command, "reason": "Unavailable: " + code}))
        return 1
    print(json.dumps({"result": "PASS", "step": args.command, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
