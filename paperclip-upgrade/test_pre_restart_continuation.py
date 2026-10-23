#!/usr/bin/env python3
"""Independent pre-restart acceptance fixtures; no Docker daemon or host actions.

CI executes the shipped SELECT verbatim in SQLite with PostgreSQL's JSON ->>
text semantics supplied as an operator. --postgres additionally executes the
predicate cases on temporary tables on agent-testdb, never an env-supplied DSN.
The shell cases fake only external commands; the entire guard runs unchanged.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SQL = HERE / "pending-decisions.sql"
LEGACY = HERE / "pre-restart-check.sh"
CANONICAL = REPO / "ops/paperclip/pre-restart-check.sh"
POSTGRES = "--postgres" in sys.argv
COMPANY = "00000000-0000-4000-8000-000000000001"
OTHER_COMPANY = "00000000-0000-4000-8000-000000000002"
ORIGIN = "00000000-0000-4000-8000-000000000003"


def fixture(status="paused", *, policy="wake_origin_agent", metadata=None,
            decision_status="pending", expires="2000-01-01T00:00:00Z",
            agent_company=COMPANY, origin=ORIGIN):
    return {
        "agents": [[ORIGIN, agent_company, status]],
        "decisions": [["00000000-0000-4000-8000-000000000004", COMPANY,
                       origin, policy,
                       json.dumps({"continuationPending": True} if metadata is None else metadata),
                       decision_status, expires]],
    }


SCHEMA = (
    "agents (id text, company_id text, status text)",
    "decisions (id text, company_id text, origin_agent_id text, "
    "continuation_policy text, metadata {json_type}, status text, expires_at {time_type})",
)


def pg_json_text(document, key):
    """SQLite's built-in ->> returns integer 1 for true; PostgreSQL returns text."""
    value = json.loads(document).get(key) if document is not None else None
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def sqlite_rows(data, sql):
    with closing(sqlite3.connect(":memory:")) as db:
        db.create_function("->>", 2, pg_json_text)
        for table in SCHEMA:
            db.execute("CREATE TABLE " + table.format(json_type="text", time_type="text"))
        db.executemany("INSERT INTO agents VALUES (?,?,?)", data["agents"])
        db.executemany("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)", data["decisions"])
        db.execute("PRAGMA query_only = ON")
        return db.execute(sql).fetchall()


class PredicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = None
        if POSTGRES:
            import psycopg2
            if any(os.environ.get(key) for key in ("PGPASSWORD", "DATABASE_URL", "PGSERVICE")):
                raise RuntimeError("fixture PostgreSQL proof takes no credential or service DSN")
            # Explicit test-only target: never accept an env-supplied database.
            cls.pg = psycopg2.connect(host="agent-testdb", port=5432,
                                      user="agent_test", password="", dbname="postgres", connect_timeout=3)
            cls.addClassCleanup(cls.pg.close)
            with cls.pg.cursor() as cur:
                cur.execute("SET search_path TO pg_temp")
                for table in SCHEMA:
                    cur.execute("CREATE TEMP TABLE " + table.format(
                        json_type="jsonb", time_type="timestamptz"))
            cls.pg.commit()
            cls.pg.set_session(readonly=True)

    def rows(self, data):
        sql = SQL.read_text()
        if self.pg is None:
            return sqlite_rows(data, sql)
        with self.pg.cursor() as cur:
            # In a read-only transaction PostgreSQL permits DML on temporary
            # tables only; search_path=pg_temp prevents touching product rows.
            cur.execute("DELETE FROM decisions")
            cur.execute("DELETE FROM agents")
            cur.executemany("INSERT INTO agents VALUES (%s,%s,%s)", data["agents"])
            cur.executemany("INSERT INTO decisions VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            data["decisions"])
            cur.execute(sql)
            return cur.fetchall()

    def assert_count(self, data, count):
        self.assertEqual(self.rows(data), [(f"pending_wake_origin_decisions|{count}",)])

    def test_each_disallowed_origin_is_counted(self):
        # Dropping any one state from the predicate must fail its own fixture.
        for state in ("paused", "terminated", "pending_approval"):
            with self.subTest(state=state):
                self.assert_count(fixture(state), 1)

    def test_eligible_origins_are_excluded(self):
        for state in ("idle", "active", "running", "error"):
            with self.subTest(state=state):
                self.assert_count(fixture(state), 0)

    def test_empty_database_is_zero(self):
        self.assert_count({"agents": [], "decisions": []}, 0)

    def test_nonpending_metadata_is_excluded(self):
        for metadata in ({"continuationPending": False}, {}, {"continuationPending": None}):
            with self.subTest(metadata=metadata):
                self.assert_count(fixture(metadata=metadata), 0)

    def test_other_continuation_policies_are_excluded(self):
        for policy in ("none", "wake_assignee", None):
            with self.subTest(policy=policy):
                self.assert_count(fixture(policy=policy), 0)

    def test_origin_join_requires_id_and_company(self):
        self.assert_count(fixture(origin="00000000-0000-4000-8000-000000000099"), 0)
        self.assert_count(fixture(agent_company=OTHER_COMPANY), 0)

    def test_expiry_and_decision_status_do_not_hide_pending_continuations(self):
        for state in ("paused", "terminated", "pending_approval"):
            for status in ("pending", "resolved", "expired", "failed"):
                for expires in (None, "2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z"):
                    with self.subTest(origin=state, decision=status, expires=expires):
                        self.assert_count(fixture(state, decision_status=status, expires=expires), 1)

    def test_multiple_pending_rows_are_not_deduplicated_or_limited(self):
        data = fixture()
        second = list(data["decisions"][0])
        second[0] = "00000000-0000-4000-8000-000000000005"
        data["decisions"].append(second)
        self.assert_count(data, 2)


def fake_docker(args):
    """Transport double: record and reject unsafe argv before answering SQL."""
    config = json.loads(Path(os.environ["PRESTART_FIXTURE"]).read_text())
    expected_prefix = ["exec", "-i", "-e", "PGOPTIONS=-c default_transaction_read_only=on",
                       "fixture-db", "sh", "-c"]
    violations = []
    if args[:7] != expected_prefix or len(args) != 8:
        violations.append("expected read-only docker exec, not a host mutation")
    elif args[7] != 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -At -v ON_ERROR_STOP=1 -f -':
        violations.append("psql must disable startup files and stop on SQL errors")
    sql = sys.stdin.read()
    if sql.strip() == SQL.read_text().strip():
        kind = "pending"
    elif "drizzle.__drizzle_migrations" in sql:
        kind = "migrations"
    elif "native_lease_expired" in sql:
        kind = "orphans"
    else:
        kind = "unexpected"
        violations.append("unexpected SQL")
    with Path(os.environ["PRESTART_TRACE"]).open("a") as trace:
        trace.write(json.dumps({"kind": kind, "violations": violations}) + "\n")
    if violations:
        return 99
    if kind == "pending":
        if config.get("mode") in ("sql-error", "transport-error"):
            print("pending_wake_origin_decisions|0")
            print("fixture query failed", file=sys.stderr)
            return 3 if config["mode"] == "sql-error" else 125
        if config.get("mode") == "output":
            sys.stdout.write(config["output"])
        else:
            for row in sqlite_rows(config["data"], sql):
                print(row[0])
    elif kind == "migrations":
        print("1 0280aaaa\n2 0281bbbb" if not config.get("missing_migration") else "1 0280aaaa")
    else:
        print(f"native_lease_expired|{config.get('orphans', 0)}")
    return 0


class ShellGuardTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        tmp = tempfile.TemporaryDirectory(prefix="prestart-acceptance-", dir=scratch)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ("bin", "state", "backup", "data"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "state/drained_at").write_text("2026-01-01T00:00:00Z\n")
        self.dump = self.root / "backup/paperclip.dump"
        self.dump.write_bytes(b"offline fixture dump\n")
        digest = hashlib.sha256(self.dump.read_bytes()).hexdigest()
        (self.root / "backup/SHA256SUMS").write_text(f"{digest}  paperclip.dump\n")
        self.config = {"data": fixture("idle")}
        self.trace = self.root / "trace.jsonl"
        self.config_path = self.root / "fixture.json"
        self.write_bin("docker", 'exec python3 "$PRESTART_TEST_DRIVER" --docker "$@"\n')
        self.write_bin("df", 'printf "Filesystem 1B-blocks Used Available Use%% Mounted\\nfixture 17179869184 0 %s 1%% /\\n" "${PRESTART_AVAIL:-8589934592}"\n')
        self.write_bin("sudo", 'printf "%s" "${PRESTART_ROOT_PATHS:-}"\n')
        self.env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ['PATH']}",
                        DOCKER=str(self.root / "bin/docker"), PAPERCLIP_DATA=str(self.root / "data"),
                        PAPERCLIP_UPGRADE_DB_CONTAINER="fixture-db",
                        PRESTART_TEST_DRIVER=str(Path(__file__).resolve()),
                        PRESTART_FIXTURE=str(self.config_path), PRESTART_TRACE=str(self.trace))
        # Noninteractive shell startup files can overwrite PATH and escape the
        # fixture commands. No user shell configuration belongs in this test.
        for key in ("BASH_ENV", "ENV", "PRESTART_AVAIL", "PRESTART_ROOT_PATHS"):
            self.env.pop(key, None)

    def write_bin(self, name, body):
        path = self.root / "bin" / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        path.chmod(0o700)

    def scripts(self):
        # Missing canonical entry has one explicit failure, not dozens of
        # FileNotFound errors. Once present it runs every guard fixture too.
        return [LEGACY] + ([CANONICAL] if CANONICAL.is_file() else [])

    def args(self):
        return ["--state-dir", str(self.root / "state"), "--backup-dir", str(self.root / "backup"),
                "--max-backup-age-mins", "120", "--ack-orphans", "0",
                "--require", "0280", "--require", "0281"]

    def run_command(self, command):
        self.config_path.write_text(json.dumps(self.config))
        self.trace.write_text("")
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=15)
        calls = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([v for call in calls for v in call["violations"]], [], result.stdout)
        return result, calls

    def guard(self, script, want, extra=()):
        before = self.dump.read_bytes() if self.dump.exists() else None
        result, calls = self.run_command(["bash", str(script), *self.args(), *extra])
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, want, f"{script}: {output}")
        self.assertEqual([c["kind"] for c in calls].count("pending"), 1, output)
        self.assertEqual(self.dump.read_bytes() if self.dump.exists() else None, before,
                         "guard must report, never repair the backup")
        if want == 0:
            self.assertEqual([c["kind"] for c in calls], ["migrations", "orphans", "pending"], output)
        else:
            self.assertIn("FAIL:", output)
        return output

    def test_canonical_entry_point_runs_full_guard(self):
        self.assertTrue(CANONICAL.is_file(), "missing canonical ops/paperclip/pre-restart-check.sh")
        self.guard(CANONICAL, 0)

    def test_pending_origins_block_through_actual_sql(self):
        for script in self.scripts():
            for state in ("paused", "terminated", "pending_approval"):
                with self.subTest(script=script, state=state):
                    self.config["data"] = fixture(state)
                    self.guard(script, 1)

    def test_eligible_and_no_pending_cases_pass(self):
        controls = [fixture("idle"), fixture(metadata={"continuationPending": False}),
                    fixture(policy="none"), {"agents": [], "decisions": []}]
        for script in self.scripts():
            for data in controls:
                with self.subTest(script=script, data=data):
                    self.config["data"] = data
                    self.guard(script, 0)

    def test_sql_and_transport_failures_block(self):
        for script in self.scripts():
            for mode in ("sql-error", "transport-error"):
                with self.subTest(script=script, mode=mode):
                    self.config["mode"] = mode
                    self.guard(script, 1)

    def test_empty_malformed_and_extra_rows_block(self):
        outputs = ("", "\n", "garbage\n", "pending_wake_origin_decisions|\n",
                   "pending_wake_origin_decisions|-1\n", "pending_wake_origin_decisions|0x0\n",
                   "pending_wake_origin_decisions|1.0\n", "other_gate|0\n",
                   "pending_wake_origin_decisions|0|extra\n",
                   "pending_wake_origin_decisions|0\npending_wake_origin_decisions|0\n",
                   "pending_wake_origin_decisions|0\nother_gate|0\n")
        for script in self.scripts():
            for output in outputs:
                with self.subTest(script=script, output=output):
                    self.config.update(mode="output", output=output)
                    self.guard(script, 1)

    def test_positive_transport_counts_cannot_wrap_to_zero(self):
        for script in self.scripts():
            for count in ("1", "2", "18446744073709551616"):
                with self.subTest(script=script, count=count):
                    self.config.update(mode="output", output=f"pending_wake_origin_decisions|{count}\n")
                    self.guard(script, 1)

    def test_orphan_ack_does_not_acknowledge_continuations(self):
        self.config.update(data=fixture("pending_approval"), orphans=7)
        for script in self.scripts():
            with self.subTest(script=script):
                self.guard(script, 1, ["--ack-orphans", "7"])

    def test_no_continuation_ack_or_force_flag_bypasses_gate(self):
        self.config["data"] = fixture("paused")
        for script in self.scripts():
            for flag in ("--ack-pending-decisions", "--ack-continuations", "--force"):
                with self.subTest(script=script, flag=flag):
                    result, _ = self.run_command(["bash", str(script), *self.args(), flag, "1"])
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_force_environment_does_not_bypass_gate(self):
        self.config["data"] = fixture("terminated")
        self.env["FORCE"] = "1"
        for script in self.scripts():
            with self.subTest(script=script):
                self.guard(script, 1)

    def test_missing_drained_marker_still_blocks(self):
        (self.root / "state/drained_at").unlink()
        for script in self.scripts():
            with self.subTest(script=script):
                self.guard(script, 1)

    def test_corrupted_and_stale_backups_still_block(self):
        original = self.dump.read_bytes()
        for mode in ("corrupt", "stale"):
            self.dump.write_bytes(original + b"tampered" if mode == "corrupt" else original)
            if mode == "stale":
                os.utime(self.dump, (1, 1))
            for script in self.scripts():
                with self.subTest(script=script, mode=mode):
                    self.guard(script, 1)

    def test_missing_required_migration_still_blocks(self):
        self.config["missing_migration"] = True
        for script in self.scripts():
            with self.subTest(script=script):
                self.guard(script, 1)

    def test_unacknowledged_orphans_still_block(self):
        self.config["orphans"] = 2
        for script in self.scripts():
            with self.subTest(script=script):
                self.guard(script, 1)
                self.guard(script, 0, ["--ack-orphans", "2"])

    def test_low_disk_headroom_still_blocks(self):
        self.env["PRESTART_AVAIL"] = "1073741824"
        for script in self.scripts():
            with self.subTest(script=script):
                self.guard(script, 1)

    def test_root_paths_remain_blocking_only_when_requested(self):
        self.env["PRESTART_ROOT_PATHS"] = "/fixture/root-owned\n"
        for script in self.scripts():
            with self.subTest(script=script):
                self.guard(script, 0)
                self.guard(script, 1, ["--block-on-root"])

    def test_legacy_restart_stops_before_restart_side_effects(self):
        # Execute the shipped restart function verbatim, with earlier host
        # boundaries inert. A sentinel immediately past precheck proves that
        # the negative case stops for the guard, not an unrelated prerequisite.
        source = (HERE / "drain.sh").read_text()
        match = re.search(r"^cmd_restart\(\) \{\n.*?^\}", source, re.M | re.S)
        self.assertIsNotNone(match, "cannot locate the legacy restart consumer")
        harness = self.root / "restart-harness.sh"
        harness.write_text('''#!/usr/bin/env bash
set -uo pipefail
check_auth_file() { :; }; open_state() { :; }; need() { :; }; none() { :; }
drain_still_on() { :; }; inflight_check() { :; }
compose_check() { :; }; compose_no_legacy_overlay() { :; }
st() { printf '%s/%s' "$STATE_DIR" "$1"; }
image_id() { printf '%s' "$EXPECT_IMAGE_ID"; }
fail() { printf 'FAIL: %s\\n' "$*"; exit 1; }
mark() { printf '%s\\n' "$1" >> "$PRESTART_EFFECTS"; }
image_mig_hashes() { printf 'past-precheck\\n' >> "$PRESTART_EFFECTS"; exit 73; }
compose_up() { printf 'compose-up\\n' >> "$PRESTART_EFFECTS"; exit 74; }
STATE_DIR="$PRESTART_STATE"; LOG="$STATE_DIR/drain.log"
IMAGE=fixture-image; EXPECT_IMAGE_ID="sha256:$(printf 'a%.0s' {1..64})"
PRECHECK="$PRESTART_GUARD"; MAX_BACKUP_AGE_MINS=120; ACK_ORPHANS=0; REQUIRE=(0280 0281)
''' + match.group(0) + '\ncmd_restart\n')
        # cmd_restart expects the dump in state/backup, just like drain.sh.
        (self.root / "state/backup").symlink_to(self.root / "backup", target_is_directory=True)
        effects = self.root / "effects"
        self.env.update(PRESTART_STATE=str(self.root / "state"),
                        PRESTART_GUARD=str(LEGACY), PRESTART_EFFECTS=str(effects))
        for state, want in (("idle", 73), ("paused", 1), ("terminated", 1), ("pending_approval", 1)):
            with self.subTest(state=state):
                effects.write_text("")
                self.config["data"] = fixture(state)
                result, calls = self.run_command(["bash", str(harness)])
                self.assertIn("pending", [c["kind"] for c in calls], result.stdout + result.stderr)
                self.assertEqual(result.returncode, want, result.stdout + result.stderr)
                self.assertEqual(effects.read_text(), "hashes-ahead-src\npast-precheck\n" if state == "idle" else "")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--docker":
        sys.exit(fake_docker(sys.argv[2:]))
    if POSTGRES:
        sys.argv.remove("--postgres")
        unittest.main(defaultTest="PredicateTests", verbosity=2)
    else:
        unittest.main(verbosity=2)
