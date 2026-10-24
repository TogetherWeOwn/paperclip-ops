"""Read-only/resource acceptance using real SQLite and denied I/O boundaries.

The fixed contract, not a source-text grep, sets expected outcomes. SQLite
fault injection below keeps the real evaluator/SQL/engine; only the external
boundary or deadline is perturbed. No implementation or real service writes.

Run with test_convergence_watches; both files are Test Author-owned.
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from test_convergence_watches import (
    MODULE, ROOT, START, END, WATCH_SOURCES,
    cap, encode, failure, review, run, scratch, snapshot, transition, watch,
)


def load_module():
    spec = importlib.util.spec_from_file_location("convergence_acceptance_subject", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicFunctionTests(unittest.TestCase):
    def test_public_functions_evaluate_real_findings_without_mutating_input(self):
        module = load_module()
        value = snapshot(workspace_observations=[{"id": "obs", "at": START, "coherent": False}])
        original = copy.deepcopy(value)
        loaded = module.load_export(encode(value))
        self.assertEqual(loaded, value)
        report = module.evaluate(loaded)
        self.assertEqual(watch(report, "workspace_incoherence")["count"], 1)
        self.assertIn("workspace_incoherence: 1", module.render_digest(report))
        self.assertEqual(value, original)
        self.assertEqual(loaded, original)
        self.assertEqual(module.evaluate(loaded), report)
        self.assertEqual(module.render_digest(module.evaluate(snapshot())), "")

    def test_loader_rejects_ambiguous_json_before_evaluation(self):
        module = load_module()
        data = encode(snapshot())
        invalid = [data + b'{}', b'\xff',
                   data.replace(b'"complete":true', b'"complete":true,"complete":false', 1),
                   data.replace(b'"start":10000', b'"start":NaN', 1)]
        for raw in invalid:
            with self.subTest(raw=raw[:80]):
                with self.assertRaises((ValueError, UnicodeError)):
                    module.load_export(raw)

    def test_evaluate_rejects_invalid_manifest_instead_of_healthy_report(self):
        module = load_module()
        for field, invalid in (("company_id", "invalid"), ("start", True),
                               ("end", END + 1), ("schema", "future")):
            with self.subTest(field=field):
                value = snapshot()
                value[field] = invalid
                with self.assertRaises(ValueError):
                    module.evaluate(value)

    def test_sql_like_grouping_keys_are_bound_data_not_statements(self):
        marker = "x'); ATTACH DATABASE '/private/not-a-real-db' AS x; --"
        value = snapshot(
            run_intervals=[run("r1", START, agent=marker), run("r2", START, agent=marker)],
            cap_intervals=[cap(agent=marker)],
            reviews=[review("a", 1, endpoint=marker), review("b", START, endpoint=marker)],
        )
        report = load_module().evaluate(value)
        self.assertEqual(watch(report, "over_cap_starts")["count"], 2)
        self.assertEqual(watch(report, "duplicate_review_heads")["count"], 1)
        self.assertNotIn(marker, json.dumps(report))


class SQLiteSafetyTests(unittest.TestCase):
    def test_real_connection_is_memory_only_query_only_and_restricts_authority(self):
        module = load_module()
        real_connect = sqlite3.connect
        connections = []
        configurations = []

        class RecordingConnection(sqlite3.Connection):
            def set_authorizer(self, callback):
                configurations.append((self.execute("PRAGMA query_only").fetchone()[0],
                                       self.execute("PRAGMA temp_store").fetchone()[0]))
                return super().set_authorizer(callback)

        def connect(database, *args, **kwargs):
            # Refuse before real connect: this test must never open a disk DB.
            if database != ":memory:":
                raise AssertionError("evaluator attempted a non-memory SQLite database")
            db = real_connect(database, *args, factory=RecordingConnection, **kwargs)
            connections.append(db)
            return db

        with mock.patch.object(module.sqlite3, "connect", side_effect=connect):
            report = module.evaluate(snapshot())
        self.assertEqual(len(connections), 1)
        self.assertEqual(configurations, [(1, 2)], "query_only ON and temp_store MEMORY")
        self.assertTrue(all(row["count"] == 0 for row in report["results"]))
        db = connections[0]
        try:
            self.assertEqual(db.execute("SELECT 7").fetchone()[0], 7)
            for statement in ("CREATE TABLE unsafe (id INTEGER)",
                              "ATTACH DATABASE ':memory:' AS unsafe",
                              "PRAGMA query_only=OFF",
                              "BEGIN", "SELECT load_extension('/not-a-real-extension')"):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.DatabaseError):
                        db.execute(statement)
        finally:
            db.close()

    def test_sqlite_execution_fault_is_unknown_and_preserves_other_finding(self):
        module = load_module()
        real_connect = sqlite3.connect

        class FaultingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("SELECT") and ":run_failures" in sql:
                    raise sqlite3.OperationalError("SYNTHETIC-PRIVATE-SQL-ERROR")
                return super().execute(sql, parameters)

        def connect(database, *args, **kwargs):
            if database != ":memory:":
                raise AssertionError("non-memory database")
            return real_connect(database, *args, factory=FaultingConnection, **kwargs)

        value = snapshot(workspace_observations=[{"id": "obs", "at": START, "coherent": False}])
        with mock.patch.object(module.sqlite3, "connect", side_effect=connect):
            report = module.evaluate(value)
        row = watch(report, "stdin_deadline_failures")
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["count"])
        self.assertEqual(watch(report, "workspace_incoherence")["count"], 1)
        self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(report))
        self.assertIn("UNKNOWN", module.render_digest(report))

    def test_real_vm_budget_exhaustion_does_not_turn_into_zero(self):
        module = load_module()
        # 5,000 valid transitions, distinct issues, no matching done/blocked
        # pairs: a full result would be 0, not permission to return 0 after
        # truncating the fixed query. This drives the actual SQLite VM budget.
        rows = [transition("d" + str(i), START, 1, "done", "done-issue-" + str(i))
                for i in range(2500)]
        rows += [transition("b" + str(i), START + 1, 1, "blocked", "blocked-issue-" + str(i))
                 for i in range(2500)]
        report = module.evaluate(snapshot(status_transitions=rows))
        row = watch(report, "rapid_reblock")
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["count"])
        for name in WATCH_SOURCES:
            if name != "rapid_reblock":
                self.assertEqual(watch(report, name)["count"], 0)

    def test_elapsed_query_deadline_interrupts_before_claiming_a_count(self):
        module = load_module()
        rows = [failure(str(i)) for i in range(200)]
        tick = 0

        def expired_clock():
            nonlocal tick
            tick += 3
            return tick

        # No waiting: each progress callback observes that its two-second
        # deadline has elapsed. Other empty watches may legitimately finish
        # before a callback; the populated failure SELECT must be UNKNOWN.
        with mock.patch.object(module.time, "monotonic", side_effect=expired_clock):
            report = module.evaluate(snapshot(run_failures=rows))
        row = watch(report, "stdin_deadline_failures")
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["count"])

    def test_read_only_evaluation_attempts_no_network_subprocess_state_or_secret_io(self):
        # Audit hooks live only in this bounded child. Deny actual authority,
        # not import/source text; imports may read installed Python modules.
        code = r'''
import importlib.util, json, os, subprocess, sys
from pathlib import Path
from unittest import mock

def audit(event, args):
    if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn')) or event == 'os.system':
        raise AssertionError('external side effect: ' + event)
    if event in ('os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.chmod', 'os.truncate'):
        raise AssertionError('state mutation: ' + event)
    if event == 'open':
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            raise AssertionError('file write')
    if event == 'sqlite3.connect' and args[0] != ':memory:':
        raise AssertionError('external database')

sys.addaudithook(audit)
spec = importlib.util.spec_from_file_location('subject', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
value = json.loads(sys.stdin.buffer.read())
with mock.patch.object(module.os, 'getenv', side_effect=AssertionError('secret/environment lookup')):
    report = module.evaluate(module.load_export(json.dumps(value).encode()))
    assert 'UNKNOWN' not in module.render_digest(report)
assert report['runtime_qualified'] is False
assert {row['watch']: row['count'] for row in report['results']} == {
    'rapid_reblock': 1, 'over_cap_starts': 2, 'workspace_incoherence': 1,
    'duplicate_review_heads': 1, 'stdin_deadline_failures': 1,
}
print('read-only evaluation verified')
'''
        value = snapshot(
            status_transitions=[transition("d", START, 1, "done"),
                                transition("b", START + 1, 2, "blocked")],
            run_intervals=[run("a", START), run("b", START)], cap_intervals=[cap()],
            workspace_observations=[{"id": "obs", "at": START, "coherent": False}],
            reviews=[review("old", 1), review("new", START)],
            run_failures=[failure("failed", error="no stdin data received in 3s")],
        )
        result = subprocess.run([sys.executable, "-B", "-c", code, str(MODULE)],
                                input=encode(value).decode(), capture_output=True,
                                text=True, timeout=8, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "read-only evaluation verified\n")
        self.assertEqual(result.stderr, "")


class OpenedDescriptorTests(unittest.TestCase):
    def test_descriptor_swap_to_pipe_fails_before_reading_even_with_open_writer(self):
        # The named file is regular; the opened descriptor is not. A pathname
        # check (or blocking read before fstat) would hang on this live pipe.
        code = r'''
import contextlib, importlib.util, io, json, os, sys
from unittest import mock
spec = importlib.util.spec_from_file_location('subject', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
r, w = os.pipe()
seen = []
def swapped_open(path, flags, *args, **kwargs):
    seen.append(flags)
    return r
out = io.StringIO()
try:
    with mock.patch.object(module.os, 'open', side_effect=swapped_open), contextlib.redirect_stdout(out):
        result = module.main(['--snapshot', sys.argv[2]])
    assert result == 2, out.getvalue()
    assert len(seen) == 1 and seen[0] & os.O_NONBLOCK
    if hasattr(os, 'O_NOFOLLOW'):
        assert seen[0] & os.O_NOFOLLOW
    assert 'Traceback' not in out.getvalue()
    assert sys.argv[2] not in out.getvalue()
finally:
    os.close(w)
'''
        with scratch() as directory:
            path = Path(directory) / "regular.json"
            path.write_bytes(encode(snapshot()))
            result = subprocess.run([sys.executable, "-B", "-c", code, str(MODULE), str(path)],
                                    capture_output=True, text=True, timeout=8, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_open_descriptor_size_growth_still_obeys_read_byte_limit(self):
        module = load_module()
        # A regular descriptor whose fstat was stale: content grew after the
        # check. Bounded read + load_export must still reject the 8 MiB + 1.
        raw = encode(snapshot())
        with scratch() as directory:
            path = Path(directory) / "grown.json"
            path.write_bytes(raw + b' ' * (8 * 1024 * 1024 + 1 - len(raw)))
            actual_stat = os.stat(path)
            fields = list(actual_stat)
            fields[6] = len(raw)  # st_size; retain actual regular-file mode.
            stale_stat = os.stat_result(fields)
            out = io.StringIO()
            with mock.patch.object(module.os, "fstat", return_value=stale_stat), \
                    contextlib.redirect_stdout(out):
                result = module.main(["--snapshot", str(path)])
        self.assertEqual(result, 2, out.getvalue())
        self.assertNotIn(str(path), out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())


if __name__ == "__main__":
    unittest.main()
