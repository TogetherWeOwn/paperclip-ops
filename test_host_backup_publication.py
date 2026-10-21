#!/usr/bin/env python3
"""Offline acceptance for host_db_backup.sh; no runtime implementation lives here.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_host_backup_publication

The actual Bash entrypoint runs with a credential-free environment and a closed
PATH. Only synthetic podman replies and fixture-scoped filesystem adapters are
available. Real gzip, grep, find, stat, rename, unlink and flock do the work;
adapters inject failures at those external boundaries, never decide a verdict.
Every process group has a deadline. Unexpected commands are recorded and fail
closed, even when the entrypoint ignores their exit status.

Minimal v1 --json contract pinned by these tests (stdout is JSON/JSONL, text
may remain on stderr): schema_version=1, cycle_id, archive_id (null before
publication), outcome, stage, error_code (null on success), retryable (bool),
next_action (nonempty string), backup_landed (bool), retention={status, pruned}.
Outcomes: success, error, partial, planned, skipped. Retention status: complete,
failed, not_started. Unknown enumeration has pruned=null, not a guessed zero.
Stage/error codes are stable machine tokens, not prescribed diagnostic prose.
IDs correlate all records in a cycle; different cycles have different IDs.
This interface describes evidence only, never authorization or an auto-retry.

Builder handoff: this entire acceptance file is immutable. Corrections return
to its test author; bundle the unchanged file with runtime fixes in one PR.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "host_db_backup.sh"
STAMP = "20300102-030405"
FINAL_NAME = f"paperclip-{STAMP}-hostcron.sql.gz"
MARKER = b"-- PostgreSQL database dump complete"
FOOTER = b"\n--\n" + MARKER + b"\n--\n\n"
# Hand-built SQL with deterministic, incompressible comment padding. No SQL is
# executed; the bytes only travel through the actual dump/verification pipeline.
PADDING = b"".join(
    b"-- " + hashlib.sha256(f"fixture-{i}".encode()).hexdigest().encode() + b"\n"
    for i in range(128)
)
HEADER = b"-- PostgreSQL database dump\n\nCREATE TABLE public.sample (value text);\n"
GOOD = HEADER + PADDING + b"\nCOPY public.sample (value) FROM stdin;\nhello\n\\.\n" + FOOTER


# Generated only in a run-owned temporary directory. This is an external-command
# adapter, not a copy of the backup algorithm. Unknown argv is always a failure.
ADAPTER = r'''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["FIXTURE_ROOT"]).resolve()
name = Path(sys.argv[0]).name
args = sys.argv[1:]
config = json.loads((root / "adapters.json").read_text())

def record(kind, **fields):
    fd = os.open(root / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(dict(kind=kind, command=name, args=args, **fields)) + "\n").encode())
    finally:
        os.close(fd)

def deny(reason):
    record("unexpected", reason=reason)
    print("fixture refused: " + reason, file=sys.stderr)
    sys.exit(97)

def scoped(value):
    p = Path(value)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.resolve().is_relative_to(root):
        deny("path outside fixture")
    return p

record("call")
mode = os.environ.get("FIXTURE_MODE", "good")
if name == "podman":
    ctr = "fixture-db"
    if args == ["container", "exists", ctr]:
        sys.exit(0)
    if args[:2] != ["exec", ctr]:
        deny("unexpected container operation")
    cmd = args[2:]
    if cmd == ["sh", "-c", 'echo "$POSTGRES_USER"']:
        print("fixture_user_sensitive")
    elif cmd == ["sh", "-c", 'echo "$POSTGRES_DB"']:
        print("fixture_database_sensitive")
    elif (len(cmd) == 10 and cmd[:9] == ["psql", "-X", "-A", "-t", "-U", "fixture_user_sensitive", "-d", "fixture_database_sensitive", "-c"]
          and cmd[9].strip() == "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'COPY %TO stdout%' AND pid <> pg_backend_pid();"):
        print("1" if mode == "courtesy" else "0")
    elif cmd == ["pg_dump", "-U", "fixture_user_sensitive", "-d", "fixture_database_sensitive", "--no-owner", "--no-privileges"]:
        sys.stdout.buffer.write((root / "dump.sql").read_bytes())
        sys.stdout.buffer.flush()
        sys.exit(74 if mode == "interrupted" else 0)
    else:
        deny("unexpected DB command (restore/SQL execution is forbidden)")
    sys.exit(0)

# Restrict every path-taking adapter before delegating to a real utility.
if name in ("rm", "mv", "stat", "find", "gzip", "mktemp", "mkdir", "touch", "ln"):
    operands = [a for a in args if not a.startswith("-")]
    if name == "find":
        if not args:
            deny("find without a fixture root")
        scoped(args[0])
        if any(a in args for a in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf")):
            deny("mutating find action")
        if mode in ("find_failure", "find_partial_failure"):
            if mode == "find_partial_failure":
                sys.stdout.buffer.write(str(root / "backups" / "paperclip-20200101-000000-hostcron.sql.gz").encode() + b"\0")
            print("fixture traversal failure", file=sys.stderr)
            sys.exit(73)
    else:
        if name == "stat":
            operands = [a for a in operands if a not in ("%s",)]
        for value in operands:
            scoped(value)
    if name == "mv" and mode == "publish_failure":
        print("fixture atomic publication failure", file=sys.stderr)
        sys.exit(72)
    if name == "rm" and mode == "delete_failure":
        if any(Path(a).name == "paperclip-20200101-000000-hostcron.sql.gz" for a in operands):
            print("fixture retention deletion failure", file=sys.stderr)
            sys.exit(71)
    if name == "gzip" and args == ["-c"] and mode == "corrupt_gzip":
        # A compressor that reports success but leaves a damaged stream proves
        # that gzip integrity checking remains independent of pipeline status.
        import gzip
        sys.stdout.buffer.write(gzip.compress(sys.stdin.buffer.read(), mtime=0)[:-8])
        sys.exit(0)
elif name == "flock":
    if args != ["-n", "9"]:
        deny("unexpected lock operation")
elif name == "date":
    args = ["--date=" + os.environ.get("FIXTURE_DATE", "2030-01-02 03:04:05 UTC"), *args]
elif name in ("grep", "awk", "tail", "head", "wc"):
    # Permit alternative verifier implementations, not just the baseline grep.
    # These tools may read stdin/fixture files, never execute another command.
    if name == "awk":
        import re
        if any(re.search(r"\b(system|getline)\b", a) for a in args):
            deny("awk process or external input")
        if any(a in ("-f", "--file") for a in args):
            deny("external awk program")
    for value in args:
        if value.startswith("/") and not (name == "awk" and value.rstrip().endswith("}")):
            scoped(value)
elif name == "basename":
    if len(args) != 1:
        deny("unexpected basename argv")
    scoped(args[0])
elif name == "cat":
    for a in args:
        scoped(a)
else:
    deny("command not allowed")

if name not in config:
    deny("no approved executable")
os.execv(config[name], [name, *args])
'''


# DEBUG traps also cover absolute commands (which a closed PATH alone cannot).
# Permit shell syntax/builtins and our exact adapter vocabulary, nothing else.
BASH_GUARD = r'''
set -T
fixture_guard() {
  local word="${BASH_COMMAND%%[[:space:]]*}"
  if [[ "$word" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] && declare -F -- "$word" >/dev/null 2>&1; then return 0; fi
  case "$word" in
    source) [[ "$BASH_COMMAND" == 'source "$BASH_ENV"' || "$BASH_COMMAND" == 'source "$entrypoint" "$@"' ]] && return 0 ;;
    exec) [[ "$BASH_COMMAND" == 'exec 9>'* ]] && return 0 ;;
    command) [[ "$BASH_COMMAND" == 'command -v '* ]] && return 0 ;;
    podman|gzip|flock|date|find|stat|rm|mv|basename|grep|awk|tail|head|wc|cat|mktemp|mkdir|touch|ln|echo|printf|read|exit|shift|local|return|usage|trap|set|true|false|test|'['|'[['|'(('*|'(('|'((('|'!'|':') return 0 ;;
    *=*|for|while|if|then|do|done|case|esac|'{'|'}') return 0 ;;
  esac
  printf 'unexpected shell command: %s\n' "$word" >> "$FIXTURE_ROOT/guard-errors"
  exit 97
}
trap fixture_guard DEBUG
command_not_found_handle() {
  printf 'unexpected command: %s\n' "$1" >> "$FIXTURE_ROOT/guard-errors"
  return 97
}
'''


class BackupPublicationAcceptance(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR") or os.environ.get("TMPDIR")
        self.temp = tempfile.TemporaryDirectory(prefix="backup-publication-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.backups = self.work / "backups"
        self.locks = self.work / "locks"
        self.bin = self.work / "bin"
        for directory in (self.backups, self.locks, self.bin, self.work / "home"):
            directory.mkdir()
        self.final = self.backups / FINAL_NAME
        self.old = self.backups / "paperclip-20200101-000000-hostcron.sql.gz"
        self.server = self.backups / "paperclip-20200101-000000.sql.gz"
        self.young = self.backups / "paperclip-20300101-000000-hostcron.sql.gz"
        self.unrelated = self.backups / "unrelated.partial"
        for path in (self.old, self.server, self.young, self.unrelated):
            path.write_bytes(b"preexisting:" + path.name.encode())
        aged = time.time() - 10 * 86400
        os.utime(self.old, (aged, aged))
        os.utime(self.server, (aged, aged))
        (self.backups / "nested").mkdir()
        self.nested = self.backups / "nested" / self.old.name
        self.nested.write_bytes(b"nested archive is outside maxdepth 1")
        os.utime(self.nested, (aged, aged))
        self.before = {p: p.read_bytes() for p in (self.old, self.server, self.young, self.unrelated, self.nested)}
        tools = ("gzip", "flock", "date", "find", "stat", "rm", "mv", "basename", "grep", "awk", "tail", "head", "wc", "cat", "mktemp", "mkdir", "touch", "ln")
        real = {name: shutil.which(name) for name in tools}
        self.assertTrue(all(real.values()), f"missing fixture dependencies: {real}")
        (self.work / "adapters.json").write_text(json.dumps(real))
        adapter = self.bin / "adapter"
        adapter.write_text(f"#!{sys.executable}\n" + ADAPTER)
        adapter.chmod(0o755)
        for name in (*tools, "podman"):
            (self.bin / name).symlink_to(adapter.name)
        (self.work / "guard.sh").write_text(BASH_GUARD)
        (self.work / "dump.sql").write_bytes(GOOD)
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.work / "home"),
            "TMPDIR": str(self.work), "LC_ALL": "C", "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1", "BASH_ENV": str(self.work / "guard.sh"),
            "FIXTURE_ROOT": str(self.work), "FIXTURE_MODE": "good",
            "PAPERCLIP_DB_CTR": "fixture-db", "PAPERCLIP_DB_BACKUP_DIR": str(self.backups),
            "PAPERCLIP_DB_BACKUP_RETENTION_DAYS": "2", "HOST_DB_BACKUP_LOCK_DIR": str(self.locks),
        }

    def events(self):
        path = self.work / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_cycle(self, *args, mode="good", dump=GOOD):
        (self.work / "dump.sql").write_bytes(dump)
        self.env["FIXTURE_MODE"] = mode
        process = subprocess.Popen(
            # Explicit bootstrap does not rely on Bash honoring startup files.
            # Source the untouched entrypoint with its real argv/BASH_SOURCE.
            ["/bin/bash", "-c", 'source "$BASH_ENV" || exit 97; entrypoint=$1; shift; source "$entrypoint" "$@"', "fixture", str(ENTRYPOINT), *args], cwd=self.work,
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            self.fail(f"entrypoint exceeded 12s; group killed: {stdout}\n{stderr}")
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        self.assertFalse([e for e in self.events() if e["kind"] == "unexpected"], result.stderr)
        guard = self.work / "guard-errors"
        self.assertFalse(guard.exists(), guard.read_text() if guard.exists() else "")
        return result

    def output(self, result):
        return result.stdout + result.stderr

    def archives(self):
        return set(self.backups.glob("paperclip-*-hostcron.sql.gz")) - {self.old, self.young}

    def assert_boundaries(self, *, old_exists=True):
        for path, data in self.before.items():
            if path == self.old and not old_exists:
                self.assertFalse(path.exists(), "eligible old archive was not pruned")
            else:
                self.assertTrue(path.exists(), f"unowned/protected artifact removed: {path.name}")
                self.assertEqual(path.read_bytes(), data)

    def assert_not_published(self, result):
        observed = {
            "nonzero": result.returncode != 0,
            "pass_reported": "host_db_backup=PASS" in self.output(result),
            "new_archives": sorted(p.name for p in self.archives()),
            "retention_started": any(e["command"] == "find" for e in self.events()),
            "owned_partials": sorted(str(p.relative_to(self.backups)) for p in self.backups.rglob("*") if p.is_file() and p not in self.before),
        }
        self.assertEqual(observed, {"nonzero": True, "pass_reported": False, "new_archives": [], "retention_started": False, "owned_partials": []}, self.output(result))
        self.assert_boundaries()

    def receipt(self, result, outcome, landed, retention):
        self.assertTrue(result.stdout.strip(), f"no JSON receipt: {self.output(result)}")
        try:
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        except ValueError as error:
            self.fail(f"--json stdout is not JSON/JSONL: {error}: {result.stdout}")
        self.assertTrue(rows)
        for row in rows:
            self.assertIsInstance(row, dict)
            self.assertEqual(row.get("schema_version"), 1)
            self.assertIsInstance(row.get("cycle_id"), str)
            self.assertRegex(row["cycle_id"], r"^[A-Za-z0-9._:-]{1,160}$")
            self.assertEqual(row["cycle_id"], rows[-1].get("cycle_id"))
        row = rows[-1]
        self.assertTrue({"schema_version", "cycle_id", "archive_id", "outcome", "stage", "error_code", "retryable", "next_action", "backup_landed", "retention"}.issubset(row), row)
        for token in ("stage", "next_action"):
            self.assertIsInstance(row.get(token), str)
            self.assertTrue(row[token].strip())
        self.assertRegex(row["stage"], r"^[a-z][a-z0-9_]*$")
        self.assertIs(type(row.get("retryable")), bool)
        self.assertEqual(row.get("outcome"), outcome)
        self.assertIs(row.get("backup_landed"), landed)
        self.assertEqual(row.get("retention", {}).get("status"), retention)
        self.assertIn("pruned", row["retention"])
        if landed:
            self.assertIsInstance(row.get("archive_id"), str)
            self.assertRegex(row["archive_id"], r"^[A-Za-z0-9._:-]{1,160}$")
        else:
            self.assertIsNone(row.get("archive_id"))
        if outcome in ("partial", "error"):
            self.assertIsInstance(row.get("error_code"), str)
            self.assertRegex(row["error_code"], r"^[A-Za-z][A-Za-z0-9_]*$")
            self.assertNotEqual(result.returncode, 0, self.output(result))
        else:
            self.assertIn("error_code", row)
            self.assertIsNone(row["error_code"])
            self.assertEqual(result.returncode, 0, self.output(result))
        for secret in ("fixture_user_sensitive", "fixture_database_sensitive"):
            self.assertNotIn(secret, result.stdout)
        return row

    def test_positive_control_publishes_real_gzip_then_prunes_only_eligible_files(self):
        result = self.run_cycle()
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("host_db_backup=PASS", self.output(result))
        self.assertEqual(self.archives(), {self.final})
        self.assertGreaterEqual(self.final.stat().st_size, 1024)
        self.assertEqual(gzip.decompress(self.final.read_bytes()), GOOD)
        self.assert_boundaries(old_exists=False)
        self.assertEqual({p for p in self.backups.rglob("*") if p.is_file() and p not in self.before}, {self.final})
        commands = [e["command"] for e in self.events()]
        self.assertLess(commands.index("mv"), commands.index("find"))

    def test_atomic_publication_failure_never_passes_or_starts_retention(self):
        self.assert_not_published(self.run_cycle(mode="publish_failure"))

    def test_existing_destination_is_never_overwritten_or_pruned(self):
        original = b"unrelated preexisting final archive"
        self.final.write_bytes(original)
        result = self.run_cycle()
        self.assertEqual(hashlib.sha256(self.final.read_bytes()).hexdigest(), hashlib.sha256(original).hexdigest(), "publication overwrote an existing archive")
        self.assertNotEqual(result.returncode, 0, self.output(result))
        self.assertNotIn("host_db_backup=PASS", self.output(result))
        self.assertFalse(any(e["command"] == "find" for e in self.events()))
        self.assert_boundaries()

    def test_publication_failure_preserves_preexisting_partial_not_owned_by_cycle(self):
        collision = self.backups / (FINAL_NAME + ".partial")
        collision.write_bytes(b"another invocation owns these bytes")
        result = self.run_cycle(mode="publish_failure")
        self.assertTrue(collision.exists(), "preexisting temporary artifact was removed")
        self.assertEqual(hashlib.sha256(collision.read_bytes()).hexdigest(), hashlib.sha256(b"another invocation owns these bytes").hexdigest(), "preexisting temporary artifact was overwritten")
        self.assertNotEqual(result.returncode, 0, self.output(result))
        self.assert_boundaries()

    def test_valid_terminal_trailer_with_marker_like_copy_data_is_accepted(self):
        dump = HEADER + PADDING + b"\nCOPY public.sample (value) FROM stdin;\n" + MARKER + b"\na value containing " + MARKER + b"\n\\.\n" + FOOTER
        result = self.run_cycle(dump=dump)
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(gzip.decompress(self.final.read_bytes()), dump)
        self.assertIn("host_db_backup=PASS", self.output(result))

    def test_modern_pg_dump_unrestrict_after_real_footer_is_accepted(self):
        dump = b"\\restrict fixture_token\n" + GOOD + b"\\unrestrict fixture_token\n\n"
        result = self.run_cycle(dump=dump)
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(gzip.decompress(self.final.read_bytes()), dump)

    def test_copy_marker_cannot_substitute_for_missing_terminal_trailer(self):
        dump = HEADER + PADDING + b"\nCOPY public.sample (value) FROM stdin;\n" + MARKER + b"\n\\.\n"
        self.assert_not_published(self.run_cycle(dump=dump))

    def test_data_after_completion_comment_is_not_a_terminal_trailer(self):
        dump = GOOD + b"COPY public.sample (value) FROM stdin;\nunexpected tail\n"
        self.assert_not_published(self.run_cycle(dump=dump))

    def test_missing_footer_is_refused_above_compressed_size_floor(self):
        dump = HEADER + PADDING
        self.assertGreater(len(gzip.compress(dump, mtime=0)), 1024)
        self.assert_not_published(self.run_cycle(dump=dump))

    def test_missing_create_table_is_refused_above_compressed_size_floor(self):
        dump = b"-- PostgreSQL database dump\n" + PADDING + FOOTER
        self.assertGreater(len(gzip.compress(dump, mtime=0)), 1024)
        self.assert_not_published(self.run_cycle(dump=dump))

    def test_valid_but_undersized_archive_is_refused(self):
        dump = HEADER + FOOTER
        self.assertLess(len(gzip.compress(dump, mtime=0)), 1024)
        self.assert_not_published(self.run_cycle(dump=dump))

    def test_interrupted_dump_is_refused_even_if_it_emitted_a_complete_trailer(self):
        self.assert_not_published(self.run_cycle(mode="interrupted"))

    def test_corrupt_gzip_is_refused_even_when_compressor_returns_zero(self):
        self.assert_not_published(self.run_cycle(mode="corrupt_gzip"))

    def test_retention_traversal_failure_is_nonzero_and_preserves_published_archive(self):
        result = self.run_cycle(mode="find_failure")
        self.assertEqual(gzip.decompress(self.final.read_bytes()), GOOD)
        self.assert_boundaries()
        self.assertNotEqual(result.returncode, 0, "traversal failure was reported as a successful cycle: " + self.output(result))
        self.assertRegex(self.output(result), r"(?i)(partial|error|fail)")
        self.assertNotIn("pruned=0", self.output(result), "unknown enumeration is not a measured zero")

    def test_retention_deletion_failure_is_nonzero_and_preserves_both_archives(self):
        result = self.run_cycle(mode="delete_failure")
        self.assertEqual(gzip.decompress(self.final.read_bytes()), GOOD)
        self.assert_boundaries()
        self.assertNotEqual(result.returncode, 0, "deletion failure was reported as a successful cycle: " + self.output(result))
        self.assertRegex(self.output(result), r"(?i)(partial|error|fail)")

    def test_dry_run_does_not_create_lock_artifacts_or_call_podman(self):
        before = self.snapshot()
        result = self.run_cycle("--dry-run")
        observed = {"exit": result.returncode, "files": self.snapshot(), "podman": any(e["command"] == "podman" for e in self.events()), "mutators": [e["command"] for e in self.events() if e["command"] in ("flock", "rm", "mv", "find", "mktemp", "mkdir", "touch", "ln")]}
        self.assertEqual(observed, {"exit": 0, "files": before, "podman": False, "mutators": []}, self.output(result))
        self.assertIn("DRY-RUN", self.output(result))

    def snapshot(self):
        return {str(p.relative_to(self.work)): (p.stat().st_mtime_ns, p.read_bytes()) for base in (self.backups, self.locks) for p in base.rglob("*") if p.is_file()}

    def test_bad_flags_and_values_never_activate_a_cycle(self):
        for args in (("--bogus",), ("--retention-days", "0"), ("--retention-days", "--dry-run"), ("--backup-dir",), ("--container", "bad;command"), ("--json", "--bogus")):
            with self.subTest(args=args):
                result = self.run_cycle(*args)
                self.assertEqual(result.returncode, 2, self.output(result))
                self.assertFalse(list(self.locks.iterdir()))
                self.assertFalse(any(e["command"] == "podman" for e in self.events()))
                self.assert_boundaries()

    def test_held_lock_skips_without_podman_dump_or_retention(self):
        with (self.locks / "host_db_backup.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_cycle()
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("SKIP", self.output(result))
        self.assertFalse(any(e["command"] in ("podman", "find", "mv") for e in self.events()))
        self.assertFalse(self.archives())
        self.assert_boundaries()

    def test_live_copy_courtesy_skip_does_not_dump_or_prune(self):
        result = self.run_cycle(mode="courtesy")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("SKIP", self.output(result))
        self.assertFalse(any(e["command"] in ("find", "mv") or "pg_dump" in e["args"] for e in self.events()))
        self.assertFalse(self.archives())
        self.assert_boundaries()

    def test_json_success_reports_correlated_nonsecret_identifiers_and_measured_retention(self):
        result = self.run_cycle("--json")
        row = self.receipt(result, "success", True, "complete")
        self.assertEqual(row["retention"]["pruned"], 1)
        self.assertEqual(gzip.decompress(self.final.read_bytes()), GOOD)
        self.assert_boundaries(old_exists=False)
        self.env["FIXTURE_DATE"] = "2030-01-02 03:04:06 UTC"
        second = self.receipt(self.run_cycle("--json"), "success", True, "complete")
        self.assertNotEqual(row["cycle_id"], second["cycle_id"])
        self.assertNotEqual(row["archive_id"], second["archive_id"])
        self.assertEqual(second["retention"]["pruned"], 0)

    def test_json_publication_failure_is_error_without_landed_archive_or_retention(self):
        result = self.run_cycle("--json", mode="publish_failure")
        self.receipt(result, "error", False, "not_started")
        self.assert_not_published(result)

    def test_json_failed_enumeration_is_partial_with_unknown_not_zero_pruned(self):
        result = self.run_cycle("--json", mode="find_failure")
        row = self.receipt(result, "partial", True, "failed")
        self.assertIsNone(row["retention"]["pruned"])
        self.assertEqual(gzip.decompress(self.final.read_bytes()), GOOD)
        self.assert_boundaries()

    def test_json_partially_read_enumeration_is_still_unknown(self):
        result = self.run_cycle("--json", mode="find_partial_failure")
        row = self.receipt(result, "partial", True, "failed")
        self.assertIsNone(row["retention"]["pruned"])
        self.assertEqual(gzip.decompress(self.final.read_bytes()), GOOD)
        self.assertEqual(self.server.read_bytes(), self.before[self.server])

    def test_json_failed_deletion_is_partial_and_preserves_valid_archive(self):
        result = self.run_cycle("--json", mode="delete_failure")
        row = self.receipt(result, "partial", True, "failed")
        self.assertEqual(row["retention"]["pruned"], 0)
        self.assertEqual(gzip.decompress(self.final.read_bytes()), GOOD)
        self.assert_boundaries()

    def test_json_verification_error_codes_remain_stable_between_cycles(self):
        first = self.run_cycle("--json", dump=HEADER + PADDING)
        a = self.receipt(first, "error", False, "not_started")
        self.assert_not_published(first)
        self.env["FIXTURE_DATE"] = "2030-01-02 03:04:06 UTC"
        second = self.run_cycle("--json", dump=HEADER + PADDING)
        b = self.receipt(second, "error", False, "not_started")
        self.assertEqual((a["stage"], a["error_code"]), (b["stage"], b["error_code"]))
        self.assertNotEqual(a["cycle_id"], b["cycle_id"])
        self.assert_not_published(second)

    def test_json_dump_verification_and_publication_errors_identify_distinct_stages(self):
        rows = []
        for mode, dump in (("interrupted", GOOD), ("good", HEADER + PADDING), ("publish_failure", GOOD)):
            result = self.run_cycle("--json", mode=mode, dump=dump)
            rows.append(self.receipt(result, "error", False, "not_started"))
            self.assert_not_published(result)
        self.assertEqual(len({r["stage"] for r in rows}), 3, rows)
        self.assertEqual(len({r["error_code"] for r in rows}), 3, rows)

    def test_json_courtesy_skip_is_not_a_published_backup(self):
        result = self.run_cycle("--json", mode="courtesy")
        self.receipt(result, "skipped", False, "not_started")
        self.assertFalse(self.archives())
        self.assert_boundaries()
        self.assertFalse(any(e["command"] in ("find", "mv") or "pg_dump" in e["args"] for e in self.events()))

    def test_json_bad_arguments_are_refused_without_activating_cycle(self):
        result = self.run_cycle("--json", "--retention-days", "0")
        self.receipt(result, "error", False, "not_started")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.events())
        self.assertFalse(list(self.locks.iterdir()))
        self.assert_boundaries()

    def test_fixture_guard_refuses_unexpected_and_absolute_host_commands(self):
        for command in ("curl --version", "/usr/bin/curl --version", "exec /usr/bin/curl --version", "command /usr/bin/curl --version"):
            with self.subTest(command=command):
                result = subprocess.run(["/bin/bash", "-c", 'source "$BASH_ENV" || exit 97; ' + command], env=self.env, cwd=self.work, capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 97, result.stdout + result.stderr)
                self.assertIn("unexpected shell command", (self.work / "guard-errors").read_text())

    def test_fixture_podman_refuses_unexpected_sql_instead_of_contacting_container(self):
        result = subprocess.run([str(self.bin / "podman"), "exec", "fixture-db", "psql", "-c", "SELECT 1"], env=self.env, cwd=self.work, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 97, result.stdout + result.stderr)
        self.assertTrue(any(e["kind"] == "unexpected" for e in self.events()))

    def test_json_dry_run_is_a_no_write_plan_without_live_calls(self):
        before = self.snapshot()
        result = self.run_cycle("--json", "--dry-run")
        self.receipt(result, "planned", False, "not_started")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(e["command"] in ("podman", "flock", "rm", "mv", "find", "mktemp", "mkdir", "touch", "ln") for e in self.events()))

    def test_help_documents_machine_output_and_dry_run_without_activating_cycle(self):
        result = self.run_cycle("--help")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("--json", self.output(result))
        self.assertIn("--dry-run", self.output(result))
        self.assertFalse(self.events())
        self.assertFalse(list(self.locks.iterdir()))
        self.assert_boundaries()


if __name__ == "__main__":
    unittest.main()
