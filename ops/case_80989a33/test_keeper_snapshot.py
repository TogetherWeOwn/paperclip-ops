import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import keeper_restore_gate as gate
import keeper_snapshot as ks

SECRET = "DO_NOT_PRINT_SYNTHETIC_SECRET"
COMMIT = "dab6760c66c80b3d32a7b649cfcfe3bf1d7ee4a1"
MOUNT_FSTYPE = ks.mount_fstype
DIGEST = "sha256:" + "f533cd3630da32e31ed0e6833e460f279a6ac2916342d510b077d94c91893c6e"

# Commits rows into the WAL with checkpoints off, then dies without closing:
# app.db, -wal and -shm stay behind exactly as under a running Keeper.
CRASH_WRITER = r"""
import os, sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA wal_autocheckpoint=0")
for _ in range(int(sys.argv[2])):
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO ledger(amount, note, ts) VALUES (1, 'wal', ?)", (time.monotonic_ns(),))
    db.execute("UPDATE totals SET total = total + 1")
    db.execute("COMMIT")
os._exit(0)
"""

# Keeper-like writer: busy_timeout 15000, default auto-checkpoint, commits until told to stop.
LIVE_WRITER = r"""
import os, sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=15)
db.execute("PRAGMA synchronous=NORMAL")
ready = False
while not os.path.exists(sys.argv[2]):
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO ledger(amount, note, ts) VALUES (1, 'live', ?)", (time.monotonic_ns(),))
    db.execute("UPDATE totals SET total = total + 1")
    db.execute("COMMIT")
    if not ready:
        print("ready", flush=True)
        ready = True
os._exit(0)
"""

EXCLUSIVE_HOLDER = r"""
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA locking_mode=EXCLUSIVE")
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE totals SET total = total")
db.execute("COMMIT")
print("ready", flush=True)
time.sleep(60)
"""


def fingerprint(path):
    info = path.stat()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_size, info.st_ino, info.st_mtime_ns)


class KeeperSnapshotTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        if not scratch:
            raise RuntimeError("Synthetic Keeper snapshot tests require a scratch directory")
        self.tmp = tempfile.TemporaryDirectory(prefix="keeper-snapshot-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.live = self.base / "live"
        self.live.mkdir()
        self.db = self.live / "app.db"
        self.custody = self.base / "custody"
        self.custody.mkdir(mode=0o700)
        self.stage = self.base / "stage"
        self.stage.mkdir(mode=0o700)
        self.mounts = [self.live]
        # The scratch filesystem is overlay; the live-fstype allowlist has its own tests.
        patcher = mock.patch.object(ks, "mount_fstype", return_value="ext4")
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, filler_bytes=0):
        with contextlib.closing(sqlite3.connect(self.db, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, amount INTEGER, note TEXT, ts INTEGER)")
            db.execute("CREATE TABLE totals(id INTEGER PRIMARY KEY, total INTEGER)")
            db.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY, data BLOB)")
            db.execute("INSERT INTO totals VALUES (1, 7)")
            db.execute("INSERT INTO ledger(amount, note, ts) VALUES (7, ?, 0)", (SECRET,))
            for _ in range(filler_bytes // (1 << 20)):
                db.execute("INSERT INTO filler(data) VALUES (randomblob(1048576))")

    def crash_write(self, rows):
        subprocess.run([sys.executable, "-B", "-c", CRASH_WRITER, str(self.db), str(rows)], check=True)
        for suffix in ("-wal", "-shm"):
            self.assertTrue(Path(str(self.db) + suffix).is_file())

    def live_set(self):
        return [self.db, Path(str(self.db) + "-wal")]

    def snapshot_rows(self, path):
        with contextlib.closing(ks.open_sealed(path)) as db:
            count, amount = db.execute("SELECT count(*), sum(amount) FROM ledger").fetchone()
            total = db.execute("SELECT total FROM totals").fetchone()[0]
        return count, amount, total

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ks.main([str(arg) for arg in argv])
        text = out.getvalue()
        self.assertNotIn(SECRET, text)
        self.assertNotIn(self.tmp.name, text)
        return code, json.loads(text)

    def snapshot(self, **kwargs):
        return ks.snapshot(self.custody, self.db, self.mounts, **kwargs)

    def test_snapshot_carries_wal_only_rows_and_leaves_live_wal_untouched(self):
        self.seed()
        self.crash_write(40)
        before = [fingerprint(path) for path in self.live_set()]
        control = self.base / "cp-control.db"
        control.write_bytes(self.db.read_bytes())
        record = self.snapshot()
        self.assertEqual(before, [fingerprint(path) for path in self.live_set()])
        self.assertEqual(self.snapshot_rows(self.custody / ks.SNAPSHOT), (41, 47, 47))
        # Positive control: a file copy of app.db alone misses every WAL-only row.
        self.assertEqual(self.snapshot_rows(control), (1, 7, 7))
        self.assertEqual(record["integrity"], "ok")
        self.assertEqual(sorted(p.name for p in self.custody.iterdir()),
                         [ks.SNAPSHOT, ks.SNAPSHOT_RECEIPT])
        self.assertEqual((self.custody / ks.SNAPSHOT).stat().st_mode & 0o777, 0o400)

    def test_read_only_source_alone_never_checkpoints_or_removes_live_wal(self):
        self.seed()
        self.crash_write(25)
        before = [fingerprint(path) for path in self.live_set()]
        dest = self.custody / "inprocess.db"
        with mock.patch.object(ks, "NO_CKPT", None):
            ks.copy_database("live", self.db, dest, 1)
        self.assertEqual(before, [fingerprint(path) for path in self.live_set()])
        self.assertEqual(self.snapshot_rows(dest), (26, 32, 32))

    def test_live_source_connection_rejects_writes(self):
        self.seed()
        self.crash_write(3)
        with contextlib.closing(ks.open_live_source(self.db, 1)) as db:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("INSERT INTO totals VALUES (2, 0)")
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("CREATE TABLE intruder(x)")

    def test_snapshot_is_consistent_while_a_writer_keeps_committing(self):
        self.seed(filler_bytes=32 << 20)
        stop = self.base / "stop-writer"
        writer = subprocess.Popen([sys.executable, "-B", "-c", LIVE_WRITER, str(self.db), str(stop)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(writer.stdout.close)
        self.addCleanup(writer.kill)
        self.assertEqual(writer.stdout.readline().strip(), "ready")
        time.sleep(0.2)
        started = time.monotonic_ns()
        record = self.snapshot()
        finished = time.monotonic_ns()
        self.assertTrue(Path(str(self.db) + "-wal").is_file())
        time.sleep(0.2)
        stop.touch()
        self.assertEqual(writer.wait(timeout=30), 0)
        count, amount, total = self.snapshot_rows(self.custody / ks.SNAPSHOT)
        self.assertEqual(amount, total)
        self.assertGreater(count, 2)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            final = db.execute("SELECT count(*) FROM ledger").fetchone()[0]
            earlier = db.execute("SELECT count(*) FROM ledger WHERE ts < ?", (started,)).fetchone()[0]
            during = db.execute("SELECT count(*) FROM ledger WHERE ts BETWEEN ? AND ?",
                                (started, finished)).fetchone()[0]
        # Single sequential writer: at most one row can be INSERTed (ts <
        # started) but not yet COMMITted when `started` is recorded. If that
        # in-flight row commits after the backup's read point it is invisible
        # to the snapshot, so `earlier` exceeds `count` by at most one.
        self.assertLessEqual(earlier, count + 1)
        self.assertLess(count, final)
        self.assertGreater(during, 0, "the writer was blocked during the online snapshot")
        self.assertEqual(record["integrity"], "ok")

    def test_lock_timeout_removes_partial_copy(self):
        self.seed()
        holder = subprocess.Popen([sys.executable, "-B", "-c", EXCLUSIVE_HOLDER, str(self.db)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        dest = self.custody / ks.SNAPSHOT
        started = time.monotonic()
        with self.assertRaisesRegex(ks.ProtocolError, "time budget"):
            ks.run_copy("live", self.db, dest, 0.5, 3)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(list(self.custody.iterdir()), [])

    def test_failed_copy_reports_without_paths_and_removes_partial(self):
        bogus = self.live / "bogus.db"
        bogus.write_bytes(b"not a database " * 512)
        dest = self.custody / ks.SNAPSHOT
        with self.assertRaises(ks.ProtocolError) as caught:
            ks.run_copy("live", bogus, dest, 1, 30)
        self.assertNotIn("/", str(caught.exception))
        self.assertEqual(list(self.custody.iterdir()), [])

    def test_copy_never_reuses_or_removes_an_existing_destination(self):
        self.seed()
        self.crash_write(1)
        dest = self.custody / ks.SNAPSHOT
        dest.write_bytes(b"occupied")
        with self.assertRaisesRegex(ks.ProtocolError, "already exists"):
            ks.run_copy("live", self.db, dest, 1, 30)
        self.assertEqual(dest.read_bytes(), b"occupied")

    def test_missing_live_sidecars_refuse_before_any_open(self):
        self.seed()
        with self.assertRaisesRegex(ks.ProtocolError, "Live WAL"):
            self.snapshot()
        self.assertFalse(Path(str(self.db) + "-wal").exists())
        self.assertFalse(Path(str(self.db) + "-shm").exists())
        self.assertEqual(list(self.custody.iterdir()), [])

    def test_missing_live_shm_refuses_before_any_open(self):
        self.seed()
        self.crash_write(2)
        shm = Path(str(self.db) + "-shm")
        shm.unlink()
        wal = fingerprint(Path(str(self.db) + "-wal"))
        with self.assertRaisesRegex(ks.ProtocolError, "Live SHM"):
            self.snapshot()
        self.assertFalse(shm.exists())
        self.assertEqual(wal, fingerprint(Path(str(self.db) + "-wal")))
        self.assertEqual(list(self.custody.iterdir()), [])

    def test_custody_refusals(self):
        self.seed()
        self.crash_write(1)
        self.custody.chmod(0o750)
        with self.assertRaisesRegex(ks.ProtocolError, "0700"):
            self.snapshot()
        self.custody.chmod(0o700)
        (self.custody / "stale").write_text("x")
        with self.assertRaisesRegex(ks.ProtocolError, "fresh empty"):
            self.snapshot()
        (self.custody / "stale").unlink()
        link = self.base / "custody-link"
        link.symlink_to(self.custody)
        with self.assertRaisesRegex(ks.ProtocolError, "symlinks"):
            ks.snapshot(link, self.db, self.mounts)
        inside = self.live / "custody"
        inside.mkdir(mode=0o700)
        with self.assertRaisesRegex(ks.ProtocolError, "must not be inside"):
            ks.snapshot(inside, self.db, self.mounts)
        inside.rmdir()
        self.base.chmod(0o700)
        with self.assertRaisesRegex(ks.ProtocolError, "must not be inside"):
            ks.snapshot(self.base, self.db, self.mounts)
        self.assertEqual(list(self.custody.iterdir()), [])

    def test_live_path_refusals(self):
        self.seed()
        self.crash_write(1)
        other = self.base / "other"
        other.mkdir()
        with self.assertRaisesRegex(ks.ProtocolError, "not under a passed"):
            ks.snapshot(self.custody, self.db, [other])
        with self.assertRaisesRegex(ks.ProtocolError, "--live-mount"):
            ks.snapshot(self.custody, self.db, [])
        link = self.live / "link.db"
        link.symlink_to(self.db)
        for suffix in ("-wal", "-shm"):
            Path(str(link) + suffix).symlink_to(str(self.db) + suffix)
        with self.assertRaisesRegex(ks.ProtocolError, "symlinks"):
            ks.snapshot(self.custody, link, self.mounts)
        with mock.patch.object(ks, "mount_fstype", return_value="nfs4"):
            with self.assertRaisesRegex(ks.ProtocolError, "local filesystem"):
                self.snapshot()
        self.assertEqual(list(self.custody.iterdir()), [])

    def test_disk_budget_requires_twice_db_plus_wal_plus_two_gib(self):
        self.seed()
        self.crash_write(5)
        need = 2 * sum(path.stat().st_size for path in self.live_set()) + 2 * ks.GIB
        with mock.patch.object(ks, "free_bytes", return_value=need - 1):
            with self.assertRaisesRegex(ks.ProtocolError, "Insufficient free space"):
                self.snapshot()
        self.assertEqual(list(self.custody.iterdir()), [])
        with mock.patch.object(ks, "free_bytes", return_value=need):
            self.assertEqual(self.snapshot()["requiredBytes"], need)

    def test_mountinfo_parser_takes_longest_prefix_and_unescapes(self):
        info = self.base / "mountinfo"
        info.write_text(
            "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "40 22 0:50 / /srv/keeper\\040data rw - nfs4 host:/x rw\n"
            "41 22 0:51 / /srv/keeper rw - xfs /dev/sdb1 rw\n"
        )
        self.assertEqual(MOUNT_FSTYPE(Path("/srv/keeper data/app"), str(info)), "nfs4")
        self.assertEqual(MOUNT_FSTYPE(Path("/srv/keeper/app"), str(info)), "xfs")
        self.assertEqual(MOUNT_FSTYPE(Path("/srv/keeperish"), str(info)), "ext4")

    def test_end_to_end_restore_gate_receipt_and_stage_rollback(self):
        self.seed(filler_bytes=2 << 20)
        self.crash_write(30)
        before = [fingerprint(path) for path in self.live_set()]
        mounts = [arg for mount in self.mounts for arg in ("--live-mount", mount)]
        code, result = self.run_cli("preflight", "--custody", self.custody, "--live-db", self.db, *mounts)
        self.assertEqual((code, result["preflight"]), (0, "PASS"))
        code, taken = self.run_cli("snapshot", "--custody", self.custody, "--live-db", self.db,
                                   *mounts, "--timeout", 120)
        self.assertEqual(code, 0)
        code, restored = self.run_cli("restore", "--custody", self.custody, "--stage", self.stage,
                                      *mounts, "--timeout", 120)
        self.assertEqual(code, 0)
        self.assertEqual(restored["restoredSha256"], taken["sha256"])
        self.assertEqual(sorted(p.name for p in self.stage.iterdir()),
                         [".keeper-rehearsal", "backup.db", "restored.db"])
        verdict = gate.verify(self.stage, 60)
        self.assertEqual(verdict["copyRestoration"], "PASS")
        self.assertEqual(verdict["backupSha256"], taken["sha256"])
        (self.custody / ks.GATE_RESULT).write_text(json.dumps(verdict) + "\n")
        code, receipt = self.run_cli("receipt", "--custody", self.custody, "--service-version", "1.15.7",
                                     "--commit", COMMIT, "--image-digest", DIGEST)
        self.assertEqual(code, 0)
        text = (self.custody / ks.RECEIPT).read_text()
        for leaked in ("/", SECRET, self.tmp.name):
            self.assertNotIn(leaked, text)
        stored = json.loads(text)
        self.assertEqual(stored["snapshot"]["sha256"], stored["restore"]["sha256"])
        self.assertEqual(stored["gate"]["backupSha256"], stored["snapshot"]["sha256"])
        self.assertEqual(stored["origin"]["version"], "1.15.7")
        self.assertEqual(receipt["receipt"], "KEEPER_SNAPSHOT_RESTORE_V1")
        code, gone = self.run_cli("discard-stage", "--stage", self.stage, "--custody", self.custody, *mounts)
        self.assertEqual((code, gone["stageDiscarded"]), (0, True))
        self.assertFalse(self.stage.exists())
        self.assertTrue((self.custody / ks.SNAPSHOT).is_file())
        self.assertEqual(before, [fingerprint(path) for path in self.live_set()])

    def test_restore_refuses_tampered_snapshot_and_reused_or_live_stage(self):
        self.seed()
        self.crash_write(4)
        self.snapshot()
        inside = self.live / "stage"
        inside.mkdir(mode=0o700)
        with self.assertRaisesRegex(ks.ProtocolError, "must not be inside"):
            ks.restore(self.custody, inside, self.mounts)
        nested = self.custody / "stage"
        nested.mkdir(mode=0o700)
        with self.assertRaisesRegex(ks.ProtocolError, "must not be inside"):
            ks.restore(self.custody, nested, self.mounts)
        (self.stage / "leftover").write_text("x")
        with self.assertRaisesRegex(ks.ProtocolError, "fresh empty"):
            ks.restore(self.custody, self.stage, self.mounts)
        (self.stage / "leftover").unlink()
        sealed = self.custody / ks.SNAPSHOT
        sealed.chmod(0o600)
        with sealed.open("r+b") as target:
            target.seek(200)
            flipped = target.read(1)[0] ^ 0xFF
            target.seek(200)
            target.write(bytes([flipped]))
        with self.assertRaisesRegex(ks.ProtocolError, "no longer matches"):
            ks.restore(self.custody, self.stage, self.mounts)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_receipt_refuses_failed_or_mismatched_gate_and_bad_origin(self):
        self.seed()
        self.crash_write(4)
        self.snapshot()
        ks.restore(self.custody, self.stage, self.mounts)
        verdict = gate.verify(self.stage, 60)
        gate_file = self.custody / ks.GATE_RESULT
        gate_file.write_text(json.dumps({"copyRestoration": "FAIL", "reason": "x"}))
        with self.assertRaisesRegex(ks.ProtocolError, "did not PASS"):
            ks.receipt(self.custody, "1.15.7", COMMIT, DIGEST)
        gate_file.write_text(json.dumps({**verdict, "backupSha256": "0" * 64}))
        with self.assertRaisesRegex(ks.ProtocolError, "retained snapshot"):
            ks.receipt(self.custody, "1.15.7", COMMIT, DIGEST)
        gate_file.write_text(json.dumps(verdict))
        for args in (("1.15", COMMIT, DIGEST), ("1.15.7", COMMIT[:12], DIGEST),
                     ("1.15.7", COMMIT, "ghcr.io/willxup/cpa-usage-keeper@" + DIGEST)):
            with self.assertRaises(ks.ProtocolError):
                ks.receipt(self.custody, *args)
        self.assertFalse((self.custody / ks.RECEIPT).exists())

    def test_discard_stage_requires_marker_and_never_touches_custody(self):
        self.seed()
        self.crash_write(2)
        self.snapshot()
        with self.assertRaisesRegex(ks.ProtocolError, "Rehearsal marker"):
            ks.discard_stage(self.stage, self.custody, self.mounts)
        with self.assertRaisesRegex(ks.ProtocolError, "must not be inside"):
            ks.discard_stage(self.custody, self.custody, self.mounts)
        self.assertTrue(self.stage.is_dir())
        self.assertTrue((self.custody / ks.SNAPSHOT).is_file())


if __name__ == "__main__":
    unittest.main()
