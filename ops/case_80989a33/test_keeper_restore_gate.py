import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import keeper_restore_gate as gate


class RestoreGateTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        if not scratch:
            raise RuntimeError("Set PAPERCLIP_RUN_SCRATCH_DIR for synthetic test fixtures")
        self.temp = tempfile.TemporaryDirectory(prefix="keeper-offline-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".keeper-rehearsal").write_bytes(b"KEEPER_REHEARSAL_V1\n")
        with contextlib.closing(sqlite3.connect(self.root / "backup.db")) as db:
            db.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, secret TEXT, total INTEGER)")
            db.execute("INSERT INTO ledger VALUES (1, 'DO_NOT_PRINT_SYNTHETIC_SECRET', 97)")
            db.commit()
        shutil.copyfile(self.root / "backup.db", self.root / "restored.db")

    def assertFails(self):
        with self.assertRaises((gate.GateError, OSError)):
            gate.verify(self.root)

    def test_positive_control_and_no_snapshot_writes(self):
        before = {p.name: gate.identity(p) for p in self.root.iterdir()}
        result = gate.verify(self.root)
        self.assertEqual(result["copyRestoration"], "PASS")
        self.assertEqual(result["applicationTables"], 1)
        self.assertEqual(result["liveBackupConsistency"], "NOT_PROVEN")
        self.assertEqual(result["migrationRehearsal"], "NOT_RUN")
        self.assertEqual(result["runtimeCompatibility"], "NOT_RUN")
        self.assertNotIn("DO_NOT_PRINT_SYNTHETIC_SECRET", json.dumps(result))
        self.assertEqual(before, {p.name: gate.identity(p) for p in self.root.iterdir()})

    def test_changed_ledger_rejected(self):
        with contextlib.closing(sqlite3.connect(self.root / "restored.db")) as db:
            db.execute("UPDATE ledger SET total=98")
            db.commit()
        self.assertFails()

    def test_non_sqlite_identical_files_rejected(self):
        for name in ("backup.db", "restored.db"):
            (self.root / name).write_bytes(b"not a database")
        self.assertFails()

    def test_corrupt_identical_files_rejected(self):
        for name in ("backup.db", "restored.db"):
            (self.root / name).write_bytes(b"SQLite format 3\0" + b"\0" * 200)
        self.assertFails()

    def test_empty_database_rejected(self):
        for name in ("backup.db", "restored.db"):
            (self.root / name).unlink()
            with contextlib.closing(sqlite3.connect(self.root / name)) as db:
                db.execute("VACUUM")
        self.assertFails()

    def test_sidecars_rejected_on_either_copy(self):
        for name in ("backup.db", "restored.db"):
            for suffix in ("-wal", "-shm", "-journal"):
                with self.subTest(name=name, suffix=suffix):
                    sidecar = self.root / (name + suffix)
                    sidecar.touch()
                    self.assertFails()
                    sidecar.unlink()

    def test_dangling_sidecar_symlink_rejected(self):
        (self.root / "backup.db-wal").symlink_to(self.root / "missing")
        self.assertFails()

    def test_symlink_rejected(self):
        (self.root / "restored.db").unlink()
        (self.root / "restored.db").symlink_to(self.root / "backup.db")
        self.assertFails()

    def test_hardlink_rejected(self):
        (self.root / "restored.db").unlink()
        os.link(self.root / "backup.db", self.root / "restored.db")
        self.assertFails()

    def test_directory_instead_of_snapshot_rejected(self):
        (self.root / "restored.db").unlink()
        (self.root / "restored.db").mkdir()
        self.assertFails()

    def test_missing_or_wrong_marker_rejected(self):
        marker = self.root / ".keeper-rehearsal"
        marker.unlink()
        self.assertFails()
        marker.write_bytes(b"wrong")
        self.assertFails()

    def test_nonregular_marker_rejected_before_read(self):
        marker = self.root / ".keeper-rehearsal"
        marker.unlink()
        os.mkfifo(marker)
        self.assertFails()
        marker.unlink()
        marker.symlink_to(self.root / "backup.db")
        self.assertFails()

    def test_oversized_marker_rejected(self):
        (self.root / ".keeper-rehearsal").write_bytes(b"KEEPER_REHEARSAL_V1\nextra")
        self.assertFails()

    def test_elapsed_deadline_after_inspection_rejected(self):
        inspect = gate.inspect_snapshot
        expired = False
        def monotonic():
            return 61 if expired else 0
        def expire(path, deadline):
            nonlocal expired
            result = inspect(path, deadline)
            if path.name == "restored.db":
                expired = True
            return result
        with patch.object(gate.time, "monotonic", side_effect=monotonic), \
                patch.object(gate, "inspect_snapshot", side_effect=expire):
            self.assertFails()

    def test_symlink_root_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(gate.GateError):
            gate.verify(alias)

    def test_public_directory_rejected(self):
        self.root.chmod(0o755)
        self.assertFails()

    def test_timeout_bounds_and_deadline_rejected(self):
        for timeout in (0, -1, 301, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(gate.GateError):
                gate.verify(self.root, timeout)
        with self.assertRaises(gate.GateError):
            gate.inspect_snapshot(self.root / "backup.db", -1)

    def test_change_to_first_snapshot_during_second_inspection_rejected(self):
        inspect = gate.inspect_snapshot
        def mutate(path, deadline):
            result = inspect(path, deadline)
            if path.name == "restored.db":
                with (self.root / "backup.db").open("ab") as file:
                    file.write(b"changed")
            return result
        with patch.object(gate, "inspect_snapshot", side_effect=mutate):
            self.assertFails()

    def test_sidecar_created_during_inspection_rejected(self):
        inspect = gate.inspect_snapshot
        def mutate(path, deadline):
            result = inspect(path, deadline)
            (self.root / "backup.db-wal").touch()
            return result
        with patch.object(gate, "inspect_snapshot", side_effect=mutate):
            self.assertFails()

    def test_cli_pass_and_fail_exit_codes_without_record_values(self):
        for fail in (False, True):
            if fail:
                (self.root / "restored.db").write_bytes(b"invalid")
            output = io.StringIO()
            with patch("sys.argv", ["gate", str(self.root)]), contextlib.redirect_stdout(output):
                code = gate.main()
            self.assertEqual(code, int(fail))
            report = json.loads(output.getvalue())
            self.assertEqual(report["copyRestoration"], "FAIL" if fail else "PASS")
            self.assertNotIn("DO_NOT_PRINT_SYNTHETIC_SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
